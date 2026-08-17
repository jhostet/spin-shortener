"""Repair companion to the read-only `GET /api/admin/consistency` check
(`api/consistency.py`). Eight of the twelve checks have exactly one safe,
derivable, automatic repair; four never do because repairing them requires a
judgment call the store can't answer. See docs/plans/consistency-repair.md
for the full design, the per-check verdict, and the rejected alternatives.

Zero WASI SDK imports — `store` views, `request`, `list_keys` and
`get_many` arrive as plain parameters, matching `consistency.py` and
`analyticsorphans.py`. Dependency direction is `consistencyrepair ->
consistency -> responses`, no cycle.

**Re-detect before repairing; never trust a submitted report.** The client
submits check ids only, never findings — `handle_repair` always re-runs
`consistency.collect`/`consistency.analyze` in-request. That dissolves the
report's `MAX_FINDINGS_PER_CHECK` truncation too: the repair calls `analyze`
with `max_findings=None` and so acts on every finding, not just the first 100
of a check the operator happened to be looking at.

**The single most important correctness property: `present_slugs` guards
every removal repair.** A `slug:` record that exists but fails to parse is
reported as `missing_link_record`/`orphan_owner_index_entry` (see
`consistency.collect`'s docstring) even though its key is still physically
present. A repair that stripped such a slug from an index would delete the
only reference to a record that still exists — the link becomes invisible to
the dashboard and the next backup restore prunes it entirely. That is data
loss caused by a repair tool, which is the worst thing this feature could do.
So `plan_repairs` requires a removal target's slug to be ABSENT from
`present_slugs`, never merely absent from `link_records` (which a corrupt
record is also excluded from).

**Repairs are writes: sequential and chunked only, exactly like
`POST /api/admin/analytics/purge`.** `apply_repairs` never fans out a write
concurrently — `api/kvbatch.py`'s concurrency helper is reads-only by
design, and the WASI key-value batch interface's multi-key write functions
are already rejected repo-wide (`TASKS.md`, "Considered and rejected",
2026-08-15) on the grounds that the WIT itself disclaims atomicity and
ordering while every write path in this app depends on a stated ordering.
Writes are cap-bound at
50/second app-wide while reads have 1,000/second of headroom (CLAUDE.md,
"Parallel KV reads"), so issuing them concurrently would queue against the
cap rather than overlap, and would compete with live click recording.

**No new KV key type.** The repair only mutates keys that already exist and
are already understood by `backup.py`'s `INDEX_KEYS`, `consistency.py`'s
key-shape recognition, and `kvprefix.STORE_PREFIXES`: `all_links`,
`owner_links:<U>`, `_meta:usernames`, `session:<token>`. It is stateless — it
records nothing about past repairs, exactly like the analytics purge.

**The lost-update window.** Spin's KV has no compare-and-swap and the
consistency walk has no snapshot, so a concurrent write between `collect` and
`apply_repairs` can produce a lost update. Three things bound it: (1) every
delta write re-reads the index immediately before writing it, so the window
is one `get` + one `set` rather than the whole collect-to-write span; (2) a
repair never writes a wholesale computed list, only adds/removes the specific
members `collect` formed an opinion about, so a concurrently-created slug
survives untouched; (3) the only reachable residual failure mode is a lost
*removal*, which surfaces as ordinary drift on the next consistency run and
is fixed by re-running. The worst case is "run it again".
"""

import json

import consistency
from responses import Response, iso_now, json_response

REPAIR_CONFIRMATION = "REPAIR"
REPAIR_FORMAT = "spin-shortener-consistency-repair"
SCHEMA_VERSION = 1
MAX_REPAIR_WRITES = 100  # ~75 ms/write measured on Akamai (TASKS.md,
# 2026-08-15) x 100 =~ 7.5s, well under the 30s handler limit. Deliberately
# well below the analytics purge's 250 — the measured write cost tripled
# after that constant was chosen. Raising this needs real timing evidence
# from a full-cap repair, not a hunch — the same rule MAX_BULK_ROWS and
# MAX_PURGE_KEYS_PER_REQUEST carry.
MAX_BLOCKED_DETAIL = 20  # per blocked entry's sample of at-risk slugs

ALL_LINKS_KEY = consistency.ALL_SLUGS_INDEX_KEY
USERNAMES_KEY = consistency.USERNAMES_INDEX_KEY
OWNER_LINKS_PREFIX = consistency.OWNER_LINKS_PREFIX


# --- Pure functions ---------------------------------------------------------


