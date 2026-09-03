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


# --- Per-link domain restriction (docs/plans/per-link-domain-restriction.md) ---
#
# allowed_domains (on a LINK record) is a different concept from
# assigned_domains (on a USER record) and the vocabulary collides:
#
#   assigned_domains -- which domains a user's nav selector OFFERS them.
#                        Enforced nowhere server-side (a convenience
#                        guardrail, not a security control).
#   allowed_domains  -- which domains a LINK actually RESOLVES on. Enforced
#                        in redirect, on the hot path, per request.
#
# Neither reads the other. A user assigned one domain can still be handed a
# link restricted to another.


def normalize_allowed_domains(
    value,
    configured: list[str],
    also_allowed: list[str] | None = None,
) -> tuple[list[str] | None, str | None]:
    """Canonicalize a link's allowed_domains, or say why it is invalid.

    Returns (canonical_list, None), or (None, "invalid_allowed_domains").

    `value` may be None or [] (both meaning "unrestricted"), or a list of
    strings. Each member is put through normalize_base_url first, so a
    differently-cased or trailing-slashed form of a configured domain is
    accepted and STORED IN THE SERVER'S OWN CANONICAL FORM, never the
    caller's -- the same property resolve_base_url carries for ?base=.

    `configured` is the deployment's public_base_urls and is the membership
    test. `also_allowed` is the record's CURRENT stored list on an UPDATE
    path only: an entry already in the record stays valid even after an
    operator removes that domain from public_base_urls, so a stale entry can
    be kept or deliberately removed, but can never be silently widened away
    and can never make the record unsaveable. Create, bulk-create and the
    bulk `restrict` action pass nothing.

    Order is `configured` order first, then any retained `also_allowed`
    entry in the order given, de-duplicated -- so two equivalent submissions
    produce byte-identical records.
    """
    if value is None or value == []:
        return [], None

    if not isinstance(value, list) or not all(isinstance(d, str) for d in value):
        return None, "invalid_allowed_domains"

    normalized_members: list[str] = []
    for member in value:
        normalized = normalize_base_url(member)
        if normalized is None:
            return None, "invalid_allowed_domains"
        normalized_members.append(normalized)

    member_set = set(normalized_members)
    also_allowed_set = set(also_allowed or [])
    unconfigured = member_set - set(configured) - also_allowed_set
    if unconfigured:
        return None, "invalid_allowed_domains"

    result: list[str] = []
    for domain in configured:
        if domain in member_set and domain not in result:
            result.append(domain)
    for domain in also_allowed or []:
        if domain in member_set and domain not in result:
            result.append(domain)

    return result, None


def base_url_allowed_for_link(base_url: str, allowed: list[str] | None) -> bool:
    """The Python twin of linkgate.HostAllowed -- empty/None `allowed` means
    unrestricted (True); otherwise membership is by HOSTNAME only, ignoring
    scheme and port, matching `redirect`'s own matching rule exactly.

    Deliberately NOT pinned against the Go implementation (unlike keys.go's
    prefixes/CountShards, which fail silently at runtime if they drift) --
    this one fails loudly and in the safe direction: the worst drift produces
    a QR the API refuses to draw for a link that would have resolved, which
    an operator sees immediately.
    """
    if not allowed:
        return True

    host = urlparse(base_url).hostname
    if not host:
        return False
    host = host.lower()

    for entry in allowed:
        entry_host = urlparse(entry).hostname
        if entry_host and entry_host.lower() == host:
            return True
    return False
