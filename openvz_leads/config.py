"""Configuration loader for OpenVZ Leads. Reads openvz-leads.yaml + .env."""

import logging
import os
from datetime import time as _time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError, field_validator

from openvz_leads import paths as _paths

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
    """Who you want to reach.

    The four required fields are what the Scout searches on. The three below
    them exist because a real request is rarely just four fields: "dental
    clinics in California with outdated websites and 5-50 employees" carries a
    qualifier that no industry/size/geo triple can hold, and dropping it on
    the floor is how a tool returns technically-matching accounts nobody
    wanted. See openvz_leads/icp.py, which is what fills these in.
    """

    industries: list[str]
    company_size: str
    titles: list[str]
    geography: list[str]
    # Traits beyond the four fields — "outdated website", "hiring engineers",
    # "recently funded". Used twice: to shape search queries, and as criteria
    # the account analysis is told to check rather than assume.
    keywords: list[str] = []
    # What rules an account out. Checked in analysis, not in search: a search
    # engine cannot exclude, but a brief can say "this one does not qualify".
    exclusions: list[str] = []
    # The sentence this ICP was parsed from, kept verbatim. It is the only
    # record of what was actually asked for once the fields are edited.
    request: str = ""

    @field_validator("keywords", "exclusions", mode="before")
    @classmethod
    def _clean_list(cls, v):
        """Hand-edited YAML: tolerate a bare string, a null, or a list."""
        if v is None:
            return []
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        if isinstance(v, (list, tuple)):
            return [str(s).strip() for s in v if str(s).strip()]
        raise ValueError("must be a list of short phrases")


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


class ModelConfig(BaseModel):
    """Which model does the thinking.

    The default is the Claude Code CLI on your machine: no API key, no second
    bill, and the reason this product can claim "no extra model cost". The
    other providers exist for the cases that default cannot serve — a server
    with no interactive CLI login, a team that already buys OpenAI credit, or
    somewhere the Claude CLI is not available at all. Everything downstream
    speaks to the Brain, not to a provider, so switching is a config line.
    """

    provider: str = "claude_cli"
    # Blank means "use this provider's default", so switching providers does
    # not require also knowing a model id. See brain.DEFAULT_MODELS.
    name: str = ""
    # Only read for provider "openai_compatible" — the escape hatch for
    # anything else that speaks the OpenAI chat-completions shape (vLLM,
    # OpenRouter, a local Ollama). The named providers ignore it.
    base_url: str = ""
    temperature: float = 0.3
    max_tokens: int = 4096
    # A day's worth of calls at 100%. usage.max_daily_claude_percent is taken
    # as a percentage of this, which is why the old 200 stays the default.
    daily_call_budget: int = 200

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        known = {"claude_cli", "openai", "deepseek", "openai_compatible"}
        normalized = (v or "claude_cli").strip().lower()
        if normalized not in known:
            raise ValueError(
                f"'{v}' is not a supported model provider. Use one of: {sorted(known)}."
            )
        return normalized

    @field_validator("temperature")
    @classmethod
    def _valid_temperature(cls, v: float) -> float:
        if not 0.0 <= v <= 2.0:
            raise ValueError("temperature must be between 0.0 and 2.0")
        return v

    @field_validator("max_tokens", "daily_call_budget")
    @classmethod
    def _positive_int(cls, v: int) -> int:
        if v < 1:
            raise ValueError("must be at least 1")
        return v

    @property
    def needs_api_key(self) -> bool:
        """True when the provider bills you separately from the CLI."""
        return self.provider != "claude_cli"


class CrawlConfig(BaseModel):
    """How pages get read.

    Three tiers, in order of what they cost and what they can do:

    - basic       httpx + BeautifulSoup. Always present, no extra install.
    - crawl4ai    renders JavaScript and returns Markdown, which is what the
                  model actually wants to read. Optional dependency.
    - browser_use an agent driving a real browser. Only worth its cost on
                  pages the first two cannot get through: a consent wall, a
                  search that only exists behind a click.

    "auto" walks down that list and stops at the first tier that returns
    something usable, so an install with no optional packages behaves exactly
    as it did before this setting existed.
    """

    provider: str = "auto"
    # Escalate to a real browser when the cheaper tiers come back blocked or
    # empty. Off by default: it is slow, and most sites do not need it.
    browser_fallback: bool = False
    # Past this a homepage is navigation and boilerplate, which only dilutes
    # the prompt it is being gathered for.
    max_chars: int = 6000
    timeout_seconds: float = 30.0
    # Seconds between page fetches. Politeness, not throttling — one page per
    # account is already gentle, but a discovery run is not.
    delay_seconds: float = 1.0

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        known = {"auto", "basic", "crawl4ai", "browser_use"}
        normalized = (v or "auto").strip().lower()
        if normalized not in known:
            raise ValueError(
                f"'{v}' is not a supported crawl provider. Use one of: {sorted(known)}."
            )
        return normalized

    @field_validator("max_chars")
    @classmethod
    def _sane_chars(cls, v: int) -> int:
        if v < 500:
            raise ValueError("max_chars must be at least 500")
        return v

    @field_validator("timeout_seconds", "delay_seconds")
    @classmethod
    def _non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("must be >= 0")
        return v


