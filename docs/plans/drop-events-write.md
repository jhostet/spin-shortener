# Drop the Recent-Events Write

## Context

Every recorded click currently performs **two** KV writes: one
`analytics:count:<slug>:<shard>` (a read-modify-write) and one
`analytics:events:<slug>:<slot>` (a blind overwrite into a 30-slot ring
buffer). Akamai Functions caps KV writes at 50/second app-wide, so two writes
per click puts the whole service's click ceiling at ~25 clicks/second, above
which clicks are silently under-counted. Dropping the second write doubles that
ceiling.

Three independent write-ups have converged on this one change, and **none of
them cites the others — that convergence is the actual news**:

1. **The write cap.** `TASKS.md`'s Future-work entry *"Reduce the redirect's KV
   writes per click from two to one, doubling the app-wide click ceiling"*
   (raised 2026-08-06) filed it as the highest-value analytics follow-up now
   that counter sharding leaves the write cap as the sole binding constraint on
   click accuracy.
2. **Key-space growth.** `CLAUDE.md`'s "Parallel KV reads" section separately
   observed that the `events:` slots are 30 of the up-to-95 analytics keys per
   link, and that a key enumeration costs ~68.7 µs per physical key in the whole
   store. `TASKS.md`'s 2026-08-10 `list_keys` measurement section says the same
   thing in its own words and explicitly notes that neither write-up mentions
   the other.
3. **Every key walk in the app.** Since `docs/plans/derived-link-indexes.md`
   Stage 2 shipped (2026-08-18), `GET /api/links` itself derives its slug list
   from a whole-store key enumeration, so the enumeration is no longer only the
   dashboard's Clicks column problem — it is on the main list endpoint too.

**A fourth justification that has been quoted for this change is now void, and
this plan retires it.** `CLAUDE.md` still says `recordAnalytics` "runs before
`http.Redirect` — so every visitor waits for bookkeeping they have no stake
in." That has not been true since commit `398f391` (2026-08-06): `redirect/main.go`'s
`sendRedirectThenRecord` sends the 302 first and records *all* analytics
afterwards, measured at 98.0 ms → 74.3 ms median TTFB. **No visitor waits for
the events write today**, so "it costs the visitor latency" is not an argument
for removing it. Three justifications, not four.

What is lost is real and must not be soft-pedalled: `GET /api/links/{slug}/analytics`
returns `recent_events`, and `gui/links/detail.js` renders a Recent events table
of timestamp / referrer / device class. **Referrer and device class are stored
nowhere else in this application.** Removing the write removes the product's
only answer to "where did this traffic come from."

**Recommendation: remove it — write, read and GUI together, in one release.**
The reasoning, ranked, is in "Trade-offs and rejected alternatives" below. The
short form: the write ceiling is the one hard limit in this product whose
failure mode is *silent* (under-counted clicks, the number the feature exists to
report), it binds on exactly the traffic shape this audience produces (a
campaign email or ad burst), and this is the only lever on it that is under our
own control; what is given up is a ≤30-entry blind-overwrite ring that the
dashboard never reads, that answers the referrer question only anecdotally, and
that spent most of its life broken in production. A strictly better answer to
the referrer question is available later at **zero** additional writes (fold
aggregate device/referrer-host counts into the `count:` blob that is already
read-modify-written) and this removal does not foreclose it.

**Confirmed decisions (settled by the user before planning):**

- Anything that changes the **counter** (`analytics:count:<slug>:<shard>`) is
  out of scope. This is only about the second write.
- Writes are never gathered or batched; reads may use
  `scoped_get_many`/`gather_reads`.
- No new KV key type without accounting for the three obligations
  (`backup.py`'s `INDEX_KEYS`/`restore_write_order`, `consistency.py`'s key-shape
  recognition, `kvprefix.STORE_PREFIXES`). **This plan introduces no new key
  type.**
- Rejected alternatives go in `TASKS.md`'s "Considered and rejected"; anything
  needing a fresh deployed measurement is to be flagged rather than guessed.

## Key technical facts confirmed during research

- **All analytics already runs after the response.** `redirect/main.go`'s
  `sendRedirectThenRecord` writes `Location`, `Content-Length: 0`,
  `WriteHeader(302)` and an empty `Write` *before* calling `recordClickCount`
  and `recordClickEvent`. Confirmed by reading the function and by
  `git show 398f391` ("Experiment: record analytics after sending the redirect",
  2026-08-06). The `CLAUDE.md` sentence claiming otherwise is stale.
- **The op profile today is 6 for a successful redirect, 2 for a miss.** Read
  from the code: `open` (handler) + `get` (`lookupLink`) + `open` (analytics) +
  `get` + `set` (`recordClickCount`) + `set` (`recordClickEvent`). After this
  change it is **5** (2 `open`, 2 `get`, 1 `set`); a miss is unchanged at 2.
- **`analytics_event_slots = 0` does not disable the write.** `EventSlot(now, 0)`
  → `ShardFor(entropy, 0)` → `numShards <= 1` returns `0`, so the component
  would still write `events:<slug>:0` on every click. Confirmed by reading
  `redirect/linkgate/analytics.go`'s `ShardFor`. The variable is **not** an
  off-switch, and could only become one with a code change in
  `recordClickEvent` — which removes the entire "reversible with no code change"
  appeal of that option.
- **`analytics_event_slots` is declared in three places in `spin.toml`** (top-level
  `[variables]` line 14, `[component.redirect.variables]` line 35,
  `[component.api.variables]` line 57) and is read in exactly two places in
  Python (`api/app.py` lines 341, 401, 411) and one in Go
  (`redirect/main.go:469`). Confirmed by `grep -n analytics_event_slots spin.toml api/app.py redirect/main.go`.
- **Two of the three Python reads feed a parameter that is never used.**
  `backup.handle_export` and `backup.handle_restore` both take
  `num_event_slots: int` and neither function body references it —
  `grep -n "num_event_slots\|event" api/backup.py` returns only the two
  signature lines. Dead since it was added.
- **`EventSlot` and the counter share `ShardFor`, and `ShardFor`'s splitmix64
  finalizer must survive.** `EventSlot` is a one-line delegation to `ShardFor`;
  the long derivation of *why a single multiply is linear over the modulus* sits
  above `EventSlot` in `redirect/linkgate/analytics.go`, not above `ShardFor`.
  Deleting `EventSlot` naively deletes that derivation. `ShardFor` is still used
  by `recordClickCount` via `linkgate.ShardFor(clickEntropy(now), linkgate.CountShards)`.
