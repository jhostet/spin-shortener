"""Link authoring: create/list/get/delete for the /api/links surface, plus
custom slugs and per-link password protection.
"""

import json
import re
import secrets
import string
from urllib.parse import urlparse

import auth
from auth import Principal
from responses import iso_now, json_response, parse_iso8601_utc, to_iso8601_utc

SLUG_ALPHABET = string.ascii_letters + string.digits
SLUG_LENGTH = 7
SLUG_GENERATION_ATTEMPTS = 5

CUSTOM_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
MIN_LINK_PASSWORD_LENGTH = 4
LINK_STATUSES = ("active", "disabled")


def generate_slug() -> str:
    return "".join(secrets.choice(SLUG_ALPHABET) for _ in range(SLUG_LENGTH))


async def allocate_random_slug(store, taken: set[str]) -> str:
    """Random slug not in `taken` and not already in the store. Adds the
    result to `taken` so a caller allocating many in one pass cannot collide
    with itself without a KV round trip per candidate."""
    for _ in range(SLUG_GENERATION_ATTEMPTS):
        slug = generate_slug()
        if slug not in taken and not await store.exists(f"slug:{slug}"):
            taken.add(slug)
            return slug
    raise RuntimeError("failed to allocate a unique slug")


async def _owned_slugs(store, username: str) -> list[str]:
    raw = await store.get(f"owner_links:{username}")
    return json.loads(raw) if raw else []


ALL_SLUGS_INDEX_KEY = "all_links"


async def _all_slugs(store) -> list[str]:
    raw = await store.get(ALL_SLUGS_INDEX_KEY)
    return json.loads(raw) if raw else []


async def add_slugs_to_indexes(store, owner: str, slugs: list[str]) -> None:
    """One read+write of `all_links`, one of `owner_links:<owner>`, for any
    number of slugs. Order-preserving, skips slugs already present."""
    all_slugs = await _all_slugs(store)
    for slug in slugs:
        if slug not in all_slugs:
            all_slugs.append(slug)
    await store.set(ALL_SLUGS_INDEX_KEY, json.dumps(all_slugs).encode("utf-8"))

    owned = await _owned_slugs(store, owner)
    for slug in slugs:
        if slug not in owned:
            owned.append(slug)
    await store.set(f"owner_links:{owner}", json.dumps(owned).encode("utf-8"))


async def remove_slugs_from_indexes(store, slugs_by_owner: dict[str, list[str]]) -> None:
    """One read+write of `all_links` total, plus one per distinct owner. Takes
    a per-owner mapping because a `links.edit_all` user can delete links
    belonging to several owners in a single action."""
    all_to_remove = {slug for slugs in slugs_by_owner.values() for slug in slugs}
    all_slugs = await _all_slugs(store)
    all_slugs = [slug for slug in all_slugs if slug not in all_to_remove]
    await store.set(ALL_SLUGS_INDEX_KEY, json.dumps(all_slugs).encode("utf-8"))

    for owner, slugs in slugs_by_owner.items():
        to_remove = set(slugs)
        owned = await _owned_slugs(store, owner)
        owned = [slug for slug in owned if slug not in to_remove]
        await store.set(f"owner_links:{owner}", json.dumps(owned).encode("utf-8"))


async def get_link(store, slug: str) -> dict | None:
    raw = await store.get(f"slug:{slug}")
    return json.loads(raw) if raw else None


