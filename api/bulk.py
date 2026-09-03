"""Bulk link management: a pure text parser/row validator (this module) plus
the two /api/links/bulk* handlers built on top of them.

Zero `spin_sdk` imports — `store`/`request` arrive as plain parameters and
`Request`/`Response` come from `responses`, matching the testability rule the
rest of `api/` follows (see `CLAUDE.md`).
"""

import json
from dataclasses import dataclass, field
from typing import Callable

import auth
import domains
import kvretry
import links
import tags
import urlpolicy
from responses import iso_now, json_response

MAX_BULK_ROWS = 50
MAX_BULK_BODY_BYTES = 262_144

ACTION_STATUSES = {"enable": "active", "disable": "disabled"}  # values ⊆ links.LINK_STATUSES

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


async def handle_bulk_create(store, principal, request, configured_domains, get_many, write):
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

    # Batch-level, applied to every link created in this submission — no
    # per-row allowed_domains, matching the batch-level password/start_at/
    # end_at precedent. No also_allowed: a brand-new record has no prior
    # value to retain (docs/plans/per-link-domain-restriction.md).
    allowed_domains_result, allowed_domains_error = domains.normalize_allowed_domains(
        payload.get("allowed_domains"), configured_domains
    )
    if allowed_domains_error:
        return json_response(400, {"error": allowed_domains_error})

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
            "allowed_domains": allowed_domains_result,
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


@dataclass(frozen=True)
class PlannedMutation:
    """One slug's write, decided BEFORE any write happens. `kind` is "set"
    (write `record` back to slug:<slug>) or "delete" (remove the record).

    Planning is pure and KV-free; `_apply_mutations` is the only thing that
    writes, and it never sees the action name."""

    slug: str
    kind: str            # "set" | "delete"
    record: dict | None  # the full record for "set"; None for "delete"


@dataclass(frozen=True)
class ActionContext:
    """Everything the request-validation phase computed, handed to the
    per-action planner. Frozen: a planner must not smuggle state back into
    validation."""

    action: str
    now: str
    tag_list: list[str] | None = None
    new_owner: str | None = None
    new_target_url: str | None = None
    planned_windows: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)
    has_start: bool = False
    has_end: bool = False
    new_start_at: str | None = None
    new_end_at: str | None = None
    new_allowed_domains: list[str] | None = None


def _plan_status(ctx, slug, record):
    record["status"] = ACTION_STATUSES[ctx.action]
    record["updated_at"] = ctx.now
    return PlannedMutation(slug, "set", record)


def _plan_tag(ctx, slug, record):
    record["tags"] = tags.apply_tags(record.get("tags", []), ctx.tag_list)
    record["updated_at"] = ctx.now
    return PlannedMutation(slug, "set", record)


def _plan_untag(ctx, slug, record):
    record["tags"] = tags.remove_tags(record.get("tags", []), ctx.tag_list)
    record["updated_at"] = ctx.now
    return PlannedMutation(slug, "set", record)


def _plan_reassign(ctx, slug, record):
    record["owner"] = ctx.new_owner
    record["updated_at"] = ctx.now
    return PlannedMutation(slug, "set", record)


def _plan_repoint(ctx, slug, record):
    record["target_url"] = ctx.new_target_url
    record["updated_at"] = ctx.now
    return PlannedMutation(slug, "set", record)


def _plan_schedule(ctx, slug, record):
    # The merged pair the validation loop already computed — NEVER recomputed
    # here. One side may come from the stored record, so recomputing is how a
    # slug gets validated against one pair and written with another.
    record["start_at"], record["end_at"] = ctx.planned_windows[slug]
    record["updated_at"] = ctx.now
    return PlannedMutation(slug, "set", record)


def _plan_restrict(ctx, slug, record):
    record["allowed_domains"] = ctx.new_allowed_domains
    record["updated_at"] = ctx.now
    return PlannedMutation(slug, "set", record)


