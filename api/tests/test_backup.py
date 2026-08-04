import base64
import json

import auth
import backup
from responses import Request
from tests.fakes import FakeStore, fake_list_keys


def _principal(username="admin", role="admin", permissions=None):
    return auth.Principal(username=username, role=role, permissions=permissions or [], csrf_token="x")


# --- parse_stores_param ---


def test_parse_stores_param_absent_returns_all_in_order():
    stores, error = backup.parse_stores_param(None)
    assert error is None
    assert stores == list(backup.BACKUP_STORES)


def test_parse_stores_param_subset():
    stores, error = backup.parse_stores_param("links,users")
    assert error is None
    assert stores == ["links", "users"]


def test_parse_stores_param_unknown_store():
    stores, error = backup.parse_stores_param("nope")
    assert stores is None
    assert error == {"error": "unknown_store", "store": "nope", "allowed_stores": list(backup.BACKUP_STORES)}


def test_parse_stores_param_empty_string():
    stores, error = backup.parse_stores_param("")
    assert stores is None
    assert error == {"error": "no_stores"}


def test_parse_stores_param_dedupes():
    stores, error = backup.parse_stores_param("links,links")
    assert error is None
    assert stores == ["links"]


# --- redact_user_value ---


def test_redact_user_value_strips_password_hash_keeps_other_fields():
    raw = json.dumps({
        "username": "alice",
        "password_hash": "pbkdf2_sha256$100$c2FsdA==$aGFzaA==",
        "role": "user",
        "permissions": [],
    }).encode("utf-8")
    redacted = backup.redact_user_value(raw)
    parsed = json.loads(redacted)
    assert "password_hash" not in parsed
    assert parsed["username"] == "alice"
    assert parsed["role"] == "user"
    assert parsed["permissions"] == []


def test_redact_user_value_non_json_passes_through_unchanged():
    raw = b"not json at all \xff\xfe"
    assert backup.redact_user_value(raw) == raw


def test_redact_user_value_json_but_not_object_passes_through_unchanged():
    raw = json.dumps([1, 2, 3]).encode("utf-8")
    assert backup.redact_user_value(raw) == raw


def test_redact_user_value_no_password_hash_key_passes_through_unchanged():
    raw = json.dumps({"username": "alice"}).encode("utf-8")
    assert backup.redact_user_value(raw) == raw


# --- is_excluded_key ---


def test_is_excluded_key_bootstrapped_marker_in_users():
    assert backup.is_excluded_key("users", "_meta:bootstrapped") is True


def test_is_excluded_key_session_prefix_in_users():
    assert backup.is_excluded_key("users", "session:abc123") is True


def test_is_excluded_key_session_like_key_in_links_store_not_excluded():
    """The exclusion is users-store-specific: a coincidentally-named
    "session:x" key in the *links* store must be retained, not swept up by
    a store-agnostic prefix check."""
    assert backup.is_excluded_key("links", "session:x") is False


def test_is_excluded_key_ordinary_users_key_not_excluded():
    assert backup.is_excluded_key("users", "user:alice") is False
    assert backup.is_excluded_key("users", "_meta:usernames") is False


def test_is_excluded_key_ordinary_links_key_not_excluded():
    assert backup.is_excluded_key("links", "slug:abc") is False
    assert backup.is_excluded_key("links", "_meta:bootstrapped") is False


# --- Link tags and owner reassignment: no new KV key type, so backup.py
# needs no code change — these tests prove the existing machinery already
# round-trips both. See docs/plans/link-tags-and-ownership.md's "Prove tags
# and reassignment round-trip through backup and restore" task.


def test_slug_record_with_tags_round_trips_byte_identical():
    tagged_value = json.dumps({
        "slug": "abc",
        "target_url": "https://example.com",
        "owner": "alice",
        "tags": ["q4", "sale"],
    }).encode("utf-8")
    entries = {"links": {"slug:abc": tagged_value}}
    doc = backup.build_backup(entries, created_at="x", created_by="admin", fidelity="full")

    decoded_entries_by_store, error = backup.validate_backup(doc)
    assert error is None
    assert decoded_entries_by_store["links"]["slug:abc"] == tagged_value


def test_is_excluded_key_slug_key_still_not_excluded():
    assert backup.is_excluded_key("links", "slug:x") is False


# --- build_backup ---


