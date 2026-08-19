# Redirect Read Failures Must Not Answer 404

## Context

`redirect` is the only component in this app with a known correctness defect, and
it is the one component whose answer a visitor reads as a statement of fact about
someone's campaign URL. `lookupLink` (`redirect/main.go:496`) collapses three
distinct conditions into a single `false`:

```go
func lookupLink(store linkgate.KVStore, slug string) (linkgate.Link, bool) {
	raw, err := store.Get(linkgate.LinkKey(slug))
	if err != nil || len(raw) == 0 {
		return linkgate.Link{}, false
	}
	l, err := linkgate.ParseLink(raw)
	if err != nil {
		return linkgate.Link{}, false
	}
	return l, true
}
```

(a) the KV read *failed*, (b) the key is genuinely *absent*, (c) the record is
present but *unparseable*. Both callers — `handleRedirectGet` (`main.go:280`, the
`!ok` branch at 290–293) and `handleRedirectPost` (`main.go:307`, identical shape
at 317–320) — answer `http.NotFound`. So read-path saturation is reported to a
visitor as "this link does not exist," and a corrupt record is reported the same
way.

This was measured, not theorised. On deployed build `43d66c6-no-events` with
`hey -disable-redirects` (TASKS.md, `### THE "~58 req/s CEILING" WAS MY OWN
HARNESS`): 0% wrongly-404 at 690 rps, **7% at 726, 31% at 947, 43% at 1292**,
with the same link resolving normally at rest immediately afterwards. A
successful redirect performs 2 KV gets, so ~700 rps is ~1,400 reads/second
against Akamai's documented 1,000 reads/second app-wide cap. The filed bug is
TASKS.md's Future-work entry at line 375; the session handoff names it as the
only known correctness defect and the first thing to pick up.

Why it deserves a fix rather than a note: a 404 is a claim about the **link**,
not about the server, and this codebase deliberately makes it indistinguishable
from a disabled or out-of-window link (CLAUDE.md, "Security tradeoffs" — that
indistinguishability is a probing-resistance feature and a liability here).
`Cache-Control: no-store` is the only reason the lie is not also cached.

**Confirmed decisions (settled by the requester before planning):**

- The three options in the filed entry (503 + `Retry-After`; bounded retry inside
  `redirect`; threading an explicit absent-vs-unreadable-vs-unavailable
  distinction out of `lookupLink`) are **not mutually exclusive**; recommend one,
  record the rejected ones.
- Fixing the 1,000 reads/second cap itself, and reducing the redirect's read
  count, are **out of scope**. This is about correct behaviour when a read
  fails, not about failing less often.
- **No deploy.** Deploys are the user's call. The plan must still say exactly how
  the fix would be confirmed against a deployed build, using the established
  `hey -disable-redirects` harness, and noting that `hey` divides `-n` by `-c`
  (so use `n = c × k`).
- Probing resistance is a hard constraint: **absent, disabled and out-of-window
  must stay indistinguishable from each other.**
- The success path should ideally be byte-identical; any added KV operation or
  retry on it must be called out loudly.

## Key technical facts confirmed during research

- **The SDK already distinguishes absent from failed; only `lookupLink` throws
  the distinction away.** `spin-go-sdk/v3/kv.Store.Get` (read at
  `$GOMODCACHE/github.com/spinframework/spin-go-sdk/v3@v3.0.0/kv/kv.go`) returns
  `([]byte(""), nil)` when the host reports `value.IsNone()`, and `(nil, err)`
  when the host returns an error. So `err != nil` and `len(raw) == 0` are already
  clean, separate signals at the call site. No new KV operation is needed to tell
  them apart — this fix costs zero KV ops.
- **There is no typed KV error variant to match on.** The same file's
  `errorVariantToError` maps the four WIT variants onto bare `fmt.Errorf`
  strings: `"access denied"`, `"no such store"`, `"store table full"`, and
  `fmt.Errorf("%v", code.Other())` for everything else — which is where a
  throttling message would arrive. There are no exported sentinel errors. This
  mirrors the Python side exactly (CLAUDE.md, "Write-throttle resilience": every
  write error is treated as retryable *unconditionally*, and the
  `"too many requests"` substring match is **labelling only, never control
  flow**). This plan therefore treats **every** `Get` error as "unavailable"
  with no variant check and no string match.
- **`access denied` / `no such store` cannot realistically reach `Get`.** They
  are `kv.Open` failures — `Open` returns them before a handle exists — so the
  practically reachable `Get` error set is `other` (host errors, throttling
  included) and `store table full`. Both are server-side and plausibly
  transient. Confirmed by reading `Open`/`Get` in the file above.
- **Akamai caches 404 by default for 10 seconds; it does not cache 500/502/503/504
  unless a behaviour is explicitly enabled.** `techdocs.akamai.com/property-mgr/docs/cache-http-error-responses`,
  fetched 2026-08-19: 204, 305, **404**, 405 and 501 are "automatically cached for
  10 seconds by default"; "error responses with HTTP codes 500, 502, 503, and 504
  are not cached unless you enable this behavior." **The proposed statuses are
  strictly safer to cache-poison than the status they replace.**
- **The 302-not-301/308 rule is unaffected.** `techdocs.akamai.com/property-mgr/docs/cache-http-redirects`,
  re-fetched 2026-08-19: 302 and 307 are not cached by default, 301 and 308 are
  cached "according to the same caching rules that are used for 200 responses."
  This plan does not touch any redirect status.
