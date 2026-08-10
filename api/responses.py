"""Small shared helpers used across the api component's handlers.

`Request`/`Response` are local dataclasses rather than imports from
`spin_sdk.http` — the real `Handler.handle()` only ever accesses
`.status`/`.headers`/`.body` on whatever `handle_request` returns (no
`isinstance` check), so these behave identically at runtime while also being
importable under host Python. `spin_sdk.http`/`key_value`/`variables` fail at
import time outside the actual componentize-py build/run pipeline, so this is
what keeps every module except `app.py` (the real WASI entrypoint) unit-testable.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, MutableMapping, Optional


@dataclass
class Request:
    method: str
    uri: str
    headers: MutableMapping[str, str]
    body: Optional[bytes]


@dataclass
class Response:
    status: int
    headers: MutableMapping[str, str]
    body: Optional[bytes]


# This is a pure JSON API — nothing it returns should ever be rendered,
# executed, or framed by a browser, so the CSP can be maximally strict
# (`default-src 'none'`) unlike the GUI pages' CSP, which has to accommodate
# inline scripts. Every response goes through `json_response` below except
# `qr.py`'s image responses, which merge these headers in separately.
SECURITY_HEADERS: dict[str, str] = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "x-frame-options": "DENY",
    "strict-transport-security": "max-age=31536000; includeSubDomains",
    "content-security-policy": "default-src 'none'",
}


def json_response(status: int, data: dict[str, Any], headers: Optional[dict[str, str]] = None) -> Response:
    # SECURITY_HEADERS is applied last, not first — a code review found that
    # applying it first let any caller-supplied `headers` collide with and
    # silently override a security header with no warning. Not exploited by
    # any current call site (only `set-cookie` is ever passed), but the
    # ordering was backwards for a header set whose whole point is to be a
    # hard-to-bypass baseline. Applying it last guarantees these values
    # always win, regardless of what a future call site passes in.
    hdrs = {"content-type": "application/json"}
    if headers:
        hdrs.update(headers)
    hdrs.update(SECURITY_HEADERS)
    return Response(status, hdrs, json.dumps(data).encode("utf-8"))


def get_header(headers: dict[str, str], name: str) -> Optional[str]:
    name_lower = name.lower()
    for key, value in headers.items():
        if key.lower() == name_lower:
            return value
    return None


def parse_cookies(cookie_header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in cookie_header.split(";"):
        part = part.strip()
        if "=" in part:
            key, value = part.split("=", 1)
            cookies[key.strip()] = value.strip()
    return cookies


def to_iso8601_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_iso8601_utc_ms(dt: datetime) -> str:
    """Second-resolution loses real information for analytics events, which
    are recorded with `UnixMilli()` and routinely arrive several to a second.

    A separate function rather than a flag on `to_iso8601_utc`, because the
    two have different obligations. Link windows (`start_at`/`end_at`) are
    operator-entered datetimes that round-trip through `parse_iso8601_utc`
    and must keep their exact existing shape; an event timestamp is
    display-only and never parsed back. Widening the shared function would
    have put milliseconds into stored link records for no benefit.

    `parse_iso8601_utc` accepts this format anyway (`datetime.fromisoformat`
    handles fractional seconds), so nothing breaks if one is ever fed back.
    """
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def iso_now() -> str:
    return to_iso8601_utc(datetime.now(timezone.utc))


def parse_iso8601_utc(value: str) -> Optional[datetime]:
    """Parses an ISO8601 timestamp with an explicit timezone/offset, normalized to UTC.

    Rejects naive values (no tzinfo) — callers must always be explicit about
    timezone, whether that's the GUI's `toISOString()` output (`Z`-suffixed
    UTC) or another offset.
    """
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)
