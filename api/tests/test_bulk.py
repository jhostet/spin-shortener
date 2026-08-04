import json

import auth
import bulk
import links
import tags
import urlpolicy
from responses import Request
from tests.fakes import FakeStore


def _principal(username="alice", role="user", permissions=None):
    return auth.Principal(username=username, role=role, permissions=permissions or [], csrf_token="x")


async def _set_policy(store, default_action, rules):
    policy, error = urlpolicy.parse_policy_document(
        {"default_action": default_action, "rules": rules}, now="2026-08-04T00:00:00Z", actor="admin",
    )
    assert error is None
    await urlpolicy.save_policy(store, policy)


def _request(payload=None, body=None):
    if body is None:
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    return Request(method="POST", uri="/api/links/bulk", headers={}, body=body)


# --- parse_bulk_text ---


def test_parse_worked_example_from_plan():
    text = (
        "﻿Slug,Destination URL\n"
        "black-friday,https://tirerack.com/promo?a=1,2\n"
        ",https://tirerack.com/other\n"
        "\n"
        "# holiday campaign\n"
        "xmas-2026\thttps://tirerack.com/xmas\n"
        "https://tirerack.com/plain\n"
    )
    rows = bulk.parse_bulk_text(text)
    assert [(r.line, r.slug, r.target_url) for r in rows] == [
        (2, "black-friday", "https://tirerack.com/promo?a=1,2"),
        (3, None, "https://tirerack.com/other"),
        (6, "xmas-2026", "https://tirerack.com/xmas"),
        (7, None, "https://tirerack.com/plain"),
    ]


def test_parse_bom_crlf_and_header_row_stripped():
    text = "﻿slug,destination\r\nfoo,https://example.com/a\r\n"
    rows = bulk.parse_bulk_text(text)
    assert len(rows) == 1
    assert rows[0].slug == "foo"
    assert rows[0].target_url == "https://example.com/a"
    assert rows[0].line == 2  # physical line number, header was line 1


def test_parse_lone_cr_normalized():
    text = "foo,https://example.com/a\rbar,https://example.com/b\r"
    rows = bulk.parse_bulk_text(text)
    assert [r.slug for r in rows] == ["foo", "bar"]


def test_parse_destination_with_comma_and_explicit_slug():
    rows = bulk.parse_bulk_text("black-friday,https://x.com/p?ids=1,2")
    assert len(rows) == 1
    assert rows[0].slug == "black-friday"
    assert rows[0].target_url == "https://x.com/p?ids=1,2"


def test_parse_destination_with_comma_no_slug():
    # A URL is recognized whole (rule 6) before any comma-splitting happens.
    rows = bulk.parse_bulk_text("https://x.com/p?ids=1,2")
    assert len(rows) == 1
    assert rows[0].slug is None
    assert rows[0].target_url == "https://x.com/p?ids=1,2"


def test_parse_tab_delimited_row():
    rows = bulk.parse_bulk_text("my-slug\thttps://example.com/x")
    assert len(rows) == 1
    assert rows[0].slug == "my-slug"
    assert rows[0].target_url == "https://example.com/x"


def test_parse_comment_line_skipped():
    rows = bulk.parse_bulk_text("# a comment\nfoo,https://example.com/a")
    assert len(rows) == 1
    assert rows[0].slug == "foo"


def test_parse_trailing_newline_produces_no_extra_row():
    rows = bulk.parse_bulk_text("foo,https://example.com/a\n")
    assert len(rows) == 1


def test_parse_blank_lines_skipped():
    rows = bulk.parse_bulk_text("foo,https://example.com/a\n\n\nbar,https://example.com/b\n")
    assert len(rows) == 2


def test_parse_quoted_destination_with_comma_dequoted():
    rows = bulk.parse_bulk_text('foo,"https://example.com/a,b"')
    assert rows[0].target_url == "https://example.com/a,b"


def test_parse_single_token_matching_slug_pattern_is_slug_with_empty_destination():
    rows = bulk.parse_bulk_text("bad-row")
    assert rows[0].slug == "bad-row"
    assert rows[0].target_url == ""


def test_parse_single_token_not_matching_slug_pattern_is_destination():
    rows = bulk.parse_bulk_text("not a url or slug!!")
    assert rows[0].slug is None
    assert rows[0].target_url == "not a url or slug!!"


def test_parse_blank_slug_field_means_auto_generate():
    rows = bulk.parse_bulk_text(",https://example.com/a")
    assert rows[0].slug is None


def test_parse_header_word_outside_set_is_not_dropped():
    # "banana" isn't in HEADER_WORDS, so this line is treated as real data
    # (and will fail validation as an invalid URL, which is the intended
    # behaviour per the plan).
    rows = bulk.parse_bulk_text("banana,not-a-real-header-word")
    assert len(rows) == 1
    assert rows[0].slug == "banana"


