#!/usr/bin/env bash
# Paced click load against /r/<slug>, for measuring analytics click loss.
# See docs/plans/click-count-accuracy.md.
#
#   ./dev/click-load.sh <base-url> <rate-per-second> <count> <slug> [slug...]
#
# Issues <count> GETs to <base-url>/r/<slug>, round-robining the slugs, paced to
# approximately <rate-per-second>. Prints the per-slug request count and the
# number of non-302 responses. It deliberately does NOT read the click totals
# back — that needs an authenticated session, and the operator is already
# looking at the link detail page.
#
# THE WRITE-RATE WARNING BELOW IS THE POINT OF THIS SCRIPT, not a nicety.
# Every recorded click performs one KV write (analytics:count:<slug>:<n> —
# the recent-events ring buffer's second write, analytics:events:<slug>:<slot>,
# was retired 2026-08-18, see docs/plans/drop-events-write.md), so the app-wide
# write rate is rate x 1 against Akamai's 50 write RPS cap. A run above ~50
# requests/second measures that cap, not counter contention. This is not
# hypothetical: the click-count gating probe was first run at 38.5 req/s (then
# 77 writes/s, when the ceiling was still two writes per click), showed 32.5%
# loss, and was read as a clean failure of the sharding design. Re-run under
# the cap it passed unambiguously. One line of warning would have saved that
# round trip.
#
# `set -u` is deliberately omitted, matching dev/kv-explorer-up.sh: macOS's
# system bash 3.2 treats "$@" with no positional parameters as an unbound
# variable. The argument checks below cover what actually matters.
set -eo pipefail

# curl's -w formats floats under the current locale; force a '.' decimal point
# so the awk arithmetic below can parse them.
export LC_ALL=C

readonly AKAMAI_WRITE_RPS_CAP=50
readonly KV_WRITES_PER_CLICK=1

usage() {
  cat >&2 <<'EOF'
usage: dev/click-load.sh <base-url> <rate-per-second> <count> <slug> [slug...]

  base-url         e.g. https://<app-id>.fwf.app  or  http://localhost:3000
  rate-per-second  requests/second, may be fractional (e.g. 9.4)
  count            total number of requests to issue
  slug ...         one or more slugs, round-robined

example:
  ./dev/click-load.sh https://example.fwf.app 9.4 100 abc123
EOF
  exit 2
}

[ $# -ge 4 ] || usage

base_url="$1"
rate="$2"
count="$3"
shift 3
slugs=("$@")
num_slugs=${#slugs[@]}

# Strip any trailing slashes so "<base>/r/<slug>" never doubles up.
while [ "${base_url%/}" != "$base_url" ]; do base_url="${base_url%/}"; done

case "$base_url" in
  http://*|https://*) ;;
  *) echo "error: base-url must start with http:// or https:// (got '$base_url')" >&2; exit 2 ;;
esac

# awk, not bash arithmetic: the rate is allowed to be fractional.
if ! awk -v r="$rate" 'BEGIN { exit !(r + 0 > 0) }' 2>/dev/null; then
  echo "error: rate-per-second must be a positive number (got '$rate')" >&2
  exit 2
fi
case "$count" in
  ''|*[!0-9]*) echo "error: count must be a positive integer (got '$count')" >&2; exit 2 ;;
esac
[ "$count" -gt 0 ] || { echo "error: count must be a positive integer (got '$count')" >&2; exit 2; }

interval=$(awk -v r="$rate" 'BEGIN { printf "%.6f", 1 / r }')
write_rate=$(awk -v r="$rate" -v w="$KV_WRITES_PER_CLICK" 'BEGIN { printf "%.1f", r * w }')

echo "base-url:      $base_url"
echo "rate:          $rate req/s (one request every ${interval}s)"
echo "count:         $count"
echo "slugs:         $num_slugs ($(IFS=,; echo "${slugs[*]}"))"
echo "implied write rate: $write_rate KV writes/s  (${rate} req/s x $KV_WRITES_PER_CLICK writes per recorded click)"

over_cap=$(awk -v w="$write_rate" -v c="$AKAMAI_WRITE_RPS_CAP" 'BEGIN { print (w >= c) ? 1 : 0 }')
if [ "$over_cap" -eq 1 ]; then
  cat >&2 <<EOF

