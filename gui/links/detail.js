const slug = new URLSearchParams(location.search).get("slug");
let currentPrincipal = null;
// Set by loadLinkInfo(), read by applyShortUrl() on every domain change
// (docs/plans/per-link-domain-restriction.md) — [] means unrestricted.
let currentAllowedDomains = [];

// Hides every content article and leaves one error line plus a way out.
//
// Without this the page rendered a confident skeleton of nothing for a link
// that doesn't exist: every field label with a blank value, a QR <img> whose
// src 404s (so the browser drew its broken-image icon), two live download
// buttons that would also 404, "Total clicks: 0", and both tables showing
// headers with no rows — because the empty-state rows only render inside
// loadAnalytics()'s success path. These are the states reached from a
// bookmark, a truncated Slack paste, or a link a colleague deleted, i.e. by
// people who are already confused. Looking like it worked is the worst
// available answer. (Impeccable critique, 2026-08-08.)
function showOnlyError(message) {
  for (const id of ["link-info", "qr-section", "analytics-section"]) {
    const el = document.getElementById(id);
    if (el) el.hidden = true;
  }
  const errorEl = document.getElementById("page-error");
  errorEl.textContent = message;
  document.getElementById("detail-dead-end-exit").hidden = false;
}

async function loadLinkInfo() {
  const { ok, data } = await api.get(`/links/${slug}`);
  if (!ok) {
    // A page-local override: app.js's shared not_found copy says "try
    // refreshing the page", which is true where a stale list is the likely
    // cause and actively misleading here — refreshing a deleted link can
    // never help.
    showOnlyError(friendlyError(data, "Could not load this link.", {
      not_found: "That short link doesn't exist — it may have been deleted.",
    }));
    return false;
  }

  document.getElementById("target-url").textContent = data.target_url;
  const statusEl = document.getElementById("status");
  // Resolvability, not the stored `status` field — same correction as the
  // dashboard's status badge; see resolveLinkState in app.js.
  const state = resolveLinkState(data);
  statusEl.textContent = STATE_LABELS[state] || state;
  statusEl.classList.add(`status-${state}`);
  document.getElementById("custom-slug-status").textContent = data.custom ? "Yes" : "No";
  const tagList = data.tags ?? [];
  document.getElementById("tags").innerHTML = tagList.length
    ? tagList.map((t) => `<span class="tag-chip">#${escapeHtml(t)}</span>`).join("")
    : "—";
  document.getElementById("password-status").textContent = data.password_protected ? "Password-protected" : "None";
  document.getElementById("start-at").textContent = formatTimestamp(data.start_at);
  document.getElementById("end-at").textContent = formatTimestamp(data.end_at);

  currentAllowedDomains = data.allowed_domains ?? [];
  const worksOnLine = document.getElementById("works-on-line");
  if (currentAllowedDomains.length) {
    document.getElementById("works-on-hosts").textContent = currentAllowedDomains.map(hostOf).join(", ");
    worksOnLine.hidden = false;
  } else {
    worksOnLine.hidden = true;
  }

  // Mirrors the server's own links._can_edit — only show Edit when the
  // viewer is this link's owner, an admin, or has links.edit_all.
  const canEdit = currentPrincipal && (
    currentPrincipal.role === "admin" ||
    currentPrincipal.permissions.includes("links.edit_all") ||
    data.owner === currentPrincipal.username
  );
  document.getElementById("detail-edit-link").hidden = !canEdit;

  applyShortUrl();
  return true;
}

