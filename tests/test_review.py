"""Tests for the human review workflow.

The product's core promise is that nothing is sent without a person saying
so, so these tests are about the *refusals* as much as the happy path.
"""

import os
import tempfile

import aiosqlite
import pytest
import pytest_asyncio

from openvz_leads.config import LeadsConfig
from openvz_leads.models.campaign import Campaign, EmailStep
from openvz_leads.models.prospect import Prospect
from openvz_leads.state import MIGRATIONS, SENDABLE_CAMPAIGN_STATUS, StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


def _campaign(status: str = "pending_review", name: str = "batch") -> Campaign:
    return Campaign(
        id="",
        name=name,
        sequence=[EmailStep(step=1, subject="hi", body="hello")],
        status=status,
    )


@pytest.mark.asyncio
async def test_approve_moves_campaign_to_sendable(state):
    cid = await state.add_campaign(_campaign())

    assert await state.review_campaign(cid, approved=True, note="looks good", reviewer="v")

    campaign = await state.get_campaign(cid)
    assert campaign.status == SENDABLE_CAMPAIGN_STATUS
    assert campaign.review_note == "looks good"
    assert campaign.reviewed_by == "v"
    assert campaign.reviewed_at is not None


@pytest.mark.asyncio
async def test_reject_is_not_sendable(state):
    cid = await state.add_campaign(_campaign())
    assert await state.review_campaign(cid, approved=False, note="wrong angle")

    campaign = await state.get_campaign(cid)
    assert campaign.status == "rejected"
    assert not await state.get_campaigns_by_status(SENDABLE_CAMPAIGN_STATUS)


@pytest.mark.asyncio
async def test_cannot_re_decide_an_already_decided_campaign(state):
    """A second click must not resurrect a campaign that already shipped."""
    cid = await state.add_campaign(_campaign())
    assert await state.review_campaign(cid, approved=True)

    # Simulate the Sender deploying it.
    await state.update_campaign(cid, status="active")

    assert not await state.review_campaign(cid, approved=False)
    assert (await state.get_campaign(cid)).status == "active"


@pytest.mark.asyncio
async def test_review_of_unknown_campaign_is_a_no_op(state):
    assert not await state.review_campaign("does-not-exist", approved=True)


@pytest.mark.asyncio
async def test_review_note_survives_non_ascii(state):
    cid = await state.add_campaign(_campaign())
    await state.review_campaign(cid, approved=True, note="第二封改了开头")
    assert (await state.get_campaign(cid)).review_note == "第二封改了开头"


@pytest.mark.asyncio
async def test_summary_separates_pending_from_approved(state):
    await state.add_campaign(_campaign("pending_review", "a"))
    await state.add_campaign(_campaign("pending_review", "b"))
    approved = await state.add_campaign(_campaign("pending_review", "c"))
    await state.review_campaign(approved, approved=True)

    summary = await state.get_state_summary()
    assert summary["pending_review"] == 2
    assert summary["approved_campaigns"] == 1


