"""Pure routing logic for the gui-pages component.

No `spin_sdk` imports, so this stays host-importable and unit-testable
under plain `pytest` — `app.py` (the real WASI entrypoint) does the actual
file read and is excluded from the test suite the same way `api/app.py` is,
per this repo's established convention.
"""

from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse

# Path -> file relative to the gui/ directory mounted at /gui in this
# component's virtual filesystem (see spin.toml's [component.gui-pages]
# `files` mapping). Only these exact paths are served; everything else is
# a 404. This is a fixed, known allowlist, not a general-purpose static
# file server — there's no dynamic filesystem path resolution against
# request input, so there's no path-traversal surface to defend here the
# way a real static-file-server implementation would need to.
ROUTES: dict[str, str] = {
    "/": "index.html",
    "/index.html": "index.html",
    "/login.html": "login.html",
    "/dashboard.html": "dashboard.html",
    "/admin/users.html": "admin/users.html",
    "/links/detail.html": "links/detail.html",
}

# Security headers are only meaningful on the navigated document itself —
# they govern framing/loading/referrer behavior for the page — not on the
# .js/.css subresources it loads, which still route to the original
# spin_static_fs.wasm component (see spin.toml), unchanged.
#
# The CSP here is a deliberate, disclosed tradeoff, not an oversight: the
# GUI's pages carry roughly 700 lines of inline <script>/<style> across
# them, and rewriting all of that to external files + nonces was out of
# scope for this pass (see TASKS.md's future-work note) — so script-src/
# style-src include 'unsafe-inline' rather than blocking inline script
# execution outright. Every other directive is locked down for real: no
# plugins/objects, no framing, no cross-origin form submission, no
# base-tag hijacking.
SECURITY_HEADERS: dict[str, str] = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "x-frame-options": "DENY",
    "strict-transport-security": "max-age=31536000; includeSubDomains",
    "content-security-policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        # Pico CSS renders several UI affordances (sortable-column chevrons,
        # the search-box icon, datetime-local's calendar icon) as inline
        # data:image/svg+xml background-images, not <img> tags — confirmed
        # live: img-src 'self' alone blocked all of them with real CSP
        # violation errors, a genuine regression caught only by loading the
        # actual page, not by reading the CSS.
        "img-src 'self' data:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
}


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


def resolve_file(path: str) -> Optional[str]:
    """Maps a request path (no query string) to a file under gui/, or None for unknown paths."""
    return ROUTES.get(path)


def build_response(uri: str, read_file: Callable[[str], bytes]) -> Response:
    """`read_file` is injected (rather than reading the filesystem directly)
    so this function stays testable with a fake in unit tests — the real
    WASI entrypoint (`app.py`) passes in an actual file read."""
    path = urlparse(uri).path
    filename = resolve_file(path)
    if filename is None:
        return Response(404, {**SECURITY_HEADERS, "content-type": "text/plain; charset=utf-8"}, b"Not found")

    # A code review flagged that an unguarded read_file() call means a
    # ROUTES-vs-filesystem drift (a page renamed/removed on one side but not
    # the other) would raise unhandled and propagate out without
    # SECURITY_HEADERS attached — the one guarantee this component exists to
    # provide. ROUTES and the real files match today, but this is cheap
    # insurance against that drift.
    try:
        body = read_file(filename)
    except OSError:
        return Response(500, {**SECURITY_HEADERS, "content-type": "text/plain; charset=utf-8"}, b"Internal error")

    headers = {**SECURITY_HEADERS, "content-type": "text/html; charset=utf-8"}
    return Response(200, headers, body)
