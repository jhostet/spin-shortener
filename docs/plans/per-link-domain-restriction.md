# Per-Link Domain Restriction

## Context

The deployment's `public_base_urls` now lists a branded domain, `trrk.io`, that
is **not yet live in DNS** (confirmed NXDOMAIN) — it was added deliberately so
it can be exercised ahead of going live. That is a staged rollout, and it is
the requirement that this plan exists for. The user's words: *"We need to plan
out a function to only allow some links to work on select domains."*

Today that is impossible. `redirect/main.go` resolves purely by
`links:slug:<slug>` → KV → 302 and **never reads the `Host` header at all**, so
every link resolves on every domain that points at this deployment, forever,
with no way to say otherwise. There is no per-link domain field of any kind.

**This plan supersedes a rejection.** `docs/plans/multi-domain-display.md`
designed exactly this feature and explicitly rejected both halves of it, and
`TASKS.md`'s `## Considered and rejected` entry (2026-08-02, "A per-link
`domain` field, and `Host`-header enforcement in `redirect` to make it honest")
records the reasoning in full. That entry names its own revisit condition:

> Revisit only if a real requirement appears to make a link resolve on exactly
> one domain — and then take both halves together, plus an explicit decision
> that a mismatched `Host` returns 404 to match the time-window behaviour.

That condition has fired, for real and not hypothetically. This plan takes both
halves together and makes the 404 decision explicitly. The three stated reasons
for the original rejection are each answered head-on in
"Trade-offs and rejected alternatives" below — they are not silently re-decided:

1. *Hot-path cost in the one Go component kept deliberately minimal* — answered
   with an operation-level costing: **zero additional KV operations**, one map
   lookup, and no work at all for an unrestricted link.
2. *A new failure mode where a link tests fine and 404s in production because a
   CDN/proxy/DNS rewrote `Host`* — this is the real risk and it is **not**
   dismissed. It is answered structurally: a measurement task lands and is
   verified on the deployed app **before** any enforcement ships, the observed
   host becomes a permanent, greppable diagnostic, and an unresolvable host
   emits an unconditional failure line.
3. *Slugs are already enumerable and non-secret, so this buys no security
   property* — accepted as true and **not** the justification. This is an
   availability/routing control, in the same family as `status` and the
   `[start_at, end_at)` window, neither of which buys a confidentiality
   property either.

**This is a DIFFERENT concept from `assigned_domains`, and the vocabulary
collides.** State it wherever both appear:

| | `assigned_domains` (on a **user** record) | `allowed_domains` (on a **link** record) |
|---|---|---|
| what it restricts | which domains the viewer's nav selector **offers** them | which domains the link actually **resolves** on |
| enforced where | nowhere server-side | `redirect`, on the hot path, per request |
| CLAUDE.md's own words | "a convenience guardrail... **not a security control**... gates nothing server-side" | a real, server-side, hot-path restriction |
| absent/`[]` means | unrestricted | unrestricted |

They are structurally independent: neither reads the other, and a user assigned
one domain can still be handed a link restricted to another.

**Confirmed decisions** (settled by the user before planning — not reopened):

1. A link's restriction is an **allowlist of domains**, plural on both sides —
   a subset of the configured `public_base_urls`, not a single domain.
2. **Absent or empty means unrestricted** — today's exact behaviour, so every
   existing link record needs no backfill and no migration.
3. **Enforcement happens in `redirect` (Go), on the hot path, via the `Host`
   header.** A GUI-only version would not satisfy "only allow… to work".
4. **A domain mismatch joins the existing indistinguishable-404 family**
   (absent / disabled / out-of-window), pinned as *equal*, not merely as three
   values that each happen to map to 404.
5. **All four link-authoring paths enforce it**, the same rule the destination
   URL policy carries — a restriction settable at creation but droppable via
   another path is not enforced.
6. **The allowlist is validated against the deployment's actual
   `public_base_urls`** (`api/domains.py`), the way `assigned_domains` already
   is in `api/users.py`.

## Key technical facts confirmed during research

- **`redirect` reads no host of any kind today.** `redirect/main.go`'s two
  handlers take `r.PathValue("slug")` and nothing else from the request except
  `X-SS-Debug` and (on POST) the form body. Confirmed by reading
  `handleRedirectGet`/`handleRedirectPost` (`main.go:312-381`).

- **The Spin Go SDK does NOT populate `http.Request.Host`, and drops the WASI
  authority entirely.** `spin-go-sdk/v3@v3.0.0/http/convertor_incoming_request.go`
  builds the request as `http.NewRequest(method, pathWithQuery, body)` — a
  *relative* URL — so `req.URL.Host` and therefore `req.Host` are `""`. The
  incoming `wasi.Request` **does** expose `GetAuthority()`
  (`imports/wasi_http_0_3_0_rc_2026_03_15_types/wit_bindings.go:3593`), but
  `newHttpRequest` never calls it and `ir.Drop()`s the request before returning.
  Bypassing the SDK to reach it would mean replacing its `handler.Exports.Handle`
  registration — out of the question.

- **Consequence: the ONLY reachable source is the copied `host` header.**
  `toHttpHeader` (`convertor_incoming_request.go:72-78`) copies every field
  verbatim into `req.Header` with no filtering and no special-casing of `Host`
  — so unlike a normal Go server, `r.Header.Get("Host")` is meaningful here and
  `r.Host` is not. The plan reads `r.Host` first anyway (free, and correct if a
  future SDK starts populating it) and falls back to the header.

- **Spin 4.0.2 does NOT inject the `spin-full-url` / `spin-path-info` headers
  the SDK still defines constants for.** `grep -ao "spin-[a-z-]\{4,24\}"` over
  the `spin` binary (4.0.2, bfc7543) returns only CLI-internal strings —
  `spin-full-url`, `spin-matched-route`, `spin-client-addr` and
  `spin-component-route` appear **zero** times, while `x-forwarded-for` appears.
  So `spinhttp.HeaderFullUrl` is a stale SDK constant, not a usable fallback.

- **UNCONFIRMED, and it is the gating fact: whether the `host` header is
  actually present in the WASI fields, locally under Spin 4.0.2 and on Akamai
  Functions.** wasmtime's wasi-http builds incoming fields from the hyper header
  map, which carries `host` for HTTP/1.1, so it is *expected* to be there
  locally; Akamai Functions publishes nothing about its host runtime. Nothing in
  this repo has ever read it. **Confirming it is Task 1**, it is confirmed by
  reading a `host=` field off the existing summary log line, and no enforcement
  task may land before it is confirmed **on the deployed app as well as
  locally**.

- **UNCONFIRMED, and it is a property-side question, not an app one: whether
  the Akamai property in front of `trrk.io` forwards the incoming `Host`
  unchanged.** `docs/plans/toggleable-redirect-prefix.md` (landed 2026-09-03,
  commit `dffc339`) establishes that an edge property will rewrite
  `https://go.example.com/{slug}` → `/r/{slug}` in front of this app. Akamai
  Property Manager's "Forward Host Header" behaviour can be set to the origin
  hostname, which would present every request to this app as
  `<app-id>.fwf.app` and make every restriction unmatchable. This app cannot
  detect or fix that; it becomes a **third property-side requirement** in
  CLAUDE.md, alongside the two that plan already recorded.

- **`Cache-Control: no-store` and the 302-not-301 rule already cover this
  feature with no new code.** `setSecurityHeaders` (`main.go:78-102`) sets
  `no-store` on every response including 404s, and Akamai does not cache 302s.
  A domain-restricted 404 must not be cached — a cached "not allowed here" would
  survive the restriction being lifted — and it already is not.

- **A new field on an existing key type imposes NONE of the three
  "new KV key type" obligations.** `api/backup.py` base64s every value verbatim
  (`INDEX_KEYS`/`restore_write_order` key on key *names*), `kvprefix.STORE_PREFIXES`
  keys on prefixes, and `consistency._parse_link_record` (`consistency.py:129-141`)
  reads **only** `owner` — so an unknown or even malformed `allowed_domains`
  can never produce an `unreadable_value` or `unrecognized_key` finding.

- **Every bulk action preserves unknown record fields already.** `_plan_status`,
  `_plan_tag`, `_plan_untag`, `_plan_reassign`, `_plan_repoint` and
  `_plan_schedule` (`bulk.py:350-386`) each mutate specific keys on the fully
  parsed record and re-serialize it, as do `links.handle_update` and
  `handle_set_password`. So no existing write path can silently drop
  `allowed_domains`. This is pinned by test rather than assumed.

- **`ACTION_SPECS` makes a new bulk action structurally safe.** `BULK_ACTIONS`
  is `frozenset(ACTION_SPECS)` (`bulk.py:462`) and `_apply_mutations` takes no
  `action` parameter, so a new name cannot reach a write path without a planner
  and cannot fall into another action's loop.

