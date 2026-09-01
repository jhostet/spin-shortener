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

// TestVerifyPassword_RejectsAbsurdIterationCount pins the CPU-amplification
// clamp (see MaxStoredPBKDF2Iterations): a stored hash claiming an absurd
// iteration count must FAIL VERIFICATION and, critically, fail FAST — the
// assertion that each case completes here at all is the test, because an
// unclamped VerifyPassword would spend minutes hashing at 2,000,000,000
// iterations before returning. This is the mutation-guardable property: removing
// the clamp makes the test hang rather than fail, exactly the failure mode the
// clamp exists to prevent on the real hot path.
func TestVerifyPassword_RejectsAbsurdIterationCount(t *testing.T) {
	cases := []struct {
		name       string
		iterations string
	}{
		{"two-billion", "2000000000"},
		{"max-plus-one", "1000001"},
		{"zero", "0"},
		{"negative", "-100000"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			stored := "pbkdf2_sha256$" + tc.iterations + "$c2FsdA==$aGFzaA=="
			if VerifyPassword("anything", stored) {
				t.Errorf("iterations=%s: expected verification to fail before hashing", tc.iterations)
			}
		})
	}
}

// TestVerifyPassword_AcceptsBoundaryIterationCount pins that the cap is a
// range, not a rejection of anything above the shipped 100,000: a hash
// claiming exactly MaxStoredPBKDF2Iterations is accepted (by construction, in
// the sense that it verifies correctly when the digest matches), so a future
// legitimate raise of PBKDF2_ITERATIONS up to the cap keeps working with no
// migration. At 1,000,000 iterations this is the slowest test in the package
// (~100-300ms); the alternative — asserting 1000001 is rejected but 1000000
// has no test — would leave the boundary value unpinned.
func TestVerifyPassword_AcceptsBoundaryIterationCount(t *testing.T) {
	stored := makeStoredHash(t, "hunter2", MaxStoredPBKDF2Iterations)
	if !VerifyPassword("hunter2", stored) {
		t.Fatal("expected a hash at exactly MaxStoredPBKDF2Iterations to verify")
	}
}

// TestVerifyPassword_RejectsBelowLowerBound pins the lower edge of the same
// range: 0 (and the malformed cases above) must fail fast. A low count is not
// a CPU danger server-side — it only weakens the stored hash, which harms no
// one but a defender who relies on it — but it is implausible for anything
// this app ever wrote, and accepting it would make the range check look
// optional.
func TestVerifyPassword_RejectsZeroAndNegative(t *testing.T) {
	for _, it := range []string{"0", "-1", "-100000"} {
		if VerifyPassword("anything", "pbkdf2_sha256$"+it+"$c2FsdA==$aGFzaA==") {
			t.Errorf("iterations=%s: expected verification to fail before hashing", it)
		}
	}
}
