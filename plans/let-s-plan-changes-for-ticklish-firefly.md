# spin-shortener: URL Shortener Feature Build-Out

## Context

`spin-shortener` is currently an early-stage scaffold: three stub Spin components (`redirect` in Go, `api` in Python, `gui` as an empty static-file directory) wired together in `spin.toml` with no real logic, no KV storage, and no auth. The goal is to turn this into a real URL shortener: auto-generated and (permission-gated) custom short links, per-link QR codes suitable for web/print/video, lightweight click analytics, an app-portal login (local auth now, SAML/OIDC pluggable later), and optional per-link visitor passwords. The long-term deployment target is Akamai Functions/Akamai KV, but the app must stay portable across any Spin-compliant host, so all storage goes through Spin's own generic key-value API rather than a custom abstraction.

Given the size of this feature set, the work is sequenced into four phases so each can ship and be validated before the next builds on it, rather than attempting one big-bang implementation. Throughout, the existing Go/Python split is preserved: `redirect` stays minimal and network-free (`allowed_outbound_hosts = []` never changes), doing only KV lookups and local computation; all authoring, permissions, auth, and analytics aggregation live in `api`.

Confirmed decisions driving this plan:
- Phase the build (P1–P4 below), don't build everything at once.
- v1 auth = local username/password only, but architected so SAML/OIDC can be added as additional providers later without reworking sessions/permissions.
- Storage = Spin's built-in key-value API only (Go `spin-go-sdk/v3/kv`, Python `spin_sdk.key_value`) — no custom storage layer.
- `redirect` stays minimal: it alone does the KV lookup, the password-gate check/prompt, and the redirect. No calls to `api`, no network calls.
- Password-protected links: **re-prompt on every click**, no "remember me" unlock cookie in v1 — simplest, most secure, and a password change/removal takes effect immediately with no stale-cookie edge cases.

## Key technical facts confirmed during research

- `redirect` is standard Go compiled to `wasip1/wasm` (not TinyGo), on Go 1.25.5. **Go's standard library already has `crypto/pbkdf2`** (confirmed present via `go doc crypto/pbkdf2` on this machine, added in Go 1.24) — password hashing needs **zero new Go dependencies**, keeping `redirect`'s `go.mod` exactly as lean as it is today.
- Python's `hashlib.pbkdf2_hmac` + `hmac.compare_digest` + `secrets.token_urlsafe` are pure stdlib — no new dependency for auth/session code either. `componentize-py` cannot bundle C-extension packages (rules out `bcrypt`, `argon2-cffi`, Pillow), so stdlib PBKDF2 is also the *safe* choice, not just the convenient one.
- QR generation **will** need a first-ever third-party Python dependency: the pure-Python `qrcode` package (with `qrcode.image.svg.SvgImage` for vector output and `qrcode.image.pure.PyPNGImage`, backed by pure-Python `pypng`, for raster) — both avoid Pillow. Because this is new toolchain territory, the phase that introduces it starts with a small build spike (add deps, `uv run componentize-py ... -o app.wasm`, confirm it runs under `spin up`) before writing the full endpoint.
- Spin's generic KV interface (`Get`/`Set`/`Delete`/`Exists`/`GetKeys`) has **no TTL, no atomic increment/CAS, no prefix scan**. This shapes the analytics design (deterministic slot keys instead of scans/atomic counters) and means session expiry and password rate-limiting are enforced in application logic, not the KV layer — the latter is called out explicitly as a soft/best-effort control, not a real security boundary, given `redirect` also has no outbound network access to reach a real atomic rate limiter.
- Both languages' HTTP handler contracts (`spinhttp.Handle` in Go, `Handler` in Python) are single blocking request→response calls — there's no fire-and-forget primitive, so any analytics writes from `redirect` execute synchronously before the redirect response, wrapped so a KV error there never blocks the redirect itself.

## Data model (Spin KV)

Three named KV stores, least-privilege per component:

| Store | Granted to | Contents |
|---|---|---|
| `links` | `redirect` (read), `api` (read/write) | link records, owner→slug index |
| `users` | `api` only | user accounts, sessions — `redirect` never gets this store; it has no legitimate reason to see credentials |
| `analytics` | `redirect` (write), `api` (read/aggregate) | click counters, bounded recent-events ring buffer |

`spin.toml`: `[component.redirect] key_value_stores = ["links", "analytics"]`; `[component.api] key_value_stores = ["links", "users", "analytics"]`. No `[key_value_store.*]` runtime-config block is needed for local dev — Spin's CLI auto-provisions local stores for any named store a component declares; mapping these to Akamai's KV product is a P4 deploy concern.

