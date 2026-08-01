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
      <td colspan="8">
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

  const visibleLinks = getVisibleLinks();
  if (!visibleLinks.length) {
    const message = allLinks.length ? "No links match your filter." : "No links yet — create one above.";
    body.innerHTML = `<tr><td colspan="8" class="empty-state">${escapeHtml(message)}</td></tr>`;
    return;
  }

  for (const link of visibleLinks) {
    const shortUrl = `${location.origin}/r/${link.slug}`;
    const row = document.createElement("tr");
    row.dataset.slug = link.slug;
    row.innerHTML = `
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
  const errorEl = form.querySelector(".edit-error");
  errorEl.textContent = "";

  const { ok, data } = await api.patch(`/links/${slug}`, { target_url: targetUrl, start_at: startAt, end_at: endAt });
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
    displayRow.children[2].textContent = targetUrl;
    displayRow.children[5].innerHTML = formatWindowField(startAt, { noteIfFuture: linkRecord?.status === "active" });
    displayRow.children[6].innerHTML = formatWindowField(endAt, { warnIfSoon: linkRecord?.status === "active" });
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

// loadMe() must resolve before loadLinks()'s first render — renderLinksTable()
// reads currentPrincipal (set by loadMe()) to decide whether to show each
// row's Edit/Delete controls, and running them concurrently could render
// the first paint with every row's Edit/Delete hidden, even the viewer's own.
loadMe().then(() => loadLinks().then(openDeepLinkedEditRow));
