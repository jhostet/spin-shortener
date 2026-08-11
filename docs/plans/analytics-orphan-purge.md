# Orphaned Analytics Purge

## Context

`links.handle_delete` and `bulk.handle_bulk_action`'s delete path remove
`slug:<slug>` and rewrite the indexes. **Neither touches the analytics
namespace.** So every link ever deleted leaves behind up to
`linkgate.CountShards` (64) `count:<slug>:<shard>` keys, up to
`analytics_event_slots` (30) `events:<slug>:<slot>` keys, and possibly one
legacy unsharded `count:<slug>` key — 95 keys per link at saturation,
permanently, with nothing anywhere able to read them again.

That was a deliberate decision, argued at length in
`docs/plans/kv-consistency-check.md`'s rejected alternative #1 ("Checking the
`analytics` store"), whose load-bearing clause was: *"orphan analytics are
harmless in a way orphan links are not — nothing reads `count:<slug>` except
`GET /api/links/{slug}/analytics`, which 404s on the missing link first."*

**That clause is now false, and it was falsified by measurement.** Two facts
recorded on 2026-08-10:

1. **A key enumeration costs ~68.7 µs per physical key in the whole `default`
   store, linearly, R² = 0.997** (TASKS.md, "Measure the `list_keys` growth
   rate"; four store sizes spanning 10.4×, with `get` latency flat at
   25.4–28.6 ms across the whole run as the control). `GET
   /api/analytics/click-totals` runs on **every dashboard load** and makes one
   full-store `list_keys` that `gather_reads` cannot overlap, because no read
   can be issued until the key list exists.
2. **The live store is 964 keys, 943 of them analytics, and 907 of those
   (96%) belong to 28 slugs with no link record** — against 14 live links, only
   2 of which have any analytics at all. That is **62.3 ms of every dashboard
   load spent enumerating data that nothing can ever read.**

The usage profile makes this a permanent leak rather than a curiosity: the
user has confirmed the audience is **marketing staff who are not technical,
who will create a lot of links and delete many of them as they make mistakes
and correct**. Live links plateau; orphaned analytics grows without bound. At
a plausible 300 links created and 200 deleted per year, the orphan walk alone
reaches ~370 ms per dashboard load in year one and ~1.9 s by year five.

There is a second, non-performance reason, found while reading the code for
this plan: **a slug can be reused, and today it inherits the deleted link's
click history.** `CUSTOM_SLUG_PATTERN` permits any `[A-Za-z0-9_-]{3,32}`, so
deleting `spring-sale` and recreating it next year produces a brand-new link
whose `GET /api/links/spring-sale/analytics` sums the *old* link's 64 count
shards. Purging orphans removes that. It is not the motivating problem, but it
means this work fixes a correctness bug as well as a cost one.

Confirmed decisions the user settled before planning:

- The recent-events ring buffer **stays** (decided 2026-08-10) — dropping the
  `events:` write is not the fix being pursued here.
- Caching `click-totals` behind a TTL, lazy-loading the dashboard's Clicks
  column, and prefix-scoped enumeration are all rejected (see "Trade-offs").
- `redirect` is not to be changed, and what a click writes is not to be
  changed.
- Purging must be gated on `users.manage`, must be chunked, and must never
  delete analytics for a live link.

## Key technical facts confirmed during research

- **`links.handle_delete` writes exactly three keys and never opens
  analytics.** `api/links.py:352-360` — `store.delete(f"slug:{slug}")` then
  `remove_slugs_from_indexes`. `bulk.handle_bulk_action`'s delete branch
  (`api/bulk.py:377-382`) is the same shape.
- **`handle_click_totals` makes exactly one full-namespace enumeration per
  call** — `keys = await list_keys(analytics_store)` (`api/analytics.py:116`),
  and `kvprefix.scoped_list_keys` calls `raw_list_keys(store.raw)` and filters
  by prefix *afterwards* (`api/kvprefix.py:119-122`), so it walks the whole
  physical store. Confirmed by measurement, not inference: consistency's 3-key
  `users` namespace and 941-key `analytics` namespace cost the same ~62 ms.
