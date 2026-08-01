#!/usr/bin/env bash
# Runs the whole app locally WITH Fermyon's KV explorer at
# /internal/kv-explorer/ — see docs/plans/kv-explorer.md.
#
# spin.toml never mentions the explorer. This regenerates a throwaway,
# gitignored spin-dev.toml from spin.toml + dev/kv-explorer.toml on every run,
# so the dev manifest cannot drift from the real one. Edit spin.toml or
# dev/kv-explorer.toml — never spin-dev.toml.
#
# `set -u` is deliberately omitted: macOS's system bash 3.2 treats "$@" with no
# positional parameters as an unbound variable. The :? guards below cover the
# variables that actually matter.
set -eo pipefail

cd "$(dirname "$0")/.."

: "${SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD:?must be set (seeds the first admin user)}"
: "${SPIN_VARIABLE_KV_EXPLORER_PASSWORD:?must be set (KV explorer basic-auth password; username defaults to 'kv')}"

{
  echo "# GENERATED FILE — DO NOT COMMIT, DO NOT DEPLOY."
  echo "# spin.toml + dev/kv-explorer.toml, rebuilt by dev/kv-explorer-up.sh."
  echo "# Every local run overwrites this file; edit the two sources instead."
  echo
  cat spin.toml
  echo
  cat dev/kv-explorer.toml
} > spin-dev.toml

exec spin up -f spin-dev.toml --build --runtime-config-file runtime-config.toml "$@"
