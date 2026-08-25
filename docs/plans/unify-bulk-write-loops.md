# Unify Bulk Write Loops

## Context

`api/bulk.py`'s `handle_bulk_action` now dispatches eight actions — `delete`,
`enable`, `disable`, `tag`, `untag`, `reassign`, `repoint`, `schedule` — through
an `if / elif` chain of **eight separate write loops** (`api/bulk.py:458-532`).
Six of them are byte-for-byte the same loop body:

```python
now = iso_now()
for slug, record in records.items():
    record[<one or two fields>] = <value>
    record["updated_at"] = now
    try:
        await write(lambda s=slug, r=record: store.set(f"slug:{s}", json.dumps(r).encode("utf-8")))
    except kvretry.WriteFailed as exc:
        write_failure = (exc,)
        break
    applied.append(slug)
```

and the seventh (`delete`) is the same shape over `store.delete`. The response
tail then repeats the per-action echo fields (`tags` / `owner` / `target_url` /
`start_at` / `end_at`) **twice** — once in the success body, once in the partial
body — so a ninth action, or a change to an existing one's echo, has to be
remembered in two places that no test compares against each other.

The duplication is not merely cosmetic. The `if/elif` chain's final arm used to
be a bare `else:  # delete`, which meant **a name added to `BULK_ACTIONS` with
no write branch fell into the delete loop** — the failure mode of a
half-finished action was deleting every selected link.
`docs/plans/bulk-schedule-and-repoint.md`'s task 2 fixed that defensively
(`elif action == "delete":` plus a catch-all `else: return 500
{"error": "unhandled_action", ...}`, pinned by
`test_bulk_action_unhandled_action_name_returns_500_and_writes_nothing`). That
guard is a seatbelt on a structure that still permits the crash. This change
removes the structure.

This work is the Future-work entry raised 2026-08-24 while planning
`docs/plans/bulk-schedule-and-repoint.md` and rejected *for that change* by its
Trade-offs #4 ("touches every existing action's code path in a change whose
stated non-goal is *do not change any existing action's behaviour* — the risk
lands on `delete`, the one irreversible action … Worth reopening as its own
change, with its own verification"). `schedule` and `repoint` have since
shipped and deployed (`acf924a-bulkschedrepoint`), so all eight loops now
genuinely exist and the entry's trigger has fired.

**Confirmed decisions (settled by the user before planning):**

- This is a **pure refactor**. No new functionality, no new endpoint, no new
  action, no behaviour change visible to any caller.
- Every existing action's external behaviour is preserved exactly: response
  shapes (`partial` / `not_applied` / `write_error` / `next_step` / the absence
  of `index_updated`), the `RECORD_WRITE` retry policy through `kvretry`,
  all-or-nothing validation vs. best-effort execution, `reassign`'s `can_edit`
  skip, `repoint`'s dual `links.target_url_error` + `urlpolicy.evaluate` checks
  paid *only* by that action, `schedule`'s per-slug pre-computed merged windows,
  and `delete`'s lack of an analytics purge.
- `api/tests/test_bulk.py` is the regression net. **No existing assertion may be
  weakened.** New tests may be added; existing ones must pass untouched.
- Non-goals: no changes to `redirect/`, `gui/`, `gui-pages/`, `spin.toml`,
  `api/app.py`, or `Jenkinsfile`.

## Key technical facts confirmed during research

- **Baseline, measured now:** `cd api && uv run pytest` → **701 passed** in
  12.55s. `cd gui-pages && uv run pytest` and `cd redirect && go test
  ./linkgate/...` are untouched by this change but are listed in Verification.
- **The eight loops are at `api/bulk.py:458-532`**, in the order
  `ACTION_STATUSES` (enable/disable) → tag/untag → reassign → repoint →
  schedule → delete → `else:` 500 guard. Read in full; the six `set` loops
  differ *only* in which record fields they assign.
