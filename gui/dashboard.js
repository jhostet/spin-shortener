let currentPrincipal = null;
// Populated from GET /api/users only when the principal holds users.manage
// (that endpoint 403s otherwise, and api.get surfaces that as ok: false) —
// the source for #bulk-owner-select and each edit row's .edit-owner select.
let allUsernames = [];

function canTagLinks() {
  return !!currentPrincipal && (currentPrincipal.role === "admin" || currentPrincipal.permissions.includes("links.tag"));
}

function canManageUsers() {
  return !!currentPrincipal && (currentPrincipal.role === "admin" || currentPrincipal.permissions.includes("users.manage"));
}

async function loadMe() {
  const { ok, data } = await initHeader();
  if (ok) {
    currentPrincipal = data;
    if (data.role === "admin" || data.permissions.includes("links.create_custom_slug")) {
      document.getElementById("custom-slug-field").hidden = false;
    }
    if (data.role === "admin" || data.permissions.includes("links.view_all") || data.permissions.includes("links.edit_all")) {
      document.getElementById("links-heading").textContent = "All links";
    }
    if (canManageUsers()) {
      const usersResult = await api.get("/users");
      if (usersResult.ok) allUsernames = usersResult.data.users.map((u) => u.username);
    }
  }
}

// Mirrors the server's own links._can_edit — a link's Edit/Delete
// controls are only rendered for its owner, an admin, or a user with
// links.edit_all, so the row-action buttons never promise access the
// API would then 403 on.
function canEditLink(link) {
  if (!currentPrincipal) return false;
  return (
    currentPrincipal.role === "admin" ||
    currentPrincipal.permissions.includes("links.edit_all") ||
    link.owner === currentPrincipal.username
  );
}

// Mirrors the server's own invalid_window_range check (start >= end is
// invalid, not just start > end) so a bad combination is caught natively
// before submission instead of round-tripping to the API to find out.
// datetime-local values are fixed-width "YYYY-MM-DDTHH:mm" strings, so a
// plain string comparison is already correct chronological ordering.
function wireWindowValidation(startInput, endInput) {
  const validate = () => {
    if (startInput.value && endInput.value && startInput.value >= endInput.value) {
      endInput.setCustomValidity("Expires must be after Starts.");
    } else {
      endInput.setCustomValidity("");
    }
  };
  startInput.addEventListener("input", validate);
  endInput.addEventListener("input", validate);
}

// An em-dash while totals are still in flight, not "0" — a real zero and
// "not loaded yet" are different facts, and showing 0 for the second one
// tells the operator their campaign got no clicks.
function clicksCell(slug) {
  if (clickTotals === null) return '<span class="clicks-pending" aria-label="Loading">—</span>';
  return escapeHtml(String(clickTotals[slug] ?? 0));
}

function formatWindowField(value, { warnIfSoon = false, noteIfFuture = false } = {}) {
  if (!value) return "—";
  const text = escapeHtml(formatTimestamp(value));
  const hoursUntil = (new Date(value) - Date.now()) / 3600000;
  if (noteIfFuture && hoursUntil > 0) {
    return `<span class="not-yet-live">${text} (not yet live)</span>`;
  }
  if (warnIfSoon) {
    if (hoursUntil <= 0) {
      return `<span class="expired">${text} (expired)</span>`;
    }
    if (hoursUntil <= 24) {
      return `<span class="expiring-soon">${text} (expiring soon)</span>`;
    }
  }
  return text;
}

let allLinks = [];
// slug -> click total, from GET /api/analytics/click-totals. Loaded AFTER the
// table renders, deliberately: the links list must never wait on analytics,
// which reads a different store and is the slower of the two. Until it
// arrives every row shows an em-dash rather than a misleading 0.
let clickTotals = null;
let sortKey = null;
let sortDir = 1;

// The owner named by ?owner= on first load — the admin Users page links here
// when a delete is refused because the user still owns links. Consumed once
// and then cleared, so a later loadLinks() (after a bulk action) preserves
// whatever the operator has since chosen instead of snapping back to the URL.
let pendingOwnerFilter = new URLSearchParams(location.search).get("owner");

// The sorted distinct union of every tag across allLinks — the autocomplete
// and filter-option source. Deliberately client-derived, not a server call:
// allLinks is already ownership-scoped by handle_list, so this never
// suggests a tag the viewer couldn't already see (see docs/plans/
// link-tags-and-ownership.md's rejected _meta:tags registry).
function allKnownTags() {
  const tags = new Set();
  for (const link of allLinks) {
    for (const tag of link.tags ?? []) tags.add(tag);
  }
  return [...tags].sort();
}

// The sorted distinct union of every `owner` across allLinks — record-derived
// like allKnownTags(), so it's correctly ownership-scoped for free (allLinks
// is already what handle_list returned for this principal) and catches index
// drift the 409 gate's owner_links: read would miss.
function allKnownOwners() {
  return [...new Set(allLinks.map((l) => l.owner))].sort();
}

// allUsernames is empty for anyone without users.manage — without this guard
// every owner would read as deleted for such a viewer, which is wrong.
function isDeletedOwner(owner) {
  return allUsernames.length > 0 && !allUsernames.includes(owner);
}

// Repopulates #owner-filter from the current allKnownOwners(), preserving the
// active selection if it still exists; otherwise consumes pendingOwnerFilter
// (the admin Users page's ?owner= deep link) exactly once.
function rebuildOwnerFilterOptions() {
  const wrap = document.getElementById("owner-filter-wrap");
  const select = document.getElementById("owner-filter");
  const current = select.value;
  const owners = allKnownOwners();
  const options = owners
    .map((o) => `<option value="${escapeHtml(o)}">${escapeHtml(o)}${isDeletedOwner(o) ? " — deleted account" : ""}</option>`)
    .join("");
  select.innerHTML = `<option value="">All owners</option>${options}`;
  if (current && owners.includes(current)) {
    select.value = current;
  } else if (pendingOwnerFilter) {
    // Added even when it matches no link, so a deep link that arrives one
    // action too late (e.g. the owner was already reassigned) still reads as
    // "no links match your filter" rather than silently showing everything.
    if (!owners.includes(pendingOwnerFilter)) {
      select.insertAdjacentHTML(
        "beforeend",
        `<option value="${escapeHtml(pendingOwnerFilter)}">${escapeHtml(pendingOwnerFilter)}${isDeletedOwner(pendingOwnerFilter) ? " — deleted account" : ""}</option>`
      );
    }
    select.value = pendingOwnerFilter;
    pendingOwnerFilter = null;
  }
  wrap.hidden = owners.length < 2 && !select.value;
}

// --- Tag chip-input component ----------------------------------------------
// See docs/plans/tag-ux-chips-and-filtering.md. One component, instantiated
// per placement via the `.tag-input` container markup contract (a chip
// button per committed tag, followed by a `.tag-input-buffer` text input).
// All handlers are delegated on `document`, so a per-row edit form costs no
// per-instance listeners. This replaced the old comma-field <datalist>
// prefix trick, retired once every placement below held exactly one token
// per buffer, which is what makes <datalist>'s native prefix-match honest
// again — see the plan's "Key technical facts" section.

// Mirrors api/tags.py's MAX_TAGS_PER_LINK / MAX_TAG_LENGTH / TAG_PATTERN so a
// mistyped tag gets an answer without a round trip. The server stays
// authoritative: a stricter client here is only annoying, and a looser one
// just gets a 400 naming the real rule. No cross-language test pin, unlike
// keys.go's prefixes/CountShards: those fail silently at runtime with data
// loss, while these fail loudly and harmlessly (a visible refusal or a 400
// naming the real limit) — the same reasoning BULK_MAX_SELECTION already
// carries above.
const TAG_MAX_PER_LINK = 10;
const TAG_MAX_LENGTH = 32;
const TAG_PATTERN = /^[a-z0-9][a-z0-9_-]*$/;

function isValidTagToken(token) {
  return token.length >= 1 && token.length <= TAG_MAX_LENGTH && TAG_PATTERN.test(token);
}

// Splits on any run of whitespace or commas, trims, lowercases, drops
// empties, de-duplicates. This is what the retired comma-only splitter
// became once a buffer could hold more than a comma-separated list — the new
// name stops it reading as "parse the whole field", which it no longer is
// now that every authoring placement is a chip input. Whitespace is a safe
// delimiter because TAG_PATTERN excludes it, so a pasted "sale, q4 email"
// yields three tokens.
function splitTagTokens(value) {
  const seen = new Set();
  for (const raw of (value || "").split(/[\s,]+/)) {
    const tag = raw.trim().toLowerCase();
    if (tag) seen.add(tag);
  }
  return [...seen];
}

// The DOM is the single source of truth for a placement's committed tags —
// no parallel JS state to desync across a renderLinksTable() rebuild.
function readTagChips(container) {
  return [...container.querySelectorAll(".tag-chip-editable")].map((el) => el.dataset.tag);
}

function tagChipHtml(tag) {
  return `<button type="button" class="tag-chip tag-chip-editable" data-tag="${escapeHtml(tag)}" aria-label="Remove tag ${escapeHtml(tag)}">#${escapeHtml(tag)} <span aria-hidden="true">&times;</span></button>`;
}

function tagNoteEl(container) {
  const id = container.dataset.note;
  return id ? document.getElementById(id) : null;
}

// Refusal feedback — reuses the exact ERROR_MESSAGES copy the server-side
// 400 for the same code would show, so client and server never say two
// different things for the same rule.
function showTagNote(container, reason) {
  const note = tagNoteEl(container);
  if (!note) return;
  if (reason === "cap") {
    note.textContent = friendlyError({ error: "too_many_tags" }, "That's too many tags for one link.");
  } else if (reason === "invalid") {
    note.textContent = friendlyError({ error: "invalid_tag" }, "Tags can only use lowercase letters, numbers, hyphens and underscores (up to 32 characters).");
  }
  note.hidden = false;
}

