package linkgate

import (
	"crypto/pbkdf2"
	"crypto/sha256"
	"encoding/base64"
	"fmt"
	"testing"
)

// makeStoredHash builds a valid "pbkdf2_sha256$<iter>$<salt_b64>$<hash_b64>"
// value for a given password, using the same stdlib primitives VerifyPassword
// itself relies on (Go never hashes passwords in production -- only the
// Python side does -- so this is purely test fixture construction).
func makeStoredHash(t *testing.T, password string, iterations int) string {
	t.Helper()
	salt := []byte("fixed-test-salt-16b")
	digest, err := pbkdf2.Key(sha256.New, password, salt, iterations, 32)
	if err != nil {
		t.Fatalf("failed to build fixture hash: %v", err)
	}
	return fmt.Sprintf("pbkdf2_sha256$%d$%s$%s", iterations,
		base64.StdEncoding.EncodeToString(salt),
		base64.StdEncoding.EncodeToString(digest))
}

func TestVerifyPassword_CorrectPasswordSucceeds(t *testing.T) {
	stored := makeStoredHash(t, "hunter2", 1000)
	if !VerifyPassword("hunter2", stored) {
		t.Fatal("expected correct password to verify successfully")
	}
}

func TestVerifyPassword_WrongPasswordFails(t *testing.T) {
	stored := makeStoredHash(t, "hunter2", 1000)
	if VerifyPassword("wrong", stored) {
		t.Fatal("expected wrong password to fail verification")
	}
}

func TestVerifyPassword_MalformedStoredValues(t *testing.T) {
	cases := []string{
		"",
		"not_pbkdf2$1000$c2FsdA==$aGFzaA==",
		"pbkdf2_sha256$1000$c2FsdA==",
		"pbkdf2_sha256$notanumber$c2FsdA==$aGFzaA==",
		"pbkdf2_sha256$1000$not-valid-base64!!!$aGFzaA==",
		"pbkdf2_sha256$1000$c2FsdA==$not-valid-base64!!!",
	}
	for _, stored := range cases {
		if VerifyPassword("anything", stored) {
			t.Errorf("expected malformed stored value %q to fail verification, not panic or succeed", stored)
		}
	}
}
