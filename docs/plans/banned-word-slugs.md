# Banned-Word Check for Link Slugs

## Context

Every short link this app publishes carries its slug in the URL itself, and
increasingly on printed material — `api/qr.py` renders a QR code for each link,
and the personas in `PRODUCT.md` are a marketing team producing campaign links
for ads and promotions. Today nothing between a user's keystrokes and a live
`/r/<slug>` looks at what the slug *says*. `api/links.py:95`'s
`is_valid_custom_slug` checks shape only (`^[A-Za-z0-9_-]{3,32}$`), and
`allocate_random_slug` (`api/links.py:28`) accepts whatever
`secrets.choice`-sampled base62 string it draws. Two distinct failure modes
follow from that:

1. **A careless custom slug.** Someone in a hurry types a slug containing a word
   the company would not put on a billboard, it goes out in a campaign, and the
   only remedy is deleting the link — there is no rename path (see the confirmed
   facts below), so every QR code and printed URL already produced is dead.
2. **An unlucky generated slug.** This is the one with no human in the loop at
   all. A 7-character base62 string happens to contain an unfortunate three-letter
   sequence, and nobody reads it before it is printed.

This plan adds a small, static banned-word check covering both paths. There is
no `TASKS.md` Future-work entry for it; the request came from the user directly.
The nearest adjacent entry is the 2026-07-18 **"Multi-domain short-link hosting +
admin-managed destination domain allow/deny-list"** item, whose part (2) is an
admin-managed list constraining a link's *`target_url`*. **This work must not
become a half-duplicate of that entry** — it touches slugs only and leaves
destination filtering entirely to it.

**Confirmed decisions** (settled by the user before planning — not reopened
here):

1. **The list is static, in `api/` code** — a module-level constant behind a
   predicate function. No KV storage, no admin UI, no CRUD endpoints, no new
   permission. Keeping it behind a function is deliberate: a future move to
   KV-backed storage becomes a change to one module's internals with every call
   site already shaped correctly.
2. **Custom slugs only.** Destination URLs are explicitly out of scope.
3. **Segment-aware matching for custom slugs**, case-insensitive: split on `-`
   and `_`, match whole segments. `black-friday-<word>` is rejected;
   `classic-glasses` and `assets-2026` are not. This exists specifically to
   avoid the Scunthorpe problem, which plain substring matching on a
   32-character slug walks straight into.
4. **Auto-generated slugs are checked too, and regenerated on a hit.**

## Key technical facts confirmed during research

- **`is_valid_custom_slug` has exactly two callers**, and they need different
  error shapes: `api/links.py:153` (inside `handle_create`, returns a single
  `400 {"error": ...}` body) and `api/bulk.py:131` (inside `validate_bulk_rows`,
  which builds one `{"line", "slug", "error"}` dict *per row* and reports them
  all together). Confirmed by grep over `api/`. This is why the new check is a
  **separate predicate called alongside** rather than folded into
  `is_valid_custom_slug` — see Trade-offs.

- **`validate_bulk_rows` already carries an extra key on one error code.**
  `duplicate_slug_in_submission` rows include `"first_line"`
  (`api/bulk.py:138-143`), so adding a `"segment"` key to the new code's row
  dicts follows established shape rather than inventing one.

- **Both create paths allocate random slugs through the same function.**
  `links.handle_create` calls `allocate_random_slug(store, set())`
  (`api/links.py:160`) and `bulk.handle_bulk_create` calls
  `links.allocate_random_slug(store, taken)` (`api/bulk.py:220`). One check
  inside that function's loop therefore covers every generated slug in the
  application. `generate_slug()` itself (`api/links.py:24`) is a pure alphabet
  sampler with no store access and stays that way.

- **The retry loop is already there and is cheap to reuse.**
  `allocate_random_slug` loops `SLUG_GENERATION_ATTEMPTS` (5) times, currently
  discarding candidates that are in `taken` or that `store.exists` reports.
  Rejecting a banned candidate in the same loop, *before* the `store.exists`
  call, costs nothing and saves a KV read on the rejected candidate.

- **Slugs are immutable after creation — confirmed, not assumed.**
  `UPDATABLE_FIELDS = {"target_url", "status", "start_at", "end_at"}`
  (`api/links.py:219`) and `handle_update` never writes `record["slug"]`. The
  full route table in `api/app.py` (lines 63-120) is `GET|POST /api/links`,
  `POST /api/links/bulk`, `POST /api/links/bulk-action`,
  `POST /api/links/{slug}/password`, `GET /api/links/{slug}/analytics`,
  `GET /api/links/{slug}/qr`, and `GET|PATCH|DELETE /api/links/{slug}` — there
  is no rename endpoint. So no edit of an existing link can ever re-run slug
  validation, and a link whose slug predates the list keeps working and stays
  fully editable.

