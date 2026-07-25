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
	w.Header().Set("Cache-Control", "no-store")
	// Stricter than the GUI pages' CSP: prompt.html has zero <script> tags
	// (confirmed — it's a bare form), so script-src can be 'none' outright
	// rather than needing 'unsafe-inline'. style-src still needs it for the
	// one inline style="color: red" error-message attribute.
	w.Header().Set("Content-Security-Policy",
		"default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
	w.WriteHeader(status)
	_ = promptTemplate.Execute(w, promptData{Slug: slug, Error: errMsg})
}
