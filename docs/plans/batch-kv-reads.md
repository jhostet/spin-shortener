# Batch KV Reads (`wasi:keyvalue/batch` `get_many`)

## Context

Every read-heavy path in `api` today issues one KV host call per key and hides
the latency with `api/kvbatch.py`'s `gather_reads` (bounded at 100 concurrent,
order-preserving, reads only). That works — CLAUDE.md's "Parallel KV reads"
records 2.7–50.3× measured overlap — but it does not reduce the *number* of
reads, and the number of reads is what binds:

- `GET /api/analytics/click-totals` at 100 links × 200 clicks issues ~6,126
  reads, **613% of the app's entire 1,000 reads/second budget for one dashboard
  load** (TASKS.md, "MEASURED: the Clicks column does not scale", 2026-08-11).
- `GET /api/admin/backup` issued 999 operations summing to 48 s of KV time
  inside 957 ms of wall (TASKS.md, "Parallelize sequential KV reads").
- Two handlers were found during this planning pass to have **no** fan-out
  mitigation at all: `bulk.handle_bulk_action` reads up to 50 records in a
  strictly sequential `for` loop (`api/bulk.py:327-328`) and
  `bulk.handle_bulk_create` does up to 50 sequential `exists` probes
  (`api/bulk.py:229`). At the 2026-08-15 measured 7–25 ms per read that is
  0.35–1.25 s of serial reads before either handler writes anything.

`wasi:keyvalue/batch`'s `get_many` collapses K reads into one host call. It was
spiked three times on 2026-08-15 (twice locally, once on the deployed app) and
**measured at ~one round trip regardless of K** — `get_many(1)` ≈ 20.4 ms,
`get_many(100)` ≈ 23–77 ms, a ~90× fall in per-key cost. Those spikes were
reverted; nothing of them is in the tree.

Two facts remain unknown, **and they change the design, so they are the first
two tasks and the rest of this document branches on their answers rather than
assuming one**: whether there is a maximum K (or a response-size ceiling), and
whether Akamai bills a `get_many` of K keys as 1 read or K.

Motivating TASKS.md entries: "The Clicks column's read cost — re-costing the
denormalised total (2026-08-12)", "The `get_many` spike — ANSWERED, and the
answer is no (2026-08-15)" **and its same-day RETRACTION**, and "MEASURED ON
AKAMAI: `get_many` is ~ONE round trip regardless of K (2026-08-15)".

**Confirmed decisions (settled before planning, carried in from the brief):**

- Never batch **writes**. `set_many`/`delete_many` are out of scope entirely and
  are recorded below as a rejected alternative with the arithmetic, not as a
  task.
- `redirect/` is not touched. It reads one key per click; batching buys it
  nothing.
- Pure modules stay host-testable: zero `spin_sdk` imports, dependencies passed
  as plain parameters.
- No new module may shadow a stdlib name.
- A staged adoption is acceptable and expected.

## Key technical facts confirmed during research

- **`get_many`'s exact signature**, read from
  `api/.venv/lib/python3.14/site-packages/spin_sdk/wit/imports/wasi_keyvalue_batch_0_2_0_draft2.py:38`:
  `def get_many(bucket: wasi_keyvalue_store_0_2_0_draft2.Bucket, keys: List[str]) -> List[Tuple[str, Optional[bytes]]]`.
- **The batch bindings are SYNCHRONOUS; Spin's KV bindings are `async`.**
  `wasi_keyvalue_batch_0_2_0_draft2.get_many` and
  `wasi_keyvalue_store_0_2_0_draft2.open` are plain `def`
  (`wasi_keyvalue_store_0_2_0_draft2.py:173`), while every method on
  `spin_key_value_key_value_3_0_0.Store` is `async def`
  (verified by grep: `async def open/get/set/delete/exists/get_keys`, lines
  48–91). **Three consequences, all load-bearing:** a `get_many` call cannot be
  overlapped with anything — `asyncio.gather` over several `get_many` calls
  would run them one after another, so **chunk count multiplies wall time
  directly**; it blocks the instance for its duration, so a concurrent request
  dispatched through `componentize_py_async_support.spawn` waits; and the
  wrapper below is `async def` purely for call-site uniformity, never awaiting
  anything.
- **The WIT doc and the observed behaviour disagree about missing keys.** The
  generated docstring says "If any of the keys do not exist in the store, it
  returns a `none` value for that pair in the list"; the 2026-08-15 local spike
  observed missing keys **omitted entirely** (TASKS.md, RETRACTION section).
  The seam below therefore handles **both** forms — a `(key, None)` pair and an
  absent pair are normalised to the same thing.
