import domains


# --- normalize_base_url ---


def test_normalize_strips_trailing_slash_and_lowercases_host():
    assert domains.normalize_base_url("https://Go.Example.com/") == "https://go.example.com"
    assert domains.normalize_base_url("https://go.example.com") == "https://go.example.com"


def test_normalize_lowercases_scheme():
    assert domains.normalize_base_url("HTTP://localhost:3000") == "http://localhost:3000"


def test_normalize_rejects_path():
    assert domains.normalize_base_url("https://go.example.com/r/abc") is None


def test_normalize_rejects_query():
    assert domains.normalize_base_url("https://go.example.com?x=1") is None


def test_normalize_rejects_fragment():
    assert domains.normalize_base_url("https://go.example.com#frag") is None


def test_normalize_rejects_non_http_scheme():
    assert domains.normalize_base_url("ftp://go.example.com") is None


def test_normalize_rejects_missing_scheme():
    assert domains.normalize_base_url("go.example.com") is None


def test_normalize_rejects_blank():
    assert domains.normalize_base_url("") is None
    assert domains.normalize_base_url("   ") is None


def test_normalize_preserves_port():
    assert domains.normalize_base_url("http://127.0.0.1:3000") == "http://127.0.0.1:3000"


# --- parse_base_urls ---


def test_parse_comma_separated_with_blanks_duplicates_and_one_malformed():
    raw = "https://go.example.com, ,https://go.example.com,not-a-url,http://localhost:3000/"
    assert domains.parse_base_urls(raw) == [
        "https://go.example.com",
        "http://localhost:3000",
    ]


def test_parse_empty_string_returns_empty_list():
    assert domains.parse_base_urls("") == []


def test_parse_all_malformed_returns_empty_list():
    assert domains.parse_base_urls("not-a-url,also-bad") == []


def test_parse_preserves_order():
    raw = "https://c.example.com,https://a.example.com,https://b.example.com"
    assert domains.parse_base_urls(raw) == [
        "https://c.example.com",
        "https://a.example.com",
        "https://b.example.com",
    ]


# --- visible_base_urls ---


def test_visible_empty_assigned_returns_whole_configured_list():
    configured = ["https://a.example.com", "https://b.example.com"]
    assert domains.visible_base_urls([], configured) == configured


def test_visible_fully_stale_assigned_returns_whole_configured_list():
    configured = ["https://a.example.com", "https://b.example.com"]
    assert domains.visible_base_urls(["https://removed.example.com"], configured) == configured


def test_visible_partial_assigned_returns_configured_order_not_assigned_order():
    configured = ["https://a.example.com", "https://b.example.com", "https://c.example.com"]
    assigned = ["https://c.example.com", "https://a.example.com"]
    assert domains.visible_base_urls(assigned, configured) == [
        "https://a.example.com",
        "https://c.example.com",
    ]


def test_visible_full_assigned_matches_configured():
    configured = ["https://a.example.com", "https://b.example.com"]
    assert domains.visible_base_urls(list(configured), configured) == configured


# --- resolve_base_url ---


def test_resolve_none_candidate_returns_first_configured():
    configured = ["https://a.example.com", "https://b.example.com"]
    assert domains.resolve_base_url(None, configured) == "https://a.example.com"


def test_resolve_empty_string_candidate_returns_first_configured():
    configured = ["https://a.example.com", "https://b.example.com"]
    assert domains.resolve_base_url("", configured) == "https://a.example.com"


def test_resolve_none_candidate_with_empty_configured_returns_none():
    assert domains.resolve_base_url(None, []) is None


def test_resolve_unconfigured_candidate_returns_none():
    configured = ["https://a.example.com", "https://b.example.com"]
    assert domains.resolve_base_url("https://evil.example", configured) is None


def test_resolve_returns_configured_entry_not_caller_string():
    """The security-relevant property: the returned value must be the
    server's own configured string, not a (possibly differently-cased or
    trailing-slashed) copy of the caller's input, even when they normalize
    to the same value."""
    configured = ["https://go.example.com"]
    result = domains.resolve_base_url("HTTPS://GO.EXAMPLE.COM/", configured)
    assert result == "https://go.example.com"
    assert result is configured[0]


def test_resolve_second_configured_domain():
    configured = ["https://a.example.com", "https://b.example.com"]
    assert domains.resolve_base_url("https://b.example.com", configured) == "https://b.example.com"


# --- parse_include_redirect_prefix ---


def test_parse_include_redirect_prefix_true_cases():
    for raw in (None, "", "true", "TRUE", "yes", "1", "ture"):
        assert domains.parse_include_redirect_prefix(raw) is True


def test_parse_include_redirect_prefix_non_string_is_true():
    assert domains.parse_include_redirect_prefix(123) is True
    assert domains.parse_include_redirect_prefix(True) is True


def test_parse_include_redirect_prefix_false_cases():
    for raw in ("false", "FALSE", " false "):
        assert domains.parse_include_redirect_prefix(raw) is False


# --- short_url_for ---


