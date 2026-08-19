import re
from pathlib import Path

import pytest

import analytics
import kvprefix
from tests.fakes import FakeStore


# --- STORE_PREFIXES invariant ---


def test_no_prefix_is_a_prefix_of_another():
    names = list(kvprefix.STORE_PREFIXES)
    for a in names:
        for b in names:
            if a == b:
                continue
            pa, pb = kvprefix.STORE_PREFIXES[a], kvprefix.STORE_PREFIXES[b]
            assert not pa.startswith(pb), f"{a!r} prefix {pa!r} starts with {b!r} prefix {pb!r}"


# --- PrefixedStore round trip ---


async def test_prefixed_store_round_trips_get_set_delete_exists():
    physical = FakeStore()
    view = kvprefix.PrefixedStore(physical, "links:")

    assert await view.exists("slug:a") is False
    await view.set("slug:a", b"value")
    assert await view.exists("slug:a") is True
    assert await view.get("slug:a") == b"value"
    assert physical.keys() == ["links:slug:a"]

    await view.delete("slug:a")
    assert await view.exists("slug:a") is False
    assert physical.keys() == []


def test_prefixed_store_has_no_get_keys():
    view = kvprefix.PrefixedStore(FakeStore(), "links:")
    assert not hasattr(view, "get_keys")


# --- Mutual invisibility between views over one physical store ---


async def test_two_views_over_one_store_cannot_see_each_others_keys():
    physical = FakeStore({"links:user:alice": b"link-shaped-value"})
    views = kvprefix.open_views(physical)

    assert await views["users"].get("user:alice") is None
    assert await views["links"].get("user:alice") == b"link-shaped-value"

    await views["users"].set("user:alice", b"user-shaped-value")
    assert await views["links"].get("user:alice") == b"link-shaped-value"
    assert await views["users"].get("user:alice") == b"user-shaped-value"


# --- memoized_raw_list_keys, and the op it must NOT record ---


class _CountingCollector:
    """Minimal obs.Collector stand-in: records only what it was asked to."""

    def __init__(self):
        self.ops: list[str] = []

    def record(self, op, namespace, duration_ns, num_bytes, num_keys=1):
        self.ops.append(op)


async def test_memoized_raw_list_keys_performs_one_real_enumeration():
    calls = []

    async def raw(store):
        calls.append(store)
        return ["links:slug:a"]

    memoized = kvprefix.memoized_raw_list_keys(raw)
    store = FakeStore()
    assert await memoized(store) == ["links:slug:a"]
    assert await memoized(store) == ["links:slug:a"]
    assert await memoized(store) == ["links:slug:a"]
    assert len(calls) == 1


async def test_memoized_cache_hit_records_no_kv_operation():
    """The bug this fixes, measured on the deployed build 2026-08-18:
    handle_click_totals traced as `list_keys=2` while performing exactly ONE
    whole-store walk, because the memoized hit was still recorded. kv_ops
    counts HOST operations, and a cache hit never reaches the host."""
    async def raw(store):
        return ["links:slug:a", "analytics:count:a:1"]

    collector = _CountingCollector()
    memoized = kvprefix.memoized_raw_list_keys(raw)
    list_keys = kvprefix.scoped_list_keys(memoized, collector)

    views = kvprefix.open_views(FakeStore())
    await list_keys(views["links"])
    await list_keys(views["analytics"])

    assert collector.ops.count("list_keys") == 1


async def test_a_plain_raw_callable_still_records_every_call():
    """The getattr default is what keeps every non-memoized call site
    unchanged — without it this fix would silently stop recording real walks."""
    async def raw(store):
        return ["links:slug:a"]

    collector = _CountingCollector()
    list_keys = kvprefix.scoped_list_keys(raw, collector)
    views = kvprefix.open_views(FakeStore())
    await list_keys(views["links"])
    await list_keys(views["links"])

    assert collector.ops.count("list_keys") == 2


# --- scoped_list_keys ---


async def test_scoped_list_keys_filters_and_strips_prefix():
    physical = FakeStore({
        "links:slug:a": b"1",
        "users:user:bob": b"2",
        "analytics:count:a": b"3",
        "orphan-key": b"4",
    })
    views = kvprefix.open_views(physical)

    async def raw_list_keys(store):
        return store.keys()

    list_keys = kvprefix.scoped_list_keys(raw_list_keys)
    assert await list_keys(views["links"]) == ["slug:a"]


async def test_scoped_list_keys_raises_when_handed_the_physical_store():
    physical = FakeStore({"links:slug:a": b"1"})

    async def raw_list_keys(store):
        return store.keys()

    list_keys = kvprefix.scoped_list_keys(raw_list_keys)
    with pytest.raises(TypeError):
        await list_keys(physical)