// The proactive "you're at the cap" advisory — shown continuously once a
// placement's tag count reaches its data-max, not only when an 11th tag is
// actively refused. A placement with no data-max (the tag filter) never
// shows this, since filtering by eleven tags is pointless but harmless.
function updateCapAdvisory(container) {
  const note = tagNoteEl(container);
  if (!note) return;
  const max = container.dataset.max ? parseInt(container.dataset.max, 10) : null;
  if (max !== null && readTagChips(container).length >= max) {
    note.textContent = `${max} of ${max} tags — remove one to add another.`;
    note.hidden = false;
  } else {
    note.hidden = true;
    note.textContent = "";
  }
}

// Rebuilds #tag-suggestions from allKnownTags() minus the chips already
// committed in this buffer's own container. No prefix arithmetic — the
// retired comma-field datalist refresh needed that only because its field
// held more than one token; a chip-input buffer's whole value is always
// exactly one token, so <datalist>'s native prefix-match already works
// correctly.
function refreshTagSuggestions(buffer) {
  const container = buffer.closest(".tag-input");
  const already = new Set(container ? readTagChips(container) : []);
  document.getElementById("tag-suggestions").innerHTML = allKnownTags()
    .filter((t) => !already.has(t))
    .map((t) => `<option value="${escapeHtml(t)}"></option>`)
    .join("");
}

// The one hook fired after every chip add/remove. Scoping the filter-specific
// side effects to container.id === "tag-filter-chips" is load-bearing: an
// unscoped re-render would rebuild the links table out from under an open
// edit form the moment a tag was typed into it.
function tagChipsChanged(container) {
  const buffer = container.querySelector(".tag-input-buffer");
  if (buffer) refreshTagSuggestions(buffer);
  updateCapAdvisory(container);
  if (container.id === "tag-filter-chips") {
    const ruleWrap = document.getElementById("tag-rule-wrap");
    if (ruleWrap) ruleWrap.hidden = readTagChips(container).length < 2;
    // renderLinksTable() itself calls updateFilterSummary() on every render,
    // so re-filtering here keeps the summary line in sync for free.
    renderLinksTable();
  }
}

// Normalise -> validate -> de-duplicate -> cap-check -> insert before the
// buffer. Returns a reason string on refusal ("invalid"/"cap") or null on
// success/no-op (an empty token or an already-present tag is a silent no-op,
// not an error — retyping a tag that's already a chip is not a mistake).
function addTagChip(container, token) {
  const tag = (token || "").trim().toLowerCase();
  if (!tag) return null;
  if (!isValidTagToken(tag)) return "invalid";
  if (readTagChips(container).includes(tag)) return null;
  const max = container.dataset.max ? parseInt(container.dataset.max, 10) : null;
  if (max !== null && readTagChips(container).length >= max) return "cap";
  const buffer = container.querySelector(".tag-input-buffer");
  buffer.insertAdjacentHTML("beforebegin", tagChipHtml(tag));
  tagChipsChanged(container);
  return null;
}

// Removes the chip, moves focus to the container's buffer input (otherwise
// focus lands on <body> and a keyboard user is lost mid-form), and fires the
// changed hook.
function removeTagChip(button) {
  const container = button.closest(".tag-input");
  if (!container) return;
  button.remove();
  const buffer = container.querySelector(".tag-input-buffer");
  tagChipsChanged(container);
  if (buffer) buffer.focus();
}

// splitTagTokens() the buffer, addTagChip() each token in order; on the first
// refusal, leave the offending token in the buffer and show the note rather
// than silently dropping the rest of what was typed.
function commitTagBuffer(container) {
  const buffer = container.querySelector(".tag-input-buffer");
  if (!buffer) return;
  const tokens = splitTagTokens(buffer.value);
  for (const token of tokens) {
    const reason = addTagChip(container, token);
    if (reason) {
      buffer.value = token;
      showTagNote(container, reason);
      return;
    }
  }
  buffer.value = "";
}

// Removes every chip and empties the buffer. Used by the create/bulk-create
// resets and after a successful bulk tag action.
function clearTagChips(container) {
  container.querySelectorAll(".tag-chip-editable").forEach((el) => el.remove());
  const buffer = container.querySelector(".tag-input-buffer");
  if (buffer) buffer.value = "";
  tagChipsChanged(container);
}

// `,` typed or pasted commits the token(s) before it — covers paste too, with
// no separate paste listener. A comma-free keystroke is a no-op here; Enter
// (below) and blur/change (below) are the other two commit paths.
function handleTagBufferInput(buffer) {
  if (!buffer.value.includes(",")) return;
  const container = buffer.closest(".tag-input");
  if (!container) return;
  const idx = buffer.value.lastIndexOf(",");
  const toCommit = buffer.value.slice(0, idx);
  buffer.value = buffer.value.slice(idx + 1);
  for (const token of splitTagTokens(toCommit)) {
    const reason = addTagChip(container, token);
    if (reason) {
      showTagNote(container, reason);
      return;
    }
  }
}

// Covers both choosing a <datalist> suggestion and blurring after typing. On
// an invalid token this stays silent and leaves the text alone — the user is
// leaving the field, and the submit-time commitTagBuffer() is the authority
// that will surface the error.
function handleTagBufferChange(buffer) {
  const container = buffer.closest(".tag-input");
  if (!container) return;
  const tokens = splitTagTokens(buffer.value);
  if (!tokens.length || !tokens.every(isValidTagToken)) return;
  for (const token of tokens) {
    if (addTagChip(container, token) === "cap") break;
  }
  buffer.value = "";
}

document.addEventListener("input", (e) => {
  if (e.target.matches(".tag-input-buffer")) handleTagBufferInput(e.target);
});

document.addEventListener("change", (e) => {
  if (e.target.matches(".tag-input-buffer")) handleTagBufferChange(e.target);
});

document.addEventListener("keydown", (e) => {
  if (!e.target.matches(".tag-input-buffer")) return;
  const container = e.target.closest(".tag-input");
  if (!container) return;
  if (e.key === "Enter") {
    // #link-tags sits inside the create <form> and .edit-tags inside the
    // edit <form>, so an uncaught Enter submits the form with a half-typed
    // tag — this preventDefault is the single easiest thing here to get
    // wrong, and its symptom is a spurious link create.
    e.preventDefault();
    commitTagBuffer(container);
  } else if (e.key === "Backspace" && e.target.value === "") {
    // The standard fast path — a keyboard user need not Tab through chips to
    // delete one.
    const chips = container.querySelectorAll(".tag-chip-editable");
    if (chips.length) removeTagChip(chips[chips.length - 1]);
  }
});

// Enter/Space activation of a chip button is native <button> behaviour and
// needs no extra wiring here — this only handles the mouse/pointer path.
document.addEventListener("click", (e) => {
  const chip = e.target.closest(".tag-chip-editable");
  if (chip) removeTagChip(chip);
});

// Refreshes #tag-suggestions for whichever buffer just gained focus, since
// the shared datalist's exclusion set (allKnownTags() minus this container's
// own chips) differs per placement.
document.addEventListener("focusin", (e) => {
  if (e.target.matches(".tag-input-buffer")) refreshTagSuggestions(e.target);
});

// Slugs currently checked for a bulk action. Cleared at the top of every
// renderLinksTable() call (filter, sort, loadLinks()) so a user can never
// act on rows they are not currently looking at.
let selectedSlugs = new Set();

// Mirrors api/bulk.py's MAX_BULK_ROWS so the bulk bar can warn before
// submitting a request that is guaranteed to be rejected. The server is
// authoritative — if this drifts from the real cap, the only symptom is a
// too_many_rows rejection naming the actual limit, never silently wrong
// client behavior.
const BULK_MAX_SELECTION = 50;

function updateBulkBar() {
  const bar = document.getElementById("bulk-bar");
  const count = selectedSlugs.size;
  bar.hidden = count === 0;

  document.getElementById("bulk-tag-controls").hidden = !canTagLinks();
  document.getElementById("bulk-owner-controls").hidden = !canManageUsers();
  if (canManageUsers()) populateOwnerSelect(document.getElementById("bulk-owner-select"));

  if (count === 0) return;

  const countEl = document.getElementById("bulk-count");
  const overCap = count > BULK_MAX_SELECTION;
  countEl.textContent = overCap
    ? `${count} links selected — bulk actions apply to at most ${BULK_MAX_SELECTION} at a time. Narrow the filter, or clear some selections.`
    : `${count} link${count === 1 ? "" : "s"} selected`;

  for (const id of ["bulk-enable-btn", "bulk-disable-btn", "bulk-delete-btn",
                     "bulk-schedule-set-btn", "bulk-schedule-clear-btn", "bulk-repoint-btn",
                     "bulk-tag-add-btn", "bulk-tag-remove-btn", "bulk-reassign-btn"]) {
    document.getElementById(id).disabled = overCap;
  }
}

// Shared by #bulk-owner-select and each edit row's .edit-owner — builds the
// options from allUsernames, selecting `selected` if it's one of them.
function populateOwnerSelect(select, selected) {
  const usernames = allUsernames.length ? allUsernames : (selected ? [selected] : []);
  let html = usernames
    .map((u) => `<option value="${escapeHtml(u)}" ${u === selected ? "selected" : ""}>${escapeHtml(u)}</option>`)
    .join("");
  // A current owner absent from allUsernames (an orphaned link) gets a
  // disabled placeholder option so the select shows the truth instead of
  // silently pre-selecting the first real user — and, because the select's
  // value then equals link.owner, handleEditFormSubmit's
  // `ownerSelect.value !== linkRecord.owner` check stops firing a spurious
  // reassign confirmation on an unrelated save. Mirrors users.js's
  // domainCheckboxesHtml handling of a no-longer-configured domain.
  if (selected && allUsernames.length && !allUsernames.includes(selected)) {
    html = `<option value="${escapeHtml(selected)}" selected disabled>${escapeHtml(selected)} — deleted account</option>${html}`;
  }
  select.innerHTML = html;
}

