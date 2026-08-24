# Tag UX: Chip Input and Multi-Tag Filtering

## Context

Two of the three tag Future-work entries filed 2026-08-03 while planning
`docs/plans/link-tags-and-ownership.md` (`TASKS.md:421` and `TASKS.md:422`) are
being picked up together, because they turn out to share one component:

1. **A real tag-input component (chips with individual remove buttons)**
   replacing the comma-separated text input. What shipped is a plain
   `<input type="text">` plus a single shared `<datalist id="tag-suggestions">`
   whose options are rewritten on every keystroke with everything up to and
   including the last comma prepended (`refreshTagDatalist`,
   `gui/dashboard.js:191-198`). That is a trick around the fact that
   `<datalist>` prefix-matches the input's **whole** value, so per-token
   autocomplete is otherwise impossible in a comma-separated field. The entry's
   own words: "a chip input is the honest control."
2. **Multi-select / AND-OR tag filtering** replacing the single-select
   `#tag-filter` (`gui/dashboard.html:107`). The entry states the crux: *"the
   combining rule is the actual decision, not the widget."*

Entry 1 was filed as "blocked on the same question as clickable tag chips": a
per-chip remove button inherits DESIGN.md's sitewide 44px tap-target floor
(`DESIGN.md:203`), and `DESIGN.md:213` records the in-row chips being kept inert
for exactly that reason. **That blocker is resolved in this plan rather than
deferred** — see "The 44px tap-target floor" below. Entry 1 also said "pick it
up only if someone actually complains about the text input"; that trigger has
now fired (the user asked for it), which is recorded rather than assumed.

The third entry — **renaming a tag across every link (`TASKS.md:423`) — is out
of scope** and must not grow out of this work. It needs the whole-tag
server-side action rejected on 2026-08-03 (`TASKS.md:258`) and would write once
per affected link against Akamai's 50 writes/second cap.

**Confirmed decisions (settled by the user before planning):**

- Both features are **pure client-side**. No API change, no new endpoint, no
  writes, no new KV key type. `GET /api/links` has no pagination, so
  `allLinks` already holds every visible link.
- **No new served files.** The `gui` static component serves exact routes only
  (a wildcard 404s, confirmed live), so everything lands in `gui/dashboard.html`,
  `gui/dashboard.js`, `gui/dashboard.css` and `gui/theme.css`, all already
  routed.
- **Zero inline code**: no inline `<script>`/`<style>`/`style="…"`/`on*=`.
  Hiding is the native `hidden` attribute plus `theme.css`'s
  `[hidden] { display: none !important; }`.
- The in-row table chips **stay inert `<span>`s**. DESIGN.md rejected clickable
  in-row chips on the record and this plan does not reopen it.
- **No new nav items.** The nav is at its item budget (`DESIGN.md:254`).
- Tag vocabulary stays server-owned (`api/tags.py`); the client mirrors it as
  *feedback* only.
- Suggestions stay **ownership-scoped** — derived from `allLinks`, never a
  global tag list.
- The 10-tag cap must be **visible**, never a silently dropped 11th tag.
- Whatever is decided about the 44px floor is **a DESIGN.md change written up as
  one**, with reasoning, not slipped in.
- Measurement is the standard: 1400/768/480/390px in both themes.
- No deploy is planned; verification is local `spin up` plus browser.

## Key technical facts confirmed during research

- **`refreshTagDatalist`'s prefix trick exists solely because the field holds
  more than one token.** `gui/dashboard.js:186-198`, whose own comment says so.
  Once the input's whole value is exactly one token, `<datalist>` prefix-matches
  correctly with no rewriting — so **the chip input retires the trick by
  construction, not by replacing it with a bigger one.** This is the single
  most consequential fact in the plan: no custom ARIA combobox is needed.
- **The 44px floor is already `min-width` as well as `min-height`, and the
  repo's own phrasing is "the guideline is a target, not a height."**
  `gui/theme.css:880-888` (`button, [type=submit], [type=button], [type=reset],
  [role=button] { min-width: 44px; }`) with that comment, plus
  `TASKS.md:851`, the completed audit line: *"172 interactive elements across
  all five pages, zero under the floor."*
- **The "hit area ≥ 44px, ink stays small" pattern is already shipped, named and
  measured.** `.checkbox-hit` (`gui/theme.css:916-924`): *"The visual box stays
  20px; only the target grows"* — verified 44×44 in `TASKS.md:851`. Sibling
  precedents in the same file: `label:has(> input[type=checkbox])`,
  `details > summary { padding-block: 0.875rem }`, `#links-table th.sortable
  { height: 44px }`, `.operator-link`, `.slug-chip-link`.
- **DESIGN.md is out of date on this.** `DESIGN.md:203` records only
  `min-height: 44px` on the button family plus the nav-anchor extension. It does
  **not** record `min-width`, the checkbox/radio exclusion, `.checkbox-hit`,
  the `input`/`select`/`textarea` coverage, `<summary>`, `th.sortable`,
  `.operator-link` or `.slug-chip-link` — all of which are in `gui/theme.css`
  today. Confirmed by reading both files. The DESIGN.md task below is therefore
  partly a **catch-up on shipped behaviour**, and must be written from
  `theme.css`, not invented.
- **A `<button>` inside a chip needs no new CSS to meet the floor**, and no
  exemption: the sitewide `button` rule already gives it `min-height: 44px;
  min-width: 44px`. Confirmed by reading `gui/theme.css:875-888`.
- **Pico sets `width: 100%` on `input:not([type=checkbox],[type=radio]), select,
  textarea` but NOT on plain `button`.** Confirmed by `grep -o
  '[^}]\{0,60\}width:100%[^}]\{0,40\}' gui/vendor/pico.min.css`. So the chip
  buffer input needs `width: auto` (the same override `.domain-select` already
  carries — `DESIGN.md:253`), and a `type="button"` chip does not.
- **Pico's `button` rule sets `--pico-background-color: var(--pico-primary-background)`,
  `--pico-border-color`, `--pico-color` and its own padding.** Confirmed by
  `grep -o 'button{[^}]\{0,300\}' gui/vendor/pico.min.css`. A chip rendered as a
  `<button>` must neutralise fill/border/padding by class (specificity `0,1,0`
  beats element `0,0,1`, so no specificity fight).
- **`[role=group]` is heavily styled by Pico**, which is why
  `#bulk-bar [role=group] { width: auto }` and `#links-table [role=group]`
  exist (`gui/dashboard.css:24-26, 193-197`). A chip container must therefore
  **not** be a `role="group"`.
