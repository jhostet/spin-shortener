# Read-Only KV Consistency Check

## Context

**The motivating gap is a live hole in work that shipped on 2026-08-04.**

`api/users.py`'s `handle_delete` refuses to delete a user who still owns links,
and it makes that decision from **one KV read**: `links.owned_slugs(links_store,
username)`, which is `owner_links:<username>` (`api/links.py:41-46`). That was a
deliberate choice — an O(all links) walk inside a request that otherwise does
three KV operations was rejected — and `docs/plans/user-deletion-link-ownership.md`
states the residual plainly:

> if the index has drifted low (an interrupted write, a KV-explorer edit), a
> record whose `owner` is carol but which is missing from `owner_links:carol`
> slips through the gate.

So a link record owned by carol that has drifted out of `owner_links:carol`
**does not block carol's deletion, and is orphaned by the very flow built to
prevent orphans.** The dashboard's record-derived owner filter catches that by
eye at the point of action (`gui/dashboard.js`'s `allKnownOwners()` reads
`allLinks[].owner`, not the index). Nothing detects it at scale. That is the
concrete reason this endpoint is worth building now, ahead of more featureful
backlog items.

The second reason is that `CLAUDE.md` now carries **four separate "what an
interruption leaves" paragraphs** — bulk create, bulk delete ("Bulk link
management"), backup restore ("KV backup and restore"), owner reassignment
("Link tags and ownership") and user deletion ("User deletion and link
ownership") — each describing a store state that an operator has, today, no way
whatsoever to observe. Those paragraphs document conditions that are currently
invisible. This endpoint makes every one of them observable.

This work is the union of two `TASKS.md` Future-work entries, both of which this
plan resolves:

- **2026-08-02**, raised while planning `docs/plans/kv-backup-restore.md`
  (`TASKS.md:304`): unindexed `slug:` records, `all_links` entries with no
  backing record, and `owner_links:<user>` disagreeing with the records' own
  `owner`. It states the core rule — it **reports and never silently fixes** —
  and says it was kept out of restore deliberately so as not to blur what
  restore promises.
- **2026-08-04**, raised while planning
  `docs/plans/user-deletion-link-ownership.md` (`TASKS.md:310`): a dangling
  `owner_links:<username>` for a user who no longer exists, and a `slug:` record
  whose `owner` is not a known user — with the note that the second "is the one
  that matters" for exactly the reason above.

**Confirmed decisions** (settled by the requester before planning; recorded so a
future reader knows they were deliberate, not re-litigated here):

1. **It reports; it never repairs.** No auto-fix, no `?fix=true`, no repair
   endpoint anywhere in this plan. The manual repair paths already exist (the
   dashboard owner filter, the bulk reassign/delete tools). A repair companion,
   if ever wanted, is future work — recorded as such below.
2. **`redirect` is not touched.** This is `api` plus GUI.
3. Rejected alternatives get dated (2026-08-04) entries under `TASKS.md`'s
   `## Considered and rejected`.
4. Both Future-work entries above are marked resolved when the new work section
   is appended.

## Key technical facts confirmed during research

- **Baseline, measured now at `c2da04e`:** `cd api && uv run pytest` → **354
  passed**; `cd gui-pages && uv run pytest` → **64 passed**; `cd redirect && go
  test ./linkgate/...` → **ok**. `git status` clean apart from unrelated
  untracked plan files.

- **`_kv_keys` works and is already in production use.** `api/app.py:18-33`
  drains the `(stream, future)` pair that `spin:key-value/key-value@3.0.0`'s
  `get-keys` returns; its docstring records it as a confirmed idiom from the
  backup spike. It is already passed into `backup.handle_export`
  (`api/app.py:186`), `backup.handle_restore` (`api/app.py:198`) and
  `users.handle_delete` (`api/app.py:175`). `api/tests/fakes.py:29-33` ships
  `fake_list_keys(store)` returning `store.keys()`. **This plan needs no new
  plumbing of any kind** — the callable, the parameter shape and the test double
  all exist.

- **Cost evidence, measured, from `TASKS.md:498`** (the backup feature's live
  verification note): *"a 4,402-entry / 1.78 MB restore took 84 ms, reproducible
  across two runs (~19 us per KV write)."* A restore does one `set` per entry
  plus one `delete` per stale key plus a full `get_keys` drain of every store.
  **This endpoint does strictly less work over strictly fewer stores**: one
  `get_keys` drain and one `get` per key, across two of the three stores, and it
  never opens the largest one (see the analytics decision below). A walk of the
  same 4,402 entries is therefore expected to land in the same tens of
  milliseconds. **UNCONFIRMED at the exact shape this endpoint uses** — the 84 ms
  figure is a restore, not a read walk. Verification step 8 measures a real run
  and records the number, and that measurement is the evidence any future input
  cap must be argued from.

- **`links.handle_delete` does not touch the analytics store.** Confirmed by
  reading `api/links.py`: it deletes `slug:<slug>` and calls
  `remove_slugs_from_indexes`, and nothing more. So **every deleted link leaves
  `count:<slug>` and up to `analytics_event_slots` `events:<slug>:<slot>` keys
  behind permanently, by design.** This is the fact that settles the analytics
  question below: an "orphan analytics" check would fire on normal, expected
  state, on every deployment, forever.

- **Empty `owner_links:` keys are normal, not drift.**
  `links.remove_slugs_from_indexes` writes `owner_links:<owner>` = `[]` rather
  than deleting it (`api/links.py:86`), and `move_slugs_between_owners` does the
  same for a fully-drained old owner (`api/links.py:122`).
  `docs/plans/user-deletion-link-ownership.md`'s trade-off #6 deliberately
  declined to change either helper. **No check in this plan ever reports an
  empty index key**, in any store — doing so would produce a finding for every
  user who has ever had all their links removed.

- **`owned_slugs` cannot distinguish an absent key from an empty one.**
  `api/links.py:45-46` is `raw = await store.get(...)` then `json.loads(raw) if
  raw else []`. Same conclusion as above, from the other direction.

- **`handle_list` already tolerates an index entry with no record.**
  `api/links.py`'s `handle_list` iterates the slug list and skips any slug whose
  `get_link` returns `None`. `api/users.py:54-57` does the identical thing for
  `_meta:usernames`. Both states are therefore inert at runtime, which is what
  makes them `info` rather than `warning` in the severity assignment below.

- **A `user:<U>` record with no `_meta:usernames` entry can still log in.**
  `auth.LocalAuthProvider.authenticate` (`api/auth.py:142-161`) calls
  `get_user(store, username)`, which reads `user:<username>` directly and never
  consults the index; `resolve_session` (same file) does the same. Meanwhile
  `users.handle_list` iterates `_meta:usernames`, so the account is **invisible
  to administration while remaining able to authenticate**. That asymmetry is
  why `unindexed_user` is a `warning`, and it is exactly the state
  `backup.restore_write_order` leaves behind if a restore is interrupted mid
  users-store (that function puts `_meta:usernames` last, deliberately).

- **A surviving session for a deleted user is the residue of a security fix.**
  `docs/plans/user-deletion-link-ownership.md`'s interruption table lists
  "partway through 6 — some sessions purged" as a real state, and CLAUDE.md's
  "User deletion and link ownership" section records that recreating a username
  used to revive such a session **with the new account's role and permissions**.
  Purging at deletion is what makes username reuse safe; a purge that did not
  finish silently reopens the vector. That is why `orphan_session` is included.

- **This endpoint never reads a `user:` record's value.** Checks 6-9 need only
  the *key names* (`user:<username>`), and check 10 needs only the `username`
  field of a `session:` value. So no code path in `consistency.py` ever holds a
  `password_hash`, and no field of a user record can leak into the report.
  Deliberate, and worth pinning with a test.

- **`gui/admin/backup.html` is already the operator-maintenance page and needs
  no new route.** It is in `gui-pages/routing.py`'s `ROUTES` at
  `/admin/backup.html`, its script is routed at `spin.toml:119`
  (`route = "/admin/backup.js"`), and it is reached by a plain in-body link from
  `gui/admin/users.html:54`. **Adding a third `<article>` to that page and a
  renderer to that script therefore changes no route, no `ROUTES` entry and no
  `test_routing.py` case.**

- **The nav is closed to new items, by the design system.** `DESIGN.md`'s
  Navigation section: *"The nav is full — the Backup page is reached from the
  page body, not from here"*, with a measured `scrollWidth` 716 vs
  `clientWidth` 700 overflow at 768px for a fifth item, and *"Treat the next nav
  addition as a redesign, not an insertion."* This plan adds no nav item and
  measures no nav width, because it adds nothing to the nav.

- **`gui-pages`'s test count must stay at exactly 64.** `PAGES` is derived from
  `routing.ROUTES` and `SCRIPTS` from a `gui/**/*.js` glob
  (`gui-pages/tests/test_no_inline_code.py`). This plan adds no page and no
  `.js` file, so **any movement in that number means something unplanned was
  added.**

- **There is no JavaScript test runner in this repo.** `TASKS.md`'s 2026-08-01
  rejection of client-side bulk parsing records the check: *"`find . -name
  package.json` outside `node_modules` returns nothing; the `Jenkinsfile` runs
  three Python/Go commands."* This is the reason the GUI renderer below is
  deliberately generic rather than carrying twelve bespoke per-check formatters.

- **`users.manage` is already equivalent to the `admin` role, by escalation.**
  `users.handle_update` has no self-promotion guard, so any `users.manage`
  holder can PATCH their own record to `{"role": "admin"}`. Confirmed by reading
  the handler; recorded first in `docs/plans/kv-backup-restore.md`. So gating on
  `users.manage` is not a weaker bar than `role == "admin"`.

- **`_require_session` applies `check_csrf` for writes and is a no-op for GET**
  (`api/app.py:40-47` → `auth.check_csrf`). A `GET /api/admin/consistency`
  therefore needs no CSRF token, exactly like `GET /api/admin/backup`.

- **Spin KV has no transactions, no snapshots and no compare-and-swap**
  (`CLAUDE.md`, "Security tradeoffs"). The consequence for this feature is
  spelled out under "Consistency of the consistency check" below — it is real
  and it is not fixable, only disclosed.

- **UNCONFIRMED: the wall-clock cost of two `get_keys` drains plus one `get` per
  key on a store with several thousand keys, under a real `spin up`.** See the
  cost bullet above. Verification step 8 is the measurement.

## The check set

Twelve checks. Each has a fixed id, a fixed severity, a documented cause, and a
documented operator action — a diagnostic nobody can act on is worthless.

Severity is one of exactly two values:

- **`warning`** — something is invisible, misattributed, or able to act when it
  should not be. The operator should do something.
- **`info`** — inert residue that the running code already tolerates by design.
  Worth knowing about, not worth acting on.

Findings within a check are **sorted deterministically** (by slug, then
username, then key) so two runs are diffable. Real KV key order is unspecified.

### Links store — index coherence

**1. `unindexed_link`** — `warning`
A `slug:<S>` record exists but `S` is not in `all_links`.
*Cause:* an interrupted bulk create (`CLAUDE.md`, Bulk link management: records
are written first, `add_slugs_to_indexes` last — deliberately, because this is
the recoverable direction), an interrupted restore of the links store
(`restore_write_order` puts index keys last for the same reason), or a
KV-explorer edit.
*Effect:* the link resolves at `/r/<S>` but is invisible to every dashboard for
a `links.view_all` viewer.
*Action:* there is **no GUI repair path for this one**, and saying so plainly is
part of the report's value. Two options: `curl -X DELETE /api/links/<S>` (works
by slug, needs no index — but destroys the link), or hand-add `S` to `all_links`
with the KV explorer (local dev only; `users` is withheld from it but `links` is
not). This is the strongest single argument for a future repair companion.
*Finding:* `{"slug": "...", "owner": "..."}`

**2. `missing_link_record`** — `info`
`all_links` names `S` but there is no `slug:<S>` record.
*Cause:* an interrupted bulk delete or single delete (records removed first,
indexes last).
*Effect:* none. `links.handle_list` skips any slug whose record is `None`.
*Action:* none required. Optionally remove the stale entry with the KV explorer.
Nothing in the app ever prunes it, so it persists indefinitely — inertly.
*Finding:* `{"slug": "..."}`

**3. `unindexed_owner_link`** — `warning` — **the motivating check**
A `slug:<S>` record has `owner: U`, `U` has a `user:<U>` record, and `S` is not
in `owner_links:<U>` (including the case where that key is absent entirely).
*Cause:* an interrupted bulk create, an interrupted reassignment
(`move_slugs_between_owners` adds to the new owner first, then removes from each
old owner — an interruption between those leaves the slug under both, which this
check does **not** fire on; the reverse ordering, which would trip this check, is
what the plan for that feature deliberately rejected), an interrupted restore, or
a KV-explorer edit.
*Effect:* **this is the state that defeats the user-deletion 409 gate.** `U`'s
own dashboard does not list the link, and `DELETE /api/users/<U>` succeeds
because it reads the index, orphaning the link.
*Action:* reassign the link to its stated owner via the dashboard's owner filter
plus bulk Reassign (selecting the same owner is a no-op on `all_links` and
rewrites `owner_links:<U>` correctly, because `move_slugs_between_owners` adds to
the new owner before removing from old ones and skips `old == new`). Do it
**before** deleting that user.
*Records whose owner has no `user:` record are deliberately excluded here* —
they are reported by check 6, whose action is different, and reporting them
twice would double the list for the single commonest root cause.
*Finding:* `{"slug": "...", "owner": "..."}`

**4. `owner_index_mismatch`** — `warning`
`S` is listed in `owner_links:<U>` but the `slug:<S>` record's `owner` is `V ≠
U`.
*Cause:* an interrupted reassignment, or a KV-explorer edit of either side.
*Effect:* the link appears in `U`'s dashboard but `U` cannot edit it —
`links.can_edit` is `record["owner"] == principal.username`, which is `V`. Every
Edit/Delete button on that row 403s.
*Action:* bulk Reassign the link to whichever owner is correct; that rewrites
both indexes and the record together.
*Finding:* `{"slug": "...", "indexed_under": "U", "record_owner": "V"}`

**5. `orphan_owner_index_entry`** — `info`
`S` is listed in `owner_links:<U>` but there is no `slug:<S>` record at all.
*Cause:* an interrupted delete (same event as check 2 — `remove_slugs_from_indexes`
rewrites `all_links` and every owner index in one call, so 2 and 5 usually fire
together).
*Effect:* none. `handle_list` skips it for a non-`view_all` viewer exactly as it
does for `all_links`.
*Action:* none required.
*Finding:* `{"slug": "...", "indexed_under": "U"}`

### Cross-store — links vs. users

For both of these, "a known user" means **a `user:<U>` record exists**, not "`U`
is in `_meta:usernames`". One definition throughout, and it is the one that
governs whether the account can actually authenticate and act
(`auth.get_user`/`resolve_session` read the record directly). Index-versus-record
disagreement in the users store is separately reported by checks 8 and 9, so
nothing is lost by this choice — and using `_meta:usernames` here would have made
check 7 fire on a user who exists and works perfectly but is missing from an
index, which is a different problem with a different fix.

**6. `unknown_link_owner`** — `warning`
A `slug:<S>` record's `owner` names a user with no `user:` record.
*Cause:* a user deleted before the 2026-08-04 gate shipped; a user deleted
through the gate while owning a link that had drifted out of their index (check
3 — this is the downstream consequence); a links-only restore taken before a
deletion; a KV-explorer edit.
*Effect:* the link keeps resolving (correct and deliberate — `redirect` never
reads `owner`), but nobody without `links.edit_all` can edit it, and its owner
can never be contacted.
*Action:* the dashboard's owner filter marks the owner `— deleted account`;
select and bulk Reassign or Delete.
*Finding:* `{"slug": "...", "owner": "..."}`

**7. `dangling_owner_index`** — `warning`
`owner_links:<U>` exists with **at least one slug** and there is no `user:<U>`
record. An empty `owner_links:<U>` is never reported (see the confirmed facts).
*Cause:* a user deleted before the 2026-08-04 gate shipped; an interrupted
deletion; a restore that reintroduced the index key without the user.
*Effect:* the listed slugs are attributed to a nonexistent account. If that
username is ever recreated, the new account inherits every one of them — the
exact inheritance vector the deletion work closed at the source.
*Action:* reassign or delete those links, then delete the key with the KV
explorer, or simply recreate-and-delete the username (deletion now removes the
key). Reassigning alone leaves the key present-but-empty, which is then no longer
reported.
*Finding:* `{"username": "...", "slug_count": n}`

### Users store

**8. `unindexed_user`** — `warning`
A `user:<U>` record exists but `U` is not in `_meta:usernames`.
*Cause:* an interrupted restore of the users store (`_meta:usernames` is written
last, by `restore_write_order`), or an interrupted `handle_create`.
*Effect:* **the account can still sign in and its sessions still resolve, but it
does not appear in `GET /api/users` and cannot be edited, disabled or deleted
through the GUI at all.** An account invisible to administration that can
nonetheless authenticate.
*Action:* re-run the restore, or add the name to `_meta:usernames`. Note the KV
explorer deliberately has no access to the `users` store, so this one is a curl
job against `PATCH /api/users/<U>`… which also 404s. Realistically: restore
again, or create a same-named user (`handle_create` 409s on `get_user` finding
the record — so in practice this needs a manual KV fix at the host, and the
report's job is to make the operator aware before it becomes a surprise).
*Finding:* `{"username": "..."}`

**9. `missing_user_record`** — `info`
`_meta:usernames` names `U` but there is no `user:<U>` record.
*Cause:* the documented non-retryable interruption window in user deletion
(`docs/plans/user-deletion-link-ownership.md`'s table, row "7 — `user:<u>`
deleted, before 8"): a second `handle_delete` 404s on the missing record, so the
stale name cannot be cleaned up through the API.
*Effect:* none. `users.handle_list` skips it, and `auth.add_username`
de-duplicates, so recreating the username converges it.
*Action:* none required; recreating that username resolves it.
*Finding:* `{"username": "..."}`

**10. `orphan_session`** — `warning`
One or more `session:<token>` records name a `username` with no `user:` record.
*Cause:* an interrupted session purge during user deletion (the documented
"partway through 6" window), or a session issued before the 2026-08-04 purge
shipped.
*Effect:* inert *while the username is absent* — `resolve_session` returns
`None`. **It stops being inert the moment that username is recreated**, at which
point the old cookie resolves again with the new account's role and permissions.
That is the vector CLAUDE.md's "Session revival, the worse one" paragraph
describes.
*Action:* create the username and delete it again — `handle_delete` now purges
sessions — or wait out `SESSION_TTL_SECONDS` (8 hours) and confirm on a re-run.
**Never recreate that username for a different person until this is clear.**
*Finding:* `{"username": "...", "session_count": n}` — **grouped by username,
and the token is never emitted**, in any field, for any reason.

### Any store

**11. `unreadable_value`** — `warning`
A key whose value could not be parsed into its expected shape: a `slug:` or
`session:` value that is not a JSON object, a `slug:` record with no string
`owner`, or an `all_links` / `owner_links:` / `_meta:usernames` value that is not
a JSON list of strings.
*Cause:* a KV-explorer hand-edit (it has full CRUD with no undo, and CLAUDE.md
records that "a stray click destroys local data"), a truncated write, or a
restore of a hand-edited file — `backup.validate_backup` checks base64 validity
and the users-store forbidden keys, but does **not** validate that a value is
well-formed for its key type.
*Effect:* varies by key; the key is excluded from every other check, so **one
unreadable value can mask other findings.** Re-run after fixing.
*Action:* inspect the key with the KV explorer and repair or delete it.
*Finding:* `{"store": "links"|"users", "key": "..."}`

**12. `unrecognized_key`** — `info`
A key in the `links` or `users` store matching none of the known shapes:
`links` → `slug:*`, `all_links`, `owner_links:*`; `users` → `user:*`,
`session:*`, `_meta:usernames`, `_meta:bootstrapped`.
*Cause:* a hand-typed KV-explorer key, or **a new KV key type added to the app
without updating `consistency.py`.**
*Effect:* none at runtime.
*Action:* if it is junk, delete it. **If it is a new key type someone added, add
it to `consistency.py`'s `KNOWN_KEY_SHAPES`** — the same obligation CLAUDE.md
already states for `backup.py`'s `INDEX_KEYS`/`restore_write_order` when a new
key type appears. This check is what makes forgetting loud instead of silent.
*Finding:* `{"store": "...", "key": "..."}`

### Deliberately not checked

- **Anything in the `analytics` store.** The endpoint never opens it. See
  "Trade-offs" — this is the single biggest scoping decision in the plan.
- **Expired `session:` records.** Nothing sweeps them, so they accumulate as
  normal operation; `resolve_session` checks expiry on every request. Reporting
  them would be noise by construction, the same failure mode as the analytics
  checks. A session sweeper is future work.
- **Empty `owner_links:` keys.** Normal, by two shipped helpers' explicit
  design.
- **Semantic validity of a record's contents** — an unreachable `target_url`, a
  malformed `start_at`, a tag outside the vocabulary. This endpoint checks that
  the store's *structures* agree with each other, not that individual values are
  good. A validity linter is a different feature with a different name.

## The report shape

`GET /api/admin/consistency` → `200 application/json`:

```json
{
  "format": "spin-shortener-consistency-report",
  "schema_version": 1,
  "generated_at": "2026-08-04T18:22:10Z",
  "generated_by": "admin",
  "ok": false,
  "stores_scanned": ["links", "users"],
  "scanned": {
    "links": {"keys": 128, "records": 61, "owner_indexes": 4},
    "users": {"keys": 12, "records": 5, "sessions": 6}
  },
  "totals": {"findings": 2, "checks_with_findings": 2, "checks_skipped": 0},
  "truncated": false,
  "max_findings_per_check": 100,
  "checks": [
    {
      "check": "unindexed_link",
      "severity": "warning",
      "count": 0,
      "truncated": false,
      "skipped": false,
      "findings": []
    },
    {
      "check": "unindexed_owner_link",
      "severity": "warning",
      "count": 1,
      "truncated": false,
      "skipped": false,
      "findings": [{"slug": "spring-sale", "owner": "carol"}]
    }
    // ... all twelve, always, in CHECKS order
  ]
}
```

Field-by-field rationale, and the decisions each one encodes:

- **Every check appears in every report, even at `count: 0`.** A check that is
  absent when clean is indistinguishable from a check that was never written. An
  all-zero report is the endpoint's own statement of what it looked for, which is
  what makes an all-clear trustworthy rather than merely quiet. This is the
  report-shape half of the brief's rule that a checker which cannot be shown to
  fire is worse than none.
- **`ok`** is `true` only when `totals.findings == 0` **and**
  `totals.checks_skipped == 0`. A report that could not run a check must never
  read as clean.
- **`skipped`** is `true` when a check's input index was unreadable, so it could
  not run. The dependency rules are exactly:
  - `all_links` unreadable → checks 1 and 2 skipped.
  - `_meta:usernames` unreadable → checks 8 and 9 skipped.
  - `owner_links:<U>` unreadable → `U` is excluded from checks 3, 4, 5 and 7
    (not skipped globally; other users are still checked). Without this rule an
    unreadable index would report every one of that user's links as unindexed —
    a false-positive storm from a single bad key.
  - In every case the offending key is also reported under `unreadable_value`.
- **`severity`** — two values only, `warning` and `info`, assigned per check
  above. The GUI groups on it. Two values rather than three or five because
  there is exactly one decision the operator makes from a finding: act, or don't.
- **`count` is always the true total**, even when `findings` is truncated.
- **`truncated`** per check, plus a top-level `truncated` that is `true` if any
  check truncated. **A cap never truncates silently** — CLAUDE.md's standing
  rule.
- **`max_findings_per_check`** is echoed in the payload so the client never
  hardcodes a limit it can drift from — the same rule `too_many_rows`/`max_rows`
  and `body_too_large`/`max_bytes` already follow.
- **`format` / `schema_version`** — copied from the backup document's
  conventions, deliberately. Nothing re-ingests this report today, so strictly
  they are not needed; they cost twenty bytes and they mean a report pasted into
  a ticket six months from now is self-identifying and its shape is versioned.
  Matching the sibling `/api/admin/` endpoint's document conventions is worth
  more than the bytes.
- **`scanned`** — the walk size, so the operator can see what the report covered
  and so the timing measurement has a denominator.
- **No `description` field.** Prose belongs in the GUI's own label map (the
  `BACKUP_ERROR_MESSAGES` precedent in `gui/admin/backup.js`) and in this
  document, not in a JSON payload that would then have to be maintained in two
  places.

### The cap

```python
MAX_FINDINGS_PER_CHECK = 100
```

A plain module constant in `api/consistency.py`, following `bulk.py`'s
`MAX_BULK_ROWS` and `backup.py`'s `MAX_BACKUP_ENTRIES` precedent — read by
exactly one function in one component, expressing a safety rail on what a single
`componentize-py` response can usefully carry, not an operator-tunable policy.

**Per check, not global.** A global cap would let one noisy check — say three
thousand `missing_link_record`s from a botched delete — crowd out the single
`unindexed_owner_link` that actually matters. Per-check guarantees that every
check which fired shows evidence.

**Why 100.** The bulk tools cap at 50 slugs per action, so 100 findings is
already two full bulk repairs' worth — well past what anyone hand-repairs in one
sitting. The exact `count` is always present, so nothing is hidden.

**There is deliberately no input cap** — no `too_many_keys` refusal, no
`?stores=` subsetting. Refusing to diagnose a large store is exactly backwards:
the large store is where you most need the diagnosis. The cost argument is in the
confirmed facts (this endpoint does strictly less work than a backup export over
strictly fewer stores, and a 4,402-entry restore measured 84 ms). If verification
step 8 measures something slow, that is a genuine finding to report loudly, and
an input cap becomes a deliberate follow-up decision made with the number in
hand — not a guess made now.

### Consistency of the consistency check

Spin KV has no transactions and no snapshots, so this walk is N independent reads
over a live store. **A write that lands mid-walk can produce a false finding** —
the classic case being a link created between the key listing and the `all_links`
read, which surfaces as a spurious `missing_link_record` (or, depending on
ordering, a spurious `unindexed_link`). Reordering the reads only moves which
direction is affected; it cannot close the window.

This is disclosed, not mitigated, and the mitigation is operational and stated in
three places (this plan, the GUI copy, CLAUDE.md): **re-run the report; a finding
that persists across two runs is real.** The realistic concurrent writer is a
person clicking Create, and the operator runs this deliberately, so the window is
small — but the report must not be read as a proof.

## API changes

All Python, in the `api` component. Nothing here is on the `/r/...` hot path, so
this follows the language-split rule without ambiguity (`CLAUDE.md`, "Why Go for
`redirect` but Python for `api`/`gui-pages`").

### New module: `api/consistency.py`

Zero `spin_sdk` imports. `store` objects and the `list_keys` callable arrive as
plain parameters; `Response`/`json_response`/`iso_now` come from `responses`.
`api/backup.py` is the model, line for line — constants, then pure functions,
then the one `handle_*` coroutine.

```python
MAX_FINDINGS_PER_CHECK = 100

CONSISTENCY_FORMAT = "spin-shortener-consistency-report"
SCHEMA_VERSION = 1

CONSISTENCY_STORES = ("links", "users")

ALL_SLUGS_INDEX_KEY = "all_links"          # == links.ALL_SLUGS_INDEX_KEY
USERNAMES_INDEX_KEY = "_meta:usernames"    # == auth.USERNAMES_INDEX_KEY
BOOTSTRAPPED_KEY = "_meta:bootstrapped"    # == auth.BOOTSTRAPPED_KEY
SLUG_PREFIX = "slug:"
OWNER_LINKS_PREFIX = "owner_links:"        # == backup.OWNER_LINKS_PREFIX
USER_PREFIX = "user:"                      # == backup.USER_PREFIX
SESSION_PREFIX = "session:"                # == auth.SESSION_PREFIX

# Ordered. Every check appears in every report, at count 0 when clean.
CHECKS: tuple[tuple[str, str], ...] = (
    ("unindexed_link", "warning"),
    ("missing_link_record", "info"),
    ("unindexed_owner_link", "warning"),
    ("owner_index_mismatch", "warning"),
    ("orphan_owner_index_entry", "info"),
    ("unknown_link_owner", "warning"),
    ("dangling_owner_index", "warning"),
    ("unindexed_user", "warning"),
    ("missing_user_record", "info"),
    ("orphan_session", "warning"),
    ("unreadable_value", "warning"),
    ("unrecognized_key", "info"),
)
```

The duplicated key literals carry the same `# == <module>.<NAME>` comment
convention `backup.py:45` already uses (`BOOTSTRAPPED_KEY  # == auth.BOOTSTRAPPED_KEY`).
Importing `links`/`auth` into `consistency.py` would work and is acyclic, but
`backup.py` deliberately duplicates rather than imports and this module should
match its sibling. Note it, do not invent a third convention.

Three functions, split so the analysis is pure and testable from literals while
the walk is testable from `FakeStore`s:

```python
async def collect(stores_by_name: dict[str, object], list_keys) -> dict:
    """The only I/O in this module. Returns the raw material `analyze` needs:

        {
          "link_records": {slug: {"owner": str}},   # parsed slug:<slug> records
          "all_links": list[str] | None,            # None if unreadable/absent
          "owner_index": {username: list[str]},     # readable owner_links: only
          "unreadable_owners": set[str],            # owner_links: that failed
          "usernames": list[str] | None,            # None if unreadable/absent
          "user_records": set[str],                 # from user: KEY NAMES only
          "session_usernames": list[str],           # from session: values
          "unreadable": [{"store": str, "key": str}],
          "unrecognized": [{"store": str, "key": str}],
          "scanned": {...},
        }

    Never raises on malformed data: every parse failure becomes an entry in
    `unreadable` and the key is excluded from everything else. A diagnostic that
    500s on a broken store fails exactly when it is needed.

    A `user:` record's VALUE is never read — only its key name — so this
    function can never hold a password_hash.
    """


def analyze(collected: dict) -> tuple[list[dict], dict]:
    """Pure. Returns (checks, totals). One entry per CHECKS member, always all
    twelve, in CHECKS order, each
    {"check", "severity", "count", "truncated", "skipped", "findings"}.
    Findings are sorted deterministically and capped at MAX_FINDINGS_PER_CHECK.
    """


def build_report(checks, totals, scanned, *, generated_at, generated_by) -> dict:
    """Pure — no I/O, no clock, no store. Assembles the document above."""


async def handle_consistency(
    stores_by_name: dict[str, object],   # {"links": store, "users": store}
    principal,
    list_keys,
) -> Response:
```

`handle_consistency`:

1. `if not principal.has_permission("users.manage"):` →
   `json_response(403, {"error": "forbidden", "required_permission": "users.manage"})`
   — byte-identical to `backup.handle_export`'s gate and to `users._forbidden()`.
   Inlined rather than imported, matching `backup.py`.
2. `collected = await collect(stores_by_name, list_keys)`
3. `checks, totals = analyze(collected)`
4. `json_response(200, build_report(checks, totals, collected["scanned"],
   generated_at=iso_now(), generated_by=principal.username))`

No query parameters. A `?checks=` filter was considered and rejected — the walk
is the cost and it has already happened by the time a filter could apply, so
filtering saves nothing and adds an allowlist to maintain.

### `api/app.py` wiring

One exact-path branch, immediately after the `/api/admin/restore` branch
(`api/app.py:189-199`):

```python
        if path == "/api/admin/consistency" and method == "GET":
            result = await _require_session(users_store, request)
            if isinstance(result, Response):
                return result
            links_store = await key_value.open("links")
            # The analytics store is deliberately NOT opened: orphan analytics
            # are normal (links.handle_delete never removes them), so a check
            # over them would fire on healthy state forever. See
            # docs/plans/kv-consistency-check.md's rejected alternatives.
            return await consistency.handle_consistency(
                {"links": links_store, "users": users_store}, result, _kv_keys,
            )
```

Plus `import consistency` in the alphabetical import block (between `bulk` and
`domains`). `_require_session` is a no-op for CSRF on GET.

### API surface summary

| Method | Path | Gate | Body |
|---|---|---|---|
| `GET` | `/api/admin/consistency` | `users.manage` | the report document above; `403 {"error": "forbidden", "required_permission": "users.manage"}` otherwise |

No new KV key type, no new permission, no write path anywhere. `api/backup.py`
needs no change.

### Why `users.manage`

Four reasons, in descending order of weight:

1. **It is the same bar as the two sibling endpoints** in the same namespace, on
   the same page, reached by the same operator: `GET /api/admin/backup` and
   `POST /api/admin/restore` both gate on it.
2. **The report is a superset of what other permissions already allow.** It
   names slugs across every owner (a `links.view_all` capability) and usernames
   (a `users.manage` capability). Anyone who can read this report can already
   read `GET /api/users` and `GET /api/admin/backup`, both of which contain
   strictly more.
3. **It is not weaker than `role == "admin"`.** `users.handle_update` has no
   self-promotion guard, so the two describe the same set of principals today. A
   role check would buy an appearance of strictness and a second, inconsistent
   notion of "admin" in a codebase whose permission model is the granular one.
4. **A new `admin.diagnostics` permission was rejected.**
   `auth.KNOWN_PERMISSIONS` is deliberately "a small, fixed, hardcoded
   vocabulary"; adding an entry that grants strictly less than an existing entry
   already implies is vocabulary growth for zero access-control gain — the same
   argument that rejected `backup.manage`.

## GUI changes

**No new page, no new route, no new `.js` or `.css` file, no `spin.toml` change,
no `gui-pages/routing.py` change, no `test_routing.py` case, no CSS change, no
new design token, and no nav item.** Every change lands in two files that
already exist and are already routed.

### `gui/admin/backup.html` — a third `<article>`

Appended inside the existing `#admin-content` div, after the Restore article, so
it inherits the page's existing `users.manage` hide (`backup.js` sets
`#admin-content`'s display to none for a viewer without the permission, and
reveals `#forbidden-notice`).

