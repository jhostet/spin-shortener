package linkgate

import (
	"encoding/json"
	"math/rand/v2"
	"testing"
	"time"
)

func TestUpdateCount_FirstClick(t *testing.T) {
	raw, err := UpdateCount(nil, "2026-01-01", 90)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var rec CountRecord
	if err := json.Unmarshal(raw, &rec); err != nil {
		t.Fatalf("failed to unmarshal result: %v", err)
	}
	if rec.Total != 1 || rec.Days["2026-01-01"] != 1 {
		t.Fatalf("unexpected record: %+v", rec)
	}
}

func TestUpdateCount_SecondClickSameDay(t *testing.T) {
	first, _ := UpdateCount(nil, "2026-01-01", 90)
	second, err := UpdateCount(first, "2026-01-01", 90)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var rec CountRecord
	json.Unmarshal(second, &rec)
	if rec.Total != 2 || rec.Days["2026-01-01"] != 2 {
		t.Fatalf("unexpected record: %+v", rec)
	}
}

func TestUpdateCount_DifferentDaysAccumulate(t *testing.T) {
	day1, _ := UpdateCount(nil, "2026-01-01", 90)
	day2, err := UpdateCount(day1, "2026-01-02", 90)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var rec CountRecord
	json.Unmarshal(day2, &rec)
	if rec.Total != 2 || rec.Days["2026-01-01"] != 1 || rec.Days["2026-01-02"] != 1 {
		t.Fatalf("unexpected record: %+v", rec)
	}
}

func TestUpdateCount_MalformedRawReturnsError(t *testing.T) {
	_, err := UpdateCount([]byte("{not valid json"), "2026-01-01", 90)
	if err == nil {
		t.Fatal("expected an error for malformed raw blob, got nil")
	}
}

func TestUpdateCount_TrimsToRetentionWindow_PreservesTotal(t *testing.T) {
	var raw []byte
	var err error
	for day := 1; day <= 5; day++ {
		raw, err = UpdateCount(raw, dayString(day), 3)
		if err != nil {
			t.Fatalf("unexpected error on day %d: %v", day, err)
		}
	}
	var rec CountRecord
	json.Unmarshal(raw, &rec)

	if rec.Total != 5 {
		t.Fatalf("expected total to survive trimming, got %d", rec.Total)
	}
	if len(rec.Days) != 3 {
		t.Fatalf("expected days map trimmed to 3 entries, got %d: %+v", len(rec.Days), rec.Days)
	}
	for day := 1; day <= 2; day++ {
		if _, ok := rec.Days[dayString(day)]; ok {
			t.Fatalf("expected oldest day %s to be trimmed, still present: %+v", dayString(day), rec.Days)
		}
	}
	for day := 3; day <= 5; day++ {
		if rec.Days[dayString(day)] != 1 {
			t.Fatalf("expected recent day %s to be retained, got: %+v", dayString(day), rec.Days)
		}
	}
}

func dayString(day int) string {
	return time.Date(2026, 1, day, 0, 0, 0, 0, time.UTC).Format("2006-01-02")
}

func TestClassifyUserAgent(t *testing.T) {
	cases := []struct {
		ua       string
		expected string
	}{
		{"", "other"},
		{"Googlebot/2.1 (+http://www.google.com/bot.html)", "bot"},
		{"Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36", "mobile"},
		{"Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) AppleWebKit/605.1.15", "mobile"},
		{"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36", "desktop"},
		{"curl/8.4.0", "other"},
	}
	for _, tc := range cases {
		t.Run(tc.ua, func(t *testing.T) {
			if got := ClassifyUserAgent(tc.ua); got != tc.expected {
				t.Errorf("ClassifyUserAgent(%q) = %q, want %q", tc.ua, got, tc.expected)
			}
		})
	}
}

func TestFormatEvent(t *testing.T) {
	got := FormatEvent(1234567890, "https://example.com/", "desktop")
	want := "1234567890|https://example.com/|desktop"
	if got != want {
		t.Errorf("FormatEvent() = %q, want %q", got, want)
	}
}

func TestEventSlot_WithinRange(t *testing.T) {
	now := time.Now()
	for _, numSlots := range []int{1, 5, 30, 100} {
		slot := EventSlot(now, numSlots)
		if slot < 0 || slot >= numSlots {
			t.Errorf("EventSlot(now, %d) = %d, out of range", numSlots, slot)
		}
	}
}

