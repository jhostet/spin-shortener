---
name: spin-shortener
description: A dark-navy, data-dense admin console for a self-hosted URL shortener
colors:
  signal-blue: "#276fb8"
  signal-blue-hover: "#1e6bb8"
  navy-950: "#0a1628"
  navy-800: "#14243d"
  slate-muted: "#5b6b85"
  border-mist: "#d6deea"
  bg-mist: "#eef1f6"
  surface-white: "#ffffff"
  ok-green: "#1a7f5a"
  danger-red: "#b3261e"
  inherited-error-red: "rgb(136, 56.5, 53)"
typography:
  display:
    fontFamily: "system-ui, \"Segoe UI\", Roboto, Helvetica, Arial, sans-serif"
    fontSize: "1.5rem"
    fontWeight: 700
    lineHeight: 1.4
  headline:
    fontFamily: "system-ui, \"Segoe UI\", Roboto, Helvetica, Arial, sans-serif"
    fontSize: "1.25rem"
    fontWeight: 700
    lineHeight: 1.4
  title:
    fontFamily: "system-ui, \"Segoe UI\", Roboto, Helvetica, Arial, sans-serif"
    fontSize: "1.1rem"
    fontWeight: 700
    lineHeight: 1.4
  body:
    fontFamily: "system-ui, \"Segoe UI\", Roboto, Helvetica, Arial, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.4
  label:
    fontFamily: "\"SFMono-Regular\", \"JetBrains Mono\", Menlo, Consolas, monospace"
    fontSize: "0.85em"
    fontWeight: 400
    lineHeight: 1.6
  caption:
    fontFamily: "system-ui, \"Segoe UI\", Roboto, Helvetica, Arial, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 600
    lineHeight: 1.4
rounded:
  sm: "0.25rem"
  pill: "999px"
spacing:
  base: "0.75rem"
  field-vertical: "0.5rem"
  field-horizontal: "0.75rem"
components:
  button-primary:
    backgroundColor: "{colors.signal-blue}"
    textColor: "#ffffff"
    rounded: "{rounded.sm}"
    padding: "{spacing.field-vertical} {spacing.field-horizontal}"
  button-primary-hover:
    backgroundColor: "{colors.signal-blue-hover}"
  chip-slug:
    backgroundColor: "{colors.navy-950}"
    textColor: "#ffffff"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "0.15em 0.6em"
  chip-slug-hover:
    backgroundColor: "{colors.navy-800}"
    textColor: "{colors.signal-blue}"
  badge-status-active:
    textColor: "{colors.ok-green}"
  badge-status-disabled:
    textColor: "{colors.danger-red}"
  nav-shell:
    backgroundColor: "{colors.navy-950}"
    textColor: "#ffffff"
    rounded: "{rounded.sm}"
    padding: "0.75rem 1rem"
---

# Design System: spin-shortener

## Overview

**Creative North Star: "The Signal Room"**

A quiet, dark-navy control room where every accent-blue "signal" marks a link actively routing traffic. The system already names its own accent `signal-500` in code — the metaphor isn't imposed, it's read off what's there. This is an internal operations console for a marketing team creating and tracking campaign links, not a marketing surface itself: density and scanability outrank expression, and the one confirmed brand instruction so far is "dark theme preferred" over any specific identity. Nothing here is officially locked — treat the palette and component choices below as the incumbent system to preserve during refinement, not an untouchable brand.

**Key Characteristics:**
- Dark-navy chrome (persistent nav, slug chips) against a light, cool-gray content surface — dark accents on a light working canvas, not a fully dark UI
- Flat and bordered, never shadowed — depth comes from a `border-mist` hairline and background contrast, not elevation
- Monospace reserved strictly for data (slugs, URLs, timestamps), never as decoration
- A pinned, non-scaling type/spacing scale — deliberately rejects Pico's default prose-scaling behavior in favor of admin density

## Colors

A restrained palette: one accent, one dark neutral used two ways (chrome + text), one light neutral family for surfaces, and two functional status colors.

### Primary
- **Signal Blue** (`#276fb8`): the one accent. Links, primary buttons, the persistent nav's hover state, sortable-column hover. Used sparingly — outside the nav and buttons it appears only as a hover accent, never a resting fill. (Originally `#2b7fd1`; darkened to clear 4.5:1 AA text contrast — the original measured ~4.15:1 against white on both button fills and plain link text.)
- **Signal Blue, Hover** (`#1e6bb8`): darkens on `:hover`/`:active` for every primary-accent element.