- **At write time, `records` contains every requested slug, in request order.**
  Confirmed by reading `handle_bulk_action`: the row loop appends `not_found`
  (`bulk.py:415`) and `forbidden` (`:424`) instead of adding to `records`, the
  tag-cap check appends `too_many_tags` (`:431`), the schedule merge appends
  `invalid_window_range` (`:441`), and `if row_errors: return json_response(400,
  ...)` (`:447-448`) runs before any write. So `records` is either
  request-complete or the handler already returned. `dict` preserves insertion
  order, so `records.items()` is exactly `slugs` order. **This is what lets a
  single planning pass produce one mutation per requested slug with no
  reordering and no gaps.**
- **`BULK_ACTIONS` and `ACTION_STATUSES` are referenced nowhere outside
  `api/bulk.py` and `api/tests/test_bulk.py`** — confirmed by
  `grep -rn "BULK_ACTIONS\|ACTION_STATUSES" api gui gui-pages CLAUDE.md
  README.md`. `gui/dashboard.js` posts literal action strings
  (`"reassign"`, `"schedule"`, `"repoint"`, …) and never reads the set, so
  deriving `BULK_ACTIONS` from a table changes nothing client-side.
- **`frozenset({...}) | {"x"}` returns a `frozenset`** (checked in the
  interpreter), so `test_bulk_action_unhandled_action_name_returns_500_and_writes_nothing`'s
  `monkeypatch.setattr(bulk, "BULK_ACTIONS", bulk.BULK_ACTIONS | {"bogus_but_allowed"})`
  (`api/tests/test_bulk.py:559`) keeps working when `BULK_ACTIONS` becomes a
  `frozenset` derived from the spec table — and now exercises exactly the
  desync it was written for.
- **`json_response` does `json.dumps(data)` with default `sort_keys=False`**
  (`api/responses.py:59`), so a dict's insertion order *is* the wire byte order.
  `{**base, **extra}` therefore reproduces today's
  "build the base dict, then assign the echo fields" order byte-for-byte.
- **`handle_bulk_action` has no `purge_analytics` parameter at all** — its
  signature is `(store, users_store, principal, request, get_many, write)`,
  called from `api/app.py:343`. Bulk delete's lack of an analytics purge is
  therefore structural, not a branch that could be "fixed" by accident, and
  stays that way: the unified loop performs exactly one KV op per slug and
  takes no purge callable. Pinned by
  `test_bulk_action_delete_leaves_analytics_untouched`
  (`api/tests/test_bulk.py:1223`).
- **`write(make_coro)` defaults to `policy=RECORD_WRITE`** (`api/kvretry.py:159`),
  and `kvretry.direct` accepts-and-ignores a policy (`:137-142`). Calling
  `write(make_coro)` with no explicit policy inside the unified loop is
  therefore identical to every existing branch.
- **`json.dumps(r)` currently happens *inside* the lambda**, i.e. re-evaluated on
  each retry attempt. The unified loop keeps the lambda text identical rather
  than hoisting the serialisation — no reason to change when-it-serialises in a
  refactor whose whole claim is byte-identical behaviour.
- **`dataclasses.field(default_factory=...)` is already used in this component**
  (`api/auth.py:16,94`, `assigned_domains`), so it is confirmed to survive the
  `componentize-py` build. `typing.Callable` is likewise already imported by
  `api/kvretry.py:42`, which is compiled into the same component.
- **CI runs bare `uv run pytest -v`** (`Jenkinsfile`) with no coverage gate and
  no linter, and `api/pyproject.toml` configures only pytest. So a
  `# pragma: no cover` marker is decorative here; keeping or dropping it changes
  nothing mechanical.
- **Correction to the brief:** `TASKS.md` line 495 sits under
  **`## Future work (not scheduled)`** (heading at line 392), not under
  `## Considered and rejected` (heading at line 217). The entry is otherwise
  exactly as quoted.
- **`docs/plans/bulk-schedule-and-repoint.md:577-586`** is the prior rejection
  this change reopens; its reasoning is quoted in Context above.
- **UNCONFIRMED:** nothing in this plan requires a live Akamai measurement. The
  one claim that is model-only is "the refactor changes no KV operation count" —
  it is confirmed *locally* by the existing call-counting tests
  (`test_bulk_action_delete_writes_exactly_one_delete_per_slug_no_index_writes`,
  `test_bulk_action_repoint_only_loads_policy_for_repoint`), which is sufficient
  because the operation count is decided by pure Python, not by the host.

