"""kvbatch.gather_reads: order preservation and the concurrency bound.
kvbatch.scoped_get_many: the seam that normalises wasi:keyvalue/batch's two
hostile semantics (unordered replies, two different missing-key shapes)
into dict[str, bytes | None].

Both gather_reads properties are silent when they break — an unbounded
fan-out still returns correct data (it just stops respecting the read cap),
and an out-of-order result still returns the right *set* of links. Neither
would be caught by the callers' own tests, which is why they are pinned
here. scoped_get_many's properties are equally silent: a positional
mis-association is a wrong VALUE attached to the wrong key, not a crash.
"""

import asyncio

import pytest

import kvbatch
import kvprefix
import obs
from tests.fakes import FakeStore, fake_raw_get_many


async def test_results_come_back_in_input_order_not_completion_order():
    """handle_list returns links in index order, and backup/consistency zip
    results back against their key lists — all three break if this slips."""

    async def slow_then_fast(value, delay):
        await asyncio.sleep(delay)
        return value

    # Deliberately inverted: the first coroutine finishes last.
    results = await kvbatch.gather_reads(
        [slow_then_fast("a", 0.03), slow_then_fast("b", 0.02), slow_then_fast("c", 0.0)]
    )
    assert results == ["a", "b", "c"]


async def test_never_exceeds_the_concurrency_limit():
    peak = 0
    in_flight = 0

    async def tracked():
        nonlocal peak, in_flight
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.001)
        in_flight -= 1
        return None

    await kvbatch.gather_reads([tracked() for _ in range(50)], limit=5)
    assert peak <= 5


async def test_actually_overlaps_rather_than_running_one_at_a_time():
    """The whole point. A serial implementation would also pass the two tests
    above, so this one pins that concurrency happens at all."""
    peak = 0
    in_flight = 0

    async def tracked():
        nonlocal peak, in_flight
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.005)
        in_flight -= 1
        return None

    await kvbatch.gather_reads([tracked() for _ in range(20)], limit=10)
    assert peak > 1


async def test_empty_input_is_not_an_error():
    """A store with no keys, or a user who owns no links — both reach this
    with nothing to fetch."""
    assert await kvbatch.gather_reads([]) == []


async def test_default_limit_matches_the_measured_deployed_fan_out():
    """100 is empirical, not derived: it is the largest fan-out measured
    working against Akamai (the 64-shard analytics endpoint's 98 gets).
    Changing it should be a deliberate act with a fresh measurement."""
    assert kvbatch.MAX_CONCURRENT_READS == 100


# --- scoped_get_many ---


def test_max_keys_per_get_many_matches_the_2026_08_16_akamai_measurement():
    """1000 is empirical: get_many worked at K=5,000 and failed at K=10,000
    on the deployed app. 1000 is comfortably inside the working range, not
    the observed ceiling. Changing it should be a deliberate act with a
    fresh measurement, the same rule MAX_CONCURRENT_READS carries."""
    assert kvbatch.MAX_KEYS_PER_GET_MANY == 1000


async def test_every_requested_key_present_with_none_for_a_miss_omitting_form():
    """Local Spin's observed shape: a miss is omitted from the raw reply
    entirely. scoped_get_many must still report it as None, not leave it
    out of the result."""
    physical = FakeStore()
    view = kvprefix.PrefixedStore(physical, "links:")
    raw = fake_raw_get_many({"links:slug:a": b"1"})  # "links:slug:b" absent
    get_many = kvbatch.scoped_get_many(raw)

    result = await get_many(view, ["slug:a", "slug:b"])
    assert result == {"slug:a": b"1", "slug:b": None}


async def test_every_requested_key_present_with_none_for_a_miss_key_none_form():
    """Akamai's observed (and WIT-documented) shape: a miss comes back as an
    explicit (key, None) pair. Must normalise to the identical result as the
    omitting form above."""
    physical = FakeStore()
    view = kvprefix.PrefixedStore(physical, "links:")
    raw = fake_raw_get_many({"links:slug:a": b"1"}, none_for_missing=True)
    get_many = kvbatch.scoped_get_many(raw)

    result = await get_many(view, ["slug:a", "slug:b"])
    assert result == {"slug:a": b"1", "slug:b": None}


async def test_values_associated_by_key_not_position():
    """The load-bearing property. fake_raw_get_many reverses the reply order
    relative to the request, so a positional zip would swap these two
    values onto the wrong keys."""
    physical = FakeStore()
    view = kvprefix.PrefixedStore(physical, "links:")
    raw = fake_raw_get_many({"links:slug:a": b"AAA", "links:slug:b": b"BBB"})
    get_many = kvbatch.scoped_get_many(raw)

    result = await get_many(view, ["slug:a", "slug:b"])
    assert result == {"slug:a": b"AAA", "slug:b": b"BBB"}


async def test_raises_type_error_for_a_non_prefixed_store():
    physical = FakeStore()
    raw = fake_raw_get_many({})
    get_many = kvbatch.scoped_get_many(raw)

    with pytest.raises(TypeError):
        await get_many(physical, ["slug:a"])


