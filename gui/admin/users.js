const ALL_PERMISSIONS = ["links.create_custom_slug", "links.view_all", "links.edit_all", "links.tag", "users.manage"];
const PERMISSION_LABELS = {
  "links.create_custom_slug": "Create custom slugs",
  "links.view_all": "View all links",
  "links.edit_all": "Edit all links",
  "links.tag": "Tag links in bulk",
  "users.manage": "Manage users",
};

function permissionsLegendText(role) {
  return role === "admin" ? "Permissions (ignored — admins have full access)" : 'Permissions (for role "user")';
}

const DOMAINS_LEGEND_TEXT = "Short-link domains (none checked = all domains)";

// The full base URL is unpoisonable value; the host is all that's shown so
// the checkbox list stays scannable next to the permissions column.
function domainHost(domain) {
  try {
    return new URL(domain).host;
  } catch {
    return domain;
  }
}

// assigned_domains is not a permission (it's not consulted by
// Principal.has_permission at all), so unlike the permissions fieldset it is
// never disabled for admins — an admin still needs a domain list to drive
// their own selector.
function domainCheckboxesHtml(className, configured, selected) {
  const known = configured.map((domain) => `
    <label>
      <input type="checkbox" class="${className}" value="${escapeHtml(domain)}" ${selected.includes(domain) ? "checked" : ""} />
      ${escapeHtml(domainHost(domain))}
    </label>
  `).join("");
  // A domain the user was previously assigned that has since been removed
  // from public_base_urls: rendered checked-and-disabled so the operator can
  // see it, and :not(:disabled) on the submit selector drops it on next save
  // instead of round-tripping a now-invalid value back into
  // _validate_assigned_domains.
  const stale = selected.filter((domain) => !configured.includes(domain)).map((domain) => `
    <label>
      <input type="checkbox" class="${className}" value="${escapeHtml(domain)}" checked disabled />
      ${escapeHtml(domainHost(domain))} — no longer configured
    </label>
  `).join("");
  return known + stale;
}

// Admins bypass every permission check (see auth.Principal.has_permission),
// so the checkbox values are inert for them; disabling the fieldset makes
// that visible instead of implying the checked boxes still matter.
function updatePermissionsFieldset(fieldset, legend, role) {
  fieldset.disabled = role === "admin";
  legend.textContent = permissionsLegendText(role);
}

function renderNewDomainsFieldset() {
  const fieldset = document.getElementById("new-domains-fieldset");
  fieldset.hidden = allDomains.length < 2;
  fieldset.innerHTML = `
    <legend>${escapeHtml(DOMAINS_LEGEND_TEXT)}</legend>
    ${domainCheckboxesHtml("new-domain", allDomains, [])}
  `;
}

// One source for both permission fieldsets. The create form used to hardcode
// its checkboxes in users.html while the edit form generated them from
// ALL_PERMISSIONS — so the two drifted, and links.tag shipped grantable by
// editing a user but not by creating one. Same shape as domainCheckboxesHtml
// above, for the same reason.
function permissionCheckboxesHtml(className, selected) {
  return ALL_PERMISSIONS.map((perm) => `
    <label>
      <input type="checkbox" class="${className}" value="${escapeHtml(perm)}" ${selected.includes(perm) ? "checked" : ""} />
      ${escapeHtml(PERMISSION_LABELS[perm] || perm)}
    </label>
  `).join("");
}

// Appends rather than replacing innerHTML: the <legend> is a long-lived node
// captured once at the bottom of this file and handed to
// updatePermissionsFieldset on every role change, so regenerating the
// fieldset wholesale would leave that reference pointing at a detached node.
function renderNewPermissionsFieldset() {
  document
    .getElementById("new-permissions-fieldset")
    .insertAdjacentHTML("beforeend", permissionCheckboxesHtml("new-permission", []));
}

