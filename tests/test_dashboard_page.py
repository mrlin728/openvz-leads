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


# ── Theming ──

def _css(html: str) -> str:
    return html[: html.index("</style>")]


def test_every_theme_token_is_defined_in_both_palettes():
    """A token defined only on :root keeps its dark value under the light theme.

    That is how the toast ended up as near-black behind the light theme's
    darker accent text: the ink moved and the surface did not.
    """
    import re

    css = _css(dashboard._read_page())
    dark_block = css[css.index(":root {"): css.index('[data-theme="light"]')]
    dark = set(re.findall(r"(--[a-z0-9-]+):", dark_block))

    forced = css[css.index(':root[data-theme="light"] {'):]
    forced = forced[: forced.index("\n  }")]
    light = set(re.findall(r"(--[a-z0-9-]+):", forced))

    # Fonts and the chart steps are deliberately shared across both themes:
    # the four chart hues were validated against a light and a dark surface.
    shared = {"--mono", "--sans", "--chart-1", "--chart-2", "--chart-3", "--chart-4"}
    missing = dark - light - shared
    assert not missing, f"tokens with no light value: {sorted(missing)}"


def test_no_rule_hardcodes_a_surface_colour():
    """Colours in rules must come from tokens, or the theme cannot move them."""
    import re

    css = _css(dashboard._read_page())
    body = css[css.index("  * { margin: 0"):]
    # Comments explain choices and name colours while doing so; only rules count.
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    offenders = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        for literal in re.findall(r"#[0-9a-fA-F]{6}\b", line):
            offenders.append((literal, stripped[:70]))

    # The brand mark is a fixed logo, not a themed surface.
    allowed = {"#2fbf82", "#17795a", "#04140d"}
    real = [o for o in offenders if o[0] not in allowed]
    assert not real, f"hardcoded colours in rules: {real}"


def test_all_three_theme_states_are_expressed():
    """System default, forced light, forced dark — each needs its own rule."""
    css = _css(dashboard._read_page())
    assert ':root[data-theme="light"]' in css, "no forced-light palette"
    assert "prefers-color-scheme: light" in css, "system light is not handled"
    assert ':root:not([data-theme="dark"])' in css, "forced dark cannot beat a light OS"
    assert 'html[data-theme="light"] { color-scheme: light; }' in css


def test_keyboard_shortcuts_do_not_fire_while_typing():
    """A shortcut that fires mid-sentence in the review editor is a bug."""
    html = dashboard._read_page()
    assert "function isTyping" in html
    assert "if (isTyping(document.activeElement)) return;" in html


def test_the_page_carries_its_own_favicon():
    """Otherwise every load logs a 404 for /favicon.ico."""
    assert 'rel="icon"' in dashboard._read_page()
