# Click-Count Accuracy on Akamai

> **Revised 2026-08-06 after the gating probe was run on the live app.** The
> probe passed and the design holds, but it corrected three things: the probe
> as originally specified was confounded by Akamai's write-rate cap, the
> problem is **two independent mechanisms** rather than one, and the
> "~43 clicks/second" ceiling this plan originally claimed is unreachable
> arithmetic. Every number below is now labelled with which mechanism it
> speaks to. See "Two mechanisms" and "Probe result".

## Context

`analytics:count:<slug>` is a read-modify-write (`recordClickCount` in
`redirect/main.go` → `linkgate.UpdateCount`), and Spin's KV has no
compare-and-swap. On the live Akamai deployment it loses increments. The
original measurement, 2026-08-06, fresh link, 20 requests per condition, one
slug:

| per-link request rate | implied app writes/s | recorded loss |
|---|---|---|
| 0.5 /s | 1.0 | 0% |
| 0.9 /s | 1.8 | 0% |
| 1.6 /s | 3.2 | 0% |
| 2.7 /s | 5.4 | 0% |
| **9.4 /s** | 18.8 | **25%** |
| 34 /s | 68 | 70% |
| 65 /s | 130 | 75% |

**That curve blends two mechanisms and only its first five rows are usable for
reasoning about per-key contention.** Every click costs two KV writes, so the
last two rows are running at 68 and 130 writes/second against Akamai's
**50 write RPS** cap — they are measuring throttling as much as contention.
The `9.4 /s` row (18.8 writes/s) is the highest-rate point that is safely
under the cap, and it is the anchor for everything below.

The losses are permanent, not lag: a 25-request burst recorded 19 and was
still 19 at t+60 s. Locally (sqlite) the identical test records 20/20, so none
of this reproduces off the deployment.

Motivating `TASKS.md` Future-work entry: *"Fix Akamai click under-counting —
`analytics:count` loses ~15% under rapid sequential requests"* (raised
2026-08-06), carrying the curve above and the instruction **"The loss curve has
now been measured — do that no more; design the fix."** Related entries this
plan touches: *"Ask Akamai for a KV write-rate increase if redirect throughput
demands it"* (which this work makes **more** important, not less) and *"Find
the real cause of the analytics recent-events slot collisions"*.

**Confirmed decisions (settled by the user):**

- The gating probe **has been run**; its corrected result is recorded below and
  it is not a task for the builder.
- Any verification must be **on the deployed app**, on fresh links, and must
  hold aggregate writes/second below 50 or it measures the wrong thing.
- The plan must state explicitly what the fix does **not** fix, with numbers.
- **16 shards, not 8.** Reads are the cheap operation here (1,000 RPS cap
  against 50 for writes), and 8 shards would put a single link's per-shard rate
  at ~3.1/s at the app-wide ceiling — past the measured-clean band, with no
  direct evidence. 16 puts it at ~1.6/s, and the probe measured 0% loss at
  1.23/s per key directly.
- **Keep `linkgate.EventSlot`.** The optional task that would have replaced it
  with random slot selection is dropped; deleting it would orphan the open
  investigation into the recent-events collisions, which is now *more*
  interesting, not less (see "The recent-events ring buffer" below).
- **Retraction:** the original brief called the "~25 redirects/second"
  write-RPS ceiling *academic*. It is not. It is the real, measured, app-wide
  ceiling and after this work it is the **only** binding constraint. Anywhere
  that framing survives in this repo, it is wrong.

## Two mechanisms

The single-slug curve in Context is the sum of two independent effects that
have to be reasoned about separately, because one of them is fixable here and
the other is not.

**Mechanism 1 — per-key contention on a hot counter.** Two clicks on the *same
slug* close together: the second one's `Get` does not see the first one's
`Set`, so one increment is lost. Scoped to **one key**. Depends on that slug's
own click rate. This is what sharding fixes, and the probe below proves it is
real and separable.

**Mechanism 2 — app-wide write throttling.** Every click costs two KV writes
(`analytics:count:...` and `analytics:events:...`). Akamai's cap is 50 write
RPS **for the whole application**, so total clicks across *all* links, times
two, must stay under 50 — roughly **25 clicks/second app-wide**. Scoped to the
whole app, indifferent to which keys are involved. Sharding cannot touch it;
only writing fewer keys per click can (see Out of scope).

Numbers in the rest of this document are labelled **(M1)** or **(M2)**.

## Probe result (run 2026-08-06 on the live app)

The design rests on one property: **that contention is per-key.** N distinct
slugs are indistinguishable from N shards of one slug as far as the KV store is
concerned, so 16 fresh links simulate 16-way sharding exactly, with no build
and no deploy.

**The probe as this plan originally specified it (16 slugs at 34/s aggregate)
was confounded and returned a false negative.** At 38.5 req/s the app attempts
~77 writes/second — half again over the cap — so that run measured M2, not M1.
Re-run under the cap, the signal is unambiguous:

| condition | req/s | app writes/s | per-key rate | loss | measures |
|---|---|---|---|---|---|
| 1 slug | 9.4 | 18.8 | 9.4 /s | **25%** | M1 |
| **16 slugs** | **9.2** | **18.5** | **0.6 /s** | **0%** | M1 |
| 16 slugs | 19.7 | 39.4 | 1.2 /s | **0%** | M1 |
| 16 slugs | 38.5 | 77 | 2.4 /s | 32.5% | **M2** (over cap) |

