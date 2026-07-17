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


def json_response(status: int, data: dict[str, Any], headers: Optional[dict[str, str]] = None) -> Response:
    hdrs = {"content-type": "application/json"}
    if headers:
        hdrs.update(headers)
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
