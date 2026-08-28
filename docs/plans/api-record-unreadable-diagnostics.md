# API Record-Unreadable Diagnostics

## Context

`docs/plans/disposition-unreadable-logging.md` shipped the `redirect`-side half
of one question: **when a stored link record exists and will not parse, what
does the operator learn?** On `/r/{slug}` the answer is now good — one
unconditional `ev=record_unreadable` line carrying the slug, the decoder's own
Go type (`*json.SyntaxError` vs `*json.UnmarshalTypeError`) and its sanitized
message. That plan filed two `api`-side follow-ups and deliberately built
neither. Both are now being built.

**Follow-up 1 — the `422` path.** `api/links.py`'s `get_link` raises
`UnreadableLinkError(slug)`; `api/app.py:209` catches it once and answers a
plain `422 link_record_unreadable`, discarding the `json.JSONDecodeError` that
`raise ... from exc` already put in its hands. The catch-all's `ev=exc` line
never fires (this is a caught exception, not an unhandled one), so **the same
corrupt record is diagnosable from `/r/{slug}` and invisible from every `api`
surface.**

**Follow-up 2 — `unreadable_value` carries no reason.** `api/consistency.py`'s
`collect` appends `{"store", "key"}` at four sites and drops the parse failure
on the floor. So the one report in the product whose *job* is finding corrupt
values tells an operator *which* key is broken and never *why* — while the
redirect log line now says exactly why, for one key type, on one route.

Two facts sharpen this beyond "two missing fields":

1. **The three code paths do not agree on what "unreadable" means, so they
   detect overlapping-but-different populations.** Measured, not assumed
   (see Key technical facts): `linkgate.ParseLink` rejects a *type* mismatch on
   any of ten fields (`{"status": 7}` → `500` at `/r/{slug}`); `api`'s
   `get_link` accepts that same record happily (Python's `json.loads` type-checks
   nothing) and rejects only non-JSON/invalid-UTF-8; `consistency._parse_link_record`
   rejects a third set (non-object, or `owner` missing/not a string). An
   operator faced with "the redirect 500s but the API says the link is fine"
   currently has nothing in any log to explain it. That is precisely the
   asymmetry follow-up 1 removes.
2. **The `422` path is a dead end today even for the operator standing in front
   of it.** `handle_delete` is the one caller that catches `UnreadableLinkError`
   itself, deletes the record and returns `200 {"record_was_unreadable": true}` —
   destroying the only evidence of what was wrong with it, with nothing written
   anywhere. Follow-up 2 is what covers that gap (run the consistency check
   *before* deleting and the reason is in the report), which is one of the
   reasons the two are planned together.

Motivating `TASKS.md` entries, both under `## Future work (not scheduled)`,
both raised 2026-08-25 while planning `docs/plans/disposition-unreadable-logging.md`:

- "**`api`'s `422 link_record_unreadable` path discards the decoder's error
  exactly the way `redirect`'s used to**" (TASKS.md:516). Trigger: *"the first
  time the Go-side `ev=record_unreadable` line actually fires in real traffic."*
- "**`api/consistency.py`'s `unreadable_value` finding carries no reason**"
  (TASKS.md:517). Trigger: *"an `unreadable_value` finding an operator cannot
  diagnose from the key name alone."*

**Neither trigger has fired.** The user has asked for both to be built anyway,
which outranks the trigger-gating convention — the same override
`docs/plans/disposition-unreadable-logging.md` itself took, annotated in the
same style (TASKS.md:508).

**Confirmed decisions (settled by the user before planning):**

- Build both follow-ups now, despite neither trigger having fired.
- Annotate both existing Future-work entries as picked up, matching the
  "[PICKED UP … despite its trigger NOT having fired …]" precedent at
  TASKS.md:508.
- **Do not touch `redirect/` at all** — its half shipped and is verified.
- **Do not touch the `/r/{slug}` disposition contract.**
- Scope is `api/`, plus `gui/admin/store-maintenance.js` / `gui/theme.css` only
  if the new `reason` field genuinely needs a rendering change.

## Key technical facts confirmed during research

- **`UnreadableLinkError` is raised at exactly one site and caught at exactly
  two.** Raised only in `links.get_link` (`api/links.py:121`,
  `raise UnreadableLinkError(slug) from exc`). Caught in
  `api/app.py:209` (the central `422`) and in `links.handle_delete`
  (`api/links.py:471`, the deliberate repair path that deletes and returns
  `200`). Confirmed by `grep -rn "UnreadableLinkError" api/*.py`.
- **`get_link` has six call sites, and five of them reach the central `422`.**
  `links.handle_get` (359), `links.handle_update` (371), `links.handle_delete`
  (470 — the local catch), `links.handle_set_password` (506),
  `analytics.handle_analytics` (191), `qr.handle_qr` (50). CLAUDE.md's "six
  `api` paths" is a count of call sites, not of `422`-producing ones.
  `api/bulk.py` and `api/urlpolicy.py` never call `get_link` — they parse
  records themselves with their own skip behaviour — so nothing bulk can raise
  this.
- **At most ONE `UnreadableLinkError` can reach `app.py` per request, by
  construction.** Every one of the five propagating call sites is on a
  single-slug route (`/api/links/{slug}`, `…/analytics`, `…/qr`,
  `…/password`), each calls `get_link` once, and the raise unwinds `_dispatch`
  immediately. This is the fact that settles the dedup question below.
- **`raise ... from exc` already preserves the decoder error as
  `exc.__cause__`** — so follow-up 1 could technically read it with no change to
  `links.py`. Rejected below (alternative 2): implicit, and one future
  `raise UnreadableLinkError(slug)` without `from` silently loses it.
- **`json.JSONDecodeError.__str__` never echoes document bytes.** Measured
  directly (`uv run python`, 2026-08-27) across seven malformed inputs including
  one containing a `pbkdf2_sha256$…` literal: every message is
  `<fixed msg>: line L column C (char P)` — `Expecting value`, `Extra data`,
  `Invalid \escape`, `Unterminated string starting at`,
  `Illegal trailing comma before end of array`, `Expecting ',' delimiter`. The
  input `{"password_hash": "pbkdf2_sha256\$…", }` produced
  `Invalid \escape: line 1 column 33 (char 32)` — position only, no content.
  `UnicodeDecodeError` echoes exactly one byte in hex
  (`'utf-8' codec can't decode byte 0x80 in position 0: invalid start byte`).
  **This makes the sanitizer defence-in-depth rather than load-bearing — and it
  is the reason the "prove the sanitizer is on the path" test cannot rely on a
  natural input** (see the test design in the consistency section).
