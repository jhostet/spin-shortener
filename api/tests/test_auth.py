import json
import time
from typing import Optional

import pytest

import auth
from responses import Request
from tests.fakes import FakeStore


def _fake_request(cookie: Optional[str] = None, csrf_header: Optional[str] = None, method: str = "GET") -> Request:
    headers = {}
    if cookie is not None:
        headers["cookie"] = cookie
    if csrf_header is not None:
        headers["x-csrf-token"] = csrf_header
    return Request(method=method, uri="/api/x", headers=headers, body=None)


def test_hash_verify_roundtrip():
    hashed = auth.hash_password("correct horse", iterations=100)
    assert auth.verify_password("correct horse", hashed)


def test_verify_wrong_password_fails():
    hashed = auth.hash_password("correct horse", iterations=100)
    assert not auth.verify_password("wrong", hashed)


@pytest.mark.parametrize(
    "stored",
    [
        "",
        "not_pbkdf2$100$c2FsdA==$aGFzaA==",
        "pbkdf2_sha256$100$c2FsdA==",
        "pbkdf2_sha256$notanumber$c2FsdA==$aGFzaA==",
        "pbkdf2_sha256$100$not-valid-base64!!!$aGFzaA==",
        "pbkdf2_sha256$100$c2FsdA==$not-valid-base64!!!",
    ],
)
def test_verify_malformed_stored_value_fails_without_raising(stored):
    assert not auth.verify_password("anything", stored)


async def test_local_auth_provider_valid_credentials():
    store = FakeStore()
    await auth.put_user(store, {
        "username": "alice",
        "password_hash": auth.hash_password("hunter2", iterations=100),
        "role": "user",
        "permissions": [],
        "provider": "local",
        "disabled": False,
    })
    result = await auth.LocalAuthProvider().authenticate(store, "alice", "hunter2")
    assert result is not None
    assert result.username == "alice"
    assert result.role == "user"


async def test_local_auth_provider_wrong_password():
    store = FakeStore()
    await auth.put_user(store, {
        "username": "alice",
        "password_hash": auth.hash_password("hunter2", iterations=100),
        "role": "user",
        "permissions": [],
        "provider": "local",
        "disabled": False,
    })
    assert await auth.LocalAuthProvider().authenticate(store, "alice", "wrong") is None


async def test_local_auth_provider_disabled_user():
    store = FakeStore()
    await auth.put_user(store, {
        "username": "alice",
        "password_hash": auth.hash_password("hunter2", iterations=100),
        "role": "user",
        "permissions": [],
        "provider": "local",
        "disabled": True,
    })
    assert await auth.LocalAuthProvider().authenticate(store, "alice", "hunter2") is None


async def test_local_auth_provider_unknown_user():
    store = FakeStore()
    assert await auth.LocalAuthProvider().authenticate(store, "nobody", "hunter2") is None


async def test_create_and_resolve_session_roundtrip():
    store = FakeStore()
    await auth.put_user(store, {
        "username": "alice", "password_hash": "x", "role": "user",
        "permissions": [], "provider": "local", "disabled": False,
    })
    token, csrf_token = await auth.create_session(store, "alice", "local")

    principal = await auth.resolve_session(store, _fake_request(cookie=f"session={token}"))
    assert principal is not None
    assert principal.username == "alice"
    assert principal.csrf_token == csrf_token


async def test_resolve_session_expired_deletes_record():
    store = FakeStore()
    await auth.put_user(store, {
        "username": "alice", "password_hash": "x", "role": "user",
        "permissions": [], "provider": "local", "disabled": False,
    })
    token, _ = await auth.create_session(store, "alice", "local")

    session = json.loads(await store.get(f"session:{token}"))
    session["expires_at"] = int(time.time()) - 10
    await store.set(f"session:{token}", json.dumps(session).encode("utf-8"))

    assert await auth.resolve_session(store, _fake_request(cookie=f"session={token}")) is None
    assert await store.exists(f"session:{token}") is False


async def test_resolve_session_missing_or_tampered_cookie():
    store = FakeStore()
    assert await auth.resolve_session(store, _fake_request(cookie=None)) is None
    assert await auth.resolve_session(store, _fake_request(cookie="session=doesnotexist")) is None


