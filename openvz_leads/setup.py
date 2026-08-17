"""Interactive setup wizard — Claude walks you through everything OpenVZ Leads needs."""

import asyncio
import getpass
import logging
import shutil
from pathlib import Path

import yaml

from openvz_leads.brain import Brain
from openvz_leads.state import StateManager

logger = logging.getLogger("openvz_leads.setup")

PROJECT_ROOT = Path(__file__).parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
CONFIG_FILE = PROJECT_ROOT / "openvz-leads.yaml"


def _say(message: str):
    """Print a wizard message."""
    print(f"\n  OpenVZ Leads: {message}")


def _print_step(step: int, total: int, title: str):
    """Print a step header."""
    print(f"\n{'─'*60}")
    print(f"  Step {step}/{total}: {title}")
    print(f"{'─'*60}")


def _ask(prompt: str, default: str = "", required: bool = True, secret: bool = False) -> str:
    """Ask the user a question. secret=True hides input (passwords, API keys)."""
    suffix = f" [{default}]" if default else ""
    reader = getpass.getpass if secret else input
    while True:
        try:
            answer = reader(f"\n  → {prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n  Setup cancelled.")
            raise SystemExit(1)

        if not answer and default:
            return default
        if not answer and required:
            print("    (required — please enter a value)")
            continue
        return answer


def _ask_yes_no(prompt: str, default: bool = True) -> bool:
    """Ask a yes/no question."""
    suffix = " [Y/n]" if default else " [y/N]"
    try:
        answer = input(f"\n  → {prompt}{suffix}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n\n  Setup cancelled.")
        raise SystemExit(1)

    if not answer:
        return default
    return answer in ("y", "yes")


def _check_claude_cli() -> bool:
    """Check if Claude CLI is installed and accessible."""
    return shutil.which("claude") is not None


async def _test_claude(brain: Brain) -> bool:
    """Test that Claude Code headless mode works."""
    response = await brain.think(
        "Respond with exactly: LEADS_READY",
        session_id="leads-setup-test",
    )
    return "LEADS_READY" in response


async def _test_instantly(api_key: str) -> bool:
    """Test the Instantly API key."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.instantly.ai/api/v2/accounts",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            return resp.status_code == 200
    except Exception:
        return False


class SetupWizard:
    def __init__(self):
        self.env_vars: dict[str, str] = {}
        self.config: dict = {}

    async def run(self):
        """Run the full interactive setup."""
        print(f"""
