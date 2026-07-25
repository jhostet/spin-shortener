from spin_sdk.http import Handler, Request, Response

from routing import build_response

# Matches spin.toml's [component.gui-pages] `files` mapping
# (`{ source = "gui", destination = "/gui" }`).
GUI_DIR = "/gui"


def _read_file(relative_path: str) -> bytes:
    with open(f"{GUI_DIR}/{relative_path}", "rb") as f:
        return f.read()


class HttpHandler(Handler):
    async def handle_request(self, request: Request) -> Response:
        result = build_response(request.uri, _read_file)
        return Response(result.status, result.headers, result.body)
