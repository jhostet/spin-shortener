# Click-Count Accuracy on Akamai

## Context

`analytics:count:<slug>` is a read-modify-write (`recordClickCount` in
`redirect/main.go` → `linkgate.UpdateCount`), and Spin's KV has no
compare-and-swap. On the live Akamai deployment it loses increments, and the
loss was measured on 2026-08-06 against a fresh link, 20 requests per
condition:

| per-link request rate | recorded loss |
|---|---|
| 0.5 /s | 0% |
| 0.9 /s | 0% |
| 1.6 /s | 0% |
| 2.7 /s | 0% |
| **9.4 /s** | **25%** |
| 34 /s | 70% |
| 65 /s | 75% |

It is a cliff, not a gradient — zero losses across 80 requests at or below
2.7/s. The losses are permanent, not lag: a 25-request burst recorded 19 and
was still 19 at t+60 s. Locally (sqlite) the identical test records 20/20, so
this does not reproduce off the deployment at all.

The practical statement today: **click analytics is trustworthy below ~3
clicks/second on one link and unusable above ~9.** That also makes the
"~25 redirects/second write-RPS ceiling" recorded in `TASKS.md` academic — at
25/s roughly 70% of a single link's clicks are already lost, long before any
write cap binds.

Motivating `TASKS.md` Future-work entry: *"Fix Akamai click under-counting —
`analytics:count` loses ~15% under rapid sequential requests"* (raised
2026-08-06), which now carries the full curve above and the instruction
**"The loss curve has now been measured — do that no more; design the fix."**
Related entries this plan touches: *"Find the real cause of the analytics
recent-events slot collisions"* and *"Ask Akamai for a KV write-rate increase
if redirect throughput demands it"*.

**Confirmed decisions (settled by the user before planning):**

- The loss curve above is the measurement of record. Do not re-measure it to
  characterise the problem; measure only to verify a fix.
- Any fix must be verified **against that same curve, on a fresh link, on the
  deployed app**, at minimum at 9.4/s and 34/s. A local-only verification
  section is worthless here.
- The plan must state explicitly what the fix does **not** fix, with a number
  rather than an implied exactness.
- The three candidate directions on the table were sharded counters,
  append-only click keys, and accept-and-document. The list was explicitly
  not closed.
- Whether confirming the mechanism deserves a step of its own was left to the
  planner. **It does — see "The probe" below — but not the step that was
  implied.**

## Key technical facts confirmed during research

- **`recordClickCount` is a `Get` then a `Set` on one key**, and the key is
  built by `linkgate.CountKey(slug)` = `"analytics:count:" + slug`
  (`redirect/main.go:389-398`, `redirect/linkgate/keys.go:18`). `CountKey` has
  exactly one caller (`grep -rn CountKey redirect api` → `main.go:393`, the
  definition, and `keys_test.go`).
- **All analytics already runs after the response**, in
  `sendRedirectThenRecord` (`redirect/main.go:354-372`). Hot-path *latency* is
  therefore not a constraint on this work; hot-path *write RPS* still is.
- **A successful redirect is 6 KV operations today** (2 `open`, 2 `get`,
  2 `set`) — CLAUDE.md "Toggleable structured logging", and the `X-SS-Debug`
  trace format `kv_ops=6`. Sharding as designed below leaves this at 6.
- **The analytics read path is `api/analytics.py:handle_analytics`**, which
  does one `get` for `count:{slug}` and `num_event_slots` (default 30) gets for
  `events:{slug}:{slot}` — plus one `get_link` on the links view. It is the
  only reader of the counter: `grep -rn "count:\|analytics" api/links.py
  gui/dashboard.js` returns nothing, so the dashboard never reads click
  totals and the read-cost change is confined to `gui/links/detail.html`.
- **A slug can never contain a colon.** `api/links.py:21`
  `CUSTOM_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,32}$")`, and generated
  slugs are `secrets.choice(string.ascii_letters + string.digits)` (`links.py`
  lines 17-27). So `count:<slug>` and `count:<slug>:<n>` are unambiguous key
  shapes that can never collide.
