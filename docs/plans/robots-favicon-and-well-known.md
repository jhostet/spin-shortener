# `robots.txt`, `favicon.ico` and `/.well-known/*` on the `gui-pages` Catch-All

## Context

`gui-pages/routing.py`'s `build_response` maps a request path against a fixed
exact-match `ROUTES` allowlist and answers **everything else** with the styled
HTML 404 that `docs/plans/gui-pages-error-pages.md` shipped
(`errorpages.ERROR_PAGES[404]`, ~1 KB). Three well-known non-page paths land
there today:

- `/robots.txt` — a crawler asks, gets an HTML page for a human, and concludes
  the site has no crawl policy at all.
- `/favicon.ico` — a browser asks for an image, gets `text/html`, and (with
  `X-Content-Type-Options: nosniff` on the response) discards it.
- `/.well-known/<anything>` — a protocol client (password manager, ACME client,
  status-code-reliability probe) asks, gets a rendered page.

`TASKS.md`'s Future-work entry (line 503, raised 2026-08-23 while planning
`docs/plans/gui-pages-error-pages.md`, and repeated in that plan's "Out of scope"
section) is the brief. It filed the behaviour as *harmless* — correctly: no
recursion is possible, because every asset the error page loads (`/theme-init.js`,
`/favicon.svg`, `/vendor/pico.min.css`, `/theme.css`) is an exact route on the
`gui` component and so can never re-enter the catch-all. It deferred the work
with "adding real routes for any of them is a separate product decision," gated
behind "crawler noise actually showing up somewhere." **The user has now made
that decision and asked for real handling for all three**, so the gate no longer
applies and this plan supersedes it.

This is a sibling of `docs/plans/redirect-error-pages.md` and
`docs/plans/gui-pages-error-pages.md` and deliberately reuses their shape: one
new pure, host-importable module holding constants rendered at import time, wired
into `build_response` with the component's existing `SECURITY_HEADERS` spread
verbatim. It is small: ~45 new lines in one new module, ~6 changed lines in
`routing.py`, one new test file.

**Confirmed decisions (settled by the user before planning):**

- Scope is `gui-pages` only. `redirect/` and `api/` are not touched.
- No crawler-blocking logic anywhere but `robots.txt` itself — no user-agent
  sniffing, no CAPTCHA, no bot heuristics.
- `gui/` is touched only if a real `favicon.ico` binary asset turns out to be
  warranted (it does not — see Decision 2).

**The four decisions this plan makes, each argued below:**

1. **`robots.txt` is a real `200 text/plain` response with `User-agent: *` /
   `Disallow: /`** — a blanket disallow, with `/r/` in scope *deliberately*
   because a crawled short link is an unfilterable fake click.
2. **`/favicon.ico` gets a cheap plain `404`, and no `.ico` binary is added.**
3. **`/.well-known/*` is matched by prefix (`ROUTES` cannot express it) and gets
   the same cheap plain `404`.**
4. **All three land in `gui-pages`, in one new pure module, and carry the
   component's existing `SECURITY_HEADERS` verbatim** — not a `gui` static route,
   not a `ROUTES` entry.

## Key technical facts confirmed during research

- **`ROUTES` is a fixed exact-match `dict[str, str]`** (`gui-pages/routing.py:22-33`,
  read in full) and `resolve_file` is a bare `ROUTES.get(path)` (`:83-85`). There
  is no prefix or pattern matching anywhere in the module, so **a single `ROUTES`
  entry cannot cover `/.well-known/*`'s arbitrary sub-paths.** Confirmed by
  reading the file.
- **The 200 path hardcodes one content type.** `build_response`'s success return
  is `{**SECURITY_HEADERS, "content-type": "text/html; charset=utf-8"}`
  (`routing.py:108`). `ROUTES` carries only a filename, with no place to put a
  content type — so serving `robots.txt` through `ROUTES` would require widening
  the allowlist's value shape. Read directly.
- **Both error branches already serve HTML.** `routing.py:95` and `:106` return
  `ERROR_PAGES[404]` / `ERROR_PAGES[500]` with `content-type: text/html;
  charset=utf-8`. So the current `/robots.txt` response is a ~1 KB HTML document
  under a `404`.
- **`SECURITY_HEADERS`** (`routing.py:52-73`) is
  `x-content-type-options: nosniff`, `referrer-policy: strict-origin-when-cross-origin`,
  `x-frame-options: DENY`, `strict-transport-security: max-age=31536000; includeSubDomains`,
  and a CSP of `default-src 'self'; script-src 'self'; style-src 'self';
  img-src 'self' data:; object-src 'none'; base-uri 'self'; form-action 'self';
  frame-ancestors 'none'`. Every directive in that CSP is inert on a
  `text/plain` body — a CSP governs a *document*'s subresource loading, and a
  `text/plain` response under `nosniff` is never parsed as a document.
- **`app.py` adds `x-ss-version` to whatever `build_response` returns**
  (`gui-pages/app.py:35`), so any new response kind inherits the version header
  with no extra work. Read directly.
- **`redirect`'s four served HTML templates declare NO `<link rel="icon">`.**
  `grep -rn 'rel="icon"' gui/ redirect/ gui-pages/` matches all eight `gui/`
  pages and `gui-pages/errorpages.py:41`, and **zero** files under `redirect/`
  (`prompt.html`, `error-404.html`, `error-500.html`, `error-503.html`). Per
  `spin.toml:146-152`'s own comment, a page with no `<link rel="icon">` is
  exactly the case in which a browser makes an unprompted `/favicon.ico`
  request — so the probe is real, and it originates on the app's *most public*
  surface, not on the console.
