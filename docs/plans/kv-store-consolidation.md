# KV Store Consolidation Onto Spin's Single `"default"` Store

## Context

An Akamai Functions deploy is being prepared now — this is no longer the
preemptive refactor `CLAUDE.md`'s "Deployment: known Akamai Functions blocker"
section deferred. That section, and the matching `TASKS.md:303` Future-work
entry, record the confirmed finding: **Akamai Functions allows exactly one
key-value store label, `"default"`, which it auto-provisions, and it supports
no runtime configuration at all.** This app declares three named stores
(`links`, `users`, `analytics`) and maps them via `runtime-config.toml`, so it
is not deployable as-is. Nothing else in the manifest is known to block the
deploy; this is the one architectural change standing in the way.

The definition of done for this plan therefore reaches an actual deploy, not a
green test suite: the app running on Akamai Functions, resolving a real short
link, with the operator surfaces (auth, link CRUD, analytics, backup/restore,
consistency) exercised against the deployed URL.

Two decisions the user settled before planning, treated as fixed:

- **Confirmed decisions:**
  - **The KV explorer keeps full access to everything, including `users:`
    keys.** The exposure is accepted as local-dev-only. No config seam is to be
    invented to preserve store separation locally, and the explorer is not to
    be dropped. The plan owns the consequences instead: the test that asserts
    the withhold is rewritten (never deleted), and the two `CLAUDE.md`
    assertions that become false are rewritten to state the accepted exposure
    plainly.
  - **An Akamai deploy is in scope**, including the Akamai-side variable
    configuration and a live deploy smoke test. Confirming Akamai's KV rate
    limits with Akamai directly is an explicit task and an external dependency
    that can block the deploy.

The central design decision — a prefixing *view* over one physical store,
rather than flattening the prefixes into every key literal — is argued in full
under "Trade-offs and rejected alternatives". Everything below assumes the
view.

## Key technical facts confirmed during research

**Spin and the default store**

- **Spin auto-provisions a store named `"default"` with no runtime config.**
  Confirmed at <https://spinframework.dev/v3/kv-store-api-guide>: "Spin defines
  a key-value store named `"default"` and provides automatic backing storage,"
  and access still requires `key_value_stores = ["default"]` on each component
  ("by default, a given component of an app will not have access to any key
  value store"). Local Spin CLI is `spin 4.0.2 (bfc7543 2026-06-23)`.
- Local backing storage for the default store is "a file in the application
  `.spin` directory" (<https://spinframework.dev/v3/dynamic-configuration>).
  The exact filename with `type = "spin"` and no `path` is **UNCONFIRMED** and
  does not matter here: `.spin/` in this repo currently holds only `logs/`, and
  `CLAUDE.md` already records that local KV appears non-persistent across runs.
  This plan does not change that; to confirm, add an explicit
  `path = ".spin/kv.db"` and observe the file appear.

**Akamai Functions** (all from `techdocs.akamai.com/akamai-functions`, fetched
2026-08-04)

- `docs/use-the-key-value-store`: "Akamai Functions, only allows the
  `"default"` label"; "Akamai Functions provisions the key value store for
  you"; stores "are scoped to single applications and cannot be shared between
  applications" — so `redirect` and `api`, being components of one app, do
  share one store. Also: "the `wasi:keyvalue/store` and `wasi:keyvalue/batch`
  interfaces are supported, the `wasi:keyvalue/atomic` interface is not
  supported." This app uses no atomic/CAS operation anywhere (`CLAUDE.md`
  states this repeatedly as the reason for every write-ordering rule), so that
  restriction costs nothing.
- `docs/quotas-and-limits` — hard numbers, previously undocumented in this
  repo:

  | Quota | Default limit |
  |---|---|
  | Memory (RAM per execution) | 128 MiB |
  | **App size** | **50 MiB** |
  | **Request handler duration** | **30 seconds** |
  | Request/response size | 10 MiB |
  | KV storage (all stores) | 2 GB |
  | **KV read requests per app** | **1,000 RPS** |
  | **KV write requests per app** | **50 RPS** |
  | Max value size | 1 MB |
  | Max key size | 8 KB |

  Same page: "SQLite storage, Redis triggers, wasi-blobstore, wasi-messaging,
  and custom triggers are not supported. **Runtime configuration is also
  unavailable.**" That last sentence independently confirms `runtime-config.toml`
  is inert on Akamai.
- Whether the 50 MiB app size is per component or compressed is **not
  specified** on that page — UNCONFIRMED, and a real deploy risk given two
  `componentize-py` Wasm artifacts (see "Deploy risks" below).
- Deploy flow: `spin plugin install aka`, `spin aka login`, `spin build`,
  `spin aka deploy` (`docs/quickstart`); variables via
  `spin aka deploy --variable key=value` or `--variable @file.toml`
  (`docs/deploy-app-variables`, `docs/aka-command-reference`). The docs do
  **not** distinguish `secret = true` variables from plain ones —
  UNCONFIRMED whether `admin_bootstrap_password` can be supplied this way;
  to confirm, run the deploy and observe whether the app boots or fails on the
  required variable.
- Whether Akamai's KV host implements Spin's `get-keys` is **UNCONFIRMED** —
  the KV page never mentions key enumeration. `backup.py`, `consistency.py`,
  `urlpolicy.handle_violations` and `users.handle_delete` all depend on it.
  This is the single highest-risk unknown in the deploy and gets its own
  early verification step (see Verification step 11).

**This repo, confirmed by reading the code**

- `redirect/main.go` has exactly three `kv.Open` calls: lines 47 and 74
  (`"links"`, in `handleRedirectGet`/`handleRedirectPost`) and line 111
  (`"analytics"`, in `recordAnalytics`).
- **The hot path does two KV data operations per resolution, not one.**
  `lookupLink` (`redirect/main.go:147`) calls `store.Exists("slug:"+slug)`
  *and then* `store.Get("slug:"+slug)`. `CLAUDE.md`'s Akamai section and the
  `TASKS.md` "Resolution-time destination enforcement" rejection both say
  "exactly one `slug:` read" and both name a function `resolveLink` that does
  not exist — the accurate count is 1 `Open` + `Exists` + `Get` for a
  resolution, plus 1 `Open` + 1 `Get` + 2 `Set`s when a click is recorded.
  The non-goal below is stated against the *real* counts.
- All 17 `key_value.open` calls live in `api/app.py`, lines 59–227. No other
  Python module imports `spin_sdk` at all.
- **Every business-logic module already works in a store-agnostic, logical key
  space.** `backup.py` and `consistency.py` receive a `stores_by_name` dict and
  a `list_keys` callable; `links.py`/`auth.py`/`users.py`/`bulk.py`/
  `urlpolicy.py`/`analytics.py` receive a bare `store`. The only store methods
  used anywhere are `get`, `set`, `delete`, `exists`
  (`grep -n "store\.\(get\|set\|delete\|exists\)" api/*.py`), plus `get_keys`
  used exclusively inside `app.py:_kv_keys`. That four-method surface is what
  makes a wrapper viable.
- `api/tests/fakes.py`'s `FakeStore` implements exactly those four methods plus
  a synchronous `keys()`, drained by `fake_list_keys`.
- Test baselines, run 2026-08-04 on this checkout: `api` **474 passed**,
  `gui-pages` **71 passed**, `cd redirect && go test ./linkgate/...` **ok**.
- `gui-pages/tests/test_manifest_components.py:43-45` asserts
  `kv_explorer["key_value_stores"] == ["links", "analytics"]` and
  `"users" not in ...`. This test **will fail** on the manifest change and must
  be rewritten, not deleted.
- `docs/plans/kv-explorer.md:461` already anticipated this exact collision:
  "with one store, `key_value_stores = ["default"]` grants the [explorer
  everything]". The consequence is not a surprise; it is a previously-costed
  trade now being accepted.
- `dev/kv-explorer-up.sh` concatenates `spin.toml` + `dev/kv-explorer.toml`
  into a gitignored `spin-dev.toml` on every run and execs
  `spin up -f spin-dev.toml --build --runtime-config-file runtime-config.toml`.
  The reviewer's check `grep -c kv-explorer spin.toml` → `0` is unaffected by
  anything in this plan.
- CI (`Jenkinsfile`) runs the three suites in parallel Docker stages with the
  repo as the workspace and `dir('api')` / `dir('gui-pages')` / `dir('redirect')`
  inside it — so a test that reads a file from another component's tree works
  in CI. `gui-pages/tests/test_manifest_components.py` already relies on this
  (`Path(__file__).resolve().parents[2]`). **The Jenkinsfile itself needs no
  change**: this plan does not alter how any suite is invoked.
- No `.wasm` artifacts exist in the working tree, so the built app size could
  not be measured during planning — see Verification step 9.

## Data model: the physical key space

One physical store, `"default"`. Three logical namespaces, distinguished by a
prefix on every key:

| Logical store | Prefix | Physical keys |
|---|---|---|
| `links` | `links:` | `links:slug:<slug>`, `links:all_links`, `links:owner_links:<username>`, `links:_meta:url_policy` |
| `users` | `users:` | `users:user:<username>`, `users:session:<token>`, `users:_meta:usernames`, `users:_meta:bootstrapped` |
| `analytics` | `analytics:` | `analytics:count:<slug>`, `analytics:events:<slug>:<slot>` |

**Collision safety.** The scheme is unambiguous iff no prefix is a prefix of
another. `links:`, `users:` and `analytics:` differ in their first character,
so the invariant holds by inspection — and it is pinned by a test rather than
by inspection (`test_kvprefix.py`, below), because the failure mode of a
violated invariant is silent cross-namespace aliasing. Nothing *inside* a
namespace can escape it: a logical key is appended whole, so a slug containing
a colon (today impossible — `CUSTOM_SLUG_PATTERN` is `^[A-Za-z0-9_-]{3,32}$`,
generated slugs are alphanumeric) still lands under `links:`.

**Key size.** Longest physical key is
`links:owner_links:<username>` or `links:slug:<32 chars>` — tens of bytes,
against Akamai's 8 KB key limit. No concern.

**An unprefixed physical key belongs to no namespace and is invisible to the
application.** After this change, a key written directly through the KV
explorer without a prefix is not returned by any view, does not appear in a
backup, is not seen by the consistency check, and **will be pruned by the next
restore**. Before consolidation such a key would have surfaced as
`unrecognized_key` in the consistency report. This is a real, deliberate loss
of visibility, accepted because the only way to create one is by hand in a
dev-only tool; a 13th consistency check for it is listed under Future work.

## API changes

### New: `api/kvprefix.py`

Pure logic, zero `spin_sdk` imports, host-importable under pytest — the same
rule `backup.py` and `consistency.py` follow. This is the whole of the
consolidation's Python-side mechanism.

```python
"""Maps this app's three logical KV namespaces onto Spin's single "default"
physical store by prefixing every key. Required by Akamai Functions, which
allows only the "default" label (see CLAUDE.md's Akamai deployment section).

Zero `spin_sdk` imports: the already-opened physical store arrives as a plain
parameter, so this module stays host-importable, the same rule backup.py and
consistency.py follow.
"""

PHYSICAL_STORE = "default"

# No prefix may be a prefix of another — see test_kvprefix.py, which pins it.
STORE_PREFIXES = {
    "links": "links:",
    "users": "users:",
    "analytics": "analytics:",
}


class PrefixedStore:
    """A logical view of one namespace inside the physical store.

    Deliberately exposes only get/set/delete/exists — the entire store surface
    every business-logic module uses. It does NOT expose get_keys: an
    unscoped enumeration through a view would return every key in the app,
    including `users:user:*`, to callers (backup.py, consistency.py,
    urlpolicy.handle_violations, auth.delete_sessions_for_user) whose guards
    are all written against unprefixed key shapes and would silently fail to
    match. Omitting the method turns that mistake into an AttributeError
    instead of a credential leak. Enumerate through scoped_list_keys().
    """

    __slots__ = ("raw", "prefix")

    def __init__(self, raw, prefix: str):
        self.raw = raw
        self.prefix = prefix

    async def get(self, key: str):
        return await self.raw.get(self.prefix + key)

    async def set(self, key: str, value: bytes) -> None:
        await self.raw.set(self.prefix + key, value)

    async def delete(self, key: str) -> None:
        await self.raw.delete(self.prefix + key)

    async def exists(self, key: str) -> bool:
        return await self.raw.exists(self.prefix + key)


def open_views(physical_store) -> dict[str, PrefixedStore]:
    """{"links": view, "users": view, "analytics": view} over one open store."""
    return {
        name: PrefixedStore(physical_store, prefix)
        for name, prefix in STORE_PREFIXES.items()
    }


def scoped_list_keys(raw_list_keys):
    """Wrap a raw list_keys(physical_store) callable into one that takes a
    PrefixedStore and returns only that namespace's keys, prefix stripped.

    THIS FILTER IS A SECURITY CONTROL, not tidiness. backup.py's
    redact_user_value matches the `user:` prefix and is_excluded_key returns
    False for every store but "users"; consistency.py classifies keys by
    unprefixed shape; auth.delete_sessions_for_user matches `session:`. Every
    one of those guards silently stops matching if a view's enumeration
    returns another namespace's keys — the concrete failure being full PBKDF2
    account hashes written into a backup file.
    """

    async def list_keys(store) -> list[str]:
        if not isinstance(store, PrefixedStore):
            raise TypeError(
                "scoped_list_keys requires a PrefixedStore; enumerating the "
                "physical store directly would cross namespace boundaries"
            )
        prefix = store.prefix
        return [
            key[len(prefix):]
            for key in await raw_list_keys(store.raw)
            if key.startswith(prefix)
        ]

    return list_keys
```

Two properties worth stating explicitly because they are what make the whole
approach safe:

1. **No module other than `app.py` and `kvprefix.py` learns that prefixes
   exist.** `links.py`, `auth.py`, `users.py`, `bulk.py`, `backup.py`,
   `consistency.py`, `urlpolicy.py` and `analytics.py` are byte-for-byte
   unchanged, as are all ~257 lines of key literals across the nine test
   modules.
2. **`isinstance` here is against our own class**, never against a `spin_sdk`
   type, so it behaves identically under pytest (wrapping a `FakeStore`) and
   under WASI (wrapping a real `Store`). This is the same duck-typing
   discipline `responses.Request`/`Response` already rely on.

### `api/app.py` — the only rewiring site

Add `import kvprefix` to the alphabetical block (between `domains` and
`links`). Replace line 59 (`users_store = await key_value.open("users")`) with:

```python
        physical_store = await key_value.open(kvprefix.PHYSICAL_STORE)
        stores = kvprefix.open_views(physical_store)
        links_store = stores["links"]
        users_store = stores["users"]
        analytics_store = stores["analytics"]
        list_keys = kvprefix.scoped_list_keys(_kv_keys)
```

Then **delete all 16 remaining `await key_value.open(...)` lines** inside the
branches (lines 92, 101, 108, 118, 128, 129, 140, 150, 176, 183, 184, 195, 196,
207, 220, 227) — the local names they bound are now bound once at the top. The
views are plain Python objects, so binding all three unconditionally costs
nothing and reduces the per-request host `open` calls from up to four to one.

Every argument that is `_kv_keys` today becomes `list_keys` (four call sites:
`users.handle_delete`, `backup.handle_export`, `backup.handle_restore`,
`urlpolicy.handle_violations`, plus `consistency.handle_consistency`).
**`_kv_keys` itself is unchanged and must not be passed to a handler
directly** — it now enumerates the whole app.

`backup.handle_export`/`handle_restore` may take `stores` directly (it is
exactly `{"links", "users", "analytics"}`). **`consistency.handle_consistency`
must keep its explicit `{"links": links_store, "users": users_store}` literal
and its existing comment** explaining why `analytics` is deliberately not
handed over — passing `stores` there would silently widen the walk.

`from spin_sdk import key_value, variables` stays; `key_value` is still used
once.

### What does not change

`api/links.py`, `auth.py`, `users.py`, `bulk.py`, `backup.py`,
`consistency.py`, `urlpolicy.py`, `analytics.py`, `qr.py`, `domains.py`,
`responses.py`, `tags.py` — no edits. `api/tests/fakes.py` — no edits;
`FakeStore` becomes the *physical* store in the new tests and is still used
directly by every existing test.

The backup file format stays at `schema_version: 1` with logical store names
(`BACKUP_STORES`, `RESTORE_STORE_ORDER`) and unprefixed keys, and the
consistency report's `stores_scanned` stays `["links", "users"]`. Both are
public contracts — the `?stores=links,users` query parameter and the
downloaded file, and the report the GUI renders — and under the view approach
there is no reason to churn either. **A backup file taken before this change
restores unmodified after it**, which is the documented upgrade path below and
is pinned by a test.

## Redirect (Go) changes

The language-split rule is not in play here: this is the redirect hot path, it
stays in Go, and no logic moves between components. Per the testability rule,
the new pure logic goes in `redirect/linkgate/`, never in `package main`.

### New: `redirect/linkgate/keys.go`

```go
package linkgate

import "strconv"

// Physical key prefixes for the single "default" KV store. These MUST stay
// byte-identical to api/kvprefix.py's STORE_PREFIXES — a mismatch means the
// API writes links the redirect path cannot find, with no error anywhere.
// api/tests/test_kvprefix.py reads this file and pins that equality.
const (
	LinksPrefix     = "links:"
	AnalyticsPrefix = "analytics:"
)

// LinkKey is the physical key of a link record: links:slug:<slug>.
func LinkKey(slug string) string { return LinksPrefix + "slug:" + slug }

// CountKey is the physical key of a slug's click counter.
func CountKey(slug string) string { return AnalyticsPrefix + "count:" + slug }

// EventKey is the physical key of one recent-events ring-buffer slot.
func EventKey(slug string, slot int) string {
	return AnalyticsPrefix + "events:" + slug + ":" + strconv.Itoa(slot)
}
```

