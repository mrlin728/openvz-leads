"""Configuration loader for OpenVZ Leads. Reads openvz-leads.yaml + .env."""

import logging
import os
from datetime import time as _time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError, field_validator, model_validator

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


class GmailFooterConfig(BaseModel):
    """The opt-out line, and the postal address that has to sit under it.

    Instantly appended these for us. Gmail does not — it is a mailbox, not a
    sending platform — so with `provider: gmail` this product is the only
    thing standing between the user and an unlawful commercial email.

    CAN-SPAM (US), PECR (UK) and most equivalents require a working opt-out
    mechanism and a valid physical postal address in commercial mail. There
    is no link to click here, so the mechanism is a reply: the Handler
    already treats "stop", "remove me" and every other refusal as immediate
    and permanent, which makes a reply a real mechanism rather than a
    gesture.

    `postal_address` has no default on purpose. A placeholder would ship as
    a fake address in real mail, which is worse than refusing to send.
    """

    enabled: bool = True
    opt_out_line: str = "Reply \"stop\" and I won't contact you again."
    postal_address: str = ""

    @field_validator("opt_out_line", "postal_address", mode="before")
    @classmethod
    def _clean(cls, v):
        return str(v or "").strip()

    def problem(self) -> str:
        """Why this footer cannot be used, or '' when it can."""
        if not self.enabled:
            return (
                "channels.email.gmail.footer.enabled is false. Commercial "
                "email needs a working opt-out and a postal address; turning "
                "the footer off is a decision to send without them."
            )
        if not self.opt_out_line:
            return "channels.email.gmail.footer.opt_out_line is empty."
        if not self.postal_address:
            return (
                "channels.email.gmail.footer.postal_address is empty. Most "
                "jurisdictions require a real postal address in commercial "
                "mail, and a made-up one is worse than not sending."
            )
        return ""

    def render(self) -> str:
        return f"{self.opt_out_line}\n{self.postal_address}"


class GmailConfig(BaseModel):
    """Sending through the user's own mailbox.

    The trade against a sending platform is deliberate. A platform warms
    domains, rotates inboxes and reports deliverability; Gmail does none of
    that, and a personal mailbox that suddenly sends fifty cold emails a day
    is a mailbox that gets limited. In exchange the mail comes from a real
    person's real address, threads properly, and no third party holds the
    prospect list.

    Which is why max_daily_sends matters more here, not less.
    """

    # Blank falls back to persona.name — one place to write who you are.
    sender_name: str = ""
    # What the mailbox is allowed to read back.
    #
    #   metadata  headers only. Enough to see that they replied and stop the
    #             follow-ups, which is the part that matters. Default.
    #   readonly  message bodies too, so the Handler can classify intent and
    #             (if you turn it on) draft a reply. Broader than most
    #             prospecting needs, so it is opt-in.
    #   none      send only. Follow-ups will not stop on a reply — an
    #             unpleasant enough combination that it is rejected below
    #             unless follow-ups are off.
    read_scope: str = "metadata"
    # Cap on follow-ups regardless of how long a sequence the Writer produced.
    max_followups: int = 2
    # Minimum gap between two sends to the same person, whatever the sequence
    # says. A Writer that emits delay_days: 0 twice should not send twice in
    # a minute.
    min_followup_days: int = 2
    footer: GmailFooterConfig = GmailFooterConfig()

    @field_validator("read_scope")
    @classmethod
    def _known_scope(cls, v: str) -> str:
        known = {"metadata", "readonly", "none"}
        normalized = (v or "metadata").strip().lower()
        if normalized not in known:
            raise ValueError(
                f"'{v}' is not a Gmail read scope. Use one of: {sorted(known)}."
            )
        return normalized

    @field_validator("max_followups", "min_followup_days")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("must be >= 0")
        return v

    @property
    def can_detect_replies(self) -> bool:
        return self.read_scope != "none"


class EmailChannelConfig(BaseModel):
    enabled: bool = True
    # "none" means OpenVZ Leads never sends: it finds, analyses and drafts,
    # and you export the results. Sending is strictly opt-in.
    provider: str = "none"
    max_daily_sends: int = 50
    # Only read when provider is "gmail".
    gmail: GmailConfig = GmailConfig()

    @field_validator("max_daily_sends")
    @classmethod
    def _sends_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_daily_sends must be >= 0")
        return v

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        known = {"none", "gmail", "instantly"}
        normalized = (v or "none").strip().lower()
        if normalized not in known:
            raise ValueError(
                f"'{v}' is not a supported email provider. Use one of: {sorted(known)}."
            )
        return normalized

    @model_validator(mode="after")
    def _reply_detection_is_possible(self):
        """Refuse a configuration that would follow up into a reply.

        Sending follow-ups you cannot stop is the single worst thing this
        product could do: the prospect answers, and the machine keeps mailing
        them on schedule. Better to fail at startup than to be that.
        """
        if self.provider == "gmail" and not self.gmail.can_detect_replies:
            if self.gmail.max_followups > 0:
                raise ValueError(
                    "channels.email.gmail.read_scope is 'none', so replies "
                    "cannot be seen and follow-ups could not be stopped. "
                    "Either widen read_scope to 'metadata', or set "
                    "max_followups to 0 to send one email and nothing more."
                )
        return self

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
    # Google OAuth client for sending through the user's own mailbox. These
    # identify the *application*, not the account — the account is authorised
    # separately by `openvz-leads gmail login`, and its refresh token is
    # written to the workspace, never here.
    google_client_id: str = ""
    google_client_secret: str = ""

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
        with open(config_path, encoding="utf-8") as f:
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
        google_client_id=os.getenv("GOOGLE_CLIENT_ID", "").strip(),
        google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", "").strip(),
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
