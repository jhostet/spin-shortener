"""Unit tests for api/consistencyrepair.py: the pure planner (apply_list_delta,
plan_repairs), the sequential applier (apply_repairs), and the handler
(handle_repair). See docs/plans/consistency-repair.md for the design this
pins.
"""

import json

import auth
import consistency
import consistencyrepair as repair
from tests.fakes import FakeStore, fake_get_many, fake_list_keys


def _principal(username="admin", role="admin", permissions=None):
    return auth.Principal(username=username, role=role, permissions=permissions or [], csrf_token="x")


def _j(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


async def _collect_and_analyze(links_data=None, users_data=None):
    links_store = FakeStore(links_data or {})
    users_store = FakeStore(users_data or {})
    collected = await consistency.collect(
        {"links": links_store, "users": users_store}, fake_list_keys, fake_get_many)
    checks, _totals = consistency.analyze(collected, max_findings=None)
    return collected, checks


def _by_id(checks):
    return {c["check"]: c for c in checks}


# --- apply_list_delta --------------------------------------------------------


def test_apply_list_delta_removes_before_appending_and_preserves_order():
    assert repair.apply_list_delta(["a", "b", "c"], add=["d"], remove=["b"]) == ["a", "c", "d"]


def test_apply_list_delta_skips_an_add_already_present():
    assert repair.apply_list_delta(["a", "b"], add=["a"], remove=[]) == ["a", "b"]


def test_apply_list_delta_no_op_returns_equal_list():
    assert repair.apply_list_delta(["a", "b"], add=[], remove=["c"]) == ["a", "b"]


# --- plan_repairs: write-cost sharing (the core design property) -----------


async def _incident_data(with_owner_findings=False):
    links_data = {"all_links": _j([f"ghost{i}" for i in range(152)])}
    for i in range(20):
        links_data[f"slug:extra{i}"] = _j({"owner": "admin"})
    users_data = {"user:admin": _j({}), "_meta:usernames": _j(["admin"])}
    if with_owner_findings:
        links_data["owner_links:admin"] = _j([f"gone{i}" for i in range(152)])
        # owner_links:admin lists 152 slugs with no record at all (orphan_
        # owner_index_entry) and omits the 20 extra{i} slugs already created
        # above, which fires unindexed_owner_link for exactly those 20.
    return links_data, users_data


async def test_plan_repairs_one_write_for_the_observed_incident_shape():
    links_data, users_data = await _incident_data()
    collected, checks = await _collect_and_analyze(links_data, users_data)
    by_id = _by_id(checks)
    assert by_id["unindexed_link"]["count"] == 20
    assert by_id["missing_link_record"]["count"] == 152

    plan = repair.plan_repairs(collected, checks, ["unindexed_link", "missing_link_record"], budget=100)
    assert plan["planned_writes"] == 1
    assert list(plan["links"]["deltas"].keys()) == ["all_links"]
    assert len(plan["links"]["deltas"]["all_links"]["add"]) == 20
    assert len(plan["links"]["deltas"]["all_links"]["remove"]) == 152


async def test_plan_repairs_two_writes_when_owner_checks_are_added():
    links_data, users_data = await _incident_data(with_owner_findings=True)
    collected, checks = await _collect_and_analyze(links_data, users_data)
    by_id = _by_id(checks)
    assert by_id["unindexed_owner_link"]["count"] == 20
    assert by_id["orphan_owner_index_entry"]["count"] == 152

    plan = repair.plan_repairs(
        collected, checks,
        ["unindexed_link", "missing_link_record", "unindexed_owner_link", "orphan_owner_index_entry"],
        budget=100,
    )
    assert plan["planned_writes"] == 2
    assert set(plan["links"]["deltas"].keys()) == {"all_links", "owner_links:admin"}


# --- present_slugs guard: the sharpest hazard ------------------------------


async def test_corrupt_but_present_record_never_planned_for_removal_from_all_links():
    collected, checks = await _collect_and_analyze(
        links_data={"slug:bad": b"not-json", "all_links": _j(["bad"])},
    )
    by_id = _by_id(checks)
    assert by_id["missing_link_record"]["count"] == 1

    plan = repair.plan_repairs(collected, checks, ["missing_link_record"], budget=100)
    assert plan["links"]["deltas"] == {}
    assert plan["planned_writes"] == 0
    blocked = [b for b in plan["blocked"] if b["check"] == "missing_link_record"]
    assert blocked == [{"check": "missing_link_record", "slug": "bad", "reason": "record_unreadable", "next_step": "unreadable_value"}]
    check_report = _by_id(plan["checks"])["missing_link_record"]
    assert check_report["blocked"] == 1
    assert check_report["remaining"] == 0
    assert check_report["planned"] == 0


async def test_corrupt_but_present_record_never_planned_for_removal_from_owner_index():
    collected, checks = await _collect_and_analyze(
        links_data={
            "slug:bad": b"not-json",
            "owner_links:carol": _j(["bad"]),
            "all_links": _j([]),
        },
        users_data={"user:carol": _j({}), "_meta:usernames": _j(["carol"])},
    )
    by_id = _by_id(checks)
    assert by_id["orphan_owner_index_entry"]["count"] == 1

    plan = repair.plan_repairs(collected, checks, ["orphan_owner_index_entry"], budget=100)
    assert plan["links"]["deltas"] == {}
    blocked = [b for b in plan["blocked"] if b["check"] == "orphan_owner_index_entry"]
    assert blocked == [{
        "check": "orphan_owner_index_entry", "slug": "bad", "username": "carol",
        "reason": "record_unreadable", "next_step": "unreadable_value",
    }]


# --- dangling_owner_index precondition --------------------------------------


async def test_dangling_owner_index_blocked_when_it_would_orphan_an_unindexed_link():
    collected, checks = await _collect_and_analyze(
        links_data={
            "slug:zzreal": _j({"owner": "phantom"}),
            "owner_links:phantom": _j(["zzreal"]),
            "all_links": _j([]),  # zzreal is NOT indexed
        },
    )
    by_id = _by_id(checks)
    assert by_id["dangling_owner_index"]["count"] == 1

    plan = repair.plan_repairs(collected, checks, ["dangling_owner_index"], budget=100)
    assert plan["links"]["deletes"] == []
    blocked = [b for b in plan["blocked"] if b["check"] == "dangling_owner_index"]
    assert len(blocked) == 1
    assert blocked[0]["reason"] == "would_orphan_unindexed_link"
    assert blocked[0]["next_step"] == "unindexed_link"
    assert blocked[0]["slugs"] == ["zzreal"]


async def test_dangling_owner_index_not_blocked_when_unindexed_link_repaired_in_same_pass():
    collected, checks = await _collect_and_analyze(
        links_data={
            "slug:zzreal": _j({"owner": "phantom"}),
            "owner_links:phantom": _j(["zzreal"]),
            "all_links": _j([]),
        },
    )
    plan = repair.plan_repairs(collected, checks, ["dangling_owner_index", "unindexed_link"], budget=100)
    assert plan["links"]["deletes"] == ["owner_links:phantom"]
    assert not [b for b in plan["blocked"] if b["check"] == "dangling_owner_index"]
    # unindexed_link still adds zzreal to all_links in the same pass.
    assert plan["links"]["deltas"]["all_links"]["add"] == ["zzreal"]


async def test_dangling_owner_index_never_fires_on_ghost_only_owner():
    """Control: an owner index naming only slugs with no record at all
    (no present_slugs entry) is NOT at risk and deletes cleanly."""
    collected, checks = await _collect_and_analyze(
        links_data={"owner_links:phantom": _j(["ghost"]), "all_links": _j([])},
    )
    plan = repair.plan_repairs(collected, checks, ["dangling_owner_index"], budget=100)
    assert plan["links"]["deletes"] == ["owner_links:phantom"]
    assert not plan["blocked"]


async def test_all_dangling_deletions_blocked_when_all_links_is_unreadable():
    collected, checks = await _collect_and_analyze(
        links_data={
            "all_links": b"not-json",
            "owner_links:phantom": _j(["ghost"]),
            "owner_links:spooky": _j(["ghost2"]),
        },
    )
    plan = repair.plan_repairs(collected, checks, ["dangling_owner_index"], budget=100)
    assert plan["links"]["deletes"] == []
    reasons = {b["username"]: b["reason"] for b in plan["blocked"]}
    assert reasons == {"phantom": "links_index_unreadable", "spooky": "links_index_unreadable"}


# --- a key scheduled for deletion never also carries a delta ---------------


async def test_owner_index_entry_removal_superseded_by_dangling_deletion():
    collected, checks = await _collect_and_analyze(
        links_data={"owner_links:phantom": _j(["ghost"]), "all_links": _j([])},
    )
    plan = repair.plan_repairs(collected, checks, ["dangling_owner_index", "orphan_owner_index_entry"], budget=100)
    assert plan["links"]["deletes"] == ["owner_links:phantom"]
    # orphan_owner_index_entry's finding for the same key must NOT also
    # produce a delta on top of the deletion.
    assert "owner_links:phantom" not in plan["links"]["deltas"]
    report = _by_id(plan["checks"])["orphan_owner_index_entry"]
    assert report["planned"] == 1
    assert report["remaining"] == 0


# --- skipped checks -----------------------------------------------------


async def test_skipped_check_never_planned_and_never_blocks_completion():
    collected, checks = await _collect_and_analyze(
        links_data={"all_links": b"not-json", "slug:x": _j({"owner": "carol"})},
    )
    by_id = _by_id(checks)
    assert by_id["unindexed_link"]["skipped"] is True

    plan = repair.plan_repairs(collected, checks, ["unindexed_link", "missing_link_record"], budget=100)
    report = _by_id(plan["checks"])
    assert report["unindexed_link"]["skipped"] is True
    assert report["unindexed_link"]["skip_reason"] == "index_unreadable"
    assert report["unindexed_link"]["remaining"] == 0
    assert report["unindexed_link"]["planned"] == 0
    assert plan["planned_writes"] == 0


# --- unindexed_user: invalid (empty) username guard -------------------------


async def test_unindexed_user_skips_empty_username():
    collected, checks = await _collect_and_analyze(
        users_data={"user:": _j({}), "_meta:usernames": _j([])},
    )
    by_id = _by_id(checks)
    assert by_id["unindexed_user"]["count"] == 1

    plan = repair.plan_repairs(collected, checks, ["unindexed_user"], budget=100)
    assert plan["users"]["deltas"] == {}
    blocked = [b for b in plan["blocked"] if b["check"] == "unindexed_user"]
    assert blocked == [{"check": "unindexed_user", "username": "", "reason": "invalid_username", "next_step": None}]


# --- orphan_session -----------------------------------------------------


async def test_orphan_session_plans_a_delete_per_session_key():
    collected, checks = await _collect_and_analyze(
        users_data={
            "session:tok1": _j({"username": "nobody"}),
            "session:tok2": _j({"username": "nobody"}),
            "_meta:usernames": _j([]),
        },
    )
    plan = repair.plan_repairs(collected, checks, ["orphan_session"], budget=100)
    assert sorted(plan["users"]["deletes"]) == ["session:tok1", "session:tok2"]
    assert plan["planned_writes"] == 2


# --- budget ---------------------------------------------------------------


async def test_budget_is_respected_and_leaves_remaining_findings():
    links_data = {
        "owner_links:ghost1": _j(["a"]),
        "owner_links:ghost2": _j(["b"]),
        "all_links": _j([]),
    }
    collected, checks = await _collect_and_analyze(links_data)
    by_id = _by_id(checks)
    assert by_id["dangling_owner_index"]["count"] == 2

    plan = repair.plan_repairs(collected, checks, ["dangling_owner_index"], budget=1)
    assert plan["planned_writes"] == 1
    assert len(plan["links"]["deletes"]) == 1
    report = _by_id(plan["checks"])["dangling_owner_index"]
    assert report["planned"] == 1
    assert report["remaining"] == 1
    assert report["blocked"] == 0


async def test_two_runs_over_identical_input_produce_byte_identical_plans():
    links_data, users_data = await _incident_data(with_owner_findings=True)
    collected, checks = await _collect_and_analyze(links_data, users_data)
    requested = ["unindexed_link", "missing_link_record", "unindexed_owner_link", "orphan_owner_index_entry"]
    plan1 = repair.plan_repairs(collected, checks, requested, budget=100)
    plan2 = repair.plan_repairs(collected, checks, requested, budget=100)
    assert json.dumps(plan1, sort_keys=True) == json.dumps(plan2, sort_keys=True)


# --- module hygiene ---------------------------------------------------------


def test_no_spin_sdk_import():
    import inspect
    source = inspect.getsource(repair)
    assert "spin_sdk" not in source


# --- apply_repairs -----------------------------------------------------


async def test_apply_repairs_writes_links_before_users():
    order: list[str] = []

    class RecordingStore(FakeStore):
        def __init__(self, data, name):
            super().__init__(data)
            self.name = name

        async def set(self, key, value):
            order.append(self.name)
            await super().set(key, value)

        async def delete(self, key):
            order.append(self.name)
            await super().delete(key)

    links_store = RecordingStore({"all_links": _j(["x"])}, "links")
    users_store = RecordingStore({"_meta:usernames": _j(["y"])}, "users")
    plan = {
        "links": {"deltas": {"all_links": {"add": ["z"], "remove": []}}, "deletes": []},
        "users": {"deltas": {}, "deletes": ["session:tok"]},
    }
    await repair.apply_repairs({"links": links_store, "users": users_store}, plan)
    assert order == ["links", "users"]


async def test_apply_repairs_reads_before_writing_and_is_idempotent():
    links_store = FakeStore({"all_links": _j(["x"])})
    plan = {
        "links": {"deltas": {"all_links": {"add": ["y"], "remove": []}}, "deletes": []},
        "users": {"deltas": {}, "deletes": []},
    }
    result = await repair.apply_repairs({"links": links_store, "users": FakeStore()}, plan)
    assert result["keys_written"] == 1
    assert json.loads(await links_store.get("all_links")) == ["x", "y"]

    # A second identical application is a no-op: the delta is already applied.
    result2 = await repair.apply_repairs({"links": links_store, "users": FakeStore()}, plan)
    assert result2["keys_written"] == 0


async def test_apply_repairs_skips_a_key_that_became_unparseable_since_collection():
    links_store = FakeStore({"all_links": b"not-json-anymore"})
    plan = {
        "links": {"deltas": {"all_links": {"add": ["y"], "remove": []}}, "deletes": []},
        "users": {"deltas": {}, "deletes": []},
    }
    result = await repair.apply_repairs({"links": links_store, "users": FakeStore()}, plan)
    assert result["keys_written"] == 0
    assert result["write_skipped"] == [{"store": "links", "key": "all_links", "reason": "index_unreadable_at_write"}]


async def test_apply_repairs_never_touches_a_user_record_key():
    class RecordingStore(FakeStore):
        def __init__(self, data):
            super().__init__(data)
            self.touched: list[str] = []

        async def get(self, key):
            self.touched.append(("get", key))
            return await super().get(key)

        async def set(self, key, value):
            self.touched.append(("set", key))
            await super().set(key, value)

        async def delete(self, key):
            self.touched.append(("delete", key))
            await super().delete(key)

    users_store = RecordingStore({"user:alice": _j({"password_hash": "x"})})
    plan = {
        "links": {"deltas": {}, "deletes": []},
        "users": {"deltas": {"_meta:usernames": {"add": ["alice"], "remove": []}}, "deletes": ["session:tok"]},
    }
    await repair.apply_repairs({"links": FakeStore(), "users": users_store}, plan)
    assert not [op for op in users_store.touched if op[1].startswith("user:")]


async def test_no_gather_or_batch_deletes_anywhere_in_the_module():
    import inspect
    source = inspect.getsource(repair)
    assert "gather" not in source
    assert "delete_many" not in source
    assert "set_many" not in source
    assert "asyncio.gather" not in source


# --- handle_repair -----------------------------------------------------


async def _seeded_stores():
    links_data = {
        "slug:foo": _j({"owner": "carol"}),
        "all_links": _j([]),
        "owner_links:carol": _j([]),
    }
    users_data = {"user:carol": _j({}), "_meta:usernames": _j(["carol"])}
    return FakeStore(links_data), FakeStore(users_data)


async def test_handle_repair_forbidden_without_users_manage():
    links_store, users_store = await _seeded_stores()
    from responses import Request
    resp = await repair.handle_repair(
        {"links": links_store, "users": users_store},
        _principal(role="user", permissions=[]),
        Request(method="POST", uri="/api/admin/consistency/repair", headers={}, body=b'{"confirm":"REPAIR","checks":["unindexed_link"]}'),
        fake_list_keys, fake_get_many,
    )
    assert resp.status == 403


async def test_handle_repair_invalid_json():
    links_store, users_store = await _seeded_stores()
    from responses import Request
    resp = await repair.handle_repair(
        {"links": links_store, "users": users_store}, _principal(),
        Request(method="POST", uri="/x", headers={}, body=b"not json"),
        fake_list_keys, fake_get_many,
    )
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_json"


async def test_handle_repair_confirmation_required():
    links_store, users_store = await _seeded_stores()
    from responses import Request
    resp = await repair.handle_repair(
        {"links": links_store, "users": users_store}, _principal(),
        Request(method="POST", uri="/x", headers={}, body=b'{"checks":["unindexed_link"]}'),
        fake_list_keys, fake_get_many,
    )
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "confirmation_required"


async def test_handle_repair_no_checks():
    links_store, users_store = await _seeded_stores()
    from responses import Request
    resp = await repair.handle_repair(
        {"links": links_store, "users": users_store}, _principal(),
        Request(method="POST", uri="/x", headers={}, body=b'{"confirm":"REPAIR","checks":[]}'),
        fake_list_keys, fake_get_many,
    )
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["error"] == "no_checks"
    assert body["repairable_checks"] == list(consistency.REPAIRABLE_CHECKS)


async def test_handle_repair_duplicate_check():
    links_store, users_store = await _seeded_stores()
    from responses import Request
    resp = await repair.handle_repair(
        {"links": links_store, "users": users_store}, _principal(),
        Request(method="POST", uri="/x", headers={}, body=b'{"confirm":"REPAIR","checks":["unindexed_link","unindexed_link"]}'),
        fake_list_keys, fake_get_many,
    )
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "duplicate_check"


async def test_handle_repair_unknown_check():
    links_store, users_store = await _seeded_stores()
    from responses import Request
    resp = await repair.handle_repair(
        {"links": links_store, "users": users_store}, _principal(),
        Request(method="POST", uri="/x", headers={}, body=b'{"confirm":"REPAIR","checks":["nope"]}'),
        fake_list_keys, fake_get_many,
    )
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "unknown_check"


async def test_handle_repair_check_not_repairable():
    links_store, users_store = await _seeded_stores()
    from responses import Request
    resp = await repair.handle_repair(
        {"links": links_store, "users": users_store}, _principal(),
        Request(method="POST", uri="/x", headers={}, body=b'{"confirm":"REPAIR","checks":["owner_index_mismatch"]}'),
        fake_list_keys, fake_get_many,
    )
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "check_not_repairable"


async def test_handle_repair_success_and_idempotence():
    links_store, users_store = await _seeded_stores()
    from responses import Request

    def make_request():
        return Request(
            method="POST", uri="/x", headers={},
            body=b'{"confirm":"REPAIR","checks":["unindexed_link"]}',
        )

    resp = await repair.handle_repair(
        {"links": links_store, "users": users_store}, _principal(), make_request(), fake_list_keys, fake_get_many)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["writes"] == 1
    assert body["keys_written"] == 1
    assert body["keys_deleted"] == 0
    assert body["complete"] is True
    assert body["max_writes_per_request"] == repair.MAX_REPAIR_WRITES == 100
    assert body["checks"] == [{"check": "unindexed_link", "findings": 1, "repaired": 1, "remaining": 0, "blocked": 0, "skipped": False, "skip_reason": None}]
    assert b"password_hash" not in resp.body
    assert b"pbkdf2_sha256" not in resp.body

    # Second identical request writes nothing and still reports complete.
    resp2 = await repair.handle_repair(
        {"links": links_store, "users": users_store}, _principal(), make_request(), fake_list_keys, fake_get_many)
    body2 = json.loads(resp2.body)
    assert body2["writes"] == 0
    assert body2["complete"] is True


async def test_handle_repair_never_leaks_password_hash():
    links_store, users_store = await _seeded_stores()
    await users_store.set("user:alice", _j({
        "username": "alice",
        "password_hash": "pbkdf2_sha256$100$c2FsdA==$aGFzaA==",
        "role": "user",
        "permissions": [],
    }))
    from responses import Request
    resp = await repair.handle_repair(
        {"links": links_store, "users": users_store}, _principal(),
        Request(method="POST", uri="/x", headers={}, body=b'{"confirm":"REPAIR","checks":["unindexed_link"]}'),
        fake_list_keys, fake_get_many,
    )
    assert b"password_hash" not in resp.body
    assert b"pbkdf2_sha256" not in resp.body
