package linkgate

import "time"

// IsWithinWindow reports whether now falls in [startAt, endAt) — inclusive
// start, exclusive end. An empty startAt means "no lower bound"; an empty
// endAt means "no upper bound". A non-empty but unparsable timestamp fails
// closed (returns false) rather than being silently ignored — the
// out-of-window case this produces still answers a plain 404, unchanged.
// (A malformed KV *record* is a different fail-closed path: since
// docs/plans/redirect-read-failure-not-404.md, a ParseLink error is
// DispositionUnreadable -> 500, not 404 -> this file's fail-closed behaviour
// is about a well-formed record with a bad timestamp string, not about a
// record that won't parse at all.)
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
