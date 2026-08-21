"""Sending through Gmail, and the four things the platform used to do.

The tests that matter most here are the ones about *not* sending: into a
reply, without an opt-out footer, with a merge variable that has no value, or
twice. Each of those is a way to put a genuinely bad email in front of a real
person, and none of them fails loudly on its own.
"""

import pytest
import pytest_asyncio

from openvz_leads import outreach, pipeline
from openvz_leads.agents.sender import Sender
from openvz_leads.config import (
    ChannelsConfig,
    EmailChannelConfig,
    EnvConfig,
    GmailConfig,
    GmailFooterConfig,
    ICPConfig,
    LeadsConfig,
    OfferConfig,
    PersonaConfig,
    ProductConfig,
)
from openvz_leads.integrations import gmail as gmail_api
from openvz_leads.models.campaign import Campaign, EmailStep
from openvz_leads.models.prospect import Prospect
from openvz_leads.state import StateManager


# ── Fixtures ──


def make_config(**gmail_kwargs) -> LeadsConfig:
    gmail = GmailConfig(
        footer=GmailFooterConfig(postal_address="12 Example St, Springfield"),
        **gmail_kwargs,
    )
    return LeadsConfig(
        persona=PersonaConfig(
            name="Sam Rep", company="Acme", role="BD",
            email="sam@acme.test", linkedin="", tone="direct",
        ),
        product=ProductConfig(
            name="Thing", description="Does things", pricing="$1",
            key_benefits=["a"], objection_responses={}, offer=OfferConfig(),
        ),
        icp=ICPConfig(
            industries=["Dental"], company_size="5-50", titles=["Owner"],
            geography=["United States"],
        ),
        channels=ChannelsConfig(
            email=EmailChannelConfig(provider="gmail", gmail=gmail)
        ),
    )


class FakeGmail:
    """A mailbox that records what it was asked to send."""

    def __init__(self, replies=None, send_error=None, read_error=None):
        self.sent = []
        self.replies = replies or {}
        self.send_error = send_error
        self.read_error = read_error
        self.thread_reads = 0

    def readiness(self):
        return True, ""

    async def address(self):
        return "sam@acme.test"

    async def send(self, *, to, subject, body, sender_name="", thread_id="", in_reply_to=""):
        if self.send_error:
            raise self.send_error
        self.sent.append({
            "to": to, "subject": subject, "body": body,
            "sender_name": sender_name, "thread_id": thread_id,
            "in_reply_to": in_reply_to,
        })
        index = len(self.sent)
        return gmail_api.SentMessage(
            message_id=f"m{index}",
            thread_id=thread_id or f"t{index}",
            rfc_message_id=f"<{index}@mail.test>",
        )

    async def thread_replies(self, thread_id, *, our_address="", after_ms=0):
        self.thread_reads += 1
        if self.read_error:
            raise self.read_error
        return self.replies.get(thread_id, [])


@pytest_asyncio.fixture
async def state(tmp_path):
    manager = StateManager(str(tmp_path / "leads.db"))
    await manager.init_db()
    return manager


@pytest_asyncio.fixture
async def prospect_id(state):
    return await state.add_prospect(
        Prospect(
            first_name="Lena", last_name="Fischer", title="Head of Procurement",
            email="lena@northwind.test", company="Northwind", status="queued",
        )
    )


async def make_campaign(state, prospect_id, steps=3):
    campaign = Campaign(
        id="",
        name="dental-outreach",
        status="approved",
        prospect_ids=[prospect_id],
        sequence=[
            EmailStep(
                step=i,
                subject=f"{{{{first_name}}}}, about {{{{company}}}} #{i}",
                body=f"Hi {{{{first_name}}}}, body {i}.",
                delay_days=0 if i == 1 else 3,
            )
            for i in range(1, steps + 1)
        ],
    )
    campaign.id = await state.add_campaign(campaign)
    return await state.get_campaign(campaign.id)


def make_sender(state, config, gmail):
    sender = Sender(brain=None, state=state, config=config, env=EnvConfig())
    sender._gmail_client = lambda: gmail
    return sender


# ── Scheduling ──


