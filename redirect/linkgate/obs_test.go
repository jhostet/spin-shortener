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

// --- Observable KV failures (docs/plans/observable-kv-failures.md) ---

func TestSanitizeErrorMessage_RedactsKeyShapedSubstringAndDropsTheOriginalWord(t *testing.T) {
	msg := "read failed for users:session:9f8a7b6c-abcd"
	sanitized, redacted, truncated := SanitizeErrorMessage(msg)
	if strings.Contains(sanitized, "9f8a7b6c-abcd") {
		t.Errorf("sanitized = %q, must not contain the token", sanitized)
	}
	if strings.Contains(sanitized, "session") {
		t.Errorf("sanitized = %q, must not contain the word 'session'", sanitized)
	}
	if !strings.Contains(sanitized, "[key:users]") {
		t.Errorf("sanitized = %q, want it to contain [key:users]", sanitized)
	}
	if !redacted {
		t.Errorf("redacted = false, want true")
	}
	if truncated {
		t.Errorf("truncated = true, want false")
	}
}

func TestSanitizeErrorMessage_RedactsPbkdf2HashToken(t *testing.T) {
	msg := "value was pbkdf2_sha256$100000$saltsalt$hashhash rejected"
	sanitized, redacted, _ := SanitizeErrorMessage(msg)
	want := "value was [hash] rejected"
	if sanitized != want {
		t.Errorf("sanitized = %q, want %q", sanitized, want)
	}
	if !redacted {
		t.Errorf("redacted = false, want true")
	}
}

func TestSanitizeErrorMessage_LeavesKeyValueErrorColonSpaceIntact(t *testing.T) {
	msg := "key-value error: internal server error"
	sanitized, redacted, truncated := SanitizeErrorMessage(msg)
	if sanitized != msg {
		t.Errorf("sanitized = %q, want unchanged %q", sanitized, msg)
	}
	if redacted {
		t.Errorf("redacted = true, want false")
	}
	if truncated {
		t.Errorf("truncated = true, want false")
	}
}

func TestSanitizeErrorMessage_TruncatesAt200AndSetsTruncated(t *testing.T) {
	msg := strings.Repeat("x", 300)
	sanitized, redacted, truncated := SanitizeErrorMessage(msg)
	if len(sanitized) != MaxErrorMessageChars {
		t.Errorf("len(sanitized) = %d, want %d", len(sanitized), MaxErrorMessageChars)
	}
	if !truncated {
		t.Errorf("truncated = false, want true")
	}
	if redacted {
		t.Errorf("redacted = true, want false")
	}
}

func TestSanitizeErrorMessage_ReplacesControlCharsAndNewlines(t *testing.T) {
	sanitized, _, _ := SanitizeErrorMessage("line one\nline two\ttabbed")
	if strings.ContainsAny(sanitized, "\n\t") {
		t.Errorf("sanitized = %q, must not contain control characters", sanitized)
	}
}

func TestSanitizeErrorMessage_MsgLastIsUpToTheCaller(t *testing.T) {
	// SanitizeErrorMessage itself has no opinion on field ordering; that
	// property lives in RenderFailureLine, tested below.
	sanitized, _, _ := SanitizeErrorMessage("too many requests")
	if sanitized != "too many requests" {
		t.Errorf("sanitized = %q, want unchanged", sanitized)
	}
}

func TestSanitizeSlugForLog_ValidSlugPassesThroughUnchanged(t *testing.T) {
	for _, slug := range []string{"promo", "Summer_Sale-2026", "a", strings.Repeat("x", 128)} {
		if got := SanitizeSlugForLog(slug); got != slug {
			t.Errorf("SanitizeSlugForLog(%q) = %q, want unchanged", slug, got)
		}
	}
}

func TestSanitizeSlugForLog_RejectsWhitespaceThatWouldSplitAField(t *testing.T) {
	// Confirmed live before this fix: a slug containing a space split the
	// "slug=" field in two ("slug=a b" reads as slug=a plus a bare token
	// "b"), corrupting every field emitted after it.
	if got := SanitizeSlugForLog("a b"); got != "[invalid_slug]" {
		t.Errorf("SanitizeSlugForLog(%q) = %q, want the placeholder", "a b", got)
	}
}

