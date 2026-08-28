# gui-pages Page-Read Failure Logging

## Context

`gui-pages` is the catch-all component (`route = "/..."`) that serves the GUI's
HTML pages from a fixed allowlist and attaches this app's security response
headers. `routing.build_response`'s `except OSError` branch exists for one
condition — a **`ROUTES`-vs-filesystem drift**, a page named in
`routing.py`'s `ROUTES` dict whose file cannot actually be read — and when it
fires it serves the styled 500 page (`errorpages.INTERNAL_ERROR_HTML`) with
every security header intact and **records nothing anywhere**. The component
has no logging seam at all: it imports `spin_sdk.variables` solely to read
`app_version` for the `X-SS-Version` header.

That silence is why the 500 page's copy says *"let whoever runs this service
know"* — the visitor is currently a better signal than the logs. The comment
above `INTERNAL_ERROR_HTML` in `gui-pages/errorpages.py` says so in as many
words:

```python
# No "we've been notified": gui-pages has no logging at all (CLAUDE.md,
# "Toggleable structured logging"), so a ROUTES-vs-filesystem drift produces
# no signal anywhere. Naming the operator is the honest ask.
```

Motivating entry: `TASKS.md` line 517, *"`gui-pages` has no instrumentation at
all, so a `ROUTES`-vs-filesystem drift `500` is invisible to the operator"*,
raised 2026-08-23 while planning `docs/plans/gui-pages-error-pages.md`. Its own
stated trigger — *"a `500` actually observed from the catch-all"* — has **not
fired**. This is being built anyway on the user's direct instruction, the same
override precedent as `docs/plans/disposition-unreadable-logging.md`
(2026-08-25) and `docs/plans/api-record-unreadable-diagnostics.md` (2026-08-27).
That entry is annotated accordingly, in the established
`[PICKED UP … despite its trigger NOT having fired …]` style.

Confirmed decisions (settled by the user before planning):

