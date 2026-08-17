"""api/analyticsorphans.py — the pure classification/plan layer and both
handlers. See docs/plans/analytics-orphan-purge.md.
"""

import json

import analyticsorphans as orphans_mod
import auth
import kvretry
from responses import Request
from tests.fakes import FakeStore, ThrottlingStore, fake_list_keys, recording_sleep


def _principal(username="admin", role="admin", permissions=None):
    if permissions is None:
        permissions = ["users.manage"]
    return auth.Principal(username=username, role=role, permissions=permissions, csrf_token="x")


def _count(total=1):
    return json.dumps({"total": total, "days": {}}).encode("utf-8")


def _event():
    return b"1700000000000|(direct)|desktop"


class RecordingStore(FakeStore):
    """Counts every get/set/delete/exists call, so the "exactly N KV
    operations" claims are testable rather than aspirational — same pattern
    as test_click_totals.py's RecordingStore."""

    def __init__(self, data=None):
        super().__init__(data)
        self.ops: list[tuple[str, str]] = []

    async def get(self, key):
        self.ops.append(("get", key))
        return await super().get(key)

    async def set(self, key, value):
        self.ops.append(("set", key))
        await super().set(key, value)

    async def delete(self, key):
        self.ops.append(("delete", key))
        await super().delete(key)

    async def exists(self, key):
        self.ops.append(("exists", key))
        return await super().exists(key)


def _purge_request(payload):
    return Request(
        method="POST", uri="/api/admin/analytics/purge", headers={},
        body=json.dumps(payload).encode("utf-8"),
    )


# --- classify_analytics_keys ------------------------------------------------


def test_classify_picks_up_the_legacy_unsharded_count_key():
    by_slug, unrecognized = orphans_mod.classify_analytics_keys(["count:promo", "count:promo:3"])
    assert unrecognized == []
    assert by_slug["promo"]["count_keys"] == 2
    assert set(by_slug["promo"]["keys"]) == {"count:promo", "count:promo:3"}


def test_classify_sends_unrecognized_shape_to_unrecognized():
    by_slug, unrecognized = orphans_mod.classify_analytics_keys(["totals:weird", "count:promo:1"])
    assert unrecognized == ["totals:weird"]
    assert list(by_slug.keys()) == ["promo"]


def test_classify_sends_invalid_slug_shape_to_unrecognized():
    """A slug that fails is_valid_custom_slug (too short, or containing a
    character outside the allowed set) must never become a purge target —
    the safety valve the module docstring describes."""
    by_slug, unrecognized = orphans_mod.classify_analytics_keys(["count:a:1", "count:has a space:1"])
    assert by_slug == {}
    assert set(unrecognized) == {"count:a:1", "count:has a space:1"}


def test_classify_counts_event_keys_separately():
    by_slug, _ = orphans_mod.classify_analytics_keys(["events:promo:7", "count:promo:1"])
    assert by_slug["promo"]["event_keys"] == 1
    assert by_slug["promo"]["count_keys"] == 1


# --- split_by_liveness -------------------------------------------------------


def test_split_by_liveness():
    by_slug = {
        "orphan1": {"keys": ["count:orphan1:1"], "count_keys": 1, "event_keys": 0},
        "live1": {"keys": ["count:live1:1"], "count_keys": 1, "event_keys": 0},
    }
    orphans, live = orphans_mod.split_by_liveness(by_slug, {"live1"})
    assert list(orphans.keys()) == ["orphan1"]
    assert list(live.keys()) == ["live1"]


# --- plan_purge ---------------------------------------------------------------


def test_plan_purge_is_whole_slug_and_biggest_first():
    orphans = {
        "small": {"keys": ["count:small:1"], "count_keys": 1, "event_keys": 0},
        "big": {"keys": [f"count:big:{i}" for i in range(5)], "count_keys": 5, "event_keys": 0},
    }
    to_purge, keys, remaining = orphans_mod.plan_purge(orphans, ["small", "big"], budget=100)
    assert to_purge == ["big", "small"]
    assert len(keys) == 6
    assert remaining == []


