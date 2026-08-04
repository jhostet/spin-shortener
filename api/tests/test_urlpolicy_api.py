"""Tests for urlpolicy.py's three handlers: handle_get_policy,
handle_put_policy and handle_violations. app.py's wiring is exercised only by
the live spin up smoke test (see docs/plans/destination-url-policy.md's
Verification section) — these are the host-testable handler-level tests.
"""

import json

import auth
import urlpolicy
from responses import Request
from tests.fakes import FakeStore, fake_list_keys


def _principal(username="admin", role="admin", permissions=None):
    return auth.Principal(username=username, role=role, permissions=permissions or [], csrf_token="x")


def _put_request(payload):
    return Request(method="PUT", uri="/api/admin/url-policy", headers={}, body=json.dumps(payload).encode("utf-8"))


# --- handle_get_policy ---


async def test_get_policy_requires_users_manage():
    store = FakeStore()
    resp = await urlpolicy.handle_get_policy(store, _principal(permissions=[], role="user"))
    assert resp.status == 403
    assert json.loads(resp.body) == {"error": "forbidden", "required_permission": "users.manage"}


async def test_get_policy_returns_empty_policy_on_fresh_store():
    store = FakeStore()
    resp = await urlpolicy.handle_get_policy(store, _principal(permissions=["users.manage"]))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["default_action"] == "allow"
    assert body["rules"] == []


async def test_get_policy_returns_saved_document():
    store = FakeStore()
    await urlpolicy.save_policy(store, {"version": 1, "default_action": "deny", "rules": []})
    resp = await urlpolicy.handle_get_policy(store, _principal(permissions=["users.manage"]))
    body = json.loads(resp.body)
    assert body["default_action"] == "deny"


# --- handle_put_policy ---


async def test_put_policy_requires_users_manage():
    store = FakeStore()
    resp = await urlpolicy.handle_put_policy(
        store, _principal(permissions=[], role="user"), _put_request({"default_action": "allow", "rules": []}),
    )
    assert resp.status == 403
    assert json.loads(resp.body) == {"error": "forbidden", "required_permission": "users.manage"}


async def test_put_policy_body_too_large():
    store = FakeStore()
    huge = Request(method="PUT", uri="/api/admin/url-policy", headers={}, body=b"x" * (urlpolicy.MAX_POLICY_BODY_BYTES + 1))
    resp = await urlpolicy.handle_put_policy(store, _principal(permissions=["users.manage"]), huge)
    assert resp.status == 413
    assert json.loads(resp.body) == {"error": "body_too_large", "max_bytes": urlpolicy.MAX_POLICY_BODY_BYTES}


async def test_put_policy_invalid_body_returns_parse_error():
    store = FakeStore()
    resp = await urlpolicy.handle_put_policy(
        store, _principal(permissions=["users.manage"]), _put_request({"default_action": "nope", "rules": []}),
    )
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_default_action"


async def test_put_policy_saves_and_returns_document_with_stamps():
    store = FakeStore()
    resp = await urlpolicy.handle_put_policy(
        store, _principal(username="alice", permissions=["users.manage"]),
        _put_request({"default_action": "deny", "rules": [{"host": "evil.example", "action": "deny"}]}),
    )
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["default_action"] == "deny"
    assert body["rules"] == [{
        "host": "evil.example", "action": "deny", "note": None,
        "created_at": body["updated_at"], "created_by": "alice",
    }]
    assert body["updated_by"] == "alice"

    reloaded = await urlpolicy.load_policy(store)
    assert reloaded == body


# --- handle_violations ---


async def test_violations_requires_users_manage():
    store = FakeStore()
    resp = await urlpolicy.handle_violations(store, _principal(permissions=[], role="user"), fake_list_keys)
    assert resp.status == 403
    assert json.loads(resp.body) == {"error": "forbidden", "required_permission": "users.manage"}


async def test_violations_empty_store_is_a_valid_empty_report():
    store = FakeStore()
    resp = await urlpolicy.handle_violations(store, _principal(permissions=["users.manage"]), fake_list_keys)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["format"] == urlpolicy.VIOLATIONS_FORMAT
    assert body["count"] == 0
    assert body["violations"] == []


async def test_violations_lists_link_that_violates_current_policy():
    store = FakeStore({
        "slug:promo": json.dumps({
            "slug": "promo", "owner": "bob", "status": "active", "target_url": "https://evil.example/x",
        }).encode("utf-8"),
    })
    await urlpolicy.save_policy(store, {
        "version": 1, "default_action": "allow",
        "rules": [{"host": "evil.example", "action": "deny", "note": None, "created_at": "t", "created_by": "a"}],
    })
    resp = await urlpolicy.handle_violations(store, _principal(permissions=["users.manage"]), fake_list_keys)
    body = json.loads(resp.body)
    assert body["count"] == 1
    assert body["violations"] == [{
        "slug": "promo", "owner": "bob", "status": "active", "target_url": "https://evil.example/x",
        "host": "evil.example", "reason": "denied_by_rule", "matched_rule": "evil.example",
    }]


async def test_violations_performs_zero_writes():
    store = FakeStore({
        "slug:promo": json.dumps({
            "slug": "promo", "owner": "bob", "status": "active", "target_url": "https://evil.example/x",
        }).encode("utf-8"),
    })
    await urlpolicy.save_policy(store, {
        "version": 1, "default_action": "deny", "rules": [],
    })
    before = dict(store._data)
    await urlpolicy.handle_violations(store, _principal(permissions=["users.manage"]), fake_list_keys)
    after = dict(store._data)
    assert after == before


async def test_violations_caps_at_max_violations_but_count_stays_exact():
    data = {}
    for i in range(urlpolicy.MAX_VIOLATIONS + 5):
        slug = f"s{i:04d}"
        data[f"slug:{slug}"] = json.dumps({
            "slug": slug, "owner": "bob", "status": "active", "target_url": "https://evil.example/x",
        }).encode("utf-8")
    store = FakeStore(data)
    await urlpolicy.save_policy(store, {
        "version": 1, "default_action": "allow",
        "rules": [{"host": "evil.example", "action": "deny", "note": None, "created_at": "t", "created_by": "a"}],
    })
    resp = await urlpolicy.handle_violations(store, _principal(permissions=["users.manage"]), fake_list_keys)
    body = json.loads(resp.body)
    assert body["count"] == urlpolicy.MAX_VIOLATIONS + 5
    assert len(body["violations"]) == urlpolicy.MAX_VIOLATIONS
    assert body["truncated"] is True


async def test_violations_sorted_by_slug():
    data = {
        "slug:zebra": json.dumps({"slug": "zebra", "owner": "a", "status": "active", "target_url": "https://evil.example/x"}).encode(),
        "slug:apple": json.dumps({"slug": "apple", "owner": "a", "status": "active", "target_url": "https://evil.example/x"}).encode(),
    }
    store = FakeStore(data)
    await urlpolicy.save_policy(store, {
        "version": 1, "default_action": "deny", "rules": [],
    })
    resp = await urlpolicy.handle_violations(store, _principal(permissions=["users.manage"]), fake_list_keys)
    body = json.loads(resp.body)
    assert [v["slug"] for v in body["violations"]] == ["apple", "zebra"]
