"""Link authoring: create/list/get/delete for the /api/links surface, plus
custom slugs and per-link password protection.
"""

import json
import re
import secrets
import string
from urllib.parse import urlparse

import auth
import domains
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


class UnreadableLinkError(Exception):
    """A `slug:` record exists but its value will not parse.

    Distinct from "absent" on purpose. Before this existed, `get_link` let
    `json.loads` raise, every one of its six callers inherited that, and the
    handler turned it into `500 internal_error` — which tells an operator to
    retry a transient fault when it is a permanent data fault only they can
    fix. Every other surface already knew better: `handle_list` skips it, and
    `GET /api/admin/consistency` names it `unreadable_value`.

    `redirect` used to be the odd one out here: it treated an unparseable
    record as not-found and 404'd (`lookupLink`). Since
    docs/plans/redirect-read-failure-not-404.md it answers `500` instead
    (`linkgate.Resolve` -> `DispositionUnreadable`), for the same reason this
    class exists on the API side — a 404 tells a visitor "no such link",
    which is false for a record that exists but is corrupt. This class's own
    422 stays distinct from `redirect`'s 500 for a different reason than
    before: `api` has an authenticated JSON client that can be told something
    specific (`link_record_unreadable`), while a browser navigating to
    `/r/{slug}` has no way to consume that detail — 500 is the right answer
    there precisely because 503 (this codebase's new "transient, retry"
    status) now exists to take the other meaning by contrast.

    `api`'s own notion of "unreadable" is narrower than `linkgate.ParseLink`'s:
    `json.loads` type-checks nothing, so a record like `{"status": 7}` parses
    happily here and is served as a link record, while the same bytes 500 at
    `/r/{slug}` (a Go struct-tagged type mismatch). `get_link` rejects only
    non-JSON and invalid-UTF-8 — the two things `json.loads` itself can raise
    on. `api/consistency.py`'s `_parse_link_record` rejects a third,
    overlapping-but-different set again (non-object, or `owner` missing/not a
    string). The three code paths genuinely disagree about what "unreadable"
    means; see docs/plans/api-record-unreadable-diagnostics.md.
    """

    def __init__(self, slug: str, cause: BaseException | None = None):
        super().__init__(slug)
        self.slug = slug
        # The decoder error json.loads raised, kept EXPLICITLY rather than
        # relying on __cause__ from `raise ... from exc`: it is the only thing
        # in this component that says WHY a record will not parse (line and
        # column, and JSONDecodeError vs UnicodeDecodeError), and a future
        # `raise UnreadableLinkError(slug)` with no `from` would silently drop
        # it with nothing failing. Defaulted to None so this stays
        # constructible from a test with no exception in hand; a None cause
        # degrades the log line, never breaks it (see api/app.py).
        self.cause = cause