```html
      <article>
        <h2>Check store consistency</h2>
        <p>
          Reads the links and users stores and reports anything out of step —
          a link missing from an index, an index entry with no link, a link
          owned by an account that no longer exists.
          <strong>It only reports; it never changes anything.</strong>
          Most useful straight after a restore. If a finding appears while
          people are actively creating links, run it again — a report taken
          during a write can show a finding that isn't real, and a real one
          shows up on both runs.
        </p>
        <button type="button" id="consistency-btn" class="outline">Run consistency check</button>
        <p id="consistency-error" class="form-error" role="alert"></p>
        <div id="consistency-result" aria-live="polite"></div>
      </article>
```

Zero inline code — no `<script>`, no `<style>`, no `style="`, no `on<event>=`,
including inside comments, which `gui-pages/tests/test_no_inline_code.py` also
scans. `class="outline"` is a stock Pico modifier already used on this page
(`#restore-btn` is `outline secondary`); a read-only action should read as
neither primary nor destructive.

**The page's `<h1>`, `<title>`, `initHeader`'s `pageLabel` and
`gui/admin/users.html`'s link text all stay exactly "Backup and restore".** The
alternative — retitling the page "Maintenance" now that it hosts three tools —
was considered and rejected; see trade-offs. The trigger for revisiting is a
fourth tool landing on this page.

