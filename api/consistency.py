"""Read-only KV consistency check: a two-store walk (links, users) that
reports where the links index, the owner indexes, and the users index have
drifted out of step with the records they're supposed to describe.

Zero WASI SDK imports — `store` objects and the `list_keys` callable arrive
as plain parameters, and `Response`/`json_response`/`iso_now` come from
`responses`, matching the testability rule the rest of `api/` follows (see
`CLAUDE.md`). `api/backup.py` is the model, line for line.

It reports; it never repairs. See docs/plans/kv-consistency-check.md for the
full design, the twelve checks' causes/effects/actions, and the rejected
alternatives.

A `user:` record's VALUE is never read here — only its key name — so this
module can never hold a password_hash. Checks 6-9 need only the key names,
and check 10 needs only the `username` field of a `session:` value.
"""

import json

from responses import Response, iso_now, json_response

MAX_FINDINGS_PER_CHECK = 100

CONSISTENCY_FORMAT = "spin-shortener-consistency-report"
SCHEMA_VERSION = 1

CONSISTENCY_STORES = ("links", "users")

ALL_SLUGS_INDEX_KEY = "all_links"  # == links.ALL_SLUGS_INDEX_KEY
USERNAMES_INDEX_KEY = "_meta:usernames"  # == auth.USERNAMES_INDEX_KEY
BOOTSTRAPPED_KEY = "_meta:bootstrapped"  # == auth.BOOTSTRAPPED_KEY
SLUG_PREFIX = "slug:"
OWNER_LINKS_PREFIX = "owner_links:"  # == backup.OWNER_LINKS_PREFIX
USER_PREFIX = "user:"  # == backup.USER_PREFIX
SESSION_PREFIX = "session:"  # == auth.SESSION_PREFIX

# Ordered. Every check appears in every report, at count 0 when clean.
CHECKS: tuple[tuple[str, str], ...] = (
    ("unindexed_link", "warning"),
    ("missing_link_record", "info"),
    ("unindexed_owner_link", "warning"),
    ("owner_index_mismatch", "warning"),
    ("orphan_owner_index_entry", "info"),
    ("unknown_link_owner", "warning"),
    ("dangling_owner_index", "warning"),
    ("unindexed_user", "warning"),
    ("missing_user_record", "info"),
    ("orphan_session", "warning"),
    ("unreadable_value", "warning"),
    ("unrecognized_key", "info"),
)


def _parse_str_list(raw: bytes | None) -> list[str] | None:
    """None for both an absent key and a malformed value — callers
    distinguish the two by checking `raw is None` themselves."""
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return value


def _parse_link_record(raw: bytes | None) -> dict | None:
    """Only ever extracts `owner`. Never used on a `user:` record."""
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    owner = value.get("owner")
    if not isinstance(owner, str):
        return None
    return {"owner": owner}


def _parse_session_username(raw: bytes | None) -> str | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    username = value.get("username")
    if not isinstance(username, str):
        return None
    return username


