# Bulk Schedule and Repoint

## Context

The dashboard has had row selection and a bulk action bar since
`docs/plans/bulk-link-management.md` (2026-08-01), and six bulk actions have
accumulated on it since: `delete`, `enable`, `disable`, `tag`, `untag`,
`reassign`. Two obvious actions were named as explicit non-goals in that plan
and parked in `TASKS.md`'s Future work as
*"Bulk editing of existing links — destination, schedule, and slug — plus undo
for bulk delete"*: setting a start/end window on N selected links, and
repointing N selected links at a new destination. Both are now in scope. The
other two ideas bundled into that one entry are not (see Out of scope).

What is inadequate today: changing the window or the destination on N links
means N passes through the row-level edit form — open the row, edit two
fields, save, wait for `loadLinks()`, repeat. For the audience this product is
built for (marketing staff correcting a campaign, per `PRODUCT.md`), the two
most common post-creation edits are exactly "this campaign now ends Friday"
and "the landing page moved". A 12-link campaign is 12 manual edits for a
single decision.

**One correction to the Future-work entry, made in passing.** It justifies
these two actions with *"both are `handle_bulk_action` variants that need no
new index writes, since neither touches `all_links`"*. That clause is stale in
our favour: `links:all_links` and `links:owner_links:<owner>` were deleted
outright by `docs/plans/derived-link-indexes.md`'s Stage 2. There is no index
to touch, avoid or keep in step — **both actions are pure `slug:<slug>` record
rewrites, exactly like the shipped `reassign` action, and every interruption
point leaves precisely the records that landed, all of them listed.** The
entry's reasoning survives; its mechanism no longer exists.

Confirmed decisions (settled by the user before planning):

- **Exactly two actions**, both as variants on the existing
  `POST /api/links/bulk-action`. No new endpoint, no new route, no `spin.toml`
  change.
- **Bulk slug editing is out**, permanently — it means deleting one KV record
  and writing another, and silently breaking every QR code and printed link
  already in the wild.
- **Undo for bulk delete is out** — it needs a tombstone record and a
  retention policy; the existing count-bearing confirmation dialog is the
  accepted mitigation.
- No existing action's behaviour or response shape changes.
- No deploy. Local verification only; deploys are the user's call.
- Per-row `can_edit` applies, as it does for every action except `reassign`.
  `reassign`'s deliberate skip is not copied.
- `redirect` is untouched.

## Key technical facts confirmed during research

- **Destination validation lives in exactly three places today, and `repoint`
  becomes the fourth.** `links.handle_create` (`api/links.py:221-226`),
  `links.handle_update` (`api/links.py:385-389`) and
  `bulk.validate_bulk_rows` (`api/bulk.py:145-148`, with the policy loaded at
  `api/bulk.py:227`) each call `links.target_url_error` then
  `urlpolicy.evaluate`. Read directly in all three files.
- **`links.target_url_error` (`api/links.py:152`) is the shared choke point**
  for scheme (`http`/`https` + non-empty netloc) and the 4096-**byte**
  `MAX_TARGET_URL_BYTES` cap (`api/links.py:136`). Its docstring states the
  rule this plan has to satisfy: *"a constraint enforced in two of three
  places is not enforced."*
- **`links._target_url_error_body` (`api/links.py:144`) is module-private and
  is what echoes `max_bytes` back to the client.** Both single-link call sites
  use it (`:223`, `:387`); `bulk.validate_bulk_rows` does not, so a bulk-create
  row error for an over-long URL carries only the code. Repoint's error is
  request-level (one URL for all N links), so it must echo the cap the way
  the single paths do — which needs this function public. Confirmed private by
  `grep -n "_target_url_error_body" api/` returning exactly three lines, all in
  `links.py`.
- **`api/tests/test_url_policy_enforcement.py` exists specifically to prove
  all three paths reject the same destination and write nothing** — 5 tests, a
  `POLICY_CONFIGS` parametrization over both ways to express a block
  (default-allow + deny rule, default-deny + allow rule), and a docstring
  recording a mutation run per path. This module must gain a fourth path.
- **`handle_bulk_action`'s final write branch is a catch-all `else: # delete`
  (`api/bulk.py:446`).** This is the single most dangerous fact in this plan:
  `BULK_ACTIONS` (`api/bulk.py:27`) is validated at `:330`, so **adding an
  action name to that set without adding a matching write branch silently
  routes it into the delete loop.** An operator asking to reschedule 50 links
  would delete them. Read directly; `BULK_ACTIONS` is referenced nowhere else
  in the repo (`grep -rn "BULK_ACTIONS" api gui gui-pages docs` → `bulk.py:27`,
  `bulk.py:330`, plus prose in `docs/plans/link-tags-and-ownership.md`).
- **`handle_update` merges each candidate window side with the record's
  existing value before validating** (`api/links.py:408-424`): `merged_start`
  defaults to `record.get("start_at")`, is overwritten only when
  `"start_at" in payload`, and the `merged_start >= merged_end` check runs on
  the merged pair. So `PATCH {"end_at": <earlier than stored start_at>}` is
  correctly rejected today.
