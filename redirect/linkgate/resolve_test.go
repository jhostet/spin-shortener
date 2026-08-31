package linkgate

import (
	"encoding/json"
	"errors"
	"fmt"
	"testing"
	"time"
)

func TestResolve_ZeroValueOfDispositionIsUnavailable(t *testing.T) {
	// Pins the fail-safe direction: a reordering of the iota block must not
	// silently make the zero value "claim the link is absent".
	var d Disposition
	if d != DispositionUnavailable {
		t.Errorf("zero value of Disposition = %v, want DispositionUnavailable", d)
	}
}

func TestResolve_GetErrorIsUnavailable(t *testing.T) {
	store := fakeStore{getErr: errors.New("too many requests")}
	l, disp, _ := Resolve(store, "abc123", time.Now())
	if disp != DispositionUnavailable {
		t.Errorf("disp = %v, want DispositionUnavailable", disp)
	}
	if l != (Link{}) {
		t.Errorf("l = %+v, want zero value", l)
	}
}

// TestResolve_GetErrorIsReturnedUnchanged pins that the error Resolve
// returns alongside DispositionUnavailable is EXACTLY the error store.Get
// produced — unwrapped, unmodified — which is what lets a caller log the
// host's own message (docs/plans/observable-kv-failures.md).
func TestResolve_GetErrorIsReturnedUnchanged(t *testing.T) {
	wantErr := errors.New("too many requests")
	store := fakeStore{getErr: wantErr}
	_, disp, err := Resolve(store, "abc123", time.Now())
	if disp != DispositionUnavailable {
		t.Errorf("disp = %v, want DispositionUnavailable", disp)
	}
	if err != wantErr {
		t.Errorf("err = %v, want the exact error fakeStore.getErr produced (%v)", err, wantErr)
	}
}

// TestResolve_ErrorIsNilForNotFoundRedirectAndPrompt pins the other half of
// the contract: DispositionNotFound, DispositionRedirect and
// DispositionPrompt mean nothing failed, so there is nothing to report and
// the error must always be nil. (DispositionUnreadable is NOT in this set any
// more — see TestResolve_UnreadableReturnsExactlyTheParseError and
// TestResolve_UnreadableErrorIsUnwrapped, which pin its own non-nil
// contract.)
func TestResolve_ErrorIsNilForNotFoundRedirectAndPrompt(t *testing.T) {
	now := time.Now()

	cases := map[Disposition]KVStore{
		DispositionNotFound: fakeStore{getResult: []byte("")},
		DispositionRedirect: fakeStore{getResult: []byte(
			`{"slug":"abc123","target_url":"https://example.com","status":"active","password_hash":"","start_at":"","end_at":""}`,
		)},
		DispositionPrompt: fakeStore{getResult: []byte(
			`{"slug":"abc123","target_url":"https://example.com","status":"active","password_hash":"somehash","start_at":"","end_at":""}`,
		)},
	}

	for wantDisp, store := range cases {
		_, disp, err := Resolve(store, "abc123", now)
		if disp != wantDisp {
			t.Errorf("disp = %v, want %v", disp, wantDisp)
		}
		if err != nil {
			t.Errorf("disposition %v: err = %v, want nil", disp, err)
		}
	}
}

// TestResolve_UnreadableReturnsExactlyTheParseError pins that Resolve's error
// alongside DispositionUnreadable is exactly what ParseLink produced on the
// same bytes — not merely "some error".
func TestResolve_UnreadableReturnsExactlyTheParseError(t *testing.T) {
	raw := []byte("not json")
	store := fakeStore{getResult: raw}
	_, disp, err := Resolve(store, "abc123", time.Now())
	if disp != DispositionUnreadable {
		t.Fatalf("disp = %v, want DispositionUnreadable", disp)
	}
	if err == nil {
		t.Fatal("err = nil, want the ParseLink error")
	}
	_, wantErr := ParseLink(raw)
	if wantErr == nil {
		t.Fatal("test fixture invalid: ParseLink(raw) did not error")
	}
	if err.Error() != wantErr.Error() {
		t.Errorf("err = %q, want exactly %q", err.Error(), wantErr.Error())
	}
}

// TestResolve_UnreadableErrorIsUnwrapped pins that Resolve does not wrap
// ParseLink's error: errors.As must find the concrete decoder type, AND %T
// must report that concrete type rather than a wrapper's. The %T half is the
// one that fails if someone later wraps the error, which would silently
// degrade the ev=record_unreadable log line's etype field
// (docs/plans/disposition-unreadable-logging.md).
func TestResolve_UnreadableErrorIsUnwrapped(t *testing.T) {
	store := fakeStore{getResult: []byte("not json")}
	_, disp, err := Resolve(store, "abc123", time.Now())
	if disp != DispositionUnreadable {
		t.Fatalf("disp = %v, want DispositionUnreadable", disp)
	}

	var se *json.SyntaxError
	if !errors.As(err, &se) {
		t.Errorf("errors.As did not find a *json.SyntaxError in %v", err)
	}
	if gotType := fmt.Sprintf("%T", err); gotType != "*json.SyntaxError" {
		t.Errorf("%%T = %q, want \"*json.SyntaxError\" (error must be unwrapped)", gotType)
	}
}

