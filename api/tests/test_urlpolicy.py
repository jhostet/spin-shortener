"""Tests for the pure destination URL policy rule model: normalization, host
matching, precedence evaluation and whole-document parsing.

See docs/plans/destination-url-policy.md for the precedence rule and the
matching semantics these tests pin.
"""

import urlpolicy
from tests.fakes import FakeStore

DENY_RULE = {"host": "evil.example", "action": "deny", "note": None, "created_at": "t", "created_by": "a"}
ALLOW_RULE = {"host": "example.com", "action": "allow", "note": None, "created_at": "t", "created_by": "a"}


def _policy(default_action, rules):
    return {"version": 1, "default_action": default_action, "rules": rules}


# --- host_matches ---


def test_host_matches_exact():
    assert urlpolicy.host_matches("example.com", "example.com") is True


def test_host_matches_subdomain():
    assert urlpolicy.host_matches("www.example.com", "example.com") is True
    assert urlpolicy.host_matches("evil.example.com", "example.com") is True
    assert urlpolicy.host_matches("a.b.example.com", "example.com") is True


def test_host_matches_not_suffix_lookalike():
    assert urlpolicy.host_matches("notexample.com", "example.com") is False


def test_host_matches_not_reversed_suffix():
    assert urlpolicy.host_matches("example.com.evil.net", "example.com") is False


def test_host_matches_not_different_tld():
    assert urlpolicy.host_matches("example.co", "example.com") is False


# --- destination_host ---


def test_destination_host_uses_hostname_not_netloc():
    # userinfo-prefix spoof: netloc would be "example.com@evil.com".
    assert urlpolicy.destination_host("https://example.com@evil.com/x") == "evil.com"


def test_destination_host_strips_trailing_dot():
    assert urlpolicy.destination_host("https://evil.com./x") == "evil.com"


def test_destination_host_strips_port():
    assert urlpolicy.destination_host("https://evil.com:8443/x") == "evil.com"


def test_destination_host_none_for_hostless_url():
    assert urlpolicy.destination_host("https://user@/path") is None


def test_destination_host_lowercases():
    assert urlpolicy.destination_host("https://EVIL.com/x") == "evil.com"


# --- normalize_rule_host ---


def test_normalize_rule_host_variants_all_equal():
    for raw in ("EVIL.com", "  evil.com  ", "*.evil.com", "https://evil.com/path?x=1#y", "evil.com.", "evil.com:8443"):
        assert urlpolicy.normalize_rule_host(raw) == "evil.com", raw


def test_normalize_rule_host_rejects_empty():
    assert urlpolicy.normalize_rule_host("") is None
    assert urlpolicy.normalize_rule_host("   ") is None


def test_normalize_rule_host_rejects_whitespace_inside():
    assert urlpolicy.normalize_rule_host("evil .com") is None


def test_normalize_rule_host_rejects_too_long():
    assert urlpolicy.normalize_rule_host("a" * 254) is None


def test_normalize_rule_host_rejects_non_ascii():
    assert urlpolicy.normalize_rule_host("exämple.com") is None


def test_normalize_rule_host_allows_bare_localhost():
    assert urlpolicy.normalize_rule_host("localhost") == "localhost"


def test_normalize_rule_host_rejects_non_string():
    assert urlpolicy.normalize_rule_host(None) is None
    assert urlpolicy.normalize_rule_host(123) is None


# --- is_active ---


def test_is_active_false_for_empty_policy():
    assert urlpolicy.is_active(urlpolicy.EMPTY_POLICY) is False


def test_is_active_true_for_default_deny():
    assert urlpolicy.is_active(_policy("deny", [])) is True


def test_is_active_true_with_any_rule():
    assert urlpolicy.is_active(_policy("allow", [ALLOW_RULE])) is True


# --- evaluate ---


def test_evaluate_empty_policy_allows_with_no_policy_reason():
    verdict = urlpolicy.evaluate("https://example.com/x", urlpolicy.EMPTY_POLICY)
    assert verdict == {"allowed": True, "host": "example.com", "reason": "no_policy", "matched_rule": None}


