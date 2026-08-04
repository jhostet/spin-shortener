import json

import pytest

import auth
import links
from responses import Request
from tests.fakes import FakeStore


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


async def test_get_not_found():
    store = FakeStore()
    resp = await links.handle_get(store, _principal(), "doesnotexist")
    assert resp.status == 404


async def test_list_only_shows_own_links_by_default():
    store = FakeStore()
    await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/alice"}))
    await links.handle_create(store, _principal(username="bob"), _request({"target_url": "https://example.com/bob"}))

    resp = await links.handle_list(store, _principal(username="alice"))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert [link["target_url"] for link in body["links"]] == ["https://example.com/alice"]


async def test_list_admin_sees_all_links():
    store = FakeStore()
    await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/alice"}))
    await links.handle_create(store, _principal(username="bob"), _request({"target_url": "https://example.com/bob"}))

    resp = await links.handle_list(store, _principal(username="admin", role="admin"))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert {link["target_url"] for link in body["links"]} == {"https://example.com/alice", "https://example.com/bob"}


async def test_list_view_all_permission_sees_all_links():
    store = FakeStore()
    await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/alice"}))
    await links.handle_create(store, _principal(username="bob"), _request({"target_url": "https://example.com/bob"}))

    viewer = _principal(username="carol", permissions=["links.view_all"])
    resp = await links.handle_list(store, viewer)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert {link["target_url"] for link in body["links"]} == {"https://example.com/alice", "https://example.com/bob"}


async def test_list_edit_all_permission_sees_all_links():
    store = FakeStore()
    await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/alice"}))
    await links.handle_create(store, _principal(username="bob"), _request({"target_url": "https://example.com/bob"}))

    editor = _principal(username="dave", permissions=["links.edit_all"])
    resp = await links.handle_list(store, editor)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert {link["target_url"] for link in body["links"]} == {"https://example.com/alice", "https://example.com/bob"}


async def test_delete_owner_succeeds_and_removes_index():
    store = FakeStore()
    owner = _principal(username="alice")
    created = await links.handle_create(store, owner, _request({"target_url": "https://example.com/x"}))
    slug = json.loads(created.body)["slug"]

    resp = await links.handle_delete(store, owner, slug)
    assert resp.status == 200
    assert await store.exists(f"slug:{slug}") is False
    assert slug not in await links._owned_slugs(store, "alice")
    assert slug not in await links._all_slugs(store)


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
    assert slug not in await links._owned_slugs(store, "alice")


async def test_delete_view_all_permission_alone_still_forbidden():
    store = FakeStore()
    created = await links.handle_create(store, _principal(username="alice"), _request({"target_url": "https://example.com/x"}))
    slug = json.loads(created.body)["slug"]

    viewer = _principal(username="carol", permissions=["links.view_all"])
    resp = await links.handle_delete(store, viewer, slug)
    assert resp.status == 403
    assert json.loads(resp.body)["required_permission"] == "links.edit_all"


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


# --- Batched index writers (add_slugs_to_indexes / remove_slugs_from_indexes) ---


async def test_add_slugs_to_indexes_writes_all_links_and_owner_once():
    store = FakeStore()
    await links.add_slugs_to_indexes(store, "alice", ["s1", "s2", "s3"])
    assert await links._all_slugs(store) == ["s1", "s2", "s3"]
    assert await links._owned_slugs(store, "alice") == ["s1", "s2", "s3"]


async def test_add_slugs_to_indexes_skips_already_present_and_preserves_order():
    store = FakeStore()
    await links.add_slugs_to_indexes(store, "alice", ["s1"])
    await links.add_slugs_to_indexes(store, "alice", ["s1", "s2"])
    assert await links._all_slugs(store) == ["s1", "s2"]
    assert await links._owned_slugs(store, "alice") == ["s1", "s2"]


