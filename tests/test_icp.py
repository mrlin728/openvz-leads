"""Parsing a request, and writing it back without wrecking the config."""

import pytest
import yaml

from openvz_leads.icp import (
    ICPDraft,
    apply_to_file,
    heuristic_parse,
    parse_request,
    render_icp_block,
    replace_icp_block,
)


# ── The draft model has to survive whatever a model returns ──


class TestDraftCoercion:
    def test_comma_string_becomes_a_list(self):
        draft = ICPDraft(industries="SaaS, Marketing Agency")
        assert draft.industries == ["SaaS", "Marketing Agency"]

    def test_list_of_dicts_is_unwrapped(self):
        draft = ICPDraft(titles=[{"name": "CMO"}, {"value": "Head of Growth"}])
        assert draft.titles == ["CMO", "Head of Growth"]

    def test_nulls_do_not_raise(self):
        draft = ICPDraft(industries=None, titles=None, company_size=None)
        assert draft.industries == [] and draft.company_size == ""

    def test_duplicates_collapse_case_insensitively(self):
        assert ICPDraft(geography=["US", "us", "United States"]).geography == [
            "US",
            "United States",
        ]

    def test_confidence_out_of_vocabulary_lands_somewhere_sane(self):
        assert ICPDraft(confidence="7/10").confidence == "medium"
        assert ICPDraft(confidence="VERY HIGH").confidence == "high"
        assert ICPDraft(confidence=None).confidence == "low"

    def test_wrapping_quotes_go_but_inner_quotes_stay(self):
        draft = ICPDraft(
            industries='"SaaS"',
            assumptions=['"Outdated website" is checked during analysis, not search'],
        )
        assert draft.industries == ["SaaS"]
        assert draft.assumptions[0].startswith('"Outdated website"')

    def test_an_assumption_is_one_sentence_not_two_fragments(self):
        # Splitting on commas is right for a field of labels and wrong for a
        # field of sentences, which is why they have separate validators.
        draft = ICPDraft(assumptions=["You did not name titles, so I inferred them."])
        assert len(draft.assumptions) == 1

    def test_usable_needs_something_searchable(self):
        assert not ICPDraft(geography=["US"], titles=["CEO"]).is_usable()
        assert ICPDraft(industries=["dental clinic"]).is_usable()


# ── The path that runs when there is no model ──


class TestHeuristicParse:
    def test_chinese_request(self):
        draft = heuristic_parse("帮我找美国牙科诊所")
        assert draft.geography == ["United States"]
        assert "牙科诊所" in draft.industries[0]
        assert draft.via == "heuristic"
        assert draft.confidence == "low"

    def test_the_qualifier_survives(self):
        # The whole point of that request. A four-field parse drops it and
        # returns clinics that are perfectly happy with their website.
        draft = heuristic_parse(
            "Find dental clinics in California with outdated websites "
            "and 5-50 employees"
        )
        assert draft.industries == ["dental clinics"]
        assert draft.company_size == "5-50 employees"
        assert draft.geography == ["California"]
        assert draft.keywords == ["outdated websites"]

    def test_upper_bound_size(self):
        assert heuristic_parse("agencies under 200 employees").company_size == (
            "1-200 employees"
        )

    def test_titles_named_outright_are_picked_up(self):
        draft = heuristic_parse("SaaS companies, CMO or Head of Growth")
        assert "CMO" in draft.titles

    def test_empty_request_is_not_usable(self):
        assert not heuristic_parse("").is_usable()


# ── Falling back when the model is absent or unhelpful ──


class _Brain:
    """Minimal stand-in: returns whatever it was constructed with."""

    def __init__(self, answer):
        self.answer = answer

    def load_prompt(self, name, **kwargs):
        return ""

    async def think_json(self, prompt, session_id=None):
        return self.answer