### `gui/admin/backup.js` — the renderer

Reuses, without reimplementing: `api.get`, `friendlyError`, `escapeHtml` (all
`gui/app.js`), and the page's existing `#admin-content` permission hide.

- **`CONSISTENCY_CHECK_LABELS`** — a local map from check id to a short title
  and a one-sentence meaning, following the `BACKUP_ERROR_MESSAGES` precedent
  already in this file (kept local because these strings matter only on this
  page). Falls back to the raw check id for an id it does not know, so a check
  added server-side without a label renders as itself rather than as `undefined`.
- **`renderConsistencyReport(report)`** — three groups, in this order:
  - **Needs attention** — `severity === "warning" && count > 0`.
  - **Informational** — `severity === "info" && count > 0`.
  - **Not checked** — `skipped === true`, with the note that an unreadable index
    key blocked it.

  Each rendered check is `<h3>{title} — {count}</h3><p>{meaning}</p><ul>…</ul>`,
  plus `<p>Showing the first {max_findings_per_check} of {count}.</p>` when
  `truncated`.
- **Findings render generically**, one `<li>` per finding, as the finding
  object's own key/value pairs: `Object.entries(f).map(([k, v]) => \`${k}:
  ${escapeHtml(String(v))}\`).join(" · ")`. **This is deliberate, not lazy.**
  There is no JavaScript test runner in this repo (`TASKS.md`, 2026-08-01), so
  twelve bespoke formatters would be twelve pieces of untested logic in a file
  that has none today; a generic renderer cannot go out of step with a finding
  shape it has never heard of. Every interpolated value goes through
  `escapeHtml` — the findings contain user-supplied slugs and usernames.
