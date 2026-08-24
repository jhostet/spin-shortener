import json

import auth
import bulk
import consistency
import kvretry
import links
import tags
import urlpolicy
from responses import Request
from tests.fakes import FakeStore, ThrottlingStore, fake_get_many, fake_list_keys


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
    resp = await bulk.handle_bulk_create(store, principal, _request({"text": text}), fake_get_many, kvretry.direct)
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

    # docs/plans/derived-link-indexes.md, Stage 2: there is no index any
    # more — a record's existence, and its own owner field, are the truth.
    for slug in slugs:
        record = json.loads(await store.get(f"slug:{slug}"))
        assert record["owner"] == "alice"


async def test_bulk_create_body_too_large_rejected():
    store = FakeStore()
    big_body = b"x" * (bulk.MAX_BULK_BODY_BYTES + 1)
    resp = await bulk.handle_bulk_create(store, _principal(), _request(body=big_body), fake_get_many, kvretry.direct)
    assert resp.status == 413
    body = json.loads(resp.body)
    assert body["error"] == "body_too_large"
    assert body["max_bytes"] == bulk.MAX_BULK_BODY_BYTES


async def test_bulk_create_rejects_an_oversized_target_url_and_writes_nothing():
    """The THIRD authoring path. validate_bulk_rows takes the same choke point
    as handle_create/handle_update, because a cap enforced in two of three
    places is not enforced — CLAUDE.md's destination-URL-policy rule, which
    exists because the bulk path is exactly the one that stayed open before."""
    store = FakeStore()
    over = "https://example.com/" + "a" * links.MAX_TARGET_URL_BYTES
    text = "good,https://example.com/fine\nbad," + over

    resp = await bulk.handle_bulk_create(
        store, _principal(), _request({"text": text}), fake_get_many, kvretry.direct
    )

    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["error"] == "bulk_validation_failed"
    codes = [e["error"] for e in body["row_errors"]]
    assert "target_url_too_long" in codes
    # Validation stays all-or-nothing: the VALID row is not written either.
    assert store._data == {}


async def test_bulk_create_invalid_json():
    store = FakeStore()
    resp = await bulk.handle_bulk_create(store, _principal(), _request(body=b"not json"), fake_get_many, kvretry.direct)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_json"


async def test_bulk_create_invalid_text_type():
    store = FakeStore()
    resp = await bulk.handle_bulk_create(store, _principal(), _request({"text": 123}), fake_get_many, kvretry.direct)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_text"


async def test_bulk_create_no_rows():
    store = FakeStore()
    resp = await bulk.handle_bulk_create(store, _principal(), _request({"text": "\n\n# just a comment\n"}), fake_get_many, kvretry.direct)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "no_rows"


async def test_bulk_create_too_many_rows_carries_both_numbers():
    store = FakeStore()
    text = "\n".join(f"bulk-{i},https://example.com/{i}" for i in range(bulk.MAX_BULK_ROWS + 5))
    resp = await bulk.handle_bulk_create(store, _principal(), _request({"text": text}), fake_get_many, kvretry.direct)
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
    resp = await bulk.handle_bulk_create(store, principal, _request(payload), fake_get_many, kvretry.direct)
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
    resp = await bulk.handle_bulk_create(store, principal, _request(payload), fake_get_many, kvretry.direct)
    assert resp.status == 201
    for slug in ("foo", "bar", "baz"):
        record = json.loads(await store.get(f"slug:{slug}"))
        assert record["tags"] == ["sale"]


async def test_bulk_create_no_tags_key_gives_every_record_empty_list():
    store = FakeStore()
    text = "foo,https://example.com/a\nbar,https://example.com/b\n"
    principal = _principal(permissions=["links.create_custom_slug"])
    resp = await bulk.handle_bulk_create(store, principal, _request({"text": text}), fake_get_many, kvretry.direct)
    assert resp.status == 201
    for slug in ("foo", "bar"):
        record = json.loads(await store.get(f"slug:{slug}"))
        assert record["tags"] == []


async def test_bulk_create_invalid_batch_tag_creates_nothing():
    store = FakeStore()
    await links.handle_create(store, _principal(), _request_for_links({"target_url": "https://example.com/pre"}))
    before = {key: value for key, value in store._data.items()}

    text = "foo,https://example.com/a\n"
    resp = await bulk.handle_bulk_create(store, _principal(), _request({"text": text, "tags": ["Bad Tag"]}), fake_get_many, kvretry.direct)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_tag"

    after = {key: value for key, value in store._data.items()}
    assert after == before


