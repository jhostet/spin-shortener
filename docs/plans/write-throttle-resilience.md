# Write-Throttle Resilience

## Decisions taken 2026-08-17 — the two open questions are CLOSED

**1. Use `200` with `"partial": true`, NOT `207`.** The plan's justification for 207 was that
`gui/app.js`'s `apiCall` routes it to `ok: true` so the GUI can render detail — but `apiCall`
returns `{ ok: res.ok, ... }`, and `res.ok` is true for **every** 2xx, so the GUI cannot tell 200
from 207 and the argument does not distinguish them. Two things decide it against 207:

- **A sibling endpoint already established the convention.** `POST /api/admin/analytics/purge`
  returns **`200` with `complete: false` and `remaining_slugs`** for a partial run — observed
  directly during the 911-key production purge, where rounds 1–3 each returned 200 while
  incomplete. A second partial-execution endpoint answering differently would make the API
  inconsistent with itself for no gain.
- **The app's status vocabulary is 200/201/400/401/403/404/409/413/422/500.** 207 is a WebDAV code
  handled inconsistently by intermediaries and monitoring, and it would be the only 2xx beyond
  200/201.

The honest cost, accepted: a `curl` user or a monitor sees `200` and could assume full success.
That is mitigated the same way the purge mitigates it — `partial`, the applied/not-applied lists,
and `next_step` are explicit in the body — and it is the cost the codebase has already chosen once.

**2. The sleep spike stays task 1, and its blocker is CONFIRMED rather than suspected.** Verified
directly: `componentize_py_async_support`'s `_Loop.call_later`, `call_at` and `time` all
`raise NotImplementedError`, so **`await asyncio.sleep(d)` for any `d > 0` raises inside the
component.** The proposed replacement exists in the SDK —
`wasi_clocks_monotonic_clock_0_3_0_rc_2026_03_15` exposes `async def wait_for(how_long: int)` —
but **its presence in the venv proves nothing about its presence in the built component.** That is
precisely the trap that produced a published-and-retracted "batch is unreachable" conclusion
earlier in this session: the module was in the venv and absent from the build, because
componentize-py bundles only what a **module-scope** import reaches. Confirm `wait_for` with a
top-level import inside a real `spin up --build`, exactly as the batch interface was eventually
confirmed. Branch C (no usable sleep) still leaves the entire reporting half of this plan intact,
so nothing here is fully blocked on the answer.

## Context

Akamai Functions caps KV writes at **50 per second app-wide** (CLAUDE.md,
"Deployment: Akamai Functions"). When that cap is exceeded the host rejects the
write and the Python binding raises. `api/bulk.py` catches exactly one
exception — `json.JSONDecodeError`, for request-body parsing — so a throttled
write propagates to `api/app.py`'s catch-all `except Exception` and the caller
receives a bare `500 {"error": "internal_error"}`.

That is not hypothetical. It happened twice in ordinary operation and both
incidents are recorded in `TASKS.md`:

- **2026-08-15** ("BOTH SPIKES ANSWERED"): seeding and deleting ~1,000 links
  pushed writes past the cap and left **20 `unindexed_link` + 152
  `missing_link_record` + 152 `orphan_owner_index_entry`** findings. The
  write-up's own conclusion: *"`GET /api/links` reads the index, so it cannot
  see an unindexed record. My 'all seeded links deleted' check passed while 20
  were still live and resolving at `/r/`."*
- **2026-08-17** (deploy of `4cd6c30-repair-and-500fix`): six bulk creates and
  six bulk deletes fired concurrently produced **87 `unindexed_link` + 87
  `unindexed_owner_link`** — 87 live records resolving at `/r/` but invisible
  in the dashboard.

The mechanism is exactly the repo's own write-ordering rule doing what it was
designed to do. `bulk.handle_bulk_create` writes every `slug:` record and calls
`links.add_slugs_to_indexes` **last**, deliberately, so an interruption leaves
recoverable orphans rather than index entries advertising slugs that 404. A
throttled run therefore leaves records written, the index never updated, and
the caller holding a `500` that tells them *nothing* about which rows landed.

**A repair now exists** — `POST /api/admin/consistency/repair`, shipped
2026-08-17, fixes all 174 findings of the second incident in **13 KV operations
and 368 ms**. But it only helps if a human notices, and the audience is
non-technical marketing staff who have no reason to ever open the Store
maintenance page.

**Why real users will hit this.** One bulk action alone is safe: 50 record
writes plus one or two index writes, sequential at the measured ~75 ms each
(TASKS.md, 2026-08-15 traced deletes), is ~13 writes/second. But **concurrent
operations stack**. Roughly four simultaneous bulk actions — or one user firing
several in quick succession — crosses 50/second. Two people tidying links at
once is an ordinary Tuesday, and the 2026-08-17 incident was produced by
exactly that (twelve concurrent bulk requests).

**Be precise about what is and is not broken.** The endpoints' documented
"all-or-nothing" refers to **validation**: any invalid row means nothing is
written and every problem is reported. That remains true and this plan does not
weaken it. **Execution** is not atomic, cannot be (Spin KV has no transactions
and no compare-and-swap), and never claimed to be. The defect is that execution
failure is **not reported usefully** — the operator gets `internal_error`,
which says "transient, retry" about a state where retrying may create
duplicates or hit `slug_taken`.

There is no existing `TASKS.md` Future-work entry for this; the nearest
neighbours are "Ask Akamai for a KV write-rate increase if redirect throughput
demands it" (2026-08-04) and "Chunked or resumable backup/restore for Akamai's
30-second handler limit" (2026-08-04), neither of which addresses reporting.

**Confirmed decisions (settled by the requester before planning):**

