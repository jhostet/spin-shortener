"""Local authentication, sessions, and the pluggable AuthProvider seam.

`LocalAuthProvider` is the only provider implemented today. Future SAML/OIDC
providers would be redirect-based (`initiate`/`handle_callback`) rather than
credential-based, but would issue sessions through the same `create_session`
call so the session/permission model never needs to change to add them.
"""

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from responses import Request, get_header, iso_now, parse_cookies

PBKDF2_ITERATIONS = 100_000
SESSION_TTL_SECONDS = 8 * 60 * 60
BOOTSTRAPPED_KEY = "_meta:bootstrapped"

_SHA256_DIGEST_SIZE = 32


def _pbkdf2_hmac_sha256(password: bytes, salt: bytes, iterations: int, dklen: int = _SHA256_DIGEST_SIZE) -> bytes:
    """RFC 8018 PBKDF2-HMAC-SHA256, implemented from hmac/hashlib primitives only.

    The WASI-compiled CPython bundled by componentize-py 0.23.0 does not expose
    `hashlib.pbkdf2_hmac` (confirmed by a build/run spike), so this can't rely on
    the stdlib convenience function; it only depends on `hmac.new` + `hashlib.sha256`.
    """
    num_blocks = -(-dklen // _SHA256_DIGEST_SIZE)  # ceil division
    output = bytearray()
    for block_num in range(1, num_blocks + 1):
        u = hmac.new(password, salt + block_num.to_bytes(4, "big"), hashlib.sha256).digest()
        block = bytearray(u)
        for _ in range(iterations - 1):
            u = hmac.new(password, u, hashlib.sha256).digest()
            for i in range(_SHA256_DIGEST_SIZE):
                block[i] ^= u[i]
        output += block
    return bytes(output[:dklen])


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = _pbkdf2_hmac_sha256(password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations_str, salt_b64, hash_b64 = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(hash_b64, validate=True)
        digest = _pbkdf2_hmac_sha256(password.encode("utf-8"), salt, int(iterations_str))
    except (ValueError, AttributeError, binascii.Error):
        return False
    return hmac.compare_digest(digest, expected)


@dataclass
class Principal:
    username: str
    role: str
    permissions: list[str]
    csrf_token: str

    def has_permission(self, permission: str) -> bool:
        return self.role == "admin" or permission in self.permissions


@dataclass
class AuthResult:
    username: str
    role: str
    permissions: list[str]
    provider: str


async def get_user(store, username: str) -> Optional[dict]:
    raw = await store.get(f"user:{username}")
    if raw is None:
        return None
    return json.loads(raw)


async def put_user(store, user: dict) -> None:
    await store.set(f"user:{user['username']}", json.dumps(user).encode("utf-8"))


class LocalAuthProvider:
    provider_id = "local"
    login_type = "credentials"

    async def authenticate(self, store, username: str, password: str) -> Optional[AuthResult]:
        user = await get_user(store, username)
        if user is None or user.get("disabled"):
            return None
        if not verify_password(password, user["password_hash"]):
            return None
        return AuthResult(
            username=user["username"],
            role=user["role"],
            permissions=user["permissions"],
            provider=user.get("provider", "local"),
        )


async def ensure_bootstrap_admin(store, username: str, password: str) -> None:
    if await store.exists(BOOTSTRAPPED_KEY):
        return

    user = {
        "username": username,
        "password_hash": hash_password(password),
        "role": "admin",
        "permissions": [],
        "provider": "local",
        "disabled": False,
        "created_at": iso_now(),
    }
    await put_user(store, user)
    await store.set(BOOTSTRAPPED_KEY, b"1")


async def create_session(store, username: str, provider: str) -> tuple[str, str]:
    """Issue a new session for `username`, regardless of which AuthProvider authenticated them.

    Returns (session_token, csrf_token).
    """
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(16)
    issued_at = int(time.time())
    session = {
        "username": username,
        "csrf_token": csrf_token,
        "issued_at": issued_at,
        "expires_at": issued_at + SESSION_TTL_SECONDS,
        "auth_provider": provider,
    }
    await store.set(f"session:{token}", json.dumps(session).encode("utf-8"))
    return token, csrf_token


async def resolve_session(store, request: Request) -> Optional[Principal]:
    cookie_header = get_header(request.headers, "cookie")
    if not cookie_header:
        return None
    token = parse_cookies(cookie_header).get("session")
    if not token:
        return None

    raw = await store.get(f"session:{token}")
    if raw is None:
        return None
    session = json.loads(raw)

    if session["expires_at"] < int(time.time()):
        await store.delete(f"session:{token}")
        return None

    user = await get_user(store, session["username"])
    if user is None or user.get("disabled"):
        return None

    return Principal(
        username=user["username"],
        role=user["role"],
        permissions=user["permissions"],
        csrf_token=session["csrf_token"],
    )


async def delete_session(store, request: Request) -> None:
    cookie_header = get_header(request.headers, "cookie")
    if not cookie_header:
        return
    token = parse_cookies(cookie_header).get("session")
    if token:
        await store.delete(f"session:{token}")


def check_csrf(request: Request, principal: Principal) -> bool:
    if request.method not in ("POST", "PATCH", "PUT", "DELETE"):
        return True
    header_token = get_header(request.headers, "x-csrf-token")
    return header_token is not None and hmac.compare_digest(header_token, principal.csrf_token)


def build_session_cookie(token: str, cookie_secure: bool) -> str:
    flags = "Path=/; HttpOnly; SameSite=Lax"
    if cookie_secure:
        flags += "; Secure"
    return f"session={token}; Max-Age={SESSION_TTL_SECONDS}; {flags}"


def build_logout_cookie(cookie_secure: bool) -> str:
    flags = "Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
    if cookie_secure:
        flags += "; Secure"
    return f"session=; {flags}"