def _plan_delete(ctx, slug, record):
    # No analytics purge, deliberately — CLAUDE.md's "Orphaned analytics purge":
    # 50 slugs x ~95 keys is ~95-123 s against a 30 s handler limit.
    # handle_bulk_action takes no purge_analytics callable at all, so this
    # cannot regress by accident.
    return PlannedMutation(slug, "delete", None)


def _no_extra_fields(ctx):
    return {}


def _tag_fields(ctx):
    return {"tags": ctx.tag_list}


def _owner_fields(ctx):
    return {"owner": ctx.new_owner}


def _target_url_fields(ctx):
    return {"target_url": ctx.new_target_url}


def _window_fields(ctx):
    # Echo only the sides the caller actually sent, exactly as today.
    fields = {}
    if ctx.has_start:
        fields["start_at"] = ctx.new_start_at
    if ctx.has_end:
        fields["end_at"] = ctx.new_end_at
    return fields


def _domain_fields(ctx):
    return {"allowed_domains": ctx.new_allowed_domains}


@dataclass(frozen=True)
class ActionSpec:
    """Everything `handle_bulk_action` needs to know about one action name.

    `BULK_ACTIONS` is DERIVED from `ACTION_SPECS` below, so a name cannot be
    accepted by the endpoint without also carrying a `plan`. The write dispatch
    a half-finished action used to fall through into no longer exists — see
    docs/plans/unify-bulk-write-loops.md."""

    name: str
    plan: Callable[[ActionContext, str, dict], PlannedMutation]
    per_row_can_edit: bool = True
    required_permission: str | None = None
    result_fields: Callable[[ActionContext], dict] = _no_extra_fields


ACTION_SPECS: dict[str, ActionSpec] = {
    "delete":   ActionSpec("delete", _plan_delete),
    "enable":   ActionSpec("enable", _plan_status),
    "disable":  ActionSpec("disable", _plan_status),
    "tag":      ActionSpec("tag", _plan_tag,
                           required_permission="links.tag", result_fields=_tag_fields),
    "untag":    ActionSpec("untag", _plan_untag,
                           required_permission="links.tag", result_fields=_tag_fields),
    # Reassignment deliberately skips the per-row can_edit check — it is gated
    # on users.manage alone (see docs/plans/link-tags-and-ownership.md,
    # "Trade-offs" #7). Requiring can_edit here would break the departed-
    # employee case this feature exists for, and buys no real security since a
    # users.manage holder can already self-promote to admin.
    "reassign": ActionSpec("reassign", _plan_reassign, per_row_can_edit=False,
                           required_permission="users.manage", result_fields=_owner_fields),
    "repoint":  ActionSpec("repoint", _plan_repoint, result_fields=_target_url_fields),
    "schedule": ActionSpec("schedule", _plan_schedule, result_fields=_window_fields),
    # No new permission (docs/plans/per-link-domain-restriction.md, Trade-offs
    # #5): restricting is strictly LESS dangerous than disable/repoint, both
    # gated on per-row can_edit alone, so a links.restrict_domains permission
    # would give the least dangerous of the three the strongest gate.
    "restrict": ActionSpec("restrict", _plan_restrict, result_fields=_domain_fields),
}

# DERIVED, never a literal. This is the structural fix: a name cannot reach the
# endpoint's write path without an ActionSpec, and an ActionSpec cannot exist
# without a `plan`. The `unhandled_action` 500 below survives only as a guard
# against the two ever being decoupled again.
BULK_ACTIONS = frozenset(ACTION_SPECS)


async def _apply_mutations(store, write, mutations):
    """THE one write loop. Every bulk action's writes go through here, and it
    has NO `action` parameter — a new action cannot acquire a write loop of its
    own, correct or otherwise.

    Best-effort and fully reported (docs/plans/write-throttle-resilience.md):
    on the first write whose RECORD_WRITE budget is exhausted it abandons the
    rest rather than hammering a throttled store, and returns exactly what
    landed. Returns (applied_slugs_in_request_order, WriteFailed | None).
    """
    applied: list[str] = []
    for plan in mutations:
        try:
            if plan.kind == "delete":
                await write(lambda s=plan.slug: store.delete(f"slug:{s}"))
            else:
                await write(lambda s=plan.slug, r=plan.record: store.set(f"slug:{s}", json.dumps(r).encode("utf-8")))
        except kvretry.WriteFailed as exc:
            return applied, exc
        applied.append(plan.slug)
    return applied, None


