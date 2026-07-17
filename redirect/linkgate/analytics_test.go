package linkgate

import (
	"encoding/json"
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