def is_valid_target_url(target_url: str) -> bool:
    parsed = urlparse(target_url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def is_valid_custom_slug(slug: str) -> bool:
    return bool(CUSTOM_SLUG_PATTERN.match(slug))


def parse_window_field(value) -> tuple[str | None, bool]:
    """Returns (normalized_iso8601_utc_or_None, is_invalid). `value=None` means
    "unset", not invalid — an explicit-but-unparsable string is what's invalid.
    """
    if value is None:
        return None, False
    if not isinstance(value, str):
        return None, True
    parsed = parse_iso8601_utc(value)
    if parsed is None:
        return None, True
    return to_iso8601_utc(parsed), False


def public_link(record: dict) -> dict:
    """Link record with `password_hash` replaced by a boolean flag before it's ever serialized to a client."""
    public = {k: v for k, v in record.items() if k != "password_hash"}
    public["password_protected"] = bool(record.get("password_hash"))
    return public


def can_view(principal: Principal, record: dict) -> bool:
    """`links.edit_all` implies view access — a user who can edit any link
    shouldn't also need `links.view_all` separately to open one. Shared
    (not module-private) since analytics.py and qr.py gate on the same
    view semantics for a link's analytics/QR endpoints."""
    return (
        record["owner"] == principal.username
        or principal.has_permission("links.view_all")
        or principal.has_permission("links.edit_all")
    )


def can_edit(principal: Principal, record: dict) -> bool:
    """Shared (not module-private) — bulk.py gates bulk-action rows on the
    same write semantics as the single-link edit/delete/password handlers
    below."""
    return record["owner"] == principal.username or principal.has_permission("links.edit_all")


async def handle_create(store, principal: Principal, request):
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return json_response(400, {"error": "invalid_json"})

    target_url = payload.get("target_url")
    if not isinstance(target_url, str) or not is_valid_target_url(target_url):
        return json_response(400, {"error": "invalid_target_url"})

    custom_slug = payload.get("custom_slug")
    if custom_slug is not None:
        if not principal.has_permission("links.create_custom_slug"):
            return json_response(403, {"error": "forbidden", "required_permission": "links.create_custom_slug"})
        if not isinstance(custom_slug, str) or not is_valid_custom_slug(custom_slug):
            return json_response(400, {"error": "invalid_custom_slug"})
        if await store.exists(f"slug:{custom_slug}"):
            return json_response(409, {"error": "slug_taken"})
        slug = custom_slug
        custom = True
    else:
        slug = await allocate_random_slug(store, set())
        custom = False

    password = payload.get("password")
    password_hash = None
    if password:
        if not isinstance(password, str) or len(password) < MIN_LINK_PASSWORD_LENGTH:
            return json_response(400, {"error": "invalid_password"})
        password_hash = auth.hash_password(password)

    start_at, start_invalid = parse_window_field(payload.get("start_at"))
    if start_invalid:
        return json_response(400, {"error": "invalid_start_at"})
    end_at, end_invalid = parse_window_field(payload.get("end_at"))
    if end_invalid:
        return json_response(400, {"error": "invalid_end_at"})
    if start_at is not None and end_at is not None and start_at >= end_at:
        return json_response(400, {"error": "invalid_window_range"})

    now = iso_now()
    record = {
        "slug": slug,
        "target_url": target_url,
        "owner": principal.username,
        "custom": custom,
        "password_hash": password_hash,
        "status": "active",
        "start_at": start_at,
        "end_at": end_at,
        "created_at": now,
        "updated_at": now,
    }
    await store.set(f"slug:{slug}", json.dumps(record).encode("utf-8"))
    await add_slugs_to_indexes(store, principal.username, [slug])
    return json_response(201, public_link(record))


async def handle_list(store, principal: Principal):
    if principal.has_permission("links.view_all") or principal.has_permission("links.edit_all"):
        slugs = await _all_slugs(store)
    else:
        slugs = await _owned_slugs(store, principal.username)
    records = []
    for slug in slugs:
        record = await get_link(store, slug)
        if record is not None:
            records.append(public_link(record))
    return json_response(200, {"links": records})


async def handle_get(store, principal: Principal, slug: str):
    record = await get_link(store, slug)
    if record is None:
        return json_response(404, {"error": "not_found"})
    if not can_view(principal, record):
        return json_response(403, {"error": "forbidden", "required_permission": "links.view_all"})
    return json_response(200, public_link(record))


UPDATABLE_FIELDS = {"target_url", "status", "start_at", "end_at"}


async def handle_update(store, principal: Principal, slug: str, request):
    record = await get_link(store, slug)
    if record is None:
        return json_response(404, {"error": "not_found"})
    if not can_edit(principal, record):
        return json_response(403, {"error": "forbidden", "required_permission": "links.edit_all"})

    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return json_response(400, {"error": "invalid_json"})

    if not UPDATABLE_FIELDS & payload.keys():
        return json_response(400, {"error": "no_fields_to_update"})

    if "target_url" in payload:
        target_url = payload["target_url"]
        if not isinstance(target_url, str) or not is_valid_target_url(target_url):
            return json_response(400, {"error": "invalid_target_url"})
        record["target_url"] = target_url

    if "status" in payload:
        status = payload["status"]
        if status not in LINK_STATUSES:
            return json_response(400, {"error": "invalid_status"})
        record["status"] = status

    # Merge candidate start_at/end_at (new value if provided, else the
    # record's existing value) so e.g. patching only end_at earlier than an
    # existing start_at is still caught.
    merged_start = record.get("start_at")
    merged_end = record.get("end_at")

    if "start_at" in payload:
        merged_start, invalid = parse_window_field(payload["start_at"])
        if invalid:
            return json_response(400, {"error": "invalid_start_at"})

    if "end_at" in payload:
        merged_end, invalid = parse_window_field(payload["end_at"])
        if invalid:
            return json_response(400, {"error": "invalid_end_at"})

    if merged_start is not None and merged_end is not None and merged_start >= merged_end:
        return json_response(400, {"error": "invalid_window_range"})

    record["start_at"] = merged_start
    record["end_at"] = merged_end

    record["updated_at"] = iso_now()
    await store.set(f"slug:{slug}", json.dumps(record).encode("utf-8"))
    return json_response(200, public_link(record))


async def handle_delete(store, principal: Principal, slug: str):
    record = await get_link(store, slug)
    if record is None:
        return json_response(404, {"error": "not_found"})
    if not can_edit(principal, record):
        return json_response(403, {"error": "forbidden", "required_permission": "links.edit_all"})
    await store.delete(f"slug:{slug}")
    await remove_slugs_from_indexes(store, {record["owner"]: [slug]})
    return json_response(200, {"ok": True})


async def handle_set_password(store, principal: Principal, slug: str, request):
    record = await get_link(store, slug)
    if record is None:
        return json_response(404, {"error": "not_found"})
    if not can_edit(principal, record):
        return json_response(403, {"error": "forbidden", "required_permission": "links.edit_all"})

    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return json_response(400, {"error": "invalid_json"})

    password = payload.get("password")
    if password:
        if not isinstance(password, str) or len(password) < MIN_LINK_PASSWORD_LENGTH:
            return json_response(400, {"error": "invalid_password"})
        record["password_hash"] = auth.hash_password(password)
    else:
        record["password_hash"] = None

    record["updated_at"] = iso_now()
    await store.set(f"slug:{slug}", json.dumps(record).encode("utf-8"))
    return json_response(200, public_link(record))
