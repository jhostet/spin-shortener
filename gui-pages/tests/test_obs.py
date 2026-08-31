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


# --- error_type_name / exc_location / unhandled_exception_line -------------


def _raise_inner():
    raise ValueError("inner boom")


def _raise_outer():
    _raise_inner()


def test_exc_location_names_the_innermost_frame():
    try:
        _raise_outer()
    except ValueError as exc:
        location = obs.exc_location(exc)
    # The innermost frame is the one inside _raise_inner, not _raise_outer.
    assert location.startswith("test_obs.py:")
    inner_lineno = _raise_inner.__code__.co_firstlineno + 1
    assert location == f"test_obs.py:{inner_lineno}"


def test_exc_location_returns_dash_for_no_traceback():
    assert obs.exc_location(ValueError("never raised")) == "-"


def test_exc_location_returns_a_basename_not_a_path():
    try:
        _raise_outer()
    except ValueError as exc:
        location = obs.exc_location(exc)
    assert "/" not in location
    parts = location.split(":")
    assert len(parts) == 2


class _StandInErrorVariant:
    pass


_StandInErrorVariant.__name__ = "Error_Undefined"


class _StandInErr(Exception):
    def __init__(self, value):
        super().__init__()
        self.value = value


def test_error_type_name_renders_wit_style_variant():
    exc = _StandInErr(_StandInErrorVariant())
    assert obs.error_type_name(exc) == "_StandInErr/Error_Undefined"

    plain = ValueError("boom")
    assert obs.error_type_name(plain) == "ValueError"


class _NotAnErrorVariant:
    pass


def test_error_type_name_falls_back_when_value_class_name_lacks_error_prefix():
    exc = _StandInErr(_NotAnErrorVariant())
    assert obs.error_type_name(exc) == "_StandInErr"


def test_unhandled_exception_line_renders_expected_field_sequence():
    try:
        _raise_outer()
    except ValueError as exc:
        line, _ = obs.unhandled_exception_line(exc)

    assert line.startswith("ss ")
    order = ["comp=", "ev=", "etype=", "at=", " msg="]
    positions = [line.index(tok) for tok in order]
    assert positions == sorted(positions)
    assert "comp=gui-pages" in line
    assert "ev=exc" in line
    assert "etype=ValueError" in line


def test_unhandled_exception_line_msg_is_last_field():
    try:
        _raise_outer()
    except ValueError as exc:
        line, _ = obs.unhandled_exception_line(exc)

    msg_index = line.rindex(" msg=")
    for field in ("comp=", "ev=", "etype=", "at="):
        assert line.index(field) < msg_index


def test_unhandled_exception_line_newline_in_message_cannot_forge_a_second_line():
    try:
        raise ValueError("boom\nss comp=gui-pages ev=exc forged=1")
    except ValueError as exc:
        line, _ = obs.unhandled_exception_line(exc)
    assert len(line.splitlines()) == 1


def test_unhandled_exception_line_truncates_long_message():
    try:
        raise ValueError("x" * 250)
    except ValueError as exc:
        line, _ = obs.unhandled_exception_line(exc)
    assert " msg_truncated=1 " in line
    truncated_index = line.index(" msg_truncated=1 ")
    msg_index = line.rindex(" msg=")
    assert truncated_index < msg_index

    try:
        raise ValueError("short")
    except ValueError as exc:
        short_line, _ = obs.unhandled_exception_line(exc)
    assert " msg_truncated=" not in short_line


def test_unhandled_exception_line_empty_message_renders_msg_dash():
    try:
        raise ValueError()
    except ValueError as exc:
        line, _ = obs.unhandled_exception_line(exc)
    assert line.endswith("msg=-")


def test_unhandled_exception_line_dedup_key_disjoint_from_page_read_failed():
    try:
        raise ValueError("boom")
    except ValueError as exc:
        _, exc_dedup_key = obs.unhandled_exception_line(exc)

    file_exc = FileNotFoundError(2, "No such file or directory")
    file_exc.errno = 2
    _, page_dedup_key = obs.page_read_failed_line("/login.html", "login.html", file_exc)

    assert exc_dedup_key.startswith("exc\x00")
    assert page_dedup_key.startswith("page_read_failed\x00")
    assert exc_dedup_key != page_dedup_key
    assert not exc_dedup_key.startswith(page_dedup_key)
    assert not page_dedup_key.startswith(exc_dedup_key)


def test_unhandled_exception_line_dedup_key_differs_by_raise_line():
    def raise_at_a():
        raise ValueError("same message")

    def raise_at_b():
        raise ValueError("same message")

    try:
        raise_at_a()
    except ValueError as exc:
        _, key_a = obs.unhandled_exception_line(exc)

    try:
        raise_at_b()
    except ValueError as exc:
        _, key_b = obs.unhandled_exception_line(exc)

    assert key_a != key_b


def test_unhandled_exception_line_never_emits_route_method_op_ns():
    try:
        _raise_outer()
    except ValueError as exc:
        line, _ = obs.unhandled_exception_line(exc)
    for forbidden in ("route=", "method=", "op=", "ns="):
        assert forbidden not in line
