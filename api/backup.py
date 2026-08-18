"""KV backup and restore: the file format, redaction and validation helpers
(this module) plus the two /api/admin/backup and /api/admin/restore handlers
built on top of them.

Zero `spin_sdk` imports — `store`/`request` arrive as plain parameters and
`Request`/`Response` come from `responses`, matching the testability rule the
rest of `api/` follows (see `CLAUDE.md`). The `get_keys` drain itself lives in
`app.py` (the one piece of genuinely untestable plumbing) and is passed in
here as a `list_keys` callable, the same way `gui-pages/routing.py` takes
`read_file`.

See docs/plans/kv-backup-restore.md for the full design and rationale.
"""

import base64
import binascii
import json

from kvbatch import gather_reads
from responses import Response, iso_now, json_response

BACKUP_FORMAT = "spin-shortener-kv-backup"
SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = (1,)

# Export/display order.
BACKUP_STORES = ("links", "users", "analytics")
# Restore order. users LAST, deliberately: links and analytics land first so a
# mid-restore failure leaves the operator's session intact for a retry.
RESTORE_STORE_ORDER = ("links", "analytics", "users")

MAX_BACKUP_BODY_BYTES = 5_242_880
MAX_BACKUP_ENTRIES = 5_000

RESTORE_CONFIRMATION = "REPLACE"

# Index keys, written last within their store (see "Indexes" in the plan).
INDEX_KEYS = {
    "links": ("all_links",),  # plus every "owner_links:" prefixed key
    "users": ("_meta:usernames",),
    "analytics": (),
}
OWNER_LINKS_PREFIX = "owner_links:"
SESSION_PREFIX = "session:"
USER_PREFIX = "user:"
BOOTSTRAPPED_KEY = "_meta:bootstrapped"  # == auth.BOOTSTRAPPED_KEY


def parse_stores_param(raw: str | None) -> tuple[list[str] | None, dict | None]:
    """(stores, error_body). None/absent -> all of BACKUP_STORES, in that
    order. Allowlist-validated against BACKUP_STORES, never trusted directly
    — the same rule qr.handle_qr applies to ?base=."""
    if raw is None:
        return list(BACKUP_STORES), None

    names = [name.strip() for name in raw.split(",") if name.strip()]
    if not names:
        return None, {"error": "no_stores"}

    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        if name not in BACKUP_STORES:
            return None, {"error": "unknown_store", "store": name, "allowed_stores": list(BACKUP_STORES)}
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result, None


def redact_user_value(raw: bytes) -> bytes:
    """A user record with password_hash removed. A value that is not JSON, or
    not a JSON object, passes through unchanged rather than raising — the
    exporter must never fail on an unexpected value shape."""
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw
    if not isinstance(value, dict) or "password_hash" not in value:
        return raw
    redacted = {k: v for k, v in value.items() if k != "password_hash"}
    return json.dumps(redacted).encode("utf-8")


def is_excluded_key(store: str, key: str) -> bool:
    """True for the users store's BOOTSTRAPPED_KEY and any SESSION_PREFIX key.
    False for everything else, in every store."""
    if store != "users":
        return False
    return key == BOOTSTRAPPED_KEY or key.startswith(SESSION_PREFIX)


def build_backup(
    entries_by_store: dict[str, dict[str, bytes]],
    *,
    created_at: str,
    created_by: str,
    fidelity: str,
) -> dict:
    """Applies is_excluded_key, then redact_user_value to every USER_PREFIX key
    in the users store, then base64-encodes every value. Returns the document
    described in the plan's "Data model" section. Pure — no I/O, no clock, no
    store."""
    stores_out: dict[str, dict[str, str]] = {}
    counts: dict[str, int] = {}

    for store_name in BACKUP_STORES:
        if store_name not in entries_by_store:
            continue
        out: dict[str, str] = {}
        for key, value in entries_by_store[store_name].items():
            if is_excluded_key(store_name, key):
                continue
            if store_name == "users" and key.startswith(USER_PREFIX):
                value = redact_user_value(value)
            out[key] = base64.b64encode(value).decode("ascii")
        stores_out[store_name] = out
        counts[store_name] = len(out)

    return {
        "format": BACKUP_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "created_at": created_at,
        "created_by": created_by,
        "fidelity": fidelity,
        "key_encoding": "utf8",
        "value_encoding": "base64",
        "excluded": ["users/_meta:bootstrapped", "users/session:*", "users/user:*#password_hash"],
        "counts": counts,
        "stores": stores_out,
    }