- **UNCONFIRMED: whether the `*.fwf.app` (Akamai Functions) edge inherits the
  Property Manager error-caching defaults, and whether it replaces an origin 5xx
  with its own error page.** Akamai Functions publishes no caching documentation
  at all (CLAUDE.md: `docs/caching` 404s). What *is* measured on this app is that
  fwf.app does not cache 302s **or 404s** (2026-08-06, twenty 302s and fifteen
  404s with distinct `akamai-grn`, no `Age`/`X-Cache`/`X-Check-Cacheable`, origin
  service time on every request) — and 404 is on the by-default-cached list, so
  fwf.app is *already* more conservative than the Property Manager default. To
  confirm for 503, capture headers on a real 503 during a saturating run; that is
  a numbered step in Verification.
- **UNCONFIRMED: whether a burst of origin 5xx trips anything at the fwf.app
  edge** (health-check ejection, a circuit breaker). Confirming it needs a
  saturating run followed by an immediate re-run of the clean `c=60` baseline —
  also a numbered Verification step.
- **UNCONFIRMED: whether `time.Sleep` works inside this component at all.** The
  Python side proved `asyncio.sleep` raises under componentize-py and needed
  `wasi_clocks_monotonic_clock...wait_for`; nothing equivalent has ever been
  tested in the Go component. This plan rejects retry on other grounds, so the
  question is not blocking — but do not assume a sleep is available if retry is
  ever revisited. Confirming it needs a temporary spike route and a real
  `spin up --build`, the same way the Python primitive was confirmed.
- **The filed entry's "cheap first step" does not work, and this is worth knowing
  before anyone tries it.** It claims "the traced log line already distinguishes
  them — a failed get shows as a `get` op with `status=404`, so a deployed build
  with `log_level=summary` could confirm whether this ever happens in real
  traffic." Verified against the code: `linkgate.TimedStore.Get`
  (`redirect/linkgate/obs.go:130`) records the op **regardless of the error** and
  discards the error; it records `len(v)` bytes, which is 0 for both an error
  (`nil`) and an absent key (`[]byte("")`); `emitLogLine` (`main.go:184`) emits
  only `comp`/`route`/`slug`/`status`, and `RenderLogLine` has no `err` field for
  this component (`err=1` is `api`-only). So a read failure and a genuine miss
  produce **the same field set with the same values**: `status=404 kv_ops=2 …
  open=1/… get=1/…`. A `log_level=summary` build could not have answered the
  question. After this fix the **status code itself** is the signal, which is why
  no new log field is proposed.
- **`lookupLink` has exactly two callers and no test references it** (grepped
  repo-wide: `redirect/main.go:290`, `:317`, plus its own definition and doc
  comment). `package main` is not host-testable at all — `go test ./...`,
  `go build ./...` and `go vet ./...` fail by design with
  `wit_exports.go:934:6: missing function body` — so nothing about this function
  is covered today.
- **`linkgate` already has everything needed to host this logic and test it.**
  `KVStore` (the three-method interface `*kv.Store` satisfies) and `TimedStore`
  live in `linkgate/obs.go:107–124`; `linkgate/obs_test.go:11` already has a
  `fakeStore` with `getResult` **and `getErr`** fields, in-package, so the
  unavailable case is testable today with no new fake.
- **Two comments in the tree assert the behaviour this plan changes.**
  `redirect/linkgate/window.go:10` — "consistent with how a malformed KV record
  already fails closed elsewhere (ParseLink error -> lookupLink returns ok=false
  -> 404)"; and `api/links.py:93`, inside `UnreadableLinkError`'s docstring —
  "`redirect` treats an unparseable record as not-found and 404s
  (`lookupLink`)". Both become false and are in scope.
- **`setSecurityHeaders` survives `http.Error`.** It runs before
  `mux.ServeHTTP` (`main.go:26`) and `http.Error` only sets `Content-Type` and
  `X-Content-Type-Options` and deletes `Content-Length` — it never clears the
  header map. So a new 503 carries `Cache-Control: no-store`, `X-SS-Version`,
  HSTS, `X-Frame-Options` and the rest for free, exactly as the existing 404 and
  500 do. Confirmed by reading `main.go:76–103` against the stdlib's `http.Error`.
- **A non-404 error status on this path is not unprecedented.** Both handlers
  already answer `500` via `http.Error(w, "internal error", …)` when `kv.Open`
  fails (`main.go:286`, `main.go:313`). The "new status on the hot path"
  objection in the filed entry is therefore weaker than it reads.
- **`dev/click-load.sh` needs no change.** It counts any non-302 as a loss and
  prints the distinct codes it saw (`dev/click-load.sh:121–125`, `:161`), so a
  503 shows up there as a named code rather than as silent loss.
- **The `hey` harness traps are recorded and real** (TASKS.md, "Measurement traps
  learned the hard way this session"): `hey` follows redirects by default (use
  `-disable-redirects`, or you are measuring `example.com` refusing TLS
  handshakes and getting `NaN` latency), and it divides `-n` across `-c` by
  integer division (`-n 300 -c 80` issues 240). Both bit this repo already.

## The decision

**Do all three of the filed options' *useful* halves, and neither of its two
retry-shaped ones.** Concretely:

1. Thread an explicit disposition out of the lookup — as the filed entry
   suspected, this is the prerequisite, and it is where the testability comes
   from.
2. Answer a **failed read** with `503` + `Retry-After`.
3. Answer an **unparseable record** with `500` — distinct from the 503, so an
   operator can tell "capacity, will pass" from "corrupt data, a human is
   needed".
