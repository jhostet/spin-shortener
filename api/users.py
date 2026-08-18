"""Admin user management: /api/users* endpoints. Every handler here requires
the `users.manage` permission (checked internally, same convention as
`links.py`'s custom-slug gate), not just a valid session.
"""

import json
from typing import Optional

import auth
import links
from auth import Principal
from responses import iso_now, json_response

MIN_PASSWORD_LENGTH = 8
VALID_ROLES = ("user", "admin")


def _public_user(user: dict) -> dict:
    public = {k: v for k, v in user.items() if k != "password_hash"}
    public["password_set"] = bool(user.get("password_hash"))
    return public


def _forbidden():
    return json_response(403, {"error": "forbidden", "required_permission": "users.manage"})


def _validate_permissions(permissions) -> Optional[str]:
    """Returns an error string, or None if valid."""
    if not isinstance(permissions, list) or not all(isinstance(p, str) for p in permissions):
        return "invalid_permissions"
    if set(permissions) - auth.KNOWN_PERMISSIONS:
        return "invalid_permissions"
    return None


def _validate_assigned_domains(value, configured: list[str]) -> Optional[str]:
    """Mirrors _validate_permissions exactly: one error string for both a
    malformed value and a member outside the allowed set, checked against
    `configured` (the deployment's `public_base_urls`) rather than a fixed
    frozenset — this is configuration, not a vocabulary."""
    if not isinstance(value, list) or not all(isinstance(d, str) for d in value):
        return "invalid_assigned_domains"
    if set(value) - set(configured):
        return "invalid_assigned_domains"
    return None


async def handle_list(store, principal: Principal, configured_domains: list[str]):
    if not principal.has_permission("users.manage"):
        return _forbidden()

    usernames = await auth.list_usernames(store)
    users = []
    for username in usernames:
        user = await auth.get_user(store, username)
        if user is not None:
            users.append(_public_user(user))
    return json_response(200, {"users": users, "all_domains": configured_domains})


async def handle_create(store, principal: Principal, request, configured_domains: list[str]):
    if not principal.has_permission("users.manage"):
        return _forbidden()

    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return json_response(400, {"error": "invalid_json"})

    username = payload.get("username")
    if not isinstance(username, str) or not username.strip():
        return json_response(400, {"error": "invalid_username"})

    password = payload.get("password")
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        return json_response(400, {"error": "invalid_password"})

    if await auth.get_user(store, username) is not None:
        return json_response(409, {"error": "username_taken"})

    role = payload.get("role", "user")
    if role not in VALID_ROLES:
        return json_response(400, {"error": "invalid_role"})

    permissions = payload.get("permissions", [])
    error = _validate_permissions(permissions)
    if error:
        return json_response(400, {"error": error})

    assigned_domains = payload.get("assigned_domains", [])
    error = _validate_assigned_domains(assigned_domains, configured_domains)
    if error:
        return json_response(400, {"error": error})

    user = {
        "username": username,
        "password_hash": auth.hash_password(password),
        "role": role,
        "permissions": permissions,
        "assigned_domains": assigned_domains,
        "provider": "local",
        "disabled": False,
        "created_at": iso_now(),
    }
    await auth.put_user(store, user)
    await auth.add_username(store, username)
    return json_response(201, _public_user(user))


async def handle_get(store, principal: Principal, username: str):
    if not principal.has_permission("users.manage"):
        return _forbidden()

    user = await auth.get_user(store, username)
    if user is None:
        return json_response(404, {"error": "not_found"})
    return json_response(200, _public_user(user))


UPDATABLE_FIELDS = {"role", "permissions", "disabled", "password", "assigned_domains"}


async def handle_update(store, principal: Principal, username: str, request, configured_domains: list[str]):
    if not principal.has_permission("users.manage"):
        return _forbidden()

    user = await auth.get_user(store, username)
    if user is None:
        return json_response(404, {"error": "not_found"})

    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return json_response(400, {"error": "invalid_json"})

    if not UPDATABLE_FIELDS & payload.keys():
        return json_response(400, {"error": "no_fields_to_update"})

    if "role" in payload:
        role = payload["role"]
        if role not in VALID_ROLES:
            return json_response(400, {"error": "invalid_role"})
        user["role"] = role

    if "permissions" in payload:
        error = _validate_permissions(payload["permissions"])
        if error:
            return json_response(400, {"error": error})
        user["permissions"] = payload["permissions"]

    if "assigned_domains" in payload:
        error = _validate_assigned_domains(payload["assigned_domains"], configured_domains)
        if error:
            return json_response(400, {"error": error})
        user["assigned_domains"] = payload["assigned_domains"]

    if "disabled" in payload:
        disabled = payload["disabled"]
        if not isinstance(disabled, bool):
            return json_response(400, {"error": "invalid_disabled"})
        if disabled and username == principal.username:
            return json_response(400, {"error": "cannot_disable_self"})
        user["disabled"] = disabled

    if "password" in payload:
        password = payload["password"]
        if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
            return json_response(400, {"error": "invalid_password"})
        user["password_hash"] = auth.hash_password(password)

    await auth.put_user(store, user)
    return json_response(200, _public_user(user))


async def handle_delete(store, links_store, principal: Principal, username: str, list_keys, get_many):
    if not principal.has_permission("users.manage"):
        return _forbidden()

    user = await auth.get_user(store, username)
    if user is None:
        return json_response(404, {"error": "not_found"})
    if username == principal.username:
        return json_response(400, {"error": "cannot_delete_self"})

    # docs/plans/derived-link-indexes.md: derived from the records rather
    # than the owner_links:<username> index, which is a shared, racy,
    # read-modify-write key that can drift from the record's actual owner.
    # Before this change, a link whose owner_links: entry had drifted did
    # NOT block its owner's deletion — exactly the unindexed_owner_link
    # scenario docs/plans/kv-consistency-check.md built that check for.
    owned = await links.slugs_owned_by(links_store, username, list_keys, get_many)
    if owned:
        return json_response(409, {"error": "user_owns_links", "username": username, "link_count": len(owned)})

    # Cross-store write order (see docs/plans/user-deletion-link-ownership.md,
    # "Write ordering and what an interruption leaves"): links store first,
    # then sessions, then the user record, then the usernames index. Spin KV
    # has no transactions and no compare-and-swap, so this order is chosen so
    # that every interruption point leaves the *stronger* invariant — a crash
    # after this point never leaves a live session for a nonexistent user,
    # and never leaves the user deleted with a dangling owner_links: key that
    # a retry could no longer reach (handle_delete would 404 on the missing
    # user record).
    await links_store.delete(f"owner_links:{username}")
    await auth.delete_sessions_for_user(store, username, list_keys)
    await store.delete(f"user:{username}")
    await auth.remove_username(store, username)
    return json_response(200, {"ok": True})
