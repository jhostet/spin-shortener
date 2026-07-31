# Test Coverage Foundations + Time-Windowed Short Links

## Context

Two things are needed before continuing to the original Phase 3 (analytics + GUI polish): real automated test coverage (the repo has none today, and "no tests" stops being an acceptable state now that the codebase is growing), and a new product feature — short links that are only active within a creator-specified start/end date-time window (e.g. a promo link that shouldn't work before launch or after it ends).

A research spike (this session) surfaced two hard constraints that shape the whole approach:
- **Go**: `go test`/`go build`/`go vet` all fail on anything importing `spin-go-sdk` (`wit_exports.go:934:6: missing function body` — a generated file only completed by the special `go tool componentize-go build` toolchain). Only a subpackage with zero `spin-go-sdk` imports is host-testable via plain `go test`.
- **Python**: `spin_sdk.http`/`key_value`/`variables` all fail at **import time** under host CPython (`ModuleNotFoundError: No module named 'componentize_py_runtime'`) — these are synthetic bindings only injected by the actual componentize-py build/run pipeline. Any module importing them can't even be collected by pytest.

Both have clean fixes that don't sacrifice production behavior (detailed below), and both follow the same principle: **isolate pure logic into files/packages with zero SDK imports; keep the thin SDK-touching glue (KV I/O, HTTP entrypoint wiring) covered by the manual `spin up` + curl smoke testing already used throughout this project, not unit tests.**

Confirmed decisions:
- Retroactively add tests for existing Phase 1-2 logic now, not just tooling for future work.
- No CI setup in this task — local `go test`/`pytest` only; CI is a fast-follow.
- Out-of-window links return a plain 404, identical to a nonexistent link — no distinct "not yet active"/"expired" messaging, so a probing visitor can't learn a link's existence or schedule.
- Link time windows are editable after creation via a new `PATCH /api/links/{slug}` — this endpoint was planned in the original design but never implemented, so this closes that gap too.

This becomes a new **Phase 3** inserted into `TASKS.md`, between the completed Phase 2 and the not-yet-started analytics/GUI-polish phase (which shifts to Phase 4, and hardening/deploy to Phase 5 — header renumbering only).

## Go refactor for testability

New package `redirect/linkgate/` (zero `spin-go-sdk` imports, confirmed via `go list -deps` that stdlib crypto/json/time have no wasm/wasi/syscall-js dependency):
- `link.go`: exported `Link` struct (moved from `main.go`'s unexported `link`, same JSON tags) + `ParseLink(raw []byte) (Link, error)`. Adds two new fields: `StartAt`, `EndAt` (`json:"start_at"`/`"end_at"`, plain strings — JSON `null` unmarshals into a non-pointer Go string as a no-op, leaving `""`, exactly like `PasswordHash` already behaves).
- `password.go`: `VerifyPassword(password, stored string) bool` — moved verbatim from `passwordgate.go`'s `verifyLinkPassword`.
- `window.go`: new `IsWithinWindow(startAt, endAt string, now time.Time) bool` — inclusive start / exclusive end, parses via `time.RFC3339`; empty string means unbounded on that side; unparsable-but-non-empty fails closed (returns false), consistent with malformed KV records already failing closed via `ParseLink`'s error path.

`main.go`/`passwordgate.go` keep KV I/O, `http.ServeMux` wiring, and the `//go:embed prompt.html` template (embed must stay co-located with `package main`), calling into `linkgate.ParseLink`/`VerifyPassword`/`IsWithinWindow` instead of local equivalents.

**Test command: `go test ./linkgate/...` (run from `redirect/`)** — not `go test ./...`, which will still fail against `package main` for the same SDK-import reason as `go build`. This must be called out explicitly in CLAUDE.md so it isn't mistaken for a broken test suite.

## Time-window redirect logic

Wire `linkgate.IsWithinWindow` into both `handleRedirectGet` and `handleRedirectPost` in `main.go`, on the same line as the existing `Status != "active"` check, same fail-closed 404:
```go
if !ok || l.Status != "active" || !linkgate.IsWithinWindow(l.StartAt, l.EndAt, time.Now()) {
    http.NotFound(w, r)
    return
}
```
Both handlers already re-fetch the link fresh from KV on every request (the password gate's existing "never cache" principle) — the window check inherits that for free. `time.Now()` is confirmed to work under the wasip1/componentize-go target (backed by `wasi_snapshot_preview1 clock_time_get`).

## API changes

**`POST /api/links`** (`api/links.py`): optional, independently-nullable `start_at`/`end_at` in the payload. New helpers in `api/responses.py`: `parse_iso8601_utc(value) -> datetime | None` (via `datetime.fromisoformat`, rejecting naive/no-timezone values) and `to_iso8601_utc(dt) -> str` (canonical `%Y-%m-%dT%H:%M:%SZ`; `iso_now()` refactors to use it too). Validation: each field parsed independently (400 `invalid_start_at`/`invalid_end_at` on failure); if both resolve non-null, require `start_at < end_at` strictly (400 `invalid_window_range`) — a window that can never be active is a creation mistake, not a valid state.

**New `handle_update` → `PATCH /api/links/{slug}`** (`api/links.py`, wired into `api/app.py`'s existing `/api/links/{slug}` branch alongside GET/DELETE — `check_csrf` already treats PATCH as protected, no changes needed there): fetch → owner-or-admin check (matching `handle_get`/`handle_delete`) → parse body → accept `target_url`/`status`/`start_at`/`end_at`, each **presence-checked via `"key" in payload`** (not `.get()`) so explicit `null` clears a field while an absent key leaves it untouched → re-validate the *merged* window (new value if provided, else existing stored value) so e.g. patching only `end_at` earlier than an existing `start_at` still 400s → empty payload (no recognized keys) → 400 `no_fields_to_update` → apply, bump `updated_at`, return `_public_link(record)`.

**GUI**: `gui/dashboard.html`/`app.js` gets optional `datetime-local` inputs on the create form (converted via `new Date(localValue).toISOString()` before sending — blank stays out of the payload), an `api.patch` helper alongside the existing `api.get`/`post`/`delete`, Starts/Expires columns in the link table, and a basic edit affordance to PATCH an existing link's window. Deeper styling stays deferred to the (renumbered) Phase 4 polish pass.

## Python refactor for testability

Move `Request`/`Response` dataclasses into `api/responses.py` as plain local dataclasses (same field shape/order as `spin_sdk.http`'s, e.g. `Response(status, headers, body)`) — safe because neither our code nor the real `Handler.handle()` ever does an `isinstance` check, only duck-typed attribute access. `auth.py` and `qr.py` switch their imports from `spin_sdk.http` to `responses`. `links.py` needs no change (confirmed it has no `spin_sdk` import today).

`ensure_bootstrap_admin` changes from reading `spin_sdk.variables` internally to accepting resolved credentials: `ensure_bootstrap_admin(store, username, password)`. `auth.py` drops its `spin_sdk` import entirely. `api/app.py` (the one module that legitimately keeps real `spin_sdk` imports, since it's the actual WASI entrypoint) resolves both variables and passes them in, mirroring the existing `_cookie_secure()` pattern.

End state: `auth.py`, `links.py`, `qr.py`, `responses.py` have **zero** `spin_sdk` imports → fully host-importable and unit-testable. `app.py` stays covered by manual `spin up` + curl, as it has been all along.

## Test suite

**Go** (`redirect/linkgate/*_test.go`, run via `go test ./linkgate/...`): password verify (correct/wrong/malformed-hash table); link parsing (valid full/partial JSON, malformed JSON error); window check (no-window, before-start, exactly-at-start [active], mid-window, exactly-at-end [inactive], after-end, start-only, end-only, malformed timestamps fail closed, degenerate `start==end` always inactive).

**Python** (`api/tests/`, new `fakes.py` with an in-memory `FakeStore` implementing `get`/`set`/`delete`/`exists`; `pyproject.toml` gets `uv add --dev pytest` + `[tool.pytest.ini_options] pythonpath = ["."]`, run via `uv run pytest` from `api/`):
- `test_auth.py`: hash/verify roundtrip + wrong password + malformed stored value; `LocalAuthProvider.authenticate` valid/wrong/disabled/unknown; session create/resolve/expire(+deletes record)/tampered-cookie; CSRF match/mismatch/GET-exempt; permission admin-bypass/explicit/forbidden; `ensure_bootstrap_admin` seeds once, doesn't reseed.
- `test_links.py`: custom-slug validation boundaries; create with/without permission, collision (409); random-slug collision retry; invalid target URL/short password; get/delete owner/admin/forbidden/not-found; set-password set/change/clear; window validation on create (valid combinations, inverted range, malformed, explicit null); `handle_update` partial updates, status validation, merged-window revalidation, empty-payload 400.
- `test_qr.py`: not-found/forbidden; valid svg/png at both sizes (content-type + magic-byte checks); invalid format/size 400; download header; and critically — mock `qrcode.make` to assert it's called with `{public_base_url}/r/{slug}`, never the raw `target_url`.

## CLAUDE.md updates

Replace "There are no tests, linters, or CI configured yet" with concrete commands (`go test ./linkgate/...` from `redirect/`; `uv run pytest` from `api/`), an explicit warning that `go test ./...`/`go build ./...`/`go vet ./...` still fail on `package main` and why, a note that `app.py` is intentionally excluded from unit tests, and guidance that new pure logic should be written against `responses.py`'s local `Request`/`Response` types with `store` passed in as a parameter — preserving the testable/untestable boundary. Also document the `start_at`/`end_at` convention (ISO8601 UTC, inclusive/exclusive, 404-not-messaging).

## Critical files
- `redirect/linkgate/link.go`, `password.go`, `window.go` (new)
- `redirect/main.go`, `redirect/passwordgate.go`
- `api/responses.py` (Request/Response relocation + iso8601 helpers)
- `api/auth.py` (import switch + `ensure_bootstrap_admin` signature)
- `api/qr.py` (import switch)
- `api/links.py` (window validation + `handle_update`)
- `api/app.py` (PATCH route wiring, bootstrap credential resolution)
- `api/tests/fakes.py`, `test_auth.py`, `test_links.py`, `test_qr.py` (new)
- `gui/dashboard.html`, `gui/app.js`
- `CLAUDE.md`, `TASKS.md`

## Verification
1. `cd redirect && go test ./linkgate/...` — all new/moved tests pass.
2. `cd api && uv run pytest` — all new tests pass.
3. `spin up --build --runtime-config-file runtime-config.toml` (with bootstrap password + `cookie_secure=false` for local http) — confirm the refactor didn't change behavior: login, create a link, `/r/{slug}` still redirects, password gate still works.
4. Manually create a link with `start_at` in the future → `/r/{slug}` returns 404; PATCH `start_at` into the past → now redirects; PATCH `end_at` into the past → 404 again.
