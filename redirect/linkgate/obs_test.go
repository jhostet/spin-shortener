package linkgate

import (
	"errors"
	"strings"
	"testing"
	"time"
)

// fakeStore is a minimal KVStore fake, keyed by name only — no real KV
// involved, so these tests never need a fake server or WASI runtime.
type fakeStore struct {
	existsResult bool
	existsErr    error
	getResult    []byte
	getErr       error
	setErr       error

	// getKeyCapture, if non-nil, receives the key Get was called with. A
	// pointer field so capture works even though fakeStore has value
	// receivers and is passed around by value (resolve_test.go's key-shape
	// pin needs this; no other test sets it, so this is additive).
	getKeyCapture *string
}

func (f fakeStore) Exists(key string) (bool, error) { return f.existsResult, f.existsErr }
func (f fakeStore) Get(key string) ([]byte, error) {
	if f.getKeyCapture != nil {
		*f.getKeyCapture = key
	}
	return f.getResult, f.getErr
}
func (f fakeStore) Set(key string, value []byte) error {
	return f.setErr
}

func TestCollectorTotals_Empty(t *testing.T) {
	c := NewCollector()
	ops, us, bytes := c.Totals()
	if ops != 0 || us != 0 || bytes != 0 {
		t.Errorf("Totals() on empty collector = (%d, %d, %d), want all zero", ops, us, bytes)
	}
}

func TestCollectorTotals_Nil(t *testing.T) {
	var c *Collector
	ops, us, bytes := c.Totals()
	if ops != 0 || us != 0 || bytes != 0 {
		t.Errorf("Totals() on nil collector = (%d, %d, %d), want all zero", ops, us, bytes)
	}
	// Record on a nil collector must not panic — this is what makes
	// collectorFrom's no-op collector free to implement as a plain nil.
	c.Record("get", "links", 5*time.Microsecond, 10)
}

func TestCollector_CountAndTotalMicros(t *testing.T) {
	c := NewCollector()
	c.Record("get", "links", 5*time.Microsecond, 100)
	c.Record("get", "links", 7*time.Microsecond, 50)
	c.Record("set", "analytics", 12*time.Microsecond, 20)

	ops, us, bytes := c.Totals()
	if ops != 3 {
		t.Errorf("Totals() ops = %d, want 3", ops)
	}
	if us != 24 {
		t.Errorf("Totals() us = %d, want 24", us)
	}
	if bytes != 170 {
		t.Errorf("Totals() bytes = %d, want 170", bytes)
	}
}

func TestRenderLogLine_FieldFormatAndOmission(t *testing.T) {
	c := NewCollector()
	c.Record("open", "-", 20*time.Microsecond, 0)
	c.Record("open", "-", 15*time.Microsecond, 0)
	c.Record("exists", "links", 17*time.Microsecond, 0)
	c.Record("get", "links", 11*time.Microsecond, 262)

	line := RenderLogLine(
		[]Field{{"comp", "redirect"}, {"route", "/r/{slug}"}, {"status", "302"}},
		174*time.Microsecond,
		c,
	)

	// count/total_µs field format for each present op type.
	if !strings.Contains(line, "open=2/35") {
		t.Errorf("line %q missing open=2/35", line)
	}
	if !strings.Contains(line, "exists=1/17") {
		t.Errorf("line %q missing exists=1/17", line)
	}
	if !strings.Contains(line, "get=1/11") {
		t.Errorf("line %q missing get=1/11", line)
	}

	// Zero-count fields (set, delete, list_keys) must be omitted entirely,
	// never rendered as "set=0/0".
	for _, absent := range []string{"set=", "delete=", "list_keys="} {
		if strings.Contains(line, absent) {
			t.Errorf("line %q contains zero-count field %q, want omitted", line, absent)
		}
	}

	if !strings.HasPrefix(line, "ss comp=redirect route=/r/{slug} status=302 dur_us=174") {
		t.Errorf("line %q does not start with expected prefix", line)
	}

	if !strings.Contains(line, "kv_ops=4") {
		t.Errorf("line %q missing kv_ops=4", line)
	}
	if !strings.Contains(line, "kv_bytes=262") {
		t.Errorf("line %q missing kv_bytes=262", line)
	}
}

func TestRenderLogLine_SlowestOperationIncludingOpenDashNamespace(t *testing.T) {
	c := NewCollector()
	c.Record("open", "-", 20*time.Microsecond, 0)
	c.Record("exists", "links", 5*time.Microsecond, 0)

	line := RenderLogLine(nil, 30*time.Microsecond, c)
	if !strings.Contains(line, "slow=open:-:20") {
		t.Errorf("line %q missing slow=open:-:20 (the open/'-' namespace case)", line)
	}
}

