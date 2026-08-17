# Derived Link Indexes

*(Filed under the solution rather than the symptom. The measured defect it fixes
is the concurrent index lost update of 2026-08-17; if you came looking for
`index-lost-update.md`, this is it.)*

## Context

`links:all_links` and `links:owner_links:<owner>` are read-modify-write over a
single shared JSON array, and Spin's KV has no compare-and-swap. Concurrent
authoring requests each read the index, add their own slugs, and write back —
last writer wins, and everyone else's additions are silently lost. The link
*records* all land; only the index entries vanish. The result is links that
resolve at `/r/{slug}` and are invisible in the dashboard.

Measured directly on the deployed build `751f368-throttle-resilience`,
2026-08-17 (TASKS.md, `### DEPLOYED AND TRACED (2026-08-17)`):

- **6 concurrent 50-row bulk creates** → 37 `unindexed_link` + 28
  `unindexed_owner_link`, with `index_updated: true` on all six responses.
  Exactly 13 links became visible, matching one request's `count=13` to the
  link — the last-writer-wins signature.
- **The control run, which is the one that matters:** 4 concurrent 5-row creates
  (~28 writes, far under Akamai's 50 writes/second cap) returned four clean
  `201`s with **zero retries and zero write failures** — their log lines carry
  no `write_retry`/`write_failed` field at all, and the renderer omits
  zero-count op types, so absence is proof — and still produced 5
  `unindexed_link`. **This is independent of write throttling.**
- **2 concurrent 3-row creates** (~10 writes, 0.7 s) lost one request's three
  additions outright. The threshold is "any two overlapping bulk creates", not
  "a busy store".

Reproduce with `dev/bulk-concurrent.sh <requests> <rows> <prefix>`; read its
header, which explains both regimes.

The just-shipped write-throttle resilience work (`api/kvretry.py`, CLAUDE.md's
"Write-throttle resilience") does not address this and **no retry constant can**
— the control run proves nothing was failing. The motivating Future-work entry
is TASKS.md's "Concurrent bulk creates lose index updates, and retry cannot fix
it", which carries four uncosted options; this plan costs them.

**Confirmed decisions (settled by the user before planning):**

- `redirect` is untouched. It never reads either index; `/r/{slug}` is a direct
  `slug:` read.
- **No batched or gathered writes, ever.** `wasi:keyvalue/batch`'s
  `set_many`/`delete_many` stay rejected repo-wide. Reads may use
  `scoped_get_many`/`gather_reads`.
- Do not propose retry.
- Any new KV key type carries three obligations (`backup.py`'s
  `INDEX_KEYS`/`restore_write_order`, `consistency.py`'s key-shape recognition,
  a prefix in `kvprefix.STORE_PREFIXES`). **This plan adds no new key type.**
- "Accept the race" is a legitimate outcome if the costing supports it. It does
  not; the reasoning is in Trade-offs #1.
- Whatever ships must keep `GET /api/admin/consistency` meaningful. It does —
  see "Stage 2".

## Key technical facts confirmed during research

**About the code as it stands today (all read this session, at the line numbers
given):**

- `api/links.py:309` `handle_list` reads `all_slugs(store)` (`:54`) or
  `owned_slugs(store, username)` (`:43`) **purely to obtain a slug list**, then
  `scoped_get_many`s the records and, at `:344-350`, **skips any slug whose
  record is missing or unreadable**. Nothing in the app treats either index as
  truth: `handle_create` gates a custom slug on `store.exists(f"slug:{slug}")`
  (`:243`), `allocate_random_slug` probes the record key (`:37`), and
  `bulk.handle_bulk_create` explicitly re-confirms every submitted slug with a
  `get_many` over `slug:` keys, with a comment saying `all_links` "is an index,
  not the truth" (`api/bulk.py:217-235`).
- `links.can_view(principal, record)` (`api/links.py:197`) already expresses
  exactly the rule a derived listing needs: owner, or `links.view_all`, or
  `links.edit_all`. It is reused rather than re-branched.
- The three read-modify-write sites are `add_slugs_to_indexes` (`:59`),
  `remove_slugs_from_indexes` (`:83`) and `move_slugs_between_owners` (`:100`).
  Each does one `get` then one `set` per index key, so **the race window is one
  KV round trip (~25-75 ms on Akamai), not the request duration.**
- A single link create costs **3 KV writes** (record, `all_links`,
  `owner_links:<owner>`); a 50-row bulk create costs **52**; a bulk delete of 50
  costs 52; a 50-slug reassign costs 50 + one per distinct owner + 1.
- `users.handle_delete` (`api/users.py:186`) reads `links.owned_slugs` for its
  `409 user_owns_links` gate and **already receives `list_keys` as a parameter**
  (`api/app.py:381`) for session purging.
- `analytics.handle_click_totals` (`api/analytics.py:134-137`) reads the same
  two index keys purely to build a `visible` set.
- `api/tests/fakes.py` already supplies `fake_list_keys(store)` (returns
  `store.keys()`) and `fake_get_many(store, keys)` (returns
  `dict[str, bytes | None]` for every requested key), so every handler test
  below can be written with the existing fakes and no new ones.
- Baseline: `cd api && uv run pytest` → **668 passed** (run this session).
  22 tests in `test_links.py`, 1 in `test_bulk.py` and 1 in `test_users.py`
  call `add_slugs_to_indexes` to seed fixtures.
- `gui/dashboard.js:94` sets `sortKey = null` by default and only sorts when a
  header is clicked (`:327-336`), so **the order the server returns is the order
  the dashboard renders.** Today that is `all_links` append order, i.e.
  approximately creation order, oldest first.
- `gui/dashboard.js` branches on `data.index_updated === false` at `:797`,
  `:858`, `:916` and `:1079`; `gui/admin/backup.js:275` renders a `next_step`.
  `gui/admin/backup.js:182-215` hardcodes copy for the six index-drift checks.

**About the platform (all previously measured; cited, not re-derived):**

- **`list_keys` ≈ one KV operation + ~68.7 µs per physical key in the WHOLE
  `default` store**, floor ~24 ms measured at a 57-key store (CLAUDE.md,
  "Parallel KV reads"; TASKS.md 2026-08-10/11). A single `get` is ~20-26 ms.
- **A prefix-scoped enumeration does not exist.** `get-keys()` takes no
  arguments — no prefix, no cursor (CLAUDE.md, confirmed against both SDKs). So
  `scoped_list_keys` filters a whole-physical-store walk after the fact, and any
  design must *avoid* the enumeration rather than narrow it.
- **`get_many(K≤1000)` is ~one host round trip regardless of K (~10 ms at
  K=1,000) and does NOT bill as K reads** against the 1,000 RPS cap (measured on
  Akamai 2026-08-15/16; TASKS.md "BOTH SPIKES ANSWERED"). `api/kvbatch.py`
  chunks at `MAX_KEYS_PER_GET_MANY = 1000`.
- **Caching the physical key enumeration per request was rejected on 2026-08-04
  for one specific reason: `backup.handle_restore` calls `list_keys` *after*
  writing, to find pre-existing keys to prune** (TASKS.md, "Considered and
  rejected"). Any cache introduced here must therefore be scoped to specific
  handlers and must never reach restore. That constraint is honoured below.
- **Akamai KV shows at least one eventual-consistency artifact: a deleted key's
  `exists()` still reported it present** (TASKS.md 2026-08-16;
  `docs/plans/consistency-repair.md:70`). The click-counter loss curve
  (TASKS.md:406) independently suggests "a stale read across instances… with a
  propagation window on the order of 100 ms". This is why every read-back /
  verify-and-retry mitigation is rejected below.
- The deferred pagination design (`docs/plans/links-pagination.md:73`,
  `:282-288`, `:324`) **slices `all_links`/`owner_links:` before the record
  fetch and depends on their append order.** Deriving the list forecloses that
  specific design — see Trade-offs #6.

**UNCONFIRMED, and both are blocking (tasks 1 and 2):**

- **M1 — does `get_keys` see a `slug:` record written milliseconds earlier?**
  Everything below assumes read-your-own-write freshness for the enumeration.
  If it lags, a derived list makes a just-created link *transiently invisible*,
  which is a worse symptom than the permanent-but-rare one being fixed. Weak
  supporting evidence: across many concurrent runs the consistency check has
  reported `unindexed_link` (records present, index missing) and never a
  spurious `missing_link_record` naming a just-created slug — but that has never
  been probed deliberately. **Confirmable in minutes with shipped endpoints; see
  Verification step 1.**
- **M2 — does `get_keys` still return a `slug:` key deleted milliseconds
  earlier?** If it does, a derived list would name a deleted slug — which
  `handle_list` already skips, *provided* the record `get` is fresh enough to
  return `None`. Bounded and transient either way, but measure it.

## Recommendation

**Derive both link indexes from the key enumeration, in two stages, gated on M1.**

Stage 1 changes only the *read* paths and is ~4 call sites; it makes the bug
user-invisible immediately and is trivially revertible. Stage 2 stops writing
the indexes and deletes the six consistency checks and five repairs that exist
only to describe their drift.

The reasoning, in order of weight:

1. **Nothing treats the index as truth already.** `handle_list` tolerates every
   drift direction, `handle_create`/`allocate_random_slug`/`handle_bulk_create`
   all gate on the record key. The index is a *cached slug list*, and it is the
   only shared mutable key on the authoring hot path. Deriving it removes the
   race by removing the shared key, rather than making the shared key less
   likely to be clobbered.
2. **The cost is measured and, today, is zero.** Within `GET /api/links` the
   change swaps one `get` (~25 ms) for one `list_keys` (~24 ms at the deployed
   store's 57 keys). The delta is `0.0687 × physical_keys − 1` ms.
3. **It removes 2 of 3 writes from every single-link authoring request**, which
   is the other measured incident class: both throttle incidents this month
   (2026-08-15, 2026-08-17) were bulk-create driven, and index writes are the
   ones whose loss actually causes drift.
4. **It deletes machinery rather than adding it** — three index helpers, six of
   twelve consistency checks, five of eight repairs (including
   `consistencyrepair.py`'s two most intricate safety preconditions), and the
   "records first, indexes last" rule, which is replaced by "there is no index;
   a record's existence is the only truth". A partially-completed bulk create
   then leaves exactly the records that landed, all of them visible.
5. **No new KV key type**, so none of the three obligations fire.

**What it costs, stated plainly.** `GET /api/links` gains a whole-store key
enumeration whose cost grows at ~68.7 µs per physical key without bound, on the
app's core authenticated endpoint, coupled to *click volume* (analytics keys are
up to 94 per link and dominate the store) rather than to link count. That is the
same axis already identified as the app's #1 scaling problem. The accounting per
dashboard load — `GET /api/links` followed by `GET /api/analytics/click-totals`:

| | walks per initial load | walks per mutation-driven reload |
|---|---|---|
| today | 1 (click-totals) | 1 (click-totals) |
| after Stage 1 | 2 | 2 |
| after Stage 1 + the queued click-totals decoupling | 2 | **1 — back to today** |

The two queued items genuinely push in opposite directions and they **cancel on
mutation-driven reloads**: decoupling removes click-totals' walk from exactly
the reloads where a derived `/api/links` adds one. The net standing cost is
**one extra whole-store walk on the initial dashboard load.** A per-request
memoisation (task 5) is what keeps `click-totals` at one walk rather than two
once its `visible` set is also derived.

Absolute numbers, using the measured model:

| store shape | physical keys | one walk | delta on `GET /api/links` |
|---|---|---|---|
| today (14 links, 2 clicked) | 57 | 24 ms | **−1 ms** |
| 100 links @ 50 clicks | ~6,000 | 436 ms | +411 ms |
| 1,000 links @ 10 clicks | ~19,000 | 1,330 ms | +1,305 ms |
| 500 links @ 50 clicks | ~30,200 | 2,100 ms | +2,075 ms |

At the row where this starts to hurt, `click-totals` on the same page is already
~3,500 reads (~3.5 s at the inferred 1,000-reads/second single-handler ceiling),
so the added walk is ~11% of an already-broken page. **Revisit trigger: a traced
`list_keys` inside `GET /api/links` above ~250 ms** (≈3,300 physical keys). The
levers then, in order: the queued "drop the `events:` ring-buffer write" (−32%
of analytics keys, hence −32% of every walk in the app), the shipped orphan
purge, and only then a different index shape.

**If M1 fails**, stop and re-plan toward the fallback: keep the indexes, accept
the race, and add a narrow client-driven reindex (Trade-offs #2 describes it and
why it is second choice). Do not build the fallback speculatively.

## Decisions taken 2026-08-17 (supersede the plan body where they differ)

**M1 is RESOLVED and it PASSED — `get_keys` is read-your-own-write fresh for
creates**, so the Stage 1 recommendation stands and the Trade-offs #2 fallback
is not being built. Measured on `751f368-throttle-resilience`: 14 cycles across
two probe shapes (one 5-row bulk create sampled six times from +0.11 s to
+1.88 s; eight single-link creates each checked at +0.31–0.38 s, the tighter
test), zero `missing_link_record` findings naming a just-created slug in any of
them. Probe sensitivity was validated against 37 real findings rather than
inferred from a clean negative. Full record, including a false start worth
knowing about, is on the M1 task line in TASKS.md.

**M2 (staleness after a DELETE) remains open and is informational, not
blocking.** Note a prior signal already in the repo: CLAUDE.md's "Consistency
repair" section cites "Akamai's eventually-consistent `exists()`" during the
2026-08-15 incident, which is a stale *positive* after deletion and therefore
bears on M2, not M1. The plan's claim that a stale enumeration entry is harmless
(`handle_list` skips a slug whose record `get` returns `None`) is what M2 must
confirm or correct.

**Stage 2 is GATED on Stage 1's deployed trace, not shipped alongside it.**
The decisive reason is a verification one rather than a caution: while the
indexes are still being written, `GET /api/admin/consistency` keeps reporting
`unindexed_link` after a concurrent bulk create — so a run in which every
created link is present in `GET /api/links` *while the check still flags drift*
is direct, positive proof that the derived reads work. Stage 2 deletes exactly
that signal. Ship Stage 1, deploy, trace, then decide.

**Bulk-create submission order: ACCEPTED as slug-ascending within a batch.** Do
NOT widen `created_at` to millisecond resolution to preserve paste order. All
rows of one bulk create share a single second-resolution `created_at`
(`api/bulk.py:272`), so they tie and sort by slug; ordering across batches and
for single links is unchanged (oldest first). Preserving paste order would mean
per-row `to_iso8601_utc_ms` stamps, which widens the stored timestamp format for
new link records and obliges both the sort and `parse_iso8601_utc` to tolerate
two resolutions — rejected as not worth it for intra-batch ordering.

## Stage 1 — derive the reads (`api/`)

Python, in `api/` — the language rule is unchanged: none of this is on the
redirect hot path, and every module touched is already host-testable Python.
All new logic takes `store` / `list_keys` / `get_many` as plain parameters and
imports nothing from `spin_sdk`.

### `api/links.py`

Add, next to the existing `ALL_SLUGS_INDEX_KEY`:

```python
SLUG_KEY_PREFIX = "slug:"


async def enumerate_slugs(store, list_keys) -> list[str]:
    """Every slug that has a record, derived from a key enumeration rather
    than read from `all_links`. Order is UNSPECIFIED — KV key order is not
    defined, so any caller that renders must impose its own."""
    return [
        key[len(SLUG_KEY_PREFIX):]
        for key in await list_keys(store)
        if key.startswith(SLUG_KEY_PREFIX)
    ]
```

Rewrite `handle_list` to take `list_keys` and derive:

```python
async def handle_list(store, principal: Principal, get_many, list_keys):
    slugs = await enumerate_slugs(store, list_keys)
    fetched = await get_many(store, [f"slug:{slug}" for slug in slugs])
    records = []
    for slug in slugs:
        raw = fetched.get(f"slug:{slug}")
        if raw is None:
            continue                      # unchanged tolerance
        try:
            record = json.loads(raw)
        except (ValueError, TypeError):
            continue                      # unchanged tolerance
        if not can_view(principal, record):
            continue
        records.append(record)
    records.sort(key=lambda r: (r.get("created_at") or "", r.get("slug") or ""))
    return json_response(200, {"links": [public_link(r) for r in records]})
```

Three things about that function are load-bearing:

- **`can_view` replaces the `view_all`/`owned_slugs` branch.** It is the
  existing shared helper and already encodes owner-or-`links.view_all`-or-
  `links.edit_all`. Do not re-implement the rule.
- **The explicit sort is not optional.** Key enumeration order is unspecified
  (`consistency._finding_sort_key` exists for exactly this reason), and the
  dashboard renders server order by default. Ascending `created_at` reproduces
  today's oldest-first `all_links` append order. The `slug` tie-break matters
  because a bulk create stamps one `created_at` on all its rows; those rows will
  now render slug-ascending instead of submission order. That is the one
  cosmetic behaviour change in Stage 1 and it is accepted.
- **A caller without `links.view_all` now reads every link record** and filters
  server-side. That is one `get_many` host call, the same shape an admin's
  request already makes today; it is not a new class of exposure, only a new
  population paying it. (The byte-ceiling caveat CLAUDE.md records for
  `backup.handle_export`'s chunking applies here in principle — `target_url` has
  no length cap — but `handle_list` already fetches every visible record for
  admins today, so this changes degree, not kind. Noted under follow-ups.)

Do **not** touch `all_slugs`/`owned_slugs` in Stage 1 — they still have callers
until Stage 2, and leaving them lets Stage 1 land alone.

### `api/analytics.py`

`handle_click_totals` derives `visible` from the same enumeration:

```python
async def handle_click_totals(links_store, analytics_store, principal, list_keys, get_many):
    slugs = await links.enumerate_slugs(links_store, list_keys)
    if principal.has_permission("links.view_all") or principal.has_permission("links.edit_all"):
        visible = set(slugs)
    else:
        fetched = await get_many(links_store, [f"slug:{s}" for s in slugs])
        visible = set()
        for slug in slugs:
            raw = fetched.get(f"slug:{slug}")
            ...  # parse, skip missing/unreadable, keep if can_view(principal, record)
```

The rest of the function (enumerate the analytics namespace, filter with
`parse_analytics_key`, `get_many` the count keys, `_merge_counts`) is unchanged.
Keeping the `visible` filter matters: without it the endpoint would return other
users' click totals.

### `api/app.py` — one raw walk per request, scoped

`handle_click_totals` now enumerates twice (links namespace, then analytics
namespace) and `scoped_list_keys` walks the whole physical store each time. Add
a per-request memoised raw walk and hand the `scoped_list_keys` built over it
**only** to `handle_click_totals`:

```python
def _memoized_kv_keys():
    """One raw get_keys walk per request, shared by every namespace view.

    NEVER pass the scoped_list_keys built over this to backup.handle_restore:
    restore calls list_keys AFTER writing, specifically to find pre-existing
    keys to prune, and a pre-write snapshot would silently change what it
    prunes. That is the exact reason a global cache was rejected on
    2026-08-04 (TASKS.md, "Considered and rejected").
    """
    cached = None

    async def kv_keys(store):
        nonlocal cached
        if cached is None:
            cached = await _kv_keys(store)
        return cached

    return kv_keys
```

Wire it as `list_keys_once = kvprefix.scoped_list_keys(_memoized_kv_keys(), collector)`
built inside `_dispatch` alongside the existing `list_keys`, and pass
`list_keys_once` to `analytics.handle_click_totals` only. Every other call site
keeps today's uncached `list_keys`.

A traced `click-totals` will afterwards show **one** `list_keys` op rather than
two — that is the intended signature, not a lost measurement.

`links.handle_list` also gains `list_keys` at its call site (`api/app.py:285`),
using the uncached one (it enumerates once).

### `api/users.py`

Replace the `409` gate's index read with a record scan:

```python
owned = await links.slugs_owned_by(links_store, username, list_keys, get_many)
```

with `links.slugs_owned_by` implemented over `enumerate_slugs` + `get_many` +
`record["owner"] == username`, skipping missing/unreadable records. `handle_delete`
gains a `get_many` parameter (it already has `list_keys`); wire it in
`api/app.py:381`. This costs one walk plus one `get_many` on a rare admin
action, and it fixes a real hole in passing: today a link whose `owner_links:`
entry has drifted **does not block its owner's deletion** — that is the exact
scenario `docs/plans/kv-consistency-check.md` says the `unindexed_owner_link`
check was built for, and it is pinned by
`api/tests/test_consistency_scenarios.py`. Derivation closes it, so that test's
"and `handle_delete` still returns 200" half must be inverted.

### `api/bulk.py`

`handle_bulk_create`'s `existing = set(await links.all_slugs(store))`
(`api/bulk.py:217`) becomes `existing: set[str] = set()`, with a comment
pointing at the `get_many` confirmation 15 lines below that is already the real
check. Verify no row can be reported twice: with `existing` empty, no row is
pre-flagged `slug_taken` by `validate_bulk_rows`, so every collision is reported
exactly once by the `get_many` pass, with the identical `slug_taken` shape.
`taken = set(existing)` still seeds `allocate_random_slug`, which probes
`store.exists` per candidate, so uniqueness is unaffected.

This removes one KV read from every bulk create.

## Stage 2 — stop writing the indexes, and retire six checks

**Stage 2's two halves must ship in the same release.** The moment the indexes
stop being written, every new link would report as `unindexed_link` on a
perfectly healthy store — the textbook way a checker gets ignored. Deleting the
writes without deleting the checks is worse than doing neither.

### Writes

- Delete `add_slugs_to_indexes`, `remove_slugs_from_indexes`,
  `move_slugs_between_owners`, `all_slugs`, `owned_slugs` and
  `ALL_SLUGS_INDEX_KEY` from `api/links.py`.
- `handle_create`: drop the index call and, with it, the whole
  `except kvretry.WriteFailed` branch that returns
  `{"partial": true, "index_updated": false, "next_step": "consistency_repair"}`.
  It always returns `201` again. The record write keeps its `RECORD_WRITE`
  retry.
- `handle_delete`: drop `remove_slugs_from_indexes` and, in the
  unreadable-record branch, the `all_links` rewrite and the `hint` about
  `orphan_owner_index_entry` (that entire failure mode disappears — a corrupt
  record's owner no longer needs to be known in order to clean up after it).
  The response loses `index_updated`.
- `bulk.handle_bulk_create`: drop `add_slugs_to_indexes` and `index_updated`
  from the response; `next_step` on a partial run becomes `"resubmit"`
  unconditionally, since drift is no longer possible.
- `bulk.handle_bulk_action`: the `delete` and `reassign` branches lose their
  index steps entirely; `reassign` becomes a pure record rewrite. All six
  branches report `index_updated: true` today; the field goes away.
- `users.handle_delete`: drop `await links_store.delete(f"owner_links:{username}")`.
  Its cross-store write-order comment shrinks to sessions → user record →
  `_meta:usernames`.
- **Writes per authoring request afterwards:** single create 3 → **1**, single
  delete 3 → **1** (plus the inline analytics purge), 50-row bulk create 52 →
  **50**, 50-slug bulk delete 52 → **50**, 50-slug reassign 51+ → **50**.

**What replaces "records first, indexes last":** *there is no index; a record's
existence is the only truth.* Every interruption point leaves exactly the
records that landed, all of them listed, none of them advertised-but-missing.
This subsumes the "abandon and index what landed" rule from
`docs/plans/write-throttle-resilience.md` — that rule existed to keep a
throttled run from leaving drift, and after Stage 2 a throttled run structurally
cannot. The **record-write retry stays**; only the index half of that plan
becomes moot.

### Checks

`api/consistency.py`'s `CHECKS` drops six and keeps six:

| id | fate |
|---|---|
| `unindexed_link` | **delete** — no index to be missing from |
| `missing_link_record` | **delete** — no index to name a missing record |
| `unindexed_owner_link` | **delete** |
| `owner_index_mismatch` | **delete** |
| `orphan_owner_index_entry` | **delete** |
| `dangling_owner_index` | **delete** |
| `unknown_link_owner` | keep — a record whose `owner` has no user record is still real drift, and it is now the *only* owner check |
| `unindexed_user` | keep (still repairable) |
| `missing_user_record` | keep (still repairable) |
| `orphan_session` | keep (still repairable) |
| `unreadable_value` | keep |
| `unrecognized_key` | keep |

`REPAIRABLE_CHECKS` goes from eight to three. `api/consistencyrepair.py` loses
five repair branches and, with them, both of its safety preconditions — the
`present_slugs` guard and `would_orphan_unindexed_link` / the `all_links`
post-state computation. Those exist solely to protect index removals that no
longer happen. `collect` stops parsing `all_links`/`owner_links:` and stops
returning `all_links`, `owner_index`, `unreadable_owners` and `present_slugs`;
it still returns `link_records` (for `unknown_link_owner`) and everything on the
users side.

**`GET /api/admin/consistency` stays meaningful** because its remaining subjects
are all still authoritative keys: `users:_meta:usernames` is still a maintained
index, sessions are still keys that can outlive their user, values can still be
corrupt, and unknown key shapes still want reporting. A clean store still
reports `ok: true` with every remaining check at `count: 0`.

### The leftover keys

`links:all_links` and `links:owner_links:<U>` stay in the store, inert. They are
**not** deleted by this change:

- `collect` must classify both shapes as known-and-ignored (the way
  `_meta:bootstrapped` already is) or they will report as `unrecognized_key` on
  every run forever.
- `api/backup.py` needs **no change**: leaving `all_links`/`owner_links:` in
  `INDEX_KEYS` and `restore_write_order` keeps every pre-change backup file
  round-tripping byte-identically, and writing an inert key back is harmless.
  A test pins this rather than leaving it to inference.
- Deleting them is a follow-up, not a task here: at ~68.7 µs each they cost
  ~0.1 ms of walk on a store with 10 users, and inventing a one-shot deletion
  path is more machinery than the saving justifies.

## GUI changes

`gui/` is served from a startup snapshot — **restart `spin up` after editing
anything under `gui/`**, or a correct fix will keep reproducing the old
behaviour.

Stage 1 needs **no GUI change at all.** That is a feature: it is the whole
user-visible fix, and it ships without touching the snapshot-served assets.

Stage 2:

- `gui/dashboard.js` — remove the `data.index_updated === false` branches at
  `:797`, `:858`, `:916` and `:1079`, and the shared
  `renderIndexNotUpdatedWarning()` helper at `:950`. A partial bulk create still
  renders its `not_created` error table.
- `gui/admin/backup.js` — remove the six retired ids from the `CHECK_COPY` map
  at `:182-215`. The repair buttons are already driven by the server's
  `repairable_checks`, so no button logic changes; only copy is deleted.
- `gui/admin/backup.html` — only if its consistency article names a retired
  check in prose.

No new tokens, no new components, no `DESIGN.md` implications: this is deletion
of existing copy.

## Data model

No new key type. No change to any record shape. Two existing key types stop
being written and become inert. `kvprefix.STORE_PREFIXES` is untouched, and so
is `redirect/linkgate/keys.go` — `redirect` never constructs or reads either
index key.

**What still races after Stage 2, stated so nobody believes this closed the
category:**

- `users:_meta:usernames` — read-modify-write on user create/delete. Admin
  frequency, and `unindexed_user`/`missing_user_record` (both repairable)
  survive precisely to cover it.
- `links:_meta:url_policy` — a lost update loses one admin's rule edit. Single
  admin, deliberate action; no check covers it, and none is proposed.
- `analytics:count:<slug>:<shard>` — documented separately (mechanisms M1/M2 in
  CLAUDE.md's Analytics section); unchanged.
- Two concurrent `PATCH`es to one link record — last writer wins, which is the
  ordinary and defensible semantics for a record edit.

## Trade-offs and rejected alternatives

**1. Do nothing — accept the race and lean on the repair tool.** Genuinely
attractive: zero code, and the repair is cheap and proven (65 findings in 2
writes on 2026-08-17, the O(distinct index keys) property confirmed live).
Rejected on three grounds. The damage is *silent* — the affected user sees a
successful `201` and a dashboard missing their links, with no in-product signal.
The repair is gated on `users.manage`, so the population that creates the drift
(marketing staff, non-technical, creating and deleting links heavily) cannot fix
it and has no reason ever to open the Store maintenance page. And the threshold
is embarrassingly low: two overlapping bulk creates, with a race window of one
KV round trip per authoring request — this is not a stress-test artifact. It
would be a defensible answer if the fix were expensive; the measured fix cost at
current scale is −1 ms.

**2. Verify after write — re-read the index and re-apply if your slugs are
missing.** Attractive because it is ~10 lines, needs no enumeration, costs
nothing on the happy path, and converges probabilistically. Rejected: Akamai KV
has a documented staleness artifact (a deleted key still reporting present via
`exists()`) and an inferred ~100 ms cross-instance propagation window from the
click-counter loss curve, so the read-back is exactly as untrustworthy as the
read that caused the problem. Worse, a re-apply writes a full array derived from
a possibly-stale read, so **a repair attempt can itself clobber a third
request's additions**, and the extra writes land precisely when the store is
already contended — cutting against the write-throttle plan's own
"abandon rather than hammer" rule. It converts a rare permanent loss into a
rarer permanent loss plus a new failure mode. **This is the fallback if M1
fails**, because if the enumeration is not fresh the derived design is worse
than what it replaces — but it is second choice, not first.

**3. Shard the index the way the click counter was sharded.** Attractive by
precedent: `CountShards` worked, it is raise-only, and it needed no migration.
Rejected because the precedent does not transfer. A count shard holds a
commutative integer whose loss is a *statistical* error already disclosed as
best-effort; an index shard holds set membership whose loss is *a link
disappearing*. Sharding divides the collision probability by S and eliminates
nothing — with 16 shards and two simultaneous writers, roughly 1 in 16 pairs
still collides, forever. It also costs: a new key type (all three obligations), a
migration off `all_links`, a removal path that must read every shard to find the
slug, rewritten consistency checks and repairs, and `get_many(S × owners)` on
every read. Maximum machinery for a partial fix.

**4. Serialise index writes behind a single owner.** Rejected as not
implementable: there is no CAS, no lock primitive, no queue, and
`wasi:messaging` is unsupported on Akamai. Both components have
`allowed_outbound_hosts = []`, so an external coordinator is out for the same
reason the login rate limiter is.

**5. Derive `all_links` only; keep `owner_links:<owner>` maintained** (the split
answer the brief invited). Attractive because it halves the change and dodges
the one genuinely expensive derivation — deriving per-owner membership means
reading every record to learn its owner, even for a user who owns 3 of 900.
Rejected on who it leaves broken: an ordinary account has role `user` and no
permissions (`auth.KNOWN_PERMISSIONS`; the bootstrap admin is the only account
created with `role: "admin"`), so a plain user's dashboard reads
`owner_links:<them>` and nothing else. The measured incident produced 28
`unindexed_owner_link` alongside 37 `unindexed_link` — both indexes lose, and
the half this option fixes is the half only admins read. It would leave the
symptom exactly as visible for the population that hits it. The cost that
motivates it also turns out to be smaller than it looks: reading every record is
one `get_many` host call (~10 ms at K=1,000, not billed as K reads), and
`handle_list` already does exactly that for admins today.

**6. Derive `all_links` from `users:_meta:usernames` + `owner_links:<U>`, with
no enumeration at all.** The most interesting rejected option, and the only one
that fixes anything without paying for a walk: one `get` plus one `get_many`
over K owner keys, O(users) and bounded, killing the single hottest shared key
outright and saving a write per authoring request. Rejected for three reasons.
It does not fix same-owner concurrency, which is exactly what the measured runs
exercised. It makes `dangling_owner_index` catastrophic rather than cosmetic — a
link owned by a user missing from `_meta:usernames` becomes invisible to
*everyone*, and CLAUDE.md documents the admin dashboard's owner filter (which
surfaces links whose owner no longer exists, marked "— deleted account") as
**the repair path** for deployments that orphaned links before the `409` gate
existed. And it substitutes one read-modify-write shared key
(`_meta:usernames`) for another on the link path, which is a smaller blast
radius, not a different property.

**7. Batched or gathered writes (`set_many`/`delete_many`, or gathering the
index writes).** Rejected repo-wide and restated here only so it is not
re-proposed: writes are cap-bound at 50/second app-wide while reads have 1,000
of headroom, the WIT disclaims atomicity for batched writes, and gathering would
queue against the cap rather than overlap.

**8. Rebuild the index on write from an enumeration** (instead of
read-modify-write). Rejected: two concurrent rebuilds still clobber, the outcome
becomes order-dependent rather than race-free, and it puts a whole-store walk on
every *write* path instead of every read path. Strictly worse than deriving on
read.

**9. The cost of derivation, accepted with eyes open.** It ties `GET /api/links`
to an unbounded per-key enumeration and forecloses the deferred pagination
design in `docs/plans/links-pagination.md`, which slices `all_links` before the
record fetch and relies on its append order — after this change a paginated
response would have to read every record to sort them, which is what pagination
was trying to avoid. That is accepted because that plan already ranks pagination
third and concludes it is the wrong tool for what people usually mean (the real
answer there is windowed client rendering, which needs no API change and is
unaffected by any of this).

## Tasks

The lines below were appended to `TASKS.md` under
`## Derived link indexes (fixing the concurrent index lost update)`. **TASKS.md
is authoritative**; this is a readable mirror and carries no checkbox state.

```
- [ ] M1 (BLOCKING): confirm the key enumeration sees a just-written slug: record — file(s): (none — measurement on the deployed build), TASKS.md — done when: five create-then-immediately-check cycles are run on the deployed build (3-row bulk create, then GET /api/admin/consistency issued within ~200 ms, then again after ~2 s), the count of missing_link_record findings naming a just-created slug is recorded for each, and TASKS.md states either "get_keys is read-your-own-write fresh for creates" (zero such findings across all five immediate runs) or "it lags", in which case docs/plans/derived-link-indexes.md's Stage 1 is stopped and re-planned toward its Trade-offs #2 fallback
- [ ] M2: measure enumeration staleness after a delete — file(s): (none — measurement on the deployed build), TASKS.md — done when: five delete-then-immediately-check cycles record whether a just-deleted slug still enumerates (it would surface as unindexed_link naming that slug), the observed lag window is written into TASKS.md, and the plan's "a stale enumeration entry is skipped by handle_list because its record get returns None" claim is confirmed or corrected
- [ ] Derive the link list from a key enumeration in handle_list (depends on M1 passing) — file(s): api/links.py, api/app.py, api/tests/test_links.py — done when: links.enumerate_slugs(store, list_keys) exists, handle_list(store, principal, get_many, list_keys) builds its result from every slug: key rather than all_links/owner_links:, filters with the existing links.can_view, sorts by (created_at, slug) ascending, still skips a slug whose record is missing or unreadable, a test proves a link whose record exists but which is in NEITHER index is listed, a test proves a slug named by all_links with no record is absent, a test proves a caller without links.view_all sees only their own links, and cd api && uv run pytest passes
- [ ] Derive click-totals' visible set, with one raw key walk per request — file(s): api/app.py, api/analytics.py, api/tests/test_click_totals.py — done when: app.py builds a per-request memoised raw get_keys walk and passes the scoped_list_keys built over it ONLY to analytics.handle_click_totals (never to backup.handle_restore, which enumerates after writing to find keys to prune — TASKS.md's 2026-08-04 rejected entry), handle_click_totals derives visible from links.enumerate_slugs plus a get_many over the records for a caller without links.view_all/links.edit_all, a test proves an unindexed link's clicks are returned, a test proves a caller without view_all gets totals only for links they own, and cd api && uv run pytest passes
- [ ] Derive the user-deletion 409 gate from records instead of owner_links: — file(s): api/links.py, api/users.py, api/app.py, api/tests/test_user_deletion.py, api/tests/test_consistency_scenarios.py — done when: links.slugs_owned_by(store, username, list_keys, get_many) exists, users.handle_delete uses it (taking get_many as a new parameter, wired in app.py), a test proves a user whose only link is missing from owner_links: still gets 409 user_owns_links, a test proves an owner_links: entry with no backing record no longer blocks deletion, the test_consistency_scenarios.py case asserting handle_delete still returns 200 under unindexed_owner_link is inverted with a comment naming this plan, and cd api && uv run pytest passes
- [ ] Stop seeding bulk create's duplicate check from all_links — file(s): api/bulk.py, api/tests/test_bulk.py — done when: handle_bulk_create passes an empty existing set to validate_bulk_rows and relies on the get_many record confirmation it already performs, a submitted slug that already exists is still reported exactly once as slug_taken with an unchanged error shape, random slug allocation is unaffected, and cd api && uv run pytest passes
- [ ] Deploy Stage 1 and trace the derived dashboard load (the user's call; depends on the four Stage 1 tasks) — file(s): (none — deploy + measurement), TASKS.md — done when: GET /api/links and GET /api/analytics/click-totals are each traced with X-SS-Debug on the deployed build, their list_keys count and duration and wall time are recorded in TASKS.md beside the ~175 ms pre-change /api/links baseline and compared against the 24 ms + 68.7 us/key model, click-totals is confirmed to show ONE list_keys op rather than two, and dev/bulk-concurrent.sh 4 5 <prefix> is re-run with every created link confirmed present in GET /api/links even while the consistency check still reports unindexed_link
- [ ] Stop writing all_links and owner_links: (MUST ship in the same release as the check-retirement task below) — file(s): api/links.py, api/bulk.py, api/users.py, api/app.py, api/tests/test_links.py, api/tests/test_bulk.py, api/tests/test_users.py — done when: add_slugs_to_indexes, remove_slugs_from_indexes, move_slugs_between_owners, all_slugs, owned_slugs and ALL_SLUGS_INDEX_KEY are deleted, no handler writes either key, handle_create returns 201 with no partial/index_updated/next_step branch, handle_delete and both bulk handlers no longer return index_updated, bulk create's partial next_step is always "resubmit", users.handle_delete no longer deletes owner_links:<username>, tests assert a single create performs exactly one KV write and a 50-row bulk create exactly 50, and cd api && uv run pytest passes
- [ ] Retire the six index-drift consistency checks and their five repairs (MUST ship with the task above) — file(s): api/consistency.py, api/consistencyrepair.py, api/tests/test_consistency.py, api/tests/test_consistency_repair.py, api/tests/test_consistency_scenarios.py — done when: CHECKS holds only unknown_link_owner, unindexed_user, missing_user_record, orphan_session, unreadable_value and unrecognized_key, REPAIRABLE_CHECKS holds three, collect classifies a leftover all_links or owner_links:<U> key as known-and-inert so it is never reported as unrecognized_key, collect no longer returns all_links/owner_index/unreadable_owners/present_slugs, the present_slugs and would_orphan_unindexed_link preconditions are deleted with the repairs that needed them, a store containing leftover index keys and a healthy set of links reports ok: true with every remaining check at count: 0, and cd api && uv run pytest passes
- [ ] Pin that a pre-change backup still round-trips unchanged — file(s): api/tests/test_backup.py — done when: a test restores a backup containing all_links and owner_links:admin, asserts both keys are written back byte-identically, asserts the consistency check reports neither of them, and carries a comment stating that api/backup.py deliberately needs no change because the keys are inert rather than unknown, and cd api && uv run pytest passes
- [ ] Remove the GUI's index-drift copy and branches — file(s): gui/dashboard.js, gui/admin/backup.js, gui/admin/backup.html — done when: dashboard.js has no data.index_updated branch and no renderIndexNotUpdatedWarning helper, backup.js's CHECK_COPY names only the six surviving checks, a partial bulk create still renders its not_created error table, the consistency article's prose names no retired check, spin up is restarted before checking (the gui snapshot is taken at startup), and cd gui-pages && uv run pytest passes
- [ ] Record the outcome in CLAUDE.md — file(s): CLAUDE.md — done when: the "KV consistency check" section states six checks and three repairable ones and explains that the index checks were retired because the indexes were, the "Consistency repair" section's twelve-row table is reduced accordingly and its present_slugs/would_orphan_unindexed_link paragraphs are removed, a new subsection states that all_links and owner_links: are derived from a key enumeration and why (the measured 2026-08-17 lost update), the "records first, indexes last" rule is replaced by "a record's existence is the only truth" everywhere it appears, the write-throttle section notes that its index-what-landed half is now moot while its record retry stands, the residual read-modify-write keys (_meta:usernames, _meta:url_policy) are named, and the ~250 ms traced list_keys revisit trigger is recorded
- [ ] End-to-end manual verification of derived link indexes — file(s): (none — verification step) — done when: against a local spin up, two overlapping bulk creates (dev/bulk-concurrent.sh style, or two curl calls issued in the same second) both have every one of their links present in GET /api/links and visible in the dashboard, a plain non-admin user sees exactly their own links and no others, deleting a link removes it from the list on the next load, the dashboard's default row order is unchanged (oldest first) and its owner filter still lists an owner whose user record no longer exists, and GET /api/admin/consistency returns ok: true immediately after the concurrent creates
```

## Critical files

- `docs/plans/derived-link-indexes.md` (new)
- `TASKS.md`
- `api/links.py`
- `api/analytics.py`
- `api/bulk.py`
- `api/users.py`
- `api/app.py`
- `api/consistency.py`
- `api/consistencyrepair.py`
- `api/tests/test_links.py`
- `api/tests/test_bulk.py`
- `api/tests/test_users.py`
- `api/tests/test_user_deletion.py`
- `api/tests/test_click_totals.py`
- `api/tests/test_consistency.py`
- `api/tests/test_consistency_repair.py`
- `api/tests/test_consistency_scenarios.py`
- `api/tests/test_backup.py`
- `gui/dashboard.js`
- `gui/admin/backup.js`
- `gui/admin/backup.html`
- `CLAUDE.md`

`api/backup.py`, `api/kvprefix.py`, `api/kvretry.py`, `redirect/`, `spin.toml`
and `Jenkinsfile` are deliberately **not** touched. CI's three commands are
unchanged.

## Verification

1. **M1, before writing any code.** On the deployed build, with the debug token
   from the operator's deploy-secrets file:

   ```bash
   # x5, and repeat the consistency call ~2 s later each time
   curl -sS -X POST "$APP_URL/api/links/bulk" -H "Cookie: $SESSION" \
        -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
        -d '{"text":"m1a,https://example.com\nm1b,https://example.com\nm1c,https://example.com"}'
   curl -sS "$APP_URL/api/admin/consistency" -H "Cookie: $SESSION" -H "X-CSRF-Token: $CSRF" \
     | python3 -c 'import json,sys; r=json.load(sys.stdin); print([c for c in r["checks"] if c["check"]=="missing_link_record"])'
   ```

   **Pass:** zero `missing_link_record` findings naming a just-created slug in
   all five immediate runs — the enumeration is read-your-own-write fresh, and
   Stage 1 is safe to build. **Fail:** any such finding — stop, record the lag,
   and re-plan toward Trade-offs #2. Clean up the `m1*` links afterwards
   (sequential deletes, never a concurrent burst).

2. **M2**, same shape: delete a link, immediately request the consistency
   report, and look for `unindexed_link` naming the just-deleted slug.

3. After each Stage 1 task: `cd api && uv run pytest` (baseline 668 passed).

4. After the GUI task: `cd gui-pages && uv run pytest`.

5. `cd redirect && go test ./linkgate/...` — expected to be unaffected, run once
   at the end to prove it. **Never `go test ./...`, `go build ./...` or
   `go vet ./...`** — they fail by design on `package main`.

6. **Local end-to-end, after Stage 1:**

   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```

   Log in at `http://localhost:3000/dashboard.html`. Fire two bulk creates in
   the same second (two backgrounded `curl`s, or `dev/bulk-concurrent.sh 2 3
   <prefix>` pointed at a local run). **Pass:** all six links appear in the
   dashboard. Before Stage 1 this reliably loses one request's three. Then
   confirm the row order is unchanged (oldest first), create a link and see it
   appended at the bottom, delete it and see it gone on the next load, and check
   a non-admin account sees only its own links.

7. **Deployed, after Stage 1 (the user's call to deploy).** Trace both dashboard
   requests with `X-SS-Debug` and record `list_keys` count/duration and wall time
   against the ~175 ms `/api/links` baseline. Confirm `click-totals` reports
   **one** `list_keys`, not two. Re-run `dev/bulk-concurrent.sh 4 5 <prefix>`:
   every created link must be present in `GET /api/links` **even though**
   `GET /api/admin/consistency` still reports `unindexed_link` — that
   combination is the proof Stage 1 works, and it is expected until Stage 2.
   Clean up paced, then repair, then confirm `ok: true`.

8. **Deployed, after Stage 2.** `dev/bulk-concurrent.sh 4 5 <prefix>` then
   `GET /api/admin/consistency` must return `ok: true` with zero findings —
   no drift to report because there is no index to drift. Confirm a traced
   single `POST /api/links` shows exactly one `set`.

## Out of scope / follow-ups

- **Deleting the now-inert `all_links` / `owner_links:` keys.** They cost ~68.7
  µs each per walk and are recognised-and-ignored by the consistency check. A
  one-shot cleanup is not worth its own endpoint; belongs under Future work if
  it ever is.
- **The `write_error=other` observability gap** (Akamai's real write-failure
  message is unknown and nothing logs it) — already its own Future-work entry,
  untouched here.
- **Stop re-fetching click totals on mutation-driven dashboard reloads** —
  decided 2026-08-12, still unbuilt, and its interaction with this plan is
  costed in "Recommendation": it *cancels* Stage 1's added walk on mutation
  reloads. Landing it after Stage 1 is worth more than it was before.
- **Dropping the `events:` ring-buffer write** — filed to double the app-wide
  click ceiling; it would also cut ~32% of every key walk in the app, which
  after this change includes `GET /api/links`. That makes it the first lever if
  the ~250 ms revisit trigger ever fires.
- **A length cap on `target_url`.** Not a new exposure (admins already
  `get_many` every visible record), but Stage 1 makes every user do it, and
  CLAUDE.md's byte-ceiling caveat for `backup.handle_export`'s 1,000-key chunks
  applies in principle. Worth a Future-work entry.
- **Pagination for `GET /api/links`** — unchanged in status (deferred, third in
  line), but `docs/plans/links-pagination.md`'s slice-the-index design is
  foreclosed by this change and would need re-planning if its trigger ever
  fires. Recorded under Trade-offs #9.