def test_parse_no_header_when_first_row_is_data():
    rows = bulk.parse_bulk_text("foo,https://example.com/a\nbar,https://example.com/b")
    assert len(rows) == 2


# --- validate_bulk_rows ---


def _row(line, slug, target_url):
    return bulk.BulkRow(line=line, slug=slug, target_url=target_url)


def test_validate_all_valid_rows_returns_no_errors():
    rows = [_row(1, "foo", "https://example.com/a"), _row(2, None, "https://example.com/b")]
    errors = bulk.validate_bulk_rows(rows, existing_slugs=set(), can_custom_slug=True, policy=urlpolicy.EMPTY_POLICY)
    assert errors == []


def test_validate_invalid_target_url():
    rows = [_row(1, None, "not-a-url")]
    errors = bulk.validate_bulk_rows(rows, existing_slugs=set(), can_custom_slug=True, policy=urlpolicy.EMPTY_POLICY)
    assert errors == [{"line": 1, "slug": None, "error": "invalid_target_url"}]


def test_validate_missing_target_url():
    rows = [_row(1, "bad-row", "")]
    errors = bulk.validate_bulk_rows(rows, existing_slugs=set(), can_custom_slug=True, policy=urlpolicy.EMPTY_POLICY)
    assert errors == [{"line": 1, "slug": "bad-row", "error": "missing_target_url"}]


def test_validate_invalid_custom_slug():
    rows = [_row(1, "no", "https://example.com/a")]  # too short
    errors = bulk.validate_bulk_rows(rows, existing_slugs=set(), can_custom_slug=True, policy=urlpolicy.EMPTY_POLICY)
    assert errors == [{"line": 1, "slug": "no", "error": "invalid_custom_slug"}]


def test_validate_custom_slug_forbidden():
    rows = [_row(1, "custom-slug", "https://example.com/a")]
    errors = bulk.validate_bulk_rows(rows, existing_slugs=set(), can_custom_slug=False, policy=urlpolicy.EMPTY_POLICY)
    assert errors == [{"line": 1, "slug": "custom-slug", "error": "custom_slug_forbidden"}]


def test_validate_custom_slug_forbidden_mixed_submission_flags_every_slugged_row():
    rows = [
        _row(1, "custom-a", "https://example.com/a"),
        _row(2, None, "https://example.com/b"),
        _row(3, "custom-b", "https://example.com/c"),
    ]
    errors = bulk.validate_bulk_rows(rows, existing_slugs=set(), can_custom_slug=False, policy=urlpolicy.EMPTY_POLICY)
    assert [e["line"] for e in errors] == [1, 3]
    assert all(e["error"] == "custom_slug_forbidden" for e in errors)


def test_validate_slug_taken():
    rows = [_row(1, "taken-slug", "https://example.com/a")]
    errors = bulk.validate_bulk_rows(rows, existing_slugs={"taken-slug"}, can_custom_slug=True, policy=urlpolicy.EMPTY_POLICY)
    assert errors == [{"line": 1, "slug": "taken-slug", "error": "slug_taken"}]


def test_validate_duplicate_slug_in_submission_carries_first_line():
    rows = [
        _row(1, "dup", "https://a.com"),
        _row(2, "dup", "https://b.com"),
    ]
    errors = bulk.validate_bulk_rows(rows, existing_slugs=set(), can_custom_slug=True, policy=urlpolicy.EMPTY_POLICY)
    assert errors == [{"line": 2, "slug": "dup", "error": "duplicate_slug_in_submission", "first_line": 1}]


def test_validate_case_sensitive_duplicate_detection():
    rows = [
        _row(1, "Sale", "https://a.com"),
        _row(2, "sale", "https://b.com"),
    ]
    errors = bulk.validate_bulk_rows(rows, existing_slugs=set(), can_custom_slug=True, policy=urlpolicy.EMPTY_POLICY)
    assert errors == []


def test_validate_one_error_per_row_in_precedence_order():
    # A row with both an invalid slug and a taken-slug condition should only
    # report the higher-precedence invalid_custom_slug.
    rows = [_row(1, "no", "https://example.com/a")]
    errors = bulk.validate_bulk_rows(rows, existing_slugs={"no"}, can_custom_slug=True, policy=urlpolicy.EMPTY_POLICY)
    assert len(errors) == 1
    assert errors[0]["error"] == "invalid_custom_slug"


def test_action_statuses_values_subset_of_link_statuses():
    assert set(bulk.ACTION_STATUSES.values()) <= set(links.LINK_STATUSES)


# --- handle_bulk_create ---


