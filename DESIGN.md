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
- **Slate Muted** (`#5b6b85`): muted/secondary text and doubles as the hairline border color's source hue (see Border Mist) — passes 4.5:1 on the white card surface.
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
- **Caption** (600, 0.75rem, 1.4, sans-serif): compact functional text that isn't data and isn't prose — the links table's tightened action-button group, and the slug-kind/lock badges next to a slug chip. Added to reconcile two ad-hoc small sizes (`0.8rem`, `0.7rem`) that had drifted outside any documented step.

### Named Rules
**The Data-Is-Mono Rule.** Monospace is used for exactly one purpose: columns and chips that carry a slug, URL, or timestamp. It never appears as a "technical-looking" costume elsewhere.

## Layout

Built on Pico's responsive `.container`, but with its default viewport-scaling font-size pinned flat (`100%` at every breakpoint) — this is a dense table-and-form admin tool, not prose content, so type/spacing don't grow with screen width.

- **Spacing scale:** base spacing `0.75rem` (Pico default is `1rem`, tightened); form fields use `0.5rem`/`0.75rem` vertical/horizontal padding.
- **Grid:** Pico's built-in equal-width `.grid` for logically-paired fields (custom slug + start/end dates on link creation; role + password on user edit).
- **Persistent app shell:** every authenticated page renders the same `#app-header nav` — Deep Navy fill, white text, contained to the page's `.container` width, `margin-top: 1rem` separating it from the viewport edge.
- **Data tables:** every table lives inside a `<figure>` with `overflow-x: auto`, so wide tables scroll horizontally on narrow viewports instead of breaking layout — applied uniformly across the links, users, and both analytics tables.
- **Auth page:** the one page that breaks from the app-shell pattern — a single narrow (`26rem`) card, vertically centered via a flex body (`body.auth-page`) rather than sharing the persistent nav.

## Elevation & Depth

Flat by design — Pico's default button/card `box-shadow` is `0 0 0 rgba(0,0,0,0)` (no shadow at all), and the theme never overrides it. Depth is conveyed entirely by the `border-mist` hairline border plus the `surface-white` card against the `bg-mist` page background, not by elevation.

### Named Rules
**The No-Shadow Rule.** Nothing in this system casts a shadow. Separation between surfaces is border + background-contrast only.

## Shapes

Modest, consistent corner rounding (`0.25rem`, Pico's default) on every card, button, and the nav bar — with exactly one deliberate exception.

### Named Rules
**The Pill-Is-For-Links Rule.** Full pill rounding (`999px`) is reserved for the slug chip — the product's signature "this is a shortened link" marker. No other component uses it; introducing a second pill-shaped element would dilute what the pill means.

## Components

### Buttons
- **Shape:** `0.25rem` radius (Pico default), unchanged by the theme.
- **Primary:** Signal Blue fill, white text, darkens to Signal-Blue-Hover on hover/active.
- **Secondary / Outline:** row-level actions (View/Copy/Edit) use the `outline` variant; the one destructive action (Delete) additionally gets `.secondary` so it visually recedes rather than draws the eye — de-emphasis, not warning color, is how "destructive" reads here.
- **Accepted convention:** the "View" row action is a real `<a href>` styled as a button via Pico's `[role=button]` selector (Pico's own idiom for making an anchor look like a button — it's load-bearing for the visual treatment, not just a label). This is a known, minor ARIA-semantics tradeoff (a "link" gets a "button" role) rather than a bug; removing `role="button"` without replicating Pico's full styling surface for that selector would be a larger, riskier change than the tradeoff justifies.

### Chips
- **Slug chip** (signature component): monospace pill, Deep Navy background, white text; hover shifts background to Slate Navy and text to Signal Blue. Appears everywhere a short link is shown — the dashboard table, and as the `h1` on the link-detail page.

### Status Badges
- **Style:** plain colored text, no background/pill, `font-weight: 600`, value capitalized.
- **Active:** `ok-green` (`#1a7f5a`). **Disabled:** `danger-red` (`#b3261e`). Color is never the only signal — the text itself already reads "active"/"disabled".

### Cards / Containers
- **Corner Style:** `0.25rem`.
- **Background:** `surface-white` on `bg-mist`.
- **Shadow Strategy:** none (see Elevation & Depth).
- **Border:** `border-mist` hairline, Pico default width.

### Inputs / Fields
- **Style:** Pico defaults, unmodified — bordered, `border-mist`-toned, `0.25rem` radius.
- **Error:** a shared `.form-error` class (bold, `margin-top: 0.5rem`, colored with Pico's inherited `--pico-del-color`) — used consistently for every form/page-level error message across all four pages.
- **Success:** `.form-success` mirrors `.form-error`'s weight/spacing but in `ok-green`, laid out as a flex row so it can hold a slug chip plus a small "Copy" affordance inline. Introduced for the link-creation flow so the moment a link is made has a visible payoff instead of the form silently clearing.

### Navigation
- Deep Navy fill, white text/links, hover reveals Signal Blue. Persistent across every authenticated page; conditionally shows a "Manage users" link only for admins/permitted roles. The one non-nav page (auth) has no chrome at all.

### Empty States
- A single centered, muted-color (`slate-muted`) row spanning the table's full column count — used identically across the links, users, and both analytics tables whenever a list has nothing to show. The links table additionally distinguishes "nothing exists yet" from "nothing matches the current filter" with different copy.

## Do's and Don'ts

### Do:
- **Do** keep `--pico-font-size` pinned at `100%` — this app rejects Pico's default viewport-scaling type, deliberately, because it's a dense admin tool.
- **Do** reserve the `999px` pill radius for the slug chip only.
- **Do** reserve the monospace font for slug/URL/timestamp data, never as a "technical" decorative costume.
- **Do** route every form/page error through `.form-error` rather than a one-off inline style.
- **Do** give a real completed action visible payoff (see `.form-success`) instead of a silent form-clear.

### Don't:
- **Don't** add a `box-shadow` anywhere — the system is flat by construction, not by oversight.
- **Don't** introduce a third status/error color without reconciling the two that already exist: `danger-red` (`#b3261e`, status badges) and Pico's inherited `--pico-del-color` (`rgb(136, 56.5, 53)`, form errors) are visually close but not the same value — a real, minor inconsistency worth unifying in a future pass, not papering over with a third red.
- **Don't** let a page-level heading imply every page needs one — several pages intentionally rely on the persistent nav's brand name instead of a redundant `h1`.