**Rows 1 and 2 are the pair that decides it:** identical app-wide write load
(18.8 vs 18.5 writes/s), so M2 is held constant. One hot key loses 25%;
sixteen keys lose nothing. Contention is per-key, and 16-way sharding works on
this host.

Row 4 is the corollary and is just as important: 32.5% loss at a per-key rate
of 2.4/s, which is *inside the clean band* on the single-slug curve. That loss
cannot be M1. It is M2, and no amount of sharding would have removed it.

**The constraint this imposes on every future measurement, including this
plan's own verification: keep `request_rate × 2 < 50 writes/second`, i.e.
under ~25 requests/second aggregate.** Above that, a run measures the write cap
and any conclusion drawn about counter accuracy is wrong. This is why the
verification section below deliberately does **not** use the 34/s and 65/s
conditions the original brief asked for — at those rates a perfectly working
sharded build would show loss and read as a failure.

## Design: 16-way sharded counters

### Data model

| key | written by | read by | note |
|---|---|---|---|
| `analytics:count:<slug>:<n>`, `n` in `[0, 16)` | `redirect` | `api` | new; one `{total, days}` blob per shard, identical in shape to today's |
| `analytics:count:<slug>` | nothing, ever again | `api` | the pre-sharding key, summed in so upgrades keep their history |
| `analytics:events:<slug>:<slot>` | `redirect` | `api` | unchanged |

The write path stays **one `Get` + one `Set`** — the same two operations on a
differently-named key. **No new hot-path KV operation, no new latency, and —
critically, given M2 — no change to writes per click.** That last point is what
makes this safe to ship: a design that fixed M1 by adding a write would have
made M2 worse, and M2 is now the binding constraint.

The read path grows from 1 counter `get` to 17 (16 shards + the legacy key).
Reads are the cheap side: 1,000 read RPS against 50 write RPS, and the counter
is read only by one admin page.

**Migration is "there isn't one."** Existing `analytics:count:<slug>` keys are
never written again, so they are frozen and race-free, and the reader adds them
to the sum. No backfill, no admin endpoint, no downtime, and a deployment that
rolls back keeps counting into the legacy key exactly as before.

### Why 16 shards, stated against the reachable envelope

The honest way to size this is not "how high can the clean ceiling go" but
"is every shard inside the measured-clean band everywhere the app can actually
operate."

M2 caps the app at ~25 clicks/second. The worst case for M1 inside that
envelope is all 25 clicks/second landing on **one** link:

| shard count | per-shard rate at the app-wide ceiling | direct evidence |
|---|---|---|
| 8 | 3.1 /s | none — past the 2.7/s clean band, unmeasured |
| **16** | **1.6 /s** | probe row 3 measured **0% at 1.2/s per key** |

**So: within the app's reachable operating envelope, 16-way sharding
eliminates M1 entirely.** Not "reduces"; there is no rate the app can reach at
which a 16-shard counter's per-shard rate leaves the measured-clean band. The
benefit to state is therefore *not* a bigger number — it is that **two
constraints collapse into one**: before, an operator had to keep a single link
under ~3 clicks/second *and* the app under ~25; after, only the app-wide ~25
clicks/second write cap remains, and it is a constraint the deployment already
has for other reasons.

This is a better property than the "~43 clicks/second" figure this plan
originally claimed, which was 16 × 2.7/s on the blended curve and describes a
rate the app cannot reach.

### Redirect (Go) changes

**`redirect/linkgate/keys.go`** — replace `CountKey` (delete it; the file's own
closing comment already argues that an unused exported symbol is an
invitation):

```go
// CountShards is how many shards a slug's click counter is spread across.
// It MUST stay equal to api/analytics.py's COUNT_SHARDS: the writer picks a
// shard in [0, CountShards) and the reader sums shards [0, COUNT_SHARDS).
// If the reader's value is LOWER than the writer's, every click recorded in a
// higher shard silently disappears from the total, with no error anywhere —
// the same failure shape the prefixes above have, so it is pinned the same
// way, by api/tests/test_kvprefix.py reading this file.
//
// 16 is chosen so that even if every click the app can serve (~25/s, set by
// Akamai's 50 write RPS cap at two writes per click) landed on one slug, each
// shard would still see only ~1.6/s — inside the band measured lossless on
// the live app (0% at 1.2/s per key). See docs/plans/click-count-accuracy.md.
//
// RAISE ONLY, NEVER LOWER, and change both languages in the same commit.
const CountShards = 16

// CountShardKey is the physical key of one shard of a slug's click counter:
// analytics:count:<slug>:<shard>. The pre-sharding key was
// analytics:count:<slug> with no shard suffix; nothing writes that key any
// more and api/analytics.py still reads it, so no history is lost and there
// is no migration to run. A slug can never contain a colon (api/links.py's
// CUSTOM_SLUG_PATTERN is ^[A-Za-z0-9_-]{3,32}$ and generated slugs are drawn
// from ascii_letters+digits), so the two key shapes can never collide.
func CountShardKey(slug string, shard int) string {
	return AnalyticsPrefix + "count:" + slug + ":" + strconv.Itoa(shard)
}
```

