"""Machine-facing responses that bypass the ROUTES allowlist entirely.

`/robots.txt`, `/favicon.ico` and every `/.well-known/*` path are requested by
software (crawlers, browsers' unprompted favicon probe, password managers,
ACME clients), not by a person who typed a URL — so they get a cheap, honestly
typed answer here instead of `routing.py`'s styled HTML 404 meant for a human.
Zero `spin_sdk` imports, so this stays host-importable under plain pytest, the
same convention `errorpages.py` and `routing.py` already follow. This module
deliberately does NOT import `routing` (that would be a cycle) and does not
build a `Response` itself — it returns a plain `(status, content_type, body)`
triple and lets `routing.build_response` assemble the actual response with
`SECURITY_HEADERS`, so those headers continue to be spread in exactly one
place in this component.

Why the served `robots.txt` comment names only `/r/` and not `/admin/`,
`/api/` or `/login.html`: `robots.txt` is famously read as a map of
interesting paths, and `Disallow: /` already names nothing. The `/r/`
rationale stays inside the served bytes anyway because it's the sentence that
stops a future maintainer "helpfully" narrowing the disallow rule back open
for short links — see the comment on ROBOTS_TXT below. `redirect` reads no
`User-Agent` header at all (the classifier was deleted along with the
recent-events write, docs/plans/drop-events-write.md), so a crawler that
fetches a published short link is recorded as a real click in
`analytics:count:<slug>:<shard>`, indistinguishable from a person's, forever.

Why there is no `/favicon.ico` binary: `redirect/prompt.html`'s CSP is
`default-src 'none'` (the strictest in the app, guarding the page where a
visitor types a link password), and a root `.ico` would be fetched there by
the browser's own unprompted probe — whether that succeeds depends on whether
the browser enforces `img-src` against a favicon request, which varies by
browser. The existing `/favicon.svg` already covers every page that declares
`<link rel="icon">` (all eight `gui/` pages), and SVG favicons are supported
by ~95% of browsers as of 2026 (Safari 15.4+). So `/favicon.ico` here is a
cheap plain 404, not a redirect to the SVG and not a 204 — 404 is the honest
answer (there is no such resource) and is what Akamai caches by default,
suppressing repeat origin hits for free.

This is also where a future `/.well-known/security.txt` or ACME `http-01`
challenge response would go, as a new exact-path entry in
NON_PAGE_RESPONSES — both were considered and left out of this change (see
docs/plans/robots-favicon-and-well-known.md's Trade-offs #5 and Out of scope).
"""

from typing import Optional

NonPageResponse = tuple[int, str, bytes]  # status, content-type, body

PLAIN_TEXT = "text/plain; charset=utf-8"

WELL_KNOWN_PREFIX = "/.well-known/"

NOT_FOUND: NonPageResponse = (404, PLAIN_TEXT, b"Not found\n")

# The comment lines below are part of the served bytes and are load-bearing —
# see the module docstring for why the /r/ rationale lives here rather than
# in a Python comment. Written one line per line so the served file's shape
# is visible in the source.
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

NON_PAGE_RESPONSES: dict[str, NonPageResponse] = {
    "/robots.txt": (200, PLAIN_TEXT, ROBOTS_TXT),
    "/favicon.ico": NOT_FOUND,
}


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