- **`gui-pages/tests/test_no_inline_code.py` scans `gui/**/*.js` for
  `\bstyle\s*=`**, not just the HTML pages (`test_script_has_no_style_attribute_in_templates`,
  and `SCRIPTS` is a glob so new code is covered automatically). `el.style.display = …`
  does not match the regex (the `=` is not adjacent to `style`) and is already
  used at `gui/dashboard.js:615`; `el.style = …` would match. Baseline
  confirmed: `cd gui-pages && uv run pytest` → **108 passed**.
- **`.tag-chip` carries no background, no border and no radius.**
  `gui/theme.css:569-586`: `display: inline-block; margin-left: 0.35em;`
  sans-serif, `0.75rem`, `font-weight: 600`, `color: var(--ss-slate-500)`. It is
  two selector-list memberships on the `.slug-kind-badge`/`.lock-badge` rules,
  exactly as `docs/plans/link-tags-and-ownership.md` specified.
- **`--ss-slate-500` (`#526078`) is measured at 5.6:1 on the table-cell
  background** (`gui/theme.css:123-130`), and its dark counterpart `#a8b6cc` at
  7.58:1 on card / 8.83:1 on cells (`DESIGN.md:139`). Reusing it for chip ink
  needs no new contrast measurement **on a cell**; on the card/field background
  it is a different background and **must be re-measured** (this is the exact
  "measured against the wrong assumed background" mistake DESIGN.md records
  twice).
- **`--pico-muted-border-color` measures 1.36:1 vs card in light and 1.62:1 in
  dark** (`DESIGN.md:135`) — accepted for card borders. WCAG 1.4.11's 3:1
  applies to a boundary *required to identify an interactive control*;
  `DESIGN.md:253` records the domain `<select>` being fixed for exactly this at
  1.10:1. UNCONFIRMED whether the chip's hairline needs the same treatment —
  the ink and the `×` glyph carry the affordance here, not the border. The
  measurement task below settles it; if the border is the only identifying
  boundary in either theme, apply the `rgba(255,255,255,0.4)` /
  `--pico-border-color` fix `#logout-btn` and `.domain-select` already use
  rather than inventing anything.
- **Slugs are safe as HTML `id` fragments.** `links.CUSTOM_SLUG_PATTERN` is
  `^[A-Za-z0-9_-]{3,32}$` and auto-generated slugs are base62, so
  `id="edit-tags-${slug}"` needs no escaping beyond the `escapeHtml` already
  applied everywhere in `editRowHtml`.
- **Nothing outside `gui/dashboard.{html,js}` references `#tag-filter` or
  `#tag-suggestions`.** Confirmed: `grep -rn "tag-filter\|tag-suggestions"
  gui/ gui-pages/` returns nothing else. No test, no other page, no deep link.
- **There are five tag-entry surfaces today**, all four authoring ones sharing
  `parseTagsInput` and the one shared datalist: `#link-tags`
  (`dashboard.html:52`), `#bulk-tags` (`:90`), `.edit-tags`
  (`dashboard.js:359`, one per editable row), `#bulk-tag-input` (`:136`), plus
  the `#tag-filter` select.
- **`ERROR_MESSAGES` already holds the exact copy for every tag failure**
  (`gui/app.js:174-177`: `invalid_tag`, `invalid_tags`, `too_many_tags`,
  `no_tags`). Client-side feedback must reuse `friendlyError` rather than
  writing a second wording of the same rule.
- **`api/tags.py` constants**: `MAX_TAGS_PER_LINK = 10`,
  `MAX_TAG_LENGTH = 32`, `TAG_PATTERN = ^[a-z0-9][a-z0-9_-]*$`,
  `normalize_tag` = `strip().lower()`. `parse_tags` de-duplicates and **sorts**.
- **`BULK_MAX_SELECTION = 50` is the repo's precedent for a mirrored constant**
  (`gui/dashboard.js:112-118`): a comment saying the server is authoritative and
  that drift's only symptom is a rejection naming the real limit, with **no
  cross-language test pin**. Tag constants follow the same convention — see
  "Why no cross-language pin" below.
- **`getVisibleLinks()` reads its filters straight from the DOM** on every call
  (`gui/dashboard.js:307-325`), and `renderLinksTable()` clears `selectedSlugs`
  at the top so a filter change can never leave a stale selection
  (`:433-440`). Both properties are relied on below and neither changes.

## The 44px tap-target floor — the blocker, resolved

**Decision: the input chip is itself a `<button type="button">`, it takes the
sitewide 44×44 floor with no exemption and no new CSS to reach it, and its ink
stays byte-identical to the inert in-row `.tag-chip`.**

The three options put to the planner, and why this is the answer:

- *"44px tall, compliant but visually heavy inside a form field"* — the premise
  is wrong about the cost. `gui/theme.css:892-897` already puts
  `min-height: 44px` on every `input`/`select`/`textarea`, so **the field row is
  already 44px tall.** A 44px chip sitting beside a 44px input adds zero height
  to a single-chip row and reads as one field row, not as weight. Only *wrapped*
  rows add height, and that only happens once someone has several long tags.
- *"hit area reaches 44px while the visual chip stays smaller"* — attractive,
  and it is exactly `.checkbox-hit`'s shipped pattern, but it does not
  transfer. `.checkbox-hit` works because the 44×44 wrapper **is** the layout
  box and the 20px checkbox sits inside it. Making a small chip's hit area
  *overflow* its own box (a `::after` overlay or negative margins) would put
  adjacent chips' hit areas on top of each other, so a tap near a boundary
  removes the wrong tag. That is a worse accessibility outcome than the thing it
  optimises, so it is rejected on its own merits, not on effort.
- *"a deliberate, recorded amendment to the design system"* — **no exemption is
  needed, so none is taken.** What *is* needed is a recorded DESIGN.md
  amendment stating the distinction, plus the catch-up on the floor's real
  shipped shape (see the DESIGN.md section). Narrowing the floor is refused:
  DESIGN.md's history is of the floor being *extended* twice (nav anchors at
  38.4px, then `<select>`), never narrowed.

**Why the same rule reads differently in a table row and in a field, without
being a different rule.** `TASKS.md:262` rejected clickable in-row chips partly
because *"the `tabindex="0"` escape hatch would add ten focus stops to every
row."* That arithmetic is the whole distinction, and it is about *context*, not
about the rule: ten chips across thirty rows is up to **300 focus stops in a
table nobody asked to interact with**, while ten chips inside an edit form the
user deliberately opened is **ten stops in a form that already has eight
controls**. Same 44px rule, same `<button>` element, opposite verdicts, for a
reason that can be stated in one sentence. The in-row chips stay inert.

