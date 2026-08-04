# Link Tags and Owner Reassignment

## Context

Two gaps, requested together because they share one surface (the dashboard's
existing selection + bulk bar) and one risk (KV index writes with no
transaction).

**Tags.** A link record today carries `slug`, `target_url`, `owner`, `custom`,
`password_hash`, `status`, `start_at`, `end_at`, `created_at`, `updated_at`
(`api/links.py:180-191`) — and nothing that groups links. The dashboard's only
grouping affordance is `#links-filter`, a substring match over slug and
destination (`gui/dashboard.js:132-138`). That works if a campaign's links all
share a slug prefix and fails otherwise, which is the normal case for
auto-generated slugs. `TASKS.md`'s bulk-link-management entry records the
originating persona finding — "Alex/power-user... managing dozens of campaign
links one row at a time" — and bulk management shipped without any way to
*describe* which links belong to a campaign.

**Owner reassignment.** `owner` is set once, at creation, from
`principal.username`, and no endpoint can ever change it. `UPDATABLE_FIELDS` in
both `api/links.py:219` and `api/users.py:120` omit it. When an employee leaves
and their account is deleted (`users.handle_delete`, which removes
`user:<username>` and the `_meta:usernames` entry but touches **nothing** in the
`links` store), their links keep resolving, keep an `owner` naming a user record
that no longer exists, and keep an `owner_links:<departed>` index nobody can
reach. Today the only recovery is deleting and recreating each link under a new
account — which changes every slug and invalidates every printed QR code.

There is **no existing `TASKS.md` Future-work entry for either half** —
confirmed by reading the whole `## Future work (not scheduled)` section
(TASKS.md:259-289) and by `grep -in "tag\b|tags|reassign|transfer" TASKS.md`,
whose only hits are the word "tag" inside unrelated prose about `<script>` tags.
The closest adjacent entry is "Bulk editing of existing links — destination,
schedule, and slug — plus undo for bulk delete", which explicitly covers
different fields and says to re-confirm scope. So the constraints below come
from the requester, not from prior reasoning in the repo.

**Confirmed decisions** (settled by the user before planning; recorded so a
future reader knows they were deliberate, not defaults that drifted in):

1. **A new `links.tag` permission**, following the existing granular model
   (`links.view_all`, `links.edit_all`, `users.manage`) rather than an
   `admin`-role check. Tagging your own links needs no extra permission;
   `links.tag` gates bulk tag operations. Bulk actions still respect existing
   ownership scoping.
2. **Owner reassignment is in scope, gated on the existing `users.manage`**,
   for both single-link and multi-link. Motivating case: an employee leaves and
   their links are orphaned.
3. **Tags are free-form with autocomplete.** Any user can type a new tag; the UI
   suggests existing ones. Normalize (lowercase) and validate the character set.
   No admin-curated vocabulary.
