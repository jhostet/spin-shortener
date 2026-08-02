# Multi-Domain Display

## Context

The app has exactly one base domain today, and it is inconsistent with itself.
The GUI builds every short URL from `location.origin` (five sites — see
"Surfaces"), while the QR endpoint builds its from the `public_base_url` Spin
variable (`api/app.py:116`, `api/qr.py:61`). Browse the app on any host other
than the one `public_base_url` names, and the URL you copy off the dashboard
and the URL encoded in the QR code you print are different strings. Neither is
wrong; there is just no single answer to "what is this link's URL."

`TASKS.md`'s Future-work entry **"Multi-domain short-link hosting +
admin-managed destination domain allow/deny-list"** (raised 2026-07-18) is the
brief. That entry bundles two independent ideas. **This plan supersedes the
first half only** — multi-domain short-link hosting. The second half, the
admin-managed allow/deny-list constraining which domains a link's `target_url`
may point at, is a different feature (content/abuse control, not display) and
**remains untouched Future work**; a replacement entry narrowed to just that
half is added under `## Future work (not scheduled)`.

That entry also assumed a much larger feature than this one: a KV-backed domain
registry, an admin domain-management page, a new `domain` field on every link
record, and an open question about `Host`-header enforcement in the redirect.
The entry itself says "re-confirm scope/priority before starting, don't assume
this write-up is a complete spec." Scope was re-confirmed and it landed
somewhere much smaller. The load-bearing reason:

> **`redirect` resolves purely by slug and never reads the `Host` header.**
> Every link already works on every domain pointed at the deployment. Storing
> "which domain this link is for" without enforcing it produces metadata that
> lies — it would appear in the dashboard, the CSV export and the API as though
> it were binding, and the first person to share an off-brand URL discovers it
> works fine. A field that describes a constraint the system does not apply is
> worse than no field.

So domains here are a **viewer preference**, in the same category as the
light/dark theme: chosen in the persistent nav, stored in `localStorage`, and
driving every URL the UI hands the user. Nothing is stored per link, nothing is
enforced, nothing changes in `redirect/`.

**Confirmed decisions** (settled by the user before planning — not reopened
here):

1. **No per-link domain field.** Domains are purely a viewer preference. No
   schema change, no migration, no new field in the link record, the API, the
   CSV export or the bulk-create format.
2. **No enforcement in the redirect.** `redirect/main.go` is unchanged. Nothing
   in this plan touches the Go hot path.
3. **Slugs stay globally unique**, exactly as today.
4. **The domain list is a Spin variable**, not a KV registry and not an admin
   page — adding a domain already requires DNS and routing work, so it can
   never be self-service anyway.
5. **`assigned_domains` per user** restricts which domains the selector offers
   them. With no enforcement anywhere this is a convenience guardrail against
   handing out an off-brand URL by accident, **not a security control**. It
   stays out of `auth.py`'s `KNOWN_PERMISSIONS`, which is a deliberately fixed
   vocabulary.
6. **The selector lives in the persistent nav and persists in `localStorage`**,
   following the existing theme control exactly (`renderThemeToggle` in
   `gui/app.js`, `gui/theme-init.js`, the `ss-theme` key).

## Key technical facts confirmed during research

- **`redirect` never reads `Host`.** `grep -rn "Host\|host" redirect/main.go`
  returns nothing that inspects the request host; resolution is
  `slug:{slug}` → KV → 302. Nothing in this plan changes that file.

- **The GUI has exactly five `location.origin` sites.** `grep -rn
  "location.origin" gui/` → `dashboard.js:207` (row chip `title`),
  `dashboard.js:259` (`handleCopyClick`), `dashboard.js:623` (create-success
  banner), `dashboard.js:686` (CSV `Short link` column), `links/detail.js:15`
  (heading + Copy). That is the complete set.

- **The bulk-create success banner has no short URL and no Copy button.**
  `dashboard.js:577` writes `Created N links.` and nothing else. The
  requirement's surface list named it; it does not exist. Corrected here rather
  than planned for.

- **The dashboard's visible short-link column does not display an origin.**
  `dashboard.js:221` renders `/r/${slug}` as the chip text with the full URL in
  the `title` attribute only — deliberately, per the comment there and
  DESIGN.md's Chips entry. So on the dashboard the domain change is visible in
  hover tooltips, Copy output, the CSV and the create-success banner, but not
  in the table text. The detail page's `h1` *does* show the full URL
  (`detail.js:16`).

- **Three QR URLs, all on the detail page.** `links/detail.js:38-40` — the
  `<img id="qr-preview">` src plus the SVG and PNG download `href`s. They are
  raw `<img src>`/`<a href>`, not `api.get` calls, so no CSRF token is
  involved (all GET).

- **`/auth/me` already carries the principal to every page.**
  `api/app.py:53-61` returns `{username, role, permissions}`; `initHeader()`
  (`gui/app.js:287`) fetches it and returns the result, and all three
  authenticated pages already sequence their first render behind it
  (`dashboard.js:672`, `links/detail.js:82`, `admin/users.js:223`). This is the
  natural carrier for the domain list — no new endpoint needed.

- **`Principal` is only ever constructed with keyword arguments.** `grep -rn
  "Principal(" api/` → one production site (`auth.py:198`) and eleven test
  sites, all keyword. Appending a defaulted `assigned_domains` field is
  backwards-compatible with every one of them.

- **`_public_user` is allowlist-by-exclusion.** `api/users.py:17` returns every
  key except `password_hash`, so `assigned_domains` appears in API responses
  automatically once it is stored. Existing user records lack the key entirely,
  so every reader must use `.get("assigned_domains", [])` /
  `user.assigned_domains || []`.

- **`_validate_permissions` is the precedent for the new validator.**
  `api/users.py:25-31` returns one error string (`invalid_permissions`) for
  both a malformed value and an unknown member, checked against a frozenset.
  `_validate_assigned_domains` mirrors it exactly.

- **Pico sets `color` on `select` explicitly, so the nav's `#fff` does not leak
  into it.** `gui/vendor/pico.min.css`'s `input,select,textarea` rule locally
  redefines `--pico-color: var(--pico-form-element-color)` and then declares
  `color: var(--pico-color)`. A declared value on the element always beats an
  inherited one, so `#app-header nav li { color: #fff }` (the specificity trap
  DESIGN.md warns about three times) cannot affect the select's own text. It
  **would** affect any visible text label placed beside it inside the `<li>` —
  which is why the control uses `aria-label`, not a visible label.

- **Pico's `select` is `width: 100%` with a `margin-bottom`.** Confirmed in
  `vendor/pico.min.css`: `select,textarea{width:100%}` and
  `select,textarea{margin-bottom:var(--pico-spacing)}`. Inside a nav `<li>` both
  must be overridden — the identical `width: auto` fix `.theme-toggle`,
  `#links-table [role=group]`, `#bulk-bar [role=group]`,
  `#users-table ... [role=group]` and `.detail-heading [role=group]` all
  already needed.