@pytest.mark.asyncio
class TestParseRequest:
    async def test_no_brain_falls_back_and_says_so(self):
        draft = await parse_request(None, "帮我找美国牙科诊所")
        assert draft.via == "heuristic"
        assert "without a model" in draft.assumptions[0]

    async def test_model_answer_is_used(self):
        draft = await parse_request(
            _Brain({
                "industries": ["dental clinic"],
                "company_size": "5-50 employees",
                "geography": ["California"],
                "titles": ["Owner"],
                "keywords": ["outdated website"],
                "assumptions": ["You did not name titles."],
                "confidence": "high",
                "summary": "Dental practices in California.",
            }),
            "Find dental clinics in California",
        )
        assert draft.via == "model"
        assert draft.keywords == ["outdated website"]
        assert draft.confidence == "high"

    async def test_unsearchable_model_answer_falls_back(self):
        # Geography and titles alone cannot be searched on.
        draft = await parse_request(
            _Brain({"geography": ["California"], "titles": ["Owner"]}),
            "dental clinics in California",
        )
        assert draft.via == "heuristic"

    async def test_model_assumptions_are_not_padded_with_english(self):
        # The model already wrote them, in the user's language. Appending our
        # own English notes duplicates the point and looks automated.
        draft = await parse_request(
            _Brain({
                "industries": ["牙科诊所"],
                "assumptions": ["你没有说职位，我推断了三个。"],
            }),
            "帮我找牙科诊所",
        )
        assert draft.assumptions == ["你没有说职位，我推断了三个。"]

    async def test_a_broken_model_answer_still_yields_something(self):
        draft = await parse_request(_Brain("not json at all"), "dental clinics in Texas")
        assert draft.is_usable()
        assert draft.via == "heuristic"


# ── Writing it back into a config that people read ──


CONFIG = """\
# A header comment.
persona:
  name: "Someone"

# Who you want to reach.
icp:
  industries:
    - "SaaS"
  company_size: "10-200 employees"
  titles: []
  geography: []

# ── Human review ──
review:
  require_approval: true
"""


class TestWriteBack:
    def test_only_the_icp_block_changes(self):
        draft = ICPDraft(industries=["dental clinic"], company_size="5-50 employees")
        out = replace_icp_block(CONFIG, render_icp_block(draft))
        parsed = yaml.safe_load(out)
        assert parsed["icp"]["industries"] == ["dental clinic"]
        assert parsed["persona"]["name"] == "Someone"
        assert parsed["review"]["require_approval"] is True

    def test_the_next_block_keeps_its_own_header_comment(self):
        # Comments above a key belong to that key. Eating "# ── Human review ──"
        # while replacing the block above it is the classic off-by-one here.
        out = replace_icp_block(CONFIG, render_icp_block(ICPDraft(industries=["x"])))
        assert "# ── Human review ──" in out
        assert "# A header comment." in out

    def test_saving_twice_does_not_drift(self):
        draft = ICPDraft(industries=["dental clinic"], request="find dental clinics")
        once = replace_icp_block(CONFIG, render_icp_block(draft))
        twice = replace_icp_block(once, render_icp_block(draft))
        assert once == twice

    def test_a_config_with_no_icp_block_gains_one(self):
        out = replace_icp_block(
            "persona:\n  name: \"Someone\"\n", render_icp_block(ICPDraft(industries=["x"]))
        )
        assert yaml.safe_load(out)["icp"]["industries"] == ["x"]

    def test_awkward_values_stay_valid_yaml(self):
        # A colon, a hash, a quote and CJK all have to survive the round trip.
        draft = ICPDraft(
            industries=['dentists: the "good" ones # really'],
            company_size="5-50 employees",
            request="帮我找美国牙科诊所",
        )
        parsed = yaml.safe_load(replace_icp_block(CONFIG, render_icp_block(draft)))
        assert parsed["icp"]["industries"] == ['dentists: the "good" ones # really']
        assert parsed["icp"]["request"] == "帮我找美国牙科诊所"

    def test_a_multiline_request_does_not_break_the_comment(self):
        draft = ICPDraft(industries=["x"], request="find dental\nclinics\nplease")
        out = replace_icp_block(CONFIG, render_icp_block(draft))
        assert yaml.safe_load(out)["icp"]["request"] == "find dental\nclinics\nplease"

    def test_apply_writes_the_file(self, tmp_path):
        path = tmp_path / "openvz-leads.yaml"
        path.write_text(CONFIG, encoding="utf-8")
        apply_to_file(ICPDraft(industries=["dental clinic"]), path)
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["icp"]["industries"] == ["dental clinic"]

    def test_the_config_the_product_ships_round_trips(self):
        """The template is the file most users edit — it must survive a save."""
        from pathlib import Path

        template = Path(__file__).resolve().parent.parent / "openvz-leads.yaml"
        if not template.exists():
            pytest.skip("running outside a checkout")
        original = template.read_text(encoding="utf-8")
        draft = ICPDraft(industries=["dental clinic"], request="find dental clinics")
        out = replace_icp_block(original, render_icp_block(draft))
        before, after = yaml.safe_load(original), yaml.safe_load(out)
        assert after["icp"]["industries"] == ["dental clinic"]
        for key in before:
            if key != "icp":
                assert after[key] == before[key], f"{key} was disturbed"