func TestResolve_AbsentKeyIsNotFound(t *testing.T) {
	store := fakeStore{getResult: []byte(""), getErr: nil}
	_, disp, _ := Resolve(store, "abc123", time.Now())
	if disp != DispositionNotFound {
		t.Errorf("disp = %v, want DispositionNotFound", disp)
	}
}

func TestResolve_UnparseableRecordIsUnreadable(t *testing.T) {
	store := fakeStore{getResult: []byte("not json")}
	_, disp, _ := Resolve(store, "abc123", time.Now())
	if disp != DispositionUnreadable {
		t.Errorf("disp = %v, want DispositionUnreadable", disp)
	}
}

// TestResolve_ControlCharTargetIsUnreadable pins the redirect's refusal to
// emit a control-character Location header: such a record is present but
// cannot be safely served, so it resolves to DispositionUnreadable -> 500
// with ErrUnsafeTargetURL, exactly like an unparseable record — the
// wire-safety half of "a fault must never be dressed up as a product state".
// This matters even though api/links.py's target_url_error rejects control
// chars at all four authoring paths: restore writes records WITHOUT the
// authoring choke point by design, and a hand-edited store can contain
// anything.
func TestResolve_ControlCharTargetIsUnreadable(t *testing.T) {
	store := fakeStore{getResult: []byte(
		`{"slug":"abc","target_url":"https://example.com/\r\nX-Evil: yes","status":"active"}`)}
	_, disp, err := Resolve(store, "abc", time.Now())
	if disp != DispositionUnreadable {
		t.Errorf("disp = %v, want DispositionUnreadable", disp)
	}
	if !errors.Is(err, ErrUnsafeTargetURL) {
		t.Errorf("err = %v, want ErrUnsafeTargetURL", err)
	}
}

func TestResolve_ControlFreeTargetStillRedirects(t *testing.T) {
	// Guard against the guard: a percent-encoded control sequence is inert
	// literal text in the header and must keep resolving.
	rec := `{"slug":"abc123","target_url":"https://example.com/x%0d%0a","status":"active","password_hash":"","start_at":"","end_at":""}`
	store := fakeStore{getResult: []byte(rec)}
	_, disp, _ := Resolve(store, "abc123", time.Now())
	if disp != DispositionRedirect {
		t.Errorf("disp = %v, want DispositionRedirect", disp)
	}
}

func TestResolve_ActiveNoPasswordIsRedirect(t *testing.T) {
	rec := `{"slug":"abc123","target_url":"https://example.com","status":"active","password_hash":"","start_at":"","end_at":""}`
	store := fakeStore{getResult: []byte(rec)}
	l, disp, _ := Resolve(store, "abc123", time.Now())
	if disp != DispositionRedirect {
		t.Errorf("disp = %v, want DispositionRedirect", disp)
	}
	if l.TargetURL != "https://example.com" {
		t.Errorf("TargetURL = %q, want intact", l.TargetURL)
	}
}

func TestResolve_ActiveWithPasswordIsPrompt(t *testing.T) {
	rec := `{"slug":"abc123","target_url":"https://example.com","status":"active","password_hash":"somehash","start_at":"","end_at":""}`
	store := fakeStore{getResult: []byte(rec)}
	_, disp, _ := Resolve(store, "abc123", time.Now())
	if disp != DispositionPrompt {
		t.Errorf("disp = %v, want DispositionPrompt", disp)
	}
}

func TestResolve_DisabledIsNotFound(t *testing.T) {
	rec := `{"slug":"abc123","target_url":"https://example.com","status":"disabled","password_hash":"","start_at":"","end_at":""}`
	store := fakeStore{getResult: []byte(rec)}
	_, disp, _ := Resolve(store, "abc123", time.Now())
	if disp != DispositionNotFound {
		t.Errorf("disp = %v, want DispositionNotFound", disp)
	}
}

func TestResolve_FutureStartAtIsNotFound(t *testing.T) {
	rec := `{"slug":"abc123","target_url":"https://example.com","status":"active","password_hash":"","start_at":"2999-01-01T00:00:00Z","end_at":""}`
	store := fakeStore{getResult: []byte(rec)}
	_, disp, _ := Resolve(store, "abc123", time.Now())
	if disp != DispositionNotFound {
		t.Errorf("disp = %v, want DispositionNotFound", disp)
	}
}