- **All-clear** — `report.ok === true` renders one line into
  `#consistency-result`:
  `<p class="form-success">No inconsistencies found — N keys scanned across the links and users stores.</p>`,
  reusing the class the page already uses for `#export-success`.
- **Errors** — `friendlyError(data, "Could not run the consistency check.",
  BACKUP_ERROR_MESSAGES)`; the only realistic failure is the shared `403
  forbidden`, which `gui/app.js`'s `ERROR_MESSAGES` already covers.

No new class, no new token, no `style=` attribute anywhere. `.form-error`,
`.form-success`, `<h3>`, `<p>` and `<ul>` are all already styled by Pico plus
`theme.css`.

## Redirect (Go) changes

**None**, and deliberately so. `redirect/main.go` resolves `slug:{slug}` → KV →
302; it never reads `owner`, never opens the `users` store, and never reads
`Host`. Every state this endpoint reports leaves `/r/<slug>` resolving exactly as
before — which is the whole reason the repairs are reassignments rather than
deletions.

`cd redirect && go test ./linkgate/...` is still run in verification purely as a
no-regression check. **Never** `go test ./...`, `go build ./...` or `go vet
./...` — they fail by design on `package main`
(`wit_exports.go:934:6: missing function body`).

## Trade-offs and rejected alternatives