func TestSanitizeSlugForLog_RejectsNewlineThatWouldForgeASecondLine(t *testing.T) {
	// Confirmed live before this fix: a slug containing \n split the LINE in
	// two, letting one unauthenticated request forge a second, fully-formed
	// "ss "-prefixed line indistinguishable from a genuine one.
	malicious := "x\nss comp=redirect ev=kv_fail route=/r/{slug} slug=FORGED op=set ns=users etype=access_denied msg=fake"
	got := SanitizeSlugForLog(malicious)
	if got != "[invalid_slug]" {
		t.Errorf("SanitizeSlugForLog(%q) = %q, want the placeholder", malicious, got)
	}
	if strings.Contains(got, "\n") {
		t.Errorf("SanitizeSlugForLog(%q) = %q, must not contain a newline", malicious, got)
	}
}

func TestSanitizeSlugForLog_ReturnedPlaceholderCarriesNoOriginalBytes(t *testing.T) {
	malicious := "super-secret-probe-value-should-never-appear"
	got := SanitizeSlugForLog(malicious + "\n")
	if strings.Contains(got, malicious) {
		t.Errorf("SanitizeSlugForLog result = %q, must not contain any byte of the input", got)
	}
}

func TestSanitizeSlugForLog_RejectsOverlongInput(t *testing.T) {
	if got := SanitizeSlugForLog(strings.Repeat("x", 129)); got != "[invalid_slug]" {
		t.Errorf("SanitizeSlugForLog(129 x's) = %q, want the placeholder", got)
	}
}

func TestSanitizeSlugForLog_RejectsEmptyString(t *testing.T) {
	if got := SanitizeSlugForLog(""); got != "[invalid_slug]" {
		t.Errorf("SanitizeSlugForLog(\"\") = %q, want the placeholder", got)
	}
}

func TestSanitizeHostForLog_ValidHostPassesThroughUnchanged(t *testing.T) {
	for _, host := range []string{"localhost:3000", "127.0.0.1:3000", "trrk.io", "[::1]:8080", "a.b.example.com"} {
		if got := SanitizeHostForLog(host); got != host {
			t.Errorf("SanitizeHostForLog(%q) = %q, want unchanged", host, got)
		}
	}
}

func TestSanitizeHostForLog_EmptyHostIsADash(t *testing.T) {
	if got := SanitizeHostForLog(""); got != "-" {
		t.Errorf(`SanitizeHostForLog("") = %q, want "-"`, got)
	}
}

func TestSanitizeHostForLog_RejectsWhitespaceThatWouldSplitAField(t *testing.T) {
	if got := SanitizeHostForLog("a b"); got != "[invalid_host]" {
		t.Errorf("SanitizeHostForLog(%q) = %q, want the placeholder", "a b", got)
	}
}

func TestSanitizeHostForLog_RejectsNewlineThatWouldForgeASecondLine(t *testing.T) {
	malicious := "x\nss comp=redirect ev=kv_fail route=/r/{slug} op=set ns=users etype=access_denied msg=fake"
	got := SanitizeHostForLog(malicious)
	if got != "[invalid_host]" {
		t.Errorf("SanitizeHostForLog(%q) = %q, want the placeholder", malicious, got)
	}
	if strings.Contains(got, "\n") {
		t.Errorf("SanitizeHostForLog(%q) = %q, must not contain a newline", malicious, got)
	}
}

func TestHostUnresolvedLine_FieldOrderAndMsgLast(t *testing.T) {
	line, _ := HostUnresolvedLine()
	want := "ss comp=redirect ev=host_unresolved route=/r/{slug} msg=request carries no host header, domain-restricted links cannot resolve"
	if line != want {
		t.Errorf("line = %q, want %q", line, want)
	}
}

func TestHostUnresolvedLine_CarriesNoSlugOpNsOrEtype(t *testing.T) {
	line, _ := HostUnresolvedLine()
	for _, forbidden := range []string{"slug=", "op=", "ns=", "etype="} {
		if strings.Contains(line, forbidden) {
			t.Errorf("line = %q, must not contain %q", line, forbidden)
		}
	}
}

