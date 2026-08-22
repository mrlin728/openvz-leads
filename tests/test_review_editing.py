"""Editing a draft before approving it — and the guards on who may."""

import json

import pytest
from fastapi.testclient import TestClient

from openvz_leads import dashboard
from openvz_leads.models.campaign import Campaign
from openvz_leads.state import StateManager

SEQUENCE = [
    {"step": 1, "delay_days": 0, "subject": "first", "body": "written by the Writer"},
    {"step": 2, "delay_days": 3, "subject": "second", "body": "a follow-up"},
    {"step": 3, "delay_days": 7, "subject": "third", "body": "last one"},
]


async def _campaign(db_path, status="pending_review", prospect_ids=()):
    state = StateManager(str(db_path))
    await state.init_db()
    campaign = Campaign(
        id="camp-1", name="A campaign", sequence=list(SEQUENCE),
        prospect_ids=list(prospect_ids), status=status,
    )
    await state.add_campaign(campaign)
    return state


async def _stored(state, campaign_id="camp-1"):
    async with state._connect() as db:
        async with db.execute(
            "SELECT sequence_json FROM campaigns WHERE id = ?", (campaign_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return json.loads(row[0])


@pytest.mark.asyncio
async def test_edit_replaces_subject_body_and_delay(tmp_path):
    state = await _campaign(tmp_path / "leads.db")
    result = await state.edit_pending_sequence("camp-1", [
        {"step": 1, "subject": "a person wrote this", "body": "and this", "delay_days": 1},
    ])
    assert result[0]["subject"] == "a person wrote this"
    assert result[0]["body"] == "and this"
    assert result[0]["delay_days"] == 1
    # Untouched steps survive intact.
    assert result[1]["subject"] == "second"
    assert await _stored(state) == result


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["approved", "rejected", "active", "failed"])
async def test_a_decided_campaign_cannot_be_rewritten(tmp_path, status):
    """Once approved, the copy is what a person signed off on.

    Once active it may already be in someone's inbox. Rewriting either would
    leave the record no longer saying what was actually sent.
    """
    state = await _campaign(tmp_path / f"{status}.db", status=status)
    assert await state.edit_pending_sequence("camp-1", [
        {"step": 1, "subject": "sneaky", "body": "rewrite"},
    ]) is None
    assert (await _stored(state))[0]["subject"] == "first"


@pytest.mark.asyncio
async def test_a_draft_is_editable_too(tmp_path):
    state = await _campaign(tmp_path / "leads.db", status="draft")
    assert await state.edit_pending_sequence("camp-1", [{"step": 1, "subject": "ok"}]) is not None


@pytest.mark.asyncio
async def test_an_edit_cannot_add_a_step_nobody_reviewed(tmp_path):
    state = await _campaign(tmp_path / "leads.db")
    result = await state.edit_pending_sequence("camp-1", [
        {"step": 4, "subject": "a fourth email", "body": "that nobody approved"},
    ])
    assert len(result) == 3
    assert all(step["subject"] != "a fourth email" for step in result)


@pytest.mark.asyncio
async def test_a_negative_delay_is_clamped(tmp_path):
    """A follow-up must not be scheduled before the email it follows up on."""
    state = await _campaign(tmp_path / "leads.db")
    result = await state.edit_pending_sequence("camp-1", [{"step": 2, "delay_days": -5}])
    assert result[1]["delay_days"] == 0


@pytest.mark.asyncio
async def test_a_junk_delay_leaves_the_old_one(tmp_path):
    state = await _campaign(tmp_path / "leads.db")
    result = await state.edit_pending_sequence("camp-1", [{"step": 2, "delay_days": "soon"}])
    assert result[1]["delay_days"] == 3


@pytest.mark.asyncio
async def test_unknown_campaign_returns_none(tmp_path):
    state = await _campaign(tmp_path / "leads.db")
    assert await state.edit_pending_sequence("nope", [{"step": 1, "subject": "x"}]) is None


@pytest.mark.asyncio
async def test_non_ascii_copy_survives_the_round_trip(tmp_path):
    state = await _campaign(tmp_path / "leads.db")
    result = await state.edit_pending_sequence("camp-1", [
        {"step": 1, "subject": "你的预约页面", "body": "八次点击才能选到时间。"},
    ])
    assert result[0]["subject"] == "你的预约页面"
    assert (await _stored(state))[0]["body"] == "八次点击才能选到时间。"


# ── The route ──


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "leads.db"
    monkeypatch.setattr(dashboard, "DB_PATH", db)
    return TestClient(dashboard.app), db


@pytest.mark.asyncio
async def test_route_saves_and_returns_the_stored_sequence(client):
    c, db = client
    await _campaign(db)
    r = c.post("/api/review/camp-1/sequence",
               json={"sequence": [{"step": 1, "subject": "edited"}]})
    assert r.status_code == 200
    assert r.json()["sequence"][0]["subject"] == "edited"


@pytest.mark.asyncio
async def test_route_refuses_a_campaign_that_is_no_longer_open(client):
    c, db = client
    await _campaign(db, status="approved")
    r = c.post("/api/review/camp-1/sequence",
               json={"sequence": [{"step": 1, "subject": "edited"}]})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_route_rejects_a_body_without_a_sequence(client):
    c, db = client
    await _campaign(db)
    assert c.post("/api/review/camp-1/sequence", json={}).status_code == 400
    assert c.post("/api/review/camp-1/sequence", json={"sequence": "no"}).status_code == 400


# ── The brief's checks, alongside the draft ──


@pytest.mark.asyncio
async def test_review_payload_carries_the_do_not_say_list(client):
    c, db = client
    state = await _campaign(db, prospect_ids=["p1", "p2"])
    briefs = {
        "p1": {"company": "Acme Dental", "confidence": "medium",
               "avoid": ["Do not name a competitor."],
               "evidence_gaps": ["No pricing page."]},
        "p2": {"company": "Baker Dental", "confidence": "low",
               "avoid": ["Do not name a competitor."],
               "evidence_gaps": ["Staff count unknown."]},
    }
    async with state._connect() as conn:
        for pid, brief in briefs.items():
            await conn.execute(
                "INSERT INTO prospects (id, email, company, profile_json) VALUES (?,?,?,?)",
                (pid, f"{pid}@example.com", brief["company"], json.dumps(brief)),
            )
        await conn.commit()

    checks = c.get("/api/review/pending").json()[0]["checks"]

    # Grouped by the warning, not repeated per account.
    assert len(checks["avoid"]) == 1
    assert checks["avoid"][0]["accounts"] == ["Acme Dental", "Baker Dental"]
    # Most widely applicable first.
    assert [g["text"] for g in checks["evidence_gaps"]][0] in (
        "No pricing page.", "Staff count unknown.")
    assert len(checks["evidence_gaps"]) == 2
    assert checks["low_confidence"] == 1
    assert checks["briefed"] == 2


@pytest.mark.asyncio
async def test_unprofiled_recipients_leave_the_checks_empty(client):
    c, db = client
    state = await _campaign(db, prospect_ids=["p1"])
    async with state._connect() as conn:
        await conn.execute(
            "INSERT INTO prospects (id, email) VALUES ('p1','p1@example.com')")
        await conn.commit()
    checks = c.get("/api/review/pending").json()[0]["checks"]
    assert checks == {"avoid": [], "evidence_gaps": [], "low_confidence": 0, "briefed": 0}


@pytest.mark.asyncio
async def test_a_corrupt_brief_does_not_break_the_queue(client):
    c, db = client
    state = await _campaign(db, prospect_ids=["p1"])
    async with state._connect() as conn:
        await conn.execute(
            "INSERT INTO prospects (id, email, profile_json) VALUES ('p1','p1@x.com','{ bad')")
        await conn.commit()
    r = c.get("/api/review/pending")
    assert r.status_code == 200
    assert r.json()[0]["checks"]["avoid"] == []
