package main

import (
	"context"
	"fmt"
	"math/rand/v2"
	"net/http"
	"os"
	"strconv"
	"sync"
	"time"

	spinhttp "github.com/spinframework/spin-go-sdk/v3/http"
	"github.com/spinframework/spin-go-sdk/v3/kv"
	"github.com/spinframework/spin-go-sdk/v3/variables"

	"github.com/redirect/linkgate"
)

func init() {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /r/{slug}", handleRedirectGet)
	mux.HandleFunc("POST /r/{slug}", handleRedirectPost)

	spinhttp.Handle(func(w http.ResponseWriter, r *http.Request) {
		setSecurityHeaders(w)

		logLevel, debugToken := obsConfig()
		traced := linkgate.TokenMatches(debugToken, r.Header.Get("X-SS-Debug"))
		summary := logLevel == "summary"

		// Off path: byte-identical to pre-logging behaviour. The real
		// http.ResponseWriter is passed straight through, nothing wrapped,
		// nothing buffered, no context allocated — this is what makes it
		// safe to deploy with logging disabled (the default).
		if !traced && !summary {
			mux.ServeHTTP(w, r)
			return
		}

		collector := linkgate.NewCollector()
		r = r.WithContext(context.WithValue(r.Context(), collectorContextKey, collector))
		start := time.Now()

		if traced {
			// Buffer status and body so Server-Timing can be set on the
			// real writer's header map before its first Write — which is
			// what actually latches the header snapshot, not WriteHeader.
			// Confined to token-bearing requests only, so a bug in the
			// buffering cannot affect normal traffic.
			bw := newBufferingWriter(w)
			mux.ServeHTTP(bw, r)
			dur := time.Since(start)
			bw.flush(linkgate.RenderServerTiming(dur, collector))
			emitLogLine(r, bw.status, dur, collector)
			return
		}

		// log_level=summary, no token: forward every call immediately, just
		// record the status code for the log line. No buffering; streaming
		// unchanged.
		rw := newRecordingWriter(w)
		mux.ServeHTTP(rw, r)
		dur := time.Since(start)
		emitLogLine(r, rw.status, dur, collector)
	})
}