- Build it now despite the unfired trigger; do not re-litigate the trigger.
- Do not touch `redirect/` or `api/` at all.
- Do not add Collector-style per-request timing to `gui-pages` unless research
  shows it is genuinely warranted. (Research says it is not — see Trade-offs #1.)
- Do not invent a new `log_level` value beyond the existing `off`/`summary`
  unless justified. (None is invented; in fact `gui-pages` reads no `log_level`
  at all — see Trade-offs #2.)
- The line carries the `ROUTES` filename, never the attacker-controlled
  requested URI.

**Two corrections to the TASKS.md entry, found by reading the code, that change
the shape of the work.** They are the reason this plan is much smaller than the
entry's *"cheap to want and not cheap to build"* warning predicts:

1. **The entry's proposed gating is wrong on the merits.** It says the line
   should sit *"behind the same `log_level`/`X-SS-Debug` gating the other two
   components use."* Since `docs/plans/observable-kv-failures.md`
   (`TASKS.md` § "Observable KV failures", which predates the entry), a
   **failure** line in this app is unconditional by doctrine — `ev=kv_fail`,
   `ev=exc` and `ev=record_unreadable` are all emitted *"independent of
   `log_level` and of `X-SS-Debug`, because the entire point is to catch a fault
   that has never been reproduced on demand under tracing."* Gating a diagnostic
   behind the toggle it exists to work without defeats the point. Only the
   *summary* line is gated, and `gui-pages` is not getting one.
2. **Therefore `gui-pages` needs no new Spin variable and `spin.toml` is not in
   scope.** The entry's cost estimate ("a whole logging seam ... a component
   that currently reads no Spin variable except `app_version`") assumed the
   gating in (1). Without it, the work is one new pure module, one extra
   optional parameter on `build_response`, and four lines in `app.py`.

## Key technical facts confirmed during research

- **`routing.ROUTES` has 10 path keys mapping to 8 distinct filenames**, all
  module-level string literals: `index.html`, `login.html`, `dashboard.html`,
  `admin/index.html`, `admin/users.html`, `admin/store-maintenance.html`,
  `admin/url-policy.html`, `links/detail.html`. Read directly from
  `gui-pages/routing.py:23-34`.
- **Neither the filename nor the matched path is attacker-controlled.**
  `resolve_file(path)` is `ROUTES.get(path)`; a non-`None` return means `path`
  compared equal to one of those 10 literals, and `str.__eq__` is
  character-identity, so at the point the `except OSError` branch runs, `path`
  **is** one of the 10 literals and `filename` **is** one of the 8. The raw
  requested URI never reaches the read: `build_response` uses
  `urlparse(uri).path` only as a dict key. Confirmed by reading
  `gui-pages/routing.py:89-121`.
- **Every `ROUTES` key and value is already logfmt-safe** — no spaces, no
  control characters, `^[A-Za-z0-9._/-]+$` throughout. This is a property that
  can silently stop holding when someone adds a route, which is why the plan
  pins it with a test *and* sanitizes at runtime (Trade-offs #4).
- **An `OSError` message from a failed `open()` embeds the resolved filesystem
  path and nothing else.** Measured on the component's own host venv
  (`cd gui-pages && uv run python -c "..."`, Python 3.14.6):
  `str(FileNotFoundError)` for `open("/gui/login.html","rb")` is
  `"[Errno 2] No such file or directory: '/gui/login.html'"`, with
  `type(exc).__name__ == "FileNotFoundError"`, `exc.errno == 2`,
  `exc.filename == "/gui/login.html"`. The path is `GUI_DIR + "/" + <ROUTES
  value>`, so it carries no request data.
- **Copying `api/obs.py`'s `sanitize_error_message` verbatim would actively
  destroy signal here.** Measured against its real `_KEY_SHAPED_PATTERN`:

  | input | after the api key-shaped rule |
  |---|---|
  | `[Errno 2] No such file or directory: '/gui/login.html'` | unchanged |
  | `wasi:filesystem/types@0.2.0#read: access denied` | `[key:wasi] access denied` |
  | `key-value error: internal server error` | unchanged |

  The `wasi:`-prefixed shape is **UNCONFIRMED** as something componentize-py
  actually surfaces for a file read (the measured `open()` failure produced a
  plain `[Errno N]` message) — confirming it would take a real WASI-side
  filesystem fault under `spin up`. But the direction of the risk is settled
  regardless: in a component that can never touch a KV key or a PBKDF2 hash, the
  key-shaped rule protects nothing and can only mangle. See Trade-offs #3.
- **Errno-to-subclass mapping under componentize-py is UNCONFIRMED.** On host
  Python, `errno.ENOENT == 2` and `open()` raises `FileNotFoundError`; wasi-libc
  numbers `ENOENT` as 44, and whether the WASI CPython build maps it to the same
  subclass has not been verified. Confirming it takes a live `spin up` run
  against a deliberately-broken `ROUTES` entry (Verification step 5). This is
  precisely why the line carries an explicit `errno=` field alongside `etype=`
  — if the mapping is off, `etype` degrades to a bare `OSError` and `errno`
  becomes the only discriminator.
- **`print(line, file=sys.stderr)` reaches the Spin log from a componentize-py
  component.** Not assumed — `api/app.py:201` and `:276` already do exactly this
  and their lines are the shipped `ev=`/summary output.
- **Module-level mutable state persists for a Wasm instance's lifetime under
  componentize-py.** `api/app.py`'s `_obs_log_level` / `_app_version` caches and
  `gui-pages/app.py`'s own `_app_version` are built on this, and CLAUDE.md
  states it as the reason a Spin variable can be read once. Under a
  one-instance-per-request regime the dedup degrades to no dedup, which is
  already the accepted position for `redirect`'s package-scope map (see the
  comment beside `failureDedupMu` in `redirect/main.go`).
- **Baseline is green.** `cd gui-pages && uv run pytest` → `135 passed in 0.08s`
  before any change.
- **`tests/test_no_inline_code.py` globs `redirect/*.html` and `gui/**/*.js`
  only** — it does not glob `gui-pages/*.py`, so a new sibling module cannot
  trip it. Confirmed by reading its `REDIRECT_TEMPLATES` / `GUI_DIR.rglob` set-up.
- **`spin.toml`'s `[component.gui-pages]` declares only `app_version`** under
  `variables`, and `watch = ["*.py", ...]` already covers a new sibling module.
  `gui-pages/tests/test_manifest_components.py` asserts the manifest declares
  exactly `{redirect, api, gui, gui-pages}` — untouched, since this plan makes
  no manifest change.

## The log line

One new line kind, `ev=page_read_failed`, emitted to stderr on failure only,
unconditionally:

```
ss comp=gui-pages ev=page_read_failed route=/login.html file=login.html etype=FileNotFoundError errno=2 msg=[Errno 2] No such file or directory: '/gui/login.html'
```

Field order, all load-bearing and matching the existing failure-line convention
(`comp`, `ev`, `route`, then event-specific fields, then `msg` last):

| field | value | notes |
|---|---|---|
| `comp` | `gui-pages` | fixed literal; a fourth component value alongside `redirect`/`api` |
| `ev` | `page_read_failed` | fixed literal; a fourth `ev` alongside `kv_fail`/`exc`/`record_unreadable` |
| `route` | the matched `ROUTES` **key** | e.g. `/` vs `/index.html` — both map to `index.html`, and the difference is "the landing page is dead" vs "a bookmark is dead" |
| `file` | the `ROUTES` **value** | the thing an operator actually has to fix |
| `etype` | `type(exc).__name__` | a **third, independent** `etype` vocabulary — see below |
| `errno` | `exc.errno`, **omitted** when not an `int` | hedges the unconfirmed errno mapping |
| `msg_truncated` | `1`, omitted otherwise | before `msg`, never after |
| `msg` | sanitized `str(exc)`, or `-` | **always last; nothing may ever be appended after it** |

**No `op`/`ns` field, deliberately** — no KV operation failed, and this
component performs none. Anyone filtering `ev=kv_fail` must not see these, and
anyone counting KV failures must not count them. This is the same rule
`linkgate.RecordUnreadableLine` already states for `ev=record_unreadable`.

**No `msg_redacted` field, ever, by construction** — see Trade-offs #3. Its
absence is correct rather than a gap: the `grep 'msg_redacted=1'` contract
exists to answer *"does an Akamai KV error string embed a key?"*, and
`gui-pages` makes no KV calls.

**`etype`'s vocabulary is per-`ev` and never global**, which CLAUDE.md already
states as a rule. `ev=kv_fail` spells it `other`/`access_denied`, `api/obs.py`
spells it `Err/Error_Other`, `ev=record_unreadable` spells it `*json.SyntaxError`
(Go's `%T`), and this line spells it as a Python exception class name. Four
vocabularies, none pinned against another.

**Greppability:** `grep 'ev=page_read_failed'` cannot collide with a summary
line (which carries no `ev` field at all) or with any other `ev` value.

## New module: `gui-pages/obs.py`

New file, **zero `spin_sdk` imports**, host-importable under plain pytest — the
same contract `routing.py`, `errorpages.py` and `nonpages.py` already hold.

**Named `obs.py`, never `logging.py`** (nor `time.py`/`json.py`). CLAUDE.md's
rule applies here identically to `api/`: componentize-py compiles `app.py`
alongside its siblings with the component's own directory on the import path, so
a module shadowing a stdlib name would break every stdlib module that imports
the real one, for the whole component.

Constants:

```python
MAX_ERROR_MESSAGE_CHARS = 200      # mirrors api/obs.py and linkgate.MaxErrorMessageChars
MAX_FAILURE_DEDUP_KEYS = 32        # mirrors redirect/main.go's maxFailureDedupPairs
```

Functions:

```python
def sanitize_error_message(text: str) -> tuple[str, bool]:
    """(sanitized, truncated). Two rules, in order:
    (a) replace control characters/newlines with "_", so one failure is always
        one line and no message can forge a second "ss "-prefixed line;
    (b) truncate to MAX_ERROR_MESSAGE_CHARS, setting the flag.
    Deliberately NARROWER than api/obs.py's three-rule sanitizer — see the
    plan's Trade-offs #3 for why the key-shaped and pbkdf2 rules are absent."""


def sanitize_path_for_log(value: str) -> str:
    """value unchanged if it matches ^[A-Za-z0-9._/-]{1,128}$, else the fixed
    placeholder "[invalid_path]", carrying none of the original bytes."""


def render_failure_line(fields: list[tuple[str, str]]) -> str:
    """One "ss "-prefixed logfmt line, fields in order, with NOTHING appended
    after them — a separate function from any future summary renderer, exactly
    as api/obs.render_failure_line is, so nothing can ever land after msg."""


def page_read_failed_line(path: str, filename: str,
                          exc: BaseException) -> tuple[str, str]:
    """Returns (line, dedup_key). Every decision lives here, where it is
    host-testable; the caller does nothing but consult a dedup set and write the
    string. Modelled on linkgate.RecordUnreadableLine, which returns the same
    pair for the same reason (package main / app.py are not host-testable)."""


def make_dedup(max_keys: int = MAX_FAILURE_DEDUP_KEYS):
    """Returns should_emit(key) -> bool, closing over ONE set. First sighting
    of a key returns True and records it; a repeat returns False; once max_keys
    distinct keys are held, every further key — including a genuinely novel one
    — returns False. A factory rather than a module-level set so tests can build
    independent instances with no cross-test contamination."""
```

Dedup key construction, inside `page_read_failed_line`:

```python
"page_read_failed" + SEP + route + SEP + file + SEP + etype + SEP + errno + SEP + msg
```

with `SEP = "\x00"`, mirroring `linkgate.dedupKeySep`. NUL is safe because
`sanitize_error_message` replaces every control character with `_`, so no part
can contain one and the parts can never be ambiguously re-split or collide by
concatenation. The `"page_read_failed"` literal prefix keeps this key space
disjoint from any future line kind sharing the same dedup set — the same move
`linkgate.RecordUnreadableDedupKey` makes.

The key is **everything the line renders**, which is the rule
`api/obs.make_failure_reporter` already encodes ("two lines that differ only in
an `extra` field are two events, not one"). Concretely: `/` and `/index.html`
both failing on `index.html` are two events, which is correct and is bounded by
the 10-key route space.

## `gui-pages/routing.py` changes

One new **optional** parameter, defaulting to `None`, so every existing caller
and all four existing `build_response` test call sites are unaffected:

```python
def build_response(uri: str, read_file: Callable[[str], bytes],
                   on_read_error: Optional[Callable[[str, str, BaseException], None]] = None) -> Response:
```

Only the `except OSError` branch changes:

```python
    try:
        body = read_file(filename)
    except OSError as exc:
        # A diagnostic must never be able to break the response it is
        # diagnosing. This component's entire job is attaching SECURITY_HEADERS;
        # a reporter that raised (a closed stderr, a bug in obs.py) would turn a
        # styled, header-bearing 500 into an unhandled exception with no headers
        # at all — the exact failure this try/except was added to prevent.
        if on_read_error is not None:
            try:
                on_read_error(path, filename, exc)
            except Exception:
                pass
        return Response(500, {**SECURITY_HEADERS, "content-type": "text/html; charset=utf-8"}, ERROR_PAGES[500])
```

`routing.py` gains **no import of `obs`** and no `sys` import. It stays a
routing module that reports a failure through an injected callable, exactly the
seam its own docstring already praises for `read_file`. The "where does the line
go" decision belongs to `app.py`.

`resolve_file`, `ROUTES`, `SECURITY_HEADERS`, `nonpages` handling and the 404
branch are all untouched. No response status, header or body changes anywhere.

## `gui-pages/app.py` changes

Four additions and one changed call. This is the only untestable code in the
change, and it is kept to the minimum the repo's convention allows.

```python
import sys

from spin_sdk import variables
from spin_sdk.http import Handler, Request, Response

import obs
from routing import build_response
```

```python
# docs/plans/gui-pages-failure-logging.md. Module scope, so the dedup budget
# spans this Wasm instance's whole life rather than one request — a
# ROUTES-vs-filesystem drift is PERMANENT until a redeploy, so a per-request
# dedup (api/obs.make_failure_reporter's model) would bound nothing across
# requests and a drifted page would re-log on every single visit.
#
# This is NOT the forbidden shared-collector pattern, despite the shape. A
# shared Collector is forbidden because it ACCUMULATES per-request statistics
# that must never be attributed to the wrong request. This set accumulates
# nothing about any request, and correctness does not depend on which
# concurrently dispatched request wins the race to insert a key — the tuple gets
# logged once for the life of this instance, which is exactly the deduplication
# intended. Same reasoning as redirect/main.go's failureDedupSeen.
_should_emit_failure = obs.make_dedup()


def _report_read_error(path: str, filename: str, exc: BaseException) -> None:
    """Injected into build_response as on_read_error. Unconditional — emitted
    regardless of any log toggle, because this fault has never been observed and
    a diagnostic gated behind a toggle nobody has turned on records nothing."""
    line, dedup_key = obs.page_read_failed_line(path, filename, exc)
    if _should_emit_failure(dedup_key):
        print(line, file=sys.stderr)
```

and in `handle_request`:

```python
        result = build_response(request.uri, _read_file, _report_read_error)
```

`_read_file`, `_app_version_value` and the `X-SS-Version` header logic are
untouched.

## `gui-pages/errorpages.py` change (comment only, zero served bytes)

The comment above `INTERNAL_ERROR_HTML` becomes false the moment this ships. It
must be corrected, and the served copy must **not** change — see Trade-offs #5.
Replace it with something to this effect:

```python
# No "we've been notified": a ROUTES-vs-filesystem drift now emits one
# ev=page_read_failed line to stderr (docs/plans/gui-pages-failure-logging.md),
# but nothing monitors that stream, so "we've been notified" would still be a
# claim this app cannot honour. Naming the operator remains the honest ask. No
# "try again" either — this drift is permanent until a redeploy, not transient.
```

`NOT_FOUND_HTML`, `INTERNAL_ERROR_HTML`, `_SHELL`, `_render` and `ERROR_PAGES`
are otherwise untouched, so `tests/test_no_inline_code.py`'s coverage of
`ERROR_PAGES` continues to pass unchanged.

## Tests

`gui-pages/tests/test_obs.py` (new) — everything above is pure, so the only
thing that escapes host coverage is the four lines in `app.py`:

1. `sanitize_error_message` replaces `\n`, `\r` and `\x00` with `_` — pinned as
   *"a message containing a newline cannot forge a second `ss `-prefixed line"*,
   asserted on the rendered line, not just the sanitizer's return.
2. Truncation: a 250-char message truncates to exactly 200 with
   `truncated is True`; a 199-char one is unchanged with `truncated is False`.
3. `sanitize_path_for_log` returns `admin/store-maintenance.html` and
   `/admin/url-policy.html` unchanged; returns `[invalid_path]` for a value
   containing a space, a newline, a non-ASCII character, an empty string, and a
   129-character value.
4. **`test_every_routes_value_is_log_safe`** — imports `routing.ROUTES` and
   asserts `sanitize_path_for_log(k) == k` and `sanitize_path_for_log(v) == v`
   for every key and value. This is the guard that makes the runtime sanitizer a
   fallback rather than a load-bearing control, and it is what catches a future
   route added with an unsafe filename at CI time instead of at log time.
5. `page_read_failed_line` for a real `FileNotFoundError` renders the exact
   expected field sequence, in order, and `line.startswith("ss ")`.
6. `msg` is last: `line.rindex(" msg=")` is greater than the index of every
   other field, and no `=` -delimited field follows it.
7. `errno` is omitted entirely when `exc.errno` is `None` and present when it is
   an `int` — asserted as `" errno=" not in line` / `" errno=2 " in line`.
8. An exception with an empty message renders `msg=-`, never `msg=`.
9. `make_dedup`: first key `True`, same key `False`; two lines differing only in
   `route` are two distinct keys; after `max_keys` distinct keys, the
   `max_keys + 1`-th novel key is `False`; two `make_dedup()` instances share no
   state.
10. `page_read_failed_line` returns a dedup key beginning with
    `"page_read_failed\x00"`.

`gui-pages/tests/test_routing.py` (extended):

11. `build_response` with a failing `read_file` **and** an `on_read_error` spy:
    the spy is called exactly once with `(path, filename, exc)` where
    `path == "/login.html"`, `filename == "login.html"` and `exc` is the raised
    `FileNotFoundError` instance; the response is still `500` with
    `ERROR_PAGES[500]` and every `SECURITY_HEADERS` entry.
12. `build_response` with a failing `read_file` and **no** `on_read_error`
    (default): still `500`, still header-bearing, no raise. Back-compat pin for
    the three-arg signature.
13. **`on_read_error` raising must not break the response** — a spy that raises
    `RuntimeError` still yields the `500` with `ERROR_PAGES[500]` and full
    `SECURITY_HEADERS`. This is the test that pins "a diagnostic must never be
    able to break the response it is diagnosing."
14. `build_response` on the **404** path and on a `nonpages` path never calls
    `on_read_error` — a spy that fails the test if invoked.

No change to `test_no_inline_code.py` (no new HTML, no served-byte change) or
`test_manifest_components.py` (no manifest change). `Jenkinsfile` is **not** in
scope: the three test commands it runs are unchanged.

## Trade-offs and rejected alternatives

**1. Giving `gui-pages` the full `log_level=summary` Collector/`Server-Timing`
seam — rejected.** Attractive because it would make all three Python/Go
components structurally identical, and because a summary line would expose the
catch-all's 404 rate and page-serving latency. It loses on three counts, the
third being fatal. (a) There is nothing to summarize: the entire content of the
other two components' summary lines is the KV block (`kv_ops`, `kv_us`,
`kv_bytes`, the per-op-type fields, `slow=`), and `gui-pages` makes zero KV
calls — `render_log_line` with `collector=None` degenerates to
`comp`/`route`/`status`/`dur_us`, where `dur_us` times one WASI read of a static
file. (b) It costs the whole seam the TASKS entry warned about: `log_level` and
`log_debug_token` in `[component.gui-pages.variables]`, an instance-cached
variable read, an `X-SS-Debug` header comparison, a `Server-Timing` + `Vary`
header pair, and a `Collector` with nothing to collect. (c) **The one field with
any content is the one field that cannot safely be logged** — a summary line
covers every request including the 404s, whose `route` is the raw
attacker-controlled requested path. Making it safe means collapsing every
unmatched path to a placeholder, at which point the line says "a 404 happened,
in N microseconds." Filed under Future work with a trigger.

**2. Gating the failure line behind `log_level`/`X-SS-Debug`, as the TASKS entry
sketches — rejected.** Attractive because it is what the entry literally asks
for and it bounds volume by default. It loses to the doctrine that
`docs/plans/observable-kv-failures.md` already settled and CLAUDE.md already
records: *"gating a diagnostic behind the very toggle it exists to catch
failures without would defeat the point."* The specific consequence here is
sharp — `log_level` must stay `off` in production, and `log_debug_token` cannot
be changed without a redeploy, so a gated line would record nothing on exactly
the deploy where the drift shipped. The volume argument that justifies gating
the summary line does not transfer: a failure line costs nothing on the success
path, and the failing path is bounded by requests to 10 known page routes, not
by the redirect hot path's 1,000+/s. Rejecting this is also what removes the
`spin.toml` change and most of the entry's cost estimate.

**3. Copying `api/obs.py`'s three-rule `sanitize_error_message` verbatim —
rejected in favour of a deliberately narrower two-rule sanitizer.** Attractive
because a third identical implementation keeps one vocabulary across the app,
preserves the `msg_redacted=1` grep contract everywhere, and means a future
`gui-pages` line that *could* carry sensitive material already has the stronger
sanitizer waiting. It loses on measurement: the key-shaped rule
(`[A-Za-z][A-Za-z0-9_-]*:[^\s'")\]]+` → `[key:<word>]`) exists because *"every
physical key this app sends is prefixed, so a host that echoes a key echoes a
prefixed one"* — and `gui-pages` sends no keys and holds no PBKDF2 hash, so both
the key-shaped and the pbkdf2 rules are **provably dead** here. Worse than dead:
measured, the key-shaped rule turns `wasi:filesystem/types@0.2.0#read: access
denied` into `[key:wasi] access denied`, deleting the operative half of a
message in a component whose only diagnostic *is* the message. The two rules
that matter — control-character scrubbing (so one failure is one line) and
truncation (so an unbounded host string is bounded) — are kept verbatim. The
cost is that `gui-pages` lines never carry `msg_redacted`, which is documented
as correct-by-construction rather than as a gap. Like the existing Go/Python
sanitizer pair, this third one is **deliberately not pinned** against either:
divergence produces differently-shaped log lines and nothing else, unlike
`keys.go`'s prefixes, whose divergence fails silently at runtime.

**4. Logging nothing but a hardcoded `route=/...` (the component's one trigger
route), or conversely logging the raw requested URI — both rejected.** The
hardcoded literal is attractive because it is provably non-attacker-derived with
zero reasoning required; it loses because `file=index.html` alone cannot
distinguish a dead landing page from a dead bookmark, and the equality argument
for the matched key is airtight (a `dict.get` hit means character identity with
a module literal). The raw URI is what the TASKS entry explicitly forbids and it
stays forbidden — it is attacker-controlled, and one component over, a `%0A`
-bearing path segment was confirmed live to forge a complete second `ss `-prefixed
log line (`TASKS.md`, 2026-08-27, the `SanitizeSlugForLog` fix). Chosen instead:
the matched `ROUTES` key, *plus* a runtime allowlist sanitizer, *plus* a CI test
that every `ROUTES` entry passes it unchanged — so the field's safety does not
depend on the equality invariant continuing to hold. This mirrors
`SanitizeSlugForLog`'s own stated reasoning: it costs nothing for a value that
already matches, and it stops the field's safety from resting on an invariant.

**5. Rewriting the 500 page's copy now that a signal exists — rejected.** The
current copy tells the visitor to *"let whoever runs this service know"*,
justified by there being no log. It is tempting to soften it to "we've been
notified." It loses because nothing monitors this stream: `spin aka logs` has a
7-day retention window that a human has to go and read, there is no alerting
anywhere in this app, and CLAUDE.md's standing position (the
`ev=kv_fail` alerting note) is that a line in stderr is not a notification.
Changing served bytes also re-opens `test_no_inline_code.py`'s coverage for no
gain. The *comment* explaining the copy is corrected instead; the copy itself is
byte-identical.

**6. Emitting to stderr directly from `routing.py` instead of through an
injected `on_read_error` callback — rejected.** Attractive because `sys` is
stdlib, so `routing.py` would keep its zero-`spin_sdk` property and the change
would be two lines with no signature churn. It loses because it makes the
routing module own a logging responsibility and a piece of instance-lifetime
mutable state, it makes every unit test of the 500 branch write to stderr
(needing `capsys` to stay quiet), and it breaks the component's own established
pattern — `read_file` is injected for exactly this reason, and the docstring
says so. The injected callback also buys test 13 (a raising reporter cannot
break the response) essentially for free.

**7. Per-request dedup (`api/obs.make_failure_reporter`'s model) — rejected in
favour of per-instance.** `api` builds a fresh reporter per request because a
single throttled 50-row bulk create can fail the same write 150 times *within*
one request; its bound is intra-request by necessity. Here the fault is the
opposite shape: at most one read failure per request by construction, but the
fault is **permanent until a redeploy**, so a per-request dedup would bound
nothing at all and a drifted `dashboard.html` would log on every dashboard load
forever. `redirect`'s per-instance model is the right one, for the reason its
own comment gives about `ev=record_unreadable`. Accepted residual: under a
cold-instance-per-request regime the dedup degrades to no dedup — already the
accepted position for `redirect`'s far hotter path, and bounded here by page
request rate rather than click rate.

**8. Adding an `ev=exc` catch-all to `handle_request` in the same change —
deferred, not rejected on the merits.** `gui-pages/app.py` has no `try/except`
at all, so an unexpected exception propagates to the host and produces a bare
500 **with none of this component's security headers** — from the one component
whose entire job is attaching them. That is a real hole and the fix would reuse
everything this plan builds. It is deferred because it changes response
behaviour rather than only observability, it needs its own decision about
`at=<file>:<line>` (`api/obs.exc_location`), and the user scoped this work to
the page-read line. Filed under Future work with the header-loss argument
recorded so the next planner does not have to rediscover it.

**9. Doing nothing (honouring the unfired trigger) — overridden by the user,
recorded for completeness.** The trigger ("a `500` actually observed from the
catch-all") is a reasonable gate for a change that costs a whole variable-backed
seam. Corrections (1) and (2) above are what make it cheap enough that waiting
buys little: the failing state is *permanent and silent*, so the trigger can
only fire via a user reporting a blank page, which is the failure mode the
change exists to remove.

## Tasks

The exact lines appended to `TASKS.md` under `## gui-pages page-read failure
logging`. `TASKS.md` is authoritative; checkboxes are ticked only there.

```
- [ ] Add gui-pages/obs.py — the two-rule sanitizer, path sanitizer, failure-line renderer and per-instance dedup factory — file(s): gui-pages/obs.py — done when: `sanitize_error_message`, `sanitize_path_for_log`, `render_failure_line`, `page_read_failed_line` and `make_dedup` all exist with ZERO `spin_sdk` imports, `MAX_ERROR_MESSAGE_CHARS = 200` and `MAX_FAILURE_DEDUP_KEYS = 32` are plain module constants, `sanitize_error_message` deliberately does NOT carry api/obs.py's key-shaped or pbkdf2 rules (with the measured `[key:wasi]` mangling recorded in its docstring), and `cd gui-pages && uv run python -c "import obs"` succeeds from the host venv.
- [ ] Cover gui-pages/obs.py under pytest, including the ROUTES log-safety guard — file(s): gui-pages/tests/test_obs.py — done when: `cd gui-pages && uv run pytest` passes with new tests pinning all ten items in the plan's Tests section 1-10, including `test_every_routes_value_is_log_safe` (every `routing.ROUTES` key AND value survives `sanitize_path_for_log` unchanged), that a message containing a newline yields a rendered line with no `\n` in it, that `msg` is the final field, that `errno` is omitted when `exc.errno` is None, and that two `make_dedup()` instances share no state.
- [ ] Thread an optional `on_read_error` callback through routing.build_response — file(s): gui-pages/routing.py, gui-pages/tests/test_routing.py — done when: `build_response(uri, read_file, on_read_error=None)` calls the callback with `(matched_path, filename, exc)` inside the `except OSError` branch, guarded by its own `try/except Exception: pass`; `routing.py` still imports nothing from `spin_sdk` and nothing from `obs`; and `uv run pytest` passes with the plan's tests 11-14 — notably that a callback which RAISES still yields the 500 with `ERROR_PAGES[500]` and every `SECURITY_HEADERS` entry, and that the 404 and nonpages paths never call it.
- [ ] Wire the failure reporter into the gui-pages WASI entrypoint (depends on the two tasks above) — file(s): gui-pages/app.py — done when: `app.py` imports `sys` and `obs` at module scope, holds exactly one module-scope `_should_emit_failure = obs.make_dedup()` with the comment explaining why instance-lifetime (not per-request) and why it is not the forbidden shared-collector pattern, defines `_report_read_error(path, filename, exc)` that prints only when the dedup admits the key, and passes it as `build_response`'s third argument; `spin up --build` completes with no new component in `spin.toml` and no new entry under `[component.gui-pages.variables]`.
- [ ] Correct errorpages.py's now-false "gui-pages has no logging at all" comment without changing a served byte — file(s): gui-pages/errorpages.py — done when: the comment above `INTERNAL_ERROR_HTML` names the new `ev=page_read_failed` line and states why the copy still does NOT say "we've been notified" (nothing monitors the stream), `git diff` shows changes to comment lines only, and `uv run pytest` still passes unchanged (test_no_inline_code.py covers `ERROR_PAGES`' bytes).
- [ ] Document the new line kind in CLAUDE.md — file(s): CLAUDE.md — done when: the "Observable KV failures" subsection lists `ev=page_read_failed` as a fourth line kind alongside `ev=kv_fail`/`ev=exc`/`ev=record_unreadable`, states that it comes from `gui-pages` and carries no `op`/`ns` (no KV operation failed — this component makes none) and no `msg_redacted` (its sanitizer has no redaction rule that can fire, deliberately), records that `gui-pages` reads NO `log_level`/`log_debug_token` and gets no summary line or `Server-Timing`, and the "Toggleable structured logging" opening sentence ("gui-pages and gui are untouched") is amended to say the KV-timing half still does not cover gui-pages while the failure-line half now does.
- [ ] End-to-end manual verification of ev=page_read_failed — file(s): (none — verification step) — done when: with a TEMPORARY extra `ROUTES` entry pointing at a nonexistent file, `spin up --build` serves that path as a 500 carrying the styled error page and every security header, exactly ONE `ss comp=gui-pages ev=page_read_failed ...` line appears on stderr across THREE identical requests (dedup proven, not assumed), the line's `route`/`file`/`etype`/`errno`/`msg` fields are recorded verbatim in TASKS.md (settling the UNCONFIRMED errno-to-subclass mapping), every real page still loads with no new stderr line, and the temporary `ROUTES` entry is reverted with `git diff gui-pages/routing.py` showing only the intended `on_read_error` change.
```

## Critical files

- `docs/plans/gui-pages-failure-logging.md` (new)
- `gui-pages/obs.py` (new)
- `gui-pages/tests/test_obs.py` (new)
- `gui-pages/routing.py`
- `gui-pages/app.py`
- `gui-pages/errorpages.py` (comment only — zero served bytes change)
- `gui-pages/tests/test_routing.py`
- `CLAUDE.md`
- `TASKS.md`

Explicitly **not** touched: `spin.toml`, `Jenkinsfile`, `redirect/`, `api/`,
`gui/`, `gui-pages/tests/test_no_inline_code.py`,
`gui-pages/tests/test_manifest_components.py`.

## Verification

1. Baseline, before any change (it was `135 passed` at planning time):

   ```bash
   cd gui-pages && uv run pytest
   ```

2. After every code task, the component suite — the only suite this change can
   affect:

   ```bash
   cd gui-pages && uv run pytest
   ```

3. Confirm the new module is genuinely host-importable and `spin_sdk`-free:

   ```bash
   cd gui-pages && uv run python -c "import obs; print(obs.MAX_FAILURE_DEDUP_KEYS)"
   grep -n "spin_sdk" gui-pages/obs.py gui-pages/routing.py   # expect no matches
   ```

4. Confirm the other two suites are untouched (they should be — no file in
   either component changes — but CI runs all three, so prove it):

   ```bash
   cd redirect && go test ./linkgate/...     # NEVER go test ./... — fails by design
   cd api && uv run pytest
   ```

5. **Live, and the only way to settle the `errno` mapping.** Temporarily add one
   entry to `routing.ROUTES` pointing at a file that does not exist —
   `"/logtest.html": "no-such-page.html"` — then, from the repo root:

   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```

   Then, watching the `spin up` terminal for stderr:

   ```bash
   curl -i http://localhost:3000/logtest.html
   curl -s -o /dev/null http://localhost:3000/logtest.html
   curl -s -o /dev/null http://localhost:3000/logtest.html
   ```

   A pass is **all** of:
   - the first response is `500` with `content-type: text/html; charset=utf-8`,
     the styled "Something went wrong" body, and all of
     `x-content-type-options`, `referrer-policy`, `x-frame-options`,
     `strict-transport-security`, `content-security-policy`, `x-ss-version`;
   - **exactly one** `ss comp=gui-pages ev=page_read_failed ...` line on stderr
     across all three requests — this is the dedup proof, and three requests
     rather than one is the point;
   - the line's field order is `comp ev route file etype [errno] msg`, `msg` is
     last, and nothing follows it;
   - `route=/logtest.html`, `file=no-such-page.html`;
   - the observed `etype` and `errno` are recorded verbatim in `TASKS.md`
     (this is what confirms or refutes the errno-to-subclass mapping under
     componentize-py).

6. Still live, confirm the success path is silent and unchanged — load
   `http://localhost:3000/login.html`, `/dashboard.html` (after signing in) and
   `/admin/users.html` in a browser and confirm each renders normally with **no**
   new stderr line, and that `curl -i http://localhost:3000/nope` still returns
   the styled 404 with no line.

7. Revert the temporary `ROUTES` entry and confirm the diff:

   ```bash
   git diff gui-pages/routing.py    # only the on_read_error parameter and branch
   git diff --numstat TASKS.md      # only checkbox lines + the recorded verbatim line
   ```

## Out of scope / follow-ups

- **An `ev=exc` catch-all in `gui-pages`' `handle_request`.** Today an
  unexpected exception anywhere in `app.py` (outside `build_response`'s own
  guard) propagates to the host, producing a bare 500 with **none** of this
  component's security headers. Deferred per Trade-offs #8; added to `TASKS.md`
  "Future work (not scheduled)" with the header-loss argument recorded. Trigger:
  any observed bare/unstyled 500 from the catch-all, or the next change that
  adds real logic to `handle_request`.
- **A `log_level=summary` line for `gui-pages`.** Deferred per Trade-offs #1;
  added to "Future work (not scheduled)". Trigger: `gui-pages` gaining per-request
  work worth timing (a KV read, an outbound call, a template render), or the
  catch-all's 404 rate becoming a question that Akamai's own request logs cannot
  answer. Note the prerequisite it inherits: a summary line covers 404s, whose
  `route` is the raw requested path, so it cannot ship without deciding how that
  field is collapsed.
- **Cross-language pinning of the three `sanitize_error_message`
  implementations.** Deliberately not done, on the rule CLAUDE.md already states
  for the existing pair. Not filed — this is a settled position, not a deferral.
- **Alerting on `ev=page_read_failed`.** Out of scope and probably always will
  be: this app has no alerting anywhere, and CLAUDE.md's `ev=kv_fail` note is
  explicit that naively paging on a failure line is a mistake. The line's value
  is that the answer exists in `spin aka logs` when someone goes looking.
- **`gui`, the prebuilt `spin_static_fs.wasm` component.** Still has no
  instrumentation and cannot get any — it is a third-party binary. Unchanged by
  this plan and by anything foreseeable.
