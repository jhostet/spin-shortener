# Toggleable Redirect Prefix

## Context

An Akamai property (edge configuration, **outside this repo entirely**) will be
set up in production to rewrite an end-user-facing URL like
`https://go.example.com/{slug}` into `/r/{slug}` before proxying to this Spin
app. Real end users will therefore never see or type `/r/`, but the Spin app
still receives `/r/{slug}` on the wire in every environment — via the
property's rewrite in production, and directly in local/test environments that
have no such property configured.

What is inadequate today: every place this app *displays* or *encodes* a short
URL hardcodes `/r/`. So the moment the property goes live, an operator who
clicks Copy, exports the CSV, or prints a QR code hands out a URL that is one
segment longer than the one end users are told about. The two shapes still both
work (the property passes `/r/...` through), but the operator's artifacts stop
matching the campaign's own published address — and a printed QR code is
unrecallable.

This is the same category of feature as CLAUDE.md's "Multi-domain display": a
display/encoding-layer concern that never touches resolution. That section's own
framing applies verbatim here — the value is "read fresh at the moment each URL
is built" and "nothing enforces a link to a domain." Nothing about `/r/{slug}`
resolution changes.

There is no existing TASKS.md Future-work entry for this; it is a new
requirement arriving with the property work.

**Confirmed decisions (settled by the user before planning — do not
re-litigate):**

- **One global setting for the whole deployment, not per-domain.** The user was
  explicitly offered "per-domain inside `public_base_urls`" and chose "one
  global Spin variable". Accepted consequence, stated by the user: you cannot
  simultaneously serve a property-fronted production domain and a
  direct-to-Spin test domain with different `/r/` behaviour in the *same*
  running deployment. You flip the flag per environment at deploy time.
- **The default MUST be today's behaviour (include `/r/`).** Every existing
  deployment, and anyone without the property configured, keeps working with
  zero config changes.
- **`gui/app.js`'s `slugChip()` pill label follows the same toggle.** When the
  toggle hides `/r/` in the real short URL, the admin-facing chip shows the
  bare slug too, for visual consistency with the real URL shown on the same
  page. (This is a cosmetic label, not a URL.)
- **Non-goal: the app's own wire routing never changes.** See "Non-goals" below
  for the exact list and the one narrow exception in `spin.toml`.

## Key technical facts confirmed during research

- **There are FOUR hardcoded `/r/` display sites, not three.** `grep -rn -- '/r/'
  gui gui-pages api --include='*.js' --include='*.html' --include='*.py'`
  returns, excluding comments and test assertions:
  1. `gui/app.js:443` — `shortUrlFor(slug)`: `` return `${getSelectedDomain()}/r/${slug}`; ``
  2. `gui/app.js:262` — `slugChip(slug, …)`: `` const label = `/r/${escapeHtml(slug)}`; ``
  3. `api/qr.py:95` — `short_url = f"{base_url}/r/{slug}"`
  4. **`gui/dashboard.js:761`** — the dashboard table's Short-link cell renders
     its own inline chip, **not** via `slugChip()`:
     `` <span class="slug-chip" title="${escapeHtml(shortUrl)}">/r/${escapeHtml(link.slug)}</span> ``
     This one was not in the brief. It must be changed too, or the dashboard —
     the busiest surface in the app — is the only page still showing `/r/`.
- **`shortUrlFor` has 6 call sites, all already funnelling through that one
  function**, so they need no individual change: `gui/dashboard.js:747` (the
  row's `title` tooltip), `:804` (row Copy button), `:1611`/`:1613` (create-success
  banner + its Copy), `:1688` (CSV export's "Short link" column), and
  `gui/links/detail.js:74`/`:122` (detail heading + Copy). Confirmed by
  `grep -rn "shortUrlFor\|slugChip" gui/`.
- **`slugChip` has 4 call sites**, all admin-facing tables:
  `gui/admin/store-maintenance.js:227`, `:246`, `:506`, and
  `gui/admin/url-policy.js:223`.
- **`/api/auth/me` is the existing carrier for exactly this kind of
  configuration.** `api/app.py:341-352` returns `username`, `role`,
  `permissions`, `assigned_domains`, and `domains` — the last computed as
  `domains.visible_base_urls(result.assigned_domains, configured_domains)`,
  where `configured_domains = domains.parse_base_urls(await
  variables.get("public_base_urls"))` (`api/app.py:329`). It is the precedent
  for handing the GUI a server-side config value. There is no other config
  endpoint.
- **Every GUI page that displays a short URL or a slug chip already sequences
  its first render behind `initHeader()` resolving**, so a field added to
  `/auth/me` is available before any chip or URL is built. Confirmed at
  `gui/dashboard.js:16` (`loadMe()` awaits `initHeader()`) and `:1674`
  (`loadMe().then(() => loadLinks(...))`); `gui/links/detail.js:107`;
  `gui/admin/url-policy.js:275`; `gui/admin/store-maintenance.js:644`.
  `initHeader()` itself calls `renderDomainSelector(...)` from inside the
  `if (result.ok)` block for the same reason (`gui/app.js:575`).
- **`gui/app.js` is a classic script loaded before every page script**
  (`<script src="app.js">` then `<script src="dashboard.js">` — confirmed in
  `gui/dashboard.html:210-211`, `gui/links/detail.html:72-73`,
  `gui/admin/url-policy.html:126-127`). So a module-level `let` in `app.js` is
  fully initialized before any page code runs, and a hoisted `function`
  declared later in `app.js` may be referenced by one declared earlier
  (`slugChip` at line 261 calling a helper defined near line 442 is safe).
- **`api/app.py` has two established patterns for reading a Spin variable**, and
  they differ. `_obs_config()` (`:74-79`) and `_app_version_value()` (`:87-91`)
  **cache** the value in a module global with a `None` sentinel, with a comment
  explaining that a Spin variable cannot change without a redeploy (Akamai has
  no update-a-variable command) or a restart. `_cookie_secure()` (`:160-161`)
  does **not** cache: `(await variables.get("cookie_secure")).strip().lower() == "true"`.
- **`api/domains.py` is a pure module with zero `spin_sdk` imports**, is fully
  host-testable (`api/tests/test_domains.py`), and already owns "how a short
  URL's origin is decided" (`normalize_base_url`, `parse_base_urls`,
  `visible_base_urls`, `resolve_base_url`). Its own docstring names the
  testability rule.
