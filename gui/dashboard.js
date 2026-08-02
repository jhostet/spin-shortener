let currentPrincipal = null;

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

function formatDateTime(iso) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso));
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

function formatWindowField(value, { warnIfSoon = false, noteIfFuture = false } = {}) {
  if (!value) return "—";
  const text = escapeHtml(formatDateTime(value));
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
let sortKey = null;
let sortDir = 1;

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
  if (count === 0) return;

  const countEl = document.getElementById("bulk-count");
  const overCap = count > BULK_MAX_SELECTION;
  countEl.textContent = overCap
    ? `${count} links selected — bulk actions apply to at most ${BULK_MAX_SELECTION} at a time.`
    : `${count} link${count === 1 ? "" : "s"} selected`;

  for (const id of ["bulk-enable-btn", "bulk-disable-btn", "bulk-delete-btn"]) {
    document.getElementById(id).disabled = overCap;
  }
}

// The header checkbox only ever reflects/affects the *selectable*
// (canEditLink) rows in the current filtered view — selecting a row nobody
// is allowed to act on would promise an action the API just refuses anyway.
function getSelectableVisibleSlugs() {
  return getVisibleLinks().filter(canEditLink).map((link) => link.slug);
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

function getVisibleLinks() {
  const term = document.getElementById("links-filter").value.trim().toLowerCase();
  let visible = !term
    ? allLinks
    : allLinks.filter(
        (link) => link.slug.toLowerCase().includes(term) || link.target_url.toLowerCase().includes(term)
      );

  if (sortKey) {
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
      <td colspan="9">
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
          <div class="grid">
            <label><input type="checkbox" class="edit-disabled" ${link.status === "disabled" ? "checked" : ""} /> Disabled (stops <code>/r/${escapeHtml(link.slug)}</code> resolving)</label>
          </div>
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

async function loadLinks() {
  const { ok, data } = await api.get("/links");
  if (!ok) return;
  allLinks = data.links;
  renderLinksTable();
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

  const visibleLinks = getVisibleLinks();
  if (!visibleLinks.length) {
    const message = allLinks.length ? "No links match your filter." : "No links yet — create one above.";
    body.innerHTML = `<tr><td colspan="9" class="empty-state">${escapeHtml(message)}</td></tr>`;
    updateSelectAllState();
    return;
  }

  for (const link of visibleLinks) {
    const shortUrl = `${location.origin}/r/${link.slug}`;
    const row = document.createElement("tr");
    row.dataset.slug = link.slug;
    row.innerHTML = `
      <td class="select-cell">
        ${canEditLink(link) ? `<input type="checkbox" class="row-select" data-slug="${escapeHtml(link.slug)}" aria-label="Select link ${escapeHtml(link.slug)}" />` : ""}
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
      </td>
      <td>${escapeHtml(link.owner)}</td>
      <td class="destination-cell" title="${escapeHtml(link.target_url)}">${escapeHtml(link.target_url)}</td>
      <td>${formatDateTime(link.created_at)}</td>
      <td><span class="status-badge status-${escapeHtml(link.status)}">${escapeHtml(link.status)}</span></td>
      <td>${formatWindowField(link.start_at, { noteIfFuture: link.status === "active" })}</td>
      <td>${formatWindowField(link.end_at, { warnIfSoon: link.status === "active" })}</td>
      <td>
        <div role="group">
          <a role="button" class="outline" aria-label="View link ${escapeHtml(link.slug)}" href="links/detail.html?slug=${encodeURIComponent(link.slug)}">View</a>
          <button data-slug="${escapeHtml(link.slug)}" class="copy-btn outline" aria-label="Copy link ${escapeHtml(link.slug)}">Copy</button>
          ${canEditLink(link) ? `
            <button data-slug="${escapeHtml(link.slug)}" class="edit-btn outline" aria-label="Edit link ${escapeHtml(link.slug)}">Edit</button>
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
  const shortUrl = `${location.origin}/r/${btn.dataset.slug}`;
  copyToClipboard(shortUrl, btn);
}

async function handleDeleteClick(btn) {
  if (!await confirmDialog(`Delete the link "${btn.dataset.slug}"? This can't be undone.`)) return;
  const errorEl = document.getElementById("links-error");
  errorEl.textContent = "";
  const { ok, data } = await api.delete(`/links/${btn.dataset.slug}`);
  if (!ok) {
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
  // PATCH /links/{slug} has accepted `status` since Phase 1, but until this
  // control existed no client ever sent it — bulk enable/disable was the only
  // way to change a link's status from the GUI, so disabling one link meant
  // selecting it and using a bar labelled for bulk work.
  const status = form.querySelector(".edit-disabled").checked ? "disabled" : "active";
  const errorEl = form.querySelector(".edit-error");
  errorEl.textContent = "";

  const { ok, data } = await api.patch(`/links/${slug}`, { target_url: targetUrl, start_at: startAt, end_at: endAt, status });
  if (!ok) {
    errorEl.textContent = friendlyError(data, "Could not update link.");
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
  }
  const displayRow = editRow.previousElementSibling;
  if (displayRow) {
    // Positional indices shifted by 1 when the select-column was inserted
    // as the new first <td> — destination was children[2], now children[3];
    // Starts/Expires were [5]/[6], now [6]/[7]. This fails silently if wrong.
    displayRow.children[3].textContent = targetUrl;
    displayRow.children[6].innerHTML = formatWindowField(startAt, { noteIfFuture: linkRecord?.status === "active" });
    displayRow.children[7].innerHTML = formatWindowField(endAt, { warnIfSoon: linkRecord?.status === "active" });
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

  loadLinks();
}

document.getElementById("links-body").addEventListener("click", (e) => {
  const btn = e.target.closest(".copy-btn, .delete-btn, .edit-btn, .cancel-edit-btn");
  if (!btn) return;
  if (btn.matches(".copy-btn")) handleCopyClick(btn);
  else if (btn.matches(".delete-btn")) handleDeleteClick(btn);
  else if (btn.matches(".edit-btn")) handleEditToggleClick(btn);
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

  clearBulkResults();
  const errorEl = document.getElementById("bulk-error");
  const errorsEl = document.getElementById("bulk-errors");
  const successEl = document.getElementById("bulk-success");

  const payload = { text, start_at: startAt, end_at: endAt };
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

  successEl.textContent = `Created ${data.count} link${data.count === 1 ? "" : "s"}.`;
  successEl.hidden = false;
  document.getElementById("bulk-text").value = "";
  document.getElementById("bulk-start-at").value = "";
  document.getElementById("bulk-end-at").value = "";
  document.getElementById("bulk-password").value = "";
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
  const errorEl = document.getElementById("create-error");
  const successEl = document.getElementById("create-success");
  errorEl.textContent = "";
  successEl.hidden = true;

  const payload = {
    target_url: targetUrl,
    custom_slug: customSlug,
    start_at: startAt,
    end_at: endAt,
  };
  if (password) payload.password = password;

  const { ok, data } = await api.post("/links", payload);
  if (!ok) {
    errorEl.textContent = friendlyError(data, "Could not create link.", {
      invalid_password: "Link passwords must be at least 4 characters.",
    });
    return;
  }
  document.getElementById("target-url").value = "";
  document.getElementById("custom-slug").value = "";
  document.getElementById("start-at").value = "";
  document.getElementById("end-at").value = "";
  document.getElementById("link-password").value = "";
  document.getElementById("advanced-options").open = false;

  const shortUrl = `${location.origin}/r/${data.slug}`;
  successEl.innerHTML = `Link created: <span class="slug-chip">${escapeHtml(shortUrl)}</span> <button type="button" class="outline">Copy</button>`;
  successEl.hidden = false;
  successEl.querySelector("button").addEventListener("click", (evt) => copyToClipboard(shortUrl, evt.currentTarget));

  loadLinks();
});

let filterDebounceTimer = null;
document.getElementById("links-filter").addEventListener("input", () => {
  clearTimeout(filterDebounceTimer);
  filterDebounceTimer = setTimeout(renderLinksTable, 200);
});

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

// loadMe() must resolve before loadLinks()'s first render — renderLinksTable()
// reads currentPrincipal (set by loadMe()) to decide whether to show each
// row's Edit/Delete controls, and running them concurrently could render
// the first paint with every row's Edit/Delete hidden, even the viewer's own.
loadMe().then(() => loadLinks().then(openDeepLinkedEditRow));

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
  ["Short link", (l) => `${location.origin}/r/${l.slug}`],
  ["Owner", (l) => l.owner],
  ["Destination", (l) => l.target_url],
  ["Created", (l) => l.created_at ?? ""],
  ["Status", (l) => l.status],
  ["Starts", (l) => l.start_at ?? ""],
  ["Expires", (l) => l.end_at ?? ""],
];

// RFC 4180: quote a field that contains a comma, quote, CR or LF, and double
// any quote inside it. Destinations really do contain commas — the bulk parser
// has a case for exactly that — so this is load-bearing, not defensive.
function csvField(value) {
  const s = String(value ?? "");
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
});
