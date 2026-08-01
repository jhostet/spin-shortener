# Local-Only KV Explorer

## Context

There is currently no way to see what is actually in the `links`, `users`, and
`analytics` KV stores during local development. The only readers are the app's
own code paths: `api/links.py`'s handlers, `api/analytics.py`, and
`redirect/main.go`. When a link record looks wrong, or an analytics `count:<slug>`
blob does not match what the dashboard shows, the only debugging tool is adding
a `print()` to a component that has to be rebuilt to Wasm before it runs. Bad
local test data cannot be repaired at all — it can only be worked around by
restarting (the local stores appear to be non-persistent; see the facts below)
and re-seeding by hand through the GUI.

Fermyon publishes a prebuilt component for exactly this — `spin-kv-explorer` —
which mounts a browser UI and a REST API over any KV stores the manifest grants
it. It is added by URL + digest, the same mechanism this repo already uses for
the `gui` component's `spin_static_fs.wasm`, so it costs no build step and no
new toolchain.

There is no `TASKS.md` Future-work entry for this; the request came from the
user directly. The relevant existing context is `CLAUDE.md`'s "Deployment: known
Akamai Functions blocker" (which this change has to stay compatible with) and
the `gui` component's URL+digest source in `spin.toml`, which is the precedent
being followed.

**Confirmed decisions** (settled by the user before planning — not reopened
here):

1. **Local development only.** It must not be reachable in any deployed
   configuration. Enforcing that, given Spin has no conditional components, is
   the central technical problem this plan solves.
2. **Stores: `links` and `analytics` only. `users` is withheld.** That store
   holds PBKDF2 password hashes and live session tokens; read access alone
   enables impersonation and write access means forging a session or replacing a
   hash. The admin UI already exposes user records in a safe form.
3. **Its full CRUD is accepted and must be documented plainly.** This route can
   delete and rewrite records. Being able to fix bad local test data is part of
   its value.
4. **Its CDN dependencies and inline code are accepted, scoped, and
   documented** — not forked, not vendored, not restyled.

## Key technical facts confirmed during research

- **The published artifact's digest matches the upstream snippet.**
  `curl -sL https://github.com/fermyon/spin-kv-explorer/releases/download/v0.10.0/spin-kv-explorer.wasm | shasum -a 256`
  → `65bc286f8315746d1beecd2430e178f539fa487ebf6520099daae09a35dbce1d`, identical
  to the digest in `readme.md`'s snippet. Size: 546,527 bytes. Spin caches it
  after the first run (`~/Library/Caches/spin/registry`), so this is a one-time
  download, but the **first** run of the dev script needs network access.

- **It is a core Wasm module (`wasi_snapshot_preview1`), not a component.**
  First eight bytes are `0061 736d 0100 0000` (core module version 1; a
  component would be `0d00 0100`). `strings` over the binary shows
  `__imported_wasi_snapshot_preview1_*` and
  `github.com/fermyon/spin/sdk/go/v2/kv.OpenStore` — it is a Spin 2.x-era
  TinyGo build from January 2024. **This repo already runs a core module of the
  same vintage**: `spin_static_fs.wasm` v0.3.0 is also `0061 736d 0100 0000` and
  serves `/theme.css` today under Spin 4.0.2 (`spin --version` →
  `spin 4.0.2 (bfc7543 2026-06-23)`). That is strong evidence Spin 4.0.2 still
  adapts these, but it is not proof for the KV imports specifically —
  **UNCONFIRMED until the component is actually run**; the first runnable task's
  done-when is exactly that.

- **The store name is taken from the URL path and passed straight to
  `kv.OpenStore`.** From `explorer/main.go`:

  ```go
  func ListKeysHandler(w http.ResponseWriter, _ *http.Request, p spinhttp.Params) {
      storeName := p.ByName("store")
      store, err := kv.OpenStore(storeName)
      if err != nil {
          http.Error(w, err.Error(), http.StatusInternalServerError)
  ```

  There is no server-side list of permitted stores and no dropdown — the UI has
  a free-text "Store Label" input (`index.html:216`) and a Load button. **The
  manifest's `key_value_stores` list is the only thing that denies the `users`
  store**, and it denies it at the host boundary, not in the UI. That is the
  right place for it, but it means the denial is only as good as the fragment
  being correct — hence the CI guard test below.