### 1. Checking the `analytics` store — rejected

**Attractive because** it is the third store, it is by far the largest, and
"orphan `count:<slug>` for a link that no longer exists" sounds exactly like the
kind of thing a consistency check should find. Completeness has obvious appeal in
a diagnostic.

**Why it loses — decisively.** `links.handle_delete` deletes `slug:<slug>` and
rewrites the indexes and **does not touch the analytics store**, confirmed by
reading it. `bulk.handle_bulk_action`'s delete path does the same. So **every
link ever deleted, on every deployment, leaves `count:<slug>` and up to
`analytics_event_slots` `events:<slug>:<slot>` keys behind, permanently, by
design.** A check over them would report normal, expected, intended state as an
inconsistency — on a deployment with any history at all it would dominate the
report and never go to zero. A diagnostic that always finds something is a
diagnostic nobody reads, and it would poison the one property that makes this
endpoint worth having: that an all-clear means something.

Three supporting reasons: the recent-events ring buffer is already documented as
lossy and capped, so a missing `events:` slot carries no information; the
analytics store is the one that dominates key counts (CLAUDE.md's backup section
computes ~4,650 analytics entries against a handful of link entries at 150
links), so scanning it would multiply the walk cost by an order of magnitude for
zero signal; and orphan analytics are harmless in a way orphan links are not —
nothing reads `count:<slug>` except `GET /api/links/{slug}/analytics`, which
404s on the missing link first.

