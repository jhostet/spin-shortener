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
