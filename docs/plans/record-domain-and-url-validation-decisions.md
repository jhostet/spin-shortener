# Plan: Record two future-work decisions in TASKS.md

## Context

You raised two related ideas while testing the GUI: (1) checking that a
destination URL is "valid" before/without blocking link creation, and (2) an
admin-managed domain feature. Your answers clarified both:

- The domain feature is actually **two things combined**: multi-domain
  short-link hosting (the shortener servable under more than one base domain,
  with permission-gated users choosing which domain(s) a new link publishes
  under) **and** a destination allow/deny-list (restricting what `target_url`
  values are permitted, independent of which domain the link itself lives
  under).
- Domain access per user should be a **new, separate `assigned_domains` list
  field** on the user record — not a new dynamically-generated permission
  string — because `auth.py`'s `KNOWN_PERMISSIONS` is deliberately a small,
  fixed, hardcoded vocabulary ("reject anything outside this set rather than
  silently accepting typos"), and extending it at runtime per registered
  domain would break that design.
- The URL-reachability check idea is **rejected**, not deferred: neither
  `redirect` nor `api` has any `allowed_outbound_hosts` entries (confirmed
  empty/absent in `spin.toml` for both), so neither component can make the
  check server-side. A browser-side check is limited to a `no-cors` opaque
  `fetch`, which can only detect a hard DNS/connection failure — it can never
  distinguish a real `200` from a `404`/`500` across origins. That's not
  enough signal to justify building it, especially since it could never be
  allowed to block submission anyway (legitimate sites may reject bot-like
  probing).
- You want these recorded as **two separate entries**, not one combined
  bullet.

This plan makes no code changes — it only adds documentation to `TASKS.md`,
per your request to record a future task (and the rejected idea) before doing
anything else.

## Approach

Add two entries to `TASKS.md`:

1. **A new unchecked (`[ ]`) task** under the existing "Future work (not
   scheduled)" section — the multi-domain hosting + destination allow/deny-list
   feature, written at the same conceptual level as the section's existing
   entries (e.g. the Akamai KV consolidation, SAML/OIDC bullets) — enough
   detail to resume work later without re-deriving the decisions above, but
   not a full implementation spec. It will name the likely touched files
   (`spin.toml`, `api/users.py`, `api/links.py`, `api/app.py`,
   `gui/dashboard.html`, a new `gui/admin/domains.html`) and call out:
   - Replacing the single hardcoded `public_base_url` Spin variable with a
     KV-backed domain registry the admin manages (register/enable/disable).
   - A separate admin-managed destination allow/deny-list for `target_url`.
   - The `assigned_domains` per-user field decision (parallel to, but
     independent of, `permissions`).
   - Each link record gaining a `domain` field (which domain it was
     published under), used for the displayed short URL and QR code instead
     of the single global `public_base_url`.
   - The create-link form showing a domain multi-select (checkboxes) only
     when the user has any assigned domains.
   - One explicitly *open* design question left for whenever this is picked
     up: whether `redirect`'s hot path should validate the incoming `Host`
     header against a link's `domain` (since `/r/{slug}` resolution is
     currently Host-header-agnostic) — not resolved now, deliberately.

2. **A new small "Considered and rejected" section**, placed right before
   "Future work (not scheduled)", containing one `[x]` entry (the *decision*
   is done, even though the *feature* was rejected) documenting the
   URL-reachability check idea and exactly why it was rejected, so a future
   session doesn't re-propose it without this context. This is kept out of
   "Future work" since that section is specifically for genuinely deferred
   (still-desired) items, and this one isn't.

## Files to change

- `TASKS.md` only — no other files touched, no code changes.

## Verification

Documentation-only change: re-read the two new `TASKS.md` entries for
accuracy against this plan and the conversation before considering it done.
No build/test run is needed since no code changes.
