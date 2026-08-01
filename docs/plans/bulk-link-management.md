# Bulk Link Management

## Context

The dashboard is a one-link-at-a-time tool. Creating a campaign's worth of
links means filling the same form N times, and there is no way to act on more
than one existing link at once — no multi-delete, and (see the facts below) no
way to disable a link at all from the GUI, in bulk or otherwise. `TASKS.md`'s
Future-work entry "Bulk link management on the dashboard" (raised 2026-07-24,
the Alex/power-user persona finding from the Impeccable critique: *"No bulk
operations, no export... exactly the workload this persona represents"*) is the
brief, and `PRODUCT.md` names the same population as the primary user —
"Tire Rack's marketing/campaign team, creating and tracking short links for
campaigns, ads, and promotions." That entry says "Not scoped in detail yet —
re-plan before starting." **This plan supersedes it.**

Two things about the current state make a naive implementation wrong rather
than merely slow:

- Every create and delete does a read-modify-write on the `all_links` and
  `owner_links:<username>` index keys (`api/links.py:40-73`). Spin's KV has no
  atomic operations and no compare-and-swap — already documented in `CLAUDE.md`
  as an accepted v1 constraint. N concurrent `POST /api/links` calls from a
  browser loop would race on those two keys and silently lose index entries,
  producing links that exist in KV, resolve at `/r/<slug>`, and are invisible
  in the dashboard forever.
- Link passwords are hashed with a **pure-Python** PBKDF2 (`api/auth.py:34-57`
  — the WASI CPython bundled by componentize-py has no `hashlib.pbkdf2_hmac`),
  100,000 iterations of `hmac.new(...).digest()` in a Python loop inside Wasm.
  Doing that once per row is not a performance nitpick; it is the difference
  between one hash per submission and one per row.

**Confirmed decisions** (settled by the user before planning — not reopened
here):

1. **Bulk-create input is two columns: slug + destination.** A blank slug means
   auto-generate. No per-row password or schedule columns.
2. **Password and start/end scheduling are batch-level UI controls**, applied to
   every link created in that submission. This matches the campaign use case and
   keeps the file format trivial.
3. **All-or-nothing on validation.** If any row is invalid, nothing is created
   and every bad row is reported. Validate everything before writing anything.
4. **Bulk actions are Delete and Enable/Disable.** CSV export is deferred to
   `TASKS.md`'s "Future work (not scheduled)".
5. **Selection: "select all" selects all currently-filtered rows, and the
   selection clears on filter change, sort change, or a completed bulk action.**
   A user must never act on rows they are not looking at.
6. **File upload is read client-side** (`FileReader`), so no multipart parsing
   ever reaches a `componentize-py` Wasm component.
7. **`MAX_BULK_ROWS` is 50.** Set by the user on 2026-08-01, overruling this
   plan's original 200. 50 comfortably covers the "dozens of campaign links"
   the persona finding describes, and at that size the Wasm-timing question
   largely evaporates rather than needing to be defended.

**One deliberate deviation, called out here so it does not read as an oversight:**
decision 6 says the *file* is read client-side, and it is — `FileReader` fills
the textarea and no multipart ever reaches a Wasm component. But the text is
**not parsed** client-side; it is posted raw and parsed in `api/bulk.py`. The
reason is that this repo has no JavaScript test runner at all (no
`package.json`, no jest/vitest config; `Jenkinsfile` runs exactly `go test
./linkgate/...` and two `uv run pytest`), so a JS parser would be permanently
untested — and server-side parsing is also what lets an error name the physical
line number of the user's own file. Confirmed and accepted by the user
2026-08-01. Full reasoning under Trade-offs, "Parsing the CSV/TSV in
JavaScript".

## Key technical facts confirmed during research

- **The index read-modify-write is per-call.** `api/links.py:40-51`
  (`_add_owned_slug`/`_remove_owned_slug`) and `:62-73`
  (`_add_all_slug`/`_remove_all_slug`) each `get` the key, mutate the list, and
  `set` it back. `handle_create` calls two of them; `handle_delete` calls two.
  There is no batched variant today.

- **`handle_list` already tolerates a dangling index entry.**
  `api/links.py:191-196` iterates the index and skips any slug whose record is
  `None`. This is what makes "write records first, indexes last" the safe crash
  ordering in *both* directions (see "Write ordering" below).

- **There is no JavaScript test runner in this repo.** `find . -name
  package.json` (excluding `node_modules`) returns nothing; `Jenkinsfile` runs
  exactly three commands, all Python/Go. Any parser written in JS is
  permanently untested. This is the decisive argument for parsing the pasted
  text **on the server**, in `api/`, where `uv run pytest` covers it.

- **Nothing in the GUI can change a link's `status` today.** `grep -rn "status"
  gui/` shows `dashboard.js:164` and `links/detail.js:22` rendering it, and the
  row edit form (`editRowHtml`, `dashboard.js:98-124`) offering only
  destination, schedule and password. `PATCH /api/links/<slug>` has accepted
  `status` since Phase 1 (`api/links.py:232-236`) but no client sends it. **Bulk
  enable/disable will be the first and only way to disable a link from the
  GUI** — worth stating plainly, since it makes a "bulk" action the sole path to
  a single-link operation. A single-row toggle is listed under Out of scope.

- **`POST /api/links/bulk` cannot collide with a link whose slug is `bulk`.**
  `api/app.py:104` matches `path.startswith("/api/links/") and method in
  ("GET", "PATCH", "DELETE")` — POST is not in that list, and the only existing
  POST-on-a-slug route is the `/password` suffix at `:71`. Dispatching the new
  routes on **exact** `path ==` equality (not `startswith`) above those branches
  means `/api/links/bulk/password` for a real slug named `bulk` still reaches
  `handle_set_password`. Verified by reading the router, not assumed.

- **`api.delete` in `gui/app.js:56` sends no body.** `delete: (path) =>
  apiCall(path, { method: "DELETE" })`. Any "DELETE /api/links with a list of
  slugs" design would require widening that shared helper; the chosen design
  uses `api.post` unchanged.

- **CSRF needs no work.** `apiFetch` (`gui/app.js:19-22`) attaches
  `X-CSRF-Token` to every POST/PATCH/PUT/DELETE, and `_require_session`
  (`api/app.py:19-26`) checks it via `auth.check_csrf`. Both new endpoints are
  POSTs behind `_require_session`, so they inherit this.

- **The inline-code guard covers `dashboard.js`.**
  `gui-pages/tests/test_no_inline_code.py:41-45` globs every non-vendor `.js`
  under `gui/` and asserts no `\bstyle\s*=` and no srcless `<script>` appears in
  any of them, plus the four HTML checks over `ROUTES`' pages. New markup must
  use classes, the `hidden` attribute, and `addEventListener`. Note the regex
  matches **comments too** — do not write `style=` in a comment. (`element.style
  .display = "…"`, as at `dashboard.js:182`, does not match `\bstyle\s*=` and is
  not blocked by CSP either, since CSSOM mutation is outside `style-src` — but
  new code should use `hidden` per `CLAUDE.md`.)

- **The table's column indices are load-bearing in four places**, all of which
  break silently if a column is inserted at position 1:
  `gui/theme.css:520-524` (the monospace-data rule, `nth-child(1),(3),(4),(6),(7)`),
  `gui/dashboard.css:68-90` and `:120-123` (the 600px mobile rules), the two
  `colspan="8"` literals in `dashboard.js:101` and `:141`, and — the nastiest —
  `dashboard.js:257-259`, which writes an edited row's saved values into
  `displayRow.children[2] / [5] / [6]` by **positional index**. Get that last
  one wrong and saving an inline edit writes a formatted date into the wrong
  cell, with no error anywhere.

- **`confirmDialog` already takes a custom confirm label.**
  `gui/app.js:106` — `confirmDialog(message, { confirmLabel = "Delete",
  cancelLabel = "Cancel" })`. The destructive button's own text can therefore
  state the scale ("Delete 50 links") with no change to the shared helper.

- **`friendlyError(data, fallback, overrides)`** (`gui/app.js:171`) reads
  `data.error`. Shaping each per-row error as `{"line", "slug", "error"}` — with
  the code under the key `error`, not `code` — lets the row-error renderer call
  `friendlyError(rowErr, …)` directly with a dashboard-local overrides map,
  exactly as `handleEditFormSubmit` already does for `invalid_password`
  (`dashboard.js:271-273`). No change to `app.js` is needed anywhere in this
  plan.

- **No new `spin.toml` routes are needed.** All new GUI code extends
  `gui/dashboard.js` and `gui/dashboard.css`, both already exact-routed
  (`spin.toml:118-124`). A new `gui/bulk.js` would need its own exact route or
  it would 404 silently (the failure mode `spin.toml:77-84` warns about); the
  added code does not justify that.

- **`FakeStore` (`api/tests/fakes.py`) implements `get`/`set`/`delete`/
  `exists`** — everything both new handlers need, with no additions.

- **Baseline is green** as of `0ec613d`: `cd api && uv run pytest` → 135
  passed; `cd gui-pages && uv run pytest` → 57 passed; `cd redirect && go test
  ./linkgate/...` → ok.

- **UNCONFIRMED: how long a full 50-row submission takes** inside the
  `componentize-py` Wasm component, and whether Spin imposes a request-body or
  request-duration limit that a 256 KB body would hit. Nothing in this repo has
  ever sent a body larger than a few KB or done more than ~5 KV round trips in
  one request. **At a 50-row cap this is no longer a design risk** — 50 rows is
  roughly 100 KV operations plus at most one PBKDF2 hash, and the per-request
  work is bounded well below anything the redirect hot path does at scale.
  Verification step 7 still measures a real full-cap submission, but its purpose
  has changed: it is now **confirmation that a full-cap submission is
  comfortably fast, not a gate that might force the cap down**. If 50 rows is
  slow (say, over a couple of seconds), that is a genuine finding to report
  loudly — it would mean something is wrong beyond the cap, most likely in the
  per-row KV path, and lowering the cap would be treating the symptom.

- **UNCONFIRMED: the maximum value size Spin's sqlite-backed KV accepts** for
  the `all_links` index. 50 additional 7-character slugs adds roughly 500 bytes
  to a JSON array that is already unbounded in the current design, so this
  feature makes an existing, unmeasured limit arrive marginally sooner rather
  than introducing a new one. Not addressed here; the KV explorer
  (`dev/kv-explorer-up.sh`) is the tool for eyeballing the index if it ever
  matters.

## Data model

**Unchanged.** Bulk create writes exactly the record shape `handle_create`
writes today (`api/links.py:168-179`) — `slug`, `target_url`, `owner`, `custom`,
`password_hash`, `status`, `start_at`, `end_at`, `created_at`, `updated_at` — and
bulk actions only mutate `status`/`updated_at` or delete the key. No new fields,
no migration, no new KV keys, no new permissions.

Two record-level details:

- `custom` is `true` for a row that supplied a slug and `false` for a row that
  did not, matching single-create semantics and therefore the "Custom" badge.
- Every record in one submission shares a single `iso_now()` value for
  `created_at`/`updated_at`. One clock read, and sorting the table by Created
  keeps a batch contiguous instead of interleaving it by write order.

## Text format specification

This is the part users hit first, so it is specified exhaustively. All of it is
implemented in `parse_bulk_text` in `api/bulk.py` and unit-tested.

Preprocessing, in this order:

1. Strip a leading UTF-8 BOM (`﻿`). Excel's "CSV UTF-8" export writes one,
   and without this the first row's slug becomes `﻿myslug` and fails
   `invalid_custom_slug` for a reason invisible on screen.
2. Normalize `\r\n` → `\n`, then lone `\r` → `\n`. Then `split("\n")`.
   Deliberately **not** `str.splitlines()`, which also breaks on `\v`, `\f`,
   `\x1c`–`\x1e`, `\x85` and ` ` — those would shift every reported line
   number away from what the user's text editor shows.
3. Line numbers are **1-based physical line numbers of the original text**,
   assigned before any skipping, so a reported line number always matches
   "go to line N" in an editor.

Per line:

4. Strip leading/trailing whitespace. If the result is empty, skip the line
   (this absorbs a trailing newline and any blank separator lines).
5. If it starts with `#`, skip it. A slug can only contain `[A-Za-z0-9_-]`
   and a destination must start with a scheme, so `#` is unambiguously a
   comment marker.
6. **If the line starts with `http://` or `https://` (case-insensitive), the
   entire line is the destination and the slug is blank.** This rule comes
   first, and it exists because URLs contain commas
   (`https://x.com/p?ids=1,2`) and the naive "split on the first comma" would
   otherwise mangle a single-column list of URLs into nonsense. A slug can never
   contain `:` or `/`, so this test can never misfire on a real two-column row.
7. Otherwise, if the line contains a tab, split on the **first** tab.
   Spreadsheet paste (Excel, Google Sheets) is tab-delimited, and a URL never
   contains a literal tab.
8. Otherwise, if the line contains a comma, split on the **first** comma. Later
   commas therefore stay inside the destination, which is correct because the
   destination is always the last field.
9. Otherwise the line is a single token. If it matches
   `links.CUSTOM_SLUG_PATTERN`, treat it as `(slug=token, destination="")` so
   the user gets `missing_target_url` ("this row has a short link but no
   destination"); if it does not, treat it as `(slug=None, destination=token)`
   so they get `invalid_target_url`. Two lines of code that turn one confusing
   message into the right one.
10. Each field is then `.strip()`ed, has **one** pair of matching surrounding
    double quotes removed if present, and is `.strip()`ed again. This is not
    RFC 4180 quoting (see Trade-offs) — it exists so that a spreadsheet's
    `slug,"https://x.com/a,b"` round-trips correctly, which it does: rule 8
    splits at the first comma, and the dequote step removes the wrapper.
11. A blank slug field (`,https://example.com/x`) means auto-generate, exactly
    as a blank "Custom short link" input does today.

Header row:

12. After parsing, **the first parsed row is dropped iff** its destination
    field, lowercased and stripped, is in `HEADER_WORDS`, **or** its destination
    field is empty and its slug field, lowercased, is in `HEADER_WORDS`.
    `HEADER_WORDS` is a fixed set covering both columns' plausible labels:
    `{"slug", "short link", "short_link", "shortlink", "short url",
    "short_url", "destination", "destination url", "destination_url",
    "destinationurl", "target", "target url", "target_url", "url", "link",
    "long url", "long_url"}`.
    This is safe rather than magic: none of those strings can ever be a valid
    destination (rule 6 requires a scheme), so a dropped header row could only
    ever have produced an error. If a header uses a word outside the set, the
    submission fails with `invalid_target_url` on line 1 and the UI's message
    for that code (below) names the header case explicitly.

Case sensitivity: slugs are case-**sensitive**, because KV keys are and
`POST /api/links` already lets `Sale` and `sale` coexist. `Sale` and `sale` in
one file are therefore two links, not a duplicate. Called out here because it is
the first thing someone will file a bug about.

Caps:

- `MAX_BULK_ROWS = 50` — parsed rows, not physical lines. Above it:
  `400 {"error": "too_many_rows", "max_rows": 50, "row_count": <submitted>}`.
- `MAX_BULK_BODY_BYTES = 262144` (256 KB) — checked on `len(request.body)`
  before any JSON parsing. Above it:
  `413 {"error": "body_too_large", "max_bytes": 262144}`.

Both are enforced **server-side**, which is the enforcement that matters. The
client mirrors them only to fail fast with a nicer message (a 5 MB file is
rejected before `FileReader` reads it), and it renders whatever `max_rows` /
`max_bytes` the server returns rather than hardcoding the numbers a second
time.

**The `too_many_rows` rejection must be actionable, not just a refusal.** At a
50-row cap this is a message real users will hit — a 120-row spreadsheet export
is an entirely reasonable thing to paste — so the response carries both numbers
and the UI says what to do about it:

> Too many rows — this file has 120 and the limit is 50 per submission. Split it
> into 3 smaller batches and submit them one at a time.

The batch count is `Math.ceil(row_count / max_rows)`, computed in the client
from the two values in the response body. Nothing is created, and the textarea
keeps its contents so the user can cut it down in place rather than re-pasting.
The same `too_many_rows` code and both fields are returned by
`/api/links/bulk-action` when a selection exceeds the cap, with the wording
adjusted to selection ("You've selected 120 links; bulk actions apply to at most
50 at a time.").

Worked example — this input:

```
﻿Slug,Destination URL
black-friday,https://tirerack.com/promo?a=1,2
,https://tirerack.com/other

# holiday campaign
xmas-2026	https://tirerack.com/xmas
https://tirerack.com/plain
```

produces four rows: `("black-friday", "https://tirerack.com/promo?a=1,2")` from
line 2, `(None, "https://tirerack.com/other")` from line 3,
`("xmas-2026", "https://tirerack.com/xmas")` from line 6, and
`(None, "https://tirerack.com/plain")` from line 7. Line 1 is a header, line 4
is blank, line 5 is a comment.

## API changes

### Task-ordering note

The `links.py` refactor below **must land before** `api/bulk.py` — `bulk.py`
imports the promoted names, and the batched index writers are what make the
whole design correct.

### `api/links.py` — promote shared helpers, add batched index writers

`analytics.py` and `qr.py` already import `can_view` and `get_link` from
`links.py`, so cross-module reuse of link helpers is the established pattern;
these promotions extend it rather than inventing it.

Renames (mechanical; update every call site in `links.py` and
`api/tests/test_links.py`, which references `links._is_valid_custom_slug`
directly at `test_links.py:34`):

| today | becomes |
| --- | --- |
| `_is_valid_target_url` | `is_valid_target_url` |
| `_is_valid_custom_slug` | `is_valid_custom_slug` |
| `_can_edit` | `can_edit` |
| `_public_link` | `public_link` |
| `_parse_window_field` | `parse_window_field` |
| `_generate_slug` | `generate_slug` |

`can_edit`'s docstring/comment at `links.py:123` currently justifies its
privacy ("module-private since only `links.py` needs write-gating", in the
`can_view` docstring above it) — that sentence is now false and must be
corrected in the same change.

New constant, replacing the inline tuple at `links.py:234`:

```python
LINK_STATUSES = ("active", "disabled")
```

New/changed functions:

```python
async def allocate_random_slug(store, taken: set[str]) -> str:
    """Random slug not in `taken` and not already in the store. Adds the
    result to `taken` so a caller allocating many in one pass cannot collide
    with itself without a KV round trip per candidate."""
```
`_allocate_random_slug` is replaced by this; `handle_create` calls
`allocate_random_slug(store, set())`.

```python
async def add_slugs_to_indexes(store, owner: str, slugs: list[str]) -> None:
    """One read+write of `all_links`, one of `owner_links:<owner>`, for any
    number of slugs. Order-preserving, skips slugs already present."""

async def remove_slugs_from_indexes(store, slugs_by_owner: dict[str, list[str]]) -> None:
    """One read+write of `all_links` total, plus one per distinct owner. Takes
    a per-owner mapping because a `links.edit_all` user can delete links
    belonging to several owners in a single action."""
```

`handle_create` replaces its `_add_owned_slug` + `_add_all_slug` pair with
`await add_slugs_to_indexes(store, principal.username, [slug])`, and
`handle_delete` replaces its `_remove_owned_slug` + `_remove_all_slug` pair
with `await remove_slugs_from_indexes(store, {record["owner"]: [slug]})`. The
four single-slug helpers are then deleted. This is not cosmetic: it guarantees
the bulk and single paths write the indexes through exactly one implementation,
so they can never drift.

Existing behaviour of `handle_create`/`handle_update`/`handle_delete`/
`handle_set_password`/`handle_list`/`handle_get` is otherwise untouched. All 135
current tests must still pass unmodified except for the renamed symbol at
`test_links.py:34`.

### `api/bulk.py` (new) — pure logic

Zero `spin_sdk` imports; `store`/`request` arrive as parameters;
`Request`/`Response` come from `responses`, per `CLAUDE.md`'s testability rule.

```python
MAX_BULK_ROWS = 50
MAX_BULK_BODY_BYTES = 262_144
ACTION_STATUSES = {"enable": "active", "disable": "disabled"}   # values ⊆ links.LINK_STATUSES

@dataclass
class BulkRow:
    line: int
    slug: str | None      # None = auto-generate
    target_url: str

def parse_bulk_text(text: str) -> list[BulkRow]: ...

def validate_bulk_rows(
    rows: list[BulkRow],
    existing_slugs: set[str],
    can_custom_slug: bool,
) -> list[dict]:
    """One {"line", "slug", "error"[, "first_line"]} dict per bad row, in line
    order. Empty list means the whole submission is valid."""
```

`validate_bulk_rows` is pure — it takes the existing-slug set rather than the
store — which is what makes the whole format spec above testable without a
FakeStore. Per-row codes:

| code | when |
| --- | --- |
| `invalid_target_url` | not a `str`, or `links.is_valid_target_url` says no |
| `missing_target_url` | slug present, destination empty (format rule 9) |
| `invalid_custom_slug` | slug present but fails `links.is_valid_custom_slug` |
| `custom_slug_forbidden` | slug present and the principal lacks `links.create_custom_slug` |
| `slug_taken` | slug already exists in the store |
| `duplicate_slug_in_submission` | same slug appeared on an earlier line; carries `first_line` |

A row can produce more than one problem; report the **first** in the order
above, so each bad line yields exactly one message.

**The mixed permission case is decided explicitly:** a user without
`links.create_custom_slug` who submits some rows with slugs and some without
gets `custom_slug_forbidden` on **every** slugged row and nothing is created.
The alternatives were to ignore the slug and auto-generate (silently gives the
user a different result than they asked for — the worst outcome of the three)
or to create only the blank-slug rows (a partial write, which decision 3 rules
out). Reporting every offending line means the fix is "delete the first
column", and the user can see exactly which lines to fix. This mirrors
`handle_create`'s existing 403 for the single-link case; the status code differs
(400 with per-row detail rather than a bare 403) because the submission as a
whole is a validation failure, not an unauthorized request — the same user may
legitimately create every one of those links by blanking the slug column.

### `POST /api/links/bulk` — bulk create

Request:

```json
{
  "text": "black-friday,https://example.com/sale\n,https://example.com/other\n",
  "password": null,
  "start_at": "2026-11-27T05:00:00.000Z",
  "end_at": null
}
```

`text` is the raw pasted/uploaded text; the three batch fields are optional and
validated **once** using `links.MIN_LINK_PASSWORD_LENGTH`,
`links.parse_window_field`, and the same `start_at >= end_at` check
`handle_create` performs (`links.py:164-165`), returning the same
`invalid_password` / `invalid_start_at` / `invalid_end_at` /
`invalid_window_range` codes so `gui/app.js`'s existing `ERROR_MESSAGES` map
already covers them.

`handle_bulk_create(store, principal, request)`, in order:

1. `len(request.body or b"") > MAX_BULK_BODY_BYTES` → `413 body_too_large`.
2. JSON decode error → `400 invalid_json`; `text` not a `str` → `400
   invalid_text`.
3. Validate the three batch fields (above). **Do not hash yet.**
4. `rows = parse_bulk_text(text)`; empty → `400 no_rows`;
   `len(rows) > MAX_BULK_ROWS` → `400 too_many_rows` with **both** `max_rows`
   and `row_count` (the number actually submitted), so the client can tell the
   user how far over they are and into how many batches to split.
5. `existing = set(await links.all_slugs(store))` — **one** KV read for the
   whole submission, not one per row.
6. `row_errors = validate_bulk_rows(rows, existing, principal.has_permission("links.create_custom_slug"))`.
7. **Index-drift confirmation:** for each row with an explicit slug, `await
   store.exists(f"slug:{slug}")`; any hit that step 6 missed becomes a
   `slug_taken` row error. This is the one place the plan spends N KV reads
   deliberately. `all_links` is an index, not the truth; if it has ever drifted
   (an interrupted write, a KV-explorer edit), trusting it alone would
   **overwrite a live link record**, which is data loss. `store.exists` is the
   same check `handle_create` makes at `links.py:143`.
8. If `row_errors` is non-empty → `400 {"error": "bulk_validation_failed",
   "row_errors": [...], "row_count": len(rows)}` and **write nothing**.
9. `password_hash = auth.hash_password(password) if password else None` —
   computed at most once per request, after every validation gate, so an
   invalid submission never pays the PBKDF2 cost at all.
10. Assign slugs: explicit ones first (into a `taken` set seeded from
    `existing`), then `links.allocate_random_slug(store, taken)` for each blank
    row.
11. Write every `slug:<slug>` record with one shared `iso_now()`, **then** call
    `links.add_slugs_to_indexes(store, principal.username, new_slugs)` once.
12. `201 {"count": N, "links": [links.public_link(r) for r in created]}` — the
    `links` key matches `handle_list`'s response shape and the records match
    `handle_create`'s.

### `POST /api/links/bulk-action` — bulk delete / enable / disable

Request: `{"slugs": ["a", "b"], "action": "delete" | "enable" | "disable"}`.

`handle_bulk_action(store, principal, request)`:

1. `400 invalid_json`; `action` not in `{"delete"} | ACTION_STATUSES.keys()` →
   `400 invalid_action`; `slugs` not a list, empty, or containing a non-string
   → `400 no_slugs`; duplicates within `slugs` → `400 duplicate_slug`;
   `len(slugs) > MAX_BULK_ROWS` → `400 too_many_rows` carrying `max_rows` and
   `row_count`, the same contract as bulk create.
2. Read every `slug:<slug>` record. Missing → row error `not_found`;
   `links.can_edit` false → row error `forbidden`. Any row error →
   `400 {"error": "bulk_validation_failed", "row_errors": [{"slug", "error"}]}`
   with **no writes**. (Row errors here carry no `line` key — the selection has
   no lines. The client's renderer treats `line` as optional.)
3. `enable`/`disable`: set `record["status"] = ACTION_STATUSES[action]` and
   `record["updated_at"] = iso_now()`, rewrite each `slug:` record. Indexes are
   untouched — status is not indexed.
4. `delete`: delete every `slug:` record **first**, then one
   `links.remove_slugs_from_indexes(store, slugs_by_owner)` call, where
   `slugs_by_owner` is built from each record's `owner` (a `links.edit_all` user
   can delete across owners in one action).
5. `200 {"ok": true, "action": action, "count": N}`.

**Status validation reuses `PATCH`'s vocabulary rather than duplicating it.**
The client never sends a raw status string; it sends `enable`/`disable`, and
`ACTION_STATUSES`' values are the same strings `handle_update` validates
against. A unit test asserts `set(ACTION_STATUSES.values()) <=
set(links.LINK_STATUSES)`, so adding a third status to one place and not the
other fails a test instead of shipping.

**Bulk actions are all-or-nothing too**, matching decision 3. The realistic
failure is a stale selection (someone else deleted a link since the page
loaded), and the honest response is "your view is out of date, refresh" rather
than "we did 49 of the 50 things you asked for, work out which." Partial
success is recorded as a rejected alternative.

### Write ordering (both endpoints)

**Records first, indexes last, in both directions.** This is not arbitrary:

- Interrupted create → link records exist that no index lists. They resolve at
  `/r/<slug>` and are invisible in the dashboard. Recoverable, and step 7's
  `store.exists` confirmation stops a later submission from silently
  overwriting one.
- Interrupted delete → index entries exist whose records are gone.
  `handle_list` already skips exactly this (`links.py:193-195`), so the only
  visible effect is nothing at all.

The inverse ordering makes both failures worse: an index written first
advertises slugs that 404, and a delete that de-indexes first leaves working
links the user believes are gone. It also matches what the single-item handlers
already do, so there is one rule for the whole file.

### `api/app.py` — routing

Three lines of dispatch, placed **above** the existing `/api/links/...`
branches (before the `/password` branch at `:71`), both on exact equality:

```python
if path == "/api/links/bulk" and method == "POST":
    ...  # _require_session, key_value.open("links"), bulk.handle_bulk_create

if path == "/api/links/bulk-action" and method == "POST":
    ...  # _require_session, key_value.open("links"), bulk.handle_bulk_action
```

Both follow the existing `_require_session` → `isinstance(result, Response)`
short-circuit → `links_store = await key_value.open("links")` shape used by
every other link route. `app.py` gains routing and wiring only, no logic —
it stays out of pytest by design.

## GUI changes

Everything lands in `gui/dashboard.html`, `gui/dashboard.js`,
`gui/dashboard.css`, plus a renumbering edit in `gui/theme.css`. **No new
files, no new `spin.toml` routes, no new colour tokens.**

### Bulk-create panel

`DESIGN.md`'s Do — *"collapse occasional-use fields behind a native `<details>`
disclosure when a form's primary action is a single common field"* — is the
incumbent pattern and this follows it: a second `<details>` inside the existing
"Create a new link" `<article>`, placed **after** `</form>` and after the
`#create-error`/`#create-success` paragraphs (a nested `<form>` is invalid HTML,
so it cannot go inside `#advanced-options`).

```html
<details id="bulk-panel">
  <summary>Create many at once</summary>
  <form id="bulk-form">
    <label for="bulk-text">Short link and destination, one per line</label>
    <textarea id="bulk-text" name="text" rows="8"
      placeholder="black-friday,https://example.com/sale&#10;,https://example.com/other"></textarea>
    <small id="bulk-format-hint">…</small>

    <label for="bulk-file">…or choose a .csv, .tsv or .txt file</label>
    <input type="file" id="bulk-file" accept=".csv,.tsv,.txt,text/csv,text/tab-separated-values,text/plain" />

    <fieldset>
      <legend>Applies to every link in this batch</legend>
      <div class="grid">
        <label for="bulk-start-at">Starts (optional)<input type="datetime-local" id="bulk-start-at" /></label>
        <label for="bulk-end-at">Expires (optional)<input type="datetime-local" id="bulk-end-at" /></label>
      </div>
      <label for="bulk-password">Password protection (optional)
        <input type="password" id="bulk-password" minlength="4" placeholder="Leave blank for no password" /></label>
    </fieldset>

    <button type="submit">Create links</button>
  </form>
  <p id="bulk-error" class="form-error" role="alert"></p>
  <div id="bulk-errors" hidden></div>
  <p id="bulk-success" class="form-success" aria-live="polite" hidden></p>
</details>
```

`#bulk-format-hint` states the format in one sentence: *"Two columns separated
by a comma or a tab: short link, then destination. Leave the short link blank
(or give just a URL) to generate one. Blank lines and `#` comments are ignored;
a header row is detected and skipped."*

**The file input fills the textarea; it is not a second submission path.**
`change` → size check against the server's byte cap → `FileReader.readAsText`
→ assign to `#bulk-text` → clear the file input's value. The user then sees and
can edit exactly what will be sent, and the submit handler has one source of
truth. Use `reader.addEventListener("load", …)`, not `reader.onload`.

Submit posts `{ text, password, start_at, end_at }` via `api.post("/links/bulk",
…)`, reusing `datetimeLocalToIso` for the two datetime fields and
`wireWindowValidation(#bulk-start-at, #bulk-end-at)` for the native
start-before-end check — the same two helpers the single-create form uses.

Responses:

- `201` → `#bulk-success` reads `Created 12 links.`, the textarea and the three
  batch fields are cleared, `loadLinks()` refreshes the table. The panel stays
  **open** (the success banner lives inside it; closing it would hide the
  payoff — note this deliberately differs from `#advanced-options`, which
  `dashboard.js:328` closes on success because its banner is outside it).
- `400 bulk_validation_failed` → `#bulk-error` reads `Nothing was created — N
  rows need fixing.` and `#bulk-errors` renders a table:

  | Line | Short link | Problem |
  | --- | --- | --- |
  | 3 | black-friday | That short link is already in use — try a different one. |
  | 7 | — | Enter a valid destination URL (including https://). |

  Built with `innerHTML` from `escapeHtml`'d values. **No truncation is
  needed** — `MAX_BULK_ROWS` is 50, so `row_errors` can never exceed 50 entries
  and the worst case is a 50-row table, which is a readable list rather than a
  wall. (The original 200-row cap needed a "first 50, then …and N more" cutoff;
  dropping the cap to 50 removed that code path entirely. If the cap is ever
  raised, reinstate the cutoff — see the `too_many_rows` entry under Considered
  and rejected.) Message text comes from `friendlyError(rowErr, "This row
  isn't valid.", BULK_ROW_MESSAGES)`, where `BULK_ROW_MESSAGES` is a
  dashboard-local overrides map (the `invalid_password` override at
  `dashboard.js:271-273` is the precedent):

  ```js
  const BULK_ROW_MESSAGES = {
    missing_target_url: "This line has a short link but no destination URL.",
    duplicate_slug_in_submission: "This short link appears earlier in your list.",
    custom_slug_forbidden: "You don't have permission to choose your own short links — leave the first column blank.",
    invalid_target_url: "Not a valid destination URL (include https://). If this is a header row, delete it.",
  };
  ```
- `400 too_many_rows` → `#bulk-error` reads `Too many rows — this file has
  ${data.row_count} and the limit is ${data.max_rows} per submission. Split it
  into ${Math.ceil(data.row_count / data.max_rows)} smaller batches and submit
  them one at a time.` **The textarea is not cleared**, so the user can cut the
  list down in place. This is the one over-limit message real users will
  actually hit at a 50-row cap, so it names both numbers and the remedy rather
  than just refusing.
- Any other error → `#bulk-error` via `friendlyError`, plus explicit copy for
  `body_too_large` (`That's too much text — the limit is
  ${Math.floor(data.max_bytes / 1024)} KB.`), reading the number out of the
  response rather than duplicating the constant.

Every render clears the other two elements first, so a success never sits above
a stale error table.

### Selection column

A new **first** column. `dashboard.html`'s `<thead>` gains, before the "Short
link" `<th>`:

```html
<th class="select-col"><input type="checkbox" id="select-all-links" aria-label="Select all links in view" /></th>
```

It is deliberately **not** `class="sortable"` and carries no `aria-sort`, so
`document.querySelectorAll("#links-table th.sortable")` (`dashboard.js:73`,
`:344`) is unaffected.

Per row in `renderLinksTable`:

```html
<td class="select-cell">
  <input type="checkbox" class="row-select" data-slug="…" aria-label="Select link …" />
</td>
```

rendered **only when `canEditLink(link)`** — otherwise an empty
`<td class="select-cell"></td>`, so the column stays aligned. This mirrors the
server: `handle_bulk_action` re-checks `links.can_edit` on every slug, so a
hand-crafted request gains nothing.

**Select-all with a mixed filtered set:** the header checkbox selects every
*selectable* (i.e. `canEditLink`) row in the current filtered view, ignoring the
rest. It shows `indeterminate` when some but not all selectable rows are
selected, and is `disabled` when the filtered view contains no selectable rows
at all. Anything else would either promise an action the API will refuse or
require a fourth visual state to explain a partial selection nobody asked for.

**Select-all can exceed the 50-row cap, and that has to be handled in the UI.**
This is a direct consequence of the cap moving from 200 to 50: a user with 120
links who clicks select-all now selects more than one action can carry. Rather
than silently checking only the first 50 (which would act on rows the user
believes are excluded — the inverse of decision 5's principle), the header
checkbox selects everything selectable and the **bar disables its three action
buttons** above the cap, with `#bulk-count` reading:

> 120 links selected — bulk actions apply to at most 50 at a time.

`dashboard.js` needs the number to render that, so it carries
`const BULK_MAX_SELECTION = 50;` with a comment stating that **the server is
authoritative** (`bulk.MAX_BULK_ROWS`) and that a drift between the two shows up
as a `too_many_rows` rejection naming the real cap, not as silently wrong client
behaviour. This is the one place the constant is duplicated, and it is duplicated
because the alternative is issuing a request that is guaranteed to fail.

**Selection clears at the top of `renderLinksTable()`** — one line
(`selectedSlugs.clear(); updateBulkBar();`), not three call-site edits. Every
existing caller is a filter change, a sort change, or `loadLinks()` (which every
completed bulk action calls), so this implements decision 5 exactly and is
structurally impossible to forget when a fourth caller appears later.
`selectedSlugs` is a module-level `Set` of slug strings; row checkboxes are
wired through the existing delegated `#links-body` click listener
(`dashboard.js:281`) rather than per-row listeners, matching the comment at
`:188-192` about why delegation is used.

### Bulk-action bar

Between `#links-filter` and `#links-figure` in the links `<article>`:

```html
<div id="bulk-bar" role="status" hidden>
  <span id="bulk-count"></span>
  <div role="group">
    <button type="button" id="bulk-enable-btn" class="outline">Enable</button>
    <button type="button" id="bulk-disable-btn" class="outline">Disable</button>
    <button type="button" id="bulk-delete-btn" class="secondary outline">Delete</button>
  </div>
</div>
<p id="links-success" class="form-success" aria-live="polite" hidden></p>
```

`hidden` whenever `selectedSlugs.size === 0`. `#bulk-count` reads
`1 link selected` / `12 links selected`. Delete is `secondary outline`, matching
the row-action Delete and `DESIGN.md`'s rule that a destructive action reads
through de-emphasis rather than a danger fill. `role="status"` announces the
count change without stealing focus.

Confirmation, via the existing `confirmDialog`:

- **1 selected:** `Delete the link "black-friday"? This can't be undone.` —
  byte-identical to today's single-row message (`dashboard.js:199`), so the one
  case that overlaps the existing flow reads identically.
- **N selected:** `Delete 50 links? This can't be undone.` with
  `{ confirmLabel: "Delete 50 links" }`, so the destructive button itself states
  the scale. The dialog does **not** list the slugs — 50 slugs in a modal is
  unreadable, the count is the actual safety signal, and the rows are visible
  and checked directly behind the dialog.
- **Enable/Disable: no confirmation.** Both are reversible by the adjacent
  button, and a confirm on a reversible action trains people to dismiss
  confirms.

On success: `#links-success` reads `Deleted 12 links.` / `Enabled 3 links.` /
`Disabled 3 links.`, then `loadLinks()` (which clears the selection and hides
the bar). On `bulk_validation_failed`: `#links-error` reads `Nothing was
changed — N of the selected links are no longer available. Refresh and try
again.`, with the per-slug reasons rendered in the same `#bulk-errors`-style
table (line column omitted). Also hide `#create-success` on a successful bulk
delete, for the same reason `handleDeleteClick` does at `dashboard.js:211` — its
banner holds a live Copy button for a slug that may have just been deleted.

### Column renumbering — the part that breaks silently

Inserting a column at position 1 shifts every positional selector and index.
Exhaustive list (this is the whole set; there are no others):

| file | today | becomes |
| --- | --- | --- |
| `gui/theme.css:520-524` | `#links-table td:nth-child(1),(3),(4),(6),(7)` | `(2),(4),(5),(7),(8)` |
| `gui/dashboard.css:68-77` | hides `th/td:nth-child(2),(3),(4),(6)` @600px | hides `(3),(4),(5),(7)` — **and adds `(1)`**, the select column |
| `gui/dashboard.css:87-90` | `th:nth-child(1)` / `td:nth-child(1)` `max-width:100px` | `nth-child(2)` |
| `gui/dashboard.css:120-123` | `thead th:nth-child(7)` / `td:nth-child(7)` `max-width:75px` | `nth-child(8)` |
| `gui/dashboard.js:101` | `<td colspan="8">` (edit row) | `colspan="9"` |
| `gui/dashboard.js:141` | `<td colspan="8">` (empty state) | `colspan="9"` |
| `gui/dashboard.js:257` | `displayRow.children[2]` (destination) | `children[3]` |
| `gui/dashboard.js:258` | `displayRow.children[5]` (starts) | `children[6]` |
| `gui/dashboard.js:259` | `displayRow.children[6]` (expires) | `children[7]` |

The sticky action column (`dashboard.css:17-23`) uses `:last-child` and needs no
change. `.destination-cell` and `.slug-chip` are class-based and need no change.

**Hiding the select column below 600px is deliberate.** `dashboard.css:59-66`
records a live measurement that at 390px the sticky action column already
occludes other columns, and the 100px/75px caps below it were derived from that
measurement. A ~40px checkbox column would invalidate all of it, to enable a
multi-select workflow nobody performs on a phone. With the column hidden the
visible set at 390px is exactly what it is today — Short link, Status, Expires,
actions — and the bar can never appear there, because nothing is selectable.

### New CSS (all in `gui/dashboard.css`, no new tokens)

- `#links-table th.select-col, #links-table .select-cell { width: 1%; white-space: nowrap; text-align: center; }`
- `#links-table .select-cell input[type="checkbox"], #links-table th.select-col input[type="checkbox"] { margin: 0; }`
  — Pico gives checkboxes a bottom margin that misaligns them in a table cell;
  verify with `getComputedStyle`, not by eye.
- `#bulk-bar { display: flex; align-items: center; flex-wrap: wrap; gap: 0.75rem; margin-bottom: var(--pico-spacing); padding: 0.5rem 0.75rem; border: 1px solid var(--pico-muted-border-color); border-radius: var(--pico-border-radius); }`
  — a hairline border and background contrast, no shadow (`DESIGN.md`'s
  No-Shadow Rule), reusing the one border token.
- `#bulk-bar [role=group] { width: auto; }` — Pico groups stretch to 100%; the
  identical override already exists for the row actions at `dashboard.css:24-26`.
- `#bulk-text { font-family: var(--ss-mono-font); }` — it holds slugs and URLs,
  which is exactly what `DESIGN.md`'s monospace-for-data rule is for.
- `#bulk-errors td:nth-child(1), #bulk-errors td:nth-child(2) { font-family: var(--ss-mono-font); font-size: 0.85em; }`
  — mirrors the existing `#days-table td:first-child` treatment. It goes in
  `dashboard.css` rather than `theme.css` because the CSP pass established
  page-scoped stylesheets; `theme.css`'s existing `#links-table` selectors
  predate that and are only edited here to renumber, not relocated.

Both themes are covered without measurement work because no new colour is
introduced — every new surface inherits `--pico-color`,
`--pico-muted-border-color` and `--pico-card-background-color`, all of which are
already defined in both `theme.css` blocks. Verification still requires loading
the dashboard in both themes and confirming legibility, per this repo's history
of thin-margin colours.

## Trade-offs and rejected alternatives

**A client-side loop of N single-item requests.** Ships with zero API work: the
dashboard already has `api.post`/`api.delete`, and "bulk" becomes a `for` loop
with a progress counter. It is also wrong, not just slow — every iteration
read-modify-writes `all_links` and `owner_links:<user>`, and Spin's KV has no
CAS (`CLAUDE.md`, "Security tradeoffs"), so overlapping in-flight requests
silently drop index entries. The result is links that exist and resolve but can
never be seen or managed again, produced by a bug that will not reproduce on a
developer's machine with three rows. A server-side loop over `handle_create`
has the identical defect for the identical reason, with the addition that it
would hash the batch password once per row.

**Parsing the CSV/TSV in JavaScript and posting structured rows.** Genuinely
attractive: the client could show a parsed preview before submitting, the API
would take a clean `rows` array, and the wire format would be self-describing.
Lost on testability. There is no JS test runner in this repo (no
`package.json`, and `Jenkinsfile` runs three Python/Go commands), so the
fiddliest logic in the feature — BOM, CRLF, quote stripping, header detection,
delimiter precedence — would be the only untested code in it. Posting the raw
text also gives strictly better errors: the server reports the **physical line
number of the user's file**, which a rows-array design throws away before the
server ever sees it. The user's constraint that the file be read client-side is
fully honoured — `FileReader` fills the textarea, and there is exactly one
submission path.

**Multipart file upload to the `api` component.** The conventional shape for
"upload a CSV", and it would let the browser stream a large file. Rejected
because it means writing a multipart parser inside a `componentize-py` Wasm
component (`email`/`cgi` based parsing under a WASI CPython is precisely the
category of stdlib assumption this repo has already been burned by — see
`hashlib.pbkdf2_hmac`'s absence), to solve a problem that does not exist: the
files in question are a few KB of text, and `FileReader` + a JSON string field
handles them with no new dependency and no new parser.

**Full RFC 4180 CSV parsing** (quoted fields containing delimiters, `""`
escapes, embedded newlines). Correct in the abstract and available from
Python's stdlib `csv` module. Rejected because the two fields here are a slug
(`[A-Za-z0-9_-]{3,32}`, which can never contain a delimiter) and a URL (which
can never contain a tab, and whose commas are already handled by splitting on
the *first* delimiter only). The one real spreadsheet artifact — a quoted URL
containing a comma — is handled by the single-pair dequote step. Embedded
newlines inside a quoted field would break the "one line = one row" contract
that makes line-numbered errors possible, which is worth more here than
completeness. Revisit only if someone actually produces a file the current rules
mangle.

**Per-row password and schedule columns.** Settled by the user before planning,
and worth recording: a 5-column format would let one file describe a whole
heterogeneous campaign. It loses because it makes the file format something
users must learn (column order, empty-cell semantics, per-row ISO-8601
timestamps typed by hand) instead of something they can guess, and because
per-row passwords mean one PBKDF2 hash per row — 50 × 100,000 pure-Python HMAC
iterations in Wasm, in a single request. Batch-level controls keep the file at
two columns and the hash count at one.

**A 200-row cap instead of 50.** The plan originally specified 200, on the
reasoning that "dozens" was the stated workload and 200 gave generous headroom.
Overruled by the user on 2026-08-01 in favour of **50**, and the reasoning is
better: 50 already covers the persona's workload comfortably, and it moves the
Wasm-timing question from "a risk the cap is sized around" to "a non-issue"
— at 50 rows the per-request work is bounded well below anything the redirect
path does at scale, so the live timing check becomes confirmation rather than a
gate. It also deleted a whole code path, the error-table's "first 50, then …and
N more" truncation, which can no longer trigger. The real cost is that the cap
is now reachable — a 120-row spreadsheet export will hit it — which is why
`too_many_rows` carries `row_count` alongside `max_rows` and the UI tells the
user how many batches to split into, and why the bulk-action bar disables itself
above 50 selected rows instead of quietly acting on a subset. Raising it later
should be a deliberate decision made **with timing evidence from Verification
step 7 in hand**, not a number that drifts upward because someone found the
limit inconvenient; if it is raised past ~100, reinstate the error-table
truncation at the same time.

**Partial success — create the valid rows, report the invalid ones.** The most
tempting alternative, and the one most bulk importers actually implement:
nobody enjoys re-submitting 199 good rows because of one typo. Rejected on the
user's decision 3, and it holds up: with partial success the user must diff what
they submitted against what exists to work out what to retry, and a re-submit of
the corrected file then collides with the rows that did succeed and produces a
second wave of `slug_taken` errors. All-or-nothing means the fix loop is "edit
the file, submit again" with no bookkeeping. The same argument extends to bulk
actions, where the failure is a stale selection and the honest answer is
"refresh".

**Putting the selection column last instead of first.** This is not a cosmetic
preference: at position 8 (before the sticky action column) it would shift
**zero** `nth-child` selectors and zero `children[]` indices, eliminating the
entire renumbering table above and its silent-breakage risk — including the
`displayRow.children[5]` bug class that writes a date into the wrong cell with
no error. It loses to convention. Every table users touch daily (Gmail, GitHub,
every admin console) puts the checkbox at the left edge, and a right-edge
checkbox next to four action buttons invites mis-clicks on Delete. The
renumbering is mechanical, fully enumerated above, and verified by a live 390px
layout check that this repo already has a documented procedure for.

**A modal dialog, or a tab bar, for bulk create.** A dialog would keep the
create card uncluttered, and a "Single / Bulk" tab pair would be the most
explicit framing. The dialog loses because this form is a textarea plus a file
picker plus three batch fields plus a potentially 50-row error table — a scroll
trap in a modal — and because the app's only `<dialog>` is `confirmDialog`,
deliberately a tiny confirm; growing it into a form host is a new component. The
tab bar loses because no tab component exists in the design system and
introducing one for two panels adds a pattern that must then be maintained
everywhere. A second `<details>` is the incumbent pattern, already styled,
already keyboard-accessible, and already has its `:focus-visible` Pico gotcha
fixed in `theme.css`.

**Making the row cap a Spin variable** (`bulk_max_rows`, like
`analytics_event_slots`). It would let an operator tune the cap for a slower
host without a rebuild. Rejected because `analytics_event_slots` is a variable
for a specific reason — two components must agree on it — and this one is read
by exactly one function in one component. It would add a `[variables]` entry, a
`[component.api.variables]` entry, a `variables.get` + `int()` in `app.py`, and
a parameter threaded through `handle_bulk_create`, to express a safety rail
tied to what the runtime can do in one request rather than an operator policy.
The error body returns `max_rows`, so the UI stays truthful if the constant
changes. Revisit if a deployment target actually needs a different number.

**`DELETE /api/links` with a JSON body of slugs**, which is the most RESTful
shape for bulk delete. Rejected because `gui/app.js:56`'s shared `api.delete`
sends no body, so it would have to be widened for one call site; because
`DELETE` with a semantically significant body is under-specified and
inconsistently supported by proxies; and because this API already uses
`POST /api/links/<slug>/password` for what is logically a `PATCH`, so a POST
action endpoint is the incumbent convention, not a deviation.

**Doing nothing.** Live throughout: the persona finding is two years of
one-at-a-time clicking away from being fatal, and the workaround (create links
one by one) works. It loses because the whole product thesis in `PRODUCT.md` is
"give the marketing team enough self-serve control that routine link operations
don't need engineering involvement", and a 40-link campaign launch is currently
40 form submissions — the exact workload the persona critique named.

## Tasks

Appended verbatim to `TASKS.md` under `## Bulk link management`. `TASKS.md` is
authoritative; the builder ticks boxes only there.

```
- [ ] Promote the shared link helpers and add batched index writers (must land before every other task in this section) — file(s): api/links.py, api/tests/test_links.py — done when: `_is_valid_target_url`, `_is_valid_custom_slug`, `_can_edit`, `_public_link`, `_parse_window_field` and `_generate_slug` are renamed to public names (`is_valid_target_url`, `is_valid_custom_slug`, `can_edit`, `public_link`, `parse_window_field`, `generate_slug`) with every call site in `links.py` and `api/tests/test_links.py` updated and the now-false "module-private since only links.py needs write-gating" note in `can_view`'s docstring corrected; a `LINK_STATUSES = ("active", "disabled")` constant replaces the inline tuple in `handle_update`; `allocate_random_slug(store, taken: set[str]) -> str` replaces `_allocate_random_slug`, skipping slugs in `taken` as well as ones `store.exists` reports and adding its result to `taken`; new `add_slugs_to_indexes(store, owner, slugs)` and `remove_slugs_from_indexes(store, slugs_by_owner)` each read+write `all_links` exactly once and each `owner_links:<owner>` key exactly once, `handle_create` and `handle_delete` route through them, and the four single-slug index helpers are deleted; `cd api && uv run pytest` passes with all 135 existing tests plus new ones covering both batched writers including a multi-owner remove.
- [ ] Add api/bulk.py's pure text parser and row validator with unit tests — file(s): api/bulk.py (new), api/tests/test_bulk.py (new) — done when: `parse_bulk_text(text) -> list[BulkRow]` implements the format spec in docs/plans/bulk-link-management.md exactly (BOM strip; CRLF and lone-CR normalization then `split("\n")`, not `splitlines()`; 1-based physical line numbers; blank and `#` lines skipped; a line starting with `http://`/`https://` is entirely a destination; else first tab wins over first comma; else a lone token that matches the slug pattern is a slug with an empty destination; per-field strip, one surrounding double-quote pair removed, strip again; first-row header drop via HEADER_WORDS) and `validate_bulk_rows(rows, existing_slugs, can_custom_slug) -> list[dict]` returns one `{"line", "slug", "error"}` dict per bad row using `invalid_target_url`, `missing_target_url`, `invalid_custom_slug`, `custom_slug_forbidden`, `slug_taken` and `duplicate_slug_in_submission` (which also carries `first_line`), at most one per row in that precedence order; both functions are pure (no `store`, no `spin_sdk` import); `cd api && uv run pytest` passes with new tests covering a BOM+CRLF+header file, a destination URL containing a comma both with and without a leading slug, a tab-delimited row, a comment line, a trailing newline, and case-sensitive duplicate detection (`Sale` and `sale` are two links, not a duplicate).
- [ ] Add POST /api/links/bulk for bulk creation (depends on the two tasks above) — file(s): api/bulk.py, api/app.py, api/tests/test_bulk.py — done when: `handle_bulk_create(store, principal, request)` rejects a body over `MAX_BULK_BODY_BYTES` (262144) with `413 body_too_large` carrying `max_bytes`, invalid JSON with `400 invalid_json`, a non-string `text` with `400 invalid_text`, zero parsed rows with `400 no_rows`, and more than `MAX_BULK_ROWS` (**50**) rows with `400 too_many_rows` carrying both `max_rows` and `row_count` (the number actually submitted, so the client can say how far over the user is and into how many batches to split); validates batch-level `password`/`start_at`/`end_at` once via `links.MIN_LINK_PASSWORD_LENGTH` and `links.parse_window_field` plus the same `start >= end` check `handle_create` uses, returning the same `invalid_password`/`invalid_start_at`/`invalid_end_at`/`invalid_window_range` codes; reads `all_links` exactly once for existence checks and then re-confirms each explicitly-slugged row with `store.exists`; returns `400 {"error": "bulk_validation_failed", "row_errors": [...], "row_count": N}` having written nothing if any row fails; otherwise calls `auth.hash_password` at most once, writes every `slug:<slug>` record sharing one `iso_now()` before a single `links.add_slugs_to_indexes` call, and returns `201 {"count": N, "links": [...]}` of `links.public_link` records; `app.py` dispatches on exact `path == "/api/links/bulk"` and `method == "POST"` above the existing `/api/links/` branches, using the same `_require_session` + `key_value.open("links")` shape as its neighbours; `cd api && uv run pytest` passes with FakeStore tests including one that submits 3 good rows and 1 bad row and asserts `all_links`, `owner_links:<user>` and every `slug:` key are byte-identical afterwards, and one asserting a principal without `links.create_custom_slug` gets `custom_slug_forbidden` on every slugged row of a mixed submission.
- [ ] Add POST /api/links/bulk-action for bulk delete/enable/disable (depends on the refactor task) — file(s): api/bulk.py, api/app.py, api/tests/test_bulk.py — done when: `handle_bulk_action(store, principal, request)` accepts `{"slugs": [...], "action": "delete"|"enable"|"disable"}` and rejects a missing/unknown action with `400 invalid_action`, a non-list/empty/non-string-containing `slugs` with `400 no_slugs`, duplicates within `slugs` with `400 duplicate_slug`, and more than 50 slugs (`MAX_BULK_ROWS`) with `400 too_many_rows` carrying `max_rows` and `row_count`; pre-validates every slug (record exists, `links.can_edit` passes) and returns `400 {"error": "bulk_validation_failed", "row_errors": [{"slug", "error"}]}` with `not_found`/`forbidden` codes and zero writes if any fails; `enable`/`disable` map through `ACTION_STATUSES = {"enable": "active", "disable": "disabled"}` and only rewrite `slug:` records (status is not indexed); `delete` removes every `slug:` record first and then calls `links.remove_slugs_from_indexes` once with slugs grouped by each record's owner; both return `200 {"ok": true, "action": ..., "count": N}`; `app.py` dispatches on exact `path == "/api/links/bulk-action"` and `method == "POST"`; `cd api && uv run pytest` passes with tests covering a cross-owner delete by a `links.edit_all` user (both `owner_links:` keys updated, `all_links` written once), an all-or-nothing rejection that leaves the store unchanged, and an assertion that `set(ACTION_STATUSES.values()) <= set(links.LINK_STATUSES)`.
- [ ] Add the row-selection column and the bulk-action bar to the dashboard (depends on the bulk-action endpoint) — file(s): gui/dashboard.html, gui/dashboard.js, gui/dashboard.css, gui/theme.css — done when: a non-sortable first column carries `#select-all-links` in the header and a `.row-select` checkbox per row rendered only when `canEditLink(link)` (an empty `.select-cell` otherwise); the header checkbox selects every selectable row in the current filtered view, shows `indeterminate` when some but not all are selected, and is `disabled` when the view has none; a selection larger than `BULK_MAX_SELECTION` (50, duplicated in dashboard.js with a comment naming the server as authoritative) leaves every row selected but **disables all three action buttons** and reads "N links selected — bulk actions apply to at most 50 at a time." rather than silently acting on a subset; `selectedSlugs.clear()` plus a bulk-bar update run at the top of `renderLinksTable()` so filtering, sorting and `loadLinks()` all clear the selection; row checkboxes are handled through the existing delegated `#links-body` listener; `#bulk-bar` is `hidden` at zero selection and otherwise shows "N link(s) selected" plus Enable/Disable/Delete (Delete `secondary outline`); Delete confirms via `confirmDialog` with today's exact single-row wording at 1 selected and `Delete N links? This can't be undone.` with `confirmLabel: "Delete N links"` above 1, Enable/Disable do not confirm; each action posts one request to `/links/bulk-action`, then on success writes `#links-success`, hides `#create-success` (delete only) and calls `loadLinks()`, and on `bulk_validation_failed` writes the refresh-and-retry message to `#links-error` with the per-slug reasons rendered beneath; **every entry in the plan's column-renumbering table is applied** — `theme.css`'s monospace rule to `(2),(4),(5),(7),(8)`, `dashboard.css`'s 600px hidden set to `(3),(4),(5),(7)` plus the new `(1)`, its two `max-width` rules to `nth-child(2)`/`nth-child(8)`, both `colspan="8"` to `9`, and `displayRow.children[2]/[5]/[6]` to `[3]/[6]/[7]`; `cd gui-pages && uv run pytest` still passes (no inline code); in a real browser an inline row edit saved after the change still updates the destination/Starts/Expires cells and not the wrong ones, and at 390px the visible column set and the previously-measured clearances are unchanged from before this task.
- [ ] Add the bulk-create panel to the dashboard (depends on the bulk-create endpoint) — file(s): gui/dashboard.html, gui/dashboard.js, gui/dashboard.css — done when: a `<details id="bulk-panel">` "Create many at once" sits inside the create card after `#create-form` and its error/success paragraphs (not nested inside the form), containing `#bulk-text`, a format hint, `#bulk-file` accepting .csv/.tsv/.txt, a fieldset of batch-level Starts/Expires/password wired through `wireWindowValidation` and `datetimeLocalToIso`, a submit button, `#bulk-error`, `#bulk-errors` and `#bulk-success`; choosing a file checks its size against the server's byte cap, reads it with `FileReader` via `addEventListener("load", …)` into `#bulk-text`, and clears the file input, so submission has exactly one source of truth; submit posts `{text, password, start_at, end_at}` to `/links/bulk`; a 201 writes "Created N links.", clears the textarea and batch fields, leaves the panel open and calls `loadLinks()`; a `bulk_validation_failed` renders a Line/Short link/Problem table (escaped; no truncation, since the 50-row cap already bounds `row_errors` at 50) using `friendlyError` with a dashboard-local `BULK_ROW_MESSAGES` overrides map; a `too_many_rows` reads both `row_count` and `max_rows` out of the response and states how many rows the file has, the limit, and to split it into `Math.ceil(row_count / max_rows)` batches, leaving the textarea contents intact so the user can cut it down in place; `body_too_large` reads `max_bytes` out of the response rather than hardcoding it; each render clears the other two result elements first; `#bulk-text` renders in `var(--ss-mono-font)`; `cd gui-pages && uv run pytest` still passes (no inline `<script>`, `<style>`, `style=` or `on<event>=` anywhere in the new markup, including in JS comments).
- [ ] Document bulk link management in CLAUDE.md, PRODUCT.md and DESIGN.md — file(s): CLAUDE.md, PRODUCT.md, DESIGN.md, .impeccable/design.json — done when: CLAUDE.md gains a "Bulk link management" section stating the two endpoints, that both are all-or-nothing, the 50-row and 256 KB caps, why they are constants rather than Spin variables, and that raising the row cap needs timing evidence rather than being a free knob, the accepted text format in brief, the records-first/indexes-last write ordering and the reason `handle_list` makes it safe, and that bulk enable/disable is the only GUI path that changes a link's status; PRODUCT.md's Capabilities list gains one accurate line; DESIGN.md gains a `Bulk Action Bar` entry under `## Components` (hairline border, no shadow, de-emphasized Delete, count-bearing confirm label) and a Do covering "a select-all control acts on the filtered set only, and selection clears on any re-render"; `.impeccable/design.json` gains a matching `components` entry in the existing entries' shape; no doc claims a capability the shipped code does not have.
- [ ] End-to-end manual verification of bulk link management — file(s): (none — verification step) — done when: with `SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false spin up --build --runtime-config-file runtime-config.toml` running, every numbered step in the plan's Verification section is executed in a real browser with the console open and zero errors of any kind (in particular zero CSP violations) in both light and dark themes, including: a mixed paste (header row, blank slug, comment, tab-delimited row, URL containing a comma) creating the right links; a deliberately broken paste reporting the right line numbers and creating nothing; a full-cap 50-row submission succeeding comfortably fast with its wall-clock time recorded as confirmation, not as a gate (report loudly if it takes more than a couple of seconds — at 50 rows that indicates a real problem in the per-row KV path, not a cap that is too high); a 120-row submission creating nothing and reporting both numbers plus "split it into 3 smaller batches", with the textarea contents left intact; a selection of more than 50 rows disabling the three bulk-action buttons with the reason shown; select-all under an active filter selecting only filtered rows; the selection clearing on filter, sort and after each action; bulk disable then enable round-tripping the Status badges; bulk delete of 3 links confirming with the count and removing exactly those 3; a non-admin without `links.edit_all` seeing checkboxes only on their own rows; and the 390px layout matching its pre-change screenshots; `cd api && uv run pytest`, `cd gui-pages && uv run pytest` and `cd redirect && go test ./linkgate/...` all pass.
```

## Critical files

- `api/links.py` — helper promotions, `LINK_STATUSES`, `allocate_random_slug`,
  batched index writers; `handle_create`/`handle_delete` rerouted through them.
- `api/bulk.py` **(new)** — parser, row validator, both handlers.
- `api/app.py` — two exact-path POST dispatch branches.
- `api/tests/test_links.py` — renamed symbol reference, new tests for the
  batched index writers.
- `api/tests/test_bulk.py` **(new)** — parser, validator and both handlers.
- `gui/dashboard.html` — selection column header, bulk-action bar,
  `#links-success`, the bulk-create `<details>` panel.
- `gui/dashboard.js` — selection state, select-all, bulk-bar wiring, bulk-create
  submit and error rendering, and every column-index fix.
- `gui/dashboard.css` — select column, bulk bar, error table, monospace
  textarea, renumbered 600px rules.
- `gui/theme.css` — renumbered monospace-data `nth-child` rule only.
- `CLAUDE.md`, `PRODUCT.md`, `DESIGN.md`, `.impeccable/design.json` — docs.

Not touched: `spin.toml` (no new routes), `Jenkinsfile` (no change to how tests
are invoked), `redirect/` (nothing on the hot path — resolution, the password
gate and analytics are all unaffected, which is why this feature is entirely
Python per the language-split rule), `gui-pages/` (no new page, no new route).

## Verification

1. `cd api && uv run pytest` — 135 existing plus the new `test_bulk.py` and the
   index-writer tests, all passing.
2. `cd gui-pages && uv run pytest` — 57 passing, including the inline-code guard
   over the new `dashboard.js` and `dashboard.html` content.
3. `cd redirect && go test ./linkgate/...` — unchanged, must still pass (nothing
   in this plan touches Go; run it to prove that).
4. Start the app:
   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```
5. Log in at `http://localhost:3000/login.html`, open **Create a new link →
   Create many at once**, and paste exactly the worked example from the "Text
   format specification" section (including the header row, the blank line, the
   comment, the tab-delimited row and the URL containing a comma). Submit.
   **Pass:** "Created 4 links.", and the table shows `black-friday` (Custom
   badge), `xmas-2026` (Custom badge) and two auto-generated slugs, all with
   identical Created timestamps. Click one of the auto-generated slugs' View and
   confirm `/r/<slug>` redirects.
6. Set a batch password and a Starts/Expires window, submit two more rows.
   **Pass:** both new rows show the Password badge and the window columns; a
   `curl -sI http://localhost:3000/r/<slug>` returns the password prompt.
7. Paste a full-cap batch of 50 generated rows
   (`for i in $(seq 50); do echo "bulk-$i,https://example.com/$i"; done`) and
   submit, timing it. **Pass:** 201 and 50 new rows in the table, comfortably
   fast. Record the wall-clock time. This is **confirmation, not a gate** — the
   cap is not going to move based on it. But if a 50-row submission takes more
   than a couple of seconds, stop and report it loudly: at this size that would
   indicate a real problem in the per-row KV path, not a cap that is too high.
   Then paste 120 rows. **Pass:** nothing is created, the textarea keeps its
   contents, and the error names both numbers and the remedy — "this file has
   120 and the limit is 50 per submission. Split it into 3 smaller batches…".
8. Paste a deliberately broken batch — line 1 `bad-row` (no destination), line 2
   `black-friday,https://example.com/x` (already taken), line 3
   `dup,https://a.com`, line 4 `dup,https://b.com`, line 5
   `ok-slug,not-a-url`. **Pass:** five rows reported with those exact line
   numbers and reasons, and `GET /api/links` shows no new links whatsoever.
9. Save the file `bulk.csv` with a Windows CRLF line ending and a UTF-8 BOM,
   choose it with the file picker. **Pass:** the textarea fills with its
   contents and submitting produces the same result as pasting it. Then try a
   file larger than 256 KB. **Pass:** rejected client-side with the limit named,
   without the page hanging.
10. Filter the table to a subset, click the header checkbox. **Pass:** only
    filtered rows are checked and the bar reads the filtered count. Change the
    filter. **Pass:** the selection clears and the bar disappears. Repeat for a
    sort-header click.
11. Select 3 links, click **Disable**. **Pass:** no confirmation, all three
    Status badges read `disabled`, `#links-success` reads "Disabled 3 links.",
    the selection clears, and `curl -sI http://localhost:3000/r/<slug>` for one
    of them behaves as a disabled link. Click **Enable** on the same 3.
    **Pass:** back to `active`.
12. Select 1 link, **Delete**. **Pass:** the dialog reads exactly `Delete the
    link "<slug>"? This can't be undone.` Cancel; nothing is deleted. Select 3,
    **Delete**. **Pass:** the dialog reads `Delete 3 links? …` with a "Delete 3
    links" confirm button; confirming removes exactly those three and no others.
13. In a second browser profile, log in as a non-admin user without
    `links.edit_all` who owns one link. **Pass:** checkboxes render only on
    their own row, select-all checks only that row, and
    `curl -X POST .../api/links/bulk-action` with someone else's slug returns
    400 with a `forbidden` row error and changes nothing.
14. Resize to 390px. **Pass:** the select column is hidden, the visible columns
    are Short link / Status / Expires / actions exactly as before this change,
    and the sticky action column does not occlude Expires.
15. Switch to the dark theme via the nav control and repeat steps 5, 8 and 11.
    **Pass:** the bulk bar's border, the error table and the textarea are all
    legible, and the console shows zero CSP violations.
16. Open the KV explorer (`./dev/kv-explorer-up.sh`, credentials per
    `CLAUDE.md`) and inspect `all_links` after a bulk create and a bulk delete.
    **Pass:** the index contains exactly the live slugs, with no duplicates and
    no entries whose `slug:` record is missing.

## Out of scope / follow-ups

- **CSV export of the links table** — deferred by the user's decision 4 and
  added to `TASKS.md`'s "Future work (not scheduled)". It needs none of this
  plan's machinery (no selection UI, no API): it is a client-side
  `Blob`/`download` over the already-fetched `allLinks`. Pick it up when someone
  asks to get data *out*.
- **A single-row status toggle.** This feature makes bulk enable/disable the
  only way to change a link's status from the GUI, so disabling one link means
  selecting it and using a "bulk" action. That is odd enough to be worth fixing,
  but the user's non-goals forbid changing the single-link edit flow here.
  Added to Future work.
- **Bulk editing of destinations or slugs after creation**, and **bulk schedule
  changes on existing links.** A natural next step once selection exists — the
  bar would grow a "Set schedule…" action reusing this plan's batch controls —
  but explicitly a non-goal now. Added to Future work with undo.
- **Undo for bulk delete.** Would need a tombstone record and a retention
  policy; the count-bearing confirmation is the mitigation instead.
- **Any change to the single-link create/edit/delete flows** beyond routing them
  through the shared validation and index helpers, which is behaviour-preserving
  by construction and covered by the existing 135 tests.
- **New auth or permission concepts.** Bulk create gates on
  `links.create_custom_slug` exactly as single create does; bulk actions gate on
  `links.can_edit` exactly as single edit/delete do.
- **Progress reporting / streaming for large submissions.** The 50-row cap
  exists so a submission is a single fast request, and at that size a progress
  indicator would be gone before it rendered. If verification step 7 somehow
  measures something slow enough to want one, the answer is to find out why 50
  rows is slow — not to add a streaming protocol.
- **Selecting and acting on more than 50 rows at once.** Above the cap the
  action buttons disable and say so; the workaround is to filter the table down
  and act in batches. Raising the cap is a deliberate decision requiring the
  timing evidence named under Trade-offs, not a response to the first person who
  finds the limit inconvenient.
