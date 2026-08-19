package linkgate

import (
	"crypto/subtle"
	"fmt"
	"regexp"
	"strings"
	"time"
)

// kvOpOrder is the fixed emission order for per-op-type fields in the logfmt
// line. Matches the plan's sample output and is not alphabetical — it
// mirrors the order operations actually happen in on the redirect hot path.
var kvOpOrder = []string{"open", "exists", "get", "set", "delete", "list_keys"}

type opStat struct {
	count      int
	totalUs    int64
	totalBytes int64
}

// Collector accumulates per-request KV operation timing: per operation type
// (open/exists/get/set/delete/list_keys), a count, total microseconds and
// total bytes moved, plus the single slowest operation seen.
//
// Record's signature is deliberately (opType, namespace, duration, bytes) —
// it has NO parameter that could accept a key. users:session:<token> is a
// live session credential and spin aka logs retains 7 days by default, so a
// key-logging design would put working session tokens in a week-long
// retention window. This is the same structural move PrefixedStore makes by
// having no get_keys method.
//
// Every method is nil-safe, so a nil *Collector behaves as a no-op collector
// — this is what lets collectorFrom return "no tracing" for free with no
// separate no-op type. Deliberately NOT a package-level variable: a shared
// collector would silently interleave concurrent requests' operations into
// one another's line, which is worse than no instrument at all. Each request
// must construct (or omit) its own via NewCollector.
type Collector struct {
	stats         map[string]*opStat
	hasSlow       bool
	slowType      string
	slowNamespace string
	slowUs        int64
}

// NewCollector returns a fresh, empty collector for one request.
func NewCollector() *Collector {
	return &Collector{stats: make(map[string]*opStat)}
}

// Record adds one KV operation's outcome. namespace is "-" for an open
// (which has none) or a namespace like "links"/"analytics" otherwise. bytes
// is the value size moved; pass 0 when not applicable (open, exists).
func (c *Collector) Record(opType, namespace string, dur time.Duration, bytes int) {
	if c == nil {
		return
	}
	us := dur.Microseconds()

	st, ok := c.stats[opType]
	if !ok {
		st = &opStat{}
		c.stats[opType] = st
	}
	st.count++
	st.totalUs += us
	st.totalBytes += int64(bytes)

	if !c.hasSlow || us > c.slowUs {
		c.hasSlow = true
		c.slowUs = us
		c.slowType = opType
		c.slowNamespace = namespace
	}
}

// Totals returns the sums across every operation type: count, total
// microseconds, total bytes moved. A nil collector reports all zeros.
func (c *Collector) Totals() (ops int, us int64, bytes int64) {
	if c == nil {
		return 0, 0, 0
	}
	for _, st := range c.stats {
		ops += st.count
		us += st.totalUs
		bytes += st.totalBytes
	}
	return ops, us, bytes
}

// Namespace classifies a physical KV key into the namespace it belongs to,
// built on the existing LinksPrefix/AnalyticsPrefix constants. Deliberately
// has no users: case — the redirect component never constructs a users key
// (see keys.go), so any key reaching here that isn't links/analytics is
// unrecognised, not a users key silently misclassified as "-".
func Namespace(key string) string {
	switch {
	case strings.HasPrefix(key, LinksPrefix):
		return "links"
	case strings.HasPrefix(key, AnalyticsPrefix):
		return "analytics"
	default:
		return "-"
	}
}

// KVStore is the minimal surface *kv.Store already satisfies
// (spin-go-sdk/v3/kv.Store exposes exactly these three methods with these
// signatures), kept as a local interface so this package imports nothing
// from spin-go-sdk and stays host-testable against a fake.
type KVStore interface {
	Exists(key string) (bool, error)
	Get(key string) ([]byte, error)
	Set(key string, value []byte) error
}