**What would justify revisiting:** deletion being changed to purge analytics.
Then "an orphan analytics key" would become a genuine anomaly rather than the
expected outcome, and a check would be worth adding in the same change.

### 2. A repair mode (`?fix=true`, or `POST /api/admin/repair`) — rejected

**Attractive because** a report whose top finding has *no GUI repair path at
all* (check 1, `unindexed_link`) is a frustrating artifact: it tells you
something is broken and then tells you to use a dev-only KV browser.

**Why it loses.** It was ruled out by the requester and the reasoning holds
independently. `docs/plans/kv-backup-restore.md` refused to make restore
repairing, on the grounds that a repair pass "would have to decide whether an
unindexed record is orphaned junk or a link someone still wants, and getting that
wrong deletes data." `docs/plans/user-deletion-link-ownership.md` rejected
`POST /api/admin/reap-orphans` for being "an unbounded, destructive-or-mutating
server-side action whose blast radius is invisible at the moment of clicking" —
the third time that document rejected that shape — and specifically noted that
building a repairing sibling before the reporting one exists gets the order
backwards. Building both in one change would repeat the mistake in one commit.

There is also a sequencing argument: **a repair tool designed before anyone has
seen a real report is designed from imagination.** Ship the report, look at what
real deployments actually contain, then decide. Recorded as future work with that
trigger.

### 3. A separate `gui/admin/consistency.html` page — rejected

**Attractive because** the backup page's `<h1>` says "Backup and restore" and
will now host a third, differently-shaped tool, and because
`docs/plans/kv-backup-restore.md` argued at length for giving a destructive tool
its own page rather than bolting it onto `admin/users.html`.

**Why it loses on three counts.** (a) That earlier argument was specifically
about not putting a *destructive* control next to routine work; this control is
read-only, and the adjacency runs the other way — the backup plan's own
Future-work entry says the report is "most valuable right after a restore", which
is literally this page. (b) The cost is real and entirely avoidable: a new
`spin.toml` route for the page's script, a new `gui-pages/routing.py` `ROUTES`
entry, a new `test_routing.py` parametrize case, a new `.js` file (which
`test_no_inline_code.py`'s glob would pick up, moving `gui-pages`'s count off 64
and destroying a useful invariant), and another in-body link from somewhere,
since `DESIGN.md` has closed the nav to new items. (c) A third page reachable
only by an in-body link from a second page reachable only by an in-body link from
a third is worse navigation than one maintenance page with three articles.

**A sub-alternative, also rejected: retitling the page to "Maintenance"** —
`<title>`, `<h1>`, `initHeader`'s `pageLabel` and `users.html`'s anchor text —
while keeping the filename. Honest, and cheap in isolation. It loses because
`DESIGN.md`'s Navigation section quotes the in-body anchor verbatim (`<a
href="backup.html">Backup and restore</a>`) as part of a **measured** overflow
finding, so the rename would churn a historical measurement record for a
cosmetic gain, and because it would leave a `backup.html` URL under a
"Maintenance" title, trading one small mismatch for another. **Revisit when a
fourth operator tool lands on this page**, and then rename the file, the route,
the `ROUTES` entry and the title together as one deliberate change. Recorded as
future work.

### 4. API-only, no GUI at all — rejected, but it was live

**Attractive because** it is genuinely the smallest honest thing: the endpoint is
the feature, the report is JSON, and `curl` renders it fine. It would cut the
plan roughly in half and touch no GUI file.

**Why it loses.** The operator who most needs this is the one who has just run a
restore — in a browser, on `admin/backup.html`, with the restore's own result
still on screen. Asking them to open a terminal and construct a cookie-bearing
`curl` at that exact moment is asking them not to run it. The GUI cost here is
unusually low precisely because the page already exists: two files, no route, no
new asset, no nav decision, no CSS, no token, and `gui-pages`'s test count
provably unchanged. When the incremental cost of the surface is that small and
the moment of need is that specific, API-only is false economy.

### 5. A global finding cap instead of per-check — rejected

**Attractive because** it bounds the response size directly, which is the thing
the cap actually exists to bound, and it is one number instead of a per-check
rule.

**Why it loses.** Findings are not fungible. One noisy `info` check — three
thousand `missing_link_record`s after a botched bulk delete — would consume the
entire budget and hide the single `unindexed_owner_link` that is the whole reason
this endpoint exists. Per-check guarantees every check that fired shows evidence,
which is what an operator triages from. The worst case is 12 × 100 findings,
which is a bounded and perfectly serviceable response.

### 6. Making `MAX_FINDINGS_PER_CHECK` a Spin variable — rejected

Same argument, same conclusion, as `TASKS.md`'s 2026-08-01 rejection of
`bulk_max_rows`. `analytics_event_slots` is a Spin variable because **two
components must agree on it**; this one is read by exactly one function in one
component. It would cost a `[variables]` entry, a `[component.api.variables]`
entry, a `variables.get` + `int()` in `app.py` and a parameter threaded through
the handler, to express a response-size rail rather than an operator policy. The
value is echoed in every report, so the GUI stays truthful if the constant
changes.

### 7. An input cap that refuses to scan a large store — rejected for now

**Attractive because** every other bounded operation in this codebase has one
(`MAX_BULK_ROWS`, `MAX_BACKUP_ENTRIES`, `MAX_BACKUP_BODY_BYTES`) and the house
rule is that unbounded per-request work is a hazard.

**Why it loses.** Those caps all bound *writes* or *response bytes*. This
endpoint writes nothing and its response is already bounded by
`MAX_FINDINGS_PER_CHECK`. Refusing to diagnose a large store is backwards — the
large store is where the diagnosis matters — and the measured evidence points the
other way (a 4,402-entry restore, which does strictly more work over strictly
more stores, took 84 ms). **Revisit with the number from verification step 8 in
hand**, not before; if that number is bad, an input cap plus a `?stores=` subset
parameter is the shape to add, following `backup.parse_stores_param` exactly.

### 8. Reporting expired sessions, and empty `owner_links:` keys — rejected

Both fail the same test as the analytics checks: they report normal, expected
state. Nothing sweeps expired sessions, so they accumulate as ordinary operation
and `resolve_session` handles them correctly on every request; and two shipped
index helpers deliberately write `[]` rather than deleting, so an empty index key
is the *designed* outcome of removing a user's last link. A report that fires on
either would never reach zero. A session sweeper is recorded as future work; the
empty-index-key question already has its own Future-work entry from
`docs/plans/user-deletion-link-ownership.md`'s trade-off #6, whose stated trigger
was "a real need to enumerate index keys (the consistency endpoint) where empty
keys become noise worth removing at the source" — **this plan is that endpoint,
and the answer is that they are not noise, because they are never reported.**

### 9. Do nothing — live, and rejected

**Worth taking seriously.** This repo has an honourable tradition of accepting
disclosed limitations, the dashboard owner filter already catches the commonest
case by eye, and the drift states are rare.

**Why it loses.** The five interruption paragraphs in `CLAUDE.md` are
documentation of conditions no operator can observe, which is a strange thing for
a codebase that documents this carefully. More concretely, the 2026-08-04
deletion gate has a stated, known hole that nothing detects, and "the operator
might notice on the dashboard" is not detection. And unlike the entries in
CLAUDE.md's "Security tradeoffs", this gap stems from no architectural constraint
at all — key enumeration works, the plumbing exists, and the whole feature is one
read-only module.

## Tasks

Appended verbatim to `TASKS.md` under `## Read-only KV consistency check`.
`TASKS.md` is authoritative; this mirror does not track checkbox state.

