"""QR code generation for GET /api/links/{slug}/qr.

Uses the pure-Python `qrcode` package (SVG output natively, PNG via the
pure-Python `pypng`-backed `PyPNGImage` factory) since componentize-py cannot
bundle C-extension packages like Pillow.

The QR always encodes the *short* link (`{base_url}/r/{slug}`, or
`{base_url}/{slug}` when `include_redirect_prefix` is off — see
docs/plans/toggleable-redirect-prefix.md), never the raw target_url —
encoding the target directly would let a scan bypass the redirect, click
analytics, and any password gate entirely. `base_url` is resolved (never
trusted directly from the caller) against the configured `public_base_urls`
list via `domains.resolve_base_url` — see docs/plans/multi-domain-display.md
for why an unvalidated client-supplied base URL is a QR-poisoning vector.
"""

import io

import qrcode
import qrcode.image.pure as qr_pure
import qrcode.image.svg as qr_svg

import domains
from auth import Principal
from links import CUSTOM_SLUG_PATTERN, can_view, get_link
from responses import Response, SECURITY_HEADERS, json_response

BOX_SIZE_BY_PRESET = {"web": 6, "print": 20}


def _query_value(query: dict, key: str, default: str) -> str:
    values = query.get(key)
    return values[0] if values else default


def _safe_filename_slug(slug: str) -> str:
    """Returns slug unchanged if it matches CUSTOM_SLUG_PATTERN, otherwise the
    fixed placeholder "link", carrying none of the original bytes.

    A slug reaching this function is already existence-checked against a
    stored `links:slug:<slug>` record (get_link above), and only `api` ever
    writes one, always under this exact pattern — so an attacker-crafted
    slug cannot reach this field today. Sanitized anyway, for the same
    reason `linkgate.SanitizeSlugForLog`/`obs.sanitize_slug_for_log` sanitize
    their identical field: storage can still drift by a route that bypasses
    normal authoring (`backup.handle_restore` writes records without
    re-validating their keys by design, and a hand-edited store can contain
    anything), and this is the one place `slug` is emitted verbatim into a
    header value with no other guard — the same "verbatim into a header the
    Go SDK serializes unvalidated" shape `docs/plans/reject-control-chars-in-target-url.md`
    just closed for target_url, one field over.
    """
    if CUSTOM_SLUG_PATTERN.match(slug):
        return slug
    return "link"


def _render(short_url: str, fmt: str, size: str) -> tuple[bytes, str, str]:
    box_size = BOX_SIZE_BY_PRESET.get(size, BOX_SIZE_BY_PRESET["web"])
    buf = io.BytesIO()

    if fmt == "svg":
        img = qrcode.make(short_url, image_factory=qr_svg.SvgPathImage, box_size=box_size)
        img.save(buf)
        return buf.getvalue(), "image/svg+xml", "svg"

    img = qrcode.make(short_url, image_factory=qr_pure.PyPNGImage, box_size=box_size)
    img.save(buf)
    return buf.getvalue(), "image/png", "png"


async def handle_qr(store, principal: Principal, slug: str, query: dict, base_urls: list[str],
                    include_redirect_prefix: bool = True):
    record = await get_link(store, slug)
    if record is None:
        return json_response(404, {"error": "not_found"})
    if not can_view(principal, record):
        return json_response(403, {"error": "forbidden", "required_permission": "links.view_all"})

    fmt = _query_value(query, "format", "svg")
    if fmt not in ("svg", "png"):
        return json_response(400, {"error": "invalid_format"})

    size = _query_value(query, "size", "web")
    if size not in BOX_SIZE_BY_PRESET:
        return json_response(400, {"error": "invalid_size"})

    download = _query_value(query, "download", "0") == "1"

    if not base_urls:
        return json_response(500, {"error": "no_base_url_configured"})

    base_url = domains.resolve_base_url(_query_value(query, "base", ""), base_urls)
    if base_url is None:
        return json_response(400, {"error": "invalid_base_url"})

    short_url = domains.short_url_for(base_url, slug, include_redirect_prefix)
    body, content_type, ext = _render(short_url, fmt, size)

    # SECURITY_HEADERS applied last (see responses.json_response's comment) —
    # neither key added here collides today, but this keeps the same
    # can-never-be-overridden guarantee if that ever changes.
    headers = {"content-type": content_type}
    if download:
        headers["content-disposition"] = f'attachment; filename="{_safe_filename_slug(slug)}-qr.{ext}"'
    headers.update(SECURITY_HEADERS)

    return Response(200, headers, body)
