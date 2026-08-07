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
// 64, raised from 16 on 2026-08-07 after measuring the deployed 16-shard
// build. The original 16 was justified by per-shard RATE (~1.6 clicks/s per
// shard at the app-wide ceiling, inside a band measured lossless) and that
// reasoning was WRONG: a rate does not lose an increment, two clicks sharing
// one shard's read-modify-write window does. In-flight requests are rate x KV
// latency, and at 250-400ms latency ~20 clicks/s puts 5-8 requests in flight,
// so collisions over S shards grow as roughly k^2/2S. 16 shards measured 92.5
// of 100 recorded at 19.9 clicks/s; 4x the shards should cut that loss ~4x.
//
// The read cost this imposes — the analytics page sums every shard — is why
// this was affordable: api/analytics.py issues those reads concurrently, and
// the host was measured genuinely overlapping them (~16.6-way), so shard count
// is no longer a linear latency knob on that page. Do not make those reads
// sequential again without revisiting this number.
//
// RAISE ONLY, NEVER LOWER, and change both languages in the same commit.
// Lowering silently discards every click already recorded in a higher shard.
const CountShards = 64

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
