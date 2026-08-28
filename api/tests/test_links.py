import json

import pytest

import auth
import kvretry
import links
import urlpolicy
from responses import Request
from tests.fakes import FakeStore, ThrottlingStore, fake_get_many, fake_list_keys, recording_sleep


async def _set_policy(store, default_action, rules):
    policy, error = urlpolicy.parse_policy_document(
        {"default_action": default_action, "rules": rules}, now="2026-08-04T00:00:00Z", actor="admin",
    )
    assert error is None
    await urlpolicy.save_policy(store, policy)


def _principal(username="alice", role="user", permissions=None):
    return auth.Principal(username=username, role=role, permissions=permissions or [], csrf_token="x")


def _request(payload=None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    return Request(method="POST", uri="/api/links", headers={}, body=body)


@pytest.mark.parametrize(
    "slug,expected",
    [
        ("abc", True),          # min length
        ("a" * 32, True),       # max length
        ("valid-slug_123", True),
        ("ab", False),          # too short
        ("a" * 33, False),      # too long
        ("has space", False),
        ("has/slash", False),
        ("", False),
    ],
)
def test_custom_slug_validation_boundaries(slug, expected):
    assert links.is_valid_custom_slug(slug) is expected


async def test_create_random_slug_success():
    store = FakeStore()
    resp = await links.handle_create(store, _principal(), _request({"target_url": "https://example.com/x"}))
    assert resp.status == 201
    body = json.loads(resp.body)
    assert body["custom"] is False
    assert len(body["slug"]) == links.SLUG_LENGTH
    assert "password_hash" not in body
    assert body["password_protected"] is False


async def test_create_invalid_target_url():
    store = FakeStore()
    resp = await links.handle_create(store, _principal(), _request({"target_url": "not-a-url"}))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_target_url"


async def test_create_custom_slug_without_permission_forbidden():
    store = FakeStore()
    resp = await links.handle_create(
        store, _principal(permissions=[]),
        _request({"target_url": "https://example.com/x", "custom_slug": "my-slug"}),
    )
    assert resp.status == 403
    assert json.loads(resp.body)["required_permission"] == "links.create_custom_slug"


async def test_create_custom_slug_with_permission_succeeds():
    store = FakeStore()
    resp = await links.handle_create(
        store, _principal(permissions=["links.create_custom_slug"]),
        _request({"target_url": "https://example.com/x", "custom_slug": "my-slug"}),
    )
    assert resp.status == 201
    body = json.loads(resp.body)
    assert body["slug"] == "my-slug"
    assert body["custom"] is True


async def test_create_custom_slug_admin_bypasses_permission():
    store = FakeStore()
    resp = await links.handle_create(
        store, _principal(role="admin"),
        _request({"target_url": "https://example.com/x", "custom_slug": "admin-slug"}),
    )
    assert resp.status == 201


async def test_create_custom_slug_collision_returns_409():
    store = FakeStore()
    principal = _principal(permissions=["links.create_custom_slug"])
    first = await links.handle_create(store, principal, _request({"target_url": "https://example.com/a", "custom_slug": "taken"}))
    assert first.status == 201

    second = await links.handle_create(store, principal, _request({"target_url": "https://example.com/b", "custom_slug": "taken"}))
    assert second.status == 409
    assert json.loads(second.body)["error"] == "slug_taken"


async def test_create_password_too_short():
    store = FakeStore()
    resp = await links.handle_create(store, _principal(), _request({"target_url": "https://example.com/x", "password": "abc"}))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_password"


async def test_create_with_valid_password_masks_hash_in_response():
    store = FakeStore()
    resp = await links.handle_create(store, _principal(), _request({"target_url": "https://example.com/x", "password": "longenough"}))
    assert resp.status == 201
    body = json.loads(resp.body)
    assert "password_hash" not in body
    assert body["password_protected"] is True
    stored_record = json.loads(await store.get(f"slug:{body['slug']}"))
    assert stored_record["password_hash"] is not None
    assert auth.verify_password("longenough", stored_record["password_hash"])


async def test_handle_create_performs_exactly_one_kv_write(monkeypatch):
    """docs/plans/derived-link-indexes.md, Stage 2: a single create is
    exactly one KV write (the record) — 3 before this change (record,
    all_links, owner_links:<owner>)."""
    store = FakeStore()
    set_calls = []
    original_set = store.set

    async def counting_set(key, value):
        set_calls.append(key)
        await original_set(key, value)

    monkeypatch.setattr(store, "set", counting_set)

    resp = await links.handle_create(store, _principal(), _request({"target_url": "https://example.com/x"}))
    assert resp.status == 201
    body = json.loads(resp.body)
    assert set_calls == [f"slug:{body['slug']}"]


async def test_random_slug_retries_on_collision(monkeypatch):
    store = FakeStore()
    calls = iter(["colliding", "colliding", "free"])
    monkeypatch.setattr(links, "generate_slug", lambda: next(calls))

    await store.set("slug:colliding", b"{}")
    slug = await links.allocate_random_slug(store, set())
    assert slug == "free"


async def test_random_slug_raises_after_exhausting_attempts(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(links, "generate_slug", lambda: "always-colliding")
    await store.set("slug:always-colliding", b"{}")

    with pytest.raises(RuntimeError):
        await links.allocate_random_slug(store, set())


async def test_get_owner_can_view():
    store = FakeStore()
    owner = _principal(username="alice")
    created = await links.handle_create(store, owner, _request({"target_url": "https://example.com/x"}))
    slug = json.loads(created.body)["slug"]

    resp = await links.handle_get(store, owner, slug)
    assert resp.status == 200


async def test_get_non_owner_forbidden():
    store = FakeStore()
    created = await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/x"}))
    slug = json.loads(created.body)["slug"]

    resp = await links.handle_get(store, _principal(username="bob"), slug)
    assert resp.status == 403


async def test_get_admin_can_view_others_links():
    store = FakeStore()
    created = await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/x"}))
    slug = json.loads(created.body)["slug"]

    resp = await links.handle_get(store, _principal(username="admin", role="admin"), slug)
    assert resp.status == 200


async def test_get_view_all_permission_can_view_others_links():
    # Regression test: handle_get previously only checked owner-or-admin and
    # ignored links.view_all entirely, even though handle_list already uses
    # that same permission to decide whether the link appears in the list at
    # all — a links.view_all user could see a link in their dashboard but get
    # a 403 clicking into it.
    store = FakeStore()
    created = await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/x"}))
    slug = json.loads(created.body)["slug"]

    viewer = _principal(username="carol", permissions=["links.view_all"])
    resp = await links.handle_get(store, viewer, slug)
    assert resp.status == 200


async def test_get_edit_all_permission_can_view_others_links():
    # links.edit_all implies view access on its own; a user shouldn't need
    # links.view_all granted separately just to open a link they can edit.
    store = FakeStore()
    created = await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/x"}))
    slug = json.loads(created.body)["slug"]

    editor = _principal(username="dave", permissions=["links.edit_all"])
    resp = await links.handle_get(store, editor, slug)
    assert resp.status == 200


def _long_url(nbytes):
    """A valid http(s) URL whose UTF-8 length is exactly nbytes."""
    head = "https://example.com/"
    return head + "a" * (nbytes - len(head))


async def test_target_url_at_the_cap_is_accepted_and_over_it_is_rejected():
    store = FakeStore()
    at_cap = _long_url(links.MAX_TARGET_URL_BYTES)
    over = _long_url(links.MAX_TARGET_URL_BYTES + 1)
    assert len(at_cap.encode("utf-8")) == links.MAX_TARGET_URL_BYTES

    ok = await links.handle_create(store, _principal(), _request({"target_url": at_cap}))
    assert ok.status == 201

    bad = await links.handle_create(store, _principal(), _request({"target_url": over}))
    assert bad.status == 400
    body = json.loads(bad.body)
    assert body["error"] == "target_url_too_long"
    # The cap is echoed so no client hardcodes it.
    assert body["max_bytes"] == links.MAX_TARGET_URL_BYTES


async def test_target_url_cap_is_measured_in_bytes_not_characters():
    """A percent-free non-ASCII URL costs more bytes than it has characters,
    and the bound being protected is a stored value's SIZE."""
    store = FakeStore()
    # Each 'é' is 2 bytes in UTF-8, so this is under the cap by character
    # count and over it by byte count.
    head = "https://example.com/"
    url = head + "é" * ((links.MAX_TARGET_URL_BYTES - len(head)) // 2 + 1)
    assert len(url) < links.MAX_TARGET_URL_BYTES
    assert len(url.encode("utf-8")) > links.MAX_TARGET_URL_BYTES

    resp = await links.handle_create(store, _principal(), _request({"target_url": url}))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "target_url_too_long"


def test_target_url_error_body_is_public():
    """links.target_url_error_body (not _target_url_error_body) is shared the
    same way can_view/can_edit/target_url_error are — bulk.py's repoint
    action builds its 400 body from this directly."""
    assert links.target_url_error_body("target_url_too_long") == {
        "error": "target_url_too_long",
        "max_bytes": links.MAX_TARGET_URL_BYTES,
    }
    assert links.target_url_error_body("invalid_target_url") == {"error": "invalid_target_url"}


async def test_target_url_cap_is_enforced_on_update_too():
    """The second of the three authoring paths. A cap enforced at create only
    is trivially bypassed by creating short and then updating long."""
    store = FakeStore()
    created = await links.handle_create(store, _principal(), _request({"target_url": "https://example.com/short"}))
    slug = json.loads(created.body)["slug"]

    resp = await links.handle_update(
        store, _principal(), slug, _request({"target_url": _long_url(links.MAX_TARGET_URL_BYTES + 1)})
    )
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "target_url_too_long"
    # And the stored record is untouched.
    record = await links.get_link(store, slug)
    assert record["target_url"] == "https://example.com/short"


async def test_list_returns_a_link_that_is_in_NEITHER_index():
    """THE load-bearing test for docs/plans/derived-link-indexes.md's Stage 1.

    This is the exact state the measured 2026-08-17 lost update produces: two
    overlapping bulk creates each read `all_links`, add their own slugs and
    write back, so the loser's records exist and resolve at `/r/{slug}` while
    appearing in neither `all_links` nor `owner_links:<owner>` -- live links,
    invisible in the dashboard. A control run of just two concurrent 3-row
    creates reproduced it on the deployed build.

    Deriving the list from the `slug:` key enumeration is what fixes it, and
    this test is what proves the fix rather than merely exercising the new
    code path: it fails against the old index-reading handle_list.
    """
    store = FakeStore()
    await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/indexed"}))

    # A record with NO index entry anywhere -- exactly what a clobbered index
    # write leaves behind. Written directly, because no handler can produce
    # this state on purpose.
    orphan = {
        "slug": "lostupdate",
        "owner": "alice",
        "target_url": "https://example.com/lost-update",
        "status": "active",
        "created_at": "2026-01-01T00:00:00Z",
        "start_at": None,
        "end_at": None,
        "password_hash": None,
        "custom": True,
        "tags": [],
    }
    await store.set("slug:lostupdate", json.dumps(orphan).encode("utf-8"))

    # docs/plans/derived-link-indexes.md, Stage 2: there is no index left to
    # assert this record's absence from — its record existing with no index
    # entry anywhere is now simply the only state there is.

    resp = await links.handle_list(store, _principal(username="alice"), fake_get_many, fake_list_keys)

    assert resp.status == 200
    body = json.loads(resp.body)
    assert "lostupdate" in [link["slug"] for link in body["links"]]
    assert len(body["links"]) == 2


async def test_list_returns_an_unindexed_link_for_a_view_all_caller_too():
    """The sibling of the test above on the other branch of handle_list: a
    caller with links.view_all takes the all-slugs path, which must also be
    derived rather than read from `all_links`."""
    store = FakeStore()
    record = {
        "slug": "lostupdate",
        "owner": "bob",
        "target_url": "https://example.com/lost-update",
        "status": "active",
        "created_at": "2026-01-01T00:00:00Z",
        "start_at": None,
        "end_at": None,
        "password_hash": None,
        "custom": True,
        "tags": [],
    }
    await store.set("slug:lostupdate", json.dumps(record).encode("utf-8"))
    # docs/plans/derived-link-indexes.md, Stage 2: no index to assert empty.

    resp = await links.handle_list(
        store, _principal(username="alice", permissions=["links.view_all"]), fake_get_many, fake_list_keys
    )

    assert resp.status == 200
    assert [link["slug"] for link in json.loads(resp.body)["links"]] == ["lostupdate"]


async def test_list_skips_a_slug_whose_record_is_missing():
    """An interrupted bulk delete leaves index entries with no backing record
    (records are removed before indexes, deliberately). The gathered fetch in
    handle_list must skip those the same way the sequential loop did, rather
    than erroring or emitting a null entry — otherwise a crash mid-delete
    would take the whole dashboard down instead of being invisible."""
    store = FakeStore()
    await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/one"}))
    await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/two"}))

    slugs = await links.enumerate_slugs(store, fake_list_keys)
    await store.delete(f"slug:{slugs[0]}")

    resp = await links.handle_list(store, _principal(username="alice"), fake_get_many, fake_list_keys)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert [link["target_url"] for link in body["links"]] == ["https://example.com/two"]


async def test_list_skips_a_record_that_cannot_be_parsed():
    """The sibling case to the test above, and a real bug rather than a
    hypothetical: measured live on 2026-08-17, ONE corrupt `slug:` record made
    `GET /api/links` return 500, so the entire links table disappeared instead
    of the single bad row — while every link still resolved at /r/{slug},
    because `redirect` never reads through this path. So the service looked
    healthy with its management UI dead, and on a deployed app there was no
    remedy: the KV explorer is dev-only.

    Skipping is the same policy already applied to a record that is missing
    entirely, and nothing is concealed by it — the consistency check reports
    the same record as `unreadable_value`, which is deliberately not
    auto-repairable because a corrupt value's intended content is unknowable.
    """
    store = FakeStore()
    await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/one"}))
    await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/two"}))

    slugs = await links.enumerate_slugs(store, fake_list_keys)
    await store.set(f"slug:{slugs[0]}", b"{not valid json at all")

    resp = await links.handle_list(store, _principal(username="alice"), fake_get_many, fake_list_keys)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert [link["target_url"] for link in body["links"]] == ["https://example.com/two"]


async def test_get_not_found():
    store = FakeStore()
    resp = await links.handle_get(store, _principal(), "doesnotexist")
    assert resp.status == 404


async def test_list_only_shows_own_links_by_default():
    store = FakeStore()
    await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/alice"}))
    await links.handle_create(store, _principal(username="bob"), _request({"target_url": "https://example.com/bob"}))

    resp = await links.handle_list(store, _principal(username="alice"), fake_get_many, fake_list_keys)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert [link["target_url"] for link in body["links"]] == ["https://example.com/alice"]


async def test_list_admin_sees_all_links():
    store = FakeStore()
    await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/alice"}))
    await links.handle_create(store, _principal(username="bob"), _request({"target_url": "https://example.com/bob"}))

    resp = await links.handle_list(store, _principal(username="admin", role="admin"), fake_get_many, fake_list_keys)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert {link["target_url"] for link in body["links"]} == {"https://example.com/alice", "https://example.com/bob"}


async def test_list_view_all_permission_sees_all_links():
    store = FakeStore()
    await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/alice"}))
    await links.handle_create(store, _principal(username="bob"), _request({"target_url": "https://example.com/bob"}))

    viewer = _principal(username="carol", permissions=["links.view_all"])
    resp = await links.handle_list(store, viewer, fake_get_many, fake_list_keys)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert {link["target_url"] for link in body["links"]} == {"https://example.com/alice", "https://example.com/bob"}


async def test_list_edit_all_permission_sees_all_links():
    store = FakeStore()
    await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/alice"}))
    await links.handle_create(store, _principal(username="bob"), _request({"target_url": "https://example.com/bob"}))

    editor = _principal(username="dave", permissions=["links.edit_all"])
    resp = await links.handle_list(store, editor, fake_get_many, fake_list_keys)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert {link["target_url"] for link in body["links"]} == {"https://example.com/alice", "https://example.com/bob"}


async def test_delete_owner_succeeds_and_removes_the_record():
    """docs/plans/derived-link-indexes.md, Stage 2: there is no index to
    remove the slug from any more — the record's own existence is the only
    truth, so deleting it is the whole story."""
    store = FakeStore()
    owner = _principal(username="alice")
    created = await links.handle_create(store, owner, _request({"target_url": "https://example.com/x"}))
    slug = json.loads(created.body)["slug"]

    resp = await links.handle_delete(store, owner, slug)
    assert resp.status == 200
    assert await store.exists(f"slug:{slug}") is False


async def test_delete_non_owner_forbidden():
    store = FakeStore()
    created = await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/x"}))
    slug = json.loads(created.body)["slug"]

    resp = await links.handle_delete(store, _principal(username="bob"), slug)
    assert resp.status == 403


async def test_delete_edit_all_permission_can_delete_others_links():
    store = FakeStore()
    created = await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/x"}))
    slug = json.loads(created.body)["slug"]

    editor = _principal(username="dave", permissions=["links.edit_all"])
    resp = await links.handle_delete(store, editor, slug)
    assert resp.status == 200
    assert await store.exists(f"slug:{slug}") is False


async def test_delete_view_all_permission_alone_still_forbidden():
    store = FakeStore()
    created = await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/x"}))
    slug = json.loads(created.body)["slug"]

    viewer = _principal(username="carol", permissions=["links.view_all"])
    resp = await links.handle_delete(store, viewer, slug)
    assert resp.status == 403
    assert json.loads(resp.body)["required_permission"] == "links.edit_all"


class _RecordingStore(FakeStore):
    """Records op order so handle_delete's record-then-indexes-then-analytics
    ordering (docs/plans/inline-analytics-purge-on-delete.md) is testable
    rather than aspirational — same pattern as
    tests/test_analytics_orphans.py's RecordingStore."""

    def __init__(self, data=None):
        super().__init__(data)
        self.ops: list[tuple[str, str]] = []

    async def get(self, key):
        self.ops.append(("get", key))
        return await super().get(key)

    async def set(self, key, value):
        self.ops.append(("set", key))
        await super().set(key, value)

    async def delete(self, key):
        self.ops.append(("delete", key))
        await super().delete(key)


async def test_delete_without_purge_analytics_is_byte_identical_to_today():
    """purge_analytics defaults to None, and omitting it must be exactly
    today's behaviour — bulk.handle_bulk_action's delete branch does not pass
    it and must stay untouched."""
    store = FakeStore()
    owner = _principal(username="alice")
    created = await links.handle_create(store, owner, _request({"target_url": "https://example.com/x"}))
    slug = json.loads(created.body)["slug"]

    resp = await links.handle_delete(store, owner, slug)
    assert resp.status == 200
    assert json.loads(resp.body) == {"ok": True}


async def test_delete_with_purge_analytics_includes_the_result_in_the_response():
    store = FakeStore()
    owner = _principal(username="alice")
    created = await links.handle_create(store, owner, _request({"target_url": "https://example.com/x"}))
    slug = json.loads(created.body)["slug"]

    async def purge(s):
        assert s == slug
        return {"status": "complete", "found_keys": 3, "deleted_keys": 3}

    resp = await links.handle_delete(store, owner, slug, purge_analytics=purge)
    assert resp.status == 200
    assert json.loads(resp.body) == {
        "ok": True,
        "analytics_purge": {"status": "complete", "found_keys": 3, "deleted_keys": 3},
    }


async def test_delete_purge_analytics_is_never_called_on_404():
    store = FakeStore()
    calls = []

    async def purge(s):
        calls.append(s)
        return {"status": "complete", "found_keys": 0, "deleted_keys": 0}

    resp = await links.handle_delete(store, _principal(username="alice"), "nosuchslug", purge_analytics=purge)
    assert resp.status == 404
    assert calls == []


async def test_delete_purge_analytics_is_never_called_on_403():
    store = FakeStore()
    created = await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/x"}))
    slug = json.loads(created.body)["slug"]
    calls = []

    async def purge(s):
        calls.append(s)
        return {"status": "complete", "found_keys": 0, "deleted_keys": 0}

    resp = await links.handle_delete(store, _principal(username="bob"), slug, purge_analytics=purge)
    assert resp.status == 403
    assert calls == []


async def test_delete_record_delete_completes_before_the_first_analytics_delete():
    """docs/plans/derived-link-indexes.md, Stage 2: there is no index write
    left between the record delete and the analytics purge — the ordering
    rule shrinks to "record, then purge", and this pins exactly that."""
    store = _RecordingStore()
    owner = _principal(username="alice")
    created = await links.handle_create(store, owner, _request({"target_url": "https://example.com/x"}))
    slug = json.loads(created.body)["slug"]
    store.ops.clear()

    purge_started_at = []

    async def purge(s):
        purge_started_at.append(len(store.ops))
        return {"status": "complete", "found_keys": 0, "deleted_keys": 0}

    resp = await links.handle_delete(store, owner, slug, purge_analytics=purge)
    assert resp.status == 200

    # Everything recorded before the purge callable ran must be exactly the
    # record delete — never anything analytics-shaped — and the purge
    # callable itself must have run exactly once, after it.
    assert len(purge_started_at) == 1
    ops_before_purge = store.ops[:purge_started_at[0]]
    assert ("delete", f"slug:{slug}") in ops_before_purge
    assert not any(op == "set" for op, _ in ops_before_purge)
    assert not any(key in ("all_links",) or key.startswith("owner_links:") for _, key in ops_before_purge)


async def test_delete_purge_analytics_raising_still_yields_200_with_failed_status():
    store = FakeStore()
    owner = _principal(username="alice")
    created = await links.handle_create(store, owner, _request({"target_url": "https://example.com/x"}))
    slug = json.loads(created.body)["slug"]

    async def purge(s):
        raise RuntimeError("simulated KV failure")

    resp = await links.handle_delete(store, owner, slug, purge_analytics=purge)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["ok"] is True
    assert body["analytics_purge"] == {"status": "failed", "found_keys": 0, "deleted_keys": 0}
    # The link itself is still gone — the KV failure in the purge must never
    # roll back or otherwise affect the already-completed deletion.
    assert await store.exists(f"slug:{slug}") is False


async def test_set_password_set_change_clear():
    store = FakeStore()
    owner = _principal(username="alice")
    created = await links.handle_create(store, owner, _request({"target_url": "https://example.com/x"}))
    slug = json.loads(created.body)["slug"]

    set_resp = await links.handle_set_password(store, owner, slug, _request({"password": "firstpass"}))
    assert set_resp.status == 200
    assert json.loads(set_resp.body)["password_protected"] is True

    record = json.loads(await store.get(f"slug:{slug}"))
    assert auth.verify_password("firstpass", record["password_hash"])

    change_resp = await links.handle_set_password(store, owner, slug, _request({"password": "secondpass"}))
    assert change_resp.status == 200
    record = json.loads(await store.get(f"slug:{slug}"))
    assert auth.verify_password("secondpass", record["password_hash"])
    assert not auth.verify_password("firstpass", record["password_hash"])

    clear_resp = await links.handle_set_password(store, owner, slug, _request({"password": None}))
    assert clear_resp.status == 200
    assert json.loads(clear_resp.body)["password_protected"] is False
    record = json.loads(await store.get(f"slug:{slug}"))
    assert record["password_hash"] is None


async def test_set_password_edit_all_permission_can_set_others_links():
    store = FakeStore()
    created = await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/x"}))
    slug = json.loads(created.body)["slug"]

    editor = _principal(username="dave", permissions=["links.edit_all"])
    resp = await links.handle_set_password(store, editor, slug, _request({"password": "newpass"}))
    assert resp.status == 200
    record = json.loads(await store.get(f"slug:{slug}"))
    assert auth.verify_password("newpass", record["password_hash"])


async def test_set_password_view_all_permission_alone_still_forbidden():
    store = FakeStore()
    created = await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/x"}))
    slug = json.loads(created.body)["slug"]

    viewer = _principal(username="carol", permissions=["links.view_all"])
    resp = await links.handle_set_password(store, viewer, slug, _request({"password": "newpass"}))
    assert resp.status == 403
    assert json.loads(resp.body)["required_permission"] == "links.edit_all"


# --- Time-window validation on create ---


async def test_create_with_valid_start_and_end():
    store = FakeStore()
    resp = await links.handle_create(store, _principal(), _request({
        "target_url": "https://example.com/x",
        "start_at": "2026-01-01T00:00:00Z",
        "end_at": "2026-02-01T00:00:00Z",
    }))
    assert resp.status == 201
    body = json.loads(resp.body)
    assert body["start_at"] == "2026-01-01T00:00:00Z"
    assert body["end_at"] == "2026-02-01T00:00:00Z"


async def test_create_with_start_only():
    store = FakeStore()
    resp = await links.handle_create(store, _principal(), _request({
        "target_url": "https://example.com/x",
        "start_at": "2026-01-01T00:00:00Z",
    }))
    assert resp.status == 201
    body = json.loads(resp.body)
    assert body["start_at"] == "2026-01-01T00:00:00Z"
    assert body["end_at"] is None


async def test_create_with_end_only():
    store = FakeStore()
    resp = await links.handle_create(store, _principal(), _request({
        "target_url": "https://example.com/x",
        "end_at": "2026-02-01T00:00:00Z",
    }))
    assert resp.status == 201
    body = json.loads(resp.body)
    assert body["start_at"] is None
    assert body["end_at"] == "2026-02-01T00:00:00Z"


async def test_create_with_explicit_null_window_fields_is_unset():
    store = FakeStore()
    resp = await links.handle_create(store, _principal(), _request({
        "target_url": "https://example.com/x",
        "start_at": None,
        "end_at": None,
    }))
    assert resp.status == 201
    body = json.loads(resp.body)
    assert body["start_at"] is None
    assert body["end_at"] is None


async def test_create_inverted_window_range_rejected():
    store = FakeStore()
    resp = await links.handle_create(store, _principal(), _request({
        "target_url": "https://example.com/x",
        "start_at": "2026-02-01T00:00:00Z",
        "end_at": "2026-01-01T00:00:00Z",
    }))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_window_range"


async def test_create_equal_start_and_end_rejected():
    store = FakeStore()
    resp = await links.handle_create(store, _principal(), _request({
        "target_url": "https://example.com/x",
        "start_at": "2026-01-01T00:00:00Z",
        "end_at": "2026-01-01T00:00:00Z",
    }))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_window_range"


async def test_create_malformed_start_at_rejected():
    store = FakeStore()
    resp = await links.handle_create(store, _principal(), _request({
        "target_url": "https://example.com/x",
        "start_at": "not-a-date",
    }))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_start_at"


async def test_create_naive_datetime_start_at_rejected():
    store = FakeStore()
    resp = await links.handle_create(store, _principal(), _request({
        "target_url": "https://example.com/x",
        "start_at": "2026-01-01T00:00:00",  # no timezone
    }))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_start_at"


async def test_create_malformed_end_at_rejected():
    store = FakeStore()
    resp = await links.handle_create(store, _principal(), _request({
        "target_url": "https://example.com/x",
        "end_at": "not-a-date",
    }))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_end_at"


# --- PATCH /api/links/{slug} (handle_update) ---


async def _make_link(store, owner="alice", **extra_payload):
    owner_p = _principal(username=owner)
    payload = {"target_url": "https://example.com/x", **extra_payload}
    created = await links.handle_create(store, owner_p, _request(payload))
    return json.loads(created.body)["slug"]


async def test_update_not_found():
    store = FakeStore()
    resp = await links.handle_update(store, _principal(), "doesnotexist", _request({"status": "disabled"}))
    assert resp.status == 404


async def test_update_non_owner_forbidden():
    store = FakeStore()
    slug = await _make_link(store, owner="alice")
    resp = await links.handle_update(store, _principal(username="bob"), slug, _request({"status": "disabled"}))
    assert resp.status == 403


async def test_update_admin_can_update_others_links():
    store = FakeStore()
    slug = await _make_link(store, owner="alice")
    resp = await links.handle_update(store, _principal(username="admin", role="admin"), slug, _request({"status": "disabled"}))
    assert resp.status == 200


async def test_update_edit_all_permission_can_update_others_links():
    store = FakeStore()
    slug = await _make_link(store, owner="alice")
    editor = _principal(username="dave", permissions=["links.edit_all"])
    resp = await links.handle_update(store, editor, slug, _request({"status": "disabled"}))
    assert resp.status == 200


async def test_update_view_all_permission_alone_still_forbidden():
    # links.view_all grants read access (see test_get_view_all_permission_*)
    # but not write access — only links.edit_all (or ownership/admin) does.
    store = FakeStore()
    slug = await _make_link(store, owner="alice")
    viewer = _principal(username="carol", permissions=["links.view_all"])
    resp = await links.handle_update(store, viewer, slug, _request({"status": "disabled"}))
    assert resp.status == 403
    assert json.loads(resp.body)["required_permission"] == "links.edit_all"


async def test_update_empty_payload_rejected():
    store = FakeStore()
    owner = _principal(username="alice")
    slug = await _make_link(store, owner="alice")
    resp = await links.handle_update(store, owner, slug, _request({}))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "no_fields_to_update"


async def test_update_partial_field_leaves_others_untouched():
    store = FakeStore()
    owner = _principal(username="alice")
    slug = await _make_link(store, owner="alice", start_at="2026-01-01T00:00:00Z")

    resp = await links.handle_update(store, owner, slug, _request({"status": "disabled"}))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["status"] == "disabled"
    assert body["start_at"] == "2026-01-01T00:00:00Z"  # untouched
    assert body["target_url"] == "https://example.com/x"  # untouched


async def test_update_invalid_status_rejected():
    store = FakeStore()
    owner = _principal(username="alice")
    slug = await _make_link(store, owner="alice")
    resp = await links.handle_update(store, owner, slug, _request({"status": "bogus"}))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_status"


async def test_update_target_url_valid_and_invalid():
    store = FakeStore()
    owner = _principal(username="alice")
    slug = await _make_link(store, owner="alice")

    ok = await links.handle_update(store, owner, slug, _request({"target_url": "https://example.com/new"}))
    assert ok.status == 200
    assert json.loads(ok.body)["target_url"] == "https://example.com/new"

    bad = await links.handle_update(store, owner, slug, _request({"target_url": "not-a-url"}))
    assert bad.status == 400
    assert json.loads(bad.body)["error"] == "invalid_target_url"


async def test_update_window_fields_independently():
    store = FakeStore()
    owner = _principal(username="alice")
    slug = await _make_link(store, owner="alice")

    resp = await links.handle_update(store, owner, slug, _request({"start_at": "2026-01-01T00:00:00Z"}))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["start_at"] == "2026-01-01T00:00:00Z"
    assert body["end_at"] is None


async def test_update_explicit_null_clears_window_field():
    store = FakeStore()
    owner = _principal(username="alice")
    slug = await _make_link(store, owner="alice", start_at="2026-01-01T00:00:00Z")

    resp = await links.handle_update(store, owner, slug, _request({"start_at": None}))
    assert resp.status == 200
    assert json.loads(resp.body)["start_at"] is None


async def test_update_merged_window_revalidation_rejects_bad_patch():
    store = FakeStore()
    owner = _principal(username="alice")
    slug = await _make_link(store, owner="alice", start_at="2026-02-01T00:00:00Z")

    # Patching only end_at to before the existing start_at must still be caught.
    resp = await links.handle_update(store, owner, slug, _request({"end_at": "2026-01-01T00:00:00Z"}))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_window_range"


async def test_update_bumps_updated_at():
    store = FakeStore()
    owner = _principal(username="alice")
    slug = await _make_link(store, owner="alice")
    original = json.loads(await store.get(f"slug:{slug}"))

    resp = await links.handle_update(store, owner, slug, _request({"status": "disabled"}))
    updated = json.loads(resp.body)
    assert updated["updated_at"] >= original["updated_at"]
    assert updated["created_at"] == original["created_at"]


# --- Tags on create/update/public shape ---


async def test_create_with_tags_normalizes_sorts_and_dedupes():
    store = FakeStore()
    resp = await links.handle_create(store, _principal(), _request({"target_url": "https://example.com/x", "tags": ["Q4", " sale "]}))
    assert resp.status == 201
    body = json.loads(resp.body)
    assert body["tags"] == ["q4", "sale"]


async def test_create_with_no_tags_key_stores_empty_list():
    store = FakeStore()
    resp = await links.handle_create(store, _principal(), _request({"target_url": "https://example.com/x"}))
    assert resp.status == 201
    body = json.loads(resp.body)
    assert body["tags"] == []


async def test_create_with_invalid_tag_rejected():
    store = FakeStore()
    resp = await links.handle_create(store, _principal(), _request({"target_url": "https://example.com/x", "tags": ["Bad Tag"]}))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_tag"


async def test_legacy_record_with_no_tags_key_serializes_empty_list():
    store = FakeStore()
    slug = await _make_link(store, owner="alice")
    record = json.loads(await store.get(f"slug:{slug}"))
    del record["tags"]
    await store.set(f"slug:{slug}", json.dumps(record).encode("utf-8"))

    resp = await links.handle_get(store, _principal(username="alice"), slug)
    assert resp.status == 200
    assert json.loads(resp.body)["tags"] == []


async def test_patch_tags_empty_list_clears_them():
    store = FakeStore()
    owner = _principal(username="alice")
    slug = await _make_link(store, owner="alice")
    await links.handle_update(store, owner, slug, _request({"tags": ["sale"]}))

    resp = await links.handle_update(store, owner, slug, _request({"tags": []}))
    assert resp.status == 200
    assert json.loads(resp.body)["tags"] == []


async def test_patch_tags_null_rejected():
    store = FakeStore()
    owner = _principal(username="alice")
    slug = await _make_link(store, owner="alice")
    resp = await links.handle_update(store, owner, slug, _request({"tags": None}))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_tags"


async def test_patch_tags_invalid_tag_rejected():
    store = FakeStore()
    owner = _principal(username="alice")
    slug = await _make_link(store, owner="alice")
    resp = await links.handle_update(store, owner, slug, _request({"tags": ["Bad Tag"]}))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_tag"


async def test_patch_omitting_tags_leaves_existing_list_untouched():
    store = FakeStore()
    owner = _principal(username="alice")
    slug = await _make_link(store, owner="alice", tags=["sale"])
    resp = await links.handle_update(store, owner, slug, _request({"status": "disabled"}))
    assert resp.status == 200
    assert json.loads(resp.body)["tags"] == ["sale"]


async def test_patch_tags_full_replacement_not_merge():
    store = FakeStore()
    owner = _principal(username="alice")
    slug = await _make_link(store, owner="alice", tags=["sale", "q4"])
    resp = await links.handle_update(store, owner, slug, _request({"tags": ["promo"]}))
    assert resp.status == 200
    assert json.loads(resp.body)["tags"] == ["promo"]


# docs/plans/derived-link-indexes.md, Stage 2: add_slugs_to_indexes,
# remove_slugs_from_indexes and move_slugs_between_owners are deleted along
# with the indexes they maintained. The whole "Batched index writers" and
# "Injectable write" test blocks that exercised them directly are gone with
# them — the write-retry behaviour they pinned (INDEX_WRITE policy routing)
# no longer applies to anything, since there is no index write left in
# links.py at all. The record-write retry these helpers never touched is
# still pinned by the handle_create/handle_update/handle_delete tests below.


# --- Destination URL policy enforcement ---


async def test_create_succeeds_unchanged_when_no_policy_key_exists():
    store = FakeStore()
    resp = await links.handle_create(store, _principal(), _request({"target_url": "https://evil.example/x"}))
    assert resp.status == 201


async def test_create_rejects_destination_denied_by_policy():
    store = FakeStore()
    await _set_policy(store, "allow", [{"host": "evil.example", "action": "deny"}])

    resp = await links.handle_create(store, _principal(), _request({"target_url": "https://evil.example/x"}))
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body == {
        "error": "destination_not_allowed",
        "host": "evil.example",
        "reason": "denied_by_rule",
        "matched_rule": "evil.example",
    }


async def test_create_rejection_consumes_no_slug():
    store = FakeStore()
    await _set_policy(store, "allow", [{"host": "evil.example", "action": "deny"}])

    resp = await links.handle_create(
        store, _principal(permissions=["links.create_custom_slug"]),
        _request({"target_url": "https://evil.example/x", "custom_slug": "my-slug"}),
    )
    assert resp.status == 400
    assert await store.exists("slug:my-slug") is False


async def test_update_rejects_destination_denied_by_policy():
    store = FakeStore()
    create_resp = await links.handle_create(store, _principal(), _request({"target_url": "https://ok.example/x"}))
    slug = json.loads(create_resp.body)["slug"]

    await _set_policy(store, "allow", [{"host": "evil.example", "action": "deny"}])

    resp = await links.handle_update(
        store, _principal(), slug, _request({"target_url": "https://evil.example/x"}),
    )
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["error"] == "destination_not_allowed"
    assert body["host"] == "evil.example"

    unchanged = await links.get_link(store, slug)
    assert unchanged["target_url"] == "https://ok.example/x"


async def test_update_legacy_violator_stays_editable_via_status_patch():
    """The load-bearing guarantee the whole retroactive design rests on: a
    link whose stored target_url already violates the CURRENT policy must
    stay editable for every field except target_url, so the operator's
    remediation path (bulk disable) is never blocked by the very enforcement
    that motivates it."""
    store = FakeStore()
    create_resp = await links.handle_create(store, _principal(), _request({"target_url": "https://evil.example/x"}))
    slug = json.loads(create_resp.body)["slug"]

    # Policy is added AFTER the link already exists — the link is now a
    # legacy violator, but PATCH must never re-check a target_url it isn't
    # changing.
    await _set_policy(store, "allow", [{"host": "evil.example", "action": "deny"}])

    resp = await links.handle_update(store, _principal(), slug, _request({"status": "disabled"}))
    assert resp.status == 200
    assert json.loads(resp.body)["status"] == "disabled"


async def test_update_legacy_violator_stays_editable_via_tags_patch():
    store = FakeStore()
    create_resp = await links.handle_create(store, _principal(), _request({"target_url": "https://evil.example/x"}))
    slug = json.loads(create_resp.body)["slug"]

    await _set_policy(store, "deny", [])  # default-deny, no allow rule for anything

    resp = await links.handle_update(store, _principal(), slug, _request({"tags": ["q4"]}))
    assert resp.status == 200
    assert json.loads(resp.body)["tags"] == ["q4"]


async def test_get_link_raises_a_named_error_for_an_unreadable_record():
    """`get_link` must distinguish "absent" from "present but unparseable".
    Before this, both six callers and the handler saw a bare JSONDecodeError
    and answered `500 internal_error`, which tells an operator to retry a
    transient fault when it is permanent stored data only they can fix."""
    store = FakeStore()
    await store.set("slug:broken", b"{not json")
    try:
        await links.get_link(store, "broken")
    except links.UnreadableLinkError as exc:
        assert exc.slug == "broken"
    else:
        raise AssertionError("expected UnreadableLinkError")

    # Absent is still plain None, not an error — the distinction is the point.
    assert await links.get_link(store, "absent") is None


async def test_get_link_unreadable_error_carries_the_json_decode_error_as_cause():
    store = FakeStore()
    await store.set("slug:broken", b"{not json")
    try:
        await links.get_link(store, "broken")
    except links.UnreadableLinkError as exc:
        assert isinstance(exc.cause, json.JSONDecodeError)
        assert "line" in str(exc.cause) and "column" in str(exc.cause)
        assert exc.__cause__ is exc.cause  # `from exc` still sets __cause__
    else:
        raise AssertionError("expected UnreadableLinkError")


async def test_get_link_unreadable_error_carries_a_unicode_decode_error_as_cause():
    store = FakeStore()
    await store.set("slug:bad-utf8", bytes([0x80]))
    try:
        await links.get_link(store, "bad-utf8")
    except links.UnreadableLinkError as exc:
        assert isinstance(exc.cause, UnicodeDecodeError)
        assert exc.slug == "bad-utf8"
    else:
        raise AssertionError("expected UnreadableLinkError")


async def test_get_link_does_not_raise_on_a_type_mismatched_field():
    """api's notion of unreadable is narrower than linkgate.ParseLink's:
    json.loads type-checks nothing, so a record like {"status": 7} parses
    fine here even though the same bytes 500 at /r/{slug}."""
    store = FakeStore()
    await store.set("slug:weird", json.dumps({"slug": "weird", "status": 7}).encode())
    record = await links.get_link(store, "weird")
    assert record["status"] == 7


def test_unreadable_link_error_is_still_constructible_with_no_cause():
    exc = links.UnreadableLinkError("x")
    assert exc.slug == "x"
    assert exc.cause is None


async def test_delete_still_works_on_an_unreadable_record():
    """Deletion is the ONE path that must survive a corrupt record, because
    it is the repair. Everything else already treats such a link as dead
    (`redirect` 404s it, the list skips it, editing cannot read it), so
    refusing to delete would make it permanent — the KV explorer is dev-only,
    leaving a hand-edited backup restore as the only remedy on a deployment.
    """
    store = FakeStore()
    await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/a"}))
    slug = (await links.enumerate_slugs(store, fake_list_keys))[0]
    await store.set(f"slug:{slug}", b"{corrupt")

    admin = _principal(username="root", role="admin")
    resp = await links.handle_delete(store, admin, slug)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["record_was_unreadable"] is True
    assert await store.get(f"slug:{slug}") is None


async def test_delete_of_an_unreadable_record_fails_closed_without_edit_all():
    """Ownership is unknowable for a corrupt record, so the ordinary
    owner-or-admin check cannot run. It must fall back to the WIDER
    permission, not to a guess — otherwise an unreadable record would be
    deletable by anyone who asked."""
    store = FakeStore()
    await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/a"}))
    slug = (await links.enumerate_slugs(store, fake_list_keys))[0]
    await store.set(f"slug:{slug}", b"{corrupt")

    resp = await links.handle_delete(store, _principal(username="alice"), slug)
    assert resp.status == 403
    assert await store.get(f"slug:{slug}") is not None


# --- Write-throttle resilience for single-link handlers (docs/plans/write-throttle-resilience.md) ---


async def test_handle_create_success_shape_unchanged_with_a_real_writer():
    store = FakeStore()
    sleep, delays = recording_sleep()
    write = kvretry.make_writer(sleep)
    resp = await links.handle_create(store, _principal(), _request({"target_url": "https://example.com/x"}), write)
    assert resp.status == 201
    assert delays == []


async def test_handle_create_retries_a_throttled_record_write_and_still_succeeds():
    """docs/plans/derived-link-indexes.md, Stage 2: there is no index write
    left for create to fail on — the old
    test_handle_create_throttled_index_write_reports_partial_link_still_created
    exercised a scenario that no longer exists (create has never had a way
    to report `partial`/`index_updated` since the index itself was
    removed). What remains is the record write's own retry, the same shape
    handle_update/handle_set_password already pin below."""
    store = ThrottlingStore(fail_times={})
    store._fail_times["slug:my-slug"] = 2  # fails twice, succeeds on the 3rd (RECORD_WRITE.attempts == 3)
    sleep, delays = recording_sleep()
    write = kvretry.make_writer(sleep)
    resp = await links.handle_create(
        store, _principal(permissions=["links.create_custom_slug"]),
        _request({"target_url": "https://example.com/x", "custom_slug": "my-slug"}), write)
    assert resp.status == 201
    assert len(delays) == 2
    assert await store.exists("slug:my-slug") is True


async def test_handle_update_retries_a_throttled_write_and_still_succeeds():
    store = ThrottlingStore(fail_times={})
    owner = _principal(username="alice")
    created = await links.handle_create(store, owner, _request({"target_url": "https://example.com/x"}))
    slug = json.loads(created.body)["slug"]
    store._fail_times[f"slug:{slug}"] = 2  # fails twice, succeeds on the 3rd (RECORD_WRITE.attempts == 3)

    sleep, delays = recording_sleep()
    write = kvretry.make_writer(sleep)
    resp = await links.handle_update(store, owner, slug, _request({"status": "disabled"}), write)
    assert resp.status == 200
    assert len(delays) == 2
    record = json.loads(await store.get(f"slug:{slug}"))
    assert record["status"] == "disabled"


async def test_handle_set_password_retries_a_throttled_write_and_still_succeeds():
    store = ThrottlingStore(fail_times={})
    owner = _principal(username="alice")
    created = await links.handle_create(store, owner, _request({"target_url": "https://example.com/x"}))
    slug = json.loads(created.body)["slug"]
    store._fail_times[f"slug:{slug}"] = 2

    sleep, delays = recording_sleep()
    write = kvretry.make_writer(sleep)
    resp = await links.handle_set_password(store, owner, slug, _request({"password": "longenough"}), write)
    assert resp.status == 200
    assert len(delays) == 2


async def test_handle_delete_retries_a_throttled_record_delete_and_still_succeeds():
    """docs/plans/derived-link-indexes.md, Stage 2: there is no owner index
    write left for delete to fail on — the old
    test_handle_delete_throttled_index_write_reports_index_updated_false
    exercised a scenario that no longer exists. What remains is the record
    delete's own retry."""
    store = ThrottlingStore(fail_times={})
    owner = _principal(username="alice")
    created = await links.handle_create(store, owner, _request({"target_url": "https://example.com/x"}))
    slug = json.loads(created.body)["slug"]
    store._fail_times[f"slug:{slug}"] = 2  # fails twice, succeeds on the 3rd

    sleep, delays = recording_sleep()
    write = kvretry.make_writer(sleep)
    resp = await links.handle_delete(store, owner, slug, write=write)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body == {"ok": True}
    assert len(delays) == 2
    assert await store.exists(f"slug:{slug}") is False


async def test_handle_delete_unreadable_record_branch_unchanged_by_write_param():
    """The unreadable-record delete branch never threads `write` at all — it
    is unaffected by this task, confirmed with a real retrying writer passed
    in (which would show up as retries/delays if it were somehow reached)."""
    store = ThrottlingStore(fail_times={})
    await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/a"}))
    slug = (await links.enumerate_slugs(store, fake_list_keys))[0]
    await store.set(f"slug:{slug}", b"{corrupt")

    sleep, delays = recording_sleep()
    write = kvretry.make_writer(sleep)
    resp = await links.handle_delete(store, _principal(role="admin"), slug, write=write)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["record_was_unreadable"] is True
    assert delays == []
