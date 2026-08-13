# Inline Analytics Purge On Single-Link Delete

## Decisions taken 2026-08-11 — the three open questions are CLOSED

**1. The coupon-collector key-count model is MEASURED and holds. The
production datum that appeared to contradict it does not.** The plan flagged
that a 524-click link showing few keys was hard to reconcile with a model
predicting near-saturation. Decoding a live backup settles it:

| slug | shards | slots | legacy | total | clicks |
|---|---|---|---|---|---|
| `cYrI0dR` | **0** | 30 | 1 | 31 | 524 |
| `jwh` | 3 | 2 | 0 | 5 | 3 |

`cYrI0dR` holds **zero shard keys** — all 524 clicks are in the single legacy
unsharded `count:<slug>` key, so its entire history predates sharding and the
model was never meant to govern it. The planner's hypothesis was right.
**`jwh`, the only link whose whole history is post-sharding, holds 5 keys
against a predicted 6 at 3 clicks** — so the model is validated on the only
link that can test it, and the cost table in this plan stands. A useful
corollary: **legacy high-traffic links are cheap to purge** (31 keys, not 94),
because their clicks never fanned out across shards.

**2. No policy cap on inline purge; `MAX_INLINE_PURGE_KEYS = 128` stays as a
rail only.** The worst case — ~2.2 s at ~86% of the app-wide write cap, which
can silently drop concurrent clicks on other links — is accepted. It matches
the already-shipped purge endpoint (5.75 s per chunk), saturated deletes are
rare and low-urgency (end-of-campaign), and the frequent case costs ~6 writes.
A policy cap was rejected for the reason this plan already gives: it defers
exactly the links carrying the most keys.

**3. The delete confirmation copy is unchanged, and nothing is rendered for
`analytics_purge`.** Deleting a link already made its analytics unreachable
(the endpoint 404s on the missing link first), so nothing observable changes
for the user; adding words to the app's most common corrective action to
describe a consequence that already effectively existed is noise.

**Post-build local measurement (2026-08-12), corroborating point 1 above.** A
link clicked exactly 12 times and then deleted through the real dashboard
returned `analytics_purge: {"status": "complete", "found_keys": 20,
"deleted_keys": 20}`. The coupon-collector formula predicts
`64(1-(63/64)^12) + 30(1-(29/30)^12) ≈ 21.04` for C=12 — measured 20 is within
one key. This was run locally (sqlite-backed KV, not Akamai), so it
corroborates only the key-*count* half of the model, not the per-write timing
figures in the cost table below, which remain modelled pending a deployed
trace (Verification step 12).

## Scope correction worth carrying into the build

**Inline purge removes the unbounded growth term, NOT the baseline cost — it
buys time rather than solving the problem.** This plan's own correction of the
"feedback property" is right and deserves to be louder: post-purge production
is 57 keys of which **36 (63%) are live analytics**, and in a mature
deployment live analytics *is* the store. At 100 live busy links the store
holds ~9,400 live analytics keys — **~650 ms of enumeration on every dashboard
load with zero orphans present.** Pagination, caching `click-totals`, or
cutting the `events:` write remain necessary eventually; none of them is
displaced by this work.

## Context

`docs/plans/analytics-orphan-purge.md` shipped, deployed as
`55dc06d-orphan-purge`, and ran against production on 2026-08-11: 911 orphan
keys deleted across 4 chunked rounds, the analytics namespace went 947 → 36
keys, `list_keys` went 74.9 ms → 23.9 ms, and live click data was verified
intact by two independent code paths. That work stands and nothing here
touches it.

That plan's **rejected alternative #3, "Purging inline in `handle_delete` /
bulk delete"** (`docs/plans/analytics-orphan-purge.md:645–666`), is being
reopened at the user's request. Its four reasons were:

1. ~2–2.5 s added to every link deletion.
2. ~95 writes against the 50/second app-wide cap.
3. It adds a full-store enumeration to a request that currently makes none.
4. Bulk delete cannot fit: 4,750 writes ≈ 95 s against a 30 s handler limit.

**Reasons 1 and 2 were priced against the saturated 95-key worst case, and
that is not the common case.** Analytics keys accumulate by coupon-collector,
not linearly with clicks. The user has confirmed the dominant action for this
app's audience — non-technical marketing staff — is exactly the cheap case: a
link created with the wrong destination, deleted, recreated. Such a link holds
single-digit analytics keys.

**Reason 3's one-line dismissal of the conditional variant was inverted.** It
said knowing how few keys exist "requires the enumeration, which is most of
the cost". Measured, the enumeration is ~24 ms against ~2,185 ms for 95 writes
— the writes dominate by ~90×. The enumeration is what makes an inline purge
*cheap*, because it is what lets the handler delete only the keys that exist
instead of issuing 95 blind ones.

**Reason 4 stands and is confirmed below.** Bulk delete stays out.

The verdict: **build it, for single-link delete only.** The gap it closes is
that the shipped tool is operator-driven and the audience that creates the
orphans is not the audience that runs the tool — a marketing user deleting a
mistake has no reason to ever open the Store maintenance page, so today every
such delete ratchets the store up until an administrator remembers.

Confirmed decisions the user settled before planning:

- The three decisions recorded at the top of `docs/plans/analytics-orphan-purge.md`
  (retitle scope, the `{"confirm": "PURGE"}` + count-bearing-dialog
  confirmation posture, the `analyticsorphans.py` module name) are **closed and
  are not reopened here.**
- The manual operator tool survives regardless of the outcome — bulk delete
  will still orphan.
- `redirect` is not to be changed; the hot path stays at 6 KV operations.
- Deletes are writes and are **never** gathered.
- Bulk delete is out of scope unless the 95 s figure can be shown wrong.

## Key technical facts confirmed during research

- **The coupon-collector model is arithmetically right.** With
  `CountShards = 64` and `analytics_event_slots = 30`, both selected from
  entropy per click (`linkgate.ShardFor`, `linkgate.EventSlot`), expected
  distinct keys after `C` clicks is
  `64(1-(63/64)^C) + 30(1-(29/30)^C)`. Computed: C=1 → 2.0, C=3 → **5.9**,
  C=5 → 9.7, C=10 → 18.9, C=20 → **32.1**, C=50 → 59.5, C=100 → 79.8,
  C=200 → 91.3. The user's 6-at-3-clicks and 32-at-20-clicks figures both
  check out exactly.
- **But they are modelled, not measured per link, and one production data
  point suggests the model *over*-predicts.** The 2026-08-11 orphan report
  found **36 live analytics keys across 14 live links**, while the busiest
  live slug reports **524 clicks** — which the model would put near
  saturation (~94 keys) on its own. The likely reconciliation is that click
  history spans configuration changes (clicks recorded before sharding went
  into the single legacy `count:<slug>` key; clicks recorded at
  `CountShards = 16` spread over 16, not 64), so a link's key count reflects
  clicks *since the last config change*, not lifetime clicks. **Treat every
  per-link key count in this plan as MODELLED.** Verification step 6 measures
  a real one.
- **The `events:` ring buffer is the floor, and it saturates fast.** 30 slots
  are ~97% filled by 100 clicks, so any link past ~60 clicks holds ~28–30
  event keys regardless of how the count shards fell. A "modest" link's purge
  cost is dominated by event slots, not count shards.
- **A `list_keys` costs one KV round trip plus ~68.7 µs per physical key**
  (TASKS.md, "Deploy of the orphan purge + events fix", 2026-08-11; six-point
  regime-normalised refit, R² = 0.9989, floor measured at 0.85× a single
  `get`). The deployed store is **57 physical keys today**, so an enumeration
  is **23.9 ms measured**. It was 74.9 ms at 968 keys and the fit projects
  ~690 ms at 10,000.
- **A KV write costs ~20–26 ms on Akamai, and the latency regime swings ~3×
  between windows minutes apart** (CLAUDE.md, "Redirect caching"). Every
  millisecond figure below is therefore a proportion dressed as an absolute;
  re-measure rather than extrapolate.
- **`links.handle_delete` today is 6 data operations**: `get_link` (1 get),
  `store.delete("slug:<slug>")` (1 write), and `remove_slugs_from_indexes`
  (`api/links.py:75-88` — 1 get + 1 set of `all_links`, 1 get + 1 set of
  `owner_links:<owner>`). It never opens analytics. Confirmed by reading
  `api/links.py:352-360`.
- **`delete` on a missing key is a documented no-op**, so a blind-delete
  variant is possible:
  `api/.venv/.../spin_sdk/wit/deps/spin-key-value@3.0.0/key-value.wit:21-24`
  — *"No error is raised if a tuple did not previously exist for `key`."*
  It is costed and rejected in Trade-offs #2.
- **`get-keys()` takes no arguments** (same file, line 30) — no prefix, no
  cursor. An enumeration always walks the whole physical `default` store;
  `kvprefix.scoped_list_keys` filters *afterwards* (`api/kvprefix.py:119-122`).
  Already recorded in TASKS.md as CONFIRMED UNAVAILABLE.
- **`api/analytics.py` imports `links`** (`api/analytics.py:9`), so
  `links.py` must never import `analytics` or `analyticsorphans`. This is what
  forces the injected-callable shape in "API changes" below rather than a
  direct import.
- **`analyticsorphans.classify_analytics_keys` already does exactly the
  classification an inline purge needs** and is production-verified, including
  the legacy unsharded `count:<slug>` key and the `is_valid_custom_slug`
  safety valve (`api/analyticsorphans.py:70-104`).
- **`PrefixedStore` exposes `get`/`set`/`delete`/`exists` and deliberately no
  `get_keys`** (CLAUDE.md, "KV store"). Enumeration only ever happens through
  the injected `list_keys` callable.
- **Baseline test suite: `cd api && uv run pytest` → 557 passed** (run
  2026-08-11 at the start of this planning session).
- **UNCONFIRMED: Akamai KV read-after-write consistency for the same key.**
  No measurement exists. This plan is deliberately designed so the question
  never arises — the inline purge performs no read of a key it just wrote (see
  "Why the inline path does not re-check liveness"). Confirming it would need a
  traced deployed request that writes then immediately reads one key.
- **UNCONFIRMED: the wall-time cost of ~95 gathered `exists` reads.** The
  closest measurement is the 64-shard analytics endpoint's 98 gathered `get`s
  at 175 ms median / 246 ms mean wall (CLAUDE.md, "Click counting is sharded"),
  which is a whole handler rather than an isolated fan-out. This is why the
  gathered-probe alternative loses to the enumeration in Trade-offs #3 — one
  side is measured and the other is estimated.