**Consequence worth stating plainly:** a chip is `label + ×` inside a
`min-width: 44px` button, so a short tag like `#q4` renders a ~44px-wide chip
with ~24px of ink. That airiness is the app's own rule doing what it was
extended to do (`min-width` was added precisely because a 39.8px-*wide* Edit
button had slipped through), and overriding it here would be the exemption this
plan refuses.

**The chip is the button; there is no nested `×` button.** Rejected
alternative: `<span class="tag-chip">#sale<button class="tag-chip-remove">×</button></span>`.
It doubles focus stops (20 for a 10-tag form), puts a `min-width: 44px` button
next to a ~35px label so the chip is mostly the button anyway, and needs a
second CSS block for the nested control — for no accessibility gain, since a
chip inside an input has exactly one action.

## The filtering decision

**Rule: all-of (AND) is the default; the viewer can switch to any-of (OR); the
active rule is always stated in words.**

**Why all-of by default — three independent reasons, the third decisive:**

1. **Monotone narrowing.** Every chip added can only shrink the result set,
   which is what "filter" means. Any-of *grows* the set as you add tags, so
   adding a second tag returns *more* rows — the surprise this entry exists to
   avoid.
2. **Composition consistency.** The tag filter already ANDs with `#links-filter`
   and `#owner-filter` (`gui/dashboard.js:307-325`). If chips OR'd among
   themselves while ANDing with everything else, one filter row would carry two
   combining semantics with nothing on screen distinguishing them.
3. **The filtered set feeds bulk actions, and any-of over-matches.**
   Select-all is scoped to the filtered set (`DESIGN.md`'s Do's), and the bulk
   bar offers Delete and Reassign. An operator filtering `q4` + `sale` intending
   "the q4 sale links", getting every q4 link plus every sale link, then
   select-all → Delete, deletes links they never meant to. All-of can only ever
   *under*-match, which is recoverable. **This is the same reasoning that
   rejected owner substring-matching on 2026-08-04** (`TASKS.md:272`: *"the
   filtered set feeds a bulk reassign"*), applied to the same table.

**Why switching is still offered:** "everything in either campaign" is a real
question, and refusing it just makes people filter twice and lose the ability to
select the union at all. Making it *explicit and visible* is the answer, not
forbidding it.

**The control is a two-option `<select id="tag-rule">`, not a segmented
button group.** Grounds: a `<select>` is already the established affordance in
that exact row (`DESIGN.md:213`, `#owner-filter`); it is narrower than two
`min-width: 44px` buttons in a row DESIGN.md has already measured as tight
(the links table has overflowed its container once at a realistic desktop
width, `DESIGN.md:211`); it is one focus stop rather than two; and it needs no
`aria-pressed` bookkeeping and no new CSS class. Its rendered value *is* the
statement of the rule, which is the property that matters most here.

**How the rule is visible — two layers, because an implicit combining rule is a
filter that lies:**

1. The select sits **immediately before** the chips so the control reads as a
   sentence: `[All of these tags ▾] #q4 × #sale ×`.
2. A `#filter-summary` line under the filter row states the whole active filter
   in words plus the count: `Showing 7 of 42 links · tags: all of #q4, #sale ·
   owner: alice · text: "promo"`. Hidden when no filter is active.
3. The **empty state names the rule**, because the moment a user is most
   confused by all-of is the moment it returns zero rows. When ≥2 chips are
   active and the result is empty, the existing "No links match your filter."
   copy gains `No link carries all of #q4, #sale.` plus, for all-of only, `Try
   "Any of these tags".`

**`#tag-rule` is hidden when fewer than two chips are active**, because with one
chip all-of and any-of are provably identical, so a visible control that cannot
change the result is clutter. This is the established precedent in this row and
this nav: `#owner-filter-wrap` hides below two owners, and the domain selector
is *"hidden whenever fewer than 2 domains are on offer (a one-option selector
is pure clutter)"* (`DESIGN.md:253`). A hidden `<select>` keeps its `value`, so
the chosen rule survives being hidden and re-shown within the session.

**The rule is in-memory only — deliberately not persisted to `localStorage`.**
Theme and domain persist because they are viewer *preferences*; a filter is
transient, and neither `#links-filter` nor `#owner-filter` persists today.

**Composition with the existing filters, stated so it is not re-derived:**
text AND tags AND owner, with the tag chips combined among themselves by
`#tag-rule`. `#links-filter` keeps matching tags as substrings (it does today —
`gui/dashboard.js:315`) and that is left alone: it is a different question
("does any tag contain q4") from the chip filter's ("does this link carry tag
q4"). **`?owner=`'s consume-once behaviour is untouched** (`pendingOwnerFilter`,
`gui/dashboard.js:101`), and **no new URL parameter is added** — a `?tags=`
deep link is listed under follow-ups.

**One deliberate behaviour change to call out:** `#tag-filter`'s options were
rebuilt from `allKnownTags()` on every `loadLinks()`, so a tag that stopped
existing silently reset the filter (`rebuildTagFilterOptions`,
`gui/dashboard.js:132-138`). Chips are not rebuilt from the data, so a chip for
a tag no longer in use **stays**, and the table says nothing matches. That is
more honest — the viewer set that filter and nothing silently undid it — and it
is why the empty state has to name the tags.

## GUI changes

Every change lands in files that already exist and are already routed. **No new
`.js`/`.css` file, no `spin.toml` change, no new route, no new nav item.**

### `gui/dashboard.js` — the shared chip-input component

One component, five placements, all handlers **delegated on `document`**
(matching the existing `document.addEventListener("input", …)` at
`gui/dashboard.js:202-204`) so a per-row edit form costs no per-instance
listeners.

**Markup contract.** A placement is a container plus a buffer input:

```html
<div class="tag-input" data-max="10" data-note="link-tags-note"
     data-original-tags="sale q4">
  <button type="button" class="tag-chip tag-chip-editable" data-tag="sale"
          aria-label="Remove tag sale">#sale <span aria-hidden="true">&times;</span></button>
  <input type="text" id="link-tags" class="tag-input-buffer"
         list="tag-suggestions" placeholder="Add a tag&hellip;" />
</div>
<p id="link-tags-note" class="form-note" role="status" hidden></p>
```

- Chips precede the buffer, so they accumulate left and the buffer stays last.
- `data-max` is **per placement**: `"10"` on the four authoring placements,
  **absent on the filter** (filtering by eleven tags is pointless but harmless,
  and the cap is a per-link storage rule, not a filter rule).
- `data-note` names the message element's id — an explicit link rather than DOM
  adjacency, which a future markup edit would silently break.