// The header checkbox only ever reflects/affects the *selectable*
// (canEditLink) rows in the current filtered view — selecting a row nobody
// is allowed to act on would promise an action the API just refuses anyway.
function getSelectableVisibleSlugs() {
  return getVisibleLinks().filter(canEditLink).map((link) => link.slug);
}

// Sets selectedSlugs to exactly `slugs` and reflects it in the currently
// rendered checkboxes — used by every bulk flow's partial-write branch
// (docs/plans/write-throttle-resilience.md) to narrow the selection to
// `not_applied` after a `loadLinks()` re-render (which clears selectedSlugs
// as a side effect, per renderLinksTable's own comment), so clicking the
// same bulk button again retries exactly the failures.
function narrowSelectionTo(slugs) {
  selectedSlugs = new Set(slugs);
  document.querySelectorAll("#links-body .row-select").forEach((cb) => {
    cb.checked = selectedSlugs.has(cb.dataset.slug);
  });
  updateSelectAllState();
  updateBulkBar();
}

function updateSelectAllState() {
  const selectAll = document.getElementById("select-all-links");
  const selectableSlugs = getSelectableVisibleSlugs();
  if (!selectableSlugs.length) {
    selectAll.checked = false;
    selectAll.indeterminate = false;
    selectAll.disabled = true;
    return;
  }
  selectAll.disabled = false;
  const selectedCount = selectableSlugs.filter((slug) => selectedSlugs.has(slug)).length;
  selectAll.checked = selectedCount === selectableSlugs.length;
  selectAll.indeterminate = selectedCount > 0 && selectedCount < selectableSlugs.length;
}

function updateSortIndicators() {
  document.querySelectorAll("#links-table th.sortable").forEach((th) => {
    const indicator = th.querySelector(".sort-indicator");
    const isActive = th.dataset.sortKey === sortKey;
    indicator.textContent = isActive ? (sortDir === 1 ? " ▲" : " ▼") : "";
    th.setAttribute("aria-sort", isActive ? (sortDir === 1 ? "ascending" : "descending") : "none");
  });
}

// The tags currently active in the #tag-filter-chips chip input. Reads the
// DOM (readTagChips), not any parallel JS state — see the chip component's
// own comment on why that's the single source of truth.
function activeTagFilterChips() {
  const container = document.getElementById("tag-filter-chips");
  return container ? readTagChips(container) : [];
}

// "all" (the default) or "any" — see docs/plans/tag-ux-chips-and-filtering.md's
// "The filtering decision" for why all-of is the default (monotone
// narrowing, composition consistency with the other filters, and — the
// decisive reason — the filtered set feeds bulk Delete/Reassign, so any-of's
// over-matching is unsafe there).
function activeTagRule() {
  const select = document.getElementById("tag-rule");
  return select && select.value === "any" ? "any" : "all";
}

function getVisibleLinks() {
  const term = document.getElementById("links-filter").value.trim().toLowerCase();
  let visible = !term
    ? allLinks
    : allLinks.filter(
        (link) =>
          link.slug.toLowerCase().includes(term) ||
          link.target_url.toLowerCase().includes(term) ||
          (link.tags ?? []).some((t) => t.includes(term))
      );

  // Tag chips AND with every other filter on this row; the chips combine
  // among THEMSELVES per activeTagRule() — all-of (every chip must be
  // present) by default, any-of (at least one) when switched.
  const tags = activeTagFilterChips();
  if (tags.length) {
    visible =
      activeTagRule() === "any"
        ? visible.filter((link) => tags.some((t) => (link.tags ?? []).includes(t)))
        : visible.filter((link) => tags.every((t) => (link.tags ?? []).includes(t)));
  }

  // Exact equality, not a substring match — this selection feeds a bulk
  // reassign, and a fuzzy owner match would eventually move somebody else's
  // link.
  const owner = document.getElementById("owner-filter").value;
  if (owner) visible = visible.filter((link) => link.owner === owner);

  if (sortKey === "clicks") {
    // Numeric, not lexicographic — localeCompare would order 10 before 9.
    // Unloaded totals sort as 0 rather than throwing.
    visible = [...visible].sort(
      (a, b) => sortDir * (((clickTotals ?? {})[a.slug] ?? 0) - ((clickTotals ?? {})[b.slug] ?? 0))
    );
  } else if (sortKey) {
    // start_at/end_at may be null (unbounded window) — sort those first regardless of direction.
    visible = [...visible].sort(
      (a, b) => sortDir * String(a[sortKey] ?? "").localeCompare(String(b[sortKey] ?? ""))
    );
  }
  return visible;
}

function editRowHtml(link) {
  return `
    <tr class="edit-row" data-slug="${escapeHtml(link.slug)}">
      <td colspan="10">
        <form class="edit-form">
          <label>Destination URL <input type="url" class="edit-target-url" value="${escapeHtml(link.target_url)}" required /></label>
          <div class="grid">
            <label>Starts <input type="datetime-local" class="edit-start-at" value="${isoToDatetimeLocal(link.start_at)}" /></label>
            <label>Expires <input type="datetime-local" class="edit-end-at" value="${isoToDatetimeLocal(link.end_at)}" /></label>
          </div>
          <div class="grid">
            <label>New link password
              <input type="password" class="edit-password" minlength="4" placeholder="New password" />
              <small>(currently ${link.password_protected ? "password-protected" : "no password"} — leave blank to keep as-is)</small>
            </label>
            <label><input type="checkbox" class="edit-remove-password" /> Remove password protection</label>
          </div>
          <label for="edit-tags-${escapeHtml(link.slug)}">Tags (up to 10)</label>
          <div class="tag-input edit-tags" data-max="10" data-note="edit-tags-note-${escapeHtml(link.slug)}"
               data-original-tags="${escapeHtml((link.tags ?? []).join(" "))}">
            ${(link.tags ?? []).map((t) => tagChipHtml(t)).join("")}
            <input type="text" id="edit-tags-${escapeHtml(link.slug)}" class="tag-input-buffer" list="tag-suggestions" />
          </div>
          <p id="edit-tags-note-${escapeHtml(link.slug)}" class="form-note" role="status" hidden></p>
          ${canManageUsers() ? `<label>Owner <select class="edit-owner"></select></label>` : ""}
          <div role="group">
            <button type="submit" class="save-edit-btn">Save</button>
            <button type="button" class="cancel-edit-btn secondary outline">Cancel</button>
          </div>
          <p class="edit-error form-error" role="alert"></p>
        </form>
      </td>
    </tr>
  `;
}

// Deliberately sits between Edit and Delete: it is the reversible correction,
// and it belongs in the user's path BEFORE the irreversible one. Until this
// existed the asymmetry ran the other way — Delete was one click while
// disabling meant opening the edit form and finding a checkbox seven fields
// down, past the password inputs. For a non-technical author fixing a mistake
// that made the destructive action the path of least resistance.
//
// Only `active` and `disabled` are ever stored; `scheduled`/`expired` are
// derived from the window by resolveLinkState(), so this is a genuine binary
// and the label can state the outcome rather than the current state.
function statusToggleHtml(link) {
  const label = link.status === "disabled" ? "Enable" : "Disable";
  return `<button data-slug="${escapeHtml(link.slug)}" data-status="${escapeHtml(link.status)}" class="status-btn outline" aria-label="${label} link ${escapeHtml(link.slug)}">${label}</button>`;
}

// `refreshTotals` defaults to FALSE, so only the bootstrap call at the end of
// this file fetches click totals. Every other caller is mutation-driven —
// create, bulk create, edit save, a cancelled reassign, bulk
// delete/enable/disable, bulk tag/untag, bulk reassign, create-success
// dismiss — and none of those can change a click count, so re-fetching would
// re-pay a whole-store key enumeration plus the shard read fan-out for data
// that provably cannot have moved. Decided 2026-08-12, and deliberately with
// NO refresh control: the totals are stale the moment they render, so an
// on-demand refresh would imply a liveness they have never had. The only
// staleness this introduces is "clicks since page load".
//
// A link created after the totals loaded renders 0, not an em-dash, for free:
// clicksCell only shows the em-dash while `clickTotals` is still null, and
// falls back to 0 for any slug absent from a loaded map.
async function loadLinks({ refreshTotals = false } = {}) {
  const { ok, data } = await api.get("/links");
  if (!ok) return;
  allLinks = data.links;
  // Chips are NOT rebuilt from the data the way #tag-filter's <option>s used
  // to be — a chip for a tag no longer in use stays, and the table says
  // nothing matches, which is more honest than a filter silently resetting
  // itself (see the plan's "One deliberate behaviour change to call out").
  rebuildOwnerFilterOptions();
  renderLinksTable();
  if (refreshTotals) loadClickTotals();
}

// Fired after the table is already on screen, and deliberately not awaited by
// loadLinks(): totals read a different store and are the slower half, so
// blocking the links list on them would make every dashboard load feel like
// the analytics page. A failure here leaves the em-dash in place — the table
// stays fully usable without it, which is why this swallows the error rather
// than surfacing one for a column nobody asked to wait for.
async function loadClickTotals() {
  const { ok, data } = await api.get("/analytics/click-totals");
  if (!ok) return;
  clickTotals = data.totals || {};
  paintClickTotals();
}

// Writes the totals into the cells that already exist, rather than calling
// renderLinksTable(). A full re-render clears the selection by design (see
// the comment there), so re-rendering ~a second after load would silently
// drop any checkbox the operator ticked while the totals were in flight.
function paintClickTotals() {
  for (const row of document.querySelectorAll("#links-body tr[data-slug]:not(.edit-row)")) {
    const cell = row.querySelector('[data-cell="clicks"]');
    if (cell) cell.innerHTML = clicksCell(row.dataset.slug);
  }
}

