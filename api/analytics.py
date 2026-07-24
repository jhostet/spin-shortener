"""GET /api/links/{slug}/analytics — reads the count/events data redirect
writes into the `analytics` KV store on every successful click.
"""

import json
from datetime import datetime, timezone

from auth import Principal
from links import can_view, get_link
from responses import json_response, to_iso8601_utc


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

    count_raw = await analytics_store.get(f"count:{slug}")
    if count_raw:
        count = json.loads(count_raw)
    else:
        count = {"total": 0, "days": {}}

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
        "total": count.get("total", 0),
        "days": count.get("days", {}),
        "recent_events": events,
    })
