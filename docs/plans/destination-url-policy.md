# Destination URL Policy

## Context

Today `api/links.py:130`'s `is_valid_target_url` is the *entire* destination
check:

```python
def is_valid_target_url(target_url: str) -> bool:
    parsed = urlparse(target_url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)
```

Any authenticated account can shorten anything. The specific harm is that **a
short link launders its destination**: a recipient sees `go.example.com/r/promo`
and cannot tell where it leads, so a bad destination borrows the organization's
credibility. That is a different problem from the slug-hygiene one
`docs/plans/banned-word-slugs.md` looked at (and declined to implement) — the
slug is visible to the recipient; the destination is not.

This is the surviving half of the 2026-07-18 `TASKS.md` entry "Multi-domain
short-link hosting + admin-managed destination domain allow/deny-list". The
multi-domain half shipped 2026-08-02 as `docs/plans/multi-domain-display.md`;
the open entry is the one under `## Future work (not scheduled)` reading
"Admin-managed destination-URL domain allow/deny-list", which says in terms:
*"it needs its own storage decision … `docs/plans/banned-word-slugs.md`'s
segment-versus-substring analysis is the closest prior art for the matching-rule
question. Re-confirm scope before starting."* This plan is that re-confirmation.

**Confirmed decisions (settled by the requester before planning):**

- Enforcement must cover `links.handle_create`, `links.handle_update` (where
  `target_url` is updatable) **and** `bulk.handle_bulk_create`. "A policy
  enforced in two of three places is not enforced."
- Bulk stays all-or-nothing: one violating row means nothing is written and
  every problem is reported.
- **The `redirect` hot path must not change.** No extra KV read per click.
  Enforcement is authoring-time, in `api/`.
- The new KV key type carries **two** obligations, not one: `api/backup.py`
  (`INDEX_KEYS`/`restore_write_order`) **and** `api/consistency.py`'s key-shape
  recognition.
- Pure logic, host-testable: zero WASI SDK imports, `store` as a plain
  parameter, `Request`/`Response` from `api/responses.py`, `FakeStore` in tests.
- GUI: zero inline code anywhere. A new page needs an exact `spin.toml` route
  for its script, a `gui-pages/routing.py` `ROUTES` entry and a
  `test_routing.py` case.
- Error bodies carry their own values, matching `too_many_rows`/`max_tags`.
- Tasks sequenced API → GUI → docs, each independently landable, with a
  dedicated task that proves all three bypass paths are closed.
- The open `## Future work (not scheduled)` entry gets marked resolved (as a
  builder task — the planner is append-only on `TASKS.md`).

## Key technical facts confirmed during research

- **Baseline, measured on `main` at the start of this plan:** `cd api && uv run
  pytest` → **396 passed**; `cd gui-pages && uv run pytest` → **64 passed**;
  `cd redirect && go test ./linkgate/...` → `ok`.
- **`urlparse(...).hostname` is the right extractor, and `.netloc` is a
  security bug.** Verified by running against the repo's own interpreter (`cd
  api && uv run python -c ...`):

  | input | `.hostname` | `.netloc` |
  |---|---|---|
  | `https://example.com@evil.com/x` | `evil.com` | `example.com@evil.com` |
  | `https://EVIL.com:8080/x` | `evil.com` | `EVIL.com:8080` |
  | `http://localhost:3000/a` | `localhost` | `localhost:3000` |
  | `https://[::1]:8080/x` | `::1` | `[::1]:8080` |
  | `https://exämple.com/` | `exämple.com` | `exämple.com` |
  | `https://evil.com.` | `evil.com.` | `evil.com.` |

  `.hostname` strips userinfo, strips the port, strips IPv6 brackets and
  lowercases ASCII. It does **not** strip a trailing dot and does **not**
  IDNA-encode. Both of those are handled explicitly below.
- **`'notexample.com'.endswith('.example.com')` is `False`; `'evil.example.com'
  .endswith('.example.com')` is `True`** — confirmed in the same run. The
  leading `.` in the suffix is the entire correctness of subdomain matching.
- **`redirect/main.go` never reads a destination policy and does exactly one KV
  read for resolution** — `grep -n "kv.Open\|\.Get(" redirect/main.go` shows
  `kv.Open("links")` then `store.Exists("slug:"+slug)` / `store.Get("slug:"+slug)`
  in `resolveLink`, plus a separate `kv.Open("analytics")` for the write-side
  `recordAnalytics`. Nothing validates `target_url` at resolution time.