def apply_list_delta(current: list[str], add: list[str], remove: list[str]) -> list[str]:
    """Removals first, then order-preserving appends of anything not already
    present — byte-for-byte the shape `links.add_slugs_to_indexes`/
    `remove_slugs_from_indexes` use, so a repaired index is indistinguishable
    from one the normal authoring path would have written."""
    remove_set = set(remove)
    result = [item for item in current if item not in remove_set]
    for item in add:
        if item not in result:
            result.append(item)
    return result


class _Budget:
    """Tiny mutable counter threaded through `plan_repairs`'s helpers so they
    can share one running total without every helper returning it back."""

    def __init__(self, limit: int):
        self.limit = limit
        self.spent = 0

    def has_room(self) -> bool:
        return self.spent < self.limit

    def spend(self) -> None:
        self.spent += 1


def plan_repairs(collected: dict, checks: list[dict], requested: list[str], budget: int) -> dict:
    """Pure. `checks` must be `consistency.analyze(collected,
    max_findings=None)`'s first return value — a partial (truncated) list
    would silently under-repair (see the module docstring).

    Every repair unit costs exactly one write (per distinct target key, not
    per finding — many findings for one check share a single key), so any
    budget >= 1 makes progress. Unlike `analyticsorphans.plan_purge`, this
    function needs no "at least one slug is always planned" special case.
    """
    checks_by_id = {c["check"]: c for c in checks}
    requested_set = set(requested)
    present_slugs: set[str] = collected["present_slugs"]

    links_deltas: dict[str, dict[str, list[str]]] = {}
    links_deletes: list[str] = []
    users_deltas: dict[str, dict[str, list[str]]] = {}
    users_deletes: list[str] = []

    touched_links: set[str] = set()
    touched_users: set[str] = set()

    budget_counter = _Budget(budget)
    check_reports: list[dict] = []
    blocked_entries: list[dict] = []

    def _delta_finding(deltas: dict, touched: set[str], key: str, kind: str, value: str) -> str:
        """Schedule `value` into `deltas[key][kind]`. Returns "planned" if the
        finding is (now or already) accounted for within budget, "remaining"
        if the budget was exhausted before this key could be touched."""
        if key in touched:
            deltas[key][kind].append(value)
            return "planned"
        if not budget_counter.has_room():
            return "remaining"
        touched.add(key)
        deltas.setdefault(key, {"add": [], "remove": []})
        deltas[key][kind].append(value)
        budget_counter.spend()
        return "planned"

    def _delete_finding(deletes: list[str], touched: set[str], key: str) -> str:
        if key in touched:
            return "planned"
        if not budget_counter.has_room():
            return "remaining"
        touched.add(key)
        deletes.append(key)
        budget_counter.spend()
        return "planned"

    # --- Precompute unindexed_link's planned adds, needed for the dangling
    # precondition's post-state check (rule 3) BEFORE dangling is planned. ---
    unindexed_link_check = checks_by_id.get("unindexed_link")
    unindexed_link_add_slugs: set[str] = set()
    if (
        "unindexed_link" in requested_set
        and unindexed_link_check is not None
        and not unindexed_link_check["skipped"]
    ):
        for finding in unindexed_link_check["findings"]:
            unindexed_link_add_slugs.add(finding["slug"])

    all_links_unreadable = collected["all_links"] is None
    all_links_post_state = set(collected["all_links"] or []) | unindexed_link_add_slugs

    # === Phase A: dangling_owner_index, always planned first (its deletion
    # can target the very key #5 (orphan_owner_index_entry) would otherwise
    # write a delta to). ===
    if "dangling_owner_index" in requested_set:
        check = checks_by_id["dangling_owner_index"]
        planned = remaining = blocked = 0
        if check["skipped"]:
            check_reports.append(_skipped_report("dangling_owner_index"))
        else:
            for finding in check["findings"]:
                username = finding["username"]
                key = f"{OWNER_LINKS_PREFIX}{username}"
                owner_slugs = collected["owner_index"].get(username, [])

                if all_links_unreadable:
                    blocked_entries.append({
                        "check": "dangling_owner_index", "username": username,
                        "reason": "links_index_unreadable", "next_step": None,
                        "slug_count": finding["slug_count"],
                    })
                    blocked += 1
                    continue

                at_risk = sorted(
                    slug for slug in owner_slugs
                    if slug in present_slugs and slug not in all_links_post_state
                )
                if at_risk:
                    blocked_entries.append({
                        "check": "dangling_owner_index", "username": username,
                        "reason": "would_orphan_unindexed_link", "next_step": "unindexed_link",
                        "slug_count": len(at_risk), "slugs": at_risk[:MAX_BLOCKED_DETAIL],
                    })
                    blocked += 1
                    continue

                outcome = _delete_finding(links_deletes, touched_links, key)
                if outcome == "planned":
                    planned += 1
                else:
                    remaining += 1
            check_reports.append({
                "check": "dangling_owner_index", "findings": check["count"],
                "planned": planned, "remaining": remaining, "blocked": blocked,
                "skipped": False, "skip_reason": None,
            })

    # === Main pass: the remaining seven checks, in REPAIRABLE_CHECKS order. ===
    for check_id in consistency.REPAIRABLE_CHECKS:
        if check_id == "dangling_owner_index" or check_id not in requested_set:
            continue
        check = checks_by_id[check_id]
        if check["skipped"]:
            check_reports.append(_skipped_report(check_id))
            continue

        planned = remaining = blocked = 0

        if check_id == "unindexed_link":
            for finding in check["findings"]:
                outcome = _delta_finding(links_deltas, touched_links, ALL_LINKS_KEY, "add", finding["slug"])
                planned += outcome == "planned"
                remaining += outcome == "remaining"

        elif check_id == "missing_link_record":
            for finding in check["findings"]:
                slug = finding["slug"]
                if slug in present_slugs:
                    blocked_entries.append({
                        "check": check_id, "slug": slug,
                        "reason": "record_unreadable", "next_step": "unreadable_value",
                    })
                    blocked += 1
                    continue
                outcome = _delta_finding(links_deltas, touched_links, ALL_LINKS_KEY, "remove", slug)
                planned += outcome == "planned"
                remaining += outcome == "remaining"

        elif check_id == "unindexed_owner_link":
            for finding in check["findings"]:
                owner = finding["owner"]
                key = f"{OWNER_LINKS_PREFIX}{owner}"
                outcome = _delta_finding(links_deltas, touched_links, key, "add", finding["slug"])
                planned += outcome == "planned"
                remaining += outcome == "remaining"

        elif check_id == "orphan_owner_index_entry":
            for finding in check["findings"]:
                slug = finding["slug"]
                owner = finding["indexed_under"]
                key = f"{OWNER_LINKS_PREFIX}{owner}"
                if slug in present_slugs:
                    blocked_entries.append({
                        "check": check_id, "slug": slug, "username": owner,
                        "reason": "record_unreadable", "next_step": "unreadable_value",
                    })
                    blocked += 1
                    continue
                if key in links_deletes:
                    # The owner's whole index key is being deleted (phase A) —
                    # the removal is already accomplished, at no extra cost.
                    planned += 1
                    continue
                outcome = _delta_finding(links_deltas, touched_links, key, "remove", slug)
                planned += outcome == "planned"
                remaining += outcome == "remaining"

        elif check_id == "unindexed_user":
            for finding in check["findings"]:
                username = finding["username"]
                if not username.strip():
                    blocked_entries.append({
                        "check": check_id, "username": username,
                        "reason": "invalid_username", "next_step": None,
                    })
                    blocked += 1
                    continue
                outcome = _delta_finding(users_deltas, touched_users, USERNAMES_KEY, "add", username)
                planned += outcome == "planned"
                remaining += outcome == "remaining"

        elif check_id == "missing_user_record":
            for finding in check["findings"]:
                outcome = _delta_finding(users_deltas, touched_users, USERNAMES_KEY, "remove", finding["username"])
                planned += outcome == "planned"
                remaining += outcome == "remaining"

        elif check_id == "orphan_session":
            for finding in check["findings"]:
                username = finding["username"]
                for session_key in collected["sessions_by_username"].get(username, []):
                    outcome = _delete_finding(users_deletes, touched_users, session_key)
                    planned += outcome == "planned"
                    remaining += outcome == "remaining"

        check_reports.append({
            "check": check_id, "findings": check["count"],
            "planned": planned, "remaining": remaining, "blocked": blocked,
            "skipped": False, "skip_reason": None,
        })

    # Re-order check_reports to REPAIRABLE_CHECKS order (dangling_owner_index
    # was appended out of turn, in phase A).
    order = {check_id: i for i, check_id in enumerate(consistency.REPAIRABLE_CHECKS)}
    check_reports.sort(key=lambda c: order[c["check"]])

    return {
        "links": {"deltas": links_deltas, "deletes": links_deletes},
        "users": {"deltas": users_deltas, "deletes": users_deletes},
        "checks": check_reports,
        "blocked": blocked_entries,
        "planned_writes": budget_counter.spent,
    }


