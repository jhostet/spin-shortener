# User Deletion and Link Ownership

## Context

`api/users.py`'s `handle_delete` (line 175) deletes `user:<username>` and
removes the name from `_meta:usernames`. **It touches nothing in the `links`
store.** The link records keep `owner: "<deleted-username>"` and the
`owner_links:<deleted-username>` index key survives.

Because `links.handle_list` scopes a non-`view_all` user by reading
`owner_links:<principal.username>` (`api/links.py:244`) and `links.can_edit` is
a bare string comparison `record["owner"] == principal.username`
(`api/links.py:174`), **there is no user identity anywhere in this app beyond
the username string.** The requester reproduced the consequence live against a
running app:

```
carol creates https://internal.example.com/carols-secret   (custom slug carol-private)
admin deletes carol                  -> 200; /r/carol-private still resolves (302)
admin recreates "carol"              -> 201, different person, different password
new carol logs in, GET /api/links    -> sees carol-private
new carol PATCHes target_url         -> 200, now points at https://attacker.example.com/
```

The short URL keeps working throughout, so anyone who already shared it is now
sending traffic wherever the new account chooses. It needs an admin to recreate
the same username, so it is not remotely exploitable — but username reuse is an
ordinary administrative act and nothing anywhere warns about it.

There is a second, quieter half: even without reuse, a deleted user's links
become **uneditable by anyone without `links.edit_all`**, and
`owner_links:<deleted>` is a dangling index key nothing ever cleans up.

**And there is a third vector this plan found during research, which the
reported sequence does not mention and which disposing of the links does not
touch:** deleting a user does not invalidate their sessions, and recreating the
username **revives them**. See "Key technical facts" below — it is the single
most consequential finding here, and it is what decides the username-reuse
question.

This work is the `TASKS.md` Future-work entry **"Automatic handling of a deleted
user's links"** (TASKS.md:301, raised 2026-08-03 while planning
`docs/plans/link-tags-and-ownership.md`), which says in as many words that
`handle_delete` needs a `links` store handle and *"a policy for where the links
go — reassign to the deleting admin, reassign to a named successor, or disable
them — which is a product decision, not a refactor."* Choosing that policy is
the core of this document. The same entry names the related
`GET /api/admin/consistency` backlog item (TASKS.md:297); this plan deliberately
stays out of its way (see "Repair path").

**Confirmed decisions** (settled by the requester before planning; recorded so a
future reader knows they were deliberate):

1. The bug is reproduced and real. It is not to be re-litigated, and the
   reproduction sequence must be **pinned by a test**, not merely fixed.
2. `links.move_slugs_between_owners` (`api/links.py:86-119`) is the reassignment
   primitive. Reuse it; do not write a second index mover.
3. All-or-nothing is the house rule for anything batch-shaped: validate
   everything, then write, and report every problem.
4. `users.manage` is the gate, and the asymmetry the reassign action established
   stands — reassignment deliberately skips per-row `can_edit`, because the
   whole point is acting on links the operator cannot edit.
5. **No new GUI page and no new nav item.** The nav is at its documented item
   budget (a fifth item measured 716px against a 700px budget at 768px and was
   reverted). Prefer no new `spin.toml` route and no new `gui-pages/routing.py`
   entry.
6. **Deleting a user is already confirmed in the GUI.** Whatever disposition is
   chosen has to fit that flow without turning it into a wizard.
7. Rejected alternatives get dated (2026-08-04) entries under `TASKS.md`'s
   `## Considered and rejected`.

## Key technical facts confirmed during research

- **Baseline, measured now at `8678387`:** `cd api && uv run pytest` → **345
  passed**; `cd gui-pages && uv run pytest` → **64 passed**;
  `cd redirect && go test ./linkgate/...` → **ok**.

- **Exactly three things in this app are keyed by username.** Confirmed by
  reading every KV key literal in `api/` and `redirect/`:
  `user:<username>` (users store), `owner_links:<username>` (links store), and
  the `owner` field inside each `slug:<slug>` record (links store). Analytics
  keys (`count:<slug>`, `events:<slug>:<slot>`) carry no username. A fourth,
  indirect one is the value inside `session:<token>`, which stores
  `{"username": ...}` — see the next bullet. Nothing else in the app is
  username-addressed, which is what makes the reuse question tractable.

- **Deleting a user does not invalidate their sessions, and recreating the
  username revives them.** `auth.create_session` (`api/auth.py:175-191`) writes
  `session:<token>` = `{"username", "csrf_token", "issued_at", "expires_at",
  "auth_provider"}` — keyed by token, with the username only as a value.
  `auth.resolve_session` (`api/auth.py:194-221`) looks the session up, checks
  expiry, then does `get_user(store, session["username"])` and returns a
  `Principal` built from **whatever user record now bears that name**. So while
  carol is deleted her old cookie 401s (correct), but the moment an admin
  recreates "carol", the departed employee's still-unexpired cookie starts
  working again — with the *new* account's `role` and `permissions`, for up to
  `SESSION_TTL_SECONDS` (8 hours, `api/auth.py:22`) from her last login. This is
  the same "no identity beyond the username string" defect as the link half, and
  no amount of link disposal fixes it.
  **Disabling** a user has no such gap: `resolve_session` re-reads the user
  record on every request and returns `None` for `user.get("disabled")`, so a
  disable takes effect immediately.

- **`get_keys` works and is already used against the `users` store.**
  `api/app.py:18-33`'s `_kv_keys` drains the `(stream, future)` pair that
  `spin:key-value/key-value@3.0.0`'s `get-keys` returns, and
  `api/app.py:183-186` already passes it plus `users_store` into
  `backup.handle_export`. It is passed as a plain `list_keys` callable so
  `backup.py` stays host-importable, and `api/tests/fakes.py` already ships
  `fake_list_keys(store)` as the test double. Enumerating and purging a deleted
  user's sessions therefore needs **no new plumbing** — the pattern, the
  parameter shape and the fake all exist.

- **Backups contain no sessions, so a restore cannot revive one.**
  `backup.is_excluded_key` (`api/backup.py:82-88`) returns `True` for the users
  store's `_meta:bootstrapped` and any `session:`-prefixed key, and
  `redact_user_value` strips `password_hash`. A restore *can* reintroduce
  `user:carol` and `owner_links:carol` (restore is deliberately faithful, not
  repairing), but never a live token.

- **`_owned_slugs` treats a missing key and an empty list identically.**
  `api/links.py:41-43`: `raw = await store.get(f"owner_links:{username}")` then
  `json.loads(raw) if raw else []`. So deleting an empty `owner_links:<user>`
  key changes no behavior anywhere — which is exactly why deleting it is safe,
  and also why this plan is honest that doing so is tidiness rather than a
  security fix.

- **`remove_slugs_from_indexes` writes `owner_links:<owner>` = `[]` rather than
  deleting it** (`api/links.py:79-83`), and `move_slugs_between_owners` does the
  same for a fully-drained old owner (`api/links.py:113-119`). Empty index keys
  are therefore normal and pre-existing, and this plan deliberately does not
  change either helper — see Trade-offs #6.

