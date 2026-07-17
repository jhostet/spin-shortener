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
