"""Pins the served bytes for robots.txt, favicon.ico and /.well-known/* —
see gui-pages/nonpages.py and docs/plans/robots-favicon-and-well-known.md.

All but the two structural tests go through `build_response`, not
`non_page_response` alone, so they pin the actual served response (headers
included) rather than an internal shape.

Note: these bytes are deliberately NOT added to test_no_inline_code.py's
PAGES (which derives from ROUTES.values(), and nothing here is in ROUTES).
That guard exists to keep a *document* free of inline script/style under a
script-src/style-src 'self' CSP. A text/plain response under `nosniff` is
never parsed as a document, so those four regexes have nothing to say about
it — test_robots_txt_is_utf8_and_contains_no_markup below (`"<" not in body`)
is a strictly stronger, simpler invariant that makes "this became HTML"
structurally impossible. Do not "fix" this omission by adding these bytes to
test_no_inline_code.py.
"""

import tomllib
from pathlib import Path

import pytest

import errorpages
import nonpages
from routing import ROUTES, SECURITY_HEADERS, build_response

REPO_ROOT = Path(__file__).resolve().parents[2]
SPIN_TOML = REPO_ROOT / "spin.toml"


def _fail_read_file(_):
    raise AssertionError("should not read a file for a non-page path")


def test_robots_txt_is_served_as_plain_text_with_security_headers():
    response = build_response("/robots.txt", _fail_read_file)

    assert response.status == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.body == nonpages.ROBOTS_TXT
    for key, value in SECURITY_HEADERS.items():
        assert response.headers[key] == value


def test_robots_txt_disallows_everything():
    lines = [
        line.strip()
        for line in nonpages.ROBOTS_TXT.decode("utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    user_agent_lines = [line for line in lines if line.lower().startswith("user-agent:")]
    disallow_lines = [line for line in lines if line.lower().startswith("disallow:")]
    allow_lines = [line for line in lines if line.lower().startswith("allow:")]

    assert user_agent_lines == ["User-agent: *"], (
        "ROBOTS_TXT must have exactly one `User-agent: *` directive"
    )
    assert disallow_lines == ["Disallow: /"], (
        "ROBOTS_TXT must have exactly one `Disallow: /` directive — a crawler "
        "that follows a published short link is recorded as a real click "
        "(analytics:count:<slug>:<shard>) that nothing downstream can tell "
        "apart from a person's, since redirect reads no User-Agent header. "
        "Narrowing this disallow (e.g. to exempt /r/) re-opens that click "
        "inflation — see docs/plans/robots-favicon-and-well-known.md, Decision 1."
    )
    assert allow_lines == [], "ROBOTS_TXT must not contain any Allow: directive"


def test_robots_txt_is_utf8_and_contains_no_markup():
    text = nonpages.ROBOTS_TXT.decode("utf-8")  # raises if not valid UTF-8
    assert text.endswith("\n")
    assert "<" not in text


def test_favicon_ico_is_a_cheap_plain_404():
    response = build_response("/favicon.ico", _fail_read_file)

    assert response.status == 404
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.body != errorpages.ERROR_PAGES[404]
    assert len(response.body) < 100
    for key, value in SECURITY_HEADERS.items():
        assert response.headers[key] == value


@pytest.mark.parametrize(
    "path",
    [
        "/.well-known/security.txt",
        "/.well-known/change-password",
        "/.well-known/acme-challenge/tok3n",
        "/.well-known/",
        "/.well-known",
    ],
)
def test_well_known_paths_are_plain_404s(path):
    response = build_response(path, _fail_read_file)

    assert response.status == 404
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.body != errorpages.ERROR_PAGES[404]


def test_well_known_reliability_probe_is_not_200():
    """Implements the W3C "Detecting the reliability of HTTP status codes"
    probe (§3.1): a password manager checks that an unknown well-known path
    returns a non-200 before trusting /.well-known/change-password."""
    path = "/.well-known/resource-that-should-not-exist-whose-status-code-should-not-be-200"

    response = build_response(path, _fail_read_file)

    assert response.status != 200


@pytest.mark.parametrize(
    "path",
    [
        "/.well-knownx",
        "/.well-known-backup",
        "/nope",
        "/admin/nope",
    ],
)
def test_paths_outside_the_well_known_prefix_still_get_the_styled_page(path):
    response = build_response(path, _fail_read_file)

    assert response.status == 404
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.body == errorpages.ERROR_PAGES[404]


def test_non_page_paths_do_not_overlap_routes():
    assert set(nonpages.NON_PAGE_RESPONSES) & set(ROUTES) == set()
    assert not any(key.startswith("/.well-known") for key in ROUTES)


def test_no_gui_route_shadows_a_non_page_path():
    """Spin routes by specificity, so an exact /robots.txt (or /favicon.ico)
    route added to the gui static component later would silently make
    nonpages.NON_PAGE_RESPONSES dead code with no test failing anywhere else
    — this is what would catch it. Same tomllib-parse idiom as
    test_manifest_components.py; a grep is not a usable guard here either."""
    manifest = tomllib.loads(SPIN_TOML.read_text())

    gui_routes = {
        trigger["route"]
        for trigger in manifest["trigger"]["http"]
        if trigger.get("component") == "gui"
    }

    assert not (gui_routes & set(nonpages.NON_PAGE_RESPONSES))
