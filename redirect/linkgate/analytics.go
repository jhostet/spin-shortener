package linkgate

import (
	"encoding/json"
	"sort"
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

// ShardFor maps a 64-bit entropy value onto [0, numShards).
//
// The value is run through a splitmix64 finalizer before the modulo so the
// result depends on all 64 input bits, and — the property that actually
// matters — so that inputs in arithmetic progression do not map to shards in
// arithmetic progression. A single multiply-then-reduce has that flaw:
//
//	A single multiply is LINEAR OVER THE MODULUS: multiplication distributes over
//	the modulo, so for two inputs Δ apart the result advances by a constant
//	stride, and the reachable outputs are one additive cycle rather than the whole
//	range. Go's multiply wraps at 2^64, which is divisible by 2, so wraparound
//	preserves the low bit — pinning output parity to the input's low bit forever.
//	Clicks arriving at a steady cadence are exactly that shape. This is not
//	hypothetical: it was the cause of the recent-events collision defect found
//	2026-08-07 (8 clicks 300 ms apart reached 1 slot of 30). Do not "simplify"
//	this back into a multiply.
//
// The caller that originally suffered that defect, EventSlot (the
// recent-events ring buffer's slot picker), was retired 2026-08-18 along with
// the whole recent-events feature — see docs/plans/drop-events-write.md. This
// derivation moved here, onto ShardFor, because ShardFor is what the click
// counter (recordClickCount, via CountShards) still calls, and it is the only
// written record of why the splitmix64 finalizer is load-bearing. The
// surviving pin on this property is
// TestShardFor_DistributesUniformlyOverTimestampShapedInput.
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