async def collect(stores_by_name: dict[str, object], list_keys) -> dict:
    """The only I/O in this module. Returns the raw material `analyze` needs:

        {
          "link_records": {slug: {"owner": str}},   # parsed slug:<slug> records
          "all_links": list[str] | None,            # None only if unreadable
          "owner_index": {username: list[str]},     # readable owner_links: only
          "unreadable_owners": set[str],             # owner_links: that failed
          "usernames": list[str] | None,            # None only if unreadable
          "user_records": set[str],                 # from user: KEY NAMES only
          "session_usernames": list[str],           # from session: values
          "unreadable": [{"store": str, "key": str}],
          "unrecognized": [{"store": str, "key": str}],
          "scanned": {...},
        }

    Never raises on malformed data: every parse failure becomes an entry in
    `unreadable` and the key is excluded from everything else. A diagnostic
    that 500s on a broken store fails exactly when it is needed.

    An absent index key (`all_links`, `_meta:usernames`, or a given
    `owner_links:<U>`) is treated as an empty list, not as unreadable — a
    missing index is real drift the checks below must still report (e.g.
    `unindexed_link` for every record when `all_links` was never written at
    all), whereas a present-but-malformed value can't be trusted for any
    check that depends on it, so those checks are skipped instead.

    A `user:` record's VALUE is never read — only its key name — so this
    function can never hold a password_hash.
    """
    links_store = stores_by_name["links"]
    users_store = stores_by_name["users"]

    link_records: dict[str, dict] = {}
    all_links: list[str] | None = []
    owner_index: dict[str, list[str]] = {}
    unreadable_owners: set[str] = set()
    unreadable: list[dict] = []
    unrecognized: list[dict] = []

    links_keys = await list_keys(links_store)
    slug_count = 0
    owner_index_count = 0
    for key in links_keys:
        if key == ALL_SLUGS_INDEX_KEY:
            raw = await links_store.get(key)
            parsed = _parse_str_list(raw)
            if raw is not None and parsed is None:
                unreadable.append({"store": "links", "key": key})
                all_links = None
            elif parsed is not None:
                all_links = parsed
        elif key.startswith(SLUG_PREFIX):
            slug_count += 1
            raw = await links_store.get(key)
            record = _parse_link_record(raw)
            if raw is not None and record is None:
                unreadable.append({"store": "links", "key": key})
            elif record is not None:
                link_records[key[len(SLUG_PREFIX):]] = record
        elif key.startswith(OWNER_LINKS_PREFIX):
            owner_index_count += 1
            username = key[len(OWNER_LINKS_PREFIX):]
            raw = await links_store.get(key)
            parsed = _parse_str_list(raw)
            if raw is not None and parsed is None:
                unreadable.append({"store": "links", "key": key})
                unreadable_owners.add(username)
            elif parsed is not None:
                owner_index[username] = parsed
        else:
            unrecognized.append({"store": "links", "key": key})

    usernames: list[str] | None = []
    user_records: set[str] = set()
    session_usernames: list[str] = []

    users_keys = await list_keys(users_store)
    user_count = 0
    session_count = 0
    for key in users_keys:
        if key == USERNAMES_INDEX_KEY:
            raw = await users_store.get(key)
            parsed = _parse_str_list(raw)
            if raw is not None and parsed is None:
                unreadable.append({"store": "users", "key": key})
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
            raw = await users_store.get(key)
            username = _parse_session_username(raw)
            if raw is not None and username is None:
                unreadable.append({"store": "users", "key": key})
            elif username is not None:
                session_usernames.append(username)
        else:
            unrecognized.append({"store": "users", "key": key})

    return {
        "link_records": link_records,
        "all_links": all_links,
        "owner_index": owner_index,
        "unreadable_owners": unreadable_owners,
        "usernames": usernames,
        "user_records": user_records,
        "session_usernames": session_usernames,
        "unreadable": unreadable,
        "unrecognized": unrecognized,
        "scanned": {
            "links": {"keys": len(links_keys), "records": slug_count, "owner_indexes": owner_index_count},
            "users": {"keys": len(users_keys), "records": user_count, "sessions": session_count},
        },
    }


def _finding_sort_key(finding: dict) -> tuple[str, str, str]:
    """Deterministic across runs: by slug, then username, then key. Real KV
    key order is unspecified, so this is what makes two reports diffable."""
    return (finding.get("slug", ""), finding.get("username", ""), finding.get("key", ""))