- **Of the three obligations a new KV key type imposes (CLAUDE.md, "KV
  consistency check"), only one applies here, and it is already satisfied.**
  Verified by reading the code, not by assuming:
  - `api/kvprefix.py:STORE_PREFIXES` — the new key lives under the existing
    `analytics:` prefix. Nothing to add.
  - `api/consistency.py` — never opens or scans the `analytics` store
    (`grep -n analytics api/consistency.py` → no matches; `api/app.py:275-277`
    comments that the analytics view is deliberately not handed to it).
    **No change needed.**
  - `api/backup.py` — `INDEX_KEYS["analytics"] = ()` (line 40) and
    `is_excluded_key` returns `False` for every store but `users` (line 87).
    Export enumerates the analytics view generically via `list_keys` and
    restore prunes generically, so a new `analytics:` key shape round-trips
    with **no code change**. Confirmed by reading `build_backup`,
    `validate_backup`, `restore_write_order` and `handle_restore`
    (`api/backup.py:92-310`).
  - The one real consequence for backup is a **quota** one, not a code one —
    see "Backup entry count" under Trade-offs.
- **`math/rand/v2` is available**: `go doc math/rand/v2 Uint64` and
  `go doc math/rand/v2 IntN` both resolve under this module (`redirect/go.mod`
  declares `go 1.25.5`).
- **Baseline is green**: `cd redirect && go test ./linkgate/...` → `ok`;
  `cd api && uv run pytest -q` → `508 passed`.
- **`analytics_day_retention_days` is declared for `redirect` only**
  (`spin.toml:29`), not for `api` (`spin.toml:44-51`). The read path therefore
  has no retention value available without a manifest change — which this plan
  deliberately does not make; see "Merged `days` overshoot".
- **The cross-language pin technique already exists**:
  `api/tests/test_kvprefix.py:94-110`, under a `# --- Cross-language drift
  guard ---` heading, reads `redirect/linkgate/keys.go` with
  `Path(__file__).resolve().parents[2]` and regex-extracts the constants.
- **`<small>` is the established hint element** in this GUI
  (`gui/dashboard.html:70`, `#bulk-format-hint`), styled by Pico with no
  project CSS class. A caveat line on the detail page therefore needs no new
  token and no `DESIGN.md` change.
- **The mechanism is read staleness, not read-modify-write overlap —
  strongly inferred, still UNCONFIRMED.** Two pieces of existing evidence
  point the same way and neither is new measurement:
  1. Before commit `6f2634f`, `recordClickCount` ran **before** the response
     was sent. A strictly sequential client (a `curl` loop) therefore could not
     possibly overlap two counter updates — request N's `Set` completed before
     N's response left the handler, and N+1 had not been issued. That arm of
     the A/B still lost ~15% (85/100). A pure RMW-overlap model predicts 0%
     there.
  2. The vulnerable window under an RMW-overlap model is the gap between the
     `Get` and the `Set`, which is one KV operation, measured at 5.5–16.7 ms.
     The observed cliff sits between 106 ms (9.4/s) and 370 ms (2.7/s) of
     request spacing — one to two orders of magnitude wider.
  A propagation window on the order of 100–370 ms fits both. **What would
  confirm it:** a deployed build that writes a unique value, immediately
  re-reads the same key, and logs whether the read returned what it just
  wrote. That build is not proposed here — see "The probe" for why the
  *behavioural* question is the one that actually gates the design.
- **UNCONFIRMED: whether `math/rand/v2`'s global source is seeded from real
  entropy under `componentize-go`/wasip1.** Go seeds it from
  `runtime.readRandom`, which on wasip1 uses the `random_get` syscall; whether
  Spin/wasmtime backs that with `wasi:random` in a component built this way has
  not been checked. The design below makes this not matter — see
  `clickEntropy` — but if it is ever confirmed, the XOR with the clock could be
  simplified away.
- **UNCONFIRMED: whether Akamai Functions creates one Wasm instance per
  request.** This is why shard selection is per-request rather than
  per-instance; see the rejected "instance-sticky shard" alternative.

## The probe (do this before writing any counter code)

The whole sharding design rests on one property: **that contention is
per-key.** If a stale-read window is a property of a *key*, spreading writes
over N keys divides the collision probability by N. If it is a property of the
*app* or the *store* — a global write pipeline, a per-instance cache flush —
sharding buys nothing at all and the answer changes to append-only or
accept-and-document.

That property is directly measurable **on the existing deployment with no code
change at all**, because N distinct slugs are indistinguishable from N shards
of one slug as far as the KV store is concerned: N different keys in the
`analytics:` namespace, written by the same app, at the same aggregate rate.

Three conditions, 100 requests each (100, not 20 — a 20-sample run cannot
resolve a loss rate below ~5%, so every "0%" in the table above really means
"under 5%"):

| # | condition | expectation if contention is per-key | expectation if app-wide |
|---|---|---|---|
| a | 1 fresh slug at 34/s | ~70% loss (reproduces the curve) | ~70% loss |
| b | 16 fresh slugs, round-robin, 34/s aggregate (2.1/s each) | ~0% loss on each | ~70% loss on each |
| c | 16 fresh slugs, round-robin, 65/s aggregate (4.1/s each) | ~0% loss on each | ~75% loss on each |

**(a) is the control and must reproduce**, or the deployment has changed
underneath the measurement and nothing else in this section means anything.

**If (b) and (c) come back clean, sharding is confirmed to work on this host**
and the rest of this plan proceeds unchanged.

**If (b) loses as much as (a), stop.** Sharding is dead; do not implement it.
Fall through to "If the probe fails" below.

This is deliberately *not* a step that confirms the underlying mechanism. The
mechanism is interesting and is inferred above, but the design does not depend
on knowing it — it depends only on whether the loss is per-key, and that is
one afternoon of `curl` against a live app rather than a deploy-instrument-
deploy cycle.

## Design: 16-way sharded counters

### Data model

| key | written by | read by | note |
|---|---|---|---|
| `analytics:count:<slug>:<n>`, `n` in `[0, 16)` | `redirect` | `api` | new; one `{total, days}` blob per shard, identical in shape to today's |
| `analytics:count:<slug>` | nothing, ever again | `api` | the pre-sharding key, summed in so upgrades keep their history |
| `analytics:events:<slug>:<slot>` | `redirect` | `api` | unchanged |

The write path stays **one `Get` + one `Set`** — the same two operations on a
differently-named key. **No new hot-path KV operation, no new latency, no
change to write RPS.** That is the single strongest property of this design and
the reason it wins over every alternative below.

The read path grows from 1 counter `get` to 17 (16 shards + the legacy key).

**Migration is "there isn't one."** Existing `analytics:count:<slug>` keys are
never written again, so they are frozen and race-free, and the reader adds them
to the sum. No backfill, no admin endpoint, no downtime, and a deployment that
rolls back keeps counting into the legacy key exactly as before (it just stops
seeing the shard totals until it rolls forward again).

### Why 16 shards

Applying the measured curve to a per-shard rate of `R/16`:

| per-link click rate `R` | per-shard rate | expected loss |
|---|---|---|
| ≤ 43 /s | ≤ 2.7 /s | below the measurement floor (~0%) |
| 150 /s | 9.4 /s | ~25% |
| 544 /s | 34 /s | ~70% |

The app's own throughput ceiling is roughly **25 clicks/second app-wide** (two
KV writes per click against Akamai's 50 write RPS cap). 16 shards put the
counter's clean ceiling at ~43 clicks/second on a *single* link — about 1.7×
above the ceiling the rest of the app already has. **That is the target: make
the counter stop being the accuracy-limiting component.** 8 shards would put
the clean ceiling at ~21/s, i.e. exactly at the app's own ceiling with no
margin, which is not a fix so much as a tie.

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
`UpdateCount` and `trimDays` are unchanged; they operate on one blob and do not
care that there are now sixteen of them.

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

The long comment block above `recordClickCount` must be rewritten: its current
text says the ~15% loss "needs a different counter shape, not a different
order" and points at `TASKS.md`. That is now what the function does, and the
comment should say so and carry the residual-loss number.

**Go tests (`redirect/linkgate/`, host-testable, `go test ./linkgate/...`):**

- `keys_test.go`: `CountShardKey("abc", 3) == "analytics:count:abc:3"`;
  `CountShardKey("abc", 0) == "analytics:count:abc:0"`. Replace `TestCountKey`.
- `analytics_test.go`: `ShardFor(x, 0)` and `ShardFor(x, 1)` return `0`;
  `ShardFor` over 100,000 draws puts each of 16 shards within ±10% of `1/16`;
  and — the regression that matters — **`ShardFor` fed only timestamp-shaped
  input** (a base nanosecond value plus `i * 1_000_000` for
  `i` in `[0, 100000)`, i.e. clicks exactly 1 ms apart) still hits all 16
  shards within ±10% of uniform. That last one is the exact input pattern
  `EventSlot` is documented to handle badly, and pinning it is what stops this
  design inheriting that defect.

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
independently, so for a *low-traffic* link whose shards happened to collect
clicks on different days, the merged map can hold more than 90 entries — up to
16 × 90 in the pathological case of a link clicked a handful of times a year
for a decade. Nothing is wrong with the data and every stored value stays well
inside the 1 MB value cap; the response is just longer than the retention
window nominally promises. Trimming it at read time would require declaring
`analytics_day_retention_days` for the `api` component in `spin.toml`
(it is currently declared for `redirect` only, `spin.toml:29` vs `44-51`) — a
manifest change and a new variable read on every analytics request, to bound a
response that is at most a few kilobytes. Not worth it. For a link with enough
traffic to matter, every shard sees clicks on the same days and the union is
the retention window.

**Python tests (`api/tests/test_analytics.py`):**

- counts written to shards `0`, `7` and `15` sum into one `total` and one
  merged `days` map (same day in two shards adds);
- a legacy `count:<slug>` value **plus** shard values sum together;
- an unparseable shard blob is skipped and the other shards still total
  correctly;
- `test_analytics_reports_count_and_events` (which writes the legacy
  `count:{slug}` key) **must keep passing unmodified** — it is now the
  migration regression test, and that is worth a comment in the file.

**`api/tests/test_kvprefix.py`** — extend the existing
`# --- Cross-language drift guard ---` section with a test that reads
`redirect/linkgate/keys.go`, regex-extracts
`CountShards\s*=\s*(\d+)`, and asserts it equals `analytics.COUNT_SHARDS`.
Same file, same technique, same `parents[2]` path as the prefix guard.

### GUI changes

**`gui/links/detail.html`** — one `<small>` after the Total clicks line
(`detail.html:50`), reusing the `#bulk-format-hint` pattern from
`dashboard.html:70`. No new CSS class, no new design token, no `DESIGN.md`
change, no `detail.js` change. Wording along the lines of:

> Recorded best-effort. Accurate up to roughly 40 clicks per second on a
> single link; heavier bursts under-count.

The number is derived from `CountShards × 2.7 /s`, so it is stale if the shard
count changes — call that out in the CLAUDE.md task so the two move together.

### Tooling

**`dev/click-load.sh` (new)** — the probe and the verification both need paced,
slug-round-robining load, and there is no such script today (`ls dev/` →
`kv-explorer-up.sh`, `kv-explorer.toml`). One script serves both:

```
./dev/click-load.sh <base-url> <rate-per-second> <count> <slug> [slug...]
```

Issues `<count>` GETs to `<base-url>/r/<slug>`, round-robining the slugs,
paced to approximately `<rate-per-second>`, printing the per-slug request count
and the number of non-302 responses. It deliberately does **not** read the
totals back — that needs an authenticated session, and the operator is already
looking at the link detail page. Keep it small; it is a measuring stick, not a
load-testing framework.

## If the probe fails

If condition (b) loses as much as (a), contention is not per-key and sharding
is worthless. Do not implement any of the above. The plan becomes:

1. **Accept and document the real number** — CLAUDE.md's Analytics section and
   Security-tradeoffs bullet carry the full curve (they partly do already), the
   detail page carries the caveat text (that task stands on its own and is
   worth landing either way), and the "~25 redirects/second" ceiling entry is
   rewritten to say the counter binds first.
2. Open a Future-work entry for **append-only click keys with a compaction
   story**, which is the only design that is exact under an app-wide
   contention model — and which is rejected today for the reasons in the next
   section. It needs a compaction trigger this app does not have, so it is
   genuinely a separate piece of work, not a fallback to reach for in the same
   pass.
3. Come back to the planner with the probe numbers.

## Trade-offs and rejected alternatives

**1. Append-only click keys (one unique key per click, no read).** Attractive
because it is *exact*: with no read there is no update to lose, at any rate,
under any mechanism — including the app-wide one that would kill sharding.
Rejected because the read side is unbounded in a way this app cannot absorb:

- Spin's KV enumerates the **whole physical store** (`get_keys`); there is no
  prefix scan. Counting one slug's clicks would mean enumerating every key in
  the app and filtering, on a host where each data operation costs 5.5–16.7 ms
  and the handler limit is 30 s.
- `api/backup.py`'s export already enumerates and reads every analytics key
  against `MAX_BACKUP_ENTRIES = 5_000`. Today's ~31 analytics keys per slug
  already put that limit at roughly 160 links; a single popular link's click
  keys would break backup for the whole application, not just for analytics.
- Compaction is the only answer to both, and this app has no trigger to run it
  from — Akamai Functions does not support cron or custom triggers, so it would
  have to be an admin endpoint someone remembers to press, or piggybacked onto
  the analytics read (a GET that writes, and racy between two concurrent
  readers double-counting the same click keys).

It trades a bounded, measured, quantified inaccuracy for an unbounded,
unquantified operational failure. Revisit only if the probe kills sharding, and
then as its own plan with the compaction trigger designed first.

**2. Do nothing; accept and document the real number.** This was live and is
not unreasonable: most links in a URL shortener never see 3 clicks/second, the
loss is already disclosed in CLAUDE.md, and the change costs a real read-path
regression on the detail page. Rejected because the honest ceiling — "trustworthy
below ~3 clicks/second" — is *below the app's own throughput ceiling* of ~25
clicks/second, so the product's headline analytics number is wrong before any
other limit in the system binds. A campaign link during a send is precisely the
case where the count matters and precisely the case that breaks. The caveat
text from this option is kept anyway, on the detail page.

**3. 8 shards instead of 16.** Attractive: half the extra read cost (39 vs 47
KV ops on the detail page) and half the extra backup entries per slug. Rejected
because it puts the clean ceiling at ~21 clicks/second on a single link, which
is *at* the app's own ~25/s write-RPS ceiling with no margin — the counter
would remain the binding accuracy constraint, which is exactly the thing this
work exists to remove. The read cost is paid on a single-link admin page that
already spends 31 KV operations, not on the hot path.

**4. Making the shard count a Spin variable** (as `analytics_event_slots` is).
Attractive because CLAUDE.md's own rule is that a value two components must
agree on becomes a variable. Rejected because the failure mode here is
different in kind: a mismatched `analytics_event_slots` produces a lossier
*sample*, whereas a shard count that the reader sets lower than the writer
**silently deletes click history from every total**, with no error anywhere.
That is precisely the failure shape the KV prefixes have, and the repo's
answer to it is a constant in each language pinned by a cross-language test
(`api/tests/test_kvprefix.py`), not a knob. Following the prefix precedent also
removes the "operator lowers it in a hurry" foot-gun entirely, and the
raise-only rule can then be enforced by review of a single commit that touches
both languages.

**5. Instance-sticky shard selection** (draw one shard per Wasm instance at
startup, reuse it for every click that instance handles). Attractive under the
stale-read model: an instance always reads back its own last write, so
read-your-writes would eliminate self-collision entirely. Rejected on two
grounds. If the staleness lives in the KV backend (replica lag) rather than in
a per-instance cache, stickiness buys literally nothing. And if Akamai creates
one instance per request — plausible on a FaaS, and unconfirmed — sticky
selection *is* per-request selection, with the added risk that two long-lived
instances drawing the same shard are welded to it permanently rather than
colliding 1/16 of the time. Same expected loss, strictly worse tail, more code.

**6. Read-after-write verification / retry.** Attractive as a way to detect a
lost increment. Rejected: it doubles the counter's KV operations on the hot
path (from 2 to 4, roughly +11–33 ms per click and +25 write RPS pressure at
the app's ceiling), and under the inferred stale-read mechanism the read-back
cannot distinguish "my write was lost" from "my read is stale" — so the retry
would fire on healthy writes and double-count them.

**7. In-instance batching** (accumulate clicks in a package-level counter,
flush every K clicks or every T ms). Attractive because it collapses N writes
into one and would fix both the loss and the write-RPS ceiling at once.
Rejected because a Wasm instance on a FaaS can be torn down at any moment, with
no shutdown hook — every unflushed click is lost outright, silently, and the
loss is unbounded rather than merely rate-dependent. It also reintroduces the
package-level shared state that `docs/plans/toggleable-logging.md` explicitly
designed *out* of this component (the collector is per-request context, never a
package variable, precisely because concurrent requests interleave).

**8. Splitting `{total, days}` into separate keys** so the total could be a
narrower value. Rejected: it makes every click two counter writes instead of
one, halving the app's already-binding ~25 clicks/second write-RPS ceiling, and
does nothing about the read-modify-write on either key.

**9. A dedicated deploy-instrument-deploy step to confirm the mechanism**
(write a sentinel, immediately re-read it, log whether the read was stale).
Attractive because the mechanism is genuinely unknown and it would settle it.
Rejected as the *gating* step: the design depends on one property only —
per-key versus app-wide contention — and that is measurable today on the
running deployment with `curl` and sixteen extra links, with no build, no
deploy and no code. The mechanism question is recorded in "Key technical facts"
with the evidence and the experiment that would answer it, for whoever wants it
later.

## Tasks

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
commands, unchanged), `DESIGN.md`, `gui/links/detail.js`.

## Verification

Run in this order. Steps 1–2 gate everything after them.

1. **Baseline, before any change** (already confirmed while planning; re-run if
   time has passed):
   ```bash
   cd redirect && go test ./linkgate/...     # ok
   cd api && uv run pytest                    # 508 passed
   cd gui-pages && uv run pytest
   ```

2. **The probe, on the deployed app, before writing counter code.** Create 16
   fresh links plus one more for the control. Note each detail page's Total
   clicks (0). Then:
   ```bash
   ./dev/click-load.sh https://<app-id>.fwf.app 34 100 <control-slug>
   ./dev/click-load.sh https://<app-id>.fwf.app 34 100 <s1> <s2> ... <s16>
   ./dev/click-load.sh https://<app-id>.fwf.app 65 100 <s1> <s2> ... <s16>
   ```
   **Pass:** the control loses ~70% (reproducing the curve) and the 16-slug runs
   lose under ~5% on every slug. Record all three in
   `docs/plans/click-count-accuracy-scratch.md`. **Fail:** the 16-slug runs lose
   like the control — stop and go to "If the probe fails".

3. **Unit suites, after the Go and Python changes:**
   ```bash
   cd redirect && go test ./linkgate/...
   cd api && uv run pytest
   cd gui-pages && uv run pytest
   ```
   The distribution tests are the ones that matter here — a `ShardFor` that
   concentrates on one shard passes every functional test and fixes nothing.

4. **Local end-to-end** (proves *correctness*, not accuracy — local sqlite
   loses nothing, so a local run can never reproduce the bug):
   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```
   - Create a link, hit `/r/<slug>` 10 times, open its detail page: **Total
     clicks: 10**, and the per-day table shows 10 for today.
   - Trace one redirect and confirm the hot path did not grow:
     ```bash
     curl -sD- -o/dev/null -H 'X-SS-Debug: <token>' http://localhost:3000/r/<slug>
     ```
     with `SPIN_VARIABLE_LOG_DEBUG_TOKEN=<token>` set on the `spin up`. The
     stderr line must still read `kv_ops=6` with `get=2/... set=2/...`.
   - Trace the analytics page before and after the change and diff the `get`
     count: it must rise by exactly 16.
   - The caveat `<small>` renders under Total clicks.

5. **Legacy-key migration, locally**, using the dev KV explorer:
   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> \
   SPIN_VARIABLE_KV_EXPLORER_PASSWORD=<explorer-pw> \
   SPIN_VARIABLE_COOKIE_SECURE=false \
     ./dev/kv-explorer-up.sh
   ```
   Set `analytics:count:<slug>` to
   `{"total": 5, "days": {"2026-01-01": 5}}`, then click the link 3 more times.
   The detail page must show **Total clicks: 8** with both `2026-01-01` (5) and
   today (3) in the per-day table. This is the whole migration story; if it
   works, no deployment needs anything done to it.

6. **The real verification, on the deployed app, against the original curve.**
   Deploy, then for each of three fresh links:
   ```bash
   ./dev/click-load.sh https://<app-id>.fwf.app 9.4 100 <slug-a>
   ./dev/click-load.sh https://<app-id>.fwf.app 34  100 <slug-b>
   ./dev/click-load.sh https://<app-id>.fwf.app 65  100 <slug-c>
   ```
   **Pass:** each detail page reads 98–100. Before the change the same runs
   would have read ~75, ~30 and ~25. 100 samples gives a ~1% resolution, which
   is the reason for not reusing the original 20.
   Then trace one deployed redirect with `X-SS-Debug` and confirm `kv_ops=6`.

7. **Only after 6 passes**, if the optional event-slot task is taken: 20 clicks
   300 ms apart against a local `spin up`, then count distinct entries in the
   detail page's Recent events list — 12 or more, against 3 of 8 measured
   before.

## What this does NOT fix

State these plainly; they are the honest residual:

- **It is not exact.** It is a probability reduction, not a correctness fix.
  There is still no compare-and-swap and there is still a read-modify-write.
- **Loss returns above roughly 43 clicks per second on a single link**
  (16 × the measured 2.7/s clean point), reaching **~25% around 150
  clicks/second** and **~70% around 544 clicks/second**, on the same curve as
  before, just shifted 16× to the right.
- **The "0%" readings, before and after, mean "below the measurement floor."**
  20 samples cannot resolve a loss rate under ~5%; 100 samples cannot resolve
  one under ~1%. Nothing here establishes zero.
- **Clicks already lost are lost.** Nothing reconstructs history.
- **The recent-events ring buffer is unchanged** by the counter work and
  remains a best-effort sample. The optional last task improves it but does not
  make it complete.
- **Nothing about app-wide throughput changes.** The redirect still costs two
  KV writes per click against Akamai's 50 write RPS cap, so the ~25
  clicks/second app-wide ceiling stands — this work moves the *counter* out
  from underneath it, nothing more.
- **The mechanism remains unconfirmed.** The design works under both candidate
  mechanisms, which is why it is safe to ship without knowing, but "we fixed
  the counter" is not "we understand the store".

## Out of scope / follow-ups

- **Folding the legacy `analytics:count:<slug>` key into shard 0 and deleting
  it**, to get the read path back from 17 counter gets to 16. Safe to do (the
  legacy key is never written again, so a one-shot fold has nothing to race
  against) but worth almost nothing. Belongs under `TASKS.md`'s "Future work
  (not scheduled)" if anyone wants it; not added by this plan.
- **`MAX_BACKUP_ENTRIES` versus analytics keys.** Sharding raises the analytics
  key count per slug from ~31 to ~46, moving the practical full-backup ceiling
  from roughly 160 links to roughly 108. Backup with analytics included is
  *already* impractical past ~150 links and the documented workaround is
  `?stores=links,users`; this makes an existing problem modestly worse and does
  not create a new one. The existing "Chunked or resumable backup/restore"
  Future-work entry is where that gets solved. Flagged here so the regression is
  on the record, not fixed here.
- **`TASKS.md`'s "Find the real cause of the analytics recent-events slot
  collisions"** — the optional last task removes the app's *dependence* on
  `EventSlot` (by deleting it) without answering why it behaved as it did. That
  entry becomes unactionable if the optional task lands. **Deciding whether to
  close it is the user's call**, not the builder's; per the append-only rule
  nobody edits that line without being asked.
- **A deploy-instrumented confirmation of the stale-read mechanism** — the
  experiment is described in "Key technical facts". Trigger: the verification in
  step 6 failing in a way the per-key model does not explain.
- **Append-only click keys** stay rejected unless the probe kills sharding; the
  rejection is recorded under `TASKS.md`'s "Considered and rejected".
