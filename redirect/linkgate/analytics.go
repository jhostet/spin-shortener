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
// The raw nanosecond timestamp is multiplied by a large odd constant before
// reducing mod numSlots, spreading entropy from all its bits into the
// result. A plain `UnixNano() % numSlots` collided far more than a uniform
// distribution would predict under realistic request timing (e.g. periodic
// clicks a fixed interval apart), because clock resolution and/or timing
// regularity concentrates entropy in a way that aliases against a small
// modulus — confirmed empirically against the actual componentize-go/wasip1
// clock. This is a de-correlation trick (Knuth's multiplicative hash
// constant), not a cryptographic hash.
func EventSlot(now time.Time, numSlots int) int {
	if numSlots <= 0 {
		numSlots = 1
	}
	mixed := uint64(now.UnixNano()) * 2654435761
	return int(mixed % uint64(numSlots))
}
