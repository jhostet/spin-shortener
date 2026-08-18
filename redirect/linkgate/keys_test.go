package linkgate

import "testing"

func TestLinkKey(t *testing.T) {
	if got := LinkKey("abc"); got != "links:slug:abc" {
		t.Errorf("LinkKey(%q) = %q, want %q", "abc", got, "links:slug:abc")
	}
}

func TestCountShardKey(t *testing.T) {
	cases := []struct {
		slug  string
		shard int
		want  string
	}{
		{"abc", 3, "analytics:count:abc:3"},
		{"abc", 0, "analytics:count:abc:0"},
		{"abc", CountShards - 1, "analytics:count:abc:63"},
	}
	for _, c := range cases {
		if got := CountShardKey(c.slug, c.shard); got != c.want {
			t.Errorf("CountShardKey(%q, %d) = %q, want %q", c.slug, c.shard, got, c.want)
		}
	}
}

// The pre-sharding key was analytics:count:<slug> with no suffix. api/analytics.py
// still reads it so no click history is lost, which only holds while a shard key
// can never be mistaken for it — a slug cannot contain a colon, so it cannot.
func TestCountShardKeyNeverCollidesWithTheLegacyKey(t *testing.T) {
	legacy := AnalyticsPrefix + "count:" + "abc"
	for shard := 0; shard < CountShards; shard++ {
		if got := CountShardKey("abc", shard); got == legacy {
			t.Errorf("CountShardKey(%q, %d) = %q, which is the legacy unsharded key", "abc", shard, got)
		}
	}
}
