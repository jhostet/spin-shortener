// Maps api/backup.py's error codes to friendly copy, following
// dashboard.js's BULK_ROW_MESSAGES precedent — kept local because these
// codes matter only on this page, not across the whole app's ERROR_MESSAGES.
const BACKUP_ERROR_MESSAGES = {
  invalid_backup: "That file isn't a recognized backup.",
  invalid_backup_format: "That file isn't a spin-shortener backup.",
  unsupported_schema_version: "This backup was made with a newer, unsupported format version.",
  no_stores: "Select at least one store.",
  unknown_store: "The backup file references an unknown store.",
  invalid_entries: "The backup file's contents are malformed.",
  too_many_entries: "This backup has too many entries to restore in one request.",
  invalid_value_encoding: "The backup file contains a corrupted value.",
  forbidden_key: "The backup file contains a session or bootstrap key, which isn't allowed.",
  credential_material_in_backup: "The backup file contains a password hash, which isn't allowed.",
  body_too_large: "That file is too large to restore.",
  confirmation_required: "Type REPLACE exactly to confirm.",
  backup_too_large: "The backup is too large to export in one request.",
};

document.getElementById("export-btn").addEventListener("click", async () => {
  const errorEl = document.getElementById("export-error");
  const successEl = document.getElementById("export-success");
  errorEl.textContent = "";
  successEl.hidden = true;

  const stores = Array.from(document.querySelectorAll(".export-store:checked")).map((cb) => cb.value);
  if (!stores.length) {
    errorEl.textContent = "Select at least one store to include.";
    return;
  }

  const { ok, data } = await api.get(`/admin/backup?stores=${stores.join(",")}`);
  if (!ok) {
    errorEl.textContent = friendlyError(data, "Could not download backup.", BACKUP_ERROR_MESSAGES);
    return;
  }

  // Blob -> object URL -> synthetic <a download> -> revoke, the exact
  // sequence dashboard.js's CSV export uses (dashboard.js:743-751).
  // api.get already parsed the JSON; re-stringifying it costs nothing and
  // yields a diffable, human-inspectable file.
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `spin-shortener-backup-${new Date().toISOString().slice(0, 10)}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);

  successEl.textContent = "Backup downloaded.";
  successEl.hidden = false;
});

// Mirrors api/backup.py's MAX_BACKUP_BODY_BYTES so an oversized file can be
// rejected before FileReader ever reads it. The server is authoritative — a
// drift here only ever produces a body_too_large rejection naming the real
// limit, never silently wrong client behavior (same reasoning as
// dashboard.js's BULK_MAX_BODY_BYTES, dashboard.js:472-477).
const BACKUP_MAX_BODY_BYTES = 5242880;

let parsedBackup = null;

function formatCounts(counts) {
  const entries = Object.entries(counts || {});
  if (!entries.length) return "no entries";
  return entries.map(([store, n]) => `${n} ${store}`).join(", ");
}

function updateRestoreButtonState() {
  const confirmValue = document.getElementById("restore-confirm").value;
  document.getElementById("restore-btn").disabled =
    !(parsedBackup && parsedBackup.format === "spin-shortener-kv-backup" && confirmValue === "REPLACE");
}

// Rendered the moment the file is chosen, before any request is made — the
// single best safety affordance in this feature (see the plan): a parse
// failure or a wrong `format` is reported here, not after a POST.
function renderRestoreSummary(backup) {
  const summaryEl = document.getElementById("restore-summary");
  if (!backup || typeof backup !== "object") {
    summaryEl.innerHTML = `<p class="form-error">That file isn't valid JSON.</p>`;
    return;
  }
  if (backup.format !== "spin-shortener-kv-backup") {
    summaryEl.innerHTML = `<p class="form-error">That file isn't a spin-shortener backup.</p>`;
    return;
  }
  summaryEl.innerHTML = `
    <p>
      Created ${escapeHtml(backup.created_at || "an unknown time")} by ${escapeHtml(backup.created_by || "an unknown user")}
      (fidelity: ${escapeHtml(backup.fidelity || "unknown")}).<br />
      Contains: ${escapeHtml(formatCounts(backup.counts))}.
    </p>
  `;
}

document.getElementById("restore-file").addEventListener("change", (e) => {
  const fileInput = e.target;
  const file = fileInput.files[0];
  const errorEl = document.getElementById("restore-error");
  errorEl.textContent = "";
  document.getElementById("restore-result").innerHTML = "";
  document.getElementById("restore-summary").innerHTML = "";
  parsedBackup = null;
  updateRestoreButtonState();
  if (!file) return;

  if (file.size > BACKUP_MAX_BODY_BYTES) {
    errorEl.textContent = `That file is too large — the limit is ${Math.floor(BACKUP_MAX_BODY_BYTES / 1024 / 1024)} MiB.`;
    fileInput.value = "";
    return;
  }

  const reader = new FileReader();
  reader.addEventListener("load", () => {
    let backup;
    try {
      backup = JSON.parse(reader.result);
    } catch {
      renderRestoreSummary(null);
      return;
    }
    parsedBackup = backup;
    renderRestoreSummary(backup);
    updateRestoreButtonState();
  });
  reader.readAsText(file);
});