## API changes — `api/bulk.py`

Everything below lands in `api/bulk.py`. No new module: `bulk.py` is the only
consumer, and a new module would add an import edge for zero reuse.

### 1. Two new dataclasses and a context object

```python
@dataclass(frozen=True)
class PlannedMutation:
    """One slug's write, decided BEFORE any write happens. `kind` is "set"
    (write `record` back to slug:<slug>) or "delete" (remove the record).

    Planning is pure and KV-free; `_apply_mutations` is the only thing that
    writes, and it never sees the action name."""

    slug: str
    kind: str            # "set" | "delete"
    record: dict | None  # the full record for "set"; None for "delete"
```

```python
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
```

```python
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
```

Add `field` to the existing `from dataclasses import dataclass` import and
`from typing import Callable` (both already proven to build, see facts above).

### 2. Seven pure planner functions

Module-level, pure, no `await`, no KV. Each mutates the in-memory record and
returns its `PlannedMutation`.

```python
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


def _plan_delete(ctx, slug, record):
    # No analytics purge, deliberately — CLAUDE.md's "Orphaned analytics purge":
    # 50 slugs x ~95 keys is ~95-123 s against a 30 s handler limit.
    # handle_bulk_action takes no purge_analytics callable at all, so this
    # cannot regress by accident.
    return PlannedMutation(slug, "delete", None)
```

`enable`/`disable` share `_plan_status`, which reads `ACTION_STATUSES[ctx.action]` —
that dict already exists and is pinned by
`test_action_statuses_values_subset_of_link_statuses`, so it stays the table for
that one datum. `tag`/`untag` get two functions rather than one branching on
`ctx.action`, because they differ by *which helper they call*, which is not
table data.

### 3. Five result-field functions

```python
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
```

`_no_extra_fields` must be defined **above** `ActionSpec`, since it is that
dataclass's default.

### 4. The table, and `BULK_ACTIONS` derived from it

Replaces today's `BULK_ACTIONS` set literal (`api/bulk.py:24-27`, including its
now-stale "the actual owner-move logic lands in a later task" comment, which
should be dropped).

```python
ACTION_SPECS: dict[str, ActionSpec] = {
    "delete":   ActionSpec("delete", _plan_delete),
    "enable":   ActionSpec("enable", _plan_status),
    "disable":  ActionSpec("disable", _plan_status),
    "tag":      ActionSpec("tag", _plan_tag,
                           required_permission="links.tag", result_fields=_tag_fields),
    "untag":    ActionSpec("untag", _plan_untag,
                           required_permission="links.tag", result_fields=_tag_fields),
    "reassign": ActionSpec("reassign", _plan_reassign, per_row_can_edit=False,
                           required_permission="users.manage", result_fields=_owner_fields),
    "repoint":  ActionSpec("repoint", _plan_repoint, result_fields=_target_url_fields),
    "schedule": ActionSpec("schedule", _plan_schedule, result_fields=_window_fields),
}

# DERIVED, never a literal. This is the structural fix: a name cannot reach the
# endpoint's write path without an ActionSpec, and an ActionSpec cannot exist
# without a `plan`. The `unhandled_action` 500 below survives only as a guard
# against the two ever being decoupled again.
BULK_ACTIONS = frozenset(ACTION_SPECS)
```

`ACTION_STATUSES` (`api/bulk.py:22`) stays exactly as it is.

### 5. The single write loop

```python
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
```

The two `write(...)` calls are textually the same call shapes as today's, with
the same default `RECORD_WRITE` policy and the same
`json.dumps(...)`-inside-the-lambda timing.

### 6. `handle_bulk_action`'s new shape

Only three edits to the body; everything before the write phase keeps its
current text unless named here.

**(a) Spec lookup, immediately after the membership check** (replacing nothing
else at `api/bulk.py:329-331`):

```python
    action = payload.get("action")
    if action not in BULK_ACTIONS:
        return json_response(400, {"error": "invalid_action"})

    spec = ACTION_SPECS.get(action)
    if spec is None:  # pragma: no cover - BULK_ACTIONS is derived from ACTION_SPECS
        # Unreachable by construction. Kept as the last line of defence if the
        # two are ever decoupled: a name with no spec must be a clean 500,
        # never someone else's write loop.
        return json_response(500, {"error": "unhandled_action", "action": action})
```

