"""Small shared helpers used across the api component's handlers."""

import json
from datetime import datetime, timezone
from typing import Any, Optional

from spin_sdk.http import Response


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


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