def test_plan_purge_always_plans_at_least_one_slug_even_over_budget():
    """Without this invariant a slug whose own key count exceeds the budget
    (possible if analytics_event_slots was once set very high) would make
    every request purge nothing, and the GUI's re-POST loop would never
    terminate."""
    orphans = {
        "huge": {"keys": [f"count:huge:{i}" for i in range(10)], "count_keys": 10, "event_keys": 0},
    }
    to_purge, keys, remaining = orphans_mod.plan_purge(orphans, ["huge"], budget=1)
    assert to_purge == ["huge"]
    assert len(keys) == 10
    assert remaining == []


def test_plan_purge_respects_budget_across_multiple_slugs():
    orphans = {
        "aaa": {"keys": ["count:aaa:1", "count:aaa:2"], "count_keys": 2, "event_keys": 0},
        "bbb": {"keys": ["count:bbb:1", "count:bbb:2"], "count_keys": 2, "event_keys": 0},
    }
    to_purge, keys, remaining = orphans_mod.plan_purge(orphans, ["aaa", "bbb"], budget=2)
    assert to_purge == ["aaa"]
    assert len(keys) == 2
    assert remaining == ["bbb"]


def test_plan_purge_is_deterministic_across_two_runs():
    orphans = {
        "z": {"keys": ["count:z:1"], "count_keys": 1, "event_keys": 0},
        "a": {"keys": ["count:a:1"], "count_keys": 1, "event_keys": 0},
    }
    first = orphans_mod.plan_purge(orphans, ["z", "a"], budget=100)
    second = orphans_mod.plan_purge(orphans, ["z", "a"], budget=100)
    assert first == second
    # Same key count -> tie-broken alphabetically.
    assert first[0] == ["a", "z"]


# --- GET /api/admin/analytics/orphans ----------------------------------------


async def test_handle_orphan_report_requires_users_manage():
    links_store = FakeStore()
    analytics_store = FakeStore()
    resp = await orphans_mod.handle_orphan_report(
        links_store, analytics_store, _principal(role="user", permissions=[]), fake_list_keys,
    )
    assert resp.status == 403
    assert json.loads(resp.body) == {"error": "forbidden", "required_permission": "users.manage"}


async def test_handle_orphan_report_makes_exactly_two_kv_operations():
    links_store = RecordingStore({"all_links": json.dumps(["keepme"]).encode("utf-8")})
    analytics_store = RecordingStore({
        "count:keepme:1": _count(3),
        "count:killme:1": _count(9),
        "count:killme:2": _count(1),
    })

    resp = await orphans_mod.handle_orphan_report(
        links_store, analytics_store, _principal(), fake_list_keys,
    )
    assert resp.status == 200
    # One get on the links store (all_links). fake_list_keys reads the store's
    # keys directly rather than issuing a "get", matching the real list_keys
    # callable's shape, so the one enumeration doesn't show up as a `get`.
    assert links_store.ops == [("get", "all_links")]
    assert analytics_store.ops == []


async def test_handle_orphan_report_names_orphans_and_excludes_live_slugs():
    links_store = FakeStore({"all_links": json.dumps(["keepme"]).encode("utf-8")})
    analytics_store = FakeStore({
        "count:keepme:1": _count(3),
        "count:killme:1": _count(9),
        "count:killme:2": _count(1),
        "events:killme:5": _event(),
    })

    resp = await orphans_mod.handle_orphan_report(
        links_store, analytics_store, _principal(), fake_list_keys,
    )
    body = json.loads(resp.body)
    assert body["totals"]["orphan_slugs"] == 1
    assert body["totals"]["orphan_keys"] == 3
    assert body["totals"]["live_keys"] == 1
    assert body["scanned"]["analytics_keys"] == 4
    assert body["scanned"]["live_slugs"] == 1
    slugs_reported = [o["slug"] for o in body["orphans"]]
    assert slugs_reported == ["killme"]
    assert "keepme" not in slugs_reported


