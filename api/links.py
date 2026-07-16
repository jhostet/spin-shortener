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
from responses import iso_now, json_response

SLUG_ALPHABET = string.ascii_letters + string.digits
SLUG_LENGTH = 7
SLUG_GENERATION_ATTEMPTS = 5

CUSTOM_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
MIN_LINK_PASSWORD_LENGTH = 4


def _generate_slug() -> str:
    return "".join(secrets.choice(SLUG_ALPHABET) for _ in range(SLUG_LENGTH))


async def _allocate_random_slug(store) -> str:
    for _ in range(SLUG_GENERATION_ATTEMPTS):
        slug = _generate_slug()
        if not await store.exists(f"slug:{slug}"):
            return slug
    raise RuntimeError("failed to allocate a unique slug")


async def _owned_slugs(store, username: str) -> list[str]:
    raw = await store.get(f"owner_links:{username}")
    return json.loads(raw) if raw else []


async def _add_owned_slug(store, username: str, slug: str) -> None:
    slugs = await _owned_slugs(store, username)
    if slug not in slugs:
        slugs.append(slug)
    await store.set(f"owner_links:{username}", json.dumps(slugs).encode("utf-8"))


async def _remove_owned_slug(store, username: str, slug: str) -> None:
    slugs = await _owned_slugs(store, username)
    if slug in slugs:
        slugs.remove(slug)
    await store.set(f"owner_links:{username}", json.dumps(slugs).encode("utf-8"))


async def get_link(store, slug: str) -> dict | None:
    raw = await store.get(f"slug:{slug}")
    return json.loads(raw) if raw else None


def _is_valid_target_url(target_url: str) -> bool:
    parsed = urlparse(target_url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _is_valid_custom_slug(slug: str) -> bool:
    return bool(CUSTOM_SLUG_PATTERN.match(slug))


def _public_link(record: dict) -> dict:
    """Link record with `password_hash` replaced by a boolean flag before it's ever serialized to a client."""
    public = {k: v for k, v in record.items() if k != "password_hash"}
    public["password_protected"] = bool(record.get("password_hash"))
    return public


async def handle_create(store, principal: Principal, request):
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return json_response(400, {"error": "invalid_json"})

    target_url = payload.get("target_url")
    if not isinstance(target_url, str) or not _is_valid_target_url(target_url):
        return json_response(400, {"error": "invalid_target_url"})

    custom_slug = payload.get("custom_slug")
    if custom_slug is not None:
        if not principal.has_permission("links.create_custom_slug"):
            return json_response(403, {"error": "forbidden", "required_permission": "links.create_custom_slug"})
        if not isinstance(custom_slug, str) or not _is_valid_custom_slug(custom_slug):
            return json_response(400, {"error": "invalid_custom_slug"})
        if await store.exists(f"slug:{custom_slug}"):
            return json_response(409, {"error": "slug_taken"})
        slug = custom_slug
        custom = True
    else:
        slug = await _allocate_random_slug(store)
        custom = False

    password = payload.get("password")
    password_hash = None
    if password:
        if not isinstance(password, str) or len(password) < MIN_LINK_PASSWORD_LENGTH:
            return json_response(400, {"error": "invalid_password"})
        password_hash = auth.hash_password(password)

    now = iso_now()
    record = {
        "slug": slug,
        "target_url": target_url,
        "owner": principal.username,
        "custom": custom,
        "password_hash": password_hash,
        "status": "active",
        "created_at": now,
        "updated_at": now,
    }
    await store.set(f"slug:{slug}", json.dumps(record).encode("utf-8"))
    await _add_owned_slug(store, principal.username, slug)
    return json_response(201, _public_link(record))


async def handle_list(store, principal: Principal):
    slugs = await _owned_slugs(store, principal.username)
    records = []
    for slug in slugs:
        record = await get_link(store, slug)
        if record is not None:
            records.append(_public_link(record))
    return json_response(200, {"links": records})


async def handle_get(store, principal: Principal, slug: str):
    record = await get_link(store, slug)
    if record is None:
        return json_response(404, {"error": "not_found"})
    if record["owner"] != principal.username and principal.role != "admin":
        return json_response(403, {"error": "forbidden", "required_permission": "links.view_all"})
    return json_response(200, _public_link(record))


async def handle_delete(store, principal: Principal, slug: str):
    record = await get_link(store, slug)
    if record is None:
        return json_response(404, {"error": "not_found"})
    if record["owner"] != principal.username and principal.role != "admin":
        return json_response(403, {"error": "forbidden", "required_permission": "links.view_all"})
    await store.delete(f"slug:{slug}")
    await _remove_owned_slug(store, record["owner"], slug)
    return json_response(200, {"ok": True})


async def handle_set_password(store, principal: Principal, slug: str, request):
    record = await get_link(store, slug)
    if record is None:
        return json_response(404, {"error": "not_found"})
    if record["owner"] != principal.username and principal.role != "admin":
        return json_response(403, {"error": "forbidden", "required_permission": "links.view_all"})

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
    return json_response(200, _public_link(record))