4. Leave **absent, disabled and out-of-window** on a bare, identical `404`.
5. **No retry anywhere in `redirect`.**

The organising principle, which is what makes the mapping memorable and is worth
quoting in CLAUDE.md verbatim:

> **404 is for product states — absent, disabled, out-of-window. 5xx is for
> faults — 503 transient, 500 permanent-data. A fault must never be dressed up
> as a product state.**

### The status contract, condition by condition

| condition | today | after | leaks about the link |
|---|---|---|---|
| active, in window, no password | 302 | **302** (unchanged) | destination |
| active, in window, password set | 200 prompt | **200 prompt** (unchanged) | that it is protected (already disclosed) |
| key absent | 404 | **404** | nothing |
| `status != "active"` | 404 | **404** | nothing |
| outside `[start_at, end_at)` | 404 | **404** | nothing |
| non-empty but unparseable `start_at`/`end_at` | 404 | **404** | nothing |
| **`Get` returned an error** | 404 | **503** + `Retry-After: 2` | **nothing — see below** |
| **`kv.Open` returned an error** | 500 | **503** + `Retry-After: 2` (task 3) | nothing |
| **record present, `ParseLink` failed** | 404 | **500** | that the slug has a record |

**Probing resistance holds, and the 503 case is the strongest of the lot.** When
the read fails, the server does not *know* whether the link exists — the 503 is
emitted from a state of ignorance, so it cannot leak a fact the process never
learned. Absent, disabled and out-of-window remain byte-identical `http.NotFound`
responses, and a test pins that they produce the *same* disposition value rather
than merely three values that each happen to map to 404.

**The one accepted new disclosure is the 500.** It reveals that a slug has a
record which will not parse. Accepted because: it is not attacker-controllable
(only `api` writes link records, and it always writes valid JSON — a corrupt
record comes from a KV-explorer accident or store damage); slugs are already
treated as non-secret (CLAUDE.md, "Security tradeoffs"); and the same fact is
already exposed on six authenticated `api` paths as `422 link_record_unreadable`
plus `unreadable_value` in the consistency report. A second, weaker disclosure:
503-vs-404 tells a prober that the store is saturated. Accepted — throughput and
latency already tell them that, and 503 is the correct, standard answer.

**Caching.** `Cache-Control: no-store` continues to be set once in
`setSecurityHeaders` and therefore covers both new statuses with no new code. The
direction of travel is favourable rather than merely neutral: today's wrong
answer is a **404, which Akamai's Property Manager docs list as cached for 10
seconds by default**, while 500 and 503 are documented as *not* cached unless the
"Cache HTTP Error Responses" behaviour is enabled. No redirect status changes, so
the 302-not-301/308 requirement is untouched.

**Hot-path cost: zero.** `Resolve` performs exactly one `Get` on
`linkgate.LinkKey(slug)`, the same single data operation `lookupLink` performs
today. A successful redirect stays at **5 KV operations (2 `open`, 2 `get`, 1
`set`)** and a miss stays at **2**. The added work on the success path is one
`switch` on an `int` and no allocation; `Resolve` returns `Link` by value exactly
as `lookupLink` did. The off path (no collector) still hands the raw `*kv.Store`
straight through with no wrapper, unchanged.

## Redirect (Go) changes — `linkgate`

New file **`redirect/linkgate/resolve.go`**. This is where the logic goes because
`package main` is not host-testable and `linkgate` imports nothing from
`spin-go-sdk` (CLAUDE.md, Tests). It needs no new imports: `time` and
`IsWithinWindow` are already in the package, and `KVStore` is already declared in
`obs.go`.

```go
// Disposition is what a /r/{slug} handler must do about one request.
//
// The zero value is DispositionUnavailable, deliberately: this whole type
// exists because a fault was being reported as "no such link", so an
// unset or unhandled disposition must fail towards "the server has a
// problem", never towards a claim about the link.
type Disposition int

const (
	DispositionUnavailable Disposition = iota // KV read failed; nothing is known about the link
	DispositionRedirect                       // active, in window, no password
	DispositionPrompt                         // active, in window, password required
	DispositionNotFound                       // absent, disabled, or outside its window
	DispositionUnreadable                     // record present, will not parse
)

func (d Disposition) String() string

// Resolve performs the single KV read for slug and decides the disposition.
// Exactly ONE data operation, the same one lookupLink performed.
func Resolve(store KVStore, slug string, now time.Time) (Link, Disposition)
```

`Resolve`'s order of decisions, which is load-bearing:

1. `raw, err := store.Get(LinkKey(slug))`; `err != nil` → `Link{}, DispositionUnavailable`.
   **Every** error, no variant check, no string match — see the facts section.
2. `len(raw) == 0` → `Link{}, DispositionNotFound`. (A stored record is always
   JSON and can never be zero-length; the SDK returns `[]byte("")` for a missing
   key. Keep this check explicit rather than letting `json.Unmarshal` fail, so
   absence stays a stated condition — the existing `lookupLink` comment makes
   this same point and it should survive the move.)
3. `ParseLink` error → `Link{}, DispositionUnreadable`.
4. `l.Status != "active"` → `l, DispositionNotFound`.
5. `!IsWithinWindow(l.StartAt, l.EndAt, now)` → `l, DispositionNotFound`.
6. `l.PasswordHash != ""` → `l, DispositionPrompt`.
7. otherwise → `l, DispositionRedirect`.

Returning the `Link` alongside a `NotFound` in 4/5 is harmless and keeps the
signature uniform; callers must not read it, and no caller does.

