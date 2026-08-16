"""GET /api/analytics/click-totals — the dashboard's Clicks column.

The read-cost design is the interesting part (see handle_click_totals's
docstring), and the tests that matter are the ones that pin it: only existing
keys are read, and only for slugs the caller may see.
"""

import json

import analytics
import auth
from tests.fakes import FakeStore, fake_get_many, fake_list_keys


def _principal(username="alice", role="user", permissions=None):
    return auth.Principal(
        username=username, role=role, permissions=permissions or [], csrf_token="x"
    )


def _count(total):
    return json.dumps({"total": total, "days": {}}).encode("utf-8")


class RecordingStore(FakeStore):
    """Counts reads, so the cost claims in the docstring are testable rather
    than aspirational."""

    def __init__(self, data):
        super().__init__(data)
        self.read_keys: list[str] = []

    async def get(self, key):
        self.read_keys.append(key)
        return await super().get(key)


async def test_sums_shards_and_the_legacy_unsharded_key():
    links = FakeStore({"all_links": json.dumps(["promo"]).encode("utf-8")})
    analytics_store = FakeStore({
        "count:promo": _count(5),        # pre-sharding history
        "count:promo:3": _count(2),
        "count:promo:41": _count(4),
    })

    resp = await analytics.handle_click_totals(
        links, analytics_store, _principal(role="admin"), fake_list_keys, fake_get_many
    )
    assert resp.status == 200
    assert json.loads(resp.body)["totals"] == {"promo": 11}


async def test_reads_only_the_shard_keys_that_exist():
    """The whole point of the design. A link with clicks in two shards must
    cost two reads, not COUNT_SHARDS + 1 — otherwise a 200-link dashboard is
    13,000 reads against a 1,000/second app-wide cap."""
    links = FakeStore({"all_links": json.dumps(["promo"]).encode("utf-8")})
    analytics_store = RecordingStore({
        "count:promo:3": _count(2),
        "count:promo:41": _count(4),
    })

    await analytics.handle_click_totals(
        links, analytics_store, _principal(role="admin"), fake_list_keys, fake_get_many
    )
    count_reads = [k for k in analytics_store.read_keys if k.startswith("count:")]
    assert sorted(count_reads) == ["count:promo:3", "count:promo:41"]
    assert len(count_reads) < analytics.COUNT_SHARDS


async def test_never_reads_analytics_for_a_link_the_caller_cannot_see():
    """Ownership scoping is enforced before any read, not filtered afterwards
    — otherwise the endpoint would leak the existence and traffic of another
    user's links, both through the response and through what it touches."""
    links = FakeStore({
        "all_links": json.dumps(["mine", "theirs"]).encode("utf-8"),
        "owner_links:alice": json.dumps(["mine"]).encode("utf-8"),
    })
    analytics_store = RecordingStore({
        "count:mine:1": _count(3),
        "count:theirs:1": _count(99),
    })

    resp = await analytics.handle_click_totals(
        links, analytics_store, _principal(username="alice"), fake_list_keys, fake_get_many
    )
    assert json.loads(resp.body)["totals"] == {"mine": 3}
    assert not [k for k in analytics_store.read_keys if "theirs" in k]


async def test_events_keys_are_never_read():
    """Totals only. The events ring is the expensive, useless half here."""
    links = FakeStore({"all_links": json.dumps(["promo"]).encode("utf-8")})
    analytics_store = RecordingStore({
        "count:promo:1": _count(1),
        "events:promo:7": b"1754600000000|(direct)|other",
    })

    await analytics.handle_click_totals(
        links, analytics_store, _principal(role="admin"), fake_list_keys, fake_get_many
    )
    assert not [k for k in analytics_store.read_keys if k.startswith("events:")]


async def test_a_link_with_no_clicks_reports_zero_not_absent():
    """The column renders `clickTotals[slug] ?? 0`, but an explicit 0 keeps
    the contract honest: the endpoint answers for every visible link."""
    links = FakeStore({"all_links": json.dumps(["quiet"]).encode("utf-8")})
    resp = await analytics.handle_click_totals(
        links, FakeStore({}), _principal(role="admin"), fake_list_keys, fake_get_many
    )
    assert json.loads(resp.body)["totals"] == {"quiet": 0}


async def test_a_corrupt_shard_costs_only_its_own_clicks():
    links = FakeStore({"all_links": json.dumps(["promo"]).encode("utf-8")})
    analytics_store = FakeStore({
        "count:promo:1": _count(4),
        "count:promo:2": b"not json at all",
    })
    resp = await analytics.handle_click_totals(
        links, analytics_store, _principal(role="admin"), fake_list_keys, fake_get_many
    )
    assert json.loads(resp.body)["totals"] == {"promo": 4}


async def test_no_visible_links_short_circuits_without_enumerating():
    links = FakeStore({})
    analytics_store = RecordingStore({"count:someoneelse:1": _count(9)})
    resp = await analytics.handle_click_totals(
        links, analytics_store, _principal(username="nobody"), fake_list_keys, fake_get_many
    )
    assert json.loads(resp.body)["totals"] == {}
    assert analytics_store.read_keys == []