async def test_bulk_create_invalid_batch_password_rejected_before_any_write():
    store = FakeStore()
    text = "foo,https://example.com/a\n"
    resp = await bulk.handle_bulk_create(store, _principal(), _request({"text": text, "password": "ab"}), fake_get_many, kvretry.direct)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_password"
    assert store._data == {}  # nothing written at all (docs/plans/derived-link-indexes.md: no index to check either)


async def test_bulk_create_invalid_window_range_rejected():
    store = FakeStore()
    text = "foo,https://example.com/a\n"
    payload = {"text": text, "start_at": "2026-02-01T00:00:00Z", "end_at": "2026-01-01T00:00:00Z"}
    resp = await bulk.handle_bulk_create(store, _principal(), _request(payload), fake_get_many, kvretry.direct)
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
    resp = await bulk.handle_bulk_create(store, principal, _request({"text": text}), fake_get_many, kvretry.direct)
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
        store, _principal(permissions=["links.create_custom_slug"]), _request({"text": text}), fake_get_many, kvretry.direct)
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
        store, _principal(permissions=["links.create_custom_slug"]), _request({"text": text}), fake_get_many, kvretry.direct)
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
        store, _principal(), _request({"text": "https://evil.example/x\n"}), fake_get_many, kvretry.direct)
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
        store, _principal(permissions=["links.create_custom_slug"]), _request({"text": text}), fake_get_many, kvretry.direct)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["row_errors"] == [{"line": 1, "slug": "drifted", "error": "slug_taken"}]


async def test_bulk_create_mixed_submission_custom_slug_forbidden_on_every_slugged_row():
    store = FakeStore()
    text = "custom-a,https://example.com/a\n,https://example.com/b\ncustom-c,https://example.com/c\n"
    resp = await bulk.handle_bulk_create(store, _principal(permissions=[]), _request({"text": text}), fake_get_many, kvretry.direct)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["error"] == "bulk_validation_failed"
    assert [e["line"] for e in body["row_errors"]] == [1, 3]
    assert all(e["error"] == "custom_slug_forbidden" for e in body["row_errors"])
    assert store._data == {}  # nothing written at all (docs/plans/derived-link-indexes.md: no index to check either)


async def test_bulk_create_ten_rows_performs_exactly_ten_kv_writes(monkeypatch):
    """docs/plans/derived-link-indexes.md, Stage 2: no index writes any
    more — 10 rows is exactly 10 record writes (12 before this change: 10
    records + all_links + owner_links:alice)."""
    store = FakeStore()
    set_calls = []
    original_set = store.set

    async def counting_set(key, value):
        set_calls.append(key)
        await original_set(key, value)

    monkeypatch.setattr(store, "set", counting_set)

    text = "\n".join(f",https://example.com/{i}" for i in range(10))
    resp = await bulk.handle_bulk_create(store, _principal(), _request({"text": text}), fake_get_many, kvretry.direct)
    assert resp.status == 201

    assert len(set_calls) == 10
    assert "all_links" not in set_calls
    assert "owner_links:alice" not in set_calls


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
    resp = await bulk.handle_bulk_action(store, FakeStore(), _principal(), _action_request({"slugs": ["a"], "action": "bogus"}), fake_get_many, kvretry.direct)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_action"


async def test_bulk_action_missing_action():
    store = FakeStore()
    resp = await bulk.handle_bulk_action(store, FakeStore(), _principal(), _action_request({"slugs": ["a"]}), fake_get_many, kvretry.direct)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_action"


async def test_bulk_action_unhandled_action_name_returns_500_and_writes_nothing(monkeypatch):
    store = FakeStore()
    slug = await _make_link(store)
    before = dict(store._data)
    monkeypatch.setattr(bulk, "BULK_ACTIONS", bulk.BULK_ACTIONS | {"bogus_but_allowed"})
    resp = await bulk.handle_bulk_action(
        store, FakeStore(), _principal(), _action_request({"slugs": [slug], "action": "bogus_but_allowed"}), fake_get_many, kvretry.direct)
    assert resp.status == 500
    assert json.loads(resp.body) == {"error": "unhandled_action", "action": "bogus_but_allowed"}
    assert store._data == before  # nothing deleted — the hazard this guard exists for