### Neutral
- **Deep Navy** (`#0a1628`): the darkest neutral. Doubles as the persistent nav/header background *and* every heading's text color (`h1`–`h3`) — the same dark value grounds both the chrome and the content hierarchy.
- **Slate Navy** (`#14243d`): body text color, and the slug chip's hover background (one step lighter than Deep Navy so hover reads as "lifting," not just recoloring).
- **Slate Muted** (`#5b6b85`, token `--pico-muted-color`): muted/secondary text and doubles as the hairline border color's source hue (see Border Mist) — passes 4.5:1 on the white card surface. **A distinct, separate token** (`--ss-slate-500`, `#526078`) covers the slug-kind/lock badges and the "not yet live" annotation specifically — these render on table cells (`--pico-background-color`, the mist background, not the white card), where the original shared `#5b6b85` value cleared AA by only 0.27:1; darkened for real margin against future palette drift, the same "measured against the wrong assumed background" gotcha that caused the ok-green status-badge fix. The two tokens coincidentally shared one hex value before this fix; they no longer do, and aren't meant to track each other going forward.
- **Border Mist** (`#d6deea`): the one border color, used identically for card borders and the muted-text-adjacent divider role.
- **Background Mist** (`#eef1f6`): the page background — a cool, faint blue-gray, never pure white, so white cards read as distinct surfaces.
- **Surface White** (`#ffffff`): card/article backgrounds.

### Named Rules
**The Chrome-Equals-Heading Rule.** Deep Navy (`#0a1628`) is the single dark value used for both the nav bar's fill and every heading's text. There is no separate "chrome color" — the darkest neutral just gets reused for both jobs.

## Typography

**Display/Body Font:** `system-ui` stack (Segoe UI, Roboto, Helvetica, Arial, sans-serif) — Pico's default, unchanged.
**Label/Mono Font:** `SFMono-Regular, "JetBrains Mono", Menlo, Consolas, monospace` — reserved for data, never prose.

**Character:** Plain system UI type at a pinned, non-scaling size — the point is legibility and density for a task-completion tool, not a typographic identity.

### Hierarchy
- **Display** (700, 1.5rem, 1.4): page-level `h1` — used sparingly; `dashboard.html` is the one page that skips it entirely, relying on the persistent nav's brand name instead (`login.html`, `admin/users.html`, and `links/detail.html` all have a real `h1`).
- **Headline** (700, 1.25rem, 1.4): `h2` — section headings within a page ("Create a new link", "Your links").
- **Title** (700, 1.1rem, 1.4): `h3` — sub-section headings (e.g. "Clicks per day" / "Recent events" side by side).
- **Body** (400, 1rem, 1.4): all prose, labels, and table cells that aren't data columns.
- **Label** (400, 0.85em, 1.6, monospace): the slug chip and every data-like table column (slug, destination, timestamps) — set apart from the sans-serif prose around it.
- **Caption** (600, 0.75rem, 1.4, sans-serif): compact functional text that isn't data and isn't prose — the links table's tightened action-button group, the slug-kind/lock badges next to a slug chip, and the nav's identity-chip role text/avatar-letter. Added to reconcile two ad-hoc small sizes (`0.8rem`, `0.7rem`) that had drifted outside any documented step; reused rather than re-drifted when the identity chip introduced its own one-off `0.8rem`/`0.9rem`/`0.7rem` sizes — caught live by the mechanical detector's `design-system-font-size` advisory.

### Named Rules
**The Data-Is-Mono Rule.** Monospace is used for exactly one purpose: columns and chips that carry a slug, URL, or timestamp. It never appears as a "technical-looking" costume elsewhere.

## Layout

Built on Pico's responsive `.container`, but with its default viewport-scaling font-size pinned flat (`100%` at every breakpoint) — this is a dense table-and-form admin tool, not prose content, so type/spacing don't grow with screen width.