- **`api/backup.py` needs no new logic for this key, only a pinning test.**
  `build_backup` copies every key in every store verbatim; `is_excluded_key`
  returns `False` for every store except `users` (`api/backup.py:84-89`);
  `validate_backup` accepts any key in `links`; and `restore_write_order`
  classifies anything that is not `all_links` and does not start with
  `owner_links:` as a **non-index** key, written first — which is correct for a
  record. This mirrors the finding `docs/plans/link-tags-and-ownership.md` made
  for `tags` ("a test in `api/tests/test_backup.py` pins that today's tags
  round-trip needs no such change"). The obligation is discharged by a test plus
  a comment, not by new branching — and the plan says so explicitly so nobody
  reads its absence as an oversight.
- **`api/consistency.py` genuinely does need a code change.** Its `links`-store
  loop (`api/consistency.py:143-171`) has exactly three recognized shapes —
  `all_links`, `slug:`, `owner_links:` — and an `else` that appends to
  `unrecognized`. An unregistered `_meta:url_policy` would be reported as
  `unrecognized_key` on every single run forever. The `users` loop already
  carries the pattern to copy: `elif key == BOOTSTRAPPED_KEY: continue  # known
  shape; carries no content any check needs` (`api/consistency.py:189-190`).
- **`validate_bulk_rows` is pure and synchronous and takes no `store`**
  (`api/bulk.py:118`), receiving `existing_slugs` and `can_custom_slug` as
  precomputed parameters. The policy follows that same shape rather than making
  the function async.
- **`gui-pages`'s test count is derived, not listed.** `test_no_inline_code.py`
  builds `PAGES` from `routing.ROUTES.values()` (4 parametrized tests per page)
  and `SCRIPTS` from `GUI_DIR.rglob("*.js")` minus `vendor/` (2 per script).
  Current split, from `uv run pytest --collect-only -q`: `test_no_inline_code.py`
  43, `test_routing.py` 19, `test_manifest_components.py` 2 = 64. Adding one
  page and one script adds 4 + 2 + 1 (a `test_resolve_file` parametrize case) =
  7. **Expect `gui-pages` to go 64 → 71.** `api` grows by however many tests the
  new suites add; no existing `api` test should change count, though several in
  `test_bulk.py` change *call signature* (see below).
- **`test_manifest_components.py` asserts the component set, not the route
  count** (`EXPECTED_COMMITTED_COMPONENTS = {"redirect","api","gui","gui-pages"}`).
  Adding a `gui` trigger route does not touch it.
- **HTML pages need no `spin.toml` route.** `gui/admin/backup.html` has none —
  it is served by the `gui-pages` catch-all via `ROUTES`. Only the sibling
  `.js` needs an exact `gui` route (`spin.toml:118-120`).
- **The nav is full and this is not a nav item.** `DESIGN.md:247` records a
  fifth nav item being built, measured (`scrollWidth` 716 vs `clientWidth` 700
  at 768px on `links/detail.html`), and reverted, with "**Treat the next nav
  addition as a redesign, not an insertion.**" The established alternative is an
  in-body anchor under `admin/users.html`'s "Users" heading
  (`gui/admin/users.html:54`).
- **`gui/app.js` has no `api.put`.** `app.js:52-57` defines `get`, `post`,
  `patch` and `delete` only. The new page needs one added, in the same shape, so
  the request still goes through `apiFetch` and gets the `x-csrf-token` header.
- **UNCONFIRMED — IDNA/punycode behaviour under componentize-py.** `str.encode
  ("idna")` pulls in `encodings.idna`, which imports `stringprep` and
  `unicodedata` (a C extension). Whether it is available in the WASI CPython
  componentize-py bundles is unverified, and this plan deliberately does not
  depend on it (see "Non-ASCII hosts" below). Confirming it would take a
  build-and-run spike of the kind `docs/plans/kv-backup-restore-scratch.md`
  Round 1 used for `get_keys`. Do not add an IDNA dependency without that spike.

## The retroactive question — the decision

**What happens to links that already exist and violate a newly-added rule?**

**Decision: report them, change nothing.** A new read-only endpoint,
`GET /api/admin/url-policy/violations`, walks the `links` store and lists every
live link whose destination the current policy would refuse. It performs no
writes. Remediation is the operator's, through tools that already exist: the
dashboard's multi-select **Disable** or **Delete** bulk actions.

The four options, and why the other three lost:

**1. No retroactive effect at all** (the `banned-word-slugs.md` posture —
"Effect on existing links: None, and this is a guarantee"). Attractive: it is
free, it is the smallest possible change, and it has real precedent in this
repo. Rejected because the two situations are not analogous. A banned word in a
slug is visible in the short URL itself, so a legacy one is self-evident to
anyone who looks at the dashboard; a bad *destination* is exactly the thing
nobody can see. And the harm here is ongoing, not historical: the link is still
being clicked. "We have a policy, and we have no idea whether anything already
violates it" is not a policy. Note that this option is not fully absent from the
design anyway — see the guarantee below.

**2. Report them (chosen).** It is the only option that closes the "someone
already created the bad link" gap without a mutation the operator did not ask
for, and it is the posture this repo has now taken twice under pressure:
`docs/plans/kv-backup-restore.md` "already refused to build a silent repairer",
and `GET /api/admin/consistency` "reports; it never repairs — there is no
`?fix=`". A report is also the only option that lets the operator **look before
acting**: the violations list is the preview that options 3 and 4 do not have.
Its cost is honest and stated on the page: nothing is protected until a human
acts on the report.

**3. Disable them automatically** when a rule is added. Attractive, and it was
live: it is the only option where adding a rule actually *does* something
immediately, and disabling is reversible (unlike deleting), so the blast radius
is recoverable. Rejected on three counts. (a) It makes a configuration edit into
an unbounded, unpreviewable bulk mutation of link records — with no CAS and no
transaction in Spin KV, a partial failure leaves an arbitrary subset disabled
and no record of which. (b) The operator already has one-click bulk Disable, so
the automation saves two clicks and costs the preview. (c) It breaks third
parties who are relying on links that were legitimate when they were created,
with no human in the loop deciding that the trade was worth it. **Bulk-disabling
violators remains exactly the recommended remediation — it is just the
operator's action, taken after reading the list, not a side effect of saving a
rule.**

**4. Delete them.** Rejected without much argument: it destroys link records and
their slugs irrecoverably (analytics keys survive as orphans, making the mess
worse), it is not undoable, and it is strictly dominated by option 3, which
achieves the same "the link no longer works" outcome reversibly. `redirect`
already 404s a `disabled` link, so deletion buys nothing over disabling.

**One guarantee, carried over from option 1 and load-bearing:** a link that
already violates the policy **stays fully editable**. `handle_update` runs the
policy check **only inside the `if "target_url" in payload:` branch**, never on
a PATCH that leaves the destination alone. Without that, an operator could not
disable, retag, reschedule or reassign a legacy violator — which would break the
very remediation path this decision depends on. A regression test pins it.

### Why this is a separate surface, not a thirteenth consistency check

`GET /api/admin/consistency` was the obvious host and it was live; it lost on
three points, set out in full as rejected alternative #4 below — in short,
`consistency.py` is scoped to *structural* drift while a policy violation is a
coherent store whose rule changed; folding one in would set `ok: false` forever
for a structurally flawless deployment; and consistency findings deliberately
have no GUI repair path while policy violations should point straight at one.

The violations walk therefore uses `_kv_keys` and reads `slug:` records
directly, **not** `all_links` — the same "walk the keys, don't trust the index"
reasoning `consistency.py` established, so a link that has drifted out of the
index is still checked.

## Rule model and matching semantics

### Allow-list, deny-list, or both — both, in one list

There is one rule list. Each rule carries an `action` of `"allow"` or `"deny"`,
and the policy carries one `default_action` of `"allow"` or `"deny"`.

**Precedence, complete, in one sentence: a deny rule always wins; otherwise an
allow rule (or a `default_action` of `allow`) lets it through; otherwise it is
blocked.**

```
if any deny rule matches host:      BLOCK   reason = "denied_by_rule"
elif default_action == "allow":     ALLOW   reason = "allowed_by_default"
elif any allow rule matches host:   ALLOW   reason = "allowed_by_rule"
else:                               BLOCK   reason = "not_allowed_by_default"
```

That covers all three real configurations with no ordering semantics and no
specificity ranking:

| operator wants | `default_action` | rules |
|---|---|---|
| block a few known-bad destinations | `allow` | `deny` rules |
| only our own properties | `deny` | `allow` rules |
| our properties, minus one bad subdomain | `deny` | `allow example.com`, `deny bad.example.com` |

**Deny-wins was chosen over "most specific match wins" deliberately: the failure
direction.** Under specificity ranking an `allow evil.example.com` silently
defeats a `deny example.com` — **fail-open**. Deny-wins fails closed. The one
thing this model cannot do is carve an allowed exception *out of* a denied tree;
the workaround, stated in the UI, is to invert to `default_action: "deny"` and
enumerate. Full argument as rejected alternative #5 below.

### Host matching

**A rule is a bare host, and it matches that host and every subdomain of it.
Nothing else.**

```python
def host_matches(host: str, rule_host: str) -> bool:
    """`rule_host` matches `host` exactly, or any subdomain of it.

    The leading "." in the suffix is the whole correctness of this function:
    a bare host.endswith(rule_host) would match "notexample.com" against a
    rule for "example.com". Confirmed: "notexample.com".endswith(".example.com")
    is False; "evil.example.com".endswith(".example.com") is True.
    """
    return host == rule_host or host.endswith("." + rule_host)
```

So, spelled out because getting it backwards is the whole ballgame — for a rule
`example.com`:

| destination host | matches? |
|---|---|
| `example.com` | yes |
| `www.example.com` | yes |
| **`evil.example.com`** | **yes** |
| `a.b.example.com` | yes |
| `notexample.com` | **no** |
| `example.com.evil.net` | **no** |
| `example.co` | no |

**There is no per-rule `include_subdomains` toggle**, deliberately: one
semantic, always the same, nothing for an operator to pick wrong. The
consequence is a sharp edge worth stating in the UI copy — an allow rule for a
shared-subdomain provider (`github.io`, `blogspot.com`, `s3.amazonaws.com`)
allows anyone's content there. The repair is a deny rule for the specific
subdomain, which wins.

**Scheme, port and path are never part of a rule.** Scheme is already
constrained to `http`/`https` by the untouched `is_valid_target_url`. Port is
stripped before matching, so `https://evil.com:8443/x` matches a rule for
`evil.com`. Path is not matched at all — **the host is the unit of trust.** Both
omissions are real decisions; see rejected alternative #7.

### Extracting the destination host

```python
def destination_host(target_url: str) -> str | None:
    """The lowercased, port-free, trailing-dot-free host of `target_url`,
    or None if there isn't one."""
    try:
        host = urlparse(target_url).hostname
    except ValueError:
        return None
    if not host:
        return None
    return host.rstrip(".").lower() or None
```

Three things this must do, each with a test:

1. **Use `.hostname`, never `.netloc`.** `https://example.com@evil.com/x` has
   netloc `example.com@evil.com` and hostname `evil.com`. Matching on netloc
   would let a userinfo prefix spoof an allow rule — a real, classic bypass.
2. **Strip the trailing dot.** `https://evil.com./x` has hostname `evil.com.`,
   which does **not** match a rule for `evil.com`. Confirmed above. This is the
   cheapest real bypass of the whole feature and must be pinned.
3. **Return `None` for a hostless URL.** `https://user@/path` passes today's
   `is_valid_target_url` (netloc `user@` is truthy) but has no host.

### Non-ASCII hosts — a disclosed limitation, not a hole

`urlparse` does not IDNA-encode, and rule hosts are restricted to ASCII LDH (see
`normalize_rule_host`), so a destination with a Unicode host **matches no rule**.
The consequence follows the mode the operator chose, which is the coherent
outcome:

- `default_action: "deny"` → the Unicode host matches no allow rule → **blocked**
  (`not_allowed_by_default`). Fail-closed. Correct.
- `default_action: "allow"` → no deny rule matches → **allowed**. Fail-open: a
  determined user can evade a deny rule by typing the Unicode form. That is
  inherent to deny-lists (so is picking a domain that isn't on the list), and
  belongs in the same category as `banned-word-slugs.md`'s "a carelessness
  guard, not a content-moderation system, and it cannot stop a determined
  insider." State it in `CLAUDE.md` in those words. Closing it properly needs
  IDNA, UNCONFIRMED under componentize-py — see "Out of scope".

### Rule normalization

`normalize_rule_host(value) -> str | None` is permissive on input and strict on
storage — the same posture `api/domains.py:normalize_base_url` takes for base
URLs, but a separate function, because that one *requires* a scheme and a bare
host rule has none.

Accepted and normalized: `EVIL.com`, `  evil.com  `, `*.evil.com`,
`https://evil.com/path?x=1#y`, `evil.com.`, `evil.com:8443` → all become
`evil.com`.

Rejected (`None`): empty/whitespace-only; anything longer than
`MAX_RULE_HOST_LENGTH = 253`; anything that after the above stripping does not
match `^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$`. That
last regex is what rejects non-ASCII, embedded whitespace, a leftover `/`, and a
leading/trailing hyphen. It deliberately **does not require a dot**, so
`localhost` is a valid rule (it is a valid destination in dev).

## Data model

**One key, `_meta:url_policy`, in the `links` store.**

- **`links`, not a fourth store:** a new store means a `spin.toml` change, a
  `runtime-config.toml` change, and one more namespace for the Akamai
  single-`"default"`-store consolidation to collapse. The policy is about links.
- **`_meta:` prefix:** matches the existing `_meta:usernames` /
  `_meta:bootstrapped` convention in the `users` store, and cannot collide with
  `slug:`, `owner_links:` or `all_links`.
- **One key for the whole document, not one key per rule:** the policy is read
  on every create/update/bulk-create, so it must be **one** KV read, not N.
  There is no index, therefore no index/record drift, therefore nothing new for
  `consistency.py` to cross-check beyond recognizing the key. Rule counts are in
  the tens; whole-document writes are trivially cheap.
- **This does hand the local KV explorer read/write access to the policy**
  (it holds `links` + `analytics`). That is consistent with it already having
  full CRUD over every link record, and it is local-dev-only. No change needed.

```json
{
  "version": 1,
  "default_action": "allow",
  "rules": [
    {
      "host": "evil.example",
      "action": "deny",
      "note": "reported phishing",
      "created_at": "2026-08-04T10:00:00Z",
      "created_by": "alice"
    }
  ],
  "updated_at": "2026-08-04T10:00:00Z",
  "updated_by": "alice"
}
```

**An absent key means no policy, and no policy means everything is allowed** —
so every existing deployment and every fresh store behaves byte-for-byte as
today, with no migration and no backfill. `load_policy` returns
`EMPTY_POLICY = {"version": 1, "default_action": "allow", "rules": []}` when the
key is absent or unparseable. (Unparseable → treated as absent rather than
raising, for the same reason `consistency.collect` never raises on bad data: a
malformed policy must not turn every link creation into a `500`. The
consistency report is what surfaces it — see below.)

`is_active(policy)` is `policy["default_action"] == "deny" or bool(policy["rules"])`.
When inactive, `evaluate` returns allowed immediately with reason `"no_policy"`.

**Caps** — plain module constants in `api/urlpolicy.py`, not Spin variables, on
the same reasoning `MAX_BULK_ROWS` and `MAX_FINDINGS_PER_CHECK` carry (one
function in one component reads each; they are safety rails, not operator
policy):

| constant | value | error |
|---|---|---|
| `MAX_POLICY_RULES` | `200` | `400 {"error": "too_many_rules", "max_rules": 200, "rule_count": n}` |
| `MAX_RULE_HOST_LENGTH` | `253` | folded into `invalid_rule_host` |
| `MAX_RULE_NOTE_LENGTH` | `200` | `400 {"error": "invalid_rule_note", "max_length": 200}` |
| `MAX_POLICY_BODY_BYTES` | `65_536` | `413 {"error": "body_too_large", "max_bytes": 65536}` |

### The two mandatory key-type obligations

**1. `api/consistency.py` — a real code change.** Add
`URL_POLICY_KEY = "_meta:url_policy"  # == urlpolicy.POLICY_KEY` alongside the
existing prefix constants, and a branch in the `links`-store loop, placed before
the `else` that appends to `unrecognized`, mirroring the `users` loop's
`BOOTSTRAPPED_KEY` handling:

```python
        elif key == URL_POLICY_KEY:
            # Known shape. Parsed only far enough to report a corrupted
            # policy as unreadable_value — no new check id, and no field of
            # it is needed by any of the twelve.
            raw = await links_store.get(key)
            if raw is not None and _parse_policy(raw) is None:
                unreadable.append({"store": "links", "key": key})
```

Without this, every consistency report in every deployment reports
`unrecognized_key` forever. With it, a corrupted policy surfaces through the
existing `unreadable_value` check (#11) — no thirteenth check, no change to
`CHECKS`, no change to the report's shape or to `ok`'s meaning.

**2. `api/backup.py` — a pinning test and a comment, no new logic.** Verified
above: `build_backup` copies it, `is_excluded_key` is `users`-only,
`validate_backup` accepts it, and `restore_write_order` already classifies it as
a **non-index** key (written before indexes), which is correct — it is a record.
The obligation is discharged by `api/tests/test_backup.py` pinning
(a) `restore_write_order("links", [...])` puts `_meta:url_policy` in the
non-index group, and (b) a full export → `validate_backup` → restore round-trip
returns the policy byte-identical. That mirrors exactly what
`docs/plans/link-tags-and-ownership.md` did for `tags`. **If a future change
ever adds a second policy key or an index over rules, this stops being true and
`INDEX_KEYS` must be revisited.**

## API changes

### New module: `api/urlpolicy.py`

Zero `spin_sdk` imports; `store` and `list_keys` are plain parameters;
`Response`/`json_response`/`iso_now` come from `responses`. `api/domains.py`,
`api/tags.py` and `api/consistency.py` are the models. It imports nothing from
`links.py`, so `links.py` can import it with no cycle — the same rule
`api/tags.py`'s docstring states.

```python
POLICY_KEY = "_meta:url_policy"          # in the LINKS store
POLICY_SCHEMA_VERSION = 1
ACTIONS = ("allow", "deny")
EMPTY_POLICY = {"version": 1, "default_action": "allow", "rules": []}
MAX_POLICY_RULES = 200
MAX_RULE_HOST_LENGTH = 253
MAX_RULE_NOTE_LENGTH = 200
MAX_POLICY_BODY_BYTES = 65_536
MAX_VIOLATIONS = 100
VIOLATIONS_FORMAT = "spin-shortener-url-policy-violations"

# --- pure ---
def normalize_rule_host(value: str) -> str | None
def destination_host(target_url: str) -> str | None
def host_matches(host: str, rule_host: str) -> bool
def is_active(policy: dict) -> bool
def evaluate(target_url: str, policy: dict) -> dict
def parse_policy_document(value, *, now: str, actor: str) -> tuple[dict | None, dict | None]

# --- store I/O, store passed in ---
async def load_policy(store) -> dict
async def save_policy(store, policy: dict) -> None

# --- handlers ---
async def handle_get_policy(store, principal) -> Response
async def handle_put_policy(store, principal, request) -> Response
async def handle_violations(store, principal, list_keys) -> Response
```

**`evaluate` is the single source of truth for the decision, and both the
enforcement error body and the violations report are built from its return
value** — so the two can never disagree about why something was blocked. It
returns a fixed-shape dict, every key always present:

```python
{"allowed": bool, "host": str | None, "reason": str, "matched_rule": str | None}
```

`reason` is one of `"no_policy"`, `"allowed_by_default"`, `"allowed_by_rule"`,
`"denied_by_rule"`, `"not_allowed_by_default"`, `"unparsable_target_url"`.

`unparsable_target_url` (a URL that clears `is_valid_target_url` but yields no
host) is **allowed when the policy is inactive and blocked whenever it is
active** — preserving "no policy configured behaves exactly as today" while
being fail-closed the moment a policy exists.

`parse_policy_document` is all-or-nothing and returns `(policy, None)` or
`(None, error_body)` — the same shape as `tags.parse_tags` and
`backup.parse_stores_param`. It normalizes every rule host, de-duplicates on
`(host, action)`, preserves `created_at`/`created_by` for rules already present
by host+action and stamps `now`/`actor` on new ones, and sorts rules by host
then action so two saves of the same set are byte-identical. Error bodies:

| body | when |
|---|---|
| `{"error": "invalid_policy"}` | not an object, or `rules` not a list |
| `{"error": "invalid_default_action", "allowed_actions": ["allow","deny"]}` | bad `default_action` |
| `{"error": "invalid_rule_action", "host": h, "allowed_actions": ["allow","deny"]}` | bad rule `action` |
| `{"error": "invalid_rule_host", "host": <as submitted>, "max_length": 253}` | `normalize_rule_host` returned `None` |
| `{"error": "invalid_rule_note", "host": h, "max_length": 200}` | note not a string, or too long |
| `{"error": "too_many_rules", "max_rules": 200, "rule_count": n}` | over the cap |

### Enforcement point 1 — `api/links.py`

`import urlpolicy` alongside `import auth` / `import tags`.

**`handle_create`** — one added block, immediately after the existing
`is_valid_target_url` check (`api/links.py:186-188`) and **before** slug
allocation, so a rejected destination never consumes a slug:

```python
    policy = await urlpolicy.load_policy(store)
    verdict = urlpolicy.evaluate(target_url, policy)
    if not verdict["allowed"]:
        return json_response(400, {
            "error": "destination_not_allowed",
            "host": verdict["host"],
            "reason": verdict["reason"],
            "matched_rule": verdict["matched_rule"],
        })
```

**`handle_update`** — the identical block, **inside the
`if "target_url" in payload:` branch only** (`api/links.py:283-287`), after the
format check and before `record["target_url"] = target_url`. Putting it outside
that branch would make every legacy violator uneditable and undisable-able,
destroying the remediation path the whole retroactive decision rests on. A test
named for that fact pins it.

Cost: one extra KV read per single-link create, and per update that changes the
destination. Not the hot path.

### Enforcement point 2 — `api/bulk.py`

`validate_bulk_rows` gains a **fourth required positional parameter**, no
default:

```python
def validate_bulk_rows(
    rows: list[BulkRow],
    existing_slugs: set[str],
    can_custom_slug: bool,
    policy: dict,
) -> list[dict]:
```

**No default value, deliberately.** A `policy=None` default meaning "no policy"
is exactly how the third enforcement path stays silently open forever. Making it
required means the compiler-of-last-resort (a `TypeError` at the call site) and
every existing test in `api/tests/test_bulk.py` force the decision into the
open. Those tests are updated to pass `urlpolicy.EMPTY_POLICY`; that is a
mechanical change and is expected, not a surprise.

Per-row placement in the precedence chain: **immediately after the
`is_valid_target_url` branch and before the slug checks** (`api/bulk.py:131-134`)
— it is a destination problem, so it is reported alongside the other destination
problems:

```python
        if error_code is None:
            verdict = urlpolicy.evaluate(row.target_url, policy)
            if not verdict["allowed"]:
                errors.append({
                    "line": row.line, "slug": row.slug,
                    "error": "destination_not_allowed",
                    "host": verdict["host"], "reason": verdict["reason"],
                })
                continue
```

The row-error shape stays `{"line", "slug", "error", ...}` with extra keys, which
the existing `first_line` on `duplicate_slug_in_submission` already establishes
and which `dashboard.js`'s `renderBulkErrorTable` already tolerates (it reads
only `line`, `slug` and `error`).

`handle_bulk_create` loads the policy **once** for the whole batch, next to the
existing `existing = set(await links._all_slugs(store))` line
(`api/bulk.py:197`), and passes it in. One KV read per submission, not per row.

### The other two write paths, explicitly

- **`bulk.handle_bulk_action`** — `delete`/`enable`/`disable`/`tag`/`untag`/
  `reassign` never touch `target_url`. No check, and none is wanted: `disable` is
  the remediation.
- **`backup.handle_restore`** — writes raw records and is **deliberately
  exempt**. A restore is a faithful replacement of a snapshot, not an authoring
  action; policy-checking it would make a backup un-restorable after a rule
  change, which is a far worse failure than a legacy violator that the
  violations report will list on the next run anyway. Say this in `CLAUDE.md`.

### Does an admin bypass the policy? No.

Enforcement applies to every principal, `role == "admin"` included. An admin who
wants an exception edits the policy — one action, stamped with `updated_by`.
See rejected alternative #12.

### Endpoints

All three gate on **`users.manage`**, returning the exact body every other admin
surface returns: `json_response(403, {"error": "forbidden", "required_permission": "users.manage"})`.

| method + path | handler | notes |
|---|---|---|
| `GET /api/admin/url-policy` | `handle_get_policy` | returns the stored document, or `EMPTY_POLICY` when absent |
| `PUT /api/admin/url-policy` | `handle_put_policy` | whole-document replace; `413` over `MAX_POLICY_BODY_BYTES`; `400` per `parse_policy_document`; returns the saved document |
| `GET /api/admin/url-policy/violations` | `handle_violations` | read-only walk; never writes |

**Whole-document `PUT`, not per-rule `POST`/`DELETE`.** Spin KV has no
compare-and-swap, so a per-rule endpoint is a read-modify-write race either way;
one handler beats three; and it matches the established "`PATCH {"tags": [...]}`
is a full replacement, not a merge" precedent. The cost — two admins editing
concurrently, last write wins, one loses their edit with no warning — is
disclosed, not fixed. See "Out of scope".

**Why `users.manage` and not a new `links.policy` permission.** `KNOWN_PERMISSIONS`
(`api/auth.py:37-39`) is a deliberately small fixed vocabulary; an addition is
cheap but permanent. `users.manage` is already the bar for every other admin
surface and is not the weaker one (a holder can self-promote via
`users.handle_update`). See rejected alternative #11 for the trigger to revisit.

### Violations report shape

```json
{
  "format": "spin-shortener-url-policy-violations",
  "schema_version": 1,
  "generated_at": "2026-08-04T10:00:00Z",
  "generated_by": "alice",
  "policy_default_action": "deny",
  "rule_count": 4,
  "scanned": {"links": 412},
  "count": 2,
  "truncated": false,
  "max_violations": 100,
  "violations": [
    {
      "slug": "promo",
      "owner": "bob",
      "status": "active",
      "target_url": "https://evil.example/x",
      "host": "evil.example",
      "reason": "denied_by_rule",
      "matched_rule": "evil.example"
    }
  ]
}
```

- Sorted by `slug` for a diffable report across runs, the same reason
  `consistency._finding_sort_key` exists.
- **`MAX_VIOLATIONS = 100` caps `violations`; `count` stays exact and
  `truncated` is set** — the `MAX_FINDINGS_PER_CHECK` contract, verbatim,
  including the GUI's "Showing the first N of M".
- A record that will not parse is skipped and counted in `scanned`, never
  raised on — `consistency.collect`'s rule ("a diagnostic that 500s on a broken
  store fails exactly when it is needed").
- `target_url` is included because the operator cannot judge without it. It is
  already visible to any `links.view_all` holder, and this endpoint is behind
  `users.manage`.
- When the policy is inactive the report is a valid empty one (`count: 0`,
  `rule_count: 0`), not an error.

### `api/app.py` wiring

`import urlpolicy` in the alphabetical block. Three exact-path branches
immediately after the `/api/admin/consistency` branch (`api/app.py:202-213`),
each reaching through the existing `_require_session` (which enforces CSRF on
the `PUT` automatically, and is a no-op on the two `GET`s). Only the `links`
store is opened; `users` is already open.

```python
        if path == "/api/admin/url-policy/violations" and method == "GET":
            ...  links_store; urlpolicy.handle_violations(links_store, result, _kv_keys)

        if path == "/api/admin/url-policy" and method in ("GET", "PUT"):
            ...  links_store; handle_get_policy / handle_put_policy
```

List the `/violations` branch first for readability; with `==` comparisons the
order is not load-bearing.

## GUI changes

### New page: `gui/admin/url-policy.html` + `gui/admin/url-policy.js`

**Why a new page rather than a fourth article on `gui/admin/backup.html`.** The
backup page is *operator maintenance* — three destructive-or-diagnostic
one-shot actions. This is *standing configuration* an admin returns to and
edits, and PRODUCT.md principle 5 ("Keep admin visually and functionally
distinct from everyday link-creation workflows") reads onto the distinction
between "recover the system" and "set the rules". A rules editor with an add
form, a table, per-row removal and a Save is also simply too much content to
bolt under a Restore control the page already warns can't be undone.

**Has the deferred rename trigger fired?** `TASKS.md` carries "Renaming
`gui/admin/backup.html` to something naming operator maintenance generally —
Trigger: a fourth operator tool landing there." **No — and this plan is the
reason it has not.** Nothing lands on `backup.html`; the page keeps its three
articles and its name. Leave that Future-work entry open and untouched.

Reached the same way `backup.html` is: an in-body anchor on
`gui/admin/users.html`, per `DESIGN.md:247`. Change `users.html:54` from a bare
anchor to two, separated by a middot, under the existing "Users" heading — no
new class, no new token:

```html
        <p><a href="backup.html">Backup and restore</a> · <a href="url-policy.html">Destination URL policy</a></p>
```

The way back is the nav's existing "Manage users" link, which
`initHeader({manageUsersHref: "users.html"})` renders — exactly what `backup.js`
does.

**Page structure**, following `backup.html` line for line (same `<head>`, same
`#app-header`, same `#forbidden-notice` + `#admin-content` gate, same
`initHeader(...).then(...)` `canManage` check at the bottom of the `.js`, same
`../app.js` + sibling script pair). No new `.css` file: `.form-error`,
`.form-success`, Pico `<article>`, `<table>`, `<select>` and the `hidden`
attribute cover everything. **No new design token.**

1. **`<h1>Destination URL policy</h1>`**, `#forbidden-notice` reading "You don't
   have permission to manage the destination URL policy."
2. **Article — "How destinations are checked."** Static copy stating the
   precedence rule and the subdomain rule in the plainest possible words,
   including the worked example: *"A rule for `example.com` also covers
   `shop.example.com` and `evil.example.com` — but not `notexample.com`."* Plus
   the two sharp edges: an allow rule for a shared-hosting domain allows
   everyone on it, and an exception cannot be carved out of a denied tree
   (switch the default to Block and enumerate instead).
3. **Article — "Rules."** `<select id="default-action">` (Allow / Block) with a
   live sentence beneath it describing what the current selection means; a
   `<table id="rules-table">` of Host / Action / Note / Added / (Remove); an
   add-rule row (`#rule-host`, `#rule-action`, `#rule-note`, `#rule-add`); a
   `#policy-save` button, `#policy-error`, `#policy-success`. **Edits are staged
   client-side and committed by Save**, matching the whole-document `PUT` and
   letting the operator see the whole change before it lands. Save calls
   `api.put`, **which does not exist yet** — `gui/app.js:52-57`'s `api` object
   has `get`/`post`/`patch`/`delete` only. Add one line beside `patch`, in the
   identical shape, rather than hand-rolling a `fetch` (which would bypass
   `apiFetch`'s CSRF header). Save uses the existing `confirmDialog` from
   `app.js` **only** when
   the pending change sets `default_action` to `deny`, since that is the one
   edit that can block everyone at once; the dialog names the allow-rule count.
4. **Article — "Existing links that don't match."** `#violations-btn`,
   `#violations-error`, `#violations-result`. Copy states plainly:
   **nothing here has been changed or disabled — these links still work.**
   Renders a table of slug (an `<a href="../links/detail.html?slug=…">`), owner,
   status, host, reason; "Showing the first N of M" when `truncated`; a
   `.form-success` all-clear naming the scanned count when `count` is 0. A
   closing sentence points at the dashboard's bulk Disable/Delete as the fix.
   Every interpolated value goes through `escapeHtml`, as
   `backup.js:renderConsistencyFindings` does.
5. A local `POLICY_ERROR_MESSAGES` map, following the
   `BACKUP_ERROR_MESSAGES`/`BULK_ROW_MESSAGES` precedent (kept local because
   these codes matter only on this page), and a `VIOLATION_REASONS` map from
   `reason` to human copy with a raw-value fallback, following
   `consistencyCheckLabel`'s shape.

### `spin.toml` and `gui-pages`

- `spin.toml`: one new exact route in the "Per-page scripts and styles" block,
  next to `/admin/backup.js`:
  ```toml
  [[trigger.http]]
  route = "/admin/url-policy.js"
  component = "gui"
  ```
  **Exact, never a wildcard** — the confirmed `spin_static_fs` gotcha. Without
  this route the page renders fully and does nothing, silently.
- `gui-pages/routing.py`: `"/admin/url-policy.html": "admin/url-policy.html"` in
  `ROUTES`.
- `gui-pages/tests/test_routing.py`: one parametrize case
  `("/admin/url-policy.html", "admin/url-policy.html")`. The 6 new
  `test_no_inline_code.py` tests appear automatically (`PAGES` and `SCRIPTS` are
  derived). **64 → 71.**
- No HTML route is needed in `spin.toml` — the `gui-pages` catch-all serves it.

### `gui/app.js` and `gui/dashboard.js` — the error copy

- `gui/app.js`'s shared `ERROR_MESSAGES` (`app.js:148`) gains one entry:
  ```js
  destination_not_allowed: "That destination isn't allowed by this site's URL policy.",
  ```
- The two single-link call sites — the create form (`dashboard.js:~890`) and the
  edit/save path (`dashboard.js:~482`) — append the server-supplied host when
  present, so the user learns *which* host was refused rather than being told to
  guess:
  ```js
  const msg = friendlyError(data, "Could not create link.", { invalid_password: "…" });
  errorEl.textContent = data && data.host ? `${msg} (${data.host})` : msg;
  ```
  **The host comes from the response, never from a client-side re-parse** — the
  same "the client hardcodes nothing" rule `too_many_rows` follows.
- `dashboard.js`'s `BULK_ROW_MESSAGES` (`dashboard.js:743`) gains
  `destination_not_allowed: "This destination isn't allowed by the site's URL policy."`
  The row already shows the offending line, so the host is not repeated there.

## Redirect (Go) changes

**None. `redirect/` is not touched at all**, and the plan costs the alternative
rather than merely asserting it:

Resolution-time enforcement would add a second KV read (`_meta:url_policy`) to
every `/r/{slug}` click — a **100% increase in the hot path's resolution KV
round trips**, which is exactly one `slug:` read today (confirmed above). It
would also require reimplementing `normalize_rule_host`, `destination_host` and
`host_matches` in Go, creating a second, independently-maintained copy of the one
rule the entire feature turns on — the drift class that `analytics_event_slots`
needed a shared Spin variable to avoid, and that was for a single integer, not a
matching algorithm. And it buys nothing the chosen design does not: a legacy
violator is *reported* and one bulk Disable away from 404ing through the `status`
check `redirect` already performs.

`linkgate.Link` gains no field. No `.wasm` in `redirect/` needs rebuilding for
this feature (though `spin up --build` will rebuild everything anyway).

## Trade-offs and rejected alternatives

1. **Disabling existing violators automatically when a rule is added** —
   rejected. Live and genuinely attractive (reversible, and it makes saving a
   rule actually *do* something). Lost because it turns a config edit into an
   unpreviewable bulk mutation with no CAS and no transaction, saves the
   operator two clicks over the bulk Disable they already have, and breaks third
   parties with no human deciding it was worth it.
2. **Deleting existing violators** — rejected. Irrecoverable, leaves orphan
   analytics keys, and strictly dominated by disabling, which produces the same
   404.
3. **No retroactive effect at all** (the `banned-word-slugs.md` posture) —
   rejected. A bad slug is visible; a bad destination is the thing nobody can
   see, and the harm is ongoing rather than historical. Its one good idea — that
   legacy links stay *editable* — is kept as an explicit guarantee.
4. **A thirteenth check on `GET /api/admin/consistency`** — rejected. That
   endpoint is scoped to structural drift (index vs. record); a policy violation
   is a coherent store whose *rule* changed. It would flip `ok` to false forever
   for a structurally perfect deployment, its "run it twice" guidance is false
   for a stable finding, and its "we never repair" framing is wrong for a report
   whose whole point is to send you to the bulk actions.
5. **"Most specific match wins" precedence** — rejected in favour of
   deny-always-wins. More expressive (it is the only way to carve an allowed
   exception out of a denied tree), and it is what DNS and cookies do. Lost on
   failure direction: under specificity, `allow evil.example.com` silently
   defeats `deny example.com` — **fail-open**. Deny-wins fails closed. Revisit
   only if the exception-inside-a-denied-tree case turns up for real and
   inverting the default is genuinely unworkable.
6. **A per-rule `include_subdomains` toggle** — rejected. One always-on semantic
   means nothing for an operator to pick wrong, and the over-broad-allow case it
   would solve is already repairable with a deny rule, which wins.
7. **Path and/or port matching in a rule** — rejected. Path precision is
   illusory (`..`, percent-encoding, `;` params, fragments) and a port rule is
   bypassed by using the default port. The host is the unit of trust.
8. **Two separate lists (an allow-list and a deny-list) as distinct fields** —
   rejected. One list with a per-rule `action` and one `default_action` covers
   every configuration, has a precedence rule that fits in a sentence, and makes
   "which list is authoritative" a question that cannot be asked.
9. **A Spin variable instead of a KV key** — rejected. `public_base_urls` is a
   Spin variable because a display domain needs DNS and routing anyway, so it is
   already deploy-time configuration. A destination policy has no such coupling
   and must be editable by an admin at runtime without a rebuild and redeploy —
   which is precisely the cost `banned-word-slugs.md` accepted for its
   compiled-in word list and flagged as its main liability.
10. **One KV key per rule** — rejected. It would turn one read into N on every
    single link creation, and it would introduce an index (and therefore
    index/record drift, and therefore real new work in `consistency.py`) for a
    list of tens of entries.
11. **A new `links.policy` permission** — rejected in favour of `users.manage`.
    `KNOWN_PERMISSIONS` is a small fixed vocabulary and an addition is cheap but
    permanent; `users.manage` is already the bar for every other admin surface
    and is not the weaker one (a holder can self-promote via
    `users.handle_update`). Trigger to revisit: a real request for a
    compliance/abuse role that is deliberately *not* an account administrator.
12. **Letting admins bypass enforcement** — rejected. An admin who needs an
    exception edits the policy, which is one action and leaves an `updated_by`
    trail; a silent bypass leaves none and makes bulk enforcement conditional.
13. **Per-rule `POST`/`DELETE` endpoints instead of a whole-document `PUT`** —
    rejected. No CAS exists either way, one handler beats three, and full
    replacement matches the `PATCH {"tags": […]}` precedent.
14. **A fourth article on `gui/admin/backup.html`** — rejected. Cheapest option
    and it fires no new routes, but it conflates standing policy configuration
    with destructive operator maintenance, and a rules editor is too much
    content for that page. It would also fire the deferred backup-page rename
    trigger, which this plan explicitly leaves unfired.
15. **A fifth nav item** — not even attempted. `DESIGN.md:247` measured this and
    reverted it: "Treat the next nav addition as a redesign, not an insertion."
16. **Blocking non-ASCII destination hosts outright** — rejected. It would break
    legitimate IDN destinations for every deployment to close a deny-list
    evasion that deny-lists are inherently open to anyway. Disclosed instead.
17. **Policy-checking `backup.handle_restore`** — rejected. A restore is a
    faithful replacement, not authoring; checking it would make a backup
    un-restorable after a rule change.
18. **Doing nothing** — live, and rejected. The status quo is that any account
    can point the organization's short domain at anything, with no way to
    prevent it and no way to find out afterwards. `is_valid_target_url` checking
    only `scheme in ("http","https") and bool(netloc)` is the whole of today's
    control.

## Tasks

The lines below were appended to `TASKS.md` under a new
`## Destination URL policy` heading at the end of the file. `TASKS.md` is
authoritative; the builder ticks boxes only there.

```
- [ ] Add api/urlpolicy.py — the pure rule model, matching and evaluation — file(s): api/urlpolicy.py (new), api/tests/test_urlpolicy.py (new) — done when: `cd api && grep -c spin_sdk urlpolicy.py` → 0 and the module imports nothing from links.py; it exposes POLICY_KEY = "_meta:url_policy", EMPTY_POLICY, ACTIONS, MAX_POLICY_RULES = 200, MAX_RULE_HOST_LENGTH = 253, MAX_RULE_NOTE_LENGTH = 200, MAX_POLICY_BODY_BYTES = 65536, MAX_VIOLATIONS = 100, plus normalize_rule_host / destination_host / host_matches / is_active / evaluate / parse_policy_document / load_policy / save_policy; `cd api && uv run pytest` passes with tests pinning each of: a rule for `example.com` matches `example.com`, `www.example.com`, `evil.example.com` and `a.b.example.com` but NOT `notexample.com` or `example.com.evil.net`; `destination_host` uses .hostname not .netloc so `https://example.com@evil.com/x` yields `evil.com`; a trailing dot is stripped so `https://evil.com./x` still matches a rule for `evil.com`; a port is ignored; `evaluate` returns allowed with reason "no_policy" for EMPTY_POLICY; a deny rule beats an allow rule for the same host in BOTH default_action modes; default_action "deny" with no matching allow rule blocks with reason "not_allowed_by_default"; a hostless-but-scheme-valid URL (`https://user@/path`) is allowed when the policy is inactive and blocked with reason "unparsable_target_url" when it is active; a non-ASCII host matches no rule (blocked under default deny, allowed under default allow); normalize_rule_host maps `EVIL.com`, `*.evil.com`, `https://evil.com/p?q`, `evil.com.` and `evil.com:8443` all to `evil.com` and returns None for empty, whitespace-containing, >253-char and non-ASCII input; and parse_policy_document returns each of the six documented error bodies with its own values embedded.
- [ ] Register _meta:url_policy with consistency.py and pin its backup.py round-trip (depends on api/urlpolicy.py; must land BEFORE the endpoints task, which is the first code that writes the key) — file(s): api/consistency.py, api/tests/test_consistency.py, api/tests/test_backup.py — done when: consistency.py gains `URL_POLICY_KEY = "_meta:url_policy"  # == urlpolicy.POLICY_KEY` and a branch in the links-store loop placed before the `else`, so a store containing the key reports `unrecognized_key` count 0 while a store containing a corrupted policy value reports it under the existing `unreadable_value` check — with CHECKS, the report shape and the meaning of `ok` all unchanged (still exactly twelve checks); a new test in test_consistency.py asserts both halves; AND api/tests/test_backup.py gains two tests pinning that `restore_write_order("links", [...])` places `_meta:url_policy` in the non-index group (before `all_links` and `owner_links:`) and that a policy value survives build_backup → validate_backup → restore byte-identical, with a comment recording that backup.py needs no new logic because is_excluded_key is users-only and restore_write_order already treats unknown links-store keys as records; `cd api && uv run pytest` passes.
- [ ] Enforce the policy in links.handle_create and links.handle_update — file(s): api/links.py, api/tests/test_links.py — done when: handle_create loads the policy once and rejects a disallowed destination with `400 {"error": "destination_not_allowed", "host", "reason", "matched_rule"}` built from evaluate()'s return value, placed after the is_valid_target_url check and BEFORE slug allocation so a rejected create consumes no slug (a test asserts the slug store is unchanged after a rejection); handle_update runs the identical check ONLY inside the `if "target_url" in payload:` branch; `cd api && uv run pytest` passes with a test named for the guarantee asserting that a link whose stored target_url violates the current policy still returns 200 from PATCH {"status": "disabled"} and from PATCH {"tags": [...]} — the remediation path must not be broken by the enforcement; plus a test that creating any link succeeds unchanged when no policy key exists at all.
- [ ] Enforce the policy in bulk.handle_bulk_create with a required parameter — file(s): api/bulk.py, api/tests/test_bulk.py — done when: `validate_bulk_rows(rows, existing_slugs, can_custom_slug, policy)` takes policy as a fourth REQUIRED positional parameter with no default (a default is how the third enforcement path stays silently open), every existing call site and test is updated to pass `urlpolicy.EMPTY_POLICY`, handle_bulk_create loads the policy exactly once per submission (not per row) next to the existing `_all_slugs` read, and a violating row yields `{"line", "slug", "error": "destination_not_allowed", "host", "reason"}` in row_errors positioned after the invalid_target_url branch and before the slug checks; `cd api && uv run pytest` passes with a test asserting a 3-row submission containing one violating row writes NOTHING (no slug: record, no index change) and reports every problem, and a test asserting the bulk path rejects a destination that the single-link path also rejects.
- [ ] Add the three /api/admin/url-policy endpoints and wire them into app.py (depends on the three tasks above) — file(s): api/urlpolicy.py, api/app.py, api/tests/test_urlpolicy_api.py (new) — done when: handle_get_policy / handle_put_policy / handle_violations all gate on users.manage returning the exact body `{"error": "forbidden", "required_permission": "users.manage"}`; PUT replaces the whole document, returns 413 `{"error": "body_too_large", "max_bytes": 65536}` over the cap and the parse_policy_document error bodies otherwise, and returns the saved document with updated_at/updated_by stamped; GET returns EMPTY_POLICY when the key is absent; handle_violations walks `slug:` records via the injected list_keys (NOT all_links), performs zero writes (a test asserts the FakeStore's contents are identical before and after), sorts by slug, caps `violations` at MAX_VIOLATIONS while leaving `count` exact and setting `truncated`, and returns `format: "spin-shortener-url-policy-violations"`; app.py gains `import urlpolicy` in the alphabetical block and two exact-path branches immediately after the /api/admin/consistency branch opening only the links store; `cd api && uv run pytest` passes, and against a live `spin up --build --runtime-config-file runtime-config.toml`, `curl -s -b "session=<admin>" http://localhost:3000/api/admin/url-policy` returns 200 with `default_action: "allow"` and `rules: []` on a fresh store, while the same request as a user without users.manage returns 403.
- [ ] Prove all three bypass paths are closed with one end-to-end test module (depends on every API task above) — file(s): api/tests/test_url_policy_enforcement.py (new) — done when: `cd api && uv run pytest` passes with a single module that, for EACH of default_action "allow" + a deny rule AND default_action "deny" + an allow rule, saves the policy through handle_put_policy and then asserts all three authoring paths reject the same violating destination with `destination_not_allowed` — links.handle_create, links.handle_update with a target_url change, and bulk.handle_bulk_create — and that each of the three wrote nothing; plus an admin-role principal is rejected identically (there is no admin bypass); plus bulk.handle_bulk_action with action "disable" on the pre-existing violating link still returns 200; plus a test asserting that removing the policy check from any one of the three handlers fails at least one assertion in this module (state the mutation used, in a comment, as test_backup.py's mutation notes do).
- [ ] Add the gui/admin/url-policy.html page, its script and its routes (depends on the endpoints) — file(s): gui/admin/url-policy.html (new), gui/admin/url-policy.js (new), gui/app.js, spin.toml, gui-pages/routing.py, gui-pages/tests/test_routing.py — done when: `gui/app.js`'s `api` object gains a `put` helper in the identical shape to the existing `patch` (it has get/post/patch/delete only today, and a hand-rolled fetch would bypass apiFetch's CSRF header); the page follows backup.html's structure exactly (same head, #app-header, #forbidden-notice + #admin-content gate, ../app.js + sibling script, initHeader({dashboardHref: "../dashboard.html", pageLabel: "Destination URL policy", manageUsersHref: "users.html"}).then(...) canManage check) and holds three articles — the precedence/subdomain explainer including the worked "a rule for example.com also covers evil.example.com but not notexample.com" example, the rules editor (#default-action select, #rules-table, add-rule row, #policy-save committing the whole document via a PUT, #policy-error, #policy-success, with app.js's confirmDialog required only when the pending change sets default_action to deny), and the violations article (#violations-btn, #violations-error, #violations-result) whose copy states plainly that nothing has been changed or disabled and that points at the dashboard's bulk Disable/Delete; NO new .css file and no new design token; spin.toml gains exactly one new exact route `/admin/url-policy.js` → gui in the per-page block; routing.py's ROUTES gains "/admin/url-policy.html"; test_routing.py gains the matching parametrize case; `cd gui-pages && uv run pytest` passes with the count moving 64 → 71 (4 new no-inline page tests + 2 new no-inline script tests + 1 routing case, all derived automatically).
- [ ] Surface destination_not_allowed in the dashboard and link the new page from the users page (depends on the page) — file(s): gui/app.js, gui/dashboard.js, gui/admin/users.html — done when: app.js's shared ERROR_MESSAGES gains `destination_not_allowed`; the single-link create and edit call sites in dashboard.js append the server-supplied `data.host` in parentheses when present (never a client-side re-parse of the URL); dashboard.js's BULK_ROW_MESSAGES gains its own destination_not_allowed entry; gui/admin/users.html's existing `<p><a href="backup.html">Backup and restore</a></p>` becomes that anchor plus a middot plus `<a href="url-policy.html">Destination URL policy</a>` with no new class and no new token; `cd gui-pages && uv run pytest` still passes (the no-inline-code guards cover both changed files).
- [ ] Document the destination URL policy in CLAUDE.md, PRODUCT.md and DESIGN.md (depends on every task above) — file(s): CLAUDE.md, PRODUCT.md, DESIGN.md — done when: CLAUDE.md gains a "Destination URL policy" section (peer to "KV consistency check") recording the `_meta:url_policy` key in the links store and that an absent key means everything is allowed with no migration; the exact precedence sentence (a deny rule always wins, then an allow rule or a default of allow, otherwise blocked) and that a rule matches the host and every subdomain of it with the evil.example.com worked example; that scheme, port and path are never matched and why; that enforcement covers create, update-with-target_url and bulk-create and deliberately NOT bulk-action or restore, and that there is no admin bypass; that a legacy violator stays editable and that PATCH must never re-check a target_url it isn't changing; that `redirect` is untouched and why resolution-time enforcement was costed and rejected; that the violations endpoint reports and never repairs and lives on its own page rather than as a thirteenth consistency check; the four caps as plain module constants; and the two disclosed limitations (a Unicode-host destination matches no rule, so it evades a deny-list; and a whole-document PUT means last-write-wins between two concurrent admins); CLAUDE.md's existing "a new KV key type obliges TWO changes" rule gains `_meta:url_policy` as its worked example with the note that backup.py needed a pinning test rather than new logic; PRODUCT.md's Capabilities list gains one bullet; DESIGN.md's nav bullet (the "The nav is full" paragraph) gains one sentence recording that a SECOND admin tool is now reached by an in-body anchor on admin/users.html rather than by a nav item, confirming the pattern; and `grep -c "public_base_url\b" CLAUDE.md` behaviour is unaffected.
- [ ] Mark the 2026-07-18 destination-URL allow/deny-list Future-work entry resolved — file(s): TASKS.md — done when: the `- [ ] Admin-managed destination-URL domain allow/deny-list` line under `## Future work (not scheduled)` is checked `[x]` and carries a trailing `— **RESOLVED 2026-08-04.** Shipped as docs/plans/destination-url-policy.md ...` note in the same style as the 2026-08-04 closure already appended to the "Multi-domain short-link hosting + admin-managed destination domain allow/deny-list" line above it, naming that the retroactive question was answered with "report, never mutate" and that the storage decision the entry left open was resolved as a single KV key rather than a Spin variable; no other existing line in TASKS.md is modified.
- [ ] End-to-end manual verification of the destination URL policy — file(s): (none — verification step) — done when: every numbered step in docs/plans/destination-url-policy.md's Verification section is executed against a real `spin up --build --runtime-config-file runtime-config.toml` in a browser with the console open and zero errors of any kind, in particular zero CSP violations, in both light and dark themes; a deny rule blocks create, update and bulk-create; an allow-list default blocks an unlisted destination through all three; a link created before the rule still resolves at /r/<slug>, appears in the violations report, remains editable, and 404s only after the operator bulk-disables it; the consistency report shows `unrecognized_key` count 0 with the policy key present; and a backup taken with a policy configured restores with the policy intact.
```

## Critical files

- `api/urlpolicy.py` **(new)**
- `api/tests/test_urlpolicy.py` **(new)**
- `api/tests/test_urlpolicy_api.py` **(new)**
- `api/tests/test_url_policy_enforcement.py` **(new)**
- `gui/admin/url-policy.html` **(new)**
- `gui/admin/url-policy.js` **(new)**
- `api/links.py`
- `api/bulk.py`
- `api/consistency.py`
- `api/app.py`
- `api/tests/test_links.py`
- `api/tests/test_bulk.py`
- `api/tests/test_consistency.py`
- `api/tests/test_backup.py`
- `gui/app.js`
- `gui/dashboard.js`
- `gui/admin/users.html`
- `gui-pages/routing.py`
- `gui-pages/tests/test_routing.py`
- `spin.toml`
- `CLAUDE.md`
- `PRODUCT.md`
- `DESIGN.md`
- `TASKS.md`

`api/backup.py` is deliberately **not** in this list: the obligation it carries
is discharged by tests in `api/tests/test_backup.py`, for the reason recorded
above. `Jenkinsfile` is not in scope — the three test commands are unchanged.

## Verification

Run in this order.

1. **Unit suites.**
   ```bash
   cd api && uv run pytest
   cd gui-pages && uv run pytest
   cd redirect && go test ./linkgate/...
   ```
   `api` must exceed its 396 baseline; `gui-pages` must be exactly **71** (64 +
   4 page + 2 script + 1 routing); `redirect` must be `ok` — never
   `go test ./...`, which fails by design.
2. **Purity guards.**
   ```bash
   cd api && grep -c spin_sdk urlpolicy.py          # → 0
   cd api && grep -n "^import links\|^from links" urlpolicy.py   # → no output
   grep -c kv-explorer spin.toml                     # → 0
   ```
3. **Start the app.**
   ```bash
   SPIN_VARIABLE_ADMIN_BOOTSTRAP_PASSWORD=<pw> SPIN_VARIABLE_COOKIE_SECURE=false \
     spin up --build --runtime-config-file runtime-config.toml
   ```
4. **Fresh store, no policy.** Sign in as the bootstrap admin. `curl -s -b
   "session=<token>" http://localhost:3000/api/admin/url-policy` → `200` with
   `"default_action": "allow"`, `"rules": []`. Create a link to
   `https://example.com/anything` from the dashboard — it succeeds exactly as
   before. **This is the no-migration guarantee.**
5. **Create the legacy violator now, before any rule exists.** Create a link to
   `https://evil.example/promo` and note its slug. Confirm `curl -sI
   http://localhost:3000/r/<slug>` returns `302`.
6. **Add a deny rule.** Open `http://localhost:3000/admin/url-policy.html` from
   the "Destination URL policy" link on `admin/users.html`. Add `evil.example`
   with action Block and a note; Save. Reload the page — the rule persists with
   its `Added by` stamp.
7. **All three authoring paths, deny-list mode.**
   - Create form: destination `https://evil.example/x` → refused, message names
     `evil.example`.
   - Also try `https://good.example.com@evil.example/x` (userinfo spoof) and
     `https://evil.example./x` (trailing dot) → **both refused**. These two are
     the bypasses; if either succeeds the matching is wrong.
   - Also try `https://sub.evil.example/x` → refused (subdomain).
   - Also try `https://notevil.example/x` → **allowed** (not a subdomain).
   - Edit an existing link's destination to `https://evil.example/x` → refused.
   - Bulk create with 3 rows, one of them `https://evil.example/x` → refused,
     nothing created, all problems listed. Reload the dashboard and confirm none
     of the three slugs exists.
8. **The retroactive guarantee.** The legacy link from step 5 still returns
   `302` at `/r/<slug>`. On the policy page, click **Run check** — it is listed
   with reason `denied_by_rule` and matched rule `evil.example`, and the copy
   says nothing has been changed. On the dashboard, PATCH it in a way that does
   not touch the destination (add a tag, change the schedule) → **200**. Then
   select it and use bulk **Disable** → `/r/<slug>` now returns `404`. Re-run
   the check: still listed (it exists and still violates), with
   `status: "disabled"`.
9. **Allow-list mode.** Set the default to Block; the confirmation dialog must
   appear and name the allow-rule count. Add `example.com` with action Allow;
   Save. Create a link to `https://shop.example.com/x` → allowed (subdomain).
   Create a link to `https://other.test/x` → refused with reason
   `not_allowed_by_default`. Add `bad.example.com` with action Block, Save, and
   create a link to `https://bad.example.com/x` → refused (**deny wins over the
   parent allow** — this is the precedence rule).
10. **Permission gate.** Sign in as a user with no `users.manage`.
    `http://localhost:3000/admin/url-policy.html` shows the forbidden notice and
    hides the content; `curl` against all three endpoints returns
    `403 {"error":"forbidden","required_permission":"users.manage"}`. Confirm
    that same user is still *subject to* the policy at create time.
11. **Consistency and backup interop.** With the policy configured, run the
    consistency check on `admin/backup.html` → `unrecognized_key` count **0**
    and the report is otherwise unchanged. Download a backup, restore it, sign
    back in, and confirm `GET /api/admin/url-policy` returns the same rules.
12. **Browser console, both themes.** Reload `admin/url-policy.html` in light
    and dark with the console open — **zero errors, in particular zero CSP
    violations**. Confirm the page has no inline code (`cd gui-pages && uv run
    pytest tests/test_no_inline_code.py` already covers it, but the console is
    what catches a missing `spin.toml` route: if `/admin/url-policy.js` 404s the
    page renders fully and does nothing).
13. **Nav overflow.** Measure `scrollWidth` vs `clientWidth` on `#app-header nav`
    at 1400/768/480/390px on the new page in both themes. The nav gained no item,
    so this should be clean — but `DESIGN.md` requires it measured, not assumed.

## Out of scope / follow-ups

Each of these belongs under `TASKS.md`'s `## Future work (not scheduled)` if and
when its trigger fires; none is being added there now, since none is a
commitment.

- **Optimistic-concurrency on the policy `PUT`** (echo the loaded `updated_at`,
  `409` on mismatch). Trigger: more than one person actually editing the policy.
  Today's behaviour is last-write-wins with no warning, disclosed in `CLAUDE.md`.
- **IDNA/punycode normalization of both rules and destination hosts**, closing
  the Unicode-host deny-list evasion. Blocked on a build-and-run spike
  confirming `encodings.idna`/`unicodedata` exist under componentize-py — the
  same shape of spike `docs/plans/kv-backup-restore-scratch.md` Round 1 used for
  `get_keys`. Do not add the dependency without it.
- **A per-rule `include_subdomains: false`**, and/or specificity-ranked
  precedence. Trigger: a real need to allow an exception inside a denied tree
  where inverting `default_action` is genuinely unworkable.
- **Path or URL-pattern rules.** Deliberately rejected above; would need a
  concrete case that host matching cannot serve.
- **A separate `links.policy` permission.** Trigger: a request for a
  compliance/abuse role that is deliberately not an account administrator.
- **Automatic disabling of violators**, as an *explicit operator action* on the
  violations page ("Disable all N") rather than a side effect of saving a rule.
  This is the only rejected mutation with a plausible future: it keeps the
  preview, which is what killed the automatic version. Trigger: a real report
  with more violators than a person wants to select by hand.
- **Renaming `gui/admin/backup.html`.** Its trigger — "a fourth operator tool
  landing there" — has **not** fired: this feature lands on its own page.
- **Seeding a starter rule list.** Deliberately none. The empty policy allows
  everything, so a fresh deployment needs no rules and gets today's behaviour;
  which hosts to block is a business judgement, exactly as
  `banned-word-slugs.md` argued about its word list, and belongs to the
  operator rather than to this repo.