- `redirect` is **explicitly out of scope**. Its analytics writes are already
  documented as best-effort lossy (mechanism M2), and the hot path must stay at
  6 KV operations. Disagreement is to be recorded as a rejected alternative,
  not as a task. (I do not disagree — see Trade-offs #2.)
- **Never gather or batch writes.** Sequential stays sequential. `set_many` /
  `delete_many` were already rejected repo-wide on 2026-08-15; this plan makes
  writes *survivable*, not faster.
- Pure logic stays host-testable: zero `spin_sdk` imports, dependencies passed
  as parameters. The retry helper needs a sleep, which is I/O, so the sleep is
  **injected**, never a hardcoded `asyncio.sleep`.
- Any test fake must be able to simulate a throttled write.
- Do not name a new module after a stdlib module.

## Key technical facts confirmed during research

**Confirmed by reading the generated bindings on disk:**

- **There is no dedicated rate-limit error variant.**
  `.venv/lib/python3.14/site-packages/spin_sdk/wit/imports/spin_key_value_key_value_3_0_0.py`
  defines
  `Error = Union[Error_StoreTableFull, Error_NoSuchStore, Error_AccessDenied, Error_Other]`.
  `Error_Other` carries a single `value: str`. `Store.set`/`delete`/`get` all
  document `Raises: componentize_py_types.Err(...Error)`. Throttling arrives as
  `Err(Error_Other(value='too many requests'))` — the string TASKS.md's Spike B
  observed 9/10 times under a deliberate over-cap read load.
- **`componentize_py_types` does not exist in the host venv.**
  `find .venv -iname "*componentize_py_types*"` returns nothing; it is injected
  by componentize-py at build time. **A pure, host-testable module therefore
  cannot import `Err` or `Error_AccessDenied` at all** — any classification
  must be duck-typed or string-based, which is a large part of why "retry
  everything, bounded" wins over variant-matching (Trade-offs #1).
- **`str(exc)` on the raised error is usable and non-empty.** `Err` is
  `@dataclass(frozen=True)` subclassing `Exception`, so `BaseException.__new__`
  populates `args` and `str()` delegates to the single arg. Reproduced locally
  against a faithful reconstruction of the two generated dataclasses:
  `str(Err(Error_Other(value='too many requests')))` →
  `"Error_Other(value='too many requests')"`, and
  `str(Err(Error_AccessDenied()))` → `"Error_AccessDenied()"`. So a substring
  test for `too many requests` in `str(exc).lower()` works and cannot
  false-positive on `AccessDenied`. **It is used for the log/report label only,
  never to decide whether to retry.**

**Confirmed by reading the componentize-py async runtime:**

- **`await asyncio.sleep(d)` with `d > 0` RAISES inside the component.**
  `.venv/lib/python3.14/site-packages/componentize_py_async_support/__init__.py`
  defines `class _Loop(asyncio.AbstractEventLoop)` whose `call_later`,
  `call_at` and `time` all `raise NotImplementedError` (lines 134-148).
  CPython's `asyncio.sleep` calls `loop.call_later` for any positive delay. So
  the obvious implementation of backoff does not work at all. `asyncio.sleep(0)`
  *does* work (it takes the `__sleep0` bare-yield path and `call_soon` is
  implemented), but it costs zero wall time and is therefore useless as backoff.
- **A real awaitable sleep does appear to exist:**
  `spin_sdk/wit/imports/wasi_clocks_monotonic_clock_0_3_0_rc_2026_03_15.py`
  exposes `async def wait_for(how_long: int) -> None` ("Wait for the specified
  duration to elapse", nanoseconds) and `async def wait_until(when: int)`. The
  older `wasi_clocks_monotonic_clock_0_2_6` instead exposes
  `subscribe_duration(when) -> Pollable`.
- **UNCONFIRMED, and it is the one thing that must be spiked before the retry
  half of this plan is built:** whether `wasi_clocks_monotonic_clock_0_3_0_rc_2026_03_15`
  is (a) reachable in the built component and (b) actually sleeps. This repo
  has been burned twice on exactly this question — TASKS.md's "The `get_many`
  spike — ANSWERED, and the answer is no (2026-08-15)" was **retracted the same
  day**, because *"componentize-py bundles only modules reachable from a
  TOP-LEVEL import"* and the spike had imported inside a function. **Reading
  the SDK on the host proves nothing about what is in the component.** Task 1
  is that spike, with three defined branches.
- **Incidental, from the same TASKS.md retraction:** the componentize-py runtime
  prunes the stdlib (`pkgutil` raised `ModuleNotFoundError` inside the
  component while working on the host). Relevant because the retry helper's
  only stdlib needs are `random` and `dataclasses`, both already used by
  shipped `api/` modules (`links.py` uses `secrets`; `bulk.py` uses
  `dataclasses`), so neither is a new risk.

**Confirmed by reading the repo:**

- **`api/bulk.py` has no KV-write error handling of any kind.** Its only
  `except` clauses are the two `json.JSONDecodeError` guards at lines 185 and
  286. Verified by reading the whole file.
- **Every multi-write handler is a bare sequential loop with no guard:**
  `bulk.py:272` (record writes), `bulk.py:365/374/384` (status/tag/reassign
  writes), `bulk.py:389` (record deletes); `backup.py:300` (restore writes) and
  `backup.py:306` (prune deletes); `consistencyrepair.py:372/378`;
  `analyticsorphans.py:257` (inline purge) and `:357` (operator purge);
  `links.py:271/399/435/444/457/492` plus the index helpers at `:65/71/81/87/115/123`.
- **`api/kvbatch.py`'s module docstring already states the governing rule:**
  *"Reads only. Never wrap writes in either helper... writes are already
  cap-bound, so batching or gathering them would queue against the cap rather
  than overlap."* This plan adds nothing that contradicts it.
- **The write cost is measured, and it is ~75 ms, not ~23 ms.** TASKS.md,
  "Deploy of the inline purge" (2026-08-15): two traced deletes in the same
  request measured `delete` at 74.6 / 75.1 ms while `get` was 6.7 / 7.2 ms — an
  **11× read/write asymmetry**, with the explicit warning that *"every
  write-path estimate in this repo derived from ~23 ms/write is optimistic by
  ~3×."* All wall-time arithmetic below uses 75 ms.
- **The GUI's orphan-purge chunk loop has no no-progress guard.**
  `gui/admin/backup.js:574-584` is `while (slugs.length && !orphanPurgeStopped)`
  with `slugs = data.remaining_slugs.concat(slugs.slice(50))`. Today
  `plan_purge` always plans at least one slug so progress is guaranteed; the
  moment a purge can return "0 deleted, same slugs remaining" (which write
  failure reporting introduces) **this becomes an infinite loop**. The repair
  loop at `backup.js:421-435` already has the guard (`"Repair made no
  progress."`). This cascade is mandatory, not optional.
- **DESIGN.md already settled the "invent a warning colour?" question, in the
  negative.** Its Status Badges note on the "no password" badge: *"inventing a
  distinct warning color would imply a severity distinction the operator
  doesn't need to make."* Partial execution therefore renders through the
  existing `.form-error` class — **no new token, no new contrast measurement,
  no DESIGN.md change.**
- **A precedent for the response contract already exists**, in TASKS.md's
  "Considered and rejected" (2026-08-10, the analytics purge): *"input
  validation is all-or-nothing; state-dependent per-slug outcomes are reported
  and skipped."* This plan extends the same sentence to execution.
- **Baseline suites, run before planning:** `cd api && uv run pytest` →
  **629 passed** in 11.93s.

## The retry seam — `api/kvretry.py` (new)

A new pure module, zero `spin_sdk` imports, sitting with its siblings
`kvbatch.py` / `kvprefix.py`. **Not** `retry.py` (no stdlib collision today,
but the family naming is `kv*` and CLAUDE.md's stdlib-shadowing rule makes
generic names a standing hazard).

```python
MAX_RECORD_RETRY_SLEEP_NS = 2_000_000_000   # 2 s per request, all record writes
MAX_INDEX_RETRY_SLEEP_NS  = 3_000_000_000   # 3 s per request, all index writes
JITTER_FRACTION = 0.25

@dataclass(frozen=True)
class WritePolicy:
    kind: str                    # "record" | "index" — selects the budget
    attempts: int                # total attempts, INCLUDING the first
    backoff_ns: tuple[int, ...]  # len == attempts - 1

RECORD_WRITE = WritePolicy("record", 3, (100_000_000, 300_000_000))
INDEX_WRITE  = WritePolicy("index",  6, (100_000_000, 300_000_000, 700_000_000,
                                         1_200_000_000, 2_000_000_000))

class WriteFailed(Exception):
    """Raised when a write is still failing after its policy is exhausted.
    Carries `attempts`, `label` ("throttled" | "other") and `cause`."""

def classify_write_error(exc: BaseException) -> str: ...   # "throttled" | "other"

async def direct(make_coro) -> None:                       # no retry; the default
    await make_coro()

def make_writer(sleep, collector=None, *, jitter=random.random):
    """Returns `async def write(make_coro, policy=RECORD_WRITE) -> None`."""
```

`make_writer` closes over **one `RetryBudget` per request** — two independent
nanosecond counters, `record_ns` and `index_ns`. Once a counter is spent,
further writes of that kind get zero retries and fail immediately. The budget
is never module-level, for the same reason `obs.Collector` and the wasi bucket
are not: `Handler.handle()` dispatches each request through
`componentize_py_async_support.spawn`, so a shared budget would let one
request's retries starve another's.

**Two budgets rather than one shared pool, deliberately.** Records are written
before indexes (the repo's one write-ordering rule), so a single pool would be
drained by the 50 cheap-to-lose record writes before the one expensive-to-lose
index write ever ran — the exact inversion of what matters. Two counters
achieve the reservation with no draw-order logic.

**`sleep` is an injected `async def sleep(nanoseconds: int) -> None`.** The
module never imports a clock, never imports `asyncio`, and is fully exercised
under pytest by a fake that records the requested delays into a list and
returns immediately — so the backoff *schedule* is asserted without any test
ever waiting. This is the same shape as `list_keys`, `read_file`, `get_many`
and `purge_analytics` elsewhere in this codebase.

**Retryability: every write error is retryable, bounded.** No variant check, no
string match, in the control path. The rationale is Trade-offs #1; the
mechanical part is that a pure module cannot import the WIT error types at all.
`classify_write_error` exists purely to label the failure for the log line and
the response body (`"throttled"` vs `"other"`) — a wording change at Akamai
degrades that label, and nothing else.

### Wall-time arithmetic (at the measured 75 ms/write)

Worst case, a full 50-row bulk create (50 record writes + 2 index writes):

| term | worst case |
|---|---|
| 52 base writes × 75 ms | 3.9 s |
| record-retry sleep budget | 2.0 s |
| record retry round trips (≤ 2 s / 100 ms min backoff = 20 extra) × 75 ms | 1.5 s |
| index-retry sleep budget | 3.0 s |
| index retry round trips (2 writes × 5 retries) × 75 ms | 0.75 s |
| **total** | **≈ 11.2 s** |

Against Akamai's 30-second handler limit that is a **2.7× margin**, the same
band `MAX_PURGE_KEYS_PER_REQUEST` (250 deletes ≈ 5 s) and `MAX_REPAIR_WRITES`
(100 writes ≈ 7.5 s, ~4×) were accepted at. The sleep budgets are fixed wall
time and do not scale with the latency regime, so at the documented 3× regime
swing only the 5.4 s of round trips inflates: ≈ 21 s, still inside the limit.

**The abandon-on-exhaustion rule below is the second bound**, and the one that
matters more: without it, 50 rows each retrying to exhaustion would be
50 × (3 × 75 ms + 400 ms) ≈ 31 s, over the limit on its own.

Both figures are modelled, not measured. Per the standing rule every sibling
cap in this codebase carries: **raising any of these four constants needs real
timing evidence from a full-cap run, not a hunch.**

## Instrumentation — `api/obs.py`

`_KV_OP_ORDER` gains `"write_retry"` and `"write_failed"` immediately after
`"delete"`. `kvretry`'s writer records `("write_retry", "-", sleep_ns +
failed_attempt_ns, 0)` per retry and `("write_failed", "-", 0, 0)` on
exhaustion, so a traced request shows `write_retry=3/1150000 write_failed=1/0`.

Today a throttled write is **completely invisible** — `PrefixedStore.set`
raises before reaching its own `collector.record` line, so the only evidence is
a 500. This is the field that makes "is the deployment throttling?" answerable
from `X-SS-Debug` at all.

Namespace is the literal `"-"` (matching how `app.py` records `open`), so the
helper stays ignorant of stores entirely and **the collector still has no
parameter that could accept a key** — the structural invariant CLAUDE.md
records for `Collector.record` is untouched.

`redirect/linkgate/obs.go` is **not** changed. This is the second deliberate
divergence between the two vocabularies (`get_many`/`get_many_error` was the
first) and for the same reason: `redirect` does not use this mechanism, so a
Go-side field would be dead code. Nothing pins the two vocabularies against
each other and nothing should start.

## API changes

### `api/app.py` — one new per-request closure

Alongside the existing `list_keys = ...` / `get_many = ...` lines in
`_dispatch`:

```python
write = kvretry.make_writer(_sleep_ns, collector)
```

`_sleep_ns` is defined at module scope next to `_make_raw_get_many`, with the
clock imported **at module scope** (the get_many lesson) under the same guarded
`try/except ImportError` shape `app.py` already uses for `wasi_batch`:

```python
try:
    from spin_sdk.wit.imports import wasi_clocks_monotonic_clock_0_3_0_rc_2026_03_15 as wasi_clock
except ImportError:
    wasi_clock = None

async def _sleep_ns(nanoseconds: int) -> None:
    if wasi_clock is None:
        return                      # no clock: retries collapse to immediate re-issue
    await wasi_clock.wait_for(nanoseconds)
```

Task 1's spike decides whether this is the shape that ships (branch A), whether
it becomes the `0_2_6` `subscribe_duration` + `Pollable.block()` form (branch
B), or whether `_sleep_ns` is a documented no-op (branch C).

`write` is then threaded, as one plain parameter, into
`bulk.handle_bulk_create`, `bulk.handle_bulk_action`,
`consistencyrepair.handle_repair` and `analyticsorphans.handle_orphan_purge`.
**`backup.handle_restore` deliberately does not receive it** — see below.

### `api/links.py` — the three index helpers take a `write`

```python
async def add_slugs_to_indexes(store, owner, slugs, write=kvretry.direct) -> None
async def remove_slugs_from_indexes(store, slugs_by_owner, write=kvretry.direct) -> None
async def move_slugs_between_owners(store, slugs_by_old_owner, new_owner, write=kvretry.direct) -> None
```

Each `await store.set(...)` becomes
`await write(lambda: store.set(...), kvretry.INDEX_WRITE)`.

The default is `kvretry.direct` (call through, no retry) so all ~20 existing
test call sites that use these helpers for *seeding* are unchanged. That is a
deliberate softening of the "required parameter with no default" rule
`validate_bulk_rows`'s `policy` carries, and it is paid for by a specific
guard: **a test seeds a `ThrottlingStore` that fails only the `all_links`
write and asserts `handle_bulk_create` returns `index_updated: false`.** That
test passes only if `bulk.py` actually threads its `write` down, which is the
one place forgetting would matter.

### `api/bulk.py` — retry, then abandon-and-index-what-landed

`handle_bulk_create(store, principal, request, get_many, write)` and
`handle_bulk_action(store, users_store, principal, request, get_many, write)`
both take `write` as a **required** positional parameter, no default — the
same reasoning `validate_bulk_rows`'s `policy` carries, because a default is
exactly how the bulk path stays silently unprotected.

Nothing before the first write changes. Validation is untouched, still
all-or-nothing, still returns `400 bulk_validation_failed` with the identical
`row_errors` array.

The record loop becomes:

```python
    write_failure = None
    for row, slug, custom in assigned:
        record = {...}
        try:
            await write(lambda: store.set(f"slug:{slug}", json.dumps(record).encode("utf-8")))
        except kvretry.WriteFailed as exc:
            write_failure = (exc, row.line, slug)
            break                      # abandon the rest; index what landed
        created_records.append(record)
```

**The `break` is the single most important line in this plan.** Continuing to
hammer the remaining 40 writes keeps the app over the cap, burns handler time,
and produces more orphans. Stopping and then indexing exactly what landed means
**a throttled bulk create no longer produces `unindexed_link` at all** — the
index describes precisely the records that exist. The fix for the 2026-08-15
and 2026-08-17 incidents is not "retry harder", it is "index what landed."

Then:

```python
    index_updated = True
    if created_records:
        try:
            await links.add_slugs_to_indexes(
                store, principal.username, [r["slug"] for r in created_records], write)
        except kvretry.WriteFailed as exc:
            index_updated = False
            write_failure = write_failure or (exc, None, None)
```

`handle_bulk_action` takes the same shape in all six branches: break on
`WriteFailed`, then run the index step (`remove_slugs_from_indexes` for delete,
`move_slugs_between_owners` for reassign; enable/disable/tag/untag have no
index step and so are structurally incapable of drift).

### The partial-execution response contract

**Success is byte-identical to today.** When every write lands, both endpoints
return exactly what they return now — `201 {"count", "links"}` and
`200 {"ok": true, "action", "count"[, "tags"|"owner"]}`. This is a hard
requirement: no existing test, client or GUI branch may change behaviour on the
happy path.

When execution is partial, the status is **`207 Multi-Status`** and the body
carries `"partial": true`:

```jsonc
// POST /api/links/bulk, partial
{
  "ok": false,
  "partial": true,
  "count": 31,                       // links actually created
  "links": [ ...31 public_link objects... ],
  "not_created": [                   // the {line, slug, error} row-error shape,
    {"line": 32, "slug": "promo-b", "error": "write_failed"},   // reused verbatim
    ...
  ],
  "index_updated": true,
  "write_error": "throttled",        // "throttled" | "other" — our label, never the host string
  "next_step": "resubmit",           // "resubmit" | "consistency_repair"
  "row_count": 50
}
```

```jsonc
// POST /api/links/bulk-action, partial
{
  "ok": false, "partial": true, "action": "delete", "count": 50,
  "applied": ["a", "b", ...], "not_applied": ["c", "d", ...],
  "index_updated": false,
  "write_error": "throttled",
  "next_step": "consistency_repair"
}
```

Four decisions inside that shape:

1. **`207`, not `200` and not `5xx`.** A 5xx says "nothing happened", which is
   the current bug. A 200 says "it worked", which invites a client to skip the
   partial branch. 207 is the one status whose entire meaning is "some parts
   succeeded and some did not", and `gui/app.js`'s `apiCall` routes it to
   `ok: true` with a parsed body — so the GUI receives the detail rather than
   falling into a generic error path.
2. **`"partial": true` is the contract; the status code is documentation.**
   The GUI branches on the field. A client that only ever checks `res.ok` still
   gets `"ok": false` in the body.
3. **`not_created` reuses the existing `{line, slug, error}` row-error shape**,
   so `gui/dashboard.js`'s `renderBulkErrorTable` and `renderRowErrorList`
   render it with no new function — only a new entry in the dashboard-local
   `BULK_ERROR_MESSAGES` override map for the `write_failed` code.
4. **`next_step` is computed, not guessed.** `"consistency_repair"` when
   `index_updated` is false (records exist that the index does not describe, or
   vice versa — precisely what `POST /api/admin/consistency/repair` fixes);
   `"resubmit"` otherwise (the store is structurally sound, some rows just
   didn't land).

**This does not contradict the documented all-or-nothing language.** That
language is about validation and stays literally true: `bulk_validation_failed`
still writes nothing, and the GUI's "Nothing was created — N rows need fixing"
copy is unchanged. The sentence this plan adds, extending the analytics purge's
2026-08-10 precedent: **validation is all-or-nothing; execution is best-effort
and fully reported.**

### `api/consistencyrepair.py` — retry, and stop on exhaustion

`apply_repairs(stores_by_name, plan, write)` wraps both its `set` and its
`delete` in `write(..., kvretry.INDEX_WRITE)` — every key it touches *is* an
index — and on `WriteFailed` appends
`{"store", "key", "reason": "write_failed"}` to a new `write_failed` list and
**stops** rather than continuing. `handle_repair` surfaces `write_failed` in
the response and forces `complete: false` when it is non-empty.

This path is high value out of proportion to its size: the repair tool is what
an operator reaches for **during** an incident, i.e. exactly when the app is
throttling. A repair that 500s while fixing throttle damage is a trap. It is
also the cheapest path to protect — the 2026-08-17 incident's 174 findings
repaired in **two writes**, so retrying them costs essentially nothing.

Re-running is already safe: `handle_repair` re-runs `collect`/`analyze`
in-request and every delta write is idempotent.

### `api/analyticsorphans.py` — retry the operator purge, and NOT the inline one

`handle_orphan_purge(links_store, analytics_store, principal, request,
list_keys, write)` wraps its delete loop in `write(..., kvretry.RECORD_WRITE)`.
On `WriteFailed` it stops, and the slug whose delete failed goes back into
`remaining_slugs` so the GUI's chunk loop picks it up. The response gains
`"write_failed": true` and keeps `complete: false`.

**`purge_slug_analytics` (the inline purge on single-link delete) deliberately
does NOT get the helper — this is the "where should it not be used" answer.**
Three reasons: it runs *after* the link is already deleted, so its failure
cannot corrupt anything; its failure mode is orphaned analytics keys, which
CLAUDE.md documents as *"expected, normal, intended state between purges"* with
a shipped operator tool; and it already catches every exception and reports
`{"status": "failed", ...}` without turning a successful deletion into a 500.
Retrying here would spend the request's write budget — and add wall time to a
delete already measured at **1.9 s** — on the least valuable writes in the
application, while making app-wide cap pressure worse for everyone else.

### `api/backup.py` — report only, deliberately no retry

`handle_restore` wraps its write loop (`backup.py:299-307`) in a `try/except
Exception`, stops on the first failure, and returns **`207`** with the counts
restored and pruned so far, `stopped_at_store`, `write_error` and
`next_step: "retry_restore"` — instead of the bare 500 that today leaves a
half-restored store with no information about how far it got.

**It gets no `write` parameter and no retries.** A full-cap restore is already
documented as unable to complete inside Akamai's 30-second handler limit
(5,000 writes ≈ 100 s at the cap; CLAUDE.md, "Deployment: Akamai Functions"),
so adding sleeps makes a doomed request slower for no gain. Restore replaces
rather than merges and is therefore idempotent, so "run it again" is a genuine
next step. This path is the clearest demonstration that **retry and report are
independent decisions** and one can be taken without the other.

### `api/links.py` single-link handlers — lower priority, same shape

`handle_create` is the only single-link path with a record-then-index pair and
therefore the only one that can drift: if the record write lands and
`add_slugs_to_indexes` fails, the link exists at `/r/{slug}` and is invisible
in the dashboard — a one-row version of the incident. It returns `207
{"partial": true, "link": {...}, "index_updated": false, "next_step":
"consistency_repair"}` on that path; the `201 public_link(record)` success
shape is unchanged.

`handle_update` / `handle_set_password` are single writes with no index, so
they gain retry only and keep their existing responses.
`handle_delete` gains retry on the record delete and the index step, and
reports `index_updated` on the existing `200 {"ok": true, ...}` body.

Ranked last because each is 1–3 writes with a small collision window; land it
after the bulk paths, on its own.

## GUI changes

### `gui/dashboard.js`

Both bulk flows branch on `data.partial` before their success path.

- **Bulk create partial:** message names the split and the next move —
  `"Created 31 of 50 links. 19 rows could not be written because the store was
  busy. The list below has been left as you submitted it — re-submit only the
  rows listed here."` The `not_created` array renders through the existing
  `renderBulkErrorTable`. **The textarea is not cleared** (the same deliberate
  choice `too_many_rows` already makes at `dashboard.js:972`), and the success
  banner is not shown.
- **Bulk action partial:** message names what applied, and **the selection is
  narrowed to `not_applied` rather than cleared**, so clicking the same button
  again retries exactly the failures. That is a better next move than any
  sentence.
- **`index_updated: false`, either flow:** an additional, prominent line —
  `"Some links are not yet listed in the dashboard. They work, but they're
  invisible here until the store index is repaired."` — plus a link to
  `admin/backup.html` **only when the viewer holds `users.manage`**, using the
  same permission-gated-link pattern `gui/admin/users.html` already uses for
  its `dashboard.html?owner=` link. A viewer without it is told to ask an
  administrator to run the store consistency repair, never handed a dead link.
- `BULK_ERROR_MESSAGES` gains `write_failed: "The store was busy and this row
  wasn't written. Try again."`
- **All of this renders through the existing `.form-error` class.** No new
  token, no new CSS rule, no DESIGN.md change — following DESIGN.md's own
  recorded decision not to invent a warning colour for "needs attention but
  isn't broken".

### `gui/admin/backup.js`

- **Add a no-progress guard to the orphan-purge chunk loop** (`:574-584`),
  mirroring the repair loop's existing one at `:432-435`: if a pass returns
  `deleted_keys: 0` with the same `remaining_slugs`, stop and say
  `"The store was busy — wait a moment and try Find again."` **This is
  mandatory and must land in the same task as the purge's write reporting**,
  because that reporting is what first makes a zero-progress pass possible and
  the current loop would spin forever on one.
- The repair loop's existing `"Repair made no progress."` message becomes
  `"The store was busy — some repairs could not be written. Wait a moment and
  run the check again."` when the last response carried a non-empty
  `write_failed`, so the operator learns the cause rather than a symptom.

## Trade-offs and rejected alternatives

**1. String-matching `'too many requests'` to decide whether to retry —
rejected for control flow, kept for the label.**
Attractive because it is precise: only a genuine throttle would be retried, and
`Error_AccessDenied` (a manifest misconfiguration) would fail instantly instead
of wasting three attempts. Rejected on two grounds. First, it degrades in the
worst possible direction: if Akamai rewords the message, the match silently
stops firing and the app reverts to *exactly today's behaviour* — no retries,
bare 500, invisible — which is the bug this plan exists to remove. A vendor
copy-edit must not be able to un-ship a feature. Second, the classification
would have to live somewhere host-testable, and a pure module **cannot import
the WIT error variants at all** (`componentize_py_types` is absent from the
venv), so the only available discriminator is the string anyway. The cost of
the chosen policy is bounded and small: a permanent error wastes at most 2
extra attempts and ~400 ms on a request that was going to fail regardless, and
`AccessDenied` on a KV write is a deploy-time fault that fires on the very
first request. The string match survives as `classify_write_error`, feeding
the log line and the `write_error` field — a fragile signal used only where
being wrong costs a slightly less specific message.

**2. Retrying `redirect`'s analytics writes — rejected (agreeing with the
brief, not merely deferring to it).**
Attractive because the redirect path is where the write cap actually binds:
two writes per click, ~25 clicks/second app-wide. Rejected three times over.
The loss is already documented, measured and accepted as mechanism M2, and
CLAUDE.md is explicit that only *fewer writes per click* addresses it. A retry
would add wall time to a visitor's redirect for bookkeeping they have no stake
in — `recordAnalytics` already runs *before* `http.Redirect`, which CLAUDE.md
flags as a problem in its own right. And retrying into a saturated cap actively
worsens M2 for every other caller: the redirect path is the largest consumer of
the write budget, so it is the last place that should be re-issuing writes.
The hot path stays at 6 KV operations.

**3. Transparent retry inside `PrefixedStore.set`/`delete` — rejected.**
By far the most attractive option: one edit to `api/kvprefix.py`, every write
path in the application covered, impossible to forget at a call site. Rejected
for three reasons. It makes wall time unbounded and invisible — a 50-row bulk
create could silently grow by 50 × the full backoff schedule with no per-request
budget anywhere, against a 30-second handler limit. It puts write-cap policy
inside the module CLAUDE.md designates as *"the only Python module that knows
prefixes exist"*, whose single responsibility is namespace separation. And,
decisively, it removes the handler's ability to make the decision that matters
most: **stop, and index what landed.** A transparent retry can only ever
succeed or raise; it cannot know that abandoning the remaining 40 records and
spending the budget on the index write is the right trade. The explicit
`write(...)` call site is what makes that choice expressible.

**4. Making bulk execution truly all-or-nothing by rolling back on failure —
rejected.**
Attractive because it would make the documented "all-or-nothing" true of
execution as well as validation, and would need no new response shape at all.
Rejected because a rollback is *itself writes*, issued into the same saturated
cap that just rejected the forward write, with no transaction and no
compare-and-swap to make it correct. A rollback that itself fails halfway
leaves a store that is neither the before-state nor the after-state and about
which nothing can be said — strictly worse than a reported partial. This is the
same reasoning that made restore write-then-prune rather than wipe-then-write.

**5. Client-side pacing — deferred, not rejected outright.**
Attractive because it attacks the cause rather than the symptom: if the GUI
serialised bulk submissions and spaced them, the app would rarely reach the cap.
Insufficient on its own — the endpoints are reachable by `curl`, several users'
browsers cannot coordinate with each other, and the redirect path draws from the
same budget without the GUI's knowledge. Worth having *in addition*, so it goes
under `TASKS.md`'s Future work rather than into this plan.

**6. Retrying `analyticsorphans.purge_slug_analytics` — rejected.**
See the API section. Retrying the least valuable writes in the application, on
a delete already measured at 1.9 s, while making cap pressure worse for
everyone else.

**7. Retrying `backup.handle_restore`'s writes — rejected; report only.**
A full-cap restore already cannot finish inside the handler limit. Sleeps make
a doomed request slower. Report-only turns a silent half-restore into a
reported one, which is the whole available win.

**8. Do nothing.** A live option, and stronger than it was a week ago: the
repair tool shipped 2026-08-17 fixes the exact damage in one click, 174
findings in 368 ms. Rejected because it requires a human to notice, and the two
recorded incidents were both noticed by an engineer running
`/api/admin/consistency` for an unrelated reason. The audience — non-technical
marketing staff doing bulk work at the 50-row cap — will never open Store
maintenance, and the failure is silent by construction: the links resolve, the
dashboard just doesn't show them.

**9. Batching writes with `set_many`/`delete_many` to reduce the write count —
already rejected repo-wide, not re-litigated here.** See TASKS.md's "Considered
and rejected", 2026-08-15, and `api/kvbatch.py`'s module docstring. Recorded
only so a reader does not mistake its absence for an oversight.

## Tasks

The exact lines appended to `TASKS.md` under `## Write-throttle resilience`.
`TASKS.md` is authoritative; do not maintain checkbox state here.

```
- [ ] Spike whether the component can actually sleep (blocks every retry task; report-only tasks are unblocked) — file(s): (none — spike, reverted after) — done when: a MODULE-SCOPE `from spin_sdk.wit.imports import wasi_clocks_monotonic_clock_0_3_0_rc_2026_03_15` in api/app.py is confirmed to survive componentize-py's bundler in a real `spin up --build`, `await wait_for(500_000_000)` inside a temporary token-gated handler is timed with time.monotonic_ns() and confirmed to elapse ~500 ms (not ~0 and not a trap), it is recorded whether `await asyncio.sleep(0.1)` raises NotImplementedError as `componentize_py_async_support._Loop.call_later` predicts, the spike is reverted with `grep -c spike api/app.py` returning 0, and the outcome is recorded in TASKS.md as branch A (0.3.0-rc wait_for works — ship `_sleep_ns` as planned), B (only 0_2_6 subscribe_duration + Pollable.block works — ship that instead) or C (no sleep available — ship `_sleep_ns` as a documented no-op, retries collapse to immediate re-issue spaced only by the ~75 ms round trip, and say so in CLAUDE.md)
- [ ] Land the api/kvretry.py seam with no call sites — file(s): api/kvretry.py, api/tests/fakes.py, api/tests/test_kvretry.py — done when: `make_writer(sleep, collector=None, jitter=random.random)` returns an async `write(make_coro, policy=RECORD_WRITE)` closing over ONE per-request RetryBudget with independent record_ns/index_ns counters, `RECORD_WRITE` is 3 attempts and `INDEX_WRITE` is 6, a write that succeeds first time calls sleep zero times, a write that fails twice then succeeds returns normally having slept exactly the first two backoff values (jitter injected as a constant), a write that never succeeds raises `WriteFailed` carrying attempts/label/cause, an exhausted record budget leaves index writes with their full budget and vice versa, `classify_write_error` returns "throttled" for an error whose str() is `Error_Other(value='too many requests')` and "other" for `Error_AccessDenied()`, `direct` performs exactly one call and never sleeps, the module has zero `spin_sdk` and zero `asyncio` imports, fakes.py gains `KvThrottleError` (whose str() matches the real Err shape), `ThrottlingStore` (fails set/delete for chosen keys a chosen number of times then succeeds) and `recording_sleep()` returning (sleep, delays_list), and `cd api && uv run pytest` passes
- [ ] Teach obs.Collector's log line to show retries and write failures — file(s): api/obs.py, api/tests/test_obs.py — done when: `_KV_OP_ORDER` gains "write_retry" and "write_failed" immediately after "delete", a request that retries nothing renders a byte-identical log line to before this change, a request with 3 retries and 1 exhaustion renders `write_retry=3/<us> write_failed=1/0`, `Collector.record` still has no parameter that could accept a key, and `redirect/linkgate/obs.go` is NOT touched (`git diff redirect/` empty)
- [ ] Wire the sleep primitive and the writer into app.py with no handler consuming it (depends on the spike and the seam) — file(s): api/app.py — done when: the clock import sits at MODULE scope under the same guarded try/except ImportError shape `wasi_batch` already uses, `_sleep_ns(nanoseconds)` is defined beside `_make_raw_get_many` and is a documented no-op when the binding is absent, `_dispatch` builds `write = kvretry.make_writer(_sleep_ns, collector)` beside the existing `list_keys`/`get_many` lines and never at module scope, `spin up --build` succeeds, and every endpoint's behaviour and traced log line are unchanged
- [ ] Give links.py's three index helpers an injectable write (must land before the bulk tasks) — file(s): api/links.py, api/tests/test_links.py — done when: `add_slugs_to_indexes`, `remove_slugs_from_indexes` and `move_slugs_between_owners` each take `write=kvretry.direct` and route every `store.set` through `await write(lambda: ..., kvretry.INDEX_WRITE)`, all existing call sites and all ~20 seeding call sites in the test suite are unchanged, a test passes a recording writer and asserts each helper's writes carry `INDEX_WRITE`, and `cd api && uv run pytest` passes
- [ ] Make bulk create survive a throttled write and report what landed — file(s): api/bulk.py, api/app.py, api/tests/test_bulk.py — done when: `handle_bulk_create` takes `write` as a REQUIRED parameter with no default, the record loop breaks on `kvretry.WriteFailed` instead of propagating, `add_slugs_to_indexes` is then called with EXACTLY the records that landed and receives the same `write`, a fully successful create returns a byte-identical `201 {"count","links"}` to today, a partial create returns `207` with `partial`/`count`/`links`/`not_created` (in the existing `{line,slug,error}` shape with error `write_failed`)/`index_updated`/`write_error`/`next_step`, `next_step` is "consistency_repair" when `index_updated` is false and "resubmit" otherwise, a `ThrottlingStore` failing only the `links:all_links` write yields `index_updated: false` with every record still written (this is the test that pins bulk threading `write` down into the index helper), a `ThrottlingStore` failing the 32nd record write yields 31 created links and 19 `not_created` rows with `index_updated: true` and ZERO unindexed_link findings from a subsequent `consistency.collect`/`analyze`, validation errors still return `400 bulk_validation_failed` having written nothing, and `cd api && uv run pytest` passes
- [ ] Make bulk-action survive a throttled write and report what landed — file(s): api/bulk.py, api/app.py, api/tests/test_bulk.py — done when: `handle_bulk_action` takes `write` as a REQUIRED parameter, all six action branches break on `kvretry.WriteFailed`, the delete branch calls `remove_slugs_from_indexes` with only the slugs actually deleted and the reassign branch calls `move_slugs_between_owners` with only the records actually rewritten, a fully successful action returns a byte-identical `200 {"ok": true, ...}` to today, a partial action returns `207` with `partial`/`action`/`applied`/`not_applied`/`index_updated`/`write_error`/`next_step`, a throttled delete leaves zero `missing_link_record` and zero `orphan_owner_index_entry` findings, enable/disable/tag/untag report a partial with `index_updated: true` (they have no index step), and `cd api && uv run pytest` passes
- [ ] Make the consistency repair survive a throttled write — file(s): api/consistencyrepair.py, api/app.py, api/tests/test_consistency_repair.py — done when: `apply_repairs` takes `write`, routes every `set` and `delete` through it with `kvretry.INDEX_WRITE`, stops on `kvretry.WriteFailed` rather than continuing, and returns a `write_failed` list of `{store,key,reason}`; `handle_repair` surfaces `write_failed` and forces `complete: false` when it is non-empty; a clean repair's response is byte-identical to today apart from an empty `write_failed`; a repair against a `ThrottlingStore` reports the failed key and `complete: false`; no `gather`/`set_many`/`delete_many` appears anywhere in the file; and `cd api && uv run pytest` passes
- [ ] Make the orphan purge survive a throttled delete, and stop the GUI loop spinning (both halves in one task) — file(s): api/analyticsorphans.py, api/app.py, api/tests/test_analytics_orphans.py, gui/admin/backup.js — done when: `handle_orphan_purge` takes `write`, routes its deletes through it with `kvretry.RECORD_WRITE`, stops on `kvretry.WriteFailed`, returns the un-deleted slug in `remaining_slugs` and sets `write_failed: true` with `complete: false`; `purge_slug_analytics` is deliberately UNCHANGED and takes no writer (a comment records why); and `gui/admin/backup.js`'s purge chunk loop halts with "The store was busy — wait a moment and try Find again." when a pass returns `deleted_keys: 0` with an unchanged remaining list, verified to previously loop forever against a stubbed zero-progress response
- [ ] Make a throttled restore report how far it got instead of 500ing (report only — no retry, deliberately) — file(s): api/backup.py, api/tests/test_backup.py — done when: `handle_restore` catches a write failure in its write and prune loops, stops, and returns `207` with `partial: true`, the `restored`/`pruned` counts achieved so far, `stopped_at_store`, `write_error` and `next_step: "retry_restore"`; a successful restore's `200` body is byte-identical to today; `handle_restore` takes NO `write` parameter and `api/backup.py` contains no `kvretry` import; and `cd api && uv run pytest` passes
- [ ] Extend retry and partial reporting to the single-link handlers — file(s): api/links.py, api/app.py, api/tests/test_links.py — done when: `handle_create`, `handle_update`, `handle_delete` and `handle_set_password` take `write=kvretry.direct` and route their record writes through it, `handle_create` returns `207 {"partial": true, "link": {...}, "index_updated": false, "next_step": "consistency_repair"}` when the record lands but the index write is exhausted while its `201 public_link(record)` success shape is unchanged, `handle_delete` reports `index_updated` on its existing `200` body, the unreadable-record delete branch is unchanged, and `cd api && uv run pytest` passes
- [ ] Render partial bulk results and the repair next-step in the dashboard — file(s): gui/dashboard.js — done when: both bulk flows branch on `data.partial` before their success path, a partial create names the created/not-created split and leaves the textarea untouched while rendering `not_created` through the existing `renderBulkErrorTable`, a partial action narrows the selection to `not_applied` so the same button retries exactly the failures, `index_updated: false` additionally shows "Some links are not yet listed in the dashboard..." with a link to admin/backup.html shown ONLY to a viewer holding `users.manage` (and the missing permission named otherwise), `BULK_ERROR_MESSAGES` gains a `write_failed` entry, every new message renders through the existing `.form-error` class, and `git diff DESIGN.md .impeccable/design.json gui/theme.css` is empty
- [ ] Document write-throttle resilience in CLAUDE.md (depends on every task above) — file(s): CLAUDE.md — done when: a new peer section records that throttling arrives as `Err(Error_Other(value='too many requests'))` with no dedicated variant, that every write error is treated as retryable because a pure module cannot import the WIT error types and a message rewording must not silently disable retries, that `classify_write_error`'s string match is observability-only, that `asyncio.sleep(d>0)` raises inside the component because `_Loop.call_later` is unimplemented and which sleep primitive the spike selected, the four `kvretry` constants with their ~11.2 s worst-case arithmetic at the measured 75 ms/write and the raise-only-with-evidence rule, that a throttled bulk run now indexes exactly what landed and therefore no longer produces `unindexed_link`, the `207`/`partial` contract and the sentence "validation is all-or-nothing; execution is best-effort and fully reported", that `purge_slug_analytics` and `backup.handle_restore` deliberately do NOT retry and why, that writes are still never gathered or batched, and that `redirect` is untouched; the "Bulk link management" section's all-or-nothing paragraph is amended to say validation rather than execution; and `git diff redirect/ DESIGN.md .impeccable/design.json` is empty
- [ ] End-to-end manual verification of write-throttle resilience — file(s): (none — verification step) — done when: against a real `SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false spin up --build --runtime-config-file runtime-config.toml`, a TEMPORARY fault injection in api/kvprefix.py (raise on the Nth `set`) has been used to drive a 20-row bulk create into a partial result in the browser, the dashboard shows the created/not-created split, `GET /api/admin/consistency` reports ZERO `unindexed_link` findings afterwards, a second injection failing only `links:all_links` produces `index_updated: false` with the Store maintenance link visible to an admin, one click of Repair all clears the store to `ok: true`, the injection is reverted with `git diff api/kvprefix.py` empty, and with no injection the bulk create/delete/tag/reassign flows plus restore and the orphan purge all behave byte-identically to before
- [ ] Deploy and trace a real throttled bulk run on Akamai — file(s): (none — measurement) — done when: a build carrying this work is deployed with `app_version` and `log_debug_token` set and confirmed live via `X-SS-Version`, concurrent bulk creates are fired past the 50 writes/second cap the same way the 2026-08-17 incident was reproduced, an `X-SS-Debug` trace shows `write_retry` (and `write_failed` if exhausted), the response is a `207` rather than a `500`, `GET /api/admin/consistency` afterwards reports zero `unindexed_link`, the modelled ~11.2 s worst case is compared against real wall time, and the numbers are recorded in TASKS.md
```

## Critical files

- `api/kvretry.py` **(new)**
- `api/tests/test_kvretry.py` **(new)**
- `api/obs.py`
- `api/app.py`
- `api/links.py`
- `api/bulk.py`
- `api/consistencyrepair.py`
- `api/analyticsorphans.py`
- `api/backup.py`
- `api/tests/fakes.py`
- `api/tests/test_bulk.py`
- `api/tests/test_links.py`
- `api/tests/test_obs.py`
- `api/tests/test_backup.py`
- `api/tests/test_consistency_repair.py`
- `api/tests/test_analytics_orphans.py`
- `gui/dashboard.js`
- `gui/admin/backup.js`
- `CLAUDE.md`
- `TASKS.md`

Deliberately **not** touched: `redirect/` (out of scope — Trade-offs #2),
`gui-pages/`, `spin.toml`, `runtime-config.toml`, `Jenkinsfile` (no change to
how tests are invoked), `DESIGN.md`, `.impeccable/design.json`, `gui/theme.css`
(no new token — the partial banner reuses `.form-error`).

## Verification

Run in this order.

1. **Baseline, before any change** — confirm the suites are green so a later
   failure is attributable:
   ```bash
   cd api && uv run pytest            # expect 629 passed (measured 2026-08-17)
   cd gui-pages && uv run pytest       # expect 71 passed
   cd redirect && go test ./linkgate/...
   ```
   Never `go test ./...` — it fails by design on `package main`.
2. **After the spike (task 1)** — the spike leaves no code; confirm with
   `grep -c spike api/app.py` returning `0` and `git status` clean.
3. **After each API task** — `cd api && uv run pytest`. The suite must grow,
   never shrink, and no pre-existing test may need editing except to pass the
   new `write` parameter.
4. **Confirm `redirect` is untouched at every stage** —
   `git diff redirect/` must be empty, and `cd redirect && go test ./linkgate/...`
   must stay green.
5. **Confirm no write was gathered or batched** —
   `grep -n "gather\|set_many\|delete_many" api/bulk.py api/backup.py api/consistencyrepair.py api/analyticsorphans.py api/kvretry.py`
   must show no write ever passing through any of them.
6. **Mutation-check the guard that matters.** Temporarily change
   `bulk.handle_bulk_create` to call `links.add_slugs_to_indexes` without
   forwarding `write`, and confirm the `index_updated: false` test fails.
   Restore, and confirm it passes again. That test is the only thing standing
   in for the required-parameter discipline on the index helpers.
7. **Full app, with a temporary fault injection** (this is the only way to
   reach a throttle at all — local sqlite has no write cap, so the partial
   path is unreachable without one):
   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=devpass123 SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```
   With a temporary `raise` on the Nth `PrefixedStore.set` in
   `api/kvprefix.py`:
   - a 20-row bulk create returns **207**, the dashboard shows the
     created/not-created split, and `GET /api/admin/consistency` reports
     **zero `unindexed_link`** — this is the headline pass condition;
   - failing only the `links:all_links` write yields `index_updated: false`,
     the Store-maintenance link appears for an admin, and one click of "Repair
     all repairable findings" drives the store to `ok: true`;
   - the orphan-purge loop, driven to a zero-progress pass, **halts** with the
     busy message rather than spinning.
   Then revert: `git checkout api/kvprefix.py` and confirm
   `git diff api/kvprefix.py` is empty.
8. **Full app, no injection** — bulk create, bulk delete, bulk tag, bulk
   reassign, single-link create/edit/delete, backup export, restore and the
   orphan purge all behave exactly as before, and with
   `SPIN_VARIABLE_LOG_LEVEL=summary` the log lines in `.spin/logs/api_stderr.txt`
   carry **no** `write_retry` or `write_failed` field on any request.
9. **Deployed (deferred until the user next deploys)** — the final task line
   above: reproduce the 2026-08-17 incident's concurrent-bulk pattern against
   Akamai, trace it with `X-SS-Debug`, and record the real wall time against
   the modelled 11.2 s worst case.

## Out of scope / follow-ups

- **`redirect`.** Settled; see Trade-offs #2. Recorded under "Considered and
  rejected" rather than left implicit.
- **`auth.create_session` / `auth.ensure_bootstrap_admin` / `users.handle_delete`
  / `urlpolicy.handle_put_policy`.** Single writes on low-frequency paths. A
  throttled login still 500s after this plan. Cheap to add later once the seam
  exists; filed under Future work.
- **Client-side and server-side write pacing** — attacking the cause rather
  than the symptom. Trade-offs #5; filed under Future work.
- **Raising any of the four `kvretry` constants.** Standing rule: needs real
  timing evidence from a full-cap run. The deployed-trace task is what would
  produce it.
- **Chunked or resumable restore.** Already an existing Future-work entry
  (2026-08-04); this plan makes a throttled restore *reportable*, not
  *completable*.
- **A `write_failed` consistency check.** Explicitly not proposed — the whole
  point of the abandon-and-index-what-landed rule is that a throttled run
  leaves a structurally clean store, so there is nothing new for the checker to
  find.