**`redirect/linkgate/analytics.go`** — add the pure shard-selection logic.
`UpdateCount`, `trimDays` and **`EventSlot`** are all unchanged; `EventSlot`
stays (see "The recent-events ring buffer").

```go
// ShardFor maps a 64-bit entropy value onto [0, numShards).
//
// The value is run through a splitmix64 finalizer before the modulo so the
// result depends on all 64 input bits. This is deliberately NOT EventSlot's
// single multiply-then-reduce: that is documented (CLAUDE.md, "Analytics") as
// distributing badly against real request timing, the cause has never been
// found, and a counter's correctness must not inherit an unexplained defect.
func ShardFor(entropy uint64, numShards int) int {
	if numShards <= 1 {
		return 0
	}
	return int(mix64(entropy) % uint64(numShards))
}

// mix64 is the splitmix64 finalizer: three xorshift/multiply rounds that
// avalanche every input bit across the whole output word.
func mix64(x uint64) uint64 {
	x ^= x >> 30
	x *= 0xbf58476d1ce4e5b9
	x ^= x >> 27
	x *= 0x94d049bb133111eb
	x ^= x >> 31
	return x
}
```

**`redirect/main.go`** — the entropy source lives in `package main` (it needs
`math/rand/v2` and the clock; `linkgate` stays pure and host-testable):

```go
// clickEntropy produces the 64-bit value linkgate.ShardFor reduces to a shard
// index. Two independent sources are XORed deliberately:
//
//   - math/rand/v2's global source, which advances on every call and so varies
//     within one Wasm instance even if its seed does not.
//   - the raw nanosecond clock, which varies across instances even if WASI's
//     random_get is stubbed and every instance seeds identically.
//
// Each source has a plausible failure mode on this host that the other covers.
// If the RNG seeded identically per instance AND Akamai created one instance
// per request — both plausible, neither confirmed — a rand-only shard would
// send every instance's first click to the same shard, which is worse than not
// sharding at all.
func clickEntropy(now time.Time) uint64 {
	return uint64(now.UnixNano()) ^ rand.Uint64()
}
```

and inside `recordClickCount`, replacing the `countKey` line only:

```go
	shard := linkgate.ShardFor(clickEntropy(now), linkgate.CountShards)
	countKey := linkgate.CountShardKey(slug, shard)
```

The long comment block above `recordClickCount` must be rewritten. Its current
text says the ~15% loss "needs a different counter shape, not a different
order" and defers to `TASKS.md`; that is now what the function does. The
replacement must name **both** mechanisms — that sharding removes per-key
contention within the reachable envelope, and that the residual loss above
~25 clicks/second app-wide is the write cap and is not something this function
can do anything about.

**Go tests (`redirect/linkgate/`, host-testable, `go test ./linkgate/...`):**

- `keys_test.go`: `CountShardKey("abc", 3) == "analytics:count:abc:3"`;
  `CountShardKey("abc", 0) == "analytics:count:abc:0"`. Replace `TestCountKey`.
- `analytics_test.go`: `ShardFor(x, 0)` and `ShardFor(x, 1)` return `0`;
  `ShardFor` over 100,000 draws puts each of 16 shards within ±10% of `1/16`;
  and — the regression that matters — **`ShardFor` fed only timestamp-shaped
  input** (a base nanosecond value plus `i * 1_000_000` for `i` in
  `[0, 100000)`, i.e. clicks exactly 1 ms apart) still hits all 16 shards
  within ±10% of uniform. That is the exact input pattern `EventSlot` is
  documented to handle badly; pinning it is what stops this design inheriting
  that defect. `EventSlot`'s own existing tests stay untouched.

### API (Python) changes

**`api/analytics.py`** — add the constants and the merge, and read 17 keys:

```python
# MUST stay equal to redirect/linkgate/keys.go's CountShards — see that file
# for the full rule. Lowering this silently drops every click that was
# recorded into a higher shard. api/tests/test_kvprefix.py pins the equality.
COUNT_SHARDS = 16
```

`handle_analytics` reads `f"count:{slug}"` (the legacy key, may be absent) and
`f"count:{slug}:{n}"` for `n in range(COUNT_SHARDS)`, and merges them with a
new pure helper:

```python
def _merge_counts(blobs) -> tuple[int, dict[str, int]]:
    """Sum shard blobs into one {total, days}. A blob that is absent, empty,
    not JSON, or not an object contributes nothing rather than raising — one
    corrupt shard must never blank out a link's whole history."""
```

Everything else in the handler — `get_link`, `can_view`, the event-slot loop,
the response shape — is unchanged. **The response body's keys and shape do not
change**, so `gui/links/detail.js` needs no edit.