Moving the guard here (from the tail of the write dispatch) means the
monkeypatched-desync case now returns before the `get_many` read — strictly
cheaper, and `test_bulk_action_unhandled_action_name_returns_500_and_writes_nothing`'s
three assertions (500, exact body, `store._data` unchanged) all still hold.

**(b) The generic permission check**, placed exactly where the
`if action == "reassign":` block begins today (`api/bulk.py:344`), i.e. after
the `no_slugs` / `duplicate_slug` / `too_many_rows` validations and before every
per-action payload block. Placement is load-bearing in both directions: hoisting
it above the slug validations would turn a permission-less empty-`slugs` request
from `400 no_slugs` into `403 forbidden`, and sinking it below the owner lookup
would restore the username-enumeration hazard
`test_bulk_action_reassign_without_permission_cannot_distinguish_a_real_owner_from_a_fake_one`
exists to prevent.

```python
    # Permission BEFORE any payload-derived lookup, deliberately: the reverse
    # order lets a caller without users.manage tell "no such user"
    # (400 unknown_owner) from "user exists" (403 forbidden) and so enumerate
    # the very username list GET /api/users gates on this same permission.
    if spec.required_permission and not principal.has_permission(spec.required_permission):
        return json_response(403, {"error": "forbidden", "required_permission": spec.required_permission})
```

The two now-redundant inline 403 returns (`:350-351` for `reassign`,
`:360-361` for `tag`/`untag`) are deleted; the surrounding
`if action == "reassign":` / `if action in ("tag", "untag"):` payload blocks
otherwise stay exactly as they are. They are independent `if` guards, not an
`if/elif/else` chain, so an unknown name simply skips them all — that half of
the dispatch has never had a fall-through hazard and is deliberately left alone.

**(c) The row loop's `can_edit` condition** (`api/bulk.py:423`) loses its action
literal:

```python
        if spec.per_row_can_edit and not links.can_edit(principal, record):
```

with the existing "Reassignment deliberately skips the per-row can_edit check"
comment moved onto `ActionSpec.per_row_can_edit`'s declaration in the table.

**(d) The whole write dispatch and response tail** (`api/bulk.py:450-572`)
becomes:

```python
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
```

### Two behaviour deltas, both audited and both unobservable

1. **`iso_now()` is now called once per request including for `delete`**, where
   today it is never called. `responses.iso_now` is
   `to_iso8601_utc(datetime.now(timezone.utc))` — pure, no KV, no I/O — and
   `_plan_delete` ignores `ctx.now`. Nothing observes it.
2. **Every record dict is mutated in memory before the first write**, where
   today record *N* is mutated only when the loop reaches it. If a write fails
   at record 3, records 4..N are now mutated in memory and never written. The
   store is the only observable surface and it is untouched — pinned by the
   existing `after3 == before3` assertions in
   `test_bulk_action_repoint_report_partial_with_write_error` and
   `test_bulk_action_schedule_report_partial_with_write_error`, and by
   `test_bulk_action_delete_throttled_leaves_the_undeleted_records_intact`.
   The alternative (calling `spec.plan(...)` lazily inside the write loop)
   preserves the interleaving but puts the action back inside the loop; see
   Trade-offs #2.

Everything else is identical: same KV operations in the same order, same retry
policy, same `applied` / `not_applied` ordering, same JSON key ordering, same
statuses.

## Test changes — `api/tests/test_bulk.py`

**No existing test is edited.** All 701 must pass untouched; that is the primary
acceptance signal. Five tests are *added*, each pinning something the old shape
could not express.

1. `test_bulk_actions_is_exactly_the_action_spec_table` — asserts
   `bulk.BULK_ACTIONS == frozenset(bulk.ACTION_SPECS)` **and**
   `bulk.BULK_ACTIONS == {"delete", "enable", "disable", "tag", "untag",
   "reassign", "repoint", "schedule"}`. The first half is the structural
   property; the second stops the table quietly growing an action nobody
   reviewed.