- **Result order is not preserved** (2026-08-15 spike: "the reply came back in a
  different order than requested"). `backup.py:240` and `consistency.py:167,224`
  both do `zip(keys, values)` today, so a positional swap would silently
  mis-associate values with keys — data corruption, not a crash.
- **`spin.toml`'s `key_value_stores` allowlist still applies** to a
  `wasi:keyvalue/store`-opened bucket: `open("users")` and `open("links")` were
  both refused, only `"default"` opened (2026-08-15 spike). So this opens no new
  security hole and `api/kvprefix.py`'s prefixing remains the only namespace
  boundary, exactly as today.
- **componentize-py bundles only modules reachable from a TOP-LEVEL import.** A
  function-level import produces a bare `ModuleNotFoundError` that reads like
  "not supported" — this is what caused the wrong "batch is unreachable"
  conclusion earlier on 2026-08-15. Any import of the batch bindings must be at
  module scope.
- **Akamai quota accounting for batch operations is UNDOCUMENTED.** Fetched
  2026-08-15: `techdocs.akamai.com/akamai-functions/docs/use-the-key-value-store`
  confirms verbatim that "the `wasi:keyvalue/store` and `wasi:keyvalue/batch`
  interfaces are supported" and that `wasi:keyvalue/atomic` is not, but contains
  **no** statement about how reads are counted, whether a batch counts as one
  request, or any maximum keys per batch.
  `techdocs.akamai.com/akamai-functions/docs/quotas-and-limits` gives the flat
  rows only (1,000 read RPS, 50 write RPS, 1 MB max value, 8 KB max key, 10 MiB
  request/response, 30 s handler) with no counting methodology. **Both unknowns
  are therefore UNCONFIRMED and must be spiked or asked.** The KV page does say
  "Rates can be increased to meet production needs per customer request", which
  establishes that a support channel exists — that is the cheap route for
  Unknown 2.
- **`wasi_keyvalue_store.Bucket.list_keys(cursor)` takes a cursor** (line 139),
  unlike `spin:key-value`'s argument-less `get_keys()`. It still takes **no
  prefix**, so CLAUDE.md's rule stands unchanged: any fix for enumeration cost
  must *avoid* the enumeration, not narrow it. Out of scope here; noted so it is
  not mistaken for a new lever.
- **Baseline is green:** `cd api && uv run pytest` → **572 passed** in 12.13 s,
  run 2026-08-15 before any change.
- **No cross-language test pins the logfmt op-type vocabulary.** `keys.go`'s
  prefixes and `CountShards` are pinned by `api/tests/test_kvprefix.py`;
  `redirect/linkgate/obs.go:13`'s `kvOpOrder` is not pinned against
  `api/obs.py:21`'s `_KV_OP_ORDER`. Adding Python-only op types is therefore
  safe, and deliberate: `redirect` will never batch.

## Unknown 1 — is there a maximum K, or a size ceiling?

Only 114 keys existed when `get_many` was measured. `handle_click_totals` at
100 links × 200 clicks wants ~6,100 keys, and `backup.handle_export` reads every
value in the store, where the documented max value size is 1 MB.

**The design does not wait for this answer** — it chunks from day one, so the
spike sets a constant rather than changing a shape. But the constant's value,
and whether `backup.handle_export` adopts at all, do depend on it.

**Spike A, on the deployed app, token-gated, reverted afterwards.** Seed junk
keys directly into the analytics namespace as `analytics:count:zzspikeNNNN:0`
(the slug form matches `CUSTOM_SLUG_PATTERN`, so they are orphans by
construction and the **shipped orphan purge cleans them up** —
`POST /api/admin/analytics/purge`, 50 slugs / 250 keys per request — rather than
needing bespoke teardown). Seeding is write-capped at 50/s and bounded by the
30 s handler limit, so seed in chunks of ≤ 500 per request.

Measure, at K = 100, 500, 1,000, 2,000, 5,000, 10,000: the number of pairs
returned, the wall time, and whether the call raises. Then a **separate size
arm**: 100 KB values at K = 10, 50, 100, 200 (i.e. 1 MB → 20 MB of returned
bytes) to find whether a byte ceiling exists independent of key count.

**How the design branches:**

| outcome | what changes |
|---|---|
| **A — no cap up to 10,000, no size error below ~10 MB** | `MAX_KEYS_PER_GET_MANY = 500` on byte-safety grounds alone (500 × the ~1.9 KB worst-case analytics blob ≈ 950 KB). Every stage below proceeds. |
| **B — a hard cap at K_max** | `MAX_KEYS_PER_GET_MANY = min(500, K_max)`. Nothing structural changes; the chunk loop already exists. If K_max forces many chunks, remember chunks are **sequential** (sync bindings) — at K_max = 128 a 6,100-key `click-totals` is 48 chunks ≈ 1.2 s, which is still ~5× better than today but no longer "one round trip", and the cached-totals blob gets more attractive. |
| **C — a size ceiling that a 500-key chunk of link records can breach** | `backup.handle_export` gets its own smaller constant, or does not adopt (Stage 6 is dropped). Nothing else changes: analytics blobs and link records are hundreds of bytes. |
| **D — cap below ~20 keys** | Stop. The win does not justify a second KV interface. Record the outcome and abandon Stages 1–6; only the seam and its tests would have landed. |

## Unknown 2 — quota accounting

**The 2026-08-15 measurement establishes round trips, not billing.** Akamai's
cap is 1,000 KV reads/second app-wide, shared with redirects.

**Route 1, preferred because it is authoritative and cheap: ask Akamai.** The
docs already invite rate conversations ("Rates can be increased to meet
production needs per customer request"), so ask the account/support contact
directly: *does a `wasi:keyvalue/batch` `get-many` of K keys count as 1 read
request or K against the 1,000 RPS per-app cap?* If a written answer arrives,
Route 2 is unnecessary.

**Route 2, if no answer: measure, but establish a positive control first.** The
experiment is only interpretable if exceeding the read cap is *observable*, and
nothing in this repo has ever observed it. So:

1. **Positive control.** One token-gated spike request that issues ~2,000
   single `get`s through `gather_reads` (bounded 100, ~20 ms each → ~400 ms →
   ~5,000 reads/s). Record errors, per-op latency distribution, and any knee.
   **If this shows nothing at all, the measurement route is closed** — say so
   and fall back to Route 1 or to assuming the pessimistic answer.
2. **Test arm.** One request that issues 40 × `get_many(100)` = 4,000 keys. If
   billed as K that is ~4,000 reads inside well under a second and should show
   the control's signature. If billed as 1, it is 40 reads and should show
   nothing.

**How the design branches:**

| outcome | what changes |
|---|---|
| **1 read per `get_many`** | The read-cap problem that motivated this whole investigation is **solved** for every batched path. The cached-totals blob's first trigger ("the `get_many` spike failing or coming back billed per-key") is retired; it stays under Future work for the *enumeration* term only, which batching does not touch. Say this in CLAUDE.md. |
| **K reads per `get_many`** | **The wall-time win still stands and this still ships** — one round trip instead of K is worth having regardless. But say plainly: the cap problem is **not** moved, `click-totals` at 100 links × 200 clicks is still ~6,126 reads per dashboard load, and **the cached totals blob remains the front-runner answer to the cap**, with its trigger intact. Do not let a latency win be mistaken for a cap fix. |

Either way `handle_click_totals` still pays one full-store `list_keys`
(~68.7 µs per physical key plus about one KV round trip — CLAUDE.md "Parallel KV
reads"), which batching does not address at all.

## The seam: `kvbatch.scoped_get_many`

`api/kvbatch.py` gains a second helper alongside `gather_reads`. **No new
module** — the two multi-key read helpers live together so the "ordered list vs.
keyed dict" decision is made in one docstring, and `api/kvprefix.py` stays a
leaf with no local imports. `kvbatch.py` imports `PrefixedStore` from
`kvprefix.py`; that is acyclic.

```python
MAX_KEYS_PER_GET_MANY = 500  # value set by Spike A; see Unknown 1


def scoped_get_many(raw_get_many, collector=None):
    """Wrap a raw get_many(physical_keys) callable into one that takes a
    PrefixedStore and a list of LOGICAL keys.

    Returns dict[str, bytes | None] containing EVERY requested key, with None
    for keys the store does not hold.
    """
```

**Five properties, each deliberate:**

1. **It returns a dict, never a list.** `get_many` does not preserve order, so a
   positional result would be a data-corruption bug waiting for a caller. A dict
   makes order a non-question.
2. **Every requested key is present in the result, with `None` for a miss.**
   This is what makes it a *drop-in* for the existing
   `dict(zip(keys, await gather_reads(...)))` idiom in `backup.py` and
   `consistency.py`. It also preserves `consistency.py:230`'s deliberate
   `users_values[key]` **indexing** (its comment says a future branch reading an
   un-allowlisted key must "fail loudly" with a `KeyError`) — an omitting result
   would turn a benign enumerate-then-delete race into a 500. Both awkward
   semantics are normalised away here, in ~10 lines, under test.
3. **It requires a `PrefixedStore` and raises `TypeError` otherwise**, exactly
   like `kvprefix.scoped_list_keys`. Physical keys are built by prefixing inside
   the helper; a caller never sees or supplies one. **A returned key that does
   not carry this view's prefix is dropped, never returned** — the host has no
   reason to send one, and dropping it keeps the namespace invariant explicit
   rather than incidental. `PrefixedStore` gains **no** batched method and still
   has no `get_keys`: the view's four-method surface is unchanged, so the
   security posture CLAUDE.md documents for it is untouched.
4. **It chunks, always, sequentially.** Chunks cannot overlap (sync bindings),
   so chunk count is a direct wall-time multiplier — that is the reason the
   constant matters and the reason Spike A runs first.
5. **It falls back per chunk, visibly.** If `raw_get_many` raises, that chunk is
   re-read through `gather_reads(store.get(k) for k in chunk)` and the request
   still succeeds. The failure is recorded as a `get_many_error` operation, so a
   trace shows it unambiguously rather than the path silently being slow forever.

Load-bearing body (the exact text matters for the prefix handling and the
both-forms normalisation):

```python
    async def get_many(store, keys):
        if not isinstance(store, PrefixedStore):
            raise TypeError(
                "scoped_get_many requires a PrefixedStore; batching against "
                "the physical store directly would cross namespace boundaries"
            )
        prefix = store.prefix
        wanted = list(dict.fromkeys(keys))        # de-duplicated; order is irrelevant
        results: dict[str, bytes | None] = {key: None for key in wanted}

        for start in range(0, len(wanted), MAX_KEYS_PER_GET_MANY):
            chunk = wanted[start:start + MAX_KEYS_PER_GET_MANY]
            t0 = time.monotonic_ns()
            try:
                pairs = raw_get_many([prefix + key for key in chunk])
            except Exception:
                if collector is not None:
                    collector.record("get_many_error", store._namespace(),
                                     time.monotonic_ns() - t0, 0, num_keys=len(chunk))
                for key, value in zip(chunk, await gather_reads(store.get(k) for k in chunk)):
                    results[key] = value
                continue
            num_bytes = 0
            for physical_key, value in pairs:
                if not physical_key.startswith(prefix):
                    continue                      # never surface another namespace's key
                logical = physical_key[len(prefix):]
                if logical in results and value is not None:
                    results[logical] = value
                    num_bytes += len(value)
            if collector is not None:
                collector.record("get_many", store._namespace(),
                                 time.monotonic_ns() - t0, num_bytes, num_keys=len(chunk))
        return results
```

Note the `value is not None` guard: it absorbs the WIT-documented `(key, None)`
form and the observed omission into one behaviour, so whichever the host does,
the result is identical.

`gather_reads` **stays**. It is the fallback path, it is what
`analyticsorphans`' `exists` fan-out uses (there is no `exists_many`), and its
docstring gains a "which one do I use" paragraph: `gather_reads` for
heterogeneous coroutines or `exists` probes, `scoped_get_many` for a set of
plain key reads in one namespace.

## Opening the bucket: `api/app.py` wiring

`get_many` needs a `wasi_keyvalue_store.Bucket`, a **different WIT resource
type** from the `spin_key_value.Store` the app opens — so a second `open` is
required. On Akamai `open` measured ~154 µs against ~20 ms per data operation,
so the handle itself is nearly free; the question is when to pay it.

**Decision: opened lazily, at most once per request, never at module level.**

- Lazily, because a request that never batches (login, `PATCH`, every write
  path) should not pay for it, and because a failure in `wasi_store.open` should
  be confined to the paths that actually batch rather than breaking every
  endpoint.
- Never at module level, for the same reason `api/obs.py`'s `Collector` is never
  module-level: `Handler.handle()` dispatches each request through
  `componentize_py_async_support.spawn`, so a shared resource handle would be
  shared across concurrently-dispatched requests, and Spin's own
  `Error_StoreTableFull` shows handles are a bounded resource with a lifetime.
  Every other store handle in this app is per-request; match it.

```python
# api/app.py — TOP-LEVEL. componentize-py bundles only modules reachable from a
# top-level import; a function-level import yields a bare ModuleNotFoundError
# that reads like "the interface is unsupported" (TASKS.md, 2026-08-15).
try:
    from spin_sdk.wit.imports import wasi_keyvalue_batch_0_2_0_draft2 as wasi_batch
    from spin_sdk.wit.imports import wasi_keyvalue_store_0_2_0_draft2 as wasi_store
except ImportError:  # toolchain did not bundle it; every caller falls back
    wasi_batch = None
    wasi_store = None


def _make_raw_get_many(collector):
    """Per-request closure over one lazily-opened wasi bucket."""
    bucket = None

    def raw_get_many(physical_keys):
        nonlocal bucket
        if wasi_batch is None or wasi_store is None:
            raise RuntimeError("wasi:keyvalue/batch not available in this build")
        if bucket is None:
            start = time.monotonic_ns()
            bucket = wasi_store.open(kvprefix.PHYSICAL_STORE)
            if collector is not None:
                collector.record("open", "-", time.monotonic_ns() - start, 0)
        return wasi_batch.get_many(bucket, physical_keys)

    return raw_get_many
```

The guarded `try/import/except ImportError` is still a module-scope import
statement, so the bundler follows it; **Spike A must confirm that**, since it is
the one place the toolchain's behaviour is being assumed rather than measured.
If the guard turns out to defeat bundling, drop the guard and take the bare
import — a component that fails to start is loud and is caught by the first
local `spin up --build`.

In `_dispatch`, next to the existing `list_keys` line:

```python
        get_many = kvbatch.scoped_get_many(_make_raw_get_many(collector), collector)
```

and threaded into handlers exactly as `list_keys` already is.

**`get_many` is a required parameter on every handler that takes it — no
default.** Precedent: `bulk.validate_bulk_rows` takes the URL policy as a
required fourth parameter with no default, because "a default is exactly how the
bulk path would stay silently open". Here the failure is different in kind but
identical in shape: a default would let a call site silently never batch, which
is invisible and is precisely the failure this work exists to remove.

## Instrumentation (`api/obs.py`)

A `get_many` is **one host operation covering K keys**. `kv_ops` has always
counted host operations and must keep doing so — but K must not vanish, because
`kv_ops` and the traced `get` count are the numbers this repo has repeatedly
reasoned from, and CLAUDE.md names "a traced production
`GET /api/analytics/click-totals` showing a `get` count above ~500" as a trigger
for the cached-totals blob. **After `click-totals` batches, that `get` count
reads 1 forever and the trigger silently never fires.** Fixing that is not
cosmetic.

Changes:

1. `Collector.record(op_type, namespace, duration_ns, num_bytes=0, num_keys=1)`
   — a fifth, keyword-defaulted parameter. Every existing call site is
   unchanged. **It is still structurally impossible to log a key**: `num_keys`
   is a count, not a key, and `record` still has no parameter that could accept
   one.
2. `_stats` entries become `[count, total_us, total_bytes, total_keys]`;
   `totals()` returns `(ops, us, bytes, keys)`.
3. `render_log_line` emits `kv_keys=<n>` immediately after `kv_bytes`, **only
   when `keys != ops`** — so every non-batching request's line is byte-identical
   to today's, and `kv_keys` appearing at all means a batch happened.
4. `_KV_OP_ORDER` gains `"get_many"` and `"get_many_error"` after `"get"`.
5. `render_server_timing` keeps `desc="N ops"` (host calls) unchanged.

**`redirect/linkgate/obs.go` is deliberately NOT changed to match.** No test
pins the two vocabularies against each other (unlike `keys.go`'s prefixes and
`CountShards`, which `api/tests/test_kvprefix.py` does pin), and `redirect` will
never batch — a Go-side `get_many` field would be dead code. This divergence
must be stated in the CLAUDE.md task, because CLAUDE.md currently says `obs.py`
"mirrors `redirect/linkgate/obs.go`'s shape".

**The monitoring consequence, to be written into CLAUDE.md:** once
`click-totals` batches, the number to watch is **`kv_keys`**, not the `get`
count, and the cached-totals-blob trigger of ~500 transfers to it unchanged.

## Adoption order

Ranked by benefit ÷ risk. Each stage is independently landable and independently
revertible.

| stage | call site | keys | value size | frequency | why here |
|---|---|---|---|---|---|
| 1 | `analytics.handle_click_totals` (`api/analytics.py:148`) | grows with traffic, ~6,100 at the modelled ceiling | ~0.2–1.9 KB blobs | every dashboard load | The motivating one. Already keyed by name (`by_key.get(key)`), so it is a two-line change. `_merge_counts` already tolerates absent/corrupt blobs. |
| 2 | `analytics.handle_analytics` (`api/analytics.py:183`) | fixed 95 (`COUNT_SHARDS + 1` + 30 slots) | small | every detail-page load | Bonus: it uses a **bare `asyncio.gather`**, not `gather_reads`, so it is the one unbounded fan-out left in the component. Batching removes the fan-out entirely. Requires rewriting the positional `fetched[:len(count_keys)]` slicing into key lookups. |
| 3 | `links.handle_list` (`api/links.py:266`) | one per link | ~300–600 B records | every dashboard load | Needs a small refactor: it currently gathers `get_link()` coroutines (which JSON-parse). Fetch `slug:<slug>` bytes with `get_many`, then `json.loads` in the loop, preserving index order by iterating `slugs`. Keep today's behaviour on a corrupt record (it raises today; do not silently change that). |
| 4 | `bulk.handle_bulk_action` (`api/bulk.py:327-328`) and `bulk.handle_bulk_create` (`api/bulk.py:229`) | ≤ 50 | small | every bulk action | **Newly found: neither has any fan-out mitigation.** Both are strictly sequential loops. `handle_bulk_create`'s `exists` probes become one `get_many` over `slug:<slug>` with `is not None` as the existence test — trading 50 round trips for one, at the cost of ~25 KB of record bytes nobody reads. Write ordering and all-or-nothing semantics are untouched: this changes only how the pre-write reads are issued. |
| 5 | `consistency.collect` (`api/consistency.py:167` and `:224`) | whole links store + an allowlisted users subset | mixed | admin-only | True drop-in — both sites are already `dict(zip(keys, gather_reads(...)))`. **The users-side allowlist must stay exactly as it is**: `test_collect_never_even_reads_a_user_record_value` asserts `get()` is never called on a `user:` key, and batching must not become an excuse to "simplify" it into the wholesale links-side shape. |
| 6 | `backup.handle_export` (`api/backup.py:238`) | every key in the store (999 measured) | **up to 1 MB each** | rare | Largest fan-out and largest measured win, but also the only path where a 500-key chunk could carry hundreds of MB. **Gated on Spike A's size arm.** If a byte ceiling exists, give this call site its own smaller constant or leave it on `gather_reads` — it already runs in 957 ms at 50.3× overlap, so the status quo is not broken. |

**Deliberately NOT adopted: `analyticsorphans`' liveness pre-check**
(`api/analyticsorphans.py:334`). It uses `exists`, which has no batch
equivalent, so adopting means fetching up to 50 full records to answer a boolean.
The arithmetic kills it: that request's cost is dominated by up to 250
**sequential deletes** at the 2026-08-15 measured ~75 ms each ≈ 19 s, against
which the ~150 ms the read side could save is noise. Revisit only if a traced
purge shows the pre-check is a material fraction of its wall time.

## Testing

`api/tests/fakes.py` gains **two** fakes, and putting the awkward semantics in
the right one is the whole point:

- `fake_get_many(store, keys)` — stands in for the **scoped** callable that
  `app.py` threads into handlers. It returns the *normalised* contract:
  `{key: value_or_None}` for every requested key. Handler tests (Stages 1–6) use
  this. Reproducing the hostile semantics here would be wrong: handlers never
  see them.
- `fake_raw_get_many(store, physical_keys)` — stands in for
  `wasi_batch.get_many` itself, used **only** by `api/tests/test_kvbatch.py` to
  exercise `scoped_get_many`. It **must** reproduce the hostile semantics:
  missing keys **omitted**, and the returned list in a deliberately different
  order (reversed — deterministic and obviously not the requested order).
  Variants alongside it: one returning `(key, None)` pairs for misses (the
  WIT-documented form), and one that raises (the fallback path).

New/extended tests in `api/tests/test_kvbatch.py`:

- every requested key appears in the result, with `None` for a miss, under
  **both** the omitting and the `(key, None)` raw fakes;
- values are associated by key, not position — pinned with a raw fake that
  reverses order and values that would be visibly swapped if zipped;
- a `TypeError` when handed the raw physical store instead of a `PrefixedStore`
  (mirrors the `scoped_list_keys` guard in `api/tests/test_kvprefix.py`);
- a raw fake that returns a key from another namespace has it **dropped**, not
  surfaced;
- chunking: `MAX_KEYS_PER_GET_MANY + 1` keys produce exactly two raw calls, each
  within the cap, and the merged result is complete;
- duplicate keys in the request are de-duplicated on the wire and both resolve;
- the raising fake produces a complete, correct result via the fallback, and the
  collector records exactly one `get_many_error`;
- the collector records `num_keys` equal to the chunk size, and
  `render_log_line` emits `kv_keys` only when it differs from `kv_ops`
  (`api/tests/test_obs.py`).

## Trade-offs and rejected alternatives

1. **`set_many` / `delete_many` — rejected outright, with arithmetic.** The same
   interface exposes them and the symmetry is tempting, especially for
   `backup.handle_restore` (up to 5,000 sequential writes) and the orphan
   purge's 250 sequential deletes. Rejected because writes are **cap-bound, not
   latency-bound**: 50 write RPS app-wide against reads' 1,000, so a batch of K
   writes either still consumes K against the cap (in which case it queues, and
   the only thing it changes is that the queueing is invisible) or it does not,
   in which case one handler could silently blow through the write budget the
   whole service shares. Neither is a good outcome, and the WIT explicitly
   disclaims atomicity ("does not guarantee atomicity ... some key-value pairs
   could be set while others might fail"), which would turn today's clean
   "records first, indexes last" interruption story into a partially-applied
   batch with no way to know what landed. CLAUDE.md's asymmetry rule
   ("**Never gather writes**") is the whole safety argument for every read-side
   change in this area; extending batching to writes would dissolve it. Not a
   task. Revisit only if Akamai documents batch write accounting *and* the
   ordering guarantees each write-ordering rule in this repo depends on.
2. **Do nothing.** Live: `gather_reads` already delivers 2.7–50.3× overlap, and
   every path is inside the 30 s handler limit today. Rejected because overlap
   does not reduce *read count*, and read count is what the measured 613%-of-
   budget dashboard load consumes; because two handlers (`bulk.py`) have no
   mitigation at all; and because `get_many(100)` measured cheaper than
   `gather_reads(50)` in wall time as well. The cost of adopting is one seam,
   one constant and a fake.
3. **A drop-in replacement of `gather_reads` at the same call signature.**
   Attractive: no caller changes at all. Rejected because `get_many` cannot
   preserve order and omits missing keys, so a positionally-compatible wrapper
   would have to re-sort into request order — which is exactly the silent
   mis-association risk, one refactor away from returning. A dict-returning
   helper makes the hazard structurally unreachable.
4. **Put the batched read on `PrefixedStore` as a `get_many` method.**
   Attractive: callers already hold a view, no extra parameter to thread.
   Rejected because the view is constructed from the *Spin* store handle and has
   no access to the wasi bucket, so the method would need a second handle
   plumbed into every view whether or not it batches; and because the view's
   minimal four-method surface is a documented security property (it has no
   `get_keys` precisely so that misuse is an `AttributeError`). A factory taking
   a raw callable, mirroring `kvprefix.scoped_list_keys`, keeps that surface
   untouched.
5. **Open the wasi bucket eagerly in `_dispatch`, next to `key_value.open`.**
   Attractive: simpler, one place, no closure. Rejected because it makes every
   request — including every write path and every 401 — depend on a brand-new
   KV interface for no benefit, and ~154 µs × every request is a real if small
   tax. Lazy confines both the cost and the blast radius to the paths that batch.
6. **No fallback: let `get_many` failures 500.** Attractive: failures are loud,
   and a silent fallback can hide a deployment where batching never works.
   Rejected because this is the first use of a second KV interface in this app's
   history and a raise would turn a working dashboard into an error page. The
   compromise is a fallback that is **not silent**: `get_many_error` in the
   trace, and the absence of `get_many` where it should be, both name the
   problem. The first deployed trace after Stage 1 is an explicit verification
   step for exactly this.
7. **Skip the spikes and ship the seam on the local measurement alone.**
   Attractive: local timings already show 2.4×, and the deployed run already
   showed ~90× per-key. Rejected because both unknowns change what gets built:
   a low K_max turns "one round trip" into "a sequential loop of round trips"
   and materially changes `click-totals`' profile, and K-per-read billing means
   this is a latency fix that must not be described as a cap fix. Building
   first and discovering second is how the "the answer is no" conclusion got
   published and retracted inside one day.
8. **Wait for the cached totals blob instead** (`docs/plans/denormalised-click-total.md`
   option 3, under Future work). Attractive: 2 reads on a cache hit, no
   enumeration, the best read profile of any option considered. Rejected as an
   *alternative* — they are complementary, not competing. Batching costs no new
   key type, no staleness, no `parse_analytics_key` change and no new failure
   mode, and it helps five other call sites the blob does nothing for. **But see
   Unknown 2:** if `get_many` bills as K, the blob remains the only answer to
   the read cap and its priority is unchanged.

**Effect on the one planned-but-unbuilt item.** The cached totals blob's two
triggers are "the `get_many` spike failing or coming back billed per-key" and "a
traced `click-totals` `get` count above ~500". This work resolves the first
either way (it is answered, not pending) and **breaks the second unless
`kv_keys` lands with it** — which is why the `obs.py` change is a required part
of this plan and not a nicety. Under a 1-read answer, the blob drops to Future
work justified only by the enumeration term; under a K-read answer, its priority
is unchanged and it should be picked up next.

## Tasks

Appended verbatim to `TASKS.md` under `## Batch KV reads (wasi:keyvalue/batch get_many)`:

```
- [ ] Spike A — find get_many's maximum K and any response-size ceiling on the deployed app — file(s): (none — spike, reverted after) — done when: a token-gated spike endpoint deployed to Akamai has measured get_many at K = 100, 500, 1000, 2000, 5000 and 10000 over seeded analytics:count:zzspike<N>:0 keys (recording pairs returned, wall time, and any raised error at each K), a separate arm has measured 100 KB values at K = 10, 50, 100 and 200 to find any byte ceiling, it is recorded whether a module-scope `try: from spin_sdk.wit.imports import wasi_keyvalue_batch_0_2_0_draft2 / except ImportError` is still followed by componentize-py's bundler, the seeded keys have been removed via POST /api/admin/analytics/purge and the orphan report is back to its pre-spike count, the spike endpoint is reverted with `grep -c spike api/app.py` returning 0, and the outcome is recorded in TASKS.md as branch A, B, C or D per docs/plans/batch-kv-reads.md's Unknown 1 table
- [ ] Spike B — answer whether Akamai bills a get_many of K keys as 1 read or K — file(s): (none — spike/inquiry) — done when: either a written answer from Akamai support is recorded verbatim in TASKS.md, or a positive control (one request issuing ~2,000 single gets via gather_reads) has established what exceeding the 1,000 read RPS cap looks like and a test arm (one request issuing 40 x get_many(100)) has been compared against it — and if the positive control shows no observable signature, that is recorded as "the measurement route is closed" rather than as a negative result; the outcome is recorded in TASKS.md as branch "1 read" or "K reads" per docs/plans/batch-kv-reads.md's Unknown 2 table, together with what it means for the cached-totals-blob Future-work entry
- [ ] Land the kvbatch.scoped_get_many seam with no call sites (blocked on Spike A for MAX_KEYS_PER_GET_MANY's value; stop and re-plan if Spike A returned branch D) — file(s): api/kvbatch.py, api/tests/fakes.py, api/tests/test_kvbatch.py — done when: kvbatch.scoped_get_many(raw_get_many, collector=None) returns an async callable taking a PrefixedStore plus logical keys and returning dict[str, bytes | None] containing every requested key, it raises TypeError for a non-PrefixedStore, it de-duplicates, it chunks at MAX_KEYS_PER_GET_MANY sequentially, it drops any returned key not carrying the view's prefix, it treats an omitted pair and a (key, None) pair identically, it falls back to gather_reads for a chunk whose raw call raises, tests/fakes.py carries both fake_get_many (normalised, for handler tests) and fake_raw_get_many (omitting AND order-reversing, for seam tests) plus raising and (key, None) variants, gather_reads is unchanged and its docstring says which helper to use when, and cd api && uv run pytest passes with the new tests failing against a positional-zip implementation
- [ ] Teach obs.Collector to carry a key count and emit kv_keys — file(s): api/obs.py, api/tests/test_obs.py — done when: Collector.record takes a fifth keyword-defaulted num_keys=1 parameter and still has no parameter that could accept a key, _stats entries carry a total_keys slot, totals() returns (ops, us, bytes, keys), _KV_OP_ORDER gains "get_many" and "get_many_error" after "get", render_log_line emits kv_keys immediately after kv_bytes only when the key total differs from the op total (so every existing non-batching line is byte-identical), render_server_timing's desc="N ops" is unchanged, redirect/linkgate/obs.go is NOT changed, and cd api && uv run pytest passes
- [ ] Wire the wasi bucket in app.py and adopt get_many in handle_click_totals — file(s): api/app.py, api/analytics.py, api/tests/test_click_totals.py — done when: app.py carries the wasi_keyvalue_batch/wasi_keyvalue_store imports at MODULE scope, _make_raw_get_many(collector) opens the bucket lazily at most once per request and never at module level and records the open into the collector, _dispatch builds get_many = kvbatch.scoped_get_many(...) beside the existing list_keys line, handle_click_totals takes get_many as a REQUIRED parameter with no default and uses it instead of gather_reads, its docstring records that the read count is now one host call per chunk, and cd api && uv run pytest passes
- [ ] Adopt get_many in handle_analytics, removing the last bare asyncio.gather — file(s): api/analytics.py, api/tests/test_analytics.py — done when: handle_analytics fetches its 65 count keys and 30 event keys through get_many keyed by name rather than by the fetched[:len(count_keys)] slice, `import asyncio` is gone from analytics.py if nothing else needs it, the endpoint's JSON response is byte-identical for a link with clicks and events, events still sort newest-first with unix_ms stripped, and cd api && uv run pytest passes
- [ ] Adopt get_many in links.handle_list — file(s): api/links.py, api/app.py, api/tests/test_links.py — done when: handle_list fetches slug:<slug> bytes through get_many and json.loads them while iterating the slug index (so the response stays in index order), a slug present in the index with no record is still skipped, a corrupt record still raises exactly as it does today rather than being silently dropped, and cd api && uv run pytest passes
- [ ] Adopt get_many in both bulk handlers' pre-write reads — file(s): api/bulk.py, api/app.py, api/tests/test_bulk.py — done when: handle_bulk_action's per-slug links.get_link loop and handle_bulk_create's per-row store.exists loop are each replaced by a single get_many over the slug: keys (existence tested as `is not None`), all-or-nothing validation semantics and every row_error code are unchanged, the record-then-index write ordering is untouched, no write is gathered or batched anywhere, and cd api && uv run pytest passes with the existing bulk tests unmodified except for the new parameter
- [ ] Adopt get_many in consistency.collect — file(s): api/consistency.py, api/app.py, api/tests/test_consistency.py — done when: both dict(zip(keys, await gather_reads(...))) sites are replaced by get_many, the users-side two-shape allowlist is unchanged and test_collect_never_even_reads_a_user_record_value still passes, users_values[key] indexing still raises KeyError for a key outside the allowlist, a report over a store holding a real PBKDF2 hash still contains neither password_hash nor pbkdf2_sha256, and cd api && uv run pytest passes
- [ ] Adopt get_many in backup.handle_export, or record why it did not (gated on Spike A's size arm) — file(s): api/backup.py, api/app.py, api/tests/test_backup.py — done when: either handle_export reads through get_many with a chunk size justified by Spike A's measured byte ceiling, or the decision to leave it on gather_reads is recorded in TASKS.md with the measured ceiling that drove it; in the adopting case a full export round-trips byte-identically to a gather_reads export of the same store, the restore loop is still a sequential for-loop with no batching, and cd api && uv run pytest passes
- [ ] Deploy and trace one batched dashboard load on Akamai — file(s): (none — measurement) — done when: a build carrying at least Stage 1 is deployed with app_version and log_debug_token set, X-SS-Version confirms the new build is live, an X-SS-Debug trace of GET /api/analytics/click-totals shows a get_many op with kv_keys equal to the count key total and NO get_many_error, the wall time is compared against the pre-change trace in TASKS.md's 2026-08-11 table, and the numbers are recorded in TASKS.md
- [ ] Record the batch-read mechanism and its monitoring number in CLAUDE.md — file(s): CLAUDE.md — done when: the "Parallel KV reads" section states that gather_reads and kvbatch.scoped_get_many both exist and which to use when, that get_many is one host call for K keys with the measured flat profile, that its bindings are synchronous so chunks cannot overlap, that missing keys are omitted and order is not preserved and the seam normalises both, that the spin.toml key_value_stores allowlist still applies to a wasi-opened bucket, that writes are still never batched and why, that api/obs.py now diverges from redirect/linkgate/obs.go by design, and that the monitoring number for the cached-totals-blob trigger is now kv_keys rather than the traced get count; and the Akamai deployment section records Spike B's quota-accounting answer
- [ ] End-to-end manual verification of batched KV reads — file(s): (none — verification step) — done when: against a real `SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false spin up --build --runtime-config-file runtime-config.toml`, the dashboard loads with correct Clicks totals for a clicked link and 0 for an unclicked one, a link detail page shows the same total and recent events as before, a bulk create of 3 links then a bulk disable then a bulk tag all succeed with correct row errors on a duplicate slug, GET /api/admin/consistency reports ok on a clean store, GET /api/admin/backup downloads and POST /api/admin/restore restores it, and the server log shows no get_many_error on any request
```

## Critical files

- `api/kvbatch.py`
- `api/obs.py`
- `api/app.py`
- `api/analytics.py`
- `api/links.py`
- `api/bulk.py`
- `api/consistency.py`
- `api/backup.py`
- `api/tests/fakes.py`
- `api/tests/test_kvbatch.py`
- `api/tests/test_obs.py`
- `api/tests/test_click_totals.py`
- `api/tests/test_analytics.py`
- `api/tests/test_links.py`
- `api/tests/test_bulk.py`
- `api/tests/test_consistency.py`
- `api/tests/test_backup.py`
- `CLAUDE.md`
- `docs/plans/batch-kv-reads.md` (new)
- `docs/plans/batch-kv-reads-scratch.md` (new, gitignored)

Not touched, deliberately: `redirect/` (one key per click), `gui/`,
`gui-pages/`, `spin.toml` (no new route, no new store grant — the existing
`key_value_stores = ["default"]` already covers the wasi bucket, confirmed
2026-08-15), `Jenkinsfile` (no change to how tests are invoked; `uv run pytest`
picks up the new tests automatically).

## Verification

1. Baseline, before anything: `cd api && uv run pytest` → 572 passed (recorded
   2026-08-15).
2. After each stage: `cd api && uv run pytest`.
3. `cd gui-pages && uv run pytest` — unchanged by this work, but
   `test_manifest_components.py` is the guard that `spin.toml` still declares
   exactly four components after any spike work, so run it once at the end.
4. `cd redirect && go test ./linkgate/...` — unchanged; run once to confirm
   nothing drifted. **Never `go test ./...`, `go build ./...` or `go vet ./...`;
   they fail by design on `package main`.**
5. Mutation check on the seam, run manually and then discarded: change
   `scoped_get_many` to zip results positionally instead of keying by name, and
   confirm the order-association test in `test_kvbatch.py` fails. A test that
   passes against a positional implementation has not pinned the property.
6. Full local app:
   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```
   Then, in a browser: sign in, load the dashboard (Clicks column correct — a
   clicked link shows its real total, a new link shows `0` not an em-dash),
   open a link's detail page (total and recent events unchanged), run a bulk
   create of 3 rows plus one duplicate slug (all-or-nothing rejection with the
   duplicate named), a bulk disable, and a bulk tag. Then
   `GET /api/admin/consistency` (all twelve checks at `count: 0` on a clean
   store), `GET /api/admin/backup`, and `POST /api/admin/restore` of that file.
   **Pass means: no behaviour differs from before, and the server log carries no
   `get_many_error` on any request.**
7. Deployed trace (Stage 1 onward):
   ```bash
   curl -sI https://<app>.fwf.app/ | grep -i x-ss-version      # confirm the build is live
   curl -s -H "X-SS-Debug: <token>" -b <cookies> \
     https://<app>.fwf.app/api/analytics/click-totals -D - -o /dev/null
   ```
   Pass means: `Server-Timing` present, the logfmt line shows `get_many=<n>/<µs>`
   with `kv_keys` > `kv_ops`, **no `get_many_error`**, and wall time at or below
   the 2026-08-11 baseline for a comparable store. Remember the deploy CLI's
   60-second readiness timeout is not a failure — check `X-SS-Version`, do not
   redeploy.

## Out of scope / follow-ups

- **`set_many` / `delete_many`.** Rejected outright above; recorded under
  `TASKS.md`'s `## Considered and rejected`.
- **`redirect`.** One key per click; batching buys nothing. Untouched.
- **The `list_keys` enumeration.** `get_many` does nothing for it, and
  `wasi_keyvalue_store.Bucket.list_keys(cursor)` still takes no prefix, so
  CLAUDE.md's "avoid the enumeration, do not narrow it" rule is unchanged. This
  remains the dominant residual cost of `handle_click_totals` after batching.
- **The cached totals blob** (`docs/plans/denormalised-click-total.md` option 3).
  Stays under `TASKS.md`'s Future work. Its priority is decided by Spike B:
  demoted to "enumeration term only" under a 1-read answer, unchanged under a
  K-read answer.
- **`analyticsorphans`' liveness pre-check.** Not adopted; the arithmetic is
  above. Trigger for revisiting: a traced purge showing the pre-check is a
  material fraction of wall time.
- **`GET /api/links` pagination.** Still unaddressed and still the real answer
  for large deployments; batching converts N reads into one host call but does
  not bound the response body. Already under Future work
  (`docs/plans/links-pagination.md`).
- **Removing `gather_reads`.** Not proposed. It remains the fallback path, the
  only way to fan out `exists` probes, and the right tool for heterogeneous
  coroutines.
