"""Sender — deploys human-approved campaigns to an outbound provider.

Sending is opt-in. With `channels.email.provider: none` (the default) this
agent is inert: campaigns stay in the review queue and you take them out via
`openvz-leads export`. Nothing is ever sent that a human hasn't approved.

Two providers, and they divide the work very differently.

**instantly** — hand over the sequence and the leads, and the platform owns
everything after that: when each step goes out, substituting merge variables,
the unsubscribe footer, and noticing replies.

**gmail** — send from the user's own mailbox, which means this file owns all
four of those. Each is a way to send a genuinely bad email, so each is
handled explicitly rather than assumed:

  scheduling      one outbox row per step, with the time it may go out
  merge variables openvz_leads/outreach.py, which refuses rather than
                  sending "Hi ,"
  the footer      an opt-out line and a postal address, required by law in
                  most places and by config here
  stop on reply   checked before every follow-up, because a sequence that
                  keeps going after they answer is this product's worst
                  possible failure
"""

import logging
import re
from datetime import date, datetime, timedelta, timezone

from openvz_leads import outreach, pipeline
from openvz_leads.brain import Brain
from openvz_leads.config import LeadsConfig, EnvConfig
from openvz_leads.integrations import gmail as gmail_api
from openvz_leads.integrations.crm import CrmSync
from openvz_leads.integrations.instantly import InstantlyClient
from openvz_leads.state import SENDABLE_CAMPAIGN_STATUS, StateManager

logger = logging.getLogger("openvz_leads.sender")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# A send that failed for a reason that might not be true in ten minutes stays
# pending and is tried again this far out. A send that failed because the
# message itself is wrong is not retried at all.
RETRY_AFTER = timedelta(minutes=30)
MAX_SEND_ATTEMPTS = 3

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Only prospects in these statuses may be added as leads. This guarantees
# we never re-send to someone already contacted, replied, opted out, or lost.
SENDABLE_STATUSES = {"new", "queued"}