```
- [ ] Add api/consistency.py — the twelve checks as pure logic plus the two-store walk — file(s): api/consistency.py (new), api/tests/test_consistency.py (new) — done when: the module has zero `spin_sdk` imports and takes `store` objects and `list_keys` as plain parameters (verified by `cd api && grep -c spin_sdk consistency.py` → 0); `CHECKS` lists exactly the twelve ids and severities in docs/plans/kv-consistency-check.md's order; `collect(stores_by_name, list_keys)` reads only the `links` and `users` stores, never parses a `user:` record's value (only its key name), and turns every parse failure into an `unreadable` entry rather than raising; `analyze(collected)` is pure, returns all twelve checks in `CHECKS` order every time including at count 0, sorts findings deterministically, caps each check's `findings` at `MAX_FINDINGS_PER_CHECK = 100` while leaving `count` exact and setting `truncated`, and sets `skipped` per the plan's dependency rules (`all_links` unreadable → checks 1-2 skipped; `_meta:usernames` unreadable → checks 8-9 skipped; an unreadable `owner_links:<U>` excludes only U from checks 3/4/5/7); `handle_consistency` returns the exact body `{"error": "forbidden", "required_permission": "users.manage"}` with status 403 for a principal without that permission; and `cd api && uv run pytest` passes with unit tests for each check in isolation, for the three skip rules, for the 100-cap leaving `count` exact, for an all-clear setting `ok: true`, and for a store containing a `user:` record with a `password_hash` producing a report in which the string `password_hash` appears nowhere.
- [ ] Seed each of the twelve inconsistencies and assert the endpoint reports exactly it (depends on api/consistency.py) — file(s): api/tests/test_consistency_scenarios.py (new) — done when: `cd api && uv run pytest` passes with one test per check that seeds exactly that one inconsistency into FakeStores, calls `consistency.handle_consistency` with `fake_list_keys`, and asserts that check has `count == 1` (or the right `slug_count`/`session_count`) with the exact expected finding dict AND that **all eleven other checks have `count == 0`** and `ok` is `false`; plus a healthy-store test that builds links and users through the real handlers (`links.handle_create`, `users.handle_create`, `bulk.handle_bulk_create`, `bulk.handle_bulk_action` with `reassign`, `links.handle_delete`, `users.handle_delete`) and asserts the report is `ok: true` with `count == 0` on every one of the twelve and `checks_skipped == 0` — a checker that cries wolf on a healthy store is worse than none; plus a test that the motivating case (a `slug:` record owned by carol removed from `owner_links:carol`) reports `unindexed_owner_link` while `users.handle_delete(carol)` still returns 200, pinning the gap this endpoint exists to detect.
- [ ] Wire GET /api/admin/consistency into api/app.py (depends on the two tasks above) — file(s): api/app.py — done when: an exact-path `GET /api/admin/consistency` branch sits immediately after the `/api/admin/restore` branch, opens only the `links` store alongside the already-open `users` store with a comment naming why `analytics` is deliberately not opened, passes the existing `_kv_keys` as `list_keys`, and reaches `consistency.handle_consistency` through `_require_session` (which is a no-op for CSRF on GET); `import consistency` sits in the alphabetical block; `cd api && grep -n "analytics" app.py` shows no analytics store opened in the consistency branch; and against a live `spin up --build --runtime-config-file runtime-config.toml`, `curl -s -b "session=<admin>" http://localhost:3000/api/admin/consistency` returns 200 with `format: "spin-shortener-consistency-report"`, all twelve checks present, and `ok: true` on a fresh store, while the same request as a user without `users.manage` returns `403 {"error":"forbidden","required_permission":"users.manage"}`.
- [ ] Add the consistency article and its renderer to the existing backup page (depends on the endpoint) — file(s): gui/admin/backup.html, gui/admin/backup.js — done when: a third `<article>` inside the existing `#admin-content` holds an `<h2>Check store consistency</h2>`, copy stating plainly that it only reports and never changes anything and that a finding seen during active writes should be confirmed by a second run, a `#consistency-btn`, a `#consistency-error` `.form-error` and a `#consistency-result` container; `backup.js` gains a local `CONSISTENCY_CHECK_LABELS` map (following the existing `BACKUP_ERROR_MESSAGES` precedent, falling back to the raw check id for an unknown one) and a renderer that groups checks into Needs attention (warning, count > 0), Informational (info, count > 0) and Not checked (skipped), renders findings **generically** from each finding object's own key/value pairs with `escapeHtml` on every value, states "Showing the first N of M" when `truncated`, and renders a single `.form-success` all-clear line naming the scanned key count when `ok` is true; **no new page, no new route, no new .js or .css file, no spin.toml change, no routing.py change, no CSS change and no new design token** (verified by `git diff --stat -- spin.toml gui-pages/ gui/theme.css .impeccable/design.json DESIGN.md` being empty); and `cd gui-pages && uv run pytest` still passes at exactly **64** with zero inline `<script>`/`<style>`/`style="`/`on<event>=`.
- [ ] Document the consistency check in CLAUDE.md and PRODUCT.md (depends on every task above) — file(s): CLAUDE.md, PRODUCT.md — done when: CLAUDE.md gains a "KV consistency check" section (peer to "KV backup and restore") stating that `GET /api/admin/consistency` is read-only and gated on `users.manage`, listing the twelve check ids with their severities, recording that the `analytics` store is deliberately never scanned because `links.handle_delete` never removes analytics keys so orphans there are normal state, recording that empty `owner_links:` keys and expired sessions are deliberately never reported for the same reason, recording the `MAX_FINDINGS_PER_CHECK = 100` per-check cap and that `count` stays exact when `truncated`, recording that the walk has no snapshot so a finding seen during concurrent writes must be confirmed by a second run, and stating that `unindexed_owner_link` is the check that detects the known hole in `users.handle_delete`'s index-read gate; CLAUDE.md's existing "a new KV key type obliges a `backup.py` change" rule (in "Link tags and ownership") gains `consistency.py`'s `KNOWN_KEY_SHAPES` alongside `backup.py`'s `INDEX_KEYS`/`restore_write_order`, since a new key type otherwise reports itself as `unrecognized_key` on every run; PRODUCT.md's Capabilities list gains one accurate line saying the report never repairs; DESIGN.md and .impeccable/design.json are **not** touched (no new pattern, no new token); and no doc claims a capability the shipped code does not have.
- [ ] End-to-end manual verification of the KV consistency check — file(s): (none — verification step) — done when: every numbered step in docs/plans/kv-consistency-check.md's Verification section is executed against a real `spin up --build --runtime-config-file runtime-config.toml` in a browser with the console open and **zero errors of any kind, in particular zero CSP violations, in both light and dark themes**; a fresh store reports `ok: true` with all twelve checks at 0; each of the nine links-store-reachable inconsistencies is seeded live through `./dev/kv-explorer-up.sh` and reported (checks 8, 9 and 10 are unreachable live because the explorer deliberately has no `users` access, and are covered by the scenario tests only — state that rather than claiming otherwise); the motivating case is demonstrated end to end (remove one slug from `owner_links:carol` while the record still says carol → the report shows `unindexed_owner_link` **and** `DELETE /api/users/carol` still returns 200, orphaning the link, which the report then shows as `unknown_link_owner`); **the wall-clock time of one report is measured and recorded against the number of keys scanned**, to be compared against the 84 ms / 4,402-entry restore baseline in TASKS.md and to serve as the evidence any future input cap is argued from; and `cd api && uv run pytest`, `cd gui-pages && uv run pytest` (still exactly 64) and `cd redirect && go test ./linkgate/...` all pass.
```

## Critical files

- `api/consistency.py` **(new)**
- `api/tests/test_consistency.py` **(new)**
- `api/tests/test_consistency_scenarios.py` **(new)**
- `api/app.py`
- `gui/admin/backup.html`
- `gui/admin/backup.js`
- `CLAUDE.md`
- `PRODUCT.md`
- `TASKS.md`

Deliberately **not** touched: `spin.toml`, `runtime-config.toml`, `Jenkinsfile`
(test invocation is unchanged), `dev/kv-explorer.toml`, all of `gui-pages/` (no
new page or route), `gui/theme.css`, `gui/app.js`, `gui/admin/users.html`,
`DESIGN.md`, `.impeccable/design.json` (no new pattern, no new token),
`api/backup.py`, `api/links.py`, `api/users.py`, `api/auth.py`, `README.md`, and
all of `redirect/`.

## Verification

Run in this order.

1. **Unit suites, after each task lands:**
   ```bash
   cd api && uv run pytest
   cd gui-pages && uv run pytest
   cd redirect && go test ./linkgate/...
   ```
   Baseline at `c2da04e` is **354 / 64 / ok**. `gui-pages` must still read
   exactly **64** — this plan adds no page and no `.js` file, so any movement
   there means something unplanned was added. Never `go test ./...`.

2. **Confirm the blast radius:**
   ```bash
   git diff --stat main -- redirect/ spin.toml runtime-config.toml Jenkinsfile \
     gui-pages/ gui/theme.css gui/app.js gui/admin/users.html \
     DESIGN.md .impeccable/design.json
   ```
   Must be empty.

3. **Confirm the testability boundary and the credential property:**
   ```bash
   cd api && grep -c spin_sdk consistency.py          # expect 0
   cd api && grep -n "password_hash" consistency.py   # expect no output
   ```

4. **Boot the app:**
   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```

