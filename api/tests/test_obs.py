import obs


def test_collector_totals_empty():
    c = obs.Collector()
    assert c.totals() == (0, 0, 0, 0)


def test_collector_count_and_total_micros():
    c = obs.Collector()
    c.record("get", "links", 5_000, 100)
    c.record("get", "links", 7_000, 50)
    c.record("set", "analytics", 12_000, 20)

    ops, us, num_bytes, num_keys = c.totals()
    assert ops == 3
    assert us == 24
    assert num_bytes == 170
    assert num_keys == 3  # num_keys defaults to 1 per record() call


def test_collector_num_keys_defaults_to_one_so_pre_existing_call_sites_are_unaffected():
    c = obs.Collector()
    c.record("get", "links", 1_000, 4)  # no num_keys passed
    ops, _, _, num_keys = c.totals()
    assert ops == num_keys == 1


def test_collector_num_keys_accumulates_for_get_many():
    c = obs.Collector()
    c.record("get_many", "analytics", 10_000, 900, num_keys=65)
    c.record("get_many", "analytics", 10_000, 900, num_keys=30)
    ops, _, _, num_keys = c.totals()
    assert ops == 2       # two host calls
    assert num_keys == 95  # covering 95 keys total


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


def test_render_log_line_omits_kv_keys_when_it_equals_kv_ops():
    """Every non-batching request (the whole app before this field existed,
    and every handler that never calls get_many) must render a
    byte-identical line to before kv_keys existed."""
    c = obs.Collector()
    c.record("get", "links", 5_000, 100)
    c.record("get", "links", 7_000, 50)

    line = obs.render_log_line([("comp", "api")], 20_000, c)
    assert "kv_keys" not in line


def test_render_log_line_emits_kv_keys_immediately_after_kv_bytes_when_it_differs():
    c = obs.Collector()
    c.record("get_many", "analytics", 10_000, 900, num_keys=65)

    line = obs.render_log_line([("comp", "api")], 20_000, c)
    assert "kv_bytes=900 kv_keys=65" in line


def test_render_log_line_with_no_retries_is_byte_identical_to_before(  # docs/plans/write-throttle-resilience.md
):
    c_before = obs.Collector()
    c_before.record("open", "-", 20_000, 0)
    c_before.record("get", "links", 11_000, 262)
    line_before = obs.render_log_line([("comp", "api")], 40_000, c_before)

    # Same operations, same collector construction — write_retry/write_failed
    # were never recorded, so the rendered line must not change at all.
    c_after = obs.Collector()
    c_after.record("open", "-", 20_000, 0)
    c_after.record("get", "links", 11_000, 262)
    line_after = obs.render_log_line([("comp", "api")], 40_000, c_after)

    assert line_before == line_after
    assert "write_retry" not in line_after
    assert "write_failed" not in line_after


def test_render_log_line_shows_retries_and_exhaustion():
    c = obs.Collector()
    c.record("set", "-", 5_000, 0)
    c.record("write_retry", "-", 300_000, 0)
    c.record("write_retry", "-", 400_000, 0)
    c.record("write_retry", "-", 450_000, 0)
    c.record("write_failed", "-", 0, 0)

    line = obs.render_log_line([("comp", "api")], 1_200_000, c)
    assert "write_retry=3/1150" in line
    assert "write_failed=1/0" in line
    # Ordering: write_retry/write_failed immediately after delete, before list_keys.
    assert line.index("write_retry=") < line.index("write_failed=")


def test_collector_record_has_no_key_parameter():
    """Structural invariant: Collector.record must never gain a parameter
    that could accept a KV key, for write_retry/write_failed exactly as for
    every other op type."""
    import inspect

    params = list(inspect.signature(obs.Collector.record).parameters)
    assert params == ["self", "op_type", "namespace", "duration_ns", "num_bytes", "num_keys"]


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
