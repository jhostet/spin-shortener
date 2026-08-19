"""Toggleable structured logging: a per-request KV-timing collector, the
logfmt line renderer, the Server-Timing renderer and the debug-token check.
Mirrors redirect/linkgate/obs.go's shape so both components emit the same
line format.

Named `obs.py`, deliberately never `logging.py` (or `time.py`/`json.py`) —
componentize-py compiles app.py alongside its siblings with this component's
own directory on the import path, so a module shadowing a stdlib name would
break every stdlib module that imports the real one.

Zero `spin_sdk` imports — pure logic, host-testable exactly like
api/urlpolicy.py and every other pure module in this component.
"""

import hmac
import re
from typing import Optional

# Fixed emission order for per-op-type fields in the logfmt line. Matches
# redirect/linkgate/obs.go's kvOpOrder and the plan's sample output — not
# alphabetical, mirrors the order operations actually happen in.
#
# "get_many"/"get_many_error" are deliberately NOT mirrored into
# redirect/linkgate/obs.go (docs/plans/batch-kv-reads.md) — redirect never
# batches (it reads exactly one key per click), so a Go-side field would be
# dead code. This is the one place api/obs.py's vocabulary diverges from its
# Go counterpart; nothing pins the two vocabularies against each other the
# way api/tests/test_kvprefix.py pins keys.go's prefixes and CountShards.
#
# "write_retry"/"write_failed" (docs/plans/write-throttle-resilience.md) are
# the SECOND such divergence, for the same reason: `redirect` is explicitly
# out of scope for the write-retry seam (its analytics writes stay
# best-effort lossy, mechanism M2), so a Go-side field would also be dead
# code. Nothing pins these two against redirect/linkgate/obs.go either.
_KV_OP_ORDER = (
    "open", "exists", "get", "get_many", "get_many_error", "set", "delete",
    "write_retry", "write_failed", "list_keys",
)


class Collector:
    """Accumulates one request's KV operation timing: per operation type, a
    count, total microseconds, total bytes moved and total KEYS covered, plus
    the single slowest operation seen.

    record()'s signature is (op_type, namespace, duration_ns, num_bytes,
    num_keys) — it has NO parameter that could accept a key. users:session:
    <token> is a live session credential and `spin aka logs` retains 7 days
    by default, so a key-logging design would put working session tokens in
    a week-long retention window. This is the same structural move
    PrefixedStore makes by having no get_keys method.

    `num_keys` (keyword-defaulted to 1, so every pre-existing call site is
    unaffected) exists because a single `get_many` operation covers K keys —
    `kv_ops` has always counted HOST OPERATIONS and must keep doing so, but K
    must not vanish: once handle_click_totals batches, its `get` count reads
    1 forever, so the monitoring number CLAUDE.md names for the
    cached-totals-blob trigger ("a traced get count above ~500") moves to
    this field, `kv_keys`, at the same threshold. See
    docs/plans/batch-kv-reads.md's Instrumentation section.

    Every caller constructs its own Collector for its own request — there is
    no module-level instance and none should ever be added. A shared
    collector would silently interleave concurrent requests' operations into
    one another's line, which `handle()` dispatching each request through
    `componentize_py_async_support.spawn` makes a real, not theoretical, risk.
    """

    def __init__(self) -> None:
        # op_type -> [count, total_us, total_bytes, total_keys]
        self._stats: dict[str, list[int]] = {}
        self._slow: Optional[tuple[str, str, int]] = None  # (op_type, namespace, us)

    def record(
        self, op_type: str, namespace: str, duration_ns: int, num_bytes: int = 0, num_keys: int = 1
    ) -> None:
        us = duration_ns // 1000  # truncating, matching Go's time.Duration.Microseconds()
        count, total_us, total_bytes, total_keys = self._stats.get(op_type, [0, 0, 0, 0])
        self._stats[op_type] = [count + 1, total_us + us, total_bytes + num_bytes, total_keys + num_keys]
        if self._slow is None or us > self._slow[2]:
            self._slow = (op_type, namespace, us)

    def totals(self) -> tuple[int, int, int, int]:
        """Returns (ops, total_us, total_bytes, total_keys) summed across
        every operation type."""
        ops = sum(s[0] for s in self._stats.values())
        us = sum(s[1] for s in self._stats.values())
        num_bytes = sum(s[2] for s in self._stats.values())
        keys = sum(s[3] for s in self._stats.values())
        return ops, us, num_bytes, keys


