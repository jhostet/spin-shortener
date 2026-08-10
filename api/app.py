import json
import sys
import time
from urllib.parse import parse_qs, urlparse

from spin_sdk import key_value, variables
from spin_sdk.http import Handler

import analytics
import auth
import backup
import bulk
import consistency
import domains
import kvprefix
import links
import obs
import qr
import urlpolicy
import users
from responses import Request, Response, get_header, json_response

# --- Toggleable structured logging (docs/plans/toggleable-logging.md) ---
#
# Both variables are read once and cached for the lifetime of the Wasm
# instance, exactly like redirect/main.go's sync.Once-guarded accessor —
# sound because a Spin variable cannot change without a redeploy (Akamai
# has no "update a variable" command) or a restart (locally), both of which
# produce a fresh instance. _obs_log_level is the cache sentinel: None means
# "not yet read", so it is checked, never _obs_debug_token (which is a
# legitimate "" when unset).
_obs_log_level: str | None = None
_obs_debug_token: str = ""


async def _obs_config() -> tuple[str, str]:
    global _obs_log_level, _obs_debug_token
    if _obs_log_level is None:
        _obs_log_level = obs.parse_log_level(await variables.get("log_level"))
        _obs_debug_token = await variables.get("log_debug_token")
    return _obs_log_level, _obs_debug_token


# Cached like the logging variables above, for the same reason: a Spin variable
# cannot change without a redeploy. None means "not yet read"; "unknown" is the
# legitimate value when no operator supplied one.
_app_version: str | None = None


async def _app_version_value() -> str:
    global _app_version
    if _app_version is None:
        _app_version = await variables.get("app_version") or "unknown"
    return _app_version


