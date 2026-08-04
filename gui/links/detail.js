const slug = new URLSearchParams(location.search).get("slug");
let currentPrincipal = null;

function formatDateTime(iso) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso));
}

async function loadLinkInfo() {
  const { ok, data } = await api.get(`/links/${slug}`);
  if (!ok) {
    document.getElementById("page-error").textContent = friendlyError(data, "Could not load this link.");
    return;
  }

  document.getElementById("target-url").textContent = data.target_url;
  const statusEl = document.getElementById("status");
  statusEl.textContent = data.status;
  statusEl.classList.add(`status-${data.status}`);
  document.getElementById("custom-slug-status").textContent = data.custom ? "Yes" : "No";
  const tagList = data.tags ?? [];
  document.getElementById("tags").innerHTML = tagList.length
    ? tagList.map((t) => `<span class="tag-chip">#${escapeHtml(t)}</span>`).join("")
    : "—";
  document.getElementById("password-status").textContent = data.password_protected ? "Password-protected" : "None";
  document.getElementById("start-at").textContent = data.start_at ? formatDateTime(data.start_at) : "—";
  document.getElementById("end-at").textContent = data.end_at ? formatDateTime(data.end_at) : "—";

  // Mirrors the server's own links._can_edit — only show Edit when the
  // viewer is this link's owner, an admin, or has links.edit_all.
  const canEdit = currentPrincipal && (
    currentPrincipal.role === "admin" ||
    currentPrincipal.permissions.includes("links.edit_all") ||
    data.owner === currentPrincipal.username
  );
  document.getElementById("detail-edit-link").hidden = !canEdit;

  applyShortUrl();
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

  const base = encodeURIComponent(getSelectedDomain());
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
    row.innerHTML = `<td>${escapeHtml(day)}</td><td>${escapeHtml(String(count))}</td>`;
    daysBody.appendChild(row);
  }

  const eventsBody = document.getElementById("events-body");
  eventsBody.innerHTML = "";
  if (!data.recent_events.length) {
    eventsBody.innerHTML = `<tr><td colspan="3" class="empty-state">No recent events yet.</td></tr>`;
  }
  for (const event of data.recent_events) {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>${escapeHtml(formatDateTime(event.timestamp))}</td>
      <td>${escapeHtml(event.referrer || "(direct)")}</td>
      <td>${escapeHtml(event.device_class)}</td>
    `;
    eventsBody.appendChild(row);
  }
}

// initHeader() must resolve before loadLinkInfo() runs — loadLinkInfo()
// reads currentPrincipal (set below) to decide whether to show Edit.
initHeader({ dashboardHref: "../dashboard.html", pageLabel: "Link details", manageUsersHref: "../admin/users.html" }).then((result) => {
  if (result.ok) currentPrincipal = result.data;

  if (!slug) {
    document.body.textContent = "No link specified.";
  } else {
    document.getElementById("detail-edit-link").href = `../dashboard.html?edit=${encodeURIComponent(slug)}`;
    // Registered once, as today — reads shortUrlFor(slug) at click time
    // rather than capturing a URL, so applyShortUrl() re-running on a domain
    // change never needs to (and never does) re-register this listener.
    document.getElementById("detail-copy-btn").addEventListener("click", (evt) => copyToClipboard(shortUrlFor(slug), evt.currentTarget));
    onDomainChange(applyShortUrl);
    loadLinkInfo();
    loadAnalytics();
  }
});
