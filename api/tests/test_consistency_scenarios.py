"""Seeds each of the six surviving inconsistencies one at a time and asserts
the endpoint reports exactly it — the "a checker that cries wolf on a
healthy store is worse than none" property from
docs/plans/kv-consistency-check.md.

docs/plans/derived-link-indexes.md, Stage 2: six of the original twelve
checks (everything about all_links/owner_links:<owner>) were retired along
with the indexes they described, because links.py no longer writes either
index — see consistency.py's module docstring. This file used to seed all
twelve, including the "motivating case" (an owner_links: drift that used to
hide a link from users.handle_delete's 409 gate) and the 2026-08-15
throttled-index-write incident's repair. Both scenarios are gone along with
the mechanism they exercised: the gate has derived ownership from records
since Stage 1 regardless of any index's state, and a throttled bulk create
can no longer produce index drift at all because there is no index write
left to throttle (see api/bulk.py's write-abandonment comments). What
replaces them here is the upgrade-path property Stage 2 introduces: a store
carrying leftover, never-again-written index keys must still report ok: true
alongside a healthy set of real links.

Also carries the healthy-store test (built through the real handlers).
"""

import json

import auth
import bulk
import consistency
import kvretry
import links
import users
from responses import Request
from tests.fakes import FakeStore, fake_get_many, fake_list_keys

CONFIGURED_DOMAINS = ["https://a.example.com"]

ALL_CHECK_IDS = [check_id for check_id, _ in consistency.CHECKS]


def _principal(username, role="user", permissions=None):
    return auth.Principal(username=username, role=role, permissions=permissions or [], csrf_token="x")


