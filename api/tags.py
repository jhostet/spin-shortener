"""Pure tag vocabulary helpers: normalization, validation, and the two
mutation primitives (`apply_tags`/`remove_tags`) bulk and single-link tagging
both build on.

Zero `spin_sdk` imports, and deliberately imports nothing from `links.py` (so
`links.py` can import this module with no cycle) — same testability rule as
`auth.py`/`qr.py`/`responses.py` (see CLAUDE.md).
"""

import re

MAX_TAGS_PER_LINK = 10
MAX_TAG_LENGTH = 32
TAG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def normalize_tag(value: str) -> str:
    """strip() + lower(). No validation — is_valid_tag does that."""
    return value.strip().lower()


def is_valid_tag(tag: str) -> bool:
    """True for an already-normalized tag of 1..MAX_TAG_LENGTH characters
    matching TAG_PATTERN."""
    return 1 <= len(tag) <= MAX_TAG_LENGTH and bool(TAG_PATTERN.match(tag))


def parse_tags(value, *, allow_none: bool = True) -> tuple[list[str] | None, dict | None]:
    """(tags, None) on success or (None, error_body) on the first problem —
    the same all-or-nothing (value, error_body) shape as
    backup.parse_stores_param. Normalizes, validates, de-duplicates and
    sorts. `value=None` -> `([], None)` when `allow_none`, else the
    `invalid_tags` error.

    Error bodies:
      {"error": "invalid_tags"}                        non-list, or a non-string member
      {"error": "invalid_tag", "tag": <as submitted>}   a member failing is_valid_tag
      {"error": "too_many_tags", "max_tags": 10}        more than MAX_TAGS_PER_LINK distinct
    """
    if value is None:
        if allow_none:
            return [], None
        return None, {"error": "invalid_tags"}

    if not isinstance(value, list) or any(not isinstance(t, str) for t in value):
        return None, {"error": "invalid_tags"}

    normalized: list[str] = []
    for raw_tag in value:
        tag = normalize_tag(raw_tag)
        if not is_valid_tag(tag):
            return None, {"error": "invalid_tag", "tag": raw_tag}
        if tag not in normalized:
            normalized.append(tag)

    if len(normalized) > MAX_TAGS_PER_LINK:
        return None, {"error": "too_many_tags", "max_tags": MAX_TAGS_PER_LINK}

    return sorted(normalized), None


def apply_tags(existing: list[str], add: list[str]) -> list[str]:
    """Union, de-duplicated and sorted. Cap enforcement is the caller's, so
    the caller can report which link overflowed."""
    return sorted(set(existing) | set(add))


def remove_tags(existing: list[str], remove: list[str]) -> list[str]:
    """Difference, sorted. Removing a tag a link does not carry is a no-op,
    not an error."""
    return sorted(set(existing) - set(remove))