// setSecurityHeaders applies the baseline security headers to every response
// this component sends — redirects (a 302 with no body a browser would ever
// execute) and error pages alike. Headers are set before ServeHTTP dispatches
// to a handler; http.Error/http.NotFound/http.Redirect only ever call
// Header().Set on their own specific keys (Content-Type, Location), never
// clearing the whole header map, so these survive alongside them. The
// password-prompt page additionally sets its own stricter CSP (see
// renderPasswordPrompt in passwordgate.go), since it's the one response here
// that actually renders real HTML a browser executes/lays out.
func setSecurityHeaders(w http.ResponseWriter) {
	h := w.Header()
	h.Set("X-Content-Type-Options", "nosniff")
	h.Set("Referrer-Policy", "strict-origin-when-cross-origin")
	h.Set("X-Frame-Options", "DENY")
	h.Set("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
	h.Set("X-SS-Version", appVersion())

	// Never cached, anywhere, by anything. Resolution re-reads KV on every
	// request by design: the status check, the [start_at, end_at) window, a
	// repointed destination, a deleted slug and the destination-policy
	// remediation path (bulk Disable -> 404) are all only correct if no layer
	// is serving a remembered answer. This covers the 404s as well as the
	// 302s — a cached "not yet active" 404 means the link never starts
	// working when its window opens.
	//
	// The 302 in handleRedirectGet/handleRedirectPost is load-bearing and
	// must never become a 301 or 308: Akamai edge servers do not cache 302
	// or 307 by default, but they DO cache 301 and 308 by default
	// (techdocs.akamai.com/property-mgr/docs/cache-http-redirects). This
	// header is defence-in-depth for browsers and intermediate proxies —
	// Akamai does not honour origin Cache-Control by default, so at the edge
	// the 302 status is the actual control.
	h.Set("Cache-Control", "no-store")
}

// --- Toggleable structured logging (docs/plans/toggleable-logging.md) ---
//
// Two Spin variables control this: log_level ("off"/"summary", default
// "off") and log_debug_token (a shared secret, default ""). Both are read
// once via variables.Get and cached for the lifetime of the Wasm instance —
// sound because a Spin variable cannot change without a restart locally or
// a redeploy on Akamai, both of which produce a fresh instance.

var (
	obsOnce       sync.Once
	obsLogLevel   string
	obsDebugToken string
)

func obsConfig() (logLevel, debugToken string) {
	obsOnce.Do(func() {
		raw, err := variables.Get("log_level")
		if err != nil {
			raw = "off"
		}
		obsLogLevel = linkgate.ParseLogLevel(raw)

		token, err := variables.Get("log_debug_token")
		if err != nil {
			token = ""
		}
		obsDebugToken = token
	})
	return obsLogLevel, obsDebugToken
}

// appVersion returns the operator-supplied build identifier emitted as
// X-SS-Version, cached for the instance's lifetime for the same reason the
// logging variables are: a Spin variable cannot change without a redeploy.
//
// This exists because a deploy propagates more slowly than `spin aka app
// deploy` waits before declaring failure, so "is the new build serving yet?"
// had to be answered by probing for a behavioural difference — which is
// circular when the behaviour you are trying to measure IS the change.
func appVersion() string {
	versionOnce.Do(func() {
		v, err := variables.Get("app_version")
		if err != nil || v == "" {
			v = "unknown"
		}
		appVersionValue = v
	})
	return appVersionValue
}

var (
	versionOnce     sync.Once
	appVersionValue string
)

// collectorContextKey is an unexported key type so no other package can
// collide with it. The collector is attached to the request context only
// when tracing is enabled for that request — never a package-level
// variable, which would silently interleave concurrent requests' operations
// into one another's line.
type collectorCtxKey struct{}

var collectorContextKey = collectorCtxKey{}

// collectorFrom returns the collector attached to ctx, or nil if none is —
// every linkgate.Collector method is nil-safe, so a nil result is already a
// correct no-op collector for the off path.
func collectorFrom(ctx context.Context) *linkgate.Collector {
	c, _ := ctx.Value(collectorContextKey).(*linkgate.Collector)
	return c
}

// emitLogLine renders and writes one "ss "-prefixed logfmt line for the
// request, after the response has already been sent — so the ~µs cost of
// the stderr write lands in neither the measured handler duration nor the
// visitor's latency. route is hardcoded to this component's one route
// shape; slug is logged raw (not redacted) because correlating a slow
// resolution to a specific link is the entire point of instrumenting this
// path, and slugs are already treated as non-secret (CLAUDE.md, "Security
// tradeoffs").
func emitLogLine(r *http.Request, status int, dur time.Duration, collector *linkgate.Collector) {
	fields := []linkgate.Field{
		{Key: "comp", Value: "redirect"},
		{Key: "route", Value: "/r/{slug}"},
	}
	if slug := r.PathValue("slug"); slug != "" {
		fields = append(fields, linkgate.Field{Key: "slug", Value: slug})
	}
	fields = append(fields, linkgate.Field{Key: "status", Value: strconv.Itoa(status)})
	fmt.Fprintln(os.Stderr, linkgate.RenderLogLine(fields, dur, collector))
}

// recordingWriter forwards Header/Write immediately (no buffering —
// streaming unchanged) while recording the status code that was actually
// sent, for the baseline (log_level=summary, no token) log line. Embeds
// http.ResponseWriter so Header() returns the real, live map, never a copy.
type recordingWriter struct {
	http.ResponseWriter
	status int
}

func newRecordingWriter(w http.ResponseWriter) *recordingWriter {
	return &recordingWriter{ResponseWriter: w, status: http.StatusOK}
}

func (w *recordingWriter) WriteHeader(code int) {
	w.status = code
	w.ResponseWriter.WriteHeader(code)
}

// bufferingWriter buffers the status code and body so Server-Timing can be
// added to the real ResponseWriter's header map before its first real
// Write — the point at which the Spin Go SDK actually snapshots the header
// map (WriteHeader only stores the int; see the plan's fact 7). Confined to
// token-bearing requests, so a bug here cannot affect normal traffic.
type bufferingWriter struct {
	real   http.ResponseWriter
	status int
	body   []byte
}

func newBufferingWriter(real http.ResponseWriter) *bufferingWriter {
	return &bufferingWriter{real: real, status: http.StatusOK}
}

// Header returns the real, live header map — never a copy — so
// Location/Content-Type/the password prompt's CSP keep working, and
// Server-Timing can be added to it afterwards as one more Set.
func (w *bufferingWriter) Header() http.Header {
	return w.real.Header()
}

func (w *bufferingWriter) WriteHeader(code int) {
	w.status = code
}

func (w *bufferingWriter) Write(b []byte) (int, error) {
	w.body = append(w.body, b...)
	return len(b), nil
}

// flush sends the buffered status and body through the real writer, setting
// Server-Timing first (if non-empty) so it is present in the header map
// before the real writer's first Write. If the buffered body is empty (a
// HEAD request — Go 1.22's ServeMux matches HEAD against a GET pattern),
// the send still has to happen, via a nil Write.
func (w *bufferingWriter) flush(serverTiming string) {
	if serverTiming != "" {
		w.real.Header().Set("Server-Timing", serverTiming)
	}
	w.real.WriteHeader(w.status)
	if len(w.body) == 0 {
		_, _ = w.real.Write(nil)
		return
	}
	_, _ = w.real.Write(w.body)
}

// openTimedStore opens the default KV store, recording the open's duration
// into collector (a nil collector makes this a plain, unwrapped kv.Open —
// no timer calls, no wrapper allocation — which is what keeps the off path
// cheap). When collector is non-nil, the returned store is wrapped so every
// subsequent Exists/Get/Set on it is also recorded.
func openTimedStore(collector *linkgate.Collector) (linkgate.KVStore, error) {
	if collector == nil {
		return kv.Open("default")
	}
	start := time.Now()
	store, err := kv.Open("default")
	collector.Record("open", "-", time.Since(start), 0)
	if err != nil {
		return nil, err
	}
	return linkgate.TimedStore{Store: store, Collector: collector}, nil
}

func handleRedirectGet(w http.ResponseWriter, r *http.Request) {
	slug := r.PathValue("slug")
	collector := collectorFrom(r.Context())

	store, err := openTimedStore(collector)
	if err != nil {
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	l, ok := lookupLink(store, slug)
	if !ok || l.Status != "active" || !linkgate.IsWithinWindow(l.StartAt, l.EndAt, time.Now()) {
		http.NotFound(w, r)
		return
	}

	if l.PasswordHash != "" {
		renderPasswordPrompt(w, http.StatusOK, slug, "")
		return
	}

	sendRedirectThenRecord(w, r, slug, l.TargetURL, collector)
}

// handleRedirectPost re-fetches the link fresh from KV — never trusting a
// prior GET — so a password change or removal takes effect on the very next
// submission with no stale-session window.
func handleRedirectPost(w http.ResponseWriter, r *http.Request) {
	slug := r.PathValue("slug")
	collector := collectorFrom(r.Context())

	store, err := openTimedStore(collector)
	if err != nil {
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	l, ok := lookupLink(store, slug)
	if !ok || l.Status != "active" || !linkgate.IsWithinWindow(l.StartAt, l.EndAt, time.Now()) {
		http.NotFound(w, r)
		return
	}

	if l.PasswordHash == "" {
		sendRedirectThenRecord(w, r, slug, l.TargetURL, collector)
		return
	}

	if err := r.ParseForm(); err != nil {
		renderPasswordPrompt(w, http.StatusBadRequest, slug, "Invalid form submission.")
		return
	}

	if !linkgate.VerifyPassword(r.FormValue("password"), l.PasswordHash) {
		renderPasswordPrompt(w, http.StatusUnauthorized, slug, "Incorrect password.")
		return
	}

	sendRedirectThenRecord(w, r, slug, l.TargetURL, collector)
}

// sendRedirectThenRecord issues the 302 and only then records the click.
//
// ALL analytics runs after the response. Measured on Akamai 2026-08-06: this
// moves median TTFB from 98.0 ms to 74.3 ms (~24 ms, untraced, warm
// connection) at no measurable accuracy cost — a controlled A/B put click
// recording at 86/100 deferred against 85/100 not deferred. An intermediate
// "split" arrangement (count before, event after) was built and measured at
// ~5.5 ms, inside the noise band, and dropped.
//
// All analytics is best-effort in the sense that a KV error is swallowed
// rather than propagated: failing to record a click must never block the
// redirect. This opens its own store rather than reusing the handler's,
// keeping a successful redirect at 6 KV operations (2 opens); threading the
// handler's store in would remove one kv.Open, which measured ~0.2% on Akamai
// (~154 µs against ~20 ms per data operation) and is not worth doing.
//
// A visitor has no stake in the events write, so it runs after the response
// has been handed to the host rather than before. The Spin Go SDK makes that
// possible: wasiHandle passes the response to the host as soon as the first
// Write calls send(), without waiting for the handler to return (see
// spin-go-sdk/v3 http/http.go) — so the redirect is already travelling while
// the analytics writes are still in flight.
//
// The courtesy body is deliberately dropped and Content-Length: 0 set
// explicitly. http.Redirect writes a small `<a href=...>Found</a>.` body with
// NO Content-Length, which leaves a client unable to tell the message is
// complete until the body stream closes — and that only happens when the
// handler returns, which would hand back exactly the latency this reordering
// exists to remove. A 302 needs no body; every client acts on Location.
//
// The empty Write is load-bearing, not redundant: Write is what calls send().
// WriteHeader alone only records the status int (see
// convertor_outgoing_response.go), so without it nothing reaches the host
// until the handler returns and the reordering buys nothing.
//
// NOTE: when a request is traced with X-SS-Debug, bufferingWriter holds the
// response until the handler returns so Server-Timing can be attached. That
// is correct for tracing but it defeats this optimisation, so a traced
// request does NOT show the improvement. Measure it untraced.
func sendRedirectThenRecord(w http.ResponseWriter, r *http.Request, slug, target string, collector *linkgate.Collector) {
	store, err := openTimedStore(collector)
	analytics := err == nil // best-effort: a KV failure must never block the redirect
	now := time.Now()

	h := w.Header()
	h.Set("Location", target)
	h.Set("Content-Length", "0")
	w.WriteHeader(http.StatusFound)
	_, _ = w.Write([]byte{})

	// Both halves after the response. Kept as two functions because they have
	// genuinely different failure modes — see each — even though both are now
	// deferred.
	if analytics {
		recordClickCount(store, slug, now)
		recordClickEvent(store, slug, r, now)
	}
}

// recordClickCount performs the analytics:count read-modify-write, against one
// of linkgate.CountShards shard keys picked per click.
//
// It runs AFTER the response, and a controlled A/B on the deployed Akamai app
// (2026-08-06) is why that is safe. Four rounds of 25 rapid requests recorded
// 85/100 with both halves before the response and 86/100 with this half after
// it — indistinguishable. An earlier reading of 8/10 twice was taken as proof
// that deferring lost counts; at the measured ~85% baseline rate, 20 requests
// should record 17.1 +/- 1.6, so 16 was ordinary noise, not a regression.
//
// TWO INDEPENDENT MECHANISMS LOSE CLICKS HERE, and sharding addresses exactly
// one of them. Both were measured on the live app on 2026-08-06:
//
//   - Per-key contention. This is a read-modify-write and Spin's KV has no
//     compare-and-swap, so overlapping clicks on ONE key lose increments: 25%
//     loss at 9.4 clicks/second on a single slug. Spreading the counter over
//     shard keys is what fixes it — sixteen slugs at the same app-wide write
//     load lost 0%, which is the measurement that justified this design.
//     Ordering cannot fix it; that was A/B'd and it does not help.
//
//   - The app-wide write cap. Akamai allows 50 KV writes/second per app and a
//     recorded click costs two (this shard, plus one events slot), so above
//     ~25 clicks/second ACROSS THE WHOLE SERVICE writes are throttled and the
//     loss is not something this function can do anything about. Sharding does
//     not touch it; only writing less per click would.
//
// With CountShards=16, a single link taking every click the app can serve sees
// only ~1.6 clicks/second per shard, well inside the band measured lossless.
// So below the app-wide ceiling the per-link penalty is gone, and above it the
// write cap binds regardless. See docs/plans/click-count-accuracy.md.
func recordClickCount(store linkgate.KVStore, slug string, now time.Time) {
	retentionDays := intVariable("analytics_day_retention_days", 90)
	day := now.UTC().Format("2006-01-02")

	shard := linkgate.ShardFor(clickEntropy(now), linkgate.CountShards)
	countKey := linkgate.CountShardKey(slug, shard)
	raw, _ := store.Get(countKey)
	if updated, err := linkgate.UpdateCount(raw, day, retentionDays); err == nil {
		_ = store.Set(countKey, updated)
	}
}

// clickEntropy produces the 64-bit value linkgate.ShardFor reduces to a shard
// index. Two independent sources are XORed deliberately:
//
//   - math/rand/v2's global source, which advances on every call and so varies
//     within one Wasm instance even if its seed does not.
//   - the raw nanosecond clock, which varies across instances even if WASI's
//     random_get is stubbed and every instance seeds identically.
//
// Each source has a plausible failure mode on this host that the other covers.
// If the RNG seeded identically per instance AND Akamai created one instance
// per request — both plausible, neither confirmed — a rand-only shard would
// send every instance's first click to the same shard, which is worse than not
// sharding at all.
func clickEntropy(now time.Time) uint64 {
	return uint64(now.UnixNano()) ^ rand.Uint64()
}

// recordClickEvent writes one recent-events ring-buffer slot.
//
// Safe to run AFTER the response, which is the whole point of the split: it is
// a blind overwrite with no read, so deferring it cannot lose anything the
// design does not already accept losing. Its slot collisions depend on the
// timestamp and the slot count (see CLAUDE.md's Analytics section, which
// documents this log as a best-effort sample), not on when the write happens.
// now is passed in rather than re-read so the event still carries the click's
// timestamp, not the post-response one.
func recordClickEvent(store linkgate.KVStore, slug string, r *http.Request, now time.Time) {
	numSlots := intVariable("analytics_event_slots", 30)
	slot := linkgate.EventSlot(now, numSlots)
	eventKey := linkgate.EventKey(slug, slot)
	event := linkgate.FormatEvent(now.UnixMilli(), r.Referer(), linkgate.ClassifyUserAgent(r.UserAgent()))
	_ = store.Set(eventKey, []byte(event))
}

// intVariable reads a Spin variable and parses it as an int, falling back to
// fallback on any error. variables.Get is not a KV operation and is
// deliberately never recorded into a collector.
func intVariable(name string, fallback int) int {
	value, err := variables.Get(name)
	if err != nil {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return fallback
	}
	return parsed
}

// lookupLink fetches and decodes the link record for slug, returning ok=false
// if the key is absent or the stored value can't be parsed. Takes the local
// linkgate.KVStore interface rather than *kv.Store directly, so it can be
// handed either a raw store (collector nil, off path) or a
// linkgate.TimedStore (collector present) with no branching here.
//
// Deliberately ONE KV data operation, not two. The Exists probe this used to
// do ahead of the Get was pure overhead: the SDK's Get returns
// ([]byte(""), nil) for a missing key — an empty slice with NO error, see
// spin-go-sdk/v3 kv.Store.Get — so an absent key is already distinguishable
// without asking first. A stored link record is always JSON and can never be
// zero-length, so len(raw) == 0 means absent and nothing else. The empty
// check is explicit rather than left to ParseLink's json.Unmarshal failing,
// so absence is a stated condition rather than a side effect of a decoder.
//
// Measured on the deployed Akamai app 2026-08-06: removing this probe saved
// 5.2 ms of 38.8 ms, or 13.5%, measured like-for-like against the 7-op build
// in the same latency window. Akamai charges per data operation and barely at
// all per store handle (~150 µs), so one fewer data op is the whole win.
// Do NOT read the absolute figure as a constant — the same 7 operations
// measured 116.7 ms earlier the same day, a 3x regime swing, so per-op cost
// there ranged 5.5-16.7 ms. The stable statement is "one operation's worth,
// ~14% of the redirect's KV time". Locally the probe measured 13-18 us and
// was correctly judged not worth removing; the local numbers do not transfer.
// See CLAUDE.md's Akamai section.
func lookupLink(store linkgate.KVStore, slug string) (linkgate.Link, bool) {
	raw, err := store.Get(linkgate.LinkKey(slug))
	if err != nil || len(raw) == 0 {
		return linkgate.Link{}, false
	}

	l, err := linkgate.ParseLink(raw)
	if err != nil {
		return linkgate.Link{}, false
	}

	return l, true
}

// main function must be included for the compiler but is not executed.
func main() {}
