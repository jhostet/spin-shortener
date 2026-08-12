# Links Pagination

**Recommendation: DEFER. Pagination is third in line, and for the problem people
actually mean by "pagination" it is the wrong tool.** This document answers the
prior question, ranks what binds first with arithmetic, designs the pagination
that *would* be built when its trigger fires so that firing the trigger means
"execute this" rather than "re-plan", and schedules only measurement.

## Context

`GET /api/links` returns every link the caller may see, with no pagination.
CLAUDE.md's "Parallel KV reads" section closes with *"Still unfixed: `GET
/api/links` has no pagination… Pagination is the real answer if link counts ever
get large — this bought a lot of headroom, not unlimited headroom."* That
sentence is the motivating entry, and it is the sentence this plan revises.

What is true today that makes the question live: the app's intended audience is
non-technical marketing staff who create links heavily and delete them heavily
as they correct mistakes (TASKS.md, "Row-level Disable/Enable (2026-08-10)").
Live links plateau; the store does not. Three separate 2026-08-10/11
measurements — the `list_keys` growth fit, the production purge, and the
inline-purge plan's key-count model — all converged on the dashboard load being
the page whose cost grows without bound.

The user's framing on opening this plan was that they were *not* certain
pagination is the right lever and would rather be told. It is not. The
arithmetic below says `GET /api/analytics/click-totals` — which runs on the same
dashboard load — costs roughly **35× more reads** than `GET /api/links` at every
deployment size worth planning for, on two independent axes, neither of which
pagination touches.

**Confirmed decisions (settled by the user before planning):**

- Never gather writes. `api/kvbatch.py`'s `gather_reads` stays reads-only and
  bounded at 100 concurrent.
- `redirect` is not touched. The hot path stays at 6 KV operations.
- New pure logic stays host-testable: zero `spin_sdk` imports, `store` /
  `request` / `list_keys` passed as plain parameters.
- No `'unsafe-inline'` in the GUI: no inline `<script>`, `<style>` block or
  `style="…"` attribute. `gui-pages/tests/test_no_inline_code.py` enforces it.
- The dashboard's mobile invariant holds: at 390px the links figure measures
  327/327 and must not scroll (`scrollWidth` vs `clientWidth`, not cell gaps).
- A well-argued "defer, here is the trigger" is an acceptable deliverable.

## Key technical facts confirmed during research

**Measured, cited:**

- **`GET /api/links` wall time stopped scaling with link count.** ~173 ms median
  at 50 links against ~178 ms at 14, while ops went 19 → 55 (CLAUDE.md "Parallel
  KV reads", deployed build `ac650c9-gatherreads`, 2026-08-07). Pagination
  therefore buys **no latency at all** at sizes reachable today.
- **A `list_keys` costs about one KV round trip plus ~68.7 µs per physical key in
  the whole `default` store.** Six points, refit regime-normalised, R² = 0.9989;
  floor confirmed at 23.9 ms by purging production to 57 keys (TASKS.md,
  "Measure the `list_keys` growth rate" and "Deploy of the orphan purge"). It is
  un-overlappable — `gather_reads` cannot issue a read until the key list exists.
- **`GET /api/analytics/click-totals` makes exactly one such enumeration on every
  dashboard load** (`api/analytics.py:137`, `keys = await
  list_keys(analytics_store)`), and it is fired unconditionally by
  `gui/dashboard.js`'s `loadLinks()` → `loadClickTotals()` (line 379).
- **`click-totals` then issues one `get` per *existing* count-shard key of every
  visible slug** — `api/analytics.py:147-149`, `flat = [key for slug_keys in
  wanted.values() for key in slug_keys]` then `gather_reads`. Its docstring
  frames this as the win ("cost becomes proportional to real traffic rather than
  to links × shard count"). Proportional-to-real-traffic is an **unbounded**
  term; that is the finding.
- **`spin:key-value/key-value@3.0.0`'s `get-keys()` takes no arguments** — no
  prefix, no cursor, in either SDK (CLAUDE.md, "Parallel KV reads"; TASKS.md
  2026-08-10). Any pagination cursor must come from the app's own index.
- **The link indexes are append-ordered single JSON values.**
  `links.add_slugs_to_indexes` (`api/links.py:59-72`) appends new slugs to the
  tail of `all_links` and of `owner_links:<owner>` and writes each back as one
  `json.dumps` blob; `remove_slugs_from_indexes` filters in place.
  `links.all_slugs`/`owned_slugs` read one KV key each.
- **`links.move_slugs_between_owners` appends reassigned slugs to the new owner's
  tail** (`api/links.py:112-116`) regardless of their creation dates, and
  `backup.handle_restore` writes an index back verbatim. So index order is
  creation order **with two documented exceptions**, not a guarantee.
- **`GET /api/links` has exactly one consumer**: `gui/dashboard.js:373`. Grepped
  across `gui/*.js` and `gui/**/*.js`; `links/detail.js` uses
  `/links/{slug}`, never the list.
- **Akamai quotas**: 1,000 KV reads/s and 50 KV writes/s per app, 30 s handler
  duration, 1 MB max value size, 128 MiB RAM (CLAUDE.md quota table, fetched
  2026-08-04).
- **Production store post-purge is 57 physical keys, 36 of them live analytics
  across 14 live links, only 2 of which have any analytics at all** (TASKS.md,
  2026-08-11). There is no production data point above this size.
- **The coupon-collector key model was validated on the one link that can test
  it**: `jwh` holds 5 analytics keys against a predicted 6 at 3 clicks
  (TASKS.md, "Inline analytics purge", 2026-08-11).
- **`MAX_BULK_ROWS = 50`** (`api/bulk.py`), mirrored client-side as
  `BULK_MAX_SELECTION` (`gui/dashboard.js:216`).

**MODELLED, not measured — labelled everywhere it is used below:**

- **Analytics keys per link**: `64·(1−(63/64)^C)` count shards plus
  `30·(1−(29/30)^C)` event slots, for `C` clicks. Gives 5.9 keys at 3 clicks,
  18.0 at 10, 59.4 at 50, 79.7 at 100, 94.0 at 500. Validated at one point only.
- **`click-totals`'s `get` count** = the count-shard half of that, summed over
  visible links. Never measured against a clicked store: the 2026-08-10 seeding
  experiment deliberately seeded links that were **never clicked**, precisely so
  the enumeration could be isolated, so it says nothing about this axis.

**UNCONFIRMED, with what it would take:**

- **A single handler's read throughput appears to top out at ~1,000 reads/second,
  exactly Akamai's published per-app read cap.** Computing ops ÷ wall from the
  four recorded gathered traces: `/api/links` 55 ops → 318/s, consistency 69 →
  246/s, analytics 100 → 407/s, **export 999 → 1,044/s**. Throughput rises
  monotonically with fan-out and stops dead on the published number. If that is
  the cap rather than a coincidence, **wall time for any fan-out above ~1,000
  reads is `reads ÷ 1,000` seconds and no amount of parallelism improves it** —
  which is the single most important number in this document. The competing
  explanation is per-operation queueing under 100-way concurrency (100 in flight
  ÷ 96 ms/op also gives ~1,044/s). Both give the same practical rule. To
  confirm: trace one handler with a fan-out of ~2,000–3,000 reads on the deployed
  build and check whether ops/s stays pinned near 1,000. Task 3 below.
- **Whether `gather_reads` even survives a 3,500-coroutine fan-out.** The largest
  ever measured is the export's 999. 3,500 coroutines in a 128 MiB instance is
  untested. `MAX_CONCURRENT_READS = 100` bounds concurrency, not the number of
  coroutine objects created up front.
- **The dashboard's client-side ceiling.** No measurement of `allLinks` memory or
  `renderLinksTable()` wall time at any size above 14 links exists. The
  2026-08-10 seeding run reached 9,014 links on the deployed app and nobody
  recorded what the dashboard did. Cheap to measure locally — task 1 below.

## Which constraint actually binds

Reads per dashboard load, by deployment shape. Analytics key counts and
`click-totals` `get` counts are **MODELLED** from the coupon-collector formula;
enumeration cost uses the **measured** `24 ms + 68.7 µs/key`.

| shape | physical keys | enumeration | `click-totals` gets | `/api/links` reads | `/api/links` share of the two |
|---|---|---|---|---|---|
| today (14 links, 2 clicked) | 57 | 24 ms | ~6 | 17 | 74% |
| 100 links @ 50 clicks | ~6,000 | ~440 ms | ~3,500 | 103 | **2.9%** |
| 200 links @ 50 clicks | ~12,100 | ~854 ms | ~7,000 | 203 | **2.8%** |
| 500 links @ 50 clicks | ~30,200 | ~2,100 ms | ~17,400 | 503 | **2.8%** |
| 1,000 links @ 10 clicks | ~19,000 | ~1,330 ms | ~9,300 | 1,003 | **9.7%** |

Apply the ~1,000 reads/second handler ceiling (UNCONFIRMED but well-supported)
and those `get` columns become wall-time floors: **3.5 s at 100 links, 17.4 s at
500 links** — the latter over half of Akamai's 30-second handler limit, plus
2.1 s of enumeration in front of it that cannot overlap with anything.

**`GET /api/links` at 500 links is a 0.5 s floor. `click-totals` at the same size
is a 19.5 s floor. Pagination fixes the 2.8%.**

Three consequences worth stating plainly:

1. **Latency**: already answered. `gather_reads` flattened `/api/links` to a
   constant at the measured sizes, and above them it is bounded by
   `links ÷ 1,000` seconds. Pagination buys nothing until ~1,000 links.
2. **Read-cap headroom** — the strongest argument for pagination in the brief.
   It is real but misattributed: at 100 links the dashboard consumes ~3,600 reads
   and `/api/links` is 103 of them. Redirects draw 2 reads each from the same
   1,000/s pool, so a mature dashboard load really does starve the redirect path
   — but paginating `/api/links` recovers under 3% of what is being consumed.
   **The lever that matters here is `click-totals`, by a factor of ~35.**
3. **Client-side cost** — `allLinks` in memory and the DOM rows built from it.
   This is genuinely unbounded and genuinely unmeasured, and **its answer is not
   API pagination.** See "Two different problems" below.

**The one shape where pagination binds first is many links with almost no
clicks** (the 1,000-links-@-10-clicks row, where `/api/links` reaches 9.7%). That
is the shape of the 2026-08-10 seeding experiment, not of a marketing
deployment: this audience creates links *to be clicked*. If a deployment ever
does look like that, the trigger below catches it.

### Two different problems are both called "pagination"

- **Server read cost.** Fixed by pagination, worth ~3% of the dashboard's reads,
  and it costs the entire six-feature cascade below. Poor trade.
- **Client render and memory cost.** Fixed by **rendering a window of
  `getVisibleLinks()` instead of all of it** — no API change, no cascade, no
  correctness regression, because `allLinks` still holds everything and every one
  of the six features keeps reading it verbatim. Roughly 30 lines in
  `gui/dashboard.js`. **This is strictly better than API pagination for the
  client-side half**, and it is the designed response if task 1's measurement
  fires. Design in "GUI: windowed rendering" below.

That distinction is the most consequential finding in this document. Nothing in
the brief's list of six client-side features is threatened by windowing; every
one of them is threatened by pagination.

## The cascade: what paginating `GET /api/links` actually costs

`GET /api/links` returning everything is load-bearing for **seven** client-side
behaviours (the brief named six; the seventh is called out below), plus one
documented architectural decision. Each entry states what happens under
pagination, honestly.

1. **Tag autocomplete** — `allKnownTags()` (`gui/dashboard.js:121-127`) unions
   `link.tags` across every loaded link, feeding both `#tag-filter`
   (`rebuildTagFilterOptions`) and the shared `#tag-suggestions` `<datalist>`
   (`refreshTagDatalist`). Under pagination it offers only the current page's
   tags. **Degrades silently** — a user typing a tag that exists on page 3 gets
   no suggestion and has no way to know why.