async def test_handle_orphan_report_fails_closed_on_unreadable_links_index():
    links_store = FakeStore({"all_links": b"not json"})
    analytics_store = FakeStore()
    resp = await orphans_mod.handle_orphan_report(
        links_store, analytics_store, _principal(), fake_list_keys,
    )
    assert resp.status == 409
    assert json.loads(resp.body)["error"] == "links_index_unreadable"


async def test_handle_orphan_report_fails_closed_when_index_is_not_a_list_of_strings():
    """A value that parses as JSON but isn't a list of strings (e.g. an
    object) must not silently succeed with wrong "live" membership."""
    links_store = FakeStore({"all_links": json.dumps({"not": "a list"}).encode("utf-8")})
    analytics_store = FakeStore()
    resp = await orphans_mod.handle_orphan_report(
        links_store, analytics_store, _principal(), fake_list_keys,
    )
    assert resp.status == 409


async def test_handle_orphan_report_truncates_at_max_and_totals_stay_exact():
    slugs = [f"slug{i:04d}" for i in range(orphans_mod.MAX_ORPHAN_SLUGS_REPORTED + 5)]
    links_store = FakeStore({"all_links": b"[]"})
    analytics_store = FakeStore({f"count:{s}:1": _count(1) for s in slugs})

    resp = await orphans_mod.handle_orphan_report(
        links_store, analytics_store, _principal(), fake_list_keys,
    )
    body = json.loads(resp.body)
    assert body["totals"]["orphan_slugs"] == len(slugs)
    assert body["truncated"] is True
    assert len(body["orphans"]) == orphans_mod.MAX_ORPHAN_SLUGS_REPORTED


# --- POST /api/admin/analytics/purge -----------------------------------------


async def test_handle_orphan_purge_requires_users_manage():
    resp = await orphans_mod.handle_orphan_purge(
        FakeStore(), FakeStore(), _principal(role="user", permissions=[]),
        _purge_request({"confirm": "PURGE", "slugs": ["killme"]}), fake_list_keys, kvretry.direct)
    assert resp.status == 403


async def test_handle_orphan_purge_requires_confirmation():
    links_store = FakeStore()
    analytics_store = FakeStore({"count:killme:1": _count(1)})
    resp = await orphans_mod.handle_orphan_purge(
        links_store, analytics_store, _principal(),
        _purge_request({"slugs": ["killme"]}), fake_list_keys, kvretry.direct)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "confirmation_required"
    assert await analytics_store.exists("count:killme:1") is True


async def test_handle_orphan_purge_rejects_no_slugs():
    for payload in ({"confirm": "PURGE"}, {"confirm": "PURGE", "slugs": []}, {"confirm": "PURGE", "slugs": [1]}):
        resp = await orphans_mod.handle_orphan_purge(
            FakeStore(), FakeStore(), _principal(), _purge_request(payload), fake_list_keys, kvretry.direct)
        assert resp.status == 400
        assert json.loads(resp.body)["error"] == "no_slugs"


async def test_handle_orphan_purge_rejects_duplicate_slug():
    resp = await orphans_mod.handle_orphan_purge(
        FakeStore(), FakeStore(), _principal(),
        _purge_request({"confirm": "PURGE", "slugs": ["a", "a"]}), fake_list_keys, kvretry.direct)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "duplicate_slug"


async def test_handle_orphan_purge_rejects_too_many_slugs():
    slugs = [f"slug{i:04d}" for i in range(orphans_mod.MAX_PURGE_SLUGS + 1)]
    resp = await orphans_mod.handle_orphan_purge(
        FakeStore(), FakeStore(), _principal(),
        _purge_request({"confirm": "PURGE", "slugs": slugs}), fake_list_keys, kvretry.direct)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["error"] == "too_many_slugs"
    assert body["max_slugs"] == orphans_mod.MAX_PURGE_SLUGS
    assert body["slug_count"] == len(slugs)


