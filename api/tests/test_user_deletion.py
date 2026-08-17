"""End-to-end regression test for docs/plans/user-deletion-link-ownership.md.

Pins the two reported/discovered vulnerabilities directly, as scenarios
rather than as unit assertions on a single function, since neither defect
was visible from any single module in isolation:

1. **Link inheritance via username reuse.** Deleting a user used to leave
   their links owned by a username string that could be recreated, handing
   the new account the old one's links.
2. **Session revival.** Deleting a user used to leave their session tokens
   live; recreating the username revived them with the new account's role.

Deliberately cross-module (users + links + bulk + auth), since the whole
point of both scenarios is that no single module's own tests would catch
either — each module behaved correctly in isolation.
"""

import json

import auth
import bulk
import kvretry
import links
import users
from responses import Request
from tests.fakes import FakeStore, fake_get_many, fake_list_keys

CONFIGURED_DOMAINS = ["https://a.example.com"]


def _principal(username, role="user", permissions=None):
    return auth.Principal(username=username, role=role, permissions=permissions or [], csrf_token="x")


def _links_request(payload):
    return Request(method="POST", uri="/api/links", headers={}, body=json.dumps(payload).encode("utf-8"))


def _update_request(payload):
    return Request(method="PATCH", uri="/api/links/x", headers={}, body=json.dumps(payload).encode("utf-8"))


def _action_request(payload):
    return Request(method="POST", uri="/api/links/bulk-action", headers={}, body=json.dumps(payload).encode("utf-8"))


async def _make_user(users_store, username, password="longenough", role="user", permissions=None):
    payload = {"username": username, "password": password, "role": role, "permissions": permissions or []}
    resp = await users.handle_create(
        users_store, _principal("admin", role="admin"), Request(method="POST", uri="/api/users", headers={}, body=json.dumps(payload).encode("utf-8")), CONFIGURED_DOMAINS,
    )
    assert resp.status == 201, resp.body
    return resp


async def test_reported_sequence_inheritance_no_longer_happens():
    users_store = FakeStore()
    links_store = FakeStore()
    admin = _principal("admin", role="admin")

    # carol is created and makes a custom slug.
    await _make_user(users_store, "carol", password="carolspassword", permissions=["links.create_custom_slug"])
    carol = _principal("carol", permissions=["links.create_custom_slug"])
    created = await links.handle_create(
        links_store, carol, _links_request({"target_url": "https://internal.example.com/carols-secret", "custom_slug": "carol-private"}),
    )
    assert created.status == 201

    # admin tries to delete carol -> refused, she still owns a link.
    resp = await users.handle_delete(users_store, links_store, admin, "carol", fake_list_keys)
    assert resp.status == 409
    assert json.loads(resp.body) == {"error": "user_owns_links", "username": "carol", "link_count": 1}

    # The link is reassigned to dave through the bulk tool.
    await _make_user(users_store, "dave", password="davespassword")
    reassign = await bulk.handle_bulk_action(
        links_store, users_store, admin, _action_request({"slugs": ["carol-private"], "action": "reassign", "owner": "dave"}), fake_get_many, kvretry.direct)
    assert reassign.status == 200

    # carol is now deletable.
    resp = await users.handle_delete(users_store, links_store, admin, "carol", fake_list_keys)
    assert resp.status == 200

    # A NEW carol is created, with a different password.
    await _make_user(users_store, "carol", password="differentpassword", permissions=["links.create_custom_slug"])
    new_carol = _principal("carol", permissions=["links.create_custom_slug"])

    # The new carol sees zero links.
    listing = await links.handle_list(links_store, new_carol, fake_get_many)
    assert json.loads(listing.body)["links"] == []

    # The new carol cannot edit the old carol-private link.
    update = await links.handle_update(links_store, new_carol, "carol-private", _update_request({"target_url": "https://attacker.example.com/"}))
    assert update.status == 403

    # The record's target_url is unchanged.
    record = await links.get_link(links_store, "carol-private")
    assert record["target_url"] == "https://internal.example.com/carols-secret"
    assert record["owner"] == "dave"


async def test_session_revival_no_longer_happens():
    users_store = FakeStore()
    links_store = FakeStore()
    admin = _principal("admin", role="admin")

    await _make_user(users_store, "carol", password="carolspassword")
    token, _ = await auth.create_session(users_store, "carol", "local")

    # Before deletion, the session resolves to a Principal.
    request = Request(method="GET", uri="/api/auth/me", headers={"cookie": f"session={token}"}, body=None)
    principal = await auth.resolve_session(users_store, request)
    assert principal is not None
    assert principal.username == "carol"
    assert principal.role == "user"

    # carol owns no links, so deletion succeeds outright.
    resp = await users.handle_delete(users_store, links_store, admin, "carol", fake_list_keys)
    assert resp.status == 200

    # After deletion, the same token resolves to None.
    assert await auth.resolve_session(users_store, request) is None

    # A new carol is created with an escalated role, under the same username,
    # reusing the same still-unexpired token. It must STILL resolve to None.
    await _make_user(users_store, "carol", password="newpassword", role="admin")
    assert await auth.resolve_session(users_store, request) is None
