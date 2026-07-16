# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

`spin-shortener` is a polyglot WebAssembly application built on [Spin](https://spinframework.dev) (Fermyon's WASI HTTP framework). It is an early-stage scaffold for a URL shortener with three independently-built components, each in its own language runtime, composed via `spin.toml`.

## Architecture

`spin.toml` is the single source of truth for routing and build wiring. It defines three HTTP triggers, each mapped to a separate Wasm component:

- `route = "/r/..."` → **`redirect`** component (`redirect/`, Go) — intended to handle short-link resolution/redirects. Built with `go tool componentize-go build`, compiling `redirect/main.go` to `redirect/main.wasm`. Uses `github.com/spinframework/spin-go-sdk/v3/http` and registers a handler via `spinhttp.Handle`. `allowed_outbound_hosts = []` — currently no outbound network access is permitted from this component; expand this list in `spin.toml` if the redirect logic needs to call the API or an external store.
- `route = "/api/..."` → **`api`** component (`api/`, Python) — intended to hold the shortener's data/API logic. Built with `uv run componentize-py -w spin:up/http-trigger@4.0.0 componentize app -o app.wasm`, compiling `api/app.py` (module name `app`) to `api/app.wasm`. Uses `spin_sdk.http.Handler`.
- `route = "/..."` (catch-all) → **`gui`** component — a prebuilt static file server (`spin_static_fs.wasm`, fetched by digest from the `spin-fileserver` GitHub release) that serves static files from the `gui/` directory. `gui/` is currently empty — add static assets (HTML/JS/CSS) there for the frontend.

Each component is built independently and only in its own `workdir` (`redirect/` or `api/`); there is no shared build step or root-level package manifest. When editing one component, you generally don't need to touch the others' toolchains.

**Why Go for `redirect` but Python for `api`/`gui`:** the redirect path is the hot path (every short-link click) and is written in Go for raw performance. The `api`/`gui` surfaces (link creation, management, frontend) aren't on that hot path, so they're written in Python to prioritize developer velocity and code understandability over raw speed — the performance tradeoff isn't worth it there. Keep this split in mind when adding new functionality: if it's on the redirect hot path, it likely belongs in the Go component; otherwise default to Python for velocity.

Both `redirect/main.wasm` and `api/app.wasm` are build artifacts and are gitignored — they must be rebuilt via `spin up --build` (or the per-component build commands in `spin.toml`) after any source change; they are not checked into the repo.

## Task tracking
- Maintain a `TASKS.md` file in the repo root as the single source of truth for multi-step work.
- Each task uses the format: `- [ ] Task name — file(s): path/to/file — done when: <criteria>`
- Before starting any task, re-read TASKS.md.
- After finishing a task, immediately update its checkbox in TASKS.md before starting the next one — don't batch updates at the end.
- If context is compacted or a new session starts, re-read TASKS.md before doing anything else.

## Commands

Build and run the whole app (all three components) locally:

```bash
SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<some-password> spin up --build --runtime-config-file runtime-config.toml
```

This invokes each component's `[component.<name>.build]` command from `spin.toml` and then serves all routes together. Requires the [Spin CLI](https://spinframework.dev) to be installed.

`--runtime-config-file runtime-config.toml` is required locally: Spin does not auto-provision named (non-`default`) key-value stores, so the `links`/`users`/`analytics` stores declared in `spin.toml` must be mapped to a backing provider via `runtime-config.toml` (sqlite-backed `type = "spin"` for local dev). `admin_bootstrap_password` is a required secret variable (seeds the first admin user on a fresh KV store) and has no default, so it must be supplied via env var (or another Spin variable provider) on every run.

When testing the `gui` in a real browser over plain `http://localhost`, also set `SPIN_VARIABLE_COOKIE_SECURE=false` — the session cookie's `Secure` flag otherwise stops the browser from storing/sending it over non-HTTPS, breaking login. Leave `cookie_secure` at its default `true` for any HTTPS deployment.

Per-component builds (equivalent to what `spin up --build` runs), if you need to build just one component while iterating:

```bash
# redirect (Go) — from redirect/
go tool componentize-go build

# api (Python) — from api/
uv run componentize-py -w spin:up/http-trigger@4.0.0 componentize app -o app.wasm
```

Python component dependencies (`componentize-py`, `spin-sdk`) are managed by [`uv`](https://docs.astral.sh/uv/) and pinned in `api/pyproject.toml`/`api/uv.lock`. `uv run` syncs `api/.venv` from the lockfile automatically before running, so no manual install step is required — even on a fresh clone, `spin up --build` just works. (To set up the environment yourself, e.g. for editor/language server support, run `uv sync` from `api/`.)

There are no tests, linters, or CI configured yet in this repository.