async def test_bulk_action_no_slugs_empty_list():
    store = FakeStore()
    resp = await bulk.handle_bulk_action(store, FakeStore(), _principal(), _action_request({"slugs": [], "action": "delete"}), fake_get_many, kvretry.direct)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "no_slugs"


async def test_bulk_action_no_slugs_not_a_list():
    store = FakeStore()
    resp = await bulk.handle_bulk_action(store, FakeStore(), _principal(), _action_request({"slugs": "a", "action": "delete"}), fake_get_many, kvretry.direct)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "no_slugs"


async def test_bulk_action_no_slugs_non_string_member():
    store = FakeStore()
    resp = await bulk.handle_bulk_action(store, FakeStore(), _principal(), _action_request({"slugs": ["a", 1], "action": "delete"}), fake_get_many, kvretry.direct)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "no_slugs"


async def test_bulk_action_duplicate_slug_rejected():
    store = FakeStore()
    resp = await bulk.handle_bulk_action(store, FakeStore(), _principal(), _action_request({"slugs": ["a", "a"], "action": "delete"}), fake_get_many, kvretry.direct)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "duplicate_slug"


async def test_bulk_action_too_many_slugs_carries_both_numbers():
    store = FakeStore()
    slugs = [f"s{i}" for i in range(bulk.MAX_BULK_ROWS + 3)]
    resp = await bulk.handle_bulk_action(store, FakeStore(), _principal(), _action_request({"slugs": slugs, "action": "delete"}), fake_get_many, kvretry.direct)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["error"] == "too_many_rows"
    assert body["max_rows"] == bulk.MAX_BULK_ROWS
    assert body["row_count"] == bulk.MAX_BULK_ROWS + 3


async def test_bulk_action_not_found_row_error():
    store = FakeStore()
    resp = await bulk.handle_bulk_action(store, FakeStore(), _principal(), _action_request({"slugs": ["missing"], "action": "delete"}), fake_get_many, kvretry.direct)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["error"] == "bulk_validation_failed"
    assert body["row_errors"] == [{"slug": "missing", "error": "not_found"}]


async def test_bulk_action_forbidden_row_error():
    store = FakeStore()
    slug = await _make_link(store, owner="alice")
    resp = await bulk.handle_bulk_action(
        store, FakeStore(), _principal(username="bob"), _action_request({"slugs": [slug], "action": "delete"}), fake_get_many, kvretry.direct)
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
        _action_request({"slugs": [good_slug, "missing"], "action": "delete"}), fake_get_many, kvretry.direct)
    assert resp.status == 400
    after = {key: value for key, value in store._data.items()}
    assert after == before


async def test_bulk_action_enable_disable_round_trip():
    store = FakeStore()
    slug1 = await _make_link(store, owner="alice")
    slug2 = await _make_link(store, owner="alice")

    resp = await bulk.handle_bulk_action(
        store, FakeStore(), _principal(username="alice"), _action_request({"slugs": [slug1, slug2], "action": "disable"}), fake_get_many, kvretry.direct)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body == {"ok": True, "action": "disable", "count": 2}
    for slug in (slug1, slug2):
        record = json.loads(await store.get(f"slug:{slug}"))
        assert record["status"] == "disabled"


    resp2 = await bulk.handle_bulk_action(
        store, FakeStore(), _principal(username="alice"), _action_request({"slugs": [slug1, slug2], "action": "enable"}), fake_get_many, kvretry.direct)
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
        store, FakeStore(), editor, _action_request({"slugs": [alice_slug, bob_slug], "action": "delete"}), fake_get_many, kvretry.direct)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body == {"ok": True, "action": "delete", "count": 2}

    assert await store.exists(f"slug:{alice_slug}") is False
    assert await store.exists(f"slug:{bob_slug}") is False
    # docs/plans/derived-link-indexes.md, Stage 2: no index to update, so
    # deleting both records leaves the store empty.
    assert store._data == {}