// Sets the heading, the Copy target and the three QR URLs from the currently
// selected domain — extracted so a later domain change (registered via
// onDomainChange below) can re-run just this part, without re-fetching the
// link or re-registering #detail-copy-btn's listener. Re-setting
// #qr-preview's src re-fetches the image, which is the point: the preview
// must show the QR for the domain currently selected.
function applyShortUrl() {
  document.getElementById("short-link-heading").textContent = shortUrlFor(slug);
  document.getElementById("detail-copy-btn").hidden = false;

  const selectedDomain = getSelectedDomain();
  const mismatchEl = document.getElementById("qr-domain-mismatch");
  const contentEl = document.getElementById("qr-content");

  // Mirrors api/domains.base_url_allowed_for_link / linkgate.HostAllowed:
  // empty/no allowed_domains means unrestricted; otherwise membership by
  // hostname (docs/plans/per-link-domain-restriction.md). Without this check
  // the <img> would just fail silently against the API's own
  // 400 base_not_allowed_for_link refusal.
  const allowed = !currentAllowedDomains.length || currentAllowedDomains.some(
    (d) => hostOf(d) === hostOf(selectedDomain)
  );

  if (!allowed) {
    mismatchEl.textContent = `This link does not work on ${hostOf(selectedDomain)}. Switch domains in the header to see its QR code.`;
    mismatchEl.hidden = false;
    contentEl.hidden = true;
    return;
  }
  mismatchEl.hidden = true;
  contentEl.hidden = false;

  const base = encodeURIComponent(selectedDomain);
  document.getElementById("qr-preview").src = `/api/links/${slug}/qr?format=png&size=web&base=${base}`;
  document.getElementById("qr-svg-download").href = `/api/links/${slug}/qr?format=svg&size=print&download=1&base=${base}`;
  document.getElementById("qr-png-download").href = `/api/links/${slug}/qr?format=png&size=print&download=1&base=${base}`;
}

async function loadAnalytics() {
  const { ok, data } = await api.get(`/links/${slug}/analytics`);
  if (!ok) {
    document.getElementById("page-error").textContent = friendlyError(data, "Could not load analytics for this link.");
    return;
  }

  document.getElementById("total-clicks").textContent = data.total;

  const daysBody = document.getElementById("days-body");
  daysBody.innerHTML = "";
  const sortedDays = Object.entries(data.days).sort((a, b) => b[0].localeCompare(a[0]));
  if (!sortedDays.length) {
    daysBody.innerHTML = `<tr><td colspan="2" class="empty-state">No clicks yet.</td></tr>`;
  }
  for (const [day, count] of sortedDays) {
    const row = document.createElement("tr");
    row.innerHTML = `<td>${escapeHtml(formatTimestamp(day))}</td><td>${escapeHtml(String(count))}</td>`;
    daysBody.appendChild(row);
  }
}

// initHeader() must resolve before loadLinkInfo() runs — loadLinkInfo()
// reads currentPrincipal (set below) to decide whether to show Edit.
initHeader({ dashboardHref: "../dashboard.html", pageLabel: "Link details", adminHref: "../admin/index.html" }).then((result) => {
  if (result.ok) currentPrincipal = result.data;

  if (!slug) {
    // Previously `document.body.textContent = "No link specified."`, which
    // destroyed the nav, the stylesheet association and every route out —
    // leaving a bare sentence at (0,0) with no landmark, no heading and
    // nothing to navigate. Keep the shell; use the same dead-end treatment as
    // a missing link.
    showOnlyError("No short link was specified.");
  } else {
    document.getElementById("detail-edit-link").href = `../dashboard.html?edit=${encodeURIComponent(slug)}`;
    // Registered once, as today — reads shortUrlFor(slug) at click time
    // rather than capturing a URL, so applyShortUrl() re-running on a domain
    // change never needs to (and never does) re-register this listener.
    document.getElementById("detail-copy-btn").addEventListener("click", (evt) => copyToClipboard(shortUrlFor(slug), evt.currentTarget));
    onDomainChange(applyShortUrl);
    // Gated, not fired in parallel: if the link doesn't exist, loadAnalytics
    // would overwrite the dead-end message with its own error and re-reveal
    // nothing useful. It also used to be the reason the empty-state rows
    // never rendered on a failed load — they only exist inside its success
    // path, so the tables showed bare headers instead.
    loadLinkInfo().then((found) => {
      if (found) loadAnalytics();
    });
  }
});
