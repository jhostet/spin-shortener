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

REPO_ROOT = Path(__file__).resolve().parents[2]
GUI_DIR = REPO_ROOT / "gui"

PAGES = sorted(set(ROUTES.values()))

# The redirect component's password prompt is the app's other served HTML.
# It belongs to a Go component, not this one, and a Python test in gui-pages
# policing it is admittedly cross-component — but it is the only HTML outside
# ROUTES, its CSP is the strictest in the app (script-src 'none',
# style-src 'self'), and package main is not host-testable at all, so there
# is nowhere better for this to live. Without it the one page that takes a
# password is the one page with no inline-code guard.
PROMPT_HTML = REPO_ROOT / "redirect" / "prompt.html"

# Every first-party script the gui component serves, discovered rather than
# listed, for the same reason PAGES is derived from ROUTES: a new page's
# script is covered the moment it exists instead of being quietly exempt
# until someone remembers to add it here. vendor/ is third-party and not
# ours to police.
SCRIPTS = sorted(
    str(p.relative_to(GUI_DIR))
    for p in GUI_DIR.rglob("*.js")
    if "vendor" not in p.relative_to(GUI_DIR).parts
)

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


# The scripts are served by the gui component and so are not in ROUTES, but
# they build page markup with innerHTML, and an injected style attribute or
# srcless <script> is checked by the CSP exactly like a parsed one. app.js is
# where the seventh and last style attribute lived and is the file most likely
# to regrow one; dashboard.js, admin/users.js and links/detail.js build markup
# the same way and were an unguarded gap until this was widened.
def test_scripts_list_is_not_empty():
    """Globbing that matches nothing would silently pass every test below."""
    assert SCRIPTS, "no first-party scripts discovered under gui/"


@pytest.mark.parametrize("filename", SCRIPTS)
def test_script_has_no_style_attribute_in_templates(filename):
    assert not STYLE_ATTR.search(_read(filename)), (
        f'gui/{filename} has a style="..." attribute in a template; an '
        "innerHTML-inserted style attribute is blocked by the CSP just like a "
        "parsed one — use the hidden attribute instead"
    )


@pytest.mark.parametrize("filename", SCRIPTS)
def test_script_has_no_inline_script_tag_in_templates(filename):
    assert not INLINE_SCRIPT.search(_read(filename)), (
        f"gui/{filename} builds a <script> tag without a src attribute; "
        "injected inline script is blocked by the CSP"
    )


# The password prompt renders under script-src 'none'; style-src 'self', so an
# inline <script>, <style> block, style attribute or on<event>= handler added
# to it is not merely blocked but blocked harder than on any GUI page. It is
# also the page whose single style="color: red" was the last 'unsafe-inline'
# in the application, so it is exactly the file most likely to regrow one.
def test_password_prompt_has_no_inline_code():
    src = PROMPT_HTML.read_text(encoding="utf-8")
    assert not INLINE_SCRIPT.search(src), "redirect/prompt.html has an inline <script>; its CSP is script-src 'none'"
    assert not STYLE_BLOCK.search(src), "redirect/prompt.html has a <style> block; its CSP is style-src 'self'"
    assert not STYLE_ATTR.search(src), (
        'redirect/prompt.html has a style="..." attribute — use theme.css\'s '
        ".form-error (or another shared class) as DESIGN.md requires; that "
        "attribute was the last 'unsafe-inline' in the app"
    )
    assert not EVENT_HANDLER.search(src), "redirect/prompt.html has an on<event>= handler; its CSP is script-src 'none'"