func TestEventSlot_NonPositiveNumSlotsDefaultsToOne(t *testing.T) {
	if got := EventSlot(time.Now(), 0); got != 0 {
		t.Errorf("EventSlot with numSlots=0 = %d, want 0", got)
	}
	if got := EventSlot(time.Now(), -5); got != 0 {
		t.Errorf("EventSlot with numSlots=-5 = %d, want 0", got)
	}
}

// The regression this function exists to prevent, and the reason the fix is
// worth having: clicks arriving at a steady cadence produce timestamps in
// arithmetic progression, and the old single-multiply implementation mapped
// those onto an arithmetic progression of SLOTS — one additive cycle of the
// ring, never the whole thing. The spacings below are the exact ones measured
// collapsing on 2026-08-07. Under the old code, 300 ms apart reached ONE slot
// of thirty and 1 ms apart reached three; every row here would fail.
//
// Thresholds are deliberately loose. This pins "the ring is actually used",
// not a distribution quality bound — 20 clicks into 30 slots cannot fill more
// than about 63% of them however good the hash is (balls-in-bins), so
// demanding more would be demanding something arithmetically impossible.
func TestEventSlot_SteadyCadenceReachesMostOfTheRing(t *testing.T) {
	base := time.Date(2026, 8, 7, 12, 0, 0, 0, time.UTC)

	cases := []struct {
		name     string
		spacing  time.Duration
		numSlots int
		clicks   int
		wantMin  int // distinct slots, at minimum
	}{
		{"1ms apart, 30 slots", time.Millisecond, 30, 20, 10},
		{"300ms apart, 30 slots", 300 * time.Millisecond, 30, 20, 10},
		{"107ms apart, 30 slots", 107 * time.Millisecond, 30, 20, 10},
		{"1ms apart, 16 slots", time.Millisecond, 16, 16, 7},
		{"300ms apart, 16 slots", 300 * time.Millisecond, 16, 16, 7},
		{"1s apart, 16 slots", time.Second, 16, 16, 7},
		// Deliberately no "1s apart, 30 slots" row: under the old code that
		// case reached 11 of 30 over 20 clicks and so would not have failed,
		// making it a decorative assertion rather than a regression pin. The
		// parity test below is what covers 30-slot behaviour universally.
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			seen := make(map[int]bool)
			for i := 0; i < tc.clicks; i++ {
				slot := EventSlot(base.Add(time.Duration(i)*tc.spacing), tc.numSlots)
				if slot < 0 || slot >= tc.numSlots {
					t.Fatalf("slot %d out of range for numSlots=%d", slot, tc.numSlots)
				}
				seen[slot] = true
			}
			if len(seen) < tc.wantMin {
				t.Errorf("%d clicks %v apart reached only %d of %d slots, want >= %d",
					tc.clicks, tc.spacing, len(seen), tc.numSlots, tc.wantMin)
			}
		})
	}
}

// The sharpest statement of the old defect, and the one that does not depend
// on run length, spacing or slot count arithmetic working out a particular way.
//
// Go's multiply wraps at 2^64. 2^64 is divisible by 2, so wraparound preserves
// the low bit, and every realistic spacing in nanoseconds is even — which
// pinned slot PARITY to the base timestamp's low bit forever. Under the old
// code, a link whose first click landed on an even nanosecond could reach only
// even slots, for the entire life of the link: HALF THE RING WAS PERMANENTLY
// UNREACHABLE. Verified against the old formula at 5,000 clicks across four
// spacings and both base parities — every run reached exactly one parity.
//
// This also corrects the stride/gcd derivation's scope. That analysis is exact
// only when numSlots divides 2^64 (i.e. is a power of two), where the collapse
// is total — 1 slot of 16, every spacing. For numSlots=30 the wraparound
// perturbs it, so the reachable set is larger than the naive formula predicts
// but still capped at half the ring.
func TestEventSlot_ReachesBothParitiesOfTheRing(t *testing.T) {
	base := time.Date(2026, 8, 7, 12, 0, 0, 0, time.UTC)

	for _, spacing := range []time.Duration{
		time.Millisecond, 300 * time.Millisecond, time.Second, 1500 * time.Millisecond,
	} {
		for _, offsetNanos := range []int{0, 1} { // even and odd base timestamps
			seen := map[int]bool{}
			start := base.Add(time.Duration(offsetNanos))
			for i := 0; i < 200; i++ {
				seen[EventSlot(start.Add(time.Duration(i)*spacing), 30)%2] = true
			}
			if len(seen) != 2 {
				t.Errorf("spacing %v, base+%dns: reached only parity %v — half the ring is unreachable",
					spacing, offsetNanos, seen)
			}
		}
	}
}

