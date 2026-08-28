"""Read-only KV consistency check: a two-store walk (links, users) that
reports where the users index and sessions have drifted out of step with
the records they're supposed to describe, plus corrupt/unrecognized values
in either store.

Zero WASI SDK imports — `store` objects and the `list_keys` callable arrive
as plain parameters, and `Response`/`json_response`/`iso_now` come from
`responses`, matching the testability rule the rest of `api/` follows (see
`CLAUDE.md`). `api/backup.py` is the model, line for line.

It reports; it never repairs. See docs/plans/kv-consistency-check.md for the
original twelve-check design and docs/plans/derived-link-indexes.md for why
six of them (every check about `all_links`/`owner_links:<owner>`) were
retired: since that plan's Stage 2, links.py no longer writes either index at
all — `GET /api/links` and every authoring handler derive slug lists and
ownership from a `slug:` key enumeration instead (`links.enumerate_slugs`,
`links.slugs_owned_by`) — so there is no index left for those six checks to
find drifted. `unknown_link_owner` survives as the one remaining owner check:
a record naming an owner with no user: record is still real drift, derived
entirely from `link_records` with no index involved.

A `user:` record's VALUE is never read here — only its key name — so this
module can never hold a password_hash. The remaining checks need only key
names (`unindexed_user`, `missing_user_record`) or the `username` field of a
`session:` value (`orphan_session`).
"""

import json

import obs
from responses import Response, iso_now, json_response

MAX_FINDINGS_PER_CHECK = 100

CONSISTENCY_FORMAT = "spin-shortener-consistency-report"
SCHEMA_VERSION = 1

CONSISTENCY_STORES = ("links", "users")

USERNAMES_INDEX_KEY = "_meta:usernames"  # == auth.USERNAMES_INDEX_KEY
BOOTSTRAPPED_KEY = "_meta:bootstrapped"  # == auth.BOOTSTRAPPED_KEY
SLUG_PREFIX = "slug:"
URL_POLICY_KEY = "_meta:url_policy"  # == urlpolicy.POLICY_KEY
USER_PREFIX = "user:"  # == backup.USER_PREFIX
SESSION_PREFIX = "session:"  # == auth.SESSION_PREFIX

# docs/plans/derived-link-indexes.md, Stage 2: all_links and every
# owner_links:<U> are inert leftover keys now, not a maintained index —
# nothing writes them any more. They must be recognised as a KNOWN shape (the
# same treatment _meta:bootstrapped already gets below) or they would report
# as unrecognized_key on every single run, forever, on any store that was
# ever used before this change landed. They are never parsed or acted on.
ALL_SLUGS_INDEX_KEY = "all_links"  # leftover, inert — see module docstring
OWNER_LINKS_PREFIX = "owner_links:"  # leftover, inert — see module docstring

# Ordered. Every check appears in every report, at count 0 when clean.
CHECKS: tuple[tuple[str, str], ...] = (
    ("unknown_link_owner", "warning"),
    ("unindexed_user", "warning"),
    ("missing_user_record", "info"),
    ("orphan_session", "warning"),
    ("unreadable_value", "warning"),
    ("unrecognized_key", "info"),
)


# The three checks with exactly one safe, derivable, automatic repair, in
# CHECKS order — consistencyrepair.py's whole mandate. Named here, not in the
# repairing module, so `build_report` can publish it without `consistency`
# importing its repairing sibling (which imports `consistency`). See
# docs/plans/consistency-repair.md for the per-check verdict and why the
# other three (unknown_link_owner, unreadable_value, unrecognized_key) are
# never repaired. The five index-repair branches this list used to name
# (unindexed_link, missing_link_record, unindexed_owner_link,
# orphan_owner_index_entry, dangling_owner_index) were deleted along with the
# checks and the indexes they repaired — docs/plans/derived-link-indexes.md.
REPAIRABLE_CHECKS: tuple[str, ...] = (
    "unindexed_user",
    "missing_user_record",
    "orphan_session",
)


