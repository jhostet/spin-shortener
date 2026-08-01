# Light/Dark Theme For The GUI

## Context

`gui/` is light-only, on purpose and by construction. `gui/theme.css:89` sets
`color-scheme: light`, and the comment at `gui/theme.css:64-88` records *why*
every token is declared unconditionally, outside any media query: Pico ships a
`@media (prefers-color-scheme: dark)` block that was silently leaking dark
form-field styling onto the login screen for any user whose OS preferred dark.
The fix was to override each leaking value unconditionally so it always wins.
That fix is still correct, and this plan does not undo it — it keeps it as the
no-JavaScript fallback and adds a real dark theme on top.

The motivation is `PRODUCT.md`'s Brand Commitments: *"A dark theme is the
preferred stylistic direction."* `DESIGN.md`'s north star ("The Signal Room" —
a quiet, dark-navy control room) describes an identity the light theme can only
half-express, since it renders dark navy as *chrome* on a light working canvas.
A dark theme is where that identity actually lands. There is no `TASKS.md`
Future-work entry for this; it was raised directly by the user.

**Confirmed decisions** (settled by the user before planning — not reopened
here):

1. **Auto + manual override.** Follow the OS via `prefers-color-scheme` by
   default, plus a toggle in the persistent nav that forces light or dark.
2. **A custom dark palette derived from the existing navy identity** — not
   Pico's stock dark theme. Signal Blue and the navy identity stay recognizable.
3. **Persistence via `localStorage`, client-side only.** No API call, no
   user-record schema change.
4. **Scope is the `gui/` pages only.**

**Non-goals**, stated so the builder does not drift into them:

- **No redesign of the light theme.** Every light value renders byte-identically
  after this change. Two of the eight tasks below are explicitly "no visual
  change" refactors whose done-criteria are `getComputedStyle` equality.
- **No change to any CSP directive.** The chosen FOUC fix needs none (see below).
- **Not `redirect`'s password-prompt page.** Its `'unsafe-inline'` cleanup is a
  separate, already-deferred `TASKS.md` Future-work entry.
- **No server-side or per-user persistence.**

## Key technical facts confirmed during research

**Cascade and selectors**

- **Pico's light block is `:host(:not([data-theme=dark])),:root:not([data-theme=dark]),[data-theme=light]`.**
  Confirmed by parsing `gui/vendor/pico.min.css`. `:root:not([data-theme=dark])`
  is specificity `(0,2,0)`; `theme.css`'s light block matches it exactly, so
  load order breaks the tie in `theme.css`'s favour. **This also means setting
  `data-theme="light"` does not break the existing light theme** — Pico's light
  values then arrive via `[data-theme=light]` `(0,1,0)`, and `theme.css`'s
  `(0,2,0)` block still matches and still wins.
- **Pico's dark block is a bare `[data-theme=dark]`, specificity `(0,1,0)`.**
  A `theme.css` dark block written as `:root[data-theme="dark"]` is `(0,2,0)`
  and therefore wins **by specificity, not by load order** — strictly more
  robust than the light block's tie-plus-load-order arrangement. Confirmed by
  parsing the vendor file.
- **Pico's `prefers-color-scheme` dark block is scoped to `:root:not([data-theme])`.**
  Confirmed: the block opens
  `@media only screen and (prefers-color-scheme:dark){:host(:not([data-theme])),:root:not([data-theme]){color-scheme:dark;…}`.
  Consequence, and it is a large one: **once anything sets a `data-theme`
  attribute at all, Pico's entire OS-preference block stops matching.** The leak
  class documented at `theme.css:64-88` is structurally eliminated whenever the
  init script runs. The unconditional light declarations remain as the
  script-didn't-run fallback and must not be deleted.
- **Pico's `[data-theme=dark]` block is 102 declarations and is a coherent,
  usable base.** It covers `--pico-secondary*`, `--pico-contrast*`, `--pico-code-*`,
  `--pico-accordion-*`, `--pico-dropdown-*`, `--pico-modal-overlay-background-color`,
  `--pico-table-border-color`, `--pico-switch-*`, `--pico-range-*` — all
  families `theme.css` leaves to Pico in light mode too. **Layer onto it; do not
  re-specify it variable-for-variable.** The subset that must be overridden is
  exactly the subset the light block already overrides, plus three specific
  traps listed next.
- **Three traps inside Pico's dark block, each confirmed by reading it:**
  1. `--pico-card-box-shadow:var(--pico-box-shadow)` and
     `--pico-dropdown-box-shadow:var(--pico-box-shadow)` — Pico **re-enables a
     real 7-layer drop shadow in dark mode.** `theme.css` sets both to `none` in
     the light block only. Missing this reintroduces exactly the No-Shadow Rule
     violation that went unnoticed for the theme's entire history (`DESIGN.md`,
     Elevation & Depth).
  2. `--pico-card-border-color:var(--pico-card-background-color)` — Pico makes
     card borders *invisible* in dark mode. This system's depth comes from a
     hairline border plus background contrast, with no shadow allowed, so a
     borderless card in dark mode leaves nothing at all separating surfaces.
  3. The density/type tokens are **not** in Pico's theme blocks — see the next
     bullet, which is the sharpest trap in this whole change.
- **`theme.css`'s density tokens are currently inside the light block and would
  silently vanish in dark mode.** `--pico-font-size: 100%`, `--pico-line-height`,
  `--pico-spacing`, and both `--pico-form-element-spacing-*` live at
  `gui/theme.css:22-26`, inside `:root:not([data-theme="dark"])`. That selector
  stops matching when `data-theme="dark"` is set, handing `--pico-font-size`
  back to Pico's viewport-scaling defaults (`106.25%`…`131.25%` at
  `@media (min-width:576px)`…`1536px`, confirmed by parsing the vendor file) and
  `--pico-spacing` back to `1rem`. Dark mode would render at a visibly different
  type and spacing scale from light mode — a subtle, easy-to-miss regression
  that reverses `DESIGN.md`'s single most emphatic Do ("keep `--pico-font-size`
  pinned at `100%`"). Task 1 exists solely to fix this before anything else
  lands.
- **Pico declares those density tokens at `:host,:root` — specificity `(0,1,0)`,
  including inside the viewport media queries.** Confirmed by parsing. So a
  shared block at `:root:not([data-theme="dark"]), :root[data-theme="dark"]`
  (`(0,2,0)` on both branches, covering every possible root state) beats them
  outright in both themes.

**Icons, images, and native chrome**