def test_build_backup_shape_and_metadata():
    entries = {
        "links": {"slug:abc": b'{"slug": "abc"}', "all_links": b'["abc"]'},
    }
    doc = backup.build_backup(entries, created_at="2026-08-02T00:00:00Z", created_by="admin", fidelity="full")
    assert doc["format"] == backup.BACKUP_FORMAT
    assert doc["schema_version"] == backup.SCHEMA_VERSION
    assert doc["created_at"] == "2026-08-02T00:00:00Z"
    assert doc["created_by"] == "admin"
    assert doc["fidelity"] == "full"
    assert doc["key_encoding"] == "utf8"
    assert doc["value_encoding"] == "base64"
    assert doc["counts"] == {"links": 2}
    assert set(doc["stores"].keys()) == {"links"}
    assert base64.b64decode(doc["stores"]["links"]["slug:abc"]) == b'{"slug": "abc"}'


def test_build_backup_excludes_bootstrapped_and_session_keys():
    entries = {
        "users": {
            "user:admin": json.dumps({"username": "admin", "password_hash": "x"}).encode(),
            "_meta:usernames": b'["admin"]',
            "_meta:bootstrapped": b"1",
            "session:tok123": b'{"username": "admin"}',
        },
    }
    doc = backup.build_backup(entries, created_at="2026-08-02T00:00:00Z", created_by="admin", fidelity="full")
    users_out = doc["stores"]["users"]
    assert "_meta:bootstrapped" not in users_out
    assert "session:tok123" not in users_out
    assert set(users_out.keys()) == {"user:admin", "_meta:usernames"}
    assert doc["counts"]["users"] == 2


def test_build_backup_strips_password_hash_from_user_records():
    entries = {
        "users": {
            "user:admin": json.dumps({"username": "admin", "password_hash": "secret-hash", "role": "admin"}).encode(),
        },
    }
    doc = backup.build_backup(entries, created_at="x", created_by="admin", fidelity="full")
    decoded = json.loads(base64.b64decode(doc["stores"]["users"]["user:admin"]))
    assert "password_hash" not in decoded
    assert decoded["username"] == "admin"


def test_build_backup_only_covers_stores_actually_passed_in():
    entries = {"links": {}}
    doc = backup.build_backup(entries, created_at="x", created_by="admin", fidelity="full")
    assert set(doc["stores"].keys()) == {"links"}
    assert "users" not in doc["stores"]
    assert "analytics" not in doc["stores"]


def test_build_backup_non_utf8_value_round_trips_byte_identical():
    """The load-bearing property: an arbitrary non-UTF-8 KV value must survive
    a full build_backup -> validate_backup round trip byte-for-byte."""
    weird_value = b"\xff\xfe\x00"
    entries = {"analytics": {"count:abc": weird_value}}
    doc = backup.build_backup(entries, created_at="x", created_by="admin", fidelity="full")

    payload = {"confirm": backup.RESTORE_CONFIRMATION, "backup": doc}
    decoded_entries_by_store, error = backup.validate_backup(payload["backup"])
    assert error is None
    assert decoded_entries_by_store["analytics"]["count:abc"] == weird_value


# --- validate_backup: the eleven refusal rows ---


def _good_doc(**overrides):
    doc = {
        "format": backup.BACKUP_FORMAT,
        "schema_version": backup.SCHEMA_VERSION,
        "created_at": "2026-08-02T00:00:00Z",
        "created_by": "admin",
        "fidelity": "full",
        "key_encoding": "utf8",
        "value_encoding": "base64",
        "excluded": [],
        "counts": {"links": 1},
        "stores": {"links": {"slug:abc": base64.b64encode(b'{"slug": "abc"}').decode()}},
    }
    doc.update(overrides)
    return doc


def test_validate_backup_not_a_json_object():
    decoded, error = backup.validate_backup([1, 2, 3])
    assert decoded is None
    assert error == {"error": "invalid_backup"}


def test_validate_backup_wrong_format():
    decoded, error = backup.validate_backup(_good_doc(format="something-else"))
    assert decoded is None
    assert error == {"error": "invalid_backup_format", "expected": backup.BACKUP_FORMAT}


def test_validate_backup_unsupported_schema_version():
    decoded, error = backup.validate_backup(_good_doc(schema_version=2))
    assert decoded is None
    assert error == {
        "error": "unsupported_schema_version",
        "schema_version": 2,
        "supported_versions": list(backup.SUPPORTED_SCHEMA_VERSIONS),
    }


