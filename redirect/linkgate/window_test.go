package linkgate

import (
	"testing"
	"time"
)

func TestIsWithinWindow(t *testing.T) {
	start := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	end := time.Date(2026, 2, 1, 0, 0, 0, 0, time.UTC)
	startStr := start.Format(time.RFC3339)
	endStr := end.Format(time.RFC3339)

	cases := []struct {
		name     string
		startAt  string
		endAt    string
		now      time.Time
		expected bool
	}{
		{"no window set", "", "", start.Add(-time.Hour), true},
		{"before start", startStr, endStr, start.Add(-time.Second), false},
		{"exactly at start (inclusive)", startStr, endStr, start, true},
		{"mid window", startStr, endStr, start.Add(time.Hour), true},
		{"exactly at end (exclusive)", startStr, endStr, end, false},
		{"after end", startStr, endStr, end.Add(time.Second), false},
		{"start-only, before start", startStr, "", start.Add(-time.Second), false},
		{"start-only, active forever after start", startStr, "", end.Add(24 * time.Hour), true},
		{"end-only, active until end", "", endStr, start, true},
		{"end-only, expired", "", endStr, end.Add(time.Second), false},
		{"malformed start_at fails closed", "not-a-time", endStr, start, false},
		{"malformed end_at fails closed", startStr, "not-a-time", start, false},
		{"degenerate start == end always inactive", startStr, startStr, start, false},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := IsWithinWindow(tc.startAt, tc.endAt, tc.now)
			if got != tc.expected {
				t.Errorf("IsWithinWindow(%q, %q, %v) = %v, want %v", tc.startAt, tc.endAt, tc.now, got, tc.expected)
			}
		})
	}
}