function editRowHtml(user) {
  const checkboxes = permissionCheckboxesHtml("edit-permission", user.permissions);

  return `
    <tr class="edit-row" data-username="${escapeHtml(user.username)}">
      <td colspan="5">
        <form class="edit-form" data-original-role="${escapeHtml(user.role)}">
          <div class="grid">
            <label>Role
              <select class="edit-role">
                <option value="user" ${user.role === "user" ? "selected" : ""}>user</option>
                <option value="admin" ${user.role === "admin" ? "selected" : ""}>admin</option>
              </select>
            </label>
            <label>New password (optional)
              <input type="password" class="edit-password" minlength="8" placeholder="Leave blank to keep current" />
            </label>
          </div>
          <fieldset class="edit-permissions-fieldset" ${user.role === "admin" ? "disabled" : ""}>
            <legend class="edit-permissions-legend">${escapeHtml(permissionsLegendText(user.role))}</legend>
            ${checkboxes}
          </fieldset>
          <fieldset class="edit-domains-fieldset" ${allDomains.length < 2 ? "hidden" : ""}>
            <legend>${escapeHtml(DOMAINS_LEGEND_TEXT)}</legend>
            ${domainCheckboxesHtml("edit-domain", allDomains, user.assigned_domains || [])}
          </fieldset>
          <label>
            <input type="checkbox" class="edit-disabled" ${user.disabled ? "checked" : ""} /> Disabled
          </label>
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

let currentPrincipal = null;
let allDomains = [];

// Same disjunction links.can_view uses server-side — gates the "Show these
// links on the dashboard" link so a users.manage-only operator (who would
// see an empty dashboard) gets a sentence naming the permission they need
// instead of a dead link.
function canViewAllLinks() {
  return !!currentPrincipal && (
    currentPrincipal.role === "admin" ||
    currentPrincipal.permissions.includes("links.view_all") ||
    currentPrincipal.permissions.includes("links.edit_all")
  );
}

async function loadUsers() {
  const { ok, data } = await api.get("/users");
  if (!ok) {
    document.getElementById("forbidden-notice").hidden = false;
    document.getElementById("admin-content").style.display = "none";
    return;
  }

  document.getElementById("users-error").textContent = "";
  allDomains = data.all_domains || [];
  renderNewDomainsFieldset();

  const body = document.getElementById("users-body");
  body.innerHTML = "";

  // A restored account carries no usable password_hash by design (see
  // docs/plans/kv-backup-restore.md) and can never authenticate
  // (api/auth.py's LocalAuthProvider.authenticate). This notice turns
  // "the data was restored" into "here is the work left to do" — the
  // per-row badge below identifies which accounts, this identifies how many.
  const noPasswordCount = data.users.filter((user) => user.password_set === false).length;
  const noticeEl = document.getElementById("password-reset-notice");
  if (noPasswordCount > 0) {
    noticeEl.textContent = noPasswordCount === 1
      ? `1 account has no password and can't sign in — set one with Edit.`
      : `${noPasswordCount} accounts have no password and can't sign in — set one with Edit.`;
    noticeEl.hidden = false;
  } else {
    noticeEl.hidden = true;
  }

  if (!data.users.length) {
    body.innerHTML = `<tr><td colspan="5" class="empty-state">No users yet.</td></tr>`;
    return;
  }
  for (const user of data.users) {
    const row = document.createElement("tr");
    row.dataset.username = user.username;
    row.innerHTML = `
      <td>${escapeHtml(user.username)}</td>
      <td>${escapeHtml(user.role)}</td>
      <td>${user.permissions.length ? escapeHtml(user.permissions.map((p) => PERMISSION_LABELS[p] || p).join(", ")) : "—"}</td>
      <td>
        <span class="status-badge status-${user.disabled ? "disabled" : "active"}">${user.disabled ? "disabled" : "active"}</span>
        ${user.password_set === false ? `<span class="status-badge status-disabled">no password</span>` : ""}
      </td>
      <td>
        <div role="group">
          <button class="edit-btn outline" data-username="${escapeHtml(user.username)}" aria-label="Edit user ${escapeHtml(user.username)}">Edit</button>
          ${currentPrincipal && user.username === currentPrincipal.username ? "" : `
            <button class="delete-btn secondary outline" data-username="${escapeHtml(user.username)}" aria-label="Delete user ${escapeHtml(user.username)}">Delete</button>
          `}
        </div>
      </td>
    `;
    body.appendChild(row);
    row.insertAdjacentHTML("afterend", editRowHtml(user));
    body.querySelector(`tr.edit-row[data-username="${CSS.escape(user.username)}"]`).style.display = "none";
  }

  body.querySelectorAll(".edit-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const editRow = body.querySelector(`tr.edit-row[data-username="${CSS.escape(btn.dataset.username)}"]`);
      const opening = editRow.style.display === "none";
      editRow.style.display = opening ? "" : "none";
      // Without this, opening the edit form on a row near/below the fold
      // produces a click that visually does nothing.
      if (opening) editRow.scrollIntoView({ block: "center" });
    });
  });

  body.querySelectorAll(".edit-role").forEach((select) => {
    const editRow = select.closest("tr.edit-row");
    const fieldset = editRow.querySelector(".edit-permissions-fieldset");
    const legend = editRow.querySelector(".edit-permissions-legend");
    select.addEventListener("change", () => {
      updatePermissionsFieldset(fieldset, legend, select.value);
    });
  });

  body.querySelectorAll(".cancel-edit-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      btn.closest("tr.edit-row").style.display = "none";
    });
  });

  body.querySelectorAll(".delete-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const username = btn.dataset.username;
      const actionEl = document.getElementById("users-error-action");
      actionEl.hidden = true;
      if (!await confirmDialog(`Delete the user "${username}"? This can't be undone.`)) return;
      const { ok, data } = await api.delete(`/users/${encodeURIComponent(username)}`);
      if (!ok) {
        const count = (data && typeof data.link_count === "number") ? data.link_count : 0;
        document.getElementById("users-error").textContent = friendlyError(data, "Could not delete user.", {
          user_owns_links:
            `"${username}" still owns ${count} link${count === 1 ? "" : "s"}. `
            + `Reassign or delete them first, then delete the account.`
            + (canViewAllLinks() ? "" : ` You'll need the "View all links" permission to do that.`),
        });
        if (data && data.error === "user_owns_links" && canViewAllLinks()) {
          document.getElementById("show-owner-links").href = `../dashboard.html?owner=${encodeURIComponent(username)}`;
          actionEl.hidden = false;
        }
        return;
      }
      // Mirrors dashboard.html's fix for the same class of bug: a
      // create-success banner referencing a now-deleted username should
      // not linger.
      document.getElementById("create-success").hidden = true;
      loadUsers();
    });
  });

  body.querySelectorAll(".edit-form").forEach((form) => {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const editRow = form.closest("tr.edit-row");
      const username = editRow.dataset.username;
      const errorEl = form.querySelector(".edit-error");
      errorEl.textContent = "";

      const newRole = form.querySelector(".edit-role").value;
      // Promoting to admin grants full, unconditional access (admins
      // bypass every permission check) — the single most consequential
      // action on this page, so it gets its own confirmation instead of
      // riding through on a plain Save like every other field here.
      if (newRole === "admin" && form.dataset.originalRole !== "admin") {
        if (!await confirmDialog(`Make "${username}" an admin? Admins bypass every permission check.`, { confirmLabel: "Make admin" })) {
          return;
        }
      }

      const payload = {
        role: newRole,
        permissions: Array.from(form.querySelectorAll(".edit-permission:checked")).map((cb) => cb.value),
        assigned_domains: Array.from(form.querySelectorAll(".edit-domain:checked:not(:disabled)")).map((cb) => cb.value),
        disabled: form.querySelector(".edit-disabled").checked,
      };
      const password = form.querySelector(".edit-password").value;
      if (password) payload.password = password;

      const { ok, data } = await api.patch(`/users/${encodeURIComponent(username)}`, payload);
      if (!ok) {
        errorEl.textContent = friendlyError(data, "Could not update user.");
        return;
      }
      loadUsers();
    });
  });
}

