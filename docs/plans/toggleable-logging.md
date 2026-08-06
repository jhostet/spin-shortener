# Toggleable structured logging, with KV timing as the first consumer

**Status:** planned 2026-08-06, reviewed by the planner agent the same day. Not implemented.
**Scope:** `redirect` (Go) and `api` (Python). `gui-pages` and `gui` are out of scope.
**First consumer:** per-request KV operation timing.

## Context

The app has **zero logging today** — grepping for `print(`, `logging`, `sys.stderr`, `log.`,
`fmt.Print` and `os.Stderr` across `api/`, `gui-pages/` and `redirect/` returns nothing outside
tests. Every question about runtime behaviour has so far been answered by adding a temporary
probe, rebuilding, and reverting. That is literally how the measurements below were produced, and
it does not work at all against a deployed app, where you cannot edit and rebuild to ask a
question.

The immediate need is **KV timing**. Three of the four tabled Akamai tasks in `TASKS.md` are
questions that timing data would answer or sharpen:

- whether sustained redirect throughput really ceilings at ~25/second (two KV writes per click
  against a published 50 write RPS cap),
- whether a full-cap 5,000-entry restore can finish inside the 30-second handler limit,
- whether `get_keys` works on Akamai's KV host, and at what cost.

None of those can be answered locally: the local store is sqlite/in-memory (Spin logs
`Storing default key-value data to a temporary in-memory store`) and Akamai's is not.
**This logging is the instrument that makes the deploy measurable**, so it is a prerequisite for
closing those tasks rather than a parallel nicety.

The counter-pressure is that this app should not log constantly. It is a redirect service whose
hot path is ~175 µs end to end; a logging design that costs a meaningful fraction of that, or
fills a 7-day retention window with per-click noise, is worse than none. Hence: default off, and
cheap when off.

## Key technical facts confirmed during research

Measurements taken 2026-08-06 against a live `spin up --build --runtime-config-file runtime-config.toml`
on darwin, via temporary probes in `redirect/main.go` and `api/app.py`, reverted immediately
afterwards. **Local KV is sqlite/in-memory, so these are lower bounds** that establish the
instrument works and what it costs — not predictions of Akamai behaviour.

**`redirect` per click** (5 successful 302s plus one 404 miss):

| operation | observed |
|---|---|
| `kv.Open` | 12–33 µs (**twice** per click — `main.go:63` and `main.go:127` inside `recordAnalytics`) |
| `store.Exists` (`links:slug:*`) | 13–18 µs |
| `store.Get` (`links:slug:*`, 262 B) | 5–9 µs |
| `store.Get` (`analytics:count:*`) | ~4 µs |
| `store.Set` (`analytics:count:*`) | 9–12 µs |
| `store.Set` (`analytics:events:*`) | 5–6 µs |
| whole handler | 137–180 µs **excluding the cold first request, which was 356 µs** |

**`api`** (login, list, consistency):

| operation | observed |
|---|---|
| `key_value.open` | 1.2 ms **cold**, then 35–70 µs |
| `variables.get` | ~25 µs each |
| `ensure_bootstrap_admin` | **790 ms on the first request against a fresh store**, 45–65 µs after |

1. **One stderr write costs ≈6–8 µs in Go, ≈12–16 µs in Python — isolated directly.** A
   dedicated probe (200 consecutive ~130-byte `fmt.Fprintf(os.Stderr, ...)` / `print(...,
   file=sys.stderr)` calls, nothing else between them) measured 5,716–7,543 ns/write in Go and
   12,474–16,233 ns/write in Python, live under `spin up --build`, 2026-08-06. Both are well
   under the ~30 µs report threshold, confirming the rollup-not-per-op decision. This replaces
   the earlier ≈13 µs Go estimate (derived indirectly from the 404-miss probe row) and the
   previously-unmeasured Python figure. 7 µs is ~4% of a 175 µs handler for one rollup line, and
   ~28–40% for seven per-operation lines — the gap that is the entire basis for the
   rollup-not-per-op decision, and it survives the corrected numbers for both languages.