@pytest.mark.asyncio
class TestScheduling:
    async def test_a_sequence_becomes_dated_rows(self, state, prospect_id):
        config = make_config()
        campaign = await make_campaign(state, prospect_id)
        await make_sender(state, config, FakeGmail())._schedule_campaign(campaign)

        assert await state.count_pending_outbox() == 3
        # Only the first is due; the follow-ups are days out.
        assert len(await state.get_due_outbox()) == 1

    async def test_max_followups_truncates_the_writer(self, state, prospect_id):
        config = make_config(max_followups=1)
        campaign = await make_campaign(state, prospect_id, steps=5)
        await make_sender(state, config, FakeGmail())._schedule_campaign(campaign)
        assert await state.count_pending_outbox() == 2  # first + one follow-up

    async def test_a_zero_delay_still_gets_a_gap(self, state, prospect_id):
        """A Writer that emits delay_days: 0 twice must not send twice at once."""
        config = make_config(min_followup_days=2)
        campaign = Campaign(
            id="", name="c", status="approved", prospect_ids=[prospect_id],
            sequence=[
                EmailStep(step=1, subject="a", body="a", delay_days=0),
                EmailStep(step=2, subject="b", body="b", delay_days=0),
            ],
        )
        campaign.id = await state.add_campaign(campaign)
        campaign = await state.get_campaign(campaign.id)
        await make_sender(state, config, FakeGmail())._schedule_campaign(campaign)
        assert len(await state.get_due_outbox()) == 1

    async def test_scheduling_twice_queues_nothing_the_second_time(
        self, state, prospect_id
    ):
        config = make_config()
        campaign = await make_campaign(state, prospect_id)
        sender = make_sender(state, config, FakeGmail())
        await sender._schedule_campaign(campaign)
        await sender._schedule_campaign(campaign)
        assert await state.count_pending_outbox() == 3

    async def test_an_already_contacted_prospect_is_not_queued(self, state, prospect_id):
        await state.update_prospect_status(prospect_id, "contacted")
        config = make_config()
        campaign = await make_campaign(state, prospect_id)
        await make_sender(state, config, FakeGmail())._schedule_campaign(campaign)
        assert await state.count_pending_outbox() == 0


# ── Sending ──


@pytest.mark.asyncio
class TestSending:
    async def test_the_first_email_goes_out_rendered_and_footed(
        self, state, prospect_id
    ):
        config = make_config()
        gmail = FakeGmail()
        sender = make_sender(state, config, gmail)
        await sender._schedule_campaign(await make_campaign(state, prospect_id))
        await sender._flush_outbox(gmail)

        assert len(gmail.sent) == 1
        message = gmail.sent[0]
        assert "{{" not in message["subject"] and "{{" not in message["body"]
        assert "Lena" in message["subject"] and "Northwind" in message["subject"]
        assert "12 Example St" in message["body"]
        assert "stop" in message["body"]
        assert message["sender_name"] == "Sam Rep"
        # First email opens a thread rather than joining one.
        assert message["thread_id"] == "" and message["in_reply_to"] == ""

    async def test_sending_advances_the_stage(self, state, prospect_id):
        config = make_config()
        gmail = FakeGmail()
        sender = make_sender(state, config, gmail)
        await sender._schedule_campaign(await make_campaign(state, prospect_id))
        await sender._flush_outbox(gmail)
        assert (await state.get_prospect(prospect_id)).status == "contacted"

    async def test_the_daily_cap_counts_follow_ups_too(self, state, prospect_id):
        config = make_config()
        config.channels.email.max_daily_sends = 1
        gmail = FakeGmail()
        sender = make_sender(state, config, gmail)
        await sender._schedule_campaign(await make_campaign(state, prospect_id))
        await sender._flush_outbox(gmail)
        await sender._flush_outbox(gmail)  # budget is spent
        assert len(gmail.sent) == 1

    async def test_a_follow_up_threads_onto_the_first(self, state, prospect_id):
        config = make_config()
        gmail = FakeGmail()
        sender = make_sender(state, config, gmail)
        await sender._schedule_campaign(await make_campaign(state, prospect_id))
        await sender._flush_outbox(gmail)

        # Bring step 2 forward, as time would.
        due = await _make_everything_due(state)
        assert due
        await sender._flush_outbox(gmail)

        assert len(gmail.sent) == 2
        follow_up = gmail.sent[1]
        assert follow_up["thread_id"] == "t1"
        assert follow_up["in_reply_to"] == "<1@mail.test>"
        assert follow_up["subject"].startswith("Re: ")

    async def test_a_backlog_does_not_fire_the_whole_sequence_at_once(
        self, state, prospect_id
    ):
        """After an outage every overdue step is due at the same instant.

        Sending them back to back would defeat the follow-up gap entirely —
        three emails in one minute, which is the shape of a bug the recipient
        experiences as harassment.
        """
        config = make_config()
        gmail = FakeGmail()
        sender = make_sender(state, config, gmail)
        await sender._schedule_campaign(await make_campaign(state, prospect_id))
        await _make_everything_due(state)

        await sender._flush_outbox(gmail)
        assert len(gmail.sent) == 1

        # And the ones behind it were pushed out, rather than staying due.
        assert await state.count_due_outbox() == 0
        assert await state.count_pending_outbox() == 2

    async def test_a_transient_send_error_is_retried_not_dropped(
        self, state, prospect_id
    ):
        config = make_config()
        gmail = FakeGmail(send_error=gmail_api.GmailError("502 upstream"))
        sender = make_sender(state, config, gmail)
        await sender._schedule_campaign(await make_campaign(state, prospect_id))
        await sender._flush_outbox(gmail)

        assert gmail.sent == []
        # Still queued, just later.
        assert await state.count_pending_outbox() == 3
        assert await state.count_due_outbox() == 0


