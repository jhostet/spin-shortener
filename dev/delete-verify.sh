#!/usr/bin/env bash
# The ghost-DELETE harness: create a link, DELETE it, and re-check whether it
# is really gone — recording the DELETE's own status code and tracing every
# request with X-SS-Debug. Built for the filed report of a single-link
# DELETE that once returned 200 without deleting (seen once 2026-08-18, not
# reproduced in 10 attempts). See docs/plans/observable-kv-failures.md.
#
# Usage:
#   ./dev/delete-verify.sh [N] [BASE_URL]
#
#   N defaults to 25. BASE_URL defaults to http://localhost:3000 for a local
#   `spin up` run; pass the deployed app's URL to run this against Akamai
#   (in which case the deploy-secrets file supplies credentials instead —
#   see the deploy-secrets-location memory).
#
# THE TRAP THIS SCRIPT MUST NOT FALL INTO: TASKS.md records that
# GET /r/{slug} can keep answering 302 for a SUB-SECOND window after a
# successful delete (eventual consistency on Akamai's KV) and that this
# self-heals. That is documented, expected behaviour, NOT the anomaly this
# script exists to catch — so staleness observed immediately or at +2s is
# only ever printed as informational. ONLY a record still present at +10s
# counts as the anomaly, and only then does this script fail.
#
# On the first anomaly, exits non-zero, printing the slug, every status
# code recorded for that cycle, and the `spin aka logs` command to pull the
# matching traces (only useful against a deployed app with a known
# log_debug_token).
set -euo pipefail

N=${1:-25}
BASE_URL=${2:-http://localhost:3000}

case "$N" in
  ''|*[!0-9]*) echo "usage: delete-verify.sh [N] [BASE_URL]  (N must be a positive integer, got '$N')" >&2; exit 2 ;;
esac
[ "$N" -gt 0 ] || { echo "usage: delete-verify.sh [N] [BASE_URL]  (N must be a positive integer)" >&2; exit 2; }

case "$BASE_URL" in
  http://*|https://*) ;;
  *) echo "error: BASE_URL must start with http:// or https:// (got '$BASE_URL')" >&2; exit 2 ;;
esac

OUT=$(mktemp -d)
DEBUG_TOKEN="${SPIN_VARIABLE_LOG_DEBUG_TOKEN:-}"
ADMIN_PASSWORD="${SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD:-}"

# Against a deployed app, credentials come from the operator's chmod-600
# deploy-secrets file, exactly like dev/bulk-concurrent.sh and
# dev/kv-read-pressure.sh. Against a local `spin up`, the caller is expected
# to have exported SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD (and optionally
# SPIN_VARIABLE_LOG_DEBUG_TOKEN) into this shell themselves, since local runs
# have no secrets file at all.
if [ "$BASE_URL" != "http://localhost:3000" ] && [ -z "$ADMIN_PASSWORD" ]; then
  SECRETS=~/.claude/projects/-Users-jhostetler-git-tirerack-spin-shortener/deploy-secrets.env
  if [ ! -r "$SECRETS" ]; then
    echo "cannot read $SECRETS — see the deploy-secrets-location memory" >&2
    exit 1
  fi
  # shellcheck disable=SC1090
  set -a; source "$SECRETS"; set +a
  ADMIN_PASSWORD="$SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD"
  DEBUG_TOKEN="${SPIN_VARIABLE_LOG_DEBUG_TOKEN:-}"
  BASE_URL="${APP_URL:-$BASE_URL}"
fi
if [ -z "$ADMIN_PASSWORD" ]; then
  echo "error: SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD is not set — export it for a local run" >&2
  exit 1
fi

echo "running $N create/delete/verify cycles against $BASE_URL"
echo "artifacts in $OUT"
echo

LOGIN=$(curl -s -c "$OUT/cookies" -X POST "$BASE_URL/api/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$ADMIN_PASSWORD\"}")
CSRF=$(printf '%s' "$LOGIN" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("csrf_token",""))')
if [ -z "$CSRF" ]; then echo "login failed: $LOGIN" >&2; exit 1; fi

# probe(label) prints "label:status" for GET /api/links/<slug> and
# GET /r/<slug> (redirects disabled so a 302 doesn't get followed), tracing
# both with X-SS-Debug.
probe() {
  local label="$1" slug="$2"
  local api_code r_code
  api_code=$(curl -s -o /dev/null -w '%{http_code}' -b "$OUT/cookies" \
    -H "X-SS-Debug: $DEBUG_TOKEN" "$BASE_URL/api/links/$slug")
  r_code=$(curl -s -o /dev/null -w '%{http_code}' --max-redirs 0 \
    -H "X-SS-Debug: $DEBUG_TOKEN" "$BASE_URL/r/$slug")
  # The human-readable line goes to stderr, NOT stdout — every caller
  # captures this function's stdout via $(probe ...) to get api_code back,
  # and stdout can only carry one of the two without this split.
  echo "    $label: GET /api/links/$slug -> $api_code, GET /r/$slug -> $r_code" >&2
  printf '%s' "$api_code"
}

fail() {
  local slug="$1"
  shift
  echo
  echo "ANOMALY on slug $slug: record still present at +10s" >&2
  echo "recorded status codes: $*" >&2
  echo "pull the matching traces with:" >&2
  echo "  spin aka logs --app-name \"\$APP_NAME\" --since 15m -n 500 | grep \"$slug\"" >&2
  exit 1
}

for i in $(seq 1 "$N"); do
  CREATE=$(curl -s -b "$OUT/cookies" -X POST "$BASE_URL/api/links" \
    -H 'Content-Type: application/json' -H "X-CSRF-Token: $CSRF" \
    -H "X-SS-Debug: $DEBUG_TOKEN" \
    -d "{\"target_url\":\"https://example.com/delete-verify/$i\"}")
  slug=$(printf '%s' "$CREATE" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("slug",""))')
  if [ -z "$slug" ]; then
    echo "cycle $i: create failed: $CREATE" >&2
    exit 1
  fi

  delete_code=$(curl -s -o /dev/null -w '%{http_code}' -b "$OUT/cookies" \
    -X DELETE -H "X-CSRF-Token: $CSRF" -H "X-SS-Debug: $DEBUG_TOKEN" \
    "$BASE_URL/api/links/$slug")

  echo "cycle $i: slug=$slug DELETE=$delete_code"
  immediate_api=$(probe "immediate" "$slug")
  sleep 2
  plus2_api=$(probe "+2s" "$slug")
  sleep 8
  plus10_api=$(probe "+10s" "$slug")

  # Only a record still present (200, not 404) at +10s is the anomaly —
  # sub-second/low-second staleness self-heals and is expected (TASKS.md's
  # M2 section) and must never trip this check.
  if [ "$plus10_api" = "200" ]; then
    fail "$slug" "DELETE=$delete_code immediate=$immediate_api +2s=$plus2_api +10s=$plus10_api"
  fi
done

echo
echo "PASS: $N clean create/delete/verify cycles, no record survived to +10s."
