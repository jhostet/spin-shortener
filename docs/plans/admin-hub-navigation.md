# Admin Hub Navigation

## Context

`gui/admin/store-maintenance.html` (backup/restore, consistency check + repair,
orphaned-analytics purge) and `gui/admin/url-policy.html` (destination allow/deny
rules) are reachable today from exactly one place: an in-body anchor pair inside
the **"Users"** `<h2>` article on `gui/admin/users.html`:

```html
<p><a class="operator-link" href="store-maintenance.html">Store maintenance</a> · <a class="operator-link" href="url-policy.html">Destination URL policy</a></p>
```

Two problems with that, and they are different problems:

1. **Reachability.** The only entry point into either tool is the Manage Users
   page. An operator who wants to restore a backup must first land on a page
   about accounts.
2. **Framing.** Sitting under a "Users" heading makes both tools read as a
   sub-feature of user management. They are not. `PRODUCT.md` principle 5 asks
   admin to be "visually and functionally distinct from everyday link-creation
   workflows"; it says nothing about admin being a wing of user administration.

This arrangement was never designed — it is the residue of a measured constraint.
`DESIGN.md`'s Navigation section records that a fifth persistent-nav item
("Backup") was **built exactly as specified, measured, and reverted**: on
`links/detail.html` at 768px it overflowed `#app-header nav` in both themes
(`scrollWidth` 716 vs `clientWidth` 700), and hiding that single `<li>` returned
`scrollWidth` to exactly 700. An overflow/dropdown menu was explicitly rejected in
the same note. The instruction left behind is: **"Treat the next nav addition as a
redesign, not an insertion."** The in-body anchor was the pre-approved fallback,
and `DESIGN.md` adds: *"Two is still legible; a third should trigger a rethink."*

So the literal request — "add a nav item for each" — is the exact thing already
shown to break, twice over (two items, not one). This plan does the redesign
instead: **the nav's admin surface collapses from one page-specific item
("Manage users") to one area item ("Admin"), pointing at a new admin hub page
that links all three tools as equals.** Item count in the nav is unchanged;
rendered width goes *down*; and every future admin tool becomes a card on the hub
rather than a fight with the nav budget.

**Confirmed decisions (settled before planning):**

- Do not add a flat 6th (or 5th) nav item — that is the measured failure.
- Do not add a nav overflow menu — already rejected in `DESIGN.md`.
- All three pages gate on `users.manage` today, so there is no
  permission-visibility subtlety of the kind the domain selector has (confirmed
  below).
- The planner picks the approach; the hub-page shape and the dropdown were both
  named as candidates to weigh.

## Key technical facts confirmed during research

- **The nav has exactly five account-group items today**, in this order:
  `#whoami` (identity chip), `#manage-users-link` (hidden unless
  `role === "admin" || permissions.includes("users.manage")`, and additionally
  hidden on the Manage Users page itself via `onManageUsersPage`),
  `#domain-control` (hidden when fewer than 2 domains are on offer),
  `#theme-control`, and the Log out button. Confirmed in `gui/app.js`
  `initHeader()` (lines 459–521).
- **`initHeader()` has exactly five callers**, all found by grep for
  `manageUsersHref|onManageUsersPage`: `gui/dashboard.js:16` (no options — takes
  every default), `gui/links/detail.js:107` (`manageUsersHref:
  "../admin/users.html"`), `gui/admin/users.js:345` (`manageUsersHref:
  "users.html", onManageUsersPage: true`), `gui/admin/url-policy.js:285` and
  `gui/admin/store-maintenance.js:643` (both `manageUsersHref: "users.html"`).
  `#manage-users-link` appears in no CSS file — grep over `gui/**` returns only
  `app.js`.
- **Only `admin/users.html` hides the nav's admin item today.** `url-policy.js`
  and `store-maintenance.js` pass no `onManageUsersPage`, so those two pages
  already render five account-group items. **Consequence for measurement:
  `admin/users.html` is the one page that *gains* an item under this plan**;
  every other page's item gets narrower ("Admin" replaces "Manage users").