func TestHostUnresolvedLine_DedupKeyIsDisjointFromTheOtherTwoKeySpaces(t *testing.T) {
	_, dedupKey := HostUnresolvedLine()
	if dedupKey != "host_unresolved" {
		t.Errorf("dedupKey = %q, want the fixed literal %q", dedupKey, "host_unresolved")
	}
	kvKey := KVFailureDedupKey("get", "some message")
	recordKey := RecordUnreadableDedupKey("some-slug", "some message")
	if dedupKey == kvKey || dedupKey == recordKey {
		t.Errorf("dedupKey %q collides with another key space", dedupKey)
	}
	if strings.HasPrefix(dedupKey, "record_unreadable"+dedupKeySep) {
		t.Errorf("dedupKey %q must not fall inside RecordUnreadableDedupKey's key space", dedupKey)
	}
}

func TestRenderFailureLine_MsgIsFinalFieldAndNothingFollows(t *testing.T) {
	line := RenderFailureLine([]Field{
		{Key: "comp", Value: "redirect"},
		{Key: "ev", Value: "kv_fail"},
		{Key: "etype", Value: "other"},
		{Key: "msg", Value: "too many requests"},
	})
	want := "ss comp=redirect ev=kv_fail etype=other msg=too many requests"
	if line != want {
		t.Errorf("line = %q, want %q", line, want)
	}
	if !strings.HasSuffix(line, "msg=too many requests") {
		t.Errorf("line = %q, want it to end with the msg field", line)
	}
}

func TestRenderFailureLine_FieldOrderIsExactlyAsGiven(t *testing.T) {
	line := RenderFailureLine([]Field{{Key: "b", Value: "2"}, {Key: "a", Value: "1"}})
	want := "ss b=2 a=1"
	if line != want {
		t.Errorf("line = %q, want %q", line, want)
	}
}

// --- ev=record_unreadable (docs/plans/disposition-unreadable-logging.md) ---

func TestKVFailureDedupKey_IsOpNulMsg(t *testing.T) {
	got := KVFailureDedupKey("get", "too many requests")
	want := "get\x00too many requests"
	if got != want {
		t.Errorf("KVFailureDedupKey = %q, want %q", got, want)
	}
}

// TestRecordUnreadableDedupKey_IsPrefixedWithRecordUnreadable only pins the
// literal prefix and message suffix, deliberately NOT the slug's exact
// position — TestRecordUnreadableLine_TwoDifferentSlugsWithIdenticalMessageProduceDifferentDedupKeys
// below is the one test that must fail if the slug argument stops mattering,
// per the plan's mutation-check spec ("no other test fail").
func TestRecordUnreadableDedupKey_IsPrefixedWithRecordUnreadable(t *testing.T) {
	got := RecordUnreadableDedupKey("abc123", "some message")
	if !strings.HasPrefix(got, "record_unreadable\x00") {
		t.Errorf("RecordUnreadableDedupKey = %q, want prefix %q", got, "record_unreadable\x00")
	}
	if !strings.HasSuffix(got, "\x00some message") {
		t.Errorf("RecordUnreadableDedupKey = %q, want suffix %q", got, "\x00some message")
	}
}

func TestRecordUnreadableLine_SyntaxErrorFromNotJSON(t *testing.T) {
	_, parseErr := ParseLink([]byte("not json"))
	if parseErr == nil {
		t.Fatal("test fixture invalid: ParseLink did not error on \"not json\"")
	}

	line, dedupKey := RecordUnreadableLine("abc123", parseErr)

	wantPrefix := "ss comp=redirect ev=record_unreadable route=/r/{slug} slug=abc123 etype=*json.SyntaxError msg="
	if !strings.HasPrefix(line, wantPrefix) {
		t.Errorf("line = %q, want prefix %q", line, wantPrefix)
	}
	if strings.Contains(line, " op=") || strings.Contains(line, " ns=") {
		t.Errorf("line = %q, must not contain an op or ns field", line)
	}
	if !strings.HasSuffix(line, "msg="+dedupKey[strings.LastIndex(dedupKey, "\x00")+1:]) {
		t.Errorf("line = %q, want it to end with the same sanitized msg the dedup key carries", line)
	}
	wantDedupKey := RecordUnreadableDedupKey("abc123", dedupKey[strings.LastIndex(dedupKey, "\x00")+1:])
	if dedupKey != wantDedupKey {
		t.Errorf("dedupKey = %q, want %q", dedupKey, wantDedupKey)
	}
}