async def test_handle_orphan_purge_rejects_invalid_slug():
    resp = await orphans_mod.handle_orphan_purge(
        FakeStore(), FakeStore(), _principal(),
        _purge_request({"confirm": "PURGE", "slugs": ["a:b"]}), fake_list_keys, kvretry.direct)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["error"] == "invalid_slug"
    assert body["slug"] == "a:b"


async def test_handle_orphan_purge_skips_a_slug_whose_link_record_exists():
    """The load-bearing re-check: even if the caller (or a stale report)
    names a live slug, the purge must never delete its analytics."""
    links_store = FakeStore({"slug:keepme": b'{"slug": "keepme"}'})
    analytics_store = FakeStore({
        "count:keepme:1": _count(3),
        "count:killme:1": _count(9),
    })

    resp = await orphans_mod.handle_orphan_purge(
        links_store, analytics_store, _principal(),
        _purge_request({"confirm": "PURGE", "slugs": ["keepme", "killme"]}), fake_list_keys, kvretry.direct)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["purged_slugs"] == ["killme"]
    assert {"slug": "keepme", "reason": "link_exists"} in body["skipped"]
    assert await analytics_store.exists("count:keepme:1") is True
    assert await analytics_store.exists("count:killme:1") is False


async def test_handle_orphan_purge_reports_no_analytics_keys_for_an_already_clean_slug():
    resp = await orphans_mod.handle_orphan_purge(
        FakeStore(), FakeStore(), _principal(),
        _purge_request({"confirm": "PURGE", "slugs": ["neverclicked"]}), fake_list_keys, kvretry.direct)
    body = json.loads(resp.body)
    assert body["ok"] is True
    assert body["purged_slugs"] == []
    assert body["skipped"] == [{"slug": "neverclicked", "reason": "no_analytics_keys"}]


async def test_handle_orphan_purge_bounds_deletes_by_budget_and_reports_remaining():
    orig_budget = orphans_mod.MAX_PURGE_KEYS_PER_REQUEST
    try:
        orphans_mod.MAX_PURGE_KEYS_PER_REQUEST = 2
        analytics_store = FakeStore({
            "count:aaa:1": _count(1), "count:aaa:2": _count(1),
            "count:bbb:1": _count(1), "count:bbb:2": _count(1),
        })
        resp = await orphans_mod.handle_orphan_purge(
            FakeStore(), analytics_store, _principal(),
            _purge_request({"confirm": "PURGE", "slugs": ["aaa", "bbb"]}), fake_list_keys, kvretry.direct)
        body = json.loads(resp.body)
        assert body["complete"] is False
        assert body["remaining_slugs"] == ["bbb"]
        assert body["deleted_keys"] == 2
        assert body["max_keys_per_request"] == 2
    finally:
        orphans_mod.MAX_PURGE_KEYS_PER_REQUEST = orig_budget


async def test_purging_the_same_slugs_twice_is_a_no_op_the_second_time():
    analytics_store = FakeStore({"count:killme:1": _count(9), "count:killme:2": _count(1)})
    first = await orphans_mod.handle_orphan_purge(
        FakeStore(), analytics_store, _principal(),
        _purge_request({"confirm": "PURGE", "slugs": ["killme"]}), fake_list_keys, kvretry.direct)
    assert json.loads(first.body)["purged_slugs"] == ["killme"]

    second = await orphans_mod.handle_orphan_purge(
        FakeStore(), analytics_store, _principal(),
        _purge_request({"confirm": "PURGE", "slugs": ["killme"]}), fake_list_keys, kvretry.direct)
    body2 = json.loads(second.body)
    assert body2["purged_slugs"] == []
    assert body2["deleted_keys"] == 0
    assert body2["skipped"] == [{"slug": "killme", "reason": "no_analytics_keys"}]