New file **`redirect/linkgate/resolve_test.go`**, in-package, reusing the
existing `fakeStore` from `obs_test.go` (`getResult`, `getErr`). **Do not
duplicate or restructure `fakeStore`** — the obs tests construct it with keyed
literals, so adding a field is safe if a key-capturing case needs one, but a
second fake type in the same package is not. Required cases:

- `getErr` set → `DispositionUnavailable`, and the returned `Link` is the zero value.
- `getResult` empty → `DispositionNotFound`.
- `getResult` = `[]byte("not json")` → `DispositionUnreadable`.
- active record, no `password_hash` → `DispositionRedirect`, `TargetURL` intact.
- active record with `password_hash` → `DispositionPrompt`.
- `"status": "disabled"` → `DispositionNotFound`.
- `start_at` in the future → `DispositionNotFound`; `end_at` in the past →
  `DispositionNotFound`; a non-empty unparseable `start_at` →
  `DispositionNotFound` (pins `IsWithinWindow`'s existing fail-closed behaviour
  through the new seam).
- **The probing-resistance pin.** A table test asserting the absent, disabled and
  out-of-window cases produce values that are **equal to one another**, not
  merely each equal to `DispositionNotFound`. That is the guard against a future
  change teasing them apart for a "better error message".
- **The zero-value pin.** `var d Disposition; d == DispositionUnavailable` — so a
  reordering of the `iota` block cannot silently make the fail-safe direction
  "claim the link is absent".
- A case asserting the key read is exactly `LinkKey(slug)` (`links:slug:<slug>`),
  via a fake that captures the key it was handed. A wrong key would 404 every
  link in the store.

**Mutation verification the builder must actually run and report:** re-introduce
the old collapse (`if err != nil || len(raw) == 0 { return Link{}, DispositionNotFound }`)
and confirm the unavailable test fails and **only** it fails; then restore. This
is the repo's established practice for a guard whose whole value is that it fires
(see the `ShardFor` and `positional zip` mutation checks).

## Redirect (Go) changes — `package main`

**`redirect/main.go`**:

- Delete `lookupLink` (lines 471–509 including its doc comment). **Move the
  parts of that comment that are still true onto `Resolve`** — specifically the
  "deliberately ONE KV data operation", the `[]byte("")`-means-absent
  explanation, and the measured 13.5% saving from removing the `Exists` probe
  (2026-08-06). That comment is the only written record of why the probe is gone,
  and the same mistake this plan corrects elsewhere in the repo (a stale comment
  outliving its code and then being cited as justification) is exactly what
  losing it would set up.
- Add one small helper:

```go
// retryAfterSeconds is what a 503 tells a client to wait. Modelled, not
// measured: the read cap is a per-second window, so 1 is the minimum honest
// value, but every throttled client retrying at exactly +1s re-collides on
// the moment the window resets. 2 gives one clear window. A plain constant,
// tunable — but change it with evidence, not a hunch, per this repo's rule
// for every sibling constant.
const retryAfterSeconds = "2"

// serviceUnavailable answers a request the store could not serve. It makes NO
// claim about the link, because when a read fails the handler does not know
// whether the link exists. Cache-Control: no-store arrives from
// setSecurityHeaders; Retry-After must be set before http.Error writes the
// header. Akamai does not cache 503 by default (Property Manager docs) — this
// is deliberately not on the by-default-cached list that 404 is on.
func serviceUnavailable(w http.ResponseWriter) {
	w.Header().Set("Retry-After", retryAfterSeconds)
	http.Error(w, "temporarily unavailable", http.StatusServiceUnavailable)
}
```

- `handleRedirectGet` replaces its `lookupLink` + combined `!ok || Status ||
  IsWithinWindow` branch with a switch:

```go
l, disp := linkgate.Resolve(store, slug, time.Now())
switch disp {
case linkgate.DispositionRedirect:
	sendRedirectThenRecord(w, slug, l.TargetURL, collector)
case linkgate.DispositionPrompt:
	renderPasswordPrompt(w, http.StatusOK, slug, "")
case linkgate.DispositionNotFound:
	http.NotFound(w, r)
case linkgate.DispositionUnreadable:
	http.Error(w, "internal error", http.StatusInternalServerError)
default: // DispositionUnavailable, and the zero value: fail towards the server's fault
	serviceUnavailable(w)
}
```

- `handleRedirectPost` takes the same switch, differing only in the `Prompt`
  case, which keeps today's behaviour exactly: `r.ParseForm()` →
  `renderPasswordPrompt(w, http.StatusBadRequest, …)` on error;
  `linkgate.VerifyPassword(r.FormValue("password"), l.PasswordHash)` →
  `renderPasswordPrompt(w, http.StatusUnauthorized, …)` on failure; otherwise
  `sendRedirectThenRecord`. Note that today's POST fast path
  (`if l.PasswordHash == "" { redirect }`) is exactly
  `DispositionRedirect`, so no behaviour changes.

**Both handlers must be edited in the same commit and kept structurally
identical.** They are already duplicated today and that duplication is how GET
and POST would drift; a reviewer's check is that the two switches differ only in
the `Prompt` arm.

