package linkgate

import "time"

// Disposition is what a /r/{slug} handler must do about one request.
//
// The zero value is DispositionUnavailable, deliberately: this whole type
// exists because a fault was being reported as "no such link", so an unset
// or unhandled disposition must fail towards "the server has a problem",
// never towards a claim about the link.
type Disposition int

const (
	DispositionUnavailable Disposition = iota // KV read failed; nothing is known about the link
	DispositionRedirect                       // active, in window, no password
	DispositionPrompt                         // active, in window, password required
	DispositionNotFound                       // absent, disabled, or outside its window
	DispositionUnreadable                     // record present, will not parse
)

func (d Disposition) String() string {
	switch d {
	case DispositionUnavailable:
		return "unavailable"
	case DispositionRedirect:
		return "redirect"
	case DispositionPrompt:
		return "prompt"
	case DispositionNotFound:
		return "not_found"
	case DispositionUnreadable:
		return "unreadable"
	default:
		return "unknown"
	}
}

// Resolve performs the single KV read for slug and decides the disposition,
// replacing the old lookupLink, which collapsed a KV read failure, a
// genuinely absent key and an unparseable record into one indistinguishable
// "not found" (see docs/plans/redirect-read-failure-not-404.md). Exactly ONE
// KV data operation, the same one lookupLink performed — a successful
// redirect's KV op count is unaffected by this change.
//
// Order of decisions is load-bearing:
//
//  1. A Get error means the read failed and NOTHING is known about the link
//     — DispositionUnavailable, not DispositionNotFound. Every error is
//     treated identically (no variant check, no string match on the error
//     text): the SDK has no typed error variant to match on, and a
//     substring match on "too many requests" would be control flow that a
//     vendor's wording change could silently defeat (see CLAUDE.md,
//     "Write-throttle resilience").
//  2. An empty (zero-length) result with no error means the key is
//     genuinely absent — DispositionNotFound. A stored link record is
//     always JSON and can never be zero-length; the SDK's Get returns
//     []byte("") with a nil error for a missing key. This check stays
//     explicit rather than left to ParseLink's json.Unmarshal failing, so
//     absence is a stated condition, not a side effect of a decoder.
//  3. A ParseLink failure on a non-empty result means the record is present
//     but unusable — either it will not parse (corrupt JSON / type mismatch)
//     or it parses but cannot be safely served (a target_url carrying ASCII
//     control characters would be emitted verbatim into an unvalidated
//     Location header, see ParseLink) — DispositionUnreadable. This is a
//     fault (a human needs to look at the record), not a product state.
//  4. status != "active" or outside [start_at, end_at) — DispositionNotFound.
//     Deliberately indistinguishable from "absent": this is a
//     probing-resistance property (CLAUDE.md, "Security tradeoffs"), not an
//     oversight, and it must stay that way.
//  5. A non-empty password_hash — DispositionPrompt.
//  6. Otherwise — DispositionRedirect.
//
// Deliberately ONE KV data operation, not two. The Exists probe this used to
// do ahead of the Get was pure overhead: the SDK's Get returns
// ([]byte(""), nil) for a missing key — an empty slice with NO error, see
// spin-go-sdk/v3 kv.Store.Get — so an absent key is already distinguishable
// without asking first. Measured on the deployed Akamai app 2026-08-06:
// removing this probe saved 5.2 ms of 38.8 ms, or 13.5%, measured
// like-for-like against the 7-op build in the same latency window. Akamai
// charges per data operation and barely at all per store handle (~150 µs),
// so one fewer data op is the whole win. Do NOT read the absolute figure as
// a constant — the same 7 operations measured 116.7 ms earlier the same day,
// a 3x regime swing, so per-op cost there ranged 5.5-16.7 ms. The stable
// statement is "one operation's worth, ~14% of the redirect's KV time".
// Locally the probe measured 13-18 us and was correctly judged not worth
// removing; the local numbers do not transfer. See CLAUDE.md's Akamai
// section.
//
// Returning the Link alongside a NotFound disposition (cases 4) is harmless
// and keeps the signature uniform; callers must not read it, and no caller
// does.
//
// The returned error is non-nil for exactly TWO dispositions, and its meaning
// differs between them — so a caller must switch on the disposition FIRST and
// read the error only inside an arm it has already identified. Both handlers
// in main.go structurally do that; "err != nil" alone must never be read as
// "the store failed".
//
//   - DispositionUnavailable — exactly the error store.Get produced,
//     unmodified and unwrapped (docs/plans/observable-kv-failures.md), so a
//     caller can log the host's own message.
//   - DispositionUnreadable — exactly the error ParseLink produced, unmodified
//     and unwrapped (docs/plans/disposition-unreadable-logging.md). Do NOT wrap
//     it: unwrapped, fmt's %T yields the concrete decoder type, which
//     distinguishes a truncated write (*json.SyntaxError) from a schema
//     mismatch between what api writes and what Link expects
//     (*json.UnmarshalTypeError) with no string matching at all.
//
// DispositionNotFound, DispositionRedirect and DispositionPrompt always return
// a nil error: nothing failed, so there is no message to capture, and a
// non-nil error alongside one of those would invite a caller to log it as if
// it meant something it doesn't.
//
// This REPLACES the narrower rule this comment used to state ("non-nil ONLY
// alongside DispositionUnavailable"), whose stated reason — that there is "no
// unknown host message to capture for ... an unparseable record" — was simply
// wrong about the record case. json.Unmarshal's error was always real and
// always diagnostically useful; Resolve was throwing it away. It is also the
// ONLY place in the application that says why a record will not parse:
// api/consistency.py's unreadable_value finding carries {store, key} and no
// reason at all.
func Resolve(store KVStore, slug string, now time.Time) (Link, Disposition, error) {
	raw, err := store.Get(LinkKey(slug))
	if err != nil {
		return Link{}, DispositionUnavailable, err
	}
	if len(raw) == 0 {
		return Link{}, DispositionNotFound, nil
	}

	l, err := ParseLink(raw)
	if err != nil {
		return Link{}, DispositionUnreadable, err
	}

	if l.Status != "active" {
		return l, DispositionNotFound, nil
	}
	if !IsWithinWindow(l.StartAt, l.EndAt, now) {
		return l, DispositionNotFound, nil
	}

	if l.PasswordHash != "" {
		return l, DispositionPrompt, nil
	}
	return l, DispositionRedirect, nil
}
