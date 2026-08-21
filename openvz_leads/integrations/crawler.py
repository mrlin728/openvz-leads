"""Page reading — one door, three tiers behind it.

Everything that needs to read a web page (the Profiler gathering evidence,
the Scout characterising a company, the Trainer learning a product) calls
``PageReader.read``. What actually fetches the page depends on what is
installed and what the page turns out to need:

    basic        httpx + BeautifulSoup. Always available, no extra install,
                 and enough for most company sites, which are still HTML.
    crawl4ai     Renders JavaScript and returns Markdown. Markdown is the
                 point: headings and lists survive, so the model reads a
                 document instead of a wall of de-tagged text.
    browser_use  An agent driving a real browser. The only tier that can get
                 past a consent wall or click into a page that does not exist
                 as a URL. Slow and needs its own model key, so it is off
                 until you turn it on and only ever runs after the others
                 came back empty.

Both upper tiers are optional dependencies. With neither installed this
module behaves exactly like the inline fetching it replaced — that is the
contract, and the reason installs that never touch `crawl` keep working.

    pip install "openvz-leads[crawl]"    # crawl4ai
    pip install "openvz-leads[browser]"  # browser-use
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import random
import re
from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup

from openvz_leads.config import CrawlConfig

logger = logging.getLogger("openvz_leads.crawler")

HTTP_TIMEOUT = 15.0
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
_WHITESPACE = re.compile(r"\s+")

# Text that means "you got a challenge page, not the site". Matching one of
# these is what escalates a URL to the next tier rather than accepting the
# 200 at face value.
_BLOCK_MARKERS = (
    "enable javascript and cookies to continue",
    "checking your browser",
    "verify you are human",
    "just a moment...",
    "attention required! | cloudflare",
    "access denied",
    "request unsuccessful",
    "captcha",
)

# Below this a "successful" fetch is a cookie banner or an error page, not a
# company website — treat it as a miss so the next tier gets a turn.
MIN_USEFUL_CHARS = 200

# Tiers we have already told the user are not installed. See _warn_missing.
_MISSING_TIER_WARNED: set[str] = set()


@dataclass
class PageContent:
    """What came back, and which tier produced it.

    `via` is carried all the way into the account brief's evidence section on
    purpose: an analyst reading a brief should be able to tell whether a claim
    came off a rendered page or a raw HTML scrape.
    """

    url: str = ""
    title: str = ""
    text: str = ""
    markdown: str = ""
    via: str = ""
    blocked: bool = False
    error: str = ""
    attempts: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.text.strip())

    def usable(self) -> bool:
        return len(self.text.strip()) >= MIN_USEFUL_CHARS and not self.blocked

    def best_text(self) -> str:
        """Markdown when we have it, plain text otherwise."""
        return self.markdown.strip() or self.text.strip()


def looks_blocked(text: str) -> bool:
    if not text:
        return False
    low = text[:4000].lower()
    return any(marker in low for marker in _BLOCK_MARKERS)


def crawl4ai_available() -> bool:
    return importlib.util.find_spec("crawl4ai") is not None


def browser_use_available() -> bool:
    return importlib.util.find_spec("browser_use") is not None


def describe_tiers(config: CrawlConfig) -> str:
    """One line for the setup screen: what will actually be used."""
    tiers = []
    if crawl4ai_available():
        tiers.append("crawl4ai")
    tiers.append("basic")
    if config.browser_fallback and browser_use_available():
        tiers.append("browser_use")
    if config.provider != "auto":
        return f"{config.provider} (available: {', '.join(tiers)})"
    return " → ".join(tiers)


class PageReader:
    """Reads pages using the best tier available, cheapest first."""

    def __init__(self, config: CrawlConfig | None = None, env=None):
        self.config = config or CrawlConfig()
        self.env = env
        # Once an optional package fails to import or blows up on its first
        # use, stop paying the cost of finding out again every page.
        self._disabled: set[str] = set()

    # ── Public API ──

    async def read(self, url: str, *, goal: str = "") -> PageContent:
        """Fetch one page. Never raises; returns an empty PageContent on failure.

        `goal` is only meaningful for the browser_use tier, which is given an
        instruction rather than a URL ("find the team page and list the
        names"). The other tiers ignore it.
        """
        url = self._normalize(url)
        if not url:
            return PageContent(error="no url")

        result = PageContent(url=url)
        for tier in self._tier_order():
            page = await self._run_tier(tier, url, goal)
            result.attempts.append(f"{tier}:{'ok' if page.usable() else 'miss'}")
            if page.usable():
                page.attempts = result.attempts
                await self._be_polite()
                return page
            # A challenge page is a different answer from an unreachable one,
            # and the caller acts on the difference — "they blocked us" is
            # worth recording, "the site was down" is worth retrying. The
            # flag would otherwise be dropped with the empty body it came on.
            blocked_so_far = result.blocked or page.blocked

            # Keep the best near-miss so a short-but-real page still beats
            # returning nothing at all.
            if page and len(page.text) > len(result.text):
                page.attempts = result.attempts
                result = page
            elif page.error and not result.error:
                result.error = page.error
            result.blocked = blocked_so_far

        await self._be_polite()
        if not result:
            logger.debug(f"No tier could read {url[:80]} ({result.attempts}).")
        return result

    async def read_text(self, url: str, max_chars: int | None = None) -> str:
        """Convenience wrapper for callers that only want the words."""
        page = await self.read(url)
        limit = max_chars if max_chars is not None else self.config.max_chars
        return page.best_text()[:limit]

    # ── Tier selection ──

    def _tier_order(self) -> list[str]:
        provider = self.config.provider

        if provider == "basic":
            return ["basic"]
        if provider == "crawl4ai":
            # An explicit choice is a preference, not a suicide pact: if the
            # package is missing, say so once and still read the page.
            if self._tier_ready("crawl4ai"):
                return ["crawl4ai", "basic"]
            self._warn_missing("crawl4ai")
            return ["basic"]
        if provider == "browser_use":
            if self._tier_ready("browser_use"):
                return ["browser_use", "basic"]
            self._warn_missing("browser_use")
            return ["basic"]

        # auto: best available first, real browser only as a last resort.
        order = []
        if self._tier_ready("crawl4ai"):
            order.append("crawl4ai")
        order.append("basic")
        if self.config.browser_fallback and self._tier_ready("browser_use"):
            order.append("browser_use")
        return order

    def _tier_ready(self, tier: str) -> bool:
        if tier in self._disabled:
            return False
        return {
            "crawl4ai": crawl4ai_available,
            "browser_use": browser_use_available,
        }[tier]()

    def _warn_missing(self, tier: str):
        # Process-wide, deliberately: "install crawl4ai" is the same sentence
        # for every account, and repeating it per page buries the log.
        if tier in _MISSING_TIER_WARNED:
            return
        _MISSING_TIER_WARNED.add(tier)
        extra = {"crawl4ai": "crawl", "browser_use": "browser"}[tier]
        logger.warning(
            f"crawl.provider is '{tier}' but the package is not installed. "
            f"Falling back to basic fetching. Install it with: "
            f'pip install "openvz-leads[{extra}]"'
        )

    async def _run_tier(self, tier: str, url: str, goal: str) -> PageContent:
        try:
            if tier == "crawl4ai":
                return await self._read_crawl4ai(url)
            if tier == "browser_use":
                return await self._read_browser_use(url, goal)
            return await self._read_basic(url)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # One tier misbehaving must never end the read — that is the whole
            # reason there is more than one.
            logger.debug(f"Tier {tier} raised on {url[:80]}: {e}")
            if tier != "basic":
                self._disabled.add(tier)
                logger.warning(
                    f"Disabling the {tier} tier for this run after an error: {e}"
                )
            return PageContent(url=url, via=tier, error=str(e))

    async def _be_polite(self):
        delay = self.config.delay_seconds
        if delay > 0:
            await asyncio.sleep(random.uniform(delay * 0.5, delay * 1.5))

    @staticmethod
    def _normalize(url: str) -> str:
        url = (url or "").strip()
        if not url:
            return ""
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        return url

    # ── Tier: basic ──

    async def _read_basic(self, url: str) -> PageContent:
        """httpx + BeautifulSoup. The floor, and always present."""
        try:
            async with httpx.AsyncClient(
                timeout=min(self.config.timeout_seconds, HTTP_TIMEOUT),
                follow_redirects=True,
            ) as client:
                resp = await client.get(url, headers={"User-Agent": USER_AGENT})
        except Exception as e:
            logger.debug(f"basic: could not fetch {url[:80]}: {e}")
            return PageContent(url=url, via="basic", error=str(e))

        if resp.status_code != 200 or not resp.text:
            return PageContent(
                url=url, via="basic", error=f"HTTP {resp.status_code}"
            )
        if looks_blocked(resp.text):
            return PageContent(url=url, via="basic", blocked=True)

        try:
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            return PageContent(url=url, via="basic", error=f"parse failed: {e}")

        title = soup.title.get_text(strip=True) if soup.title else ""
        for tag in soup(["script", "style", "noscript", "nav", "footer", "svg"]):
            tag.decompose()
        text = _WHITESPACE.sub(" ", soup.get_text(" ", strip=True))
        return PageContent(
            url=url,
            title=title,
            text=text[: self.config.max_chars],
            via="basic",
        )

    # ── Tier: crawl4ai ──

    async def _read_crawl4ai(self, url: str) -> PageContent:
        """Rendered page, returned as Markdown.

        crawl4ai has changed the shape of both its config object and its
        result's `markdown` attribute across releases, so everything here is
        read defensively — a version that returns something we do not
        recognise degrades to the next tier rather than raising.
        """
        from crawl4ai import AsyncWebCrawler  # imported late: optional dep

        run_config = None
        try:
            from crawl4ai import CrawlerRunConfig

            run_config = CrawlerRunConfig(
                page_timeout=int(self.config.timeout_seconds * 1000)
            )
        except Exception:
            # Older releases take these as kwargs to arun(); no config object.
            pass

        async with AsyncWebCrawler(verbose=False) as crawler:
            if run_config is not None:
                result = await crawler.arun(url=url, config=run_config)
            else:
                result = await crawler.arun(url=url)

        if result is None or not getattr(result, "success", True):
            error = getattr(result, "error_message", "") or "crawl failed"
            return PageContent(url=url, via="crawl4ai", error=str(error)[:200])

        markdown = self._coerce_markdown(getattr(result, "markdown", ""))
        text = markdown or self._strip_html(getattr(result, "cleaned_html", "") or "")
        if looks_blocked(text):
            return PageContent(url=url, via="crawl4ai", blocked=True)

        return PageContent(
            url=url,
            title=str(getattr(result, "metadata", {}).get("title", "") or ""),
            text=text[: self.config.max_chars],
            markdown=markdown[: self.config.max_chars],
            via="crawl4ai",
        )

    @staticmethod
    def _coerce_markdown(value) -> str:
        """`result.markdown` is a str in some versions and an object in others."""
        if isinstance(value, str):
            return value.strip()
        for attr in ("raw_markdown", "fit_markdown", "markdown"):
            got = getattr(value, attr, None)
            if isinstance(got, str) and got.strip():
                return got.strip()
        return str(value).strip() if value else ""

    @staticmethod
    def _strip_html(html: str) -> str:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return ""
        return _WHITESPACE.sub(" ", soup.get_text(" ", strip=True))

    # ── Tier: browser_use ──

    async def _read_browser_use(self, url: str, goal: str) -> PageContent:
        """An agent driving a real browser, for pages the others cannot open.

        This tier needs a model key of its own: browser-use drives an API
        model, and the Claude Code CLI is not one. Without a key it declines
        rather than failing halfway through opening a browser.
        """
        api_key, base_url, model_name = self._browser_model()
        if not api_key:
            logger.warning(
                "browser_use tier needs an API key (OPENAI_API_KEY, "
                "DEEPSEEK_API_KEY or MODEL_API_KEY) — it drives an API model, "
                "not the Claude CLI. Skipping this tier."
            )
            self._disabled.add("browser_use")
            return PageContent(url=url, via="browser_use", error="no api key")

        task = goal.strip() or (
            f"Open {url} and report what this company does, who it serves, "
            "and anything that reads as recent news, hiring or expansion. "
            "Quote the page; do not infer beyond it."
        )

        agent = self._build_browser_agent(task, api_key, base_url, model_name)
        if agent is None:
            self._disabled.add("browser_use")
            return PageContent(url=url, via="browser_use", error="unsupported browser-use version")

        history = await asyncio.wait_for(
            agent.run(), timeout=max(self.config.timeout_seconds, 60.0)
        )
        text = self._browser_result_text(history)
        if not text:
            return PageContent(url=url, via="browser_use", error="empty result")
        return PageContent(
            url=url,
            text=text[: self.config.max_chars],
            markdown=text[: self.config.max_chars],
            via="browser_use",
        )

    def _browser_model(self) -> tuple[str, str, str]:
        """(api_key, base_url, model) for the browser agent's own LLM."""
        env = self.env
        if env is None:
            from openvz_leads.config import load_env

            env = self.env = load_env()
        if getattr(env, "openai_api_key", ""):
            return env.openai_api_key, "", "gpt-4.1-mini"
        if getattr(env, "deepseek_api_key", ""):
            return env.deepseek_api_key, "https://api.deepseek.com/v1", "deepseek-chat"
        return getattr(env, "model_api_key", ""), "", "gpt-4.1-mini"

    @staticmethod
    def _build_browser_agent(task: str, api_key: str, base_url: str, model: str):
        """Construct a browser-use Agent across the shapes it has shipped.

        browser-use moved its chat model from langchain to its own package,
        and the Agent's kwargs moved with it. Rather than pin a version and
        break on the next one, try the known constructions and give up
        quietly — this is an optional last-resort tier, not the product.
        """
        from browser_use import Agent  # imported late: optional dep

        kwargs = {"api_key": api_key, "model": model}
        if base_url:
            kwargs["base_url"] = base_url

        llm = None
        try:  # browser-use >= 0.2, its own chat model
            from browser_use import ChatOpenAI  # type: ignore

            llm = ChatOpenAI(**kwargs)
        except Exception:
            try:  # earlier releases expected a langchain model
                from langchain_openai import ChatOpenAI as LangChainChatOpenAI  # type: ignore

                llm = LangChainChatOpenAI(
                    model=model,
                    api_key=api_key,
                    base_url=base_url or None,
                )
            except Exception as e:
                logger.warning(
                    f"Could not build a model for browser-use ({e}). "
                    "Install a supported browser-use, or leave "
                    "crawl.browser_fallback off."
                )
                return None

        try:
            return Agent(task=task, llm=llm)
        except Exception as e:
            logger.warning(f"Could not construct a browser-use Agent: {e}")
            return None

    @staticmethod
    def _browser_result_text(history) -> str:
        """Pull the final answer out of whatever browser-use returned."""
        for attr in ("final_result", "final_answer"):
            getter = getattr(history, attr, None)
            if callable(getter):
                try:
                    value = getter()
                except Exception:
                    continue
                if isinstance(value, str) and value.strip():
                    return value.strip()
            elif isinstance(getter, str) and getter.strip():
                return getter.strip()
        if isinstance(history, str):
            return history.strip()
        return ""