@pytest.mark.asyncio
async def test_legacy_draft_campaigns_migrate_into_the_review_queue():
    """Upgrading must not make an old 'draft' campaign silently sendable."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "legacy.db")

        # Build a pre-review-queue (v2) database by hand.
        async with aiosqlite.connect(db_path) as db:
            for version, script in enumerate(MIGRATIONS[:2], start=1):
                await db.executescript(script)
                await db.execute(f"PRAGMA user_version = {version}")
            await db.execute(
                "INSERT INTO campaigns (id, name, status) VALUES ('c1', 'legacy', 'draft')"
            )
            await db.execute(
                "INSERT INTO campaigns (id, name, status) VALUES ('c2', 'live', 'active')"
            )
            await db.commit()

        sm = StateManager(db_path)
        await sm.init_db()

        counts = await sm.count_campaigns_by_status()
        assert counts == {"pending_review": 1, "active": 1}

        # And the upgrade is idempotent.
        await sm.init_db()
        assert await sm.count_campaigns_by_status() == counts


class TestSendingIsOptional:
    def test_sending_is_off_by_default(self):
        config = LeadsConfig(
            persona={
                "name": "A", "company": "B", "role": "C",
                "email": "a@b.com", "linkedin": "x", "tone": "y",
            },
            product={
                "name": "P", "description": "D", "pricing": "$1",
                "key_benefits": ["b"], "objection_responses": {},
            },
            icp={
                "industries": ["SaaS"], "company_size": "1-10",
                "titles": ["CEO"], "geography": ["US"],
            },
        )
        assert config.channels.email.provider == "none"
        assert config.channels.email.sending_enabled is False
        assert config.review.require_approval is True

    def test_provider_none_disables_sending_even_when_channel_enabled(self):
        from openvz_leads.config import EmailChannelConfig

        assert EmailChannelConfig(enabled=True, provider="none").sending_enabled is False
        assert EmailChannelConfig(enabled=True, provider="instantly").sending_enabled is True
        assert EmailChannelConfig(enabled=False, provider="instantly").sending_enabled is False

    def test_unknown_provider_is_rejected(self):
        from pydantic import ValidationError

        from openvz_leads.config import EmailChannelConfig

        with pytest.raises(ValidationError):
            EmailChannelConfig(provider="mailchimp")


@pytest.mark.asyncio
async def test_sender_ignores_campaigns_that_are_not_approved(state):
    """The Sender must only ever see 'approved'. Everything else is invisible."""
    for status in ("draft", "pending_review", "rejected", "active", "failed"):
        await state.add_campaign(_campaign(status, f"c-{status}"))

    assert await state.get_campaigns_by_status(SENDABLE_CAMPAIGN_STATUS) == []

    approved = await state.add_campaign(_campaign("pending_review", "ready"))
    await state.review_campaign(approved, approved=True)

    sendable = await state.get_campaigns_by_status(SENDABLE_CAMPAIGN_STATUS)
    assert [c.name for c in sendable] == ["ready"]


@pytest.mark.asyncio
async def test_prospect_profile_round_trip(state):
    pid = await state.add_prospect(
        Prospect(first_name="Jane", last_name="Doe", title="CTO",
                 email="jane@acme.com", score=8)
    )
    assert [p.id for p in await state.get_prospects_needing_profile(min_score=5)] == [pid]

    await state.save_prospect_profile(pid, {"fit_score": 9, "confidence": "medium"})

    assert await state.get_prospects_needing_profile(min_score=5) == []
    profiled = await state.get_profiled_prospects()
    assert len(profiled) == 1
    assert profiled[0].profile()["fit_score"] == 9
    assert profiled[0].profiled_at is not None


@pytest.mark.asyncio
async def test_min_score_keeps_low_value_prospects_out_of_analysis(state):
    await state.add_prospect(
        Prospect(first_name="Low", last_name="Score", title="Intern",
                 email="low@acme.com", score=2)
    )
    assert await state.get_prospects_needing_profile(min_score=5) == []
    assert len(await state.get_prospects_needing_profile(min_score=0)) == 1


@pytest.mark.asyncio
async def test_closed_prospects_are_never_queued_for_analysis(state):
    pid = await state.add_prospect(
        Prospect(first_name="Gone", last_name="Away", title="CEO",
                 email="gone@acme.com", score=9)
    )
    await state.update_prospect_status(pid, "opted_out")
    assert await state.get_prospects_needing_profile(min_score=0) == []


class TestPromptTemplating:
    """load_prompt's unfilled-variable check must not cry wolf."""

    def test_merge_variables_do_not_trigger_a_warning(self, caplog, tmp_path):
        from openvz_leads.brain import Brain
        from openvz_leads.state import StateManager

        brain = Brain(StateManager(str(tmp_path / "t.db")))
        with caplog.at_level("WARNING"):
            filled = brain.load_prompt(
                "writer",
                product_name="P", product_description="D", product_benefits="B",
                product_pricing="$1", persona_name="N", persona_company="C",
                persona_role="R", persona_tone="T",
            )
        assert "{{first_name}}" in filled, "merge variables must survive templating"
        assert "unfilled variables" not in caplog.text

    def test_a_genuine_typo_still_warns(self, caplog, tmp_path, monkeypatch):
        """The check still has to catch the case it exists for."""
        from openvz_leads.brain import Brain
        from openvz_leads.state import StateManager

        # Point the whole workspace at a temp dir — the same override a
        # frozen build and a multi-workspace install use.
        monkeypatch.setenv("OPENVZ_LEADS_HOME", str(tmp_path))
        prompts = tmp_path / "prompts"
        prompts.mkdir()
        (prompts / "typo.md").write_text("Sell {{prodcut_name}} to {{first_name}}.")

        brain = Brain(StateManager(str(tmp_path / "t.db")))
        with caplog.at_level("WARNING"):
            brain.load_prompt("typo", product_name="P")

        assert "prodcut_name" in caplog.text
        assert "first_name" not in caplog.text


class TestWorkspaceResolution:
    """paths.workspace() is what makes the app installable anywhere."""

    def test_env_override_wins(self, tmp_path, monkeypatch):
        from openvz_leads import paths

        monkeypatch.setenv("OPENVZ_LEADS_HOME", str(tmp_path))
        assert paths.workspace() == tmp_path
        assert paths.prompts_dir() == tmp_path / "prompts"
        assert paths.config_file() == tmp_path / "openvz-leads.yaml"

    def test_checkout_resolves_beside_the_package(self, monkeypatch):
        """Unpacked-archive users must see no behaviour change."""
        from pathlib import Path

        from openvz_leads import paths

        monkeypatch.delenv("OPENVZ_LEADS_HOME", raising=False)
        expected = Path(paths.__file__).resolve().parent.parent
        assert paths.workspace() == expected

    def test_seeding_copies_editable_files_without_clobbering(self, tmp_path, monkeypatch):
        from openvz_leads import paths

        source = tmp_path / "bundle"
        (source / "prompts").mkdir(parents=True)
        (source / "skills").mkdir()
        (source / "prompts" / "writer.md").write_text("shipped")
        (source / "prompts" / "scout.md").write_text("shipped")
        (source / "openvz-leads.yaml").write_text("shipped")

        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "prompts").mkdir()
        (ws / "prompts" / "writer.md").write_text("MINE")

        monkeypatch.setenv("OPENVZ_LEADS_HOME", str(ws))
        monkeypatch.setattr(paths, "bundle_root", lambda: source)

        paths.ensure_workspace()

        # An edited prompt survives; a new one arrives.
        assert (ws / "prompts" / "writer.md").read_text() == "MINE"
        assert (ws / "prompts" / "scout.md").read_text() == "shipped"
        assert (ws / "openvz-leads.yaml").read_text() == "shipped"
        assert (ws / "data").is_dir()