5. **The all-clear, on a fresh store.** Sign in as `admin` at
   `http://localhost:3000/login.html`. On `/admin/users.html` follow the
   "Backup and restore" link. Click **Run consistency check**. Expect the single
   green line "No inconsistencies found — N keys scanned across the links and
   users stores." Then by curl:
   ```bash
   curl -s -b "session=<admin session>" http://localhost:3000/api/admin/consistency | python3 -m json.tool
   ```
   Expect `"ok": true`, `"format": "spin-shortener-consistency-report"`,
   **twelve** entries in `checks`, every one at `"count": 0`,
   `"checks_skipped": 0`, and `"max_findings_per_check": 100`.

6. **The permission gate.** Create a user `alice` with no permissions. Sign in as
   alice and:
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' -b "session=<alice session>" \
     http://localhost:3000/api/admin/consistency        # expect 403
   ```
   And in the browser, `/admin/backup.html` as alice must show
   `#forbidden-notice` with the whole `#admin-content` block — all three
   articles, including the new one — hidden.

7. **Seed and catch, live.** Restart with the KV explorer
   (`./dev/kv-explorer-up.sh`; note local KV is non-persistent, so re-seed
   first). Create `carol`, sign in as carol, create two links, then sign back in
   as admin. Using the explorer's `links` store, produce and then confirm each
   of these one at a time, re-running the report between each and **undoing each
   before seeding the next** so every run reports exactly one thing:

   | Seed | Expected check |
   |---|---|
   | Remove one slug from `all_links` | `unindexed_link` |
   | Add `"ghost1"` to `all_links` | `missing_link_record` |
   | Remove one slug from `owner_links:carol` | `unindexed_owner_link` |
   | Add carol's slug to `owner_links:admin` | `owner_index_mismatch` |
   | Add `"ghost2"` to `owner_links:carol` | `orphan_owner_index_entry` |
   | Set a record's `owner` to `"nobody"` | `unknown_link_owner` |
   | Create `owner_links:nobody` = `["x"]` | `dangling_owner_index` + `orphan_owner_index_entry` |
   | Set `all_links` to `not-json` | `unreadable_value` + checks 1 and 2 **skipped**, `ok: false` |
   | Create a key named `junk` | `unrecognized_key` |

   Checks 8 (`unindexed_user`), 9 (`missing_user_record`) and 10
   (`orphan_session`) **cannot be seeded live** — the KV explorer deliberately
   has no access to the `users` store (`dev/kv-explorer.toml` grants only
   `links`/`analytics`, and that restriction is itself pinned by
   `gui-pages/tests/test_manifest_components.py`). They are covered by the
   scenario tests only. Record that honestly rather than claiming live coverage.

8. **The motivating case, end to end — the point of the whole feature.**
   With carol owning exactly one link `spring-sale`, use the explorer to remove
   `spring-sale` from `owner_links:carol`, leaving the `slug:spring-sale`
   record's `owner` as `carol`. Then:
   - The report shows exactly one `unindexed_owner_link`:
     `{"slug": "spring-sale", "owner": "carol"}`.
   - `DELETE /api/users/carol` as admin returns **200, not 409** — the gate reads
     the index and the index no longer knows about the link. This is the hole,
     reproduced live.
   - Re-run the report: it now shows `unknown_link_owner` for `spring-sale`.
   - `curl -sI http://localhost:3000/r/spring-sale` still returns **302** with
     the original `Location` — the link is orphaned, not broken.
   - Repair it: `/dashboard.html` as admin, owner filter `nobody`/`carol —
     deleted account`, select, bulk Reassign to admin. Re-run the report → clean.

9. **Timing, and record the number.** With the store loaded (create ~200 links
   via the bulk-create panel, four full 50-row batches), time one report:
   ```bash
   curl -s -o /dev/null -w 'total=%{time_total}s\n' \
     -b "session=<admin session>" http://localhost:3000/api/admin/consistency
   ```
   Record the elapsed time **and** the `scanned` key counts from the same run in
   the task's completion note. Compare against `TASKS.md`'s recorded backup
   baseline (a 4,402-entry / 1.78 MB restore at 84 ms, ~19 µs per KV write).
   If this is materially slower per key than that, say so loudly — it would mean
   something is wrong in the read path, and it is the evidence an input cap would
   have to be argued from.

10. **The cap.** Add 120 junk slugs to `all_links` via the explorer. The report's
    `missing_link_record` must show `"count": 120`, `"truncated": true`, exactly
    100 entries in `findings`, top-level `"truncated": true`, and the GUI must
    render "Showing the first 100 of 120."

11. **Concurrency honesty.** Run the report in one tab while creating a link in
    another, repeatedly. If a transient `unindexed_link` or
    `missing_link_record` appears and is gone on the next run, that is the
    documented no-snapshot window behaving as described — confirm the GUI copy
    tells the operator to re-run, and note the observation rather than treating
    it as a bug.

12. **Themes and console.** Repeat steps 5, 7 and 10 with the nav theme control
    on Dark. **Zero console errors and zero CSP violations in both themes** — a
    CSP violation fails a page silently in a browser rather than failing a test,
    which is why this is a manual step.

13. **Re-run all three suites** (step 1) after the docs task, and confirm
    `git diff --numstat TASKS.md` shows only the checkbox lines the builder was
    supposed to tick.

## Out of scope / follow-ups

- **A repair companion.** Trade-off #2. The strongest case for it is
  `unindexed_link`, which has no GUI repair path at all today. Appended to
  Future work; **the trigger is having seen real reports from a real
  deployment** — a repair tool designed before anyone has looked at one is
  designed from imagination, and the two prior plans that touched this shape both
  rejected unbounded server-side mutation whose blast radius is invisible at the
  click.
- **Renaming `gui/admin/backup.html` and retitling the page.** Trade-off #3's
  sub-alternative. Appended to Future work; the trigger is a fourth operator tool
  landing on that page, at which point the file, the `spin.toml` route, the
  `ROUTES` entry, the `test_routing.py` case, the title, the `pageLabel`,
  `users.html`'s anchor and `DESIGN.md`'s quoted anchor all move together as one
  deliberate change.
- **A session sweeper** deleting expired `session:` records. Adjacent, real, and
  deliberately not a consistency check (expired sessions are normal
  accumulation, not drift). Appended to Future work.
- **An input cap and a `?stores=` subset parameter.** Trade-off #7. Appended to
  Future work with verification step 9's measurement as the trigger.
- **Checks over the `analytics` store.** Trade-off #1. Appended to Future work
  with a precise trigger: deletion being changed to purge analytics keys.
- **Any change to `links.remove_slugs_from_indexes` / `move_slugs_between_owners`
  so an emptied `owner_links:` key is deleted rather than written as `[]`.**
  `docs/plans/user-deletion-link-ownership.md`'s trade-off #6 deferred this with
  the stated trigger "a real need to enumerate index keys (the consistency
  endpoint) where empty keys become noise worth removing at the source." This
  plan **is** that endpoint and the answer is no: empty index keys are never
  reported by any check, so they are not noise, and the trigger is not met. The
  existing Future-work entry stands, unchanged, now with a recorded answer.
- **Scheduling the check, or alerting on it.** Requires an outbound host or a
  cron trigger, neither of which this app has. Not raised; not added.
