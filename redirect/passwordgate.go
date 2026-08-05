package main

import (
	_ "embed"
	"html/template"
	"net/http"
)

//go:embed prompt.html
var promptHTMLSource string

var promptTemplate = template.Must(template.New("prompt").Parse(promptHTMLSource))

type promptData struct {
	Slug  string
	Error string
}

func renderPasswordPrompt(w http.ResponseWriter, status int, slug, errMsg string) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	// Cache-Control: no-store moved to setSecurityHeaders in main.go, which
	// now covers every response this component sends (302s and 404s
	// included), not just this prompt page.
	//
	// Stricter than the GUI pages' CSP, and now the strictest in the app:
	// prompt.html has zero <script> tags (confirmed — it's a bare form), so
	// script-src stays 'none' outright rather than the GUI's 'self'. This is
	// the one page where a visitor types a credential, so keeping scripts
	// impossible here is worth more than the theme-following that loading
	// theme-init.js would buy.
	//
	// style-src is 'self' rather than 'unsafe-inline': the page's one inline
	// style="color: red" is gone, replaced by theme.css's own .form-error —
	// the class DESIGN.md already required every other form error to use.
	// That leaves no 'unsafe-inline' anywhere in the application.
	//
	// base-uri isn't covered by default-src's fallback (only fetch-directives
	// are) — a code review caught it missing here despite form-action/
	// frame-ancestors (also not covered by default-src) being handled
	// explicitly.
	//
	// form-action lists http:/https: alongside 'self', and must: a correct
	// password answers this POST with a 302 to the link's target, and Chrome
	// applies form-action to that redirect, not just to the form's own action
	// URL. With a bare 'self' the browser blocked the navigation outright —
	// the server sent the 302 and the visitor simply stayed on the prompt,
	// so every password-protected link was a dead end in the browser while
	// working perfectly under curl. Found by loading the real page; no test
	// could have caught it, since the block happens in the browser after a
	// correct server response. The two schemes mirror exactly what
	// api/links.py:83 accepts for a target URL, so this is no broader than
	// the product allows, and javascript:/data: form targets stay blocked.
	w.Header().Set("Content-Security-Policy",
		"default-src 'none'; script-src 'none'; style-src 'self'; base-uri 'self'; form-action 'self' https: http:; frame-ancestors 'none'")
	w.WriteHeader(status)
	_ = promptTemplate.Execute(w, promptData{Slug: slug, Error: errMsg})
}
