"""Maps this app's three logical KV namespaces onto Spin's single "default"
physical store by prefixing every key. Required by Akamai Functions, which
allows only the "default" label (see CLAUDE.md's Akamai deployment section).

Zero `spin_sdk` imports: the already-opened physical store arrives as a plain
parameter, so this module stays host-importable, the same rule backup.py and
consistency.py follow.
"""

import time

PHYSICAL_STORE = "default"

# No prefix may be a prefix of another — see test_kvprefix.py, which pins it.
STORE_PREFIXES = {
    "links": "links:",
    "users": "users:",
    "analytics": "analytics:",
}


class PrefixedStore:
    """A logical view of one namespace inside the physical store.

    Deliberately exposes only get/set/delete/exists — the entire store surface
    every business-logic module uses. It does NOT expose get_keys: an
    unscoped enumeration through a view would return every key in the app,
    including `users:user:*`, to callers (backup.py, consistency.py,
    urlpolicy.handle_violations, auth.delete_sessions_for_user) whose guards
    are all written against unprefixed key shapes and would silently fail to
    match. Omitting the method turns that mistake into an AttributeError
    instead of a credential leak. Enumerate through scoped_list_keys().

    `collector` is an optional obs.Collector (docs/plans/toggleable-logging.md)
    defaulting to None, so every existing call site that never passes one
    keeps working unchanged and pays no timing cost. When present, every
    method records its own duration and (for get/set) the value's byte
    length into it, namespaced by this view's prefix with the trailing
    colon stripped (e.g. "links", not "links:") — never by key.
    """

    __slots__ = ("raw", "prefix", "collector")

    def __init__(self, raw, prefix: str, collector=None):
        self.raw = raw
        self.prefix = prefix
        self.collector = collector

    async def get(self, key: str):
        start = time.monotonic_ns()
        value = await self.raw.get(self.prefix + key)
        if self.collector is not None:
            self.collector.record(
                "get", self._namespace(), time.monotonic_ns() - start, len(value) if value else 0
            )
        return value

    async def set(self, key: str, value: bytes) -> None:
        start = time.monotonic_ns()
        await self.raw.set(self.prefix + key, value)
        if self.collector is not None:
            self.collector.record("set", self._namespace(), time.monotonic_ns() - start, len(value))

    async def delete(self, key: str) -> None:
        start = time.monotonic_ns()
        await self.raw.delete(self.prefix + key)
        if self.collector is not None:
            self.collector.record("delete", self._namespace(), time.monotonic_ns() - start, 0)

    async def exists(self, key: str) -> bool:
        start = time.monotonic_ns()
        result = await self.raw.exists(self.prefix + key)
        if self.collector is not None:
            self.collector.record("exists", self._namespace(), time.monotonic_ns() - start, 0)
        return result

    def _namespace(self) -> str:
        return self.prefix.rstrip(":")


def open_views(physical_store, collector=None) -> dict[str, PrefixedStore]:
    """{"links": view, "users": view, "analytics": view} over one open store.

    `collector` is an optional obs.Collector, threaded into every view so
    PrefixedStore's get/set/delete/exists calls record their own timing.
    Defaults to None so every existing caller (which never passes one)
    is unaffected.
    """
    return {
        name: PrefixedStore(physical_store, prefix, collector)
        for name, prefix in STORE_PREFIXES.items()
    }


def scoped_list_keys(raw_list_keys, collector=None):
    """Wrap a raw list_keys(physical_store) callable into one that takes a
    PrefixedStore and returns only that namespace's keys, prefix stripped.

    THIS FILTER IS A SECURITY CONTROL, not tidiness. backup.py's
    redact_user_value matches the `user:` prefix and is_excluded_key returns
    False for every store but "users"; consistency.py classifies keys by
    unprefixed shape; auth.delete_sessions_for_user matches `session:`. Every
    one of those guards silently stops matching if a view's enumeration
    returns another namespace's keys — the concrete failure being full PBKDF2
    account hashes written into a backup file.

    `collector` is an optional obs.Collector, defaulting to None exactly like
    open_views, so every existing call site is unaffected.
    """

    async def list_keys(store) -> list[str]:
        if not isinstance(store, PrefixedStore):
            raise TypeError(
                "scoped_list_keys requires a PrefixedStore; enumerating the "
                "physical store directly would cross namespace boundaries"
            )
        prefix = store.prefix
        start = time.monotonic_ns()
        raw_keys = await raw_list_keys(store.raw)
        if collector is not None:
            collector.record("list_keys", store._namespace(), time.monotonic_ns() - start, 0)
        return [key[len(prefix):] for key in raw_keys if key.startswith(prefix)]

    return list_keys