def _skipped_report(check_id: str) -> dict:
    return {
        "check": check_id, "findings": 0, "planned": 0, "remaining": 0, "blocked": 0,
        "skipped": True, "skip_reason": "index_unreadable",
    }


# --- The applier -------------------------------------------------------------


async def apply_repairs(stores_by_name: dict[str, object], plan: dict) -> dict:
    """Sequential, never concurrent — see the module docstring. Returns
    {"keys_written": n, "keys_deleted": n, "write_skipped": [...]}.

    Stores in ("links", "users") order: links first, users last, so a
    mid-request failure leaves the operator's own session material untouched
    for a retry — the same rule `backup.RESTORE_STORE_ORDER` states.

    No `user:` key is ever read, written or deleted here — only
    `_meta:usernames` and `session:<token>`.
    """
    keys_written = 0
    keys_deleted = 0
    write_skipped: list[dict] = []

    for store_name in ("links", "users"):
        store = stores_by_name[store_name]
        store_plan = plan[store_name]

        for key, delta in store_plan["deltas"].items():
            raw = await store.get(key)
            parsed = consistency.parse_str_list(raw)
            if raw is not None and parsed is None:
                write_skipped.append({"store": store_name, "key": key, "reason": "index_unreadable_at_write"})
                continue
            new = apply_list_delta(parsed or [], delta["add"], delta["remove"])
            if new == (parsed or []):
                continue  # idempotent: a second pass over the same input writes nothing
            await store.set(key, json.dumps(new).encode("utf-8"))
            keys_written += 1

        # Deletes are always sequential, one `await store.delete(key)` at a
        # time — see the module docstring's "Repairs are writes" section.
        for key in store_plan["deletes"]:
            await store.delete(key)
            keys_deleted += 1

    return {"keys_written": keys_written, "keys_deleted": keys_deleted, "write_skipped": write_skipped}


