# Denormalised Per-Slug Click Total — Re-Costing and Verdict

## Context

`GET /api/analytics/click-totals` feeds the dashboard's Clicks column. It
enumerates the whole physical KV store once and then reads **every existing
`analytics:count:<slug>:<shard>` key for every visible slug**, so its read count
grows with traffic toward `CountShards` (64) per link. That was measured on the
deployed build on 2026-08-11 (`TASKS.md`, "MEASURED: the Clicks column does not
scale") and the coupon-collector model held across a 130× range. Extrapolated,
one dashboard load at 100 links averaging 200 clicks issues ~6,126 reads —
**613% of the application's entire 1,000 reads/second budget**, which the
redirect hot path draws from too.

`api/analytics.py`'s `handle_click_totals` docstring already records a rejected
alternative:

> *"Rejected alternative, recorded so it is not re-proposed: maintaining a
> denormalized `analytics:total:<slug>` would make this O(N) reads, but it adds
> a THIRD KV write to every click. Writes are the binding constraint (50/second
> app-wide, already two per click); trading read cost for write cost is
> backwards here."*

That rejection had no measurement of the read side. It now does, so the user
asked for an honest re-costing, with "the original rejection stands" named as a
fully acceptable outcome.

**Verdict: the rejection stands — but its stated reason is wrong and must be
corrected, because a future reader arguing from the new numbers will overturn
the wrong reason and reach the wrong conclusion.** The write budget is *not*
what kills a denormalised total. What kills it is that
`analytics:total:<slug>` is a single-key read-modify-write — precisely the shape
that measured **25% click loss at 9.4 clicks/second on one key** on 2026-08-06
and that counter sharding exists to avoid — and the one host primitive that
would fix it, `wasi:keyvalue/atomics`' `increment`, is **documented by Akamai as
unsupported**. Options 1 and 2 both walk into it; dropping the events write buys
back the write budget and does nothing at all for the accuracy problem.

**What this plan therefore schedules is small and does not include a
denormalised total:** correct the docstring's reasoning, remove a measured
multiplier on the existing read cost that costs nothing to remove, and run one
bounded spike (`wasi:keyvalue/batch`'s `get_many`) that could collapse the read
fan-out with no data-model change, no write cost, no backfill, no accuracy
change and no feature loss. A cached-totals blob is designed here as the
fallback if the spike fails, and deliberately not built.

Confirmed decisions carried in from the user before planning:

- The re-costing is the deliverable; rejecting is an acceptable outcome.
- **Recent events stays** (decided 2026-08-10). That decision predates this
  measurement, so option 2 is presented as an explicit reopening for the user to
  rule on rather than quietly taken.
- Never gather writes; `gather_reads` is reads-only.
- Any measurement claim must say whether it is measured or modelled.
- A future measurement of this endpoint must seed **clicks**, not just links.

## Key technical facts confirmed during research

**Confirmed by reading code in this repo:**

1. **Analytics runs after the response is handed to the host, so an extra write
   costs write *budget*, not TTFB.** `sendRedirectThenRecord`
   (`redirect/main.go:380-398`) sets `Location`/`Content-Length`, calls
   `WriteHeader(302)` and then `w.Write([]byte{})` — the empty `Write` is
   load-bearing and documented as such, because `Write` is what calls the SDK's
   `send()`. `recordClickCount` and `recordClickEvent` are both invoked *after*
   that. The user's reading is correct.
2. **A successful redirect is 6 KV operations today**: `open`, `get(links:slug:…)`
   (`lookupLink`, `main.go:516`), `open`, `get(analytics:count:…)`,
   `set(analytics:count:…)` (`recordClickCount`), `set(analytics:events:…)`
   (`recordClickEvent`). 2 writes, 2 data reads, 2 opens.
3. **`handle_click_totals`' read shape** (`api/analytics.py:100-156`): one
   `get` of the visible-slug index (`links.all_slugs` or `owned_slugs`), one
   `list_keys` over the **whole physical store** (`kvprefix.scoped_list_keys`
   filters by prefix *after* the enumeration, `api/kvprefix.py:111-122`), then
   one gathered `get` per existing `count:` key of every visible slug.
4. **`consistency.py` never scans the analytics namespace.**
   `CONSISTENCY_STORES = ("links", "users")` (`api/consistency.py:29`). So a new
   `analytics:`-namespace key carries **no** consistency-check obligation —
   contrary to the general three-obligation rule in CLAUDE.md, which is written
   for `links:`/`users:` keys.
5. **`backup.py` round-trips a new analytics key with no code change.**
   `build_backup` loops over whatever keys it was handed
   (`api/backup.py:107-118`); `is_excluded_key`/`redact_user_value` are scoped to
   the `users` store; `INDEX_KEYS["analytics"] = ()` so `restore_write_order`
   treats it as a non-index key. A pinning test would be prudent, code would not
   be needed.
6. **A FOURTH obligation the brief did not list, and it is the expensive one.**
   `analytics.parse_analytics_key` (`api/analytics.py:62-79`) recognises only
   `count:` and `events:`. `analyticsorphans.classify_analytics_keys`
   (`api/analyticsorphans.py:67-96`) routes anything it does not recognise to
   `unrecognized`, which is **never purgeable by design** ("a future analytics
   key type must show up as something a human is told about, never as something
   this feature quietly deletes"). So a `total:<slug>` key would be a
   **permanent, unpurgeable orphan for every deleted link**, and the orphan
   report's `unrecognized_keys` count would grow without bound. It also breaks
   the planned-but-unbuilt `purge_slug_analytics`
   (`docs/plans/inline-analytics-purge-on-delete.md`).
7. **`gui/dashboard.js` re-fetches click totals on every mutation.**
   `loadClickTotals()` is called from inside `loadLinks()`
   (`gui/dashboard.js:379`), and `loadLinks()` is called from **9 sites**: 500
   (create-success dismiss), 634 and 650 (edit save / cancelled reassign), 754
   (bulk delete/enable/disable), 794 (bulk tag/untag), 838 (bulk reassign), 971
   (bulk create), 1013 (single create), and 1088 (bootstrap). Every one of those
   fires a full-store enumeration plus the whole count-shard fan-out. **Click
   totals cannot change as a result of any of those actions — only a click
   changes them.**
8. **A reassign cannot bring a link with pre-existing clicks into a viewer's
   dashboard**, so skipping the refetch has no staleness cost beyond "clicks
   that arrived since page load". Reasoning: to reassign a link you must be able
   to see it, and a viewer holding `links.view_all`/`links.edit_all` already has
   every slug in `clickTotals`; a `users.manage`-only operator cannot see links
   to select them (CLAUDE.md, "User deletion and link ownership", already
   documents that such an operator "would land on an empty dashboard").
9. **The dashboard's Clicks column carries no accuracy caveat; the detail page
   does.** `gui/links/detail.html:60` — *"Recorded best-effort. Accurate while
   the whole service stays under roughly 25 clicks per second; heavier traffic
   under-counts."* `grep -n -i "accura\|caveat\|best-effort" gui/dashboard.html`
   returns nothing.
10. **`recent_events` is read only by the per-link detail page**
    (`gui/links/detail.js:106,109`); the dashboard never reads it. The user's
    framing is accurate.
11. **`PRODUCT.md` commits to "accurate running totals"** and separately labels
    only the recent-events sample as "best-effort (lossy, not complete)". So the
    two halves of option 2 sit on opposite sides of a stated product commitment.
12. **Baseline confirmed green before planning:** `cd redirect && go test
    ./linkgate/...` → `ok`; `cd api && uv run pytest` → **557 passed** in 12.84 s.

**Confirmed against upstream documentation and the vendored SDKs:**

13. **`wasi:keyvalue/atomics` is NOT supported on Akamai Functions.**
    `techdocs.akamai.com/akamai-functions/docs/use-the-key-value-store`, fetched
    2026-08-12: *"the `wasi:keyvalue/atomic` interface is not supported because
    it requires a consistency guarantee not provided by our global store."* The
    same page: *"the `wasi:keyvalue/store` and `wasi:keyvalue/batch` interfaces
    are supported."* `developer.fermyon.com/wasm-functions/using-key-value-store`
    301-redirects to this same page, so there is one authoritative source, not
    two. **This closes the increment question the brief asked to spike — no
    spike needed, it is documented as unavailable.**
14. **The bindings do exist, which is why this needed checking rather than
    assuming.** Go: `imports/wasi_keyvalue_0_2_0_draft2_atomics/wit_bindings.go`
    exposes `Increment(bucket, key string, delta int64) Result[int64, Error]`
    (line 265). Python:
    `api/.venv/.../spin_sdk/wit/imports/wasi_keyvalue_atomics_0_2_0_draft2.py`.
    The Spin world imports the whole family — `include
    wasi:keyvalue/imports@0.2.0-draft2;` at
    `spin-go-sdk/v3@v3.0.0/wit/world.wit:24`. So it would *compile*; it is the
    host that will not serve it.
15. **`kv.Store` is not a `wasi:keyvalue` bucket.** `kv.Store` wraps
    `spin_key_value_3_0_0_key_value.Store` (`kv/kv.go:11-14`) while both the
    atomics and batch bindings take a `wasi_keyvalue_0_2_0_draft2_store.Bucket`.
    Using either interface means a second, separate `open` against a different
    WIT interface. Same in Python: `spin_sdk.key_value.Store` is
    `spin_key_value_key_value_3_0_0.Store`, while
    `wasi_keyvalue_batch_0_2_0_draft2.get_many` takes
    `wasi_keyvalue_store_0_2_0_draft2.Bucket`, which has its own `open(identifier)`.
16. **`get_many` exists and its own spec docstring states the win:** *"if you
    want to get the values associated with 100 keys, you can either do 100 get
    operations or you can do 1 batch get operation. The batch operation is
    faster because it only needs to make 1 network call instead of 100."*
    Signature: `get_many(bucket, keys: List[str]) -> List[Tuple[str,
    Optional[bytes]]]`. It is **synchronous** in the generated Python bindings
    (`def`, not `async def`) and its generated body is `raise
    NotImplementedError` — componentize-py substitutes the real import at build
    time, so **it cannot be exercised under host `pytest` at all**.

**UNCONFIRMED, and what it would take:**

17. **Whether Akamai counts a `get-many` of K keys as 1 read request or K
    against the 1,000 reads/second cap.** This is the single fact that decides
    whether batch reads are a complete fix or only a latency fix. The quota page
    says only "Key value query rates are limited" with no batch clause. Requires
    either a direct answer from Akamai or a deployed measurement (issue a
    known-size `get_many` at a known rate and watch for throttling).
18. **Whether `spin.toml`'s `key_value_stores = ["default"]` grant authorises a
    `wasi_keyvalue_store_0_2_0_draft2.open("default")`, locally and on Akamai.**
    Requires building a component that calls it.
19. **Whether Spin 4.0.2 locally implements `wasi:keyvalue/batch` at all.** The
    Akamai page speaks for Akamai. Requires a local `spin up` spike.
20. **The `~1,000 reads/second single-handler throughput ceiling`** recorded as
    an inference in `TASKS.md` ("Links pagination — deferred") is still
    unconfirmed, and the batch spike would distinguish its two competing
    explanations (app-wide cap vs. queueing under 100-way concurrency), because
    one host call is not 100-way concurrency.
21. **This deployment's real burst click rate.** `PRODUCT.md`'s "Evidence on
    Hand" is *"None on hand — no customer testimonials, campaign case studies,
    or usage metrics have been captured for this product record."* So every
    statement below about whether 16.7 clicks/second binds is reasoning about
    traffic *shape*, not a measurement. Requires instrumenting a real campaign
    send.

## The comparison, both caps, both sides, same units

### Read side

Reads per `click-totals` call, with `s(C) = 64 × (1 − (63/64)^C)` shards touched
per link at C clicks (**MEASURED** against real clicks on the deployed build
2026-08-11, model error −9% to +2% across 7 → 930 reads):

| clicks/link C | s(C) reads/link | reads @ 20 links | reads @ 100 links | links to reach 1,000 reads |
|---|---|---|---|---|
| 5 | 4.85 | 97 | 485 | 206 |
| 10 | 9.34 | 187 | 934 | 107 |
| 25 | 20.8 | 417 | 2,083 | 48 |
| 50 | 34.9 | 698 | 3,488 | 29 |
| 100 | 50.7 | 1,015 | 5,075 | 20 |
| 200 | 61.3 | 1,225 | 6,126 | **16** |

**The trigger is not link count; it is how many links have been clicked more
than a handful of times.** At 200 clicks per link the dashboard costs 1,000
reads — one full second of the whole application's read budget — at **16
links**. Sixteen campaign links with 200 clicks each is an ordinary year for
this audience. Today's production store is nowhere near it (14 links, only **2**
ever clicked, 36 live analytics keys, and the busiest slug's 524 clicks sit
entirely in the *legacy unsharded* `count:<slug>` key, so it contributes 1 read,
not 64 — see `TASKS.md`, "Inline analytics purge on single-link delete"), which
is why the endpoint measures ~7 reads and ~175 ms in production right now.

With a denormalised total the same call is `1 + N` reads with **no traffic term
and no enumeration at all**: 101 reads at 100 links, 1,001 at 1,000 links. At
100 links × 200 clicks that is a **60× reduction**, and it is the only option
considered here that removes the un-overlappable full-store `list_keys`
(measured `~24 ms + 68.7 µs/physical key`, ~44% of this endpoint's wall time)
from the dashboard path. That is a genuine, large win and this document does not
dispute it.

Wall time, **MEASURED** 2026-08-11: 175 ms at 7 reads, 197 at 106, 412 at 417,
792 at 930 — the fan-out serialises into waves once it passes `gather_reads`'
100-concurrent bound.

### Write side

| shape | writes/click | app-wide sustained click ceiling | change |
|---|---|---|---|
| today | 2 | 50 ÷ 2 = **25/s** | — |
| option 1 (add total) | 3 | 50 ÷ 3 = **16.7/s** | **−33%** |
| option 2 (add total, drop events) | 2 | **25/s** | none |

**Is 16.7 clicks/second the binding number, or is the realistic rate far below
it?** For *sustained* traffic, far below: this deployment's busiest link has 524
clicks in its whole lifetime, so sustained load is three or more orders of
magnitude under the ceiling, and 16.7 clicks/second sustained is ~1.44 million
clicks/day. **For bursts, unknown and plausibly closer.** The audience is a
marketing/campaign team (`PRODUCT.md`) and the traffic shape that comes with it
is an email or ad blast: a large fraction of a campaign's clicks arrive in the
first hour, concentrated on one slug. A 100k-recipient send at a 20% first-hour
click-through is ~5.5 clicks/second averaged, and real openers cluster, so a 5×
peak reaches ~28/second — over even *today's* 25/second ceiling. There is no
measurement of this (fact 21), so the honest statement is:

> **On the two caps alone, the read side is the nearer constraint and the
> original rejection's premise is the weaker half of the argument.** A 33% cut
> in a sustained ceiling this deployment has never approached is a smaller real
> cost than 613% of the read budget per dashboard load. If the write budget were
> the only objection, option 1 would win and option 2 would win outright.

That concession is exactly why the accuracy argument below has to carry the
verdict.

### Accuracy — the argument that decides it

`analytics:total:<slug>` is **one key per slug, read-modify-written on every
click, with no compare-and-swap available anywhere in Spin's KV.** That is,
byte for byte, the shape of the pre-sharding `analytics:count:<slug>` key, whose
loss curve was **MEASURED on the live deployment 2026-08-06** (20 requests per
condition, fresh link):

| clicks/second on one slug | loss |
|---|---|
| 0.5, 0.9, 1.6, 2.7 | **0%** |
| 9.4 | **25%** |

So a denormalised total is **exact below roughly 3 clicks/second on a given
link, and loses a quarter of clicks at 9.4/second** — a rate the app can
comfortably serve, and precisely the rate a campaign burst produces on the one
link the marketing team is watching. Meanwhile the 64-shard sum stays exact, so:

- **The dashboard's Clicks column would silently disagree with the same link's
  detail page**, low, by up to 25%+.
- **The wrong number is the one with no caveat** (fact 9), and the caveated page
  is the one that is right.
- `PRODUCT.md` commits to "accurate running totals" (fact 11). This breaks that
  commitment on the summary view while keeping it on the detail view — the worst
  of both, because the operator cannot tell which page to trust without reading
  this document.

**There is no way out within this host.** The three obvious escapes all fail:

- **`wasi:keyvalue/atomics increment`** — documented unsupported on Akamai
  (fact 13). The bindings compile (fact 14), which makes this a trap rather than
  an option: it would work locally-if-Spin-implements-it and fail on the only
  deployment target that matters.
- **Shard the total** — that is what `count:<slug>:<shard>` already is; the read
  cost returns immediately.
- **Make the total's write blind (no read)** — a blind `set` cannot accumulate.
  The redirect knows its own shard's new value and nothing about the other 63,
  so it cannot compute a true total without 63 more reads.

Exact totals + no CAS + no atomic increment ⇒ **the write set must be spread ⇒
the read set must be gathered.** The only escapes are to move aggregation off
the per-load path (option 3), to make N reads cost less than N round trips
(batch), or to reduce how often the demand occurs.

### Accuracy, second order: both options make the *accurate* counter less accurate

**MODELLED**, using this repo's own documented collision model (CLAUDE.md,
"Click counting is sharded across 16 keys": in-flight requests ≈ rate × KV
latency, and collisions over S shards grow as roughly k²/2S), and quoting
proportions rather than milliseconds per CLAUDE.md's explicit rule about the
Akamai regime swings:

| shape | data ops/click | handler KV time | in-flight k | shard collision rate |
|---|---|---|---|---|
| today | 4 (2 get, 2 set) | 1.00× | 1.00× | 1.00× |
| option 1 | 6 (3 get, 3 set) | **1.50×** | 1.50× | **≈2.25×** |
| option 2 | 5 (3 get, 2 set) | **1.25×** | 1.25× | **≈1.56×** |

Residual sharded loss was measured at 0–1% at ~20 clicks/second on the 64-shard
build, so 2.25× of that is ~2% in absolute terms — **small, and this is a
supporting argument, not the decisive one.** But the direction is the thing:
**both options lengthen the hot path in order to add a number that can be wrong,
and in doing so make the number that is currently right slightly less right.**

### Op-count changes to `redirect`, which every prior plan was forbidden to touch

| | ops | writes | reads | opens |
|---|---|---|---|---|
| today, success | **6** | 2 | 2 | 2 |
| today, miss | 2 | 0 | 1 | 1 |
| option 1, success | **8** | 3 | 3 | 2 |
| option 2, success | **7** | 2 | 3 | 2 |

A miss is unchanged in every option (no analytics is recorded). Neither option
changes TTFB (fact 1). Verification for either would be: `X-SS-Debug` trace of
one untraced-then-traced redirect showing `kv_ops=8` (or 7) with
`get=3/…  set=3/…`, plus `dev/click-load.sh` at ≤20 req/s against a fresh slug
comparing the denormalised total against the sharded sum on the same link — the
divergence is the thing being measured, and it will not reproduce locally
(sqlite loses nothing).

## The four new-KV-key-type obligations, costed

For an `analytics:total:<slug>` key specifically:

| obligation | cost | why |
|---|---|---|
| `api/kvprefix.py`'s `STORE_PREFIXES` | **free** | `analytics:` already exists; `total:<slug>` sits under it |
| `api/consistency.py` key-shape recognition | **free** | `CONSISTENCY_STORES = ("links", "users")` — the analytics namespace is never opened (fact 4) |
| `api/backup.py`'s `INDEX_KEYS`/`restore_write_order` | **near-free** | generic key loop, `INDEX_KEYS["analytics"] = ()` (fact 5). One round-trip test in `api/tests/test_backup.py` |
| **`parse_analytics_key` + `classify_analytics_keys`** | **real, and not in the brief** | otherwise every deleted link leaves a permanent unpurgeable orphan and the orphan report's `unrecognized_keys` grows forever (fact 6) |

Plus, for options 1 and 2: `redirect/linkgate/keys.go` gains `TotalKey(slug)`,
and `api/tests/test_kvprefix.py` gains a cross-language pin on it in the same
style as the existing `LinkKey`/`CountShardKey`/`CountShards` guards. And
`docs/plans/inline-analytics-purge-on-delete.md` — planned, unbuilt — would need
amending, because `purge_slug_analytics` inherits
`classify_analytics_keys`' behaviour and would silently skip the new key.

So the general "three obligations" rule in CLAUDE.md over-prices this key on two
counts and under-prices it on a third. **Worth recording independently of this
verdict.**

## Backfill, settled either way

Existing links have no `total:` key. The three candidate answers:

1. **Reader falls back to summing shards when the key is absent.** This is the
   one that looks free and is not. Falling back requires the shard keys, and
   discovering which shards exist requires the **full-store enumeration** — so
   one un-backfilled link keeps the enumeration in the dashboard path. Worse, a
   link that is never clicked again *never* gets a total key, so the fallback is
   permanent for exactly the dormant links a mature store is mostly made of.
   **The enumeration would never go away, which forfeits about 44% of the win.**
2. **Operator backfill action**, in the shape of the existing chunked purge
   endpoint: enumerate once, sum each slug's shards, write one `total:` per
   slug. N writes, sequential (writes are never gathered), so ~50/second — a
   1,000-link store is ~20 seconds, over Akamai's 30-second handler limit at
   ~1,500 links, therefore chunked with a `remaining_slugs` loop exactly like
   `handle_orphan_purge`. This is the only sound answer, and it is another
   endpoint, another GUI article and another confirmation posture.
3. **Treat absent as 0.** Silently zeroes every pre-change link's Clicks column.
   Unacceptable — indistinguishable from data loss to the audience.

**Decision: if a denormalised total were ever built, it must be #2, and #1 must
be explicitly rejected in that plan.** Recorded here so the cheap-looking answer
is not reached for later.

## Options costed

### Option 1 — denormalised total, third write per click. **REJECTED**

Read side 60× better, write ceiling 33% worse, and the total is a single-key
read-modify-write measured to lose 25% at 9.4 clicks/s on one link while the
detail page stays exact. Adds 2 ops to the hot path, raising modelled shard
collisions ~2.25×. Needs the fourth obligation, a `keys.go` constant, a
cross-language pin, and a chunked backfill endpoint. The write-budget objection
in the current docstring is real but is *not* what disqualifies it.

### Option 2 — denormalised total, drop the `events:` write. **REJECTED, and it reopens a settled decision**

⚠️ The user decided on 2026-08-10 that recent events stays. Presenting the
arithmetic rather than acting on it, as instructed.

**What it now buys, honestly:** the write ceiling is unchanged at 25 clicks/s,
so the original rejection's entire stated reason evaporates. Reads drop 60× and
the enumeration leaves the dashboard path. Analytics keys per link drop from
up to 95 to up to 65 (−32%), which independently slows every enumeration in the
app — the orphan report, the purge, and `handle_export`'s 999-operation walk.
It sacrifices a feature `PRODUCT.md` already labels "best-effort (lossy, not
complete)" and that the dashboard never reads (fact 10). `TASKS.md`'s Future
work already carries "Reduce the redirect's KV writes per click from two to
one" with two independent justifications, so half of this is separately
sanctioned.

**Why it still loses:** it fixes the write-budget objection and **does nothing
whatever for the accuracy objection.** The `total:` key is still one key per
slug, still a read-modify-write, still on the measured 0%-below-3/s,
25%-at-9.4/s curve. It still adds an op to the hot path (7 vs 6) and still
raises modelled shard collisions ~1.56×. It still needs the fourth obligation,
the backfill endpoint and the cross-language pin. **So it pays a real feature to
buy a read-side win that is available more cheaply and with no accuracy cost at
all if fact 17 goes the right way** — and if the batch spike fails, the cached
blob (option 3) delivers a *better* read profile than this option without
touching `redirect`, the events ring, or accuracy.

**The user's call, on these terms: dropping recent events is a reasonable trade
for the write ceiling alone (that is the existing Future-work entry), but it is
the wrong way to pay for the read cost.** Recommendation: keep the two questions
apart and do not couple them.

### Option 3 — cached totals blob. **DEFERRED as the designed fallback, not built**

One key, `analytics:_meta:click_totals`, holding `{"computed_at": iso,
"totals": {slug: n}}`, written by `api` and never by `redirect`.

- **Cache-hit read cost is 2 reads** — one visible-slug index `get` plus one
  blob `get`, no enumeration, no traffic term. That is *cheaper than option 1's
  `1 + N`* and it is the best read profile of any option here.
- **Zero per-click writes. `redirect` is untouched. No Go change, no
  cross-language constant, no hot-path op-count change, no TTFB change, no
  accuracy change to the sharded counter, recent events survives.**
- **Totals are exact at compute time and stale by up to the TTL.** Staleness is
  an honest, explainable failure mode (render "as of 14:02") where loss is not.
  This is the significant qualitative advantage over options 1 and 2.
- **Stampede is bounded by concurrency, not by request rate**: read the blob,
  serve it if `computed_at` is within TTL, otherwise recompute and write.
  Two simultaneous loads both recompute, produce the same value and last-write
  wins — wasteful, not wrong. No lock is needed and none is available.
- **The expensive path still exists and is still ~6,126 reads when it runs**,
  once per TTL rather than once per load. Its own ceiling is the 30-second
  handler limit at ~30,000 reads — roughly **500 links × 200 clicks** — beyond
  which the cache can never refresh at all, which is a worse failure than a slow
  page. A `MAX_` rail plus a partial-refresh design (refresh only the K stalest
  slugs per load, bounded writes on a GET) is where that goes, and it is real
  complexity.
- **New key type cost:** the same four obligations, but for **one** key rather
  than one per slug, so the `parse_analytics_key` change is a single allowlist
  entry and the "permanent orphan" problem shrinks to one cosmetic line in the
  orphan report's `unrecognized_sample`.
- **Trigger to build it:** the batch spike (task 3) failing, *or* the read
  threshold in the table above being crossed in production.

### Option 4 — do nothing. **This is the interim position, with a stated ceiling**

The operating ceiling, in links-and-clicks terms, from the measured model:

> **One dashboard load costs `1 + N × 64 × (1 − (63/64)^C)` KV reads for N
> visible links averaging C clicks each. It crosses 1,000 reads — one full
> second of the application's entire 1,000 reads/second budget, shared with
> every concurrent redirect — at roughly 200 links × 5 clicks, 48 × 25, 29 × 50,
> 20 × 100, or 16 × 200. Wall time crosses one second at about the same point.
> Production today sits at ~7 reads and ~175 ms.**

**Monitor it with one number, not two:** the `get` count on an `X-SS-Debug`
trace of `GET /api/analytics/click-totals` **is** Σ shards-touched, so a single
traced dashboard load answers "where are we on that curve?" exactly. Trigger:
`get` above ~500 on a traced production load.

### Option 5 — `wasi:keyvalue/batch`'s `get_many`. **SPIKE THIS FIRST**

Not in the brief's list; found while checking the atomics question, on the same
Akamai page that killed it.

**If Akamai counts a `get-many` of K keys as one read request (fact 17), this is
a complete fix that costs nothing else at all**: `handle_click_totals` becomes
`open` + `list_keys` + **one** batch read, regardless of traffic — better than
option 1's `1 + N` on the fan-out, with **no new KV key type, no backfill, no
`redirect` change, no write-budget change, no accuracy change, and recent events
untouched.** `backup.handle_export`, `consistency.collect` and
`links.handle_list` are the same shape and would benefit identically.

**If it counts as K reads, it is still a large latency fix and not a cap fix**:
one host call instead of 100-way concurrency collapses the 792 ms wave
serialisation, and would also distinguish the two competing explanations of the
unconfirmed ~1,000 reads/second single-handler ceiling (fact 20).

**Why it is a spike and not a design:** facts 17, 18 and 19 are all unconfirmed;
it needs a *second* store open against a different WIT interface (fact 15); the
Python binding is synchronous and its host-side body cannot be exercised under
`pytest` at all (fact 16), so any shipped use must sit behind a seam whose
fallback is the tested path; and a 6,000-key batch returns ~1.8 MB of values
against a 10 MiB response and 128 MiB memory limit, which needs its own bound.
**Do not design it before the spike answers fact 17** — that is the same mistake
this document exists to avoid.

## Trade-offs and rejected alternatives

1. **Option 1, the straightforward denormalised total.** Attractive because the
   read-side win is real, large and now measured, and because the brief's
   observation is correct that the extra write costs no TTFB. Lost on accuracy:
   a single-key read-modify-write is the exact shape measured losing 25% at 9.4
   clicks/s, and `increment` is documented unsupported on Akamai. Would ship a
   Clicks column that silently under-reports a busy campaign link while the
   detail page reports it correctly, with the caveat on the wrong page. Revisit
   **only** if Akamai ever supports `wasi:keyvalue/atomic` or Spin's KV gains a
   compare-and-swap.
2. **Option 2, the same with the events write dropped.** The most attractive
   alternative considered, and the one that defeats the *recorded* rejection
   completely — writes/click stays at 2, so the write ceiling does not move at
   all. Lost because dropping events pays for the wrong problem: it buys back
   the write budget, which was never the real objection, and leaves the accuracy
   objection fully intact. Reopens a decision the user took on 2026-08-10.
   Revisit as **two separate decisions**: "drop the events write to double the
   click ceiling" is a live Future-work item on its own merits; "denormalise the
   total" is rejected on its own merits.
3. **`wasi:keyvalue/atomics`' `increment` as the contention fix.** The one
   primitive that would make option 1 correct. Bindings ship in both SDKs and
   the Spin world imports the interface, so it would compile and might even work
   locally — which makes it a trap, not an option. Akamai documents it as
   unsupported *for the reason that matters*: it "requires a consistency
   guarantee not provided by our global store." Rejected as a documented dead
   end, not deferred as a spike.
4. **Lazy shard-summing fallback for un-backfilled links.** Attractive because
   it needs no migration and no operator action. Rejected: it keeps the
   full-store enumeration in the dashboard path forever, because a dormant link
   never acquires a `total:` key, forfeiting ~44% of the win and all of the
   simplicity.
5. **Option 3, the cached blob, built now.** Genuinely good — the cheapest read
   profile of any option (2 reads), no `redirect` change, staleness instead of
   loss. Deferred rather than rejected only because the batch spike may make it
   unnecessary, and it is strictly more code, a new key type and a new staleness
   concept in the UI. Trigger recorded under Future work.
6. **Doing nothing at all, including the two cheap tasks.** Live, and defensible
   for the *magnitude* problem — production is at ~7 reads against a ~1,000-read
   threshold. Rejected only for the *multiplier*: fact 7 means a marketing user
   who bulk-creates and then tags and then disables fires four full-store
   enumerations and four full fan-outs for data that provably cannot have
   changed. Removing that is five lines and has no downside worth the words.
7. **Making the Clicks column opt-in (a "Load click counts" button).** Would
   remove the cost from the default path entirely and is trivial. Rejected for
   now as a product regression on a column shipped deliberately two days ago,
   and because fact 7's fix captures most of the same benefit without taking
   anything away. Kept as the fallback lever if the spike and the cache both
   fail.
8. **Scoping `click-totals` to a `?slugs=` subset.** Attractive alongside the
   already-planned windowed rendering. Rejected because sorting the table by
   Clicks requires every visible link's total, and because the un-overlappable
   enumeration is unchanged by it — it caps the fan-out and not the base cost.
   `docs/plans/links-pagination.md` already records the stronger form of this
   argument: page-scoping `click-totals` "would make sorting the table by Clicks
   impossible in principle."

## Effect on the two planned-but-unbuilt items

- **`docs/plans/inline-analytics-purge-on-delete.md` — priority UNCHANGED, ship
  it next.** It addresses the **enumeration** term (`~24 ms + 68.7 µs/physical
  key`), and *none* of the options this document recommends touches that term:
  the batch spike collapses the fan-out and leaves `list_keys` exactly as it is.
  Only a denormalised total or the cached blob's hit path would have removed the
  enumeration, and both are rejected or deferred. So the plan's own conclusion
  stands verbatim — it "removes the unbounded orphan term, not the baseline".
  One amendment it needs if a denormalised total is ever built: fact 6.
- **`docs/plans/links-pagination.md` — priority UNCHANGED, still third.** Its
  ordering constraint ("do not pick this up before `click-totals` is bounded")
  is untouched by this document, which does not bound `click-totals`. Its
  measured 2.9%-of-a-dashboard-load figure is unchanged.
- **The `## Links pagination` section's task 2** ("Measure
  `GET /api/analytics/click-totals`' shard-read fan-out against a clicked
  store") is now **satisfied** by the 2026-08-11 measurement and should be read
  as done when that section is next revisited; task 3 (the ~1,000 reads/second
  ceiling) is partly answerable by the batch spike here.

## Tasks

Appended verbatim to `TASKS.md` under `## The Clicks column's read cost — re-costing the denormalised total (2026-08-12)`:

```
- [ ] Correct handle_click_totals' rejected-alternative docstring to the real reason — file(s): api/analytics.py — done when: the docstring no longer says writes are the binding constraint, states instead that a single analytics:total:<slug> key is a read-modify-write with the same measured loss curve as the pre-sharding counter (0% below ~3 clicks/s on one link, 25% at 9.4/s, live 2026-08-06) while the sharded sum stays exact, names that wasi:keyvalue/atomic is documented unsupported on Akamai so increment cannot rescue it, points at docs/plans/denormalised-click-total.md, and cd api && uv run pytest still passes
- [ ] Stop re-fetching click totals on mutation-driven dashboard reloads — file(s): gui/dashboard.js — done when: loadLinks() takes an options argument defaulting to not refreshing totals, loadClickTotals() runs only from the bootstrap call at the end of the file, all eight mutation-driven loadLinks() call sites (create, bulk create, edit save, cancelled reassign, bulk delete/enable/disable, bulk tag/untag, bulk reassign, create-success dismiss) fire no /api/analytics/click-totals request, a newly created link still renders 0 rather than an em-dash in the Clicks column, sorting by Clicks still works after a bulk action, and cd gui-pages && uv run pytest still passes
- [ ] Spike whether wasi:keyvalue/batch get_many is usable and how it counts against the read cap — file(s): (none — spike, no shipped code) — done when: it is recorded whether wasi_keyvalue_store_0_2_0_draft2.open("default") succeeds under the existing key_value_stores = ["default"] grant locally on Spin 4.0.2 and on a deployed Akamai build, whether get_many over ~100 and ~1,000 keys returns correct values, the wall time of one get_many against the same keys read via gather_reads is compared on the deployed build with X-SS-Debug, and — the decisive question — whether Akamai bills a get-many of K keys as 1 read request or K is answered either by measurement under sustained load or by a direct answer from Akamai
- [ ] Record the verdict, the operating ceiling and the spike outcome — file(s): CLAUDE.md, TASKS.md — done when: CLAUDE.md's "The Clicks column's read cost scales with traffic" section states that the denormalised analytics:total:<slug> was re-costed against the measured read numbers and rejected on single-key contention rather than on write budget, carries the reads = 1 + N x 64 x (1 - (63/64)^C) formula with the 1,000-read threshold table, names the traced get count as the single monitoring number with a trigger of ~500, and records the batch get_many spike's outcome; and TASKS.md's "Future work (not scheduled)" carries the cached-totals-blob entry with its trigger
- [ ] End-to-end manual verification of the dashboard totals-refetch change — file(s): (none — verification step) — done when: against a real spin up with the browser network panel open, the initial dashboard load issues exactly one GET /api/analytics/click-totals, a bulk create of 3 links followed by a bulk disable and a bulk tag issues zero further click-totals requests, the three new links show 0 clicks, a clicked link's total is still correct after a page reload, and sorting by the Clicks column orders correctly after the bulk actions
```

## Critical files

- `docs/plans/denormalised-click-total.md` (new) — this document
- `api/analytics.py` — the rejected-alternative docstring on `handle_click_totals`
- `gui/dashboard.js` — `loadLinks()` / `loadClickTotals()` refetch coupling
- `CLAUDE.md` — the Clicks-column read-cost section, the verdict and the ceiling
- `TASKS.md` — the new work section, one Future-work entry, three rejected entries

Deliberately **not** touched, and worth stating because a denormalised total
would have touched all of them: `redirect/main.go`, `redirect/linkgate/keys.go`,
`api/kvprefix.py`, `api/backup.py`, `api/consistency.py`,
`api/analyticsorphans.py`, `api/tests/test_kvprefix.py`, `spin.toml`.

## Verification

1. `cd /Users/jhostetler/git/tirerack/spin-shortener/api && uv run pytest` —
   expect 557 passed (the confirmed baseline; the docstring change adds no
   tests).
2. `cd /Users/jhostetler/git/tirerack/spin-shortener/gui-pages && uv run pytest`
   — `test_no_inline_code.py` must still pass, since the change touches a `.js`
   file the CSP guard covers by omission.
3. `cd /Users/jhostetler/git/tirerack/spin-shortener/redirect && go test
   ./linkgate/...` — expect `ok`. Included only to confirm nothing drifted;
   **never `go test ./...`, `go build ./...` or `go vet ./...`, all of which fail
   by design on `package main`.**
4. Full app, from the repo root:
   ```
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```
   Sign in through the **login form**, not a raw fetch (a raw fetch login
   produces `csrf_mismatch` 403s that mimic permission bugs).
5. With the browser network panel filtered to `click-totals`: load
   `http://localhost:3000/dashboard.html`. **Pass:** exactly one request.
6. Create one link, then bulk-create three more, then bulk-disable them, then
   bulk-tag them. **Pass:** still exactly one `click-totals` request in total.
   The four new links render `0` in the Clicks column, never an em-dash.
7. `curl -s http://localhost:3000/r/<slug>` five times against one of the new
   links, then reload the dashboard. **Pass:** that link shows `5`; the others
   show `0`.
8. Click the Clicks column header twice. **Pass:** rows sort ascending then
   descending by total, with the just-created links at the correct end — this is
   the regression the change could plausibly cause, since sorting reads
   `clickTotals` directly (`gui/dashboard.js:316`).
9. For the spike only, and only against a deployed build: trace
   `GET /api/analytics/click-totals` with `X-SS-Debug` and record `get`,
   `list_keys` and `dur_us`. **Discard the first sample after any idle period —
   it measures ~175 ms against a 60–70 ms warm median.** Any click seeding must
   use `dev/click-load.sh` (it prints the implied write rate and warns at 50)
   and must stay under ~20 req/s, and **every seeded link's analytics must be
   purged afterwards via `POST /api/admin/analytics/purge`** with a follow-up
   orphan report showing `orphan_slugs 0`, or the next `list_keys` measurement
   anyone takes is corrupted.

## Out of scope / follow-ups

- **Building the cached totals blob (option 3).** Designed above, deliberately
  unbuilt. Added to `TASKS.md`'s "Future work (not scheduled)" with its trigger:
  the batch spike failing, or a traced production `click-totals` showing `get`
  above ~500.
- **Dropping the `events:` write to double the app-wide click ceiling.** Already
  a Future-work entry with two independent justifications. This document
  deliberately does **not** advance or retire it, and recommends it stay
  decoupled from the read-cost question.
- **A "Refresh totals" affordance on the dashboard.** The consequence of task 2
  is that click totals refresh on page load rather than after every mutation. A
  small refresh control would restore on-demand freshness for a campaign manager
  watching numbers arrive. Left out to keep the change to five lines; raise it if
  the user finds the new behaviour surprising.
- **Making the Clicks column opt-in.** The fallback lever if the spike and the
  cache both fail. Not proposed now (rejected alternative 7).
- **Instrumenting a real campaign send to get this deployment's burst click
  rate** (fact 21). Every statement in this document about whether the 25 or
  16.7 clicks/second ceiling binds is reasoning about traffic shape, not
  measurement. It is the one missing number that could change the write-side
  half of the comparison.
- **Pagination and windowed rendering.** Unchanged in priority and rationale;
  see `docs/plans/links-pagination.md`.
