"""Handler — monitors replies and manages conversations."""

import logging
from datetime import datetime

from openvz_leads import pipeline
from openvz_leads.brain import Brain
from openvz_leads.config import LeadsConfig, EnvConfig
from openvz_leads.integrations.crm import CrmSync
from openvz_leads.integrations.instantly import InstantlyClient
from openvz_leads.models.conversation import Conversation, Message
from openvz_leads.state import StateManager

logger = logging.getLogger("openvz_leads.handler")

INTENT_LABELS = {
    "interested",
    "objection",
    "not_interested",
    "ooo",
    "wrong_person",
    "question",
    "unsubscribe",
    "escalate",
}

# Intents that get an automated reply. Everything else is closed,
# deferred, or escalated to a human.
AUTO_REPLY_INTENTS = {"interested", "objection", "question", "wrong_person"}

# Hard opt-out phrases. If any of these appear, we ALWAYS honor the
# opt-out regardless of what the LLM classifier says.
OPT_OUT_PATTERNS = (
    "unsubscribe",
    "opt out",
    "opt-out",
    "remove me",
    "take me off",
    "stop emailing",
    "stop contacting",
    "stop sending",
    "do not contact",
    "do not email",
    "don't contact",
    "don't email",
    "never contact",
    "delete my info",
    "delete my data",
)

# Phrases that mean a human must take over. Never auto-reply to these.
ESCALATION_PATTERNS = (
    "lawyer",
    "attorney",
    "legal action",
    "lawsuit",
    "cease and desist",
    "harassment",
    "harassing",
    "report you",
    "reporting you",
    "spam complaint",
    "ftc",
    "gdpr",
    "can-spam",
    "blacklist",
)