def test_validate_backup_stores_missing():
    doc = _good_doc()
    del doc["stores"]
    decoded, error = backup.validate_backup(doc)
    assert decoded is None
    assert error == {"error": "invalid_backup"}


def test_validate_backup_stores_not_an_object():
    decoded, error = backup.validate_backup(_good_doc(stores=[]))
    assert decoded is None
    assert error == {"error": "invalid_backup"}


def test_validate_backup_stores_empty():
    decoded, error = backup.validate_backup(_good_doc(stores={}))
    assert decoded is None
    assert error == {"error": "no_stores"}


def test_validate_backup_unknown_store_name():
    decoded, error = backup.validate_backup(_good_doc(stores={"bogus": {}}))
    assert decoded is None
    assert error == {"error": "unknown_store", "store": "bogus", "allowed_stores": list(backup.BACKUP_STORES)}


def test_validate_backup_entries_not_object_of_str_to_str():
    decoded, error = backup.validate_backup(_good_doc(stores={"links": {"slug:abc": 12345}}))
    assert decoded is None
    assert error == {"error": "invalid_entries", "store": "links"}


def test_validate_backup_too_many_entries():
    huge = {f"slug:{i}": base64.b64encode(b"x").decode() for i in range(backup.MAX_BACKUP_ENTRIES + 1)}
    decoded, error = backup.validate_backup(_good_doc(stores={"links": huge}))
    assert decoded is None
    assert error == {
        "error": "too_many_entries",
        "max_entries": backup.MAX_BACKUP_ENTRIES,
        "entry_count": backup.MAX_BACKUP_ENTRIES + 1,
    }


def test_validate_backup_invalid_base64_value():
    decoded, error = backup.validate_backup(_good_doc(stores={"links": {"slug:abc": "not-valid-base64!!!"}}))
    assert decoded is None
    assert error == {"error": "invalid_value_encoding", "store": "links", "key": "slug:abc"}


def test_validate_backup_forbidden_key_bootstrapped():
    doc = _good_doc(stores={"users": {"_meta:bootstrapped": base64.b64encode(b"1").decode()}})
    decoded, error = backup.validate_backup(doc)
    assert decoded is None
    assert error == {"error": "forbidden_key", "store": "users", "key": "_meta:bootstrapped"}


def test_validate_backup_forbidden_key_session():
    doc = _good_doc(stores={"users": {"session:tok": base64.b64encode(b"{}").decode()}})
    decoded, error = backup.validate_backup(doc)
    assert decoded is None
    assert error == {"error": "forbidden_key", "store": "users", "key": "session:tok"}


def test_validate_backup_credential_material_in_backup():
    value = base64.b64encode(json.dumps({"username": "alice", "password_hash": "leaked"}).encode()).decode()
    doc = _good_doc(stores={"users": {"user:alice": value}})
    decoded, error = backup.validate_backup(doc)
    assert decoded is None
    assert error == {"error": "credential_material_in_backup", "key": "user:alice"}


def test_validate_backup_accepts_a_well_formed_file():
    decoded, error = backup.validate_backup(_good_doc())
    assert error is None
    assert decoded == {"links": {"slug:abc": b'{"slug": "abc"}'}}


# --- restore_write_order ---


def test_restore_write_order_links_all_links_last():
    keys = ["all_links", "slug:abc", "owner_links:admin", "slug:def"]
    ordered = backup.restore_write_order("links", keys)
    assert ordered == ["slug:abc", "slug:def", "all_links", "owner_links:admin"]


def test_restore_write_order_users_usernames_index_last():
    keys = ["_meta:usernames", "user:alice", "user:bob"]
    ordered = backup.restore_write_order("users", keys)
    assert ordered == ["user:alice", "user:bob", "_meta:usernames"]


def test_restore_write_order_analytics_has_no_index_keys():
    keys = ["count:abc", "events:abc:0"]
    assert backup.restore_write_order("analytics", keys) == keys


def test_restore_write_order_reassigned_links_slugs_before_both_owner_indexes_and_all_links():
    """A links store carrying two owner_links: indexes (the shape a
    reassignment leaves behind) plus tagged slug: records must still order
    every slug: write before every owner_links:/all_links write."""
    keys = ["owner_links:alice", "owner_links:bob", "all_links", "slug:abc", "slug:def"]
    ordered = backup.restore_write_order("links", keys)
    assert ordered == ["slug:abc", "slug:def", "owner_links:alice", "owner_links:bob", "all_links"]
    assert ordered.index("slug:abc") < ordered.index("owner_links:alice")
    assert ordered.index("slug:def") < ordered.index("owner_links:bob")
    assert ordered.index("slug:abc") < ordered.index("all_links")


