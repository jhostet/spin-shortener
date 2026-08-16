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