async def test_bulk_create_success_shared_created_at_and_public_shape():
    store = FakeStore()
    text = "black-friday,https://example.com/a\n,https://example.com/b\n"
    principal = _principal(permissions=["links.create_custom_slug"])
    resp = await bulk.handle_bulk_create(store, principal, _request({"text": text}))
    assert resp.status == 201
    body = json.loads(resp.body)
    assert body["count"] == 2
    slugs = {link["slug"] for link in body["links"]}
    assert "black-friday" in slugs
    assert len(slugs) == 2
    created_ats = {link["created_at"] for link in body["links"]}
    assert len(created_ats) == 1  # one shared iso_now()
    for link in body["links"]:
        assert "password_hash" not in link
        assert link["password_protected"] is False

    all_slugs = await links._all_slugs(store)
    assert set(all_slugs) == slugs
    assert set(await links.owned_slugs(store, "alice")) == slugs


async def test_bulk_create_body_too_large_rejected():
    store = FakeStore()
    big_body = b"x" * (bulk.MAX_BULK_BODY_BYTES + 1)
    resp = await bulk.handle_bulk_create(store, _principal(), _request(body=big_body))
    assert resp.status == 413
    body = json.loads(resp.body)
    assert body["error"] == "body_too_large"
    assert body["max_bytes"] == bulk.MAX_BULK_BODY_BYTES


async def test_bulk_create_invalid_json():
    store = FakeStore()
    resp = await bulk.handle_bulk_create(store, _principal(), _request(body=b"not json"))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_json"


async def test_bulk_create_invalid_text_type():
    store = FakeStore()
    resp = await bulk.handle_bulk_create(store, _principal(), _request({"text": 123}))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_text"


async def test_bulk_create_no_rows():
    store = FakeStore()
    resp = await bulk.handle_bulk_create(store, _principal(), _request({"text": "\n\n# just a comment\n"}))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "no_rows"


async def test_bulk_create_too_many_rows_carries_both_numbers():
    store = FakeStore()
    text = "\n".join(f"bulk-{i},https://example.com/{i}" for i in range(bulk.MAX_BULK_ROWS + 5))
    resp = await bulk.handle_bulk_create(store, _principal(), _request({"text": text}))
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["error"] == "too_many_rows"
    assert body["max_rows"] == bulk.MAX_BULK_ROWS
    assert body["row_count"] == bulk.MAX_BULK_ROWS + 5


async def test_bulk_create_batch_password_and_window_applied_to_every_row():
    store = FakeStore()
    text = "foo,https://example.com/a\nbar,https://example.com/b\n"
    payload = {
        "text": text,
        "password": "longenough",
        "start_at": "2026-01-01T00:00:00Z",
        "end_at": "2026-02-01T00:00:00Z",
    }
    principal = _principal(permissions=["links.create_custom_slug"])
    resp = await bulk.handle_bulk_create(store, principal, _request(payload))
    assert resp.status == 201
    for slug in ("foo", "bar"):
        record = json.loads(await store.get(f"slug:{slug}"))
        assert record["start_at"] == "2026-01-01T00:00:00Z"
        assert record["end_at"] == "2026-02-01T00:00:00Z"
        assert auth.verify_password("longenough", record["password_hash"])


async def test_bulk_create_batch_tags_applied_to_every_row():
    store = FakeStore()
    text = "foo,https://example.com/a\nbar,https://example.com/b\nbaz,https://example.com/c\n"
    payload = {"text": text, "tags": ["SALE"]}
    principal = _principal(permissions=["links.create_custom_slug"])
    resp = await bulk.handle_bulk_create(store, principal, _request(payload))
    assert resp.status == 201
    for slug in ("foo", "bar", "baz"):
        record = json.loads(await store.get(f"slug:{slug}"))
        assert record["tags"] == ["sale"]


async def test_bulk_create_no_tags_key_gives_every_record_empty_list():
    store = FakeStore()
    text = "foo,https://example.com/a\nbar,https://example.com/b\n"
    principal = _principal(permissions=["links.create_custom_slug"])
    resp = await bulk.handle_bulk_create(store, principal, _request({"text": text}))
    assert resp.status == 201
    for slug in ("foo", "bar"):
        record = json.loads(await store.get(f"slug:{slug}"))
        assert record["tags"] == []


async def test_bulk_create_invalid_batch_tag_creates_nothing():
    store = FakeStore()
    await links.handle_create(store, _principal(), _request_for_links({"target_url": "https://example.com/pre"}))
    before = {key: value for key, value in store._data.items()}

    text = "foo,https://example.com/a\n"
    resp = await bulk.handle_bulk_create(store, _principal(), _request({"text": text, "tags": ["Bad Tag"]}))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_tag"

    after = {key: value for key, value in store._data.items()}
    assert after == before


