"""Extra config coverage: YAML loading, validation errors, env parsing."""

import pytest
import yaml
from pydantic import ValidationError

from openvz_leads.config import (
    ConfigError,
    LeadsConfig,
    EnvConfig,
    QuietHoursConfig,
    load_config,
    load_env,
)


MINIMAL = {
    "persona": {
        "name": "H", "company": "A", "role": "B",
        "email": "h@a.com", "linkedin": "", "tone": "casual",
    },
    "product": {
        "name": "P", "description": "D", "pricing": "$1",
        "key_benefits": ["x"], "objection_responses": {},
    },
    "icp": {
        "industries": ["SaaS"], "company_size": "10-50",
        "titles": ["CEO"], "geography": ["US"],
    },
}


def test_load_config_from_yaml_file(tmp_path):
    path = tmp_path / "openvz-leads.yaml"
    path.write_text(yaml.safe_dump(MINIMAL), encoding="utf-8")
    config = load_config(str(path))
    assert config.persona.name == "H"
    assert config.icp.titles == ["CEO"]


def test_load_config_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "nope.yaml"))


def test_load_config_malformed_yaml_raises(tmp_path):
    path = tmp_path / "openvz-leads.yaml"
    path.write_text("persona: [unclosed\n  - :bad", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(str(path))


def test_load_config_yaml_missing_sections_raises(tmp_path):
    # Invalid config must be rejected; the exact exception type has churned
    # between ConfigError and pydantic ValidationError, both are acceptable.
    path = tmp_path / "openvz-leads.yaml"
    path.write_text(yaml.safe_dump({"persona": MINIMAL["persona"]}), encoding="utf-8")
    with pytest.raises((ConfigError, ValidationError)):
        load_config(str(path))


def test_load_config_empty_file_raises(tmp_path):
    path = tmp_path / "openvz-leads.yaml"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(str(path))


def test_load_config_non_mapping_yaml_raises(tmp_path):
    path = tmp_path / "openvz-leads.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(str(path))


def test_config_missing_required_persona_field():
    data = {k: dict(v) for k, v in MINIMAL.items()}
    data["persona"] = dict(MINIMAL["persona"])
    del data["persona"]["email"]
    with pytest.raises(ValidationError):
        LeadsConfig(**data)


def test_config_wrong_type_raises():
    data = dict(MINIMAL)
    data["icp"] = {
        "industries": "not-a-list", "company_size": "1",
        "titles": [], "geography": [],
    }
    with pytest.raises(ValidationError):
        LeadsConfig(**data)


def test_usage_and_channel_overrides():
    data = dict(MINIMAL)
    data["usage"] = {
        "max_daily_claude_percent": 50,
        "heartbeat_interval_minutes": 5,
        "quiet_hours": {"start": "23:00", "end": "06:00", "timezone": "UTC"},
    }
    data["channels"] = {"email": {"enabled": False, "max_daily_sends": 10}}
    config = LeadsConfig(**data)
    assert config.usage.max_daily_claude_percent == 50.0
    assert config.usage.heartbeat_interval_minutes == 5
    assert config.usage.quiet_hours.timezone == "UTC"
    assert config.channels.email.enabled is False
    assert config.channels.email.max_daily_sends == 10
    # linkedin channel keeps its defaults
    assert config.channels.linkedin.max_daily_connections == 20


def test_quiet_hours_defaults():
    q = QuietHoursConfig()
    assert q.start == "22:00"
    assert q.end == "07:00"


def test_quiet_hours_rejects_bad_time():
    with pytest.raises(ValidationError):
        QuietHoursConfig(start="25:99")


def test_quiet_hours_rejects_bad_timezone():
    with pytest.raises(ValidationError):
        QuietHoursConfig(timezone="Mars/Olympus_Mons")


def test_usage_rejects_out_of_range_percent():
    data = dict(MINIMAL)
    data["usage"] = {"max_daily_claude_percent": 150}
    with pytest.raises(ValidationError):
        LeadsConfig(**data)
    data["usage"] = {"max_daily_claude_percent": 0}
    with pytest.raises(ValidationError):
        LeadsConfig(**data)


def test_usage_rejects_zero_heartbeat():
    data = dict(MINIMAL)
    data["usage"] = {"heartbeat_interval_minutes": 0}
    with pytest.raises(ValidationError):
        LeadsConfig(**data)


def test_email_channel_rejects_negative_sends():
    data = dict(MINIMAL)
    data["channels"] = {"email": {"max_daily_sends": -5}}
    with pytest.raises(ValidationError):
        LeadsConfig(**data)


def test_offer_override():
    data = dict(MINIMAL)
    data["product"] = dict(MINIMAL["product"])
    data["product"]["offer"] = {"goal": "start_trial", "booking_url": "https://cal.com/x"}
    config = LeadsConfig(**data)
    assert config.product.offer.goal == "start_trial"
    assert config.product.offer.booking_url == "https://cal.com/x"
    assert config.product.offer.meeting_duration == "15 minutes"


def test_load_env_reads_environment(monkeypatch):
    monkeypatch.setenv("INSTANTLY_API_KEY", "key-123")
    monkeypatch.setenv("SERPER_API_KEY", "  serp-456  ")
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    env = load_env()
    assert env.instantly_api_key == "key-123"
    assert env.serper_api_key == "serp-456"
    assert env.hunter_api_key == ""


def test_env_config_rejects_non_string():
    with pytest.raises(ValidationError):
        EnvConfig(instantly_api_key=["not", "a", "string"])


# ── Which workspace's configuration is this? ──


class TestConfigDiscovery:
    """OPENVZ_LEADS_HOME is how one install drives several workspaces.

    The database, prompts and skills all follow it. Until this was fixed the
    config did not, so running from inside a checkout paired one workspace's
    prospects with another's configuration — the wrong persona and ICP, and
    on the Gmail path the wrong sender name and postal address on real mail.
    Silently, because both files exist and both parse.
    """

    def _write(self, path, company):
        import yaml

        data = {k: v for k, v in MINIMAL.items()}
        data["persona"] = {**MINIMAL["persona"], "company": company}
        path.write_text(yaml.safe_dump(data), encoding="utf-8")

    def test_an_explicit_workspace_wins_over_the_working_directory(
        self, tmp_path, monkeypatch
    ):
        from openvz_leads.config import _find_config_file

        cwd = tmp_path / "checkout"
        workspace = tmp_path / "workspace"
        cwd.mkdir()
        workspace.mkdir()
        self._write(cwd / "openvz-leads.yaml", "Checkout Co")
        self._write(workspace / "openvz-leads.yaml", "Workspace Co")

        monkeypatch.chdir(cwd)
        monkeypatch.setenv("OPENVZ_LEADS_HOME", str(workspace))
        assert _find_config_file() == str(workspace / "openvz-leads.yaml")

    def test_without_an_override_the_working_directory_still_wins(
        self, tmp_path, monkeypatch
    ):
        """A plain checkout keeps behaving exactly as it did."""
        from openvz_leads.config import _find_config_file

        cwd = tmp_path / "checkout"
        cwd.mkdir()
        self._write(cwd / "openvz-leads.yaml", "Checkout Co")

        monkeypatch.chdir(cwd)
        monkeypatch.delenv("OPENVZ_LEADS_HOME", raising=False)
        assert _find_config_file() == str(cwd / "openvz-leads.yaml")

    def test_an_override_pointing_nowhere_falls_back_rather_than_failing(
        self, tmp_path, monkeypatch
    ):
        from openvz_leads.config import _find_config_file

        cwd = tmp_path / "checkout"
        cwd.mkdir()
        self._write(cwd / "openvz-leads.yaml", "Checkout Co")

        monkeypatch.chdir(cwd)
        monkeypatch.setenv("OPENVZ_LEADS_HOME", str(tmp_path / "does-not-exist"))
        assert _find_config_file() == str(cwd / "openvz-leads.yaml")