╔══════════════════════════════════════════════════════════╗
║                                                          ║
║   OpenVZ Leads — Setup                                   ║
║   Find them. Understand them. Reach them.                ║
║                                                          ║
╚══════════════════════════════════════════════════════════╝""")

        _say("Let's get you configured. This takes about 5 minutes.")
        _say("By default nothing is ever sent: OpenVZ Leads finds accounts,")
        _say("analyses them, drafts the outreach, and waits for you.\n")

        total_steps = 6

        # Step 1: Check prerequisites
        _print_step(1, total_steps, "Checking Prerequisites")
        await self._check_prerequisites()

        # Step 2: Connect email platform
        _print_step(2, total_steps, "Sending (Optional)")
        await self._setup_email_platform()

        # Step 3: LinkedIn (optional)
        _print_step(3, total_steps, "LinkedIn (Optional)")
        self._setup_linkedin()

        # Step 4: Cloudflare crawling (optional)
        _print_step(4, total_steps, "Website Crawling (Optional)")
        self._setup_cloudflare()

        # Step 5: Train on product
        _print_step(5, total_steps, "Train OpenVZ Leads on Your Product")
        await self._setup_product()

        # Step 6: Configure behavior
        _print_step(6, total_steps, "Configure Behavior")
        self._setup_behavior()

        # Write everything
        self._write_env()
        self._write_config()

        # Final summary
        await self._print_summary()

    async def _check_prerequisites(self):
        """Verify Claude CLI is installed and working."""
        # Check Claude CLI
        print("\n  Checking Claude Code CLI...", end=" ")
        if _check_claude_cli():
            print("✓ Found")
        else:
            print("✗ Not found")
            _say("Claude Code CLI is the thinking engine. Install it from:")
            print("         https://claude.ai/download")
            print("\n         After installing, run 'claude login' to authenticate,")
            print("         then re-run this setup.")
            raise SystemExit(1)

        # Test headless mode
        print("  Testing Claude headless mode...", end=" ", flush=True)
        state = StateManager()
        await state.init_db()
        brain = Brain(state)
        if await _test_claude(brain):
            print("✓ Working")
        else:
            print("✗ Failed")
            _say("Claude CLI is installed but headless mode isn't working.")
            _say("Make sure you're logged in: run 'claude login'")
            print("\n         Also verify your Max subscription is active.")
            if not _ask_yes_no("Continue anyway?", default=False):
                raise SystemExit(1)

        # Check Python packages
        print("  Checking Python dependencies...", end=" ")
        missing = []
        for pkg in ["httpx", "aiosqlite", "yaml", "pydantic", "bs4"]:
            try:
                __import__(pkg)
            except ImportError:
                missing.append(pkg)
        if not missing:
            print("✓ All installed")
        else:
            print(f"✗ Missing: {', '.join(missing)}")
            print("\n  Run: pip install -r requirements.txt")
            if not _ask_yes_no("Continue anyway?", default=False):
                raise SystemExit(1)

        _say("Prerequisites look good. Let's keep going.")

    async def _setup_email_platform(self):
        """Configure an outbound provider — entirely optional."""
        _say("Sending is optional and off by default.")
        _say("Without it you still get the full pipeline: accounts found,")
        _say("accounts analysed, outreach drafted — then you export the")
        _say("results as CSV or Markdown and send from your own tool.")
        _say("Connect a provider only if you want OpenVZ Leads to send for you.")

        self.email_provider = "none"
        self.env_vars["INSTANTLY_API_KEY"] = ""

        if not _ask_yes_no(
            "Connect a sending provider now? (Instantly)", default=False
        ):
            _say("Draft-only mode it is. You can connect one any time by")
            _say("adding INSTANTLY_API_KEY to .env and setting")
            _say("channels.email.provider to 'instantly' in openvz-leads.yaml.")
            return

        api_key = _ask(
            "Instantly API key (Settings → Integrations; input hidden)", secret=True
        )
        self.env_vars["INSTANTLY_API_KEY"] = api_key
        self.email_provider = "instantly"

        print("\n  Testing API key...", end=" ", flush=True)
        if await _test_instantly(api_key):
            print("✓ Connected")
            _say("Connected. Approved campaigns will go out through Instantly.")
        else:
            print("✗ Failed")
            _say("Couldn't connect — the key may be wrong, or API access may")
            _say("need Instantly's Growth plan. Saving it anyway; fix it in .env.")

        _say("Note: human review stays ON. Nothing is sent until you approve")
        _say("it in the dashboard or with 'openvz-leads review approve <id>'.")

    def _setup_linkedin(self):
        """Configure LinkedIn credentials."""
        _say("LinkedIn can be searched for prospects matching your ICP.")
        _say("That means logging into your LinkedIn account in a browser, with")
        _say("conservative rate limits and human-like pacing.")
        print("\n  ⚠  Heads up: automating LinkedIn violates their Terms of Service.")
        print("     Accounts doing this can be restricted or banned. Only use an")
        print("     account you're comfortable putting at risk, and keep the")
        print("     default rate limits. This is entirely optional.")

        use_linkedin = _ask_yes_no("Set up LinkedIn prospecting anyway?", default=False)

        if use_linkedin:
            email = _ask("LinkedIn email/username")
            password = _ask("LinkedIn password (input hidden)", secret=True)
            self.env_vars["LINKEDIN_EMAIL"] = email
            self.env_vars["LINKEDIN_PASSWORD"] = password

            _say("Got it. I'll stay well under the rate limits and behave")
            _say("like a careful human, but the ToS risk is yours to own.")
        else:
            _say("That's fine. I can still find prospects via Google")
            _say("and company websites. You can add LinkedIn later.")
            self.env_vars["LINKEDIN_EMAIL"] = ""
            self.env_vars["LINKEDIN_PASSWORD"] = ""

    def _setup_cloudflare(self):
        """Configure Cloudflare Browser Rendering for deep crawling."""
        _say("When I train on a product website, I can crawl it deeply")
        _say("using Cloudflare's Browser Rendering API. This means I can")
        _say("handle JavaScript-heavy sites and crawl hundreds of pages.")
        _say("It's $5/month for ~12,000 pages. Totally optional.")

        use_cloudflare = _ask_yes_no("Set up Cloudflare crawling?", default=False)

        if use_cloudflare:
            account_id = _ask("Cloudflare Account ID")
            api_token = _ask("Cloudflare API Token (needs Browser Rendering - Edit permission; input hidden)", secret=True)
            self.env_vars["CLOUDFLARE_ACCOUNT_ID"] = account_id
            self.env_vars["CLOUDFLARE_API_TOKEN"] = api_token
            _say("Nice. I'll use Cloudflare for deep crawling during training.")
        else:
            _say("No problem. I'll use my built-in crawler instead. It works")
            _say("great for most sites — just can't render JavaScript.")
            self.env_vars["CLOUDFLARE_ACCOUNT_ID"] = ""
            self.env_vars["CLOUDFLARE_API_TOKEN"] = ""

    async def _setup_product(self):
        """Train OpenVZ Leads on the product — either via URL or manual entry."""
        _say("Now the important part — I need to learn about what you're selling.")
        _say("Your website can be crawled to work this out automatically,")
        _say("or you can tell me the basics manually.\n")

        method = _ask(
            "Train from website URL or enter manually? (url/manual)",
            default="url",
        ).lower()

        if method in ("url", "u", "website"):
            url = _ask("Your product's website URL (e.g. https://yourcompany.com)")

            _say(f"Give me a minute — I'm going to crawl {url}")
            _say("and learn everything I can about your product.\n")

            from openvz_leads.trainer import Trainer
            trainer = Trainer()
            self.config = await trainer.train(url, str(CONFIG_FILE)) or {}

            if self.config:
                _say("Training complete! I've got a solid understanding")
                _say("of your product now. Check openvz-leads.yaml to review")
                _say("what I learned — tweak anything that looks off.")
                return

            _say("Hmm, had trouble with that URL. Let's do it manually.")

        # Manual entry
        _say("A few questions about your product, then.\n")

        company = _ask("Company name")
        product = _ask("Product/service name")
        description = _ask("One-line description (what does it do, who is it for?)")
        pricing = _ask("Pricing info", default="Contact for pricing")

        print("\n  Enter 3-5 key benefits (one per line, empty line to finish):")
        benefits = []
        while True:
            b = _ask(f"  Benefit {len(benefits) + 1}", required=False)
            if not b:
                break
            benefits.append(b)

        _say("Now let's define your ideal customer.\n")

        industries = _ask("Target industries (comma-separated)").split(",")
        industries = [i.strip() for i in industries if i.strip()]

        titles = _ask("Decision-maker titles (comma-separated)").split(",")
        titles = [t.strip() for t in titles if t.strip()]

        company_size = _ask("Target company size", default="10-200 employees")
        geography = _ask("Target geography", default="United States").split(",")
        geography = [g.strip() for g in geography if g.strip()]

        _say("Now let's talk about your offer — what happens when someone's interested?\n")

        primary_offer = _ask(
            "What's your main offer? (e.g., 'SaaS subscription at $99/mo', 'Marketing retainer')",
            required=False,
        )
        entry_offer = _ask(
            "Any low-commitment entry offer? (e.g., 'Free trial', 'Free audit', 'Sample report')",
            default="",
            required=False,
        )

        print("\n  What's the goal when someone shows interest?")
        print("    1. Book a call")
        print("    2. Start a free trial")
        print("    3. Just get a reply / start a conversation")
        goal_choice = _ask("Goal (1/2/3)", default="1")
        goal_map = {"1": "book_call", "2": "start_trial", "3": "get_reply"}
        goal = goal_map.get(goal_choice, "book_call")

        booking_method = "suggest_times"
        booking_url = ""
        meeting_duration = "15 minutes"
        meeting_owner = ""

        if goal == "book_call":
            has_calendar = _ask_yes_no("Do you have a calendar booking link (Calendly, Cal.com, etc.)?")
            if has_calendar:
                booking_url = _ask("Booking URL")
                booking_method = "calendar_link"
            else:
                print("\n  How should OpenVZ Leads suggest meeting times?")
                print("    1. Suggest specific times ('How about Thursday at 2pm?')")
                print("    2. Ask for their preference ('What does your calendar look like?')")
                method_choice = _ask("Method (1/2)", default="1")
                booking_method = "suggest_times" if method_choice == "1" else "ask_preference"

            meeting_duration = _ask("How long is the call?", default="15 minutes")
            meeting_owner = _ask("Who takes the meeting? (your name or role)", required=False)

        _say("What persona should I use for outreach?\n")

        persona_name = _ask("My name (what prospects see)", default="OpenVZ Leads")
        persona_email = _ask("My email address")
        persona_role = _ask("My title/role", default="Business Development")

        self.config = {
            "persona": {
                "name": persona_name,
                "company": company,
                "role": persona_role,
                "email": persona_email,
                "linkedin": "",
                "tone": "professional, consultative, confident",
            },
            "product": {
                "name": product,
                "description": description,
                "pricing": pricing,
                "key_benefits": benefits or ["Benefit 1"],
                "objection_responses": {},
                "offer": {
                    "primary": primary_offer or "",
                    "entry": entry_offer or "",
                    "goal": goal,
                    "booking_method": booking_method,
                    "booking_url": booking_url,
                    "meeting_duration": meeting_duration,
                    "meeting_owner": meeting_owner or "",
                },
            },
            "icp": {
                "industries": industries or ["Technology"],
                "company_size": company_size,
                "titles": titles or ["VP", "Director"],
                "geography": geography,
            },
            "channels": {
                "email": {
                    "enabled": True,
                    "provider": getattr(self, "email_provider", "none"),
                    "max_daily_sends": 50,
                },
                "linkedin": {
                    "enabled": bool(self.env_vars.get("LINKEDIN_EMAIL")),
                    "max_daily_connections": 20,
                    "max_daily_messages": 10,
                },
            },
            "review": {
                "require_approval": True,
                "auto_reply": False,
            },
            "profiling": {
                "enabled": True,
                "min_score": 5,
                "max_per_cycle": 5,
                "output_language": getattr(self, "brief_language", "English"),
                "fetch_website": True,
            },
            "usage": {
                "max_daily_claude_percent": 80,
                "heartbeat_interval_minutes": 15,
                "quiet_hours": {
                    "start": "22:00",
                    "end": "07:00",
                    "timezone": "America/New_York",
                },
            },
        }

        _say("Got it — that's everything needed about your product.")

    def _setup_behavior(self):
        """Configure operational behaviour."""
        _say("Last thing — a few settings for how it runs.\n")

        # Language of the written account analysis
        print("  Account briefs can be written in any language. The outreach")
        print("  emails keep the language of your product copy — this is only")
        print("  the analysis you read.")
        self.brief_language = _ask(
            "Language for account briefs (e.g. English, 简体中文, 日本語)",
            default="English",
        ) or "English"
        if self.config:
            self.config.setdefault("profiling", {})["output_language"] = self.brief_language

        # Usage limit
        print("\n  Claude usage is tracked so it doesn't eat your whole daily quota.")
        usage_pct = _ask(
            "Max % of daily Claude usage it may consume",
            default="80",
        )
        try:
            usage_pct = min(100.0, max(1.0, float(usage_pct)))
        except ValueError:
            print("    (didn't catch that — using 80)")
            usage_pct = 80.0

        # Heartbeat interval
        print("\n  It wakes on a schedule to check for work.")
        interval = _ask(
            "How often should it check for work (minutes)?",
            default="15",
        )
        try:
            interval = max(1, int(interval))
        except ValueError:
            print("    (didn't catch that — using 15)")
            interval = 15

        # Quiet hours
        print("\n  I won't send emails or do outreach during quiet hours.")
        quiet_start = _ask("Quiet hours start (24h format)", default="22:00")
        quiet_end = _ask("Quiet hours end (24h format)", default="07:00")
        timezone = _ask("Your timezone", default="America/New_York")

        # Daily send limit
        daily_sends = _ask(
            "Max emails to send per day",
            default="50",
        )
        try:
            daily_sends = max(1, int(daily_sends))
        except ValueError:
            print("    (didn't catch that — using 50)")
            daily_sends = 50
        if daily_sends > 200:
            print("\n  ⚠  200+ sends/day from a fresh domain will torch your")
            print("     deliverability. Warm up gradually (see README).")

        # Update config
        if self.config:
            self.config["usage"] = {
                "max_daily_claude_percent": usage_pct,
                "heartbeat_interval_minutes": interval,
                "quiet_hours": {
                    "start": quiet_start,
                    "end": quiet_end,
                    "timezone": timezone,
                },
            }
            if "channels" in self.config and "email" in self.config["channels"]:
                self.config["channels"]["email"]["max_daily_sends"] = daily_sends

        _say("Perfect. All configured.")

    def _write_env(self):
        """Write the .env file, preserving any existing variables we didn't touch."""
        existing: dict[str, str] = {}
        if ENV_FILE.exists():
            for line in ENV_FILE.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    existing[key.strip()] = value.strip()

        # Wizard answers win, but never blank out a previously-set value
        # (e.g. re-running setup and skipping the LinkedIn step).
        merged = dict(existing)
        for key, value in self.env_vars.items():
            if value or key not in merged:
                merged[key] = value

        lines = [f"{key}={value}" for key, value in merged.items()]
        ENV_FILE.write_text("\n".join(lines) + "\n")
        try:
            ENV_FILE.chmod(0o600)  # credentials — owner read/write only
        except OSError:
            pass
        logger.info(f"Wrote {ENV_FILE}")

    def _write_config(self):
        """Write openvz-leads.yaml (only if we have manual config — trainer writes its own)."""
        if self.config and not CONFIG_FILE.exists():
            with open(CONFIG_FILE, "w") as f:
                yaml.dump(self.config, f, default_flow_style=False, sort_keys=False)
            logger.info(f"Wrote {CONFIG_FILE}")
        elif self.config:
            # Config already exists (from trainer), update behavior settings only
            try:
                with open(CONFIG_FILE) as f:
                    existing = yaml.safe_load(f) or {}
                existing["usage"] = self.config.get("usage", existing.get("usage", {}))
                if "channels" in self.config:
                    existing.setdefault("channels", {})
                    if "email" in self.config["channels"]:
                        existing["channels"].setdefault("email", {})
                        existing["channels"]["email"]["max_daily_sends"] = (
                            self.config["channels"]["email"].get("max_daily_sends", 50)
                        )
                with open(CONFIG_FILE, "w") as f:
                    yaml.dump(existing, f, default_flow_style=False, sort_keys=False)
            except Exception:
                pass

    async def _print_summary(self):
        """Print final summary and next steps."""
        print(f"""
╔══════════════════════════════════════════════════════════╗
║                                                          ║
║   Setup Complete!                                        ║
║                                                          ║
╚══════════════════════════════════════════════════════════╝
""")

        # Show what's configured
        sending_on = bool(self.env_vars.get("INSTANTLY_API_KEY"))
        checks = {
            "Claude Code CLI": _check_claude_cli(),
            "Product configured": CONFIG_FILE.exists(),
            "LinkedIn prospecting": bool(self.env_vars.get("LINKEDIN_EMAIL")),
            "Deep website crawl": bool(self.env_vars.get("CLOUDFLARE_ACCOUNT_ID")),
            "Sending provider (optional)": sending_on,
        }

        for name, ok in checks.items():
            status = "✓" if ok else "–"
            print(f"  {status} {name}")

        print(f"""
  Files created:
    - .env (your credentials)
    - openvz-leads.yaml (your configuration)

  Start it:
    openvz-leads run          (or: python -m openvz_leads)

  Watch it work:
    openvz-leads dashboard    → http://localhost:5555

  Approve what it drafts:
    openvz-leads review list

  Take the results elsewhere:
    openvz-leads export leads --format csv
    openvz-leads export profiles --format markdown
    openvz-leads export emails --format markdown

  Re-configure for a different product:
    openvz-leads train https://newproduct.com
""")

        if sending_on:
            print("""  Before you send a single email:
    1. Use a dedicated sending domain (not your main domain) with
       SPF, DKIM, and DMARC configured.
    2. Warm it up 2-4 weeks before real volume.
    3. Every campaign needs a working unsubscribe and your physical
       mailing address (CAN-SPAM). GDPR/PECR add more if you email
       into the EU/UK — a named legal basis, and in most cases prior
       consent for individuals.
    See the "Legal & Deliverability" section of the README.
""")
        else:
            print("""  Draft-only mode: nothing will be sent. Outreach is written and
  queued for your review, and you export it from there.
""")

        _say("Ready. Run 'openvz-leads run' to start finding accounts.\n")


async def run_setup():
    wizard = SetupWizard()
    await wizard.run()


def main():
    asyncio.run(run_setup())


if __name__ == "__main__":
    main()
