"""QR code generation for GET /api/links/{slug}/qr.

Uses the pure-Python `qrcode` package (SVG output natively, PNG via the
pure-Python `pypng`-backed `PyPNGImage` factory) since componentize-py cannot
bundle C-extension packages like Pillow.

The QR always encodes the *short* link (`{public_base_url}/r/{slug}`), never
the raw target_url — encoding the target directly would let a scan bypass the
redirect, click analytics, and any password gate entirely.
"""

import io

import qrcode
import qrcode.image.pure as qr_pure
import qrcode.image.svg as qr_svg

from auth import Principal
from links import can_view, get_link
from responses import Response, json_response

BOX_SIZE_BY_PRESET = {"web": 6, "print": 20}


def _query_value(query: dict, key: str, default: str) -> str:
    values = query.get(key)
    return values[0] if values else default


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


async def handle_qr(store, principal: Principal, slug: str, query: dict, public_base_url: str):
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

    short_url = f"{public_base_url.rstrip('/')}/r/{slug}"
    body, content_type, ext = _render(short_url, fmt, size)

    headers = {"content-type": content_type}
    if download:
        headers["content-disposition"] = f'attachment; filename="{slug}-qr.{ext}"'

    return Response(200, headers, body)
