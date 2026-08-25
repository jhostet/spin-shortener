# Disposition-Unreadable Logging

## Context

A request to `/r/{slug}` whose stored record exists but will not parse
(`linkgate.Resolve` → `DispositionUnreadable`) answers a styled `500` and
**logs nothing at all** — not the gated summary line, and not the unconditional
`ev=kv_fail` failure line that `docs/plans/observable-kv-failures.md` built
specifically to catch faults nobody can reproduce on demand. `redirect/main.go`
calls `emitFailureLine` from exactly two arms, both KV faults (`open` failing,
and `Resolve` returning `DispositionUnavailable`); the unreadable arm is one
statement, `internalError(w)`, and the comment above `maxFailureDedupPairs`
names `DispositionUnreadable` as a case that deliberately emits nothing.

That silence was a documented decision, not an oversight — `observable-kv-failures`
scoped itself to KV *faults*, and a decoder failure is not one — and it is filed
in `TASKS.md`'s `## Future work (not scheduled)` as **"`DispositionUnreadable`
produces no log line, so a `500` on `/r/{slug}` is invisible to the operator"**
(raised 2026-08-23 while planning `docs/plans/redirect-error-pages.md`). That
entry sets a trigger: *"a `500` actually observed on `/r/{slug}` in real traffic,
or an `unreadable_value` finding appearing in a consistency report nobody
expected."* **The trigger has not fired. The user has asked for this to be built
anyway, and a direct instruction outranks the trigger-gating convention.** The
entry is annotated as picked up rather than silently left open.

Two facts make the current state worse than "one missing log line":

1. **A corrupt record is permanent until a human acts.** Unlike a throttled KV
   read, which is transient and self-heals, a record that will not parse keeps
   500ing every single click on that link until someone deletes or rewrites it.
   The only existing way to learn about it is `GET /api/admin/consistency`'s
   `unreadable_value` finding, which nobody runs unprompted. The new `500` page's
   copy tells the visitor to report it to whoever shared the link precisely
   because the visitor is currently a better signal than the logs.
2. **Nothing anywhere in the application records *why* a record will not parse.**
   Confirmed by reading `api/consistency.py`: the `unreadable_value` finding is
   built from `unreadable.append({"store": ..., "key": ...})` (lines 203, 212,
   240, 254) and carries no reason, message or exception. `api/links.py`'s
   `UnreadableLinkError` carries the slug. `linkgate.Resolve` receives a real
   `json.Unmarshal` error and **discards it** before any caller sees it. So the
   distinction between a truncated write (`unexpected end of JSON input`) and a
   schema mismatch between what `api` writes and what `linkgate.Link` expects
   (`json: cannot unmarshal number into Go struct field Link.status of type
   string`) exists nowhere in the product, even though those are completely
   different root causes with completely different fixes.

**Confirmed decisions (settled by the user before planning):**

- Build it now, despite the Future-work trigger not having fired.
- Mark the existing Future-work entry as picked up, in that section's existing
  annotation style.
- `gui-pages` instrumentation (the next Future-work entry down) is **not** in
  scope — different component, different problem.
- Do not touch the `open`/`get`/`DispositionUnavailable` failure-line paths
  beyond what is needed to share code cleanly with the new case.

## Key technical facts confirmed during research

- **`Resolve` discards the parse error today.** `redirect/linkgate/resolve.go`
  line 106-109: `l, err := ParseLink(raw); if err != nil { return Link{},
  DispositionUnreadable, nil }` — the `err` is shadowed and dropped on the
  floor. Confirmed by reading the file.
- **The nil-for-unreadable contract is documented AND pinned by a test.**
  `resolve.go`'s doc comment: *"The returned error is non-nil ONLY alongside
  DispositionUnavailable … there is no unknown host message to capture for a
  genuinely absent key, an unparseable record or a business-rule mismatch."*
  `redirect/linkgate/resolve_test.go`'s
  `TestResolve_ErrorIsNilForEveryOtherDisposition` (line 48) iterates a map
  containing `DispositionUnreadable: fakeStore{getResult: []byte("not json")}`
  and asserts `err == nil` for each. **That test must be narrowed by this work;**
  it is the single mechanical guard on the invariant being changed.
- **The rationale in that doc comment is wrong about the record case.**
  "No unknown host message to capture" is true for an absent key and for a
  business-rule mismatch (nothing failed), and false for an unparseable record:
  `encoding/json` produced a real, specific error. The plan rewrites that
  paragraph rather than working around it.
- **Neither handler reads `resolveErr` outside the `default:` (Unavailable)
  arm.** `redirect/main.go` lines 322-337 and 354-377: both handlers `switch
  disp` first and only touch `resolveErr` inside `default:`. So a non-nil error
  arriving with `DispositionUnreadable` cannot leak into the `ev=kv_fail` path
  — the switch is the discriminator, not the error's nil-ness. Confirmed by
  reading both handlers.