- **A `user:` record's value can never produce an `unreadable_value` finding.**
  `consistency.collect` builds an explicit two-shape allowlist for the users
  store (`_meta:usernames`, `session:*`, `api/consistency.py:228-231`) and never
  fetches a `user:` value at all — `test_collect_never_even_reads_a_user_record_value`
  pins it. So `TASKS.md:517`'s stated hazard ("a `user:` record's
  `JSONDecodeError` message can echo record bytes") **cannot occur through that
  key type.** The real residual hazard is one key type over: a `links:slug:<slug>`
  record legitimately contains a link's own `password_hash`
  (`pbkdf2_sha256$…` — see CLAUDE.md, "KV backup and restore", on why link
  hashes are deliberately not stripped), and that record *is* fetched and parsed.
  The guard is still required; only its motivation moves.
- **Both new redactions in `obs.sanitize_error_message` leave a natural decoder
  message intact.** `_KEY_SHAPED_PATTERN` requires a non-whitespace character
  immediately after the colon, and every decoder message has `: line` or
  `: invalid` (colon-space). Same property `test_sanitize_error_message_leaves_key_value_error_colon_space_intact`
  already pins for `key-value error: internal server error`.
- **`obs.make_failure_reporter`'s dedup key is `(op or "-", ns or "-", etype,
  msg)` and deliberately excludes `extra`** (`api/obs.py:358`). The cap is
  `MAX_FAILURE_LINES_PER_REQUEST = 3` **distinct tuples per request**, and a
  fresh reporter is built per request in `handle_request` (`api/app.py:200`).
- **`api` has no slug-log sanitizer; `redirect` gained one on 2026-08-27.**
  `linkgate.SanitizeSlugForLog` (`redirect/linkgate/obs.go:326`,
  `slugLogSafePattern = ^[A-Za-z0-9_-]{1,128}$`, placeholder `[invalid_slug]`)
  was added after a **confirmed live logfmt-injection** — a `%0A`-bearing slug
  forged a complete second `ss `-prefixed line (TASKS.md:518). Adding a `slug=`
  field to an `api` line without the twin would reopen that class of hole on a
  new surface.
- **CLAUDE.md currently asserts the opposite of what follow-up 1 does.** "The
  collector structurally cannot log a KV key" (CLAUDE.md:532) ends: *"`api` logs
  only a route **template** … the actual username or slug embedded in the URL
  never reaches the log line, so it has no analogous exposure."* This work makes
  that sentence false. It is a documentation task, not a reason not to do it —
  the slug is already non-secret by policy (CLAUDE.md, "Security tradeoffs") and
  `redirect` logs it deliberately.
- **The four `unreadable.append` sites are still at lines 203, 212, 240 and
  254**, exactly as `TASKS.md:517` records. Confirmed by reading
  `api/consistency.py`.
- **`analyze` copies findings through verbatim** (`dict(entry)`,
  `api/consistency.py:339`) and `_finding_sort_key` reads only
  `slug`/`username`/`key` — so a new `reason` key needs **no `analyze` change
  and changes no ordering**.
- **`consistency.parse_str_list` has an external caller**:
  `api/consistencyrepair.py:258`, whose very next line is
  `if raw is not None and parsed is None:` guarding the
  `index_unreadable_at_write` skip. **A tuple return left unpacked there is
  always truthy, so that guard would silently stop firing** and
  `apply_list_delta` would be handed a tuple. This is the one genuinely
  dangerous edit in the whole plan, and the mitigation is a rename (below).
  `test_consistency_repair.py:229` is the regression net.
- **The GUI needs no logic change: `renderConsistencyFindings` is already
  generic.** `gui/admin/store-maintenance.js:224-233` maps `Object.entries(f)`,
  special-cases `slug` into a chip and renders every other pair as
  `<span class="finding-field"><span class="finding-key">${escapeHtml(k)}</span> ${escapeHtml(String(v))}</span>`.
  A new `reason` key renders automatically, escaped, last (dict insertion
  order). **The one real risk is layout, not correctness:**
  `gui/theme.css:678-681` sets `.finding-field { white-space: nowrap }`, and the
  longest realistic reason (`'utf-8' codec can't decode byte 0x80 in position 0:
  invalid start byte`, 69 chars) will not wrap. UNCONFIRMED whether that
  overflows at 390px — it is measured, not assumed, by a task below, following
  the same discipline DESIGN.md records for the nav.
- **`reason` is already this page's vocabulary for exactly this kind of field.**
  `renderBlockedEntries` (`store-maintenance.js:244`) renders `b.reason` for a
  repair's blocked entries, and `consistencyrepair` emits
  `{"store", "key", "reason": "index_unreadable_at_write"}`. Same word, same
  shape, different array — no collision, and the operator sees one consistent
  label.
- **Five test assertions compare an `unreadable_value` finding by exact dict
  equality** and must be updated: `api/tests/test_consistency.py:186, 195, 278,
  289` and `api/tests/test_consistency_scenarios.py:141`. The
  `unrecognized_key` assertions (test_consistency.py:202, 209;
  test_consistency_scenarios.py:149) are **not** affected — that check gains no
  reason.
- **`app.py` is excluded from pytest** in both Python components (CLAUDE.md,
  "Tests"), so follow-up 1's `app.py` edit is verifiable only by a live run.
  Everything with a decision in it therefore goes in `obs.py`/`links.py`, and
  `app.py` gets wiring only.
- **Baseline is green, 2026-08-27**: `cd api && uv run pytest` → 712 passed;
  `cd gui-pages && uv run pytest` → 135 passed; `cd redirect && go test
  ./linkgate/...` → ok. (`go test ./...` fails by design and is never run.)
- **`TASKS.md:517` names a stale file**: the GUI is
  `gui/admin/store-maintenance.js`, renamed from `gui/admin/backup.js` on
  2026-08-27 (commits `e3b5826`/`64f9bd2`).

## Decision 0 — one plan, two independently landable stages

**Chosen: one plan.** Stage A (the `422` line) and Stage B (the consistency
`reason`) touch disjoint source files and have **no ordering constraint between
them** — either can land, ship and be reverted alone. They are nevertheless one
document because three decisions are genuinely shared, and planning them apart
would mean deciding each twice, differently:

1. **The `ev=record_unreadable` vocabulary and the "say why, not just that"
   rule.** Both stages exist to answer the same operator question about the same
   fault.
2. **The sanitizer rule.** Both put a decoder-derived string somewhere an
   operator reads, and both must route it through `obs.sanitize_error_message`
   with a test proving the call is on the path rather than merely importable.
3. **One CLAUDE.md sentence that both falsify, from opposite directions.**
   CLAUDE.md:107 currently reads: *"It is the only place in the application that
   says why a record will not parse: `api/consistency.py`'s `unreadable_value`
   finding carries `{store, key}` and no reason at all."* Stage A adds a second
   place; Stage B deletes the clause's own example. Planned separately, the
   second plan would silently rewrite the first's edit and one of the two
   framings would end up wrong in the file everyone reads first.

The tasks are ordered A-then-B only for reading convenience, and each task's
"done when" is independently checkable.

## Decision 1 — the `422` path emits one `ev=record_unreadable` line

### `api/links.py` — carry the cause explicitly

`UnreadableLinkError` gains a second, defaulted constructor argument and an
attribute:

```python
    def __init__(self, slug: str, cause: BaseException | None = None):
        super().__init__(slug)
        self.slug = slug
        # The decoder error json.loads raised, kept EXPLICITLY rather than
        # relying on __cause__ from `raise ... from exc`: it is the only thing
        # in this component that says WHY a record will not parse (line and
        # column, and JSONDecodeError vs UnicodeDecodeError), and a future
        # `raise UnreadableLinkError(slug)` with no `from` would silently drop
        # it with nothing failing. Defaulted to None so this stays
        # constructible from a test with no exception in hand; a None cause
        # degrades the log line, never breaks it (see api/app.py).
        self.cause = cause
