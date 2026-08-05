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
