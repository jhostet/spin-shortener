import obs


def test_collector_totals_empty():
    c = obs.Collector()
    assert c.totals() == (0, 0, 0)


def test_collector_count_and_total_micros():
    c = obs.Collector()
    c.record("get", "links", 5_000, 100)
    c.record("get", "links", 7_000, 50)
    c.record("set", "analytics", 12_000, 20)

    ops, us, num_bytes = c.totals()
    assert ops == 3
    assert us == 24
    assert num_bytes == 170


def test_render_log_line_field_format_and_zero_count_omission():
    c = obs.Collector()
    c.record("open", "-", 20_000, 0)
    c.record("open", "-", 15_000, 0)
    c.record("exists", "links", 17_000, 0)
    c.record("get", "links", 11_000, 262)

    line = obs.render_log_line(
        [("comp", "redirect"), ("route", "/r/{slug}"), ("status", "302")],
        174_000,
        c,
    )

    assert "open=2/35" in line
    assert "exists=1/17" in line
    assert "get=1/11" in line

    # Zero-count fields (set, delete, list_keys) must be omitted entirely,
    # never rendered as "set=0/0".
    for absent in ("set=", "delete=", "list_keys="):
        assert absent not in line

    assert line.startswith("ss comp=redirect route=/r/{slug} status=302 dur_us=174")
    assert "kv_ops=4" in line
    assert "kv_bytes=262" in line


def test_render_log_line_slowest_operation_including_open_dash_namespace():
    c = obs.Collector()
    c.record("open", "-", 20_000, 0)
    c.record("exists", "links", 5_000, 0)

    line = obs.render_log_line([], 30_000, c)
    assert "slow=open:-:20" in line


def test_render_log_line_slowest_operation_updates_to_later_larger_duration():
    c = obs.Collector()
    c.record("exists", "links", 5_000, 0)
    c.record("get", "analytics", 40_000, 4)

    line = obs.render_log_line([], 50_000, c)
    assert "slow=get:analytics:40" in line


def test_render_log_line_none_collector_omits_kv_summary_entirely():
    line = obs.render_log_line([("comp", "api"), ("status", "401")], 12_000, None)
    assert line == "ss comp=api status=401 dur_us=12"


def test_render_server_timing_microseconds_render_as_milliseconds():
    c = obs.Collector()
    c.record("open", "-", 80_000, 0)

    got = obs.render_server_timing(174_000, c)
    assert got == 'kv;dur=0.080;desc="1 ops", handler;dur=0.174'


def test_render_server_timing_none_collector():
    got = obs.render_server_timing(174_000, None)
    assert got == 'kv;dur=0.000;desc="0 ops", handler;dur=0.174'


def test_route_template_links_slug_and_suffixes():
    assert obs.route_template("/api/links/abc123") == "/api/links/{slug}"
    assert obs.route_template("/api/links/abc123/password") == "/api/links/{slug}/password"
    assert obs.route_template("/api/links/abc123/analytics") == "/api/links/{slug}/analytics"
    assert obs.route_template("/api/links/abc123/qr") == "/api/links/{slug}/qr"


def test_route_template_leaves_non_dynamic_link_routes_unchanged():
    assert obs.route_template("/api/links") == "/api/links"
    assert obs.route_template("/api/links/bulk") == "/api/links/bulk"
    assert obs.route_template("/api/links/bulk-action") == "/api/links/bulk-action"


def test_route_template_users():
    assert obs.route_template("/api/users/alice") == "/api/users/{username}"
    assert obs.route_template("/api/users") == "/api/users"


def test_route_template_leaves_unrelated_paths_unchanged():
    assert obs.route_template("/api/auth/login") == "/api/auth/login"
    assert obs.route_template("/api/admin/backup") == "/api/admin/backup"


def test_parse_log_level():
    assert obs.parse_log_level("summary") == "summary"
    assert obs.parse_log_level("off") == "off"
    assert obs.parse_log_level("") == "off"
    assert obs.parse_log_level("SUMMARY") == "off"
    assert obs.parse_log_level("verbose") == "off"
    assert obs.parse_log_level("garbage") == "off"


def test_token_matches_empty_configured_never_matches():
    assert obs.token_matches("", "") is False
    assert obs.token_matches("", "anything") is False


def test_token_matches_correct_and_wrong_token():
    assert obs.token_matches("secret123", "secret123") is True
    assert obs.token_matches("secret123", "wrong") is False
    assert obs.token_matches("secret123", "") is False
