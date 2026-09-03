"""End-to-end proof that the destination URL policy is enforced in all
FOUR authoring paths — links.handle_create, links.handle_update (only when
target_url changes), bulk.handle_bulk_create, and bulk.handle_bulk_action's
"repoint" branch — not just some of them.
"A policy enforced in two of three places is not enforced" (now three of four).

Also proves: no admin bypass, and that the retroactive remediation path
(bulk disable of a pre-existing violator) still works.

Mutation check performed while writing this module (not committed, by
design — the point is that THIS module would have caught it): a script
temporarily removed the `verdict = urlpolicy.evaluate(...)` block from each
of links.handle_create, links.handle_update, bulk.validate_bulk_rows and
bulk.handle_bulk_action's repoint branch in turn, one at a time, re-ran
`uv run pytest tests/test_url_policy_enforcement.py`, and restored the
original source. Each removal made this module fail:
  - links.handle_create removed  -> test_create_rejects_across_both_policy_configs
    (both params) and test_admin_is_not_exempt_from_the_policy (both params)
    failed with "201 == 400" (the violating link was created).
  - links.handle_update removed  -> test_update_rejects_across_both_policy_configs
    (both params) failed with "200 == 400" (the violating edit was saved).
  - bulk.validate_bulk_rows' policy check removed -> test_bulk_create_rejects_
    across_both_policy_configs (both params) and test_admin_is_not_exempt_from_
    the_policy (both params) failed with "201 == 400" (the violating row was
    created).
  - bulk.handle_bulk_action's repoint policy check removed -> test_bulk_repoint_
    rejects_across_both_policy_configs (both params) and
    test_admin_is_not_exempt_from_the_policy (both params) failed with
    "200 == 400" (the violating repoint was written).
Confirms this module actually exercises all four enforcement points rather
than asserting a shape that would pass vacuously.
"""

import json

import pytest

import auth
import bulk
import kvretry
import links
import urlpolicy
from responses import Request
from tests.fakes import FakeStore, fake_get_many

VIOLATING_URL = "https://evil.example/x"
OK_URL = "https://good.example/y"

# The two ways an operator can express "block evil.example": a default-allow
# policy carrying an explicit deny rule, and a default-deny policy that
# simply never allows it (no allow rule mentions it). Both must reject the
# same destination through all three authoring paths.
POLICY_CONFIGS = [
    pytest.param({"default_action": "allow", "rules": [{"host": "evil.example", "action": "deny"}]}, id="default-allow+deny-rule"),
    pytest.param({"default_action": "deny", "rules": [{"host": "good.example", "action": "allow"}]}, id="default-deny+allow-rule"),
]


def _principal(username="alice", role="user", permissions=None):
    return auth.Principal(username=username, role=role, permissions=permissions or [], csrf_token="x")


def _admin():
    return auth.Principal(username="admin", role="admin", permissions=[], csrf_token="x")


def _links_request(payload):
    return Request(method="POST", uri="/api/links", headers={}, body=json.dumps(payload).encode("utf-8"))


def _bulk_request(payload):
    return Request(method="POST", uri="/api/links/bulk", headers={}, body=json.dumps(payload).encode("utf-8"))


def _bulk_action_request(payload):
    return Request(method="POST", uri="/api/links/bulk-action", headers={}, body=json.dumps(payload).encode("utf-8"))


def _put_policy_request(payload):
    return Request(method="PUT", uri="/api/admin/url-policy", headers={}, body=json.dumps(payload).encode("utf-8"))


CONFIGURED_DOMAINS = ["https://trrk.io", "http://localhost:3000"]


async def _save_policy(store, config):
    """Saves the policy THROUGH the real handler (handle_put_policy), not by
    poking urlpolicy.save_policy directly — this is the same path an admin
    actually uses, and it exercises parse_policy_document end to end too."""
    resp = await urlpolicy.handle_put_policy(store, _admin(), _put_policy_request(config))
    assert resp.status == 200


@pytest.mark.parametrize("policy_config", POLICY_CONFIGS)
async def test_create_rejects_across_both_policy_configs(policy_config):
    store = FakeStore()
    await _save_policy(store, policy_config)
    before = dict(store._data)

    resp = await links.handle_create(store, _principal(), _links_request({"target_url": VIOLATING_URL}), CONFIGURED_DOMAINS)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "destination_not_allowed"

    after = dict(store._data)
    assert after == before  # nothing written


