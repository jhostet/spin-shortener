"""Destination URL policy: the pure rule model (normalization, host matching,
precedence evaluation, whole-document parsing) plus the three
/api/admin/url-policy handlers built on top of it.

Zero WASI SDK imports — `store` arrives as a plain parameter and
`Request`/`Response`/`json_response`/`iso_now` come from `responses`,
matching the testability rule the rest of `api/` follows (see `CLAUDE.md`).
`api/tags.py`, `api/domains.py` and `api/consistency.py` are the models.
Imports nothing from `links.py`, so `links.py` can import this module with no
cycle — the same rule `tags.py`'s docstring states.

See docs/plans/destination-url-policy.md for the full design, the precedence
rule, the matching semantics and the rejected alternatives.

**Precedence, complete: a deny rule always wins; otherwise an allow rule (or
a default_action of "allow") lets it through; otherwise it is blocked.**
Deny-wins was chosen over "most specific match wins" because a misunderstood
specificity rule fails open, where deny-wins fails closed.

**An absent `_meta:url_policy` key allows everything.** No deployment changes
behaviour on upgrade, and nothing needs a migration or a backfill.
"""

import json
import re
from urllib.parse import urlparse

from responses import Response, iso_now, json_response

POLICY_KEY = "_meta:url_policy"  # in the LINKS store
POLICY_SCHEMA_VERSION = 1
ACTIONS = ("allow", "deny")
EMPTY_POLICY = {"version": 1, "default_action": "allow", "rules": []}

MAX_POLICY_RULES = 200
MAX_RULE_HOST_LENGTH = 253
MAX_RULE_NOTE_LENGTH = 200
MAX_POLICY_BODY_BYTES = 65_536
MAX_VIOLATIONS = 100
VIOLATIONS_FORMAT = "spin-shortener-url-policy-violations"

_LDH_LABEL = r"[a-z0-9]([a-z0-9-]*[a-z0-9])?"
_HOST_PATTERN = re.compile(rf"^{_LDH_LABEL}(\.{_LDH_LABEL})*$")


# --- pure ---