async def test_bulk_create_invalid_batch_password_rejected_before_any_write():
    store = FakeStore()
    text = "foo,https://example.com/a\n"
    resp = await bulk.handle_bulk_create(store, _principal(), _request({"text": text, "password": "ab"}))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_password"
    assert await links._all_slugs(store) == []


async def test_bulk_create_invalid_window_range_rejected():
    store = FakeStore()
    text = "foo,https://example.com/a\n"
    payload = {"text": text, "start_at": "2026-02-01T00:00:00Z", "end_at": "2026-01-01T00:00:00Z"}
    resp = await bulk.handle_bulk_create(store, _principal(), _request(payload))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_window_range"


async def test_bulk_create_all_or_nothing_leaves_store_unchanged():
    store = FakeStore()
    # Seed an existing link so we can prove nothing about the store's
    # pre-existing indexes changes either.
    await links.handle_create(store, _principal(), _request_for_links({"target_url": "https://example.com/pre"}))
    before = {key: value for key, value in store._data.items()}

    text = "good-one,https://example.com/a\nbad-row\n"
    principal = _principal(permissions=["links.create_custom_slug"])
    resp = await bulk.handle_bulk_create(store, principal, _request({"text": text}))
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["error"] == "bulk_validation_failed"
    assert body["row_errors"] == [{"line": 2, "slug": "bad-row", "error": "missing_target_url"}]

    after = {key: value for key, value in store._data.items()}
    assert after == before


async def test_bulk_create_slug_taken_reported_with_line_number():
    store = FakeStore()
    await links.handle_create(store, _principal(permissions=["links.create_custom_slug"]), _request_for_links({
        "target_url": "https://example.com/x", "custom_slug": "existing-slug",
    }))

    text = "ok-one,https://example.com/a\nexisting-slug,https://example.com/b\n"
    resp = await bulk.handle_bulk_create(
        store, _principal(permissions=["links.create_custom_slug"]), _request({"text": text}),
    )
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["row_errors"] == [{"line": 2, "slug": "existing-slug", "error": "slug_taken"}]


# --- Destination URL policy enforcement ---


async def test_bulk_create_one_violating_row_writes_nothing():
    """Bulk stays all-or-nothing: one violating row means nothing is
    written and every problem is reported, never a partial result."""
    store = FakeStore()
    await _set_policy(store, "allow", [{"host": "evil.example", "action": "deny"}])
    before = dict(store._data)

    text = "good-one,https://example.com/a\nbad-one,https://evil.example/b\n"
    resp = await bulk.handle_bulk_create(
        store, _principal(permissions=["links.create_custom_slug"]), _request({"text": text}),
    )
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["error"] == "bulk_validation_failed"
    assert body["row_errors"] == [
        {"line": 2, "slug": "bad-one", "error": "destination_not_allowed", "host": "evil.example", "reason": "denied_by_rule"},
    ]

    after = dict(store._data)
    assert after == before
    assert await store.exists("slug:good-one") is False
    assert await store.exists("slug:bad-one") is False


async def test_bulk_create_rejects_the_same_destination_the_single_link_path_rejects():
    store = FakeStore()
    await _set_policy(store, "allow", [{"host": "evil.example", "action": "deny"}])

    single_resp = await links.handle_create(
        store, _principal(), _request_for_links({"target_url": "https://evil.example/x"}),
    )
    assert single_resp.status == 400
    assert json.loads(single_resp.body)["error"] == "destination_not_allowed"

    bulk_resp = await bulk.handle_bulk_create(
        store, _principal(), _request({"text": "https://evil.example/x\n"}),
    )
    assert bulk_resp.status == 400
    body = json.loads(bulk_resp.body)
    assert body["row_errors"][0]["error"] == "destination_not_allowed"


async def test_bulk_create_index_drift_confirmation_catches_stale_all_links():
    # all_links is an index, not the truth. If a slug: record exists but
    # all_links somehow doesn't list it (drift), the store.exists()
    # confirmation must still catch it and refuse to overwrite it.
    store = FakeStore()
    await store.set("slug:drifted", json.dumps({
        "slug": "drifted", "target_url": "https://example.com/x", "owner": "someone-else",
        "custom": True, "password_hash": None, "status": "active",
        "start_at": None, "end_at": None, "created_at": "x", "updated_at": "x",
    }).encode("utf-8"))
    # Deliberately do NOT add "drifted" to all_links, simulating drift.

    text = "drifted,https://example.com/new\n"
    resp = await bulk.handle_bulk_create(
        store, _principal(permissions=["links.create_custom_slug"]), _request({"text": text}),
    )
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["row_errors"] == [{"line": 1, "slug": "drifted", "error": "slug_taken"}]


