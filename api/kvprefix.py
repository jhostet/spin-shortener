"""Maps this app's three logical KV namespaces onto Spin's single "default"
physical store by prefixing every key. Required by Akamai Functions, which
allows only the "default" label (see CLAUDE.md's Akamai deployment section).

Zero `spin_sdk` imports: the already-opened physical store arrives as a plain
parameter, so this module stays host-importable, the same rule backup.py and
consistency.py follow.
"""

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
    """

    __slots__ = ("raw", "prefix")

    def __init__(self, raw, prefix: str):
        self.raw = raw
        self.prefix = prefix

    async def get(self, key: str):
        return await self.raw.get(self.prefix + key)

    async def set(self, key: str, value: bytes) -> None:
        await self.raw.set(self.prefix + key, value)

    async def delete(self, key: str) -> None:
        await self.raw.delete(self.prefix + key)

    async def exists(self, key: str) -> bool:
        return await self.raw.exists(self.prefix + key)


def open_views(physical_store) -> dict[str, PrefixedStore]:
    """{"links": view, "users": view, "analytics": view} over one open store."""
    return {
        name: PrefixedStore(physical_store, prefix)
        for name, prefix in STORE_PREFIXES.items()
    }


def scoped_list_keys(raw_list_keys):
    """Wrap a raw list_keys(physical_store) callable into one that takes a
    PrefixedStore and returns only that namespace's keys, prefix stripped.

    THIS FILTER IS A SECURITY CONTROL, not tidiness. backup.py's
    redact_user_value matches the `user:` prefix and is_excluded_key returns
    False for every store but "users"; consistency.py classifies keys by
    unprefixed shape; auth.delete_sessions_for_user matches `session:`. Every
    one of those guards silently stops matching if a view's enumeration
    returns another namespace's keys — the concrete failure being full PBKDF2
    account hashes written into a backup file.
    """

    async def list_keys(store) -> list[str]:
        if not isinstance(store, PrefixedStore):
            raise TypeError(
                "scoped_list_keys requires a PrefixedStore; enumerating the "
                "physical store directly would cross namespace boundaries"
            )
        prefix = store.prefix
        return [
            key[len(prefix):]
            for key in await raw_list_keys(store.raw)
            if key.startswith(prefix)
        ]

    return list_keys
