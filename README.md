# spin-shortener

A self-hosted URL shortener built as a polyglot [Spin](https://spinframework.dev) WebAssembly application — a Go component for fast redirects, a Python component for the API, a second Python component serving the GUI's HTML pages, and a static file server for its JS/CSS.

## Features

- Auto-generated short links, or custom slugs for permitted users
- Optional per-link password protection
- Optional start/end time windows — a link only redirects within its scheduled window
- QR codes (SVG/PNG, web/print sizes) for every link
- Click analytics: totals and per-day counts
- Local username/password login with sessions, CSRF protection, and admin user management (roles + fine-grained permissions)
- Bulk link creation and bulk delete/enable/disable/tag/reassign/repoint/schedule actions
- Free-form link tags, with ownership reassignment (e.g. when an employee leaves)
- Admin-managed destination URL policy (allow/deny rules on what a link may point at)
- Light/dark theme, auto-following the OS with a manual override
- Multiple public base domains, selectable per viewer, applied to every short URL/QR code
- KV backup and restore, a consistency check with automated repair, and an orphaned-analytics purge tool
- Styled 404/500/503 error pages and password-prompt page (no raw error strings)

## Quick start

Requires the [Spin CLI](https://spinframework.dev), Go, and [uv](https://docs.astral.sh/uv/).

```bash
SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<some-password> spin up --build --runtime-config-file runtime-config.toml
```

This builds and serves all four components together at `http://localhost:3000`. On first run it seeds one admin user (`admin` / your chosen bootstrap password) — log in at `/login.html` to create your first short link.

See [CLAUDE.md](CLAUDE.md) for architecture details, the full command reference (including running each component's test suite), and the accepted security tradeoffs of the current design. See [TASKS.md](TASKS.md) for the phase-by-phase build history.

## Status

Feature-complete and **deployed to Akamai Functions** since 2026-08-06. The multi-store KV architecture that once blocked deployment was consolidated onto Spin's single `default` store; see CLAUDE.md's "Deployment: Akamai Functions" section for the measured quotas and operating limits that came out of running it there.
