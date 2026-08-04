// Maps api/urlpolicy.py's parse_policy_document error codes to friendly
// copy, following backup.js's BACKUP_ERROR_MESSAGES precedent — kept local
// because these codes matter only on this page.
const POLICY_ERROR_MESSAGES = {
  invalid_policy: "That policy document isn't valid.",
  invalid_default_action: "Choose a valid default action.",
  invalid_rule_action: "Choose a valid action for that rule.",
  invalid_rule_host: "That host isn't valid.",
  invalid_rule_note: "That note is too long.",
  too_many_rules: "That's too many rules for one policy.",
  body_too_large: "This policy is too large to save.",
};

// urlpolicy.evaluate()'s `reason` values, mapped to copy a non-technical
// operator can read. Falls back to the raw value for a reason this map
// doesn't know, following backup.js's consistencyCheckLabel precedent.
const VIOLATION_REASONS = {
  denied_by_rule: "Blocked by a deny rule",
  not_allowed_by_default: "Blocked by the default action (no matching allow rule)",
  unparsable_target_url: "The destination has no readable host",
};

function violationReasonLabel(reason) {
  return VIOLATION_REASONS[reason] || reason;
}

// The currently-loaded/staged policy. Edits (default action, add rule,
// remove rule) mutate this in memory; nothing reaches the server until
// Save is clicked, matching the whole-document PUT and letting the
// operator see the whole change before it lands.
let stagedPolicy = { version: 1, default_action: "allow", rules: [] };

function defaultActionSummary(action) {
  return action === "deny"
    ? "Only destinations matched by an explicit Allow rule (or a subdomain of one) are permitted. Everything else is blocked."
    : "Every destination is permitted unless it's matched by an explicit Block rule (or is a subdomain of one).";
}

function renderDefaultActionSummary() {
  document.getElementById("default-action-summary").textContent =
    defaultActionSummary(document.getElementById("default-action").value);
}

function renderRulesTable() {
  const body = document.getElementById("rules-table-body");
  const rules = stagedPolicy.rules || [];

  if (!rules.length) {
    body.innerHTML = `<tr id="rules-empty-row"><td colspan="5">No rules yet.</td></tr>`;
    return;
  }

  body.innerHTML = rules
    .map((rule, index) => {
      const action = rule.action === "allow" ? "Allow" : "Block";
      const note = rule.note ? escapeHtml(rule.note) : "—";
      const added = rule.created_by
        ? `${escapeHtml(rule.created_at || "")} by ${escapeHtml(rule.created_by)}`
        : escapeHtml(rule.created_at || "—");
      return `
        <tr>
          <td>${escapeHtml(rule.host)}</td>
          <td>${action}</td>
          <td>${note}</td>
          <td>${added}</td>
          <td><button type="button" class="outline secondary rule-remove" data-index="${index}">Remove</button></td>
        </tr>
      `;
    })
    .join("");
}

document.getElementById("default-action").addEventListener("change", (e) => {
  stagedPolicy.default_action = e.target.value;
  renderDefaultActionSummary();
});

document.getElementById("rule-add").addEventListener("click", () => {
  const errorEl = document.getElementById("policy-error");
  errorEl.textContent = "";

  const hostInput = document.getElementById("rule-host");
  const host = hostInput.value.trim();
  const action = document.getElementById("rule-action").value;
  const note = document.getElementById("rule-note").value.trim() || null;

  if (!host) {
    errorEl.textContent = "Enter a host to add a rule for it.";
    return;
  }

  // Staged client-side only — the server re-validates and normalizes every
  // host on Save (parse_policy_document), which is the actual source of
  // truth. This is just enough of a check to give immediate feedback.
  const key = `${host.toLowerCase()}:${action}`;
  if ((stagedPolicy.rules || []).some((r) => `${r.host}:${r.action}` === key)) {
    errorEl.textContent = "That host and action are already in the list.";
    return;
  }

  stagedPolicy.rules = [...(stagedPolicy.rules || []), { host, action, note, created_at: null, created_by: null }];
  renderRulesTable();

  hostInput.value = "";
  document.getElementById("rule-note").value = "";
});

document.getElementById("rules-table-body").addEventListener("click", (e) => {
  const btn = e.target.closest(".rule-remove");
  if (!btn) return;
  const index = Number(btn.dataset.index);
  stagedPolicy.rules = (stagedPolicy.rules || []).filter((_, i) => i !== index);
  renderRulesTable();
});