- **The Go redirect component never validates slug shape.** `redirect/main.go`
  does `store.Exists("slug:" + slug)` then `store.Get` (lines 147-153) with no
  pattern check anywhere. Confirmed by grep for `slug` across `main.go`.
  **Nothing in this plan touches the Go hot path**, and existing links with
  now-banned slugs keep resolving.

- **The GUI already has the seam for the new message.** `gui/app.js:149`'s
  shared `ERROR_MESSAGES` maps API error codes to human text, and
  `friendlyError(data, fallback, overrides)` (`gui/app.js:171`) resolves
  `(overrides && overrides[code]) || ERROR_MESSAGES[code] || fallback` — so an
  override key that is absent (or falsy) falls through to the shared map
  automatically. The create-link path at `gui/dashboard.js:318` already passes
  an overrides object.

- **The dashboard's bulk-create panel does not exist yet.** `TASKS.md`'s
  `## Bulk link management` section has its last four tasks unchecked — the two
  API endpoints shipped, the GUI panel did not. Because that panel is specced to
  use `friendlyError` with a local overrides map, adding the new code to the
  shared `ERROR_MESSAGES` means it will render a correct (if generic) message
  there for free whenever it lands. No cross-task coupling is required.

- **Baseline is green.** `cd api && uv run pytest` → `192 passed in 7.66s`
  (run 2026-08-01, before any change).

- **Order-of-magnitude collision estimate for generated slugs — arithmetic, not
  measurement.** For a case-insensitive three-letter target inside a 7-character
  base62 string: 5 start positions × (2/62)³ ≈ **1 in 6,000** per word. A
  four-letter word is ~1 in 230,000, so three-letter entries dominate the rate
  entirely. A list with a handful of three-letter entries therefore triggers a
  regeneration on the order of once per one-to-two thousand generated links.
  **Treat this as an order of magnitude, not a figure to design against** — it
  is only used here to justify two claims: that rejection is rare enough to
  consume one of the five existing attempts without materially raising the
  `RuntimeError("failed to allocate a unique slug")` risk, and that the
  substring rule costs nothing in practice.

- **UNCONFIRMED: the actual contents of the word list.** This plan deliberately
  does not enumerate the words (see "The list itself" below) — it specifies the
  shape, the size band, the machine-checkable invariants, and who signs off.
  Confirming it means the user reviewing the literal in the pull request.

## API changes — the word list module

**New file: `api/slugwords.py`.** Pure logic, zero `spin_sdk` imports, no
`store` parameter — host-importable under pytest like `links.py` and `bulk.py`,
per `CLAUDE.md`'s testability rule.

It holds one constant and two predicates:

```python
BANNED_SLUG_WORDS: frozenset[str] = frozenset({
    # alphabetized, lowercase, letters/digits only, 3 characters minimum
})

_SEGMENT_SEPARATORS = str.maketrans({"-": " ", "_": " "})


def banned_slug_segment(slug: str) -> str | None:
    """The first whole segment of `slug` that is a banned word, as the caller
    typed it, or None. Segments are split on `-` and `_` and compared
    case-insensitively. Whole-segment matching, not substring: `classic-glasses`
    and `assets-2026` must pass."""
    for segment in slug.translate(_SEGMENT_SEPARATORS).split():
        if segment.lower() in BANNED_SLUG_WORDS:
            return segment
    return None


def contains_banned_word(candidate: str) -> bool:
    """Substring rule, for auto-generated slugs only. A generated slug has no
    separators and therefore no segments, and nobody is attached to a
    particular random string — so there is no false-positive cost here and the
    stricter rule is free."""
    lowered = candidate.lower()
    return any(word in lowered for word in BANNED_SLUG_WORDS)
```

**Two rules, deliberately.** This is the subtlest part of the design and must
not be collapsed later by someone tidying up:

| Path | Rule | Why |
| --- | --- | --- |
| Custom slug (user typed it) | whole segment, case-insensitive | The Scunthorpe problem is real at 32 characters. A false positive here blocks a slug a human chose on purpose and costs a deploy to fix. |
| Generated slug (`allocate_random_slug`) | substring, case-insensitive | There are no segments in a 7-char base62 string, so segment matching would literally never fire. A false positive costs one extra loop iteration and is invisible. |