**Merged `days` overshoot, accepted and documented, not fixed.** Each shard
trims its own `days` map to `analytics_day_retention_days` (default 90)
independently, so for a *low-traffic* link whose shards collected clicks on
different days, the merged map can hold more than 90 entries — up to 16 × 90 in
the pathological case of a link clicked a handful of times a year for a decade.
Nothing is wrong with the data and every stored value stays well inside the
1 MB value cap; the response is just longer than the retention window nominally
promises. Trimming at read time would require declaring
`analytics_day_retention_days` for the `api` component in `spin.toml` (it is
currently declared for `redirect` only, `spin.toml:29` vs `44-51`) — a manifest
change and a new variable read on every analytics request, to bound a response
that is at most a few kilobytes. Not worth it. For a link with enough traffic to
matter, every shard sees clicks on the same days and the union is the retention
window.

**Python tests (`api/tests/test_analytics.py`):**

- counts written to shards `0`, `7` and `15` sum into one `total` and one
  merged `days` map (the same day in two shards adds);
- a legacy `count:<slug>` value **plus** shard values sum together;
- an unparseable shard blob is skipped and the other shards still total
  correctly;
- `test_analytics_reports_count_and_events` (which writes the legacy
  `count:{slug}` key) **must keep passing unmodified** — it is now the
  migration regression test, and that is worth a comment in the file.

**`api/tests/test_kvprefix.py`** — extend the existing
`# --- Cross-language drift guard ---` section with a test that reads
`redirect/linkgate/keys.go`, regex-extracts `CountShards\s*=\s*(\d+)`, and
asserts it equals `analytics.COUNT_SHARDS`. Same file, same technique, same
`parents[2]` path as the prefix guard.

### GUI changes

**`gui/links/detail.html`** — one `<small>` after the Total clicks line
(`detail.html:50`), reusing the `#bulk-format-hint` pattern from
`dashboard.html:70`. No new CSS class, no new design token, no `DESIGN.md`
change, no `detail.js` change.

The wording must describe **M2**, since M1 is gone within the reachable
envelope — and it must be an app-wide statement, not a per-link one, because
the write cap is app-wide:

> Recorded best-effort. Accurate while the whole service stays under roughly
> 25 clicks per second; heavier traffic under-counts.

Do not word this per-link — "25 clicks/second on this link" would be wrong in
the common case of many links sharing the cap.

### Tooling

**`dev/click-load.sh` (new)** — the verification needs paced,
slug-round-robining load, and there is no such script today (`ls dev/` →
`kv-explorer-up.sh`, `kv-explorer.toml`):

```
./dev/click-load.sh <base-url> <rate-per-second> <count> <slug> [slug...]
```

Issues `<count>` GETs to `<base-url>/r/<slug>`, round-robining the slugs, paced
to approximately `<rate-per-second>`, printing the per-slug request count and
the number of non-302 responses.

**It must also print the implied app write rate (`rate × 2`) and warn loudly
when that reaches 50/s.** That single line is the guard against the exact
confound that produced the probe's false negative — anyone running this at 34/s
is measuring the write cap and will misread the result. Cheap to add, and it
encodes the one thing about this measurement that is easy to get wrong.

It deliberately does **not** read the totals back; that needs an authenticated
session, and the operator is already looking at the link detail page.

## The recent-events ring buffer

`EventSlot` and the `analytics:events:<slug>:<slot>` write are **untouched by
this plan**, and the optional task that would have replaced `EventSlot` with
random slot selection is dropped. Two reasons:

1. Deleting it would orphan the open `TASKS.md` investigation *"Find the real
   cause of the analytics recent-events slot collisions"*, which is now more
   interesting rather than less. **M2 is a plausible additional contributor on
   the deployment**: an events write that is throttled is an event that never
   lands, which looks identical to a slot collision from the read side.
2. Being precise about what that does and does not explain: **M2 cannot explain
   the original observation**, which was made *locally* (8 requests 300 ms apart
   retaining 3 distinct events under `spin up`), and local sqlite has no write
   cap. So `EventSlot`'s hashing remains a live suspect for the local case, and
   whoever picks that investigation up now has two candidate causes to separate
   — which is exactly why the investigation should keep its subject alive.

## Trade-offs and rejected alternatives

**1. Append-only click keys (one unique key per click, no read).** Attractive
because it is *exact* for M1: with no read there is no update to lose, at any
rate. Rejected because the read side is unbounded in a way this app cannot
absorb — and independently verified while reviewing this plan:

- Spin's KV enumerates the **whole physical store** (`kv.Store.GetKeys` takes
  no prefix argument); there is no prefix scan. Counting one slug's clicks
  would mean enumerating every key in the app and filtering, on a host where
  each data operation costs 5.5–16.7 ms against a 30 s handler limit.
- `api/backup.py`'s export already enumerates and reads every analytics key
  against `MAX_BACKUP_ENTRIES = 5_000` (`api/backup.py:32`), and
  `BACKUP_STORES` includes `"analytics"` (line 26). Today's ~31 analytics keys
  per slug already put that ceiling near 160 links; a single popular link's
  click keys would break backup for the **whole application**, not just for
  analytics.
- Compaction answers both and this app has no trigger to run it from — Akamai
  Functions supports neither cron nor custom triggers — so it would be an admin
  button someone remembers to press, or a GET that writes and races another
  concurrent reader into double-counting.
- It would also make **M2 strictly worse per click** if it replaced the
  read-modify-write with a write plus a periodic compaction write.

It trades a bounded, measured, quantified inaccuracy for an unbounded,
unquantified operational failure.

