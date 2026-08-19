# Observable KV Failures

## Context

Three open items in `TASKS.md`'s "What to pick up next" are blocked on the same
missing thing: **when a KV operation fails on Akamai, nothing anywhere in this
application records what the host said.**

1. **Reproduce a KV read failure on Akamai.** `docs/plans/redirect-read-failure-not-404.md`
   shipped a `503` arm for `DispositionUnavailable` (deployed 2026-08-19 as
   `cb4793d-resolve503`). Measured: zero wrongly-404 across ~9,200 requests and
   **zero 503s** — the arm has never run on Akamai, and the read-cap explanation
   for the original failures is in doubt (2,195–2,628 implied reads/s ran clean
   against a documented 1,000/s cap). See `TASKS.md`'s
   `### DEPLOYED AND MEASURED (2026-08-19)`.
2. **`write_error` reports `other`, not `throttled`** (`TASKS.md` Future work,
   2026-08-17). `api/kvretry.py`'s `classify_write_error` substring-matches
   `"too many requests"` purely to label the failure, and **nothing anywhere
   logs `str(exc)`**, so Akamai's real write-failure message is unknown because
   no code path prints it.
3. **A single-link DELETE returned `200` without deleting**, seen once
   2026-08-18, not reproduced in 10 attempts. The filed entry says the
   diagnosis needs "a loop that records the DELETE's status code AND an
   `X-SS-Debug` trace" — i.e. a harness, which does not exist.

This plan builds the diagnostic capability, and only that. It fixes nothing the
diagnostics might reveal.

**The tension it has to resolve** is CLAUDE.md's "Toggleable structured
logging" doctrine: `log_level` must stay `off` in production (~280 MB/day at
the sustained ceiling), and **the collector structurally cannot log a KV key**
because `users:session:<token>` is a live session credential and `spin aka logs`
retains 7 days by default. An error string may embed the key that failed.
Nobody has checked. So the naive fix — log `str(exc)` — could reintroduce
exactly the hazard that design decision exists to prevent, on the rare path
where it would go unnoticed.

**Confirmed decisions (settled by the user before planning):**

- Diagnostics only. No fix for anything they reveal.
- No deploy is planned here; the plan specifies exactly what a verification run
  would do once the user chooses to deploy.
- `log_level=summary` is **not** turned on as the shipped default.
- The new `TASKS.md` section goes **above** the `# START HERE — session
  handoff, 2026-08-18` block, which stays last.
- Evaluate, and argue, "log only failures, unconditionally, rather than gating
  on `log_level`". (Answer below: **yes for `api`, yes-but-bounded-differently
  for `redirect`, and the volume argument does not hold in the form it was
  proposed** — see Trade-offs #1.)

## Key technical facts confirmed during research

**The Python error shape, and a correction to how it has been reasoned about.**
`componentize_py_types.Err` is a *frozen dataclass that subclasses Exception*
(found on this machine at
`/Users/jhostetler/.claude/jobs/7b52984b/tmp/bind/componentize_py_types.py`);
`Error_Other` is a dataclass with a single `value: str`
(`api/.venv/lib/python3.14/site-packages/spin_sdk/wit/imports/spin_key_value_key_value_3_0_0.py`,
lines 16–36). Executing a faithful reconstruction of both:

```
str  : "Error_Other(value='too many requests')"
repr : "Err(value=Error_Other(value='too many requests'))"
args : (Error_Other(value='too many requests'),)
```

Two consequences. First, `str()` does surface the message, so
`api/tests/fakes.py`'s `KvThrottleError` stand-in is accurate and
`classify_write_error` would fire on that message — confirming the 2026-08-17
note. Second, and new: **`exc.value` is the `Error_*` instance, so
`type(exc.value).__name__` yields `Error_Other` / `Error_AccessDenied` /
`Error_StoreTableFull` / `Error_NoSuchStore` with no import at all.** The
2026-08-17 reasoning ("a pure module cannot import the WIT error types" — true)
does not imply a pure module cannot *identify the variant*. Duck-typing an
attribute is not importing a type. **This gives us a wording-independent
signal that a vendor copy-edit cannot break.**

**The Go error shape is even better: the SDK flattens the variant into a plain
string and passes the host's message through verbatim.**
`$GOMODCACHE/github.com/spinframework/spin-go-sdk/v3@v3.0.0/kv/kv.go`, lines
111–122:

```go
func errorVariantToError(code keyvalue.Error) error {
	switch {
	case keyvalue.ErrorAccessDenied:    return fmt.Errorf("access denied")
	case keyvalue.ErrorNoSuchStore:     return fmt.Errorf("no such store")
	case keyvalue.ErrorStoreTableFull:  return fmt.Errorf("store table full")
	case keyvalue.ErrorOther:           return fmt.Errorf("%v", code.Other())
	}
	return fmt.Errorf("no error provided by host implementation")
}
```

So in Go, `err.Error()` for an `Other` is **exactly** what Akamai said, with no
`Error_Other(value=…)` wrapper, and the other three variants are identifiable
by their fixed English strings. `redirect` is therefore the *cleanest* place to
read a raw host message — it just happens only to read, never to write.

**Two real Akamai KV error messages are already on record, and neither embeds a
key.** From `TASKS.md`'s `### BOTH SPIKES ANSWERED (2026-08-15)`: a read
throttle produced `Error_Other('too many requests')` (the positive control: 10
parallel requests × 200 gathered single reads, 9/10 throttled), and
`get_many` at K=10,000 produced `Error_Other('key-value error: internal server
error')`. Both key-free, both read-path. **The write-failure message remains
unknown.**

**Nothing logs any exception string today — verified by reading every path:**

- `api/app.py:210` — `except Exception:` sets `err = True`, returns
  `{"error": "internal_error"}`, and the log line carries `err=1` and nothing
  else. The exception object is discarded on the spot.
- `api/kvretry.py:78` — `classify_write_error` returns only `"throttled"` /
  `"other"`. `WriteFailed` *carries* `.cause`, and every caller
  (`bulk.py:317,472`, `consistencyrepair.py:267,278`,
  `analyticsorphans.py:393`, `backup.py:324`) uses it only to compute that
  two-valued label.