2. `test_every_action_spec_carries_a_callable_plan` — for each spec: `plan` is
   callable, `result_fields` is callable, `name` equals its dict key.
3. `test_only_reassign_skips_the_per_row_can_edit_check` — asserts
   `{n for n, s in bulk.ACTION_SPECS.items() if not s.per_row_can_edit} ==
   {"reassign"}`, and that `required_permission` is `"links.tag"` for
   `tag`/`untag`, `"users.manage"` for `reassign`, `None` for the other five.
4. `test_apply_mutations_takes_no_action_parameter` — 
   `set(inspect.signature(bulk._apply_mutations).parameters) == {"store", "write", "mutations"}`.
   A cheap structural pin, the same genre as
   `api/tests/test_kvprefix.py`'s cross-language guard: it fails the moment
   someone reintroduces per-action branching into the write path.
5. `test_success_and_partial_responses_carry_the_same_echo_fields` —
   parametrized over the five echo-carrying actions (`tag`, `untag`, `reassign`,
   `repoint`, `schedule`). For each, run one success (`FakeStore` +
   `kvretry.direct`) and one partial (`ThrottlingStore` with the second slug's
   key in `_fail_times`, `kvretry.make_writer(recording_sleep()[0])`), then
   assert
   `set(success_body) - {"ok", "action", "count"} ==
   set(partial_body) - {"ok", "partial", "action", "count", "applied",
   "not_applied", "write_error", "next_step"}`.
   This is the invariant the duplicated response tail could never state.

Optionally (cheap, and it documents the planners as the unit they now are), two
direct planner tests: `_plan_schedule` reads `ctx.planned_windows[slug]` verbatim
and never recomputes from `ctx.new_start_at`/`new_end_at`; `_plan_delete` returns
`kind == "delete"` with `record is None` and mutates nothing.

## Documentation changes — `CLAUDE.md`

Exactly one sentence is now wrong, and it is *prescriptive*, so leaving it would
instruct a future reader to reintroduce the shape this change removes.
`CLAUDE.md`'s "Bulk link management" section currently reads:

> **The write dispatch's final branch is `elif action == "delete":` followed by
> a catch-all `else: return 500 {"error": "unhandled_action", ...}` — never a
> bare `else: # delete`.** `BULK_ACTIONS` is validated separately from the write
> dispatch, so a name added to that set with no matching write branch used to
> fall into the delete loop as the implicit catch-all; the guard turns that into
> a clean 500 instead of deleting every selected link.

It must be replaced with the structural rule — substance to convey, wording the
builder's:

> **There is no per-action write dispatch any more.** `ACTION_SPECS` maps each
> action name to an `ActionSpec` (its planner, whether the per-row `can_edit`
> check applies, its required permission, its response echo fields), and
> `BULK_ACTIONS` is **derived** from it (`frozenset(ACTION_SPECS)`). A single
> loop, `_apply_mutations`, performs every bulk write and takes no `action`
> parameter at all, over a list of `PlannedMutation`s computed by a pure,
> KV-free planning pass. So a name cannot reach the endpoint without a planner,
> and cannot fall into another action's write loop — the hazard the older
> `else: # delete` catch-all created. The `unhandled_action` 500 survives only
> as a guard against `BULK_ACTIONS` and `ACTION_SPECS` ever being decoupled
> again. Plan: `docs/plans/unify-bulk-write-loops.md`.

Deliberately **not** changed:

- `CLAUDE.md`'s "Write-throttle resilience" line saying `handle_bulk_action`
  "break[s] their record-write loops on `kvretry.WriteFailed` and report[s]
  exactly what landed" — still accurate at the level it describes (the loop now
  `return`s instead of `break`s; the contract is unchanged).
- `CLAUDE.md`'s "Orphaned analytics purge" and "Destination URL policy"
  references to "`bulk.handle_bulk_action`'s delete branch" / "repoint branch",
  and the same phrasing in `api/analyticsorphans.py`'s module docstring and
  `api/tests/test_links.py:529`'s comment. These describe *behaviour*, not
  structure, and read correctly against `ACTION_SPECS["delete"]` /
  `ACTION_SPECS["repoint"]`. Touching four files to reword a phrase that is
  still true would widen the diff of a refactor whose value is its
  reviewability.