def test_evaluate_deny_wins_over_allow_default_allow_mode():
    policy = _policy("allow", [DENY_RULE])
    verdict = urlpolicy.evaluate("https://evil.example/x", policy)
    assert verdict["allowed"] is False
    assert verdict["reason"] == "denied_by_rule"
    assert verdict["matched_rule"] == "evil.example"


def test_evaluate_deny_wins_over_allow_rule_default_deny_mode():
    # A deny rule for a host that's also covered by a broader allow rule.
    policy = _policy("deny", [
        {"host": "example.com", "action": "allow", "note": None, "created_at": "t", "created_by": "a"},
        {"host": "bad.example.com", "action": "deny", "note": None, "created_at": "t", "created_by": "a"},
    ])
    verdict = urlpolicy.evaluate("https://bad.example.com/x", policy)
    assert verdict["allowed"] is False
    assert verdict["reason"] == "denied_by_rule"
    assert verdict["matched_rule"] == "bad.example.com"


def test_evaluate_default_deny_no_matching_allow_rule_blocks():
    policy = _policy("deny", [ALLOW_RULE])
    verdict = urlpolicy.evaluate("https://other.test/x", policy)
    assert verdict["allowed"] is False
    assert verdict["reason"] == "not_allowed_by_default"
    assert verdict["matched_rule"] is None


def test_evaluate_default_deny_matching_allow_rule_allows():
    policy = _policy("deny", [ALLOW_RULE])
    verdict = urlpolicy.evaluate("https://shop.example.com/x", policy)
    assert verdict["allowed"] is True
    assert verdict["reason"] == "allowed_by_rule"
    assert verdict["matched_rule"] == "example.com"


def test_evaluate_default_allow_no_rules_matching_allows_by_default():
    policy = _policy("allow", [DENY_RULE])
    verdict = urlpolicy.evaluate("https://good.example/x", policy)
    assert verdict["allowed"] is True
    assert verdict["reason"] == "allowed_by_default"


def test_evaluate_hostless_url_allowed_when_inactive():
    verdict = urlpolicy.evaluate("https://user@/path", urlpolicy.EMPTY_POLICY)
    assert verdict["allowed"] is True
    assert verdict["reason"] == "no_policy"
    assert verdict["host"] is None


def test_evaluate_hostless_url_blocked_when_active():
    policy = _policy("deny", [ALLOW_RULE])
    verdict = urlpolicy.evaluate("https://user@/path", policy)
    assert verdict["allowed"] is False
    assert verdict["reason"] == "unparsable_target_url"
    assert verdict["host"] is None


def test_evaluate_non_ascii_host_matches_no_rule_blocked_under_default_deny():
    policy = _policy("deny", [ALLOW_RULE])
    verdict = urlpolicy.evaluate("https://exämple.com/x", policy)
    assert verdict["allowed"] is False
    assert verdict["reason"] == "not_allowed_by_default"


def test_evaluate_non_ascii_host_matches_no_rule_allowed_under_default_allow():
    policy = _policy("allow", [DENY_RULE])
    verdict = urlpolicy.evaluate("https://exämple.com/x", policy)
    assert verdict["allowed"] is True
    assert verdict["reason"] == "allowed_by_default"


# --- parse_policy_document ---


def test_parse_policy_document_valid_roundtrip():
    doc = {"default_action": "allow", "rules": [
        {"host": "EVIL.com", "action": "deny", "note": "bad"},
    ]}
    policy, error = urlpolicy.parse_policy_document(doc, now="2026-08-04T00:00:00Z", actor="alice")
    assert error is None
    assert policy["default_action"] == "allow"
    assert policy["rules"] == [
        {"host": "evil.com", "action": "deny", "note": "bad", "created_at": "2026-08-04T00:00:00Z", "created_by": "alice"},
    ]
    assert policy["updated_at"] == "2026-08-04T00:00:00Z"
    assert policy["updated_by"] == "alice"


def test_parse_policy_document_dedupes_on_host_and_action():
    doc = {"default_action": "allow", "rules": [
        {"host": "evil.com", "action": "deny"},
        {"host": "EVIL.com", "action": "deny"},
        {"host": "evil.com", "action": "allow"},
    ]}
    policy, error = urlpolicy.parse_policy_document(doc, now="t", actor="a")
    assert error is None
    assert len(policy["rules"]) == 2