def normalize_rule_host(value: str) -> str | None:
    """Permissive on input, strict on storage — the same posture
    `domains.normalize_base_url` takes for base URLs, but a separate
    function, since a bare host rule has no scheme to require.

    Accepts a bare host, a scheme-qualified URL, a host with a trailing dot,
    a wildcard prefix (`*.evil.com`), surrounding whitespace and a port —
    all stripped down to a lowercased, dot-free, port-free host. Returns
    `None` for anything empty, too long, or that doesn't match a valid
    LDH hostname after stripping (non-ASCII, embedded whitespace, a leftover
    path, a leading/trailing hyphen).
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None

    if "://" in candidate:
        host = urlparse(candidate).hostname
        if not host:
            return None
        candidate = host
    else:
        # No scheme: still allow a bare "host:port" or "host/path" shape by
        # stripping a port and a path the same way, without invoking a full
        # URL parse that would require a scheme to find netloc.
        candidate = candidate.split("/", 1)[0]
        if candidate.count(":") == 1:
            candidate = candidate.split(":", 1)[0]

    if candidate.startswith("*."):
        candidate = candidate[2:]

    candidate = candidate.rstrip(".").lower()

    if not candidate or len(candidate) > MAX_RULE_HOST_LENGTH:
        return None
    if not _HOST_PATTERN.match(candidate):
        return None
    return candidate


def destination_host(target_url: str) -> str | None:
    """The lowercased, port-free, trailing-dot-free host of `target_url`, or
    `None` if there isn't one.

    Uses `.hostname`, never `.netloc` — `.netloc` includes userinfo, so
    `https://example.com@evil.com/x` would spoof a rule for `example.com`
    while the real destination is `evil.com`. `.hostname` strips userinfo,
    port and IPv6 brackets and lowercases ASCII, but does not strip a
    trailing dot, so that is done here explicitly.
    """
    try:
        host = urlparse(target_url).hostname
    except ValueError:
        return None
    if not host:
        return None
    return host.rstrip(".").lower() or None


def host_matches(host: str, rule_host: str) -> bool:
    """`rule_host` matches `host` exactly, or any subdomain of it.

    The leading "." in the suffix is the whole correctness of this function:
    a bare `host.endswith(rule_host)` would match "notexample.com" against a
    rule for "example.com". Confirmed: "notexample.com".endswith(".example.com")
    is False; "evil.example.com".endswith(".example.com") is True.
    """
    return host == rule_host or host.endswith("." + rule_host)


def is_active(policy: dict) -> bool:
    """A policy with no rules and a default of allow is indistinguishable
    from no policy at all, and is treated the same way for the
    "unparsable_target_url" special case."""
    return policy.get("default_action") == "deny" or bool(policy.get("rules"))


def evaluate(target_url: str, policy: dict) -> dict:
    """The single source of truth for the allow/deny decision. Both the
    enforcement error body and the violations report are built from this
    return value, so the two can never disagree about why something was
    blocked.

    Returns a fixed-shape dict, every key always present:
        {"allowed": bool, "host": str | None, "reason": str, "matched_rule": str | None}

    `reason` is one of "no_policy", "allowed_by_default", "allowed_by_rule",
    "denied_by_rule", "not_allowed_by_default", "unparsable_target_url".
    """
    active = is_active(policy)
    host = destination_host(target_url)

    if host is None:
        # A URL that clears is_valid_target_url but yields no host (e.g.
        # "https://user@/path"). Allowed when the policy is inactive
        # (preserves "no policy configured behaves exactly as today");
        # blocked the moment a policy exists (fail-closed).
        return {
            "allowed": not active,
            "host": None,
            "reason": "no_policy" if not active else "unparsable_target_url",
            "matched_rule": None,
        }

    if not active:
        return {"allowed": True, "host": host, "reason": "no_policy", "matched_rule": None}

    for rule in policy.get("rules", []):
        if rule.get("action") == "deny" and host_matches(host, rule.get("host", "")):
            return {"allowed": False, "host": host, "reason": "denied_by_rule", "matched_rule": rule["host"]}

    if policy.get("default_action") == "allow":
        return {"allowed": True, "host": host, "reason": "allowed_by_default", "matched_rule": None}

    for rule in policy.get("rules", []):
        if rule.get("action") == "allow" and host_matches(host, rule.get("host", "")):
            return {"allowed": True, "host": host, "reason": "allowed_by_rule", "matched_rule": rule["host"]}

    return {"allowed": False, "host": host, "reason": "not_allowed_by_default", "matched_rule": None}


def parse_policy_document(value, *, now: str, actor: str) -> tuple[dict | None, dict | None]:
    """All-or-nothing. Returns (policy, None) or (None, error_body) — the
    same shape as `tags.parse_tags` and `backup.parse_stores_param`.

    Normalizes every rule host, de-duplicates on (host, action), preserves
    `created_at`/`created_by` for rules already present in `value` by
    host+action, stamps `now`/`actor` on new ones, and sorts rules by host
    then action so two saves of the same set are byte-identical.
    """
    if not isinstance(value, dict):
        return None, {"error": "invalid_policy"}

    default_action = value.get("default_action")
    if default_action not in ACTIONS:
        return None, {"error": "invalid_default_action", "allowed_actions": list(ACTIONS)}

    raw_rules = value.get("rules")
    if not isinstance(raw_rules, list):
        return None, {"error": "invalid_policy"}

    seen: dict[tuple[str, str], dict] = {}
    for raw_rule in raw_rules:
        if not isinstance(raw_rule, dict):
            return None, {"error": "invalid_policy"}

        submitted_host = raw_rule.get("host")
        action = raw_rule.get("action")
        if action not in ACTIONS:
            return None, {
                "error": "invalid_rule_action",
                "host": submitted_host,
                "allowed_actions": list(ACTIONS),
            }

        host = normalize_rule_host(submitted_host) if isinstance(submitted_host, str) else None
        if host is None:
            return None, {
                "error": "invalid_rule_host",
                "host": submitted_host,
                "max_length": MAX_RULE_HOST_LENGTH,
            }

        note = raw_rule.get("note")
        if note is not None and (not isinstance(note, str) or len(note) > MAX_RULE_NOTE_LENGTH):
            return None, {"error": "invalid_rule_note", "host": host, "max_length": MAX_RULE_NOTE_LENGTH}

        key = (host, action)
        rule = {
            "host": host,
            "action": action,
            "note": note,
            "created_at": raw_rule.get("created_at") if isinstance(raw_rule.get("created_at"), str) else now,
            "created_by": raw_rule.get("created_by") if isinstance(raw_rule.get("created_by"), str) else actor,
        }
        seen[key] = rule

    rules = sorted(seen.values(), key=lambda r: (r["host"], r["action"]))

    if len(rules) > MAX_POLICY_RULES:
        return None, {"error": "too_many_rules", "max_rules": MAX_POLICY_RULES, "rule_count": len(rules)}

    return {
        "version": POLICY_SCHEMA_VERSION,
        "default_action": default_action,
        "rules": rules,
        "updated_at": now,
        "updated_by": actor,
    }, None


def _parse_policy(raw: bytes) -> dict | None:
    """`None` for a value that isn't a parseable, minimally-shaped policy
    document — treated as absent/corrupted by callers, never raised on."""
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    if value.get("default_action") not in ACTIONS:
        return None
    if not isinstance(value.get("rules"), list):
        return None
    return value


# --- store I/O, store passed in ---


async def load_policy(store) -> dict:
    """`EMPTY_POLICY` when the key is absent or unparseable — a malformed
    policy must not turn every link creation into a 500. The consistency
    report is what surfaces a corrupted value; enforcement fails open to
    "no policy" instead, matching "an absent key allows everything"."""
    raw = await store.get(POLICY_KEY)
    if raw is None:
        return dict(EMPTY_POLICY)
    parsed = _parse_policy(raw)
    if parsed is None:
        return dict(EMPTY_POLICY)
    return parsed


async def save_policy(store, policy: dict) -> None:
    await store.set(POLICY_KEY, json.dumps(policy).encode("utf-8"))


# --- handlers ---


async def handle_get_policy(store, principal) -> Response:
    if not principal.has_permission("users.manage"):
        return json_response(403, {"error": "forbidden", "required_permission": "users.manage"})
    policy = await load_policy(store)
    return json_response(200, policy)


async def handle_put_policy(store, principal, request) -> Response:
    if not principal.has_permission("users.manage"):
        return json_response(403, {"error": "forbidden", "required_permission": "users.manage"})

    if len(request.body or b"") > MAX_POLICY_BODY_BYTES:
        return json_response(413, {"error": "body_too_large", "max_bytes": MAX_POLICY_BODY_BYTES})

    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return json_response(400, {"error": "invalid_policy"})

    policy, error = parse_policy_document(payload, now=iso_now(), actor=principal.username)
    if error:
        return json_response(400, error)

    await save_policy(store, policy)
    return json_response(200, policy)


async def handle_violations(store, principal, list_keys) -> Response:
    if not principal.has_permission("users.manage"):
        return json_response(403, {"error": "forbidden", "required_permission": "users.manage"})

    policy = await load_policy(store)

    keys = await list_keys(store)
    scanned = 0
    all_violations: list[dict] = []
    for key in keys:
        if not key.startswith("slug:"):
            continue
        scanned += 1
        raw = await store.get(key)
        if raw is None:
            continue
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(record, dict):
            continue

        target_url = record.get("target_url")
        if not isinstance(target_url, str):
            continue
        verdict = evaluate(target_url, policy)
        if verdict["allowed"]:
            continue

        all_violations.append({
            "slug": key[len("slug:"):],
            "owner": record.get("owner"),
            "status": record.get("status"),
            "target_url": target_url,
            "host": verdict["host"],
            "reason": verdict["reason"],
            "matched_rule": verdict["matched_rule"],
        })

    all_violations.sort(key=lambda v: v["slug"])
    count = len(all_violations)
    truncated = count > MAX_VIOLATIONS

    return json_response(200, {
        "format": VIOLATIONS_FORMAT,
        "schema_version": POLICY_SCHEMA_VERSION,
        "generated_at": iso_now(),
        "generated_by": principal.username,
        "policy_default_action": policy.get("default_action"),
        "rule_count": len(policy.get("rules", [])),
        "scanned": {"links": scanned},
        "count": count,
        "truncated": truncated,
        "max_violations": MAX_VIOLATIONS,
        "violations": all_violations[:MAX_VIOLATIONS],
    })
