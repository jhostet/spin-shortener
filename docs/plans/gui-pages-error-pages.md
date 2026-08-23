# Styled Error Pages for the `gui-pages` Catch-All

## Context

`gui-pages/routing.py`'s `build_response` answers two things with a raw byte
string on a `text/plain` response:

- `Response(404, ..., b"Not found")` — any path outside `ROUTES` (line 91)
- `Response(500, ..., b"Internal error")` — a `ROUTES`-vs-filesystem drift, i.e.
  `read_file` raising `OSError` (line 102)

**A visitor who drops the `/r/` from a shared short URL lands on the first one.**
That is the same defect `docs/plans/redirect-error-pages.md` just removed from
`/r/{slug}` (shipped and deployed as `7ce879a-errpages`), one route over — and it
is the exact scenario `TASKS.md`'s Future-work entry (line 472, raised
2026-08-23) names. That entry is the brief; its trigger ("the next time a
visitor-facing surface is reviewed") has fired, and the session handoff's "What
to pick up next" list names it second.

This plan is deliberately small: two one-line returns in a 105-line module. It
does not re-derive the component's architecture and does not touch `redirect`.

**Confirmed decisions (settled by the user before planning):**

- Reuse `/vendor/pico.min.css` and `/theme.css`, already exact routes on the
  `gui` static component. No new `spin.toml` route, no new served asset file, no
  new design token.
- No inline `<script>`, `<style>` or `style="…"` anywhere. The app has zero
  `'unsafe-inline'` and that stays true.
- The 500 branch must stay defensive: no unguarded read, `SECURITY_HEADERS`
  never dropped.
- `routing.py` keeps zero `spin_sdk` imports and stays fully host-testable.
- No change to `ROUTES`, no new page in the app, no deploy.

**The four decisions this plan makes, each argued below:**

1. **The markup is an inlined `bytes` constant, not a file fetched through
   `read_file`** — a new pure module `gui-pages/errorpages.py`.
2. **The copy differs from `redirect`'s and is deliberately more specific.** This
   404 means "no such page", and `/r/`'s indistinguishability constraint does
   **not** apply here.
3. **`test_no_inline_code.py` does *not* pick the markup up automatically** and
   must be widened — it reaches the bytes by `import`, not by reading a file.
4. **The responses keep `gui-pages`' existing `SECURITY_HEADERS` verbatim** — no
   tightened per-page CSP — and, consequently, **these pages *do* load
   `/theme-init.js`**, diverging from `redirect`'s always-light error pages on
   purpose.

## Key technical facts confirmed during research

- **The two returns and their content types.** `gui-pages/routing.py:91` and
  `:102`, both `{**SECURITY_HEADERS, "content-type": "text/plain; charset=utf-8"}`.
  Read directly.
- **Nothing consumes either body.** `grep -rn "Not found"` across `*.sh`, `*.py`,
  `*.js`, `*.md` (excluding `.claude/worktrees`) matches only `routing.py` itself
  and the two `TASKS.md` entries. No `dev/*.sh` script, no test, and no `gui/*.js`
  reads them, so changing the content type from `text/plain` to `text/html` breaks
  no caller.
- **`SECURITY_HEADERS`' CSP already permits everything these pages need**:
  `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:;
  object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'`
  (`routing.py:48-69`). Same-origin stylesheets, a same-origin script and a
  same-origin favicon are all allowed with no change.
- **Every asset these pages reference is already an exact route on `gui`:**
  `/theme.css` (`spin.toml:74`), `/vendor/pico.min.css` (`:86`),
  `/theme-init.js` (`:103`), `/favicon.svg` (`:150`). Confirmed by
  `grep -n 'route = ' spin.toml`. **So the error page's own subresources can never
  recurse into the error page** — they never reach the `/...` catch-all.
- **Absolute paths with a leading slash are mandatory here, and the trap is worse
  than `redirect`'s.** Every real page in this component uses *relative*,
  depth-aware paths: `gui/login.html` links `theme-init.js`/`theme.css`, while
  `gui/admin/users.html` and `gui/links/detail.html` link `../theme-init.js`/
  `../theme.css`. An error page is served at **arbitrary** depth (`/nope`,
  `/admin/nope`, `/links/nope`), so a relative path would resolve to
  `/admin/theme.css` — not a route on `gui`, so the catch-all would answer it with
  the *error page itself* under `content-type: text/html`, which
  `X-Content-Type-Options: nosniff` then correctly refuses to apply as CSS. The
  failure is a silently unstyled page at depth only.
- **`.auth-page` / `.auth-shell` already exist and are generic.**
  `gui/theme.css:957` (`body.auth-page { min-height: 100vh; display: flex;
  align-items: center; }`) and `:962` (`.auth-shell { max-width: 26rem; }`). The
  four `redirect`-served pages already reuse them, so **no `gui/` file is edited
  and no new token is introduced.**
- **The redirect error pages are the pattern.** `redirect/error-404.html` /
  `error-500.html` and `redirect/errorpage.go` — read in full. Shell:
  `<body class="auth-page"><main class="container auth-shell"><article><hgroup>
  <h1>…</h1><p>lead</p></hgroup><p>detail</p></article></main></body>`. Their
  `<title>`s carry **no `— spin-shortener` suffix**; that was a deliberate change
  recorded in `TASKS.md`'s shipped task-1 note (multi-domain display means these
  render under whatever branded domain the link was shared on, so the internal
  product name is off-brand and a small infra disclosure).