- **Window comparison by plain string `>=` is correct**, not a shortcut:
  `responses.to_iso8601_utc` is `strftime("%Y-%m-%dT%H:%M:%SZ")`
  (`api/responses.py:80-81`) — fixed width, always `Z`, so lexicographic order
  is chronological order. Every stored `start_at`/`end_at` went through
  `links.parse_window_field` → `to_iso8601_utc`. A hand-edited record could
  hold a non-normalized string; `handle_update` carries the identical exposure
  today, so this adds no new risk.
- **`PATCH` semantics for a window side are key-presence based**: an absent
  key leaves the stored value untouched, an explicit `null` clears it
  (`parse_window_field(None)` → `(None, False)`, i.e. valid-and-unset,
  `api/links.py:172-181`). This is the semantics the bulk action mirrors.
- **`handle_bulk_action` already has a per-row validation precedent whose
  verdict depends on the record's existing state**: the `action == "tag"`
  block at `api/bulk.py:391-394` appends a per-row `too_many_tags` error
  computed from `record.get("tags", [])`, and `if row_errors:` at `:396`
  returns `400 bulk_validation_failed` having written nothing. Per-link
  schedule validation is the same shape.
- **No signature change is needed anywhere.**
  `bulk.handle_bulk_action(store, users_store, principal, request, get_many, write)`
  already receives everything both actions need; `urlpolicy.load_policy(store)`
  reads `_meta:url_policy` from the same links view `handle_bulk_create`
  already loads it from. `api/app.py:339-343` therefore needs **no edit**.
- **Writes already go through the `kvretry` seam and abandon on exhaustion.**
  Every branch is `await write(lambda ...: store.set(...))` inside
  `try/except kvretry.WriteFailed`, breaking the loop and reporting
  `200 {"ok": false, "partial": true, applied, not_applied, write_error,
  next_step: "resubmit"}` (`api/bulk.py:399-483`). `records` is a dict built by
  iterating `slugs`, so insertion order — and therefore `applied` order —
  matches the request.
- **`redirect` re-reads the record on every request**, so a rescheduled or
  repointed link takes effect on the next click with no cache story:
  `linkgate.Resolve` (`redirect/linkgate/resolve.go:114`) calls
  `IsWithinWindow(l.StartAt, l.EndAt, now)` per request, and every response
  carries `Cache-Control: no-store` with a 302 (never 301/308) per CLAUDE.md's
  "Redirect caching" section. `linkgate` needs no change: neither action adds
  or renames a record field.
- **No new KV key type**, so none of the three obligations a new key imposes
  (`backup.py`'s `INDEX_KEYS`/`restore_write_order`, `consistency.py`'s
  key-shape recognition, `kvprefix.STORE_PREFIXES`) applies. Both actions
  rewrite an existing `links:slug:<slug>` value.
- **GUI shape of the bulk bar, read from source:** `#bulk-bar`
  (`gui/dashboard.html:150-171`) is a `flex-wrap` row
  (`gui/dashboard.css:196-207`) holding a count, a `role="group"` of
  Enable/Disable/Delete, a `<span id="bulk-tag-controls">` (a span, not a
  group — `gui/dashboard.css:305-316` records why Pico's group styling can't
  wrap a `.tag-input`), and a `<div id="bulk-owner-controls" role="group">`
  holding a `<select>` + one button. `updateBulkBar()`
  (`gui/dashboard.js:423-444`) disables a hardcoded list of six button ids past
  `BULK_MAX_SELECTION = 50`.
- **Shared GUI helpers that get reused rather than rewritten:**
  `confirmDialog(message, {confirmLabel})` (`gui/app.js:107`, escapes both
  strings), `datetimeLocalToIso` (`gui/app.js:63`), `formatTimestamp`
  (`gui/app.js:267`), `friendlyError`/`ERROR_MESSAGES` (`gui/app.js:150-190`),
  `wireWindowValidation` (`gui/dashboard.js:50`), `narrowSelectionTo`
  (`gui/dashboard.js:479`), `renderRowErrorList` (`gui/dashboard.js:1079`),
  `api.post` (`gui/app.js:53`), and `.visually-hidden` (`gui/theme.css:794`).
- **Baselines, run before planning:** `cd api && uv run pytest` → **679
  passed**; `cd gui-pages && uv run pytest` → **108 passed**;
  `cd redirect && go test ./linkgate/...` → `ok`. `go test ./...` was not run
  — it fails by design.
- **UNCONFIRMED: the bulk bar's wrapped height at 1400/768/480/390 px with two
  more clusters in it.** No browser was available during planning. Nine
  buttons plus three inputs will wrap to more rows than today's six-plus-one.
  Confirming it requires a real `spin up` run and a measurement in both
  themes; the fallback if it measures badly is named under Out of scope.
- **UNCONFIRMED: whether Pico gives the `<label>` and `datetime-local`
  elements a bottom margin that misaligns them inside the flex bar.** The
  precedent says assume yes and neutralise it — `.tag-input-buffer` carries
  `width: auto` + `margin-bottom: 0` for exactly this
  (`gui/dashboard.css:267-274`), and `#links-table .select-cell
  input[type=checkbox] { margin: 0 }` was added after a `getComputedStyle`
  check, "not by eye" (`gui/dashboard.css:218-224`). Verify with
  `getComputedStyle`, not by eye.