- **Every endpoint behind all three pages gates on `users.manage`.** Confirmed by
  grep over `api/*.py`: `api/users.py` (5 sites), `api/backup.py` (export +
  restore), `api/consistency.py`, `api/consistencyrepair.py`,
  `api/analyticsorphans.py` (report + purge), `api/urlpolicy.py` (3 sites). All
  use `principal.has_permission("users.manage")`, which `api/auth.py:96` defines
  as `self.role == "admin" or permission in self.permissions`. `users.manage` is
  in `KNOWN_PERMISSIONS` (`api/auth.py:37-39`).
- **The client-side gate differs by page, and that difference is pre-existing.**
  `store-maintenance.js:643-655` and `url-policy.js:275-296` both compute
  `canManage = result.ok && (result.data.role === "admin" || result.data.permissions.includes("users.manage"))`
  and, when false, unhide `#forbidden-notice` and set
  `#admin-content`'s `style.display = "none"`. `admin/users.js` has **no**
  client-side permission check — it reveals the same two elements from the
  `GET /api/users` failure path (`users.js:147-152`). This plan copies the
  `store-maintenance.js` shape for the new hub and leaves `users.js`'s gate alone.
- **`gui-pages/routing.py`'s `ROUTES` is a fixed 8-entry allowlist**, and `/` and
  `/index.html` already both map to `index.html` — precedent for mapping two
  request paths onto one file.
- **`gui-pages/tests/test_no_inline_code.py` derives its page list from
  `ROUTES`** (`PAGES = sorted(set(ROUTES.values()))`, line 26) and its script list
  from `GUI_DIR.rglob("*.js")` (line 44-46). A new page and a new script are
  therefore covered automatically, and both must carry no inline
  `<script>`/`<style>`/`style="`/`on<event>=`.
