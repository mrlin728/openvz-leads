"""Profiler — turns a found prospect into a decision-ready account brief.

This is the "analyse them" stage of the pipeline, between Scout (find) and
Writer (draft). Scout scores prospects cheaply and in bulk; the Profiler
spends one Claude call on a single account to produce something a human can
actually act on: what the company does, why they fit (or don't), what would
make them buy, who signs, and how to open the conversation.

Everything it writes is a hypothesis over collected evidence, and it is
prompted to say so — see prompts/profiler.md.
"""

import logging

from openvz_leads.brain import Brain
from openvz_leads.config import LeadsConfig
from openvz_leads.integrations.crawler import PageReader
from openvz_leads.models.profile import AccountProfile
from openvz_leads.state import StateManager

logger = logging.getLogger("openvz_leads.profiler")


class Profiler:
    def __init__(self, brain: Brain, state: StateManager, config: LeadsConfig):
        self.brain = brain
        self.state = state
        self.config = config
        # One reader per Profiler: it remembers which crawl tiers failed, so
        # a missing optional package is discovered once rather than per page.
        self.reader = PageReader(config.crawl)

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

        page = None
        url = ""
        if self.config.profiling.fetch_website and company:
            url = company.website or (
                f"https://{company.domain}" if company.domain else ""
            )
            if url:
                page = await self._fetch_site(url)
        if page:
            limit = self.config.crawl.max_chars
            parts.append(
                f"### Their website ({url}), read via {page.via}\n"
                f"{page.best_text()[:limit]}"
            )
        elif page is not None and page.blocked:
            # Worth distinguishing: a bot wall is not evidence of anything
            # about the company, and an analyst reading "unreachable" might
            # otherwise infer a dead business from a live one.
            parts.append(
                "### Their website\nNot available — the site returned a bot "
                "challenge rather than a page. This says nothing about the "
                "company. Treat company claims below as unverified."
            )
        else:
            parts.append(
                "### Their website\nNot available — no page could be fetched. "
                "Treat company claims below as unverified."
            )

        return "\n\n".join(parts)

    async def _fetch_site(self, url: str):
        """Best-effort homepage read. Never raises; may come back empty.

        Which tier answered is kept and printed into the evidence, because
        "read as Markdown from a rendered page" and "de-tagged HTML" are not
        equally trustworthy and the analysis should be able to tell.
        """
        page = await self.reader.read(url)
        if not page:
            logger.debug(
                f"Profiler: no tier could read {url[:80]} ({page.attempts})."
            )
        return page

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
            keywords=", ".join(getattr(icp, "keywords", []) or []) or "(none given)",
            exclusions=", ".join(getattr(icp, "exclusions", []) or []) or "(none given)",
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
                f"titles {', '.join(icp.titles)}. "
                f"Must also match: {', '.join(getattr(icp, 'keywords', []) or []) or 'nothing extra'}. "
                f"Disqualifiers: {', '.join(getattr(icp, 'exclusions', []) or []) or 'none'}. "
                f"Never invent evidence. "
                "Return JSON with keys: fit_score, fit_reasons, risks, "
                "company_snapshot, buying_signals, pain_hypotheses, "
                "decision_chain, opening_angles, avoid, confidence, "
                f"evidence_gaps. Write free text in "
                f"{self.config.profiling.output_language}."
            )

        if getattr(self, "skills", ""):
            prompt += "\n\n" + self.skills

        return f"{prompt}\n\n---\n## EVIDENCE\n\n{evidence}"