def validate_backup(payload) -> tuple[dict[str, dict[str, bytes]] | None, dict | None]:
    """All-or-nothing. Returns (decoded_entries_by_store, None) only if EVERY
    check passes; otherwise (None, error_body). Nothing partially decoded is
    ever returned, so a caller cannot accidentally write from a bad file."""
    if not isinstance(payload, dict):
        return None, {"error": "invalid_backup"}

    if payload.get("format") != BACKUP_FORMAT:
        return None, {"error": "invalid_backup_format", "expected": BACKUP_FORMAT}

    schema_version = payload.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        return None, {
            "error": "unsupported_schema_version",
            "schema_version": schema_version,
            "supported_versions": list(SUPPORTED_SCHEMA_VERSIONS),
        }

    stores = payload.get("stores")
    if not isinstance(stores, dict):
        return None, {"error": "invalid_backup"}
    if not stores:
        return None, {"error": "no_stores"}

    total_entries = 0
    for store_name, entries in stores.items():
        if store_name not in BACKUP_STORES:
            return None, {"error": "unknown_store", "store": store_name, "allowed_stores": list(BACKUP_STORES)}
        if not isinstance(entries, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in entries.items()
        ):
            return None, {"error": "invalid_entries", "store": store_name}
        total_entries += len(entries)

    if total_entries > MAX_BACKUP_ENTRIES:
        return None, {"error": "too_many_entries", "max_entries": MAX_BACKUP_ENTRIES, "entry_count": total_entries}

    decoded_entries_by_store: dict[str, dict[str, bytes]] = {}
    for store_name, entries in stores.items():
        decoded_entries: dict[str, bytes] = {}
        for key, value in entries.items():
            try:
                raw = base64.b64decode(value, validate=True)
            except (binascii.Error, ValueError):
                return None, {"error": "invalid_value_encoding", "store": store_name, "key": key}

            if store_name == "users":
                if key == BOOTSTRAPPED_KEY or key.startswith(SESSION_PREFIX):
                    return None, {"error": "forbidden_key", "store": store_name, "key": key}
                if key.startswith(USER_PREFIX):
                    try:
                        parsed = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        parsed = None
                    if isinstance(parsed, dict) and "password_hash" in parsed:
                        return None, {"error": "credential_material_in_backup", "key": key}

            decoded_entries[key] = raw
        decoded_entries_by_store[store_name] = decoded_entries

    return decoded_entries_by_store, None


def restore_write_order(store: str, keys: list[str]) -> list[str]:
    """Non-index keys first (in the file's own order), index keys last."""

    def _is_index(key: str) -> bool:
        if key in INDEX_KEYS.get(store, ()):
            return True
        if store == "links" and key.startswith(OWNER_LINKS_PREFIX):
            return True
        return False

    non_index = [key for key in keys if not _is_index(key)]
    index = [key for key in keys if _is_index(key)]
    return non_index + index


