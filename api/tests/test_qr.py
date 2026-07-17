import json
from unittest.mock import patch

import auth
import links
import qr
from responses import Request
from tests.fakes import FakeStore

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _principal(username="alice", role="user"):
    return auth.Principal(username=username, role=role, permissions=[], csrf_token="x")


def _create_request(payload):
    return Request(method="POST", uri="/api/links", headers={}, body=json.dumps(payload).encode("utf-8"))


async def _make_link(store, owner="alice", target_url="https://example.com/x"):
    created = await links.handle_create(store, _principal(username=owner), _create_request({"target_url": target_url}))
    return json.loads(created.body)["slug"]


async def test_qr_not_found():
    store = FakeStore()
    resp = await qr.handle_qr(store, _principal(), "doesnotexist", {}, "http://localhost:3000")
    assert resp.status == 404


async def test_qr_forbidden_for_non_owner():
    store = FakeStore()
    slug = await _make_link(store, owner="alice")
    resp = await qr.handle_qr(store, _principal(username="bob"), slug, {}, "http://localhost:3000")
    assert resp.status == 403


async def test_qr_admin_can_access_others_links():
    store = FakeStore()
    slug = await _make_link(store, owner="alice")
    resp = await qr.handle_qr(store, _principal(username="admin", role="admin"), slug, {}, "http://localhost:3000")
    assert resp.status == 200


async def test_qr_svg_default_format():
    store = FakeStore()
    slug = await _make_link(store)
    resp = await qr.handle_qr(store, _principal(), slug, {}, "http://localhost:3000")
    assert resp.status == 200
    assert resp.headers["content-type"] == "image/svg+xml"
    assert resp.body.startswith(b"<?xml")


async def test_qr_png_format():
    store = FakeStore()
    slug = await _make_link(store)
    resp = await qr.handle_qr(store, _principal(), slug, {"format": ["png"]}, "http://localhost:3000")
    assert resp.status == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.body.startswith(PNG_MAGIC)


async def test_qr_print_size_larger_than_web():
    store = FakeStore()
    slug = await _make_link(store)
    web_resp = await qr.handle_qr(store, _principal(), slug, {"format": ["png"], "size": ["web"]}, "http://localhost:3000")
    print_resp = await qr.handle_qr(store, _principal(), slug, {"format": ["png"], "size": ["print"]}, "http://localhost:3000")
    assert len(print_resp.body) > len(web_resp.body)


async def test_qr_invalid_format():
    store = FakeStore()
    slug = await _make_link(store)
    resp = await qr.handle_qr(store, _principal(), slug, {"format": ["bmp"]}, "http://localhost:3000")
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_format"


async def test_qr_invalid_size():
    store = FakeStore()
    slug = await _make_link(store)
    resp = await qr.handle_qr(store, _principal(), slug, {"size": ["huge"]}, "http://localhost:3000")
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid_size"


async def test_qr_download_sets_content_disposition():
    store = FakeStore()
    slug = await _make_link(store)
    resp = await qr.handle_qr(store, _principal(), slug, {"download": ["1"]}, "http://localhost:3000")
    assert resp.headers["content-disposition"] == f'attachment; filename="{slug}-qr.svg"'


async def test_qr_no_download_omits_content_disposition():
    store = FakeStore()
    slug = await _make_link(store)
    resp = await qr.handle_qr(store, _principal(), slug, {}, "http://localhost:3000")
    assert "content-disposition" not in resp.headers


async def test_qr_encodes_short_link_not_target_url():
    store = FakeStore()
    slug = await _make_link(store, target_url="https://evil-should-not-appear.example/secret")

    with patch("qr.qrcode.make", wraps=qr.qrcode.make) as mock_make:
        resp = await qr.handle_qr(store, _principal(), slug, {}, "http://localhost:3000")

    assert resp.status == 200
    encoded_data = mock_make.call_args[0][0]
    assert encoded_data == f"http://localhost:3000/r/{slug}"
    assert "evil-should-not-appear" not in encoded_data
