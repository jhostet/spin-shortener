"""GET /api/links/{slug}/analytics — reads the count/events data redirect
writes into the `analytics` KV store on every successful click.
"""

import json
from datetime import datetime, timezone

import links
from auth import Principal
from links import can_view, get_link
from responses import json_response, to_iso8601_utc_ms


# MUST stay equal to redirect/linkgate/keys.go's CountShards — see that file
# for the full rule. Lowering this silently drops every click that was
# recorded into a higher shard. api/tests/test_kvprefix.py pins the equality.
COUNT_SHARDS = 64


def _merge_counts(blobs) -> tuple[int, dict[str, int]]:
    """Sum shard blobs into one {total, days}.

    A blob that is absent, empty, not JSON, or not an object contributes
    nothing rather than raising — one corrupt shard must never blank out a
    link's whole history.

    The merged ``days`` map can exceed ``analytics_day_retention_days``: each
    shard trims its own map independently, so a low-traffic link whose shards
    collected clicks on different days unions to more than the window. That is
    accepted — the data is correct and small, and trimming here would mean
    declaring the retention variable for the `api` component purely to shorten
    a response that is at most a few kilobytes.
    """
    total = 0
    days: dict[str, int] = {}

    for raw in blobs:
        if not raw:
            continue
        try:
            blob = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(blob, dict):
            continue

        shard_total = blob.get("total")
        if isinstance(shard_total, int) and not isinstance(shard_total, bool):
            total += shard_total

        shard_days = blob.get("days")
        if isinstance(shard_days, dict):
            for day, count in shard_days.items():
                if isinstance(count, int) and not isinstance(count, bool):
                    days[day] = days.get(day, 0) + count

    return total, days


def parse_analytics_key(key: str) -> tuple[str, str] | None:
    """("count"|"event", slug) for a recognized analytics key, else None.

    Shape only — it does not judge whether `slug` is a *valid* slug, because
    handle_click_totals intersects against a known-visible set and must keep
    its current behaviour byte for byte. analyticsorphans.py applies
    links.is_valid_custom_slug on top before anything is deleted.
    """
    if key.startswith("count:"):
        kind, rest = "count", key[len("count:"):]
    elif key.startswith("events:"):
        kind, rest = "event", key[len("events:"):]
    else:
        return None
    slug = rest.split(":", 1)[0]
    if not slug:
        return None
    return kind, slug


def _parse_event(raw: bytes) -> dict | None:
    try:
        text = raw.decode("utf-8")
        unix_ms_str, referrer, device_class = text.split("|", 2)
        unix_ms = int(unix_ms_str)
    except (ValueError, UnicodeDecodeError):
        return None

    # Millisecond resolution, deliberately. The record has always carried it
    # (redirect writes UnixMilli), and this function used to round it away to
    # whole seconds — which was invisible while EventSlot's slot aliasing was
    # discarding most events, and became misleading the moment that was fixed:
    # several clicks legitimately land in one second, so the table rendered
    # rows that looked like duplicates of each other.
    timestamp = to_iso8601_utc_ms(datetime.fromtimestamp(unix_ms / 1000, tz=timezone.utc))
    return {"timestamp": timestamp, "unix_ms": unix_ms, "referrer": referrer, "device_class": device_class}