- **The anti-aliasing property stays pinned after the `EventSlot` tests go.**
  `TestShardFor_DistributesUniformlyOverTimestampShapedInput` drives 100,000
  timestamps 1 ms apart through `ShardFor` at `CountShards` and demands every
  shard land within ±10% of an even share. `CLAUDE.md` records that substituting
  a single multiply "puts all 100,000 draws of the timestamp-distribution test
  into shard 0", so this test alone catches the regression the five `EventSlot`
  tests were written for. This plan requires that be re-verified by mutation
  rather than assumed.
- **`parse_analytics_key`'s `events:` branch is load-bearing after the change.**
  `analyticsorphans.classify_analytics_keys` routes any key
  `parse_analytics_key` does not recognise into `unrecognized`, which is
  deliberately never purgeable. Removing the `events:` branch would make every
  leftover `events:` key permanently unpurgeable *and* would show it in the
  orphan report's `unrecognized_sample`. Confirmed by reading
  `api/analyticsorphans.py`'s `classify_analytics_keys` and its docstring.
- **`gui/app.js`'s `formatTimestamp({ precise: true })` has exactly one caller**,
  `gui/links/detail.js:112`. Confirmed by `grep -rn precise gui/`.
- **`responses.to_iso8601_utc_ms` has exactly one production caller**,
  `analytics._parse_event`. Confirmed by `grep -n to_iso8601_utc_ms api/*.py`.
- **`dev/click-load.sh` hardcodes `KV_WRITES_PER_CLICK=2`** and derives both its
  pre-run warning and its post-run achieved-write-rate from it. Confirmed by
  reading lines 14-18, 32, 77, 83, 148.
- **Baseline suites, run 2026-08-18 before any change:** `cd redirect && go test ./linkgate/...`
  ok; `cd api && uv run pytest` **648 passed**; `cd gui-pages && uv run pytest`
  **71 passed**.
- **The live deployment's store is currently 14 links / 37 analytics keys, and
  its `list_keys` is at the floor** — `list_keys=1/24787` (24.8 ms warm) traced
  on `88f4f4c-no-indexes`, against the documented floor of "about one KV
  operation" (23.9 ms measured at 57 keys). Source: `TASKS.md`'s 2026-08-18
  sections. **Consequence, stated plainly: on today's store this change saves
  nothing measurable on enumeration**, because the enumeration is already at its
  irreducible floor. The enumeration benefit is entirely about slope at future
  scale.
- **UNCONFIRMED — the current `count:`/`events:` key split on any real store.**
  Nothing today reports it. The orphan report knows `event_keys` per slug but
  publishes it only for *orphan* slugs, so live links' event keys are invisible.
  This plan adds a `totals.obsolete_event_keys` field precisely so the residual
  becomes a measured number instead of an estimate. Until then the modelled
  share below is arithmetic, not measurement.
- **UNCONFIRMED — whether the app-wide write cap, or request concurrency, is
  what actually binds the click ceiling today.** `CLAUDE.md`'s Akamai section
  says a click costs ~95-120 ms of handler time, ~97% of it KV, so sustaining 50
  writes/second needs ~25 concurrent in-flight requests, and flags that "latency
  and concurrency are the plausible binding constraint, not the write cap."
  Existing data brackets the knee only loosely: 19.7 req/s (39.4 writes/s) lost
  0%, 38.5 req/s (77 writes/s) lost 32.5%. **What it would take to confirm:**
  `dev/click-load.sh` against the deployed build at 16 / 22 / 26 / 32 req/s over
  ≥16 slugs, before and after this change, locating where loss first appears. If
  the knee sits at ~25 req/s before and ~50 after, the doubling is real and
  measured. This is planned as a before/after verification (tasks 1 and 9), not
  as a gate — the change is directionally right under either hypothesis, since
  removing one of three data operations also shortens the post-response handler
  and therefore reduces in-flight concurrency.

### The enumeration saving, modelled

For a link with `k` clicks, expected distinct keys are
`64·(1−(63/64)^k)` count shards and `30·(1−(29/30)^k)` event slots. The event
slots' share of that link's analytics keys is therefore:

| clicks on the link | count keys | event keys | events' share |
|---|---|---|---|
| 10 | 9.3 | 8.6 | **48%** |
| 100 | 50.8 | 29.0 | **36%** |
| 1,000+ | 64 | 30 | **32%** |

So the "roughly a third" both prior write-ups quote is the *busy-link* figure;
for the long tail of quiet links it is closer to a half, because both key types
grow ~1-per-click until they saturate. **This is arithmetic (balls-in-bins), not
a measurement** — it is exactly what `totals.obsolete_event_keys` will let
someone check against a real store.

It is still a **slope** change, not a shape change. The enumeration remains
linear and unbounded in clicks. At ~200 links of which ~50 carry real traffic,
the store is ~5,000 keys (~365 ms enumeration) today and ~3,400 keys (~256 ms)
after — both past the ~250 ms revisit trigger `docs/plans/derived-link-indexes.md`
records for `GET /api/links`. **Do not present this change as the fix for
enumeration growth.** It buys roughly 50% more headroom before the same wall;
the real answers (a cached per-request walk, pagination, a different totals
shape) are already filed separately.

## Redirect (Go) changes

This is the hot path, so the language rule applies in the usual direction: the
write being removed is on the redirect path and therefore lives in Go, and
nothing new is added anywhere. **This change makes the hot path cheaper, which
is unusual for this repo** — the whole point is one fewer KV operation.

**New op profile: 5 KV operations for a successful redirect** (2 `open`, 2
`get`, 1 `set`), **2 for a miss** (unchanged). A traced request's logfmt line
should read `set=1/…` where it currently reads `set=2/…`.

**Do not also remove the second `kv.Open`.** With one write left, two `open`s
for three data operations looks newly wasteful. It is not: `CLAUDE.md` records
that threading the handler's store through measured ~0.2% on Akamai (~154 µs
against ~20 ms per data operation) and is not worth doing. That decision is
unchanged by this one.

### `redirect/main.go`

- Delete `recordClickEvent` entirely (currently lines ~459-475), including its
  doc comment.
- In `sendRedirectThenRecord`, delete the `recordClickEvent(store, slug, r, now)`
  call, leaving only `recordClickCount(store, slug, now)`.