async def test_handle_orphan_purge_deletes_sequentially_never_gathered():
    """Pins the module's central safety rule: deletes must never be issued
    through gather_reads/asyncio.gather. A RecordingStore that raises if two
    deletes are ever in flight concurrently would be ideal, but there is no
    concurrency signal available from a synchronous FakeStore; instead this
    pins the observable proxy — the delete order matches keys_to_delete's
    order exactly, which asyncio.gather over a bounded semaphore would not
    guarantee for a real (non-instant) backend."""
    analytics_store = RecordingStore({f"count:killme:{i}": _count(1) for i in range(5)})
    resp = await orphans_mod.handle_orphan_purge(
        FakeStore(), analytics_store, _principal(),
        _purge_request({"confirm": "PURGE", "slugs": ["killme"]}), fake_list_keys, kvretry.direct)
    body = json.loads(resp.body)
    assert body["deleted_keys"] == 5
    delete_ops = [key for kind, key in analytics_store.ops if kind == "delete"]
    assert sorted(delete_ops) == sorted(f"count:killme:{i}" for i in range(5))


async def test_handle_orphan_purge_throttled_delete_puts_slug_back_in_remaining():
    """docs/plans/write-throttle-resilience.md: a throttled delete must stop
    (never keep hammering the store), report write_failed, and put the
    failing slug — plus anything not yet attempted — back into
    remaining_slugs so the GUI's existing chunk loop picks it up."""
    analytics_store = ThrottlingStore(
        {"count:aaa:1": _count(1), "count:bbb:1": _count(1)},
        fail_times={"count:bbb:1": 10},
    )
    sleep, _ = recording_sleep()
    write = kvretry.make_writer(sleep)
    resp = await orphans_mod.handle_orphan_purge(
        FakeStore(), analytics_store, _principal(),
        _purge_request({"confirm": "PURGE", "slugs": ["aaa", "bbb"]}), fake_list_keys, write)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["purged_slugs"] == ["aaa"]
    assert body["deleted_keys"] == 1
    assert body["remaining_slugs"] == ["bbb"]
    assert body["write_failed"] is True
    assert body["complete"] is False
    # The successfully-purged key really is gone; the throttled one is not.
    assert await analytics_store.exists("count:aaa:1") is False
    assert await analytics_store.exists("count:bbb:1") is True


async def test_handle_orphan_purge_no_write_failure_reports_write_failed_false():
    analytics_store = FakeStore({"count:killme:1": _count(1)})
    sleep, _ = recording_sleep()
    write = kvretry.make_writer(sleep)
    resp = await orphans_mod.handle_orphan_purge(
        FakeStore(), analytics_store, _principal(),
        _purge_request({"confirm": "PURGE", "slugs": ["killme"]}), fake_list_keys, write)
    body = json.loads(resp.body)
    assert body["write_failed"] is False
    assert body["complete"] is True


# --- purge_slug_analytics ---------------------------------------------------
#
# docs/plans/inline-analytics-purge-on-delete.md. THE CALLER MUST HAVE
# ESTABLISHED slug HAS NO LINK RECORD before calling this — these tests never
# assert a liveness check because purge_slug_analytics performs none.


async def test_purge_slug_analytics_deletes_only_the_named_slugs_keys():
    analytics_store = RecordingStore({
        "count:killme:0": _count(2),
        "count:killme:3": _count(1),
        "events:killme:5": _event(),
        "count:keepme:0": _count(9),
        "events:keepme:1": _event(),
    })
    result = await orphans_mod.purge_slug_analytics(analytics_store, "killme", fake_list_keys)
    assert result == {"status": "complete", "found_keys": 3, "deleted_keys": 3}
    assert sorted(analytics_store.keys()) == ["count:keepme:0", "events:keepme:1"]