document.getElementById("policy-save").addEventListener("click", async () => {
  const errorEl = document.getElementById("policy-error");
  const successEl = document.getElementById("policy-success");
  errorEl.textContent = "";
  successEl.hidden = true;

  if (stagedPolicy.default_action === "deny") {
    const allowCount = (stagedPolicy.rules || []).filter((r) => r.action === "allow").length;
    const confirmed = await confirmDialog(
      `Setting the default to Block means every destination is refused unless it matches one of ${allowCount} allow rule${allowCount === 1 ? "" : "s"}. Save anyway?`,
      { confirmLabel: "Save" },
    );
    if (!confirmed) return;
  }

  const { ok, data } = await api.put("/admin/url-policy", {
    default_action: stagedPolicy.default_action,
    rules: stagedPolicy.rules || [],
  });
  if (!ok) {
    const msg = friendlyError(data, "Could not save the policy.", POLICY_ERROR_MESSAGES);
    errorEl.textContent = data && data.host ? `${msg} (${data.host})` : msg;
    return;
  }

  stagedPolicy = data;
  renderRulesTable();
  successEl.textContent = "Policy saved.";
  successEl.hidden = false;
});

function renderViolations(report) {
  const resultEl = document.getElementById("violations-result");

  if (report.count === 0) {
    resultEl.innerHTML = `<p class="form-success">No violations — ${report.scanned.links} link${report.scanned.links === 1 ? "" : "s"} checked.</p>`;
    return;
  }

  const rows = report.violations
    .map((v) => `
      <tr>
        <td><a href="../links/detail.html?slug=${encodeURIComponent(v.slug)}">${escapeHtml(v.slug)}</a></td>
        <td>${escapeHtml(v.owner || "—")}</td>
        <td>${escapeHtml(v.status || "—")}</td>
        <td>${escapeHtml(v.host || "—")}</td>
        <td>${escapeHtml(violationReasonLabel(v.reason))}</td>
      </tr>
    `)
    .join("");

  const truncatedHtml = report.truncated
    ? `<p>Showing the first ${report.max_violations} of ${report.count}.</p>`
    : "";

  resultEl.innerHTML = `
    <table>
      <thead><tr><th>Slug</th><th>Owner</th><th>Status</th><th>Host</th><th>Reason</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    ${truncatedHtml}
  `;
}

document.getElementById("violations-btn").addEventListener("click", async () => {
  const errorEl = document.getElementById("violations-error");
  const resultEl = document.getElementById("violations-result");
  errorEl.textContent = "";
  resultEl.innerHTML = "";

  const { ok, data } = await api.get("/admin/url-policy/violations");
  if (!ok) {
    errorEl.textContent = friendlyError(data, "Could not run the check.");
    return;
  }

  renderViolations(data);
});

initHeader({
  dashboardHref: "../dashboard.html",
  // Deliberately shorter than the page's own <h1>. "Destination URL policy"
  // overflowed #app-header nav at 390px (scrollWidth 374 vs clientWidth 351,
  // both themes; backup.html measured 351/351 at the same width, so the label
  // length was the whole difference). Safe to shorten here because this page
  // has a real <h1> carrying the full name — DESIGN.md's rule about keeping
  // the breadcrumb at narrow widths exists for links/detail.html, which has
  // no <h1> at all and would lose its only page identity.
  pageLabel: "URL policy",
  manageUsersHref: "users.html",
}).then(async (result) => {
  // Both /api/admin/url-policy and /api/admin/url-policy/violations gate on
  // users.manage (auth.py's Principal.has_permission — true for role ==
  // "admin" too), mirroring backup.js's own canManage check rather than
  // round-tripping to the server just to discover the same answer.
  const canManage = result.ok && (result.data.role === "admin" || result.data.permissions.includes("users.manage"));
  if (!canManage) {
    document.getElementById("forbidden-notice").hidden = false;
    document.getElementById("admin-content").style.display = "none";
    return;
  }

  const { ok, data } = await api.get("/admin/url-policy");
  if (ok) {
    stagedPolicy = data;
  }
  document.getElementById("default-action").value = stagedPolicy.default_action;
  renderDefaultActionSummary();
  renderRulesTable();
});