async def get_link(store, slug: str) -> dict | None:
    raw = await store.get(f"slug:{slug}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise UnreadableLinkError(slug, exc) from exc


# A link record has no size bound of its own below Akamai's 1 MB max value
# size, and since docs/plans/derived-link-indexes.md EVERY visible record is
# fetched on every dashboard load, so the population paying for an oversized
# record is now everyone rather than its owner. 4096 bytes bounds a record to
# roughly 4.5 KB while staying clear of real marketing URLs — UTM-laden
# campaign links run to a few hundred bytes, so this is orders of magnitude of
# headroom, not a tight fit. **Raising it needs evidence of a real rejected
# URL, and LOWERING it can reject links that already exist** — the asymmetry
# the sibling caps (MAX_BULK_ROWS, MAX_BACKUP_ENTRIES, MAX_PURGE_KEYS_PER_REQUEST)
# all carry. Measured in BYTES, not characters: the bound being protected is a
# stored value's size, and a percent-encoded or non-ASCII URL costs more bytes
# than it has characters.
MAX_TARGET_URL_BYTES = 4096


def is_valid_target_url(target_url: str) -> bool:
    parsed = urlparse(target_url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def target_url_error_body(error_code: str) -> dict:
    """Echoes the cap in the response so no client ever hardcodes it, the same
    convention every sibling cap in this codebase follows. Public — shared
    across modules the same way can_view/can_edit/target_url_error are:
    bulk.py's repoint action builds this same body without restating the cap."""
    if error_code == "target_url_too_long":
        return {"error": error_code, "max_bytes": MAX_TARGET_URL_BYTES}
    return {"error": error_code}


# Control characters in a target URL are rejected at authoring time, not
# just discouraged: the redirect component emits target_url VERBATIM as the
# Location header of its 302, and the Go SDK serializes header values to the
# wire unvalidated (toWasiHeaders checks header NAMES only — confirmed in
# spin-go-sdk/v3@v3.0.0 http/http.go). So "https://example.com/x\r\nX-Evil: yes"
# is a real CRLF in a live 302, not a curiosity. urlparse strips \t\r\n from
# its parsed view but keeps \x00-\x08, \x0b-\x0c, \x0e-\x1f and \x7f — and the
# ORIGINAL string is what gets stored — so the check must be explicit and run
# against the stored bytes, never delegated to the parser. This is the same
# "enforced in two of three places is not enforced" rule the length cap
# carries: all four authoring paths (create, update, bulk create, repoint)
# funnel through target_url_error, and the redirect is the un-enforced fourth
# place, closed here.
#
# Percent-encoded forms (%0d%0a) need NO rejection: they are inert literal
# text inside the header value and are only decoded by the *new* URL the
# Location points at, never by this app's header emission.
_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


def target_url_error(target_url) -> str | None:
    """The single choke point for destination-URL shape, returning an error
    code or None. All three authoring paths (handle_create, handle_update,
    bulk.validate_bulk_rows) go through this rather than repeating the checks
    — CLAUDE.md's destination-URL-policy section states the rule this follows:
    a constraint enforced in two of three places is not enforced.

    Deliberately does NOT cover the destination *policy* (urlpolicy.evaluate),
    which needs the policy record and returns a richer verdict.

    Control characters reuse "invalid_target_url" rather than a distinct code
    deliberately: a control-bearing URL is indistinguishable from "not a URL"
    to every client, the GUI already maps that code to a sensible message, and
    a distinct code would add surface for an input only ever crafted as an
    attack. The redirect-side guard (linkgate.ParseLink) is the second half of
    this fix — a control-char target that reaches storage by ANY route (e.g.
    restore) is refused there as DispositionUnreadable -> 500, never emitted.
    """
    if not isinstance(target_url, str) or not is_valid_target_url(target_url):
        return "invalid_target_url"
    if _CONTROL_CHAR_PATTERN.search(target_url):
        return "invalid_target_url"
    if len(target_url.encode("utf-8")) > MAX_TARGET_URL_BYTES:
        return "target_url_too_long"
    return None


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
    # allowed_domains always emits as a list, absent/None/[] all meaning
    # "unrestricted" (docs/plans/per-link-domain-restriction.md) — the same
    # "carry a list, never sometimes omit the key" shape tags already has.
    public["allowed_domains"] = record.get("allowed_domains") or []
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


async def handle_create(store, principal: Principal, request, configured_domains: list[str], write=kvretry.direct):
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return json_response(400, {"error": "invalid_json"})

    target_url = payload.get("target_url")
    url_error = target_url_error(target_url)
    if url_error:
        return json_response(400, target_url_error_body(url_error))

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

    allowed_domains_result, allowed_domains_error = domains.normalize_allowed_domains(
        payload.get("allowed_domains"), configured_domains
    )
    if allowed_domains_error:
        return json_response(400, {"error": allowed_domains_error})

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
        "allowed_domains": allowed_domains_result,
        "created_at": now,
        "updated_at": now,
    }
    # docs/plans/write-throttle-resilience.md: retried under RECORD_WRITE. If
    # the record write itself is exhausted, nothing has been written and
    # kvretry.WriteFailed propagates uncaught, same as any other write
    # failure did before this — no special handling, since there is nothing
    # to report a partial result about.
    #
    # docs/plans/derived-link-indexes.md, Stage 2: there is no index write
    # here any more. A record's existence is the only truth, so a single
    # create is exactly one KV write and this always returns 201.
    await write(lambda: store.set(f"slug:{slug}", json.dumps(record).encode("utf-8")), kvretry.RECORD_WRITE)
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


UPDATABLE_FIELDS = {"target_url", "status", "start_at", "end_at", "tags", "allowed_domains"}


async def handle_update(
    store, principal: Principal, slug: str, request, configured_domains: list[str], write=kvretry.direct
):
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
        url_error = target_url_error(target_url)
        if url_error:
            return json_response(400, target_url_error_body(url_error))
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

    # Key-presence decides, exactly like start_at/end_at above: absent leaves
    # the stored value untouched, a list replaces wholesale, null or []
    # clears. also_allowed lets a stored-but-no-longer-configured entry stay
    # valid on resubmission, so an operator retiring a domain from
    # public_base_urls can never make an existing restricted record
    # unsaveable — but omitting the field entirely never silently widens it.
    if "allowed_domains" in payload:
        allowed_domains_result, allowed_domains_error = domains.normalize_allowed_domains(
            payload["allowed_domains"],
            configured_domains,
            also_allowed=record.get("allowed_domains") or [],
        )
        if allowed_domains_error:
            return json_response(400, {"error": allowed_domains_error})
        record["allowed_domains"] = allowed_domains_result

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

    Ordering is load-bearing: record delete, then (only if passed) the
    analytics purge. Every interruption before the purge leaves a recoverable
    state (orphan analytics keys, the shipped operator tool's whole reason to
    exist); the reverse ordering would leave a live, resolving link whose
    click history vanished with no tool able to restore it. See
    docs/plans/inline-analytics-purge-on-delete.md.

    docs/plans/derived-link-indexes.md, Stage 2: there is no index to update
    any more, so the response no longer carries index_updated at all.

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
        # docs/plans/derived-link-indexes.md, Stage 2: there is no index to
        # correct any more — deleting the record is the whole repair. That
        # entire failure mode (an unreadable record's owner being unknowable,
        # blocking an index cleanup) disappears with the index itself.
        await store.delete(f"slug:{slug}")
        return json_response(200, {"ok": True, "record_was_unreadable": True})

    if record is None:
        return json_response(404, {"error": "not_found"})
    if not can_edit(principal, record):
        return json_response(403, {"error": "forbidden", "required_permission": "links.edit_all"})
    # docs/plans/write-throttle-resilience.md: retried under RECORD_WRITE. If
    # exhausted, nothing has been deleted and kvretry.WriteFailed propagates
    # uncaught, same as any other write failure did before this.
    await write(lambda: store.delete(f"slug:{slug}"), kvretry.RECORD_WRITE)

    if purge_analytics is None:
        return json_response(200, {"ok": True})

    try:
        analytics_purge = await purge_analytics(slug)
    except Exception:
        analytics_purge = {"status": "failed", "found_keys": 0, "deleted_keys": 0}

    return json_response(200, {"ok": True, "analytics_purge": analytics_purge})


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