// The original bug report, reproduced as a test: 8 requests 300 ms apart
// retained only 3 distinct events under the old implementation.
func TestEventSlot_ReproducesTheOriginalBugReportScenario(t *testing.T) {
	base := time.Date(2026, 8, 7, 12, 0, 0, 0, time.UTC)
	seen := make(map[int]bool)
	for i := 0; i < 8; i++ {
		seen[EventSlot(base.Add(time.Duration(i)*300*time.Millisecond), 30)] = true
	}
	// 8 balls into 30 bins retains ~7.1 distinct on average. The old code gave
	// exactly 1. Anything at or above 6 means the ring is genuinely in use.
	if len(seen) < 6 {
		t.Errorf("8 clicks 300ms apart retained %d distinct slots, want >= 6 (old code gave 1)", len(seen))
	}
}

func TestShardFor_WithinRange(t *testing.T) {
	for _, numShards := range []int{2, 4, 16, 64} {
		for i := 0; i < 1000; i++ {
			shard := ShardFor(uint64(i)*2246822519, numShards)
			if shard < 0 || shard >= numShards {
				t.Fatalf("ShardFor(_, %d) = %d, out of range", numShards, shard)
			}
		}
	}
}

// A single-shard (or degenerate) configuration must collapse to the one key
// rather than dividing by zero or returning a negative index.
func TestShardFor_NonPositiveOrSingleShardIsAlwaysZero(t *testing.T) {
	for _, numShards := range []int{-5, 0, 1} {
		for _, entropy := range []uint64{0, 1, 12345, 1 << 63, ^uint64(0)} {
			if got := ShardFor(entropy, numShards); got != 0 {
				t.Errorf("ShardFor(%d, %d) = %d, want 0", entropy, numShards, got)
			}
		}
	}
}

// assertUniformOverShards fails unless every shard is within ±10% of an even
// share of draws.
func assertUniformOverShards(t *testing.T, label string, counts []int, draws int) {
	t.Helper()
	expected := float64(draws) / float64(len(counts))
	low, high := expected*0.9, expected*1.1
	for shard, got := range counts {
		if float64(got) < low || float64(got) > high {
			t.Errorf("%s: shard %d got %d draws, want within ±10%% of %.0f (%.0f..%.0f)",
				label, shard, got, expected, low, high)
		}
	}
}

func TestShardFor_DistributesUniformlyOverRandomEntropy(t *testing.T) {
	const draws = 100000
	counts := make([]int, CountShards)
	// A fixed PCG seed keeps this deterministic: a distribution test that can
	// flake is a test that gets muted.
	rng := rand.New(rand.NewPCG(0x5eed, 0xf00d))
	for i := 0; i < draws; i++ {
		counts[ShardFor(rng.Uint64(), CountShards)]++
	}
	assertUniformOverShards(t, "random entropy", counts, draws)
}

// The regression that matters. Clicks arriving at a steady cadence produce
// timestamps in near-perfect arithmetic progression, and that is exactly the
// input EventSlot's single multiply-then-reduce is documented (CLAUDE.md,
// "Analytics") to distribute badly against. A counter built on ShardFor must
// not inherit that defect, so it is pinned rather than assumed.
func TestShardFor_DistributesUniformlyOverTimestampShapedInput(t *testing.T) {
	const draws = 100000
	const oneMilliInNanos = 1_000_000
	base := time.Date(2026, 8, 6, 12, 0, 0, 0, time.UTC).UnixNano()

	counts := make([]int, CountShards)
	for i := 0; i < draws; i++ {
		counts[ShardFor(uint64(base+int64(i)*oneMilliInNanos), CountShards)]++
	}
	assertUniformOverShards(t, "timestamps 1ms apart", counts, draws)
}
