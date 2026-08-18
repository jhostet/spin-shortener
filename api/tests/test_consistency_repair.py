"""Unit tests for api/consistencyrepair.py: the pure planner (apply_list_delta,
plan_repairs), the sequential applier (apply_repairs), and the handler
(handle_repair). See docs/plans/consistency-repair.md for the original design
and docs/plans/derived-link-indexes.md for why five of the eight repairs
(everything about all_links/owner_links:<owner>) were retired along with the
checks and indexes they repaired — links.py no longer writes either index, so
there is nothing left for those repairs to fix. Only the three users-side
repairs (unindexed_user, missing_user_record, orphan_session) survive.
"""

import json

import auth
import consistency
import consistencyrepair as repair
import kvretry
from tests.fakes import FakeStore, ThrottlingStore, fake_get_many, fake_list_keys, recording_sleep


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


async def test_plan_repairs_one_write_for_many_unindexed_user_findings():
    """docs/plans/derived-link-indexes.md's Stage 2 kept this design property
    for the surviving checks: many findings against one index key share a
    single planned write, not one per finding."""
    users_data = {f"user:extra{i}": _j({}) for i in range(20)}
    users_data["_meta:usernames"] = _j([])
    collected, checks = await _collect_and_analyze(users_data=users_data)
    by_id = _by_id(checks)
    assert by_id["unindexed_user"]["count"] == 20

    plan = repair.plan_repairs(collected, checks, ["unindexed_user"], budget=100)
    assert plan["planned_writes"] == 1
    assert list(plan["users"]["deltas"].keys()) == [repair.USERNAMES_KEY]
    assert len(plan["users"]["deltas"][repair.USERNAMES_KEY]["add"]) == 20


async def test_plan_repairs_two_writes_for_unindexed_user_and_missing_user_record_together():
    users_data = {"user:extra": _j({}), "_meta:usernames": _j(["ghost1", "ghost2"])}
    collected, checks = await _collect_and_analyze(users_data=users_data)
    by_id = _by_id(checks)
    assert by_id["unindexed_user"]["count"] == 1
    assert by_id["missing_user_record"]["count"] == 2

    plan = repair.plan_repairs(collected, checks, ["unindexed_user", "missing_user_record"], budget=100)
    # Both checks share the SAME key (_meta:usernames), so this is still one
    # write, not two — the sharpest version of "many findings, one write".
    assert plan["planned_writes"] == 1
    assert list(plan["users"]["deltas"].keys()) == [repair.USERNAMES_KEY]
    assert plan["users"]["deltas"][repair.USERNAMES_KEY]["add"] == ["extra"]
    assert set(plan["users"]["deltas"][repair.USERNAMES_KEY]["remove"]) == {"ghost1", "ghost2"}


# --- skipped checks -----------------------------------------------------


async def test_skipped_check_never_planned_and_never_blocks_completion():
    collected, checks = await _collect_and_analyze(
        users_data={"_meta:usernames": b"not-json", "user:carol": _j({})},
    )
    by_id = _by_id(checks)
    assert by_id["unindexed_user"]["skipped"] is True

    plan = repair.plan_repairs(collected, checks, ["unindexed_user", "missing_user_record"], budget=100)
    report = _by_id(plan["checks"])
    assert report["unindexed_user"]["skipped"] is True
    assert report["unindexed_user"]["skip_reason"] == "index_unreadable"
    assert report["unindexed_user"]["remaining"] == 0
    assert report["unindexed_user"]["planned"] == 0
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
    users_data = {
        "session:tok1": _j({"username": "ghost1"}),
        "session:tok2": _j({"username": "ghost2"}),
        "_meta:usernames": _j([]),
    }
    collected, checks = await _collect_and_analyze(users_data=users_data)
    by_id = _by_id(checks)
    assert by_id["orphan_session"]["count"] == 2

    plan = repair.plan_repairs(collected, checks, ["orphan_session"], budget=1)
    assert plan["planned_writes"] == 1
    assert len(plan["users"]["deletes"]) == 1
    report = _by_id(plan["checks"])["orphan_session"]
    assert report["planned"] == 1
    assert report["remaining"] == 1
    assert report["blocked"] == 0


async def test_two_runs_over_identical_input_produce_byte_identical_plans():
    users_data = {"user:extra": _j({}), "_meta:usernames": _j(["ghost1", "ghost2"])}
    collected, checks = await _collect_and_analyze(users_data=users_data)
    requested = ["unindexed_user", "missing_user_record"]
    plan1 = repair.plan_repairs(collected, checks, requested, budget=100)
    plan2 = repair.plan_repairs(collected, checks, requested, budget=100)
    assert json.dumps(plan1, sort_keys=True) == json.dumps(plan2, sort_keys=True)


# --- module hygiene ---------------------------------------------------------


def test_no_spin_sdk_import():
    import inspect
    source = inspect.getsource(repair)
    assert "spin_sdk" not in source


# --- apply_repairs -----------------------------------------------------