- `api/kvprefix.py:49–75` — `PrefixedStore` starts its timer, awaits, and
  records into the collector **after** the await. A failed operation therefore
  records *nothing at all*, so a failed KV op is invisible even in a traced
  line except via `kvretry`'s `write_retry`/`write_failed` markers.
- `api/kvbatch.py:133` — `scoped_get_many` **swallows** a raw `get_many`
  exception, records `get_many_error` into the collector and falls back to
  `gather_reads`. With tracing off, that failure is entirely invisible.
- `redirect/linkgate/resolve.go:104` — `if err != nil { return Link{},
  DispositionUnavailable }`. The error is discarded.
- `redirect/main.go:464–467` — `raw, _ := store.Get(countKey)` and
  `_ = store.Set(countKey, updated)`. Deliberately swallowed (best-effort
  analytics).

**stderr lines are retrievable from the deployed app.** `TASKS.md`'s
`### STAGE 2 DEPLOYED AND TRACED (2026-08-18)` records write counts "traced
with `X-SS-Debug`, read back from `spin aka logs`", and
`dev/bulk-concurrent.sh`'s header carries the exact command. Default retention
is 7 days (CLAUDE.md).

**`Error_StoreTableFull` is a named, plausible, unconfirmed candidate for the
`other` label.** Its own SDK docstring says it is raised "if too many stores
have been opened simultaneously". `api/app.py` opens one `spin_key_value` store
per request plus, lazily, one `wasi_keyvalue_store` bucket
(`_make_raw_get_many`); `redirect` opens two per request
(`handleRedirectGet` + `sendRedirectThenRecord`). Neither closes them. A
concurrent burst is exactly the condition under which the 2026-08-17
`write_error: other` was observed. **UNCONFIRMED** — but the `etype` field this
plan adds would settle it in one line, with no wording dependency.

