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
from dataclasses import dataclass, field
from typing import Optional

from responses import Request, get_header, iso_now, parse_cookies

PBKDF2_ITERATIONS = 100_000
SESSION_TTL_SECONDS = 8 * 60 * 60
BOOTSTRAPPED_KEY = "_meta:bootstrapped"
USERNAMES_INDEX_KEY = "_meta:usernames"

# Upper bound on the iteration count a STORED hash may claim, checked at
# verify time (both languages) and at restore (api/backup.py, the earlier
# choke point). This app always writes exactly PBKDF2_ITERATIONS (100,000);
# the cap bounds a corrupted or malicious record instead of accommodating
# variety. A hostile restore or a hand-edited store could otherwise plant an
# absurd count on one hash and turn every verification against it into an
# unbounded PBKDF2 computation — on the redirect hot path for a link
# password (Go) or in the login handler for an account (here). 1,000,000 is
# 10x the shipped value: room for a legitimate raise (current guidance is
# ~600k) while capping an attacker's CPU amplification at ~10x. Deliberately
# NOT cross-language pinned against redirect/linkgate/password.go's
# MaxStoredPBKDF2Iterations — independent policy constants, a divergence
# means a leniency difference, not a silently-broken shared format.
MAX_STORED_PBKDF2_ITERATIONS = 1_000_000

# Used only by delete_sessions_for_user below. The three existing
# f"session:{token}" literals (create_session, resolve_session,
# delete_session) are deliberately left as-is — rewriting them is a
# mechanical tidy-up with no behavioural gain. api/backup.py:43 carries its
# own identical SESSION_PREFIX by the same duplication convention its
# `BOOTSTRAPPED_KEY  # == auth.BOOTSTRAPPED_KEY` line documents.
SESSION_PREFIX = "session:"

# The fixed permission vocabulary maintained in code, per the pluggable-auth
# design — reject anything outside this set rather than silently accepting
# typos that would never actually grant anything.
KNOWN_PERMISSIONS = frozenset(
    {"links.create_custom_slug", "links.view_all", "links.edit_all", "links.tag", "users.manage"}
)

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
        # Clamp BEFORE any hashing (see MAX_STORED_PBKDF2_ITERATIONS): an
        # absurd count must cost one integer comparison, never CPU. A
        # malformed iterations_str is caught by int() below and fails closed;
        # the range check makes the failure explicit and cheap.
        iterations = int(iterations_str)
        if not (1 <= iterations <= MAX_STORED_PBKDF2_ITERATIONS):
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(hash_b64, validate=True)
        digest = _pbkdf2_hmac_sha256(password.encode("utf-8"), salt, iterations)
    except (ValueError, AttributeError, binascii.Error, OverflowError):
        return False
    return hmac.compare_digest(digest, expected)


def stored_pbkdf2_iterations(stored: str) -> int | None:
    """The iteration count a stored pbkdf2_sha256 hash claims, or None if
    `stored` is not a pbkdf2_sha256-shaped value or its count is unparseable.

    Exposed for api/backup.py's restore validation (the earlier choke point
    for the link-record CPU-amplification finding — see
    docs/plans/limit-stored-pbkdf2-iterations.md): backup validates the count
    against MAX_STORED_PBKDF2_ITERATIONS before a hostile file ever reaches a
    verifier, and shares this one parse so backup's notion of the hash shape
    can never drift from verify_password's. Kept deliberately narrow: any
    other scheme (or an unparseable count) returns None and is left to the
    verifiers' existing fail-closed behaviour, since only the iteration count
    is a CPU-amplification knob.
    """
    try:
        scheme, iterations_str, _salt_b64, _hash_b64 = stored.split("$")
    except (ValueError, AttributeError):
        return None
    if scheme != "pbkdf2_sha256":
        return None
    try:
        return int(iterations_str)
    except ValueError:
        return None


@dataclass
class Principal:
    username: str
    role: str
    permissions: list[str]
    csrf_token: str
    # A convenience guardrail, not a security control — restricts which
    # short-link domains the GUI's selector offers this user. Deliberately
    # not in KNOWN_PERMISSIONS (see the module comment above); appended last
    # with a default so every existing keyword-argument construction site
    # keeps working unchanged.
    assigned_domains: list[str] = field(default_factory=list)

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


async def list_usernames(store) -> list[str]:
    raw = await store.get(USERNAMES_INDEX_KEY)
    return json.loads(raw) if raw else []


async def add_username(store, username: str) -> None:
    usernames = await list_usernames(store)
    if username not in usernames:
        usernames.append(username)
        await store.set(USERNAMES_INDEX_KEY, json.dumps(usernames).encode("utf-8"))


async def remove_username(store, username: str) -> None:
    usernames = await list_usernames(store)
    if username in usernames:
        usernames.remove(username)
        await store.set(USERNAMES_INDEX_KEY, json.dumps(usernames).encode("utf-8"))


class LocalAuthProvider:
    provider_id = "local"
    login_type = "credentials"

    async def authenticate(self, store, username: str, password: str) -> Optional[AuthResult]:
        user = await get_user(store, username)
        if user is None or user.get("disabled"):
            return None
        stored_hash = user.get("password_hash")
        if not stored_hash:
            # A restored account carries no password hash by design (see
            # docs/plans/kv-backup-restore.md). Defined here as an explicit
            # "cannot authenticate" rather than left to verify_password: the
            # old user["password_hash"] raised KeyError, which the SDK's bare
            # except turned into a 500 instead of a 401.
            return None
        if not verify_password(password, stored_hash):
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
        "assigned_domains": [],
        "provider": "local",
        "disabled": False,
        "created_at": iso_now(),
    }
    await put_user(store, user)
    await add_username(store, username)
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
        assigned_domains=user.get("assigned_domains", []),
    )


async def delete_session(store, request: Request) -> None:
    cookie_header = get_header(request.headers, "cookie")
    if not cookie_header:
        return
    token = parse_cookies(cookie_header).get("session")
    if token:
        await store.delete(f"session:{token}")


async def delete_sessions_for_user(store, username: str, list_keys) -> int:
    """Delete every session:<token> in `store` whose record names `username`.
    Returns the number deleted.

    `list_keys` is the same callable api/app.py passes to backup.handle_export
    (the get-keys drain), taken as a parameter so this module stays
    host-importable with zero spin_sdk imports.

    A session value that has vanished between the key listing and the read,
    or that is not valid JSON, is skipped rather than raised on: this runs
    inside user deletion, and a single malformed session record must never
    turn a delete into a 500.
    """
    deleted = 0
    for key in await list_keys(store):
        if not key.startswith(SESSION_PREFIX):
            continue
        raw = await store.get(key)
        if raw is None:
            continue
        try:
            session = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if session.get("username") == username:
            await store.delete(key)
            deleted += 1
    return deleted


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
