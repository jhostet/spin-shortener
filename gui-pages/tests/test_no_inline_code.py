"""Guards the CSP's `script-src 'self'; style-src 'self'` (see routing.py).

Those directives are only safe because no served page contains inline code.
Nothing else enforces that: a CSP violation does not fail a test, it fails a
page in a browser, silently, in whatever environment it reaches first. This
test is the enforcement.

The page list is derived from `ROUTES` rather than hardcoded, so a page added
to the component is covered automatically instead of being quietly exempt.
Reading real files from `gui/` is fine here — the constraint that matters is
that `routing.py` itself never touches the filesystem (it takes an injected
`read_file`), and it still doesn't.
"""

import re
from pathlib import Path

import pytest

from routing import ROUTES

GUI_DIR = Path(__file__).resolve().parents[2] / "gui"

PAGES = sorted(set(ROUTES.values()))

# A <script> with no src= attribute, i.e. one with inline content. Matches
# `<script>` and `<script type="...">` but not `<script src="app.js">`.
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc\s*=)[^>]*>", re.IGNORECASE)
STYLE_BLOCK = re.compile(r"<style[^>]*>", re.IGNORECASE)
STYLE_ATTR = re.compile(r"\bstyle\s*=", re.IGNORECASE)
# on<event>= handler attributes (onclick, onsubmit, ...). Requires a preceding
# space so it cannot match inside an unrelated attribute value or word.
EVENT_HANDLER = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)


def _read(relative_path: str) -> str:
    return (GUI_DIR / relative_path).read_text(encoding="utf-8")


def test_pages_list_is_not_empty():
    """A regex-based guard that silently checks nothing is worse than no guard."""
    assert PAGES, "ROUTES produced no pages to check"


@pytest.mark.parametrize("page", PAGES)
def test_page_has_no_inline_script(page):
    assert not INLINE_SCRIPT.search(_read(page)), (
        f"{page} has an inline <script>; move it to a sibling .js file and add "
        f"an exact route for it in spin.toml, or the CSP will block it"
    )


@pytest.mark.parametrize("page", PAGES)
def test_page_has_no_style_block(page):
    assert not STYLE_BLOCK.search(_read(page)), (
        f"{page} has a <style> block; move it to a sibling .css file and add "
        f"an exact route for it in spin.toml, or the CSP will block it"
    )


@pytest.mark.parametrize("page", PAGES)
def test_page_has_no_style_attribute(page):
    assert not STYLE_ATTR.search(_read(page)), (
        f'{page} has a style="..." attribute; a nonce cannot cover one, so use '
        f"the hidden attribute or a class from theme.css instead"
    )


@pytest.mark.parametrize("page", PAGES)
def test_page_has_no_inline_event_handler(page):
    assert not EVENT_HANDLER.search(_read(page)), (
        f"{page} has an on<event>= handler attribute; use addEventListener, "
        f"as every other handler in this app already does"
    )


# app.js is served by the gui component and so is not in ROUTES, but it builds
# the nav markup for every page via innerHTML. A style attribute in one of its
# templates is checked by the CSP exactly like a parsed one — that is where the
# seventh and last style attribute lived, and it is the file most likely to
# regrow one.
def test_app_js_has_no_style_attribute_in_templates():
    assert not STYLE_ATTR.search(_read("app.js")), (
        'gui/app.js has a style="..." attribute in a template; an '
        "innerHTML-inserted style attribute is blocked by the CSP just like a "
        "parsed one — use the hidden attribute instead"
    )


def test_app_js_has_no_inline_script_tag_in_templates():
    assert not INLINE_SCRIPT.search(_read("app.js")), (
        "gui/app.js builds a <script> tag without a src attribute; injected "
        "inline script is blocked by the CSP"
    )
