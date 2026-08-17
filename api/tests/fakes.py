"""In-memory stand-in for spin_sdk.key_value.Store, matching its async method
signatures so business-logic functions (which take `store` as a plain
parameter) can be tested without the real WASI KV bridge.
"""

from typing import Optional


class FakeStore:
    def __init__(self, initial: Optional[dict[str, bytes]] = None):
        self._data: dict[str, bytes] = dict(initial or {})

    async def get(self, key: str) -> Optional[bytes]:
        return self._data.get(key)

    async def set(self, key: str, value: bytes) -> None:
        self._data[key] = value

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def exists(self, key: str) -> bool:
        return key in self._data

    def keys(self) -> list[str]:
        return list(self._data.keys())


class WriteRaisingStore(FakeStore):
    """A FakeStore whose `set`/`delete` raise — used to pin that a read-only
    handler (e.g. `consistency.handle_consistency`) never writes. `get`,
    `exists` and `keys()` behave exactly like FakeStore, so this can seed and
    be read from normally; only a write is fatal."""

    async def set(self, key: str, value: bytes) -> None:
        raise AssertionError(f"unexpected write: set({key!r})")

    async def delete(self, key: str) -> None:
        raise AssertionError(f"unexpected write: delete({key!r})")


async def fake_list_keys(store) -> list[str]:
    """Stands in for app.py's real `_kv_keys` drain helper (the `get_keys`
    stream/future pair), so backup.py's pure handlers can be exercised without
    a real WASI KV bridge."""
    return store.keys()


async def fake_get_many(store, keys: list[str]) -> dict:
    """Stands in for the SCOPED `get_many` callable app.py threads into
    handlers (kvbatch.scoped_get_many's return value) — used by every
    handler-level test (Stages 1-5 of docs/plans/batch-kv-reads.md).

    Deliberately ignores any PrefixedStore-ness, exactly like handler tests
    already pass a raw FakeStore directly for get/set: the awkward host
    semantics (omitted misses, out-of-order replies) are hostile behaviour
    of the RAW callable scoped_get_many wraps, not of the normalised contract
    handlers see, so reproducing them here would be testing the wrong layer.
    Returns dict[str, bytes | None] for EVERY requested key, matching exactly
    what the real scoped_get_many guarantees to its callers.
    """
    return {key: await store.get(key) for key in keys}


def fake_raw_get_many(data: dict[str, bytes], *, none_for_missing: bool = False, raises: bool = False):
    """Factory for a stand-in for `wasi_batch.get_many` ITSELF — the raw,
    physical-key, synchronous callable `kvbatch.scoped_get_many` wraps. Used
    only by tests/test_kvbatch.py to exercise the seam's own normalisation;
    every other test uses fake_get_many above instead.

    Reproduces the two hostile semantics real hosts show, on purpose:

    - A requested key absent from `data` is OMITTED from the reply entirely
      by default (observed on local Spin, 2026-08-15) — pass
      `none_for_missing=True` for the WIT-documented `(key, None)` form
      instead (observed on Akamai, 2026-08-16). scoped_get_many must
      normalise BOTH into the same result.
    - The reply comes back in a DELIBERATELY DIFFERENT order than requested
      (reversed) — deterministic, and obviously not the requested order — so
      an implementation that zipped positionally instead of keying by name
      would visibly mis-associate a value with the wrong key.

    `raises=True` makes every call raise instead, for exercising
    scoped_get_many's per-chunk `gather_reads` fallback.
    """

    def raw_get_many(physical_keys: list[str]) -> list[tuple]:
        if raises:
            raise RuntimeError("simulated wasi:keyvalue/batch failure")
        pairs = []
        for key in physical_keys:
            if key in data:
                pairs.append((key, data[key]))
            elif none_for_missing:
                pairs.append((key, None))
            # else: omitted entirely — the local-Spin-observed behaviour
        return list(reversed(pairs))

    return raw_get_many


class KvThrottleError(Exception):
    """Stands in for the real `Err(Error_Other(value='too many requests'))`
    that a throttled `spin_sdk.key_value` write raises. `componentize_py_types`
    (which defines the real `Err`/`Error_Other` dataclasses) is injected by
    componentize-py at build time and doesn't exist in the host venv, so a
    test cannot construct the real thing — but its `str()` is reproduced
    exactly (docs/plans/write-throttle-resilience.md confirmed this against a
    faithful reconstruction of the two generated dataclasses), which is all
    `kvretry.classify_write_error` ever looks at."""

    def __init__(self):
        super().__init__("Error_Other(value='too many requests')")


class KvOtherError(Exception):
    """Stands in for a non-throttle write failure, e.g. `Err(Error_AccessDenied())`."""

    def __init__(self):
        super().__init__("Error_AccessDenied()")


class ThrottlingStore(FakeStore):
    """A FakeStore whose `set`/`delete` fail for chosen keys a chosen number
    of times before succeeding (or forever, if `fail_times` is `None`) — used
    to simulate a throttled write for `kvretry`/bulk/backup/repair/purge
    tests. `fail_times` maps key -> remaining-failures-before-success; a key
    absent from the map never fails.
    """

    def __init__(self, initial: Optional[dict[str, bytes]] = None, *, fail_times: Optional[dict[str, int]] = None,
                 error_factory=KvThrottleError):
        super().__init__(initial)
        self._fail_times: dict[str, int] = dict(fail_times or {})
        self._error_factory = error_factory
        self.set_attempts: dict[str, int] = {}
        self.delete_attempts: dict[str, int] = {}

    async def _maybe_fail(self, key: str, counts: dict[str, int]) -> None:
        counts[key] = counts.get(key, 0) + 1
        remaining = self._fail_times.get(key)
        if remaining is None:
            return
        if remaining > 0:
            self._fail_times[key] = remaining - 1
            raise self._error_factory()

    async def set(self, key: str, value: bytes) -> None:
        await self._maybe_fail(key, self.set_attempts)
        await super().set(key, value)

    async def delete(self, key: str) -> None:
        await self._maybe_fail(key, self.delete_attempts)
        await super().delete(key)


def recording_sleep():
    """Returns `(sleep, delays_list)` — a fake `kvretry` sleep primitive that
    records every requested delay (nanoseconds) into `delays_list` and
    returns immediately, so a retry backoff *schedule* can be asserted
    without any test ever actually waiting."""
    delays: list[int] = []

    async def sleep(nanoseconds: int) -> None:
        delays.append(nanoseconds)

    return sleep, delays
