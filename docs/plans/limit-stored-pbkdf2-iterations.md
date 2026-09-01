# Bound the PBKDF2 iteration count a stored hash may claim

**Filed from the 2026-08-31 adversarial security audit** (redirect/deployment
fork), the second of the three deferred findings, picked up 2026-08-31. It is
the audit's independent confirmation of this repo's own review Finding 4
("PBKDF2 iteration counts are read unbounded from stored hashes").

## The defect

Both password verifiers trust whatever iteration count is stored on a hash,
with no upper bound:

- `redirect/linkgate/password.go`'s `VerifyPassword` — the redirect hot path —
  parses `pbkdf2_sha256$<iter>$...` and passes `iterations` straight to
  `pbkdf2.Key`. A count of `2000000000` parses fine (`strconv.Atoi` on 64-bit)
  and means two billion HMAC-SHA256 rounds per guess. Every `POST /r/{slug}`
  on a password-protected link submits a guess, each an unbounded CPU burn.
- `api/auth.py`'s `verify_password` has the identical shape on the login path.
- `api/backup.py`'s `validate_backup` checks a `user:` record's `password_hash`
  only for *presence* (and rejects its very existence as credential material),
  and never inspects a **link** record's `password_hash` at all — and link
  hashes are deliberately NOT stripped from backups.

**How it arrives:** link records reach storage by two routes. Authoring always
writes exactly `PBKDF2_ITERATIONS` (100,000), so the choke point is not the
authoring paths but **restore** (which writes records without re-validating
content, by documented design) and a hand-edited store. Same trust boundary as
the QR-filename and target_url findings, but with no cap today.

**Threat:** an attacker who gets an admin to restore a malicious backup (or a
store that is otherwise corrupted) plants an absurd iteration count on one
link's hash, turning every guess against that link — an unauthenticated,
rate-unlimited POST — into an unbounded PBKDF2 computation. A self-inflicted
CPU-exhaustion knob on the hot path.

## The fix — two choke points, deliberately layered

The finding named both, in order: "clamp or reject an implausible iteration
count in `VerifyPassword` (and/or validate it in `validate_backup`, the earlier
and preferred choke point)". This fix does both, plus the Python verifier for
account hashes (same shape, same hand-edited-store exposure, two lines).

### Choke point 1 (earlier, preferred): `backup.validate_backup`

For every `links` store `slug:` key, if the record's `password_hash` is a
string, parse it through a new shared helper `auth.stored_pbkdf2_iterations`
and reject the whole backup with `unreasonable_password_iterations` (echoing
`store`/`key`/`max_iterations`, the standing "echo the cap" convention) when
the count is outside `[1, MAX]`. Rejects hostile files **before** they can ever
reach a verifier.

Deliberately narrow: only a `pbkdf2_sha256`-shaped hash with a parseable count
is checked. A foreign scheme (or an unparseable count) returns `None` from the
helper and is left to the verifiers' existing fail-closed behaviour — such a
link simply can never be unlocked, which is harmless server-side, and rejecting
it here would make restore a strictness enforcer for a category that is not a
CPU-amplification knob. The narrowness is pinned by a test.

The helper lives in `auth.py` (the natural owner of the password format) and is
shared rather than inlined so backup's notion of the hash shape can never drift
from `verify_password`'s — the same "shared, not module-private" convention
`links.can_view`/`target_url_error` already carry. `backup.py` gains its first
import of `auth`; no cycle (`auth` imports only `responses`).

### Choke point 2 (last line): the two verifiers clamp before hashing

Both `linkgate.VerifyPassword` and `auth.verify_password` reject
`iterations < 1 || iterations > Max` **before** any hashing — one integer
comparison, never CPU. This is what protects the already-stored case (a
hand-edited store, or anything that reaches storage outside restore).

## The bound: `1_000_000`, and why

`MAX_STORED_PBKDF2_ITERATIONS = 1_000_000` (Go:
`MaxStoredPBKDF2Iterations`) — 10x the shipped 100,000. It must be:

- **Above any legitimate raise.** OWASP's current guidance is ~600k for
  PBKDF2-HMAC-SHA256, so 1M leaves room for the app to raise `PBKDF2_ITERATIONS`
  without a migration — each record carries its own count, verified against the
  range, not an exact value.
- **Below absurd.** 10x caps an attacker's CPU amplification at ~10x rather
  than unbounded. The exact multiple is a judgement call; 10x keeps a
  hostile 2-billion count rejected while a future legitimate 600k passes.

No floor stricter than 1: a low count is not a server-side CPU danger (it only
weakens the stored hash, which harms a defender who relies on it — not the
attacker who planted it), and `pbkdf2.Key` rejects `< 1` on its own in Go. The
boundary at exactly `Max` is pinned accepted in Go (a real 1,000,000-iteration
hash, ~0.2s test) and accepted-by-parse in Python (where a 1M pure-Python hash
would be too slow even for a test — the parse is shared, so the count semantic
is pinned without running it).

## Deliberately NOT done

- **No cross-language pin** between
  `MaxStoredPBKDF2Iterations` and `MAX_STORED_PBKDF2_ITERATIONS`, unlike
  `keys.go`'s prefixes/`CountShards`. Those are pinned because a divergence
  breaks a shared data format silently; these are independent policy constants
  (each language clamps the hashes it verifies), and a divergence means a
  leniency difference only. Recorded, not forgotten.
- **No consistency-check finding.** `consistency.collect` never reads a
  `user:` value, and its links-store parse reads only the `owner` field — it
  doesn't parse `password_hash`, and adding a check would pin `ok: false` on a
  structurally fine store for a policy matter (the same reason destination
  policy violations aren't a check). The redirect's verify-time clamp plus
  restore rejection are the diagnosis.
- **No change to `auth.hash_password`** — it already writes exactly
  `PBKDF2_ITERATIONS`, comfortably inside the range.
- **No attempt to equalize login timing** (the audit's separate finding #1 is
  its own item; this fix does not touch the timing oracle, only the ceiling on
  stored counts).

## Files

- `api/auth.py` — `MAX_STORED_PBKDF2_ITERATIONS`, clamp in `verify_password`,
  shared `stored_pbkdf2_iterations` helper
- `api/backup.py` — links-store iteration validation in `validate_backup`
- `redirect/linkgate/password.go` — `MaxStoredPBKDF2Iterations`, clamp in
  `VerifyPassword`
- `api/tests/test_auth.py`, `api/tests/test_backup.py`,
  `redirect/linkgate/password_test.go` — new tests
- `TASKS.md`, `CLAUDE.md` — documentation

## Tests & verification

- **Go**: absurd counts (`2000000000`, `1000001`, `0`, `-100000`) fail fast
  (sub-millisecond — the test completing at all is the assertion); boundary at
  exactly `MaxStoredPBKDF2Iterations` verifies (~0.19s); malformed cases still
  fail. **Mutation-checked**: removing the upper clamp makes
  `TestVerifyPassword_RejectsAbsurdIterationCount` hang (8s timeout) — the
  test is the failure mode the clamp prevents.
- **Python**: absurd counts reject fast (a 100-iteration legitimate hash still
  round-trips); the parse helper returns the count for `pbkdf2_sha256`, `None`
  for every other shape, never raises; backup rejects absurd and zero counts
  with `unreasonable_password_iterations` naming the key and cap, accepts a
  genuine 100k-hash backup, and deliberately leaves foreign/unparseable
  schemes alone. **Mutation-checked**: shrinking `MAX` to 10 fails the
  legitimate-hash acceptance test; disabling the backup links-check fails both
  backup rejection tests.
- **Builds**: `componentize-py` (api) and `componentize-go` (redirect) both
  rebuild; full suites green (api 767, gui-pages 170, Go ok).