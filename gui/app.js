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

// Maps API error codes (the JSON body's `error` field) to human-readable
// text. Codes not listed here fall back to the caller-supplied default
// rather than ever surfacing the raw machine code to a user.
const ERROR_MESSAGES = {
  invalid_credentials: "Incorrect username or password.",
  cannot_delete_self: "You can't delete your own account while logged in as it.",
  cannot_disable_self: "You can't disable your own account while logged in as it.",
};

function friendlyError(data, fallback) {
  const code = data && data.error;
  return (code && ERROR_MESSAGES[code]) || fallback;
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
