"""The Brain in front of more than one provider."""

import json

import httpx
import pytest

from openvz_leads.brain import DEFAULT_MODELS, Brain
from openvz_leads.config import EnvConfig, ModelConfig
from openvz_leads.state import StateManager


def make_brain(tmp_path, provider="openai", **model_kwargs):
    env = EnvConfig(
        openai_api_key=model_kwargs.pop("openai_key", "sk-test"),
        deepseek_api_key=model_kwargs.pop("deepseek_key", ""),
        model_api_key=model_kwargs.pop("model_key", ""),
    )
    model = ModelConfig(provider=provider, **model_kwargs)
    return Brain(StateManager(str(tmp_path / "t.db")), model, env)


class _Response:
    """The parts of httpx.Response the Brain touches."""

    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def completion(content):
    return {"choices": [{"message": {"content": content}}]}


class TestConfiguration:
    def test_the_default_is_still_the_cli(self, tmp_path):
        brain = Brain(StateManager(str(tmp_path / "t.db")))
        assert brain.provider == "claude_cli"
        assert "no extra model cost" in brain.describe()
        assert brain.readiness() == (True, "")

    def test_a_provider_without_a_key_says_which_variable(self, tmp_path):
        brain = make_brain(tmp_path, "openai", openai_key="")
        ok, why = brain.readiness()
        assert not ok and "OPENAI_API_KEY" in why

    def test_a_shared_key_variable_covers_any_remote_provider(self, tmp_path):
        brain = make_brain(tmp_path, "deepseek", openai_key="", model_key="sk-shared")
        assert brain.readiness()[0]

    def test_a_blank_model_name_falls_back_to_the_provider_default(self, tmp_path):
        assert make_brain(tmp_path, "deepseek").model_name() == DEFAULT_MODELS["deepseek"]

    def test_an_explicit_model_name_wins(self, tmp_path):
        assert make_brain(tmp_path, "openai", name="o4-mini").model_name() == "o4-mini"

    def test_a_compatible_endpoint_needs_its_url(self, tmp_path):
        brain = make_brain(tmp_path, "openai_compatible", model_key="k", name="local")
        ok, why = brain.readiness()
        assert not ok and "base_url" in why

    def test_an_unknown_provider_is_rejected_at_config_time(self):
        with pytest.raises(ValueError):
            ModelConfig(provider="mistral")


@pytest.mark.asyncio
class TestHttpCalls:
    async def test_a_normal_completion_comes_back(self, tmp_path, monkeypatch):
        seen = {}

        async def fake_post(self, url, json=None, headers=None):
            seen.update(url=url, body=json, headers=headers)
            return _Response(payload=completion("  hello  "))

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        brain = make_brain(tmp_path, "openai", name="gpt-test")
        await brain.state.init_db()

        assert await brain.think("hi") == "hello"
        assert seen["url"] == "https://api.openai.com/v1/chat/completions"
        assert seen["body"]["model"] == "gpt-test"
        assert seen["headers"]["Authorization"] == "Bearer sk-test"

    async def test_deepseek_goes_to_deepseek(self, tmp_path, monkeypatch):
        seen = {}

        async def fake_post(self, url, json=None, headers=None):
            seen["url"] = url
            return _Response(payload=completion("ok"))

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        brain = make_brain(tmp_path, "deepseek", deepseek_key="sk-ds", openai_key="")
        await brain.state.init_db()
        await brain.think("hi")
        assert seen["url"].startswith("https://api.deepseek.com/")

    async def test_a_bad_key_does_not_retry(self, tmp_path, monkeypatch):
        calls = []

        async def fake_post(self, url, json=None, headers=None):
            calls.append(url)
            return _Response(status_code=401, text="nope")

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        brain = make_brain(tmp_path, "openai")
        await brain.state.init_db()

        assert await brain.think("hi") == ""
        assert len(calls) == 1  # a wrong key stays wrong

    async def test_a_rate_limit_does_retry(self, tmp_path, monkeypatch):
        calls = []

        async def fake_post(self, url, json=None, headers=None):
            calls.append(url)
            if len(calls) == 1:
                return _Response(status_code=429, text="slow down")
            return _Response(payload=completion("second time"))

        async def no_sleep(_seconds):
            return None

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        monkeypatch.setattr("openvz_leads.brain.asyncio.sleep", no_sleep)
        brain = make_brain(tmp_path, "openai")
        await brain.state.init_db()

        assert await brain.think("hi") == "second time"
        assert len(calls) == 2

    async def test_an_unknown_model_names_the_setting_to_change(
        self, tmp_path, monkeypatch, caplog
    ):
        async def fake_post(self, url, json=None, headers=None):
            return _Response(status_code=404, text="no such model")

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        brain = make_brain(tmp_path, "openai", name="gpt-imaginary")
        await brain.state.init_db()

        assert await brain.think("hi") == ""
        assert "model.name" in caplog.text

    async def test_a_missing_key_fails_before_any_request(self, tmp_path, monkeypatch):
        async def explode(*args, **kwargs):
            raise AssertionError("should not have made a request")

        monkeypatch.setattr(httpx.AsyncClient, "post", explode)
        brain = make_brain(tmp_path, "openai", openai_key="")
        assert await brain.think("hi") == ""

    async def test_think_json_parses_a_fenced_answer(self, tmp_path, monkeypatch):
        async def fake_post(self, url, json=None, headers=None):
            return _Response(
                payload=completion('```json\n{"industries": ["dental clinic"]}\n```')
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        brain = make_brain(tmp_path, "openai")
        await brain.state.init_db()
        assert await brain.think_json("hi") == {"industries": ["dental clinic"]}


class TestResponseShapes:
    """Clones and reasoning models do not all put the text in one place."""

    def test_plain_string_content(self):
        assert Brain._extract_completion(_Response(payload=completion(" hi "))) == "hi"

    def test_content_as_a_list_of_parts(self):
        payload = {
            "choices": [
                {"message": {"content": [
                    {"type": "text", "text": "one "},
                    {"type": "text", "text": "two"},
                ]}}
            ]
        }
        assert Brain._extract_completion(_Response(payload=payload)) == "one two"

    def test_a_body_that_is_not_json_is_empty_not_an_exception(self):
        assert Brain._extract_completion(_Response(payload=None, text="<html>")) == ""

    def test_no_choices_is_empty(self):
        assert Brain._extract_completion(_Response(payload={"choices": []})) == ""


@pytest.mark.asyncio
class TestBudget:
    async def test_the_budget_comes_from_the_model_config(self, tmp_path):
        brain = make_brain(tmp_path, "openai", daily_call_budget=2)
        await brain.state.init_db()
        assert await brain.is_within_budget()
        await brain.state.increment_usage()
        await brain.state.increment_usage()
        assert not await brain.is_within_budget()

    async def test_an_explicit_count_still_overrides(self, tmp_path):
        brain = make_brain(tmp_path, "openai", daily_call_budget=1)
        await brain.state.init_db()
        await brain.state.increment_usage()
        assert await brain.is_within_budget(10)
