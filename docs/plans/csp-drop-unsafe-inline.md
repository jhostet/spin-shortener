# Drop `'unsafe-inline'` From The `gui-pages` CSP

## Context

`gui-pages/routing.py`'s `SECURITY_HEADERS` currently ships
`script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'`. That is the
single remaining accepted gap from the 2026-07-25 security-headers pass — every
other directive in that CSP is locked down for real. It is documented in three
places, all of which say the same thing and all of which defer for the same
reason (scope, not disagreement):

- `CLAUDE.md`, "Security response headers" — "the `'unsafe-inline'` is a
  deliberate, disclosed tradeoff, not an oversight."
- `CLAUDE.md`, "Security tradeoffs (accepted for v1)" — "The one remaining
  accepted gap."
- `TASKS.md`, "Future work (not scheduled)" — "Harden `gui-pages`'s CSP by
  dropping `'unsafe-inline'` from `script-src`/`style-src` … A substantial
  refactor — re-confirm scope before starting, don't assume this write-up is a
  complete spec."

`'unsafe-inline'` on `script-src` is the directive that matters. It is the one
that turns any successful HTML-injection into script execution; with it present,
the rest of the policy is mostly defence-in-depth around a hole. This app renders
user-controlled strings (slugs, destination URLs, usernames, referrers) into the
DOM on every page, and while every one of those paths currently goes through
`escapeHtml()` or `textContent`, the CSP is precisely the layer that is supposed
to survive one of them being wrong.

This plan is a re-confirmation of that Future-work entry's scope, not an
acceptance of it. Two of its assumptions turned out to be wrong or incomplete
(the ~700-line figure, and the belief that nonces alone would be sufficient) —
see "Key technical facts confirmed during research".

**Confirmed decisions** (settled by the user before planning):

- Scope is `script-src` and `style-src` only. `img-src 'self' data:` is
  load-bearing (Pico renders sortable-column chevrons, the search icon, and the
  datetime-local calendar icon as inline `data:image/svg+xml` background-images;
  tightening it produced real CSP-violation console errors) and is not reopened.
  No other directive and no other accepted v1 tradeoff is in scope.
- Any new external asset is served by the `gui` static component, which supports
  exact routes only — a wildcard route on it 404s. The plan must state how many
  new routes this adds.
- `gui-pages/routing.py`'s `build_response(uri, read_file)` must keep taking
  `read_file` as an injected callable, so the module stays host-importable and
  unit-testable.
- The nonce-vs-externalization fork needs a recommendation, not a survey.

## Key technical facts confirmed during research

- **The inline code is 925 lines, not ~700.** Measured directly by extracting
  every `<script>` block without a `src=` attribute and every `<style>` block
  from the five served HTML files:

  | File | inline `<script>` | inline `<style>` | static `style=` attrs |
  |---|---|---|---|
  | `gui/index.html` | 4 | 0 | 0 |
  | `gui/login.html` | 16 | 0 | 0 |
  | `gui/dashboard.html` | 379 | 125 | 2 |
  | `gui/admin/users.html` | 227 | 66 | 2 |
  | `gui/links/detail.html` | 93 | 15 | 2 |
  | **Total** | **719** | **206** | **6** |

  The docs' "~700 lines" is very close to the `<script>` subtotal alone (719) and
  omits the 206 lines of `<style>` entirely. Real total: **925**.

- **`style-src` without `'unsafe-inline'` also blocks `style="…"` attributes.**
  Confirmed against MDN's
  [`style-src-attr`](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/style-src-attr)
  page: `style-src-attr` falls back to `style-src`, and with `'unsafe-inline'`
  absent, `<div style="display: inline">` is not applied. The same page confirms
  `setAttribute("style", …)` and `el.style.cssText = …` are also blocked, while
  **`el.style.display = "…"` (a direct CSSOM property write) is explicitly not
  blocked**. This is the fact the `TASKS.md` future-work entry misses: nonces
  cover `<style>` *blocks* only. A `style=` attribute cannot carry a nonce, so
  seven of them have to be removed no matter which approach is chosen.

- **There are 7 static `style=` attributes, not 6** — six in the HTML files
  (counted above) plus one generated inside an `innerHTML` template in
  `gui/app.js:216`
  (`<li id="manage-users-link" style="display: none">`). An `innerHTML`-inserted
  style attribute is checked by CSP exactly like a parsed one, so `gui/app.js`
  is in scope even though it is already an external file.

- **Every other `.style.display` write in the codebase is a direct CSSOM
  property write and is therefore already CSP-safe.** Confirmed by grepping all
  of `gui/` for `style.display`: 20 hits, all of the form
  `el.style.display = "none" / ""`. Two of them (`#admin-content` at
  `gui/admin/users.html:206-207`, and the `tr.edit-row` toggles in
  `dashboard.html`/`users.html`) act on elements that have **no** static
  `style=` attribute, so they need no change at all.