def test_parse_policy_document_sorts_by_host_then_action():
    doc = {"default_action": "allow", "rules": [
        {"host": "b.com", "action": "allow"},
        {"host": "a.com", "action": "deny"},
        {"host": "a.com", "action": "allow"},
    ]}
    policy, error = urlpolicy.parse_policy_document(doc, now="t", actor="a")
    assert error is None
    assert [(r["host"], r["action"]) for r in policy["rules"]] == [
        ("a.com", "allow"), ("a.com", "deny"), ("b.com", "allow"),
    ]


def test_parse_policy_document_preserves_created_fields_for_existing_rules():
    doc = {"default_action": "allow", "rules": [
        {"host": "evil.com", "action": "deny", "created_at": "2020-01-01T00:00:00Z", "created_by": "bob"},
    ]}
    policy, error = urlpolicy.parse_policy_document(doc, now="2026-08-04T00:00:00Z", actor="alice")
    assert error is None
    assert policy["rules"][0]["created_at"] == "2020-01-01T00:00:00Z"
    assert policy["rules"][0]["created_by"] == "bob"


def test_parse_policy_document_not_a_dict():
    policy, error = urlpolicy.parse_policy_document([], now="t", actor="a")
    assert policy is None
    assert error == {"error": "invalid_policy"}


def test_parse_policy_document_rules_not_a_list():
    policy, error = urlpolicy.parse_policy_document(
        {"default_action": "allow", "rules": "nope"}, now="t", actor="a"
    )
    assert policy is None
    assert error == {"error": "invalid_policy"}


def test_parse_policy_document_invalid_default_action():
    policy, error = urlpolicy.parse_policy_document(
        {"default_action": "block", "rules": []}, now="t", actor="a"
    )
    assert policy is None
    assert error == {"error": "invalid_default_action", "allowed_actions": ["allow", "deny"]}


def test_parse_policy_document_invalid_rule_action():
    policy, error = urlpolicy.parse_policy_document(
        {"default_action": "allow", "rules": [{"host": "evil.com", "action": "block"}]}, now="t", actor="a"
    )
    assert policy is None
    assert error == {"error": "invalid_rule_action", "host": "evil.com", "allowed_actions": ["allow", "deny"]}


def test_parse_policy_document_invalid_rule_host():
    policy, error = urlpolicy.parse_policy_document(
        {"default_action": "allow", "rules": [{"host": "not a host!", "action": "deny"}]}, now="t", actor="a"
    )
    assert policy is None
    assert error == {"error": "invalid_rule_host", "host": "not a host!", "max_length": 253}


def test_parse_policy_document_invalid_rule_note():
    policy, error = urlpolicy.parse_policy_document(
        {"default_action": "allow", "rules": [{"host": "evil.com", "action": "deny", "note": "x" * 201}]},
        now="t", actor="a",
    )
    assert policy is None
    assert error == {"error": "invalid_rule_note", "host": "evil.com", "max_length": 200}


def test_parse_policy_document_too_many_rules():
    rules = [{"host": f"h{i}.com", "action": "deny"} for i in range(201)]
    policy, error = urlpolicy.parse_policy_document(
        {"default_action": "allow", "rules": rules}, now="t", actor="a"
    )
    assert policy is None
    assert error == {"error": "too_many_rules", "max_rules": 200, "rule_count": 201}


# --- load_policy / save_policy ---


async def test_load_policy_absent_key_returns_empty_policy():
    store = FakeStore()
    policy = await urlpolicy.load_policy(store)
    assert policy == urlpolicy.EMPTY_POLICY


async def test_load_policy_corrupted_value_returns_empty_policy():
    store = FakeStore({urlpolicy.POLICY_KEY: b"not json"})
    policy = await urlpolicy.load_policy(store)
    assert policy == urlpolicy.EMPTY_POLICY


async def test_save_then_load_roundtrip():
    store = FakeStore()
    policy, error = urlpolicy.parse_policy_document(
        {"default_action": "deny", "rules": [{"host": "evil.com", "action": "deny"}]}, now="t", actor="a"
    )
    assert error is None
    await urlpolicy.save_policy(store, policy)
    loaded = await urlpolicy.load_policy(store)
    assert loaded == policy