- **The dedup key is `op + "\x00" + msg`.** `redirect/main.go`'s
  `shouldEmitFailureLine` (line 536). With a fixed or shared message, every
  corrupt slug in the app collapses onto one dedup entry and only the **first**
  one is ever logged for the Wasm instance's life — the exact hazard the
  Future-work entry names.
- **`redirect`'s per-instance dedup is close to a no-op on Akamai, measured.**
  `TASKS.md`, `### DEPLOYED AND ANSWERED (2026-08-21)`: six concurrent `api`
  bulk creates produced one line each, *"while the redirect runs produced
  hundreds — confirming that `redirect`'s per-instance dedup is close to a
  no-op under a one-instance-per-request regime, exactly as its own comment
  predicts, and that the 32-pair cap is what actually bounds volume there."*
  So the dedup fix matters most for a long-lived instance (local `spin up`,
  and any future host that reuses instances), and the shared 32-entry cap is
  what bounds stderr volume in production.
- **`Err`-message sanitization already exists and is the right tool here.**
  `linkgate.SanitizeErrorMessage` (`redirect/linkgate/obs.go` line 273) applies
  key-shaped redaction → `[key:<word>]`, `pbkdf2_sha256` → `[hash]`,
  control-character scrubbing, then truncation at `MaxErrorMessageChars` (200),
  returning `(sanitized, redacted, truncated)`.
- **A typical `json` error survives sanitization intact.** `keyShapedPattern`
  is `[A-Za-z][A-Za-z0-9_-]*:[^\s'")\]]+`, whose trailing class requires at
  least one non-whitespace character immediately after the colon — so the
  `json: ` prefix of an `UnmarshalTypeError` message does **not** match. This is
  the same property `TestSanitizeErrorMessage_LeavesKeyValueErrorColonSpaceIntact`
  already pins for `key-value error: internal server error`.
- **How much record content a `json` error can echo, read from the stdlib
  rather than assumed:** `SyntaxError` messages echo at most a single offending
  byte (`invalid character 'o' in literal null (expecting 'u')`);
  `UnmarshalTypeError.Error()` is `"json: cannot unmarshal " + Value + " into Go
  struct field " + Struct + "." + Field + " of type " + Type`, where `Value` is a
  *kind* ("string", "bool", "object") except for numbers, where the decoder sets
  `Value: "number " + literal` and the literal itself appears. Every `Link` field
  is a `string` or `bool`, so a stored PBKDF2 hash can only ever be a JSON string
  that parses fine; the only way a value reaches the message is as a bare numeric
  literal. `SanitizeErrorMessage`'s `hashTokenPattern` covers the residual case
  by construction regardless.
- **`%T` on an unwrapped decoder error yields the concrete type.**
  `fmt.Sprintf("%T", err)` gives `*json.SyntaxError` or `*json.UnmarshalTypeError`
  — a wording-independent classification, obtained with no hand-maintained
  string table of the kind `classifyKVFailure` needs (that table exists only
  because `spin-go-sdk` flattens the WIT error variant into fixed English
  strings). **This only holds while the error stays unwrapped** — wrapping it in
  a sentinel would make `%T` report the wrapper. That is one of the two reasons
  the sentinel alternative is rejected below.
- **A slug reaching the unreadable arm is necessarily one `api` wrote.**
  `DispositionUnreadable` requires a non-empty value at `links:slug:<slug>`, and
  only `api` writes link records, always validated against
  `CUSTOM_SLUG_PATTERN` (`^[A-Za-z0-9_-]{3,32}$`) or auto-generated. So the slug
  on this line cannot carry a space or a newline and cannot break the logfmt
  line. (**UNCONFIRMED, and out of scope:** the *existing* `op=get` `ev=kv_fail`
  line logs `r.PathValue("slug")`, which is the unescaped path segment for an
  arbitrary requested slug that need not exist — reading `net/http`'s `ServeMux`
  wildcard behaviour, `/r/a%20b` would yield `a b`. Filed as follow-up, not
  fixed here; confirming it needs a live request against a `spin up` build with
  an induced `op=get` failure.)
- **Baseline is green.** `cd redirect && go test ./linkgate/...` → `ok
  github.com/redirect/linkgate` (2026-08-25). `go test ./...` still fails by
  design on `package main` and is never run.

## Decision 1 — `Resolve` returns the parse error

**Chosen: change the contract.** `Resolve` returns the exact `ParseLink` error
alongside `DispositionUnreadable`, unmodified and unwrapped, mirroring what it
already does for `store.Get`'s error alongside `DispositionUnavailable`.

The new contract is *"the error is non-nil for exactly two dispositions, and its
meaning is determined by the disposition"* — which is safe because both callers
switch on the disposition first and read the error only inside an arm they have
already identified. It is not safe to state as *"non-nil means the store
failed"*, and the doc comment must say so explicitly rather than leaving the
next reader to infer it.

### `redirect/linkgate/resolve.go`

The body change is two tokens:

```go
	l, err := ParseLink(raw)
	if err != nil {
		return Link{}, DispositionUnreadable, err
	}
```

The final paragraph of `Resolve`'s doc comment (currently lines 90-96, beginning
"The returned error is non-nil ONLY alongside DispositionUnavailable") is
**replaced in full** with:

```go
// The returned error is non-nil for exactly TWO dispositions, and its meaning
// differs between them — so a caller must switch on the disposition FIRST and
// read the error only inside an arm it has already identified. Both handlers
// in main.go structurally do that; "err != nil" alone must never be read as
// "the store failed".
//
//   - DispositionUnavailable — exactly the error store.Get produced,
//     unmodified and unwrapped (docs/plans/observable-kv-failures.md), so a
//     caller can log the host's own message.
//   - DispositionUnreadable — exactly the error ParseLink produced, unmodified
//     and unwrapped (docs/plans/disposition-unreadable-logging.md). Do NOT wrap
//     it: unwrapped, fmt's %T yields the concrete decoder type, which
//     distinguishes a truncated write (*json.SyntaxError) from a schema
//     mismatch between what api writes and what Link expects
//     (*json.UnmarshalTypeError) with no string matching at all.
//
// DispositionNotFound, DispositionRedirect and DispositionPrompt always return
// a nil error: nothing failed, so there is no message to capture, and a
// non-nil error alongside one of those would invite a caller to log it as if
// it meant something it doesn't.
//
// This REPLACES the narrower rule this comment used to state ("non-nil ONLY
// alongside DispositionUnavailable"), whose stated reason — that there is "no
// unknown host message to capture for ... an unparseable record" — was simply
// wrong about the record case. json.Unmarshal's error was always real and
// always diagnostically useful; Resolve was throwing it away. It is also the
// ONLY place in the application that says why a record will not parse:
// api/consistency.py's unreadable_value finding carries {store, key} and no
// reason at all.
```

### `redirect/linkgate/resolve_test.go`