## The decision, and the arithmetic that supports it

**Single-link `handle_delete` purges inline, unconditionally in practice**,
using one enumeration and then sequential deletes of exactly the keys that
exist. A `MAX_INLINE_PURGE_KEYS = 128` ceiling exists as a safety rail that
**cannot fire at shipped configuration** (64 shards + 1 legacy + 30 event
slots = 95 keys maximum).

Cost, using today's measured 23.9 ms enumeration and ~23 ms per write, against
today's ~6-operation delete (~140 ms of KV time):

| link kind | modelled keys | enum | deletes | added | delete total |
|---|---|---|---|---|---|
| never clicked | 0 | 24 ms | 0 ms | **24 ms** | ~165 ms |
| mistake caught early (~3 clicks) | 6 | 24 ms | 138 ms | **162 ms** | ~300 ms |
| modest (~20 clicks) | 32 | 24 ms | 736 ms | **760 ms** | ~900 ms |
| saturated campaign (200+ clicks) | 95 | 24 ms | 2,185 ms | **2,209 ms** | ~2,350 ms |

The user's table reproduces. Three things it does not say, which are the
actual load-bearing findings:

**1. The enumeration term is not constant — it is `24 ms + 68.7 µs × (store
size − 57)`, and the store size in a healthy deployment is set by *live*
analytics, which purging cannot reduce.** Post-purge the production store is
57 keys of which **36 (63%) are live analytics for 14 links**. In a mature
deployment live analytics *is* the store. So the feedback property the user
asked about is real but **weak: inline purging bounds growth, it does not
shrink the store.** At 30 well-used live links (~2,850 keys) the enumeration
is ~200 ms; at 100 (~9,500 keys) it is ~650 ms. It is self-stabilising in the
sense that it removes the *unbounded* term, and fragile in the sense that the
bounded term still grows with the business. This is accepted, because
`GET /api/analytics/click-totals` already pays the identical walk on every
dashboard load — a far more frequent request than a delete — so the marginal
aggregate cost of adding one to DELETE is small. **It is also the documented
trigger for switching implementations** (Trade-offs #3).

**2. The sequential delete loop runs at ~40–50 writes/second on its own,
which is at the app-wide cap.** A cap does not change that rate; it only
changes how long it lasts. So the honest statement is: **a saturated-link
delete suppresses roughly 2.2 seconds of concurrent click recording.** That is
accepted because the already-shipped `POST /api/admin/analytics/purge` does
the same thing for **5.75 seconds** per 250-key chunk and ran four such chunks
against production without incident. An inline purge is strictly less
aggressive than something already in service.

**3. Bulk delete's rejection stands, re-derived.** 50 links × 95 keys = 4,750
writes; at 20 ms that is 95 s and at 23 ms it is 109 s, against a 30-second
handler limit. Even the *cheap* case is uncomfortable: 50 × 6 = 300 writes ≈
6.9 s added to a bulk delete. And a bounded-budget variant would make an
endpoint whose defining property is all-or-nothing (CLAUDE.md, "Bulk link
management") partially complete, for a fraction of the benefit. Out of scope,
as instructed.

## API changes

### `api/analyticsorphans.py` — extract the executor, add the inline entry point

Two changes, both additive.

**Extract the enumerate-classify-delete core** that `handle_orphan_purge`
already performs inline (`api/analyticsorphans.py:274-296`) into a reusable
function, and have `handle_orphan_purge` call it. This is the reuse the plan
turns on — no new classification logic, no new key-shape knowledge, and the
inline path inherits the production-verified property that it **deletes
enumerated keys, never constructed ones**, so it picks up the legacy
unsharded `count:<slug>`, keys left by a since-lowered
`analytics_event_slots`, and any future analytics key type for free.

```python
MAX_INLINE_PURGE_KEYS = 128


async def purge_slug_analytics(analytics_store, slug: str, list_keys,
                               max_keys: int = MAX_INLINE_PURGE_KEYS) -> dict:
    """Delete every analytics key belonging to `slug`. One enumeration, then
    sequential deletes.

    THE CALLER MUST HAVE ESTABLISHED THAT `slug` HAS NO LINK RECORD. This
    function forms no liveness opinion of its own — `handle_orphan_purge`
    establishes it with an `exists("slug:<S>")` re-check against a possibly
    stale report, and `links.handle_delete` establishes it by having deleted
    the record itself, earlier in the same request. Do not wire a third
    caller without deciding which of those two it is.

    Returns {"status", "found_keys", "deleted_keys"[, "max_inline_keys"]}.
    Never raises for a KV failure mid-loop: it returns status "failed" with
    the count that got through, because the caller has already deleted a link
    and must not be told the deletion failed.
    """
```

Behaviour:

- `status: "complete"` — all `found_keys` deleted (including `found_keys: 0`,
  the never-clicked link).
- `status: "deferred"` — `found_keys > max_keys`; **nothing is deleted**, and
  `max_inline_keys` is included. Deferring wholesale rather than deleting a
  partial prefix is deliberate: a half-purged slug is still an orphan needing
  the operator tool, so spending ~3 s to not finish buys nothing.
- `status: "failed"` — an exception during the loop; `deleted_keys` is what
  completed. The remainder is an orphan, which is exactly the state the
  operator tool exists for.

**`DELETES ARE ALWAYS SEQUENTIAL, NEVER GATHERED`** — the rule already in the
module docstring covers this function unchanged. `gather_reads` is used only
for the enumeration's consumers, never for a delete.

### `api/links.py` — `handle_delete` gains an injected purge callable

`analytics.py` imports `links`, so `links.py` cannot import `analyticsorphans`
without a cycle. The dependency is therefore injected as a plain parameter —
the same idiom the repo already uses for `list_keys` (`backup.py`,
`consistency.py`, `auth.delete_sessions_for_user`) and `read_file`
(`gui-pages/routing.py`), and the reason those modules stay host-importable.

```python
async def handle_delete(store, principal: Principal, slug: str, purge_analytics=None):
```

Body, in this exact order:

1. `get_link` → 404 `not_found`.
2. `can_edit` → 403 `forbidden`.
3. `await store.delete(f"slug:{slug}")` — **the record.**
4. `await remove_slugs_from_indexes(store, {record["owner"]: [slug]})` — **the
   indexes.**
5. If `purge_analytics is not None`: `result = await purge_analytics(slug)`,
   wrapped in `try/except Exception` yielding
   `{"status": "failed", "found_keys": 0, "deleted_keys": 0}`.
6. `return json_response(200, {"ok": True, "analytics_purge": result})`, or
   exactly `{"ok": True}` when no callable was passed.

**`purge_analytics=None` must default to today's byte-identical behaviour**,
because `bulk.handle_bulk_action`'s delete branch (`api/bulk.py:377-382`) does
not go through `handle_delete` and must stay untouched, and because a test
pins that.

**The `try/except` is load-bearing, not defensive dressing.** Without it, a KV
failure during the purge turns a *successful* deletion into a `500
internal_error` (`api/app.py`'s handler wrapper), and the operator retries a
delete that will now 404. That converts a currently-reliable feature into one
that appears to half-work — the exact failure mode the original rejection
named for bulk. The link is gone; the response must say so.

### Write ordering — the user's reading is correct, confirmed

**Record → indexes → analytics.** Every interruption point leaves the stronger
invariant:

| interrupted after | state left | recoverable? |
|---|---|---|
| record delete | index entries with no record | yes — `handle_list` already skips them; `consistency.py` reports `missing_link_record` (info) |
| index rewrite | orphan analytics keys | yes — this is today's steady state; the shipped operator tool removes them |
| some analytics deletes | fewer orphan analytics keys | yes — same tool, same run |

**The reverse ordering is wrong and unrecoverably so.** Deleting analytics
first and then failing before the record delete leaves a **live link whose
click history has silently vanished**, with no tool anywhere that can restore
it and nothing that reports it. That asymmetry — recoverable orphans versus
irrecoverable data loss on a live link — is the whole argument, and it is the
same rule `bulk.py`, `backup.restore_write_order` and the single-link handlers
already follow.

### Why the inline path does not re-check liveness

`handle_orphan_purge` re-checks `exists(f"slug:{s}")` because its slug list
comes from a report that may be stale — that re-check is what makes the
lower confirmation bar safe, and it is unchanged.

The inline path does not, for three reasons:

1. It deleted the record itself, in this request, after loading it and
   checking `can_edit`. The invariant is established by control flow, which is
   stronger than a read.
2. A re-check would be the app's first **read-after-write on the same key**,
   and Akamai KV's consistency there is UNCONFIRMED. If it is not immediate,
   the check returns `True` forever and the feature silently never runs —
   a failure that would be invisible except through the orphan report.
   Not depending on it is better than confirming it.
3. It costs a KV read (~23 ms) on every delete for insurance against a coding
   error the docstring already guards.

**The one race this leaves, stated rather than hidden:** between our record
delete and our enumeration, another user could create a link on the same slug.
The purge would then delete that new link's analytics keys. The window is tens
of milliseconds and a brand-new link has approximately zero analytics keys, so
the worst case is one lost click on a link created in that window. Accepted.
Note the *opposite* case is a fix, not a bug: `docs/plans/analytics-orphan-purge.md`
records that slug reuse today inherits the deleted link's click history, and
purging on delete removes that inheritance at the source.

### `api/app.py` — wiring

The `DELETE` branch of the `/api/links/{slug}` route
(`api/app.py:249` region) passes the callable; nothing else changes:

```python
return await links.handle_delete(
    links_store, result, slug,
    purge_analytics=lambda s: analyticsorphans.purge_slug_analytics(
        analytics_store, s, list_keys),
)
```

`analyticsorphans` is already imported. `num_event_slots` is **not** needed —
the enumeration finds keys rather than constructing them, which is precisely
why this path has no dependency on `COUNT_SHARDS` or
`analytics_event_slots`.

## GUI changes

`gui/dashboard.js` only, and deliberately minimal.

- **`handleDeleteClick` disables the button for the duration of the request**
  and re-enables it on the error path, matching `handleStatusToggleClick`'s
  existing `btn.disabled = true` pattern (`gui/dashboard.js:516`). The success
  path calls `loadLinks()`, which re-renders the row away. This is the only
  visible change and it exists because the request now typically takes
  ~300 ms and can reach ~2.3 s on a heavily-clicked link.
- **The confirmation text does not change.** It stays `Delete the link "X"?
  This can't be undone.` Mentioning click history would be technically honest
  and practically misleading: from the user's point of view the analytics
  became unreachable the moment the link was deleted, purge or no purge — the
  detail page 404s on the missing record either way. Saying it here and not in
  the bulk dialog (which does *not* purge) would also advertise a distinction
  the user cannot act on and cannot see.
- **Nothing is rendered for `analytics_purge`, including the `deferred` and
  `failed` statuses.** The audience is non-technical marketing staff; a key
  count is not information they can use, and "some keys were left for an
  administrator" is an instruction they cannot follow. The field exists for
  `curl`, for tests, and for a builder debugging a deployed run. The operator
  learns through the Store maintenance page's orphan report, which is that
  page's entire purpose.
- **No new DESIGN.md token, no new component, no new CSS.** `DESIGN.md`
  contains no busy/spinner guidance and none is introduced; `disabled` is
  already the app's in-flight idiom.

## Does `consistency.py`'s "orphans are normal state" argument survive?

**Yes, and the revisit trigger does not fire.** Checked rather than assumed.

`docs/plans/kv-consistency-check.md`'s rejected alternative #1 states its own
trigger as *"deletion being changed to purge analytics. Then 'an orphan
analytics key' would become a genuine anomaly rather than the expected
outcome."* Read strictly, this change does alter deletion — so the trigger has
to be tested against what actually remains normal afterwards. Four independent
sources of orphan analytics survive this change:

1. **Bulk delete**, which is explicitly out of scope and stays unchanged.
2. **Every orphan created before this ships**, on any deployment that has ever
   deleted a link.
3. **An interrupted or failed inline purge** (`status: "failed"`), which is a
   designed, reported outcome rather than an error.
4. **A `deferred` purge**, if `MAX_INLINE_PURGE_KEYS` is ever reached.

So an orphan analytics key remains **expected, normal, intended state between
purges**. A 13th consistency check over them would still pin `ok: false` on a
structurally flawless store, and both of `consistency.py`'s framings — "re-run
to confirm" (orphan findings are perfectly stable) and "it reports and never
repairs" (the whole point here is the repair) — would still be wrong for it.
`api/app.py`'s comment at the consistency route, which explains why the
analytics view is deliberately not handed to `handle_consistency`, needs one
clause updated to say "bulk delete never removes them" instead of
"`links.handle_delete` never removes them" — the reasoning is unchanged, the
example is now wrong.

## What does not change

- **The manual operator tool.** `GET /api/admin/analytics/orphans`,
  `POST /api/admin/analytics/purge`, the `{"confirm": "PURGE"}` gate, the
  count-bearing `confirmDialog`, the no-typed-field decision, the 50-slug and
  250-key caps, the chunked client loop and the Stop button are all untouched.
  It is still needed for bulk-delete orphans, pre-existing orphans, failed
  inline purges and deferred ones.
- **The three decisions at the top of `docs/plans/analytics-orphan-purge.md`.**
  Closed, not reopened.
- **`redirect/`.** Not touched. The hot path stays at 6 KV operations and
  what a click writes is unchanged.
- **`spin.toml`, `Jenkinsfile`, `gui-pages/`, `api/kvprefix.py`,
  `api/backup.py`, `api/consistency.py`, `api/bulk.py`.** No new KV key type,
  no new route, no new page, no change to how tests are invoked.

## Trade-offs and rejected alternatives

### 1. Do nothing — the original rejection stands — rejected, and it was live

**Attractive because** the operator tool exists, works, and has been run
against production successfully. Nothing is broken. And a planner reopening a
settled decision on the strength of a hand-computed table is exactly how a
repo accumulates churn.

**Why it loses.** The tool's users and the orphans' creators are disjoint
populations. A marketing user deleting a mistyped link has no reason to open
the Store maintenance page, ever; an administrator has no signal that a delete
happened. So the shipped design requires a human to remember a chore whose
symptom (a dashboard that got 60 ms slower) is invisible until it is large.
The cost of removing that requirement for the *common* case is a measured
24 ms plus roughly 6 writes — three orders of magnitude below the figure the
original rejection priced. That is a genuinely different decision, not the
same one re-litigated.

### 2. Blind deletes with no enumeration — rejected

**Attractive because** `delete` on a missing key is a documented no-op
(`spin-key-value@3.0.0/key-value.wit:21-24`), so the handler could skip the
enumeration entirely and issue `count:<slug>`, `count:<slug>:0..63` and
`events:<slug>:0..29` unconditionally. No read at all, no store-size
dependence, and the simplest possible code.

**Why it loses, decisively.** It costs **95 writes on every single delete**,
including the never-clicked link that is the entire motivating case — 2,185 ms
and 95 writes where the enumeration variant spends 24 ms and 0 writes. It
inverts the cost model: the enumeration exists precisely to convert 95
speculative writes into a handful of real ones, and writes are the scarce
resource (50 RPS app-wide) while reads have 1,000. It would also make the
delete path depend on `COUNT_SHARDS` and `analytics_event_slots` and construct
keys rather than discovering them, losing the legacy-key and future-key-type
coverage that `classify_analytics_keys` gives for free.

### 3. A gathered `exists` probe over constructed candidate keys — rejected for now, with a trigger

**Attractive, and this is the strongest rejected option.** Probe the 95
candidate keys with `gather_reads(analytics_store.exists(k) for k in
candidates)` — reads, so gathering is allowed — then delete only those that
exist. The write count is identical to the enumeration variant, and the read
cost is **independent of store size**, which removes the one structural
weakness of the chosen design: that its enumeration term grows with live
analytics, which purging cannot shrink. At 10,000 keys the enumeration is
~690 ms and the probe is unchanged.

**Why it loses today.** The enumeration's cost is **measured** — six points,
R² = 0.9989, floor confirmed at 0.85× a `get`, currently **23.9 ms**. The
probe's cost is **estimated**: the nearest real datum is the 64-shard
analytics endpoint's 98 gathered `get`s inside a 175 ms median handler, which
is a whole request rather than an isolated fan-out, so the honest range is
roughly 50–250 ms. At today's store the measured option is cheaper than the
estimated option's *best* case, and this repo's standing rule is to re-measure
rather than extrapolate. The probe also constructs keys, so it misses the
legacy unsharded key unless special-cased, misses keys left by a since-lowered
`analytics_event_slots`, and misses any future analytics key type — all of
which the enumeration handles for free by reusing `classify_analytics_keys`.

**Revisit trigger, stated so this does not have to be re-argued:** a traced
`GET /api/analytics/click-totals` on the deployed build showing `list_keys`
above **~120 ms** (roughly 1,500 physical keys), *and* a measurement of an
isolated 95-way gathered `exists` fan-out on the same build. The two designs
are interchangeable behind `purge_slug_analytics`'s signature, so the switch
is a body change in one function.

### 4. A key-count cap that defers the expensive links to the operator tool — rejected as a *policy*, kept only as a rail

**Attractive because** it bounds the worst-case delete latency and the
write-cap contention window, and the enumeration tells you the count for free
— which is precisely the inversion this revisit corrects. A cap of 32 would
cover a ~20-click link and hold the whole delete under ~900 ms.

**Why it loses as a policy.** The links it would defer — heavily clicked ones
— are exactly the links carrying the most orphan keys, so the cap would opt
out of most of the benefit while keeping all of the complexity. It also
invents a caller-visible state (`deferred`) that the audience cannot act on,
and a delete whose behaviour silently changes past a click threshold is harder
to reason about than one that always purges. And the harm it bounds is smaller
than it looks: the sequential loop runs at ~40–50 writes/second whatever the
cap, so the cap shortens the contention window rather than reducing its depth,
and the shipped purge endpoint already sustains that window for 5.75 s per
chunk.

**What is kept:** `MAX_INLINE_PURGE_KEYS = 128` as a **safety rail, not a
policy**. At shipped configuration the maximum possible is 95, so it can never
fire; it exists so that a slug carrying keys from a once-larger
`analytics_event_slots`, or a future key type, cannot make a delete unbounded
and push a request toward the 30-second handler limit. A test pins
`MAX_INLINE_PURGE_KEYS >= analytics.COUNT_SHARDS + 1 + 30`, which is what
makes the "can never fire" claim survive the raise-only `CountShards` rule.

### 5. Inline purge on bulk delete too — rejected, the original arithmetic holds

**Attractive because** bulk delete is where large numbers of orphans actually
came from historically: the 911 keys purged from production were dominated by
`shardverify*`/`s64verify*` load-test slugs, and the 9,000 seeded links of the
`list_keys` growth measurement were removed in 180 bulk requests.

**Why it loses.** 50 slugs × 95 keys = 4,750 writes; at the measured 20–26 ms
per write that is **95–123 seconds** against a 30-second handler limit. Even
the cheap case adds ~6.9 s. A bounded-budget variant would make partial an
endpoint whose defining documented property is all-or-nothing, for a fraction
of the benefit, and would need the same chunked client loop the operator tool
already implements — at which point it *is* the operator tool, invoked from
the wrong page. The user's 95 s figure is confirmed and bulk stays out.

### 6. Doing the purge before the record delete — rejected

**Attractive because** it would remove even the tens-of-milliseconds
slug-recreation race, since the enumeration would provably see only the old
link's keys.

**Why it loses.** It inverts the write ordering. An interruption after the
analytics deletes but before the record delete leaves a live, resolving link
whose click history is gone, silently and unrecoverably — against orphan keys,
which are recoverable by a shipped tool and are today's normal state. The race
it closes costs at most one click on a link created inside a tens-of-ms
window. That trade is not close.

### 7. Making the purge asynchronous / fire-and-forget — rejected, unavailable

**Attractive because** the visitor-facing cost would disappear entirely.

**Why it loses.** There is no background execution under WASI — CLAUDE.md
records this for the analogous case of moving `recordAnalytics` off the
redirect's critical path, and TASKS.md's purge-schedule Future-work entry
records that every mutation in this app is request-driven. Nothing to build
against.

### 8. Emitting the purge result in the GUI — rejected

**Attractive because** silent behaviour is usually worse than visible
behaviour, and the `deferred`/`failed` statuses are real information.

**Why it loses.** The audience is explicitly non-technical marketing staff.
"6 analytics keys removed" is noise; "some keys were left behind for an
administrator" names a person the user cannot summon and an action they cannot
take. The information has a correct home — the Store maintenance page's orphan
report, which exists for the operator who *can* act — and adding it to the
delete flow would trade a real cognitive cost for no decision change. The
field stays in the JSON response for `curl` and for tests.

## Tasks

The lines appended to `TASKS.md` under `## Inline analytics purge on
single-link delete`, mirrored here for readability. `TASKS.md` is
authoritative; check the boxes only there.

```
- [ ] Extract purge_slug_analytics from handle_orphan_purge (must land before the links.py change) — file(s): api/analyticsorphans.py, api/tests/test_analytics_orphans.py — done when: purge_slug_analytics(analytics_store, slug, list_keys, max_keys=MAX_INLINE_PURGE_KEYS) exists with zero spin_sdk imports and returns {"status", "found_keys", "deleted_keys"}, handle_orphan_purge's behaviour is unchanged (its existing tests pass untouched), and new tests pin: only the named slug's keys are deleted, deletes are sequential rather than gathered (RecordingStore op order), a slug with no analytics keys returns status complete with found_keys 0, found_keys > max_keys returns status deferred with max_inline_keys and deletes nothing, and a store whose delete raises mid-loop returns status failed with the partial deleted_keys rather than propagating
- [ ] Pin MAX_INLINE_PURGE_KEYS above the shipped configuration ceiling — file(s): api/tests/test_analytics_orphans.py — done when: a test asserts MAX_INLINE_PURGE_KEYS >= analytics.COUNT_SHARDS + 1 + 30 and its failure message names the raise-only CountShards rule, so a future shard raise cannot silently start deferring every delete
- [ ] Add the injected purge callable to links.handle_delete (depends on the extraction) — file(s): api/links.py, api/tests/test_links.py — done when: handle_delete takes purge_analytics=None, omitting it returns exactly {"ok": true} with no analytics access at all, passing it returns {"ok": true, "analytics_purge": {...}}, the callable is invoked only on the 200 path (never after a 404 or 403), a RecordingStore pins that slug:<slug> and both index writes complete before the first analytics delete, and a callable that raises still yields 200 with analytics_purge.status == "failed"
- [ ] Pin that bulk delete still leaves analytics untouched — file(s): api/tests/test_bulk.py — done when: a test drives bulk.handle_bulk_action's delete branch over a store holding analytics keys for the deleted slugs and asserts every analytics key is byte-identical afterwards, with a comment naming docs/plans/inline-analytics-purge-on-delete.md's Trade-offs #5 as the reason
- [ ] Wire the callable in api/app.py (depends on the links.py change) — file(s): api/app.py — done when: the DELETE /api/links/{slug} branch passes purge_analytics built from analyticsorphans.purge_slug_analytics with the analytics view and list_keys, the consistency-route comment says bulk delete rather than links.handle_delete never removes analytics keys, and a real `spin up` DELETE of a clicked link returns 200 with analytics_purge.status == "complete"
- [ ] Pin that an inline purge cannot touch another namespace — file(s): api/tests/test_store_isolation.py — done when: a sixth test builds one physical FakeStore holding links:, users: and analytics: keys, drives links.handle_delete with the real purge callable over kvprefix.open_views, and asserts every users: key and every unrelated links:/analytics: key is byte-identical afterwards while only the deleted slug's record, index entries and analytics keys are gone
- [ ] Disable the Delete button while the request is in flight — file(s): gui/dashboard.js — done when: handleDeleteClick sets btn.disabled = true before the api.delete call and clears it on the error path, matching handleStatusToggleClick's existing pattern, the confirmation text is unchanged, nothing is rendered for analytics_purge, and gui-pages/tests/test_no_inline_code.py still passes
- [ ] Document inline purging on delete (depends on every task above) — file(s): CLAUDE.md — done when: CLAUDE.md's "Orphaned analytics purge" section states that single-link delete purges inline while bulk delete does not and why, records the record-then-indexes-then-analytics ordering with the irrecoverable-loss reason for it, notes that MAX_INLINE_PURGE_KEYS cannot fire at shipped configuration, and its consistency-check paragraph still correctly explains why orphans remain normal state
- [ ] End-to-end manual verification of inline purge on delete — file(s): (none — verification step) — done when: every numbered step in docs/plans/inline-analytics-purge-on-delete.md's Verification section passes against a real `spin up`, including that a sibling link's analytics survive untouched
- [ ] Measure a real link's analytics key count against the coupon-collector model — file(s): TASKS.md, docs/plans/inline-analytics-purge-on-delete.md — done when: a link is clicked a known number of times on a running app, its analytics_purge.found_keys on delete is recorded next to the model's prediction for that click count, and this plan's cost table is relabelled measured or its modelled figures corrected
- [ ] Deploy and trace one inline purge on Akamai (depends on a deploy) — file(s): TASKS.md — done when: a traced DELETE /api/links/{slug} with X-SS-Debug against the deployed build records dur_us, kv_ops, list_keys and delete timings for a link with known analytics keys, the real added latency is compared against this plan's ~24 ms + n x 23 ms model, and the figure is recorded
```

## Critical files

- `api/analyticsorphans.py`
- `api/links.py`
- `api/app.py`
- `api/tests/test_analytics_orphans.py`
- `api/tests/test_links.py`
- `api/tests/test_bulk.py`
- `api/tests/test_store_isolation.py`
- `gui/dashboard.js`
- `CLAUDE.md`
- `TASKS.md`
- `docs/plans/inline-analytics-purge-on-delete.md` (new)
- `docs/plans/analytics-orphan-purge.md` (amendment to rejected alternative #3 only)

No change to `spin.toml` (no new route, no new variable), `redirect/`,
`gui-pages/`, `Jenkinsfile` (test invocation unchanged), `api/kvprefix.py`,
`api/backup.py`, `api/consistency.py`, `api/bulk.py`, `gui/admin/backup.html`
or `gui/admin/backup.js`.

## Verification

1. `cd api && uv run pytest` — the baseline before this work is **557
   passed**; nothing may regress, and the new tests pass.
2. `cd gui-pages && uv run pytest` — still passes, confirming the
   `dashboard.js` change added no inline code.
3. `cd redirect && go test ./linkgate/...` — unchanged and passing. (Never
   `go test ./...`, `go build ./...` or `go vet ./...`: all three fail by
   design on `package main`.)
4. Start the app with a **persistent** store, so seeded analytics survive:

   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=devpass SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build
   ```

   `--runtime-config-file` is deliberately omitted — passing it selects an
   in-memory store wiped on every restart (CLAUDE.md's measured three-way
   table).
5. Seed the fixture in a browser signed in as `admin` (log in through the
   form — a raw `curl`/`fetch` login produces `csrf_mismatch` 403s that mimic
   permission bugs):
   1. Create two links, `keepme` and `killme`.
   2. Visit `http://localhost:3000/r/killme` **exactly 12 times** and
      `http://localhost:3000/r/keepme` several times, so both hold analytics
      keys.
   3. Confirm on the Store maintenance page that "Find orphaned analytics"
      reports `orphan_slugs: 0` — the fixture starts clean.
6. Delete `killme` from the dashboard row button. **Pass:** the button is
   disabled for the duration; the row disappears; the `DELETE` response body
   is `{"ok": true, "analytics_purge": {"status": "complete", "found_keys": N,
   "deleted_keys": N}}` with `N > 0` (read it from the browser devtools
   Network tab). **Record `N` against the model's prediction for 12 clicks
   (~21 keys)** — that is the measurement task above.
7. **The load-bearing check.** Open `keepme`'s detail page
   (`/links/detail.html?slug=keepme`). **Pass:** its click total and recent
   events are exactly what they were before step 6, and the dashboard's Clicks
   column still shows the same total. A purge that reached a sibling link
   fails here and nowhere else.
8. Re-run "Find orphaned analytics". **Pass:** `orphan_slugs: 0` — the delete
   left nothing behind, which is the entire feature.
9. Create a link, delete it without ever visiting `/r/`. **Pass:**
   `analytics_purge` is `{"status": "complete", "found_keys": 0,
   "deleted_keys": 0}` and the delete is visibly as fast as before.
10. Confirm bulk delete is unchanged: create two links, click each once,
    select both, Bulk delete. **Pass:** "Find orphaned analytics" now reports
    `orphan_slugs: 2`. Purge them with the existing tool. **Pass:** back to
    zero. This proves both that bulk was left alone and that the operator tool
    still works.
11. Browser console must be clean on `dashboard.html` throughout — a CSP
    violation fails a page silently rather than failing a test.
12. **Deployed, after the next deploy:** confirm `X-SS-Version` reports the new
    label before measuring anything (a request made during the ~100 s
    propagation window returns the OLD build and is actively misleading).
    Then trace a `DELETE /api/links/{slug}` with `X-SS-Debug` on a link with
    known analytics keys, discarding the first sample after idle. **Pass:**
    `kv_ops` equals 6 + 1 `list_keys` + `deleted_keys`, `analytics_purge.status`
    is `complete` (**not** `link_exists` — that status is impossible on this
    path by design, and its appearance would mean the code took the wrong
    branch), and the added wall time is within an order of magnitude of
    `24 ms + n × 23 ms`. Record the figure in TASKS.md.

## Out of scope / follow-ups

- **Inline purging on bulk delete.** Rejected with re-derived arithmetic
  (Trade-offs #5). Would only become viable if a future host offered a batched
  or ranged delete, at which point the whole design changes shape. No
  Future-work entry — the existing operator tool already covers it and a
  standing entry would invite someone to try the 95-second version.
- **Switching `purge_slug_analytics` from an enumeration to a gathered
  `exists` probe.** Trade-offs #3, with an explicit measurable trigger. Added
  under `TASKS.md`'s "Future work (not scheduled)".
- **A purge scheduler.** Unchanged and still open; this change reduces the
  need for one on the single-delete path only. Its existing Future-work entry
  stands.
- **Caching the physical KV key enumeration for the lifetime of one request.**
  Unchanged and still open. It would make the inline purge's enumeration free
  on any request that already made one — but a `DELETE` makes exactly one, so
  it buys this feature nothing directly.
- **Reducing the redirect's KV writes per click from two to one.** Still the
  complement: it would cut the `events:` slots, which are the *floor* on a
  clicked link's key count and therefore the dominant term in an inline
  purge's cost past ~60 clicks. Weigh the two together, as its entry says.
- **`GET /api/links` pagination** and **renaming `gui/admin/backup.html` on
  disk.** Untouched, both keep their existing entries.
- **No scratch file** (`docs/plans/inline-analytics-purge-on-delete-scratch.md`)
  was created: this is single-round planning with no open handoff state.