async def test_bulk_action_delete_writes_exactly_one_delete_per_slug_no_index_writes(monkeypatch):
    """docs/plans/derived-link-indexes.md, Stage 2: bulk delete no longer
    writes any index — one `delete` per slug and zero `set` calls."""
    store = FakeStore()
    alice_slugs = [await _make_link(store, owner="alice") for _ in range(3)]
    bob_slugs = [await _make_link(store, owner="bob") for _ in range(2)]

    set_calls = []
    delete_calls = []
    original_set, original_delete = store.set, store.delete

    async def counting_set(key, value):
        set_calls.append(key)
        await original_set(key, value)

    async def counting_delete(key):
        delete_calls.append(key)
        await original_delete(key)

    monkeypatch.setattr(store, "set", counting_set)
    monkeypatch.setattr(store, "delete", counting_delete)

    editor = _principal(username="dave", permissions=["links.edit_all"])
    resp = await bulk.handle_bulk_action(
        store, FakeStore(), editor, _action_request({"slugs": alice_slugs + bob_slugs, "action": "delete"}), fake_get_many, kvretry.direct)
    assert resp.status == 200
    assert set_calls == []
    assert sorted(delete_calls) == sorted(f"slug:{s}" for s in alice_slugs + bob_slugs)


# --- handle_bulk_action: tag / untag ---


async def test_bulk_action_tag_requires_links_tag_permission():
    store = FakeStore()
    slug = await _make_link(store, owner="alice")
    resp = await bulk.handle_bulk_action(
        store, FakeStore(), _principal(username="alice"),
        _action_request({"slugs": [slug], "action": "tag", "tags": ["sale"]}), fake_get_many, kvretry.direct)
    assert resp.status == 403
    assert json.loads(resp.body) == {"error": "forbidden", "required_permission": "links.tag"}
    record = json.loads(await store.get(f"slug:{slug}"))
    assert record["tags"] == []


async def test_bulk_action_tag_no_tags_rejected():
    store = FakeStore()
    slug = await _make_link(store, owner="alice")
    tagger = _principal(username="alice", permissions=["links.tag"])
    resp = await bulk.handle_bulk_action(
        store, FakeStore(), tagger, _action_request({"slugs": [slug], "action": "tag", "tags": []}), fake_get_many, kvretry.direct)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "no_tags"


async def test_bulk_action_tag_invalid_tag_rejected():
    store = FakeStore()
    slug = await _make_link(store, owner="alice")
    tagger = _principal(username="alice", permissions=["links.tag"])
    resp = await bulk.handle_bulk_action(
        store, FakeStore(), tagger, _action_request({"slugs": [slug], "action": "tag", "tags": ["Bad Tag"]}), fake_get_many, kvretry.direct)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_tag"


async def test_bulk_action_tag_holder_without_edit_all_forbidden_on_others_link_and_writes_nothing():
    store = FakeStore()
    alice_slug = await _make_link(store, owner="alice")
    tagger = _principal(username="bob", permissions=["links.tag"])
    before = {key: value for key, value in store._data.items()}

    resp = await bulk.handle_bulk_action(
        store, FakeStore(), tagger, _action_request({"slugs": [alice_slug], "action": "tag", "tags": ["sale"]}), fake_get_many, kvretry.direct)
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
        store, FakeStore(), tagger, _action_request({"slugs": [slug], "action": "tag", "tags": ["Q4", "SALE"]}), fake_get_many, kvretry.direct)
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
        store, FakeStore(), tagger, _action_request({"slugs": [slug], "action": "tag", "tags": ["sale"]}), fake_get_many, kvretry.direct)
    assert resp.status == 200
    record = json.loads(await store.get(f"slug:{slug}"))
    assert record["tags"] == ["sale"]


async def test_bulk_action_untag_absent_tag_is_a_no_op_returning_200():
    store = FakeStore()
    slug = await _make_link(store, owner="alice", tags=["sale"])
    tagger = _principal(username="alice", permissions=["links.tag"])

    resp = await bulk.handle_bulk_action(
        store, FakeStore(), tagger, _action_request({"slugs": [slug], "action": "untag", "tags": ["nope"]}), fake_get_many, kvretry.direct)
    assert resp.status == 200
    record = json.loads(await store.get(f"slug:{slug}"))
    assert record["tags"] == ["sale"]


async def test_bulk_action_untag_removes_tag():
    store = FakeStore()
    slug = await _make_link(store, owner="alice", tags=["sale", "q4"])
    tagger = _principal(username="alice", permissions=["links.tag"])

    resp = await bulk.handle_bulk_action(
        store, FakeStore(), tagger, _action_request({"slugs": [slug], "action": "untag", "tags": ["q4"]}), fake_get_many, kvretry.direct)
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
        _action_request({"slugs": [over_cap_slug, fine_slug], "action": "tag", "tags": ["one-more", "two-more"]}), fake_get_many, kvretry.direct)
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
        store, FakeStore(), manager, _action_request({"slugs": [slug], "action": "reassign"}), fake_get_many, kvretry.direct)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_owner"


