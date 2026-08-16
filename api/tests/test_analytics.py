import json

import analytics
import auth
import links
from responses import Request
from tests.fakes import FakeStore, fake_get_many


def _principal(username="alice", role="user", permissions=None):
    return auth.Principal(username=username, role=role, permissions=permissions or [], csrf_token="x")


def _create_request(payload):
    return Request(method="POST", uri="/api/links", headers={}, body=json.dumps(payload).encode("utf-8"))


async def _make_link(links_store, owner="alice"):
    created = await links.handle_create(links_store, _principal(username=owner), _create_request({"target_url": "https://example.com/x"}))
    return json.loads(created.body)["slug"]


async def test_analytics_not_found():
    links_store = FakeStore()
    analytics_store = FakeStore()
    resp = await analytics.handle_analytics(links_store, analytics_store, _principal(), "doesnotexist", 30, fake_get_many)
    assert resp.status == 404


async def test_analytics_forbidden_for_non_owner():
    links_store = FakeStore()
    analytics_store = FakeStore()
    slug = await _make_link(links_store, owner="alice")
    resp = await analytics.handle_analytics(links_store, analytics_store, _principal(username="bob"), slug, 30, fake_get_many)
    assert resp.status == 403


async def test_analytics_admin_can_view_others_links():
    links_store = FakeStore()
    analytics_store = FakeStore()
    slug = await _make_link(links_store, owner="alice")
    resp = await analytics.handle_analytics(links_store, analytics_store, _principal(username="admin", role="admin"), slug, 30, fake_get_many)
    assert resp.status == 200


async def test_analytics_view_all_permission_can_view_others_links():
    # Regression test: handle_analytics previously only checked owner-or-admin
    # and ignored links.view_all/links.edit_all entirely, the same bug fixed
    # in links.handle_get.
    links_store = FakeStore()
    analytics_store = FakeStore()
    slug = await _make_link(links_store, owner="alice")
    viewer = _principal(username="carol", permissions=["links.view_all"])
    resp = await analytics.handle_analytics(links_store, analytics_store, viewer, slug, 30, fake_get_many)
    assert resp.status == 200


async def test_analytics_edit_all_permission_can_view_others_links():
    links_store = FakeStore()
    analytics_store = FakeStore()
    slug = await _make_link(links_store, owner="alice")
    editor = _principal(username="dave", permissions=["links.edit_all"])
    resp = await analytics.handle_analytics(links_store, analytics_store, editor, slug, 30, fake_get_many)
    assert resp.status == 200


async def test_analytics_no_clicks_yet_returns_zeros():
    links_store = FakeStore()
    analytics_store = FakeStore()
    slug = await _make_link(links_store)
    resp = await analytics.handle_analytics(links_store, analytics_store, _principal(), slug, 30, fake_get_many)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["total"] == 0
    assert body["days"] == {}
    assert body["recent_events"] == []


# This test writes the LEGACY unsharded count:{slug} key, which nothing has
# written since the counter was sharded across CountShards keys. Keep it, and
# keep its assertions unmodified: it is the migration regression test, and it
# is what proves clicks recorded before sharding still show up in the total.
async def test_analytics_reports_count_and_events():
    links_store = FakeStore()
    analytics_store = FakeStore()
    slug = await _make_link(links_store)

    await analytics_store.set(f"count:{slug}", json.dumps({"total": 3, "days": {"2026-01-01": 3}}).encode("utf-8"))
    await analytics_store.set(f"events:{slug}:0", b"1000|https://ref-a.example/|desktop")
    await analytics_store.set(f"events:{slug}:1", b"2000|https://ref-b.example/|mobile")

    resp = await analytics.handle_analytics(links_store, analytics_store, _principal(), slug, 30, fake_get_many)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["total"] == 3
    assert body["days"] == {"2026-01-01": 3}
    assert len(body["recent_events"]) == 2
    # Most recent (larger unix_ms) first.
    assert body["recent_events"][0]["referrer"] == "https://ref-b.example/"
    assert body["recent_events"][0]["device_class"] == "mobile"
    assert body["recent_events"][1]["referrer"] == "https://ref-a.example/"
    assert "unix_ms" not in body["recent_events"][0]


async def test_analytics_skips_malformed_event_entries():
    links_store = FakeStore()
    analytics_store = FakeStore()
    slug = await _make_link(links_store)

    await analytics_store.set(f"events:{slug}:0", b"not-a-valid-event-format")
    await analytics_store.set(f"events:{slug}:1", b"2000|https://ref-b.example/|mobile")

    resp = await analytics.handle_analytics(links_store, analytics_store, _principal(), slug, 30, fake_get_many)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert len(body["recent_events"]) == 1
    assert body["recent_events"][0]["referrer"] == "https://ref-b.example/"


