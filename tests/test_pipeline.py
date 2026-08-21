"""Stage transitions, and what the CRM is told about them."""

import pytest
import pytest_asyncio

from openvz_leads import pipeline
from openvz_leads.config import CrmConfig
from openvz_leads.integrations.crm import CrmSync, build_payload
from openvz_leads.models.company import Company
from openvz_leads.models.prospect import Prospect
from openvz_leads.state import StateManager


@pytest_asyncio.fixture
async def state(tmp_path):
    manager = StateManager(str(tmp_path / "leads.db"))
    await manager.init_db()
    return manager


@pytest_asyncio.fixture
async def prospect_id(state):
    company_id = await state.add_company(
        Company(name="Northwind", domain="northwind.test")
    )
    return await state.add_prospect(
        Prospect(
            first_name="Lena",
            last_name="Fischer",
            title="Head of Procurement",
            email="lena@northwind.test",
            company_id=company_id,
            company="Northwind",
            score=72,
        )
    )


class Recorder(CrmSync):
    """A CRM that remembers instead of sending, and can be told to fail."""

    def __init__(self, fail_times=0, permanent=False, stages=None):
        config = CrmConfig(provider="webhook", webhook_url="https://crm.test/hook")
        if stages is not None:
            config.sync_stages = stages
        super().__init__(config)
        self.sent = []
        self.fail_times = fail_times
        self.permanent = permanent

    async def push(self, payload):
        if self.fail_times > 0:
            self.fail_times -= 1
            return False, "boom", self.permanent
        self.sent.append(payload)
        return True, "", False


# ── The rules themselves ──


class TestRules:
    def test_legacy_values_map_to_real_stages(self):
        assert pipeline.normalize("closed") == "won"
        assert pipeline.normalize("unsubscribed") == "opted_out"
        assert pipeline.normalize("") == "new"

    def test_an_unknown_value_lands_at_the_start_not_the_end(self):
        # The safe wrong answer: a record replayed through outreach is
        # recoverable, a record skipped past it is not.
        assert pipeline.normalize("whatever-this-is") == "new"

    def test_terminal_stages_do_not_reopen(self):
        ok, why = pipeline.can_move("won", "replied")
        assert not ok and "final" in why

    def test_opting_out_is_reachable_from_a_terminal_stage(self):
        assert pipeline.can_move("won", "opted_out")[0]

    def test_nothing_moves_back_to_new(self):
        assert not pipeline.can_move("contacted", "new")[0]

    def test_moving_to_where_you_already_are_is_refused(self):
        assert not pipeline.can_move("replied", "replied")[0]

    def test_skipping_ahead_is_allowed(self):
        # A reply can arrive before the send is recorded, and a person can
        # book a meeting off a phone call. Order is not enforced forwards.
        assert pipeline.can_move("new", "meeting")[0]


@pytest.mark.asyncio
class TestAdvance:
    async def test_a_move_is_recorded_with_its_reason(self, state, prospect_id):
        await pipeline.advance(
            state, prospect_id, "contacted", reason="Sequence sent", actor="sender"
        )
        history = await state.get_stage_history(prospect_id)
        assert [(e["from_stage"], e["to_stage"]) for e in history] == [
            ("new", "contacted")
        ]
        assert history[0]["reason"] == "Sequence sent"
        assert history[0]["actor"] == "sender"

    async def test_an_agent_cannot_declare_a_win(self, state, prospect_id):
        await pipeline.advance(state, prospect_id, "replied", actor="handler")
        assert not await pipeline.advance(
            state, prospect_id, "won", actor="handler"
        )
        assert (await state.get_prospect(prospect_id)).status == "replied"

    async def test_a_person_can(self, state, prospect_id):
        await pipeline.advance(state, prospect_id, "replied", actor="handler")
        assert await pipeline.advance(state, prospect_id, "won", actor="human")

    async def test_force_records_the_correction_rather_than_hiding_it(
        self, state, prospect_id
    ):
        await pipeline.advance(state, prospect_id, "won", actor="human")
        assert await pipeline.advance(
            state, prospect_id, "replied", actor="human", force=True
        )
        history = await state.get_stage_history(prospect_id)
        assert history[-1]["from_stage"] == "won"

    async def test_a_missing_prospect_is_refused_not_raised(self, state):
        assert not await pipeline.advance(state, "nope", "contacted")


