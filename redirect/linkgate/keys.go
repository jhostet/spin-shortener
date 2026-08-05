package linkgate

import "strconv"

// Physical key prefixes for the single "default" KV store. These MUST stay
// byte-identical to api/kvprefix.py's STORE_PREFIXES — a mismatch means the
// API writes links the redirect path cannot find, with no error anywhere.
// api/tests/test_kvprefix.py reads this file and pins that equality.
const (
	LinksPrefix     = "links:"
	AnalyticsPrefix = "analytics:"
)

// LinkKey is the physical key of a link record: links:slug:<slug>.
func LinkKey(slug string) string { return LinksPrefix + "slug:" + slug }

// CountKey is the physical key of a slug's click counter.
func CountKey(slug string) string { return AnalyticsPrefix + "count:" + slug }

// EventKey is the physical key of one recent-events ring-buffer slot.
func EventKey(slug string, slot int) string {
	return AnalyticsPrefix + "events:" + slug + ":" + strconv.Itoa(slot)
}

// There is deliberately no users: prefix constant here: the redirect
// component has no business constructing a users key, and an unused
// constant is an invitation.