- **Drop the now-unused `r *http.Request` parameter from `sendRedirectThenRecord`**
  and update its three call sites (`handleRedirect`, and two branches of
  `handleRedirectPost`). Go permits an unused parameter, so this will compile
  either way — it is a cleanliness requirement, and the three call sites are
  named here because a previous edit to this exact set matched only 2 of 3 (the
  POST no-password branch is nested one level deeper; see `git show 398f391`).
- Rewrite the two paragraphs of `sendRedirectThenRecord`'s doc comment that are
  now false: the one beginning *"Both halves after the response. Kept as two
  functions…"* (there is one half now) and the one beginning *"A visitor has no
  stake in the events write…"* — the visitor still has no stake in the *count*
  write, so keep the reasoning and change the subject. Keep everything about the
  bodyless 302, `Content-Length: 0`, the load-bearing empty `Write`, and the
  `bufferingWriter`/`X-SS-Debug` caveat: none of that changes.
- In `recordClickCount`'s doc comment, the M2 bullet currently reads *"a
  recorded click costs two (this shard, plus one events slot), so above ~25
  clicks/second ACROSS THE WHOLE SERVICE writes are throttled"*. It becomes one
  write and ~50 clicks/second. Keep the M1/M2 split intact — **M1 (per-key
  contention) is fixed by sharding and is not touched by this change**, and
  conflating the two mechanisms has produced a wrong conclusion in this repo
  before.
- `intVariable` stays (still used for `analytics_day_retention_days`); its
  `analytics_event_slots` call site goes with `recordClickEvent`.

### `redirect/linkgate/analytics.go`

Delete `ClassifyUserAgent`, its `containsAny` helper, `FormatEvent`, and
`EventSlot`. Keep `CountRecord`, `UpdateCount`, `trimDays`, `ShardFor`,
`mix64`.

**The single most important instruction in this plan: move `EventSlot`'s
derivation onto `ShardFor` rather than deleting it with `EventSlot`.** That
comment is the only written record of *why* `ShardFor` runs the value through a
splitmix64 finalizer instead of a single multiply, and `ShardFor` is still the
counter's shard selector. Concretely, `ShardFor`'s doc comment must retain, in
its own words:

> A single multiply is LINEAR OVER THE MODULUS: multiplication distributes over
> the modulo, so for two inputs Δ apart the result advances by a constant
> stride, and the reachable outputs are one additive cycle rather than the whole
> range. Go's multiply wraps at 2^64, which is divisible by 2, so wraparound
> preserves the low bit — pinning output parity to the input's low bit forever.
> Clicks arriving at a steady cadence are exactly that shape. This is not
> hypothetical: it was the cause of the recent-events collision defect found
> 2026-08-07 (8 clicks 300 ms apart reached 1 slot of 30). Do not "simplify"
> this back into a multiply.

It should also note that the caller that suffered the original defect
(`EventSlot`) no longer exists, so the surviving pin is
`TestShardFor_DistributesUniformlyOverTimestampShapedInput`.

**Imports shrink and Go will not compile with unused ones.** After the
deletions, `fmt` (only `FormatEvent`), `strings` (only `ClassifyUserAgent`) and
`time` (only `EventSlot`) are all unused. The import block becomes
`encoding/json` and `sort`.

### `redirect/linkgate/keys.go`

Delete `EventKey` and its comment. `LinksPrefix`, `AnalyticsPrefix`, `LinkKey`,
`CountShards` and `CountShardKey` all stay unchanged — `CountShards` in
particular keeps its RAISE-ONLY rule and its cross-language pin in
`api/tests/test_kvprefix.py`, which reads this file. **Removing `EventKey` does
not affect that test** (it pins the two prefixes and `CountShards`, not
`EventKey`); the builder should confirm rather than assume, since the test parses
`keys.go` textually.

### `redirect/linkgate/analytics_test.go` and `keys_test.go`

Retire, with the feature: `TestFormatEvent`, `TestClassifyUserAgent`,
`TestEventSlot_WithinRange`, `TestEventSlot_NonPositiveNumSlotsDefaultsToOne`,
`TestEventSlot_SteadyCadenceReachesMostOfTheRing`,
`TestEventSlot_ReachesBothParitiesOfTheRing`,
`TestEventSlot_ReproducesTheOriginalBugReportScenario`, and `TestEventKey`.

Keep, untouched: all four `ShardFor` tests, all five `UpdateCount` tests,
`TestLinkKey`, `TestCountShardKey`,
`TestCountShardKeyNeverCollidesWithTheLegacyKey`.

**The three `EventSlot` regression tests are the ones this repo cares most
about, and they exist because of a genuine defect fixed 2026-08-07 — so their
property must be shown to survive rather than assumed to.** The required check,
which is a task's done-when criterion: temporarily replace `ShardFor`'s body
with the old shape

```go
return int((entropy * 2654435761) % uint64(numShards))
```

run `go test ./linkgate/...`, confirm
`TestShardFor_DistributesUniformlyOverTimestampShapedInput` **fails**, then
restore `ShardFor` byte-identically (`git diff redirect/linkgate/analytics.go`
shows only the intended edits). If it does *not* fail, stop: the anti-aliasing
property has become unpinned and a replacement test is owed before the
`EventSlot` tests are deleted.

## API changes

Python, per the language rule — none of this is on the hot path.

### `api/analytics.py`

- `handle_analytics(links_store, analytics_store, principal, slug, num_event_slots, get_many)`
  → `handle_analytics(links_store, analytics_store, principal, slug, get_many)`.
- Delete the `event_keys` construction, the events fetch, the event parsing loop,
  the `events.sort(...)`/`del event["unix_ms"]` block, and the `recent_events`
  key from the 200 body. The response becomes exactly
  `{"total": ..., "days": {...}}`.
- Delete `_parse_event` and the `datetime`/`timezone` imports it needed
  (`from datetime import datetime, timezone`) if nothing else uses them —
  confirm, since Go-style unused imports are legal in Python and would simply
  linger.
- Drop `to_iso8601_utc_ms` from the `responses` import list. **Do not delete
  `responses.to_iso8601_utc_ms` itself.** It is a three-line general-purpose
  formatter, a sibling of the in-use `to_iso8601_utc`, and it has its own test in
  `api/tests/test_responses.py`. The asymmetry with deleting
  `linkgate.ClassifyUserAgent` is deliberate and worth stating: that one is
  domain logic for a retired feature, this one is a serialisation primitive.