- **The UI page itself is served without authentication; only the API is behind
  HTTP Basic.** In `serve()`, the four `/api/...` routes are wrapped in
  `BasicAuth(...)` while `router.GET(spinRoute, UIHandler)` is not, with the
  upstream comment "We want to allow users to access the UI without basic auth
  in order to set the credentials." So an unauthenticated `GET
  /internal/kv-explorer/` returns 200 HTML. No data is exposed by that page, but
  it is another reason this route must not exist in a deployed manifest.

- **`SPIN_APP_KV_SKIP_AUTH=1` bypasses Basic auth entirely** (`ShouldSkipAuth()`
  → `os.LookupEnv("SPIN_APP_KV_SKIP_AUTH")`), and `spin up`'s only way to set it
  is `--env`, which `spin up --help` documents as "Pass an environment variable
  (key=value) to **all components** of the application". Not used — see
  "Credentials" below.

- **The component makes no outbound network requests.** Its entire import
  surface, per the source, is `spinhttp`, `kv`, and `variables`; there is no
  `spinhttp.Send`, no redis/mysql/postgres client. Upstream's
  `allowed_outbound_hosts = ["redis://*:*", "mysql://*:*", "postgres://*:*"]`
  exists for deployments where a KV label is backed by an external database.
  This app's stores are `type = "spin"` in `runtime-config.toml`. The narrowest
  value is therefore `[]`, matching `redirect`. **UNCONFIRMED** that Spin 4.0.2
  does not gate the sqlite-backed KV path on the guest's outbound allowlist
  (nothing in the docs suggests it does, and `redirect` already uses `[]` with
  two KV stores — which is the strongest available evidence, since `redirect`
  reads and writes `links` and `analytics` with no outbound hosts at all). A
  task verifies it live.

  **If `[]` turns out not to work, the required action is to stop and report —
  not to fall back to upstream's `redis://*:*` / `mysql://*:*` /
  `postgres://*:*`.** That value would make this the single most
  outbound-permissive component in an application where `redirect` runs `[]`
  deliberately and `api`/`gui-pages` omit the key entirely (Spin denies all
  outbound HTTP when it is absent). Widening it is a conscious security
  decision for the user to take, with the evidence of the actual failure in
  hand, not something a builder lands silently to get a dev tool working.

