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
_KV_OP_ORDER = ("open", "exists", "get", "get_many", "get_many_error", "set", "delete", "list_keys")


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
