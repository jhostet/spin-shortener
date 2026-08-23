# Styled Visitor-Facing Error Pages for `/r/{slug}`

## Context

`/r/{slug}` is the only route in this application a member of the public ever
lands on, and every error it returns is a raw Go string. `redirect/main.go`
answers `DispositionNotFound` with `http.NotFound(w, r)` (`404 page not found`,
`text/plain`), `DispositionUnreadable` with `http.Error(w, "internal error",
500)`, and `serviceUnavailable` with `http.Error(w, "temporarily unavailable",
503)`. A marketing team hands these links out in ads and print; a dead campaign
link currently shows a recipient the default net/http string on an unstyled
white page.

The work is the filed Future-work entry at `TASKS.md` line 462 ("**A styled,
visitor-facing error page for `/r/{slug}` — both the 404 and the 503, never just
one**", raised 2026-08-19 while planning
`docs/plans/redirect-read-failure-not-404.md`, where it is rejected alternative
#6). That entry is the brief, and two things in it are load-bearing:

- **There is no measurement gate.** It was filed for scope discipline, not cost.
  The pattern to copy already exists and needs no new served file and no new
  `spin.toml` route.
- **Do the 404 and the 503 in one pass, never just one.** Styling only the 503
  would make a transient hiccup look polished while a dead link — by far the
  more common page on this route — still looks like a Go error.

What has changed since that entry was filed: `docs/plans/redirect-read-failure-not-404.md`
shipped (`cb4793d`), so this route now has **three** error statuses, not two. The
entry predates the 500 and does not mention it.

**Confirmed decisions (settled by the user before planning):**

- Presentation only. No status code changes, no change to `linkgate.Resolve` or
  the disposition mapping. The 302 success path and the 200 password prompt are
  untouched.
- No new `spin.toml` routes and no new served asset files — reuse
  `/vendor/pico.min.css` and `/theme.css`, which are already exact routes on the
  `gui` component.
- No JavaScript beyond (possibly) the existing `/theme-init.js`; no inline
  `<script>`, `<style>` or `style="…"` anywhere. The app has zero
  `'unsafe-inline'` and that stays true.
- The 404's copy must be identical for absent, disabled and out-of-window.
- No deploy. Local `spin up` verification only; deploys are the user's call.
- `gui-pages/tests/test_no_inline_code.py` must be extended to whatever new
  templates appear.

**Decisions this plan makes (each argued below):** the 500 gets a page too; all
three pages are static, data-free `go:embed`ed files rendered with no
`html/template` execution; they share one CSP; they load no script and therefore
always render light; and the 404's non-distinguishing copy is pinned by a
forbidden-word assertion in the existing Python guard, because no Go test can
reach it.

## Key technical facts confirmed during research

- **Three error statuses exist on this route today.** `redirect/main.go`'s
  `handleRedirectGet`/`handleRedirectPost` each switch on `linkgate.Resolve`'s
  disposition: `DispositionNotFound` → `http.NotFound`, `DispositionUnreadable`
  → `http.Error(..., 500)`, `DispositionUnavailable`/zero value →
  `serviceUnavailable(w)` (which sets `Retry-After: 2` then
  `http.Error(..., 503)`). Read directly from `redirect/main.go`.
- **`setSecurityHeaders(w)` runs in the `spinhttp.Handle` wrapper, before
  `mux.ServeHTTP`,** so `X-Content-Type-Options`, `Referrer-Policy`,
  `X-Frame-Options`, `Strict-Transport-Security`, `X-SS-Version` and
  `Cache-Control: no-store` are already on the header map when a handler runs.
  New pages inherit all six with no new code. Confirmed in `redirect/main.go`.
- **`Retry-After` is set inside `serviceUnavailable` *before* `http.Error` writes
  the header,** and `http.Error` is what calls `WriteHeader`. Any replacement must
  keep the same ordering — set `Retry-After` first, then write the status.
  Confirmed in `redirect/main.go`'s `serviceUnavailable`.
- **The `go:embed` + render pattern is `redirect/passwordgate.go`:**
  `//go:embed prompt.html` into a `string`, `template.Must(...Parse(...))` at
  package scope, and `renderPasswordPrompt(w, status, slug, errMsg)` sets
  `Content-Type`, then a page-specific `Content-Security-Policy`, then
  `w.WriteHeader(status)`, then `promptTemplate.Execute(w, ...)`.
- **The prompt page's CSP is** `default-src 'none'; script-src 'none'; style-src
  'self'; base-uri 'self'; form-action 'self' https: http:; frame-ancestors
  'none'`. The `form-action` scheme list exists **only** because a correct
  password answers the POST with a 302 and Chrome applies `form-action` to that
  redirect — read the comment in `passwordgate.go`. It must **not** be copied to
  a page with no form.
- **`prompt.html` links `/vendor/pico.min.css` and `/theme.css` with absolute
  paths,** because the page is served from `/r/{slug}` and relative paths would
  resolve under `/r/`. Both are exact `[[trigger.http]]` routes on `gui`
  (`spin.toml` lines 74 and 86), so styling cost no new routes and no new files.
- **`.auth-page` and `.auth-shell` already exist in `gui/theme.css`** (lines 957
  and 962): `body.auth-page { min-height: 100vh; display: flex; align-items:
  center; }` and `.auth-shell { max-width: 26rem; }`. `prompt.html` and
  `login.html` both use them. These pages can reuse them, so **no `gui/` file is
  edited by this plan and no new design token is introduced.**
- **A page with no script always renders light, and that is documented, not
  assumed.** CLAUDE.md's Theming section, "No-JS fallback": with no `data-theme`
  attribute ever set, `theme.css`'s unconditional light-block declarations win —
  `:root:not([data-theme="dark"])` and Pico's `@media (prefers-color-scheme:
  dark)` block's `:root:not([data-theme])` are equal specificity and `theme.css`
  loads second.
- **`go:embed`ed templates are NOT subject to the `spin_static_fs` staleness
  trap.** That trap (CLAUDE.md, "Commands": a `gui/` edit is invisible until
  `spin up` restarts, because `spin_static_fs` serves a startup snapshot) applies
  only to files served by the `gui` component. `redirect` is rebuilt by
  `spin up --build`, exactly like `api` and `gui-pages`. Worth stating because it
  has cost this project time before, and someone will reasonably worry about it.
- **`package main` is not host-testable at all.** `go test ./...`, `go build
  ./...` and `go vet ./...` fail by design with `wit_exports.go:934:6: missing
  function body`. The only Go test command is `go test ./linkgate/...` from
  `redirect/` (baseline confirmed green: `ok github.com/redirect/linkgate`). The
  only compile check available for `package main` is
  `go tool componentize-go build`.
- **`gui-pages/tests/test_no_inline_code.py` already reaches across components**
  and reads `redirect/prompt.html` via `REPO_ROOT = Path(__file__).resolve().parents[2]`.
  Its own philosophy is stated in its docstring: page and script lists are
  *derived* (from `ROUTES`, from an `rglob`) "so a page added to the component is
  covered automatically instead of being quietly exempt." Baseline confirmed
  green: 71 passed.
- **A Python test reading a Go source file is established precedent** —
  `api/tests/test_kvprefix.py` reads `redirect/linkgate/keys.go` to pin the
  cross-language prefix and `CountShards` equality.
- **The guard is deliberately dumb about comments.** `prompt.html`'s own comment
  says so: naming a `style=` attribute literally inside an HTML comment would trip
  it. New templates' comments must not contain the literal strings `<script`,
  `<style` or `style=`.
- **`resolve_test.go` pins the three 404 causes equal to *each other*, not to a
  response body.** `TestResolve_ErrorIsNilForEveryOtherDisposition` and the
  absent/disabled/out-of-window equality test operate on `Disposition` values.
  Nothing anywhere pins a rendered byte. That gap is exactly where a well-meaning
  "better error message" would land.
- **`dev/redirect-load.sh` and `dev/click-load.sh` inspect status codes only** —
  `grep -q '\[404\]'` on `hey`'s distribution, and `curl -s -o /dev/null -w
  '%{http_code}'` respectively. Neither reads a body, so neither needs a change.
  Confirmed by grep.
- **`Jenkinsfile` needs no change.** Its `gui-pages (Python)` stage runs
  `uv run pytest -v` in the repo checkout, which is how the existing
  `redirect/prompt.html` assertion already works. No test invocation changes, so
  `Jenkinsfile` is out of scope.
- **The KV explorer's write endpoint is `POST /api/stores/default {key, value}`** —
  recorded in `TASKS.md`'s task-2 note under "## Redirect read failures must not
  answer 404", found by grepping the served HTML for `fetch(`. This is how a
  corrupt `links:slug:<slug>` value gets written to force a 500.
- **Stripping `key_value_stores` from `[component.redirect]` in `spin.toml`
  produces a real 503** carrying `retry-after: 2` and `cache-control: no-store` —
  already done and verified live in that plan's task 3, with `git diff spin.toml`
  confirmed empty afterwards.
- **Akamai does not cache 500/502/503/504 by default; it does cache 404 for 10
  seconds** (`techdocs.akamai.com/property-mgr/docs/cache-http-error-responses`,
  fetched 2026-08-19 for the prior plan). `Cache-Control: no-store` is already on
  every response. This plan changes no status and no caching-relevant header.
- **UNCONFIRMED: what `GET /r/` (trailing slash, no slug) and a method mismatch
  (e.g. `PUT /r/abc`) actually return.** Go 1.25's `ServeMux` should match neither
  registered pattern for `/r/`, handing it to net/http's own `NotFoundHandler`
  (plain-text `404 page not found`, no CSP, no HTML), and should answer a method
  mismatch with its own `405`. Those responses come from `ServeMux`, not from this
  component's code, so they stay plain text after this change. To confirm: `curl
  -i localhost:3000/r/` and `curl -i -X PUT localhost:3000/r/abc` against a local
  run — a recorded observation in Verification, deliberately not a pass/fail, and
  see "Out of scope" for why it is not fixed here.
- **UNCONFIRMED: whether the Spin Go SDK suppresses a response body on a HEAD
  request.** Go 1.22+ `ServeMux` matches `HEAD` against a `GET` pattern, and
  `bufferingWriter.flush` has a comment acknowledging an empty body on HEAD.
  Today's `http.NotFound` writes its plain-text body on HEAD too, so a ~1 KB HTML
  body there is not a regression and needs no handling. To confirm if anyone
  cares: `curl -I localhost:3000/r/nosuchslug`.
- **UNCONFIRMED: what `Cache-Control` the `gui` component sends for
  `/theme.css` and `/vendor/pico.min.css`.** `spin.toml`'s `[component.gui]` sets
  no `CACHE_CONTROL` environment variable, so it is `spin_static_fs` v0.3.0's
  default. Tangential — it affects only whether a repeat visitor re-fetches two
  small static files — but it is the reason not to add a third subresource to
  these pages lightly.

## Redirect (Go) changes

Everything in this plan lands in the **`redirect` component (Go)**, and that
follows the language rule rather than bending it: these pages are rendered by the
handler on the `/r/...` hot path, in the component that already owns the only
HTML it serves (`prompt.html`). There is no Python option here — `api` and
`gui-pages` are not on this route.

**No `linkgate` change.** Nothing in this plan is logic: it is three byte
constants and a header-writing helper. `go test ./linkgate/...` output should be
identical before and after. See Trade-offs #4 for why the templates deliberately
do *not* move into `linkgate` to buy a Go test.

### New: `redirect/error-404.html`, `redirect/error-500.html`, `redirect/error-503.html`

Three **static, data-free** HTML files, status-named so the mapping is obvious at
a glance and so the Python guard can identify the 404 file by name. Each is a
near-copy of `prompt.html`'s shell with the form removed.

`redirect/error-404.html` — the exact intended content:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Link unavailable — spin-shortener</title>
  <!-- Absolute paths: this page is served from /r/{slug}, so relative ones
       would resolve under /r/. Both files are already exact routes on the gui
       static component, so styling this page cost no new routes and no new
       files. No script is loaded, deliberately: these pages keep script-src
       'none', so the page always renders the light theme regardless of OS
       preference (see docs/plans/redirect-error-pages.md).
       THE COPY BELOW IS A SECURITY PROPERTY, NOT A DRAFT. An absent slug, a
       disabled link and a link outside its [start_at, end_at) window all
       render THIS page, byte for byte. It must never say expired, disabled,
       scheduled or not yet active — that trio is deliberately
       indistinguishable (CLAUDE.md, "Security tradeoffs"). -->
  <link rel="stylesheet" href="/vendor/pico.min.css" />
  <link rel="stylesheet" href="/theme.css" />
</head>
<body class="auth-page">
  <main class="container auth-shell">
    <article>
      <hgroup>
        <h1>Link unavailable</h1>
        <p>This short link isn&rsquo;t available.</p>
      </hgroup>
      <p>Double-check the address, or ask whoever shared it for an up-to-date link.</p>
    </article>
  </main>
</body>
</html>
```

`redirect/error-503.html` — identical shell; `<title>Temporarily unavailable —
spin-shortener</title>`, `<h1>Temporarily unavailable</h1>`, hgroup paragraph
"We couldn&rsquo;t check this link just now.", body paragraph "Try again in a few
moments."

`redirect/error-500.html` — identical shell; `<title>Something went wrong —
spin-shortener</title>`, `<h1>Something went wrong</h1>`, hgroup paragraph "This
request couldn&rsquo;t be completed.", body paragraph "If someone shared this
link with you, let them know it isn&rsquo;t working."

**Copy rules, all three pages:**

- **No diagnostics.** No slug, no status code, no error id, no host message. The
  templates take no data at all, so there is nothing to interpolate and no
  path-input-into-HTML surface even under `html/template`'s escaping.
- **No "we've been notified".** It would be a lie: `emitFailureLine` is called
  only on the `open`/`get` failure arms, and `DispositionUnreadable` deliberately
  logs nothing (`redirect/main.go`, and the comment above `maxFailureDedupPairs`).
  Nobody is notified of a 500. See "Out of scope" — that gap is a real follow-up.
- **The 500 must not widen its disclosure.** The status code already reveals that
  the slug has a record (accepted, CLAUDE.md's "Security tradeoffs" bullet). The
  copy therefore says nothing about a link existing, a record being corrupt, or a
  retry helping — a 500 here is permanent until a human edits the record, so
  "try again" would be misleading.
- **The 503 tells the human what `Retry-After: 2` already tells the machine.**
  "Try again in a few moments" is the whole point of the header expressed in
  words.
- **Nothing in danger red.** The h1 uses the default heading treatment (Deep
  Navy). `theme.css`'s own comment above `.form-note` states the rule: "Red is
  reserved for 'something just failed'. Reusing it for a standing advisory makes
  a real failure visually identical to permanent furniture." A whole page whose
  only content is an error message is standing furniture, not a flash of
  feedback.
- **No links and no navigation.** A public visitor has no account, so a link to
  the login page or dashboard is useless to them and points the curious at the
  internal console. No favicon link either, which keeps the CSP free of an
  `img-src` directive (matching `prompt.html`, which also has none).

### New: `redirect/errorpage.go`

```go
//go:embed error-404.html
var notFoundHTML []byte

//go:embed error-500.html
var serverErrorHTML []byte

//go:embed error-503.html
var unavailableHTML []byte

// errorPageCSP is shared by all three pages, and is deliberately NOT a copy of
// the password prompt's.
const errorPageCSP = "default-src 'none'; script-src 'none'; style-src 'self'; " +
    "base-uri 'self'; form-action 'none'; frame-ancestors 'none'"

func writeErrorPage(w http.ResponseWriter, status int, page []byte)
func notFound(w http.ResponseWriter)
func internalError(w http.ResponseWriter)
```

- **No `html/template`.** There is nothing to interpolate, so the pages are
  `[]byte` written straight to the writer. This removes a template execution from
  the error path, and — more importantly — it is what makes the byte-identical
  404 *structural* rather than a thing someone has to remember. `passwordgate.go`
  keeps its `html/template` use; it has a real parameter (`.Slug`, `.Error`).
- **`writeErrorPage` sets, in this order:** `Content-Type: text/html;
  charset=utf-8`, `Content-Security-Policy: errorPageCSP`, `Content-Length:
  strconv.Itoa(len(page))`, then `w.WriteHeader(status)`, then `w.Write(page)`.
  `Content-Length` is computed from the same variable that is written, so it
  cannot drift, and setting it manually is already an established pattern here
  (`sendRedirectThenRecord` sets `Content-Length: 0`). Every other security
  header arrives from `setSecurityHeaders`.
- **The CSP differences from `prompt.html`'s, and why each one matters:**
  `form-action 'none'` instead of `'self' https: http:` — the prompt's scheme
  list exists solely so Chrome permits the 302 that answers its password POST;
  these pages have no form, so the narrowest possible value is correct, and
  copying the prompt's verbatim would needlessly permit cross-origin form
  submission from a public page. `script-src 'none'` is stated explicitly even
  though `default-src 'none'` already covers it, matching the prompt page's own
  explicitness and surviving any future widening of `default-src`. `base-uri` and
  `frame-ancestors` are listed because `default-src` does not cover them (only
  fetch directives) — the same code-review catch recorded in `passwordgate.go`.
- **One shared CSP, not three.** The Future-work entry says "give each its own
  CSP"; that requirement is met by *authoring a policy for these pages* rather
  than inheriting the prompt's. Three identical string constants would be three
  places for a drift to hide. All three pages are structurally identical — no
  script, no form, two stylesheets — so one constant is the honest expression of
  that.

### Modified: `redirect/main.go`

Four call-site replacements plus one helper body. No other change; no status
changes anywhere.

| location | today | after |
|---|---|---|
| `handleRedirectGet`, `DispositionNotFound` | `http.NotFound(w, r)` | `notFound(w)` |
| `handleRedirectGet`, `DispositionUnreadable` | `http.Error(w, "internal error", http.StatusInternalServerError)` | `internalError(w)` |
| `handleRedirectPost`, `DispositionNotFound` | `http.NotFound(w, r)` | `notFound(w)` |
| `handleRedirectPost`, `DispositionUnreadable` | `http.Error(w, "internal error", http.StatusInternalServerError)` | `internalError(w)` |
| `serviceUnavailable` | `w.Header().Set("Retry-After", retryAfterSeconds)` then `http.Error(w, "temporarily unavailable", http.StatusServiceUnavailable)` | same `Retry-After` line, then `writeErrorPage(w, http.StatusServiceUnavailable, unavailableHTML)` |

- **`Retry-After` must stay above the write.** `http.Error` was what latched the
  header; `writeErrorPage` now is. Setting it after would silently drop it, and
  no unit test can catch that (this is `package main`) — the Verification step
  that greps for `retry-after: 2` is the only guard.
- **`serviceUnavailable`'s existing doc comment stays**, including the
  accepted-imprecision paragraph about a permanently-failing `kv.Open`. It is
  still true.
- **`r` remains used** in both handlers (`r.PathValue`, `r.ParseForm`), so
  dropping `http.NotFound`'s `r` argument leaves no unused parameter.
- **Both handlers must stay structurally identical except for the
  `DispositionPrompt` arm** — a property CLAUDE.md's status-contract section
  states explicitly. Two identical one-line replacements per handler preserve it.
- **The traced path needs no special handling.** `bufferingWriter.Header()`
  returns the real, live header map, and it buffers `WriteHeader`/`Write`, so
  `Content-Type`, the CSP, `Content-Length` and `Retry-After` all land correctly
  and `Server-Timing` is still attached before the real first write.
- **The logging paths need no change.** `emitLogLine` already reports
  `status=404/500/503`, and `emitFailureLine` is untouched. Observability is
  unchanged, deliberately.

## Testing changes

### Modified: `gui-pages/tests/test_no_inline_code.py`

Extend the existing cross-component guard rather than adding a file. Four
changes, in the file's own derived-not-hardcoded idiom:

1. **Replace the single `PROMPT_HTML` constant with a glob** over
   `REPO_ROOT / "redirect"` for `*.html`, so a fifth template added later is
   covered the moment it exists. Add a `test_redirect_templates_discovered`
   asserting at least 4 files were found **and** that `prompt.html` is among them
   — a glob that silently matches nothing (or stops matching the prompt after a
   rename) would pass every test below, which the file already guards against
   twice (`test_pages_list_is_not_empty`, `test_scripts_list_is_not_empty`).
2. **Parametrize the existing four assertions** (`INLINE_SCRIPT`, `STYLE_BLOCK`,
   `STYLE_ATTR`, `EVENT_HANDLER`) over that list, replacing
   `test_password_prompt_has_no_inline_code`. Keep its failure messages' spirit:
   these pages' CSP is stricter than any GUI page's.
3. **Add a no-script-at-all assertion** — `re.compile(r"<script", re.I)` — over
   every `redirect/*.html`. This is strictly stronger than `INLINE_SCRIPT`
   (which permits `<script src=…>`) and it is what makes the always-light theming
   decision *enforced* rather than remembered: adding `<script
   src="/theme-init.js">` without also widening `errorPageCSP` would be blocked in
   the browser and caught by nothing else. `prompt.html` already satisfies it, so
   extending it to every template is free.
4. **Add a drift guard and the 404 copy guard.** For each `error-*.html`, assert
   both `"/vendor/pico.min.css"` and `"/theme.css"` appear **with the leading
   slash** (three near-identical files are exactly where a stylesheet update lands
   in one and not the others, and the leading slash is the served-from-`/r/`
   trap). For `error-404.html` only, assert the text contains none of
   `expire`, `expiring`, `disabled`, `schedul`, `not yet`, `inactive`, `deleted`,
   `no longer` (case-insensitive), with a docstring citing CLAUDE.md's "Security
   tradeoffs" and `redirect/linkgate/resolve_test.go`'s disposition-equality pin.

### Should a test pin the rendered bytes identical across the three 404 causes?

**No automated test can, and this plan says so rather than pretending otherwise.**
The three causes are distinguished nowhere except inside `linkgate.Resolve`, which
already collapses them into one `DispositionNotFound` (pinned by
`resolve_test.go`), and the rendering happens in `package main`, which is not
host-testable at all. There is no seam a Go test could reach and no HTTP surface
a Python test could call.

What replaces it, in three layers:

1. **Structure.** All three causes reach the *same* `notFound(w)` call, which
   writes a *data-free* `[]byte`. Byte identity is not maintained, it is
   unavoidable — there is no parameter through which a difference could enter.
2. **The copy guard** above, which is the one thing that could still go wrong: a
   future "more helpful" message that names expiry or disablement. That is caught
   in CI.
3. **A manual sha256 comparison** across four real requests (absent, disabled,
   window-ended, window-not-started), as its own numbered verification task.

## Documentation changes (builder tasks, not done by this plan)

- **CLAUDE.md, "Security response headers":** the `redirect` bullet currently
  describes the password prompt as "the one HTML `redirect` renders". Add the
  three error pages, their shared `errorPageCSP`, why `form-action` is `'none'`
  here and a scheme list there, and the always-light decision with its reason.
- **CLAUDE.md, "The `/r/{slug}` status contract":** note that 404, 500 and 503
  now render a styled HTML page instead of a Go string, that no status or header
  changed, and that the 404's copy is part of the indistinguishability property.
- **CLAUDE.md, "Tests":** the `test_no_inline_code.py` bullet says it "also
  covers `redirect/prompt.html`, the only served HTML outside `gui-pages`'
  `ROUTES`". After this change it covers every `redirect/*.html` by glob, and
  `prompt.html` is no longer the only one.
- **DESIGN.md, Layout:** the "Auth page" bullet calls it "the one page that
  breaks from the app-shell pattern". Four pages served by `redirect` now reuse
  `body.auth-page` + `.container.auth-shell`. Also record the error-copy rules
  (no danger red, no diagnostics, no distinguishing wording) under Components, so
  the next person editing the copy meets the constraint before they edit it.
- **No `.impeccable/design.json` change and no new token.** The pages introduce
  no colour, no size and no class that does not already exist.

## Trade-offs and rejected alternatives

**1. Leaving the 500 as a bare `http.Error` string while styling the 404 and the
503. Rejected.** Attractive on the entry's own scope logic: the 500 is the rarest
of the three by a wide margin (it needs a link record that will not parse, which
only a hand-edited store or a KV corruption produces — it has never been seen
outside a deliberately-corrupted local key), and unlike the other two it implies
*operator* action rather than visitor action. It loses for the exact reason the
entry's own scope rule exists: leaving one of three as a raw Go string reproduces
the inconsistency the rule was written to prevent, one status later. It is also
the cheapest of the three to include — one more static file and one more
one-line call-site swap, with the CSP, the helper and the guard already paid for.
The rarity argument cuts the other way too: the 500 is the page most likely to be
seen at the *worst* moment, by a recipient of a campaign link an operator has not
yet noticed is broken.

**2. Loading `/theme-init.js` so the pages follow the OS light/dark preference.
Rejected; the pages always render light.** Genuinely available for the first
time — the reason `prompt.html` refuses it (it is the app's one credential-entry
page, so `script-src 'none'` is worth more than theme-following) does not apply
to a page with no credential on it. It loses on four counts. (a) It widens the
strictest CSP in the app from `script-src 'none'` to `'self'`, on the most
exposed route the app has, to buy an aesthetic. (b) These are public pages: a
visitor who has never used the console has no `ss-theme` key, so the feature
reduces to "follow the OS", which is nice-to-have, not a fix. (c) It adds a
render-blocking subresource to a page whose 503 variant is *by definition* served
when the system is degraded; an error page that depends on one more fetch to look
right is the wrong shape, and `theme-init.js` 404ing produces exactly the
unstyled flash `gui/app.js`'s `window.ssTheme` guard exists to contain elsewhere.
(d) Consistency inside one component: a visitor hitting a password prompt and
then an error page would see two different themes from the same route. Cost to
change one's mind later, recorded so it stays cheap: one `<script
src="/theme-init.js">` line per template, `script-src 'self'` in `errorPageCSP`,
and loosening the no-script-at-all guard — that last one is the point, because it
makes the reversal visible in a diff instead of silent.

**3. One parameterized `error.html` with the copy in Go constants. Rejected in
favour of three static files.** Attractive: one embed, one guard entry, no
duplicated `<head>` block across three near-identical files. It loses on two
things. First, it moves the copy out of the file a human edits copy in and into
Go string constants, which puts the 404 copy guard out of reach of the HTML-file
guard that already exists (it would have to read a `.go` file — possible, per
`api/tests/test_kvprefix.py`'s precedent, but a worse fit). Second, it
reintroduces a template parameter on the one page whose defining property is that
it must render identically for three different causes; "the template takes no
data" is a stronger guarantee than "we always pass the same data". The
duplication cost is real and is mitigated by the stylesheet-drift assertion in
the guard.

**4. Moving the templates and a `Disposition → (status, page)` mapping into
`redirect/linkgate/` so a Go test could pin it. Rejected.** `go:embed` is stdlib,
so `linkgate` could legally hold the files and stay free of `spin-go-sdk`
imports, and it would buy the only host-runnable Go assertion available here.
What it would actually pin is thin: `Resolve` already collapses the three 404
causes into one disposition (pinned), so a mapping test can only assert that one
disposition maps to one page — which is true by construction of a `switch` in
`main.go` either way. Against that it drags served HTML out of the component root
where `prompt.html` lives, splits the two kinds of served HTML across two
directories, makes the Python guard's glob follow, and puts presentation assets
inside the package whose stated purpose is pure logic. The honest position is that
this change has no logic in it and therefore no Go test.

**5. Registering `mux.HandleFunc("/r/", ...)` so `GET /r/` and method mismatches
also get a styled page. Rejected.** `ServeMux` currently answers `/r/` (no slug)
from net/http's own `NotFoundHandler` and a `PUT /r/abc` with its own `405`, both
plain text. A subtree pattern would catch them — but a method-mismatched request
matches only the subtree pattern, so a `405` would silently become whatever the
new handler returns, i.e. a status change, which this plan's non-goals forbid
outright. Getting it right needs method-aware routing, which is beyond
"presentation only". Left as an accepted residual (see Out of scope), on the
reasoning that neither shape is one a human reaches by clicking a shared link.

**6. Do nothing; leave the entry filed. Rejected, and it was a live option** —
there is no measurement gate and no correctness defect here, and the entry
survived being filed twice. It loses because the cost is genuinely small (three
static files, one helper, four one-line swaps, one test extension, no new route,
no new asset, no new token, zero hot-path KV work) and because the failure it
addresses is the most publicly-visible surface the product has: a recipient of a
printed campaign URL, holding a phone, looking at `404 page not found`.

**7. Adding a "report this" mailto or contact link. Rejected.** No contact
address exists in configuration, adding one means a new Spin variable read on the
error path, and an operator address on a public page is an invitation to scrape
it. The 500's copy pushes the visitor at the person who shared the link, who is
the one with an account and a path to an operator.

**8. `<meta name="color-scheme" content="light">`. Rejected as unnecessary.**
`index.html` uses `light dark` because it is a script-driven stub with no
stylesheet. These pages ship a stylesheet whose light block is unconditional, and
DESIGN.md's own "Don't" warns against reasoning about `color-scheme` as if it
gated anything. It would only nudge native scrollbar/autofill rendering, and
these pages have neither.

## Tasks

The exact unchecked lines appended to `TASKS.md` under
`## Styled visitor-facing error pages for /r/{slug}` (appended immediately above
the `# START HERE — session handoff, 2026-08-18` block, which stays last).
`TASKS.md` is authoritative; checkbox state is not maintained here.

```
- [ ] Add the three embedded error-page templates and the writeErrorPage helper, with main.go untouched — file(s): redirect/error-404.html, redirect/error-500.html, redirect/error-503.html, redirect/errorpage.go — done when: `cd redirect && go tool componentize-go build` succeeds, each template links `/vendor/pico.min.css` and `/theme.css` with leading slashes and contains no `<script`, `<style`, `style=`, `on<event>=` or any interpolated value, `errorPageCSP` is one shared const reading `default-src 'none'; script-src 'none'; style-src 'self'; base-uri 'self'; form-action 'none'; frame-ancestors 'none'`, and `git diff redirect/main.go` is empty
- [ ] Serve the three pages from both /r/{slug} handlers, replacing http.NotFound and http.Error (needs the task above) — file(s): redirect/main.go — done when: no status code anywhere changes, `Retry-After` is still set before the status is written in `serviceUnavailable`, both handlers still differ only in their DispositionPrompt arm, and against a local `spin up --build` an absent slug returns `404` with `content-type: text/html; charset=utf-8` and the rendered page while `curl -sI` shows `cache-control: no-store`, `x-ss-version` and the error-page CSP on it
- [ ] Extend the inline-code guard to every redirect/*.html by glob and pin the 404 copy as non-distinguishing — file(s): gui-pages/tests/test_no_inline_code.py — done when: `cd gui-pages && uv run pytest` passes, the template list is a glob asserting at least 4 files including prompt.html, no `<script` in any form appears in any of them, each error-*.html links both stylesheets with a leading slash, error-404.html contains none of expire/expiring/disabled/schedul/not yet/inactive/deleted/no longer, and inserting `<script src="/theme-init.js">` into one template and the word "expired" into error-404.html each fails a named test (report the mutation result)
- [ ] Verify the 404 is byte-identical for absent, disabled, window-ended and window-not-started — file(s): (none — verification step) — done when: against a local `spin up --build` with a disabled link, an ended-window link and a not-yet-started link created via the API, `curl -s` of those three plus a never-existent slug produce ONE distinct sha256 across all four bodies, all four return 404, and their headers differ in nothing but Date
- [ ] End-to-end manual verification of all three error pages in a real browser — file(s): (none — verification step) — done when: the 404, 503 and 500 each render the narrow centered card with Pico + theme.css applied, DevTools Console shows zero CSP violations on each, all three render light with no data-theme attribute on <html>, the 503 (forced by removing key_value_stores from [component.redirect]) carries `retry-after: 2` and the 500 (forced by writing `not json` into `links:slug:<slug>` via dev/kv-explorer-up.sh) carries no Retry-After, and `git diff spin.toml` is empty afterwards
- [ ] Record the error pages in CLAUDE.md and DESIGN.md — file(s): CLAUDE.md, DESIGN.md — done when: CLAUDE.md's "Security response headers" section names the three pages with their shared CSP, why form-action is 'none' here and a scheme list on the prompt, and the always-light decision; its "/r/{slug} status contract" section notes all three statuses now render HTML with no status or header change; its "Tests" section says test_no_inline_code.py covers every redirect/*.html by glob rather than prompt.html alone; and DESIGN.md's Layout "Auth page" bullet no longer calls it "the one page", with the error-copy rules (no danger red, no diagnostics, no wording that distinguishes the three 404 causes) recorded under Components
```

## Critical files

- `redirect/error-404.html` (new)
- `redirect/error-500.html` (new)
- `redirect/error-503.html` (new)
- `redirect/errorpage.go` (new)
- `redirect/main.go`
- `gui-pages/tests/test_no_inline_code.py`
- `CLAUDE.md`
- `DESIGN.md`

Explicitly **not** touched: `spin.toml` (no new route; the temporary strip during
verification must be reverted), anything under `gui/` (no new asset, no new token,
and therefore no exposure to the `spin_static_fs` staleness trap),
`redirect/linkgate/**`, `redirect/passwordgate.go`, `redirect/prompt.html`,
`dev/*.sh`, `Jenkinsfile`, `api/**`, `gui-pages/routing.py`.

## Verification

Run in this order.

1. **Compile `package main`** — the only compile check available for it:
   ```bash
   cd redirect && go tool componentize-go build
   ```
   **Pass:** builds, producing `main.wasm`. Never `go build ./...` or
   `go vet ./...` — they fail by design with `wit_exports.go:934:6: missing
   function body`.

2. **The Go test suite, unchanged by this plan:**
   ```bash
   cd redirect && go test ./linkgate/...
   ```
   **Pass:** `ok github.com/redirect/linkgate`. If this changes, something moved
   into `linkgate` that this plan says should not.

3. **The extended guard:**
   ```bash
   cd gui-pages && uv run pytest
   ```
   **Pass:** all pass (71 before this change; expect more, from the parametrized
   template list). Then the mutation check the task requires: temporarily insert
   `<script src="/theme-init.js">` into `redirect/error-503.html` and the word
   `expired` into `redirect/error-404.html`, confirm a *named* test fails for
   each, and revert both. A guard that cannot fail is not a guard.

4. **Run the app** (from the repo root; the bootstrap password is required every
   run, and `COOKIE_SECURE=false` is needed for browser login over plain HTTP):
   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=devpassword SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```

5. **The 404, in `curl` and in a browser.** With `$S` an absent slug:
   ```bash
   curl -sD - -o /tmp/404.html "http://localhost:3000/r/nosuchslug" \
     | grep -iE '^(HTTP|content-type|content-length|content-security-policy|cache-control|x-ss-version|x-frame-options|retry-after)'
   ```
   **Pass:** `404`, `content-type: text/html; charset=utf-8`,
   `content-security-policy: default-src 'none'; script-src 'none'; style-src
   'self'; base-uri 'self'; form-action 'none'; frame-ancestors 'none'`,
   `cache-control: no-store`, `x-ss-version`, `x-frame-options: DENY`, **no**
   `retry-after`. Then load `http://localhost:3000/r/nosuchslug` in Chrome:
   the narrow centered card renders with Pico + `theme.css`, the DevTools Console
   is empty of CSP violations, and `document.documentElement.dataset.theme` is
   `undefined` (light, no script ran).

6. **The 404 is byte-identical for all four causes.** Log in, create three links
   via the dashboard or `POST /api/links`: one then Disabled (bulk Disable is the
   only status toggle in the GUI), one with `end_at` in the past, one with
   `start_at` in the future. Then:
   ```bash
   for s in nosuchslug <disabled> <ended> <future>; do
     curl -s "http://localhost:3000/r/$s" | shasum -a 256
   done
   ```
   **Pass:** four identical digests. This is the security property; a difference
   here is a probing-resistance regression, not a cosmetic bug.

7. **The 500.** Start the app with the KV explorer instead:
   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=devpassword \
   SPIN_VARIABLE_KV_EXPLORER_PASSWORD=devkvpw \
   SPIN_VARIABLE_COOKIE_SECURE=false \
     ./dev/kv-explorer-up.sh
   ```
   Write a corrupt record through the explorer's own endpoint (the shape recorded
   in `TASKS.md`'s "Redirect read failures must not answer 404" task-2 note):
   `POST http://localhost:3000/internal/kv-explorer/api/stores/default` with
   `{"key": "links:slug:tcorru", "value": "not json"}` and basic auth `kv` /
   `devkvpw`. Then `curl -sD - -o /tmp/500.html http://localhost:3000/r/tcorru`.
   **Pass:** `500`, `text/html`, the error-page CSP, `cache-control: no-store`,
   **no** `retry-after`; the browser renders the "Something went wrong" card with
   an empty CSP console; the body mentions no slug, no record, no notification.

8. **The 503.** Temporarily remove the `key_value_stores` line from
   `[component.redirect]` in `spin.toml`, restart, then:
   ```bash
   curl -sD - -o /tmp/503.html "http://localhost:3000/r/anything" \
     | grep -iE '^(HTTP|retry-after|cache-control|content-type|content-security-policy)'
   ```
   **Pass:** `503`, **`retry-after: 2`**, `cache-control: no-store`, `text/html`,
   the error-page CSP; the browser renders the "Temporarily unavailable" card
   with an empty CSP console. Then restore the manifest and confirm
   `git diff spin.toml` is empty — a stripped `key_value_stores` committed by
   accident breaks the whole app.

9. **The success paths still work, byte for byte in behaviour.** With the
   manifest restored: an active link returns `302` with `Location` and
   `content-length: 0`; a password-protected link returns `200` with the prompt
   page and its **own** CSP (`form-action 'self' https: http:`, unchanged); a
   correct password still redirects in a real browser, not just under `curl` —
   that distinction is exactly how the `form-action` bug was originally missed.

10. **Record, do not gate:** `curl -i http://localhost:3000/r/` and
    `curl -i -X PUT http://localhost:3000/r/abc`. Note what `ServeMux` returns
    for each (expected: a plain-text `404` and a `405`, neither styled). This
    resolves the UNCONFIRMED fact above and feeds the Out-of-scope entry; it is
    not a pass/fail for this work.

CI runs `go test ./linkgate/...`, `cd api && uv run pytest` and
`cd gui-pages && uv run pytest` in parallel Docker stages and builds no Wasm.
Only the third is affected, and no test invocation changes, so `Jenkinsfile` is
not in scope.

## Out of scope / follow-ups

- **`ServeMux`'s own `404` for `GET /r/` and `405` for a method mismatch stay
  plain text.** Accepted residual, argued in Trade-offs #5: covering them means
  either changing a status (forbidden here) or adding method-aware routing.
  Trigger to revisit: evidence anyone actually lands on `/r/` with no slug — a
  `log_level=summary` build's lines would not even show it, since those requests
  never reach a handler that logs.
- **`gui-pages`' catch-all returns plain text too.** `gui-pages/routing.py`'s
  `build_response` answers an unknown path with `Response(404, ..., b"Not
  found")` and a `ROUTES`-vs-filesystem drift with `b"Internal error"`. A visitor
  who drops the `/r/` from a shared URL lands there and sees exactly the raw
  string this plan removes from `/r/`. Out of scope (different component,
  different language, and the requirement named `/r/{slug}`), but it is the same
  defect and it now has a page to reuse — `gui-pages`' CSP already permits
  `style-src 'self'` and both stylesheets are routed. **Added to `TASKS.md`'s
  "Future work (not scheduled)"** with a trigger.
- **An unreadable link record produces no log line at all.** `emitFailureLine` is
  called only on the `open`/`get` failure arms; `DispositionUnreadable` logs
  nothing, so a 500 is discoverable only through `GET /api/admin/consistency`'s
  `unreadable_value` finding. This plan's 500 page tells the visitor to report it
  to whoever shared the link — which is the right advice precisely *because* the
  operator has no signal of their own. Observability is a non-goal here, so it is
  **added to `TASKS.md`'s "Future work (not scheduled)"** with a trigger.
- **No change to `redirect`'s KV work, op counts, statuses, headers or
  observability.** A successful redirect stays at 5 KV operations, a miss at 2.
  These pages are bytes and headers.
- **The two extra subresource requests these pages trigger** (`/vendor/pico.min.css`,
  `/theme.css`) go to the `gui` component, which does no KV work, so a styled
  error page adds nothing to the read or write cap the 503 exists to report on.
  Whether those two are browser-cached depends on `spin_static_fs`'s default
  `Cache-Control` — UNCONFIRMED above, and not worth chasing for this change.
- **The 404 body grows from ~19 bytes to ~1 KB.** Irrelevant against Akamai's
  10 MiB response limit, and worth noting only because the 404 is the *cheap*
  path under saturation (2 KV operations, no write) — this adds bytes on the
  wire, not origin work.
- **No copy review with a real stakeholder.** The wording above is chosen against
  PRODUCT.md's audience and the constraints in this document; if marketing wants
  different words, the 404's are the ones that must be re-checked against the
  indistinguishability rule before they change.