- **Task 3, separable and independently revertable:** replace both
  `http.Error(w, "internal error", http.StatusInternalServerError)` calls on the
  `kv.Open` failure path (`main.go:286`, `:313`) with `serviceUnavailable(w)`.
  Rationale: an `Open` failure is the same root cause as a failed `Get` — the
  store is not available — and answering the same cause with two different
  statuses is the inconsistency this plan is otherwise removing. It also leaves
  `500` meaning exactly one thing in this component: *a record is present and
  will not parse*. It is a separate task because it changes **existing**
  behaviour rather than fixing a bug, so the user can decline it without
  affecting anything else.

  **Accepted imprecision, recorded rather than fixed:** `kv.Open` can also
  fail *permanently* — a manifest misconfiguration, which is exactly how this
  task's own verification step reproduces the failure (removing
  `key_value_stores` from `[component.redirect]` in `spin.toml`) — in which
  case `Retry-After: 2` will occasionally invite retries of something that
  can never succeed. Accepted because a misconfigured store fails 100% of
  requests on every route and is discovered in seconds, so a wrong hint on a
  comprehensively broken deploy costs nothing.

**Not changed:** `sendRedirectThenRecord`'s own `openTimedStore` failure and
`recordClickCount`'s errors stay swallowed. Analytics is best-effort by design
and runs after the response — a bookkeeping failure must never affect what the
visitor got. Nothing in this plan alters the 5-op success profile or the
ordering.

## Observability changes: none, deliberately

**No new log field, and no new collector op type.** After this change the status
code *is* the signal, and it is already in the log line (`emitLogLine` emits
`status=`), in `hey`'s status distribution, in `curl -I`, and in
`dev/click-load.sh`'s "codes seen" output — visible with `log_level=off` and no
debug token, which is strictly better than a field that requires enabling
logging. This also, finally, makes the question the filed entry wanted to answer
answerable: a `log_level=summary` build can now grep for `status=503` and learn
whether real traffic ever hits saturation, which (see the facts section) it
provably could not do before, because a read failure's log line was
field-for-field identical to a genuine miss.

Two things must **not** happen here, both tempting:

- Do not add `err=1` or `kv_err=1` to the redirect's log line. It duplicates the
  status code, and CLAUDE.md's rule that the **off path stays byte-identical**
  makes every field a cost paid on the traced path for no new information.
- Do not record the error into the collector. `Collector.Record`'s signature has
  no parameter that could carry one, and that structural absence (the same move
  `PrefixedStore` makes by having no `get_keys`) is the thing keeping session
  tokens out of a 7-day log retention window. Leave it alone.

## Documentation changes

- **`CLAUDE.md`**, "Redirect caching: `Cache-Control: no-store` and the
  302-not-301/308 requirement": add the status contract table and the organising
  principle sentence, plus the two cited Akamai facts (404 cached 10 s by
  default; 500/502/503/504 not cached unless the behaviour is enabled). Keep the
  existing 302 paragraphs exactly as they are — nothing about redirects changed.
- **`CLAUDE.md`**, "Security tradeoffs", the slug-enumerability bullet: state
  which conditions stay indistinguishable (absent / disabled / out-of-window) and
  the two accepted new disclosures (the 500 reveals a record exists; the 503
  reveals saturation).
- **`CLAUDE.md`**, "Toggleable structured logging": correct the record that the
  log line distinguishes a read failure from a miss — it did not, and now the
  status code does. The 5-op / 2-op profile statement is unchanged and must stay.
- **`redirect/linkgate/window.go:10`**: the comment's "(ParseLink error ->
  lookupLink returns ok=false -> 404)" is now false in two ways. The
  out-of-window fail-closed behaviour it is explaining is unchanged; only the
  cited precedent moves (`ParseLink` error → `DispositionUnreadable` → 500).
- **`api/links.py:93`**, inside `UnreadableLinkError`'s docstring: "`redirect`
  treats an unparseable record as not-found and 404s (`lookupLink`)" becomes
  false. Worth noting *why* this is a correction rather than a contradiction:
  that docstring rejected 500 because it "tells an operator to retry a transient
  fault when it is a permanent data fault." That objection was correct in a world
  where 500 was the only error status available. Once 503 owns "transient,
  retry", 500 means "permanent fault" by contrast, and the objection dissolves.
  The API keeps its 422 — it has a JSON client that can be told something
  specific; a browser navigation does not.

## Tooling: `dev/redirect-load.sh`

New file. The deployed verification is the whole proof of this fix and it depends
on a harness that has already produced two confident wrong answers in this repo.
A script institutionalises the traps the way `dev/click-load.sh` institutionalises
the write cap:

- Wraps `hey`, always passing `-disable-redirects`.
- Takes `-c` and a per-worker count `k`, and computes `-n = c × k` itself, so
  integer-division truncation is impossible.
- Prints `hey`'s status-code distribution verbatim.
- Prints the implied **read** rate (`achieved_rps × 2`) against the documented
  1,000 reads/second cap and warns when it is exceeded — the read-side analogue
  of `click-load.sh`'s 50-writes/second warning.
- **Exits non-zero if any `404` appears in the distribution**, which is exactly
  the regression this plan fixes, and prints the 503 count as informational
  rather than as a failure.
- Refuses to run if `hey` is not on `PATH`, with the install hint.

## Trade-offs and rejected alternatives

**1. A bounded retry of the `Get` inside `redirect`. Rejected.**
Attractive because it is the only option that keeps the link *working* rather
than merely honest, and because the repo already built a retry seam for writes
(`api/kvretry.py`), so it looks like a consistent move. It loses on the
mechanism. The write case retries into *headroom*: a throttled write defers into
a later 50/s window that is usually idle. This failure is **read-cap saturation
of a 1,000/s app-wide budget** — at the measured 947 rps, 31% of requests are
already failing, so retrying spends *more of the exact resource that is
exhausted*, converting a partial failure into a retry storm and making every
concurrent request worse, including the ones that would have succeeded. It also
adds latency to the one path this repo protects hardest (CLAUDE.md,
"Write-throttle resilience" Trade-offs #2 excluded `redirect` from the retry seam
for a weaker version of this reason), any sleep long enough to outlast a
per-second cap window is far longer than a visitor will wait, and whether a sleep
is even available in this component is UNCONFIRMED. The correct place to put a
retry decision for a saturated origin is **the client**, which is precisely what
`503` + `Retry-After` does, at zero origin cost. If retry is ever revisited, the
prerequisite is a measurement that the read cap has headroom at the moment of
failure — and the 690/726/947/1292 rps sweep says it does not.

