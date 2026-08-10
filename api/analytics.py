"""GET /api/links/{slug}/analytics — reads the count/events data redirect
writes into the `analytics` KV store on every successful click.
"""

import asyncio
import json
from datetime import datetime, timezone

from auth import Principal
from kvbatch import gather_reads
from links import can_view, get_link, owned_slugs
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


async def handle_click_totals(links_store, analytics_store, principal: Principal, list_keys):
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

    Rejected alternative, recorded so it is not re-proposed: maintaining a
    denormalized `analytics:total:<slug>` would make this O(N) reads, but it
    adds a THIRD KV write to every click. Writes are the binding constraint
    (50/second app-wide, already two per click); trading read cost for write
    cost is backwards here.
    """
    if principal.has_permission("links.view_all") or principal.has_permission("links.edit_all"):
        visible = set(await _all_slugs_for_totals(links_store))
    else:
        visible = set(await owned_slugs(links_store, principal.username))

    if not visible:
        return json_response(200, {"totals": {}})

    # One enumeration, then only the keys that exist and belong to a slug the
    # caller may see. A slug can never contain a colon (CUSTOM_SLUG_PATTERN),
    # so splitting on it is unambiguous.
    keys = await list_keys(analytics_store)
    wanted: dict[str, list[str]] = {}
    for key in keys:
        if not key.startswith("count:"):
            continue
        rest = key[len("count:"):]
        slug = rest.split(":", 1)[0]
        if slug in visible:
            wanted.setdefault(slug, []).append(key)

    flat = [key for slug_keys in wanted.values() for key in slug_keys]
    values = await gather_reads(analytics_store.get(key) for key in flat)
    by_key = dict(zip(flat, values))

    totals = {}
    for slug in visible:
        total, _days = _merge_counts(by_key.get(key) for key in wanted.get(slug, []))
        totals[slug] = total

    return json_response(200, {"totals": totals})


async def _all_slugs_for_totals(links_store):
    raw = await links_store.get("all_links")
    return json.loads(raw) if raw else []


async def handle_analytics(links_store, analytics_store, principal: Principal, slug: str, num_event_slots: int):
    record = await get_link(links_store, slug)
    if record is None:
        return json_response(404, {"error": "not_found"})
    if not can_view(principal, record):
        return json_response(403, {"error": "forbidden", "required_permission": "links.view_all"})

    # Every read below is independent, so they are issued together rather than
    # one after another. Sequentially this endpoint costs one KV round trip per
    # shard plus one per event slot, which is what makes the shard count show up
    # directly in page latency; concurrently it costs about one round trip in
    # total, which is what decouples COUNT_SHARDS from how slow this page feels.
    #
    # Whether the host actually overlaps them is visible in the logfmt line:
    # kv_us is the SUM of per-operation durations while dur_us is wall time, so
    # kv_us >> dur_us means real overlap and kv_us ~= dur_us means the awaits
    # ran one at a time. Correctness does not depend on the answer.
    #
    # The legacy unsharded key goes first — nothing writes it any more, but
    # clicks recorded before sharding landed still live there, so summing it in
    # is what makes this a no-migration change.
    count_keys = [f"count:{slug}"] + [f"count:{slug}:{shard}" for shard in range(COUNT_SHARDS)]
    event_keys = [f"events:{slug}:{slot}" for slot in range(num_event_slots)]

    fetched = await asyncio.gather(
        *(analytics_store.get(key) for key in count_keys + event_keys)
    )
    total, days = _merge_counts(fetched[:len(count_keys)])

    events = []
    for raw in fetched[len(count_keys):]:
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
