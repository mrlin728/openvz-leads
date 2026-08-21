"""Writer — crafts personalized email sequences."""

import json
import logging

from openvz_leads import pipeline
from openvz_leads.brain import Brain
from openvz_leads.config import LeadsConfig
from openvz_leads.models.campaign import Campaign, EmailStep
from openvz_leads.models.profile import AccountProfile
from openvz_leads.state import StateManager

logger = logging.getLogger("openvz_leads.writer")


class Writer:
    def __init__(self, brain: Brain, state: StateManager, config: LeadsConfig):
        self.brain = brain
        self.state = state
        self.config = config

    async def run(self):
        """Create email campaigns for prospects that need outreach."""
        logger.info("Writer: Crafting email campaigns...")

        # Load foundational skills for this agent
        self.skills = self.brain.load_skills_for_agent("writer")

        # Get prospects that haven't been contacted yet
        new_prospects = await self.state.get_prospects_by_status("new")
        if not new_prospects:
            logger.info("Writer: No new prospects to write for.")
            return

        # Filter to only those with emails
        prospects_with_email = [p for p in new_prospects if p.email]
        if not prospects_with_email:
            logger.info("Writer: No prospects with verified emails.")
            return

        # Batch prospects into campaign groups (by industry/title for relevance)
        batches = self._group_prospects(prospects_with_email)

        for batch_name, prospects in batches.items():
            if not prospects:
                continue

            logger.info(
                f"Writer: Creating campaign '{batch_name}' for "
                f"{len(prospects)} prospects."
            )

            # Generate the email sequence
            sequence = await self._write_sequence(prospects)
            if not sequence:
                logger.warning(f"Writer: Failed to generate sequence for {batch_name}")
                continue

            # Queue for a human unless review has been explicitly switched off.
            status = (
                "pending_review"
                if self.config.review.require_approval
                else "approved"
            )
            campaign = Campaign(
                id="",
                name=batch_name,
                channel="email",
                sequence=sequence,
                prospect_ids=[p.id for p in prospects],
                status=status,
            )
            campaign_id = await self.state.add_campaign(campaign)

            # Mark prospects so they aren't picked up again
            for p in prospects:
                await pipeline.advance(
                    self.state, p.id, "queued",
                    reason="Outreach drafted", actor="writer",
                )

            await self.state.log_action(
                action_type="write_campaign",
                agent="writer",
                details={
                    "campaign_id": campaign_id,
                    "campaign_name": batch_name,
                    "prospect_count": len(prospects),
                    "steps": len(sequence),
                    "status": status,
                },
            )
            if status == "pending_review":
                logger.info(
                    f"Writer: Campaign '{batch_name}' — {len(sequence)} emails for "
                    f"{len(prospects)} prospects — is waiting for your review. "
                    "Approve it in the dashboard or with "
                    f"'openvz-leads review approve {campaign_id}'."
                )
            else:
                logger.info(
                    f"Writer: Campaign '{batch_name}' created with "
                    f"{len(sequence)} emails for {len(prospects)} prospects "
                    "(review is off — marked approved)."
                )

    async def _write_sequence(self, prospects: list) -> list[EmailStep]:
        """Ask the brain to write a 3-email sequence."""
        prospect_summary = self._describe_prospects(prospects[:5])

        prompt = self.brain.load_prompt(
            "writer",
            product_name=self.config.product.name,
            product_description=self.config.product.description,
            product_benefits="\n".join(f"- {b}" for b in self.config.product.key_benefits),
            product_pricing=self.config.product.pricing,
            persona_name=self.config.persona.name,
            persona_company=self.config.persona.company,
            persona_role=self.config.persona.role,
            persona_tone=self.config.persona.tone,
        )

        if not prompt:
            prompt = f"""You are {self.config.persona.name}, {self.config.persona.role} at {self.config.persona.company}.
Your tone is: {self.config.persona.tone}

Product: {self.config.product.name}
Description: {self.config.product.description}
Key benefits: {', '.join(self.config.product.key_benefits)}
Pricing: {self.config.product.pricing}"""

        # Inject email framework skills
        if self.skills:
            prompt += "\n\n" + self.skills

        prompt += f"""

Write a 3-email cold outreach sequence for prospects like these:
{prospect_summary}

Requirements:
- Email 1: Personalized cold observation + one question. Under 75 words. No pitch.
- Email 2: Follow-up 3 days later. Different angle, one concrete proof point. Under 75 words.
- Email 3: Break-up email 4 days after that. Under 40 words. Gracious, not guilt-tripping.
- Use {{{{first_name}}}}, {{{{company}}}}, {{{{title}}}} as merge variables — every email
  must use at least one, and email 1 must reference something specific to
  these prospects' industry or role (use the notes above).
- Never be pushy or salesy. Be consultative and value-driven.
- Subject lines: lowercase, 2-5 words.
- Where an account brief is given, build email 1 on one of its opening angles
  and stay off everything in its "Do NOT say" line.
- The brief's pains and signals are hypotheses, not verified facts. Write them
  as something you suspect and want to check, never as something you know —
  asserting a detail that turns out to be wrong kills the thread.
- Follow every rule in the STRICT EMAIL RULES above. No exceptions.

Return ONLY a JSON array (no markdown fences, no commentary):
[
  {{"step": 1, "subject": "...", "body": "...", "delay_days": 0}},
  {{"step": 2, "subject": "...", "body": "...", "delay_days": 3}},
  {{"step": 3, "subject": "...", "body": "...", "delay_days": 4}}
]"""

        result = await self.brain.think_json(prompt, session_id="leads-writer")
        return self._parse_sequence(result)

    @staticmethod
    def _describe_prospects(prospects: list) -> str:
        """Describe a sample of the batch, folding in the Profiler's analysis.

        The account brief is what makes email 1 specific rather than generic,
        so it goes in wherever it exists — including the Profiler's `avoid`
        list, which is the part that stops a rep torching the account.
        """
        blocks = []
        for p in prospects:
            lines = [f"- {p.full_name()}, {p.title} at {p.company}"]
            if p.personalization_notes:
                lines.append(f"  Notes: {p.personalization_notes}")

            profile = p.profile()
            if profile:
                try:
                    brief = AccountProfile(**profile).brief()
                except Exception:
                    brief = ""
                if brief:
                    lines.append(
                        "  Account brief:\n"
                        + "\n".join(f"    {line}" for line in brief.splitlines())
                    )
            blocks.append("\n".join(lines))
        return "\n".join(blocks)

    def _parse_sequence(self, result) -> list[EmailStep]:
        """Robustly coerce LLM output into a validated EmailStep list.

        Never raises. Tolerates a wrapper dict ({"emails": [...]}), missing
        or wrong-typed fields, and extra keys. Drops invalid steps rather
        than failing the whole sequence.
        """
        if isinstance(result, dict):
            # Model wrapped the array in an object — unwrap common keys.
            for key in ("emails", "sequence", "steps", "campaign"):
                if isinstance(result.get(key), list):
                    result = result[key]
                    break
        if not isinstance(result, list) or not result:
            logger.error(f"Writer: Brain did not return an email array (got {type(result).__name__}).")
            return []

        steps: list[EmailStep] = []
        for i, raw in enumerate(result[:5]):  # never accept absurdly long sequences
            if not isinstance(raw, dict):
                logger.warning(f"Writer: Skipping non-dict step at index {i}.")
                continue
            subject = str(raw.get("subject") or "").strip()
            body = str(raw.get("body") or "").strip()
            if not subject or not body:
                logger.warning(f"Writer: Skipping step {i + 1} with empty subject/body.")
                continue
            try:
                delay = max(0, int(raw.get("delay_days", 3 if steps else 0)))
            except (TypeError, ValueError):
                delay = 3 if steps else 0
            try:
                steps.append(
                    EmailStep(
                        step=len(steps) + 1,
                        subject=subject,
                        body=body,
                        delay_days=delay,
                    )
                )
            except Exception as e:
                logger.warning(f"Writer: Skipping invalid step {i + 1}: {e}")

        if steps and steps[0].delay_days != 0:
            steps[0].delay_days = 0  # first email always sends immediately

        if len(steps) < 3:
            logger.warning(f"Writer: Expected 3 emails, got {len(steps)}.")
        return steps

    def _group_prospects(self, prospects: list) -> dict[str, list]:
        """Group prospects into campaign batches by industry/title combo."""
        batches: dict[str, list] = {}

        for prospect in prospects:
            # Group by industry + rough title category
            industry = prospect.industry or "general"
            key = f"{industry}-outreach"

            if key not in batches:
                batches[key] = []
            batches[key].append(prospect)

        # Cap each batch at 50 prospects (Instantly best practice)
        capped = {}
        for key, group in batches.items():
            if len(group) > 50:
                capped[key] = group[:50]
            else:
                capped[key] = group

        return capped
