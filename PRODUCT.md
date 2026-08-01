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

Built as a polyglot Spin (WASM) app: a Go component for the hot-path redirect, a Python component for the authoring/auth/analytics API, and a static HTML/JS admin frontend (`gui/`). Currently self-hosted/local (`spin up --build`); not yet deployed to a production host — Akamai Functions is the intended target but is currently blocked on this app's multi-store KV architecture (see CLAUDE.md). Users authenticate via local username/password sessions; admins manage other users' roles and permissions through the same frontend.

## Capabilities and Constraints

- Auto-generated short links, or custom slugs for users with the `links.create_custom_slug` permission
- Optional per-link password protection (one-way hashed — not recoverable/displayable once set)
- Optional start/end scheduling windows; outside the window a link 404s indistinguishably from a nonexistent slug
- QR codes (SVG/PNG) per link
- Click analytics: accurate running totals and per-day counts, plus a best-effort (lossy, not complete) recent-events sample
- Local auth with sessions; admin role + fine-grained permission system (e.g. `links.view_all`)
- No outbound network access from either Wasm component by design — rules out external rate-limiting or abuse-detection services; login/link-password brute-force protection currently relies only on PBKDF2 cost, not attempt-throttling
- Slug existence and password-protection status are enumerable by a probing visitor (accepted, not treated as a vulnerability)
- Security response headers (CSP, HSTS, `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`) are set by every component that serves a response, added 2026-07-25. The GUI's CSP was hardened to `script-src 'self'; style-src 'self'` on 2026-07-31 by externalizing every page's inline script and style, with a test guarding against inline code returning. One `'unsafe-inline'` remains anywhere in the app — `style-src` on the redirect component's password-prompt page, for a single `style="color: red"` attribute — deliberately deferred, since that page has no external stylesheet

## Brand Commitments

No official Tire Rack brand guide applies to this tool. A dark theme is the preferred stylistic direction, but the current dark-navy palette (`gui/theme.css`, layered on Pico CSS) is a placeholder the team chose, not an official identity to preserve — free to evolve.

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