**Both functions must read the module global at call time** (as written above),
not capture `BANNED_SLUG_WORDS` in a default argument or a closure — the tests
depend on being able to `monkeypatch.setattr(slugwords, "BANNED_SLUG_WORDS", …)`
so behavioural tests do not break every time the list is edited.

### The list itself

**Recommendation: a short curated list — roughly 20 to 40 unambiguous terms —
not an exhaustive profanity corpus.** Reasoning:

- **This is a carelessness guard, not a content-moderation system, and it cannot
  stop a determined insider.** Everyone who can reach `POST /api/links` is an
  authenticated, named employee; anyone intent on publishing something
  embarrassing can pick a word that is not on the list, or a phrase that is not
  a single segment. The value is entirely in catching a slug typed in a hurry
  and a random string that came out badly. State this plainly in `CLAUDE.md` so
  nobody later mistakes it for a guarantee.
- **Every entry is a permanent false-positive liability with a deploy-shaped
  fix.** A corpus of several hundred words — the kind shipped in public
  profanity lists — is full of terms that are also ordinary campaign words in
  some context. Nobody in this repo will curate that, and each false positive
  costs a rebuild and a redeploy of the `api` component.
- **Short entries drive the generated-slug rejection rate**, per the estimate
  above. A three-character minimum keeps the rate negligible; a two-character
  entry would be roughly 40× more likely to fire.

**The plan deliberately does not enumerate the words.** The builder seeds the
constant with unambiguous English profanity and slurs (the kind of minimal list
that appears in every "profanity filter" starter set, trimmed to terms with no
innocent whole-segment reading) plus any internally-embarrassing terms the user
names, and **the user reviews the literal in the pull request**. The word list
is a product/HR judgement, not an engineering one; the engineering deliverables
are the mechanism, the invariants and the tests.

**Machine-checkable invariants**, each enforced by a test in
`api/tests/test_slugwords.py`, because a violating entry is *dead code that
silently never matches*:

- every entry is lowercase (matching lowercases both sides);
- every entry matches `^[a-z0-9]+$` — an entry containing `-` or `_` could never
  equal a segment, since those are the separators;
- every entry is at least 3 characters;
- the set is non-empty.

## API changes — wiring the check in

### `api/links.py`

Two edits, plus `import slugwords` at the top (alongside `import auth`).

**1. `handle_create`, immediately after the existing format check and before the
existence check** (`api/links.py:153-156`). Order matters: the format check must
run first, so segment splitting only ever sees a well-formed slug; the existence
check runs last, so a banned slug is rejected without a KV read.

```python
        if not isinstance(custom_slug, str) or not is_valid_custom_slug(custom_slug):
            return json_response(400, {"error": "invalid_custom_slug"})
        banned = slugwords.banned_slug_segment(custom_slug)
        if banned is not None:
            return json_response(400, {"error": "banned_word_in_slug", "segment": banned})
        if await store.exists(f"slug:{custom_slug}"):
            return json_response(409, {"error": "slug_taken"})
```

**2. `allocate_random_slug`'s loop** (`api/links.py:32-36`) — reject before the
`store.exists` round trip:

```python
    for _ in range(SLUG_GENERATION_ATTEMPTS):
        slug = generate_slug()
        if slugwords.contains_banned_word(slug):
            continue
        if slug not in taken and not await store.exists(f"slug:{slug}"):
            taken.add(slug)
            return slug
```

A rejected candidate consumes one of the five attempts. That is deliberate and
safe given the estimate above; do **not** raise `SLUG_GENERATION_ATTEMPTS` as
part of this change. The existing `RuntimeError` on exhaustion is unchanged.

### `api/bulk.py`

One edit inside `validate_bulk_rows`'s precedence chain (`api/bulk.py:130-146`),
plus `import slugwords`:

```python
        if error_code is None and row.slug:
            if not links.is_valid_custom_slug(row.slug):
                error_code = "invalid_custom_slug"
            elif (banned := slugwords.banned_slug_segment(row.slug)) is not None:
                errors.append({
                    "line": row.line,
                    "slug": row.slug,
                    "error": "banned_word_in_slug",
                    "segment": banned,
                })
                continue
            elif not can_custom_slug:
                error_code = "custom_slug_forbidden"
            …
```

**Precedence: after `invalid_custom_slug`, before `custom_slug_forbidden`.** The
banned-word check is a property of the string the user typed, exactly like the
format check, so it groups with format rather than with permission
(`custom_slug_forbidden`) or with store state (`slug_taken`). The practical
consequence is that two users pasting the same file get identical row errors
regardless of their permissions. This is a low-stakes judgement call — a
permission-less user's rows all fail anyway, so which code shows changes nothing
about the outcome — but it should be a decision on the record rather than an
accident of line order.

