"""End-to-end proof that ASCII control characters in a destination URL are
rejected at ALL FOUR authoring paths — links.handle_create,
links.handle_update, bulk.handle_bulk_create, and bulk.handle_bulk_action's
"repoint" branch — not just some of them. Same structure and "nothing was
written" discipline as test_url_policy_enforcement.py, for the same reason:
a constraint enforced in two of three places is not enforced.

Why the constraint exists at all: the redirect component emits target_url
verbatim as the Location header of its 302, and the Go SDK serializes header
VALUES to the wire unvalidated (toWasiHeaders checks header names only —
confirmed in spin-go-sdk/v3@v3.0.0/http/http.go). So a stored URL like
"https://example.com/x\r\nX-Evil: yes" is a real CRLF in a live 302, not a
curiosity. urlparse strips \\t\\r\\n from its parsed view but keeps
\\x00-\\x08, \\x0b-\\x0c, \\x0e-\\x1f and \\x7f, and the ORIGINAL string is
what gets stored and later served — reproduced before this fix:
target_url_error() returned None for CRLF-, NUL-, DEL- and ESC-bearing URLs.

Percent-encoded forms (%0d%0a) are deliberately NOT rejected anywhere: they
are inert literal text inside the header value, decoded only by the *new*
URL the Location points at, never by this app's header emission.

The redirect side of this fix is pinned in Go
(linkgate.ParseLink rejects a control-bearing target as ErrUnsafeTargetURL
-> DispositionUnreadable), so a control-char target that reaches storage by
ANY route — restore bypasses the authoring choke point by design — is
refused there as a 500, never emitted.
"""

import json

import pytest

import auth
import bulk
import kvretry
import links
from responses import Request
from tests.fakes import FakeStore, fake_get_many


def _principal(username="alice", role="user", permissions=None):
    return auth.Principal(username=username, role=role, permissions=permissions or [], csrf_token="x")


def _links_request(payload):
    return Request(method="POST", uri="/api/links", headers={}, body=json.dumps(payload).encode("utf-8"))


def _patch_request(payload):
    return Request(method="PATCH", uri="/api/links/abc", headers={}, body=json.dumps(payload).encode("utf-8"))


def _bulk_request(payload):
    return Request(method="POST", uri="/api/links/bulk", headers={}, body=json.dumps(payload).encode("utf-8"))


def _bulk_action_request(payload):
    return Request(method="POST", uri="/api/links/bulk-action", headers={}, body=json.dumps(payload).encode("utf-8"))


CONFIGURED_DOMAINS = ["https://trrk.io", "http://localhost:3000"]


# Payloads for the JSON-body paths (create/update/repoint), where CRLF and LF
# arrive intact via JSON escape sequences. Authority-position CRLF matters too:
# urlparse strips it from its parsed view, so it would PASS the parser and
# only the explicit control-char check catches it.
JSON_BAD_TARGETS = [
    pytest.param("https://example.com/x\r\nX-Evil: yes", id="crlf-in-path"),
    pytest.param("https://example.com\r\nX-Evil: yes", id="crlf-in-authority"),
    pytest.param("https://example.com/x\nInjected: 1", id="lf-in-path"),
    pytest.param("https://example.com/x\tyes", id="tab"),
    pytest.param("https://example.com/\x00nul", id="nul"),
    pytest.param("https://example.com/\x1b[31mred\x1b[0m", id="ansi-esc"),
    pytest.param("https://example.com/\x7fdel", id="del"),
]

# Payloads for the bulk-create TEXT format, where CR/LF cannot exist inside a
# row (the parser normalizes them into row separators first) — the meaningful
# control characters are the surviving non-newline ones.
BULK_TEXT_BAD_TARGETS = [
    pytest.param("https://example.com/\x00nul", id="nul"),
    pytest.param("https://example.com/\x1b[31mred\x1b[0m", id="ansi-esc"),
    pytest.param("https://example.com/\x7fdel", id="del"),
]

OK_URL = "https://example.com/ok"


@pytest.mark.parametrize("target", JSON_BAD_TARGETS)
async def test_create_rejects_control_char_target_and_writes_nothing(target):
    store = FakeStore()
    before = dict(store._data)

    resp = await links.handle_create(store, _principal(), _links_request({"target_url": target}), CONFIGURED_DOMAINS)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_target_url"

    after = dict(store._data)
    assert after == before  # nothing written


@pytest.mark.parametrize("target", JSON_BAD_TARGETS)
async def test_update_rejects_control_char_target_and_writes_nothing(target):
    store = FakeStore()
    create_resp = await links.handle_create(store, _principal(), _links_request({"target_url": OK_URL}), CONFIGURED_DOMAINS)
    slug = json.loads(create_resp.body)["slug"]
    before = dict(store._data)

    resp = await links.handle_update(
        store, _principal(), slug,
        Request(method="PATCH", uri=f"/api/links/{slug}", headers={}, body=json.dumps({"target_url": target}).encode("utf-8")),
        CONFIGURED_DOMAINS,
    )
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_target_url"

    after = dict(store._data)
    assert after == before  # record unchanged


@pytest.mark.parametrize("target", BULK_TEXT_BAD_TARGETS)
async def test_bulk_create_rejects_control_char_target_and_writes_nothing(target):
    store = FakeStore()
    before = dict(store._data)

    text = f"good-one,{OK_URL}\nbad-one,{target}\n"
    resp = await bulk.handle_bulk_create(
        store, _principal(permissions=["links.create_custom_slug"]), _bulk_request({"text": text}),
        CONFIGURED_DOMAINS, fake_get_many, kvretry.direct,
    )
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["error"] == "bulk_validation_failed"
    assert any(e["error"] == "invalid_target_url" for e in body["row_errors"])

    after = dict(store._data)
    assert after == before  # ALL-OR-NOTHING: the good row was not written either


@pytest.mark.parametrize("target", JSON_BAD_TARGETS)
async def test_bulk_repoint_rejects_control_char_target_and_writes_nothing(target):
    store = FakeStore()
    users_store = FakeStore()
    create_resp = await links.handle_create(store, _principal(), _links_request({"target_url": OK_URL}), CONFIGURED_DOMAINS)
    slug = json.loads(create_resp.body)["slug"]
    before = dict(store._data)

    resp = await bulk.handle_bulk_action(
        store, users_store, _principal(),
        _bulk_action_request({"slugs": [slug], "action": "repoint", "target_url": target}),
        CONFIGURED_DOMAINS, fake_get_many, kvretry.direct,
    )
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_target_url"

    after = dict(store._data)
    assert after == before  # record unchanged


def test_control_free_target_still_valid():
    """Guard against the four paths above failing for the wrong reason: the
    check must be narrow (control chars only), not a general URL veto."""
    assert links.target_url_error(OK_URL) is None
    assert links.target_url_error("https://example.com/x%0d%0a") is None  # percent-encoded stays accepted
    assert links.target_url_error("https://example.com/x?utm_source=na&utm_campaign=q4") is None