**2. Do nothing; accept and document the real number.** This was live. Rejected
because before this change the honest per-link ceiling (M1, ~3 clicks/second)
sat *below* the app's own throughput ceiling (M2, ~25 clicks/second), so the
product's headline analytics number was wrong before any other limit in the
system bound. The caveat text from this option is kept anyway, on the detail
page — it now describes M2 instead of M1.

**3. 8 shards instead of 16.** Attractive: half the extra read cost (39 vs 47
KV ops on the detail page) and half the extra backup entries per slug. Rejected
on the user's decision and on the evidence: at the app-wide ceiling a single
hot link would see ~3.1 clicks/second per shard, which is *past* the 2.7/s
clean band with no direct measurement behind it, whereas 16 gives ~1.6/s
against a probe that measured 0% at 1.2/s per key. Reads are also the cheap
operation here — 1,000 read RPS against 50 write RPS — and the extra reads land
on a single-link admin page that already spends 31 KV operations, never on the
hot path.

**4. Making the shard count a Spin variable** (as `analytics_event_slots` is).
Attractive because CLAUDE.md's own rule is that a value two components must
agree on becomes a variable. Rejected because the failure mode differs in kind:
a mismatched `analytics_event_slots` produces a lossier *sample*, whereas a
shard count the reader sets lower than the writer **silently deletes click
history from every total**, with no error anywhere. That is the failure shape
the KV prefixes have, and the repo's answer to it is a constant per language
pinned by a cross-language test, not a knob.

**5. Instance-sticky shard selection.** Attractive under a stale-read model:
an instance always reads back its own last write. Rejected — if the staleness
lives in the KV backend rather than a per-instance cache, stickiness buys
nothing; and if Akamai creates one instance per request (plausible,
unconfirmed), sticky selection *is* per-request selection, with the added
hazard that two long-lived instances drawing the same shard are welded to it
permanently instead of colliding 1-in-16 of the time.

**6. Read-after-write verification / retry.** Rejected: it doubles the
counter's KV operations on the hot path, adds a write per retry directly
against the M2 cap that is now the binding constraint, and cannot distinguish
"my write was lost" from "my read is stale".

**7. In-instance batching** (accumulate clicks in a package-level counter,
flush every K clicks or T ms). This is the one alternative that would attack
**M2** as well as M1, which makes it more attractive now than when this plan
was first written. Still rejected: a Wasm instance on a FaaS can be torn down
at any moment with no shutdown hook, so every unflushed click is lost outright
and silently, and the loss is *unbounded* rather than rate-dependent. It also
reintroduces the package-level shared state
`docs/plans/toggleable-logging.md` deliberately designed out of this component.
Revisit only if Spin/WASI exposes a real instance-shutdown hook.

**8. Splitting `{total, days}` into separate keys.** Rejected: it makes every
click two counter writes instead of one, halving the M2 ceiling from ~25 to
~17 clicks/second, and does nothing about the read-modify-write on either key.

**9. A deploy-instrumented step to confirm the underlying mechanism** (write a
sentinel, immediately re-read the same key, log whether the read was stale).
Rejected as the gating step, and vindicated: the *behavioural* probe — per-key
versus app-wide — settled the design question in an afternoon with `curl` and
sixteen extra links, no build and no deploy. It also surfaced M2 as a separate
effect, which a sentinel test would not have. Whether the M1 mechanism is
replica lag or read-modify-write overlap is still unknown and still does not
change the design.

## Tasks

Two blocks in `TASKS.md`, both appended. The second supersedes parts of the
first — `TASKS.md` is authoritative and carries the same note.

Original section, `## Click-count accuracy (sharded analytics counters)`:

