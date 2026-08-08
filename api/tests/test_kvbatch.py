"""kvbatch.gather_reads: order preservation and the concurrency bound.

Both properties are silent when they break — an unbounded fan-out still
returns correct data (it just stops respecting the read cap), and an
out-of-order result still returns the right *set* of links. Neither would be
caught by the callers' own tests, which is why they are pinned here.
"""

import asyncio

import kvbatch


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
