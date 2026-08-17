# Consistency Repair

## Decisions taken 2026-08-17 — the plan's three open questions are CLOSED

**1. `MAX_REPAIR_WRITES = 100` stands.** The concern was that the ~75 ms/write figure came from
`links.handle_delete`'s inline purge, a differently-shaped handler. It stands anyway, because the
cap almost never binds: **index repair is O(distinct index keys), not O(findings)** — the live
deployment has exactly **two** `owner_links:` keys, so the entire 344-finding incident that
motivated this feature repairs in **2 writes**. The cap binds only in the one O(findings) case,
`orphan_session`, where each session is its own key; 100 × ~75 ms ≈ 7.5 s leaves the same ~4×
margin under the 30 s limit that the inline purge was accepted at. Trace it on a deployed build
before raising it, not before shipping it.

**2. The "only reports" copy is quoted in TWO places, and the second one matters more.**
`gui/admin/backup.html:70` says *"It only reports; it never changes anything."* — but
**`PRODUCT.md:37` also says "It only reports and never repairs anything; the fixes are the
ordinary reassign and delete tools"**, and that sentence becomes actively false when this ships.
Update both. Keep the half that stays true — the *check* still only reads; it is the new,
separately-confirmed action that writes — and do not let PRODUCT.md keep describing reassign and
delete as the only fixes.

**3. Yes, drive the KV explorer to build the hand-crafted drift state** for the
`dangling_owner_index`-would-orphan-a-link case. That is precisely what CLAUDE.md says it is for
("repairing bad local test data is half its value"), it is dev-only and never present in a
deployed manifest, and the state in question (a real `slug:` record listed in `owner_links:phantom`
but absent from `all_links`) cannot be produced through the API at all — which is the same gap
that makes this whole feature necessary.

**Independently verified before accepting the plan: the corrupt-record trap is REAL.** A `slug:`
record whose value fails to parse is added to `unreadable` but **not** to `link_records`
(`api/consistency.py:183-186`), and check 2 then reports every `all_links` slug absent from
`link_records`. So a **present-but-corrupt record is reported as `missing_link_record`**, and a
naive repair would remove the index entry of a record that still exists — making a live link
invisible to the dashboard and causing the next restore to prune it entirely. **That is data loss
caused by a repair tool**, which is the worst thing this feature could do. The plan's
`present_slugs` guard on all three removal repairs is therefore mandatory, not defensive.

## Context

`GET /api/admin/consistency` (`api/consistency.py`, `docs/plans/kv-consistency-check.md`)
reports twelve classes of index drift and **never repairs**: no `?fix=`, no repair
endpoint, no writes at all. That was deliberate, and the plan that shipped it recorded
the sequencing argument verbatim under its rejected alternative #2 — *"a repair tool
designed before anyone has seen a real report is designed from imagination. Ship the
report, look at what real deployments actually contain, then decide."* `TASKS.md`'s
Future-work entry carries the same trigger: **"having seen real reports from a real
deployment, so the tool is designed from observed drift rather than imagined drift."**

**That trigger fired on 2026-08-15, from ordinary operation.** Seeding and bulk-deleting
~1,000 links during the `get_many` spike pushed writes past Akamai's 50/second cap, and
throttled index writes left the store carrying (TASKS.md, "BOTH SPIKES ANSWERED"):

| finding | count | effect |
|---|---|---|
| `unindexed_link` | 20 | live `slug:` records, resolving at `/r/{slug}`, **invisible to the dashboard** — `GET /api/links` reads the index |
| `unindexed_owner_link` | 20 | the same records missing from `owner_links:admin` |
| `missing_link_record` | 152 | `all_links` entries naming slugs whose records are gone |
| `orphan_owner_index_entry` | 152 | the same, in `owner_links:admin` |

The operator's "cleanup complete" check passed while 20 links were still live, precisely
because the dashboard reads the index that had drifted.

**The gap this plan closes: no API path repairs any of it.**

- `DELETE /api/links/{slug}` 404s on a missing record (`api/links.py:378-380`), so a
  dangling index entry cannot be removed.
- `POST /api/links/bulk-action` with `delete` is all-or-nothing and refuses the whole
  batch on the first `not_found`.
- `POST /api/links/bulk` refuses `slug_taken` — and **Akamai's `exists()` reported a
  deleted key as still present**, an eventual-consistency artifact recorded in the same
  TASKS.md section.

The only route available was exporting a backup, hand-editing `all_links` and
`owner_links:admin`, and restoring — done links-store-only on 2026-08-16. That is
expert-only, needs an undocumented request envelope (`{"confirm": "REPLACE", "backup":
{...}}`, which cost two attempts), and a *full* restore would additionally have left the
deployment's second account (`test12`) permanently unable to authenticate, because export
redacts `password_hash`.

**The single number that shapes this whole design:** those 344 findings touch exactly
**two** KV keys — `links:all_links` and `links:owner_links:admin`. Index repair is
O(distinct index keys), not O(findings). The entire observed incident repairs in
**two writes**. That is the fact that makes a repair tool cheap, bounded, and safe here
in a way the analytics purge (genuinely one delete per key) never was.

Confirmed decisions (settled by the requester before planning):

- Repairs are an **explicit, confirmed operator action** — never automatic, never a side
  effect of the read-only check. `GET /api/admin/consistency` stays write-free.
- **Re-detect before repairing; never trust a submitted report.**
  `analyticsorphans.handle_orphan_purge` sets the precedent.
