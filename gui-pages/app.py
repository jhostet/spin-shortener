from spin_sdk import variables
from spin_sdk.http import Handler, Request, Response

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


class HttpHandler(Handler):
    async def handle_request(self, request: Request) -> Response:
        result = build_response(request.uri, _read_file)
        # X-SS-Version is attached here rather than in routing.SECURITY_HEADERS
        # because it comes from a Spin variable, and routing.py deliberately
        # imports nothing from spin_sdk so it stays host-testable under pytest.
        headers = {**result.headers, "x-ss-version": await _app_version_value()}
        return Response(result.status, headers, result.body)
