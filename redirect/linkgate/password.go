package linkgate

import (
	"crypto/pbkdf2"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"strconv"
	"strings"
)

// MaxStoredPBKDF2Iterations caps the iteration count a stored
// "pbkdf2_sha256$<iter>$..." hash may claim. This app always writes exactly
// api/auth.py's PBKDF2_ITERATIONS (100,000), so the cap exists to bound a
// corrupted or malicious record, not to accommodate variety: a hostile
// restore (link password hashes are deliberately not stripped from backups,
// and api/backup.py's validate_backup is the earlier choke point) or a
// hand-edited store could otherwise plant an absurd count on one link's hash
// and turn every guess against it — and every POST /r/{slug} submits
// guesses — into an unbounded PBKDF2 computation on the redirect hot path.
//
// 1,000,000 is 10x the shipped value: room for any legitimate future raise
// (current guidance is ~600k for PBKDF2-HMAC-SHA256) while capping an
// attacker's CPU amplification at ~10x rather than leaving it unbounded.
//
// Deliberately NOT cross-language pinned against
// auth.MAX_STORED_PBKDF2_ITERATIONS the way keys.go's prefixes/CountShards
// are: the two are independent policy constants (each language clamps the
// hashes it verifies), and a divergence means a slightly different
// leniency bound, not a silently-broken shared data format.
const MaxStoredPBKDF2Iterations = 1_000_000

// VerifyPassword checks password against a "pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>"
// value using only stdlib crypto/pbkdf2 + crypto/subtle — no network calls, no extra dependencies.
func VerifyPassword(password, stored string) bool {
	parts := strings.Split(stored, "$")
	if len(parts) != 4 || parts[0] != "pbkdf2_sha256" {
		return false
	}

	iterations, err := strconv.Atoi(parts[1])
	if err != nil {
		return false
	}
	// The clamp is checked BEFORE any hashing, so an absurd count costs one
	// integer comparison, never CPU. pbkdf2.Key rejects iterations < 1 on its
	// own, but the bounds are stated here explicitly rather than left to that
	// library behaviour: this function is the point of use for the hot path.
	// The lower bound of 1 matters only for hygiene (this app never writes
	// below 100,000); the upper bound is the whole point (see the constant
	// above).
	if iterations < 1 || iterations > MaxStoredPBKDF2Iterations {
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
