"""Two multi-key KV **read** helpers: `gather_reads` (bounded-concurrency
fan-out of heterogeneous coroutines) and `scoped_get_many` (a single
`wasi:keyvalue/batch` `get_many` host call for K keys in one namespace).
Both live here together deliberately, so "which one do I use" is one
docstring rather than two modules' worth of cross-references — see each
function's own docstring below for the answer.

**Which one do I use?** `gather_reads` for a set of heterogeneous
coroutines (e.g. mixed `get`/`exists` calls) or for `exists` probes, which
have no batch equivalent (see `analyticsorphans.py`'s liveness pre-check).
`scoped_get_many` for a set of plain key reads in one `PrefixedStore`
namespace — it collapses K host round trips into one, which `gather_reads`
cannot do (it still issues K reads, just concurrently).

**Reads only. Never wrap writes in either helper.** Akamai allows 1,000 KV
read requests per second per app but only 50 writes. Reads therefore have
wide headroom to exploit; writes are already cap-bound, so batching or
gathering them would queue against the cap rather than overlap, and risks
throttling. That asymmetry is the whole reason the read-side changes in this
module are safe — see "Deployment: Akamai Functions" in CLAUDE.md for the
quota table.

`MAX_CONCURRENT_READS` is 100 because that is the largest fan-out actually
measured working against the deployed app (the 64-shard analytics endpoint
issues 98 gets in one request, and effective parallelism there was still
*rising* rather than saturating). It is an empirical ceiling on this host,
not a derived one — raise it only with a fresh measurement, not by
extrapolating, and note that the read cap is app-wide, so one handler
monopolising it degrades every concurrent request.

`MAX_KEYS_PER_GET_MANY` is 1000, likewise empirical: measured directly
against the deployed app on 2026-08-15/16, `get_many` worked at K=5,000 and
failed at K=10,000 with `Error_Other('key-value error: internal server
error')`, with latency flat across the whole range (~10 ms at K=1,000).
1,000 is comfortably inside the working range, not the observed ceiling —
raise it only with a fresh measurement, the same rule `MAX_CONCURRENT_READS`
carries. The same measurement run also confirmed `get_many(K)` does NOT bill
as K reads against Akamai's 1,000 RPS app-wide read cap: a positive control
of 10 parallel x 200 single gathered reads (2,000 reads/s) was throttled
9/10, while 10 parallel x `get_many(500)` (5,000 keys/s, 2.5x the control's
load) was throttled 0/10 — see TASKS.md's "BOTH SPIKES ANSWERED
(2026-08-15)" for the full numbers. See docs/plans/batch-kv-reads.md.
"""

import asyncio
import time

from kvprefix import PrefixedStore

MAX_CONCURRENT_READS = 100
MAX_KEYS_PER_GET_MANY = 1000


async def gather_reads(coros, limit: int = MAX_CONCURRENT_READS) -> list:
    """Await `coros` concurrently, at most `limit` in flight, and return their
    results in input order.

    Order-preserving like `asyncio.gather`, which several callers depend on:
    `backup.py` zips results back against its key list, and `scoped_get_many`
    below uses it as the per-chunk fallback when a raw `get_many` call raises.

    A semaphore rather than fixed-size chunks so a single slow operation
    stalls only its own slot instead of acting as a barrier for the whole
    batch.
    """
    semaphore = asyncio.Semaphore(limit)

    async def run(coro):
        async with semaphore:
            return await coro

    return await asyncio.gather(*(run(coro) for coro in coros))