```
- [ ] Add a paced click-load script for measuring loss against the deployed app — file(s): dev/click-load.sh — done when: `./dev/click-load.sh <base-url> <rate-per-second> <count> <slug> [slug...]` issues `count` GETs to `<base-url>/r/<slug>` round-robining the slugs at approximately the requested rate, prints per-slug request counts and the number of non-302 responses, and exits non-zero if any request failed to connect
- [ ] Probe whether click-count loss is per-key or app-wide (BLOCKS every sharding task below) — file(s): (none — live measurement on the deployed app) — done when: three conditions have each been run with 100 requests and recorded in docs/plans/click-count-accuracy-scratch.md — (a) one fresh slug at 34/s, expected ~70% loss as the control, (b) sixteen fresh slugs round-robined at 34/s aggregate, (c) sixteen fresh slugs round-robined at 65/s aggregate; if (b) loses as much as (a), sharding does not work on this host: stop, implement nothing below, and re-plan against the "If the probe fails" section of docs/plans/click-count-accuracy.md
- [ ] Add CountShards, CountShardKey and ShardFor to linkgate, with distribution tests — file(s): redirect/linkgate/keys.go, redirect/linkgate/analytics.go, redirect/linkgate/keys_test.go, redirect/linkgate/analytics_test.go — done when: `cd redirect && go test ./linkgate/...` passes with new tests asserting CountShardKey("abc", 3) == "analytics:count:abc:3", ShardFor(x, 0) and ShardFor(x, 1) return 0, ShardFor over 100000 draws puts each of the 16 shards within 10% of uniform, ShardFor over timestamp-shaped input 1 ms apart also puts each of the 16 shards within 10% of uniform, and the old CountKey function and TestCountKey are gone
- [ ] Write the sharded count key from the redirect hot path — file(s): redirect/main.go — done when: recordClickCount reads and writes linkgate.CountShardKey(slug, linkgate.ShardFor(clickEntropy(now), linkgate.CountShards)), clickEntropy XORs math/rand/v2's rand.Uint64() with now.UnixNano(), the function's comment block no longer says the loss "needs a different counter shape", `go tool componentize-go build` from redirect/ succeeds, and an X-SS-Debug-traced redirect against a local `spin up` still reports kv_ops=6
- [ ] Sum the shards plus the legacy key on the analytics read path — file(s): api/analytics.py, api/tests/test_analytics.py — done when: `cd api && uv run pytest` passes with new tests showing counts in shards 0, 7 and 15 summing into one total and one merged days map, a legacy `count:<slug>` value summing in alongside shard values, and an unparseable shard blob being skipped without blanking the total — and test_analytics_reports_count_and_events still passes with its assertions unmodified
- [ ] Pin the Go and Python shard counts against each other — file(s): api/tests/test_kvprefix.py — done when: a test under the existing "Cross-language drift guard" heading reads redirect/linkgate/keys.go, extracts CountShards, asserts it equals analytics.COUNT_SHARDS, and fails when either side is edited alone (verified by temporarily changing one)
- [ ] State on the link detail page what the click counter does and does not guarantee — file(s): gui/links/detail.html — done when: a `<small>` element follows the Total clicks line naming the approximate per-link click rate above which clicks are under-counted, `cd gui-pages && uv run pytest` still passes, and no new CSS class or design token was introduced
- [ ] Verify the sharded counter against the original loss curve on the deployed app (requires a deploy) — file(s): (none — verification step) — done when: after deploying the sharded build, 100 requests to a fresh link at each of 9.4/s, 34/s and 65/s each record within 2 of 100 on the link detail page (against 75, 30 and 25 recorded before the change), an X-SS-Debug-traced redirect still reports kv_ops=6, and an X-SS-Debug-traced GET /api/links/{slug}/analytics shows its get count risen by exactly 16 over a pre-change trace of the same page
- [ ] Document the sharded counter and correct every stale accuracy claim — file(s): CLAUDE.md — done when: the Analytics section describes the shard key shape, the legacy-key sum, the raise-only-never-lower rule and the newly measured curve; the Security-tradeoffs click-count bullet no longer says ~15%; the "~25 redirects/second" ceiling entry no longer says the counter makes it academic; the "KV store: the single default store" section lists CountShards alongside the prefixes as a value that must stay identical across the two languages; and the detail page's hardcoded rate figure is named as something that must change with CountShards
- [ ] OPTIONAL, land only after the counter verification above has passed: choose the recent-events slot at random rather than from the timestamp — file(s): redirect/main.go, redirect/linkgate/analytics.go, redirect/linkgate/analytics_test.go — done when: recordClickEvent picks its slot with linkgate.ShardFor(clickEntropy(now), numSlots), EventSlot and its tests are removed, `cd redirect && go test ./linkgate/...` passes, and 20 clicks 300 ms apart against a local `spin up` retain 12 or more distinct recent events on the detail page (against 3 of 8 measured before)
- [ ] End-to-end manual verification of click-count accuracy — file(s): (none — verification step) — done when: on a local `spin up --build`, a fresh link clicked 10 times reports exactly 10 on its detail page; a `count:<slug>` key seeded to {"total": 5, "days": {"2026-01-01": 5}} through dev/kv-explorer-up.sh and then clicked 3 more times reports a total of 8 with both days present; and the detail page's new caveat text renders
```

Corrections section,
`## Click-count accuracy — corrections after the live probe (2026-08-06)`:

```
- [ ] Make dev/click-load.sh refuse to silently measure the wrong mechanism (SUPERSEDES the script task above; do that one to this spec) — file(s): dev/click-load.sh — done when: in addition to the original spec, the script prints the implied app write rate (`rate-per-second` × 2, since every click is two KV writes) and prints a loud warning when that value reaches 50, naming Akamai's 50 write RPS cap and that any loss measured above it is throttling rather than counter contention
- [ ] Correct the link detail page's caveat to describe the app-wide write cap, not a per-link rate (SUPERSEDES the detail-page task above) — file(s): gui/links/detail.html — done when: the `<small>` following Total clicks states the limit as roughly 25 clicks per second **across the whole service** rather than per link, `cd gui-pages && uv run pytest` still passes, and no new CSS class or design token was introduced
- [ ] Verify the sharded counter on the deployed app, under the write cap (SUPERSEDES the deployed-verification task above — do NOT use its 34/s and 65/s conditions) — file(s): (none — verification step, requires a deploy) — done when: after deploying the sharded build, 100 requests to a fresh link at 9.4/s (18.8 writes/s) records 98–100 against the 75 measured pre-change at the same rate, 100 requests to a second fresh link at 19.7/s (39.4 writes/s) also records 98–100, an X-SS-Debug-traced redirect still reports kv_ops=6, and an X-SS-Debug-traced GET /api/links/{slug}/analytics shows its get count risen by exactly 16 over a pre-change trace of the same page
- [ ] Document both mechanisms, not one, and retract the "academic" framing (SUPERSEDES the CLAUDE.md task above) — file(s): CLAUDE.md — done when: the Analytics section names per-key contention and the app-wide 50-write-RPS cap as two separate effects, describes the shard key shape, the legacy-key sum and the raise-only-never-lower rule, and records that 16 shards put a single link's per-shard rate at ~1.6/s at the app-wide ceiling against 0% loss measured at 1.2/s per key; the Security-tradeoffs click-count bullet no longer says ~15%; **the "~25 redirects/second" ceiling is described as the real and now sole binding constraint, never as academic**; the "KV store: the single default store" section lists CountShards alongside the prefixes; and any measurement guidance states that a run above ~25 requests/second measures the write cap rather than counter accuracy
- [ ] Delete the probe's test links from the deployed app when this work closes — file(s): (none — cleanup on the deployed app) — done when: slug Xu0CtDs and the 16 probe slugs have been deleted through the dashboard's bulk delete, and the dashboard shows no leftover load-test links
```

