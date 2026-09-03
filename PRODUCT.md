# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary users are Tire Rack's marketing/campaign team, creating and tracking short links for campaigns, ads, and promotions — they care most about custom slugs, scheduled (time-windowed) links, and click analytics. A secondary, smaller admin population manages user accounts, roles, and fine-grained permissions (who may create custom slugs, view all links, etc.), a distinct concern from day-to-day link creation.

## Product Purpose

A self-hosted URL shortener that gives the marketing team the core feature set of a commercial shortener (custom slugs, scheduling, password protection, QR codes, click analytics) without recurring per-seat SaaS licensing cost.

## Positioning

Cost avoidance versus a commercial shortener (e.g. bit.ly-style per-seat licensing), not a data-control or privacy driver — self-hosting is justified here primarily because it's cheaper to run once built, for a well-understood, boundable feature set.

## Operating Context

Built as a polyglot Spin (WASM) app across four components: a Go component for the hot-path redirect, a Python component for the authoring/auth/analytics API, a second Python component serving the GUI's HTML pages (so security headers can be attached to the navigated document), and a prebuilt static file server for the frontend's JS/CSS assets (`gui/`). **Deployed to Akamai Functions since 2026-08-06**, and measured there regularly since — the multi-store KV architecture that used to block this was consolidated onto Spin's single `default` store, with the three logical namespaces (links, users, analytics) kept apart by key prefixes instead of by store. The same app still runs locally under `spin up --build` for development. Users authenticate via local username/password sessions; admins manage other users' roles and permissions through the same frontend.

## Capabilities and Constraints