- `data-original-tags` is a space-joined snapshot of what the placement was
  initialised with (tags cannot contain spaces, the same reasoning the CSV
  column's space join already uses). Only the edit rows need it; see
  "The tag-wipe guard".
- **`role="group"` is not used** on the container: Pico styles it as a joined
  button group (see the confirmed facts).

**New functions.**

| Function | Behaviour |
|---|---|
| `readTagChips(container)` | `[...container.querySelectorAll(".tag-chip-editable")].map(el => el.dataset.tag)` — the DOM is the single source of truth, so there is no parallel JS state to desync across a `renderLinksTable()` rebuild. |
| `splitTagTokens(value)` | Splits on `/[\s,]+/`, trims, lowercases, drops empties, de-duplicates. **This is the renamed `parseTagsInput`** — the new name stops it reading as "parse the whole field", which it no longer is. Whitespace is a safe delimiter because `TAG_PATTERN` excludes it, so a pasted `"sale, q4 email"` yields three tokens. |
| `addTagChip(container, token)` | Normalise → validate → de-duplicate → cap-check → insert before the buffer. Returns a reason string on refusal (`"invalid"`, `"cap"`) or `null` on success/no-op. A duplicate is a silent no-op, not an error. |
| `removeTagChip(button)` | Removes the chip, moves focus to the container's buffer input, and fires the changed hook. |
| `commitTagBuffer(container)` | `splitTagTokens(buffer.value)`, `addTagChip` each in order, clear the buffer of everything consumed; on the first refusal, leave the offending text in the buffer and show the note. |
| `clearTagChips(container)` | Removes every chip and empties the buffer. Used by the create/bulk-create resets and after a successful bulk tag action. |
| `tagChipsChanged(container)` | The one hook. Updates that placement's note, refreshes `#tag-suggestions`, and **only when `container.id === "tag-filter-chips"`** also toggles `#tag-rule-wrap`, calls `renderLinksTable()` and `updateFilterSummary()`. Scoping this is load-bearing: an unscoped re-render would rebuild the table out from under an open edit form the moment a tag was typed into it. |
| `refreshTagSuggestions(buffer)` | Replaces `refreshTagDatalist`. Rebuilds `#tag-suggestions` from `allKnownTags()` minus the chips already in that buffer's container. **No prefix arithmetic** — the buffer's whole value is one token, which is the whole point. |
| `isValidTagToken(token)` | Mirrors `api/tags.py`: `1 <= len <= 32` and `/^[a-z0-9][a-z0-9_-]*$/`. |

**Mirrored constants**, following `BULK_MAX_SELECTION`'s convention verbatim
(comment says the server is authoritative; drift's only symptom is a 400 naming
the real limit):

```js
// Mirrors api/tags.py's MAX_TAGS_PER_LINK / MAX_TAG_LENGTH / TAG_PATTERN so a
// mistyped tag gets an answer without a round trip. The server stays
// authoritative: a stricter client here is only annoying, and a looser one
// just gets a 400 naming the real rule.
const TAG_MAX_PER_LINK = 10;
const TAG_MAX_LENGTH = 32;
const TAG_PATTERN = /^[a-z0-9][a-z0-9_-]*$/;
```

**Why no cross-language test pin**, unlike `keys.go`'s prefixes and
`CountShards`: those fail *silently at runtime* with data loss (a reader on a
lower shard drops clicks). These fail *loudly and harmlessly* — a client
stricter than the server refuses a tag the server would accept (visible,
annoying, no data risk), and a looser client gets a `400` carrying the rule.
Same reasoning `BULK_MAX_SELECTION` already carries. Say so in the comment.

**Keyboard and commit model** — each item below is a required behaviour, and the
first two are the ones that bite:

- **`Enter` commits the buffer and MUST call `preventDefault()`.** `#link-tags`
  sits inside the create `<form>` and `.edit-tags` inside the edit `<form>`, so
  an uncaught Enter submits the form with a half-typed tag. This is the single
  easiest thing here to get wrong and its symptom is a spurious link create.
- **Every form submit commits the buffer first.** `handleEditFormSubmit`, the
  create submit handler, the bulk-create submit handler and `handleBulkTag` all
  call `commitTagBuffer(container)` before `readTagChips(container)`, or the tag
  the user just typed and did not press Enter on is silently dropped.
- `,` typed or pasted commits the token(s) before it — handled in the `input`
  handler, which also covers paste with no separate `paste` listener.
- `change` on the buffer commits if the token is valid. This covers both
  choosing a `<datalist>` suggestion and blurring after typing. On an *invalid*
  token, `change` leaves the text alone and stays silent — the user is leaving
  the field, and the submit-time commit is the authority.
- `Backspace` on an empty buffer removes the last chip (the standard fast path,
  so keyboard users need not Tab through chips to delete one).
- `Tab` stays a focus move. Never intercepted.
- After a chip is removed by its own button, **focus moves to that container's
  buffer input.** Otherwise focus lands on `<body>` and a keyboard user is lost
  mid-form. Verified explicitly in the manual pass.
- The sitewide `:focus-visible` 2px Signal Blue outline (`DESIGN.md:221`)
  applies to chip buttons for free and must not be suppressed.

**Cap and validation feedback** (the "make the 10-cap visible" requirement),
written into the placement's `data-note` element:

- At cap: `10 of 10 tags — remove one to add another.`
- Refusing an 11th: `friendlyError({ error: "too_many_tags" }, …)` — reusing
  `ERROR_MESSAGES.too_many_tags` so client and server say the same thing.
- Invalid token: `friendlyError({ error: "invalid_tag" }, …)`, i.e. *"Tags can
  only use lowercase letters, numbers, hyphens and underscores (up to 32
  characters)."* — again the existing copy, not a second wording.
- The note is `role="status"` so a refusal is announced; it clears on the next
  successful commit.
- The three authoring labels additionally state the cap statically:
  `Tags (optional, up to 10)`.

**Deletions.** `refreshTagDatalist` (the prefix trick) and
`rebuildTagFilterOptions` are **removed**, and `loadLinks()` drops its
`rebuildTagFilterOptions()` call (`gui/dashboard.js:405`). `parseTagsInput` is
renamed to `splitTagTokens` and its four call sites become `readTagChips`.
**The trick can only be deleted once all four authoring placements are
converted** — that is why the conversion is one task, not four.

### `gui/dashboard.html` — the five placements

1. **Create form** (`:50-55`) — `#link-tags` becomes the buffer inside a
   `.tag-input` container with `data-max="10"`, plus a
   `<p id="link-tags-note" class="form-note" role="status" hidden>`. The shared
   `<datalist id="tag-suggestions">` stays exactly where it is, unchanged.
2. **Bulk-create panel** (`:89-90`) — same shape, `#bulk-tags` as the buffer,
   `data-max="10"`, note id `bulk-tags-note`.
3. **Filter row** (`:105-111`) — `#tag-filter` and its `visually-hidden` label
   are **replaced**:
   ```html
   <span id="tag-rule-wrap" hidden>
     <label for="tag-rule" class="visually-hidden">How to combine tag filters</label>
     <select id="tag-rule">
       <option value="all" selected>All of these tags</option>
       <option value="any">Any of these tags</option>
     </select>
   </span>
   <label for="tag-filter-input" class="visually-hidden">Filter by tag</label>
   <div class="tag-input" id="tag-filter-chips">
     <input type="text" id="tag-filter-input" class="tag-input-buffer"
            list="tag-suggestions" placeholder="Filter by tag&hellip;" />
   </div>
   ```
   No `data-max`, no `data-note`. `#links-filter`'s placeholder is unchanged.
4. **Filter summary**, immediately after `#export-csv` and before
   `#links-error`:
   ```html
   <p id="filter-summary" class="form-note" hidden></p>
   ```
   **Deliberately not a live region.** It is rebuilt on every keystroke of
   `#links-filter`, and a keystroke-rate `role="status"` is noise, not help.
   The `<select>` announces its own value change natively, and the table
   already re-renders silently on every keystroke today, so this is consistent
   with the status quo rather than a regression.
5. **Bulk bar** (`:135-139`) — `#bulk-tag-controls` stops being the
   `role="group"` and becomes a plain `<span id="bulk-tag-controls" hidden>`
   holding a `.tag-input` container (`data-max="10"`, buffer keeps
   `id="bulk-tag-input"` and its `aria-label`) followed by an inner
   `<div role="group">` with the unchanged `#bulk-tag-add-btn` /
   `#bulk-tag-remove-btn`. Pico's group styling then applies only to the two
   buttons, which is what it is for; `updateBulkBar()`'s existing
   `document.getElementById("bulk-tag-controls").hidden = …` and its six-button
   over-cap disable loop are untouched.
6. **Edit row** (`gui/dashboard.js:359`, `editRowHtml`) — the `.edit-tags`
   input becomes:
   ```html
   <label for="edit-tags-${slug}">Tags (up to 10)</label>
   <div class="tag-input" data-max="10" data-note="edit-tags-note-${slug}"
        data-original-tags="${escapeHtml((link.tags ?? []).join(" "))}">
     …one chip per tag…
     <input type="text" id="edit-tags-${slug}" class="tag-input-buffer" list="tag-suggestions" />
   </div>
   <p id="edit-tags-note-${slug}" class="form-note" role="status" hidden></p>
   ```
   The `<label for>`/`id` pair is per-row because a `<div>` is not phrasing
   content and so cannot legally sit inside a wrapping `<label>`; slugs are
   `id`-safe (see the confirmed facts).

### `gui/dashboard.css` — the only new CSS

No new token. New declarations are unavoidable — this is a component that did
not exist — and they are confined to layout plus neutralising Pico's button
skin:

```css
.tag-input { display: flex; flex-wrap: wrap; align-items: center; gap: 0.35rem;
             margin-bottom: var(--pico-spacing); }
/* Pico sets width:100% on every input; the same override .domain-select
   already carries (DESIGN.md's Domain selector note). */
.tag-input-buffer { width: auto; flex: 1 1 8rem; min-width: 8rem; margin-bottom: 0; }
/* A <button> carrying the .tag-chip ink. Pico's own button rule sets a
   primary fill, border and generous padding via variables — neutralised by
   class (0,1,0 beats element 0,0,1, so no specificity fight). The 44x44 floor
   is inherited from theme.css's sitewide button rule and is NOT overridden:
   the target is the point (see DESIGN.md's Chips section). */
.tag-chip-editable { display: inline-flex; align-items: center; gap: 0.25em;
                     margin-left: 0; padding: 0 0.5rem;
                     background: none;
                     border: 1px solid var(--pico-muted-border-color);
                     border-radius: var(--pico-border-radius); }
.tag-chip-editable:hover { color: var(--pico-primary); border-color: var(--pico-primary); }
#bulk-tag-controls { display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem; }
```

- **`border-radius: var(--pico-border-radius)` (0.25rem), never `999px`.** The
  Pill-Is-For-Links Rule holds: a tag is metadata about a link, not a link.
  Note the trap `theme.css:654-671` records for `input[type=search]` — Pico
  redefines `--pico-border-radius` *on the element* there. It does **not** do
  that for `button`, so reading the variable here is safe; confirm with
  `getComputedStyle` anyway, because this file has been bitten by that exact
  shape.
- **Colour and type come entirely from the shared `.tag-chip` class**, so the
  chip in an input and the chip in a table row have identical ink by
  construction and cannot drift.
- Hover uses Signal Blue as an accent only, matching the slug chip's hover
  (`DESIGN.md:209`) — no new token.

### The tag-wipe guard

`PATCH {"tags": [...]}` is a **full replacement**, and `handleEditFormSubmit`
currently always sends `tags: parseTagsInput(...)`
(`gui/dashboard.js:626, 637`). With a component in the middle, a broken or
missing container would read as zero chips and **wipe every tag on save**.
Three layers, all required:

1. **`tags` is omitted from the PATCH unless it changed.** Compare
   `readTagChips(container)` against `container.dataset.originalTags.split(" ")`
   as sets; equal → do not send the key at all, and the server leaves tags
   untouched (`if "tags" in payload`, `api/links.py`). Intentionally clearing
   every tag is a *change* to `[]`, so it still sends.
2. **A missing container omits the key too.** `const container =
   form.querySelector(".tag-input"); if (container) { … }` — a markup or JS
   break degrades to "an unrelated save leaves tags alone", never to data loss.
3. `linkRecord.tags = tagList` (`:652`) is updated **only when the key was
   sent**, so the optimistic row update cannot claim a change that was not
   requested.

### What does not change

- `allKnownTags()` (`gui/dashboard.js:121-127`) is untouched and remains the
  only suggestion source, so **suggestions stay ownership-scoped for free**:
  `allLinks` is exactly what `handle_list` returned for this principal. A user
  without `links.view_all` is still only offered tags they have used. Do not
  "improve" this.
- The in-row `.tag-chip` `<span>`s in `renderLinksTable()` (`:472`) — inert,
  unchanged, not clickable.
- CSV export's `["Tags", …]` column, `links/detail.html`'s read-only tag row,
  the `#bulk-tag-add-btn`/`#bulk-tag-remove-btn` handlers' request shape,
  `updateBulkBar()`'s permission reveal and over-cap disable loop.
- `api/`, `redirect/`, `spin.toml`, `Jenkinsfile`: **no changes at all.**

## Data model, API and redirect changes

**None.** Both features read `link.tags`, which `GET /api/links` already returns
(synthesised `[]` for legacy records), and write through the existing
`PATCH /api/links/{slug}`, `POST /api/links`, `POST /api/links/bulk` and
`POST /api/links/bulk-action` payloads unchanged. No new endpoint, no new KV key
type, no new Spin variable, no write-path change, no hot-path change. This
section is deliberately short rather than deleted: "is there really no server
change?" is the first question a reviewer will ask, and the answer is yes.

## DESIGN.md changes

A DESIGN.md edit is a **builder task in this plan**, not something the planner
does. Four edits, and the first is a catch-up:

1. **Buttons → "Minimum tap target": record the floor's real shipped shape.**
   `DESIGN.md:203` documents only `min-height: 44px` on the button family plus
   the nav-anchor extension, while `gui/theme.css:875-944` also ships
   `min-width: 44px` on that family ("the guideline is a target, not a height"),
   the `min-height` on `input:not([type=checkbox]):not([type=radio])`/`select`/
   `textarea`, the deliberate checkbox/radio *exclusion* with the floor moved to
   the wrapping `<label>` via `:has()` and to `.checkbox-hit` for the two
   label-less table checkboxes (**hit area 44×44, visual box still 20×20**),
   `details > summary`'s `0.875rem` padding, `#links-table th.sortable`'s
   `height: 44px`, `.operator-link` and `.slug-chip-link`. Write this from
   `theme.css` and `TASKS.md:851` — **record what ships, invent nothing.** This
   is what makes the chip decision an application of an existing rule rather
   than a new exemption.
2. **Chips → amend the filter-affordance paragraph (`DESIGN.md:213`).** The
   existing sentences about in-row chips staying inert `<span>`s **stay, and
   stay true**. What must change is the claim that the tag filter *is* a
   `<select>`: it is now a chip input, with a `<select>` carrying the combining
   rule. Keep the sentence's substance (a `<select>` is the established
   affordance in that row) and correct what it now describes.