The `continue`/append shape mirrors `duplicate_slug_in_submission`'s existing
branch, which is already the module's pattern for an error code carrying an
extra field. Precedence stays "at most one error per row", as documented in
`validate_bulk_rows`'s docstring.

`handle_bulk_create` needs **no** change: it already returns
`400 bulk_validation_failed` with the whole `row_errors` list and writes nothing
when it is non-empty. Auto-generated rows are covered because they route through
`links.allocate_random_slug`.

`handle_bulk_action` needs **no** change and must not gain one — it operates on
slugs that already exist, and re-validating them would break exactly the legacy
links this plan promises not to break.

### Error code and message

**Code: `banned_word_in_slug`. Status: `400`.** A distinct code, not a reuse of
`invalid_custom_slug` — that code's user-facing text is about which *characters*
are allowed, and showing it for a perfectly well-formed slug would send the user
to fix something that is not wrong. `400` matches `invalid_custom_slug`'s class
(a bad value in the request); `409 slug_taken` is about store state and does not
apply.

**Single create** — `400 {"error": "banned_word_in_slug", "segment": "<segment>"}`.
**Bulk row** — `{"line": N, "slug": "<slug>", "error": "banned_word_in_slug", "segment": "<segment>"}`.

**The message names the offending segment. Decided yes**, for three reasons:
these are trusted, authenticated internal staff, not anonymous public
submitters, so coyness buys nothing; a slug can have up to sixteen segments and
an unnamed rejection turns fixing it into a guessing game; and a 50-row bulk
paste is unfixable without per-row specificity. The echoed value is **the user's
own input**, never another entry from the list — so the list itself is not
enumerable through the API except by guessing one word at a time, which is not a
threat model that matters for an internal tool.

No escaping question arises: the returned segment is a segment of a string that
already passed `^[A-Za-z0-9_-]{3,32}$` and was split on `-`/`_`, so it can only
ever match `[A-Za-z0-9]+`. The GUI should still route it through the existing
`escapeHtml`/`textContent` conventions, because that is the house rule, not
because this value needs it.

## GUI changes

Two small edits. No markup, no CSS, no new tokens, no `DESIGN.md` entry — this
adds a string to an existing map, not a visual pattern.

**`gui/app.js`** — one entry in the shared `ERROR_MESSAGES` map (line 149),
alphabetically wherever it reads naturally in that block:

```js
  banned_word_in_slug: "That short link contains a word that isn't allowed — try different wording.",
```

**`gui/dashboard.js`** — in the create-link submit handler (around line 318),
name the segment when the response carries one:

```js
  const { ok, data } = await api.post("/links", payload);
  if (!ok) {
    const overrides = { invalid_password: "Link passwords must be at least 4 characters." };
    if (data && data.segment) {
      overrides.banned_word_in_slug = `"${data.segment}" can't be used in a short link — try different wording.`;
    }
    errorEl.textContent = friendlyError(data, "Could not create link.", overrides);
    return;
  }