- Auto-generated short links, or custom slugs for users with the `links.create_custom_slug` permission
- Optional per-link password protection (one-way hashed — not recoverable/displayable once set)
- Optional start/end scheduling windows; outside the window a link 404s indistinguishably from a nonexistent slug
- QR codes (SVG/PNG) per link
- Bulk link management on the dashboard: create up to 50 links at once from a pasted list or an uploaded .csv/.tsv/.txt file (batch-level password/schedule, all-or-nothing validation with a per-row error report), plus multi-select Delete and Enable/Disable of up to 50 existing links at once. A single link's status can also be flipped straight from its row in the table, without going through the multi-select path. Selected links can also be bulk-rescheduled (set or clear their start/expiry window) and bulk-repointed to a new destination in one action
- Click analytics: accurate running totals and per-day counts. (A best-effort recent-events sample — timestamp, referrer, device class per click — shipped and was later withdrawn on 2026-08-18: it doubled the app-wide write-rate cost of every recorded click for a lossy, never-complete anecdote nobody was confirmed to be reading. See `docs/plans/drop-events-write.md`.)
- Local auth with sessions; admin role + fine-grained permission system (e.g. `links.view_all`)
- No outbound network access from either Wasm component by design — rules out external rate-limiting or abuse-detection services; login/link-password brute-force protection currently relies only on PBKDF2 cost, not attempt-throttling
- Slug existence and password-protection status are enumerable by a probing visitor (accepted, not treated as a vulnerability)
- Admin-managed rules on where links may point, as an allow-list, a deny-list, or both, matching a domain and its subdomains. Enforced everywhere a link can be created or edited, including bulk. Existing links that break a newly-added rule are listed for review rather than silently disabled, and links created before a rule keep working
- An admin-run store consistency check that reports where the data has drifted out of step with itself — six findings covering a link owned by an account that no longer exists, a user record missing from the user index (or indexed with no record), a session belonging to a deleted user, a value that will not parse, and a key matching no known shape. The check itself only reads; three of the six have exactly one safe, derivable fix and offer a one-click Repair alongside them. The other three are never auto-repaired because fixing them needs a judgement call — those name the ordinary reassign/delete tools instead. (Six further checks about link-index drift were retired in August 2026 when the indexes themselves were removed: a link record's existence is now the only truth, so there is no second copy left to drift from.)
- Deleting a user account is refused while that user still owns links: the operator is told how many and sent to a dashboard pre-filtered to exactly those links, to reassign or delete them first. Deletion also revokes the account's sessions immediately, so reusing a username later is safe — neither the old account's links nor its still-valid cookies carry over to the new one
- Free-form tags on links (up to 10 each), set individually or applied to a whole batch, with a dashboard tag filter, tag autocomplete, and bulk add/remove behind a `links.tag` permission. Links can also be reassigned to a different owner in bulk — gated on `users.manage`, and deliberately not requiring edit rights on the links themselves, since the case it exists for is a departed employee's orphaned links
- Operator-run backup and restore of all three data namespaces (links, users, analytics — one physical KV store since the consolidation) from the admin UI: download a JSON snapshot (or a subset of stores), and replace the stores from one after a typed confirmation. The file deliberately contains no account password hashes and no sessions, so restored accounts can't sign in until an admin sets each one a new password (the users table flags them). It does still contain link password hashes, so that restoring keeps protected links protected — which makes the file sensitive and worth storing somewhere access-controlled. Restore is all-or-nothing and replaces rather than merges
- Security response headers (CSP, HSTS, `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`) are set by every component that serves a response, added 2026-07-25. The GUI's CSP was hardened to `script-src 'self'; style-src 'self'` on 2026-07-31 by externalizing every page's inline script and style, with a test guarding against inline code returning; the redirect component's password-prompt page followed on 2026-08-01, so there is now **no `'unsafe-inline'` anywhere in the app**
- Every error a visitor can land on is a styled page rather than a bare server string, on both public surfaces: a dead, disabled or out-of-window short link, a link the server temporarily could not look up, a corrupt link record, and a mistyped or truncated URL that misses the app's pages entirely. The short-link 404 is deliberately identical whatever the underlying reason, so a visitor probing for links cannot tell a nonexistent slug from a disabled or scheduled one; the mistyped-URL page is free to be more specific, and points out that a short link's path starts with `/r/`, which is the common way a copied link gets truncated
- The GUI ships both a light and a dark theme, following the OS's `prefers-color-scheme` by default with a manual Auto/Light/Dark override in the persistent nav, persisted client-side only (`localStorage`, no server-side or per-user storage)
- The app can be reachable at more than one base domain (configured via the `public_base_urls` Spin variable); a nav selector lets each viewer pick which one every short URL, Copy, CSV export and QR code is built from, optionally narrowed per-user via `assigned_domains` — a display preference only, gating nothing server-side
- A link can optionally be restricted to a subset of the configured domains (`allowed_domains`), enforced server-side by `redirect` on every request via the incoming `Host` header — this is a real, hot-path restriction, not a display preference, and is a different concept from `assigned_domains` above. Absent or empty means unrestricted, matching every link that existed before this shipped. A domain mismatch reads as a plain 404, identical to a nonexistent, disabled or out-of-window link — a restricted link's existence and password-protection status are never disclosed on a domain it does not serve. Settable at creation, editable per-link, or applied/cleared across a bulk selection
- Deleting a link never deletes its click data, which piles up as pure overhead — every dashboard load scans it and can never read it again. An admin-run tool on the Store maintenance page reports which deleted links' analytics are safe to remove and, gated on a lower-ceremony confirmation than backup/restore's since it can only ever delete data for links that no longer exist, purges it in bounded chunks
- The app publishes a `robots.txt` disallowing everything, including short-link (`/r/`) paths, because `redirect` reads no `User-Agent` header at all — a crawler that follows a short link is recorded as a real click that nothing downstream can tell apart from a person's, forever. This reduces bot-driven click inflation from well-behaved crawlers and link unfurlers only; it is not a defence against a hostile or `robots.txt`-ignoring enumerator

## Brand Commitments

No official Tire Rack brand guide applies to this tool. A dark theme was the preferred stylistic direction and now ships as a real, selectable theme (2026-07-31) — a custom dark-navy palette derived at constant hue from the same identity as the light theme, not Pico's stock dark preset. The dark-navy palette itself (`gui/theme.css`, layered on Pico CSS, in both its light and dark forms) remains a placeholder the team chose, not an official identity to preserve — free to evolve.

## Evidence on Hand

None on hand — no customer testimonials, campaign case studies, or usage metrics have been captured for this product record. Future work should not fabricate any.

## Product Principles

1. Keep the redirect path fast (Go) and everything else (authoring, admin, frontend) optimized for developer velocity and clarity (Python) — most work here isn't on the hot path and shouldn't be engineered as if it were.
2. Self-hosting only pays for itself if it stays operationally simple — avoid adding infrastructure (e.g. an external rate-limiter) to close a gap unless a real incident or requirement forces it.
3. Disclose accepted security/reliability tradeoffs rather than hiding them; close them when a genuine need arises, not preemptively.
4. Give the marketing team enough self-serve control (custom slugs, scheduling, QR, analytics) that routine link operations don't need engineering involvement.
5. Keep admin (user/role/permission management) visually and functionally distinct from everyday link-creation workflows.

## Accessibility & Inclusion

No accessibility requirement has been established for this internal tool beyond what Pico CSS provides by default.