def _decode_json(raw: bytes) -> tuple[object | None, str | None]:
    """Shared decoder for all four parse helpers below. Returns (value, None)
    on success, or (None, reason) on failure, where `reason` is a sanitized
    decoder message — never the raw exception text.

    A finding's reason is ALWAYS one of exactly two things, and never anything
    else: a fixed, data-free literal from a shape check below, or a decoder
    message routed through obs.sanitize_error_message. **No reason ever
    interpolates a value read from the store.** That is the property that
    keeps this report free of credential material (a links:slug:<slug>
    record legitimately carries the link's own pbkdf2_sha256 password hash),
    and it is structural rather than incidental: a shape check has nothing
    but a literal to report, and the decoder path is sanitized by
    construction.

    The reason is prose for a human, NOT a machine-readable code — no client
    may switch on it, and the GUI renders it verbatim.
    """
    try:
        return json.loads(raw), None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        sanitized, _redacted, _truncated = obs.sanitize_error_message(str(exc))
        return None, sanitized or "value did not decode as JSON"


def parse_str_list_with_reason(raw: bytes | None) -> tuple[list[str] | None, str | None]:
    """(None, None) for an absent key; (value, None) on success; (None,
    reason) for a malformed value — callers distinguish "absent" from
    "malformed" by checking `raw is None` themselves.

    Renamed from `parse_str_list` (docs/plans/api-record-unreadable-diagnostics.md):
    a caller that unpacks only one value from the old function's tuple-shaped
    return would get an always-truthy tuple, so a missed call site would
    silently disable a guard rather than fail loudly. The rename makes a
    missed call site an immediate AttributeError instead."""
    if raw is None:
        return None, None
    value, reason = _decode_json(raw)
    if reason is not None:
        return None, reason
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None, "not a JSON array of strings"
    return value, None


def _parse_link_record(raw: bytes | None) -> tuple[dict | None, str | None]:
    """Only ever extracts `owner`. Never used on a `user:` record."""
    if raw is None:
        return None, None
    value, reason = _decode_json(raw)
    if reason is not None:
        return None, reason
    if not isinstance(value, dict):
        return None, "not a JSON object"
    owner = value.get("owner")
    if not isinstance(owner, str):
        return None, "owner field missing or not a string"
    return {"owner": owner}, None


def _parse_policy(raw: bytes) -> tuple[dict | None, str | None]:
    """`None` if `raw` doesn't parse as a minimally-shaped policy document.
    Mirrors `urlpolicy._parse_policy` exactly (duplicated rather than
    imported, since this module reads only far enough to classify the value
    as readable/unreadable — no field of it feeds any of the twelve checks)."""
    value, reason = _decode_json(raw)
    if reason is not None:
        return None, reason
    if not isinstance(value, dict):
        return None, "not a JSON object"
    if value.get("default_action") not in ("allow", "deny"):
        return None, "default_action must be allow or deny"
    if not isinstance(value.get("rules"), list):
        return None, "rules must be a list"
    return value, None


def _parse_session_username(raw: bytes | None) -> tuple[str | None, str | None]:
    if raw is None:
        return None, None
    value, reason = _decode_json(raw)
    if reason is not None:
        return None, reason
    if not isinstance(value, dict):
        return None, "not a JSON object"
    username = value.get("username")
    if not isinstance(username, str):
        return None, "username field missing or not a string"
    return username, None