```

Assigning the override conditionally (rather than computing a ternary inline) is
exactly equivalent to the shared-map fallback, because `friendlyError` resolves
`(overrides && overrides[code]) || ERROR_MESSAGES[code] || fallback` — an absent
key falls through to the shared entry with no extra code.

**Nothing else in the GUI needs to change.** The dashboard's bulk-create panel
is still an unchecked task in `TASKS.md`; when it lands it calls `friendlyError`
with its own overrides map, so it inherits the shared generic message
automatically. Naming the segment in that table is optional polish, listed under
follow-ups.

## Data model

**No change.** No new KV keys, no new fields on the link record, no new
permission in `auth.KNOWN_PERMISSIONS`. The check is a pure function of the slug
string.

## Effect on existing links

**None, and this is a guarantee the plan makes deliberately.**

- Existing links whose slugs contain a now-banned word **keep resolving**.
  `redirect/main.go` does a bare KV lookup with no shape or content validation
  (confirmed above).
- They stay **fully editable**. `handle_update` only accepts
  `target_url`/`status`/`start_at`/`end_at` and never re-validates the slug;
  `handle_set_password`, `handle_delete`, and `handle_bulk_action` never look at
  slug content either. **Confirmed, not assumed: there is no rename endpoint
  anywhere in `api/app.py`**, so no code path exists that could re-run the check
  against a stored slug.
- **No retroactive scan, no reporting, no flagging.** If someone wants a legacy
  link gone, the existing Delete does it. A regression test pins the "edit a
  legacy banned slug and get a 200" behaviour so a later refactor cannot quietly
  take it away.

## False positives — the main failure mode

Segment matching removes the Scunthorpe class outright, and a short curated list
keeps the rest small, but false positives will happen eventually. The honest
statement of the process:

1. The user sees `"<word>" can't be used in a short link — try different
   wording.` on the create form (or the per-row equivalent in a bulk paste).
2. **Their immediate workaround is to reword the slug.** There is no bypass, no
   override permission, and no admin escape hatch — decision 1 rules all three
   out, and adding one would reintroduce exactly the permission/UI surface the
   decision exists to avoid.
3. They report it to whoever maintains this repo. There is no in-app reporting
   mechanism, deliberately.
4. The maintainer removes or narrows the entry in `api/slugwords.py`,
   **rebuilds** (`cd api && uv run componentize-py -w spin:up/http-trigger@4.0.0
   componentize app -o app.wasm`) **and redeploys**. The list is compiled into
   `api/app.wasm`; there is no runtime knob.

**Step 4 is the accepted cost of decision 1** and should be written into
`CLAUDE.md` in those words, so the trade is visible to whoever hits it. If false
positives ever become frequent enough that this loop is a real burden, that is
the trigger to revisit the KV-backed variant recorded under "Considered and
rejected" — not a reason to quietly bolt on a bypass.

## Tests

All new tests are in `api/tests/`, which already has 192 passing tests and the
in-memory `FakeStore` (`api/tests/fakes.py`). `api/links.py`, `api/bulk.py` and
the new `api/slugwords.py` all keep zero `spin_sdk` imports, so everything stays
host-importable under pytest.

**`api/tests/test_slugwords.py` (new).** Behavioural tests
`monkeypatch.setattr(slugwords, "BANNED_SLUG_WORDS", frozenset({"badword", "worseword"}))`
so they never break when the real list is edited:

- `banned_slug_segment` returns the segment for `black-friday-badword`,
  `BADWORD`, `badword_promo`, `a-badword-b`;
- returns `None` for `classic-glasses` and `assets-2026` (the Scunthorpe cases,
  named explicitly with a comment saying why they are there) and for
  `badwordly` / `xbadword` (substring, not a whole segment);
- returns the segment **as typed**, so `Black-BADWORD` yields `BADWORD`;
- `contains_banned_word` is `True` for `xxbadwordxx` and `XBADWORDX` and `False`
  for `abcdefg`.

Plus the four invariant tests over the **real** constant (lowercase,
`^[a-z0-9]+$`, ≥3 characters, non-empty).

**`api/tests/test_links.py`.** Using the existing `_principal(...)`/`_request(...)`
helpers and monkeypatching the list the same way:

- `handle_create` with a banned custom slug → `400`, body
  `{"error": "banned_word_in_slug", "segment": …}`, **and nothing written** —
  assert `await store.exists("slug:<slug>")` is `False` and `all_links` is
  absent/unchanged;
- the banned check runs *after* the format check (a slug that is both malformed
  and banned reports `invalid_custom_slug`);
- the banned check runs *after* the permission check (a principal without
  `links.create_custom_slug` still gets `403 forbidden`);
- `allocate_random_slug` skips a banned candidate: monkeypatch
  `links.generate_slug` to return a banned string once and a clean one after,
  assert the returned slug is the clean one — deterministic, no retry flake;
- **legacy-slug regression:** seed `FakeStore` with a link whose slug contains a
  banned word, `handle_update` its `target_url` → `200` and the record updates.

**`api/tests/test_bulk.py`.**

- `validate_bulk_rows` flags a banned slugged row with
  `{"line", "slug", "error": "banned_word_in_slug", "segment"}`;
- precedence: a row that is both banned and already in `existing_slugs` reports
  `banned_word_in_slug`, and a malformed-and-banned row reports
  `invalid_custom_slug`;
- `handle_bulk_create` with one banned row among three good ones returns `400
  bulk_validation_failed` and the store is byte-identical afterwards (the same
  assertion shape the existing all-or-nothing test uses);
- `handle_bulk_action` on an existing link with a banned slug still succeeds —
  the legacy-link guarantee, at the bulk layer.

No `gui-pages` or Go tests are affected. `gui-pages/tests/test_no_inline_code.py`
is unaffected because no markup changes.

## Trade-offs and rejected alternatives

**Folding the check into `is_valid_custom_slug`.** Genuinely tempting: one
function, both call sites already correct, zero new imports in `bulk.py`, and
the whole feature becomes a three-line diff. It loses on the error surface,
which is the thing that actually matters here. Both callers only learn
"true/false", so both must report `invalid_custom_slug` — a code whose GUI text
is *"Custom short links can only use letters, numbers, hyphens, and underscores
(3–32 characters)"* (`gui/app.js:154`). Telling someone that about
`black-friday-<word>`, which satisfies every one of those rules, sends them to
fix a problem that does not exist. It is worse still in bulk, where the per-row
error table is the *only* feedback and a wrong reason on one row of fifty is
genuinely hard to recover from. A separate predicate costs one import and one
`if`.

**Plain substring matching on custom slugs.** The obvious implementation and the
one a two-line version would reach for. Rejected (and pre-rejected by the user's
decision 3) because of the Scunthorpe problem: on a 32-character slug, substring
matching against any realistic list rejects legitimate campaign slugs —
`classic-glasses`, `assets-2026`, and a long tail nobody can predict — and each
one costs a deploy to unblock. Note carefully that this rejection applies to
custom slugs **only**: for generated slugs substring is the *chosen* rule,
because there are no segments to match and no false-positive cost.

**Leetspeak / homoglyph normalization** (`4`→`a`, `0`→`o`, `$`→`s`, Cyrillic
lookalikes). This is the obvious next request and it is a no, on four counts.
(1) It is an arms race with no end state — every normalization rule invites the
next substitution. (2) It multiplies false positives precisely where they are
most expensive: normalizing digits to letters makes real campaign slugs like
`s4le` and `4th-of-july` collide with the list, and the whole point of segment
matching was to *reduce* false positives. (3) It implies a guarantee the feature
does not make — the moment the check looks clever, people assume it is
comprehensive. (4) It defends against a deliberate evader, who is explicitly
outside the threat model: an authenticated named employee determined to publish
something embarrassing has a dozen easier routes. Revisit only if there is a
real incident of deliberate evasion, and then as a moderation policy decision,
not a regex.

**A KV-backed, admin-editable list with a management UI.** Ruled out by the
user's decision 1, and the reasoning holds: it needs a store or key namespace, a
CRUD endpoint set, a new entry in `auth.KNOWN_PERMISSIONS`, a new admin page in
`gui/admin/`, and a KV read on every link creation — for a list that will change
perhaps twice a year, in an app whose product principle 2 is "self-hosting only
pays for itself if it stays operationally simple". The accepted cost is that
fixing a false positive needs a rebuild and redeploy. Because the check sits
behind `slugwords.banned_slug_segment` / `contains_banned_word`, converting
later means changing that module's internals (and threading a `store` parameter
through two call sites) — not hunting down inlined comparisons.

**Retroactively scanning existing links.** Attractive as a one-off cleanup, and
it would answer "what do we already have out there?" Rejected: there is no
rename path, so the only available action on a flagged legacy link is deletion,
which breaks every QR code and printed URL already in circulation. A read-only
report would be more defensible, but it needs a new endpoint and a new admin
surface for a question that can be answered by eyeballing the dashboard once.

**Not naming the offending segment** (`That short link contains a word that
isn't allowed`). Standard practice for public-facing filters, where naming the
match helps an attacker probe the list. It loses here because the audience is
authenticated internal staff, the echoed text is the user's own input rather
than a disclosure from the list, and a slug can hold up to sixteen segments — an
unnamed rejection turns a fix into a guessing game, and does so worst in the
50-row bulk case. The generic message is retained anyway, as the shared
`ERROR_MESSAGES` fallback for any surface that has no `segment` to hand.