def scoped_get_many(raw_get_many, collector=None, on_error=None):
    """Wrap a raw `get_many(physical_keys) -> list[tuple[str, bytes | None]]`
    callable (synchronous — see below) into an async callable that takes a
    `PrefixedStore` and a list of LOGICAL keys, and returns
    `dict[str, bytes | None]` containing EVERY requested key, `None` for any
    the store does not hold.

    Five properties, each deliberate — see docs/plans/batch-kv-reads.md's
    "The seam" section for the full rationale:

    1. Returns a dict, never a list — `get_many` does not preserve order (a
       2026-08-15 spike observed a reply come back in a different order than
       requested), so a positional result would be a data-corruption bug
       waiting for a caller. Never zip this positionally.
    2. Every requested key is present in the result, `None` for a miss — a
       drop-in for the `dict(zip(keys, await gather_reads(...)))` idiom
       elsewhere in this codebase (and it absorbs BOTH observed missing-key
       shapes, see point 5).
    3. Requires a `PrefixedStore` and raises `TypeError` otherwise, exactly
       like `kvprefix.scoped_list_keys`. A returned physical key that does not
       carry this view's prefix is dropped, never surfaced — the host has no
       reason to send one, but the namespace invariant stays explicit rather
       than incidental. `PrefixedStore` gains no batched method and still has
       no `get_keys`.
    4. Chunks, always, sequentially, at `MAX_KEYS_PER_GET_MANY`. The
       `wasi:keyvalue/batch` bindings are SYNCHRONOUS (`def get_many`, not
       `async def`) while Spin's KV bindings are `async` — so chunks cannot be
       overlapped with `asyncio.gather` or anything else, and chunk count is
       a direct wall-time multiplier. That is why the constant's value came
       from a measurement rather than a guess.
    5. Falls back per chunk, visibly. If `raw_get_many` raises, that chunk is
       re-read through `gather_reads(store.get(k) for k in chunk)` and the
       request still succeeds; the failure is recorded as a `get_many_error`
       operation (never silent) so a trace shows it rather than the path
       merely being slow forever. `on_error` (optional, docs/plans/
       observable-kv-failures.md), when set, is called with the raw
       exception BEFORE the fallback runs — this is the blind spot with the
       most known failure modes (K>=10,000 -> internal server error; batch
       throttling) and today it is otherwise invisible whenever tracing is
       off.

    The WIT-documented form (a `(key, None)` pair for a miss) and the
    2026-08-15 local-Spin-observed form (the pair OMITTED entirely) are
    normalised into the same thing here — TASKS.md's "BOTH SPIKES ANSWERED"
    section confirms local Spin and Akamai genuinely disagree on this, so
    both forms must be handled, not just the one seen locally.
    """

    async def get_many(store, keys):
        if not isinstance(store, PrefixedStore):
            raise TypeError(
                "scoped_get_many requires a PrefixedStore; batching against "
                "the physical store directly would cross namespace boundaries"
            )
        prefix = store.prefix
        wanted = list(dict.fromkeys(keys))  # de-duplicated; order is irrelevant
        results: dict[str, bytes | None] = {key: None for key in wanted}

        for start in range(0, len(wanted), MAX_KEYS_PER_GET_MANY):
            chunk = wanted[start:start + MAX_KEYS_PER_GET_MANY]
            t0 = time.monotonic_ns()
            try:
                pairs = raw_get_many([prefix + key for key in chunk])
            except Exception as exc:
                if on_error is not None:
                    on_error("get_many", store._namespace(), time.monotonic_ns() - t0, exc)
                if collector is not None:
                    collector.record(
                        "get_many_error", store._namespace(), time.monotonic_ns() - t0, 0, num_keys=len(chunk)
                    )
                for key, value in zip(chunk, await gather_reads(store.get(k) for k in chunk)):
                    results[key] = value
                continue
            num_bytes = 0
            for physical_key, value in pairs:
                if not physical_key.startswith(prefix):
                    continue  # never surface another namespace's key
                logical = physical_key[len(prefix):]
                if logical in results and value is not None:
                    results[logical] = value
                    num_bytes += len(value)
            if collector is not None:
                collector.record(
                    "get_many", store._namespace(), time.monotonic_ns() - t0, num_bytes, num_keys=len(chunk)
                )
        return results

    return get_many