- **Spin has no conditional components, no manifest include/import, and no
  environment profiles.** Confirmed against
  [Writing Spin Applications](https://spinframework.dev/v3/writing-apps): the
  manifest statically declares all components; the only composition features are
  component-level `dependencies` and runtime configuration. `spin up --profile`
  is a *build* profile, not a component selector. `spin up -c/--component-id`
  can run a subset, but the docs mark it "[Experimental] … it may change even
  between minor versions", and it is opt-out, not opt-in.

- **Concatenating a fragment onto `spin.toml` produces valid TOML with exactly
  the intended data model** — verified by running Python's `tomllib` over the
  real `spin.toml` plus the fragment below. Two things this pins down: a
  fragment must not contain a bare `[variables]` header (TOML forbids reopening
  a table), and the sub-table form `[variables.kv_explorer_password]` with
  `required = true` / `secret = true` deserializes to the identical dict as the
  inline `{ required = true, secret = true }` form used by
  `admin_bootstrap_password`. The composed document yields components
  `{api, gui, gui-pages, kv-explorer, redirect}` and appends
  `/internal/kv-explorer/...` to the trigger list. Whether Spin's own Rust
  parser accepts the sub-table form is **UNCONFIRMED** but near-certain (same
  data model); the failure would be loud (`spin up` refuses to start) and is
  covered by the first runnable task.

- **Route precedence is by longest matching prefix.** The Spin HTTP trigger docs
  state: "If multiple triggers could potentially handle the same request based
  on their defined routes, the trigger whose route has the longest matching
  prefix takes precedence", and "exact matches take precedence over wildcard
  matches". `/internal/kv-explorer/...` therefore wins over `gui-pages`'s `/...`
  regardless of declaration order — the same rule the 12 exact `gui` routes
  already rely on live (`spin.toml:51-56` says so explicitly).

- **The `spin_static_fs` wildcard-404 gotcha does not apply here.** That is a
  bug in that binary's internal path resolution. This component computes its own
  base from request headers — `getBasePath()` reads `Spin-Base-Path` and
  `Spin-Component-Route` and joins them. The Spin docs note "Earlier versions of
  Spin supported an application-wide base path; this is removed in Spin 3", so
  `Spin-Base-Path` is empty and `path.Join("", "/internal/kv-explorer")` still
  yields the correct root. A wildcard route is *required* here (the API lives at
  sub-paths).

- **The UI is 425 lines with 1 srcless `<script>`, 1 `<style>` block, and
  subresources from four external origins**: `cdn.jsdelivr.net` (Bootstrap 4.3.1
  CSS), `cdnjs.cloudflare.com` (Font Awesome 6.3.0, jQuery 3.4.1, Bootstrap
  4.5.3 JS, Popper 2.11.6), `fonts.googleapis.com` (preconnect + Chivo Mono),
  `fonts.gstatic.com` (preconnect). Plus three anchor links to
  `developer.fermyon.com`. Counted from the release's embedded
  `explorer/index.html`. The three CDN scripts carry SRI hashes; the jsdelivr
  Bootstrap CSS and the Google Fonts stylesheet do not.

- **The local KV stores are almost certainly non-persistent, and in any case
  are not reachable from a second process.** `runtime-config.toml` declares
  `type = "spin"` with no `path` for all three stores, and **no `.db` file
  exists anywhere** — not under the repo, not in `~/Library/Caches/spin`, and
  not in `~/Library/Application Support/spin` (checked while verifying this
  plan). `.spin/` contains only `logs/`. Spin's docs only document the
  `path`-provided case, so the precise mechanism is **UNCONFIRMED**, but two
  consequences follow regardless of what it is:

  1. **The explorer must run inside the same application**, because whatever
     `type = "spin"` does with no `path`, a separate Spin process cannot be
     relied on to reach it. This is what decides the "separate app on a second
     port" alternative below.
  2. **Expect the explorer to show only the data written during the current
     `spin up` session.** Restart the app and the stores are very likely empty
     again — the admin user is re-seeded from `admin_bootstrap_password` on
     every fresh store, which is exactly what has been masking this. "The
     explorer is empty after a restart" is expected behavior, not a bug in the
     explorer, and should not be filed as one. The end-to-end verification task
     records it explicitly so the first person to hit it recognises it.

  If this ever becomes annoying, the fix is a persistent local backend
  (`path = ".spin/kv.db"` in `runtime-config.toml`) — a separate decision with
  its own consequences, listed under follow-ups.

- **Its keys and values are base64 in API responses.** `GetResult.Value` is
  `[]byte`, which Go's JSON encoder emits as base64; path params must be
  standard base64 of the raw key with `/` replaced by `-` (`DecodeSafeKey` in
  `main.go`). The browser UI does this for you; `curl` users need to know.

- **The UI reports a denied store badly.** Its click handler only special-cases
  `response.status == 401`; a 500 (which is what a non-permitted store produces)
  falls through to `response.json()` on a plain-text body, which rejects with no
  `.catch()`. Observable behavior of asking for `users`: an empty table and a
  console error, not a message. `curl` is the reliable way to verify the denial.

## Recommendation: a generated dev manifest

**`spin.toml` never mentions the explorer. A committed fragment plus a small
script generate a gitignored `spin-dev.toml` on every local run.**

Three files:

| File | Committed? | Role |
|---|---|---|
| `dev/kv-explorer.toml` | yes | The manifest fragment: one trigger, one component, two variables. Never read by Spin on its own. |
| `dev/kv-explorer-up.sh` | yes | Regenerates `spin-dev.toml` from `spin.toml` + the fragment, then `exec`s `spin up -f spin-dev.toml`. |
| `spin-dev.toml` | **no** (gitignored) | Generated on every run. Overwritten, never edited. |

Why this shape:

- **No drift.** The dev manifest is a byte-for-byte copy of `spin.toml` plus an
  append, regenerated on every single run. Editing a route or a component in
  `spin.toml` is picked up automatically. This is the failure the
  "commit a second full manifest" option cannot avoid.
- **`spin-dev.toml` must sit at the repo root.** Build `workdir`s (`redirect`,
  `api`, `gui-pages`), `files` sources (`gui`), and the default state directory
  (`spin up --help`: "For local apps, this defaults to `.spin/` relative to the
  `spin.toml` file") are all resolved relative to the manifest. Root placement
  means every relative path — and the local KV data — is identical to a normal
  run.
- **The reviewer's at-a-glance check is one command:** `grep -c kv-explorer
  spin.toml` → `0`. The committed manifest — the one `spin build`, `spin up`,
  and `spin deploy` all read by default — has four components and no explorer.
  Any manifest that exposes the explorer is a file that is not in git.
- **The convention is enforced by a test, not by memory.**
  `gui-pages/tests/test_manifest_components.py` asserts `spin.toml`'s component
  set is exactly `{redirect, api, gui, gui-pages}`. Pasting the fragment into
  `spin.toml` — the single most likely way this goes wrong, since it is what
  upstream's own `spin add -t kv-explorer` does — fails CI. It runs in the
  existing `gui-pages (Python)` Jenkins stage with no `Jenkinsfile` change.

  **The guard must parse the manifest with `tomllib` and compare the key set of
  `manifest["component"]`. Do not reach for grep.** The obvious shortcut,
  `grep -c '^\[component\.' spin.toml`, returns **9**, not 4 — it also matches
  `[component.redirect.variables]`, `[component.redirect.build]`,
  `[component.api.variables]`, `[component.api.build]`, and
  `[component.gui-pages.build]`. A count-based guard written that way would
  either never fire or fire on unrelated edits; a set comparison also produces a
  legible failure (`{'api', 'gui', 'gui-pages', 'kv-explorer', 'redirect'} !=
  {...}`) that names the offending component.
- **Failure mode when someone ignores the convention.** To deploy the explorer
  you would have to (a) paste the fragment into `spin.toml`, which fails CI, or
  (b) run `spin deploy -f spin-dev.toml` against a file that is gitignored and
  only exists on a machine where `dev/kv-explorer-up.sh` has already run. Neither
  is something you do by forgetting; both are things you do on purpose. That is
  the difference between this and "keep it in `spin.toml` and remember" —
  the latter fails *by* forgetting.

### `dev/kv-explorer.toml`

Exact intended content (the comments are load-bearing — constraints 2, 3, and 4
say these facts must be documented where someone will see them):

```toml
# ---------------------------------------------------------------------------
# LOCAL DEVELOPMENT ONLY. Appended to a copy of spin.toml by
# dev/kv-explorer-up.sh to produce the gitignored spin-dev.toml. Spin never
# reads this file on its own, and nothing in the committed spin.toml refers
# to it: the deployed manifest has four components and no explorer.
# See docs/plans/kv-explorer.md.
#
# Fermyon's prebuilt KV explorer (github.com/fermyon/spin-kv-explorer v0.10.0),
# third-party and unmodified, added by URL + digest exactly like the gui
# component's spin_static_fs.wasm.
#
# THIS COMPONENT HAS FULL CRUD over every store listed below: it can read,
# overwrite and delete any key, with no undo. That is deliberate — repairing
# bad local test data is half its value — but a stray click destroys local
# data.
#
# `users` is deliberately NOT listed: it holds PBKDF2 password hashes and live
# session tokens, so read access alone enables impersonation and write access
# forges a session or replaces a hash. The store name comes from the request
# path (/api/stores/:store) and is handed straight to kv.OpenStore, so this
# list is the only thing that denies it — and it denies it at the host, not in
# the UI.
#
# Its UI loads jQuery, Bootstrap, Popper, Font Awesome and Google Fonts from
# cdnjs/jsdelivr/Google, and contains inline script and style. Accepted and
# scoped: it is a separate component, so none of this app's security response
# headers or CSP apply to it, and it is not reachable outside a local run.
# ---------------------------------------------------------------------------

[[trigger.http]]
route = "/internal/kv-explorer/..."
component = "kv-explorer"

[component.kv-explorer]
source = { url = "https://github.com/fermyon/spin-kv-explorer/releases/download/v0.10.0/spin-kv-explorer.wasm", digest = "sha256:65bc286f8315746d1beecd2430e178f539fa487ebf6520099daae09a35dbce1d" }
# Narrowed from upstream's ["redis://*:*", "mysql://*:*", "postgres://*:*"],
# which exists only for KV labels backed by an external database. This app's
# stores are sqlite-backed (type = "spin", runtime-config.toml) and the
# component's source makes no outbound request of any kind, so it needs
# nothing — the same [] the redirect component uses.
allowed_outbound_hosts = []
key_value_stores = ["links", "analytics"]

[component.kv-explorer.variables]
kv_credentials = "{{ kv_explorer_user }}:{{ kv_explorer_password }}"

# Sub-table form, not a second `[variables]` header: this file is concatenated
# onto a spin.toml that already opens [variables], and TOML forbids reopening a
# table. `[variables.x]` after `[variables]` is legal and deserializes
# identically (verified with tomllib against the real spin.toml).
[variables.kv_explorer_user]
default = "kv"

[variables.kv_explorer_password]
required = true
secret = true
```

### `dev/kv-explorer-up.sh`

```bash
#!/usr/bin/env bash
# Runs the whole app locally WITH Fermyon's KV explorer at
# /internal/kv-explorer/ — see docs/plans/kv-explorer.md.
#
# spin.toml never mentions the explorer. This regenerates a throwaway,
# gitignored spin-dev.toml from spin.toml + dev/kv-explorer.toml on every run,
# so the dev manifest cannot drift from the real one. Edit spin.toml or
# dev/kv-explorer.toml — never spin-dev.toml.
#
# `set -u` is deliberately omitted: macOS's system bash 3.2 treats "$@" with no
# positional parameters as an unbound variable. The :? guards below cover the
# variables that actually matter.
set -eo pipefail

cd "$(dirname "$0")/.."

: "${SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD:?must be set (seeds the first admin user)}"
: "${SPIN_VARIABLE_KV_EXPLORER_PASSWORD:?must be set (KV explorer basic-auth password; username defaults to 'kv')}"

{
  echo "# GENERATED FILE — DO NOT COMMIT, DO NOT DEPLOY."
  echo "# spin.toml + dev/kv-explorer.toml, rebuilt by dev/kv-explorer-up.sh."
  echo "# Every local run overwrites this file; edit the two sources instead."
  echo
  cat spin.toml
  echo
  cat dev/kv-explorer.toml
} > spin-dev.toml

exec spin up -f spin-dev.toml --build --runtime-config-file runtime-config.toml "$@"
```

Invocation, alongside the two env vars the documented run command already
needs:

```bash
SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> \
SPIN_VARIABLE_KV_EXPLORER_PASSWORD=<kvpw> \
SPIN_VARIABLE_COOKIE_SECURE=false \
  ./dev/kv-explorer-up.sh
```

Extra `spin up` flags pass through (`"$@"`), e.g. `--listen 127.0.0.1:3001`.
`spin watch -f spin-dev.toml` also works if you want rebuild-on-change
(`spin watch --help` confirms `-f`), but it will not regenerate `spin-dev.toml`
when `spin.toml` changes — rerun the script for that.

`.gitignore` gains one line: `spin-dev.toml`.

## Credentials: real Basic auth, not `SPIN_APP_KV_SKIP_AUTH`

**Require real local credentials.** `kv_explorer_user` defaults to `kv`;
`kv_explorer_password` is `{ required = true, secret = true }` — deliberately
mirroring the existing `admin_bootstrap_username` / `admin_bootstrap_password`
pair in `spin.toml`, so the dev manifest's variable block looks like the rest of
the app rather than like a bolt-on. Cost to the developer: one more env var in
one command.

Why not `SPIN_APP_KV_SKIP_AUTH=1`:

- `spin up --env` sets the variable **on every component in the application**,
  not just the explorer. Harmless today (nothing else reads that name) but it is
  a blunt instrument, and `[component.kv-explorer.environment]` in the fragment
  would be a quieter way to do the same wrong thing.
- Unauthenticated CRUD on `127.0.0.1:3000` is reachable by anything else on the
  developer's machine, including a page in the browser they already have open. A
  cross-origin `fetch` with `Content-Type: text/plain` is a CORS "simple
  request" — no preflight — and `AddKeyHandler` never checks `Content-Type`
  before `json.NewDecoder(r.Body).Decode(&input)`. That is a narrow but real
  drive-by write path onto local link records. Basic auth answers it with a 401.
- The required variable also makes the credential a startup requirement rather
  than something you notice when the UI silently lets you in. Because the
  variables exist only in the fragment, `required = true` costs the production
  manifest nothing.

The one thing to know: **the UI page is not behind Basic auth** (see the facts
above), so the browser flow is "load the page, click Load, then the browser
prompts for `kv` / your password when the first API call 401s". That is upstream
behavior, not a misconfiguration.

## Route

Keep upstream's `/internal/kv-explorer/...`.

- No collision: `/r/...`, `/api/...`, and the 12 exact `gui` routes all fail to
  match `/internal/...`, and the longest-matching-prefix rule puts it ahead of
  `gui-pages`'s `/...` catch-all.
- In the committed manifest, `/internal/kv-explorer/` is just another unknown
  path: `gui-pages/routing.py`'s `resolve_file` returns `None` and
  `build_response` produces a 404 with the app's full security headers. Nothing
  about production changes.
- Use the **trailing slash**. The component registers its UI handler on exactly
  `/internal/kv-explorer/`; the no-slash form is left to the embedded router's
  trailing-slash redirect. UNCONFIRMED whether that yields a 301 or a 404 under
  Spin 4.0.2 — irrelevant if the documented URL has the slash.

## Store access

`key_value_stores = ["links", "analytics"]`. No `users`, permanently.

Enforcement is at the Spin host: the explorer calls `kv.OpenStore("users")`,
the host denies a label the component was not granted, `OpenStore` returns an
error, and the handler answers `500` with the error text (`main.go`,
`ListKeysHandler`). Nothing in the explorer's own code or UI is involved, which
is exactly why this is trustworthy — but also why the fragment's store list is
worth a test.

What a reviewer should expect to see in each store, locally:

- `links` — `slug:<slug>` records (`api/links.py` / `redirect/linkgate.ParseLink`).
- `analytics` — `count:<slug>` blobs and `events:<slug>:<slot>` strings
  (`CLAUDE.md`, "Analytics").

## Security posture: what this route is not covered by

To be written into `CLAUDE.md` (see the documentation task), not just here:

- **None of this app's security response headers apply.** `redirect`, `api`, and
  `gui-pages` each set their own headers in their own code; the explorer is a
  separate third-party component and sets none. No CSP, no
  `X-Content-Type-Options`, no `X-Frame-Options`, no HSTS on
  `/internal/kv-explorer/*`.
- **A CSP could not be added without breaking it**, since its page is built from
  five external origins plus inline script and style. Not attempted — non-goal.
- **`gui-pages/tests/test_no_inline_code.py` deliberately does not cover it.**
  That test derives its page list from `routing.py`'s `ROUTES` and globs
  `gui/**/*.js`; the explorer's HTML is embedded inside a prebuilt `.wasm` that
  is not in this repo and is not served by `gui-pages`. Its inline code is
  therefore out of that guard's scope by construction, not by omission — and the
  guard exists to protect a CSP that this route does not have.
- **Full CRUD, no undo, no audit trail.** Deleting `slug:<x>` from `links` in
  the explorer deletes the link.

## Akamai interaction (one line that must not be missed)

`CLAUDE.md`'s Akamai section prescribes consolidating the three named stores
into a single `"default"` store with key prefixes. **That refactor silently
breaks decision 2**: with one store, `key_value_stores = ["default"]` grants the
explorer the `users:` keys too, and no manifest-level mechanism can restrict a
component to a key prefix. The documentation task must add a sentence to that
section saying so, with the resolution being a decision at that time (drop the
explorer, or consciously accept `users` exposure in a local-only tool), not a
config tweak.

## Documentation changes

**`CLAUDE.md`** (builder task, not done by this plan):

1. *Architecture* — a fifth bullet for `kv-explorer`: prebuilt third-party
   component added by URL + digest, `/internal/kv-explorer/...`, **present only
   in the generated `spin-dev.toml`, never in `spin.toml`**, run via
   `dev/kv-explorer-up.sh`, full CRUD over `links` and `analytics`, no `users`
   and why.
2. *Security response headers* — a paragraph stating the explorer sits outside
   every guarantee in that section (no headers, no CSP), and that
   `gui-pages/tests/test_no_inline_code.py` deliberately does not cover it.
3. *Commands* — the dev invocation with all three env vars.
4. *Tests* — mention `gui-pages/tests/test_manifest_components.py` as the guard
   that keeps dev-only components out of the committed manifest.
5. *Deployment: known Akamai Functions blocker* — the store-consolidation
   sentence above.

**`PRODUCT.md`: no change.** It describes the product's capabilities, personas,
and operating context. The explorer is developer tooling that no persona can
reach in any deployed configuration and that ships in no artifact — recording it
under "Capabilities and Constraints" would misrepresent the product surface. If
that judgement is ever revisited, the right home is one clause in *Operating
Context*, not a capability bullet.

**`DESIGN.md`: no change.** The Impeccable tooling scans `gui/`; the explorer's
Bootstrap UI is neither in `gui/` nor first-party, so there is nothing to exempt.

**`README.md`: no change.** Its Quick start covers running the product and
already defers to `CLAUDE.md` for "the full command reference".

## Trade-offs and rejected alternatives

**Keep the explorer in the committed `spin.toml`, relying on the required
credential variables plus deployment discipline.** By far the cheapest: no
script, no fragment, no generated file, and the `required = true` variables mean
a deploy that forgets them fails to start. Rejected because it directly
contradicts constraint 1 — "reachable in production unless someone remembers" is
not local-only. It also makes the two credential variables mandatory for every
production run of an app that otherwise has no need of them, and puts an
unauthenticated HTML page and an authenticated full-CRUD API on the public
origin, with the only protection being a password that now has to be managed
like a real secret.

**Commit a second full manifest (`spin-dev.toml`) and run it with `spin up
-f`.** Simpler than a generator — no script, no generated file, and the reviewer
check is just as easy. Rejected on drift: `spin.toml` currently has 16 triggers
across four components, and the recent CSP work added eight routes to it in a
single change. A duplicate would have silently missed all eight, and the failure
mode is the worst kind — the dev manifest keeps working while diverging from the
thing that actually ships, so local testing stops testing production's routing.

**Run the explorer as its own Spin app on a second port, pointed at the same
state directory.** Structurally the strongest answer to constraint 1: the
explorer never appears in any file that `spin deploy` could read, and there is
nothing to generate. Rejected on evidence: this repo's local stores are declared
`type = "spin"` with no `path`, and no `.db` file exists anywhere — repo,
`~/Library/Caches/spin`, or `~/Library/Application Support/spin` — so there is
no file for a second process to open and it would almost certainly see an empty,
unrelated store; and even if a file did appear, two Spin processes holding the
same sqlite store is an untested arrangement whose failure mode is confusing
rather than obvious. Worth revisiting
only if the local KV backend is ever moved to an explicit `path = ".spin/kv.db"`
file, at which point this becomes clearly better than a generated manifest.

**Keep it in `spin.toml` and exclude it at run time with `spin up -c` /
`--component-id`.** Attractive because it needs no new files at all. Rejected
three times over: the docs mark it "[Experimental] … may change even between
minor versions"; it is opt-out (production runs must list all four other
components, and forgetting exposes the explorer, which is the exact failure mode
constraint 1 rules out); and it does not remove the component from the deployed
application, only from the running subset. Whether `spin deploy` honours it at
all is undocumented.

**`SPIN_APP_KV_SKIP_AUTH=1` instead of real local credentials.** One fewer env
var, no `kv_credentials` variable, no secret to invent. Rejected: `--env` applies
to every component; unauthenticated CRUD on localhost is reachable by a
cross-origin simple-request POST from any page the developer has open (the
handler never checks `Content-Type`); and the required-variable pattern already
exists in this manifest for `admin_bootstrap_password`, so matching it is
cheaper than explaining an exception.

**Do nothing.** Live: the app works, and the data can be inspected indirectly
through `GET /api/links` and the analytics endpoints. Rejected because those
endpoints show the *interpreted* record, not the stored bytes — the failure modes
worth debugging (a malformed `count:<slug>` blob, an orphaned
`events:<slug>:<slot>` entry, a record written by an older schema) are exactly
the ones the API's own parsing hides, and none of them can be repaired without
one.

**Fork, vendor, restyle, or proxy the explorer behind this app's session auth.**
Not evaluated — an explicit non-goal (constraint 4 and the stated non-goals). The
instinct to wrap it in `api`'s `check_csrf` / `users.manage` permission is
precisely what "local development only" makes unnecessary.

## Tasks

Appended verbatim to `TASKS.md` under a new `## KV explorer` heading. `TASKS.md`
is authoritative for checkbox state.

```
- [ ] Add the local-only KV explorer manifest fragment — file(s): dev/kv-explorer.toml (new), .gitignore — done when: dev/kv-explorer.toml declares the kv-explorer trigger and component exactly as in docs/plans/kv-explorer.md (URL + digest sha256:65bc286f…, allowed_outbound_hosts = [], key_value_stores = ["links", "analytics"], kv_credentials variable, [variables.kv_explorer_user]/[variables.kv_explorer_password] sub-tables) with the CRUD/users/CDN comment banner intact; .gitignore lists spin-dev.toml; `python3 -c "import tomllib,pathlib;d=tomllib.loads(pathlib.Path('spin.toml').read_text()+pathlib.Path('dev/kv-explorer.toml').read_text());print(sorted(d['component']))"` prints all five component names; `git diff --exit-code spin.toml` still passes.
- [ ] Add dev/kv-explorer-up.sh, the generated-dev-manifest runner (depends on the fragment task) — file(s): dev/kv-explorer-up.sh (new) — done when: the script is executable, exits with a clear message if SPIN_VARIABLE_KV_EXPLORER_PASSWORD or SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD is unset, otherwise regenerates spin-dev.toml (with the DO-NOT-COMMIT banner) from spin.toml + dev/kv-explorer.toml and execs `spin up -f spin-dev.toml --build --runtime-config-file runtime-config.toml "$@"`; a real run builds all four existing components, serves GET /login.html as 200, and serves GET http://127.0.0.1:3000/internal/kv-explorer/ as 200 HTML; `git status --porcelain` shows spin-dev.toml as ignored, not untracked.
- [ ] Verify the narrowed allowed_outbound_hosts and the links/analytics-only store scope — file(s): dev/kv-explorer.toml — done when: with the app running from the script and at least one link created through the GUI, `curl -u kv:<pw> http://127.0.0.1:3000/internal/kv-explorer/api/stores/links` returns 200 with a keys array containing a real `slug:<slug>` key, the same call for `analytics` returns 200, `curl -u kv:<pw> .../api/stores/users` returns a non-200 with no key data, and an unauthenticated `.../api/stores/links` returns 401. If any store call fails with an outbound-networking error, STOP AND REPORT — do not fall back to upstream's redis/mysql/postgres wildcards, which would make this the most outbound-permissive component in an app where redirect runs [] deliberately; widening it is the user's decision, taken with the actual error in hand.
- [ ] Add the CI guard against dev-only components in the committed manifest — file(s): gui-pages/tests/test_manifest_components.py (new) — done when: `cd gui-pages && uv run pytest` passes with two new tests — one asserting the key set of tomllib-parsed spin.toml's `component` table is exactly {redirect, api, gui, gui-pages} (a set comparison on parsed TOML, NOT a grep: `grep -c '^\[component\.' spin.toml` returns 9, since it also matches the .build/.variables sub-tables), one asserting spin.toml + dev/kv-explorer.toml parses and yields kv-explorer with key_value_stores == ["links", "analytics"], "users" absent, allowed_outbound_hosts == [], and a trigger route of "/internal/kv-explorer/..." — and temporarily pasting the fragment into spin.toml makes the first test fail.
- [ ] Document the KV explorer in CLAUDE.md — file(s): CLAUDE.md — done when: the Architecture section has a kv-explorer bullet stating it is third-party, prebuilt by URL+digest, present only in the generated spin-dev.toml and never in spin.toml, has full CRUD over links and analytics, and deliberately has no access to users and why; the Security response headers section states the route is outside every header/CSP guarantee and that gui-pages/tests/test_no_inline_code.py deliberately does not cover it and why; the Commands section shows the dev/kv-explorer-up.sh invocation with all three env vars; the Tests section names gui-pages/tests/test_manifest_components.py; and the Akamai section notes that consolidating to a single "default" store would grant the explorer the users keys, which must be decided then, not silently accepted.
- [ ] End-to-end manual verification of the KV explorer — file(s): (none — verification step) — done when: `SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_KV_EXPLORER_PASSWORD=<kvpw> SPIN_VARIABLE_COOKIE_SECURE=false ./dev/kv-explorer-up.sh` runs; logging in and creating a link still works; http://127.0.0.1:3000/internal/kv-explorer/ loads, prompts for kv/<kvpw> on first Load, lists the new slug:<slug> key under store label `links` and count:<slug> under `analytics`, editing a value through the UI is visible on the next GET /api/links, and store label `users` returns no keys; restarting the script and reloading the explorer shows the stores empty again, confirming the documented expectation that it only ever shows the current session's data (expected, not a bug); then a plain `spin up --build --runtime-config-file runtime-config.toml` serves /internal/kv-explorer/ as a 404 from gui-pages.
```

## Critical files

- `dev/kv-explorer.toml` (new)
- `dev/kv-explorer-up.sh` (new)
- `gui-pages/tests/test_manifest_components.py` (new)
- `spin-dev.toml` (new, generated, gitignored — never committed)
- `.gitignore`
- `CLAUDE.md`
- `TASKS.md`
- `docs/plans/kv-explorer.md` (this file)

Explicitly **not** modified: `spin.toml`, `runtime-config.toml`, `Jenkinsfile`
(the new test runs inside the existing `gui-pages (Python)` stage), and every
file under `redirect/`, `api/`, and `gui/`.

## Verification

In execution order.

1. Fragment composes as TOML, and the real manifest is untouched:

   ```bash
   python3 -c "import tomllib,pathlib; d=tomllib.loads(pathlib.Path('spin.toml').read_text()+pathlib.Path('dev/kv-explorer.toml').read_text()); print(sorted(d['component']))"
   git diff --exit-code spin.toml && echo "spin.toml unchanged"
   grep -c kv-explorer spin.toml   # expect 0
   ```

2. Test suites — only `gui-pages`'s is affected, but the other two confirm no
   collateral damage:

   ```bash
   cd gui-pages && uv run pytest
   cd api && uv run pytest
   cd redirect && go test ./linkgate/...   # never `go test ./...`
   ```

3. The guard actually guards. Temporarily append `dev/kv-explorer.toml` to
   `spin.toml`, re-run `cd gui-pages && uv run pytest`, confirm
   `test_committed_manifest_has_no_dev_only_components` **fails**, then revert
   with `git checkout spin.toml`.

4. Full local run with the explorer:

   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> \
   SPIN_VARIABLE_KV_EXPLORER_PASSWORD=<kvpw> \
   SPIN_VARIABLE_COOKIE_SECURE=false \
     ./dev/kv-explorer-up.sh
   ```

   Then, in a browser: log in at `/login.html`, create a link, and click it once
   so analytics are written.

5. Store scoping, by `curl` (the reliable check — the UI reports a denied store
   as an empty table, see the facts section):

   ```bash
   curl -s -u kv:<kvpw> http://127.0.0.1:3000/internal/kv-explorer/api/stores/links
   # 200, {"store":"links","keys":["slug:abc123", ...]}

   curl -s -u kv:<kvpw> http://127.0.0.1:3000/internal/kv-explorer/api/stores/analytics
   # 200, keys include count:abc123 and events:abc123:<slot>

   curl -s -o /dev/null -w '%{http_code}\n' -u kv:<kvpw> \
     http://127.0.0.1:3000/internal/kv-explorer/api/stores/users
   # NOT 200 (expect 500) — and the response body must contain no key names

   curl -s -o /dev/null -w '%{http_code}\n' \
     http://127.0.0.1:3000/internal/kv-explorer/api/stores/links
   # 401
   ```

   If any of the permitted-store calls fails with an outbound-networking error
   rather than a KV error, **stop here and report it.** Do not widen
   `allowed_outbound_hosts` to upstream's wildcards to make it pass.

6. The UI, at `http://127.0.0.1:3000/internal/kv-explorer/` (trailing slash):
   type `links` into Store Label, click Load, authenticate as `kv` / `<kvpw>`
   when prompted, and confirm the `slug:` key appears and its value is the
   link's JSON record. Repeat with `analytics`. Type `users` and confirm no keys
   appear. Confirm the app itself is unaffected — `/dashboard.html` still loads,
   themes still switch.

7. Full CRUD is real (do this deliberately, on a throwaway link): edit the
   `slug:` value in the UI, save, and confirm `GET /api/links` reflects the edit;
   delete the key and confirm `/r/<slug>` now 404s.

8. Session-scoped data, so nobody files it as a bug later: stop the script,
   rerun it, and reload the explorer. Expect the `links` and `analytics` stores
   to be **empty** — the local `type = "spin"` stores are not persisted (see the
   facts section), which is also why `admin_bootstrap_password` has to be
   supplied on every run. If the previous session's keys *do* survive, that is
   worth knowing too: it means a persistent file exists somewhere after all, and
   the "separate Spin app" alternative is back on the table.

9. Production shape — the same tree, without the script:

   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3000/internal/kv-explorer/
   # 404, served by gui-pages' catch-all
   ```

   This run must also succeed **without** `SPIN_VARIABLE_KV_EXPLORER_PASSWORD`
   set — proof the explorer's required variables never leak into the real
   manifest.

## Out of scope / follow-ups

- **Any change to the explorer itself** — no fork, no vendoring, no restyling,
  no CSP, no integration with this app's sessions, roles, or
  `KNOWN_PERMISSIONS`. Stated non-goals.
- **Access to the `users` store**, permanently, at any credential level.
- **Pinning a newer explorer release.** v0.10.0 (January 2024) is the latest;
  the repo is not archived but has had no release in over two years. If it ever
  stops working under a future Spin, the options are to build it from the
  upstream source unmodified or to drop the tool — not to fork it. Belongs under
  `TASKS.md`'s "Future work (not scheduled)" only if it actually breaks.
- **A generated dev manifest for anything else.** If a second dev-only component
  ever appears, `dev/kv-explorer-up.sh` should become a general
  `dev/up.sh` that concatenates every `dev/*.fragment.toml` — a rename and a
  glob, deliberately not done pre-emptively for one component.
- **Making the local KV stores persistent** (`path = ".spin/kv.db"` in
  `runtime-config.toml`). Tempting while working on data-inspection tooling, and
  it would reopen the "separate Spin app" option above, but it changes the
  behavior of every existing local workflow (the admin bootstrap, test data
  lifetime) and is a separate decision. Worth a `TASKS.md` "Future work" entry if
  anyone wants it.
