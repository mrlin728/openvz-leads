"""Configuration loader for OpenVZ Leads. Reads openvz-leads.yaml + .env."""

import logging
import os
from datetime import time as _time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError, field_validator

logger = logging.getLogger("openvz_leads.config")


class ConfigError(Exception):
    """Raised when OpenVZ Leads' configuration is missing or invalid."""


class ConfigFileNotFoundError(ConfigError, FileNotFoundError):
    """Config file is missing. Subclasses FileNotFoundError for
    backward compatibility with existing callers/tests."""


class PersonaConfig(BaseModel):
    name: str
    company: str
    role: str
    email: str
    linkedin: str
    tone: str


class OfferConfig(BaseModel):
    primary: str = ""
    entry: str = ""
    goal: str = "book_call"  # book_call, start_trial, get_reply
    booking_method: str = "calendar_link"  # calendar_link, suggest_times, ask_preference
    booking_url: str = ""
    meeting_duration: str = "15 minutes"
    meeting_owner: str = ""


class ProductConfig(BaseModel):
    name: str
    description: str
    pricing: str
    key_benefits: list[str]
    objection_responses: dict[str, str]
    offer: OfferConfig = OfferConfig()


class ICPConfig(BaseModel):
    industries: list[str]
    company_size: str
    titles: list[str]
    geography: list[str]


class EmailChannelConfig(BaseModel):
    enabled: bool = True
    # "none" means OpenVZ Leads never sends: it finds, analyses and drafts,
    # and you export the results. Sending is strictly opt-in.
    provider: str = "none"
    max_daily_sends: int = 50

    @field_validator("max_daily_sends")
    @classmethod
    def _sends_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_daily_sends must be >= 0")
        return v

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        known = {"none", "instantly"}
        normalized = (v or "none").strip().lower()
        if normalized not in known:
            raise ValueError(
                f"'{v}' is not a supported email provider. Use one of: {sorted(known)}."
            )
        return normalized

    @property
    def sending_enabled(self) -> bool:
        """True only when a real outbound channel is wired up."""
        return self.enabled and self.provider != "none"


class LinkedInChannelConfig(BaseModel):
    enabled: bool = True
    max_daily_connections: int = 20
    max_daily_messages: int = 10


class ChannelsConfig(BaseModel):
    email: EmailChannelConfig = EmailChannelConfig()
    linkedin: LinkedInChannelConfig = LinkedInChannelConfig()


class ReviewConfig(BaseModel):
    """Human-in-the-loop settings.

    Defaults are deliberately conservative: nothing leaves the machine until
    a person has read it. Flip these off only once you trust the output.
    """

    require_approval: bool = True  # campaigns wait in the review queue
    auto_reply: bool = False  # replies are classified but never auto-answered


class ProfilingConfig(BaseModel):
    """Account-analysis settings (the Profiler agent)."""

    enabled: bool = True
    # Skip anything that scored below this — analysis costs a Claude call each.
    min_score: int = 5
    max_per_cycle: int = 5
    # Language for the written analysis. The outreach emails keep their own
    # language (set by persona.tone / the writer prompt) — this is only the
    # brief you read. e.g. "简体中文", "English", "日本語".
    output_language: str = "English"
    # Pull the company's own site when we have a URL. Costs nothing but time
    # and gives the analysis something real to stand on.
    fetch_website: bool = True

    @field_validator("max_per_cycle")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_per_cycle must be at least 1")
        return v

    @field_validator("min_score")
    @classmethod
    def _score_range(cls, v: int) -> int:
        if not 0 <= v <= 10:
            raise ValueError("min_score must be between 0 and 10")
        return v


class QuietHoursConfig(BaseModel):
    start: str = "22:00"
    end: str = "07:00"
    timezone: str = "America/New_York"

    @field_validator("start", "end")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        try:
            _time.fromisoformat(v)
        except ValueError:
            raise ValueError(
                f"'{v}' is not a valid time. Use 24h HH:MM format, e.g. '22:00'."
            )
        return v

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: str) -> str:
        import pytz

        if v not in pytz.all_timezones_set:
            raise ValueError(
                f"'{v}' is not a valid timezone. Use an IANA name like 'America/New_York'."
            )
        return v


