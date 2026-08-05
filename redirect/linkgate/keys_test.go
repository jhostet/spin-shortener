package linkgate

import "testing"

func TestLinkKey(t *testing.T) {
	if got := LinkKey("abc"); got != "links:slug:abc" {
		t.Errorf("LinkKey(%q) = %q, want %q", "abc", got, "links:slug:abc")
	}
}

func TestCountKey(t *testing.T) {
	if got := CountKey("abc"); got != "analytics:count:abc" {
		t.Errorf("CountKey(%q) = %q, want %q", "abc", got, "analytics:count:abc")
	}
}

func TestEventKey(t *testing.T) {
	if got := EventKey("abc", 7); got != "analytics:events:abc:7" {
		t.Errorf("EventKey(%q, %d) = %q, want %q", "abc", 7, got, "analytics:events:abc:7")
	}
}