**Doing nothing.** A live option, and defensible for custom slugs alone: a human
types every one of them, and the same human is accountable for it. It loses on
the *generated* path, which has no human in the loop at all — a random base62
string is the one thing here that can produce genuinely surprising output on a
printed QR code, and it is also the cheapest thing in the world to guard, since
`allocate_random_slug` already has a retry loop.

**A `mode=`-flagged single predicate instead of two functions.** The user's
decision 1 says "a single predicate function". Two named predicates were chosen
instead because the two matching rules are genuinely different and a boolean
parameter at the call site (`banned(slug, whole_segments=True)`) reads worse and
invites passing the wrong value. The constraint that is actually load-bearing —
one module, one constant, so a future KV move is small and localized — is fully
honoured. Trivially reversible if the user prefers the literal reading.

## Tasks

Appended to `TASKS.md` under a new `## Banned-word slug check` heading:

```
- [ ] Add api/slugwords.py with the banned-word list and its two predicates (must land before every other task in this section) — file(s): api/slugwords.py (new), api/tests/test_slugwords.py (new) — done when: `BANNED_SLUG_WORDS` is a module-level `frozenset` of 20–40 lowercase, alphabetized entries seeded per docs/plans/banned-word-slugs.md ("The list itself" — unambiguous terms only, 3-character minimum, contents flagged in the PR for the user to review, not chosen unilaterally); `banned_slug_segment(slug) -> str | None` splits on `-`/`_` and returns the first whole segment that matches case-insensitively, as the caller typed it; `contains_banned_word(candidate) -> bool` does a case-insensitive substring match for generated slugs; both read the module global at call time so tests can monkeypatch it; the module has zero `spin_sdk` imports and takes no `store`; `cd api && uv run pytest` passes with new tests covering `classic-glasses` and `assets-2026` returning None, `black-friday-<word>` and `BADWORD` matching, `badwordly`/`xbadword` not matching as segments but matching under `contains_banned_word`, and four invariant tests over the real constant (all lowercase, all `^[a-z0-9]+$`, all ≥3 chars, non-empty).
- [ ] Reject banned custom slugs and regenerate banned auto-slugs in api/links.py (depends on the slugwords task) — file(s): api/links.py, api/tests/test_links.py — done when: `handle_create` returns `400 {"error": "banned_word_in_slug", "segment": "<segment>"}` for a banned custom slug, checked after `is_valid_custom_slug` and before `store.exists` so nothing is written and no KV read is spent; `allocate_random_slug` calls `contains_banned_word` on each candidate before its `store.exists` call and `continue`s on a hit, with `SLUG_GENERATION_ATTEMPTS` left at 5 and `generate_slug` unchanged; `cd api && uv run pytest` passes with tests that a banned custom slug writes no `slug:` key and no `all_links` entry, that a malformed-and-banned slug still reports `invalid_custom_slug`, that a principal without `links.create_custom_slug` still gets `403 forbidden`, that `allocate_random_slug` returns the clean slug when `generate_slug` is monkeypatched to yield a banned one first, and that `handle_update` on a pre-existing link whose slug contains a banned word returns 200.
- [ ] Report banned slugs per row in api/bulk.py (depends on the slugwords task; independent of the links.py task) — file(s): api/bulk.py, api/tests/test_bulk.py — done when: `validate_bulk_rows` appends `{"line", "slug", "error": "banned_word_in_slug", "segment"}` for a slugged row whose slug contains a banned segment, positioned after `invalid_custom_slug` and before `custom_slug_forbidden` in the precedence chain and still at most one error per row; `handle_bulk_create` and `handle_bulk_action` are unchanged; `cd api && uv run pytest` passes with tests that a banned-and-taken row reports `banned_word_in_slug`, a malformed-and-banned row reports `invalid_custom_slug`, a submission of three good rows plus one banned row returns `400 bulk_validation_failed` and leaves the store byte-identical, and `handle_bulk_action` on an existing link with a banned slug still succeeds.
- [ ] Surface the banned-word error in the GUI (depends on the links.py task) — file(s): gui/app.js, gui/dashboard.js — done when: `ERROR_MESSAGES` in gui/app.js gains `banned_word_in_slug: "That short link contains a word that isn't allowed — try different wording."`; the create-link handler in gui/dashboard.js builds its overrides object before calling `friendlyError` and adds `banned_word_in_slug: '"<segment>" can\'t be used in a short link — try different wording.'` only when `data.segment` is present, so a response without one falls through to the shared entry; no markup, CSS or token changes; `cd gui-pages && uv run pytest` still passes (no inline code introduced); submitting a banned custom slug in a real browser shows the segment-naming message on the create form and creates nothing.
- [ ] Document the banned-word slug check in CLAUDE.md and PRODUCT.md — file(s): CLAUDE.md, PRODUCT.md — done when: CLAUDE.md gains a short "Banned words in slugs" section (peer to "Time-windowed links") stating the two matching rules and why they differ, that the list is a static `frozenset` in `api/slugwords.py` behind two predicates so a future KV move is localized, the `banned_word_in_slug` error code and its `segment` field, that it is a carelessness guard rather than a content-moderation system and cannot stop a determined insider, that existing links are unaffected and slugs are immutable (no rename endpoint exists), and that fixing a false positive requires editing the list plus a rebuild and redeploy with no bypass or override permission; PRODUCT.md's Capabilities list gains one accurate line; DESIGN.md and .impeccable/design.json are deliberately untouched (no new visual pattern); no doc claims a capability the shipped code does not have.
- [ ] End-to-end manual verification of the banned-word slug check — file(s): (none — verification step) — done when: with `SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false spin up --build --runtime-config-file runtime-config.toml` running, creating a link with a banned custom slug shows the segment-naming message with zero console errors and no new row in the table; `classic-glasses` and `assets-2026` both create successfully; a link created with no custom slug still succeeds; a pre-existing link whose slug contains a banned word still redirects at `/r/<slug>` and can still have its destination edited from the dashboard; and `cd api && uv run pytest`, `cd gui-pages && uv run pytest` and `cd redirect && go test ./linkgate/...` all pass.
```