**2. Serving a remembered redirect from in-instance memory when the read fails.
Rejected.** The most attractive option on the list, because it is the only one
where the visitor still reaches the destination: cache `slug → target` in
process, use it *only* when the read fails. It loses to CLAUDE.md's caching
section, which is emphatic that resolution must re-read KV on every request —
`status`, the `[start_at, end_at)` window, deletion, repointing and the
destination-policy remediation path are each correct *only* if no layer serves a
remembered answer. "Only on failure" does not soften that; it means the app
serves a remembered answer at precisely the moment it cannot verify it, so a
deleted or disabled link keeps redirecting for as long as the store is saturated
— and saturation is exactly when an operator most needs a kill switch to work.
Wasm instance lifetime on Akamai is also unknown and unpredictable, so the
cache's hit rate and staleness window are both unknowable. If "keep redirecting
through saturation" ever becomes a real requirement, it must be a deliberate
product decision about serving stale redirects with a bounded TTL and an
explicit list of the guarantees being traded away — not a fallback smuggled in
behind an error branch.

**3. Keeping the 404 for an unparseable record (case c). Rejected, and it was
close.** Three real arguments for it: it preserves probing resistance perfectly;
a 5xx invites a visitor and a crawler to retry a *permanently* broken link
forever, where a 404 lets a search engine deindex it; and the operator already
has three other surfaces for a corrupt record (`422 link_record_unreadable` on
six `api` paths, `unreadable_value` in the consistency report, and the record
being skipped from `handle_list`), so the marginal value of a fourth is small.
It loses on the principle that the rest of this plan rests on: 404 means "we read
the store and there is no link available to you", which covers absent, disabled
and out-of-window — all **intentional product states**. A corrupt record is a
**fault**, and answering a fault with a product state is the same lie, in
miniature, that this plan exists to remove. A campaign URL with a damaged record
exists; telling its visitors it does not is wrong for the same reason case (a)
was wrong. Revisit if a deployment ever sees corrupt records at a rate where the
5xx noise costs more than the honesty buys — which would itself be the signal
that something upstream needs fixing.

**4. `500` for the read failure instead of `503`. Rejected.** It is the smallest
possible diff (both handlers already emit 500 on the `kv.Open` path, so the
status is already precedented and the "new status on the hot path" objection
disappears entirely). Rejected because it throws away the only thing this fix can
give an operator for free: the distinction between "capacity, this will pass" and
"data is damaged, a human is needed". `500` also carries no `Retry-After`
semantics, so a well-behaved crawler or link-checker learns nothing actionable.
Caching is *not* the discriminator here — per Akamai's docs neither 500 nor 503
is cached by default.

**5. `502 Bad Gateway` instead of `503`. Rejected.** There is no upstream gateway
in this architecture; the KV store is a host capability, not an origin behind
this component. 503 is the standard "the server is currently unable to handle the
request" and is the only one of the two with a defined `Retry-After` idiom.

**6. A styled, visitor-facing HTML error page for the 503. Rejected for now,
filed as Future work.** It would be better UX than `http.Error`'s plain text, and
the pattern exists (`prompt.html` is embedded, styled off the already-routed
`/vendor/pico.min.css` + `/theme.css`, and covered by
`gui-pages/tests/test_no_inline_code.py`). It loses on consistency and scope: the
*far* more common failure page on this route is the `404`, which is today a bare
`http.NotFound`, so styling only the 503 would produce the odd result that a
transient server hiccup looks polished while a dead link looks like a raw Go
error. If this is ever done, do both statuses in one pass, with a CSP and an
inline-code-guard entry each.

**7. Classifying `access denied` / `no such store` as permanent by matching the
SDK's error strings. Rejected.** Tempting because a permanent misconfiguration
answered with `503 Retry-After: 2` invites an infinite retry. Rejected on the
repo's standing rule that an error-message match must never be control flow (a
reword silently un-ships the behaviour), reinforced by the fact that those two
variants are `kv.Open` failures and cannot realistically reach `Get` at all.
Every `Get` error is treated as transient, full stop.

**8. Adding an `err=1`/`kv_err=1` field to the redirect's log line. Rejected.**
See "Observability changes" — the status code already carries it, on the off path
as well as the traced one.

**9. Do nothing; leave the entry filed. Rejected.** It was a live option: the bug
only appears above ~700 rps, which no real traffic on this app approaches, and
`Cache-Control: no-store` keeps the wrong answer from persisting. It loses
because the fix is small, costs the hot path nothing measurable, and the failure
mode is the worst-shaped one this app has — a **silent** lie about a customer's
live campaign URL, at exactly the moment of peak traffic, which is when a
campaign link is most valuable and when an operator is least able to tell a
capacity problem from a data problem. It is also the last known correctness
defect in the repo.

## Tasks

Appended to `TASKS.md` under `## Redirect read failures must not answer 404`:

