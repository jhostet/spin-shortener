"""Link authoring: create/list/get/delete for the /api/links surface, plus
custom slugs and per-link password protection.
"""

import json
import re
import secrets
import string
from urllib.parse import urlparse

import auth
import kvretry
import tags
import urlpolicy
from auth import Principal
from responses import iso_now, json_response, parse_iso8601_utc, to_iso8601_utc

SLUG_ALPHABET = string.ascii_letters + string.digits
SLUG_LENGTH = 7
SLUG_GENERATION_ATTEMPTS = 5

CUSTOM_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
MIN_LINK_PASSWORD_LENGTH = 4
LINK_STATUSES = ("active", "disabled")


def generate_slug() -> str:
    return "".join(secrets.choice(SLUG_ALPHABET) for _ in range(SLUG_LENGTH))


async def allocate_random_slug(store, taken: set[str]) -> str:
    """Random slug not in `taken` and not already in the store. Adds the
    result to `taken` so a caller allocating many in one pass cannot collide
    with itself without a KV round trip per candidate."""
    for _ in range(SLUG_GENERATION_ATTEMPTS):
        slug = generate_slug()
        if slug not in taken and not await store.exists(f"slug:{slug}"):
            taken.add(slug)
            return slug
    raise RuntimeError("failed to allocate a unique slug")


async def owned_slugs(store, username: str) -> list[str]:
    """Shared (not module-private) — users.py's handle_delete reads it to
    decide whether a user still owns links before allowing their deletion,
    the same reason can_view/can_edit below are public."""
    raw = await store.get(f"owner_links:{username}")
    return json.loads(raw) if raw else []


ALL_SLUGS_INDEX_KEY = "all_links"


async def all_slugs(store) -> list[str]:
    raw = await store.get(ALL_SLUGS_INDEX_KEY)
    return json.loads(raw) if raw else []


SLUG_KEY_PREFIX = "slug:"


async def enumerate_slugs(store, list_keys) -> list[str]:
    """Every slug that has a record, derived from a key enumeration rather
    than read from `all_links`/`owner_links:<owner>`. This is what
    docs/plans/derived-link-indexes.md's Stage 1 uses to remove the two
    indexes as a shared, racy, read-modify-write bottleneck on the authoring
    hot path — see that plan for the measured lost-update incident this
    fixes. Order is UNSPECIFIED — KV key order is not defined, so any caller
    that renders a list must impose its own ordering."""
    return [
        key[len(SLUG_KEY_PREFIX):]
        for key in await list_keys(store)
        if key.startswith(SLUG_KEY_PREFIX)
    ]


async def slugs_owned_by(store, username: str, list_keys, get_many) -> list[str]:
    """Every slug whose record's `owner` field equals `username`, derived
    from enumerate_slugs + get_many rather than read from the
    `owner_links:<username>` index. Used by users.handle_delete's 409 gate
    (docs/plans/derived-link-indexes.md) so that a drifted or missing index
    entry can no longer let a user's links go unnoticed at deletion time.
    Skips a slug whose record is missing or unreadable, the same tolerance
    handle_list applies."""
    slugs = await enumerate_slugs(store, list_keys)
    fetched = await get_many(store, [f"{SLUG_KEY_PREFIX}{slug}" for slug in slugs])
    owned = []
    for slug in slugs:
        raw = fetched.get(f"{SLUG_KEY_PREFIX}{slug}")
        if raw is None:
            continue
        try:
            record = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if record.get("owner") == username:
            owned.append(slug)
    return owned