- **`test_no_inline_code.py` derives its lists from `ROUTES` and from
  `redirect/*.html`.** `PAGES = sorted(set(ROUTES.values()))`,
  `REDIRECT_TEMPLATES = sorted(REDIRECT_DIR.glob("*.html"))`. **A page that is
  neither in `ROUTES` nor a `redirect/*.html` file is covered by neither list** —
  confirmed by reading the file. Baseline: `cd gui-pages && uv run pytest -q` →
  **96 passed**.
- **`gui-pages/pyproject.toml` sets `pythonpath = ["."]`**, so a new sibling
  module (`errorpages.py`) is importable from `tests/` with no `conftest.py` and
  no packaging change. There is no `conftest.py` in `gui-pages/` today.
- **`app.py` passes a real `_read_file` that opens `/gui/<relative_path>`**, the
  `files = [{ source = "gui", destination = "/gui" }]` mount (`spin.toml:180`).
  It is excluded from pytest and cannot be imported on the host.
- **`redirect/error-404.html`'s guard needs a comment-stripping regex** because
  the guard is deliberately dumb about HTML comments and that file's comment
  legitimately names all eight forbidden words (recorded in `TASKS.md`'s shipped
  task-3 note). An inlined Python constant sidesteps this entirely: explanatory
  prose lives in `#` comments, outside the guarded bytes.