```

and `get_link`'s raise becomes `raise UnreadableLinkError(slug, exc) from exc`
— **keeping the `from exc`**, so the traceback chain a future `ev=exc` line
would use is unchanged.

The class docstring gains one paragraph recording what the two `api`-side
notions of unreadable actually are, since this is where a reader will look:
`get_link` rejects only non-JSON and invalid UTF-8 (Python's `json.loads`
type-checks nothing), so a record `linkgate.ParseLink` refuses on a type
mismatch (`{"status": 7}` → `500` at `/r/{slug}`) is served happily here — and
`consistency._parse_link_record` rejects a third set again.

### `api/obs.py` — a slug sanitizer twin, and `extra` in the dedup key

**(a) `sanitize_slug_for_log(slug: str) -> str`** — the Python twin of
`linkgate.SanitizeSlugForLog`: returns the slug unchanged if it matches
`^[A-Za-z0-9_-]{1,128}$` (compiled once at module scope, mirroring
`links.CUSTOM_SLUG_PATTERN`'s character class minus its 3-32 length bound),
otherwise the fixed placeholder `[invalid_slug]`, carrying none of the original
bytes.

The docstring must say why it exists *here*, where the invariant is stronger
than `redirect`'s: an `UnreadableLinkError` can only be raised for a slug whose
`links:slug:<slug>` record already exists, and only `api` writes those, always
under `CUSTOM_SLUG_PATTERN` — so an attacker-crafted slug cannot reach this
field today. Sanitized anyway, for the same reason the Go side sanitizes its
`ev=record_unreadable` field: it costs nothing for a slug that already matches,
and it means the field's safety stops depending on that invariant continuing to
hold. The Go side's identical field is **not** hypothetical — a `%0A`-bearing
slug forged a complete second log line there, confirmed live (TASKS.md:518).

Like `sanitize_error_message`, this is deliberately **not** pinned against its
Go counterpart by a cross-language test (CLAUDE.md, "Parallel KV reads": a
divergence produces two slightly-differently-shaped log lines and nothing else,
unlike `keys.go`'s prefixes, which fail silently at runtime).

**(b) `extra` joins the dedup key** in `make_failure_reporter`:

```python
        key = (op or "-", namespace or "-", etype, msg, tuple(extra or ()))
```

The docstring's "Dedup key is (op, namespace, etype, msg)" paragraph is
rewritten to state the rule this generalises to: **the dedup key is everything
that distinguishes one rendered line from another, so two lines that differ
only in an `extra` field are two events, not one.** Rationale and its one
behavioural consequence:

- **Today it is a no-op for `ev=record_unreadable`** — at most one such line can
  exist per request (Key technical facts), so its dedup can never fire either
  way. It is included because the *shape* is wrong without it: a future
  multi-slug path emitting two corrupt slugs with the identical decoder message
  would collapse them onto one line and hide the second — exactly the blind spot
  `docs/plans/disposition-unreadable-logging.md` fixed on the Go side by keying
  on `(slug, msg)`. Fixing it now costs one expression; fixing it later costs
  another investigation into why a corrupt slug never appeared.
- **The one live behavioural change is to `ev=exc`**, the only current `extra`
  user: two exceptions with the same `etype` and message raised at *different*
  `at=<file>:<line>` frames now produce two lines instead of one. That is
  strictly better diagnostics, and volume is still bounded by
  `MAX_FAILURE_LINES_PER_REQUEST = 3`, which is unchanged.

### `api/app.py` — two lines of wiring, and a comment that is now false

Inside the existing `except links.UnreadableLinkError as exc:` arm
(`api/app.py:209`), **before** building the response — matching `redirect`,
which emits before writing:

```python
            failure_reporter(
                "record_unreadable", None, None, None,
                exc.cause if exc.cause is not None else exc,
                extra=[("slug", obs.sanitize_slug_for_log(exc.slug))],
            )
