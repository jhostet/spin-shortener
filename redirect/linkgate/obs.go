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

// slugLogSafePattern mirrors api/links.py's CUSTOM_SLUG_PATTERN character
// class, deliberately without its 3-32 length bound (this function only
// needs to reject unsafe bytes, not enforce the shape a slug is created
// with) up to a generous cap: every slug the API ever writes is drawn from
// [A-Za-z0-9_-]+, so anything outside that class reaching this function is
// a probe or an attack against a path that was never a real link, never a
// real one.
var slugLogSafePattern = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)

// SanitizeSlugForLog returns slug unchanged if it is safe to place inside a
// logfmt line's "slug=" field, and a fixed placeholder otherwise.
//
// Unlike SanitizeErrorMessage's msg field, slug is never the last field
// RenderLogLine/RenderFailureLine emit — op, ns, etype and (for the summary
// line) status/dur_us all follow it. A raw request-supplied slug is
// attacker-controlled (redirect's PathValue("slug") is never validated
// before a KV lookup, since an invalid slug is meant to look identical to a
// nonexistent one) and net/http.ServeMux's wildcard hands back the
// unescaped path segment — confirmed live: a slug containing "%20" splits
// the "slug=" field in two, and one containing "%0A" splits the LINE in
// two, letting a single unauthenticated request to /r/{slug} forge a
// second, fully-formed "ss "-prefixed line that a naive log parser cannot
// distinguish from a genuine one.
//
// The replacement carries none of the original bytes rather than escaping
// them: a real link's slug can never take any shape but the one
// slugLogSafePattern already matches, so a slug that fails it has already
// told an operator everything logging it further would — that this
// request's slug is not one links.py could have written — and echoing the
// bytes back (even escaped) would only invite the next bypass to be found
// in whatever escaping scheme replaced this one.
func SanitizeSlugForLog(slug string) string {
	if slugLogSafePattern.MatchString(slug) {
		return slug
	}
	return "[invalid_slug]"
}

// hostLogSafePattern is the character class a real Host header value is drawn
// from: hostname/IPv4/bracketed-IPv6 characters, a colon for a port, and
// nothing else. Mirrors slugLogSafePattern's role but for host= instead of
// slug=, at a generous 253-byte cap (the DNS name length limit).
var hostLogSafePattern = regexp.MustCompile(`^[A-Za-z0-9._:\[\]-]{1,253}$`)