- **`move_slugs_between_owners` already works on an owner who no longer
  exists.** It never consults the users store; it reads `owner_links:<old>`
  (missing → `[]`) and writes the difference. And `bulk.handle_bulk_action`
  skips the per-row `can_edit` check for `action: "reassign"`
  (`api/bulk.py:312-319`), so a `users.manage` holder can already reassign an
  orphaned link today. **The repair primitive is already shipped**; what is
  missing is a way to *find* the orphans and a rule that stops new ones being
  created.

- **`handle_bulk_action` already takes both stores.** Its signature is
  `(store, users_store, principal, request)` and `api/app.py:102-107` passes
  both. So "a handler needs a second store" is an established, precedented shape
  in this component, not a new one.

- **A `DELETE` with a semantically significant body is already rejected house
  policy.** `gui/app.js:56`'s shared helper is
  `delete: (path) => apiCall(path, { method: "DELETE" })` — it sends no body —
  and `TASKS.md`'s `## Considered and rejected` entry dated 2026-08-01 rejected
  `DELETE /api/links` with a JSON body of slugs for exactly this reason. This
  constrains the shape of any "explicit disposition" design (Trade-offs #2).

- **`409` is the established status for "conflicting state" in this handler
  family.** `users.handle_create` returns `409 {"error": "username_taken"}`
  (`api/users.py:78-79`) and `links.handle_create` returns
  `409 {"error": "slug_taken"}`. A refusal to delete is the same category.

- **`friendlyError(data, fallback, overrides)` exists for exactly this
  message.** `gui/app.js:176-180` — a call site can supply a computed override
  for one code while the shared `ERROR_MESSAGES` map holds a generic fallback.
  The precedent comment in the file names `invalid_password` (8 chars for
  accounts, 4 for link passwords) as the reason it exists.

- **"Hide a control when there is nothing to choose between" is a precedent in
  this exact codebase.** `gui/admin/users.js:59-66`'s `renderNewDomainsFieldset`
  does `fieldset.hidden = allDomains.length < 2`. The owner filter follows it.

- **A "second badge reusing the disabled treatment" is a precedent too.**
  `DESIGN.md:217` documents the users table's "no password" badge as a second
  badge beside the active/disabled one, reusing `status-disabled` and therefore
  `danger-red`, **with no new token and no new contrast measurement**, on the
  reasoning that both mean "this account can't be used right now" and inventing
  a distinct warning colour would imply a severity distinction the operator does
  not need. The deleted-owner marker is the third use of that same pattern, so
  **this plan changes no CSS at all.**

- **Query-string deep links already work through `gui-pages`.**
  `gui/links/detail.html` is reached as `detail.html?slug=<slug>` and
  `routing.py` resolves on the path alone. `dashboard.html?owner=carol` needs no
  route change, no `ROUTES` entry and no `test_routing.py` case.

- **`gui-pages`'s test count stays at exactly 64.** `PAGES` is derived from
  `ROUTES` and `SCRIPTS` from a `gui/**/*.js` glob
  (`gui-pages/tests/test_no_inline_code.py:25,41-45`). This plan adds no page and
  no `.js` file, so any movement in that number means something unplanned was
  added.

- **`redirect` is untouched.** `redirect/main.go` resolves `slug:{slug}` → KV →
  302 and never reads `owner`, the users store, or the `Host` header (the same
  property `CLAUDE.md`'s "Multi-domain display" section relies on). An orphaned
  link keeps resolving, deliberately — see "Redirect (Go) changes".

- **UNCONFIRMED: the wall-clock cost of `get_keys` on the `users` store under a
  real `spin up`.** `backup.handle_export` already does it over all three
  stores and is used interactively from the admin page, so the risk is low, but
  the session purge adds one drain to an operation that today does three KV
  ops. Verification step 9 measures it on a store with a handful of sessions;
  if it is ever slow at scale, the fallback is a `user_sessions:<username>`
  index, which is recorded and rejected in Trade-offs #5.

## The decision

**`DELETE /api/users/{username}` refuses with `409` when the user still owns
links, naming the count. It never disposes of links itself.**

```
409 {"error": "user_owns_links", "username": "carol", "link_count": 7}
```

Nothing is written to either store on the refusal path. The operator disposes of
the links first — reassign or delete — using the bulk tools that already exist,
and then deletes the account.

Three things make this the right call rather than merely the safest:

1. **The disposition decision is made with the links on screen.** This is the
   same argument that settled the immediately adjacent feature: `TASKS.md`'s
   2026-08-03 rejection of a server-side whole-tag action chose
   filter-then-select-then-act over raw power, because *"the operator sees a tag
   name, not 300 links, and a mistyped or over-broad tag deletes links nobody
   was looking at."* A `reassign_to`/`delete_their_links_too` parameter on user
   deletion is that exact shape with a username in place of a tag.
2. **Per-request work stays bounded.** `MAX_BULK_ROWS` was deliberately dropped
   from 200 to 50 to keep one request's work comfortably bounded
   (`CLAUDE.md`, Bulk link management; `TASKS.md` 2026-08-01). A disposition
   inside `handle_delete` would do N record writes plus index writes for an
   unbounded N, in a request that today does three KV operations, with no
   transaction and no cap. Refusing keeps user deletion O(1) and pushes the
   O(N) work through the capped, all-or-nothing, already-tested endpoint built
   for it.
3. **It needs no new write path.** Reassignment already exists, already skips
   `can_edit` for `users.manage` holders, and already has an interruption
   analysis. This plan adds a *gate*, not a second mover.

Its one real cost — an extra round trip, and the fact that a user with more than
50 links takes several rounds — is paid down by the dashboard owner filter
described under "GUI changes", which is what makes "select this departed user's
links" a two-click operation instead of a scavenger hunt.

**Deletion also purges the departed user's sessions** (see below), which is what
makes username reuse safe rather than merely tidy.

## API changes

All Python, all in the `api` component. None of it is on the `/r/...` hot path,
so it follows the language-split rule without ambiguity (`CLAUDE.md`, "Why Go
for `redirect` but Python for `api`/`gui-pages`").

### `api/auth.py` — one new constant and one new function

```python
SESSION_PREFIX = "session:"
```

Deliberately used **only** by the new function below; the three existing
`f"session:{token}"` literals (`create_session`, `resolve_session`,
`delete_session`) are left exactly as they are. Rewriting them is a mechanical
tidy-up that would enlarge the diff of a security fix for no behavioural gain.
`api/backup.py:42` already carries its own identical `SESSION_PREFIX`, with the
same duplication convention its `BOOTSTRAPPED_KEY  # == auth.BOOTSTRAPPED_KEY`
line documents.

```python
async def delete_sessions_for_user(store, username: str, list_keys) -> int:
    """Delete every session:<token> in `store` whose record names `username`.
    Returns the number deleted.

    `list_keys` is the same callable api/app.py passes to backup.handle_export
    (the get-keys drain), taken as a parameter so this module stays
    host-importable with zero spin_sdk imports.

    A session value that has vanished between the key listing and the read, or
    that is not valid JSON, is skipped rather than raised on: this runs inside
    user deletion, and a single malformed session record must never turn a
    delete into a 500.
    """
```

Reuses nothing new — `json`, `store.get`/`store.delete` and the `list_keys`
parameter shape are all already in the file or in `backup.py`.

### `api/users.py` — `handle_delete` gains a gate and a cleanup

```python
async def handle_delete(store, links_store, principal, username, list_keys):
```

Parameter order follows the two existing precedents: the second store comes
straight after the first (`bulk.handle_bulk_action(store, users_store,
principal, request)`) and `list_keys` comes last
(`backup.handle_export(stores, principal, query, list_keys, num_event_slots)`).
All five are required positionals — an optional `links_store=None` is an
`AttributeError` waiting for the first real call, the same reasoning the tags
plan used when threading `users_store` into `handle_bulk_action`.

Order of operations:

1. `users.manage` → `_forbidden()` (unchanged).
2. `get_user` → `404 not_found` (unchanged).
3. `username == principal.username` → `400 cannot_delete_self` (unchanged).
4. **New gate.** `owned = await links._owned_slugs(links_store, username)`.
   If `owned`, return
   `409 {"error": "user_owns_links", "username": username, "link_count": len(owned)}`
   **before writing anything to either store.**
5. `await links_store.delete(f"owner_links:{username}")` — provably an empty or
   absent key, given step 4.
6. `await auth.delete_sessions_for_user(store, username, list_keys)`.
7. `await store.delete(f"user:{username}")` (unchanged).
8. `await auth.remove_username(store, username)` (unchanged).
9. `200 {"ok": True}` (unchanged).

`users.py` must `import links` for `_owned_slugs`. **Check for an import cycle
before writing it:** `links.py` imports `auth` and `tags`, not `users`, and
`users.py` imports `auth` and `responses` today — so `users → links` is
acyclic. If a future change makes `links` import `users`, inline the two-line
read instead of importing.

Steps 4 and 5 both use the `owner_links:` key literal; `links._owned_slugs` is
module-private by name only and is already called from `bulk.py` via
`links._all_slugs`, so reusing it is in keeping with the file's existing
practice.

**Why the gate reads the index rather than walking every record.** One KV read,
not O(all links). `owner_links:<username>` is already the operative definition
of "this user's links" everywhere in the app — it is exactly what
`handle_list` reads for a non-`view_all` principal — so the 409's count is
always the same number the departing user's own dashboard would have shown.
The residual is real and stated plainly: if the index has drifted low (an
interrupted write, a KV-explorer edit), a record whose `owner` is carol but
which is missing from `owner_links:carol` slips through the gate. The
dashboard's owner filter is derived from the **records**, not the index, so it
catches those; and detection at scale is `GET /api/admin/consistency`'s job, not
this plan's.

### `api/app.py` — one branch

The `/api/users/{username}` DELETE arm (`api/app.py:163-174`) opens the links
store and passes it plus the existing `_kv_keys`:

```python
            if method == "PATCH":
                return await users.handle_update(users_store, result, username, request, configured_domains)
            links_store = await key_value.open("links")
            return await users.handle_delete(users_store, links_store, result, username, _kv_keys)
```

Opened inside the DELETE path only, so `GET`/`PATCH` on a user do not pay for a
store they never touch.

### API surface summary

| Method | Path | Change | Gate |
|---|---|---|---|
| `DELETE` | `/api/users/{username}` | `409 user_owns_links` when the user's `owner_links:` index is non-empty; on success also deletes `owner_links:<username>` and every `session:` record naming the user | `users.manage` (unchanged) |

One new error code: `user_owns_links`. No new route, no new endpoint, no new KV
key type — so `api/backup.py` needs no change (`INDEX_KEYS`/`restore_write_order`
already cover `owner_links:` via `OWNER_LINKS_PREFIX`, and sessions are excluded
from backups entirely).

## Write ordering and what an interruption leaves

Spin KV has no transactions and no compare-and-swap (`CLAUDE.md`, Security
tradeoffs), and this operation now spans **two stores**. The rule below is the
same one `CLAUDE.md`'s Bulk link management section states — records first,
indexes last — extended across the store boundary.

**Cross-store rule: the `links` store is written first, the `users` store
second.** The reverse order has a crash window that leaves the user gone *and*
`owner_links:<username>` behind, with no way to retry (a second `handle_delete`
would 404 on the missing user record) — which is precisely the dangling key this
work exists to remove. The chosen order's worst case is a user who still exists
with an index key already removed, which is indistinguishable from the empty
list it replaced.

**Within the users store: sessions before the user record.** If the record went
first, a crash would leave live tokens for a nonexistent username — the weaker
invariant, and the exact precondition for the revival vector. Purging first
means every interruption leaves the *stronger* state.

| Interrupted after | State left behind | Who notices | Recovery |
|---|---|---|---|
| the gate read (steps 1-4) | Nothing written at all | Nobody | Retry |
| 5 — `owner_links:<u>` deleted | User still exists; index key gone. `_owned_slugs` returns `[]` for a missing key exactly as it did for the empty list it replaced (`api/links.py:41-43`) | Nobody — no code path can tell the difference | Retry — converges |
| partway through 6 — some sessions purged | User exists; signed out of some browsers | The departing user | Retry — converges (the purge is a per-key delete, idempotent) |
| 6 complete, before 7 | User exists, is signed out everywhere, and could sign in again with their password | The departing user | Retry — converges |
| 7 — `user:<u>` deleted, before 8 | `_meta:usernames` names a user with no record | Nobody — `users.handle_list` already skips a `None` user (`api/users.py:54-57`) | **Not retryable**: `handle_delete` now 404s. The stale name is inert and invisible, `auth.add_username` de-duplicates, so recreating the username converges it. This window is unchanged from today's behaviour and is not made worse by this plan. |

The 409 path writes nothing, so it has no interruption story at all — which is
itself part of the argument for it.

## Username reuse: no tombstone, and why

**Decision: username reuse stays permitted. There is no reserved-username
tombstone list.** Instead, the two things a reused username could inherit are
each removed at the source:

- **Links** — the 409 gate makes it impossible to delete a user who still owns
  any, and step 5 removes the index key, so a new account of the same name
  starts with nothing.
- **Sessions** — step 6 deletes every token issued to the departed user, so
  there is nothing left to revive.

The reasoning, since this is the part a future reader will most want explained:

A tombstone does not fix the defect; it forbids the one act that exposes it. The
defect is that this app has no user identity beyond a string, and that is still
true after a tombstone is added — it is just unreachable through the admin UI. A
future feature keyed by username (an audit log, per-user analytics, a saved
view) would reintroduce the inheritance the tombstone was papering over, and
nobody would think to check.

A tombstone is also the wrong shape for the product. Usernames here are employee
identifiers (`PRODUCT.md`'s personas are named internal staff), and reusing
`jsmith` for a different John Smith is an ordinary administrative act. To be
sound the reservation would have to be **permanent** — a reservation shorter
than "forever" is just a bet about how long a stale artifact survives, and the
only artifact with a natural expiry (the 8-hour session) is the one we now
delete outright. A permanent reservation with no escape hatch will be worked
around within a month (`jsmith2`), and an escape hatch — an "allow reuse"
override flag — restores the vector on the exact code path an operator reaches
for when they are in a hurry.

Finally, it would cost a new KV key (`_meta:deleted_usernames`), a new error
code, a new branch in `handle_create`, a policy decision about
un-reserving, and a line in the backup format's mental model — for a rule whose
only job is to stop something that no longer happens.

**What would justify revisiting:** a fourth username-keyed artifact appearing
that cannot be cleaned up at deletion time (an append-only audit log is the
realistic example). At that point the honest fix is a stable per-user
identifier, not a reservation list — see Trade-offs #5.

## Repair path for existing deployments

**Decision: no repair endpoint, no migration, no server-side scan. The forward
fix plus the dashboard owner filter is the whole repair story.**

A deployment that already has orphans has, in the `links` store, some
`owner_links:<gone>` keys with slugs in them and some records whose `owner`
names a nonexistent user. Everything needed to fix that is already shipped:
`bulk-action`'s `reassign` skips `can_edit` for `users.manage` holders and
`move_slugs_between_owners` handles a missing old-owner index key. The only
missing piece is *finding* them, which is a display problem, and the dashboard
owner filter solves it:

1. An admin opens the dashboard (they hold `links.view_all` implicitly, so
   `allLinks` is every link).
2. The owner filter lists every distinct `owner` across those records —
   **derived from the records, not from any index**, so it catches drift the
   409 gate would miss — and marks any owner absent from the user list as
   `— deleted account`.
3. Select that owner, select all, Reassign (or Delete) through the existing
   count-bearing bulk bar, 50 at a time.
4. The rows also carry a `deleted account` badge in the Owner cell, so the
   condition is visible without going looking for it.

This is deliberately **detection by eye, at the point of action**, and it stops
exactly there. `GET /api/admin/consistency` (TASKS.md:297) remains the right
home for detection at scale — unindexed `slug:` records, `all_links` entries
with no backing record, `owner_links:` disagreeing with the records' own
`owner` — and this plan absorbs none of it: it adds no `/api/admin/*` endpoint,
no store walk, and no reporting surface. A Future-work line is appended noting
that two more checks belong in that endpoint when it is built: an
`owner_links:<username>` key whose username is not in `_meta:usernames`, and a
`slug:` record whose `owner` is not a known user.

Two residuals, stated rather than fixed:

- Repairing an orphan leaves an **empty** `owner_links:<gone>` key behind, because
  `move_slugs_between_owners` writes `[]` rather than deleting. It is inert —
  `_owned_slugs` cannot distinguish it from absence — and Trade-offs #6 explains
  why the helper is not being changed.
- **A restore can reintroduce orphans.** `handle_restore` is faithful, not
  repairing (`docs/plans/kv-backup-restore.md`), so restoring a backup taken
  before a user was deleted brings `user:carol` and `owner_links:carol` back
  together — consistently, as it happens, which is the good case. Restoring a
  *links-only* backup taken before the deletion, after the user is gone,
  recreates the orphan. The owner filter finds it.

## Redirect (Go) changes

**None, and that is a deliberate property rather than an oversight.**
`redirect/main.go` resolves `slug:{slug}` → `linkgate.ParseLink` → 302 and never
reads `owner`, never opens the `users` store, and never reads the `Host` header.
An orphaned link therefore keeps resolving at `/r/<slug>` throughout — which is
the whole reason cascade-deletion is rejected: a short URL that has been printed
or shared must not stop working because an employee left.

No new test is warranted here either (unlike the tags plan's `ParseLink` guard,
which existed because that plan added a field to the record; this plan changes
no record shape). `cd redirect && go test ./linkgate/...` is still run in
verification, purely to confirm the baseline is unaffected. **Never**
`go test ./...`, `go build ./...` or `go vet ./...` — they fail by design on
`package main` (`wit_exports.go:934:6: missing function body`).

## GUI changes

No new page, no new route, no new nav item, no new `.js` or `.css` file, **and
no CSS change at all**. Every change lands in files that already exist and are
already routed.

### `gui/dashboard.html`

One addition, immediately after `#tag-filter` (line 106):

```html
      <span id="owner-filter-wrap" hidden>
        <label for="owner-filter" class="visually-hidden">Filter by owner</label>
        <select id="owner-filter"><option value="">All owners</option></select>
      </span>
```

The wrapper exists so one `hidden` toggle covers both the label and the select —
a `visually-hidden` label pointing at a `hidden` control is an accessibility
wart. `[hidden] { display: none !important; }` in `theme.css` is what makes
`hidden` win against Pico's `display` here, the same load-bearing `!important`
`CLAUDE.md` records elsewhere.

**Zero inline code**: no `<script>`, no `<style>`, no `style="`, no `on<event>=`
— including inside comments, which `gui-pages/tests/test_no_inline_code.py` also
scans.

### `gui/dashboard.js`

Reuses `escapeHtml`, `allUsernames` (already populated in `loadMe()` from
`GET /api/users` when the principal holds `users.manage`), `getVisibleLinks`,
`renderLinksTable`, `rebuildTagFilterOptions`'s exact shape, and
`populateOwnerSelect`.

- **The deep-link parameter**, read once at module scope:
  ```js
  // The owner named by ?owner= on first load — the admin Users page links here
  // when a delete is refused because the user still owns links. Consumed once
  // and then cleared, so a later loadLinks() (after a bulk action) preserves
  // whatever the operator has since chosen instead of snapping back to the URL.
  let pendingOwnerFilter = new URLSearchParams(location.search).get("owner");
  ```
- **`allKnownOwners()`** — `[...new Set(allLinks.map((l) => l.owner))].sort()`.
  Record-derived, exactly like `allKnownTags()`, and correctly ownership-scoped
  for free because `allLinks` is already what `handle_list` returned for this
  principal.
- **`isDeletedOwner(owner)`** — `allUsernames.length > 0 && !allUsernames.includes(owner)`.
  The `length > 0` half is load-bearing: `allUsernames` is empty for anyone
  without `users.manage`, and without the guard every owner would be labelled
  deleted.
- **`rebuildOwnerFilterOptions()`** — modelled line-for-line on
  `rebuildTagFilterOptions()` (`dashboard.js:117-123`) and called from
  `loadLinks()` immediately after it. Preserves the current selection if it
  still exists; otherwise consumes `pendingOwnerFilter` (adding it as an option
  even when it matches no link, so a deep link that arrives one action too late
  still reads as "no links match your filter" rather than silently showing
  everything) and then sets it to `null`. Option labels get the
  ` — deleted account` suffix when `isDeletedOwner`. Finally:
  ```js
  wrap.hidden = allKnownOwners().length < 2 && !select.value;
  ```
  Fewer than two distinct owners means there is nothing to choose between —
  the `renderNewDomainsFieldset` precedent — but a filter that is *applied* is
  never hidden, because invisible applied state is worse than clutter.
- **`getVisibleLinks()`** — one more AND, next to the existing tag filter
  (`dashboard.js:232-233`):
  ```js
  const owner = document.getElementById("owner-filter").value;
  if (owner) visible = visible.filter((link) => link.owner === owner);
  ```
  **Exact equality, not a substring match** — the selection feeds a bulk
  reassign, and a fuzzy owner match would eventually move somebody else's link.
  For the same reason the free-text filter is **not** extended to match owner;
  its placeholder stays as it is.
- **The Owner cell** (`dashboard.js:324`) gains the marker and nothing else — no
  new column, no `colspan` change, no `nth-child` change:
  ```js
  <td>${escapeHtml(link.owner)}${isDeletedOwner(link.owner)
    ? ' <span class="status-badge status-disabled">deleted account</span>' : ""}</td>
  ```
- **The change listener**, beside the tag filter's (`dashboard.js:874`):
  ```js
  document.getElementById("owner-filter").addEventListener("change", renderLinksTable);
  ```
- **`populateOwnerSelect(select, selected)`** (`dashboard.js:183-188`) gains a
  stale-value branch, mirroring `users.js`'s `domainCheckboxesHtml` handling of a
  domain that is no longer configured (`gui/admin/users.js:37-47`): when
  `selected` is truthy and absent from `allUsernames`, prepend
  `<option value="…" selected disabled>… — deleted account</option>`. Two payoffs:
  the row edit form on an orphaned link shows the truth instead of silently
  pre-selecting the first real user, and — because the select's value then
  equals `link.owner` — `handleEditFormSubmit`'s
  `ownerSelect.value !== linkRecord.owner` check (`dashboard.js:459`) stops
  firing a spurious reassign confirmation every time someone saves a destination
  edit on an orphaned link. That is a live pre-existing wart this plan happens to
  close.
- **CSV export is unchanged.** `CSV_COLUMNS` already has an `Owner` column
  (`dashboard.js:926`), and adding a "(deleted)" annotation to exported data
  would put a display artifact in a data file.

### `gui/admin/users.html`

One element, immediately after `#users-error` (line 57):

```html
        <p id="users-error-action" hidden><a id="show-owner-links">Show these links on the dashboard</a></p>
```

The anchor's `href` is set by JS, so nothing needs `innerHTML` and there is no
inline handler.

### `gui/admin/users.js`

- **`canViewAllLinks()`**, alongside the existing `currentPrincipal` usage:
  `role === "admin" || permissions.includes("links.view_all") || permissions.includes("links.edit_all")`
  — the same disjunction `links.can_view` uses server-side.
- **The delete handler** (`users.js:202-216`) hides `#users-error-action` at the
  top of every attempt, and on failure builds a computed override:
  ```js
  const count = (data && typeof data.link_count === "number") ? data.link_count : 0;
  document.getElementById("users-error").textContent = friendlyError(data, "Could not delete user.", {
    user_owns_links:
      `"${username}" still owns ${count} link${count === 1 ? "" : "s"}. `
      + `Reassign or delete them first, then delete the account.`
      + (canViewAllLinks() ? "" : ` You'll need the "View all links" permission to do that.`),
  });
  ```
  and, only when the code is `user_owns_links` **and** `canViewAllLinks()`, sets
  `show-owner-links.href = \`../dashboard.html?owner=${encodeURIComponent(username)}\``
  and unhides the paragraph. Gating the link on view access is what stops a
  `users.manage`-only operator being sent to a dashboard that will show them
  nothing; they get a sentence naming the permission they need instead of a dead
  end.
  **The confirmation flow is untouched** — same single `confirmDialog`, same
  copy. The disposition surfaces as an informative failure, not as a second
  step, which is what keeps this from becoming a wizard.

### `gui/app.js`

One entry in `ERROR_MESSAGES`, as the generic fallback for any surface without a
count to hand:

```js
  user_owns_links: "That user still owns links — reassign or delete them first.",
```

## Trade-offs and rejected alternatives

### 1. Cascade-delete the user's links

**Attractive because** it is one round trip, it leaves nothing dangling by
construction, and it is what "delete this user and everything they own" means in
most admin tools.

**Why it loses:** it breaks live short URLs. A URL shortener's entire promise is
that a published short link keeps resolving; the links most worth caring about
are exactly the ones that have already been printed on something, handed to a
third party, or encoded into a QR code that outlives any later fix. The blast
radius is unbounded and invisible at the moment of clicking — the operator sees
a username, not 300 destinations — and there is no undo (`TASKS.md`'s Future-work
entry records that bulk-delete undo needs a tombstone record and a retention
policy that do not exist). It is also the single most irreversible operation the
app could offer, hidden behind a confirmation dialog whose text is about
deleting a *user*. The capability is not lost: filter by owner, look at the
rows, and use the existing count-bearing bulk delete — where the confirmation
names the number of links, because deleting links is what you are doing.

### 2. Require an explicit disposition in the delete request

**The alternative:** `reassign_to: "bob"`, or an explicit
`delete_their_links: true`, so deletion can never happen without a decision
having been made. **Attractive because** it is one round trip and it makes the
decision mandatory rather than merely blocked — which is a real advantage over a
409 that an operator might satisfy by cascade-deleting in a hurry anyway.

**Why it loses, on four counts:**
- **Nowhere clean to put it.** `gui/app.js:56`'s shared `api.delete` sends no
  body, and `TASKS.md`'s 2026-08-01 entry already rejected a semantically
  significant `DELETE` body as under-specified and inconsistently handled by
  intermediaries. That leaves query parameters —
  `DELETE /api/users/carol?links=reassign&to=bob` — which is a strange, easily
  mistyped place for an irreversible instruction, or a second endpoint, which is
  a bigger change than the 409.
- **It reintroduces unbounded per-request work.** N record writes plus index
  writes for an N nobody has capped, in a handler that today does three KV
  operations, with no transaction. `MAX_BULK_ROWS` exists precisely to keep this
  kind of work bounded and was deliberately *lowered* to 50 to keep it that way.
- **The decision is made blind.** The operator picks a disposition from the
  Users page, where the links are not visible, their destinations are not
  visible, and the count is whatever the error message last said. This is the
  shape `TASKS.md` rejected on 2026-08-03 for whole-tag actions.
- **It implements reassignment twice.** `bulk-action` already owns the
  record-then-index dance and its interruption analysis; the tags plan's
  trade-off #6 explicitly rejected a second implementation of it for the
  single-link case. A third would be worse.

**What would justify revisiting:** a deployment where users routinely own more
than a few hundred links, making the 50-at-a-time loop genuinely painful — at
which point the right answer is probably a paginated server-filtered list
endpoint plus a raised cap, not a disposition parameter.

### 3. Auto-reassign the links to the deleting admin

**Attractive because** no link is ever lost, nothing dangles, and the operator
is not blocked. It is the "just make it work" option.

**Why it loses:** it hides the decision at exactly the moment it should be made,
and it makes the deleting admin the owner of record for links they may know
nothing about — which is worse than an obvious orphan, because it *looks*
correct. One admin who offboards five people ends up owning several hundred
links, an unbounded `owner_links:<admin>` index, and a dashboard they can no
longer use. It also silently changes data in a store the request was not
ostensibly about, with unbounded writes and no operator awareness that they
happened, so an interruption leaves a half-moved set that nobody knows to
re-drive. If a deployment genuinely wants this policy, the 409 costs one extra
click to enact it deliberately.

### 4. Do nothing (accept it as a documented limitation)

**Live, and worth taking seriously:** the inheritance needs an admin to recreate
the exact username, so it is not remotely exploitable, and this app has a
documented, honourable tradition of accepting disclosed limitations rather than
over-engineering (`CLAUDE.md`, Security tradeoffs).

**Why it loses:** every accepted limitation in that section stems from a real
architectural constraint — no outbound network, no atomic KV. This one stems
from a missing three-line check. The quiet half is also not a limitation but an
accumulating defect: every departed employee permanently adds links nobody
without `links.edit_all` can edit and an index key nothing cleans up, and the
count only ever goes up. And the session-revival vector found during research is
a privilege-retention bug in an authentication system, which is not the kind of
thing this repo has ever chosen to merely disclose.

### 5. Prevent username reuse with a tombstone / reserved-usernames list

Argued in full under "Username reuse" above. Summary: it forbids an ordinary
administrative act to paper over two specific inheritance vectors that this plan
closes directly; to be sound it must be permanent, and a permanent reservation
with no escape hatch gets worked around while an escape hatch restores the
vector on the path an operator uses when in a hurry.

**A near neighbour, also rejected: give users a stable identifier and stamp it
into sessions** — e.g. a `uid` on the user record, copied into each session
record and compared in `resolve_session`. Genuinely more robust than purging: it
is O(1), it needs no key enumeration, and it would also defeat a revival via a
restored backup or a purge that missed something. It loses on the legacy gap,
which is the wrong shape: existing user records have no `uid` and existing
sessions have none either, so `None == None` passes and precisely the accounts
that predate the change keep the vulnerability — the opposite of what a security
fix should do. Making it fail-closed instead signs everyone out on deploy and
still needs a backfill for the user records. Purging is complete for every
account the moment it ships, and reuses a plumbing pattern (`list_keys`) that is
already in the file next door. **Revisit if a fourth username-keyed artifact
appears that cannot be cleaned up at deletion time.**

**A third neighbour, also rejected: a `user_sessions:<username>` index** so the
purge is one read instead of a key walk. It would be another read-modify-write
index with no compare-and-swap, written on every login and every logout, to
speed up an operation that happens a handful of times a year — and a drifted
session index fails in the direction of *not* purging a session, which is the
failure this whole change exists to prevent.

### 6. Delete an owner index key when its list becomes empty

**The alternative:** change `links.remove_slugs_from_indexes` and
`links.move_slugs_between_owners` so that an `owner_links:<owner>` list that
becomes empty is `delete`d rather than written as `[]`. **Attractive because**
it would eliminate the whole class of dangling empty index keys — including the
one that repairing an orphan leaves behind for a user who has already been
deleted and so will never be deleted again.

**Why it loses:** an empty `owner_links:` key is indistinguishable from an absent
one in every code path — `_owned_slugs` returns `[]` for both
(`api/links.py:41-43`), `handle_list` shows nothing either way,
`add_slugs_to_indexes` refills either way, backup captures an empty array and
restore writes it back harmlessly. So the change is tidiness with zero
behavioural payoff, and it would be paid for by touching two of the most
load-bearing, most-tested helpers in the component: it invalidates existing
assertions in `test_links.py:757-759` and `test_bulk.py:616-617` (both count
`set()` calls per distinct owner) and restates their "exactly one index write
per distinct owner" property as "one write-or-delete", in the same commit as a
security fix. `handle_delete` deletes `owner_links:<username>` because that key
is what the *next* holder of the name would inherit and "deletion leaves nothing
behind under that username" is a statement worth being able to test — but that
is one line in a new code path, not a change to a shared write helper.
**What would justify revisiting:** a real need to enumerate index keys (the
consistency endpoint) where empty keys become noise worth removing at the
source.

### 7. A one-shot repair endpoint (`POST /api/admin/reap-orphans`)

**Attractive because** an existing deployment gets fixed in one click instead of
50 links at a time.

**Why it loses:** it is an unbounded, destructive-or-mutating server-side action
whose blast radius is invisible at the moment of clicking — the third time this
document rejects that shape — and it overlaps a backlog item that is already
scoped and deliberately read-only (`GET /api/admin/consistency`, TASKS.md:297,
which exists precisely because *"it reports and never silently fixes"*).
Building a repairing sibling before the reporting one exists gets the order
exactly backwards. The realistic scale is also small: one departed employee's
links, once, per deployment.

### 8. Make the owner filter a text match instead of a select

**Attractive because** it is one line — add `link.owner` to the existing
free-text filter's match — and needs no new markup.

**Why it loses:** the filtered set feeds a bulk reassign. A substring match on
`carol` also matches the slug `carols-secret` and any destination containing the
string, so select-all-then-reassign would eventually move a link that was never
carol's. Exact equality is the whole point, and a `<select>` is also the
established affordance in this row (`#tag-filter`, chosen over clickable chips
for the 44px tap-target reason recorded in `DESIGN.md:212`).

## Tasks

Appended verbatim to `TASKS.md` under `## User deletion and link ownership`.
`TASKS.md` is authoritative; this mirror does not track checkbox state.

```
- [ ] Add auth.delete_sessions_for_user, a list_keys-driven session purge — file(s): api/auth.py, api/tests/test_auth.py — done when: `SESSION_PREFIX = "session:"` exists as a module constant (used by the new function only; the three existing `f"session:{token}"` literals are deliberately left alone) and `async def delete_sessions_for_user(store, username, list_keys) -> int` deletes every `session:`-prefixed key whose decoded JSON `username` equals the argument and returns how many it deleted, skipping non-`session:` keys, keys whose value has vanished, and values that are not valid JSON rather than raising; `api/auth.py` still has zero `spin_sdk` imports; and `cd api && uv run pytest` passes with new tests (using `tests/fakes.py`'s existing `fake_list_keys`) covering two sessions for the target user plus one for another user — only the two are deleted and the third still resolves through `auth.resolve_session` afterwards — a store with no sessions returning 0, and a `session:` key holding `b"not json"` being skipped rather than raising.
- [ ] Refuse to delete a user who still owns links, and clean up what deletion leaves behind (depends on the session purge; changes `users.handle_delete`'s signature, so the `api/app.py` wiring must land in the same commit or the app is broken between tasks) — file(s): api/users.py, api/app.py, api/tests/test_users.py — done when: `handle_delete(store, links_store, principal, username, list_keys)` reads `owner_links:<username>` via `links._owned_slugs(links_store, username)` after the existing users.manage/404/cannot_delete_self checks and returns `409 {"error": "user_owns_links", "username": ..., "link_count": n}` having written **nothing to either store** when that list is non-empty; on the empty path it deletes `owner_links:<username>` from the links store FIRST, then purges the user's sessions, then deletes `user:<username>`, then removes the name from `_meta:usernames`; `api/app.py`'s `/api/users/{username}` DELETE arm opens the `links` store inside that arm only and passes it plus the existing `_kv_keys`; and `cd api && uv run pytest` passes with the four existing `handle_delete` tests updated for the new signature plus new tests that a user owning one link gets a 409 whose `link_count` is 1 with both FakeStores byte-identical to before the call, that a user whose `owner_links:` key exists but is empty is deleted and that key is **absent** afterwards (not `b"[]"`), that a user who never had an index key deletes cleanly, and that deletion removes that user's session while another user's session still resolves.
- [ ] Pin the ownership-inheritance and session-revival scenarios with an end-to-end regression test (depends on both API tasks; a deliberately cross-module scenario file, since it exercises users + links + bulk + auth together) — file(s): api/tests/test_user_deletion.py (new) — done when: `cd api && uv run pytest` passes with a test that walks the reported sequence — carol is created, carol creates the custom slug `carol-private`, `users.handle_delete` returns 409, the link is reassigned to another user through `bulk.handle_bulk_action`'s `reassign` action, carol is then deleted with a 200, a **new** carol is created with a different password, and the new carol's `links.handle_list` returns zero links while her `links.handle_update` of `carol-private` returns 403 and the record's `target_url` is unchanged — and a second test that a session token issued to the first carol resolves to a Principal before deletion, resolves to `None` after deletion, and **still** resolves to `None` after a new carol with a different role is created under the same username.
- [ ] Add an owner filter and a deleted-account marker to the dashboard (independently landable; the `?owner=` parameter is what the next task links to) — file(s): gui/dashboard.html, gui/dashboard.js — done when: an `#owner-filter` select inside a `#owner-filter-wrap` span (with a `visually-hidden` label) sits next to `#tag-filter`, is repopulated from the distinct `owner` values across `allLinks` on every load, ANDs an **exact-equality** match with the text and tag filters, is hidden when there are fewer than 2 distinct owners **unless** a filter is currently applied, and applies `?owner=` once on first load and then never again; owners absent from `allUsernames` are labelled `— deleted account` in the select and get a `<span class="status-badge status-disabled">deleted account</span>` inside the existing Owner cell, but only when `allUsernames` is non-empty (a viewer without users.manage must not see every owner labelled deleted); `populateOwnerSelect` prepends a `selected disabled` option for a current owner who is not in `allUsernames`, so saving a destination edit on an orphaned link no longer fires a spurious reassign confirmation; **no new column, no colspan change, no nth-child change, no CSS change, no new .js or .css file**; `cd gui-pages && uv run pytest` still passes at exactly 64 with zero inline `<script>`/`<style>`/`style="`/`on<event>=`; and in a real browser filtering by an owner shows only that owner's links and select-all then Reassign moves exactly those.
- [ ] Report the 409 on the admin users page and link to the disposition (depends on the API tasks and the dashboard owner filter) — file(s): gui/admin/users.html, gui/admin/users.js, gui/app.js — done when: a `<p id="users-error-action" hidden>` containing an `<a id="show-owner-links">` sits after `#users-error` and is hidden at the top of every delete attempt; a `user_owns_links` failure sets `#users-error` via `friendlyError`'s existing `overrides` parameter to a message naming the username and the exact `link_count` with correct singular/plural, and reveals the anchor pointing at `../dashboard.html?owner=<encodeURIComponent(username)>` **only** when the viewer holds admin/`links.view_all`/`links.edit_all` — a viewer without them instead gets a sentence naming the permission they need, and no dead link; the existing single `confirmDialog` and its copy are unchanged (no extra step, no wizard); `app.js`'s `ERROR_MESSAGES` gains `user_owns_links` as a generic fallback; `cd gui-pages && uv run pytest` still passes at 64 with zero inline code; and in a real browser deleting a user who owns links shows the count, following the link lands on a dashboard pre-filtered to that owner, and deleting the same user after reassigning succeeds.
- [ ] Document user deletion and link ownership in CLAUDE.md, PRODUCT.md and DESIGN.md (depends on every task above) — file(s): CLAUDE.md, PRODUCT.md, DESIGN.md — done when: CLAUDE.md gains a "User deletion and link ownership" section (peer to "Bulk link management") stating that `DELETE /api/users/{username}` returns `409 user_owns_links` with a `link_count` and writes nothing on that path, why refusal was chosen over cascade/auto-reassign/disposition-parameter, the cross-store write order (links store first, then sessions, then the user record, then `_meta:usernames`) with what each interruption leaves, that sessions are purged at delete time and that this — not a reserved-username tombstone — is what makes username reuse safe, that the gate reads the `owner_links:` index rather than walking every record so index drift can still let an orphan through, and that the dashboard's record-derived owner filter is the repair path while `GET /api/admin/consistency` remains the right home for detection at scale; PRODUCT.md's Capabilities list gains one accurate line; DESIGN.md's `### Status Badges` gains the deleted-account marker as the third reuse of the disabled treatment (no new token, no new measurement) and `### Inputs / Fields` gains the owner filter as the third control in the links filter row with its hidden-below-two-owners rule; and no doc claims a capability the shipped code does not have.
- [ ] End-to-end manual verification of user deletion and link ownership — file(s): (none — verification step) — done when: every numbered step in docs/plans/user-deletion-link-ownership.md's Verification section is executed against a real `spin up --build --runtime-config-file runtime-config.toml` in a browser with the console open and **zero errors of any kind, in particular zero CSP violations, in both light and dark themes**; deleting a link-owning user is refused with the correct count, the deep link lands pre-filtered, reassigning then deleting succeeds, the reassigned link still returns a 302 at `/r/<slug>`, a session cookie captured before deletion does not work after the username is recreated, and `cd api && uv run pytest`, `cd gui-pages && uv run pytest` and `cd redirect && go test ./linkgate/...` all pass.
```

## Critical files

- `api/auth.py`
- `api/users.py`
- `api/app.py`
- `api/tests/test_auth.py`
- `api/tests/test_users.py`
- `api/tests/test_user_deletion.py` **(new)**
- `gui/dashboard.html`
- `gui/dashboard.js`
- `gui/admin/users.html`
- `gui/admin/users.js`
- `gui/app.js`
- `CLAUDE.md`
- `PRODUCT.md`
- `DESIGN.md`
- `TASKS.md`

Deliberately **not** touched: `spin.toml`, `runtime-config.toml`, `Jenkinsfile`
(test invocation is unchanged), `gui-pages/` (no new page or route),
`gui/theme.css` and `gui/dashboard.css` (no new style),
`.impeccable/design.json` (no new token), `api/links.py`, `api/bulk.py`,
`api/backup.py` (no new KV key type), and all of `redirect/`.

## Verification

Run in this order.

1. **Unit suites, after each task lands:**
   ```bash
   cd api && uv run pytest
   cd gui-pages && uv run pytest
   cd redirect && go test ./linkgate/...
   ```
   Baseline at `8678387` is **345 / 64 / ok**. `gui-pages` must still read
   exactly **64** — this plan adds no page and no `.js` file, so any movement
   there means something unplanned was added. Never `go test ./...`.

2. **Confirm the redirect component and the manifest are untouched:**
   ```bash
   git diff --stat main -- redirect/ spin.toml gui-pages/ gui/theme.css gui/dashboard.css
   ```
   Must be empty.

3. **Boot the app:**
   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```

4. **Seed.** Log in as `admin` at `http://localhost:3000/login.html`. On
   `/admin/users.html` create `carol` (permissions: `links.create_custom_slug`),
   `dave` (no permissions) and `peoplemgr` (`users.manage` only). Log in as
   `carol` and create a link with the custom slug `carol-private` pointing at
   `https://internal.example.com/carols-secret`.

5. **The refusal.** As `admin`, delete `carol` on `/admin/users.html`. Confirm
   the dialog. Expect the error line to read
   `"carol" still owns 1 link. Reassign or delete them first, then delete the
   account.` and a visible "Show these links on the dashboard" link. Confirm on
   `/dashboard.html` that `carol-private` is still listed and still owned by
   carol — **nothing was written**.

6. **The deep link and the filter.** Click the link. The dashboard must load
   with the owner filter set to `carol` and exactly one row visible. Select it,
   choose `dave` in the bulk owner select, click Reassign, confirm the
   count-bearing dialog.

7. **The delete now succeeds.** Back on `/admin/users.html`, delete `carol` →
   the row disappears and no error is shown.

8. **The reproduction, live.** Recreate `carol` with a different password. Log
   in as the new carol: `/dashboard.html` must show **no links** (heading "Your
   links", empty state). Then confirm the API directly:
   ```bash
   curl -s -X PATCH http://localhost:3000/api/links/carol-private \
     -b "session=<new carol session>" -H "X-CSRF-Token: <new carol csrf>" \
     -H 'Content-Type: application/json' \
     -d '{"target_url":"https://attacker.example.com/"}'
   ```
   Expect `403`. Then `curl -sI http://localhost:3000/r/carol-private` → `302`
   with the **original** `Location`. This is the whole point: the link keeps
   working and the new account cannot touch it.

9. **Session revival.** Log in as a fresh throwaway user in a second browser
   profile and copy its `session` cookie value. As `admin`, delete that user
   (they own no links) and confirm the delete is fast — this is the request that
   now does a `get_keys` drain (the one UNCONFIRMED item above). Then:
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' http://localhost:3000/api/auth/me \
     -b "session=<captured token>"          # expect 401
   ```
   Recreate the same username with role `admin`, and repeat the same curl:
   **still 401.** Before this change it returned `200` with the new account's
   role.

10. **The deleted-account marker and repair path.** Using the KV explorer
    (`./dev/kv-explorer-up.sh`, and note local KV is non-persistent so re-seed
    first), hand-edit one link record's `owner` to a username that does not
    exist. Reload `/dashboard.html` as `admin`: the Owner cell must show
    `ghost` followed by a `Deleted Account` badge, the owner filter must offer
    `ghost — deleted account`, and selecting it plus Reassign must move the link
    and clear the marker.

11. **Permission split.** Log in as `peoplemgr` (`users.manage`, no
    `links.view_all`). Delete a user who owns links: the message must name the
    count **and** add "You'll need the \"View all links\" permission to do
    that." with **no** dashboard link rendered. Confirm the same 409 by curl:
    ```bash
    curl -s -X DELETE http://localhost:3000/api/users/<owner> \
      -b "session=<peoplemgr session>" -H "X-CSRF-Token: <peoplemgr csrf>"
    ```
    → `409 {"error":"user_owns_links","username":"…","link_count":N}`.

12. **The filter hides itself.** Log in as `dave` (owns links now, no
    `links.view_all`). The owner filter must not be visible at all — one distinct
    owner, nothing to choose between.

13. **Themes and console.** Repeat steps 5, 6 and 10 with the nav theme control
    on Dark. **Zero console errors and zero CSP violations in both themes** — a
    CSP violation fails a page silently in a browser rather than failing a test,
    which is why this is a manual step.

14. **Re-run all three suites** (step 1) after the docs task, and confirm
    `git diff --numstat TASKS.md` shows only the checkbox lines.

## Out of scope / follow-ups

- **`GET /api/admin/consistency`.** Untouched, deliberately. Two checks belong in
  it when it is built and are appended to `TASKS.md`'s Future-work note: an
  `owner_links:<username>` key whose username is not in `_meta:usernames`, and a
  `slug:` record whose `owner` is not a known user. This plan detects both by
  eye, at the point of action, and adds no endpoint.
- **A stable per-user identifier (`uid`) decoupling identity from the username
  string.** The structurally correct fix for the whole class of problem, rejected
  here on the legacy-gap argument in Trade-offs #5. Appended to Future work; the
  trigger is a fourth username-keyed artifact that cannot be cleaned up at
  deletion time.
- **Deleting an empty `owner_links:` key from the shared index helpers.**
  Trade-offs #6. Appended to Future work, triggered by the consistency endpoint
  actually wanting to enumerate index keys.
- **A `links.tag` checkbox on the users page's *create* form.** Found while
  reading `gui/admin/users.html:40-43`: the create fieldset's four hardcoded
  checkboxes never gained `links.tag`, while `users.js`'s `ALL_PERMISSIONS`
  (which drives the *edit* form) has it. So the permission can be granted by
  editing a user but not by creating one. A pre-existing one-line gap, unrelated
  to this work, appended to Future work rather than smuggled into a security fix.
- **Bulk-reassigning more than 50 links in one go.** Unchanged: `MAX_BULK_ROWS`
  applies, and a departed user with 200 links takes four rounds. Raising the cap
  needs real timing evidence from a full-cap submission (`CLAUDE.md`, Bulk link
  management), which this plan does not gather.
- **Transferring analytics or QR history along with ownership.** There is
  nothing to transfer — analytics keys are slug-scoped, not user-scoped, so a
  reassigned link keeps its entire click history automatically.
- **Preventing the *last* admin from being deleted.** Adjacent, real, and
  entirely separate from link ownership. Not raised by the requester; not added.