func TestResolve_PastEndAtIsNotFound(t *testing.T) {
	rec := `{"slug":"abc123","target_url":"https://example.com","status":"active","password_hash":"","start_at":"","end_at":"2020-01-01T00:00:00Z"}`
	store := fakeStore{getResult: []byte(rec)}
	_, disp, _ := Resolve(store, "abc123", time.Now())
	if disp != DispositionNotFound {
		t.Errorf("disp = %v, want DispositionNotFound", disp)
	}
}

func TestResolve_UnparseableStartAtIsNotFound(t *testing.T) {
	// Pins IsWithinWindow's existing fail-closed behaviour through the new
	// seam: a non-empty but unparseable start_at must fail closed, not be
	// silently ignored.
	rec := `{"slug":"abc123","target_url":"https://example.com","status":"active","password_hash":"","start_at":"not-a-date","end_at":""}`
	store := fakeStore{getResult: []byte(rec)}
	_, disp, _ := Resolve(store, "abc123", time.Now())
	if disp != DispositionNotFound {
		t.Errorf("disp = %v, want DispositionNotFound", disp)
	}
}

// TestResolve_AbsentDisabledAndOutOfWindowAreEqualToEachOther is the
// probing-resistance pin. It is not enough for each case to map to
// DispositionNotFound independently — they must be indistinguishable from
// EACH OTHER, so a future change cannot tease them apart for a "better
// error message" without this test failing.
func TestResolve_AbsentDisabledAndOutOfWindowAreEqualToEachOther(t *testing.T) {
	now := time.Now()

	absentStore := fakeStore{getResult: []byte("")}
	_, absentDisp, _ := Resolve(absentStore, "abc123", now)

	disabledRec := `{"slug":"abc123","target_url":"https://example.com","status":"disabled","password_hash":"","start_at":"","end_at":""}`
	disabledStore := fakeStore{getResult: []byte(disabledRec)}
	_, disabledDisp, _ := Resolve(disabledStore, "abc123", now)

	windowRec := `{"slug":"abc123","target_url":"https://example.com","status":"active","password_hash":"","start_at":"","end_at":"2020-01-01T00:00:00Z"}`
	windowStore := fakeStore{getResult: []byte(windowRec)}
	_, windowDisp, _ := Resolve(windowStore, "abc123", now)

	if absentDisp != disabledDisp || disabledDisp != windowDisp {
		t.Errorf("absent=%v disabled=%v out-of-window=%v, want all three equal to each other",
			absentDisp, disabledDisp, windowDisp)
	}
}

func TestResolve_ReadsExactlyLinkKeyOfSlug(t *testing.T) {
	var capturedKey string
	rec := `{"slug":"my-slug","target_url":"https://example.com","status":"active","password_hash":"","start_at":"","end_at":""}`
	store := fakeStore{getResult: []byte(rec), getKeyCapture: &capturedKey}

	Resolve(store, "my-slug", time.Now())

	want := LinkKey("my-slug")
	if capturedKey != want {
		t.Errorf("Get called with key %q, want %q", capturedKey, want)
	}
}

// TestOldCollapse_SanityCheck reproduces the old lookupLink collapse inline,
// as a fixture for the mutation-verification step
// docs/plans/redirect-read-failure-not-404.md requires be run and reported:
// re-introduce the collapse INSIDE Resolve itself (not here — this
// function's body is a standalone, never-changed reference implementation),
// re-run `go test ./linkgate/...`, and confirm TestResolve_GetErrorIsUnavailable
// is the only failure. This test only pins that the reference collapse
// below reproduces the old bug shape; it is not itself the mutation check.
func TestOldCollapse_SanityCheck(t *testing.T) {
	collapsedResolve := func(store KVStore, slug string, now time.Time) Disposition {
		raw, err := store.Get(LinkKey(slug))
		if err != nil || len(raw) == 0 {
			return DispositionNotFound
		}
		l, err := ParseLink(raw)
		if err != nil {
			return DispositionUnreadable
		}
		if l.Status != "active" || !IsWithinWindow(l.StartAt, l.EndAt, now) {
			return DispositionNotFound
		}
		if l.PasswordHash != "" {
			return DispositionPrompt
		}
		return DispositionRedirect
	}

	store := fakeStore{getErr: errors.New("boom")}
	got := collapsedResolve(store, "abc123", time.Now())
	if got != DispositionNotFound {
		t.Fatalf("sanity check: collapsed resolver returned %v, want DispositionNotFound (confirming the reference collapse reproduces the old bug shape)", got)
	}
}