async def test_analytics_sums_counts_across_shards():
    links_store = FakeStore()
    analytics_store = FakeStore()
    slug = await _make_link(links_store)

    await analytics_store.set(f"count:{slug}:0", json.dumps({"total": 4, "days": {"2026-01-01": 3, "2026-01-02": 1}}).encode("utf-8"))
    await analytics_store.set(f"count:{slug}:7", json.dumps({"total": 2, "days": {"2026-01-02": 2}}).encode("utf-8"))
    await analytics_store.set(f"count:{slug}:15", json.dumps({"total": 5, "days": {"2026-01-03": 5}}).encode("utf-8"))

    resp = await analytics.handle_analytics(links_store, analytics_store, _principal(), slug, 30, fake_get_many)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["total"] == 11
    # The same day appearing in two shards adds rather than overwriting.
    assert body["days"] == {"2026-01-01": 3, "2026-01-02": 3, "2026-01-03": 5}


async def test_analytics_sums_the_legacy_key_alongside_shards():
    # The no-migration guarantee: pre-sharding clicks live in count:{slug} and
    # post-sharding ones in count:{slug}:{n}, and the total is both.
    links_store = FakeStore()
    analytics_store = FakeStore()
    slug = await _make_link(links_store)

    await analytics_store.set(f"count:{slug}", json.dumps({"total": 100, "days": {"2025-12-31": 100}}).encode("utf-8"))
    await analytics_store.set(f"count:{slug}:3", json.dumps({"total": 7, "days": {"2025-12-31": 2, "2026-01-01": 5}}).encode("utf-8"))

    resp = await analytics.handle_analytics(links_store, analytics_store, _principal(), slug, 30, fake_get_many)
    body = json.loads(resp.body)
    assert body["total"] == 107
    assert body["days"] == {"2025-12-31": 102, "2026-01-01": 5}


async def test_analytics_skips_an_unparseable_shard_without_blanking_the_total():
    # One corrupt shard must cost only its own clicks, never the whole history.
    links_store = FakeStore()
    analytics_store = FakeStore()
    slug = await _make_link(links_store)

    await analytics_store.set(f"count:{slug}:0", json.dumps({"total": 6, "days": {"2026-01-01": 6}}).encode("utf-8"))
    await analytics_store.set(f"count:{slug}:1", b"{not valid json at all")
    await analytics_store.set(f"count:{slug}:2", b"[1, 2, 3]")  # valid JSON, wrong shape
    await analytics_store.set(f"count:{slug}:4", json.dumps({"total": 9, "days": {"2026-01-02": 9}}).encode("utf-8"))

    resp = await analytics.handle_analytics(links_store, analytics_store, _principal(), slug, 30, fake_get_many)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["total"] == 15
    assert body["days"] == {"2026-01-01": 6, "2026-01-02": 9}


async def test_analytics_reads_every_shard_up_to_count_shards():
    # A click can land in any shard, so the reader must cover the whole range;
    # a reader that stopped early would silently lose the tail shards.
    links_store = FakeStore()
    analytics_store = FakeStore()
    slug = await _make_link(links_store)

    for shard in range(analytics.COUNT_SHARDS):
        await analytics_store.set(f"count:{slug}:{shard}", json.dumps({"total": 1, "days": {"2026-01-01": 1}}).encode("utf-8"))

    resp = await analytics.handle_analytics(links_store, analytics_store, _principal(), slug, 30, fake_get_many)
    body = json.loads(resp.body)
    assert body["total"] == analytics.COUNT_SHARDS
    assert body["days"] == {"2026-01-01": analytics.COUNT_SHARDS}


async def test_analytics_only_reads_configured_number_of_slots():
    links_store = FakeStore()
    analytics_store = FakeStore()
    slug = await _make_link(links_store)

    await analytics_store.set(f"events:{slug}:0", b"1000|https://in-range.example/|desktop")
    await analytics_store.set(f"events:{slug}:5", b"2000|https://out-of-range.example/|desktop")

    resp = await analytics.handle_analytics(links_store, analytics_store, _principal(), slug, 3, fake_get_many)
    body = json.loads(resp.body)
    assert len(body["recent_events"]) == 1
    assert body["recent_events"][0]["referrer"] == "https://in-range.example/"