document.getElementById("restore-confirm").addEventListener("input", updateRestoreButtonState);

document.getElementById("restore-btn").addEventListener("click", async () => {
  const errorEl = document.getElementById("restore-error");
  const resultEl = document.getElementById("restore-result");
  errorEl.textContent = "";
  resultEl.innerHTML = "";

  if (!parsedBackup || document.getElementById("restore-confirm").value !== "REPLACE") return;

  const countText = formatCounts(parsedBackup.counts);
  if (!await confirmDialog(`Replace ${countText}? This can't be undone.`, { confirmLabel: "Replace everything" })) {
    return;
  }

  const { ok, data } = await api.post("/admin/restore", { confirm: "REPLACE", backup: parsedBackup });
  if (!ok) {
    errorEl.textContent = friendlyError(data, "Could not restore backup.", BACKUP_ERROR_MESSAGES);
    return;
  }

  const restoredText = formatCounts(data.restored);
  if (data.signed_out) {
    // Every session, including this one, was just deleted server-side by the
    // users-store prune pass. No auto-redirect and no timer: the restored
    // counts are the only confirmation the operator will ever get that the
    // restore succeeded, and yanking the page away before they read it would
    // be exactly wrong.
    setCsrfToken(null);
    resultEl.innerHTML = `
      <p class="form-success">Restored ${escapeHtml(restoredText)}.</p>
      <p>You have been signed out. Sign in again with the bootstrap admin credentials.</p>
      <p><a href="/login.html" role="button">Go to sign in</a></p>
    `;
  } else {
    resultEl.innerHTML = `<p class="form-success">Restored ${escapeHtml(restoredText)}.</p>`;
  }

  document.getElementById("restore-file").value = "";
  document.getElementById("restore-confirm").value = "";
  document.getElementById("restore-summary").innerHTML = "";
  parsedBackup = null;
  updateRestoreButtonState();
});

// Check id -> {title, meaning}, following BACKUP_ERROR_MESSAGES's precedent
// above: kept local because these strings matter only on this page. Falls
// back to the raw check id below for an id this map doesn't know, so a check
// added server-side without a label here renders as itself, not "undefined".
const CONSISTENCY_CHECK_LABELS = {
  unindexed_link: { title: "Links missing from the index", meaning: "A link record exists but isn't listed in the all-links index, so it's invisible to the dashboard even though it still resolves." },
  missing_link_record: { title: "Index entries with no link", meaning: "The all-links index names a slug that has no backing record. Harmless — the dashboard already skips it." },
  unindexed_owner_link: { title: "Links missing from their owner's index", meaning: "A link's owner record doesn't list it, so it's invisible on that owner's dashboard and deleting the owner would silently orphan it." },
  owner_index_mismatch: { title: "Links indexed under the wrong owner", meaning: "A link is listed under one owner's index but its record names a different owner, so that owner can't edit or delete it." },
  orphan_owner_index_entry: { title: "Owner index entries with no link", meaning: "An owner's index names a slug that has no backing record. Harmless — the dashboard already skips it." },
  unknown_link_owner: { title: "Links owned by a deleted account", meaning: "A link's record names an owner with no user record, so nobody can edit it and its owner can never be contacted." },
  dangling_owner_index: { title: "Owner indexes for deleted accounts", meaning: "An owner index still lists links for a username with no user record. If that username is ever recreated, it would inherit these links." },
  unindexed_user: { title: "Users missing from the username index", meaning: "A user record exists but isn't listed in the usernames index, so the account can sign in but is invisible to user administration." },
  missing_user_record: { title: "Username index entries with no user", meaning: "The usernames index names a user with no backing record. Harmless — recreating that username resolves it." },
  orphan_session: { title: "Sessions for deleted accounts", meaning: "A session names a username with no user record. Inert for now, but it would resolve again if that username were ever recreated." },
  unreadable_value: { title: "Unreadable values", meaning: "A key's value couldn't be parsed into its expected shape. It's excluded from every other check, which can mask other findings — fix or remove it and run again." },
  unrecognized_key: { title: "Unrecognized keys", meaning: "A key matches none of the known shapes for this store. It may be junk, or a new key type that this check needs to be taught about." },
};

function consistencyCheckLabel(checkId) {
  return CONSISTENCY_CHECK_LABELS[checkId] || { title: checkId, meaning: "" };
}