- `DESIGN.md`, `PRODUCT.md`, `README.md`, `gui/` — nothing user-visible changes.
- `Jenkinsfile` — test invocation is unchanged.

## Trade-offs and rejected alternatives

1. **Do nothing — keep the eight loops and the `unhandled_action` guard.**
   Attractive: zero risk to `delete`, the one irreversible action, and the guard
   already converts the historical failure mode into a clean 500. Lost because
   the guard is defence on a structure that still *invites* the mistake, and the
   duplication has already cost real edits — the response echo fields exist
   twice with nothing comparing them, and the eight loops were copied twice in
   the last two features. The Future-work entry explicitly says to pick this up
   as its own change with its own verification; that is what this is.
2. **Plan lazily inside the write loop (`spec.plan(ctx, slug, record)` called
   immediately before each write) instead of materialising the list first.**
   Attractive: preserves today's exact plan/write interleaving, so records the
   loop never reaches are never even mutated in memory. Lost because it puts the
   spec — and therefore the action — back inside the write loop, which is
   precisely the property this change buys. With planning hoisted,
   `_apply_mutations` has no way to know what action it is serving, and
   `test_apply_mutations_takes_no_action_parameter` can say so. The delta it
   costs is in-memory-only and unobservable (see "Two behaviour deltas").
3. **Fold `handle_bulk_create`'s write loop into `_apply_mutations` too.**
   Attractive: it is a ninth copy of the same try/write/except/break shape.
   Lost because it is *one* loop, not eight — there is no duplication to remove
   — and its failure report is a different shape (`not_created` rows carrying
   `line` numbers derived from `assigned[idx:]`, plus `created_records` for
   `links.public_link`). Unifying it would mean `_apply_mutations` returning a
   failure *index* and both callers reconstructing their own reports from it —
   more coupling, wider blast radius, no duplication removed. Left alone
   deliberately; noted under Out of scope.
4. **Table-drive the per-action payload validation too** (owner lookup, tag
   parsing, URL + policy checks, window parsing) via a `prepare` coroutine on
   each spec. Attractive: it would make "action added with no validation" as
   impossible as "action added with no write branch" now is. Lost because those
   four blocks share nothing — different stores, different awaits, different
   4xx bodies — so a `prepare` hook would be eight bespoke functions behind a
   uniform signature: the same code, one indirection further away. And they are
   independent `if` guards with no catch-all, so they have never had the
   fall-through hazard the write dispatch had. The one genuinely uniform piece,
   the permission check, *is* hoisted onto the spec (§6b).
5. **A `BulkAction` class hierarchy (one subclass per action) instead of a
   dataclass table of functions.** Attractive to some readers, and it would
   colocate each action's planner with its result fields. Lost on repo
   convention: `api/` has no class hierarchies outside dataclasses and the WASI
   `Handler`, everything pure is plain functions taking explicit parameters, and
   a table of frozen dataclasses is trivially assertable from a test
   (`ACTION_SPECS.items()`) in a way an inheritance tree is not.
6. **Batch or gather the unified loop's writes now that they are in one place.**
   Rejected repo-wide and unchanged here: writes are cap-bound at 50/s app-wide,
   so concurrency queues against the cap rather than overlapping, and
   `wasi:keyvalue/batch`'s multi-key writes disclaim atomicity (CLAUDE.md,
   "Parallel KV reads"; `TASKS.md` 2026-08-15). `_apply_mutations` is
   sequential, permanently, and its docstring should say so.
7. **Add an analytics purge to `_plan_delete` while delete's code is open.**
   Rejected: the arithmetic still does not close (50 slugs × ~95 keys ≈
   95–123 s against a 30 s handler limit), and `handle_bulk_action` has no
   `purge_analytics` parameter to purge *with*. Out of scope by definition — this
   is a refactor.

## Tasks

The lines appended to `TASKS.md` under `## Unify bulk write loops`, verbatim:

```
- [ ] Replace handle_bulk_action's eight write loops with an ActionSpec table and one planned-mutation loop — file(s): api/bulk.py — done when: `cd api && uv run pytest` passes at 701 with zero edits under api/tests/; `grep -c "elif action ==" api/bulk.py` is 0; `grep -c "except kvretry.WriteFailed" api/bulk.py` is 2 (handle_bulk_create's loop and _apply_mutations); `python -c "import bulk; assert bulk.BULK_ACTIONS == frozenset(bulk.ACTION_SPECS)"` succeeds from api/; _apply_mutations' parameters are exactly (store, write, mutations); and the row loop's can_edit condition reads `spec.per_row_can_edit`, not `action != "reassign"`
- [ ] Move each action's required permission onto its ActionSpec (depends on the task above) — file(s): api/bulk.py — done when: the inline 403 returns inside the reassign and tag/untag payload blocks are gone, one `if spec.required_permission and not principal.has_permission(...)` check sits where the reassign block used to begin (after the too_many_rows validation, before every per-action payload block), test_bulk_action_reassign_requires_users_manage_permission, test_bulk_action_tag_requires_links_tag_permission and test_bulk_action_reassign_without_permission_cannot_distinguish_a_real_owner_from_a_fake_one all pass unedited, a permission-less request with an empty slugs list still returns 400 no_slugs (not 403), and `cd api && uv run pytest` passes
- [ ] Pin the structural invariants the new shape makes statable — file(s): api/tests/test_bulk.py — done when: five new tests assert BULK_ACTIONS == frozenset(ACTION_SPECS) and equals the eight known names, every spec's plan/result_fields are callable and name matches its key, only reassign has per_row_can_edit False with the three expected required_permission values, _apply_mutations' signature has no action parameter, and (parametrized over tag/untag/reassign/repoint/schedule) a success body and a ThrottlingStore partial body carry the identical set of echo fields; `cd api && uv run pytest` passes at 706+
- [ ] Mutation-verify the structural guard and the echo-field invariant — file(s): (none — verification step) — done when: temporarily setting `BULK_ACTIONS = frozenset(ACTION_SPECS) | {"bogus"}` makes the new spec-table equality test fail AND a POST with action "bogus" returns 500 unhandled_action with store._data byte-identical; temporarily deleting `**extra` from the partial response body makes the new echo-field parity test fail for all five actions; both mutations reverted and `cd api && uv run pytest` passes; the outcomes recorded in the task note
- [ ] Correct CLAUDE.md's bulk write-dispatch paragraph — file(s): CLAUDE.md — done when: the "Bulk link management" bullet no longer prescribes `elif action == "delete":` plus a catch-all else, and instead states that BULK_ACTIONS is derived from ACTION_SPECS, that _apply_mutations is the single action-agnostic write loop over PlannedMutations, and that the unhandled_action 500 survives only as a decoupling guard, linking docs/plans/unify-bulk-write-loops.md; no other CLAUDE.md section, and no DESIGN.md/PRODUCT.md/README.md text, is touched
- [ ] End-to-end manual verification of the unified bulk write loop — file(s): (none — verification step) — done when: against a real `spin up --build` run, all eight actions are exercised from the dashboard on real links (delete, enable, disable, tag, untag, reassign, repoint, schedule set + clear) and each produces the same success copy and the same table state as before the refactor, a policy-violating repoint is still refused with both destinations unchanged, an inverted merged schedule window still writes nothing for any slug, and `curl -sI localhost:3000/r/<slug>` still 302s to a repointed destination
```

## Critical files

- `docs/plans/unify-bulk-write-loops.md` (new)
- `api/bulk.py`
- `api/tests/test_bulk.py` (additions only — no existing assertion edited)
- `CLAUDE.md`
- `TASKS.md`

## Verification

In execution order.

1. **Baseline, before any edit** (already measured while planning; re-take it so
   the builder owns the number):
   ```bash
   cd api && uv run pytest
   ```
   Expect `701 passed`.

2. **After the refactor tasks, with `api/tests/` untouched:**
   ```bash
   cd api && uv run pytest
   ```
   Expect `701 passed`. **This is the whole regression argument.** Any failure
   here is a behaviour change, not a test that needs updating — the existing
   per-action tests, including the five `ThrottlingStore` partial-write tests
   (`test_bulk_action_delete_throttled_leaves_the_undeleted_records_intact`,
   `..._enable_disable_report_partial_with_no_index_field`,
   `..._tag_untag_report_partial_with_no_index_field`,
   `..._repoint_report_partial_with_write_error`,
   `..._schedule_report_partial_with_write_error`), are the contract.

