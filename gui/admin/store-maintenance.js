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
  unknown_link_owner: {
    title: "Links owned by a deleted account",
    meaning: "A link's record names an owner with no user record, so nobody can edit it and its owner can never be contacted.",
    fix: "Reassign or delete these links from the dashboard's owner filter.",
  },
  unindexed_user: {
    title: "Users missing from the username index",
    meaning: "A user record exists but isn't listed in the usernames index, so the account can sign in but is invisible to user administration.",
    fix: "Adds these usernames back to the usernames index.",
  },
  missing_user_record: {
    title: "Username index entries with no user",
    meaning: "The usernames index names a user with no backing record. Harmless — recreating that username resolves it.",
    fix: "Removes these usernames from the usernames index.",
  },
  orphan_session: {
    title: "Sessions for deleted accounts",
    meaning: "A session names a username with no user record. Inert for now, but it would resolve again if that username were ever recreated.",
    fix: "Deletes these sessions.",
  },
  unreadable_value: {
    title: "Unreadable values",
    meaning: "A key's value couldn't be parsed into its expected shape. It's excluded from every other check, which can mask other findings — fix or remove it and run again.",
    fix: "Inspect the key with the KV explorer and repair or delete it by hand.",
  },
  unrecognized_key: {
    title: "Unrecognized keys",
    meaning: "A key matches none of the known shapes for this store. It may be junk, or a new key type that this check needs to be taught about.",
    fix: "Inspect the key with the KV explorer — do not delete it without understanding what wrote it.",
  },
};

function consistencyCheckLabel(checkId) {
  return CONSISTENCY_CHECK_LABELS[checkId] || { title: checkId, meaning: "", fix: "" };
}

function renderConsistencyFindings(findings) {
  if (!findings.length) return "";
  // Was `<li>slug: winter · owner: admin</li>` — the raw record, with the
  // app's own components sitting unused three files away. A `slug` field IS a
  // link, so it renders as the slug chip and links to that link's detail page;
  // every other field stays a labelled pair, since the shapes vary by check.
  const items = findings
    .map((f) => {
      const parts = Object.entries(f).map(([k, v]) => {
        if (k === "slug") return slugChip(String(v), { linked: true });
        // "reason" is the one field whose value is a sentence rather than an
        // identifier (docs/plans/api-record-unreadable-diagnostics.md) — measured
        // live at 390px, the longest realistic reason overflows the viewport
        // under .finding-field's nowrap, so it alone gets a wrapping modifier.
        const cls = k === "reason" ? "finding-field finding-field-wrap" : "finding-field";
        return `<span class="${cls}"><span class="finding-key">${escapeHtml(k)}</span> ${escapeHtml(String(v))}</span>`;
      });
      return `<li>${parts.join(" ")}</li>`;
    })
    .join("");
  return `<ul class="finding-list">${items}</ul>`;
}

function renderBlockedEntries(blocked) {
  if (!blocked || !blocked.length) return "";
  const items = blocked
    .map((b) => {
      const parts = [];
      if (b.slug) parts.push(slugChip(String(b.slug), { linked: true }));
      if (b.username) parts.push(`<span class="finding-field"><span class="finding-key">username</span> ${escapeHtml(b.username)}</span>`);
      parts.push(`<span class="finding-field"><span class="finding-key">reason</span> ${escapeHtml(b.reason)}</span>`);
      if (b.next_step) {
        parts.push(`<span class="finding-field"><span class="finding-key">next step</span> Repair ${escapeHtml(consistencyCheckLabel(b.next_step).title)} first, then run this again</span>`);
      }
      return `<li>${parts.join(" ")}</li>`;
    })
    .join("");
  return `<p><strong>Blocked — needs a judgement call:</strong></p><ul class="finding-list">${items}</ul>`;
}