async def collect(stores_by_name: dict[str, object], list_keys, get_many) -> dict:
    """The only I/O in this module. Returns the raw material `analyze` needs:

        {
          "link_records": {slug: {"owner": str}},   # parsed slug:<slug> records
          "usernames": list[str] | None,            # None only if unreadable
          "user_records": set[str],                 # from user: KEY NAMES only
          "session_usernames": list[str],           # from session: values
          "sessions_by_username": {username: [session KEY names]},
          "unreadable": [{"store": str, "key": str, "reason": str}],
          "unrecognized": [{"store": str, "key": str}],
          "scanned": {...},
        }

    Never raises on malformed data: every parse failure becomes an entry in
    `unreadable` and the key is excluded from everything else. A diagnostic
    that 500s on a broken store fails exactly when it is needed.

    An absent index key (`_meta:usernames`) is treated as an empty list, not
    as unreadable — a missing index is real drift `unindexed_user`/
    `missing_user_record` must still report, whereas a present-but-malformed
    value can't be trusted for either check, so both are skipped instead.

    A `user:` record's VALUE is never read — only its key name — so this
    function can never hold a password_hash.
    """
    links_store = stores_by_name["links"]
    users_store = stores_by_name["users"]

    link_records: dict[str, dict] = {}
    unreadable: list[dict] = []
    unrecognized: list[dict] = []

    links_keys = await list_keys(links_store)
    # One get_many host call for the whole store (docs/plans/batch-kv-reads.md,
    # superseding the earlier gather_reads-based fan-out) rather than a round
    # trip per key. Every key is fetched, including ones the loop classifies
    # as `unrecognized` and discards: an allowlist here would have to restate
    # the branch conditions below, and a condition that drifted out of it
    # would silently hand the loop `None` and report a healthy key as
    # unreadable.
    links_values = await get_many(links_store, links_keys)
    slug_count = 0
    for key in links_keys:
        if key == ALL_SLUGS_INDEX_KEY or key.startswith(OWNER_LINKS_PREFIX):
            # docs/plans/derived-link-indexes.md, Stage 2: known-and-inert,
            # the same treatment BOOTSTRAPPED_KEY gets below. Never parsed,
            # never reported — reporting it as unrecognized_key would fire on
            # every single run forever for any store that predates this
            # change, and there is nothing left that could act on its content.
            continue
        elif key.startswith(SLUG_PREFIX):
            slug_count += 1
            slug = key[len(SLUG_PREFIX):]
            raw = links_values[key]
            record, reason = _parse_link_record(raw)
            if reason is not None:
                unreadable.append({"store": "links", "key": key, "reason": reason})
            elif record is not None:
                link_records[slug] = record
        elif key == URL_POLICY_KEY:
            # Known shape. Parsed only far enough to report a corrupted
            # policy as unreadable_value — no new check id, and no field of
            # it is needed by any of the remaining checks.
            raw = links_values[key]
            if raw is not None:
                _value, reason = _parse_policy(raw)
                if reason is not None:
                    unreadable.append({"store": "links", "key": key, "reason": reason})
        else:
            unrecognized.append({"store": "links", "key": key})

    usernames: list[str] | None = []
    user_records: set[str] = set()
    session_usernames: list[str] = []
    sessions_by_username: dict[str, list[str]] = {}

    users_keys = await list_keys(users_store)
    # Deliberately NOT every key, unlike the links store above: a `user:`
    # record's value holds a PBKDF2 hash and must never be read here, so this
    # is an explicit allowlist of the two shapes the loop below actually reads.
    # Indexed with `[]` rather than `.get()` on purpose — a future branch that
    # reads a value without being added here fails loudly instead of silently
    # seeing None and reporting a healthy key as unreadable.
    users_value_keys = [
        key for key in users_keys
        if key == USERNAMES_INDEX_KEY or key.startswith(SESSION_PREFIX)
    ]
    users_values = await get_many(users_store, users_value_keys)
    user_count = 0
    session_count = 0
    for key in users_keys:
        if key == USERNAMES_INDEX_KEY:
            raw = users_values[key]
            parsed, reason = parse_str_list_with_reason(raw)
            if reason is not None:
                unreadable.append({"store": "users", "key": key, "reason": reason})
                usernames = None
            elif parsed is not None:
                usernames = parsed
        elif key == BOOTSTRAPPED_KEY:
            continue  # known shape; carries no content any check needs
        elif key.startswith(USER_PREFIX):
            user_count += 1
            user_records.add(key[len(USER_PREFIX):])
        elif key.startswith(SESSION_PREFIX):
            session_count += 1
            raw = users_values[key]
            username, reason = _parse_session_username(raw)
            if reason is not None:
                unreadable.append({"store": "users", "key": key, "reason": reason})
            elif username is not None:
                session_usernames.append(username)
                sessions_by_username.setdefault(username, []).append(key)
        else:
            unrecognized.append({"store": "users", "key": key})

    return {
        "link_records": link_records,
        "usernames": usernames,
        "user_records": user_records,
        "session_usernames": session_usernames,
        "sessions_by_username": sessions_by_username,
        "unreadable": unreadable,
        "unrecognized": unrecognized,
        "scanned": {
            "links": {"keys": len(links_keys), "records": slug_count},
            "users": {"keys": len(users_keys), "records": user_count, "sessions": session_count},
        },
    }


def _finding_sort_key(finding: dict) -> tuple[str, str, str]:
    """Deterministic across runs: by slug, then username, then key. Real KV
    key order is unspecified, so this is what makes two reports diffable."""
    return (finding.get("slug", ""), finding.get("username", ""), finding.get("key", ""))


