"""A deploy must actually reach the browser.

2026-11-05: the server was serving the new 20 894-byte stylesheet while the browser kept a cached copy
that had no `.theme-toggle` rule in it. The theme button fell back to pico's default blue slab and
stacked above "Sair", so the panel looked broken while the server was perfectly fine. A hard refresh
fixes it for whoever knows to do that - which is not the client.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.web.deps import STATIC_DIR, static_url

BASE = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "base.html"


def test_every_cacheable_asset_is_versioned():
    """The three files that change with a deploy. Icons are excluded on purpose: they are stable, and a
    stale favicon costs nothing."""
    html = BASE.read_text(encoding="utf-8")
    for name in ("app.css", "pico.min.css", "htmx.min.js"):
        assert f"static_url('{name}')" in html, f"{name} would be served from a stale cache after a deploy"
        assert f'"/static/{name}"' not in html, f"{name} still has an unversioned link"


def test_the_version_changes_when_the_file_does(tmp_path, monkeypatch):
    import app.web.deps as deps

    asset = tmp_path / "x.css"
    asset.write_text("a{}", encoding="utf-8")
    monkeypatch.setattr(deps, "STATIC_DIR", tmp_path)
    first = deps.static_url("x.css")

    import os
    os.utime(asset, (1_700_000_000, 1_700_000_000))
    second = deps.static_url("x.css")
    assert first != second, "the query string must follow the file, or a deploy is invisible"
    assert second.endswith("?v=1700000000")


def test_the_same_file_keeps_the_same_url():
    """Caching between deploys is the point; a random token every request would defeat it."""
    assert static_url("app.css") == static_url("app.css")


def test_a_missing_file_does_not_raise():
    """A typo in a template must not become a 500 on every page."""
    assert static_url("does-not-exist.css") == "/static/does-not-exist.css"


def test_the_real_assets_exist_and_are_versioned():
    for name in ("app.css", "pico.min.css", "htmx.min.js"):
        assert (STATIC_DIR / name).exists(), f"{name} is linked but not shipped"
        assert re.search(r"\?v=\d+$", static_url(name))


def test_the_theme_icons_have_intrinsic_dimensions():
    """An SVG with only a viewBox collapses to a few pixels when its CSS is missing - which is exactly
    what the stale cache looked like: a tiny dot on a blue slab."""
    html = BASE.read_text(encoding="utf-8")
    icons = re.findall(r"<svg class=\"i-(?:light|dark)\"[^>]*>", html)
    assert len(icons) == 2
    for tag in icons:
        assert 'width="18"' in tag and 'height="18"' in tag, tag