async def test_bulk_action_reassign_unknown_owner_writes_nothing():
    store = FakeStore()
    slug = await _make_link(store, owner="alice")
    before = {key: value for key, value in store._data.items()}
    manager = _principal(username="mgr", permissions=["users.manage"])

    resp = await bulk.handle_bulk_action(
        store, FakeStore(), manager, _action_request({"slugs": [slug], "action": "reassign", "owner": "ghost"}), fake_get_many, kvretry.direct)
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
        _action_request({"slugs": [slug], "action": "reassign", "owner": "bob"}), fake_get_many, kvretry.direct)
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
        _action_request({"slugs": [slug], "action": "reassign", "owner": "bob"}), fake_get_many, kvretry.direct)
    fake = await bulk.handle_bulk_action(
        store, users_store, _principal(username="alice"),
        _action_request({"slugs": [slug], "action": "reassign", "owner": "nobody-here"}), fake_get_many, kvretry.direct)
    assert real.status == fake.status == 403
    assert real.body == fake.body


async def test_bulk_action_reassign_disabled_user_is_an_acceptable_target():
    store = FakeStore()
    slug = await _make_link(store, owner="alice")
    users_store = FakeStore()
    await _seed_user(users_store, "bob", disabled=True)
    manager = _principal(username="mgr", permissions=["users.manage"])

    resp = await bulk.handle_bulk_action(
        store, users_store, manager, _action_request({"slugs": [slug], "action": "reassign", "owner": "bob"}), fake_get_many, kvretry.direct)
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
        _action_request({"slugs": [alice_slug, "missing"], "action": "reassign", "owner": "bob"}), fake_get_many, kvretry.direct)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body == {"error": "bulk_validation_failed", "row_errors": [{"slug": "missing", "error": "not_found"}]}
    # Nothing written — this is still all-or-nothing.
    record = json.loads(await store.get(f"slug:{alice_slug}"))
    assert record["owner"] == "alice"


async def test_bulk_action_reassign_success_updates_owner_field_only():
    """docs/plans/derived-link-indexes.md, Stage 2: reassign is now a pure
    record rewrite — no owner index to update, so no all_links key at all."""
    store = FakeStore()
    alice_slug = await _make_link(store, owner="alice")
    users_store = FakeStore()
    await _seed_user(users_store, "bob")
    manager = _principal(username="mgr", permissions=["users.manage"])

    resp = await bulk.handle_bulk_action(
        store, users_store, manager,
        _action_request({"slugs": [alice_slug], "action": "reassign", "owner": "bob"}), fake_get_many, kvretry.direct)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body == {"ok": True, "action": "reassign", "count": 1, "owner": "bob"}

    record = json.loads(await store.get(f"slug:{alice_slug}"))
    assert record["owner"] == "bob"
    assert "all_links" not in store._data
    assert not any(k.startswith("owner_links:") for k in store._data)


async def test_bulk_action_reassign_two_old_owners_updates_both_records():
    store = FakeStore()
    alice_slug = await _make_link(store, owner="alice")
    bob_slug = await _make_link(store, owner="bob")
    users_store = FakeStore()
    await _seed_user(users_store, "carol")
    manager = _principal(username="mgr", permissions=["users.manage"])

    resp = await bulk.handle_bulk_action(
        store, users_store, manager,
        _action_request({"slugs": [alice_slug, bob_slug], "action": "reassign", "owner": "carol"}), fake_get_many, kvretry.direct)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body == {"ok": True, "action": "reassign", "count": 2, "owner": "carol"}

    for slug in (alice_slug, bob_slug):
        record = json.loads(await store.get(f"slug:{slug}"))
        assert record["owner"] == "carol"