**Dropped, not superseded:** the `OPTIONAL ... choose the recent-events slot at
random` line above is **not to be actioned**. `EventSlot` stays. See "The
recent-events ring buffer"; a dated entry recording the decision is under
`TASKS.md`'s "Considered and rejected".

**Already satisfied:** the probe line above was run on 2026-08-06; its
corrected result is in "Probe result". It is not builder work.

## Critical files

- `dev/click-load.sh` (new)
- `redirect/linkgate/keys.go`
- `redirect/linkgate/keys_test.go`
- `redirect/linkgate/analytics.go`
- `redirect/linkgate/analytics_test.go`
- `redirect/main.go`
- `api/analytics.py`
- `api/tests/test_analytics.py`
- `api/tests/test_kvprefix.py`
- `gui/links/detail.html`
- `CLAUDE.md`
- `TASKS.md`
- `docs/plans/click-count-accuracy-scratch.md` (new, gitignored — probe results)

Not touched, deliberately: `api/backup.py`, `api/consistency.py`,
`api/kvprefix.py`, `spin.toml`, `Jenkinsfile` (CI runs the same three test
commands, unchanged), `DESIGN.md`, `gui/links/detail.js`, and
`linkgate.EventSlot` with its tests.

## Verification

Run in this order.

1. **Baseline, before any change** (confirmed while planning; re-run if time
   has passed):
   ```bash
   cd redirect && go test ./linkgate/...     # ok
   cd api && uv run pytest                    # 508 passed
   cd gui-pages && uv run pytest
   ```

2. **Unit suites, after the Go and Python changes:**
   ```bash
   cd redirect && go test ./linkgate/...
   cd api && uv run pytest
   cd gui-pages && uv run pytest
   ```
   The distribution tests are the ones that matter — a `ShardFor` that
   concentrates on one shard passes every functional test and fixes nothing.

3. **Local end-to-end** (proves *correctness*, not accuracy — local sqlite
   loses nothing and has no write cap, so a local run can never reproduce
   either mechanism):
   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```
   - Create a link, hit `/r/<slug>` 10 times, open its detail page: **Total
     clicks: 10**, per-day table shows 10 for today.
   - Trace one redirect and confirm the hot path did not grow:
     ```bash
     curl -sD- -o/dev/null -H 'X-SS-Debug: <token>' http://localhost:3000/r/<slug>
     ```
     with `SPIN_VARIABLE_LOG_DEBUG_TOKEN=<token>` on the `spin up`. The stderr
     line must still read `kv_ops=6` with `get=2/... set=2/...`. **Two writes
     per click is the M2 arithmetic; if this ever becomes three, the app-wide
     ceiling drops to ~17 clicks/second.**
   - Trace the analytics page before and after the change and diff the `get`
     count: it must rise by exactly 16.
   - The caveat `<small>` renders under Total clicks and reads as an app-wide
     statement, not a per-link one.

4. **Legacy-key migration, locally**, using the dev KV explorer:
   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> \
   SPIN_VARIABLE_KV_EXPLORER_PASSWORD=<explorer-pw> \
   SPIN_VARIABLE_COOKIE_SECURE=false \
     ./dev/kv-explorer-up.sh
   ```
   Set `analytics:count:<slug>` to `{"total": 5, "days": {"2026-01-01": 5}}`,
   then click the link 3 more times. The detail page must show **Total clicks:
   8** with both `2026-01-01` (5) and today (3) in the per-day table. That is
   the whole migration story; if it works, no deployment needs anything done
   to it.

5. **The real verification, on the deployed app — and it must stay under the
   write cap.** Deploy, then on two fresh links:
   ```bash
   ./dev/click-load.sh https://<app-id>.fwf.app 9.4  100 <slug-a>   # 18.8 writes/s
   ./dev/click-load.sh https://<app-id>.fwf.app 19.7 100 <slug-b>   # 39.4 writes/s
   ```
   **Pass:** each detail page reads 98–100. The anchor comparison is `slug-a`:
   the identical 9.4/s condition measured **25% loss (75/100)** before this
   change, at the same app-wide write load, so a clean run there is a direct
   before/after on M1. `slug-b` at 19.7/s is the highest rate that still leaves
   headroom under the 50 write RPS cap.

   **Do NOT run this at 34/s or 65/s.** Those are 68 and 130 writes/second —
   the confound that produced the probe's false negative. A perfectly working
   sharded build loses ~32% at 38.5 req/s (measured), and reading that as a
   failed fix is the single most likely way to misjudge this work.

   Then trace one deployed redirect with `X-SS-Debug` and confirm `kv_ops=6`.

