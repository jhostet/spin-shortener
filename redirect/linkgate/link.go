// Package linkgate holds the redirect component's pure link-record logic —
// no spin-go-sdk imports, so it builds and tests under plain `go test`
// (importing spin-go-sdk pulls in a wit_exports.go stub only completed by
// the special `go tool componentize-go build` toolchain).
package linkgate

import (
	"encoding/json"
	"errors"
)

type Link struct {
	Slug         string `json:"slug"`
	TargetURL    string `json:"target_url"`
	Owner        string `json:"owner"`
	Custom       bool   `json:"custom"`
	PasswordHash string `json:"password_hash"`
	Status       string `json:"status"`
	StartAt      string `json:"start_at"`
	EndAt        string `json:"end_at"`
	CreatedAt    string `json:"created_at"`
	UpdatedAt    string `json:"updated_at"`
}

// ErrUnsafeTargetURL is what ParseLink returns when a record decodes but its
// target_url contains ASCII control characters. It is kept DISTINCT from a
// decoder error so a caller (or an operator reading an ev=record_unreadable
// line) can tell "does not parse" from "parses but cannot be safely served",
// while both stay ParseLink failures and therefore DispositionUnreadable.
//
// The message must stay free of ':' — SanitizeErrorMessage's key-shaped
// redaction treats "word:" as a potential key, and a redacted msg would be
// noise for a message that is the whole diagnosis.
var ErrUnsafeTargetURL = errors.New("target_url contains control characters")

// ParseLink decodes a link record fetched from KV, and it is the ONE place
// the redirect component decides whether a stored record may be served. A
// JSON `null` for any string field (e.g. password_hash/start_at/end_at when
// unset) unmarshals as a no-op, leaving that field at its zero value "".
//
// Beyond decoding, ParseLink rejects a target_url containing ASCII control
// characters (U+0000-U+001F, DEL). This is the wire-safety half of the
// control-char fix; api/links.py's target_url_error rejects them at all four
// authoring paths, but storage can still drift (restore, a hand-edited
// record), and THIS component is the one that places target_url verbatim into
// a Location header whose values the Go SDK serializes unvalidated. A record
// that cannot be emitted becomes DispositionUnreadable -> 500 via Resolve,
// exactly like a record that will not parse — the same "a fault must never be
// dressed up as a product state" rule. api's own notion of "unreadable"
// (json.loads) stays narrower, the established three-way divergence.
//
// Returning ErrUnsafeTargetURL rather than silently dropping the record also
// means a visitor gets the data-free 500 page (no existence leak beyond what
// 500 already discloses for unreadable records) and the operator gets one
// ev=record_unreadable line carrying the slug and this message.
func ParseLink(raw []byte) (Link, error) {
	var l Link
	if err := json.Unmarshal(raw, &l); err != nil {
		return Link{}, err
	}
	if hasControlChars(l.TargetURL) {
		return Link{}, ErrUnsafeTargetURL
	}
	return l, nil
}

// hasControlChars reports whether s contains any ASCII control character
// (U+0000-U+001F) or DEL (U+007F), matching api/links.py's
// _CONTROL_CHAR_PATTERN. Byte scanning is safe for UTF-8: every byte of a
// multi-byte code point is >= 0x80. Percent-encoded forms (%0d%0a) contain
// no control bytes and pass, correctly — they are inert literal text in the
// header, decoded only by the destination URL, never by this emission.
func hasControlChars(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] < 0x20 || s[i] == 0x7f {
			return true
		}
	}
	return false
}
