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


def test_no_tab_calls_a_sending_provider_required():
    """Sending is opt-in. The UI must never say otherwise.

    The whole pipeline — find, analyse, draft, export — runs with no provider
    and no keys. Copy that labels Instantly "required" tells a new user they
    are 0% set up while holding a working install, and sends them off to buy
    a Growth plan before they have seen a single draft.
    """
    html = dashboard._read_page()
    for phrase in (
        "Instantly API Key (required)",
        "Instantly API 密钥（必填）",
    ):
        assert phrase not in html, f"UI still calls Instantly required: {phrase!r}"

    # The Instantly lede sits next to the key input in Settings; it opened
    # with a bare "Required." / "必填。" before this was fixed.
    for opener in ("'settings.instantlyLede': 'Required.", "'settings.instantlyLede': '必填。"):
        assert opener not in html


def test_getting_started_does_not_open_with_an_api_key():
    """Step one is teaching it the product, not buying a sending plan."""
    html = dashboard._read_page()
    for line in html.splitlines():
        if "help.startSteps" not in line:
            continue
        first_step = line.split("1.", 1)[1][:80]
        assert "Instantly" not in first_step, f"step one still wants a key: {first_step!r}"


def test_every_pipeline_status_has_a_badge_style():
    """A status with no rule renders as bare text beside styled neighbours."""
    from openvz_leads import pipeline

    html = dashboard._read_page()
    for stage in pipeline.STAGES:
        assert f".badge-{stage}" in html, f"no badge style for stage {stage!r}"


def test_every_campaign_status_has_a_badge_style():
    html = dashboard._read_page()
    for status in (
        "draft", "pending_review", "approved", "rejected", "active", "failed",
    ):
        assert f".badge-{status}" in html, f"no badge style for campaign status {status!r}"