6. **Optional, to characterise M2 rather than verify M1:** run the sharded
   build at 38.5 req/s on 16 slugs and confirm the loss is close to the 32.5%
   the unsharded probe measured at the same rate. Equal loss is the *expected*
   result and confirms that what remains above the cap is throttling, not
   contention. This is a diagnosis, not a pass/fail gate.

7. **Cleanup:** delete slug `Xu0CtDs` and the 16 probe slugs from the deployed
   app when the work closes.

## What this does NOT fix

Plainly, with numbers, and labelled by mechanism:

- **M1 — per-key contention: eliminated within the app's reachable operating
  envelope, not made impossible.** There is still no compare-and-swap and still
  a read-modify-write. The claim is that at the highest rate the app can serve
  (~25 clicks/second app-wide, M2), even a single link taking *all* of it sees
  only ~1.6 clicks/second per shard, and 0% loss was measured at 1.2/s per key.
  If Akamai ever raises the write cap, M1 comes back into range and the shard
  count must be revisited — **raise only, never lower**.
- **M2 — app-wide write throttling: untouched.** Two KV writes per click
  against a 50 write RPS cap is ~25 clicks/second **for the whole
  application**, all links combined. Above that, clicks are lost regardless of
  sharding: measured at **32.5% loss at 38.5 requests/second** on sixteen
  separate keys, where per-key contention cannot be the explanation. A single
  link taking more than ~25 clicks/second is lossy and sharding will not save
  it.
- **The "0%" readings mean "below the measurement floor."** 20 samples cannot
  resolve a loss rate under ~5%; 100 samples cannot resolve one under ~1%.
  Nothing here establishes zero.
- **Clicks already lost are lost.** Nothing reconstructs history.
- **The recent-events ring buffer is unchanged** and remains a best-effort
  sample, now with two candidate causes rather than one.
- **The mechanism behind M1 remains unconfirmed** — replica lag versus
  read-modify-write overlap. The design works under either, which is why it is
  safe to ship without knowing, but "we fixed the counter" is not "we
  understand the store".

## Out of scope / follow-ups

- **Reducing writes per click from two to one — now the highest-value
  follow-up, because M2 is the sole binding constraint.** The arithmetic: a
  click writes `analytics:count:<slug>:<n>` and
  `analytics:events:<slug>:<slot>`, so 50 write RPS ÷ 2 ≈ **25 clicks/second**.
  Dropping the events write entirely gives 50 ÷ 1 ≈ **50 clicks/second** — a
  straight doubling of the app's ceiling, a bigger win than sharding for a busy
  deployment, and it costs a feature that is already documented as a lossy
  best-effort sample. Sampling instead of dropping is the obvious middle
  ground (write the event 1 time in 4 → 1.25 writes/click → ~40 clicks/second)
  but has a real flaw worth stating before anyone reaches for it:
  unconditional sampling thins the recent-events list most for *low-traffic*
  links, which are exactly the links where those events are worth reading. A
  rate-adaptive sample ("always write below some rate") needs to know the rate,
  which needs state, which needs a read — circular. **Belongs under `TASKS.md`'s
  "Future work (not scheduled)"; added there by this plan.** Trigger: sustained
  traffic approaching ~25 clicks/second, or an Akamai write-cap increase being
  refused.
- **Asking Akamai for a write-rate increase** — the existing Future-work entry.
  This work makes it *more* relevant, not less: it is now the only lever left on
  the binding constraint besides writing fewer keys.
- **Folding the legacy `analytics:count:<slug>` key into shard 0 and deleting
  it**, to get the read path from 17 counter gets back to 16. Safe (the legacy
  key is never written again, so a one-shot fold has nothing to race) but worth
  almost nothing. Not added.
- **`MAX_BACKUP_ENTRIES` versus analytics keys.** Sharding raises the analytics
  key count per slug from ~31 to ~47, moving the practical full-backup ceiling
  from roughly 160 links to roughly 106. Backup with analytics included is
  *already* impractical past ~150 links and the documented workaround is
  `?stores=links,users`; this makes an existing problem modestly worse and does
  not create a new one. The existing "Chunked or resumable backup/restore"
  Future-work entry is where that gets solved. On the record, not fixed here.
- **`TASKS.md`'s "Find the real cause of the analytics recent-events slot
  collisions"** stays open and gains a second candidate cause (M2 on the
  deployment, `EventSlot` hashing locally). Nobody edits that line without being
  asked; the extra context lives in this plan's "The recent-events ring buffer".
- **A deploy-instrumented confirmation of the M1 mechanism** — write a
  sentinel, immediately re-read the same key, log whether the read was stale.
  Trigger: step 5 failing in a way the per-key model does not explain.
