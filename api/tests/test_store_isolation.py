"""Pins the four cross-namespace hazards the prefixing view exists to prevent
— see docs/plans/kv-store-consolidation.md's "New: api/tests/test_store_isolation.py".

Each test builds ONE physical FakeStore holding keys from more than one
logical namespace, wraps it with kvprefix.open_views, and drives the real
backup.py/consistency.py handlers over the resulting views — never over the
physical store directly.
"""

import base64
import json

import auth
import analyticsorphans
import backup
import consistency
import kvprefix
import kvretry
import links
from responses import Request
from tests.fakes import FakeStore, fake_get_many, fake_list_keys


def _principal(username="admin", role="admin", permissions=None):
    return auth.Principal(
        username=username, role=role, permissions=permissions or ["users.manage"], csrf_token="x"
    )


async def test_backup_export_through_links_view_cannot_leak_a_users_password_hash():
    """backup.py's own guards (redact_user_value, is_excluded_key) match
    unprefixed key SHAPE (a `user:` key, a `session:` key) — they never see
    this data at all, since ?stores=links only opens the links view. This
    test would NOT have been caught by those guards; what catches it here is
    scoped_list_keys refusing to let the links view's enumeration return a
    users:-prefixed key in the first place. The mutation that breaks this
    test: replacing scoped_list_keys's filter with a pass-through (returning
    every physical key unfiltered to every view).
    """
    physical = FakeStore({
        "users:user:alice": json.dumps({
            "username": "alice",
            "password_hash": "pbkdf2_sha256$100000$somesalt$somehash",
            "role": "user",
        }).encode("utf-8"),
        "links:slug:a": json.dumps({"slug": "a", "target_url": "https://example.com", "owner": "admin"}).encode("utf-8"),
    })
    views = kvprefix.open_views(physical)
    list_keys = kvprefix.scoped_list_keys(fake_list_keys)

    response = await backup.handle_export(
        {"links": views["links"]}, _principal(), {"stores": ["links"]}, list_keys, num_event_slots=30,
    )

    assert response.status == 200
    doc = json.loads(response.body)

    # Per CLAUDE.md's backup section: every value is base64, and the
    # document's own "excluded" array documents "users/user:*#password_hash"
    # as literal text — a raw string search of the response body for
    # "password_hash" would always match that documentation string and
    # falsely "pass" even with a real leak. Decode every value actually
    # exported instead, exactly like the real verification step does.
    assert list(doc["stores"]["links"].keys()) == ["slug:a"]
    for value_b64 in doc["stores"]["links"].values():
        decoded = base64.b64decode(value_b64).decode("utf-8")
        assert "password_hash" not in decoded
        assert "pbkdf2_sha256" not in decoded


async def test_restore_prunes_only_within_its_own_prefix():
    """A restore of a links-only file must not touch a single users: or
    analytics: key, even though they share the same physical store."""
    physical = FakeStore({
        "links:slug:old": b"old-value",
        "links:all_links": b'["old"]',
        "users:user:alice": b"alice-value",
        "users:session:tok": b"session-value",
        "analytics:count:old": b"count-value",
    })
    views = kvprefix.open_views(physical)
    list_keys = kvprefix.scoped_list_keys(fake_list_keys)

    backup_doc = {
        "format": backup.BACKUP_FORMAT,
        "schema_version": backup.SCHEMA_VERSION,
        "created_at": "2026-08-04T00:00:00Z",
        "created_by": "admin",
        "fidelity": "full",
        "counts": {"links": 1},
        "stores": {
            "links": {"slug:new": base64.b64encode(b"new-value").decode("ascii")},
        },
    }
    request = Request(
        method="POST",
        uri="/api/admin/restore",
        headers={},
        body=json.dumps({"confirm": "REPLACE", "backup": backup_doc}).encode("utf-8"),
    )

    response = await backup.handle_restore(
        {"links": views["links"]}, _principal(), request, list_keys, num_event_slots=30,
    )
    assert response.status == 200

    assert await views["links"].exists("slug:old") is False
    assert await views["links"].get("slug:new") == b"new-value"
    assert physical._data["users:user:alice"] == b"alice-value"
    assert physical._data["users:session:tok"] == b"session-value"
    assert physical._data["analytics:count:old"] == b"count-value"


