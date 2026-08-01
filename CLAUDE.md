# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

`spin-shortener` is a polyglot WebAssembly URL shortener built on [Spin](https://spinframework.dev) (Fermyon's WASI HTTP framework), with three independently-built components composed via `spin.toml`. Shipped functionality: auto-generated and permission-gated custom short links, optional per-link passwords, optional start/end time windows, per-link QR codes (SVG/PNG), click analytics (totals, per-day, a best-effort recent-events sample), local username/password auth with session cookies, and admin user management. See `TASKS.md` for the full phase-by-phase build history and `README.md` for a user-facing overview.

## Architecture

`spin.toml` is the single source of truth for routing and build wiring. It defines four Wasm components across six HTTP triggers (two components — `gui` and `gui-pages` — split the GUI's routes between them):

- `route = "/r/..."` → **`redirect`** component (`redirect/`, Go) — resolves short links and issues redirects; the hot path, hit on every click. Built with `go tool componentize-go build`, compiling `redirect/main.go` (+ `passwordgate.go`, the embedded `prompt.html`, and the pure-logic `redirect/linkgate/` package) to `redirect/main.wasm`. Uses `github.com/spinframework/spin-go-sdk/v3/http` and registers a handler via `spinhttp.Handle`. `allowed_outbound_hosts = []` — no outbound network access, by design (see "Security tradeoffs" below for what this rules out).
- `route = "/api/..."` → **`api`** component (`api/`, Python) — all authoring/auth/analytics logic: link CRUD, custom slugs, passwords, time windows, QR generation, analytics aggregation, local auth/sessions, and user management. Built with `uv run componentize-py -w spin:up/http-trigger@4.0.0 componentize app -o app.wasm`, compiling `api/app.py` (the WASI entrypoint/router) plus `auth.py`/`links.py`/`qr.py`/`analytics.py`/`users.py`/`responses.py`. Uses `spin_sdk.http.Handler`.
- `route = "/app.js"`, `route = "/theme.css"`, `route = "/vendor/pico.min.css"`, plus one route per page-scoped asset (`/index.js`, `/login.js`, `/dashboard.js`, `/dashboard.css`, `/admin/users.js`, `/admin/users.css`, `/links/detail.js`, `/links/detail.css`) → **`gui`** component — a prebuilt static file server (`spin_static_fs.wasm`, fetched by digest from the `spin-fileserver` GitHub release) serving only these genuinely-static, non-HTML assets. Its `files` mapping still covers all of `gui/` (unchanged from before the route split — narrowing it broke file resolution, see below), but only these 11 exact routes are actually reachable. The 8 page-scoped ones exist because the CSP dropped `'unsafe-inline'`; adding a page's asset without its route serves a fully-rendered page whose script silently 404s. **Route gotcha, confirmed live:** once this component has more than one trigger route, `spin_static_fs`'s internal path resolution breaks specifically for wildcard (`/...`) routes — a `/vendor/...` wildcard 404'd on every request; the identical file under the exact route `/vendor/pico.min.css` served correctly. Stick to exact routes for this component if any more assets are ever added to it.
- `route = "/..."` (catch-all) → **`gui-pages`** component (`gui-pages/`, Python, same `componentize-py` toolchain as `api`) — serves the GUI's actual HTML pages (`index.html`, `login.html`, `dashboard.html`, `admin/users.html`, `links/detail.html`) via a fixed path→file allowlist (`gui-pages/routing.py`), and attaches the security response headers below to every response. Introduced specifically because `spin_static_fs` has no custom-header capability at all (confirmed: only a `CACHE_CONTROL` env var) — security headers (CSP, `X-Frame-Options`, etc.) are only meaningful on the navigated document itself, not on a `.js`/`.css` subresource, so only the actual HTML pages needed to move off the static-fileserver.

Each component is built independently and only in its own `workdir`; there is no shared build step or root-level package manifest. When editing one component, you generally don't need to touch the others' toolchains.

**Why Go for `redirect` but Python for `api`/`gui-pages`:** the redirect path is the hot path (every short-link click) and is written in Go for raw performance. The `api`/`gui-pages` surfaces (link creation, management, frontend) aren't on that hot path, so they're written in Python to prioritize developer velocity and code understandability over raw speed — the performance tradeoff isn't worth it there. Keep this split in mind when adding new functionality: if it's on the redirect hot path, it likely belongs in the Go component; otherwise default to Python for velocity.

`redirect/main.wasm`, `api/app.wasm`, and `gui-pages/app.wasm` are all build artifacts and are gitignored — they must be rebuilt via `spin up --build` (or the per-component build commands in `spin.toml`) after any source change; they are not checked into the repo.

## Security response headers

Every response from `redirect`, `api`, and `gui-pages` sets `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, `X-Frame-Options: DENY`, and `Strict-Transport-Security`. Each component's CSP is scoped to what it actually serves:

- `gui-pages` (the real HTML pages): `default-src 'self'` plus `script-src 'self'` and `style-src 'self'` — **no `'unsafe-inline'`.** Every page's script and style live in a sibling `.js`/`.css` file served by the `gui` component (e.g. `dashboard.html` → `dashboard.js` + `dashboard.css`), so no served page contains an inline `<script>`, a `<style>` block, or a `style="…"` attribute for the policy to have to allow. `gui-pages/tests/test_no_inline_code.py` enforces that — a CSP violation fails a page silently in a browser rather than failing a test, so the guard is what keeps the policy true. Hiding is done with the native `hidden` attribute plus `theme.css`'s `[hidden] { display: none !important; }` (the `!important` is load-bearing — Pico sets `display` on `label`, `nav li`, and buttons, all elements this app hides, and the UA stylesheet's `display: none` loses to them). Every other directive is locked down for real: no plugins/objects, no framing, no cross-origin form submission, no base-tag hijacking. `img-src` includes `data:` because Pico CSS renders several UI affordances (sortable-column chevrons, the search icon, the calendar icon) as inline `data:image/svg+xml` background-images — confirmed live that `img-src 'self'` alone produced real CSP-violation console errors blocking them, caught only by loading the actual pages, not by reading the CSS.
- `api` (pure JSON): `default-src 'none'` — nothing it returns should ever be rendered, executed, or framed.
- `redirect`'s password-prompt page (the one HTML `redirect` renders): `script-src 'none'` (the page has zero `<script>` tags) plus `style-src 'unsafe-inline'` for its one inline `style="color: red"` error-message attribute.

The `gui` component (the static asset routes above) gets none of these — headers on a `.js`/`.css` subresource response don't provide any real protection the navigated-document headers don't already cover.

## Task tracking
- Maintain a `TASKS.md` file in the repo root as the single source of truth for multi-step work.
- Each task uses the format: `- [ ] Task name — file(s): path/to/file — done when: <criteria>`
- Before starting any task, re-read TASKS.md.
- After finishing a task, immediately update its checkbox in TASKS.md before starting the next one — don't batch updates at the end.
- If context is compacted or a new session starts, re-read TASKS.md before doing anything else.

## Planner / builder subagents

Non-trivial work in this repo goes through two committed subagents in `.claude/agents/`:

- **`planner`** (opus) — explores the code, weighs trade-offs, and writes a plan to `docs/plans/<feature>.md` (kebab-case, descriptive — never a random slug). It also appends the unchecked `- [ ] ... — file(s): ... — done when: ...` lines to `TASKS.md` under a new descriptively-named section at the end of the file, and records rejected alternatives under `TASKS.md`'s "Considered and rejected". It never writes implementation code — its only writable paths are `docs/plans/`, its scratch file, and appends to `TASKS.md`.
- **`builder`** (sonnet) — reads the plan file first, implements it following existing conventions, ticks each `TASKS.md` checkbox immediately after finishing that task, and verifies with the real test suites plus a live `spin up` run for user-visible changes. It stops and reports back rather than silently deviating when the plan turns out to be wrong.

Typical flow: invoke `planner` with the requirement and the relevant file paths → review the plan it writes → invoke `builder` with that plan's path. Skip both for one-line fixes; the overhead isn't worth it. When delegating, give concrete references — file paths, the existing code the change interacts with, the specific requirement and non-goals — not "implement the plan"; anything left unstated gets filled in confidently and possibly wrong.

Plans live in `docs/plans/` and are committed. Multi-round work uses `docs/plans/<feature>-scratch.md` (gitignored via `docs/plans/*-scratch.md`) as an append-only handoff note — one `## Round <n> — <agent> — <date>` heading per round, with Done / Open questions / Next. It is a handoff note, not either agent's memory: anything durable gets promoted into the plan file, `TASKS.md`, or here.

## Commands

Build and run the whole app (all four components) locally:

```bash
SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<some-password> spin up --build --runtime-config-file runtime-config.toml
```

This invokes each component's `[component.<name>.build]` command from `spin.toml` and then serves all routes together. Requires the [Spin CLI](https://spinframework.dev) to be installed.

`--runtime-config-file runtime-config.toml` is required locally: Spin does not auto-provision named (non-`default`) key-value stores, so the `links`/`users`/`analytics` stores declared in `spin.toml` must be mapped to a backing provider via `runtime-config.toml` (sqlite-backed `type = "spin"` for local dev). `admin_bootstrap_password` is a required secret variable (seeds the first admin user on a fresh KV store) and has no default, so it must be supplied via env var (or another Spin variable provider) on every run.

When testing the `gui` in a real browser over plain `http://localhost`, also set `SPIN_VARIABLE_COOKIE_SECURE=false` — the session cookie's `Secure` flag otherwise stops the browser from storing/sending it over non-HTTPS, breaking login. Leave `cookie_secure` at its default `true` for any HTTPS deployment.

Per-component builds (equivalent to what `spin up --build` runs), if you need to build just one component while iterating:

```bash
# redirect (Go) — from redirect/
go tool componentize-go build

# api (Python) — from api/
uv run componentize-py -w spin:up/http-trigger@4.0.0 componentize app -o app.wasm

# gui-pages (Python) — from gui-pages/
uv run componentize-py -w spin:up/http-trigger@4.0.0 componentize app -o app.wasm
```

Python component dependencies (`componentize-py`, `spin-sdk`) are managed by [`uv`](https://docs.astral.sh/uv/) and pinned in each Python component's own `pyproject.toml`/`uv.lock` (`api/` and `gui-pages/` each have their own — they're independent, not shared). `uv run` syncs each component's own `.venv` from its lockfile automatically before running, so no manual install step is required — even on a fresh clone, `spin up --build` just works. (To set up the environment yourself, e.g. for editor/language server support, run `uv sync` from `api/` or `gui-pages/`.)

## Tests

Go and Python each have their own test suite. A `Jenkinsfile` at the repo root runs both suites (plus `gui-pages`'s) in parallel, each on its own pinned Docker agent — wire it up as a Jenkins Multibranch Pipeline (or "Pipeline script from SCM") job pointed at this repo to get it running; nothing auto-registers it.

```bash
# redirect (Go) — from redirect/
go test ./linkgate/...

# api (Python) — from api/
uv run pytest

# gui-pages (Python) — from gui-pages/
uv run pytest
```

**`go test ./...` (bare), `go build ./...`, and `go vet ./...` will FAIL** on `package main` with `wit_exports.go:934:6: missing function body` — `main.go`/`passwordgate.go` import `spin-go-sdk`, which only compiles via the special `go tool componentize-go build` toolchain, not plain `go`. This is expected, not a broken build. Only `redirect/linkgate/` (zero `spin-go-sdk` imports) is host-testable — new pure Go logic belongs there, not in `package main`.

`app.py` is intentionally excluded from `pytest` in both `api/` and `gui-pages/` — it's the real WASI entrypoint (routing dispatch + actual `spin_sdk.key_value`/`variables`/`http.Handler` I/O, or in `gui-pages`'s case, real WASI file reads) and can't be imported under host Python (`spin_sdk`'s submodules fail at import time outside the actual componentize-py build/run pipeline). It's covered by manual `spin up --build --runtime-config-file runtime-config.toml` + curl/browser smoke testing instead. `api/auth.py`, `links.py`, `qr.py`, `responses.py`, and `gui-pages/routing.py` have zero `spin_sdk` imports and are fully unit-tested under `uv run pytest` (`api/` also uses an in-memory `FakeStore`, `api/tests/fakes.py`, standing in for the real KV store — `gui-pages` needs no such fake, since `routing.py`'s `build_response` takes a `read_file` callable as a parameter instead of touching the filesystem directly). New pure logic should follow this same pattern: take `store`/`request`/`read_file`-style dependencies as plain parameters, and (in `api/`) use `responses.Request`/`responses.Response` (not `spin_sdk.http`'s) — these are local dataclasses that behave identically at runtime (the real `Handler.handle()` only ever does duck-typed attribute access, never an `isinstance` check) while keeping the module host-importable.

## Time-windowed links

A link record's `start_at`/`end_at` fields (ISO8601 UTC, e.g. `2026-01-01T00:00:00Z`) make it active only in `[start_at, end_at)` — inclusive start, exclusive end. Either or both may be `null` (unbounded on that side). A link outside its window returns a plain `404`, identical to a nonexistent slug — deliberately no distinct "not yet active"/"expired" messaging, so a probing visitor can't learn a link's existence or schedule. The window is re-checked from a fresh KV fetch on every `/r/{slug}` request (`redirect/linkgate.IsWithinWindow`), the same "never cache" principle the password gate already uses — so editing a link's window via `PATCH /api/links/{slug}` takes effect on the very next request.

## Analytics

Every successful redirect updates two keys in the `analytics` KV store, written by `redirect` (`recordAnalytics` in `main.go`, pure logic in `redirect/linkgate/analytics.go`) and read by `GET /api/links/{slug}/analytics` (`api/analytics.py`):

- `count:<slug>` — one JSON blob `{total, days: {"YYYY-MM-DD": n, ...}}`, read-modified-written on every click (one KV round trip), with `days` trimmed to `analytics_day_retention_days` (default 90) entries.
- `events:<slug>:<slot>` — a fixed-shape `"<unix_ms>|<referrer>|<device_class>"` string, blind-overwritten (no read) into one of `analytics_event_slots` (default 30) ring-buffer slots selected by `linkgate.EventSlot(now, numSlots)`.

**Known limitation, confirmed empirically:** the recent-events ring buffer loses far more entries to slot collisions than a uniform-random model would predict — e.g. 8 requests spaced 300ms apart under local `spin up` retained only 3 distinct events. `count.total` stayed exactly accurate in the same test (it's read-modify-write, not a blind overwrite), so only the bounded recent-events log is affected, not the click totals. This points to the WASI clock having deliberately limited resolution in this environment (a documented WASI mitigation against timing side-channels in sandboxed/multi-tenant hosts, not a bug in this code) — several requests can read the literal same raw timestamp, so no slot-selection hash can recover entropy that was never there. `EventSlot` already multiplies by a large odd constant before reducing mod `numSlots` to decorrelate any periodicity in the low-order bits; this is a real improvement but did not eliminate the collisions in this environment. Treat "recent events" as a best-effort sample, never a complete log — this was accepted as a lossy/capped design from the outset, but the loss rate can be considerably higher than "occasional loss under heavy simultaneous bursts" suggests. If a production host's clock has finer resolution, this may not reproduce there.

## Security tradeoffs (accepted for v1)

These are deliberate, disclosed limitations, not oversights — each stems from a real architectural constraint (no outbound network from either component by default, no atomic KV operations) rather than an easy fix that was skipped.

- **No brute-force rate limiting on login or link passwords.** Neither `redirect` nor `api` has any `allowed_outbound_hosts` entries (confirmed: Spin denies all outbound HTTP by default when the key is omitted from a component's manifest), so neither component can reach an external atomic rate limiter (e.g. Redis `INCR`/`EXPIRE`). A KV-based attempt counter would be racy under concurrent requests anyway, since Spin's KV interface has no compare-and-swap. The only real mitigation in place is the PBKDF2 cost factor itself (100,000 iterations) slowing down each individual guess. If this becomes a real requirement, it needs a deliberate `allowed_outbound_hosts` expansion plus an external rate-limiting service — not a quiet KV-counter bolt-on.
- **Slug/link existence and password-protection status are enumerable.** A `404` (no such slug) vs. a `200` password prompt vs. a `302` redirect inherently tells a probing visitor whether a slug exists and whether it's password-protected. This is treated as an accepted characteristic of a public redirect service, not a vulnerability to fix — slugs aren't meant to be secret. Custom slugs are more guessable than random ones; pairing a custom slug with a link password is a sensible pattern worth surfacing in product UX, not something to enforce in code.
- **Security response headers are now set** (CSP, `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`, HSTS) by `redirect`, `api`, and `gui-pages` — see "Security response headers" above for exactly what each component sends. This is no longer a tradeoff entry: `gui-pages`'s CSP dropped `'unsafe-inline'` on 2026-07-31 once every page's inline code was externalized. The only `'unsafe-inline'` left anywhere in the app is `style-src` on `redirect`'s password-prompt page, for one `style="color: red"` attribute — deliberately deferred, since that page has no external stylesheet and adding one means a new file plus a route on the minimal hot path (tracked under `TASKS.md`'s "Future work").
- **Click counts can under-count slightly during concurrent bursts** (documented above) and **the recent-events log can lose more entries than expected** to clock-resolution-driven slot collisions (documented above) — both accepted as the cost of a KV-only, no-network hot path.
- **Reversible link passwords are not supported** — they're hashed one-way, so a link's creator can never have the plaintext redisplayed. If "show me the password I set" UX is ever wanted, that requires a materially different storage model (encryption with real key management), not a tweak to the current hashing approach.

## Deployment: known Akamai Functions blocker

**Akamai Functions (the intended production target) does not support this app's current KV architecture.** Confirmed directly against Akamai's own docs (`techdocs.akamai.com/akamai-functions/docs/use-the-key-value-store`): Akamai Functions only allows the `"default"` key-value store label — it auto-provisions and manages that single store, and no `runtime-config.toml` is used or supported for it at all (that mechanism is a Spin-generic/local-dev/other-host concept, not an Akamai one). This app declares three named stores (`links`, `users`, `analytics`, per `spin.toml`'s `key_value_stores` lists), which is not deployable to Akamai Functions as-is.

**The fix, if/when Akamai deployment becomes a near-term goal:** consolidate `redirect` and `api` to use a single `"default"` store with key-prefixing to keep the three logical namespaces distinct — e.g. `links:slug:<slug>` instead of a separate `links` store's `slug:<slug>`, `users:user:<username>` instead of a separate `users` store's `user:<username>`, `analytics:count:<slug>` instead of a separate `analytics` store's `count:<slug>`. This is a real, moderately invasive change (touches `spin.toml`, `redirect/main.go`'s `kv.Open` calls, `api/app.py`'s `key_value.open` calls, and every key literal across both components' non-test and test code) — deliberately not done as part of this task, since it wasn't otherwise motivated and Akamai deployment isn't an immediate goal. Do this refactor only when actually preparing an Akamai deployment, not preemptively.

**Also unconfirmed:** whether Akamai Functions' single default store has capacity/rate limits that matter at this app's scale — their docs mention "key value query rates are limited to enable experimenting with this feature. Rates can be increased to meet production needs per customer request," but give no concrete numbers. Confirm with Akamai directly before relying on this for production traffic.
