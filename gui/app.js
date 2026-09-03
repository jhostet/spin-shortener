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
  // Every call site interpolates this into either text-node content or a
  // quoted attribute value, and the two need different escaping: the
  // div.textContent/innerHTML round-trip below correctly escapes &, <, >
  // for both, but leaves a literal " or ' untouched, since neither is
  // special in text-node content. Inside an attribute value (data-username=
  // "${escapeHtml(...)}", the common shape across users.js/dashboard.js/
  // store-maintenance.js/url-policy.js/admin/index.js/links/detail.js), an
  // unescaped " breaks out of the attribute early and lets the remainder of
  // the string be parsed as new attributes on the same element — confirmed
  // live: a username of x" onmouseover="alert(1) added a real onmouseover
  // handler to the row's Edit button with no interaction beyond a mouse
  // passing over it. Escaping both quote characters closes that for every
  // attribute-context call site, and is a no-op (renders as a literal quote)
  // wherever the value lands in text-node content instead.
  const div = document.createElement("div");
  div.textContent = value;
  return div.innerHTML.replaceAll('"', "&quot;").replaceAll("'", "&#39;");
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
  // A stored record that won't parse. Named rather than left to the generic
  // fallback because the generic one ("something went wrong, try again")
  // is actively wrong here: retrying never helps, and the only fix is a
  // delete. Points at the tool that reports it as unreadable_value.
  link_record_unreadable:
    "This link's stored record is corrupt and can't be read. Deleting the link is the only fix — "
    + "its details can't be recovered. An admin can confirm it with the store consistency check.",
  cannot_delete_self: "You can't delete your own account while logged in as it.",
  cannot_disable_self: "You can't disable your own account while logged in as it.",
  invalid_target_url: "Enter a valid destination URL (including https://).",
  // The 4,096-byte figure is api/links.py's MAX_TARGET_URL_BYTES, restated
  // here for copy only — same reasoning as BULK_MAX_SELECTION above: the
  // server is authoritative, so a drift here only ever produces a slightly
  // stale number in this sentence, never wrong enforcement.
  target_url_too_long: "That destination URL is too long — the limit is 4,096 bytes.",
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

// `status` is a STORED field; "does this link resolve right now" is a COMPUTED
// one — the redirect component answers it as status AND [start_at, end_at).
// Rendering the stored field and labelling it the fact is how a link whose
// window opened in November came to show a green "active" badge while
// /r/<slug> returned 404 (Impeccable critique, 2026-08-08). The qualifier
// lived in the Starts cell, which dashboard.css hides below 600px, so on a
// phone it was not merely invisible — it was absent from the accessibility
// tree entirely.
//
// Mirrors redirect/linkgate.IsWithinWindow: inclusive start, exclusive end.
// A non-active stored status wins over the window, matching the server's own
// precedence (a disabled link never resolves, whatever its schedule says).
//
// Shared here rather than in dashboard.js because the dashboard table, the
// CSV export and links/detail.html must never disagree about what a link's
// state is — three call sites, one definition.
function resolveLinkState(link) {
  if (link.status !== "active") return link.status;
  const now = Date.now();
  if (link.start_at && new Date(link.start_at).getTime() > now) return "scheduled";
  if (link.end_at && new Date(link.end_at).getTime() <= now) return "expired";
  return "active";
}

const STATE_LABELS = {
  active: "Active",
  scheduled: "Scheduled",
  expired: "Expired",
  disabled: "Disabled",
};

// Introduces no new design token: `scheduled` reuses .not-yet-live's slate
// (informational, not an alarm) and `expired` reuses .expired's danger red,
// so the badge and the Starts/Expires cell agree by construction. The label
// text carries the distinction on its own, so color is reinforcement rather
// than the only signal.
function statusBadge(link) {
  const state = resolveLinkState(link);
  return `<span class="status-badge status-${escapeHtml(state)}">${escapeHtml(STATE_LABELS[state] || state)}</span>`;
}

// The app's one genuinely product-specific component, shared so pages added
// later can reach for it instead of falling back to a plain <a>. A critique
// found the URL-policy violations table rendering slugs as bare sans-serif
// prose while this existed three files away — the signature element absent
// from the one new page that lists links.
//
// `linked` is opt-in rather than the default: the dashboard renders the chip
// inside a row that already has its own View action, and nesting a link there
// would give the row two competing targets.
function slugChip(slug, { linked = false, title = null } = {}) {
  const label = `${redirectPathPrefix()}/${escapeHtml(slug)}`;
  const attrs = title ? ` title="${escapeHtml(title)}"` : "";
  const chip = `<span class="slug-chip"${attrs}>${label}</span>`;
  return linked
    ? `<a class="slug-chip-link" href="/links/detail.html?slug=${encodeURIComponent(slug)}">${chip}</a>`
    : chip;
}

