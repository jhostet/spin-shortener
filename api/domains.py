"""Pure base-URL helpers for multi-domain display.

Domains are a viewer preference, not a stored/enforced property of a link —
see docs/plans/multi-domain-display.md. This module has zero `spin_sdk`
imports and takes no `store`, matching the testability rule the rest of
`api/` follows (see `CLAUDE.md`), the same shape as `links.py`'s pure
validators and `bulk.py`'s parser half.
"""

from urllib.parse import urlparse


def normalize_base_url(value: str) -> str | None:
    """`scheme://host[:port]`, lowercased, no trailing slash.

    None if `value` is not an absolute http/https origin, or carries a path,
    query or fragment.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None

    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return None
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return None
    if parsed.params:
        return None

    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def parse_base_urls(raw: str) -> list[str]:
    """Comma-separated origins -> normalized list.

    Order preserved; blanks, duplicates and malformed entries dropped. May
    return [].
    """
    if not isinstance(raw, str):
        return []

    result: list[str] = []
    seen: set[str] = set()
    for entry in raw.split(","):
        normalized = normalize_base_url(entry)
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def visible_base_urls(assigned: list[str], configured: list[str]) -> list[str]:
    """The domains a user may select, in *configured* order.

    An empty assignment -- or one that no longer intersects `configured` --
    means unrestricted: a user must always have at least one domain to hand
    out.
    """
    if not assigned:
        return list(configured)

    assigned_set = set(assigned)
    intersection = [domain for domain in configured if domain in assigned_set]
    return intersection if intersection else list(configured)


def resolve_base_url(candidate: str | None, configured: list[str]) -> str | None:
    """The exact `configured` entry matching `candidate`, or None if it is
    not configured.

    A falsy `candidate` selects `configured[0]`. Returns the server's own
    string, never the caller's.
    """
    if not candidate:
        return configured[0] if configured else None

    normalized = normalize_base_url(candidate)
    if normalized is None:
        return None

    for domain in configured:
        if domain == normalized:
            return domain
    return None


# The path segment the redirect component is routed on. It is a constant of
# this app's wire protocol, NOT configuration — spin.toml's
# `route = "/r/..."` and redirect/prompt.html's form action never change.
# Only whether a *displayed or encoded* URL includes it is configurable.
REDIRECT_PATH_PREFIX = "/r"


def parse_include_redirect_prefix(raw: str | None) -> bool:
    """True unless `raw` is exactly "false" (whitespace- and case-insensitive).

    Inverted relative to app.py's `cookie_secure` parse, deliberately: an
    unrecognised value must land on today's behaviour. Stripping /r/ from a
    deployment with no edge rewrite in front of it produces dead copied links
    and unrecallable printed QR codes.
    """
    if not isinstance(raw, str):
        return True
    return raw.strip().lower() != "false"


def short_url_for(base_url: str, slug: str, include_prefix: bool = True) -> str:
    """The short URL to display or encode. `base_url` carries no trailing
    slash by `normalize_base_url`'s construction, so exactly one slash is
    added either way."""
    prefix = REDIRECT_PATH_PREFIX if include_prefix else ""
    return f"{base_url}{prefix}/{slug}"