## Data model

**Unchanged.** No new field, no new key, no new prefix. `repoint` writes
`target_url`; `schedule` writes `start_at`/`end_at`; both bump `updated_at`.
All three are existing fields on the `slug:<slug>` record, and all three are
already writable through `PATCH /api/links/{slug}`.

## API changes — `api/bulk.py`

### 0. Prerequisite: make the target-URL error body public

Rename `links._target_url_error_body` → **`links.target_url_error_body`** and
update its two call sites (`api/links.py:223`, `:387`). Response bodies are
byte-identical; the only change is that `bulk.py` can now build a
`target_url_too_long` body that echoes `max_bytes` without restating the cap.
This follows the convention `links.can_view`/`can_edit`/`target_url_error`
already carry — public precisely *because* they are shared across modules.
Add a one-line docstring note saying so, so it does not drift back to private.

**This must land before the repoint branch**, or that branch will either
duplicate the cap or drop `max_bytes` from its error.

### 1. `BULK_ACTIONS` and the catch-all delete branch

```python
BULK_ACTIONS = {"delete", "enable", "disable", "tag", "untag", "reassign", "repoint", "schedule"}
```

**And in the same change, convert the write loop's `else: # delete`
(`api/bulk.py:446`) into `elif action == "delete":`, with a new final**

```python
    else:  # pragma: no cover - guarded by the BULK_ACTIONS check above
        return json_response(500, {"error": "unhandled_action", "action": action})
```

This is not tidying. Today, a name added to `BULK_ACTIONS` with no write branch
falls into the delete loop, so the failure mode of a half-finished action is
**deleting every selected link**. This plan adds two names to that set, which
makes the trap live. `delete`'s own behaviour is unchanged — it keeps the same
loop body, the same response, the same order.

Pin it with a test that cannot pass by accident:

```python
async def test_an_action_in_BULK_ACTIONS_with_no_write_branch_deletes_nothing(monkeypatch):
    monkeypatch.setattr(bulk, "BULK_ACTIONS", bulk.BULK_ACTIONS | {"frobnicate"})
    # ... assert 500 {"error": "unhandled_action"} and dict(store._data) unchanged
```

### 2. `repoint` — the fourth destination-authoring path

Request: `{"slugs": [...], "action": "repoint", "target_url": "https://..."}`.

Request-level validation, placed **after** the existing `tag`/`untag` block
(`api/bulk.py:358-366`) and **before** the `get_many` fetch at `:371`:

```python
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
```

Notes that are load-bearing:

- **Both checks, in this order, or it is a bypass.** A holder of
  `links.edit_all` could otherwise point 50 links anywhere the admin's rules
  forbid — laundered behind the organisation's own short domain, which is the
  entire threat `docs/plans/destination-url-policy.md` exists to answer.
- **The error body is request-level, not a `row_errors` entry**, because the
  URL is one value for the whole request. `matched_rule` is included, matching
  `handle_create`/`handle_update` exactly (bulk *create* omits it in a per-row
  error; this is not a per-row error). The precedent for request-level
  rejection in this same function is `reassign`'s `unknown_owner` and
  `tag`'s `parse_tags` error.
- **The policy `get` is paid only by `repoint`.** The other seven actions do
  not load it, so their KV profile is unchanged: one `get_many` plus N writes.
- Validating before the fetch also means a bad URL costs zero record reads.

Write branch, inserted before the `delete` branch:

```python
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
```

Byte-for-byte the `reassign` branch's shape (`api/bulk.py:433-445`) with one
field swapped. Sequential, never gathered, never batched.

Response echo, in both the success builder (`:455-462`) and the partial builder
(`:465-483`): `result["target_url"] = new_target_url`.

### 3. `schedule` — per-link merge, all-or-nothing

Request: `{"slugs": [...], "action": "schedule", "start_at": ..., "end_at": ...}`,
where **key presence decides what changes**:

| payload | effect on each selected link |
|---|---|
| `{"end_at": "2026-12-01T00:00:00Z"}` | `end_at` set; `start_at` left byte-identical |
| `{"start_at": "...", "end_at": "..."}` | both set |
| `{"end_at": null}` | `end_at` cleared; `start_at` untouched |
| `{"start_at": null, "end_at": null}` | both cleared — link becomes unbounded |
| neither key | `400 {"error": "no_window_fields"}` |

This is `PATCH /api/links/{slug}`'s own semantics, unchanged, applied to N
records. Request-level validation, next to the repoint block:

```python
has_start = has_end = False
new_start_at = new_end_at = None
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
```

**There is deliberately no `new_start_at >= new_end_at` check here.** The
range check cannot be made request-level, because the other side of the window
may come from each record. It runs per link, in the per-row validation block
alongside the existing `too_many_tags` check (`api/bulk.py:391`), after the
`get_many` fetch and after the `not_found`/`can_edit` filter:

```python
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
```

Two details worth being exact about:

- **`planned_windows` is what the write loop consumes** — it does not recompute
  the merge. Validating one value and writing another, derived a second time
  from the same inputs, is a drift a future edit could introduce silently.