@pytest.mark.parametrize("policy_config", POLICY_CONFIGS)
async def test_update_rejects_across_both_policy_configs(policy_config):
    store = FakeStore()
    create_resp = await links.handle_create(store, _principal(), _links_request({"target_url": OK_URL}), CONFIGURED_DOMAINS)
    slug = json.loads(create_resp.body)["slug"]

    await _save_policy(store, policy_config)
    before = dict(store._data)

    resp = await links.handle_update(store, _principal(), slug, _links_request({"target_url": VIOLATING_URL}), CONFIGURED_DOMAINS)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "destination_not_allowed"

    after = dict(store._data)
    assert after == before  # nothing written, record unchanged


@pytest.mark.parametrize("policy_config", POLICY_CONFIGS)
async def test_bulk_create_rejects_across_both_policy_configs(policy_config):
    store = FakeStore()
    await _save_policy(store, policy_config)
    before = dict(store._data)

    text = f"good-one,{OK_URL}\nbad-one,{VIOLATING_URL}\n"
    resp = await bulk.handle_bulk_create(
        store, _principal(permissions=["links.create_custom_slug"]), _bulk_request({"text": text}), CONFIGURED_DOMAINS, fake_get_many, kvretry.direct)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert any(e["error"] == "destination_not_allowed" for e in body["row_errors"])

    after = dict(store._data)
    assert after == before  # ALL-OR-NOTHING: the good row was not written either


@pytest.mark.parametrize("policy_config", POLICY_CONFIGS)
async def test_bulk_repoint_rejects_across_both_policy_configs(policy_config):
    store = FakeStore()
    users_store = FakeStore()
    create_resp = await links.handle_create(store, _principal(), _links_request({"target_url": OK_URL}), CONFIGURED_DOMAINS)
    slug = json.loads(create_resp.body)["slug"]

    await _save_policy(store, policy_config)
    before = dict(store._data)

    resp = await bulk.handle_bulk_action(
        store, users_store, _principal(),
        _bulk_action_request({"slugs": [slug], "action": "repoint", "target_url": VIOLATING_URL}),
        CONFIGURED_DOMAINS, fake_get_many, kvretry.direct)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "destination_not_allowed"

    after = dict(store._data)
    assert after == before  # nothing written, record unchanged


@pytest.mark.parametrize("policy_config", POLICY_CONFIGS)
async def test_admin_is_not_exempt_from_the_policy(policy_config):
    """Enforcement applies to every principal, role == "admin" included. An
    admin who wants an exception edits the policy — one action, stamped with
    updated_by — not a silent bypass."""
    store = FakeStore()
    users_store = FakeStore()
    await _save_policy(store, policy_config)

    create_resp = await links.handle_create(store, _admin(), _links_request({"target_url": VIOLATING_URL}), CONFIGURED_DOMAINS)
    assert create_resp.status == 400
    assert json.loads(create_resp.body)["error"] == "destination_not_allowed"

    bulk_resp = await bulk.handle_bulk_create(store, _admin(), _bulk_request({"text": VIOLATING_URL + "\n"}), CONFIGURED_DOMAINS, fake_get_many, kvretry.direct)
    assert bulk_resp.status == 400
    assert json.loads(bulk_resp.body)["row_errors"][0]["error"] == "destination_not_allowed"

    ok_create_resp = await links.handle_create(store, _admin(), _links_request({"target_url": OK_URL}), CONFIGURED_DOMAINS)
    ok_slug = json.loads(ok_create_resp.body)["slug"]
    repoint_resp = await bulk.handle_bulk_action(
        store, users_store, _admin(),
        _bulk_action_request({"slugs": [ok_slug], "action": "repoint", "target_url": VIOLATING_URL}),
        CONFIGURED_DOMAINS, fake_get_many, kvretry.direct)
    assert repoint_resp.status == 400
    assert json.loads(repoint_resp.body)["error"] == "destination_not_allowed"


async def test_legacy_violator_can_still_be_bulk_disabled():
    """The retroactive decision's whole point: a link that already violates a
    newly-added rule stays fully editable, and the recommended remediation —
    bulk Disable — must keep working even though the link's own destination
    would now be rejected if resubmitted."""
    store = FakeStore()
    users_store = FakeStore()
    create_resp = await links.handle_create(store, _principal(), _links_request({"target_url": VIOLATING_URL}), CONFIGURED_DOMAINS)
    slug = json.loads(create_resp.body)["slug"]

    await _save_policy(store, {"default_action": "allow", "rules": [{"host": "evil.example", "action": "deny"}]})

    resp = await bulk.handle_bulk_action(
        store, users_store, _principal(),
        Request(method="POST", uri="/api/links/bulk-action", headers={}, body=json.dumps({
            "slugs": [slug], "action": "disable",
        }).encode("utf-8")), CONFIGURED_DOMAINS, fake_get_many, kvretry.direct)
    assert resp.status == 200
    assert (await links.get_link(store, slug))["status"] == "disabled"
