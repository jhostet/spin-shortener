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
