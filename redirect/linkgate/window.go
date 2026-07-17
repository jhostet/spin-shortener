package linkgate

import "time"

// IsWithinWindow reports whether now falls in [startAt, endAt) — inclusive
// start, exclusive end. An empty startAt means "no lower bound"; an empty
// endAt means "no upper bound". A non-empty but unparsable timestamp fails
// closed (returns false) rather than being silently ignored — consistent
// with how a malformed KV record already fails closed elsewhere (ParseLink
// error -> lookupLink returns ok=false -> 404).
func IsWithinWindow(startAt, endAt string, now time.Time) bool {
	if startAt != "" {
		start, err := time.Parse(time.RFC3339, startAt)
		if err != nil || now.Before(start) {
			return false
		}
	}
	if endAt != "" {
		end, err := time.Parse(time.RFC3339, endAt)
		if err != nil || !now.Before(end) {
			return false
		}
	}
	return true
}