# --- handle_export ---


async def test_handle_export_requires_users_manage():
    store = FakeStore()
    resp = await backup.handle_export(
        {"links": store}, _principal(role="user", permissions=[]), {}, fake_list_keys, 30,
    )
    assert resp.status == 403
    assert json.loads(resp.body)["required_permission"] == "users.manage"


async def test_handle_export_unknown_store_query_param():
    resp = await backup.handle_export(
        {"links": FakeStore()}, _principal(), {"stores": ["nope"]}, fake_list_keys, 30,
    )
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "unknown_store"


async def test_handle_export_empty_stores_query_param():
    resp = await backup.handle_export(
        {"links": FakeStore()}, _principal(), {"stores": [""]}, fake_list_keys, 30,
    )
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "no_stores"


async def test_handle_export_full_three_store_counts_match_stores():
    links_store = FakeStore({"slug:abc": b'{"slug": "abc"}', "all_links": b'["abc"]'})
    users_store = FakeStore({
        "user:admin": json.dumps({"username": "admin", "password_hash": "x", "role": "admin"}).encode(),
        "_meta:usernames": b'["admin"]',
        "_meta:bootstrapped": b"1",
    })
    analytics_store = FakeStore({"count:abc": b'{"total": 3}'})
    stores_by_name = {"links": links_store, "users": users_store, "analytics": analytics_store}

    resp = await backup.handle_export(stores_by_name, _principal(), {}, fake_list_keys, 30)
    assert resp.status == 200
    doc = json.loads(resp.body)
    assert doc["fidelity"] == "full"
    assert set(doc["stores"].keys()) == {"links", "users", "analytics"}
    for store_name in ("links", "users", "analytics"):
        assert doc["counts"][store_name] == len(doc["stores"][store_name])
    assert "_meta:bootstrapped" not in doc["stores"]["users"]
    decoded_admin = json.loads(base64.b64decode(doc["stores"]["users"]["user:admin"]))
    assert "password_hash" not in decoded_admin


async def test_handle_export_partial_stores_only_covers_requested():
    links_store = FakeStore({"slug:abc": b'{"slug": "abc"}'})
    users_store = FakeStore({"user:admin": b'{"username": "admin"}'})
    stores_by_name = {"links": links_store, "users": users_store}

    resp = await backup.handle_export(stores_by_name, _principal(), {"stores": ["links"]}, fake_list_keys, 30)
    doc = json.loads(resp.body)
    assert set(doc["stores"].keys()) == {"links"}


async def test_handle_export_no_credential_material_anywhere():
    """The whole design rests on this: prove it directly rather than letting
    it follow from the code shape."""
    users_store = FakeStore({
        "user:admin": json.dumps({
            "username": "admin", "password_hash": "super-secret-hash", "role": "admin",
        }).encode(),
        "session:abc123": json.dumps({"username": "admin"}).encode(),
        "_meta:bootstrapped": b"1",
        "_meta:usernames": b'["admin"]',
    })
    resp = await backup.handle_export({"users": users_store}, _principal(), {"stores": ["users"]}, fake_list_keys, 30)
    body_text = resp.body.decode("utf-8")
    assert "super-secret-hash" not in body_text
    doc = json.loads(resp.body)
    assert "_meta:bootstrapped" not in doc["stores"]["users"]
    assert "session:abc123" not in doc["stores"]["users"]
    decoded_admin = json.loads(base64.b64decode(doc["stores"]["users"]["user:admin"]))
    assert "password_hash" not in decoded_admin


async def test_handle_export_backup_too_large():
    huge_value = b"x" * (backup.MAX_BACKUP_BODY_BYTES + 1)
    links_store = FakeStore({"slug:abc": huge_value})
    resp = await backup.handle_export(
        {"links": links_store}, _principal(), {"stores": ["links"]}, fake_list_keys, 30,
    )
    assert resp.status == 500
    body = json.loads(resp.body)
    assert body["error"] == "backup_too_large"
    assert body["max_bytes"] == backup.MAX_BACKUP_BODY_BYTES