- **The sitewide 44px tap-target floor does not cover `<select>`.**
  `gui/theme.css:693-699` lists `button, [type=submit], [type=button],
  [type=reset], [role=button]`; `:721` adds `#app-header nav a`. A `<select>`
  matches neither. DESIGN.md records that this exact gap was found by an audit
  once already (nav anchors at 38.4px). The new control needs its own
  `min-height: 44px`.

- **The dark theme's select boundary against the nav fill is computed at
  roughly 1.10:1 (fill) and 1.97:1 (border) — both under WCAG 1.4.11's 3:1.**
  Derived from the token values in `gui/theme.css`:
  `--pico-form-element-background-color: #0d1a2e` (:220),
  `--pico-form-element-border-color: #31456a` (:221),
  `--ss-chrome-bg: #060f1d` (:237). **These are arithmetic from the hex values,
  not live measurements — UNCONFIRMED until the builder measures with
  `getComputedStyle` in a real browser.** The light theme's equivalent
  (`#fff` field on `#0a1628` chrome) is far above 3:1 and is not a concern. The
  fix, if the measurement confirms the arithmetic, is the treatment
  `#app-header nav #logout-btn` already uses at `gui/theme.css:284-287`:
  `--pico-border-color: rgba(255, 255, 255, 0.4)`. No new token.

- **The nav already overflowed at 480px when the theme control was added.**
  DESIGN.md's Theme control entry and `gui/theme.css:452-470` record the
  pre-approved fallback: the account `<ul>` wraps inside `@media (max-width:
  480px)`, with `align-items: flex-start` on `<nav>` (not optional — a stretched
  brand group was the original bug). A fourth item in that `<ul>` makes it wrap
  further; see "Nav crowding" below for what must be measured at 390px.

- **The inline-code guard covers every new file this plan touches.**
  `gui-pages/tests/test_no_inline_code.py:41-45` globs every non-vendor `.js`
  under `gui/` (so `app.js`, `dashboard.js`, `links/detail.js`,
  `admin/users.js` are all covered) and checks `ROUTES`' HTML pages for inline
  `<script>`, `<style>`, `style=` and `on<event>=`. The regexes match inside
  comments too. New markup must use classes, the native `hidden` attribute and
  `addEventListener`.

- **No new `spin.toml` route is needed.** The selector's code lives in
  `gui/app.js`, which already has an exact route (`spin.toml:57-59`) and is
  loaded by `dashboard.html`, `links/detail.html`, `admin/users.html` and
  `login.html`. A separate head-loaded file like `theme-init.js` exists only to
  beat the FOUC; no short URL is painted before JS runs, so that reason does not
  apply here.

- **Baseline test counts, run 2026-08-02 at `66f60fc`:** `cd api && uv run
  pytest` → 192 passed; `cd gui-pages && uv run pytest` → 57 passed;
  `cd redirect && go test ./linkgate/...` → ok.

- **UNCONFIRMED: that a Spin variable value containing commas passes through
  the env-var and `runtime-config.toml` providers verbatim.** Spin variables are
  plain strings and nothing in the manifest treats a comma specially, so this is
  expected to work, but it has not been run. Confirming it costs nothing and is
  built into the end-to-end verification step (which sets two domains).

- **UNCONFIRMED: that Spin silently ignores `SPIN_VARIABLE_PUBLIC_BASE_URL`
  once the variable is renamed to `public_base_urls`.** The env-var provider
  looks up declared variables by name rather than enumerating the environment,
  so an env var for an undeclared variable is expected to be ignored with no
  warning. This matters because it is a silent-misconfiguration path: an
  operator who kept the old env var would get the default and never be told.
  The mitigation is documentation (CLAUDE.md), not code.

## Data model

**Nothing changes in the `links` store.** No `domain` field, no migration, no
new key, no change to `public_link`, the CSV columns or the bulk-create format.
This is worth stating flatly because it is the single largest difference
between this plan and the Future-work entry it supersedes.

**One optional field is added to the user record** in the `users` store:

```
assigned_domains: list[str]     # absent or [] means "unrestricted"
```

Absent and `[]` are treated identically everywhere, so **no backfill of
existing user records is required**. `auth.ensure_bootstrap_admin`
(`api/auth.py:144-152`) gains `"assigned_domains": []` for shape consistency
on a fresh store only; existing stores keep the old shape and behave the same.

## Configuration: the `public_base_urls` variable

`spin.toml:13`'s

```toml
public_base_url = { default = "http://localhost:3000" }
```

is **replaced** (not supplemented) by

```toml
public_base_urls = { default = "http://localhost:3000" }
```

and `spin.toml:44`'s `public_base_url = "{{ public_base_url }}"` under
`[component.api.variables]` becomes `public_base_urls = "{{ public_base_urls }}"`.

Format: a comma-separated list of absolute origins, e.g.

```
SPIN_VARIABLE_PUBLIC_BASE_URLS="https://go.tirerack.com,https://tr.link,http://localhost:3000"
```

- Each entry is `scheme://host[:port]` — scheme is required (the QR needs a
  complete URL and local dev needs `http` and a port), and a path, query or
  fragment is rejected.
- **Order is meaningful: the first entry is the default selection** for a user
  who has never chosen one, and is also the base the QR endpoint uses when no
  `base` parameter is supplied.
- Entries are normalized (lowercased scheme and host, trailing `/` stripped)
  and de-duplicated; malformed entries are dropped rather than raising, so one
  typo cannot 500 every API request.