def route_template(path: str) -> str:
    """Collapses a raw /api/... path into its route template, so a slug or
    username embedded in the URL never reaches a log line — only the shape
    of the route does. Every other path (including /api/links itself,
    /api/links/bulk and /api/links/bulk-action, which have no dynamic
    segment) is returned unchanged.
    """
    segments = path.split("/")

    if len(segments) >= 4 and segments[1] == "api" and segments[2] == "links":
        identifier = segments[3]
        if identifier in ("", "bulk", "bulk-action"):
            return path
        suffix = "/".join(segments[4:])
        base = "/api/links/{slug}"
        return f"{base}/{suffix}" if suffix else base

    if len(segments) >= 4 and segments[1] == "api" and segments[2] == "users":
        identifier = segments[3]
        if identifier == "":
            return path
        return "/api/users/{username}"

    return path


def render_log_line(fields: list[tuple[str, str]], duration_ns: int, collector: Optional[Collector]) -> str:
    """Renders one "ss "-prefixed logfmt line: the caller-supplied fields in
    order, then dur_us, then (if collector is not None) the KV summary —
    kv_ops/kv_us/kv_bytes[/kv_keys], one field per non-zero-count operation
    type in _KV_OP_ORDER ("count/total_µs"), and the single slowest
    operation as "type:namespace:µs". Zero-count operation-type fields are
    omitted entirely, never emitted as "=0/0".

    kv_keys is emitted immediately after kv_bytes, but ONLY when the key
    total differs from the op total — so every request that never batches
    (i.e. every request before docs/plans/batch-kv-reads.md, and every
    non-batching handler after it) renders a byte-identical line to before
    this field existed. Its presence at all means a get_many happened.
    """
    parts = ["ss"]
    for key, value in fields:
        parts.append(f"{key}={value}")
    parts.append(f"dur_us={duration_ns // 1000}")

    if collector is None:
        return " ".join(parts)

    ops, us, num_bytes, num_keys = collector.totals()
    parts.append(f"kv_ops={ops}")
    parts.append(f"kv_us={us}")
    parts.append(f"kv_bytes={num_bytes}")
    if num_keys != ops:
        parts.append(f"kv_keys={num_keys}")

    for op_type in _KV_OP_ORDER:
        stat = collector._stats.get(op_type)
        if not stat or stat[0] == 0:
            continue
        parts.append(f"{op_type}={stat[0]}/{stat[1]}")

    if collector._slow is not None:
        slow_type, slow_namespace, slow_us = collector._slow
        parts.append(f"slow={slow_type}:{slow_namespace}:{slow_us}")

    return " ".join(parts)