3. **Chips → a new paragraph on the input chip**, stating: an interactive chip
   inside a form field is a different component in a different context; it is a
   real `<button>` and therefore carries the sitewide 44×44 floor with **no
   exemption and no new CSS to reach it**; its ink is the shared `.tag-chip`
   treatment unchanged (0.75rem caption, `--ss-slate-500`, sans-serif) and its
   radius is `--pico-border-radius`, **never the pill** — the Pill-Is-For-Links
   Rule holds; and the one-sentence reason the same rule reads differently in a
   table row (up to 300 focus stops in a table nobody opened) and in an edit
   form (10 stops in a form the user opened, which already has 8 controls).
   Include the measured numbers from the measurement task: the chip's ink
   contrast against its real rendered background in both themes, and the
   hairline's boundary contrast with whatever verdict it earns.
4. **Components → a short note on the filter row**: the combining-rule
   `<select>` is hidden below two active chips, citing the same
   one-option-selector-is-clutter reasoning as `#owner-filter-wrap` and the
   domain selector; and the filter summary line reuses `.form-note` (no new
   token, no new colour) and is deliberately not a live region.

`CLAUDE.md`'s "Link tags and ownership" section also needs a short addition
(builder task): the chip input replaced the comma field and retired the
`<datalist>` prefix trick, the dashboard tag filter is multi-tag with an
explicit all-of/any-of rule defaulting to all-of, **why** it defaults to all-of
(the bulk-action over-match argument), and a pointer to this plan.

