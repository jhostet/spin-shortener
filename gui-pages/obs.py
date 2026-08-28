"""Unconditional page-read failure logging for gui-pages.

docs/plans/gui-pages-failure-logging.md. `routing.build_response`'s
`except OSError` branch (a ROUTES-vs-filesystem drift — a page named in
`ROUTES` whose file cannot actually be read) used to serve the styled 500
page and record nothing anywhere. This module renders one `ev=page_read_failed`
failure line for that condition, emitted UNCONDITIONALLY — independent of
`log_level`/`X-SS-Debug`, the same doctrine `docs/plans/observable-kv-failures.md`
already settled for `ev=kv_fail`/`ev=exc`/`ev=record_unreadable`: gating a
diagnostic behind the very toggle it exists to catch failures without would
defeat the point. `gui-pages` has no `log_level`/`log_debug_token` variable
at all and gets no summary line or `Server-Timing` — see CLAUDE.md,
"Toggleable structured logging" and "Observable KV failures".

Named `obs.py`, never `logging.py` (or `time.py`/`json.py`) — componentize-py
compiles `app.py` alongside its siblings with this component's own directory
on the import path, so a module shadowing a stdlib name would break every
stdlib module that imports the real one, for the whole component.

Zero `spin_sdk` imports — pure logic, host-testable exactly like
`routing.py`, `errorpages.py` and `nonpages.py`.
"""

import re

MAX_ERROR_MESSAGE_CHARS = 200  # mirrors api/obs.py and linkgate.MaxErrorMessageChars
MAX_FAILURE_DEDUP_KEYS = 32  # mirrors redirect/main.go's maxFailureDedupPairs

# Control characters and newlines only — see sanitize_error_message's
# docstring for why this is deliberately narrower than api/obs.py's
# three-rule sanitizer.
_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x1f\x7f]")

# Mirrors routing.ROUTES' own character class (^[A-Za-z0-9._/-]+$), bounded
# to a generous length so an unexpectedly long value can't blow up the log
# line; every real ROUTES key/value is well under this.
_PATH_LOG_SAFE_PATTERN = re.compile(r"^[A-Za-z0-9._/-]{1,128}$")

# NUL, mirroring redirect/linkgate/obs.go's dedupKeySep — safe because
# sanitize_error_message replaces every control character (NUL included)
# with "_", so no part of the key can contain one, and the parts can never
# be ambiguously re-split or collide by concatenation.
_SEP = "\x00"


def sanitize_error_message(text: str) -> tuple[str, bool]:
    """Sanitizes a raw OSError message for safe inclusion in a log line.

    Two rules, in order:
    (a) replace control characters/newlines with "_", so one failure is
        always one line and no message can forge a second "ss "-prefixed
        line;
    (b) truncate to MAX_ERROR_MESSAGE_CHARS, setting the returned flag.

    Returns (sanitized, truncated).

    Deliberately NARROWER than api/obs.py's three-rule sanitizer — it omits
    the key-shaped (`[key:<word>]`) and pbkdf2-hash-token rules entirely.
    Both are provably dead code in this component: `gui-pages` sends no KV
    keys and holds no PBKDF2 hash, so redacting either shape protects
    nothing. Worse than dead — measured directly against the api key-shaped
    pattern, `wasi:filesystem/types@0.2.0#read: access denied` (an
    UNCONFIRMED but plausible componentize-py filesystem error shape) becomes
    `[key:wasi] access denied`, destroying the operative half of the one
    message this component's only diagnostic has. See the plan's
    Trade-offs #3.
    """
    sanitized = _CONTROL_CHAR_PATTERN.sub("_", text)

    truncated = False
    if len(sanitized) > MAX_ERROR_MESSAGE_CHARS:
        sanitized = sanitized[:MAX_ERROR_MESSAGE_CHARS]
        truncated = True

    return sanitized, truncated


def sanitize_path_for_log(value: str) -> str:
    """Returns value unchanged if it matches ^[A-Za-z0-9._/-]{1,128}$,
    otherwise the fixed placeholder "[invalid_path]", carrying none of the
    original bytes.

    Every `routing.ROUTES` key and value already matches this pattern today
    (pinned by test_obs.py's test_every_routes_value_is_log_safe), so this
    function is a runtime fallback, not a load-bearing control — but the
    field's safety should not rest on that invariant continuing to hold any
    more than redirect/linkgate.SanitizeSlugForLog's does.
    """
    if _PATH_LOG_SAFE_PATTERN.match(value):
        return value
    return "[invalid_path]"


def render_failure_line(fields: list[tuple[str, str]]) -> str:
    """Renders one "ss "-prefixed logfmt line from fields, in order, with
    NOTHING appended after them — a separate function from any future
    summary-line renderer, exactly as api/obs.render_failure_line and
    linkgate.RenderFailureLine are, so nothing can ever land after `msg`,
    which every caller places last."""
    parts = ["ss"]
    for key, value in fields:
        parts.append(f"{key}={value}")
    return " ".join(parts)


def page_read_failed_line(path: str, filename: str, exc: BaseException) -> tuple[str, str]:
    """Returns (line, dedup_key) for one ev=page_read_failed failure line.

    Every decision lives here, where it is host-testable; the caller
    (app.py's _report_read_error) does nothing but consult a dedup set and
    write the string — modelled on linkgate.RecordUnreadableLine, which
    returns the same pair for the same reason (package main / app.py are not
    host-testable).

    Field order, all load-bearing: comp, ev, route, file, etype,
    [errno], [msg_truncated], msg (always last, nothing after it).

    No op/ns field, deliberately — no KV operation failed, and this
    component performs none. No msg_redacted field, ever, by construction —
    sanitize_error_message has no redaction rule that can fire.
    """
    route = sanitize_path_for_log(path)
    file_field = sanitize_path_for_log(filename)
    etype = type(exc).__name__

    raw_msg = str(exc)
    sanitized, truncated = sanitize_error_message(raw_msg)
    msg = sanitized if sanitized else "-"

    errno = getattr(exc, "errno", None)
    errno_str = str(errno) if isinstance(errno, int) else None

    fields: list[tuple[str, str]] = [
        ("comp", "gui-pages"),
        ("ev", "page_read_failed"),
        ("route", route),
        ("file", file_field),
        ("etype", etype),
    ]
    if errno_str is not None:
        fields.append(("errno", errno_str))
    if truncated:
        fields.append(("msg_truncated", "1"))
    fields.append(("msg", msg))

    line = render_failure_line(fields)

    dedup_key = _SEP.join(
        ["page_read_failed", route, file_field, etype, errno_str or "-", msg]
    )
    return line, dedup_key


def make_dedup(max_keys: int = MAX_FAILURE_DEDUP_KEYS):
    """Returns should_emit(key) -> bool, closing over ONE set.

    First sighting of a key returns True and records it; a repeat returns
    False; once max_keys distinct keys are held, every further key —
    including a genuinely novel one — returns False.

    A factory rather than a module-level set so tests can build independent
    instances with no cross-test contamination. The real caller
    (gui-pages/app.py) builds exactly one, at module scope, so the dedup
    budget spans this Wasm instance's whole life — see app.py's comment for
    why that is the correct lifetime here (a ROUTES-vs-filesystem drift is
    permanent until a redeploy) and why it is not the forbidden
    shared-collector pattern.
    """
    seen: set[str] = set()

    def should_emit(key: str) -> bool:
        if key in seen:
            return False
        if len(seen) >= max_keys:
            return False
        seen.add(key)
        return True

    return should_emit
