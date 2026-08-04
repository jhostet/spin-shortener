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
  put: (path, body) => apiCall(path, { method: "PUT", body: body !== undefined ? JSON.stringify(body) : undefined }),
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
  invalid_tag: "Tags can only use lowercase letters, numbers, hyphens and underscores (up to 32 characters).",
  invalid_tags: "That list of tags isn't valid.",
  too_many_tags: "That's too many tags for one link.",
  no_tags: "Enter at least one tag.",
  unknown_owner: "That user doesn't exist — pick someone from the list.",
  user_owns_links: "That user still owns links — reassign or delete them first.",
  destination_not_allowed: "That destination isn't allowed by this site's URL policy.",
};

// `overrides` lets one call site's copy win over the shared map for a code
// whose meaning isn't fixed globally — e.g. "invalid_password" means an
// 8-char minimum for user accounts but a 4-char minimum for link passwords.
function friendlyError(data, fallback, overrides) {
  const code = data && data.error;
  if (!code) return fallback;
  return (overrides && overrides[code]) || ERROR_MESSAGES[code] || fallback;
}

// Builds and wires the Auto/Light/Dark control into `container`. Shared
// rather than living inside initHeader() because login.html has no
// `#app-header` and never calls initHeader(), yet still needs somewhere to
// change the theme — a stored preference already applies there, since every
// page loads theme-init.js, but without this there is no way to set one
// before logging in.
//
// window.ssTheme comes from theme-init.js, a separate file on its own exact
// spin.toml route. If that route is ever missing or the file 404s, the page
// still renders and still themes (the CSS falls back to light) — but an
// unguarded ssTheme.get() would throw, and on the authenticated pages that
// throw happens inside async initHeader(), whose rejection no page catches,
// taking down the entire init chain rather than just the toggle. A
// silently-404ing asset is the exact failure spin.toml's own route comment
// warns about, so degrade to hiding a control that cannot work.
function renderThemeToggle(container) {
  const ssTheme = window.ssTheme;
  if (!ssTheme) {
    container.hidden = true;
    return;
  }

  container.innerHTML = `
    <div role="group" class="theme-toggle" aria-label="Color theme">
      <button type="button" class="outline secondary" data-theme-choice="system">Auto</button>
      <button type="button" class="outline secondary" data-theme-choice="light">Light</button>
      <button type="button" class="outline secondary" data-theme-choice="dark">Dark</button>
    </div>
  `;

  const buttons = Array.from(container.querySelectorAll("[data-theme-choice]"));
  // Reflects the current mode onto the three buttons — pressing one
  // un-presses the other two, so this always rewrites all three.
  const renderPressed = () => {
    const current = ssTheme.get();
    buttons.forEach((btn) => {
      btn.setAttribute("aria-pressed", String(btn.dataset.themeChoice === current));
    });
  };

  renderPressed();
  buttons.forEach((btn) => {
    btn.addEventListener("click", () => {
      ssTheme.set(btn.dataset.themeChoice);
      renderPressed();
    });
  });

  // theme-init.js repaints on a cross-tab change; the pressed state is this
  // file's half of that and would otherwise go stale — the page would flip
  // to dark while still showing Light as the selected button.
  window.addEventListener("storage", (e) => {
    if (e.key === ssTheme.KEY || e.key === null) renderPressed();
  });
}

// Domain selector — the persistent-nav counterpart to the theme control,
// following its exact precedent (decision 6 in
// docs/plans/multi-domain-display.md): a viewer preference, persisted in
// localStorage, degrading safely rather than throwing if its dependency
// (here, a populated domain list from /auth/me) is missing.
const SS_DOMAIN_KEY = "ss-domain";
// Set by initHeader() from the /auth/me response's `domains` field before any
// page can call shortUrlFor() — every authenticated page already sequences
// its first render behind initHeader() resolving.
let availableDomains = [];
const domainChangeListeners = [];

// localStorage access is wrapped in try/catch on both read and write, mirroring
// theme-init.js — Safari private mode and blocked-storage configurations throw
// on *access*, not just on write.
function readStoredDomain() {
  try {
    return localStorage.getItem(SS_DOMAIN_KEY);
  } catch {
    return null;
  }
}

function writeStoredDomain(value) {
  try {
    localStorage.setItem(SS_DOMAIN_KEY, value);
  } catch {
    // best-effort — the selector still works for the rest of this page load
  }
}

// An unrecognized stored value (e.g. an assignment that dropped a domain)
// falls through to availableDomains[0] without rewriting storage — same rule
// as theme-init.js's get() — so the choice comes back if it becomes valid
// again. `|| location.origin` is the last-resort fallback, matching today's
// pre-feature behavior, so a failed /auth/me or an empty configured list
// degrades to the current app rather than rendering "undefined/r/x".
function getSelectedDomain() {
  const stored = readStoredDomain();
  if (stored && availableDomains.includes(stored)) return stored;
  return availableDomains[0] || location.origin;
}

// Called at the moment of use, never captured in a closure at render time —
// this is what lets a Copy button keep working across a domain change
// without the caller re-registering its listener.
function shortUrlFor(slug) {
  return `${getSelectedDomain()}/r/${slug}`;
}