**UNCONFIRMED: whether any Akamai KV error message ever embeds the key.** Two
data points say no; both are read-path. What would confirm it: the
`msg_redacted=1` field this plan adds appearing in a captured line (see "The
key-in-message question", below).

**UNCONFIRMED, and worth stating because it reframes item 1: the read cap may
bind on burst shape rather than average rate.** 2,628 implied reads/s spread
across ~1,300 requests of 2 reads each ran clean (2026-08-19), while 2,000
reads/s issued as 10 requests × 200-way `gather_reads` fan-outs was throttled
9/10 (2026-08-15). Same order of aggregate rate, opposite outcome. If burst
shape is what the host sees, `redirect` (2 sequential reads per request) may be
structurally incapable of provoking the throttle by itself, and the provocation
has to come from the `api` side. That hypothesis is what the item-4 experiment
below is built on. It is a hypothesis.

**Baseline, measured today (2026-08-19) before any change:**
`cd redirect && go test ./linkgate/...` → ok;
`cd api && uv run pytest` → **648 passed**;
`cd gui-pages && uv run pytest` → **71 passed**.

## The design: unconditional failure lines, sanitized by construction

One new line kind, emitted to stderr **regardless of `log_level` and with no
`X-SS-Debug` token**, on failure only. It is a *separate line*, never a field
on the existing per-request summary line, because that line does not exist
unless tracing is already on — which is the whole point of the change.

```
ss comp=api ev=kv_fail route=/api/links/bulk method=POST op=set ns=links op_us=24310 etype=Err/Error_Other msg=Error_Other(value='too many requests')
ss comp=api ev=exc route=/api/links/{slug} method=DELETE etype=Err/Error_AccessDenied at=kvprefix.py:66 msg=Error_AccessDenied()
ss comp=redirect ev=kv_fail route=/r/{slug} slug=M7RyJVC op=get ns=links etype=other msg=too many requests
```

Field rules, all load-bearing:

- **`comp` first, `ev` second**, so `grep 'ss comp=api ev='` and
  `grep 'ev=kv_fail'` both work and neither collides with the existing summary
  lines (which have no `ev` field at all and are byte-identical after this
  change).
- **`msg` is ALWAYS the final field, and nothing may ever be appended after
  it.** The message is unquoted and may contain spaces; that is a deliberate
  legibility choice over logfmt purity (nothing in this repo parses these lines
  — `TASKS.md`, "JSON log lines instead of logfmt", 2026-08-06). Any flag about
  the message (`msg_truncated=1`, `msg_redacted=1`) goes **before** it.
- **`route` is always a route template**, never a raw path — `api` uses
  `obs.route_template`, `redirect` hardcodes `/r/{slug}`. A raw path carries a
  slug or a username.
- **`op`/`ns` carry the operation and namespace, never a key.** This is the
  same structural move the collector already makes: the reporter's signature
  has no parameter that could accept a key.
- **`etype`** is the WIT variant, wording-independent: `Err/Error_Other` in
  Python (`type(exc).__name__ + "/" + type(exc.value).__name__` when the inner
  type name starts with `Error_`, else the bare class name), and in Go one of
  `access_denied` / `no_such_store` / `store_table_full` / `no_error_provided`
  / `other`, matched against the five fixed strings `errorVariantToError`
  produces.
- **No `dur_us` on the redirect line.** Timing the failed `Get` would require
  wrapping the store on the off path, which CLAUDE.md forbids. `api` gets
  `op_us` for free because `PrefixedStore` already starts its timer before the
  collector check.

### The key-in-message question, and how it is answered without ever risking a key

**The message is sanitized before it is ever rendered, so the answer is never
load-bearing.** And the sanitizer's own output *is* the answer: a
`msg_redacted=1` field means the host echoed something key-shaped. **`grep
'msg_redacted=1'` is therefore the one-command answer to "does Akamai's error
string embed the key?", obtainable without a single key ever reaching the log.**

The sanitizer, in both languages, in this order:

1. **Redact key-shaped substrings.** Replace every match of
   `[A-Za-z][A-Za-z0-9_-]*:[^\s'")\]]+` with `[key:<leading-word>]`. So
   `links:slug:promo` → `[key:links]`, and
   `Error_Other(value='users:session:9f8a7b6c')` →
   `Error_Other(value='[key:users]')`. The trailing-character class is what
   keeps `key-value error: internal server error` intact — a colon followed by
   whitespace does not match.
   **This is complete for keys, because of an existing invariant, not by luck:**
   every physical key this app sends is prefixed (`api/kvprefix.py`'s
   `STORE_PREFIXES`; `redirect/linkgate/keys.go`'s `LinkKey`/`CountShardKey`),
   so a host that echoes a key echoes a prefixed one. The rule is written
   generically rather than against the three known prefixes so that a future
   namespace, and `users:` on the Go side, are covered without `keys.go`
   gaining a `users:` constant it deliberately does not have.
2. **Redact hash material.** Replace any whitespace-delimited token containing
   `pbkdf2_sha256` with `[hash]`. A link record's value carries a link password
   hash (`auth.hash_password` → `pbkdf2_sha256$100000$…`), and a `set` failure
   is the one place a *value* could plausibly be echoed. Three lines, and it is
   the difference between safe-by-construction and safe-by-argument.
3. **Replace control characters and newlines** with `_`, so one failure is
   always one line.
4. **Truncate to `MAX_ERROR_MESSAGE_CHARS = 200`**, setting `msg_truncated=1`.
   The two known Akamai messages are 17 and 40 characters, so 200 is 5×
   headroom while bounding an unbounded host string. Raising it needs a real
   observed truncation, per this repo's standing rule for every sibling
   constant.
5. Empty result renders `msg=-`.

If a raw, unredacted message is ever genuinely needed, the escape hatch is a
**token-gated echo in a response body, never a log** — one request, one
operator, no 7-day retention. That is Stage C below, and it follows the
`spike-kv-DO-NOT-KEEP` precedent (deployed, measured, reverted, 2026-08-15).

### Volume: bounded per request in `api`, per instance in `redirect`

The proposed reasoning — "a KV failure is rare by definition, so an
unconditional failure log has negligible volume" — **does not hold as stated,
and the asymmetry it hides is what shapes the design.**

- In `api`, a single throttled 50-row bulk create can fail up to 50 writes × 3
  attempts = 150 times *in one request*. "Rare" is wrong per-request.
  **Bound: the reporter deduplicates on `(op, ns, etype, msg)` and emits each
  distinct tuple once per request, capped at
  `MAX_FAILURE_LINES_PER_REQUEST = 3` distinct tuples.** A throttle storm
  produces 150 identical messages and we want the message once. Counts are
  already available from the traced line's `write_retry`/`write_failed` and
  from the response's `partial`/`not_created`. **Result: ≤ 3 lines per api
  request, unconditionally, with no dependency on instance lifetime.**
- In `redirect`, a read failure is ≤ 1 per request by construction, but the
  request rate is 1,000+/s. At the pre-fix incident's 43% failure rate at 1,292
  rps that is ~555 lines/s ≈ 67 KB/s. **A per-instance line cap cannot be
  trusted, because instance count is not bounded** — `main.go`'s own
  `clickEntropy` comment records that "Akamai created one instance per request"
  is plausible and unconfirmed, and a per-instance cap of N under that regime
  is N lines *per request*. **Bound: deduplicate on `(op, msg)` per Wasm
  instance, stopping after 32 distinct pairs.** Novelty is what a diagnostic
  needs; frequency is already visible as the 503 count in `hey`'s status
  distribution and in Akamai's own logs. Under a one-instance-per-request
  regime this degrades to one line per *distinct message* per request, which is
  the honest worst case and still two orders of magnitude below the 280 MB/day
  figure that motivates `log_level=off`.

**Both bounds are stated as safety rails, not policy** — the same framing
`MAX_INLINE_PURGE_KEYS` carries. Raising either needs a real captured run.

## API changes

**`api/obs.py`** (pure, host-testable, zero `spin_sdk` imports — unchanged
property) gains:

```python
MAX_ERROR_MESSAGE_CHARS = 200
MAX_FAILURE_LINES_PER_REQUEST = 3

def sanitize_error_message(text: str) -> tuple[str, bool, bool]:
    """Returns (sanitized, redacted, truncated) — see the plan's sanitizer rules."""

def error_type_name(exc: BaseException) -> str:
    """"Err/Error_Other" for a WIT-shaped error, the bare class name otherwise.
    Duck-typed via getattr(exc, "value", None); imports nothing."""

def exc_location(exc: BaseException) -> str:
    """"<basename>:<lineno>" of the INNERMOST traceback frame. Never source text,
    never a value — a location, so a 500 is diagnosable without a traceback."""

def render_failure_line(fields: list[tuple[str, str]]) -> str:
    """"ss " + fields in order, nothing appended. Separate from render_log_line
    precisely so nothing can ever land after msg."""

def make_failure_reporter(emit, *, comp: str, route: str, method: str | None = None,
                          max_distinct: int = MAX_FAILURE_LINES_PER_REQUEST):
    """Returns report(ev, op, namespace, duration_ns, exc, extra=None) -> None,
    closing over ONE dedup set for the lifetime of this reporter (i.e. one
    request — never module-level, for the same reason obs.Collector never is)."""
```

`render_log_line`, `Collector`, `_KV_OP_ORDER`, `route_template`,
`render_server_timing`, `parse_log_level` and `token_matches` are **untouched**.
No new collector op type; `kv_ops` keeps meaning host operations and every
existing traced line stays byte-identical.

**`api/kvprefix.py`** — `PrefixedStore.__init__(raw, prefix, collector=None,
on_error=None)`, `__slots__` gains `"on_error"`, and `open_views(physical_store,
collector=None, on_error=None)` threads it. Each of `get`/`set`/`delete`/
`exists` wraps its await in `try/except BaseException`, calls
`on_error(op, namespace, duration_ns, exc)` when one is set, and **re-raises
unchanged**. Nothing is recorded into the collector on failure — deliberately,
so traces do not shift under a change whose entire purpose is diagnosis.

**`api/kvbatch.py`** — `scoped_get_many(raw_get_many, collector=None,
on_error=None)` reports the raw `get_many` exception it already catches, *before*
falling back to `gather_reads`. This is the blind spot with the most known
failure modes (K≥10,000 → `internal server error`; batch throttling) and today
it is invisible whenever tracing is off.

**`api/app.py`** — `handle_request` builds **one** reporter per request (before
dispatch, so route/method are known once and one dedup budget covers the whole
request) and passes it into `_dispatch`, which hands a narrowed
`on_error(op, ns, dur_ns, exc)` closure to `open_views` and `scoped_get_many`.
The `except Exception` catch-all calls the same reporter with `ev="exc"`,
`extra=[("at", obs.exc_location(exc))]`, before returning its unchanged 500
body. `emit` is `lambda line: print(line, file=sys.stderr)`.

**`api/kvretry.py` gets no `on_failure` hook, deliberately.** It does not know
the op or the namespace — only a `make_coro` lambda — so a hook there would
produce a strictly worse line than `PrefixedStore`'s, at a second wiring point.
Because `PrefixedStore` reports *every attempt*, a write that is throttled and
then succeeds on retry is now visible too, which a `WriteFailed`-only hook would
have missed entirely.

**`api/tests/fakes.py`** gains `FakeWitErr` — a frozen dataclass subclassing
`Exception` with a `value` field holding an `Error_Other`-shaped dataclass —
reproducing the two-level structure verified above, so `error_type_name` is
tested against the real shape rather than against `KvThrottleError`'s
flat string. The existing `KvThrottleError`/`KvOtherError`/`ThrottlingStore`
stay as they are; ~20 call sites depend on them.

**No response body changes, and no GUI changes.** See Trade-offs #3.

## Redirect (Go) changes

**`redirect/linkgate/obs.go`** gains `SanitizeErrorMessage(msg string) (string,
bool, bool)` and `RenderFailureLine(fields []Field) string`, mirroring the
Python rules. `Collector`, `kvOpOrder`, `TimedStore`, `RenderLogLine`,
`RenderServerTiming`, `ParseLogLevel` and `TokenMatches` are untouched.

**`redirect/linkgate/resolve.go`** — `Resolve` becomes
`func Resolve(store KVStore, slug string, now time.Time) (Link, Disposition, error)`,
with a non-nil error returned **only** alongside `DispositionUnavailable`. Every
disposition decision, and the KV operation count, is unchanged; this is purely
"stop discarding the error". The existing thirteen tests in `resolve_test.go`
keep their assertions and gain a `_`; two new ones pin that the returned error
is exactly what `fakeStore.getErr` produced, and that it is nil for the other
four dispositions.

**`redirect/main.go`** — the two KV-fault arms emit one failure line:

- `openTimedStore` returning an error, in both handlers → `op=open ns=-`.
- `Resolve` returning `DispositionUnavailable` → `op=get ns=links`, with the
  slug (already treated as non-secret, and already logged on the summary line).

A package-scope `sync.Mutex`-guarded `map[string]struct{}` capped at 32 entries
does the per-instance dedup. A map keyed on `(op, msg)` is not per-request
state, so it carries none of the interleaving hazard that makes a package-level
*collector* forbidden — but say so in the comment, because the shape looks
similar.

**Three exclusions, all deliberate:**

- **`DispositionUnreadable` emits nothing.** It is a parse failure, not a KV
  failure: there is no unknown host message to capture, and the condition is
  already diagnosable (500, plus `api/consistency.py` reporting the record as
  `unreadable_value` and `api/links.py`'s `UnreadableLinkError` naming it).
- **`recordClickCount`'s swallowed `Get`/`Set` errors emit nothing.** That is
  the highest-frequency write in the application and its lossiness is
  documented, measured and accepted (mechanism M2). See Trade-offs #2.
- **The success path is untouched**: no new KV operation, no store wrapping when
  no collector is attached, no line. The off path stays byte-identical for every
  request that does not fault.

**`gui-pages` and `gui` are untouched** — `gui-pages` performs no KV work and
`gui` is a prebuilt third-party binary.

**The two sanitizers are deliberately NOT pinned against each other.** Nothing
reads both and compares them, so a divergence cannot fail silently at runtime —
unlike `keys.go`'s prefixes and `CountShards`, which `api/tests/test_kvprefix.py`
pins precisely because they *can*. This follows the precedent CLAUDE.md already
records for `get_many`/`get_many_error` and `write_retry`/`write_failed`: the
`api` and `redirect` observability vocabularies diverge by design.

## Tooling

- **`dev/bulk-concurrent.sh`** (existing) — the write-regime harness for item 2.
  Its `WRITES=$(( N * (R + 2) ))` is stale: the two per-request index writes are
  gone since `docs/plans/derived-link-indexes.md` Stage 2, so the estimate must
  be `N × R`, or a run lands in the wrong regime while the script claims
  otherwise. Its header also still tells the reader to expect `index_updated`
  in the response and to clean up "paced, never concurrently", both of which
  `TASKS.md`'s 2026-08-18 Stage-2 section explicitly obsoleted.
- **`dev/kv-read-pressure.sh`** (new) — N parallel `GET /api/admin/backup`
  requests, defaulting to the **10** that threw
  `Error_Other('too many requests')` on 9 of 10 requests on 2026-08-15. Each is
  a measured ~999-operation `gather_reads` fan-out, which is the *only*
  burst-shaped read load this app can generate on demand. It is both the
  read-message capture for item 2 and the read-pressure generator for item 4.
- **`dev/delete-verify.sh`** (new) — the create → DELETE → re-check loop item 3
  asks for, recording the DELETE's own status code and tracing every request.
  **The trap it must encode:** `TASKS.md`'s M2 section records sub-second
  redirect staleness that self-heals, so a `/r/{slug}` still answering 302
  immediately after a delete is normal. Only a record still present at **+10 s**
  counts as the anomaly.
- **`dev/redirect-load.sh`** (existing, unchanged) — already encodes the `hey`
  traps (`-disable-redirects`, `n = c × k`, non-zero exit on any 404).

## Answering the five deliverables directly

1. **What gets logged, where, at what gating.** One sanitized failure line per
   distinct failure, to stderr, **unconditional** — independent of `log_level`
   and of `X-SS-Debug` — in both `api` (KV op failures via `PrefixedStore` and
   `scoped_get_many`, plus the catch-all exception path) and `redirect` (failed
   `kv.Open` and failed link `Get` only). Bounded by per-request dedup in `api`
   (≤3 lines) and per-instance dedup in `redirect` (≤32 distinct messages).
2. **How the key-in-error-string question is answered safely.** It is not
   answered before shipping and does not need to be: the message is sanitized
   by construction, and the sanitizer emits `msg_redacted=1` when it fires.
   `grep 'msg_redacted=1'` on a real deployment answers the question with no
   key ever reaching the log. Redaction is complete for keys because every key
   this app sends is prefixed. If the answer turns out to be "yes it embeds the
   key" and a raw message is still wanted, the escape hatch is a token-gated
   response echo (Stage C), never a log.
3. **A concrete experiment.** Task 13: baseline the store by measurement, run
   `dev/bulk-concurrent.sh 4 5` (under the 50/s cap — the control, which must
   produce **zero** lines, or an absent line later means nothing), then
   `6 50` (~300 writes, the regime that produced `write_error: other` on
   2026-08-17), with **at least one arm sending no `X-SS-Debug` header at all**
   — that arm is what actually proves "unconditional", and it is the easy thing
   to forget because `bulk-concurrent.sh` always sends the token. Then
   `dev/kv-read-pressure.sh` for the read-side message. Traps encoded: discard
   the first traced sample after idle; verify every mutation's status code;
   poll `X-SS-Version` rather than trusting the CLI's "failed to wait for
   deployment to go live"; return the store to its measured baseline and
   re-verify with `GET /api/admin/consistency` plus the orphan report.
4. **Can anything be done for item 1 beyond detection?** **No mechanism is
   known to provoke `DispositionUnavailable` on Akamai, and this plan does not
   invent one.** The read cap is not a credible lever at redirect's burst shape
   (2,628 implied reads/s ran clean). What the plan does instead is (a) make
   the fault self-reporting with its message the first time it ever happens
   naturally, and (b) offer one hypothesis-driven attempt, derived from the one
   measured throttle signature: run `dev/kv-read-pressure.sh` (burst-shaped
   `api` reads, the shape that *did* throttle) **concurrently** with
   `dev/redirect-load.sh` and watch `/r/` for 503s. If that produces nothing,
   the honest recorded outcome is "the arm remains unexercised; the fix is
   insurance", and task 14's done-when accepts exactly that.
5. **Should the `redirect` read-failure path gain a log field?** **No field —
   a separate line.** `docs/plans/redirect-read-failure-not-404.md`'s
   "Observability changes: none, deliberately" section is being **reopened
   deliberately**, and both of its prohibitions are upheld verbatim: no `err=1`
   / `kv_err=1` field on the summary line (it would duplicate the status code
   and cost the traced path), and nothing recorded into the collector (whose
   signature has no parameter for it). What that section did not consider is a
   line emitted *only from the fault arm*, which costs the success path nothing
   and carries the one thing a status code cannot: the host's own message and
   variant. The status code remains the frequency signal; the line is the
   content signal.

## Trade-offs and rejected alternatives

1. **Gating failure logging on `log_level=summary`** — attractive because it is
   the mechanism that already exists, needs no new bound and no new argument.
   Rejected. It requires one redeploy to arm and a second to disarm, carries
   the "remember to turn it off" hazard, and cannot catch a failure that
   happens next week — which is the entire requirement, since the read fault
   has failed to reproduce on demand twice. **The volume objection that
   justifies `log_level=off` does not transfer**: it is about ~130 bytes on
   *every* request at ~50 rps sustained, while this is ≤3 lines per faulting
   api request and ≤32 distinct messages per redirect instance. The user's
   framing ("failures are rare by definition") is directionally right and
   arithmetically wrong for the redirect write path, which is why the bounds
   above are dedup-based rather than a flat "log every failure".
2. **Logging `redirect`'s swallowed analytics `Get`/`Set` failures** — very
   attractive, because it is the one place in the application that would
   directly capture Akamai's **write**-failure message, in the clean unwrapped
   Go form, with no experiment needed. Rejected. It is the highest-frequency
   write in the app and the one already known to fail routinely above ~50
   clicks/second (mechanism M2, documented, measured, accepted), so it is the
   single worst place to attach an unconditional log. `api`'s bulk path reaches
   the same host with the same message at human-scale request rates. Revisit
   only if the `api` capture comes back empty *and* someone wants a
   counter-only (never a line) signal — filed as Future work.
3. **Putting the sanitized message in the API response body** (a
   `write_error_detail` beside the existing `write_error`) — attractive: no log
   access needed, visible immediately to the operator who ran the bulk
   operation, and outside any 7-day retention window. Rejected. That body is
   rendered in the dashboard for *ordinary* users, not just `users.manage`
   holders, so it would put raw host error text in a user-facing surface and
   disclose backend detail to every authenticated account, for a field only an
   operator can act on. The operator who needs it has `spin aka logs`. The GUI
   stays untouched, which also keeps this change out of `gui/` and its
   restart-required staleness trap entirely.
4. **Logging a full Python traceback** — attractive for item 3 and for any
   future 500. Rejected: multi-line output breaks the one-failure-one-line
   contract, tracebacks embed source text, and the innermost frame's
   `file:lineno` (`obs.exc_location`) delivers most of the diagnostic value in
   one field that provably contains no data.
5. **A post-delete `exists` verification read to catch the ghost DELETE** —
   attractive because it would turn "seen once, unreproducible" into "logged
   whenever it happens". Rejected on evidence: `TASKS.md` (2026-08-15) already
   records Akamai's `exists()` reporting a *deleted* key as present, so a
   positive result cannot distinguish a lost write from documented eventual
   consistency — it would fire on normal state, which is how a checker gets
   ignored (the same reasoning that kept orphan analytics out of the
   consistency check). It also costs a KV read on every delete. The harness
   (`dev/delete-verify.sh`) gets the same evidence with no production cost and
   no ambiguity, and it is what the filed entry actually asked for.
6. **Making the `etype` variant name control flow now** — i.e. retry
   `Error_Other`, refuse to retry `Error_AccessDenied`/`Error_NoSuchStore`.
   Genuinely attractive, and newly *possible* (the duck-typed variant needs no
   import, correcting the 2026-08-17 reading). Rejected here as out of scope:
   this plan is diagnostics only, and the correct sequence is to observe which
   variants actually occur before writing a policy against them. Filed as
   Future work with the trigger. Note the standing rule this must not break:
   the **string** match must never become control flow. A variant check is a
   different mechanism and does not inherit that prohibition — but it does
   inherit the obligation to be justified by observation.
7. **Adding a `kv_err`/`read_failed` op type to the collectors, or a field to
   the existing per-request summary line** — rejected, upholding
   `docs/plans/redirect-read-failure-not-404.md`'s two explicit prohibitions.
   A field on the summary line is invisible unless tracing is on, duplicates
   `status=`, and costs the traced path. A collector op type would change
   `kv_ops` — which CLAUDE.md defines as counting host operations — and would
   shift every existing trace, under a change whose only purpose is to observe.
8. **Pinning the Python and Go sanitizers against each other** (a cross-language
   test like `test_kvprefix.py`'s) — rejected. That pin exists for values whose
   divergence fails *silently at runtime* (a prefix mismatch means the API
   writes links redirect cannot find). Two log sanitizers diverging produces
   two slightly differently-shaped log lines and nothing else. The cost of the
   pin (a Python test parsing Go source) is not repaid.
9. **Doing nothing** — live, and rejected. The status quo is three items that
   cannot progress: an incident report that says `other` and names nothing, a
   `503` arm that has never run with no way to learn why when it does, and a
   `200`-without-deleting seen once with no trace. All three are blocked on the
   same missing line, and the shipped code discards the answer at five separate
   points on purpose.

## Tasks

The lines appended to `TASKS.md`, under a new `## Observable KV failures`
section placed **above** the `# START HERE` handoff block. `TASKS.md` is
authoritative; checkboxes are ticked only there.

```
- [ ] Add the failure-line renderer, message sanitizer and failure reporter to api/obs.py (must land before every other api task in this section) — file(s): api/obs.py, api/tests/test_obs.py, api/tests/fakes.py — done when: `cd api && uv run pytest` passes with new tests covering render_failure_line (msg is the final field and nothing is appended after it), sanitize_error_message (a message containing `users:session:<token>` renders `[key:users]` and contains neither the token nor the word `session`; a `pbkdf2_sha256$...` token renders `[hash]`; `key-value error: internal server error` is left INTACT; a 300-char message truncates to 200 and sets truncated), error_type_name (returns `Err/Error_Other` for the two-level WIT-shaped stand-in and the bare class name otherwise), exc_location, and make_failure_reporter (deduplicates identical (op, ns, etype, msg) tuples to one line and stops after MAX_FAILURE_LINES_PER_REQUEST = 3 distinct tuples), and api/tests/fakes.py gains FakeWitErr, a frozen-dataclass-Exception stand-in whose str() is exactly `Error_Other(value='too many requests')` and whose `.value` type name is `Error_Other`
- [ ] Report every failed KV operation through an injected reporter in api/kvprefix.py and api/kvbatch.py (needs the task above) — file(s): api/kvprefix.py, api/kvbatch.py, api/tests/test_kvprefix.py, api/tests/test_kvbatch.py — done when: PrefixedStore takes on_error=None (with __slots__ updated) and open_views/scoped_get_many thread it, each of get/set/delete/exists calls on_error(op, namespace, duration_ns, exc) and then RE-RAISES the original exception unchanged, scoped_get_many reports its already-swallowed raw get_many failure before falling back to gather_reads, nothing is recorded into obs.Collector on a failure so every existing traced line and kv_ops value is byte-identical, and `cd api && uv run pytest` passes with a test asserting the re-raise and one asserting a get_many fallback still returns correct values while reporting exactly one failure
- [ ] Emit the unconditional api failure line from api/app.py (needs both tasks above) — file(s): api/app.py — done when: exactly one reporter is built per request in handle_request before dispatch (route via obs.route_template, never the raw path) and threaded into _dispatch, open_views and scoped_get_many receive a narrowed on_error closure, the catch-all `except Exception` emits `ev=exc` with etype and `at=<file>:<line>` before returning its unchanged 500 body, and no line is gated on log_level or X-SS-Debug
- [ ] Verify the api failure line locally against a real WIT error (needs the task above) — file(s): (none — verification step) — done when: with key_value_stores temporarily removed from [component.api] in spin.toml and `spin up --build --runtime-config-file runtime-config.toml` running, `curl localhost:3000/api/auth/me` returns 500 and stderr carries exactly one `ss comp=api ev=exc ... etype=Err/Error_AccessDenied ... msg=Error_AccessDenied()` line with log_level unset and no X-SS-Debug header sent, a second identical request produces a second line (per-request dedup, not per-instance), and `git diff spin.toml` is empty afterwards
- [ ] Add the Go message sanitizer and failure-line renderer (must land before the redirect wiring task) — file(s): redirect/linkgate/obs.go, redirect/linkgate/obs_test.go — done when: `cd redirect && go test ./linkgate/...` passes with SanitizeErrorMessage and RenderFailureLine covering the same five properties as the Python sanitizer's tests (prefixed-substring redaction to `[key:<word>]`, `pbkdf2_sha256` to `[hash]`, `key-value error: internal server error` left intact, 200-char truncation, msg last), and a doc comment records that the two implementations are deliberately NOT pinned against each other because a divergence cannot fail silently at runtime
- [ ] Return the read error from linkgate.Resolve without changing any disposition (must land before the redirect wiring task) — file(s): redirect/linkgate/resolve.go, redirect/linkgate/resolve_test.go — done when: Resolve returns (Link, Disposition, error) with a non-nil error ONLY alongside DispositionUnavailable, all thirteen existing tests keep their disposition assertions unchanged, one new test asserts the returned error is exactly the error fakeStore.getErr produced, one asserts it is nil for the other four dispositions, and `cd redirect && go test ./linkgate/...` passes
- [ ] Emit the unconditional redirect failure line from the two KV-fault arms (needs the two tasks above) — file(s): redirect/main.go — done when: a failed kv.Open emits one `ss comp=redirect ev=kv_fail ... op=open ns=-` line and a failed link Get emits one with `op=get ns=links` and the slug, both with log_level=off and no debug token; DispositionUnreadable, DispositionNotFound and recordClickCount's swallowed Get/Set errors emit nothing; the line is deduplicated per Wasm instance on (op, msg) behind a mutex-guarded map capped at 32 distinct pairs; and a successful redirect emits no line and performs exactly the same 5 KV operations as before
- [ ] Verify the redirect failure line locally (needs the task above) — file(s): (none — verification step) — done when: with key_value_stores temporarily removed from [component.redirect] in spin.toml, `/r/anything` returns 503 with `Retry-After: 2` and stderr carries one `ss comp=redirect ev=kv_fail op=open` line, a second identical request adds NO second line (per-instance dedup), `git diff spin.toml` is empty afterwards, and against an unmodified manifest a successful `/r/{slug}` 302 emits no line at all
- [ ] Correct dev/bulk-concurrent.sh's write arithmetic and stale response expectations — file(s): dev/bulk-concurrent.sh — done when: the printed write estimate is N × R (the two per-request index writes are gone since docs/plans/derived-link-indexes.md Stage 2), the header no longer tells the reader to expect index_updated in the response or to clean up "paced, never concurrently" (both obsoleted 2026-08-18), and it prints a `spin aka logs ... | grep 'ev=kv_fail'` command alongside the existing trace-reading one
- [ ] Add dev/kv-read-pressure.sh, the burst-shaped read-cap provoker — file(s): dev/kv-read-pressure.sh (new) — done when: it fires N parallel `GET /api/admin/backup` requests (each a measured ~999-operation gathered fan-out) against $APP_URL, defaults to the 10 parallel that produced `Error_Other('too many requests')` on 9 of 10 requests on 2026-08-15, prints every response's HTTP status and Server-Timing header, refuses to run with a clear message when the deploy-secrets file is unreadable, and prints the `spin aka logs ... | grep 'ev=kv_fail'` command needed to read the captured messages back
- [ ] Add dev/delete-verify.sh for the ghost-DELETE report — file(s): dev/delete-verify.sh (new) — done when: it loops N times creating a link, DELETEing it while recording the DELETE's own status code, then re-checking `GET /api/links/{slug}` and `GET /r/{slug}` immediately, at +2 s and at +10 s, traces every request with X-SS-Debug, treats ONLY a record still present at +10 s as an anomaly (sub-second redirect staleness is documented and self-heals), and on the first anomaly exits non-zero printing the slug, every recorded status code and the `spin aka logs` grep needed to pull the matching traces
- [ ] Document observable KV failures in CLAUDE.md (needs the api and redirect wiring tasks) — file(s): CLAUDE.md — done when: the "Toggleable structured logging" section records that failure lines are unconditional and independent of log_level and X-SS-Debug, the ev=kv_fail / ev=exc field vocabulary, the rule that msg is always the final field and nothing may ever be appended after it, the redaction rule with msg_redacted=1 named as the greppable answer to whether a host message embeds a key, the api-per-request (≤3 lines) versus redirect-per-instance (≤32 distinct) dedup asymmetry with the rate reasoning behind it, and that the collector itself still structurally cannot accept a key; and the "Write-throttle resilience" section records that etype is a wording-independent variant signal obtained by duck-typing type(exc.value).__name__ with no import, and that it is deliberately NOT control flow
- [ ] End-to-end deployed capture of Akamai's KV write-failure message (needs a deploy — the user's call) — file(s): (none — verification step) — done when: on a deployed build carrying the api and redirect wiring, a baseline is measured first (`GET /api/admin/consistency` → ok: true, plus the orphan report), `dev/bulk-concurrent.sh 4 5` under the cap produces ZERO ev=kv_fail lines, `dev/bulk-concurrent.sh 6 50` over the cap produces at least one, at least one run sends NO X-SS-Debug header and still produces lines, `dev/kv-read-pressure.sh` captures a read-side message too, and TASKS.md records the verbatim etype and msg values, whether msg_redacted=1 ever appeared, and whether the label should now widen — with the store returned to its measured baseline and re-verified afterwards
- [ ] End-to-end deployed attempt to provoke a redirect read failure (needs the deploy above) — file(s): (none — verification step) — done when: dev/kv-read-pressure.sh runs concurrently with `dev/redirect-load.sh -c 250 -k 8` against a live password-free slug, every /r/ status is 302 or 503 and never 404, and TASKS.md records either the first ev=kv_fail op=get line ever captured on Akamai or an explicit negative result naming the achieved request rate, the parallel-backup status distribution and the fact that the DispositionUnavailable arm remains unexercised
```

## Critical files

- `api/obs.py`
- `api/kvprefix.py`
- `api/kvbatch.py`
- `api/app.py`
- `api/tests/test_obs.py`
- `api/tests/test_kvprefix.py`
- `api/tests/test_kvbatch.py`
- `api/tests/fakes.py`
- `redirect/linkgate/obs.go`
- `redirect/linkgate/obs_test.go`
- `redirect/linkgate/resolve.go`
- `redirect/linkgate/resolve_test.go`
- `redirect/main.go`
- `dev/bulk-concurrent.sh`
- `dev/kv-read-pressure.sh` (new)
- `dev/delete-verify.sh` (new)
- `CLAUDE.md`
- `TASKS.md`

`Jenkinsfile` is **not** in scope: this change adds no new test command and
alters none of the three existing ones.

## Verification

In execution order.

1. `cd redirect && go test ./linkgate/...` — never `go test ./...`, which fails
   by design on `package main`.
2. `cd api && uv run pytest` — expect **more than 648** passing (baseline
   measured 2026-08-19).
3. `cd gui-pages && uv run pytest` — expect **71**, unchanged; this component is
   untouched and a change here means something leaked.
4. **Mutation checks, reported in the task notes, not assumed.** (a) Remove the
   redaction step from `sanitize_error_message` and confirm the
   `users:session:<token>` test — and only that test — fails. (b) Change the
   `api` reporter's dedup from per-tuple to per-call and confirm the dedup test
   fails. (c) Re-introduce `if err != nil { return Link{}, DispositionUnavailable }`
   discarding the error in `Resolve` and confirm the new error-propagation test
   fails and no disposition test does.
5. **Local, `api`, a real WIT error.** Remove `key_value_stores` from
   `[component.api]` in `spin.toml`, then
   `SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false spin up --build --runtime-config-file runtime-config.toml`,
   then `curl -i localhost:3000/api/auth/me` (no `X-SS-Debug`, no `log_level`).
   Pass: `500 {"error":"internal_error"}` and one stderr line
   `ss comp=api ev=exc route=/api/auth/me method=GET etype=Err/Error_AccessDenied at=…:… msg=Error_AccessDenied()`.
   Repeat the curl: a **second** line appears (per-request dedup). Then
   `git checkout spin.toml` and confirm `git diff spin.toml` is empty.
6. **Local, `redirect`.** Same trick on `[component.redirect]`:
   `curl -i localhost:3000/r/anything` → `503`, `Retry-After: 2`,
   `Cache-Control: no-store`, and one
   `ss comp=redirect ev=kv_fail route=/r/{slug} slug=anything op=open ns=- etype=access_denied msg=access denied`
   line. Repeat: **no** second line (per-instance dedup). Restore `spin.toml`.
7. **Local, the success path is silent.** Against an unmodified manifest, log
   in, create a link, hit `/r/{slug}` ten times: ten `302`s, zero `ss` lines of
   any kind, and `GET /api/links/{slug}/analytics` shows the clicks recorded.
   Then hit a nonexistent slug: `404`, still zero lines.
8. **Local, tracing still works unchanged.** With
   `SPIN_VARIABLE_LOG_DEBUG_TOKEN=t` set, `curl -H 'X-SS-Debug: t' -i
   localhost:3000/r/{slug}` still returns a `Server-Timing` header reading
   `desc="5 ops"`, and the summary line is byte-identical in shape to before
   this change (no `ev`, no `msg`).
9. **Deployed (the user's call to deploy).** Poll
   `curl -sI "$APP_URL/" | grep -i x-ss-version` in a loop until it reports the
   new label — the CLI's `failed to wait for deployment to go live` is a false
   negative and has taken 100–110 s every time this month. Then: login `200`,
   `/api/auth/me` shows the real `fwf.app` domain and no `localhost`,
   `GET /api/links` `200`.
10. **Deployed, the write-failure capture.** Baseline by measurement first
    (`GET /api/admin/consistency` → `ok: true`; orphan report). Then
    `./dev/bulk-concurrent.sh 4 5 ctl` — expect four clean `201`s and **zero**
    `ev=kv_fail` lines. Then `./dev/bulk-concurrent.sh 6 50 wfail` — expect
    `200` with `"partial": true` and at least one `ev=kv_fail` line. Then repeat
    the over-cap arm with the `X-SS-Debug` header removed and confirm the lines
    still appear. Read back with
    `spin aka logs --app-name "$APP_NAME" --since 15m -n 500 | grep 'ev=kv_fail'`.
    Discard the first traced sample after idle. Record `etype`, `msg` and
    whether `msg_redacted=1` appeared. Clean up (bulk delete the created
    slugs), then re-verify the store is back to baseline.
11. **Deployed, the read-failure attempt.** `./dev/kv-read-pressure.sh 10`
    alone first (expect some `500`s and `ev=kv_fail` lines carrying a read-side
    message). Then run it concurrently with
    `./dev/redirect-load.sh -u "$APP_URL/r/<slug>" -c 250 -k 8`. Pass condition
    is **not** "a 503 appeared": it is that every `/r/` status is `302` or
    `503` and never `404`, and that the outcome — positive or negative — is
    written down with the achieved rate. A negative result is a result.
12. **Deployed, item 3.** `./dev/delete-verify.sh 25`. Pass: 25 clean cycles and
    exit 0, or a non-zero exit that names the slug and hands over the trace —
    either outcome closes a question that is currently open.

## Out of scope / follow-ups

- **Any fix.** Widening `classify_write_error`'s label, promoting `etype` to
  control flow, retrying reads in `redirect`, closing KV store handles to rule
  out `Error_StoreTableFull` — all deliberately excluded. Observe first.
- **`log_level=verbose`** (per-operation lines) — already filed under
  `TASKS.md` Future work and untouched here; it needs a per-request id, which
  this change does not add.
- **Instrumenting `gui-pages`' WASI file reads** — separate filed entry, no KV
  involvement.
- **Logging `redirect`'s analytics write failures** (Trade-offs #2) — filed as
  Future work with a counter-only shape and a trigger, so the M2 loss rate
  could one day be attributed without an unconditional line on the hottest
  write in the app.
- **A token-gated raw-message echo** — filed as Future work, triggered
  specifically by `msg_redacted=1` appearing in a captured line, i.e. by the
  sanitizer telling us the host does embed the key.
- **Promoting the WIT variant name to control flow** — filed as Future work,
  triggered by a captured `etype` showing a permanently-failing variant
  (`Error_AccessDenied`, `Error_NoSuchStore`) being retried pointlessly.