// TimedStore wraps a KVStore, recording each call's duration (and, for
// Get/Set, the value size) into Collector. Collector may be nil, in which
// case every Record call is a no-op and this wrapper costs one extra
// time.Now()/time.Since() pair per call — the off-path cost this plan
// accepts is instead never wrapping at all (see main.go's three request
// paths).
type TimedStore struct {
	Store     KVStore
	Collector *Collector
}

func (t TimedStore) Exists(key string) (bool, error) {
	start := time.Now()
	ok, err := t.Store.Exists(key)
	t.Collector.Record("exists", Namespace(key), time.Since(start), 0)
	return ok, err
}

func (t TimedStore) Get(key string) ([]byte, error) {
	start := time.Now()
	v, err := t.Store.Get(key)
	t.Collector.Record("get", Namespace(key), time.Since(start), len(v))
	return v, err
}

func (t TimedStore) Set(key string, value []byte) error {
	start := time.Now()
	err := t.Store.Set(key, value)
	t.Collector.Record("set", Namespace(key), time.Since(start), len(value))
	return err
}

// Field is one already-rendered key=value pair for the request-specific
// portion of a log line (comp, route, slug, status, err, ...) — deliberately
// plain strings the caller has already route-templated, since this package
// has no notion of what a "route" or "slug" is.
type Field struct {
	Key   string
	Value string
}

// RenderLogLine renders one "ss "-prefixed logfmt line: the caller-supplied
// fields in order, then dur_us, then (if c is non-nil) the KV summary —
// kv_ops/kv_us/kv_bytes, one field per non-zero-count operation type in
// kvOpOrder ("count/total_µs"), and the single slowest operation as
// "type:namespace:µs". Zero-count operation-type fields are omitted
// entirely, never emitted as "=0/0".
func RenderLogLine(fields []Field, dur time.Duration, c *Collector) string {
	var b strings.Builder
	b.WriteString("ss")
	for _, f := range fields {
		fmt.Fprintf(&b, " %s=%s", f.Key, f.Value)
	}
	fmt.Fprintf(&b, " dur_us=%d", dur.Microseconds())

	if c == nil {
		return b.String()
	}

	ops, us, bytes := c.Totals()
	fmt.Fprintf(&b, " kv_ops=%d kv_us=%d kv_bytes=%d", ops, us, bytes)

	for _, opType := range kvOpOrder {
		st, ok := c.stats[opType]
		if !ok || st.count == 0 {
			continue
		}
		fmt.Fprintf(&b, " %s=%d/%d", opType, st.count, st.totalUs)
	}

	if c.hasSlow {
		fmt.Fprintf(&b, " slow=%s:%s:%d", c.slowType, c.slowNamespace, c.slowUs)
	}

	return b.String()
}

// RenderServerTiming renders the Server-Timing header value for a
// token-bearing request: `kv;dur=<ms>;desc="N ops", handler;dur=<ms>`.
// Durations are milliseconds as floats — 80 µs must render as 0.080, never
// 80 — because Server-Timing's dur parameter is defined in milliseconds.
func RenderServerTiming(dur time.Duration, c *Collector) string {
	ops, us, _ := c.Totals()
	kvMs := float64(us) / 1000.0
	handlerMs := float64(dur.Microseconds()) / 1000.0
	return fmt.Sprintf(`kv;dur=%.3f;desc="%d ops", handler;dur=%.3f`, kvMs, ops, handlerMs)
}

// ParseLogLevel maps a raw log_level variable value to a known level. Any
// unrecognised value (including empty/unset) is treated as "off" —
// fail-closed, never raise. Only "off" and "summary" exist today; a future
// "verbose" needs no rename here.
func ParseLogLevel(raw string) string {
	if raw == "summary" {
		return "summary"
	}
	return "off"
}

