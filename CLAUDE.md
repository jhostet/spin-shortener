# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

`spin-shortener` is a polyglot WebAssembly URL shortener built on [Spin](https://spinframework.dev) (Fermyon's WASI HTTP framework), with three independently-built components composed via `spin.toml`. Shipped functionality: auto-generated and permission-gated custom short links, optional per-link passwords, optional start/end time windows, per-link QR codes (SVG/PNG), click analytics (totals, per-day, a best-effort recent-events sample), local username/password auth with session cookies, and admin user management. See `TASKS.md` for the full phase-by-phase build history and `README.md` for a user-facing overview.

## Architecture

`spin.toml` is the single source of truth for routing and build wiring. It defines four Wasm components across six HTTP triggers (two components — `gui` and `gui-pages` — split the GUI's routes between them):

- `route = "/r/..."` → **`redirect`** component (`redirect/`, Go) — resolves short links and issues redirects; the hot path, hit on every click. Built with `go tool componentize-go build`, compiling `redirect/main.go` (+ `passwordgate.go`, the embedded `prompt.html`, and the pure-logic `redirect/linkgate/` package) to `redirect/main.wasm`. Uses `github.com/spinframework/spin-go-sdk/v3/http` and registers a handler via `spinhttp.Handle`. `allowed_outbound_hosts = []` — no outbound network access, by design (see "Security tradeoffs" below for what this rules out). `key_value_stores = ["default"]` — see "KV store: the single `default` store and the prefixing view" below.
- `route = "/api/..."` → **`api`** component (`api/`, Python) — all authoring/auth/analytics logic: link CRUD, custom slugs, passwords, time windows, QR generation, analytics aggregation, local auth/sessions, and user management. Built with `uv run componentize-py -w spin:up/http-trigger@4.0.0 componentize app -o app.wasm`, compiling `api/app.py` (the WASI entrypoint/router) plus `auth.py`/`links.py`/`qr.py`/`analytics.py`/`users.py`/`responses.py`. Uses `spin_sdk.http.Handler`. `key_value_stores = ["default"]` — see "KV store: the single `default` store and the prefixing view" below.
- `route = "/app.js"`, `route = "/theme.css"`, `route = "/vendor/pico.min.css"`, `route = "/theme-init.js"`, plus one route per page-scoped asset (`/index.js`, `/login.js`, `/dashboard.js`, `/dashboard.css`, `/admin/users.js`, `/admin/users.css`, `/links/detail.js`, `/links/detail.css`) → **`gui`** component — a prebuilt static file server (`spin_static_fs.wasm`, fetched by digest from the `spin-fileserver` GitHub release) serving only these genuinely-static, non-HTML assets. Its `files` mapping still covers all of `gui/` (unchanged from before the route split — narrowing it broke file resolution, see below), but only these 12 exact routes are actually reachable. The 8 page-scoped ones exist because the CSP dropped `'unsafe-inline'`; adding a page's asset without its route serves a fully-rendered page whose script silently 404s. `/theme-init.js` is a different shape from the other 11: it isn't scoped to one page, it's loaded by every page (the light/dark theming bootstrap — see "Theming" below), so it lives in the same "Per-page scripts and styles" comment block in `spin.toml` but isn't actually page-scoped. **Route gotcha, confirmed live:** once this component has more than one trigger route, `spin_static_fs`'s internal path resolution breaks specifically for wildcard (`/...`) routes — a `/vendor/...` wildcard 404'd on every request; the identical file under the exact route `/vendor/pico.min.css` served correctly. Stick to exact routes for this component if any more assets are ever added to it.
- `route = "/..."` (catch-all) → **`gui-pages`** component (`gui-pages/`, Python, same `componentize-py` toolchain as `api`) — serves the GUI's actual HTML pages (`index.html`, `login.html`, `dashboard.html`, `admin/users.html`, `links/detail.html`) via a fixed path→file allowlist (`gui-pages/routing.py`), and attaches the security response headers below to every response. Introduced specifically because `spin_static_fs` has no custom-header capability at all (confirmed: only a `CACHE_CONTROL` env var) — security headers (CSP, `X-Frame-Options`, etc.) are only meaningful on the navigated document itself, not on a `.js`/`.css` subresource, so only the actual HTML pages needed to move off the static-fileserver.

A fifth component exists for local development only and is **deliberately absent from `spin.toml`**:

- `route = "/internal/kv-explorer/..."` → **`kv-explorer`** — [Fermyon's prebuilt KV explorer](https://github.com/fermyon/spin-kv-explorer) (v0.10.0), third-party and unmodified, added by URL + digest exactly like `gui`'s `spin_static_fs.wasm`. It browses raw key-value contents so store data can be verified during development. It lives in `dev/kv-explorer.toml`, which `dev/kv-explorer-up.sh` concatenates onto a copy of `spin.toml` to produce a gitignored `spin-dev.toml` on every local run — so the dev manifest can never drift from the real one, and `spin deploy` has nothing to pick up. **The reviewer's check is `grep -c kv-explorer spin.toml` → `0`**, and `gui-pages/tests/test_manifest_components.py` fails CI if the committed manifest ever gains a fifth component (a `tomllib` set comparison, not a grep — `grep -c '^\[component\.' spin.toml` returns 9, not 4, because sub-tables like `[component.api.build]` match, so a grep guard would never fire).
  - **It has full CRUD over the single `default` store** — every key in the app, `links:`/`users:`/`analytics:` prefixes included. It can overwrite and delete any key with no undo. That is deliberate (repairing bad local test data is half its value), but a stray click destroys local data.
  - **Since `docs/plans/kv-store-consolidation.md` collapsed the three named stores onto Spin's single `"default"` label (required by Akamai Functions, which allows only that one label), there is no longer any store-level separation for this component to respect.** Before that change, `users` was deliberately withheld via the `key_value_stores` list — the explorer takes the store name straight from the request path (`/api/stores/:store`) and hands it to `kv.OpenStore` with no allowlist of its own, so that list was the only thing enforcing it, at the Spin host boundary rather than in the UI. Granting `["default"]` grants everything in one grant, `users:user:*` PBKDF2 hashes and `users:session:*` tokens included — full read and write access to exactly the material that used to be withheld. This is accepted, not overlooked: the user explicitly chose it over inventing a config seam (e.g. a variable toggling one-store vs. three-store mode) to preserve the old separation, on the reasoning that a deployed-vs-developed configuration mismatch is a worse property to own than a dev-only credential exposure in a tool that already has full CRUD over everything else. It is acceptable *only* because this fragment is never part of a deployed manifest — the committed `spin.toml` has four components and no explorer, and `gui-pages/tests/test_manifest_components.py` fails CI if that ever stops being true. Confirmed live: listing keys in the `default` store now returns `users:user:admin`, `users:_meta:usernames`, `users:session:*` and every other prefixed key, where the old three-store split returned `500 access denied` for `users` specifically.
  - `allowed_outbound_hosts = []`, narrowed from upstream's `["redis://*:*", "mysql://*:*", "postgres://*:*"]` (those exist only for externally-backed KV; this app's store is sqlite-backed). Verified sufficient live.
  - Basic auth is required even locally rather than using upstream's `SPIN_APP_KV_SKIP_AUTH=1`: `--env` reaches every component, and unauthenticated CRUD on localhost is reachable from a cross-origin `text/plain` POST, since upstream's `AddKeyHandler` never checks `Content-Type`. Username defaults to `kv`; the password is a required secret variable.
  - **Local KV appears to be non-persistent** — no `.db` file exists in the repo, `~/Library/Caches/spin`, or `~/Library/Application Support/spin`. The explorer therefore only ever shows the current `spin up` session's data; empty stores after a restart are expected, not a bug.

Each component is built independently and only in its own `workdir`; there is no shared build step or root-level package manifest. When editing one component, you generally don't need to touch the others' toolchains.

**Why Go for `redirect` but Python for `api`/`gui-pages`:** the redirect path is the hot path (every short-link click) and is written in Go for raw performance. The `api`/`gui-pages` surfaces (link creation, management, frontend) aren't on that hot path, so they're written in Python to prioritize developer velocity and code understandability over raw speed — the performance tradeoff isn't worth it there. Keep this split in mind when adding new functionality: if it's on the redirect hot path, it likely belongs in the Go component; otherwise default to Python for velocity.

`redirect/main.wasm`, `api/app.wasm`, and `gui-pages/app.wasm` are all build artifacts and are gitignored — they must be rebuilt via `spin up --build` (or the per-component build commands in `spin.toml`) after any source change; they are not checked into the repo.

### KV store: the single `default` store and the prefixing view

Since `docs/plans/kv-store-consolidation.md`, both `redirect` and `api` declare `key_value_stores = ["default"]` — Spin's single auto-provisioned store, required because Akamai Functions (the intended production target) allows only that one label. The app still has three logical namespaces (`links`, `users`, `analytics`); they're kept apart by prefixing every physical key (`links:slug:<slug>`, `users:user:<username>`, `analytics:count:<slug>`, …) rather than by store name.

- **`api/kvprefix.py` is the only Python module that knows prefixes exist.** It exposes `PrefixedStore` (a `get`/`set`/`delete`/`exists` view over one namespace), `open_views(physical_store)` (returns the `{"links", "users", "analytics"}` view dict `api/app.py` binds once per request), and `scoped_list_keys(raw_list_keys)`. `links.py`, `auth.py`, `users.py`, `bulk.py`, `backup.py`, `consistency.py`, `urlpolicy.py` and `analytics.py` are all unaware of it and take a `store` parameter exactly as before.
- **`scoped_list_keys` is a security control, not tidiness.** It filters a raw physical-key enumeration down to one namespace's keys (prefix stripped) before any caller sees them. `backup.py`'s redaction/exclusion guards and `consistency.py`'s key-shape classification are all written against unprefixed keys, so an unfiltered enumeration would silently defeat every one of them — the concrete failure being a full PBKDF2 account hash written into a links-only backup.
- **`PrefixedStore` deliberately has no `get_keys` method.** Omitting it turns "a caller tried to enumerate through a view" into an `AttributeError` instead of a cross-namespace credential leak; enumeration only ever happens through `scoped_list_keys`, which additionally raises `TypeError` if handed the physical store directly instead of a view.
- **`redirect/linkgate/keys.go`** mirrors the Go side: `LinksPrefix`/`AnalyticsPrefix` constants (deliberately no `users:` one — the redirect component has no business constructing a users key) and `LinkKey`/`CountKey`/`EventKey` builders. It must stay byte-identical to `kvprefix.STORE_PREFIXES`, or the API writes links the redirect path can't find with no error anywhere — `api/tests/test_kvprefix.py` reads `keys.go` from the Python test suite and pins the two sides' equality as a CI-time guard rather than a runtime one.
- The `api`/`gui-pages`/redirect suites cover this mechanism directly: `api/tests/test_kvprefix.py` (the view's four-method surface, the non-overlap invariant, the cross-language guard) and `api/tests/test_store_isolation.py` (the four cross-namespace hazards: a links-only backup can't leak a users hash, a links-only restore prunes only `links:` keys, a pre-consolidation backup fixture restores unchanged, and the consistency check never sees `analytics:` keys sharing the same physical store).

## Security response headers

Every response from `redirect`, `api`, and `gui-pages` sets `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, `X-Frame-Options: DENY`, and `Strict-Transport-Security`. Each component's CSP is scoped to what it actually serves:

- `gui-pages` (the real HTML pages): `default-src 'self'` plus `script-src 'self'` and `style-src 'self'` — **no `'unsafe-inline'`.** Every page's script and style live in a sibling `.js`/`.css` file served by the `gui` component (e.g. `dashboard.html` → `dashboard.js` + `dashboard.css`), so no served page contains an inline `<script>`, a `<style>` block, or a `style="…"` attribute for the policy to have to allow. `gui-pages/tests/test_no_inline_code.py` enforces that — a CSP violation fails a page silently in a browser rather than failing a test, so the guard is what keeps the policy true. Hiding is done with the native `hidden` attribute plus `theme.css`'s `[hidden] { display: none !important; }` (the `!important` is load-bearing — Pico sets `display` on `label`, `nav li`, and buttons, all elements this app hides, and the UA stylesheet's `display: none` loses to them). Every other directive is locked down for real: no plugins/objects, no framing, no cross-origin form submission, no base-tag hijacking. `img-src` includes `data:` because Pico CSS renders several UI affordances (sortable-column chevrons, the search icon, the calendar icon) as inline `data:image/svg+xml` background-images — confirmed live that `img-src 'self'` alone produced real CSP-violation console errors blocking them, caught only by loading the actual pages, not by reading the CSS.
- `api` (pure JSON): `default-src 'none'` — nothing it returns should ever be rendered, executed, or framed.
- `redirect`'s password-prompt page (the one HTML `redirect` renders): the strictest policy in the app — `default-src 'none'`, `script-src 'none'` (the page loads and contains no script at all, deliberately: it is the one page where a visitor types a credential, which is worth more than the OS-following theme that loading `theme-init.js` would buy, so it always renders light), and `style-src 'self'`. It links `/vendor/pico.min.css` and `/theme.css` — already exact routes on `gui`, so styling it cost no new files or routes — and uses `theme.css`'s own `.form-error` for its error message, which is what retired its last inline style attribute and with it the last `'unsafe-inline'` anywhere in the app.
  - **`form-action` must list `https:`/`http:`, not just `'self'`.** A correct password answers the POST with a 302 to the link's target, and Chrome applies `form-action` to that redirect, not only to the form's action URL. With a bare `'self'` the browser silently blocked the navigation: the server sent a correct 302 and the visitor just stayed on the prompt, so **every password-protected link was a dead end in a real browser while working perfectly under `curl`**. The two schemes mirror exactly what `api/links.py`'s target-URL validation accepts, so `javascript:`/`data:` form targets stay blocked.

The `gui` component (the static asset routes above) gets none of these — headers on a `.js`/`.css` subresource response don't provide any real protection the navigated-document headers don't already cover.

**`/internal/kv-explorer/...` sits outside every guarantee in this section.** It is a separate third-party component that sets its own headers, so none of the above applies to it, and its UI contains an inline `<script>` and `<style>` block and loads jQuery, Bootstrap, Popper, Font Awesome and Google Fonts from external CDNs — the opposite of the vendored, `default-src 'self'` posture everything else holds. `gui-pages/tests/test_no_inline_code.py` **deliberately does not cover it**: the file is not ours to fix, so a guard over it could only ever fail, and failing on an upstream binary's markup would be noise rather than signal. This is acceptable solely because the route exists only in a local `spin-dev.toml` and is unreachable in any deployed manifest. If that ever changes, this is the paragraph that has to change with it.

### Redirect caching: `Cache-Control: no-store` and the 302-not-301/308 requirement

Every `redirect` response (302s, 404s, and the password prompt alike) now carries `Cache-Control: no-store`, set once in `setSecurityHeaders` (`redirect/main.go`) rather than at the three individual redirect call sites, so it covers `http.NotFound` too — a cached "not yet active" 404 would mean a not-yet-started link never starts working when its window opens.

**302 is a hard requirement for `/r/{slug}`; 301 and 308 are forbidden.** Akamai edge servers do not cache HTTP 302 (`Found`) or 307 (`Temporary Redirect`) responses by default, but they **do** cache 301 (`Moved Permanently`) and 308 (`Permanent Redirect`) by default (`techdocs.akamai.com/property-mgr/docs/cache-http-redirects`). `redirect/main.go`'s three redirect sites all use `http.StatusFound` (302) today — do not "simplify" this to a 301 for a URL that never changes; that single-line change would make Akamai start caching the response at the edge.

**The `no-store` header is defence-in-depth, not the actual control.** Akamai edge servers do **not** honour origin `Cache-Control`/`Expires` by default, so this header protects against browsers and intermediate proxies — the 302 status code is what actually keeps Akamai's own edge from caching the response. A cached redirect would silently break every one of: the `status` check (a disabled link keeps redirecting), the `[start_at, end_at)` window (a link keeps redirecting past `end_at`), deletion (a deleted slug keeps resolving), repointing (a repointed link keeps serving its old destination), and the destination-policy remediation path (a bulk-Disabled violator keeps resolving instead of 404ing).

**Akamai Functions itself publishes no caching documentation at all** (`docs/caching` 404s), but the behaviour is now **CONFIRMED MEASURED against the live deployment** (2026-08-06, `https://e96461a3-8bfe-4dea-83a5-9fed97601f59.fwf.app`, running the post-consolidation build): **a `*.fwf.app` app does inherit the "302 not cached by default" behaviour.** Twenty requests to one active `/r/{slug}` returned twenty `302`s with twenty *distinct* `akamai-grn` values, no `Age`, `X-Cache` or `X-Check-Cacheable` header on any of them, and an origin service time of 126–165 ms on every single request — a cached edge response would have returned in single-digit milliseconds. The click total moved from 45 to exactly 55 across the last ten, which additionally proves `recordAnalytics` ran and **both** its KV writes succeeded on every request, with no losses to the write-RPS cap at that rate. Fifteen further requests to a nonexistent slug confirmed the same for `404`s, which matters independently: a cached "not yet active" 404 would mean a scheduled link never starts working.

**Now measured directly, not inferred (2026-08-06, deployed logging build, `X-SS-Debug` tracing).** Mean over 6 traced redirects: `kv_us≈116.7 ms` of `dur_us≈120.4 ms` — **KV is ~97% of handler time on Akamai**, against 46% locally. The per-operation split is the useful part:

| operation | Akamai | local | ratio |
|---|---|---|---|
| `open` | ~154 µs | 12–33 µs | ~7× |
| `exists` | 21.7 ms | 13–18 µs | ~1,400× |
| `get` | 21.2 ms | 5–9 µs | ~2,900× |
| `set` | 26.1 ms | 5–12 µs | ~3,000× |

**The cost is per *data operation*, not per store handle** — which inverts two things this repo had concluded from local measurement. First, **removing `recordAnalytics`'s second `kv.Open` is not worth doing**: it looked like an 8–20% win locally and is ~0.2% here. Second, **dropping the `Exists` in `lookupLink` is worth ~21.7 ms, ~19% of the redirect's KV time** — that is the optimisation that matters. Third, and new: **`recordAnalytics` is ~73 ms, 63% of the redirect's KV time, and it runs before `http.Redirect`**, so every visitor waits for bookkeeping. See `TASKS.md` Future work for both, including why "just make it async" is not automatically available under WASI.

**The earlier, cruder inference that produced these numbers, retained because it is how the question was first opened:** On the same deployment a `404` (2 KV operations) cost 26–40 ms of origin time while a `302` (7 operations) cost 126–165 ms — about **110 ms for the 5 extra operations, ≈22 ms each**, against 5–17 µs locally. Treat the per-operation figure as an inference rather than a measurement: `x-envoy-upstream-service-time` includes the network hop and Wasm instantiation, and the 302 path also does a JSON parse and the count read-modify-write, so the subtraction controls for fixed overhead but not for those. The direction is not in doubt, though, and it reframes the throughput ceiling below — at ~150 ms of origin time per click, latency and concurrency may bind well before the 50 write RPS cap does. **Use the `X-SS-Debug` header (see "Toggleable structured logging") against a deployed build to get the real per-operation split rather than this subtraction** — that is precisely what it was built for, and no build carrying it has been deployed yet.

## Theming

`gui/` ships a light theme and a custom dark theme (derived from the same navy identity, not Pico's stock dark palette), auto-following the OS by default with a manual override persisted client-side. See `DESIGN.md`'s Colors section for the palette and its measured contrast ratios; this section covers the mechanism.

**Three blocks in `gui/theme.css`, in this order:**

1. **Theme-independent** (`:root:not([data-theme="dark"]), :root[data-theme="dark"]`) — density/type-scale tokens (`--pico-font-size`, `--pico-line-height`, `--pico-spacing`, both `--pico-form-element-spacing-*`) plus the raw navy ramp (`--ss-navy-950`, `--ss-navy-800`). These must never live in a theme-specific block: a theme block's selector stops matching the instant the *other* theme's `data-theme` is set, so a density token placed there would silently hand that property back to Pico's own viewport-scaling defaults the moment the app switched themes.
2. **Light** (`:root:not([data-theme="dark"])`) — unchanged from before this feature, selector included. **This selector must stay exactly `:root:not([data-theme="dark"])`, never a plain `:root`.** Pico's own light block uses that same selector (`:host(:not([data-theme=dark])),:root:not([data-theme=dark]),[data-theme=light]`), so matching it exactly makes this an equal-specificity tie that load order (this file loads after `pico.min.css`) breaks in this file's favor. A plain `:root` would silently lose that tie regardless of load order — confirmed live once already, for a different reason, before this feature existed.
3. **Dark** (`:root[data-theme="dark"]`) — new. `(0,2,0)` specificity beats Pico's own bare `[data-theme=dark]` block (`(0,1,0)`) by specificity, not load order, which is strictly more robust than the light block's tie-plus-load-order arrangement.

**`gui/theme-init.js`** is a small (~90-line) script loaded render-blocking as the first real element of `<head>`, before any stylesheet link, on every page except `index.html` (a redirect stub with no stylesheet, which gets `<meta name="color-scheme" content="light dark">` instead). It reads the `ss-theme` `localStorage` key — one of three values, `"system"` (the default), `"light"`, or `"dark"` — and **always sets `document.documentElement.dataset.theme` to a literal `"light"` or `"dark"`, never `"system"` and never left unset**. Setting that attribute at all, to any value, is what disables Pico's own `@media (prefers-color-scheme: dark)` block entirely (that block is scoped to `:root:not([data-theme])`, which stops matching the instant any `data-theme` is present) — so once `theme-init.js` has run once, the app's own dark block is the only thing driving dark mode, not Pico's. It exposes `window.ssTheme` (`KEY`, `get()`, `set(mode)`, `resolve()`, `apply()`) as the one source of truth for the storage key and the resolution rule; `gui/app.js`'s nav theme control calls these rather than reimplementing them.

**No-JS fallback:** if `theme-init.js` never runs at all (blocked, 404, JS disabled), no `data-theme` attribute is ever set, and the app falls back to whatever `theme.css`'s unconditional light-block declarations render — light, regardless of OS preference. This is why those declarations must never be deleted even though they're no longer the sole defense against Pico's dark-mode leak (see `DESIGN.md`'s Inputs/Fields note for the full history).

**Two Pico dark-block traps**, both must be re-fixed in `theme.css`'s dark block independently of the light block's existing fixes: Pico's own `[data-theme=dark]` block (1) re-enables `--pico-card-box-shadow`/`--pico-dropdown-box-shadow` (both default to a real multi-layer drop shadow, violating the No-Shadow Rule) and (2) sets `--pico-card-border-color` equal to the card background (an invisible border, in a system whose depth comes from a hairline border plus background contrast with no shadow allowed).

**A guard worth knowing about, in `gui/app.js`:** `initHeader()` checks `window.ssTheme` exists before calling into it, because `theme-init.js` is served on its own separate `spin.toml` route (`/theme-init.js`) rather than being bundled into `app.js`. If that route ever 404s, an unguarded call would throw inside `initHeader()`'s async body — a promise no page `.catch()`s — which would silently kill the *entire* page's init chain (nav, identity chip, links table, everything), not just the theme control. The guard turns that failure mode into "the theme control doesn't render and the app stays light" instead of "the whole page is dead."

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

**A `builder` run that involves `spin up --build` may return early**, mid-verification, with the code already written — observed once during the CSP work, where it returned the bare text "Waiting for the spin build to finish." **Resume it rather than re-running the task**: its transcript is intact, so a follow-up message picks up where it stopped, while a fresh invocation redoes work that is already on disk. Worth saying so in the delegation prompt too. Whatever it produces, verify the diff yourself before committing — check that it touched only the files its task line names, and that `git diff --numstat TASKS.md` shows only the checkbox lines it was supposed to tick.

Plans live in `docs/plans/` and are committed. Multi-round work uses `docs/plans/<feature>-scratch.md` (gitignored via `docs/plans/*-scratch.md`) as an append-only handoff note — one `## Round <n> — <agent> — <date>` heading per round, with Done / Open questions / Next. It is a handoff note, not either agent's memory: anything durable gets promoted into the plan file, `TASKS.md`, or here.

## Commands

Build and run the whole app (all four components) locally:

```bash
SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<some-password> spin up --build --runtime-config-file runtime-config.toml
```

This invokes each component's `[component.<name>.build]` command from `spin.toml` and then serves all routes together. Requires the [Spin CLI](https://spinframework.dev) to be installed.

**`--runtime-config-file runtime-config.toml` is now optional locally**, since Spin auto-provisions the `"default"` key-value store with no runtime config at all — it's kept in the documented command anyway so the local backing provider (sqlite-backed `type = "spin"`) stays explicit and every existing script/command that passes it (including `dev/kv-explorer-up.sh`) keeps working unchanged. It has no effect at all on an Akamai Functions deployment, which ignores runtime configuration entirely. `admin_bootstrap_password` is a required secret variable (seeds the first admin user on a fresh KV store) and has no default, so it must be supplied via env var (or another Spin variable provider) on every run.

When testing the `gui` in a real browser over plain `http://localhost`, also set `SPIN_VARIABLE_COOKIE_SECURE=false` — the session cookie's `Secure` flag otherwise stops the browser from storing/sending it over non-HTTPS, breaking login. Leave `cookie_secure` at its default `true` for any HTTPS deployment.

To run the same app **plus the local-only KV explorer** at `/internal/kv-explorer/` (see the `kv-explorer` bullet under Architecture for what it can and cannot reach):

```bash
SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<some-password> \
SPIN_VARIABLE_KV_EXPLORER_PASSWORD=<explorer-password> \
SPIN_VARIABLE_COOKIE_SECURE=false \
  ./dev/kv-explorer-up.sh
```

The script regenerates a gitignored `spin-dev.toml` from `spin.toml` + `dev/kv-explorer.toml` and runs `spin up -f spin-dev.toml`, forwarding any extra arguments. Edit `spin.toml` or `dev/kv-explorer.toml` — never `spin-dev.toml`, which every run overwrites. The explorer's basic-auth username defaults to `kv`.

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

Two tests in `gui-pages/tests/` police things outside that component, because there is nowhere better for them to live and `package main` is not host-testable at all:

- **`test_manifest_components.py`** — asserts the committed `spin.toml` declares exactly `{redirect, api, gui, gui-pages}`, so a dev-only component (today, the KV explorer) can never leak into the deployed manifest, and separately asserts the `dev/kv-explorer.toml` fragment grants `key_value_stores = ["default"]` (the single consolidated store — see "KV store: the single `default` store and the prefixing view") with `allowed_outbound_hosts = []`.
- **`test_no_inline_code.py`** — also covers `redirect/prompt.html`, the only served HTML outside `gui-pages`' `ROUTES`. It deliberately does **not** cover the KV explorer's UI; see "Security response headers".

**`go test ./...` (bare), `go build ./...`, and `go vet ./...` will FAIL** on `package main` with `wit_exports.go:934:6: missing function body` — `main.go`/`passwordgate.go` import `spin-go-sdk`, which only compiles via the special `go tool componentize-go build` toolchain, not plain `go`. This is expected, not a broken build. Only `redirect/linkgate/` (zero `spin-go-sdk` imports) is host-testable — new pure Go logic belongs there, not in `package main`.

`app.py` is intentionally excluded from `pytest` in both `api/` and `gui-pages/` — it's the real WASI entrypoint (routing dispatch + actual `spin_sdk.key_value`/`variables`/`http.Handler` I/O, or in `gui-pages`'s case, real WASI file reads) and can't be imported under host Python (`spin_sdk`'s submodules fail at import time outside the actual componentize-py build/run pipeline). It's covered by manual `spin up --build --runtime-config-file runtime-config.toml` + curl/browser smoke testing instead. `api/auth.py`, `links.py`, `qr.py`, `responses.py`, and `gui-pages/routing.py` have zero `spin_sdk` imports and are fully unit-tested under `uv run pytest` (`api/` also uses an in-memory `FakeStore`, `api/tests/fakes.py`, standing in for the real KV store — `gui-pages` needs no such fake, since `routing.py`'s `build_response` takes a `read_file` callable as a parameter instead of touching the filesystem directly). New pure logic should follow this same pattern: take `store`/`request`/`read_file`-style dependencies as plain parameters, and (in `api/`) use `responses.Request`/`responses.Response` (not `spin_sdk.http`'s) — these are local dataclasses that behave identically at runtime (the real `Handler.handle()` only ever does duck-typed attribute access, never an `isinstance` check) while keeping the module host-importable.

## Time-windowed links

A link record's `start_at`/`end_at` fields (ISO8601 UTC, e.g. `2026-01-01T00:00:00Z`) make it active only in `[start_at, end_at)` — inclusive start, exclusive end. Either or both may be `null` (unbounded on that side). A link outside its window returns a plain `404`, identical to a nonexistent slug — deliberately no distinct "not yet active"/"expired" messaging, so a probing visitor can't learn a link's existence or schedule. The window is re-checked from a fresh KV fetch on every `/r/{slug}` request (`redirect/linkgate.IsWithinWindow`), the same "never cache" principle the password gate already uses — so editing a link's window via `PATCH /api/links/{slug}` takes effect on the very next request.

## Analytics

Every successful redirect updates two keys in the `analytics` KV store, written by `redirect` (`recordAnalytics` in `main.go`, pure logic in `redirect/linkgate/analytics.go`) and read by `GET /api/links/{slug}/analytics` (`api/analytics.py`):

- `count:<slug>` — one JSON blob `{total, days: {"YYYY-MM-DD": n, ...}}`, read-modified-written on every click (one KV round trip), with `days` trimmed to `analytics_day_retention_days` (default 90) entries.
- `events:<slug>:<slot>` — a fixed-shape `"<unix_ms>|<referrer>|<device_class>"` string, blind-overwritten (no read) into one of `analytics_event_slots` (default 30) ring-buffer slots selected by `linkgate.EventSlot(now, numSlots)`.

**Known limitation, confirmed empirically:** the recent-events ring buffer loses far more entries to slot collisions than a uniform-random model would predict — e.g. 8 requests spaced 300ms apart under local `spin up` retained only 3 distinct events. `count.total` stayed exactly accurate in the same test (it's read-modify-write, not a blind overwrite), so only the bounded recent-events log is affected, not the click totals. This points to the WASI clock having deliberately limited resolution in this environment (a documented WASI mitigation against timing side-channels in sandboxed/multi-tenant hosts, not a bug in this code) — several requests can read the literal same raw timestamp, so no slot-selection hash can recover entropy that was never there. `EventSlot` already multiplies by a large odd constant before reducing mod `numSlots` to decorrelate any periodicity in the low-order bits; this is a real improvement but did not eliminate the collisions in this environment. Treat "recent events" as a best-effort sample, never a complete log — this was accepted as a lossy/capped design from the outset, but the loss rate can be considerably higher than "occasional loss under heavy simultaneous bursts" suggests. If a production host's clock has finer resolution, this may not reproduce there.

## Bulk link management

`POST /api/links/bulk` (bulk create) and `POST /api/links/bulk-action` (bulk delete/enable/disable), both in `api/bulk.py` and routed from `api/app.py` on exact-path match above the existing `/api/links/...` branches. Both are **all-or-nothing**: any invalid row/slug in a submission means nothing is written and every problem is reported, never a partial result — with no atomic KV operations available (see "Security tradeoffs" below), a partial-success design would leave the user needing to diff what they submitted against what exists just to know what to retry.

- **`POST /api/links/bulk`** takes raw pasted/uploaded text plus batch-level `password`/`start_at`/`end_at` (applied to every link created in that submission, not per-row) and parses it with `parse_bulk_text`/validates it with `validate_bulk_rows`. Text format, briefly: one row per line, `slug,destination` or `slug<TAB>destination` (first delimiter wins; commas after the first stay in the destination); a bare URL line has no slug (blank = auto-generate); `#` and blank lines are skipped; a leading UTF-8 BOM is stripped; a first row whose fields match a small `HEADER_WORDS` set is dropped as a header. Slugs are case-sensitive. Full spec and worked examples are in `docs/plans/bulk-link-management.md`.
- **`POST /api/links/bulk-action`** takes `{"slugs": [...], "action": "delete"|"enable"|"disable"}`. **Bulk enable/disable is the only path in the GUI that changes a link's `status` at all** — there is still no single-link status toggle anywhere in the dashboard, so disabling one link means selecting it and using the "bulk" action.
- **Caps: `MAX_BULK_ROWS = 50` rows/slugs per request, `MAX_BULK_BODY_BYTES = 262144` (256 KB) per request body.** Both are plain module constants in `api/bulk.py`, not Spin variables — unlike `analytics_event_slots` (which two components must agree on), the cap is read by exactly one function in one component, and it expresses a safety rail tied to what a single `componentize-py` request can do, not an operator-tunable policy. Both error responses carry the limit and (for `too_many_rows`) the actual `row_count`, so the client never hardcodes either number. **Raising `MAX_BULK_ROWS` needs real timing evidence from a full-cap submission, not just "50 felt limiting"** — the cap was deliberately dropped from an original 200 to 50 specifically to keep a submission's per-request work (~100 KV ops, at most one PBKDF2 hash) comfortably bounded; going back up should be a deliberate decision made with that evidence in hand, and if it goes much past ~100 the bulk-create error table's now-removed "first 50, then …and N more" truncation needs to come back too.
- **Write ordering: records first, indexes last, in both directions.** Bulk create writes every `slug:<slug>` record before the single `add_slugs_to_indexes` call; bulk delete removes every `slug:<slug>` record before the single `remove_slugs_from_indexes` call. An interrupted create leaves link records with no index entry — they resolve at `/r/<slug>` and are recoverable, and merely invisible in the dashboard until the index is fixed. An interrupted delete leaves index entries with no backing record, which `handle_list` already tolerates by skipping any slug whose record is `None` — so the only visible effect of a crash mid-delete is nothing at all, not a dangling reference. Reversing either ordering would advertise slugs that 404 (create) or hide links a user believes are gone but that are still live (delete), so this is the one rule for the whole file, matching what the single-item handlers already did.

## Multi-domain display

The app can be reachable at more than one base domain (e.g. a branded `go.example.com` alongside `localhost:3000` in dev), and the GUI lets a viewer pick which one every short URL, Copy button, CSV column and QR code is built from. This is display-only: **there is no per-link `domain` field, and nothing enforces a link to a domain.** `redirect/main.go` never reads the `Host` header — resolution is purely `slug:{slug}` → KV → 302 — so every link already works on every domain that points at this deployment, regardless of which one a link was created or shared under. Domains are a **viewer preference**, in the same category as the light/dark theme: chosen in the persistent nav, persisted client-side in `localStorage` (`ss-domain`), and read fresh at the moment each URL is built (`shortUrlFor(slug)` in `gui/app.js`) rather than stored anywhere server-side.

- **`public_base_urls`** (plural) is a comma-separated Spin variable, e.g. `SPIN_VARIABLE_PUBLIC_BASE_URLS="https://go.example.com,http://localhost:3000"`. Each entry is a bare `scheme://host[:port]` — no path, query or fragment — normalized (lowercased, no trailing slash) and de-duplicated by `api/domains.py`. **Order is meaningful: the first entry is the default** — the nav selector's initial choice for a viewer who hasn't chosen one, and what the QR endpoint uses when no `?base=` is supplied.
- **This variable replaced the old singular `public_base_url`, which no longer exists anywhere in `spin.toml`.** Spin's env-var provider looks up declared variables by name, so a deployment that upgrades without renaming its env var (`SPIN_VARIABLE_PUBLIC_BASE_URL` → `SPIN_VARIABLE_PUBLIC_BASE_URLS`) gets the default `http://localhost:3000` **silently** — the stale variable is simply never read, with no warning anywhere in the server log. Confirmed live. Check this first if a deployed app's QR codes or copied links unexpectedly point at `localhost` after an upgrade.
- **`assigned_domains`**, an optional list on each user record, restricts which of the configured domains that user's nav selector offers. Absent or `[]` means **unrestricted** (every configured domain), not "no domains" — no existing user record needs a backfill. It is a convenience guardrail against handing out an off-brand URL by accident, **not a security control**: it is deliberately not in `auth.py`'s `KNOWN_PERMISSIONS` (a small, fixed, hardcoded vocabulary that would lose its "reject anything outside this set" property if permission strings were generated per configured domain), and it gates nothing server-side — a user can still obtain a link or QR code for any configured domain by hand-crafting a request. The admin Users page manages it via `gui/admin/users.html`/`users.js`.
- **`GET /api/links/{slug}/qr`'s `?base=` parameter is allowlist-validated against `public_base_urls`, never trusted directly.** An endpoint that encoded an arbitrary client-supplied base URL into a QR code would be a QR-poisoning vector — the image is printed and handed out, outliving any later fix. A `base` not in the configured list returns `400 {"error": "invalid_base_url"}`; an empty configured list returns `500 {"error": "no_base_url_configured"}` rather than quietly falling back to `localhost`; a valid, differently-cased or trailing-slashed match is accepted and the response always encodes the server's own canonical string, never the caller's.

Local dev with two domains:

```bash
SPIN_VARIABLE_PUBLIC_BASE_URLS="http://localhost:3000,http://127.0.0.1:3000" \
SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<some-password> \
SPIN_VARIABLE_COOKIE_SECURE=false \
  spin up --build --runtime-config-file runtime-config.toml
```

Plan: `docs/plans/multi-domain-display.md` (also records the rejected per-link `domain` field and `Host`-header enforcement designs, and why).

## Link tags and ownership

Links carry free-form tags, and a `users.manage` holder can reassign a link's owner. Plan: `docs/plans/link-tags-and-ownership.md`.

**Tags live only inside the `slug:<slug>` record, as a `tags` array — there is no `tag:` index and no `_meta:tags` registry.** Vocabulary rules are in `api/tags.py`: lowercase, `^[a-z0-9][a-z0-9_-]*$`, 1–32 chars, **max 10 per link**, stored normalized, de-duplicated and sorted. The character set is deliberately narrower than the slug's (no uppercase) so that `Sale` and `sale` can never become two tags; the 10-per-link cap exists because tags render as chips inside the links table's existing Short-link cell, and a row with dozens of them would wreck the column.

**Why no index.** The dashboard already holds every link in `allLinks` (`GET /api/links` has no pagination), so filtering and autocomplete are pure client-side work over data already in memory. A `tag:<tag>` index would have bought nothing for that and cost a two-index read-modify-write on every single-link `PATCH` — up to 20 of them at the 10-tag cap, with no compare-and-swap available anywhere in Spin's KV. It would also have created a new KV key type, which obliges a matching `api/backup.py` change (`INDEX_KEYS`, `restore_write_order`). **If either an index or a registry is ever added, that `backup.py` change is mandatory, not optional** — a new key type that `backup.py` doesn't know about is silently dropped on restore. **Since 2026-08-04 there is a second obligation:** `api/consistency.py` must learn the new key's shape too, or the consistency check reports it as `unrecognized_key` on every run (see "KV consistency check"). A test in `api/tests/test_backup.py` pins that today's tags round-trip needs no such change.

**The cost of that choice:** tag suggestions are ownership-scoped. A user without `links.view_all` is only ever offered tags they have personally used, because the suggestion list is derived from the links they can see. Filtering still works correctly on everything they can see.

**`links.tag` gates bulk tag/untag only.** Setting tags on a single link — at create, or via `PATCH /api/links/{slug}` — needs only the edit rights that link already required. The permission exists for `POST /api/links/bulk-action` with `action` of `tag` or `untag`, which still applies the per-row `can_edit` check on top. `PATCH {"tags": [...]}` is a **full replacement**, not a merge; omitting the key entirely leaves existing tags untouched.

**Owner reassignment is `action: "reassign"` on the same bulk endpoint, gated on `users.manage` alone.** It deliberately **skips the per-row `can_edit` check** every other bulk action applies. That is the point of the feature: the motivating case is an employee leaving, whose links the operator by definition cannot edit. It is not a weaker bar than an `admin` role check either — `api/users.py`'s `handle_update` already lets a `users.manage` holder promote themselves to admin.

- **The permission check runs before the owner lookup, and must stay that way.** Reversed, a caller without `users.manage` gets `400 unknown_owner` for a name that doesn't exist and `403 forbidden` for one that does — enumerating the username list that `GET /api/users` gates behind that same permission. `test_bulk_action_reassign_without_permission_cannot_distinguish_a_real_owner_from_a_fake_one` is the guard.
- **Write ordering: records first, then the new owner's index, then the old owners' — and `all_links` is never read or written.** A reassignment doesn't change which links exist. An interruption mid-move therefore leaves a slug listed under *both* owners: visible, harmless, and self-correcting, because `links.move_slugs_between_owners` is idempotent. The reverse ordering would make the link vanish from both dashboards while still resolving at `/r/<slug>` — the failure nobody notices. A test asserts `all_links` is byte-identical across a reassignment.
- A **disabled** user is an acceptable reassignment target (parking links on a deactivated account is a legitimate move); a nonexistent one is not.

**`redirect` is untouched.** `linkgate.Link` has no `Tags` field and gains none — Go's `encoding/json` ignores unknown fields unless a decoder opts into `DisallowUnknownFields`, so the new field costs the hot path nothing. `redirect/linkgate/link_test.go` pins both halves of that.

**Bulk tag, untag and reassign share the 50-slug `MAX_BULK_ROWS` cap** with the existing actions, and are all-or-nothing like them: if adding a tag would push any one link past 10, nothing is written and every offending slug is reported. The dashboard disables all six bulk buttons past the cap and tells the user to narrow the filter, since a tag filter is the easy way to select 200 links at once.

## User deletion and link ownership

**`DELETE /api/users/{username}` refuses with `409 {"error": "user_owns_links", "username", "link_count"}` while the user still owns links**, writing nothing to either store. The operator disposes of the links first — reassign or delete, via the bulk actions above — and then deletes the account. Plan: `docs/plans/user-deletion-link-ownership.md`.

**This exists because deleting a user used to leave two things behind, both of which were reproduced against a running app before the fix.**

1. **Link inheritance.** The link records kept `owner: "<deleted-username>"` and `owner_links:<deleted-username>` survived in the `links` store. **There is no user identity anywhere beyond the username string** — `links.can_edit` is `record["owner"] == principal.username` and `handle_list` reads `owner_links:<principal.username>` — so recreating the username handed the new account every link the old one owned, editable and repointable, while the short URL kept resolving for everyone who already had it.
2. **Session revival, the worse one.** `auth.resolve_session` builds the `Principal` from the **current** `user:` record and keys only on the username stored inside the session, and deletion never purged `session:*`. A deleted user's cookie correctly 401'd *while the username was absent* — and then came back to life the moment the name was recreated, carrying **the new account's role and permissions**. A plain user's revoked cookie was observed returning as an admin's, without its holder ever learning the new password. Deleting a user was never actually revoking their sessions; it was making them temporarily unresolvable.

Both scenarios are pinned by `api/tests/test_user_deletion.py` rather than merely fixed, and both are mutation-verified: removing the 409 gate fails the first, removing the session purge fails the second.

**There is deliberately no tombstone or reserved-username list.** Purging sessions at deletion is what makes username reuse *safe*, which is a better property than making it forbidden — a reserved list would grow forever, would need its own admin UI to ever release a name, and would still not have fixed the session bug. `auth.delete_sessions_for_user(store, username, list_keys)` takes the key-listing callable as a parameter, the same way `backup.py` does, so `auth.py` stays free of `spin_sdk` imports and host-testable.

**Cross-store write ordering on the success path** (deletion now touches both the `links` and `users` stores, with no transaction and no compare-and-swap): `owner_links:` → sessions → `user:` → `_meta:usernames`. Every interruption point leaves the stronger invariant. In particular the reverse order is wrong twice over: it could leave a live session for a nonexistent user, and it could leave the user record deleted with a dangling `owner_links:` key that **a retry can no longer reach**, because `handle_delete` would then 404 on the missing user before getting to the cleanup.

**`links.owned_slugs` is public** (it lost its underscore when this landed) because `users.py` reads it to make the 409 decision — the same "shared, not module-private" convention `can_view`/`can_edit` already carry.

**The GUI turns the refusal into an action.** `gui/admin/users.html` names the username and count, and offers a link to `dashboard.html?owner=<username>` **only when the viewer holds admin/`links.view_all`/`links.edit_all`** — an operator with `users.manage` alone would land on an empty dashboard, so they get the missing permission named instead of a dead link. That is accepted rather than fixed: such an operator can already self-promote through `handle_update`.

- The dashboard's `#owner-filter` is **derived from the `owner` field on the loaded records, not from `owner_links:`**, so it is ownership-scoped for free and additionally surfaces links whose owner no longer exists. **That makes it the repair path** for any deployment that orphaned links before this gate existed; those owners render with a `— deleted account` marker reusing the existing `status-disabled` treatment (no new token).
- `?owner=` is **consumed once**. A reload after a bulk action keeps whatever filter the operator has since chosen instead of snapping back to the URL.

## Destination URL policy

Admin-managed rules on what a link may point at, enforced at authoring time. Pure logic in `api/urlpolicy.py`; the admin page is `gui/admin/url-policy.html`, reached by an in-body anchor on `admin/users.html` (the nav is full). Plan: `docs/plans/destination-url-policy.md`.

**Why it exists:** a short link *launders* its destination. A recipient sees `go.example.com/r/promo` and cannot tell where it leads, so a bad destination borrows the organization's credibility. Before this, `links.is_valid_target_url` accepted any `http`/`https` URL with a netloc, and any account could shorten anything.

**Rules live in one `_meta:url_policy` key in the `links` store.** An absent key means everything is allowed, so no deployment changes behaviour on upgrade and nothing needs a migration. **No rules are seeded** — a list shipped in the repo needs a deploy to change and carries false-positive risk, the same reasoning that retired the banned-word slug list.

**Precedence is one sentence: a deny rule always wins, then an allow rule or a `default_action` of `allow`, otherwise blocked.** This was chosen over "most specific match wins" for one reason — **a misunderstood specificity rule fails open, and deny-wins fails closed.** Do not "improve" it into specificity ordering.

**Matching covers a host and every subdomain of it**, via `host == rule or host.endswith("." + rule)`. The dot is load-bearing: a bare `endswith("example.com")` would also match `notexample.com` and silently mis-scope every rule. Verified live in both directions — under a deny rule for `example.com`, `evil.example.com` and `a.b.example.com` are blocked while `notexample.com` and `myexample.com` are not; and under an allow-list with `default_action: "deny"`, the classic bypasses `example.com.attacker.net` and `user@example.com.evil.net` are both blocked (the host comes from `urlparse(...).hostname`, so userinfo can't spoof it).

**Enforced at all three authoring paths — `links.handle_create`, `links.handle_update` and `bulk.handle_bulk_create`.** A policy enforced in two of three places is not enforced, and `api/tests/test_url_policy_enforcement.py` exists to prove all three reject the same destination and write nothing. **`validate_bulk_rows` takes the policy as a required fourth parameter with no default** — deliberately, because a default is exactly how the bulk path would stay silently open. Rejections carry `host` and `reason` so the GUI can name what caught the link rather than saying "not allowed".

**Existing links that violate a newly-added rule are reported, never mutated.** `GET /api/admin/url-policy/violations` lists them; remediation is the operator's existing bulk Disable/Delete. Nothing is retroactively protected until a human acts — that is the accepted trade, taken so that a config edit can never become an unpreviewable bulk mutation with no compare-and-swap and no undo. It is the same posture restore and the consistency check already hold. **Violations are deliberately *not* a thirteenth consistency check:** `consistency.py` is scoped to structural drift, a policy finding would pin `ok: false` on a structurally flawless store, and its "re-run to confirm" and "never repairs" framings are both wrong for policy.

**`redirect` is untouched.** Enforcement is at authoring time; the hot path keeps its single KV read per click and never consults the policy. A link created before a rule existed keeps resolving.

**`_meta:url_policy` carries both obligations a new KV key type now imposes** (see "KV consistency check"): `consistency.py` recognises its shape, and `backup.py` round-trips it — verified byte-identically, because otherwise restoring a backup would silently wipe an operator's policy.

## KV consistency check

`GET /api/admin/consistency` walks the `links` and `users` stores and reports where the indexes have drifted from the records they describe. **It reports; it never repairs** — there is no `?fix=`, no repair endpoint, and the walk performs no writes at all. Gated on `users.manage`. Pure logic in `api/consistency.py` (zero WASI SDK imports, `store` objects and the `list_keys` callable passed in, `api/backup.py` as the model); the GUI is a third article on `gui/admin/backup.html`. Plan: `docs/plans/kv-consistency-check.md`.

**Twelve checks**, always all present in every report at `count: 0` when clean, in this order. Warnings first in each group below only for readability — the report's own order is this one:

| id | severity | means |
|---|---|---|
| `unindexed_link` | warning | a `slug:` record missing from `all_links` — resolves, but invisible in the dashboard |
| `missing_link_record` | info | `all_links` names a slug with no record — `handle_list` already skips it |
| `unindexed_owner_link` | warning | a record whose owner's `owner_links:` doesn't list it |
| `owner_index_mismatch` | warning | listed under one owner, record names another |
| `orphan_owner_index_entry` | info | an `owner_links:` entry with no backing record |
| `unknown_link_owner` | warning | a record whose `owner` has no user record |
| `dangling_owner_index` | warning | an `owner_links:<U>` for a `U` that isn't a known user |
| `unindexed_user` | warning | a `user:` record missing from `_meta:usernames` — can sign in, invisible to administration |
| `missing_user_record` | info | `_meta:usernames` names a user with no record |
| `orphan_session` | warning | a `session:` naming a user with no record |
| `unreadable_value` | warning | a value that wouldn't parse into its expected shape |
| `unrecognized_key` | info | a key matching none of the known shapes for its store |

**`unindexed_owner_link` is the one this endpoint was built for.** `users.handle_delete`'s `409` gate reads `links.owned_slugs` — the `owner_links:<username>` index, one KV read, deliberately not an O(all links) walk. So a record whose `owner` is carol but which has drifted out of `owner_links:carol` **does not block carol's deletion** and is orphaned by the very flow built to prevent orphans (it then shows up as `unknown_link_owner`). `api/tests/test_consistency_scenarios.py` pins both halves: the check fires, and `handle_delete` still returns 200.

**The `analytics` store is never opened or scanned.** `links.handle_delete` never removes analytics keys, so `count:<slug>`/`events:<slug>:<slot>` for a deleted slug is **normal state, not drift** — reporting it would make every healthy deployment show findings, which is how a checker gets ignored. Revisit only if deletion is ever changed to purge analytics. **Expired `session:` records and empty `owner_links:` keys are excluded for the same reason** (that second exclusion also answers the question `docs/plans/user-deletion-link-ownership.md` deferred about deleting emptied index keys at the source: empty keys are never reported, so they are not noise, and its stated trigger is not met).

**A report can never carry credential material.** A `user:` record's *value* is never read — only its key name; checks 6-9 need nothing else, and check 10 reads only the `username` field of a `session:` value. A test generates a report over a store holding a real PBKDF2 hash and asserts neither `password_hash` nor `pbkdf2_sha256` appears in the body.

**`MAX_FINDINGS_PER_CHECK = 100`, per check, not global** — one noisy check would otherwise starve every other one out of the report. It is a plain module constant, not a Spin variable, on the same reasoning as `MAX_BULK_ROWS`: one function in one component reads it. **When a check truncates, `count` stays exact and `truncated` is set**, and the GUI says "Showing the first N of M" — a capped list must never read as complete.

**The walk has no snapshot.** Spin KV offers no transaction, so a concurrent write can produce a transient finding. That is why the page tells the operator to re-run: a real finding appears on both runs. Do not "fix" this with locking; there is nothing to lock with.

**A new KV key type now obliges *three* changes, not two** (see also "Link tags and ownership"): `backup.py`'s `INDEX_KEYS`/`restore_write_order`, `consistency.py`'s key-shape recognition — otherwise the new key reports itself as `unrecognized_key` on every single run — **and, since the KV store consolidation, the key must live under one of the three prefixes (`links:`, `users:`, `analytics:`) in `api/kvprefix.py`'s `STORE_PREFIXES`, or it is invisible to the whole application**: no view will ever return it, so it won't appear in a backup, won't be seen by the consistency check, and will be silently pruned by the next restore.

## KV backup and restore

`GET /api/admin/backup` downloads a JSON snapshot of the `links`, `users` and `analytics` stores; `POST /api/admin/restore` replaces them from one. Both live in `api/backup.py` (pure logic, zero `spin_sdk` imports) and are routed on exact paths from `api/app.py`. The GUI is `gui/admin/backup.html` + `backup.js`. Plan: `docs/plans/kv-backup-restore.md`.

**File format.** `{"format": "spin-shortener-kv-backup", "schema_version": 1, "created_at", "created_by", "fidelity", "counts", "stores": {...}}`. Keys are plaintext, **every value is base64** — uniformly, regardless of whether it happens to be JSON, so the format never has to care what a value contains. `?stores=links,users` exports a subset, allowlist-validated against `BACKUP_STORES` exactly the way `qr.handle_qr` validates `?base=` (unknown name → `400 unknown_store` naming `allowed_stores`; empty → `400 no_stores`). `fidelity` is `"full"`: the `get_keys` spike confirmed Spin's KV can enumerate keys, so the planned index-walk fallback (and its `"index-walk"`/`incomplete: true` labelling) was never needed and is not implemented.

**A backup carries no *account* credentials — but it does carry link password hashes. It is a sensitive file.** Three exclusions are applied at export: `redact_user_value` strips `password_hash` from every `user:` record, and `is_excluded_key` drops every `session:*` key and `_meta:bootstrapped`. So the file cannot be replayed into a session and cannot leak an *account* hash. **`validate_backup` independently refuses a file containing any of that material on the way back in** — a second guard on the same property, not a redundant one, since it means a hand-edited file can't inject a session or an account hash either. Both halves are mutation-tested: disabling redaction fails 5 tests, disabling exclusion fails 6, and both additionally fail a *restore* test.

**What is deliberately *not* stripped: a link's own `password_hash`, inside its `slug:` record in the `links` store.** `redact_user_value` is scoped to `user:` keys and `is_excluded_key` returns `False` for every store but `users`, so a password-protected link exports with its full PBKDF2 hash. That is the right call — stripping it would make restore silently *unprotect* every protected link, a security downgrade invisible to the operator — but it means **the file must be treated as sensitive**, not as the freely-shareable artifact the account-hash exclusion alone would suggest. Link passwords are the weaker population, too: the GUI enforces only `minlength="4"`, so a short link password behind PBKDF2-100k is realistically crackable offline by anyone holding the file. Store backups somewhere access-controlled.

**Do not verify this property with `grep password_hash <file>`.** Every value in the document is base64, so that grep cannot see inside a record — it only ever matches the plaintext `excluded` array, which lists `"users/user:*#password_hash"` as documentation and therefore always returns a nonzero count. A grep of the raw file both misses real embedded credential material and reports a false positive; decode the values and inspect them (the `docs/plans/kv-backup-restore.md` verification step that specified this grep was wrong on both counts, and this is how the link-password exposure went unnoticed).

**Restore is all-or-nothing and it replaces, it does not merge.** `validate_backup` runs to completion before a single write. Then each store is written and pruned: any pre-existing key absent from the file is deleted. Store order is `links` → `analytics` → `users` (`RESTORE_STORE_ORDER`) — **users last, deliberately**, so a mid-restore failure leaves the operator's own session intact to retry with. Within each store, **records are written before indexes** (`restore_write_order`, using `INDEX_KEYS` plus the `owner_links:` prefix) — the same rule the bulk handlers follow, for the same reason: an interruption leaves records with no index entry, which resolve at `/r/<slug>` and are merely invisible in the dashboard, rather than index entries advertising slugs that 404.

**Recovery walkthrough, confirmed live end to end.** Restore a backup → the response carries `signed_out: true` and every session is gone, including the caller's. **No `spin up` restart is needed**: `ensure_bootstrap_admin` runs on *every request* (`api/app.py:60`), and since restore deleted `_meta:bootstrapped`, the next request re-seeds the bootstrap admin from `admin_bootstrap_password`. Sign in as that admin and the restored links resolve immediately. **The disclosed corner: that re-seed overwrites a restored user record named `admin`** — same username, but a fresh record with `role: "admin"`, `permissions: []` and `assigned_domains: []`, so any custom permissions or domain assignments that account had are lost. Every *other* restored account survives intact but **has no usable `password_hash` and can therefore never authenticate** — `LocalAuthProvider.authenticate` returns a clean `401`, not a `500`. An admin must set each one a new password; the users table flags them (see DESIGN.md's Status Badges).

**Caps: `MAX_BACKUP_BODY_BYTES = 5_242_880` (5 MiB) and `MAX_BACKUP_ENTRIES = 5_000`,** plain module constants in `api/backup.py`. Export refuses with `500 backup_too_large` naming both the cap and the actual size; restore refuses with `413 body_too_large`. **Raising either needs real timing evidence from a full-cap restore, not a hunch** — the same rule `MAX_BULK_ROWS` carries above, and for the same reason: a restore is a single `componentize-py` request doing thousands of sequential KV writes with no batching available.

**Both endpoints gate on the `users.manage` permission**, returning `users.py`'s exact `_forbidden()` body. That is deliberately the same bar as user administration rather than a `role == "admin"` check, and it is not the weaker of the two: a `users.manage` holder can already create an admin account, or promote themselves, via `handle_update`. Gating backup on the role instead would add no real protection while inventing a second, inconsistent notion of "admin" in a codebase whose whole permission model is the granular one.

**Restore requires `{"confirm": "REPLACE"}` in the body** (`400 confirmation_required` otherwise), and the GUI additionally requires the literal string typed into a field *plus* a count-bearing confirmation dialog. Server-side confirmation is the one that matters — the endpoint is reachable by `curl`.

## Toggleable structured logging

The app has KV-timing instrumentation in `redirect` (Go) and `api` (Python) only — `gui-pages` and `gui` are untouched (`gui-pages` does no KV work at all; `gui` is a prebuilt third-party binary). Plan: `docs/plans/toggleable-logging.md`.

**Two Spin variables, both read once and cached for the lifetime of the Wasm instance** (`sync.Once` in Go, a module-level cache in Python) — sound because a Spin variable cannot change without a redeploy on Akamai (`spin aka` has no command to change a deployed app's variables) or a restart locally, both of which produce a fresh instance:

- **`log_level`** — `"off"` (default) or `"summary"`; any other value is treated as `"off"`, fail-closed. **This must stay `off` in production.** At ~130 bytes/line and a sustained ~25 redirects/second (the Akamai write-RPS ceiling documented above), baseline logging is on the order of 280 MB/day into a 7-day `spin aka logs` retention window.
- **`log_debug_token`** — a shared secret (default `""`), compared against a request's `X-SS-Debug` header. A match traces that one request and adds a `Server-Timing` response header **regardless of `log_level`** — this is what lets one request be traced with no redeploy, since the variable alone would mean one redeploy to turn logging on and a second to turn it off. **An empty configured token never matches anything**, including an empty or absent header — checked explicitly before any comparison (`crypto/subtle.ConstantTimeCompare` in Go, `hmac.compare_digest` in Python), not as an incidental property of the comparison. Getting this backwards makes the default configuration "anyone can enable tracing." **The token cannot be rotated without a redeploy either** — there is no runtime-variable-update path on Akamai.

**Output: one logfmt line per request to stderr**, prefixed `ss ` so it's greppable and distinguishable from Spin's own output, e.g.:

```
ss comp=redirect route=/r/{slug} slug=M7RyJVC status=302 dur_us=174 kv_ops=7 kv_us=80 kv_bytes=262 open=2/35 exists=1/17 get=2/11 set=2/17 slow=open:-:20
```

Per-op-type fields are `count/total_µs`, one per non-empty operation type (`open`/`exists`/`get`/`set`/`delete`/`list_keys`) — a zero-count type is omitted entirely, never `=0/0`. `slow` names the single slowest operation as `type:namespace:µs`. `api` additionally logs `method` and, on the exception path, `err=1` alongside `status=500`. **`Server-Timing` durations are milliseconds as floats** — 80 µs renders as `0.080`, never `80` — and are only emitted for a valid token, never merely because `log_level=summary`, so a baseline-logging deployment never hands internal timing to every visitor.

**The collector structurally cannot log a KV key.** Its record method takes an operation type, a namespace and a duration (plus a byte count) — it has no parameter that could accept a key, the same move `PrefixedStore` makes by having no `get_keys`. This matters because `users:session:<token>` is a live session credential and `spin aka logs` retains 7 days by default; a key-logging design would put working session tokens in a week-long retention window. `redirect` does log the raw **slug** (not the key), deliberately — correlating a slow resolution to a specific link is the entire point of instrumenting that path, and slugs are already treated as non-secret (see "Security tradeoffs" below). `api` logs only a route **template** (e.g. `/api/users/{username}`, `/api/links/{slug}/analytics`) — the actual username or slug embedded in the URL never reaches the log line.

**`redirect`'s off path is deliberately byte-identical to the uninstrumented component**, and its op profile is deliberately unchanged at **7 KV operations per successful redirect** (2 `open`, 1 `exists`, 2 `get`, 2 `set`) — the second `kv.Open` inside `recordAnalytics` was kept rather than threaded from the handler's already-open store, even though removing it would be a real 8–20% win, because doing so here would change the very baseline this instrument exists to measure against. When no collector is attached (the off path), KV calls go straight to the real `*kv.Store` with no wrapper and no timer calls at all — never a wrapped-but-no-op collector — which is what keeps the off path cheap, not merely silent. The collector itself is **never a package-level variable** in either language: it is attached to the Go request's `context` (retrieved by a `collectorFrom(ctx)` helper that returns `nil` — every `Collector` method is nil-safe) or passed as an explicit `_dispatch(self, request, collector)` parameter in Python, precisely because `componentize_py_async_support.spawn` dispatches each request independently and a shared collector would silently interleave concurrent requests' operations into one another's line — confirmed live: a 20-way concurrent burst against one slug produced exactly 20 log lines, every one reporting `kv_ops=7`.

**A new pure Python module for this lives at `api/obs.py` — never `logging.py`, and the same rule applies to any future module named after a stdlib module (`time.py`, `json.py`, ...).** `componentize-py` compiles `app.py` alongside its siblings with the component's own directory on the import path, so a module shadowing a stdlib name would break every stdlib module that imports the real one, for the whole component.

## Security tradeoffs (accepted for v1)

These are deliberate, disclosed limitations, not oversights — each stems from a real architectural constraint (no outbound network from either component by default, no atomic KV operations) rather than an easy fix that was skipped.

- **No brute-force rate limiting on login or link passwords.** Neither `redirect` nor `api` has any `allowed_outbound_hosts` entries (confirmed: Spin denies all outbound HTTP by default when the key is omitted from a component's manifest), so neither component can reach an external atomic rate limiter (e.g. Redis `INCR`/`EXPIRE`). A KV-based attempt counter would be racy under concurrent requests anyway, since Spin's KV interface has no compare-and-swap. The only real mitigation in place is the PBKDF2 cost factor itself (100,000 iterations) slowing down each individual guess. If this becomes a real requirement, it needs a deliberate `allowed_outbound_hosts` expansion plus an external rate-limiting service — not a quiet KV-counter bolt-on.
- **Slug/link existence and password-protection status are enumerable.** A `404` (no such slug) vs. a `200` password prompt vs. a `302` redirect inherently tells a probing visitor whether a slug exists and whether it's password-protected. This is treated as an accepted characteristic of a public redirect service, not a vulnerability to fix — slugs aren't meant to be secret. Custom slugs are more guessable than random ones; pairing a custom slug with a link password is a sensible pattern worth surfacing in product UX, not something to enforce in code.
- **Security response headers are now set** (CSP, `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`, HSTS) by `redirect`, `api`, and `gui-pages` — see "Security response headers" above for exactly what each component sends. This is no longer a tradeoff entry at all: `gui-pages`'s CSP dropped `'unsafe-inline'` on 2026-07-31, and `redirect`'s password prompt followed on 2026-08-01. **There is no `'unsafe-inline'` anywhere in the application.** The prompt page turned out to be far cheaper than the deferral assumed — it needed no new file and no new route, since `/vendor/pico.min.css` and `/theme.css` were already routed and `theme.css` already had the `.form-error` class DESIGN.md required it to use.
- **Click counts can under-count slightly during concurrent bursts** (documented above) and **the recent-events log can lose more entries than expected** to clock-resolution-driven slot collisions (documented above) — both accepted as the cost of a KV-only, no-network hot path.
- **Reversible link passwords are not supported** — they're hashed one-way, so a link's creator can never have the plaintext redisplayed. If "show me the password I set" UX is ever wanted, that requires a materially different storage model (encryption with real key management), not a tweak to the current hashing approach.

## Deployment: Akamai Functions

The former blocker is resolved: `docs/plans/kv-store-consolidation.md` moved both `redirect` and `api` onto Spin's single auto-provisioned `"default"` KV store (see "KV store: the single `default` store and the prefixing view" above), which is the one thing Akamai Functions requires that the three-named-store design couldn't provide. **That consolidation would have silently handed the KV explorer the `users` keys, and it did — deliberately, on the user's decision.** See the `kv-explorer` bullet under Architecture for the accepted-exposure record; nothing further to do here.

**Confirmed quotas** (`techdocs.akamai.com/akamai-functions/docs/quotas-and-limits`, fetched 2026-08-04):

| Quota | Default limit |
|---|---|
| Memory (RAM per execution) | 128 MiB |
| **App size** | **50 MiB** |
| **Request handler duration** | **30 seconds** |
| Request/response size | 10 MiB |
| KV storage (all stores) | 2 GB |
| **KV read requests per app** | **1,000 RPS** |
| **KV write requests per app** | **50 RPS** |
| Max value size | 1 MB |
| Max key size | 8 KB |

The same page: SQLite storage, Redis triggers, wasi-blobstore, wasi-messaging and custom triggers are not supported, and **runtime configuration is unavailable** — `runtime-config.toml` has no deployment role at all there (it's read only by the local Spin CLI). Whether the 50 MiB app-size limit is compressed and/or per-component, and whether the RPS defaults above apply to any given account, are **unconfirmed** — raise both with Akamai directly before relying on them for production traffic.

**Deploy commands** (`techdocs.akamai.com/akamai-functions/docs/quickstart`, `docs/deploy-app-variables`):

```bash
spin plugin install aka
spin aka login
spin build
spin aka deploy \
  --variable admin_bootstrap_password=<pw> \
  --variable cookie_secure=true \
  --variable public_base_urls=https://<app-id>.fwf.app
```

Three variables must be set on every deploy: `admin_bootstrap_password` (required secret, no default — seeds the first admin), `cookie_secure=true` (the local-dev `false` override must NOT ship — see "Commands" above), and `public_base_urls` pointing at the real deployed app URL (see "Multi-domain display" above for the silent-fallback-to-`localhost` trap if this is ever renamed or misconfigured on upgrade). Whether a `secret = true` variable like `admin_bootstrap_password` can be supplied through `--variable` is unconfirmed until a deploy is actually attempted.

**Upgrading an existing deployment is a backup → deploy → restore, never in-place**, because the KV store consolidation changed the physical key space (unprefixed `slug:<slug>` under three named stores → prefixed `links:slug:<slug>` under `default`) — a build from before the consolidation and one from after are reading disjoint physical keys. The backup file format needs no conversion for this: it already speaks logical store names and unprefixed keys, and `kvprefix`'s views map them onto the new physical keys transparently on the way in (`api/tests/test_store_isolation.py`'s pre-consolidation-fixture test pins exactly this). Concretely: `GET /api/admin/backup` on the old build, deploy the new build, `POST /api/admin/restore` with `{"confirm": "REPLACE", ...}` against it.

**Two operating ceilings this deploy surfaces, both a direct consequence of the 50 write RPS cap:**

- **Sustained redirect throughput ceilings around 25/second.** A recorded click performs two writes (`analytics:count:<slug>` and one `analytics:events:<slug>:<slot>` slot), so 50 write RPS / 2 writes per click ≈ 25 redirects/second before writes are throttled. Reads (3 per click: `Exists` + `Get` in `lookupLink`, plus the analytics read-modify-write's read) are nowhere near the 1,000 RPS read cap.
- **A full-cap 5,000-entry restore (`MAX_BACKUP_ENTRIES`) needs roughly 100 seconds of writes at 50 write RPS and cannot complete inside Akamai's 30-second handler duration**, even though the same restore measured 84 ms locally. Export is read-bound and safer (5,000 reads at 1,000 RPS ≈ 5 s), but a store with many analytics event keys enumerates and reads more than the entry cap actually restored, since that cap is applied only at restore time. Bulk create at its own 50-row cap (~100 KV ops) is fine. Chunked/resumable backup-restore is listed under `TASKS.md`'s Future work if real data ever exceeds what one request can move.
