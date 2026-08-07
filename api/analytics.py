"""GET /api/links/{slug}/analytics — reads the count/events data redirect
writes into the `analytics` KV store on every successful click.
"""

import json
from datetime import datetime, timezone

from auth import Principal
from links import can_view, get_link
from responses import json_response, to_iso8601_utc


# MUST stay equal to redirect/linkgate/keys.go's CountShards — see that file
# for the full rule. Lowering this silently drops every click that was
# recorded into a higher shard. api/tests/test_kvprefix.py pins the equality.
COUNT_SHARDS = 16


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

    timestamp = to_iso8601_utc(datetime.fromtimestamp(unix_ms / 1000, tz=timezone.utc))
    return {"timestamp": timestamp, "unix_ms": unix_ms, "referrer": referrer, "device_class": device_class}


async def handle_analytics(links_store, analytics_store, principal: Principal, slug: str, num_event_slots: int):
    record = await get_link(links_store, slug)
    if record is None:
        return json_response(404, {"error": "not_found"})
    if not can_view(principal, record):
        return json_response(403, {"error": "forbidden", "required_permission": "links.view_all"})

    # The legacy unsharded key first — nothing writes it any more, but clicks
    # recorded before sharding landed still live there, so summing it in is
    # what makes this a no-migration change.
    blobs = [await analytics_store.get(f"count:{slug}")]
    for shard in range(COUNT_SHARDS):
        blobs.append(await analytics_store.get(f"count:{slug}:{shard}"))
    total, days = _merge_counts(blobs)

    events = []
    for slot in range(num_event_slots):
        raw = await analytics_store.get(f"events:{slug}:{slot}")
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
