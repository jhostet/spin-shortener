#!/usr/bin/env bash
# Wraps `hey` for load-testing /r/{slug}, institutionalising two traps that
# have already produced wrong conclusions in this repo (TASKS.md, "Measurement
# traps learned the hard way this session"):
#
#   1. `hey` follows redirects by default. Without -disable-redirects, a load
#      test against a working /r/{slug} actually measures the DESTINATION
#      host's behaviour (e.g. example.com refusing a TLS handshake), which
#      shows up as NaN latency and tells you nothing about `redirect` itself.
#      This script always passes -disable-redirects.
#
#   2. `hey -n <n> -c <c>` divides n by c with INTEGER division, so
#      `-n 300 -c 80` silently issues 240 requests, not 300. This script
#      takes a concurrency (-c) and a per-worker count (-k) and computes
#      n = c * k itself, so truncation is impossible by construction.
#
# See docs/plans/redirect-read-failure-not-404.md, "Tooling: dev/redirect-load.sh".
#
# Usage:
#   ./dev/redirect-load.sh -u <url> -c <concurrency> -k <per-worker-count>
#
# Exits non-zero if ANY 404 appears in hey's status distribution — that is
# exactly the regression this tool exists to catch (a KV read failure being
# reported as "this link does not exist" rather than 503). A 503 is printed
# as informational, never a failure: it is the correct, honest answer to
# read-cap saturation.
set -eo pipefail
export LC_ALL=C

# Akamai's documented KV read cap, app-wide. A successful redirect performs 2
# gets (the link lookup + the analytics count read-modify-write's own read),
# so the implied read rate is 2x the achieved request rate — the read-side
# analogue of dev/click-load.sh's write-rate warning.
readonly AKAMAI_READ_RPS_CAP=1000
readonly KV_READS_PER_REDIRECT=2

usage() {
  cat >&2 <<'EOF'
usage: dev/redirect-load.sh -u <url> -c <concurrency> -k <per-worker-count>

  -u  target URL, e.g. http://localhost:3000/r/abc123 or https://<app>.fwf.app/r/abc123
  -c  number of concurrent workers (hey's -c)
  -k  requests per worker; n = c * k is computed here so hey's integer
      division of -n by -c can never silently truncate the request count

example:
  ./dev/redirect-load.sh -u http://localhost:3000/r/abc123 -c 60 -k 10   # n = 600
EOF
  exit 2
}

if ! command -v hey >/dev/null 2>&1; then
  cat >&2 <<'EOF'
error: `hey` is not on PATH.

Install it, e.g.:
  brew install hey                       # macOS
  go install github.com/rakyll/hey@latest   # anywhere with Go

See https://github.com/rakyll/hey.
EOF
  exit 2
fi

url=""
concurrency=""
per_worker=""

while getopts "u:c:k:h" opt; do
  case "$opt" in
    u) url="$OPTARG" ;;
    c) concurrency="$OPTARG" ;;
    k) per_worker="$OPTARG" ;;
    h) usage ;;
    *) usage ;;
  esac
done

[ -n "$url" ] || usage
case "$url" in
  http://*|https://*) ;;
  *) echo "error: -u must start with http:// or https:// (got '$url')" >&2; exit 2 ;;
esac

case "$concurrency" in
  ''|*[!0-9]*) echo "error: -c must be a positive integer (got '$concurrency')" >&2; exit 2 ;;
esac
[ "$concurrency" -gt 0 ] || { echo "error: -c must be a positive integer (got '$concurrency')" >&2; exit 2; }

case "$per_worker" in
  ''|*[!0-9]*) echo "error: -k must be a positive integer (got '$per_worker')" >&2; exit 2 ;;
esac
[ "$per_worker" -gt 0 ] || { echo "error: -k must be a positive integer (got '$per_worker')" >&2; exit 2; }

n=$((concurrency * per_worker))

echo "url:            $url"
echo "concurrency:    $concurrency"
echo "per-worker:     $per_worker"
echo "n (= c * k):    $n"
echo

hey_out=$(hey -disable-redirects -n "$n" -c "$concurrency" "$url" 2>&1)
echo "$hey_out"
echo

# hey's own "Requests/sec:" summary line, e.g. "  Requests/sec:\t812.3456".
achieved_rps=$(printf '%s\n' "$hey_out" | awk -F'[ \t]+' '/Requests\/sec:/ { print $NF }')
if [ -n "$achieved_rps" ]; then
  implied_reads=$(awk -v r="$achieved_rps" -v k="$KV_READS_PER_REDIRECT" 'BEGIN { printf "%.1f", r * k }')
  echo "achieved rate:       $achieved_rps req/s"
  echo "implied read rate:   $implied_reads KV reads/s  (${achieved_rps} req/s x $KV_READS_PER_REDIRECT reads per redirect)"

  over_cap=$(awk -v rd="$implied_reads" -v c="$AKAMAI_READ_RPS_CAP" 'BEGIN { print (rd >= c) ? 1 : 0 }')
  if [ "$over_cap" -eq 1 ]; then
    cat >&2 <<EOF

!!  WARNING: $implied_reads reads/s is AT OR ABOVE Akamai's $AKAMAI_READ_RPS_CAP KV read RPS cap.
!!  This is exactly the regime docs/plans/redirect-read-failure-not-404.md fixes:
!!  read-path saturation should now surface as 503s, not wrong 404s.
EOF
  fi
else
  echo "warning: could not parse 'Requests/sec:' from hey's output; skipping read-rate check" >&2
fi
echo

# Informational count of 503s, printed but never a failure — it is the
# correct, honest answer to saturation.
count_503=$(printf '%s\n' "$hey_out" | awk '/\[503\]/ { for (i = 1; i <= NF; i++) if ($i ~ /^[0-9]+$/) { print $i; exit } }')
if [ -n "$count_503" ]; then
  echo "503 responses (informational, read-cap saturation): $count_503"
fi

# The regression this tool exists to catch: read-path saturation reported as
# "this link does not exist" instead of "temporarily unavailable".
if printf '%s\n' "$hey_out" | grep -q '\[404\]'; then
  echo
  echo "FAIL: 404 appeared in the status distribution. A read failure must" >&2
  echo "answer 503, never 404 — see docs/plans/redirect-read-failure-not-404.md." >&2
  exit 1
fi

echo "PASS: no 404s in the status distribution."
