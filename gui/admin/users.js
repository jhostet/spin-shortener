const ALL_PERMISSIONS = ["links.create_custom_slug", "links.view_all", "links.edit_all", "users.manage"];
const PERMISSION_LABELS = {
  "links.create_custom_slug": "Create custom slugs",
  "links.view_all": "View all links",
  "links.edit_all": "Edit all links",
  "users.manage": "Manage users",
};

function permissionsLegendText(role) {
  return role === "admin" ? "Permissions (ignored — admins have full access)" : 'Permissions (for role "user")';
}

// Admins bypass every permission check (see auth.Principal.has_permission),
// so the checkbox values are inert for them; disabling the fieldset makes
// that visible instead of implying the checked boxes still matter.
function updatePermissionsFieldset(fieldset, legend, role) {
  fieldset.disabled = role === "admin";
  legend.textContent = permissionsLegendText(role);
}

function editRowHtml(user) {
  const checkboxes = ALL_PERMISSIONS.map((perm) => `
    <label>
      <input type="checkbox" class="edit-permission" value="${escapeHtml(perm)}" ${user.permissions.includes(perm) ? "checked" : ""} />
      ${escapeHtml(PERMISSION_LABELS[perm] || perm)}
    </label>
  `).join("");

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

async function loadUsers() {
  const { ok, data } = await api.get("/users");
  if (!ok) {
    document.getElementById("forbidden-notice").hidden = false;
    document.getElementById("admin-content").style.display = "none";
    return;
  }

  document.getElementById("users-error").textContent = "";

  const body = document.getElementById("users-body");
  body.innerHTML = "";
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
      <td><span class="status-badge status-${user.disabled ? "disabled" : "active"}">${user.disabled ? "disabled" : "active"}</span></td>
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
      if (!await confirmDialog(`Delete the user "${btn.dataset.username}"? This can't be undone.`)) return;
      const { ok, data } = await api.delete(`/users/${encodeURIComponent(btn.dataset.username)}`);
      if (!ok) {
        document.getElementById("users-error").textContent = friendlyError(data, "Could not delete user.");
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
initHeader({ dashboardHref: "../dashboard.html", pageLabel: "Manage users", manageUsersHref: "users.html", onManageUsersPage: true }).then((result) => {
  if (result.ok) currentPrincipal = result.data;
  loadUsers();
});