```

`op`/`namespace`/`duration_ns` are all `None` deliberately: `report` omits
`op`/`ns`/`op_us` entirely when `op is None`, which is exactly right here — **no
KV operation failed.** The `get` succeeded and returned bytes; the decoder
failed. Anyone filtering `ev=kv_fail` must not see these, and anyone counting KV
failures must not count them. This mirrors `linkgate.RecordUnreadableLine`'s
identical rule, one component over.

The `cause is None` fallback passes the `UnreadableLinkError` itself, degrading
the line to `etype=UnreadableLinkError msg=<slug>` rather than crashing — the
same "tolerate, degrade, never panic" posture `RecordUnreadableLine` takes for a
nil error. It is unreachable through `get_link` today.

The rendered line, for a record whose value is the literal bytes `not json`,
fetched by the link-detail page:

```
ss comp=api ev=record_unreadable route=/api/links/{slug} method=GET etype=JSONDecodeError slug=promo msg=Expecting value: line 1 column 1 (char 0)
```

Note the field order differs from `redirect`'s (`slug` lands after `etype`,
because `report` appends `extra` there, and `method` appears at all). That is
accepted, not overlooked: logfmt is key=value and order-insensitive, the only
ordering rule in this codebase is **`msg` is always last with nothing after
it** (CLAUDE.md, "Observable KV failures"), and reordering `report`'s field
assembly to cosmetically match Go would change every existing `ev=kv_fail` and
`ev=exc` line for no diagnostic gain.

**The comment at `api/app.py:218-221` becomes false and must be rewritten by the
same task.** It currently reads: *"Deliberately NOT reported through
failure_reporter: this is a data-quality fault … and it already has its own
diagnosis path (the consistency check's unreadable_value finding)."* Both halves
have to go: it *is* now reported, and the consistency check's finding is exactly
what Stage B stops treating as a sufficient diagnosis path. Replace it with why
it *is* reported (a permanent fault nobody can reproduce on demand; the record
is invisible to `GET /api/links`, which skips it silently), and keep the
existing, still-correct justification for the `422` status itself.

Nothing else in `app.py` changes: `err` stays `False` (this is a handled
exception, not a `500`), so the traced summary line still reads `status=422`
with no `err=1`, and the response body is byte-identical.

### Three things deliberately NOT done in Stage A

- **The `422` response body does not gain the reason.** `get_link` raises before
  `can_view`, so *any* authenticated principal can trigger a `422` for any slug —
  putting the decoder message in the body would widen what a non-viewer learns
  about a record they may not read. The message goes to stderr only, exactly as
  on the Go side. (That the `422` already discloses existence to a non-viewer is
  pre-existing and out of scope.)
- **`handle_delete`'s local catch stays silent.** See Trade-offs #5.
- **`handle_list`'s silent skip stays silent.** See Trade-offs #6.

## Decision 2 — every `unreadable_value` finding carries a `reason`

### `api/consistency.py` — four parse helpers return `(value, reason)`

The module gains `import obs` (pure, zero `spin_sdk` imports, no cycle — `obs`
imports only `hmac` and `re`) and one shared decoder:

```python
# A finding's reason is ALWAYS one of exactly two things, and never anything
# else: a fixed, data-free literal from a shape check below, or a decoder
# message routed through obs.sanitize_error_message. **No reason ever
# interpolates a value read from the store.** That is the property that keeps
# this report free of credential material (a links:slug:<slug> record
# legitimately carries the link's own pbkdf2_sha256 password hash), and it is
# structural rather than incidental: a shape check has nothing but a literal to
# report, and the decoder path is sanitized by construction.
#
# The reason is prose for a human, NOT a machine-readable code — no client may
# switch on it, and the GUI renders it verbatim.
def _decode_json(raw: bytes) -> tuple[object | None, str | None]:
    try:
        return json.loads(raw), None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        sanitized, _redacted, _truncated = obs.sanitize_error_message(str(exc))
        return None, sanitized or "value did not decode as JSON"
```

The four helpers each return `(value, reason)`, where `reason is None` means
"parsed fine" and `(None, None)` means "nothing to parse" (`raw is None`):

| helper | reasons it can return |
|---|---|
| `_parse_link_record` | decoder message · `not a JSON object` · `owner field missing or not a string` |
| `_parse_policy` | decoder message · `not a JSON object` · `default_action must be allow or deny` · `rules must be a list` |
| `parse_str_list_with_reason` | decoder message · `not a JSON array of strings` |
| `_parse_session_username` | decoder message · `not a JSON object` · `username field missing or not a string` |

Each of `collect`'s four append sites becomes the same two-line shape, and the
`raw is not None and X is None` guards collapse into `reason is not None`, which
is exactly equivalent (a `reason` is non-`None` only when `raw` was not `None`
and the parse failed):

```python
            record, reason = _parse_link_record(raw)
            if reason is not None:
                unreadable.append({"store": "links", "key": key, "reason": reason})
            elif record is not None:
                link_records[slug] = record
```

`collect`'s docstring shape line changes to
`"unreadable": [{"store": str, "key": str, "reason": str}]`. `analyze` and
`build_report` need **no change at all**.

**No `msg_redacted`/`msg_truncated` flags are added to a finding.** Those exist
on a log line because they are the *greppable* signal that a redaction fired
(CLAUDE.md, "Observable KV failures"); a report is read by a human, and the
substitutions are self-evident in the text itself (`[key:…]`, `[hash]`, an
abrupt 200-character cut).

### The rename that keeps a silent failure loud

`parse_str_list` is renamed to **`parse_str_list_with_reason`**, and
`api/consistencyrepair.py:258` is updated in the same task:

```python
        parsed, _reason = consistency.parse_str_list_with_reason(raw)
```

**Renaming is load-bearing, not tidiness.** If the name were kept and a call
site were missed, `parsed` would be a *tuple* — always truthy — so
`consistencyrepair`'s `if raw is not None and parsed is None:` guard would
silently stop firing, the `index_unreadable_at_write` skip would never happen,
and `apply_list_delta` would be handed a tuple. Renaming turns that into an
immediate `AttributeError`. `test_consistency_repair.py:229` (which pins
`write_skipped == [{"store": "users", "key": "_meta:usernames", "reason":
"index_unreadable_at_write"}]`) is the regression net and must stay green
unchanged.

The three other helpers are module-private with call sites in the same file, so
a missed one is visible in the same diff; they keep their names.

### Proving the sanitizer is on the path, not merely importable

`TASKS.md:517` requires "a new test asserting the sanitizer is actually on the
path". **A natural input cannot demonstrate it** — measured above, no real
`json.JSONDecodeError` message contains anything either redaction matches. Three
tests together, and the first is the one that satisfies the requirement:

1. **A spy, in `api/tests/test_consistency.py`.** `monkeypatch.setattr(consistency.obs,
   "sanitize_error_message", spy)` where the spy records its argument and
   returns `("REDACTED-BY-SPY", True, False)`; seed `links_data={"slug:bad":
   b"not json"}`; assert the spy was called exactly once with the *exact*
   `str(exc)` the decoder produced (`"Expecting value: line 1 column 1 (char
   0)"`) **and** that the finding's `reason` is `"REDACTED-BY-SPY"`. This fails
   the moment anyone inlines `str(exc)`, which is the whole point. (`consistency`
   does `import obs` and calls `obs.sanitize_error_message(...)`, so setting the
   attribute on the module object intercepts the real call and pytest reverts
   it.)