def render_server_timing(handler_duration_ns: int, collector: Optional[Collector]) -> str:
    """Renders the Server-Timing header value for a token-bearing request:
    `kv;dur=<ms>;desc="N ops", handler;dur=<ms>`. Durations are milliseconds
    as floats — 80 µs must render as 0.080, never 80 — because
    Server-Timing's dur parameter is defined in milliseconds.
    """
    ops, us, _, _ = collector.totals() if collector is not None else (0, 0, 0, 0)
    kv_ms = us / 1000.0
    handler_ms = (handler_duration_ns // 1000) / 1000.0
    return f'kv;dur={kv_ms:.3f};desc="{ops} ops", handler;dur={handler_ms:.3f}'


def parse_log_level(raw: str) -> str:
    """Maps a raw log_level variable value to a known level. Any
    unrecognised value (including empty/unset) is treated as "off" —
    fail-closed, never raise. Only "off" and "summary" exist today; a future
    "verbose" needs no rename here.
    """
    if raw == "summary":
        return "summary"
    return "off"


def token_matches(configured: str, provided: str) -> bool:
    """Reports whether provided matches configured using a constant-time
    comparison. An empty configured token NEVER matches anything, including
    an empty or absent provided value — checked explicitly before any
    comparison, not as an incidental property of hmac.compare_digest (which
    would happily report two empty strings as equal). Getting this backwards
    makes the default configuration "anyone can enable tracing", exactly
    what the token exists to prevent.
    """
    if configured == "":
        return False
    return hmac.compare_digest(configured, provided)


# --- Observable KV failures (docs/plans/observable-kv-failures.md) ---
#
# A SEPARATE line kind from render_log_line's summary line above, emitted to
# stderr UNCONDITIONALLY on failure only — independent of log_level and of
# X-SS-Debug, because the whole point is to catch a fault that has never
# been reproduced on demand. render_log_line, Collector, _KV_OP_ORDER,
# route_template, render_server_timing, parse_log_level and token_matches
# above are all untouched by this section; nothing here changes what they
# emit or when.
#
# The sanitizer (sanitize_error_message) is NOT pinned against
# redirect/linkgate/obs.go's SanitizeErrorMessage in a cross-language test,
# unlike keys.go's prefixes/CountShards — a divergence between the two
# produces two slightly-differently-shaped log lines and nothing else,
# where the keys.go pin exists because THAT divergence fails silently at
# runtime (the API writes links the redirect path can't find). See the
# plan's Trade-offs #8.

MAX_ERROR_MESSAGE_CHARS = 200
MAX_FAILURE_LINES_PER_REQUEST = 3

# (a) A key-shaped substring: a leading word, a colon, then one or more
# non-whitespace/quote/paren/bracket characters. The trailing character
# class is what keeps "key-value error: internal server error" intact — a
# colon followed by a space does not match, because the class requires at
# least one non-whitespace character immediately after the colon. This is
# complete for keys because every physical key this app sends is prefixed
# (kvprefix.STORE_PREFIXES; redirect/linkgate/keys.go's LinkKey/
# CountShardKey), so a host that echoes a key echoes a prefixed one. The
# pattern is written generically rather than against the three known
# prefixes so a future namespace is covered for free.
_KEY_SHAPED_PATTERN = re.compile(r"([A-Za-z][A-Za-z0-9_-]*):[^\s'\")\]]+")

# (b) A pbkdf2 hash token — a link password hash is the one place a *value*
# (rather than a key) could plausibly be echoed by a set failure.
_HASH_TOKEN_PATTERN = re.compile(r"\S*pbkdf2_sha256\S*")

# (c) Control characters and newlines, so one failure is always one line.
_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_error_message(text: str) -> tuple[str, bool, bool]:
    """Sanitizes a raw exception message for safe inclusion in a log line,
    applying these rules IN ORDER:

    (a) redact key-shaped substrings ("links:slug:promo" -> "[key:links]");
    (b) redact any whitespace-delimited token containing "pbkdf2_sha256"
        ("[hash]");
    (c) replace control characters/newlines with "_";
    (d) truncate to MAX_ERROR_MESSAGE_CHARS (200).

    Returns (sanitized, redacted, truncated). `redacted` is True if (a) or
    (b) fired at all — this is the one-command answer to "does an Akamai KV
    error string ever embed a key": `grep 'msg_redacted=1'` on a captured
    line, obtainable with no key ever having reached the log. An empty
    `text` (or a text that redacts to nothing) returns an empty string; the
    caller renders that as `msg=-`, exactly as `sanitize_error_message`
    itself has no opinion on log-line formatting.
    """
    redacted = False

    def _redact_key(match: re.Match) -> str:
        nonlocal redacted
        redacted = True
        return f"[key:{match.group(1)}]"

    sanitized = _KEY_SHAPED_PATTERN.sub(_redact_key, text)

    def _redact_hash(_match: re.Match) -> str:
        nonlocal redacted
        redacted = True
        return "[hash]"

    sanitized = _HASH_TOKEN_PATTERN.sub(_redact_hash, sanitized)
    sanitized = _CONTROL_CHAR_PATTERN.sub("_", sanitized)

    truncated = False
    if len(sanitized) > MAX_ERROR_MESSAGE_CHARS:
        sanitized = sanitized[:MAX_ERROR_MESSAGE_CHARS]
        truncated = True

    return sanitized, redacted, truncated


def error_type_name(exc: BaseException) -> str:
    """Returns a wording-independent signal of the WIT error variant.

    `componentize_py_types.Err` is a frozen dataclass subclassing Exception
    whose `.value` holds the inner `Error_*` dataclass instance — so
    `type(exc.value).__name__` identifies the variant with NO import at
    all (duck-typed via getattr, never an isinstance check against a type
    this module cannot import). When that shape is present, returns
    "<OuterClassName>/<InnerClassName>" (e.g. "Err/Error_Other"); otherwise
    falls back to the bare exception class name, so every other exception
    in the app (a plain ValueError, a KeyError, ...) still gets a usable
    etype.
    """
    inner = getattr(exc, "value", None)
    inner_name = type(inner).__name__ if inner is not None else None
    if inner_name is not None and inner_name.startswith("Error_"):
        return f"{type(exc).__name__}/{inner_name}"
    return type(exc).__name__


def exc_location(exc: BaseException) -> str:
    """Returns "<basename>:<lineno>" of the INNERMOST traceback frame — never
    source text, never a value, so a 500 is diagnosable from one field that
    provably contains no data. Returns "-" if the exception carries no
    traceback at all (e.g. one constructed but never raised, as in a test).
    """
    tb = exc.__traceback__
    if tb is None:
        return "-"
    while tb.tb_next is not None:
        tb = tb.tb_next
    filename = tb.tb_frame.f_code.co_filename
    basename = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return f"{basename}:{tb.tb_lineno}"


def render_failure_line(fields: list[tuple[str, str]]) -> str:
    """Renders one "ss "-prefixed logfmt line from fields, in order, with
    NOTHING appended after them — deliberately separate from render_log_line
    so nothing (a dur_us, a kv_ops summary, anything) can ever land after
    `msg`, which every caller places last."""
    parts = ["ss"]
    for key, value in fields:
        parts.append(f"{key}={value}")
    return " ".join(parts)


def make_failure_reporter(emit, *, comp: str, route: str, method: Optional[str] = None,
                           max_distinct: int = MAX_FAILURE_LINES_PER_REQUEST):
    """Returns `report(ev, op, namespace, duration_ns, exc, extra=None)`,
    closing over ONE dedup set for the lifetime of THIS reporter instance.

    That lifetime must be exactly one request — never module-level, for the
    identical reason obs.Collector never is: a shared reporter would
    interleave concurrent requests' dedup state (and their distinct-tuple
    budgets) into one another.

    Dedup key is (op, namespace, etype, msg) — the exact tuple named in the
    plan — so a throttle storm producing the same failure 150 times in one
    request still emits it once, and a request is capped at `max_distinct`
    DISTINCT tuples total (further distinct tuples beyond the cap are
    silently dropped, not queued).

    Field order in the rendered line, all load-bearing: comp, ev, route,
    [method], [op, ns, op_us] (only when op is not None — an `ev="exc"`
    call has no KV operation to report), etype, [extra fields, e.g. "at"],
    [msg_redacted=1], [msg_truncated=1], msg (always last).
    """
    seen: set[tuple[str, str, str, str]] = set()

    def report(ev: str, op: Optional[str], namespace: Optional[str], duration_ns: Optional[int],
                exc: BaseException, extra: Optional[list[tuple[str, str]]] = None) -> None:
        etype = error_type_name(exc)
        sanitized, redacted, truncated = sanitize_error_message(str(exc))
        msg = sanitized if sanitized else "-"

        key = (op or "-", namespace or "-", etype, msg)
        if key in seen:
            return
        if len(seen) >= max_distinct:
            return
        seen.add(key)

        fields: list[tuple[str, str]] = [("comp", comp), ("ev", ev), ("route", route)]
        if method is not None:
            fields.append(("method", method))
        if op is not None:
            fields.append(("op", op))
            fields.append(("ns", namespace if namespace is not None else "-"))
            fields.append(("op_us", str((duration_ns or 0) // 1000)))
        fields.append(("etype", etype))
        if extra:
            fields.extend(extra)
        if redacted:
            fields.append(("msg_redacted", "1"))
        if truncated:
            fields.append(("msg_truncated", "1"))
        fields.append(("msg", msg))

        emit(render_failure_line(fields))

    return report