// States the whole active filter in words plus the count — the second of the
// two layers (with the select's own rendered value) that make the tag
// combining rule visible rather than implicit. Deliberately not a live
// region (see the HTML comment above #filter-summary): it is rebuilt on
// every keystroke of #links-filter, and a keystroke-rate role="status" would
// be noise, not help. Hidden entirely when no filter is active.
function updateFilterSummary() {
  const summaryEl = document.getElementById("filter-summary");
  if (!summaryEl) return;
  const term = document.getElementById("links-filter").value.trim();
  const tags = activeTagFilterChips();
  const owner = document.getElementById("owner-filter").value;
  if (!term && !tags.length && !owner) {
    summaryEl.hidden = true;
    summaryEl.textContent = "";
    return;
  }
  const clauses = [];
  if (tags.length === 1) {
    clauses.push(`tags: #${tags[0]}`);
  } else if (tags.length >= 2) {
    const verb = activeTagRule() === "any" ? "any of" : "all of";
    clauses.push(`tags: ${verb} ${tags.map((t) => `#${t}`).join(", ")}`);
  }
  if (owner) clauses.push(`owner: ${owner}`);
  if (term) clauses.push(`text: "${term}"`);
  const count = getVisibleLinks().length;
  summaryEl.textContent =
    `Showing ${count} of ${allLinks.length} link${allLinks.length === 1 ? "" : "s"}` +
    (clauses.length ? ` · ${clauses.join(" · ")}` : "");
  summaryEl.hidden = false;
}

// With ≥2 chips active and no matching link, the empty state names the
// combining rule rather than leaving the viewer to guess why an all-of
// filter over two real tags returned nothing — the moment a user is most
// confused by all-of is exactly the moment it returns zero rows.
function emptyStateMessage() {
  if (!allLinks.length) return "No links yet — create one above.";
  const tags = activeTagFilterChips();
  if (tags.length >= 2) {
    const rule = activeTagRule();
    const tagList = tags.map((t) => `#${t}`).join(", ");
    const verb = rule === "any" ? "any of" : "all of";
    let message = `No link carries ${verb} ${tagList}.`;
    if (rule === "all") message += ` Try "Any of these tags".`;
    return message;
  }
  return "No links match your filter.";
}

function renderLinksTable() {
  const body = document.getElementById("links-body");
  body.innerHTML = "";
  updateSortIndicators();
  // Filtering, sorting and reloading all reach this function, so clearing
  // the selection here — rather than at each of those call sites — makes it
  // structurally impossible for a future caller to forget: a user must never
  // act on rows they are no longer looking at.
  selectedSlugs.clear();
  updateBulkBar();
  updateFilterSummary();

  const visibleLinks = getVisibleLinks();
  if (!visibleLinks.length) {
    body.innerHTML = `<tr><td colspan="10" class="empty-state">${escapeHtml(emptyStateMessage())}</td></tr>`;
    updateSelectAllState();
    return;
  }

  for (const link of visibleLinks) {
    const shortUrl = shortUrlFor(link.slug);
    const row = document.createElement("tr");
    row.dataset.slug = link.slug;
    row.innerHTML = `
      <td class="select-cell">
        ${canEditLink(link) ? `<label class="checkbox-hit"><input type="checkbox" class="row-select" data-slug="${escapeHtml(link.slug)}" aria-label="Select link ${escapeHtml(link.slug)}" /></label>` : ""}
      </td>
      <td>
        <!-- The origin (http://host) is identical on every row — showing it
             30 times per page is pure width cost for zero information gain,
             and was the single biggest contributor to the table overflowing
             its own container even at a realistic desktop width with only a
             handful of rows. Copy/View still use the full shortUrl below;
             only the displayed chip text drops the redundant prefix. -->
        <span class="slug-chip" title="${escapeHtml(shortUrl)}">/r/${escapeHtml(link.slug)}</span>
        ${link.custom ? '<span class="slug-kind-badge">Custom</span>' : ""}
        ${link.password_protected ? '<span class="lock-badge">Password</span>' : ""}
        ${(link.tags ?? []).map((t) => `<span class="tag-chip">#${escapeHtml(t)}</span>`).join("")}
      </td>
      <td>${escapeHtml(link.owner)}${isDeletedOwner(link.owner) ? ' <span class="status-badge status-disabled">deleted account</span>' : ""}</td>
      <td class="destination-cell" data-cell="destination" title="${escapeHtml(link.target_url)}">${escapeHtml(link.target_url)}</td>
      <td>${formatTimestamp(link.created_at)}</td>
      <td data-cell="status">${statusBadge(link)}</td>
      <td class="clicks-cell" data-cell="clicks">${clicksCell(link.slug)}</td>
      <td data-cell="starts">${formatWindowField(link.start_at, { noteIfFuture: link.status === "active" })}</td>
      <td data-cell="expires">${formatWindowField(link.end_at, { warnIfSoon: link.status === "active" })}</td>
      <td>
        <div role="group">
          <a role="button" class="outline" aria-label="View link ${escapeHtml(link.slug)}" href="links/detail.html?slug=${encodeURIComponent(link.slug)}">View</a>
          <button data-slug="${escapeHtml(link.slug)}" class="copy-btn outline" aria-label="Copy link ${escapeHtml(link.slug)}">Copy</button>
          ${canEditLink(link) ? `
            <button data-slug="${escapeHtml(link.slug)}" class="edit-btn outline" aria-label="Edit link ${escapeHtml(link.slug)}">Edit</button>
            ${statusToggleHtml(link)}
            <button data-slug="${escapeHtml(link.slug)}" class="delete-btn secondary outline" aria-label="Delete link ${escapeHtml(link.slug)}">Delete</button>
          ` : ""}
        </div>
      </td>
    `;
    body.appendChild(row);
    if (canEditLink(link)) {
      row.insertAdjacentHTML("afterend", editRowHtml(link));
      const editRow = body.querySelector(`tr.edit-row[data-slug="${CSS.escape(link.slug)}"]`);
      editRow.style.display = "none";
      wireWindowValidation(editRow.querySelector(".edit-start-at"), editRow.querySelector(".edit-end-at"));
      const ownerSelect = editRow.querySelector(".edit-owner");
      if (ownerSelect) populateOwnerSelect(ownerSelect, link.owner);
    }
  }
  updateSelectAllState();
}

// Row actions are wired once via delegation on #links-body, not re-queried
// and re-attached to every button on every render — renderLinksTable() reruns
// on every sort click and (debounced) filter keystroke, and re-listening
// to every row's buttons each time is wasted work that scales with row count
// for no benefit, since the buttons' behavior never depends on render state.
async function handleCopyClick(btn) {
  copyToClipboard(shortUrlFor(btn.dataset.slug), btn);
}

async function handleDeleteClick(btn) {
  if (!await confirmDialog(`Delete the link "${btn.dataset.slug}"? This can't be undone.`)) return;
  const errorEl = document.getElementById("links-error");
  errorEl.textContent = "";
  // Delete now purges the slug's analytics keys inline
  // (docs/plans/inline-analytics-purge-on-delete.md), so a clicked link's
  // delete takes noticeably longer than before. MEASURED on Akamai
  // 2026-08-15, not modelled: a 12-click link (21 keys) took 1.9 s and an
  // 8-click link (16 keys) 1.6 s, extrapolating to ~7 s at the 95-key
  // shipped ceiling. The earlier "~300 ms typical, up to ~2.3 s" here was
  // modelled at 23 ms/write; real deletes cost ~75 ms each. Matching
  // handleStatusToggleClick's existing btn.disabled pattern so the button
  // can't be double-clicked mid-request.
  //
  // `aria-busy` is what communicates the wait. A disabled button alone says
  // "not clickable", not "working" — and at ~2 s a silent pause on the app's
  // most common corrective action reads as a hang to a non-technical user.
  // Pico styles [aria-busy=true] natively (a spinner prepended via ::before,
  // using a data: SVG already permitted by the CSP's img-src, which is why
  // this needs no new CSS and no new token). It also announces the busy state
  // to assistive tech, which a disabled attribute does not.
  //
  // The label is deliberately NOT changed to "Deleting…": the button lives in
  // a sticky actions column whose width is load-bearing at 390px, where the
  // group is capped at 100px and the figure must stay at 327/327 without
  // scrolling. The spinner's ~1.3em is affordable there; a longer word is not.
  //
  // This does not reopen the closed decision that the purge RESULT is never
  // rendered — that is about the outcome; this is about the wait.
  btn.disabled = true;
  btn.setAttribute("aria-busy", "true");
  const { ok, data } = await api.delete(`/links/${btn.dataset.slug}`);
  if (!ok) {
    btn.disabled = false;
    btn.removeAttribute("aria-busy");
    errorEl.textContent = friendlyError(data, "Could not delete link.");
    return;
  }
  // The create-success banner (with its own live Copy button) would
  // otherwise keep referencing whatever slug was just created even after
  // it's deleted here — silently handing out a Copy affordance for a
  // link that no longer resolves.
  document.getElementById("create-success").hidden = true;
  loadLinks();
}

// No confirmation dialog, deliberately, and that is the point of the control
// rather than an omission: disabling is instantly reversible by the same
// button, and the existing bulk Enable/Disable actions already run without one
// (only bulk delete confirms). Adding friction here would re-create the very
// imbalance this fixes.
async function handleStatusToggleClick(btn) {
  const slug = btn.dataset.slug;
  const next = btn.dataset.status === "disabled" ? "active" : "disabled";
  const errorEl = document.getElementById("links-error");
  errorEl.textContent = "";

  // PATCH gates every field on `if "field" in payload`, so a status-only body
  // is a true partial update and cannot disturb the destination or schedule.
  btn.disabled = true;
  const { ok, data } = await api.patch(`/links/${slug}`, { status: next });
  btn.disabled = false;
  if (!ok) {
    errorEl.textContent = friendlyError(data, `Could not ${next === "disabled" ? "disable" : "enable"} link.`);
    return;
  }

  // Repaint in place instead of calling loadLinks(). A full re-render clears
  // the bulk selection by design, so refreshing here would silently drop any
  // checkbox ticked beforehand — the same reason click totals paint in place.
  const record = allLinks.find((l) => l.slug === slug);
  if (record) record.status = next;
  btn.dataset.status = next;
  const label = next === "disabled" ? "Enable" : "Disable";
  btn.textContent = label;
  btn.setAttribute("aria-label", `${label} link ${slug}`);

  // Starts/Expires repaint too, not just the badge: both cells' "starts soon"
  // and "expires soon" notes are predicated on the link being active, so a
  // toggle that updated only the badge would leave an expiry warning showing
  // on a link that no longer resolves at all.
  const row = btn.closest("tr");
  if (record && row) {
    row.querySelector('[data-cell="status"]').innerHTML = statusBadge(record);
    row.querySelector('[data-cell="starts"]').innerHTML = formatWindowField(record.start_at, { noteIfFuture: record.status === "active" });
    row.querySelector('[data-cell="expires"]').innerHTML = formatWindowField(record.end_at, { warnIfSoon: record.status === "active" });
  }
}

function handleEditToggleClick(btn) {
  const editRow = document.querySelector(`#links-body tr.edit-row[data-slug="${CSS.escape(btn.dataset.slug)}"]`);
  const opening = editRow.style.display === "none";
  editRow.style.display = opening ? "" : "none";
  // Without this, opening the edit form on a row near/below the fold
  // produces a click that visually does nothing — the newly-revealed
  // form renders off-screen with no indication anything happened.
  if (opening) editRow.scrollIntoView({ block: "center" });
}

function handleCancelEditClick(btn) {
  btn.closest("tr.edit-row").style.display = "none";
}

async function handleEditFormSubmit(form) {
  const editRow = form.closest("tr.edit-row");
  const slug = editRow.dataset.slug;
  const targetUrl = form.querySelector(".edit-target-url").value.trim();
  const startAt = datetimeLocalToIso(form.querySelector(".edit-start-at").value);
  const endAt = datetimeLocalToIso(form.querySelector(".edit-end-at").value);
  const newPassword = form.querySelector(".edit-password").value;
  const removePassword = form.querySelector(".edit-remove-password").checked;
  const errorEl = form.querySelector(".edit-error");
  errorEl.textContent = "";

  // The tag-wipe guard (docs/plans/tag-ux-chips-and-filtering.md). PATCH
  // {"tags": [...]} is a full replacement, so `tags` must be sent ONLY when
  // it actually changed from what this row was rendered with — otherwise an
  // edit-row save that never touched tags would read as "zero chips" and
  // wipe every tag on save. Three layers, all required:
  //   1. tags is omitted from the payload unless the committed chip set
  //      differs from data-original-tags (compared as sets, so a save is
  //      never falsely flagged "changed" by chip reordering alone).
  //      Intentionally clearing every tag is a change to [], so it still
  //      sends.
  //   2. A missing .tag-input container (a markup/JS break) leaves
  //      tagsChanged false, so the save degrades to "tags left alone",
  //      never to data loss.
  //   3. linkRecord.tags below is updated ONLY when the key was actually
  //      sent, so the optimistic row update can't claim a change that was
  //      never requested.
  const tagContainer = form.querySelector(".tag-input");
  let tagsChanged = false;
  let tagList = null;
  if (tagContainer) {
    // Every form submit commits the buffer first, or a tag the user just
    // typed and did not press Enter on would be silently dropped.
    commitTagBuffer(tagContainer);
    tagList = readTagChips(tagContainer);
    const original = (tagContainer.dataset.originalTags || "").split(" ").filter(Boolean);
    const sameSet = tagList.length === original.length && tagList.every((t) => original.includes(t));
    tagsChanged = !sameSet;
  }

  // `status` is deliberately NOT sent here. It used to be, from a checkbox in
  // this form, but the row's own Disable/Enable button now owns it — and the
  // two together were a live bug rather than a redundancy: this form's markup
  // is built once by renderLinksTable(), so a checkbox rendered before a
  // button toggle would still hold the old value and silently revert it on
  // Save. PATCH gates each field on `if "field" in payload`, so omitting it
  // leaves the stored status untouched.
  const patchPayload = { target_url: targetUrl, start_at: startAt, end_at: endAt };
  if (tagsChanged) patchPayload.tags = tagList;
  const { ok, data } = await api.patch(`/links/${slug}`, patchPayload);
  if (!ok) {
    const msg = friendlyError(data, "Could not update link.");
    errorEl.textContent = data && data.host ? `${msg} (${data.host})` : msg;
    return;
  }

  // Reflect the saved destination/schedule in the visible row immediately —
  // not just in the "saved" message — in case the password step below fails
  // and this function returns before reaching the full loadLinks() refresh.
  const linkRecord = allLinks.find((l) => l.slug === slug);
  if (linkRecord) {
    linkRecord.target_url = targetUrl;
    linkRecord.start_at = startAt;
    linkRecord.end_at = endAt;
    if (tagsChanged) linkRecord.tags = tagList;
  }
  const displayRow = editRow.previousElementSibling;
  if (displayRow) {
    // Positional indices shifted by 1 when the select-column was inserted
    // Looked up by data-cell rather than by index. These were children[3],
    // [6] and [7], and the comment they replace recorded that the indices had
    // already shifted once when the select column was inserted — adding the
    // Clicks column would have shifted them again, silently writing the start
    // date into the clicks cell.
    // Starts/Expires were [5]/[6], now [6]/[7]. This fails silently if wrong.
    displayRow.querySelector('[data-cell="destination"]').textContent = targetUrl;
    displayRow.querySelector('[data-cell="starts"]').innerHTML = formatWindowField(startAt, { noteIfFuture: linkRecord?.status === "active" });
    displayRow.querySelector('[data-cell="expires"]').innerHTML = formatWindowField(endAt, { warnIfSoon: linkRecord?.status === "active" });
  }

  if (removePassword || newPassword) {
    const passwordResult = await api.post(`/links/${slug}/password`, {
      password: removePassword ? null : newPassword,
    });
    if (!passwordResult.ok) {
      // The destination/schedule PATCH above already succeeded — say so,
      // so a failed password change never reads as "nothing happened."
      errorEl.textContent =
        "Destination and schedule saved. " +
        friendlyError(passwordResult.data, "Could not update link password.", {
          invalid_password: "Link passwords must be at least 4 characters.",
        });
      return;
    }
  }

  // Owner is a separate call, exactly mirroring the password change above —
  // reassignment goes through the shared bulk-action endpoint (there is no
  // single-link owner endpoint; see docs/plans/link-tags-and-ownership.md's
  // "Owner reassignment goes through bulk-action" trade-off), with a
  // count-and-target-bearing confirm since reassignment is destructive-ish.
  const ownerSelect = form.querySelector(".edit-owner");
  if (ownerSelect && linkRecord && ownerSelect.value !== linkRecord.owner) {
    if (!await confirmDialog(`Reassign "${slug}" to "${ownerSelect.value}"?`, { confirmLabel: "Reassign" })) {
      loadLinks();
      return;
    }
    const reassignResult = await api.post("/links/bulk-action", {
      slugs: [slug],
      action: "reassign",
      owner: ownerSelect.value,
    });
    if (!reassignResult.ok) {
      errorEl.textContent =
        "Destination, schedule and tags saved. " +
        friendlyError(reassignResult.data, "Could not reassign this link.");
      return;
    }
  }

  loadLinks();
}

document.getElementById("links-body").addEventListener("click", (e) => {
  const btn = e.target.closest(".copy-btn, .delete-btn, .edit-btn, .status-btn, .cancel-edit-btn");
  if (!btn) return;
  if (btn.matches(".copy-btn")) handleCopyClick(btn);
  else if (btn.matches(".delete-btn")) handleDeleteClick(btn);
  else if (btn.matches(".edit-btn")) handleEditToggleClick(btn);
  else if (btn.matches(".status-btn")) handleStatusToggleClick(btn);
  else if (btn.matches(".cancel-edit-btn")) handleCancelEditClick(btn);
});

document.getElementById("links-body").addEventListener("submit", (e) => {
  if (!e.target.matches(".edit-form")) return;
  e.preventDefault();
  handleEditFormSubmit(e.target);
});

// Row checkboxes go through the same delegated #links-body listener as the
// row-action buttons above, rather than a per-row listener attached in
// renderLinksTable() — that delegation exists deliberately for performance
// (see the comment above handleCopyClick) and applies equally here.
document.getElementById("links-body").addEventListener("change", (e) => {
  const cb = e.target.closest(".row-select");
  if (!cb) return;
  if (cb.checked) selectedSlugs.add(cb.dataset.slug);
  else selectedSlugs.delete(cb.dataset.slug);
  updateSelectAllState();
  updateBulkBar();
});

document.getElementById("select-all-links").addEventListener("change", (e) => {
  const selectableSlugs = getSelectableVisibleSlugs();
  if (e.target.checked) {
    selectableSlugs.forEach((slug) => selectedSlugs.add(slug));
  } else {
    selectableSlugs.forEach((slug) => selectedSlugs.delete(slug));
  }
  document.querySelectorAll("#links-body .row-select").forEach((cb) => {
    cb.checked = selectedSlugs.has(cb.dataset.slug);
  });
  updateSelectAllState();
  updateBulkBar();
});

// Renders one <li> per row error. Shared shape for both bulk endpoints: a
// bulk-create row error carries {line, slug, error} while a bulk-action row
// error carries {slug, error} — the "line" label is simply omitted when
// there isn't one, so this needs no separate branch per caller.
function renderRowErrorList(rowErrors, overrides) {
  const items = rowErrors
    .map((rowErr) => {
      const label = rowErr.line != null ? `Line ${rowErr.line}` : rowErr.slug;
      const problem = escapeHtml(friendlyError(rowErr, "This row isn't valid.", overrides));
      return `<li>${label ? `<strong>${escapeHtml(String(label))}:</strong> ` : ""}${problem}</li>`;
    })
    .join("");
  return `<ul>${items}</ul>`;
}

async function handleBulkAction(action) {
  const slugs = [...selectedSlugs];
  if (!slugs.length || slugs.length > BULK_MAX_SELECTION) return;

  if (action === "delete") {
    const message =
      slugs.length === 1
        ? `Delete the link "${slugs[0]}"? This can't be undone.`
        : `Delete ${slugs.length} links? This can't be undone.`;
    const options = slugs.length === 1 ? {} : { confirmLabel: `Delete ${slugs.length} links` };
    if (!(await confirmDialog(message, options))) return;
  }

  const errorEl = document.getElementById("links-error");
  const errorsEl = document.getElementById("bulk-action-errors");
  const successEl = document.getElementById("links-success");
  errorEl.textContent = "";
  errorsEl.hidden = true;
  errorsEl.innerHTML = "";
  successEl.hidden = true;

  const { ok, data } = await api.post("/links/bulk-action", { slugs, action });
  if (!ok) {
    if (data && data.error === "bulk_validation_failed") {
      errorEl.textContent = `Nothing was changed — ${data.row_errors.length} of the selected links are no longer available. Refresh and try again.`;
      errorsEl.innerHTML = renderRowErrorList(data.row_errors);
      errorsEl.hidden = false;
    } else {
      errorEl.textContent = friendlyError(data, "Could not update the selected links.");
    }
    return;
  }

  if (data.partial) {
    // docs/plans/write-throttle-resilience.md: names what applied, and
    // narrows the selection to not_applied so clicking the same button
    // again retries exactly the failures — a better next move than any
    // sentence alone.
    const verb = action === "delete" ? "Deleted" : action === "enable" ? "Enabled" : "Disabled";
    errorEl.textContent =
      `${verb} ${data.count} of ${slugs.length} links. The store was busy for the rest — ` +
      `they're still selected below so you can try again.`;
    await loadLinks();
    narrowSelectionTo(data.not_applied || []);
    return;
  }

  const verb = action === "delete" ? "Deleted" : action === "enable" ? "Enabled" : "Disabled";
  successEl.textContent = `${verb} ${data.count} link${data.count === 1 ? "" : "s"}.`;
  successEl.hidden = false;

  if (action === "delete") {
    // Same reasoning as handleDeleteClick: the create-success banner's own
    // live Copy button may reference a slug that was just bulk-deleted.
    document.getElementById("create-success").hidden = true;
  }

  loadLinks();
}

document.getElementById("bulk-enable-btn").addEventListener("click", () => handleBulkAction("enable"));
document.getElementById("bulk-disable-btn").addEventListener("click", () => handleBulkAction("disable"));
document.getElementById("bulk-delete-btn").addEventListener("click", () => handleBulkAction("delete"));

// action is "tag" or "untag". No confirmation dialog: both are reversible by
// the adjacent button, and DESIGN.md's Bulk Action Bar rule says confirming
// a reversible action trains people to dismiss confirms.
async function handleBulkTag(action) {
  const slugs = [...selectedSlugs];
  if (!slugs.length || slugs.length > BULK_MAX_SELECTION) return;
  const tagContainer = document.getElementById("bulk-tag-input").closest(".tag-input");
  if (tagContainer) commitTagBuffer(tagContainer);
  const tagList = tagContainer ? readTagChips(tagContainer) : [];
  if (!tagList.length) return;

  const errorEl = document.getElementById("links-error");
  const errorsEl = document.getElementById("bulk-action-errors");
  const successEl = document.getElementById("links-success");
  errorEl.textContent = "";
  errorsEl.hidden = true;
  errorsEl.innerHTML = "";
  successEl.hidden = true;

  const { ok, data } = await api.post("/links/bulk-action", { slugs, action, tags: tagList });
  if (!ok) {
    if (data && data.error === "bulk_validation_failed") {
      errorEl.textContent = `Nothing was changed — ${data.row_errors.length} of the selected links are no longer available. Refresh and try again.`;
      errorsEl.innerHTML = renderRowErrorList(data.row_errors);
      errorsEl.hidden = false;
    } else {
      errorEl.textContent = friendlyError(data, "Could not update tags on the selected links.");
    }
    return;
  }

  const verb = action === "tag" ? "Tagged" : "Untagged";

  if (data.partial) {
    errorEl.textContent =
      `${verb} ${data.count} of ${slugs.length} links. The store was busy for the rest — ` +
      `they're still selected below so you can try again.`;
    await loadLinks();
    narrowSelectionTo(data.not_applied || []);
    return;
  }

  successEl.textContent = `${verb} ${data.count} link${data.count === 1 ? "" : "s"}.`;
  successEl.hidden = false;
  if (tagContainer) clearTagChips(tagContainer);
  loadLinks();
}

document.getElementById("bulk-tag-add-btn").addEventListener("click", () => handleBulkTag("tag"));
document.getElementById("bulk-tag-remove-btn").addEventListener("click", () => handleBulkTag("untag"));

// Reassignment is destructive-ish (a link disappears from its old owner's
// dashboard), so unlike tag/untag it gets a count-and-target-bearing
// confirmDialog — matching the bulk-delete confirmation's
// count-states-the-scale convention.
async function handleBulkReassign() {
  const slugs = [...selectedSlugs];
  if (!slugs.length || slugs.length > BULK_MAX_SELECTION) return;
  const owner = document.getElementById("bulk-owner-select").value;
  if (!owner) return;

  const n = slugs.length;
  if (!await confirmDialog(
    `Reassign ${n} link${n === 1 ? "" : "s"} to "${owner}"? They will move out of their current owners' lists.`,
    { confirmLabel: `Reassign ${n} link${n === 1 ? "" : "s"}` }
  )) return;

  const errorEl = document.getElementById("links-error");
  const errorsEl = document.getElementById("bulk-action-errors");
  const successEl = document.getElementById("links-success");
  errorEl.textContent = "";
  errorsEl.hidden = true;
  errorsEl.innerHTML = "";
  successEl.hidden = true;

  const { ok, data } = await api.post("/links/bulk-action", { slugs, action: "reassign", owner });
  if (!ok) {
    if (data && data.error === "bulk_validation_failed") {
      errorEl.textContent = `Nothing was changed — ${data.row_errors.length} of the selected links are no longer available. Refresh and try again.`;
      errorsEl.innerHTML = renderRowErrorList(data.row_errors);
      errorsEl.hidden = false;
    } else {
      errorEl.textContent = friendlyError(data, "Could not reassign the selected links.");
    }
    return;
  }

  if (data.partial) {
    errorEl.textContent =
      `Reassigned ${data.count} of ${slugs.length} links to "${owner}". The store was busy for the rest — ` +
      `they're still selected below so you can try again.`;
    await loadLinks();
    narrowSelectionTo(data.not_applied || []);
    return;
  }

  successEl.textContent = `Reassigned ${data.count} link${data.count === 1 ? "" : "s"} to "${owner}".`;
  successEl.hidden = false;
  loadLinks();
}

document.getElementById("bulk-reassign-btn").addEventListener("click", handleBulkReassign);

// Both schedule and repoint confirm, and that is a decision, not a default
// (DESIGN.md's Bulk Action Bar rule: confirm what is not reversible by the
// adjacent control). Enable/Disable and tag/untag skip it because the
// neighbouring button undoes them; reassign confirms because the UI no
// longer shows each link's previous owner. Schedule and repoint are in
// reassign's category — neither Set nor Clear nor Repoint can restore N
// different previous per-link values.
//
// mode is "set" or "clear". A BLANK datetime input under "set" means "leave
// this side alone" (posted as an omitted key), never "clear it" — the one
// place this bulk bar deliberately diverges from the row edit form, where a
// blank field clears. That divergence is the whole point: a bulk clear must
// be asked for, not achieved by leaving a box empty. "clear" always posts
// both sides as explicit null.
async function handleBulkSchedule(mode) {
  const slugs = [...selectedSlugs];
  if (!slugs.length || slugs.length > BULK_MAX_SELECTION) return;

  const startInput = document.getElementById("bulk-schedule-start");
  const endInput = document.getElementById("bulk-schedule-end");

  const payload = { slugs, action: "schedule" };
  const n = slugs.length;
  let message, confirmLabel, verb;

  if (mode === "clear") {
    payload.start_at = null;
    payload.end_at = null;
    message = `Clear the schedule on ${n} link${n === 1 ? "" : "s"}? They become active immediately and never expire.`;
    confirmLabel = `Clear schedule on ${n} link${n === 1 ? "" : "s"}`;
    verb = "Cleared the schedule on";
  } else {
    const hasStart = Boolean(startInput.value);
    const hasEnd = Boolean(endInput.value);
    if (!hasStart && !hasEnd) return;
    if (hasStart) payload.start_at = datetimeLocalToIso(startInput.value);
    if (hasEnd) payload.end_at = datetimeLocalToIso(endInput.value);

    if (hasStart && hasEnd) {
      message = `Set Starts to ${formatTimestamp(payload.start_at)} and Expires to ${formatTimestamp(payload.end_at)} on ${n} link${n === 1 ? "" : "s"}? Any existing dates on those links are replaced.`;
    } else if (hasEnd) {
      message = `Set Expires to ${formatTimestamp(payload.end_at)} on ${n} link${n === 1 ? "" : "s"}? Their Starts dates are left as they are.`;
    } else {
      message = `Set Starts to ${formatTimestamp(payload.start_at)} on ${n} link${n === 1 ? "" : "s"}? Their Expires dates are left as they are.`;
    }
    confirmLabel = `Set schedule on ${n} link${n === 1 ? "" : "s"}`;
    verb = "Rescheduled";
  }

  if (!(await confirmDialog(message, { confirmLabel }))) return;

  const errorEl = document.getElementById("links-error");
  const errorsEl = document.getElementById("bulk-action-errors");
  const successEl = document.getElementById("links-success");
  errorEl.textContent = "";
  errorsEl.hidden = true;
  errorsEl.innerHTML = "";
  successEl.hidden = true;

  const { ok, data } = await api.post("/links/bulk-action", payload);
  if (!ok) {
    if (data && data.error === "bulk_validation_failed") {
      errorEl.textContent = `Nothing was changed — ${data.row_errors.length} of the selected links couldn't take this schedule. Refresh and try again.`;
      errorsEl.innerHTML = renderRowErrorList(data.row_errors);
      errorsEl.hidden = false;
    } else {
      errorEl.textContent = friendlyError(data, "Could not reschedule the selected links.");
    }
    return;
  }

  if (data.partial) {
    errorEl.textContent =
      `${verb} ${data.count} of ${n} links. The store was busy for the rest — ` +
      `they're still selected below so you can try again.`;
    await loadLinks();
    narrowSelectionTo(data.not_applied || []);
    return;
  }

  successEl.textContent = `${verb} ${data.count} link${data.count === 1 ? "" : "s"}.`;
  successEl.hidden = false;
  loadLinks();
}

document.getElementById("bulk-schedule-set-btn").addEventListener("click", () => handleBulkSchedule("set"));
document.getElementById("bulk-schedule-clear-btn").addEventListener("click", () => handleBulkSchedule("clear"));

async function handleBulkRepoint() {
  const slugs = [...selectedSlugs];
  if (!slugs.length || slugs.length > BULK_MAX_SELECTION) return;
  const urlInput = document.getElementById("bulk-repoint-url");
  const targetUrl = urlInput.value.trim();
  if (!targetUrl) return;

  const n = slugs.length;
  // A 4096-byte destination would wreck the ~26rem confirm dialog, so the
  // displayed URL is truncated — the request itself still sends the full
  // value, and a too-long one is caught server-side same as everywhere else.
  const displayUrl = targetUrl.length > 80 ? `${targetUrl.slice(0, 80)}…` : targetUrl;
  if (!await confirmDialog(
    `Point ${n} link${n === 1 ? "" : "s"} at "${displayUrl}"? Their current destinations can't be recovered.`,
    { confirmLabel: `Repoint ${n} link${n === 1 ? "" : "s"}` }
  )) return;

  const errorEl = document.getElementById("links-error");
  const errorsEl = document.getElementById("bulk-action-errors");
  const successEl = document.getElementById("links-success");
  errorEl.textContent = "";
  errorsEl.hidden = true;
  errorsEl.innerHTML = "";
  successEl.hidden = true;

  const { ok, data } = await api.post("/links/bulk-action", { slugs, action: "repoint", target_url: targetUrl });
  if (!ok) {
    if (data && data.error === "bulk_validation_failed") {
      errorEl.textContent = `Nothing was changed — ${data.row_errors.length} of the selected links are no longer available. Refresh and try again.`;
      errorsEl.innerHTML = renderRowErrorList(data.row_errors);
      errorsEl.hidden = false;
    } else {
      errorEl.textContent = friendlyError(data, "Could not repoint the selected links.");
    }
    return;
  }

  if (data.partial) {
    errorEl.textContent =
      `Repointed ${data.count} of ${slugs.length} links. The store was busy for the rest — ` +
      `they're still selected below so you can try again.`;
    await loadLinks();
    narrowSelectionTo(data.not_applied || []);
    return;
  }

  successEl.textContent = `Repointed ${data.count} link${data.count === 1 ? "" : "s"}.`;
  successEl.hidden = false;
  urlInput.value = "";
  loadLinks();
}

document.getElementById("bulk-repoint-btn").addEventListener("click", handleBulkRepoint);

// Dashboard-local overrides for api/bulk.py's per-row error codes — the
// invalid_password override at handleEditFormSubmit is the precedent for
// giving one call site's copy priority over the shared ERROR_MESSAGES map.
// Codes not listed here (invalid_custom_slug, slug_taken) already read fine
// from the shared map.
const BULK_ROW_MESSAGES = {
  missing_target_url: "This line has a short link but no destination URL.",
  duplicate_slug_in_submission: "This short link appears earlier in your list.",
  custom_slug_forbidden: "You don't have permission to choose your own short links — leave the first column blank.",
  invalid_target_url: "Not a valid destination URL (include https://). If this is a header row, delete it.",
  destination_not_allowed: "This destination isn't allowed by the site's URL policy.",
  // docs/plans/write-throttle-resilience.md: the one row-error code that can
  // appear on a PARTIAL (200 ok:false) response rather than only on a 400
  // bulk_validation_failed one — renders through the same renderBulkErrorTable
  // / renderRowErrorList either way, no new function needed.
  write_failed: "The store was busy and this row wasn't written. Try again.",
};

// Mirrors api/bulk.py's MAX_BULK_BODY_BYTES so a large file can be rejected
// before FileReader ever reads it. The server is authoritative — a drift
// here only ever produces a body_too_large rejection naming the real limit,
// never silently wrong client behavior (same reasoning as BULK_MAX_SELECTION
// above).
const BULK_MAX_BODY_BYTES = 262144;

// Builds the Line/Short link/Problem table for a bulk_validation_failed
// response. No truncation needed — MAX_BULK_ROWS caps row_errors at 50,
// a readable table rather than a wall (see the plan's rejected-alternatives
// note on the now-removed 200-row cutoff).
function renderBulkErrorTable(rowErrors) {
  const rows = rowErrors
    .map((rowErr) => {
      const line = escapeHtml(String(rowErr.line));
      const slug = rowErr.slug ? escapeHtml(rowErr.slug) : "—";
      const problem = escapeHtml(friendlyError(rowErr, "This row isn't valid.", BULK_ROW_MESSAGES));
      return `<tr><td>${line}</td><td>${slug}</td><td>${problem}</td></tr>`;
    })
    .join("");
  return `
    <table id="bulk-errors-table">
      <thead><tr><th>Line</th><th>Short link</th><th>Problem</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

// Clears all three bulk-create result elements — called at the top of every
// render path so a stale success/error never sits alongside a fresh one.
function clearBulkResults() {
  const errorEl = document.getElementById("bulk-error");
  const errorsEl = document.getElementById("bulk-errors");
  const successEl = document.getElementById("bulk-success");
  errorEl.textContent = "";
  errorsEl.hidden = true;
  errorsEl.innerHTML = "";
  successEl.hidden = true;
  successEl.textContent = "";
}

// The file input is a convenience, not a second submission path: choosing a
// file reads it into #bulk-text and clears the input, so there is always
// exactly one source of truth (the textarea) at submit time.
document.getElementById("bulk-file").addEventListener("change", (e) => {
  const fileInput = e.target;
  const file = fileInput.files[0];
  if (!file) return;

  if (file.size > BULK_MAX_BODY_BYTES) {
    clearBulkResults();
    document.getElementById("bulk-error").textContent =
      `That's too much text — the limit is ${Math.floor(BULK_MAX_BODY_BYTES / 1024)} KB.`;
    fileInput.value = "";
    return;
  }

  const reader = new FileReader();
  reader.addEventListener("load", () => {
    document.getElementById("bulk-text").value = reader.result;
    fileInput.value = "";
  });
  reader.readAsText(file);
});