// One formatter for every timestamp in the app. Three formats were live
// simultaneously before this — "Aug 8, 2026, 1:07 AM" on the dashboard, a
// bare "2026-08-07" in the analytics days table, and a raw ISO
// "2026-08-08T05:07:13Z by admin" in the URL-policy Added column — so the
// same instant read three ways depending on which page you were on.
//
// `dateOnly` exists because a per-day bucket genuinely has no time component;
// rendering one would invent precision the data does not have.
//
// A bare "YYYY-MM-DD" is built from its own parts rather than handed to
// `new Date(string)`, which parses a date-only string as UTC midnight — so
// west of Greenwich every analytics day bucket would have rendered as the
// PREVIOUS day. The server buckets clicks by a calendar date, not an instant;
// shifting it into the viewer's timezone would be inventing a fact.
const DATE_ONLY = /^(\d{4})-(\d{2})-(\d{2})$/;

function formatTimestamp(value, { dateOnly = false } = {}) {
  if (!value) return "—";
  const parts = DATE_ONLY.exec(String(value));
  const date = parts
    ? new Date(Number(parts[1]), Number(parts[2]) - 1, Number(parts[3]))
    : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  if (dateOnly || parts) {
    return new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(date);
  }
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
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
// The admin area's page list, in nav order. Every consumer of this lives in
// gui/admin/, so the hrefs are deliberately sibling-relative with no depth
// prefix — renderAdminNav() and the hub page are the only callers, and both
// are served from /admin/. A fourth admin tool is added here and nowhere else.
const ADMIN_PAGES = [
  {
    id: "users",
    label: "Manage users",
    href: "users.html",
    blurb: "Create accounts, set roles, permissions and short-link domains, reset a password, or remove someone who has left.",
  },
  {
    id: "store-maintenance",
    label: "Store maintenance",
    href: "store-maintenance.html",
    blurb: "Download or restore a backup, check the store for inconsistencies and repair the safe ones, and clean up analytics left behind by deleted links.",
  },
  {
    id: "url-policy",
    label: "Destination URL policy",
    href: "url-policy.html",
    blurb: "Choose which hosts a short link may point at, and review existing links a new rule would refuse.",
  },
];

// Renders the sibling-tool strip on an admin page: every ADMIN_PAGES entry
// EXCEPT the one being viewed, so it is always exactly two links today and can
// never re-create the "link to the page you are already on" confusion the nav's
// own Manage-users link was fixed for. The way back to the hub is the nav's
// "Admin" item, which is why "Admin home" is deliberately not repeated here.
// Anchors carry .operator-link for the sitewide 44px tap-target floor (see
// theme.css) — a plain body anchor is not covered by it.
function renderAdminNav(container, currentId) {
  if (!container) return;
  const links = ADMIN_PAGES
    .filter((page) => page.id !== currentId)
    .map((page) => `<a class="operator-link" href="${escapeHtml(page.href)}">${escapeHtml(page.label)}</a>`)
    .join(" · ");
  container.innerHTML = `More admin tools: ${links}`;
}

const SS_DOMAIN_KEY = "ss-domain";
// Set by initHeader() from the /auth/me response's `domains` field before any
// page can call shortUrlFor() — every authenticated page already sequences
// its first render behind initHeader() resolving.
let availableDomains = [];
const domainChangeListeners = [];

// Display/encoding only. The app always serves /r/{slug}; this decides whether
// a URL we *show, copy, export or encode* includes that segment — false when an
// edge property rewrites /{slug} -> /r/{slug} in front of the app.
// Set by initHeader() from /auth/me, before any page can build a URL or a chip.
// Defaults to true and stays true if /auth/me fails or omits the field (an
// older API build), matching getSelectedDomain()'s degrade-to-today's-behavior
// rule — a wrong `true` shows a longer URL that still works, a wrong `false`
// shows one that does not.
let includeRedirectPrefix = true;

// The only place `/r` is spelled in URL or label construction anywhere in
// gui/. A hoisted function declaration, so slugChip (defined earlier in this
// file) may call it even though it's defined here.
function redirectPathPrefix() {
  return includeRedirectPrefix ? "/r" : "";
}

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
  return `${getSelectedDomain()}${redirectPathPrefix()}/${slug}`;
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
// `dashboardHref`/`adminHref` let each page supply paths relative to its own
// depth. `adminHref` points at the admin hub (gui/admin/index.html), not at
// any one tool page — the nav's admin item names the admin *area*, not a
// page in it. `onAdminHome` hides the "Admin" link when it would otherwise
// point at the page already being viewed (i.e. on the hub itself). Returns
// the `/auth/me` result so callers can layer page-specific logic (e.g.
// showing/hiding other fields) on top of the same principal data.
async function initHeader({
  dashboardHref = "dashboard.html",
  pageLabel = null,
  adminHref = "admin/index.html",
  onAdminHome = false,
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
        <li id="admin-link" hidden><a href="${adminHref}">Admin</a></li>
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
    if (canManageUsers && !onAdminHome) {
      document.getElementById("admin-link").hidden = false;
    }
    // `!== false` rather than `=== true`: an absent field (an older api build,
    // or a response shape change) must mean "include", the exact mirror of the
    // server's `!= "false"` parse in domains.parse_include_redirect_prefix.
    includeRedirectPrefix = result.data.include_redirect_prefix !== false;
    // Called here, after the /auth/me await, rather than eagerly like
    // renderThemeToggle — the domain list comes from this response.
    renderDomainSelector(document.getElementById("domain-control"), result.data.domains);
  }
  return result;
}