```
- [ ] Add `linkgate.Disposition` and `linkgate.Resolve` splitting absent from unavailable from unreadable — file(s): redirect/linkgate/resolve.go, redirect/linkgate/resolve_test.go — done when: `cd redirect && go test ./linkgate/...` passes with cases for all five dispositions, one test asserts absent/disabled/out-of-window produce values equal to EACH OTHER, one pins that the zero value of `Disposition` is `DispositionUnavailable`, one pins the key read is `LinkKey(slug)`, and re-introducing the old `err != nil || len(raw) == 0` collapse fails the unavailable test and only that test (report the mutation result); `redirect/main.go` is untouched by this task
- [ ] Answer a failed link read with 503 + `Retry-After` and an unparseable record with 500 (needs the task above) — file(s): redirect/main.go — done when: `lookupLink` is deleted with its still-true comment text moved onto `Resolve`, both handlers switch on `linkgate.Resolve` and differ only in the `DispositionPrompt` arm, and against a local `spin up --build` a live slug returns 302, an absent/disabled/out-of-window slug returns 404, and a slug whose `links:slug:<slug>` value has been overwritten with `not json` returns 500 — every one of them still carrying `Cache-Control: no-store`
- [ ] Answer a failed `kv.Open` with 503 instead of 500, so one cause has one status (needs the task above; independently revertable) — file(s): redirect/main.go — done when: with `key_value_stores` temporarily removed from `[component.redirect]` in `spin.toml`, `/r/<any-slug>` returns `503` carrying `Retry-After: 2` and `Cache-Control: no-store`, and `git diff spin.toml` is empty again afterwards
- [ ] Correct the two comments that assert `redirect` 404s an unparseable record — file(s): redirect/linkgate/window.go, api/links.py — done when: neither file claims a `ParseLink`/unreadable record produces a 404, `api/links.py`'s `UnreadableLinkError` docstring records why 500 is now correct for `redirect` while 422 stays correct for the API, and `cd api && uv run pytest` still passes with no behaviour change
- [ ] Add `dev/redirect-load.sh` so the `hey` traps cannot recur — file(s): dev/redirect-load.sh — done when: it always passes `-disable-redirects`, computes `-n` as `c × k` itself, prints `hey`'s status distribution, prints the implied read rate (rps × 2) against the 1,000 reads/second cap with a warning above it, exits non-zero if any `404` appears in the distribution, and refuses to run with an install hint when `hey` is absent
- [ ] Record the `/r/{slug}` status contract in CLAUDE.md — file(s): CLAUDE.md — done when: the "Redirect caching" section carries the 404-is-product-states/5xx-is-faults principle and the per-condition table, cites that Akamai caches 404 for 10 s by default while 500/502/503/504 are not cached unless the behaviour is enabled, the "Security tradeoffs" enumerability bullet names what stays indistinguishable and the two accepted new disclosures, and the "Toggleable structured logging" section records that a read failure's log line was field-identical to a genuine miss and that the status code is now the signal
- [ ] End-to-end manual verification of the 503-under-saturation fix on a deployed build — file(s): (none — verification step) — done when: `dev/redirect-load.sh` at c=60, c=80 and c=90 against a live password-free `/r/{slug}` returns ZERO 404s at every concurrency (was 0%/31%/43%), 503s appear instead at the higher two, the slug returns 302 at rest immediately afterwards, a header capture taken during saturation shows a real 503 carrying `retry-after: 2` and `cache-control: no-store` with no `age`/`x-cache` header and distinct `akamai-grn` values, and a repeat of the c=60 baseline afterwards is still clean at ~690 rps
```

## Critical files

- `redirect/linkgate/resolve.go` (new)
- `redirect/linkgate/resolve_test.go` (new)
- `redirect/main.go`
- `redirect/linkgate/window.go`
- `api/links.py`
- `dev/redirect-load.sh` (new)
- `CLAUDE.md`
- `TASKS.md` (checkboxes only)

## Verification

In execution order.

1. **Unit tests, and the mutation check that gives them their value.**
   ```bash
   cd redirect && go test ./linkgate/...
   ```
   Then re-introduce `if err != nil || len(raw) == 0 { return Link{}, DispositionNotFound }`
   in `Resolve`, re-run, confirm the unavailable test fails and **only** it, and
   restore the file. Report the failing test's name. **Never run
   `go test ./...`, `go build ./...` or `go vet ./...`** — they fail by design on
   `package main` with `wit_exports.go:934:6: missing function body`.

2. **The touched Python file's suite** (the docstring correction):
   ```bash
   cd api && uv run pytest
   ```

3. **The manifest guard**, only if step 6's temporary `spin.toml` edit was made,
   as the revert check:
   ```bash
   cd gui-pages && uv run pytest
   git diff --stat spin.toml   # must be empty
   ```

