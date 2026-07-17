package main

import (
	"fmt"
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
		mux.ServeHTTP(w, r)
	})
}

func handleRedirectGet(w http.ResponseWriter, r *http.Request) {
	slug := r.PathValue("slug")

	store, err := kv.Open("links")
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

	store, err := kv.Open("links")
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
	store, err := kv.Open("analytics")
	if err != nil {
		return
	}

	now := time.Now()
	retentionDays := intVariable("analytics_day_retention_days", 90)
	day := now.UTC().Format("2006-01-02")

	countKey := "count:" + slug
	raw, _ := store.Get(countKey)
	if updated, err := linkgate.UpdateCount(raw, day, retentionDays); err == nil {
		_ = store.Set(countKey, updated)
	}

	numSlots := intVariable("analytics_event_slots", 30)
	slot := linkgate.EventSlot(now, numSlots)
	eventKey := fmt.Sprintf("events:%s:%d", slug, slot)
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
	exists, err := store.Exists("slug:" + slug)
	if err != nil || !exists {
		return linkgate.Link{}, false
	}

	raw, err := store.Get("slug:" + slug)
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
