"""Tests for account analysis parsing and the export formats.

The Profiler's output is LLM-generated, so `AccountProfile.from_raw` has to
survive anything: wrong types, strings where objects were asked for, missing
sections. It must never raise, and it must never turn junk into a
confident-looking brief.
"""

import csv
import json
import os
import tempfile

import pytest
import pytest_asyncio

from openvz_leads.exporter import ExportError, Exporter
from openvz_leads.models.campaign import Campaign, EmailStep
from openvz_leads.models.profile import AccountProfile
from openvz_leads.models.prospect import Prospect
from openvz_leads.state import StateManager

FULL_RAW = {
    "fit_score": 8,
    "fit_reasons": ["Right title", "Size matches"],
    "risks": ["May already have a vendor"],
    "company_snapshot": {
        "what_they_do": "Builds robot arms",
        "market": "APAC OEMs",
        "likely_size": "80-150",
        "positioning": "Cost-efficient automation",
    },
    "buying_signals": [
        {"signal": "Hiring in the EU", "evidence": "careers page", "strength": "high"}
    ],
    "pain_hypotheses": ["EU certification overhead"],
    "decision_chain": {
        "this_contact_role": "economic_buyer",
        "likely_economic_buyer": "COO",
        "likely_champion": "Head of Ops",
        "likely_blocker": "Finance",
    },
    "opening_angles": [{"angle": "EU expansion", "why_it_lands": "They just started hiring"}],
    "avoid": ["Don't assume they're a startup"],
    "confidence": "medium",
    "evidence_gaps": ["No revenue data"],
}


class TestAccountProfileParsing:
    def test_parses_a_complete_response(self):
        profile = AccountProfile.from_raw(FULL_RAW)
        assert profile.fit_score == 8
        assert profile.confidence == "medium"
        assert profile.buying_signals[0].strength == "high"
        assert profile.decision_chain.this_contact_role == "economic_buyer"

    def test_returns_none_for_non_dict_input(self):
        for junk in (None, [], "a string", 42):
            assert AccountProfile.from_raw(junk) is None

    def test_returns_none_when_there_is_no_substance(self):
        """A brief with only a score is a failure, not a finding — surfacing
        it would read as 'we looked and found nothing'."""
        assert AccountProfile.from_raw({"fit_score": 7, "confidence": "high"}) is None

    def test_score_is_clamped_to_the_documented_range(self):
        for raw_score, expected in ((99, 10), (-4, 0), ("6", 6), ("nonsense", 0), (None, 0)):
            profile = AccountProfile.from_raw({**FULL_RAW, "fit_score": raw_score})
            assert profile.fit_score == expected

    def test_unknown_enum_values_fall_back_instead_of_raising(self):
        profile = AccountProfile.from_raw({
            **FULL_RAW,
            "confidence": "extremely sure",
            "buying_signals": [{"signal": "x", "strength": "CRITICAL"}],
            "decision_chain": {"this_contact_role": "the boss"},
        })
        assert profile.confidence == "low"
        assert profile.buying_signals[0].strength == "low"
        assert profile.decision_chain.this_contact_role == "unknown"

    def test_accepts_bare_strings_where_objects_were_requested(self):
        profile = AccountProfile.from_raw({
            **FULL_RAW,
            "buying_signals": ["Hiring aggressively"],
            "opening_angles": ["Their new warehouse"],
        })
        assert profile.buying_signals[0].signal == "Hiring aggressively"
        assert profile.buying_signals[0].evidence == ""
        assert profile.opening_angles[0].angle == "Their new warehouse"

    def test_malformed_sections_are_dropped_not_fatal(self):
        profile = AccountProfile.from_raw({
            **FULL_RAW,
            "company_snapshot": "not an object",
            "decision_chain": ["also wrong"],
            "risks": None,
        })
        assert profile is not None
        assert profile.company_snapshot.what_they_do == ""
        assert profile.decision_chain.this_contact_role == "unknown"
        assert profile.risks == []

    def test_lists_are_bounded(self):
        profile = AccountProfile.from_raw({
            **FULL_RAW,
            "fit_reasons": [f"reason {i}" for i in range(50)],
            "buying_signals": [{"signal": f"s{i}"} for i in range(50)],
        })
        assert len(profile.fit_reasons) == 5
        assert len(profile.buying_signals) == 6

    def test_empty_strings_are_stripped_from_lists(self):
        profile = AccountProfile.from_raw({**FULL_RAW, "fit_reasons": ["ok", "", "  ", None]})
        assert profile.fit_reasons == ["ok"]

    def test_brief_includes_the_avoid_list(self):
        """The Writer reads brief() — losing 'avoid' there would let it say
        the one thing the analysis flagged as fatal."""
        brief = AccountProfile.from_raw(FULL_RAW).brief()
        assert "Do NOT say" in brief
        assert "startup" in brief
        assert "Fit 8/10" in brief