function renderConsistencyFindings(findings) {
  if (!findings.length) return "";
  // Was `<li>slug: winter · owner: admin</li>` — the raw record, with the
  // app's own components sitting unused three files away. A `slug` field IS a
  // link, so it renders as the slug chip and links to that link's detail page;
  // every other field stays a labelled pair, since the shapes vary by check.
  const items = findings
    .map((f) => {
      const parts = Object.entries(f).map(([k, v]) =>
        k === "slug"
          ? slugChip(String(v), { linked: true })
          : `<span class="finding-field"><span class="finding-key">${escapeHtml(k)}</span> ${escapeHtml(String(v))}</span>`
      );
      return `<li>${parts.join(" ")}</li>`;
    })
    .join("");
  return `<ul class="finding-list">${items}</ul>`;
}

function renderConsistencyCheck(check, { showSkippedNote } = {}) {
  const { title, meaning } = consistencyCheckLabel(check.check);
  const heading = showSkippedNote
    ? `<h3>${escapeHtml(title)}</h3>`
    : `<h3>${escapeHtml(title)} — ${check.count}</h3>`;
  const meaningHtml = meaning ? `<p>${escapeHtml(meaning)}</p>` : "";
  if (showSkippedNote) {
    return `${heading}${meaningHtml}<p>Not checked — an index key this check depends on was unreadable.</p>`;
  }
  const truncatedHtml = check.truncated
    ? `<p>Showing the first ${check.max_findings_per_check} of ${check.count}.</p>`
    : "";
  return `${heading}${meaningHtml}${renderConsistencyFindings(check.findings)}${truncatedHtml}`;
}

function renderConsistencyReport(report) {
  const resultEl = document.getElementById("consistency-result");

  if (report.ok) {
    const totalKeys = Object.values(report.scanned || {}).reduce((sum, s) => sum + (s.keys || 0), 0);
    const ran = report.checks || [];
    // A clean result used to be one sentence, which made it unfalsifiable to
    // the operator: "no inconsistencies" is only reassuring if you can see
    // WHAT was looked for. The list is collapsed behind the app's own
    // <details> idiom so the happy path stays one line unless asked.
    const checkList = ran
      .map((c) => `<li>${escapeHtml(consistencyCheckLabel(c.check).title)}${c.skipped ? " <em>(not checked)</em>" : ""}</li>`)
      .join("");
    resultEl.innerHTML = `
      <p class="form-success">
        All ${ran.length} check${ran.length === 1 ? "" : "s"} passed — ${totalKeys} keys scanned across the links and users stores.
      </p>
      <details>
        <summary>What was checked</summary>
        <ul>${checkList}</ul>
      </details>
    `;
    return;
  }

  const needsAttention = report.checks.filter((c) => c.severity === "warning" && c.count > 0 && !c.skipped);
  const informational = report.checks.filter((c) => c.severity === "info" && c.count > 0 && !c.skipped);
  const notChecked = report.checks.filter((c) => c.skipped);

  const sections = [];
  if (needsAttention.length) {
    sections.push(`<h2>Needs attention</h2>${needsAttention.map((c) => renderConsistencyCheck({ ...c, max_findings_per_check: report.max_findings_per_check })).join("")}`);
  }
  if (informational.length) {
    sections.push(`<h2>Informational</h2>${informational.map((c) => renderConsistencyCheck({ ...c, max_findings_per_check: report.max_findings_per_check })).join("")}`);
  }
  if (notChecked.length) {
    sections.push(`<h2>Not checked</h2>${notChecked.map((c) => renderConsistencyCheck(c, { showSkippedNote: true })).join("")}`);
  }

  resultEl.innerHTML = sections.join("");
}

document.getElementById("consistency-btn").addEventListener("click", async () => {
  const errorEl = document.getElementById("consistency-error");
  const resultEl = document.getElementById("consistency-result");
  errorEl.textContent = "";
  resultEl.innerHTML = "";

  const { ok, data } = await api.get("/admin/consistency");
  if (!ok) {
    errorEl.textContent = friendlyError(data, "Could not run the consistency check.", BACKUP_ERROR_MESSAGES);
    return;
  }

  renderConsistencyReport(data);
});

initHeader({
  dashboardHref: "../dashboard.html",
  pageLabel: "Backup and restore",
  manageUsersHref: "users.html",
}).then((result) => {
  // Both /api/admin/backup and /api/admin/restore gate on users.manage
  // (auth.py's Principal.has_permission — true for role == "admin" too),
  // mirroring app.js's own canManageUsers check rather than round-tripping
  // to the server just to discover the same answer.
  const canManage = result.ok && (result.data.role === "admin" || result.data.permissions.includes("users.manage"));
  if (!canManage) {
    document.getElementById("forbidden-notice").hidden = false;
    document.getElementById("admin-content").style.display = "none";
  }
});