- **Narrow** `TestResolve_ErrorIsNilForEveryOtherDisposition`: remove the
  `DispositionUnreadable` entry from its `cases` map (leaving `NotFound`,
  `Redirect`, `Prompt`), rename it to
  `TestResolve_ErrorIsNilForNotFoundRedirectAndPrompt`, and rewrite its doc
  comment to state the *new* half-contract it pins ("nothing failed, so nothing
  to report"), not the old one.
- **Add** `TestResolve_UnreadableReturnsExactlyTheParseError`: read the same
  bytes through `ParseLink` directly and assert `Resolve`'s error is non-nil and
  `err.Error()` equals it, so the assertion is "exactly what ParseLink produced"
  rather than merely "some error".
- **Add** `TestResolve_UnreadableErrorIsUnwrapped`: `var se *json.SyntaxError;
  errors.As(err, &se)` succeeds **and** `fmt.Sprintf("%T", err) ==
  "*json.SyntaxError"`. The `%T` half is the one that fails if someone later
  wraps the error, which is what would silently degrade the new log line's
  `etype` field. The test file gains `encoding/json` and `fmt` imports.
- Every other existing test in the file keeps its disposition assertions
  unchanged; `TestResolve_UnparseableRecordIsUnreadable` already ignores the
  error with `_`.

## Decision 2 — pure line construction in `linkgate`, thin wrapper in `main.go`

`package main` is not host-testable (CLAUDE.md, "Tests"), so everything with a
decision in it moves into `redirect/linkgate/obs.go`, exactly the split
`SanitizeErrorMessage`/`RenderFailureLine` (testable) vs `emitFailureLine`
(wrapper) already uses. The new code decides three things — the message
sanitization wiring, the field list and its order, and the dedup key — and all
three are pure.

### `redirect/linkgate/obs.go` — three new exported symbols

```go
// dedupKeySep separates the parts of a failure-line dedup key. NUL, because no
// op name, slug or sanitized message can contain one (SanitizeErrorMessage
// replaces every control character with "_"), so the parts can never be
// ambiguously re-split by a reader or collide by concatenation.
const dedupKeySep = "\x00"

// KVFailureDedupKey builds the per-instance dedup key for an ev=kv_fail line.
// Byte-identical to the key redirect/main.go built inline before this function
// existed, deliberately: this is a move, not a behaviour change.
func KVFailureDedupKey(op, msg string) string {
	return op + dedupKeySep + msg
}

// RecordUnreadableDedupKey builds the per-instance dedup key for an
// ev=record_unreadable line.
//
// Keyed on the SLUG, not just the message — this is the whole point. A corrupt
// record is a fact about one specific slug; two different corrupt records
// commonly produce the identical decoder message ("unexpected end of JSON
// input"), so a message-keyed dedup would log the first corrupt slug an
// instance meets and hide every other one for that instance's life. The
// message is included as well so that a slug whose record is rewritten into a
// DIFFERENT kind of corruption reports again.
//
// The literal "record_unreadable" prefix keeps this key space disjoint from
// KVFailureDedupKey's, which always begins with an op name ("open"/"get"), so
// the two kinds share one map and one cap without any possibility of collision.
func RecordUnreadableDedupKey(slug, msg string) string {
	return "record_unreadable" + dedupKeySep + slug + dedupKeySep + msg
}

// RecordUnreadableLine renders the complete ev=record_unreadable failure line
// for one link record that will not parse, and the key that line must be
// deduplicated on. The caller does nothing but consult its dedup map and write
// the string (see main.go's emitRecordUnreadableLine) — every decision lives
// here, where it is host-testable.
//
// err is ParseLink's error, exactly as linkgate.Resolve returned it alongside
// DispositionUnreadable. A nil err is tolerated rather than assumed impossible
// — a future change to Resolve must degrade this line to "msg=-", never
// panic — in which case the etype field is omitted entirely, the same way
// RenderLogLine omits a zero-count op rather than emitting "=0/0".
//
// This is NOT an ev=kv_fail line and deliberately carries no op or ns field:
// no KV operation failed. The read succeeded and returned bytes; the DECODER
// failed. Anyone filtering ev=kv_fail must not see these, and anyone counting
// KV failures must not count them.
//
// etype is fmt's %T of the unwrapped error (*json.SyntaxError,
// *json.UnmarshalTypeError), which is a wording-independent classification for
// free. classifyKVFailure needs a hand-maintained English-string table only
// because spin-go-sdk flattens the WIT error variant into fixed strings; Go's
// own type system needs no such table here. The etype VOCABULARY is per-ev,
// never global — ev=kv_fail already spells it "other"/"access_denied" and
// api/obs.py already spells it "Err/Error_Other".
//
// msg is always the final field and nothing may ever be appended after it
// (CLAUDE.md, "Observable KV failures"), which is enforced structurally by
// rendering through RenderFailureLine.
func RecordUnreadableLine(slug string, err error) (line, dedupKey string)
```

`RecordUnreadableLine`'s body, in order:

1. `msg, redacted, truncated := "-", false, false`; if `err != nil`, run
   `SanitizeErrorMessage(err.Error())` and fall back to `"-"` if the sanitized
   string is empty (matching `emitFailureLine`).
2. Build the field slice in this exact order, omitting `slug` when empty,
   omitting `etype` when `err == nil`, and omitting each of `msg_redacted` /
   `msg_truncated` when false:

   `comp=redirect`, `ev=record_unreadable`, `route=/r/{slug}`, `slug=<slug>`,
   `etype=<%T>`, `msg_redacted=1`, `msg_truncated=1`, `msg=<msg>`

3. `return RenderFailureLine(fields), RecordUnreadableDedupKey(slug, msg)`.

The dedup key uses the **sanitized** message, so it can never contain a control
character and can never be longer than `MaxErrorMessageChars`.

A rendered line, for a record whose value is the literal bytes `not json`:

```
ss comp=redirect ev=record_unreadable route=/r/{slug} slug=M7RyJVC etype=*json.SyntaxError msg=invalid character 'o' in literal null (expecting 'u')
```

Note the spaces and apostrophes inside `msg` — a live demonstration of why
`msg` is last and why `RenderFailureLine` (which appends nothing) is the
renderer rather than `RenderLogLine` (which appends `dur_us` and the KV
summary).

### `redirect/main.go` — the wrapper and the two arms

`shouldEmitFailureLine` stops building the key and takes a prebuilt one. This is
the only change to the existing `kv_fail` path, and it is what "share code
cleanly" requires — one map, one mutex, one cap, one place the 32-entry budget
is spent:

```go
// shouldEmitFailureLine reports whether dedupKey has not yet been logged by
// this Wasm instance, recording it if so. The key is built by the caller
// (linkgate.KVFailureDedupKey / linkgate.RecordUnreadableDedupKey) because the
// right notion of "novel" differs per line kind — a KV fault is novel per
// (op, message), a corrupt record is novel per (slug, message) — while the
// budget below is deliberately shared, since it bounds this instance's stderr
// volume as a whole. Once maxFailureDedupPairs distinct keys have been seen,
// every further key (even a genuinely new one) is also suppressed ...
func shouldEmitFailureLine(dedupKey string) bool {
	failureDedupMu.Lock()
	defer failureDedupMu.Unlock()
	if _, seen := failureDedupSeen[dedupKey]; seen {
		return false
	}
	if len(failureDedupSeen) >= maxFailureDedupPairs {
		return false
	}
	failureDedupSeen[dedupKey] = struct{}{}
	return true
}
```

`emitFailureLine`'s one changed line:

```go
	if !shouldEmitFailureLine(linkgate.KVFailureDedupKey(op, msg)) {
		return
	}
```

The new wrapper, placed immediately after `emitFailureLine`:

```go
// emitRecordUnreadableLine writes one ev=record_unreadable line for a link
// record that exists and will not parse, deduplicated per Wasm instance on
// (slug, sanitized msg). Every decision is in linkgate.RecordUnreadableLine;
// this wrapper exists only because package main is not host-testable.
//
// Dedup matters MORE here than for a KV fault, not less: a throttled read is
// transient, while a corrupt record is permanent until a human rewrites or
// deletes it, so an undeduplicated line would re-emit on every single click of
// a shared link for as long as the instance lives.
func emitRecordUnreadableLine(slug string, err error) {
	line, dedupKey := linkgate.RecordUnreadableLine(slug, err)
	if !shouldEmitFailureLine(dedupKey) {
		return
	}
	fmt.Fprintln(os.Stderr, line)
}
```

Both handlers' unreadable arm becomes two statements, keeping
`handleRedirectGet` and `handleRedirectPost` structurally identical except for
the `DispositionPrompt` arm (CLAUDE.md, "The `/r/{slug}` status contract"):

```go
	case linkgate.DispositionUnreadable:
		emitRecordUnreadableLine(slug, resolveErr)
		internalError(w)
```

Emit **before** writing the response, matching the two `kv_fail` arms, so the
diagnostic is already on stderr regardless of what happens in the writer.

The section comment above `maxFailureDedupPairs` (lines 495-503) currently reads
*"Only the two KV-fault arms named below ever call this: DispositionUnreadable,
DispositionNotFound and recordClickCount's swallowed Get/Set errors emit
nothing"* — that sentence becomes false and must be rewritten to name the three
emitting arms (`open` fault, `get` fault, unreadable record) and the two that
still emit nothing (`DispositionNotFound`, and `recordClickCount`'s swallowed
`Get`/`Set` errors, whose reasons are unchanged).

## Decision 3 — `ev=record_unreadable`, a third event kind

`ev=kv_fail` is wrong (no KV operation failed) and `ev=exc` is wrong (that is
`api`'s catch-all `except Exception`, carries `etype`/`at=<file>:<line>`, and
has no Go counterpart — nothing here panics). The Future-work entry's proposed
`ev=record_unreadable` is kept, because it aligns with vocabulary the product
already uses for exactly this fact: `api`'s `422 {"error":
"link_record_unreadable"}` and the consistency report's `unreadable_value`
finding. One `grep unreadable` reaches all three.

Structural distinguishability holds the same way the existing two kinds hold it:
the summary line has no `ev` field at all, and the three `ev` values are
disjoint literals, so `grep 'ev=record_unreadable'` cannot collide with either
other kind, and an operator counting KV failures with `grep -c 'ev=kv_fail'`
keeps counting exactly what they counted before.

### CLAUDE.md changes (a builder task, not done by this plan)

In the **"Observable KV failures: unconditional failure lines, independent of
`log_level`/`X-SS-Debug`"** subsection, all inside the existing structure — no
new top-level section:

- The opening paragraph's "one sanitized failure line per distinct KV operation
  failure or unhandled exception" gains the third case: a link record that
  exists and will not parse.
- The **"Vocabulary"** paragraph gains `ev=record_unreadable`: emitted by
  `redirect` from both `/r/{slug}` handlers' `DispositionUnreadable` arm,
  carrying `slug` and an `etype` that is the Go type of the decoder error
  (`*json.SyntaxError` / `*json.UnmarshalTypeError`) and **no `op`/`ns`**,
  because no KV operation failed. State that `etype`'s vocabulary is per-`ev`
  and never global.
- The **"Bounded differently in the two components"** paragraph gains a
  sentence: `redirect`'s dedup key is now built by the caller, `(op, msg)` for
  `ev=kv_fail` and `(slug, msg)` for `ev=record_unreadable`, sharing one
  32-entry per-instance budget — and the reason: a message-keyed dedup would log
  only the first corrupt slug an instance meets.
- The **"The collector structurally cannot accept a key"** paragraph is
  unchanged and stays true: `RecordUnreadableLine` takes `(slug, err)`, nothing
  is recorded into a `Collector`, and a traced request's `kv_ops`/`kv_us`/
  `kv_bytes` are byte-identical to before.
- In **"The `/r/{slug}` status contract"**, the `record present, ParseLink
  failed → 500` row gains no new column, but a sentence after the table should
  note that this row is now the only one that emits an `ev=record_unreadable`
  line, and that the line carries the decoder's message — the only place in the
  application that says *why*.
- **"Security tradeoffs"**' 500-disclosure bullet is unchanged: this work adds
  no new disclosure to a visitor. The message goes to stderr, never into a
  response body.

## Trade-offs and rejected alternatives

1. **A contentless line — slug only, `Resolve` untouched** (the shape the
   Future-work entry filed). Genuinely attractive: zero contract change, zero
   test rewrite, no question about what a decoder message might echo, and it
   still answers "which slug", which is what the operator needs to go fix it.
   **Lost because the operator's very next question is "why", and nothing in the
   application answers it** — `api/consistency.py` records `{store, key}` with no
   reason, `api/links.py`'s `UnreadableLinkError` carries the slug, and
   `Resolve` throws the only real answer away. A truncated write and a schema
   mismatch between `api`'s writer and `linkgate.Link` need different fixes, and
   a contentless line makes the operator reproduce the parse by hand (which on a
   deployment means the KV explorer, which is dev-only) to learn which one it is.
   The contract change is two tokens plus an honest doc comment; the invariant it
   breaks is one the callers do not actually rely on, because they switch on the
   disposition first.
2. **Wrap the parse error in a sentinel (`ErrUnreadableRecord`, `errors.Join`,
   or `fmt.Errorf("%w")`) so `errors.Is` can still separate "store failed" from
   "record corrupt".** Attractive because it keeps a *checkable* invariant
   rather than a documented convention. Rejected twice over: the disposition
   already separates them exhaustively and visibly in a `switch`, so the
   sentinel is a redundant second discriminator that can drift out of agreement
   with the first; and wrapping destroys the `%T` classification that gives the
   log line its `etype` for free, replacing it with a hand-maintained mapping of
   exactly the kind `classifyKVFailure` only exists because it has no
   alternative. `TestResolve_UnreadableErrorIsUnwrapped` pins the no-wrapping
   rule.
3. **Re-derive the parse error in `main.go` by re-reading the record in the
   unreadable arm.** Leaves `Resolve` completely untouched, which is worth
   something. Rejected: it spends a second KV operation to recover information
   the first one already had, puts real logic in the one file that cannot be
   tested, and could observe a *different* value than the one `Resolve` judged
   (a concurrent repair), producing a line that describes a record state that
   never caused the 500.
4. **Reuse the `(op, msg)` dedup key unchanged, with `op="parse"`.** Cheapest
   possible change — no new key builder, no signature change to
   `shouldEmitFailureLine`. Rejected: two corrupt records overwhelmingly produce
   the same decoder message, so this logs the first corrupt slug an instance
   meets and hides every other one for that instance's life, which is precisely
   the failure the Future-work entry predicted. It also mislabels the event as
   an operation named "parse", inviting `op`-based filters to count a
   non-operation.
5. **Dedup on the slug alone, dropping the message from the key.** Very
   defensible — "a corrupt record is a fact about one slug" argues for exactly
   one line per slug — and it spends dedup budget more slowly. Rejected
   narrowly: including the sanitized message costs nothing in the realistic case
   (a stored value does not change without a write, so the message is stable per
   slug) and buys a second line in the one case that matters, where someone
   rewrites a record and breaks it a *different* way.
6. **A parallel dedup map/mutex/cap just for this event kind.** Avoids touching
   `shouldEmitFailureLine` at all, which respects the non-goal most literally.
   Rejected: two independent 32-entry budgets means the instance's real stderr
   bound becomes 64 and grows with every future event kind, and the duplicated
   mutex/map/cap trio is exactly the code a later reader would be right to
   consolidate. Moving the key *construction* out of the shared function is the
   smaller and more honest change.
7. **Gate the new line behind `log_level=summary` or `X-SS-Debug`.** Rejected
   for the same reason `docs/plans/observable-kv-failures.md` rejected it for
   `ev=kv_fail`: the gating exists because the summary line costs ~130 bytes on
   *every* request at a sustained rate, whereas this line costs nothing on the
   success path and fires only on a fault whose entire problem is that nobody
   knows it is happening. Volume is bounded by the per-instance dedup, not by a
   toggle.
8. **Do nothing — leave the Future-work entry and its trigger in place.** This
   was live, and the trigger is a real convention with real value (it stops the
   backlog being spent on imagined problems). Overridden by direct user
   instruction. The honest cost of building it early is small: ~90 lines
   including tests, no hot-path cost on any successful request, no new KV
   operation, and no new response-visible behaviour.

## Tasks

```
- [ ] Return the ParseLink error from linkgate.Resolve for DispositionUnreadable (must land before the obs and main.go tasks) — file(s): redirect/linkgate/resolve.go, redirect/linkgate/resolve_test.go — done when: Resolve returns the exact error ParseLink produced alongside DispositionUnreadable and still returns nil for NotFound/Redirect/Prompt; its doc comment's final paragraph is replaced with the two-disposition contract and states plainly that "no unknown host message to capture" was wrong about the record case; TestResolve_ErrorIsNilForEveryOtherDisposition is narrowed to the three nil dispositions and renamed; a new test asserts err.Error() equals what a direct ParseLink call on the same bytes produced; another asserts errors.As finds a *json.SyntaxError AND fmt.Sprintf("%T", err) == "*json.SyntaxError" (the no-wrapping pin); and `cd redirect && go test ./linkgate/...` passes
- [ ] Add RecordUnreadableLine and the two dedup-key builders to linkgate/obs.go (needs the task above) — file(s): redirect/linkgate/obs.go, redirect/linkgate/obs_test.go — done when: KVFailureDedupKey(op, msg) returns exactly op+"\x00"+msg, RecordUnreadableDedupKey(slug, msg) is prefixed with the literal "record_unreadable", and RecordUnreadableLine(slug, err) returns a line beginning `ss comp=redirect ev=record_unreadable route=/r/{slug} slug=` with etype from %T, msg as the final field and nothing after it, no op or ns field, msg_redacted/msg_truncated emitted only when true, and `msg=-` with the etype field omitted entirely for a nil err; tests cover a real ParseLink error from `not json` (etype *json.SyntaxError) and from a type-mismatched record (etype *json.UnmarshalTypeError), that a `json: cannot unmarshal ...` message survives sanitization unredacted, that a 250-char error truncates and sets msg_truncated=1, that two different slugs with an identical message produce DIFFERENT dedup keys, and that no (op, msg) pair can produce a KVFailureDedupKey equal to any RecordUnreadableDedupKey; `cd redirect && go test ./linkgate/...` passes
- [ ] Emit the record_unreadable line from both /r/{slug} handlers and re-key the shared dedup map (needs both tasks above) — file(s): redirect/main.go — done when: shouldEmitFailureLine takes a single prebuilt dedupKey string, emitFailureLine passes linkgate.KVFailureDedupKey(op, msg) so its behaviour is byte-identical to before, a new ~6-line emitRecordUnreadableLine wrapper consults the same map and writes linkgate.RecordUnreadableLine's output to stderr, both handlers' DispositionUnreadable arm calls it with (slug, resolveErr) BEFORE internalError(w), the two handlers stay structurally identical except for the DispositionPrompt arm, the comment above maxFailureDedupPairs no longer claims DispositionUnreadable emits nothing, and `cd redirect && go tool componentize-go build` produces redirect/main.wasm
- [ ] Mutation-verify the per-slug dedup key (needs the task above) — file(s): (none — verification step) — done when: temporarily changing RecordUnreadableDedupKey to ignore its slug parameter makes the two-different-slugs test fail and no other test fail, temporarily wrapping Resolve's parse error in fmt.Errorf("%w") makes the %T no-wrapping test fail, both mutations are reverted, `cd redirect && go test ./linkgate/...` passes, and both outcomes are recorded in the task note
- [ ] Document the third event kind in CLAUDE.md (needs the wiring task) — file(s): CLAUDE.md — done when: the "Observable KV failures" subsection names ev=record_unreadable alongside ev=kv_fail and ev=exc, states that it carries slug and a Go-type etype and deliberately no op/ns because no KV operation failed, that etype's vocabulary is per-ev and never global, and that redirect's dedup key is now caller-built — (op, msg) for kv_fail, (slug, msg) for record_unreadable — sharing one 32-entry per-instance budget because a message-keyed dedup would log only the first corrupt slug an instance meets; the "/r/{slug} status contract" section notes the 500 row is now the only one that emits a failure line and that the line carries the decoder's message; and no other CLAUDE.md section and no DESIGN.md/PRODUCT.md/README.md text is touched
- [ ] End-to-end manual verification against a deliberately corrupted record (needs the wiring task) — file(s): (none — verification step) — done when: against a real ./dev/kv-explorer-up.sh run with log_level unset and no X-SS-Debug header, two links are created through the GUI and both their links:slug:<slug> values are overwritten with invalid JSON via the KV explorer (different breakages — `not json` and a record with `"status": 7`); GET /r/<slug-A> returns 500 with the styled error page and stderr carries exactly one `ss comp=redirect ev=record_unreadable route=/r/{slug} slug=<slug-A> etype=*json.SyntaxError msg=...` line; a second identical GET adds NO second line; POST /r/<slug-B> emits a NEW line carrying slug-B and etype=*json.UnmarshalTypeError (proving the dedup is per-slug, not per-message, and that the POST arm emits too); an untouched slug still 302s and emits no line at all; and the outcome including both verbatim log lines is recorded in the task note
```

## Critical files

- `redirect/linkgate/resolve.go`
- `redirect/linkgate/resolve_test.go`
- `redirect/linkgate/obs.go`
- `redirect/linkgate/obs_test.go`
- `redirect/main.go`
- `CLAUDE.md`
- `TASKS.md`
- `docs/plans/disposition-unreadable-logging.md` (new)

No `spin.toml` change, no new route, no new Spin variable, no `api`/`gui`/
`gui-pages` file, and no `Jenkinsfile` change — CI keeps running the same three
commands, and `cd redirect && go test ./linkgate/...` is the only Go one.

## Verification

1. `cd redirect && go test ./linkgate/...` — the only Go test command in this
   repo. **Never `go test ./...`, `go build ./...` or `go vet ./...`**: they fail
   by design on `package main` with `wit_exports.go:934:6: missing function
   body` (CLAUDE.md, "Tests").
2. Mutation checks, run and reverted, with outcomes recorded in the task note:
   (a) make `RecordUnreadableDedupKey` ignore its `slug` argument → the
   two-different-slugs test must fail and nothing else; (b) wrap `Resolve`'s
   parse error in `fmt.Errorf("record unreadable: %w", err)` → the `%T`
   no-wrapping test must fail.
3. `cd redirect && go tool componentize-go build` — confirms `package main`
   still compiles under the only toolchain that can build it.
4. `cd api && uv run pytest` and `cd gui-pages && uv run pytest` — expected
   unchanged and untouched; run once to confirm nothing incidental broke (the
   `api` suite reads `redirect/linkgate/keys.go` from Python, so it is not
   structurally impossible for a `linkgate` edit to reach it).
5. Live run. **Use the KV explorer manifest, because corrupting a record needs
   raw KV write access:**

   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> \
   SPIN_VARIABLE_KV_EXPLORER_PASSWORD=<explorer-pw> \
   SPIN_VARIABLE_COOKIE_SECURE=false \
     ./dev/kv-explorer-up.sh
   ```

   That script always passes `--runtime-config-file`, so **the store is
   in-memory and wiped on every restart** (CLAUDE.md, "Commands") — create the
   links, corrupt them, and run every check inside one process lifetime. Set no
   `log_level` and send no `X-SS-Debug` header: the whole point is that these
   lines appear anyway.

   1. Sign in at `http://localhost:3000/` **through the real login form** (a raw
      `fetch` login produces `csrf_mismatch` 403s) and create two links; note
      their slugs as A and B.
   2. At `http://localhost:3000/internal/kv-explorer/` (basic auth, user `kv`),
      overwrite `links:slug:<A>` with `not json` and `links:slug:<B>` with
      `{"slug":"<B>","target_url":"https://example.com","status":7}`.
   3. `curl -sS -o /dev/null -w '%{http_code}\n' localhost:3000/r/<A>` → `500`.
      Confirm in the browser that the styled 500 page renders (unchanged from
      today — this work changes no response byte).
   4. In the `spin up` terminal, expect exactly one line, shaped:
      `ss comp=redirect ev=record_unreadable route=/r/{slug} slug=<A>
      etype=*json.SyntaxError msg=invalid character 'o' in literal null
      (expecting 'u')`.
   5. Repeat step 3 → still `500`, and **no second line** (per-instance dedup).
   6. `curl -sS -X POST -d 'password=x' localhost:3000/r/<B>` → `500`, and a
      **new** line carrying `slug=<B>` and `etype=*json.UnmarshalTypeError`.
      This is the load-bearing observation: the dedup is per-slug, the POST arm
      emits too, and the `etype` field really does discriminate the two root
      causes.
   7. `curl -sSI localhost:3000/r/<an untouched slug>` → `302`, and **no line of
      any kind** on stderr — the success path is untouched at 5 KV operations.
   8. `git status` clean apart from the intended source changes; `git diff
      spin.toml` empty (`spin-dev.toml` is generated and gitignored).