- **`parse_analytics_key` keeps its `events:` branch, unchanged.** Its docstring
  should gain one sentence saying why: nothing writes `events:` keys any more,
  but leftover keys still exist in real stores, and
  `analyticsorphans.classify_analytics_keys` must keep recognising them or they
  become permanently unpurgeable `unrecognized_key`s.
- `handle_click_totals` is untouched. It already ignores `events:` keys
  (`parsed[0] != "count"`), which is what
  `api/tests/test_click_totals.py::test_events_keys_are_never_read` pins — and
  that test **stays**, because leftover `events:` keys still exist and reading
  them would still be a regression.

### `api/backup.py`

Drop the unused `num_event_slots: int` parameter from both `handle_export` and
`handle_restore`. This is dead weight rather than behaviour, but it is the last
thing forcing `app.py` to read the variable at those two routes.

Backup/restore is otherwise key-agnostic and needs no change: leftover `events:`
keys export and restore like any other analytics key. A restore of a pre-change
backup will re-create `events:` keys — harmless (nothing reads them) and visible
in the new `obsolete_event_keys` total.

### `api/app.py`

- `GET /api/links/{slug}/analytics` branch: delete
  `num_event_slots = int(await variables.get("analytics_event_slots"))` and drop
  the argument from the `handle_analytics` call.
- `GET /api/admin/backup` and `POST /api/admin/restore` branches: same deletion,
  and drop the argument from the `handle_export`/`handle_restore` calls.
- After these three, `grep -n analytics_event_slots api/` must return nothing.

### `api/analyticsorphans.py` — report the residual

`build_orphan_report`'s `totals` gains **`obsolete_event_keys`**: the sum of
`event_keys` across **both** the `orphans` and `live` dicts (it already receives
both, and already computes `event_keys` per slug in
`classify_analytics_keys`). Nothing else in that module changes — no new
endpoint, no new purge behaviour, no new key type.

The field's purpose is stated in the docstring: it is the measurement that
decides whether the deferred sweep (see "Leftover `events:` keys") is worth
building.

## GUI changes

No new design tokens, no `.impeccable/design.json` change, no new route, no
`spin.toml` GUI change. Remember `spin_static_fs` serves a startup snapshot —
**restart `spin up` after editing anything under `gui/`**, and diff the served
asset (`curl localhost:3000/links/detail.js`) against disk before doubting a fix.

### `gui/links/detail.html`

Remove the entire second column of the analytics grid — the
`<div><h3>Recent events</h3><figure><table id="events-table">…</table></figure></div>`
block.

**Then remove the `<div class="grid">` wrapper and the surviving inner `<div>`
too**, promoting the "Clicks per day" block to full width. A Pico `.grid` with
one child renders as one full-width column anyway, so this is not a visual
change — it is removing a wrapper that now means nothing. `<h3>Clicks per day</h3>`
**stays**: it still labels the table, and `DESIGN.md`'s Title step is unaffected
(only its parenthetical example cites "Recent events").

Also update `#click-accuracy-hint`, which currently reads *"Accurate while the
whole service stays under roughly 25 clicks per second; heavier traffic
under-counts."* → **roughly 50 clicks per second**. This sentence is a
user-visible consequence of the write arithmetic and must move with it.

### `gui/links/detail.js`

Delete the recent-events render block (the `eventsBody` lookup, the empty-state
row, and the `for (const event of data.recent_events)` loop, ~lines 104-118).
Nothing else in `loadAnalytics` changes.

### `gui/app.js`

Delete `formatTimestamp`'s `precise` parameter and its whole `if (precise)`
branch, plus the comment paragraph above the function that explains what
`precise` is for. `dateOnly` and the `DATE_ONLY` handling stay exactly as they
are.

