package main

import (
	"net/http"
	"time"

	spinhttp "github.com/spinframework/spin-go-sdk/v3/http"
	"github.com/spinframework/spin-go-sdk/v3/kv"

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

	http.Redirect(w, r, l.TargetURL, http.StatusFound)
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
