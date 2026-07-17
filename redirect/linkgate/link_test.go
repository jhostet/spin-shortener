package linkgate

import "testing"

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

func TestParseLink_MalformedJSON(t *testing.T) {
	_, err := ParseLink([]byte(`{not valid json`))
	if err == nil {
		t.Fatal("expected an error for malformed JSON, got nil")
	}
}
