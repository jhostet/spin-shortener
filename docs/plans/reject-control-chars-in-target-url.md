# Reject control characters in destination URLs

**Filed from code review** (2026-08-30, Finding 1). The finding and its
reproduction are recorded in the conversation that produced it; this plan
records the fix, the wiring, and what was deliberately not done.

## The defect

A destination URL carrying ASCII control characters is accepted by every
authoring path and then emitted **verbatim** as the `Location` header of the
redirect component's 302:

1. `api/links.py`'s `is_valid_target_url` validates through `urlparse`, but the
   **original string is stored**, not the parsed one. `urlparse` silently strips
   `\t\r\n` from its parsed view — so `https://example.com/\r\nX-Evil: yes`
   *passes* the parser — while the stored record retains the raw bytes. NUL,
   `0x01–0x08`, `0x0b–0x0c`, `0x0e–0x1f` and `0x7f` additionally survive
   `urlparse` verbatim (measured).
2. `redirect` performs **zero validation of `TargetURL`** — `main.go`'s
   `sendRedirectThenRecord` sets it on the header map directly.
3. `toWasiHeaders` (`spin-go-sdk/v3@v3.0.0/http/http.go:92`) serializes header
   **values** as raw bytes; only header **names** are syntax-checked (read from
   the SDK source).

Result: a literal CRLF in a live 302. Whether the host/XSS layer rejects the
response or splits the header is host-dependent — both are unacceptable, and a
lenient host silently produces header injection. Reachable by any authenticated
user (link creation needs no special permission).

## The fix — two halves, one per component that can write or emit the bytes

### Half 1: the authoring choke point (`api/links.py`)

`_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x1f\x7f]")`, checked in
`target_url_error` — the **single** function all four authoring paths already
funnel through (`links.handle_create`, `links.handle_update`,
`bulk.handle_bulk_create` via `validate_bulk_rows`, and `bulk.handle_bulk_action`'s
`repoint` branch). The same "a constraint enforced in two of three places is not
enforced" rule the length cap carries.

Reuses the existing `"invalid_target_url"` error code rather than adding a new
one, deliberately: a control-bearing URL is indistinguishable from "not a URL"
to every client, the GUI already maps that code to a sensible message, and a
distinct code would add surface for an input only ever crafted as an attack.

### Half 2: the wire-safety guard (`redirect/linkgate`)

Storage is not only written by the choke point: `backup.handle_restore` writes
records **without** re-validating their content by design, and a hand-edited
store can contain anything. So the reader — the one component that places
`target_url` onto the wire — must refuse a record it cannot safely emit.

`ParseLink` (the single "is this record servable?" decision, called exactly once
per redirect) now rejects a `target_url` containing ASCII controls with the new
sentinel `ErrUnsafeTargetURL`. This flows through `Resolve` as
`DispositionUnreadable` → 500 with the data-free error page and one
`ev=record_unreadable` stderr line carrying the slug and the message
("target_url contains control characters"). No new disposition, no new `ev`
kind, no handler changes: a record that cannot be served is a record that is
unreadable-as-a-servable-link, the exact same fault class as a record that will
not parse (`{"status": 7}` etc.), which already 500s this way. This extends the
documented "api's notion of unreadable is narrower than `linkgate.ParseLink`'s"
divergence by one case, exactly along the existing axis.

The sentinel message is deliberately free of `:` so
`SanitizeErrorMessage`'s key-shaped redaction can never mangle it.

### What is deliberately NOT done

- **No rejection of percent-encoded controls** (`%0d%0a`). They are inert
  literal text inside the header value, decoded only by the *new* URL the
  Location points at, never by this app's emission. Pinned both sides.
- **No distinct error code / no GUI change.** See Half 1.
- **No new disposition, no new `ev` kind.** See Half 2's rationale; the
  existing `unreadable` fault class already has exactly the right status
  (500), page (data-free), and logging (deduped, `msg` last) semantics.
- **No scan/sanitize on the redirect hot path** beyond the O(len(target))
  byte scan inside `ParseLink` — one pure loop, no KV op, negligible. It runs
  exactly once per request, folded into the decode it already performs.
- **No change to `qr.py`'s `content-disposition` filename** (a separate
  cosmetic nitpick; the slug there is existence-checked against stored records,
  which the API-side slug pattern already bounds).
- **No consistency-check change**: a control-char target parses as JSON, so it
  is not structural drift; the redirect's 500 + `ev=record_unreadable` line is
  the diagnosis, matching how `{"status": 7}` type mismatches are handled today.

## Tests

- `api/tests/test_target_url_control_chars.py` — all four authoring paths, each
  parametrized over CRLF/LF/tab/NUL/ESC/DEL (authority-position CRLF included),
  each asserting `400 invalid_target_url` AND "nothing was written" (the
  all-or-nothing bulk discipline included). Plus a narrowness guard: a
  control-free URL and a percent-encoded one still validate. Mirrors
  `test_url_policy_enforcement.py`'s structure.
- `redirect/linkgate/link_test.go` — `ParseLink` table rejects
  CRLF/LF/tab/NUL/ESC/DEL targets (`errors.Is(err, ErrUnsafeTargetURL)`), and
  accepts a percent-encoded target.
- `redirect/linkgate/resolve_test.go` — a CRLF-target record resolves
  `DispositionUnreadable` with `ErrUnsafeTargetURL`; a percent-encoded target
  still resolves `DispositionRedirect`.

**Mutation-verified 2026-08-30:** removing Half 1's check failed 24/25 API
tests; removing Half 2's check failed the Go Resolve test. Both restored.

## Files

- `api/links.py` (`_CONTROL_CHAR_PATTERN`, `target_url_error`)
- `redirect/linkgate/link.go` (`ErrUnsafeTargetURL`, `hasControlChars`, `ParseLink`)
- `redirect/linkgate/resolve.go` (doc-comment step 3 only)
- `api/tests/test_target_url_control_chars.py` (new)
- `redirect/linkgate/link_test.go`, `redirect/linkgate/resolve_test.go`
- `TASKS.md`, `CLAUDE.md` (documentation)