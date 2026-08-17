"""The write-retry seam (docs/plans/write-throttle-resilience.md):
`make_writer` returns an async `write(make_coro, policy=RECORD_WRITE)` that
retries a KV write a bounded number of times, spending from a per-request
nanosecond sleep budget, and raises `WriteFailed` once that budget or the
policy's attempt count is exhausted.

**This module never decides what to do about a failed write — that stays
with the caller.** The load-bearing move in the whole plan is
"abandon-and-index-what-landed": a bulk handler that catches `WriteFailed`
mid-loop breaks out and indexes exactly the records that did land, rather
than continuing to hammer a throttled store. This module only makes reaching
that decision point less likely, by retrying first.

**Retryability: every write error is retryable, bounded. No variant check,
no string match, in the control path.** Two reasons, not one. First, a pure
module cannot import the WIT error types at all — `componentize_py_types` is
injected by componentize-py at build time and does not exist in the host
venv, so a pure module has no type to match against even if it wanted to.
Second, and more important: a message-substring match used for CONTROL FLOW
degrades in the worst possible direction. If Akamai rewords "too many
requests", the match silently stops firing and the app reverts to exactly
today's bug — no retries, bare 500, invisible drift. `classify_write_error`
still exists, but purely to LABEL the failure for the log line and the
response body ("throttled" vs "other") — a wording change at Akamai degrades
that label, and nothing else. See CLAUDE.md's write-throttle-resilience
section and the plan's Trade-offs #1 for the full argument.

**`sleep` is injected, never imported.** The module imports neither
`asyncio` nor a WASI clock — `await asyncio.sleep(d)` for any `d > 0` raises
inside the component (`componentize_py_async_support._Loop.call_later` is
unimplemented), so the real sleep lives in `api/app.py`, backed by
`wasi_clocks_monotonic_clock_0_3_0_rc_2026_03_15.wait_for` (confirmed
reachable and functional by the task-1 spike). Under pytest, `sleep` is a
fake that records the requested delays and returns immediately — the
backoff *schedule* is asserted with no test ever actually waiting, the same
shape `list_keys`/`read_file`/`get_many`/`purge_analytics` already take
elsewhere in this codebase.
"""

import random
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional


@dataclass(frozen=True)
class WritePolicy:
    kind: str                      # "record" | "index" — selects the RetryBudget counter
    attempts: int                  # total attempts, INCLUDING the first
    backoff_ns: tuple[int, ...]    # len == attempts - 1


RECORD_WRITE = WritePolicy("record", 3, (100_000_000, 300_000_000))
INDEX_WRITE = WritePolicy(
    "index", 6,
    (100_000_000, 300_000_000, 700_000_000, 1_200_000_000, 2_000_000_000),
)

MAX_RECORD_RETRY_SLEEP_NS = 2_000_000_000  # 2 s per request, all record writes
MAX_INDEX_RETRY_SLEEP_NS = 3_000_000_000   # 3 s per request, all index writes

JITTER_FRACTION = 0.25


class WriteFailed(Exception):
    """Raised when a write is still failing after its policy is exhausted
    (either its attempt count or its request-scoped sleep budget). Carries
    `attempts` (how many were actually made), `label` ("throttled" | "other",
    from `classify_write_error` on the LAST failure) and `cause` (the last
    raised exception)."""

    def __init__(self, attempts: int, label: str, cause: BaseException):
        super().__init__(f"write failed after {attempts} attempt(s): {label}")
        self.attempts = attempts
        self.label = label
        self.cause = cause


def classify_write_error(exc: BaseException) -> str:
    """Labels a write failure for the log line / response body only —
    NEVER used to decide whether to retry (see module docstring). Every
    write error is retried the same way regardless of what this returns.
    """
    return "throttled" if "too many requests" in str(exc).lower() else "other"


@dataclass
class _RetryBudget:
    """One per request. Two independent nanosecond counters — record writes
    and index writes draw from separate pools so the 50 cheap-to-lose record
    writes in a bulk create can never starve the one expensive-to-lose index
    write of its own budget (records are written first, so a shared pool
    would be drained before the index write ever ran)."""

    record_ns: int = MAX_RECORD_RETRY_SLEEP_NS
    index_ns: int = MAX_INDEX_RETRY_SLEEP_NS

    def remaining(self, kind: str) -> int:
        return self.record_ns if kind == "record" else self.index_ns

    def spend(self, kind: str, ns: int) -> None:
        if kind == "record":
            self.record_ns = max(0, self.record_ns - ns)
        else:
            self.index_ns = max(0, self.index_ns - ns)


async def direct(make_coro: Callable[[], Awaitable[None]], policy: Optional[WritePolicy] = None) -> None:
    """No retry — the default for every existing call site that doesn't
    thread a real writer through (e.g. test seeding). Performs exactly one
    call and never sleeps. Accepts (and ignores) a `policy` argument so it is
    a drop-in for `write(make_coro, policy)` at every real call site."""
    await make_coro()


def make_writer(sleep: Callable[[int], Awaitable[None]], collector=None, *, jitter: Callable[[], float] = random.random):
    """Returns `async def write(make_coro, policy=RECORD_WRITE) -> None`,
    closing over ONE `_RetryBudget` for the lifetime of this writer (i.e. one
    request — `app.py` constructs a fresh writer per `_dispatch` call, never
    a module-level one, for the same reason `obs.Collector` and the wasi
    bucket in `_make_raw_get_many` are per-request: `Handler.handle()`
    dispatches each request through `componentize_py_async_support.spawn`,
    so a shared budget would let one request's retries starve another's).

    `jitter` is injected (default `random.random`) so a test can pass a
    constant and assert the exact sleep sequence.
    """
    budget = _RetryBudget()

    async def write(make_coro: Callable[[], Awaitable[None]], policy: WritePolicy = RECORD_WRITE) -> None:
        last_exc: Optional[BaseException] = None
        for attempt in range(policy.attempts):
            try:
                await make_coro()
                return
            except Exception as exc:  # noqa: BLE001 — see module docstring: every write error is retryable
                last_exc = exc
                is_last_attempt = attempt == policy.attempts - 1
                if is_last_attempt:
                    break
                base_delay = policy.backoff_ns[attempt]
                # +/- JITTER_FRACTION, never negative.
                delay = int(base_delay * (1 + JITTER_FRACTION * (2 * jitter() - 1)))
                delay = max(0, delay)
                remaining = budget.remaining(policy.kind)
                if delay > remaining:
                    break  # budget exhausted — stop retrying, fall through to WriteFailed
                budget.spend(policy.kind, delay)
                await sleep(delay)
                if collector is not None:
                    collector.record("write_retry", "-", delay, 0)

        label = classify_write_error(last_exc)
        if collector is not None:
            collector.record("write_failed", "-", 0, 0)
        raise WriteFailed(attempt + 1, label, last_exc)

    return write
