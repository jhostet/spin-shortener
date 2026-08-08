"""Bounded-concurrency gather for KV **reads**.

The host genuinely overlaps concurrent KV reads — measured at 19–58x
effective parallelism across 100 operations on the deployed app while
verifying the sharded click counter — so a handler that awaits one read at a
time pays full KV latency per key. On Akamai a single data operation costs
roughly 5–20 ms against 5–20 us locally, which is why this matters there and
is invisible in local testing.

**Reads only. Never wrap writes in this.** Akamai allows 1,000 KV read
requests per second per app but only 50 writes. Reads therefore have wide
headroom to exploit; writes are already cap-bound, so gathering them would
queue against the cap rather than overlap, and risks throttling. That
asymmetry is the whole reason the read-side changes are safe — see
"Deployment: Akamai Functions" in CLAUDE.md for the quota table.

`MAX_CONCURRENT_READS` is 100 because that is the largest fan-out actually
measured working against the deployed app (the 64-shard analytics endpoint
issues 98 gets in one request, and effective parallelism there was still
*rising* rather than saturating). It is an empirical ceiling on this host,
not a derived one — raise it only with a fresh measurement, not by
extrapolating, and note that the read cap is app-wide, so one handler
monopolising it degrades every concurrent request.
"""

import asyncio

MAX_CONCURRENT_READS = 100


async def gather_reads(coros, limit: int = MAX_CONCURRENT_READS) -> list:
    """Await `coros` concurrently, at most `limit` in flight, and return their
    results in input order.

    Order-preserving like `asyncio.gather`, which several callers depend on:
    `handle_list` returns links in index order, and both `backup.py` and
    `consistency.py` zip results back against their key lists.

    A semaphore rather than fixed-size chunks so a single slow operation
    stalls only its own slot instead of acting as a barrier for the whole
    batch.
    """
    semaphore = asyncio.Semaphore(limit)

    async def run(coro):
        async with semaphore:
            return await coro

    return await asyncio.gather(*(run(coro) for coro in coros))
