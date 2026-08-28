import obs
from routing import ROUTES


def test_sanitize_error_message_replaces_control_characters():
    text = "line one\nline two\rtab\tnull\x00end"
    sanitized, truncated = obs.sanitize_error_message(text)
    assert "\n" not in sanitized
    assert "\r" not in sanitized
    assert "\x00" not in sanitized
    assert truncated is False


def test_a_message_containing_a_newline_cannot_forge_a_second_line():
    exc = FileNotFoundError("boom\nss comp=gui-pages ev=page_read_failed forged=1")
    line, _ = obs.page_read_failed_line("/login.html", "login.html", exc)
    assert "\n" not in line
    assert line.count("ss ") == 1 or line.startswith("ss ")
    # The forged content must not appear as a second recognizable "ss "-prefixed
    # line — there is only one newline-free line at all.
    assert len(line.splitlines()) == 1


def test_sanitize_error_message_truncates_long_messages():
    long_msg = "x" * 250
    sanitized, truncated = obs.sanitize_error_message(long_msg)
    assert len(sanitized) == 200
    assert truncated is True


def test_sanitize_error_message_does_not_truncate_short_messages():
    msg = "y" * 199
    sanitized, truncated = obs.sanitize_error_message(msg)
    assert sanitized == msg
    assert truncated is False


def test_sanitize_path_for_log_returns_safe_values_unchanged():
    assert obs.sanitize_path_for_log("admin/store-maintenance.html") == "admin/store-maintenance.html"
    assert obs.sanitize_path_for_log("/admin/url-policy.html") == "/admin/url-policy.html"


def test_sanitize_path_for_log_rejects_unsafe_values():
    assert obs.sanitize_path_for_log("has space") == "[invalid_path]"
    assert obs.sanitize_path_for_log("has\nnewline") == "[invalid_path]"
    assert obs.sanitize_path_for_log("café") == "[invalid_path]"
    assert obs.sanitize_path_for_log("") == "[invalid_path]"
    assert obs.sanitize_path_for_log("a" * 129) == "[invalid_path]"


def test_every_routes_value_is_log_safe():
    """The guard that makes the runtime sanitizer a fallback rather than a
    load-bearing control — this is what catches a future route added with an
    unsafe filename at CI time instead of at log time."""
    for key, value in ROUTES.items():
        assert obs.sanitize_path_for_log(key) == key
        assert obs.sanitize_path_for_log(value) == value


def test_page_read_failed_line_renders_expected_field_sequence():
    exc = FileNotFoundError(2, "No such file or directory")
    exc.filename = "/gui/login.html"
    exc.errno = 2
    line, _ = obs.page_read_failed_line("/login.html", "login.html", exc)

    assert line.startswith("ss ")
    assert "comp=gui-pages" in line
    assert "ev=page_read_failed" in line
    assert "route=/login.html" in line
    assert "file=login.html" in line
    assert "etype=FileNotFoundError" in line
    assert "errno=2" in line
    assert " msg=" in line

    # Field order.
    order = ["comp=", "ev=", "route=", "file=", "etype=", "errno=", " msg="]
    positions = [line.index(tok) for tok in order]
    assert positions == sorted(positions)


def test_msg_is_last_field_and_nothing_follows_it():
    exc = FileNotFoundError(2, "No such file or directory")
    exc.errno = 2
    line, _ = obs.page_read_failed_line("/login.html", "login.html", exc)

    msg_index = line.rindex(" msg=")
    for field in ("comp=", "ev=", "route=", "file=", "etype=", "errno="):
        assert line.index(field) < msg_index

    after_msg = line[msg_index + len(" msg="):]
    # No further " key=" delimited field may follow msg.
    assert " " not in after_msg.split("msg=")[-1] or "=" not in after_msg


def test_errno_omitted_when_not_an_int():
    exc = OSError("generic failure")
    exc.errno = None
    line, _ = obs.page_read_failed_line("/login.html", "login.html", exc)
    assert " errno=" not in line

    exc2 = OSError("with errno")
    exc2.errno = 5
    line2, _ = obs.page_read_failed_line("/login.html", "login.html", exc2)
    assert " errno=5 " in line2


def test_empty_message_renders_msg_dash():
    exc = OSError()
    exc.errno = None
    line, _ = obs.page_read_failed_line("/login.html", "login.html", exc)
    assert line.endswith("msg=-")


def test_make_dedup_first_key_true_repeat_false():
    should_emit = obs.make_dedup()
    assert should_emit("a") is True
    assert should_emit("a") is False


def test_make_dedup_distinct_keys_are_distinct():
    should_emit = obs.make_dedup()
    assert should_emit("route=/a") is True
    assert should_emit("route=/b") is True


def test_make_dedup_caps_at_max_keys():
    should_emit = obs.make_dedup(max_keys=2)
    assert should_emit("k1") is True
    assert should_emit("k2") is True
    assert should_emit("k3") is False  # a genuinely novel key, still refused


def test_make_dedup_instances_share_no_state():
    first = obs.make_dedup()
    second = obs.make_dedup()
    assert first("shared") is True
    assert second("shared") is True


def test_page_read_failed_line_dedup_key_prefix():
    exc = FileNotFoundError(2, "No such file or directory")
    exc.errno = 2
    _, dedup_key = obs.page_read_failed_line("/login.html", "login.html", exc)
    assert dedup_key.startswith("page_read_failed\x00")