## Out of scope / follow-ups

- **`gui-pages` instrumentation** — the next Future-work entry down, explicitly
  excluded by the user. Different component, and it means giving a component
  that reads no Spin variable but `app_version` a whole logging seam.
- **`api`'s `422 link_record_unreadable` path emits no failure line either.**
  `api/app.py` catches `UnreadableLinkError` and returns 422, so `ev=exc` never
  fires and the `json.JSONDecodeError` is discarded exactly the way `Resolve`
  used to discard its Go counterpart — meaning an operator who opens a corrupt
  link's detail page still learns nothing. Symmetrical fix, `api`-side, deliberately
  not bundled: it would drag `api/links.py`'s error type, `api/app.py`'s
  exception handler and `api/obs.py`'s reporter into a change that is currently
  three files in one component. **Filed under `TASKS.md`'s "Future work (not
  scheduled)". Trigger: the first time this Go-side line actually fires in real
  traffic, since the same record is then reachable from both surfaces.**
- **`api/consistency.py`'s `unreadable_value` finding carries no reason.** Adding
  one would mean changing a report shape the GUI renders and that
  `test_handle_consistency_never_leaks_password_hash` guards — a `user:` record's
  `JSONDecodeError` message could echo record bytes, which is exactly the
  material that test exists to keep out. Not attempted here. Also filed under
  Future work.
- **The existing `op=get` `ev=kv_fail` line logs an attacker-supplied slug.**
  Unlike the new line (which can only fire for a slug `api` itself wrote and
  validated), that arm fires for any requested path segment, and `PathValue`
  returns it unescaped — so `/r/a%20b` would plausibly emit `slug=a b` and split
  one logfmt field into two. Out of scope per this plan's non-goals, unconfirmed,
  and filed under Future work with the confirmation step named.
- **No deploy is planned by this work.** Deploys are the user's call. When one
  happens, `spin aka logs | grep 'ev=record_unreadable'` is the query, and zero
  hits is the expected and desired result.