async def handle_click_totals(links_store, analytics_store, principal: Principal, list_keys, get_many):
    """Click totals for every link the caller can see, for the dashboard's
    Clicks column. Totals only — no per-day map, no events.

    THE READ COST IS THE WHOLE DESIGN HERE, so read this before changing it.

    The naive shape is `COUNT_SHARDS + 1` reads per link: 65 x N. At 200 links
    that is 13,000 reads for one dashboard load, against an app-wide cap of
    1,000 reads/second — a single page view would consume the entire
    application's read budget for thirteen seconds. That is why the Clicks
    column was originally deferred as a product decision rather than treated
    as a small addition.

    Instead this enumerates the analytics namespace ONCE and reads only the
    count keys that actually exist. A shard key is written on first use, so a
    link with clicks in five shards costs five reads, not sixty-five. Cost
    becomes proportional to real traffic rather than to links x shard count,
    which also means raising COUNT_SHARDS again does not multiply this
    endpoint's cost the way it would have multiplied the naive one.

    Since docs/plans/batch-kv-reads.md, those reads are also issued through
    `get_many` (kvbatch.scoped_get_many) rather than `gather_reads`: the
    read COUNT is unchanged (still exactly the shard keys that exist), but
    it now costs one host call per MAX_KEYS_PER_GET_MANY-sized chunk instead
    of one host call per key — at the modelled ceiling of ~6,100 keys for a
    100-link x 200-click dashboard load, a handful of chunked calls instead
    of thousands of individual round trips. This is a LATENCY fix, not
    (necessarily) a read-cap fix — see kvbatch.py's docstring and TASKS.md's
    "BOTH SPIKES ANSWERED" for the measured quota-accounting answer.

    Rejected alternative, recorded so it is not re-proposed: maintaining a
    denormalized `analytics:total:<slug>` would make this O(N) reads, but it
    adds a THIRD KV write to every click. Writes are the binding constraint
    (50/second app-wide, already two per click); trading read cost for write
    cost is backwards here.
    """
    # docs/plans/derived-link-indexes.md, Stage 1: visible is derived from a
    # `slug:` key enumeration rather than read from all_links/owner_links:.
    # `list_keys` here is the per-request MEMOIZED walk (api/app.py), shared
    # with the analytics-namespace enumeration below, so this endpoint still
    # costs exactly one raw get_keys walk per request, not two.
    slugs = await links.enumerate_slugs(links_store, list_keys)
    if principal.has_permission("links.view_all") or principal.has_permission("links.edit_all"):
        visible = set(slugs)
    else:
        fetched = await get_many(links_store, [f"slug:{slug}" for slug in slugs])
        visible = set()
        for slug in slugs:
            raw = fetched.get(f"slug:{slug}")
            if raw is None:
                continue
            try:
                record = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if can_view(principal, record):
                visible.add(slug)

    if not visible:
        return json_response(200, {"totals": {}})

    # One enumeration, then only the keys that exist and belong to a slug the
    # caller may see. A slug can never contain a colon (CUSTOM_SLUG_PATTERN),
    # so splitting on it is unambiguous.
    keys = await list_keys(analytics_store)
    wanted: dict[str, list[str]] = {}
    for key in keys:
        parsed = parse_analytics_key(key)
        if parsed is None or parsed[0] != "count":
            continue
        slug = parsed[1]
        if slug in visible:
            wanted.setdefault(slug, []).append(key)

    flat = [key for slug_keys in wanted.values() for key in slug_keys]
    by_key = await get_many(analytics_store, flat)

    totals = {}
    for slug in visible:
        total, _days = _merge_counts(by_key.get(key) for key in wanted.get(slug, []))
        totals[slug] = total

    return json_response(200, {"totals": totals})


async def handle_analytics(
    links_store, analytics_store, principal: Principal, slug: str, num_event_slots: int, get_many
):
    record = await get_link(links_store, slug)
    if record is None:
        return json_response(404, {"error": "not_found"})
    if not can_view(principal, record):
        return json_response(403, {"error": "forbidden", "required_permission": "links.view_all"})

    # Every key below is independent, so they are all fetched in one
    # get_many host call (docs/plans/batch-kv-reads.md) rather than one
    # round trip per shard plus one per event slot — which is what used to
    # make COUNT_SHARDS show up directly in page latency. Correctness does
    # not depend on how many host calls this costs; only kv_ops/kv_keys in
    # the logfmt line make that visible.
    #
    # The legacy unsharded key goes first — nothing writes it any more, but
    # clicks recorded before sharding landed still live there, so summing it in
    # is what makes this a no-migration change.
    count_keys = [f"count:{slug}"] + [f"count:{slug}:{shard}" for shard in range(COUNT_SHARDS)]
    event_keys = [f"events:{slug}:{slot}" for slot in range(num_event_slots)]

    fetched = await get_many(analytics_store, count_keys + event_keys)
    total, days = _merge_counts(fetched.get(key) for key in count_keys)

    events = []
    for key in event_keys:
        raw = fetched.get(key)
        if raw is None:
            continue
        event = _parse_event(raw)
        if event is not None:
            events.append(event)

    events.sort(key=lambda e: e["unix_ms"], reverse=True)
    for event in events:
        del event["unix_ms"]

    return json_response(200, {
        "total": total,
        "days": days,
        "recent_events": events,
    })