async def test_apply_repairs_only_touches_the_users_store():
    """docs/plans/derived-link-indexes.md, Stage 2: apply_repairs no longer
    reads or writes the links store at all — passing one that would raise on
    any access confirms it."""
    class RaisingStore(FakeStore):
        async def get(self, key):
            raise AssertionError(f"links store should never be read, got {key}")

        async def set(self, key, value):
            raise AssertionError(f"links store should never be written, got {key}")

        async def delete(self, key):
            raise AssertionError(f"links store should never be deleted from, got {key}")

    links_store = RaisingStore()
    users_store = FakeStore({"_meta:usernames": _j(["y"])})
    plan = {
        "users": {"deltas": {"_meta:usernames": {"add": ["z"], "remove": []}}, "deletes": []},
    }
    result = await repair.apply_repairs({"links": links_store, "users": users_store}, plan, kvretry.direct)
    assert result["keys_written"] == 1


async def test_apply_repairs_reads_before_writing_and_is_idempotent():
    users_store = FakeStore({"_meta:usernames": _j(["x"])})
    plan = {
        "users": {"deltas": {"_meta:usernames": {"add": ["y"], "remove": []}}, "deletes": []},
    }
    result = await repair.apply_repairs({"links": FakeStore(), "users": users_store}, plan, kvretry.direct)
    assert result["keys_written"] == 1
    assert json.loads(await users_store.get("_meta:usernames")) == ["x", "y"]

    # A second identical application is a no-op: the delta is already applied.
    result2 = await repair.apply_repairs({"links": FakeStore(), "users": users_store}, plan, kvretry.direct)
    assert result2["keys_written"] == 0


async def test_apply_repairs_skips_a_key_that_became_unparseable_since_collection():
    users_store = FakeStore({"_meta:usernames": b"not-json-anymore"})
    plan = {
        "users": {"deltas": {"_meta:usernames": {"add": ["y"], "remove": []}}, "deletes": []},
    }
    result = await repair.apply_repairs({"links": FakeStore(), "users": users_store}, plan, kvretry.direct)
    assert result["keys_written"] == 0
    assert result["write_skipped"] == [{"store": "users", "key": "_meta:usernames", "reason": "index_unreadable_at_write"}]


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
        "users": {"deltas": {"_meta:usernames": {"add": ["alice"], "remove": []}}, "deletes": ["session:tok"]},
    }
    await repair.apply_repairs({"links": FakeStore(), "users": users_store}, plan, kvretry.direct)
    assert not [op for op in users_store.touched if op[1].startswith("user:")]


async def test_apply_repairs_stops_on_write_failed_and_reports_the_key():
    """docs/plans/write-throttle-resilience.md: a repair against a
    throttled store must STOP rather than continue to the next key, and
    report exactly which key failed."""
    users_store = ThrottlingStore({"_meta:usernames": _j(["x"])}, fail_times={"_meta:usernames": 10})
    sleep, _ = recording_sleep()
    write = kvretry.make_writer(sleep)
    plan = {
        "users": {
            "deltas": {"_meta:usernames": {"add": ["y"], "remove": []}},
            "deletes": ["session:should-never-be-attempted"],
        },
    }
    result = await repair.apply_repairs({"links": FakeStore(), "users": users_store}, plan, write)
    assert result["keys_written"] == 0
    assert result["write_failed"] == [{"store": "users", "key": "_meta:usernames", "reason": "write_failed"}]
    # The delete must never even have been attempted — the delta failure
    # stops the applier before it reaches the deletes loop.
    assert await users_store.exists("session:should-never-be-attempted") is False


async def test_apply_repairs_write_failed_defaults_to_empty_list_on_success():
    users_store = FakeStore({"_meta:usernames": _j(["x"])})
    sleep, _ = recording_sleep()
    write = kvretry.make_writer(sleep)
    plan = {
        "users": {"deltas": {"_meta:usernames": {"add": ["y"], "remove": []}}, "deletes": []},
    }
    result = await repair.apply_repairs({"links": FakeStore(), "users": users_store}, plan, write)
    assert result["write_failed"] == []


async def test_handle_repair_against_throttled_store_reports_failed_key_and_complete_false():
    links_store = FakeStore({"slug:foo": _j({"owner": "carol"})})
    users_store = ThrottlingStore(
        {"user:extra": _j({}), "_meta:usernames": _j([])},
        fail_times={"_meta:usernames": 10},
    )
    sleep, _ = recording_sleep()
    write = kvretry.make_writer(sleep)
    from responses import Request

    resp = await repair.handle_repair(
        {"links": links_store, "users": users_store}, _principal(),
        Request(method="POST", uri="/x", headers={}, body=b'{"confirm":"REPAIR","checks":["unindexed_user"]}'),
        fake_list_keys, fake_get_many, write)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["complete"] is False
    assert body["write_failed"] == [{"store": "users", "key": "_meta:usernames", "reason": "write_failed"}]