async def test_pre_consolidation_fixture_restores_unchanged_and_reexports_identically():
    """The documented upgrade path: a backup taken before this consolidation
    restores through the views into prefixed physical keys, and a fresh
    export over all three views reproduces the same `stores` object (modulo
    created_at/created_by)."""
    import pathlib

    fixture_path = pathlib.Path(__file__).resolve().parent / "fixtures" / "backup-pre-consolidation.json"
    fixture = json.loads(fixture_path.read_text())

    physical = FakeStore()
    views = kvprefix.open_views(physical)
    list_keys = kvprefix.scoped_list_keys(fake_list_keys)

    request = Request(
        method="POST",
        uri="/api/admin/restore",
        headers={},
        body=json.dumps({"confirm": "REPLACE", "backup": fixture}).encode("utf-8"),
    )
    response = await backup.handle_restore(
        {"links": views["links"], "users": views["users"], "analytics": views["analytics"]},
        _principal(), request, list_keys, num_event_slots=30,
    )
    assert response.status == 200

    for store_name, entries in fixture["stores"].items():
        prefix = kvprefix.STORE_PREFIXES[store_name]
        for key in entries:
            assert (prefix + key) in physical._data

    export_response = await backup.handle_export(
        {"links": views["links"], "users": views["users"], "analytics": views["analytics"]},
        _principal(), {}, list_keys, num_event_slots=30,
    )
    assert export_response.status == 200
    exported = json.loads(export_response.body)
    assert exported["stores"] == fixture["stores"]


async def test_consistency_report_does_not_see_analytics_keys_sharing_the_physical_store():
    """analytics keys are never opened by the consistency check even though
    they physically share the same "default" store — a non-zero
    unrecognized_key count here would mean the prefix filter isn't being
    applied, and it would fire on every healthy deployment forever."""
    physical = FakeStore({
        "links:slug:a": json.dumps({"slug": "a", "target_url": "https://example.com", "owner": "admin"}).encode("utf-8"),
        "links:all_links": b'["a"]',
        "links:owner_links:admin": b'["a"]',
        "users:user:admin": b'{"username": "admin"}',
        "users:_meta:usernames": b'["admin"]',
        "analytics:count:a": b'{"total": 1, "days": {}}',
        "analytics:events:a:3": b"1700000000000|referrer|desktop",
    })
    views = kvprefix.open_views(physical)
    list_keys = kvprefix.scoped_list_keys(fake_list_keys)

    response = await consistency.handle_consistency(
        {"links": views["links"], "users": views["users"]}, _principal(), list_keys, fake_get_many)
    assert response.status == 200
    report = json.loads(response.body)
    assert report["ok"] is True
    unrecognized = next(c for c in report["checks"] if c["check"] == "unrecognized_key")
    assert unrecognized["count"] == 0


async def test_orphan_purge_through_views_touches_only_targeted_analytics_keys():
    """A fifth cross-namespace hazard: the purge must never touch links: or
    users: keys sharing the physical store, even though it deletes by raw key
    name once it has enumerated the analytics view. If scoped_list_keys or
    open_views ever let the purge see (and therefore plan to delete) a
    links:/users:-prefixed key, this is what would catch it."""
    physical = FakeStore({
        "links:slug:keepme": json.dumps({
            "slug": "keepme", "target_url": "https://example.com", "owner": "admin",
        }).encode("utf-8"),
        "links:all_links": b'["keepme"]',
        "links:owner_links:admin": b'["keepme"]',
        "users:user:admin": b'{"username": "admin", "password_hash": "pbkdf2_sha256$100000$s$h"}',
        "users:_meta:usernames": b'["admin"]',
        "analytics:count:keepme:1": b'{"total": 3, "days": {}}',
        "analytics:count:killme:1": b'{"total": 9, "days": {}}',
        "analytics:count:killme:2": b'{"total": 1, "days": {}}',
        "analytics:events:killme:5": b"1700000000000|referrer|desktop",
    })
    before = dict(physical._data)
    views = kvprefix.open_views(physical)
    list_keys = kvprefix.scoped_list_keys(fake_list_keys)

    request = Request(
        method="POST", uri="/api/admin/analytics/purge", headers={},
        body=json.dumps({"confirm": "PURGE", "slugs": ["keepme", "killme"]}).encode("utf-8"),
    )
    response = await analyticsorphans.handle_orphan_purge(
        views["links"], views["analytics"], _principal(), request, list_keys, kvretry.direct)
    assert response.status == 200
    body = json.loads(response.body)
    assert body["purged_slugs"] == ["killme"]
    assert {"slug": "keepme", "reason": "link_exists"} in body["skipped"]

    for key, value in before.items():
        if key.startswith("links:") or key.startswith("users:"):
            assert physical._data.get(key) == value

    assert "analytics:count:keepme:1" in physical._data
    assert "analytics:count:killme:1" not in physical._data
    assert "analytics:count:killme:2" not in physical._data
    assert "analytics:events:killme:5" not in physical._data