- **`spin.toml` has 16 `gui`-component routes today** (`grep -c 'component = "gui"'`
  → 16, matching CLAUDE.md's "only these 16 exact routes"). A page's `.js` needs
  its own exact route or the page renders with a silently 404ing script; wildcard
  routes on this component are confirmed broken.
- **Baseline test counts, run 2026-08-27:** `cd gui-pages && uv run pytest` →
  **108 passed**. `api/` and `redirect/` are untouched by this plan.
- **`.operator-link` already exists in `gui/theme.css:949-953`**
  (`display: inline-flex; align-items: center; min-height: 44px`), described in
  its own comment as *"the sanctioned in-body alternative to a nav item"*. The
  sitewide 44px tap-target floor covers the button family, `a[role="button"]`,
  `#app-header nav a`, `select`/`input`/`textarea`, and `.operator-link` — **not
  plain body anchors** (`gui/theme.css:866-882`). So any new in-body navigation
  anchor must carry `.operator-link` to meet the app's own floor.
- **UNCONFIRMED: that "Admin" renders narrower than "Manage users" in the nav.**
  It is 5 characters against 12 in the same font at the same size, so it is very
  hard for it not to be — but this repo measures rather than reasons about nav
  width, and `DESIGN.md` records three separate occasions where a nav width
  assumption was wrong. Confirming it takes the `scrollWidth`/`clientWidth`
  protocol in the Verification section below; the measurement task carries a
  hard stop if it overflows.
- **UNCONFIRMED: whether Pico's `<details class="dropdown">` behaves acceptably
  inside `#app-header nav`.** Not confirmed because the dropdown design was
  rejected (see Trade-offs) — it would need a live measurement of the open panel
  at 390px, a `getComputedStyle` check against the
  `#app-header nav li { color: #fff }` specificity trap, and a check that
  `--pico-dropdown-box-shadow: none` (already set in both theme blocks,
  `gui/theme.css:86` and `:184`) actually holds for a nav-hosted dropdown.

## The decision

**One nav item for the admin *area*, not for a page in it, plus a hub page that
lists the tools as equals, plus a two-link sibling strip on each tool page.**

Concretely:

1. `#manage-users-link` → `#admin-link`, labelled **"Admin"**, pointing at a new
   `gui/admin/index.html`. Same permission condition, same position in the
   account group, one item — **not a sixth item, and not a wider one.**
2. New `gui/admin/index.html` — an admin hub listing three cards (Manage users,
   Store maintenance, Destination URL policy), each a link plus a one-sentence
   description of what the tool does.
3. Each of the three tool pages gains a rendered sibling strip naming the **other
   two** tools, so lateral movement never routes through the hub and never routes
   through Manage Users.
4. `admin/users.html`'s `<p>Store maintenance · Destination URL policy</p>` under
   the "Users" heading is **deleted** — the framing bias goes away with it.

Why this is the redesign `DESIGN.md` asked for rather than an insertion:

- The nav's admin entry stops naming a *page* and starts naming an *area*. That
  is a structural change to what the nav's fourth item means, which is why a
  fifth admin tool later costs zero nav width instead of another overflow
  measurement and another middot.
- It spends nav width rather than adding it: the widest text item in the account
  group shrinks from "Manage users" to "Admin".
- It retires the "a third middot should trigger a rethink" trigger without ever
  reaching three: the strip on each page holds exactly two links (the other two
  tools), permanently, by construction — the current page is always the one
  omitted.

## GUI changes — shared code (`gui/app.js`)

Three edits, all in `gui/app.js`, which every page already loads.

**1. A single source of truth for the admin page list.** Add near the other
module-level constants (e.g. beside `SS_DOMAIN_KEY`):

```js
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
```

**2. `renderAdminNav(container, currentId)`** — the sibling strip, rendered into a
container that each tool page provides:

```js
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
```

**3. `initHeader()`'s admin item.** Rename the two options and the `<li>`:

- signature: `manageUsersHref = "admin/users.html"` → `adminHref = "admin/index.html"`;
  `onManageUsersPage = false` → `onAdminHome = false`.
- markup: `<li id="manage-users-link" hidden><a href="${manageUsersHref}">Manage users</a></li>`
  → `<li id="admin-link" hidden><a href="${adminHref}">Admin</a></li>`.
- reveal condition: unchanged permission test, new flag —
  `if (canManageUsers && !onAdminHome) { document.getElementById("admin-link").hidden = false; }`.
  Keep the local variable name `canManageUsers`: it is still exactly the
  `users.manage`-or-admin test, and renaming it would imply a permission change
  that is not happening.
- Update the block comment above `initHeader()` (lines 445–458) so it describes
  `adminHref`/`onAdminHome` and says the item names the admin *area*, hidden only
  on the hub itself.

**No stale option may be left behind at a call site.** A caller still passing
`manageUsersHref` would silently fall back to the `adminHref` default, producing a
wrong-depth link from `links/detail.html` — destructuring ignores unknown keys, so
nothing would throw. All five callers are listed below.

## GUI changes — the hub page (new)

**`gui/admin/index.html` (new).** Structure mirrors `admin/store-maintenance.html`
exactly (same `<head>` order, same `#app-header`, same `#forbidden-notice` /
`#admin-content` pair), with **no page-scoped `.css`**:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>spin-shortener — Admin</title>
  <script src="../theme-init.js"></script>
  <link rel="icon" href="../favicon.svg" type="image/svg+xml" />
  <link rel="stylesheet" href="../vendor/pico.min.css" />
  <link rel="stylesheet" href="../theme.css" />
</head>
<body>
  <header id="app-header" class="container"></header>

  <main class="container">
    <h1>Admin</h1>
    <p id="forbidden-notice" class="form-error" role="alert" hidden>
      You don't have permission to use the admin tools.
    </p>

    <div id="admin-content">
      <p>Tools for administering this shortener. Each one needs the Manage users permission.</p>
      <div id="admin-cards"></div>
    </div>
  </main>

  <script src="../app.js"></script>
  <script src="index.js"></script>
</body>
</html>
```

**`gui/admin/index.js` (new).** Renders the cards from `ADMIN_PAGES` and gates on
the same client-side check `store-maintenance.js` uses:

```js
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
```

Two deliberate details:

- **Cards are rendered only after the check passes**, rather than rendered and
  then hidden. The other two pages hide static markup because their content is
  static; this page's is not, so there is no reason to paint it first. The
  `#forbidden-notice` / `style.display = "none"` pair is kept identical anyway so
  the forbidden state looks the same on all four admin pages.
- **`escapeHtml` on literals** is not defensive necessity (nothing here is user
  data) — it matches `renderDomainSelector`'s existing habit so no future editor
  has to work out which `innerHTML` template in this file is the unescaped one.

**No new CSS, no new design token.** The hub uses `<article>` (the existing card),
`<h2>` (existing type step) and `.operator-link` (existing). `.impeccable/design.json`
needs no change — say so explicitly in the docs task rather than silently omitting it.

## GUI changes — the three tool pages

Each of `gui/admin/users.html`, `gui/admin/store-maintenance.html` and
`gui/admin/url-policy.html` gains, as **the first child of `#admin-content`**
(so a forbidden viewer never sees it — that element is hidden on the forbidden
path on all three pages):

```html
      <p id="admin-nav"></p>
```

and each page's script calls the renderer immediately after `initHeader(...)`
resolves:

| file | call |
|---|---|
| `gui/admin/users.js` | `renderAdminNav(document.getElementById("admin-nav"), "users");` |
| `gui/admin/store-maintenance.js` | `renderAdminNav(document.getElementById("admin-nav"), "store-maintenance");` |
| `gui/admin/url-policy.js` | `renderAdminNav(document.getElementById("admin-nav"), "url-policy");` |

**`gui/admin/users.html` additionally deletes line 55** — the
`<p><a class="operator-link" href="store-maintenance.html">Store maintenance</a> · <a class="operator-link" href="url-policy.html">Destination URL policy</a></p>`
inside the "Users" article. That deletion is the point of the whole change; do not
leave it in place "for now."

`initHeader` option updates at the five existing call sites:

| file | before | after |
|---|---|---|
| `gui/dashboard.js:16` | `initHeader()` | **unchanged** — it takes every default, and the default `adminHref` is `"admin/index.html"` |
| `gui/links/detail.js:107` | `manageUsersHref: "../admin/users.html"` | `adminHref: "../admin/index.html"` |
| `gui/admin/users.js:345` | `manageUsersHref: "users.html", onManageUsersPage: true` | `adminHref: "index.html"` (drop the flag — the item now points somewhere else, so it is useful here) |
| `gui/admin/store-maintenance.js:643` | `manageUsersHref: "users.html"` | `adminHref: "index.html"` |
| `gui/admin/url-policy.js:285` | `manageUsersHref: "users.html"` | `adminHref: "index.html"` |

## Routing and manifest changes

**`spin.toml`** — one new exact route on the `gui` component, placed with the
other page-scoped assets:

```toml
[[trigger.http]]
route = "/admin/index.js"
component = "gui"
```

Exact, never a wildcard (the confirmed `spin_static_fs` gotcha), and mandatory:
without it the hub renders and its script silently 404s. No `files` mapping change
— `gui` already maps all of `gui/`.

**`gui-pages/routing.py`** — two `ROUTES` entries for one file:

```python
    "/admin/": "admin/index.html",
    "/admin/index.html": "admin/index.html",
```

Both are wanted. Every in-app link uses `index.html` (consistent with every other
link in this app); `/admin/` exists because an operator who types the directory
would otherwise get the styled 404, and mapping two paths to one file already has
precedent in `"/"` + `"/index.html"`. Relative asset resolution is identical for
both (`../theme-init.js` resolves against `/admin/` either way).

**`gui-pages/tests/test_routing.py`** — two new `test_resolve_file` parameters,
`("/admin/", "admin/index.html")` and `("/admin/index.html", "admin/index.html")`.
`test_no_inline_code.py` needs no edit: `PAGES` derives from `ROUTES` and `SCRIPTS`
globs `gui/**/*.js`, so the new page and script are covered the moment they exist.

## Documentation changes (builder tasks, not planner edits)

- **`CLAUDE.md`, Architecture, the `gui` component bullet:** add `/admin/index.js`
  to the page-scoped asset list, and update both counts ("16 exact routes" → 17,
  "11 page-scoped routes" → 12) — recount from `spin.toml` rather than trusting
  these numbers.
- **`CLAUDE.md`, `gui-pages` bullet / wherever the page allowlist is described:**
  note the hub page and its two `ROUTES` paths.
- **`DESIGN.md`, Navigation section**, three separate edits:
  - The first bullet's "conditionally shows a 'Manage users' link … and now
    additionally hides that link on the Manage Users page itself" becomes the
    "Admin" item pointing at the hub, hidden only on the hub itself.
  - **"The nav is full"** bullet: keep the entire measured history verbatim (it is
    the reason this design exists) and append what changed — the admin surface was
    consolidated from a page item to an area item, so item count held at five and
    width went down, with the newly measured numbers recorded.
  - **"In-body operator links are the sanctioned alternative to a nav item"**
    bullet: the "used twice … a third should trigger a rethink" trigger is
    resolved, not deferred. Record that the pair moved off the "Users" heading
    onto a uniform, rendered sibling strip present on all three admin pages, that
    it is always exactly the *other* tools, and that a fourth admin tool is now a
    hub card rather than a third middot.
- **`.impeccable/design.json`:** no change — no new token was introduced. State
  that in the task note rather than omitting it.
- **`PRODUCT.md`:** no change. No capability is added or removed; this is
  navigation only. Principle 5 already covers the intent.

## Trade-offs and rejected alternatives

**1. Add a nav item for each page (the literal request) — rejected on measured
evidence.** It is what was asked for, it is one line of markup per item, and it is
the shortest path from any page to either tool. It loses to a number:
`links/detail.html` at 768px measured `scrollWidth` 716 vs `clientWidth` 700 with
**one** extra item, and hiding that item returned it to exactly 700 — so the nav
has 0px of slack at that breakpoint, and this proposal asks for two items. The
only way to make it fit is widening the wrap escape hatch (today scoped to
`@media (max-width: 480px)`) to 768px, which `DESIGN.md` explicitly declined to do
in favour of the in-body fallback. Revisit only if the nav is genuinely
re-laid-out (e.g. the identity chip moves out of the nav), not as an insertion.

**2. An "Admin ▾" dropdown in the nav (Pico's `<details class="dropdown">`) —
rejected, and it was the closest call.** Genuinely attractive: one nav item, no
new page, no new route, no `ROUTES` entry, and it is the only option that reaches
Manage Users in one navigation instead of two. It loses on four specifics:
(a) `DESIGN.md` rejected an overflow menu on the grounds that it "would add a
second, hidden navigation model" — an admin menu is a *scoped* menu rather than a
spill-over, but it is still the app's first hidden navigation model, and the honest
reading of that note is that the objection was to the model, not to the trigger;
(b) native `<details>` does not close on outside click or Esc, so it needs bespoke
JS with focus handling — the app has no dropdown anywhere today, so this is a new
component, not a reuse; (c) it walks straight into two documented traps —
`#app-header nav li { color: #fff }` would paint the open panel's links white on a
light panel (`DESIGN.md`'s thrice-recorded specificity trap) and Pico's
`--pico-dropdown-box-shadow` must stay neutralised in both theme blocks under the
No-Shadow Rule; (d) the open panel is absolutely positioned inside a nav that
`flex-wrap`s at 480px, so it needs its own narrow-viewport measurement that the
hub page simply does not have. **Trigger to revisit:** operators complain about the
hub hop for Manage Users specifically, *and* someone is willing to pay for the
measurement protocol in (c)+(d). Note that the click count is identical either way
(open menu + choose vs. load hub + choose); the dropdown's real win is one fewer
page load, not one fewer decision.

**3. Do nothing (keep the two in-body links under "Users") — rejected.** It is
free and it works. It loses on both stated problems: the entry point is still a
page about accounts, and `DESIGN.md`'s own "a third should trigger a rethink" note
means the next admin tool forces this conversation anyway, with less room. Doing
nothing is choosing to have the same conversation later with a worse layout.

**4. A "Store maintenance · Destination URL policy" strip on the dashboard instead
of a hub — rejected.** Cheapest possible fix: no new page, no new route, no test
case, and it does technically make both tools reachable without touching Manage
Users. It loses because the dashboard is the app's densest page (create form,
filters, bulk action bar, links table) and the single page every non-admin lands
on; putting permission-gated admin chrome in its body inverts `PRODUCT.md`
principle 5's "keep admin visually and functionally distinct from everyday
link-creation workflows." It also does not scale: a fourth tool makes it a third
middot on the busiest page in the app.

**5. A sibling strip on the three admin pages, with no hub and no nav change —
rejected on its own, adopted as half the plan.** Lateral movement between admin
pages becomes one click, which is real. It does not solve the stated problem at
all: the *entry* into the admin area is still the nav's "Manage users" item, so
reaching Store maintenance still means landing on Manage Users first. It is kept
as a component of the chosen design precisely because the hub alone would make
every lateral move a two-hop trip through the hub.

**6. Naming the hub `/admin/home.html` instead of `/admin/index.html` — rejected,
narrowly.** It avoids a second file named `index.js` in the repo (`gui/index.js`
is the root redirect stub) and makes greps unambiguous. It loses because
`/admin/` is the URL an operator will actually type, and only an `index.html`
makes that path natural to serve; the two files live at unambiguous paths
(`/index.js` vs `/admin/index.js`) in a route table that is exact-match anyway.

**7. Hardcoding the three cards as static HTML in the hub, rather than rendering
from a shared `ADMIN_PAGES` — rejected.** Static markup keeps the copy in the HTML
file where it reads naturally and survives a JS failure. It loses to a drift
argument this repo has already been bitten by: `gui/admin/users.html` once
hardcoded four permission checkboxes and drifted out of sync with `users.js`'s
`ALL_PERMISSIONS` (the fix comment is still in the file at line 40-42). Two lists
of admin pages would drift the same way the moment a fourth tool lands. Every page
in this app is already non-functional without JS, so the fallback argument buys
nothing real.

## Tasks

The exact lines appended to `TASKS.md` under a new `## Admin hub navigation`
heading (TASKS.md is authoritative; the builder ticks boxes only there):

```
- [ ] Add the admin hub page and route it (no nav change yet) — file(s): gui/admin/index.html (new), gui/admin/index.js (new), gui/app.js, spin.toml, gui-pages/routing.py, gui-pages/tests/test_routing.py — done when: `gui/app.js` gains the `ADMIN_PAGES` constant (three entries, sibling-relative hrefs, one-sentence blurbs) described in docs/plans/admin-hub-navigation.md; `spin.toml` gains an **exact** `route = "/admin/index.js"` on the `gui` component; `ROUTES` gains both `"/admin/"` and `"/admin/index.html"` mapped to `admin/index.html` with matching `test_resolve_file` cases; the page carries no inline `<script>`, `<style>`, `style="` or `on<event>=` and loads `../theme-init.js`, `../vendor/pico.min.css`, `../theme.css`, `../app.js`, `index.js` with no page-scoped `.css`; `index.js` calls `initHeader({dashboardHref: "../dashboard.html", pageLabel: "Admin", adminHref: "index.html", onAdminHome: true})` and renders the three cards from `ADMIN_PAGES` only when `role === "admin" || permissions.includes("users.manage")`, otherwise unhiding `#forbidden-notice` and hiding `#admin-content`; and `cd gui-pages && uv run pytest` passes above its 108 baseline (verify the real number, do not assert a predicted one)
- [ ] Replace the "Manage users" nav item with a single "Admin" item and re-measure nav overflow (depends on the hub page task) — file(s): gui/app.js, gui/links/detail.js, gui/admin/users.js, gui/admin/store-maintenance.js, gui/admin/url-policy.js — done when: `initHeader`'s `manageUsersHref`/`onManageUsersPage` options are renamed to `adminHref` (default `"admin/index.html"`)/`onAdminHome`, `<li id="manage-users-link">` becomes `<li id="admin-link">` reading "Admin", the reveal condition is unchanged apart from `!onAdminHome`, no caller still passes `manageUsersHref` or `onManageUsersPage` (grep returns zero hits outside docs/plans and TASKS.md), `gui/dashboard.js` is not edited (it takes the defaults), and `scrollWidth` vs `clientWidth` on `#app-header nav` is measured and **recorded in the task note** at 1400/768/480/390px in **both** themes with two domains configured, on `links/detail.html` (the historical worst case, which measured 700/700 clean before) and on `admin/users.html` (the one page that gains an item), plus `admin/index.html` at 390px in both themes — every measurement showing zero overflow. If any breakpoint overflows, STOP and report: do not widen the 480px wrap fallback and do not add an overflow menu
- [ ] Add the sibling-tool strip to the three admin pages and delete users.html's "Users"-heading link pair (depends on the hub page task) — file(s): gui/app.js, gui/admin/users.html, gui/admin/users.js, gui/admin/store-maintenance.html, gui/admin/store-maintenance.js, gui/admin/url-policy.html, gui/admin/url-policy.js — done when: `gui/app.js` gains `renderAdminNav(container, currentId)` rendering every `ADMIN_PAGES` entry except `currentId` as `.operator-link` anchors joined by ` · ` after a "More admin tools: " lead-in; each of the three pages carries `<p id="admin-nav"></p>` as the first child of `#admin-content` and calls `renderAdminNav` with its own id right after `initHeader(...)` resolves; `gui/admin/users.html`'s `<p><a class="operator-link" href="store-maintenance.html">…</a> · <a class="operator-link" href="url-policy.html">…</a></p>` under the "Users" `<h2>` is deleted; no new CSS rule and no new design token is added; and `cd gui-pages && uv run pytest` still passes with no inline code
- [ ] Document the admin hub in CLAUDE.md and DESIGN.md (depends on every task above) — file(s): CLAUDE.md, DESIGN.md — done when: CLAUDE.md's `gui`-component bullet lists `/admin/index.js` and both route counts are recounted from `spin.toml` (16 → 17 exact routes, 11 → 12 page-scoped), the `gui-pages` page allowlist mentions `/admin/` and `/admin/index.html`; DESIGN.md's Navigation section (a) describes the nav's admin item as naming the admin area and hidden only on the hub, (b) keeps "The nav is full" bullet's measured history verbatim and appends that the admin surface was consolidated from a page item to an area item with the newly measured numbers, and (c) rewrites the "In-body operator links" bullet to record that the two-link pair moved off the "Users" heading onto a uniform sibling strip on all three admin pages, always naming the *other* tools, so a fourth admin tool is a hub card rather than a third middot; and the task note states explicitly that `.impeccable/design.json` needed no change because no token was introduced
- [ ] End-to-end manual verification of the admin hub — file(s): (none — verification step) — done when: every numbered step in docs/plans/admin-hub-navigation.md's Verification section is executed against a real `spin up --build --runtime-config-file runtime-config.toml` with two domains configured, in a browser with the console open and **zero errors of any kind, in particular zero CSP violations, in both light and dark themes** — including that the nav "Admin" item reaches the hub from the dashboard and from a link detail page, each hub card opens its tool, each tool page's strip names the other two and never itself, `/admin/` and `/admin/index.html` both serve the hub, and a signed-in user without `users.manage` sees no "Admin" nav item and gets only the forbidden notice on `/admin/index.html`
```

## Critical files

- `docs/plans/admin-hub-navigation.md` (new)
- `gui/admin/index.html` (new)
- `gui/admin/index.js` (new)
- `gui/app.js`
- `gui/links/detail.js`
- `gui/admin/users.html`
- `gui/admin/users.js`
- `gui/admin/store-maintenance.html`
- `gui/admin/store-maintenance.js`
- `gui/admin/url-policy.html`
- `gui/admin/url-policy.js`
- `spin.toml`
- `gui-pages/routing.py`
- `gui-pages/tests/test_routing.py`
- `CLAUDE.md`
- `DESIGN.md`
- `TASKS.md`

`gui/dashboard.js` is deliberately **not** in this list: it calls `initHeader()`
with no options and picks up the new default. `gui/theme.css` is not in it either
— this change adds no CSS rule. `api/` and `redirect/` are untouched.

## Verification

1. `cd gui-pages && uv run pytest` — baseline is **108 passed** (measured
   2026-08-27). Expect an increase: one new page value in `PAGES` (4 parametrized
   checks), one new script in `SCRIPTS` (2 checks), two new `test_resolve_file`
   cases. **Verify the real number; do not assert the predicted one.**
2. `cd api && uv run pytest` and `cd redirect && go test ./linkgate/...` are **not
   listed as part of this change's verification** — no file under `api/` or
   `redirect/` is modified. CI (`Jenkinsfile`) runs them regardless; the
   `Jenkinsfile` itself needs no edit, since no test command changes.
3. Start the app with two domains, so the nav is in its widest configuration:

   ```bash
   SPIN_VARIABLE_PUBLIC_BASE_URLS="http://localhost:3000,http://127.0.0.1:3000" \
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> \
   SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```

   Pass: the startup "Available Routes" list includes `/admin/index.js` on the
   `gui` component.
4. `curl -si localhost:3000/admin/index.html | head -1` → `200`;
   `curl -si localhost:3000/admin/ | head -1` → `200`;
   `curl -si localhost:3000/admin/nope.html | head -1` → `404` (and the body is
   the styled `gui-pages` 404, not a bare string).
5. Sign in as the bootstrap admin. Pass: the nav's fourth account-group item reads
   **Admin**, not "Manage users". Click it → the hub renders three cards, each with
   a heading link and a one-sentence blurb, and the nav's "Admin" item is **hidden
   on this page only**.
6. From the hub, open each of the three tools in turn. Pass: each loads; each shows
   `More admin tools:` naming exactly the **other two** and never itself; the nav's
   "Admin" item is visible on all three and returns to the hub.
7. On `admin/users.html`, confirm the old
   `Store maintenance · Destination URL policy` line under the **"Users"** `<h2>`
   is gone, and that the surviving strip sits at the top of `#admin-content`.
8. Reach Store maintenance from a cold start without passing through Manage Users:
   dashboard → Admin → Store maintenance. Pass: two navigations, neither of them
   the users page. This is the requirement the whole plan exists for.
9. **Nav overflow measurement**, the protocol `DESIGN.md` records for every nav
   change. For each of `links/detail.html` (historical worst case, measured
   700/700 clean before this change) and `admin/users.html` (the one page gaining
   an item), at 1400 / 768 / 480 / 390 px, in **both** themes, read
   `document.querySelector('#app-header nav').scrollWidth` and `.clientWidth`.
   Pass: `scrollWidth === clientWidth` at all 16. Then measure
   `admin/index.html` at 390px in both themes (it cannot be the worst case — its
   breadcrumb is 5 characters and its admin item is hidden — so two readings
   suffice as a sanity check). **Record every number in the TASKS.md task note.**
   If any reading overflows, stop and report rather than widening the 480px wrap
   fallback or adding a menu.
10. Console check: with devtools open, visit dashboard, hub, all three admin
    pages, and a link detail page, in both themes. Pass: **zero** console errors
    and zero CSP violations.
11. Permission check: create a user with no permissions (or reuse one), sign in as
    them. Pass: no "Admin" item in the nav; hand-navigating to
    `/admin/index.html` shows only the forbidden notice with no cards rendered;
    the same holds on all three tool pages, unchanged from today.

## Out of scope / follow-ups

- **The dropdown.** Not built, and its trigger is written into "Considered and
  rejected": a complaint about the extra hop to Manage Users *plus* someone paying
  for the specificity/shadow/narrow-viewport measurements it needs. Filed under
  Future work.
- **A fourth admin tool.** The point of the hub is that one is now a single
  `ADMIN_PAGES` entry plus a page — no nav measurement, no third middot. Nothing
  to do until one exists.
- **`admin/users.js`'s missing client-side permission check.** It gates from the
  `GET /api/users` failure path instead of from `/auth/me`, unlike the other three
  admin pages. Harmless (the server is authoritative either way) and pre-existing;
  harmonising it is a separate, unrelated cleanup. Filed under Future work.
- **Restructuring the nav itself** (moving the identity chip out, a two-row
  header, a hamburger at narrow widths). This plan deliberately spends *less* nav
  width rather than re-laying it out. If the nav ever needs a genuine fifth item
  again, that is the redesign to have, and `DESIGN.md`'s measured history is the
  brief for it.
- **A `pageLabel` for the hub longer than "Admin".** Kept to one word deliberately
  so the nav item, the breadcrumb, the `<h1>` and the `<title>` suffix all read
  identically; there is no second name to learn.
