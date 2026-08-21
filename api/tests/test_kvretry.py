import pytest

import kvretry
from tests.fakes import KvOtherError, KvRateLimitError, KvThrottleError, recording_sleep


def _const_jitter(value: float = 0.5):
    """Midpoint jitter — makes `delay == base_delay` exactly, per
    kvretry.make_writer's +/- JITTER_FRACTION formula, so a test can assert
    the schedule byte-for-byte."""
    return lambda: value


@pytest.mark.asyncio
async def test_write_succeeds_first_try_sleeps_zero_times():
    sleep, delays = recording_sleep()
    write = kvretry.make_writer(sleep, jitter=_const_jitter())

    calls = []

    async def make_coro():
        calls.append(1)

    await write(make_coro)
    assert calls == [1]
    assert delays == []


@pytest.mark.asyncio
async def test_write_fails_twice_then_succeeds_sleeps_exact_backoff():
    sleep, delays = recording_sleep()
    write = kvretry.make_writer(sleep, jitter=_const_jitter())

    attempts = {"n": 0}

    async def make_coro():
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise KvThrottleError()

    await write(make_coro, kvretry.RECORD_WRITE)
    assert attempts["n"] == 3
    assert delays == list(kvretry.RECORD_WRITE.backoff_ns[:2])


@pytest.mark.asyncio
async def test_write_never_succeeds_raises_write_failed_with_details():
    sleep, delays = recording_sleep()
    write = kvretry.make_writer(sleep, jitter=_const_jitter())

    async def make_coro():
        raise KvThrottleError()

    with pytest.raises(kvretry.WriteFailed) as exc_info:
        await write(make_coro, kvretry.RECORD_WRITE)

    err = exc_info.value
    assert err.attempts == kvretry.RECORD_WRITE.attempts
    assert err.label == "throttled"
    assert isinstance(err.cause, KvThrottleError)
    assert len(delays) == kvretry.RECORD_WRITE.attempts - 1


@pytest.mark.asyncio
async def test_exhausted_record_budget_leaves_index_budget_full():
    sleep, delays = recording_sleep()
    write = kvretry.make_writer(sleep, jitter=_const_jitter())

    async def always_fails():
        raise KvThrottleError()

    with pytest.raises(kvretry.WriteFailed):
        await write(always_fails, kvretry.RECORD_WRITE)

    # Record budget should now be reduced (or exhausted); index writes must
    # still have their FULL budget, since the two counters are independent.
    # (Three failures' worth of INDEX_WRITE backoff — 100+300+700ms — comfortably
    # fits the 3s index budget; used rather than a number requiring the full
    # 6-attempt schedule, whose backoff sum (4.3s) exceeds the budget on its
    # own and would exercise budget exhaustion instead of what this test is
    # actually pinning: independence of the two counters.)
    attempts = {"n": 0}

    async def succeeds_after_three_fails():
        attempts["n"] += 1
        if attempts["n"] <= 3:
            raise KvThrottleError()

    await write(succeeds_after_three_fails, kvretry.INDEX_WRITE)
    assert attempts["n"] == 4


@pytest.mark.asyncio
async def test_exhausted_index_budget_leaves_record_budget_full():
    sleep, delays = recording_sleep()
    write = kvretry.make_writer(sleep, jitter=_const_jitter())

    async def always_fails():
        raise KvThrottleError()

    with pytest.raises(kvretry.WriteFailed):
        await write(always_fails, kvretry.INDEX_WRITE)

    attempts = {"n": 0}

    async def succeeds_after_two_fails():
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise KvThrottleError()

    await write(succeeds_after_two_fails, kvretry.RECORD_WRITE)
    assert attempts["n"] == 3


def test_classify_write_error_throttled():
    assert kvretry.classify_write_error(KvThrottleError()) == "throttled"


def test_classify_write_error_labels_the_rate_limit_message_throttled_too():
    """The regression this whole change exists for.

    `key-value error: rate limit exceeded` is what a real over-cap bulk create
    produced on the deployed app 2026-08-21, and the original
    `"too many requests"`-only match labelled it `other` — the mislabel that
    made `write_error=other` the reported value all through the 2026-08-15 and
    2026-08-17 throttling incidents. Reducing THROTTLE_MESSAGE_MARKERS back to
    a single entry must fail this test."""
    assert kvretry.classify_write_error(KvRateLimitError()) == "throttled"


def test_classify_write_error_other():
    assert kvretry.classify_write_error(KvOtherError()) == "other"


def test_classify_write_error_matches_every_observed_marker():
    """Each marker is independently load-bearing: a tuple that happens to
    contain a substring of another entry, or an entry no branch reaches, would
    pass the two tests above while leaving a real message mislabelled."""
    for marker in kvretry.THROTTLE_MESSAGE_MARKERS:
        exc = Exception(f"Error_Other(value='{marker}')")
        assert kvretry.classify_write_error(exc) == "throttled", marker


def test_classify_write_error_is_case_insensitive_on_both_markers():
    for marker in kvretry.THROTTLE_MESSAGE_MARKERS:
        assert kvretry.classify_write_error(Exception(marker.upper())) == "throttled"


def test_backup_and_kvretry_agree_on_throttle_markers():
    """`api/backup.py` inlines this tuple rather than importing it, so that the
    restore path takes no dependency on the retry seam (CLAUDE.md, "Write-
    throttle resilience"). That is a deliberate duplication, which makes it a
    drift risk: a label enforced in one of two places is not enforced. This
    reads the literal back out of backup.py's source and pins the two equal,
    the same technique api/tests/test_kvprefix.py uses to pin keys.go's
    prefixes against kvprefix.py's."""
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "backup.py").read_text()
    match = re.search(r"throttle_markers = \(([^)]*)\)", source)
    assert match, "backup.py no longer defines a throttle_markers tuple"
    inlined = tuple(re.findall(r"\"([^\"]+)\"", match.group(1)))
    assert inlined == kvretry.THROTTLE_MESSAGE_MARKERS, (
        f"backup.py has {inlined}, kvretry has {kvretry.THROTTLE_MESSAGE_MARKERS}"
    )


@pytest.mark.asyncio
async def test_direct_performs_exactly_one_call_and_never_sleeps():
    calls = []

    async def make_coro():
        calls.append(1)

    await kvretry.direct(make_coro)
    assert calls == [1]


@pytest.mark.asyncio
async def test_direct_propagates_failure_with_no_retry():
    async def make_coro():
        raise KvThrottleError()

    with pytest.raises(KvThrottleError):
        await kvretry.direct(make_coro)


def test_module_has_no_spin_sdk_or_asyncio_imports():
    import inspect

    source = inspect.getsource(kvretry)
    assert "spin_sdk" not in source
    assert "import asyncio" not in source


@pytest.mark.asyncio
async def test_writer_records_write_retry_and_write_failed_on_collector():
    class FakeCollector:
        def __init__(self):
            self.records = []

        def record(self, op_type, namespace, duration_ns, num_bytes=0, num_keys=1):
            self.records.append((op_type, namespace, duration_ns, num_bytes))

    sleep, delays = recording_sleep()
    collector = FakeCollector()
    write = kvretry.make_writer(sleep, collector, jitter=_const_jitter())

    async def always_fails():
        raise KvThrottleError()

    with pytest.raises(kvretry.WriteFailed):
        await write(always_fails, kvretry.RECORD_WRITE)

    op_types = [r[0] for r in collector.records]
    assert op_types.count("write_retry") == kvretry.RECORD_WRITE.attempts - 1
    assert op_types.count("write_failed") == 1
