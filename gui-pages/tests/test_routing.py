import pytest

import errorpages
from routing import SECURITY_HEADERS, build_response, internal_error_response, resolve_file


@pytest.mark.parametrize(
    "path,expected_file",
    [
        ("/", "index.html"),
        ("/index.html", "index.html"),
        ("/login.html", "login.html"),
        ("/dashboard.html", "dashboard.html"),
        ("/admin/", "admin/index.html"),
        ("/admin/index.html", "admin/index.html"),
        ("/admin/users.html", "admin/users.html"),
        ("/admin/store-maintenance.html", "admin/store-maintenance.html"),
        ("/admin/url-policy.html", "admin/url-policy.html"),
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
        # These are deliberately NOT pages — they're answered by
        # nonpages.non_page_response instead. See test_nonpages.py.
        ("/robots.txt", None),
        ("/favicon.ico", None),
        ("/.well-known/security.txt", None),
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
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.body == errorpages.ERROR_PAGES[404]
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
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.body == errorpages.ERROR_PAGES[500]
    for key, value in SECURITY_HEADERS.items():
        assert response.headers[key] == value


def test_error_page_bodies_are_nonempty_and_distinct():
    """A shared shell rendered twice is exactly where both pages end up
    identical by copy-paste — this guards against that."""
    not_found = errorpages.ERROR_PAGES[404]
    internal_error = errorpages.ERROR_PAGES[500]
    assert isinstance(not_found, bytes) and not_found
    assert isinstance(internal_error, bytes) and internal_error
    assert not_found != internal_error


def test_build_response_calls_on_read_error_once_with_path_filename_and_exc():
    calls = []

    def failing_read_file(_):
        raise FileNotFoundError("simulated ROUTES/filesystem drift")

    def spy(path, filename, exc):
        calls.append((path, filename, exc))

    response = build_response("/login.html", failing_read_file, spy)

    assert len(calls) == 1
    path, filename, exc = calls[0]
    assert path == "/login.html"
    assert filename == "login.html"
    assert isinstance(exc, FileNotFoundError)

    assert response.status == 500
    assert response.body == errorpages.ERROR_PAGES[500]
    for key, value in SECURITY_HEADERS.items():
        assert response.headers[key] == value


def test_build_response_with_no_on_read_error_is_back_compat():
    def failing_read_file(_):
        raise FileNotFoundError("simulated ROUTES/filesystem drift")

    response = build_response("/login.html", failing_read_file)

    assert response.status == 500
    assert response.body == errorpages.ERROR_PAGES[500]
    for key, value in SECURITY_HEADERS.items():
        assert response.headers[key] == value


def test_build_response_on_read_error_raising_does_not_break_the_response():
    """Pins 'a diagnostic must never be able to break the response it is
    diagnosing' — a raising reporter must not turn the styled 500 into an
    unhandled exception with no headers."""

    def failing_read_file(_):
        raise FileNotFoundError("simulated ROUTES/filesystem drift")

    def raising_spy(path, filename, exc):
        raise RuntimeError("obs.py bug or closed stderr")

    response = build_response("/login.html", failing_read_file, raising_spy)

    assert response.status == 500
    assert response.body == errorpages.ERROR_PAGES[500]
    for key, value in SECURITY_HEADERS.items():
        assert response.headers[key] == value


def test_build_response_404_and_nonpages_paths_never_call_on_read_error():
    def spy(path, filename, exc):
        raise AssertionError("on_read_error must not be called for a 404 or nonpages path")

    def fail_read_file(_):
        raise AssertionError("should not read a file for an unknown/nonpages path")

    response_404 = build_response("/does-not-exist.html", fail_read_file, spy)
    assert response_404.status == 404

    response_robots = build_response("/robots.txt", fail_read_file, spy)
    assert response_robots.status == 200


def test_security_headers_lock_down_framing_and_plugins():
    csp = SECURITY_HEADERS["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert SECURITY_HEADERS["x-frame-options"] == "DENY"
    assert SECURITY_HEADERS["x-content-type-options"] == "nosniff"


def test_internal_error_response_is_500_with_error_page_and_security_headers():
    response = internal_error_response()
    assert response.status == 500
    assert response.body == errorpages.ERROR_PAGES[500]
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    for key, value in SECURITY_HEADERS.items():
        assert response.headers[key] == value


def test_build_response_read_failure_500_equals_internal_error_response():
    """The pin that stops the catch-all's 500 (docs/plans/gui-pages-unhandled-
    exception-guard.md) and the read-failure's 500 from drifting apart —
    build_response's except OSError branch must return the exact same object
    internal_error_response() constructs."""

    def failing_read_file(_):
        raise FileNotFoundError("simulated ROUTES/filesystem drift")

    response = build_response("/login.html", failing_read_file)
    expected = internal_error_response()

    assert response.status == expected.status
    assert response.headers == expected.headers
    assert response.body == expected.body