# ── The refusals ──


@pytest.mark.asyncio
class TestRefusals:
    async def test_nothing_is_sent_without_a_postal_address(self, state, prospect_id):
        config = make_config()
        config.channels.email.gmail.footer.postal_address = ""
        gmail = FakeGmail()
        sender = make_sender(state, config, gmail)
        campaign = await make_campaign(state, prospect_id)

        await sender._run_gmail([campaign])

        assert gmail.sent == []
        # Not even queued: the refusal happens before anything is scheduled,
        # so the message names the setting once rather than per email.
        assert await state.count_pending_outbox() == 0

    async def test_a_missing_merge_value_stops_that_message(self, state):
        """"I saw 's site" is not a sentence, and there is no fallback."""
        prospect_id = await state.add_prospect(
            Prospect(
                first_name="Sam", last_name="W", title="Owner",
                email="sam@nowhere.test", company="", status="queued",
            )
        )
        config = make_config()
        gmail = FakeGmail()
        sender = make_sender(state, config, gmail)
        await sender._schedule_campaign(await make_campaign(state, prospect_id))
        await sender._flush_outbox(gmail)

        assert gmail.sent == []
        rows = await state.get_due_outbox(limit=10)
        assert rows == []  # marked failed, not left to retry forever

    async def test_a_reply_stops_the_follow_up(self, state, prospect_id):
        """The failure this whole feature exists to prevent."""
        config = make_config()
        gmail = FakeGmail()
        sender = make_sender(state, config, gmail)
        await sender._schedule_campaign(await make_campaign(state, prospect_id))
        await sender._flush_outbox(gmail)
        assert len(gmail.sent) == 1

        gmail.replies["t1"] = [
            gmail_api.ThreadReply(
                message_id="r1", from_address="lena@northwind.test",
                received_at=1, subject="Re:", body="Interested, tell me more",
            )
        ]
        await _make_everything_due(state)
        await sender._flush_outbox(gmail)

        assert len(gmail.sent) == 1, "a follow-up was sent after they replied"
        assert await state.count_pending_outbox() == 0
        assert (await state.get_prospect(prospect_id)).status == "replied"

    async def test_an_unreadable_mailbox_defers_rather_than_guesses(
        self, state, prospect_id
    ):
        """Not knowing whether they replied is not the same as knowing they
        did not."""
        config = make_config()
        gmail = FakeGmail()
        sender = make_sender(state, config, gmail)
        await sender._schedule_campaign(await make_campaign(state, prospect_id))
        await sender._flush_outbox(gmail)

        gmail.read_error = gmail_api.GmailError("Gmail is unreachable")
        await _make_everything_due(state)
        await sender._flush_outbox(gmail)

        assert len(gmail.sent) == 1, "sent a follow-up without checking for a reply"
        assert await state.count_pending_outbox() == 2  # deferred, not dropped

    async def test_a_stage_change_between_queue_and_send_cancels_the_rest(
        self, state, prospect_id
    ):
        config = make_config()
        gmail = FakeGmail()
        sender = make_sender(state, config, gmail)
        await sender._schedule_campaign(await make_campaign(state, prospect_id))
        await sender._flush_outbox(gmail)

        await pipeline.advance(
            state, prospect_id, "opted_out", reason="asked to stop", actor="handler"
        )
        await _make_everything_due(state)
        await sender._flush_outbox(gmail)

        assert len(gmail.sent) == 1
        assert await state.count_pending_outbox() == 0