async def _kv_keys(store) -> list[str]:
    """Drain the (stream, future) pair spin:key-value/key-value@3.0.0's
    get-keys returns into a plain list. Isolated here so backup.py can take a
    list_keys callable as a parameter and stay host-importable, the same way
    gui-pages/routing.py takes read_file. Confirmed working idiom — see
    docs/plans/kv-backup-restore-scratch.md's Round 1 spike note.
    """
    reader, fut = await store.get_keys()
    keys: list[str] = []
    while True:
        chunk = await reader.read(1024)  # returns [] once the writer drops
        if not chunk:
            break
        keys.extend(chunk)
    await fut.read()
    return keys


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
        """Thin wrapper around _dispatch: builds the per-request collector
        (only when tracing is actually active — never a shared/module-level
        collector, which would silently interleave concurrent requests'
        operations, since Handler.handle() dispatches each request through
        componentize_py_async_support.spawn), measures wall time, and
        always emits exactly one log line — including on the exception path,
        which is the only evidence anyone will have of a handler that raised.
        """
        log_level, debug_token = await _obs_config()
        provided_token = get_header(request.headers, "X-SS-Debug") or ""
        traced = obs.token_matches(debug_token, provided_token)
        summary = log_level == "summary"
        collector = obs.Collector() if (traced or summary) else None

        start_ns = time.monotonic_ns()
        err = False
        try:
            response = await self._dispatch(request, collector)
        except Exception:
            err = True
            response = json_response(500, {"error": "internal_error"})
        dur_ns = time.monotonic_ns() - start_ns

        # Unconditional, unlike Server-Timing: the whole point is to answer
        # "which build is serving?" from a plain curl, including on the
        # exception path, without needing the debug token.
        response.headers["x-ss-version"] = await _app_version_value()

        if traced:
            # Server-Timing only for a valid token, never merely because
            # log_level=summary — a baseline-logging deployment must not
            # hand internal timing data to every visitor.
            response.headers["server-timing"] = obs.render_server_timing(dur_ns, collector)
            # The response now varies on a request header while this
            # component sets no Cache-Control at all, so a heuristically
            # caching intermediary could otherwise serve one visitor's
            # timing data to another.
            response.headers["vary"] = "X-SS-Debug"

        if collector is not None:
            parsed_uri = urlparse(request.uri)
            fields = [
                ("comp", "api"),
                ("route", obs.route_template(parsed_uri.path)),
                ("method", request.method),
                ("status", str(response.status)),
            ]
            if err:
                fields.append(("err", "1"))
            print(obs.render_log_line(fields, dur_ns, collector), file=sys.stderr)

        return response

    async def _dispatch(self, request: Request, collector) -> Response:
        parsed_uri = urlparse(request.uri)
        path = parsed_uri.path
        query = parse_qs(parsed_uri.query)
        method = request.method

        start_ns = time.monotonic_ns()
        physical_store = await key_value.open(kvprefix.PHYSICAL_STORE)
        if collector is not None:
            collector.record("open", "-", time.monotonic_ns() - start_ns, 0)
        stores = kvprefix.open_views(physical_store, collector)
        links_store = stores["links"]
        users_store = stores["users"]
        analytics_store = stores["analytics"]
        list_keys = kvprefix.scoped_list_keys(_kv_keys, collector)
        admin_username = await variables.get("admin_bootstrap_username")
        admin_password = await variables.get("admin_bootstrap_password")
        await auth.ensure_bootstrap_admin(users_store, admin_username, admin_password)
        cookie_secure = await _cookie_secure()
        configured_domains = domains.parse_base_urls(await variables.get("public_base_urls"))

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
                "assigned_domains": result.assigned_domains,
                "domains": domains.visible_base_urls(result.assigned_domains, configured_domains),
            })

        if path == "/api/links" and method in ("GET", "POST"):
            result = await _require_session(users_store, request)
            if isinstance(result, Response):
                return result
            if method == "GET":
                return await links.handle_list(links_store, result)
            return await links.handle_create(links_store, result, request)

        if path == "/api/links/bulk" and method == "POST":
            result = await _require_session(users_store, request)
            if isinstance(result, Response):
                return result
            return await bulk.handle_bulk_create(links_store, result, request)

        if path == "/api/links/bulk-action" and method == "POST":
            result = await _require_session(users_store, request)
            if isinstance(result, Response):
                return result
            return await bulk.handle_bulk_action(links_store, users_store, result, request)

        if path.startswith("/api/links/") and path.endswith("/password") and method == "POST":
            slug = path.removeprefix("/api/links/").removesuffix("/password")
            if not slug or "/" in slug:
                return json_response(404, {"error": "not_found"})
            result = await _require_session(users_store, request)
            if isinstance(result, Response):
                return result
            return await links.handle_set_password(links_store, result, slug, request)

        # Deliberately NOT /api/links/clicks: "clicks" is a legal custom slug
        # (CUSTOM_SLUG_PATTERN allows it), so that path would be shadowed by
        # a real link the moment anyone created one, and GET would silently
        # return totals instead of that link's record. Namespaced under
        # /api/analytics/ instead, where no slug can ever reach.
        if path == "/api/analytics/click-totals" and method == "GET":
            result = await _require_session(users_store, request)
            if isinstance(result, Response):
                return result
            return await analytics.handle_click_totals(links_store, analytics_store, result, list_keys)

        if path.startswith("/api/links/") and path.endswith("/analytics") and method == "GET":
            slug = path.removeprefix("/api/links/").removesuffix("/analytics")
            if not slug or "/" in slug:
                return json_response(404, {"error": "not_found"})
            result = await _require_session(users_store, request)
            if isinstance(result, Response):
                return result
            num_event_slots = int(await variables.get("analytics_event_slots"))
            return await analytics.handle_analytics(links_store, analytics_store, result, slug, num_event_slots)

        if path.startswith("/api/links/") and path.endswith("/qr") and method == "GET":
            slug = path.removeprefix("/api/links/").removesuffix("/qr")
            if not slug or "/" in slug:
                return json_response(404, {"error": "not_found"})
            result = await _require_session(users_store, request)
            if isinstance(result, Response):
                return result
            return await qr.handle_qr(links_store, result, slug, query, configured_domains)

        if path.startswith("/api/links/") and method in ("GET", "PATCH", "DELETE"):
            slug = path.removeprefix("/api/links/")
            if not slug or "/" in slug:
                return json_response(404, {"error": "not_found"})
            result = await _require_session(users_store, request)
            if isinstance(result, Response):
                return result
            if method == "GET":
                return await links.handle_get(links_store, result, slug)
            if method == "PATCH":
                return await links.handle_update(links_store, result, slug, request)
            return await links.handle_delete(links_store, result, slug)

        if path == "/api/users" and method in ("GET", "POST"):
            result = await _require_session(users_store, request)
            if isinstance(result, Response):
                return result
            if method == "GET":
                return await users.handle_list(users_store, result, configured_domains)
            return await users.handle_create(users_store, result, request, configured_domains)

        if path.startswith("/api/users/") and method in ("GET", "PATCH", "DELETE"):
            username = path.removeprefix("/api/users/")
            if not username or "/" in username:
                return json_response(404, {"error": "not_found"})
            result = await _require_session(users_store, request)
            if isinstance(result, Response):
                return result
            if method == "GET":
                return await users.handle_get(users_store, result, username)
            if method == "PATCH":
                return await users.handle_update(users_store, result, username, request, configured_domains)
            return await users.handle_delete(users_store, links_store, result, username, list_keys)

        if path == "/api/admin/backup" and method == "GET":
            result = await _require_session(users_store, request)
            if isinstance(result, Response):
                return result
            num_event_slots = int(await variables.get("analytics_event_slots"))
            return await backup.handle_export(
                {"links": links_store, "users": users_store, "analytics": analytics_store},
                result, query, list_keys, num_event_slots,
            )

        if path == "/api/admin/restore" and method == "POST":
            result = await _require_session(users_store, request)
            if isinstance(result, Response):
                return result
            num_event_slots = int(await variables.get("analytics_event_slots"))
            return await backup.handle_restore(
                {"links": links_store, "users": users_store, "analytics": analytics_store},
                result, request, list_keys, num_event_slots,
            )

        if path == "/api/admin/consistency" and method == "GET":
            result = await _require_session(users_store, request)
            if isinstance(result, Response):
                return result
            # The analytics view is deliberately NOT handed to
            # handle_consistency, even though the physical store backing it is
            # already open: orphan analytics keys are normal
            # (links.handle_delete never removes them), so a check over them
            # would fire on healthy state forever. See
            # docs/plans/kv-consistency-check.md's rejected alternatives.
            return await consistency.handle_consistency(
                {"links": links_store, "users": users_store}, result, list_keys,
            )

        if path == "/api/admin/url-policy/violations" and method == "GET":
            result = await _require_session(users_store, request)
            if isinstance(result, Response):
                return result
            return await urlpolicy.handle_violations(links_store, result, list_keys)

        if path == "/api/admin/url-policy" and method in ("GET", "PUT"):
            result = await _require_session(users_store, request)
            if isinstance(result, Response):
                return result
            if method == "GET":
                return await urlpolicy.handle_get_policy(links_store, result)
            return await urlpolicy.handle_put_policy(links_store, result, request)

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