2. **Owner-filter options** — `rebuildOwnerFilterOptions()`
   (`gui/dashboard.js:158-183`), built from `allKnownOwners()`, which is derived
   from the records' own `owner` field rather than from `owner_links:`. CLAUDE.md
   names this **the repair path** for links whose owner no longer exists ("that
   makes it the repair path for any deployment that orphaned links before this
   gate existed"), and `gui/admin/users.html`'s 409 refusal deep-links into it as
   `dashboard.html?owner=<username>` (consumed once via `pendingOwnerFilter`,
   `gui/dashboard.js:101`). Under pagination that deep link filters one page.
   **This is a documented recovery workflow breaking, not a nicety.**
3. **Text and tag filtering** — `getVisibleLinks()` (`gui/dashboard.js:292-325`)
   filters `allLinks` on slug, `target_url` and tags. Under pagination it filters
   the page. **"I can't find my spring-sale link" is exactly the failure this
   audience will hit**, and it presents as data loss, not as a paging artifact.
4. **Sorting** — the same function sorts the whole set on any of eight keys.
   Under pagination it sorts within a page, which is worse than not sorting: the
   header still says "sorted by Created ▲" and the answer is wrong.
5. **Sorting by Clicks — the seventh, not in the brief's list.** `sortKey ===
   "clicks"` (`gui/dashboard.js:312-317`) sorts on `clickTotals`, which comes
   from `click-totals`, not from `allLinks`. If pagination is paired with
   page-scoping `click-totals` (the only way pagination helps that endpoint at
   all), **sorting by clicks becomes impossible in principle** — you cannot order
   by a value you only fetched for the page you are already showing.
6. **CSV export** — `gui/dashboard.js`'s `export-csv` handler over
   `getVisibleLinks()`. Its own comment reads *"the dashboard already holds every
   link the user may see in `allLinks`, so this needs no endpoint, no permission
   work and no selection."* Under pagination that sentence is false and CSV
   export needs a server endpoint with its own permission handling — which is
   real work that was explicitly avoided when the feature shipped.
7. **Bulk select-all** — `getSelectableVisibleSlugs()` (`gui/dashboard.js:264`)
   over the filtered view, capped client-side at `BULK_MAX_SELECTION = 50`
   against `api/bulk.py`'s `MAX_BULK_ROWS`. This is the **one** item pagination
   improves: a page size of 50 makes select-all-on-page structurally incapable of
   exceeding the cap, retiring the "Narrow the filter, or clear some selections"
   warning path. Worth noting; not worth the other six.

**The architectural decision that pagination invalidates.** CLAUDE.md's "Link
tags and ownership" justifies having **no `tag:` index and no `_meta:tags`
registry** on exactly this basis: *"The dashboard already holds every link in
`allLinks` (`GET /api/links` has no pagination), so filtering and autocomplete
are pure client-side work over data already in memory."* Paginating removes that
justification, and the fork it leaves has no cheap branch:

- **Server-side tag filter and autocomplete** resurrects the `tag:<tag>` index,
  rejected for costing a two-index read-modify-write on every single-link
  `PATCH` — up to 20 of them at the 10-tag cap, with no compare-and-swap
  anywhere in Spin KV — plus the three obligations every new KV key type carries
  (`backup.py`'s `INDEX_KEYS`/`restore_write_order`, `consistency.py` key-shape
  recognition, and a prefix in `kvprefix.STORE_PREFIXES`, or the key is invisible
  to the whole application).
- **Or filtering silently degrades to the current page**, which is a correctness
  regression the user experiences as lost links.

There is no third option, and **that fork alone is a sufficient reason to defer**
independently of the arithmetic.

## The pagination design, for when the trigger fires

Fully specified so that firing the trigger means execution, not re-planning. **Do
not build any of this now.**

### API changes (`api/links.py`, `api/app.py`)

`handle_list` gains two optional query parameters, both absent by default:

```python
async def handle_list(store, principal: Principal, query: dict | None = None):
```

`api/app.py:188-193` already parses a query string for other routes; pass it
through. Behaviour:

- **Both parameters absent → today's response, byte for byte.** `{"links": [...]}`
  with every visible link in index order. This is what keeps the single consumer
  working unchanged and is why no versioning is needed.
- `?limit=<1..200>&offset=<0..>` → `{"links": [...], "total": N, "offset": O,
  "limit": L}`. `total` is free: the index is one JSON array already read
  wholesale, so its length costs nothing.
- `limit` outside 1–200, or a non-integer `limit`/`offset`, → `400 {"error":
  "invalid_pagination"}` naming `max_limit`, following `bulk.py`'s convention of
  echoing the limit so no client hardcodes it. An `offset` past the end returns
  an empty `links` array with the true `total`, not a 404.

The slice is applied to the **index** before the record fetch, so the fan-out
becomes `gather_reads` over `limit` slugs rather than all of them:

```python
slugs = await all_slugs(store)          # or owned_slugs, unchanged
total = len(slugs)
page = slugs[offset:offset + limit] if limit is not None else slugs
fetched = await gather_reads(get_link(store, slug) for slug in page)
```

**No new KV key type.** The paginated endpoint reads exactly the keys it reads
today: one index key plus one record per returned link. So none of the three
obligations apply. That is a genuine merit of this design and the reason it is
the one to build if any is.

**Default page size: 50, equal to `api/bulk.py`'s `MAX_BULK_ROWS`.** Not a
coincidence to be tidied away later — it is what makes select-all-on-page
incapable of exceeding the bulk cap. `MAX_PAGE_SIZE = 200` as the hard ceiling,
a plain module constant in `api/links.py` on the same reasoning as
`MAX_BULK_ROWS` and `MAX_BACKUP_ENTRIES`: one function in one component reads
it, and raising it needs real timing evidence from a full-page fetch.

### Pagination style: offset/limit, NOT cursor

Both resolve against the same single JSON array read in one `get`, so **a cursor
buys exactly zero KV cost** — the usual cursor-vs-offset trade-off does not
apply here, because there is no "seek to the cursor" cost to avoid. What is left
is the drift behaviour, and offset wins there too:

- **Inserts never shift an existing page.** `add_slugs_to_indexes` appends to the
  tail, so a link created during a paged scan appears only after the last page.
- **A delete splices**, shifting later pages by one, so a concurrent delete can
  cause one link to be skipped in a full paged scan. For a dashboard that reloads
  a whole page at a time this is invisible; document it rather than engineer
  around it.
- **A cursor of "last slug seen" is strictly worse**: if that slug is deleted
  between requests it is not in the array at all, and the server needs a fallback
  policy for a cursor it cannot locate. Offset needs no such policy.

### Ordering — and the finding that kills server-side sort

The index is the only order available for free, and **it is not a sort key.**
`all_links` is append-ordered, so it approximates creation order — but
`move_slugs_between_owners` appends reassigned slugs to the new owner's tail
regardless of their creation dates, and `backup.handle_restore` writes an index
back verbatim. So even "creation order" is an approximation with two documented
exceptions.

Every field the dashboard sorts on — `slug`, `owner`, `target_url`, `created_at`,
`status`, `start_at`, `end_at` — lives in the **record**, not the index. Clicks
live in a third store. **Sorting server-side by any of them requires reading
every record, which is precisely the N reads pagination exists to avoid.** So:

- **Decision: the paginated endpoint serves index order only. There is no
  `?sort=` parameter, and adding one later would be a mistake unless it is backed
  by a new order-maintaining index.**
- The same argument applies to server-side text/tag/owner filtering: filtering
  requires the records.

This is the honest core of why pagination is expensive here. It is not that
pagination is hard; it is that **this endpoint's cost is one KV read per link and
that is irreducible without duplicating link fields into an index.**

### GUI: windowed rendering (the *other* problem, and the cheap fix)

Independent of pagination and buildable on its own. `renderLinksTable()`
(`gui/dashboard.js:406`) renders every element of `getVisibleLinks()`. Render a
window instead:

- Module-level `let renderWindow = PAGE_SIZE;` (50), reset to `PAGE_SIZE` at the
  top of `renderLinksTable()` alongside the existing `selectedSlugs.clear()`.
- Render `visibleLinks.slice(0, renderWindow)`; if `visibleLinks.length >
  renderWindow`, append one footer row with a "Show 50 more" button and text
  naming both numbers ("Showing 50 of 312 links"), per DESIGN.md's rule that a
  truncated list must never read as complete — the same posture
  `consistency.py`'s `truncated` flag and its "Showing the first N of M" GUI copy
  already take.
- The button raises `renderWindow` and re-renders. **It must not go through a
  path that clears `selectedSlugs`** — either hoist the clear out of
  `renderLinksTable()` into its callers, or add an explicit `preserveSelection`
  argument. Getting this wrong silently drops the operator's bulk selection,
  which is the exact failure `paintClickTotals()` was written to avoid
  (`gui/dashboard.js:395-404`).
- **Every one of the seven cascade behaviours keeps working verbatim**, because
  `allLinks` and `getVisibleLinks()` are untouched. CSV export deliberately keeps
  exporting `getVisibleLinks()` — the whole filtered set, not the window — and
  its success message already names the row count, which becomes load-bearing:
  it is what tells the operator they exported 312 rows while looking at 50.
- Re-measure the 390px invariant afterwards (`scrollWidth` vs `clientWidth` on
  the links figure, expect 327/327 and no scroll) — a footer row spanning
  `colspan="10"` is a new widest-element candidate.

### Backward compatibility

Response shape is unchanged when both parameters are absent, and
`gui/dashboard.js:373` is the only consumer in the repo. No other page, no CLI,
no `curl` snippet in any doc reads `GET /api/links`. So no versioning, no
deprecation window, and the endpoint can grow pagination without a coordinated
client change.

## Trade-offs and rejected alternatives

1. **Paginate `GET /api/links` now (the requested change).** Attractive because
   the "unbounded list endpoint" smell is real, CLAUDE.md itself names it as
   unfixed, and read-cap headroom against a 1,000 RPS app-wide cap that redirects
   share is a legitimate worry. **Rejected because it is 2.8–2.9% of the reads
   the same page fires**, buys no latency at reachable sizes (measured flat, 14 →
   50 links), and costs a seven-feature cascade plus the re-opening of the
   settled `tag:` index question. Revisit on the triggers below.

2. **Land the API-side groundwork only — `?limit=`/`?offset=` with no client
   change.** Explicitly offered as an acceptable smaller scope, and rejected. It
   would be designed against no client, and the design section above shows the
   resulting endpoint can only serve **index order with no filter and no sort** —
   which is not what any client wants, so the parameters would be dead on
   arrival and would be re-designed when a real client appeared. This repo's own
   rule ("measure, don't extrapolate") argues the same way. **Landing nothing is
   the right amount of code.**

3. **Cursor-based pagination.** Attractive as the textbook-correct answer and
   because `get-keys()` having no cursor makes cursors feel like the
   sophisticated choice. Rejected: the cursor would be derived from the app's own
   index, which is a single JSON array read wholesale, so a cursor and an offset
   cost identically — and a cursor additionally needs a policy for a cursor slug
   deleted between requests, which offset does not. Complexity with no
   corresponding benefit.

4. **Widen `all_links` from a slug array into a summary array** — `{slug, owner,
   target_url, status, created_at, tags}` per link, so the whole table is one KV
   read and every client-side feature survives untouched. **Genuinely the most
   attractive alternative in this document**: it makes `GET /api/links` O(1)
   reads instead of O(N), which is better than pagination rather than a
   mitigation of it. Rejected on the identical argument that killed the `tag:`
   index: `handle_update` touches no index today, and this would add an index
   read-modify-write to every single-link `PATCH`, every password change and
   every bulk tag operation, with no compare-and-swap available. It also caps the
   deployment: at ~250 bytes per summary, Akamai's 1 MB max value size is
   reached near 4,000 links, converting a slow dashboard into a hard write
   failure. And a summary drifting from its record is invisible — a whole new
   class of consistency finding. Revisit only if a compare-and-swap primitive
   ever appears.

5. **Move filtering and sorting server-side as part of pagination.** Attractive
   because it is the only design that preserves correctness under pagination.
   Rejected because it reads every record to filter or sort — the exact cost
   pagination was supposed to remove — unless backed by new indexes, which is
   alternative 4 or the rejected `tag:` index wearing a different hat.

6. **Do nothing at all, including no measurement.** A live option: the store is
   57 keys and 14 links, every number above 100 links is modelled, and this repo
   has been burned by extrapolation twice (the pre-sharding loss curve; the
   `list_keys` low-end intercept). Rejected because the two decisive numbers are
   cheap to measure — one local afternoon and one deployed trace — and because
   both feed a decision (whether to bound `click-totals`, and how) that is
   otherwise being made on a formula validated at a single data point.

## Tasks

Verbatim, as appended to `TASKS.md` under `## Links pagination — deferred, and
what actually binds (2026-08-11)`. TASKS.md is authoritative.

```
- [ ] Measure the dashboard's client-side ceiling locally — file(s): (none — measurement) — done when: a local `spin up` store seeded via POST /api/links/bulk to at least 1,000 and 5,000 links has GET /api/links response size, allLinks memory and renderLinksTable() wall time (performance.now() either side of the call) recorded at each size, the growth shape is stated linear or superlinear, and the link count at which renderLinksTable() first exceeds 200 ms is recorded in this section as windowed rendering's measured trigger
- [ ] Measure GET /api/analytics/click-totals' shard-read fan-out against a clicked store — file(s): (none — measurement) — done when: on the deployed build at least 3 link-count/click-count combinations have been traced with X-SS-Debug recording get count, list_keys and wall time, the per-link get growth is compared against the coupon-collector prediction 64x(1-(63/64)^C), the reads-per-dashboard-load figure is stated, and every seeded link is deleted and its analytics removed via POST /api/admin/analytics/purge afterwards with a follow-up orphan report showing orphan_slugs 0
- [ ] Confirm or refute the ~1,000 reads/second single-handler throughput ceiling (depends on the trace above) — file(s): CLAUDE.md — done when: ops divided by wall time is computed for a traced handler with a fan-out above 2,000 reads and compared against the existing four points (55 ops 318/s, 69 ops 246/s, 100 ops 407/s, 999 ops 1044/s), and CLAUDE.md's "Parallel KV reads" section either states the confirmed ceiling with the rule that wall time for a large fan-out is reads divided by 1000 seconds, or records the refutation and what the real scaling is
- [ ] Record the pagination deferral, its trigger and the ranking (depends on all three measurements) — file(s): CLAUDE.md, TASKS.md — done when: CLAUDE.md's "Parallel KV reads" closing paragraph no longer implies pagination is the next fix, states instead that click-totals binds first on two independent axes with the measured reads-per-load ratio, distinguishes server read cost from client render cost and names windowed rendering as the answer to the second, and TASKS.md's "Future work (not scheduled)" carries a links-pagination entry naming both triggers from docs/plans/links-pagination.md
```

## Critical files

Read during planning; **no file is created or modified by this plan** beyond the
two it is contractually allowed to write.

- `docs/plans/links-pagination.md` (new) — this document.
- `TASKS.md` — appended: one new section at the end, five entries under
  `## Considered and rejected`, one entry under `## Future work (not scheduled)`.

Files the deferred work would touch when its trigger fires, listed so the scope
is visible now: `api/links.py`, `api/app.py`, `api/tests/test_links.py`,
`gui/dashboard.js`, `gui/dashboard.html`, `CLAUDE.md`.

## Verification

Nothing user-visible changes, so there is no `spin up` acceptance step for this
plan itself. The scheduled work is measurement; these are its commands.

1. Baseline the suites so the measurement tasks can prove they changed nothing:

   ```bash
   cd api && uv run pytest
   cd gui-pages && uv run pytest
   cd redirect && go test ./linkgate/...
   ```

2. Task 1, local client-side ceiling. Seed and measure against a real app:

   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```

   Note that `--runtime-config-file` gives an **in-memory store wiped on
   restart** (CLAUDE.md, Commands) — which is what you want here, so the seeded
   links vanish on exit. Seed with repeated `POST /api/links/bulk` at 50 rows per
   request (~20 requests per 1,000 links), then in the browser console time the
   render directly rather than trusting the page feel:

   ```js
   const t0 = performance.now(); renderLinksTable(); performance.now() - t0;
   ```

   Record `GET /api/links` transfer size from DevTools' Network panel at each
   size. A pass is a recorded number at 1,000 and 5,000 links and a stated growth
   shape — not a verdict.

3. Task 2, deployed `click-totals` fan-out. Requires a deployed build whose
   `log_debug_token` you set at deploy time (it cannot be added later —
   CLAUDE.md, Deployment). Trace with `-H "X-SS-Debug: <token>"` and read `get`
   and `list_keys` off the `Server-Timing` header. **Hold the click-seeding rate
   under ~20 requests/second** (`dev/click-load.sh`, which prints the implied
   write rate and warns at 50) or the run measures the app-wide write cap instead.
   **Discard the first traced sample after any idle period** — it measures ~175 ms
   against a 60–70 ms warm median.

4. Task 2 cleanup, and it is not optional: bulk-delete every seeded link, then
   run the purge loop on the Store maintenance page until `remaining_slugs` is
   empty, then re-run `GET /api/admin/analytics/orphans` and confirm
   `orphan_slugs: 0`. Clicks leave permanent analytics keys; this is exactly the
   ratchet the purge exists to undo, and leaving them behind would corrupt the
   next `list_keys` measurement anyone takes.

5. Task 4, documentation. No command — the check is that CLAUDE.md's "Parallel KV
   reads" section no longer ends with a sentence implying pagination is next.

## Out of scope / follow-ups

**Out of scope, deliberately:**

- Building pagination, in any form, including an unused query parameter. See
  rejected alternative 2.
- Building windowed rendering. It is designed above and gated on task 1's
  measurement; building it against 14 links would be complexity for nothing.
- Designing the fix for `click-totals`. That is the next plan and it must be
  written against task 2's measurement, not against this document's modelled
  numbers. The leading candidate is a **cached totals snapshot key** — one
  `analytics:_meta:totals` blob holding `{slug: total}`, refreshed by a dashboard
  load only when older than some interval, turning the common case into a single
  `get`. It costs one new KV key type (all three obligations), introduces
  staleness, and has a thundering-herd corner when two loads both find it stale.
  Do not treat that sketch as a plan.
- Any change to `redirect`, to what a click writes, or to `CountShards`.

**Follow-ups, and where they belong:**

- **Pagination itself** goes under `TASKS.md`'s "Future work (not scheduled)"
  with two triggers, either of which is sufficient: **(a)** a traced `GET
  /api/links` on the deployed build exceeding 1 s wall, or a live link count
  above ~1,000 — the point where it stops being 3% of the dashboard's reads; or
  **(b)** `click-totals` having been bounded by other means, at which point
  `/api/links` genuinely becomes the largest remaining term. **Do not pick it up
  before `click-totals` is bounded** — paginating first would leave the same
  full-store enumeration and the same shard fan-out in front of a page of 50, and
  page-scoping `click-totals` to compensate would kill sorting by Clicks
  outright. That ordering constraint is the single most important thing to carry
  forward from this document.
- **`docs/plans/inline-analytics-purge-on-delete.md`'s priority is unchanged and
  it should still ship next.** It is planned, unbuilt, cheap, and it removes the
  unbounded orphan term. But do not expect it to move the numbers above: TASKS.md
  already records that post-purge production is 57 keys of which **36 (63%) are
  live analytics**, and in a mature deployment live analytics *is* the store.
  Inline purge buys time against orphans; every figure in this document assumes
  **zero orphans present**.
- **"Reduce the redirect's KV writes per click from two to one"** (existing
  Future-work entry, already carrying two independent justifications) is the
  cheapest lever that extends the runway on the enumeration axis: the `events:`
  slots are 24.5 of the ~59 modelled keys per link at 50 clicks, so dropping that
  write cuts analytics key growth by ~40% at that traffic level. It does **not**
  touch the count-shard `get` fan-out, which is the larger of `click-totals`' two
  axes. Weigh the three justifications together, not separately.