def analyze(collected: dict, max_findings: int | None = MAX_FINDINGS_PER_CHECK) -> tuple[list[dict], dict]:
    """Pure. Returns (checks, totals). One entry per CHECKS member, always
    all six, in CHECKS order, each
    {"check", "severity", "count", "truncated", "skipped", "findings"}.
    Findings are sorted deterministically and capped at `max_findings` while
    `count` stays the true, untruncated total.

    `max_findings=None` means no cap at all: every finding is returned and
    `truncated` is `False` for every check. The default preserves today's
    report behaviour byte for byte. **The repair path always calls
    `analyze(collected, max_findings=None)`** — consistencyrepair.py needs
    every finding, not just the first `MAX_FINDINGS_PER_CHECK` of each check
    (see docs/plans/consistency-repair.md, rejected alternative #10)."""
    link_records: dict[str, dict] = collected["link_records"]
    usernames: list[str] | None = collected["usernames"]
    user_records: set[str] = collected["user_records"]
    session_usernames: list[str] = collected["session_usernames"]

    findings_by_check: dict[str, list[dict]] = {check_id: [] for check_id, _ in CHECKS}
    skipped: set[str] = set()

    users_index_skipped = usernames is None
    if users_index_skipped:
        skipped.add("unindexed_user")
        skipped.add("missing_user_record")

    # 1. unknown_link_owner — the one surviving owner check. Derived entirely
    # from link_records, with no index involved: a record naming an owner
    # with no user: record is real drift regardless of any index's state.
    for slug, record in link_records.items():
        owner = record["owner"]
        if owner not in user_records:
            findings_by_check["unknown_link_owner"].append({"slug": slug, "owner": owner})

    # 2. unindexed_user
    if not users_index_skipped:
        usernames_set = set(usernames)
        for username in user_records:
            if username not in usernames_set:
                findings_by_check["unindexed_user"].append({"username": username})

    # 3. missing_user_record
    if not users_index_skipped:
        for username in usernames:
            if username not in user_records:
                findings_by_check["missing_user_record"].append({"username": username})

    # 4. orphan_session — grouped by username, the token is never emitted.
    session_counts: dict[str, int] = {}
    for username in session_usernames:
        if username not in user_records:
            session_counts[username] = session_counts.get(username, 0) + 1
    for username, count in session_counts.items():
        findings_by_check["orphan_session"].append({"username": username, "session_count": count})

    # 5. unreadable_value
    for entry in collected["unreadable"]:
        findings_by_check["unreadable_value"].append(dict(entry))

    # 6. unrecognized_key
    for entry in collected["unrecognized"]:
        findings_by_check["unrecognized_key"].append(dict(entry))

    checks: list[dict] = []
    total_findings = 0
    checks_with_findings = 0
    for check_id, severity in CHECKS:
        is_skipped = check_id in skipped
        raw_findings = findings_by_check[check_id]
        raw_findings.sort(key=_finding_sort_key)
        count = len(raw_findings)
        truncated = max_findings is not None and count > max_findings
        checks.append({
            "check": check_id,
            "severity": severity,
            "count": count,
            "truncated": truncated,
            "skipped": is_skipped,
            "findings": [] if is_skipped else (
                raw_findings if max_findings is None else raw_findings[:max_findings]
            ),
        })
        if not is_skipped:
            total_findings += count
            if count > 0:
                checks_with_findings += 1

    totals = {
        "findings": total_findings,
        "checks_with_findings": checks_with_findings,
        "checks_skipped": len(skipped),
    }
    return checks, totals


def build_report(checks: list[dict], totals: dict, scanned: dict, *, generated_at: str, generated_by: str) -> dict:
    """Pure — no I/O, no clock, no store. Assembles the document described in
    docs/plans/kv-consistency-check.md's "The report shape"."""
    return {
        "format": CONSISTENCY_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "generated_by": generated_by,
        "ok": totals["findings"] == 0 and totals["checks_skipped"] == 0,
        "stores_scanned": list(CONSISTENCY_STORES),
        "scanned": scanned,
        "totals": totals,
        "truncated": any(check["truncated"] for check in checks),
        "max_findings_per_check": MAX_FINDINGS_PER_CHECK,
        "repairable_checks": list(REPAIRABLE_CHECKS),
        "checks": checks,
    }


async def handle_consistency(
    stores_by_name: dict[str, object],  # {"links": store, "users": store}
    principal,
    list_keys,
    get_many,
) -> Response:
    if not principal.has_permission("users.manage"):
        return json_response(403, {"error": "forbidden", "required_permission": "users.manage"})

    collected = await collect(stores_by_name, list_keys, get_many)
    checks, totals = analyze(collected)
    return json_response(200, build_report(
        checks, totals, collected["scanned"],
        generated_at=iso_now(), generated_by=principal.username,
    ))
