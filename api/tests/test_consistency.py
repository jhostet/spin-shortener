"""Unit tests for api/consistency.py's pure logic: each of the six checks
in isolation, the skip rule, the per-check cap, the all-clear state, the
permission gate, and the credential-leak guarantee.

docs/plans/derived-link-indexes.md, Stage 2: six of the original twelve
checks (everything about all_links/owner_links:<owner>) were retired along
with the indexes they described, because links.py no longer writes either
index — see consistency.py's module docstring. This file used to test all
twelve; it now tests the six survivors plus the "leftover keys are known and
inert" property that replaced them.

Task-2's test_consistency_scenarios.py covers the same checks again, but
seeded through the real handlers (or FakeStores built the same way) and
asserting that every OTHER check stays at count 0 for that scenario — the
"a checker that cries wolf is worse than none" property. This file is the
narrower, faster unit layer: does each check fire correctly, in isolation,
with hand-built store contents.
"""

import json

import auth
import consistency
from tests.fakes import FakeStore, WriteRaisingStore, fake_get_many, fake_list_keys


def _principal(username="admin", role="admin", permissions=None):
    return auth.Principal(username=username, role=role, permissions=permissions or [], csrf_token="x")


def _j(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


async def _analyze(links_data=None, users_data=None):
    links_store = FakeStore(links_data or {})
    users_store = FakeStore(users_data or {})
    collected = await consistency.collect({"links": links_store, "users": users_store}, fake_list_keys, fake_get_many)
    return consistency.analyze(collected)


def _by_id(checks):
    return {c["check"]: c for c in checks}


# --- CHECKS tuple ---


def test_checks_tuple_has_the_six_ids_and_severities_in_order():
    assert consistency.CHECKS == (
        ("unknown_link_owner", "warning"),
        ("unindexed_user", "warning"),
        ("missing_user_record", "info"),
        ("orphan_session", "warning"),
        ("unreadable_value", "warning"),
        ("unrecognized_key", "info"),
    )


def test_repairable_checks_is_exactly_the_three_in_checks_order():
    assert consistency.REPAIRABLE_CHECKS == (
        "unindexed_user",
        "missing_user_record",
        "orphan_session",
    )
    check_ids_in_order = [check_id for check_id, _ in consistency.CHECKS]
    assert set(consistency.REPAIRABLE_CHECKS) <= set(check_ids_in_order)
    # Same relative order as CHECKS.
    positions = [check_ids_in_order.index(c) for c in consistency.REPAIRABLE_CHECKS]
    assert positions == sorted(positions)


def test_build_report_emits_repairable_checks():
    report = consistency.build_report(
        [], {"findings": 0, "checks_with_findings": 0, "checks_skipped": 0}, {},
        generated_at="x", generated_by="admin",
    )
    assert report["repairable_checks"] == list(consistency.REPAIRABLE_CHECKS)


async def test_analyze_max_findings_none_returns_every_finding_uncapped():
    users_data = {"_meta:usernames": _j([f"ghost{i}" for i in range(120)])}
    links_store = FakeStore({})
    users_store = FakeStore(users_data)
    collected = await consistency.collect({"links": links_store, "users": users_store}, fake_list_keys, fake_get_many)
    checks, totals = consistency.analyze(collected, max_findings=None)
    by_id = _by_id(checks)
    missing = by_id["missing_user_record"]
    assert missing["count"] == 120
    assert len(missing["findings"]) == 120
    assert missing["truncated"] is False
    assert all(not c["truncated"] for c in checks)
    assert totals["findings"] == 120


async def test_collect_sessions_by_username_groups_session_key_names():
    collected = await consistency.collect(
        {"links": FakeStore({}), "users": FakeStore({
            "session:tok1": _j({"username": "nobody"}),
            "session:tok2": _j({"username": "nobody"}),
            "session:tok3": _j({"username": "carol"}),
        })},
        fake_list_keys, fake_get_many,
    )
    assert sorted(collected["sessions_by_username"]["nobody"]) == ["session:tok1", "session:tok2"]
    assert collected["sessions_by_username"]["carol"] == ["session:tok3"]
    # session_usernames stays unchanged (a flat list, not grouped).
    assert sorted(collected["session_usernames"]) == ["carol", "nobody", "nobody"]


# --- Each surviving check, in isolation ---


async def test_unknown_link_owner():
    checks, _ = await _analyze(
        links_data={"slug:x": _j({"owner": "nobody"})},
        users_data={"_meta:usernames": _j([])},
    )
    by_id = _by_id(checks)
    assert by_id["unknown_link_owner"]["count"] == 1
    assert by_id["unknown_link_owner"]["findings"] == [{"slug": "x", "owner": "nobody"}]


async def test_unknown_link_owner_derived_purely_from_records_no_index_involved():
    """docs/plans/derived-link-indexes.md: this check no longer reads
    all_links/owner_links: at all — a record naming an unknown owner fires
    regardless of any index's state, including a leftover key that names
    the record correctly."""
    checks, _ = await _analyze(
        links_data={
            "slug:x": _j({"owner": "nobody"}),
            "all_links": _j(["x"]),
            "owner_links:nobody": _j(["x"]),
        },
        users_data={"_meta:usernames": _j([])},
    )
    by_id = _by_id(checks)
    assert by_id["unknown_link_owner"]["count"] == 1


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
    assert by_id["unreadable_value"]["findings"] == [
        {"store": "links", "key": "slug:bad", "reason": "Expecting value: line 1 column 1 (char 0)"}
    ]
    # The unreadable slug is excluded from every other check, not just noted.
    assert by_id["unknown_link_owner"]["count"] == 0


async def test_unreadable_value_slug_record_missing_owner_field():
    checks, _ = await _analyze(links_data={"slug:bad": _j({"target_url": "https://example.com"})})
    by_id = _by_id(checks)
    assert by_id["unreadable_value"]["count"] == 1
    assert by_id["unreadable_value"]["findings"] == [
        {"store": "links", "key": "slug:bad", "reason": "owner field missing or not a string"}
    ]


async def test_unreadable_value_non_object_link_record_reports_shape_reason():
    checks, _ = await _analyze(links_data={"slug:bad": _j(["not", "an", "object"])})
    by_id = _by_id(checks)
    assert by_id["unreadable_value"]["count"] == 1
    assert by_id["unreadable_value"]["findings"] == [
        {"store": "links", "key": "slug:bad", "reason": "not a JSON object"}
    ]


async def test_reason_sanitizer_is_actually_on_the_path_not_merely_importable(monkeypatch):
    """A natural json.JSONDecodeError message never echoes document bytes
    (measured, see the plan), so a natural input can't prove the sanitizer is
    on the path — it would pass whether or not the call were there. A spy on
    consistency.obs.sanitize_error_message is what proves it: it fails the
    moment anyone inlines str(exc) instead."""
    calls = []

    def spy(text):
        calls.append(text)
        return "REDACTED-BY-SPY", True, False

    monkeypatch.setattr(consistency.obs, "sanitize_error_message", spy)

    checks, _ = await _analyze(links_data={"slug:bad": b"not-json"})
    by_id = _by_id(checks)

    assert calls == ["Expecting value: line 1 column 1 (char 0)"]
    assert by_id["unreadable_value"]["findings"] == [
        {"store": "links", "key": "slug:bad", "reason": "REDACTED-BY-SPY"}
    ]


def test_decode_json_reason_is_sanitized_and_redacts_key_and_hash():
    exc = json.JSONDecodeError("boom users:session:tok pbkdf2_sha256$h", "d", 0)
    # The real decode path can't be driven to raise this exact message (a
    # real json.JSONDecodeError never echoes document bytes) — this pins the
    # sanitizer's redaction rules directly against a hand-built one, the way
    # the plan requires.
    sanitized, _redacted, _truncated = consistency.obs.sanitize_error_message(str(exc))
    assert "[key:users]" in sanitized
    assert "[hash]" in sanitized
    assert "session:tok" not in sanitized
    assert "pbkdf2_sha256" not in sanitized


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


# --- Leftover all_links / owner_links:<U> — known and inert, never reported ---


async def test_leftover_all_links_key_is_known_and_inert_not_unrecognized():
    """docs/plans/derived-link-indexes.md, Stage 2: all_links is an inert
    leftover key, not an unrecognized one — reporting it would fire on every
    single run forever for any store that predates this change."""
    checks, _ = await _analyze(links_data={"all_links": _j(["whatever", "garbage-too"])})
    by_id = _by_id(checks)
    assert by_id["unrecognized_key"]["count"] == 0
    assert by_id["unreadable_value"]["count"] == 0


async def test_leftover_all_links_key_ignored_even_when_unparseable():
    """A leftover index key is never parsed at all any more, so even a
    corrupted value must not be reported — there is nothing left that reads
    it, so a corrupt value here can never affect anything."""
    checks, _ = await _analyze(links_data={"all_links": b"not json at all"})
    by_id = _by_id(checks)
    assert by_id["unrecognized_key"]["count"] == 0
    assert by_id["unreadable_value"]["count"] == 0


async def test_leftover_owner_links_key_is_known_and_inert_not_unrecognized():
    checks, _ = await _analyze(links_data={"owner_links:carol": _j(["ghost"])})
    by_id = _by_id(checks)
    assert by_id["unrecognized_key"]["count"] == 0
    assert by_id["unreadable_value"]["count"] == 0


async def test_healthy_store_with_leftover_index_keys_reports_ok_true():
    """The plan's own worked example: a store with leftover all_links /
    owner_links:<U> keys PLUS a healthy set of links reports ok: true with
    every remaining check at count: 0."""
    links_data = {
        "slug:x": _j({"owner": "carol"}),
        "slug:y": _j({"owner": "carol"}),
        "all_links": _j(["x"]),  # leftover — stale, missing y, doesn't matter
        "owner_links:carol": _j(["x", "y", "ghost-that-never-existed"]),  # leftover
    }
    users_data = {"user:carol": _j({}), "_meta:usernames": _j(["carol"])}
    collected = await consistency.collect(
        {"links": FakeStore(links_data), "users": FakeStore(users_data)}, fake_list_keys, fake_get_many)
    checks, totals = consistency.analyze(collected)
    report = consistency.build_report(checks, totals, collected["scanned"], generated_at="x", generated_by="admin")
    assert report["ok"] is True
    assert all(c["count"] == 0 for c in checks)


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
    assert by_id["unreadable_value"]["findings"] == [
        {"store": "links", "key": consistency.URL_POLICY_KEY,
         "reason": "Expecting value: line 1 column 1 (char 0)"}
    ]


# --- Skip rule ---


async def test_meta_usernames_unreadable_skips_unindexed_user_and_missing_user_record():
    checks, totals = await _analyze(users_data={"_meta:usernames": b"not-json"})
    by_id = _by_id(checks)
    assert by_id["unindexed_user"]["skipped"] is True
    assert by_id["missing_user_record"]["skipped"] is True
    assert by_id["unreadable_value"]["findings"] == [
        {"store": "users", "key": "_meta:usernames", "reason": "Expecting value: line 1 column 1 (char 0)"}
    ]
    assert totals["checks_skipped"] == 2


# --- The cap ---


async def test_cap_leaves_count_exact_and_sets_truncated():
    users_data = {"_meta:usernames": _j([f"ghost{i}" for i in range(120)])}
    checks, totals = await _analyze(users_data=users_data)
    by_id = _by_id(checks)
    missing = by_id["missing_user_record"]
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
    links_data = {"slug:x": _j({"owner": "carol"})}
    users_data = {"user:carol": _j({}), "_meta:usernames": _j(["carol"])}
    collected = await consistency.collect(
        {"links": FakeStore(links_data), "users": FakeStore(users_data)}, fake_list_keys, fake_get_many)
    checks, totals = consistency.analyze(collected)
    report = consistency.build_report(checks, totals, collected["scanned"], generated_at="x", generated_by="admin")
    assert report["ok"] is True
    assert totals["checks_skipped"] == 0
    assert all(c["count"] == 0 for c in checks)


# --- handle_consistency wiring ---


async def test_handle_consistency_forbidden_without_users_manage():
    resp = await consistency.handle_consistency(
        {"links": FakeStore(), "users": FakeStore()}, _principal(role="user", permissions=[]), fake_list_keys, fake_get_many)
    assert resp.status == 403
    assert json.loads(resp.body) == {"error": "forbidden", "required_permission": "users.manage"}


async def test_handle_consistency_allows_users_manage_without_admin_role():
    resp = await consistency.handle_consistency(
        {"links": FakeStore(), "users": FakeStore()},
        _principal(role="user", permissions=["users.manage"]),
        fake_list_keys, fake_get_many)
    assert resp.status == 200


async def test_handle_consistency_ok_true_on_fresh_stores():
    resp = await consistency.handle_consistency(
        {"links": FakeStore(), "users": FakeStore()}, _principal(), fake_list_keys, fake_get_many)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["ok"] is True
    assert body["format"] == "spin-shortener-consistency-report"
    assert len(body["checks"]) == 6
    assert body["totals"]["checks_skipped"] == 0
    assert body["max_findings_per_check"] == consistency.MAX_FINDINGS_PER_CHECK


async def test_handle_consistency_never_leaks_password_hash():
    # A links:slug record legitimately carries the LINK's own password hash
    # (CLAUDE.md, "KV backup and restore" — it's deliberately not stripped
    # from a backup), and it's the one key type a corrupt reason field could
    # plausibly echo since consistency.collect DOES fetch and parse it. The
    # record below is deliberately truncated (fails to parse) while still
    # carrying the hash, so its unreadable_value reason is exercised through
    # the real sanitizer, not a hand-built one.
    links_store = FakeStore({
        "slug:bad": b'{"slug":"bad","password_hash":"pbkdf2_sha256$100$c2FsdA==$aGFzaA==",',
    })
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
        {"links": links_store, "users": users_store}, _principal(), fake_list_keys, fake_get_many)
    assert resp.status == 200
    assert b"password_hash" not in resp.body
    assert b"pbkdf2_sha256" not in resp.body
    body = json.loads(resp.body)
    by_id = _by_id(body["checks"])
    assert by_id["unreadable_value"]["count"] == 1