## Critical files

- `api/slugwords.py` (new)
- `api/tests/test_slugwords.py` (new)
- `api/links.py`
- `api/bulk.py`
- `api/tests/test_links.py`
- `api/tests/test_bulk.py`
- `gui/app.js`
- `gui/dashboard.js`
- `CLAUDE.md`
- `PRODUCT.md`
- `TASKS.md`

Not touched, deliberately: `redirect/` (no slug validation on the hot path),
`api/app.py` (no new route), `spin.toml`, `DESIGN.md`,
`.impeccable/design.json`, `Jenkinsfile` (test invocation is unchanged).

## Verification

1. `cd api && uv run pytest` — must go from the confirmed 192-test baseline to
   192 plus the new tests, with zero failures.
2. `cd gui-pages && uv run pytest` — unchanged pass count; proves no inline code
   was introduced by the GUI edit.
3. `cd redirect && go test ./linkgate/...` — unchanged. (Never `go test ./...`;
   it fails by design on `package main`.)
4. Start the app:
   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```
5. In a real browser at `http://localhost:3000/dashboard.html`, with the console
   open: expand **More options**, enter a destination and a custom slug of the
   form `promo-<banned word>`, submit. **Pass:** the create form shows
   `"<word>" can't be used in a short link — try different wording.`, no row is
   added to the table, and the console is clean (in particular, zero CSP
   violations).