async def test_bulk_action_delete_leaves_analytics_untouched():
    """docs/plans/inline-analytics-purge-on-delete.md's Trade-offs #5: bulk
    delete's rejected inline-purge arithmetic (50 slugs x 95 keys ~= 95-123s
    against a 30s handler limit) stands, so bulk delete must keep leaving
    orphan analytics keys behind for the existing operator tool
    (analyticsorphans.handle_orphan_purge) to clean up — unlike single-link
    delete, which now purges inline (api/links.py's handle_delete)."""
    store = FakeStore()
    slug1 = await _make_link(store, owner="alice")
    slug2 = await _make_link(store, owner="alice")

    analytics_keys = {
        f"count:{slug1}:1": b'{"total": 9, "days": {}}',
        f"count:{slug1}:2": b'{"total": 1, "days": {}}',
        f"events:{slug1}:5": b"1700000000000|referrer|desktop",
        f"count:{slug2}:1": b'{"total": 3, "days": {}}',
    }
    for key, value in analytics_keys.items():
        await store.set(key, value)

    resp = await bulk.handle_bulk_action(
        store, FakeStore(), _principal(username="alice"),
        _action_request({"slugs": [slug1, slug2], "action": "delete"}), fake_get_many, kvretry.direct)
    assert resp.status == 200
    assert json.loads(resp.body) == {"ok": True, "action": "delete", "count": 2}

    assert await store.exists(f"slug:{slug1}") is False
    assert await store.exists(f"slug:{slug2}") is False
    for key, value in analytics_keys.items():
        assert await store.get(key) == value


# --- Write-throttle resilience (docs/plans/write-throttle-resilience.md) ---


async def test_bulk_create_throttled_32nd_record_write_reports_exactly_what_landed():
    """docs/plans/derived-link-indexes.md, Stage 2: there is no index write
    left for this scenario to exercise (the whole class of test the old
    test_bulk_create_throttled_index_write_... pinned no longer applies —
    there is no index to fail to update). What remains is the record-write
    retry: the 32nd record write is throttled past RECORD_WRITE's 3-attempt
    budget, the loop abandons the remaining 19 rows, and every one of the 31
    that landed is a real, independently-listed slug: record — nothing about
    it depends on an index that no longer exists."""
    from tests.fakes import recording_sleep

    store = ThrottlingStore(fail_times={"slug:s32": 10})
    sleep, _ = recording_sleep()
    write = kvretry.make_writer(sleep)

    text = "\n".join(f"s{i:02d},https://example.com/{i}" for i in range(1, 51))
    principal = _principal(permissions=["links.create_custom_slug"])
    resp = await bulk.handle_bulk_create(store, principal, _request({"text": text}), fake_get_many, write)

    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["ok"] is False
    assert body["partial"] is True
    assert body["count"] == 31
    assert len(body["not_created"]) == 19
    assert all(row["error"] == "write_failed" for row in body["not_created"])
    assert body["not_created"][0] == {"line": 32, "slug": "s32", "error": "write_failed"}
    assert body["next_step"] == "resubmit"
    assert "index_updated" not in body

    for link in body["links"]:
        assert await store.exists(f"slug:{link['slug']}") is True


async def test_bulk_create_all_writes_succeed_response_byte_identical_to_today():
    from tests.fakes import recording_sleep

    store = FakeStore()
    sleep, delays = recording_sleep()
    write = kvretry.make_writer(sleep)

    text = "ok-one,https://example.com/a\n"
    principal = _principal(permissions=["links.create_custom_slug"])
    resp = await bulk.handle_bulk_create(store, principal, _request({"text": text}), fake_get_many, write)

    assert resp.status == 201
    body = json.loads(resp.body)
    assert set(body.keys()) == {"count", "links"}
    assert delays == []  # nothing ever retried


async def test_bulk_create_single_row_performs_exactly_one_kv_write(monkeypatch):
    """docs/plans/derived-link-indexes.md, Stage 2: a bulk create of one row
    is exactly one KV write (the record) — there is no index write left."""
    store = FakeStore()
    set_calls = []
    original_set = store.set

    async def counting_set(key, value):
        set_calls.append(key)
        await original_set(key, value)

    monkeypatch.setattr(store, "set", counting_set)

    text = "ok-one,https://example.com/a\n"
    principal = _principal(permissions=["links.create_custom_slug"])
    resp = await bulk.handle_bulk_create(store, principal, _request({"text": text}), fake_get_many, kvretry.direct)
    assert resp.status == 201
    assert set_calls == ["slug:ok-one"]


