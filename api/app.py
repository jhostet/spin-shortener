import json
from urllib.parse import parse_qs, urlparse

from spin_sdk import key_value, variables
from spin_sdk.http import Handler

import auth
import links
import qr
from responses import Request, Response, json_response


async def _cookie_secure() -> bool:
    return (await variables.get("cookie_secure")).strip().lower() == "true"


async def _require_session(store, request: Request):
    """Returns a Principal, or an error Response to short-circuit with."""
    principal = await auth.resolve_session(store, request)
    if principal is None:
        return json_response(401, {"error": "unauthenticated"})
    if not auth.check_csrf(request, principal):
        return json_response(403, {"error": "csrf_mismatch"})
    return principal


class HttpHandler(Handler):
    async def handle_request(self, request: Request) -> Response:
        parsed_uri = urlparse(request.uri)
        path = parsed_uri.path
        query = parse_qs(parsed_uri.query)
        method = request.method

        users_store = await key_value.open("users")
        admin_username = await variables.get("admin_bootstrap_username")
        admin_password = await variables.get("admin_bootstrap_password")
        await auth.ensure_bootstrap_admin(users_store, admin_username, admin_password)
        cookie_secure = await _cookie_secure()

        if path == "/api/auth/login" and method == "POST":
            return await self._login(users_store, request, cookie_secure)

        if path == "/api/auth/logout" and method == "POST":
            result = await _require_session(users_store, request)
            if isinstance(result, Response):
                return result
            await auth.delete_session(users_store, request)
            return json_response(200, {"ok": True}, headers={"set-cookie": auth.build_logout_cookie(cookie_secure)})

        if path == "/api/auth/me" and method == "GET":
            result = await auth.resolve_session(users_store, request)
            if result is None:
                return json_response(401, {"error": "unauthenticated"})
            return json_response(200, {
                "username": result.username,
                "role": result.role,
                "permissions": result.permissions,
            })

        if path == "/api/links" and method in ("GET", "POST"):
            result = await _require_session(users_store, request)
            if isinstance(result, Response):
                return result
            links_store = await key_value.open("links")
            if method == "GET":
                return await links.handle_list(links_store, result)
            return await links.handle_create(links_store, result, request)

        if path.startswith("/api/links/") and path.endswith("/password") and method == "POST":
            slug = path.removeprefix("/api/links/").removesuffix("/password")
            if not slug or "/" in slug:
                return json_response(404, {"error": "not_found"})
            result = await _require_session(users_store, request)
            if isinstance(result, Response):
                return result
            links_store = await key_value.open("links")
            return await links.handle_set_password(links_store, result, slug, request)

        if path.startswith("/api/links/") and path.endswith("/qr") and method == "GET":
            slug = path.removeprefix("/api/links/").removesuffix("/qr")
            if not slug or "/" in slug:
                return json_response(404, {"error": "not_found"})
            result = await _require_session(users_store, request)
            if isinstance(result, Response):
                return result
            links_store = await key_value.open("links")
            public_base_url = await variables.get("public_base_url")
            return await qr.handle_qr(links_store, result, slug, query, public_base_url)

        if path.startswith("/api/links/") and method in ("GET", "PATCH", "DELETE"):
            slug = path.removeprefix("/api/links/")
            if not slug or "/" in slug:
                return json_response(404, {"error": "not_found"})
            result = await _require_session(users_store, request)
            if isinstance(result, Response):
                return result
            links_store = await key_value.open("links")
            if method == "GET":
                return await links.handle_get(links_store, result, slug)
            if method == "PATCH":
                return await links.handle_update(links_store, result, slug, request)
            return await links.handle_delete(links_store, result, slug)

        return json_response(404, {"error": "not_found"})

    async def _login(self, users_store, request: Request, cookie_secure: bool) -> Response:
        try:
            payload = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return json_response(400, {"error": "invalid_json"})

        username = payload.get("username")
        password = payload.get("password")
        if not username or not password:
            return json_response(400, {"error": "missing_credentials"})

        provider = auth.LocalAuthProvider()
        result = await provider.authenticate(users_store, username, password)
        if result is None:
            return json_response(401, {"error": "invalid_credentials"})

        token, csrf_token = await auth.create_session(users_store, result.username, result.provider)
        return json_response(
            200,
            {"username": result.username, "role": result.role, "permissions": result.permissions, "csrf_token": csrf_token},
            headers={"set-cookie": auth.build_session_cookie(token, cookie_secure)},
        )