- **Pico's `--pico-icon-chevron` / `-date` / `-time` / `-search` / `-close` are
  declared once at the base `:host,:root` rule with a baked-in
  `stroke='rgb(136, 145, 164)'`, and are *not* redefined in either theme block.**
  Confirmed by parsing every `--pico-icon-*` declaration site. That single slate
  measures 5.51:1 against the proposed dark field background, so the sortable-column
  chevrons, the search icon, the `datetime-local` calendar icon, and the
  `<select>` arrow need **no** dark override. (`--pico-icon-valid`/`-invalid`
  *are* theme-scoped and come from Pico's dark block for free.)
- **`color-scheme` is worth making dynamic.** The app has real native controls
  whose rendering it governs: two `datetime-local` inputs (`dashboard.html:35,39`
  plus the edit-row pickers in `dashboard.js:105-106`), an `input[type=search]`
  (`dashboard.html:58`), and two `<select>`s (`admin/users.html:32`,
  `users.js:35`). `DESIGN.md`'s Don'ts correctly warn that `color-scheme` does
  *not* block `prefers-color-scheme` matching — that remains true; it is being
  set here for the native-chrome effect it actually has (scrollbars, autofill,
  the native date/select popups), not as a cascade mechanism.
- **The QR preview is a PNG from `api/qr.py`'s `PyPNGImage` factory
  (`detail.js:38`, `format=png&size=web`), and its background colour is
  UNCONFIRMED.** If it is white (the likely case for `qrcode`'s pure-PNG
  factory), it renders as a bright white square on a dark card — visually loud
  but correct, since a QR needs a light quiet zone to scan. If it is
  *transparent*, black modules on a dark card are both unreadable and
  unscannable. Confirming means loading `/links/detail.html?slug=…` in dark mode
  and looking. (Also UNCONFIRMED and worth noting alongside it: the design
  detector referenced by earlier plans, `detect.mjs`, ships with the impeccable
  tooling and is **not** invokable from a clean checkout — `TASKS.md` corrects
  an earlier plan on exactly this point, so verification below says "an
  `/impeccable` run over `gui/`" instead.) Pre-approved fix if transparent: a light `background` plus small
  `padding` on `#qr-preview` in `gui/links/detail.css`, applied in **both**
  themes (a QR needs the light quiet zone either way). Do not "fix" this by
  inverting the QR.

**Serving the init script**

- **Adding one exact route to the `gui` component is the established, sanctioned
  pattern.** `spin.toml` already carries 11 exact routes on that component,
  eight of them added by `docs/plans/csp-drop-unsafe-inline.md`. Wildcards 404
  on it — confirmed live, documented at `spin.toml:65-72` and in `CLAUDE.md`.
  Nested exact routes work (`/links/detail.js` serves today). `[component.gui]`'s
  `files = [{ source = "gui", destination = "/" }]` already covers any new file
  under `gui/`; **do not narrow it** (`spin.toml:119-128` records that a
  non-root destination mapping 404'd live).
- **One route covers all pages.** `/theme-init.js` is referenced as
  `theme-init.js` from the root-level pages and `../theme-init.js` from
  `admin/users.html` and `links/detail.html` — exactly how `app.js` and
  `theme.css` already work with one route each.
- **No CSP change is needed.** `gui-pages/routing.py`'s `script-src 'self'`
  already permits a same-origin external script. The chosen approach adds no
  inline code, so `gui-pages/tests/test_no_inline_code.py` keeps passing
  unmodified and **is not relaxed**.
- **`spin_static_fs`'s default `Cache-Control` is still UNCONFIRMED** (carried
  over from the CSP plan; `gui` sets no `CACHE_CONTROL` env var). Not a new risk
  class — `app.js` and `theme.css` already live with whatever it is.

**Measured contrast baselines** (computed with the WCAG relative-luminance
formula over the current `theme.css` values, so the dark palette can be
calibrated against what this system already ships rather than against an
abstract ideal):

| Light-theme relationship | Ratio |
|---|---|
| page `#eef1f6` vs card `#ffffff` | 1.13:1 |
| border `#d6deea` vs card `#ffffff` | 1.36:1 |
| border `#d6deea` vs page `#eef1f6` | 1.20:1 |
| body `#14243d` on card | 15.56:1 |
| muted `#5b6b85` on page (table cells) | 4.77:1 |
| Signal Blue `#276fb8` on card | 5.18:1 |
| white on Signal Blue fill | 5.18:1 |

- **Pre-existing, out of scope, flagged so the dark-mode measurement pass does
  not "discover" it as new:** the light theme's slug-chip **hover** text
  (`--ss-signal-500` `#276fb8` on `--ss-navy-800` `#14243d`,
  `gui/theme.css:321-324`) measures **3.00:1** — under the 4.5:1 AA text
  minimum. It is a hover-only state on a decorative chip whose text is also the
  link's own href, and fixing it means changing a light-theme colour, which this
  plan is explicitly forbidden from doing. The dark theme's equivalent is
  specified at 5.47:1, so the two themes will not match on this one measurement.
  Worth a `TASKS.md` Future-work entry (added below); not worth silently
  smuggling into a dark-mode change.

## The FOUC decision

**Recommendation: a small external `gui/theme-init.js`, loaded render-blocking
as the first element of `<head>` on the four real pages, plus one exact
`spin.toml` route.**

The constraint is hard: as of `2f8f62b`, `gui-pages/routing.py:52` sends
`script-src 'self'` with no `'unsafe-inline'`, so the conventional in-head
inline snippet is blocked outright. `gui/app.js` loads at the *end of `<body>`*
on four of five pages, so applying the theme from there paints light and then
flips — a visible flash on every navigation in a five-page multi-page app.

Why the external script wins:

- **It is CSP-clean with zero policy change** and leaves
  `test_no_inline_code.py` untouched and still meaningful. A hash-based CSP is
  not merely unattractive here; it was **explicitly rejected on 2026-07-31**
  (`TASKS.md`, "Considered and rejected"; `docs/plans/csp-drop-unsafe-inline.md`)
  because the hash must be hand-recomputed on every edit with a silently-dead
  page as the failure mode, and that entry says to revisit only if a page
  "genuinely cannot externalize its script". A theme-init snippet externalizes
  perfectly. Nothing has changed; the earlier decision holds.
- **The cost is one route and one small blocking request per page.** The route
  is inert config; the pattern already exists eleven times over in `spin.toml`;
  and the file is ~15 lines, cached after first load.
- **A sync script in `<head>` runs before any body content is parsed or
  painted**, so there is no flash at all — not even for a user who has forced a
  theme against their OS preference.

Two placement details are load-bearing:

1. **The `<script>` goes first in `<head>`, before the `<link rel="stylesheet">`
   tags.** A script that follows a stylesheet link waits for that stylesheet to
   load before executing. Putting it first means the attribute is set without
   waiting on CSS.
2. **It carries no `defer`, `async`, or `type="module"`.** Any of those defers
   execution past first paint and reintroduces the flash.

Its failure mode is the one this component is known for: **if the
`spin.toml` route is missing, the script 404s, no attribute is ever set, and
the app silently stays light** (the existing unconditional light block wins) —
looking fine and simply not theming. Verification `curl`s the route explicitly
for exactly this reason.

**`gui/index.html` is deliberately excluded.** It is a four-line redirect stub
with an empty `<body>` and no stylesheet at all (`gui/index.html`, `gui/index.js`),
so `data-theme` would have nothing to act on; loading a blocking script there
would cost a request for zero effect. Instead it gets one line —
`<meta name="color-scheme" content="light dark">` — so the blank stub paints the
OS-appropriate canvas instead of a white flash on the way to `login.html`. A
user who has forced light while their OS is dark sees one frame of dark blank
canvas with nothing rendered on it; that is the accepted edge.

**`gui/login.html` gets the script but no toggle** — it has no
`<header id="app-header">` and never calls `initHeader()`. A stored preference
still applies there; there is just no control to change it from the login
screen. Accepted, disclosed, and noted as a follow-up.

**No-JS behaviour:** the app falls back to light regardless of OS preference.
This is a non-issue in practice — every page in this GUI is JS-driven (each one
fetches `/auth/me` and renders its tables from JS), so a JS-off visitor sees an
empty shell either way — and it is strictly the current behaviour, unchanged.

## Theme architecture (`gui/theme.css`)

Three blocks, in this order, replacing the current single block:

```css
/* 1. Theme-independent: density/type scale + the raw navy ramp.
 *    Both branches are (0,2,0) and together they match every possible
 *    root state, so these beat Pico's own (0,1,0) :host,:root declarations
 *    (including its viewport font-size media queries) in BOTH themes. */
:root:not([data-theme="dark"]),
:root[data-theme="dark"] { … }

/* 2. Light — the existing block, minus the tokens moved to (1), plus the
 *    four new semantic tokens. Selector UNCHANGED: it must keep matching
 *    Pico's own :root:not([data-theme=dark]) exactly. */
:root:not([data-theme="dark"]) { … }

/* 3. Dark — new. (0,2,0) beats Pico's bare [data-theme=dark] (0,1,0)
 *    outright, without relying on load order. */
:root[data-theme="dark"] { … }
```

### Block 1 — theme-independent (moved out of the light block)

`--pico-font-size: 100%`, `--pico-line-height: 1.4`, `--pico-spacing: 0.75rem`,
`--pico-form-element-spacing-vertical: 0.5rem`,
`--pico-form-element-spacing-horizontal: 0.75rem`, `--ss-mono-font`,
`--ss-navy-950: #0a1628`, `--ss-navy-800: #14243d`.

The two navy ramp values move here because they are constants that
`DESIGN.md`'s frontmatter names as identity colours; the light block then
references them through the new semantic tokens.

### Block 2 — four new semantic tokens in the light block

The `--ss-*` tokens are already the indirection layer for everything the dark
block needs to change — redefining `--ss-signal-500`, `--ss-ok-500`,
`--ss-danger-500`, and `--ss-slate-500` per theme requires **zero** call-site
edits. Four places do not have that indirection today and need it:

| New token | Light value (identical rendering to today) | Call site to repoint |
|---|---|---|
| `--ss-chrome-bg` | `var(--ss-navy-950)` | `#app-header nav { background: … }` (`theme.css:132`) |
| `--ss-chip-bg` | `var(--ss-navy-950)` | `.slug-chip { background: … }` (`theme.css:306`) |
| `--ss-chip-hover-bg` | `var(--ss-navy-800)` | `a.slug-chip:hover { background: … }` (`theme.css:322`) |
| `--ss-chip-hover-fg` | `var(--ss-signal-500)` | `a.slug-chip:hover { color: … }` (`theme.css:323`) |

Why the nav and the chip cannot just read a re-defined `--ss-navy-950`: in dark
mode they need *different* values from each other (the nav goes darker than the
canvas, the chip goes lighter than the card), and `--ss-navy-950`/`-800` stay
what `DESIGN.md` says they are — two specific navies — in both themes.

One more call-site edit, no new token: `.identity-avatar`
(`theme.css:224-239`) currently fills with `var(--ss-signal-500)` and inks with
`#fff`. `--ss-signal-500` becomes a *light* blue in dark mode, and white on it
measures 2.30:1 — a real failure, not a margin. Repoint the avatar to
`background: var(--pico-primary-background)` / `color: var(--pico-primary-inverse)`
— Pico's own fill/ink-on-fill pairing, which is whatever the active theme's
fill and label colours are: `#276fb8`/`#fff` at 5.18:1 in light (**no rendering
change**), `#6fb0ee`/`#0a1628` at 7.87:1 in dark. The point of the indirection
is that the avatar tracks the primary button automatically instead of needing
its own per-theme values.

### Block 3 — the dark palette

Derived from the navy identity, at **constant hue**: every dark value sits
within ~2° of its light counterpart's HSL hue and differs only in lightness and
saturation. (Verified numerically: signal 210°→209°, slate 218°→217°, muted
217°→217°, ok 158°→158°, danger 3°→5°, canvas 218°→216°.) That is the rule to
state in `DESIGN.md` and the rule to follow if a value has to be nudged during
the measurement pass — it is also the exact fix pattern the repo already used
twice ("clears 5.2:1 at the same hue").

Ratios below are computed, not guessed, and must be **re-measured live**
(see Verification) — they assume the element sits on the background named, and
"measured against the wrong assumed background" is this repo's signature bug.

| Variable(s) | Dark value | Measured |
|---|---|---|
| `color-scheme` | `dark` | — |
| `--pico-background-color` | `#0a1628` | canvas = Deep Navy itself; 1.17:1 vs card (light's own page/card step is 1.13:1) |
| `--pico-card-background-color`, `--pico-card-sectioning-background-color` | `#14243d` | Slate Navy — one step lighter reads as "lifting", the same relationship the slug chip already uses |
| `--pico-card-box-shadow`, `--pico-dropdown-box-shadow` | `none` | No-Shadow Rule; Pico's dark block re-enables these |
| `--pico-card-border-color`, `--pico-muted-border-color` | `#31456a` | 1.62:1 vs card, 1.89:1 vs canvas (light: 1.36 / 1.20) |
| `--pico-color` | `#dfe6f2` | 12.40:1 on card, 14.45:1 on canvas |
| `--pico-h1-color`, `-h2-`, `-h3-` | `#f2f6fc` | 14.35:1 on card |
| `--pico-muted-color` | `#93a3bd` | 6.09:1 on card, 7.09:1 on canvas (light: 4.77:1) |
| `--pico-primary`, `--pico-primary-background`, `--pico-primary-border`, `--ss-signal-500` | `#6fb0ee` | accent text 6.76:1 on card, 7.87:1 on canvas; as a **button fill**, 6.76:1 boundary vs card (**+3.76 over the 3:1 minimum**); focus ring 6.76:1 vs card, 8.34:1 vs nav |
| `--pico-primary-hover`, `--pico-primary-hover-background`, `--pico-primary-hover-border` | `#9ccbf7` | 9.11:1 on card; as a hover fill, 9.11:1 boundary — **lightens** on hover, the inverse of light mode's darken |
| `--pico-primary-inverse` | `#0a1628` (Deep Navy) | button label on the fill: **7.87:1** at rest, 10.62:1 on hover |
| `--pico-primary-underline` / `-hover-underline` / `-focus` | `rgba(111,176,238, .5 / .75 / .375)` | mirrors the light block's rgba pattern with the dark accent |

**The primary fill is the accent colour itself, in both themes.** The light
block already sets `--pico-primary` and `--pico-primary-background` to the same
`#276fb8`, and `--pico-primary-hover` / `-hover-background` to the same
`#1e6bb8`; the dark block keeps that structure exactly, with `#6fb0ee` and
`#9ccbf7`. What flips is the ink: light mode is white-on-blue, dark mode is
**Deep-Navy-on-light-blue** — the app's own darkest identity value, reused as
the label colour rather than a new token. See Trade-offs for why cross-theme
button identity was given up to get here.

**Thin-margin sweep.** Every value in this table was checked against its
threshold (4.5:1 for text, 3:1 for non-text) and none now sits within 0.3 of
it. The tightest margins after the primary fix are the slug-chip hover text at
5.47:1 (+0.97), muted text on a card at 6.09:1 (+1.59), and the resting invalid
field border at 5.17:1 (+2.17); everything else clears by more than 2.2. The
surface separations (card-vs-canvas 1.17:1, border-vs-card 1.62:1) have no WCAG
threshold to be thin against — they are calibrated to the light theme's own
1.13:1 and 1.36:1 and are equal or better on both counts.
| `--pico-form-element-background-color`, `-active-background-color` | `#0d1a2e` | text 14.7:1, placeholder 6.82:1, Pico's own chevron/date/search icon 5.51:1 |
| `--pico-form-element-border-color` | `#31456a` | 1.82:1 vs field |
| `--pico-form-element-color` | `#e6ecf7` | 14.7:1 |
| `--pico-form-element-placeholder-color` | `#93a3bd` | 6.82:1 |
| `--pico-form-element-invalid-border-color` | `#c4767c` | 5.17:1 vs field |
| `--pico-form-element-invalid-active-border-color` | `#d4756c` | 5.45:1 vs field |
| `--pico-form-element-selected-background-color` | `#1c3054` | 11.06:1 against field text |
| `--ss-slate-500` | `#a8b6cc` | 7.58:1 on card, 8.83:1 on table cells |
| `--ss-ok-500` | `#4cc79a` | 7.37:1 on card, 8.58:1 on cells |
| `--ss-danger-500` | `#ff8a80` | 6.82:1 on card, 7.94:1 on cells |
| `--ss-chrome-bg` | `#060f1d` | nav stays the darkest surface on screen, as in light |
| `--ss-chip-bg` | `#1e3a63` | white chip text 11.41:1 |
| `--ss-chip-hover-bg` | `#28486f` | — |
| `--ss-chip-hover-fg` | `#9ccbf7` | 5.47:1 on the hover background |

**The form-element list is deliberately the same eight variables the light block
overrides.** `DESIGN.md`'s Do ("sweep an entire Pico CSS-variable family") plus
the two-round history behind that list means an asymmetry between the two blocks
is the drift most likely to become round three. Everything Pico's dark block
covers that the light block also leaves to Pico — `--pico-secondary*`,
`--pico-contrast*`, `--pico-del-color`, `--pico-code-*`, `--pico-accordion-*`,
`--pico-dropdown-*` (except the shadow), `--pico-modal-overlay-background-color`,
`--pico-table-*`, `--pico-switch-*`, `--pico-range-*` — stays with Pico in dark
mode too. That symmetry is the reason the dark block is ~35 declarations rather
than ~100.

## The init script (`gui/theme-init.js`, new)

~15 lines, no dependencies, no DOM access beyond `document.documentElement`.
Contract, not implementation:

- **Storage key: `"ss-theme"`.** Values `"system"` | `"light"` | `"dark"`.
  Namespaced because the origin is shared with `sessionStorage`'s unprefixed
  `csrf_token` and a bare `"theme"` is the sort of key a future addition
  collides with.
- **Absent → `"system"`. Anything unrecognized → treated as `"system"`, and the
  stored value is left alone** rather than rewritten: a future version may add a
  value, and clobbering is not the init script's job.
- **Resolution:** `"system"` resolves through
  `window.matchMedia("(prefers-color-scheme: dark)").matches`.
- **It always sets `document.documentElement.dataset.theme` to a literal
  `"light"` or `"dark"`** — never to `"system"`, and never leaves it unset. This
  is what disables Pico's `prefers-color-scheme` block entirely, and it is why
  the attribute value and the stored *mode* are two different things.
- **Every `localStorage` access is wrapped so it cannot throw** (Safari private
  mode and disabled-storage configurations throw on access, not just on write).
  A throw before the attribute is set would silently drop the page back to the
  light fallback.
- **It exposes `window.ssTheme`** with `KEY`, `get()` → the stored mode,
  `set(mode)` → persists (best-effort) and applies, and `resolve()`/`apply()`.
  `app.js`'s toggle calls these rather than re-implementing the key and the
  resolution rule in a second place — one source of truth.
- **It registers the `matchMedia` `change` listener itself**, re-applying only
  when the current mode is `"system"`, so a user who changes their OS theme with
  the app open sees it follow without a reload.
- **`app.js` must not dereference `window.ssTheme` at top level** — `index.html`
  loads `app.js` without `theme-init.js`. Only `initHeader()` and its handlers
  touch it.

Cross-tab sync via the `storage` event is deliberately out of scope (see
follow-ups).

## The nav control (`gui/app.js`, `gui/theme.css`)

**Three states, not two.** The default state is "follow the OS", and a two-state
light/dark toggle makes that state unreachable the instant a user clicks once —
they are pinned until they clear site data. A three-state control is the honest
representation of the model the user chose in decision 1.

**Shape: a segmented group of three buttons**, added to `initHeader()`'s
template (`gui/app.js:198-220`) as a new `<li id="theme-control">` immediately
before the Log out `<li>`, so the account group reads identity chip → Manage
users → theme → Log out.

- `<div role="group" class="theme-toggle" aria-label="Color theme">` containing
  three `<button type="button" data-theme-choice="system|light|dark">` with the
  visible labels **Auto / Light / Dark**.
- **Accessible state is `aria-pressed`** on each button, exactly one `"true"`,
  set on render from `window.ssTheme.get()` and updated on click. (A
  `radiogroup`/`radio` pairing is arguably more precise semantically but obliges
  correct arrow-key roving-tabindex handling; a pressed-button group is the
  forgiving, widely-deployed pattern and every button stays individually
  tabbable.)
- Handlers are wired after the `innerHTML` assignment, alongside the existing
  logout wiring — never as `on…=` attributes, which
  `test_no_inline_code.py` also checks inside `app.js`.
- Clicking calls `window.ssTheme.set(choice)`; the change applies immediately on
  the current page and survives navigation because every page re-runs
  `theme-init.js`.

**Styling, and the specificity landmine.** `DESIGN.md` warns three times that
`#app-header nav li { color: #fff }` (`theme.css:138-142`, specificity `1,0,2`)
silently beats plain-class rules, and that this exact shape has caused three
separate silent bugs. Here the risk is different but adjacent: the buttons get
their colour from Pico's own `button` and `[role=group] [type=button]` rules
(`(0,2,0)`), which beat *inherited* colour outright. Scope the new rules as
`#app-header nav .theme-toggle button` (`1,1,1`) so they win on specificity, and
**verify with `getComputedStyle`, not by looking at it** — the repo's standing
rule for any new nav rule.

- Resting: transparent background, `#fff` text, `rgba(255,255,255,0.4)` border —
  matching the existing `#logout-btn` treatment (`theme.css:164-167`), which is
  already the precedent for "a control on the navy nav" and works unchanged in
  both themes.
- Pressed (`[aria-pressed="true"]`): `background: var(--pico-primary-background)`
  / `color: var(--pico-primary-inverse)` — the active theme's fill/ink pair,
  so `#276fb8` with white (5.18:1) in light and `#6fb0ee` with Deep Navy
  (7.87:1) in dark, both well clear of AA. Deliberately **not** a hardcoded
  `#fff` on `var(--ss-signal-500)`, which would measure 2.30:1 in dark mode.
  The pressed chip also reads more clearly against the dark nav than a
  `#276fb8` fill would have: 8.34:1 vs `--ss-chrome-bg`.
- `font-size: 0.75rem` — the existing **Caption** step, reused rather than
  drifting a new size, the same correction already applied to
  `.slug-kind-badge` and `.identity-avatar`.
- `.theme-toggle { width: auto; }` — Pico's `[role=group]` defaults to
  `width: 100%`, the documented bug this repo has now hit on three separate
  groups (`detail.css`, `users.css`, `dashboard.css` all carry this same line).

**Nav crowding is a real risk and must be measured.** The account `<ul>` already
holds the identity chip, "Manage users", and "Log out"; the nav wraps at the
`<nav>` level only, as whole `<ul>` groups (`theme.css:143-156`, with
`DESIGN.md` recording *why* wrapping inside a `<ul>` was tried and rejected).
Three more 44px buttons (~140px) may overflow at 390px. **Pre-approved fallback
if measurement shows overflow:** inside the existing
`@media (max-width: 480px)` block (`theme.css:266-270`), allow the account `<ul>`
alone to wrap (`flex-wrap: wrap; row-gap: 0.4rem`) and add
`align-items: flex-start` on the `<nav>` to prevent the brand-group stretching
bug `DESIGN.md` documents. Scoped to that media query, desktop untouched. Do not
solve it by hiding the control, and do not shorten the labels to single letters
(a visible "A" with an `aria-label` of "Auto" is a label mismatch).

## `spin.toml` changes

Exactly **one** new `[[trigger.http]]`, on the `gui` component, inside the
existing "Per-page scripts and styles" comment block (extend that comment — this
one is not page-scoped, it is loaded by every page):

```toml
[[trigger.http]]
route = "/theme-init.js"
component = "gui"
```

`gui` goes from 11 exact routes to 12; the application from 14 HTTP triggers to
15. `[component.gui]`'s `files` list is not touched. No wildcard, not even a
tempting one.

## `gui-pages` changes

No behaviour change. Two test-only additions:

- `gui-pages/tests/test_routing.py` — add `("/theme-init.js", None)` to
  `test_resolve_file`'s parametrize list, alongside the existing
  `("/dashboard.js", None)` / `("/admin/users.css", None)` entries, pinning the
  fact that the new asset is served by `gui`, not by this component.
- `gui-pages/tests/test_no_inline_code.py` — extend the two `app.js`-specific
  tests (`test_app_js_has_no_style_attribute_in_templates`,
  `test_app_js_has_no_inline_script_tag_in_templates`) to parametrize over
  `("app.js", "theme-init.js")`. **The guard itself is not relaxed in any way** —
  the chosen approach adds no inline code, so every existing assertion keeps
  holding as written.

`gui-pages/routing.py` and its `SECURITY_HEADERS` are **not** touched.
`Jenkinsfile` is not touched — no new test invocation, and the changed tests are
in a directory its `gui-pages` stage already runs.

## Documentation changes

- **`DESIGN.md`** — the normative layer, and currently light-only in both its
  frontmatter and its prose:
  - Frontmatter `colors:` gains the dark palette under `dark-`-prefixed keys
    (`dark-canvas`, `dark-surface`, `dark-chrome`, `dark-border`, `dark-text`,
    `dark-heading`, `dark-muted`, `dark-badge-slate`, `dark-signal`,
    `dark-signal-hover`, `dark-ok`, `dark-danger`, `dark-chip`,
    `dark-chip-hover`), taking it from 12 entries to 26. The existing 12 keep
    their exact values. **No key is added for the dark primary *fill* or its
    ink**, and that is deliberate: the fill is `dark-signal` itself (exactly as
    `signal-blue` doubles as the light fill), and the ink is the existing
    `navy-950` Deep Navy. The prose subsection must say so, so nobody later
    invents a `dark-signal-fill` key that duplicates a value.
  - A new `### Dark theme` subsection under `## Colors` carrying the role→value
    table above with its measured ratios, plus a new Named Rule — **The
    Constant-Hue Rule**: the dark palette is the light palette re-lightened at
    the same hue, and any future dark value is derived the same way.
  - The `Inputs / Fields` "Light-only, deliberately" note is now half-wrong and
    must be updated: the unconditional declarations remain, but their role is
    now the no-JS fallback, and the real defence is that `data-theme` is always
    explicitly set. The correction about `color-scheme` not blocking
    `prefers-color-scheme` stays exactly as written — it is still true, and
    `color-scheme` is now dynamic for the native-chrome reason only.
  - Two new Do's: *sweep both theme blocks together* (an override added to one
    and not the other is the same drift class as the form-element family), and
    *density/scale tokens belong in the theme-independent block, never in a
    theme block*.
  - `## Navigation` gains the theme control; `## Elevation & Depth` gains the
    note that Pico's dark block re-enables `--pico-card-box-shadow`, so the
    No-Shadow Rule needs enforcing twice.
  - **UNCONFIRMED: whether the Impeccable tooling validates the frontmatter's
    colour-key set**, i.e. whether 26 keys with a `dark-` prefix are accepted as
    readily as the current 12. The prose parser reads the `## Colors` section
    rather than the frontmatter (`design-parser.mjs`'s
    `extractColors(sections['Colors'])`), and the `dark-` keys follow the
    existing flat `name: hex` shape, so this is *expected* to be fine — but it
    is expectation, not knowledge. **Confirming it is part of task 6, not an
    assumption underneath it**: run `/impeccable` over `gui/` after the
    frontmatter edit and compare against the known 2-false-positive baseline. If
    the tooling rejects or ignores the prefixed keys, fall back to recording the
    dark palette in the `### Dark theme` prose only and note why in `DESIGN.md`.
- **`.impeccable/design.json`** — the sidecar is hand-refreshed (see `TASKS.md`
  "Refresh the design.json sidecar"). Add `colorMeta` entries for the dark
  palette's named colours mirroring the existing `role`/`displayName`/`canonical`
  shape, and update the `Slug Chip`, `Nav Shell`, and card/table component
  entries' CSS snapshots for the new token indirection.
- **`CLAUDE.md`** — the Architecture bullet says the `gui` component serves 11
  exact routes; it now serves 12, and the new one is *not* page-scoped. Add a
  short **Theming** section: the three-block structure and why the light block's
  selector must stay exactly as it is, `data-theme` always being set explicitly
  by `theme-init.js`, the `ss-theme` localStorage key and its three values, the
  no-JS-falls-back-to-light behaviour, and the two Pico dark-block traps (the
  re-enabled card shadow, the invisible card border).
- **`PRODUCT.md`** — one line under Capabilities and Constraints recording that
  the GUI ships light and dark themes following the OS by default with a
  client-side override, and one clause in Brand Commitments noting the dark
  palette is derived from the existing navy identity.

## Trade-offs and rejected alternatives

- **An inline `<head>` snippet allowed by a `'sha256-…'` CSP hash.** Attractive:
  zero new routes, zero new requests, zero extra latency before first paint, and
  the CSP stays a static string. Lost because this is a settled decision, not an
  open one — `docs/plans/csp-drop-unsafe-inline.md` and the dated `TASKS.md`
  entry rejected hashes on 2026-07-31 because the hash must be recomputed by
  hand on every edit with a *silently dead page* as the failure mode, and said
  to revisit only for a page that genuinely cannot externalize its script.
  Nothing has changed, and a 15-line theme-init snippet is the easiest thing in
  the app to externalize. It would also require relaxing
  `gui-pages/tests/test_no_inline_code.py`'s srcless-`<script>` assertion — the
  one thing standing between the CSP and quiet decay — for a saving of one route
  entry.
- **Accept the flash.** Genuinely live: it costs nothing and ships sooner. Lost
  because this is a five-page multi-page app where every navigation is a full
  document load, so the flash is not a one-time startup artifact — it is a white
  strobe on every click, on the surface whose entire purpose is being pleasant
  to sit in front of all day. And the fix is one small file plus one route.
- **Move `<script src="app.js">` into `<head>` and put the theme logic at the top
  of it.** The most tempting alternative: zero new routes, zero new files, zero
  new requests (`app.js` is already fetched on every page, and `index.html`
  already loads it from `<head>`), and it is behaviour-preserving — `app.js`
  declares only functions and constants at top level and touches no DOM until
  called. Lost on two counts: it makes a 10 KB shared bundle render-blocking on
  four pages instead of a ~600-byte file, and it permanently couples first paint
  to the growth of the app's shared library, so the next 200 lines added to
  `app.js` silently become a paint cost. A dedicated tiny file keeps the
  blocking payload proportional to the job.
- **`light-dark()` for every colour token.** The most elegant option on paper:
  one token block instead of two, `color-scheme` alone drives the switch, no
  duplication, correct on first paint with **no JavaScript at all** for the auto
  case, and forcing a theme becomes a three-line `color-scheme` override. Lost
  on two counts. First, it requires rewriting every one of the ~30 existing
  light declarations into `light-dark(a, b)` form — touching the single block
  this repo has already gotten silently wrong twice, against an explicit
  "no light-theme redesign" non-goal, for a light theme that is currently
  correct. Second, its degradation is bad rather than graceful: on an engine
  without `light-dark()` support the declarations are invalid at computed-value
  time and the affected custom properties resolve to unset, which is a broken
  page rather than a stale one. Revisit if the token block is ever rewritten for
  an independent reason.
- **CSS-only auto: duplicate the dark block inside
  `@media (prefers-color-scheme: dark) { :root:not([data-theme]) { … } }` and use
  JS only for the explicit override.** Attractive because the common case then
  needs no JavaScript and cannot flash. Lost because CSS offers no way to share
  one declaration block between a media-query and a non-media-query context, so
  ~35 declarations exist twice — and "the same value declared in two places that
  must be kept in sync" is precisely the drift the sweep-the-family Do exists to
  prevent. With the head script, the JS path has no flash either, so the
  duplication buys nothing.
- **Server-side persistence: a theme cookie, with `gui-pages` writing
  `data-theme` into the served HTML.** Eliminates the flash completely with zero
  JavaScript and zero new routes. Lost first on the user's confirmed decision 3
  (client-side only), and independently on the same ground the CSP plan used to
  reject nonces: it forces `build_response` to stop being a pass-through and
  start doing byte-level markup surgery on `<html>` in the one component whose
  entire job is to be a trustworthy header attacher.
- **Pico's stock `[data-theme=dark]` palette as the visible theme.** Ruled out by
  confirmed decision 2 — its azure `#01aaff` and neutral grays discard the navy
  identity. Worth being precise about what *is* being rejected: Pico's dark block
  is still the base for the ~70 variables this theme does not override (secondary,
  contrast, code, accordion, dropdown, table, switch, range, modal overlay), the
  same ones the light theme leaves to Pico. Only the identity-carrying subset is
  overridden.
- **A two-state light/dark toggle.** Simpler control, less nav width, and the
  user's own phrasing ("a toggle … that forces light or dark") permits it. Lost
  because "follow my OS" is the default state and a two-state control makes it
  permanently unreachable after the first click, with no affordance anywhere to
  get back. Given persistence is `localStorage`-only, the escape hatch would be
  clearing site data.
- **A `<select>` for the three states.** More compact than three buttons (~110px
  vs ~140px) and native keyboard/screen-reader semantics for free. Lost because
  a Pico `<select>` on the navy nav needs a fight: `width: 100%` by default, a
  form-element background/colour set that would have to be overridden for the
  navy fill, and a chevron whose colour is baked into a `data:` URL rather than
  read from a variable — so it may render near-invisible on a dark fill, needing
  a bespoke override of `--pico-icon-chevron`. Three buttons reuse the
  already-proven `#logout-btn` treatment instead.
- **Keeping the primary button white-on-`#276fb8` in dark mode, for cross-theme
  visual identity — proposed, then reversed.** The first draft of this plan kept
  `--pico-primary-background: #276fb8` with `--pico-primary-inverse: #fff` in
  dark, so the primary button would render pixel-identically in both themes. It
  measured white ink at 5.18:1 (fine) but a fill-vs-card boundary of **exactly
  3.00:1** — sitting precisely on WCAG 1.4.11's non-text minimum with zero
  headroom. **Reversed, and the deciding argument is this repo's own history:**
  it has shipped a thin-margin colour twice — `ok-green` at 4.39:1 (measured
  against the wrong background and *under* AA) and `badge-slate` clearing AA by
  0.27:1 — and had to go back and fix both. A value with zero margin breaks
  silently the first time anything adjacent moves; here, any future adjustment
  to `--pico-card-background-color` invalidates it without touching the button
  at all.

  Nor could the margin be bought cheaply: white ink caps the fill's lightness
  hard. `#2d78c0` gives white 4.61:1 with a 3.38:1 boundary and `#3079c4` gives
  4.51:1 / 3.45:1 — both merely trade one thin margin for two. Getting real
  headroom on *both* criteria requires changing the ink, so dark mode now uses
  the light-fill/dark-ink convention: `#6fb0ee` fill (the accent colour itself,
  at constant hue with the light `#276fb8` — 209° vs 210°) with
  `--pico-primary-inverse: #0a1628`, Deep Navy. That measures **6.76:1 for the
  fill against the card (+3.76 over the 3:1 minimum)** and **7.87:1 for the
  label on the fill (+3.37 over AA)**, with the hover state at 9.11:1 / 10.62:1.

  **Accepted consequence, recorded deliberately: the primary button is no longer
  pixel-identical across themes.** Light mode is white-on-mid-blue; dark mode is
  navy-on-light-blue. What survives is what actually carries the identity — the
  hue is the same Signal Blue to within 1°, the fill is still the accent colour
  and nothing else, the hover step still moves in the "lifting" direction for
  its theme, and the ink is the app's own Deep Navy rather than an imported
  neutral. Cross-theme pixel identity was a nice-to-have; contrast headroom on
  the single most-clicked control in the app is not. Two other elements follow
  the same pair automatically, by design — `.identity-avatar` and the pressed
  state of the new theme control both read `--pico-primary-background` /
  `--pico-primary-inverse` rather than hardcoding a fill and a white ink, so
  neither can drift out of step with this decision.
- **Do nothing.** Live, and cheap. Lost because `PRODUCT.md` names a dark theme
  as the preferred direction, `DESIGN.md`'s north star describes one, and the
  work is bounded: one new 15-line file, one route, ~35 CSS declarations, and a
  nav control.

## Tasks

The lines appended to `TASKS.md` under a new `## Light/dark theme` heading:

```
- [ ] Split the theme-independent tokens out of theme.css's light block (must land before every other task in this section) — file(s): gui/theme.css — done when: a new `:root:not([data-theme="dark"]), :root[data-theme="dark"]` block near the top of theme.css holds `--pico-font-size: 100%`, `--pico-line-height`, `--pico-spacing`, both `--pico-form-element-spacing-*`, `--ss-mono-font`, `--ss-navy-950` and `--ss-navy-800`, and those 8 declarations are removed from the `:root:not([data-theme="dark"])` block; a comment records that the light block's selector stops matching in dark mode, so density/scale tokens left inside it would silently hand `--pico-font-size` back to Pico's viewport-scaling defaults; on a live `/dashboard.html`, `getComputedStyle(document.documentElement)` returns the identical values for all 8 as before the change, and the page is visually unchanged.
- [ ] Add the four semantic surface tokens and repoint their call sites (no visual change) — file(s): gui/theme.css — done when: `--ss-chrome-bg`, `--ss-chip-bg`, `--ss-chip-hover-bg` and `--ss-chip-hover-fg` are defined in the light block with today's exact values (`var(--ss-navy-950)`, `var(--ss-navy-950)`, `var(--ss-navy-800)`, `var(--ss-signal-500)`); `#app-header nav`, `.slug-chip` and `a.slug-chip:hover` read them; `.identity-avatar` reads `var(--pico-primary-background)`/`var(--pico-primary-inverse)` instead of `var(--ss-signal-500)`/`#fff`; live `getComputedStyle` on `/dashboard.html` still reports nav background `rgb(10, 22, 40)`, `.slug-chip` background `rgb(10, 22, 40)`, `.identity-avatar` background `rgb(39, 111, 184)` and colour `rgb(255, 255, 255)`.
- [ ] Add the dark token block to theme.css and measure it (inert until data-theme is set) — file(s): gui/theme.css — done when: a `:root[data-theme="dark"]` block carries every value in the plan's dark-palette table including `color-scheme: dark`, `--pico-card-box-shadow: none`, `--pico-dropdown-box-shadow: none`, a `--pico-card-border-color` distinct from the card background, `--pico-primary-background` set to the dark accent `#6fb0ee` with `--pico-primary-inverse` set to Deep Navy `#0a1628` (not white), and the same 8 `--pico-form-element-*` variables the light block overrides; with `document.documentElement.dataset.theme = "dark"` set by hand in devtools on all 5 pages, live `getComputedStyle` measurement against each element's real rendered background clears 4.5:1 for every text element in the plan's Verification list and 3:1 for the focus ring, with the primary button fill measuring at least 3.3:1 against its card background (expected 6.76:1 — a bare 3.00:1 is a fail here, not a pass) and its Deep-Navy label at least 4.5:1 on that fill (expected 7.87:1); `getComputedStyle(document.documentElement).colorScheme` is `"dark"`; every `<article>`'s computed `boxShadow` is `none`; `--pico-font-size` still resolves to `100%` and `--pico-spacing` to `0.75rem`; any value that misses is re-derived at the same hue and the table in the plan is corrected to the measured number.
- [ ] Add theme-init.js, load it in every page's `<head>`, and route it (delivers OS-following dark mode with no toggle yet) — file(s): gui/theme-init.js (new), gui/login.html, gui/dashboard.html, gui/admin/users.html, gui/links/detail.html, gui/index.html, spin.toml, gui-pages/tests/test_routing.py, gui-pages/tests/test_no_inline_code.py — done when: `theme-init.js` reads the `ss-theme` localStorage key (`system`/`light`/`dark`, absent or unrecognized treated as `system` without rewriting storage), resolves `system` through `matchMedia("(prefers-color-scheme: dark)")`, always sets `document.documentElement.dataset.theme` to a literal `light` or `dark`, wraps every storage access so it cannot throw, exposes `window.ssTheme` (`KEY`, `get`, `set`, `resolve`, `apply`), and registers a `matchMedia` change listener that re-applies only while the mode is `system`; the 4 real pages load it as the first element of `<head>` with no `defer`/`async`/`type=module` and before their stylesheet links (`../theme-init.js` from the nested pages); `gui/index.html` instead gets `<meta name="color-scheme" content="light dark">` and no script; `spin.toml` gains exactly one `route = "/theme-init.js"` exact trigger on the `gui` component; `curl -s -o /dev/null -w '%{http_code}' http://localhost:3000/theme-init.js` returns 200; with the OS set to dark, `/login.html` and `/dashboard.html` render dark on first paint with no light flash and no console errors, and with the OS set to light they render light; `test_resolve_file` gains `("/theme-init.js", None)` and the two `app.js` inline-code tests are parametrized over `("app.js", "theme-init.js")`; `cd gui-pages && uv run pytest` passes.
- [ ] Add the three-state theme control to the persistent nav — file(s): gui/app.js, gui/theme.css — done when: `initHeader()`'s template renders `<li id="theme-control">` immediately before the Log out `<li>`, containing a `<div role="group" class="theme-toggle" aria-label="Color theme">` with three `type="button"` buttons labelled Auto/Light/Dark carrying `data-theme-choice`; exactly one has `aria-pressed="true"`, set from `window.ssTheme.get()` on render and updated on click; handlers are attached with `addEventListener` after the `innerHTML` assignment (no `on<event>=` attributes, no `style=` attribute — `gui-pages`'s inline-code guard still passes); clicking a choice calls `window.ssTheme.set()`, repaints immediately, and the choice survives a full navigation to another page and back; theme.css styles the group via `#app-header nav .theme-toggle button` with `width: auto` on the group, the Caption `0.75rem` size, resting `#fff` text on a `rgba(255,255,255,0.4)` border matching `#logout-btn`, and a pressed state of `var(--pico-primary-background)`/`var(--pico-primary-inverse)`; `getComputedStyle` (not visual inspection) confirms the resting and pressed colours actually apply over Pico's `[role=group] [type=button]` rules in both themes; the nav has no horizontal overflow at 1400px, 768px, 480px and 390px in both themes, applying the plan's pre-approved `@media (max-width: 480px)` account-`<ul>` wrap fallback if it does.
- [ ] Record the dark theme in DESIGN.md and the design.json sidecar — file(s): DESIGN.md, .impeccable/design.json — done when: DESIGN.md's frontmatter `colors:` gains the 14 `dark-`-prefixed entries with the as-built measured values and the existing 12 are unchanged (no key is added for the dark primary fill or its ink — the fill *is* `dark-signal` and the ink *is* `navy-950`, and the prose says so); the UNCONFIRMED question of whether the tooling accepts 26 prefixed keys is actually checked by the `/impeccable` run below rather than assumed, with the prose-only fallback taken and explained in DESIGN.md if it does not; a `### Dark theme` subsection under `## Colors` carries the role→value table with measured ratios and the new Constant-Hue Rule; the `Inputs / Fields` "Light-only, deliberately" note is updated to say the unconditional declarations are now the no-JS fallback while the real defence is that `data-theme` is always set explicitly (keeping the existing `color-scheme` correction verbatim); `## Elevation & Depth` records that Pico's dark block re-enables `--pico-card-box-shadow`; `## Navigation` documents the theme control; two new Do's cover sweeping both theme blocks together and keeping density tokens out of theme blocks; `.impeccable/design.json` gains matching `colorMeta` entries and refreshed CSS snapshots for the Slug Chip and Nav Shell components; an `/impeccable` run over `gui/` (not a bare `detect.mjs` call — that detector is not runnable from a clean checkout) reports no new findings versus the 2 known false positives, or any new finding is fixed or recorded with a reason.
- [ ] Update CLAUDE.md and PRODUCT.md for the theming architecture — file(s): CLAUDE.md, PRODUCT.md — done when: CLAUDE.md's Architecture bullet says the `gui` component serves 12 exact routes (not 11) and notes `/theme-init.js` is loaded by every page rather than page-scoped; a new Theming section documents the three-block structure in `theme.css`, why the light block's selector must stay exactly `:root:not([data-theme="dark"])`, that `theme-init.js` always sets an explicit `data-theme` (which disables Pico's `prefers-color-scheme` block entirely), the `ss-theme` localStorage key and its three values, the no-JS-falls-back-to-light behaviour, and the two Pico dark-block traps (re-enabled card/dropdown shadow, card border set equal to the card background); PRODUCT.md's Capabilities and Brand Commitments each gain one accurate line.
- [ ] End-to-end manual verification of light/dark theming — file(s): (none — verification step) — done when: with `spin up --build --runtime-config-file runtime-config.toml` running, all 5 pages are loaded in a real browser in both themes with the console open and show zero errors of any kind (in particular zero CSP violations); every measurement in the plan's Verification step 6 is taken live with `getComputedStyle` against each element's real rendered background and recorded with its number; switching Auto/Light/Dark repaints immediately and persists across navigation and a hard reload; with the mode on Auto, flipping the OS appearance repaints the open page without a reload; setting `localStorage["ss-theme"] = "banana"` and reloading resolves to the OS preference without throwing; a full flow (log in → create a link with More options → sort → filter → edit row → save → detail page with QR, per-day and recent-events tables → Manage users → create and delete a user → log out) completes in dark mode; the native `datetime-local` picker, the role `<select>` and the search box all render dark; `curl -s -o /dev/null -w '%{http_code}' http://localhost:3000/theme-init.js` returns 200; `cd gui-pages && uv run pytest`, `cd api && uv run pytest`, and `cd redirect && go test ./linkgate/...` all pass.
```

## Critical files

- `gui/theme.css`
- `gui/theme-init.js` (new)
- `gui/app.js`
- `gui/index.html`
- `gui/login.html`
- `gui/dashboard.html`
- `gui/admin/users.html`
- `gui/links/detail.html`
- `spin.toml`
- `gui-pages/tests/test_routing.py`
- `gui-pages/tests/test_no_inline_code.py`
- `DESIGN.md`
- `.impeccable/design.json`
- `CLAUDE.md`
- `PRODUCT.md`

Not touched, deliberately: `gui-pages/routing.py` (no CSP or route change),
`gui/dashboard.css` / `gui/admin/users.css` / `gui/links/detail.css` (confirmed
by reading all three — every colour they use is already a `--pico-*` token that
follows the theme, with no hardcoded hex anywhere), all page `.js` files
(confirmed by grep — no colour logic; the only `style.` writes are the
CSP-safe `display` toggles the CSP plan deliberately left alone), `api/`,
`redirect/`, `Jenkinsfile`.

## Verification

Run in this order.

1. **Static checks after each `theme.css` task** — the light theme must not move
   until the dark block exists:

   ```bash
   cd /Users/jhostetler/git/tirerack/spin-shortener
   grep -n 'data-theme' gui/theme.css        # expect exactly 3 selector sites
   grep -c 'style="' gui/app.js gui/theme-init.js   # both must print 0
   ```

2. **The Python suites.** `gui-pages` is the meaningful signal; `api` must be
   untouched:

   ```bash
   cd gui-pages && uv run pytest
   cd ../api && uv run pytest
   ```

3. **The Go suite**, to prove nothing reached the redirect component:

   ```bash
   cd redirect && go test ./linkgate/...
   ```

   Never `go test ./...` / `go build ./...` / `go vet ./...` — they fail by
   design on `package main`.

4. **Run the real app** (the user runs this; the builder does not):

   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```

5. **Confirm the new route resolves.** A 404 here means the route, not the file,
   and its symptom is a silently un-themed app:

   ```bash
   curl -s -o /dev/null -w '%{http_code} %{content_type}\n' \
     http://localhost:3000/theme-init.js
   ```

   Pass: `200` with a JavaScript content type.

6. **Live contrast measurement in a real browser (Playwright), dark mode.** This
   is the step that cannot be skipped or desk-calculated. This project has twice
   shipped a colour measured against the wrong assumed background — `ok-green`
   (4.39:1 against the table cell's real `--pico-background-color`, not the
   white card) and `badge-slate` (clearing AA by 0.27:1) — and a second palette
   doubles the surface. For each element below, resolve its **real rendered
   background** by walking up ancestors to the first non-transparent
   `backgroundColor`, then compute the ratio from the two `getComputedStyle`
   values. Do not assume the card colour.

   | Element | Where | Minimum |
   |---|---|---|
   | body prose (`article p`) | any card | 4.5:1 |
   | `h1`, `h2`, `h3` | dashboard, users, detail | 4.5:1 |
   | `.status-badge.status-active` | a links-table `<td>` | 4.5:1 |
   | `.status-badge.status-disabled` | a links-table `<td>` | 4.5:1 |
   | `.slug-kind-badge`, `.lock-badge` | a links-table `<td>` (needs a custom slug and a password-protected link seeded) | 4.5:1 |
   | `.not-yet-live` | a links-table `<td>` (needs a future `start_at`) | 4.5:1 |
   | `.expiring-soon` / `.expired` | a links-table `<td>` | 4.5:1 |
   | `.form-error` | login page and dashboard card | 4.5:1 |
   | `.form-success` | dashboard after a create | 4.5:1 |
   | `.empty-state` (muted) | an empty table | 4.5:1 |
   | destination `<a>` link text | detail page card | 4.5:1 |
   | primary button label ("Shorten") on its fill | dashboard card | 4.5:1 — expect 7.87:1 in dark (Deep Navy ink), 5.18:1 in light (white ink) |
   | `.slug-chip` text at rest, and on `:hover` | detail-page `h1` and a table row | 4.5:1 |
   | `.identity-name`, `.identity-role`, `.nav-page-label`, `.nav-separator` | nav | 4.5:1 (`.nav-separator` is `aria-hidden`; record it anyway) |
   | `.identity-avatar` letter | nav | 4.5:1 |
   | theme-control button labels, resting and pressed | nav | 4.5:1 |
   | input text and `::placeholder` | create-link form, resting **and focused** | 4.5:1 |
   | `:focus-visible` outline | vs a card and vs the nav fill | 3:1 |
   | primary button fill | vs its card background | 3:1 — expect 6.76:1; **anything under 3.3:1 is a finding, not a pass**, since this value was deliberately re-chosen for headroom (see Trade-offs) |
   | card border | vs card and vs page (informational — record, compare against light's 1.36 / 1.20) | — |

   Also assert, in the same pass:
   - `getComputedStyle(document.documentElement).colorScheme === "dark"`.
   - Every `<article>` on every page has computed `boxShadow === "none"` (Pico's
     dark block re-enables it — this is the most likely silent regression).
   - `getComputedStyle(document.documentElement).getPropertyValue("--pico-font-size").trim() === "100%"`
     and `--pico-spacing` is `0.75rem`, in **both** themes.
   - The sticky action column's computed `backgroundColor` is fully opaque in
     dark mode (it reads `--pico-card-background-color`), and content scrolled
     under it does not show through — check at 1400px and 390px on both tables.
   - The `#qr-preview` PNG is legible on the dark card (the UNCONFIRMED item);
     if it renders transparent-backgrounded, apply the pre-approved
     light-background-plus-padding fix in `gui/links/detail.css` for both themes.

7. **Behaviour checks in the browser**, both themes:
   - Load all 5 pages with the console open: zero errors, zero CSP violations.
   - With the OS in dark and no stored preference, `/login.html` paints dark on
     first load — watch for a light flash, including on a hard reload with cache
     disabled.
   - Click Light, then navigate dashboard → detail → users → dashboard: dark
     never reappears. Repeat for Dark. Click Auto and confirm it returns to the
     OS preference.
   - With Auto selected, flip the OS appearance while the page is open: it
     repaints without a reload.
   - Set `localStorage["ss-theme"] = "banana"`, reload: the app resolves to the
     OS preference and does not throw.
   - Walk the full flow in dark mode: log in → create a link (More options
     expanded, custom slug, a time window, a password) → sort a column → filter
     → open an edit row, save, cancel → open the detail page (QR renders,
     per-day and recent-events tables populate) → Manage users → create a user →
     trigger the admin-promotion confirm dialog (check the `<dialog>` and its
     backdrop in dark) → delete the user → log out.
   - Check the native controls specifically: open a `datetime-local` picker, the
     role `<select>`, and the search box in dark mode — `color-scheme: dark`
     should render the native popups dark rather than light-on-dark.
   - Keyboard-only pass over the nav: Tab reaches all three theme buttons, the
     focus ring is clearly visible against the navy fill, and Enter/Space
     activates.

8. **The design detector — via an `/impeccable` run over `gui/`, not a bare
   `detect.mjs` invocation.** That detector ships with the impeccable tooling
   rather than living in this repo; a previous plan's `detect.mjs --json gui/`
   instruction was corrected in `TASKS.md` for exactly this reason (it is not
   runnable from a clean checkout). Compare against the known baseline (2 known
   false positives). New findings are plausible — the dark block introduces raw
   hex values — and each must be judged rather than assumed benign.

## Out of scope / follow-ups

Each of the first four belongs under `TASKS.md`'s "Future work (not scheduled)"
and is added there:

- **A theme control on `login.html`.** The page has no persistent nav, so it
  needs a second rendering site for the control. A stored preference already
  applies there; only the ability to change it from the login screen is missing.
  Pick this up if anyone actually asks.
- **Cross-tab theme sync via the `storage` event.** ~4 lines in `theme-init.js`.
  Left out because every navigation already re-reads `localStorage`, so the
  inconsistency only exists between two simultaneously-open tabs until one of
  them navigates.
- **The light theme's slug-chip hover contrast (3.00:1).** Pre-existing, found
  during this plan's measurement work, deliberately not fixed here because
  fixing it means changing a light-theme colour. Fixing it means either
  lightening `--ss-chip-hover-fg` in the light block or darkening
  `--ss-chip-hover-bg` — both now single-token changes thanks to task 2.
- **Extending `test_no_inline_code.py`'s template checks to `dashboard.js`,
  `admin/users.js` and `links/detail.js`.** Those three build markup with
  `innerHTML` exactly as `app.js` does but are not covered by the guard today —
  a pre-existing gap, widened in visibility by this change adding a fourth
  covered file. Small, mechanical, and independent of theming.

Not follow-ups, just excluded:

- **Any CSP directive change.** The chosen approach needs none, and
  `img-src 'self' data:` in particular stays load-bearing for Pico's
  chevron/search/calendar affordances.
- **`redirect`'s password-prompt page.** It renders its own HTML with its own
  CSP and is not part of `gui/`; theming it would mean adding a stylesheet and a
  route to the minimal hot path, which is already its own deferred entry.
- **A high-contrast or "dim" third palette**, and per-user server-side theme
  storage. Both are materially larger changes; neither has been asked for.