func TestRecordUnreadableLine_UnmarshalTypeErrorFromSchemaMismatch(t *testing.T) {
	rec := `{"slug":"abc123","target_url":"https://example.com","status":7}`
	_, parseErr := ParseLink([]byte(rec))
	if parseErr == nil {
		t.Fatal("test fixture invalid: ParseLink did not error on a numeric status")
	}

	line, _ := RecordUnreadableLine("abc123", parseErr)

	if !strings.Contains(line, "etype=*json.UnmarshalTypeError") {
		t.Errorf("line = %q, want etype=*json.UnmarshalTypeError", line)
	}
}

func TestRecordUnreadableLine_UnmarshalTypeErrorMessageSurvivesSanitizationUnredacted(t *testing.T) {
	rec := `{"slug":"abc123","target_url":"https://example.com","status":7}`
	_, parseErr := ParseLink([]byte(rec))
	if parseErr == nil {
		t.Fatal("test fixture invalid")
	}

	line, _ := RecordUnreadableLine("abc123", parseErr)

	if strings.Contains(line, "msg_redacted=1") {
		t.Errorf("line = %q, a `json: cannot unmarshal ...` message must survive sanitization unredacted", line)
	}
	if !strings.Contains(line, "json: cannot unmarshal") {
		t.Errorf("line = %q, want the decoder message intact", line)
	}
}

func TestRecordUnreadableLine_TruncatesAt200AndSetsTruncated(t *testing.T) {
	longErr := errors.New(strings.Repeat("x", 250))
	line, _ := RecordUnreadableLine("abc123", longErr)

	if !strings.Contains(line, "msg_truncated=1") {
		t.Errorf("line = %q, want msg_truncated=1", line)
	}
	if strings.Contains(line, strings.Repeat("x", 250)) {
		t.Errorf("line = %q, want the message truncated, not carried in full", line)
	}
}

// TestRecordUnreadableLine_TwoDifferentSlugsWithIdenticalMessageProduceDifferentDedupKeys
// is the load-bearing test for this whole feature: two corrupt records
// commonly produce the identical decoder message, and a message-keyed dedup
// would hide every corrupt slug after the first one an instance meets.
func TestRecordUnreadableLine_TwoDifferentSlugsWithIdenticalMessageProduceDifferentDedupKeys(t *testing.T) {
	sameErr := errors.New("unexpected end of JSON input")

	_, dedupKeyA := RecordUnreadableLine("slug-a", sameErr)
	_, dedupKeyB := RecordUnreadableLine("slug-b", sameErr)

	if dedupKeyA == dedupKeyB {
		t.Errorf("dedupKeyA = %q, dedupKeyB = %q, want them to differ (different slugs)", dedupKeyA, dedupKeyB)
	}
}

// TestDedupKeys_KVFailureAndRecordUnreadableSpacesAreDisjoint pins that no
// (op, msg) pair can ever produce a KVFailureDedupKey equal to any
// RecordUnreadableDedupKey, since the two kinds share one map and one cap.
func TestDedupKeys_KVFailureAndRecordUnreadableSpacesAreDisjoint(t *testing.T) {
	// For every real KV op name, a KVFailureDedupKey can never collide with
	// any RecordUnreadableDedupKey, because RecordUnreadableDedupKey's first
	// NUL-separated segment is always the fixed literal "record_unreadable",
	// which is not, and never will be, a real op name.
	recordKey := RecordUnreadableDedupKey("abc123", "some message")
	for _, op := range []string{"open", "exists", "get", "set", "delete", "list_keys"} {
		got := KVFailureDedupKey(op, "abc123\x00some message")
		if got == recordKey {
			t.Errorf("collision: KVFailureDedupKey(%q, ...) == RecordUnreadableDedupKey(...) = %q", op, got)
		}
	}
}

func TestRecordUnreadableLine_NilErrOmitsEtypeAndUsesDashMessage(t *testing.T) {
	line, dedupKey := RecordUnreadableLine("abc123", nil)

	if strings.Contains(line, "etype=") {
		t.Errorf("line = %q, want no etype field for a nil err", line)
	}
	if !strings.HasSuffix(line, "msg=-") {
		t.Errorf("line = %q, want it to end with msg=-", line)
	}
	wantDedupKey := RecordUnreadableDedupKey("abc123", "-")
	if dedupKey != wantDedupKey {
		t.Errorf("dedupKey = %q, want %q", dedupKey, wantDedupKey)
	}
}