document.getElementById("bulk-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = document.getElementById("bulk-text").value;
  const startAt = datetimeLocalToIso(document.getElementById("bulk-start-at").value);
  const endAt = datetimeLocalToIso(document.getElementById("bulk-end-at").value);
  const password = document.getElementById("bulk-password").value || null;
  const bulkTagContainer = document.getElementById("bulk-tags").closest(".tag-input");
  if (bulkTagContainer) commitTagBuffer(bulkTagContainer);
  const tagList = bulkTagContainer ? readTagChips(bulkTagContainer) : [];

  clearBulkResults();
  const errorEl = document.getElementById("bulk-error");
  const errorsEl = document.getElementById("bulk-errors");
  const successEl = document.getElementById("bulk-success");

  const payload = { text, start_at: startAt, end_at: endAt, tags: tagList };
  if (password) payload.password = password;

  const { ok, data } = await api.post("/links/bulk", payload);
  if (!ok) {
    if (data && data.error === "bulk_validation_failed") {
      const n = data.row_errors.length;
      errorEl.textContent = `Nothing was created — ${n} row${n === 1 ? "" : "s"} need${n === 1 ? "s" : ""} fixing.`;
      errorsEl.innerHTML = renderBulkErrorTable(data.row_errors);
      errorsEl.hidden = false;
    } else if (data && data.error === "too_many_rows") {
      // The textarea is deliberately NOT cleared here, so the user can cut
      // the list down in place rather than re-pasting from scratch.
      const batches = Math.ceil(data.row_count / data.max_rows);
      errorEl.textContent =
        `Too many rows — this file has ${data.row_count} and the limit is ${data.max_rows} per submission. ` +
        `Split it into ${batches} smaller batch${batches === 1 ? "" : "es"} and submit them one at a time.`;
    } else if (data && data.error === "body_too_large") {
      errorEl.textContent = `That's too much text — the limit is ${Math.floor(data.max_bytes / 1024)} KB.`;
    } else {
      errorEl.textContent = friendlyError(data, "Could not create links.", {
        invalid_password: "Link passwords must be at least 4 characters.",
      });
    }
    return;
  }

  if (data.partial) {
    // docs/plans/write-throttle-resilience.md: the store was busy partway
    // through — some rows landed, some didn't. The textarea is deliberately
    // NOT cleared (same reasoning as too_many_rows above) so the operator
    // can re-submit exactly the rows listed in not_created, and no success
    // banner is shown since the submission did not fully succeed.
    const notCreated = data.not_created || [];
    errorEl.textContent =
      `Created ${data.count} of ${data.row_count} links. ${notCreated.length} row${notCreated.length === 1 ? "" : "s"} ` +
      `could not be written because the store was busy. The list below has been left as you submitted it — ` +
      `re-submit only the rows listed here.`;
    errorsEl.innerHTML = renderBulkErrorTable(notCreated);
    errorsEl.hidden = false;
    loadLinks();
    return;
  }

  successEl.textContent = `Created ${data.count} link${data.count === 1 ? "" : "s"}.`;
  successEl.hidden = false;
  document.getElementById("bulk-text").value = "";
  document.getElementById("bulk-start-at").value = "";
  document.getElementById("bulk-end-at").value = "";
  document.getElementById("bulk-password").value = "";
  if (bulkTagContainer) clearTagChips(bulkTagContainer);
  // The panel stays open — unlike #advanced-options, its success banner
  // lives inside the details it belongs to, so closing it would hide the
  // payoff the user just triggered.
  loadLinks();
});

