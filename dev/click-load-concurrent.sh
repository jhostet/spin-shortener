#!/usr/bin/env bash
# Concurrent click-load driver. Use this, not dev/click-load.sh, for any
# measurement against a REMOTE host.
#
# dev/click-load.sh is sequential by design (one request in flight), so its
# achieved rate is capped at 1/latency — measured at 4.0 req/s against Akamai
# at ~250ms latency, when the target was 9.4. It cannot reach the rates the
# click-accuracy measurements are specified at, and it under-reports the rate
# silently enough that a run looks fine. This fires each request in the
# background against an absolute schedule, so the achieved rate is set by the
# schedule rather than by latency, and it prints both.
#
# Keep dev/click-load.sh for local runs, where latency is microseconds and
# sequential pacing is accurate: it is the reviewed tool with the full
# write-cap guard. Both warn at Akamai's 50 write RPS cap.
#
#   conc-load.sh <base-url> <rate-per-second> <count> <slug>
set -eo pipefail
export LC_ALL=C

base="$1"; rate="$2"; count="$3"; slug="$4"

interval=$(awk -v r="$rate" 'BEGIN { printf "%.6f", 1 / r }')
writes=$(awk -v r="$rate" 'BEGIN { printf "%.1f", r * 2 }')
echo "target rate: $rate req/s  => $writes KV writes/s"
over=$(awk -v w="$writes" 'BEGIN { print (w >= 50) ? 1 : 0 }')
[ "$over" -eq 1 ] && echo "!! WARNING: at/over Akamai's 50 write RPS cap — measures throttling, not contention" >&2

tmp=$(mktemp -d)
start=$(perl -MTime::HiRes -e 'printf "%.6f", Time::HiRes::time()')

n=0
while [ "$n" -lt "$count" ]; do
  curl -s -o /dev/null -w '%{http_code}\n' "$base/r/$slug" >> "$tmp/codes" 2>/dev/null &
  n=$((n + 1))
  if [ "$n" -lt "$count" ]; then
    perl -MTime::HiRes -e '
      my ($s, $iv, $i) = @ARGV;
      my $d = $s + $iv * $i - Time::HiRes::time();
      Time::HiRes::sleep($d) if $d > 0;
    ' "$start" "$interval" "$n"
  fi
done

issued_end=$(perl -MTime::HiRes -e 'printf "%.6f", Time::HiRes::time()')
wait
end=$(perl -MTime::HiRes -e 'printf "%.6f", Time::HiRes::time()')

issue_span=$(awk -v s="$start" -v e="$issued_end" 'BEGIN { printf "%.3f", e - s }')
achieved=$(awk -v c="$count" -v t="$issue_span" 'BEGIN { printf "%.1f", (t > 0) ? c / t : 0 }')
ach_writes=$(awk -v a="$achieved" 'BEGIN { printf "%.1f", a * 2 }')
total=$(wc -l < "$tmp/codes" | tr -d ' ')
ok=$(grep -c '^302$' "$tmp/codes" || true)

echo "issued $count over ${issue_span}s => achieved $achieved req/s ($ach_writes writes/s)"
echo "responses: $total   302s: $ok   non-302: $((total - ok))"
awk '{print}' "$tmp/codes" | sort | uniq -c | awk '{printf "  code %s: %s\n", $2, $1}'
rm -rf "$tmp"