2. **The clock is fine.** Consecutive `time.Now()` calls differ by 750–1000 ns in Go, and
   `time.monotonic_ns()` by 400–800 ns in Python, where `time.get_clock_info('monotonic')`
   reports `clock_gettime(CLOCK_MONOTONIC)` at 1 µs. Sub-millisecond KV operations are
   measurable. **This contradicts CLAUDE.md's Analytics section**, which blames recent-events
   slot collisions on "the WASI clock having deliberately limited resolution"; requests 15–18 ms
   apart received distinct microsecond-resolution timestamps. Out of scope here, recorded in
   TASKS.md Future work so it is not lost.
3. **`api` has three KV entry points, not two.** `PrefixedStore`'s four methods
   (`api/kvprefix.py:39-49`) and `scoped_list_keys`' inner function (`kvprefix.py:73`) cover
   every business-logic module — `.raw` is touched only inside `kvprefix.py`, and
   `spin_sdk.key_value` is imported only in `api/app.py:4`. But `key_value.open` itself is called
   directly at **`api/app.py:60`**, outside both, and must be instrumented separately.
4. **`redirect` has no chokepoint** — five direct `store.*` calls across `main.go`, in
   `package main`, which is not host-testable (`go build ./...` fails by design; CLAUDE.md's
   Tests section). Solved by putting the pure logic in `linkgate` behind a local interface.
5. **`*kv.Store` already satisfies a minimal local interface.** `spin-go-sdk/v3@v3.0.0/kv/kv.go`
   exposes `Exists(string) (bool, error)`, `Get(string) ([]byte, error)`,
   `Set(string, []byte) error`. So `linkgate` can define
   `interface { Exists(...); Get(...); Set(...) }` and stay free of any `spin-go-sdk` import.
   `lookupLink` has exactly two callers (`main.go:69`, `main.go:96`) and no test references it.
6. **`crypto/subtle` is already imported** at `redirect/linkgate/password.go:6`, so the
   constant-time token compare adds no dependency.
7. **The Spin Go SDK latches response headers on the first `Write`, not on `WriteHeader`.**
   `convertor_outgoing_response.go:46-48` — `WriteHeader` only stores the int.
   `Write` (line 31-32) calls `send()`, and `send()` snapshots the header map via
   `toWasiHeaders(self.headers)` at line 68. `Header()` (line 27) returns the live map.
   **This is why a header computed after the handler runs requires buffering the body**, and why
   "defer `WriteHeader`, forward `Write`" would silently lose the header.
8. **`responseWriter` implements only `Header`, `Write` and `WriteHeader`** — no `http.Flusher`,
   `http.Hijacker`, `io.ReaderFrom` or `http.CloseNotifier` (whole file read). So wrapping it
   cannot degrade an interface a handler relies on, which is the usual hazard with response-writer
   wrapping and does not apply here.
9. **`http.Redirect` writes a body.** For a GET with no pre-existing `Content-Type` it sets
   `Content-Type: text/html; charset=utf-8`, calls `WriteHeader`, then writes
   `<a href="…">Found</a>.` So the 302 path exercises the buffer too — the responses are small,
   but they are not empty.
10. **`spin_sdk.http.Handler` invokes `handle_request` by plain name lookup.**
    `spin_sdk/http/__init__.py:86` does `await self.handle_request(Request(...))` inside a bare
    `try`, with no `isinstance` check on the return; the base method (line 42) is a
    `NotImplementedError` stub; the `except` at line 92 turns a raised exception into a 500.
    So renaming the body to `_dispatch` and overriding `handle_request` with a wrapper is safe,
    and a `try/finally` in the wrapper still emits a line on the exception path.
11. **`{ default = "", secret = true }` is a valid Spin variable declaration** — verified by
    adding both variables to `spin.toml` and running `spin up --runtime-config-file runtime-config.toml`,
    which parsed and served. Reverted immediately; manifest-parse check only.
12. **`spin aka` cannot change a variable on a deployed app.** `spin aka app --help` lists
    `list`, `delete`, `status`, `deploy`, `logs`, `cron`, `link`, `unlink`, `history` — nothing
    for variables. Changing `log_level` on Akamai means a redeploy.
13. **Spin does not truncate component log files between runs.** `spin up --help` documents a
    `--truncate-logs` flag, and `.spin/logs/gui_stderr.txt` still holds bytes written on Jul 31
    despite runs on Aug 4 and Aug 6. Any "the log is empty" assertion must truncate first.
