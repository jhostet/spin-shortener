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
// input a single multiply-then-reduce is documented (see ShardFor's own doc
// comment) to distribute badly against — the flaw that caused the
// recent-events ring buffer's collision defect (found 2026-08-07) before that
// feature (and its EventSlot caller) was retired 2026-08-18. This is now the
// SOLE surviving pin on that anti-aliasing property, since the caller that
// originally suffered the defect no longer exists. A counter built on
// ShardFor must not inherit that defect, so it is pinned rather than assumed.
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
