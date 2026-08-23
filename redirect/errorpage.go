package main

import (
	_ "embed"
	"net/http"
	"strconv"
)

//go:embed error-404.html
var notFoundHTML []byte

//go:embed error-500.html
var serverErrorHTML []byte

//go:embed error-503.html
var unavailableHTML []byte

// errorPageCSP is shared by all three error pages, and is deliberately NOT a
// copy of the password prompt's. form-action is 'none' rather than 'self'
// https: http:, because none of these pages has a form — the prompt's scheme
// list exists solely so Chrome permits the 302 that answers its password
// POST. script-src is stated explicitly even though default-src 'none'
// already covers it, matching the prompt page's own explicitness. base-uri
// and frame-ancestors are listed because default-src does not cover them
// (only fetch directives) — the same catch recorded in passwordgate.go.
const errorPageCSP = "default-src 'none'; script-src 'none'; style-src 'self'; " +
	"base-uri 'self'; form-action 'none'; frame-ancestors 'none'"

// writeErrorPage renders one of the three static, data-free error pages.
// There is nothing to interpolate, so the page is a []byte written straight
// to the writer rather than executed through html/template — that is what
// makes the 404's byte-identity across its three causes structural rather
// than a thing someone has to remember. Content-Length is computed from the
// same variable that is written, so it cannot drift (matching
// sendRedirectThenRecord's explicit Content-Length: 0 for the same reason).
// Every other security header (Cache-Control: no-store, X-SS-Version, HSTS,
// X-Frame-Options, etc.) arrives from setSecurityHeaders before this runs.
func writeErrorPage(w http.ResponseWriter, status int, page []byte) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Content-Security-Policy", errorPageCSP)
	w.Header().Set("Content-Length", strconv.Itoa(len(page)))
	w.WriteHeader(status)
	_, _ = w.Write(page)
}

// notFound answers a slug that is absent, disabled, or outside its
// [start_at, end_at) window — all three collapse to linkgate.DispositionNotFound
// upstream, and this page must render identically for all three (CLAUDE.md,
// "Security tradeoffs": slug existence/status/scheduling must not be
// distinguishable to a probing visitor).
func notFound(w http.ResponseWriter) {
	writeErrorPage(w, http.StatusNotFound, notFoundHTML)
}

// internalError answers linkgate.DispositionUnreadable: a link record exists
// but would not parse. See errorPageCSP's comment and error-500.html's copy
// rules for why this page names nothing about the record.
func internalError(w http.ResponseWriter) {
	writeErrorPage(w, http.StatusInternalServerError, serverErrorHTML)
}
