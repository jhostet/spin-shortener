"""Report and purge analytics keys left behind by a deleted link.

`links.handle_delete` and `bulk.handle_bulk_action`'s delete branch remove a
`slug:<slug>` record and rewrite the indexes, but neither one touches the
`analytics` namespace — a deliberate decision (see
`docs/plans/kv-consistency-check.md`'s rejected alternative #1) that turned
out to be expensive rather than merely untidy: every physical key in the
store costs ~68.7 us to enumerate, `GET /api/analytics/click-totals` makes
one such enumeration on every dashboard load, and a deployment that creates
and deletes links heavily accumulates orphaned analytics keys without bound.
See `docs/plans/analytics-orphan-purge.md` for the full argument and the
measurements behind it.

Zero `spin_sdk` imports — `store` views, `request` and the `list_keys`
callable arrive as plain parameters, matching `consistency.py` and
`backup.py`. Dependency direction is `analyticsorphans -> analytics ->
links`, with no cycle.

**No new KV key type.** The purge is stateless: it deletes only keys it just
enumerated, and returns what it did. It never constructs a key (e.g.
`count:<slug>:<n>` for `n in range(COUNT_SHARDS)`) — that is what lets it pick
up the legacy unsharded `count:<slug>` key, keys left by a since-lowered
`analytics_event_slots`, and any future raise of `CountShards`, all for free.

**DELETES ARE ALWAYS SEQUENTIAL, NEVER GATHERED — the one rule a
well-meaning optimisation is most likely to break.** `for key in
keys_to_delete: await analytics_store.delete(key)`, exactly like
`backup.handle_restore`'s write loop and every bulk handler. Deletes are
writes; writes are cap-bound at 50/second app-wide while reads have
1,000/second of headroom (see `kvbatch.gather_reads`'s docstring and
CLAUDE.md's "Parallel KV reads"), so gathering them would queue against the
cap rather than overlap, and risks throttling live click recording that
shares the same budget. `gather_reads` is used only for the purge's liveness
pre-check (`links_store.exists(...)`, a read) and for the report's
enumeration — never for a delete.

**The purge cannot be misled by index drift the way the report can be.** The
report derives liveness from `links.all_slugs` (cheap, O(1) KV operations);
the purge instead re-checks `exists("slug:<S>")` on the record itself,
immediately before deciding what to delete for that slug. That re-check is
load-bearing, not defensive dressing: it is what makes it safe to skip a
typed GUI confirmation field (see the plan's "Decisions taken" section) —
a stale report can misinform the operator, but it cannot make the purge
delete a live link's analytics, because the purge never trusts the report's
liveness judgement.
"""

import json

import links
from analytics import parse_analytics_key
from kvbatch import gather_reads
from responses import Response, iso_now, json_response

MAX_PURGE_SLUGS = 50              # == bulk.MAX_BULK_ROWS, same reasoning
MAX_PURGE_KEYS_PER_REQUEST = 250  # the write budget; see docs/plans/analytics-orphan-purge.md
MAX_ORPHAN_SLUGS_REPORTED = 100   # == consistency.MAX_FINDINGS_PER_CHECK
MAX_UNRECOGNIZED_SAMPLE = 20
PURGE_CONFIRMATION = "PURGE"
ORPHANS_FORMAT = "spin-shortener-analytics-orphans"
SCHEMA_VERSION = 1


# --- Pure functions -------------------------------------------------------


def classify_analytics_keys(keys: list[str]) -> tuple[dict[str, dict], list[str]]:
    """({slug: {"keys": [...], "count_keys": n, "event_keys": n}}, unrecognized).

    A key whose shape `analytics.parse_analytics_key` does not recognise, OR
    whose slug fails `links.is_valid_custom_slug`, goes to `unrecognized` and
    is therefore never purgeable. That is deliberate and it is the safety
    valve: a future analytics key type must show up as something a human is
    told about, never as something this feature quietly deletes.
    """
    by_slug: dict[str, dict] = {}
    unrecognized: list[str] = []

    for key in keys:
        parsed = parse_analytics_key(key)
        if parsed is None:
            unrecognized.append(key)
            continue
        kind, slug = parsed
        if not links.is_valid_custom_slug(slug):
            unrecognized.append(key)
            continue

        entry = by_slug.setdefault(slug, {"keys": [], "count_keys": 0, "event_keys": 0})
        entry["keys"].append(key)
        if kind == "count":
            entry["count_keys"] += 1
        else:
            entry["event_keys"] += 1

    return by_slug, unrecognized