@pytest_asyncio.fixture
async def populated():
    """A state manager with one profiled prospect and one campaign."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        pid = await sm.add_prospect(
            Prospect(first_name="张", last_name="伟", title="采购总监",
                     email="wei@acme.com", company="Acme 机器人", score=82)
        )
        await sm.save_prospect_profile(pid, AccountProfile.from_raw(FULL_RAW).model_dump())
        await sm.add_campaign(Campaign(
            id="", name="manufacturing-outreach", prospect_ids=[pid],
            status="pending_review",
            sequence=[
                EmailStep(step=1, subject="eu certification",
                          body="Hi {{first_name}},\n\nA line with, a comma."),
                EmailStep(step=2, subject="follow up", body="Circling back.", delay_days=3),
            ],
        ))
        yield sm, tmpdir


class TestExport:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("dataset,fmt", [
        ("leads", "csv"), ("leads", "markdown"), ("leads", "json"),
        ("profiles", "markdown"), ("profiles", "json"),
        ("emails", "csv"), ("emails", "markdown"), ("emails", "json"),
    ])
    async def test_every_supported_combination_writes_a_file(self, populated, dataset, fmt):
        state, tmpdir = populated
        path = await Exporter(state).export(dataset, fmt)
        assert path.exists()
        assert path.stat().st_size > 0

    @pytest.mark.asyncio
    async def test_profiles_as_csv_is_refused_with_an_explanation(self, populated):
        state, _ = populated
        with pytest.raises(ExportError, match="flattens badly into CSV"):
            await Exporter(state).export("profiles", "csv")

    @pytest.mark.asyncio
    async def test_unknown_dataset_and_format_are_refused(self, populated):
        state, _ = populated
        with pytest.raises(ExportError, match="Unknown dataset"):
            await Exporter(state).export("everything", "csv")
        with pytest.raises(ExportError, match="Unknown format"):
            await Exporter(state).export("leads", "xlsx")

    @pytest.mark.asyncio
    async def test_exporting_nothing_says_so_instead_of_writing_an_empty_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = StateManager(os.path.join(tmpdir, "empty.db"))
            await state.init_db()
            with pytest.raises(ExportError, match="Nothing to export"):
                await Exporter(state).export("leads", "csv")

    @pytest.mark.asyncio
    async def test_email_csv_has_one_row_per_step_with_bodies_intact(self, populated):
        state, tmpdir = populated
        path = await Exporter(state).export("emails", "csv",
                                            out_path=os.path.join(tmpdir, "e.csv"))
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 2
        assert rows[0]["subject"] == "eu certification"
        # Newlines and commas inside a body must survive CSV quoting.
        assert "\n" in rows[0]["body"]
        assert "a comma" in rows[0]["body"]
        assert rows[1]["delay_days"] == "3"

    @pytest.mark.asyncio
    async def test_csv_is_written_with_a_bom_so_excel_reads_utf8(self, populated):
        state, tmpdir = populated
        path = await Exporter(state).export("leads", "csv",
                                            out_path=os.path.join(tmpdir, "l.csv"))
        assert path.read_bytes().startswith(b"\xef\xbb\xbf")
        assert "张" in path.read_text(encoding="utf-8-sig")

    @pytest.mark.asyncio
    async def test_profile_markdown_surfaces_the_warning_sections(self, populated):
        state, tmpdir = populated
        path = await Exporter(state).export("profiles", "markdown",
                                            out_path=os.path.join(tmpdir, "p.md"))
        text = path.read_text(encoding="utf-8")
        assert "Do not say" in text
        assert "Evidence gaps" in text
        assert "confidence" in text
        assert "Fit **8/10**" in text

    @pytest.mark.asyncio
    async def test_json_export_is_valid_and_carries_its_metadata(self, populated):
        state, tmpdir = populated
        path = await Exporter(state).export("profiles", "json",
                                            out_path=os.path.join(tmpdir, "p.json"))
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["dataset"] == "profiles"
        assert payload["count"] == len(payload["records"]) == 1
        assert payload["records"][0]["profile"]["fit_score"] == 8
        # ensure_ascii=False keeps CJK readable rather than \uXXXX-escaped.
        assert "张" in path.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_leads_export_marks_which_accounts_were_analysed(self, populated):
        state, tmpdir = populated
        await state.add_prospect(
            Prospect(first_name="Un", last_name="Analysed", title="CTO",
                     email="un@x.com", score=5)
        )
        path = await Exporter(state).export("leads", "csv",
                                            out_path=os.path.join(tmpdir, "l2.csv"))
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = {r["email"]: r for r in csv.DictReader(f)}
        assert rows["wei@acme.com"]["analysed"] == "yes"
        assert rows["wei@acme.com"]["fit_score"] == "8"
        assert rows["un@x.com"]["analysed"] == "no"