@pytest.mark.asyncio
class TestCrmSync:
    async def test_only_configured_stages_are_pushed(self, state, prospect_id):
        crm = Recorder()
        await pipeline.advance(state, prospect_id, "queued", actor="writer", crm=crm)
        await pipeline.advance(state, prospect_id, "contacted", actor="sender", crm=crm)
        assert [p["to_stage"] for p in crm.sent] == ["contacted"]

    async def test_a_failed_push_is_retried_not_lost(self, state, prospect_id):
        crm = Recorder(fail_times=1)
        await pipeline.advance(state, prospect_id, "contacted", actor="sender", crm=crm)
        assert crm.sent == []
        assert len(await state.get_unsynced_stage_events()) == 1

        sent, failed = await pipeline.sync_pending(state, crm)
        assert (sent, failed) == (1, 0)
        assert await state.get_unsynced_stage_events() == []

    async def test_a_rejected_push_stops_blocking_the_queue(self, state, prospect_id):
        crm = Recorder(fail_times=1, permanent=True)
        await pipeline.advance(state, prospect_id, "contacted", actor="sender", crm=crm)
        await pipeline.sync_pending(state, crm)
        # Marked failed rather than pending, so it is not retried forever.
        assert await state.get_unsynced_stage_events() == []

    async def test_history_reaches_the_crm_in_order(self, state, prospect_id):
        """The bug this exists to catch: an early failure replayed late, so a
        record shows 'won' before 'contacted'."""
        crm = Recorder(fail_times=1)
        for stage, actor in (
            ("contacted", "sender"),
            ("replied", "handler"),
            ("meeting", "human"),
        ):
            await pipeline.advance(state, prospect_id, stage, actor=actor, crm=crm)
        await pipeline.sync_pending(state, crm)
        assert [p["to_stage"] for p in crm.sent] == ["contacted", "replied", "meeting"]

    async def test_sync_is_a_no_op_when_switched_off(self, state, prospect_id):
        off = CrmSync(CrmConfig(provider="none"))
        await pipeline.advance(state, prospect_id, "contacted", crm=off)
        assert await pipeline.sync_pending(state, off) == (0, 0)


class TestWhatTheCrmWants:
    def test_an_empty_stage_list_means_everything(self):
        assert CrmSync(CrmConfig(provider="webhook", sync_stages=[])).wants("queued")

    def test_opting_out_is_synced_by_default(self):
        # The one stage where the CRM not knowing eventually means someone
        # emails them anyway.
        assert CrmSync(CrmConfig(provider="webhook")).wants("opted_out")

    def test_a_disabled_sync_wants_nothing(self):
        assert not CrmSync(CrmConfig(provider="none")).wants("won")


class TestPayload:
    def _payload(self, profile=None):
        prospect = Prospect(
            id="p1",
            first_name="Lena",
            last_name="Fischer",
            title="Head of Procurement",
            email="lena@northwind.test",
            score=72,
        )
        company = Company(id="c1", name="Northwind", domain="northwind.test")
        event = {
            "id": "e1",
            "from_stage": "replied",
            "to_stage": "meeting",
            "reason": "Thursday 3pm",
            "actor": "human",
            "created_at": "2026-08-21T09:14:03",
        }
        return build_payload(event, prospect, company, profile)

    def test_the_documented_fields_are_all_there(self):
        payload = self._payload()
        assert payload["event"] == "stage_change"
        assert payload["from_stage"] == "replied"
        assert payload["to_stage"] == "meeting"
        assert payload["contact"]["email"] == "lena@northwind.test"
        assert payload["company"]["domain"] == "northwind.test"

    def test_no_brief_means_no_brief_key(self):
        assert "brief" not in self._payload()

    def test_the_brief_carries_the_do_not_say_list(self):
        payload = self._payload({
            "fit_score": 6,
            "confidence": "low",
            "company_snapshot": "Regional freight forwarder",
            "buying_signals": [{"signal": "Opened a second warehouse"}],
            "avoid": ["Do not claim they are struggling"],
        })
        assert payload["brief"]["fit_score"] == 6
        assert payload["brief"]["buying_signals"] == ["Opened a second warehouse"]
        assert payload["brief"]["avoid"] == ["Do not claim they are struggling"]

    def test_a_missing_company_record_still_names_the_company(self):
        prospect = Prospect(id="p1", first_name="A", last_name="B", company="Northwind")
        payload = build_payload({"id": "e1"}, prospect, None, None)
        assert payload["company"]["name"] == "Northwind"