document.getElementById("create-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const errorEl = document.getElementById("create-error");
  const successEl = document.getElementById("create-success");
  errorEl.textContent = "";
  successEl.hidden = true;

  const username = document.getElementById("new-username").value;
  const payload = {
    username,
    password: document.getElementById("new-password").value,
    role: document.getElementById("new-role").value,
    permissions: Array.from(document.querySelectorAll(".new-permission:checked")).map((cb) => cb.value),
    assigned_domains: Array.from(document.querySelectorAll(".new-domain:checked:not(:disabled)")).map((cb) => cb.value),
  };

  const { ok, data } = await api.post("/users", payload);
  if (!ok) {
    errorEl.textContent = friendlyError(data, "Could not create user.");
    return;
  }
  document.getElementById("create-form").reset();
  updatePermissionsFieldset(newPermissionsFieldset, newPermissionsLegend, newRoleSelect.value);
  // Mirrors dashboard.html's link-creation payoff (see DESIGN.md's
  // "give a visible payoff" rule) — a silent form-clear + table reload
  // left no confirmation the user was actually created.
  successEl.textContent = `User "${username}" created.`;
  successEl.hidden = false;
  loadUsers();
});

renderNewPermissionsFieldset();

const newRoleSelect = document.getElementById("new-role");
const newPermissionsFieldset = document.getElementById("new-permissions-fieldset");
const newPermissionsLegend = document.getElementById("new-permissions-legend");
newRoleSelect.addEventListener("change", () => {
  updatePermissionsFieldset(newPermissionsFieldset, newPermissionsLegend, newRoleSelect.value);
});

// initHeader() must resolve before loadUsers()'s first render — the
// Users table reads currentPrincipal (set below) to hide Delete on the
// viewer's own row, since the server always rejects that with
// cannot_delete_self and showing the full "are you sure, permanent"
// ritual for a guaranteed-doomed action is exactly backwards.
initHeader({ dashboardHref: "../dashboard.html", pageLabel: "Manage users", adminHref: "index.html" }).then((result) => {
  renderAdminNav(document.getElementById("admin-nav"), "users");
  // Harmonised with store-maintenance.js/url-policy.js/index.js: gate from
  // initHeader()'s /auth/me result instead of waiting on a doomed GET
  // /api/users to 403. loadUsers()'s own !ok fallback below stays as
  // defense-in-depth — the server is authoritative either way, e.g. if a
  // session is revoked between this check and the request it triggers.
  const canManage = result.ok && (result.data.role === "admin" || result.data.permissions.includes("users.manage"));
  if (!canManage) {
    document.getElementById("forbidden-notice").hidden = false;
    document.getElementById("admin-content").style.display = "none";
    return;
  }
  currentPrincipal = result.data;
  loadUsers();
});
