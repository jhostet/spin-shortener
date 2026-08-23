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

# The redirect component's HTML templates are the app's other served pages.
# They belong to a Go component, not this one, and a Python test in gui-pages
# policing them is admittedly cross-component — but they are the only HTML
# outside ROUTES, their CSP is the strictest in the app (script-src 'none',
# style-src 'self'), and package main is not host-testable at all, so there
# is nowhere better for this to live. A glob rather than a hardcoded list, so
# a fifth template added later (e.g. a future redirect-served page) is
# covered the moment it exists, matching PAGES/SCRIPTS's own idiom.
REDIRECT_DIR = REPO_ROOT / "redirect"
REDIRECT_TEMPLATES = sorted(REDIRECT_DIR.glob("*.html"))

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

# Any <script> tag at all, including one with a src= attribute. Strictly
# stronger than INLINE_SCRIPT (which permits <script src=…>) — this is what
# makes the redirect templates' always-light theming decision enforced rather
# than merely remembered: adding <script src="/theme-init.js"> without also
# widening errorPageCSP would be blocked in the browser and caught by nothing
# else if this test didn't exist.
ANY_SCRIPT_TAG = re.compile(r"<script", re.IGNORECASE)

# Wording that would make the 404 distinguish *why* a slug 404s. CLAUDE.md,
# "Security tradeoffs": an absent slug, a disabled link and a link outside its
# [start_at, end_at) window must be indistinguishable to a probing visitor.
# redirect/linkgate/resolve_test.go pins the three causes equal to each other
# at the Disposition level; nothing pins a rendered byte, which is exactly
# where a well-meaning "this link has expired" would land. This list is the
# guard for that gap.
FORBIDDEN_404_WORDS = (
    "expire",
    "expiring",
    "disabled",
    "schedul",
    "not yet",
    "inactive",
    "deleted",
    "no longer",
)


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


def test_redirect_templates_discovered():
    """A glob that silently matches nothing (or stops matching prompt.html
    after a rename) would pass every test below — the same failure mode
    test_pages_list_is_not_empty/test_scripts_list_is_not_empty already guard
    against for the other two lists."""
    assert len(REDIRECT_TEMPLATES) >= 4, (
        f"expected at least 4 templates under redirect/*.html, found "
        f"{[p.name for p in REDIRECT_TEMPLATES]}"
    )
    assert REDIRECT_DIR / "prompt.html" in REDIRECT_TEMPLATES


# All four redirect-served pages (the password prompt plus the three error
# pages) render under script-src 'none'; style-src 'self', the strictest CSP
# in the app — so an inline <script>, <style> block, style attribute or
# on<event>= handler added to any of them is not merely blocked but blocked
# harder than on any GUI page. prompt.html is also the page whose single
# style="color: red" was the last 'unsafe-inline' in the application, so it
# is exactly the kind of file most likely to regrow one.
@pytest.mark.parametrize("template", REDIRECT_TEMPLATES, ids=lambda p: p.name)
def test_redirect_template_has_no_inline_script(template):
    src = template.read_text(encoding="utf-8")
    assert not INLINE_SCRIPT.search(src), f"{template.name} has an inline <script>; its CSP is script-src 'none'"


@pytest.mark.parametrize("template", REDIRECT_TEMPLATES, ids=lambda p: p.name)
def test_redirect_template_has_no_style_block(template):
    src = template.read_text(encoding="utf-8")
    assert not STYLE_BLOCK.search(src), f"{template.name} has a <style> block; its CSP is style-src 'self'"


@pytest.mark.parametrize("template", REDIRECT_TEMPLATES, ids=lambda p: p.name)
def test_redirect_template_has_no_style_attribute(template):
    src = template.read_text(encoding="utf-8")
    assert not STYLE_ATTR.search(src), (
        f'{template.name} has a style="..." attribute — use theme.css\'s .form-error '
        "(or another shared class) as DESIGN.md requires; that attribute was the "
        "last 'unsafe-inline' in the app"
    )


@pytest.mark.parametrize("template", REDIRECT_TEMPLATES, ids=lambda p: p.name)
def test_redirect_template_has_no_event_handler(template):
    src = template.read_text(encoding="utf-8")
    assert not EVENT_HANDLER.search(src), f"{template.name} has an on<event>= handler; its CSP is script-src 'none'"


# Strictly stronger than test_redirect_template_has_no_inline_script: no
# redirect-served page may load a <script src=…> either, not just an inline
# one. This is what enforces the always-light theming decision
# (docs/plans/redirect-error-pages.md, Trade-offs #2) — a future
# <script src="/theme-init.js"> would be silently blocked by the browser's
# CSP with nothing else in this codebase catching it before that.
@pytest.mark.parametrize("template", REDIRECT_TEMPLATES, ids=lambda p: p.name)
def test_redirect_template_has_no_script_tag_at_all(template):
    src = template.read_text(encoding="utf-8")
    assert not ANY_SCRIPT_TAG.search(src), (
        f"{template.name} has a <script> tag; every redirect-served page is "
        "script-src 'none' and always renders light — see "
        "docs/plans/redirect-error-pages.md Trade-offs #2 before adding one"
    )


# All three error pages are near-identical near-copies of each other and are
# exactly where a stylesheet update lands in one and not the others.
ERROR_PAGES = sorted(REDIRECT_DIR.glob("error-*.html"))


def test_error_pages_discovered():
    assert len(ERROR_PAGES) == 3, f"expected 3 error-*.html templates, found {[p.name for p in ERROR_PAGES]}"


@pytest.mark.parametrize("template", ERROR_PAGES, ids=lambda p: p.name)
def test_error_page_links_both_stylesheets_with_leading_slash(template):
    src = template.read_text(encoding="utf-8")
    assert '"/vendor/pico.min.css"' in src, (
        f"{template.name} must link /vendor/pico.min.css with a leading slash — "
        "it is served from /r/{{slug}}, so a relative path would resolve under /r/"
    )
    assert '"/theme.css"' in src, (
        f"{template.name} must link /theme.css with a leading slash — "
        "it is served from /r/{{slug}}, so a relative path would resolve under /r/"
    )


def test_error_404_copy_does_not_distinguish_why_the_slug_is_unavailable():
    """CLAUDE.md, "Security tradeoffs": an absent slug, a disabled link and a
    link outside its [start_at, end_at) window must render an indistinguishable
    404 to a probing visitor. redirect/linkgate/resolve_test.go pins the three
    causes equal to each other at the Disposition level; this pins the
    rendered copy, which nothing else does."""
    src = (REDIRECT_DIR / "error-404.html").read_text(encoding="utf-8").lower()
    # Strip the leading HTML comment, which legitimately discusses these words
    # while documenting the very property this test enforces.
    body = re.sub(r"<!--.*?-->", "", src, flags=re.DOTALL)
    for word in FORBIDDEN_404_WORDS:
        assert word not in body, (
            f'error-404.html\'s rendered copy contains "{word}", which could let a '
            "visitor distinguish an absent slug from a disabled/expired/scheduled "
            "one — see CLAUDE.md's Security tradeoffs"
        )