- **Safari has supported SVG favicons since 15.4 (late 2021)**; roughly 95% of
  browsers globally render an SVG favicon as of 2026, the remainder being
  Safari < 15.4 and IE. Confirmed by web search
  ([jwtoolbox](https://www.jwtoolbox.com/blog/svg-favicon-2026-browser-support-dark-mode-guide),
  [faviconbuilder](https://faviconbuilder.com/guides/svg-favicon-browser-support/)).
  So the existing `/favicon.svg` (`spin.toml:153-155`, `gui/favicon.svg`, 1,091
  bytes) already covers the console's real audience.
- **`redirect` no longer reads the `User-Agent` header at all.**
  `grep -rn "ClassifyUserAgent\|User-Agent\|user_agent" redirect/*.go
  redirect/linkgate/*.go` (excluding tests) returns **nothing** — the classifier
  was retired with the recent-events write (`docs/plans/drop-events-write.md`,
  2026-08-18). Combined with CLAUDE.md's Analytics section (every successful
  redirect performs one `analytics:count:<slug>:<shard>` read-modify-write),
  this means **a crawler's fetch of `/r/{slug}` is recorded as a click that is
  structurally indistinguishable from a human's, at write time and forever
  after.** `robots.txt` is the only lever available for this that does not
  violate this task's non-goals.
- **`/.well-known/resource-that-should-not-exist-whose-status-code-should-not-be-200`
  is a real registered well-known URI**, used by password managers to check that
  an origin returns a non-200 for an unknown well-known path before trusting
  `/.well-known/change-password` (W3C "Detecting the reliability of HTTP status
  codes" §3.1; confirmed via
  [the registry issue](https://github.com/protocol-registries/well-known-uris/issues/13)).
  Today this app already returns 404 for it (styled HTML), so this plan does not
  fix a bug there — it makes the answer cheap and honestly typed.
  **PARTIALLY CONFIRMED:** the `change-password` spec itself
  (`w3c.github.io/webappsec-change-password-url/`) does not state what a
  non-supporting origin should return; it defers to the reliability document
  above. Either way a 404 is a correct answer, and this app has no self-service
  change-password page (`grep -rn "change.password\|changePassword\|change_password"
  gui/*.js gui/admin/*.js api/*.py` → no matches).
- **`spin_static_fs` (the `gui` component) sets no security headers and needs
  exact routes.** `spin.toml:77-84` records that a wildcard route on that
  component 404s once it has more than one trigger, confirmed live. So
  `/.well-known/...` could not be served there even if we wanted to.
- **Baseline test count:** `cd gui-pages && uv run pytest -q` → **116 passed**
  (measured before this change).
- **`gui-pages/pyproject.toml` sets `pythonpath = ["."]`**, so a new sibling
  module is importable from `tests/` with no `conftest.py` — the same fact
  `errorpages.py` relied on.
- **UNCONFIRMED: whether an Akamai property fronting a branded domain would serve
  this origin's `/robots.txt` or its own.** On the `*.fwf.app` deployment the
  origin's file is what a crawler sees (nothing in Akamai Functions intercepts
  application paths), but a future custom-domain property could override it at
  the edge. To confirm: `curl https://<domain>/robots.txt` after any custom-domain
  cutover and compare against the local response. Not blocking — the app-side
  file is a prerequisite either way.

## Decision 1 — `robots.txt`: `Disallow: /`, and `/r/` is in scope on purpose

**Served body, exactly** (the comment lines are part of the served bytes and are
load-bearing; see below for why the *maintainer*-facing rationale is deliberately
not in them):

```
# Nothing on this host is meant to be crawled or indexed.
#
# /r/ is disallowed deliberately, not incidentally: a crawler that follows a
# short link is answered with a 302 and recorded as a real click, in the same
# counter the marketing team reads. Nothing downstream can tell that click
# from a person's.
User-agent: *
Disallow: /
```

**Why a blanket disallow rather than a curated list.** There is no surface here
that anyone wants crawled. `/api/` is JSON behind a session cookie; `/admin/*`,
`/dashboard.html` and `/links/detail.html` are an authenticated internal console
(PRODUCT.md: "marketing/campaign team" plus "a secondary, smaller admin
population"); `/login.html` is a sign-in form whose appearance in a search result
would be pure attack surface with no upside; `/` is a redirect stub. A curated
`Disallow:` list would also have to be maintained in step with `ROUTES` and
`spin.toml` — exactly the two-lists-drift failure this repo has been bitten by
before (`gui/admin/users.html`'s permission checkboxes vs `users.js`'s
`ALL_PERMISSIONS`). `Disallow: /` cannot drift.

**Why `/r/` specifically matters, and why this is the strongest argument in the
plan.** Every successful `/r/{slug}` request performs an
`analytics:count:<slug>:<shard>` read-modify-write (CLAUDE.md, Analytics), and
`redirect` does not read the `User-Agent` header at all (confirmed above — the
classifier was deleted with the events write). So:

- a crawler that fetches a short link **inflates the click total the marketing
  team makes decisions on**, with no way to identify or subtract it afterwards;
- it also consumes the app-wide **50 KV writes/second** Akamai cap, which is the
  single binding constraint on click accuracy (CLAUDE.md, "Deployment: Akamai
  Functions") — bot clicks compete directly with real ones for it;
- short links are *published* — in emails, ads, social posts and on partner
  pages — so discovery by a crawler is the normal case, not an edge case. Link
  unfurlers (Slackbot, Twitterbot, Facebook's crawler) that honour `robots.txt`
  stop generating a phantom click every time somebody pastes a campaign link into
  a chat window.

**What this is not.** `robots.txt` is a politeness protocol, not a control. It
does nothing about a hostile enumerator, and CLAUDE.md's accepted tradeoff
("Slug/link existence… are enumerable", "No brute-force rate limiting") is
completely unchanged by this plan. The claim here is narrow and honest: *well-behaved*
crawlers stop manufacturing clicks.

**The one real cost, stated plainly.** A `Disallow: /` means a search engine will
not follow a short link that appears on a third-party page, so the destination
receives no crawl signal through the short URL. That cost is near-zero here: the
redirect is a **302** by hard requirement (301/308 are forbidden — Akamai caches
them; CLAUDE.md, "Redirect caching"), and a 302 passes little to no link equity
by design. Click-count integrity is a stated product capability; speculative SEO
value through a temporary redirect is not.

**Comment content is split deliberately.** The `/r/`-and-click-counting rationale
stays *inside* the served bytes, because that is the sentence that stops a future
maintainer "helpfully" narrowing the rule, and it discloses nothing (`/r/` is the
app's most public path — it is printed on QR codes). The rationale that names
`/admin/`, `/api/` and `/login.html` stays in the module's Python `#` comments,
**outside** the bytes: `robots.txt` is famously read as a map of interesting
paths, and `Disallow: /` has the pleasant property of naming nothing. This is the
same trick `errorpages.py` uses to keep explanatory prose out of guarded content.

## Decision 2 — `favicon.ico`: a plain 404, and no `.ico` asset

**No `favicon.ico` binary is added to `gui/`, and no new `spin.toml` route is
added.** `/favicon.ico` gets a `404` with `content-type: text/plain; charset=utf-8`
and a ten-byte body, from the same mechanism as `/.well-known/*`.

Three reasons, in order of weight:

1. **Adding a real `.ico` would partially undo a deliberate, documented decision
   in a browser-dependent way.** `redirect/prompt.html` is intentionally left
   without a `<link rel="icon">` because its CSP is the strictest in the app
   (`default-src 'none'`) and a favicon is fetched under `img-src` — widening the
   one policy guarding the page where a visitor types a credential is not worth a
   tab icon (`spin.toml:146-152`; `TASKS.md` line 905). A root `/favicon.ico`
   would be fetched by the *browser's* default probe on exactly that page — and
   whether it succeeds depends on whether the browser enforces `img-src` against
   the automatic favicon request, which Chrome does and Firefox historically does
   not. The result would be "the credential page shows a product icon in some
   browsers and not others," which is worse than the current, uniform "no icon."
2. **The SVG already covers the real audience.** Safari 15.4+ supports SVG
   favicons; ~95% of browsers do (confirmed above). Every one of the app's eight
   real pages declares `<link rel="icon" href="…favicon.svg" type="image/svg+xml">`,
   so no console page even makes the `/favicon.ico` probe on a supporting
   browser. The residual is Safari < 15.4 and IE — not the corporate-browser
   population PRODUCT.md describes.
3. **It would be the repo's only generated binary asset.** An `.ico` cannot be
   hand-edited alongside `gui/favicon.svg`; it needs a conversion step that lives
   nowhere in this build (each component builds only in its own `workdir`, with
   no shared build step), so the two would silently drift the first time the
   glyph changes.

**Why a plain 404 rather than leaving it on the styled page** (i.e. why this is
not simply "no code change, document why"): the probe is real and it happens on
the app's *public* surface — all four `redirect`-served pages declare no favicon
link (confirmed above), so every browser that renders a password prompt or a
`/r/` error page and does not block the probe asks this app for
`/favicon.ico`. Answering an image request with a 1 KB HTML document addressed to
a human is the same category error the `/.well-known/*` handling fixes, and
handling it costs exactly one dict entry once the mechanism exists. Having the
path named in code is also what stops this question being re-litigated: the
module says, in one place, that this app deliberately has no `.ico`.

**404, not 204 or a redirect to the SVG.** 404 is the honest answer (there is no
such resource) and is the one Akamai caches by default for 10 seconds
(CLAUDE.md, "Caching favours the fix"), which suppresses repeat origin hits for
free. A `204` is a suppression hack that says "this resource exists and is
empty." A `302` to `/favicon.svg` would hand an SVG to precisely the population
that asked for `.ico` *because it cannot render SVG*.

## Decision 3 — `/.well-known/*`: prefix-matched, plain 404

`ROUTES` is exact-match only (confirmed above), so this needs a prefix test.
The rule is deliberately narrow:

```python
path == WELL_KNOWN_PREFIX.rstrip("/") or path.startswith(WELL_KNOWN_PREFIX)
```

with `WELL_KNOWN_PREFIX = "/.well-known/"`. **The trailing slash in the constant
is load-bearing**, the same way `urlpolicy.py`'s `host.endswith("." + rule)` dot
is: a bare `path.startswith("/.well-known")` would also swallow
`/.well-knownx` and `/.well-known-backup`, silently converting an ordinary
mistyped-URL 404 (which should render the styled page a human can read) into a
bare byte string. The bare `/.well-known` form is matched explicitly because it
is obviously in the same path space and there is no page there either.

**A plain 404 is the right response, not the styled page and not a 200.**
RFC 8615's `/.well-known/` space is addressed to *software*, not to people —
password managers (`change-password`), ACME clients (`acme-challenge`), security
researchers' tooling (`security.txt`), Apple's app-association fetcher. None of
them renders HTML, none of them has a human looking at the response, and the one
standardised probe in that space —
`/.well-known/resource-that-should-not-exist-whose-status-code-should-not-be-200`
— asks *specifically* whether this origin returns a non-200 for an unknown
well-known path. Returning 404 with a ten-byte body answers that probe correctly
and cheaply. (It is answered correctly today too, at ~100× the bytes.)

**Nothing under `/.well-known/` is served with a 200 by this plan.** In
particular no `security.txt` — see Trade-offs #5. If an ACME `http-01` challenge
or a `security.txt` is ever needed, the new module is where it goes, and its
exact-path dict is checked before the prefix rule so a single served path is a
one-line addition.

## `gui-pages` changes

Everything lands in the **`gui-pages` component (Python)**. This follows the
language rule rather than bending it: none of it is on the redirect hot path, and
this component already owns the answer to "what happens to every path that is not
a `gui` asset route."

### New: `gui-pages/nonpages.py`

A pure module — zero `spin_sdk` imports, host-importable — holding the served
bytes and the three path decisions. It **does not import `routing`** (that would
be a cycle), and therefore does not build a `Response`: it returns a
`(status, content_type, body)` triple and lets `routing.build_response` assemble
the response with `SECURITY_HEADERS`. **That is deliberate — `SECURITY_HEADERS`
must continue to be spread in exactly one function**, since attaching them is the
single reason this component exists.

Published surface:

```python
NonPageResponse = tuple[int, str, bytes]   # status, content-type, body

ROBOTS_TXT: bytes
WELL_KNOWN_PREFIX: str = "/.well-known/"
PLAIN_TEXT: str = "text/plain; charset=utf-8"
NOT_FOUND: NonPageResponse = (404, PLAIN_TEXT, b"Not found\n")
NON_PAGE_RESPONSES: dict[str, NonPageResponse] = {
    "/robots.txt": (200, PLAIN_TEXT, ROBOTS_TXT),
    "/favicon.ico": NOT_FOUND,
}

def non_page_response(path: str) -> Optional[NonPageResponse]: ...
```

`non_page_response` is, in full:

```python
def non_page_response(path: str) -> Optional[NonPageResponse]:
    if path in NON_PAGE_RESPONSES:
        return NON_PAGE_RESPONSES[path]
    # The trailing slash in WELL_KNOWN_PREFIX is load-bearing: a bare
    # startswith("/.well-known") would also match /.well-knownx, turning an
    # ordinary mistyped-URL 404 (which should render the styled page a person
    # can read) into a bare byte string.
    if path == "/.well-known" or path.startswith(WELL_KNOWN_PREFIX):
        return NOT_FOUND
    return None
```

`ROBOTS_TXT` is a `bytes` literal, written one line per line so the served file's
shape is visible in the source:

```python
ROBOTS_TXT: bytes = (
    b"# Nothing on this host is meant to be crawled or indexed.\n"
    b"#\n"
    b"# /r/ is disallowed deliberately, not incidentally: a crawler that follows a\n"
    b"# short link is answered with a 302 and recorded as a real click, in the same\n"
    b"# counter the marketing team reads. Nothing downstream can tell that click\n"
    b"# from a person's.\n"
    b"User-agent: *\n"
    b"Disallow: /\n"
)
```

Module docstring must record: why the served comment names only `/r/` and not the
admin surface; that `redirect` reads no `User-Agent`, so a crawled short link is
an unfilterable click; that no `.ico` exists on purpose (with the
`prompt.html` CSP reason); and that this is where a future `security.txt` or ACME
challenge would go.

**Name check:** `nonpages` shadows no stdlib module. CLAUDE.md's rule ("never
`logging.py`, `time.py`, `json.py`…") is satisfied.

### Modified: `gui-pages/routing.py`

One import and one new block at the top of `build_response`. `ROUTES`,
`SECURITY_HEADERS`, `resolve_file`, the `try/except OSError`, and both error
returns are **untouched**.

```python
from nonpages import non_page_response
```

```python
def build_response(uri: str, read_file: Callable[[str], bytes]) -> Response:
    path = urlparse(uri).path

    # robots.txt, favicon.ico and /.well-known/* are requested by software, not
    # by a person, so they get a cheap typed answer instead of the styled HTML
    # page the catch-all serves a human who mistyped a URL. These paths are
    # disjoint from ROUTES (pinned by tests/test_nonpages.py), so this block's
    # position relative to the ROUTES lookup below is not load-bearing.
    machine = non_page_response(path)
    if machine is not None:
        status, content_type, body = machine
        return Response(status, {**SECURITY_HEADERS, "content-type": content_type}, body)

    filename = resolve_file(path)
    ...
```

- **`SECURITY_HEADERS` is spread verbatim on the new responses**, exactly as on
  every other response this module returns. On a `text/plain` body the CSP is
  inert (no document is parsed, so there are no subresources to govern) and
  `nosniff` is genuinely useful — it is what stops a browser deciding a
  `text/plain` body full of `#` and `/` characters is HTML. A per-kind header set
  is rejected in Trade-offs #4.
- **No `read_file` call is made on any of these paths**, so none of them can
  reach the 500 branch. Tests pass a `read_file` that raises on call to pin this.
- **No change to `gui-pages/app.py`.** It spreads `result.headers` and adds
  `x-ss-version`, so the new responses carry the version header for free.
- **No change to `spin.toml`.** No route is added or moved; all three paths
  already reach the `/...` catch-all.

### New: `gui-pages/tests/test_nonpages.py`

Nine tests, all against `build_response` (not the helper alone) except the two
structural ones, so they pin the *served* response and not an internal shape:

1. `test_robots_txt_is_served_as_plain_text_with_security_headers` — `200`,
   `content-type: text/plain; charset=utf-8`, `body == nonpages.ROBOTS_TXT`,
   every `SECURITY_HEADERS` pair present, and the injected `read_file` raises
   `AssertionError` if called.
2. `test_robots_txt_disallows_everything` — parse the decoded body ignoring `#`
   comment lines and blanks: exactly one `User-agent:` directive with value `*`,
   exactly one `Disallow:` directive with value `/`, and **no `Allow:` directive
   at all**. The failure message must name the click-inflation reason — this is
   the guard against someone narrowing the rule to re-enable `/r/`.
3. `test_robots_txt_is_utf8_and_contains_no_markup` — decodes as UTF-8 (RFC 9309
   requires it), ends with `\n`, and contains no `<` at all. The `<` assertion is
   why these bytes are deliberately **not** added to `test_no_inline_code.py`: it
   is a strictly stronger, simpler invariant than that file's four regexes, and
   it makes "this became HTML" structurally impossible.
4. `test_favicon_ico_is_a_cheap_plain_404` — `404`, `content-type: text/plain;
   charset=utf-8`, `body != errorpages.ERROR_PAGES[404]`, `len(body) < 100`, all
   `SECURITY_HEADERS` present, `read_file` never called.
5. `test_well_known_paths_are_plain_404s` — parametrized over
   `/.well-known/security.txt`, `/.well-known/change-password`,
   `/.well-known/acme-challenge/tok3n`, `/.well-known/`, and `/.well-known`;
   each `404` + `text/plain` + not the styled body.
6. `test_well_known_reliability_probe_is_not_200` — the literal path
   `/.well-known/resource-that-should-not-exist-whose-status-code-should-not-be-200`,
   asserting `status != 200`, with a docstring naming the W3C reliability probe
   it implements.
7. `test_paths_outside_the_well_known_prefix_still_get_the_styled_page` —
   parametrized over `/.well-knownx`, `/.well-known-backup`, `/nope`,
   `/admin/nope`: each returns `404` with `content-type: text/html; charset=utf-8`
   and `body == errorpages.ERROR_PAGES[404]`. This is the over-match guard for
   Decision 3's trailing slash.
8. `test_non_page_paths_do_not_overlap_routes` —
   `set(NON_PAGE_RESPONSES) & set(ROUTES) == set()` and no `ROUTES` key starts
   with `/.well-known`. This is what makes the ordering inside `build_response`
   provably not load-bearing.
9. `test_no_gui_route_shadows_a_non_page_path` — parse `spin.toml` with
   `tomllib` (the idiom `test_manifest_components.py` already uses, including its
   "a grep is not a usable guard here" reasoning) and assert no trigger with
   `component == "gui"` has a `route` in `NON_PAGE_RESPONSES`. Spin routes by
   specificity, so an exact `/robots.txt` route added to the static component
   later would silently make this module's constant dead code with no test
   failing anywhere else.

### Modified: `gui-pages/tests/test_routing.py`

Add three cases to the existing `test_resolve_file` parametrization —
`("/robots.txt", None)`, `("/favicon.ico", None)`,
`("/.well-known/security.txt", None)` — pinning that these are deliberately *not*
pages and are answered by the other mechanism. No other change; both
`SECURITY_HEADERS` loops stay intact.

### Not modified: `gui-pages/tests/test_no_inline_code.py`

`PAGES` derives from `ROUTES.values()`, and this plan adds nothing to `ROUTES`,
so nothing is picked up automatically — **and that is correct.** That guard exists
because a CSP with `script-src 'self'; style-src 'self'` is only safe if no served
*document* contains inline code. A `text/plain` body under `nosniff` is never
parsed as a document, so `INLINE_SCRIPT`/`STYLE_BLOCK`/`STYLE_ATTR`/`EVENT_HANDLER`
have nothing to say about it. Test 3 above (`"<" not in body`) is the stronger
replacement, and `test_nonpages.py` must carry a comment saying so, so a later
reader does not "fix" the omission.

## Documentation changes (builder tasks, not done by this plan)

- **CLAUDE.md, Architecture, the `gui-pages` bullet:** it currently describes the
  fixed path→file allowlist and the two styled error pages. Add that three
  machine-facing paths bypass the allowlist entirely via `gui-pages/nonpages.py`
  — `/robots.txt` (`200 text/plain`, `Disallow: /`, with `/r/` included on
  purpose because a crawled short link is recorded as a real click and `redirect`
  reads no `User-Agent`), and `/favicon.ico` plus every `/.well-known/*` path
  (cheap plain `404`, not the styled page). State that these paths carry the
  component's `SECURITY_HEADERS` unchanged and perform no `read_file` call, and
  that `/.well-known/*` needs a prefix test because `ROUTES` is exact-match only.
- **CLAUDE.md, Architecture, the `gui` bullet (or `spin.toml`'s comment at
  `:146-152` — the builder should update whichever it does not contradict):**
  record that there is deliberately no `.ico` anywhere, that the SVG covers
  Safari 15.4+ / ~95% of browsers, and that a root `/favicon.ico` was rejected
  because it would give `redirect/prompt.html` a browser-dependent tab icon
  against that page's documented `default-src 'none'` decision. **`spin.toml`'s
  existing comment is now incomplete** — it says the prompt page is the only page
  triggering an unprompted probe, but all four `redirect` templates declare no
  icon link; correcting that sentence is part of this task.
- **PRODUCT.md, Capabilities and Constraints:** one bullet — the app publishes a
  `robots.txt` disallowing everything, including short-link paths, because a
  crawler following a short link is counted as a real click and cannot be
  distinguished from a person afterwards; this reduces bot inflation from
  well-behaved crawlers and link unfurlers only, and is not a defence against a
  hostile enumerator.
- **No DESIGN.md change and no new token** — nothing rendered changes, and the
  styled 404 is untouched for every path a person can actually land on.

## Trade-offs and rejected alternatives

**1. Serving `robots.txt` as a static file from the `gui` component (a
`gui/robots.txt` file plus an exact `spin.toml` route, exactly like
`/favicon.svg`). Rejected — this is the closest call in the plan.** It is
genuinely attractive: `robots.txt` *is* a static text asset, `gui` already serves
one non-script/style asset, and it would leave `gui-pages` completely untouched.
It loses on four counts. (a) **It solves one of three problems** —
`/.well-known/*` cannot be served there at all (`spin_static_fs` wildcards 404
once the component has more than one route, confirmed live at `spin.toml:77-84`),
so the repo would carry two mechanisms for one coherent decision. (b) The `gui`
component sets **no** security headers by design, so `robots.txt` would be the
one response in the app with no `nosniff` — small, but a real asymmetry to
explain forever. (c) `gui/` is served from a **startup snapshot**, so editing the
crawl policy would silently do nothing until `spin up` is restarted — the exact
trap CLAUDE.md warns about, applied to a file whose whole purpose is to be
correct. (d) The content is *policy about routes*, and routes live in this
component's world (`ROUTES`, `spin.toml`), not in the designer-facing asset
directory. **Revisit trigger:** if `robots.txt` ever needs to differ per domain,
neither mechanism helps — `gui-pages` never reads `Host` — and the whole question
reopens.

**2. Adding `/robots.txt` to `ROUTES` and letting the existing file-read path
serve it. Rejected.** Attractive because it needs no new module and reuses
`build_response` end to end. It loses because `ROUTES`' value is a bare filename
with nowhere to put a content type, and `build_response`'s success return
hardcodes `text/html; charset=utf-8` (`routing.py:108`) — so this needs the
allowlist's value shape widened to a tuple, changing every existing entry and
`resolve_file`'s signature, to serve one four-line file. It would also drag
`robots.txt` into `test_no_inline_code.py`'s `PAGES` (derived from
`ROUTES.values()`), where four HTML regexes would be run against a text file
forever, and put the file behind the `read_file` call — meaning a
`ROUTES`-vs-filesystem drift would answer a crawler with a 500.

**3. Adding a real `favicon.ico` binary to `gui/` with an exact route.
Rejected** — argued in full under Decision 2. The decisive point is not the
~95% SVG support figure but that a root `.ico` would be fetched by the browser's
default probe on `redirect/prompt.html`, whose lack of a favicon is a *deliberate*
consequence of its `default-src 'none'` CSP, producing a tab icon in browsers
that do not enforce `img-src` against favicon requests and not in the ones that
do. **Revisit trigger:** a real report from a Safari < 15.4 or IE user, or a
decision to widen `prompt.html`'s CSP for unrelated reasons — at which point the
right change is an `.ico` *plus* an explicit `<link rel="icon">` on every page,
not a bare root file.

**4. Giving these responses a tightened, non-HTML CSP
(`default-src 'none'; sandbox`) instead of `SECURITY_HEADERS` verbatim.
Rejected**, on the same reasoning `docs/plans/gui-pages-error-pages.md`
Trade-offs #2 used and for one extra reason. The tightening protects nothing: a
`text/plain` response under `nosniff` is never parsed as a document, so there are
no subresource loads for a policy to govern, and the bodies are module constants
with no request data in them. Against that, it would introduce a second header
set inside the one module whose entire purpose is to have one, and a future
addition to `SECURITY_HEADERS` would silently not reach these responses.

**5. Serving a real `/.well-known/security.txt` (RFC 9116). Rejected, and it was
a live option** for a service with a public surface. It loses on two operational
facts: it requires a real `Contact:` address, which is an organisational decision
nobody has made, and a mandatory `Expires:` field that **goes stale** — a
security.txt past its expiry is worse than none, and this repo has no mechanism
that would notice. **Revisit trigger:** a named security contact and an owner for
the annual refresh. Filed under `TASKS.md`'s "Future work (not scheduled)".

**6. Adding `X-Robots-Tag: noindex, nofollow` to `gui-pages`' `SECURITY_HEADERS`
as belt-and-braces. Rejected, and the reason is a genuine trap worth recording.**
`robots.txt` prevents *crawling*; a header prevents *indexing* — but a crawler
that obeys `Disallow: /` never fetches the page and therefore **never sees the
header**, so the two mechanisms partly cancel. Google's own guidance is not to
block a URL in `robots.txt` if you want its `noindex` honoured. Since nothing
here is linked from anywhere public, `Disallow: /` alone is sufficient, and
adding a header that is unreachable by construction would be cargo cult.
**Revisit trigger:** a console URL actually appearing in search results, which
would mean it is being linked externally — in which case the correct fix is to
*narrow* the `Disallow` for that path and add the header, together.

**7. Blocking crawlers anywhere other than `robots.txt` — a User-Agent denylist
in `redirect`, or not counting a click when the UA looks like a bot. Rejected,
and forbidden by this task's non-goals.** It is the only thing that would actually
stop a hostile or `robots.txt`-ignoring crawler from inflating counts. It loses
on scope (it is `redirect` hot-path work, in Go, on the app's most
latency-sensitive surface), on maintenance (a UA denylist is a list that is wrong
the day after it is written), and on honesty (silently discarding clicks makes
the analytics *less* explicable, not more). **Revisit trigger:** measured
evidence of real bot inflation in a live click total, at which point it deserves
its own plan.

**8. Do nothing — leave all three on the styled 404 and keep the Future-work
entry filed. Rejected, and it was live.** Nothing is broken today: the styled
page is non-recursive, ~1 KB, and no client is harmed by it. It loses because the
`robots.txt` half is not cosmetic — the app currently publishes *no* crawl policy
for a service whose click counts are a product feature and whose only bot
mitigation is the file that does not exist — and because once the module exists
for that, the other two paths cost one dict entry and one prefix test between
them.

## Tasks

The exact unchecked lines appended to `TASKS.md` under
`## robots.txt, favicon.ico and /.well-known/* on the gui-pages catch-all`, at
the end of the file. `TASKS.md` is authoritative; checkbox state is not
maintained here.

```
- [ ] Add gui-pages/nonpages.py and wire it into build_response — file(s): gui-pages/nonpages.py, gui-pages/routing.py — done when: nonpages.py has zero spin_sdk imports, exposes ROBOTS_TXT (bytes, ending in a newline, whose only directives are `User-agent: *` and `Disallow: /`), WELL_KNOWN_PREFIX = "/.well-known/", NON_PAGE_RESPONSES mapping /robots.txt to a 200 text/plain response and /favicon.ico to a 404 text/plain one, and non_page_response(path) returning that same 404 for "/.well-known" and any path starting with WELL_KNOWN_PREFIX and None otherwise; routing.py calls it at the top of build_response and returns its (status, content_type, body) with SECURITY_HEADERS spread verbatim and no per-kind header set; no ROUTES entry, no spin.toml route, and no read_file call is made on any of the three paths; and `cd gui-pages && uv run pytest` passes
- [ ] Add gui-pages/tests/test_nonpages.py and extend test_resolve_file (depends on the nonpages task) — file(s): gui-pages/tests/test_nonpages.py, gui-pages/tests/test_routing.py — done when: the new file pins, through build_response, that /robots.txt is 200 text/plain with body == nonpages.ROBOTS_TXT and every SECURITY_HEADERS pair set, that the body's only directives are one `User-agent: *` and one `Disallow: /` with no `Allow:` anywhere, that it is UTF-8 with no "<" in it, that /favicon.ico and /.well-known/{security.txt,change-password,acme-challenge/tok3n,,} are plain 404s under 100 bytes that are NOT errorpages.ERROR_PAGES[404], that /.well-known/resource-that-should-not-exist-whose-status-code-should-not-be-200 is not 200, that /.well-knownx and /.well-known-backup and /nope still return the styled text/html 404, that set(NON_PAGE_RESPONSES) and set(ROUTES) are disjoint with no ROUTES key under /.well-known, and that no spin.toml trigger on the gui component routes /robots.txt or /favicon.ico (tomllib parse, not grep); every read_file injected in this file raises if called; test_routing.py's test_resolve_file gains ("/robots.txt", None), ("/favicon.ico", None), ("/.well-known/security.txt", None); and `cd gui-pages && uv run pytest` passes above the 116-test baseline
- [ ] Mutation-check the new guards (depends on the test task) — file(s): (none — verification step) — done when: each of these edits is made temporarily, confirmed to fail a NAMED test, and reverted: changing ROBOTS_TXT's `Disallow: /` to `Disallow: /admin/`; adding an `Allow: /r/` line to ROBOTS_TXT; changing the prefix test to `path.startswith("/.well-known")` with no trailing slash (must fail the /.well-knownx case); and pointing /favicon.ico at errorpages.ERROR_PAGES[404]; and `cd gui-pages && uv run pytest` passes cleanly afterwards with `git diff` showing no residue
- [ ] End-to-end manual verification against a running app (depends on the nonpages task) — file(s): (none — verification step) — done when: against a local `spin up --build --runtime-config-file runtime-config.toml`, `curl -s http://localhost:3000/robots.txt` prints the exact four-directive-and-comments file with `content-type: text/plain; charset=utf-8`, `x-content-type-options: nosniff` and `x-ss-version` present; /favicon.ico and /.well-known/change-password and the reliability-probe path each return 404 with content-type text/plain and a content-length under 100 (versus the styled page's ~1 KB, recorded for comparison); /.well-knownx and /nope and /admin/nope still return 404 with content-type text/html and render the styled card in a real browser; /login.html, /dashboard.html, /admin/ and /r/{a real slug} are all unchanged; and the browser console shows zero errors and zero CSP violations on a console page and on the styled 404
- [ ] Document the three paths in CLAUDE.md, spin.toml's favicon comment and PRODUCT.md (depends on every task above) — file(s): CLAUDE.md, spin.toml, PRODUCT.md — done when: CLAUDE.md's Architecture gui-pages bullet records that /robots.txt, /favicon.ico and /.well-known/* bypass the ROUTES allowlist via gui-pages/nonpages.py with SECURITY_HEADERS unchanged and no read_file call, that /.well-known/* needs a prefix test because ROUTES is exact-match only, and that robots.txt disallows everything including /r/ because redirect reads no User-Agent so a crawled short link is an unfilterable click; spin.toml's comment above the /favicon.svg route is corrected (all four redirect/*.html templates declare no <link rel="icon">, not just prompt.html) and records that no /favicon.ico is served on purpose; PRODUCT.md's Capabilities list gains one bullet on the crawl policy and its analytics-accuracy motive; and no DESIGN.md change is made
```

## Critical files

- `gui-pages/nonpages.py` (new)
- `gui-pages/routing.py`
- `gui-pages/tests/test_nonpages.py` (new)
- `gui-pages/tests/test_routing.py`
- `CLAUDE.md`
- `spin.toml` (comment only — no route added, moved or removed)
- `PRODUCT.md`

Explicitly **not** touched: anything under `gui/` (no new asset, no route, and so
no exposure to the `spin_static_fs` startup-snapshot staleness trap),
`gui-pages/app.py`, `gui-pages/errorpages.py`, `gui-pages/tests/test_no_inline_code.py`,
`gui-pages/tests/test_manifest_components.py`, `redirect/**`, `api/**`,
`DESIGN.md`, `Jenkinsfile`, `.impeccable/design.json`.

## Verification

Run in this order.

1. **The `gui-pages` suite:**
   ```bash
   cd gui-pages && uv run pytest
   ```
   **Pass:** all pass, above the **116-passed** baseline measured before this
   change.

2. **The mutation check** (a guard that cannot fail is not a guard). One at a
   time, apply, confirm a *named* test fails, revert:
   - `ROBOTS_TXT`'s `Disallow: /` → `Disallow: /admin/`
   - add `Allow: /r/` to `ROBOTS_TXT`
   - `path.startswith(WELL_KNOWN_PREFIX)` → `path.startswith("/.well-known")`
   - `/favicon.ico` → `(404, "text/html; charset=utf-8", ERROR_PAGES[404])`

   Then `git diff` to confirm no residue.

3. **The other two suites, because CI will run them** (neither is affected):
   ```bash
   cd redirect && go test ./linkgate/...
   cd api && uv run pytest
   ```
   Never `go test ./...`, `go build ./...` or `go vet ./...` — those fail by
   design with `wit_exports.go:934:6: missing function body`.

4. **Run the app** (from the repo root):
   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=devpassword SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```

5. **`robots.txt` end to end:**
   ```bash
   curl -sD - http://localhost:3000/robots.txt
   ```
   **Pass:** `200`; `content-type: text/plain; charset=utf-8`;
   `x-content-type-options: nosniff`, `x-frame-options: DENY` and `x-ss-version`
   all present; the body is the exact file from Decision 1, ending in a newline,
   with `Disallow: /` and no `Allow:` line.

6. **The cheap 404s, with the byte count that is the point:**
   ```bash
   for p in /favicon.ico /.well-known/change-password /.well-known/security.txt \
            "/.well-known/resource-that-should-not-exist-whose-status-code-should-not-be-200"; do
     curl -sD - -o /dev/null "http://localhost:3000$p" \
       | grep -iE '^(HTTP|content-type|content-length)'
   done
   ```
   **Pass:** `404` on all four, `content-type: text/plain; charset=utf-8`,
   `content-length` under 100 on each.

7. **The styled page is still what a person gets** — this is the over-match
   guard, and the one most likely to regress:
   ```bash
   for p in /nope /admin/nope /.well-knownx /.well-known-backup; do
     curl -sD - -o /dev/null "http://localhost:3000$p" \
       | grep -iE '^(HTTP|content-type|content-length)'
   done
   ```
   **Pass:** `404` with `content-type: text/html; charset=utf-8` and a
   `content-length` around 1 KB on all four. Record the two content-lengths side
   by side — that contrast is the whole change.

8. **Nothing else moved.** In a real browser with the console open: `/login.html`
   signs in, `/dashboard.html` renders its table, `/admin/` renders the hub, and a
   real `/r/{slug}` still `302`s. `http://localhost:3000/admin/nope` still renders
   the narrow centered card, fully styled, with **zero** CSP violations in the
   console.

CI (`Jenkinsfile`) runs `go test ./linkgate/...`, `cd api && uv run pytest` and
`cd gui-pages && uv run pytest` in parallel Docker stages and builds no Wasm.
Only the third is affected and no test invocation changes, so `Jenkinsfile` is
not in scope.

## Out of scope / follow-ups

- **`/.well-known/security.txt`.** Rejected above for want of a `Contact:` and an
  owner for the mandatory `Expires:` refresh. **Added to `TASKS.md`'s "Future
  work (not scheduled)"** with that trigger; the new module is where it would go.
- **An ACME `http-01` challenge responder.** Not needed — TLS terminates at
  Akamai. If a custom-domain cutover ever needs one, it is one exact-path entry
  in `NON_PAGE_RESPONSES`, but a *static* challenge response is unlikely to be
  what an ACME client needs; treat it as its own decision.
- **Per-domain `robots.txt`.** `gui-pages` never reads `Host` (like `redirect`),
  so every configured domain in `public_base_urls` gets the same file. Fine while
  the policy is `Disallow: /` for all of them; it stops being fine the moment one
  branded domain should be crawlable, which would be a real design change.
- **Whether an Akamai property fronting a custom domain serves this file or its
  own.** UNCONFIRMED above. Verify with `curl https://<domain>/robots.txt` after
  any custom-domain cutover.
- **`X-Robots-Tag`, and `noindex` on `redirect`'s 302.** Rejected in Trade-offs
  #6; the `redirect` half is out of scope by the task's non-goals besides.
- **Method handling.** `gui-pages` still ignores `request.method` entirely, so a
  `POST /robots.txt` returns the file and a `HEAD` returns a body the host may or
  may not strip. Unchanged by this plan and not worth changing — the same note
  `docs/plans/gui-pages-error-pages.md` already carries.
- **Bot-driven click inflation itself is not measured.** This plan asserts the
  mechanism (a crawler's fetch is recorded as a click, unfilterable because
  `redirect` reads no `User-Agent`) but has no live measurement of how much of a
  real link's total is bot traffic — there is no field that could tell you.
  `robots.txt` is the cheap, standard, non-invasive answer; anything stronger
  needs evidence first (Trade-offs #7).
