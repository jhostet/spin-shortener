package main

import (
	"crypto/pbkdf2"
	"crypto/sha256"
	"crypto/subtle"
	_ "embed"
	"encoding/base64"
	"html/template"
	"net/http"
	"strconv"
	"strings"
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

// verifyLinkPassword checks password against a "pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>"
// value using only stdlib crypto/pbkdf2 + crypto/subtle — no network calls, no extra dependencies.
func verifyLinkPassword(password, stored string) bool {
	parts := strings.Split(stored, "$")
	if len(parts) != 4 || parts[0] != "pbkdf2_sha256" {
		return false
	}

	iterations, err := strconv.Atoi(parts[1])
	if err != nil {
		return false
	}
	salt, err := base64.StdEncoding.DecodeString(parts[2])
	if err != nil {
		return false
	}
	expected, err := base64.StdEncoding.DecodeString(parts[3])
	if err != nil {
		return false
	}

	derived, err := pbkdf2.Key(sha256.New, password, salt, iterations, len(expected))
	if err != nil {
		return false
	}

	return subtle.ConstantTimeCompare(derived, expected) == 1
}
