# spin-shortener

A self-hosted URL shortener built as a polyglot [Spin](https://spinframework.dev) WebAssembly application — a Go component for fast redirects, a Python component for the API, and a static HTML/JS frontend.

## Features

- Auto-generated short links, or custom slugs for permitted users
- Optional per-link password protection
- Optional start/end time windows — a link only redirects within its scheduled window
- QR codes (SVG/PNG, web/print sizes) for every link
- Click analytics: totals and per-day counts
- Local username/password login with sessions, CSRF protection, and admin user management (roles + fine-grained permissions)

## Quick start

Requires the [Spin CLI](https://spinframework.dev), Go, and [uv](https://docs.astral.sh/uv/).

```bash
SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<some-password> spin up --build --runtime-config-file runtime-config.toml
```

This builds and serves all three components together at `http://localhost:3000`. On first run it seeds one admin user (`admin` / your chosen bootstrap password) — log in at `/login.html` to create your first short link.

See [CLAUDE.md](CLAUDE.md) for architecture details, the full command reference (including running each component's test suite), and the accepted security tradeoffs of the current design. See [TASKS.md](TASKS.md) for the phase-by-phase build history.

## Status

Feature-complete for local/self-hosted use (Phases 1–5 of the original build-out). Not yet deployable to Akamai Functions as-is — see CLAUDE.md's "Deployment" section for why and what it would take.