async def test_handle_consistency_performs_zero_writes_over_a_store_with_real_drift():
    """Seeds findings from at least two different checks (unknown_link_owner,
    orphan_session) and exercises `handle_consistency` against stores whose
    `set`/`delete` raise. If this read-only handler ever gains a write, this
    test fails loudly instead of the write silently landing.

    Verified live during development that this actually guards something:
    temporarily adding `await links_store.set("probe", b"1")` inside
    `consistency.collect` makes this test FAIL (reverted before commit)."""
    links_store = WriteRaisingStore({
        "slug:foo": _j({"owner": "nobody"}),          # -> unknown_link_owner
    })
    users_store = WriteRaisingStore({
        "session:tok1": _j({"username": "nobody", "csrf_token": "x"}),  # -> orphan_session
        "_meta:usernames": _j([]),
    })
    resp = await consistency.handle_consistency(
        {"links": links_store, "users": users_store}, _principal(), fake_list_keys, fake_get_many)
    assert resp.status == 200
    body = json.loads(resp.body)
    by_id = _by_id(body["checks"])
    assert by_id["unknown_link_owner"]["count"] >= 1
    assert by_id["orphan_session"]["count"] >= 1
    assert body["ok"] is False


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
        {"links": FakeStore(), "users": users_store}, fake_list_keys, fake_get_many)

    assert not [key for key in users_store.read_keys if key.startswith("user:")]
    # The two shapes that ARE read still are, so this isn't passing by reading
    # nothing at all.
    assert "session:tok" in users_store.read_keys
    assert "_meta:usernames" in users_store.read_keys
    assert collected["user_records"] == {"alice"}
    assert collected["session_usernames"] == ["alice"]