async def test_bulk_create_mixed_submission_custom_slug_forbidden_on_every_slugged_row():
    store = FakeStore()
    text = "custom-a,https://example.com/a\n,https://example.com/b\ncustom-c,https://example.com/c\n"
    resp = await bulk.handle_bulk_create(store, _principal(permissions=[]), _request({"text": text}))
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["error"] == "bulk_validation_failed"
    assert [e["line"] for e in body["row_errors"]] == [1, 3]
    assert all(e["error"] == "custom_slug_forbidden" for e in body["row_errors"])
    assert await links._all_slugs(store) == []


async def test_bulk_create_writes_indexes_exactly_once(monkeypatch):
    store = FakeStore()
    set_calls = []
    original_set = store.set

    async def counting_set(key, value):
        set_calls.append(key)
        await original_set(key, value)

    monkeypatch.setattr(store, "set", counting_set)

    text = "\n".join(f",https://example.com/{i}" for i in range(10))
    resp = await bulk.handle_bulk_create(store, _principal(), _request({"text": text}))
    assert resp.status == 201

    assert set_calls.count("all_links") == 1
    assert set_calls.count("owner_links:alice") == 1


def _request_for_links(payload):
    return Request(method="POST", uri="/api/links", headers={}, body=json.dumps(payload).encode("utf-8"))


async def _make_link(store, owner="alice", **extra_payload):
    payload = {"target_url": "https://example.com/x", **extra_payload}
    created = await links.handle_create(store, _principal(username=owner), _request_for_links(payload))
    return json.loads(created.body)["slug"]


def _action_request(payload):
    return Request(method="POST", uri="/api/links/bulk-action", headers={}, body=json.dumps(payload).encode("utf-8"))


# --- handle_bulk_action ---


async def test_bulk_action_invalid_action():
    store = FakeStore()
    resp = await bulk.handle_bulk_action(store, FakeStore(), _principal(), _action_request({"slugs": ["a"], "action": "bogus"}))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_action"


async def test_bulk_action_missing_action():
    store = FakeStore()
    resp = await bulk.handle_bulk_action(store, FakeStore(), _principal(), _action_request({"slugs": ["a"]}))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_action"


async def test_bulk_action_no_slugs_empty_list():
    store = FakeStore()
    resp = await bulk.handle_bulk_action(store, FakeStore(), _principal(), _action_request({"slugs": [], "action": "delete"}))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "no_slugs"


async def test_bulk_action_no_slugs_not_a_list():
    store = FakeStore()
    resp = await bulk.handle_bulk_action(store, FakeStore(), _principal(), _action_request({"slugs": "a", "action": "delete"}))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "no_slugs"


async def test_bulk_action_no_slugs_non_string_member():
    store = FakeStore()
    resp = await bulk.handle_bulk_action(store, FakeStore(), _principal(), _action_request({"slugs": ["a", 1], "action": "delete"}))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "no_slugs"


async def test_bulk_action_duplicate_slug_rejected():
    store = FakeStore()
    resp = await bulk.handle_bulk_action(store, FakeStore(), _principal(), _action_request({"slugs": ["a", "a"], "action": "delete"}))
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "duplicate_slug"


async def test_bulk_action_too_many_slugs_carries_both_numbers():
    store = FakeStore()
    slugs = [f"s{i}" for i in range(bulk.MAX_BULK_ROWS + 3)]
    resp = await bulk.handle_bulk_action(store, FakeStore(), _principal(), _action_request({"slugs": slugs, "action": "delete"}))
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["error"] == "too_many_rows"
    assert body["max_rows"] == bulk.MAX_BULK_ROWS
    assert body["row_count"] == bulk.MAX_BULK_ROWS + 3


async def test_bulk_action_not_found_row_error():
    store = FakeStore()
    resp = await bulk.handle_bulk_action(store, FakeStore(), _principal(), _action_request({"slugs": ["missing"], "action": "delete"}))
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["error"] == "bulk_validation_failed"
    assert body["row_errors"] == [{"slug": "missing", "error": "not_found"}]


async def test_bulk_action_forbidden_row_error():
    store = FakeStore()
    slug = await _make_link(store, owner="alice")
    resp = await bulk.handle_bulk_action(
        store, FakeStore(), _principal(username="bob"), _action_request({"slugs": [slug], "action": "delete"}),
    )
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["error"] == "bulk_validation_failed"
    assert body["row_errors"] == [{"slug": slug, "error": "forbidden"}]


async def test_bulk_action_all_or_nothing_leaves_store_unchanged():
    store = FakeStore()
    good_slug = await _make_link(store, owner="alice")
    before = {key: value for key, value in store._data.items()}

    resp = await bulk.handle_bulk_action(
        store, FakeStore(), _principal(username="alice"),
        _action_request({"slugs": [good_slug, "missing"], "action": "delete"}),
    )
    assert resp.status == 400
    after = {key: value for key, value in store._data.items()}
    assert after == before