class Handler:
    def __init__(
        self,
        brain: Brain,
        state: StateManager,
        config: LeadsConfig,
        env: EnvConfig,
    ):
        self.brain = brain
        self.state = state
        self.config = config
        self.env = env
        self.instantly = InstantlyClient(env.instantly_api_key)
        # Replies are where the interesting stages happen — replied, lost,
        # opted_out — so this is the agent a CRM most wants to hear from.
        self.crm = CrmSync(config.crm, env)
        self.skills = ""

    async def run(self):
        """Check for new replies and handle them."""
        logger.info("Handler: Checking for replies...")

        # Load foundational skills for this agent
        self.skills = self.brain.load_skills_for_agent("handler")

        if not self.config.channels.email.enabled:
            return

        if self.config.channels.email.provider == "gmail":
            await self._run_gmail()
            return

        # 1. Fetch active campaigns
        active_campaigns = await self.state.get_campaigns_by_status("active")
        if not active_campaigns:
            logger.info("Handler: No active campaigns to monitor.")
            return

        total_handled = 0

        for campaign in active_campaigns:
            if not campaign.instantly_campaign_id:
                continue

            # 2. Get replies from Instantly
            try:
                replies = await self.instantly.get_replies(campaign.instantly_campaign_id)
            except Exception as e:
                logger.error(f"Handler: Failed to fetch replies for {campaign.name}: {e}")
                continue
            if not replies:
                continue

            for reply in replies:
                if not isinstance(reply, dict):
                    continue
                try:
                    handled = await self._handle_reply(reply, campaign)
                    if handled:
                        total_handled += 1
                except Exception as e:
                    logger.error(f"Handler: Error processing reply: {e}")

        if total_handled:
            logger.info(f"Handler: Processed {total_handled} replies.")
        else:
            logger.info("Handler: No new replies.")

    # ── Gmail ────────────────────────────────────────────────────────

    async def _run_gmail(self):
        """Walk the live threads and act on anything they said.

        The Sender already checks for a reply immediately before each
        follow-up, which covers the case that matters most. This covers the
        rest: someone who answers after the sequence has finished, or during
        a gap, should still be recorded as having replied rather than sitting
        at 'contacted' until a human notices.
        """
        settings = self.config.channels.email.gmail
        if not settings.can_detect_replies:
            logger.info(
                "Handler: channels.email.gmail.read_scope is 'none', so "
                "replies cannot be seen. Nothing to check."
            )
            return

        from openvz_leads.integrations import gmail as gmail_api

        creds = gmail_api.load_credentials(self.env)
        client = gmail_api.GmailClient(creds, settings.read_scope)
        ready, why = client.readiness()
        if not ready:
            logger.warning(f"Handler: Gmail is not ready — {why}")
            return

        threads = await self.state.get_open_threads()
        if not threads:
            logger.info("Handler: no open threads to check.")
            return

        try:
            our_address = await client.address()
        except gmail_api.GmailError as e:
            logger.error(f"Handler: could not read the mailbox: {e}")
            return

        handled = 0
        for thread in threads:
            try:
                if await self._check_thread(client, thread, our_address):
                    handled += 1
            except gmail_api.GmailNotAuthorised as e:
                logger.error(f"Handler: {e}")
                return
            except Exception as e:
                logger.error(
                    f"Handler: could not check thread "
                    f"{thread.get('provider_thread_id', '')}: {e}"
                )

        logger.info(
            f"Handler: {handled} new repl{'y' if handled == 1 else 'ies'} "
            f"across {len(threads)} thread(s)."
        )

    async def _check_thread(self, client, thread: dict, our_address: str) -> bool:
        """Act on one thread. Returns True when a new reply was found."""
        replies = await client.thread_replies(
            thread["provider_thread_id"], our_address=our_address
        )
        if not replies:
            return False

        latest = replies[-1]
        # Gmail message ids are stable, so they dedupe across cycles exactly
        # the way Instantly's reply uuids did.
        if await self.state.is_reply_processed(latest.message_id):
            return False

        prospect = await self.state.get_prospect(thread["prospect_id"])
        if prospect is None:
            await self.state.mark_reply_processed(latest.message_id)
            return False

        # Stop the sequence first, and unconditionally. Everything below this
        # can fail — a model call, a classification — and none of it should be
        # able to leave a follow-up queued to someone who has answered.
        cancelled = await self.state.cancel_outbox_for_prospect(
            prospect.id, "They replied — follow-ups stopped."
        )
        if cancelled:
            logger.info(
                f"Handler: {prospect.email} replied — stopped {cancelled} "
                "queued follow-up(s)."
            )

        if not latest.body:
            # read_scope is 'metadata': we can see that they answered, not
            # what they said. Record the reply and leave the reading to a
            # person — which is the trade that scope exists to make.
            await pipeline.advance(
                self.state, prospect.id, "replied",
                reason="Replied (headers only — read_scope is 'metadata')",
                actor="handler", crm=self.crm,
            )
            await self.state.mark_reply_processed(latest.message_id)
            return True

        campaign = await self._campaign_for(thread.get("campaign_id", ""))
        try:
            await self._process_reply(
                prospect.email, latest.body, latest.message_id, campaign
            )
        finally:
            await self.state.mark_reply_processed(latest.message_id)
        return True

    async def _campaign_for(self, campaign_id: str):
        if not campaign_id:
            return None
        try:
            return await self.state.get_campaign(campaign_id)
        except Exception:
            return None

    async def _handle_reply(self, reply: dict, campaign) -> bool:
        """Process a single reply. Returns True if a new reply was handled."""
        lead_email = (reply.get("lead_email") or reply.get("from_email") or "").strip().lower()
        reply_text = (reply.get("body") or reply.get("text") or "").strip()
        reply_uuid = str(reply.get("uuid") or reply.get("id") or "")

        if not lead_email or not reply_text:
            return False

        # Dedup: skip if we already processed this reply
        if reply_uuid and await self.state.is_reply_processed(reply_uuid):
            logger.debug(f"Handler: Reply {reply_uuid} already processed. Skipping.")
            return False

        logger.info(f"Handler: Reply from {lead_email}")

        try:
            await self._process_reply(lead_email, reply_text, reply_uuid, campaign)
        finally:
            # ALWAYS mark processed — even on early exits (opt-out, OOO,
            # unknown prospect) — so the same reply is never re-handled
            # or double-replied on the next cycle.
            if reply_uuid:
                await self.state.mark_reply_processed(reply_uuid)
        return True

    async def _process_reply(self, lead_email: str, reply_text: str, reply_uuid: str, campaign):
        """Classify, record, and respond to a single reply."""
        # Find the prospect by email (indexed lookup)
        prospect = await self.state.get_prospect_by_email(lead_email)
        if not prospect:
            logger.warning(f"Handler: No prospect found for {lead_email}")
            return

        # Update prospect status
        await pipeline.advance(
            self.state, prospect.id, "replied",
            reason="They answered", actor="handler", crm=self.crm,
        )

        # 1. Classify intent. Hard keyword checks run FIRST and override
        # the LLM — opt-outs and legal threats must never be missed.
        text_lower = reply_text.lower()
        if any(p in text_lower for p in OPT_OUT_PATTERNS):
            intent = "unsubscribe"
        elif any(p in text_lower for p in ESCALATION_PATTERNS):
            intent = "escalate"
        else:
            intent = await self._classify_intent(reply_text, prospect)
        logger.info(f"Handler: Intent for {lead_email}: {intent}")

        # 2. Get or create conversation
        existing_convos = await self.state.get_conversations_by_status("open")
        convo = next(
            (c for c in existing_convos if c.prospect_id == prospect.id), None
        )

        if not convo:
            convo = Conversation(
                id="",
                prospect_id=prospect.id,
                campaign_id=campaign.id,
                channel="email",
                thread=[
                    Message(sender="prospect", content=reply_text),
                ],
                intent=intent,
                status="open",
            )
            convo.id = await self.state.add_conversation(convo)
        else:
            convo.thread.append(Message(sender="prospect", content=reply_text))
            convo.intent = intent
            await self.state.update_conversation(
                convo.id,
                thread_json=convo.thread_json(),
                intent=intent,
            )

        # 2b. Advance conversation stage based on intent
        new_stage = self._determine_stage(intent, convo.stage, reply_text)
        if new_stage != convo.stage:
            logger.info(f"Handler: Stage for {lead_email}: {convo.stage} -> {new_stage}")
            await self.state.update_conversation(convo.id, stage=new_stage)
            convo.stage = new_stage

        # 3. Route based on intent
        if intent == "unsubscribe":
            # Honor opt-outs ALWAYS. No reply, no future contact.
            await self.state.update_conversation(convo.id, status="closed", stage="closed_lost")
            await pipeline.advance(
                self.state, prospect.id, "opted_out",
                reason="Asked not to be contacted", actor="handler", crm=self.crm,
            )
            await self.state.log_action(
                action_type="opt_out",
                agent="handler",
                details={"prospect_email": lead_email},
            )
            logger.info(f"Handler: {lead_email} opted out. Suppressed permanently. No reply sent.")
            return

        if intent == "escalate":
            # Angry / legal / compliance replies go to a human. Never auto-reply.
            await self.state.update_conversation(convo.id, status="needs_human")
            await self.state.log_action(
                action_type="escalation",
                agent="handler",
                details={
                    "prospect_email": lead_email,
                    "reply_preview": reply_text[:200],
                },
            )
            logger.warning(
                f"Handler: ESCALATED reply from {lead_email} — needs human review. No auto-reply sent."
            )
            return

        if intent == "not_interested":
            await self.state.update_conversation(convo.id, status="closed", stage="closed_lost")
            await pipeline.advance(
                self.state, prospect.id, "lost",
                reason="Said they are not interested", actor="handler", crm=self.crm,
            )
            logger.info(f"Handler: {lead_email} not interested. Closing. One no is enough.")
            return

        if intent == "ooo":
            logger.info(f"Handler: {lead_email} is OOO. Will follow up later.")
            return

        if intent not in AUTO_REPLY_INTENTS:
            logger.warning(f"Handler: No auto-reply policy for intent '{intent}'. Skipping reply.")
            return

        # For interested, objection, question, wrong_person — generate a reply
        response = await self._generate_response(intent, reply_text, prospect, convo)
        if not response:
            logger.warning(f"Handler: Could not generate response for {lead_email}")
            return

        # Send via Instantly
        if reply_uuid:
            result = await self.instantly.send_reply(reply_uuid, response)
            if result is not None:
                # Add our response to conversation
                convo.thread.append(Message(sender="openvz_leads", content=response))
                await self.state.update_conversation(
                    convo.id,
                    thread_json=convo.thread_json(),
                )
                logger.info(f"Handler: Replied to {lead_email}")

                await self.state.log_action(
                    action_type="reply",
                    agent="handler",
                    details={
                        "prospect_email": lead_email,
                        "intent": intent,
                        "response_preview": response[:100],
                    },
                )

    async def _classify_intent(self, reply_text: str, prospect) -> str:
        """Ask the brain to classify the reply's intent."""
        prompt = f"""Classify this email reply into exactly ONE category:
- "interested" — wants to learn more, open to a meeting, positive response
- "objection" — has concerns but hasn't said no (price, timing, competition)
- "not_interested" — a clear no, not now, or "we're all set"
- "unsubscribe" — asks to stop being contacted, remove from list, opt out
- "escalate" — angry, hostile, threatens legal action, or mentions spam/compliance
- "ooo" — out of office / auto-reply
- "wrong_person" — not the right contact, suggests someone else
- "question" — asking for more information before deciding

When in doubt between "not_interested" and "unsubscribe", choose "unsubscribe".
When in doubt between anything and "escalate", choose "escalate".

Reply from {prospect.full_name()} ({prospect.title} at {prospect.company}):
\"\"\"{reply_text}\"\"\"

Respond with ONLY the category label, nothing else."""

        result = await self.brain.think(prompt, session_id="leads-handler")
        if not result:
            # Classifier failed. Do NOT auto-reply blind — flag for a human.
            logger.warning("Handler: Intent classifier returned nothing. Escalating.")
            return "escalate"

        # Robust extraction: take the first recognized label anywhere in
        # the response (models sometimes add explanation despite instructions).
        cleaned = result.strip().strip('"').strip("'").lower()
        if cleaned in INTENT_LABELS:
            return cleaned
        for label in sorted(INTENT_LABELS, key=len, reverse=True):
            if label in cleaned:
                return label

        logger.warning(f"Handler: Unknown intent '{cleaned[:80]}'. Defaulting to 'question'.")
        return "question"

    def _determine_stage(self, intent: str, current_stage: str, reply_text: str) -> str:
        """Advance the conversation stage based on intent and context."""
        text_lower = reply_text.lower()

        # Terminal states
        if intent in ("not_interested", "unsubscribe"):
            return "closed_lost"

        if intent == "escalate":
            return current_stage  # Human decides what happens next

        # Stage advancement rules
        if intent == "interested":
            # Meeting/call signals from an interested prospect → closing
            if any(w in text_lower for w in ["let's meet", "schedule", "calendar", "book a call", "free on", "available"]):
                return "closing"
            if current_stage == "initial_outreach":
                return "engaged"
            if current_stage == "engaged":
                # Check if they're asking about specifics → presenting
                if any(w in text_lower for w in ["price", "cost", "how much", "pricing", "demo", "trial"]):
                    return "presenting"
                return "qualifying"
            if current_stage in ("qualifying", "presenting"):
                return "negotiating"
            if current_stage == "negotiating":
                return "closing"

        if intent == "objection":
            # Objections typically happen during presenting or negotiating
            if current_stage in ("initial_outreach", "engaged"):
                return "qualifying"
            # Stay in current stage during objection handling

        if intent == "question":
            if current_stage == "initial_outreach":
                return "engaged"
            if current_stage == "engaged":
                return "qualifying"

        return current_stage

    async def _generate_response(
        self, intent: str, reply_text: str, prospect, convo: Conversation
    ) -> str:
        """Generate an appropriate response based on intent."""
        # Build conversation history for context
        history = "\n".join(
            f"{'OpenVZ Leads' if m.sender == 'openvz_leads' else prospect.full_name()}: {m.content}"
            for m in convo.thread[-6:]  # Last 6 messages for context
        )

        objection_context = ""
        if intent == "objection":
            # Check if we have a pre-configured response
            for trigger, response in self.config.product.objection_responses.items():
                if trigger.lower() in reply_text.lower():
                    objection_context = f"\nSuggested approach for this objection: {response}"
                    break

        prompt = self.brain.load_prompt("handler", stage=convo.stage)
        if not prompt:
            prompt = f"""You are {self.config.persona.name}, {self.config.persona.role} at {self.config.persona.company}.
Your tone is: {self.config.persona.tone}
Product: {self.config.product.name} — {self.config.product.description}"""

        # Inject objection handling + sales methodology skills
        if self.skills:
            prompt += "\n\n" + self.skills

        prompt += f"""

Conversation so far:
{history}

The prospect's intent is: {intent}
{objection_context}

Write a reply that:"""

        if intent == "interested":
            prompt += """
- Acknowledges their interest warmly
- Suggests a specific next step (brief call or meeting)
- Keeps it short (under 80 words)
- Includes a clear CTA with flexibility on timing"""
        elif intent == "objection":
            prompt += """
- Addresses the concern directly and empathetically
- Provides evidence or a reframe
- Doesn't argue — redirect toward value
- Keeps it under 100 words"""
        elif intent == "question":
            prompt += """
- Answers their question clearly and concisely
- Ties the answer back to value for them
- Ends with a soft CTA
- Under 100 words"""
        elif intent == "wrong_person":
            prompt += """
- Thanks them politely
- Asks who the right person would be
- Makes it easy for them to refer (one-line ask)
- Under 50 words"""

        prompt += "\n\nWrite ONLY the email body. No subject line, no greeting label, no signature block, no markdown."

        response = await self.brain.think(prompt, session_id="leads-handler")
        if not response:
            return ""
        response = response.strip()
        # Guard against the model returning meta-text instead of an email
        if response.lower().startswith(("i can't", "i cannot", "as an ai", "sorry,")):
            logger.warning("Handler: Model returned meta-text instead of an email. Discarding.")
            return ""
        return response