# --- handle_restore ---


def _restore_request(payload):
    return Request(method="POST", uri="/api/admin/restore", headers={}, body=json.dumps(payload).encode("utf-8"))


async def _export_doc(stores_by_name):
    query = {"stores": [",".join(stores_by_name.keys())]}
    resp = await backup.handle_export(stores_by_name, _principal(), query, fake_list_keys, 30)
    return json.loads(resp.body)


async def test_handle_restore_requires_users_manage():
    resp = await backup.handle_restore(
        {"links": FakeStore()}, _principal(role="user", permissions=[]),
        _restore_request({}), fake_list_keys, 30,
    )
    assert resp.status == 403


async def test_handle_restore_body_too_large():
    request = Request(method="POST", uri="/x", headers={}, body=b"x" * (backup.MAX_BACKUP_BODY_BYTES + 1))
    resp = await backup.handle_restore({"links": FakeStore()}, _principal(), request, fake_list_keys, 30)
    assert resp.status == 413
    assert json.loads(resp.body)["error"] == "body_too_large"


async def test_handle_restore_requires_confirmation():
    resp = await backup.handle_restore(
        {"links": FakeStore()}, _principal(), _restore_request({"backup": {}}), fake_list_keys, 30,
    )
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "confirmation_required"


async def test_handle_restore_invalid_backup_rejected():
    resp = await backup.handle_restore(
        {"links": FakeStore()}, _principal(),
        _restore_request({"confirm": "REPLACE", "backup": {"not": "valid"}}),
        fake_list_keys, 30,
    )
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_backup_format"


async def test_handle_restore_links_only_signed_out_false_users_untouched():
    links_store = FakeStore({"slug:old": b'{"slug": "old"}', "all_links": b'["old"]'})
    users_store = FakeStore({"user:admin": b'{"username": "admin"}', "_meta:usernames": b'["admin"]'})
    doc = await _export_doc({"links": links_store})

    resp = await backup.handle_restore(
        {"links": links_store, "users": users_store}, _principal(),
        _restore_request({"confirm": "REPLACE", "backup": doc}),
        fake_list_keys, 30,
    )
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["signed_out"] is False
    assert await users_store.get("user:admin") == b'{"username": "admin"}'


async def test_handle_restore_users_bearing_removes_sessions_and_bootstrap_marker():
    users_store = FakeStore({
        "user:admin": json.dumps({"username": "admin", "password_hash": "x", "role": "admin"}).encode(),
        "_meta:usernames": b'["admin"]',
        "_meta:bootstrapped": b"1",
        "session:abc": b'{"username": "admin"}',
    })
    doc = await _export_doc({"users": users_store})

    fresh_users_store = FakeStore({
        "user:admin": json.dumps({"username": "admin", "password_hash": "x", "role": "admin"}).encode(),
        "_meta:usernames": b'["admin"]',
        "_meta:bootstrapped": b"1",
        "session:abc": b'{"username": "admin"}',
    })
    resp = await backup.handle_restore(
        {"users": fresh_users_store}, _principal(),
        _restore_request({"confirm": "REPLACE", "backup": doc}),
        fake_list_keys, 30,
    )
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["signed_out"] is True
    assert await fresh_users_store.exists("session:abc") is False
    assert await fresh_users_store.exists("_meta:bootstrapped") is False


async def test_handle_restore_prunes_preexisting_key_absent_from_file():
    links_store = FakeStore({"slug:keep": b'{"slug": "keep"}', "all_links": b'["keep"]'})
    doc = await _export_doc({"links": links_store})

    fresh_links_store = FakeStore({
        "slug:keep": b'{"slug": "keep"}',
        "all_links": b'["keep"]',
        "slug:stale": b'{"slug": "stale"}',
    })
    resp = await backup.handle_restore(
        {"links": fresh_links_store}, _principal(),
        _restore_request({"confirm": "REPLACE", "backup": doc}),
        fake_list_keys, 30,
    )
    assert resp.status == 200
    assert await fresh_links_store.exists("slug:stale") is False
    assert await fresh_links_store.exists("slug:keep") is True


