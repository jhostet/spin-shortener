"""GET /api/links/{slug}/analytics — reads the count/events data redirect
writes into the `analytics` KV store on every successful click.
"""

import json

import links
from auth import Principal
from links import can_view, get_link
from responses import json_response


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

    The "event" branch stays even though nothing writes `events:` keys any
    more (redirect's recordClickEvent was retired 2026-08-18 — see
    docs/plans/drop-events-write.md): leftover `events:` keys from before that
    change still exist in real stores, and analyticsorphans.classify_analytics_keys
    must keep recognising them or they become permanently unpurgeable
    unrecognized_keys, showing up in the orphan report's unrecognized_sample
    on every run.
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
    denormalized `analytics:total:<slug>` would make this O(N) reads. **The
    real objection is CONTENTION, not the write cap** — an earlier version of
    this docstring said writes are the binding constraint, and that reason was
    wrong, so the re-costing on 2026-08-12 is recorded here rather than left
    to be rediscovered.

    A single `analytics:total:<slug>` is a read-modify-write on ONE key per
    link, which is exactly the shape the click counter was sharded to escape:
    it carries the same measured loss curve as the pre-sharding counter — 0%
    below ~3 clicks/second on one link, **25% at 9.4/second, measured live
    2026-08-06** — while the 64-shard sum stays exact at the same rates. A
    denormalized total would therefore be a *wrong* number, not merely a third
    write, and it would be wrong precisely for the busy links a dashboard
    total matters most for.

    `wasi:keyvalue/atomics`' `increment` cannot rescue it: it is documented
    unsupported on Akamai Functions, so there is no compare-and-swap or atomic
    add available anywhere in this stack. See
    docs/plans/denormalised-click-total.md for the full re-costing, including
    why the trade is one extra write per click against thousands of reads per
    dashboard load rather than the simple write-cap argument this docstring
    used to make.
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


async def handle_analytics(links_store, analytics_store, principal: Principal, slug: str, get_many):
    record = await get_link(links_store, slug)
    if record is None:
        return json_response(404, {"error": "not_found"})
    if not can_view(principal, record):
        return json_response(403, {"error": "forbidden", "required_permission": "links.view_all"})

    # Every shard key is independent, so they are all fetched in one
    # get_many host call (docs/plans/batch-kv-reads.md) rather than one round
    # trip per shard — which is what used to make COUNT_SHARDS show up
    # directly in page latency. Correctness does not depend on how many host
    # calls this costs; only kv_ops/kv_keys in the logfmt line make that
    # visible.
    #
    # The legacy unsharded key goes first — nothing writes it any more, but
    # clicks recorded before sharding landed still live there, so summing it in
    # is what makes this a no-migration change.
    count_keys = [f"count:{slug}"] + [f"count:{slug}:{shard}" for shard in range(COUNT_SHARDS)]

    fetched = await get_many(analytics_store, count_keys)
    total, days = _merge_counts(fetched.get(key) for key in count_keys)

    return json_response(200, {
        "total": total,
        "days": days,
    })