class UsageConfig(BaseModel):
    max_daily_claude_percent: float = 80.0
    heartbeat_interval_minutes: int = 15
    quiet_hours: QuietHoursConfig = QuietHoursConfig()

    @field_validator("max_daily_claude_percent")
    @classmethod
    def _valid_percent(cls, v: float) -> float:
        if not 0 < v <= 100:
            raise ValueError("max_daily_claude_percent must be between 0 and 100")
        return v

    @field_validator("heartbeat_interval_minutes")
    @classmethod
    def _valid_interval(cls, v: int) -> int:
        if v < 1:
            raise ValueError("heartbeat_interval_minutes must be at least 1")
        return v


class LeadsConfig(BaseModel):
    persona: PersonaConfig
    product: ProductConfig
    icp: ICPConfig
    channels: ChannelsConfig = ChannelsConfig()
    review: ReviewConfig = ReviewConfig()
    profiling: ProfilingConfig = ProfilingConfig()
    usage: UsageConfig = UsageConfig()


class EnvConfig(BaseModel):
    instantly_api_key: str = ""
    linkedin_email: str = ""
    linkedin_password: str = ""
    hunter_api_key: str = ""
    serper_api_key: str = ""


def _format_validation_error(e: ValidationError) -> str:
    """Turn a pydantic ValidationError into a readable, actionable message."""
    lines = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def load_config(config_path: str | None = None) -> LeadsConfig:
    """Load OpenVZ Leads configuration from YAML file.

    Raises ConfigError with a clear, actionable message on any problem.
    """
    if config_path is None:
        config_path = _find_config_file()

    try:
        with open(config_path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigFileNotFoundError(
            f"Config file not found: {config_path}. "
            "Create one from openvz-leads.yaml.example or run 'openvz-leads setup'."
        )
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {config_path}:\n  {e}")
    except OSError as e:
        raise ConfigError(f"Could not read {config_path}: {e}")

    if data is None:
        raise ConfigError(f"{config_path} is empty. Run 'openvz-leads setup' to configure OpenVZ Leads.")
    if not isinstance(data, dict):
        raise ConfigError(
            f"{config_path} must contain a YAML mapping (key: value pairs), "
            f"got {type(data).__name__}."
        )

    try:
        return LeadsConfig(**data)
    except ValidationError as e:
        # Log a friendly, actionable summary, then re-raise the original
        # ValidationError so callers (and tests) keep the pydantic type.
        logger.error(
            f"Invalid configuration in {config_path}:\n{_format_validation_error(e)}\n"
            "Fix the fields above or re-run 'openvz-leads setup'."
        )
        raise


def load_env() -> EnvConfig:
    """Load environment variables from .env file."""
    load_dotenv()
    env = EnvConfig(
        instantly_api_key=os.getenv("INSTANTLY_API_KEY", "").strip(),
        linkedin_email=os.getenv("LINKEDIN_EMAIL", "").strip(),
        linkedin_password=os.getenv("LINKEDIN_PASSWORD", "").strip(),
        hunter_api_key=os.getenv("HUNTER_API_KEY", "").strip(),
        serper_api_key=os.getenv("SERPER_API_KEY", "").strip(),
    )
    if not env.instantly_api_key:
        # Not a problem: sending is opt-in. Finding, analysing, drafting and
        # exporting all work without any outbound channel configured.
        logger.info(
            "No INSTANTLY_API_KEY set — running in draft-only mode. "
            "Campaigns will be written and queued for review; use "
            "'openvz-leads export' to take them elsewhere."
        )
    return env


def _find_config_file() -> str:
    """Search for openvz-leads.yaml in common locations."""
    candidates = [
        Path.cwd() / "openvz-leads.yaml",
        Path.cwd().parent / "openvz-leads.yaml",
        Path(__file__).parent.parent / "openvz-leads.yaml",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    raise ConfigFileNotFoundError(
        "openvz-leads.yaml not found in "
        + ", ".join(str(p.parent) for p in candidates)
        + ". Create one from openvz-leads.yaml.example or run 'openvz-leads setup'."
    )
