# gui-pages Unhandled-Exception Guard

## Context

`gui-pages` is the catch-all component (`route = "/..."`) whose entire job is
serving the GUI's HTML pages *and attaching this app's security response
headers* — CSP, `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`,
HSTS, plus `X-SS-Version`. Its `handle_request` has **no `try/except` at all**.

`routing.build_response` guards the one realistically-failing thing — the file
read, via its `except OSError` branch, which since
`docs/plans/gui-pages-failure-logging.md` (shipped 2026-08-28) also emits one
`ev=page_read_failed` line. Anything raising **outside** that branch —
`urlparse`, `variables.get`, a `read_file` raising something that is not an
`OSError`, the `Response` construction, or any future addition to
`handle_request` — is unguarded, and the response a visitor gets then carries
**none** of this component's headers.

Motivating entry: `TASKS.md` line 541, raised 2026-08-28 while planning
`docs/plans/gui-pages-failure-logging.md` and deliberately left out of it
(that plan's Trade-offs #8). Its stated trigger — *"any bare/unstyled 500
observed from the catch-all, or the next change that puts real logic in
`handle_request`"* — has **not** organically fired. This is being built anyway
on the user's direct instruction, the same override precedent as
`docs/plans/disposition-unreadable-logging.md` (2026-08-25),
`docs/plans/api-record-unreadable-diagnostics.md` (2026-08-27) and
`docs/plans/gui-pages-failure-logging.md` (2026-08-28). That entry is annotated
in the established `[PICKED UP … despite its trigger NOT having fired …]` style.

Confirmed decisions (settled by the user before planning):

- Build it now despite the unfired trigger; do not re-litigate the trigger.
- Do not touch `redirect/` or `api/` at all.
- Do not change the file-read failure's existing behaviour —
  `ev=page_read_failed` stays exactly as shipped.
- No response-behaviour change beyond *"an unhandled exception now gets the
  styled, header-bearing 500 instead of a bare one"*: no new status codes, no
  new headers beyond `routing.SECURITY_HEADERS`.
- Mirror `api/app.py`'s pattern: `except Exception as exc:` → one `ev=exc`
  failure line carrying `etype` and `at=<file>:<line>`, then `routing`'s own
  styled 500.

**Two research findings that correct the entry's premise. Neither changes the
fix; both change what the fix is worth and how it must be argued.**

1. **The exception does *not* "propagate straight to the WASI host."** The Spin
   Python SDK's `Handler.handle()` already wraps the call to `handle_request`
   in a **bare `except:`** — it calls `traceback.print_exc()` and returns a
   `WasiResponse` built from an empty `Fields()` with status 500 and **no body
   stream at all**. So the real observed symptom today is a `500` with *zero*
   headers and an *empty* body, not a Spin-generated error page. The
   header-loss claim in the entry is exactly right; the mechanism is not.
2. **The hole is remotely triggerable today, with no code change.**
   `urlparse("//[")` raises `ValueError: Invalid IPv6 URL`, and
   `build_response`'s first statement is `path = urlparse(uri).path`
   (`routing.py:104`), outside its own `try`. `request.uri` is the SDK's
   `get_path_with_query()`, so a request target beginning `//[` reaches it
   verbatim. This is arguably the entry's own trigger firing — it just fires
   silently, into a stack trace nobody reads, which is precisely the condition
   the change removes. (Whether Spin/Hyper forwards such a target rather than
   rejecting it at the HTTP layer is UNCONFIRMED; see below.)

Finding (1) has a consequence worth stating up front, because it inverts one
axis of the trade: today an unhandled exception **does** produce a signal on
stderr — a full Python traceback, with source text and the entire frame chain.
Catching it means the SDK's `except:` never fires, so this change **replaces**
that traceback with one bounded, sanitized, greppable line. That is the right
trade under this repo's standing rule (*"the innermost traceback frame — never
a traceback, never source text"*), and `api` already made exactly it — but it
is a trade, not a pure gain, and it is the decisive argument for carrying
`at=<file>:<line>` (see Trade-offs #3).

## Key technical facts confirmed during research

- **The SDK already catches, and returns a header-less, body-less 500.** Read
  directly from
  `gui-pages/.venv/lib/python3.14/site-packages/spin_sdk/http/__init__.py:84-97`:

  ```python
  try:
      simple_response = await self.handle_request(Request(...))
  except:
      traceback.print_exc()

      response = WasiResponse.new(Fields(), None, _trailers_future())[0]
      response.set_status_code(500)
      return response
  ```

  `Fields()` is empty, so no CSP, no `nosniff`, no `X-Frame-Options`, no
  `X-SS-Version`; the body stream argument is `None`. The
  `content-length`-filling and header-copying code below it is skipped
  entirely on this path.
- **`request.uri` is `get_path_with_query()`**, defaulting to `"/"` when
  `None` (same file, lines 78-82). It is a request-controlled string that is
  *not* a full URL — which is why `urlparse` sees `//[` as a netloc.
- **`urlparse` raises on a `//[`-prefixed path**, measured on the component's
  own interpreter (`cd gui-pages && uv run python`, Python 3.14.6):
  `urlparse("//[::1")`, `urlparse("//[v1.x")` and `urlparse("//[")` all raise
  `ValueError: Invalid IPv6 URL`; `urlparse("//[::1]:80/x")` and
  `urlparse("/%zz")` do not. The message carries **no request bytes**.
- **`exc_location` on that exception names a stdlib file, not ours.** Measured
  with `api/obs.exc_location`'s exact implementation against a `ValueError`
  raised through a local `build_response`-shaped wrapper: `at=parse.py:525`,
  `etype=ValueError`, `msg=Invalid IPv6 URL`. The innermost frame is inside
  `urllib/parse.py`. This is expected and accepted — see Trade-offs #4.
- **`spin_sdk.variables.get` raises `componentize_py_types.Err`** wrapping one
  of `Error_InvalidName` / `Error_Undefined` / `Error_Provider` /
  `Error_Other` — read from
  `.venv/.../spin_sdk/wit/imports/spin_variables_variables_3_0_0.py`, which
  documents exactly that in `get`'s docstring. So `type(exc).__name__` alone
  would render a useless bare `Err`, while `api/obs.error_type_name`'s
  duck-typed `getattr(exc, "value", None)` walk renders `Err/Error_Undefined`.
  Verified the walk's logic against a stand-in class pair on the host.
- **`componentize_py_types` does not exist in the host venv** (`ls
  gui-pages/.venv/lib/python3.14/site-packages` — absent), which is why
  `error_type_name` must stay duck-typed and can never `isinstance`-check.
  Same constraint CLAUDE.md already records for `api`.
- **`str(Err(...))` does surface the inner value's text.** Confirmed by shipped
  behaviour rather than by construction: `api/kvretry.classify_write_error`
  does `str(exc).lower()` and matches Akamai's `too many requests` /
  `rate limit exceeded` markers live (CLAUDE.md, "Write-throttle resilience").
- **`api/obs.exc_location` has zero `api`-specific coupling** — it walks
  `exc.__traceback__` to the innermost frame and returns
  `f"{basename}:{tb.tb_lineno}"`, returning `"-"` when there is no traceback
  (`api/obs.py:337-350`). Pure stdlib. Same for `error_type_name`
  (`api/obs.py:317-334`).
- **`gui-pages` and `api` are independent `uv` projects** with their own
  `pyproject.toml`/`uv.lock` and no shared package; componentize-py compiles
  `app.py` with only its own component directory on the import path. A shared
  `obs.py` is not available (Trade-offs #6).
- **`routing.build_response`'s 500 and the new catch-all's 500 must be the same
  bytes**, and today the 500 is constructed inline at `routing.py:139`:
  `Response(500, {**SECURITY_HEADERS, "content-type": "text/html; charset=utf-8"}, ERROR_PAGES[500])`.
- **`gui-pages/obs.py` already ships everything reusable**:
  `sanitize_error_message` (two-rule: control chars → `_`, then truncate at
  `MAX_ERROR_MESSAGE_CHARS = 200`), `render_failure_line`, `make_dedup`
  (`MAX_FAILURE_DEDUP_KEYS = 32`), `_SEP = "\x00"`. Zero `spin_sdk` imports.
- **The last live run settled the errno mapping and the dedup caveat**
  (`TASKS.md`, the `## gui-pages page-read failure logging` verification note,
  2026-08-28): componentize-py's WASI CPython reports `errno=44` for ENOENT,
  not host CPython's `2`; and an instance that has *just* finished a component
  build may cycle through more than one Wasm instance, degrading dedup — the
  clean result needs a settled instance. Both matter to this plan's live step.
- **Baseline is green:** `cd gui-pages && uv run pytest` → `155 passed in
  0.20s` before any change.
- **UNCONFIRMED: whether Spin/Hyper forwards a `//[`-prefixed request target**
  to the component at all, rather than answering `400` itself. Confirming it
  takes one `curl --path-as-is 'http://localhost:3000//[::1'` against a live
  `spin up`; Verification step 5 does exactly that, and step 6 is the fallback
  if it does not reach us.
- **UNCONFIRMED: `str()` of a real `componentize_py_types.Err` under the
  component runtime.** The `Error_*` variants are `@dataclass`es, so the likely
  rendering is `Error_Undefined(value='app_version')`; the `kvretry` evidence
  above proves the inner text is present but not its exact framing. It does not
  matter to any decision here — `msg` is sanitized and truncated regardless,
  and `etype` carries the variant independently.

## The log line

One `ev=exc` line, emitted to stderr on failure only, **unconditionally** —
independent of any log toggle, which `gui-pages` does not have anyway
(it declares only `app_version` under `[component.gui-pages.variables]`):

```
ss comp=gui-pages ev=exc etype=ValueError at=parse.py:525 msg=Invalid IPv6 URL
```

| field | value | notes |
|---|---|---|
| `comp` | `gui-pages` | fixed literal |
| `ev` | `exc` | **reused from `api`, not a new `ev` value** — see Trade-offs #5 |
| `etype` | `obs.error_type_name(exc)` | `Err/Error_Undefined` for a WIT error, otherwise the bare class name |
| `at` | `obs.exc_location(exc)` | `<basename>:<lineno>` of the **innermost** frame, `-` when there is no traceback |
| `msg_truncated` | `1`, omitted otherwise | before `msg`, never after |
| `msg` | sanitized `str(exc)`, or `-` | **always last; nothing may ever be appended after it** |

**No `op`/`ns`/`op_us`** — no KV operation failed, and this component performs
none. Same rule `ev=page_read_failed` and both `ev=record_unreadable` variants
already hold.

**No `route` and no `method`, deliberately — this is the one field decision
that differs from `api`'s `ev=exc`.** `api` can carry `route` because
`obs.route_template` maps the path onto a fixed vocabulary of templates;
`gui-pages` has no such function, and the only route value available at the
catch-all is derived from the raw, request-controlled URI — which, in the
`urlparse` case, *is the thing that raised*, so it may not even be parseable.
Logging it raw is forbidden (one component over, a `%0A`-bearing path segment
was confirmed live to forge a complete second `ss `-prefixed line — `TASKS.md`,
2026-08-27, the `SanitizeSlugForLog` fix), and running it through
`sanitize_path_for_log` would render `[invalid_path]` for most values a
catch-all actually sees, which is a field that says nothing. `method` is
request-controlled too (the SDK passes `Method_Other(...).value` through
verbatim). `at=` is the locating field and is provably data-free.

**No `msg_redacted`, ever, by construction** — `gui-pages/obs.py`'s sanitizer
has no redaction rule that can fire, exactly as its docstring already records
for `ev=page_read_failed`.

**Greppability:** `grep 'ev=exc'` now returns lines from two components,
distinguished by `comp=`. That is intended — an operator asking *"did any
handler blow up?"* wants both. It cannot collide with a summary line (no `ev`
field at all) or with the other three `ev` values.

## `gui-pages/obs.py` changes

Three additions. No existing function changes — in particular
`page_read_failed_line` keeps `type(exc).__name__` rather than switching to the
new `error_type_name`: for an `OSError` the two return the identical string, so
the switch would be a behaviour-touch on a shipped path for zero observable
gain, and the non-goals forbid it.

```python
def error_type_name(exc: BaseException) -> str:
    """Returns a wording-independent signal of the WIT error variant.

    A near-verbatim copy of api/obs.py's function of the same name, and
    deliberately NOT pinned against it (same standing rule as the two
    sanitize_error_message implementations: divergence produces differently
    shaped log lines and nothing else). It exists here because
    spin_sdk.variables.get raises componentize_py_types.Err wrapping an
    Error_* dataclass, and `type(exc).__name__` on that renders a useless
    bare "Err". Duck-typed via getattr, never an isinstance check —
    componentize_py_types does not exist in the host venv at all.
    """


def exc_location(exc: BaseException) -> str:
    """Returns "<basename>:<lineno>" of the INNERMOST traceback frame — never
    source text, never a value, so a 500 is diagnosable from one field that
    provably contains no data. Returns "-" when the exception carries no
    traceback (one constructed but never raised, as in a test).

    A verbatim copy of api/obs.py's. The innermost frame is often a stdlib or
    SDK file rather than one of ours (measured: a urlparse ValueError yields
    parse.py:525) — that is the intended behaviour, not a defect; see the
    plan's Trade-offs #4.
    """


def unhandled_exception_line(exc: BaseException) -> tuple[str, str]:
    """Returns (line, dedup_key) for one ev=exc failure line.

    Every decision lives here, where it is host-testable; the caller
    (app.py's _report_unhandled_exception) does nothing but consult a dedup
    set and write the string — the same split page_read_failed_line
    established, and linkgate.RecordUnreadableLine before it.

    Field order, all load-bearing: comp, ev, etype, at, [msg_truncated], msg
    (always last, nothing after it). No op/ns (no KV operation failed — this
    component performs none) and no route/method (the only available value is
    the request-controlled URI, which in the urlparse case is what raised).
    """
```

`unhandled_exception_line`'s body, in order: `etype = error_type_name(exc)`;
`at = exc_location(exc)`; `sanitized, truncated = sanitize_error_message(str(exc))`;
`msg = sanitized or "-"`; build the field list; `line = render_failure_line(fields)`;
and

```python
    dedup_key = _SEP.join(["exc", etype, at, msg])
```

The `"exc"` literal prefix keeps this key space disjoint from
`page_read_failed_line`'s, exactly as `linkgate.RecordUnreadableDedupKey`'s
does. With the two dedup sets separated (below) that prefix is strictly
redundant today — it is kept anyway so that a future decision to merge the two
sets cannot silently collide, the same "don't let safety rest on an invariant"
move `sanitize_path_for_log` already makes.

The key is **everything the line renders**, which is the rule
`api/obs.make_failure_reporter` already encodes: two `ev=exc` calls with the
same `etype`/`msg` raised at different `at=` frames are two events, not one.

## `gui-pages/routing.py` changes

One new public function, and the `except OSError` branch changed to return it —
so the two 500s this component can serve are the same object by construction,
not by two developers keeping two literals in step.

```python
def internal_error_response() -> Response:
    """The single 500 this component ever serves: the styled error page plus
    every SECURITY_HEADERS entry.

    Shared by build_response's `except OSError` branch and by app.py's
    unhandled-exception catch-all (docs/plans/gui-pages-unhandled-exception-guard.md),
    so a visitor cannot tell the two apart and the two cannot drift. Kept in
    routing.py rather than app.py because it is pure and therefore testable,
    and because app.py would otherwise have to import ERROR_PAGES and
    SECURITY_HEADERS just to rebuild a response routing.py already knows how
    to build.
    """
    return Response(500, {**SECURITY_HEADERS, "content-type": "text/html; charset=utf-8"}, ERROR_PAGES[500])
```

and in `build_response`:

```python
        return internal_error_response()
```

replacing the inline `Response(500, ...)` at today's `routing.py:139`. The
`on_read_error` call and its guarding `try/except Exception: pass` above it are
untouched, as is every other branch, `ROUTES`, `SECURITY_HEADERS`,
`resolve_file` and the `nonpages` handling. `routing.py` still imports nothing
from `spin_sdk` and nothing from `obs`.

**`urlparse` is deliberately NOT guarded here.** Turning a malformed request
target into a 404 (or a 400) is a product decision that changes a status code,
which the non-goals forbid, and which this change makes *observable* for the
first time — the right order is guard, observe, then decide. Filed as Future
work with a trigger.

## `gui-pages/app.py` changes

This is the only untestable code in the change (`app.py` is the real WASI
entrypoint and is excluded from pytest, per CLAUDE.md's "Tests"). It is kept to
the minimum: two module-scope lines, one three-line reporter, one helper
expression, and the `try/except` itself.

```python
from routing import build_response, internal_error_response
```

The existing module-scope dedup is **renamed** so the two are unambiguous —
`_should_emit_failure` → `_should_emit_page_read_failure` — and a second,
**independent** one is added beside it. The existing comment block above it
(why instance-lifetime, why this is not the forbidden shared-collector pattern)
stays and applies to both; a short note is added for why there are two sets:

```python
# Two INDEPENDENT dedup sets, not one shared 32-key budget, because the two
# line kinds have asymmetric key spaces. ev=page_read_failed's key is bounded
# by construction — 10 ROUTES keys x 8 filenames, all module literals, and in
# practice one tuple per drifted page. ev=exc's is not bounded at all: `msg`
# comes from an arbitrary exception and may embed request-derived text (a
# KeyError on a header name, a ValueError echoing a value). Sharing one budget
# would let the unbounded kind permanently silence the bounded one for this
# instance's whole life. Separate sets cost one closure and cap the instance
# at 64 lines instead of 32 — bounded either way.
_should_emit_page_read_failure = obs.make_dedup()
_should_emit_exc = obs.make_dedup()
```

```python
def _report_unhandled_exception(exc: BaseException) -> None:
    """The ev=exc twin of _report_read_error. Unconditional — emitted
    regardless of any log toggle (this component has none), because this is
    now the ONLY evidence of what a 500 from the catch-all actually was: the
    SDK's own bare `except:` and its traceback.print_exc() no longer run once
    handle_request catches."""
    line, dedup_key = obs.unhandled_exception_line(exc)
    if _should_emit_exc(dedup_key):
        print(line, file=sys.stderr)
```

and `handle_request` in full:

```python
class HttpHandler(Handler):
    async def handle_request(self, request: Request) -> Response:
        # The catch-all wraps the WHOLE body, not just build_response
        # (docs/plans/gui-pages-unhandled-exception-guard.md). build_response's
        # own `except OSError` covers exactly one statement inside itself; the
        # urlparse ahead of it, the `variables.get` behind it, and anything a
        # future edit adds here are all outside it. Without this, the Spin
        # SDK's own bare `except:` answers with an empty-Fields, empty-body
        # 500 — no CSP, no nosniff, no X-Frame-Options, no X-SS-Version, from
        # the one component whose entire job is attaching them.
        try:
            result = build_response(request.uri, _read_file, _report_read_error)
            # X-SS-Version is attached here rather than in
            # routing.SECURITY_HEADERS because it comes from a Spin variable,
            # and routing.py deliberately imports nothing from spin_sdk so it
            # stays host-testable under pytest.
            headers = {**result.headers, "x-ss-version": await _app_version_value()}
            return Response(result.status, headers, result.body)
        except Exception as exc:
            # A diagnostic must never break the response it is diagnosing —
            # the same guard routing.py puts around on_read_error, for the
            # same reason, and doubly so here where the response being built
            # IS the failure response.
            try:
                _report_unhandled_exception(exc)
            except Exception:
                pass
            fallback = internal_error_response()
            # Read from the cache, never re-awaited: _app_version_value() is
            # itself a candidate for what just raised, and a fallback path
            # that makes a host call can fail a second time — which lands back
            # in the SDK's header-less 500, the exact outcome this arm exists
            # to prevent. Nothing below this line can raise: a dict literal, a
            # dataclass field read and a dataclass construction.
            return Response(
                fallback.status,
                {**fallback.headers, "x-ss-version": _app_version or "unknown"},
                fallback.body,
            )
```

`except Exception`, not bare `except:` and not `BaseException` — matching
`api/app.py:241`, so `SystemExit`/`KeyboardInterrupt`/`asyncio.CancelledError`
still unwind rather than being converted into a 500.

`_read_file`, `_app_version_value`, `_report_read_error` and `GUI_DIR` are
otherwise untouched.

**`x-ss-version` on the fallback keeps CLAUDE.md's "every response from
`redirect`, `api` and `gui-pages` carries `X-SS-Version`" true on this path
too.** On the very first request of a fresh instance, if the failure happens
before the variable has ever been read, the header renders `unknown` — which is
already its legitimate value when no operator supplied one. That ambiguity is
accepted; the alternatives are dropping the header (breaking a documented
invariant on exactly the response an operator most wants to attribute to a
build) or awaiting inside the failure path (rejected above).

## `gui-pages/errorpages.py` change (comment only, zero served bytes)

The comment above `INTERNAL_ERROR_HTML` currently reads, in part, *"this drift
is permanent until a redeploy, not transient"* — an accurate description of the
one condition that could reach the 500 page before this change, and an
incomplete one after it. A second source now exists, and some of its instances
*are* transient (a `variables.get` provider blip), while others are permanent
per-URL (the `urlparse` case).

The comment must be corrected; the **served copy must not change**. The copy's
hedge — *"Reloading is unlikely to help — let whoever runs this service know"* —
still holds for both sources and stays byte-identical, so
`test_no_inline_code.py`'s coverage of `ERROR_PAGES` passes unchanged. Add
something to this effect:

```python
# Two conditions now reach this page, both answered identically on purpose
# (routing.internal_error_response is the single constructor): a
# ROUTES-vs-filesystem drift, which is permanent until a redeploy, and any
# unhandled exception in app.py's handle_request
# (docs/plans/gui-pages-unhandled-exception-guard.md), which may be either.
# Still no "we've been notified" — both emit a line to stderr
# (ev=page_read_failed / ev=exc) but nothing monitors that stream, so it would
# be a claim this app cannot honour. "Reloading is unlikely to help" holds for
# both: the drift is permanent, and a request whose own URI is what raised
# will raise again identically.
```

## Tests

Everything except `app.py`'s ten-odd lines is pure and host-testable. New tests
in `gui-pages/tests/test_obs.py`:

1. `exc_location` names the **innermost** frame — raise through two nested
   functions in the test module and assert the reported line number is the
   inner one's, not the outer's.
2. `exc_location` returns `"-"` for an exception with no traceback
   (`obs.exc_location(ValueError("never raised")) == "-"`).
3. `exc_location` returns a **basename**, never a path — assert `"/" not in`
   the result and that it splits into exactly two `:`-separated parts.
4. `error_type_name` returns `"Err/Error_Undefined"` for a stand-in object with
   a `.value` whose class name starts with `Error_` (duck-typed — the test must
   **not** import `componentize_py_types`, which does not exist in this venv),
   and the bare class name for a plain `ValueError`.
5. `error_type_name` for an object whose `.value` is present but whose class
   name does **not** start with `Error_` falls back to the bare outer name.
6. `unhandled_exception_line` renders exactly
   `comp=gui-pages ev=exc etype=… at=… msg=…`, in that order, and
   `line.startswith("ss ")`.
7. `msg` is last: nothing follows ` msg=` — assert `line.rindex(" msg=")`
   exceeds the index of every other field, including `at=`.
8. A message containing `\n` cannot forge a second `ss `-prefixed line —
   assert on the **rendered line**, `len(line.splitlines()) == 1`, mirroring the
   existing `test_a_message_containing_a_newline_cannot_forge_a_second_line`.
9. A 250-character message yields `msg_truncated=1` positioned **before**
   `msg=`; a short one omits the field entirely (`" msg_truncated=" not in line`).
10. An exception with an empty message renders `msg=-`, never `msg=`.
11. The dedup key starts with `"exc\x00"` and is **disjoint** from a
    `page_read_failed_line` key for a comparable exception — assert neither key
    is a prefix of the other and that they are unequal.
12. Two `ev=exc` values with identical `etype`/`msg` raised at different lines
    produce **different** dedup keys (the `at=`-in-the-key rule).
13. `unhandled_exception_line` never emits `route=`, `method=`, `op=` or `ns=` —
    a negative assertion, because this is a deliberate divergence from
    `api`'s `ev=exc` that a future edit could "helpfully" undo.

New tests in `gui-pages/tests/test_routing.py`:

14. `internal_error_response()` is `500`, carries `ERROR_PAGES[500]` and every
    `SECURITY_HEADERS` entry plus `content-type: text/html; charset=utf-8`.
15. **The two 500s are identical** — `build_response` with a failing
    `read_file` returns a response whose `status`, `headers` and `body` all
    equal `internal_error_response()`'s. This is the pin that stops the
    catch-all's 500 and the read-failure's 500 from drifting apart.
16. The existing `except OSError` tests (spy called once, no-callback
    back-compat, a raising callback not breaking the response, 404/nonpages
    never calling it) must all still pass **unmodified** — they are the
    regression guard that refactoring the branch to `internal_error_response()`
    changed nothing.

No change to `test_no_inline_code.py` (no new HTML, no served-byte change) or
`test_manifest_components.py` (no manifest change). `Jenkinsfile` is **not** in
scope: the three test commands it runs are unchanged.

## Trade-offs and rejected alternatives

**1. Wrapping something narrower than the whole `handle_request` body —
rejected.** Two variants were live. (a) Wrap only `build_response`: attractive
because that is where the known fault (`urlparse`) is, and it would leave the
version/header code visibly outside a `try`. It loses because
`await _app_version_value()` is a real host call into
`spin_variables_variables_3_0_0.get`, documented to raise `Err`, and a
`variables.get` failure is *exactly* one of the four causes the TASKS entry
names. (b) Wrap only the `await`: loses for the mirror-image reason. The whole
body is four statements; the reader's question is *"can any response leave this
component without headers?"*, and only one `try` spanning everything answers
it. This also makes the guard survive the entry's own second trigger — *"the
next change that puts real logic in `handle_request`"* — without anyone
remembering to widen it.

**2. Doing nothing / honouring the unfired trigger — overridden by the user,
recorded for completeness.** The trigger is a reasonable gate in general, but
research finding (2) above suggests it may already have fired invisibly: the
`urlparse` hole is remotely reachable, and its symptom is a blank page plus a
traceback in a log stream nobody reads. A trigger that can only fire via a
human noticing a blank page is a poor gate for a change whose whole purpose is
to stop failures being noticed that way.

**3. Omitting `at=<file>:<line>` and shipping `etype` + `msg` only — rejected,
and this is the decision the TASKS entry explicitly flagged as open.** It is
attractive: it avoids a second Python copy of `exc_location`, keeps
`gui-pages/obs.py` smaller, and `etype`+`msg` is often enough. It loses on
research finding (1). Today an unhandled exception prints a **full traceback**
via the SDK; catching it removes that traceback. Without `at=`, this change
would therefore be a net *diagnostic regression* — trading a complete frame
chain for `ss comp=gui-pages ev=exc etype=ValueError msg=Invalid IPv6 URL`,
which cannot distinguish a `ValueError` from `urlparse` from one raised in
`_read_file` or in the SDK. `at=` is what keeps the trade positive, and it is
provably data-free (a basename and an integer). The copy itself is eight lines
of stdlib traceback walking with zero `api` coupling (confirmed by reading
`api/obs.py:337-350`), which is a small price for the field that makes the line
worth emitting.

**4. Reporting the outermost frame, or the innermost frame belonging to one of
our own modules, instead of the innermost frame — rejected.** Genuinely
attractive here in a way it is not for `api`: measured, a `urlparse` failure
reports `at=parse.py:525`, a stdlib file, where `at=routing.py:104` would name
our own call site. It loses because it would diverge from `api/obs.exc_location`
— the function this plan is explicitly mirroring — for a marginal gain, and
because "our own modules" is a list that has to be maintained and that goes
silently stale when a module is added. In practice `etype`+`msg`+innermost
`at=` already identifies the call site unambiguously for every case examined
(`parse.py` + `Invalid IPv6 URL` can only be the `urlparse` in
`build_response`; an SDK file + `Err/Error_*` can only be the `variables.get`).
Revisit only if a real line is ever ambiguous in practice.

**5. A distinct `ev` value (`ev=handler_exc`, `ev=unhandled`) instead of reusing
`ev=exc` — rejected.** Attractive because `ev` is currently near-unique per
component-and-condition, and a distinct value would make
`grep 'ev=exc'` mean exactly one component. It loses because reusing an `ev`
across components with per-component field sets is already established
precedent — `ev=record_unreadable` is emitted by both `redirect` (with a Go
`%T` etype) and `api` (with a Python class name), documented in CLAUDE.md as
deliberate — and because the operator's actual question is *"did any handler
blow up anywhere?"*, which one grep should answer. `comp=` disambiguates. The
TASKS entry also specifies `ev=exc` by name.

**6. A shared `obs.py` between `api` and `gui-pages`, or importing
`api/obs.py`, instead of copying `exc_location`/`error_type_name` — rejected,
and not actually available.** The two components are independent `uv` projects
with separate lockfiles and no shared package, and componentize-py compiles
`app.py` with only its own component directory on the import path — a
cross-component import would not survive the build. A new shared distribution
packaged for both is disproportionate for sixteen lines of stdlib code. The
copies are deliberately **not pinned** against `api`'s, the same standing rule
the two `sanitize_error_message` implementations already carry: divergence here
produces differently-shaped log lines and nothing else, unlike `keys.go`'s
prefixes, whose divergence fails silently at runtime.

**7. Sharing the existing `_should_emit_failure` dedup set between
`ev=page_read_failed` and `ev=exc` — rejected in favour of two independent
sets.** Attractive on precedent: `redirect` shares one 32-entry per-instance map
between `ev=kv_fail` and `ev=record_unreadable`, relying on disjoint key
prefixes, and reuse-for-its-own-sake is the default here. It loses on an
asymmetry `redirect` does not have. `ev=page_read_failed`'s key space is
bounded **by construction** — `route` and `file` come from ten module-literal
`ROUTES` entries, and a given drift produces one tuple — while `ev=exc`'s key
includes `at` and a `msg` derived from an arbitrary exception, which may embed
request-derived text (a `KeyError` on a header name; a `ValueError` quoting a
value). One shared 32-key budget therefore lets the unbounded kind permanently
silence the bounded one for the instance's entire life — and the bounded one is
the kind that reports a *deploy defect an operator must fix*. Two sets cost one
extra module-level closure and raise the per-instance worst case from 32 lines
to 64, which is bounded either way. The `"exc"`/`"page_read_failed"` key
prefixes are kept regardless, so a future decision to merge them cannot
silently collide.

**8. Re-raising after logging, or additionally printing the SDK's traceback —
rejected.** Re-raising hands the request back to the SDK's bare `except:`,
which returns the header-less 500 — i.e. it un-does the entire change.
Printing our own traceback alongside the line violates CLAUDE.md's standing
rule for these diagnostics (*"never a traceback, never source text"*), which
exists because a traceback is unbounded, multi-line (so it breaks the one-fault
-one-`ss `-line grep contract), and prints source text into a 7-day retention
window. The bounded `at=` field is the sanctioned substitute; see #3 for why it
is not optional.

**9. Awaiting `_app_version_value()` inside the `except` arm so the fallback
always carries a fresh `X-SS-Version` — rejected.** Attractive because the
cached-global read can render `unknown` on a fresh instance's first failing
request. It loses because `_app_version_value()` performs a host call that is
itself a candidate for the exception being handled; a second raise inside the
handler lands straight back in the SDK's header-less 500 — the exact outcome
this arm exists to prevent. The chosen arm is raise-free by construction: a
module-global read, a dict literal, and two dataclass constructions. An
occasional `unknown` is a strictly better failure than an occasional bare 500.

**10. Fixing the `urlparse` `ValueError` at source — guarding it in
`build_response` and answering `404` (or `400`) — deferred, not rejected on the
merits.** It is the better *product* answer: a nonsense request target is not a
server error. It is out of scope here because it changes a status code, which
the non-goals forbid, and because the honest order is guard → observe → decide:
after this change the condition is greppable, so a decision can rest on whether
it actually occurs rather than on this planner's guess. Filed under Future work
with that trigger.

**11. Including a sanitized `route` on the `ev=exc` line — rejected.** See "The
log line" above for the full argument: the only value available is
request-controlled, may be the thing that raised, and would render
`[invalid_path]` for most catch-all traffic. Rejecting it also means this plan
adds no new use of `sanitize_path_for_log`, so its existing CI guard
(`test_every_routes_value_is_log_safe`) keeps covering the whole of its live
surface.

## Tasks

The exact lines appended to `TASKS.md` under
`## gui-pages unhandled-exception guard`. `TASKS.md` is authoritative;
checkboxes are ticked only there.

```
- [ ] Add error_type_name, exc_location and unhandled_exception_line to gui-pages/obs.py — file(s): gui-pages/obs.py — done when: all three exist with ZERO `spin_sdk` imports and no new module constant; `error_type_name` is duck-typed via `getattr(exc, "value", None)` with no import of `componentize_py_types` (which is absent from this venv); `exc_location` walks to the INNERMOST traceback frame and returns `"<basename>:<lineno>"` or `"-"`; `unhandled_exception_line` returns `(line, dedup_key)` with fields `comp, ev, etype, at, [msg_truncated], msg` in that order and a dedup key of `"exc" + _SEP + etype + _SEP + at + _SEP + msg`; `page_read_failed_line`, `sanitize_error_message`, `sanitize_path_for_log`, `render_failure_line` and `make_dedup` are all unchanged; and `cd gui-pages && uv run python -c "import obs"` succeeds.
- [ ] Cover the three new obs functions under pytest — file(s): gui-pages/tests/test_obs.py — done when: `cd gui-pages && uv run pytest` passes with new tests pinning the plan's Tests items 1-13, notably that `exc_location` reports the INNER of two nested frames and returns a basename with no `/`, that `error_type_name` renders `Err/Error_Undefined` for a duck-typed stand-in and a bare class name otherwise, that `msg` is the final field with nothing after it, that a `\n`-bearing message yields a single-line rendering, that an empty message renders `msg=-`, that two exceptions differing only in raise line produce different dedup keys, that the `ev=exc` and `ev=page_read_failed` dedup key spaces are disjoint (neither a prefix of the other), and that the line contains no `route=`, `method=`, `op=` or `ns=` field.
- [ ] Extract routing.internal_error_response() as the single 500 constructor — file(s): gui-pages/routing.py, gui-pages/tests/test_routing.py — done when: `internal_error_response()` returns `Response(500, {**SECURITY_HEADERS, "content-type": "text/html; charset=utf-8"}, ERROR_PAGES[500])`, `build_response`'s `except OSError` branch returns it instead of constructing a `Response` inline, `routing.py` still imports nothing from `spin_sdk` and nothing from `obs`, and `uv run pytest` passes with a new test asserting the read-failure 500's status, headers AND body all equal `internal_error_response()`'s — with all four pre-existing `on_read_error` tests passing UNMODIFIED.
- [ ] Wrap gui-pages' handle_request in the unhandled-exception catch-all (depends on the two tasks above) — file(s): gui-pages/app.py — done when: the whole `handle_request` body sits in one `try`, the `except Exception as exc:` arm calls `_report_unhandled_exception(exc)` inside its own `try/except Exception: pass` and then returns `internal_error_response()`'s status/body with `x-ss-version` taken from the CACHED `_app_version` global (never re-awaited); the module holds TWO independent dedup sets, `_should_emit_page_read_failure` (renamed from `_should_emit_failure`) and `_should_emit_exc`, with a comment recording why they are not shared; `_report_read_error` and the shipped `ev=page_read_failed` path are byte-for-byte unchanged in behaviour; and `spin up --build` completes with no change to `spin.toml` and no new entry under `[component.gui-pages.variables]`.
- [ ] Correct errorpages.py's now-incomplete "permanent until a redeploy" comment without changing a served byte — file(s): gui-pages/errorpages.py — done when: the comment above `INTERNAL_ERROR_HTML` names BOTH conditions that now reach the page (`ev=page_read_failed` drift and `ev=exc` from the catch-all), records that they are answered identically via `routing.internal_error_response`, and explains why the copy still says neither "we've been notified" nor "try again"; `git diff gui-pages/errorpages.py` shows changes to comment lines only; and `uv run pytest` still passes unchanged.
- [ ] Document the gui-pages ev=exc line in CLAUDE.md — file(s): CLAUDE.md — done when: the "Observable KV failures" vocabulary paragraph records that `ev=exc` is now emitted by `gui-pages` as well as `api`, from `handle_request`'s catch-all, carrying `etype` (via a `gui-pages`-local copy of `error_type_name`, deliberately unpinned against `api`'s) and `at=<file>:<line>` but deliberately NO `route`/`method` (the only available value is the request-controlled URI, which in the `urlparse` case is what raised) and no `op`/`ns`/`msg_redacted`; the per-instance dedup paragraph records that `gui-pages` holds TWO independent 32-key sets rather than one shared budget, and why; and the "Security response headers" section records that an unhandled exception in `gui-pages` used to be answered by the Spin SDK's own bare `except:` with an EMPTY `Fields()` and no body — so "every response from `redirect`, `api` and `gui-pages` sets …" was previously false on that path and is now true.
- [ ] End-to-end manual verification of the gui-pages catch-all — file(s): (none — verification step) — done when: against a live `spin up --build`, an unhandled exception in `handle_request` yields a `500` carrying the styled error page, all of `x-content-type-options`/`referrer-policy`/`x-frame-options`/`strict-transport-security`/`content-security-policy`/`x-ss-version`, and exactly ONE `ss comp=gui-pages ev=exc etype=… at=… msg=…` line across THREE identical requests on a settled instance; the observed `etype`/`at`/`msg` are recorded verbatim in TASKS.md (settling whether Spin forwards a `//[`-prefixed request target and what `at=` names under componentize-py); a `ev=page_read_failed` probe in the SAME run still emits its own line (proving the two dedup sets coexist); every real page still loads with no new stderr line and `/nope` still returns the styled 404; and every temporary probe is reverted with `git diff gui-pages/` showing only the intended changes.
```

## Critical files

- `docs/plans/gui-pages-unhandled-exception-guard.md` (new)
- `gui-pages/obs.py`
- `gui-pages/tests/test_obs.py`
- `gui-pages/routing.py`
- `gui-pages/tests/test_routing.py`
- `gui-pages/app.py`
- `gui-pages/errorpages.py` (comment only — zero served bytes change)
- `CLAUDE.md`
- `TASKS.md`

Explicitly **not** touched: `spin.toml`, `Jenkinsfile`, `redirect/`, `api/`,
`gui/`, `gui-pages/nonpages.py`, `gui-pages/tests/test_no_inline_code.py`,
`gui-pages/tests/test_manifest_components.py`,
`gui-pages/tests/test_nonpages.py`.

## Verification

1. Baseline, before any change (it was `155 passed in 0.20s` at planning time):

   ```bash
   cd gui-pages && uv run pytest
   ```

2. After every code task, the component suite — the only suite this change can
   affect:

   ```bash
   cd gui-pages && uv run pytest
   ```

3. Confirm the new code stays pure and host-importable:

   ```bash
   cd gui-pages && uv run python -c "import obs, routing; print(obs.exc_location(ValueError('x')))"   # expect: -
   grep -n "spin_sdk" gui-pages/obs.py gui-pages/routing.py                                          # expect no matches
   grep -n "componentize_py_types" gui-pages/obs.py gui-pages/tests/test_obs.py                      # expect no matches
   ```

4. Confirm the other two suites are untouched (they should be — no file in
   either component changes — but CI runs all three, so prove it):

   ```bash
   cd redirect && go test ./linkgate/...     # NEVER go test ./... — fails by design
   cd api && uv run pytest
   ```

5. **Live, zero-residue attempt first.** From the repo root:

   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```

   Wait until the instance has settled (the 2026-08-28 run recorded that a
   just-built instance may cycle through several Wasm instances and degrade
   dedup — load one normal page first, then probe). Then, watching the
   `spin up` terminal for stderr:

   ```bash
   curl -sS -i --path-as-is 'http://localhost:3000//[::1'
   curl -sS -o /dev/null --path-as-is 'http://localhost:3000//[::1'
   curl -sS -o /dev/null --path-as-is 'http://localhost:3000//[::1'
   ```

   If the first response is a `500` from this component (styled body, full
   header set), the UNCONFIRMED question is answered *yes* and no code edit is
   needed at all. If it is a `400` from Spin/Hyper with no component headers
   and no stderr line, Spin rejected the target before the component saw it —
   record that, and use step 6 instead.

6. **Live fallback, if step 5 never reaches the component.** Two temporary,
   surgical edits, both reverted afterwards:

   - in `gui-pages/app.py`'s `_read_file`, at the top of the body:
     `if relative_path == "login.html": raise RuntimeError("exc probe")`
     — a `RuntimeError`, **not** an `OSError`, so `build_response`'s own
     branch cannot catch it and it must reach the new catch-all;
   - in `gui-pages/routing.py`'s `ROUTES`, one extra entry
     `"/logtest.html": "no-such-page.html"` — the `ev=page_read_failed` probe,
     included in the same run so both line kinds are exercised together.

   Rebuild and re-run `spin up --build`, then:

   ```bash
   curl -sS -i http://localhost:3000/login.html
   curl -sS -o /dev/null http://localhost:3000/login.html
   curl -sS -o /dev/null http://localhost:3000/login.html
   curl -sS -i http://localhost:3000/logtest.html
   curl -sS -i http://localhost:3000/dashboard.html
   curl -sS -i http://localhost:3000/nope
   ```

   A pass is **all** of:
   - `/login.html` returns `500` with `content-type: text/html; charset=utf-8`,
     the styled "Something went wrong" body, and all six of
     `x-content-type-options`, `referrer-policy`, `x-frame-options`,
     `strict-transport-security`, `content-security-policy`, `x-ss-version`
     — **not** the empty-body, header-less 500 the SDK produced before;
   - **exactly one** `ss comp=gui-pages ev=exc …` line across the three
     `/login.html` requests, with field order `comp ev etype at [msg_truncated]
     msg`, `msg` last, nothing after it, and **no** `route=`/`method=`/`op=`/
     `ns=` anywhere in it;
   - **no Python traceback** on stderr for those requests — this is the
     positive confirmation that the SDK's own `except:` no longer fires;
   - `/logtest.html` emits its own `ss comp=gui-pages ev=page_read_failed …`
     line, unchanged in shape from the 2026-08-28 recording
     (`etype=FileNotFoundError errno=44`), proving the two dedup sets coexist
     and neither suppressed the other;
   - `/dashboard.html` returns `200` and `/nope` returns the styled `404`, both
     with **no** new stderr line;
   - the observed `etype`, `at` and `msg` are recorded verbatim in `TASKS.md`.

7. Revert every temporary edit and confirm the diff is clean:

   ```bash
   git diff gui-pages/app.py        # only the catch-all, the two dedup sets and the reporter
   git diff gui-pages/routing.py    # only internal_error_response and the branch that returns it
   git diff --numstat TASKS.md      # only checkbox lines + the recorded verbatim line
   ```

## Out of scope / follow-ups

- **Answering a malformed request target with `404`/`400` instead of `500`.**
  `urlparse("//[")` raising a `ValueError` is a *client* error being reported as
  a server error. Deferred per Trade-offs #10; added to `TASKS.md` "Future work
  (not scheduled)". Trigger: any `ev=exc` line whose `at=` names
  `parse.py` actually appearing in a real log — which this change is what makes
  observable.
- **A `log_level=summary` line for `gui-pages`.** Already filed by
  `docs/plans/gui-pages-failure-logging.md`; unchanged by this plan, and its
  stated prerequisite (deciding how the attacker-controlled 404 `route` field
  is collapsed) is the same blocker that keeps `route` off this plan's `ev=exc`
  line.
- **Cross-language / cross-component pinning of `exc_location`,
  `error_type_name` and `sanitize_error_message`.** Deliberately not done, on
  the rule CLAUDE.md already states for the existing pair. Not filed — a
  settled position, not a deferral.
- **Alerting on `ev=exc`.** Out of scope, as for every other failure line: this
  app has no alerting anywhere, and CLAUDE.md's `ev=kv_fail` note is explicit
  that naively paging on a failure line is a mistake.
- **The Spin SDK's own bare `except:`.** It still backstops anything raised
  *outside* `handle_request` — in `Handler.handle`'s method decoding, body
  read, or the spawned `_copy` of the response body. Those paths remain
  header-less on failure and are not ours to fix (third-party, and reached
  before or after any code we control). Not filed; recorded here so the next
  reader does not mistake this change for a total guarantee.
- **`gui`, the prebuilt `spin_static_fs.wasm` component.** Still has no
  instrumentation and cannot get any. Unchanged by this plan.
