"""End-to-end proof that allowed_domains is enforced in all FOUR authoring
paths — links.handle_create, links.handle_update, bulk.handle_bulk_create,
and bulk.handle_bulk_action's "restrict" action — not just some of them,
modelled file-for-file on test_url_policy_enforcement.py.
"A policy enforced in two of three places is not enforced" (now three of four).
"""

import json

import auth
import bulk
import kvretry
import links
from responses import Request
from tests.fakes import FakeStore, fake_get_many

CONFIGURED_DOMAINS = ["https://trrk.io", "http://localhost:3000"]
UNCONFIGURED_URL = "https://not-configured.example"


def _principal(username="alice", role="user", permissions=None):
    return auth.Principal(username=username, role=role, permissions=permissions or [], csrf_token="x")


def _links_request(payload):
    return Request(method="POST", uri="/api/links", headers={}, body=json.dumps(payload).encode("utf-8"))


def _update_request(slug, payload):
    return Request(method="PATCH", uri=f"/api/links/{slug}", headers={}, body=json.dumps(payload).encode("utf-8"))


def _bulk_request(payload):
    return Request(method="POST", uri="/api/links/bulk", headers={}, body=json.dumps(payload).encode("utf-8"))


def _bulk_action_request(payload):
    return Request(method="POST", uri="/api/links/bulk-action", headers={}, body=json.dumps(payload).encode("utf-8"))


async def test_create_rejects_unconfigured_domain_and_writes_nothing():
    store = FakeStore()
    before = dict(store._data)

    resp = await links.handle_create(
        store, _principal(),
        _links_request({"target_url": "https://example.com/x", "allowed_domains": [UNCONFIGURED_URL]}),
        CONFIGURED_DOMAINS,
    )
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_allowed_domains"

    after = dict(store._data)
    assert after == before  # nothing written


async def test_update_rejects_unconfigured_domain_and_writes_nothing():
    store = FakeStore()
    created = await links.handle_create(
        store, _principal(), _links_request({"target_url": "https://example.com/x"}), CONFIGURED_DOMAINS,
    )
    slug = json.loads(created.body)["slug"]
    before = dict(store._data)

    resp = await links.handle_update(
        store, _principal(), slug,
        _update_request(slug, {"allowed_domains": [UNCONFIGURED_URL]}),
        CONFIGURED_DOMAINS,
    )
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_allowed_domains"

    after = dict(store._data)
    assert after == before  # record unchanged


async def test_bulk_create_rejects_unconfigured_domain_and_writes_nothing():
    store = FakeStore()
    before = dict(store._data)

    resp = await bulk.handle_bulk_create(
        store, _principal(permissions=["links.create_custom_slug"]),
        _bulk_request({"text": "https://example.com/x", "allowed_domains": [UNCONFIGURED_URL]}),
        CONFIGURED_DOMAINS, fake_get_many, kvretry.direct,
    )
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_allowed_domains"

    after = dict(store._data)
    assert after == before  # nothing written


async def test_bulk_action_restrict_rejects_unconfigured_domain_and_writes_nothing():
    store = FakeStore()
    users_store = FakeStore()
    created = await links.handle_create(
        store, _principal(), _links_request({"target_url": "https://example.com/x"}), CONFIGURED_DOMAINS,
    )
    slug = json.loads(created.body)["slug"]
    before = dict(store._data)

    resp = await bulk.handle_bulk_action(
        store, users_store, _principal(),
        _bulk_action_request({"slugs": [slug], "action": "restrict", "allowed_domains": [UNCONFIGURED_URL]}),
        CONFIGURED_DOMAINS, fake_get_many, kvretry.direct,
    )
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_allowed_domains"

    after = dict(store._data)
    assert after == before  # nothing written, record unchanged


# --- No-silent-drop pin: every OTHER write path leaves allowed_domains
# byte-identical --- (mirrors test_url_policy_enforcement.py's structure and
# the plan's "no existing write path can silently drop allowed_domains")


async def test_update_target_url_only_leaves_allowed_domains_untouched():
    store = FakeStore()
    created = await links.handle_create(
        store, _principal(),
        _links_request({"target_url": "https://example.com/x", "allowed_domains": ["https://trrk.io"]}),
        CONFIGURED_DOMAINS,
    )
    slug = json.loads(created.body)["slug"]

    resp = await links.handle_update(
        store, _principal(), slug, _update_request(slug, {"target_url": "https://example.com/y"}), CONFIGURED_DOMAINS,
    )
    assert resp.status == 200
    record = await links.get_link(store, slug)
    assert record["allowed_domains"] == ["https://trrk.io"]


async def test_handle_set_password_leaves_allowed_domains_untouched():
    store = FakeStore()
    created = await links.handle_create(
        store, _principal(),
        _links_request({"target_url": "https://example.com/x", "allowed_domains": ["https://trrk.io"]}),
        CONFIGURED_DOMAINS,
    )
    slug = json.loads(created.body)["slug"]

    resp = await links.handle_set_password(
        store, _principal(), slug,
        Request(method="POST", uri=f"/api/links/{slug}/password", headers={}, body=json.dumps({"password": "longenough"}).encode("utf-8")),
    )
    assert resp.status == 200
    record = await links.get_link(store, slug)
    assert record["allowed_domains"] == ["https://trrk.io"]


async def _make_restricted_link(store, slug_target="https://example.com/x"):
    created = await links.handle_create(
        store, _principal(),
        _links_request({"target_url": slug_target, "allowed_domains": ["https://trrk.io"]}),
        CONFIGURED_DOMAINS,
    )
    return json.loads(created.body)["slug"]


async def test_other_bulk_actions_leave_allowed_domains_byte_identical():
    other_actions = [
        ({"action": "enable"}, []),
        ({"action": "disable"}, []),
        ({"action": "tag", "tags": ["sale"]}, ["links.tag"]),
        ({"action": "untag", "tags": ["sale"]}, ["links.tag"]),
        ({"action": "reassign", "owner": "bob"}, ["users.manage"]),
        ({"action": "repoint", "target_url": "https://example.com/new"}, []),
        ({"action": "schedule", "end_at": "2028-01-01T00:00:00Z"}, []),
    ]
    for action_payload, permissions in other_actions:
        store = FakeStore()
        users_store = FakeStore()
        if action_payload["action"] == "reassign":
            await users_store.set(
                "user:bob",
                json.dumps({
                    "username": "bob", "password_hash": auth.hash_password("longenough"),
                    "role": "user", "permissions": [], "disabled": False,
                }).encode("utf-8"),
            )
        slug = await _make_restricted_link(store)
        principal = _principal(permissions=permissions)
        resp = await bulk.handle_bulk_action(
            store, users_store, principal,
            _bulk_action_request({"slugs": [slug], **action_payload}),
            CONFIGURED_DOMAINS, fake_get_many, kvretry.direct,
        )
        assert resp.status == 200, (action_payload["action"], resp.body)
        record = await links.get_link(store, slug)
        assert record["allowed_domains"] == ["https://trrk.io"], action_payload["action"]
