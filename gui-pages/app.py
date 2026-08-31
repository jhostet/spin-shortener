import sys

from spin_sdk import variables
from spin_sdk.http import Handler, Request, Response

import obs
from routing import build_response, internal_error_response

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
#
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


def _report_read_error(path: str, filename: str, exc: BaseException) -> None:
    """Injected into build_response as on_read_error. Unconditional — emitted
    regardless of any log toggle, because this fault has never been observed and
    a diagnostic gated behind a toggle nobody has turned on records nothing."""
    line, dedup_key = obs.page_read_failed_line(path, filename, exc)
    if _should_emit_page_read_failure(dedup_key):
        print(line, file=sys.stderr)


def _report_unhandled_exception(exc: BaseException) -> None:
    """The ev=exc twin of _report_read_error. Unconditional — emitted
    regardless of any log toggle (this component has none), because this is
    now the ONLY evidence of what a 500 from the catch-all actually was: the
    SDK's own bare `except:` and its traceback.print_exc() no longer run once
    handle_request catches."""
    line, dedup_key = obs.unhandled_exception_line(exc)
    if _should_emit_exc(dedup_key):
        print(line, file=sys.stderr)


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