func TestRenderLogLine_SlowestOperationUpdatesToLaterLargerDuration(t *testing.T) {
	c := NewCollector()
	c.Record("exists", "links", 5*time.Microsecond, 0)
	c.Record("get", "analytics", 40*time.Microsecond, 4)

	line := RenderLogLine(nil, 50*time.Microsecond, c)
	if !strings.Contains(line, "slow=get:analytics:40") {
		t.Errorf("line %q missing slow=get:analytics:40", line)
	}
}

func TestRenderLogLine_NilCollectorOmitsKVSummaryEntirely(t *testing.T) {
	line := RenderLogLine([]Field{{"comp", "api"}, {"status", "401"}}, 12*time.Microsecond, nil)
	want := "ss comp=api status=401 dur_us=12"
	if line != want {
		t.Errorf("RenderLogLine with nil collector = %q, want %q", line, want)
	}
}

func TestRenderServerTiming_MicrosecondsRenderAsMilliseconds(t *testing.T) {
	c := NewCollector()
	c.Record("open", "-", 80*time.Microsecond, 0)

	got := RenderServerTiming(174*time.Microsecond, c)
	want := `kv;dur=0.080;desc="1 ops", handler;dur=0.174`
	if got != want {
		t.Errorf("RenderServerTiming() = %q, want %q", got, want)
	}
}

func TestRenderServerTiming_NilCollector(t *testing.T) {
	got := RenderServerTiming(174*time.Microsecond, nil)
	want := `kv;dur=0.000;desc="0 ops", handler;dur=0.174`
	if got != want {
		t.Errorf("RenderServerTiming() with nil collector = %q, want %q", got, want)
	}
}

func TestNamespace(t *testing.T) {
	cases := map[string]string{
		"links:slug:abc":      "links",
		"analytics:count:abc": "analytics",
		"users:user:admin":    "-",
		"":                    "-",
	}
	for key, want := range cases {
		if got := Namespace(key); got != want {
			t.Errorf("Namespace(%q) = %q, want %q", key, got, want)
		}
	}
}

func TestParseLogLevel(t *testing.T) {
	cases := map[string]string{
		"summary": "summary",
		"off":     "off",
		"":        "off",
		"SUMMARY": "off",
		"verbose": "off",
		"garbage": "off",
	}
	for raw, want := range cases {
		if got := ParseLogLevel(raw); got != want {
			t.Errorf("ParseLogLevel(%q) = %q, want %q", raw, got, want)
		}
	}
}

func TestTokenMatches_EmptyConfiguredNeverMatches(t *testing.T) {
	if TokenMatches("", "") {
		t.Error("TokenMatches(\"\", \"\") = true, want false — empty configured token must never match")
	}
	if TokenMatches("", "anything") {
		t.Error("TokenMatches(\"\", \"anything\") = true, want false")
	}
}

func TestTokenMatches_CorrectAndWrongToken(t *testing.T) {
	if !TokenMatches("secret123", "secret123") {
		t.Error("TokenMatches with matching token = false, want true")
	}
	if TokenMatches("secret123", "wrong") {
		t.Error("TokenMatches with wrong token = true, want false")
	}
	if TokenMatches("secret123", "") {
		t.Error("TokenMatches with empty provided against a real configured token = true, want false")
	}
}

func TestTimedStore_RecordsExistsGetSet(t *testing.T) {
	fs := fakeStore{existsResult: true, getResult: []byte("0123456789"), setErr: nil}
	c := NewCollector()
	ts := TimedStore{Store: fs, Collector: c}

	if _, err := ts.Exists("links:slug:abc"); err != nil {
		t.Fatalf("Exists: %v", err)
	}
	if _, err := ts.Get("links:slug:abc"); err != nil {
		t.Fatalf("Get: %v", err)
	}
	if err := ts.Set("analytics:count:abc", []byte("xyz")); err != nil {
		t.Fatalf("Set: %v", err)
	}

	ops, _, bytes := c.Totals()
	if ops != 3 {
		t.Errorf("Totals() ops = %d, want 3", ops)
	}
	// Get moved 10 bytes, Set moved 3.
	if bytes != 13 {
		t.Errorf("Totals() bytes = %d, want 13", bytes)
	}
}

func TestTimedStore_NilCollectorIsNoOp(t *testing.T) {
	fs := fakeStore{existsResult: true, getResult: []byte("x"), setErr: errors.New("boom")}
	ts := TimedStore{Store: fs, Collector: nil}

	if _, err := ts.Exists("links:slug:abc"); err != nil {
		t.Fatalf("Exists: %v", err)
	}
	if _, err := ts.Get("links:slug:abc"); err != nil {
		t.Fatalf("Get: %v", err)
	}
	if err := ts.Set("links:slug:abc", []byte("x")); err == nil {
		t.Fatalf("Set: want error to propagate through nil-collector wrapper")
	}
}