def split_by_liveness(by_slug: dict, live_slugs: set[str]) -> tuple[dict, dict]:
    """(orphans, live), partitioned on membership in live_slugs."""
    orphans = {slug: v for slug, v in by_slug.items() if slug not in live_slugs}
    live = {slug: v for slug, v in by_slug.items() if slug in live_slugs}
    return orphans, live


def build_orphan_report(
    orphans: dict[str, dict],
    live: dict[str, dict],
    unrecognized: list[str],
    *,
    analytics_key_count: int,
    live_slug_count: int,
    generated_at: str,
    generated_by: str,
) -> dict:
    """`totals` are always exact even when `orphans` is truncated — the same
    `MAX_FINDINGS_PER_CHECK` rule `consistency.py` follows, for the same
    reason: a capped list must never read as complete.
    """
    orphan_keys_total = sum(len(v["keys"]) for v in orphans.values())
    live_keys_total = sum(len(v["keys"]) for v in live.values())

    ordered = sorted(orphans.items(), key=lambda item: (-len(item[1]["keys"]), item[0]))
    truncated = len(ordered) > MAX_ORPHAN_SLUGS_REPORTED
    shown = ordered[:MAX_ORPHAN_SLUGS_REPORTED]

    orphans_out = [
        {
            "slug": slug,
            "keys": len(v["keys"]),
            "count_keys": v["count_keys"],
            "event_keys": v["event_keys"],
        }
        for slug, v in shown
    ]

    return {
        "format": ORPHANS_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "generated_by": generated_by,
        "scanned": {"analytics_keys": analytics_key_count, "live_slugs": live_slug_count},
        "totals": {
            "orphan_slugs": len(orphans),
            "orphan_keys": orphan_keys_total,
            "live_keys": live_keys_total,
            "unrecognized_keys": len(unrecognized),
        },
        "truncated": truncated,
        "max_orphan_slugs": MAX_ORPHAN_SLUGS_REPORTED,
        "orphans": orphans_out,
        "unrecognized_sample": sorted(unrecognized)[:MAX_UNRECOGNIZED_SAMPLE],
    }


def plan_purge(
    orphans: dict[str, dict], slugs: list[str], budget: int
) -> tuple[list[str], list[str], list[str]]:
    """(slugs_to_purge, keys_to_delete, remaining_slugs).

    Whole slugs only, biggest first (sort key: (-key_count, slug)) so a
    bounded budget reclaims the most keys it can and two runs over the same
    input produce byte-identical plans.

    INVARIANT: at least one slug is always planned, even if its own key count
    exceeds `budget` alone. Without that, a store holding a slug with more
    keys than the budget (possible if `analytics_event_slots` was once set
    very high) would make every request purge nothing and the GUI's loop
    would never terminate.
    """
    ordered = sorted(slugs, key=lambda s: (-len(orphans[s]["keys"]), s))

    slugs_to_purge: list[str] = []
    keys_to_delete: list[str] = []
    remaining_slugs: list[str] = []
    total = 0

    for index, slug in enumerate(ordered):
        slug_keys = orphans[slug]["keys"]
        if slugs_to_purge and total + len(slug_keys) > budget:
            remaining_slugs = ordered[index:]
            break
        slugs_to_purge.append(slug)
        keys_to_delete.extend(slug_keys)
        total += len(slug_keys)

    return slugs_to_purge, keys_to_delete, remaining_slugs


def _parse_live_slugs(raw: bytes | None) -> list[str] | None:
    """None only for a present-but-malformed value — an absent key means no
    links have ever been created, which is valid state (empty list), not a
    failure. Failing closed on a malformed value matters: an unreadable index
    would otherwise make every link look deleted, and a value that parses but
    isn't actually a list of strings (e.g. a JSON object) would silently
    report every one of its members as "live" if this didn't check the type.
    """
    if raw is None:
        return []
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return value


# --- Handlers ---------------------------------------------------------------