# --- Cross-language drift guard ---


def test_go_prefix_constants_match_python_store_prefixes():
    """redirect/linkgate/keys.go's LinksPrefix/AnalyticsPrefix must stay
    byte-identical to kvprefix.STORE_PREFIXES — a mismatch means the API
    writes links the redirect path cannot find, with no error anywhere.
    Precedent for reading across component trees from a test:
    gui-pages/tests/test_manifest_components.py.
    """
    keys_go = Path(__file__).resolve().parents[2] / "redirect" / "linkgate" / "keys.go"
    source = keys_go.read_text()

    links_match = re.search(r'LinksPrefix\s*=\s*"([^"]*)"', source)
    analytics_match = re.search(r'AnalyticsPrefix\s*=\s*"([^"]*)"', source)
    assert links_match, "LinksPrefix constant not found in keys.go"
    assert analytics_match, "AnalyticsPrefix constant not found in keys.go"

    assert links_match.group(1) == kvprefix.STORE_PREFIXES["links"]
    assert analytics_match.group(1) == kvprefix.STORE_PREFIXES["analytics"]


def test_go_count_shards_matches_python_count_shards():
    """redirect/linkgate/keys.go's CountShards must stay equal to
    analytics.COUNT_SHARDS. The writer picks a shard in [0, CountShards) and
    the reader sums [0, COUNT_SHARDS); if the reader's value is LOWER, every
    click recorded into a higher shard silently disappears from the total,
    with no error anywhere — the same silent failure shape as the prefixes
    above, so it is pinned the same way.
    """
    keys_go = Path(__file__).resolve().parents[2] / "redirect" / "linkgate" / "keys.go"
    source = keys_go.read_text()

    shards_match = re.search(r"CountShards\s*=\s*(\d+)", source)
    assert shards_match, "CountShards constant not found in keys.go"

    assert int(shards_match.group(1)) == analytics.COUNT_SHARDS


# --- Toggleable structured logging (docs/plans/toggleable-logging.md) ---
#
# New tests only, below this line — nothing above this comment is edited,
# so every pre-existing call site (which never passes a collector) keeps
# working unchanged.


class _FakeCollector:
    """Records (op_type, namespace, num_bytes) tuples with no real timing —
    duration_ns is asserted only to be a non-negative int, never a fixed
    value, since a real monotonic clock diff is not reproducible in a test.
    """

    def __init__(self):
        self.calls: list[tuple[str, str, int]] = []

    def record(self, op_type, namespace, duration_ns, num_bytes=0):
        assert isinstance(duration_ns, int) and duration_ns >= 0
        self.calls.append((op_type, namespace, num_bytes))


async def test_prefixed_store_records_get_set_delete_exists_when_collector_present():
    physical = FakeStore()
    collector = _FakeCollector()
    view = kvprefix.PrefixedStore(physical, "links:", collector)

    await view.exists("slug:a")
    await view.set("slug:a", b"hello")
    await view.get("slug:a")
    await view.delete("slug:a")

    op_types = [c[0] for c in collector.calls]
    assert op_types == ["exists", "set", "get", "delete"]

    namespaces = {c[1] for c in collector.calls}
    assert namespaces == {"links"}  # trailing colon stripped, never "links:"

    set_call = collector.calls[1]
    assert set_call == ("set", "links", 5)  # len(b"hello")

    get_call = collector.calls[2]
    assert get_call == ("get", "links", 5)


async def test_prefixed_store_records_zero_bytes_for_a_miss():
    physical = FakeStore()
    collector = _FakeCollector()
    view = kvprefix.PrefixedStore(physical, "links:", collector)

    result = await view.get("slug:missing")
    assert result is None
    assert collector.calls == [("get", "links", 0)]


async def test_prefixed_store_with_no_collector_is_unaffected():
    physical = FakeStore()
    view = kvprefix.PrefixedStore(physical, "links:")  # no collector passed at all

    await view.exists("slug:a")
    await view.set("slug:a", b"value")
    assert await view.get("slug:a") == b"value"
    await view.delete("slug:a")
    assert await view.exists("slug:a") is False


async def test_open_views_threads_one_collector_into_every_namespace():
    physical = FakeStore()
    collector = _FakeCollector()
    views = kvprefix.open_views(physical, collector)

    await views["links"].set("slug:a", b"x")
    await views["users"].set("user:bob", b"y")
    await views["analytics"].set("count:a", b"z")

    namespaces = [c[1] for c in collector.calls]
    assert namespaces == ["links", "users", "analytics"]