// SanitizeHostForLog returns raw unchanged if it is safe to place inside a
// logfmt line's "host=" field, the fixed placeholder "[invalid_host]"
// otherwise, and the literal "-" for an empty host.
//
// "-" rather than omitting the field entirely is deliberate: "the host was
// empty" is a positive, greppable statement this field exists specifically to
// let an operator confirm or rule out, and an absent field can't be grepped
// for the way a literal "-" can.
//
// Like SanitizeSlugForLog, this is request-controlled and is NOT the last
// field on the summary line (status follows it), so a Host header containing
// a space or a newline must never reach the line unescaped — the same
// line-splitting/forgery risk SanitizeSlugForLog's doc comment describes.
func SanitizeHostForLog(raw string) string {
	if raw == "" {
		return "-"
	}
	if hostLogSafePattern.MatchString(raw) {
		return raw
	}
	return "[invalid_host]"
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

// --- ev=record_unreadable (docs/plans/disposition-unreadable-logging.md) ---

// dedupKeySep separates the parts of a failure-line dedup key. NUL, because no
// op name, slug or sanitized message can contain one (SanitizeErrorMessage
// replaces every control character with "_"), so the parts can never be
// ambiguously re-split by a reader or collide by concatenation.
const dedupKeySep = "\x00"

// KVFailureDedupKey builds the per-instance dedup key for an ev=kv_fail line.
// Byte-identical to the key redirect/main.go built inline before this function
// existed, deliberately: this is a move, not a behaviour change.
func KVFailureDedupKey(op, msg string) string {
	return op + dedupKeySep + msg
}

// RecordUnreadableDedupKey builds the per-instance dedup key for an
// ev=record_unreadable line.
//
// Keyed on the SLUG, not just the message — this is the whole point. A corrupt
// record is a fact about one specific slug; two different corrupt records
// commonly produce the identical decoder message ("unexpected end of JSON
// input"), so a message-keyed dedup would log the first corrupt slug an
// instance meets and hide every other one for that instance's life. The
// message is included as well so that a slug whose record is rewritten into a
// DIFFERENT kind of corruption reports again.
//
// The literal "record_unreadable" prefix keeps this key space disjoint from
// KVFailureDedupKey's, which always begins with an op name ("open"/"get"), so
// the two kinds share one map and one cap without any possibility of collision.
func RecordUnreadableDedupKey(slug, msg string) string {
	return "record_unreadable" + dedupKeySep + slug + dedupKeySep + msg
}

// --- ev=host_unresolved (docs/plans/per-link-domain-restriction.md) ---

// hostUnresolvedDedupKey is the fixed literal dedup key for ev=host_unresolved
// — disjoint from KVFailureDedupKey's (always begins with a real op name) and
// RecordUnreadableDedupKey's (always begins with the literal
// "record_unreadable"), sharing the same 32-entry per-instance budget. Effect:
// at most one such line per Wasm instance, ever — exactly right for a
// condition that is either always true or always false for a given
// deployment (either the runtime supplies a host header or it never does).
const hostUnresolvedDedupKey = "host_unresolved"

// HostUnresolvedLine renders the complete ev=host_unresolved failure line: the
// runtime gave this component NO host to match a domain restriction against.
// Fires only when rawRequestHost returns "" — never for an ordinary domain
// mismatch, which is a product state (like out-of-window) and would be pure
// volume with no signal if logged per click.
//
// Carries NO slug (nothing about it is link-specific), NO op/ns (no KV
// operation failed — none has even been attempted yet at the point this can
// fire), and NO etype (there is no exception to classify). msg is a fixed
// literal and is last, as the doctrine requires (RenderFailureLine enforces
// this structurally).
func HostUnresolvedLine() (line, dedupKey string) {
	fields := []Field{
		{Key: "comp", Value: "redirect"},
		{Key: "ev", Value: "host_unresolved"},
		{Key: "route", Value: "/r/{slug}"},
		{Key: "msg", Value: "request carries no host header, domain-restricted links cannot resolve"},
	}
	return RenderFailureLine(fields), hostUnresolvedDedupKey
}

// RecordUnreadableLine renders the complete ev=record_unreadable failure line
// for one link record that will not parse, and the key that line must be
// deduplicated on. The caller does nothing but consult its dedup map and write
// the string (see main.go's emitRecordUnreadableLine) — every decision lives
// here, where it is host-testable.
//
// err is ParseLink's error, exactly as linkgate.Resolve returned it alongside
// DispositionUnreadable. A nil err is tolerated rather than assumed impossible
// — a future change to Resolve must degrade this line to "msg=-", never
// panic — in which case the etype field is omitted entirely, the same way
// RenderLogLine omits a zero-count op rather than emitting "=0/0".
//
// This is NOT an ev=kv_fail line and deliberately carries no op or ns field:
// no KV operation failed. The read succeeded and returned bytes; the DECODER
// failed. Anyone filtering ev=kv_fail must not see these, and anyone counting
// KV failures must not count them.
//
// etype is fmt's %T of the unwrapped error (*json.SyntaxError,
// *json.UnmarshalTypeError), which is a wording-independent classification for
// free. classifyKVFailure needs a hand-maintained English-string table only
// because spin-go-sdk flattens the WIT error variant into fixed strings; Go's
// own type system needs no such table here. The etype VOCABULARY is per-ev,
// never global — ev=kv_fail already spells it "other"/"access_denied" and
// api/obs.py already spells it "Err/Error_Other".
//
// msg is always the final field and nothing may ever be appended after it
// (CLAUDE.md, "Observable KV failures"), which is enforced structurally by
// rendering through RenderFailureLine.
func RecordUnreadableLine(slug string, err error) (line, dedupKey string) {
	msg, redacted, truncated := "-", false, false
	var etype string
	if err != nil {
		etype = fmt.Sprintf("%T", err)
		sanitized, r, t := SanitizeErrorMessage(err.Error())
		redacted, truncated = r, t
		if sanitized != "" {
			msg = sanitized
		}
	}

	fields := []Field{
		{Key: "comp", Value: "redirect"},
		{Key: "ev", Value: "record_unreadable"},
		{Key: "route", Value: "/r/{slug}"},
	}
	if slug != "" {
		// This arm only ever runs for a slug with an existing links:slug:<slug>
		// record, which only api writes and only under a validated slug — so
		// slug is trusted here in a way emitLogLine/emitFailureLine's are not.
		// Sanitized anyway: it costs nothing for a slug that already matches
		// (every slug taking this path does), and it means this field's safety
		// never depends on that invariant continuing to hold.
		fields = append(fields, Field{Key: "slug", Value: SanitizeSlugForLog(slug)})
	}
	if etype != "" {
		fields = append(fields, Field{Key: "etype", Value: etype})
	}
	if redacted {
		fields = append(fields, Field{Key: "msg_redacted", Value: "1"})
	}
	if truncated {
		fields = append(fields, Field{Key: "msg_truncated", Value: "1"})
	}
	fields = append(fields, Field{Key: "msg", Value: msg})

	return RenderFailureLine(fields), RecordUnreadableDedupKey(slug, msg)
}