14. **`time.Sleep` traps in the componentize-go environment** — a probe using it produced a 500
    with no Go panic message, only truncated stderr. Nothing here needs to sleep, but future
    timing work should know the failure mode is a silent 500.

`gui-pages` touches no KV, imports no `key_value`, and declares no `key_value_stores`. It is
excluded. `gui` is a prebuilt third-party binary and cannot be instrumented at all.

## Decisions

Settled with the user before this plan was written; not open for relitigation during build.

1. **Toggle: a Spin variable baseline *plus* a per-request debug token.** Both, not either.
2. **Output: one structured stderr line per request, plus a `Server-Timing` header on
   token-bearing requests.** Per-operation lines deferred.
3. **Scope: `redirect` and `api`.**
4. **The debug token is a single shared secret compared against one header in both components.**
   `redirect` has no session, cookie or principal, so a permission check is impossible there;
   gating `api` on `users.manage` while gating `redirect` on a token would mean two activation
   mechanisms for one feature.

**Why the token exists given the variable:** per fact 12, the variable alone means a redeploy to
turn logging on and a second to turn it off. The token makes one request traceable with no
redeploy — the difference between diagnosing a live incident and not.

## Configuration

Two new variables in `spin.toml`'s `[variables]`, wired into **both**
`[component.redirect.variables]` and `[component.api.variables]`, and into neither `gui` nor
`gui-pages`:

```toml
log_level       = { default = "off" }
log_debug_token = { default = "", secret = true }
```

- `log_level` accepts `off` and `summary`. **Any unrecognised value is treated as `off`** —
  fail-closed, never raise. It is a level rather than a boolean so a future `verbose` needs no
  rename and no migration.
- `log_debug_token`: a request carrying `X-SS-Debug: <value>` that matches is traced and gets a
  `Server-Timing` header regardless of `log_level`. **An empty configured token must never match
  anything**, including an empty or absent header — the guard is an explicit
  `if configured == "" { return false }` *before* any comparison, not a property of the
  comparison. Getting this wrong makes the default configuration "anyone can enable tracing",
  the exact failure the token is meant to avoid. Comparison is
  `crypto/subtle.ConstantTimeCompare` in Go, `hmac.compare_digest` in Python.

**Both variables are read once and cached for the lifetime of the Wasm instance**, not per
request — `sync.Once` in Go, a module-level sentinel in Python. This is sound *because* a Spin
variable cannot change without a restart locally or a redeploy on Akamai (fact 12), both of which
produce fresh instances. A per-request read costs ~25 µs in Python (measured) to re-read a value
that cannot have changed. If instances are not reused the cache degrades to exactly the
per-request cost and is never worse.

## What is collected

A per-request collector accumulating, per operation type (`open`, `exists`, `get`, `set`,
`delete`, `list_keys`): count, total duration, total bytes moved. Plus the single slowest
operation (type + namespace + duration) — one comparison per operation, no extra write, which
recovers most of what per-operation lines would have shown.

**The collector's record method takes an operation type, a namespace and a duration. It has no
parameter that could accept a key.** Same structural move as `PrefixedStore` deliberately having
no `get_keys`: a key cannot be logged by mistake because there is nowhere to put one. This is not
theoretical — **`users:session:<token>` is a live session credential** and `spin aka logs` serves
the last 7 days by default, so a key-logging design would put working session tokens in a
week-long retention window. Values are never touched, only their length.

In Python the namespace is free — `PrefixedStore` holds `self.prefix`. In Go it comes from
matching against the existing `linkgate.LinksPrefix`/`AnalyticsPrefix`. An `open` has no
namespace and reports `-`.

**`kv_ops`/`kv_us` are the sums across all types, and `open` counts as an operation** — on the
redirect path it is two of seven, and omitting it would hide a third of the KV cost.

**Note for anyone hand-counting `api` lines:** `auth.ensure_bootstrap_admin` runs on *every*
request (`api/app.py:68`), so every `api` line includes its operations on top of the endpoint's
own.

## Output format

One logfmt line per request to stderr, prefixed with a literal `ss ` so it is greppable and
distinguishable from Spin's own output:

```
ss comp=redirect route=/r/{slug} slug=M7RyJVC status=302 dur_us=174 kv_ops=7 kv_us=80 kv_bytes=262 open=2/35 exists=1/17 get=2/11 set=2/17 slow=open:-:20
ss comp=api route=/api/links method=POST status=201 dur_us=4210 kv_ops=9 kv_us=402 kv_bytes=1841 open=1/41 get=3/22 set=4/61 list_keys=1/278 slow=list_keys:links:278
```

Per-op-type fields are `count/total_µs`; zero-count fields are omitted. `slow` is
`type:namespace:µs`. On the Python exception path the line still emits, with `status=500 err=1`.

**Paths are logged as route templates, with one exception.** `api` paths embed usernames
(`/api/users/{username}`) and slugs; the template is logged and the identifier is not. `redirect`
additionally logs the raw slug, because correlating a slow resolution to a specific link is the
entire point of instrumenting that path, and this codebase's stated position is that slugs are
not secret (CLAUDE.md, "Security tradeoffs"). Query strings, header values and bodies are never
logged.

`Server-Timing`, on token-bearing requests only:

```
Server-Timing: kv;dur=0.080;desc="7 ops", handler;dur=0.174
```

**`Server-Timing` durations are milliseconds** as floats — 80 µs is `0.080`, not `80`. It is
emitted only for a valid token, never merely because `log_level=summary`, so a baseline-logging
deployment does not hand timing data to every visitor.

CSP does not interact with this in either direction — `api/responses.py:43`'s
`default-src 'none'` and `gui-pages`' policy both govern what a *document* may fetch or execute,
not what headers a response may carry. Nothing to do; stated here so it is not re-asked.

## Redirect (Go) changes

**The op profile does not change. Both `kv.Open` calls stay.** `recordAnalytics` keeps opening
its own store and gains only a collector parameter. Threading the handler's store into it would
remove one `kv.Open` — 12–33 µs of a 137–180 µs handler, an 8–20% improvement — which is exactly
the hot-path optimisation `TASKS.md` defers pending real Akamai timing evidence. Doing it here
would change the baseline that every future measurement is compared against, inside the very
change that builds the measuring instrument. **A successful redirect is 7 KV operations before
and after this plan.**

**Collector propagation: `context`.** The wrap point attaches the collector with
`r = r.WithContext(...)` only when tracing is enabled; handlers retrieve it with a
`collectorFrom(ctx)` helper that returns a no-op collector when absent. The off path never
allocates a context. **A package-level collector variable is forbidden** — it passes every
single-request check and silently interleaves under concurrency, mis-attributing one request's
operations into another's line, which makes the instrument worse than nothing.

**Three request paths** in `spinhttp.Handle`'s callback (`main.go:20-23`):

1. **Tracing off** — `mux.ServeHTTP(w, r)` exactly as today. The real `http.ResponseWriter` is
   passed through, nothing wrapped, nothing buffered. **The off path must remain byte-identical
   to current behaviour**, which is what makes this safe to deploy with logging disabled.
2. **`log_level=summary`, no token** — wrap `w` in a writer recording the status code and
   forwarding every call immediately. No buffering; streaming unchanged.
3. **Valid token** — buffer status and body so `Server-Timing` can be set before the first
   `Write` latches the headers (fact 7). Safe here because responses are a small 302 (which does
   carry a body, fact 9), a small 404, and the password prompt page. Deliberately confined to
   token-bearing requests so a bug in the buffering cannot affect normal traffic.

In all three cases the wrapper **returns the real `w.Header()` map, not a copy** (fact 7 — it is
the live map). That is what keeps `Location`, `Content-Type` and `renderPasswordPrompt`'s CSP
working, and lets `Server-Timing` be added afterwards as one more `Set` on the same map. Do not
"improve" this into a copied map. The wrapper's recorded status **defaults to 200**, matching
`newHttpResponseWriter`, so a path that writes without calling `WriteHeader` logs 200 rather than
0. If the buffered body is empty (a HEAD request — Go 1.22's `ServeMux` matches HEAD against a
`GET` pattern), the wrapper must still trigger the send, by calling `real.Write(nil)`.