2. **A redaction unit test**: call the decode path with a hand-built
   `json.JSONDecodeError("boom users:session:tok pbkdf2_sha256$h", "d", 0)` and
   assert the produced reason contains `[key:users]` and `[hash]` and contains
   neither `session:tok` nor `pbkdf2_sha256`.
3. **An end-to-end leak guard**, extending the existing
   `test_handle_consistency_never_leaks_password_hash`: seed an
   **unreadable link record that contains a real link password hash** —
   `b'{"slug":"bad","password_hash":"pbkdf2_sha256$100$c2FsdA==$aGFzaA==",'`
   (deliberately truncated, so it both fails to parse and carries the hash) —
   and assert `b"pbkdf2_sha256" not in resp.body` and
   `b"password_hash" not in resp.body` for a report that does contain the
   `unreadable_value` finding. This pins the property `TASKS.md:517` was worried
   about, on the key type that can actually produce it.

Plus ordinary coverage of the shape reasons: a record that is valid JSON but not
an object, and one missing `owner`, produce their literal reasons and no
decoder text.

### GUI: measure first, then one CSS-only wrap if needed

`renderConsistencyFindings` picks the field up automatically — confirmed by
reading it — so **no JS change is required for the field to appear.** The open
question is `white-space: nowrap` on `.finding-field` against a ~69-character
reason at 390px. The task below measures `document.documentElement.scrollWidth`
vs `clientWidth` on `admin/store-maintenance.html` with a seeded unreadable
finding at 390px in both themes, and only if it overflows applies the minimal
fix:

- `gui/admin/store-maintenance.js`: in the non-`slug` branch, add a
  `finding-field-wrap` class when `k === "reason"` — a second key-based special
  case beside the existing `slug` one, for the one field whose value is a
  sentence rather than an identifier.
- `gui/theme.css`, beside the existing `.finding-field` rule:
  `.finding-field-wrap { white-space: normal; overflow-wrap: anywhere; }`

**No new design token, no new colour, no shadow** — DESIGN.md's No-Shadow Rule
and token discipline are untouched either way. Remember `gui/` is served from a
startup snapshot: `spin up` must be restarted for a `.js`/`.css` edit to be
visible (CLAUDE.md, "Commands").

## Trade-offs and rejected alternatives

1. **Do nothing — leave both Future-work entries and their triggers in place.**
   Live, and the trigger convention is real: it stops the backlog being spent on
   imagined problems, and neither trigger has fired. Overridden by direct user
   instruction. The honest cost of building early is small — no new endpoint, no
   new KV key, no new KV operation, no response-body change, no hot-path cost —
   and one of the triggers ("the first time the Go-side line fires in real
   traffic") is a *worse* moment to start work than a quiet one, since it fires
   during an incident.
2. **Read `exc.__cause__` in `app.py` and change nothing in `links.py`.**
   Genuinely attractive: `raise ... from exc` already sets it, so follow-up 1
   becomes a one-file change with zero contract movement. Rejected because the
   dependency is invisible at the raise site — a future
   `raise UnreadableLinkError(slug)` written without `from exc` would degrade
   the line to `msg=<slug>` with nothing anywhere failing, and `__cause__` is
   also whatever the *outermost* `raise ... from` set, not necessarily the
   decoder error. An explicit attribute makes the intent legible at the one
   place that constructs it, for four lines.
3. **A contentless `api` line — slug only, no `etype`, no message.** Cheapest
   possible: no `links.py` change at all, and it still answers "which link".
   Rejected for the same reason its Go twin was: the operator's very next
   question is "why", and a truncated write, an invalid-UTF-8 value and a
   half-written record need different responses. It would also make the two
   components' lines gratuitously different for one fault.
4. **Put the decoder reason in the `422` response body** (alongside `slug` and
   `hint`). Attractive: the operator is *already looking at* the failed request,
   and the GUI could show it without anyone reading stderr. Rejected: `get_link`
   raises before `can_view`, so every authenticated principal can trigger a
   `422` for any slug, and the body is a stable client contract that would then
   have to keep carrying a free-text field forever. The Go side already settled
   the same question the same way — the message goes to stderr, never into a
   response.
5. **Emit a line from `handle_delete`'s local `UnreadableLinkError` catch too.**
   The strongest rejected option, because deletion is the moment the evidence is
   destroyed for good — after it, nothing can ever say what was wrong.
   Rejected on three counts: that path is a documented *success* (`200
   {"record_was_unreadable": true}`), so a "failure" line there would be
   mislabelled; emitting would mean threading `failure_reporter` into
   `links.handle_delete`'s signature for one branch, putting an observability
   parameter into a pure handler that has none; and the gap is covered by Stage
   B — the consistency check run *before* deleting now says exactly why. This
   synergy is one of the reasons the two stages ship together. Revisit if an
   operator is ever observed deleting a corrupt record without having run the
   check.
6. **Emit a line from `links.handle_list`'s silent skip.** Tempting, because the
   dashboard load is where a corrupt record is most often "encountered" — it
   silently vanishes from the table with no signal anywhere. Rejected on volume,
   the one objection `docs/plans/observable-kv-failures.md` accepts as
   decisive: unlike the `422` (rare, one deliberate operator action), the list
   runs on **every dashboard load by every user**, so a permanent corrupt record
   would emit a line per page view forever, for a fault a single line already
   reported. Also a signature change on a hot handler. Filed under Future work
   with a trigger.
7. **Reuse `ev=kv_fail` with `op="parse"` for the `api` line.** No new event
   name, no vocabulary decision. Rejected identically to its Go twin: no KV
   operation failed, so it would corrupt every `grep -c 'ev=kv_fail'` capacity
   measurement (CLAUDE.md is explicit that any future capacity claim must rest
   on origin-side `ev=kv_fail` counts), and it would invite `op`-based filters
   to count a non-operation.
8. **Leave `make_failure_reporter`'s dedup key alone**, on the (correct) ground
   that at most one `record_unreadable` line can exist per request today, so the
   blind spot is unreachable. Nearly chosen — it touches nothing. Rejected
   because the reasoning is a property of today's routing, not of the reporter:
   the fix is one expression, it makes the key mean "everything that
   distinguishes the line", and its only live effect (two `ev=exc` lines from
   different frames instead of one) is an improvement still bounded by the
   3-line cap.