- **Spacing scale:** base spacing `0.75rem` (Pico default is `1rem`, tightened); form fields use `0.5rem`/`0.75rem` vertical/horizontal padding.
- **Grid:** Pico's built-in equal-width `.grid` for logically-paired fields (custom slug + start/end dates on link creation; role + password on user edit).
- **Persistent app shell:** every authenticated page renders the same `#app-header nav` — Deep Navy fill, white text, contained to the page's `.container` width, `margin-top: 1rem` separating it from the viewport edge.
- **Data tables:** every table lives inside a `<figure>` with `overflow-x: auto`, so wide tables scroll horizontally on narrow viewports instead of breaking layout — applied uniformly across the links, users, and both analytics tables. The scrollbar itself is styled (thin, Signal Blue thumb on a `border-mist` track) rather than left to the browser default, since a default OS scrollbar (especially macOS's auto-hiding overlay style) gives no persistent hint that a table scrolls — deliberately not a shadow/gradient edge-fade, which would violate the No-Shadow Rule below.
- **Sticky action column:** the row-action column (View/Copy/Edit/Delete on the links table; Edit/Delete on the users table) is `position: sticky; right: 0` within its scrolling `<figure>`, with a `border-mist` left border and a solid `surface-white` background so scrolled content doesn't show through. Every other column (Destination, Permissions, etc.) can grow without limit and push the table wider than the viewport, but the row's actions stay reachable at the visible right edge regardless of scroll position — confirmed at both realistic desktop widths and narrow mobile viewports. Scoped to non-edit rows (`tbody > tr:not(.edit-row) td:last-child`) since the edit-row's own `<td>` is a `colspan` cell and is also `:last-child` of its `<tr>`.
- **Mobile column collapse (below 600px):** a sticky column being reachable isn't the same as nothing being hidden underneath it — a sticky element paints on top of whatever's at its screen position, regardless of that content's own position in the table's normal flow. Below 600px, both tables hide their least-essential columns outright (links: Owner, Destination, Created, Starts; users: Permissions) rather than relying on scrolling to reveal them, and the column immediately before the sticky one gets an explicit `max-width` capping its own rendered width so wrapped content can never physically reach the sticky column's screen position. **Only `max-width` on the column itself works for this** — `padding-right` doesn't create real clearance from a fixed sticky neighbor (it adds space after left-aligned content without moving the content, and combined with `text-align: right` the padding terms algebraically cancel once the cell's own width is also padding-driven in an auto-layout table); both were tried and measured to have zero effect live before `max-width` was confirmed to actually work.
- **The `max-width` cap must include the `<th>`, not just the `<td>`, *and* every column before it:** an HTML table sizes a column to its widest cell across every row, header included. Capping only the `<td>` left the header's own natural width (sort arrow + label text) in control on the links table's Expires column, rendering 5px over the intended cap. Separately, a single long custom slug *anywhere* in the table (not necessarily the row being checked) widens the Short-link column for every row, which pushes every later column rightward and can silently defeat a downstream column's clearance cap even though that cap's own value never changed — confirmed live: a table containing both a short slug and a long custom slug broke a cap that measured correctly in isolation. The Short-link column needs its own `max-width` for the same reason the Expires/Status columns do.
- **A `[role=group]` wrapper is required for the flex-based fixes (flex-wrap, gap, max-width) to apply at all** — the users table's plain Edit/Delete buttons were never wrapped in one (only the edit-row's Save/Cancel were), so an earlier gap/flex-wrap rule targeting `[role=group]` silently matched nothing there; the buttons stacked via ordinary inline-block wrapping with zero gap regardless. Wrapping them in `<div role="group">`, matching the convention already used everywhere else (dashboard.html's row actions, this same page's edit-row), is what made the fix real — plus the same `width: auto` override every other `[role=group]` here needs (Pico's default `width: 100%` would otherwise balloon it to the sticky column's full width).
- **Auth page:** the one page that breaks from the app-shell pattern — a single narrow (`26rem`) card, vertically centered via a flex body (`body.auth-page`) rather than sharing the persistent nav.

## Elevation & Depth

Flat by design — Pico's default button `box-shadow` is `0 0 0 rgba(0,0,0,0)` (no shadow at all). Depth is conveyed entirely by the `border-mist` hairline border plus the `surface-white` card against the `bg-mist` page background, not by elevation.

### Named Rules
**The No-Shadow Rule.** Nothing in this system casts a shadow. Separation between surfaces is border + background-contrast only. **Card elevation is the one place this was silently violated for the theme's entire history**: Pico's `--pico-card-box-shadow` (and `--pico-dropdown-box-shadow`) default to a real multi-layer shadow, and `theme.css` overrode every other `--pico-card-*` property except that one — so every `<article>` on every page rendered Pico's stock drop shadow from the theme's first commit until an 8th-pass critique caught it via `getComputedStyle`. Both variables are now explicitly set to `none` in `theme.css`'s `:root:not([data-theme="dark"])` block.

## Shapes

Modest, consistent corner rounding (`0.25rem`, Pico's default) on every card, button, and the nav bar — with exactly one deliberate exception.

### Named Rules
**The Pill-Is-For-Links Rule.** Full pill rounding (`999px`) is reserved for the slug chip — the product's signature "this is a shortened link" marker. No other component uses it; introducing a second pill-shaped element would dilute what the pill means.

## Components

### Buttons
- **Shape:** `0.25rem` radius (Pico default), unchanged by the theme.
- **Minimum tap target:** `min-height: 44px` on every button/submit/reset/`[role=button]`, sitewide — an audit measured every button at 40px, under the commonly-cited 44px touch-target guideline, on a system that has real mobile-specific layout (column collapse, sticky action column, stacked row actions). Implemented as a height floor rather than restoring `--pico-form-element-spacing-vertical` toward Pico's default, since that variable also drives every text input's tap target and reducing the tightening would undo this app's deliberate admin-density type scale.
- **Primary:** Signal Blue fill, white text, darkens to Signal-Blue-Hover on hover/active.
- **Secondary / Outline:** row-level actions (View/Copy/Edit) use the `outline` variant; the one destructive action (Delete) additionally gets `.secondary` so it visually recedes rather than draws the eye — de-emphasis, not warning color, is how "destructive" reads here.
- **Accepted convention:** the "View" row action is a real `<a href>` styled as a button via Pico's `[role=button]` selector (Pico's own idiom for making an anchor look like a button — it's load-bearing for the visual treatment, not just a label). This is a known, minor ARIA-semantics tradeoff (a "link" gets a "button" role) rather than a bug; removing `role="button"` without replicating Pico's full styling surface for that selector would be a larger, riskier change than the tradeoff justifies.

### Chips
- **Slug chip** (signature component): monospace pill, Deep Navy background, white text; hover shifts background to Slate Navy and text to Signal Blue. Appears everywhere a short link is shown — the dashboard table, and as the `h1` on the link-detail page. **Content differs by context, not just styling:** the detail page's `h1` shows the full `origin/r/slug` URL (a one-off "here's your link" moment worth spelling out completely), while the links table shows only `/r/slug` with the full URL in a `title` attribute — the origin is identical on every row, so repeating it 30 times per page was pure width cost for zero information gain, and was the single biggest contributor to the table overflowing its own container even at a realistic desktop width with a small dataset. The table's chip additionally gets `white-space: nowrap; text-overflow: ellipsis` past a fixed width — defensive against a custom slug (up to 32 chars) wrapping the pill onto multiple lines, which would visibly deform the app's one signature pill shape (see the Pill-Is-For-Links Rule). Scoped to just the table, never the detail page's `h1`, where showing the complete slug is the point.

### Status Badges
- **Style:** plain colored text, no background/pill, `font-weight: 600`, value capitalized.
- **Active:** `ok-green` (`#177251`). **Disabled:** `danger-red` (`#b3261e`). Color is never the only signal — the text itself already reads "active"/"disabled". (`ok-green` was `#1a7f5a` until an audit measured it at 4.39:1 against the table cell's real background — `--pico-background-color`, since Pico paints `td`/`th` with that token, not the white card behind it — just under the 4.5:1 AA minimum for the single most common status label in the app. `#177251` clears 5.2:1 at the same hue, the same fix pattern already used for `signal-blue`.)

### Focus Indicators
- **Style:** a full-opacity `2px solid signal-blue` outline, sitewide, on every focusable element. Pico's own default focus ring is a low-opacity `rgba` box-shadow that measures ~1.71:1 against a white card — well under WCAG 1.4.11's 3:1 non-text-contrast minimum — and was originally fixed for the persistent nav alone (its dark-navy fill made the default ring nearly invisible). An audit later found the identical unfixed problem on every other focusable element sitewide, including the create-link form's own "More options" disclosure — generalized to one sitewide `:focus-visible` rule rather than fixing each surface individually as it's noticed.
- **A `<summary>`-specific gotcha:** Pico defines its own higher-specificity `details summary:focus-visible:not([role])` rule that silently wins over a plain `:focus-visible` override (confirmed live: the generic rule alone left the "More options" toggle showing Pico's low-opacity default). Matching Pico's exact selector shape (rather than trying to out-specificity it some other way) makes it an equal-specificity tie, which load order then breaks in this file's favor — the same trick already used for the `:root` color-token fix.

### Cards / Containers
- **Corner Style:** `0.25rem`.
- **Background:** `surface-white` on `bg-mist`.
- **Shadow Strategy:** none (see Elevation & Depth).
- **Border:** `border-mist` hairline, Pico default width.

### Inputs / Fields
- **Style:** Pico defaults, unmodified — bordered, `border-mist`-toned, `0.25rem` radius.
- **Light-only, deliberately:** `theme.css` explicitly overrides 8 of Pico's `--pico-form-element-*` variables (background/border/color/placeholder, plus active-background, both invalid-border variants, and selected-background), unconditionally and outside any media query. Without this, every text input on every page (including the login screen) silently inherited whichever of Pico's separate `@media (prefers-color-scheme: dark)` form-element values applied, on any OS/browser set to prefer dark colors, with no `data-theme` attribute ever set anywhere in the app to opt back out. **A 2-round fix, not 1**: the first round covered 4 variables (background/border/color/placeholder) and fixed the *resting* state; a later audit found the *focused/active* state still leaked, because Pico applies a 5th, separate variable (`--pico-form-element-active-background-color`) only on `:active`/`:focus` — that round then swept the entire `--pico-form-element-*` family in `vendor/pico.min.css` for every other variable with a distinct dark-mode value not already covered indirectly through an overridden `--pico-primary*`/`--ss-*` token, closing the remaining 3 gaps (the two invalid-border variants, the selected-option background) in the same pass rather than leaving them for a 3rd round to find. **`color-scheme: light` is not what makes this fix work** — that property only hints the browser's native chrome (scrollbars, autofill styling) toward light rendering; it has no effect on which CSS rules match `prefers-color-scheme`, a common misconception an earlier version of this very note repeated. The actual mechanism is simply that every value here is declared unconditionally, so it always wins regardless of OS preference.
- **Error:** a shared `.form-error` class (bold, `margin-top: 0.5rem`, colored with `danger-red`) — used consistently for every form/page-level error message across all four pages. Previously read Pico's inherited `--pico-del-color` instead, a visually-close but distinct red; unified onto `danger-red` so status badges and form errors share exactly one error color.
- **Success:** `.form-success` mirrors `.form-error`'s weight/spacing but in `ok-green`, laid out as a flex row so it can hold a slug chip plus a small "Copy" affordance inline. Introduced for the link-creation flow so the moment a link is made has a visible payoff instead of the form silently clearing; the user-creation flow (`admin/users.html`) reuses the same class for the same reason.
- **Progressive disclosure:** occasional-use fields for a primary "paste and submit" action collapse behind a native `<details>`/`<summary>` (Pico's own styled accordion — chevron marker, no bespoke JS toggle needed), open state not persisted across submissions. The link-creation form is the one example today: Destination URL renders unconditionally, while custom slug, schedule (Starts/Expires), and password protection collapse behind a "More options" disclosure that resets to closed after each successful create.

### Navigation
- Deep Navy fill, white text/links, hover reveals Signal Blue. Persistent across every authenticated page; conditionally shows a "Manage users" link only for admins/permitted roles, and now additionally hides that link on the Manage Users page itself — showing a link to the page currently being viewed served no purpose and was flagged as confusing. The one non-nav page (auth) has no chrome at all.
- **Brand mark, persistent everywhere:** the wordmark (with a small link-glyph icon, in Signal Blue — the app's first hand-authored SVG icon, not a decorative flourish; it reinforces "The Signal Room" north star, where every accent-blue signal marks a link) is always present and always a clickable link to the dashboard, on every authenticated page. Previously it was displaced by a generic "← Back to dashboard" link on every page except the dashboard itself — the one thing a user should be able to orient by regardless of page was the one thing that changed depending on the page, a real navigation-confusion bug, not just a stylistic complaint, confirmed via direct comparison across pages.
- **Breadcrumb page label, not a brand swap:** a page identifies itself via a `/ Page Name` suffix next to the permanent brand (e.g. "spin-shortener / Manage users"), rather than by displacing it. This also gives `links/detail.html` a page identifier it previously had none at all — no `<h1>`, and formerly no brand name either, making it the least-oriented page in the app before this fix.
- **Identity chip, not parenthetical text:** the account area was `username (role)` as plain unstyled text; it's now a real component — a small initial-letter avatar badge (`0.25rem` corner radius, matching every card/button — deliberately not full pill rounding, which the Pill-Is-For-Links Rule reserves for the slug chip alone) plus a name/role stack, with role reusing the existing Caption type step (see Typography) rather than a parenthetical afterthought.
- **Mobile nav wrapping:** below 480px the breadcrumb label and the identity chip's role line are shed first (the brand mark and username are what actually matter for orientation on a phone), and the nav wraps at the `<nav>` level — the two `<ul>` groups drop to their own row as intact units when they don't fit side by side. Wrapping inside each `<ul>` instead was tried first and rejected: `<nav>`'s default `align-items: stretch` stretched the (shorter) brand group to match the taller wrapped account group, vertically centering the wordmark inside empty space instead of sitting flush at the top.

### Empty States
- A single centered, muted-color (`slate-muted`) row spanning the table's full column count — used identically across the links, users, and both analytics tables whenever a list has nothing to show. The links table additionally distinguishes "nothing exists yet" from "nothing matches the current filter" with different copy.

### Confirmation Dialogs
- A shared `confirmDialog(message)` helper (`app.js`) replaces the browser's native `confirm()` for both destructive actions in the app (link delete, user delete) with Pico's own `<dialog>`/`<article>` component instead of an unthemed OS popup. Built dynamically (no markup duplicated per page); resolves a promise, dismissible via its own Cancel button, the Esc key, or a backdrop click.
- **Button styling inverts the row-level convention on purpose:** Cancel renders as Pico's plain default (primary) button, while the destructive action stays `outline secondary` — the same de-emphasis the row-level Delete button already uses (see Buttons above). The visually prominent button in the dialog is the *safe* one, not the destructive one.
- **Narrowed and centered, not Pico's wide form-style default:** Pico's own `dialog>article` defaults to a wide box with a left-aligned message and a right-aligned footer (`text-align: right`) — fine for a form-like dialog, but for a short one-line Yes/No message it read as lopsided, with the text hugging one edge and the buttons the other. The `.confirm-dialog` class caps the article at `26rem` and centers both the message and the button row, giving the compact, symmetrical shape this content calls for.

## Do's and Don'ts

### Do:
- **Do** keep `--pico-font-size` pinned at `100%` — this app rejects Pico's default viewport-scaling type, deliberately, because it's a dense admin tool.
- **Do** reserve the `999px` pill radius for the slug chip only.
- **Do** reserve the monospace font for slug/URL/timestamp data, never as a "technical" decorative costume.
- **Do** route every form/page error through `.form-error` rather than a one-off inline style.
- **Do** give a real completed action visible payoff (see `.form-success`) instead of a silent form-clear — apply this to every flow that completes an action, not just the one that first motivated the rule.
- **Do** keep a table's row-action column reachable via a sticky column (`position: sticky; right: 0`, solid background, hairline border), not by capping other columns' content or leaving actions to scroll out of reach.
- **Do** collapse occasional-use fields behind a native `<details>` disclosure when a form's primary action is a single common field (e.g. paste-URL-and-submit) — matches the platform's own accordion idiom instead of a bespoke show/hide toggle.
- **Do** sweep an entire Pico CSS-variable family (grep `vendor/pico.min.css` for the shared prefix) whenever fixing a leak in one member of it, rather than patching only the specifically-reported variable — this exact bug class (a Pico dark-mode variable leaking through because `theme.css` never overrode it) has recurred twice: once for the resting-state form-element variables, once for the focused/active-state sibling Pico defines separately.

### Don't:
- **Don't** add a `box-shadow` anywhere — the system is flat by construction, not by oversight.
- **Don't** introduce a second error/status red — `danger-red` (`#b3261e`) is now the one value used by both status badges and form errors (previously form errors read Pico's inherited `--pico-del-color` instead, a visually-close but distinct value; unified so there's exactly one).
- **Don't** let a page-level heading imply every page needs one — several pages intentionally rely on the persistent nav's brand name instead of a redundant `h1`.
- **Don't** declare theme color-variable overrides at plain `:root` — `theme.css`'s own `:root:not([data-theme="dark"])` selector exists precisely because Pico defines `--pico-primary*`, `--pico-background-color`, `--pico-color`, `--pico-h1/h2/h3-color`, `--pico-muted-color`/`-border-color`, and `--pico-card-*-color` at that same higher-specificity selector — a plain `:root` override silently loses the cascade for all of them regardless of load order. Confirmed via a live audit that this was broken since the theme's first commit (rendering Pico's stock azure/white/gray instead of the intended navy palette) with no visible symptom, since the coincidental fallback colors still happened to pass contrast.
- **Don't** assume `color-scheme: light` blocks `prefers-color-scheme: dark` media queries from matching — it doesn't; it only hints the browser's native chrome (scrollbars, autofill) toward light rendering. The only real defense against a vendor stylesheet's dark-mode block is overriding every value it sets, unconditionally, outside any media query.