async def test_purge_slug_analytics_deletes_sequentially_never_gathered():
    analytics_store = RecordingStore({f"count:killme:{i}": _count(1) for i in range(6)})
    result = await orphans_mod.purge_slug_analytics(analytics_store, "killme", fake_list_keys)
    assert result["deleted_keys"] == 6
    delete_ops = [key for kind, key in analytics_store.ops if kind == "delete"]
    # Sequential means every delete completed (recorded) one at a time in the
    # exact order `found` iterated them — a RecordingStore can't observe true
    # concurrency, but an out-of-order or partial-then-resumed sequence would
    # falsify this, matching the existing handle_orphan_purge test's approach.
    assert sorted(delete_ops) == sorted(f"count:killme:{i}" for i in range(6))
    assert len(delete_ops) == 6


async def test_purge_slug_analytics_never_touches_the_list_keys_store_via_gather():
    """The enumeration is the only read; gather_reads must never appear in
    the delete loop. Verified indirectly: RecordingStore.ops shows a single
    contiguous run of delete ops with no interleaved reads once the loop
    starts (get is only used inside list_keys's FakeStore.keys(), which
    records no ops at all)."""
    analytics_store = RecordingStore({f"count:killme:{i}": _count(1) for i in range(4)})
    await orphans_mod.purge_slug_analytics(analytics_store, "killme", fake_list_keys)
    kinds = [kind for kind, _ in analytics_store.ops]
    assert kinds == ["delete"] * 4


async def test_purge_slug_analytics_on_a_never_clicked_slug_is_complete_with_zero_keys():
    analytics_store = RecordingStore({"count:other:0": _count(1)})
    result = await orphans_mod.purge_slug_analytics(analytics_store, "neverclicked", fake_list_keys)
    assert result == {"status": "complete", "found_keys": 0, "deleted_keys": 0}
    assert analytics_store.keys() == ["count:other:0"]


async def test_purge_slug_analytics_defers_when_found_keys_exceeds_max_and_deletes_nothing():
    analytics_store = RecordingStore({f"count:killme:{i}": _count(1) for i in range(5)})
    result = await orphans_mod.purge_slug_analytics(
        analytics_store, "killme", fake_list_keys, max_keys=4
    )
    assert result == {
        "status": "deferred", "found_keys": 5, "deleted_keys": 0, "max_inline_keys": 4,
    }
    assert len(analytics_store.keys()) == 5
    delete_ops = [key for kind, key in analytics_store.ops if kind == "delete"]
    assert delete_ops == []


class _RaisingOnKeyStore(RecordingStore):
    """Raises when deleting a specific key, to simulate a KV failure partway
    through the sequential loop."""

    def __init__(self, data, raise_on_key):
        super().__init__(data)
        self._raise_on_key = raise_on_key

    async def delete(self, key):
        if key == self._raise_on_key:
            self.ops.append(("delete", key))
            raise RuntimeError("simulated KV failure")
        await super().delete(key)


async def test_purge_slug_analytics_returns_failed_with_partial_count_on_mid_loop_exception():
    keys = {f"count:killme:{i}": _count(1) for i in range(5)}
    analytics_store = _RaisingOnKeyStore(keys, raise_on_key="count:killme:3")
    result = await orphans_mod.purge_slug_analytics(analytics_store, "killme", fake_list_keys)
    assert result["status"] == "failed"
    assert result["found_keys"] == 5
    assert result["deleted_keys"] < 5
    # Never raises out of the function.


async def test_max_inline_purge_keys_cannot_fire_at_shipped_configuration():
    """docs/plans/inline-analytics-purge-on-delete.md Trade-offs #4: the rail
    must sit above the shipped-configuration ceiling (64 count shards + 1
    legacy unsharded key + 30 event slots = 95), or a future raise of
    CountShards (RAISE ONLY, never lower — see CLAUDE.md) could silently
    start deferring every single-link delete's inline purge."""
    import analytics
    assert orphans_mod.MAX_INLINE_PURGE_KEYS >= analytics.COUNT_SHARDS + 1 + 30, (
        "MAX_INLINE_PURGE_KEYS must stay above analytics.COUNT_SHARDS + 1 + 30 "
        "(the shipped-configuration ceiling) — CountShards is raise-only, "
        "never lowered, so a future raise must not silently start deferring "
        "every single-link delete's inline analytics purge"
    )