9. **Give `ev=record_unreadable` its own per-request budget so it can never be
   squeezed out by three prior `ev=kv_fail` lines.** Correct in the abstract:
   with `MAX_FAILURE_LINES_PER_REQUEST = 3` shared, a request that already
   emitted three distinct KV failures would drop the record line. Rejected as
   unreachable-in-practice and not worth a second budget: a request that failed
   three distinct KV operations is in a different kind of trouble, and the
   corrupt record is permanent — the next request re-reports it. Recorded as an
   accepted residual; if it ever bites, the fix is a per-`ev` budget, never a
   higher shared cap.
10. **Make the consistency `reason` a machine-readable code** (`invalid_json`,
    `missing_owner`, …) instead of prose. Attractive: stable, translatable,
    switchable-on by a client, and it would sidestep the sanitizer question
    entirely for the shape cases. Rejected because it cannot cover the half that
    matters most — the decoder's line/column, which is free-text by nature — so
    the field would either need a second `detail` field (two fields for one
    thought) or would throw away the exact information the whole change exists
    to surface. The finding already carries `store` and `key` as its stable,
    switchable-on facts.
11. **Skip the sanitizer on the consistency path**, since it was measured that
    `json.JSONDecodeError` never echoes document bytes. Rejected: the guarantee
    is CPython's current message wording, not an API contract, and this is a
    report that already has a dedicated no-credential-leak test because the
    stakes are a PBKDF2 hash. Sanitizing costs one call and makes the property
    structural rather than dependent on a stdlib implementation detail.
12. **Add `sanitize_slug_for_log` and pin it against `linkgate.SanitizeSlugForLog`
    with a cross-language test**, the way `api/tests/test_kvprefix.py` pins
    `keys.go`'s prefixes and `CountShards`. Rejected on the rule CLAUDE.md
    already states for `sanitize_error_message`: those pins exist because
    divergence there fails *silently at runtime* (the API writes links the
    redirect path cannot find), whereas two log sanitizers drifting produces two
    slightly-differently-shaped log lines and nothing else. A pin would add a
    Go-file-reading test for a cosmetic property.
13. **Log no `slug=` field on the `api` side at all**, preserving CLAUDE.md's
    "no slug ever reaches an `api` log line" invariant and needing no Python slug
    sanitizer. Rejected: the line's entire value is naming *which* link is
    corrupt; without it the operator has a route template and a decoder message
    and no way to find the record. The invariant was about not leaking a URL's
    contents incidentally, and a slug is already treated as non-secret by policy
    and logged deliberately by `redirect`. The invariant is amended in CLAUDE.md,
    explicitly, rather than quietly broken.
14. **Add `reason` to `unrecognized_key` findings too, for symmetry.** Rejected:
    there is no reason to report. The key matched no known shape — the key name
    *is* the whole finding, its value is never read (deliberately, since
    `analyticsorphans.classify_analytics_keys` holds the same rule about never
    acting on an unrecognised key), and a synthetic "matched no known shape"
    string would be pure noise on every row.
15. **Change `.finding-field` itself to `white-space: normal` instead of adding a
    modifier class.** One line, no JS change. Rejected: the `nowrap` is there so
    a short `KEY value` pair never breaks between its label and its value, which
    is most of what that list renders; relaxing it globally to fix one long field
    would degrade every other finding row.

## Tasks

