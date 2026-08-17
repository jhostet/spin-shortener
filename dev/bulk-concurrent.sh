#!/usr/bin/env bash
# Concurrent bulk-create load against /api/links/bulk, for measuring write-throttle
# behaviour and index drift. See TASKS.md's "DEPLOYED AND TRACED (2026-08-17)".
#
#   ./dev/bulk-concurrent.sh <requests> <rows-per-request> <slug-prefix> [outdir]
#
# Fires <requests> bulk creates CONCURRENTLY, each carrying <rows-per-request>
# rows, and prints every response's partial/count/index_updated/write_error plus
# its Server-Timing header. Reads credentials from the operator's chmod-600
# deploy-secrets file (see the deploy-secrets-location memory) and traces every
# request with X-SS-Debug, so the matching log lines can be read back with:
#
#   spin aka logs --app-name "$APP_NAME" --since 15m -n 200 \
#     | grep -o 'ss comp=api[^"]*' | grep links/bulk
#
# TWO REGIMES, AND THE SECOND IS THE ONE PEOPLE FORGET.
#
#   Above the cap  — e.g. 6 x 50 (~306 writes) crosses Akamai's 50 writes/second
#                    app-wide cap. Expect `200` with "partial": true, write_retry
#                    and write_failed in the log line. This exercises retry.
#
#   Under the cap  — e.g. 4 x 5 (~28 writes) does NOT throttle. Expect four clean
#                    `201`s with NO write_retry/write_failed field at all. This is
#                    the control, and it still produces `unindexed_link` findings,
#                    because `links:all_links` is a read-modify-write with no
#                    compare-and-swap and concurrent requests clobber each other.
#
# RUN BOTH BEFORE CONCLUDING ANYTHING ABOUT DRIFT. Measured 2026-08-17: a 6x50 run
# left 37 unindexed_link + 28 unindexed_owner_link, which reads like a throttling
# failure and is not one — the 4x5 control produced the same class of finding with
# nothing failing at all. Attributing drift to the throttle without the control is
# exactly the wrong conclusion, and this script exists so that is one command away.
#
# ALWAYS CHECK THE STORE AFTERWARDS, and clean up PACED, never concurrently:
#   GET  /api/admin/consistency
#   POST /api/links/bulk-action  {"slugs":[...],"action":"delete"}   (sequential batches)
#   POST /api/admin/consistency/repair  {"checks":[...],"confirm":"REPAIR"}
set -euo pipefail

SECRETS=~/.claude/projects/-Users-jhostetler-git-tirerack-spin-shortener/deploy-secrets.env
if [ ! -r "$SECRETS" ]; then
  echo "cannot read $SECRETS — see the deploy-secrets-location memory" >&2
  exit 1
fi
# shellcheck disable=SC1090
set -a; source "$SECRETS"; set +a

N=${1:?usage: bulk-concurrent.sh <requests> <rows-per-request> <slug-prefix> [outdir]}
R=${2:?usage: bulk-concurrent.sh <requests> <rows-per-request> <slug-prefix> [outdir]}
PREFIX=${3:?usage: bulk-concurrent.sh <requests> <rows-per-request> <slug-prefix> [outdir]}
OUT=${4:-$(mktemp -d)}
mkdir -p "$OUT"

WRITES=$(( N * (R + 2) ))
echo "firing $N concurrent ${R}-row bulk creates — $(( N * R )) links, ~$WRITES writes"
if [ "$WRITES" -gt 50 ]; then
  echo "  NOTE: ~$WRITES writes will cross the 50/second app-wide cap — this is the retry regime."
else
  echo "  NOTE: ~$WRITES writes stays under the 50/second cap — this is the CONTROL regime."
fi

LOGIN=$(curl -s -c "$OUT/cookies" -X POST "$APP_URL/api/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD\"}")
CSRF=$(printf '%s' "$LOGIN" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("csrf_token",""))')
if [ -z "$CSRF" ]; then echo "login failed: $LOGIN" >&2; exit 1; fi

for i in $(seq 1 "$N"); do
  python3 - "$OUT/body-$i.json" "$PREFIX" "$i" "$R" <<'PY'
import json, sys
path, prefix, idx, rows_n = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
rows = [f"{prefix}{idx}x{n:02d},https://example.com/{prefix}/{idx}/{n}" for n in range(1, rows_n + 1)]
open(path, "w").write(json.dumps({"text": "\n".join(rows)}))
PY
done

START=$(python3 -c 'import time; print(time.time())')
for i in $(seq 1 "$N"); do
  (
    curl -s -o "$OUT/resp-$i.json" -D "$OUT/hdr-$i.txt" -w '%{http_code}\n' \
      -b "$OUT/cookies" -X POST "$APP_URL/api/links/bulk" \
      -H 'Content-Type: application/json' \
      -H "X-CSRF-Token: $CSRF" \
      -H "X-SS-Debug: $SPIN_VARIABLE_LOG_DEBUG_TOKEN" \
      --data-binary "@$OUT/body-$i.json" > "$OUT/code-$i.txt"
  ) &
done
wait
END=$(python3 -c 'import time; print(time.time())')

echo "wall: $(python3 -c "print(f'{$END-$START:.1f}s')")"
for i in $(seq 1 "$N"); do
  printf 'request %s: HTTP %s | ' "$i" "$(tr -d '\n' < "$OUT/code-$i.txt")"
  python3 - "$OUT/resp-$i.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
keys = ("count", "partial", "index_updated", "write_error", "next_step", "error")
print(" ".join(f"{k}={d[k]}" for k in keys if k in d))
if d.get("not_created"):
    print(f"    not_created={len(d['not_created'])} first={d['not_created'][0]}")
PY
  grep -i 'server-timing' "$OUT/hdr-$i.txt" | head -1 | sed 's/^/    /' || true
done
echo
echo "artifacts in $OUT"
echo "now check: GET /api/admin/consistency — and read the log lines back (see header comment)"
