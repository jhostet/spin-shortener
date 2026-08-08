package linkgate

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"
)

// CountRecord is the count:<slug> KV blob: a running total plus a
// day-bucketed count, bundled into one key so a click only costs one KV
// round trip (get + set) instead of separate total/per-day keys.
type CountRecord struct {
	Total int            `json:"total"`
	Days  map[string]int `json:"days"`
}

// UpdateCount parses the existing count blob (raw may be nil/empty on a
// slug's first click), increments the total and today's per-day count, trims
// Days down to the most recent retentionDays entries, and returns the
// re-marshaled blob ready to write back. day must be "YYYY-MM-DD" so string
// sorting is also chronological sorting.
func UpdateCount(raw []byte, day string, retentionDays int) ([]byte, error) {
	var rec CountRecord
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &rec); err != nil {
			return nil, err
		}
	}
	if rec.Days == nil {
		rec.Days = map[string]int{}
	}

	rec.Total++
	rec.Days[day]++
	trimDays(rec.Days, retentionDays)

	return json.Marshal(rec)
}

func trimDays(days map[string]int, retentionDays int) {
	if retentionDays <= 0 || len(days) <= retentionDays {
		return
	}
	keys := make([]string, 0, len(days))
	for k := range days {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	excess := len(keys) - retentionDays
	for _, k := range keys[:excess] {
		delete(days, k)
	}
}

// ClassifyUserAgent buckets a User-Agent header into a coarse device class
// using a small static ruleset -- deliberately not a full UA-parsing
// pipeline, per the "cheap, simple" hot-path analytics design.
func ClassifyUserAgent(ua string) string {
	if ua == "" {
		return "other"
	}
	lower := strings.ToLower(ua)
	switch {
	case containsAny(lower, "bot", "crawler", "spider"):
		return "bot"
	case containsAny(lower, "mobile", "android", "iphone"):
		return "mobile"
	case containsAny(lower, "mozilla", "chrome", "safari", "firefox", "edg/"):
		return "desktop"
	default:
		return "other"
	}
}

func containsAny(s string, substrs ...string) bool {
	for _, sub := range substrs {
		if strings.Contains(s, sub) {
			return true
		}
	}
	return false
}

// FormatEvent renders one events:<slug>:<slot> value: a fixed-shape,
// pipe-delimited string that's a blind overwrite on write (no read needed)
// and trivially parsed on read.
func FormatEvent(unixMs int64, referrer, uaClass string) string {
	return fmt.Sprintf("%d|%s|%s", unixMs, referrer, uaClass)
}

// EventSlot picks a deterministic ring-buffer slot for "now" out of
// numSlots, so recent-events reads are numSlots direct KV gets, never a scan.
//
// It delegates to ShardFor, whose splitmix64 finalizer is what makes the
// distribution hold. This used to be `(UnixNano() * 2654435761) mod numSlots`
// — a single multiply, intended as a de-correlation trick — and that was the
// cause of the long-standing recent-events collision defect, found 2026-08-07:
//
//	A single multiply is LINEAR OVER THE MODULUS. Multiplication distributes
//	over the modulo, so for two clicks Δ nanoseconds apart the slot advances by
//	a constant stride of (Δ * 2654435761) mod numSlots. The reachable slots are
//	therefore one additive cycle of length numSlots/gcd(stride, numSlots) —
//	never the full ring, no matter how distinct the timestamps are. The
//	multiplier is ≡ 1 (mod 30), so at the default numSlots=30 the stride
//	collapses to Δ mod 30, and millisecond-scale Δ values are divisible by high
//	powers of 2 and 5 while 30 = 2·3·5 shares them. Clicks 300 ms apart reached
//	exactly ONE slot; 1 ms apart reached three of thirty.
//
// That reproduced the documented observation (8 requests 300 ms apart retaining
// 3 distinct events) exactly. The timestamps were never the problem — CLAUDE.md
// previously blamed WASI clock resolution and that explanation is retracted;
// the timestamps are perfectly distinct and simply aliased onto the same slots.
//
// splitmix64's xorshifts are not linear over the modulus, so there is no
// constant stride to collapse. Do not "simplify" this back into a multiply.
func EventSlot(now time.Time, numSlots int) int {
	return ShardFor(uint64(now.UnixNano()), numSlots)
}

// ShardFor maps a 64-bit entropy value onto [0, numShards).
//
// The value is run through a splitmix64 finalizer before the modulo so the
// result depends on all 64 input bits, and — the property that actually
// matters — so that inputs in arithmetic progression do not map to slots in
// arithmetic progression. A single multiply-then-reduce has that flaw and it
// is what broke EventSlot for as long as this feature has existed; see the
// derivation above EventSlot, which now delegates here.
func ShardFor(entropy uint64, numShards int) int {
	if numShards <= 1 {
		return 0
	}
	return int(mix64(entropy) % uint64(numShards))
}

// mix64 is the splitmix64 finalizer: three xorshift/multiply rounds that
// avalanche every input bit across the whole output word.
func mix64(x uint64) uint64 {
	x ^= x >> 30
	x *= 0xbf58476d1ce4e5b9
	x ^= x >> 27
	x *= 0x94d049bb133111eb
	x ^= x >> 31
	return x
}