document.getElementById("create-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const targetUrl = document.getElementById("target-url").value;
  const customSlug = document.getElementById("custom-slug").value.trim() || null;
  const startAt = datetimeLocalToIso(document.getElementById("start-at").value);
  const endAt = datetimeLocalToIso(document.getElementById("end-at").value);
  const password = document.getElementById("link-password").value || null;
  // Every form submit commits the buffer first, or a tag the user just typed
  // and did not press Enter on would be silently dropped.
  const tagContainer = document.getElementById("link-tags").closest(".tag-input");
  if (tagContainer) commitTagBuffer(tagContainer);
  const tagList = tagContainer ? readTagChips(tagContainer) : [];
  const errorEl = document.getElementById("create-error");
  const successEl = document.getElementById("create-success");
  errorEl.textContent = "";
  successEl.hidden = true;

  const payload = {
    target_url: targetUrl,
    custom_slug: customSlug,
    start_at: startAt,
    end_at: endAt,
    tags: tagList,
  };
  if (password) payload.password = password;

  const { ok, data } = await api.post("/links", payload);
  if (!ok) {
    const msg = friendlyError(data, "Could not create link.", {
      invalid_password: "Link passwords must be at least 4 characters.",
    });
    errorEl.textContent = data && data.host ? `${msg} (${data.host})` : msg;
    return;
  }
  document.getElementById("target-url").value = "";
  document.getElementById("custom-slug").value = "";
  document.getElementById("start-at").value = "";
  document.getElementById("end-at").value = "";
  document.getElementById("link-password").value = "";
  if (tagContainer) clearTagChips(tagContainer);
  document.getElementById("advanced-options").open = false;

  renderCreateSuccess(data.slug);
  loadLinks();
});

