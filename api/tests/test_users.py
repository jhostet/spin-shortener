import json

import pytest

import auth
import links
import users
from responses import Request
from tests.fakes import FakeStore, fake_get_many, fake_list_keys


def _principal(username="admin", role="admin", permissions=None):
    return auth.Principal(username=username, role=role, permissions=permissions or [], csrf_token="x")


def _request(payload=None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    return Request(method="POST", uri="/api/users", headers={}, body=body)


CONFIGURED_DOMAINS = ["https://a.example.com", "https://b.example.com"]


async def _make_user(store, username="alice", configured_domains=CONFIGURED_DOMAINS, **overrides):
    payload = {"username": username, "password": "longenough"}
    payload.update(overrides)
    resp = await users.handle_create(store, _principal(), _request(payload), configured_domains)
    return resp


# --- Permission gating (users.manage required for every endpoint) ---


async def test_list_requires_users_manage():
    store = FakeStore()
    resp = await users.handle_list(store, _principal(role="user", permissions=[]), CONFIGURED_DOMAINS)
    assert resp.status == 403
    assert json.loads(resp.body)["required_permission"] == "users.manage"


async def test_create_requires_users_manage():
    store = FakeStore()
    resp = await users.handle_create(store, _principal(role="user", permissions=[]), _request({"username": "x", "password": "longenough"}), CONFIGURED_DOMAINS)
    assert resp.status == 403


async def test_get_requires_users_manage():
    store = FakeStore()
    resp = await users.handle_get(store, _principal(role="user", permissions=[]), "alice")
    assert resp.status == 403


async def test_update_requires_users_manage():
    store = FakeStore()
    resp = await users.handle_update(store, _principal(role="user", permissions=[]), "alice", _request({"disabled": True}), CONFIGURED_DOMAINS)
    assert resp.status == 403


async def test_delete_requires_users_manage():
    store = FakeStore()
    links_store = FakeStore()
    resp = await users.handle_delete(store, links_store, _principal(role="user", permissions=[]), "alice", fake_list_keys, fake_get_many)
    assert resp.status == 403


async def test_explicit_users_manage_permission_bypasses_admin_requirement():
    store = FakeStore()
    resp = await users.handle_list(store, _principal(username="ops", role="user", permissions=["users.manage"]), CONFIGURED_DOMAINS)
    assert resp.status == 200


# --- Create ---


async def test_create_valid_user():
    store = FakeStore()
    resp = await _make_user(store, username="alice")
    assert resp.status == 201
    body = json.loads(resp.body)
    assert body["username"] == "alice"
    assert body["role"] == "user"
    assert "password_hash" not in body

    stored = await auth.get_user(store, "alice")
    assert auth.verify_password("longenough", stored["password_hash"])
    assert await auth.list_usernames(store) == ["alice"]


async def test_create_with_role_and_permissions():
    store = FakeStore()
    resp = await _make_user(store, role="admin", permissions=["links.create_custom_slug"])
    assert resp.status == 201
    body = json.loads(resp.body)
    assert body["role"] == "admin"
    assert body["permissions"] == ["links.create_custom_slug"]


async def test_create_duplicate_username_conflict():
    store = FakeStore()
    await _make_user(store, username="alice")
    resp = await _make_user(store, username="alice")
    assert resp.status == 409
    assert json.loads(resp.body)["error"] == "username_taken"


async def test_create_invalid_username():
    store = FakeStore()
    resp = await users.handle_create(store, _principal(), _request({"username": "  ", "password": "longenough"}), CONFIGURED_DOMAINS)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_username"


async def test_create_password_too_short():
    store = FakeStore()
    resp = await users.handle_create(store, _principal(), _request({"username": "alice", "password": "short"}), CONFIGURED_DOMAINS)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_password"


async def test_create_invalid_role():
    store = FakeStore()
    resp = await users.handle_create(store, _principal(), _request({"username": "alice", "password": "longenough", "role": "superadmin"}), CONFIGURED_DOMAINS)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_role"


async def test_create_unknown_permission_rejected():
    store = FakeStore()
    resp = await users.handle_create(store, _principal(), _request({"username": "alice", "password": "longenough", "permissions": ["not.a.real.permission"]}), CONFIGURED_DOMAINS)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_permissions"


# --- List / Get ---


async def test_list_returns_all_users_without_password_hash():
    store = FakeStore()
    await _make_user(store, username="alice")
    await _make_user(store, username="bob")
    resp = await users.handle_list(store, _principal(), CONFIGURED_DOMAINS)
    assert resp.status == 200
    body = json.loads(resp.body)
    usernames = {u["username"] for u in body["users"]}
    assert usernames == {"alice", "bob"}
    assert all("password_hash" not in u for u in body["users"])


async def test_get_not_found():
    store = FakeStore()
    resp = await users.handle_get(store, _principal(), "doesnotexist")
    assert resp.status == 404


# --- Update ---


async def test_update_empty_payload_rejected():
    store = FakeStore()
    await _make_user(store, username="alice")
    resp = await users.handle_update(store, _principal(), "alice", _request({}), CONFIGURED_DOMAINS)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "no_fields_to_update"


async def test_update_not_found():
    store = FakeStore()
    resp = await users.handle_update(store, _principal(), "doesnotexist", _request({"disabled": True}), CONFIGURED_DOMAINS)
    assert resp.status == 404


async def test_update_role_and_permissions():
    store = FakeStore()
    await _make_user(store, username="alice")
    resp = await users.handle_update(store, _principal(), "alice", _request({"role": "admin", "permissions": ["links.view_all"]}), CONFIGURED_DOMAINS)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["role"] == "admin"
    assert body["permissions"] == ["links.view_all"]


async def test_update_invalid_role_rejected():
    store = FakeStore()
    await _make_user(store, username="alice")
    resp = await users.handle_update(store, _principal(), "alice", _request({"role": "superadmin"}), CONFIGURED_DOMAINS)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_role"


async def test_update_disable_another_user():
    store = FakeStore()
    await _make_user(store, username="alice")
    resp = await users.handle_update(store, _principal(username="admin"), "alice", _request({"disabled": True}), CONFIGURED_DOMAINS)
    assert resp.status == 200
    assert json.loads(resp.body)["disabled"] is True


async def test_update_cannot_disable_self():
    store = FakeStore()
    await _make_user(store, username="admin", password="longenough2")
    resp = await users.handle_update(store, _principal(username="admin"), "admin", _request({"disabled": True}), CONFIGURED_DOMAINS)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "cannot_disable_self"


async def test_update_password():
    store = FakeStore()
    await _make_user(store, username="alice")
    resp = await users.handle_update(store, _principal(), "alice", _request({"password": "newlongpassword"}), CONFIGURED_DOMAINS)
    assert resp.status == 200
    stored = await auth.get_user(store, "alice")
    assert auth.verify_password("newlongpassword", stored["password_hash"])
    assert not auth.verify_password("longenough", stored["password_hash"])


async def test_update_partial_leaves_other_fields_untouched():
    store = FakeStore()
    await _make_user(store, username="alice", permissions=["links.view_all"])
    resp = await users.handle_update(store, _principal(), "alice", _request({"disabled": True}), CONFIGURED_DOMAINS)
    body = json.loads(resp.body)
    assert body["disabled"] is True
    assert body["permissions"] == ["links.view_all"]  # untouched


# --- Delete ---


async def test_delete_another_user():
    store = FakeStore()
    links_store = FakeStore()
    await _make_user(store, username="alice")
    resp = await users.handle_delete(store, links_store, _principal(username="admin"), "alice", fake_list_keys, fake_get_many)
    assert resp.status == 200
    assert await auth.get_user(store, "alice") is None
    assert "alice" not in await auth.list_usernames(store)


async def test_delete_not_found():
    store = FakeStore()
    links_store = FakeStore()
    resp = await users.handle_delete(store, links_store, _principal(), "doesnotexist", fake_list_keys, fake_get_many)
    assert resp.status == 404


async def test_delete_cannot_delete_self():
    store = FakeStore()
    links_store = FakeStore()
    await _make_user(store, username="admin", password="longenough2")
    resp = await users.handle_delete(store, links_store, _principal(username="admin"), "admin", fake_list_keys, fake_get_many)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "cannot_delete_self"


def _link_record(slug, owner):
    return json.dumps({
        "slug": slug,
        "owner": owner,
        "target_url": "https://example.com/" + slug,
        "status": "active",
        "created_at": "2026-01-01T00:00:00Z",
    }).encode("utf-8")


async def test_delete_refuses_when_user_owns_links():
    store = FakeStore()
    links_store = FakeStore()
    await _make_user(store, username="carol")
    # Since docs/plans/derived-link-indexes.md's Stage 1 the 409 gate derives
    # ownership from the RECORDS, not from `owner_links:carol`, so seeding the
    # index alone would describe a store in which carol owns nothing at all.
    await links_store.set("slug:carol-private", _link_record("carol-private", "carol"))
    await links.add_slugs_to_indexes(links_store, "carol", ["carol-private"])

    users_before = dict(store._data)
    links_before = dict(links_store._data)

    resp = await users.handle_delete(store, links_store, _principal(username="admin"), "carol", fake_list_keys, fake_get_many)

    assert resp.status == 409
    body = json.loads(resp.body)
    assert body == {"error": "user_owns_links", "username": "carol", "link_count": 1}
    # Nothing written to either store on the refusal path.
    assert store._data == users_before
    assert links_store._data == links_before


async def test_delete_allowed_when_owner_index_names_a_slug_with_no_record():
    """The converse of the test above, and a behaviour CHANGE made by
    docs/plans/derived-link-indexes.md's Stage 1. `owner_links:carol` names a
    slug whose record does not exist -- an orphan index entry, the state an
    interrupted delete leaves. The old gate read that index and refused the
    deletion forever; the derived gate reads the records, finds carol owns
    nothing, and lets the account go."""
    store = FakeStore()
    links_store = FakeStore()
    await _make_user(store, username="carol")
    await links.add_slugs_to_indexes(links_store, "carol", ["ghost"])
    assert await links_store.exists("slug:ghost") is False

    resp = await users.handle_delete(
        store, links_store, _principal(username="admin"), "carol", fake_list_keys, fake_get_many
    )

    assert resp.status == 200
    assert await auth.get_user(store, "carol") is None


async def test_delete_with_empty_owner_links_key_removes_it():
    store = FakeStore()
    links_store = FakeStore()
    await _make_user(store, username="carol")
    await links_store.set("owner_links:carol", json.dumps([]).encode("utf-8"))

    resp = await users.handle_delete(store, links_store, _principal(username="admin"), "carol", fake_list_keys, fake_get_many)

    assert resp.status == 200
    assert await links_store.exists("owner_links:carol") is False


async def test_delete_with_no_owner_links_key_deletes_cleanly():
    store = FakeStore()
    links_store = FakeStore()
    await _make_user(store, username="carol")

    resp = await users.handle_delete(store, links_store, _principal(username="admin"), "carol", fake_list_keys, fake_get_many)

    assert resp.status == 200
    assert await auth.get_user(store, "carol") is None


async def test_delete_purges_the_users_sessions_but_not_others():
    store = FakeStore()
    links_store = FakeStore()
    await _make_user(store, username="carol")
    await _make_user(store, username="dave", password="longenough2")
    carol_token, _ = await auth.create_session(store, "carol", "local")
    dave_token, _ = await auth.create_session(store, "dave", "local")

    resp = await users.handle_delete(store, links_store, _principal(username="admin"), "carol", fake_list_keys, fake_get_many)

    assert resp.status == 200
    assert await store.exists(f"session:{carol_token}") is False
    assert await store.exists(f"session:{dave_token}") is True


# --- assigned_domains ---


async def test_create_with_assigned_domains():
    store = FakeStore()
    resp = await _make_user(store, assigned_domains=["https://a.example.com"])
    assert resp.status == 201
    body = json.loads(resp.body)
    assert body["assigned_domains"] == ["https://a.example.com"]


async def test_create_unknown_domain_rejected_and_nothing_written():
    store = FakeStore()
    resp = await users.handle_create(
        store, _principal(),
        _request({"username": "alice", "password": "longenough", "assigned_domains": ["https://evil.example"]}),
        CONFIGURED_DOMAINS,
    )
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_assigned_domains"
    assert await auth.get_user(store, "alice") is None


async def test_update_assigned_domains():
    store = FakeStore()
    await _make_user(store, username="alice")
    resp = await users.handle_update(
        store, _principal(), "alice",
        _request({"assigned_domains": ["https://b.example.com"]}),
        CONFIGURED_DOMAINS,
    )
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["assigned_domains"] == ["https://b.example.com"]


async def test_update_unknown_domain_rejected_and_nothing_written():
    store = FakeStore()
    await _make_user(store, username="alice", assigned_domains=["https://a.example.com"])
    resp = await users.handle_update(
        store, _principal(), "alice",
        _request({"assigned_domains": ["https://evil.example"]}),
        CONFIGURED_DOMAINS,
    )
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_assigned_domains"
    unchanged = await auth.get_user(store, "alice")
    assert unchanged["assigned_domains"] == ["https://a.example.com"]


async def test_list_returns_all_domains():
    store = FakeStore()
    resp = await users.handle_list(store, _principal(), CONFIGURED_DOMAINS)
    assert resp.status == 200
    assert json.loads(resp.body)["all_domains"] == CONFIGURED_DOMAINS


async def test_public_user_exposes_assigned_domains():
    user = {
        "username": "alice",
        "password_hash": "x",
        "role": "user",
        "permissions": [],
        "assigned_domains": ["https://a.example.com"],
        "provider": "local",
        "disabled": False,
    }
    public = users._public_user(user)
    assert public["assigned_domains"] == ["https://a.example.com"]
    assert "password_hash" not in public


# --- password_set ---


def test_public_user_password_set_true_when_hashed():
    user = {"username": "alice", "password_hash": "pbkdf2_sha256$1$c2FsdA==$aGFzaA==", "role": "user", "permissions": []}
    public = users._public_user(user)
    assert public["password_set"] is True
    assert "password_hash" not in public


@pytest.mark.parametrize("hashless_value", [None, "", "MISSING"])
def test_public_user_password_set_false_when_hashless(hashless_value):
    user = {"username": "alice", "role": "user", "permissions": []}
    if hashless_value != "MISSING":
        user["password_hash"] = hashless_value
    public = users._public_user(user)
    assert public["password_set"] is False


async def test_create_response_includes_password_set_true():
    store = FakeStore()
    resp = await _make_user(store, username="alice")
    body = json.loads(resp.body)
    assert body["password_set"] is True
    assert "password_hash" not in body


async def test_list_response_never_contains_password_hash_and_has_password_set():
    store = FakeStore()
    await _make_user(store, username="alice")
    resp = await users.handle_list(store, _principal(), CONFIGURED_DOMAINS)
    body = json.loads(resp.body)
    assert all("password_hash" not in u for u in body["users"])
    assert all("password_set" in u for u in body["users"])
    assert body["users"][0]["password_set"] is True
