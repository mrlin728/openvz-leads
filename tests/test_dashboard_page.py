"""The dashboard page lives on disk now, so prove it ships and is served."""

import re

import pytest

from openvz_leads import paths
from openvz_leads import dashboard


def test_page_file_is_present():
    path = paths.static_file("dashboard.html")
    assert path.is_file(), f"dashboard.html missing at {path}"


def test_page_looks_like_the_dashboard():
    html = dashboard._read_page()
    assert html.startswith("<!DOCTYPE html>")
    assert html.rstrip().endswith("</html>")
    # The three things every tab depends on.
    assert "<nav>" in html
    assert "function showTab" in html
    assert "OpenVZ Leads" in html


def test_page_is_cached_but_reloads_on_change(tmp_path, monkeypatch):
    """Editing the markup must not need a server restart."""
    page = tmp_path / "dashboard.html"
    page.write_text("<!DOCTYPE html>one</html>", encoding="utf-8")
    monkeypatch.setattr(paths, "static_file", lambda name: page)
    monkeypatch.setattr(dashboard, "_page_cache", None)

    assert "one" in dashboard._read_page()

    import os

    page.write_text("<!DOCTYPE html>two</html>", encoding="utf-8")
    os.utime(page, (0, 0))  # force a different mtime
    assert "two" in dashboard._read_page()


def test_missing_page_degrades_instead_of_500ing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        paths, "static_file", lambda name: tmp_path / "does-not-exist.html"
    )
    monkeypatch.setattr(dashboard, "_page_cache", None)
    html = dashboard._read_page()
    assert "could" in html and "not be found" in html


def test_every_tab_button_has_a_matching_section():
    """A nav button pointing at nothing renders a blank page with no error."""
    html = dashboard._read_page()
    nav = html[html.index("<nav>"): html.index("</nav>")]
    tabs = set(re.findall(r"showTab\('([a-z]+)'", nav))
    assert tabs, "no tabs found in the nav"
    for tab in tabs:
        assert f'<div id="{tab}" class="section' in html, f"no section for tab {tab!r}"