**Carry the ECMA-402 lesson out with it, or it is lost.** That branch spells out
`year`/`month`/`day` instead of `dateStyle: "medium"` because ECMA-402 forbids
combining `dateStyle`/`timeStyle` with any individual component option, and the
version that got that wrong shipped to production and threw
`TypeError: Invalid option` inside this exact render loop (see `TASKS.md`, "Recent-events
table was throwing in production", 2026-08-11). The lesson stays recorded there;
the builder should not feel obliged to preserve a dead branch to preserve a
comment.

### `gui/admin/backup.js`

One sentence in the orphans summary, rendered only when the new
`totals.obsolete_event_keys` is above zero — e.g. *"N of these are `events:`
keys from the retired recent-events feature and are safe to remove."* Keep it to
the existing summary paragraph; **do not add a purge button for them** (see
"Leftover `events:` keys"). Use the file's existing string-building style; no new
tokens or CSS.

## Configuration and tooling changes

### `spin.toml` — retire `analytics_event_slots`

Delete all three occurrences: the top-level `[variables]` declaration (line 14)
and both `[component.redirect.variables]` (line 35) and
`[component.api.variables]` (line 57) bindings.

**The deployment consequence, spelled out, because this is exactly where this
repo has been bitten before.** The `public_base_url` → `public_base_urls` rename
silently fell back to a default with no warning in any log, because a *still
needed* value was looked up under a new name and quietly found nothing. **This
case is structurally different and safe: no value is needed at all afterwards.**
The two possible mistakes and their failure modes:

| mistake | what happens |
|---|---|
| delete the top-level `[variables]` entry but leave a `{{ analytics_event_slots }}` binding in a component block | `spin up` / `spin aka app deploy` fails validation on an undefined variable — **loud, immediate, before anything serves** |
| operator keeps passing `SPIN_VARIABLE_ANALYTICS_EVENT_SLOTS=…` or `--variable analytics_event_slots=…` after the retirement | the value is ignored; nothing reads it; no behaviour change. `spin aka app deploy` rejects unknown `--variable` names, so an unrecognised flag also fails loudly rather than silently |

So: delete all three together, in one commit. `analytics_event_slots` is not one
of the three variables `CLAUDE.md` requires on every deploy
(`admin_bootstrap_password`, `cookie_secure`, `public_base_urls`), and no
deploy script or `dev/` script passes it — confirmed by
`grep -rn analytics_event_slots dev/ Jenkinsfile` returning nothing.

`analytics_day_retention_days` **stays** — the counter still uses it.
`gui-pages/tests/test_manifest_components.py` compares component *names* only,
so it is unaffected.

### `dev/click-load.sh`

- `readonly KV_WRITES_PER_CLICK=2` → `1`.
- The header comment (lines ~14-18) explains the two writes and the ~25 req/s
  measurement rule; rewrite for one write and ~50 req/s, keeping the warning
  about the 2026-08-06 probe that was first run at 38.5 req/s and misread as a
  clean failure — that lesson survives, only the threshold moves.
- The pre-run and post-run over-cap warnings derive from the constant and need
  no edit beyond it.

## Documentation changes

All of these are builder tasks; the planner does not edit them.

- **`CLAUDE.md`**, in order of appearance:
  - The stale "Redirect caching" paragraph asserting `recordAnalytics` "runs
    before `http.Redirect` — so every visitor waits" → corrected to *after* the
    response, via `sendRedirectThenRecord` (and note the function name
    `recordAnalytics` no longer exists). This is a correction that is true today
    and would be true without this change; it is bundled here because this plan
    is what found it.
  - The "Analytics" intro: two keys per successful redirect → one.
  - "The recent-events ring buffer collided…" subsection: mark **retired with
    the feature (2026-08-18)** and keep the arithmetic derivation as history,
    since it is the reasoning behind `ShardFor`'s finalizer, which survives.
  - The M1/M2 table: M2's `cause` row (two writes → one) and `threshold` row
    (~25 → ~50 clicks/second). **M1 is unchanged — do not touch it.**
  - The "Security tradeoffs" click-accuracy bullet and the Akamai deployment
    section's `50 ÷ 2 ≈ 25` arithmetic → `50 ÷ 1 ≈ 50`, keeping the standing
    caveat that latency/concurrency may bind before the write cap does.
  - The redirect op profile in "Toggleable structured logging": 6 → **5** for a
    successful redirect (2 `open`, 2 `get`, 1 `set`); a miss stays 2.
  - "Parallel KV reads"'s closing note that dropping the `events:` write is an
    unrealised ~⅓ lever → record it as done, with the modelled 32-48% range and
    the fact that it is a slope change, not a shape change.
  - Every `analytics_event_slots` reference (including the ones in
    `analyticsorphans`-adjacent prose about "keys left by a since-lowered
    `analytics_event_slots`", which stay accurate as *history* and should be
    reworded to past tense).
  - **Two stale numbers in sections this task is already rewriting:** the "KV
    store" bullet says "`keys.go` also holds `CountShards = 16`" and the
    Analytics heading reads "Click counting is sharded across 16 keys". Both are
    64. Fix while there.
- **`PRODUCT.md`** line 32: drop *"plus a best-effort (lossy, not complete)
  recent-events sample"* from the analytics capability. Consider adding the
  removal to the record honestly — this is a capability that was shipped and
  withdrawn, and `PRODUCT.md` is where a persona-facing capability lives.
- **`README.md`** line 11: drop *"and a best-effort sample of recent click
  events"*.
- **`DESIGN.md`** line 163: the Title/`h3` example cites *"'Clicks per day' /
  'Recent events' side by side"*. The h3 step itself is unchanged; only the
  example needs a survivor (e.g. "Clicks per day"). **No token changes, no
  contrast implications, no No-Shadow-Rule implications.**

## Leftover `events:` keys

**Decision: accept, measure, and defer the sweep behind a trigger.** Not a
one-shot script, and not an extension of the existing purge today.

The reasoning:

- **The leak stops immediately and the residual is frozen, not growing.** That is
  the whole point of the change. From the moment `recordClickEvent` is gone, no
  new `events:` key is ever created. The residual is at most 30 keys per link
  that had traffic before the cutover, and it *decays*: `links.handle_delete`'s
  inline purge and `analyticsorphans.handle_orphan_purge` both delete a slug's
  `events:` keys along with everything else, because both enumerate-then-classify
  rather than constructing candidate keys. No change is needed in either for that
  to keep working — only `parse_analytics_key`'s `events:` branch must survive,
  which it does.
- **A sweep cannot reuse the existing purge's safety argument, so bolting it on
  would be wrong.** `handle_orphan_purge`'s load-bearing property is that it
  re-checks `exists("slug:<S>")` and therefore *never* deletes a live link's
  analytics. A sweep of retired `events:` keys must delete them for **live**
  links too — it inverts that property. If it is ever built it belongs behind
  its own endpoint, its own confirmation string and its own justification ("this
  key type is no longer written or read by anything"), never as a mode on the
  existing one.
- **On today's store the sweep would reclaim a rounding error.** 37 analytics
  keys total across 14 links, with `list_keys` already at its ~24 ms floor.
  Building a purge endpoint for that fails this repo's own standard of measuring
  before building.

So this plan ships the *measurement* instead: `totals.obsolete_event_keys` in
the orphan report (see "API changes"), surfaced in one sentence on the Store
maintenance page. `TASKS.md` gets a Future-work entry for the sweep with an
explicit trigger: **`obsolete_event_keys` above ~500, or a traced `list_keys`
inside `GET /api/links` above ~250 ms** (the same trigger
`docs/plans/derived-link-indexes.md` already records).

**What must not happen:** `parse_analytics_key` losing its `events:` branch. That
would reclassify every leftover key as `unrecognized`, which
`classify_analytics_keys` deliberately never purges — so the keys would become
permanently unremovable *and* would show up in the orphan report's
`unrecognized_sample` on every run, exactly the "a checker that fires on healthy
state gets ignored" failure this codebase avoids everywhere else.

## Test changes

**Retired with the feature** (8 Go, ~4 Python):

- Go: `TestFormatEvent`, `TestClassifyUserAgent`, the five `TestEventSlot_*`,
  `TestEventKey`.
- Python: `api/tests/test_analytics.py`'s `test_analytics_reports_count_and_events`
  (reduce to counts only, or retire in favour of the existing count-only test),
  the malformed-event test, `test_analytics_only_reads_configured_number_of_slots`,
  and the `recent_events == []` assertion in the empty case. Every
  `handle_analytics(...)` call site in that file drops its `30` argument.
- `api/tests/test_store_isolation.py`: four call sites pass
  `num_event_slots=30` to backup handlers; drop the keyword.

**Kept, and now more load-bearing than before:**

- `TestShardFor_DistributesUniformlyOverTimestampShapedInput` — becomes the sole
  pin on the anti-aliasing property. Mutation-verified as a task criterion.
- `api/tests/test_click_totals.py::test_events_keys_are_never_read` — leftover
  `events:` keys still exist; reading them would still be wrong.
- `api/tests/test_analytics_orphans.py`'s `events:` fixtures — these now pin the
  *cleanup* path for a retired key type rather than a live one. Their docstrings
  should say so.
- `api/tests/test_kvprefix.py`'s cross-language guard — unaffected by removing
  `EventKey`, but confirm rather than assume: it parses `keys.go` textually.

**Added:**

- `api/tests/test_analytics_orphans.py`: a test that
  `build_orphan_report`'s `totals["obsolete_event_keys"]` counts `events:` keys
  belonging to **both** a live slug and a deleted one — a version that only
  counts orphans would pass a naive implementation and miss the whole point.
- `api/tests/test_analytics.py`: a test that the 200 body's keys are exactly
  `{"total", "days"}`, so `recent_events` cannot creep back in silently.

`Jenkinsfile` is **not** in scope — the three test commands are unchanged.

## Trade-offs and rejected alternatives

### 1. Keep the events write; pursue the ceiling another way (the "do nothing" option) — REJECTED, but it was live

Genuinely live, and stronger than it looks. Neither documented trigger has
fired: traffic is nowhere near 25 clicks/second, and the store's `list_keys` is
at its irreducible floor. This repo's own discipline is *measure, then act*, and
applying it consistently would say wait. And what is given up — the only
referrer/device data in a product whose primary persona is campaign marketers —
is a real capability, not an internal detail.

It lost on three counts. **First, the failure mode of the thing being fixed is
silent.** Exceeding the write cap does not error; it under-counts the exact
number the analytics feature exists to report, permanently (a 25-request burst
recorded 19 and was still 19 a minute later). A limit you cannot see yourself
hit is worth buying headroom against *before* you hit it, which is the opposite
of the usual measure-first argument. **Second, the traffic shape that breaks it
is this audience's normal work** — a campaign email send or an ad going live
produces exactly the short, sharp burst that clears 25 clicks/second, and the
only other lever (asking Akamai for a write-rate increase) is outside our
control and is the same lever from the other side. **Third, what is lost is much
smaller than "the referrer data" makes it sound**: a ≤30-entry ring, blind-overwritten,
that the dashboard never reads, that answers "where did this traffic come from"
anecdotally rather than quantitatively, and that — per `TASKS.md` — was
arithmetically broken from its creation until 2026-08-07 and then broken again
by a `TypeError` from 2026-08-10 to 2026-08-11. It has never carried a
persona-validated requirement, and `PRODUCT.md`'s own "Evidence on Hand" is
"None."

**What would flip this decision:** evidence that the marketing team actually
reads that table. There is no telemetry and the deployment is a test site, so
that is a product judgement, not a measurement — it is the one open question
flagged for the user below.

### 2. Disable it reversibly with `analytics_event_slots = 0` — REJECTED

Attractive on its face: an off switch, no code deleted, recoverable without a
deploy of new code. It fails on every part of that:

- **It does not actually disable the write.** `EventSlot(now, 0)` → `ShardFor(_, 0)`
  → `0`, so the component keeps writing `events:<slug>:0` on every click. The
  write cap benefit — the headline — is not delivered at all. Making it an off
  switch requires a code change in `recordClickEvent`, at which point "no code
  change" is gone.
- **It lands squarely in the partial state the brief warns about.** The read side
  (`range(num_event_slots)`) yields no keys at 0, so the detail page shows
  **"No recent events yet" forever, on every link** — which reads as broken
  rather than as removed. Fixing that needs the API to publish an
  events-enabled flag and the GUI to branch on it: more surface, not less.
- **It leaves dead code plus a config-dependent behaviour difference**, which
  this repo has explicitly disliked before — see `TASKS.md`'s rejection of a
  config seam toggling one-store vs. three-store mode, on the reasoning that "a
  deployed-vs-developed configuration mismatch is a worse property to own" than
  the thing it was preserving.
- **Reversibility is worth less than it seems.** The ring is a 30-slot sample,
  not history: turning it back on starts producing events immediately, and
  turning it off never destroyed any. Re-adding the write later is a small,
  git-recoverable diff (`git show` the removal commit), which is about as
  reversible as the flag would have been.

### 3. Sample the events write (write it 1 click in N) — REJECTED

The obvious middle ground: 1-in-4 gives 1.25 writes/click and ~40 clicks/second
while keeping some referrer data. Rejected on the flaw `TASKS.md`'s original
entry already identified and which nothing has since answered: **an unconditional
sample thins the recent-events list most for low-traffic links, which are
precisely the links whose events are worth reading** — a link with 8 clicks
would record 2 events. A rate-adaptive sample ("always write below some rate")
needs to know the rate, which needs state, which needs a read, which is circular
on the hot path. It also keeps 100% of the code, the tests, the GUI and the
maintenance surface for a fraction of the data, which is the worst of both
columns.

### 4. Stop writing but keep reading (drop only `recordClickEvent`) — REJECTED

Cheapest possible diff. Rejected outright: the detail page would show a frozen
list of pre-cutover events under a heading that says "Recent", which is worse
than showing nothing — it presents stale data as current — and, for links with
no leftover keys, "No recent events yet" permanently. This is the exact partial
state the brief calls out, and it is why the read-side and GUI removals are
sequenced **with or before** the write removal, never after.

### 5. Fold aggregate referrer-host and device-class counts into the existing `count:` blob — DEFERRED (out of scope, and the best answer to what is lost)

The strongest idea found while planning, and the reason removing the ring is
less costly than it first appears. The `count:<slug>:<shard>` blob is *already*
read-modify-written on every click, so adding `{"devices": {...}, "referrers": {...}}`
to it costs **zero additional KV operations** — no third write, and none of the
contention that killed the denormalised `analytics:total:<slug>` (see
`docs/plans/denormalised-click-total.md`), because it rides the existing sharded
write. It would replace a lossy 30-entry anecdote with real aggregate counts,
which is a *better* answer to "where did this traffic come from" than the table
being removed.

It is out of scope here by explicit instruction — it changes the counter blob —
and it carries real design questions that deserve their own plan: referrer
cardinality must be bounded (host only, capped map, an "other" bucket) or the
blob grows unbounded toward Akamai's 1 MB value ceiling; the per-click timestamp
("when") is lost, though it was never aggregated anyway; and `_merge_counts`
would need to merge the new maps across 64 shards. **Filed as Future work.**
Removing the ring now does not foreclose it, and does not make it harder.

### 6. Build the leftover-key sweep now — DEFERRED, with a trigger

Covered in full under "Leftover `events:` keys" above. Short form: the residual
is frozen rather than growing, decays as links are deleted, and is a rounding
error on today's store; a sweep must invert the existing purge's central safety
property so it cannot be a mode on that endpoint; and the measurement that would
justify it (`totals.obsolete_event_keys`) does not exist yet, so this plan adds
that instead of the tool.

## Tasks

The lines appended to `TASKS.md`, verbatim (that file is authoritative; the
builder ticks boxes only there):

```
- [ ] Baseline the click-loss knee on the deployed build (MUST run before the redirect change ships) — file(s): (none — measurement step) — done when: dev/click-load.sh has been run against the deployed app at 16, 22, 26 and 32 req/s over at least 16 distinct slugs, recorded-vs-issued clicks are captured for each rate, the rate at which loss first appears is written back into this task line, and the run is noted as deployed-only (local sqlite loses nothing and cannot answer this)
- [ ] Drop recent_events from the analytics API — file(s): api/analytics.py, api/app.py, api/backup.py, api/tests/test_analytics.py, api/tests/test_store_isolation.py — done when: handle_analytics takes no num_event_slots parameter and its 200 body has exactly the keys {total, days}, _parse_event is deleted, parse_analytics_key still returns ("event", slug) for an events: key with a docstring saying why, backup.handle_export/handle_restore no longer take the unused num_event_slots, grep -n analytics_event_slots api/ returns nothing, a new test pins the 200 body's key set, and cd api && uv run pytest passes
- [ ] Remove the Recent events table from the link detail page (must land with or before the redirect write removal, never after) — file(s): gui/links/detail.html, gui/links/detail.js, gui/app.js — done when: detail.html contains no events-table and no two-column .grid wrapper around the analytics tables, the Clicks per day block renders full width with its h3 intact, #click-accuracy-hint reads roughly 50 clicks per second, detail.js has no recent_events rendering, formatTimestamp has no precise parameter or branch, and cd gui-pages && uv run pytest passes
- [ ] Stop writing the events ring in redirect — file(s): redirect/main.go, redirect/linkgate/analytics.go, redirect/linkgate/keys.go, redirect/linkgate/analytics_test.go, redirect/linkgate/keys_test.go — done when: recordClickEvent, linkgate.EventSlot, FormatEvent, ClassifyUserAgent, containsAny and EventKey are all deleted, sendRedirectThenRecord no longer takes *http.Request and all three call sites are updated, ShardFor and mix64 remain with EventSlot's linear-over-the-modulus derivation moved onto ShardFor's doc comment, the import block is down to encoding/json and sort, cd redirect && go test ./linkgate/... passes, and replacing ShardFor's body with int((entropy * 2654435761) % uint64(numShards)) makes TestShardFor_DistributesUniformlyOverTimestampShapedInput FAIL before ShardFor is restored byte-identically
- [ ] Retire the analytics_event_slots Spin variable (depends on the api and redirect tasks above) — file(s): spin.toml — done when: grep -c analytics_event_slots spin.toml returns 0 (top-level [variables] and both [component.*.variables] blocks), a local spin up --build starts with no undefined-variable error and serves /r/{slug}, and cd gui-pages && uv run pytest passes
- [ ] Update dev/click-load.sh's writes-per-click arithmetic — file(s): dev/click-load.sh — done when: KV_WRITES_PER_CLICK is 1, the header comment and both over-cap warnings describe one write per recorded click and a ~50 requests/second measurement ceiling, the 2026-08-06 misread-probe caution is kept, and a run at 30 req/s no longer prints the at-or-above-cap warning
- [ ] Report leftover events keys in the orphan report — file(s): api/analyticsorphans.py, api/tests/test_analytics_orphans.py, gui/admin/backup.js — done when: build_orphan_report's totals carry obsolete_event_keys summed over BOTH orphan and live slugs, a test pins it against a store holding events keys for a live slug and a deleted slug (a version counting only orphans must fail it), the Store maintenance page's orphan summary names the count only when it is above zero, and cd api && uv run pytest passes
- [ ] Update CLAUDE.md, PRODUCT.md, README.md and DESIGN.md for the retired events write — file(s): CLAUDE.md, PRODUCT.md, README.md, DESIGN.md — done when: CLAUDE.md's Analytics section states one KV write per recorded click, the M1/M2 table's M2 row reads one write and ~50 clicks/second with M1 untouched, the recent-events subsection is marked retired-with-the-feature while keeping its derivation as the reason ShardFor uses a splitmix64 finalizer, the Security-tradeoffs bullet and the Akamai section's 50÷2≈25 arithmetic read ~50, the stale claim that analytics runs before http.Redirect is corrected to after via sendRedirectThenRecord, the redirect op profile reads 5 for a successful redirect and 2 for a miss, the two stale CountShards = 16 references read 64, PRODUCT.md and README.md no longer advertise a recent-events sample, and DESIGN.md's h3 example no longer cites Recent events
- [ ] Re-measure the click-loss knee on a deployed build carrying the change — file(s): (none — measurement step) — done when: the same four rates from the baseline task are re-run against the deployed build, the new knee is recorded beside the baseline in that task line, and an X-SS-Debug trace of one /r/{slug} shows set=1 with kv_ops=5 (2 open, 2 get, 1 set) against 6 before
- [ ] End-to-end manual verification of the dropped events write — file(s): (none — verification step) — done when: against spin up --build with NO --runtime-config-file (so KV persists to .spin/sqlite_key_value.db), a freshly created link clicked 10 times yields only analytics:count: keys and zero analytics:events: keys in that database, GET /api/links/{slug}/analytics returns total and days with no recent_events key, and links/detail.html renders a full-width Clicks per day table with no Recent events heading and a clean browser console
```

## Critical files

- `redirect/main.go`
- `redirect/linkgate/analytics.go`
- `redirect/linkgate/keys.go`
- `redirect/linkgate/analytics_test.go`
- `redirect/linkgate/keys_test.go`
- `api/analytics.py`
- `api/analyticsorphans.py`
- `api/backup.py`
- `api/app.py`
- `api/tests/test_analytics.py`
- `api/tests/test_analytics_orphans.py`
- `api/tests/test_store_isolation.py`
- `gui/links/detail.html`
- `gui/links/detail.js`
- `gui/app.js`
- `gui/admin/backup.js`
- `spin.toml`
- `dev/click-load.sh`
- `CLAUDE.md`
- `PRODUCT.md`
- `README.md`
- `DESIGN.md`

No new files.

## Verification

In execution order.

1. **Baseline the deployed knee, before any code lands.** This cannot be
   recovered afterwards, and it cannot be taken locally (sqlite loses no clicks
   at any rate).

   ```bash
   ./dev/click-load.sh https://<app-id>.fwf.app 16 160 <slug1> <slug2> ... <slug16>
   ./dev/click-load.sh https://<app-id>.fwf.app 22 220 <slug1> ... <slug16>
   ./dev/click-load.sh https://<app-id>.fwf.app 26 260 <slug1> ... <slug16>
   ./dev/click-load.sh https://<app-id>.fwf.app 32 320 <slug1> ... <slug16>
   ```

   Read each link's total from `GET /api/analytics/click-totals` before and
   after. Use ≥16 distinct slugs so per-key contention (M1) is not what is being
   measured. **A pass is simply a recorded knee**, not a threshold: the rate at
   which recorded/issued first drops materially below 1.0.

2. **Go unit tests, plus the mutation check that keeps the anti-aliasing pin
   honest.**

   ```bash
   cd redirect && go test ./linkgate/...
   # then, temporarily, replace ShardFor's body with the old single multiply:
   #   return int((entropy * 2654435761) % uint64(numShards))
   cd redirect && go test ./linkgate/...   # MUST fail TestShardFor_DistributesUniformlyOverTimestampShapedInput
   git checkout redirect/linkgate/analytics.go   # no: restore only the mutated lines; re-apply the intended edits
   ```

   Never `go test ./...`, `go build ./...` or `go vet ./...` — all three fail by
   design on `package main`.

3. **Python suites.**

   ```bash
   cd api && uv run pytest         # baseline 648; expect a small net decrease
   cd gui-pages && uv run pytest   # 71, unchanged
   ```

4. **Full local run, with a persistent store so the keys can be inspected.**
   Note the flag is deliberately omitted — passing `--runtime-config-file` gives
   an in-memory store on Spin 4.0.2.

   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build
   ```

   Then, in a second shell: sign in through the login **form** (a raw fetch login
   produces `csrf_mismatch` 403s), create a link, and click it ten times:

   ```bash
   for i in $(seq 1 10); do curl -s -o /dev/null -w '%{http_code}\n' http://localhost:3000/r/<slug>; done
   sqlite3 .spin/sqlite_key_value.db \
     "select key from spin_key_value where key like 'analytics:%' order by key;"
   ```

   **Pass:** only `analytics:count:<slug>:<n>` rows; **zero** `analytics:events:`
   rows. This is the single most direct proof the write is gone.

5. **API shape.**

   ```bash
   curl -s -b cookies.txt http://localhost:3000/api/links/<slug>/analytics | python3 -m json.tool
   ```

   **Pass:** `total` and `days` present, `recent_events` absent entirely (not
   present-and-empty).

6. **Browser.** Load `http://localhost:3000/links/detail.html?slug=<slug>`.
   **Pass:** the Analytics article shows a single full-width "Clicks per day"
   table, no "Recent events" heading or table, the accuracy hint reads ~50 clicks
   per second, and the console is clean. Remember any `gui/` edit needs a
   `spin up` restart — if the page looks unchanged, `curl http://localhost:3000/links/detail.js`
   and diff against disk before doubting the edit.

7. **Store maintenance page.** Load `http://localhost:3000/admin/backup.html`,
   click Find orphaned analytics. **Pass:** the summary renders, and — on a store
   that still holds pre-change `events:` keys — names the obsolete-event-key
   count. `curl` the raw report to confirm `totals.obsolete_event_keys` is
   present and non-zero:

   ```bash
   curl -s -b cookies.txt http://localhost:3000/api/admin/analytics/orphans | python3 -m json.tool
   ```

8. **Deployed re-measurement**, after `spin aka app deploy --app-id <id> --build --no-confirm`
   with `app_version` set. Confirm the build is actually live via
   `curl -sI https://<app>/ | grep -i x-ss-version` before measuring — a request
   during the propagation window returns the old build and is actively
   misleading, and the CLI's 60-second readiness timeout is a known false
   negative. Then repeat step 1's four rates, and trace one redirect:

   ```bash
   curl -sI -H "X-SS-Debug: <token>" https://<app>/r/<slug>
   ```

   **Pass:** the logfmt line shows `kv_ops=5` with `open=2/… get=2/… set=1/…`
   (was 6 with `set=2/…`), and the knee has moved up from its baseline.
   Discard the first sample after idle.

## Out of scope / follow-ups

- **Anything touching the click counter** — the `count:` blob's shape, `CountShards`,
  the denormalised-total question. Excluded by instruction and independently
  argued in `docs/plans/denormalised-click-total.md`.
- **Removing `recordAnalytics`'s second `kv.Open`.** Measured at ~0.2% on
  Akamai; still not worth doing, and this change does not alter that.
- **Retrying the redirect's remaining analytics write under throttling.**
  Already rejected outright (`TASKS.md`, 2026-08-17), and that entry names *this*
  change as the only context in which it should ever be revisited. Revisiting it
  is still not warranted: the surviving write is best-effort by design, and
  retrying into a saturated cap worsens M2 for every other caller.
- **The `events:` key sweep.** Deferred with a trigger; goes to `TASKS.md`'s
  "Future work (not scheduled)".
- **Aggregate device-class and referrer-host counts in the `count:` blob.** The
  replacement for what this change removes, at zero extra writes; needs its own
  plan (cardinality bounding, `_merge_counts` changes, value-size headroom).
  Goes to "Future work (not scheduled)".
- **Enumeration growth generally.** This change bends the slope by roughly a
  third; it does not change the shape, and the ~250 ms `list_keys` trigger for
  `GET /api/links` recorded in `docs/plans/derived-link-indexes.md` stands
  unchanged.
- **Pagination for `GET /api/links`** — unrelated, already deferred in
  `docs/plans/links-pagination.md`.