`redirect/linkgate/keys_test.go` pins the literal outputs
(`LinkKey("abc") == "links:slug:abc"`, `CountKey("abc") == "analytics:count:abc"`,
`EventKey("abc", 7) == "analytics:events:abc:7"`) — the point being that a
future "tidy-up" of the string construction cannot change the physical layout
without failing a test.

There is deliberately **no** `users:` prefix constant in `linkgate`: the
redirect component has no business constructing a `users` key, and an unused
constant is an invitation.

### `redirect/main.go`

- Lines 47, 74, 111: `kv.Open("links")` / `kv.Open("analytics")` all become
  `kv.Open("default")`. **The three call sites stay three call sites** — see
  the non-goal on op counts.
- Line 148/153 in `lookupLink`: `"slug:" + slug` → `linkgate.LinkKey(slug)` in
  both the `Exists` and the `Get`.
- Line 120: `countKey := "count:" + slug` → `linkgate.CountKey(slug)`.
- Line 128: `fmt.Sprintf("events:%s:%d", slug, slot)` →
  `linkgate.EventKey(slug, slot)`. **This removes the only use of `fmt` in
  `main.go`** — the `fmt` import must be dropped in the same edit or
  `go tool componentize-go build` fails on an unused import.

`redirect/linkgate/link.go`, `analytics.go` and `prompt.html` are untouched.
`passwordgate.go` loses one now-redundant line — see the next section.

## Edge caching and the never-cache invariant

Raised by the user while reviewing this plan, and a real gap: the deploy target
is a CDN. This is not a consequence of the store consolidation, but it is a
correctness property that only becomes reachable once the app is on Akamai, so
it lands here rather than waiting for its own plan.

### Facts confirmed

- **Akamai does not cache 302 by default.** `techdocs.akamai.com/property-mgr/
  docs/cache-http-redirects`: "By default, Akamai edge servers do not cache HTTP
  302 **Found** and 307 **Temporary Redirect** redirects returned from the
  origin server." All three redirect sites in `redirect/main.go` (lines 65, 88,
  103) use `http.StatusFound` — 302 — so the default posture is safe and
  `recordAnalytics` runs on every click.
- **301 and 308 permanent redirects ARE cached by default**, and enabling 302
  caching is one Property Manager toggle (`cacheRedirect`).
- **Akamai edge servers do not honour origin `Cache-Control`/`Expires` by
  default.** So a response header is defence-in-depth against browsers and
  intermediate proxies, not a guarantee at the Akamai edge.
- **Akamai Functions has no caching documentation page** (`docs/caching` 404s).
  Whether a `*.fwf.app` app inherits Property Manager's defaults is
  **UNCONFIRMED** and must be measured at deploy time, not assumed — Verification
  step 14 is that measurement.
- **Today the 302s carry no cache headers at all.** The only `Cache-Control` in
  the component is `redirect/passwordgate.go:21`'s `no-store` on the prompt
  page.
- **The password gate is not bypassable through caching**, and the plan should
  not claim otherwise: a `GET` on a protected link returns the `no-store` prompt
  page, and the 302 is only ever produced by a `POST`, which is not cacheable by
  default anyway.

### Why it matters

`CLAUDE.md` states a "never cache" principle as a correctness requirement in two
places — the time-window check ("re-checked from a fresh KV fetch on every
`/r/{slug}` request… the same 'never cache' principle the password gate already
uses") and the destination-policy remediation path ("one bulk Disable away from
`404`ing through the `status` check"). **Nothing in the code enforces it.** If a
`/r/{slug}` response ever became cacheable — someone switches to a 301 for
"performance", or someone enables `cacheRedirect` at the property level — then:

- a disabled link keeps redirecting,
- a time-windowed link keeps redirecting past its `end_at`,
- a deleted link keeps redirecting,
- a repointed link keeps serving the old destination,
- and a destination-policy violator can no longer be disabled, silently undoing
  the feature shipped immediately before this one.

The `404` responses matter too, and are the case three per-call-site header
writes would miss: a link that is not yet active returns `404`, and a cached
`404` means the link never starts working when its window opens.

### The change

`redirect/main.go`'s `setSecurityHeaders` is already called once in the
`spinhttp.Handle` wrapper, before `mux.ServeHTTP`, and therefore applies to
**every** response the component sends — 302s, 404s, 500s and the prompt page
alike. That is the right home, not three `Header().Set` calls at the redirect
sites:

```go
	// Never cached, anywhere, by anything. Resolution re-reads KV on every
	// request by design: the status check, the [start_at, end_at) window, a
	// repointed destination, a deleted slug and the destination-policy
	// remediation path (bulk Disable -> 404) are all only correct if no layer
	// is serving a remembered answer. This covers the 404s as well as the
	// 302s — a cached "not yet active" 404 means the link never starts
	// working when its window opens.
	//
	// The 302 in handleRedirectGet/handleRedirectPost is load-bearing and
	// must never become a 301 or 308: Akamai edge servers do not cache 302
	// or 307 by default, but they DO cache 301 and 308 by default
	// (techdocs.akamai.com/property-mgr/docs/cache-http-redirects). This
	// header is defence-in-depth for browsers and intermediate proxies —
	// Akamai does not honour origin Cache-Control by default, so at the edge
	// the 302 status is the actual control.
	h.Set("Cache-Control", "no-store")
```

`redirect/passwordgate.go:21`'s `w.Header().Set("Cache-Control", "no-store")`
becomes redundant and is **removed**, with a one-line comment recording that it
moved to `setSecurityHeaders` and now covers every response rather than just the
prompt. (`Header().Set` overwrites, so leaving it would not emit a duplicate
header — it would just be dead code asserting something already true.)

**No `linkgate` test.** This is a header write on an `http.ResponseWriter` in
`package main`, which is not host-testable at all
(`wit_exports.go:934:6: missing function body`), and there is no pure logic to
extract that would not be a worse abstraction than the two-line call it
replaces. It is verified live with `curl -sI`, locally and again against the
deployed app — see Verification steps 5 and 14. Inventing a
`linkgate.CacheControlValue()` constant purely to have something to assert would
test that a string equals itself.

## Manifest and runtime-config changes

`spin.toml`:

- `[component.redirect]` line 24: `key_value_stores = ["links", "analytics"]`
  → `key_value_stores = ["default"]`
- `[component.api]` line 39: `key_value_stores = ["links", "users", "analytics"]`
  → `key_value_stores = ["default"]`

Nothing else in `spin.toml` changes: no route, no variable, no build command,
no component. `gui-pages/tests/test_manifest_components.py`'s first test
(component set is exactly `{redirect, api, gui, gui-pages}`) keeps passing
untouched.

`runtime-config.toml` is **kept**, reduced to a single block:

```toml
[key_value_store.default]
type = "spin"
```

with a header comment recording that (a) Spin auto-provisions `"default"`, so
`--runtime-config-file` is now *optional* locally, (b) it is retained so the
documented local command and `dev/kv-explorer-up.sh` keep working unchanged and
the local backing provider stays explicit, and (c) **Akamai Functions ignores
runtime configuration entirely** ("Runtime configuration is also unavailable"),
so this file has no deployment role at all. Deleting the file was considered
and rejected — see the trade-offs.

`dev/kv-explorer.toml` line 42: `key_value_stores = ["links", "analytics"]` →
`key_value_stores = ["default"]`, and the comment block above it (lines 17–22,
the "`users` is deliberately NOT listed" paragraph) is **replaced**, not
deleted, with an accurate one:

> With the stores consolidated onto Spin's single `"default"` label (required
> by Akamai Functions), there is no longer any store-level separation to
> withhold: granting `"default"` grants every key, `users:user:*` PBKDF2
> hashes and `users:session:*` tokens included, with full CRUD. This is a
> deliberate, accepted local-dev exposure, not an oversight. It is acceptable
> only because this fragment is never part of a deployed manifest — the
> committed `spin.toml` has four components and no explorer, and
> `gui-pages/tests/test_manifest_components.py` fails CI if that ever stops
> being true. Anyone who runs `dev/kv-explorer-up.sh` can read and forge local
> credentials; treat a local run as untrusted, and never point it at data you
> care about.

`allowed_outbound_hosts = []` and the `/internal/kv-explorer/...` route are
unchanged.

## Test changes

### New: `api/tests/test_kvprefix.py`

Unit coverage for the mechanism plus the cross-language guard:

1. `PrefixedStore` round-trips `get`/`set`/`delete`/`exists` and the physical
   `FakeStore` holds the prefixed key (`store.keys() == ["links:slug:a"]`).
2. Two views over one physical store do not see each other's keys: a
   `users` view `get("user:alice")` returns `None` when only
   `links:user:alice` exists, and vice versa.
3. **The non-overlap invariant**: for every ordered pair of distinct values in
   `STORE_PREFIXES`, neither is a prefix of the other.
4. `PrefixedStore` has no `get_keys` attribute, and passing one to a raw
   `list_keys` raises rather than enumerating everything.
5. `scoped_list_keys` filters and strips: over a physical store holding
   `links:slug:a`, `users:user:bob`, `analytics:count:a` and a bare
   `orphan-key`, the links view yields exactly `["slug:a"]`.
6. `scoped_list_keys` raises `TypeError` when handed the physical store.
7. **Cross-language drift guard**: read
   `Path(__file__).resolve().parents[2] / "redirect" / "linkgate" / "keys.go"`,
   regex out the `LinksPrefix`/`AnalyticsPrefix` literals, and assert they
   equal `kvprefix.STORE_PREFIXES["links"]` / `["analytics"]`. Precedent for
   reading across component trees from a test:
   `gui-pages/tests/test_manifest_components.py`, which parses `spin.toml` and
   `dev/kv-explorer.toml` the same way, and works in CI because each Jenkins
   stage `dir()`s into the full repo checkout.

### New: `api/tests/test_store_isolation.py`

The four hazards that make this more than a rename. Each builds **one**
physical `FakeStore` holding keys from several namespaces, wraps it with
`open_views`, and drives the *real* handlers.

