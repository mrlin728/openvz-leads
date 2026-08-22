"""The Insights tab's data — the numbers a user makes send/stop decisions on."""

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from openvz_leads import dashboard
from openvz_leads.state import StateManager


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "leads.db"
    monkeypatch.setattr(dashboard, "DB_PATH", db)
    return TestClient(dashboard.app), db


async def _seed(db_path, rows, events=(), intents=()):
    state = StateManager(str(db_path))
    await state.init_db()
    async with state._connect() as db:
        for i, status in enumerate(rows):
            await db.execute(
                "INSERT INTO prospects (id, email, status) VALUES (?,?,?)",
                (f"p{i}", f"p{i}@example.com", status),
            )
        for prospect, to_stage in events:
            await db.execute(
                "INSERT INTO stage_events (id, prospect_id, to_stage) VALUES (?,?,?)",
                (str(uuid.uuid4()), prospect, to_stage),
            )
        for i, intent in enumerate(intents):
            await db.execute(
                "INSERT INTO conversations (id, prospect_id, intent) VALUES (?,?,?)",
                (f"c{i}", f"p{i}", intent),
            )
        await db.commit()


def test_empty_install_returns_no_data_not_zero(client):
    """A rate with no denominator must be null, never 0.0.

    0% reply rate is a claim about a campaign that went badly. No data is a
    different thing, and the two must not print the same.
    """
    c, _ = client
    body = c.get("/api/analytics").json()
    assert body["rates"]["reply"] is None
    assert body["rates"]["opt_out"] is None
    assert body["totals"]["contacted"] == 0
    assert body["insights"] == []


@pytest.mark.asyncio
async def test_reached_counts_history_not_current_stage(client):
    """Someone who replied was contacted; the funnel has to remember that."""
    c, db = client
    await _seed(
        db,
        rows=["replied", "replied", "contacted"],
        events=[
            ("p0", "contacted"), ("p0", "replied"),
            ("p1", "contacted"), ("p1", "replied"),
            ("p2", "contacted"),
        ],
    )
    body = c.get("/api/analytics").json()
    reached = {f["stage"]: f["reached"] for f in body["funnel"]}
    assert reached["contacted"] == 3, "replied prospects dropped out of contacted"
    assert reached["replied"] == 2
    assert body["rates"]["reply"] == pytest.approx(66.7, abs=0.1)


@pytest.mark.asyncio
async def test_opting_out_early_does_not_inflate_contacted(client):
    """`opted_out` is reachable from anywhere, so it is not below `contacted`.

    Summing the tail of STAGES would count someone who opted out while still
    queued as having been contacted, and quietly inflate every rate under it.
    """
    c, db = client
    await _seed(
        db,
        rows=["opted_out", "contacted"],
        events=[("p0", "queued"), ("p0", "opted_out"), ("p1", "contacted")],
    )
    body = c.get("/api/analytics").json()
    reached = {f["stage"]: f["reached"] for f in body["funnel"]}
    assert reached["contacted"] == 1, "an early opt-out was counted as contacted"


@pytest.mark.asyncio
async def test_prospect_with_no_stage_history_still_counts(client):
    """Rows that predate stage_events have a status and no events."""
    c, db = client
    await _seed(db, rows=["contacted", "contacted"], events=[])
    body = c.get("/api/analytics").json()
    reached = {f["stage"]: f["reached"] for f in body["funnel"]}
    assert reached["contacted"] == 2


@pytest.mark.asyncio
async def test_insights_come_from_the_analysts_file(client, tmp_path):
    c, db = client
    await _seed(db, rows=["new"])
    (db.parent / "analytics.json").write_text(
        json.dumps({
            "generated_at": "2026-03-01T10:00:00",
            "insights": ["ACTION: do the thing", "", "KEEP: this bit works"],
        }),
        encoding="utf-8",
    )
    body = c.get("/api/analytics").json()
    assert body["insights"] == ["ACTION: do the thing", "KEEP: this bit works"]
    assert body["insights_generated_at"] == "2026-03-01T10:00:00"


@pytest.mark.asyncio
async def test_unreadable_analytics_file_does_not_break_the_tab(client):
    c, db = client
    await _seed(db, rows=["new"])
    (db.parent / "analytics.json").write_text("{ not json", encoding="utf-8")
    r = c.get("/api/analytics")
    assert r.status_code == 200
    assert r.json()["insights"] == []


@pytest.mark.asyncio
async def test_intents_feed_the_reply_breakdown(client):
    c, db = client
    await _seed(
        db,
        rows=["replied", "replied", "opted_out"],
        events=[("p0", "contacted"), ("p1", "contacted"), ("p2", "contacted")],
        intents=["interested", "objection", "unsubscribe"],
    )
    body = c.get("/api/analytics").json()
    assert body["intents"] == {"interested": 1, "objection": 1, "unsubscribe": 1}
    # opted_out prospect + the unsubscribe reply, over 3 contacted.
    assert body["rates"]["opt_out"] == pytest.approx(66.7, abs=0.1)