// Extracted so a domain change (below) can re-render this banner for the
// slug just created, without duplicating the markup/wiring. Stores the slug
// on the element via dataset rather than a closure variable, so the
// domain-change callback (which runs long after this call returns) can find
// it again. The Copy button's handler calls shortUrlFor(slug) *inside* the
// handler rather than capturing the URL at render time, so it keeps working
// across a later domain change without needing to be re-registered.
function renderCreateSuccess(slug) {
  const successEl = document.getElementById("create-success");
  successEl.dataset.slug = slug;
  successEl.innerHTML = `Link created: <span class="slug-chip">${escapeHtml(shortUrlFor(slug))}</span> <button type="button" class="outline">Copy</button>`;
  successEl.hidden = false;
  successEl.querySelector("button").addEventListener("click", (evt) => copyToClipboard(shortUrlFor(slug), evt.currentTarget));
}

// A domain change re-renders the table (row title tooltips) and, if it's
// currently visible, the create-success banner — both of which embed a
// shortUrlFor(...) value that would otherwise go stale until the next
// action. renderLinksTable() also clears any bulk selection as a side effect
// of its normal re-render path (selectedSlugs.clear() at its top, by
// design — same as a filter or sort re-render), which is expected here too,
// not a bug.
onDomainChange(() => {
  renderLinksTable();
  const successEl = document.getElementById("create-success");
  if (!successEl.hidden && successEl.dataset.slug) renderCreateSuccess(successEl.dataset.slug);
});

