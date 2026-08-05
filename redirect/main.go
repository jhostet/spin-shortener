package main

import (
	"net/http"
	"strconv"
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
		mux.ServeHTTP(w, r)
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

func handleRedirectGet(w http.ResponseWriter, r *http.Request) {
	slug := r.PathValue("slug")

	store, err := kv.Open("default")
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

	recordAnalytics(slug, r)
	http.Redirect(w, r, l.TargetURL, http.StatusFound)
}

// handleRedirectPost re-fetches the link fresh from KV — never trusting a
// prior GET — so a password change or removal takes effect on the very next
// submission with no stale-session window.
func handleRedirectPost(w http.ResponseWriter, r *http.Request) {
	slug := r.PathValue("slug")

	store, err := kv.Open("default")
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
		recordAnalytics(slug, r)
		http.Redirect(w, r, l.TargetURL, http.StatusFound)
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

	recordAnalytics(slug, r)
	http.Redirect(w, r, l.TargetURL, http.StatusFound)
}

// recordAnalytics updates the click counter and writes one recent-events
// slot for slug. Best-effort: any KV error here is swallowed rather than
// propagated, since a failure to record a click must never block the
// redirect itself.
func recordAnalytics(slug string, r *http.Request) {
	store, err := kv.Open("default")
	if err != nil {
		return
	}

	now := time.Now()
	retentionDays := intVariable("analytics_day_retention_days", 90)
	day := now.UTC().Format("2006-01-02")

	countKey := linkgate.CountKey(slug)
	raw, _ := store.Get(countKey)
	if updated, err := linkgate.UpdateCount(raw, day, retentionDays); err == nil {
		_ = store.Set(countKey, updated)
	}

	numSlots := intVariable("analytics_event_slots", 30)
	slot := linkgate.EventSlot(now, numSlots)
	eventKey := linkgate.EventKey(slug, slot)
	event := linkgate.FormatEvent(now.UnixMilli(), r.Referer(), linkgate.ClassifyUserAgent(r.UserAgent()))
	_ = store.Set(eventKey, []byte(event))
}

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
// if the key is absent or the stored value can't be parsed.
func lookupLink(store *kv.Store, slug string) (linkgate.Link, bool) {
	key := linkgate.LinkKey(slug)
	exists, err := store.Exists(key)
	if err != nil || !exists {
		return linkgate.Link{}, false
	}

	raw, err := store.Get(key)
	if err != nil {
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