- **`GET /api/links/{slug}/qr` already fetches the link record** (`qr.py:74`,
  for the `can_view` gate), so refusing to encode a QR for a domain the link
  does not resolve on costs **zero** additional KV operations.

- **`gui/theme.css:569-586` already groups `.slug-kind-badge`, `.lock-badge`
  and `.tag-chip` into one shared rule** (inline-block, 0.75rem, weight 600,
  `--ss-slate-500`). A `.domain-badge` joins that selector list — **no new
  design token**, and it inherits the "neutral, not Signal Blue" decision
  recorded in the comment there.

- **The stale-entry footgun is already solved once, for `assigned_domains`, and
  its solution is WRONG here.** `gui/admin/users.js` renders a
  stored-but-no-longer-configured domain as a **checked, disabled** checkbox and
  excludes it from the payload with `:checked:not(:disabled)`, so the next save
  silently drops it. For a *link* that would silently widen a restriction —
  the link would start resolving everywhere. See "API changes" for the
  `also_allowed` rule that replaces it.

- **Local dev can prove enforcement with no DNS at all.**
  `SPIN_VARIABLE_PUBLIC_BASE_URLS="http://localhost:3000,http://127.0.0.1:3000"`
  (the exact two-domain setup CLAUDE.md's "Multi-domain display" already
  documents) gives two configured domains that reach the same server and differ
  **only** in the `Host` header curl sends. A link restricted to
  `http://127.0.0.1:3000` must 404 on `localhost:3000` and 302 on
  `127.0.0.1:3000`.

- **Baseline test counts, run 2026-09-03 at `dffc339` (clean tree):**
  `cd api && uv run pytest` → **777 passed**;
  `cd gui-pages && uv run pytest` → **170 passed**;
  `cd redirect && go test ./linkgate/...` → **ok**, 107 top-level test functions.

- **UNCONFIRMED, and a documented limitation rather than a bug: internationalized
  domain names.** Browsers send punycode in `Host`, while
  `domains.normalize_base_url` does nothing but lowercase the netloc — neither
  side IDNA-encodes. So a Unicode domain must be configured in
  `public_base_urls` in its punycode form or it will never match. This blind
  spot already exists today for `assigned_domains` and `?base=`; this plan
  inherits it rather than introducing it, and documents it.

## Data model

The `links:slug:<slug>` record gains **one optional field**:

```
allowed_domains: list[str]     # absent, null or [] means "unrestricted"
```

Values are **full base URLs**, byte-identical to entries in `public_base_urls`
(e.g. `["https://trrk.io"]`) — not bare hostnames. Rationale in Trade-offs #3.

Two rules that are easy to get wrong and are load-bearing:

- **Absent, `null` and `[]` are all "unrestricted" and are treated identically
  everywhere.** No existing record changes; no migration; no backfill. New
  records always write the key (`[]` when unrestricted), the same way `tags`
  does, purely for shape consistency.
- **`redirect` matches on the HOSTNAME only.** The scheme and port in a stored
  entry are ignored by the redirect. This is not sloppiness — behind a
  TLS-terminating edge the origin sees plain `http` on some other port, so a
  scheme- or port-sensitive comparison would 404 every restricted link in
  production. The consequence, stated plainly: if `http://trrk.io` and
  `https://trrk.io` were both configured, a link restricted to one resolves on
  the other. Nothing in this app can distinguish them and nothing should try.

`public_link` gains one line beside the existing `tags` normalization, so every
API response carries a list rather than sometimes omitting the key:

```python
public["allowed_domains"] = record.get("allowed_domains") or []
```

