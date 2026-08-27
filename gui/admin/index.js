initHeader({
  dashboardHref: "../dashboard.html",
  pageLabel: "Admin",
  adminHref: "index.html",
  onAdminHome: true,
}).then((result) => {
  // Every tool linked from this page gates on users.manage server-side
  // (auth.py's Principal.has_permission — true for role == "admin" too),
  // mirroring store-maintenance.js's own canManage check rather than
  // round-tripping to the server just to discover the same answer.
  const canManage = result.ok && (result.data.role === "admin" || result.data.permissions.includes("users.manage"));
  if (!canManage) {
    document.getElementById("forbidden-notice").hidden = false;
    document.getElementById("admin-content").style.display = "none";
    return;
  }

  document.getElementById("admin-cards").innerHTML = ADMIN_PAGES
    .map((page) => `
      <article>
        <h2><a class="operator-link" href="${escapeHtml(page.href)}">${escapeHtml(page.label)}</a></h2>
        <p>${escapeHtml(page.blurb)}</p>
      </article>
    `)
    .join("");
});
