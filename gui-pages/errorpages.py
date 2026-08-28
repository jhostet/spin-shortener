"""Styled 404/500 pages for gui-pages' catch-all `build_response` (routing.py).

Zero `spin_sdk` imports, host-importable — the same contract `routing.py`
holds. Both pages are rendered once, at import time, from one shared shell,
into plain `bytes` module constants — there is no runtime file read here (see
docs/plans/gui-pages-error-pages.md, Trade-offs #1, for why a file under
`gui/` fetched through the injected `read_file` was rejected: the 500 branch
exists precisely because that callable can fail, and fetching this page's own
markup through it would put a possible failure inside the failure handler).

Interpolation is from module constants only — no request data (path, status,
header) ever reaches `_render`. That is what makes these bodies safe to keep
`SECURITY_HEADERS` verbatim with no per-page CSP tightening: there is no
injection point to contain.

Absolute (leading-slash) asset paths are mandatory, not a style choice: this
component's catch-all serves a 404 at *any* depth (`/nope`, `/admin/nope`,
`/links/nope`), unlike every real page in `gui/`, which uses paths relative to
its own directory. A relative path here would resolve under the requested
depth instead of gui's root — e.g. `/admin/theme.css`, which is not a route on
the `gui` component, so it would 404 as `text/html` and `nosniff` would
correctly refuse to treat it as CSS. The failure is a silently unstyled page.

These pages DO load `/theme-init.js`, unlike `redirect`'s always-light error
pages (`redirect/error-404.html`/`error-500.html`, `script-src 'none'`). That
divergence is deliberate: every other page in this component loads it, this
component's CSP already permits `script-src 'self'`, and the degraded case
(script blocked or 404s) is identical to `redirect`'s behavior — no
`data-theme` gets set and `theme.css`'s unconditional light block renders the
page light (CLAUDE.md, Theming, "No-JS fallback"). Do not "harmonise" this
with `redirect`'s always-light pages in either direction.
"""

_SHELL = """<!doctype html>
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
"""


def _render(title: str, heading: str, lead: str, detail: str) -> bytes:
    return _SHELL.format(title=title, heading=heading, lead=lead, detail=detail).encode("utf-8")


# This 404 may be more specific than redirect's, and that is deliberate: a
# gui-pages path either is or isn't in the fixed, public ROUTES allowlist
# already in this repo, so there is no link-existence secret to protect here
# the way there is at /r/{slug} (CLAUDE.md, "Security tradeoffs"). Do not
# add a forbidden-word guard to this page, and do not relax redirect's.
NOT_FOUND_HTML: bytes = _render(
    title="Page not found",
    heading="Page not found",
    lead="There&rsquo;s no page at this address.",
    detail=(
        "If you were following a short link, check the address you were given "
        "&mdash; a short link&rsquo;s path starts with <code>/r/</code>."
    ),
)

# No "we've been notified": a ROUTES-vs-filesystem drift now emits one
# ev=page_read_failed line to stderr (docs/plans/gui-pages-failure-logging.md),
# but nothing monitors that stream, so "we've been notified" would still be a
# claim this app cannot honour. Naming the operator remains the honest ask. No
# "try again" either — this drift is permanent until a redeploy, not transient.
INTERNAL_ERROR_HTML: bytes = _render(
    title="Something went wrong",
    heading="Something went wrong",
    lead="This page couldn&rsquo;t be loaded.",
    detail="Reloading is unlikely to help &mdash; let whoever runs this service know.",
)

# The derivation surface the inline-code guard (test_no_inline_code.py) and
# routing.py's two error branches both use, matching PAGES/SCRIPTS/
# REDIRECT_TEMPLATES' own derived-not-hardcoded idiom: a third page added
# later is covered the moment it exists.
ERROR_PAGES: dict[int, bytes] = {404: NOT_FOUND_HTML, 500: INTERNAL_ERROR_HTML}