```
- [ ] Add obs.sanitize_slug_for_log, the Python twin of linkgate.SanitizeSlugForLog — file(s): api/obs.py, api/tests/test_obs.py — done when: sanitize_slug_for_log returns its argument unchanged for a slug matching ^[A-Za-z0-9_-]{1,128}$ (pattern compiled once at module scope) and the fixed string "[invalid_slug]" otherwise; tests pin that a normal slug and a 128-char slug pass through, that the two payloads confirmed live against the Go side (a slug containing a space and one containing a newline) are both replaced, that the replacement carries none of the original bytes, and that an empty slug and a 129-char slug are both replaced; the docstring records that an api slug reaching this field is already CUSTOM_SLUG_PATTERN-validated (only api writes link records) and that it is sanitized anyway so the field's safety never depends on that invariant, and that this sanitizer is deliberately NOT cross-language pinned against Go's; and `cd api && uv run pytest` passes above the 712-test baseline
- [ ] Include extra in make_failure_reporter's dedup key — file(s): api/obs.py, api/tests/test_obs.py — done when: the dedup key becomes (op or "-", ns or "-", etype, msg, tuple(extra or ())) and the docstring states the general rule (the key is everything that distinguishes one rendered line from another) plus the one live consequence (two ev=exc calls with identical etype/msg but different at= frames now emit two lines, still bounded by MAX_FAILURE_LINES_PER_REQUEST); a new test pins that two reports differing ONLY in an extra field emit two lines, another pins that two identical reports with identical extra still emit one, the existing throttle-storm dedup test and the cap test both still pass unchanged, and `cd api && uv run pytest` passes
- [ ] Carry the decoder error on UnreadableLinkError — file(s): api/links.py, api/tests/test_links.py — done when: UnreadableLinkError.__init__ takes (slug, cause=None) and exposes .cause, get_link raises UnreadableLinkError(slug, exc) while KEEPING `from exc`, the docstring records why the cause is explicit rather than read from __cause__ and that api's notion of unreadable is narrower than linkgate.ParseLink's (json.loads type-checks nothing, so a record with "status": 7 parses fine here and 500s at /r/{slug}); tests pin that a non-JSON value raises with .cause being a json.JSONDecodeError whose str() names a line and column, that an invalid-UTF-8 value raises with .cause being a UnicodeDecodeError, that .slug is unchanged, that __cause__ is still set, that a record with a type-mismatched "status" field does NOT raise, and that UnreadableLinkError("x") is still constructible with .cause None; and `cd api && uv run pytest` passes
- [ ] Emit ev=record_unreadable from app.py's 422 arm (needs the three tasks above) — file(s): api/app.py — done when: the existing `except links.UnreadableLinkError` arm calls failure_reporter("record_unreadable", None, None, None, exc.cause if exc.cause is not None else exc, extra=[("slug", obs.sanitize_slug_for_log(exc.slug))]) BEFORE building the response; the 422 response body, status and the traced summary line (status=422, no err=1) are all byte-identical to before; the stale comment claiming this is "deliberately NOT reported through failure_reporter" and that the consistency check "already has its own diagnosis path" is replaced with the current reasoning while the 422-not-500/404 justification is kept; no other app.py branch changes; and `cd api && uv run componentize-py -w spin:up/http-trigger@4.0.0 componentize app -o app.wasm` succeeds
- [ ] Add a reason to every unreadable_value finding, and rename parse_str_list — file(s): api/consistency.py, api/consistencyrepair.py — done when: consistency.py imports obs and gains a _decode_json helper returning (value, reason) whose failure reason is obs.sanitize_error_message(str(exc)) with a "value did not decode as JSON" fallback for an empty result; all four parse helpers return (value, reason) with data-free literal reasons for every shape failure (not a JSON object / owner field missing or not a string / default_action must be allow or deny / rules must be a list / not a JSON array of strings / username field missing or not a string) and NO reason ever interpolates a value read from the store; all four unreadable.append sites emit {"store", "key", "reason"} and their `raw is not None and X is None` guards become `reason is not None`; collect's docstring shape line is updated; parse_str_list is RENAMED to parse_str_list_with_reason (so a missed call site is an AttributeError, never a silently-truthy tuple) and consistencyrepair.py:258 unpacks it; analyze and build_report are unchanged; and `cd api && uv run pytest` passes with test_consistency_repair.py's index_unreadable_at_write assertion green and unmodified
- [ ] Test the reason field, including that the sanitizer is on the path (needs the task above) — file(s): api/tests/test_consistency.py, api/tests/test_consistency_scenarios.py — done when: the five exact-dict assertions on unreadable_value findings (test_consistency.py:186/195/278/289, test_consistency_scenarios.py:141) are updated to include reason; a monkeypatch spy on consistency.obs.sanitize_error_message proves it is called exactly once with the decoder's exact str(exc) and that its return value is what lands in the finding's reason; a unit test on the decode path with a hand-built json.JSONDecodeError("boom users:session:tok pbkdf2_sha256$h", "d", 0) shows the reason contains [key:users] and [hash] and neither session:tok nor pbkdf2_sha256; test_handle_consistency_never_leaks_password_hash is extended with an unreadable links:slug record containing a real pbkdf2_sha256 hash and still asserts neither password_hash nor pbkdf2_sha256 appears in the body while the unreadable_value finding IS present; shape reasons are pinned for a non-object link record and one missing owner; and `cd api && uv run pytest` passes
- [ ] Mutation-verify the two guards this work adds (needs the two tasks above) — file(s): (none — verification step) — done when: each edit is made temporarily, confirmed to fail a NAMED test, and reverted: (a) replacing the obs.sanitize_error_message call in _decode_json with a bare str(exc) must fail the spy test; (b) making sanitize_slug_for_log return its argument unchanged must fail the space and newline tests and nothing else; (c) dropping tuple(extra or ()) from the reporter's dedup key must fail the differ-only-in-extra test and nothing else; `cd api && uv run pytest` passes cleanly afterwards with `git diff` showing no residue; and all three outcomes are recorded in the task note
- [ ] Measure the reason field at 390px and wrap it only if it overflows (needs the reason task) — file(s): gui/admin/store-maintenance.js, gui/theme.css — done when: against a running app with a seeded unreadable finding whose reason is the longest realistic string ('utf-8' codec can't decode byte 0x80 in position 0: invalid start byte), document.documentElement.scrollWidth vs clientWidth is measured on admin/store-maintenance.html at 390px in BOTH themes and the numbers recorded in the task note; if and only if it overflows, renderConsistencyFindings adds a finding-field-wrap class for the "reason" key only (beside the existing slug special case) and theme.css gains `.finding-field-wrap { white-space: normal; overflow-wrap: anywhere; }` beside the existing .finding-field rule, introducing no new design token, no new colour and no shadow, and the measurement is repeated clean; if it does not overflow, both files are left untouched and the numbers are recorded as the reason why
- [ ] Document both halves in CLAUDE.md (needs every code task above) — file(s): CLAUDE.md — done when: the "Observable KV failures" subsection records that ev=record_unreadable is now emitted by api as well as redirect — from app.py's 422 arm, carrying a sanitized slug and a Python etype (JSONDecodeError/UnicodeDecodeError, a fourth independent per-ev vocabulary) with no op/ns — and that api's dedup key now includes extra; the "collector structurally cannot log a KV key" paragraph's claim that a slug never reaches an api log line is corrected, naming obs.sanitize_slug_for_log as the guard; the "/r/{slug} status contract" sentence claiming the redirect line is "the only place in the application that says why a record will not parse" is corrected to name both new places; the "KV consistency check" section records that an unreadable_value finding now carries a human-readable reason that is either a fixed data-free literal or a sanitized decoder message and never interpolates stored data, and that api's notion of unreadable is narrower than linkgate.ParseLink's; and no DESIGN.md/PRODUCT.md/README.md text is touched
- [ ] End-to-end manual verification of both halves against deliberately corrupted records — file(s): (none — verification step) — done when: against one ./dev/kv-explorer-up.sh run with log_level unset and no X-SS-Debug header, two links are created through the real login form and GUI, then links:slug:<A> is overwritten with `not json` and links:slug:<B> with `{"slug":"<B>","target_url":"https://example.com","status":7}`; GET /api/links/<A> returns 422 link_record_unreadable with an unchanged body and stderr carries one `ss comp=api ev=record_unreadable route=/api/links/{slug} method=GET etype=JSONDecodeError slug=<A> msg=Expecting value: line 1 column 1 (char 0)` line; /api/links/<A>/qr and /api/links/<A>/analytics emit the same line with their own route templates; GET /api/links/<B> returns 200 and emits NO line while GET /r/<B> returns 500 and emits the Go-side line with etype=*json.UnmarshalTypeError (the documented asymmetry, demonstrated live); GET /api/links still returns 200 with <A> silently absent; the Store maintenance page's consistency check shows the unreadable_value finding for slug:<A> with a rendered reason and no pbkdf2/password_hash text anywhere in the response; and every verbatim log line plus the 390px measurement is recorded in the task note
```