// TokenMatches reports whether provided matches configured using a
// constant-time comparison. An empty configured token NEVER matches
// anything, including an empty or absent provided value — checked explicitly
// before any comparison, not as an incidental property of
// ConstantTimeCompare (which would happily report two empty byte slices as
// equal). Getting this backwards makes the default configuration "anyone can
// enable tracing", exactly what the token exists to prevent.
func TokenMatches(configured, provided string) bool {
	if configured == "" {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(configured), []byte(provided)) == 1
}

// --- Observable KV failures (docs/plans/observable-kv-failures.md) ---
//
// SanitizeErrorMessage and RenderFailureLine mirror api/obs.py's
// sanitize_error_message and render_failure_line, but the two
// implementations are DELIBERATELY NOT PINNED against each other — unlike
// keys.go's LinksPrefix/AnalyticsPrefix and CountShards, which
// api/tests/test_kvprefix.py pins BECAUSE a divergence there fails
// silently at runtime (the API would write links the redirect path can't
// find). A divergence between these two sanitizers produces two slightly
// differently-shaped log lines and nothing else — there is no shared data
// structure for the two implementations to disagree about, so nothing here
// needs, or gets, a cross-language test. See the plan's Trade-offs #8.
//
// Collector, kvOpOrder, TimedStore, RenderLogLine, RenderServerTiming,
// ParseLogLevel and TokenMatches above are all untouched by this section.

// (a) A key-shaped substring: a leading word, a colon, then one or more
// non-whitespace/quote/paren/bracket characters. The trailing character
// class is what keeps "key-value error: internal server error" intact — a
// colon followed by a space does not match, since the class requires at
// least one non-whitespace character immediately after the colon.
var keyShapedPattern = regexp.MustCompile(`[A-Za-z][A-Za-z0-9_-]*:[^\s'")\]]+`)

// (b) A pbkdf2 hash token.
var hashTokenPattern = regexp.MustCompile(`\S*pbkdf2_sha256\S*`)

// (c) Control characters and newlines.
var controlCharPattern = regexp.MustCompile(`[\x00-\x1f\x7f]`)

// MaxErrorMessageChars mirrors api/obs.py's MAX_ERROR_MESSAGE_CHARS. The two
// known Akamai KV error messages are 17 and 40 characters, so 200 is 5x
// headroom while still bounding an unbounded host string. Raising it needs
// a real observed truncation, per this repo's standing rule for every
// sibling constant.
const MaxErrorMessageChars = 200

// SanitizeErrorMessage sanitizes a raw KV error message for safe inclusion
// in a log line, applying the same three rules (in order) as
// api/obs.py's sanitize_error_message: (a) redact key-shaped substrings to
// "[key:<word>]"; (b) redact any pbkdf2_sha256-bearing token to "[hash]";
// (c) replace control characters/newlines with "_"; then truncate to
// MaxErrorMessageChars. Returns (sanitized, redacted, truncated).
func SanitizeErrorMessage(msg string) (sanitized string, redacted bool, truncated bool) {
	sanitized = keyShapedPattern.ReplaceAllStringFunc(msg, func(match string) string {
		redacted = true
		word := match[:strings.IndexByte(match, ':')]
		return "[key:" + word + "]"
	})

	sanitized = hashTokenPattern.ReplaceAllStringFunc(sanitized, func(string) string {
		redacted = true
		return "[hash]"
	})

	sanitized = controlCharPattern.ReplaceAllString(sanitized, "_")

	if len(sanitized) > MaxErrorMessageChars {
		sanitized = sanitized[:MaxErrorMessageChars]
		truncated = true
	}

	return sanitized, redacted, truncated
}

// RenderFailureLine renders one "ss "-prefixed logfmt line from fields, in
// order, with NOTHING appended after them — deliberately separate from
// RenderLogLine so nothing can ever land after "msg", which every caller
// places last.
func RenderFailureLine(fields []Field) string {
	var b strings.Builder
	b.WriteString("ss")
	for _, f := range fields {
		fmt.Fprintf(&b, " %s=%s", f.Key, f.Value)
	}
	return b.String()
}