**`links` store:**
- `slug:<slug>` → `{slug, target_url, owner, custom: bool, password_hash: str|null, status: "active"|"disabled", created_at, updated_at}`
- `owner_links:<username>` → JSON array of owned slugs (best-effort index; not transactionally consistent with KV's primitives, acceptable at this scale)
- Random slugs: base62, length 7, collision-checked via `Exists`, retried up to ~5x. Custom slugs: validated charset/length, gated by the `links.create_custom_slug` permission, checked in `api` only.

**`users` store:**
- `user:<username>` → `{username, password_hash, role: "admin"|"user", permissions: [...], provider: "local", disabled, created_at}` — `provider` is the seam for future SAML/OIDC-sourced users.
- `session:<token>` → `{username, csrf_token, issued_at, expires_at, auth_provider}` — opaque `secrets.token_urlsafe(32)` token, no embedded signature needed (256 bits of CSPRNG entropy is already unguessable); KV record is the sole authority for validity/expiry, checked and lazily deleted on every request.
- Bootstrap admin: if `GetKeys` finds zero `user:*` records at all, seed one admin from two Spin variables (`admin_bootstrap_username`, `admin_bootstrap_password`, `secret = true`), hashed into KV immediately and never read again.

**`analytics` store** (designed for hot-path cheapness):
- `count:<slug>` → `{total, days: {"YYYY-MM-DD": n, ...}}` — one GET+SET per click, bundling total and per-day so there's a single round trip; `days` trimmed to a bounded retention window on write.
- `events:<slug>:<slot>` where `slot = now_nanos % N` (N via a shared variable, e.g. 30) — a blind overwrite (`"<unix_ms>|<referrer>|<ua_class>"`), no read required, genuinely O(1); "recent events" reads in `api` are `N` direct `Get`s on deterministic keys, never a scan.
- Explicit accepted tradeoff: the `count` read-modify-write is racy under truly concurrent clicks on the same slug (no CAS available) and can slightly under-count during bursts. Documented as accepted given the "cheap, simple" requirement; sharded counters are a noted future option, not built now.

## Component responsibilities & endpoints

**`redirect` (Go)** — `redirect/main.go`, using a `http.ServeMux` inside the existing `spinhttp.Handle` callback:
- `GET /r/{slug}` — KV lookup; 404 if missing/disabled; if `password_hash` set, render an inline password-prompt page (built via `//go:embed` + `html/template` for auto-escaping, not fetched from `gui/` — `redirect` stays self-contained); else best-effort analytics write, then `302` to `target_url`.
- `POST /r/{slug}` — re-fetch the link fresh from KV (never trust the GET-step read), verify submitted password via stdlib `crypto/pbkdf2` + `crypto/subtle.ConstantTimeCompare` against the stored `pbkdf2_sha256$<iter>$<salt>$<hash>` value; match → `302`; no match → re-render prompt with error, `401`. If the password was removed between GET and POST, just redirect — every request re-reads current KV state, which is what makes password changes take effect instantly with no stale-session window.
- `allowed_outbound_hosts` for `redirect` stays `[]` through every phase — password verification is pure local computation over data already fetched from KV.

**`api` (Python)** — `api/app.py` (likely split into `api/auth.py`, `api/links.py`, `api/qr.py`, `api/analytics.py` imported from the single `handle_request` entrypoint, since `componentize-py` targets the whole package):

| Method | Path | AuthZ |
|---|---|---|
| POST | `/api/auth/login` | public |
| POST | `/api/auth/logout` | session |
| GET | `/api/auth/me` | session |
| GET | `/api/links` (own; `?all=true` needs `links.view_all`) | session |
| POST | `/api/links` (custom slug needs `links.create_custom_slug`) | session |
| GET/PATCH/DELETE | `/api/links/{slug}` | session, owner or admin |
| POST | `/api/links/{slug}/password` | session, owner or admin |
| GET | `/api/links/{slug}/qr?format=svg\|png&size=web\|print&download=1` | session, owner or admin |
| GET | `/api/links/{slug}/analytics` | session, owner or `links.view_all` |
| GET/POST/PATCH/DELETE | `/api/users*` | session, `users.manage` (admin) |

401 = no valid session; 403 = valid session but missing permission/ownership, body includes `required_permission`.

**Auth/session design:**
- `AuthProvider` seam in `api/auth.py`: credential-type providers implement `authenticate(username, password) -> AuthResult|None`; a `LocalAuthProvider` is the only implementation now. Redirect-type providers (future OIDC/SAML) would implement `initiate(request)`/`handle_callback(request)` instead — reserve the `oauth_state:<state>` KV key pattern and `[component.api.variables]` slots (`oidc_client_id`, `oidc_client_secret` as `secret = true`, `oidc_issuer_url`) now, without building either provider.
- Session issuance (`create_session(username, auth_provider)`) is identical regardless of provider — this is the one seam SAML/OIDC plug into later.
- Cookie: `Set-Cookie: session=<token>; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=...` — `Secure` gated behind a `cookie_secure` variable defaulting true but overridable for local `spin up` over plain HTTP. `SameSite=Lax` chosen deliberately (not `Strict`) so it survives a future IdP redirect-based callback.
- CSRF: double-submit — login response includes `csrf_token`; portal JS echoes it as `X-CSRF-Token` on every mutating request; `api` rejects mismatches. Not needed on the anonymous `/r/{slug}` password POST (no session to protect there).
- Password hashing format shared by both user accounts and link passwords: `pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>`. Iteration counts are separate tuning knobs (higher for account passwords, lower for link passwords since that's closer to the hot path) and should be benchmarked in-toolchain (both WASI CPython and Go/Wasm) early in P1 rather than guessed.

**`gui`** — plain multi-page HTML + vanilla JS/`fetch`, no build step (matches the repo's no-extra-toolchain style and `spin-fileserver`'s lack of templating). Vendor a small classless CSS framework (e.g. Pico.css) as a committed file, not a CDN reference. Pages: `login.html`, `dashboard.html` (link list + create form, custom-slug field shown only if `/api/auth/me` reports the permission, though it's enforced server-side regardless), `links/detail.html` (analytics + QR download), `admin/users.html` (admin-only). Shared `app.js` fetch wrapper handles cookies and redirects to login on 401.

## Phases

**P1 — Foundations:** KV wiring in `spin.toml`; `redirect` does real slug lookup → 302 (no password/analytics yet); `api` gets local login/logout/me, bootstrap-admin seeding, link create(random-only)/list/get/delete; bare-bones unstyled `gui` (login, dashboard list, create form). Validates the riskiest shared assumptions (cross-language PBKDF2 format compatibility, session cookie mechanics, local KV auto-provisioning) before layering on complexity.

**P2 — Custom slugs, permissions, password gate, QR:** `links.create_custom_slug` permission + custom-slug creation path; link `password_hash` + `/api/links/{slug}/password` endpoint; full password-gate implementation in `redirect` (prompt page, PBKDF2 verify, re-prompt-every-time per the confirmed decision); QR endpoint + `qrcode`/`pypng` dependency spike.

**P3 — Analytics + full gui polish:** `count`/`events` KV writes in `redirect`; `/api/links/{slug}/analytics` aggregation; `gui/links/detail.html` analytics view; styling/polish pass for the "modern, novice-friendly" requirement across all screens.

**P4 — Hardening + deploy readiness:** admin user-management screens/endpoints; operational hardening notes (documenting the accepted soft-limit on password brute-force, security headers); `runtime-config.toml` mapping the three KV stores to Akamai's KV-backed provider (Akamai's exact runtime-config schema/provider name is a genuine unknown to confirm against their docs at deploy time, not guessable from this repo); README/CLAUDE.md updates; a write-up proving the P1 `AuthProvider` seam is sufficient for SAML/OIDC without building either.

## Critical files

- `spin.toml` — KV store declarations, `[variables]`/`[component.*.variables]` for signing/bootstrap secrets, unchanged `allowed_outbound_hosts = []` on `redirect`
- `redirect/main.go` (+ possibly `redirect/passwordgate.go`, `redirect/prompt.html` via `go:embed`) — slug lookup, password gate, redirect
- `api/app.py`, `api/auth.py`, `api/links.py`, `api/qr.py`, `api/analytics.py` — all authoring/auth/analytics logic
- `api/pyproject.toml` — adds `qrcode`, `pypng` in P2 only; no new deps for auth
- `gui/` — `index.html`, `login.html`, `dashboard.html`, `links/detail.html`, `admin/users.html`, `app.js`, vendored CSS
- `TASKS.md` — per CLAUDE.md's task-tracking convention (`- [ ] Task — file(s): path — done when: <criteria>`), generated from this phase breakdown before implementation starts

## Verification

- After each phase, `spin up --build` locally and exercise the new surface end-to-end by hand: log in via `gui`, create a link (random and, from P2, custom), click the resulting `/r/{slug}` link and confirm the redirect (and password prompt, from P2), check `/api/links/{slug}/analytics` output (from P3), download a QR code and scan it with a phone camera (from P2).
- Confirm `redirect`'s `allowed_outbound_hosts` remains `[]` in `spin.toml` after every phase — a diff here would signal a violation of the hot-path/no-network constraint.
- No existing tests/CI in this repo; if test coverage is wanted for the new logic, that's a decision to make explicitly when P1 implementation starts, not assumed here.