- **There are zero inline event-handler attributes** (`onclick=`, `onsubmit=`,
  …) and zero `javascript:` URLs anywhere in `gui/`. Confirmed by grep. Every
  handler already goes through `addEventListener`. This removes the single
  most painful class of `'unsafe-inline'` dependency before we start.

- **There is no `eval` / `new Function` / `document.write` anywhere in `gui/`,**
  and no `<meta http-equiv="Content-Security-Policy">` competing with the
  header. Confirmed by grep.

- **Externalizing an inline `<script>` to a same-origin `<script src>` in the
  same document position is behaviour-preserving here.** All five inline blocks
  are classic (non-module, non-`defer`, non-`async`) scripts placed immediately
  after the page's `<script src="app.js">`. Classic scripts share one global
  scope and execute in document order, so the page scripts keep seeing
  `api`, `initHeader`, `escapeHtml`, `friendlyError`, `confirmDialog`,
  `copyToClipboard`, `setCsrfToken` exactly as they do today. No hoisting,
  scoping, or ordering change. Confirmed by reading all five files:
  `index.html:6-11`, `login.html:32-49`, `dashboard.html:205-585`,
  `admin/users.html:138-366`, `links/detail.html:89-183`. `index.html`'s block is
  in `<head>` and calls `api.get()` immediately — an external classic script in
  the same position is still fetched and executed synchronously before parsing
  continues, so that also holds.

- **Nested exact routes on the `gui` static component work.**
  `route = "/vendor/pico.min.css"` (`spin.toml:73-75`) is a one-level-nested
  exact route on `spin_static_fs` and serves correctly today — that is the direct
  precedent for `/admin/users.js` and `/links/detail.js`. The confirmed-live
  gotcha (`CLAUDE.md`, `spin.toml:65-72`) is specific to **wildcard** routes on
  that component, not exact ones, and `CLAUDE.md` explicitly anticipates more
  assets being added: "Stick to exact routes for this component if any more
  assets are ever added to it."

- **`gui`'s `files` mapping already covers every new file.** It is
  `{ source = "gui", destination = "/" }` (`spin.toml:88`) — the whole directory.
  New files under `gui/` are already in that component's virtual filesystem; only
  the trigger routes need adding. Do **not** try to narrow the `files` list:
  `spin.toml:79-87` records that a directory source mapped to a non-root
  destination 404'd live.

- **Page-scoped CSS is genuinely page-scoped and safe to relocate verbatim.**
  `dashboard.html`'s block is entirely `#links-table …`, `users.html`'s entirely
  `#users-table …`, and `detail.html`'s entirely `.detail-heading …` (a class
  used on no other page — confirmed by grep). No selector collides across pages,
  so each block can move to its own file with zero edits.

- **UNCONFIRMED — the `Cache-Control` header `spin_static_fs` sends by default.**
  `gui` sets no `CACHE_CONTROL` env var. This is worth a `curl -I` during
  verification, but it is **not a new risk class**: `gui/app.js` and
  `gui/theme.css` are already served by this exact component under these exact
  conditions, so whatever caching behaviour the new files get, the app already
  lives with it for its shared script and stylesheet.

- **UNCONFIRMED — whether `secrets` / `os.urandom` work under
  `componentize-py`.** Only relevant to the nonce approach, which this plan
  rejects. Flagged because it is a live unknown: `CLAUDE.md`'s history records
  `hashlib.pbkdf2_hmac` missing under `componentize-py`, so stdlib availability
  in that runtime is not something to assume. Confirming it would mean building
  `gui-pages` and calling it from `app.py` — work the recommended approach
  avoids entirely.

## The fork: nonces vs. pure externalization

**Recommendation: pure externalization. No nonces, no hashes, no per-response
HTML rewriting.**

The nonce approach requires all of the following, every one of which is a real
cost:

1. `build_response` stops being a pass-through. It would have to do byte-level
   surgery on the served HTML (`body.replace(b"<script>", b'<script nonce="…">')`)
   to inject a fresh nonce into every inline tag. That is string-matching against
   markup, in the one component whose entire job is to be a trustworthy header
   attacher. A page that gets reformatted (`<script >`, an attribute added, a
   `<style>` block gaining a media attribute) silently stops receiving a nonce
   and starts being blocked — a failure that shows up as a dead page in a
   browser, not as a failing test.
2. It needs a CSPRNG in the WASI runtime — see the UNCONFIRMED note above.
3. **It does not solve the problem.** Nonces apply to `<script>` and `<style>`
   *blocks*. The 7 `style="display: none"` attributes cannot carry a nonce and
   would still be blocked, so `style-src 'unsafe-inline'` would have to stay —
   meaning the nonce work buys a hardened `script-src` and leaves `style-src`
   exactly as weak as it is today.