async def test_resolve_session_defaults_assigned_domains_when_key_absent():
    """A user record written before assigned_domains existed has no such
    key at all — resolve_session must still succeed and default to []."""
    store = FakeStore()
    await auth.put_user(store, {
        "username": "alice", "password_hash": "x", "role": "user",
        "permissions": [], "provider": "local", "disabled": False,
    })
    token, _ = await auth.create_session(store, "alice", "local")

    principal = await auth.resolve_session(store, _fake_request(cookie=f"session={token}"))
    assert principal is not None
    assert principal.assigned_domains == []


async def test_resolve_session_for_deleted_user_fails():
    store = FakeStore()
    await auth.put_user(store, {
        "username": "alice", "password_hash": "x", "role": "user",
        "permissions": [], "provider": "local", "disabled": False,
    })
    token, _ = await auth.create_session(store, "alice", "local")
    await store.delete("user:alice")
    assert await auth.resolve_session(store, _fake_request(cookie=f"session={token}")) is None


async def test_delete_session_removes_record():
    store = FakeStore()
    token, _ = await auth.create_session(store, "alice", "local")
    await auth.delete_session(store, _fake_request(cookie=f"session={token}"))
    assert await store.exists(f"session:{token}") is False


def test_check_csrf_match():
    principal = auth.Principal(username="alice", role="user", permissions=[], csrf_token="tok123")
    request = _fake_request(csrf_header="tok123", method="POST")
    assert auth.check_csrf(request, principal)


def test_check_csrf_mismatch():
    principal = auth.Principal(username="alice", role="user", permissions=[], csrf_token="tok123")
    request = _fake_request(csrf_header="wrong", method="POST")
    assert not auth.check_csrf(request, principal)


def test_check_csrf_exempt_on_get():
    principal = auth.Principal(username="alice", role="user", permissions=[], csrf_token="tok123")
    request = _fake_request(csrf_header=None, method="GET")
    assert auth.check_csrf(request, principal)


def test_has_permission_admin_bypass():
    principal = auth.Principal(username="admin", role="admin", permissions=[], csrf_token="x")
    assert principal.has_permission("links.create_custom_slug")


def test_has_permission_explicit_permission():
    principal = auth.Principal(username="alice", role="user", permissions=["links.create_custom_slug"], csrf_token="x")
    assert principal.has_permission("links.create_custom_slug")


def test_has_permission_forbidden():
    principal = auth.Principal(username="alice", role="user", permissions=[], csrf_token="x")
    assert not principal.has_permission("links.create_custom_slug")


async def test_ensure_bootstrap_admin_seeds_once():
    store = FakeStore()
    await auth.ensure_bootstrap_admin(store, "admin", "changeme123")
    user = await auth.get_user(store, "admin")
    assert user is not None
    assert user["role"] == "admin"
    assert auth.verify_password("changeme123", user["password_hash"])

    # A second call with different args must NOT reseed/overwrite.
    await auth.ensure_bootstrap_admin(store, "someoneelse", "differentpassword")
    assert await auth.get_user(store, "someoneelse") is None
    unchanged = await auth.get_user(store, "admin")
    assert auth.verify_password("changeme123", unchanged["password_hash"])


async def test_ensure_bootstrap_admin_adds_to_username_index():
    store = FakeStore()
    await auth.ensure_bootstrap_admin(store, "admin", "changeme123")
    assert await auth.list_usernames(store) == ["admin"]


async def test_add_and_remove_username():
    store = FakeStore()
    await auth.add_username(store, "alice")
    await auth.add_username(store, "bob")
    assert await auth.list_usernames(store) == ["alice", "bob"]

    await auth.add_username(store, "alice")  # idempotent, no duplicate
    assert await auth.list_usernames(store) == ["alice", "bob"]

    await auth.remove_username(store, "alice")
    assert await auth.list_usernames(store) == ["bob"]

    await auth.remove_username(store, "doesnotexist")  # no-op, no error
    assert await auth.list_usernames(store) == ["bob"]