async def test_inline_delete_purge_through_views_touches_only_the_deleted_slugs_keys():
    """A sixth cross-namespace hazard: docs/plans/inline-analytics-purge-on-delete.md's
    inline purge, driven through links.handle_delete's injected purge_analytics
    callable over the real kvprefix.open_views, must never touch users: keys
    or unrelated links:/analytics: keys — only the deleted slug's own record,
    both index entries, and its own analytics keys should disappear."""
    physical = FakeStore({
        "links:slug:killme": json.dumps({
            "slug": "killme", "target_url": "https://example.com", "owner": "admin",
        }).encode("utf-8"),
        "links:slug:keepme": json.dumps({
            "slug": "keepme", "target_url": "https://example.com", "owner": "admin",
        }).encode("utf-8"),
        "links:all_links": b'["killme", "keepme"]',
        "links:owner_links:admin": b'["killme", "keepme"]',
        "users:user:admin": b'{"username": "admin", "password_hash": "pbkdf2_sha256$100000$s$h"}',
        "users:_meta:usernames": b'["admin"]',
        "analytics:count:killme:1": b'{"total": 9, "days": {}}',
        "analytics:count:killme:2": b'{"total": 1, "days": {}}',
        "analytics:events:killme:5": b"1700000000000|referrer|desktop",
        "analytics:count:keepme:1": b'{"total": 3, "days": {}}',
    })
    before_users = {k: v for k, v in physical._data.items() if k.startswith("users:")}
    before_unrelated_links_and_analytics = {
        k: v for k, v in physical._data.items()
        if (k.startswith("links:") or k.startswith("analytics:")) and "keepme" in k
    }
    views = kvprefix.open_views(physical)
    list_keys = kvprefix.scoped_list_keys(fake_list_keys)

    principal = _principal(username="admin", role="admin", permissions=["links.edit_all"])

    async def purge(slug):
        return await analyticsorphans.purge_slug_analytics(views["analytics"], slug, list_keys)

    response = await links.handle_delete(views["links"], principal, "killme", purge_analytics=purge)
    assert response.status == 200
    body = json.loads(response.body)
    assert body["ok"] is True
    assert body["analytics_purge"]["status"] == "complete"
    assert body["analytics_purge"]["deleted_keys"] == 3

    # users: keys are untouched.
    for key, value in before_users.items():
        assert physical._data.get(key) == value

    # keepme's own record and analytics survive byte-identically.
    for key, value in before_unrelated_links_and_analytics.items():
        assert physical._data.get(key) == value

    # killme's record and analytics are all gone. The leftover all_links/
    # owner_links: keys are inert since docs/plans/derived-link-indexes.md's
    # Stage 2 — handle_delete no longer touches either, so they stay exactly
    # as seeded, stale and unread, rather than being corrected.
    assert "links:slug:killme" not in physical._data
    assert physical._data["links:all_links"] == b'["killme", "keepme"]'
    assert physical._data["links:owner_links:admin"] == b'["killme", "keepme"]'
    assert "analytics:count:killme:1" not in physical._data
    assert "analytics:count:killme:2" not in physical._data
    assert "analytics:events:killme:5" not in physical._data