async def test_bulk_action_enable_disable_round_trip():
    store = FakeStore()
    slug1 = await _make_link(store, owner="alice")
    slug2 = await _make_link(store, owner="alice")

    resp = await bulk.handle_bulk_action(
        store, FakeStore(), _principal(username="alice"), _action_request({"slugs": [slug1, slug2], "action": "disable"}),
    )
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body == {"ok": True, "action": "disable", "count": 2}
    for slug in (slug1, slug2):
        record = json.loads(await store.get(f"slug:{slug}"))
        assert record["status"] == "disabled"

    # Indexes are untouched by enable/disable.
    assert set(await links.owned_slugs(store, "alice")) == {slug1, slug2}

    resp2 = await bulk.handle_bulk_action(
        store, FakeStore(), _principal(username="alice"), _action_request({"slugs": [slug1, slug2], "action": "enable"}),
    )
    assert resp2.status == 200
    for slug in (slug1, slug2):
        record = json.loads(await store.get(f"slug:{slug}"))
        assert record["status"] == "active"


async def test_bulk_action_delete_cross_owner_by_edit_all_updates_both_owner_indexes():
    store = FakeStore()
    alice_slug = await _make_link(store, owner="alice")
    bob_slug = await _make_link(store, owner="bob")

    editor = _principal(username="dave", permissions=["links.edit_all"])
    resp = await bulk.handle_bulk_action(
        store, FakeStore(), editor, _action_request({"slugs": [alice_slug, bob_slug], "action": "delete"}),
    )
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body == {"ok": True, "action": "delete", "count": 2}

    assert await store.exists(f"slug:{alice_slug}") is False
    assert await store.exists(f"slug:{bob_slug}") is False
    assert alice_slug not in await links.owned_slugs(store, "alice")
    assert bob_slug not in await links.owned_slugs(store, "bob")
    assert await links._all_slugs(store) == []


async def test_bulk_action_delete_writes_indexes_exactly_once_per_owner(monkeypatch):
    store = FakeStore()
    alice_slugs = [await _make_link(store, owner="alice") for _ in range(3)]
    bob_slugs = [await _make_link(store, owner="bob") for _ in range(2)]

    set_calls = []
    original_set = store.set

    async def counting_set(key, value):
        set_calls.append(key)
        await original_set(key, value)

    monkeypatch.setattr(store, "set", counting_set)

    editor = _principal(username="dave", permissions=["links.edit_all"])
    resp = await bulk.handle_bulk_action(
        store, FakeStore(), editor, _action_request({"slugs": alice_slugs + bob_slugs, "action": "delete"}),
    )
    assert resp.status == 200
    assert set_calls.count("all_links") == 1
    assert set_calls.count("owner_links:alice") == 1
    assert set_calls.count("owner_links:bob") == 1


# --- handle_bulk_action: tag / untag ---


async def test_bulk_action_tag_requires_links_tag_permission():
    store = FakeStore()
    slug = await _make_link(store, owner="alice")
    resp = await bulk.handle_bulk_action(
        store, FakeStore(), _principal(username="alice"),
        _action_request({"slugs": [slug], "action": "tag", "tags": ["sale"]}),
    )
    assert resp.status == 403
    assert json.loads(resp.body) == {"error": "forbidden", "required_permission": "links.tag"}
    record = json.loads(await store.get(f"slug:{slug}"))
    assert record["tags"] == []


async def test_bulk_action_tag_no_tags_rejected():
    store = FakeStore()
    slug = await _make_link(store, owner="alice")
    tagger = _principal(username="alice", permissions=["links.tag"])
    resp = await bulk.handle_bulk_action(
        store, FakeStore(), tagger, _action_request({"slugs": [slug], "action": "tag", "tags": []}),
    )
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "no_tags"


async def test_bulk_action_tag_invalid_tag_rejected():
    store = FakeStore()
    slug = await _make_link(store, owner="alice")
    tagger = _principal(username="alice", permissions=["links.tag"])
    resp = await bulk.handle_bulk_action(
        store, FakeStore(), tagger, _action_request({"slugs": [slug], "action": "tag", "tags": ["Bad Tag"]}),
    )
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_tag"


async def test_bulk_action_tag_holder_without_edit_all_forbidden_on_others_link_and_writes_nothing():
    store = FakeStore()
    alice_slug = await _make_link(store, owner="alice")
    tagger = _principal(username="bob", permissions=["links.tag"])
    before = {key: value for key, value in store._data.items()}

    resp = await bulk.handle_bulk_action(
        store, FakeStore(), tagger, _action_request({"slugs": [alice_slug], "action": "tag", "tags": ["sale"]}),
    )
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["error"] == "bulk_validation_failed"
    assert body["row_errors"] == [{"slug": alice_slug, "error": "forbidden"}]

    after = {key: value for key, value in store._data.items()}
    assert after == before


