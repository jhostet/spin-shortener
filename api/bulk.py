"""Bulk link management: a pure text parser/row validator (this module) plus
the two /api/links/bulk* handlers built on top of them.

Zero `spin_sdk` imports — `store`/`request` arrive as plain parameters and
`Request`/`Response` come from `responses`, matching the testability rule the
rest of `api/` follows (see `CLAUDE.md`).
"""

import json
from dataclasses import dataclass

import auth
import kvretry
import links
import tags
import urlpolicy
from responses import iso_now, json_response

MAX_BULK_ROWS = 50
MAX_BULK_BODY_BYTES = 262_144

ACTION_STATUSES = {"enable": "active", "disable": "disabled"}  # values ⊆ links.LINK_STATUSES

# "reassign" is a member so a request naming it gets a clean 400/403 rather
# than falling into the delete branch below, but the actual owner-move logic
# lands in a later task (see TASKS.md's "Add the reassign bulk action").
BULK_ACTIONS = {"delete", "enable", "disable", "tag", "untag", "reassign", "repoint", "schedule"}

# None of these strings can ever be a valid destination (a destination must
# start with a scheme, per format rule 6), so dropping a first row whose
# destination or bare token matches one of these is safe rather than magic —
# it could only ever have produced an error otherwise.
HEADER_WORDS = {
    "slug",
    "short link",
    "short_link",
    "shortlink",
    "short url",
    "short_url",
    "destination",
    "destination url",
    "destination_url",
    "destinationurl",
    "target",
    "target url",
    "target_url",
    "url",
    "link",
    "long url",
    "long_url",
}

_BOM = "﻿"


@dataclass
class BulkRow:
    line: int
    slug: str | None  # None = auto-generate
    target_url: str


def _dequote(field: str) -> str:
    field = field.strip()
    if len(field) >= 2 and field[0] == '"' and field[-1] == '"':
        field = field[1:-1]
    return field.strip()


def _split_fields(line: str) -> tuple[str | None, str]:
    """Returns (slug_field_or_None, destination_field) for one non-blank,
    non-comment content line, per the format spec's delimiter precedence."""
    if line.lower().startswith(("http://", "https://")):
        return None, _dequote(line)

    if "\t" in line:
        slug_field, _, dest_field = line.partition("\t")
        return _dequote(slug_field), _dequote(dest_field)

    if "," in line:
        slug_field, _, dest_field = line.partition(",")
        return _dequote(slug_field), _dequote(dest_field)

    token = _dequote(line)
    if links.CUSTOM_SLUG_PATTERN.match(token):
        return token, ""
    return None, token