# --- The handler --------------------------------------------------------------


async def handle_repair(stores_by_name: dict[str, object], principal, request, list_keys, get_many) -> Response:
    """POST /api/admin/consistency/repair. Gated on `users.manage`, matching
    the read-only check, backup and the analytics purge.

    The client submits check ids only, never findings — every fact this
    handler acts on is re-derived inside this request, in this function,
    from a fresh `consistency.collect`/`consistency.analyze` call.
    """
    if not principal.has_permission("users.manage"):
        return json_response(403, {"error": "forbidden", "required_permission": "users.manage"})

    try:
        payload = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return json_response(400, {"error": "invalid_json"})

    if not isinstance(payload, dict) or payload.get("confirm") != REPAIR_CONFIRMATION:
        return json_response(400, {"error": "confirmation_required", "expected": REPAIR_CONFIRMATION})

    requested = payload.get("checks")
    if not isinstance(requested, list) or not requested or not all(isinstance(c, str) for c in requested):
        return json_response(400, {"error": "no_checks", "repairable_checks": list(consistency.REPAIRABLE_CHECKS)})

    seen: set[str] = set()
    for check_id in requested:
        if check_id in seen:
            return json_response(400, {"error": "duplicate_check", "check": check_id})
        seen.add(check_id)

    all_check_ids = {check_id for check_id, _ in consistency.CHECKS}
    repairable_set = set(consistency.REPAIRABLE_CHECKS)
    for check_id in requested:
        if check_id not in all_check_ids:
            return json_response(
                400, {"error": "unknown_check", "check": check_id, "repairable_checks": list(consistency.REPAIRABLE_CHECKS)}
            )
        if check_id not in repairable_set:
            return json_response(
                400,
                {"error": "check_not_repairable", "check": check_id, "repairable_checks": list(consistency.REPAIRABLE_CHECKS)},
            )

    collected = await consistency.collect(stores_by_name, list_keys, get_many)
    checks, _totals = consistency.analyze(collected, max_findings=None)
    plan = plan_repairs(collected, checks, requested, MAX_REPAIR_WRITES)
    applied = await apply_repairs(stores_by_name, plan)

    response_checks = [
        {
            "check": c["check"],
            "findings": c["findings"],
            "repaired": c["planned"],
            "remaining": c["remaining"],
            "blocked": c["blocked"],
            "skipped": c["skipped"],
            "skip_reason": c["skip_reason"],
        }
        for c in plan["checks"]
    ]
    complete = all(c["remaining"] == 0 for c in plan["checks"])

    return json_response(200, {
        "ok": True,
        "format": REPAIR_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "repaired_at": iso_now(),
        "repaired_by": principal.username,
        "checks": response_checks,
        "keys_written": applied["keys_written"],
        "keys_deleted": applied["keys_deleted"],
        "writes": applied["keys_written"] + applied["keys_deleted"],
        "blocked": plan["blocked"],
        "write_skipped": applied["write_skipped"],
        "complete": complete,
        "max_writes_per_request": MAX_REPAIR_WRITES,
    })