async def handle_bulk_action(store, users_store, principal, request, configured_domains, get_many, write):
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return json_response(400, {"error": "invalid_json"})

    action = payload.get("action")
    if action not in BULK_ACTIONS:
        return json_response(400, {"error": "invalid_action"})

    spec = ACTION_SPECS.get(action)
    if spec is None:  # pragma: no cover - BULK_ACTIONS is derived from ACTION_SPECS
        # Unreachable by construction. Kept as the last line of defence if the
        # two are ever decoupled: a name with no spec must be a clean 500,
        # never someone else's write loop.
        return json_response(500, {"error": "unhandled_action", "action": action})

    slugs = payload.get("slugs")
    if not isinstance(slugs, list) or not slugs or any(not isinstance(s, str) for s in slugs):
        return json_response(400, {"error": "no_slugs"})

    if len(set(slugs)) != len(slugs):
        return json_response(400, {"error": "duplicate_slug"})

    if len(slugs) > MAX_BULK_ROWS:
        return json_response(400, {"error": "too_many_rows", "max_rows": MAX_BULK_ROWS, "row_count": len(slugs)})

    # Permission BEFORE any payload-derived lookup, deliberately: the reverse
    # order lets a caller without users.manage tell "no such user"
    # (400 unknown_owner) from "user exists" (403 forbidden) and so enumerate
    # the very username list GET /api/users gates on this same permission.
    if spec.required_permission and not principal.has_permission(spec.required_permission):
        return json_response(403, {"error": "forbidden", "required_permission": spec.required_permission})

    new_owner: str | None = None
    if action == "reassign":
        new_owner = payload.get("owner")
        if not isinstance(new_owner, str) or not new_owner:
            return json_response(400, {"error": "invalid_owner"})
        if await auth.get_user(users_store, new_owner) is None:
            return json_response(400, {"error": "unknown_owner", "owner": new_owner})

    tag_list: list[str] | None = None
    if action in ("tag", "untag"):
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

    new_allowed_domains: list[str] | None = None
    if action == "restrict":
        allowed_domains_result, allowed_domains_error = domains.normalize_allowed_domains(
            payload.get("allowed_domains"), configured_domains
        )
        if allowed_domains_error:
            return json_response(400, {"error": allowed_domains_error})
        new_allowed_domains = allowed_domains_result

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
        if spec.per_row_can_edit and not links.can_edit(principal, record):
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

    ctx = ActionContext(
        action=action,
        now=iso_now(),
        tag_list=tag_list,
        new_owner=new_owner,
        new_target_url=new_target_url,
        planned_windows=planned_windows,
        has_start=has_start,
        has_end=has_end,
        new_start_at=new_start_at,
        new_end_at=new_end_at,
        new_allowed_domains=new_allowed_domains,
    )

    # Planning is pure and write-free: every slug's mutation is decided before
    # the first KV write happens. docs/plans/derived-link-indexes.md, Stage 2:
    # there is no index step, so a record's existence is the only truth and any
    # interruption inside _apply_mutations leaves exactly the records that
    # landed, all of them listed, none advertised-but-missing.
    mutations = [spec.plan(ctx, slug, record) for slug, record in records.items()]
    applied, exc = await _apply_mutations(store, write, mutations)

    # ONE source for the per-action echo fields, merged into BOTH bodies, so the
    # success and partial responses can never drift apart.
    extra = spec.result_fields(ctx)

    if exc is None:
        return json_response(200, {"ok": True, "action": action, "count": len(slugs), **extra})

    return json_response(200, {
        "ok": False,
        "partial": True,
        "action": action,
        "count": len(applied),
        "applied": applied,
        "not_applied": [s for s in slugs if s not in applied],
        "write_error": kvretry.classify_write_error(exc.cause),
        "next_step": "resubmit",
        **extra,
    })