async def test_bulk_create_fifty_rows_performs_exactly_fifty_kv_writes(monkeypatch):
    """docs/plans/derived-link-indexes.md, Stage 2: a 50-row bulk create is
    exactly 50 KV writes (52 before this change: 50 records + all_links +
    owner_links:<owner>)."""
    store = FakeStore()
    set_calls = []
    original_set = store.set

    async def counting_set(key, value):
        set_calls.append(key)
        await original_set(key, value)

    monkeypatch.setattr(store, "set", counting_set)

    text = "\n".join(f"s{i:02d},https://example.com/{i}" for i in range(1, 51))
    principal = _principal(permissions=["links.create_custom_slug"])
    resp = await bulk.handle_bulk_create(store, principal, _request({"text": text}), fake_get_many, kvretry.direct)
    assert resp.status == 201
    assert len(set_calls) == 50
    assert sorted(set_calls) == sorted(f"slug:s{i:02d}" for i in range(1, 51))


async def test_bulk_create_validation_still_all_or_nothing_writes_nothing():
    from tests.fakes import recording_sleep

    store = ThrottlingStore(fail_times={"slug:s1": 10})
    sleep, _ = recording_sleep()
    write = kvretry.make_writer(sleep)

    text = "bad-row\n"  # missing target_url -> validation error, never reaches a write
    resp = await bulk.handle_bulk_create(store, _principal(), _request({"text": text}), fake_get_many, write)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "bulk_validation_failed"
    assert store._data == {}


async def test_bulk_action_delete_throttled_leaves_the_undeleted_records_intact():
    """docs/plans/derived-link-indexes.md, Stage 2: there is no index to
    check for drift any more — what matters is that the records the loop
    never reached are still there, byte-identical, and the ones it did
    reach are gone."""
    from tests.fakes import recording_sleep

    store = ThrottlingStore()
    slugs = [await _make_link(store, owner="alice") for _ in range(4)]
    # Fail the 3rd delete persistently.
    store._fail_times[f"slug:{slugs[2]}"] = 10
    sleep, _ = recording_sleep()
    write = kvretry.make_writer(sleep)

    resp = await bulk.handle_bulk_action(
        store, FakeStore(), _principal(username="alice"),
        _action_request({"slugs": slugs, "action": "delete"}), fake_get_many, write)

    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["ok"] is False
    assert body["partial"] is True
    assert body["action"] == "delete"
    assert set(body["applied"]) == {slugs[0], slugs[1]}
    assert set(body["not_applied"]) == {slugs[2], slugs[3]}
    assert body["next_step"] == "resubmit"
    assert "index_updated" not in body

    assert await store.exists(f"slug:{slugs[0]}") is False
    assert await store.exists(f"slug:{slugs[1]}") is False
    assert await store.exists(f"slug:{slugs[2]}") is True
    assert await store.exists(f"slug:{slugs[3]}") is True


async def test_bulk_action_enable_disable_report_partial_with_no_index_field():
    """enable/disable never had an index step; Stage 2 just removes the
    (always-true) index_updated field from the response entirely."""
    from tests.fakes import recording_sleep

    store = ThrottlingStore()
    slug1 = await _make_link(store, owner="alice")
    slug2 = await _make_link(store, owner="alice")
    store._fail_times[f"slug:{slug2}"] = 10  # second write fails; loop breaks there
    sleep, _ = recording_sleep()
    write = kvretry.make_writer(sleep)

    resp = await bulk.handle_bulk_action(
        store, FakeStore(), _principal(username="alice"),
        _action_request({"slugs": [slug1, slug2], "action": "disable"}), fake_get_many, write)

    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["partial"] is True
    assert "index_updated" not in body
    assert body["applied"] == [slug1]
    assert body["not_applied"] == [slug2]


async def test_bulk_action_tag_untag_report_partial_with_no_index_field():
    from tests.fakes import recording_sleep

    store = ThrottlingStore()
    slug1 = await _make_link(store, owner="alice")
    slug2 = await _make_link(store, owner="alice")
    store._fail_times[f"slug:{slug2}"] = 10  # second write fails; loop breaks there
    sleep, _ = recording_sleep()
    write = kvretry.make_writer(sleep)

    resp = await bulk.handle_bulk_action(
        store, FakeStore(), _principal(username="alice", permissions=["links.tag"]),
        _action_request({"slugs": [slug1, slug2], "action": "tag", "tags": ["sale"]}), fake_get_many, write)

    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["partial"] is True
    assert "index_updated" not in body
    assert body["applied"] == [slug1]
    assert body["not_applied"] == [slug2]
    assert body["tags"] == ["sale"]