function renderConsistencyCheck(check, { showSkippedNote, repairableChecks = [], blockedByCheck = {} } = {}) {
  const { title, meaning, fix } = consistencyCheckLabel(check.check);
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
  const canRepair = repairableChecks.includes(check.check) && check.count > 0 && !check.skipped;
  const fixHtml = fix ? `<p class="form-note">${escapeHtml(fix)}</p>` : "";
  const repairButtonHtml = canRepair
    ? `<button type="button" class="outline repair-btn" data-check="${escapeHtml(check.check)}">Repair</button>`
    : "";
  const blockedHtml = renderBlockedEntries(blockedByCheck[check.check]);
  return `${heading}${meaningHtml}${renderConsistencyFindings(check.findings)}${truncatedHtml}${fixHtml}${repairButtonHtml}${blockedHtml}`;
}

let latestConsistencyReport = null;

function buildConsistencyReportHtml(report, { blockedByCheck = {} } = {}) {
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
    return `
      <p class="form-success">
        All ${ran.length} check${ran.length === 1 ? "" : "s"} passed — ${totalKeys} keys scanned across the links and users stores.
      </p>
      <details>
        <summary>What was checked</summary>
        <ul>${checkList}</ul>
      </details>
    `;
  }

  const repairableChecks = report.repairable_checks || [];
  const needsAttention = report.checks.filter((c) => c.severity === "warning" && c.count > 0 && !c.skipped);
  const informational = report.checks.filter((c) => c.severity === "info" && c.count > 0 && !c.skipped);
  const notChecked = report.checks.filter((c) => c.skipped);

  const repairableWithFindings = report.checks.filter(
    (c) => repairableChecks.includes(c.check) && c.count > 0 && !c.skipped
  );

  const sections = [];
  if (repairableWithFindings.length >= 2) {
    sections.push(`<button type="button" class="outline" id="repair-all-btn">Repair all repairable findings</button>`);
  }
  const renderOpts = (extra) => ({ repairableChecks, blockedByCheck, ...extra });
  if (needsAttention.length) {
    sections.push(`<h2>Needs attention</h2>${needsAttention.map((c) => renderConsistencyCheck({ ...c, max_findings_per_check: report.max_findings_per_check }, renderOpts())).join("")}`);
  }
  if (informational.length) {
    sections.push(`<h2>Informational</h2>${informational.map((c) => renderConsistencyCheck({ ...c, max_findings_per_check: report.max_findings_per_check }, renderOpts())).join("")}`);
  }
  if (notChecked.length) {
    sections.push(`<h2>Not checked</h2>${notChecked.map((c) => renderConsistencyCheck(c, { showSkippedNote: true })).join("")}`);
  }

  return sections.join("");
}