## Critical files

- `api/obs.py`
- `api/links.py`
- `api/app.py`
- `api/consistency.py`
- `api/consistencyrepair.py`
- `api/tests/test_obs.py`
- `api/tests/test_links.py`
- `api/tests/test_consistency.py`
- `api/tests/test_consistency_scenarios.py`
- `gui/admin/store-maintenance.js` (only if the 390px measurement demands it)
- `gui/theme.css` (same condition)
- `CLAUDE.md`
- `TASKS.md`
- `docs/plans/api-record-unreadable-diagnostics.md` (new)

No `redirect/` file, no `spin.toml` change, no new route, no new Spin variable,
no new KV key type (so none of the three obligations CLAUDE.md attaches to a new
key type apply), and **no `Jenkinsfile` change** — CI keeps running the same
three commands.

## Verification

1. `cd api && uv run pytest` — the whole suite, above the 712-test baseline
   measured 2026-08-27.
2. `cd gui-pages && uv run pytest` — expected untouched at 135; run once to
   confirm nothing incidental broke (`gui-pages/tests/` polices things outside
   its own component).
3. `cd redirect && go test ./linkgate/...` — expected untouched. **Never
   `go test ./...`, `go build ./...` or `go vet ./...`**: they fail by design on
   `package main` (CLAUDE.md, "Tests").
4. Mutation checks, run and reverted, outcomes recorded in the task note:
   (a) bare `str(exc)` in `_decode_json` → the spy test must fail;
   (b) `sanitize_slug_for_log` returning its argument unchanged → the space and
   newline tests must fail and nothing else;
   (c) dropping `tuple(extra or ())` from the dedup key → the
   differ-only-in-`extra` test must fail and nothing else.
5. `cd api && uv run componentize-py -w spin:up/http-trigger@4.0.0 componentize
   app -o app.wasm` — confirms `app.py`, which pytest cannot import, still
   builds.
6. Live run. **Use the KV explorer manifest, because corrupting a record needs
   raw KV write access:**

   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> \
   SPIN_VARIABLE_KV_EXPLORER_PASSWORD=<explorer-pw> \
   SPIN_VARIABLE_COOKIE_SECURE=false \
     ./dev/kv-explorer-up.sh
   ```

   That script always passes `--runtime-config-file`, so **the store is
   in-memory and wiped on every restart** (CLAUDE.md, "Commands") — create,
   corrupt and check inside one process lifetime. Set no `log_level` and send no
   `X-SS-Debug`: the whole point is that these lines appear anyway. Note also
   that any `gui/` edit from the 390px task needs a restart to be served
   (`spin_static_fs` serves a startup snapshot); `curl localhost:3000/admin/store-maintenance.js`
   is how to confirm the served asset matches disk.

   1. Sign in at `http://localhost:3000/` **through the real login form** (a raw
      `fetch` login produces `csrf_mismatch` 403s) and create two links, A and B.
   2. At `http://localhost:3000/internal/kv-explorer/` (basic auth, user `kv`),
      overwrite `links:slug:<A>` with `not json` and `links:slug:<B>` with
      `{"slug":"<B>","target_url":"https://example.com","status":7}`.
   3. `curl` the detail, QR and analytics endpoints for A with the session
      cookie → `422 link_record_unreadable` each, and one
      `ev=record_unreadable` line each on stderr, differing only in `route` and
      `method`. Confirm `msg` is the final field with nothing after it.
   4. `GET /api/links/<B>` → `200` and **no line** (Python parses it fine), while
      `curl -sI localhost:3000/r/<B>` → `500` with the Go-side
      `etype=*json.UnmarshalTypeError` line. This is the load-bearing
      observation: the two components genuinely disagree about what "unreadable"
      means, and both now say so.
   5. `GET /api/links` → `200` with A silently absent and no line (the skip is
      deliberately still silent).
   6. Open Store maintenance in a browser, run the consistency check → the
      `unreadable_value` finding for `slug:<A>` renders with a `REASON` field.
      Search the response body for `pbkdf2_sha256` and `password_hash` → zero
      hits.
   7. The 390px measurement from the GUI task, in both themes.
   8. `git status` clean apart from the intended changes; `git diff spin.toml`
      empty (`spin-dev.toml` is generated and gitignored).

## Out of scope / follow-ups

- **`links.handle_list`'s silent skip of an unreadable record.** Trade-offs #6.
  It is the most-travelled path over a corrupt record and it says nothing, but
  it runs on every dashboard load, so an unconditional line there would emit
  forever for one permanent fault. **Filed under `TASKS.md`'s "Future work (not
  scheduled)". Trigger: a corrupt record reported by a user as "my link
  vanished from the dashboard" rather than found by the consistency check** —
  which would mean the two surfaces this plan instruments were not enough. If
  built, it needs a rate/uniqueness discipline of its own, not just a
  `failure_reporter` parameter.
- **`links.handle_delete`'s silent repair path.** Trade-offs #5. Covered in
  practice by Stage B, since the consistency check now says why before the
  record is destroyed. Same Future-work entry as above.
- **A per-`ev` budget in `make_failure_reporter`.** Trade-offs #9. Accepted
  residual: three prior distinct `ev=kv_fail` lines in one request would squeeze
  out the `record_unreadable` line. Trigger: that combination actually observed.
- **`redirect/` is untouched**, per the non-goals — its half shipped, was
  mutation-verified and was verified live.
- **No deploy is planned by this work.** Deploys are the user's call. When one
  happens, `spin aka logs | grep 'ev=record_unreadable'` reaches both components
  in one query (that is the point of sharing the vocabulary), and zero hits is
  the expected and desired result.