class Sender:
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
        # Deploying a sequence is the moment a prospect becomes "contacted",
        # which is the first stage most CRMs want a record for.
        self.crm = CrmSync(config.crm, env)

    async def run(self):
        """Deploy approved campaigns to the configured outbound provider."""
        logger.info("Sender: Checking for approved campaigns to deploy...")

        if not self.config.channels.email.enabled:
            logger.info("Sender: Email channel disabled. Skipping.")
            return

        approved = await self.state.get_campaigns_by_status(
            SENDABLE_CAMPAIGN_STATUS
        )
        if not approved and self.config.channels.email.provider != "gmail":
            logger.info("Sender: No approved campaigns to deploy.")
            return
        # Gmail keeps going with none: the queue from previous campaigns is
        # what has follow-ups due in it, and "no new campaigns" is not "no
        # mail to send".

        # Sending is opt-in — say so clearly rather than failing silently.
        if not self.config.channels.email.sending_enabled:
            logger.info(
                f"Sender: {len(approved)} approved campaign(s) ready, but no "
                "outbound provider is configured (channels.email.provider is "
                "'none'). Export them with 'openvz-leads export emails' and "
                "send from your own tool."
            )
            return

        if self.config.channels.email.provider == "gmail":
            await self._run_gmail(approved)
            return

        if not self.instantly.api_key:
            logger.warning(
                f"Sender: provider is 'instantly' but INSTANTLY_API_KEY is not "
                f"set — {len(approved)} approved campaign(s) cannot be sent. "
                "Add the key to .env, or set channels.email.provider to 'none' "
                "and export instead."
            )
            return

        # Enforce daily send limit
        max_sends = self.config.channels.email.max_daily_sends
        sends_today = await self._count_sends_today()
        remaining = max_sends - sends_today
        if remaining <= 0:
            logger.info(f"Sender: Daily send limit reached ({sends_today}/{max_sends}). Skipping.")
            return

        for campaign in approved:
            try:
                await self._deploy_campaign(campaign)
            except Exception as e:
                logger.error(f"Sender: Failed to deploy campaign {campaign.name}: {e}")

    # ── Gmail ────────────────────────────────────────────────────────

    def _gmail_client(self):
        creds = gmail_api.load_credentials(self.env)
        return gmail_api.GmailClient(creds, self.config.channels.email.gmail.read_scope)

    async def _run_gmail(self, approved):
        """Schedule what has been approved, then send what is due."""
        settings = self.config.channels.email.gmail

        problem = settings.footer.problem()
        if problem:
            # Refusing here rather than at send time so the message names the
            # setting once, instead of once per queued email.
            logger.error(
                f"Sender: not sending — {problem} Nothing has been queued or "
                "sent. Fix it in openvz-leads.yaml, or set "
                "channels.email.provider to 'none' and export instead."
            )
            return

        client = self._gmail_client()
        ready, why = client.readiness()
        if not ready:
            logger.warning(f"Sender: Gmail is not ready — {why}")
            return

        for campaign in approved:
            try:
                await self._schedule_campaign(campaign)
            except Exception as e:
                logger.error(f"Sender: Could not queue campaign {campaign.name}: {e}")

        await self._flush_outbox(client)

    async def _schedule_campaign(self, campaign):
        """Turn an approved campaign into dated rows in the outbox.

        Idempotent by construction: the outbox's unique index on
        (campaign, prospect, step) means running this twice queues nothing the
        second time, so a crash between here and marking the campaign active
        cannot produce a duplicate email.
        """
        if not self._validate_sequence(campaign):
            await self.state.update_campaign(campaign.id, status="failed")
            return

        settings = self.config.channels.email.gmail
        # One first email plus at most this many follow-ups, whatever length
        # of sequence the Writer felt like producing.
        steps = campaign.sequence[: 1 + settings.max_followups]

        rows = []
        prospects = 0
        seen_emails: set[str] = set()
        for prospect_id in campaign.prospect_ids:
            prospect = await self.state.get_prospect(prospect_id)
            if not prospect or not prospect.email:
                continue
            email = prospect.email.strip().lower()
            if not EMAIL_RE.match(email):
                logger.warning(f"Sender: Skipping invalid email '{prospect.email}'")
                continue
            if email in seen_emails:
                continue
            if prospect.status not in SENDABLE_STATUSES:
                logger.debug(
                    f"Sender: Skipping {email} (status '{prospect.status}' is not sendable)."
                )
                continue
            seen_emails.add(email)
            prospects += 1

            when = _utcnow()
            for index, step in enumerate(steps):
                if index > 0:
                    # Whatever the Writer asked for, never closer together
                    # than this. A sequence with delay_days: 0 twice would
                    # otherwise arrive as two emails in one minute.
                    gap = max(int(step.delay_days), settings.min_followup_days)
                    when = when + timedelta(days=gap)
                rows.append({
                    "campaign_id": campaign.id,
                    "prospect_id": prospect.id,
                    "step": step.step or (index + 1),
                    "subject": step.subject,
                    "body": step.body,
                    "send_after": when.isoformat(),
                })

        if not rows:
            logger.warning(f"Sender: No sendable prospects in '{campaign.name}'.")
            await self.state.update_campaign(campaign.id, status="active")
            return

        queued = await self.state.schedule_outbox(rows)
        await self.state.update_campaign(campaign.id, status="active")
        await self.state.log_action(
            action_type="send_campaign",
            agent="sender",
            details={
                "campaign_name": campaign.name,
                "provider": "gmail",
                "prospects": prospects,
                "queued": queued,
            },
        )
        logger.info(
            f"Sender: Queued {queued} message(s) for {prospects} prospect(s) "
            f"from '{campaign.name}' — {len(steps)} step(s) each."
        )

    async def _flush_outbox(self, client):
        """Send everything that is due, within today's budget."""
        max_sends = self.config.channels.email.max_daily_sends
        # Counts follow-ups, not just first contacts: a cap that only counted
        # new prospects would let a mailbox send three times what it was told.
        sent_today = await self.state.count_outbox_sent_today()
        budget = max_sends - sent_today
        if budget <= 0:
            due = await self.state.count_due_outbox()
            if due:
                logger.info(
                    f"Sender: daily limit reached ({sent_today}/{max_sends}); "
                    f"{due} message(s) wait for tomorrow."
                )
            return

        due = await self.state.get_due_outbox(limit=budget)
        if not due:
            logger.info("Sender: nothing due to send.")
            return

        sender_name = (
            self.config.channels.email.gmail.sender_name
            or self.config.persona.name
        )
        footer = self.config.channels.email.gmail.footer.render()

        sent = 0
        # At most one message per person per pass. Overdue steps all become
        # due at the same instant after any outage, and the schedule alone
        # would then fire them back to back.
        served: set[str] = set()
        for row in due:
            if row["prospect_id"] in served:
                continue
            try:
                if await self._send_one(client, row, sender_name, footer):
                    sent += 1
                    served.add(row["prospect_id"])
            except gmail_api.GmailNotAuthorised as e:
                # Every remaining row would fail the same way. Stop rather
                # than burning attempts on all of them.
                logger.error(f"Sender: {e}")
                break
            except Exception as e:
                logger.error(f"Sender: unexpected error sending {row['id']}: {e}")
                await self.state.mark_outbox_failed(row["id"], str(e))

        logger.info(f"Sender: sent {sent} of {len(due)} due message(s).")

    async def _send_one(self, client, row, sender_name: str, footer: str) -> bool:
        """Send one queued message. Returns True when something went out."""
        prospect = await self.state.get_prospect(row["prospect_id"])
        if prospect is None:
            await self.state.mark_outbox_failed(row["id"], "prospect no longer exists")
            return False

        # The prospect may have moved since this was queued — days ago, for a
        # follow-up. Re-check rather than trusting the schedule.
        stage = pipeline.normalize(prospect.status)
        if stage in ("replied", "opted_out", "lost", "won", "meeting"):
            cancelled = await self.state.cancel_outbox_for_prospect(
                prospect.id, f"Stage is '{stage}' — nothing further should go out."
            )
            logger.info(
                f"Sender: cancelled {cancelled} queued message(s) for "
                f"{prospect.email}: stage is '{stage}'."
            )
            return False

        anchor = await self.state.get_thread_anchor(row["campaign_id"], prospect.id)

        # The check this whole feature exists to get right.
        if int(row["step"]) > 1 and anchor:
            try:
                if await self._stop_because_they_replied(client, prospect, anchor):
                    return False
            except gmail_api.GmailNotAuthorised:
                raise
            except gmail_api.GmailError as e:
                # Not being able to read the mailbox right now says nothing
                # about whether they replied. Deferring costs a delay;
                # guessing costs a follow-up sent into someone's answer.
                await self.state.defer_outbox(
                    row["id"], _utcnow() + RETRY_AFTER, f"reply check failed: {e}"
                )
                return False

        subject = row["subject"]
        if int(row["step"]) > 1 and anchor:
            # Threaded follow-ups keep the original subject; a new one makes
            # it a second cold email rather than the same conversation.
            subject = outreach.follow_up_subject(anchor.get("subject") or subject)

        rendered_subject, body, blockers = outreach.render_email(
            subject, row["body"], prospect, footer=footer
        )
        if blockers:
            reason = "Not sent — " + "; ".join(blockers)
            await self.state.mark_outbox_failed(row["id"], reason)
            logger.warning(f"Sender: {prospect.email}: {reason}")
            return False

        try:
            result = await client.send(
                to=prospect.email,
                subject=rendered_subject,
                body=body,
                sender_name=sender_name,
                thread_id=(anchor or {}).get("provider_thread_id", ""),
                in_reply_to=(anchor or {}).get("rfc_message_id", ""),
            )
        except gmail_api.GmailNotAuthorised:
            raise
        except gmail_api.GmailError as e:
            attempts = int(row.get("attempts") or 0) + 1
            if attempts >= MAX_SEND_ATTEMPTS:
                await self.state.mark_outbox_failed(
                    row["id"], f"gave up after {attempts} attempts: {e}"
                )
                logger.error(f"Sender: giving up on {prospect.email}: {e}")
            else:
                await self.state.defer_outbox(
                    row["id"], _utcnow() + RETRY_AFTER, str(e)
                )
                logger.warning(f"Sender: will retry {prospect.email}: {e}")
            return False

        await self.state.mark_outbox_sent(
            row["id"],
            message_id=result.message_id,
            thread_id=result.thread_id,
            rfc_message_id=result.rfc_message_id,
        )

        # Measure the next gap from when this actually went out, not from
        # when it was scheduled to. See state.rebase_outbox_after_send.
        await self.state.rebase_outbox_after_send(
            prospect.id,
            above_step=int(row["step"]),
            earliest=_utcnow()
            + timedelta(days=self.config.channels.email.gmail.min_followup_days),
        )

        if int(row["step"]) == 1:
            await pipeline.advance(
                self.state, prospect.id, "contacted",
                reason="First email sent", actor="sender", crm=self.crm,
            )
        logger.info(
            f"Sender: step {row['step']} → {prospect.email} "
            f"({'new thread' if not anchor else 'follow-up'})"
        )
        return True

    async def _stop_because_they_replied(self, client, prospect, anchor) -> bool:
        """True when a reply means this follow-up must not go out.

        Errs towards not sending. If the mailbox cannot be read right now, the
        follow-up is deferred rather than sent: a delayed email is a small
        cost, and one sent into a reply is the thing this is here to prevent.
        """
        if not self.config.channels.email.gmail.can_detect_replies:
            return False

        thread_id = anchor.get("provider_thread_id") or ""
        if not thread_id:
            return False

        try:
            replies = await client.thread_replies(
                thread_id, our_address=await client.address()
            )
        except gmail_api.GmailNotAuthorised:
            raise
        except gmail_api.GmailError as e:
            logger.warning(
                f"Sender: could not check {prospect.email}'s thread ({e}); "
                "deferring the follow-up rather than risking a send into a reply."
            )
            # Raised, not returned: the caller defers this row. Returning
            # False here would mean "no reply found", which is exactly the
            # thing we do not know.
            raise

        if not replies:
            return False

        cancelled = await self.state.cancel_outbox_for_prospect(
            prospect.id, "They replied — follow-ups stopped."
        )
        await pipeline.advance(
            self.state, prospect.id, "replied",
            reason="Replied in the email thread", actor="sender", crm=self.crm,
        )
        logger.info(
            f"Sender: {prospect.email} replied — stopped {cancelled} "
            "queued follow-up(s)."
        )
        return True

    def _validate_sequence(self, campaign) -> bool:
        """Never deploy a broken or empty sequence."""
        if not campaign.sequence:
            logger.error(f"Sender: Campaign '{campaign.name}' has no email sequence. Marking failed.")
            return False
        for step in campaign.sequence:
            if not (step.subject or "").strip() or not (step.body or "").strip():
                logger.error(
                    f"Sender: Campaign '{campaign.name}' step {step.step} has an empty "
                    "subject or body. Marking failed."
                )
                return False
            if step.delay_days < 0:
                logger.error(
                    f"Sender: Campaign '{campaign.name}' step {step.step} has a negative delay."
                )
                return False
        return True

    async def _deploy_campaign(self, campaign):
        """Deploy a single campaign to Instantly. Idempotent: safe to retry."""
        logger.info(f"Sender: Deploying campaign '{campaign.name}'...")

        # 0. Validate before touching the network
        if not self._validate_sequence(campaign):
            await self.state.update_campaign(campaign.id, status="failed")
            return

        # 1. Create campaign in Instantly — or resume one from a previous
        # partially-failed deploy. Never create a duplicate.
        campaign_id = campaign.instantly_campaign_id
        if campaign_id:
            logger.info(
                f"Sender: Campaign '{campaign.name}' already has Instantly ID "
                f"{campaign_id}. Resuming deploy instead of recreating."
            )
        else:
            instantly_campaign = await self.instantly.create_campaign(campaign.name)
            if not instantly_campaign or not isinstance(instantly_campaign, dict):
                logger.error(f"Sender: Failed to create Instantly campaign: {campaign.name}")
                return

            campaign_id = instantly_campaign.get("id")
            if not campaign_id:
                logger.error("Sender: No campaign ID returned from Instantly.")
                return

            # Persist the ID immediately so a crash mid-deploy resumes this
            # campaign instead of creating a second one (double-send guard).
            await self.state.update_campaign(campaign.id, instantly_campaign_id=campaign_id)
            campaign.instantly_campaign_id = campaign_id

        # 2. Set email sequence
        sequences = [
            {
                "subject": step.subject.strip(),
                "body": step.body.strip(),
                "wait": step.delay_days,
            }
            for step in campaign.sequence
        ]

        result = await self.instantly.set_campaign_emails(campaign_id, sequences)
        if result is None:
            logger.error(f"Sender: Failed to set emails for campaign {campaign_id}")
            return

        # 3. Add leads — validated, deduped, and only never-contacted prospects
        prospects = []
        seen_emails: set[str] = set()
        for prospect_id in campaign.prospect_ids:
            prospect = await self.state.get_prospect(prospect_id)
            if not prospect or not prospect.email:
                continue
            email = prospect.email.strip().lower()
            if not EMAIL_RE.match(email):
                logger.warning(f"Sender: Skipping invalid email '{prospect.email}'")
                continue
            if email in seen_emails:
                continue
            if prospect.status not in SENDABLE_STATUSES:
                # Already contacted / replied / opted out — never double-send.
                logger.debug(
                    f"Sender: Skipping {email} (status '{prospect.status}' is not sendable)."
                )
                continue
            seen_emails.add(email)
            prospects.append(prospect)

        if not prospects:
            # Retry path: leads were already staged and marked 'contacted'
            # on a previous cycle but activation failed. Just activate.
            already_staged = False
            for pid in campaign.prospect_ids:
                p = await self.state.get_prospect(pid)
                if p and p.status == "contacted":
                    already_staged = True
                    break
            if already_staged:
                logger.info(
                    f"Sender: Leads for '{campaign.name}' already staged. Retrying activation."
                )
                if await self.instantly.activate_campaign(campaign_id) is not None:
                    await self.state.update_campaign(campaign.id, status="active")
                    logger.info(f"Sender: Campaign '{campaign.name}' activated on retry.")
                return
            logger.warning(f"Sender: No valid prospects for campaign {campaign.name}")
            return

        # Check remaining daily send budget
        max_sends = self.config.channels.email.max_daily_sends
        sends_today = await self._count_sends_today()
        remaining = max_sends - sends_today
        if remaining <= 0:
            logger.info(f"Sender: Daily limit reached. Deferring campaign '{campaign.name}'.")
            return
        if len(prospects) > remaining:
            logger.info(f"Sender: Capping leads from {len(prospects)} to {remaining} (daily limit).")
            prospects = prospects[:remaining]

        leads = [
            {
                "email": p.email.strip().lower(),
                "first_name": p.first_name,
                "last_name": p.last_name,
                "company_name": p.company,
                "variables": {
                    "title": p.title,
                    "personalization": p.personalization_notes,
                },
            }
            for p in prospects
        ]

        result = await self.instantly.add_leads(campaign_id, leads)
        if result is None:
            logger.error(f"Sender: Failed to add leads to campaign {campaign_id}")
            return

        # Mark prospects as contacted BEFORE activation: if activation
        # succeeds but this write failed, a retry would re-add the same
        # leads. Better to under-count than double-send.
        for prospect in prospects:
            await pipeline.advance(
                self.state, prospect.id, "contacted",
                reason="Sequence deployed", actor="sender", crm=self.crm,
            )

        # 4. Activate campaign
        result = await self.instantly.activate_campaign(campaign_id)
        if result is None:
            logger.error(
                f"Sender: Failed to activate campaign {campaign_id}. "
                "Leads are staged; will retry activation next cycle."
            )
            return

        # 5. Update our records
        await self.state.update_campaign(
            campaign.id,
            instantly_campaign_id=campaign_id,
            status="active",
        )

        await self.state.log_action(
            action_type="send_campaign",
            agent="sender",
            details={
                "campaign_name": campaign.name,
                "instantly_campaign_id": campaign_id,
                "leads_added": len(leads),
            },
        )

        logger.info(
            f"Sender: Campaign '{campaign.name}' deployed to Instantly "
            f"with {len(leads)} leads. Campaign ID: {campaign_id}"
        )

    async def _count_sends_today(self) -> int:
        """Count how many prospects were contacted today."""
        import aiosqlite
        today = date.today().isoformat()
        async with aiosqlite.connect(self.state.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM prospects WHERE status = 'contacted' AND updated_at LIKE ?",
                (f"{today}%",),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0