def analyze(collected: dict) -> tuple[list[dict], dict]:
    """Pure. Returns (checks, totals). One entry per CHECKS member, always
    all twelve, in CHECKS order, each
    {"check", "severity", "count", "truncated", "skipped", "findings"}.
    Findings are sorted deterministically and capped at MAX_FINDINGS_PER_CHECK
    while `count` stays the true, untruncated total."""
    link_records: dict[str, dict] = collected["link_records"]
    all_links: list[str] | None = collected["all_links"]
    owner_index: dict[str, list[str]] = collected["owner_index"]
    unreadable_owners: set[str] = collected["unreadable_owners"]
    usernames: list[str] | None = collected["usernames"]
    user_records: set[str] = collected["user_records"]
    session_usernames: list[str] = collected["session_usernames"]

    findings_by_check: dict[str, list[dict]] = {check_id: [] for check_id, _ in CHECKS}
    skipped: set[str] = set()

    links_index_skipped = all_links is None
    if links_index_skipped:
        skipped.add("unindexed_link")
        skipped.add("missing_link_record")

    users_index_skipped = usernames is None
    if users_index_skipped:
        skipped.add("unindexed_user")
        skipped.add("missing_user_record")

    # 1. unindexed_link
    if not links_index_skipped:
        all_links_set = set(all_links)
        for slug, record in link_records.items():
            if slug not in all_links_set:
                findings_by_check["unindexed_link"].append({"slug": slug, "owner": record["owner"]})

    # 2. missing_link_record
    if not links_index_skipped:
        for slug in all_links:
            if slug not in link_records:
                findings_by_check["missing_link_record"].append({"slug": slug})

    # 3. unindexed_owner_link — excludes records whose owner has no user:
    # record (that's check 6) and any owner whose own index was unreadable.
    for slug, record in link_records.items():
        owner = record["owner"]
        if owner in unreadable_owners or owner not in user_records:
            continue
        if slug not in owner_index.get(owner, []):
            findings_by_check["unindexed_owner_link"].append({"slug": slug, "owner": owner})

    # 4. owner_index_mismatch, 5. orphan_owner_index_entry
    for owner, slugs in owner_index.items():
        for slug in slugs:
            record = link_records.get(slug)
            if record is None:
                findings_by_check["orphan_owner_index_entry"].append({"slug": slug, "indexed_under": owner})
            elif record["owner"] != owner:
                findings_by_check["owner_index_mismatch"].append(
                    {"slug": slug, "indexed_under": owner, "record_owner": record["owner"]}
                )

    # 6. unknown_link_owner
    for slug, record in link_records.items():
        owner = record["owner"]
        if owner not in user_records:
            findings_by_check["unknown_link_owner"].append({"slug": slug, "owner": owner})

    # 7. dangling_owner_index — never an empty index key, by design.
    for owner, slugs in owner_index.items():
        if owner not in user_records and slugs:
            findings_by_check["dangling_owner_index"].append({"username": owner, "slug_count": len(slugs)})

    # 8. unindexed_user
    if not users_index_skipped:
        usernames_set = set(usernames)
        for username in user_records:
            if username not in usernames_set:
                findings_by_check["unindexed_user"].append({"username": username})

    # 9. missing_user_record
    if not users_index_skipped:
        for username in usernames:
            if username not in user_records:
                findings_by_check["missing_user_record"].append({"username": username})

    # 10. orphan_session — grouped by username, the token is never emitted.
    session_counts: dict[str, int] = {}
    for username in session_usernames:
        if username not in user_records:
            session_counts[username] = session_counts.get(username, 0) + 1
    for username, count in session_counts.items():
        findings_by_check["orphan_session"].append({"username": username, "session_count": count})

    # 11. unreadable_value
    for entry in collected["unreadable"]:
        findings_by_check["unreadable_value"].append(dict(entry))

    # 12. unrecognized_key
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
        truncated = count > MAX_FINDINGS_PER_CHECK
        checks.append({
            "check": check_id,
            "severity": severity,
            "count": count,
            "truncated": truncated,
            "skipped": is_skipped,
            "findings": [] if is_skipped else raw_findings[:MAX_FINDINGS_PER_CHECK],
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
        "checks": checks,
    }


async def handle_consistency(
    stores_by_name: dict[str, object],  # {"links": store, "users": store}
    principal,
    list_keys,
) -> Response:
    if not principal.has_permission("users.manage"):
        return json_response(403, {"error": "forbidden", "required_permission": "users.manage"})

    collected = await collect(stores_by_name, list_keys)
    checks, totals = analyze(collected)
    return json_response(200, build_report(
        checks, totals, collected["scanned"],
        generated_at=iso_now(), generated_by=principal.username,
    ))