- **UNCONFIRMED: whether componentize-py can bundle a non-`.py` data file into
  the component at all.** Only `spin.toml`'s `files` mapping is a runtime
  filesystem, and it maps `gui/`, so an HTML file placed in `gui-pages/` would not
  be readable at runtime. To confirm, one would have to put a file there and try
  to open it from a built component — not worth doing, because this plan does not
  need it (see Trade-offs #1). **This is the fact that makes "embed like
  `go:embed`" unavailable in Python.**
- **UNCONFIRMED: whether a mid-run rename under `gui/` is visible to `gui-pages`
  without restarting `spin up`.** Spin maps the host directory as a preopened
  dir, so a read at request time should see the rename, but the `gui` static
  component is documented to serve a startup snapshot and it is not worth
  assuming the two behave alike. Verification below says to try it without a
  restart and restart if the response is still `200`.

## `gui-pages` changes

Everything lands in the **`gui-pages` component (Python)**, which follows the
language rule rather than bending it: this is not the redirect hot path, it is
the catch-all page server, and the component already owns every HTML response it
returns.

### New: `gui-pages/errorpages.py`

A pure module, zero `spin_sdk` imports, host-importable — the same contract
`routing.py` holds. It renders both pages **at import time** from one shared
shell, so there is exactly one `<head>` block in the repo for the two of them
(strictly less duplication than `redirect`'s three near-identical files).

Published surface:

```python
def _render(title: str, heading: str, lead: str, detail: str) -> bytes: ...

NOT_FOUND_HTML: bytes          # rendered at import
INTERNAL_ERROR_HTML: bytes     # rendered at import
ERROR_PAGES: dict[int, bytes] = {404: NOT_FOUND_HTML, 500: INTERNAL_ERROR_HTML}
```

`ERROR_PAGES` is the derivation surface the guard iterates, so a third page added
later is covered the moment it exists — matching `PAGES`/`SCRIPTS`/
`REDIRECT_TEMPLATES`' own derived-not-hardcoded idiom.

The shell, exactly (the four leading slashes are load-bearing — see the
arbitrary-depth fact above):

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <script src="/theme-init.js"></script>
  <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
  <link rel="stylesheet" href="/vendor/pico.min.css" />
  <link rel="stylesheet" href="/theme.css" />
</head>
<body class="auth-page">
  <main class="container auth-shell">
    <article>
      <hgroup>
        <h1>{heading}</h1>
        <p>{lead}</p>
      </hgroup>
      <p>{detail}</p>
    </article>
  </main>
</body>
</html>
```

- **Interpolation is from module constants only.** No request data ever reaches
  `_render` — not the requested path, not a status code, not a header. There is
  no injection point, which is also the argument that settles the CSP question
  below.
- `<script src="/theme-init.js">` is first in `<head>`, before any stylesheet,
  matching every other page in this component and CLAUDE.md's Theming section
  ("loaded render-blocking as the first real element of `<head>`, before any
  stylesheet link").
- `/favicon.svg` is included because every other page in this component has it,
  it is already an exact route, and `img-src 'self'` already permits it — which
  also stops a browser's automatic `/favicon.ico` probe being the only favicon
  request the page generates.

**The copy** (question 2, decided):

| | 404 | 500 |
|---|---|---|
| `title` / `heading` | `Page not found` | `Something went wrong` |
| lead | `There&rsquo;s no page at this address.` | `This page couldn&rsquo;t be loaded.` |
| detail | `If you were following a short link, check the address you were given &mdash; a short link&rsquo;s path starts with <code>/r/</code>.` | `Reloading is unlikely to help &mdash; let whoever runs this service know.` |

Copy rules, and how they differ from `redirect`'s:

- **This 404 may be specific, and that is the point.** `redirect`'s 404 must read
  identically for an absent slug, a disabled link and a link outside its
  `[start_at, end_at)` window, because the difference is a probing signal
  (CLAUDE.md, "Security tradeoffs"; pinned by
  `test_error_404_copy_does_not_distinguish_why_the_slug_is_unavailable`).
  **`gui-pages` has no link-existence secret to protect** — a path is either in a
  fixed, public, hardcoded `ROUTES` allowlist or it is not, and that allowlist is
  in the repo. So this page names the single most likely cause (a short link
  missing its `/r/`) and tells the visitor what to check.
  **Do not add a forbidden-word guard to these pages, and do not relax
  `redirect`'s.** The two constraints are not the same constraint and must not be
  harmonised in either direction.
- **No diagnostics.** No requested path echoed back, no status code, no error id,
  no host message — same rule as `redirect`'s pages, for the same reason (nothing
  to interpolate means no path-input-into-HTML surface at all).
- **No "we've been notified".** It would be a lie: `gui-pages` has no logging
  whatsoever (CLAUDE.md, "Toggleable structured logging": instrumentation is
  `redirect` and `api` only), so a drift-induced 500 produces no signal anywhere.
  The 500's copy points the visitor at the operator for exactly that reason.
- **The 500 does not say "try again".** A `ROUTES`-vs-filesystem drift is
  permanent until a redeploy. "Unlikely to help" is the honest phrasing and
  matches the reasoning `redirect`'s 500 used to avoid a retry prompt.
- **Nothing in `danger-red`.** The `h1` uses the default heading treatment (Deep
  Navy). DESIGN.md's existing "Redirect error pages" note states the rule: red is
  reserved for "something just failed", and a whole page whose only content is an
  error message is standing furniture.
- **No links and no navigation.** See Trade-offs #4.
- **No `— spin-shortener` title suffix**, matching the four `redirect`-served
  pages, even though this component's own pages carry it. The dominant 404
  audience is a public visitor on a branded domain.

### Modified: `gui-pages/routing.py`

Two one-line replacements plus one import. Nothing else — `ROUTES`,
`SECURITY_HEADERS`, `resolve_file` and the `try/except OSError` structure are all
untouched.

| line | today | after |
|---|---|---|
| 91 | `Response(404, {**SECURITY_HEADERS, "content-type": "text/plain; charset=utf-8"}, b"Not found")` | `Response(404, {**SECURITY_HEADERS, "content-type": "text/html; charset=utf-8"}, ERROR_PAGES[404])` |
| 102 | `Response(500, {**SECURITY_HEADERS, "content-type": "text/plain; charset=utf-8"}, b"Internal error")` | `Response(500, {**SECURITY_HEADERS, "content-type": "text/html; charset=utf-8"}, ERROR_PAGES[500])` |

- **`SECURITY_HEADERS` is spread verbatim, exactly as today** (question 4,
  decided). No per-page CSP override, no key removed, no key added beyond
  `content-type`. This is what keeps `test_routing.py`'s two existing
  `for key, value in SECURITY_HEADERS.items(): assert response.headers[key] ==
  value` loops intact — those loops are the pin on the one guarantee this
  component exists to provide, and a page-specific header set would have to
  loosen them.
- **The 500 branch stays a pure return with zero I/O.** `ERROR_PAGES[500]` is a
  dict lookup on a module constant with a literal key; it cannot raise, cannot
  drift against `ROUTES`, and cannot fail the way the read it is handling just
  did. The existing comment above the `try` (recording the code review that
  flagged the unguarded `read_file`) stays as-is and remains true.
- **`errorpages` must not import `routing`,** and does not need to — no cycle.
- No change to `gui-pages/app.py`. It spreads `result.headers` and adds
  `x-ss-version`, so both error responses keep carrying the version header
  exactly as they do today.

### Modified: `gui-pages/tests/test_routing.py`

Extend the two existing error-branch tests rather than adding a file:

- `test_build_response_unknown_path_is_404_with_security_headers_still_set` —
  additionally assert `response.headers["content-type"] == "text/html; charset=utf-8"`
  and `response.body == errorpages.ERROR_PAGES[404]`.
- `test_build_response_file_read_failure_is_500_with_security_headers_still_set` —
  same two assertions against `ERROR_PAGES[500]`. Keep the docstring; the
  defensive property it records is unchanged.
- Add one test asserting the two bodies **differ** from each other (a shared
  shell rendered twice is exactly where both pages end up identical by copy-paste)
  and that both are non-empty `bytes`.

### Modified: `gui-pages/tests/test_no_inline_code.py`

Question 3, answered: **no, nothing picks this up automatically.** `PAGES` comes
from `ROUTES.values()` and reads files under `gui/`; `REDIRECT_TEMPLATES` globs
`redirect/*.html`. An inlined Python constant is in neither list, and the file's
`_read()` helper only reads from `GUI_DIR`. **The guard reaches the new markup by
importing it** — which is available precisely because `errorpages.py` is a pure,
host-importable module, and is the reason the inlining decision does not cost
coverage.

Four additions, in the file's own derived-not-hardcoded idiom:

1. `from errorpages import ERROR_PAGES`, then
   `GUI_PAGES_ERROR_BODIES = sorted((status, body.decode("utf-8")) for status, body in ERROR_PAGES.items())`.
2. `test_gui_pages_error_pages_discovered` — assert `set(ERROR_PAGES) == {404, 500}`
   and that every value is non-empty. Same reason as the file's existing
   `test_pages_list_is_not_empty` / `test_scripts_list_is_not_empty` /
   `test_redirect_templates_discovered`: a derivation that silently yields
   nothing would pass every assertion below it.
3. Parametrize the four existing regexes — `INLINE_SCRIPT`, `STYLE_BLOCK`,
   `STYLE_ATTR`, `EVENT_HANDLER` — over the decoded bodies, keyed by status in
   the test id. These run against the **rendered** bytes, not a template, so
   anything the shared shell introduces is caught once for both pages.
4. A subresource-drift assertion: each body must contain
   `"/vendor/pico.min.css"`, `"/theme.css"` and `"/theme-init.js"` **with the
   leading slash**, with a message naming the arbitrary-depth trap. This is the
   `gui-pages` analogue of `test_error_page_links_both_stylesheets_with_leading_slash`,
   and it is the assertion that would catch someone "fixing" these paths to match
   the relative ones every other page in the component uses.

**Deliberately NOT added:** `ANY_SCRIPT_TAG` over these bodies. These pages load
`/theme-init.js` on purpose (see Trade-offs #3), so that assertion would be
wrong here even though it is right for `redirect/*.html`. `INLINE_SCRIPT` — which
permits `<script src=…>` and forbids a srcless one — is the correct guard for a
page under `script-src 'self'`, and is what the component's own `PAGES` are
already checked with.

## Documentation changes (builder tasks, not done by this plan)

- **CLAUDE.md, Architecture, the `gui-pages` bullet:** it describes the fixed
  path→file allowlist; add that a path outside it and a `read_file` failure now
  render a styled HTML page from `gui-pages/errorpages.py` (inlined `bytes`, not
  a file, so the 500 handler performs no I/O), reusing `/vendor/pico.min.css`,
  `/theme.css`, `/theme-init.js` and `/favicon.svg` with absolute paths because a
  404 can be served at any depth.
- **CLAUDE.md, "Security response headers", the `gui-pages` bullet:** record that
  the error pages carry the component's existing header set **unchanged**,
  including its CSP — no per-page policy — and that they therefore **do** load
  `/theme-init.js` and follow the OS/stored theme, unlike `redirect`'s error
  pages which keep `script-src 'none'` and always render light. State that the
  divergence is deliberate so neither side gets "harmonised" onto the other.
- **CLAUDE.md, "Tests", the `test_no_inline_code.py` bullet:** it currently says
  the file covers every `redirect/*.html`. Add that it also covers `gui-pages`'
  error-page bytes, reached by importing `errorpages.ERROR_PAGES` rather than by
  reading a file, and that `ANY_SCRIPT_TAG` is scoped to the `redirect` templates
  only, on purpose.
- **DESIGN.md, Layout, the "Auth page" bullet:** it says "Four pages now share
  this shell, all served by `redirect`". Six now, two of them served by
  `gui-pages`. Still no new class and no new token.
- **DESIGN.md, next to the existing "Redirect error pages" subsection:** add a
  short `gui-pages` error-pages note carrying the copy rules above, and stating
  in one sentence that **`redirect`'s "may never distinguish why" rule does not
  apply to these pages, and these pages' specificity must not be back-ported to
  `/r/`.**
- No `.impeccable/design.json` change and no new token.

## Trade-offs and rejected alternatives

**1. Serving the error markup from a file under `gui/` through the injected
`read_file`. Rejected — this is the crux, and the circularity is decisive.** It
is genuinely attractive: the component already mounts `gui/`, `build_response`
already takes a `read_file`, a designer would get a real HTML file to edit with
syntax highlighting, and the existing `PAGES` guard idiom reads files from `gui/`
already. It loses because **the 500 branch exists precisely because `read_file`
can fail.** Fetching the error page's own markup through the same callable puts a
possible failure inside the failure handler, and the handler for *that* failure
needs a fallback — which is a `bytes` constant, i.e. the rejected option, now
carried in addition to the file. Two further nails: (a) the plausible way to
avoid the runtime read is to embed the markup at build time, and **there is no
`go:embed` equivalent here** — only `spin.toml`'s `files` mapping is a runtime
filesystem and it maps `gui/`, so a data file in `gui-pages/` is unreachable
(UNCONFIRMED above, but the plan does not depend on it); reading it eagerly at
module import instead just moves the same failure to instance startup, where
there is no response to attach `SECURITY_HEADERS` to at all. (b) A file under
`gui/` that is not in `ROUTES` is a *fourth* category of file in that directory
(page, page-scoped asset, shared asset, unreachable-but-required), and one that
`test_manifest_components.py`-style reasoning about routes cannot see.
**The duplication objection, which is the real cost, is smaller here than in
`redirect`:** one shared `_render` shell means one `<head>` block for both pages,
against the three near-identical `<head>`s `redirect` accepted. And the mirror of
this trade — `docs/plans/redirect-error-pages.md`'s Trade-offs #3, which rejected
"copy in Go constants" — flips cleanly, because both of its reasons were
language-specific: a Python constant is *importable* by the guard (no source
parsing, unlike reading a `.go` file), and Python `#` comments live outside the
guarded bytes, so the comment-stripping regex `error-404.html`'s guard needed
does not arise.

**2. Giving these responses a tightened, `redirect`-style CSP
(`default-src 'none'; script-src 'none'; style-src 'self'; …`). Rejected; they
keep `SECURITY_HEADERS` verbatim.** Attractive on two counts: it matches the
sibling error pages, and `img-src 'self' data:` plus `form-action 'self'` are
dead permissions on a page with no form, no table and no Pico affordance to
render. It loses on three. First, **what it would actually protect against
cannot happen here**: the tightening's value is containing injected content, and
these bodies are rendered at import time from module constants with no request
data reaching them — `redirect`'s prompt page earned its strict policy because it
has real parameters (`.Slug`, `.Error`); these pages have no parameter at all.
Second, it introduces a **second header set inside the one module whose entire
purpose is to have one**, on the exact two branches whose stated guarantee is
"`SECURITY_HEADERS` still set" — and it would force both existing
`for key, value in SECURITY_HEADERS.items()` loops in `test_routing.py` to be
loosened, weakening the pin on that guarantee to buy a theoretical hardening.
Third, a future addition to `SECURITY_HEADERS` would silently not reach the error
responses. **Revisit trigger:** if these pages ever interpolate anything derived
from the request (which this plan forbids), tighten the CSP in the same change.

**3. Following `redirect`'s always-light decision and loading no script.
Rejected; these pages load `/theme-init.js`.** This is the one place the plan
deliberately diverges from the sibling pattern, so the reasoning matters. (a)
**Consistency runs the other way in this component.** Every page `gui-pages`
serves loads `theme-init.js` as the first element of `<head>`; a script-free
error page would be the odd one out *at the same origin*, and a dark-mode console
user who mistypes a URL would get a white page. In `redirect`, no page loads
script, so the opposite choice is the consistent one there. (b) **It costs no CSP
widening**, because decision #2 keeps `script-src 'self'` — which is exactly why
the argument that defeated this in `redirect` (widening the strictest policy in
the app to buy an aesthetic) does not carry over. (c) **The degraded case is
`redirect`'s behaviour exactly**: if `theme-init.js` is blocked or 404s, no
`data-theme` is set and `theme.css`'s unconditional light block renders the page
light (CLAUDE.md, Theming, "No-JS fallback"). So this is strictly additive with a
graceful fallback, not a dependency. (d) `app.js`'s `initHeader` guard exists
because an unguarded `window.ssTheme` call would kill a whole page's init chain —
these pages have no init chain to kill. **The residual cost, stated plainly:** the
guard can no longer assert "no `<script>` at all" on these two pages, so the
enforcement is `INLINE_SCRIPT` (no srcless script) plus the leading-slash
assertion on `/theme-init.js`, which is the same bar every other page in this
component is held to. Reversing this later is one deleted line per page plus
adding `ANY_SCRIPT_TAG` to the new parametrization.

**4. Adding a "Go to the dashboard" or "Sign in" link to the 404. Rejected.**
Attractive for the console user who mistyped a URL and is now stranded with no
way forward but the back button. It loses on audience: the dominant visitor here
is the public one who dropped the `/r/` from a shared link, for whom a link to a
login page is worse than no link — it is a dead end that also points the merely
curious at the internal console. The console user already has the browser's back
button and knows the app exists. **Revisit trigger:** an actual report of a
console user stranded on this page.

**5. Echoing the requested path back ("No page at `/promo`"). Rejected.** It is
the most obviously helpful thing this page could say, and it is the one thing it
must not do: it puts request-controlled input into HTML, which turns a data-free
constant into a template needing escaping, invalidates the "no injection point"
argument that Trade-offs #2 rests on, and makes the page a reflection surface on
the app's most public route. The detail line names the likely *cause* instead,
which is more actionable than the path the visitor just typed.

**6. Adding the error pages to `ROUTES` so they are ordinary served pages.
Rejected**, and forbidden by this plan's non-goals besides. It would make them
directly navigable at `/error-404.html`, add two entries to the allowlist that
are not pages anyone should visit, and reintroduce the `read_file` circularity of
Trade-offs #1 with extra steps.

**7. Do nothing; leave the entry filed. Rejected, and it was a live option** —
there is no measurement gate, no correctness defect, and the plain-text response
is functionally fine. It loses because the cost is genuinely small (one new pure
module, two one-line branch changes, two test files widened, no new route, no new
asset, no new token, no KV work) and because the sibling route just paid the same
cost: leaving one of the app's two public-facing 404s as a raw byte string
reproduces the exact inconsistency the redirect work removed, one route over.

## Tasks

The exact unchecked lines appended to `TASKS.md` under
`## Styled error pages for the gui-pages catch-all`, inserted immediately above
the `# START HERE — session handoff, 2026-08-18` block, which stays last.
`TASKS.md` is authoritative; checkbox state is not maintained here.

```
- [ ] Add gui-pages/errorpages.py and serve both pages from routing.py's 404 and 500 branches — file(s): gui-pages/errorpages.py, gui-pages/routing.py — done when: errorpages.py has zero spin_sdk imports and renders NOT_FOUND_HTML/INTERNAL_ERROR_HTML at import time from one shared shell with no request data reaching it, publishes ERROR_PAGES = {404: ..., 500: ...}, and every asset reference (/theme-init.js, /favicon.svg, /vendor/pico.min.css, /theme.css) carries a leading slash; routing.py's two error returns serve those bytes with content-type text/html; charset=utf-8 while still spreading SECURITY_HEADERS verbatim with no per-page CSP override; the 500 branch still performs no I/O of its own; and `cd gui-pages && uv run pytest` passes
- [ ] Extend test_routing.py to pin the served error bodies and content type — file(s): gui-pages/tests/test_routing.py — done when: the existing 404 and 500 tests each additionally assert content-type is text/html; charset=utf-8 and body == errorpages.ERROR_PAGES[404]/[500] with both SECURITY_HEADERS loops kept intact and unloosened, a new test asserts the two bodies are non-empty and differ from each other, and `cd gui-pages && uv run pytest` passes
- [ ] Widen the inline-code guard to cover the gui-pages error bytes by import — file(s): gui-pages/tests/test_no_inline_code.py — done when: the guard imports errorpages.ERROR_PAGES, asserts its keys are exactly {404, 500} with non-empty values, parametrizes INLINE_SCRIPT/STYLE_BLOCK/STYLE_ATTR/EVENT_HANDLER over both decoded bodies, asserts each body contains "/vendor/pico.min.css", "/theme.css" and "/theme-init.js" with leading slashes, does NOT apply ANY_SCRIPT_TAG to them, `cd gui-pages && uv run pytest` passes above the 96-test baseline, and inserting a srcless <script> into the shared shell and separately dropping a leading slash from "/theme.css" each fails a named test (report the mutation result and revert both)
- [ ] End-to-end manual verification of the gui-pages 404 and 500 in a real browser — file(s): (none — verification step) — done when: against a local `spin up --build`, /nope, /admin/nope and /links/nope all return 404 with content-type text/html and render the narrow centered card fully styled (proving the absolute asset paths work at depth), the same page renders dark for a visitor with ss-theme=dark in localStorage and light with no stored value on a light-OS machine, DevTools Console shows zero CSP violations on each, /r/nosuchslug still returns redirect's own unstyled-by-comparison 404 page unchanged, a 500 induced by renaming gui/login.html aside renders the "Something went wrong" card, and `git status --porcelain gui/` is empty afterwards
- [ ] Record the gui-pages error pages in CLAUDE.md and DESIGN.md — file(s): CLAUDE.md, DESIGN.md — done when: CLAUDE.md's Architecture gui-pages bullet says an unknown path and a read failure now render inlined styled HTML (not a file, so the 500 handler does no I/O) with absolute asset paths because a 404 can be served at any depth; its "Security response headers" gui-pages bullet records that the error pages keep the component's existing header set including its CSP unchanged and therefore DO load /theme-init.js, unlike redirect's always-light error pages, with the divergence marked deliberate; its "Tests" bullet says test_no_inline_code.py also covers these bytes by import and that ANY_SCRIPT_TAG stays scoped to redirect/*.html; DESIGN.md's Layout "Auth page" bullet says six pages share the shell, two served by gui-pages; and a DESIGN.md note next to "Redirect error pages" carries this page's copy rules and states that redirect's "may never distinguish why" rule does not apply here and this page's specificity must not be back-ported to /r/
```

## Critical files

- `gui-pages/errorpages.py` (new)
- `gui-pages/routing.py`
- `gui-pages/tests/test_routing.py`
- `gui-pages/tests/test_no_inline_code.py`
- `CLAUDE.md`
- `DESIGN.md`

Explicitly **not** touched: `spin.toml` (no new route), anything under `gui/`
(no new asset, no new token, and therefore no exposure to the `spin_static_fs`
startup-snapshot staleness trap — the rename in verification is temporary and
reverted), `gui-pages/app.py`, `redirect/**`, `api/**`, `Jenkinsfile`,
`.impeccable/design.json`, `dev/*.sh`.

## Verification

Run in this order.

1. **The `gui-pages` suite:**
   ```bash
   cd gui-pages && uv run pytest
   ```
   **Pass:** all pass, above the **96-passed** baseline measured before this
   change. Then the mutation check the task requires: temporarily insert a
   srcless `<script>` into `errorpages.py`'s shared shell, and separately change
   `"/theme.css"` to `"theme.css"`; confirm a **named** test fails for each, and
   revert both. A guard that cannot fail is not a guard.

2. **The other two suites, to confirm nothing else moved** (neither should be
   affected; run them because CI will):
   ```bash
   cd redirect && go test ./linkgate/...
   cd api && uv run pytest
   ```
   Never `go test ./...`, `go build ./...` or `go vet ./...` — they fail by design
   with `wit_exports.go:934:6: missing function body`.

3. **Run the app** (from the repo root; the bootstrap password is required every
   run, and `COOKIE_SECURE=false` is needed for browser login over plain HTTP):
   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=devpassword SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```

4. **The 404, at three depths** — the depth is the point, since the absolute
   asset paths are what make a nested 404 render at all:
   ```bash
   for p in /nope /admin/nope /links/nope /promo; do
     curl -sD - -o /dev/null "http://localhost:3000$p" \
       | grep -iE '^(HTTP|content-type|content-security-policy|x-frame-options|x-ss-version)'
   done
   ```
   **Pass:** `404` and `content-type: text/html; charset=utf-8` on all four, with
   the component's existing CSP (`default-src 'self'; script-src 'self'; …`)
   unchanged, plus `x-frame-options: DENY` and `x-ss-version`.

5. **The 404 in a real browser, which is the only thing that proves the styling.**
   Load `http://localhost:3000/admin/nope` in Chrome. **Pass:** the narrow
   centered card renders with Pico + `theme.css` applied (an unstyled full-width
   page means a relative asset path slipped in), the DevTools Console shows **no**
   CSP violation (the only expected entry is the browser's own "Failed to load
   resource: the server responded with a status of 404" for the navigation
   itself), and `document.documentElement.dataset.theme` is `"light"` or
   `"dark"` — **not `undefined`**, which is what confirms `theme-init.js` ran.
   Then set `localStorage.setItem("ss-theme", "dark")`, reload, and confirm the
   page renders dark and `dataset.theme === "dark"`; clear the key afterwards.

6. **The 500, by inducing a real `ROUTES`-vs-filesystem drift.** `login.html` is
   served **only** by `gui-pages` (the `gui` component's routes are `.js`/`.css`/
   `favicon.svg` only), so renaming it has no other effect:
   ```bash
   mv gui/login.html gui/login.html.disabled
   curl -sD - -o /tmp/500.html http://localhost:3000/login.html \
     | grep -iE '^(HTTP|content-type|content-security-policy)'
   ```
   If this still returns `200`, restart `spin up` and repeat — whether a mid-run
   rename is visible without a restart is UNCONFIRMED above. **Pass:** `500`,
   `content-type: text/html; charset=utf-8`, the unchanged component CSP; the
   browser renders the "Something went wrong" card with no CSP violation; the
   body names no path, no status code and no notification. Then:
   ```bash
   mv gui/login.html.disabled gui/login.html
   git status --porcelain gui/
   ```
   **Pass:** the second command prints nothing. A committed rename here would
   break the login page for everyone.

7. **The neighbouring surfaces are unchanged.** With the manifest and `gui/`
   restored: `/login.html` and `/dashboard.html` still return `200` and render;
   `curl -sD - -o /dev/null http://localhost:3000/r/nosuchslug` still returns
   `404` with **`redirect`'s own** CSP (`default-src 'none'; script-src 'none'; …`),
   not this component's — the two error surfaces stay distinct, which is the
   header-level expression of the decision in Trade-offs #2 and #3.

CI (`Jenkinsfile`) runs `go test ./linkgate/...`, `cd api && uv run pytest` and
`cd gui-pages && uv run pytest` in parallel Docker stages and builds no Wasm.
Only the third is affected and no test invocation changes, so `Jenkinsfile` is
not in scope.

## Out of scope / follow-ups

- **`405`/method handling on the catch-all.** `gui-pages` answers any method the
  same way (it never inspects `request.method`), so a `POST /nope` gets the 404
  page. Unchanged by this plan and not worth changing — nobody reaches it by
  clicking a shared link.
- **A `robots.txt`, `favicon.ico` or `.well-known` route.** All of them currently
  land on the catch-all and will now return a ~1 KB HTML page instead of a 19-byte
  string. Harmless (no recursion is possible — every asset the error page loads is
  an exact route on `gui`), and adding real routes for them is a separate product
  decision. Worth a Future-work entry only if crawler noise ever shows up in a log,
  and `gui-pages` has no logging today to show it in.
- **The 500 is invisible to the operator.** `gui-pages` has no instrumentation at
  all (CLAUDE.md, "Toggleable structured logging"), so a `ROUTES`-vs-filesystem
  drift produces no signal anywhere — which is exactly why the copy tells the
  visitor to report it. Instrumenting `gui-pages` is a different, larger change;
  **added to `TASKS.md`'s "Future work (not scheduled)"** with a trigger.
- **No copy review with a real stakeholder.** The wording is chosen against
  PRODUCT.md's audience and the constraints above. The 404's detail line — the one
  that names `/r/` — is the sentence most likely to be re-worded, and it is the
  one carrying all the actual help, so it should not simply be deleted for
  brevity.
- **`redirect`'s pages are untouched**, including their always-light theming and
  their forbidden-word guard. The divergence is recorded in CLAUDE.md and
  DESIGN.md by the last task specifically so a later "consistency" pass does not
  collapse the two.