# ── Rendering, at the seam where it is used ──


class TestFollowUpSubject:
    def test_a_follow_up_keeps_the_conversation(self):
        assert outreach.follow_up_subject("quick question") == "Re: quick question"

    def test_re_is_not_stacked(self):
        assert outreach.follow_up_subject("Re: already") == "Re: already"


class TestFooter:
    def test_a_blank_address_is_a_problem(self):
        assert "postal_address" in GmailFooterConfig().problem()

    def test_turning_it_off_is_a_problem_too(self):
        footer = GmailFooterConfig(enabled=False, postal_address="12 Example St")
        assert footer.problem()

    def test_a_complete_footer_renders_both_lines(self):
        footer = GmailFooterConfig(postal_address="12 Example St")
        assert footer.problem() == ""
        assert "stop" in footer.render() and "12 Example St" in footer.render()


class TestConfigGuards:
    def test_follow_ups_you_cannot_stop_are_rejected(self):
        with pytest.raises(ValueError):
            EmailChannelConfig(
                provider="gmail",
                gmail=GmailConfig(read_scope="none", max_followups=2),
            )

    def test_send_only_is_allowed_with_no_follow_ups(self):
        channel = EmailChannelConfig(
            provider="gmail",
            gmail=GmailConfig(read_scope="none", max_followups=0),
        )
        assert channel.sending_enabled

    def test_metadata_is_the_default_scope(self):
        assert GmailConfig().read_scope == "metadata"
        assert GmailConfig().can_detect_replies


# ── The upgrade path ──


@pytest.mark.asyncio
async def test_an_existing_database_gains_the_outbox_without_losing_anything(tmp_path):
    """v1.1.0 → v1.2.0, on a database with data already in it.

    Migrations only ever fail on somebody's real, populated database, which
    is the latest and most expensive moment to find out. Applying the old
    schema and then upgrading it costs a millisecond here.
    """
    import sqlite3

    from openvz_leads import state as state_module

    path = str(tmp_path / "upgrade.db")
    manager = StateManager(path)

    original = state_module.MIGRATIONS[:]
    try:
        # Everything up to and including v4 — the schema v1.1.0 shipped.
        state_module.MIGRATIONS[:] = original[:4]
        await manager.init_db()
    finally:
        state_module.MIGRATIONS[:] = original

    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name = 'outbox'"
        ).fetchone()

    prospect_id = await manager.add_prospect(
        Prospect(first_name="Lena", last_name="F", title="Owner", email="l@x.test")
    )

    await manager.init_db()  # the upgrade

    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == len(original)
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name = 'uq_outbox_step'"
        ).fetchone(), "the double-send guard did not survive the upgrade"

    assert (await manager.get_prospect(prospect_id)).first_name == "Lena"

    from openvz_leads.state import _utcnow

    row = [{
        "campaign_id": "c", "prospect_id": prospect_id, "step": 1,
        "subject": "s", "body": "b", "send_after": _utcnow().isoformat(),
    }]
    assert await manager.schedule_outbox(row) == 1
    assert await manager.schedule_outbox(row) == 0


# ── Helper ──


async def _make_everything_due(state):
    """Pull every pending send back to now, standing in for the passage of time."""
    import aiosqlite

    from openvz_leads.state import _utcnow

    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            "UPDATE outbox SET send_after = ? WHERE status = 'pending'",
            (_utcnow().isoformat(),),
        )
        await db.commit()
    return await state.get_due_outbox(limit=10)