4. It makes every HTML response uncacheable by construction (a cached page
   carries a stale nonce that the fresh header won't match), and adds per-request
   CPU to a Python Wasm component.
5. It leaves 925 lines of JS and CSS inside `.html` files, where no editor,
   linter, or formatter treats them as JS and CSS.

Pure externalization has one genuine cost — **8 new exact routes in
`spin.toml`** — and otherwise strictly dominates: `build_response` stays a
pass-through (the `read_file`-injection testability property is preserved for
free, because that function does not change at all), the assets become
cacheable, both `script-src` and `style-src` reach a clean `'self'`, and the
per-page diffs are mechanical moves that a reviewer can verify by eye.

The 7 `style=` attributes have to be dealt with either way; under
externalization they are the only content edits in the whole change.

## Shared prerequisite: the `[hidden]` attribute utility

The 7 static `style="display: none"` attributes get replaced with the native
`hidden` attribute, and their paired JS toggles switch from `.style.display` to
the `hidden` IDL property (`el.hidden = true/false`). `el.hidden` reflects to a
content attribute that is *not* `style`, so CSP does not touch it, and it is
semantically better than a bespoke class — it is exposed to assistive tech.

This needs one new rule in `gui/theme.css`:

```css
/* The native `hidden` attribute only gets `display: none` from the UA
 * stylesheet, which loses to any author rule that sets `display` — Pico sets
 * `display` on `label`, on `nav li`, and on buttons, all of which are elements
 * this app hides. `!important` is what makes a hidden element actually
 * hidden regardless of what any component rule says. Introduced when the
 * GUI's inline `style="display: none"` attributes were removed so the CSP
 * could drop 'unsafe-inline' from style-src. */
[hidden] {
  display: none !important;
}
```

Nothing in `gui/` uses the `hidden` attribute today (confirmed by grep — the
only hits are `aria-hidden` and the unrelated `.visually-hidden` utility), so
this rule cannot affect any existing element.

The exact conversion list — 7 attributes and their 10 paired JS writes:

| Element | Attribute at | JS writes to convert |
|---|---|---|
| `#manage-users-link` | `gui/app.js:216` (innerHTML template) | `app.js:242` |
| `#custom-slug-field` | `gui/dashboard.html:154` | `dashboard.html:214` |
| `#create-success` (dashboard) | `gui/dashboard.html:177` | `dashboard.html:417, 512, 538` |
| `#forbidden-notice` | `gui/admin/users.html:82` | `users.html:206` |
| `#create-success` (users) | `gui/admin/users.html:114` | `users.html:278, 325, 346` |
| `#detail-copy-btn` | `gui/links/detail.html:32` | `detail.html:108` |
| `#detail-edit-link` | `gui/links/detail.html:33` | `detail.html:126` |

Mapping: `x.style.display = "none"` → `x.hidden = true`;
`x.style.display = ""` → `x.hidden = false`;
`detail.html:126`'s `display = canEdit ? "" : "none"` → `x.hidden = !canEdit`.

**Do not convert the CSSOM-only toggles.** `#admin-content`
(`users.html:207`) and the `tr.edit-row` show/hide logic
(`dashboard.html:388, 423-424, 432`; `users.html:238, 244-245, 263`) have no
static `style=` attribute, are already CSP-safe, and reading
`editRow.style.display === "none"` is load-bearing state in the toggle. Leaving
them alone keeps the diff honest and avoids introducing a bug in a working
toggle for no security gain.

## GUI changes: the 8 new asset files

Every new file is a verbatim move of an existing inline block — same code, same
comments, no reformatting, no refactoring.

| New file | Source | Lines | Replaced in HTML by |
|---|---|---|---|
| `gui/index.js` | `index.html:7-11` | 4 | `<script src="index.js"></script>` |
| `gui/login.js` | `login.html:33-49` | 16 | `<script src="login.js"></script>` |
| `gui/dashboard.css` | `dashboard.html:9-134` | 125 | `<link rel="stylesheet" href="dashboard.css" />` |
| `gui/dashboard.js` | `dashboard.html:206-585` | 379 | `<script src="dashboard.js"></script>` |
| `gui/admin/users.css` | `admin/users.html:9-75` | 66 | `<link rel="stylesheet" href="users.css" />` |
| `gui/admin/users.js` | `admin/users.html:139-366` | 227 | `<script src="users.js"></script>` |
| `gui/links/detail.css` | `links/detail.html:9-24` | 15 | `<link rel="stylesheet" href="detail.css" />` |
| `gui/links/detail.js` | `links/detail.html:90-183` | 93 | `<script src="detail.js"></script>` |

Placement rules, which matter:

- Each `<link>` goes **immediately after** the page's existing `theme.css`
  `<link>`, preserving cascade order (the page rules currently come after
  `theme.css` and several of them depend on that).
- Each `<script src>` goes **exactly where the inline block was** — immediately
  after the page's `<script src="app.js">` — and carries no `defer`/`async`/
  `type="module"`. Adding any of those would change execution timing.
- `admin/users.html` and `links/detail.html` reference their siblings with plain
  relative paths (`users.css`, `detail.js`), matching how they already reference
  `../app.js` and `../theme.css`.

## `spin.toml` changes: 8 new exact routes

**This change adds exactly 8 new `[[trigger.http]]` entries, all on the `gui`
component.** `gui` goes from 3 trigger routes to 11; the application goes from 6
HTTP triggers to 14.

```toml
[[trigger.http]]
route = "/index.js"
component = "gui"

[[trigger.http]]
route = "/login.js"
component = "gui"

[[trigger.http]]
route = "/dashboard.js"
component = "gui"

[[trigger.http]]
route = "/dashboard.css"
component = "gui"

[[trigger.http]]
route = "/admin/users.js"
component = "gui"

[[trigger.http]]
route = "/admin/users.css"
component = "gui"

[[trigger.http]]
route = "/links/detail.js"
component = "gui"

[[trigger.http]]
route = "/links/detail.css"
component = "gui"
```

Notes for whoever writes these:

- **Exact routes only.** No `/...` wildcard, not even a tempting
  `/admin/...`. `spin.toml:65-72` and `CLAUDE.md` both record that wildcards on
  this component 404 live once it has more than one route.
- `[component.gui]`'s `files` list is **not** touched. It already maps all of
  `gui/`.
- Spin resolves routing by specificity, not declaration order, so these exact
  routes correctly beat the `gui-pages` `/...` catch-all — the same mechanism the
  existing 3 routes already rely on.
- Extend the existing explanatory comment above the `gui` routes rather than
  leaving 8 uncommented entries; the reason this component has a long list of
  exact routes instead of one wildcard is exactly the kind of thing the next
  person will otherwise "clean up".

## `gui-pages` changes

Two changes, both in `gui-pages/`. Neither touches `build_response`'s signature
or its `read_file` injection — that function's body does not change at all.

**1. `gui-pages/routing.py` — the CSP itself.** `script-src`/`style-src` drop
`'unsafe-inline'`, and the long comment above `SECURITY_HEADERS` explaining the
tradeoff is replaced with one recording that inline code was externalized:

```python
    "content-security-policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        # (existing img-src comment about Pico's data: background-images
        #  stays exactly as-is — unchanged and still load-bearing)
        "img-src 'self' data:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
```

`script-src 'self'` and `style-src 'self'` are technically redundant under
`default-src 'self'`. Keep them stated explicitly anyway: they are the two
directives this whole change exists to tighten, and stating them means a future
loosening of `default-src` cannot silently loosen them too.

**2. `gui-pages/tests/test_no_inline_code.py` (new) — the regression guard.**
Without this, the CSP is a promise enforced by nothing, and the next person to
add a quick inline `<script>` to `dashboard.html` finds out via a dead page in
production. The test derives its file list from `routing.ROUTES` so it cannot
drift, and reads real files from `gui/` (which is fine for a host test — the
constraint that matters is that `routing.py` itself stays free of filesystem
access, and it does):

```python
GUI_DIR = Path(__file__).resolve().parents[2] / "gui"
```

It should parametrize over `sorted(set(ROUTES.values()))` and assert, per file:

- no `<script>` tag without a `src=` attribute,
- no `<style>` tag at all,
- no `style="` attribute,
- no `on<event>=` handler attribute.

`gui/app.js` is not in `ROUTES`, so add one non-parametrized test asserting the
same `style="` and `<script>`-template rules against `gui/app.js` specifically —
that is where the seventh style attribute lived, and it is the file most likely
to regrow one.

Also extend `test_routing.py`'s existing `test_resolve_file` parametrize list —
which already asserts `("/app.js", None)` and `("/vendor/pico.min.css", None)` —
with `("/dashboard.js", None)` and `("/admin/users.css", None)`, pinning the
fact that the new assets are served by `gui`, not by this component.

## Documentation changes

`CLAUDE.md` is currently wrong in three places the moment this lands, so
updating it is a task, not an afterthought:

- **"Security response headers"** — the `gui-pages` bullet's entire
  `'unsafe-inline'` justification paragraph must go, replaced with the real
  policy (`script-src 'self'; style-src 'self'`) and a note that each page's
  script and style live in a sibling `.js`/`.css` file served by `gui`. The
  `img-src 'self' data:` paragraph stays verbatim.
- **"Security tradeoffs (accepted for v1)"** — the third bullet's "The one
  remaining accepted gap: `gui-pages`'s CSP allows `'unsafe-inline'` …" sentence
  is now false and must be removed. Note that the security-headers bullet then
  has no remaining caveat at all.
- **"Architecture"** — the `gui` component bullet says it serves "only these 3
  exact routes"; it now serves 11. The wildcard gotcha note stays and gets more
  important, not less, with 11 routes on that component.

`DESIGN.md` and `.impeccable/design.json` need **no** change: no CSS rule's
content, selector, or specificity changes (only its file location), and
`design.json` contains no HTML file paths. The one genuinely new CSS rule
(`[hidden]`) is a mechanical utility with no visual design content — the
existing `.visually-hidden` utility in `theme.css` is likewise undocumented in
`DESIGN.md`, so this follows precedent rather than skipping something.

`Jenkinsfile` needs no change: the new test file lands under `gui-pages/tests/`,
already covered by that stage's `uv run pytest`.

## One change or a sequence?

**A sequence of 8 commits, not one change.** Three reasons, in order of weight:

1. **The failure mode of a missing route is silent and total.** If
   `/links/detail.js` 404s — the one genuinely uncertain thing here, given this
   component's documented path-resolution weirdness — the page renders its full
   HTML shell and then simply does nothing, with one console 404. In a single
   925-line, 8-route, 5-page diff that is a bisect. As the 4th of 8 commits it is
   a two-line fix. This is why `links/detail.html` (the *smallest* nested page)
   is sequenced before the two large nested pages: it puts the riskiest unknown
   behind the cheapest possible diff.
2. **Every intermediate state is fully working and shippable.** The CSP still
   carries `'unsafe-inline'` throughout steps 1-6, so nothing can break from a
   half-finished migration — externalizing a page is invisible to the policy.
   The security posture improves in exactly one commit, atomically, at the end,
   when every page is already clean.
3. **The per-page diffs are reviewable as pure moves.** A reviewer can confirm
   "these 379 lines left `dashboard.html` and arrived in `dashboard.js`
   unchanged" at a glance. Bundled together, that property is lost.

The ordering constraints are real and must be respected:

- **Step 1 (`[hidden]` utility + `app.js`) must land first.** `app.js` is loaded
  by all five pages, so its inline style attribute would violate `style-src` from
  any page; and the `[hidden]` rule it depends on must exist before any page
  starts using the attribute.
- **Steps 2-6 (one per page) may land in any order after step 1**, but the
  recommended order is smallest-and-riskiest-first: `index.html` (4 lines,
  proves the root-level route pattern) → `login.html` (16 lines, proves an
  unauthenticated page) → `links/detail.html` (108 lines, **proves the nested
  exact route**) → `admin/users.html` (293 lines) → `dashboard.html` (504 lines,
  the largest surface).
- **Step 7 (guard test + CSP flip) requires all of 1-6.** The guard test fails
  until the last page is clean, and the CSP flip breaks any page that isn't.
- **Step 8 (`CLAUDE.md`) must land with or immediately after step 7**, never
  before — it would otherwise document a policy that isn't deployed.

## Trade-offs and rejected alternatives

- **Per-request nonce injection in `gui-pages`.** Attractive because it needs
  zero new routes and zero new files, and because it is the approach the
  `TASKS.md` future-work entry reaches for first. Lost because it does not
  actually finish the job (nonces cannot cover the 7 `style=` attributes, so
  `style-src 'unsafe-inline'` would have to stay), it requires `build_response`
  to do byte-level markup rewriting in the component whose one job is
  trustworthy headers, it depends on an unconfirmed CSPRNG under
  `componentize-py`, it makes every HTML response uncacheable, and it leaves 925
  lines of JS/CSS stranded inside `.html` files. It buys half the security win
  for more ongoing fragility.

- **Hashes (`'sha256-…'`) instead of nonces.** Attractive because they need no
  per-request work at all — the CSP is still a static string, so
  `build_response` stays a pass-through and `gui-pages` gains no runtime logic.
  Lost because the hash must be recomputed and the CSP string edited by hand on
  every single edit to any inline block, with the failure mode being a page that
  silently stops executing. That is a permanent tax on GUI work in exchange for
  avoiding a one-time file move, and like nonces it still can't cover the
  `style=` attributes.

- **Serving the new `.js`/`.css` from `gui-pages` instead of `gui`.** Genuinely
  attractive: `gui-pages` already owns the `/...` catch-all, so this would add
  **zero** `spin.toml` routes and would express the whole asset list as new
  `ROUTES` entries in an already-host-tested module. Lost on two counts. First,
  it routes every subresource request through a `componentize-py` Python Wasm
  component instead of the purpose-built static fileserver, adding a Python
  component instantiation per asset per page load — a real regression for zero
  benefit, and it would need a new content-type map in `routing.py` (the
  component currently hardcodes `text/html`) purely to serve files it has no
  business serving. Second, it inverts the architecture the security-headers
  pass deliberately established: `gui-pages` exists to serve **navigated
  documents that need headers**, `gui` exists to serve **static subresources
  that don't**. `CLAUDE.md` already sanctions the chosen path in as many words:
  "Stick to exact routes for this component if any more assets are ever added
  to it."

- **Appending the three page CSS blocks to `theme.css` instead of creating
  three new files.** Attractive: saves 3 of the 8 new routes (down to 5), since
  `/theme.css` is already routed, and all three blocks are already
  ID/class-scoped tightly enough (`#links-table`, `#users-table`,
  `.detail-heading`) that no selector would collide. Lost because it makes every
  page download 206 lines of rules that apply only to one other page, and
  because `theme.css` is the design-system file that `DESIGN.md` describes —
  merging three pages' worth of table-layout workarounds into it blurs the one
  file whose contents are supposed to be shared vocabulary. Route entries are
  cheap and inert; the muddied boundary is not. Revisit only if the
  `[[trigger.http]]` list becomes genuinely unmanageable.

- **A `.is-hidden` utility class instead of the native `hidden` attribute.**
  Functionally equivalent and CSP-safe either way. `hidden` won because it is
  the platform's own answer, needs no invented vocabulary, exposes the state to
  assistive technology, and is toggled through a plain IDL property
  (`el.hidden = true`) that reads better than `classList.toggle(...)` at the
  call sites. The `!important` caveat applies identically to both.

- **Do nothing (keep `'unsafe-inline'`).** This was a live option — it has been
  the accepted, disclosed position since 2026-07-25, and nothing has broken. It
  lost because the actual work turned out to be much smaller than the docs
  imply: there are no inline event handlers, no `eval`, no `document.write`, and
  every inline block is a classic end-of-body script that moves to an external
  file with zero code changes. The barrier was believed to be a refactor; it is
  in fact a file move plus 7 attribute conversions. That changes the
  cost/benefit enough to act.

- **Converting every `.style.display` write in the codebase, not just the 7
  paired ones.** Attractive for consistency — one hiding idiom everywhere.
  Rejected because the other writes are already CSP-safe (direct CSSOM property
  writes are explicitly not blocked, per MDN), and because
  `editRow.style.display === "none"` is read as state by the edit-row toggles in
  both tables. Rewriting working toggles inside a security change adds
  regression risk for zero security gain. If a consistency cleanup is wanted, it
  is separate work.

## Tasks

The lines appended to `TASKS.md` under a new `## CSP hardening — drop
'unsafe-inline'` heading:

```
- [ ] Add the `[hidden]` display utility and convert `app.js`'s inline style attribute (must land before every other task in this section) — file(s): gui/theme.css, gui/app.js — done when: `theme.css` has a `[hidden] { display: none !important; }` rule (with a comment explaining that the UA stylesheet's `display:none` loses to Pico's `display` rules on `label`/`nav li`/buttons); `app.js:216`'s `<li id="manage-users-link" style="display: none">` uses `hidden` instead and `app.js:242` sets `.hidden = false`; grep confirms zero `style="` occurrences remain in `gui/app.js`; verified live that the "Manage users" nav link is still absent for a non-admin, still present for an admin, and still absent for an admin already on `admin/users.html`.
- [ ] Externalize `index.html`'s inline script — file(s): gui/index.html, gui/index.js (new), spin.toml — done when: `index.html:7-11`'s 4-line block is moved verbatim to `gui/index.js`, loaded via `<script src="index.js"></script>` in the same `<head>` position immediately after `app.js` with no `defer`/`async`; a `route = "/index.js"` exact trigger is added for the `gui` component; `curl -I http://localhost:3000/index.js` returns 200; loading `/` still redirects to `login.html` when logged out and `dashboard.html` when logged in, with zero console errors.
- [ ] Externalize `login.html`'s inline script — file(s): gui/login.html, gui/login.js (new), spin.toml — done when: `login.html:33-49` is moved verbatim to `gui/login.js`, loaded via `<script src="login.js"></script>` in the same end-of-body position immediately after `app.js`; a `route = "/login.js"` exact trigger is added; a real login succeeds and a wrong password still shows the friendly "Incorrect username or password." error, with zero console errors.
- [ ] Externalize `links/detail.html`'s inline script and style (sequenced here deliberately — smallest nested page, proves the nested exact-route pattern before the two large ones) — file(s): gui/links/detail.html, gui/links/detail.js (new), gui/links/detail.css (new), spin.toml — done when: `detail.html:9-24` moves verbatim to `detail.css` (linked immediately after `../theme.css`) and `detail.html:90-183` to `detail.js` (loaded immediately after `../app.js`); the 2 `style="display: none"` attributes on `#detail-copy-btn`/`#detail-edit-link` become `hidden` and `detail.html:108`/`126` set `.hidden = false` / `.hidden = !canEdit`; `route = "/links/detail.js"` and `route = "/links/detail.css"` exact triggers are added; both `curl -I` as 200; a link's detail page renders the QR code, totals, per-day and recent-events tables with the `.detail-heading` layout visually unchanged, Copy works, and Edit is visible for an owner/admin and hidden for a `links.view_all`-only user — zero console errors in both cases.
- [ ] Externalize `admin/users.html`'s inline script and style — file(s): gui/admin/users.html, gui/admin/users.js (new), gui/admin/users.css (new), spin.toml — done when: `users.html:9-75` moves verbatim to `users.css` (linked immediately after `../theme.css`) and `users.html:139-366` to `users.js` (loaded immediately after `../app.js`); the 2 `style="display: none"` attributes on `#forbidden-notice`/`#create-success` become `hidden` with `users.html:206`, `278`, `325`, `346` converted to `.hidden = true/false`; `route = "/admin/users.js"` and `route = "/admin/users.css"` exact triggers are added; both `curl -I` as 200; creating a user shows the green success message, the admin's own row still hides Delete, the admin-promotion confirm dialog still fires, the sticky action column still pins at 390px, and a non-admin without `users.manage` still sees the forbidden notice with `#admin-content` hidden — zero console errors.
- [ ] Externalize `dashboard.html`'s inline script and style — file(s): gui/dashboard.html, gui/dashboard.js (new), gui/dashboard.css (new), spin.toml — done when: `dashboard.html:9-134` moves verbatim to `dashboard.css` (linked immediately after `theme.css`) and `dashboard.html:206-585` to `dashboard.js` (loaded immediately after `app.js`); the 2 `style="display: none"` attributes on `#custom-slug-field`/`#create-success` become `hidden` with `dashboard.html:214`, `417`, `512`, `538` converted to `.hidden = true/false`; `route = "/dashboard.js"` and `route = "/dashboard.css"` exact triggers are added; both `curl -I` as 200; create-a-link (including the "More options" panel, custom slug field for a permitted user, and the success banner with its Copy button), column sort, the filter box, the edit-row toggle, delete-with-confirm-dialog, the sticky action column at 1400px and 390px, and the `?edit=<slug>` deep link all still work — zero console errors.
- [ ] Drop `'unsafe-inline'` from the CSP and add the inline-code regression guard (requires all 6 tasks above) — file(s): gui-pages/routing.py, gui-pages/tests/test_no_inline_code.py (new), gui-pages/tests/test_routing.py — done when: `SECURITY_HEADERS`' CSP reads `script-src 'self'; style-src 'self'` with the old `'unsafe-inline'` justification comment replaced and `img-src 'self' data:` and its comment untouched; `build_response`'s signature and body are unchanged (still takes the injected `read_file`); the new test parametrizes over `sorted(set(routing.ROUTES.values()))` and asserts each file has no srcless `<script>`, no `<style>`, no `style="`, and no `on<event>=` handler, plus a separate assertion covering `gui/app.js`; `test_routing.py`'s `test_resolve_file` list gains `("/dashboard.js", None)` and `("/admin/users.css", None)`; `cd gui-pages && uv run pytest` passes; `curl -sI http://localhost:3000/dashboard.html | grep -i content-security-policy` shows no `'unsafe-inline'`.
- [ ] Update `CLAUDE.md` for the new CSP and route count — file(s): CLAUDE.md — done when: the "Security response headers" `gui-pages` bullet states `script-src 'self'; style-src 'self'` and drops the `'unsafe-inline'` justification paragraph (keeping the `img-src` one verbatim); the "Security tradeoffs (accepted for v1)" security-headers bullet no longer claims `'unsafe-inline'` is a remaining gap; the Architecture section's `gui` bullet says 11 exact routes instead of 3 and keeps the wildcard-404 gotcha; and the `TASKS.md` "Future work" entry for this item is left untouched (the builder does not edit existing TASKS.md lines).
- [ ] End-to-end manual verification of the hardened CSP — file(s): (none — verification step) — done when: with `spin up --build --runtime-config-file runtime-config.toml` running, all five pages (`/`, `/login.html`, `/dashboard.html`, `/admin/users.html`, `/links/detail.html?slug=<slug>`) are loaded in a real browser with the console open and show **zero** CSP-violation errors and zero other errors; a full flow (log in → create a link → sort → filter → open an edit row → save → open the detail page → create a user → delete it → log out) completes normally; `curl -sI` on each of the 8 new asset routes returns 200 with a sensible content-type; `detect.mjs --json gui/` is unchanged (same 2 known false positives); `cd api && uv run pytest`, `cd gui-pages && uv run pytest`, and `cd redirect && go test ./linkgate/...` all pass.
```

## Critical files

- `gui/theme.css`
- `gui/app.js`
- `gui/index.html`
- `gui/index.js` (new)
- `gui/login.html`
- `gui/login.js` (new)
- `gui/dashboard.html`
- `gui/dashboard.js` (new)
- `gui/dashboard.css` (new)
- `gui/admin/users.html`
- `gui/admin/users.js` (new)
- `gui/admin/users.css` (new)
- `gui/links/detail.html`
- `gui/links/detail.js` (new)
- `gui/links/detail.css` (new)
- `spin.toml`
- `gui-pages/routing.py`
- `gui-pages/tests/test_routing.py`
- `gui-pages/tests/test_no_inline_code.py` (new)
- `CLAUDE.md`

Not touched, deliberately: `redirect/` (its own stricter CSP already has no
`'unsafe-inline'` on `script-src`), `api/` (`default-src 'none'`), `Jenkinsfile`
(the new test lands in an existing `testpaths` directory), `DESIGN.md`,
`.impeccable/design.json`.

## Verification

Run in this order.

1. After **each** of the per-page tasks, confirm the page is genuinely clean —
   these must all print `0`:

   ```bash
   cd /Users/jhostetler/git/tirerack/spin-shortener
   grep -c '<script>' gui/dashboard.html gui/index.html gui/login.html gui/admin/users.html gui/links/detail.html
   grep -c '<style>' gui/dashboard.html gui/admin/users.html gui/links/detail.html
   grep -c 'style="' gui/*.html gui/admin/*.html gui/links/*.html gui/app.js
   ```

2. The Python suites — the `gui-pages` one is the meaningful signal here, and
   `api`'s should be untouched at 135:

   ```bash
   cd gui-pages && uv run pytest
   cd ../api && uv run pytest
   ```

3. The Go suite, to prove nothing in this change reached the redirect component:

   ```bash
   cd redirect && go test ./linkgate/...
   ```

   Never `go test ./...` / `go build ./...` / `go vet ./...` — they fail by
   design on `package main`.

4. Run the real app (the user runs this; the builder does not):

   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```

5. Confirm every new route actually resolves — this is the step that catches the
   `spin_static_fs` path-resolution gotcha, and a 404 here means the route, not
   the file:

   ```bash
   for p in /index.js /login.js /dashboard.js /dashboard.css \
            /admin/users.js /admin/users.css /links/detail.js /links/detail.css; do
     printf '%s -> ' "$p"; curl -s -o /dev/null -w '%{http_code} %{content_type}\n' \
       "http://localhost:3000$p"
   done
   ```

   Pass: eight `200`s. Also worth a one-off `curl -I http://localhost:3000/app.js`
   to see what `Cache-Control` `spin_static_fs` sends by default (the
   UNCONFIRMED item above) — informational, not a gate.

6. Confirm the header itself changed:

   ```bash
   curl -sI http://localhost:3000/dashboard.html | grep -i content-security-policy
   ```

   Pass: `script-src 'self'; style-src 'self'` with no `'unsafe-inline'`
   anywhere, and `img-src 'self' data:` still present.

7. **Load every page in a real browser with the console open.** This is the only
   step that can catch a CSP violation, and it is not optional — the
   `img-src`/`data:` regression during the original security-headers pass was
   found exactly this way and by no other means. Visit `/`, `/login.html`,
   `/dashboard.html`, `/admin/users.html`, and a real
   `/links/detail.html?slug=<slug>`. Pass: zero console messages of any kind,
   in particular nothing matching "Refused to apply inline style" or "Refused to
   execute inline script". Then walk the full flow: log in → create a link (with
   "More options" expanded, a custom slug, and a time window) → sort a column →
   filter → open an edit row, save, cancel → open the link's detail page (QR
   image renders real pixels, per-day and recent-events tables populate) → go to
   Manage users → create a user → confirm the admin-promotion dialog fires →
   delete the user → log out. Re-check at 390px that both sticky action columns
   still pin.

8. Confirm the design detector is unchanged:

   ```bash
   detect.mjs --json gui/
   ```

   Pass: same 2 known false positives as every prior pass, no new findings.

## Out of scope / follow-ups

- **Every other CSP directive**, and `img-src 'self' data:` in particular. Not
  reopened, per the confirmed scope. The `data:` allowance is load-bearing for
  Pico's chevron/search/calendar affordances.
- **The other accepted v1 tradeoffs** — no brute-force rate limiting, enumerable
  slugs, the lossy recent-events ring buffer, reversible link passwords, the
  Akamai single-`"default"`-store blocker. Untouched.
- **`redirect`'s and `api`'s CSPs.** `redirect`'s password-prompt page already
  has `script-src 'none'`; its `style-src 'unsafe-inline'` exists for one
  `style="color: red"` attribute in `prompt.html`. That is a genuinely tiny,
  separate cleanup (delete the attribute, add a class to the page's
  stylesheet — except the prompt page has no external stylesheet, so it would
  need one plus a route on a component with `allowed_outbound_hosts = []`, which
  is more work than it sounds). Not worth bundling into a GUI change; **worth a
  `TASKS.md` "Future work (not scheduled)" entry** if anyone wants the whole app
  free of `'unsafe-inline'`. `api` is already `default-src 'none'`.
- **A `Cache-Control` policy for `gui`'s static assets.** Externalization makes
  the page scripts cacheable, which is a benefit, but there is no cache-busting
  scheme (no content hashes in filenames), so a deployed change to
  `dashboard.js` could be served stale. This is not new — `app.js` and
  `theme.css` have had exactly this property since day one — but it now applies
  to five more files. If it ever bites, `spin_static_fs` exposes a
  `CACHE_CONTROL` env var, which is the smallest available lever.
- **Normalizing the remaining `.style.display` toggles to `hidden`.** Deferred
  on purpose (see the rejected alternative). A consistency cleanup, not a
  security one.
- **Extending the inline-code guard test to `redirect/prompt.html`.** The guard
  added here covers only the files in `gui-pages`' `ROUTES`. Doing the Go
  component's page too would make sense as part of the `redirect` CSP cleanup
  above, not before it.
