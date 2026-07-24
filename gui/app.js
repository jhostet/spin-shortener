const API_BASE = "/api";

function getCsrfToken() {
  return sessionStorage.getItem("csrf_token");
}

function setCsrfToken(token) {
  if (token) {
    sessionStorage.setItem("csrf_token", token);
  } else {
    sessionStorage.removeItem("csrf_token");
  }
}

async function apiFetch(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = Object.assign({}, options.headers);

  if (["POST", "PATCH", "PUT", "DELETE"].includes(method)) {
    const csrf = getCsrfToken();
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }
  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(API_BASE + path, {
    ...options,
    headers,
    credentials: "same-origin",
  });

  if (response.status === 401 && !path.startsWith("/auth/login") && !location.pathname.endsWith("login.html")) {
    setCsrfToken(null);
    location.href = "/login.html";
  }

  return response;
}

async function apiCall(path, options) {
  const res = await apiFetch(path, options);
  let data = null;
  try {
    data = await res.json();
  } catch {
    // no body
  }
  return { ok: res.ok, status: res.status, data };
}

const api = {
  get: (path) => apiCall(path),
  post: (path, body) => apiCall(path, { method: "POST", body: body !== undefined ? JSON.stringify(body) : undefined }),
  patch: (path, body) => apiCall(path, { method: "PATCH", body: body !== undefined ? JSON.stringify(body) : undefined }),
  delete: (path) => apiCall(path, { method: "DELETE" }),
};

// Converts a <input type="datetime-local"> value (browser-local time, no
// timezone) to the UTC ISO8601 string the API expects. Returns null for a
// blank input, which means "unset" to the API.
function datetimeLocalToIso(value) {
  if (!value) return null;
  return new Date(value).toISOString();
}

// Converts a stored UTC ISO8601 string back to a value a datetime-local
// input can display (browser-local time, "YYYY-MM-DDTHH:MM"). Returns "" for
// null/unset.
function isoToDatetimeLocal(value) {
  if (!value) return "";
  const d = new Date(value);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value;
  return div.innerHTML;
}

// Shared by every page with a "Copy" affordance next to a short link
// (dashboard row, the create-flow success banner, the link-detail page).
async function copyToClipboard(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
    const original = btn.textContent;
    btn.textContent = "Copied!";
    setTimeout(() => { btn.textContent = original; }, 1500);
  } catch {
    // Clipboard API unavailable or permission denied — the link is
    // still right there in the chip for the user to select manually.
  }
}

// Replaces the native confirm() for destructive actions (link/user delete)
// with an on-brand Pico <dialog>. Resolves true/false; never rejects.
// Confirm stays outline+secondary and Cancel stays the plain default
// button — matching this app's own established convention that a
// destructive action reads through de-emphasis, not a bold "danger"
// button (see DESIGN.md's row-action Delete styling) — so the visually
// prominent button in the dialog is the safe one, not the destructive one.
let confirmDialogCount = 0;

function confirmDialog(message, { confirmLabel = "Delete", cancelLabel = "Cancel" } = {}) {
  return new Promise((resolve) => {
    const messageId = `confirm-dialog-message-${++confirmDialogCount}`;
    const dialog = document.createElement("dialog");
    dialog.className = "confirm-dialog";
    // aria-labelledby, not aria-label: native <dialog> gets no accessible
    // name from its content by default, so without this a screen reader
    // announces only "dialog" on open rather than what it's asking about.
    dialog.setAttribute("aria-labelledby", messageId);
    dialog.innerHTML = `
      <article>
        <p id="${messageId}">${escapeHtml(message)}</p>
        <footer>
          <button type="button" data-action="cancel">${escapeHtml(cancelLabel)}</button>
          <button type="button" class="outline secondary" data-action="confirm">${escapeHtml(confirmLabel)}</button>
        </footer>
      </article>
    `;
    document.body.appendChild(dialog);

    function settle(result) {
      dialog.close();
      dialog.remove();
      resolve(result);
    }

    dialog.querySelector('[data-action="cancel"]').addEventListener("click", () => settle(false));
    dialog.querySelector('[data-action="confirm"]').addEventListener("click", () => settle(true));
    // Native Esc-key dismissal fires "cancel", not "close" — treat it as a no.
    dialog.addEventListener("cancel", () => settle(false));
    // A click that lands on the <dialog> element itself (not its <article>
    // content) is a backdrop click in a native dialog — dismiss like Cancel.
    dialog.addEventListener("click", (e) => {
      if (e.target === dialog) settle(false);
    });

    dialog.showModal();
  });
}

// Maps API error codes (the JSON body's `error` field) to human-readable
// text. Codes not listed here fall back to the caller-supplied default
// rather than ever surfacing the raw machine code to a user.
const ERROR_MESSAGES = {
  invalid_credentials: "Incorrect username or password.",
  cannot_delete_self: "You can't delete your own account while logged in as it.",
  cannot_disable_self: "You can't disable your own account while logged in as it.",
  invalid_target_url: "Enter a valid destination URL (including https://).",
  invalid_custom_slug: "Custom short links can only use letters, numbers, hyphens, and underscores (3–32 characters).",
  slug_taken: "That short link is already in use — try a different one.",
  invalid_start_at: "The start date/time isn't valid.",
  invalid_end_at: "The expiration date/time isn't valid.",
  invalid_window_range: "A link can't expire before it starts.",
  not_found: "This link no longer exists — try refreshing the page.",
  forbidden: "You don't have permission to do that.",
  invalid_username: "Enter a username.",
  invalid_password: "Password must be at least 8 characters.",
  username_taken: "That username is already taken.",
  invalid_role: "Choose a valid role.",
  invalid_permissions: "One or more selected permissions aren't valid.",
};

// `overrides` lets one call site's copy win over the shared map for a code
// whose meaning isn't fixed globally — e.g. "invalid_password" means an
// 8-char minimum for user accounts but a 4-char minimum for link passwords.
function friendlyError(data, fallback, overrides) {
  const code = data && data.error;
  if (!code) return fallback;
  return (overrides && overrides[code]) || ERROR_MESSAGES[code] || fallback;
}

// Renders the shared nav (brand or back-link, whoami, conditional "Manage
// users" link, logout button) into a page's `<header id="app-header">`, and
// wires the logout handler — so logout is reachable from every authenticated
// page, not just the dashboard. `backHref`/`manageUsersHref` let each page
// supply paths relative to its own depth. Returns the `/auth/me` result so
// callers can layer page-specific logic (e.g. showing/hiding other fields)
// on top of the same principal data.
async function initHeader({ backHref, manageUsersHref = "admin/users.html" } = {}) {
  const header = document.getElementById("app-header");
  header.innerHTML = `
    <nav>
      <ul>
        ${backHref
          ? `<li><a href="${backHref}">&larr; Back to dashboard</a></li>`
          : `<li><strong>spin-shortener</strong></li>`}
      </ul>
      <ul>
        <li id="whoami"></li>
        <li id="manage-users-link" style="display: none"><a href="${manageUsersHref}">Manage users</a></li>
        <li><button id="logout-btn" class="secondary outline">Log out</button></li>
      </ul>
    </nav>
  `;

  document.getElementById("logout-btn").addEventListener("click", async () => {
    await api.post("/auth/logout");
    setCsrfToken(null);
    location.href = "/login.html";
  });

  const result = await api.get("/auth/me");
  if (result.ok) {
    document.getElementById("whoami").textContent = `${result.data.username} (${result.data.role})`;
    if (result.data.role === "admin" || result.data.permissions.includes("users.manage")) {
      document.getElementById("manage-users-link").style.display = "";
    }
  }
  return result;
}