- The row error **carries the merged pair**, so a client can say *which* dates
  conflict rather than only that they do. `gui/app.js`'s existing
  `ERROR_MESSAGES.invalid_window_range` ("A link can't expire before it
  starts.") already renders it through `renderRowErrorList` with no GUI change
  required; the extra fields are simply available.

Write branch, before the `delete` branch:

```python
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
```

Response echo (success and partial): only the sides that were provided —
`if has_start: result["start_at"] = new_start_at`, likewise for `end_at`. A
response that echoed an omitted side would read as "this is what I set", which
is false.

### What happens to a link whose window the operator did not intend to touch

Stated plainly, because this is the decision:

1. **An omitted side is never written.** Setting only Expires leaves every
   selected link's Starts byte-identical, whatever it was.
2. **A link whose stored `start_at` conflicts with the new `end_at` blocks the
   whole batch.** It appears in `row_errors` with both merged dates, nothing is
   written for any slug, and the operator's remedy is to deselect it or set
   both sides. This is the cost of faithfulness, and it is the convention this
   codebase already holds: *validation is all-or-nothing; execution is
   best-effort and fully reported.* A skip-and-continue design would be the
   only place in the API where a validation failure produced a partial write.
3. **Explicitly clearing both sides does clobber, on purpose.** It is the only
   way to express "these links are unbounded again", it is reachable only via
   an explicit `null` (never by leaving a field blank in the GUI — see below),
   and the GUI names the consequence in the confirmation dialog.

### KV cost per request

| action | reads | writes |
|---|---|---|
| `schedule` | 1 `get_many` over N keys | N `set` |
| `repoint` | 1 `get_many` over N keys + 1 `get` (`_meta:url_policy`) | N `set` |

At the shared `MAX_BULK_ROWS = 50` cap that is 50 sequential writes in one
request, sitting on Akamai's 50-writes/second app-wide cap — the same shape
`delete`/`enable`/`disable`/`tag`/`reassign` already have, covered by the same
`kvretry` retry plus `partial: true` reporting. Nothing new is being asked of
the write budget, and no cap changes.

## GUI changes

### `gui/dashboard.html` — two new clusters inside `#bulk-bar`

Placed **after** the Enable/Disable/Delete `role="group"` and **before**
`#bulk-tag-controls`, so the always-available controls stay contiguous and the
two permission-gated clusters (`links.tag`, `users.manage`) stay at the end:

```html
<span id="bulk-schedule-controls">
  <label for="bulk-schedule-start">Starts</label>
  <input type="datetime-local" id="bulk-schedule-start" />
  <label for="bulk-schedule-end">Expires</label>
  <input type="datetime-local" id="bulk-schedule-end" />
  <div role="group">
    <button type="button" id="bulk-schedule-set-btn" class="outline">Set schedule</button>
    <button type="button" id="bulk-schedule-clear-btn" class="outline">Clear schedule</button>
  </div>
</span>
<div id="bulk-repoint-controls" role="group">
  <label for="bulk-repoint-url" class="visually-hidden">New destination URL</label>
  <input type="url" id="bulk-repoint-url" placeholder="New destination URL&hellip;" />
  <button type="button" id="bulk-repoint-btn" class="outline">Repoint</button>
</div>
```

- **Neither cluster is permission-gated**, unlike the tag and owner clusters:
  both need only the edit rights the selected links already required, and
  `getSelectableVisibleSlugs()` (`gui/dashboard.js:470`) already restricts the
  checkboxes to `canEditLink` rows. Adding a `hidden` toggle would imply a
  permission that does not exist.
- **The schedule wrapper is a `<span>`, not `role="group"`** — it holds labels
  and an inner button group, and `gui/dashboard.css:305-316` records why Pico's
  group styling must not wrap a whole cluster. **The repoint wrapper *is*
  `role="group"`**, mirroring `#bulk-owner-controls`: one input plus one button
  is exactly the segmented shape Pico's group is for, and it inherits the
  existing `#bulk-bar [role=group] { width: auto }` override for free.
- Labels are **visible** for the two datetime inputs (a bare
  `datetime-local` renders `yyyy-mm-dd --:--` with nothing saying which side it
  is) and `.visually-hidden` for the URL input (its placeholder says it).
- **Zero inline `style=` and zero inline `<script>`**, so
  `gui-pages/tests/test_no_inline_code.py` keeps passing.

### `gui/dashboard.css`

```css
#bulk-schedule-controls {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
}
```

plus neutralising Pico's form-element and label bottom margins inside the bar,
and stopping Pico's `width: 100%` on the three inputs from blowing the flex row
out — the `.tag-input-buffer` rule (`gui/dashboard.css:267-274`) is the exact
precedent, and it also states the house rule for this: verify with
`getComputedStyle`, not by eye. The URL input gets a bounded width
(`width: 18rem; max-width: 100%`) so it never forces `#links-figure` to scroll
at 390 px. No new colour, no new token, no shadow — the bar's existing hairline
border and radius already cover both clusters.

### `gui/dashboard.js`

Three new handlers, each a near-copy of `handleBulkReassign`
(`gui/dashboard.js:1209-1253`) — the same guard, the same
`confirmDialog`, the same clearing of `#links-error`/`#bulk-action-errors`/
`#links-success`, the same `bulk_validation_failed` → `renderRowErrorList`
branch, the same `data.partial` → `loadLinks()` + `narrowSelectionTo(data.not_applied)`
branch, the same success line + `loadLinks()`:

- **`handleBulkSchedule(mode)`**, `mode` being `"set"` or `"clear"`.
  - `"set"`: read `#bulk-schedule-start`/`#bulk-schedule-end`; put **only the
    non-blank sides** into the payload via `datetimeLocalToIso`. Both blank →
    early `return`, matching `handleBulkTag`'s existing
    `if (!tagList.length) return;`. **A blank input means "leave this side
    alone", never "clear it"** — this is the one place the bulk bar
    deliberately diverges from the row edit form, where a blank field clears.
    That divergence is the whole point: a bulk clear must be asked for, not
    achieved by leaving a box empty.
  - `"clear"`: posts `{start_at: null, end_at: null}` explicitly.
  - Confirmation copy, using `formatTimestamp` for the dates:
    - both sides: `Set Starts to <s> and Expires to <e> on N links? Any existing dates on those links are replaced.`
    - one side: `Set Expires to <e> on N links? Their Starts dates are left as they are.` (and the mirror)
    - clear: `Clear the schedule on N links? They become active immediately and never expire.`
    - `confirmLabel`: `Set schedule on N links` / `Clear schedule on N links`.
  - Success: `Rescheduled N links.` / `Cleared the schedule on N links.`
- **`handleBulkRepoint()`** — reads `#bulk-repoint-url`, early-returns when
  blank. Confirmation: `Point N links at "<url>"? Their current destinations
  can't be recovered.`, `confirmLabel: Repoint N links`, with the URL truncated
  to ~80 chars for display (a 4096-byte URL would wreck the 26 rem dialog).
  Success: `Repointed N links.`, and the URL field is cleared on success only —
  the tag input's existing `clearTagChips` on success is the precedent.
- **Both confirm**, and that is a decision, not a default. DESIGN.md's Bulk
  Action Bar rule is "confirm what is not reversible by the adjacent control":
  Enable/Disable and tag/untag skip it because the neighbouring button undoes
  them; reassign confirms because the UI no longer shows each link's *previous*
  owner. Schedule and repoint are in reassign's category — neither Set nor
  Clear nor Repoint can restore N different previous per-link values.

Two edits to existing code, both additive:

- `updateBulkBar()` (`gui/dashboard.js:440-443`): add
  `"bulk-schedule-set-btn"`, `"bulk-schedule-clear-btn"`, `"bulk-repoint-btn"`
  to the over-cap disable list — **nine ids, not six.**
- `wireWindowValidation(document.getElementById("bulk-schedule-start"), document.getElementById("bulk-schedule-end"))`
  next to the two existing calls (`gui/dashboard.js:1523-1524`). It only
  catches the both-fields-present-and-inverted case; the per-link merge cases
  are the server's to report.

One shared-copy addition in `gui/app.js`'s `ERROR_MESSAGES`:
`target_url_too_long`. It is currently unmapped, so it already falls back to a
generic sentence on the create and edit forms; naming it improves those paths
too. Copy: `That destination URL is too long — the limit is 4,096 bytes.`
(with the same "the server is authoritative if this drifts" comment
`BULK_MAX_SELECTION` carries). `no_window_fields` is deliberately **not**
mapped: the GUI early-returns instead of sending it, so a mapped string would
be dead copy; a `curl` caller gets the code.

## Redirect (Go) changes

**None.** Deliberately. Both actions rewrite fields `linkgate.Link` already
parses, on a record `linkgate.Resolve` re-reads on every request, and every
redirect response is already `Cache-Control: no-store` with a 302. A repointed
link serves its new destination on the next click; a rescheduled one starts or
stops resolving at its new boundary. The hot path stays at 5 KV operations.

## Documentation changes (builder tasks, not planner edits)

- **`CLAUDE.md`** — "Bulk link management": add `schedule` and `repoint` to the
  `bulk-action` list, state the key-presence window semantics and that a
  per-link merge conflict fails the whole batch, and record the
  `elif action == "delete"` + `unhandled_action` guard and why it exists.
  "Destination URL policy": **"three authoring paths" becomes four**, naming
  `bulk.handle_bulk_action`'s repoint branch, and the sentence "a policy
  enforced in two of three places is not enforced" updates its arithmetic.
- **`DESIGN.md`** — "Bulk Action Bar": document the two new clusters, that
  neither is permission-gated, why both confirm, and that the over-cap state
  now disables **nine** buttons.
- **`PRODUCT.md`** — the bulk bullet (line 31) gains bulk rescheduling and bulk
  repointing.

## Trade-offs and rejected alternatives

1. **Always set both window sides (the bulk-create panel's semantics) —
   rejected.** Attractive because it sidesteps the per-link merge entirely: one
   validation, one verdict, no dependence on stored state, and it is what the
   bulk-*create* batch controls already do. Lost because on *existing* links it
   silently destroys data the operator never mentioned — "these 12 links expire
   Friday" would wipe the start date off every link that had one, and the
   dashboard would show it only after the reload. The merge costs one loop over
   records already in hand, and the concern that "the validation result then
   differs per link" is already a solved shape here: `too_many_tags` is exactly
   that, and returns `400 bulk_validation_failed` having written nothing.
2. **Skip-and-continue on a per-link window conflict (write the links that
   validate, report the rest) — rejected.** Attractive because one badly-scheduled
   link no longer blocks a 50-link batch. Lost because it inverts the one
   convention every bulk path in this codebase holds — validation is
   all-or-nothing, execution is best-effort — and it would be the only place
   where a *validation* failure produced a partial write, leaving the operator
   diffing what they asked for against what happened. The reported row error
   names the slug and both conflicting dates, which is enough to fix and
   resubmit.
3. **Skipping `urlpolicy.evaluate` on repoint, on the reasoning that the policy
   is an authoring-time control and these links already exist — rejected, and
   this is the most important rejection in the document.** It is genuinely
   tempting: `docs/plans/destination-url-policy.md` says existing violators are
   *reported, never mutated*, so "editing an existing link" can be argued to
   sit outside enforcement. It fails on inspection — `handle_update` already
   enforces the policy on a single-link destination change, so repoint is a
   *bulk edit of the same field on the same records*, and exempting it would
   make the policy trivially bypassable at 50× throughput by anyone with
   `links.edit_all`. The existing-violator carve-out is about links nobody has
   touched since the rule appeared, not about new destinations chosen after it.
   Hence the fourth path, and hence `test_url_policy_enforcement.py` gaining a
   fourth case rather than a note.
4. **Refactoring the six (now eight) near-identical write loops into one shared
   loop — rejected for this change.** Attractive: eight copies of
   try/write/except/break/append is real duplication, and a shared loop would
   make the "new action falls into the delete branch" trap structurally
   impossible rather than merely guarded. Lost because it touches every
   existing action's code path in a change whose stated non-goal is *"do not
   change any existing action's behaviour"* — the risk lands on `delete`, the
   one irreversible action. The `unhandled_action` guard buys most of the
   safety for a fraction of the blast radius. Worth reopening as its own
   change, with its own verification; noted under Future work.
5. **A new `PATCH /api/links/bulk` endpoint instead of two action variants —
   rejected.** Attractive as the more REST-shaped answer: a bulk PATCH taking
   a field map reads better than an `action` discriminator, and would extend to
   future fields for free. Lost on three counts: the user scoped this as
   variants on the existing endpoint; `gui/app.js`'s `api.patch` helper would
   need the same selection/partial/`not_applied` machinery
   `handle_bulk_action` already has, duplicated; and a second bulk endpoint
   with a second set of caps and error shapes is exactly the API-inconsistent-
   with-itself outcome `docs/plans/write-throttle-resilience.md` rejected `207`
   over.
6. **A client-side pre-check of merged windows against `allLinks` before
   submitting — rejected.** Attractive because the dashboard already holds every
   visible record in memory, so it could grey out the offending rows before a
   round trip. Lost because it duplicates the server's merge rule in a second
   language, where the copy can silently drift out of step (and would then be
   *wrong* in the reassuring direction); the existing `wireWindowValidation`
   catches the one case that needs no stored state, and the row-error path
   explains the rest with the actual stored values.
7. **Bulk slug editing — rejected outright** (also recorded under
   `TASKS.md`'s "Considered and rejected"). It is not a record rewrite at all:
   it is a delete plus a create, it strands every QR code and printed asset
   already in circulation, and it needs a redirect-from-old-slug concept this
   product does not have. The Future-work entry that raised it argued against
   it itself.
8. **Undo for bulk delete — deferred, not planned here.** Needs a tombstone
   record, a retention policy, a restore path and a new KV key type (with all
   three obligations that imposes). The count-bearing confirmation dialog is
   the accepted mitigation. Stays in Future work.
9. **Do nothing.** Live, and briefly plausible: `PATCH /api/links/{slug}`
   already does both edits correctly, and the bulk bar is already the busiest
   control in the app. Rejected because the manual path is N round trips through
   a row form for one decision, and the two edits it makes tedious are the two
   most common post-creation edits this product's audience performs. The
   marginal cost is genuinely small — no new endpoint, no new key, no new
   permission, no `redirect` change, one extra KV read on one of eight actions.

## Tasks

```
- [ ] Make links._target_url_error_body public as links.target_url_error_body (must land before the repoint branch) — file(s): api/links.py, api/tests/test_links.py — done when: the function is public with a docstring saying it is shared like can_view/can_edit/target_url_error, handle_create and handle_update call it under the new name, `grep -rn "_target_url_error_body" api` returns nothing, a 400 for an over-4096-byte target_url still carries {"error": "target_url_too_long", "max_bytes": 4096} byte-identically from both handlers, and `cd api && uv run pytest` passes
- [ ] Guard handle_bulk_action's catch-all delete branch before adding any new action — file(s): api/bulk.py, api/tests/test_bulk.py — done when: the write loop's final `else:  # delete` is `elif action == "delete":` with a new final else returning 500 {"error": "unhandled_action", "action": action}, delete's own behaviour and response are unchanged, and a new test that monkeypatches BULK_ACTIONS to include an unhandled name asserts 500 unhandled_action with dict(store._data) byte-identical (i.e. nothing deleted)
- [ ] Add the repoint bulk action with BOTH destination checks — file(s): api/bulk.py, api/tests/test_bulk.py — done when: BULK_ACTIONS contains "repoint"; POST /api/links/bulk-action {"action":"repoint","slugs":[...],"target_url":...} rewrites every selected record's target_url and bumps updated_at; a missing/non-string/schemeless URL returns 400 {"error":"invalid_target_url"}; a >4096-byte URL returns 400 {"error":"target_url_too_long","max_bytes":4096}; a policy-violating URL returns 400 {"error":"destination_not_allowed","host","reason","matched_rule"} with dict(store._data) byte-identical; per-row not_found/forbidden still report all-or-nothing via bulk_validation_failed; the per-row can_edit check is applied (reassign's skip is NOT copied); urlpolicy.load_policy is called only for this action; and `cd api && uv run pytest` passes
- [ ] Extend test_url_policy_enforcement.py from three authoring paths to four (depends on the repoint task) — file(s): api/tests/test_url_policy_enforcement.py — done when: a new test parametrized over both existing POLICY_CONFIGS asserts bulk repoint returns 400 destination_not_allowed with the store byte-identical, test_admin_is_not_exempt_from_the_policy also covers repoint, the module docstring says FOUR paths and records a fresh mutation run (temporarily delete the urlpolicy.evaluate block from bulk.py's repoint branch, confirm exactly this module fails, restore), and `cd api && uv run pytest` passes
- [ ] Add the schedule bulk action with a per-link window merge — file(s): api/bulk.py, api/tests/test_bulk.py — done when: BULK_ACTIONS contains "schedule"; key presence decides (posting only start_at leaves every selected record's end_at byte-identical, an explicit null clears that side, neither key present returns 400 {"error":"no_window_fields"}); an unparseable value returns 400 invalid_start_at / invalid_end_at; a merged window where start >= end appends a per-row {"slug","error":"invalid_window_range","start_at","end_at"} and NOTHING is written for any slug; the write loop writes exactly the merged values the validation loop computed (no recomputation); updated_at is bumped; success and partial responses echo only the sides that were provided; a schedule request deletes no record; and `cd api && uv run pytest` passes
- [ ] Pin partial-write reporting for both new actions — file(s): api/tests/test_bulk.py — done when: one ThrottlingStore test per action shows 200 with ok:false, partial:true, applied/not_applied in request order, next_step:"resubmit", a classified write_error, no index_updated key, the echo fields present, and every record the loop never reached byte-identical
- [ ] Add the schedule and repoint clusters to the bulk action bar (markup + CSS) — file(s): gui/dashboard.html, gui/dashboard.css — done when: #bulk-schedule-controls (visible Starts/Expires datetime-local inputs plus Set schedule / Clear schedule) and #bulk-repoint-controls (a url input plus Repoint) render inside #bulk-bar between the Enable/Disable/Delete group and #bulk-tag-controls, neither is permission-gated, there is no inline style= or <script> anywhere (`cd gui-pages && uv run pytest` passes), getComputedStyle confirms the three inputs are not width:100% and carry no stray bottom margin, and #links-figure does not gain a horizontal scrollbar (scrollWidth <= clientWidth) with the bar visible at 1400/768/480/390 px in both themes
- [ ] Wire the schedule and repoint handlers — file(s): gui/dashboard.js, gui/app.js — done when: handleBulkSchedule("set"|"clear") and handleBulkRepoint() post to /links/bulk-action, a blank datetime input means "leave that side alone" and only Clear schedule sends explicit nulls, both actions confirm through confirmDialog with a count-bearing message and confirmLabel, a bulk_validation_failed response renders through renderRowErrorList, a partial response calls loadLinks() then narrowSelectionTo(data.not_applied), updateBulkBar disables all NINE buttons past BULK_MAX_SELECTION, wireWindowValidation is wired to the two new inputs, and ERROR_MESSAGES gains target_url_too_long
- [ ] Record bulk schedule and repoint in the docs — file(s): CLAUDE.md, DESIGN.md, PRODUCT.md — done when: CLAUDE.md's "Bulk link management" section lists both actions with the key-presence window semantics and the unhandled_action guard, its "Destination URL policy" section says FOUR authoring paths and names bulk.handle_bulk_action's repoint branch, DESIGN.md's Bulk Action Bar section documents both clusters, why each confirms, and the nine-button over-cap rule, and PRODUCT.md's bulk bullet mentions bulk rescheduling and repointing
- [ ] End-to-end manual verification of bulk schedule and repoint — file(s): (none — verification step) — done when: against a real `spin up --build` run, all seven numbered checks in docs/plans/bulk-schedule-and-repoint.md's Verification section pass, including a policy-violating repoint refused with both destinations unchanged, an inverted merged window refused with nothing written, and a repointed link's /r/{slug} returning a 302 to the NEW destination
```

## Critical files

- `api/links.py`
- `api/bulk.py`
- `api/tests/test_bulk.py`
- `api/tests/test_links.py`
- `api/tests/test_url_policy_enforcement.py`
- `gui/dashboard.html`
- `gui/dashboard.css`
- `gui/dashboard.js`
- `gui/app.js`
- `CLAUDE.md`
- `DESIGN.md`
- `PRODUCT.md`
- `docs/plans/bulk-schedule-and-repoint.md` (new)

No new file is created in any component. `api/app.py`, `spin.toml`,
`Jenkinsfile`, `redirect/` and `gui-pages/` are all untouched — the test
commands CI runs do not change.

## Verification

1. `cd api && uv run pytest` — the affected suite. Baseline before this work
   was **679 passed**; expect that plus the new cases.
2. `cd gui-pages && uv run pytest` — the markup guard
   (`test_no_inline_code.py`) covers `gui/dashboard.html`. Baseline **108
   passed**. `redirect`'s suite is not in this list because `redirect/` is not
   touched; `go test ./...` must never be run.
3. Start the app (the bootstrap password is required on every run;
   `COOKIE_SECURE=false` is required for browser testing over plain HTTP):

   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```

   Then confirm the edited GUI assets are actually being served, because
   `spin_static_fs` serves a startup snapshot:
   `curl -s localhost:3000/dashboard.js | grep -c bulk-repoint-btn` → non-zero.
4. **Repoint, happy path.** Log in through the form at
   `http://localhost:3000/login.html` (a raw `fetch` login produces
   `csrf_mismatch` 403s). Create three links. Select all three, type a new
   destination, Repoint, confirm. Pass = success line names 3, the Destination
   column shows the new URL on all three after reload, and
   `curl -sI localhost:3000/r/<slug>` returns `302` with `Location:` the new
   destination and `Cache-Control: no-store`.
5. **Repoint, policy refusal — the check this plan exists for.** As an admin,
   add a deny rule for `evil.example` on `/admin/url-policy.html`. Select two
   links, repoint them at `https://evil.example/x`. Pass = the error line says
   the destination is not allowed by the URL policy, **and both links'
   Destination cells are unchanged after a reload**. Then, over `curl` with the
   session cookie, `POST /api/links/bulk-action` with
   `{"slugs":[...],"action":"repoint","target_url":"https://evil.example/x"}`
   → `400 {"error":"destination_not_allowed","host":"evil.example",...}`; and
   with a 5,000-character `https://ok.example/` + padding URL →
   `400 {"error":"target_url_too_long","max_bytes":4096}`.
6. **Schedule, one side only.** Give link A a start date in 2027 through its
   row edit form; leave link B with no window. Select both, fill **only**
   Expires with a 2026 date, Set schedule. Pass = refused, the row-error list
   names A (and only A) as expiring before it starts, and after a reload
   **B's Expires cell is still empty** — nothing was written. Change Expires to
   2028 and repeat: both links take it, and A's Starts date is still the 2027
   value it had (the omitted side was not touched).
7. **Schedule, expiry in the past, then clear.** Select two links, set Expires
   to a past datetime. Pass = their Status badge reads Expired and
   `curl -sI localhost:3000/r/<slug>` returns `404` (indistinguishable from an
   absent slug, by design). Then select them again and use Clear schedule;
   confirm the dialog's copy names the consequence. Pass = both window cells
   read `—`, the badge is Active again, and `/r/{slug}` returns `302`.
8. **Layout and theme.** With a selection active, measure at 1400, 768, 480 and
   390 px in both light and dark: `#links-figure`'s `scrollWidth` must not
   exceed its `clientWidth`, the bar's controls must wrap rather than overflow,
   and the console must be free of errors and CSP violations. Below 600 px the
   select column is hidden and `#bulk-desktop-only` shows, so the bar is only
   reachable there by resizing with a live selection — check that state too,
   since it is the one that renders the new inputs in the narrowest box.

## Out of scope / follow-ups

- **Clearing only one side of a window from the GUI.** The endpoint supports it
  (`{"end_at": null}` with `start_at` omitted); the bar's Clear schedule button
  clears both. Adding per-side clear affordances would put two more controls in
  an already-crowded bar for a case the row edit form handles. Revisit if an
  operator actually asks.
- **Collapsing the two new clusters behind a `<details>`.** The primary design
  is inline, matching the tag and owner clusters. **Named trigger:** if
  verification step 8 measures the bar wrapping to more than three rows at
  1400 px, wrap both new clusters together in a single `<details>` inside the
  bar (DESIGN.md's "collapse occasional-use fields behind a native `<details>`"
  Do is the precedent) and record the change in DESIGN.md's Bulk Action Bar
  section. Belongs under TASKS.md's Future work if it fires.
- **Unifying the eight near-identical write loops in `handle_bulk_action`.**
  Rejected for this change (Trade-offs #4) because the blast radius lands on
  `delete`. Worth its own change; added to Future work.
- **Bulk slug editing** — rejected outright, recorded under TASKS.md's
  "Considered and rejected".
- **Undo for bulk delete** — deferred; stays in TASKS.md's Future work as its
  own entry, since the original three-idea entry is now two-thirds consumed.
- **Raising `MAX_BULK_ROWS` above 50.** Both new actions inherit the shared
  cap. CLAUDE.md's rule stands: raising it needs real timing evidence from a
  full-cap submission, not "50 felt limiting".
- **Any change to `redirect`, `api/app.py`, `spin.toml` or the `Jenkinsfile`.**
  None is needed; if one turns out to be, the plan is wrong and the builder
  should stop and report rather than improvise.
</content>
</invoke>
