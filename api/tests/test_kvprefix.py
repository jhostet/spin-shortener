import re
from pathlib import Path

import pytest

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