The stderr write happens **after** the response is written, so its ~13 µs lands in neither the
measured handler duration nor the visitor's latency.

**Where the code lives.** The collector, its formatting, the namespace classification, the
`Server-Timing` rendering **and the timing store wrapper** all go in **`redirect/linkgate/`**,
which is host-testable. The wrapper is defined against the local interface (fact 5), so
`linkgate` needs no `kv` import and can be tested against a fake store. Only the wiring —
`kv.Open`, `variables.Get`, reading the header, the `Fprintf`, the response-writer types —
stays in `package main`, verified live rather than unit-tested, as `setSecurityHeaders` already
is. `lookupLink`'s parameter changes from `*kv.Store` to the interface.

## API (Python) changes

`handle_request` returns a `responses.Response` value before anything is written, so the header
is added by mutating `response.headers` after dispatch — no buffering, no writer wrapper, no
branching. **Because the mutation happens on the returned object, it covers `qr.py`'s image
responses** (`api/qr.py:84` builds its own header dict and bypasses `json_response`) with no
special case; a handler cannot skip it.

`HttpHandler.handle_request`'s current body is renamed **`_dispatch(self, request, collector)`** —
an explicit parameter, **not** an attribute on `self`, since `handle()` dispatches through
`componentize_py_async_support.spawn` (`spin_sdk/http/__init__.py:104`) and an instance is not
obviously single-request. `handle_request` becomes a thin wrapper: build the collector, await
`_dispatch` in a `try/finally`, attach `Server-Timing` if the token matched, emit the line. The
`finally` matters — a handler that raises should still produce a line, and that line is the only
evidence anyone will have.

Instrumentation attaches to `PrefixedStore.get/set/delete/exists`, `scoped_list_keys`' inner
function, **and the direct `key_value.open` at `app.py:60`** (fact 3), which is recorded as the
`open` operation. `variables.get` is **not** a KV operation and must not enter `kv_ops`.
`open_views(physical_store, collector=None)` and `scoped_list_keys(raw_list_keys, collector=None)`
both default the collector to `None`, so every existing call site and all 485 existing tests keep
working unchanged.

**Token-bearing `api` responses also get `Vary: X-SS-Debug`.** The response now varies on a
request header while `api/responses.py` sets no `Cache-Control` at all, so a heuristically-caching
intermediary could otherwise serve one visitor's timing data to another. Low severity — it is the
same data `log_level=summary` would write to a log — but this plan rejected "emit `Server-Timing`
for everyone" on disclosure grounds, so the cheap guard is consistent. `redirect` needs none; it
already sends `Cache-Control: no-store` (`main.go:57`).

**The new pure module must be `api/obs.py`, never `api/logging.py`.** `componentize-py` compiles
`app.py` alongside its siblings with the component's own directory on the path, so a module named
`logging.py` would shadow the standard library's for every stdlib module importing it. Same for
`time.py`, `json.py`. **UNCONFIRMED** by direct test — the naming decision is correct regardless,
so this was not worth a build to prove.

## Trade-offs and rejected alternatives

1. **Per-operation log lines** — rejected for v1, deferred as a `verbose` level. At ≈13 µs per
   stderr write (fact 1) and seven operations per click, it adds ~91 µs to a 137–180 µs handler
   — a 50–66% increase — and inflates the very durations being collected. On
   `backup`/`restore`/`consistency`, which perform thousands of operations against a 30-second
   limit, 5,000 operations means 5,000 lines and ~65 ms of pure logging. It also carries a hidden
   prerequisite: without a per-request ID, interleaved lines from concurrent requests cannot be
   attributed to a request at all — plus key redaction and a volume guard. The rollup is a strict
   prefix of it (the collector visits every operation regardless), so this defers in one direction
   only; starting here would build IDs, redaction and volume guards up front for detail that may
   never be needed.
2. **A KV-backed runtime toggle** (`_meta:log_config`) — rejected. Changeable without a redeploy,
   which is genuinely what the variable lacks, but it costs a KV read on every request to decide
   whether to time KV reads. Self-defeating for the stated use case, and it spends the 1,000 read
   RPS budget on configuration. The debug token buys the same property for free.