- **`handle_qr`'s signature today is
  `async def handle_qr(store, principal, slug, query, base_urls)`** — five
  positional parameters, called from `api/app.py:409` as
  `qr.handle_qr(links_store, result, slug, query, configured_domains)`. There
  are 20 tests in `api/tests/test_qr.py`; four of them assert the exact encoded
  string (`:176`, `:194`, `:205`, `:237`).
- **Baseline is green.** `cd api && uv run pytest` → `767 passed in 13.60s`
  (run 2026-09-03, before any change).
- **`gui-pages/tests/test_manifest_components.py` parses `spin.toml` with
  `tomllib` and asserts the component set is exactly
  `{redirect, api, gui, gui-pages}`.** It does not inspect `[variables]`, so
  adding a variable declaration cannot break it — but a malformed edit to
  `spin.toml` would, which is why the `gui-pages` suite belongs in this plan's
  verification list.
- **`redirect/prompt.html:31` is `<form method="POST" action="/r/{{.Slug}}">`
  — an absolute path.** Under a property-fronted deployment the prompt page is
  served at the end-user URL `https://go.example.com/promo`, and the browser
  will POST to `https://go.example.com/r/promo`. **This is a hard requirement
  on the property, not on this app:** the property must pass a path that
  already begins `/r/` through *unchanged* rather than prefixing it again into
  `/r/r/promo`. Since `prompt.html` is a non-goal here, that requirement is
  non-negotiable. `redirect`'s prompt CSP already permits it —
  `form-action` lists `https:`/`http:`, not `'self'` (CLAUDE.md, "Security
  response headers").
- **`redirect`'s three error templates mention `/r/` only inside HTML comments**
  (`redirect/error-404.html:7-8`, `error-500.html:7-8`, `error-503.html:7-8`,
  explaining why asset paths are absolute), never in served copy. Confirmed by
  reading each. So none of them needs to change.
- **`gui-pages/errorpages.py:75` is the one piece of user-visible copy in the
  app that names `/r/`**: the 404 page's detail line reads "a short link's path
  starts with `<code>/r/</code>`". Deliberately left alone — see "Trade-offs".
- **The dashboard's text filter does not match against the short URL**, only
  `link.slug`, `link.target_url` and `link.tags`
  (`gui/dashboard.js:531-540`), so changing the chip label cannot change which
  rows a filter term matches.
- **UNCONFIRMED — the property's path-rewrite scope.** A slug is
  `^[A-Za-z0-9_-]{3,32}$` (`links.CUSTOM_SLUG_PATTERN`), so `login`, `admin`,
  `index`, `dashboard` and `vendor` are all legal slugs that collide with real
  first path segments this app serves. A property that rewrites *every*
  first-segment path to `/r/...` would break the GUI, the API and every static
  asset on that hostname. What it would take to confirm: the property
  configuration itself, or a statement that the branded domain serves short
  links only and the GUI lives on a different hostname. See "Property-side
  requirements" below — this plan does not and cannot fix it app-side.

## Non-goals (state these in CLAUDE.md too)

**The app always speaks `/r/{slug}` on its own wire protocol, in every
environment, under every setting of this flag.** None of the following changes,
and a builder that touches them has deviated from the plan:

- `spin.toml`'s `[[trigger.http]] route = "/r/..."` and its `component =
  "redirect"` binding — and no other `[[trigger.http]]` block either.
- `redirect/main.go` — its registered routes, `handleRedirectGet`,
  `handleRedirectPost`, `setSecurityHeaders`, anything.
- `redirect/prompt.html` — in particular `action="/r/{{.Slug}}"` stays exactly
  as written.
- `redirect/error-404.html`, `error-500.html`, `error-503.html`,
  `redirect/errorpage.go`, `redirect/passwordgate.go`, `redirect/linkgate/`
  (any file).
- `[component.redirect.variables]` — the redirect component does **not** learn
  about this flag and must not.
- `gui-pages/` — no file. It reads no Spin variable today and gains none here.
- The KV data model — no new field on a link record, no new key type, so none
  of the three obligations a new key type imposes (`backup.py`'s
  `INDEX_KEYS`/`restore_write_order`, `consistency.py`'s shape recognition,
  `kvprefix.STORE_PREFIXES`) is triggered.
- `Jenkinsfile` — the three test commands are unchanged.

**The one narrow exception, and it is unavoidable:** `spin.toml` gains a
`[variables]` declaration and one line under `[component.api.variables]`. Spin's
variable provider resolves declared variables by name; `variables.get()` on an
undeclared name fails, so there is no way to add a Spin variable without
declaring it. **No route, no component, no trigger, and nothing under
`[component.redirect]`/`[component.gui-pages]` changes.** This is additive
configuration, not routing.

## Property-side requirements (outside this repo — record them, do not build them)

For a deployment running with the flag off, the property must:

1. Rewrite `/{slug}` → `/r/{slug}` before proxying to the app.
2. **Pass a path that already begins with `/r/` through unchanged.** The
   password prompt's form action is an absolute `/r/{slug}` and is not
   changing; a blind prefix would produce `/r/r/{slug}` and every
   password-protected link would 404 on submit while working perfectly on the
   initial GET. This is the single most likely way to get the property wrong.
3. **Not capture the app's own first-segment paths.** On a hostname that also
   serves the GUI, the rewrite must exclude at minimum: `/`, `/api/...`,
   `/admin/*`, `/links/*`, `/vendor/*`, `/robots.txt`, `/favicon.ico`,
   `/favicon.svg`, `/.well-known/*`, and each exact `gui`-component asset route
   in `spin.toml` (`/app.js`, `/theme.css`, `/theme-init.js`, `/index.js`,
   `/login.js`, `/dashboard.js`, `/dashboard.css`, `/index.html`,
   `/login.html`, `/dashboard.html`, and the page-scoped `/admin/*`,
   `/links/*` assets). Note `login`, `admin`, `index` and `dashboard` are all
   *legal slugs* under `CUSTOM_SLUG_PATTERN`, so this is a real collision
   surface, not a theoretical one. The clean resolution is for the branded
   short-link hostname to serve short links only, with the GUI on a separate
   hostname — but that is the property owner's call, and this plan does not
   assume it.

This app deliberately does **not** reserve GUI-path slugs to defend against (3);
see "Trade-offs" and the Future-work entry.

## Configuration: the `include_redirect_prefix` Spin variable

`spin.toml`, in the existing `[variables]` block (alongside `public_base_urls`,
which it composes with):

```toml
# Display/encoding only — the app ALWAYS serves /r/{slug} on the wire, in every
# environment, whatever this is set to. Set it to "false" when an edge property
# (e.g. an Akamai property) rewrites https://host/{slug} -> /r/{slug} in front
# of this app, so every short URL the GUI copies, exports or encodes into a QR
# code matches what an end user actually types. ONE GLOBAL SETTING, deliberately
# not per-domain in public_base_urls — see CLAUDE.md. Anything other than the
# literal "false" means include the prefix, so a typo can never silently strip
# /r/ from every copied and printed URL.
include_redirect_prefix = { default = "true" }
```

and one line under `[component.api.variables]`:

```toml
include_redirect_prefix = "{{ include_redirect_prefix }}"
```

Deliberately **not** added to `[component.redirect.variables]` or
`[component.gui-pages.variables]`.

**Polarity: `!= "false"`, not `== "true"` — a deliberate divergence from
`cookie_secure`.** `_cookie_secure()` uses `== "true"`, so an unrecognised value
falls to `False`. Copying that here would mean a typo (`SPIN_VARIABLE_INCLUDE_REDIRECT_PREFIX=ture`)
silently strips `/r/` from every URL an operator copies, exports and prints, on
a deployment that has no property in front of it — dead links, and a printed QR
code cannot be recalled. The safe landing state here is today's behaviour, so
the parse is inverted. This is the same *spirit* as `log_level`'s fail-closed
`parse_log_level` (unrecognised → the safe state), applied to a variable whose
safe state happens to be the truthy one.

## API changes

### `api/domains.py` — two new pure helpers

`domains.py` is the right home: pure, zero `spin_sdk` imports, already the
module that owns short-URL origin decisions, already has a dedicated test file.

```python
# The path segment the redirect component is routed on. It is a constant of
# this app's wire protocol, NOT configuration — spin.toml's
# `route = "/r/..."` and redirect/prompt.html's form action never change.
# Only whether a *displayed or encoded* URL includes it is configurable.
REDIRECT_PATH_PREFIX = "/r"


def parse_include_redirect_prefix(raw: str | None) -> bool:
    """True unless `raw` is exactly "false" (whitespace- and case-insensitive).

    Inverted relative to app.py's `cookie_secure` parse, deliberately: an
    unrecognised value must land on today's behaviour. Stripping /r/ from a
    deployment with no edge rewrite in front of it produces dead copied links
    and unrecallable printed QR codes.
    """
    if not isinstance(raw, str):
        return True
    return raw.strip().lower() != "false"


def short_url_for(base_url: str, slug: str, include_prefix: bool = True) -> str:
    """The short URL to display or encode. `base_url` carries no trailing
    slash by `normalize_base_url`'s construction, so exactly one slash is
    added either way."""
    prefix = REDIRECT_PATH_PREFIX if include_prefix else ""
    return f"{base_url}{prefix}/{slug}"
```

### `api/qr.py` — thread the flag through, keep the resolution guard intact

Signature gains a trailing keyword parameter with a default of `True`:

```python
async def handle_qr(store, principal: Principal, slug: str, query: dict,
                    base_urls: list[str], include_redirect_prefix: bool = True):
```

and line 95 becomes:

```python
    short_url = domains.short_url_for(base_url, slug, include_redirect_prefix)
```

Everything above it is untouched — the `?base=` allowlist validation
(`domains.resolve_base_url`), the `no_base_url_configured` 500, the
`invalid_base_url` 400, `can_view`, `_safe_filename_slug`. **The flag is applied
strictly after base-URL resolution**, so it can never widen what a caller may
put in the encoded origin. Update the module docstring's
`` `{base_url}/r/{slug}` `` to note the prefix is now conditional and that the
QR still never encodes `target_url`.

**Why a default of `True` rather than a required parameter.** CLAUDE.md records
that `validate_bulk_rows` takes its policy as a *required* parameter with no
default, "because a default is exactly how the bulk path would stay silently
open." That rule is about a **security control**, where a forgotten argument
fails open. This is a display preference, where a forgotten argument fails to
today's behaviour — the safe direction — and where the 20 existing
`test_qr.py` tests then serve as a free pin on the default. A required
parameter would only trade that for a `TypeError` in `app.py`, which is the one
file pytest cannot reach.

### `api/app.py` — read, cache, and publish

Follow the `_app_version_value()` pattern (`:84-91`), not `_cookie_secure()`'s
per-request read: this value is read on `/auth/me` (every page load of every
authenticated page) and on every QR request, and CLAUDE.md documents caching as
the convention for a variable that cannot change without a redeploy or restart.
The sentinel must be `None`, never falsy-checked — `False` is a legitimate
cached value.

```python
# Cached like _app_version above, for the same reason: a Spin variable cannot
# change without a redeploy or a restart. None means "not yet read"; False is a
# legitimate value, so the sentinel is checked with `is None`, never falsiness.
_include_redirect_prefix: bool | None = None


async def _include_redirect_prefix_value() -> bool:
    global _include_redirect_prefix
    if _include_redirect_prefix is None:
        _include_redirect_prefix = domains.parse_include_redirect_prefix(
            await variables.get("include_redirect_prefix")
        )
    return _include_redirect_prefix
```

Two wirings:

1. In `handle_request`, alongside the existing
   `configured_domains = domains.parse_base_urls(...)` at `:329`, add
   `include_redirect_prefix = await _include_redirect_prefix_value()`.
2. `/api/auth/me` (`:341-352`) gains one field, next to `domains`:
   `"include_redirect_prefix": include_redirect_prefix,`
3. The QR call at `:409` becomes
   `qr.handle_qr(links_store, result, slug, query, configured_domains, include_redirect_prefix)`.

No other endpoint needs it. `users.handle_list`/`handle_create`/`handle_update`
take `configured_domains` for `assigned_domains` validation and are unaffected.

## GUI changes

### `gui/app.js` — one accessor, three consumers

Add beside the existing domain-preference state (near `SS_DOMAIN_KEY` /
`availableDomains`, ~line 401):

```js
// Display/encoding only. The app always serves /r/{slug}; this decides whether
// a URL we *show, copy, export or encode* includes that segment — false when an
// edge property rewrites /{slug} -> /r/{slug} in front of the app.
// Set by initHeader() from /auth/me, before any page can build a URL or a chip.
// Defaults to true and stays true if /auth/me fails or omits the field (an
// older API build), matching getSelectedDomain()'s degrade-to-today's-behavior
// rule — a wrong `true` shows a longer URL that still works, a wrong `false`
// shows one that does not.
let includeRedirectPrefix = true;
```

One accessor, and it is the **only** place `/r` is spelled in URL or label
construction anywhere in `gui/`:

```js
function redirectPathPrefix() {
  return includeRedirectPrefix ? "/r" : "";
}
```

Then:

- `shortUrlFor` (line 442-444) becomes
  `` return `${getSelectedDomain()}${redirectPathPrefix()}/${slug}`; ``
- `slugChip` (line 262) becomes
  `` const label = `${redirectPathPrefix()}/${escapeHtml(slug)}`; ``
  — note the slug stays individually escaped exactly as today; the prefix is a
  literal from a two-valued function and needs no escaping.
- `initHeader`, inside the existing `if (result.ok)` block and **before** the
  `renderDomainSelector(...)` call at line 575, so the flag is set before
  anything can render:
  ```js
  // `!== false` rather than `=== true`: an absent field (an older api build,
  // or a response shape change) must mean "include", the exact mirror of the
  // server's `!= "false"` parse in domains.parse_include_redirect_prefix.
  includeRedirectPrefix = result.data.include_redirect_prefix !== false;
  ```

`redirectPathPrefix` is a hoisted function declaration, so `slugChip` at line
261 may call it even though it is defined near line 442 — and
`let includeRedirectPrefix` is initialized during `app.js`'s own evaluation,
which completes before any page script runs (classic scripts, `app.js` first in
every page). Do not convert either to a `const` arrow function without
re-checking that ordering.

**No `onDomainChange`-style re-render hook is needed or wanted.** The flag is a
deploy-time Spin variable; it cannot change inside a session the way the domain
selector can. The existing `onDomainChange` handlers (`gui/dashboard.js:1625`,
`gui/links/detail.js:120`) already re-render everything that embeds a short URL
and need no change.

### `gui/dashboard.js` — the fourth site

Line 761's inline chip becomes:

```js
        <span class="slug-chip" title="${escapeHtml(shortUrl)}">${redirectPathPrefix()}/${escapeHtml(link.slug)}</span>
```

The `title` keeps carrying the full `shortUrl` (already
`shortUrlFor(link.slug)` from line 747, so it picks the flag up for free), and
the existing comment above it — explaining that the displayed chip drops the
redundant origin while Copy/View use the full URL — stays accurate and should
be kept.

Nothing else in `dashboard.js` changes: the CSV export's "Short link" column
(`:1688`), the create-success banner (`:1611`, `:1613`) and the row Copy handler
(`:804`) all already call `shortUrlFor`.

`gui/links/detail.js`, `gui/admin/store-maintenance.js` and
`gui/admin/url-policy.js` need **no** changes — they go through `shortUrlFor` or
`slugChip`.

## Tests

`gui/` has no JS test harness in this repo (CLAUDE.md, "Tests"), so the GUI half
is verified live, per this repo's standing convention. The API half is fully
host-testable.

### `api/tests/test_domains.py` — new cases

- `parse_include_redirect_prefix`: `None`, `""`, `"true"`, `"TRUE"`, `"yes"`,
  `"1"`, `"ture"` (a typo) and a non-string all → `True`; `"false"`, `"FALSE"`,
  `" false "` → `False`. The typo case is the point of the test, not padding —
  it pins the fail-safe polarity.
- `short_url_for`: `("https://go.example.com", "abc", True)` →
  `"https://go.example.com/r/abc"`; the same with `False` →
  `"https://go.example.com/abc"`; and an assertion that neither result contains
  `"//"` after the scheme (a `normalize_base_url` output carries no trailing
  slash, and this pins that the two functions compose).

### `api/tests/test_qr.py` — existing 20 unchanged, three new

The 20 existing tests keep passing **unmodified** — including the four that
assert the literal `/r/` string (`:176`, `:194`, `:205`, `:237`) — which is what
pins the default. Add:

- `test_qr_omits_redirect_prefix_when_disabled` — `handle_qr(..., ["http://localhost:3000"], False)`;
  asserts `mock_make.call_args[0][0] == f"http://localhost:3000/{slug}"` and
  `"/r/" not in encoded_data`.
- `test_qr_prefix_off_still_uses_the_resolved_configured_domain` — with
  `CONFIGURED` and `{"base": ["https://go.example.com"]}` plus `False`; asserts
  `f"https://go.example.com/{slug}"`. Proves the flag is uniform across
  configured domains and composes with multi-domain display rather than
  interacting with it.
- `test_qr_prefix_off_still_rejects_an_unconfigured_base` — with
  `{"base": ["https://evil.example"]}` plus `False`; asserts `400`,
  `error == "invalid_base_url"`, and `mock_make.assert_not_called()`. Proves the
  new flag cannot weaken the QR-poisoning close, which is the one security
  property in this file.

Mutation check for the builder: making `short_url_for` ignore its
`include_prefix` argument must fail the first two new tests; making
`parse_include_redirect_prefix` return `raw.strip().lower() == "true"` must
fail the typo case in `test_domains.py`.

## Documentation

CLAUDE.md gains a short section immediately after "Multi-domain display",
matching that section's style. It must state, at minimum:

- The app always speaks `/r/{slug}` on the wire, in every environment; this
  flag is display/encoding only, in the same category as the domain selector.
- `include_redirect_prefix`, its default `"true"`, and the `!= "false"` parse
  with the reason for the inverted polarity.
- One global setting, deliberately not per-domain in `public_base_urls`, and the
  accepted consequence (you cannot serve a property-fronted domain and a
  direct-to-Spin domain with different behaviour in one running deployment).
- The four display sites and the one accessor each language routes through
  (`domains.short_url_for`, `redirectPathPrefix()`).
- The non-goals list above, explicitly.
- The two property-side requirements: pass `/r/...` through unchanged (because
  `prompt.html`'s form action is absolute and never changes), and do not
  capture the app's own first-segment paths (with the note that `login`,
  `admin`, `index` and `dashboard` are legal slugs).
- The deploy line: `--variable include_redirect_prefix=false` joins the
  Akamai deploy command's variable list when the property is live.

`README.md` needs no change — it documents no Spin variables today (only the
`spin up` quick start).

## Trade-offs and rejected alternatives

1. **A string prefix variable (`redirect_path_prefix`, default `"/r"`, empty to
   omit) instead of a boolean.** Attractive: strictly more expressive, and it
   would cover a future property that rewrites `/go/{slug}` instead. Rejected.
   The app's route is `/r/...` and is a non-goal to change, so any value other
   than `/r` or `""` would produce a URL that resolves nowhere — the variable
   would be able to express states the app cannot serve. It also opens a
   validation surface that a boolean simply does not have: an operator-supplied
   string lands verbatim in a printed QR code and in copied text, so it would
   need leading-slash normalization, a control-character rejection (the exact
   class `docs/plans/reject-control-chars-in-target-url.md` just closed one
   field over), a `..` guard and its own test file — for a capability nobody
   has asked for. Revisit only if a real property rewrites to something other
   than `/r/`, at which point the app's own route is the thing to reconsider.

2. **Per-domain configuration inside `public_base_urls`** (e.g.
   `https://go.example.com|noprefix,http://localhost:3000`). Attractive and
   genuinely more capable: `public_base_urls` already supports several domains
   in one deployment, and per-domain would let a property-fronted production
   domain and a direct-to-Spin test domain coexist correctly in one running
   app. **Rejected by the user's explicit decision when asked**, in favour of
   one global flag. The cost is stated and accepted: you flip the flag per
   environment at deploy time rather than serving both behaviours at once. It
   also keeps `public_base_urls`' value shape a plain comma-separated list of
   bare origins — `normalize_base_url` rejects anything carrying a path, query
   or fragment today, and a `|`-suffixed entry would need that parser widened
   and every existing entry re-reasoned about.

3. **A new `GET /api/config` endpoint instead of a field on `/auth/me`.**
   Attractive: a clean home for deployment configuration, not tangled with
   identity, and reusable by a future unauthenticated page. Rejected. Every
   page that displays a short URL already awaits `initHeader()` →
   `/auth/me`, and that response is already the carrier for
   `domains`/`assigned_domains` — the exact same class of value. A new endpoint
   means a second network round trip on every page load, a second thing to
   sequence render behind, and a new routing branch in `api/app.py` for one
   boolean. Revisit if an *unauthenticated* surface ever needs to build a short
   URL, which none does today.

4. **Deriving the flag client-side** (e.g. `app.js` probes `/{slug}` and sees
   whether it 302s). Rejected outright. Locally it would always answer "prefix
   required" because no property exists there, so a local build could never
   test the property-fronted rendering; it would put a speculative request on
   every page load; and it would make the displayed URL depend on network
   conditions rather than on configuration.

5. **Do nothing — let operators strip `/r/` by hand.** This was live: the
   property passes `/r/...` through, so every URL the GUI produces today keeps
   working after the property ships. It loses on the artifacts that cannot be
   corrected after the fact — a printed QR code and an exported CSV handed to a
   campaign partner — and on the plain confusion of an operator's Copy button
   producing a different address from the one the campaign publishes.

6. **Requiring the new `handle_qr` parameter rather than defaulting it to
   `True`.** Attractive on the `validate_bulk_rows` precedent CLAUDE.md records
   ("a default is exactly how the bulk path would stay silently open"). Loses
   because that rule is about a security control failing *open*; here the
   default is today's behaviour, i.e. the safe direction, and it lets all 20
   existing `test_qr.py` tests stand unmodified as a pin on the default.

7. **Copying `cookie_secure`'s `== "true"` parse.** Rejected — see
   "Configuration" above. An unrecognised value must land on *include*.

8. **Rewriting `gui-pages/errorpages.py`'s 404 copy** ("a short link's path
   starts with `/r/`") to respect the flag. Attractive: it is the one piece of
   *user-visible* copy in the app that names `/r/`, and it is wrong for an
   end user on a property-fronted deployment. Rejected for this change. Three
   reasons. (a) It is largely unreachable in the case that matters: with the
   property rewriting `/{slug}` → `/r/{slug}`, an end user's mistyped short
   link becomes `/r/mistyped` and hits **`redirect`'s** 404, not `gui-pages`'.
   (b) `errorpages.ERROR_PAGES` renders its bytes constants **at import time**,
   specifically so the 500 branch performs no I/O of its own and cannot fail
   the way the read it is handling just did (CLAUDE.md, Architecture) — making
   the body depend on a Spin variable destroys that property. (c) `gui-pages`
   reads no Spin variable at all today; giving it its first one, plus a
   `[component.gui-pages.variables]` entry, plus re-opening
   `test_no_inline_code.py`'s coverage of `ERROR_PAGES`, is a materially larger
   change than the sentence is worth. Filed as Future work with a trigger.

9. **Reserving GUI-path slugs (`login`, `admin`, `index`, `dashboard`, …) so a
   property rewrite can never shadow a real page.** Attractive: it would make
   requirement (3) in "Property-side requirements" impossible to get wrong.
   Rejected as out of scope and probably wrong anyway — this repo already
   retired a banned-word slug list (`docs/plans/banned-word-slugs.md`) on the
   reasoning that a list shipped in the repo needs a deploy to change and
   carries false-positive risk; and the collision is a property-configuration
   defect, which the property owner must fix in the property, not a defect this
   app can paper over. Filed as Future work with a trigger.

10. **Adding the flag to `[component.redirect.variables]` "for symmetry."**
    Rejected. `redirect` has no display surface and never builds a URL for a
    human — it reads a path and writes a `Location` header. Handing it the
    variable would create a false impression that its routing is configurable
    and would put a variable read on the hot path for nothing.

## Tasks

```
- [ ] Declare the include_redirect_prefix Spin variable and map it into the api component — file(s): spin.toml — done when: `[variables]` gains `include_redirect_prefix = { default = "true" }` with a comment naming the edge-property rewrite, the display-only scope and the "anything but the literal false means include" rule; `[component.api.variables]` maps it; `[component.redirect.variables]` and `[component.gui-pages.variables]` do NOT; no `[[trigger.http]]` block and no `route =` line anywhere in the file is modified; and `cd gui-pages && uv run pytest` still passes
- [ ] Add the pure prefix helpers to api/domains.py — file(s): api/domains.py, api/tests/test_domains.py — done when: `REDIRECT_PATH_PREFIX = "/r"`, `parse_include_redirect_prefix(raw)` (True for None/""/"true"/"TRUE"/"yes"/"1"/"ture"/non-string, False only for "false"/"FALSE"/" false ") and `short_url_for(base_url, slug, include_prefix=True)` exist with docstrings recording why the polarity is inverted relative to app.py's cookie_secure parse; new tests cover every listed input plus both prefix polarities with exactly one slash after the origin; and `cd api && uv run pytest tests/test_domains.py` passes
- [ ] Thread the flag through the QR endpoint — file(s): api/qr.py, api/tests/test_qr.py — done when: `handle_qr` takes a trailing `include_redirect_prefix: bool = True`, builds `short_url` via `domains.short_url_for` strictly AFTER `domains.resolve_base_url`, and its module docstring records the prefix is now conditional; all 20 pre-existing test_qr.py tests pass UNMODIFIED (pinning the default); three new tests pass — prefix-off encodes `{base}/{slug}` with no "/r/", prefix-off with an explicit `?base=` still encodes the second configured domain, and prefix-off still returns 400 invalid_base_url for an unconfigured base with `qrcode.make` never called; and mutation-verified (making `short_url_for` ignore `include_prefix` fails the first two)
- [ ] Read, cache and publish the flag in api/app.py — file(s): api/app.py — done when: a module-level `_include_redirect_prefix: bool | None` sentinel (checked with `is None`, never falsiness) caches `domains.parse_include_redirect_prefix(await variables.get("include_redirect_prefix"))` following `_app_version_value`'s precedent; `handle_request` resolves it beside `configured_domains`; `GET /api/auth/me` returns `"include_redirect_prefix": true` by default; the QR call passes it; and a live `spin up` started with `SPIN_VARIABLE_INCLUDE_REDIRECT_PREFIX=false` returns `false` on that field for an authenticated `/api/auth/me`
- [ ] Route every gui/app.js short URL and slug chip through one prefix accessor — file(s): gui/app.js — done when: `redirectPathPrefix()` is the only place "/r" is spelled in app.js's URL/label construction, `shortUrlFor` and `slugChip` both use it (slugChip still escaping the slug individually), `let includeRedirectPrefix = true` sits beside the domain-preference state, and `initHeader()` sets it from `result.data.include_redirect_prefix !== false` inside the `if (result.ok)` block BEFORE the `renderDomainSelector` call — so it stays true when /auth/me fails or omits the field
- [ ] Fix the dashboard's inline slug chip, the fourth hardcoded /r/ site — file(s): gui/dashboard.js — done when: the Short-link cell's `<span class="slug-chip">` at ~line 761 builds its label from `redirectPathPrefix()` instead of a literal "/r/", its `title` still carries the full `shortUrlFor(link.slug)`, the existing comment above it is kept, and `grep -rn -- '"/r/\|/r/${' gui/` returns no remaining hardcoded display site
- [ ] Document the toggle in CLAUDE.md — file(s): CLAUDE.md — done when: a new section immediately after "Multi-domain display" states that the app always speaks /r/{slug} on the wire whatever the flag says; names `include_redirect_prefix`, its "true" default and the `!= "false"` fail-safe polarity; records that it is ONE global setting, deliberately not per-domain, with the accepted consequence; lists the four display sites and the two single-source accessors; states the non-goals (spin.toml routes, redirect/main.go, redirect/prompt.html, redirect's error pages, gui-pages, the KV data model) explicitly; records the two property-side requirements including that `login`/`admin`/`index`/`dashboard` are legal slugs; and adds `--variable include_redirect_prefix=false` to the Akamai deploy command's variable list
- [ ] End-to-end manual verification of the redirect-prefix toggle — file(s): (none — verification step) — done when: with the DEFAULT settings, `spin up --build` runs clean, dashboard chips read `/r/<slug>`, Copy yields `http://localhost:3000/r/<slug>`, the CSV's Short link column and the detail page heading agree, a phone scan of the detail page's QR resolves the link, and `/api/auth/me` reports `include_redirect_prefix: true`; then with `SPIN_VARIABLE_INCLUDE_REDIRECT_PREFIX=false`, all four of those read `<slug>` / `http://localhost:3000/<slug>` with no `/r/`, the admin URL-policy violations table and the store-maintenance findings chips show bare slugs too, and a phone scan of the QR yields `http://localhost:3000/<slug>`; and under BOTH settings `curl -sI localhost:3000/r/<slug>` still returns `302` while `curl -sI localhost:3000/<slug>` returns the gui-pages 404, and `curl -s localhost:3000/r/<password-protected-slug>` still serves a prompt whose form action is `/r/<slug>` — proving the wire protocol is untouched
```

## Critical files

- `spin.toml` — modified (additive: one `[variables]` entry, one
  `[component.api.variables]` line; **no route/trigger/component change**)
- `api/domains.py` — modified
- `api/qr.py` — modified
- `api/app.py` — modified
- `api/tests/test_domains.py` — modified
- `api/tests/test_qr.py` — modified
- `gui/app.js` — modified
- `gui/dashboard.js` — modified
- `CLAUDE.md` — modified
- `TASKS.md` — modified (task lines + rejected-alternative entries)
- `docs/plans/toggleable-redirect-prefix.md` — new (this file)

## Verification

Run in this order.

1. **API suite.** `cd api && uv run pytest` — passes, count ≥ 772 (baseline 767
   plus the new `test_domains.py` and `test_qr.py` cases). The four existing
   `test_qr.py` assertions containing `/r/` must still pass *unmodified*.

2. **`gui-pages` suite** (it is the suite that parses `spin.toml`):
   `cd gui-pages && uv run pytest` — passes, in particular
   `test_manifest_components.py`.

3. **Not run, deliberately:** `cd redirect && go test ./linkgate/...` — no Go
   file changes in this plan. And never `go test ./...`, `go build ./...` or
   `go vet ./...`, which fail by design on `package main`.

4. **Live, default settings** (the "everyone who upgrades and changes nothing"
   case), from the repo root:

   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```

   Sign in through the login form (not a raw fetch — a fetch login produces
   `csrf_mismatch` 403s). Create a link, note its slug, then check:
   - Dashboard Short-link chip reads `/r/<slug>`; its tooltip reads
     `http://localhost:3000/r/<slug>`.
   - The row's Copy button yields `http://localhost:3000/r/<slug>`.
   - Export CSV — the "Short link" column reads `http://localhost:3000/r/<slug>`.
   - `links/detail.html?slug=<slug>` heading reads
     `http://localhost:3000/r/<slug>`; scan the QR preview with a phone — it
     offers `http://localhost:3000/r/<slug>`.
   - In DevTools, the `/api/auth/me` response body contains
     `"include_redirect_prefix": true`.

5. **Live, toggle off.** Restart (a `gui/` edit is invisible until restart —
   `spin_static_fs` serves a startup snapshot; if a GUI change looks like it did
   not apply, `curl localhost:3000/app.js` and diff against disk before
   doubting it):

   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false \
   SPIN_VARIABLE_INCLUDE_REDIRECT_PREFIX=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```

   Repeat every check in step 4; each must now read `<slug>` /
   `http://localhost:3000/<slug>` with no `/r/` anywhere, and
   `/api/auth/me` must report `"include_redirect_prefix": false`. Additionally:
   - `admin/url-policy.html` → Check existing links: any violation row's slug
     chip reads `<slug>`, not `/r/<slug>`. (Add a deny rule matching your test
     link's host to produce a row.)
   - `admin/store-maintenance.html` → Check for inconsistencies / orphan report:
     any slug chip reads `<slug>`.

6. **The wire protocol is untouched — run under BOTH settings.** This is the
   step that proves the non-goals held:

   ```bash
   curl -sI "http://localhost:3000/r/<slug>"   # -> 302, Location: <target>
   curl -sI "http://localhost:3000/<slug>"     # -> 404 (gui-pages catch-all; no property locally)
   ```

   And for a password-protected link:

   ```bash
   curl -s "http://localhost:3000/r/<protected-slug>" | grep 'action='
   # -> <form method="POST" action="/r/<protected-slug>">   (unchanged, both settings)
   ```

7. **Typo safety.** Restart once with
   `SPIN_VARIABLE_INCLUDE_REDIRECT_PREFIX=ture` and confirm `/api/auth/me`
   reports `true` and the dashboard shows `/r/<slug>` — the fail-safe polarity,
   live.

8. **Diff review before commit.** `git diff --numstat` should show only the ten
   files in "Critical files"; `git diff --numstat TASKS.md` should show only
   ticked checkboxes and the appended sections.

## Out of scope / follow-ups

- **`gui-pages/errorpages.py`'s 404 copy naming `/r/`** — the one piece of
  user-visible copy left that assumes the prefix. Deliberately unchanged; full
  reasoning in Trade-offs #8. **Trigger for picking it up: a real report of an
  end user landing on that page with a mistyped short link on a
  property-fronted deployment**, which would also mean the property is routing
  unmatched first-segment paths to `gui-pages` rather than to `redirect`.
  Filed under TASKS.md's "Future work (not scheduled)".
- **Reserving GUI-path slugs** so a property rewrite cannot shadow a real page.
  Deliberately not built; reasoning in Trade-offs #9. **Trigger: a property
  configuration that must rewrite on a hostname shared with the GUI**, i.e. the
  branded short-link domain is not given its own hostname. Filed under Future
  work.
- **Per-domain prefix configuration** — the alternative the user considered and
  declined (Trade-offs #2). Not filed as Future work; it is a settled decision,
  not a deferral. It would be reopened only by a concrete need to serve a
  property-fronted domain and a direct-to-Spin domain from one running
  deployment.
- **Anything in `redirect/`.** The hot path resolves `/r/{slug}` and is
  completely unaffected, by design.