function renderConsistencyReport(report, opts) {
  latestConsistencyReport = report;
  document.getElementById("consistency-result").innerHTML = buildConsistencyReportHtml(report, opts);
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

// Call-site override map for POST /admin/consistency/repair's error codes —
// the same pattern PURGE_ERROR_MESSAGES follows, and necessary for the same
// reason: BACKUP_ERROR_MESSAGES.confirmation_required reads "Type REPLACE
// exactly to confirm", which is wrong for a confirmation set programmatically
// rather than typed.
const REPAIR_ERROR_MESSAGES = {
  confirmation_required: "Something went wrong confirming this action — try again.",
  no_checks: "There's nothing selected to repair.",
  unknown_check: "That check doesn't exist.",
  check_not_repairable: "That finding needs a judgement call — see its note for what to do instead.",
  duplicate_check: "The same check appeared twice in this request.",
};

let repairStopped = false;

async function onRepairClick(checkIds) {
  const report = latestConsistencyReport;
  if (!report) return;
  const byId = Object.fromEntries(report.checks.map((c) => [c.check, c]));
  const totalFindings = checkIds.reduce((sum, id) => sum + (byId[id] ? byId[id].count : 0), 0);
  const confirmed = await confirmDialog(
    `Repair ${totalFindings} finding${totalFindings === 1 ? "" : "s"} across ${checkIds.length} check${checkIds.length === 1 ? "" : "s"}? This rewrites index keys and can't be undone.`,
    { confirmLabel: "Repair" },
  );
  if (!confirmed) return;
  await runConsistencyRepair(checkIds);
}

async function runConsistencyRepair(checkIds) {
  repairStopped = false;
  const errorEl = document.getElementById("consistency-error");
  const resultEl = document.getElementById("consistency-result");
  errorEl.textContent = "";

  let totalWrites = 0;
  const blockedByCheck = {};
  let lastData = null;

  resultEl.innerHTML = `<p id="repair-progress" aria-live="polite">Repairing…</p><button type="button" id="repair-stop-btn" class="outline">Stop</button>`;
  document.getElementById("repair-stop-btn").onclick = () => { repairStopped = true; };

  // Chunked loop, modelled on runOrphanPurge: re-detects and re-plans every
  // pass rather than trusting anything from a previous pass or the report
  // the operator was looking at.
  for (;;) {
    const { ok, data } = await api.post("/admin/consistency/repair", { confirm: "REPAIR", checks: checkIds });
    if (!ok) {
      errorEl.textContent = friendlyError(data, "Could not repair these findings.", REPAIR_ERROR_MESSAGES);
      return;
    }
    lastData = data;
    totalWrites += data.writes;
    for (const b of data.blocked) {
      (blockedByCheck[b.check] = blockedByCheck[b.check] || []).push(b);
    }
    document.getElementById("repair-progress").textContent = `Repaired ${totalWrites} writes so far…`;
    if (data.complete || repairStopped) break;
    if (data.writes === 0) {
      // docs/plans/write-throttle-resilience.md: write_failed names the
      // cause (the store is throttling writes) rather than leaving the
      // operator with an unexplained symptom.
      document.getElementById("repair-progress").textContent =
        data.write_failed && data.write_failed.length
          ? "The store was busy — some repairs could not be written. Wait a moment and run the check again."
          : "Repair made no progress.";
      document.getElementById("repair-stop-btn").hidden = true;
      return;
    }
  }

  // De-duplicate blocked entries accumulated across passes — the same
  // still-blocked finding is reported again on every re-detecting pass.
  for (const checkId of Object.keys(blockedByCheck)) {
    const seen = new Set();
    blockedByCheck[checkId] = blockedByCheck[checkId].filter((b) => {
      const key = JSON.stringify(b);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  const { ok, data: freshReport } = await api.get("/admin/consistency");
  if (!ok) {
    errorEl.textContent = friendlyError(freshReport, "Could not run the consistency check.", BACKUP_ERROR_MESSAGES);
    return;
  }

  const totalRepaired = lastData.checks.reduce((sum, c) => sum + c.repaired, 0);
  const summaryHtml = repairStopped
    ? `<p>Stopped. Repaired ${totalWrites} write${totalWrites === 1 ? "" : "s"} so far.</p>`
    : `<p class="form-success">Repaired ${totalRepaired} finding${totalRepaired === 1 ? "" : "s"} in ${totalWrites} write${totalWrites === 1 ? "" : "s"}.</p>`;

  latestConsistencyReport = freshReport;
  document.getElementById("consistency-result").innerHTML =
    summaryHtml + buildConsistencyReportHtml(freshReport, { blockedByCheck });
}

// Event delegation, not per-render listener attachment: repair buttons are
// rendered dynamically (inside innerHTML swaps), so one listener on the
// container that survives every re-render is simpler and cannot double-fire.
document.getElementById("consistency-result").addEventListener("click", (e) => {
  const repairBtn = e.target.closest(".repair-btn");
  if (repairBtn) {
    onRepairClick([repairBtn.dataset.check]);
    return;
  }
  if (e.target.closest("#repair-all-btn")) {
    const report = latestConsistencyReport;
    if (!report) return;
    const repairableChecks = report.repairable_checks || [];
    const ids = report.checks
      .filter((c) => repairableChecks.includes(c.check) && c.count > 0 && !c.skipped)
      .map((c) => c.check);
    onRepairClick(ids);
  }
});

// Call-site override map, passed as friendlyError's third argument exactly
// as its docstring intends — "lets one call site's copy win over the shared
// map." Not optional tidiness here: BACKUP_ERROR_MESSAGES.confirmation_required
// reads "Type REPLACE exactly to confirm", which would be actively wrong for
// a purge whose confirmation is set programmatically, not typed.
const PURGE_ERROR_MESSAGES = {
  confirmation_required: "Something went wrong confirming this action — try again.",
  links_index_unreadable: "The links index couldn't be read. Run the consistency check first.",
  too_many_slugs: "Too many links in one batch — try again.",
  invalid_slug: "One of the links in this batch has an invalid name.",
  no_slugs: "There's nothing to purge.",
  duplicate_slug: "The same link appeared twice in this batch.",
};

let latestOrphanReport = null;
let orphanPurgeStopped = false;

function formatOrphanHeadline(report) {
  const { orphan_keys, orphan_slugs } = report.totals;
  const linkWord = orphan_slugs === 1 ? "link that no longer exists" : "links that no longer exist";
  return `${orphan_keys} of ${report.scanned.analytics_keys} analytics keys belong to ${orphan_slugs} ${linkWord}.`;
}

// docs/plans/drop-events-write.md: redirect stopped writing events:<slug>:<slot>
// keys 2026-08-18. The residual is frozen, not growing, and this names it only
// when it's non-zero — a store already swept of pre-cutover keys shows nothing.
// No purge button for these here; see the plan's "Leftover events: keys"
// section for why a sweep would need to invert the existing purge's safety
// property (it must delete a LIVE link's leftover keys too) and is deferred.
function formatObsoleteEventKeysNote(report) {
  const count = report.totals.obsolete_event_keys;
  if (!count) return "";
  return `<p>${count} of these are <code>events:</code> keys from the retired recent-events feature and are safe to remove.</p>`;
}

function renderOrphanList(report) {
  if (!report.orphans.length) return "";
  const items = report.orphans
    .map((o) => `<li>${slugChip(o.slug)} <span class="finding-field"><span class="finding-key">keys</span> ${o.keys}</span></li>`)
    .join("");
  const truncatedHtml = report.truncated
    ? `<p>Showing the first ${report.max_orphan_slugs} of ${report.totals.orphan_slugs}. Purging will fetch the rest.</p>`
    : "";
  return `<ul class="finding-list">${items}</ul>${truncatedHtml}`;
}

function renderOrphanReport(report) {
  const resultEl = document.getElementById("orphans-result");
  latestOrphanReport = report;

  if (report.totals.orphan_slugs === 0) {
    resultEl.innerHTML = `
      <p class="form-success">No orphaned analytics — every analytics key belongs to a link that still exists.</p>
      ${formatObsoleteEventKeysNote(report)}
    `;
    return;
  }

  resultEl.innerHTML = `
    <p>${escapeHtml(formatOrphanHeadline(report))}</p>
    ${formatObsoleteEventKeysNote(report)}
    ${renderOrphanList(report)}
    <button type="button" id="purge-btn" class="outline secondary">Delete these analytics keys</button>
    <button type="button" id="purge-stop-btn" class="outline" hidden>Stop</button>
    <p id="purge-progress" aria-live="polite"></p>
  `;

  document.getElementById("purge-btn").addEventListener("click", onPurgeClick);
}

async function onPurgeClick() {
  const report = latestOrphanReport;
  if (!report) return;

  const { orphan_keys, orphan_slugs } = report.totals;
  const confirmed = await confirmDialog(
    `Permanently delete ${orphan_keys} analytics keys for ${orphan_slugs} deleted link${orphan_slugs === 1 ? "" : "s"}? This can't be undone.`,
    { confirmLabel: "Delete analytics" },
  );
  if (!confirmed) return;

  await runOrphanPurge(report);
}

async function runOrphanPurge(initialReport) {
  orphanPurgeStopped = false;
  const errorEl = document.getElementById("orphans-error");
  const progressEl = document.getElementById("purge-progress");
  const findBtn = document.getElementById("orphans-btn");
  const purgeBtn = document.getElementById("purge-btn");
  const stopBtn = document.getElementById("purge-stop-btn");
  errorEl.textContent = "";
  findBtn.disabled = true;
  purgeBtn.disabled = true;
  stopBtn.hidden = false;
  stopBtn.onclick = () => { orphanPurgeStopped = true; };

  let report = initialReport;
  let slugs = report.orphans.map((o) => o.slug);
  const totalKeysAtStart = report.totals.orphan_keys;
  let deleted = 0;
  let noProgress = false;

  while (slugs.length && !orphanPurgeStopped) {
    const chunk = slugs.slice(0, 50);
    const { ok, data } = await api.post("/admin/analytics/purge", { confirm: "PURGE", slugs: chunk });
    if (!ok) {
      errorEl.textContent = friendlyError(data, "Could not purge orphaned analytics.", PURGE_ERROR_MESSAGES);
      break;
    }
    deleted += data.deleted_keys;
    const nextSlugs = data.remaining_slugs.concat(slugs.slice(50));
    // docs/plans/write-throttle-resilience.md: before write-failure reporting
    // existed, `plan_purge` always planned at least one slug, so progress was
    // guaranteed and this loop could never spin. A throttled delete can now
    // return "0 deleted, same slugs remaining" — without this guard that is
    // an infinite loop, mirroring the repair loop's existing one above.
    if (data.deleted_keys === 0 && JSON.stringify(nextSlugs) === JSON.stringify(slugs)) {
      noProgress = true;
      slugs = nextSlugs;
      break;
    }
    slugs = nextSlugs;
    progressEl.textContent = `Deleted ${deleted} of ${totalKeysAtStart} keys…`;
  }

  if (noProgress) {
    progressEl.textContent = "The store was busy — wait a moment and try Find again.";
    stopBtn.hidden = true;
    findBtn.disabled = false;
    purgeBtn.disabled = false;
    return;
  }

  // A truncated report means more orphans existed than the report showed —
  // re-fetch and keep going, since one click should finish the job on a
  // store with thousands of orphans.
  if (report.truncated && !orphanPurgeStopped && !slugs.length) {
    const { ok, data } = await api.get("/admin/analytics/orphans");
    if (ok && data.totals.orphan_slugs > 0) {
      progressEl.textContent = `Deleted ${deleted} keys so far — continuing…`;
      findBtn.disabled = false;
      stopBtn.hidden = true;
      await runOrphanPurge(data);
      return;
    }
  }

  findBtn.disabled = false;
  stopBtn.hidden = true;
  if (errorEl.textContent) {
    purgeBtn.disabled = false;
  } else if (!slugs.length) {
    document.getElementById("orphans-result").innerHTML =
      `<p class="form-success">Deleted ${deleted} analytics keys. Every analytics key now belongs to a link that still exists.</p>`;
  } else {
    progressEl.textContent = `Stopped. Deleted ${deleted} keys so far.`;
    purgeBtn.disabled = false;
  }
}

document.getElementById("orphans-btn").addEventListener("click", async () => {
  const errorEl = document.getElementById("orphans-error");
  const resultEl = document.getElementById("orphans-result");
  errorEl.textContent = "";
  resultEl.innerHTML = "";

  const { ok, data } = await api.get("/admin/analytics/orphans");
  if (!ok) {
    errorEl.textContent = friendlyError(data, "Could not check for orphaned analytics.", PURGE_ERROR_MESSAGES);
    return;
  }

  renderOrphanReport(data);
});

initHeader({
  dashboardHref: "../dashboard.html",
  pageLabel: "Store maintenance",
  adminHref: "index.html",
}).then((result) => {
  renderAdminNav(document.getElementById("admin-nav"), "store-maintenance");
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
