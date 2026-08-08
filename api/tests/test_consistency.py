"""Unit tests for api/consistency.py's pure logic: each of the twelve checks
in isolation, the three skip rules, the per-check cap, the all-clear state,
the permission gate, and the credential-leak guarantee.

Task-2's test_consistency_scenarios.py covers the same twelve checks again,
but seeded through the real handlers (or FakeStores built the same way) and
asserting that every OTHER check stays at count 0 for that scenario — the
"a checker that cries wolf is worse than none" property. This file is the
narrower, faster unit layer: does each check fire correctly, in isolation,
with hand-built store contents.
"""

import json

import auth
import consistency
from tests.fakes import FakeStore, fake_list_keys


def _principal(username="admin", role="admin", permissions=None):
    return auth.Principal(username=username, role=role, permissions=permissions or [], csrf_token="x")


def _j(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


async def _analyze(links_data=None, users_data=None):
    links_store = FakeStore(links_data or {})
    users_store = FakeStore(users_data or {})
    collected = await consistency.collect({"links": links_store, "users": users_store}, fake_list_keys)
    return consistency.analyze(collected)


def _by_id(checks):
    return {c["check"]: c for c in checks}


# --- CHECKS tuple ---


def test_checks_tuple_has_the_twelve_ids_and_severities_in_order():
    assert consistency.CHECKS == (
        ("unindexed_link", "warning"),
        ("missing_link_record", "info"),
        ("unindexed_owner_link", "warning"),
        ("owner_index_mismatch", "warning"),
        ("orphan_owner_index_entry", "info"),
        ("unknown_link_owner", "warning"),
        ("dangling_owner_index", "warning"),
        ("unindexed_user", "warning"),
        ("missing_user_record", "info"),
        ("orphan_session", "warning"),
        ("unreadable_value", "warning"),
        ("unrecognized_key", "info"),
    )


# --- Each check, in isolation ---


async def test_unindexed_link():
    checks, totals = await _analyze(
        links_data={
            "slug:foo": _j({"owner": "carol"}),
            "all_links": _j([]),
            "owner_links:carol": _j(["foo"]),
        },
        users_data={"user:carol": _j({}), "_meta:usernames": _j(["carol"])},
    )
    by_id = _by_id(checks)
    assert by_id["unindexed_link"]["count"] == 1
    assert by_id["unindexed_link"]["findings"] == [{"slug": "foo", "owner": "carol"}]
    assert totals["findings"] == 1


async def test_missing_link_record():
    checks, totals = await _analyze(links_data={"all_links": _j(["ghost"])})
    by_id = _by_id(checks)
    assert by_id["missing_link_record"]["count"] == 1
    assert by_id["missing_link_record"]["findings"] == [{"slug": "ghost"}]
    assert totals["findings"] == 1


async def test_unindexed_owner_link():
    checks, _ = await _analyze(
        links_data={
            "slug:spring-sale": _j({"owner": "carol"}),
            "all_links": _j(["spring-sale"]),
            "owner_links:carol": _j([]),  # drifted out
        },
        users_data={"user:carol": _j({}), "_meta:usernames": _j(["carol"])},
    )
    by_id = _by_id(checks)
    assert by_id["unindexed_owner_link"]["count"] == 1
    assert by_id["unindexed_owner_link"]["findings"] == [{"slug": "spring-sale", "owner": "carol"}]


async def test_unindexed_owner_link_fires_when_index_key_absent_entirely():
    checks, _ = await _analyze(
        links_data={
            "slug:spring-sale": _j({"owner": "carol"}),
            "all_links": _j(["spring-sale"]),
            # no owner_links:carol key at all
        },
        users_data={"user:carol": _j({}), "_meta:usernames": _j(["carol"])},
    )
    by_id = _by_id(checks)
    assert by_id["unindexed_owner_link"]["count"] == 1


async def test_owner_index_mismatch():
    checks, _ = await _analyze(
        links_data={
            "slug:x": _j({"owner": "dave"}),
            "all_links": _j(["x"]),
            "owner_links:carol": _j(["x"]),
            "owner_links:dave": _j(["x"]),
        },
        users_data={
            "user:carol": _j({}), "user:dave": _j({}),
            "_meta:usernames": _j(["carol", "dave"]),
        },
    )
    by_id = _by_id(checks)
    assert by_id["owner_index_mismatch"]["count"] == 1
    assert by_id["owner_index_mismatch"]["findings"] == [
        {"slug": "x", "indexed_under": "carol", "record_owner": "dave"}
    ]
    # dave's own index correctly lists x, so unindexed_owner_link stays clean.
    assert by_id["unindexed_owner_link"]["count"] == 0


async def test_orphan_owner_index_entry():
    checks, _ = await _analyze(
        links_data={"owner_links:carol": _j(["ghost"]), "all_links": _j([])},
        users_data={"user:carol": _j({}), "_meta:usernames": _j(["carol"])},
    )
    by_id = _by_id(checks)
    assert by_id["orphan_owner_index_entry"]["count"] == 1
    assert by_id["orphan_owner_index_entry"]["findings"] == [{"slug": "ghost", "indexed_under": "carol"}]


async def test_unknown_link_owner():
    checks, _ = await _analyze(
        links_data={"slug:x": _j({"owner": "nobody"}), "all_links": _j(["x"])},
        users_data={"_meta:usernames": _j([])},
    )
    by_id = _by_id(checks)
    assert by_id["unknown_link_owner"]["count"] == 1
    assert by_id["unknown_link_owner"]["findings"] == [{"slug": "x", "owner": "nobody"}]


async def test_dangling_owner_index():
    checks, _ = await _analyze(
        links_data={"owner_links:nobody": _j(["ghost"]), "all_links": _j([])},
    )
    by_id = _by_id(checks)
    assert by_id["dangling_owner_index"]["count"] == 1
    assert by_id["dangling_owner_index"]["findings"] == [{"username": "nobody", "slug_count": 1}]


async def test_dangling_owner_index_never_fires_on_empty_index():
    checks, _ = await _analyze(links_data={"owner_links:nobody": _j([])})
    by_id = _by_id(checks)
    assert by_id["dangling_owner_index"]["count"] == 0


async def test_unindexed_user():
    checks, _ = await _analyze(users_data={"user:alice": _j({}), "_meta:usernames": _j([])})
    by_id = _by_id(checks)
    assert by_id["unindexed_user"]["count"] == 1
    assert by_id["unindexed_user"]["findings"] == [{"username": "alice"}]


async def test_missing_user_record():
    checks, _ = await _analyze(users_data={"_meta:usernames": _j(["ghost"])})
    by_id = _by_id(checks)
    assert by_id["missing_user_record"]["count"] == 1
    assert by_id["missing_user_record"]["findings"] == [{"username": "ghost"}]


async def test_orphan_session():
    checks, _ = await _analyze(
        users_data={
            "session:tok1": _j({"username": "nobody", "csrf_token": "x", "issued_at": 0, "expires_at": 99999999999}),
            "_meta:usernames": _j([]),
        },
    )
    by_id = _by_id(checks)
    assert by_id["orphan_session"]["count"] == 1
    assert by_id["orphan_session"]["findings"] == [{"username": "nobody", "session_count": 1}]


async def test_orphan_session_groups_by_username_and_never_emits_the_token():
    checks, _ = await _analyze(
        users_data={
            "session:tok1": _j({"username": "nobody", "csrf_token": "x", "issued_at": 0, "expires_at": 99999999999}),
            "session:tok2": _j({"username": "nobody", "csrf_token": "y", "issued_at": 0, "expires_at": 99999999999}),
            "_meta:usernames": _j([]),
        },
    )
    by_id = _by_id(checks)
    assert by_id["orphan_session"]["count"] == 1
    assert by_id["orphan_session"]["findings"] == [{"username": "nobody", "session_count": 2}]
    report = json.dumps(by_id["orphan_session"])
    assert "tok1" not in report and "tok2" not in report


async def test_unreadable_value_slug_record_not_json():
    checks, _ = await _analyze(links_data={"slug:bad": b"not-json"})
    by_id = _by_id(checks)
    assert by_id["unreadable_value"]["count"] == 1
    assert by_id["unreadable_value"]["findings"] == [{"store": "links", "key": "slug:bad"}]
    # The unreadable slug is excluded from every other check, not just noted.
    assert by_id["unknown_link_owner"]["count"] == 0
    assert by_id["unindexed_link"]["count"] == 0


async def test_unreadable_value_slug_record_missing_owner_field():
    checks, _ = await _analyze(links_data={"slug:bad": _j({"target_url": "https://example.com"})})
    by_id = _by_id(checks)
    assert by_id["unreadable_value"]["count"] == 1
    assert by_id["unreadable_value"]["findings"] == [{"store": "links", "key": "slug:bad"}]


async def test_unrecognized_key():
    checks, _ = await _analyze(links_data={"junk": b"whatever"})
    by_id = _by_id(checks)
    assert by_id["unrecognized_key"]["count"] == 1
    assert by_id["unrecognized_key"]["findings"] == [{"store": "links", "key": "junk"}]


async def test_unrecognized_key_in_users_store():
    checks, _ = await _analyze(users_data={"junk": b"whatever"})
    by_id = _by_id(checks)
    assert by_id["unrecognized_key"]["count"] == 1
    assert by_id["unrecognized_key"]["findings"] == [{"store": "users", "key": "junk"}]


# --- _meta:url_policy: a known shape, not an unrecognized key ---


async def test_url_policy_key_present_and_valid_is_not_unrecognized_or_unreadable():
    checks, _ = await _analyze(
        links_data={consistency.URL_POLICY_KEY: _j({"default_action": "allow", "rules": []})}
    )
    by_id = _by_id(checks)
    assert by_id["unrecognized_key"]["count"] == 0
    assert by_id["unreadable_value"]["count"] == 0


async def test_url_policy_key_corrupted_reports_unreadable_value():
    checks, _ = await _analyze(links_data={consistency.URL_POLICY_KEY: b"not json"})
    by_id = _by_id(checks)
    assert by_id["unrecognized_key"]["count"] == 0
    assert by_id["unreadable_value"]["count"] == 1
    assert by_id["unreadable_value"]["findings"] == [{"store": "links", "key": consistency.URL_POLICY_KEY}]


# --- Skip rules ---


async def test_all_links_unreadable_skips_checks_1_and_2():
    checks, totals = await _analyze(links_data={"all_links": b"not-json", "slug:x": _j({"owner": "carol"})})
    by_id = _by_id(checks)
    assert by_id["unindexed_link"]["skipped"] is True
    assert by_id["unindexed_link"]["count"] == 0
    assert by_id["missing_link_record"]["skipped"] is True
    assert by_id["missing_link_record"]["count"] == 0
    assert by_id["unreadable_value"]["findings"] == [{"store": "links", "key": "all_links"}]
    assert totals["checks_skipped"] == 2


async def test_meta_usernames_unreadable_skips_checks_8_and_9():
    checks, totals = await _analyze(users_data={"_meta:usernames": b"not-json"})
    by_id = _by_id(checks)
    assert by_id["unindexed_user"]["skipped"] is True
    assert by_id["missing_user_record"]["skipped"] is True
    assert by_id["unreadable_value"]["findings"] == [{"store": "users", "key": "_meta:usernames"}]
    assert totals["checks_skipped"] == 2


async def test_unreadable_owner_links_excludes_only_that_owner_not_globally():
    checks, totals = await _analyze(
        links_data={
            "owner_links:carol": b"not-json",
            "slug:x": _j({"owner": "carol"}),
            "slug:y": _j({"owner": "dave"}),
            "all_links": _j(["x", "y"]),
            "owner_links:dave": _j(["y"]),
        },
        users_data={"user:carol": _j({}), "user:dave": _j({}), "_meta:usernames": _j(["carol", "dave"])},
    )
    by_id = _by_id(checks)
    # Carol's own drift can't be assessed (her index is unreadable), so she
    # is excluded rather than reported as a false-positive storm.
    assert by_id["unindexed_owner_link"]["count"] == 0
    # dave is unaffected — his index is readable and correct.
    assert by_id["unreadable_value"]["findings"] == [{"store": "links", "key": "owner_links:carol"}]
    # Not a global skip: no "skipped" check appears for this.
    assert not any(c["skipped"] for c in checks)
    assert totals["checks_skipped"] == 0


# --- The cap ---


async def test_cap_leaves_count_exact_and_sets_truncated():
    links_data = {"all_links": _j([f"ghost{i}" for i in range(120)])}
    checks, totals = await _analyze(links_data=links_data)
    by_id = _by_id(checks)
    missing = by_id["missing_link_record"]
    assert missing["count"] == 120
    assert missing["truncated"] is True
    assert len(missing["findings"]) == consistency.MAX_FINDINGS_PER_CHECK == 100
    assert totals["findings"] == 120


def test_build_report_sets_top_level_truncated_when_any_check_truncated():
    checks = [
        {"check": "a", "severity": "warning", "count": 200, "truncated": True, "skipped": False, "findings": []},
        {"check": "b", "severity": "info", "count": 0, "truncated": False, "skipped": False, "findings": []},
    ]
    totals = {"findings": 200, "checks_with_findings": 1, "checks_skipped": 0}
    report = consistency.build_report(checks, totals, {}, generated_at="2026-08-04T00:00:00Z", generated_by="admin")
    assert report["truncated"] is True
    assert report["ok"] is False


# --- All-clear ---


async def test_all_clear_on_a_healthy_store():
    links_data = {
        "slug:x": _j({"owner": "carol"}),
        "all_links": _j(["x"]),
        "owner_links:carol": _j(["x"]),
    }
    users_data = {"user:carol": _j({}), "_meta:usernames": _j(["carol"])}
    collected = await consistency.collect(
        {"links": FakeStore(links_data), "users": FakeStore(users_data)}, fake_list_keys
    )
    checks, totals = consistency.analyze(collected)
    report = consistency.build_report(checks, totals, collected["scanned"], generated_at="x", generated_by="admin")
    assert report["ok"] is True
    assert totals["checks_skipped"] == 0
    assert all(c["count"] == 0 for c in checks)


# --- handle_consistency wiring ---


async def test_handle_consistency_forbidden_without_users_manage():
    resp = await consistency.handle_consistency(
        {"links": FakeStore(), "users": FakeStore()}, _principal(role="user", permissions=[]), fake_list_keys,
    )
    assert resp.status == 403
    assert json.loads(resp.body) == {"error": "forbidden", "required_permission": "users.manage"}


async def test_handle_consistency_allows_users_manage_without_admin_role():
    resp = await consistency.handle_consistency(
        {"links": FakeStore(), "users": FakeStore()},
        _principal(role="user", permissions=["users.manage"]),
        fake_list_keys,
    )
    assert resp.status == 200


async def test_handle_consistency_ok_true_on_fresh_stores():
    resp = await consistency.handle_consistency(
        {"links": FakeStore(), "users": FakeStore()}, _principal(), fake_list_keys,
    )
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["ok"] is True
    assert body["format"] == "spin-shortener-consistency-report"
    assert len(body["checks"]) == 12
    assert body["totals"]["checks_skipped"] == 0
    assert body["max_findings_per_check"] == consistency.MAX_FINDINGS_PER_CHECK


async def test_handle_consistency_never_leaks_password_hash():
    users_store = FakeStore({
        "user:alice": _j({
            "username": "alice",
            "password_hash": "pbkdf2_sha256$100$c2FsdA==$aGFzaA==",
            "role": "user",
            "permissions": [],
        }),
        "_meta:usernames": _j(["alice"]),
    })
    resp = await consistency.handle_consistency(
        {"links": FakeStore(), "users": users_store}, _principal(), fake_list_keys,
    )
    assert resp.status == 200
    assert b"password_hash" not in resp.body
    assert b"pbkdf2_sha256" not in resp.body


async def test_collect_never_even_reads_a_user_record_value():
    """Stronger than the report-body check above, which a `collect` that
    fetched every user value and merely discarded it would still pass.

    The users-store read is a deliberate allowlist of the two key shapes the
    walk actually parses. The links store next to it is fetched wholesale, so
    the tempting simplification is to make this one wholesale too — that would
    pull every PBKDF2 hash in the store into the handler's memory for no
    benefit at all, since no check reads a `user:` value. This test is what
    makes that a failure rather than an invisible change.
    """
    class RecordingStore(FakeStore):
        def __init__(self, data):
            super().__init__(data)
            self.read_keys: list[str] = []

        async def get(self, key):
            self.read_keys.append(key)
            return await super().get(key)

    users_store = RecordingStore({
        "user:alice": _j({
            "username": "alice",
            "password_hash": "pbkdf2_sha256$100$c2FsdA==$aGFzaA==",
            "role": "user",
            "permissions": [],
        }),
        "session:tok": _j({"username": "alice"}),
        "_meta:usernames": _j(["alice"]),
        "_meta:bootstrapped": b"1",
    })
    collected = await consistency.collect(
        {"links": FakeStore(), "users": users_store}, fake_list_keys
    )

    assert not [key for key in users_store.read_keys if key.startswith("user:")]
    # The two shapes that ARE read still are, so this isn't passing by reading
    # nothing at all.
    assert "session:tok" in users_store.read_keys
    assert "_meta:usernames" in users_store.read_keys
    assert collected["user_records"] == {"alice"}
    assert collected["session_usernames"] == ["alice"]
