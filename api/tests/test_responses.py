from responses import SECURITY_HEADERS, json_response


def test_json_response_includes_security_headers():
    response = json_response(200, {"ok": True})
    for key, value in SECURITY_HEADERS.items():
        assert response.headers[key] == value
    assert response.headers["content-type"] == "application/json"


def test_json_response_caller_headers_cannot_override_security_headers():
    """A code review found the original merge order let a caller-supplied
    `headers` dict silently override a security header on a colliding key —
    this proves the fix: SECURITY_HEADERS always wins, no matter what a
    call site passes in."""
    response = json_response(
        200,
        {"ok": True},
        headers={"content-security-policy": "default-src *", "x-frame-options": "ALLOWALL"},
    )
    assert response.headers["content-security-policy"] == SECURITY_HEADERS["content-security-policy"]
    assert response.headers["x-frame-options"] == SECURITY_HEADERS["x-frame-options"]


def test_json_response_non_colliding_caller_headers_still_pass_through():
    response = json_response(200, {"ok": True}, headers={"set-cookie": "session=abc"})
    assert response.headers["set-cookie"] == "session=abc"


def test_to_iso8601_utc_ms_keeps_millisecond_resolution():
    """Analytics events are recorded with UnixMilli and routinely arrive
    several to a second; second-resolution made distinct visits render as
    duplicate rows. Pinned separately from to_iso8601_utc, which must keep
    its exact existing shape for link windows."""
    from datetime import datetime, timezone

    from responses import to_iso8601_utc, to_iso8601_utc_ms

    dt = datetime(2026, 8, 9, 1, 2, 38, 412_000, tzinfo=timezone.utc)
    assert to_iso8601_utc_ms(dt) == "2026-08-09T01:02:38.412Z"
    # The second-resolution function is unchanged for the same instant.
    assert to_iso8601_utc(dt) == "2026-08-09T01:02:38Z"


def test_to_iso8601_utc_ms_pads_sub_millisecond_values():
    from datetime import datetime, timezone

    from responses import to_iso8601_utc, to_iso8601_utc_ms

    dt = datetime(2026, 8, 9, 1, 2, 38, 7_000, tzinfo=timezone.utc)
    assert to_iso8601_utc_ms(dt) == "2026-08-09T01:02:38.007Z"


def test_parse_iso8601_utc_round_trips_the_millisecond_form():
    """Nothing feeds an event timestamp back today, but the docstring claims
    it would work — so pin it rather than leave the claim untested."""
    from datetime import datetime, timezone

    from responses import parse_iso8601_utc, to_iso8601_utc_ms

    dt = datetime(2026, 8, 9, 1, 2, 38, 412_000, tzinfo=timezone.utc)
    assert parse_iso8601_utc(to_iso8601_utc_ms(dt)) == dt