3. **Gating `api`'s tracing on `users.manage` instead of a token** — rejected. Avoids a new secret
   and reuses the existing authorization vocabulary, but `redirect` cannot implement it at all, so
   the feature would activate two different ways in two components. Revisit only if the shared
   secret proves awkward to rotate.
4. **JSON lines instead of logfmt** — rejected. `spin aka logs` returns plain text to a terminal
   and nothing here consumes structured logs programmatically. logfmt is readable unaided and
   `awk`-able; JSON is neither without tooling that does not exist.
5. **Emitting `Server-Timing` whenever `log_level=summary`** — rejected. It would hand internal
   timing to every visitor of a deployment that merely turned baseline logging on.
6. **Removing the second `kv.Open` while wiring the collector** — rejected; see the first
   paragraph of "Redirect (Go) changes". It is a real 8–20% win and it stays deferred.
7. **Reading the variables per request rather than caching** — rejected; see Configuration.
   **Revisit if Akamai ever ships variable updates without a redeploy**, at which point the cache
   would silently serve a stale level.
8. **Instrumenting `gui-pages`** — out of scope, not wrong. It does no KV work; its interesting
   cost is WASI file reads, a different shape this KV collector would answer badly.

## Tasks

Mirrored in `TASKS.md` under `## Toggleable structured logging`.

1. Isolate the true per-write stderr cost in both languages.
2. Add `log_level` and `log_debug_token` to `spin.toml`.
3. Add the pure collector, renderers and timing store wrapper to `redirect/linkgate/obs.go`.
4. Wire the collector through `context` into `redirect`'s KV call sites.
5. Wire `redirect`'s toggle, three request paths and stderr emission.
6. Add `api/obs.py`.
7. Instrument `PrefixedStore` and `scoped_list_keys`.
8. Wire `api`'s toggle, `_dispatch` wrapper, `Server-Timing` and `Vary`.
9. End-to-end manual verification.
10. Document the feature in CLAUDE.md.

## Critical files

**New:**

- `redirect/linkgate/obs.go` — collector, logfmt renderer, `Server-Timing` renderer, namespace
  classifier, timing store wrapper. No `spin-go-sdk` import.
- `redirect/linkgate/obs_test.go`
- `api/obs.py` — same shape, zero `spin_sdk` imports. **Not `logging.py`.**
- `api/tests/test_obs.py`

**Modified:**

- `spin.toml` — two variables, wired into `redirect` and `api` only.
- `redirect/main.go` — three request paths, cached toggle accessor, `context` propagation,
  `lookupLink`'s parameter type, `recordAnalytics`'s collector parameter, the `Fprintf`.
- `api/kvprefix.py` — optional collector on `open_views` and `scoped_list_keys`; the four
  `PrefixedStore` methods. **`get_keys` stays absent and the `TypeError` guard is unchanged.**
- `api/app.py` — `_dispatch` rename and wrapper, `key_value.open` timing, cached toggle.
- `api/tests/test_kvprefix.py` — **new tests added; no existing test edited.**
- `CLAUDE.md`

**Deliberately untouched:** `api/backup.py`, `api/consistency.py`, `kvprefix.STORE_PREFIXES` —
this plan adds no KV key type, so none of the three obligations a new key imposes apply.
`api/tests/test_kvprefix.py`'s cross-language guard scrapes `keys.go` for
`LinksPrefix`/`AnalyticsPrefix` only, so adding `obs.go` (which merely reads those constants)
cannot break it.

## Verification

Every step against a live
`SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false spin up --build --runtime-config-file runtime-config.toml`.

**Truncate `.spin/logs/*_stderr.txt` before each run** — Spin does not truncate between runs
(fact 13), and those files currently hold probe output from the measurement session.

1. **Default is silent.** With neither variable set, exercise login, link create, `/r/{slug}`,
   a 404, the password prompt, a backup and the consistency check. Both stderr files are empty
   afterwards. No response carries `Server-Timing`.
2. **Off path unchanged.** With logging off, `curl -sI` on a 302, a 404 and the prompt page
   returns the same header *set and values* as a build of current `main`. **Do not assert header
   order** — `toWasiHeaders` iterates a Go map, so order is nondeterministic even today.
