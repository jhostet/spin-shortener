import sys

from spin_sdk import variables
from spin_sdk.http import Handler, Request, Response

import obs
from routing import build_response

# Matches spin.toml's [component.gui-pages] `files` mapping
# (`{ source = "gui", destination = "/gui" }`).
GUI_DIR = "/gui"

# Cached for the instance's lifetime — a Spin variable cannot change without a
# restart locally or a redeploy on Akamai, both of which produce a fresh
# instance. None means "not yet read"; "unknown" is the legitimate value when
# no operator supplied one.
_app_version: str | None = None


async def _app_version_value() -> str:
    global _app_version
    if _app_version is None:
        _app_version = await variables.get("app_version") or "unknown"
    return _app_version


def _read_file(relative_path: str) -> bytes:
    with open(f"{GUI_DIR}/{relative_path}", "rb") as f:
        return f.read()


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


class HttpHandler(Handler):
    async def handle_request(self, request: Request) -> Response:
        result = build_response(request.uri, _read_file, _report_read_error)
        # X-SS-Version is attached here rather than in routing.SECURITY_HEADERS
        # because it comes from a Spin variable, and routing.py deliberately
        # imports nothing from spin_sdk so it stays host-testable under pytest.
        headers = {**result.headers, "x-ss-version": await _app_version_value()}
        return Response(result.status, headers, result.body)