async def test_a_key_from_another_namespace_is_dropped_not_surfaced():
    physical = FakeStore()
    view = kvprefix.PrefixedStore(physical, "links:")
    # The raw fake returns a users:-prefixed key alongside the requested one —
    # simulating a host that (incorrectly) echoed back a foreign key.
    raw = fake_raw_get_many({"links:slug:a": b"1", "users:user:bob": b"leak"})
    get_many = kvbatch.scoped_get_many(raw)

    result = await get_many(view, ["slug:a"])
    assert result == {"slug:a": b"1"}
    assert "user:bob" not in result


async def test_chunks_at_the_configured_max_and_merges_the_result(monkeypatch):
    monkeypatch.setattr(kvbatch, "MAX_KEYS_PER_GET_MANY", 3)
    physical = FakeStore()
    view = kvprefix.PrefixedStore(physical, "links:")
    keys = [f"slug:{i}" for i in range(4)]  # MAX + 1
    data = {f"links:{k}": f"v{k}".encode() for k in keys}
    calls: list[list[str]] = []

    raw_inner = fake_raw_get_many(data)

    def recording_raw(physical_keys):
        calls.append(list(physical_keys))
        return raw_inner(physical_keys)

    get_many = kvbatch.scoped_get_many(recording_raw)
    result = await get_many(view, keys)

    assert len(calls) == 2
    assert all(len(chunk) <= 3 for chunk in calls)
    assert result == {k: f"v{k}".encode() for k in keys}


async def test_duplicate_requested_keys_are_de_duplicated_on_the_wire():
    physical = FakeStore()
    view = kvprefix.PrefixedStore(physical, "links:")
    raw = fake_raw_get_many({"links:slug:a": b"1"})
    calls: list[list[str]] = []

    def recording_raw(physical_keys):
        calls.append(list(physical_keys))
        return raw(physical_keys)

    get_many = kvbatch.scoped_get_many(recording_raw)
    result = await get_many(view, ["slug:a", "slug:a", "slug:a"])

    assert calls == [["links:slug:a"]]  # de-duplicated, not sent three times
    assert result == {"slug:a": b"1"}


async def test_a_raising_raw_call_falls_back_to_gather_reads_and_still_succeeds():
    physical = FakeStore({"links:slug:a": b"1", "links:slug:b": b"2"})
    collector = obs.Collector()
    view = kvprefix.PrefixedStore(physical, "links:", collector)
    raw = fake_raw_get_many({}, raises=True)
    get_many = kvbatch.scoped_get_many(raw, collector)

    result = await get_many(view, ["slug:a", "slug:b", "slug:missing"])
    assert result == {"slug:a": b"1", "slug:b": b"2", "slug:missing": None}

    ops, _, _, _ = collector.totals()
    assert collector._stats["get_many_error"][0] == 1
    # Fallback reads (3 gets) plus the one get_many_error record.
    assert collector._stats["get"][0] == 3
    assert ops == 4


async def test_collector_records_num_keys_equal_to_chunk_size():
    physical = FakeStore({"links:slug:a": b"1"})
    view = kvprefix.PrefixedStore(physical, "links:")
    collector = obs.Collector()
    raw = fake_raw_get_many({"links:slug:a": b"1"})
    get_many = kvbatch.scoped_get_many(raw, collector)

    await get_many(view, ["slug:a", "slug:b", "slug:c"])

    _, _, _, total_keys = collector.totals()
    assert collector._stats["get_many"][3] == 3
    assert total_keys == 3


# --- on_error (docs/plans/observable-kv-failures.md) ---


async def test_scoped_get_many_reports_the_raw_failure_via_on_error_before_falling_back():
    physical = FakeStore({"links:slug:a": b"1", "links:slug:b": b"2"})
    view = kvprefix.PrefixedStore(physical, "links:")
    raw = fake_raw_get_many({}, raises=True)
    calls: list[tuple] = []

    def on_error(op, namespace, duration_ns, exc):
        calls.append((op, namespace, exc))

    get_many = kvbatch.scoped_get_many(raw, on_error=on_error)
    result = await get_many(view, ["slug:a", "slug:b"])

    # Fallback still succeeds — the values are correct — AND exactly one
    # failure was reported.
    assert result == {"slug:a": b"1", "slug:b": b"2"}
    assert len(calls) == 1
    op, namespace, exc = calls[0]
    assert op == "get_many"
    assert namespace == "links"
    assert isinstance(exc, Exception)


async def test_scoped_get_many_on_error_none_default_does_not_crash_the_fallback():
    physical = FakeStore({"links:slug:a": b"1"})
    view = kvprefix.PrefixedStore(physical, "links:")
    raw = fake_raw_get_many({}, raises=True)
    get_many = kvbatch.scoped_get_many(raw)  # no on_error passed

    result = await get_many(view, ["slug:a"])
    assert result == {"slug:a": b"1"}
