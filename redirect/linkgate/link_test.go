package linkgate

import (
	"errors"
	"testing"
)

func TestParseLink_ValidFullRecord(t *testing.T) {
	raw := []byte(`{
		"slug": "abc1234",
		"target_url": "https://example.com/x",
		"owner": "admin",
		"custom": true,
		"password_hash": "pbkdf2_sha256$100000$c2FsdA==$aGFzaA==",
		"status": "active",
		"start_at": "2026-01-01T00:00:00Z",
		"end_at": "2026-02-01T00:00:00Z",
		"created_at": "2026-01-01T00:00:00Z",
		"updated_at": "2026-01-01T00:00:00Z"
	}`)

	l, err := ParseLink(raw)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if l.Slug != "abc1234" || l.TargetURL != "https://example.com/x" || !l.Custom || l.Status != "active" {
		t.Fatalf("unexpected decode: %+v", l)
	}
	if l.StartAt != "2026-01-01T00:00:00Z" || l.EndAt != "2026-02-01T00:00:00Z" {
		t.Fatalf("unexpected window fields: %+v", l)
	}
}

func TestParseLink_MissingOptionalFieldsDefaultToZeroValue(t *testing.T) {
	raw := []byte(`{"slug": "abc1234", "target_url": "https://example.com/x", "status": "active"}`)

	l, err := ParseLink(raw)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if l.PasswordHash != "" || l.StartAt != "" || l.EndAt != "" || l.Custom {
		t.Fatalf("expected zero-value defaults for unset fields, got: %+v", l)
	}
}

func TestParseLink_NullFieldsDefaultToZeroValue(t *testing.T) {
	raw := []byte(`{"slug": "abc1234", "target_url": "https://example.com/x", "status": "active", "password_hash": null, "start_at": null, "end_at": null}`)

	l, err := ParseLink(raw)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if l.PasswordHash != "" || l.StartAt != "" || l.EndAt != "" {
		t.Fatalf("expected JSON null to leave string fields at zero value, got: %+v", l)
	}
}

// TestParseLink_AllowedDomainsAbsentOrNullIsNil pins that an absent or
// explicit-null allowed_domains field unmarshals to a nil slice, which
// HostAllowed reads as unrestricted — no migration needed for any existing
// record (docs/plans/per-link-domain-restriction.md).
func TestParseLink_AllowedDomainsAbsentOrNullIsNil(t *testing.T) {
	absent := []byte(`{"slug":"abc","target_url":"https://example.com","status":"active"}`)
	l, err := ParseLink(absent)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if l.AllowedDomains != nil {
		t.Errorf("absent allowed_domains: AllowedDomains = %#v, want nil", l.AllowedDomains)
	}

	explicitNull := []byte(`{"slug":"abc","target_url":"https://example.com","status":"active","allowed_domains":null}`)
	l2, err := ParseLink(explicitNull)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if l2.AllowedDomains != nil {
		t.Errorf("null allowed_domains: AllowedDomains = %#v, want nil", l2.AllowedDomains)
	}
}

// TestParseLink_MalformedAllowedDomainsIsAParseError pins that a shape
// mismatch (a JSON string where a list belongs) is a genuine
// *json.UnmarshalTypeError -> DispositionUnreadable -> 500, with no special
// handling needed: encoding/json already produces this for free.
func TestParseLink_MalformedAllowedDomainsIsAParseError(t *testing.T) {
	raw := []byte(`{"slug":"abc","target_url":"https://example.com","status":"active","allowed_domains":"not-a-list"}`)
	_, err := ParseLink(raw)
	if err == nil {
		t.Fatal("expected an error for a malformed allowed_domains field, got nil")
	}
}

func TestParseLink_MalformedJSON(t *testing.T) {
	_, err := ParseLink([]byte(`{not valid json`))
	if err == nil {
		t.Fatal("expected an error for malformed JSON, got nil")
	}
}

// TestParseLinkIgnoresUnknownTagsField guards the redirect hot path against
// the link-tags feature: Go's encoding/json ignores unknown object keys by
// default (ParseLink uses plain json.Unmarshal, not a Decoder with
// DisallowUnknownFields), so a record carrying a "tags" array must still
// parse cleanly. linkgate.Link deliberately gains no Tags field — the hot
// path never reads tags, and this test is what stops someone "helpfully"
// adding one (see docs/plans/link-tags-and-ownership.md, "Redirect (Go)
// changes").
func TestParseLinkIgnoresUnknownTagsField(t *testing.T) {
	raw := []byte(`{"slug":"abc","target_url":"https://example.com",` +
		`"owner":"alice","status":"active","tags":["sale","q4"]}`)
	l, err := ParseLink(raw)
	if err != nil {
		t.Fatalf("ParseLink returned an error for a record with tags: %v", err)
	}
	if l.Slug != "abc" || l.TargetURL != "https://example.com" || l.Status != "active" {
		t.Fatalf("known fields did not survive: %+v", l)
	}
}

// TestParseLink_RejectsControlCharactersInTargetURL pins the wire-safety
// half of the control-char fix (docs/plans/reject-control-chars-in-target-url.md):
// a record whose target_url carries ASCII control characters decodes fine as
// JSON but must NOT be served, because the redirect emits target_url verbatim
// as the Location header and the Go SDK serializes header values unvalidated.
// Every payload here would otherwise pass json.Unmarshal and reach the wire.
func TestParseLink_RejectsControlCharactersInTargetURL(t *testing.T) {
	cases := []struct {
		name string
		raw  string
	}{
		// JSON \uXXXX escapes, decoded by json.Unmarshal into real control
		// bytes, then rejected by hasControlChars. (A literal \xNN is not a
		// valid JSON escape and fails in Unmarshal first — that would be a
		// decoder error, not the wire-safety error this table pins.)
		{"crlf-in-path", `{"slug":"abc","target_url":"https://example.com/x\u000d\u000aX-Evil: yes","status":"active"}`},
		{"crlf-in-authority", `{"slug":"abc","target_url":"https://example.com\u000d\u000aX-Evil: yes","status":"active"}`},
		{"lf", `{"slug":"abc","target_url":"https://example.com/x\u000aInjected: 1","status":"active"}`},
		{"tab", `{"slug":"abc","target_url":"https://example.com/x\u0009yes","status":"active"}`},
		{"nul", `{"slug":"abc","target_url":"https://example.com/x\u0000nul","status":"active"}`},
		{"esc", `{"slug":"abc","target_url":"https://example.com/\u001b[31mred\u001b[0m","status":"active"}`},
		{"del", `{"slug":"abc","target_url":"https://example.com/\u007fdel","status":"active"}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := ParseLink([]byte(tc.raw))
			if !errors.Is(err, ErrUnsafeTargetURL) {
				t.Errorf("got %v, want ErrUnsafeTargetURL", err)
			}
		})
	}
}

// TestParseLink_AcceptsPercentEncodedControlCharacters pins what must NOT be
// rejected: "%0d%0a" is inert literal text inside the Location header value
// and is only decoded by the *new* URL, never by this app's emission — so it
// stays a servable target (and api/links.py's target_url_error accepts it
// too, see tests/test_target_url_control_chars.py).
func TestParseLink_AcceptsPercentEncodedControlCharacters(t *testing.T) {
	raw := []byte(`{"slug":"abc","target_url":"https://example.com/x%0d%0a","status":"active"}`)
	if _, err := ParseLink(raw); err != nil {
		t.Fatalf("percent-encoded target rejected: %v", err)
	}
}
