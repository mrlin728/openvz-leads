"""Tier selection and degradation in the page reader."""

import httpx
import pytest

from openvz_leads.config import CrawlConfig
from openvz_leads.integrations.crawler import (
    MIN_USEFUL_CHARS,
    PageContent,
    PageReader,
    looks_blocked,
)

PAGE = (
    "<html><head><title>Northwind</title></head><body>"
    "<nav>menu</nav><script>junk()</script>"
    "<p>" + ("Regional freight forwarding across the Benelux. " * 12) + "</p>"
    "<footer>legal</footer></body></html>"
)

CHALLENGE = "<html><body>Just a moment... Checking your browser</body></html>"


def reader(**kwargs):
    # No politeness delay: these tests do not need to be slow to be correct.
    kwargs.setdefault("delay_seconds", 0)
    return PageReader(CrawlConfig(**kwargs))


def fake_get(status_code=200, text=PAGE):
    async def _get(self, url, headers=None):
        return httpx.Response(status_code, text=text)

    return _get


class TestBlockDetection:
    def test_a_challenge_page_is_recognised(self):
        assert looks_blocked(CHALLENGE)

    def test_a_real_page_is_not(self):
        assert not looks_blocked(PAGE)

    def test_empty_is_not_blocked(self):
        assert not looks_blocked("")


class TestPageContent:
    def test_markdown_is_preferred_when_present(self):
        page = PageContent(text="plain", markdown="# heading")
        assert page.best_text() == "# heading"

    def test_a_cookie_banner_is_not_a_usable_page(self):
        assert not PageContent(text="We use cookies").usable()

    def test_a_blocked_page_is_never_usable(self):
        assert not PageContent(text="x" * 5000, blocked=True).usable()


class TestTierOrder:
    def test_with_nothing_installed_it_is_just_the_basic_tier(self, monkeypatch):
        monkeypatch.setattr(
            "openvz_leads.integrations.crawler.crawl4ai_available", lambda: False
        )
        monkeypatch.setattr(
            "openvz_leads.integrations.crawler.browser_use_available", lambda: False
        )
        assert reader()._tier_order() == ["basic"]

    def test_crawl4ai_goes_first_when_available(self, monkeypatch):
        monkeypatch.setattr(
            "openvz_leads.integrations.crawler.crawl4ai_available", lambda: True
        )
        monkeypatch.setattr(
            "openvz_leads.integrations.crawler.browser_use_available", lambda: False
        )
        assert reader()._tier_order() == ["crawl4ai", "basic"]

    def test_the_browser_is_last_and_only_when_asked_for(self, monkeypatch):
        monkeypatch.setattr(
            "openvz_leads.integrations.crawler.crawl4ai_available", lambda: False
        )
        monkeypatch.setattr(
            "openvz_leads.integrations.crawler.browser_use_available", lambda: True
        )
        assert reader()._tier_order() == ["basic"]
        assert reader(browser_fallback=True)._tier_order() == ["basic", "browser_use"]

    def test_asking_for_a_tier_that_is_not_installed_still_reads_the_page(
        self, monkeypatch
    ):
        # An explicit choice is a preference, not a suicide pact.
        monkeypatch.setattr(
            "openvz_leads.integrations.crawler.crawl4ai_available", lambda: False
        )
        assert reader(provider="crawl4ai")._tier_order() == ["basic"]

    def test_forcing_basic_forces_basic(self, monkeypatch):
        monkeypatch.setattr(
            "openvz_leads.integrations.crawler.crawl4ai_available", lambda: True
        )
        assert reader(provider="basic")._tier_order() == ["basic"]

    def test_an_unknown_provider_is_rejected_at_config_time(self):
        with pytest.raises(ValueError):
            CrawlConfig(provider="selenium")


@pytest.mark.asyncio
class TestReading:
    async def test_the_basic_tier_returns_the_words_not_the_markup(
        self, monkeypatch
    ):
        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get())
        page = await reader(provider="basic").read("northwind.test")
        assert page.via == "basic"
        assert page.title == "Northwind"
        assert "Benelux" in page.text
        assert "junk()" not in page.text and "menu" not in page.text

    async def test_a_bare_domain_gets_a_scheme(self, monkeypatch):
        seen = {}

        async def _get(self, url, headers=None):
            seen["url"] = url
            return httpx.Response(200, text=PAGE)

        monkeypatch.setattr(httpx.AsyncClient, "get", _get)
        await reader(provider="basic").read("northwind.test")
        assert seen["url"] == "https://northwind.test"

    async def test_max_chars_is_honoured(self, monkeypatch):
        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get())
        page = await reader(provider="basic", max_chars=500).read("northwind.test")
        assert len(page.text) <= 500

    async def test_a_challenge_page_is_reported_blocked(self, monkeypatch):
        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get(text=CHALLENGE))
        page = await reader(provider="basic").read("northwind.test")
        assert page.blocked and not page.usable()

    async def test_a_transport_error_returns_empty_rather_than_raising(
        self, monkeypatch
    ):
        async def _boom(self, url, headers=None):
            raise httpx.ConnectError("no route")

        monkeypatch.setattr(httpx.AsyncClient, "get", _boom)
        page = await reader(provider="basic").read("northwind.test")
        assert not page and page.error

    async def test_an_empty_url_is_handled(self):
        assert not await reader().read("")

    async def test_it_escalates_when_the_first_tier_comes_back_short(
        self, monkeypatch
    ):
        calls = []

        async def short_crawl4ai(self, url):
            calls.append("crawl4ai")
            return PageContent(url=url, text="tiny", via="crawl4ai")

        monkeypatch.setattr(
            "openvz_leads.integrations.crawler.crawl4ai_available", lambda: True
        )
        monkeypatch.setattr(PageReader, "_read_crawl4ai", short_crawl4ai)
        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get())

        page = await reader().read("northwind.test")
        assert calls == ["crawl4ai"]
        assert page.via == "basic"
        assert page.attempts == ["crawl4ai:miss", "basic:ok"]

    async def test_a_tier_that_raises_is_disabled_not_fatal(self, monkeypatch):
        async def explode(self, url):
            raise RuntimeError("crawl4ai fell over")

        monkeypatch.setattr(
            "openvz_leads.integrations.crawler.crawl4ai_available", lambda: True
        )
        monkeypatch.setattr(PageReader, "_read_crawl4ai", explode)
        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get())

        r = reader()
        page = await r.read("northwind.test")
        assert page.via == "basic"
        assert "crawl4ai" in r._disabled
        assert r._tier_order() == ["basic"]

    async def test_a_short_page_beats_nothing_at_all(self, monkeypatch):
        """Every tier missed, but one of them did return words. Hand them over
        rather than pretending the site was unreachable."""
        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get(text="<p>Tiny co</p>"))
        page = await reader(provider="basic").read("northwind.test")
        assert not page.usable()
        assert "Tiny co" in page.text
        assert len(page.text) < MIN_USEFUL_CHARS


class TestMarkdownCoercion:
    def test_a_plain_string(self):
        assert PageReader._coerce_markdown("# hi") == "# hi"

    def test_an_object_with_raw_markdown(self):
        class Result:
            raw_markdown = "# hi"

        assert PageReader._coerce_markdown(Result()) == "# hi"

    def test_something_unrecognised_does_not_raise(self):
        assert PageReader._coerce_markdown(None) == ""
