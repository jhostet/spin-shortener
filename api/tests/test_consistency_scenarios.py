"""Seeds each of the twelve inconsistencies one at a time and asserts the
endpoint reports exactly it — the "a checker that cries wolf on a healthy
store is worse than none" property from docs/plans/kv-consistency-check.md.

Also carries the healthy-store test (built through the real handlers) and
the motivating-case test: `unindexed_owner_link` is the check that detects
the known hole in `users.handle_delete`'s index-read 409 gate, and this test
pins both halves — the report shows the drift, and the deletion the gate was
built to prevent still goes through.

One documented exception to "all eleven others stay at 0": `dangling_owner_index`
(check 7) cannot be seeded in isolation. Its own definition — `owner_links:<U>`
non-empty, `U` unknown — forces whatever slug it lists into exactly one of
three states: no record at all (`orphan_owner_index_entry`), a record whose
owner also happens to be `U` (`unknown_link_owner`), or a record owned by
someone else (`owner_index_mismatch`). There is no fourth option, so one of
those three checks always co-fires. This matches the plan's own live
verification table, which pairs this exact seed with
`orphan_owner_index_entry` for the same reason.
"""

import json

import auth
import bulk
import consistency
import consistencyrepair
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
    companions) have findings, and every other one of the twelve is at 0."""
    by_id = _by_id(report["checks"])
    exempt = {expect_check, *also_expect}
    for check_id in ALL_CHECK_IDS:
        if check_id in exempt:
            continue
        assert by_id[check_id]["count"] == 0, f"{check_id} unexpectedly fired: {by_id[check_id]}"
    assert report["ok"] is False
    return by_id


# --- One seed per check ---


async def test_seed_unindexed_link():
    links_store = FakeStore({
        "slug:foo": _j({"owner": "carol"}),
        "all_links": _j([]),
        "owner_links:carol": _j(["foo"]),
    })
    users_store = FakeStore({"user:carol": _j({}), "_meta:usernames": _j(["carol"])})
    report = await _report(links_store, users_store)
    by_id = _assert_only_this_check_fired(report, "unindexed_link")
    assert by_id["unindexed_link"]["findings"] == [{"slug": "foo", "owner": "carol"}]


async def test_seed_missing_link_record():
    links_store = FakeStore({"all_links": _j(["ghost"])})
    users_store = FakeStore()
    report = await _report(links_store, users_store)
    by_id = _assert_only_this_check_fired(report, "missing_link_record")
    assert by_id["missing_link_record"]["findings"] == [{"slug": "ghost"}]


async def test_seed_unindexed_owner_link():
    links_store = FakeStore({
        "slug:spring-sale": _j({"owner": "carol"}),
        "all_links": _j(["spring-sale"]),
        "owner_links:carol": _j([]),
    })
    users_store = FakeStore({"user:carol": _j({}), "_meta:usernames": _j(["carol"])})
    report = await _report(links_store, users_store)
    by_id = _assert_only_this_check_fired(report, "unindexed_owner_link")
    assert by_id["unindexed_owner_link"]["findings"] == [{"slug": "spring-sale", "owner": "carol"}]


async def test_seed_owner_index_mismatch():
    links_store = FakeStore({
        "slug:x": _j({"owner": "dave"}),
        "all_links": _j(["x"]),
        "owner_links:carol": _j(["x"]),
        "owner_links:dave": _j(["x"]),
    })
    users_store = FakeStore({
        "user:carol": _j({}), "user:dave": _j({}), "_meta:usernames": _j(["carol", "dave"]),
    })
    report = await _report(links_store, users_store)
    by_id = _assert_only_this_check_fired(report, "owner_index_mismatch")
    assert by_id["owner_index_mismatch"]["findings"] == [
        {"slug": "x", "indexed_under": "carol", "record_owner": "dave"}
    ]


async def test_seed_orphan_owner_index_entry():
    links_store = FakeStore({"owner_links:carol": _j(["ghost"]), "all_links": _j([])})
    users_store = FakeStore({"user:carol": _j({}), "_meta:usernames": _j(["carol"])})
    report = await _report(links_store, users_store)
    by_id = _assert_only_this_check_fired(report, "orphan_owner_index_entry")
    assert by_id["orphan_owner_index_entry"]["findings"] == [{"slug": "ghost", "indexed_under": "carol"}]


async def test_seed_unknown_link_owner():
    links_store = FakeStore({"slug:x": _j({"owner": "nobody"}), "all_links": _j(["x"])})
    users_store = FakeStore()
    report = await _report(links_store, users_store)
    by_id = _assert_only_this_check_fired(report, "unknown_link_owner")
    assert by_id["unknown_link_owner"]["findings"] == [{"slug": "x", "owner": "nobody"}]


async def test_seed_dangling_owner_index():
    """See the module docstring: this check cannot be seeded without also
    tripping exactly one of orphan_owner_index_entry / unknown_link_owner /
    owner_index_mismatch, by construction. This seed (a dangling index
    entry pointing at a slug with no record at all) is the one the plan's
    own live verification table uses, and pairs it with the same companion."""
    links_store = FakeStore({"owner_links:nobody": _j(["ghost"]), "all_links": _j([])})
    users_store = FakeStore()
    report = await _report(links_store, users_store)
    by_id = _assert_only_this_check_fired(
        report, "dangling_owner_index", also_expect=("orphan_owner_index_entry",)
    )
    assert by_id["dangling_owner_index"]["findings"] == [{"username": "nobody", "slug_count": 1}]
    assert by_id["orphan_owner_index_entry"]["findings"] == [{"slug": "ghost", "indexed_under": "nobody"}]


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
    assert by_id["unreadable_value"]["findings"] == [{"store": "links", "key": "slug:bad"}]


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
        links_store, bob, _links_request({"target_url": "https://example.com/bob"})
    )
    assert created_bob.status == 201

    created_carol = await links.handle_create(
        links_store, carol, _links_request({"target_url": "https://example.com/carol"})
    )
    assert created_carol.status == 201
    carol_slug = json.loads(created_carol.body)["slug"]

    bulk_created = await bulk.handle_bulk_create(
        links_store, bob, _bulk_create_request({"text": "https://example.com/one\nhttps://example.com/two"}), fake_get_many, kvretry.direct)
    assert bulk_created.status == 201
    bulk_slugs = [record["slug"] for record in json.loads(bulk_created.body)["links"]]

    # Reassign carol's link to bob, so carol will own nothing.
    reassigned = await bulk.handle_bulk_action(
        links_store, users_store, admin,
        _action_request({"slugs": [carol_slug], "action": "reassign", "owner": "bob"}), fake_get_many, kvretry.direct)
    assert reassigned.status == 200

    # Delete one of the bulk-created links outright.
    deleted = await links.handle_delete(links_store, bob, bulk_slugs[0])
    assert deleted.status == 200

    # carol now owns nothing, so she's deletable.
    user_deleted = await users.handle_delete(users_store, links_store, admin, "carol", fake_list_keys)
    assert user_deleted.status == 200

    report = await _report(links_store, users_store, admin)
    assert report["ok"] is True
    assert report["totals"]["checks_skipped"] == 0
    assert report["totals"]["findings"] == 0
    for check in report["checks"]:
        assert check["count"] == 0, check
        assert check["skipped"] is False


# --- The motivating case ---


async def test_motivating_case_unindexed_owner_link_pins_the_user_deletion_gap():
    links_store = FakeStore()
    users_store = FakeStore()
    admin = _principal("admin", role="admin")

    await _make_user(users_store, admin, "carol")
    carol = _principal("carol")

    created = await links.handle_create(
        links_store, carol, _links_request({"target_url": "https://example.com/spring-sale"}),
    )
    assert created.status == 201
    slug = json.loads(created.body)["slug"]

    # Drift: carol's record still names her as owner, but her own index no
    # longer lists the slug (an interrupted write, or a KV-explorer edit).
    # This is exactly the state users.handle_delete's 409 gate cannot see,
    # because that gate reads only this index.
    owned = json.loads(await links_store.get("owner_links:carol"))
    assert slug in owned
    await links_store.set("owner_links:carol", json.dumps([s for s in owned if s != slug]).encode("utf-8"))

    report = await _report(links_store, users_store)
    by_id = _by_id(report["checks"])
    assert by_id["unindexed_owner_link"]["count"] == 1
    assert by_id["unindexed_owner_link"]["findings"] == [{"slug": slug, "owner": "carol"}]

    # The point of the whole feature: the gate reads the drifted index, not
    # the record, so carol is still deletable -- orphaning her own link.
    resp = await users.handle_delete(users_store, links_store, admin, "carol", fake_list_keys)
    assert resp.status == 200


# --- The 2026-08-15 throttled-write incident, and its repair ---------------


def _j(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


def _repair_request(payload):
    return Request(
        method="POST", uri="/api/admin/consistency/repair", headers={},
        body=json.dumps(payload).encode("utf-8"),
    )


async def test_the_2026_08_15_throttled_write_incident_repairs_in_two_writes():
    """Reproduces the incident recorded in docs/plans/consistency-repair.md:
    throttled index writes past Akamai's 50/s cap left 20 unindexed_link, 20
    unindexed_owner_link, 152 missing_link_record and 152
    orphan_owner_index_entry findings — 344 findings touching exactly two KV
    keys (`all_links` and `owner_links:admin`), which is the whole reason a
    repair here is cheap, bounded and safe."""
    links_data = {"all_links": _j([f"ghost{i}" for i in range(152)])}
    for i in range(20):
        links_data[f"slug:extra{i}"] = _j({"owner": "admin"})
    links_data["owner_links:admin"] = _j([f"gone{i}" for i in range(152)])
    users_data = {"user:admin": _j({}), "_meta:usernames": _j(["admin"])}

    links_store = FakeStore(links_data)
    users_store = FakeStore(users_data)
    admin = _principal("admin", role="admin")

    report = await consistency.handle_consistency(
        {"links": links_store, "users": users_store}, admin, fake_list_keys, fake_get_many)
    assert report.status == 200
    body = json.loads(report.body)
    assert body["ok"] is False
    by_id = _by_id(body["checks"])
    assert by_id["unindexed_link"]["count"] == 20
    assert by_id["unindexed_owner_link"]["count"] == 20
    assert by_id["missing_link_record"]["count"] == 152
    assert by_id["orphan_owner_index_entry"]["count"] == 152

    repair_resp = await consistencyrepair.handle_repair(
        {"links": links_store, "users": users_store}, admin,
        _repair_request({
            "confirm": "REPAIR",
            "checks": ["unindexed_link", "missing_link_record", "unindexed_owner_link", "orphan_owner_index_entry"],
        }),
        fake_list_keys, fake_get_many, kvretry.direct)
    assert repair_resp.status == 200
    repair_body = json.loads(repair_resp.body)
    assert repair_body["writes"] == 2
    assert repair_body["complete"] is True

    fresh_report = await consistency.handle_consistency(
        {"links": links_store, "users": users_store}, admin, fake_list_keys, fake_get_many)
    fresh_body = json.loads(fresh_report.body)
    assert fresh_body["ok"] is True
    assert len(fresh_body["checks"]) == 12
    for check in fresh_body["checks"]:
        assert check["count"] == 0, check
