# KV Backup and Restore

## Context

Every byte of this app's state lives in three Spin key-value stores — `links`,
`users`, `analytics` — and there is currently **no way to get any of it out and
no way to put it back**. The only extraction path that exists is
`gui/dashboard.js`'s CSV export, which covers eight display columns of the links
a viewer can see and nothing else: no `password_hash`, no indexes, no user
records, no analytics. There is no import path of any kind.

Three things make that inadequate today:

- **Local KV appears to be non-persistent.** `CLAUDE.md`'s `kv-explorer` bullet
  records the finding: no `.db` file exists in the repo, `~/Library/Caches/spin`
  or `~/Library/Application Support/spin`, and the explorer only ever shows the
  current `spin up` session's data. Every restart of local dev loses everything,
  including hand-built test fixtures.
- **The KV explorer has full, undoable CRUD** over `links` and `analytics`
  (`CLAUDE.md`, same bullet: "It can overwrite and delete any key with no undo…
  a stray click destroys local data").
- **The Akamai consolidation is a scheduled, moderately invasive data migration**
  (`TASKS.md` Future work; `CLAUDE.md`'s "Deployment: known Akamai Functions
  blocker") that rewrites every key literal in both components. Doing that
  without a way to snapshot and reload the data first is worse than doing it with
  one.

There is **no existing `TASKS.md` Future-work entry for this feature** —
confirmed by grepping the `## Future work (not scheduled)` section for
backup/restore/export. This is a fresh request, so the constraints below come
from the requester, not from prior reasoning in the repo.

**Confirmed decisions:** (settled by the user before planning; not re-litigated
here, only recorded so a future reader knows they were deliberate)

- Browser **download and upload only**. No S3, no cloud storage, at all. Nothing
  in this plan adds an `allowed_outbound_hosts` entry to any component.
- The backup file contains **no credential material whatsoever**. User records
  are included — `username`, `role`, `permissions`, `assigned_domains`,
  `disabled`, `created_at`, `provider` — but `password_hash` is stripped,
  `session:*` keys are excluded entirely, and `_meta:bootstrapped` is excluded.
- **Recovery path**: because `_meta:bootstrapped` is absent, `ensure_bootstrap_admin`
  re-seeds the admin from `SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD`; that admin
  then resets everyone's password through the existing
  `PATCH /api/users/{username}` flow. Walked through in full below — it is the
  entire reason stripping hashes is viable.
- **Restore replaces** the stores it covers. Not a merge.
- **Users are restored last**, and the operator is told plainly they will be
  logged out.
- **A restored account with no password hash can never authenticate**, defined
  explicitly rather than left to whatever `verify_password` happens to do, and
  the admin users table **flags** those accounts.

## Key technical facts confirmed during research

- **`get_keys` IS reachable from the Python SDK — confirmed from source.**
  `api/.venv/.../spin_sdk/key_value.py:5` is literally `Store = kv.Store`, where
  `kv` is `spin_sdk.wit.imports.spin_key_value_key_value_3_0_0`. That module's
  `Store` class declares, at line 91:

  ```python
  async def get_keys(self) -> Tuple[StreamReader[str], FutureReader[Result[None, Error]]]:
  ```

  So the object returned by `await key_value.open("links")` does expose
  `get_keys()`. The friendly wrapper module only re-exports `open`/`open_default`
  as *functions*; it does not wrap or restrict the `Store` class at all. The
  underlying WIT is
  `spin_sdk/wit/deps/spin-key-value@3.0.0/key-value.wit:30`:
  `get-keys: async func() -> tuple<stream<string>, future<result<_, error>>>;`

- **UNCONFIRMED: whether the Spin 4.0.2 host actually implements `get-keys` for
  the local sqlite-backed `type = "spin"` provider over that async 3.0.0
  interface, from a componentize-py 0.23.0 guest.** The type exists; that a call
  succeeds at runtime is a different claim. Supporting but *not* sufficient
  evidence: the Fermyon KV explorer lists real keys from `links`/`analytics`
  under local `spin up` (`CLAUDE.md`, kv-explorer bullet, "Confirmed live") —
  but it is a prebuilt third-party binary compiled against a different WIT
  version (`spin@2.0.0`'s `get-keys: func() -> result<list<string>, error>`,
  synchronous and list-shaped, also present in this venv at
  `spin_sdk/wit/imports/fermyon_spin_key_value_2_0_0.py:91`), so it proves the
  *host capability* exists, not that this guest's async-stream binding works.
  **Settling this is Task 1**, and the answer changes what the feature is
  allowed to call itself — see "The fidelity question" below.

- **UNCONFIRMED: the exact idiom for draining the returned stream.**
  `componentize_py_async_support/streams.py:108-117` shows `StreamReader.read(max_count)`
  returns `[]` once `writer_dropped` is set, and `futures.py:17` shows
  `FutureReader.read()` is a one-shot await. The drain in "API changes" below is
  written from those signatures, not from a run. **Task 1 must record the idiom
  that actually worked and the builder must use that, not the snippet here.**

- **Both request and response bodies are fully buffered in Wasm linear memory.**
  `spin_sdk/http/__init__.py:75-78` accumulates the whole request body into a
  `bytearray` before `handle_request` is called, and `responses.Response.body` is
  a single `bytes`. This is the same constraint that produced `bulk.py`'s
  `MAX_BULK_BODY_BYTES`, and it applies in *both* directions here.

- **`ensure_bootstrap_admin` runs on every `/api/...` request, not only at
  startup.** `api/app.py:38-41` opens the `users` store and calls it before any
  routing decision. `auth.py:146-147` returns early **only** if
  `_meta:bootstrapped` exists. So after a restore that removes that marker, the
  admin is re-seeded by the very next API request — including the login POST
  itself — with **no process restart required**. This is a correction to the
  brief's "next startup" framing and it makes the recovery path materially
  better than assumed.

- **`ensure_bootstrap_admin` overwrites, it does not merge.** `auth.py:150-161`
  builds a fresh dict and `put_user`s it. If the restored user set contains a
  user named `admin` (the `admin_bootstrap_username` default), that record's
  `permissions`, `assigned_domains`, `disabled` and `created_at` are silently
  replaced. The result is strictly *more* privileged (`role: "admin"`,
  unrestricted domains, not disabled), so it is not a lockout — but it is real,
  documented data loss on exactly one record and must be disclosed.

- **`LocalAuthProvider.authenticate` does an unguarded `user["password_hash"]`**
  (`auth.py:136`). A restored, hash-less user record therefore raises `KeyError`,
  which is swallowed by `spin_sdk/http/__init__.py:92`'s bare `except:` and
  returned as a **500**, not a 401. Confirmed by reading both files. This is the
  bug behind the "define it explicitly" constraint.

- **`users.manage` is already equivalent to the `admin` role, by escalation.**
  `users.handle_update` (`api/users.py:121-170`) blocks only
  `cannot_disable_self`; nothing forbids a `users.manage` holder from PATCHing
  *their own* record to `{"role": "admin"}`. So gating these endpoints on
  `users.manage` is not a weaker bar than gating on `role == "admin"` — the two
  describe the same set of principals today. Confirmed by reading the handler;
  there is no self-promotion guard anywhere in the file.

- **`_public_user` is allowlist-by-exclusion** (`api/users.py:17-18`): every key
  except `password_hash`. Adding a derived `password_set` boolean to its return
  is a one-line change and appears in `GET /api/users` automatically, exactly the
  way `links.public_link`'s `password_protected` already works (`links.py:113-117`).

- **KV keys are `string`, values are `list<u8>`.** From the WIT
  (`spin-key-value@3.0.0/key-value.wit`): `get: func(key: string) -> ... option<list<u8>>`.
  So keys never need an encoding decision; values do, and must survive
  non-UTF-8 bytes.

- **The Blob-download and FileReader-upload precedents both exist and both
  work.** Download: `gui/dashboard.js:743-751` (`new Blob` → `URL.createObjectURL`
  → synthetic `<a download>` → `revokeObjectURL`). Upload:
  `gui/dashboard.js:516-535` (`<input type="file">` change handler, a client-side
  size pre-check against a mirrored constant, `FileReader.readAsText`, then
  clearing `fileInput.value`). No multipart parsing reaches a Wasm component in
  either direction.

- **`gui-pages/tests/test_no_inline_code.py` auto-covers new files.** `PAGES` is
  derived from `routing.ROUTES` (line 25) and `SCRIPTS` is a `rglob("*.js")` over
  `gui/` minus `vendor/` (lines 41-45). A new `admin/backup.html` added to
  `ROUTES` picks up 4 tests and a new `gui/admin/backup.js` picks up 2, with no
  edit to that file. `test_routing.py:6-26`'s `test_resolve_file` parametrize
  list is hardcoded and **does** need one new case.

- **`gui-pages/tests/test_manifest_components.py` does not constrain routes** —
  it asserts the component *set* is `{redirect, api, gui, gui-pages}` and checks
  the kv-explorer fragment. Adding a `[[trigger.http]]` route on the existing
  `gui` component does not touch it.

- **Baseline test counts, run at `43071e6`:** `cd api && uv run pytest` → **227
  passed**; `cd gui-pages && uv run pytest` → **57 passed**;
  `cd redirect && go test ./linkgate/...` → **ok**. Nothing in this plan touches
  `redirect/`, so the Go suite is unchanged and is listed in Verification only as
  a no-regression check.

- **`spin --version` → `spin 4.0.2 (bfc7543 2026-06-23)`**; `api/pyproject.toml`
  pins `spin-sdk==4.0.0` and `componentize-py==0.23.0`.

## The fidelity question, and what the feature may call itself

Everything downstream of Task 1 forks on one answer.

**If `get_keys` works at runtime (expected):** the export enumerates *every* key
in each covered store. That is a true full-fidelity backup, and the feature may
be called **"Backup"** in the UI and the docs. `fidelity: "full"` goes in the
file.

**If it does not:** the export can only walk the app's own indexes —
`all_links` → `slug:<slug>`, `_meta:usernames` → `user:<username>`,
`owner_links:<username>`, and `count:<slug>` / `events:<slug>:<slot>` derived
from the slug list and `analytics_event_slots`. That **misses any key the
indexes do not know about, which is precisely the orphaned data you would care
about after corruption** — an unindexed `slug:X` (the documented outcome of an
interrupted bulk create, `CLAUDE.md`'s "Bulk link management") would be silently
absent from its own "backup". In that case the feature is a **logical export**,
must be labelled "Export" everywhere in the UI and the docs, must never use the
word "backup", and `fidelity: "index-walk"` goes in the file with an
`incomplete: true` sibling flag.

Both copies are written out below so the builder does not have to improvise
after the spike.

## Data model: the backup file

One JSON document. Top level:

```json
{
  "format": "spin-shortener-kv-backup",
  "schema_version": 1,
  "created_at": "2026-08-02T14:33:01Z",
  "created_by": "admin",
  "fidelity": "full",
  "key_encoding": "utf8",
  "value_encoding": "base64",
  "excluded": ["users/_meta:bootstrapped", "users/session:*", "users/user:*#password_hash"],
  "counts": {"links": 12, "users": 3, "analytics": 25},
  "stores": {
    "links":     {"all_links": "WyJhYmMiXQ==", "slug:abc": "eyJzbHVnIjogImFiYyJ9"},
    "users":     {"_meta:usernames": "WyJhZG1pbiJd", "user:admin": "eyJ1c2VybmFtZSI6ICJhZG1pbiJ9"},
    "analytics": {"count:abc": "eyJ0b3RhbCI6IDF9"}
  }
}
```

Field-by-field rationale:

- **`format`** — a fixed magic string. The single cheapest way for a restore to
  refuse a file that is not one of ours (a CSV export, a `package.json`, an
  unrelated backup) before it reads anything else.
- **`schema_version`** — integer, currently `1`. A restore accepts **only** the
  versions it knows and returns `400 {"error": "unsupported_schema_version",
  "schema_version": n, "supported_versions": [1]}` otherwise. Refusing forward is
  the point: a v2 file with a field this code ignores would restore *silently
  wrong*.
- **`created_at`** — `responses.iso_now()`. Same format as every other timestamp
  in the app, so the file sorts and reads like the rest of the system.
- **`created_by`** — `principal.username`, for provenance in the restore
  preview.
- **`fidelity`** — `"full"` or `"index-walk"` (see above). A `"index-walk"` file
  also carries `"incomplete": true`.
- **`key_encoding: "utf8"`** — declared, not applied. KV keys are `string` in the
  WIT, so they are already JSON object keys verbatim. Stating it means a future
  format change to, say, base64 keys is detectable rather than ambiguous.
- **`value_encoding: "base64"`** — **every value is base64, uniformly.** Values
  are `list<u8>` and can be arbitrary bytes; a per-value "JSON if it parses, else
  base64" discriminator would double the validation matrix and the test matrix to
  save ~33% of file size on a file that is already capped. Keys stay plaintext,
  which is enough for a human to eyeball what a file contains without decoding
  it. **Round-tripping a non-UTF-8 value is a required test** (e.g. `b"\xff\xfe\x00"`).
- **`excluded`** — a literal, human-readable statement of what was deliberately
  left out. This is documentation-in-the-artifact: the file's own text says it
  contains no password hashes, so an operator who finds one on disk in six months
  does not have to guess.
- **`counts`** — per-store entry counts, so the GUI's pre-restore preview can be
  rendered without walking `stores`, and so a truncated file is obvious.
- **`stores`** — only the stores actually covered. A partial export
  (`?stores=links,users`) omits the `analytics` key entirely, and a restore then
  leaves the `analytics` store completely untouched.

### Caps

Both plain module constants in `api/backup.py`, following `bulk.py`'s precedent
(`MAX_BULK_ROWS`/`MAX_BULK_BODY_BYTES` are module constants, not Spin variables,
"because the cap is read by exactly one function in one component and expresses a
safety rail tied to what a single componentize-py request can do"):

```python
MAX_BACKUP_BODY_BYTES = 5_242_880   # 5 MiB
MAX_BACKUP_ENTRIES = 5_000
```

- **Enforced on restore, before parsing:** `len(request.body or b"") > MAX_BACKUP_BODY_BYTES`
  → `413 {"error": "body_too_large", "max_bytes": MAX_BACKUP_BODY_BYTES}`, byte-for-byte
  the shape `bulk.handle_bulk_create` already returns (`bulk.py:155-156`).
- **Enforced on export, after serializing:** if the encoded response body exceeds
  `MAX_BACKUP_BODY_BYTES`, return
  `500 {"error": "backup_too_large", "max_bytes": ..., "actual_bytes": ...}`
  rather than emitting a file that can never be restored. **Symmetric caps are
  the whole point** — an export you cannot restore is worse than a refusal,
  because it looks like success.
- **`MAX_BACKUP_ENTRIES` is the second rail, on key count not bytes**, checked on
  both sides. Restore does roughly one KV `set` per entry plus one `delete` per
  stale key; 5,000 entries is already ~10,000 KV operations in a single request.
  → `400 {"error": "too_many_entries", "max_entries": ..., "entry_count": ...}`.
- **Both numbers are starting points, not measurements.** Raising either needs
  real timing evidence from a full-cap run, exactly the rule `CLAUDE.md` already
  states for `MAX_BULK_ROWS`. Verification step 9 below is that measurement.
- **Both are echoed in every error body**, so the GUI never hardcodes a limit it
  can drift from. `gui/admin/backup.js` mirrors `MAX_BACKUP_BODY_BYTES` only for
  the pre-upload file-size check, with the same "the server is authoritative"
  comment `dashboard.js:472-477` already carries.

**The analytics store is what will actually hit these caps.** A deployment with
N links holds ~N `count:` keys plus up to `N × analytics_event_slots` (default
30) `events:` keys. At 150 links that is ~4,650 analytics entries against a
handful of link and user entries. This is exactly why export takes a `?stores=`
parameter: `?stores=links,users` produces a small, always-viable backup of the
authoring data, and the operator gives up only the best-effort, already-lossy
recent-events sample (`CLAUDE.md`, "Analytics": "Treat 'recent events' as a
best-effort sample, never a complete log").

### Consistency: a fuzzy read, and that is fine

Spin's KV interface has **no transactions, no snapshots and no compare-and-swap**
(`CLAUDE.md`, "Security tradeoffs" — the same fact that rules out a KV rate
limiter). An export is therefore N independent reads over a live store, and a
write that lands halfway through produces a file mixing pre- and post-write
values.

For this app that is acceptable and needs no mitigation, for a concrete reason:
**the only concurrent writer during normal operation is `redirect`'s analytics
write.** Links and users change only on a deliberate operator action. So the
realistic worst case is a click total off by the number of clicks that landed
during the export — seconds of traffic on a store whose event log is already
documented as lossy.

The one structural case worth naming: `all_links` captured before a concurrent
create writes `slug:X` yields an index missing a record — which
`links.handle_list` already tolerates (`links.py:203-206` skips any slug whose
record is `None`). The reverse — a record captured with no index entry — is the
recoverable direction `bulk.py`'s write ordering deliberately chooses. Both are
index-vs-record drift the app already handles by design.

**No locking, no read-repeat, no quiesce mode.** Stated explicitly so nobody adds
one later thinking it was overlooked.

### Indexes

`all_links`, `owner_links:<owner>` and `_meta:usernames` are ordinary keys. They
are captured and restored like any other, so index/record consistency falls out
of "replace the whole store" for free — there is nothing to rebuild.

Two rules make that robust rather than merely true:

1. **Within each store, records are written before index keys** — the same one
   rule `bulk.py` follows in both directions (`CLAUDE.md`, "Bulk link
   management": "Write ordering: records first, indexes last, in both
   directions"). An interrupted restore then leaves records with no index entry
   (resolvable at `/r/<slug>`, invisible in the dashboard, recoverable) rather
   than index entries advertising slugs that 404.
2. **Restore does not repair or rebuild indexes.** If the backup was taken
   mid-write and is internally inconsistent, restoring it reproduces exactly that
   inconsistency — no better, no worse. A repair pass would have to decide
   whether an unindexed record is orphaned junk or a link someone still wants,
   and getting that wrong deletes data. Out of scope; see follow-ups.

## API changes

### New module: `api/backup.py`

Zero `spin_sdk` imports. Takes `store` objects, `request`, and a `list_keys`
callable as plain parameters; imports `Request`/`Response` from `responses`.
Follows `bulk.py`'s shape exactly — pure helpers at the top, the two `handle_*`
coroutines at the bottom.

```python
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

# Index keys, written last within their store (see "Indexes" above).
INDEX_KEYS = {
    "links": ("all_links",),          # plus every "owner_links:" prefixed key
    "users": ("_meta:usernames",),
    "analytics": (),
}
OWNER_LINKS_PREFIX = "owner_links:"
SESSION_PREFIX = "session:"
USER_PREFIX = "user:"
BOOTSTRAPPED_KEY = "_meta:bootstrapped"   # == auth.BOOTSTRAPPED_KEY
```

Pure functions (all unit-tested directly):

```python
def parse_stores_param(raw: str | None) -> tuple[list[str] | None, dict | None]:
    """(stores, error_body). None/absent -> all of BACKUP_STORES, in that order.
    Allowlist-validated against BACKUP_STORES, never trusted directly — the same
    rule qr.handle_qr applies to ?base= (see CLAUDE.md, "Multi-domain display")."""

def redact_user_value(raw: bytes) -> bytes:
    """A user record with password_hash removed. A value that is not JSON, or
    not a JSON object, passes through unchanged rather than raising — the
    exporter must never fail on an unexpected value shape."""

def is_excluded_key(store: str, key: str) -> bool:
    """True for the users store's BOOTSTRAPPED_KEY and any SESSION_PREFIX key.
    False for everything else, in every store."""

def build_backup(
    entries_by_store: dict[str, dict[str, bytes]],
    *,
    created_at: str,
    created_by: str,
    fidelity: str,
) -> dict:
    """Applies is_excluded_key, then redact_user_value to every USER_PREFIX key
    in the users store, then base64-encodes every value. Returns the document
    described in "Data model" above. Pure — no I/O, no clock, no store."""

def validate_backup(payload) -> tuple[dict[str, dict[str, bytes]] | None, dict | None]:
    """All-or-nothing. Returns (decoded_entries_by_store, None) only if EVERY
    check passes; otherwise (None, error_body). Nothing partially decoded is
    ever returned, so a caller cannot accidentally write from a bad file."""

def restore_write_order(store: str, keys: list[str]) -> list[str]:
    """Non-index keys first (in the file's own order), index keys last."""
```

`validate_backup`'s refusal list, in order — every one returns a `400` with the
named `error` code and nothing is written:

| Condition | `error` | Extra fields |
|---|---|---|
| not a JSON object | `invalid_backup` | |
| `format` != `BACKUP_FORMAT` | `invalid_backup_format` | `expected` |
| `schema_version` not in `SUPPORTED_SCHEMA_VERSIONS` | `unsupported_schema_version` | `schema_version`, `supported_versions` |
| `stores` missing or not an object | `invalid_backup` | |
| `stores` is empty | `no_stores` | |
| a store name outside `BACKUP_STORES` | `unknown_store` | `store`, `allowed_stores` |
| a store's entries is not an object of str→str | `invalid_entries` | `store` |
| total entry count > `MAX_BACKUP_ENTRIES` | `too_many_entries` | `max_entries`, `entry_count` |
| a value is not valid base64 | `invalid_value_encoding` | `store`, `key` |
| a `users` key is `_meta:bootstrapped` or `session:*` | `forbidden_key` | `store`, `key` |
| a `user:*` value decodes to JSON containing `password_hash` | `credential_material_in_backup` | `key` |

The last two are **security checks, not tidiness**. A file containing a
`session:` key would let an uploaded document forge a live session for any
username; a file containing a `password_hash` is either not one of ours or has
been tampered with. Rejecting rather than silently stripping is deliberate:
silently accepting would teach operators that these files may legitimately carry
credentials, which is exactly the belief this design exists to prevent.

`base64.b64decode(value, validate=True)` — `validate=True` matters; without it
Python discards non-alphabet characters instead of raising, so a corrupted value
would decode to plausible-looking garbage. This mirrors `auth.verify_password`'s
existing `b64decode(..., validate=True)` usage (`auth.py:65-66`).

### `GET /api/admin/backup`

```python
async def handle_export(
    stores_by_name: dict[str, object],   # {"links": store, "users": store, "analytics": store}
    principal,
    query: dict[str, list[str]],
    list_keys,                            # async (store) -> list[str], or None
    num_event_slots: int,
) -> Response
```

- `if not principal.has_permission("users.manage"): return _forbidden()` — the
  identical body `users.py:21-22` returns (`403 {"error": "forbidden",
  "required_permission": "users.manage"}`). See "Who can do it" below for why
  this is the right bar and not a weaker one than `role == "admin"`.
- `parse_stores_param(query.get("stores", [None])[0])`.
- For each selected store: `keys = await list_keys(store)` (full fidelity) or
  `await index_walk_keys(...)` (fallback, only if Task 1 fails), then
  `await store.get(key)` per key.
- `build_backup(...)` with `created_at=iso_now()`, `created_by=principal.username`.
- `json_response(200, doc)` — **no `content-disposition` header.** The GUI
  downloads via `Blob` + object URL, so the browser never treats this response as
  a navigation and the header would be dead weight. The filename is chosen
  client-side.
- Size check on the encoded body before returning; `500 backup_too_large` if over.

### `POST /api/admin/restore`

Body: `{"confirm": "REPLACE", "backup": { ...the file... }}`

```python
async def handle_restore(
    stores_by_name: dict[str, object],
    principal,
    request,
    list_keys,
    num_event_slots: int,
) -> Response
```

Sequence — **validation completes entirely before the first write**:

1. `users.manage` gate.
2. Body size cap → `413 body_too_large`.
3. `json.loads` → `400 invalid_json` on failure (same shape as every other
   handler in `api/`).
4. `payload.get("confirm") != RESTORE_CONFIRMATION` →
   `400 {"error": "confirmation_required", "expected": "REPLACE"}`. A typed
   literal, server-validated. Restore is the single most destructive action in
   the application and this is what stops a stray click, a replayed request, or a
   mis-wired button from wiping three stores. See trade-offs for why this rather
   than re-entering the operator's password.
5. `validate_backup(payload.get("backup"))` → any error short-circuits with
   **nothing written**.
6. Writes, in `RESTORE_STORE_ORDER`, skipping any store not present in the file:
   - For each store: **write every entry from the file first**, in
     `restore_write_order` (records, then indexes), **then** delete every
     pre-existing key in that store that is not in the file.
     Write-then-prune, not wipe-then-write: an interrupted restore then leaves a
     superset (the file's content plus leftovers), never an empty store.
   - **`users` last**, and its prune pass deletes every `session:*` key and
     `_meta:bootstrapped`, both of which are guaranteed absent from the file.
     Deleting the sessions is not incidental — a surviving session would keep
     authenticating against a user set that has just been wholly replaced,
     carrying the old permissions.
7. `200 {"ok": true, "restored": {"links": n, ...}, "pruned": {...},
   "signed_out": true, "next_step": "bootstrap_admin"}`.
   `signed_out` is `true` only when the file contained the `users` store — a
   links-only restore does not touch sessions and does not log anyone out. The
   GUI keys its post-restore copy off this flag.

**In fallback (index-walk) mode there is no way to enumerate stale keys**, so the
prune pass instead deletes every key derivable from the store's *current*
indexes before writing — which covers everything the export could have captured
— and the response carries `"fidelity": "index-walk"`. Residue outside the
indexes survives. Say so in the UI; do not call that outcome "replaced".

### `api/app.py` wiring

Two exact-path branches, placed immediately after the `/api/users/` block (no
prefix collision with anything — `/api/admin/...` is a new namespace):

```python
if path == "/api/admin/backup" and method == "GET":
    result = await _require_session(users_store, request)
    if isinstance(result, Response):
        return result
    return await backup.handle_export(
        {"links": await key_value.open("links"), "users": users_store,
         "analytics": await key_value.open("analytics")},
        result, query, _kv_keys, int(await variables.get("analytics_event_slots")),
    )

if path == "/api/admin/restore" and method == "POST":
    ...same, calling backup.handle_restore(..., request, ...)
```

`_require_session` already applies `check_csrf` for POST and is a no-op for GET
(`auth.py:223-227`).

The one piece of genuinely untestable plumbing, which belongs in `app.py`
alongside the other `spin_sdk` I/O and nowhere else — **UNCONFIRMED, replace with
whatever Task 1 proves**:

```python
async def _kv_keys(store) -> list[str]:
    """Drain the (stream, future) pair spin:key-value/key-value@3.0.0's
    get-keys returns into a plain list. Isolated here so backup.py can take a
    list_keys callable as a parameter and stay host-importable, the same way
    gui-pages/routing.py takes read_file."""
    reader, completion = await store.get_keys()
    keys: list[str] = []
    with reader:
        while not reader.writer_dropped:
            keys.extend(await reader.read(1024))
    await completion.read()
    return keys
```

### `api/auth.py` — a hash-less account can never authenticate

Replace the unguarded subscript at `auth.py:136`:

```python
        stored_hash = user.get("password_hash")
        if not stored_hash:
            # A restored account carries no password hash by design (see
            # docs/plans/kv-backup-restore.md). Defined here as an explicit
            # "cannot authenticate" rather than left to verify_password: the
            # old user["password_hash"] raised KeyError, which the SDK's bare
            # except turned into a 500 instead of a 401.
            return None
        if not verify_password(password, stored_hash):
            return None
```

`""` and `None` both fall into the same branch, so the rule is "no *usable* hash",
not "key absent". `verify_password` itself is unchanged — its `except
(ValueError, AttributeError, binascii.Error)` already returns `False` for a
malformed string; the guard above is about the *contract*, stated at the one call
site that decides whether someone gets in.

**This is a real bug fix independent of the rest of the feature** and is
sequenced early so it can land on its own.

### `api/users.py` — surfacing it

`_public_user` gains one derived field, mirroring `links.public_link` exactly:

```python
def _public_user(user: dict) -> dict:
    public = {k: v for k, v in user.items() if k != "password_hash"}
    public["password_set"] = bool(user.get("password_hash"))
    return public
```

It appears in `GET /api/users`, `GET/POST/PATCH /api/users/{username}`
automatically. No new endpoint, no new permission.

## GUI changes

### New page: `gui/admin/backup.html`

**A new admin page, not a third `<article>` on `admin/users.html`.** The users
page is where an admin goes to reset one person's password or flip one
permission — routine, frequent work. Putting a control that replaces three KV
stores and ends the session on that page puts the most destructive action in the
application one mis-click away from the most routine one. `PRODUCT.md` principle
5 ("Keep admin visually and functionally distinct from everyday link-creation
workflows") points the same way. The cost is a new page, a new `spin.toml` route,
a new `ROUTES` entry and a nav re-measurement; that is the right price.

Structure, following `admin/users.html` line for line:

- `<script src="../theme-init.js">`, then `../vendor/pico.min.css`,
  `../theme.css`. **No `backup.css`** — the page needs no bespoke styles. If the
  builder finds it does, add `gui/admin/backup.css` *and* its exact `spin.toml`
  route in the same commit; a stylesheet without a route silently 404s
  (`spin.toml`'s own route-block comment).
- `<header id="app-header" class="container">`, `<main class="container">`,
  `<h1>Backup and restore</h1>` (or `Export`, per the fidelity outcome).
- `<p id="forbidden-notice" class="form-error" role="alert" hidden>` — the same
  element and the same copy pattern `admin/users.html:17-19` uses.
- `<div id="admin-content">` containing two `<article>`s:
  - **Download** — an explanatory `<p>` stating in plain words what the file does
    and does not contain ("Passwords, password hashes and sign-in sessions are
    never included."), a `<fieldset>` of three checkboxes (Links / Users /
    Analytics, all checked), a submit button, `#export-error` (`.form-error`),
    `#export-success` (`.form-success`).
  - **Restore** — a `<p class="form-error">` stating the consequences up front
    (replaces everything in the covered stores; no undo; you will be signed out),
    `<input type="file" id="restore-file" accept=".json,application/json">`, a
    `#restore-summary` block rendered from the parsed file *before* anything is
    sent, a `Type REPLACE to confirm` text input, a `Restore` button styled
    `outline secondary`, `#restore-error`, `#restore-result`.

Using `.form-error` for the standing warning rather than inventing a warning
class follows the existing precedent: `theme.css`'s `.expiring-soon`/`.expired`
already use `--ss-danger-500` for a warning that is not an error state. **No new
design token, so no live `getComputedStyle` measurement is required for the page
body** — only for the nav (below).

### `gui/admin/backup.js`

Reuses, without reimplementing: `api.get` / `api.post`, `escapeHtml`,
`friendlyError`, `confirmDialog`, `initHeader`, `setCsrfToken` — all from
`gui/app.js`.

- **Download**: `api.get("/admin/backup?stores=links,users,analytics")`, then the
  exact `Blob` → `createObjectURL` → `<a download>` → `revokeObjectURL` sequence
  from `dashboard.js:743-751`, with
  `JSON.stringify(data, null, 2)` as the blob content (`api.get` already parsed
  it; re-stringifying costs nothing and gives a diffable, human-inspectable file)
  and
  `spin-shortener-backup-${new Date().toISOString().slice(0, 10)}.json` as the
  filename, matching `dashboard.js:747`'s naming pattern.
- **Upload**: the `dashboard.js:516-535` shape verbatim — a size pre-check
  against a mirrored `BACKUP_MAX_BODY_BYTES = 5242880` (carrying the same "the
  server is authoritative; a drift here only ever produces a `body_too_large`
  naming the real limit" comment as `dashboard.js:472-477`), then
  `FileReader.readAsText`, then `fileInput.value = ""`.
- On load, `JSON.parse` the text client-side and render `#restore-summary` from
  `format` / `created_at` / `created_by` / `fidelity` / `counts` — **the single
  best safety affordance in this feature and it costs one function.** A parse
  failure or a wrong `format` is reported here, before any request is made.
- The Restore button calls `confirmDialog` with the count-bearing wording
  DESIGN.md's Bulk Action Bar entry established — e.g.
  `Replace 12 links, 3 users and 25 analytics records? This can't be undone.`
  with `confirmLabel: "Replace everything"` — and only then POSTs.
- A local `BACKUP_ERROR_MESSAGES` override map, following `dashboard.js`'s
  `BULK_ROW_MESSAGES` precedent (`dashboard.js:465-470`), so `app.js`'s shared
  `ERROR_MESSAGES` stays about codes more than one page uses. At minimum:
  `invalid_backup_format`, `unsupported_schema_version`, `forbidden_key`,
  `credential_material_in_backup`, `too_many_entries`, `body_too_large`,
  `confirmation_required`, `backup_too_large`.
- On success with `signed_out: true`: `setCsrfToken(null)`, render the restored
  counts, then a plain message — "You have been signed out. Sign in again with
  the bootstrap admin credentials." — plus an `<a href="/login.html"
  role="button">Go to sign in</a>`. **No auto-redirect and no timer**: the counts
  are the only confirmation the operator will ever get that the restore
  succeeded, and yanking the page away before they read them is exactly wrong.

### `gui/app.js` — the nav link

`initHeader`'s options gain `backupHref = "admin/backup.html"` and
`onBackupPage = false`, matching the existing `manageUsersHref` /
`onManageUsersPage` pair. A new `<li id="backup-link" hidden><a
href="${backupHref}">Backup</a></li>` sits immediately after
`#manage-users-link`, revealed inside the `/auth/me` success block under the
**same** `canManageUsers` condition already computed at `app.js:404`, and hidden
when `onBackupPage`. All four authenticated pages pass depth-appropriate values.

**Nav crowding is a live risk, not a hypothetical.** DESIGN.md records that
adding the theme control alone overflowed the nav at 480px and 390px and needed
the `@media (max-width: 480px)` wrap fallback, and that the domain selector was
re-measured at 1400/768/480/390 in both themes before it was accepted. A fifth
item must be measured the same way, with two domains configured (the worst case:
brand + breadcrumb + identity chip + Manage users + Backup + domain select +
theme group + Log out).

**Pre-approved fallback if it overflows at any breakpoint:** move the Backup
entry off the nav and onto `admin/users.html` as a plain in-body link
(`<a href="backup.html">Backup and restore</a>` under the Users heading). Do
**not** invent a nav overflow menu — that is a new navigation pattern for one
link, and DESIGN.md has no precedent for one. The page itself and its route stay
exactly as designed either way.

### `gui/admin/users.js` — flagging hash-less accounts

Now that `_public_user` returns `password_set`:

- In the Status cell, when `user.password_set === false`, render a **second**
  badge beside the existing active/disabled one:
  `<span class="status-badge status-disabled">no password</span>`.
  Reuses an existing, already-contrast-measured token
  (`--ss-danger-500`, DESIGN.md Status Badges: 5.2:1 / documented) — **no new
  colour, so no new live measurement.** Colour is not the only signal: the text
  reads "no password", per DESIGN.md's Status Badges rule.
- Above the table, a `<p id="password-reset-notice" class="form-error"
  role="alert" hidden>` shown when at least one user lacks a password:
  `N account(s) have no password and can't sign in — set one with Edit.`
  This is what turns "the data was restored" into "here is the work left to do".

### `gui-pages/routing.py` and `spin.toml`

- `ROUTES["/admin/backup.html"] = "admin/backup.html"`.
- `spin.toml` gains `[[trigger.http]] route = "/admin/backup.js"`, `component = "gui"`,
  in the existing "Per-page scripts and styles" block. **Exact route, never a
  wildcard** — the documented `spin_static_fs` gotcha. A missing route serves a
  fully-rendered page whose script silently 404s.
- `test_routing.py`'s `test_resolve_file` parametrize list gains one case. The
  four inline-code checks on the page and the two on the script are picked up
  automatically by `test_no_inline_code.py`'s derived `PAGES`/`SCRIPTS`.

## Who can do it

**Both endpoints gate on `users.manage`**, via
`principal.has_permission("users.manage")` (which is `True` for `role == "admin"`
by `Principal.has_permission`'s first clause), returning the identical `403` body
`users.py:_forbidden()` already produces.

The obvious objection — "restore is more destructive, it should require the
`admin` role" — does not survive contact with the code. **`users.manage` is
already equivalent to `admin` by escalation**: `users.handle_update` has no
self-promotion guard, so any `users.manage` holder can PATCH their own record to
`{"role": "admin"}` and come back as an admin. The two bars describe the same set
of principals today, so choosing `role == "admin"` would buy an appearance of
strictness and nothing else, while adding a check that contradicts the file's own
`_forbidden()` convention.

**Different bars for backup vs. restore were considered and rejected for the same
reason** — there is no third, higher tier to promote restore into. What restore
gets instead is the typed `REPLACE` confirmation, which raises the bar on the
*action* rather than on the *identity*. If a self-promotion guard is ever added
to `handle_update`, revisit this and require `role == "admin"` for restore
specifically.

A new `backup.manage` permission was rejected: `KNOWN_PERMISSIONS` is
deliberately "a small, fixed, hardcoded vocabulary" (`auth.py:26-29`), and adding
an entry that grants strictly less than an existing entry already implies is
vocabulary growth for zero access-control gain.

## The recovery walkthrough

This is why stripping hashes is viable, spelled out end to end. Each step cites
the code that makes it true.

1. **The backup file has no `password_hash` for anyone, no `session:*` keys, and
   no `_meta:bootstrapped`** — `build_backup` applies `is_excluded_key` and
   `redact_user_value`, and `validate_backup` refuses a file that smuggles any of
   them back in.
2. **The operator uploads it and confirms.** Restore writes `links`, then
   `analytics`, then `users`. The `users` prune pass deletes every `session:*`
   key and `_meta:bootstrapped`.
3. **Every session in the system, including the operator's, is now dead.** The
   next request the browser makes gets a `401`, and `apiFetch`'s existing handler
   (`app.js:33-36`) bounces it to `/login.html`. The restore response has already
   told the page this would happen via `signed_out: true`.
4. **No process restart is needed.** `api/app.py:38-41` calls
   `ensure_bootstrap_admin` on *every* `/api/...` request, before routing, and
   `auth.py:147` returns early only when `_meta:bootstrapped` exists — which it
   no longer does. So the login POST itself re-seeds the admin from
   `admin_bootstrap_username` / `SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD`, writes
   `user:<admin>`, adds it to `_meta:usernames`, re-sets `_meta:bootstrapped`,
   and *then* the login proceeds against the freshly written record. The operator
   signs in with the bootstrap credentials on the same running process.
5. **Every other restored account has no usable hash and cannot authenticate** —
   `LocalAuthProvider.authenticate` returns `None` (a clean `401
   invalid_credentials`), not a `KeyError`/`500`, because of the guard added
   above.
6. **The admin resets each one** via the existing, unchanged
   `PATCH /api/users/{username}` `{"password": "..."}` path
   (`users.py:163-167` → `auth.hash_password`), surfaced in the GUI by
   `admin/users.js`'s existing "New password (optional)" field in the edit row.
   The users table's new "no password" badges are the worklist.

**One disclosed corner:** if the restored user set contains a record named
`admin` (the `admin_bootstrap_username` default), step 4 **overwrites** it —
`ensure_bootstrap_admin` builds a fresh dict rather than merging
(`auth.py:150-161`), so that one record's `permissions`, `assigned_domains`,
`disabled` and `created_at` are lost. The result is strictly more privileged
(`role: "admin"`, unrestricted domains, enabled), so it is never a lockout, but
it is real data loss on exactly one record. Documented in `CLAUDE.md`, not fixed:
making the re-seed merge would mean deciding whose `role` wins between a restored
record and the bootstrap contract, and the bootstrap contract has to win or the
recovery path stops working.

## Trade-offs and rejected alternatives

### S3 / any cloud object storage — rejected (dropped by the user, and structurally impossible)

The attraction is obvious: a real backup lives somewhere other than the operator's
Downloads folder, it can be scheduled, and it survives the laptop. The user
dropped it explicitly, and the architecture agrees — **`api` has no
`allowed_outbound_hosts`, and Spin denies all outbound HTTP when the key is
omitted** (`CLAUDE.md`, "Security tradeoffs", confirmed). Adding one is not a
config tweak in this repo: that exact property is the *stated reason* there is no
brute-force rate limiting on login or link passwords, and `PRODUCT.md` principle
2 ("Self-hosting only pays for itself if it stays operationally simple — avoid
adding infrastructure … unless a real incident or requirement forces it") points
the same way. Punching an outbound hole for a convenience feature would spend the
component's most load-bearing security property on the least urgent requirement.
Revisit only if scheduled off-host backups become a stated requirement, and then
as a deliberate `allowed_outbound_hosts` decision with its own plan — not as a
quiet addition to this one.

### Including `password_hash` in the file — rejected

Attractive because it makes restore a genuine one-step recovery: everyone's
password still works and nobody has to be contacted. It loses on what the
artifact becomes. A PBKDF2 hash is offline-attackable at the attacker's leisure,
and this file's entire distribution model is "a JSON document in a browser's
Downloads folder, emailed around, dropped in a shared drive, committed to a repo
by mistake". The 100,000-iteration cost factor is `CLAUDE.md`'s explicitly-stated
*only* mitigation against password guessing — it slows an online attacker, and it
is exactly what an offline attacker with the file no longer has to care about at
online rates.

Stripping hashes also buys something concrete: **the file needs no encryption, no
key management, and no special handling**, which is why "no encryption" below is
a defensible non-goal rather than an omission. The cost — every user needs a
password reset after a restore — is bounded, visible (the users table flags
exactly who), and uses a flow that already exists and is already tested. If
hash-preserving restore is ever genuinely needed, that is a different feature
with encryption and key management attached, not a flag on this one.

### Encrypting the backup file — rejected

Once hashes are out, the file holds link destinations, owners, schedules, click
counts, and the username/role/permission list. Internal, not secret: any
`links.view_all` user can already CSV-export most of the link half from the
dashboard, and the user half is `users.manage`-gated exactly like this endpoint.
Encryption would need a key, which means key management, which this app has none
of — and componentize-py's WASI CPython is already missing
`hashlib.pbkdf2_hmac` (`auth.py:34-40`, confirmed by a build/run spike), which is
a strong signal not to bet a recovery feature on its crypto surface. **The file
holds no secrets by construction; that is the reason, and it is only true because
hashes are stripped.** If that ever changes, this decision must change with it.

### Merge semantics instead of replace — rejected

A merge ("write what's in the file, leave everything else") never leaves the
store empty and feels safer. It is not: after a corruption event the operator
cannot tell whether a record they see is restored-good or corrupt-surviving, and
the indexes end up a union of two eras — `all_links` from the file listing slugs
whose records were not in the file, plus surviving slugs absent from the restored
index. That is precisely the index/record drift the write-ordering rule exists to
avoid, manufactured deliberately. Replace gives one comprehensible outcome: after
a successful restore, the covered stores are exactly the file. The interruption
risk is handled by write-then-prune ordering, not by weakening the semantics.

### Wipe-the-store-then-write, instead of write-then-prune — rejected

Simpler to write and to reason about, and it is the naive reading of "replace".
It loses on the interruption case: a failure between the wipe and the writes
leaves an **empty** store, which is strictly worse than the pre-restore state and
worse than any partial outcome. Write-then-prune leaves a superset on failure —
the file's content plus leftovers — which is recoverable by re-running the
restore. Same end state on success, strictly better failure mode.

### Restoring `users` first, or in `BACKUP_STORES` order — rejected

Restoring users first means the session dies before `links` and `analytics` are
written, so a failure on the second or third store leaves the operator locked out
*and* half-restored, with a bootstrap-admin round trip required just to retry.
Users last means every failure before the final store leaves the session intact
and the retry cheap. This is the user's constraint 5 and it is correct.

### Requiring the operator's password instead of a typed `REPLACE` — rejected

A password re-prompt is the stronger anti-mistake control and has real precedent
in other admin tools. It loses on cost and on fit: it needs a second PBKDF2
verification on the request path, a password field in a form whose whole purpose
is "do not do this by accident", and a new failure mode (wrong password on the
one action you take when things are already broken). The typed literal is
server-validated, costs one string comparison, cannot be triggered by a stray
click or a replayed request, and is testable in `pytest` with no crypto. The
client-side `confirmDialog` on top of it matches the count-bearing pattern
DESIGN.md's Bulk Action Bar entry already established.

### Putting the UI on `admin/users.html` — rejected, and it was close

Genuinely cheaper: no new page, no new `spin.toml` route, no `ROUTES` entry, no
nav item, and — the real prize — **no nav overflow re-measurement**, which
DESIGN.md shows has bitten this nav twice already. It loses on placement. The
users page is routine, frequent work (reset one password, flip one permission),
and this is the one action in the application that replaces three KV stores with
no undo and ends the session. Putting them on one page puts the most destructive
control one mis-click from the most routine one, and `PRODUCT.md` principle 5
asks for admin surfaces to stay distinct. Recorded rather than silently dropped
because if the nav measurement fails, this is the closest neighbour to the
pre-approved fallback (link from the users page, page stays separate).

### A per-value "JSON or base64" encoding discriminator — rejected

Would make most of the file human-readable, since nearly every value in these
stores is already JSON. It costs a discriminator field per entry, doubles the
encode/decode branches and the test matrix, and creates a genuine ambiguity — a
value that *is* valid JSON but was written as opaque bytes round-trips through a
different path than it arrived on. Uniform base64 costs ~33% file size on a
capped file and keeps exactly one code path. Keys stay plaintext, which is enough
to eyeball a file's contents without decoding it.

### Restore rebuilding or repairing the indexes — rejected

Attractive because it would turn restore into a repair tool for exactly the
corruption scenario that motivates backups. It requires deciding whether an
unindexed `slug:X` is orphaned junk to drop or a live link to re-index, and
whether an `all_links` entry with no record is a dangling reference to remove or
a record that failed to write. Getting either wrong deletes data, silently, in
the one operation an operator runs when they are already in trouble. Restore
reproduces the file faithfully; a separate consistency-check endpoint is the
honest shape for repair. See follow-ups.

### Scheduled or automatic backups — rejected

There is no scheduler in this app and no host-side cron in the target deployment,
and a component with no outbound access cannot push a file anywhere. Operator-invoked
only.

### Doing nothing — rejected, but it was live

Nothing in the app is currently broken by the absence of backup/restore, and the
CSV export covers the one thing anyone has actually asked to extract. What tips
it: the Akamai key-prefix consolidation is a scheduled data migration across
every key literal in two components, local KV appears to be non-persistent, and
the KV explorer has full undoable-free CRUD over two of the three stores. Doing
the migration with no snapshot mechanism is the specific scenario this feature
exists to make survivable.

## Tasks

The lines below were appended verbatim to `TASKS.md` under
`## KV backup and restore`. `TASKS.md` is authoritative; do not track checkbox
state here.

- [ ] Spike: settle whether `store.get_keys()` works at runtime (blocks every other task in this section) — file(s): (none — verification spike; all scratch code reverted before the task is ticked) — done when: a temporary probe added to `api/app.py` and exercised under a real `spin up --build --runtime-config-file runtime-config.toml` records, in `docs/plans/kv-backup-restore-scratch.md`, either (a) the exact working drain idiom for the `(StreamReader[str], FutureReader[...])` pair returned by `await (await key_value.open("links")).get_keys()` plus the real key list it produced for a store with at least 3 known keys, or (b) the exact exception/trap it raised; the scratch note states plainly which of `fidelity: "full"` or `fidelity: "index-walk"` the feature is therefore built as, and whether the UI copy says "Backup" or "Export"; and `git status` shows `api/app.py` unmodified.
- [ ] Make a user record with no usable password hash un-authenticatable instead of a 500 (independent of the rest of this section; can land first) — file(s): api/auth.py, api/tests/test_auth.py — done when: `LocalAuthProvider.authenticate` reads `user.get("password_hash")` and returns `None` when it is absent, `None` or `""` before ever calling `verify_password`; `cd api && uv run pytest` passes with three new tests — a user dict with no `password_hash` key, one with `"password_hash": None`, and one with `"password_hash": ""` — each asserting `authenticate` returns `None` and raises nothing.
- [ ] Add `password_set` to the public user shape — file(s): api/users.py, api/tests/test_users.py — done when: `_public_user` returns `password_set: bool(user.get("password_hash"))` alongside the existing keys and still omits `password_hash`; `cd api && uv run pytest` passes with new tests that a hashed user serializes `password_set: true`, a hash-less user serializes `password_set: false`, and no response body anywhere contains a `password_hash` key.
- [ ] Add api/backup.py with the pure format, redaction and validation helpers (depends on the get_keys spike for the `fidelity` value only) — file(s): api/backup.py (new), api/tests/test_backup.py (new), api/tests/fakes.py — done when: `BACKUP_FORMAT`, `SCHEMA_VERSION`, `BACKUP_STORES`, `RESTORE_STORE_ORDER`, `MAX_BACKUP_BODY_BYTES`, `MAX_BACKUP_ENTRIES` and `RESTORE_CONFIRMATION` exist as module constants with the values in docs/plans/kv-backup-restore.md, and `parse_stores_param`, `redact_user_value`, `is_excluded_key`, `build_backup`, `validate_backup` and `restore_write_order` exist with the signatures given there; the module has zero `spin_sdk` imports and imports `Request`/`Response` from `responses`; `fakes.py` gains a `FakeStore.keys()` method and a module-level `async def fake_list_keys(store)`; and `cd api && uv run pytest` passes with new tests covering — a `password_hash` stripped from a `user:` value while every other field survives; a non-JSON user value passing through `redact_user_value` unchanged; `_meta:bootstrapped` and `session:*` excluded from the users store but a key named `session:x` in the *links* store retained; **a non-UTF-8 value (`b"\xff\xfe\x00"`) surviving a full `build_backup` → `validate_backup` round trip byte-identical**; and every one of the eleven refusal rows in the plan's table returning its exact `error` code with `validate_backup`'s first return value `None`.
- [ ] Add GET /api/admin/backup (depends on the spike and api/backup.py) — file(s): api/backup.py, api/app.py, api/tests/test_backup.py — done when: `handle_export` gates on `users.manage` returning `users.py`'s exact `_forbidden()` body, accepts `?stores=` allowlist-validated against `BACKUP_STORES` (absent = all; an unknown name = `400 {"error": "unknown_store", "store": ..., "allowed_stores": [...]}`; empty = `400 no_stores`), returns `500 {"error": "backup_too_large", "max_bytes": ..., "actual_bytes": ...}` when the encoded body exceeds the cap, and otherwise `200` with the document shape in the plan; `app.py` routes the exact path `/api/admin/backup` on GET behind `_require_session`, opens all three stores, and passes the `_kv_keys` drain helper recorded by the spike; `cd api && uv run pytest` passes with new FakeStore-backed tests for the permission gate, each `?stores=` case, and a full three-store export whose `counts` match its `stores`; and `curl -b <session cookie> 'http://localhost:3000/api/admin/backup?stores=links,users'` against a live `spin up` returns a document with exactly those two store keys and no `password_hash` anywhere in the decoded output.
- [ ] Add POST /api/admin/restore (depends on GET /api/admin/backup) — file(s): api/backup.py, api/app.py, api/tests/test_backup.py — done when: `handle_restore` gates on `users.manage`, rejects a body over `MAX_BACKUP_BODY_BYTES` with `413 body_too_large`, requires `{"confirm": "REPLACE"}` (`400 confirmation_required` otherwise), runs `validate_backup` to completion before any write, then writes stores in `RESTORE_STORE_ORDER` (`links`, `analytics`, `users`) with non-index keys before index keys within each store and a prune pass after each store's writes, deletes every `session:*` key and `_meta:bootstrapped` during the users prune, and returns `200 {"ok": true, "restored": {...}, "pruned": {...}, "signed_out": <users store present>, "next_step": "bootstrap_admin"}`; `app.py` routes the exact path on POST behind `_require_session` (which enforces CSRF); and `cd api && uv run pytest` passes with new tests asserting that **each of the eleven validation failures leaves all three FakeStores byte-identical to their pre-call state**, that a links-only file returns `signed_out: false` and leaves the users store untouched, that a users-bearing file removes a pre-existing `session:abc` and `_meta:bootstrapped`, that a pre-existing key absent from the file is pruned, and that `slug:` writes are ordered before `all_links` and `user:` writes before `_meta:usernames`.
- [ ] Add the index-walk fallback — **ONLY IF the spike found `get_keys` unusable; skip and tick with a note otherwise** — file(s): api/backup.py, api/tests/test_backup.py — done when: `index_walk_keys(store, store_name, num_event_slots)` derives links keys from `all_links` plus `owner_links:<owner>` per username, users keys from `_meta:usernames`, and analytics keys as `count:<slug>` and `events:<slug>:<slot>` for every slug and slot; every export it produces carries `fidelity: "index-walk"` and `incomplete: true`; restore's prune pass in this mode deletes index-derived keys only; and `cd api && uv run pytest` passes with a test proving an unindexed `slug:orphan` is absent from the export (the documented limitation, asserted rather than assumed).
- [ ] Add the backup admin page and its download half (depends on GET /api/admin/backup) — file(s): gui/admin/backup.html (new), gui/admin/backup.js (new), gui-pages/routing.py, gui-pages/tests/test_routing.py, spin.toml — done when: `spin.toml` gains an **exact** `[[trigger.http]] route = "/admin/backup.js"` on the `gui` component in the per-page-assets block, `ROUTES` gains `"/admin/backup.html": "admin/backup.html"`, and `test_resolve_file` gains the matching case; the page carries no inline `<script>`, `<style>`, `style="` or `on<event>=` anywhere (including comments) and loads `../theme-init.js`, `../vendor/pico.min.css`, `../theme.css`, `../app.js`, `backup.js` with no `backup.css`; the download article offers three store checkboxes all checked by default and downloads via the `Blob` + `createObjectURL` + `<a download>` + `revokeObjectURL` sequence from `dashboard.js:743-751` with the filename `spin-shortener-backup-YYYY-MM-DD.json`; a `users.manage`-less viewer sees only `#forbidden-notice`; `cd gui-pages && uv run pytest` passes (57 → 64 expected: 4 auto-derived page checks, 2 auto-derived script checks, 1 new `test_resolve_file` case — verify the real number rather than asserting it); and in a real browser the downloaded file opens as valid JSON containing no `password_hash` string.
- [ ] Add the restore half of the backup page (depends on POST /api/admin/restore and the page task) — file(s): gui/admin/backup.html, gui/admin/backup.js — done when: a `<input type="file" accept=".json,application/json">` reads via `FileReader.readAsText` with a client-side size pre-check against a mirrored `BACKUP_MAX_BODY_BYTES` carrying the "server is authoritative" comment `dashboard.js:472-477` uses; the parsed file's `created_at`, `created_by`, `fidelity` and `counts` render into `#restore-summary` **before** any request is sent, with a bad `format` reported there; the Restore button requires the literal `REPLACE` typed into `#restore-confirm` and then a count-bearing `confirmDialog` (`confirmLabel: "Replace everything"`) before POSTing `{"confirm", "backup"}`; a local `BACKUP_ERROR_MESSAGES` map gives friendly copy for at least `invalid_backup_format`, `unsupported_schema_version`, `forbidden_key`, `credential_material_in_backup`, `too_many_entries`, `body_too_large`, `confirmation_required` and `backup_too_large`; on `signed_out: true` the page calls `setCsrfToken(null)`, shows the restored counts, the sentence "You have been signed out. Sign in again with the bootstrap admin credentials." and a `<a href="/login.html" role="button">` with **no auto-redirect and no timer**; and `cd gui-pages && uv run pytest` still passes with no inline code.
- [ ] Add the Backup nav link and re-measure nav overflow at four breakpoints (depends on the backup page task) — file(s): gui/app.js, gui/dashboard.js, gui/links/detail.js, gui/admin/users.js, gui/admin/backup.js — done when: `initHeader` accepts `backupHref` and `onBackupPage`, renders `<li id="backup-link" hidden>` immediately after `#manage-users-link`, and reveals it under the same `canManageUsers` condition at `app.js:404` while hiding it on the backup page itself; all four authenticated pages pass depth-correct hrefs; and `scrollWidth` vs `clientWidth` on `#app-header nav` is measured and **recorded in the task note** at 1400px, 768px, 480px and 390px in **both** themes with two domains configured, showing zero overflow at every one — or, if any breakpoint overflows, the pre-approved fallback is applied instead (the nav item is dropped and `admin/users.html` gains a plain in-body `<a href="backup.html">Backup and restore</a>`), never a new nav overflow menu.
- [ ] Flag accounts with no password in the admin users table (depends on the `password_set` task) — file(s): gui/admin/users.html, gui/admin/users.js — done when: a user with `password_set: false` renders a second `<span class="status-badge status-disabled">no password</span>` beside the existing active/disabled badge, using no new colour token and therefore needing no new contrast measurement; a `<p id="password-reset-notice" class="form-error" role="alert" hidden>` above the table reads `N account(s) have no password and can't sign in — set one with Edit.` and is shown only when at least one such account exists; `cd gui-pages && uv run pytest` still passes; and in a real browser, after a restore, every restored non-bootstrap account shows the badge and setting a password via Edit clears it on the next load.
- [ ] Document KV backup and restore in CLAUDE.md, PRODUCT.md and DESIGN.md (depends on every task above) — file(s): CLAUDE.md, PRODUCT.md, DESIGN.md — done when: CLAUDE.md gains a "KV backup and restore" section (peer to "Bulk link management") stating the file format and its schema-version refusal rule, that values are uniformly base64 and keys plaintext, that the file contains no `password_hash`, no `session:*` and no `_meta:bootstrapped` **and why that is what makes encryption unnecessary**, the two caps and the rule that raising either needs timing evidence, that restore is all-or-nothing and replaces (write-then-prune) rather than merges, the `links` → `analytics` → `users` ordering and the records-before-indexes rule within each store, the full recovery walkthrough **including the confirmed fact that `ensure_bootstrap_admin` runs on every request so no restart is needed** and the disclosed corner that it overwrites a restored `admin` record, the rule that a user record with no usable `password_hash` can never authenticate, the `users.manage` gate and why it is not a weaker bar than `role == "admin"`, and the fidelity label the spike settled; PRODUCT.md's Capabilities list gains one accurate line that does not use the word "backup" if the spike landed on index-walk; DESIGN.md's `### Status Badges` gains the "no password" variant and `### Navigation` gains the Backup link with its measured overflow numbers; `.impeccable/design.json` is updated only if a new token was actually introduced (none is planned — say so in the task note if none was); and no doc claims a capability the shipped code does not have.
- [ ] End-to-end manual verification of KV backup and restore — file(s): (none — verification step) — done when: every numbered step in docs/plans/kv-backup-restore.md's Verification section is executed against a real `spin up --build --runtime-config-file runtime-config.toml` in a browser with the console open and **zero errors of any kind, in particular zero CSP violations, in both light and dark themes**; the destroy-and-restore round trip is done for real (create links and a second user, download, delete everything via the dashboard, restore, confirm the links resolve at `/r/<slug>` and the second user appears flagged), the post-restore bootstrap login succeeds **without restarting `spin up`**, a restored user's login attempt returns `401` and not `500`, and `cd api && uv run pytest`, `cd gui-pages && uv run pytest` and `cd redirect && go test ./linkgate/...` all pass.

## Critical files

- `docs/plans/kv-backup-restore.md` (new) — this plan
- `docs/plans/kv-backup-restore-scratch.md` (new, gitignored) — spike handoff
- `api/backup.py` (new)
- `api/tests/test_backup.py` (new)
- `api/app.py`
- `api/auth.py`
- `api/users.py`
- `api/tests/fakes.py`
- `api/tests/test_auth.py`
- `api/tests/test_users.py`
- `gui/admin/backup.html` (new)
- `gui/admin/backup.js` (new)
- `gui/admin/users.html`
- `gui/admin/users.js`
- `gui/app.js`
- `gui/dashboard.js`
- `gui/links/detail.js`
- `gui-pages/routing.py`
- `gui-pages/tests/test_routing.py`
- `spin.toml`
- `CLAUDE.md`
- `PRODUCT.md`
- `DESIGN.md`
- `TASKS.md`

Deliberately **not** in the list: `redirect/` (anything — no Go changes, and the
language-split rule agrees: this is authoring/admin work, not hot path),
`api/links.py`, `api/bulk.py`, `api/analytics.py`, `api/qr.py`,
`api/responses.py` (`json_response` is reused unchanged), `gui/theme.css` (no new
token), `gui/dashboard.html`, `runtime-config.toml`, `Jenkinsfile` (the three
test commands are unchanged), `gui-pages/tests/test_no_inline_code.py` and
`gui-pages/tests/test_manifest_components.py` (both auto-cover the new files —
see the facts section).

## Verification

Run in this order. Steps 1-4 are CI-equivalent and need no running app.

1. `cd redirect && go test ./linkgate/...` → `ok`. Nothing in this plan touches
   Go; this is a no-regression check only. **Never `go test ./...`** — it fails
   by design on `package main`.
2. `cd api && uv run pytest` → all pass, count above the 227 baseline.
3. `cd gui-pages && uv run pytest` → all pass, expected 64 (57 baseline + 4
   derived page checks + 2 derived script checks + 1 new `test_resolve_file`
   case). Verify the real number rather than asserting this one.
4. `grep -rn "password_hash" gui/admin/backup.js gui/admin/backup.html` → no
   matches, and `grep -rn "allowed_outbound_hosts" spin.toml` → unchanged from
   `43071e6` (only `redirect`'s `[]`).
5. Start the app:

   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```

   Sign in as `admin`, create **three links** (one with a custom slug, one with a
   password, one with a start/end window), click at least one of them at
   `/r/<slug>` twice so `analytics` has real data, and create a second user
   `alice` with `links.create_custom_slug`.
6. **Export, in the browser.** Navigate to the Backup page from the nav. With all
   three stores checked, click Download. A pass is: a
   `spin-shortener-backup-YYYY-MM-DD.json` file lands, opens as valid JSON, has
   `"format": "spin-shortener-kv-backup"`, `"schema_version": 1`, a `fidelity`
   matching the spike's outcome, `counts` matching `stores`, and
   `grep -c password_hash <file>` → **0**, `grep -c '"session:' <file>` → **0**,
   `grep -c _meta:bootstrapped <file>` → **0**. Console shows zero errors.
7. **Partial export.**
   `curl -s -b "session=<cookie>" 'http://localhost:3000/api/admin/backup?stores=links,users' | python3 -c "import json,sys; print(sorted(json.load(sys.stdin)['stores']))"`
   → `['links', 'users']`. Then `?stores=nope` → HTTP 400 with
   `{"error": "unknown_store", ...}`.
8. **Permission gate.** Sign in as `alice` (no `users.manage`), navigate to
   `/admin/backup.html` directly: only `#forbidden-notice` renders, and
   `curl` of both endpoints as alice returns `403 {"error": "forbidden",
   "required_permission": "users.manage"}`.
9. **Cap behaviour and timing.** POST a body just over 5 MiB → `413
   body_too_large` naming `max_bytes`. Then, as the `MAX_BACKUP_*` sizing
   evidence, time a restore of the largest backup you can realistically produce
   locally and **record the wall-clock number in the task note** — the same rule
   `CLAUDE.md` states for `MAX_BULK_ROWS`. If a full-cap restore is slow, that is
   a finding to report loudly, not a reason to quietly lower the cap.
10. **All-or-nothing.** Hand-edit a copy of the good backup to (a) bump
    `schema_version` to `2`, (b) insert a `"session:forged"` key into
    `stores.users`, (c) insert a `"password_hash"` into a `user:` value, and (d)
    corrupt one base64 value. Upload each. A pass is: each is refused with its
    named error code, the `#restore-summary` or `#restore-error` explains it in
    plain words, and **`GET /api/links` and `GET /api/users` are byte-identical
    before and after every one of the four attempts.**
11. **The real round trip.** Delete all three links and the user `alice` through
    the GUI. Confirm `/r/<slug>` 404s. Upload the good backup, type `REPLACE`,
    confirm the count-bearing dialog. A pass is: `200`, restored counts shown,
    the "You have been signed out" message and the sign-in link render, and no
    auto-redirect fires.
12. **Recovery, without restarting `spin up`.** Reload any page → bounced to
    `/login.html`. Sign in as `admin` with the bootstrap password → **succeeds on
    the still-running process** (this is the confirmed
    `ensure_bootstrap_admin`-runs-per-request behaviour; if it fails, that fact
    was wrong and the plan needs revisiting, not a workaround). Dashboard shows
    all three links; `/r/<slug>` resolves for the plain one, prompts for the
    password-protected one, and 404s for the out-of-window one; the link's click
    total matches what it was at step 5.
13. **Hash-less accounts.** On `/admin/users.html`, `alice` shows the "no
    password" badge and the count notice renders. Attempt to sign in as `alice`
    with her old password → **`401`, not a `500`** (check the server log for a
    traceback; there must be none). Set a new password via Edit, reload → the
    badge is gone, and signing in as `alice` with the new password succeeds.
14. **Nav and themes.** With `SPIN_VARIABLE_PUBLIC_BASE_URLS="http://localhost:3000,http://127.0.0.1:3000"`
    set, measure `#app-header nav`'s `scrollWidth` vs `clientWidth` at 1400/768/480/390px
    in **both** themes on the backup page and the dashboard. Zero overflow at all
    eight measurements, or the pre-approved fallback is applied. Console must show
    **zero CSP violations** on the new page in both themes.

## Out of scope / follow-ups

- **No S3, no cloud storage, no `allowed_outbound_hosts` change** in any
  component. See the rejected-alternatives entry; this is structural, not a
  scoping convenience.
- **No scheduled or automatic backups.** Operator-invoked only.
- **No password hashes, session data or `_meta:bootstrapped` in the file** — and
  the restore actively refuses a file that contains them.
- **No merge semantics, no partial restore, no undo.**
- **No encryption of the backup file**, because it holds no secrets by
  construction. That is only true while hashes are stripped.
- **No index repair or consistency-check pass.** Restore reproduces the file
  faithfully, inconsistencies included. **This belongs under `TASKS.md`'s
  "Future work (not scheduled)"** — a `GET /api/admin/consistency` that reports
  (never silently fixes) unindexed `slug:` records, `all_links` entries with no
  record, and `owner_links` disagreeing with record owners would be genuinely
  useful and is a clean separate feature. Added there.
- **No cross-version migration.** `schema_version` exists so a v2 file can be
  refused; nothing upgrades a v1 file to v2. Write that when there is a v2.
- **No backup of Spin variables or `runtime-config.toml`.** Those are deployment
  configuration, not data, and one of them holds the bootstrap password — the
  exact thing this file is defined not to contain.
- **The Akamai single-`"default"`-store consolidation is untouched.** When that
  refactor happens, `BACKUP_STORES` and every key prefix in `backup.py` change
  with it, and the backup format gains a v2. Noted so whoever does that
  migration knows this file is in its blast radius — **and note that a v1 backup
  taken before the consolidation will not restore into a post-consolidation
  deployment**, which is worth knowing before it is the only copy of anything.
- **Whether a restore should be possible at all once the app is deployed to a
  shared host** is a deployment-policy question, not a code one. Nothing here
  gates on environment.