async def test_open_views_defaults_to_no_collector():
    views = kvprefix.open_views(FakeStore())
    for view in views.values():
        assert view.collector is None


async def test_scoped_list_keys_records_when_collector_present():
    physical = FakeStore({"links:slug:a": b"1", "links:slug:b": b"2"})
    collector = _FakeCollector()
    views = kvprefix.open_views(physical, collector)

    async def raw_list_keys(store):
        return store.keys()

    list_keys = kvprefix.scoped_list_keys(raw_list_keys, collector)
    result = await list_keys(views["links"])

    assert sorted(result) == ["slug:a", "slug:b"]
    assert collector.calls == [("list_keys", "links", 0)]


async def test_scoped_list_keys_type_error_guard_unchanged_with_collector():
    physical = FakeStore({"links:slug:a": b"1"})
    collector = _FakeCollector()

    async def raw_list_keys(store):
        return store.keys()

    list_keys = kvprefix.scoped_list_keys(raw_list_keys, collector)
    with pytest.raises(TypeError):
        await list_keys(physical)


def test_prefixed_store_still_has_no_get_keys_with_collector():
    view = kvprefix.PrefixedStore(FakeStore(), "links:", _FakeCollector())
    assert not hasattr(view, "get_keys")


# --- on_error (docs/plans/observable-kv-failures.md) ---


class _RaisingStore:
    """Raises the given exception on every operation, regardless of key."""

    def __init__(self, exc: Exception):
        self._exc = exc

    async def get(self, key: str):
        raise self._exc

    async def set(self, key: str, value: bytes) -> None:
        raise self._exc

    async def delete(self, key: str) -> None:
        raise self._exc

    async def exists(self, key: str) -> bool:
        raise self._exc


async def test_on_error_is_called_and_the_original_exception_still_reraises():
    boom = RuntimeError("kv boom")
    calls: list[tuple] = []

    def on_error(op, namespace, duration_ns, exc):
        calls.append((op, namespace, exc))

    view = kvprefix.PrefixedStore(_RaisingStore(boom), "links:", on_error=on_error)

    with pytest.raises(RuntimeError) as excinfo:
        await view.get("slug:a")
    assert excinfo.value is boom  # unchanged, not wrapped or replaced
    assert calls == [("get", "links", boom)]


async def test_on_error_fires_for_set_delete_and_exists_too():
    boom = RuntimeError("kv boom")
    calls: list[str] = []

    def on_error(op, namespace, duration_ns, exc):
        calls.append(op)

    view = kvprefix.PrefixedStore(_RaisingStore(boom), "links:", on_error=on_error)

    for coro in (view.set("slug:a", b"v"), view.delete("slug:a"), view.exists("slug:a")):
        with pytest.raises(RuntimeError):
            await coro

    assert calls == ["set", "delete", "exists"]


async def test_on_error_none_default_does_not_swallow_or_crash():
    boom = RuntimeError("kv boom")
    view = kvprefix.PrefixedStore(_RaisingStore(boom), "links:")  # no on_error passed

    with pytest.raises(RuntimeError):
        await view.get("slug:a")


async def test_on_error_does_not_affect_collector_on_success():
    """A successful call after on_error is wired must still record into the
    collector exactly as before — on_error is a failure-only seam."""
    physical = FakeStore()
    collector = _FakeCollector()
    calls = []
    view = kvprefix.PrefixedStore(physical, "links:", collector, on_error=lambda *a: calls.append(a))

    await view.set("slug:a", b"value")
    assert await view.get("slug:a") == b"value"

    assert calls == []  # on_error never fired
    assert ("set", "links", 5) in collector.calls
    assert ("get", "links", 5) in collector.calls


async def test_nothing_is_recorded_into_the_collector_on_a_failure():
    boom = RuntimeError("kv boom")
    collector = _FakeCollector()
    view = kvprefix.PrefixedStore(_RaisingStore(boom), "links:", collector, on_error=lambda *a: None)

    with pytest.raises(RuntimeError):
        await view.get("slug:a")

    assert collector.calls == []


async def test_open_views_threads_on_error_into_every_namespace():
    physical = FakeStore()
    calls: list[str] = []
    views = kvprefix.open_views(physical, on_error=lambda op, ns, dur, exc: calls.append(ns))

    # Force a failure by using a raising physical store for one view only —
    # simplest is to swap the raw store directly on the view under test.
    views["links"].raw = _RaisingStore(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await views["links"].get("slug:a")

    assert calls == ["links"]