- Chunked and **sequential** writes, like `POST /api/admin/analytics/purge`. Never
  gathered. `api/kvbatch.py`'s helpers stay reads-only; `wasi:keyvalue/batch`'s
  `set_many`/`delete_many` are already rejected repo-wide (TASKS.md, "Considered and
  rejected", 2026-08-15) and must not appear.
- Gate on `users.manage`, matching consistency/backup/purge.
- **`redirect/` is not touched.** Its hot path stays at 6 KV operations.
- Pure logic stays host-testable: zero `spin_sdk` imports; `store` views, `request`,
  `list_keys` and `get_many` arrive as plain parameters.
- The GUI home is `gui/admin/backup.html` ("Store maintenance").

## Key technical facts confirmed during research

- **Baseline suites, run 2026-08-17 before any change:** `api` **586 passed**
  (`cd api && uv run pytest`), `gui-pages` **71 passed`, `redirect` `go test
  ./linkgate/...` **ok**. Every count in Verification below is relative to these.
- **A write costs ~75 ms on Akamai; a read costs ~7 ms — measured in the *same*
  request** (TASKS.md, "Deploy of the inline purge…", 2026-08-15: two traced deletes at
  74.6 ms and 75.1 ms per `delete`, 6.68/7.18 ms per `get`). The repo's older ~23 ms/write
  figure is optimistic by ~3×. `MAX_REPAIR_WRITES` below is sized from 75 ms.
  The same section warns that op-type *ratios* are themselves unstable across windows —
  `list_keys` measured 74–91 ms there against 23.9 ms at a comparable store size six days
  earlier — so treat 75 ms as measured-in-that-window.
- **Akamai limits: 50 KV writes/second app-wide, 1,000 reads/second, 30-second handler
  duration** (CLAUDE.md, "Deployment: Akamai Functions"; fetched from
  `techdocs.akamai.com/akamai-functions/docs/quotas-and-limits` 2026-08-04).
- **`GET /api/admin/consistency` costs 9 KV operations and 156–175 ms on the deployed
  build** since `get_many` landed (TASKS.md, "DEPLOYED AND TRACED (2026-08-16) —
  `2a3a3ca-batch-reads`", 51 keys covered). That is what makes an in-request re-detection
  affordable: the repair's read half costs about one sixth of a single write.
- **`consistency.analyze` truncates `findings` to `MAX_FINDINGS_PER_CHECK = 100` while
  `count` stays exact** (`api/consistency.py:381-389`). A repair driven off the report's
  `findings` list would therefore silently under-repair. This is the concrete reason the
  observed 152 dangling entries "could not even be enumerated from the report", as
  TASKS.md records. Fixed here by giving `analyze` an opt-out parameter, not by raising
  the cap.
- **`consistency.collect` never reads a `user:` record's value** — only its key name
  (`api/consistency.py:212-221`, and `test_collect_never_even_reads_a_user_record_value`
  guards it, asserting `get()` is never called on a `user:` key at all). `user_records`
  is therefore an exact set of existing `user:` keys, which makes every users-side check
  deterministic rather than parse-dependent. The repair must preserve this invariant.
- **`collect` treats a present-but-malformed `slug:` value as `unreadable` and omits it
  from `link_records`** (`api/consistency.py:179-186`). So a corrupt-but-present record
  currently surfaces as `missing_link_record`/`orphan_owner_index_entry`. Repairing those
  naively would strip the index entry of a record that still exists. This is the sharpest
  hazard found during research and drives the new `present_slugs` field below.
- **Spin's KV has no compare-and-swap**, stated repeatedly in CLAUDE.md; and the
  consistency walk has no snapshot ("Do not 'fix' this with locking; there is nothing to
  lock with"). Every index write in this app is therefore already a read-modify-write
  (`links.add_slugs_to_indexes`, `api/links.py:58-71`). The repair follows that shape
  exactly.
- **`spin:key-value/key-value@3.0.0`'s `get-keys()` takes no arguments** — no prefix, no
  cursor — so a namespace enumeration walks every physical key in the `default` store at
  ~68.7 µs/key plus ~one KV round trip (CLAUDE.md, "Parallel KV reads"). The repair
  therefore reuses `collect`'s single pair of enumerations rather than making its own.
- **`obs.route_template` returns `/api/admin/...` paths unchanged**
  (`api/obs.py:90-106` — it rewrites only `/api/links/*` and `/api/users/*`), so the new
  route logs as its literal path with no dynamic segment and **`api/obs.py` needs no
  change**.
- **No new `spin.toml` route, no `gui-pages/routing.py` entry, no new `gui/` file** is
  needed: all GUI work lands in the already-routed `gui/admin/backup.html` and
  `gui/admin/backup.js`. `gui-pages/tests/test_no_inline_code.py`'s file count therefore
  does not move, and `gui-pages/tests/test_manifest_components.py` is unaffected.
- **`Jenkinsfile` is unaffected** — the three test commands it runs are unchanged.
- **UNCONFIRMED: the repair's real wall time on Akamai.** Modelled at ~2 writes ≈ 150 ms
  plus ~175 ms of re-detection for the observed incident, and ~7.5 s at the
  `MAX_REPAIR_WRITES = 100` ceiling. Confirming it needs a deployed build carrying a known
  `log_debug_token` and an `X-SS-Debug` trace of a real repair; filed as follow-up.
- **UNCONFIRMED: whether Akamai's eventual-consistency artifact (a deleted key still
  reported present by `exists()`) also affects `get_keys()`/`get_many`.** If it does, a
  repair pass could re-add a just-deleted slug to `all_links`; the next pass removes it
  again. The design tolerates this (see "Concurrency and the lost-update window"), but it
  has not been measured.

## The per-check verdict

This is the decision the plan exists to make. Eight of the twelve checks have exactly one
outcome derivable from the store; four do not, and are **never** repaired.

| # | check | verdict | repair action | write cost |
|---|---|---|---|---|
| 1 | `unindexed_link` | **repair** | add slug to `all_links` | 1 (all findings share the key) |
| 2 | `missing_link_record` | **repair** | remove slug from `all_links` | 1 (shares the key with #1) |
| 3 | `unindexed_owner_link` | **repair** | add slug to `owner_links:<owner>` | 1 per distinct owner |
| 4 | `owner_index_mismatch` | **never** | — | — |
| 5 | `orphan_owner_index_entry` | **repair** | remove slug from `owner_links:<owner>` | 1 per distinct owner (shares with #3) |
| 6 | `unknown_link_owner` | **never** | — | — |
| 7 | `dangling_owner_index` | **repair, with a precondition** | delete `owner_links:<U>` | 1 per dangling username |
| 8 | `unindexed_user` | **repair** | add username to `_meta:usernames` | 1 (all findings share the key) |
| 9 | `missing_user_record` | **repair** | remove username from `_meta:usernames` | 1 (shares with #8) |
| 10 | `orphan_session` | **repair** | delete each `session:<token>` | 1 per session |
| 11 | `unreadable_value` | **never** | — | — |
| 12 | `unrecognized_key` | **never** | — | — |

`REPAIRABLE_CHECKS` is exactly `CHECKS` order filtered to the eight — which means the
repair order and the report order are the same list, and there is no second ordering
concept to keep in sync. A test pins `set(REPAIRABLE_CHECKS) <= {id for id, _ in CHECKS}`
and that the order matches.

### Why #8 and #9 are repairable (the requester listed them as "unclear")

- **`unindexed_user`** — a `user:<U>` record exists but `_meta:usernames` does not list
  it. The account can already sign in (`auth.resolve_session` reads `user:<U>` directly);
  it is merely invisible to user administration. The only two candidate repairs are "add
  to the index" and "delete the account", and the second is destructive. Adding is
  strictly restorative and costs one write for all findings. **One guard:** the finding is
  derived from a key name, so `user:` with an empty suffix would yield `""`. Skip any
  username that is empty after `.strip()`, mirroring the only rule
  `users.handle_create` applies (`api/users.py:72-73`), and report it as `blocked` with
  reason `invalid_username`.
- **`missing_user_record`** — `_meta:usernames` names a user with no record. This is the
  *safest* of the eight: `collect` derives `user_records` from key names only and never
  parses a user value, so "no record" here means "no key at all" — there is no
  unreadable-value ambiguity of the kind that complicates #2 and #5. Removing the name is
  the only sensible outcome; the alternative is to invent an account.

### Why #4 and #6 are never repaired (agreeing with the requester, with the counter-argument recorded)

- **`owner_index_mismatch`.** There is a real case for "the record wins": `can_edit` is
  `record["owner"] == principal.username` (`api/links.py:178`), so the record is already
  authoritative for permissions, and the only realistic producer of a mismatch is an
  interrupted `links.move_slugs_between_owners` (which adds to the new owner first, then
  removes from each old one, `api/links.py:110-123`) — where "record wins" is exactly the
  completion of the interrupted reassignment. **It still loses.** The repair is a
  *removal* from a user's index, which makes a link disappear from that user's dashboard;
  deciding which of two conflicting owner assertions reflects intent is not derivable from
  the store, and the operator already has `POST /api/links/bulk-action` with `action:
  "reassign"` for exactly this. Decisive on the design's own terms: **the observed drift
  contained zero mismatches**, and designing from observed drift is the whole point of the
  trigger. Filed under Future work with a concrete trigger.
- **`unknown_link_owner`.** The fix is either reassigning to a real user or deleting the
  link — both product decisions with shipped bulk tools, and CLAUDE.md already documents
  the dashboard owner filter plus the `— deleted account` marker as the repair path.

### Why #11 and #12 are never repaired

- **`unreadable_value`.** The only candidate repairs are deleting the key (destroying data
  whose content is by definition unknown) or rewriting it (inventing content). Both are
  wrong. The report already tells the operator this masks other checks and must be fixed
  by hand.
- **`unrecognized_key`.** Deleting an unrecognised key is precisely how a *new key type
  the checker has not been taught about yet* gets destroyed. This repo already holds the
  opposite rule in the analogous place: `analyticsorphans.classify_analytics_keys` routes
  an unrecognised shape to `unrecognized` and therefore never purges it, documented as
  "the safety valve: a future analytics key type must show up as something a human is
  told about, never as something this feature quietly deletes"
  (`api/analyticsorphans.py:80-83`). Same rule, same reason.

Both are surfaced to the operator with shipped copy explaining *why* there is no button,
rather than a silent absence.

## API changes

### `api/consistency.py` — additive only, and it still never writes

Four changes, all additive, all landing before anything else. The module keeps its
"reports; never repairs" property: it gains no `set`/`delete` call, and a new test proves
`handle_consistency` performs zero writes.

1. **`REPAIRABLE_CHECKS: tuple[str, ...]`** — the eight ids above, in `CHECKS` order.
   Lives here, not in the new module, so that `build_report` can publish it without
   `consistency` importing its repairing sibling (which imports `consistency`). A
   constant naming what *something else* may repair is not a repair.
2. **`build_report` gains `"repairable_checks": list(REPAIRABLE_CHECKS)`** in the report
   document, so the GUI never decides for itself which findings have a repair — a
   correctness question the server owns.
3. **`analyze(collected, max_findings: int | None = MAX_FINDINGS_PER_CHECK)`.** `None`
   means no cap: every finding is returned and `truncated` is `False` for every check.
   The default preserves today's behaviour byte for byte, so `handle_consistency` and all
   existing tests are unchanged. **The repair path always calls `analyze(collected,
   max_findings=None)`** — without this it would repair only the first 100 findings per
   check.
4. **`collect` returns two new keys**, both filled by loops that already visit the data:
   - `"present_slugs": set[str]` — every `slug:` key name seen, **readable or not**. This
     is the guard against the unreadable-record hazard: `link_records` excludes a corrupt
     record, so `missing_link_record` and `orphan_owner_index_entry` fire for a slug whose
     key still exists. A removal repair must require absence from `present_slugs`, never
     merely absence from `link_records`.
   - `"sessions_by_username": dict[str, list[str]]` — the `session:` **key names** grouped
     by the username inside them, so `orphan_session` can be repaired without a second
     enumeration. The report itself still never emits a token; only the repair's internals
     see them, and nothing derived from them reaches a response body.

   `"session_usernames"` is retained unchanged so `analyze`'s input contract does not move.

5. **`_parse_str_list` is promoted to `parse_str_list`** (public). The repair re-reads each
   index key immediately before writing it and must parse it with exactly the same
   function the checker used, or the two can disagree about what "readable" means. This
   follows the repo's established convention for a helper a sibling module needs —
   `links._all_slugs` → `links.all_slugs`, `links._can_edit` → `links.can_edit`.

### `api/consistencyrepair.py` (new) — the whole write path

A separate module, not a section of `consistency.py`, so that "the read-only checker
contains no writes" stays a property a reader can verify by opening one file — the same
split `analytics.py`/`analyticsorphans.py` already uses. Zero `spin_sdk` imports;
`store` views, `request`, `list_keys` and `get_many` are plain parameters. Dependency
direction is `consistencyrepair -> consistency -> responses`, no cycle.

Constants:

```python
REPAIR_CONFIRMATION = "REPAIR"
REPAIR_FORMAT = "spin-shortener-consistency-repair"
SCHEMA_VERSION = 1
MAX_REPAIR_WRITES = 100
MAX_BLOCKED_DETAIL = 20      # per blocked entry, e.g. the at-risk slug sample
```

**`MAX_REPAIR_WRITES = 100` is a plain module constant, not a Spin variable** — one
function in one component reads it, the same reasoning `MAX_BULK_ROWS`,
`MAX_BACKUP_ENTRIES` and `MAX_PURGE_KEYS_PER_REQUEST` carry. Arithmetic: 100 × ~75 ms
measured ≈ **7.5 s**, plus ~175 ms of re-detection, against a 30-second handler limit —
roughly a 4× margin, matching the margin the inline analytics purge was judged acceptable
at. It is deliberately well below the purge's 250, because the measured write cost tripled
after that constant was chosen. The response echoes `max_writes_per_request`, so no client
hardcodes it. **Raising it needs real timing evidence from a full-cap repair, not a
hunch** — the same rule `MAX_BULK_ROWS` and `MAX_PURGE_KEYS_PER_REQUEST` carry.

**Progress is guaranteed with no special case, unlike the purge.** `plan_purge` needs an
explicit "at least one slug is always planned" invariant because a single slug can cost
more keys than the whole budget. Here **every repair unit costs exactly one write**, so
any budget ≥ 1 makes progress. Say so in the docstring; it is why this module needs no
equivalent invariant.

#### Pure functions

```python
def apply_list_delta(current: list[str], add: list[str], remove: list[str]) -> list[str]
```
Removals first, then order-preserving appends of anything not already present — byte-for-
byte the shape `links.add_slugs_to_indexes`/`remove_slugs_from_indexes` use
(`api/links.py:58-87`), so a repaired index is indistinguishable from one the normal
authoring path would have written.

```python
def plan_repairs(collected: dict, checks: list[dict], requested: list[str],
                 budget: int) -> dict
```
Pure. `checks` is `analyze(collected, max_findings=None)`'s first return value.
Returns:

```python
{
  "links": {"deltas": {key: {"add": [...], "remove": [...]}}, "deletes": [key, ...]},
  "users": {"deltas": {...}, "deletes": [...]},
  "checks":  [{"check": id, "findings": n, "planned": n, "remaining": n,
               "blocked": n, "skipped": bool, "skip_reason": str | None}],
  "blocked": [{"check": id, ..., "reason": str, "next_step": str | None}],
  "planned_writes": n,
}
```

Planning rules, in order:

1. **A requested check that `analyze` marked `skipped`** (its index key was unreadable —
   `all_links` for #1/#2, `_meta:usernames` for #8/#9) is never repaired. It reports
   `skipped: true, skip_reason: "index_unreadable"`, contributes no writes, and does not
   block completion.
2. **Phase A — deletions that suppress deltas.** `dangling_owner_index` (#7) is planned
   first, because #5 can target the very `owner_links:<U>` key #7 deletes. Any key
   scheduled for deletion has its delta dropped, so the pass never writes a key it is
   about to delete.
3. **The `dangling_owner_index` precondition.** Deleting `owner_links:<U>` destroys the
   last index reference to any slug it names that has a record but is *not* in
   `all_links` — turning a merely-invisible link into an unreachable one. So compute the
   **post-state** of `all_links` (`collected["all_links"]` plus whatever #1 is planning to
   add **in this same pass**) and defer the deletion if any slug in `owner_links:<U>` is
   in `present_slugs` but not in that post-state:

   ```python
   {"check": "dangling_owner_index", "username": U,
    "reason": "would_orphan_unindexed_link", "next_step": "unindexed_link",
    "slug_count": n, "slugs": sorted(at_risk)[:MAX_BLOCKED_DETAIL]}
   ```

   Evaluating against the post-state rather than the collected state is what lets an
   operator repair both checks in one click. If `collected["all_links"] is None`
   (unreadable), **every** #7 deletion is blocked with reason `links_index_unreadable`.
4. **Removal repairs require absence from `present_slugs`.** #2 and #5 skip any slug whose
   `slug:` key still exists, reporting it as blocked with reason `record_unreadable` and
   `next_step: "unreadable_value"`. Without this the repair strips the index entry of a
   live-but-corrupt record — the sharpest hazard in the feature.
5. **`unindexed_user` skips a username that is empty after `.strip()`**, blocked with
   reason `invalid_username`.
6. **Budget.** Iterate checks in `REPAIRABLE_CHECKS` order and findings in the report's
   deterministic sorted order. A finding whose target key already has a planned delta or
   delete costs nothing; otherwise it costs one write if `planned_writes < budget`, and
   otherwise falls into that check's `remaining`. Because `deltas` is keyed, the 344
   findings of the observed incident plan as **two** writes.
7. **Blocked findings are counted separately from `remaining` and never affect
   completion.** This is the loop-termination invariant: a blocked finding can never be
   repaired by re-running, so including it in `remaining` would make the GUI's chunked
   loop spin forever.

#### The applier

```python
async def apply_repairs(stores_by_name: dict[str, object], plan: dict) -> dict
```
Returns `{"keys_written": n, "keys_deleted": n, "write_skipped": [...]}`.

- **Stores in `("links", "users")` order — links first, users last.** The same rule
  `backup.RESTORE_STORE_ORDER` states: users last so a mid-request failure leaves the
  operator's own session material untouched for a retry.
- **Every delta is a fresh read-modify-write**, not a write of the list `collect` read:

  ```python
  raw = await store.get(key)
  parsed = consistency.parse_str_list(raw)
  if raw is not None and parsed is None:
      write_skipped.append({"key": key, "reason": "index_unreadable_at_write"})
      continue
  new = apply_list_delta(parsed or [], delta["add"], delta["remove"])
  if new == parsed:
      continue                      # idempotent: a second pass writes nothing
  await store.set(key, json.dumps(new).encode("utf-8"))
  ```

  The re-read is what bounds the lost-update window (see below), the `parsed is None`
  guard is what stops a value that became corrupt between collect and write from being
  replaced with a delta over `[]` — which would destroy the index — and the `new ==
  parsed` short-circuit is what makes the whole endpoint idempotent.
- **Deletes are sequential, one `await store.delete(key)` at a time.** Never
  `gather_reads`, never `asyncio.gather`, never `delete_many`. Writes are cap-bound at 50/s
  app-wide while reads have 1,000/s of headroom; gathering them would queue against the cap
  rather than overlap, and would compete with live click recording. This is the same rule
  `analyticsorphans.py`'s module docstring calls "the one rule a well-meaning optimisation
  is most likely to break", and it applies here identically.
- **No `user:` key is ever written, read or deleted.** Sessions and `_meta:usernames` are
  the only users-store keys this module touches. A test asserts it.

#### The handler

```python
async def handle_repair(stores_by_name, principal, request, list_keys, get_many) -> Response
```
`POST /api/admin/consistency/repair`.

Request body:

```json
{"confirm": "REPAIR", "checks": ["unindexed_link", "missing_link_record"]}
```

Validation, all-or-nothing before any write (`handle_orphan_purge`'s shape, line for line):

| condition | response |
|---|---|
| no `users.manage` | `403 {"error": "forbidden", "required_permission": "users.manage"}` |
| unparseable body | `400 {"error": "invalid_json"}` |
| `confirm != "REPAIR"` | `400 {"error": "confirmation_required", "expected": "REPAIR"}` |
| `checks` absent/empty/not a list of strings | `400 {"error": "no_checks", "repairable_checks": [...]}` |
| duplicate id | `400 {"error": "duplicate_check", "check": "..."}` |
| id not in `consistency.CHECKS` | `400 {"error": "unknown_check", "check": "...", "repairable_checks": [...]}` |
| id in `CHECKS` but not `REPAIRABLE_CHECKS` | `400 {"error": "check_not_repairable", "check": "...", "repairable_checks": [...]}` |

The last two are distinguished deliberately: "that check has no safe automatic repair" and
"there is no such check" are different operator situations and deserve different copy.

Then: `collect(...)` → `analyze(collected, max_findings=None)` → `plan_repairs(...)` →
`apply_repairs(...)` → response.

**The client submits check ids, never findings.** That is what satisfies "re-detect before
repairing": the report the operator is looking at contributes nothing but the operator's
*intent*. Every fact the repair acts on is read inside the repair request. It also
dissolves the truncation problem outright — the operator can repair all 152 dangling
entries from a report that only ever displayed 100 of them, because the repair re-derives
them itself.

Response `200`:

```json
{
  "ok": true,
  "format": "spin-shortener-consistency-repair",
  "schema_version": 1,
  "repaired_at": "2026-08-17T12:00:00Z",
  "repaired_by": "admin",
  "checks": [
    {"check": "unindexed_link",       "findings": 20,  "repaired": 20,  "remaining": 0, "blocked": 0, "skipped": false},
    {"check": "missing_link_record",  "findings": 152, "repaired": 152, "remaining": 0, "blocked": 0, "skipped": false}
  ],
  "keys_written": 1,
  "keys_deleted": 0,
  "writes": 1,
  "blocked": [],
  "write_skipped": [],
  "complete": true,
  "max_writes_per_request": 100
}
```

- `checks` covers exactly the requested ids, in `REPAIRABLE_CHECKS` order.
- `writes` is the **actual** count from `apply_repairs` (`keys_written + keys_deleted`),
  not the planned count, so the GUI's non-progress detector is honest.
- **`complete = all(c["remaining"] == 0 for c in checks)`** — blocked and skipped entries
  are excluded, by design (rule 7 above).
- The response never contains a session token, a `user:` value, or a link record. A test
  asserts `password_hash` and `pbkdf2_sha256` appear nowhere in the body, mirroring
  `test_handle_consistency_never_leaks_password_hash`.

**There is deliberately no dry-run endpoint and no `GET` companion.** The consistency
report *is* the dry run, and it now carries `repairable_checks`. A second read-only
endpoint would duplicate it and could disagree with it.

### `api/app.py` — one route

Inserted **above** the existing exact-path `/api/admin/consistency` branch (or below it —
they are exact-path matches and cannot shadow each other; place it immediately after for
readability):

```python
if path == "/api/admin/consistency/repair" and method == "POST":
    result = await _require_session(users_store, request)
    if isinstance(result, Response):
        return result
    return await consistencyrepair.handle_repair(
        {"links": links_store, "users": users_store}, result, request, list_keys, get_many,
    )
```

Plus `import consistencyrepair` alongside the existing imports. The `analytics` view is
**not** passed, for the same reason the read-only endpoint does not receive it: orphaned
analytics is normal state with its own shipped tool
(`docs/plans/analytics-orphan-purge.md`). Carry that comment across.

## Concurrency and the lost-update window

The consistency walk has no snapshot and Spin's KV has no compare-and-swap. For a
*report* that means a transient finding, which the page already tells the operator to
confirm with a second run. For a *repair* it would mean a **lost update** — a link created
between the read and the write vanishing from `all_links`. Three things bound it, and the
residual risk is stated rather than hidden:

1. **The delta write re-reads the index immediately before writing it**, so the window is
   one `get` + one `set` (~80 ms measured) rather than the whole collect→write span
   (~250 ms plus the operator's think time, which submitting findings from the client
   would have made unbounded).
2. **A repair never writes a wholesale computed list.** It only ever adds or removes the
   specific members `collect` formed an opinion about. A concurrently-created slug is not
   one of them, so it survives the write untouched.
3. **The residual failure mode is asymmetric and self-correcting.** A lost *addition* (a
   slug created and indexed in the window, then removed because `collect` had judged it
   dangling — impossible under rule 2 above) cannot occur; the only reachable outcome is a
   lost *removal*, or re-adding a slug deleted inside the window, both of which surface as
   ordinary drift on the next consistency run and are repaired by re-running. **The worst
   case is "run it again".**

The GUI copy tells the operator to run repairs when link creation is quiet — the same
posture the analytics-purge article already takes about the shared write budget — but that
advice is a courtesy, not the control. The control is (1)–(3).

## GUI changes

All in two already-routed files. No new file, no new `spin.toml` route, no new
`gui-pages/routing.py` entry, no new design token, and **`DESIGN.md` and
`.impeccable/design.json` are not touched.**

### `gui/admin/backup.html`

The consistency article's copy changes. The sentence
**"It only reports; it never changes anything."** is now false as a description of the
*article* (it stays true of the *check*) and must be replaced — leaving it would be the
worst possible outcome, a page that denies the button next to it. Replacement text, to
sit in the same `<p>`:

> **The check itself only reads.** Where a finding has exactly one safe fix, a Repair
> button appears with it — nothing is changed until you click it. Findings without a
> button need a judgement call; each one says what to do instead.

The existing "run it again if people are actively creating links" sentence stays and gains
"— and prefer a quiet moment for repairs, which share the same write budget as click
recording", matching the analytics article's existing warning.

No new markup is needed beyond that: repair controls are rendered by `backup.js` into the
existing `#consistency-result` container, exactly as the purge button is rendered into
`#orphans-result`.

### `gui/admin/backup.js`

- **`CONSISTENCY_CHECK_LABELS` gains a third field per check, `fix`** — one sentence of
  copy. For a repairable check it describes what the button will do ("Adds these slugs
  back to the all-links index."); for the four non-repairable ones it names the tool to
  use instead ("Reassign or delete these links from the dashboard's owner filter." /
  "Inspect the key with the KV explorer and repair or delete it by hand."). Copy lives
  here, next to the existing labels, following that map's own precedent; the *machine*
  decision of which checks are repairable comes from the server's
  `report.repairable_checks` and is never hardcoded client-side.
- **`renderConsistencyCheck` gains a Repair button** when
  `report.repairable_checks.includes(check.check) && check.count > 0 && !check.skipped`,
  carrying `data-check="<id>"`. **Styling: `class="outline"`, not `outline secondary`.**
  `DESIGN.md`'s Buttons section reserves `.secondary` for the destructive action —
  "de-emphasis, not warning color, is how 'destructive' reads here" — and a repair is
  restorative, not destructive. Stating this so a reviewer reads it as a decision, not an
  oversight.
- **A "Repair all repairable findings" button** above the "Needs attention" group when two
  or more repairable checks have findings, submitting every repairable check id that has a
  non-zero count. Same `outline` styling.
- **`confirmDialog` before any POST, count-bearing**, e.g.
  `Repair 172 findings across 2 checks? This rewrites index keys and can't be undone.`
  with `{ confirmLabel: "Repair" }`.
- **The chunked loop**, modelled on `runOrphanPurge`:

  ```
  do:
    POST /admin/consistency/repair {confirm: "REPAIR", checks}
    if (!ok) -> friendlyError, stop
    accumulate writes; render progress
    if (data.writes === 0 && !data.complete) -> stop with "Repair made no progress."
  while (!data.complete && !stopped)
  then: GET /admin/consistency and re-render
  ```

  A **Stop** button mirrors the purge's. The `writes === 0 && !complete` guard is the
  non-progress detector — cheaper and more precise than an iteration cap, and it cannot
  fire on a healthy pass because a pass with remaining work always plans at least one
  write.
- **The flow ends by re-running `GET /api/admin/consistency` and re-rendering**, which is
  both the operator's confirmation and an independent verification through a different
  code path. The success line reads e.g. `Repaired 172 findings in 2 writes.` above the
  fresh report.
- **`blocked` entries render under the check that produced them**, naming the reason and
  the next step (e.g. "Repair *Links missing from the index* first, then run this again"),
  using the existing `slugChip` and `.finding-field` markup so no new CSS is needed.
- **`PURGE_ERROR_MESSAGES` gets a sibling `REPAIR_ERROR_MESSAGES`** for the new codes
  (`confirmation_required`, `no_checks`, `unknown_check`, `check_not_repairable`,
  `duplicate_check`) — the same call-site-override pattern, and necessary for the same
  reason it was necessary for the purge: `BACKUP_ERROR_MESSAGES.confirmation_required`
  reads "Type REPLACE exactly to confirm", which is wrong here.

## Confirmation posture

| action | server confirmation | GUI |
|---|---|---|
| restore | `{"confirm": "REPLACE"}` | typed field **and** count-bearing dialog |
| analytics purge | `{"confirm": "PURGE"}` | count-bearing dialog only |
| **repair** | **`{"confirm": "REPAIR"}`** | **count-bearing dialog only** |

Repair sits at the purge's bar, one notch below restore, and the ordering is defensible on
blast radius: restore replaces whole stores and signs everyone out; the purge permanently
destroys click history that can never be regenerated; repair rewrites index membership and
deletes only sessions naming accounts that do not exist (re-obtainable by signing in) and
index keys naming nothing. It never touches a link record, a destination, a password hash
or an analytics key.

The server-side `confirm` is kept even though the action is non-destructive, for two
reasons: the endpoint is `curl`-reachable and every mutating admin endpoint in this app
requires one; and repairs draw on the same 50/s write budget as live click recording, so
"I did not mean to run that" has a real cost.

## Data model

**No new KV key type**, and therefore none of the three obligations CLAUDE.md attaches to
one (`backup.py`'s `INDEX_KEYS`/`restore_write_order`, `consistency.py`'s key-shape
recognition, `kvprefix.STORE_PREFIXES`). The repair only mutates keys that already exist
and are already understood by all three: `all_links`, `owner_links:<U>`,
`_meta:usernames`, `session:<token>`. It is stateless — it records nothing about past
repairs, exactly like the analytics purge. State this explicitly in the module docstring
so a future reader does not go looking for a missing `backup.py` change.

## Trade-offs and rejected alternatives

1. **Do nothing — keep backup → hand-edit → restore as the repair path. Rejected.**
   Attractive because it costs no code and the shipped plan deliberately deferred this.
   It loses on evidence: the trigger the deferral itself named has fired, and the manual
   path was exercised on 2026-08-16 and found to be expert-only (a JSON document edited by
   hand), to have an undocumented request envelope that cost two attempts, and — decisively
   — to be *unsafe in its obvious form*: a full restore would have left the deployment's
   second account permanently unable to authenticate, because export redacts
   `password_hash`. The narrow links-only restore that worked required knowing that
   `handle_restore` skips absent stores (`api/backup.py:290-292`). That is not an operator
   procedure.

2. **Per-finding repair — the client submits the findings it wants fixed. Rejected.**
   Attractive because it is maximally granular and reads as "the operator is in control".
   It loses three ways. (a) It re-introduces exactly the stale-report hazard the analytics
   purge was built to avoid, with a *much* longer window — the operator's think time. (b)
   The report truncates at 100 findings per check, so the 152 dangling entries of the real
   incident **could not be submitted at all**; the operator would have to repair in
   waves and could never see the tail. (c) Each finding costs zero marginal writes once
   its index key is scheduled, so per-finding granularity buys the operator nothing they
   would ever want: there is no meaningful "repair 80 of these 152 entries". Per-check is
   the natural unit because each check has exactly one mechanical outcome.

3. **`?fix=true` on `GET /api/admin/consistency`. Rejected outright.**
   Attractive because it is one parameter and no new route. It loses because a GET that
   writes is a category error — it is CSRF-reachable by navigation, cacheable in principle,
   retried by intermediaries, and it destroys the single property that makes a diagnostic
   trustworthy. `POST` to a distinct path also keeps `route_template`'s log line honest
   about which requests mutate.

4. **A separate cheap repair *report* endpoint (`GET /api/admin/consistency/repairable`).
   Rejected.** Attractive by symmetry with `analyticsorphans`' report/purge pair. It loses
   because the pair exists there for a reason that does not hold here: the orphan report is
   *cheap* (2 KV ops) precisely so it can be offered as a plain button, while the
   consistency report is the same walk the repair needs anyway (9 ops, 156–175 ms). A
   second endpoint would duplicate it and could disagree with it. Publishing
   `repairable_checks` inside the existing report is the whole of what was needed.

5. **Repairing `owner_index_mismatch` by "the record wins". Rejected, argued at length
   above.** The decisive point is empirical: the observed drift contained zero mismatches,
   and this tool's mandate is to be designed from observed drift. Filed under Future work
   with a trigger.

6. **Repairing `unreadable_value`/`unrecognized_key` by deleting the key. Rejected.**
   Attractive because it is the only way to get a store to `ok: true` when one of these
   fires. It loses because deleting an unrecognised key is precisely how a new key type
   the checker has not learned about yet gets destroyed — `classify_analytics_keys`
   already holds the opposite rule for the same reason — and because an unreadable value's
   content is by definition unknown, so "delete" and "rewrite" are both guesses.

7. **A blanket refusal to repair anything while any `unreadable_value` finding exists.
   Rejected in favour of precise per-finding guards.** Attractive because it is one
   condition instead of three (`present_slugs` checks in #2, #5 and #7). It loses because
   a single corrupt key in an unrelated namespace would block every repair on the store,
   which is exactly the shape of tool operators stop using. The precise guards additionally
   *tell the operator which slug* is blocking, via `blocked` entries.

8. **A fifth `<article>` on the maintenance page for repair. Rejected.** Attractive
   because it matches the page's existing structure and the requester suggested it. It
   loses because a repair is meaningless without the report it repairs: a standalone
   article would hold a button with no findings next to it, and the operator would have to
   scroll between two articles to connect a finding to its fix. The analytics purge sets
   the precedent in the right direction — its button renders *inside* `#orphans-result`,
   after the report, not as its own article.

9. **`wasi:keyvalue/batch`'s `delete_many` for the `orphan_session` deletes. Not
   reconsidered.** Already rejected repo-wide on 2026-08-15 (TASKS.md, "Considered and
   rejected") on the grounds that the WIT itself disclaims atomicity and ordering while
   every write path in this app depends on a stated ordering, and that batched writes
   either still consume K against the 50/s cap or let one handler spend the whole
   service's write budget. Noted here only so its absence is not read as an oversight.

10. **Raising `MAX_FINDINGS_PER_CHECK` instead of giving `analyze` a `max_findings=None`
    opt-out. Rejected.** Attractive as a one-line change. It loses because the cap exists
    to keep the *report* readable and its response bounded ("a capped list must never read
    as complete"), and the repair's need is unbounded by nature — 152 today, arbitrarily
    many after a worse incident. An opt-out parameter serves both without moving the
    report's behaviour at all.

## Tasks

The exact lines appended to `TASKS.md` under `## Consistency repair`:

```
- [ ] Additive consistency.py changes for repair (must land first) — file(s): api/consistency.py, api/tests/test_consistency.py — done when: `REPAIRABLE_CHECKS` names exactly unindexed_link, missing_link_record, unindexed_owner_link, orphan_owner_index_entry, dangling_owner_index, unindexed_user, missing_user_record, orphan_session in CHECKS order, `build_report` emits `repairable_checks`, `analyze(collected, max_findings=None)` returns every finding with `truncated` false for every check while the default argument leaves today's report byte-identical, `collect` additionally returns `present_slugs` (every `slug:` key name including unreadable ones) and `sessions_by_username` (session KEY names grouped by the username inside them) with `session_usernames` unchanged, `_parse_str_list` is renamed to the public `parse_str_list`, a test pins `set(REPAIRABLE_CHECKS)` as a subset of the CHECKS ids in the same relative order, and `cd api && uv run pytest` passes with every pre-existing consistency test unmodified
- [ ] Pin that GET /api/admin/consistency performs zero writes — file(s): api/tests/fakes.py, api/tests/test_consistency.py — done when: `fakes.py` gains a store variant whose `set`/`delete` raise, `handle_consistency` is exercised against it over a store seeded with findings from at least four different checks, the test passes, and it is verified to FAIL if a single `await store.set(...)` is temporarily added to `consistency.collect`
- [ ] Pure repair planner in api/consistencyrepair.py (depends on the consistency.py task) — file(s): api/consistencyrepair.py, api/tests/test_consistency_repair.py — done when: `apply_list_delta` removes before appending and preserves order, `plan_repairs` produces exactly ONE planned write for a store carrying 20 unindexed_link plus 152 missing_link_record findings and exactly TWO when 20 unindexed_owner_link plus 152 orphan_owner_index_entry for one owner are added, a slug whose `slug:` key exists but whose value is unparseable is never planned for removal from `all_links` or an owner index (reported blocked with reason `record_unreadable`), a `dangling_owner_index` deletion is blocked with reason `would_orphan_unindexed_link` when the key names a slug present in `present_slugs` but absent from the POST-STATE of `all_links` and is NOT blocked when unindexed_link is repaired in the same pass, all dangling deletions are blocked with reason `links_index_unreadable` when `all_links` is unreadable, a key scheduled for deletion never also carries a delta, a check `analyze` marked skipped is never planned and never blocks completion, blocked findings are counted separately from `remaining`, the budget is respected and two runs over identical input produce byte-identical plans, and the module has zero `spin_sdk` imports
- [ ] Sequential applier and POST /api/admin/consistency/repair — file(s): api/consistencyrepair.py, api/app.py, api/tests/test_consistency_repair.py — done when: `apply_repairs` writes the links store before the users store, re-reads each index key immediately before writing it, skips (and reports in `write_skipped`) a key whose value became unparseable since collection, writes nothing when the delta is a no-op, deletes strictly sequentially with no `gather_reads`/`asyncio.gather`/`delete_many` anywhere in the file, and never reads, writes or deletes a `user:` key; `handle_repair` returns 403 without `users.manage`, 400 for invalid_json / confirmation_required / no_checks / duplicate_check / unknown_check / check_not_repairable (the last two distinguished), 200 with `checks`/`writes`/`keys_written`/`keys_deleted`/`blocked`/`complete`/`max_writes_per_request` where `complete` ignores blocked and skipped entries, a second identical request writes nothing and still returns `complete: true`, no response body ever contains `password_hash` or `pbkdf2_sha256`, `MAX_REPAIR_WRITES` is 100 as a plain module constant echoed as `max_writes_per_request`, and `curl -X POST localhost:3000/api/admin/consistency/repair` without a body returns 400
- [ ] Regression test reproducing the 2026-08-15 throttled-write incident — file(s): api/tests/test_consistency_scenarios.py — done when: a test seeds a links store with 20 unindexed_link + 20 unindexed_owner_link + 152 missing_link_record + 152 orphan_owner_index_entry findings, asserts `handle_consistency` reports those exact counts with `ok: false`, runs `handle_repair` for all four checks, asserts the response reports exactly 2 writes and `complete: true`, and asserts a fresh `handle_consistency` then returns `ok: true` with all twelve checks at count 0
- [ ] Repair affordances on the Store maintenance page — file(s): gui/admin/backup.html, gui/admin/backup.js — done when: the consistency article no longer claims "It only reports; it never changes anything" and instead says the check only reads while a Repair button appears where a finding has one safe fix, `CONSISTENCY_CHECK_LABELS` carries a `fix` sentence for all twelve checks naming the manual tool for the four non-repairable ones, a per-check Repair button (`class="outline"`, never `outline secondary`) renders only for ids present in the server's `report.repairable_checks` with a non-zero non-skipped count, a "Repair all repairable findings" button appears when two or more qualify, every repair is preceded by a count-bearing `confirmDialog` and no typed field, the chunked loop re-POSTs while `complete` is false with a Stop button and halts with "Repair made no progress." if a pass returns `writes: 0` with `complete: false`, blocked entries render under their check naming the reason and next step, and the flow finishes by re-running GET /api/admin/consistency and rendering the fresh report
- [ ] Document the repair companion (depends on every task above) — file(s): CLAUDE.md, PRODUCT.md — done when: CLAUDE.md's "KV consistency check" section states that the check itself is still write-free and points at a new peer section "Consistency repair" recording the endpoint, the `users.manage` gate, the eight repairable checks and the four that are never repaired WITH their reasons, that the client submits check ids and never findings so the repair re-detects in-request, that index repair is O(distinct index keys) so the observed 344-finding incident repairs in two writes, the `MAX_REPAIR_WRITES = 100` cap with its ~75 ms/write arithmetic and the raise-only-with-evidence rule, that deletes are sequential and never batched, that no new KV key type is introduced so none of the three new-key-type obligations apply, and the lost-update window with why "run it again" is the worst case; PRODUCT.md's line claiming the check "only reports and never repairs anything" is corrected; DESIGN.md and .impeccable/design.json are NOT touched and `git diff DESIGN.md .impeccable/design.json` is empty
- [ ] End-to-end manual verification of consistency repair — file(s): (none — verification step) — done when: against `./dev/kv-explorer-up.sh`, hand-edited KV keys have seeded at least unindexed_link, missing_link_record, unindexed_owner_link, orphan_owner_index_entry, dangling_owner_index, unindexed_user, missing_user_record and orphan_session; the maintenance page shows Repair buttons on exactly those and none on owner_index_mismatch/unknown_link_owner/unreadable_value/unrecognized_key; "Repair all repairable findings" drives the store to `ok: true` in one click; a `dangling_owner_index` seeded so that it would orphan an unindexed link is reported blocked rather than deleted and clears once unindexed_link is repaired; a `slug:` key with a deliberately corrupt value is never removed from `all_links`; the repaired links still resolve at `/r/{slug}` and now appear in the dashboard; re-running the repair reports 0 writes and `complete: true`; and `git diff redirect/` is empty
```

## Critical files

- `docs/plans/consistency-repair.md` (new)
- `api/consistency.py`
- `api/consistencyrepair.py` (new)
- `api/app.py`
- `api/tests/test_consistency_repair.py` (new)
- `api/tests/test_consistency.py`
- `api/tests/test_consistency_scenarios.py`
- `api/tests/fakes.py`
- `gui/admin/backup.html`
- `gui/admin/backup.js`
- `CLAUDE.md`
- `PRODUCT.md`
- `TASKS.md`

Deliberately unchanged: `redirect/` (all of it), `spin.toml`, `gui-pages/`, `Jenkinsfile`,
`DESIGN.md`, `.impeccable/design.json`, `api/obs.py`, `api/kvbatch.py`, `api/backup.py`,
`api/kvprefix.py`.

## Verification

1. `cd api && uv run pytest` — expect **586 + the new tests**, with every pre-existing
   consistency test unmodified. If any existing test needed editing, the `consistency.py`
   changes were not additive and the task is not done.
2. `cd gui-pages && uv run pytest` — expect **71**, unchanged. A change here means a new
   `gui/` file or route crept in.
3. `cd redirect && go test ./linkgate/...` — expect `ok`. Also `git diff redirect/` must
   be empty. (Never `go test ./...`, `go build ./...` or `go vet ./...` — they fail by
   design on `package main`.)
4. Confirm the zero-write guard actually guards: temporarily add one
   `await links_store.set("probe", b"1")` to `consistency.collect`, re-run
   `cd api && uv run pytest -k zero_write`, see it FAIL, revert.
5. Confirm the module holds its own rules by inspection:
   `grep -c "gather\|delete_many\|set_many" api/consistencyrepair.py` → `0`;
   `grep -c "spin_sdk" api/consistencyrepair.py` → `0`;
   `grep -n "user:" api/consistencyrepair.py` → no `set`/`delete`/`get` against one.
6. Live run with the KV explorer, which is how the drift gets seeded:

   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> \
   SPIN_VARIABLE_KV_EXPLORER_PASSWORD=<explorer-pw> \
   SPIN_VARIABLE_COOKIE_SECURE=false \
     ./dev/kv-explorer-up.sh
   ```

   Sign in at `http://localhost:3000/login.html` as `admin` (use the form — a raw `fetch`
   login yields `csrf_mismatch` 403s). Create three links (`keepone`, `keeptwo`,
   `keepthree`) and one extra user (`bob`). Then, in the explorer at
   `http://localhost:3000/internal/kv-explorer/` (username `kv`), **note every key is
   physically prefixed**:

   | seed | edit |
   |---|---|
   | `unindexed_link` (+ `unindexed_owner_link`) | remove `keepone` from `links:all_links` **and** from `links:owner_links:admin` |
   | `missing_link_record` + `orphan_owner_index_entry` | add `"zzghost"` to both `links:all_links` and `links:owner_links:admin` |
   | `dangling_owner_index` | set `links:owner_links:phantom` to `["zzghost"]` |
   | `unindexed_user` | remove `bob` from `users:_meta:usernames` (leave `users:user:bob`) |
   | `missing_user_record` | add `"nosuchuser"` to `users:_meta:usernames` |
   | `orphan_session` | set `users:session:faketoken` to `{"username": "phantom", "csrf_token": "x", "provider": "local"}` |
   | `unreadable_value` (must survive repair) | set `links:slug:keeptwo` to `not-json`, and add `keeptwo` is already in `all_links` |

7. On `http://localhost:3000/admin/backup.html`, click **Run consistency check**. Confirm:
   Repair buttons appear on the eight repairable checks with findings and on none of
   `owner_index_mismatch` / `unknown_link_owner` / `unreadable_value` /
   `unrecognized_key`, each of which instead shows its `fix` sentence.
8. Confirm the two safety behaviours **before** repairing everything:
   - `dangling_owner_index` for `phantom` reports **blocked** (it names `zzghost`, which
     has no record, so it should in fact *not* block — to see the block, first re-add a
     real record: set `links:slug:zzreal` to a valid record and put `zzreal` in
     `links:owner_links:phantom` but **not** in `links:all_links`). Repair
     `dangling_owner_index` alone → the entry is reported blocked with next step
     `unindexed_link`, and `links:owner_links:phantom` still exists.
   - Repair `missing_link_record` alone → `keeptwo` is **still** in `links:all_links`
     (its record exists but is unreadable), reported blocked with reason
     `record_unreadable`.
9. Repair `keeptwo`'s value by hand (restore valid JSON), then click **Repair all
   repairable findings**, accept the count-bearing dialog, and confirm: the progress line
   reports a small number of writes, the page re-runs the check automatically, and the
   report reads `ok: true` with all twelve checks at 0.
10. Confirm the outcome in the product, not just the report: `keepone` now appears in the
    dashboard, and `curl -sI localhost:3000/r/keepone` still returns `302`.
11. Idempotence: click **Repair all repairable findings** again from a clean report — no
    button should be offered (no findings). Then drive it by hand:
    `curl -X POST localhost:3000/api/admin/consistency/repair` with a valid session,
    `{"confirm":"REPAIR","checks":["unindexed_link"]}` → `200` with `writes: 0`,
    `complete: true`.
12. Negative cases by `curl` with a valid admin session: missing `confirm` → `400
    confirmation_required`; `{"checks":["owner_index_mismatch"]}` → `400
    check_not_repairable`; `{"checks":["nope"]}` → `400 unknown_check`;
    `{"checks":["unindexed_link","unindexed_link"]}` → `400 duplicate_check`. As a user
    without `users.manage` → `403 forbidden`.
13. Confirm the read-only endpoint is still read-only in the live app: run
    `GET /api/admin/consistency` twice against the seeded store and confirm the KV
    explorer shows no key modified between them.

## Out of scope / follow-ups

Filed under `TASKS.md`'s `## Future work (not scheduled)`:

- **Repairing `owner_index_mismatch` by "the record wins."** Trigger: seeing a real
  mismatch in a real report — most plausibly from an interrupted
  `bulk-action`/`reassign`, which is the only code path that can produce one. The
  argument for it is written out above so it does not have to be re-derived.
- **Repairing `unknown_link_owner` by reassigning to an operator-chosen owner.** This
  would be a repair that takes a *parameter*, which is a different shape from everything
  here (every repair in this plan is derivable from the store alone). Trigger: operators
  reporting that the dashboard owner-filter path is too slow at scale.
- **Re-measure the repair on a deployed build with `X-SS-Debug`** and record the real
  per-pass wall time, so `MAX_REPAIR_WRITES` rests on a measurement rather than the
  2026-08-15 write figure applied to a different handler shape. Blocked on a deploy
  carrying a known `log_debug_token`.
- **Raising `MAX_REPAIR_WRITES` above 100** — only with timing evidence from a full-cap
  repair, per the standing rule.

Deliberately not addressed here:

- **Preventing the drift.** The incident's root cause was throttled index writes past the
  50/s cap. Making the write path resilient to throttling (retry, backpressure, or a
  smaller bulk cap) is a separate and much larger question, and this tool exists precisely
  because no write path can be made perfectly interruption-proof without atomicity the
  host does not offer.
- **The `analytics` namespace.** Still never scanned by the check and never touched by the
  repair; orphaned analytics remains normal state with its own shipped tool
  (`docs/plans/analytics-orphan-purge.md`).
- **`GET /api/links` pagination**, still the standing answer if link counts get large.
</content>
</invoke>