- **A prefix-scoped or cursored enumeration does not exist.**
  `spin:key-value/key-value@3.0.0`'s `get-keys()` takes no arguments in either
  SDK (`Store.get_keys(self)`; Go's `(*Store).GetKeys()`). Recorded in
  TASKS.md's final section as CONFIRMED UNAVAILABLE.
- **Per-link analytics key ceiling is 95**: `CountShards = 64`
  (`redirect/linkgate/keys.go:42`) + `analytics_event_slots` default 30
  (`spin.toml:14`) + one legacy `count:<slug>`. Keys accumulate by
  coupon-collector, so a 10-click link holds ~18 and only a heavily-clicked
  link approaches the ceiling; the measured store averages ~32 keys per orphan
  slug (907 / 28).
- **Akamai: 50 KV writes/second app-wide, 1,000 reads/second, 30-second
  handler duration** (`techdocs.akamai.com/akamai-functions/docs/quotas-and-limits`,
  fetched 2026-08-04, table reproduced in CLAUDE.md).
- **A single KV data operation on Akamai costs ~20–26 ms, and that figure
  swings ~3× between windows minutes apart** (CLAUDE.md, "Redirect caching",
  measured 2026-08-06). A *sequential* delete loop therefore issues roughly
  38–50 writes/second on its own, which is why no artificial throttle is
  needed — but it also means a purge competes for the same app-wide write
  budget as live click recording.
- **`gather_reads` is reads-only by design** (`api/kvbatch.py:10-15`).
  `MAX_CONCURRENT_READS = 100` is empirical.
- **The GUI's consistency renderer is already contract-flexible.**
  `gui/admin/backup.js:249` renders ``All ${ran.length} check…`` from
  `report.checks.length`, and `consistencyCheckLabel` falls back to the raw
  check id. The "exactly twelve" contract is asserted in prose (CLAUDE.md,
  `api/consistency.py`'s docstrings) and in tests, not in the renderer. **This
  plan does not add a 13th check anyway** — see Trade-offs #1.
- **`api/tests/test_store_isolation.py` is the established home for
  cross-namespace hazard tests**, each building one physical `FakeStore`,
  wrapping it with `kvprefix.open_views`, and driving real handlers over the
  views.
- **Baseline confirmed green before planning:** `cd api && uv run pytest` →
  **530 passed in 11.69s**.
- **`gui/admin/backup.html` is the only page with more than one operator
  tool**, and `gui/admin/users.html:55` is the only inbound link to it. Adding
  a fourth article there requires **no new file and no new `spin.toml` route**,
  because `/admin/backup.js` is already routed (`spin.toml:133-135`).
- **DESIGN.md:250 records that `backup.html`'s breadcrumb measured a clean
  351/351 at 390 px**, and that a page label must be re-measured at 390 px if
  changed. Relevant to the optional retitle task below.
- **UNCONFIRMED: the projected post-purge saving.** Removing 907 of 964 keys
  should take `click-totals`' enumeration from ~64 ms to ~4 ms and its wall
  time from ~193 ms to ~131 ms (−32%), extrapolating the measured linear fit.
  The fit is good and the extrapolation is downward (inside the measured
  range), but it is a projection until a traced `click-totals` is taken on the
  deployed build after a real purge. That measurement is a task below.
- **UNCONFIRMED: purge wall time on Akamai.** Local `spin up` will complete a
  250-key chunk in milliseconds; the 5–15 s estimate below comes from the
  documented 20–26 ms/op figure and its 3× swing, not from a timed purge. The
  first deployed purge is the measurement.

## Data model

**No new KV key type, no new prefix, no bookkeeping key.** The purge is
stateless: it deletes keys and returns what it did. Nothing in
`api/kvprefix.py`'s `STORE_PREFIXES`, `api/backup.py`'s `INDEX_KEYS` /
`restore_write_order`, or `api/consistency.py`'s key-shape recognition
changes. That was a hard constraint and it is met exactly, which is the single
biggest reason the client-driven chunking design below beat a server-side
resumable cursor.

Existing analytics key shapes, all read-only to this feature:

| key | written by | shape |
|---|---|---|
| `count:<slug>:<shard>` | `redirect`'s `recordClickCount` | `{"total": n, "days": {...}}` |
| `count:<slug>` | nothing since sharding landed | same |
| `events:<slug>:<slot>` | `redirect`'s `recordAnalytics` | `"<unix_ms>\|<referrer>\|<device_class>"` |

**The purge deletes enumerated keys, never constructed ones.** It never builds
`count:<slug>:<n>` for `n in range(COUNT_SHARDS)`. That matters for three
reasons: it picks up the legacy unsharded key for free; it picks up
`events:<slug>:<slot>` keys written when `analytics_event_slots` was set
higher than it is now; and it is immune to `CountShards` ever being raised
again. A construct-then-delete design would silently leave keys behind in all
three cases, and leaving keys behind is the exact failure this feature exists
to fix.

## API changes

### `api/analytics.py` — one small extraction (must land first)

Add a pure, shape-only parser and use it in the existing endpoint, so exactly
one function in the codebase knows what an analytics key looks like:

```python
def parse_analytics_key(key: str) -> tuple[str, str] | None:
    """("count"|"event", slug) for a recognized analytics key, else None.

    Shape only — it does not judge whether `slug` is a *valid* slug, because
    handle_click_totals intersects against a known-visible set and must keep
    its current behaviour byte for byte. analyticsorphans.py applies
    links.is_valid_custom_slug on top before anything is deleted.
    """
    if key.startswith("count:"):
        kind, rest = "count", key[len("count:"):]
    elif key.startswith("events:"):
        kind, rest = "event", key[len("events:"):]
    else:
        return None
    slug = rest.split(":", 1)[0]
    if not slug:
        return None
    return kind, slug
```

`handle_click_totals`'s loop (`api/analytics.py:118-124`) becomes a call to
it. A slug can never contain a colon (`CUSTOM_SLUG_PATTERN`), so
`split(":", 1)[0]` is unambiguous for all three shapes — that is the same
reasoning already written into `handle_click_totals`'s comment, now stated
once.

Also in this task: **`links._all_slugs` loses its underscore** and becomes
`links.all_slugs`, following the `links.owned_slugs` precedent (CLAUDE.md:
"`links.owned_slugs` is public … the same 'shared, not module-private'
convention `can_view`/`can_edit` already carry"). `analytics._all_slugs_for_totals`
is deleted and its one call site uses `links.all_slugs`, removing a
line-for-line duplicate.

### New module: `api/analyticsorphans.py`

Named for what it holds, all-lowercase-no-underscore like `urlpolicy.py`,
`kvprefix.py`, `kvbatch.py`. **Not `orphans.py`** — `consistency.py` already
owns "orphan" for index drift (`orphan_session`,
`orphan_owner_index_entry`), and the two must not be confusable. **Not a
stdlib name**, per CLAUDE.md's `api/obs.py` rule.

Zero `spin_sdk` imports. Takes `store` views, `request` and the `list_keys`
callable as plain parameters. Imports `analytics`, `links`, `kvbatch` and
`responses` — the dependency direction is `analyticsorphans → analytics →
links`, matching `bulk → links`, with no cycle.

Constants:

```python
MAX_PURGE_SLUGS = 50              # == bulk.MAX_BULK_ROWS, same reasoning
MAX_PURGE_KEYS_PER_REQUEST = 250  # the write budget; see the arithmetic below
MAX_ORPHAN_SLUGS_REPORTED = 100   # == consistency.MAX_FINDINGS_PER_CHECK
MAX_UNRECOGNIZED_SAMPLE = 20
PURGE_CONFIRMATION = "PURGE"
ORPHANS_FORMAT = "spin-shortener-analytics-orphans"
SCHEMA_VERSION = 1
```

**Why `MAX_PURGE_KEYS_PER_REQUEST = 250`, with the arithmetic that produced
it.** 250 sequential deletes is 5 seconds at the app-wide 50 writes/second
cap, and 5–6.5 s of wall clock at the measured 20–26 ms per write operation.
CLAUDE.md documents 3× latency regime swings between windows minutes apart, so
the pessimistic case is ~15–19 s — still inside Akamai's 30-second handler
limit with real headroom, which a 400- or 500-key budget would not have (at a
3× swing, 400 keys is ~30 s, i.e. exactly the limit). It is also whole-slug
granular: at the 95-key ceiling a chunk carries 2 saturated links, and at the
measured 32-keys-per-orphan average it carries ~7.

It is a plain module constant, not a Spin variable, on the same reasoning
`MAX_BULK_ROWS` and `MAX_FINDINGS_PER_CHECK` carry: one function in one
component reads it, and it expresses what a single `componentize-py` request
can do, not operator policy. **Raising it needs real timing evidence from a
full-cap purge on Akamai, not a hunch** — the same rule `MAX_BULK_ROWS` and
`MAX_BACKUP_ENTRIES` carry. The response echoes `max_keys_per_request`, so no
client ever hardcodes it.

#### Pure functions

```python
def classify_analytics_keys(keys: list[str]) -> tuple[dict[str, dict], list[str]]:
    """({slug: {"keys": [...], "count_keys": n, "event_keys": n}}, unrecognized).

    A key whose shape parse_analytics_key does not recognise, OR whose slug
    fails links.is_valid_custom_slug, goes to `unrecognized` and is therefore
    never purgeable. That is deliberate and it is the safety valve: a future
    analytics key type must show up as something a human is told about, never
    as something this feature quietly deletes.
    """

def split_by_liveness(by_slug: dict, live_slugs: set[str]) -> tuple[dict, dict]:
    """(orphans, live), partitioned on membership in live_slugs."""

def build_orphan_report(orphans, live, unrecognized, *,
                        analytics_key_count, generated_at, generated_by) -> dict

def plan_purge(orphans: dict[str, dict], slugs: list[str], budget: int
               ) -> tuple[list[str], list[str], list[str]]:
    """(slugs_to_purge, keys_to_delete, remaining_slugs).

    Whole slugs only, biggest first (sort key: (-key_count, slug)) so a
    bounded budget reclaims the most keys it can and two runs over the same
    input produce byte-identical plans.

    INVARIANT: at least one slug is always planned, even if its own key count
    exceeds `budget` alone. Without that, a store holding a slug with more
    keys than the budget (possible if analytics_event_slots was once set very
    high) would make every request purge nothing and the GUI's loop would
    never terminate.
    """
```

`plan_purge` is where the entire chunking contract lives, and it is pure —
which is the point: the budget, the ordering and the never-stall invariant are
all unit-testable with no store at all.

#### `GET /api/admin/analytics/orphans`

```python
async def handle_orphan_report(links_store, analytics_store, principal, list_keys) -> Response
```

1. `users.manage` gate, returning `users.py`'s exact `_forbidden()` body:
   `403 {"error": "forbidden", "required_permission": "users.manage"}`.
2. `keys = await list_keys(analytics_store)` — **one** enumeration.
3. `classify_analytics_keys(keys)`.
4. `live = set(await links.all_slugs(links_store))` — **one** `get`. If the
   value is present but not a JSON list of strings, return
   `409 {"error": "links_index_unreadable", "next_step": "consistency_check"}`
   and read nothing further. Failing closed matters: an unreadable index would
   otherwise make every link look deleted.
5. `split_by_liveness`, then `build_orphan_report`.

**Total cost: 2 KV operations, regardless of how many orphans exist.** That is
deliberate and it is what makes the report cheap enough to offer as a plain
button. The report is *not* where the safety guarantee lives — see below.

Response:

```json
{
  "format": "spin-shortener-analytics-orphans",
  "schema_version": 1,
  "generated_at": "2026-08-10T18:00:00Z",
  "generated_by": "admin",
  "scanned": {"analytics_keys": 943, "live_slugs": 14},
  "totals": {"orphan_slugs": 28, "orphan_keys": 907, "live_keys": 34, "unrecognized_keys": 2},
  "truncated": true,
  "max_orphan_slugs": 100,
  "orphans": [{"slug": "spring-sale", "keys": 95, "count_keys": 64, "event_keys": 31}],
  "unrecognized_sample": ["totals:weird"]
}
```

`totals` are always exact even when `orphans` is truncated — the
`MAX_FINDINGS_PER_CHECK` rule from `consistency.py`, for the same reason: a
capped list must never read as complete. `orphans` is sorted by
`(-keys, slug)`.

#### `POST /api/admin/analytics/purge`

```python
async def handle_orphan_purge(links_store, analytics_store, principal, request, list_keys) -> Response
```

Request body: `{"confirm": "PURGE", "slugs": ["spring-sale", "old-promo"]}`.

Validation, in this order, all before any I/O, all all-or-nothing (nothing is
written if any of them fails):

| condition | response |
|---|---|
| no `users.manage` | `403 {"error": "forbidden", "required_permission": "users.manage"}` |
| body is not JSON | `400 {"error": "invalid_json"}` |
| `confirm != "PURGE"` | `400 {"error": "confirmation_required", "expected": "PURGE"}` |
| `slugs` absent / not a list / empty / any non-string | `400 {"error": "no_slugs"}` |
| duplicate slug | `400 {"error": "duplicate_slug"}` |
| `len(slugs) > MAX_PURGE_SLUGS` | `400 {"error": "too_many_slugs", "max_slugs": 50, "slug_count": N}` |
| any slug fails `links.is_valid_custom_slug` | `400 {"error": "invalid_slug", "slug": "…"}` |

The slug-pattern check is a security control, not tidiness: it is what makes
it impossible for a caller to submit a crafted string that widens the set of
keys the classifier attributes to "their" slug. Every real slug matches
`CUSTOM_SLUG_PATTERN` (generated slugs are 7 characters of
`ascii_letters + digits`; custom ones are validated against that pattern at
creation), so the check rejects nothing legitimate.

Then:

1. **Liveness, verified against the record and nothing else.**
   `live = await gather_reads(links_store.exists(f"slug:{s}") for s in slugs)`
   — a bounded, order-preserving read fan-out of at most 50. Any slug whose
   record exists is **skipped**, with `{"slug": s, "reason": "link_exists"}`.
2. One `list_keys(analytics_store)` enumeration, `classify_analytics_keys`,
   restricted to the non-live submitted slugs. A submitted slug with no
   analytics keys at all is skipped with `reason: "no_analytics_keys"` (the
   normal outcome of re-submitting an already-purged slug — the idempotent
   case).
3. `plan_purge(...)` against `MAX_PURGE_KEYS_PER_REQUEST`.
4. **Delete sequentially — `for key in keys_to_delete: await
   analytics_store.delete(key)`. Never `gather_reads`, never
   `asyncio.gather`.** Deletes are writes; writes are cap-bound at 50/second
   app-wide while reads have 1,000/second of headroom, so gathering them would
   queue against the cap rather than overlap, and risks throttling. This is
   the same rule `backup.handle_restore`'s write loop and every bulk handler
   already follow, and the module docstring must say so, because it is the one
   rule a well-meaning future optimisation is most likely to break.

Response:

```json
{
  "ok": true,
  "purged_slugs": ["spring-sale"],
  "deleted_keys": 95,
  "remaining_slugs": ["old-promo"],
  "skipped": [{"slug": "still-here", "reason": "link_exists"}],
  "complete": false,
  "max_keys_per_request": 250
}
```

`complete` is `not remaining_slugs`. A skipped slug is never in
`remaining_slugs` — it is finished, either because it must not be touched or
because there was nothing to touch. The client loops by re-POSTing
`remaining_slugs`.

**There is no write ordering rule to state, and that is worth saying
explicitly**, because every other mutating path in this codebase has one.
Analytics keys have no index — `backup.INDEX_KEYS["analytics"]` is `()` — so
there is nothing that could be left advertising a key that no longer exists.
A purge interrupted halfway through a slug leaves that slug's remaining keys
orphaned exactly as they already were, and the next run collects them.

#### The race, stated precisely

Spin KV has no transaction and no compare-and-swap, so the question is not
whether a race exists but what it costs. The window is between step 1's
`exists` and step 4's delete of that slug's keys.

- **A live link's analytics can never be deleted by a lost race.** The key
  list comes from an enumeration taken *after* the liveness check, and every
  key deleted belongs to a slug whose `slug:` record was confirmed absent
  moments earlier.
- **The one reachable bad outcome:** an operator purges `spring-sale` and, in
  the same few seconds, someone recreates a link with that exact slug and it
  is clicked. The purge can then delete a count shard or event slot that the
  brand-new link had just written into. The cost is a handful of seconds' worth
  of clicks on a link created during a maintenance window; the new link keeps
  working, and its counter simply starts from the next click. It is not
  possible for the new link to lose *history*, because it has none.
- **Not mitigated further, deliberately.** Re-checking `exists` immediately
  before each slug's deletes would narrow the window from the request to
  ~2 seconds without closing it, at the cost of one extra read per slug and a
  second place where liveness is decided. Narrowing an unclosable window is
  not worth a second decision point.

**The report can be misled by index drift; the purge cannot be.** The report
derives liveness from `all_links` (cheap, O(1) operations), so an
`unindexed_link` — a real, documented drift mode that `consistency.py`'s check
1 exists to find — would appear in the report as an orphan. The purge derives
liveness from `exists("slug:<S>")`, i.e. from the record itself, so it refuses
that slug and names it in `skipped` with `reason: "link_exists"`. This split is
the design: the cheap path can be wrong, the destructive path cannot.

### `api/app.py` wiring

Two exact-path branches, placed with the other `/api/admin/...` routes (after
`/api/admin/consistency`, before `/api/admin/url-policy/violations`):

```python
if path == "/api/admin/analytics/orphans" and method == "GET":
    result = await _require_session(users_store, request)
    if isinstance(result, Response):
        return result
    return await analyticsorphans.handle_orphan_report(
        links_store, analytics_store, result, list_keys,
    )

if path == "/api/admin/analytics/purge" and method == "POST":
    result = await _require_session(users_store, request)
    if isinstance(result, Response):
        return result
    return await analyticsorphans.handle_orphan_purge(
        links_store, analytics_store, result, request, list_keys,
    )
```

Both are under `/api/admin/`, which no slug can ever reach, so neither carries
the shadowing hazard that pushed click totals to `/api/analytics/click-totals`
(`api/app.py:216-220`). `analytics_event_slots` is deliberately **not** read
for either handler — the purge enumerates rather than constructs, so the slot
count is irrelevant to it.

## GUI changes

### `gui/admin/backup.html` — a fourth `<article>`

Placed after the consistency article, since it is the one tool that acts on
what a diagnostic finds. No new file, no new `spin.toml` route, no new CSS,
no new design token — it reuses `.form-error`, `.form-success`, `.form-note`,
`.finding-list` / `.finding-field` / `.finding-key` (all already in
`gui/theme.css`) and `slugChip(slug)` **without** `{linked: true}`, because
these slugs have no detail page to link to.

```html
<article>
  <h2>Clean up analytics for deleted links</h2>
  <p>
    Deleting a link doesn't delete its click data. That data can never be read
    again — the link is gone — but every key in the store is scanned on every
    dashboard load, so it makes the app slower for everyone, forever.
  </p>
  <p class="form-note">
    Deleting it can't be undone, and restoring an older backup brings it back.
    Run this when traffic is low: it shares the same write budget as click
    recording.
  </p>
  <button type="button" id="orphans-btn" class="outline">Find orphaned analytics</button>
  <p id="orphans-error" class="form-error" role="alert"></p>
  <div id="orphans-result" aria-live="polite"></div>
</article>
```

The purge button, the progress line and the stop button are rendered into
`#orphans-result` by the script once a report exists — a destructive control
that appears only alongside the numbers justifying it, which is the same
posture the restore article takes (its button starts `disabled`).

### `gui/admin/backup.js` — report, then a chunked loop

- `PURGE_ERROR_MESSAGES`, a **call-site override map** passed as
  `friendlyError`'s third argument, exactly as its docstring intends ("lets one
  call site's copy win over the shared map"). This is not optional
  tidiness: `BACKUP_ERROR_MESSAGES.confirmation_required` reads *"Type REPLACE
  exactly to confirm"*, which would be actively wrong copy for a purge whose
  confirmation is set programmatically. New codes to cover:
  `links_index_unreadable`, `too_many_slugs`, `invalid_slug`, `no_slugs`,
  `duplicate_slug`.
- On report: render the headline number first — *"907 of 943 analytics keys
  belong to 28 links that no longer exist."* — then the slug list with key
  counts, then `Showing the first 100 of 28…` style truncation copy borrowed
  verbatim in shape from `renderConsistencyCheck`. A clean result renders one
  `.form-success` line and no purge button.
- On purge: a `confirmDialog` naming **both** exact totals from the report
  (`orphan_keys`, `orphan_slugs`), e.g. *"Permanently delete 907 analytics keys
  for 28 deleted links? This can't be undone."* with
  `{ confirmLabel: "Delete analytics" }`. Count-bearing, per the restore
  precedent.
- Then the loop, which is **new to this codebase** — no existing page batches
  its API calls:

  ```
  slugs = report.orphans.map(o => o.slug)
  while (slugs.length && !stopped):
      chunk = slugs.slice(0, 50)
      res = POST /admin/analytics/purge {confirm: "PURGE", slugs: chunk}
      if (!res.ok) -> show friendlyError, stop
      deleted += res.data.deleted_keys
      slugs = res.data.remaining_slugs.concat(slugs.slice(50))
      update #orphans-progress: "Deleted N of M keys…"
  if (report.truncated && !stopped): re-fetch the report and continue
  ```

  A **Stop** button sets `stopped` and the loop finishes after the in-flight
  request. Stopping is safe by construction: the operation is idempotent and
  every completed chunk is independently correct. Both the Find and Delete
  buttons are `disabled` for the duration.
- Following the truncated report is what makes one click finish the job on a
  store with thousands of orphans, and it is honest because the dialog quoted
  the *exact* total (`totals` are never truncated) before the first request.

### Optional, independently landable: retitle the page

`gui/admin/backup.html` will host four operator tools while its `<h1>`,
`<title>` and breadcrumb all say "Backup and restore". TASKS.md's Future-work
entry *"Renaming `gui/admin/backup.html` to something naming operator
maintenance generally"* names its trigger as "a fourth operator tool landing
there, at which point the page's own title and nav label are already lying".
**That trigger fires with this change.**

Take the cheap half only: change the `<h1>`, the `<title>`, `initHeader`'s
`pageLabel` to **"Store maintenance"**, and `gui/admin/users.html:55`'s anchor
text. **Do not rename the file.** A file rename needs a new
`gui-pages/routing.py` `ROUTES` entry, a new `spin.toml` route for the renamed
`.js`, edits to every inbound link, and breaks any bookmark — for no
functional gain. The remaining half stays deferred; a note goes under Future
work saying which half was taken.

DESIGN.md:250 requires a new page label to be measured at 390 px before it is
assumed to fit. "Store maintenance" is 17 characters against the current 19,
and `backup.html` measured a clean 351/351, so it should be safe — but it is
still a verification step, not an assumption.

## Redirect (Go) changes

**None.** `redirect` is untouched: no new file, no changed file, no rebuild
semantics, and the hot path stays at 6 KV operations. It never reads a link
list, never enumerates, and never consults anything this feature adds. The
`redirect/linkgate/keys.go` constants (`CountShards`, the key builders) are
read only as documentation here — the purge deletes enumerated keys rather
than constructing them, so it does not even depend on `CountShards` being
correct.

## Trade-offs and rejected alternatives

### 1. Making this a 13th consistency check — rejected, against the brief

The requested shape was *"report orphaned analytics, most naturally by
extending `api/consistency.py`"*, flagged with the twelve-check contract
problem. The contract is the smaller issue; the argument against is stronger
than that.

`docs/plans/kv-consistency-check.md`'s rejected alternative #1 gave three
reasons for excluding the analytics store, and states its own revisit trigger
as **"deletion being changed to purge analytics. Then 'an orphan analytics
key' would become a genuine anomaly rather than the expected outcome."** This
plan deliberately does *not* change deletion (see #3). Orphans therefore remain
expected, normal, intended state between purges — so a check over them would
pin `ok: false` on a structurally flawless store on every deployment that has
ever deleted a link and has not purged in the last five minutes. That is
precisely "a diagnostic that always finds something is a diagnostic nobody
reads."

CLAUDE.md already applies this exact reasoning once, to the destination-policy
violations endpoint: *"Violations are deliberately not a thirteenth
consistency check: `consistency.py` is scoped to structural drift, a policy
finding would pin `ok: false` on a structurally flawless store, and its
're-run to confirm' and 'never repairs' framings are both wrong for policy."*
Every clause transfers. The "re-run to confirm" framing is wrong here too, and
worse: orphan findings are perfectly stable, so telling an operator to re-run
teaches them the advice is noise. The "it reports and never repairs" framing is
wrong in the opposite direction — the whole point of this feature is the
repair.

The precedent therefore also supplies the answer: **a separate endpoint pair
plus its own article, exactly like `/api/admin/url-policy/violations`.**

**It also avoids a collision.** TASKS.md's Future work already holds a
different proposed 13th check — physical keys under no known prefix — which
*is* structural drift and does belong in `consistency.py` when its own trigger
(an unprefixed key actually appearing) fires. Spending the 13th slot on
something that does not belong there would make that entry's landing more
confusing, not less.

### 2. A server-side resumable purge with a cursor key — rejected

**Attractive because** the client would make one call and the server would
manage its own progress, and because "resumable" sounds strictly better than
"the client loops".

**Why it loses.** It needs a new KV key type, which CLAUDE.md prices at three
mandatory obligations (`backup.py`'s `INDEX_KEYS`/`restore_write_order`,
`consistency.py`'s key-shape recognition, and a `STORE_PREFIXES` prefix), all
for bookkeeping with no user-visible value. Worse, it needs a *mutable*
cursor with no compare-and-swap available anywhere, so two operators — or one
operator with two tabs — would corrupt each other's progress with no way to
detect it. And the resumability it buys is already free: a purge is idempotent
and the report is re-runnable, so "resume" means "click the button again". The
client-driven loop gets every benefit and adds no state.

### 3. Purging inline in `handle_delete` / bulk delete — rejected, checked rather than assumed

**Attractive because** orphans would never accumulate at all and no operator
tool would be needed.

**Why it loses, with the arithmetic.** A single-link delete writes 3 keys
today. Purging inline would add up to 95 deletes — at 20–26 ms per write on
Akamai, roughly **2–2.5 seconds added to every link deletion**, and ~95 writes
against a 50/second app-wide cap, so one deletion would consume about two
seconds of the entire application's write budget while live clicks compete for
it. It would also add a full-store enumeration (~66 ms today, ~690 ms at
10,000 keys) to a request that currently makes none.

For **bulk delete it is not merely expensive, it is impossible**: 50 links ×
95 keys = 4,750 writes ≈ **95 seconds** at the cap, against a 30-second
handler limit. The request would be killed mid-write, which turns a currently
reliable feature into one that half-works. The user's own analysis said this
could not fit; it does not, and the confirming number is 95 s against 30 s.

A conditional variant ("purge inline only when the link has few analytics
keys") was considered and dropped in one line: knowing how few requires the
enumeration, which is most of the cost, and a delete whose duration depends on
click history is worse to reason about than one that never purges.

### 4. Client sends the key list instead of the slug list — rejected

**Attractive because** it removes the per-request enumeration entirely: the
report already knows every key, so the purge could just delete what it is
handed. At 40 chunks that saves 40 full-store walks.

**Why it loses.** The server would be deleting strings supplied by a client,
so it would have to re-derive the slug from each key and re-check liveness
anyway — which is the same work, minus the guarantee that the key actually
exists and belongs where it appears to. And a key list from an earlier report
goes stale: clicks recorded between report and purge write *new* shard keys
that a stale list does not contain, so the purge would leave keys behind and
the operator would see the count fail to reach zero. Enumerating per request
costs ~66 ms against a 5–15 s request — about 1% — and buys exactness plus a
smaller trust surface.

### 5. All-or-nothing on the purge, matching every other bulk endpoint — rejected

**Attractive because** CLAUDE.md is emphatic that bulk create and bulk action
are all-or-nothing, and consistency of posture is worth something. The stated
reason is that *"a partial-success design would leave the user needing to diff
what they submitted against what exists just to know what to retry."*

**Why it loses here.** That reason does not apply: the response names exactly
which slugs were purged, which remain, and which were skipped and why, so
there is nothing to diff. And the alternative is worse in two concrete ways.
First, a 250-key budget means a 50-slug submission will routinely be partial by
design — making that an error would make the endpoint unusable. Second, a
*fatal* `link_exists` would stall the GUI loop permanently: the report keeps
offering the drifted slug, the purge keeps rejecting the whole batch, and the
operator can never finish. So the line is drawn deliberately: **input
validation is all-or-nothing (nothing is written if the request is malformed);
state-dependent per-slug outcomes are reported and skipped.**

### 6. Requiring a typed confirmation string in the GUI, as restore does — rejected

**Attractive because** the prompt named restore's posture as the model, and
this is irreversible deletion.

**Why it loses.** The server-side `{"confirm": "PURGE"}` gate is kept — that is
the one that matters, since the endpoint is reachable by `curl`, and it is
exactly restore's argument. But the *typed field* is calibrated to restore's
blast radius: everything, including the users store and the operator's own
session, with no undo and no partial. A purge deletes data that is already
unreachable, for links that no longer exist, from a list the operator is
looking at, in a dialog quoting both exact totals. Adding a typing ritual to
a lesser action trains operators to type through rituals, which makes the
restore field weaker. A count-bearing `confirmDialog` is the right rung.

### 7. Caching `click-totals` behind a TTL — rejected (user's pre-decision, agreed)

It hides a walk that keeps growing rather than shrinking it; the recompute
inherits the full cost, and it would need cache-invalidation state that Spin's
KV cannot maintain atomically. Recorded here because it is the first thing
anyone reaches for.

### 8. Dropping the `events:` ring-buffer write — rejected (user's pre-decision, agreed)

It would cut analytics key growth by roughly a third and double the app-wide
click ceiling, and it has a standing Future-work entry with two independent
justifications. But it only slows accumulation; it never removes what already
exists, and the user decided on 2026-08-10 that recent events stay. The two
are complements, not alternatives — if that entry is ever taken, this feature
still does the removing.

### 9. Lazy-loading the dashboard's Clicks column — rejected (user's pre-decision, agreed)

It undoes a feature shipped 2026-08-10, and does nothing for
`backup.handle_export` or `consistency.collect`, which walk the same store.

### 10. Prefix-scoped enumeration — rejected, confirmed impossible

`get-keys()` takes no arguments in either SDK. Nothing to scope. Recorded in
TASKS.md as CONFIRMED UNAVAILABLE.

### 11. Reporting each orphan slug's click total — rejected

**Attractive because** "you are about to erase 4,120 clicks of history" is a
more honest confirmation than a key count.

**Why it loses.** It costs one gathered read per orphan *count* key — ~900
reads on the measured store — to produce a number nobody can act on: the link
is gone, the data is already unreachable through every UI in the app, and no
choice changes based on the answer. The key count is the honest unit here,
because keys are what the enumeration walks and what the cost is measured in.

### 12. Do nothing — live, and rejected

Genuinely live: the app works, and 62 ms per dashboard load is not a
user-visible problem today. It loses on trajectory rather than on current
state. The growth is linear and measured (R² = 0.997), the audience is
explicitly one that creates and deletes heavily, and the cost lands on the
single most frequent authenticated request in the app. At ~14,100 keys the
enumeration alone is ~1 second per dashboard load; there is no natural ceiling,
and every day of doing nothing adds keys that will still need deleting at the
same 50 writes/second when someone finally does it.

## Tasks

The lines appended to TASKS.md under `## Orphaned analytics purge`, mirrored
here for readability. TASKS.md is authoritative; check the boxes only there.

```
- [ ] Extract parse_analytics_key and make links.all_slugs public (must land before the new module) — file(s): api/analytics.py, api/links.py, api/tests/test_click_totals.py — done when: analytics.py has one shape-only parse_analytics_key used by handle_click_totals, links._all_slugs is renamed links.all_slugs with every call site updated, analytics._all_slugs_for_totals is gone, and `cd api && uv run pytest` still passes all 530 existing tests
- [ ] Add api/analyticsorphans.py's pure layer (depends on the extraction above) — file(s): api/analyticsorphans.py (new), api/tests/test_analytics_orphans.py (new) — done when: classify_analytics_keys, split_by_liveness, build_orphan_report and plan_purge exist with zero spin_sdk imports, and tests pin: the legacy unsharded count key is classified, an unrecognized-shape key and a key whose slug fails is_valid_custom_slug both land in `unrecognized`, plan_purge is whole-slug and biggest-first, and plan_purge always plans at least one slug even when that slug alone exceeds the budget
- [ ] Add GET /api/admin/analytics/orphans (depends on the pure layer) — file(s): api/analyticsorphans.py, api/tests/test_analytics_orphans.py — done when: the handler makes exactly 2 KV operations regardless of orphan count (pinned by a RecordingStore test), returns 403 without users.manage, returns 409 links_index_unreadable for a malformed all_links, and reports exact totals alongside a list truncated at MAX_ORPHAN_SLUGS_REPORTED
- [ ] Add POST /api/admin/analytics/purge (depends on the pure layer) — file(s): api/analyticsorphans.py, api/tests/test_analytics_orphans.py — done when: tests pin 403 without users.manage, 400 for each of confirmation_required/no_slugs/duplicate_slug/too_many_slugs/invalid_slug with nothing written, a submitted slug whose slug: record exists is skipped with reason link_exists while the rest still purge, deletes are bounded by MAX_PURGE_KEYS_PER_REQUEST with the rest returned in remaining_slugs, purging the same slugs twice is a no-op the second time, and the deletes are issued sequentially rather than gathered
- [ ] Route both endpoints in api/app.py (depends on both handlers) — file(s): api/app.py — done when: `curl` against a running app returns 200 for GET /api/admin/analytics/orphans as an admin, 403 as a user without users.manage, and 400 confirmation_required for a POST to /api/admin/analytics/purge with no confirm field
- [ ] Pin that a purge can never touch another namespace — file(s): api/tests/test_store_isolation.py — done when: a fifth test builds one physical FakeStore holding links:, users: and analytics: keys, drives handle_orphan_purge over kvprefix.open_views, and asserts every links:/users: key is byte-identical afterwards while only the targeted analytics: keys are gone
- [ ] Add the fourth article and its chunked purge loop to the maintenance page (depends on the routes) — file(s): gui/admin/backup.html, gui/admin/backup.js — done when: the page reports orphan counts, a count-bearing confirmDialog precedes any deletion, the loop re-POSTs remaining_slugs until complete and follows a truncated report, a Stop button halts it after the in-flight request, and gui-pages/tests/test_no_inline_code.py still passes
- [ ] Retitle the maintenance page (independently landable; may be dropped) — file(s): gui/admin/backup.html, gui/admin/backup.js, gui/admin/users.html — done when: the <h1>, <title>, initHeader pageLabel and the users.html anchor all read "Store maintenance", the file path is unchanged, and the breadcrumb measures no overflow at 390px in both themes
- [ ] Document the orphaned-analytics purge (depends on every task above) — file(s): CLAUDE.md, PRODUCT.md — done when: CLAUDE.md gains an "Orphaned analytics purge" section peer to "KV consistency check", its consistency-check section's "analytics store is never opened" paragraph points at the new tool while keeping its own reasoning intact, and the two caps are documented with the raise-only-with-timing-evidence rule
- [ ] End-to-end manual verification of the orphaned analytics purge — file(s): (none — verification step) — done when: every numbered step in docs/plans/analytics-orphan-purge.md's Verification section passes against a real `spin up`, including that a live link's analytics survive a purge run alongside orphans
- [ ] Re-measure list_keys after the first purge on a deployed build (depends on a deploy of this change) — file(s): TASKS.md, CLAUDE.md — done when: a traced GET /api/analytics/click-totals taken after purging shows list_keys fallen roughly in proportion to the keys removed, the figure is recorded, and the projection in this plan is replaced with the measurement
```

## Critical files

- `api/analyticsorphans.py` (new)
- `api/tests/test_analytics_orphans.py` (new)
- `api/analytics.py`
- `api/links.py`
- `api/app.py`
- `api/tests/test_click_totals.py`
- `api/tests/test_store_isolation.py`
- `gui/admin/backup.html`
- `gui/admin/backup.js`
- `gui/admin/users.html` (retitle task only)
- `CLAUDE.md`
- `PRODUCT.md`
- `TASKS.md`

No change to `spin.toml` (no new route — `/admin/backup.js` is already
routed), `gui-pages/routing.py` (no new page), `redirect/` (untouched),
`Jenkinsfile` (test invocation unchanged), `api/kvprefix.py`, `api/backup.py`
or `api/consistency.py`.

## Verification

1. `cd api && uv run pytest` — all pre-existing tests plus the new ones pass.
   The baseline before this work is **530 passed**; nothing may regress.
2. `cd gui-pages && uv run pytest` — `test_no_inline_code.py` still passes,
   confirming the new article added no inline `<script>`, `<style>` or
   `style="…"`.
3. `cd redirect && go test ./linkgate/...` — unchanged and passing. (Never
   `go test ./...`: it fails by design on `package main`.)
4. Start the app with a persistent store so the seeded data survives the
   restart between steps:

   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=devpass SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build
   ```

   Note the **deliberate omission of `--runtime-config-file`**: passing it
   selects an in-memory store wiped on every restart (CLAUDE.md's measured
   three-way table), which would destroy the orphans this test needs.
5. Seed the fixture, in a browser signed in as `admin`:
   1. Create two links, `keepme` and `killme`.
   2. Click `http://localhost:3000/r/keepme` and
      `http://localhost:3000/r/killme` several times each so both have
      analytics keys.
   3. Delete `killme` from the dashboard. `keepme` stays.
6. `GET /api/admin/analytics/orphans` via the Store maintenance page's "Find
   orphaned analytics" button. **Pass:** it names `killme` with a key count
   ≥ 2, reports `orphan_slugs: 1`, and does **not** list `keepme`.
7. Purge. **Pass:** the dialog names the exact key and link counts; after
   confirming, the progress line reaches completion and the result reports
   `deleted_keys` equal to the reported orphan key count.
8. Re-run the report. **Pass:** zero orphans, rendered as a single
   `.form-success` line with no purge button.
9. **The load-bearing check —** open `keepme`'s detail page
   (`/links/detail.html?slug=keepme`). **Pass:** its click total and recent
   events are exactly what they were before the purge. Then confirm the
   dashboard's Clicks column still shows `keepme`'s total. A purge that
   damaged a live link's analytics fails here and nowhere else.
10. Curl the guards directly:

    ```bash
    # 400, and nothing written
    curl -s -X POST localhost:3000/api/admin/analytics/purge \
      -H 'content-type: application/json' -b cookies.txt -H "X-CSRF-Token: $CSRF" \
      -d '{"slugs":["killme"]}'                      # -> confirmation_required
    -d '{"confirm":"PURGE","slugs":["keepme"]}'      # -> ok, skipped: link_exists
    -d '{"confirm":"PURGE","slugs":["a:b"]}'         # -> invalid_slug
    ```

    Use the browser's own session and CSRF token — a raw `fetch`/`curl` login
    produces `csrf_mismatch` 403s that mimic permission bugs.
11. Sign in as a user **without** `users.manage`. **Pass:** the maintenance
    page shows the forbidden notice, and both endpoints return
    `403 {"error": "forbidden", "required_permission": "users.manage"}`.
12. Browser console must be clean on `admin/backup.html` throughout — a CSP
    violation from the new article fails the page silently rather than
    failing a test.
13. If the retitle task landed: at 390 px in both themes, `#app-header nav`'s
    `scrollWidth` must equal its `clientWidth` (DESIGN.md:250's rule).
14. **Deployed, after the next deploy of this change:** trace
    `GET /api/analytics/click-totals` with `X-SS-Debug` before and after a
    real purge, discarding the first sample after idle (it is a cold
    measurement — TASKS.md's baseline task records 174.6 ms cold against a
    64.2 ms warm median). **Pass:** `list_keys` falls roughly in proportion to
    the keys removed. Record the real figure and replace this plan's
    projection with it.

## Out of scope / follow-ups

- **Automatic purging on delete.** Rejected with arithmetic in Trade-offs #3.
  Would only become viable if a future host offered a batched or ranged
  delete, at which point the whole design changes shape.
- **A 13th consistency check.** Deliberately not taken (Trade-offs #1). The
  existing Future-work entry for a *different* 13th check (physical keys under
  no known prefix) is unaffected and keeps its own trigger.
- **Renaming `gui/admin/backup.html` on disk.** Only the cheap half of that
  Future-work entry is taken here (heading, title, breadcrumb, anchor text). A
  note goes under Future work recording which half remains.
- **Caching the physical key enumeration for the lifetime of one request.**
  Still open, still worth ~66 ms per eliminated walk today, and it would halve
  the purge's own per-chunk overhead. Independent of this work; its blocker
  (`backup.handle_restore`'s post-write `list_keys` needs a *fresh*
  enumeration) is unchanged.
- **Pagination for `GET /api/links`** and a **single-link enable/disable
  control** — both explicitly the user's separate work.
- **Reducing the redirect's KV writes per click from two to one.** The
  complement to this feature: it slows accumulation, this removes what has
  accumulated. Weigh them together, as its own entry now says.
- **A purge scheduler or automatic maintenance window.** There is no cron in
  Spin and no background execution under WASI; every mutation is
  request-driven. Would need an external caller hitting the endpoint on a
  schedule, which is an operations decision, not a code one. Added under
  Future work.