3. **Baseline on.** `SPIN_VARIABLE_LOG_LEVEL=summary`: exactly one `ss ` line per request.
   Hand-counted expectations, not read off the output — **7 for a successful redirect**
   (2 `open`, 1 `exists`, 2 `get`, 2 `set`); **2 for a nonexistent slug** (1 `open`, 1 `exists`;
   `lookupLink` returns at `main.go:166` before the `Get` and `recordAnalytics` never runs);
   **3 for a disabled or out-of-window link** (1 `open`, 1 `exists`, 1 `get` — `main.go:70`
   evaluates `Status`/`IsWithinWindow` only after `lookupLink` succeeds). That third case is what
   proves the collector isn't counting a fixed shape. No `Server-Timing` on any response.
4. **Durations are real.** Assert `0 < kv_us < dur_us`, that every present per-op field has a
   nonzero total, and that `Server-Timing`'s `handler;dur` equals `dur_us/1000` to within
   rounding. A collector that counts operations but records zero duration otherwise passes every
   other step here.
5. **Concurrency.** `seq 20 | xargs -P20 -I{} curl -s -o /dev/null http://127.0.0.1:3000/r/<slug>`,
   then assert exactly 20 `ss ` lines and that **every one reports `kv_ops=7`** — not 0, not 12,
   not 42. This is the step that catches a package-level or `self`-attached collector, which
   passes every single-request check.
6. **Token path.** With `SPIN_VARIABLE_LOG_DEBUG_TOKEN=<t>` and `log_level=off`: `X-SS-Debug: <t>`
   produces both a line and the header; no header produces neither; a *wrong* token produces
   neither. `api` responses to token-bearing requests carry `Vary: X-SS-Debug`.
7. **Empty token never matches.** With `log_debug_token` unset, requests carrying `X-SS-Debug:`
   (empty), `X-SS-Debug: ""`, and no header each produce no line and no header.
8. **Body integrity through the buffering writer.** With the token set, `curl -s` the password
   prompt and `diff` it byte-for-byte against the same page fetched without the token; `curl -sD-`
   the 302 and confirm `Location` is intact and the `<a href>` body matches the untokened
   response. A buffer that truncates or double-writes passes every other step.
9. **Error and non-200 paths.** An `api` request that 401s and one that 404s each produce exactly
   one line with the correct `status=`. If a 500 can be forced, it produces one line with
   `status=500 err=1`.
10. **No credential material.** With baseline logging on, log in, use the session, delete a user,
    take a backup. Neither stderr file contains `pbkdf2`, any session-cookie value, or any full KV
    key. **Do not grep for the bare word `password`** — it false-positives on the logged route
    template for `POST /api/links/{slug}/password` (`api/app.py:114`).
11. **`Server-Timing` reaches a browser.** Inject `X-SS-Debug` via Playwright's CDP
    `setExtraHTTPHeaders` (DevTools alone cannot add a header to a top-level navigation, and
    `gui/app.js`'s fetch helpers do not send it), load `dashboard.html`, and confirm the header
    renders in Network → Timing with a clean console in both themes. A `curl -sD- /api/auth/me`
    check is an acceptable substitute if CDP is unavailable; CSP cannot interact with a response
    header, so nothing is lost by dropping the browser framing.
12. **Suites.** `cd api && uv run pytest`, `cd gui-pages && uv run pytest`,
    `cd redirect && go test ./linkgate/...` all pass, counts recorded. The existing api count is
    485 and must not drop.

## Out of scope / follow-ups

- A `verbose` level emitting per-operation lines, with a per-request ID and key redaction.
  Trigger: a concrete question the rollup provably cannot answer.
- Finding the real cause of the recent-events slot collisions, and correcting CLAUDE.md's
  clock-resolution explanation (fact 2). `linkgate.EventSlot`'s hashing is the obvious suspect.
- Instrumenting `gui-pages`' WASI file reads.
- Shipping logs anywhere. `spin aka logs` (7-day default, `--since`, `--component-id`,
  `--region`, `-n`) is the retrieval mechanism; this plan adds no alternative.
- Aggregation, percentiles or an admin page rendering timings. The line format is `awk`-able on
  purpose so this stays unnecessary for as long as possible.