async def test_remove_slugs_from_indexes_multi_owner():
    store = FakeStore()
    await links.add_slugs_to_indexes(store, "alice", ["a1", "a2"])
    await links.add_slugs_to_indexes(store, "bob", ["b1"])

    await links.remove_slugs_from_indexes(store, {"alice": ["a1"], "bob": ["b1"]})

    assert await links._all_slugs(store) == ["a2"]
    assert await links._owned_slugs(store, "alice") == ["a2"]
    assert await links._owned_slugs(store, "bob") == []


async def test_move_slugs_between_owners_all_links_byte_identical():
    store = FakeStore()
    await links.add_slugs_to_indexes(store, "alice", ["a1", "a2"])
    before = await store.get(links.ALL_SLUGS_INDEX_KEY)

    await links.move_slugs_between_owners(store, {"alice": ["a1"]}, "bob")

    after = await store.get(links.ALL_SLUGS_INDEX_KEY)
    assert after == before


async def test_move_slugs_between_owners_same_owner_guard_leaves_slug_present():
    store = FakeStore()
    await links.add_slugs_to_indexes(store, "alice", ["a1"])

    await links.move_slugs_between_owners(store, {"alice": ["a1"]}, "alice")

    assert await links._owned_slugs(store, "alice") == ["a1"]


async def test_move_slugs_between_owners_no_duplicate_when_already_in_new_owner_index():
    store = FakeStore()
    await links.add_slugs_to_indexes(store, "alice", ["a1"])
    await links.add_slugs_to_indexes(store, "bob", ["a1"])

    await links.move_slugs_between_owners(store, {"alice": ["a1"]}, "bob")

    assert await links._owned_slugs(store, "bob") == ["a1"]
    assert await links._owned_slugs(store, "alice") == []


async def test_move_slugs_between_owners_idempotent():
    store = FakeStore()
    await links.add_slugs_to_indexes(store, "alice", ["a1", "a2"])

    await links.move_slugs_between_owners(store, {"alice": ["a1", "a2"]}, "bob")
    once = {key: value for key, value in store._data.items()}

    await links.move_slugs_between_owners(store, {"alice": ["a1", "a2"]}, "bob")
    twice = {key: value for key, value in store._data.items()}

    assert once == twice
    assert await links._owned_slugs(store, "bob") == ["a1", "a2"]
    assert await links._owned_slugs(store, "alice") == []


async def test_move_slugs_between_owners_two_old_owners_one_write_each(monkeypatch):
    store = FakeStore()
    await links.add_slugs_to_indexes(store, "alice", ["a1"])
    await links.add_slugs_to_indexes(store, "bob", ["b1"])

    set_calls = []
    original_set = store.set

    async def counting_set(key, value):
        set_calls.append(key)
        await original_set(key, value)

    monkeypatch.setattr(store, "set", counting_set)

    await links.move_slugs_between_owners(store, {"alice": ["a1"], "bob": ["b1"]}, "carol")

    assert set_calls.count("owner_links:carol") == 1
    assert set_calls.count("owner_links:alice") == 1
    assert set_calls.count("owner_links:bob") == 1
    assert "all_links" not in set_calls
    assert await links._owned_slugs(store, "carol") == ["a1", "b1"]
    assert await links._owned_slugs(store, "alice") == []
    assert await links._owned_slugs(store, "bob") == []


async def test_index_write_once_property_for_bulk_style_batch(monkeypatch):
    # Proves the design property the whole feature exists for: adding N slugs
    # in one call does exactly one set() on all_links and one per distinct
    # owner, not N.
    store = FakeStore()
    set_calls = []
    original_set = store.set

    async def counting_set(key, value):
        set_calls.append(key)
        await original_set(key, value)

    monkeypatch.setattr(store, "set", counting_set)

    slugs = [f"slug-{i}" for i in range(10)]
    await links.add_slugs_to_indexes(store, "alice", slugs)

    assert set_calls.count("all_links") == 1
    assert set_calls.count("owner_links:alice") == 1
    assert len(set_calls) == 2