async def add_slugs_to_indexes(store, owner: str, slugs: list[str], write=kvretry.direct) -> None:
    """One read+write of `all_links`, one of `owner_links:<owner>`, for any
    number of slugs. Order-preserving, skips slugs already present.

    `write` (docs/plans/write-throttle-resilience.md) defaults to
    `kvretry.direct` (call through, no retry) so the ~20 existing test call
    sites that use this helper purely to seed fixtures are unaffected. A real
    caller (`bulk.py`, `links.handle_create`) passes a request-scoped writer
    built by `kvretry.make_writer`, and each `store.set` is retried under
    `kvretry.INDEX_WRITE` — every key this function touches IS an index.
    """
    slugs_index = await all_slugs(store)
    for slug in slugs:
        if slug not in slugs_index:
            slugs_index.append(slug)
    await write(lambda: store.set(ALL_SLUGS_INDEX_KEY, json.dumps(slugs_index).encode("utf-8")), kvretry.INDEX_WRITE)

    owned = await owned_slugs(store, owner)
    for slug in slugs:
        if slug not in owned:
            owned.append(slug)
    await write(lambda: store.set(f"owner_links:{owner}", json.dumps(owned).encode("utf-8")), kvretry.INDEX_WRITE)


async def remove_slugs_from_indexes(store, slugs_by_owner: dict[str, list[str]], write=kvretry.direct) -> None:
    """One read+write of `all_links` total, plus one per distinct owner. Takes
    a per-owner mapping because a `links.edit_all` user can delete links
    belonging to several owners in a single action. See `add_slugs_to_indexes`
    for the `write` parameter's default and rationale."""
    all_to_remove = {slug for slugs in slugs_by_owner.values() for slug in slugs}
    slugs_index = await all_slugs(store)
    slugs_index = [slug for slug in slugs_index if slug not in all_to_remove]
    await write(lambda: store.set(ALL_SLUGS_INDEX_KEY, json.dumps(slugs_index).encode("utf-8")), kvretry.INDEX_WRITE)

    for owner, slugs in slugs_by_owner.items():
        to_remove = set(slugs)
        owned = await owned_slugs(store, owner)
        owned = [slug for slug in owned if slug not in to_remove]
        await write(lambda o=owner, ow=owned: store.set(f"owner_links:{o}", json.dumps(ow).encode("utf-8")), kvretry.INDEX_WRITE)


async def move_slugs_between_owners(
    store, slugs_by_old_owner: dict[str, list[str]], new_owner: str, write=kvretry.direct
) -> None:
    """Reassignment's index half. One read+write of `owner_links:<new_owner>`,
    plus one per distinct old owner — the same one-read-one-write-per-index
    shape as add_slugs_to_indexes/remove_slugs_from_indexes, because Spin KV
    has no compare-and-swap and a per-slug read-modify-write would multiply
    the race window by N. See `add_slugs_to_indexes` for the `write`
    parameter's default and rationale.

    Deliberately never reads or writes `all_links`: a reassignment does not
    change all_links membership, and calling remove_slugs_from_indexes here
    would strip the slugs from it entirely.

    Adds to the new owner FIRST, then removes from each old owner, and skips
    any old owner equal to new_owner (without that guard a same-owner
    "reassignment" would remove the slugs from the index it just added them
    to). Both halves are idempotent, so re-running with the same arguments
    converges rather than compounding.
    """
    all_slugs_to_move = [slug for slugs in slugs_by_old_owner.values() for slug in slugs]

    owned_new = await owned_slugs(store, new_owner)
    for slug in all_slugs_to_move:
        if slug not in owned_new:
            owned_new.append(slug)
    await write(lambda: store.set(f"owner_links:{new_owner}", json.dumps(owned_new).encode("utf-8")), kvretry.INDEX_WRITE)

    for old_owner, slugs in slugs_by_old_owner.items():
        if old_owner == new_owner:
            continue
        to_remove = set(slugs)
        owned_old = await owned_slugs(store, old_owner)
        owned_old = [slug for slug in owned_old if slug not in to_remove]
        await write(
            lambda o=old_owner, ow=owned_old: store.set(f"owner_links:{o}", json.dumps(ow).encode("utf-8")),
            kvretry.INDEX_WRITE,
        )