async def test_bulk_action_tag_success_normalizes_bumps_updated_at_and_carries_tags():
    store = FakeStore()
    slug = await _make_link(store, owner="alice")
    tagger = _principal(username="alice", permissions=["links.tag"])
    before = json.loads(await store.get(f"slug:{slug}"))

    resp = await bulk.handle_bulk_action(
        store, FakeStore(), tagger, _action_request({"slugs": [slug], "action": "tag", "tags": ["Q4", "SALE"]}),
    )
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body == {"ok": True, "action": "tag", "count": 1, "tags": ["q4", "sale"]}

    record = json.loads(await store.get(f"slug:{slug}"))
    assert record["tags"] == ["q4", "sale"]
    assert record["updated_at"] >= before["updated_at"]


async def test_bulk_action_tag_re_tagging_produces_no_duplicate():
    store = FakeStore()
    slug = await _make_link(store, owner="alice", tags=["sale"])
    tagger = _principal(username="alice", permissions=["links.tag"])

    resp = await bulk.handle_bulk_action(
        store, FakeStore(), tagger, _action_request({"slugs": [slug], "action": "tag", "tags": ["sale"]}),
    )
    assert resp.status == 200
    record = json.loads(await store.get(f"slug:{slug}"))
    assert record["tags"] == ["sale"]


async def test_bulk_action_untag_absent_tag_is_a_no_op_returning_200():
    store = FakeStore()
    slug = await _make_link(store, owner="alice", tags=["sale"])
    tagger = _principal(username="alice", permissions=["links.tag"])

    resp = await bulk.handle_bulk_action(
        store, FakeStore(), tagger, _action_request({"slugs": [slug], "action": "untag", "tags": ["nope"]}),
    )
    assert resp.status == 200
    record = json.loads(await store.get(f"slug:{slug}"))
    assert record["tags"] == ["sale"]


async def test_bulk_action_untag_removes_tag():
    store = FakeStore()
    slug = await _make_link(store, owner="alice", tags=["sale", "q4"])
    tagger = _principal(username="alice", permissions=["links.tag"])

    resp = await bulk.handle_bulk_action(
        store, FakeStore(), tagger, _action_request({"slugs": [slug], "action": "untag", "tags": ["q4"]}),
    )
    assert resp.status == 200
    record = json.loads(await store.get(f"slug:{slug}"))
    assert record["tags"] == ["sale"]


async def test_bulk_action_tag_cap_violation_on_one_slug_leaves_store_byte_identical():
    store = FakeStore()
    nine_tags = [f"tag{i}" for i in range(9)]
    over_cap_slug = await _make_link(store, owner="alice", tags=nine_tags)
    fine_slug = await _make_link(store, owner="alice")
    tagger = _principal(username="alice", permissions=["links.tag"])
    before = {key: value for key, value in store._data.items()}

    resp = await bulk.handle_bulk_action(
        store, FakeStore(), tagger,
        _action_request({"slugs": [over_cap_slug, fine_slug], "action": "tag", "tags": ["one-more", "two-more"]}),
    )
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["error"] == "bulk_validation_failed"
    assert body["row_errors"] == [{"slug": over_cap_slug, "error": "too_many_tags", "max_tags": tags.MAX_TAGS_PER_LINK}]

    after = {key: value for key, value in store._data.items()}
    assert after == before


# --- handle_bulk_action: reassign ---


async def _seed_user(users_store, username, disabled=False):
    await auth.put_user(users_store, {
        "username": username,
        "password_hash": None,
        "role": "user",
        "permissions": [],
        "disabled": disabled,
        "provider": "local",
    })


async def test_bulk_action_reassign_requires_owner_field():
    store = FakeStore()
    slug = await _make_link(store, owner="alice")
    manager = _principal(username="mgr", permissions=["users.manage"])
    resp = await bulk.handle_bulk_action(
        store, FakeStore(), manager, _action_request({"slugs": [slug], "action": "reassign"}),
    )
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_owner"


async def test_bulk_action_reassign_unknown_owner_writes_nothing():
    store = FakeStore()
    slug = await _make_link(store, owner="alice")
    before = {key: value for key, value in store._data.items()}
    manager = _principal(username="mgr", permissions=["users.manage"])

    resp = await bulk.handle_bulk_action(
        store, FakeStore(), manager, _action_request({"slugs": [slug], "action": "reassign", "owner": "ghost"}),
    )
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body == {"error": "unknown_owner", "owner": "ghost"}
    after = {key: value for key, value in store._data.items()}
    assert after == before


