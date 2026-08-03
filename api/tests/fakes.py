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