4. **Local behaviour, all five conditions, without needing auth.** Run the app
   with the KV explorer so records can be written directly:
   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> \
   SPIN_VARIABLE_KV_EXPLORER_PASSWORD=<kvpw> \
   SPIN_VARIABLE_COOKIE_SECURE=false \
     ./dev/kv-explorer-up.sh
   ```
   Through `http://localhost:3000/internal/kv-explorer/` (basic auth user `kv`),
   add four keys in the `default` store — this is the same technique the
   2026-08-17 unreadable-record work used to produce a corrupt record:
   - `links:slug:t302` → a valid record, `"status":"active"`, `"password_hash":null`,
     `"start_at":null`, `"end_at":null`, `"target_url":"https://example.com"`
   - `links:slug:tdisab` → the same with `"status":"disabled"`
   - `links:slug:twindo` → the same with `"end_at":"2020-01-01T00:00:00Z"`
   - `links:slug:tcorru` → the literal bytes `not json`

   Then:
   ```bash
   for s in t302 tdisab twindo tcorru tabsent; do
     printf '%s -> ' "$s"
     curl -s -o /dev/null -w '%{http_code}\n' "http://localhost:3000/r/$s"
   done
   ```
   **Pass:** `t302 -> 302`, `tdisab -> 404`, `twindo -> 404`,
   `tcorru -> 500`, `tabsent -> 404`. Then confirm the headers survive on the
   two error paths:
   ```bash
   curl -sD - -o /dev/null http://localhost:3000/r/tcorru | grep -iE '^(HTTP|cache-control|x-ss-version)'
   curl -sD - -o /dev/null http://localhost:3000/r/tabsent | grep -iE '^(HTTP|cache-control)'
   ```
   **Pass:** `cache-control: no-store` on both.

5. **The password path is undisturbed.** With a real password-protected link
   (create one through the dashboard, or write a record whose `password_hash` is
   copied from an existing protected link): `GET /r/{slug}` renders the prompt
   with `200`, a wrong password returns `401` with the prompt, a right password
   returns `302` with the correct `Location`. Do this **in a browser**, not only
   with `curl` — CLAUDE.md records that `form-action` once broke every protected
   link in Chrome while working perfectly under `curl`.

6. **The 503 path locally (verifies task 3, the only local way to force a KV
   failure).** Temporarily remove the `key_value_stores = ["default"]` line from
   `[component.redirect]` in `spin.toml`, restart, then:
   ```bash
   curl -sD - -o /dev/null http://localhost:3000/r/t302 | grep -iE '^(HTTP|retry-after|cache-control)'
   ```
   **Pass:** `503`, `retry-after: 2`, `cache-control: no-store`. **Then
   `git checkout spin.toml` and re-run step 3.** Do not leave this edit on disk.

7. **Deployed verification (the user's call to deploy; this is what to run once
   a build is live).** Deploy per TASKS.md's "Deploying" block with
   `app_version=<sha>-read503`, poll
   `curl -sI "$APP_URL/" | grep -i x-ss-version` in a loop until it flips (100–110 s
   is normal; the CLI's `failed to wait for deployment to go live` is a false
   negative — do not redeploy).

   Pick a live, active, password-free slug, then:
   ```bash
   ./dev/redirect-load.sh -u "$APP_URL/r/$SLUG" -c 60 -k 10   # n = 600
   ./dev/redirect-load.sh -u "$APP_URL/r/$SLUG" -c 80 -k 10   # n = 800
   ./dev/redirect-load.sh -u "$APP_URL/r/$SLUG" -c 90 -k 10   # n = 900
   curl -s -o /dev/null -w 'at rest: %{http_code}\n' "$APP_URL/r/$SLUG"
   ```
   **Pass:** **zero 404 responses at every concurrency** (the same sweep measured
   0% / 31% / 43% wrongly-404 before this change), 503s appearing at c=80 and
   c=90 in roughly the proportion the 404s used to, and `at rest: 302`
   immediately afterwards.

   Then capture a real 503's headers during saturation, the same way the
   302/404 caching behaviour was confirmed on 2026-08-06:
   ```bash
   ./dev/redirect-load.sh -u "$APP_URL/r/$SLUG" -c 90 -k 30 >/tmp/hey.txt 2>&1 &
   for i in $(seq 1 40); do
     curl -sD - -o /dev/null "$APP_URL/r/$SLUG" \
       | grep -iE '^(HTTP|retry-after|cache-control|age|x-cache|akamai-grn)'
     echo --
   done
   wait
   ```
   **Pass:** at least one `503` block carrying `retry-after: 2` and
   `cache-control: no-store`, with **no `age` and no `x-cache` header** and
   **distinct `akamai-grn` values** across repeats. If the 503 arrives without
   our headers, Akamai replaced the response with its own error page — the fix
   still holds (it is not a 404) but record it, because `Retry-After` is then not
   reaching clients.

   Finally, re-run the clean baseline to check a 5xx burst did not trip anything
   at the edge:
   ```bash
   ./dev/redirect-load.sh -u "$APP_URL/r/$SLUG" -c 60 -k 10
   ```
   **Pass:** still ~690 rps, still 100% 302.

## Out of scope / follow-ups

- **The 1,000 reads/second cap itself, and reducing the redirect's 2 reads per
  click.** Explicitly out of scope by instruction. This fix makes saturation
  *honest*, not *rarer*. Filed as Future work, with the note that TASKS.md's
  existing "Ask Akamai for a KV write-rate increase" entry says "Reads (3 per
  click) are nowhere near the 1,000 RPS read cap" — falsified by the 690-rps
  measurement, and the correction belongs where someone will read it.
- **A styled visitor-facing error page for `/r/{slug}`.** Filed as Future work
  (see rejected alternative #6), covering the 404 and the 503 together, never
  just one.
- **Retry inside `redirect`.** Rejected outright, not deferred — recorded under
  TASKS.md's "Considered and rejected" with the measurement that kills it.
- **`dev/click-load.sh`.** Needs no change: it already prints the distinct
  non-302 codes it saw, so a 503 becomes visible loss-with-a-reason rather than
  silent loss. Worth knowing when reading an old run: a click-loss figure from
  before this change could have contained wrongly-404'd requests.
- **`Jenkinsfile`.** Unchanged — this adds Go tests to the existing
  `go test ./linkgate/...` stage and changes no test invocation.