async def test_bulk_action_reassign_requires_users_manage_permission():
    store = FakeStore()
    slug = await _make_link(store, owner="alice")
    users_store = FakeStore()
    await _seed_user(users_store, "bob")

    resp = await bulk.handle_bulk_action(
        store, users_store, _principal(username="alice"),
        _action_request({"slugs": [slug], "action": "reassign", "owner": "bob"}),
    )
    assert resp.status == 403
    assert json.loads(resp.body) == {"error": "forbidden", "required_permission": "users.manage"}
    record = json.loads(await store.get(f"slug:{slug}"))
    assert record["owner"] == "alice"


async def test_bulk_action_reassign_without_permission_cannot_distinguish_a_real_owner_from_a_fake_one():
    """The permission check must run BEFORE the owner lookup. If it ran after,
    a caller without users.manage would get 400 unknown_owner for a name that
    does not exist and 403 forbidden for one that does — enumerating the very
    username list GET /api/users gates on this same permission."""
    store = FakeStore()
    slug = await _make_link(store, owner="alice")
    users_store = FakeStore()
    await _seed_user(users_store, "bob")

    real = await bulk.handle_bulk_action(
        store, users_store, _principal(username="alice"),
        _action_request({"slugs": [slug], "action": "reassign", "owner": "bob"}),
    )
    fake = await bulk.handle_bulk_action(
        store, users_store, _principal(username="alice"),
        _action_request({"slugs": [slug], "action": "reassign", "owner": "nobody-here"}),
    )
    assert real.status == fake.status == 403
    assert real.body == fake.body


async def test_bulk_action_reassign_disabled_user_is_an_acceptable_target():
    store = FakeStore()
    slug = await _make_link(store, owner="alice")
    users_store = FakeStore()
    await _seed_user(users_store, "bob", disabled=True)
    manager = _principal(username="mgr", permissions=["users.manage"])

    resp = await bulk.handle_bulk_action(
        store, users_store, manager, _action_request({"slugs": [slug], "action": "reassign", "owner": "bob"}),
    )
    assert resp.status == 200
    record = json.loads(await store.get(f"slug:{slug}"))
    assert record["owner"] == "bob"


async def test_bulk_action_reassign_skips_per_row_can_edit_but_keeps_not_found():
    store = FakeStore()
    alice_slug = await _make_link(store, owner="alice")
    users_store = FakeStore()
    await _seed_user(users_store, "bob")
    manager = _principal(username="mgr", permissions=["users.manage"])  # no links.edit_all

    resp = await bulk.handle_bulk_action(
        store, users_store, manager,
        _action_request({"slugs": [alice_slug, "missing"], "action": "reassign", "owner": "bob"}),
    )
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body == {"error": "bulk_validation_failed", "row_errors": [{"slug": "missing", "error": "not_found"}]}
    # Nothing written — this is still all-or-nothing.
    record = json.loads(await store.get(f"slug:{alice_slug}"))
    assert record["owner"] == "alice"


async def test_bulk_action_reassign_success_updates_owner_and_indexes_all_links_unchanged():
    store = FakeStore()
    alice_slug = await _make_link(store, owner="alice")
    users_store = FakeStore()
    await _seed_user(users_store, "bob")
    manager = _principal(username="mgr", permissions=["users.manage"])
    all_links_before = await store.get(links.ALL_SLUGS_INDEX_KEY)

    resp = await bulk.handle_bulk_action(
        store, users_store, manager,
        _action_request({"slugs": [alice_slug], "action": "reassign", "owner": "bob"}),
    )
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body == {"ok": True, "action": "reassign", "count": 1, "owner": "bob"}

    record = json.loads(await store.get(f"slug:{alice_slug}"))
    assert record["owner"] == "bob"
    assert alice_slug in await links.owned_slugs(store, "bob")
    assert alice_slug not in await links.owned_slugs(store, "alice")
    assert await store.get(links.ALL_SLUGS_INDEX_KEY) == all_links_before


async def test_bulk_action_reassign_two_old_owners_updates_both_old_indexes_and_new_one():
    store = FakeStore()
    alice_slug = await _make_link(store, owner="alice")
    bob_slug = await _make_link(store, owner="bob")
    users_store = FakeStore()
    await _seed_user(users_store, "carol")
    manager = _principal(username="mgr", permissions=["users.manage"])

    resp = await bulk.handle_bulk_action(
        store, users_store, manager,
        _action_request({"slugs": [alice_slug, bob_slug], "action": "reassign", "owner": "carol"}),
    )
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body == {"ok": True, "action": "reassign", "count": 2, "owner": "carol"}

    assert set(await links.owned_slugs(store, "carol")) == {alice_slug, bob_slug}
    assert await links.owned_slugs(store, "alice") == []
    assert await links.owned_slugs(store, "bob") == []