async def test_handle_restore_write_order_slug_before_all_links_user_before_usernames():
    write_order: list[str] = []

    class RecordingStore(FakeStore):
        async def set(self, key, value):
            write_order.append(key)
            await super().set(key, value)

    links_store = FakeStore({"slug:abc": b'{"slug": "abc"}', "all_links": b'["abc"]'})
    doc = await _export_doc({"links": links_store})

    recording_store = RecordingStore()
    resp = await backup.handle_restore(
        {"links": recording_store}, _principal(),
        _restore_request({"confirm": "REPLACE", "backup": doc}),
        fake_list_keys, 30,
    )
    assert resp.status == 200
    assert write_order.index("slug:abc") < write_order.index("all_links")


async def test_handle_restore_tags_and_reassigned_owner_indexes_slugs_write_before_indexes():
    """Integration-level counterpart to the pure restore_write_order test
    above: a real handle_restore round trip of a links store shaped like the
    aftermath of a reassignment (two owner_links: indexes, all_links, and
    two tagged slug: records) writes every slug: key before every
    owner_links:/all_links key, and the tags survive intact."""
    write_order: list[str] = []

    class RecordingStore(FakeStore):
        async def set(self, key, value):
            write_order.append(key)
            await super().set(key, value)

    links_store = FakeStore({
        "slug:abc": json.dumps({"slug": "abc", "owner": "bob", "tags": ["q4", "sale"]}).encode(),
        "slug:def": json.dumps({"slug": "def", "owner": "bob", "tags": []}).encode(),
        "owner_links:alice": b"[]",
        "owner_links:bob": b'["abc", "def"]',
        "all_links": b'["abc", "def"]',
    })
    doc = await _export_doc({"links": links_store})

    recording_store = RecordingStore()
    resp = await backup.handle_restore(
        {"links": recording_store}, _principal(),
        _restore_request({"confirm": "REPLACE", "backup": doc}),
        fake_list_keys, 30,
    )
    assert resp.status == 200
    assert write_order.index("slug:abc") < write_order.index("owner_links:alice")
    assert write_order.index("slug:abc") < write_order.index("owner_links:bob")
    assert write_order.index("slug:abc") < write_order.index("all_links")
    assert write_order.index("slug:def") < write_order.index("owner_links:bob")

    restored_abc = json.loads(await recording_store.get("slug:abc"))
    assert restored_abc["tags"] == ["q4", "sale"]


async def test_handle_restore_all_or_nothing_each_failure_leaves_stores_byte_identical():
    good_doc = await _export_doc({
        "links": FakeStore({"slug:abc": b'{"slug": "abc"}', "all_links": b'["abc"]'}),
    })

    bad_schema = json.loads(json.dumps(good_doc))
    bad_schema["schema_version"] = 2

    bad_session = json.loads(json.dumps(good_doc))
    bad_session.setdefault("stores", {}).setdefault("users", {})["session:forged"] = base64.b64encode(b"{}").decode()

    bad_password_hash = json.loads(json.dumps(good_doc))
    leaked = base64.b64encode(json.dumps({"username": "x", "password_hash": "leak"}).encode()).decode()
    bad_password_hash.setdefault("stores", {}).setdefault("users", {})["user:x"] = leaked

    bad_base64 = json.loads(json.dumps(good_doc))
    bad_base64["stores"]["links"]["slug:abc"] = "not-valid-base64!!!"

    for bad_doc in (bad_schema, bad_session, bad_password_hash, bad_base64):
        links_snapshot = {"slug:abc": b'{"slug": "abc"}', "all_links": b'["abc"]'}
        users_snapshot = {"user:admin": b'{"username": "admin"}', "_meta:usernames": b'["admin"]'}
        links_store = FakeStore(dict(links_snapshot))
        users_store = FakeStore(dict(users_snapshot))

        resp = await backup.handle_restore(
            {"links": links_store, "users": users_store}, _principal(),
            _restore_request({"confirm": "REPLACE", "backup": bad_doc}),
            fake_list_keys, 30,
        )
        assert resp.status == 400
        assert links_store._data == links_snapshot
        assert users_store._data == users_snapshot


async def test_handle_restore_missing_confirmation_leaves_stores_untouched():
    links_snapshot = {"slug:abc": b'{"slug": "abc"}'}
    links_store = FakeStore(dict(links_snapshot))
    resp = await backup.handle_restore(
        {"links": links_store}, _principal(),
        _restore_request({"backup": {"format": backup.BACKUP_FORMAT}}),
        fake_list_keys, 30,
    )
    assert resp.status == 400
    assert links_store._data == links_snapshot