async def handle_orphan_report(links_store, analytics_store, principal, list_keys) -> Response:
    """GET /api/admin/analytics/orphans. Exactly 2 KV operations regardless
    of how many orphans exist: one `list_keys` and one `get`. That is what
    makes the report cheap enough to offer as a plain button.
    """
    if not principal.has_permission("users.manage"):
        return json_response(403, {"error": "forbidden", "required_permission": "users.manage"})

    keys = await list_keys(analytics_store)
    by_slug, unrecognized = classify_analytics_keys(keys)

    raw_live = await links_store.get(links.ALL_SLUGS_INDEX_KEY)
    live_slugs = _parse_live_slugs(raw_live)
    if live_slugs is None:
        return json_response(409, {"error": "links_index_unreadable", "next_step": "consistency_check"})
    live_slugs_set = set(live_slugs)

    orphans, live = split_by_liveness(by_slug, live_slugs_set)
    report = build_orphan_report(
        orphans,
        live,
        unrecognized,
        analytics_key_count=len(keys),
        live_slug_count=len(live_slugs_set),
        generated_at=iso_now(),
        generated_by=principal.username,
    )
    return json_response(200, report)


async def handle_orphan_purge(links_store, analytics_store, principal, request, list_keys) -> Response:
    """POST /api/admin/analytics/purge. All input validation is all-or-nothing
    (nothing is written if the request itself is malformed); per-slug outcomes
    after that are reported and skipped rather than failing the whole batch —
    see the plan's Trade-offs #5 for why a fatal per-slug error would stall
    the GUI's chunked loop permanently.
    """
    if not principal.has_permission("users.manage"):
        return json_response(403, {"error": "forbidden", "required_permission": "users.manage"})

    try:
        payload = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return json_response(400, {"error": "invalid_json"})

    if not isinstance(payload, dict) or payload.get("confirm") != PURGE_CONFIRMATION:
        return json_response(400, {"error": "confirmation_required", "expected": PURGE_CONFIRMATION})

    slugs = payload.get("slugs")
    if not isinstance(slugs, list) or not slugs or not all(isinstance(s, str) for s in slugs):
        return json_response(400, {"error": "no_slugs"})

    if len(slugs) != len(set(slugs)):
        return json_response(400, {"error": "duplicate_slug"})

    if len(slugs) > MAX_PURGE_SLUGS:
        return json_response(
            400, {"error": "too_many_slugs", "max_slugs": MAX_PURGE_SLUGS, "slug_count": len(slugs)}
        )

    for slug in slugs:
        if not links.is_valid_custom_slug(slug):
            return json_response(400, {"error": "invalid_slug", "slug": slug})

    # 1. Liveness, verified against the record and nothing else — the
    # re-check that makes the whole lower-confirmation-bar design safe.
    exists_flags = await gather_reads(links_store.exists(f"slug:{s}") for s in slugs)
    live_set = {s for s, exists in zip(slugs, exists_flags) if exists}
    non_live_slugs = [s for s in slugs if s not in live_set]

    # 2. One enumeration, restricted to the non-live submitted slugs.
    keys = await list_keys(analytics_store)
    by_slug, _unrecognized = classify_analytics_keys(keys)

    purgeable_slugs = [s for s in non_live_slugs if s in by_slug]
    no_analytics_slugs = {s for s in non_live_slugs if s not in by_slug}
    orphans = {s: by_slug[s] for s in purgeable_slugs}

    # 3. Bounded plan.
    if purgeable_slugs:
        slugs_to_purge, keys_to_delete, remaining_slugs = plan_purge(
            orphans, purgeable_slugs, MAX_PURGE_KEYS_PER_REQUEST
        )
    else:
        slugs_to_purge, keys_to_delete, remaining_slugs = [], [], []

    # 4. Delete sequentially. NEVER gather_reads/asyncio.gather here — see
    # the module docstring.
    for key in keys_to_delete:
        await analytics_store.delete(key)

    skipped = []
    for s in slugs:
        if s in live_set:
            skipped.append({"slug": s, "reason": "link_exists"})
        elif s in no_analytics_slugs:
            skipped.append({"slug": s, "reason": "no_analytics_keys"})

    return json_response(200, {
        "ok": True,
        "purged_slugs": slugs_to_purge,
        "deleted_keys": len(keys_to_delete),
        "remaining_slugs": remaining_slugs,
        "skipped": skipped,
        "complete": not remaining_slugs,
        "max_keys_per_request": MAX_PURGE_KEYS_PER_REQUEST,
    })