6. Same form, custom slug `classic-glasses`, then `assets-2026`. **Pass:** both
   create successfully — these are the Scunthorpe regression checks and the
   whole reason for segment matching.
7. Submit with no custom slug at all, three or four times. **Pass:** every one
   succeeds with a normal 7-character slug (this exercises
   `allocate_random_slug`'s new branch on the non-rejecting path; an actual
   rejection is far too rare to trigger on demand and is covered by the
   monkeypatched unit test instead).
8. Legacy-link check, which the plan promises does not regress: use the
   dev KV explorer (`./dev/kv-explorer-up.sh`, see `CLAUDE.md`) or a direct
   `curl` create performed *before* the list is wired in, to get a link whose
   slug contains a banned word into the store. Then `curl -i
   http://127.0.0.1:3000/r/<that-slug>` → **302**, and edit its destination from
   the dashboard → **saves, 200**.
9. Bulk endpoint, by `curl` (the dashboard's bulk panel does not exist yet).
   With a session cookie and CSRF header taken from the browser's dev tools:
   ```bash
   curl -i -X POST http://127.0.0.1:3000/api/links/bulk \
     -H 'content-type: application/json' -H "x-csrf-token: <token>" \
     -b 'session=<cookie>' \
     --data '{"text":"good-slug,https://example.com/a\npromo-<banned>,https://example.com/b\n"}'
   ```
   **Pass:** `400` with
   `{"error":"bulk_validation_failed","row_errors":[{"line":2,"slug":"promo-<banned>","error":"banned_word_in_slug","segment":"<banned>"}],"row_count":2}`
   — and `GET /api/links` afterwards shows **neither** row was created.

## Out of scope / follow-ups

- **Destination-URL filtering.** Belongs entirely to the existing Future-work
  entry "Multi-domain short-link hosting + admin-managed destination domain
  allow/deny-list". Not duplicated, not partially anticipated here.
- **Naming the offending segment in the bulk-create panel's error table.** That
  panel is still an unchecked task in `TASKS.md`'s `## Bulk link management`
  section; when it lands it will render the shared generic message via
  `friendlyError` with no extra work. Reading `segment` out of the row error to
  name the word there is optional polish for whoever builds it — not a
  dependency, and not worth its own task line.
- **An admin-editable list**, a bypass permission, an in-app false-positive
  report, and a retroactive scan of existing slugs — all recorded under
  `TASKS.md`'s `## Considered and rejected` with their revisit triggers.
- **Reserved slugs** (e.g. forbidding `admin`, `api`, `login` as slugs).
  Currently a non-problem: every short link lives under the `/r/` prefix, which
  cannot collide with any GUI or API route. If that ever changes,
  `api/slugwords.py` is the natural home for a second constant and a third
  predicate. Not scheduled, and not worth a Future-work entry until the routing
  actually changes.