class UnreadableLinkError(Exception):
    """A `slug:` record exists but its value will not parse.

    Distinct from "absent" on purpose. Before this existed, `get_link` let
    `json.loads` raise, every one of its six callers inherited that, and the
    handler turned it into `500 internal_error` — which tells an operator to
    retry a transient fault when it is a permanent data fault only they can
    fix. Every other surface already knew better: `redirect` treats an
    unparseable record as not-found and 404s (`lookupLink`), `handle_list`
    skips it, and `GET /api/admin/consistency` names it `unreadable_value`.
    """

    def __init__(self, slug: str):
        super().__init__(slug)
        self.slug = slug


async def get_link(store, slug: str) -> dict | None:
    raw = await store.get(f"slug:{slug}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise UnreadableLinkError(slug) from exc


def is_valid_target_url(target_url: str) -> bool:
    parsed = urlparse(target_url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def is_valid_custom_slug(slug: str) -> bool:
    return bool(CUSTOM_SLUG_PATTERN.match(slug))


def parse_window_field(value) -> tuple[str | None, bool]:
    """Returns (normalized_iso8601_utc_or_None, is_invalid). `value=None` means
    "unset", not invalid — an explicit-but-unparsable string is what's invalid.
    """
    if value is None:
        return None, False
    if not isinstance(value, str):
        return None, True
    parsed = parse_iso8601_utc(value)
    if parsed is None:
        return None, True
    return to_iso8601_utc(parsed), False


def public_link(record: dict) -> dict:
    """Link record with `password_hash` replaced by a boolean flag before it's ever serialized to a client."""
    public = {k: v for k, v in record.items() if k != "password_hash"}
    public["password_protected"] = bool(record.get("password_hash"))
    public["tags"] = record.get("tags", [])
    return public


def can_view(principal: Principal, record: dict) -> bool:
    """`links.edit_all` implies view access — a user who can edit any link
    shouldn't also need `links.view_all` separately to open one. Shared
    (not module-private) since analytics.py and qr.py gate on the same
    view semantics for a link's analytics/QR endpoints."""
    return (
        record["owner"] == principal.username
        or principal.has_permission("links.view_all")
        or principal.has_permission("links.edit_all")
    )


def can_edit(principal: Principal, record: dict) -> bool:
    """Shared (not module-private) — bulk.py gates bulk-action rows on the
    same write semantics as the single-link edit/delete/password handlers
    below."""
    return record["owner"] == principal.username or principal.has_permission("links.edit_all")


async def handle_create(store, principal: Principal, request, write=kvretry.direct):
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return json_response(400, {"error": "invalid_json"})

    target_url = payload.get("target_url")
    if not isinstance(target_url, str) or not is_valid_target_url(target_url):
        return json_response(400, {"error": "invalid_target_url"})

    policy = await urlpolicy.load_policy(store)
    verdict = urlpolicy.evaluate(target_url, policy)
    if not verdict["allowed"]:
        return json_response(400, {
            "error": "destination_not_allowed",
            "host": verdict["host"],
            "reason": verdict["reason"],
            "matched_rule": verdict["matched_rule"],
        })

    custom_slug = payload.get("custom_slug")
    if custom_slug is not None:
        if not principal.has_permission("links.create_custom_slug"):
            return json_response(403, {"error": "forbidden", "required_permission": "links.create_custom_slug"})
        if not isinstance(custom_slug, str) or not is_valid_custom_slug(custom_slug):
            return json_response(400, {"error": "invalid_custom_slug"})
        if await store.exists(f"slug:{custom_slug}"):
            return json_response(409, {"error": "slug_taken"})
        slug = custom_slug
        custom = True
    else:
        slug = await allocate_random_slug(store, set())
        custom = False

    password = payload.get("password")
    password_hash = None
    if password:
        if not isinstance(password, str) or len(password) < MIN_LINK_PASSWORD_LENGTH:
            return json_response(400, {"error": "invalid_password"})
        password_hash = auth.hash_password(password)

    start_at, start_invalid = parse_window_field(payload.get("start_at"))
    if start_invalid:
        return json_response(400, {"error": "invalid_start_at"})
    end_at, end_invalid = parse_window_field(payload.get("end_at"))
    if end_invalid:
        return json_response(400, {"error": "invalid_end_at"})
    if start_at is not None and end_at is not None and start_at >= end_at:
        return json_response(400, {"error": "invalid_window_range"})

    tag_list, tag_error = tags.parse_tags(payload.get("tags"))
    if tag_error:
        return json_response(400, tag_error)

    now = iso_now()
    record = {
        "slug": slug,
        "target_url": target_url,
        "owner": principal.username,
        "custom": custom,
        "password_hash": password_hash,
        "status": "active",
        "start_at": start_at,
        "end_at": end_at,
        "tags": tag_list,
        "created_at": now,
        "updated_at": now,
    }
    # docs/plans/write-throttle-resilience.md: retried under RECORD_WRITE. If
    # the record write itself is exhausted, nothing has been written and
    # kvretry.WriteFailed propagates uncaught, same as any other write
    # failure did before this — no special handling, since there is nothing
    # to report a partial result about.
    await write(lambda: store.set(f"slug:{slug}", json.dumps(record).encode("utf-8")), kvretry.RECORD_WRITE)

    # The one drift-capable path in this function: the record now exists at
    # /r/{slug} regardless of what happens next, so an exhausted index write
    # must be reported rather than silently dropped — a one-row version of
    # the bulk-create incidents this plan exists for.
    try:
        await add_slugs_to_indexes(store, principal.username, [slug], write)
    except kvretry.WriteFailed:
        return json_response(200, {
            "ok": False,
            "partial": True,
            "link": public_link(record),
            "index_updated": False,
            "next_step": "consistency_repair",
        })
    return json_response(201, public_link(record))


async def handle_list(store, principal: Principal, get_many, list_keys):
    """docs/plans/derived-link-indexes.md, Stage 1: the list is derived from
    a `slug:` key enumeration rather than read from `all_links`/
    `owner_links:<owner>`. Those indexes are a shared, read-modify-write key
    with no compare-and-swap, so any two overlapping authoring requests can
    clobber each other's additions — measured directly on 2026-08-17. This
    removes the race by removing the shared key it depends on, rather than
    making it less likely to be clobbered.
    """
    slugs = await enumerate_slugs(store, list_keys)
    # One get_many host call (or a handful of MAX_KEYS_PER_GET_MANY-sized
    # chunks), not a round trip per link (docs/plans/batch-kv-reads.md,
    # superseding the earlier gather_reads-based fan-out). `GET /api/links`
    # has no pagination, so the sequential form cost ~23 ms per link against
    # a deployed store — ~2.4 s at 100 links.
    #
    # A slug enumerated with no backing record (an interrupted delete, or a
    # stale enumeration entry) is skipped, exactly as before.
    #
    # An UNREADABLE record is skipped too, and that is a fix rather than a
    # nicety: this used to let json.loads raise, which turned one corrupt
    # record into a 500 for the WHOLE page — the entire links table gone,
    # not the one row. Measured live 2026-08-17 against a deliberately
    # corrupted record, and it is the same policy already applied one line
    # above to a record that is missing entirely: a link this handler cannot
    # read is a link it cannot list, and neither case is worth failing the
    # other N links over.
    #
    # Nothing is hidden by skipping. `GET /api/admin/consistency` reports the
    # same record as `unreadable_value`, which is deliberately NOT
    # auto-repairable (the intended content of a corrupt value is
    # unknowable), so the operator still learns about it from the tool whose
    # job that is. On a deployed app that report is the ONLY way to find out:
    # the KV explorer is dev-only, so before this fix a corrupt record left
    # the dashboard dead with no in-product signal of why.
    keys = [f"slug:{slug}" for slug in slugs]
    fetched = await get_many(store, keys)
    records = []
    for slug in slugs:
        raw = fetched.get(f"slug:{slug}")
        if raw is None:
            continue
        try:
            record = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not can_view(principal, record):
            continue
        records.append(record)
    # Enumeration order is unspecified (KV key order is not defined), and the
    # dashboard renders server order by default, so this sort is load-bearing,
    # not cosmetic. Ascending created_at reproduces today's oldest-first
    # all_links append order. The slug tie-break matters because a bulk
    # create stamps one created_at on all its rows, so those rows now render
    # slug-ascending within the batch instead of submission order — the one
    # accepted cosmetic behaviour change in Stage 1.
    records.sort(key=lambda r: (r.get("created_at") or "", r.get("slug") or ""))
    return json_response(200, {"links": [public_link(r) for r in records]})


async def handle_get(store, principal: Principal, slug: str):
    record = await get_link(store, slug)
    if record is None:
        return json_response(404, {"error": "not_found"})
    if not can_view(principal, record):
        return json_response(403, {"error": "forbidden", "required_permission": "links.view_all"})
    return json_response(200, public_link(record))


UPDATABLE_FIELDS = {"target_url", "status", "start_at", "end_at", "tags"}


async def handle_update(store, principal: Principal, slug: str, request, write=kvretry.direct):
    record = await get_link(store, slug)
    if record is None:
        return json_response(404, {"error": "not_found"})
    if not can_edit(principal, record):
        return json_response(403, {"error": "forbidden", "required_permission": "links.edit_all"})

    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return json_response(400, {"error": "invalid_json"})

    if not UPDATABLE_FIELDS & payload.keys():
        return json_response(400, {"error": "no_fields_to_update"})

    if "target_url" in payload:
        target_url = payload["target_url"]
        if not isinstance(target_url, str) or not is_valid_target_url(target_url):
            return json_response(400, {"error": "invalid_target_url"})
        policy = await urlpolicy.load_policy(store)
        verdict = urlpolicy.evaluate(target_url, policy)
        if not verdict["allowed"]:
            return json_response(400, {
                "error": "destination_not_allowed",
                "host": verdict["host"],
                "reason": verdict["reason"],
                "matched_rule": verdict["matched_rule"],
            })
        record["target_url"] = target_url

    if "status" in payload:
        status = payload["status"]
        if status not in LINK_STATUSES:
            return json_response(400, {"error": "invalid_status"})
        record["status"] = status

    # Merge candidate start_at/end_at (new value if provided, else the
    # record's existing value) so e.g. patching only end_at earlier than an
    # existing start_at is still caught.
    merged_start = record.get("start_at")
    merged_end = record.get("end_at")

    if "start_at" in payload:
        merged_start, invalid = parse_window_field(payload["start_at"])
        if invalid:
            return json_response(400, {"error": "invalid_start_at"})

    if "end_at" in payload:
        merged_end, invalid = parse_window_field(payload["end_at"])
        if invalid:
            return json_response(400, {"error": "invalid_end_at"})

    if merged_start is not None and merged_end is not None and merged_start >= merged_end:
        return json_response(400, {"error": "invalid_window_range"})

    record["start_at"] = merged_start
    record["end_at"] = merged_end

    if "tags" in payload:
        tag_list, tag_error = tags.parse_tags(payload["tags"], allow_none=False)
        if tag_error:
            return json_response(400, tag_error)
        record["tags"] = tag_list

    record["updated_at"] = iso_now()
    # Single write, no index — retry only (docs/plans/write-throttle-resilience.md).
    # An exhausted write propagates kvretry.WriteFailed uncaught, same as any
    # other write failure did before this.
    await write(lambda: store.set(f"slug:{slug}", json.dumps(record).encode("utf-8")), kvretry.RECORD_WRITE)
    return json_response(200, public_link(record))


async def handle_delete(store, principal: Principal, slug: str, purge_analytics=None, write=kvretry.direct):
    """`purge_analytics`, when passed, is an injected async callable
    `slug -> dict` (see analyticsorphans.purge_slug_analytics), invoked here
    rather than imported directly — analytics.py already imports links, so
    links.py importing analyticsorphans would be a cycle.

    Ordering is load-bearing: record delete, then both indexes, then (only
    if passed) the analytics purge. Every interruption before the purge
    leaves a recoverable state (orphan analytics keys, the shipped operator
    tool's whole reason to exist); the reverse ordering would leave a live,
    resolving link whose click history vanished with no tool able to
    restore it. See docs/plans/inline-analytics-purge-on-delete.md.

    A KV failure inside `purge_analytics` must never turn a successful
    deletion into a 500 — the link is already gone, and the response must
    say so regardless of what happened to its analytics.
    """
    # Delete is the ONE path that must still work on an unreadable record,
    # and it is deliberately not left to the central 422. A corrupt record is
    # already dead everywhere else — `redirect` 404s it, the list skips it,
    # nothing can edit it — so refusing to delete it would make it permanent:
    # on a deployed app the KV explorer is dev-only, leaving a hand-edited
    # backup restore as the only remedy. Deletion is the repair.
    try:
        record = await get_link(store, slug)
    except UnreadableLinkError:
        # Ownership is unknowable, so the ownership-based check cannot run.
        # Fail closed on the wider permission rather than guessing: only a
        # principal who may edit ANY link may delete one whose owner cannot
        # be read.
        if not (principal.role == "admin" or principal.has_permission("links.edit_all")):
            return json_response(403, {"error": "forbidden", "required_permission": "links.edit_all"})
        await store.delete(f"slug:{slug}")
        # Only `all_links` can be corrected here — the per-owner index is
        # keyed by an owner this record can no longer name. That leaves one
        # `orphan_owner_index_entry`, which is deliberate rather than sloppy:
        # it is exactly what `POST /api/admin/consistency/repair` fixes in a
        # click, and the alternative (enumerating every owner_links key to
        # find the slug) would put an O(users) scan on the ordinary delete
        # path to serve a rare case.
        remaining = [s for s in await all_slugs(store) if s != slug]
        await store.set(ALL_SLUGS_INDEX_KEY, json.dumps(remaining).encode("utf-8"))
        return json_response(200, {
            "ok": True,
            "record_was_unreadable": True,
            "hint": "The record could not be parsed, so its owner index entry could not be "
                    "identified. Run the store consistency check and repair "
                    "orphan_owner_index_entry to finish tidying up.",
        })

    if record is None:
        return json_response(404, {"error": "not_found"})
    if not can_edit(principal, record):
        return json_response(403, {"error": "forbidden", "required_permission": "links.edit_all"})
    # docs/plans/write-throttle-resilience.md: retried under RECORD_WRITE. If
    # exhausted, nothing has been deleted and kvretry.WriteFailed propagates
    # uncaught, same as any other write failure did before this.
    await write(lambda: store.delete(f"slug:{slug}"), kvretry.RECORD_WRITE)

    index_updated = True
    try:
        await remove_slugs_from_indexes(store, {record["owner"]: [slug]}, write)
    except kvretry.WriteFailed:
        index_updated = False

    if purge_analytics is None:
        return json_response(200, {"ok": True, "index_updated": index_updated})

    try:
        analytics_purge = await purge_analytics(slug)
    except Exception:
        analytics_purge = {"status": "failed", "found_keys": 0, "deleted_keys": 0}

    return json_response(200, {"ok": True, "index_updated": index_updated, "analytics_purge": analytics_purge})


async def handle_set_password(store, principal: Principal, slug: str, request, write=kvretry.direct):
    record = await get_link(store, slug)
    if record is None:
        return json_response(404, {"error": "not_found"})
    if not can_edit(principal, record):
        return json_response(403, {"error": "forbidden", "required_permission": "links.edit_all"})

    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return json_response(400, {"error": "invalid_json"})

    password = payload.get("password")
    if password:
        if not isinstance(password, str) or len(password) < MIN_LINK_PASSWORD_LENGTH:
            return json_response(400, {"error": "invalid_password"})
        record["password_hash"] = auth.hash_password(password)
    else:
        record["password_hash"] = None

    record["updated_at"] = iso_now()
    # Single write, no index — retry only (docs/plans/write-throttle-resilience.md).
    await write(lambda: store.set(f"slug:{slug}", json.dumps(record).encode("utf-8")), kvretry.RECORD_WRITE)
    return json_response(200, public_link(record))