async def test_no_gather_or_batch_deletes_anywhere_in_the_module():
    import inspect
    source = inspect.getsource(repair)
    assert "gather" not in source
    assert "delete_many" not in source
    assert "set_many" not in source
    assert "asyncio.gather" not in source


# --- handle_repair -----------------------------------------------------


async def _seeded_stores():
    links_data = {"slug:foo": _j({"owner": "carol"})}
    users_data = {"user:extra": _j({}), "_meta:usernames": _j([])}
    return FakeStore(links_data), FakeStore(users_data)


async def test_handle_repair_forbidden_without_users_manage():
    links_store, users_store = await _seeded_stores()
    from responses import Request
    resp = await repair.handle_repair(
        {"links": links_store, "users": users_store},
        _principal(role="user", permissions=[]),
        Request(method="POST", uri="/api/admin/consistency/repair", headers={}, body=b'{"confirm":"REPAIR","checks":["unindexed_user"]}'),
        fake_list_keys, fake_get_many, kvretry.direct)
    assert resp.status == 403


async def test_handle_repair_invalid_json():
    links_store, users_store = await _seeded_stores()
    from responses import Request
    resp = await repair.handle_repair(
        {"links": links_store, "users": users_store}, _principal(),
        Request(method="POST", uri="/x", headers={}, body=b"not json"),
        fake_list_keys, fake_get_many, kvretry.direct)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_json"


async def test_handle_repair_confirmation_required():
    links_store, users_store = await _seeded_stores()
    from responses import Request
    resp = await repair.handle_repair(
        {"links": links_store, "users": users_store}, _principal(),
        Request(method="POST", uri="/x", headers={}, body=b'{"checks":["unindexed_user"]}'),
        fake_list_keys, fake_get_many, kvretry.direct)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "confirmation_required"


async def test_handle_repair_no_checks():
    links_store, users_store = await _seeded_stores()
    from responses import Request
    resp = await repair.handle_repair(
        {"links": links_store, "users": users_store}, _principal(),
        Request(method="POST", uri="/x", headers={}, body=b'{"confirm":"REPAIR","checks":[]}'),
        fake_list_keys, fake_get_many, kvretry.direct)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["error"] == "no_checks"
    assert body["repairable_checks"] == list(consistency.REPAIRABLE_CHECKS)


async def test_handle_repair_duplicate_check():
    links_store, users_store = await _seeded_stores()
    from responses import Request
    resp = await repair.handle_repair(
        {"links": links_store, "users": users_store}, _principal(),
        Request(method="POST", uri="/x", headers={}, body=b'{"confirm":"REPAIR","checks":["unindexed_user","unindexed_user"]}'),
        fake_list_keys, fake_get_many, kvretry.direct)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "duplicate_check"


async def test_handle_repair_unknown_check():
    links_store, users_store = await _seeded_stores()
    from responses import Request
    resp = await repair.handle_repair(
        {"links": links_store, "users": users_store}, _principal(),
        Request(method="POST", uri="/x", headers={}, body=b'{"confirm":"REPAIR","checks":["nope"]}'),
        fake_list_keys, fake_get_many, kvretry.direct)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "unknown_check"


async def test_handle_repair_check_not_repairable():
    links_store, users_store = await _seeded_stores()
    from responses import Request
    resp = await repair.handle_repair(
        {"links": links_store, "users": users_store}, _principal(),
        Request(method="POST", uri="/x", headers={}, body=b'{"confirm":"REPAIR","checks":["unknown_link_owner"]}'),
        fake_list_keys, fake_get_many, kvretry.direct)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "check_not_repairable"


async def test_handle_repair_success_and_idempotence():
    links_store, users_store = await _seeded_stores()
    from responses import Request

    def make_request():
        return Request(
            method="POST", uri="/x", headers={},
            body=b'{"confirm":"REPAIR","checks":["unindexed_user"]}',
        )

    resp = await repair.handle_repair(
        {"links": links_store, "users": users_store}, _principal(), make_request(), fake_list_keys, fake_get_many, kvretry.direct)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["writes"] == 1
    assert body["keys_written"] == 1
    assert body["keys_deleted"] == 0
    assert body["complete"] is True
    assert body["max_writes_per_request"] == repair.MAX_REPAIR_WRITES == 100
    assert body["checks"] == [{"check": "unindexed_user", "findings": 1, "repaired": 1, "remaining": 0, "blocked": 0, "skipped": False, "skip_reason": None}]
    assert b"password_hash" not in resp.body
    assert b"pbkdf2_sha256" not in resp.body

    # Second identical request writes nothing and still reports complete.
    resp2 = await repair.handle_repair(
        {"links": links_store, "users": users_store}, _principal(), make_request(), fake_list_keys, fake_get_many, kvretry.direct)
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
        Request(method="POST", uri="/x", headers={}, body=b'{"confirm":"REPAIR","checks":["unindexed_user"]}'),
        fake_list_keys, fake_get_many, kvretry.direct)
    assert b"password_hash" not in resp.body
    assert b"pbkdf2_sha256" not in resp.body