1. **A backup taken through the links view cannot contain an account hash.**
   Physical store holds `users:user:alice` whose value contains a real-shaped
   `pbkdf2_sha256$100000$...` `password_hash`, plus `links:slug:a`. Call
   `backup.handle_export({"links": views["links"]}, ...)` with
   `?stores=links` and the scoped `list_keys`; assert the response body
   contains neither `password_hash` nor `pbkdf2_sha256`, and that
   `doc["stores"]["links"]` keys are exactly `["slug:a"]`. Name the test for
   the guarantee. This is the property `CLAUDE.md` records as mutation-tested
   ("disabling redaction fails 5 tests, disabling exclusion fails 6") — those
   guards match on unprefixed key *shape*, so they would not have fired here;
   add a comment saying so, and state the mutation that breaks this test
   (replacing `scoped_list_keys`'s filter with a pass-through).
2. **A single-store restore prunes only within its own prefix.** Physical
   store holds `links:slug:old`, `links:all_links`, `users:user:alice`,
   `users:session:tok`, `analytics:count:old`. Restore a `schema_version: 1`
   file containing only a `links` store with `slug:new`. Assert afterwards:
   `links:slug:old` is gone, `links:slug:new` exists, and **every `users:` and
   `analytics:` key is byte-identical**.
3. **A pre-consolidation backup file restores unchanged after the
   consolidation.** Load the captured fixture (below), restore it through the
   views, and assert the physical store now holds the prefixed forms
   (`links:slug:<slug>`, `users:user:<u>`, `analytics:count:<slug>`) and that a
   fresh `handle_export` over all three views reproduces the same `stores`
   object as the fixture's (modulo `created_at`/`created_by`). This is the
   documented upgrade path, pinned.
4. **The consistency report does not see analytics keys.** Physical store
   holds `analytics:count:a` and `analytics:events:a:3` alongside a clean
   links/users set; run `consistency.handle_consistency` over the two views
   and assert `unrecognized_key` count is `0` and the report is `ok: true`.
   Without the prefix filter every analytics key would report as
   `unrecognized_key` on every run, which is exactly how a checker becomes
   noise and gets ignored.

### New: `api/tests/fixtures/backup-pre-consolidation.json`

A real backup captured from a live **pre-consolidation** `spin up`, so test 3
above proves format identity rather than restating the current code's beliefs.
Capture constraint: **create no password-protected link in the source data**,
so the file contains no PBKDF2 hash of any kind (account hashes are redacted at
export by design; a *link* password hash is deliberately not — see `CLAUDE.md`'s
backup section — and must not be committed to the repo). Two or three links,
one extra user, a few analytics keys.

### Rewritten: `gui-pages/tests/test_manifest_components.py`

`test_kv_explorer_fragment_grants_only_links_and_analytics` is **renamed and
rewritten**, never deleted:
`test_kv_explorer_fragment_grants_the_single_default_store`, asserting
`kv_explorer["key_value_stores"] == ["default"]`, `allowed_outbound_hosts == []`
and the unchanged route, with a docstring stating plainly that the explorer can
now read and write every key including `users:` credential material, that this
is an accepted local-dev-only exposure, and pointing at this plan. The
`"users" not in ...` assertion goes away with the store it was about. Test
count stays 71.

### New: `redirect/linkgate/keys_test.go`

As described above. Run with `cd redirect && go test ./linkgate/...` — never
`go test ./...`, which fails by design on `package main`.

## Documentation changes (builder tasks, not planner edits)

- **`CLAUDE.md` line 22** — the "`users` is deliberately withheld" bullet,
  including "Confirmed live: `users` returns `500 access denied`" — is now
  **false** and must be rewritten to the accepted-exposure wording above.
- **`CLAUDE.md` line 325** — "That consolidation would silently hand the KV
  explorer the `users` keys… Do not let it fall out of the change unnoticed" —
  its warning has been heeded and its tense is wrong; rewrite as a record of
  the decision taken.
- **`CLAUDE.md` lines 319–329**, the whole "Deployment: known Akamai Functions
  blocker" section, becomes "Deployment: Akamai Functions" — the blocker is
  gone. It should carry the confirmed quota table, the deploy commands, the
  variables that must be set (`public_base_urls` pointing at the real app URL,
  `cookie_secure=true`, `admin_bootstrap_password`), the backup→deploy→restore
  upgrade path, and the operating ceilings derived below.
- **`CLAUDE.md` Architecture** — the two `key_value_stores` descriptions, plus
  a new short subsection on the prefixing view: that `api/kvprefix.py` is the
  only module that knows prefixes exist, that `scoped_list_keys` is a security
  control, that `PrefixedStore` deliberately has no `get_keys`, and that Go's
  `linkgate/keys.go` must stay in lockstep with `STORE_PREFIXES`.
- **`CLAUDE.md` Commands / Tests** — `--runtime-config-file runtime-config.toml`
  is now optional locally; keep it in the documented command and say why it is
  kept.
- **`CLAUDE.md`'s "a new KV key type obliges two changes" rule** gains a third
  clause: the key must live under one of the three prefixes, or it is invisible
  to the whole application.
- `TASKS.md:303`'s Future-work entry needs a `— **SCHEDULED 2026-08-04**`
  closure note. The planner cannot rewrite an existing `TASKS.md` line, so this
  is an explicit builder task, exactly as `TASKS.md:583` was for the
  destination-URL-policy plan.

`README.md`, `PRODUCT.md` and `DESIGN.md` need no change — no user-visible
behaviour, no new page, no new token. (Confirmed by grep: neither `README.md`
nor `PRODUCT.md` mentions a store name.)

## Deploy risks and the operating ceilings this surfaces

None of these are *caused* by the consolidation; the deploy is what makes them
real, and the plan should not discover them at the console.

- **App size vs. 50 MiB.** Two `componentize-py` artifacts (`api/app.wasm`,
  `gui-pages/app.wasm`) plus `redirect/main.wasm` plus the vendored
  `spin_static_fs.wasm` plus `gui/`. componentize-py output is routinely tens
  of megabytes. **Unmeasured** — no `.wasm` exists in the tree. This is
  measured before the deploy is attempted (Verification step 9). If it exceeds
  the cap, options in order of preference: `wasm-opt -Oz` / stripping debug
  sections on the Python artifacts; then confirming with Akamai whether the
  limit is compressed or per-component. **Do not** respond by merging
  `gui-pages` into `api` — that trades a measured problem for a routing rewrite.

  > **CORRECTION, 2026-08-05 — this paragraph's preference order was wrong and
  > its first option is impossible.** Measured: 60.38 MiB raw, ≈10.38 MiB over.
  > (1) **`wasm-opt` cannot run on these artifacts at all** — all three are
  > component-model binaries and Binaryen 131 refuses to parse them
  > (`this looks like a wasm component, which Binaryen does not support yet`,
  > WebAssembly/binaryen#6728). (2) `wasm-tools strip -a` works but saves only
  > **1.39 MiB (2.3%)** — debug sections are not the bulk. (3) `wasm-tools
  > objdump` shows the bulk is ≈15 MiB of stdlib `data` plus a 6.01 MiB
  > interpreter module **in each** Python component — ≈21 MiB of the total is a
  > second copy of CPython. (4) **gzip -9 puts the whole app at ≈22.48 MiB, 55%
  > under the cap**, so the "compressed or not" question this paragraph ranked
  > *last* is the one that decides whether there is any problem at all. Resolve
  > it with Akamai FIRST. Only if the cap is raw does the structural question
  > open, and then the real candidates are merging `gui-pages` into `api`
  > (≈21 MiB — the "do not" above is asserted here without a stated reason and
  > should be re-argued on its merits, not inherited) or rewriting `gui-pages`
  > in Go (est. ≈14-17 MiB, never considered by this plan).
- **`get_keys` support.** If Akamai's KV host does not implement it, backup,
  restore, the consistency check, the violations report and user deletion all
  break. Verified as early as possible after deploy (step 11) because it
  affects whether the deploy is usable at all, not just whether it is complete.
- **Write rate: 50 RPS, app-wide.** A successful click performs **two writes**
  (`analytics:count:<slug>` and one `analytics:events:<slug>:<slot>`), so
  sustained click throughput ceilings at roughly **25 redirects/second**
  before writes are throttled. Reads (3 per click) are nowhere near the 1,000
  RPS read cap. Worth recording in `CLAUDE.md`; worth an explicit conversation
  with Akamai if this deployment expects more.
- **30-second handler duration vs. bulk KV work.** At 50 write RPS, a restore
  at the `MAX_BACKUP_ENTRIES = 5_000` cap needs ~100 s of writes and **cannot
  complete inside one Akamai request**, even though the same restore measured
  84 ms locally (`TASKS.md:526`). Export is read-bound and safer (5,000 reads
  at 1,000 RPS ≈ 5 s) but a store with many analytics event keys enumerates and
  reads far more than 5,000 keys, since the export entry cap is only applied at
  *restore*. Bulk create at the 50-row cap (~100 KV ops) is fine.
  **In scope for this plan: measure and document the real ceiling on the
  deployed app at small scale. Out of scope: re-architecting backup/restore
  into chunks** — that is a Future-work entry, and its trigger is a deployment
  whose data exceeds what one request can move.
- **`secret = true` variables via `--variable`.** Unconfirmed; the deploy step
  is where it is confirmed.

## Upgrade path for an existing deployment

**In-place upgrade is not possible.** The old physical stores (`links`,
`users`, `analytics`) and the new one (`default`) are distinct namespaces; the
new build reads only `default`, so every record written by the old build is
invisible to it. Nothing is destroyed — the old stores simply go unread.

**Backup/restore already solves this for free**, and this is the documented
path:

1. On the **old** build, `GET /api/admin/backup` (all three stores).
2. Deploy the **new** build.
3. `POST /api/admin/restore` with `{"confirm": "REPLACE", "backup": <file>}`.

This works precisely because the backup file speaks logical store names and
unprefixed keys, and the view maps them onto physical keys on the way in — the
file needs no conversion and no schema bump. Test 3 in
`test_store_isolation.py` is the pin on that property.

Two carried-over caveats, unchanged by this plan and already documented in
`CLAUDE.md`'s backup section: sessions and *account* password hashes are not in
the backup, so every restored account except the re-seeded bootstrap admin
needs a new password; *link* password hashes and all analytics **do** survive.

For the Akamai deploy specifically this is all moot on day one — the store is
freshly provisioned and empty, `ensure_bootstrap_admin` seeds the first admin
on the first request. It matters for the *local* store (which `CLAUDE.md`
records as non-persistent anyway) and for any future re-platforming.

## Non-goals

Stated so the builder does not "improve" them:

- **No behaviour change to any endpoint.** Every status code, error body and
  response shape is identical before and after. The backup file format stays at
  `schema_version: 1`; `BACKUP_STORES`, `RESTORE_STORE_ORDER` and
  `CONSISTENCY_STORES` keep their logical names.
- **No schema change to any record.** No field added, removed or renamed.
- **No change to the redirect hot path's KV operation count.** It stays
  1 `Open` + `Exists` + `Get` per resolution, and 1 `Open` + 1 `Get` +
  2 `Set`s for a recorded click. Merging the `Exists`/`Get` pair into one
  `Get`, or passing the already-open store into `recordAnalytics` to save an
  `Open`, are both plausible and both **out of scope** — they are performance
  changes wearing a refactor's clothes, and bundling them makes a regression
  in this change impossible to attribute.
- **No migration tool.** The upgrade path is backup → deploy → restore, as
  above.
- **No new consistency check, no 13th check id.**
- **No change to how tests are invoked**, so `Jenkinsfile` is untouched.

## Trade-offs and rejected alternatives

**1. Flattening the prefixes into every key literal (rejected).**
The obvious reading of `CLAUDE.md`'s own description of the fix: change
`f"slug:{slug}"` to `f"links:slug:{slug}"` everywhere, and let each module
speak physical keys. Attractive because there is no indirection at all — the
string in the source is the string in the store, which is exactly what a
developer debugging with the KV explorer wants, and there is no wrapper class
whose semantics someone has to learn. It loses on cost and on blast radius:
~55 key literals across eight non-test modules and ~257 lines across nine test
files, every one of them a place to typo a prefix into a silent
wrong-namespace write; `backup.py`'s `is_excluded_key`, `redact_user_value`,
`INDEX_KEYS`, `restore_write_order` and `validate_backup` would all need
prefix-aware rewrites, and `consistency.py`'s twelve-check key classification
with them. Worse, the backup file's key space would change, which means a
`schema_version` bump, `SUPPORTED_SCHEMA_VERSIONS` growing, and a converter for
v1 files — turning a free upgrade path into a feature. The view confines the
change to `app.py` plus one new 60-line module and keeps every existing test as
a regression suite for the refactor, which is worth more here than literal
fidelity.

**2. The honest cost of the view.** There is now a layer between what the code
appears to write and what is in the store: `links.py` says `slug:abc`, the
store holds `links:slug:abc`, and the KV explorer's raw listing no longer
matches any string in the business logic. Anyone debugging from the explorer
has to know the mapping. Accepted, and mitigated by: the mapping being one
table in one file, `linkgate/keys.go` making the Go side's physical keys
explicit and tested, and a `CLAUDE.md` subsection saying so. Note the mild
silver lining — the explorer's single flat listing now shows the whole app's
key space at once, which makes the prefix scheme *visible* rather than
inferred.

**3. Preserving store separation locally via a config seam (rejected — user
decision).** E.g. a Spin variable selecting one-store vs. three-store mode, so
the KV explorer could keep being denied `users`. Attractive for exactly one
reason: it preserves the host-level guarantee that the explorer cannot read
password hashes. Rejected by the user, and correctly: it would mean the
deployed configuration is not the configuration anyone develops or tests
against, which is a far more expensive property to own than a dev-only
credential exposure in a tool that already has full CRUD over everything else.
The alternative of dropping the explorer was also rejected — repairing bad
local test data is half its value.

**4. Deleting `runtime-config.toml` (rejected).** Spin auto-provisions
`"default"`, so the file is no longer required, and Akamai ignores it entirely
— a file with no remaining purpose is a file that misleads. It is kept anyway
because every documented command, `dev/kv-explorer-up.sh`, and a year of
`TASKS.md` entries pass `--runtime-config-file runtime-config.toml`; removing
it turns a mechanical refactor into a change that breaks a script and
invalidates documentation, for a tidiness gain. It is instead reduced to one
block with a comment saying it is optional locally and inert on Akamai.
Revisit if it ever acquires a second reason to exist, or when someone is
already editing the dev script.

**5. Caching the physical key enumeration per request (rejected for v1).**
`_kv_keys` now enumerates the whole app rather than one store, so a backup
(three `list_keys` calls) walks the full key space three times. A single-shot
cache in `scoped_list_keys` would make it one walk and would be strictly
faster than today. Rejected because it silently couples to
`backup.handle_restore`'s prune step, which calls `list_keys` *after* writing
specifically to find pre-existing keys — a cached pre-write snapshot happens to
produce the same stale set today (newly written keys are in `entries` by
definition and so are excluded either way), but that is a coincidence of the
current code, not a property anyone stated, and a future change to prune order
would break it invisibly. The call *count* is unchanged from today; only each
call's size grows. Listed under Future work with this caveat attached.

**6. A 13th consistency check for unprefixed physical keys (rejected here).**
It would restore the visibility lost in "Data model" above. Rejected as scope:
it changes the report contract (`CHECKS` is documented as exactly twelve, and
the GUI renders them), and the only way to create such a key is by hand in the
dev-only explorer. Future work; the trigger is an actual unprefixed key
appearing in a deployment.

**7. A Spin variable for the prefixes (rejected).** `analytics_event_slots` is
a shared Spin variable precisely because two components must agree on it, which
is the same shape as this problem. It loses because a prefix is structural, not
operator-tunable: a deployment that changed it would orphan all its data, and a
mismatch between components would be silent. Hardcoded constants in both
languages plus the cross-language test that reads `keys.go` from the Python
suite is the stronger guard — it fails at CI time rather than at runtime.

**8. Doing nothing (rejected, and no longer live).** The status quo is
undeployable to the intended production target; this is the sole architectural
blocker. It was the right answer for as long as Akamai was not an immediate
goal — `CLAUDE.md` says so explicitly — and that condition has now ended.

## Tasks

The lines appended to `TASKS.md` under `## KV store consolidation`:

```
- [ ] Capture a pre-consolidation backup fixture (MUST land before the cutover task — it can only be produced by the current build) — file(s): api/tests/fixtures/backup-pre-consolidation.json (new) — done when: a live pre-consolidation `SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false spin up --build --runtime-config-file runtime-config.toml` is seeded with 2-3 links (NONE password-protected, so no PBKDF2 hash of any kind is committed), one extra user and at least one recorded click, and `GET /api/admin/backup` is saved verbatim to that path; the committed file has `"format": "spin-shortener-kv-backup"`, `"schema_version": 1`, all three logical store names, and base64-decoding every value yields no string containing `pbkdf2_sha256`.
- [ ] Add api/kvprefix.py with the PrefixedStore view and scoped_list_keys — file(s): api/kvprefix.py (new), api/tests/test_kvprefix.py (new) — done when: the module has zero `spin_sdk` imports, exposes `PHYSICAL_STORE = "default"`, `STORE_PREFIXES` for links/users/analytics, a `PrefixedStore` with get/set/delete/exists and NO `get_keys`, `open_views(physical_store)` and `scoped_list_keys(raw_list_keys)`; `cd api && uv run pytest` passes with tests covering the four-method round trip against a FakeStore holding prefixed keys, mutual invisibility between two views over one store, the invariant that no prefix is a prefix of another, `PrefixedStore` having no `get_keys` attribute, the filter-and-strip behaviour over a store holding all three namespaces plus one unprefixed key, and `scoped_list_keys` raising TypeError when handed the physical store; nothing else imports the module yet.
- [ ] Add redirect/linkgate/keys.go with the physical key builders — file(s): redirect/linkgate/keys.go (new), redirect/linkgate/keys_test.go (new) — done when: the package exports `LinksPrefix`, `AnalyticsPrefix`, `LinkKey`, `CountKey` and `EventKey` with a comment naming api/kvprefix.py's STORE_PREFIXES as the thing they must match, there is deliberately no users prefix, and `cd redirect && go test ./linkgate/...` passes with tests asserting `LinkKey("abc") == "links:slug:abc"`, `CountKey("abc") == "analytics:count:abc"` and `EventKey("abc", 7) == "analytics:events:abc:7"`; `redirect/main.go` is not yet changed.
- [ ] Add the cross-language prefix drift guard (depends on the two tasks above) — file(s): api/tests/test_kvprefix.py — done when: a test reads `Path(__file__).resolve().parents[2] / "redirect" / "linkgate" / "keys.go"`, extracts the `LinksPrefix`/`AnalyticsPrefix` string literals and asserts they equal `kvprefix.STORE_PREFIXES["links"]`/`["analytics"]`; editing either side alone makes it fail; `cd api && uv run pytest` passes.
- [ ] Add api/tests/test_store_isolation.py pinning the four cross-namespace hazards (depends on api/kvprefix.py and the fixture) — file(s): api/tests/test_store_isolation.py (new) — done when: `cd api && uv run pytest` passes with four tests, each over ONE physical FakeStore wrapped by `open_views`: (1) `backup.handle_export` with `?stores=links` over a store also holding a `users:user:alice` record whose value carries a `pbkdf2_sha256$100000$...` password_hash returns a body containing neither `password_hash` nor `pbkdf2_sha256`, with a comment recording that backup.py's own redaction/exclusion guards match unprefixed key shapes and would NOT have caught this, and naming the mutation that breaks the test (making `scoped_list_keys` a pass-through); (2) a restore of a links-only file leaves every `users:` and `analytics:` key byte-identical while pruning stale `links:` keys; (3) the committed pre-consolidation fixture restores through the views into prefixed physical keys and a fresh export reproduces the same `stores` object; (4) `consistency.handle_consistency` over the links/users views reports `unrecognized_key` count 0 and `ok: true` despite `analytics:count:*` and `analytics:events:*` keys being present in the same physical store.
- [ ] Cut over to the single "default" store (depends on every task above; api/app.py, redirect/main.go and spin.toml must land together or the app is broken between commits) — file(s): spin.toml, runtime-config.toml, api/app.py, redirect/main.go — done when: both `key_value_stores` lines in spin.toml read `["default"]`; runtime-config.toml declares only `[key_value_store.default] type = "spin"` with a comment recording that it is now optional locally and inert on Akamai; api/app.py imports `kvprefix` in the alphabetical block, opens the physical store exactly once per request, binds `links_store`/`users_store`/`analytics_store` from `kvprefix.open_views` and `list_keys` from `kvprefix.scoped_list_keys(_kv_keys)` at the top of `handle_request`, and `grep -c "key_value.open" api/app.py` returns 1 while `grep -c "_kv_keys" api/app.py` returns 2 (the definition and the single wrap) — with the consistency branch keeping its explicit two-store dict and its comment; redirect/main.go calls `kv.Open("default")` at all three sites, uses `linkgate.LinkKey`/`CountKey`/`EventKey` for all four key constructions, and no longer imports `fmt`; `cd api && uv run pytest` (474), `cd gui-pages && uv run pytest` (71 — the manifest test will fail until the next task) and `cd redirect && go test ./linkgate/...` are run and their results recorded; and under a live `SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false spin up --build --runtime-config-file runtime-config.toml`, logging in, creating a link, resolving it at `/r/<slug>` with a 302 and reading its analytics all work.
- [ ] Point the KV explorer at the default store and rewrite the manifest guard it invalidates — file(s): dev/kv-explorer.toml, gui-pages/tests/test_manifest_components.py — done when: `dev/kv-explorer.toml` reads `key_value_stores = ["default"]` with `allowed_outbound_hosts = []` and the unchanged `/internal/kv-explorer/...` route, and its "users is deliberately NOT listed" comment block is REPLACED with one stating the accepted local-dev-only exposure of `users:` hashes and session tokens and why it is acceptable (never part of a deployed manifest); `test_kv_explorer_fragment_grants_only_links_and_analytics` is renamed to `test_kv_explorer_fragment_grants_the_single_default_store`, asserts `== ["default"]`, drops the now-meaningless `"users" not in` assertion, and carries a docstring recording the decision; `grep -c kv-explorer spin.toml` returns 0; `cd gui-pages && uv run pytest` passes at 71.
- [ ] Verify the consolidated store through the KV explorer — file(s): (none — verification step) — done when: `SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_KV_EXPLORER_PASSWORD=<kvpw> SPIN_VARIABLE_COOKIE_SECURE=false ./dev/kv-explorer-up.sh` runs, creating a link and clicking it produces visibly prefixed keys `links:slug:<slug>`, `links:all_links`, `links:owner_links:<user>`, `users:user:admin`, `users:_meta:usernames` and `analytics:count:<slug>` under the single store label `default`, and it is confirmed and recorded that `users:` keys are now readable (the accepted exposure) rather than `500 access denied`.
- [ ] Update CLAUDE.md for the consolidation and the accepted explorer exposure — file(s): CLAUDE.md — done when: the `users`-withheld bullet (today line 22, including "Confirmed live: `users` returns `500 access denied`") and the "would silently hand the explorer the users keys" paragraph (today line 325) are both rewritten to state the accepted exposure and why it is acceptable; the Architecture section's two `key_value_stores` descriptions read `["default"]` and gain a short subsection on the prefixing view (api/kvprefix.py as the only prefix-aware module, `scoped_list_keys` as a security control, `PrefixedStore` deliberately having no `get_keys`, `redirect/linkgate/keys.go` kept in lockstep by a test); the Commands section records that `--runtime-config-file` is now optional locally and why it is kept; and the "a new KV key type obliges two changes" rule gains a third clause that a new key must live under one of the three prefixes or be invisible to the whole app.
- [ ] Replace CLAUDE.md's Akamai blocker section with a deployment section — file(s): CLAUDE.md — done when: "Deployment: known Akamai Functions blocker" becomes "Deployment: Akamai Functions" and records the confirmed quota table (50 MiB app size, 30 s handler, 10 MiB request/response, 1,000 read RPS, 50 write RPS, 1 MB value, 8 KB key, 2 GB storage) with its doc URL; that runtime configuration is unavailable there; the deploy commands (`spin plugin install aka`, `spin aka login`, `spin build`, `spin aka deploy --variable ...`) and the variables that must be set (`public_base_urls` pointing at the real app URL — with a pointer to the existing silent-fallback warning — `cookie_secure=true`, `admin_bootstrap_password`); the backup → deploy → restore upgrade path and why it needs no file conversion; and the two operating ceilings this deploy surfaces (a click costs two KV writes, so ~25 sustained redirects/second against the 50 write RPS cap; and a full-cap 5,000-entry restore needs ~100 s of writes and cannot complete inside one 30 s request).
- [ ] Mark the 2026-07-18 KV-consolidation Future-work entry scheduled — file(s): TASKS.md — done when: the `- [ ] Consolidate redirect/api from three named KV stores...` line under `## Future work (not scheduled)` (today line 303) carries a trailing `— **SCHEDULED 2026-08-04**` note naming docs/plans/kv-store-consolidation.md and recording that the prefixing-view approach was chosen over flattening, in the same style as the closure notes already appended to entries above it; no other existing TASKS.md line is modified.
- [ ] Confirm Akamai's KV rate limits and app-size semantics with Akamai (EXTERNAL DEPENDENCY — may block the deploy task) — file(s): (none — external confirmation, recorded in CLAUDE.md) — done when: Akamai has confirmed in writing whether the 50 write RPS / 1,000 read RPS defaults apply to this account and what an increase would require, and whether the 50 MiB app-size limit is compressed and/or per component; the answers are recorded in CLAUDE.md's Akamai section replacing the current "no concrete numbers" wording; if Akamai declines to confirm, that is recorded too and the deploy proceeds with the published defaults treated as hard.
- [ ] Measure the built app size against the 50 MiB cap (must precede the deploy) — file(s): (none — measurement step) — done when: `spin build` has been run from the repo root and the total of `redirect/main.wasm`, `api/app.wasm`, `gui-pages/app.wasm`, the fetched `spin_static_fs.wasm` and `gui/`'s files is recorded with per-artifact byte counts; if the total exceeds 50 MiB the finding is reported with the largest artifact named and `wasm-opt -Oz`/debug-section stripping evaluated on the two componentize-py artifacts BEFORE any structural change is proposed — merging gui-pages into api is explicitly not the answer.
- [ ] Deploy to Akamai Functions (depends on the cutover, the size measurement and, if it blocks, the rate-limit confirmation) — file(s): (none — deploy step) — done when: `spin plugin install aka`, `spin aka login`, `spin build` and `spin aka deploy` complete, with `admin_bootstrap_password`, `cookie_secure=true` and `public_base_urls=https://<app-id>.fwf.app` supplied via `--variable`; the deploy prints an app URL; whether a `secret = true` variable can be supplied through `--variable` is recorded either way; and `curl -si https://<app-id>.fwf.app/login.html` returns 200 with the CSP header present.
- [ ] End-to-end manual verification of the consolidation on the local app — file(s): (none — verification step) — done when: every step in docs/plans/kv-store-consolidation.md's Verification section 1-8 is executed against a live `SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false spin up --build --runtime-config-file runtime-config.toml` in a real browser with the console open and zero errors of any kind, in particular zero CSP violations, in both light and dark themes — covering login, single and bulk link create, a 302 at `/r/<slug>`, a password-protected link's prompt and correct-password redirect, per-day and recent-events analytics, a QR code, user create/delete including the 409 owns-links refusal, a backup → destroy → restore round trip whose links resolve afterwards, the consistency report at `ok: true`, and the URL-policy violations report; and `cd api && uv run pytest` (474), `cd gui-pages && uv run pytest` (71) and `cd redirect && go test ./linkgate/...` all pass with the counts recorded.
- [ ] Set Cache-Control: no-store on every redirect-component response — file(s): redirect/main.go, redirect/passwordgate.go — done when: `setSecurityHeaders` in redirect/main.go sets `Cache-Control: no-store` alongside the four existing security headers, so it covers the 302s, the 404s and the prompt page uniformly rather than being repeated at the three redirect call sites; the comment there records that the 302 is load-bearing and must never become a 301 or 308 (Akamai does not cache 302/307 by default but DOES cache 301/308 — techdocs.akamai.com/property-mgr/docs/cache-http-redirects), that a cached redirect would break the status check, the time window, deletion, repointing and the destination-policy bulk-Disable remediation path, and that a cached 404 would stop a not-yet-active link ever starting; the now-redundant `Cache-Control` line at redirect/passwordgate.go:21 is removed with a comment saying it moved; NO linkgate test is added (this is a `package main` header write and is not host-testable — verified live instead); `cd redirect && go test ./linkgate/...` still passes; and under a live `spin up --build`, `curl -sI http://localhost:3000/r/<slug>` shows `HTTP/1.1 302` with `cache-control: no-store`, `curl -sI http://localhost:3000/r/<nonexistent>` shows `404` with the same header, and a protected link's prompt page still shows exactly one `cache-control: no-store`.
- [ ] Document the redirect caching constraint in CLAUDE.md — file(s): CLAUDE.md — done when: the "Security response headers" section (or a short peer subsection) records that every `redirect` response now carries `Cache-Control: no-store`; that **302 is a hard requirement and 301/308 are forbidden** for `/r/{slug}`, because Akamai edge servers do not cache 302/307 by default but do cache 301/308 by default, with the doc URL; that origin `Cache-Control` is **not honoured by default** at the Akamai edge, so the header is defence-in-depth for browsers and proxies while the 302 status is the actual edge-level control; that a cached redirect would break the `status` check, the `[start_at, end_at)` window, deletion, repointing and the destination-policy remediation path, and a cached 404 would stop a not-yet-active link ever starting; and that Akamai Functions publishes no caching documentation at all, so `*.fwf.app` behaviour is unverified until the deployed cache check runs — with the result of that check recorded here once it has.
- [ ] Verify no edge caching of redirects on the deployed app (depends on the deploy; complements the deployed smoke verification) — file(s): (none — verification step) — done when: against `https://<app-id>.fwf.app`, the same `/r/<slug>` is requested 5 times and the link's analytics total increments by exactly 5 (proving every request reached the origin and `recordAnalytics` ran); `curl -sI` on that URL is recorded verbatim including any `X-Cache`, `X-Cache-Key`, `X-Check-Cacheable` or `Age` header observed, together with the `cache-control` the origin sent; a link is disabled through the dashboard and the same URL returns 404 on the very next request with no delay; and the finding — whether a `*.fwf.app` app inherits Property Manager's "302 not cached by default" behaviour — is written into CLAUDE.md, replacing the UNCONFIRMED note.
- [ ] End-to-end smoke verification of the deployed Akamai app (depends on the deploy) — file(s): (none — verification step) — done when: against `https://<app-id>.fwf.app`, steps 10-13 of the plan's Verification section are executed and recorded: the bootstrap admin signs in over HTTPS with `cookie_secure=true` and the session cookie is stored; a link is created and returns 302 at `/r/<slug>` with the click reflected in its analytics; `GET /api/admin/consistency` returns 200 with `ok: true` and all twelve checks — CONFIRMING that `get_keys` works on Akamai's KV host, which is the make-or-break unknown for backup, restore, violations and user deletion; a small backup downloads and restores successfully with the post-restore bootstrap login working; a QR code encodes the real deployed base URL and not localhost; and the measured wall-clock time of the backup and restore requests is recorded against the 30 s handler limit.
```

## Critical files

- `api/kvprefix.py` (new)
- `api/tests/test_kvprefix.py` (new)
- `api/tests/test_store_isolation.py` (new)
- `api/tests/fixtures/backup-pre-consolidation.json` (new)
- `redirect/linkgate/keys.go` (new)
- `redirect/linkgate/keys_test.go` (new)
- `api/app.py`
- `redirect/main.go`
- `redirect/passwordgate.go`
- `spin.toml`
- `runtime-config.toml`
- `dev/kv-explorer.toml`
- `gui-pages/tests/test_manifest_components.py`
- `CLAUDE.md`
- `TASKS.md`
- `docs/plans/kv-store-consolidation.md` (new — this file)

(`redirect/passwordgate.go` only loses its now-redundant `Cache-Control` line;
`redirect/prompt.html` and its CSP are unchanged.)

Not touched, and deliberately so: `api/links.py`, `auth.py`, `users.py`,
`bulk.py`, `backup.py`, `consistency.py`, `urlpolicy.py`, `analytics.py`,
`qr.py`, `domains.py`, `responses.py`, `tags.py`, `api/tests/fakes.py`, all
nine existing `api/tests/test_*.py` modules, every file under `gui/`,
`gui-pages/routing.py`, `Jenkinsfile`, `README.md`, `PRODUCT.md`, `DESIGN.md`.

## Verification

In execution order. Steps 1–8 are local; 9–13 are the deploy.

1. **Unit suites, before the cutover** — after the `kvprefix.py` and `keys.go`
   tasks:
   ```bash
   cd api && uv run pytest
   cd redirect && go test ./linkgate/...
   ```
   `api` should be 474 + the new `test_kvprefix.py` and
   `test_store_isolation.py` cases; Go should be `ok` with the new
   `keys_test.go`.

2. **Full suites, after the cutover and the manifest task** — all three must
   pass, with the api count at 474 plus the new tests and `gui-pages` back at
   71:
   ```bash
   cd api && uv run pytest
   cd gui-pages && uv run pytest
   cd redirect && go test ./linkgate/...
   ```
   Never `go test ./...` — it fails by design on `package main`
   (`wit_exports.go:934:6: missing function body`).

3. **Static checks on the cutover diff:**
   ```bash
   grep -c "key_value.open" api/app.py          # expect 1
   grep -n "_kv_keys" api/app.py                # expect only the def and the one wrap
   grep -n "kv.Open" redirect/main.go           # expect 3, all "default"
   grep -n "fmt" redirect/main.go               # expect 0
   grep -n "key_value_stores" spin.toml         # expect two lines, both ["default"]
   grep -c kv-explorer spin.toml                # expect 0
   ```

4. **Boot the app** (the `--runtime-config-file` flag is now optional but is
   kept in the documented command):
   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```
   Expect no KV error at startup and a successful `POST /api/auth/login` as the
   bootstrap admin — which also proves `ensure_bootstrap_admin` wrote
   `users:user:admin` and `users:_meta:bootstrapped` through the view.

5. **The cross-component path, in a browser** (this is the one thing no unit
   test can cover — the Go and Python prefix constants agreeing at runtime):
   create a link in the dashboard, open `/r/<slug>`, expect a 302 to the
   destination, then open the link's detail page and confirm the click appears
   in both the per-day table and recent events. A prefix mismatch shows up here
   as a `404` at `/r/<slug>` for a link the dashboard lists — the exact failure
   this step exists to catch.

   In the same step, confirm the cache headers:
   ```bash
   curl -sI http://localhost:3000/r/<slug>          # 302 + cache-control: no-store
   curl -sI http://localhost:3000/r/does-not-exist  # 404 + cache-control: no-store
   curl -sI http://localhost:3000/r/<protected>     # 200 prompt, exactly one cache-control
   ```
   The status line must read `302`, never `301` or `308` — see "Edge caching and
   the never-cache invariant".

6. **The rest of the surface**, in a real browser with the console open, zero
   errors and zero CSP violations, both themes: bulk create; a
   password-protected link (prompt renders, correct password redirects, wrong
   password re-prompts); a QR code; create a user, give them a link, confirm
   `DELETE /api/users/<u>` returns `409 user_owns_links`, reassign, then delete
   successfully.

7. **Backup, destroy, restore** — the highest-value single check, because it
   exercises `get_keys`, the scoped filter, the prune and the write ordering at
   once:
   ```bash
   curl -s -b "session=<admin>" http://localhost:3000/api/admin/backup > /tmp/b.json
   ```
   Confirm the file's `stores` object has the three **logical** names and
   **unprefixed** keys. Delete every link and the extra user through the GUI,
   restore the file, confirm `signed_out: true`, sign in again as the bootstrap
   admin **without restarting `spin up`**, and confirm every link resolves at
   `/r/<slug>`. Then `curl -s -b ... "…/api/admin/backup?stores=users"` and
   confirm base64-decoding every value yields no `password_hash` — **do not
   grep the raw file**, every value is base64 and the grep both misses real
   material and false-positives on the `excluded` array (`CLAUDE.md`'s backup
   section explains why this exact mistake happened before).

8. **Consistency and violations:**
   ```bash
   curl -s -b "session=<admin>" http://localhost:3000/api/admin/consistency
   curl -s -b "session=<admin>" http://localhost:3000/api/admin/url-policy/violations
   ```
   Expect `ok: true`, all twelve checks present, `stores_scanned:
   ["links","users"]`, and **`unrecognized_key` count `0`** — with analytics
   keys sitting in the same physical store, a non-zero count here means the
   prefix filter is not being applied.

9. **Measure the app size** before attempting the deploy:
   ```bash
   spin build
   ls -l redirect/main.wasm api/app.wasm gui-pages/app.wasm
   du -sh gui/
   ```
   Sum against the 50 MiB cap and record the numbers.

10. **Deploy:**
    ```bash
    spin plugin install aka
    spin aka login
    spin build
    spin aka deploy \
      --variable admin_bootstrap_password=<pw> \
      --variable cookie_secure=true \
      --variable public_base_urls=https://<app-id>.fwf.app
    ```
    A pass is a printed app URL and `curl -si https://<app-id>.fwf.app/login.html`
    returning 200 with the CSP header.

11. **`get_keys` on Akamai — do this first after the deploy**, because
    everything operator-facing depends on it:
    ```bash
    curl -s -b "session=<admin>" https://<app-id>.fwf.app/api/admin/consistency
    ```
    A pass is 200 with all twelve checks. A 500 or a trap here means Akamai's
    KV host does not implement key enumeration, which blocks backup, restore,
    violations and user deletion and needs a plan of its own — report and stop
    rather than working around it.

12. **Deployed smoke test:** sign in over HTTPS (confirm the session cookie is
    stored with `Secure` set — `cookie_secure=true` this time, unlike local);
    create a link; hit `/r/<slug>` and confirm the 302 and the recorded click;
    open the detail page and confirm the QR code encodes
    `https://<app-id>.fwf.app/r/<slug>` and **not** `localhost` (a `localhost`
    QR means `public_base_urls` was not set — `CLAUDE.md` records that this
    fails silently).

13. **Deployed backup round trip at small scale**, timing both requests
    against the 30 s handler limit: download a backup, restore it, sign back in
    as the bootstrap admin. Record the wall-clock times — these are the numbers
    any future decision about `MAX_BACKUP_ENTRIES` on Akamai must be made
    against.

14. **Prove the edge is not caching redirects** — the empirical answer to the
    unconfirmed Functions-caching question, and the check that keeps the
    never-cache invariant honest:
    ```bash
    for i in 1 2 3 4 5; do curl -sI https://<app-id>.fwf.app/r/<slug>; done
    ```
    A pass is: the link's analytics total increments by **exactly 5** (every
    request reached the origin and `recordAnalytics` ran); the status is `302`
    on each; `cache-control: no-store` is present. Record verbatim any
    `X-Cache`, `X-Cache-Key`, `X-Check-Cacheable` or `Age` header seen — those
    are the edge's own account of what it did. Then disable the link in the
    dashboard and confirm the very next request returns `404` with no delay. If
    the count under-increments or the disabled link keeps redirecting, stop:
    that is a cached redirect, and it invalidates the status check, the time
    window, deletion, repointing and the destination-policy remediation path at
    once. Write the outcome into `CLAUDE.md` in place of the UNCONFIRMED note.

## Out of scope / follow-ups

To be added under `TASKS.md`'s existing `## Future work (not scheduled)`:

- **Chunked or resumable backup/restore for Akamai's 30 s handler limit.** At
  50 write RPS a full-cap 5,000-entry restore needs ~100 s and cannot complete
  in one request. Trigger: a deployment whose real data exceeds what one
  request moves inside 30 s — measure with Verification step 13's numbers
  before designing anything.
- **Cache the physical key enumeration for the lifetime of a request.** Would
  turn a backup's three full walks into one. Must not be done without first
  handling `backup.handle_restore`'s post-write `list_keys` call, which uses a
  *fresh* enumeration to compute the prune set; see rejected alternative 5.
- **A 13th consistency check for physical keys under no known prefix.** Such a
  key is invisible to every view today and is pruned by the next restore.
  Trigger: an unprefixed key actually appearing in a deployment.
- **Reduce the redirect hot path from two KV data operations to one** by
  dropping the `Exists` in `lookupLink` and treating a `nil` `Get` as absent,
  and pass the already-open store into `recordAnalytics` to save one `Open` per
  recorded click. Deliberately excluded here so that any regression in this
  change is attributable. Worth real timing evidence on Akamai first.
- **Ask Akamai for a KV write-rate increase** if sustained redirect throughput
  is expected to exceed ~25/second (two writes per recorded click against the
  50 write RPS cap). **Edge-caching the 302 is not an acceptable substitute**
  — see the next entry.
- **Do not reach for edge caching as the throughput fix.** If the ~25
  redirects/second write ceiling ever binds, caching `/r/{slug}` at the edge is
  the obvious-looking mitigation and is not available without first solving
  invalidation for disable, delete, repoint and window expiry — a cached
  redirect makes every one of those silently ineffective, and there is no
  purge hook anywhere in this app today. Recorded so a future reader does not
  spend the afternoon rediscovering it.

Not planned at all, and not follow-ups: any change to the backup file format,
any per-endpoint behaviour change, any new consistency check id, any change to
how CI invokes the suites.