def parse_bulk_text(text: str) -> list[BulkRow]:
    if text.startswith(_BOM):
        text = text[len(_BOM):]

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    physical_lines = text.split("\n")

    rows: list[BulkRow] = []
    for line_number, raw_line in enumerate(physical_lines, start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue

        slug_field, dest_field = _split_fields(stripped)
        slug = slug_field if slug_field else None
        rows.append(BulkRow(line=line_number, slug=slug, target_url=dest_field))

    if rows:
        first = rows[0]
        dest_lower = first.target_url.lower().strip()
        slug_lower = (first.slug or "").lower().strip()
        is_header = dest_lower in HEADER_WORDS or (not dest_lower and slug_lower in HEADER_WORDS)
        if is_header:
            rows = rows[1:]

    return rows


def validate_bulk_rows(
    rows: list[BulkRow],
    existing_slugs: set[str],
    can_custom_slug: bool,
    policy: dict,
) -> list[dict]:
    """One {"line", "slug", "error"[, "first_line"]} dict per bad row, in line
    order. Empty list means the whole submission is valid.

    `policy` is a REQUIRED fourth parameter, no default — a `policy=None`
    default meaning "no policy" is exactly how the destination URL policy's
    third enforcement path (this one) would stay silently open forever. See
    docs/plans/destination-url-policy.md."""
    errors: list[dict] = []
    seen_slugs: dict[str, int] = {}

    for row in rows:
        error_code = None

        if not row.target_url:
            error_code = "missing_target_url" if row.slug else "invalid_target_url"
        else:
            # Same choke point as handle_create/handle_update, so the length
            # cap cannot be enforced in two of three authoring paths — the
            # failure mode CLAUDE.md's destination-URL-policy section names.
            error_code = links.target_url_error(row.target_url)

        if error_code is None:
            verdict = urlpolicy.evaluate(row.target_url, policy)
            if not verdict["allowed"]:
                errors.append({
                    "line": row.line,
                    "slug": row.slug,
                    "error": "destination_not_allowed",
                    "host": verdict["host"],
                    "reason": verdict["reason"],
                })
                continue

        if error_code is None and row.slug:
            if not links.is_valid_custom_slug(row.slug):
                error_code = "invalid_custom_slug"
            elif not can_custom_slug:
                error_code = "custom_slug_forbidden"
            elif row.slug in existing_slugs:
                error_code = "slug_taken"
            elif row.slug in seen_slugs:
                errors.append({
                    "line": row.line,
                    "slug": row.slug,
                    "error": "duplicate_slug_in_submission",
                    "first_line": seen_slugs[row.slug],
                })
                continue
            else:
                seen_slugs[row.slug] = row.line

        if error_code is not None:
            errors.append({"line": row.line, "slug": row.slug, "error": error_code})

    return errors


async def handle_bulk_create(store, principal, request, get_many, write):
    if len(request.body or b"") > MAX_BULK_BODY_BYTES:
        return json_response(413, {"error": "body_too_large", "max_bytes": MAX_BULK_BODY_BYTES})

    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return json_response(400, {"error": "invalid_json"})

    text = payload.get("text")
    if not isinstance(text, str):
        return json_response(400, {"error": "invalid_text"})

    password = payload.get("password")
    if password:
        if not isinstance(password, str) or len(password) < links.MIN_LINK_PASSWORD_LENGTH:
            return json_response(400, {"error": "invalid_password"})

    start_at, start_invalid = links.parse_window_field(payload.get("start_at"))
    if start_invalid:
        return json_response(400, {"error": "invalid_start_at"})
    end_at, end_invalid = links.parse_window_field(payload.get("end_at"))
    if end_invalid:
        return json_response(400, {"error": "invalid_end_at"})
    if start_at is not None and end_at is not None and start_at >= end_at:
        return json_response(400, {"error": "invalid_window_range"})

    tag_list, tag_error = tags.parse_tags(payload.get("tags"))
    if tag_error:
        return json_response(400, tag_error)

    rows = parse_bulk_text(text)
    if not rows:
        return json_response(400, {"error": "no_rows"})
    if len(rows) > MAX_BULK_ROWS:
        return json_response(400, {"error": "too_many_rows", "max_rows": MAX_BULK_ROWS, "row_count": len(rows)})

    # docs/plans/derived-link-indexes.md: all_links was only ever a
    # pre-flagging optimization here — the real collision check is the
    # get_many confirmation pass ~15 lines below, which re-reads every
    # candidate slug's record directly. Seeding this from the index bought
    # nothing but a chance to trust a key that can drift; every submitted
    # slug still gets checked against the record itself.
    existing: set[str] = set()
    policy = await urlpolicy.load_policy(store)
    row_errors = validate_bulk_rows(rows, existing, principal.has_permission("links.create_custom_slug"), policy)

    # Index-drift confirmation: `all_links` is an index, not the truth. If it
    # has ever drifted (an interrupted write, a KV-explorer edit), trusting it
    # alone for an explicit slug could overwrite a live link record, which is
    # data loss. This is the one place the design deliberately spends N KV
    # reads instead of one — collapsed into a single get_many host call
    # (docs/plans/batch-kv-reads.md) rather than up to MAX_BULK_ROWS
    # sequential `exists` probes, existence tested as `is not None`.
    already_flagged = {(err["line"]) for err in row_errors}
    candidate_rows = [row for row in rows if row.line not in already_flagged and row.slug]
    if candidate_rows:
        existing_values = await get_many(store, [f"slug:{row.slug}" for row in candidate_rows])
        for row in candidate_rows:
            if existing_values.get(f"slug:{row.slug}") is not None:
                row_errors.append({"line": row.line, "slug": row.slug, "error": "slug_taken"})

    if row_errors:
        row_errors.sort(key=lambda e: e["line"])
        return json_response(400, {
            "error": "bulk_validation_failed",
            "row_errors": row_errors,
            "row_count": len(rows),
        })

    password_hash = auth.hash_password(password) if password else None

    taken = set(existing)
    assigned: list[tuple[BulkRow, str, bool]] = []
    for row in rows:
        if row.slug:
            taken.add(row.slug)
            assigned.append((row, row.slug, True))
    for row in rows:
        if not row.slug:
            slug = await links.allocate_random_slug(store, taken)
            assigned.append((row, slug, False))

    now = iso_now()
    created_records = []
    not_created = []
    write_failure = None  # (exc, line, slug) of whichever write first failed
    for idx, (row, slug, custom) in enumerate(assigned):
        record = {
            "slug": slug,
            "target_url": row.target_url,
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
        try:
            await write(lambda s=slug, r=record: store.set(f"slug:{s}", json.dumps(r).encode("utf-8")))
        except kvretry.WriteFailed as exc:
            # THE LOAD-BEARING LINE (docs/plans/write-throttle-resilience.md):
            # abandon the rest rather than keep hammering a throttled store,
            # then index EXACTLY what landed below. This is what makes a
            # throttled bulk create produce zero unindexed_link findings,
            # instead of retrying harder into the same cap.
            write_failure = (exc, row.line, slug)
            not_created = [
                {"line": r.line, "slug": s, "error": "write_failed"}
                for r, s, _ in assigned[idx:]
            ]
            break
        created_records.append(record)

    # docs/plans/derived-link-indexes.md, Stage 2: no index write here any
    # more — every record that landed is already listed, derived from the
    # slug: key enumeration. A partial run's next step is always "resubmit":
    # there is no index for a repair to fix.
    if write_failure is None:
        return json_response(201, {
            "count": len(created_records),
            "links": [links.public_link(r) for r in created_records],
        })

    exc, _, _ = write_failure
    return json_response(200, {
        "ok": False,
        "partial": True,
        "count": len(created_records),
        "links": [links.public_link(r) for r in created_records],
        "not_created": not_created,
        "write_error": kvretry.classify_write_error(exc.cause),
        "next_step": "resubmit",
        "row_count": len(rows),
    })


async def handle_bulk_action(store, users_store, principal, request, get_many, write):
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return json_response(400, {"error": "invalid_json"})

    action = payload.get("action")
    if action not in BULK_ACTIONS:
        return json_response(400, {"error": "invalid_action"})

    slugs = payload.get("slugs")
    if not isinstance(slugs, list) or not slugs or any(not isinstance(s, str) for s in slugs):
        return json_response(400, {"error": "no_slugs"})

    if len(set(slugs)) != len(slugs):
        return json_response(400, {"error": "duplicate_slug"})

    if len(slugs) > MAX_BULK_ROWS:
        return json_response(400, {"error": "too_many_rows", "max_rows": MAX_BULK_ROWS, "row_count": len(slugs)})

    new_owner: str | None = None
    if action == "reassign":
        # Permission BEFORE resolving the owner, deliberately: the reverse
        # order lets a caller without users.manage tell "no such user"
        # (400 unknown_owner) from "user exists" (403 forbidden) and so
        # enumerate the very username list GET /api/users gates on this
        # same permission.
        if not principal.has_permission("users.manage"):
            return json_response(403, {"error": "forbidden", "required_permission": "users.manage"})
        new_owner = payload.get("owner")
        if not isinstance(new_owner, str) or not new_owner:
            return json_response(400, {"error": "invalid_owner"})
        if await auth.get_user(users_store, new_owner) is None:
            return json_response(400, {"error": "unknown_owner", "owner": new_owner})

    tag_list: list[str] | None = None
    if action in ("tag", "untag"):
        if not principal.has_permission("links.tag"):
            return json_response(403, {"error": "forbidden", "required_permission": "links.tag"})
        tag_list, tag_error = tags.parse_tags(payload.get("tags"), allow_none=False)
        if tag_error:
            return json_response(400, tag_error)
        if not tag_list:
            return json_response(400, {"error": "no_tags"})

    new_target_url: str | None = None
    if action == "repoint":
        new_target_url = payload.get("target_url")
        # Same choke point as handle_create/handle_update/validate_bulk_rows.
        # This is the FOURTH authoring path; skipping either check here is a
        # policy bypass, not a shortcut. See docs/plans/bulk-schedule-and-repoint.md.
        url_error = links.target_url_error(new_target_url)
        if url_error:
            return json_response(400, links.target_url_error_body(url_error))
        policy = await urlpolicy.load_policy(store)
        verdict = urlpolicy.evaluate(new_target_url, policy)
        if not verdict["allowed"]:
            return json_response(400, {
                "error": "destination_not_allowed",
                "host": verdict["host"],
                "reason": verdict["reason"],
                "matched_rule": verdict["matched_rule"],
            })

    has_start = has_end = False
    new_start_at: str | None = None
    new_end_at: str | None = None
    planned_windows: dict[str, tuple[str | None, str | None]] = {}
    if action == "schedule":
        has_start = "start_at" in payload
        has_end = "end_at" in payload
        if not has_start and not has_end:
            return json_response(400, {"error": "no_window_fields"})
        if has_start:
            new_start_at, invalid = links.parse_window_field(payload["start_at"])
            if invalid:
                return json_response(400, {"error": "invalid_start_at"})
        if has_end:
            new_end_at, invalid = links.parse_window_field(payload["end_at"])
            if invalid:
                return json_response(400, {"error": "invalid_end_at"})

    # One get_many host call over every slug's record (docs/plans/batch-kv-reads.md)
    # rather than a sequential links.get_link per slug — up to MAX_BULK_ROWS
    # (50) round trips collapsed into a handful of chunked calls.
    fetched = await get_many(store, [f"slug:{slug}" for slug in slugs])

    row_errors = []
    records: dict[str, dict] = {}
    for slug in slugs:
        raw = fetched.get(f"slug:{slug}")
        if raw is None:
            row_errors.append({"slug": slug, "error": "not_found"})
            continue
        record = json.loads(raw)
        # Reassignment deliberately skips the per-row can_edit check — it is
        # gated on users.manage alone (see docs/plans/link-tags-and-ownership.md,
        # "Trade-offs" #7). Requiring can_edit here would break the departed-
        # employee case this feature exists for, and buys no real security
        # since a users.manage holder can already self-promote to admin.
        if action != "reassign" and not links.can_edit(principal, record):
            row_errors.append({"slug": slug, "error": "forbidden"})
            continue
        records[slug] = record

    if action == "tag":
        for slug, record in records.items():
            if len(tags.apply_tags(record.get("tags", []), tag_list)) > tags.MAX_TAGS_PER_LINK:
                row_errors.append({"slug": slug, "error": "too_many_tags", "max_tags": tags.MAX_TAGS_PER_LINK})

    if action == "schedule":
        for slug, record in records.items():
            merged_start = new_start_at if has_start else record.get("start_at")
            merged_end = new_end_at if has_end else record.get("end_at")
            if merged_start is not None and merged_end is not None and merged_start >= merged_end:
                row_errors.append({
                    "slug": slug,
                    "error": "invalid_window_range",
                    "start_at": merged_start,
                    "end_at": merged_end,
                })
                continue
            planned_windows[slug] = (merged_start, merged_end)

    if row_errors:
        return json_response(400, {"error": "bulk_validation_failed", "row_errors": row_errors})

    applied: list[str] = []
    write_failure = None  # (exc,) of whichever write first failed

    # docs/plans/derived-link-indexes.md, Stage 2: none of these eight branches
    # has an index step any more — there is no index. A record's existence is
    # the only truth, so every interruption point below leaves exactly the
    # records that landed, all of them listed, none of them advertised-but-
    # missing.
    if action in ACTION_STATUSES:
        new_status = ACTION_STATUSES[action]
        now = iso_now()
        for slug, record in records.items():
            record["status"] = new_status
            record["updated_at"] = now
            try:
                await write(lambda s=slug, r=record: store.set(f"slug:{s}", json.dumps(r).encode("utf-8")))
            except kvretry.WriteFailed as exc:
                write_failure = (exc,)
                break
            applied.append(slug)
    elif action in ("tag", "untag"):
        now = iso_now()
        for slug, record in records.items():
            if action == "tag":
                record["tags"] = tags.apply_tags(record.get("tags", []), tag_list)
            else:
                record["tags"] = tags.remove_tags(record.get("tags", []), tag_list)
            record["updated_at"] = now
            try:
                await write(lambda s=slug, r=record: store.set(f"slug:{s}", json.dumps(r).encode("utf-8")))
            except kvretry.WriteFailed as exc:
                write_failure = (exc,)
                break
            applied.append(slug)
    elif action == "reassign":
        # A pure record rewrite now — there is no owner index to move slugs
        # between any more.
        now = iso_now()
        for slug, record in records.items():
            record["owner"] = new_owner
            record["updated_at"] = now
            try:
                await write(lambda s=slug, r=record: store.set(f"slug:{s}", json.dumps(r).encode("utf-8")))
            except kvretry.WriteFailed as exc:
                write_failure = (exc,)
                break
            applied.append(slug)
    elif action == "repoint":
        now = iso_now()
        for slug, record in records.items():
            record["target_url"] = new_target_url
            record["updated_at"] = now
            try:
                await write(lambda s=slug, r=record: store.set(f"slug:{s}", json.dumps(r).encode("utf-8")))
            except kvretry.WriteFailed as exc:
                write_failure = (exc,)
                break
            applied.append(slug)
    elif action == "schedule":
        now = iso_now()
        for slug, record in records.items():
            record["start_at"], record["end_at"] = planned_windows[slug]
            record["updated_at"] = now
            try:
                await write(lambda s=slug, r=record: store.set(f"slug:{s}", json.dumps(r).encode("utf-8")))
            except kvretry.WriteFailed as exc:
                write_failure = (exc,)
                break
            applied.append(slug)
    elif action == "delete":
        for slug in records:
            try:
                await write(lambda s=slug: store.delete(f"slug:{s}"))
            except kvretry.WriteFailed as exc:
                write_failure = (exc,)
                break
            applied.append(slug)
    else:  # pragma: no cover - guarded by the BULK_ACTIONS check above
        # A name added to BULK_ACTIONS with no matching write branch used to
        # fall into "delete" as the implicit catch-all — the failure mode of
        # a half-finished action was deleting every selected link. This is
        # the guard: an unhandled action name is a clean 500, never a delete.
        return json_response(500, {"error": "unhandled_action", "action": action})

    if write_failure is None:
        result = {"ok": True, "action": action, "count": len(slugs)}
        if action in ("tag", "untag"):
            result["tags"] = tag_list
        elif action == "reassign":
            result["owner"] = new_owner
        elif action == "repoint":
            result["target_url"] = new_target_url
        elif action == "schedule":
            if has_start:
                result["start_at"] = new_start_at
            if has_end:
                result["end_at"] = new_end_at
        return json_response(200, result)

    (exc,) = write_failure
    not_applied = [s for s in slugs if s not in applied]
    result = {
        "ok": False,
        "partial": True,
        "action": action,
        "count": len(applied),
        "applied": applied,
        "not_applied": not_applied,
        "write_error": kvretry.classify_write_error(exc.cause),
        "next_step": "resubmit",
    }
    if action in ("tag", "untag"):
        result["tags"] = tag_list
    elif action == "reassign":
        result["owner"] = new_owner
    elif action == "repoint":
        result["target_url"] = new_target_url
    elif action == "schedule":
        if has_start:
            result["start_at"] = new_start_at
        if has_end:
            result["end_at"] = new_end_at
    return json_response(200, result)