def _j(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


def _create_user_request(payload):
    return Request(method="POST", uri="/api/users", headers={}, body=json.dumps(payload).encode("utf-8"))


def _links_request(payload):
    return Request(method="POST", uri="/api/links", headers={}, body=json.dumps(payload).encode("utf-8"))


def _bulk_create_request(payload):
    return Request(method="POST", uri="/api/links/bulk", headers={}, body=json.dumps(payload).encode("utf-8"))


def _action_request(payload):
    return Request(method="POST", uri="/api/links/bulk-action", headers={}, body=json.dumps(payload).encode("utf-8"))


async def _make_user(users_store, admin, username, password="longenough", role="user", permissions=None):
    resp = await users.handle_create(
        users_store, admin, _create_user_request(
            {"username": username, "password": password, "role": role, "permissions": permissions or []}
        ), CONFIGURED_DOMAINS,
    )
    assert resp.status == 201, resp.body
    return resp


def _by_id(checks: list[dict]) -> dict[str, dict]:
    return {c["check"]: c for c in checks}


async def _report(links_store, users_store, principal=None) -> dict:
    principal = principal or _principal("admin", role="admin")
    resp = await consistency.handle_consistency(
        {"links": links_store, "users": users_store}, principal, fake_list_keys, fake_get_many)
    assert resp.status == 200, resp.body
    return json.loads(resp.body)


def _assert_only_this_check_fired(report: dict, expect_check: str, also_expect: tuple[str, ...] = ()) -> dict:
    """Asserts `expect_check` (and, if given, the documented `also_expect`
    companions) have findings, and every other one of the six is at 0."""
    by_id = _by_id(report["checks"])
    exempt = {expect_check, *also_expect}
    for check_id in ALL_CHECK_IDS:
        if check_id in exempt:
            continue
        assert by_id[check_id]["count"] == 0, f"{check_id} unexpectedly fired: {by_id[check_id]}"
    assert report["ok"] is False
    return by_id


# --- One seed per surviving check ---


async def test_seed_unknown_link_owner():
    links_store = FakeStore({"slug:x": _j({"owner": "nobody"}), "all_links": _j(["x"])})
    users_store = FakeStore()
    report = await _report(links_store, users_store)
    by_id = _assert_only_this_check_fired(report, "unknown_link_owner")
    assert by_id["unknown_link_owner"]["findings"] == [{"slug": "x", "owner": "nobody"}]


async def test_seed_unindexed_user():
    users_store = FakeStore({"user:alice": _j({}), "_meta:usernames": _j([])})
    links_store = FakeStore()
    report = await _report(links_store, users_store)
    by_id = _assert_only_this_check_fired(report, "unindexed_user")
    assert by_id["unindexed_user"]["findings"] == [{"username": "alice"}]


async def test_seed_missing_user_record():
    users_store = FakeStore({"_meta:usernames": _j(["ghost"])})
    links_store = FakeStore()
    report = await _report(links_store, users_store)
    by_id = _assert_only_this_check_fired(report, "missing_user_record")
    assert by_id["missing_user_record"]["findings"] == [{"username": "ghost"}]


async def test_seed_orphan_session():
    users_store = FakeStore({
        "session:tok1": _j({"username": "nobody", "csrf_token": "x", "issued_at": 0, "expires_at": 99999999999}),
    })
    links_store = FakeStore()
    report = await _report(links_store, users_store)
    by_id = _assert_only_this_check_fired(report, "orphan_session")
    assert by_id["orphan_session"]["findings"] == [{"username": "nobody", "session_count": 1}]


async def test_seed_unreadable_value():
    links_store = FakeStore({"slug:bad": b"not-json"})
    users_store = FakeStore()
    report = await _report(links_store, users_store)
    by_id = _assert_only_this_check_fired(report, "unreadable_value")
    assert by_id["unreadable_value"]["findings"] == [
        {"store": "links", "key": "slug:bad", "reason": "Expecting value: line 1 column 1 (char 0)"}
    ]


async def test_seed_unrecognized_key():
    links_store = FakeStore({"junk": b"whatever"})
    users_store = FakeStore()
    report = await _report(links_store, users_store)
    by_id = _assert_only_this_check_fired(report, "unrecognized_key")
    assert by_id["unrecognized_key"]["findings"] == [{"store": "links", "key": "junk"}]


# --- Healthy store, built through the real handlers ---


async def test_healthy_store_through_real_handlers_reports_all_clear():
    links_store = FakeStore()
    users_store = FakeStore()
    admin = _principal("admin", role="admin")

    await _make_user(users_store, admin, "bob")
    await _make_user(users_store, admin, "carol")
    await _make_user(users_store, admin, "dave")

    bob = _principal("bob")
    carol = _principal("carol")

    created_bob = await links.handle_create(
        links_store, bob, _links_request({"target_url": "https://example.com/bob"}), CONFIGURED_DOMAINS
    )
    assert created_bob.status == 201

    created_carol = await links.handle_create(
        links_store, carol, _links_request({"target_url": "https://example.com/carol"}), CONFIGURED_DOMAINS
    )
    assert created_carol.status == 201
    carol_slug = json.loads(created_carol.body)["slug"]

    bulk_created = await bulk.handle_bulk_create(
        links_store, bob, _bulk_create_request({"text": "https://example.com/one\nhttps://example.com/two"}),
        CONFIGURED_DOMAINS, fake_get_many, kvretry.direct)
    assert bulk_created.status == 201
    bulk_slugs = [record["slug"] for record in json.loads(bulk_created.body)["links"]]

    # Reassign carol's link to bob, so carol will own nothing.
    reassigned = await bulk.handle_bulk_action(
        links_store, users_store, admin,
        _action_request({"slugs": [carol_slug], "action": "reassign", "owner": "bob"}),
        CONFIGURED_DOMAINS, fake_get_many, kvretry.direct)
    assert reassigned.status == 200

    # Delete one of the bulk-created links outright.
    deleted = await links.handle_delete(links_store, bob, bulk_slugs[0])
    assert deleted.status == 200

    # carol now owns nothing, so she's deletable.
    user_deleted = await users.handle_delete(users_store, links_store, admin, "carol", fake_list_keys, fake_get_many)
    assert user_deleted.status == 200

    report = await _report(links_store, users_store, admin)
    assert report["ok"] is True
    assert report["totals"]["checks_skipped"] == 0
    assert report["totals"]["findings"] == 0
    for check in report["checks"]:
        assert check["count"] == 0, check
        assert check["skipped"] is False


# --- The upgrade-path property Stage 2 introduces ---


async def test_healthy_store_with_leftover_index_keys_from_before_stage_2_reports_all_clear():
    """docs/plans/derived-link-indexes.md, Stage 2's own worked example, built
    through the real handlers rather than hand-constructed dicts (see
    test_consistency.py for the unit-level version): a store that has
    leftover all_links/owner_links:<U> keys from before this change landed —
    never written to again, never cleaned up (the plan explicitly leaves them
    in place as inert) — must still report ok: true once its links are
    healthy. A store that predates Stage 2 is exactly this shape."""
    links_store = FakeStore()
    users_store = FakeStore()
    admin = _principal("admin", role="admin")
    await _make_user(users_store, admin, "carol")
    carol = _principal("carol")

    created = await links.handle_create(
        links_store, carol, _links_request({"target_url": "https://example.com/spring-sale"}), CONFIGURED_DOMAINS,
    )
    assert created.status == 201
    slug = json.loads(created.body)["slug"]

    # Simulate leftover pre-Stage-2 index keys: stale, inconsistent with the
    # real record above, and never touched by any handler any more.
    await links_store.set("all_links", _j(["some-other-slug-that-never-existed"]))
    await links_store.set("owner_links:carol", _j([slug, "a-ghost-slug"]))

    report = await _report(links_store, users_store, admin)
    assert report["ok"] is True
    for check in report["checks"]:
        assert check["count"] == 0, check


async def test_motivating_case_owner_index_drift_no_longer_hides_carol_from_the_delete_gate():
    """The direct descendant of the old "motivating case" test. Before
    docs/plans/derived-link-indexes.md's Stage 1, users.handle_delete's 409
    gate read owner_links:<username> directly, so a drifted (or, since Stage
    2, simply never-updated) index entry could hide a link from it entirely.
    The gate now derives ownership from the records themselves
    (links.slugs_owned_by) regardless of what any owner_links: key says —
    including a leftover key that is flat-out wrong, as seeded here."""
    links_store = FakeStore()
    users_store = FakeStore()
    admin = _principal("admin", role="admin")

    await _make_user(users_store, admin, "carol")
    carol = _principal("carol")

    created = await links.handle_create(
        links_store, carol, _links_request({"target_url": "https://example.com/spring-sale"}), CONFIGURED_DOMAINS,
    )
    assert created.status == 201
    slug = json.loads(created.body)["slug"]

    # A leftover owner_links:carol key that names nothing at all — the exact
    # shape Stage 2 leaves behind (nothing writes this key any more, so it
    # simply never reflects the slug created above).
    await links_store.set("owner_links:carol", _j([]))

    # The consistency check no longer has any concept of this drift at all —
    # the key is known-and-inert, never parsed, never reported.
    report = await _report(links_store, users_store)
    assert report["ok"] is True

    # And the gate still correctly refuses to delete carol, entirely from the
    # record itself.
    resp = await users.handle_delete(users_store, links_store, admin, "carol", fake_list_keys, fake_get_many)
    assert resp.status == 409
    assert json.loads(resp.body)["error"] == "user_owns_links"
