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
	w.WriteHeader(status)
	_ = promptTemplate.Execute(w, promptData{Slug: slug, Error: errMsg})
}