!!  WARNING: $write_rate writes/s is AT OR ABOVE Akamai's $AKAMAI_WRITE_RPS_CAP KV write RPS cap.
!!  Click loss measured at this rate is WRITE THROTTLING, not counter
!!  contention. It says nothing about whether sharded counters work.
!!  To measure counter accuracy, stay under $(awk -v c="$AKAMAI_WRITE_RPS_CAP" -v w="$KV_WRITES_PER_CLICK" 'BEGIN { printf "%.1f", c / w }') requests/second.

EOF
fi
echo

# Parallel indexed arrays: macOS bash 3.2 has no associative arrays.
i=0
while [ "$i" -lt "$num_slugs" ]; do
  slug_counts[$i]=0
  i=$((i + 1))
done

non_302=0
codes_seen=""
connect_failures=0

start_ns=$(perl -MTime::HiRes -e 'printf "%.6f", Time::HiRes::time()')

n=0
while [ "$n" -lt "$count" ]; do
  idx=$((n % num_slugs))
  slug="${slugs[$idx]}"

  # -o /dev/null discards the body; no -L, since following the 302 would hit the
  # destination host and is not part of what we are measuring.
  if code=$(curl -s -o /dev/null -w '%{http_code}' "$base_url/r/$slug" 2>/dev/null); then
    slug_counts[$idx]=$(( ${slug_counts[$idx]} + 1 ))
    if [ "$code" != "302" ]; then
      non_302=$((non_302 + 1))
      case " $codes_seen " in *" $code "*) ;; *) codes_seen="$codes_seen $code" ;; esac
    fi
  else
    connect_failures=$((connect_failures + 1))
  fi

  n=$((n + 1))

  # Sleep until an ABSOLUTE target time (start + n x interval), not for a fixed
  # interval: request latency, curl's startup and this loop's own subprocesses
  # all fall inside the interval and would otherwise accumulate as drift, making
  # the achieved rate land well under the requested one. Perl supplies both the
  # sub-second clock and the sub-second sleep, neither of which macOS's date(1)
  # or sleep(1) can be relied on for together.
  if [ "$n" -lt "$count" ]; then
    perl -MTime::HiRes -e '
      my ($start, $interval, $n) = @ARGV;
      my $delay = $start + $interval * $n - Time::HiRes::time();
      Time::HiRes::sleep($delay) if $delay > 0;
    ' "$start_ns" "$interval" "$n"
  fi
done

end_ns=$(perl -MTime::HiRes -e 'printf "%.6f", Time::HiRes::time()')
elapsed_total=$(awk -v s="$start_ns" -v e="$end_ns" 'BEGIN { printf "%.3f", e - s }')
achieved=$(awk -v c="$count" -v t="$elapsed_total" 'BEGIN { printf "%.1f", (t > 0) ? c / t : 0 }')
achieved_writes=$(awk -v a="$achieved" -v w="$KV_WRITES_PER_CLICK" 'BEGIN { printf "%.1f", a * w }')

echo "requests sent: $count in ${elapsed_total}s"
echo "achieved rate: $achieved req/s  (=> $achieved_writes KV writes/s)"
echo "per-slug:"
i=0
while [ "$i" -lt "$num_slugs" ]; do
  echo "  ${slugs[$i]}  ${slug_counts[$i]}"
  i=$((i + 1))
done
echo "non-302 responses: $non_302${codes_seen:+ (codes:$codes_seen)}"
echo "connect failures:  $connect_failures"

# The requested rate warned before the run; the achieved rate is what the run
# actually did, and it is what any conclusion has to be justified against.
achieved_over_cap=$(awk -v w="$achieved_writes" -v c="$AKAMAI_WRITE_RPS_CAP" 'BEGIN { print (w >= c) ? 1 : 0 }')
if [ "$achieved_over_cap" -eq 1 ]; then
  cat >&2 <<EOF

!!  WARNING: this run averaged $achieved_writes KV writes/s, at or above Akamai's
!!  $AKAMAI_WRITE_RPS_CAP write RPS cap. Any click loss it shows is throttling, not counter
!!  contention. Do not draw conclusions about counter accuracy from it.
EOF
fi

[ "$connect_failures" -eq 0 ] || exit 1