let filterDebounceTimer = null;
document.getElementById("links-filter").addEventListener("input", () => {
  clearTimeout(filterDebounceTimer);
  filterDebounceTimer = setTimeout(renderLinksTable, 200);
});

document.getElementById("tag-rule").addEventListener("change", renderLinksTable);
document.getElementById("owner-filter").addEventListener("change", renderLinksTable);

document.querySelectorAll("#links-table th.sortable").forEach((th) => {
  const activate = () => {
    const key = th.dataset.sortKey;
    sortDir = sortKey === key ? -sortDir : 1;
    sortKey = key;
    renderLinksTable();
  };
  th.addEventListener("click", activate);
  th.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      activate();
    }
  });
});

// Lets links/detail.html's "Edit" button deep-link straight into this
// row's edit form instead of just landing on the dashboard and making
// the user re-locate the link themselves.
function openDeepLinkedEditRow() {
  const editSlug = new URLSearchParams(location.search).get("edit");
  if (!editSlug) return;
  const btn = document.querySelector(`#links-body .edit-btn[data-slug="${CSS.escape(editSlug)}"]`);
  if (!btn) return;
  btn.click();
  btn.closest("tr").scrollIntoView({ block: "center" });
}

wireWindowValidation(document.getElementById("start-at"), document.getElementById("end-at"));
wireWindowValidation(document.getElementById("bulk-start-at"), document.getElementById("bulk-end-at"));
wireWindowValidation(document.getElementById("bulk-schedule-start"), document.getElementById("bulk-schedule-end"));

// loadMe() must resolve before loadLinks()'s first render — renderLinksTable()
// reads currentPrincipal (set by loadMe()) to decide whether to show each
// row's Edit/Delete controls, and running them concurrently could render
// the first paint with every row's Edit/Delete hidden, even the viewer's own.
loadMe().then(() => loadLinks({ refreshTotals: true }).then(openDeepLinkedEditRow));

// --- CSV export -------------------------------------------------------------
// Client-side only: the dashboard already holds every link the user may see in
// `allLinks`, so this needs no endpoint, no permission work and no selection.
// It exports the current filtered/sorted view, matching how select-all behaves.
//
// Deliberately NOT re-importable into bulk create: that parser is
// first-delimiter-wins on two columns, so any third column would land inside
// the destination and fail as an invalid URL. This file is for reading in a
// spreadsheet, not for restoring — re-importing would create new links and
// collide on every slug that still exists.

const CSV_COLUMNS = [
  ["Short link", (l) => shortUrlFor(l.slug)],
  ["Owner", (l) => l.owner],
  ["Destination", (l) => l.target_url],
  ["Created", (l) => l.created_at ?? ""],
  // Two columns, deliberately. `State` is what the dashboard shows and what
  // an operator means by "is this link working" (see resolveLinkState);
  // `Status` stays the raw stored field, because a CSV is also a data export
  // and silently replacing a stored value with a derived one would be its own
  // small lie. Exporting only the derived value would have made the file
  // disagree with nothing; exporting only the stored one is what let the file
  // disagree with the table it came from.
  ["Clicks", (l) => (clickTotals ?? {})[l.slug] ?? ""],
  ["State", (l) => resolveLinkState(l)],
  ["Status", (l) => l.status],
  ["Starts", (l) => l.start_at ?? ""],
  ["Expires", (l) => l.end_at ?? ""],
  ["Tags", (l) => (l.tags ?? []).join(" ")],
];

// RFC 4180: quote a field that contains a comma, quote, CR or LF, and double
// any quote inside it. Destinations really do contain commas — the bulk parser
// has a case for exactly that — so this is load-bearing, not defensive.
//
// Excel/Sheets treats a cell whose text begins with =, +, - or @ as a
// formula regardless of RFC 4180 quoting — CSV/formula injection (CWE-1236).
// Prefixing with a leading "'" is the standard mitigation: Excel's own
// "force text" convention for a manually-typed cell applies identically to a
// CSV cell on import. Only "Owner" (a username — api/users.py places no
// character restriction on one) can actually start with one of these today;
// every other column is either character-restricted (slugs, tags) or always
// begins with a fixed http(s):// prefix. Neutralized for every field here
// rather than only "Owner" so a future column with less rigorous validation
// is covered by construction, not by an invariant someone has to remember.
function csvField(value) {
  let s = String(value ?? "");
  if (/^[=+\-@]/.test(s)) s = "'" + s;
  return /[",\r\n]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s;
}

function linksToCsv(links) {
  const rows = [CSV_COLUMNS.map(([header]) => header)];
  for (const link of links) rows.push(CSV_COLUMNS.map(([, get]) => get(link)));
  // CRLF per RFC 4180, and a UTF-8 BOM so Excel reads non-ASCII destinations
  // correctly instead of mojibake. Timestamps stay ISO 8601 rather than the
  // table's display format, so a spreadsheet can sort and filter on them.
  return "﻿" + rows.map((r) => r.map(csvField).join(",")).join("\r\n") + "\r\n";
}

document.getElementById("export-csv").addEventListener("click", () => {
  const links = getVisibleLinks();
  const errorEl = document.getElementById("links-error");
  errorEl.textContent = "";
  if (!links.length) {
    errorEl.textContent = "Nothing to export — no links match the current filter.";
    return;
  }
  const blob = new Blob([linksToCsv(links)], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `links-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);

  // Give the completed action a visible payoff, per DESIGN.md's rule that a
  // real completed action never ends in silence. Names the row count, which
  // also answers "what did I just export?" — the export follows the current
  // filter, and nothing on screen said how many rows that was.
  const successEl = document.getElementById("csv-success");
  successEl.textContent = `Exported ${links.length} link${links.length === 1 ? "" : "s"}.`;
  successEl.hidden = false;
});