def test_short_url_for_with_prefix():
    assert domains.short_url_for("https://go.example.com", "abc", True) == "https://go.example.com/r/abc"


def test_short_url_for_without_prefix():
    assert domains.short_url_for("https://go.example.com", "abc", False) == "https://go.example.com/abc"


def test_short_url_for_default_includes_prefix():
    assert domains.short_url_for("https://go.example.com", "abc") == "https://go.example.com/r/abc"


def test_short_url_for_exactly_one_slash_after_scheme_either_way():
    with_prefix = domains.short_url_for("https://go.example.com", "abc", True)
    without_prefix = domains.short_url_for("https://go.example.com", "abc", False)
    assert "//" not in with_prefix[len("https://") :]
    assert "//" not in without_prefix[len("https://") :]


# --- normalize_allowed_domains ---

CONFIGURED = ["https://trrk.io", "http://localhost:3000"]


def test_normalize_allowed_domains_none_is_unrestricted():
    assert domains.normalize_allowed_domains(None, CONFIGURED) == ([], None)


def test_normalize_allowed_domains_empty_list_is_unrestricted():
    assert domains.normalize_allowed_domains([], CONFIGURED) == ([], None)


def test_normalize_allowed_domains_non_list_is_invalid():
    assert domains.normalize_allowed_domains("https://trrk.io", CONFIGURED) == (
        None,
        "invalid_allowed_domains",
    )


def test_normalize_allowed_domains_non_string_member_is_invalid():
    assert domains.normalize_allowed_domains([123], CONFIGURED) == (None, "invalid_allowed_domains")


def test_normalize_allowed_domains_unnormalizable_member_is_invalid():
    assert domains.normalize_allowed_domains(["not-a-url"], CONFIGURED) == (
        None,
        "invalid_allowed_domains",
    )


def test_normalize_allowed_domains_unconfigured_member_is_invalid():
    result = domains.normalize_allowed_domains(["https://not-configured.example"], CONFIGURED)
    assert result == (None, "invalid_allowed_domains")


def test_normalize_allowed_domains_canonicalizes_case_and_trailing_slash():
    result = domains.normalize_allowed_domains(["HTTPS://TRRK.IO/"], CONFIGURED)
    assert result == (["https://trrk.io"], None)


def test_normalize_allowed_domains_output_in_configured_order():
    result = domains.normalize_allowed_domains(
        ["http://localhost:3000", "https://trrk.io"], CONFIGURED
    )
    assert result == (["https://trrk.io", "http://localhost:3000"], None)


def test_normalize_allowed_domains_deduplicates():
    result = domains.normalize_allowed_domains(
        ["https://trrk.io", "https://trrk.io", "HTTPS://TRRK.IO"], CONFIGURED
    )
    assert result == (["https://trrk.io"], None)


def test_normalize_allowed_domains_also_allowed_retained_when_resubmitted():
    also_allowed = ["https://retired.example"]
    result = domains.normalize_allowed_domains(
        ["https://trrk.io", "https://retired.example"], CONFIGURED, also_allowed=also_allowed
    )
    assert result == (["https://trrk.io", "https://retired.example"], None)


def test_normalize_allowed_domains_also_allowed_dropped_when_omitted():
    also_allowed = ["https://retired.example"]
    result = domains.normalize_allowed_domains(["https://trrk.io"], CONFIGURED, also_allowed=also_allowed)
    assert result == (["https://trrk.io"], None)


def test_normalize_allowed_domains_also_allowed_alone_is_still_valid():
    also_allowed = ["https://retired.example"]
    result = domains.normalize_allowed_domains(
        ["https://retired.example"], CONFIGURED, also_allowed=also_allowed
    )
    assert result == (["https://retired.example"], None)


def test_normalize_allowed_domains_member_not_in_configured_or_also_allowed_is_invalid():
    also_allowed = ["https://retired.example"]
    result = domains.normalize_allowed_domains(
        ["https://not-configured.example"], CONFIGURED, also_allowed=also_allowed
    )
    assert result == (None, "invalid_allowed_domains")


# --- base_url_allowed_for_link ---


def test_base_url_allowed_for_link_none_allowed_is_unrestricted():
    assert domains.base_url_allowed_for_link("https://trrk.io", None) is True


def test_base_url_allowed_for_link_empty_allowed_is_unrestricted():
    assert domains.base_url_allowed_for_link("https://trrk.io", []) is True


def test_base_url_allowed_for_link_matches_on_hostname_ignoring_scheme_and_port():
    allowed = ["https://trrk.io"]
    assert domains.base_url_allowed_for_link("http://trrk.io:8080", allowed) is True


def test_base_url_allowed_for_link_rejects_a_suffix_that_is_not_a_whole_label():
    allowed = ["https://trrk.io"]
    assert domains.base_url_allowed_for_link("https://nottrrk.io", allowed) is False


def test_base_url_allowed_for_link_rejects_unlisted_host():
    allowed = ["https://trrk.io"]
    assert domains.base_url_allowed_for_link("http://localhost:3000", allowed) is False
