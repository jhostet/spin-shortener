package linkgate

import (
	"errors"
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
	l, disp := Resolve(store, "abc123", time.Now())
	if disp != DispositionUnavailable {
		t.Errorf("disp = %v, want DispositionUnavailable", disp)
	}
	if l != (Link{}) {
		t.Errorf("l = %+v, want zero value", l)
	}
}

func TestResolve_AbsentKeyIsNotFound(t *testing.T) {
	store := fakeStore{getResult: []byte(""), getErr: nil}
	_, disp := Resolve(store, "abc123", time.Now())
	if disp != DispositionNotFound {
		t.Errorf("disp = %v, want DispositionNotFound", disp)
	}
}

func TestResolve_UnparseableRecordIsUnreadable(t *testing.T) {
	store := fakeStore{getResult: []byte("not json")}
	_, disp := Resolve(store, "abc123", time.Now())
	if disp != DispositionUnreadable {
		t.Errorf("disp = %v, want DispositionUnreadable", disp)
	}
}

func TestResolve_ActiveNoPasswordIsRedirect(t *testing.T) {
	rec := `{"slug":"abc123","target_url":"https://example.com","status":"active","password_hash":"","start_at":"","end_at":""}`
	store := fakeStore{getResult: []byte(rec)}
	l, disp := Resolve(store, "abc123", time.Now())
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
	_, disp := Resolve(store, "abc123", time.Now())
	if disp != DispositionPrompt {
		t.Errorf("disp = %v, want DispositionPrompt", disp)
	}
}

func TestResolve_DisabledIsNotFound(t *testing.T) {
	rec := `{"slug":"abc123","target_url":"https://example.com","status":"disabled","password_hash":"","start_at":"","end_at":""}`
	store := fakeStore{getResult: []byte(rec)}
	_, disp := Resolve(store, "abc123", time.Now())
	if disp != DispositionNotFound {
		t.Errorf("disp = %v, want DispositionNotFound", disp)
	}
}

func TestResolve_FutureStartAtIsNotFound(t *testing.T) {
	rec := `{"slug":"abc123","target_url":"https://example.com","status":"active","password_hash":"","start_at":"2999-01-01T00:00:00Z","end_at":""}`
	store := fakeStore{getResult: []byte(rec)}
	_, disp := Resolve(store, "abc123", time.Now())
	if disp != DispositionNotFound {
		t.Errorf("disp = %v, want DispositionNotFound", disp)
	}
}

func TestResolve_PastEndAtIsNotFound(t *testing.T) {
	rec := `{"slug":"abc123","target_url":"https://example.com","status":"active","password_hash":"","start_at":"","end_at":"2020-01-01T00:00:00Z"}`
	store := fakeStore{getResult: []byte(rec)}
	_, disp := Resolve(store, "abc123", time.Now())
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
	_, disp := Resolve(store, "abc123", time.Now())
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
	_, absentDisp := Resolve(absentStore, "abc123", now)

	disabledRec := `{"slug":"abc123","target_url":"https://example.com","status":"disabled","password_hash":"","start_at":"","end_at":""}`
	disabledStore := fakeStore{getResult: []byte(disabledRec)}
	_, disabledDisp := Resolve(disabledStore, "abc123", now)

	windowRec := `{"slug":"abc123","target_url":"https://example.com","status":"active","password_hash":"","start_at":"","end_at":"2020-01-01T00:00:00Z"}`
	windowStore := fakeStore{getResult: []byte(windowRec)}
	_, windowDisp := Resolve(windowStore, "abc123", now)

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