**Why replace rather than keep `public_base_url` as a separate default entry:**
one variable means exactly one ordered list, with no possibility of the default
not being a member of it, no dedup question, and nothing for an operator to
keep in sync. The app is not deployed anywhere yet (`PRODUCT.md`'s Operating
Context: "Currently self-hosted/local... not yet deployed to a production
host"), so there is no live configuration to migrate. The default value is
unchanged, so `spin up` with no variables set behaves exactly as today.

## API changes

### New module: `api/domains.py`

Pure, host-testable, zero `spin_sdk` imports, no `store` parameter — the same
shape as `api/bulk.py`'s parser half and `api/responses.py`.

```python
def normalize_base_url(value: str) -> str | None:
    """`scheme://host[:port]`, lowercased, no trailing slash. None if `value`
    is not an absolute http/https origin, or carries a path, query or
    fragment."""

def parse_base_urls(raw: str) -> list[str]:
    """Comma-separated origins -> normalized list. Order preserved; blanks,
    duplicates and malformed entries dropped. May return []."""

def visible_base_urls(assigned: list[str], configured: list[str]) -> list[str]:
    """The domains a user may select, in *configured* order. An empty
    assignment -- or one that no longer intersects `configured` -- means
    unrestricted: a user must always have at least one domain to hand out."""

def resolve_base_url(candidate: str | None, configured: list[str]) -> str | None:
    """The exact `configured` entry matching `candidate`, or None if it is not
    configured. A falsy `candidate` selects `configured[0]`. Returns the
    server's own string, never the caller's."""
```

`normalize_base_url` reuses `urlparse`, matching `links.is_valid_target_url`'s
`parsed.scheme in ("http", "https") and bool(parsed.netloc)` shape
(`api/links.py:90-92`) rather than inventing a second URL-validation idiom.

The **"returns the server's own string, never the caller's"** property of
`resolve_base_url` is the security-relevant one and belongs in the docstring:
even if `normalize_base_url` had a bug, the value handed to the QR encoder is
an element of the configured list by construction, not a transformed copy of
client input.

### `GET /api/links/{slug}/qr` — the poisoning vector, closed

Today `api/app.py:116-117` reads `public_base_url` server-side and
`api/qr.py:61` encodes `f"{public_base_url.rstrip('/')}/r/{slug}"`. Once the
client chooses a domain, that choice has to reach this endpoint. **The endpoint
must validate it against the configured list rather than trusting it.** An
endpoint that encodes an arbitrary client-supplied base URL produces a printed
QR code that looks like it came from this app and points anywhere — a
durable, offline artifact that outlives any fix.

Signature change:

```python
async def handle_qr(store, principal: Principal, slug: str, query: dict,
                    base_urls: list[str]):
```

(replacing `public_base_url: str`), and inside, after the existing
`format`/`size` validation:

```python
if not base_urls:
    return json_response(500, {"error": "no_base_url_configured"})

base_url = domains.resolve_base_url(_query_value(query, "base", ""), base_urls)
if base_url is None:
    return json_response(400, {"error": "invalid_base_url"})

short_url = f"{base_url}/r/{slug}"
```

Note `base_url` is already normalized with no trailing slash, so the existing
`.rstrip('/')` moves into `normalize_base_url` and disappears from here.

Four decisions embedded above, each deliberate:

- **A full base URL (`?base=https://go.example.com`), not an index.** An index
  into the configured list would be unpoisonable by construction, but it makes
  every QR URL positional: reordering `public_base_urls` silently repoints
  every bookmarked or in-flight QR request at a different domain. Allowlist
  membership on a full URL gives the same guarantee with stable, legible URLs.
- **Reject, do not silently fall back.** A fallback to `configured[0]` on an
  unrecognized `base` would print a QR for a domain the user did not choose —
  quieter than poisoning but still an integrity failure, and it would hide
  misconfiguration. `400 invalid_base_url`.
- **An absent `base` defaults to `configured[0]`**, preserving today's behavior
  for any caller that does not send one (an old bookmark, a curl, the endpoint
  hit directly).
- **Validation is against the *configured* list, not the viewer's
  `assigned_domains`.** The configured list is the security boundary;
  `assigned_domains` is an explicitly-non-security convenience guardrail
  (decision 5). Gating the QR on it would turn a stale assignment into a hard
  error on a page that is already open, for zero security gain — every
  configured domain resolves every slug regardless. A user *can* therefore
  obtain a QR for a domain not assigned to them by hand-crafting a request;
  that is by design and harmless.

`api/app.py`'s qr branch reads the parsed list instead of the single string.

### `assigned_domains` on the user record

`api/auth.py`:

- `Principal` gains `assigned_domains: list[str] = field(default_factory=list)`
  — **appended last, with a default**, so all twelve existing construction
  sites keep working unchanged.
- `resolve_session` (`:198-203`) populates it from
  `user.get("assigned_domains", [])`.
- `ensure_bootstrap_admin` (`:144-152`) writes `"assigned_domains": []` into
  the seeded record.
- **`KNOWN_PERMISSIONS` is untouched.** Per decision 5 and the reasoning
  already recorded in the Future-work entry, domain assignment is a separate,
  structurally independent list — not a dynamically-generated permission
  string, which would break the "reject anything outside this fixed set"
  design.

`api/users.py`:

- `_validate_assigned_domains(value, configured) -> Optional[str]`, mirroring
  `_validate_permissions` exactly: returns `"invalid_assigned_domains"` for a
  non-list, a non-string member, or any member not in `configured`; `None`
  otherwise. One code for both failure modes, matching the precedent.
- `handle_create` accepts an optional `assigned_domains` (default `[]`) and
  stores it.
- `UPDATABLE_FIELDS` (`:100`) gains `"assigned_domains"`; `handle_update`
  validates and assigns it.
- `handle_list` returns `{"users": [...], "all_domains": configured}` so the
  admin page can render one checkbox per configured domain without a second
  request or a new endpoint. This response is already gated on `users.manage`.
- All four handlers that need it take `configured_domains: list[str]` as a
  plain parameter — no `spin_sdk` import, no module-level state.

**Storage never contains an unconfigured domain**, because the validator
rejects one on the way in. A *previously* valid entry can still go stale when
an operator removes a domain from `public_base_urls`; see "Stale assignments"
below.

### `GET /api/auth/me`

```python
return json_response(200, {
    "username": result.username,
    "role": result.role,
    "permissions": result.permissions,
    "assigned_domains": result.assigned_domains,
    "domains": domains.visible_base_urls(result.assigned_domains, configured_domains),
})
```

`domains` is the pre-filtered, configured-order list the GUI renders directly —
the client does no filtering and needs no knowledge of the rules.
`assigned_domains` rides along for transparency and debugging; the GUI does not
read it.

### `api/app.py` wiring

`handle_request` already performs three `variables.get` calls at the top
(`:38-41`). A fourth joins them:

```python
configured_domains = domains.parse_base_urls(await variables.get("public_base_urls"))
```

and is passed to the `/auth/me`, `/api/users*` and `/api/links/{slug}/qr`
branches. Parsing per request is a few string operations on a short list;
`api` is explicitly not the hot path.

## GUI changes

### `gui/app.js` — shared plumbing

Everything below sits in `app.js`, next to `renderThemeToggle`, and is loaded
by every page that already loads `app.js`. No new file, no new `spin.toml`
route.

```js
const SS_DOMAIN_KEY = "ss-domain";
let availableDomains = [];                 // set by initHeader() from /auth/me
const domainChangeListeners = [];

function readStoredDomain()   // try/catch — Safari private mode throws on ACCESS
function writeStoredDomain(v) // try/catch, best-effort

function getSelectedDomain() {
  const stored = readStoredDomain();
  if (stored && availableDomains.includes(stored)) return stored;
  return availableDomains[0] || location.origin;
}

function shortUrlFor(slug) { return `${getSelectedDomain()}/r/${slug}`; }

function onDomainChange(fn) { domainChangeListeners.push(fn); }

function renderDomainSelector(container, domainList) { ... }
```

Points that are load-bearing rather than stylistic:

- **`localStorage` access is wrapped in `try`/`catch` on both read and write**,
  copying `theme-init.js:17-31` verbatim in spirit. Safari private mode and
  blocked-storage configurations throw on *access*, not just on write.
- **An unrecognized stored value falls through to `availableDomains[0]` without
  rewriting storage** — same rule as `theme-init.js`'s `get()`. A user whose
  assignment temporarily loses a domain gets it back if it returns.
- **`|| location.origin` is the last-resort fallback**, which is exactly
  today's behavior, so a failed `/auth/me` or an empty configured list degrades
  to the current app rather than rendering `undefined/r/x`.
- **`getSelectedDomain()` is called at the moment of use, never captured in a
  closure at render time.** This is what lets the Copy buttons keep working
  across a domain change without re-registering listeners (re-registering would
  stack them). The two existing Copy handlers that capture `shortUrl`
  (`dashboard.js:626`, `links/detail.js:19`) are rewritten to call
  `shortUrlFor(slug)` inside the handler.
- **`slug` is interpolated raw**, matching all five existing sites. Slugs are
  `[A-Za-z0-9_-]{3,32}` or base62 (`links.py:19-20`), so encoding is a no-op;
  changing it here would be unrelated churn.

`renderDomainSelector(container, domainList)`:

- **`if (domainList.length < 2) { container.hidden = true; return; }`** — a
  one-option selector is pure clutter, and this means **the nav is byte-for-byte
  unchanged for any single-domain deployment**, which is every deployment
  today. It also removes nav crowding from the common case entirely.
- Builds `<select class="domain-select" aria-label="Short link domain">` with
  one `<option>` per domain: `value` is the **exact server-supplied string**
  (it is compared against the configured list by the QR endpoint), and the
  visible text is just the host — `new URL(base).host` in a `try`/`catch`
  falling back to the raw string. `shop.example.com` instead of
  `https://shop.example.com` is materially narrower in a nav that already
  overflows.
- No visible text label. `aria-label` only — a visible label inside the `<li>`
  would inherit `#app-header nav li { color: #fff }` and land straight in
  DESIGN.md's thrice-recorded specificity trap for no benefit.
- Sets `select.value = getSelectedDomain()`, and on `change` writes storage and
  calls every registered listener.
- Listens for cross-tab `storage` events on `SS_DOMAIN_KEY` (and `null`, i.e.
  `clear()`), re-syncing `select.value` and notifying listeners — the same
  four lines `renderThemeToggle` already has at `gui/app.js:230-232`, for the
  same reason: two open tabs would otherwise disagree until one navigates.

`initHeader()`:

- The nav skeleton gains `<li id="domain-control"></li>` immediately before
  `<li id="theme-control">`, so the two preference controls sit together.
- **`renderDomainSelector` is called inside the `if (result.ok)` block, after
  the `/auth/me` await** — unlike `renderThemeToggle`, which is called before
  it, because the domain list *comes from* that response. `availableDomains` is
  assigned there too. Every authenticated page already sequences its first
  render behind `initHeader()` resolving, so no page can call `shortUrlFor`
  before the list is populated.
- **`login.html` gets no domain selector.** `login.js:2` mounts
  `renderThemeToggle` directly because that page has no `#app-header`; there is
  no session and therefore no `/auth/me`, no domain list, and no short link to
  hand out. Nothing to add.

### `gui/theme.css` — nav styling

One new block, placed beside the existing `.theme-toggle` rules:

```css
#app-header nav .domain-select {
  width: auto;            /* Pico's select is width:100% — same fix .theme-toggle needed */
  margin-bottom: 0;       /* Pico's select carries margin-bottom: var(--pico-spacing) */
  min-height: 44px;       /* the sitewide floor covers button/[role=button]/nav a, not select */
  max-width: 12rem;
}
```

Selector specificity is `(1,1,1)`, deliberately matching the shape of
`#app-header nav .theme-toggle button` and beating `#app-header nav li`'s
`(1,0,2)` on the class component — the approach DESIGN.md mandates instead of
hoping source order cooperates.

**Plus a dark-theme boundary fix, conditional on measurement.** Per the facts
section, the select's fill (`#0d1a2e`) and border (`#31456a`) against the dark
nav's `#060f1d` are computed at ~1.10:1 and ~1.97:1 — under WCAG 1.4.11's 3:1
for a UI-component boundary. If live `getComputedStyle` measurement confirms
this, apply the treatment `#app-header nav #logout-btn` already uses at
`gui/theme.css:284-287`:

```css
#app-header nav .domain-select {
  --pico-border-color: rgba(255, 255, 255, 0.4);
}
```

No new token, and it makes the two nav controls plus Log out share one border
treatment. The light theme (`#fff` field on `#0a1628`) needs nothing. Record
the measured ratios, both themes, in the task note — DESIGN.md's history is
explicit that assumed ratios have shipped as failures here three times.

#### Nav crowding

The nav holds brand, breadcrumb, identity chip, theme control and Log out, and
already overflowed at 480px when the theme control landed. The pre-approved
fallback (`gui/theme.css:452-470`: the account `<ul>` wraps, `<nav>` gets
`align-items: flex-start`) is in place and handles a fourth item structurally —
the account group simply wraps onto more rows.

What this plan requires the builder to do rather than assume:

- **Measure `scrollWidth` vs `clientWidth` on `#app-header nav` at 1400px,
  768px, 480px and 390px, in both themes, with two configured domains** — the
  identical protocol the theme control used. Record the numbers.
- **At 390px specifically**, the expectation is that the account `<ul>` wraps
  to three rows (identity chip / domain select + theme toggle / Log out, in
  whatever order they fit) with no horizontal overflow, and the brand group
  stays flush at the top via `align-items: flex-start`. If it overflows
  instead, the fallback is to let the select take a full row at that breakpoint
  (`#app-header nav .domain-select { max-width: 100%; width: 100%; }` inside
  the existing `@media (max-width: 480px)` block) rather than hiding the
  control — hiding it would strand a user on a phone with whatever domain was
  last chosen on a desktop.
- Do **not** shed the domain control the way `.identity-role` sheds. The role
  line is decoration; the domain determines the URL the user is about to send
  someone.

### `gui/dashboard.js`

Four `location.origin` sites become `shortUrlFor(...)`:

| Line | Today | Becomes |
|---|---|---|
| 207 | `` `${location.origin}/r/${link.slug}` `` | `shortUrlFor(link.slug)` |
| 259 | `` `${location.origin}/r/${btn.dataset.slug}` `` | `shortUrlFor(btn.dataset.slug)` |
| 623 | `` `${location.origin}/r/${data.slug}` `` | `shortUrlFor(data.slug)` |
| 686 | `` ["Short link", (l) => `${location.origin}/r/${l.slug}`] `` | `["Short link", (l) => shortUrlFor(l.slug)]` |

Two follow-on changes:

- **The create-success banner is extracted into `renderCreateSuccess(slug)`**,
  which sets `successEl.dataset.slug = slug`, writes the chip text from
  `shortUrlFor(slug)`, and wires its Copy button to call `shortUrlFor(slug)`
  *inside* the handler rather than capturing the URL (`dashboard.js:623-626`
  captures today). Called from the create handler and from the domain-change
  callback.
- **A domain-change callback** registered via `onDomainChange`:
  ```js
  onDomainChange(() => {
    renderLinksTable();
    const successEl = document.getElementById("create-success");
    if (!successEl.hidden && successEl.dataset.slug) renderCreateSuccess(successEl.dataset.slug);
  });
  ```
  `renderLinksTable()` refreshes every row's `title` attribute. It also clears
  any bulk selection, because `selectedSlugs.clear()` sits at the top of that
  function by design (`dashboard.js:195`) — consistent with DESIGN.md's Do
  ("clear the selection on any re-render") and harmless, but worth mentioning
  in the task so it does not read as a bug during verification.

The CSV gains **no new column** — the `Short link` column is a full URL and
already carries the domain.

### `gui/links/detail.js`

- `loadLinkInfo`'s heading/Copy/QR block (`:15-19`, `:38-40`) is extracted into
  `applyShortUrl()`, called at the end of `loadLinkInfo()` and again from the
  domain-change callback. It sets:
  - `#short-link-heading` text ← `shortUrlFor(slug)`
  - the three QR URLs, each gaining
    `&base=${encodeURIComponent(getSelectedDomain())}`
- `#detail-copy-btn`'s listener is registered **once** (as today) but reads
  `shortUrlFor(slug)` inside the handler instead of closing over the URL, so
  `applyShortUrl()` never re-registers and listeners cannot stack.
- `onDomainChange(applyShortUrl)`.

Re-setting `#qr-preview.src` re-fetches the image, which is the point — the
preview must show the QR for the domain currently selected.

### `gui/admin/users.html` + `gui/admin/users.js`

The Users page gains an assigned-domains control beside the existing role and
permissions controls, in both the create form and the per-row edit form.

- `users.js` gains `allDomains = []`, populated from `loadUsers()`'s
  `data.all_domains`.
- **Create form:** `users.html` gets an empty
  `<fieldset id="new-domains-fieldset">` with a legend, inside the existing
  `.grid`; `loadUsers()` fills it with one
  `<label><input type="checkbox" class="new-domain" value="..."> host</label>`
  per configured domain. The create submit handler adds
  `assigned_domains: Array.from(document.querySelectorAll(".new-domain:checked")).map(cb => cb.value)`.
  Rendering from JS (rather than static markup like the permission checkboxes)
  is unavoidable — the list is configuration, not a fixed vocabulary.
- **Edit row:** `editRowHtml(user)` gains a matching fieldset of
  `.edit-domain` checkboxes, checked from
  `(user.assigned_domains || []).includes(domain)`. The submit payload adds
  `assigned_domains: Array.from(form.querySelectorAll(".edit-domain:checked:not(:disabled)")).map(cb => cb.value)`.
- **The legend states the rule the code implements**, since it is not
  guessable: `Short-link domains (none checked = all domains)`.
- **Unlike permissions, the fieldset is NOT disabled for admins.** Role
  `admin` bypasses every *permission* check (`Principal.has_permission`), but
  `assigned_domains` is not a permission and is not consulted by
  `has_permission` at all — an admin's selector is filtered by their assignment
  exactly like anyone else's. Disabling the fieldset for admins would be a
  visible lie. Worth an explicit note in the task, because the adjacent
  permissions fieldset does exactly the opposite (`users.js:16-19`).
- **If `allDomains` is empty** (a single configured domain, or none), render
  the fieldset `hidden` — there is nothing meaningful to assign.

#### Stale assignments

An operator can remove a domain from `public_base_urls` while a user still has
it stored in `assigned_domains`. Two behaviors, chosen deliberately:

- **In the selector: filtered out silently.** `visible_base_urls` intersects
  with the configured list, so the user simply never sees it. If the
  intersection is empty, they get the full configured list — a user must always
  be able to produce a URL, and an empty selector would break every Copy button
  on the page.
- **On the admin page: surfaced, and self-healing on the next save.** The edit
  row renders one extra checkbox per stored-but-unconfigured entry, **checked
  and `disabled`**, with the label suffixed ` — no longer configured`. The
  `:not(:disabled)` in the payload selector above means the entry is dropped
  the next time that user is saved. A `:checked` selector alone would match a
  disabled input and send the stale value straight back into
  `_validate_assigned_domains`, which would reject it and make the user
  unsaveable until someone worked out why — that is the specific footgun the
  `:not(:disabled)` avoids, so it is not incidental.

## Trade-offs and rejected alternatives

### Per-link `domain` field (the Future-work entry's design) — rejected

**Attractive because** it is the obvious model, it is what a commercial
shortener shows, it lets the dashboard state "this link is a `go.example.com`
link," and it survives the user switching machines (the preference does not —
`localStorage` is per browser).

**Lost because** `redirect` resolves by slug and ignores `Host`, so the field
would describe a constraint the system does not apply. Every off-brand URL
still works. The dashboard, the CSV export, `GET /api/links` and the QR would
all present it as though it were binding, and the first person to test the
"wrong" domain finds it is not. That is worse than no field: it converts an
honest absence into a confident falsehood, and it puts a lie in an exported CSV
that outlives the session. It also costs a link-record schema change, a
migration story for every existing link, a new column in the bulk-create
format, and a new field in three API surfaces — real, permanent complexity
bought entirely on the promise of enforcement that is explicitly out of scope.

**Revisit if** `Host`-header enforcement in `redirect` becomes a real
requirement (see next). The field only earns its keep alongside enforcement.

### `Host`-header enforcement in `redirect` — rejected

**Attractive because** it would make a per-link `domain` honest, and it would
let a campaign guarantee its links only resolve under its own brand.

**Lost because** it puts a second KV-field comparison and a header parse on the
hot path that exists to be fast, in the one Go component this repo keeps
deliberately minimal (`allowed_outbound_hosts = []`, no dependencies beyond the
SDK). It creates a new, sharp failure mode: a link that works in testing and
404s in production because a CDN, a proxy or an operator's DNS change rewrote
`Host`. And it buys nothing security-relevant — slugs are already documented as
enumerable and non-secret (`CLAUDE.md`, "Security tradeoffs"), so restricting
which hostname may resolve one protects nothing that is protected today.
Decision 2 settles it; this records why.

**Revisit if** a real requirement appears to make a link resolve on exactly one
domain — at which point it needs the per-link field, a migration, and an
explicit decision about what a mismatched `Host` returns (404, to stay
consistent with the time-window behavior).

### KV-backed domain registry + an admin domain-management page — rejected

**Attractive because** it is self-service, it matches how users and links are
already managed, and it avoids a redeploy to add a domain.

**Lost because** the self-service is illusory. Adding a domain requires a DNS
record, a TLS certificate and a routing change at the edge — none of which an
admin can do from this app. A registry page would let someone add
`go.example.com` in the GUI and then discover it resolves nowhere, with the app
happily offering it in every selector. A Spin variable puts the list in the
same place as the rest of the deployment configuration it is inseparable from,
and costs one line of `spin.toml`, one pure module and no new endpoint, page,
permission, KV key or CRUD surface. It also sidesteps the Akamai single-store
blocker (`CLAUDE.md`) entirely by adding no new KV usage at all.

**Revisit if** domains ever become genuinely self-service — i.e. the app gains
the ability to provision DNS/TLS itself.

### Per-domain permission strings instead of `assigned_domains` — rejected

**Attractive because** it reuses the existing permission plumbing end to end:
`KNOWN_PERMISSIONS`, `has_permission`, the users page's checkbox list, the
403 bodies' `required_permission` field.

**Lost because** `KNOWN_PERMISSIONS` (`api/auth.py:29`) is deliberately a
small, fixed, hardcoded frozenset whose entire job is to "reject anything
outside this set rather than silently accepting typos." Generating members at
runtime from a configuration variable destroys that property. This was already
decided in the 2026-07-18 Future-work entry; it is recorded again because it is
the first idea anyone reaching for the permission system will have.

### Trusting the client's `base` parameter in the QR endpoint — rejected

**Attractive because** it is one line: `short_url = f"{query_base}/r/{slug}"`.

**Lost because** it is a QR-poisoning vector. Anyone who can construct a URL
can produce a QR image served by this app, from this app's origin, encoding any
destination at all — and a QR code's whole point is to be printed, handed out
and scanned long after the request that produced it. Allowlist membership
against the server's configured list, returning the *server's* string, closes
it for the cost of one dictionary lookup. Related and also rejected: an
opaque index (`?domain_index=2`), which is unpoisonable but makes every QR URL
positional and silently repoints in-flight requests when the operator reorders
the list.

### Defaulting the selection to `location.origin` — rejected

**Attractive because** it exactly preserves today's behavior, needs no
server-side default, and "the host you are browsing" is an intuitive answer.

**Lost because** the browsing host is frequently the host that should *never*
be shared — an internal admin hostname, an IP, a port-forwarded tunnel — and
the whole point of the feature is to hand out the brand-correct URL.
`configured[0]` is deterministic, operator-controlled, and identical on every
machine. Consequence, stated plainly: **on a single-domain deployment the GUI
switches from `location.origin` to the configured base URL**, so a developer
browsing `http://127.0.0.1:3000` with the default variable will copy
`http://localhost:3000/...`. Both resolve locally. This also *fixes* the
existing GUI-versus-QR disagreement described in Context, which is the same bug
in the opposite direction.

### A separate `domain-init.js`, mirroring `theme-init.js` — rejected

**Attractive because** it is a literal reading of decision 6 ("following the
existing theme control exactly") and keeps preference code out of `app.js`.

**Lost because** `theme-init.js` is a separate render-blocking `<head>` script
for exactly one reason — beating the flash of light theme before first paint —
and no short URL is painted before JS runs, so that reason does not transfer.
A separate file would also need a new exact `spin.toml` route, whose failure
mode `spin.toml:82-84` documents as "a fully-rendered page whose script
silently 404s, so the page looks fine and simply does nothing." `app.js` is
already routed and already loaded everywhere this is needed.

### Making an empty `assigned_domains` mean "no domains" — rejected

**Attractive because** it reads more literally, and the superseded Future-work
entry assumed it ("show a domain multi-select only when the user has any
assigned domains at all").

**Lost because** every existing user record has no `assigned_domains` key, so
"empty means none" would lock every current user out of every domain on
deploy, requiring a backfill migration of the `users` store to fix a problem
created by the reading itself. And a user with no domain has no URL to copy —
the selector would have to fall back to something anyway. "Empty means
unrestricted" needs no migration, degrades safely, and is stated in the admin
fieldset's own legend so it is not a hidden rule.

### Doing nothing — rejected, but it was live

The app works today, single-domain, and nothing is broken enough to force this.
What tipped it: the GUI and the QR endpoint already disagree about a link's
URL, which is a real (if quiet) defect that this work fixes as a side effect;
and the marketing/campaign persona in `PRODUCT.md` is precisely the population
that runs one campaign under one brand domain and another under a second.
Doing nothing also leaves the superseded Future-work entry sitting there as a
much larger, much more invasive plan that anyone picking it up would build
as written.

## Tasks

The lines below were appended verbatim to `TASKS.md` under
`## Multi-domain display`. `TASKS.md` is authoritative; do not track checkbox
state here.

- [ ] Add api/domains.py with the pure base-URL helpers and unit tests (must land before every other task in this section) — file(s): api/domains.py (new), api/tests/test_domains.py (new) — done when: `normalize_base_url`, `parse_base_urls`, `visible_base_urls` and `resolve_base_url` exist with the signatures in docs/plans/multi-domain-display.md, the module has zero `spin_sdk` imports and takes no `store`, and `cd api && uv run pytest` passes with new tests covering: a trailing slash and an uppercase host normalizing to the same value; a path, query or fragment rejected; a comma-separated string with blanks, duplicates and one malformed entry parsing to the right ordered list; an empty `assigned` and a fully-stale `assigned` both returning the whole configured list; a partial `assigned` returning configured order (not assigned order); `resolve_base_url(None, cfg)` returning `cfg[0]`; and `resolve_base_url("https://evil.example", cfg)` returning `None`.
- [ ] Replace public_base_url with public_base_urls and validate the QR endpoint's base parameter against it (depends on the domains task) — file(s): spin.toml, api/app.py, api/qr.py, api/tests/test_qr.py — done when: `spin.toml` declares `public_base_urls = { default = "http://localhost:3000" }` and maps it into `[component.api.variables]` with no `public_base_url` left anywhere; `handle_qr`'s fifth parameter is `base_urls: list[str]` and it returns `500 {"error": "no_base_url_configured"}` for an empty list, `400 {"error": "invalid_base_url"}` for a `?base=` value not in the list, and otherwise encodes `f"{resolve_base_url(...)}/r/{slug}"` using the string from the configured list rather than the caller's; `cd api && uv run pytest` passes with every existing `test_qr.py` call site migrated to a one-element list plus new tests that a valid `?base=` selects the second configured domain, that `?base=https://evil.example` returns 400 with `qrcode.make` never called, that an absent `base` still encodes `configured[0]`, and that a differently-cased/trailing-slashed form of a configured domain is accepted and encoded in its canonical form.
- [ ] Add assigned_domains to the user record and the users API (depends on the domains task) — file(s): api/auth.py, api/users.py, api/app.py, api/tests/test_users.py, api/tests/test_auth.py — done when: `Principal` gains `assigned_domains: list[str]` as a defaulted final field and `resolve_session` populates it via `user.get("assigned_domains", [])`; `ensure_bootstrap_admin` seeds `"assigned_domains": []`; `KNOWN_PERMISSIONS` is unchanged; `users._validate_assigned_domains(value, configured)` mirrors `_validate_permissions` and returns `"invalid_assigned_domains"` for a non-list, a non-string member or a member not in `configured`; `handle_create` accepts and stores the field, `UPDATABLE_FIELDS` includes it and `handle_update` validates it, and `handle_list` returns `{"users": [...], "all_domains": configured}`; the four handlers take `configured_domains: list[str]` as a plain parameter and `app.py` passes the parsed list; `cd api && uv run pytest` passes with new tests that a user record with no `assigned_domains` key still resolves a session, that an unknown domain is rejected on both create and update with nothing written, and that `_public_user` exposes the field.
- [ ] Return the viewer's selectable domains from GET /api/auth/me (depends on the domains and assigned_domains tasks) — file(s): api/app.py — done when: the `/api/auth/me` body gains `assigned_domains` (verbatim from the principal) and `domains` (`domains.visible_base_urls(...)`, already filtered and in configured order), and with `SPIN_VARIABLE_PUBLIC_BASE_URLS="http://localhost:3000,http://127.0.0.1:3000"` running, `curl` as an admin with no assignment returns both domains in that order, and as a user assigned only the second returns just that one.
- [ ] Add the domain selector to the persistent nav (depends on the /auth/me task) — file(s): gui/app.js, gui/theme.css — done when: `app.js` gains `getSelectedDomain()`, `shortUrlFor(slug)`, `onDomainChange(fn)` and `renderDomainSelector(container, domainList)` with every `localStorage` access wrapped in try/catch under the `ss-domain` key; `initHeader()` renders `<li id="domain-control">` before `#theme-control` and calls the selector inside the `/auth/me` success block with the response's `domains`; the control is `hidden` when fewer than 2 domains are offered, option text is the host and option value the full base URL, it carries `aria-label="Short link domain"` with no visible label, and a cross-tab `storage` event re-syncs it; `#app-header nav .domain-select` sets `width: auto`, `margin-bottom: 0` and `min-height: 44px`; `cd gui-pages && uv run pytest` still passes (57, no inline code, including in comments); with two domains configured the selector appears, the choice survives a reload and a navigation to another page, and with one domain the nav is visually identical to before this task.
- [ ] Measure and fix the nav's domain-selector contrast and 390px layout (depends on the nav selector task) — file(s): gui/theme.css — done when: with two domains configured, `getComputedStyle` measurements are recorded in the task note for the select's fill and border against the nav fill in **both** themes, and any ratio under 3:1 is fixed by adding `--pico-border-color: rgba(255, 255, 255, 0.4)` to `#app-header nav .domain-select` (the treatment `#app-header nav #logout-btn` already uses — no new token); and `scrollWidth` vs `clientWidth` on `#app-header nav` is recorded at 1400px, 768px, 480px and 390px in both themes with **no horizontal overflow at any of them**, using the existing `@media (max-width: 480px)` wrap fallback and, only if 390px still overflows, letting the select take a full row there rather than hiding it.
- [ ] Drive every dashboard short-link surface from the selected domain (depends on the nav selector task) — file(s): gui/dashboard.js — done when: all four `location.origin` sites (the row chip `title`, `handleCopyClick`, the create-success banner and the CSV `Short link` column) read `shortUrlFor(...)`; the create-success banner is extracted into `renderCreateSuccess(slug)` which stores `dataset.slug` and whose Copy button calls `shortUrlFor(slug)` inside the handler rather than capturing it; an `onDomainChange` callback re-renders the table and, when visible, the success banner; the CSV gains no new column; `cd gui-pages && uv run pytest` still passes; and in a real browser switching domains changes the hover title, the Copy output, the success banner and a freshly-exported CSV's Short link column, with the bulk selection clearing on the re-render as it already does for filter and sort.
- [ ] Drive the link-detail heading, Copy and QR codes from the selected domain (depends on the QR and nav selector tasks) — file(s): gui/links/detail.js — done when: an extracted `applyShortUrl()` sets `#short-link-heading` from `shortUrlFor(slug)` and appends `&base=${encodeURIComponent(getSelectedDomain())}` to all three QR URLs, `#detail-copy-btn`'s single listener reads `shortUrlFor(slug)` at click time so it is never re-registered, `onDomainChange(applyShortUrl)` is registered, and in a real browser switching domains updates the heading, the Copy output and the QR preview image, both downloaded QR files scan to the selected domain's `/r/<slug>`, and the console shows zero errors.
- [ ] Add the assigned-domains control to the admin users page (depends on the assigned_domains task) — file(s): gui/admin/users.html, gui/admin/users.js — done when: `users.js` stores `all_domains` from `loadUsers()` and renders a checkbox per configured domain in both the create fieldset (`#new-domains-fieldset`, filled from JS) and `editRowHtml`'s edit fieldset, both legends reading `Short-link domains (none checked = all domains)`; both submit payloads send `assigned_domains` using a `:checked:not(:disabled)` selector; a stored-but-unconfigured domain renders as a **checked, disabled** checkbox suffixed ` — no longer configured` so the next save drops it; the fieldset is `hidden` when fewer than 2 domains are configured and is **not** disabled for `admin` role users (unlike the permissions fieldset — `assigned_domains` is not a permission); `cd gui-pages && uv run pytest` still passes; and in a real browser assigning one domain to a test user makes that user's nav selector offer only that domain.
- [ ] Document multi-domain display in CLAUDE.md, PRODUCT.md and DESIGN.md (depends on every task above) — file(s): CLAUDE.md, PRODUCT.md, DESIGN.md, .impeccable/design.json — done when: CLAUDE.md gains a "Multi-domain display" section (peer to "Time-windowed links") stating that every link resolves on every configured domain because `redirect` ignores `Host`, that domains are a viewer preference with no per-link storage and no enforcement, the `public_base_urls` format and that the first entry is the default, that the old `public_base_url` env var is now silently ignored, that `assigned_domains` is a convenience guardrail and deliberately not in `KNOWN_PERMISSIONS`, that empty means unrestricted, and that `?base=` on the QR endpoint is allowlist-validated against the configured list because an unvalidated one is a QR-poisoning vector; PRODUCT.md's Capabilities list gains one accurate line; DESIGN.md's `### Navigation` gains a "Domain selector" entry (hidden below 2 domains, host-only option text, `aria-label` not a visible label to stay clear of the `#app-header nav li` trap, the measured contrast values and the 44px floor that `<select>` does not otherwise get) and `.impeccable/design.json` gains a matching entry in the existing entries' shape; no doc claims a capability the shipped code does not have.
- [ ] End-to-end manual verification of multi-domain display — file(s): (none — verification step) — done when: with `SPIN_VARIABLE_PUBLIC_BASE_URLS="http://localhost:3000,http://127.0.0.1:3000" SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false spin up --build --runtime-config-file runtime-config.toml` running, every numbered step in the plan's Verification section is executed in a real browser with the console open and zero errors of any kind (in particular zero CSP violations) in both light and dark themes, and `cd api && uv run pytest`, `cd gui-pages && uv run pytest` and `cd redirect && go test ./linkgate/...` all pass.

## Critical files

- `docs/plans/multi-domain-display.md` (new) — this plan
- `api/domains.py` (new)
- `api/tests/test_domains.py` (new)
- `spin.toml`
- `api/app.py`
- `api/qr.py`
- `api/auth.py`
- `api/users.py`
- `api/tests/test_qr.py`
- `api/tests/test_users.py`
- `api/tests/test_auth.py`
- `gui/app.js`
- `gui/theme.css`
- `gui/dashboard.js`
- `gui/links/detail.js`
- `gui/admin/users.html`
- `gui/admin/users.js`
- `CLAUDE.md`
- `PRODUCT.md`
- `DESIGN.md`
- `.impeccable/design.json`
- `TASKS.md`

Deliberately **not** in the list: `redirect/` (anything), `api/links.py`,
`api/bulk.py`, `api/analytics.py`, `gui/dashboard.html`, `gui-pages/`,
`runtime-config.toml`, `Jenkinsfile` (the three test commands are unchanged).

## Verification

Run in this order.

1. `cd api && uv run pytest` — expect 192 plus the new tests (roughly 20: ~10
   in `test_domains.py`, ~4 in `test_qr.py`, ~5 in `test_users.py`, ~1 in
   `test_auth.py`). Report the actual number.
2. `cd gui-pages && uv run pytest` — expect **57, unchanged**. A drop means a
   page or script regrew inline code; a rise means someone added a test, which
   is fine but should be called out.
3. `cd redirect && go test ./linkgate/...` — expect `ok`, unchanged. (Never
   `go test ./...` — it fails by design on `package main`.)
4. Start the app with two domains configured:
   ```bash
   SPIN_VARIABLE_PUBLIC_BASE_URLS="http://localhost:3000,http://127.0.0.1:3000" \
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> \
   SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```
   This is also what confirms a comma-bearing Spin variable value survives the
   env-var provider (listed UNCONFIRMED above).
5. `curl -s localhost:3000/api/auth/me` with a session cookie: `domains` is
   `["http://localhost:3000","http://127.0.0.1:3000"]` in that order and
   `assigned_domains` is `[]`.
6. Log in at `http://localhost:3000/dashboard.html`. The nav shows a domain
   `<select>` next to the theme control, defaulted to `localhost:3000`. Console
   clean.
7. Create a link. The success banner reads
   `http://localhost:3000/r/<slug>`; its Copy button pastes exactly that.
8. Switch the selector to `127.0.0.1:3000`. **Without reloading**, the success
   banner and its Copy output both change to `http://127.0.0.1:3000/r/<slug>`,
   and hovering a table row's slug chip shows the same origin in the tooltip.
9. Export CSV. The `Short link` column carries `http://127.0.0.1:3000/...`.
10. Reload the page, then navigate to Manage users and back. The selector still
    reads `127.0.0.1:3000` — the choice persists across reload and navigation.
11. Open a link's detail page. The `h1` reads
    `http://127.0.0.1:3000/r/<slug>`, the QR preview renders, and both
    Download SVG and Download PNG produce files that **scan to
    `http://127.0.0.1:3000/r/<slug>`** — scan them, do not just download them.
    Switch the selector on this page and confirm the heading and the preview
    both change.
12. **The security check.** With a session cookie:
    ```bash
    curl -si "localhost:3000/api/links/<slug>/qr?base=https://evil.example" | head -1
    ```
    → `HTTP/1.1 400`, body `{"error": "invalid_base_url"}`. Then the same URL
    with `?base=http://127.0.0.1:3000` → `200` and an image. Then no `base`
    parameter at all → `200`, encoding `http://localhost:3000/r/<slug>`.
13. Manage users: create a test user with only `http://127.0.0.1:3000` checked.
    Log in as them — their nav selector offers exactly one domain, so per the
    design it is **hidden**, and every URL they see uses `127.0.0.1:3000`.
14. As admin, edit that user, uncheck everything, save. Log back in as them —
    the selector reappears with both domains.
15. Restart the app with only one domain
    (`SPIN_VARIABLE_PUBLIC_BASE_URLS="http://localhost:3000"`). The nav has no
    domain control and is pixel-identical to before this feature; the
    dashboard, detail page and QR all still work.
16. Restart with a deliberately broken value
    (`SPIN_VARIABLE_PUBLIC_BASE_URLS="not-a-url"`). `/auth/me` returns
    `domains: []`, the GUI falls back to `location.origin` and remains usable,
    and the QR endpoint returns `500 no_base_url_configured` — loud, not a
    localhost QR quietly printed onto paper.
17. Responsive and theme pass, both themes, at 1400 / 768 / 480 / 390px with
    two domains configured: record `scrollWidth` vs `clientWidth` on
    `#app-header nav` at each, and the `getComputedStyle` contrast of the
    select's fill and border against the nav fill. No horizontal overflow
    anywhere; every boundary ratio at or above 3:1.
18. `detect.mjs --json gui/` (the Impeccable mechanical detector, invoked the
    same way previous passes recorded in `TASKS.md` did) — expect the same 2
    known false positives and nothing new.

## Out of scope / follow-ups

- **The destination-URL allow/deny-list.** The second half of the superseded
  Future-work entry — an admin-managed list constraining which domains a link's
  `target_url` may point at. Completely independent of this work (it is a
  content/abuse control, not a display concern) and **remains Future work**; a
  replacement entry narrowed to just that half is added under
  `## Future work (not scheduled)`.
- **Per-link domains and any form of `Host` enforcement.** Non-goals by
  decision, with the reasoning recorded above and under
  `## Considered and rejected`.
- **A KV domain registry or a domain-management admin page.** Non-goal by
  decision.
- **Any change to slug uniqueness.** Slugs stay globally unique.
- **Server-side persistence of the selected domain.** The preference is
  `localStorage`, exactly like the theme, so it does not follow a user between
  browsers. Matching the theme's own documented scope (`PRODUCT.md`:
  "persisted client-side only... no server-side or per-user storage") is the
  point; a per-user server-side preference would need a user-record field, a
  write path and a decision about what happens when it names a domain the user
  is no longer assigned. Added under `## Future work (not scheduled)`; pick it
  up only if someone actually complains about re-choosing on a second machine.
- **Showing which domain a link was created under.** Impossible without
  per-link storage, and per-link storage is the thing this plan rejects.
- **A `Domain` column in the CSV export.** The `Short link` column is a full
  URL and already carries it.