4. **Bulk-by-tag = filter, then use the existing bulk bar.** Selecting a tag
   filters the dashboard table; the shipped select-all checkbox and
   Enable/Disable/Delete bar (`gui/dashboard.html`'s `#bulk-bar`) act on the
   selection. **No separate "delete all 300 links tagged X" server-side action**
   — seeing-before-hitting was chosen over raw power.
5. Multiple tags per link with a per-link cap; lowercase, bounded character set,
   bounded length (numbers picked and justified below).
6. The bulk-create panel gets a batch tags field, exactly like the existing
   start/end/password fields.
7. CSV export gains a tags column.
8. The tag filter respects existing ownership scoping.
9. Deleting or clearing a tag untags links; it never deletes them.

### One interpretation call the builder should know about

Decisions 1 and 4 are in tension on their face: `links.tag` "gates bulk-acting
on a whole tag", but decision 4 removes any server-side whole-tag action. The
only reading that makes both simultaneously true, and the one this plan is built
on:

> **`links.tag` gates `POST /api/links/bulk-action` with `action` in
> `{"tag", "untag"}`** — applying or removing tags across an explicit,
> client-supplied, ≤50-slug selection. Setting tags on a single link through
> the create form, the row edit form, or `PATCH /api/links/{slug}` needs only
> the edit rights that link already required.

The two rejected readings are each directly contradicted: gating all tagging
contradicts "tagging your own links requires no extra permission", and gating a
server-side whole-tag endpoint contradicts decision 4. This is called out here
and in the task line so it can be corrected in one place if it is wrong.

## Key technical facts confirmed during research

- **Go's `encoding/json` ignores unknown object keys by default, so adding a
  `tags` field to the link record cannot break the redirect hot path.**
  Confirmed via `go doc encoding/json Decoder.DisallowUnknownFields` in
  `redirect/`: *"DisallowUnknownFields causes the Decoder to return an error
  when the destination is a struct and the input contains object keys which do
  not match any non-ignored, exported fields in the destination."* The existence
  of that opt-in is the statement that the default does not error.
  `redirect/linkgate/link.go:25-31`'s `ParseLink` uses plain `json.Unmarshal`
  with no `Decoder`, so it takes the default. **`linkgate.Link` deliberately
  does not gain a `Tags` field** — the hot path never reads tags, and an unused
  field is one more allocation per click. A regression test is added instead.

- **`redirect` never reads any KV key but `slug:<slug>` and the two analytics
  keys.** `redirect/main.go` is 167 lines; the only `kv` opens are `links` and
  `analytics`, and resolution is `slug:{slug}` → `ParseLink` → 302. Tags add
  zero KV reads to the hot path. (Same property `CLAUDE.md`'s "Multi-domain
  display" section relies on.)

- **No new KV key type is introduced by this plan, so `api/backup.py` needs no
  code change.** Tags live inside the `slug:<slug>` value, which
  `backup.build_backup` already base64-encodes verbatim
  (`api/backup.py:110-116`), and `is_excluded_key` returns `False` for
  everything outside the `users` store (`api/backup.py:84-89`). Reassignment
  creates at most a new `owner_links:<newowner>` key, already covered by
  `OWNER_LINKS_PREFIX` in `INDEX_KEYS`/`restore_write_order`
  (`api/backup.py:37-42, 196-208`). **This is asserted with a new test, not
  assumed** — see the backup task. **If a future change adds a `tag:` key type,
  `INDEX_KEYS["links"]` and `restore_write_order` must be updated in the same
  commit or restore will silently drop or mis-order it.**

- **`users.manage` is already equivalent to `admin`, so gating reassignment on
  it is not a weaker bar.** `api/users.py:123-149`: `handle_update` requires
  `users.manage` and then lets the caller set `role` on *any* username including
  their own, with no self-exclusion (only `cannot_disable_self` at line 161 and
  `cannot_delete_self` at line 182 exist). A `users.manage` holder can therefore
  already promote themselves to `admin` and obtain every permission. This is the
  same argument `docs/plans/kv-backup-restore.md` used to gate the backup
  endpoints, and it is why reassignment does **not** additionally require
  per-row `can_edit` (see "Trade-offs").

- **`links.remove_slugs_from_indexes` cannot be reused for reassignment.**
  `api/links.py:69-82` rewrites `all_links` as well as each owner index. A
  reassignment does not change `all_links` membership, so calling it would
  delete the slugs from the global index. A dedicated owner-index-only helper is
  required — this is the single most likely way to get this feature wrong.

- **`handle_bulk_action` has no access to the `users` store today.**
  `api/app.py:102-107` opens only `links` before calling it. Verifying a
  reassignment target exists needs `users_store`, which `app.py` already holds
  in scope from line 57.

- **The nav is full and this feature adds no page and no nav item.** No new
  `spin.toml` route, no `gui-pages/routing.py` `ROUTES` entry, no
  `test_routing.py` case. Confirmed by design: every control lands in
  `gui/dashboard.html` (already routed), `gui/links/detail.html` (already
  routed) and `gui/admin/users.js` (already routed). `gui-pages`'s auto-derived
  test count therefore stays at exactly 64 — `PAGES` comes from `ROUTES` and
  `SCRIPTS` from a `gui/**/*.js` glob (`gui-pages/tests/test_no_inline_code.py:25,41-45`),
  and this plan adds neither a page nor a `.js` file.

- **Baseline, measured now at `f7dbb0e`:** `cd api && uv run pytest` → **289
  passed**; `cd gui-pages && uv run pytest` → **64 passed**; `cd redirect && go
  test ./linkgate/...` → **ok**.

- **DESIGN.md's Pill-Is-For-Links Rule forbids a pill-shaped tag chip.**
  `DESIGN.md:197`: *"Full pill rounding (999px) is reserved for the slug chip…
  introducing a second pill-shaped element would dilute what the pill means."*
  The tag chip therefore reuses `.slug-kind-badge`/`.lock-badge`'s exact
  treatment (`gui/theme.css:569-583`): plain text, `--ss-slate-500`, `0.75rem`,
  `font-weight: 600`, sans-serif (which is also the opt-out from the mono rule
  the slug cell applies at `theme.css:554-563`). **Zero new tokens, zero new
  shapes, zero new contrast measurements** — `--ss-slate-500` was already
  measured at 5.6:1 against the table cell background (`theme.css:124-127`).

- **DESIGN.md's 44px tap-target floor covers `button` and `[role=button]`
  sitewide** (`DESIGN.md:203`). This is why in-row tag chips are non-interactive
  `<span>`s and the filter affordance is a `<select>` — see "Trade-offs".

- **`#app-header nav li { color: #fff }` at specificity (1,0,2) has silently
  beaten later class rules three times** (`DESIGN.md:235`, and the Don'ts
  section warns a third time). Not applicable here: **this plan touches no nav
  markup and no `#app-header` CSS.** Recorded so the builder can confirm that
  rather than wonder.

- **UNCONFIRMED: nothing.** Every claim above is cited to a file+symbol, a
  command's output, or a measured baseline. The one thing that cannot be
  confirmed by reading is the live browser behavior of the datalist
  prefix-rewrite trick (see "GUI changes"), which is why it is graceful-degrading
  by construction and is exercised in the end-to-end task rather than asserted
  in a unit test.

## Data model

### Link record

One new field on `slug:<slug>`:

```json
{
  "slug": "black-friday",
  "target_url": "https://example.com/sale",
  "owner": "alice",
  "custom": true,
  "password_hash": null,
  "status": "active",
  "start_at": null,
  "end_at": null,
  "tags": ["email", "q4", "sale"],
  "created_at": "2026-08-03T12:00:00Z",
  "updated_at": "2026-08-03T12:00:00Z"
}
```

- **Always stored normalized, de-duplicated and sorted.** Sorted because a
  link's tags have no meaningful order, and sorting makes the table, the CSV
  column, and a backup-file diff deterministic.
- **No backfill.** A record written before this change simply has no `tags` key,
  exactly like `assigned_domains` on user records (`CLAUDE.md`, Multi-domain
  display: *"Absent or `[]` means unrestricted… no existing user record needs a
  backfill"*). `public_link` synthesizes `tags: []` for the wire (below), so
  clients never see the field missing; KV is only written on the next edit.

### Tag vocabulary — the numbers, and why

| Constant | Value | Reason |
|---|---|---|
| `TAG_PATTERN` | `^[a-z0-9][a-z0-9_-]*$` | The same alphabet as `links.CUSTOM_SLUG_PATTERN` (`api/links.py:19`) minus uppercase, plus a leading-alphanumeric requirement. Leading `-` is excluded because a value like `-lead` reads as a flag in a CSV or a shell and looks like a typo. Keeping the alphabet slug-shaped means a tag is safe unquoted in a CSV field, a JSON string, a `CSS.escape` target, and a future `?tag=` query parameter, with no per-surface escaping question. |
| `MAX_TAG_LENGTH` | `32` | Matches `CUSTOM_SLUG_PATTERN`'s upper bound — one number to remember across the app. 32 characters is already too wide for a chip in a table cell that `dashboard.css:37-58` documents as the app's tightest. |
| minimum length | `1` | Deliberately **not** the slug's 3. `q3`, `pr`, `us` are legitimate tags, and unlike a slug there is no globally-unique, typeable-URL namespace to protect. |
| `MAX_TAGS_PER_LINK` | `10` | A layout cap, not a storage cap. The tags render inside the Short-link cell of a table whose column widths `dashboard.css` spends 60 lines defending; ten 32-character tags is 320 characters in one cell, well past what that layout tolerates. Five tags covers the realistic campaign axes (channel, quarter, region, team, campaign) with headroom; anyone needing more than ten is using tags as a database. It also bounds the CSV column width and the link record's size. |

Normalization is `value.strip().lower()`, applied **before** the pattern check,
so `"  Sale  "` → `"sale"` passes and `"Café"` → `"café"` is rejected by the
ASCII-only pattern. `.lower()` on an already-ASCII-restricted candidate has no
locale surprises.

### KV key layout: unchanged

**This plan introduces no new KV key.** The `links` store keeps exactly
`slug:<slug>`, `all_links`, and `owner_links:<username>`. There is no
`tag:<tag>` index and no `_meta:tags` registry — see "Trade-offs" for the full
argument, which is the single most consequential decision in this document.

## API changes

All Python, all in the `api` component. This follows the language-split rule
without ambiguity: none of it is on the `/r/...` hot path, so it defaults to
Python for velocity (`CLAUDE.md`, "Why Go for `redirect` but Python for
`api`/`gui-pages`"). The one Go change in the whole feature is a test.

### New module: `api/tags.py`

Pure. Zero `spin_sdk` imports, zero imports from `links.py` (so `links.py` can
import it with no cycle). Follows the established testability rule: dependencies
as plain parameters, `Request`/`Response` from `responses` if ever needed
(it needs neither).

```python
MAX_TAGS_PER_LINK = 10
MAX_TAG_LENGTH = 32
TAG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

def normalize_tag(value: str) -> str:
    """strip() + lower(). No validation — is_valid_tag does that."""

def is_valid_tag(tag: str) -> bool:
    """True for an already-normalized tag of 1..MAX_TAG_LENGTH characters
    matching TAG_PATTERN."""

def parse_tags(value, *, allow_none: bool = True) -> tuple[list[str] | None, dict | None]:
    """(tags, None) on success or (None, error_body) on the first problem —
    the same all-or-nothing (value, error_body) shape as
    backup.parse_stores_param. Normalizes, validates, de-duplicates and
    sorts. `None` -> ([], None) when allow_none, else the invalid_tags error.

    Error bodies:
      {"error": "invalid_tags"}                       non-list, or a non-string member
      {"error": "invalid_tag", "tag": <as submitted>} a member failing is_valid_tag
      {"error": "too_many_tags", "max_tags": 10}      more than MAX_TAGS_PER_LINK distinct
    """

def apply_tags(existing: list[str], add: list[str]) -> list[str]:
    """Union, de-duplicated and sorted. Cap enforcement is the caller's, so
    the caller can report which link overflowed."""

def remove_tags(existing: list[str], remove: list[str]) -> list[str]:
    """Difference, sorted. Removing a tag a link does not carry is a no-op,
    not an error."""
```

`invalid_tag` carries the tag **as submitted** (pre-normalization) so the error
message can echo what the user typed.

> **Naming hazard for the builder:** `links.py` and `bulk.py` will `import
> tags`. Never bind a local variable named `tags` in those modules — use
> `tag_list`. A shadowed module import fails at the *second* call site in a
> function, not the first, which is a genuinely confusing failure.

### `api/links.py`

Reuses `tags.parse_tags`, `auth.Principal.has_permission`, `can_edit`,
`json_response`, `iso_now`. Changes:

- `import tags`
- `UPDATABLE_FIELDS = {"target_url", "status", "start_at", "end_at", "tags"}`
- `public_link(record)` gains one line:
  ```python
  public["tags"] = record.get("tags", [])
  ```
  Synthesized on the wire only, exactly like the existing `password_protected`
  flag two lines above it (`api/links.py:113-117`). Nothing is written to KV, so
  no record is migrated, but every client can rely on `link.tags` being an
  array.
- `handle_create` — after the window checks, before building `record`:
  ```python
  tag_list, tag_error = tags.parse_tags(payload.get("tags"))
  if tag_error:
      return json_response(400, tag_error)
  ```
  and `"tags": tag_list` in the record dict.
- `handle_update` — inside the existing field loop:
  ```python
  if "tags" in payload:
      tag_list, tag_error = tags.parse_tags(payload["tags"], allow_none=False)
      if tag_error:
          return json_response(400, tag_error)
      record["tags"] = tag_list
  ```
  **Full replacement, not a merge** — the same contract `users.handle_update`
  already uses for `permissions` and `assigned_domains` (`api/users.py:145-155`).
  `allow_none=False` here because `PATCH {"tags": null}` is a malformed request,
  whereas "clear all tags" is `PATCH {"tags": []}`, which is accepted.
- **New helper**, the owner-index-only counterpart to
  `add_slugs_to_indexes`/`remove_slugs_from_indexes`:

  ```python
  async def move_slugs_between_owners(
      store, slugs_by_old_owner: dict[str, list[str]], new_owner: str
  ) -> None:
      """Reassignment's index half. One read+write of owner_links:<new_owner>,
      plus one per distinct old owner — the same one-read-one-write-per-index
      shape as add_slugs_to_indexes, because Spin KV has no compare-and-swap
      and a per-slug read-modify-write would multiply the race window by N.

      Deliberately never touches ALL_SLUGS_INDEX_KEY: a reassignment does not
      change all_links membership, and calling remove_slugs_from_indexes here
      would strip the slugs from it entirely.

      Adds to the new owner FIRST, then removes from each old owner, and skips
      any old owner equal to new_owner (without that guard a same-owner
      "reassignment" removes the slugs from the index it just added them to).
      """
  ```

  Both halves are idempotent — `add_slugs_to_indexes` skips slugs already
  present (`api/links.py:57-59`) and the removal is a set difference — so
  re-running an interrupted reassignment converges rather than compounding.

### `api/bulk.py`

Reuses `links.can_edit`, `links.get_link`, `links.remove_slugs_from_indexes`,
the existing `MAX_BULK_ROWS`/`MAX_BULK_BODY_BYTES` caps, the existing
all-or-nothing validate-then-write structure, and `auth.get_user`.

**Batch tags on bulk create.** `handle_bulk_create` gains, alongside the
existing batch `password`/`start_at`/`end_at`:

```python
tag_list, tag_error = tags.parse_tags(payload.get("tags"))
if tag_error:
    return json_response(400, tag_error)
```

and `"tags": tag_list` in the per-row record dict. Applied to every link created
in the submission, not per-row — identical to how the batch password and window
already work. **The pasted-text format is unchanged**: a third column is
explicitly out of scope (`CLAUDE.md`: the parser is first-delimiter-wins on two
columns, so a third would land inside the destination and fail as an invalid
URL).

**Three new bulk actions.** `handle_bulk_action` grows from
`{"delete"} | ACTION_STATUSES.keys()` to:

```python
BULK_ACTIONS = {"delete", "enable", "disable", "tag", "untag", "reassign"}
```

and its signature gains the users store, which only the `reassign` branch reads:

```python
async def handle_bulk_action(store, users_store, principal, request):
```

`api/app.py:102-107` passes `users_store` (already in scope from line 57). A
required positional, not an optional keyword defaulting to `None` — an
unused-unless-reassign `None` is an `AttributeError` waiting for the first
reassign request.

The handler's order of operations, extending what is already there:

1. Parse JSON; validate `action` against `BULK_ACTIONS` (`400 invalid_action`).
2. Validate `slugs` — list, non-empty, all strings, no duplicates, ≤
   `MAX_BULK_ROWS`. Unchanged, and the cap now covers the new actions too.
3. **Action-specific payload validation**, before any permission check:
   - `tag`/`untag`: `tag_list, err = tags.parse_tags(payload.get("tags"),
     allow_none=False)`; then `if not tag_list: return json_response(400,
     {"error": "no_tags"})`.
   - `reassign`: `owner = payload.get("owner")`; must be a non-empty string
     (`400 invalid_owner`) and `await auth.get_user(users_store, owner)` must
     not be `None` (`400 {"error": "unknown_owner", "owner": owner}`). A
     **disabled** user is an acceptable target — parking a departed employee's
     links on a disabled account is legitimate, and the links keep resolving
     either way.
4. **Action-level permission gate**, returning the same 403 body shape
   `links.py` uses:
   - `tag`/`untag` → `principal.has_permission("links.tag")`, else
     `403 {"error": "forbidden", "required_permission": "links.tag"}`
   - `reassign` → `principal.has_permission("users.manage")`, else
     `403 {"error": "forbidden", "required_permission": "users.manage"}`
   - `delete`/`enable`/`disable` → none, unchanged.
5. **Per-row fetch and gate**, the existing loop, with one exception:
   `not_found` applies to every action; the `can_edit` `forbidden` row error
   applies to every action **except `reassign`** (see "Trade-offs" for why).
6. For `tag`, one extra per-row check before any write:
   ```python
   if len(tags.apply_tags(record.get("tags", []), tag_list)) > tags.MAX_TAGS_PER_LINK:
       row_errors.append({"slug": slug, "error": "too_many_tags",
                          "max_tags": tags.MAX_TAGS_PER_LINK})
   ```
7. Any `row_errors` → `400 bulk_validation_failed`, nothing written. Unchanged,
   and this is what makes the cap check in step 6 all-or-nothing.
8. Write.

### Write ordering and what a partial failure leaves behind

Spin KV has no atomic operations and no compare-and-swap (`CLAUDE.md`, Security
tradeoffs). Every multi-write path below is therefore analyzed for the state an
interruption leaves, following the house rule from `CLAUDE.md`'s Bulk link
management section: **records first, indexes last, in both directions.**

**`tag` / `untag` — one write per link, no index writes at all.**
```
for each slug: record["tags"] = apply_tags/remove_tags(...)
               record["updated_at"] = now
               store.set(f"slug:{slug}", ...)
```
There is no tag index, so there is nothing to keep in step with the records.
An interruption leaves some links tagged and some not — every one of them
individually consistent, visible in the table, and fixed by re-running the same
action (both `apply_tags` and `remove_tags` are idempotent). **This is the
mildest failure mode in the feature, and it is the direct payoff of the
no-tag-index decision.**

**`reassign` — records first, then the owner indexes.**
```
1. for each slug: record["owner"] = new_owner
                  record["updated_at"] = now
                  store.set(f"slug:{slug}", ...)
2. await links.move_slugs_between_owners(store, slugs_by_old_owner, new_owner)
      2a. owner_links:<new_owner>  += slugs      (add first)
      2b. owner_links:<old>        -= slugs      (per distinct old owner ≠ new_owner)
```

| Interrupted at | State left behind | Who notices | Recovery |
|---|---|---|---|
| Partway through 1 | Some links moved, each individually consistent | Nobody; both dashboards show the link, one with a stale Owner column | Re-run — idempotent |
| After 1, before 2 | Records say `new_owner`; `owner_links:<old>` still lists them, `owner_links:<new>` does not | The old owner still sees them (with the Owner column reading the new owner); the new owner does not, unless they hold `links.view_all`/`links.edit_all` | Re-run the identical request; step 1 is a no-op, step 2 completes |
| Between 2a and 2b | Slug is in **both** owner indexes | Both users see the link; `handle_list` shows it once each | Re-run — 2a skips, 2b completes |

`all_links` is never touched, so **the link is never invisible to a
`links.view_all` holder and never stops resolving at `/r/<slug>`** at any point.
That is the property that makes "add first, remove second" the correct ordering:
the reverse (remove first) has a window where the slug is in *neither* owner
index, leaving the link visible only to users with a global view permission.
The chosen ordering's worst case is a duplicate — two people see one link — and
a duplicate is strictly better than a disappearance.

One residual, worth knowing rather than fixing: if a reassignment is interrupted
between 2a and 2b and the link is later deleted, `remove_slugs_from_indexes`
uses `record["owner"]` (the new owner) and so leaves the stale entry in the old
owner's index. `handle_list` already skips any slug whose record is `None`
(`api/links.py:203-206`), so the visible effect is nothing at all — the same
tolerance bulk delete already relies on.

**`bulk create` with batch tags** — unchanged ordering: every `slug:<slug>`
record (now carrying `tags`) before the single `add_slugs_to_indexes` call.
Tags add no index write, so the existing analysis in `CLAUDE.md` still holds
verbatim.

### API surface summary

| Method | Path | Change | Gate |
|---|---|---|---|
| `GET` | `/api/links` | Every link now carries `tags` (synthesized `[]` for legacy records) | unchanged |
| `GET` | `/api/links/{slug}` | Same | unchanged |
| `POST` | `/api/links` | Accepts `tags: [...]` | unchanged (session) |
| `PATCH` | `/api/links/{slug}` | Accepts `tags: [...]` (full replacement) | `can_edit` |
| `POST` | `/api/links/bulk` | Accepts batch `tags: [...]` | unchanged (session) |
| `POST` | `/api/links/bulk-action` | `action: "tag"\|"untag"` + `tags: [...]` | `links.tag` **and** per-row `can_edit` |
| `POST` | `/api/links/bulk-action` | `action: "reassign"` + `owner: "<username>"` | `users.manage` (no per-row `can_edit`) |

**No new routes.** `api/app.py`'s only change is threading `users_store` into
the existing `/api/links/bulk-action` branch.

New error codes: `invalid_tag`, `invalid_tags`, `too_many_tags`, `no_tags`,
`invalid_owner`, `unknown_owner`.

Success bodies keep the existing shape and gain the action's parameter, so a
client never has to re-derive it:
```json
{"ok": true, "action": "tag",      "count": 12, "tags": ["q4","sale"]}
{"ok": true, "action": "reassign", "count": 12, "owner": "bob"}
```

### `api/auth.py`

One line:
```python
KNOWN_PERMISSIONS = frozenset({
    "links.create_custom_slug", "links.view_all", "links.edit_all",
    "links.tag", "users.manage",
})
```
Nothing else in `auth.py` changes. `users._validate_permissions`
(`api/users.py:27-33`) validates against this set, so the admin Users page can
grant the new permission the moment the constant lands.

## Redirect (Go) changes

**One test, no production code.** `redirect/linkgate/link_test.go` gains a case
asserting `ParseLink` tolerates and ignores a `tags` array:

```go
func TestParseLinkIgnoresUnknownTagsField(t *testing.T) {
	raw := []byte(`{"slug":"abc","target_url":"https://example.com",` +
		`"owner":"alice","status":"active","tags":["sale","q4"]}`)
	l, err := ParseLink(raw)
	if err != nil {
		t.Fatalf("ParseLink returned an error for a record with tags: %v", err)
	}
	if l.Slug != "abc" || l.TargetURL != "https://example.com" || l.Status != "active" {
		t.Fatalf("known fields did not survive: %+v", l)
	}
}
```

`linkgate.Link` deliberately gains **no** `Tags` field: the hot path never reads
tags, and an unused slice field is one more allocation per click. The test is
what stops someone "helpfully" adding one, and what proves the redirect path was
considered rather than forgotten.

Verified with `cd redirect && go test ./linkgate/...` — **never** `go test
./...`, `go build ./...` or `go vet ./...`, which fail by design on `package
main` (`wit_exports.go:934:6: missing function body`).

## GUI changes

No new page, no new route, no new nav item, no new `.js` or `.css` file. Every
change lands in files that already exist and are already routed.

### `gui/dashboard.html`

1. **Create form**, inside `#advanced-options`, after the password field:
   ```html
   <label for="link-tags">
     Tags (optional, comma-separated)
     <input id="link-tags" name="tags" type="text" list="tag-suggestions"
            placeholder="sale, q4, email" />
   </label>
   <datalist id="tag-suggestions"></datalist>
   ```
   One `<datalist>` for the whole page, referenced by `list="tag-suggestions"`
   from the create input, every edit-row input, the bulk-create input, and the
   bulk-bar input.
2. **Bulk-create panel**, inside the existing
   `<fieldset><legend>Applies to every link in this batch</legend>` block,
   after `#bulk-password`:
   ```html
   <label for="bulk-tags">Tags (optional, comma-separated)
     <input type="text" id="bulk-tags" list="tag-suggestions" placeholder="sale, q4" />
   </label>
   ```
3. **Filter row**, immediately after `#links-filter`:
   ```html
   <label for="tag-filter" class="visually-hidden">Filter by tag</label>
   <select id="tag-filter"><option value="">All tags</option></select>
   ```
   and `#links-filter`'s placeholder becomes
   `Filter by short link, destination, or tag...`.
4. **Bulk bar**, two new groups inside `#bulk-bar` after the existing
   `role="group"`, both `hidden` by default and revealed by permission:
   ```html
   <div id="bulk-tag-controls" role="group" hidden>
     <input type="text" id="bulk-tag-input" list="tag-suggestions"
            aria-label="Tags to add or remove" placeholder="tag" />
     <button type="button" id="bulk-tag-add-btn" class="outline">Add tag</button>
     <button type="button" id="bulk-tag-remove-btn" class="outline">Remove tag</button>
   </div>
   <div id="bulk-owner-controls" role="group" hidden>
     <select id="bulk-owner-select" aria-label="Reassign selected links to"></select>
     <button type="button" id="bulk-reassign-btn" class="outline">Reassign</button>
   </div>
   ```
   `#bulk-bar` already has `flex-wrap: wrap` (`dashboard.css:153-162`), so the
   extra controls wrap rather than overflow. `[hidden] { display: none
   !important; }` in `theme.css` is what makes `hidden` win against Pico's
   `display` on these elements — the `!important` is load-bearing here for the
   same reason `CLAUDE.md` records it being load-bearing elsewhere.

**Zero inline code.** No `<script>`, no `<style>`, no `style="`, no
`on<event>=` — including inside comments, which
`gui-pages/tests/test_no_inline_code.py` also scans.

### `gui/theme.css`

Exactly two selector-list edits, no new declarations and **no new token**: add
`.tag-chip` to both existing groups at lines 569-583, so it inherits the
`.slug-kind-badge`/`.lock-badge` treatment (sans-serif opt-out from the mono
cell rule, `0.75rem`, `font-weight: 600`, `--ss-slate-500`, `margin-left:
0.35em`, `vertical-align: middle`).

```css
.slug-kind-badge,
.lock-badge,
.tag-chip { … }        /* both occurrences */
```

Rendered text carries a `#` prefix — `#sale` — which distinguishes a tag from
the neighbouring `Custom`/`Password` badges using typography that already
exists, rather than a second colour or a second shape. The `#` is **display
only** and is never part of the stored tag.

### `gui/dashboard.js`

Reuses `api.get/post/patch`, `escapeHtml`, `friendlyError`, `confirmDialog`,
`canEditLink`, `renderRowErrorList`, `BULK_MAX_SELECTION`, `getVisibleLinks`,
`updateBulkBar`, `updateSelectAllState`, `csvField`.

- **State:** `let allUsernames = [];` — populated in `loadMe()` from
  `GET /api/users` **only** when the principal has `users.manage` (that endpoint
  403s otherwise, and `api.get` would surface it as `ok: false`, which is
  handled by leaving the list empty and the owner controls hidden).
- **Permission mirrors**, alongside the existing `canEditLink`:
  ```js
  function canTagLinks()   { return !!currentPrincipal && (currentPrincipal.role === "admin" || currentPrincipal.permissions.includes("links.tag")); }
  function canManageUsers(){ return !!currentPrincipal && (currentPrincipal.role === "admin" || currentPrincipal.permissions.includes("users.manage")); }
  ```
- **`parseTagsInput(value)`** — split on `,`, trim, lowercase, drop empties,
  de-duplicate, return an array. Mirrors `tags.parse_tags`'s normalization so
  the common case never round-trips to a 400; the server stays authoritative
  (same "server is authoritative" comment convention as `BULK_MAX_SELECTION` at
  `dashboard.js:77-82`).
- **`allKnownTags()`** — the sorted distinct union of `link.tags ?? []` across
  `allLinks`. **This is the autocomplete source** — see "Trade-offs" for why
  there is no server-side registry.
- **`rebuildTagFilterOptions()`** — repopulates `#tag-filter` from
  `allKnownTags()`, preserving the current selection if it still exists. Called
  from `loadLinks()` after `allLinks` is assigned.
- **`refreshTagDatalist(input)`** — rebuilds `#tag-suggestions` on each `input`
  event of a tags field. `<datalist>` matches against the input's **whole**
  value, so in a comma-separated field it stops helping after the first tag.
  The fix is to prefix each option with everything up to and including the last
  comma:
  ```js
  function refreshTagDatalist(input) {
    const prefix = input.value.slice(0, input.value.lastIndexOf(",") + 1);
    const already = new Set(parseTagsInput(input.value));
    document.getElementById("tag-suggestions").innerHTML = allKnownTags()
      .filter((t) => !already.has(t))
      .map((t) => `<option value="${escapeHtml(prefix + (prefix ? " " : "") + t)}"></option>`)
      .join("");
  }
  ```
  Degrades to a plain text input if a browser ignores any of it — nothing about
  submission depends on the datalist.
- **`getVisibleLinks()`** — the text term now also matches tags, and the tag
  select ANDs with it:
  ```js
  const tag = document.getElementById("tag-filter").value;
  if (tag) visible = visible.filter((l) => (l.tags ?? []).includes(tag));
  ```
  Ownership scoping needs no work: `allLinks` is already exactly what
  `handle_list` returned for this principal, so a user without `links.view_all`
  filtering by a tag sees only their own links with it, by construction.
- **`renderLinksTable()`** — tag chips render in the **existing Short-link
  cell**, after the Custom/Password badges:
  ```js
  ${(link.tags ?? []).map((t) => `<span class="tag-chip">#${escapeHtml(t)}</span>`).join("")}
  ```
  **No new column.** `dashboard.css`'s `@media (max-width: 600px)` block hides
  columns by `nth-child` index and `dashboard.js:326-328` already carries a
  comment about those indices shifting once and failing silently; a ninth column
  would shift them again and would also require changing every `colspan="9"`.
  This keeps all of that untouched.
- **`editRowHtml(link)`** — two additions to the form:
  ```html
  <label>Tags <input type="text" class="edit-tags" list="tag-suggestions"
                     value="…joined with ', '…" /></label>
  ```
  and, only when `canManageUsers()`, an owner select built from `allUsernames`
  with `link.owner` selected:
  ```html
  <label>Owner <select class="edit-owner">…</select></label>
  ```
- **`handleEditFormSubmit(form)`** — `tags: parseTagsInput(...)` joins the
  existing PATCH payload. Owner is a **separate** call after the PATCH
  succeeds, exactly mirroring how the password change is already a second call
  (`dashboard.js:334-348`), including its "Destination and schedule saved."
  prefix convention on a partial failure:
  ```js
  if (ownerSelect && ownerSelect.value !== link.owner) {
    if (!await confirmDialog(`Reassign "${slug}" to "${ownerSelect.value}"?`,
                             { confirmLabel: "Reassign" })) return;
    const r = await api.post("/links/bulk-action",
      { slugs: [slug], action: "reassign", owner: ownerSelect.value });
    …
  }
  ```
- **`updateBulkBar()`** — two changes:
  1. The over-cap disable loop must include the new buttons. **This is the
     easiest thing in the GUI work to miss:**
     ```js
     for (const id of ["bulk-enable-btn", "bulk-disable-btn", "bulk-delete-btn",
                       "bulk-tag-add-btn", "bulk-tag-remove-btn", "bulk-reassign-btn"]) {
       document.getElementById(id).disabled = overCap;
     }
     ```
  2. Reveal `#bulk-tag-controls` when `canTagLinks()` and
     `#bulk-owner-controls` when `canManageUsers()`.

  The over-cap copy gains a next step, since a tag filter is exactly how someone
  ends up 300-selected:
  ```
  `${count} links selected — bulk actions apply to at most ${BULK_MAX_SELECTION} at a time. Narrow the filter, or clear some selections.`
  ```
  **Yes, tag-filtered bulk selection is bound by the same 50-row cap**
  (`MAX_BULK_ROWS`, `api/bulk.py:16`). Nothing about tags relaxes it, and the
  existing client-side guard already prevents a request that is guaranteed to be
  rejected.
- **`handleBulkTag(action)`** — reads `#bulk-tag-input`, `parseTagsInput`, POSTs
  `{slugs, action, tags}`, and routes a `bulk_validation_failed` response through
  the existing `renderRowErrorList` into `#bulk-action-errors`. No confirmation
  dialog: tagging is reversible by the adjacent button, and DESIGN.md's Bulk
  Action Bar rule says confirming a reversible action trains people to dismiss
  confirms.
- **`handleBulkReassign()`** — POSTs `{slugs, action: "reassign", owner}` behind
  a count-bearing `confirmDialog`:
  `Reassign ${n} links to "${owner}"? They will move out of their current owners' lists.`
  with `confirmLabel: \`Reassign ${n} links\``, matching the bulk-delete
  confirmation's count-states-the-scale convention.
- **CSV export** — one row appended to `CSV_COLUMNS`, at the **end** so an
  existing spreadsheet template's column positions do not shift:
  ```js
  ["Tags", (l) => (l.tags ?? []).join(" ")],
  ```
  Space-joined, not comma-joined: the tag charset excludes spaces, so the join
  is unambiguous and reversible, and the field never needs RFC-4180 quoting.

### `gui/links/detail.html` and `gui/links/detail.js`

One row in the Details article, after Custom short link:
```html
<p><strong>Tags:</strong> <span id="tags"></span></p>
```
and in `loadLinkInfo()`:
```js
const tagList = data.tags ?? [];
document.getElementById("tags").innerHTML = tagList.length
  ? tagList.map((t) => `<span class="tag-chip">#${escapeHtml(t)}</span>`).join("")
  : "—";
```
Read-only. Editing goes through the Edit button, which already deep-links into
the dashboard row's edit form (`detail.js:97`).

### `gui/admin/users.js`

Two lines, so the new permission is grantable:
```js
const ALL_PERMISSIONS = ["links.create_custom_slug", "links.view_all", "links.edit_all", "links.tag", "users.manage"];
PERMISSION_LABELS["links.tag"] = "Tag links in bulk";
```

### `gui/app.js`

Five entries in `ERROR_MESSAGES`:
```js
invalid_tag:   "Tags can only use lowercase letters, numbers, hyphens and underscores (up to 32 characters).",
invalid_tags:  "That list of tags isn't valid.",
too_many_tags: "That's too many tags for one link.",
no_tags:       "Enter at least one tag.",
unknown_owner: "That user doesn't exist — pick someone from the list.",
```
`too_many_tags` and `invalid_tag` responses both carry the machine-readable
detail (`max_tags`, `tag`) so a call site can override with a more specific
string via `friendlyError`'s existing `overrides` parameter if it wants to.

## Trade-offs and rejected alternatives

### 1. No `tag:<tag>` → slug-list KV index — the load-bearing decision

**The alternative, and why it is attractive:** the obvious shape. Every other
"find links by X" need in this app is served by an index (`all_links`,
`owner_links:<user>`), and a `tag:<tag>` key would make a future server-side
`GET /api/links?tag=X` a single read instead of a walk.

**Why it loses:**

- **Nothing in this feature reads it.** `handle_list` already returns every
  record the viewer may see and the dashboard already holds them all in
  `allLinks` (`dashboard.js:68`) — that is exactly the observation
  `TASKS.md`'s CSV-export entry made. Filtering by tag is therefore a
  client-side array filter over data already in memory. An index would be
  written on every create/edit/bulk-action and read by nothing.
- **It would be the only read-modify-write index in the app that a *single-link
  edit* has to touch two of.** Retagging one link means removing it from every
  old tag's index and adding it to every new tag's index — up to 20 index
  read-modify-writes for a single `PATCH` under the 10-tag cap, each one racy,
  with no compare-and-swap available (`CLAUDE.md`, Security tradeoffs). Bulk
  tagging 50 links compounds it.
- **It creates a second, durable source of truth that can silently disagree with
  the records.** `all_links` drift is already a known hazard —
  `bulk.handle_bulk_create` spends N extra KV reads specifically because
  "`all_links` is an index, not the truth" (`api/bulk.py:190-194`). Tag indexes
  would add a drift surface with no equivalent cheap confirmation.
- **It becomes a backup-schema obligation forever.** A `tag:` key type must be
  added to `backup.INDEX_KEYS["links"]` and `restore_write_order`, or restore
  silently mis-orders it — the exact trap the requester flagged. Not creating
  the key type is the cheapest way to be correct.

**What would justify revisiting it:** a deployment where `handle_list`'s
already-existing O(all visible links) KV walk becomes the bottleneck. At that
point the fix is a paginated, server-filtered list endpoint, and a tag index
falls out of that work — not a bolt-on to today's load-everything dashboard.

### 2. No `_meta:tags` registry for autocomplete; suggestions are client-derived

**The alternative:** a `_meta:tags` key in the `links` store listing every tag
ever used, mirroring `auth.USERNAMES_INDEX_KEY`'s `_meta:usernames`
(`api/auth.py:24`) — the repo's only precedent for an enumerable set. Attractive
because it is precedented, and because it would let a user discover a tag used
only on links they cannot see.

**Why it loses:**

- **Write amplification for a read nothing needs.** It would be read once per
  page load and written on every create, edit, bulk-create and bulk-tag — a
  read-modify-write on a single hot key, racy for the same no-CAS reason.
- **Staleness is unfixable without refcounting.** Delete the last link carrying
  `q3` and the registry still offers `q3` forever, unless every delete and every
  untag scans for remaining users of that tag — which is the O(all links) walk
  the registry existed to avoid. `_meta:usernames` does not have this problem
  because `users.handle_delete` removes exactly one name and there is exactly one
  record per name.
- **The client-derived list is already correctly scoped.** `allKnownTags()`
  reads `allLinks`, which is already ownership-scoped by `handle_list`. A user
  without `links.view_all` is suggested only tags they have actually used, which
  is *better* behavior than a global registry would give — a global one would
  advertise other teams' campaign names to everyone.
- **Zero new key type** — see the backup argument in (1).

**Accepted limitation, stated plainly:** a user without `links.view_all` will
not be suggested a tag they have never used, so two people can independently
create `black-friday` and `blackfriday`. Lowercase normalization removes the
casing half of that problem; the rest is the accepted cost of free-form tags
(decision 3 explicitly rejects an admin-curated vocabulary). **What would
justify revisiting:** an actual complaint about tag drift between teams, at
which point the honest fix is the curated vocabulary that was already rejected,
not a registry.

### 3. No server-side whole-tag bulk action

**The alternative:** `POST /api/links/bulk-action {"tag": "q3", "action":
"delete"}`, or `DELETE /api/tags/q3`, acting on every link carrying a tag
regardless of count. Attractive because it is the natural expression of "retire
this campaign", it is not bound by `MAX_BULK_ROWS`, and it is one request
instead of six.

**Why it loses:** this was **the user's explicit call** (decision 4), and the
reasoning is sound enough to record rather than merely cite. The action's blast
radius is unbounded and invisible at the moment of clicking — the operator sees
a tag name, not 300 links, and a mistyped or over-broad tag deletes links nobody
was looking at. There is no undo (`TASKS.md` Future work records that bulk-delete
undo needs a tombstone record and a retention policy that do not exist), and
`MAX_BULK_ROWS` was deliberately reduced from 200 to 50 specifically to keep a
single request's work bounded — an unbounded whole-tag action would reintroduce
the exact unboundedness that cap exists to prevent, in the one operation where
being wrong is unrecoverable. Filtering first means the count is on screen, the
rows are on screen, and the confirmation dialog states the number. **What would
justify revisiting:** a real workflow where >50 links per tag is routine *and*
undo exists.

### 4. Tags render in the existing Short-link cell, not a new Tags column

**The alternative:** a ninth `<th>`/`<td>`. Attractive because it is sortable,
scannable, and where a spreadsheet user would look.

**Why it loses:** `dashboard.css` spends roughly 60 lines
(`dashboard.css:59-132`) documenting measured, live-verified fixes for a table
that already overflows at ordinary desktop widths, including a `@media
(max-width: 600px)` block that hides columns by `nth-child` index. Those indices
already shifted once when the select column landed, and `dashboard.js:326-328`
carries the comment "*This fails silently if wrong*" about the positional
`displayRow.children[n]` writes that depend on them. A ninth column shifts them
again, changes three `colspan="9"` values, and needs the whole 600px clearance
measurement redone. Rendering chips in a cell that already holds two badges
costs none of that. **What would justify revisiting:** a decision to rebuild the
table's responsive strategy, at which point a Tags column is a normal
requirement rather than a risk.

### 5. Tag chips are not clickable; the filter is a `<select>`

**The alternative:** click a tag chip in a row to filter by it — the most
discoverable possible affordance, and half of the user's decision-4 phrasing
("clicking or selecting").

**Why it loses:** a clickable chip must be a `<button>` or carry
`role="button"`, and DESIGN.md's sitewide tap-target floor
(`DESIGN.md:203`) puts `min-height: 44px` on both selectors. Ten 44px chips in a
table row is an enormous row, and exempting them is a design-system change, not
a feature change — DESIGN.md's own history shows audits catching exactly this
(the nav anchors were found at 38.4px and *raised* to 44px rather than the rule
being narrowed). The non-button escape hatch, `tabindex="0"` plus click and
Enter/Space handlers — the pattern `#links-table th.sortable` already uses — is
available but would put ten focus stops in every row, which is a worse keyboard
experience than one select. The `<select>` honors the "or selecting" half of the
decision, needs no new component in DESIGN.md, and composes with the text filter
for free. **What would justify revisiting:** a DESIGN.md decision to exempt
in-table micro-chips from the 44px floor. Recorded under Future work.

### 6. Owner reassignment goes through `bulk-action`, not `PATCH` or a dedicated endpoint

**The alternatives:** (a) add `owner` to `links.UPDATABLE_FIELDS` and check
`users.manage` inside `handle_update`; (b) a `POST /api/links/{slug}/owner`
endpoint mirroring the existing `/password` one.

**Why they lose:** (a) puts two different permission gates in one handler —
`can_edit` for every other field and `users.manage` for this one — which is
exactly the shape that produced the `links.view_all` bug family
(`TASKS.md`, "Pre-existing bug fixed": four handlers gating on
`role != "admin"` and none checking the permission they claimed). (b) is clean
but means the three-write index dance is implemented **twice**, once for one
slug and once for many, in the single highest-risk operation in the feature.
`handle_bulk_action`'s existing all-or-nothing structure — validate every row,
write nothing on any error — is precisely what this operation needs, and a
one-element `slugs` list is a legitimate use of it (the GUI's own bulk-delete
already special-cases `slugs.length === 1` in copy alone). One implementation,
one set of tests, one failure mode to reason about.

### 7. Reassignment requires `users.manage` only — not `users.manage` **and** per-row `can_edit`

**The alternative:** apply the same per-row `can_edit` gate every other bulk
action uses, so reassignment respects ownership scoping like tagging does.

**Why it loses:** it would break the motivating case. Reassigning a departed
employee's orphaned links means acting on links you do not own and cannot edit;
requiring `can_edit` makes the feature useless unless the operator also holds
`links.edit_all`, which is a separate grant nobody would think to pair with it.
And it buys no security: as established above, a `users.manage` holder can
already promote themselves to `admin` through `PATCH /api/users/{username}`
(`api/users.py:139-143`, no self-exclusion), so `users.manage` is already
equivalent to full access — the same argument `docs/plans/kv-backup-restore.md`
used for its own gate. This is a deliberate, documented asymmetry with the tag
actions, not an oversight.

### 8. Tag values are lowercase-only, not case-preserving

**The alternative:** store `Black Friday` as typed and compare
case-insensitively. Nicer to look at.

**Why it loses:** two records can then disagree on the display form of the same
tag, and the client-derived suggestion list would offer both. Case-folding at
the boundary makes the stored value canonical, which is what makes a
client-derived list, a CSV column, and a filter comparison all agree with no
per-surface rule. Spaces are excluded for the same reason the CSV join is
space-separated and unambiguous.

### 9. Do nothing

Live for tags: the text filter partially substitutes if slugs are named by
convention. Not live for ownership — there is no workaround at all short of
deleting and recreating links, which changes every slug and voids every printed
QR code. Rejected on the ownership half alone.

## Tasks

Appended verbatim to `TASKS.md` under `## Link tags and owner reassignment`.
`TASKS.md` is authoritative; this mirror does not track checkbox state.

```
- [ ] Add api/tags.py with the pure tag vocabulary helpers — file(s): api/tags.py (new), api/tests/test_tags.py (new) — done when: `MAX_TAGS_PER_LINK = 10`, `MAX_TAG_LENGTH = 32` and `TAG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")` exist as module constants and `normalize_tag`, `is_valid_tag`, `parse_tags(value, *, allow_none=True)`, `apply_tags(existing, add)` and `remove_tags(existing, remove)` exist with the signatures in docs/plans/link-tags-and-ownership.md; the module has zero `spin_sdk` imports and imports nothing from `links.py`; and `cd api && uv run pytest` passes with new tests covering `"  Sale  "` normalizing to `"sale"`, a 33-character tag and `"-lead"` and `"a b"` and `"café"` each rejected as `invalid_tag` carrying the tag as submitted, an 11-entry list rejected as `too_many_tags` carrying `max_tags: 10`, a non-list and a list containing a non-string each rejected as `invalid_tags`, `None` accepted as `[]` under `allow_none=True` and rejected under `allow_none=False`, `["b","A","b"]` normalizing to exactly `["a","b"]`, and `remove_tags(["a"], ["zz"])` returning `["a"]` rather than raising.
- [ ] Add `links.tag` to the permission vocabulary — file(s): api/auth.py, api/tests/test_auth.py — done when: `KNOWN_PERMISSIONS` contains exactly `{"links.create_custom_slug", "links.view_all", "links.edit_all", "links.tag", "users.manage"}`; and `cd api && uv run pytest` passes with a test asserting `users._validate_permissions(["links.tag"])` returns `None` and `users._validate_permissions(["links.tags"])` returns `"invalid_permissions"`.
- [ ] Accept `tags` on single-link create, update and the public link shape (depends on api/tags.py) — file(s): api/links.py, api/tests/test_links.py — done when: `public_link` synthesizes `tags: record.get("tags", [])` without writing to KV, `UPDATABLE_FIELDS` contains `"tags"`, `handle_create` stores a normalized sorted list and `handle_update` fully replaces it; and `cd api && uv run pytest` passes with new FakeStore-backed tests that a create with `["Q4"," sale "]` stores `["q4","sale"]`, a create with no `tags` key stores `[]`, a legacy record with no `tags` key serializes `tags: []` through `handle_get`, `PATCH {"tags": []}` clears them, `PATCH {"tags": null}` returns `400 invalid_tags`, `PATCH {"tags": ["Bad Tag"]}` returns `400 invalid_tag`, and a `PATCH` that omits `tags` entirely leaves an existing list untouched.
- [ ] Apply batch tags to bulk create (depends on api/tags.py) — file(s): api/bulk.py, api/tests/test_bulk.py — done when: `handle_bulk_create` reads a batch-level `tags` from the payload exactly the way it already reads `password`/`start_at`/`end_at`, returns the `parse_tags` error body verbatim with a `400` on a bad value, and writes the same normalized list onto every record in the submission; the pasted-text format is unchanged (no third column); and `cd api && uv run pytest` passes with new tests that a three-row submission with `tags: ["SALE"]` gives all three records `["sale"]`, that an invalid batch tag creates nothing at all, and that a submission with no `tags` key gives every record `[]`.
- [ ] Add the tag/untag bulk actions behind the `links.tag` permission (depends on the two tasks above; NOTE — this task encodes the plan's reading that `links.tag` gates bulk tag operations and nothing else, per docs/plans/link-tags-and-ownership.md's "One interpretation call"; confirm that before starting) — file(s): api/bulk.py, api/tests/test_bulk.py — done when: `BULK_ACTIONS` is `{"delete","enable","disable","tag","untag","reassign"}` with the reassign branch unimplemented in this task, `action: "tag"|"untag"` requires a non-empty validated `tags` list (`400 no_tags` when empty, the `parse_tags` error body otherwise), returns `403 {"error": "forbidden", "required_permission": "links.tag"}` for a principal without it, still applies the per-row `can_edit` check, rejects the whole submission with a per-row `{"slug", "error": "too_many_tags", "max_tags": 10}` when adding would push any link past the cap, and bumps `updated_at`; and `cd api && uv run pytest` passes with new tests that a `links.tag` holder without `links.edit_all` gets a per-row `forbidden` for another user's link and nothing is written, that untagging a tag a link does not carry is a no-op returning `200`, that re-tagging an already-tagged link produces no duplicate, and that a cap violation on one slug leaves all FakeStore values byte-identical.
- [ ] Add links.move_slugs_between_owners, the owner-index-only reassignment helper (must land before the reassign action) — file(s): api/links.py, api/tests/test_links.py — done when: `move_slugs_between_owners(store, slugs_by_old_owner, new_owner)` does one read+write of `owner_links:<new_owner>` plus one per distinct old owner, adds to the new owner before removing from any old owner, skips any old owner equal to `new_owner`, and **never reads or writes `all_links`**; and `cd api && uv run pytest` passes with new tests asserting that `all_links` is byte-identical before and after, that a same-owner call leaves the slug present in that owner's index (the guard), that a slug already in the new owner's index is not duplicated, that calling it twice with the same arguments produces the same final state as calling it once, and that two old owners in one call each get exactly one index write.
- [ ] Add the reassign bulk action behind users.manage (depends on move_slugs_between_owners) — file(s): api/bulk.py, api/app.py, api/tests/test_bulk.py — done when: `handle_bulk_action`'s signature is `(store, users_store, principal, request)` and `api/app.py` passes the already-open `users_store`, `action: "reassign"` requires a non-empty string `owner` (`400 invalid_owner`) that resolves through `auth.get_user` (`400 {"error": "unknown_owner", "owner": ...}`, with a disabled user accepted), returns `403 {"error": "forbidden", "required_permission": "users.manage"}` without the permission, **skips the per-row `can_edit` check for this action only** while still returning per-row `not_found`, writes every `slug:` record before calling `move_slugs_between_owners`, and returns `{"ok": true, "action": "reassign", "count": n, "owner": ...}`; and `cd api && uv run pytest` passes with new tests that a `users.manage` principal without `links.edit_all` successfully reassigns another user's link, that an unknown owner writes nothing, that a two-old-owner batch updates both old indexes and the one new index, and that `all_links` is byte-identical before and after.
- [ ] Prove tags and reassignment round-trip through backup and restore (depends on the tag and reassign actions; no production code change expected) — file(s): api/tests/test_backup.py — done when: `cd api && uv run pytest` passes with new tests asserting that a `slug:` value containing a `tags` array survives `build_backup` -> `validate_backup` byte-identical, that `is_excluded_key("links", "slug:x")` is still `False`, and that a `links` store containing `owner_links:alice`, `owner_links:bob`, `all_links` and two `slug:` records with tags restores with every `slug:` write ordered before every `owner_links:`/`all_links` write via `restore_write_order`; and the task note states explicitly whether `api/backup.py` needed any change (the plan predicts none — if it did, say what and why).
- [ ] Guard the redirect hot path against the new link field — file(s): redirect/linkgate/link_test.go — done when: a new test parses a link record JSON containing a `"tags": ["sale","q4"]` array through `ParseLink`, asserts no error and that `Slug`/`TargetURL`/`Status` survive, and `linkgate.Link` still has **no** `Tags` field; `cd redirect && go test ./linkgate/...` passes (never `go test ./...`, which fails by design on `package main`).
- [ ] Show tags on the dashboard and filter by them (depends on every API task above) — file(s): gui/dashboard.html, gui/dashboard.js, gui/theme.css — done when: `.tag-chip` is added to both existing `.slug-kind-badge, .lock-badge` selector groups in `theme.css` with **no new declaration and no new token**, each row renders its tags as `#tag` chips inside the existing Short-link cell (no new column, no `colspan` change, no `nth-child` change), a `#tag-filter` select next to `#links-filter` is repopulated from the distinct tags in `allLinks` on every load and ANDs with the text filter (whose placeholder now names tags and whose match now includes them), a `#link-tags` input in `#advanced-options` and an `.edit-tags` input in each edit row read and write tags via `parseTagsInput`, a shared `<datalist id="tag-suggestions">` is rebuilt per keystroke with the last-comma prefix applied, and a `#bulk-tags` input in the bulk-create fieldset applies to the whole batch; `cd gui-pages && uv run pytest` still passes at 64 (no page and no .js file added, so the auto-derived counts do not move) with zero inline `<script>`/`<style>`/`style="`/`on<event>=`; and in a real browser a link tagged `sale` is found by both the tag select and by typing `sale` in the text filter.
- [ ] Add the bulk tag and reassign controls to the bulk bar (depends on the dashboard tags task) — file(s): gui/dashboard.html, gui/dashboard.js, gui/admin/users.js, gui/app.js — done when: `#bulk-tag-controls` is revealed only for `links.tag`/admin and `#bulk-owner-controls` only for `users.manage`/admin (whose `#bulk-owner-select` is populated from a `GET /api/users` made only when that permission is held), `updateBulkBar`'s over-cap disable loop includes `bulk-tag-add-btn`, `bulk-tag-remove-btn` and `bulk-reassign-btn` and its over-cap copy tells the user to narrow the filter, reassign (bulk and single-row) goes behind a count-and-target-bearing `confirmDialog` while tag/untag do not, the row edit form gains an `.edit-owner` select only for `users.manage` holders that POSTs `bulk-action` after a successful PATCH in the same shape the password change already uses, `admin/users.js` offers `links.tag` as "Tag links in bulk", and `app.js`'s `ERROR_MESSAGES` covers `invalid_tag`, `invalid_tags`, `too_many_tags`, `no_tags` and `unknown_owner`; `cd gui-pages && uv run pytest` still passes at 64 with zero inline code; and in a real browser selecting 3 links and clicking Add tag tags exactly those 3.
- [ ] Show tags on the link detail page and in the CSV export (depends on the dashboard tags task) — file(s): gui/links/detail.html, gui/links/detail.js, gui/dashboard.js — done when: the Details article renders a read-only `Tags:` row using the same `.tag-chip` markup and an em dash when empty, and `CSV_COLUMNS` gains `["Tags", (l) => (l.tags ?? []).join(" ")]` **appended last** so existing column positions do not shift; `cd gui-pages && uv run pytest` still passes at 64; and an exported CSV opened in a spreadsheet shows a Tags column whose space-separated values need no quoting.
- [ ] Document tags and owner reassignment in CLAUDE.md, PRODUCT.md and DESIGN.md (depends on every task above) — file(s): CLAUDE.md, PRODUCT.md, DESIGN.md — done when: CLAUDE.md gains a "Link tags and ownership" section (peer to "Bulk link management") stating the tag character set/length/per-link cap and why each number was chosen, that tags are stored normalized-deduplicated-sorted inside the `slug:` record with **no `tag:` index and no `_meta:tags` registry** and that adding either later obliges a matching `backup.INDEX_KEYS`/`restore_write_order` change, that autocomplete is derived client-side from the links already loaded and is therefore ownership-scoped, that `links.tag` gates only the bulk tag/untag actions while single-link tagging needs only edit rights, that reassignment is gated on `users.manage` alone with the reason it is not a weaker bar than `role == "admin"`, the reassign write ordering (records first, then new owner index, then old owner indexes, never `all_links`) with the interruption table's outcomes, and that `redirect` is untouched because Go's `encoding/json` ignores unknown fields; PRODUCT.md's Capabilities list gains one accurate line covering tags and reassignment; DESIGN.md's `### Chips` gains the tag chip (explicitly not a pill, reusing the slug-kind-badge treatment, no new token) and `### Bulk Action Bar` gains the two new permission-gated control groups; `.impeccable/design.json` is updated only if a new token was actually introduced (none is planned — say so in the task note if none was); and no doc claims a capability the shipped code does not have.
- [ ] End-to-end manual verification of link tags and owner reassignment — file(s): (none — verification step) — done when: every numbered step in docs/plans/link-tags-and-ownership.md's Verification section is executed against a real `spin up --build --runtime-config-file runtime-config.toml` in a browser with the console open and **zero errors of any kind, in particular zero CSP violations, in both light and dark themes**; a non-admin holding only `links.tag` can bulk-tag their own links but gets a per-row `forbidden` on someone else's; a non-admin holding only `users.manage` successfully reassigns a link they cannot edit and it moves between both dashboards; a tag-filtered select-all of more than 50 links disables every bulk button with the narrow-the-filter message; a reassigned link still resolves at `/r/<slug>`; and `cd api && uv run pytest`, `cd gui-pages && uv run pytest` and `cd redirect && go test ./linkgate/...` all pass.
```

## Critical files

- `api/tags.py` **(new)**
- `api/tests/test_tags.py` **(new)**
- `api/auth.py`
- `api/links.py`
- `api/bulk.py`
- `api/app.py`
- `api/tests/test_auth.py`
- `api/tests/test_links.py`
- `api/tests/test_bulk.py`
- `api/tests/test_backup.py`
- `redirect/linkgate/link_test.go`
- `gui/dashboard.html`
- `gui/dashboard.js`
- `gui/theme.css`
- `gui/links/detail.html`
- `gui/links/detail.js`
- `gui/admin/users.js`
- `gui/app.js`
- `CLAUDE.md`
- `PRODUCT.md`
- `DESIGN.md`
- `TASKS.md`

Deliberately **not** touched: `spin.toml`, `gui-pages/routing.py`,
`gui-pages/tests/`, `api/backup.py`, `redirect/main.go`,
`redirect/linkgate/link.go`, `gui/dashboard.css`, `Jenkinsfile`
(test invocation is unchanged).

## Verification

Run in this order.

1. **Unit suites, after each task lands:**
   ```bash
   cd api && uv run pytest
   cd gui-pages && uv run pytest
   cd redirect && go test ./linkgate/...
   ```
   Baseline at `f7dbb0e` is 289 / 64 / ok. `gui-pages` must still read exactly
   **64** — this plan adds no page and no `.js` file, so any movement there
   means something unplanned was added.

2. **Confirm the redirect component is untouched:**
   ```bash
   git diff --stat main -- redirect/
   ```
   Must show only `redirect/linkgate/link_test.go`.

3. **Confirm no new KV key type leaked in:**
   ```bash
   grep -rn '"tag:' api/ || echo "no tag: key type — as designed"
   grep -rn '_meta:tags' api/ || echo "no tag registry — as designed"
   ```

4. **Boot the app** (two domains, so the nav is at its widest for the
   overflow sanity check):
   ```bash
   SPIN_VARIABLE_PUBLIC_BASE_URLS="http://localhost:3000,http://127.0.0.1:3000" \
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> \
   SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```

5. **Seed.** Log in as `admin` at `http://localhost:3000/login.html`. On
   `/admin/users.html` create three users: `tagger` (permissions:
   `links.tag` only), `peoplemgr` (`users.manage` only), `plain` (none).
   On the dashboard create at least 6 links, giving 3 of them
   `tags: sale, q4` via "More options".

6. **Tag display and filter.** On `/dashboard.html`, confirm each tagged row
   shows `#q4 #sale` chips in the Short-link cell, in sorted order, in plain
   slate text — **not** a pill and **not** monospace. Select `sale` in the tag
   filter: only the 3 tagged links remain. Type `sale` in the text filter with
   the tag filter cleared: the same 3 remain. Both together: still 3.

7. **Normalization, live.** Edit one link and set its tags to
   `  Q4 , SALE, q4 `. Save, reload. The row must read exactly `#q4 #sale` —
   lowercased, de-duplicated, sorted. Set tags to `Bad Tag` and save: the
   edit-row error reads the friendly `invalid_tag` copy, not a raw code.

8. **Bulk create with batch tags.** In "Create many at once", paste two rows,
   put `promo` in the batch Tags field, submit. Both new links show `#promo`.

9. **Bulk tag, as a non-admin.** Log in as `tagger`. The bulk bar shows the tag
   controls and **not** the owner controls. Select 2 of their own links, type
   `email`, click Add tag → both gain `#email`. Click Remove tag → both lose it.

10. **Ownership scoping on bulk tag.** Still as `tagger` — they have no
    `links.view_all`, so admin's links are not listed. Reproduce the
    cross-owner rejection with curl instead, using `tagger`'s session cookie and
    CSRF token against a slug owned by `admin`:
    ```bash
    curl -s -X POST http://localhost:3000/api/links/bulk-action \
      -b "session=<tagger session>" -H "X-CSRF-Token: <tagger csrf>" \
      -H 'Content-Type: application/json' \
      -d '{"slugs":["<admin-owned-slug>"],"action":"tag","tags":["x"]}'
    ```
    Expect `400 bulk_validation_failed` with a `forbidden` row error, and
    confirm in the dashboard that the link gained no tag.

11. **Permission gate.** Log in as `plain`. The bulk bar shows **neither** the
    tag nor the owner controls. Repeat the curl above with `plain`'s session:
    expect `403 {"error":"forbidden","required_permission":"links.tag"}`.

12. **Owner reassignment, single link.** Log in as `peoplemgr` (has
    `users.manage`, **not** `links.edit_all`). Open a link owned by `admin` —
    reachable via `/links/detail.html?slug=<slug>` even without `links.view_all`
    only if they can view it; if not, do this step as `admin` and re-verify the
    permission split with curl in step 13. Change Owner to `peoplemgr`, save,
    confirm the confirmation dialog names both the slug and the target, and
    confirm after reload that the Owner column reads `peoplemgr`.

13. **Owner reassignment, permission split, via curl:**
    ```bash
    # peoplemgr (users.manage, no links.edit_all) — expect 200
    curl -s -X POST http://localhost:3000/api/links/bulk-action \
      -b "session=<peoplemgr session>" -H "X-CSRF-Token: <peoplemgr csrf>" \
      -H 'Content-Type: application/json' \
      -d '{"slugs":["<admin-owned-slug>"],"action":"reassign","owner":"plain"}'
    # tagger (no users.manage) — expect 403 required_permission users.manage
    # unknown owner — expect 400 unknown_owner
    ```

14. **Index integrity after reassignment.** Restart with the KV explorer:
    ```bash
    SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> \
    SPIN_VARIABLE_KV_EXPLORER_PASSWORD=<explorer-pw> \
    SPIN_VARIABLE_COOKIE_SECURE=false \
      ./dev/kv-explorer-up.sh
    ```
    (Local KV is non-persistent, so re-seed and re-reassign first.) In the
    `links` store confirm: the slug appears in `owner_links:<new>` exactly once,
    is **absent** from `owner_links:<old>`, and is still present in `all_links`.
    That last one is the whole point of `move_slugs_between_owners`.

15. **Reassigned link still resolves:** `curl -sI http://localhost:3000/r/<slug>`
    → `302` with the correct `Location`. Tags must add no latency and no KV read
    here.

16. **Bulk reassign with a count.** As `admin`, select 3 links, choose a target
    in the owner select, click Reassign. The dialog reads
    `Reassign 3 links to "<owner>"?` with the confirm button labelled
    `Reassign 3 links`. After confirming, all 3 rows show the new owner.

17. **The 50-row cap under a tag filter.** Bulk-create 50 links tagged `bulkcap`
    (one submission), then create 5 more the same way, giving 55. Filter by
    `bulkcap`, click select-all: the bar must read the narrow-the-filter message
    and **every** button — Enable, Disable, Delete, Add tag, Remove tag,
    Reassign — must be disabled. This is the step that catches a missed entry in
    `updateBulkBar`'s disable loop.

18. **Backup round trip.** As `admin`, go to `/admin/backup.html`, download a
    full backup, confirm by eye that a `slug:` entry's decoded value contains
    `"tags"`. Delete a tagged link from the dashboard, restore the file, and
    confirm the link is back with its tags intact and resolving at `/r/<slug>`.

19. **CSV.** Export CSV with a tag filter active. The file must have a trailing
    `Tags` column with space-separated values, unquoted, and must contain only
    the filtered rows.

20. **Detail page.** Open `/links/detail.html?slug=<tagged>`: the Tags row shows
    the chips; an untagged link shows `—`.

21. **Themes and console.** Repeat steps 6, 9, 16 and 20 with the nav theme
    control on Dark. **Zero console errors and zero CSP violations in both
    themes** — a CSP violation fails a page silently in a browser rather than
    failing a test, which is why this is a manual step.

22. **Re-run all three suites** (step 1) after the docs task, and confirm
    `git diff --numstat TASKS.md` shows only checkbox lines.

## Out of scope / follow-ups

Each of these belongs under `TASKS.md`'s `## Future work (not scheduled)` and is
added there by this plan:

- **A real tag-input component** (chips with individual remove buttons)
  replacing the comma-separated text input plus datalist. The datalist
  prefix-rewrite makes per-token autocomplete work, but it is a trick, and a
  chip input is the honest control. Blocked on the same 44px tap-target question
  as clickable chips. Pick it up if someone actually complains about the text
  input.
- **Clickable tag chips as a filter affordance** — needs a DESIGN.md decision to
  exempt in-table micro-chips from the sitewide 44px floor. That is a design
  system change, not a feature change.
- **Multi-select / AND-OR tag filtering.** Today's filter is one tag at a time,
  ANDed with the text box. A second tag needs a multi-select and a stated
  combining rule.
- **Renaming a tag across every link at once.** Achievable today only within the
  50-row cap (filter, select-all, Add tag, Remove tag). A true rename needs the
  whole-tag server action that was deliberately rejected above; revisit only
  together with that decision.
- **A per-row tags column in the bulk-create text format.** The parser is
  first-delimiter-wins on two columns; a third would land inside the destination
  and fail as an invalid URL. Would need a real CSV parser, which is a much
  larger change than a tags feature justifies.
- **A `tag:<tag>` KV index and a server-filtered, paginated list endpoint.** The
  right shape if `handle_list`'s existing O(all visible links) walk ever becomes
  the bottleneck — but as one piece of work, not a tag index bolted onto a
  load-everything dashboard.
- **Reassigning a link to a user who does not exist yet, or automatic
  reassignment on user delete.** `users.handle_delete` currently touches nothing
  in the `links` store, so deleting a user still orphans their links; this
  feature gives an operator the tool to fix it but does not make it automatic.
  Making it automatic means `handle_delete` needs a `links` store handle and a
  policy for where the links go, both of which deserve their own decision.