## Trade-offs and rejected alternatives

1. **`<select multiple>` for the tag filter.** Attractive: native, zero new
   component, no keyboard model to design. Lost on three counts — it has
   nowhere to *state* the combining rule, which this entry says is the actual
   decision; multi-select requires ctrl/cmd-click, which is undiscoverable and
   effectively unusable on touch; and its rendered height in a filter row
   DESIGN.md has already measured as tight is unpredictable. It also gives no
   type-to-add path, where the chip input gets one from `<datalist>` for free.

2. **A two-button segmented `role="group"` with `aria-pressed` for the
   all-of/any-of rule** (the shape `.theme-toggle` already ships). Attractive:
   both options visible at rest, so the rule is legible without opening
   anything. Lost on width (two `min-width: 44px` buttons plus group borders
   against one ~110px `<select>`, in a row where the table has already
   overflowed once), on focus stops (2 vs 1), and because `<select>` is the
   established affordance in that exact row per `DESIGN.md:213`. Revisit if the
   filter row is ever redesigned with more horizontal budget.

3. **Any-of (OR) as the default.** Attractive: it is what "select several tags"
   often means colloquially, and it never returns zero rows when each tag
   individually matches something. Lost because it grows the result set as you
   add chips (the opposite of a filter's mental model), because it would make
   one filter row carry two combining semantics, and decisively because the
   filtered set feeds bulk Delete/Reassign — over-matching plus select-all is
   how an operator deletes links they never meant to, the same argument that
   killed owner substring filtering on 2026-08-04.

4. **A hand-rolled ARIA combobox/listbox for suggestions** (with tag counts,
   keyboard arrow navigation, `aria-activedescendant`). Attractive: it could
   show "sale (12)" and would not depend on browser `<datalist>` quirks. Lost
   because `<datalist>` becomes *honest* the moment the input's whole value is
   one token — which is exactly what the chip input makes true — so the trick
   this work exists to retire is retired without adding ~150 lines of ARIA the
   app has no other instance of, in a component that must be instantiated once
   per editable table row.

5. **A `<span>` chip wrapping a separate `<button>×`.** Attractive: it matches
   the filed entry's literal wording and keeps the chip itself non-interactive.
   Lost: 20 focus stops in a 10-tag form instead of 10, a `min-width: 44px`
   button sitting next to a ~35px label (so the chip is mostly button anyway),
   and a second CSS block for a nested interactive element — for no
   accessibility gain, since a chip inside an input has exactly one action.

6. **Extending each chip's hit area past its ink with an overlay or negative
   margins**, to keep a small visual chip with a 44px target. Attractive: it is
   the letter of "the floor is about tap target, not ink", and `.checkbox-hit`
   is the shipped precedent. Lost because adjacent chips' hit areas would
   overlap, so a tap near a boundary removes the *wrong* tag — a worse
   accessibility outcome than the one it optimises. `.checkbox-hit` works
   because its 44×44 wrapper *is* the layout box; chips in a wrapping row have
   no such spare room.

7. **Making the container look like the form field** (border/radius/padding on
   the wrapper, border removed from the inner input — the classic chip-input
   look). Attractive: prettier, and unmistakably reads as one control. Lost
   because it means reproducing a field's `:focus-within`, invalid and disabled
   states on a `<div>`, plus a second focus-ring story, for looks. The chosen
   shape keeps Pico's own field treatment on the buffer, so its focus ring,
   invalid styling and 44px floor all come free.

8. **Exempting input chips from the 44px floor** (a narrowed design-system
   rule). Attractive: the smallest CSS and the lightest visuals. Lost because
   no exemption is needed — a real `<button>` inherits the floor from CSS that
   already exists — and because DESIGN.md's history is of the floor being
   *extended* twice (nav anchors, `<select>`) and never narrowed. Narrowing it
   for the newest component in the app would be exactly backwards.

9. **Converting only some of the four authoring placements** (e.g. leaving the
   bulk bar's single-tag field as a text input). Attractive: less work, less
   risk. Lost because `refreshTagDatalist`'s prefix trick would then have to
   survive, and retiring it is the stated point of the entry — plus two
   different tag-entry idioms on one page is precisely the drift this codebase
   fixes at the cause (see `ALL_PERMISSIONS`, `TASKS.md:428`).

10. **Do nothing.** Live, and the filed entry's own guidance ("pick it up only
    if someone actually complains about the text input") pointed at it. It
    stops being right the moment someone complains, which has now happened.
    Recorded so the trigger's firing is on the record rather than implied.

11. **A `?tags=q4,sale&tag_rule=all` deep link.** Attractive, and there is a
    precedent one line away (`?owner=`, consumed once). Deferred: `?owner=`
    exists because the admin Users page needs to hand the operator a worklist,
    and nothing in the app currently needs to hand anyone a tag filter.
    Filed under Future work.

## Tasks

The exact unchecked lines appended to `TASKS.md` under
`## Tag UX: chip input and multi-tag filtering`. `TASKS.md` is authoritative;
checkbox state is not maintained here.

```
- [ ] Shared tag chip-input component, wired into the create form only — file(s): gui/dashboard.js, gui/dashboard.html, gui/dashboard.css — done when: the create form's Tags field renders one `.tag-chip-editable` button per committed tag with `aria-label="Remove tag <t>"`; Enter, a typed comma and a pasted "a, b c" all commit tokens without submitting the form; Backspace on an empty buffer removes the last chip; clicking a chip removes it and leaves focus on that container's buffer input; an invalid token and an 11th tag are both refused with the existing `ERROR_MESSAGES` copy in the placement's `role="status"` note and the text left in the buffer for correction; the note reads "10 of 10 tags — remove one to add another." at the cap; and creating a link sends exactly the committed tags including one typed but not Enter-committed
- [ ] Convert the remaining three authoring placements and delete the `<datalist>` prefix trick — file(s): gui/dashboard.html, gui/dashboard.js — done when: `#bulk-tags`, `#bulk-tag-input` and every edit row's `.edit-tags` are chip inputs; `refreshTagDatalist` and `parseTagsInput` no longer exist (`grep -c "refreshTagDatalist\|parseTagsInput" gui/dashboard.js` returns 0) and `refreshTagSuggestions` does no prefix arithmetic; `#bulk-tag-controls` is a plain `<span>` whose two buttons are the only `role="group"` members; bulk create, bulk Add tag/Remove tag and an edit-row save all still round-trip tags correctly against a running app
- [ ] Guard the edit form against wiping tags — file(s): gui/dashboard.js — done when: an edit-row save that does not touch tags sends a PATCH body with no `tags` key at all (observed in the browser network panel); deleting every chip and saving sends `"tags": []` and clears them; and deliberately removing the `.tag-input` container from `editRowHtml` makes a save leave the link's tags untouched rather than clearing them (mutation check, reverted afterwards)
- [ ] Multi-tag dashboard filter with an explicit all-of/any-of rule — file(s): gui/dashboard.html, gui/dashboard.js, gui/dashboard.css — done when: `#tag-filter` and `rebuildTagFilterOptions` are gone; `#tag-filter-chips` accepts several tags; `#tag-rule` defaults to "All of these tags", is hidden below two active chips, and switching it re-filters immediately; the tag chips AND with `#links-filter` and `#owner-filter`; `?owner=` still applies exactly once; and `#filter-summary` states the count and every active clause in words (e.g. `Showing 7 of 42 links · tags: all of #q4, #sale · owner: alice`) and is hidden when no filter is active
- [ ] Empty-state copy that names the combining rule — file(s): gui/dashboard.js — done when: with two chips active and no matching link, the table's empty row reads "No link carries all of #q4, #sale." plus `Try "Any of these tags".` under the all-of rule, and names the any-of case correctly under any-of, while the unfiltered "No links yet — create one above." copy is unchanged
- [ ] Measure the new controls at 1400/768/480/390px in both themes — file(s): (none — measurement step) — done when: the links `<article>` shows no `scrollWidth` > `clientWidth` at any of the four widths in either theme with 10 tags on one link and 3 chips in the filter; `#links-figure` is still exactly 327/327 and not scrolling at 390px; every new interactive element (`.tag-chip-editable`, each `.tag-input-buffer`, `#tag-rule`) measures at least 44×44 via `getBoundingClientRect`; the chip's ink and its hairline border are contrast-measured against their real rendered backgrounds in both themes with the numbers recorded; and the chip's computed `border-radius` is confirmed to be 0.25rem, not a pill
- [ ] Record the decisions in DESIGN.md — file(s): DESIGN.md — done when: the Buttons "Minimum tap target" bullet describes the floor as it actually ships in gui/theme.css (min-width as well as min-height, the checkbox/radio exclusion, `.checkbox-hit`'s 44×44-hit/20×20-visual, `<summary>`, `th.sortable`, `.operator-link`, `.slug-chip-link`); the Chips section's filter-affordance paragraph is corrected without weakening the in-row-chips-stay-inert rule; a new paragraph states why an input chip takes the 44px floor with no exemption, why its radius is never the pill, and the focus-stop reason the same rule reads differently in a table row; and the measured numbers from the measurement task are quoted rather than described
- [ ] Record the filter rule in CLAUDE.md's "Link tags and ownership" section — file(s): CLAUDE.md — done when: the section states that the chip input replaced the comma-separated field and retired the `<datalist>` prefix trick, that the dashboard tag filter is multi-tag with an explicit all-of/any-of rule defaulting to all-of, why it defaults to all-of (a filtered set feeds bulk Delete/Reassign, so over-matching is unsafe), that suggestions are still ownership-scoped and client-derived, and links to docs/plans/tag-ux-chips-and-filtering.md
- [ ] End-to-end manual verification of the chip input and multi-tag filtering — file(s): (none — verification step) — done when: against a real `spin up --build` run, `curl localhost:3000/dashboard.js | grep -c refreshTagDatalist` returns 0 (proving the served asset is not the startup-stale one); a keyboard-only pass creates, tags, filters and untags a link without ever using the mouse, including removing a chip with Enter/Space on its button and switching `#tag-rule`; the 11th tag is refused visibly; the browser console is free of errors and CSP violations on the dashboard; and `cd gui-pages && uv run pytest` plus `cd api && uv run pytest` both pass
```

## Critical files

- `docs/plans/tag-ux-chips-and-filtering.md` (new)
- `gui/dashboard.html`
- `gui/dashboard.js`
- `gui/dashboard.css`
- `DESIGN.md`
- `CLAUDE.md`
- `TASKS.md`

`gui/theme.css` is expected **not** to change: `.tag-chip` already carries the
ink, and the 44px floor already covers a `<button>`. If a chip needs a
theme-level declaration after all, that is a finding worth writing down, not a
quiet edit.

Untouched, and deliberately so: `api/`, `redirect/`, `spin.toml`,
`runtime-config.toml`, `Jenkinsfile`, `gui-pages/`.

## Verification

In execution order.

1. `cd gui-pages && uv run pytest` — the no-inline-code guards cover
   `dashboard.html` and (by glob) `dashboard.js`, so this is the test that
   catches an accidental `style=` or inline handler. Baseline before any change:
   **108 passed**.
2. `cd api && uv run pytest` — no API change is intended, so this is a
   confirmation that none happened. (`cd redirect && go test ./linkgate/...` is
   **not** in this list: zero Go files are touched. Never `go test ./...`,
   `go build ./...` or `go vet ./...`, which fail by design on `package main`.)
3. Start the app — note that a `gui/` edit needs a **restart**, because
   `spin_static_fs` serves a startup snapshot:
   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```
4. `curl -s localhost:3000/dashboard.js | grep -c refreshTagDatalist` → `0`.
   Do this **before** doubting any browser behaviour: a stale served asset
   reproduces the old bug with the old line numbers and reads as "my fix is
   wrong."
5. Seed data through the UI: sign in, open **Bulk create**, paste three rows
   with batch tags `sale, q4`, submit; repeat with `sale, email`; then edit one
   link and give it ten tags (`t1 … t10`) to exercise the cap and the widest
   layout.
6. **Chip input, mouse:** commit tags with Enter, with a typed comma, and by
   pasting `alpha, beta gamma`. Pick a suggestion from the dropdown and confirm
   it commits without a comma prefix appearing anywhere. Remove a chip and
   confirm focus lands in the buffer. Type an 11th tag and confirm the refusal
   message names the cap and leaves the text in the buffer. Type `Q4 Sale` and
   confirm the invalid-tag copy matches `ERROR_MESSAGES.invalid_tag`.
7. **Chip input, keyboard only** (no mouse at all): Tab to the Tags field, add
   two tags, Tab onto a chip, press Enter (then Space) to remove it, confirm
   focus returns to the buffer, Backspace-remove the remaining chip from an
   empty buffer, then Tab to Save and submit. A pass is: the whole flow
   completes and the focus ring is visible on the chip button in **both**
   themes.
8. **Tag-wipe guard:** with the browser network panel open, save an edit row
   without touching tags and confirm the PATCH body has **no `tags` key**.
   Then delete every chip, save, and confirm `"tags": []` and that the row's
   chips disappear.
9. **Filter:** add `sale` — `#tag-rule` stays hidden, `#filter-summary` reads
   `Showing 6 of N links · tags: #sale`. Add `q4` — the rule select appears
   showing "All of these tags", the table narrows to the intersection, and the
   summary says `all of #q4, #sale`. Switch to "Any of these tags" and confirm
   the set widens to the union and the summary changes with it. Add a third tag
   that matches nothing and confirm the empty state names all three tags and
   offers the any-of hint. Combine with a text term and with an owner and
   confirm all three AND together. Reload with `?owner=<someone>` and confirm
   it applies once and survives a subsequent bulk action without snapping back.
10. **Bulk composition:** tag-filter to a set, select all, confirm the bulk bar
    counts the filtered set, run Add tag from the bulk bar's chip input, and
    confirm the count line, the six-button over-cap disable behaviour past 50
    and the success message are all unchanged.
11. **Measurements**, at 1400/768/480/390px in **both** themes, with 10 tags on
    one link and 3 chips in the filter: `article.scrollWidth` vs `clientWidth`
    for the links article (no overflow); `#links-figure` `scrollWidth` vs
    `clientWidth` at 390px (**327/327, not scrolling** — the new empty-state
    string and the summary line are new widest-element candidates);
    `getBoundingClientRect()` on every `.tag-chip-editable`, every
    `.tag-input-buffer` and `#tag-rule` (≥44×44); `getComputedStyle` on a chip
    for `border-radius` (0.25rem, not a pill) and for its resolved colour and
    border colour, contrast-measured against the element's **real rendered
    background** in each theme.
12. Console clean: zero errors and zero CSP violations on `dashboard.html` in
    both themes.

## Out of scope / follow-ups

- **Renaming a tag across every link** (`TASKS.md:423`) — explicitly out of
  scope and must not grow out of this work. It needs the whole-tag server-side
  action rejected on 2026-08-03 and writes once per affected link against the
  50 writes/second cap. It stays open, untouched.
- **Clickable in-row table chips** — remains rejected on the record
  (`TASKS.md:262`, `DESIGN.md:213`), and the argument is now *weaker* to
  revisit, not stronger: the filter is a chip input you can type into with
  autocomplete, so the discoverability case that motivated clickable chips is
  largely served.
- **A `?tags=…&tag_rule=…` deep link.** Added to Future work; the trigger is
  another page needing to hand an operator a tag-scoped worklist, the way the
  admin Users page hands over `?owner=`.
- **Tag counts in the suggestion list** (`sale (12)`). Added to Future work;
  it needs a real suggestion listbox, which trade-off #4 rejects for now.
- **Windowed table rendering** (`TASKS.md:475`, already filed) is untouched.
  Worth noting the interaction: a chip filter makes it easier to select 50+
  links at once, which the existing over-cap copy already handles.
- **No server-side tag registry, no `tag:` index, no API change** — all still
  rejected, unchanged.
