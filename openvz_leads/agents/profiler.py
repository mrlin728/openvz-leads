"""Profiler — turns a found prospect into a decision-ready account brief.

This is the "analyse them" stage of the pipeline, between Scout (find) and
Writer (draft). Scout scores prospects cheaply and in bulk; the Profiler
spends one Claude call on a single account to produce something a human can
actually act on: what the company does, why they fit (or don't), what would
make them buy, who signs, and how to open the conversation.

Everything it writes is a hypothesis over collected evidence, and it is
prompted to say so — see prompts/profiler.md.
"""

import asyncio
import logging
import random
import re

import httpx
from bs4 import BeautifulSoup

from openvz_leads.brain import Brain
from openvz_leads.config import LeadsConfig
from openvz_leads.models.profile import AccountProfile
from openvz_leads.state import StateManager

logger = logging.getLogger("openvz_leads.profiler")

HTTP_TIMEOUT = 15.0
# Enough of a homepage to characterise a company; past this it's navigation
# and boilerplate, which only dilutes the prompt.
MAX_SITE_CHARS = 6000
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
_WHITESPACE = re.compile(r"\s+")


class Profiler:
    def __init__(self, brain: Brain, state: StateManager, config: LeadsConfig):
        self.brain = brain
        self.state = state
        self.config = config

    async def run(self):
        """Analyse the highest-value prospects that haven't been profiled."""
        if not self.config.profiling.enabled:
            logger.info("Profiler: Disabled in config. Skipping.")
            return

        settings = self.config.profiling
        prospects = await self.state.get_prospects_needing_profile(
            min_score=settings.min_score, limit=settings.max_per_cycle
        )
        if not prospects:
            logger.info("Profiler: Nothing new to analyse.")
            return

        logger.info(f"Profiler: Analysing {len(prospects)} account(s)...")
        self.skills = self.brain.load_skills_for_agent("profiler")

        profiled = 0
        for prospect in prospects:
            try:
                if await self._profile_one(prospect):
                    profiled += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    f"Profiler: Failed to analyse {prospect.full_name()} "
                    f"at {prospect.company}: {e}"
                )

        if profiled:
            await self.state.log_action(
                action_type="profile",
                agent="profiler",
                details={"analysed": profiled, "attempted": len(prospects)},
            )
        logger.info(f"Profiler: Wrote {profiled}/{len(prospects)} account brief(s).")

    async def _profile_one(self, prospect) -> bool:
        company = None
        if prospect.company_id:
            company = await self.state.get_company(prospect.company_id)

        evidence = await self._gather_evidence(prospect, company)
        prompt = self._build_prompt(prospect, company, evidence)

        raw = await self.brain.think_json(prompt, session_id="leads-profiler")
        profile = AccountProfile.from_raw(raw)
        if profile is None:
            logger.warning(
                f"Profiler: No usable analysis for {prospect.company or prospect.full_name()}."
            )
            return False

        await self.state.save_prospect_profile(prospect.id, profile.model_dump())
        logger.info(
            f"Profiler: {prospect.company or prospect.full_name()} — "
            f"fit {profile.fit_score}/10, confidence {profile.confidence}."
        )
        return True

    # ── Evidence gathering ──

    async def _gather_evidence(self, prospect, company) -> str:
        """Assemble everything already known, plus the company's own site."""
        parts = []

        known = [
            f"Contact: {prospect.full_name() or '(name unknown)'}",
            f"Title: {prospect.title or '(unknown)'}",
            f"Seniority: {prospect.seniority or '(unknown)'}",
            f"Department: {prospect.department or '(unknown)'}",
            f"Company: {prospect.company or '(unknown)'}",
            f"Industry: {prospect.industry or '(unknown)'}",
            f"Company size: {prospect.company_size or '(unknown)'}",
            f"Scout score: {prospect.score}/100",
            f"Found via: {prospect.source or '(unknown)'} {prospect.source_url}".strip(),
        ]
        if prospect.personalization_notes:
            known.append(f"Scout's note: {prospect.personalization_notes}")
        parts.append("### What we already know\n" + "\n".join(known))

        if company:
            record = [
                f"Domain: {company.domain or '(unknown)'}",
                f"Website: {company.website or '(unknown)'}",
                f"Location: {company.location or '(unknown)'}",
            ]
            if company.description:
                record.append(f"Description on record: {company.description}")
            if company.notes:
                record.append(f"Notes on record: {company.notes}")
            parts.append("### Company record\n" + "\n".join(record))

        site_text = ""
        url = ""
        if self.config.profiling.fetch_website and company:
            url = company.website or (
                f"https://{company.domain}" if company.domain else ""
            )
            if url:
                site_text = await self._fetch_site_text(url)
        if site_text:
            parts.append(
                f"### Their website ({url}), text extract\n{site_text}"
            )
        else:
            parts.append(
                "### Their website\nNot available — no page could be fetched. "
                "Treat company claims below as unverified."
            )

        return "\n\n".join(parts)

    async def _fetch_site_text(self, url: str) -> str:
        """Best-effort homepage text. Never raises; returns '' on any failure."""
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        try:
            async with httpx.AsyncClient(
                timeout=HTTP_TIMEOUT, follow_redirects=True
            ) as client:
                resp = await client.get(url, headers={"User-Agent": USER_AGENT})
        except Exception as e:
            logger.debug(f"Profiler: Could not fetch {url[:80]}: {e}")
            return ""

        if resp.status_code != 200 or not resp.text:
            logger.debug(f"Profiler: {url[:80]} returned {resp.status_code}.")
            return ""

        try:
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception:
            return ""
        for tag in soup(["script", "style", "noscript", "nav", "footer", "svg"]):
            tag.decompose()
        text = _WHITESPACE.sub(" ", soup.get_text(" ", strip=True))
        # Politeness: this is one page per account, but don't hammer.
        await asyncio.sleep(random.uniform(0.5, 1.5))
        return text[:MAX_SITE_CHARS]

    # ── Prompt ──

    def _build_prompt(self, prospect, company, evidence: str) -> str:
        icp = self.config.icp
        product = self.config.product

        prompt = self.brain.load_prompt(
            "profiler",
            product_name=product.name,
            product_description=product.description,
            product_benefits="\n".join(f"- {b}" for b in product.key_benefits),
            product_pricing=product.pricing,
            industries=", ".join(icp.industries),
            company_size=icp.company_size,
            geography=", ".join(icp.geography),
            titles=", ".join(icp.titles),
            output_language=self.config.profiling.output_language,
        )

        if not prompt:
            # Prompt file missing — degrade to a minimal but functional brief
            # rather than skipping analysis entirely.
            logger.warning("Profiler: prompts/profiler.md missing; using fallback prompt.")
            prompt = (
                f"Analyse this account as a sales prospect for {product.name} "
                f"({product.description}). Our ICP: {', '.join(icp.industries)}, "
                f"{icp.company_size}, {', '.join(icp.geography)}, "
                f"titles {', '.join(icp.titles)}. Never invent evidence. "
                "Return JSON with keys: fit_score, fit_reasons, risks, "
                "company_snapshot, buying_signals, pain_hypotheses, "
                "decision_chain, opening_angles, avoid, confidence, "
                f"evidence_gaps. Write free text in "
                f"{self.config.profiling.output_language}."
            )

        if getattr(self, "skills", ""):
            prompt += "\n\n" + self.skills

        return f"{prompt}\n\n---\n## EVIDENCE\n\n{evidence}"
