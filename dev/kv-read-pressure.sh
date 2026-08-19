#!/usr/bin/env bash
# Burst-shaped read-cap provoker: fires N parallel GET /api/admin/backup
# requests, each a measured ~999-operation `gather_reads` fan-out
# (CLAUDE.md's "Parallel KV reads" table) — the only burst-shaped read load
# this app can generate on demand, and the shape that DID throttle on
# 2026-08-15 (10 parallel x 200 gathered single reads = 2,000 reads/s, 9/10
# throttled with Error_Other('too many requests')). See
# docs/plans/observable-kv-failures.md.
#
# Usage:
#   ./dev/kv-read-pressure.sh [N]
#
#   N defaults to 10 — the parallelism that produced the 9/10 throttle above.
#
# Prints every response's HTTP status and Server-Timing header. Traces every
# request with X-SS-Debug so a per-request line is emitted too, but the real
# payoff is unconditional: any read-side ev=kv_fail line this provokes
# (independent of log_level/X-SS-Debug — CLAUDE.md's "Toggleable structured
# logging") can be read back with:
#
#   spin aka logs --app-name "$APP_NAME" --since 15m -n 500 | grep 'ev=kv_fail'
#
# This is also runnable against a local `spin up` instance for a sanity
# check (there is no read cap locally, so no throttle is expected there —
# it is only meaningful against a deployed Akamai app).
set -euo pipefail

SECRETS=~/.claude/projects/-Users-jhostetler-git-tirerack-spin-shortener/deploy-secrets.env
if [ ! -r "$SECRETS" ]; then
  echo "cannot read $SECRETS — see the deploy-secrets-location memory" >&2
  exit 1
fi
# shellcheck disable=SC1090
set -a; source "$SECRETS"; set +a

if [ -z "${APP_URL:-}" ]; then
  echo "APP_URL is not set (expected from $SECRETS) — see the deploy-secrets-location memory" >&2
  exit 1
fi

N=${1:-10}
case "$N" in
  ''|*[!0-9]*) echo "usage: kv-read-pressure.sh [N]  (N must be a positive integer, got '$N')" >&2; exit 2 ;;
esac
[ "$N" -gt 0 ] || { echo "usage: kv-read-pressure.sh [N]  (N must be a positive integer)" >&2; exit 2; }

OUT=$(mktemp -d)
echo "firing $N parallel GET /api/admin/backup requests against $APP_URL"
echo "each is a measured ~999-operation gather_reads fan-out (CLAUDE.md's 'Parallel KV reads')"
echo "artifacts in $OUT"
echo

LOGIN=$(curl -s -c "$OUT/cookies" -X POST "$APP_URL/api/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD\"}")
CSRF=$(printf '%s' "$LOGIN" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("csrf_token",""))')
if [ -z "$CSRF" ]; then echo "login failed: $LOGIN" >&2; exit 1; fi

START=$(python3 -c 'import time; print(time.time())')
for i in $(seq 1 "$N"); do
  (
    curl -s -o "$OUT/resp-$i.json" -D "$OUT/hdr-$i.txt" -w '%{http_code}\n' \
      -b "$OUT/cookies" -X GET "$APP_URL/api/admin/backup" \
      -H "X-SS-Debug: ${SPIN_VARIABLE_LOG_DEBUG_TOKEN:-}" \
      > "$OUT/code-$i.txt"
  ) &
done
wait
END=$(python3 -c 'import time; print(time.time())')

echo "wall: $(python3 -c "print(f'{$END-$START:.1f}s')")"
echo

throttled=0
for i in $(seq 1 "$N"); do
  code=$(tr -d '\n' < "$OUT/code-$i.txt")
  printf 'request %s: HTTP %s\n' "$i" "$code"
  if [ "$code" != "200" ]; then
    throttled=$((throttled + 1))
    head -c 500 "$OUT/resp-$i.json" | sed 's/^/    body: /'
    echo
  fi
  grep -i '^server-timing' "$OUT/hdr-$i.txt" | sed 's/^/    /' || true
done

echo
echo "$throttled of $N requests were non-200"
echo "artifacts in $OUT"
echo "read the unconditional read-failure lines back with:"
echo "  spin aka logs --app-name \"\$APP_NAME\" --since 15m -n 500 | grep 'ev=kv_fail'"