function onDomainChange(fn) {
  domainChangeListeners.push(fn);
}

// domainList.length < 2 hides the control entirely — a one-option selector is
// pure clutter, and this keeps the nav byte-for-byte unchanged for any
// single-domain deployment (every deployment today).
function renderDomainSelector(container, domainList) {
  availableDomains = domainList || [];
  if (availableDomains.length < 2) {
    container.hidden = true;
    return;
  }

  // option value is the exact server-supplied base URL string (compared
  // against the configured list by the QR endpoint); option text is just the
  // host, since a full https://... prefix on every option would widen an
  // already-crowded nav for no benefit. No visible label — aria-label only,
  // so this never lands inside `#app-header nav li { color: #fff }`'s
  // thrice-recorded specificity trap for a label that would gain nothing.
  const options = availableDomains
    .map((base) => {
      let host = base;
      try {
        host = new URL(base).host;
      } catch {
        // malformed entries shouldn't reach here (server-validated), but
        // fall back to the raw string rather than an empty option
      }
      return `<option value="${escapeHtml(base)}">${escapeHtml(host)}</option>`;
    })
    .join("");
  container.innerHTML = `<select class="domain-select" aria-label="Short link domain">${options}</select>`;
  container.hidden = false;

  const select = container.querySelector(".domain-select");
  select.value = getSelectedDomain();

  select.addEventListener("change", () => {
    writeStoredDomain(select.value);
    domainChangeListeners.forEach((fn) => fn());
  });

  // Cross-tab sync — the same four lines renderThemeToggle already has, for
  // the same reason: two open tabs would otherwise disagree until one
  // navigates.
  window.addEventListener("storage", (e) => {
    if (e.key === SS_DOMAIN_KEY || e.key === null) {
      select.value = getSelectedDomain();
      domainChangeListeners.forEach((fn) => fn());
    }
  });
}

// Renders the shared nav into a page's `<header id="app-header">` and wires
// the logout handler — so logout is reachable from every authenticated page,
// not just the dashboard. The brand mark is now persistent and clickable on
// every page (previously it was replaced by a "Back to dashboard" link on
// every page except the dashboard itself — the one thing that should never
// change page-to-page was the one thing that did). A page identifies itself
// via `pageLabel`, shown as a breadcrumb suffix next to the permanent brand,
// rather than by displacing it — this also gives `links/detail.html` a page
// label it previously had none at all (no `<h1>`, no brand, nothing).
// `dashboardHref`/`manageUsersHref` let each page supply paths relative to
// its own depth. `onManageUsersPage` hides the "Manage users" link when it
// would otherwise point at the page already being viewed. Returns the
// `/auth/me` result so callers can layer page-specific logic (e.g.
// showing/hiding other fields) on top of the same principal data.
async function initHeader({
  dashboardHref = "dashboard.html",
  pageLabel = null,
  manageUsersHref = "admin/users.html",
  onManageUsersPage = false,
} = {}) {
  const header = document.getElementById("app-header");
  header.innerHTML = `
    <nav>
      <ul>
        <li>
          <a href="${dashboardHref}" class="brand-link" aria-label="spin-shortener — go to dashboard">
            <svg class="brand-mark" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">
              <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
              <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
            <strong>spin-shortener</strong>
          </a>
        </li>
        ${pageLabel
          ? `<li class="nav-separator" aria-hidden="true">/</li><li class="nav-page-label">${escapeHtml(pageLabel)}</li>`
          : ""}
      </ul>
      <ul>
        <li id="whoami"></li>
        <li id="manage-users-link" hidden><a href="${manageUsersHref}">Manage users</a></li>
        <li id="domain-control"></li>
        <li id="theme-control"></li>
        <li><button id="logout-btn" class="secondary outline">Log out</button></li>
      </ul>
    </nav>
  `;

  document.getElementById("logout-btn").addEventListener("click", async () => {
    await api.post("/auth/logout");
    setCsrfToken(null);
    location.href = "/login.html";
  });

  renderThemeToggle(document.getElementById("theme-control"));

  const result = await api.get("/auth/me");
  if (result.ok) {
    const initial = result.data.username.charAt(0).toUpperCase();
    document.getElementById("whoami").innerHTML = `
      <span class="identity-chip">
        <span class="identity-avatar" aria-hidden="true">${escapeHtml(initial)}</span>
        <span class="identity-text">
          <span class="identity-name">${escapeHtml(result.data.username)}</span>
          <span class="identity-role">${escapeHtml(result.data.role)}</span>
        </span>
      </span>
    `;
    const canManageUsers = result.data.role === "admin" || result.data.permissions.includes("users.manage");
    if (canManageUsers && !onManageUsersPage) {
      document.getElementById("manage-users-link").hidden = false;
    }
    // Called here, after the /auth/me await, rather than eagerly like
    // renderThemeToggle — the domain list comes from this response.
    renderDomainSelector(document.getElementById("domain-control"), result.data.domains);
  }
  return result;
}