**No new KV key type, therefore none of the three obligations.** `backup.py`
needs no change (values are base64'd verbatim), `consistency.py` needs no change
(`_parse_link_record` reads only `owner`), and `kvprefix.STORE_PREFIXES` needs no
change (the key name is unchanged). Each of those three is pinned by a test
rather than merely asserted here — see the Tasks.

**Restore deliberately bypasses validation**, exactly as it does for the
destination URL policy, so a restored record may name a domain that is no longer
configured. That is safe here for a reason worth stating: **`redirect` never
reads `public_base_urls`.** Matching is record-vs-request-host only, so removing
a domain from configuration never retroactively 404s a link, and a stale entry
keeps working for as long as DNS points that hostname at the deployment.

## Redirect (Go) changes

### New pure module: `redirect/linkgate/domaingate.go`

Zero `spin-go-sdk` imports, host-testable, **no `regexp` and no allocation on
the unrestricted path**. (`SanitizeSlugForLog` uses a package-level regexp, but
only on the logging path; this code runs on every redirect.)

```go
// NormalizeHost lowercases an authority and strips the port, IPv6 brackets and
// a trailing dot. Returns "" if nothing usable remains, or if the result
// carries a byte outside [a-z0-9._-] (plus ':' inside brackets).
func NormalizeHost(raw string) string

// HostFromBaseURL extracts the normalized host from a stored allowed_domains
// entry: "https://Trrk.IO:443/" -> "trrk.io". Tolerates an entry with no
// scheme, and drops any userinfo before the last '@' (normalize_base_url
// accepts "http://user@host", so an entry CAN carry one).
func HostFromBaseURL(entry string) string

// HostAllowed reports whether a request arriving with rawHost may resolve a
// link whose record carries `allowed`.
//
//   - len(allowed) == 0  -> true, unconditionally: unrestricted, today's
//     behaviour, and the ONLY branch the overwhelming majority of requests
//     take. It returns before any host work happens at all.
//   - rawHost normalizes to "" -> FALSE. Fail closed, deliberately: a
//     restriction that evaporates when the host is unknown is not a
//     restriction. This is the same direction IsWithinWindow already fails on
//     an unparseable start_at.
//   - otherwise -> membership of NormalizeHost(rawHost) in the entries' hosts.
func HostAllowed(allowed []string, rawHost string) bool
```

`HostAllowed` normalizes `rawHost` **itself**, after the `len(allowed) == 0`
early return, so a caller can never be wrong about whether normalization already
happened and an unrestricted link pays nothing.

### `redirect/linkgate/link.go`

```go
type Link struct {
	Slug           string   `json:"slug"`
	TargetURL      string   `json:"target_url"`
	Owner          string   `json:"owner"`
	Custom         bool     `json:"custom"`
	PasswordHash   string   `json:"password_hash"`
	Status         string   `json:"status"`
	StartAt        string   `json:"start_at"`
	EndAt          string   `json:"end_at"`
	AllowedDomains []string `json:"allowed_domains"`   // NEW
	CreatedAt      string   `json:"created_at"`
	UpdatedAt      string   `json:"updated_at"`
}
```

`ParseLink` is otherwise untouched: a JSON `null` or an absent key unmarshals to
a nil slice, which `HostAllowed` reads as unrestricted. A malformed
`allowed_domains` (say a JSON string where a list belongs) makes
`json.Unmarshal` return a `*json.UnmarshalTypeError`, which becomes
`DispositionUnreadable` → 500 + `ev=record_unreadable` through the existing
machinery, with no new code. That divergence from `api`'s laxer notion of
"unreadable" is the established three-way divergence CLAUDE.md already documents.

### `redirect/linkgate/resolve.go`

```go
func Resolve(store KVStore, slug string, now time.Time, rawHost string) (Link, Disposition, error)
```

One new step, inserted **after** the window check and **before** the password
check, as step 5 in the doc comment's numbered list:

```go
	if !HostAllowed(l.AllowedDomains, rawHost) {
		return l, DispositionNotFound, nil
	}
```

**Position is load-bearing, not incidental.** Before the password check, a
restricted link on the wrong domain returns 404; after it, the same link would
answer with a password prompt — disclosing both that the link exists and that it
is protected, on a domain it does not serve. Both new-decision facts belong in
the doc comment.

The doc comment's disposition table gains one row and its
"Deliberately indistinguishable from absent" note gains one member.

### `redirect/main.go`

```go
// rawRequestHost returns the request's authority as the runtime supplied it,
// unnormalized. r.Host is checked first (free, and correct if a future SDK
// starts populating it from the WASI authority) and is "" today, because
// spin-go-sdk builds the request from a RELATIVE path — see
// convertor_incoming_request.go. The header is the real source: the SDK copies
// every WASI field verbatim, Host included.
func rawRequestHost(r *http.Request) string {
	if r.Host != "" {
		return r.Host
	}
	return r.Header.Get("Host")
}
```

Both handlers gain two lines, structurally identical to each other exactly as
they already are everywhere else:

```go
	host := rawRequestHost(r)
	if host == "" {
		emitHostUnresolvedLine()
	}
	l, disp, resolveErr := linkgate.Resolve(store, slug, time.Now(), host)
```

### The new failure line: `ev=host_unresolved`

A **fifth** `ev` kind in the vocabulary CLAUDE.md's "Observable KV failures"
section maintains, and the first that reports a *deployment* fault rather than a
KV or decoder one. Unconditional — independent of `log_level` and `X-SS-Debug`,
like every other `ev=` line.

```
ss comp=redirect ev=host_unresolved route=/r/{slug} msg=request carries no host header, domain-restricted links cannot resolve
```

- Fires when `rawRequestHost` returns `""` — i.e. the runtime gave this
  component nothing to match on. It does **not** fire for an ordinary
  domain mismatch: a mismatch is a product state, like out-of-window, and
  logging one per click would be volume with no signal.
- Carries **no `slug`** (nothing about it is link-specific), **no `op`/`ns`**
  (no KV operation failed — none has even been attempted yet), and **no
  `etype`** (there is no exception to classify). `msg` is a fixed literal and
  is last, as the doctrine requires.
- Rendered by `linkgate.HostUnresolvedLine() (line, dedupKey string)`,
  mirroring `RecordUnreadableLine`'s shape so every decision stays
  host-testable, and gated through the existing `shouldEmitFailureLine`. The
  dedup key is the fixed literal `"host_unresolved"`, disjoint from
  `KVFailureDedupKey`'s (always an op name) and `RecordUnreadableDedupKey`'s
  (always the `"record_unreadable"` prefix), sharing the same 32-key
  per-instance budget. **Effect: at most one such line per Wasm instance,
  ever** — which is exactly right for a condition that is either always true or
  always false for a given deployment.

### The new summary-line field: `host=`

`emitLogLine` (`main.go:185-195`) gains one field between `slug` and `status`:

```go
	fields = append(fields, linkgate.Field{Key: "host", Value: linkgate.SanitizeHostForLog(host)})
```

`linkgate.SanitizeHostForLog(raw string) string` returns `raw` unchanged when it
matches `^[A-Za-z0-9._:\[\]-]{1,253}$`, the fixed placeholder `[invalid_host]`
otherwise, and the literal `-` for an empty host. It exists for the same
confirmed reason `SanitizeSlugForLog` does: the value is request-controlled and
is **not** the last field on the line, so a `Host` header containing a space or
a newline would split the field or forge a whole second `ss `-prefixed line.
`-` rather than omitting the field is deliberate — "the host was empty" is the
answer Task 1 exists to obtain, and it must be a positive, greppable statement
rather than an absence.

This field is **the whole of Task 1** and it lands, is verified locally and on
the deployed app, and is reported **before any enforcement task begins**. It is
kept permanently afterwards: it is the standing answer to "what host is the
origin actually seeing?", which is the one question a broken restriction will
ever raise.

### Hot-path cost, itemized

The original rejection's first objection, answered at operation granularity.

| | before | after |
|---|---|---|
| KV operations, successful redirect | 5 | **5** |
| KV operations, miss | 2 | **2** |
| Spin variable reads | unchanged | unchanged |
| per request, host unavailable-path | — | one `!= ""` string compare |
| per request, host present | — | one struct-field read, one `http.Header` map lookup |
| per request, **unrestricted** link | — | one `len(nil) == 0` check, then return |
| per request, **restricted** link | — | one lowercase-and-scan of ≤253 bytes, plus ≤N byte-compares over the record's own entries |
| allocations, unrestricted link | — | **none** (nil slice, no host normalization runs) |

Against a measured ~20 ms per Akamai KV data operation, and with KV at ~97% of
handler time on that platform, this is unmeasurable. `redirect` gains no Spin
variable, no outbound access, no dependency, and **`[component.redirect.variables]`
is not touched** — the component still never learns what `public_base_urls`
contains, which is what keeps a configuration edit from retroactively breaking a
link.

## API changes

### `api/domains.py` — one new pure function

```python
def normalize_allowed_domains(
    value,
    configured: list[str],
    also_allowed: list[str] | None = None,
) -> tuple[list[str] | None, str | None]:
    """Canonicalize a link's allowed_domains, or say why it is invalid.

    Returns (canonical_list, None), or (None, "invalid_allowed_domains").

    `value` may be None or [] (both meaning "unrestricted"), or a list of
    strings. Each member is put through normalize_base_url first, so a
    differently-cased or trailing-slashed form of a configured domain is
    accepted and STORED IN THE SERVER'S OWN CANONICAL FORM, never the
    caller's -- the same property resolve_base_url carries for ?base=.

    `configured` is the deployment's public_base_urls and is the membership
    test. `also_allowed` is the record's CURRENT stored list on an UPDATE
    path only: an entry already in the record stays valid even after an
    operator removes that domain from public_base_urls, so a stale entry can
    be kept or deliberately removed, but can never be silently widened away
    and can never make the record unsaveable. Create, bulk-create and the
    bulk `restrict` action pass nothing.

    Order is `configured` order first, then any retained `also_allowed`
    entry in the order given, de-duplicated -- so two equivalent submissions
    produce byte-identical records.
    """
```

Notes that matter:

- **No separate cap.** The list is bounded by `len(configured)` (plus retained
  stale entries) by construction, so a `MAX_ALLOWED_DOMAINS` would be a constant
  guarding nothing. Unlike `MAX_TAGS_PER_LINK`, whose vocabulary is unbounded.
- **One error code for every failure mode** (`invalid_allowed_domains`) — a
  non-list, a non-string member, a member that does not normalize, a member not
  in the allowed set. That is exactly `_validate_permissions`' and
  `_validate_assigned_domains`' precedent, and it is deliberate: enumerating
  which member failed would be more helpful and is not worth diverging from a
  twice-established shape. The GUI already knows the configured list and can say
  which chip is wrong.

### The four authoring paths

Enforced in all four, the same rule and for the same reason as the destination
URL policy — and `api/tests/test_allowed_domains_enforcement.py` exists to prove
all four reject the same unconfigured domain and write nothing, mirroring
`api/tests/test_url_policy_enforcement.py` file-for-file.

| # | path | how the field arrives | semantics |
|---|---|---|---|
| 1 | `links.handle_create` | optional `allowed_domains` in the payload | absent → `[]` |
| 2 | `links.handle_update` | `UPDATABLE_FIELDS` gains `"allowed_domains"` | **key presence decides**: absent leaves it untouched, a list replaces wholesale, `null` or `[]` clears |
| 3 | `bulk.handle_bulk_create` | batch-level `allowed_domains`, applied to every link created in that submission | absent → `[]` |
| 4 | `bulk.handle_bulk_action`, new `action: "restrict"` | `allowed_domains` on the payload | replaces wholesale on every selected slug; `[]` clears |

Signature changes — `configured_domains` is a **required positional with no
default** in every one of them, which is `validate_bulk_rows`' `policy`
precedent verbatim: *"a `policy=None` default meaning 'no policy' is exactly how
the destination URL policy's third enforcement path would stay silently open
forever."*

```python
async def handle_create(store, principal, request, configured_domains, write=kvretry.direct)
async def handle_update(store, principal, slug, request, configured_domains, write=kvretry.direct)
async def handle_bulk_create(store, principal, request, configured_domains, get_many, write)
async def handle_bulk_action(store, users_store, principal, request, configured_domains, get_many, write)
```

`api/app.py` already computes `configured_domains` once per request
(`app.py:344`) and passes it to `/auth/me`, `/api/users*` and the QR endpoint;
the four call sites above join that list. No new variable read.

Path 2 is the only one that passes `also_allowed=record.get("allowed_domains") or []`.

### The `restrict` bulk action

```python
def _plan_restrict(ctx, slug, record):
    record["allowed_domains"] = ctx.new_allowed_domains
    record["updated_at"] = ctx.now
    return PlannedMutation(slug, "set", record)


def _domain_fields(ctx):
    return {"allowed_domains": ctx.new_allowed_domains}
```

```python
    "restrict": ActionSpec("restrict", _plan_restrict, result_fields=_domain_fields),
```

`ActionContext` gains `new_allowed_domains: list[str] | None = None`. Validation
happens in `handle_bulk_action`'s per-action block beside `repoint`'s and
`schedule`'s, before the `get_many`, and a validation failure blocks the
**entire** batch — the all-or-nothing convention every other bulk error path
holds.

Three deliberate calls on this action:

- **`per_row_can_edit` stays `True`** (the default). Only `reassign` skips it,
  for the departed-employee case; there is no analogous case here.
- **`required_permission` is `None`.** No new permission. Restricting a link is
  strictly *less* severe than `disable`, which takes 50 links off **every**
  domain at once and is gated on `can_edit` alone. Adding `links.restrict_domains`
  would give the least dangerous of the two the stronger gate. See Trade-offs #5.
- **`[]` clears rather than a separate `unrestrict` action**, matching
  `schedule`'s "an explicit `null` clears that side" shape rather than adding a
  seventh verb.

### `GET /api/links/{slug}/qr` — refuse a base the link does not resolve on

The record is already fetched for `can_view`, so this costs zero KV operations.
After the existing `resolve_base_url` block:

```python
    if not domains.base_url_allowed_for_link(base_url, record.get("allowed_domains")):
        return json_response(400, {
            "error": "base_not_allowed_for_link",
            "base": base_url,
            "allowed_domains": record.get("allowed_domains") or [],
        })
```

`domains.base_url_allowed_for_link(base_url, allowed)` is the Python twin of
`linkgate.HostAllowed` — empty/None `allowed` → `True`, otherwise hostname
membership. **The two are deliberately NOT pinned against each other** (unlike
`keys.go`'s prefixes and `CountShards`, which fail silently at runtime if they
drift). This one fails loudly and in the safe direction: the worst drift
produces a QR the API refuses to draw for a link that would have resolved, which
an operator sees immediately. Pinning would mean a Python test parsing Go source
for a property that is not silent.

**Why this is worth an error path at all:** a QR code is printed, handed out and
scanned long after the request that produced it. Encoding a domain the link
404s on is the same unrecallable-artifact harm that made `?base=` allowlist
validation non-negotiable in the first place. The GUI handles it explicitly —
see "GUI changes".

### `GET /api/auth/me` — carry the configured list

One new field beside the existing `domains` / `assigned_domains` /
`include_redirect_prefix`:

```python
    "all_domains": configured_domains,
```

**Why the full configured list and not the viewer-filtered `domains`:** the
checkbox payloads in the GUI are *full replacements*. Built from a filtered
list, a save by a user assigned one domain would silently strip every other
domain from a link's restriction — a link would start resolving somewhere
nobody asked for it to. The restriction is a property of the **link**, not of
the viewer, and `assigned_domains` is documented as having no server-side force,
so filtering it here would buy nothing and cost a real correctness hole. The
field is not sensitive: it is deployment configuration the viewer's own
selector already partially exposes, and `GET /api/users` already returns the
same list as `all_domains` under a permission this deliberately does not
require.

## GUI changes

Every new control uses classes, the native `hidden` attribute and
`addEventListener` — `gui-pages/tests/test_no_inline_code.py` matches inside
comments too. No new `spin.toml` route: every file touched is already routed.

### `gui/app.js`

- `let allConfiguredDomains = []` beside `availableDomains`, set in
  `initHeader()` inside the `if (result.ok)` block from
  `result.data.all_domains || []`.
- `function hostOf(baseUrl)` — `new URL(baseUrl).host` in a `try`/`catch`
  falling back to the raw string, the same helper shape `renderDomainSelector`
  already uses for its option text. Exported for the dashboard's badges.

### `gui/dashboard.html` + `gui/dashboard.js`

1. **Create form**, inside the existing `<details id="advanced-options">`, after
   the tags input:
   ```html
   <fieldset id="allowed-domains-fieldset" hidden>
     <legend>Domains this link works on (none checked = all domains)</legend>
   </fieldset>
   ```
   Filled from JS with one `<label><input type="checkbox" class="new-allowed-domain" value="<full base url>"> host</label>`
   per entry in `allConfiguredDomains`; rendered `hidden` when fewer than 2 are
   configured. The legend **states the rule the code implements**, because
   "none checked means all" is not guessable — the same wording decision
   `assigned_domains`' fieldset already made.

2. **Edit row** (`editRowHtml`), a matching fieldset of `.edit-allowed-domain`
   checkboxes:
   - checked from `(link.allowed_domains ?? []).includes(domain)`;
   - a stored entry **not** in `allConfiguredDomains` renders as an ordinary,
     **enabled**, checked checkbox with its label suffixed
     ` — no longer configured`;
   - the payload selector is a plain `:checked`, **not**
     `:checked:not(:disabled)`.

   That is the deliberate opposite of `admin/users.js`' treatment of the same
   situation, and the difference must be commented at the call site. Dropping a
   stale entry from a *user's* assignment merely offers them one more domain in
   a dropdown; dropping one from a *link's* restriction makes the link start
   resolving on a hostname the operator never re-authorized. The server's
   `also_allowed` rule is what makes keeping it possible.

   `allowed_domains` is only sent when the committed checkbox set differs from a
   `data-original-domains` snapshot — the same guard the tag chip input already
   uses, for the same reason: `PATCH` is a full replacement, so an unrelated
   save must not rewrite the field.

3. **Table badge**, in the Short-link cell after the Custom / Password badges
   and before the tag chips:
   ```js
   ${domainBadgeHtml(link)}
   ```
   `domainBadgeHtml` returns `""` for an unrestricted link; otherwise a single
   `<span class="domain-badge" title="<full base URLs, space-joined>">` whose
   text is the hosts joined with `", "` when there are 1–2 of them and
   `"${n} domains"` at 3 or more. One badge, never N — the cell already carries
   up to two badges plus ten tag chips, and DESIGN.md's Chips entry is explicit
   that this column is the app's tightest.

4. **Bulk bar**, a new `<span id="bulk-domain-controls" hidden>` following
   `#bulk-schedule-controls` and mirroring its set/clear pair:
   one checkbox per configured domain, a `Restrict` button and an
   `Allow all domains` button, both `POST /links/bulk-action` with
   `action: "restrict"` and the second sending `allowed_domains: []`.
   Shown only when `allConfiguredDomains.length >= 2`. `Allow all domains`
   goes through the existing count-bearing `confirmDialog`, because it widens
   where N links resolve and nothing else in the bar does.

   `#bulk-bar button { flex-shrink: 0 }` (landed 2026-09-03, commit `951e9bb`)
   already covers the new buttons by construction; the bar's own `flex-wrap`
   handles the extra cluster. Re-measure at 1400 / 1280 / 768 / 480 / 390px
   anyway — that fix exists precisely because this bar was already at its limit.

5. **CSV**, one new column appended to `CSV_COLUMNS`:
   ```js
   ["Domains", (l) => (l.allowed_domains ?? []).join(" ")],
   ```
   An export that does not carry the restriction would claim a link works
   everywhere — the same "a file that outlives the session must not lie"
   argument that gave the export both a `State` and a `Status` column.
   The existing CSV escaping (including the 2026-08-31 formula-injection fix)
   applies unchanged; nothing new is needed.

### `gui/links/detail.html` + `gui/links/detail.js`

- A read-only line under the heading: `Works on: trrk.io` for a restricted link,
  the element `hidden` for an unrestricted one.
- When the currently selected domain is not in the link's `allowed_domains`,
  the QR block is replaced by a message naming the mismatch — because the
  `<img>` will otherwise just fail silently against the new
  `400 base_not_allowed_for_link`. Copy: *"This link does not work on
  `<host>`. Switch domains in the header to see its QR code."*

The dashboard deliberately gets **no** equivalent per-row live warning when the
selected domain is not allowed for a row's link — the badge already states the
restriction, and a second, selector-dependent signal on every row was judged
noise. Filed under Future work.

## Testing and mutation verification

This repo mutation-verifies almost everything security- or correctness-adjacent,
and this feature is squarely in that class. The builder **runs each mutation,
confirms exactly the named tests fail, reverts, and reports the result** — the
same protocol `docs/plans/redirect-read-failure-not-404.md` and
`docs/plans/batch-kv-reads.md` used.

### Go — `redirect/linkgate/domaingate_test.go`, `resolve_test.go`, `link_test.go`

New pins:

- `TestResolve_AbsentDisabledOutOfWindowAndWrongDomainAreAllEqual` — **replaces**
  `TestResolve_AbsentDisabledAndOutOfWindowAreEqualToEachOther`, keeping its
  comment and adding a fourth case. The four dispositions must be **equal to
  each other**, not merely each equal to `DispositionNotFound`.
- `TestResolve_PasswordProtectedLinkOnWrongDomainIsNotFound` — must be
  `DispositionNotFound`, **never** `DispositionPrompt`. This is the pin on the
  ordering decision; a prompt would disclose both existence and protection on a
  domain the link does not serve.
- `TestResolve_UnrestrictedLinkResolvesWithAnyHostIncludingEmpty` — the
  no-regression pin for every link that exists today.
- `TestResolve_RestrictedLinkWithEmptyHostIsNotFound` — the fail-closed pin.
- `TestHostAllowed_MatchesOnHostnameIgnoringSchemeAndPort`,
  `..._IgnoresUserinfo`, `..._IsCaseInsensitive`, `..._StripsTrailingDot`,
  `..._HandlesBracketedIPv6`, `..._RejectsASuffixThatIsNotAWholeLabel`
  (`nottrrk.io` must not match `trrk.io` — the same trap
  `urlpolicy.host_matches`' load-bearing dot already guards).
- `TestParseLink_AllowedDomainsAbsentOrNullIsNil` and
  `TestParseLink_MalformedAllowedDomainsIsAParseError`.
- `TestSanitizeHostForLog_*` — a host with a space and one with a newline both
  become `[invalid_host]`, mirroring `TestSanitizeSlugForLog_*`.
- `TestHostUnresolvedLine_*` — field order, `msg` last, dedup key disjoint from
  the other two key spaces.

Mutations to run and report:

| # | mutation | expected sole failures |
|---|---|---|
| M1 | `HostAllowed` returns `true` unconditionally | the wrong-domain, password-on-wrong-domain and four-way-equality tests |
| M2 | `HostAllowed` returns `true` when the host is empty (fail open) | `TestResolve_RestrictedLinkWithEmptyHostIsNotFound` only |
| M3 | move the host check **after** the password check in `Resolve` | `TestResolve_PasswordProtectedLinkOnWrongDomainIsNotFound` only |
| M4 | `HostFromBaseURL` drops the whole-label check (bare `strings.HasSuffix`) | `TestHostAllowed_RejectsASuffixThatIsNotAWholeLabel` only |

### Python

- `api/tests/test_allowed_domains_enforcement.py` (new) — the four-path pin,
  modelled on `test_url_policy_enforcement.py`: each of the four paths rejects
  `https://not-configured.example` with `invalid_allowed_domains` and **writes
  nothing** (asserted against `FakeStore`, not just the status code).
- `api/tests/test_domains.py` — `normalize_allowed_domains` across: `None`/`[]`,
  a non-list, a non-string member, an unconfigured member, a
  differently-cased/trailing-slashed member canonicalizing, configured-order
  output, de-duplication, and the `also_allowed` retention rule in both
  directions (retained when resubmitted, gone when omitted).
- `api/tests/test_links.py` — `PATCH` key-presence semantics (absent leaves
  untouched, `[]` clears, `null` clears); `public_link` always emits a list.
- `api/tests/test_bulk.py` — the `restrict` action end to end, `[]` clearing,
  all-or-nothing on one bad domain, and **`"restrict" in BULK_ACTIONS` derived
  rather than declared**.
- **A no-silent-drop pin**: `handle_update` (changing only `target_url`),
  `handle_set_password`, and every one of the other seven bulk actions leave a
  record's `allowed_domains` byte-identical. This is the "settable at creation
  but droppable via another path" failure the four-path rule exists to prevent,
  turned into a test.
- `api/tests/test_backup.py` — a restricted record round-trips byte-identically
  through export → restore. Cheap, and it turns "backup.py needs no change" from
  an assertion into something checkable.
- `api/tests/test_consistency.py` — a record with a malformed `allowed_domains`
  produces **no** finding, pinning that `_parse_link_record`'s owner-only read
  is deliberate and not an oversight to be "fixed" later.
- `api/tests/test_qr.py` — `base_not_allowed_for_link` returns 400 with
  `qrcode.make` never called; an allowed base still renders; an unrestricted
  link is unaffected.

Python mutation: **remove the `allowed_domains` validation from
`bulk.handle_bulk_create`** → exactly one of the four enforcement cases fails.

## Consistency check, backup and the three obligations

**No new consistency check, and no change to `consistency.py` at all.** Reasoned
the same way `destination-policy violations` and `orphaned analytics` were
reasoned, and reaching the same answer for the same reasons:

- **There is no structural drift to detect.** `consistency.py` is scoped to
  structural drift — an index and a record disagreeing, a session naming a
  missing user. An `allowed_domains` entry that is no longer in
  `public_base_urls` is a **configuration** fact about a store that is
  structurally flawless, exactly the shape of a policy violation.
- **Nothing is broken by it.** `redirect` never reads `public_base_urls`, so a
  stale entry keeps resolving on that hostname for as long as DNS points there.
  There is no failure to report and nothing a repair could derive.
- **It would pin `ok: false` on a healthy deployment.** An operator who retires
  a domain would see findings on every run forever, which is how a checker gets
  ignored.
- **A malformed value cannot produce `unreadable_value`.**
  `_parse_link_record` reads only `owner` — pinned by test above, so the
  omission is recorded as deliberate rather than discovered later and "fixed".

`backup.py` needs **no change**: values are base64'd verbatim, `INDEX_KEYS` and
`restore_write_order` key on key *names*, and no key name changes. Likewise
`kvprefix.STORE_PREFIXES`. The general rule CLAUDE.md states — *"a new KV key
type now obliges three changes"* — does not fire, because this is a new **field
on an existing key type**. Worth saying explicitly in the CLAUDE.md update so
the next reader does not have to re-derive it.

## Deployment and property-side requirements

A **third** property-side requirement joins the two
`docs/plans/toggleable-redirect-prefix.md` recorded, and it is the one this
feature lives or dies on:

> **The edge property must forward the incoming `Host` header unchanged.** In
> Akamai Property Manager that is the "Forward Host Header" behaviour set to
> *Incoming Host Header*, not *Origin Hostname*. Set to the origin hostname,
> every request reaches this app as `<app-id>.fwf.app` and **every
> domain-restricted link 404s everywhere** while unrestricted links keep
> working — a failure that looks exactly like "that campaign link is dead".
> The diagnostic is one traced request: `X-SS-Debug: <token>` against
> `/r/{slug}` and read the `host=` field off the `ss ` log line.

No new deploy variable. `redirect` gains no Spin variable at all.

## Trade-offs and rejected alternatives

### 1. Doing nothing — rejected, and it was live

The status quo is honest: no field, no enforcement, every link everywhere, and
`multi-domain-display.md` argued persuasively that an unenforced field is worse
than none. What tips it is that the requirement is now concrete rather than
hypothetical — a branded domain is configured and deliberately not yet in DNS
so it can be tested ahead of launch, and there is currently **no way at all** to
say "this link is for the new domain" or "this link must not follow us there".
Doing nothing also leaves the previous rejection standing as the repo's stated
position, so the next person to want this re-derives the whole argument.

### 2. Enforcing in `redirect` — the objection this reopens, answered

The 2026-08-02 rejection gave three reasons. Taking them in order:

**"It puts a header parse plus a second KV-field comparison on the one hot path
this repo keeps deliberately minimal."** Costed above at operation granularity:
**zero additional KV operations**, one map lookup, and for an unrestricted link
literally one `len()` check before returning. The "header parse" turns out not
to exist — the SDK has already parsed and copied the headers into a map before
the handler runs. Against KV at ~97% of handler time on Akamai, this is not
measurable. The component gains no variable, no dependency and no outbound
access; `[component.redirect.variables]` is untouched.

**"It creates a sharp new failure mode where a link works in testing and 404s in
production because a CDN, proxy or DNS change rewrote `Host`."** This is
correct, it is the real risk, and it is not argued away. It is answered
structurally, in four parts: (a) the `host=` field lands **first**, as its own
task, and must be verified on the deployed app before any enforcement task
begins — so the question "does the origin see the right host?" is answered by
measurement, not hope; (b) that field is permanent, so the same question stays
answerable on any future deploy with one traced request; (c) `ev=host_unresolved`
fires unconditionally, once per Wasm instance, if the runtime supplies no host
at all; (d) forwarding the incoming `Host` becomes a documented property-side
requirement with its symptom and its diagnostic spelled out. The residual risk
is real and is **accepted**: a property misconfigured *after* the fact will
break restricted links, exactly as a DNS change would. Unrestricted links —
every link that exists today — are unaffected under every one of those failures.

**"It protects nothing, since slugs are already documented as enumerable and
non-secret."** True, and this feature does not claim otherwise. Restriction is
not a confidentiality control and must never be described as one; anyone who
knows a slug still learns the link exists, from a 302 on an allowed domain.
What it controls is **where a link resolves**, which is an availability and
brand-routing property — the same category as `status` (disable a link) and the
`[start_at, end_at)` window (when a link resolves), neither of which buys a
confidentiality property either and neither of which is therefore pointless.
The motivating case makes this concrete: a domain that is deliberately not yet
in DNS needs a way to be exercised without every existing link silently
becoming reachable through it the moment it is.

### 3. Storing bare hostnames instead of full base URLs — rejected, closely

**Attractive because** it is what the redirect actually compares, so the hot
path would need no scheme-stripping at all, and it would make the
scheme-and-port-are-ignored rule self-evident from the stored value.

**Lost because** it forks the vocabulary. `public_base_urls`, `assigned_domains`
and the QR endpoint's `?base=` all speak full base URLs; a fourth surface
speaking bare hostnames means the GUI, the API and the operator reading a raw
record all have to convert between two representations, and the validator can no
longer be a plain membership test against the configured list. The saving it
buys is a handful of byte comparisons per *restricted* request. The
scheme-and-port rule is documented loudly instead, in both CLAUDE.md and
`HostFromBaseURL`'s own doc comment.

**Revisit if** a deployment ever needs two configured entries that share a
hostname and differ only by scheme or port, which is the one configuration this
choice cannot represent.

### 4. Making `redirect` read `public_base_urls` and validate against it — rejected

**Attractive because** it would let the redirect reject a record naming a domain
that is no longer configured, catching drift at resolution time.

**Lost because** it inverts the failure direction, badly. Under it, removing a
domain from `public_base_urls` — a display-configuration edit — would
retroactively 404 every link restricted to it, at the edge, with no warning and
no preview. That is precisely the "a config edit must never become an
unpreviewable bulk mutation" posture that the destination URL policy, restore
and the consistency check all already hold. It would also add a Spin variable
read to the hot path and give `redirect` a second thing that can be
misconfigured. Matching is record-vs-request-host only, and the configured list
is an **authoring-time** gate — exactly like `urlpolicy`.

### 5. A new `links.restrict_domains` permission — rejected

**Attractive because** `links.tag` exists for precisely the analogous case
(bulk tag/untag), so symmetry argues for one, and a bulk restriction can take 50
links off a domain in one request.

**Lost because** it would give the *less* dangerous action the *stronger* gate.
`disable` takes 50 links off every domain at once and is gated on per-row
`can_edit` alone; `repoint` changes where 50 links send people and is likewise.
Restricting is strictly weaker than both. `KNOWN_PERMISSIONS` is also a
deliberately small fixed vocabulary, and each addition needs a reason stronger
than symmetry.

**Revisit if** restriction ever becomes the primary mechanism for taking a
domain out of service, at which point it stops being weaker than `disable`.

### 6. A distinguishable response for a domain mismatch — rejected

**Attractive because** "this link doesn't work on this domain" is genuinely more
helpful than a bare 404, and the visitor is usually an ordinary person who
mistyped or followed an old URL.

**Lost because** it would tell an unauthenticated prober that a slug exists,
that it is restricted, and — with the domain named — where to find it. The
existing 404 family (absent / disabled / out-of-window) is deliberately
byte-identical and is pinned as *equal* rather than merely as three values that
each map to 404. A domain mismatch is the same kind of fact about the same kind
of link and joins that family. This is the "explicit decision that a mismatched
`Host` returns 404 to match the time-window behaviour" the original rejection
asked a future plan to make.

### 7. Reusing `assigned_domains` for both meanings — rejected outright

**Attractive because** it is one field, one validator and one GUI control.

**Lost because** the two mean opposite things about opposite objects. One
restricts a *user's* choices and is explicitly documented as gating nothing
server-side; the other restricts a *link's* resolution and is enforced on the
hot path. Conflating them would make a user's convenience guardrail into a
security control by accident, and would make it impossible to have a user who
may hand out `trrk.io` URLs but whose links are unrestricted — the common case.

### 8. Making the GUI's checkbox lists come from the viewer's `domains` rather
### than `all_domains` — rejected

**Attractive because** it is consistent with the nav selector, keeps
`assigned_domains` meaningful in one more place, and needs no new `/auth/me`
field.

**Lost because** the payload is a full replacement. A user assigned one domain
editing a link restricted to two would submit only the one they can see, and the
save would silently drop the other — widening where the link resolves, with no
error and nothing in the UI to notice. That is a real correctness hole bought to
extend a guardrail that is documented as having no server-side force anyway.

### 9. `assigned_domains`' checked-and-disabled treatment for stale entries — rejected here

**Attractive because** it is a shipped, tested, understood pattern in this exact
app, solving what looks like the identical problem, and it self-heals on the
next save.

**Lost because** "self-heals on the next save" means the opposite thing for a
link. For a user it drops an assignment they could not use anyway; for a link it
**removes a restriction**, so an unrelated edit — changing a destination —
would silently make the link start resolving on a hostname the operator had
excluded. The replacement is the server-side `also_allowed` rule plus an
enabled, checked checkbox: a stale entry is kept by default, can be removed
deliberately, and never blocks a save.

### 10. A `restrict` GUI control on the dashboard rows themselves (not just the edit form) — rejected

**Attractive because** it would put the control next to the badge that shows it,
the way the `.status-btn` row toggle sits next to the status badge.

**Lost because** unlike status, this is not a binary — it is a subset of a list,
so a row control would need the whole checkbox set inline in an already
overcrowded actions cell. The edit form and the bulk bar cover the one-link and
many-link cases respectively, which is the same split tags and schedule already
use.

### 11. A domain-restriction violations report, like the URL policy's — rejected

**Attractive because** `GET /api/admin/url-policy/violations` is the established
answer to "which existing records disagree with current configuration", and
retiring a domain leaves exactly that kind of record behind.

**Lost because** there is no violation. A retired domain's restriction still
works — `redirect` never consults `public_base_urls`, so nothing is broken and
nothing needs remediation. The dashboard badge and the edit form's
"— no longer configured" label already surface it where an operator is already
looking. Filed under Future work in case a deployment ever retires a domain and
wants a list.

## Tasks

The lines below were appended verbatim to `TASKS.md` under
`## Per-link domain restriction`. `TASKS.md` is authoritative; do not track
checkbox state here.

- [ ] Log the request host on redirect's summary line (MUST land and be verified on the deployed app before any enforcement task in this section) — file(s): redirect/linkgate/obs.go, redirect/linkgate/obs_test.go, redirect/main.go — done when: `linkgate.SanitizeHostForLog(raw)` returns `raw` for `^[A-Za-z0-9._:\[\]-]{1,253}$`, `[invalid_host]` otherwise and `-` for empty, with tests pinning that a host containing a space and one containing a newline both become `[invalid_host]`; `main.go` gains `rawRequestHost(r)` (checking `r.Host` first, then `r.Header.Get("Host")`) and `emitLogLine` emits a `host=` field between `slug` and `status`; `cd redirect && go test ./linkgate/...` passes; and with `SPIN_VARIABLE_LOG_LEVEL=summary SPIN_VARIABLE_PUBLIC_BASE_URLS="http://localhost:3000,http://127.0.0.1:3000" spin up --build --runtime-config-file runtime-config.toml`, `curl -s localhost:3000/r/<slug>` and `curl -s 127.0.0.1:3000/r/<slug>` produce two `ss ` lines reading `host=localhost:3000` and `host=127.0.0.1:3000` respectively — **and the same field is read off the DEPLOYED Akamai app via one `X-SS-Debug` traced request and its value reported**, since every later task in this section is conditional on that value being the real request host and not the origin hostname
- [ ] Add normalize_allowed_domains and base_url_allowed_for_link to api/domains.py — file(s): api/domains.py, api/tests/test_domains.py — done when: `normalize_allowed_domains(value, configured, also_allowed=None) -> tuple[list[str] | None, str | None]` and `base_url_allowed_for_link(base_url, allowed) -> bool` exist with the docstrings in docs/plans/per-link-domain-restriction.md, the module still has zero `spin_sdk` imports and takes no `store`, and `cd api && uv run pytest tests/test_domains.py` passes with new tests covering `None` and `[]` both yielding `[]`; a non-list, a non-string member, an unnormalizable member and an unconfigured member each yielding `(None, "invalid_allowed_domains")`; a trailing-slashed and an uppercase form of a configured domain being accepted and stored in the server's canonical form; output in configured order and de-duplicated; `also_allowed` retaining a stored-but-unconfigured entry when resubmitted and dropping it when omitted; and `base_url_allowed_for_link` returning True for `None`/`[]`, matching on hostname while ignoring scheme and port, and refusing `nottrrk.io` against `trrk.io`
- [ ] Add allowed_domains to the single-link create and update paths — file(s): api/links.py, api/app.py, api/tests/test_links.py, api/tests/test_backup.py, api/tests/test_consistency.py — done when: `handle_create` and `handle_update` take `configured_domains` as a required positional with no default (before `write`), `UPDATABLE_FIELDS` includes `"allowed_domains"`, `handle_update` passes `also_allowed=record.get("allowed_domains") or []`, `public_link` emits `allowed_domains` as a list always, `app.py` passes the already-computed `configured_domains` at both call sites, and `cd api && uv run pytest` passes with new tests that: an absent key on PATCH leaves the stored value untouched while `[]` and `null` both clear it; an unconfigured domain is rejected on both paths with `invalid_allowed_domains` and **nothing written to FakeStore**; `handle_set_password` and a `target_url`-only PATCH both leave `allowed_domains` byte-identical; a restricted record round-trips byte-identically through backup export and restore with **no change to backup.py**; and a record whose `allowed_domains` is malformed produces **no** consistency finding, with **no change to consistency.py**
- [ ] Add allowed_domains to bulk create and a new restrict bulk action — file(s): api/bulk.py, api/app.py, api/tests/test_bulk.py, api/tests/test_allowed_domains_enforcement.py (new) — done when: `handle_bulk_create` and `handle_bulk_action` take `configured_domains` as a required positional with no default; bulk create accepts a batch-level `allowed_domains` applied to every row; `ACTION_SPECS` gains `"restrict": ActionSpec("restrict", _plan_restrict, result_fields=_domain_fields)` with `per_row_can_edit=True` and no `required_permission`, `ActionContext` gains `new_allowed_domains`, and `BULK_ACTIONS` still derives from `ACTION_SPECS` rather than listing the name; one bad domain blocks the entire batch with nothing written; `allowed_domains: []` clears the restriction; and `cd api && uv run pytest` passes including a new `test_allowed_domains_enforcement.py` proving **all four** authoring paths reject `https://not-configured.example` with `invalid_allowed_domains` and write nothing, plus a test that the other seven bulk actions leave `allowed_domains` byte-identical — mutation-verified by removing the validation from `handle_bulk_create` and confirming exactly one enforcement case fails
- [ ] Enforce allowed_domains in redirect's Resolve (depends on the host-logging task being verified on the deployed app) — file(s): redirect/linkgate/domaingate.go (new), redirect/linkgate/domaingate_test.go (new), redirect/linkgate/link.go, redirect/linkgate/link_test.go, redirect/linkgate/resolve.go, redirect/linkgate/resolve_test.go, redirect/main.go — done when: `NormalizeHost`, `HostFromBaseURL` and `HostAllowed` exist with the semantics in the plan and **no `regexp` and no allocation on the unrestricted path**, `Link` gains `AllowedDomains []string` tagged `json:"allowed_domains"`, `Resolve` takes a fourth `rawHost string` parameter and checks the host **after** the window check and **before** the password check with its doc comment's numbered list and disposition table updated, both handlers in `main.go` pass `rawRequestHost(r)`, and `cd redirect && go test ./linkgate/...` passes with `TestResolve_AbsentDisabledOutOfWindowAndWrongDomainAreAllEqual` (four cases, equal to **each other**), a password-protected link on a wrong domain returning NotFound and never Prompt, an unrestricted link resolving under any host including empty, a restricted link with an empty host returning NotFound, and `nottrrk.io` not matching `trrk.io` — and mutations M1–M4 from the plan's table are each run, each fails exactly the named tests, is reverted, and the result is reported
- [ ] Emit ev=host_unresolved when a redirect request carries no host (depends on the enforcement task) — file(s): redirect/linkgate/obs.go, redirect/linkgate/obs_test.go, redirect/main.go — done when: `linkgate.HostUnresolvedLine() (line, dedupKey string)` renders `ss comp=redirect ev=host_unresolved route=/r/{slug} msg=...` with **no slug, no op, no ns and no etype**, `msg` last, and a fixed `"host_unresolved"` dedup key proven disjoint from `KVFailureDedupKey`'s and `RecordUnreadableDedupKey`'s key spaces; `main.go`'s `emitHostUnresolvedLine` routes it through the existing `shouldEmitFailureLine` so at most one such line is emitted per Wasm instance; the line is unconditional (fires with `log_level=off` and no `X-SS-Debug`); and `cd redirect && go test ./linkgate/...` passes
- [ ] Refuse a QR code for a base URL the link does not resolve on — file(s): api/qr.py, api/tests/test_qr.py — done when: after `resolve_base_url` succeeds, `handle_qr` returns `400 {"error": "base_not_allowed_for_link", "base": ..., "allowed_domains": [...]}` when `domains.base_url_allowed_for_link` says no, with **zero additional KV operations** (the record is already fetched for `can_view`); all pre-existing `test_qr.py` tests pass unmodified; and new tests cover an unrestricted link being unaffected, an allowed base still rendering, and a disallowed base returning 400 with `qrcode.make` never called
- [ ] Add the allowed-domains controls to the dashboard create form, edit row, badge and CSV (depends on the single-link API task) — file(s): gui/app.js, gui/dashboard.html, gui/dashboard.js, gui/theme.css — done when: `app.js` stores `allConfiguredDomains` from `/auth/me`'s new `all_domains` field and exposes `hostOf(baseUrl)`; the create form's `<details id="advanced-options">` gains `#allowed-domains-fieldset` (hidden below 2 configured domains) with the legend `Domains this link works on (none checked = all domains)`; `editRowHtml` renders a matching fieldset checked from the record, sends `allowed_domains` only when the set differs from a `data-original-domains` snapshot, and renders a stored-but-unconfigured entry as an **enabled, checked** checkbox suffixed ` — no longer configured` collected with a plain `:checked` selector (deliberately **not** `admin/users.js`' `:checked:not(:disabled)`, with a comment saying why); a `.domain-badge` joins `theme.css`'s existing `.slug-kind-badge, .lock-badge, .tag-chip` rule with **no new token** and renders one badge per restricted link (hosts joined for 1–2, `N domains` for 3+, full base URLs in `title`); `CSV_COLUMNS` gains `["Domains", ...]`; and `cd gui-pages && uv run pytest` still passes at 170
- [ ] Add the bulk restrict controls to the bulk bar (depends on the bulk API and dashboard tasks) — file(s): gui/dashboard.html, gui/dashboard.js, gui/dashboard.css — done when: `#bulk-domain-controls` follows `#bulk-schedule-controls` with one checkbox per configured domain plus `Restrict` and `Allow all domains` buttons, both posting `action: "restrict"` and the second sending `allowed_domains: []` behind the existing count-bearing `confirmDialog`; the cluster is hidden below 2 configured domains and disabled past `MAX_BULK_ROWS` like every other bulk button; and `#app-header nav`-style overflow is re-measured with `scrollWidth` vs `clientWidth` on `#bulk-bar` at 1400 / 1280 / 768 / 480 / 390px in both themes with **no horizontal overflow and no mid-word label breaking** at any of them, the numbers recorded in the task note
- [ ] Show the restriction and handle a disallowed QR on the link detail page (depends on the QR task) — file(s): gui/links/detail.html, gui/links/detail.js — done when: a `Works on: <hosts>` line renders for a restricted link and is `hidden` for an unrestricted one; when the selected domain is not in the link's `allowed_domains` the QR block is replaced by `This link does not work on <host>. Switch domains in the header to see its QR code.` instead of a silently-broken `<img>`; switching the nav domain selector updates both without a reload; and the browser console shows zero errors and zero CSP violations
- [ ] Document per-link domain restriction in CLAUDE.md, PRODUCT.md and DESIGN.md (depends on every task above) — file(s): CLAUDE.md, PRODUCT.md, DESIGN.md, .impeccable/design.json — done when: CLAUDE.md gains a "Per-link domain restriction" section after "Toggleable /r/ prefix" that leads with the `allowed_domains`-vs-`assigned_domains` distinction table, states that absent/null/`[]` means unrestricted with no migration, records that matching is **hostname-only** (scheme and port in a stored entry are ignored) and why, records that `redirect` never reads `public_base_urls` so a config edit can never retroactively 404 a link, names all four enforcement paths, states that a new field on an existing key type triggers **none** of the three new-key-type obligations, and records the punycode/IDN limitation; the `/r/{slug}` status-contract table gains a domain-mismatch 404 row and the indistinguishability note gains a fourth member; the "Security tradeoffs" enumerability bullet is updated; "Observable KV failures" gains `ev=host_unresolved` as a fifth `ev` kind with its no-slug/no-op/no-ns/no-etype shape; the summary-line example gains `host=`; the "Toggleable /r/ prefix" section's property-side requirements gain the **Forward Host Header = Incoming Host Header** requirement with its symptom and its one-traced-request diagnostic; PRODUCT.md's Capabilities gains one accurate line; DESIGN.md gains a `.domain-badge` entry noting it reuses the existing badge rule with no new token, with a matching `.impeccable/design.json` entry in the existing entries' shape; and no doc claims a capability the shipped code does not have
- [ ] End-to-end manual verification of per-link domain restriction — file(s): (none — verification step) — done when: with `SPIN_VARIABLE_PUBLIC_BASE_URLS="http://localhost:3000,http://127.0.0.1:3000" SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false spin up --build --runtime-config-file runtime-config.toml` running, every numbered step in the plan's Verification section is executed in a real browser with the console open and zero errors of any kind (in particular zero CSP violations) in both light and dark themes, `curl -sI localhost:3000/r/<restricted-slug>` returns 404 while `curl -sI 127.0.0.1:3000/r/<restricted-slug>` returns 302 for the same slug, an unrestricted slug returns 302 on both, and `cd api && uv run pytest`, `cd gui-pages && uv run pytest` and `cd redirect && go test ./linkgate/...` all pass

## Critical files

- `docs/plans/per-link-domain-restriction.md` (new) — this plan
- `redirect/linkgate/domaingate.go` (new)
- `redirect/linkgate/domaingate_test.go` (new)
- `redirect/linkgate/link.go`
- `redirect/linkgate/link_test.go`
- `redirect/linkgate/resolve.go`
- `redirect/linkgate/resolve_test.go`
- `redirect/linkgate/obs.go`
- `redirect/linkgate/obs_test.go`
- `redirect/main.go`
- `api/domains.py`
- `api/links.py`
- `api/bulk.py`
- `api/qr.py`
- `api/app.py`
- `api/tests/test_domains.py`
- `api/tests/test_links.py`
- `api/tests/test_bulk.py`
- `api/tests/test_qr.py`
- `api/tests/test_backup.py`
- `api/tests/test_consistency.py`
- `api/tests/test_allowed_domains_enforcement.py` (new)
- `gui/app.js`
- `gui/dashboard.html`
- `gui/dashboard.js`
- `gui/dashboard.css`
- `gui/theme.css`
- `gui/links/detail.html`
- `gui/links/detail.js`
- `CLAUDE.md`
- `PRODUCT.md`
- `DESIGN.md`
- `.impeccable/design.json`
- `TASKS.md`

Deliberately **not** in the list, and a builder that touches one has deviated:
`spin.toml` (no new variable, no route change, `[component.redirect.variables]`
unchanged), `api/backup.py`, `api/consistency.py`, `api/consistencyrepair.py`,
`api/kvprefix.py`, `api/users.py`, `api/auth.py` (`KNOWN_PERMISSIONS` is
untouched), `redirect/linkgate/keys.go`, `gui-pages/`, `runtime-config.toml`,
`Jenkinsfile` (the three test commands are unchanged).

## Verification

Run in this order.

1. `cd redirect && go test ./linkgate/...` — expect `ok`. Never `go test ./...`,
   which fails by design on `package main`.
2. `cd api && uv run pytest` — expect **777** plus roughly 40 new tests. Report
   the actual number.
3. `cd gui-pages && uv run pytest` — expect **170, unchanged**. A drop means a
   page or script regrew inline code.
4. Run the four Go mutations (M1–M4) and the one Python mutation from
   "Testing and mutation verification", confirming each fails exactly the named
   tests, reverting each, and reporting all five results.
5. Start the app with two domains:
   ```bash
   SPIN_VARIABLE_PUBLIC_BASE_URLS="http://localhost:3000,http://127.0.0.1:3000" \
   SPIN_VARIABLE_LOG_LEVEL=summary \
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> \
   SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```
6. **The gating measurement.** `curl -s localhost:3000/r/anything` and
   `curl -s 127.0.0.1:3000/r/anything`; read the two `ss ` lines on stderr. They
   must read `host=localhost:3000` and `host=127.0.0.1:3000`. **If either reads
   `host=-`, stop — the whole feature is unreachable on this runtime and the
   plan needs revisiting, not the code.**
7. Log in at `http://localhost:3000/dashboard.html`. Create a link with **no**
   domains checked. `curl -sI localhost:3000/r/<slug>` → `302`;
   `curl -sI 127.0.0.1:3000/r/<slug>` → `302`. Unrestricted is unchanged.
8. Create a second link with only `127.0.0.1:3000` checked.
   `curl -sI localhost:3000/r/<slug>` → **`404`**, and its body must be
   byte-identical to `curl -s localhost:3000/r/definitely-no-such-slug`
   (`diff <(...) <(...)` → no output). `curl -sI 127.0.0.1:3000/r/<slug>` →
   `302` with the right `Location`.
9. **The disclosure check.** Give that restricted link a password. On
   `localhost:3000` it must still return the byte-identical `404`, **not** the
   password prompt. On `127.0.0.1:3000` it prompts as normal.
10. The dashboard row for the restricted link shows a `127.0.0.1` domain badge;
    its `title` carries the full base URL. Export CSV — the new `Domains` column
    carries `http://127.0.0.1:3000`.
11. Edit that link and change only its destination. Save. `curl` both domains
    again: the restriction is **unchanged**. (The no-silent-drop property, by
    hand.)
12. Select several links, use the bulk bar's `Restrict` with `127.0.0.1:3000`
    checked, then `Allow all domains` on the same selection. Each must round-trip
    the badges and re-verify by `curl` on both domains.
13. Open the restricted link's detail page with the nav selector on
    `localhost:3000`: the `Works on:` line reads `127.0.0.1:3000` and the QR
    block shows the switch-domains message rather than a broken image. Switch
    the selector to `127.0.0.1:3000`: the QR preview renders, and both Download
    SVG and Download PNG produce files that **scan to
    `http://127.0.0.1:3000/r/<slug>`** — scan them, do not just download them.
14. **The API security check**, with a session cookie:
    ```bash
    curl -si "localhost:3000/api/links/<restricted-slug>/qr?base=http://localhost:3000" | head -1
    ```
    → `HTTP/1.1 400`, body `base_not_allowed_for_link`. Then
    `?base=http://127.0.0.1:3000` → `200` and an image.
    Then `curl -X PATCH ... -d '{"allowed_domains":["https://not-configured.example"]}'`
    → `400 invalid_allowed_domains`, and a subsequent `GET` shows the stored
    value unchanged.
15. Restart with a **single** configured domain
    (`SPIN_VARIABLE_PUBLIC_BASE_URLS="http://localhost:3000"`). Every
    allowed-domains fieldset and the bulk cluster are hidden; the previously
    restricted link's edit row still shows `127.0.0.1:3000` as an enabled,
    checked checkbox suffixed ` — no longer configured`; saving the row with it
    still checked succeeds and leaves the restriction intact; unchecking it and
    saving clears the restriction. **Crucially: the link still 404s on
    `localhost` and 302s on `127.0.0.1` throughout** — removing a domain from
    configuration must not change resolution.
16. Backup and restore round trip: `GET /api/admin/backup`, then
    `POST /api/admin/restore` with `{"confirm": "REPLACE", ...}`; the restricted
    link's `allowed_domains` survives byte-identically and still enforces.
17. Responsive and theme pass, both themes, at 1400 / 1280 / 768 / 480 / 390px:
    record `scrollWidth` vs `clientWidth` on `#bulk-bar` at each, with no
    horizontal overflow and no mid-word label breaking anywhere.
18. `detect.mjs --json gui/` (the Impeccable mechanical detector, invoked the
    same way previous passes recorded in `TASKS.md` did) — expect the same known
    false positives and nothing new.

## Out of scope / follow-ups

- **Any per-domain behaviour other than resolution.** `include_redirect_prefix`
  stays one global setting, `gui-pages` still reads no host, and `robots.txt`
  stays identical on every domain. Unchanged non-goals.
- **A domain-restriction violations report**, listing links restricted to a
  domain that is no longer configured. Rejected above (there is no violation —
  such a link still resolves), but a *list* could be wanted by a deployment that
  actually retires a domain. Added under `## Future work (not scheduled)`;
  trigger is an operator asking "which links still point at the old brand?"
- **A live per-row warning on the dashboard** when the selected domain is not in
  a row's `allowed_domains` — the badge states the restriction, and a second,
  selector-dependent signal on every row was judged noise. Added under
  `## Future work (not scheduled)`; trigger is someone actually copying a URL
  that 404s.
- **IDN/punycode normalization** on either side. Documented as a limitation
  (both sides compare the literal configured form), inherited rather than
  introduced. Trigger: anyone configuring a non-ASCII domain.
- **Enforcing which domain a link may be *created* under**, i.e. tying
  `assigned_domains` to `allowed_domains` server-side. Deliberately not done —
  `assigned_domains` remains a display guardrail with no server-side force, and
  making it binding here would silently turn it into a security control.
- **Per-domain analytics** (which domain a click arrived on). The host is now
  read on the hot path, so it is newly *available* to `recordClickCount` — but
  it would mean a new key shape, a `CountShards`-style cross-language pin, and
  more analytics keys per link in a store already measured at 96% analytics
  keys. Added under `## Future work (not scheduled)`.
- **Restricting the `redirect` component by `Host` at the edge instead**, i.e.
  making the property refuse unlisted hostnames. That is a per-deployment
  control, not a per-link one, and cannot express "these 5 links, not those 50".
  Complementary, not a substitute.
