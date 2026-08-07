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

// CountShards is how many shards a slug's click counter is spread across.
// It MUST stay equal to api/analytics.py's COUNT_SHARDS: the writer picks a
// shard in [0, CountShards) and the reader sums shards [0, COUNT_SHARDS).
// If the reader's value is LOWER than the writer's, every click recorded in a
// higher shard silently disappears from the total, with no error anywhere —
// the same failure shape the prefixes above have, so it is pinned the same
// way, by api/tests/test_kvprefix.py reading this file.
//
// 16 is chosen so that even if every click the app can serve (~25/s, set by
// Akamai's 50 write RPS cap at two writes per click) landed on one slug, each
// shard would still see only ~1.6/s — inside the band measured lossless on
// the live app (0% at 1.2/s per key). See docs/plans/click-count-accuracy.md.
//
// RAISE ONLY, NEVER LOWER, and change both languages in the same commit.
const CountShards = 16

// CountShardKey is the physical key of one shard of a slug's click counter:
// analytics:count:<slug>:<shard>. The pre-sharding key was
// analytics:count:<slug> with no shard suffix; nothing writes that key any
// more and api/analytics.py still reads it, so no history is lost and there
// is no migration to run. A slug can never contain a colon (api/links.py's
// CUSTOM_SLUG_PATTERN is ^[A-Za-z0-9_-]{3,32}$ and generated slugs are drawn
// from ascii_letters+digits), so the two key shapes can never collide.
func CountShardKey(slug string, shard int) string {
	return AnalyticsPrefix + "count:" + slug + ":" + strconv.Itoa(shard)
}

// EventKey is the physical key of one recent-events ring-buffer slot.
func EventKey(slug string, slot int) string {
	return AnalyticsPrefix + "events:" + slug + ":" + strconv.Itoa(slot)
}

// There is deliberately no users: prefix constant here: the redirect
// component has no business constructing a users key, and an unused
// constant is an invitation.