class CrmConfig(BaseModel):
    """Where a deal's stage changes go once they happen.

    OpenVZ Leads owns the pipeline up to the point a person replies. What
    happens after that — meeting, won, lost — belongs in whatever system
    already holds your customers. This pushes each stage change there instead
    of asking you to keep two records in your head.

    provider "none" still records every stage change locally; it just does not
    tell anyone about it.
    """

    provider: str = "none"
    # For "webhook": where to POST. The payload shape is documented in
    # integrations/crm.py and is stable.
    webhook_url: str = ""
    # Sent as a bearer token when set (env: CRM_WEBHOOK_TOKEN wins over this).
    auth_header: str = "Authorization"
    # Stages worth telling the CRM about. Earlier ones are noise for most
    # teams — a prospect that was merely found is not a record yet.
    #
    # opted_out is in the default list and should stay there whatever else
    # you trim: it is the one stage where the CRM not knowing has a cost
    # beyond tidiness, because someone will eventually mail them from it.
    sync_stages: list[str] = [
        "contacted", "replied", "meeting", "won", "lost", "opted_out",
    ]
    timeout_seconds: float = 15.0

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        known = {"none", "webhook", "file"}
        normalized = (v or "none").strip().lower()
        if normalized not in known:
            raise ValueError(
                f"'{v}' is not a supported CRM provider. Use one of: {sorted(known)}."
            )
        return normalized

    @field_validator("sync_stages", mode="before")
    @classmethod
    def _clean_stages(cls, v):
        """Tolerate a YAML scalar or nulls — this list is hand-edited."""
        if v is None:
            return []
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        if isinstance(v, (list, tuple)):
            return [str(s).strip() for s in v if str(s).strip()]
        raise ValueError("sync_stages must be a list of stage names")

    @property
    def sync_enabled(self) -> bool:
        return self.provider != "none"


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
    model: ModelConfig = ModelConfig()
    crawl: CrawlConfig = CrawlConfig()
    crm: CrmConfig = CrmConfig()
    profiling: ProfilingConfig = ProfilingConfig()
    usage: UsageConfig = UsageConfig()


class EnvConfig(BaseModel):
    instantly_api_key: str = ""
    linkedin_email: str = ""
    linkedin_password: str = ""
    hunter_api_key: str = ""
    serper_api_key: str = ""
    # Only read when model.provider is not the default claude_cli.
    openai_api_key: str = ""
    deepseek_api_key: str = ""
    # For model.provider "openai_compatible": whatever that endpoint wants.
    model_api_key: str = ""
    # Bearer token for crm.webhook_url. Kept out of the YAML because the
    # YAML is the file people paste into issues.
    crm_webhook_token: str = ""

    def key_for_provider(self, provider: str) -> str:
        """The API key a model provider needs, or '' for the CLI.

        MODEL_API_KEY is honoured as an override for every remote provider so
        one variable can drive whichever is configured — useful in a container
        where the config is baked in and only the secret varies.
        """
        if provider == "claude_cli":
            return ""
        specific = {
            "openai": self.openai_api_key,
            "deepseek": self.deepseek_api_key,
        }.get(provider, "")
        return specific or self.model_api_key


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
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
        model_api_key=os.getenv("MODEL_API_KEY", "").strip(),
        crm_webhook_token=os.getenv("CRM_WEBHOOK_TOKEN", "").strip(),
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
        _paths.config_file(),
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    raise ConfigFileNotFoundError(
        "openvz-leads.yaml not found in "
        + ", ".join(str(p.parent) for p in candidates)
        + ". Create one from openvz-leads.yaml.example or run 'openvz-leads setup'."
    )
