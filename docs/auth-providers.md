# Adding SAML/OIDC as auth providers

`api/auth.py` today has exactly one provider, `LocalAuthProvider`, but the session/permission model around it was designed so a redirect-based provider (SAML or OIDC) can be added later without reworking anything else. This document shows concretely how that would work — it does not implement either provider.

## The seam that already exists

- `AuthResult` (`username`, `role`, `permissions`, `provider`) is the one shape every provider produces, regardless of how it authenticated the user.
- `create_session(store, username, provider)` is the one function that turns an `AuthResult` into a live session — it doesn't care which provider produced it.
- User records already have a `provider` field (`"local"` today), so a federated user's record looks identical to a local one except for that field and having no usable `password_hash`.
- `LocalAuthProvider.login_type = "credentials"` — a plain string tag, not an enforced interface, but it's the seam a router would switch on: credential-type providers get a login form POST, redirect-type providers get a `GET` that 302s to the IdP.

## What a redirect-type provider needs that `LocalAuthProvider` doesn't

A credential provider (`LocalAuthProvider`) is called synchronously with a username/password and returns an `AuthResult` or `None` — one function call, no state in between. A redirect-type provider is a two-step flow spanning two separate HTTP requests (the browser leaves and comes back), so it needs two entrypoints instead of one:

```python
class SamlAuthProvider:  # or OidcAuthProvider — same shape
    provider_id = "saml"          # or "oidc"
    login_type = "redirect"

    async def initiate(self, request: Request) -> Response:
        """GET /api/auth/saml/login — redirect the browser to the IdP."""
        ...

    async def handle_callback(self, store, request: Request) -> Optional[AuthResult]:
        """GET /api/auth/saml/callback — validate the IdP's response, return an AuthResult or None."""
        ...
```

`app.py`'s routing would add two new endpoints per redirect-type provider (`/api/auth/{provider}/login`, `/api/auth/{provider}/callback`), each doing exactly what the existing `/api/auth/login` does today after getting an `AuthResult`: call `create_session`, set the cookie via `build_session_cookie`, return the same JSON shape. No change to `resolve_session`, `check_csrf`, `Principal`, or any of `links.py`/`qr.py`/`analytics.py`/`users.py` — they only ever see a `Principal`, never a provider.

## State needed between `initiate` and `handle_callback`

Both SAML and OIDC need to correlate the callback with the request that started it (CSRF/replay protection for the auth flow itself) — OIDC via `state`/`nonce`/PKCE verifier, SAML via a `RelayState` and the expected `InResponseTo`. This needs a short-lived KV record, reserved now as `oauth_state:<state>` in the `users` store (not a new store — this is auth-flow bookkeeping, same trust boundary as sessions), holding whatever the provider needs to validate the callback (e.g. `{nonce, pkce_verifier, created_at}` for OIDC). `initiate` writes it, `handle_callback` reads-and-deletes it (single use). No `retentionDays`-style trimming needed — these are single-use and short-lived (a few minutes), so a lazy expiry check on read (like sessions already do) is enough.

## JIT (just-in-time) user provisioning

A federated login won't have a pre-existing local account in most setups. On a successful `handle_callback`, if no local user is mapped to that IdP subject yet, create one:

- New KV key pattern: `federated_identity:<provider_id>:<subject>` → `username`, so repeat logins from the same IdP subject resolve to the same local account.
- The JIT-created `user:<username>` record defaults to `role: "user"`, `permissions: []`, `password_hash: null` (a federated user has no local password — `LocalAuthProvider.authenticate` already returns `None` for this via the existing `verify_password` failing closed on a non-conforming stored value, so a federated user simply can't log in via the local form, which is correct).
- `username` for a JIT-provisioned user needs a real collision/derivation strategy (e.g. from the IdP's email/subject claim, with a numeric suffix on collision) — not designed here since it's provider-specific policy, not an architecture question.

## Config additions

Per provider, `spin.toml` would need:
- `[component.api.variables]` entries: `{provider}_client_id`, `{provider}_client_secret` (`secret = true`), `{provider}_issuer_url` (OIDC) or `{provider}_idp_metadata_url`/`{provider}_idp_cert` (SAML).
- `allowed_outbound_hosts` expanded on `[component.api]` (not `redirect` — auth never touches the hot path) to include the IdP's token/metadata endpoint host. `api` currently has no `allowed_outbound_hosts` entry at all (meaning zero outbound access today, confirmed — see CLAUDE.md's "Security tradeoffs" section), so this is a deliberate, visible widening of `api`'s network access, not a silent one.

## Why this isn't built now

Confirmed decision from the original design: v1 ships local auth only. The seam above is real (every piece it depends on — `AuthResult`, `create_session`, the `provider` field — already exists in shipped code) but building an actual SAML or OIDC client means picking specific libraries for XML signature validation (SAML) or JWT/JWKS validation (OIDC), both of which are non-trivial to get right and both of which need to work under componentize-py's WASI constraints (pure-Python only, same limitation that ruled out `hashlib.pbkdf2_hmac` and forced the manual PBKDF2 implementation in `auth.py`) — that's real, scoped follow-up work, not a design gap.