async def handle_export(
    stores_by_name: dict[str, object],
    principal,
    query: dict,
    list_keys,
) -> Response:
    if not principal.has_permission("users.manage"):
        return json_response(403, {"error": "forbidden", "required_permission": "users.manage"})

    raw_stores = query.get("stores", [None])[0] if isinstance(query, dict) else None
    selected_stores, error = parse_stores_param(raw_stores)
    if error:
        return json_response(400, error)

    entries_by_store: dict[str, dict[str, bytes]] = {}
    for store_name in selected_stores:
        store = stores_by_name[store_name]
        keys = await list_keys(store)
        # Gathered, not a round trip per key: an export reads every key in the
        # store, so this is the largest read fan-out in the app and the one
        # most exposed to per-operation latency. Bounded by gather_reads —
        # export is already the path closest to the 30-second handler limit,
        # and an unbounded fan-out over a large store would swap a latency
        # problem for a read-cap one. Writes (the restore loop below) are
        # deliberately NOT gathered; they are cap-bound, not latency-bound.
        values = await gather_reads(store.get(key) for key in keys)
        entries: dict[str, bytes] = {
            key: value for key, value in zip(keys, values) if value is not None
        }
        entries_by_store[store_name] = entries

    doc = build_backup(
        entries_by_store,
        created_at=iso_now(),
        created_by=principal.username,
        fidelity="full",
    )

    body_bytes = json.dumps(doc).encode("utf-8")
    if len(body_bytes) > MAX_BACKUP_BODY_BYTES:
        return json_response(500, {
            "error": "backup_too_large",
            "max_bytes": MAX_BACKUP_BODY_BYTES,
            "actual_bytes": len(body_bytes),
        })

    return json_response(200, doc)


async def handle_restore(
    stores_by_name: dict[str, object],
    principal,
    request,
    list_keys,
) -> Response:
    if not principal.has_permission("users.manage"):
        return json_response(403, {"error": "forbidden", "required_permission": "users.manage"})

    if len(request.body or b"") > MAX_BACKUP_BODY_BYTES:
        return json_response(413, {"error": "body_too_large", "max_bytes": MAX_BACKUP_BODY_BYTES})

    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return json_response(400, {"error": "invalid_json"})

    if payload.get("confirm") != RESTORE_CONFIRMATION:
        return json_response(400, {"error": "confirmation_required", "expected": RESTORE_CONFIRMATION})

    decoded_entries_by_store, error = validate_backup(payload.get("backup"))
    if error:
        return json_response(400, error)

    restored: dict[str, int] = {}
    pruned: dict[str, int] = {}

    for store_name in RESTORE_STORE_ORDER:
        if store_name not in decoded_entries_by_store:
            continue
        store = stores_by_name[store_name]
        entries = decoded_entries_by_store[store_name]

        # Write-then-prune, not wipe-then-write: an interrupted restore then
        # leaves a superset (the file's content plus leftovers), never an
        # empty store. Records before indexes within the store.
        #
        # Report only, deliberately NO retry here
        # (docs/plans/write-throttle-resilience.md) — a full-cap restore
        # (MAX_BACKUP_ENTRIES=5,000) is already documented as unable to
        # complete inside Akamai's 30-second handler limit (~100s of writes
        # at the 50/s cap), so adding retry sleeps makes an already-doomed
        # request slower for no gain. Restore replaces rather than merges and
        # is therefore idempotent, so "run it again" is a genuine next step
        # — hence `next_step: "retry_restore"` below rather than pointing at
        # the consistency repair tool. This is the clearest place in the app
        # where "retry" and "report" are independent decisions: this path
        # takes the second without the first, deliberately, and imports
        # nothing from kvretry.
        try:
            for key in restore_write_order(store_name, list(entries.keys())):
                await store.set(key, entries[key])
            restored[store_name] = len(entries)

            existing_keys = await list_keys(store)
            stale_keys = [key for key in existing_keys if key not in entries]
            for key in stale_keys:
                await store.delete(key)
            pruned[store_name] = len(stale_keys)
        except Exception as exc:
            # Same "throttled" vs "other" label kvretry.classify_write_error
            # renders elsewhere, inlined rather than imported: this path
            # takes no dependency on the retry seam at all, by design.
            write_error = "throttled" if "too many requests" in str(exc).lower() else "other"
            return json_response(200, {
                "ok": False,
                "partial": True,
                "restored": restored,
                "pruned": pruned,
                "stopped_at_store": store_name,
                "write_error": write_error,
                "next_step": "retry_restore",
            })

    signed_out = "users" in decoded_entries_by_store

    return json_response(200, {
        "ok": True,
        "restored": restored,
        "pruned": pruned,
        "signed_out": signed_out,
        "next_step": "bootstrap_admin",
    })