3. **Structural greps:**
   ```bash
   cd api
   grep -c "elif action ==" bulk.py          # 0
   grep -c "except kvretry.WriteFailed" bulk.py   # 2
   grep -n "BULK_ACTIONS = " bulk.py         # frozenset(ACTION_SPECS)
   ```

4. **After the new tests land:**
   ```bash
   cd api && uv run pytest
   ```
   Expect `706 passed` (701 + the five new tests; more if the two optional
   planner tests are added).

5. **Mutation check A — the structural guard.** Temporarily edit
   `BULK_ACTIONS = frozenset(ACTION_SPECS) | {"bogus"}`, run
   `cd api && uv run pytest -k "spec_table or unhandled_action"`. Pass =
   `test_bulk_actions_is_exactly_the_action_spec_table` **fails**, and
   `test_bulk_action_unhandled_action_name_returns_500_and_writes_nothing` still
   passes (the 500 fires, nothing is deleted). Revert.

6. **Mutation check B — the echo-field invariant.** Temporarily delete `**extra`
   from the partial response body, run
   `cd api && uv run pytest -k same_echo_fields`. Pass = it fails for all five
   parametrized actions. Revert.

7. **The other two suites, to prove nothing leaked sideways:**
   ```bash
   cd gui-pages && uv run pytest        # expect 108 passed
   cd redirect && go test ./linkgate/...   # expect ok
   ```
   (Never `go test ./...` — it fails by design on `package main`.)

8. **Live end-to-end**, from the repo root:
   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```
   Log in through the real form at `http://localhost:3000/login.html` (a raw
   `fetch` login produces `csrf_mismatch` 403s). Create six links, then exercise
   every action from the dashboard's bulk bar:
   1. **Disable** two → both Status badges read Disabled; `curl -sI
      localhost:3000/r/<slug>` → `404`. **Enable** them back → `302`.
   2. **Tag** three with `sale` → chips appear on all three; **Untag** two →
      chips gone from those two only.
   3. **Reassign** two to another account (as an admin) → the Owner column shows
      the new owner on both.
   4. **Repoint** three at `https://example.com/new` → the Destination column
      updates on all three and `curl -sI localhost:3000/r/<slug>` returns `302`
      with `Location: https://example.com/new` and `Cache-Control: no-store`.
   5. **Repoint** two at a destination denied by a live rule added on
      `/admin/url-policy.html` → the error line names the policy and **both
      destinations are unchanged after a reload**.
   6. **Schedule** — set Expires only on two links → their Starts cells are
      untouched; set a Starts later than an existing Expires → the row error
      names the link and both merged dates and **nothing is written for any
      selected slug**; **Clear schedule** → both window cells read `—` and the
      links are Active again.
   7. **Delete** the remainder → rows disappear and `curl -sI
      localhost:3000/r/<slug>` returns `404`.

   Pass = every one of these behaves exactly as it did before the refactor, with
   the same success/error copy. This list is deliberately the same ground
   `docs/plans/bulk-schedule-and-repoint.md`'s Verification covered, widened to
   the six older actions, because "unchanged" is the entire claim.

## Out of scope / follow-ups

- **`handle_bulk_create`'s write loop stays as it is** (Trade-offs #3). If a
  second create-shaped bulk path ever appears, revisit; one loop is not
  duplication. Not worth a Future-work entry on its own.
- **Table-driving the per-action payload validation** (Trade-offs #4) — recorded
  under `TASKS.md`'s `## Considered and rejected` with its revisit trigger: a
  future action whose validation *is* uniform with an existing one's, at which
  point the shared piece can move onto `ActionSpec` the way
  `required_permission` just did.
- **Batching or gathering bulk writes** stays rejected repo-wide; no new entry
  needed, `TASKS.md` already carries the 2026-08-15 decision.
- **No analytics purge for bulk delete** — unchanged, and CLAUDE.md's existing
  "Orphaned analytics purge" section already carries the arithmetic and the
  revisit condition.
