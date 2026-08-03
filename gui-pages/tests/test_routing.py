import pytest

from routing import SECURITY_HEADERS, build_response, resolve_file


@pytest.mark.parametrize(
    "path,expected_file",
    [
        ("/", "index.html"),
        ("/index.html", "index.html"),
        ("/login.html", "login.html"),
        ("/dashboard.html", "dashboard.html"),
        ("/admin/users.html", "admin/users.html"),
        ("/admin/backup.html", "admin/backup.html"),
        ("/links/detail.html", "links/detail.html"),
        ("/nonexistent.html", None),
        ("/app.js", None),  # served by the other gui component now, not this one
        ("/theme-init.js", None),
        ("/vendor/pico.min.css", None),
        # The per-page assets the CSP hardening externalized are served by the
        # gui component too, via exact routes in spin.toml — not by this one.
        ("/dashboard.js", None),
        ("/admin/users.css", None),
        ("/../../etc/passwd", None),
    ],
)
def test_resolve_file(path, expected_file):
    assert resolve_file(path) == expected_file


def test_build_response_known_path_reads_correct_file_and_sets_headers():
    reads = []

    def fake_read_file(relative_path):
        reads.append(relative_path)
        return b"<html>hi</html>"

    response = build_response("/login.html", fake_read_file)

    assert reads == ["login.html"]
    assert response.status == 200
    assert response.body == b"<html>hi</html>"
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    for key, value in SECURITY_HEADERS.items():
        assert response.headers[key] == value


def test_build_response_strips_query_string_before_resolving():
    response = build_response("/links/detail.html?slug=abc123", lambda _: b"body")
    assert response.status == 200


def test_build_response_unknown_path_is_404_with_security_headers_still_set():
    def fail_read_file(_):
        raise AssertionError("should not read a file for an unknown path")

    response = build_response("/does-not-exist.html", fail_read_file)

    assert response.status == 404
    for key, value in SECURITY_HEADERS.items():
        assert response.headers[key] == value


def test_build_response_file_read_failure_is_500_with_security_headers_still_set():
    """A code review flagged the original build_response as having no guard
    around read_file() — a ROUTES-vs-filesystem drift (a page renamed on
    disk but not in ROUTES, or vice versa) would raise unhandled instead of
    returning a graceful, header-carrying error."""

    def failing_read_file(_):
        raise FileNotFoundError("simulated ROUTES/filesystem drift")

    response = build_response("/login.html", failing_read_file)

    assert response.status == 500
    for key, value in SECURITY_HEADERS.items():
        assert response.headers[key] == value


def test_security_headers_lock_down_framing_and_plugins():
    csp = SECURITY_HEADERS["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert SECURITY_HEADERS["x-frame-options"] == "DENY"
    assert SECURITY_HEADERS["x-content-type-options"] == "nosniff"
