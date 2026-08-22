"""Search, filter, sort and paging on the Contacts and Companies lists."""

import pytest
from fastapi.testclient import TestClient

from openvz_leads import dashboard
from openvz_leads.state import StateManager


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "leads.db"
    monkeypatch.setattr(dashboard, "DB_PATH", db)
    return TestClient(dashboard.app), db


async def _seed_prospects(db_path, people):
    state = StateManager(str(db_path))
    await state.init_db()
    async with state._connect() as db:
        for i, (first, last, company, title, status, score) in enumerate(people):
            await db.execute(
                """INSERT INTO prospects
                   (id, first_name, last_name, email, company, title, status, score)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (f"p{i}", first, last, f"{first.lower()}{i}@{company.lower()}.com",
                 company, title, status, score),
            )
        await db.commit()


PEOPLE = [
    ("Maria", "Alvarez", "Brightsmile", "Practice Owner", "new", 9),
    ("James", "Chen", "Coastal", "Office Manager", "contacted", 6),
    ("Priya", "Patel", "Redwood", "Clinical Director", "contacted", 8),
    ("Daniel", "Okafor", "Brightsmile", "Managing Partner", "replied", 7),
    ("Aisha", "Silva", "Vista", "Practice Owner", "won", 10),
]


def test_empty_db_returns_an_envelope_not_a_bare_list(client):
    c, _ = client
    body = c.get("/api/prospects").json()
    assert body == {"rows": [], "total": 0, "limit": 50, "offset": 0}


@pytest.mark.asyncio
async def test_total_counts_matches_not_the_page(client):
    """Without total you cannot tell 50 rows from the first 50 of 3,000."""
    c, db = client
    await _seed_prospects(db, PEOPLE)
    body = c.get("/api/prospects?limit=2").json()
    assert len(body["rows"]) == 2
    assert body["total"] == 5


@pytest.mark.asyncio
async def test_search_spans_name_email_company_and_title(client):
    c, db = client
    await _seed_prospects(db, PEOPLE)
    for term, expected in [
        ("Chen", 1),            # surname
        ("Brightsmile", 2),     # company
        ("Practice Owner", 2),  # title
        ("priya2@", 1),         # email
    ]:
        body = c.get(f"/api/prospects?q={term}").json()
        assert body["total"] == expected, f"{term!r} matched {body['total']}, want {expected}"


@pytest.mark.asyncio
async def test_wildcards_in_a_search_term_are_literal(client):
    """Searching for a literal % must not match every row."""
    c, db = client
    await _seed_prospects(db, PEOPLE + [("Zed", "100%", "Odd", "Owner", "new", 1)])
    assert c.get("/api/prospects?q=%25").json()["total"] == 1
    assert c.get("/api/prospects?q=_").json()["total"] == 0


@pytest.mark.asyncio
async def test_stage_filter_only_accepts_a_real_stage(client):
    """A hand-edited URL must not return an empty page that reads as 'none'."""
    c, db = client
    await _seed_prospects(db, PEOPLE)
    assert c.get("/api/prospects?status=contacted").json()["total"] == 2
    # Not a stage — the filter is ignored rather than matching nothing.
    assert c.get("/api/prospects?status=nonsense").json()["total"] == 5


@pytest.mark.asyncio
async def test_sort_column_cannot_come_from_the_client(client):
    """Anything outside the whitelist falls back; no SQL is built from input."""
    c, db = client
    await _seed_prospects(db, PEOPLE)
    r = c.get("/api/prospects?sort=score,(SELECT+1)&order=asc")
    assert r.status_code == 200
    assert r.json()["total"] == 5

    ordered = c.get("/api/prospects?sort=score&order=desc").json()["rows"]
    assert [p["score"] for p in ordered] == [10, 9, 8, 7, 6]


@pytest.mark.asyncio
async def test_paging_walks_the_whole_set_without_repeats(client):
    c, db = client
    await _seed_prospects(db, PEOPLE)
    seen = []
    for offset in (0, 2, 4):
        seen += [p["id"] for p in
                 c.get(f"/api/prospects?limit=2&offset={offset}&sort=score&order=asc")
                  .json()["rows"]]
    assert len(seen) == 5
    assert len(set(seen)) == 5


@pytest.mark.asyncio
async def test_limit_is_capped(client):
    """One page must not be allowed to hand the browser the whole database."""
    c, db = client
    await _seed_prospects(db, PEOPLE)
    assert c.get("/api/prospects?limit=99999").json()["limit"] == dashboard.PAGE_MAX


@pytest.mark.asyncio
async def test_junk_paging_values_fall_back_instead_of_500ing(client):
    c, db = client
    await _seed_prospects(db, PEOPLE)
    body = c.get("/api/prospects?limit=abc&offset=-40").json()
    assert body["limit"] == 50 and body["offset"] == 0
    assert body["total"] == 5


@pytest.mark.asyncio
async def test_companies_search_and_contact_counts(client):
    c, db = client
    await _seed_prospects(db, PEOPLE)
    state = StateManager(str(db))
    async with state._connect() as conn:
        await conn.execute(
            "INSERT INTO companies (id, name, domain, industry, location)"
            " VALUES ('c1','Brightsmile Dental','brightsmile.com','Dental','California')",
        )
        await conn.execute("UPDATE prospects SET company_id = 'c1' WHERE company = 'Brightsmile'")
        await conn.commit()

    body = c.get("/api/companies?q=dental").json()
    assert body["total"] == 1
    assert body["rows"][0]["contact_count"] == 2
    assert c.get("/api/companies?q=california").json()["total"] == 1
    assert c.get("/api/companies?q=nothinghere").json()["total"] == 0
