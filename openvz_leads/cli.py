"""OpenVZ Leads CLI — simple commands to install, setup, run, and manage OpenVZ Leads."""

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path


def cmd_install(args):
    """Install all dependencies including Playwright browsers."""
    print("\n  Installing OpenVZ Leads dependencies...\n")

    # Install Python packages
    print("  [1/2] Installing Python packages...")
    requirements = Path(args._project_root) / "requirements.txt"
    if requirements.exists():
        pip_args = ["-r", "requirements.txt"]
    else:
        # Fall back to an editable install from pyproject.toml
        pip_args = ["-e", "."]
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", *pip_args],
        cwd=args._project_root,
    )
    if result.returncode != 0:
        print("\n  Failed to install Python packages.")
        print("  Tip: make sure you're inside a virtualenv "
              "(python3 -m venv .venv && source .venv/bin/activate).")
        sys.exit(1)
    print("  ✓ Python packages installed.\n")

    # Install Playwright browsers
    print("  [2/2] Installing Playwright browsers...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            timeout=600,
        )
        playwright_ok = result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        playwright_ok = False
    if not playwright_ok:
        print("\n  Playwright browser install failed (optional — needed for LinkedIn).")
    else:
        print("  ✓ Playwright browsers installed.\n")

    print("  OpenVZ Leads is installed. Run 'openvz-leads setup' next.\n")


def cmd_setup(args):
    """Run the interactive setup wizard."""
    from openvz_leads.setup import run_setup

    asyncio.run(run_setup())


def cmd_run(args):
    """Start OpenVZ Leads' heartbeat loop."""
    from openvz_leads.main import main

    main()


def cmd_train(args):
    """Train OpenVZ Leads on a website."""
    from openvz_leads.trainer import Trainer

    url = args.url.strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
        print(f"  No scheme given — using {url}")

    if args.max_pages < 1:
        print("  max_pages must be at least 1.")
        sys.exit(2)

    trainer = Trainer()
    asyncio.run(trainer.train(url, max_pages=args.max_pages))


def cmd_dashboard(args):
    """Launch the local web dashboard."""
    from openvz_leads.dashboard import start_dashboard

    if not 1 <= args.port <= 65535:
        print(f"  Invalid port: {args.port}. Must be 1-65535.")
        sys.exit(2)

    start_dashboard(host=args.host, port=args.port)


def cmd_status(args):
    """Show current pipeline status."""
    from openvz_leads.state import StateManager

    async def _status():
        state = StateManager()
        await state.init_db()
        summary = await state.get_state_summary()

        print("\n  OpenVZ Leads — Pipeline Status")
        print("  " + "=" * 44)
        print(f"  Prospects:              {summary['prospects']}")
        print(f"  Waiting to be analysed: {summary['unprofiled_prospects']}")
        print(f"  Awaiting your review:   {summary['pending_review']}")
        print(f"  Approved, ready to go:  {summary['approved_campaigns']}")
        print(f"  Active campaigns:       {summary['active_campaigns']}")
        print(f"  Open conversations:     {summary['open_conversations']}")
        print(f"  Claude calls today:     {summary['usage_today']}")
        if summary["pending_review"]:
            print("\n  Review them with: openvz-leads review list")
        print()

    asyncio.run(_status())


def cmd_export(args):
    """Export leads, account briefs or outreach drafts to a file."""
    from openvz_leads.exporter import ExportError, Exporter
    from openvz_leads.state import StateManager

    async def _export():
        state = StateManager()
        await state.init_db()
        try:
            path = await Exporter(state).export(
                dataset=args.dataset, fmt=args.format, out_path=args.out
            )
        except ExportError as e:
            print(f"\n  {e}\n")
            sys.exit(1)
        print(f"\n  Exported to {path}\n")

    asyncio.run(_export())


def cmd_review(args):
    """List, read, approve or reject campaigns waiting for review."""
    from openvz_leads.state import StateManager

    async def _review():
        state = StateManager()
        await state.init_db()

        if args.review_command == "list":
            pending = await state.get_campaigns_by_status("pending_review")
            if not pending:
                print("\n  Nothing is waiting for review.\n")
                return
            print(f"\n  {len(pending)} campaign(s) awaiting review")
            print("  " + "=" * 44)
            for c in pending:
                print(f"  {c.id}")
                print(f"    {c.name} — {len(c.sequence)} email(s), "
                      f"{len(c.prospect_ids)} recipient(s)")
                if c.sequence:
                    print(f"    First subject: {c.sequence[0].subject}")
            print("\n  Read one:    openvz-leads review show <id>")
            print("  Approve:     openvz-leads review approve <id>")
            print("  Reject:      openvz-leads review reject <id> --note \"why\"\n")
            return

        campaign = await state.get_campaign(args.campaign_id)
        if campaign is None:
            print(f"\n  No campaign with id '{args.campaign_id}'.\n")
            sys.exit(1)

        if args.review_command == "show":
            print(f"\n  {campaign.name}  [{campaign.status}]")
            print("  " + "=" * 44)
            print(f"  Recipients: {len(campaign.prospect_ids)}")
            for step in campaign.sequence:
                when = ("sends immediately" if not step.delay_days
                        else f"{step.delay_days} day(s) later")
                print(f"\n  --- Email {step.step} ({when}) ---")
                print(f"  Subject: {step.subject}")
                print("")
                for line in step.body.splitlines():
                    print(f"  {line}")
            print()
            return

        approved = args.review_command == "approve"
        ok = await state.review_campaign(
            campaign.id, approved=approved, note=args.note or ""
        )
        if not ok:
            print(f"\n  Campaign '{campaign.name}' is '{campaign.status}' — "
                  "only campaigns awaiting review can be decided.\n")
            sys.exit(1)
        verb = "approved" if approved else "rejected"
        print(f"\n  {campaign.name} {verb}.")
        if approved:
            print("  It will be sent on the next cycle if an outbound channel "
                  "is configured;\n  otherwise export it with "
                  "'openvz-leads export emails'.\n")
        else:
            print()

    asyncio.run(_review())


def main():
    # Imported here (not at module scope) so `--help` and the light commands
    # don't pay for pulling in the database layer.
    from openvz_leads.exporter import DATASETS, FORMATS

    project_root = str(Path(__file__).parent.parent)

    parser = argparse.ArgumentParser(
        prog="openvz-leads",
        description=(
            "OpenVZ Leads — find customers, analyse them, draft the outreach. "
            "Nothing is sent without your approval."
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    # openvz-leads install
    sub = subparsers.add_parser("install", help="Install dependencies")
    sub.set_defaults(func=cmd_install)

    # openvz-leads setup
    sub = subparsers.add_parser("setup", help="Run the interactive setup wizard")
    sub.set_defaults(func=cmd_setup)

    # openvz-leads run
    sub = subparsers.add_parser("run", help="Start OpenVZ Leads' heartbeat loop")
    sub.set_defaults(func=cmd_run)

    # openvz-leads train <url>
    sub = subparsers.add_parser("train", help="Train OpenVZ Leads on a website")
    sub.add_argument("url", help="Website URL to crawl and learn from")
    sub.add_argument(
        "max_pages",
        nargs="?",
        type=int,
        default=100,
        help="Max pages to crawl (default: 100)",
    )
    sub.set_defaults(func=cmd_train)

    # openvz-leads dashboard
    sub = subparsers.add_parser("dashboard", help="Open the web dashboard")
    sub.add_argument("--host", default="127.0.0.1", help="Host (default: 127.0.0.1)")
    sub.add_argument("--port", type=int, default=5555, help="Port (default: 5555)")
    sub.set_defaults(func=cmd_dashboard)

    # openvz-leads status
    sub = subparsers.add_parser("status", help="Show pipeline status")
    sub.set_defaults(func=cmd_status)

    # openvz-leads review <list|show|approve|reject>
    sub = subparsers.add_parser(
        "review", help="Review the outreach drafts waiting for your approval"
    )
    review_subs = sub.add_subparsers(dest="review_command")
    review_subs.add_parser("list", help="List campaigns awaiting review")
    for name, helptext in (
        ("show", "Print a campaign's full email sequence"),
        ("approve", "Approve a campaign for sending"),
        ("reject", "Reject a campaign"),
    ):
        rsub = review_subs.add_parser(name, help=helptext)
        rsub.add_argument("campaign_id", help="Campaign id (from 'review list')")
        if name != "show":
            rsub.add_argument("--note", default="", help="Why (saved with the decision)")
    sub.set_defaults(func=cmd_review, review_command="list", note="")

    # openvz-leads export <dataset> --format <fmt>
    sub = subparsers.add_parser(
        "export", help="Export leads, account briefs or outreach drafts"
    )
    sub.add_argument(
        "dataset",
        nargs="?",
        default="leads",
        choices=list(DATASETS),
        help="What to export (default: leads)",
    )
    sub.add_argument(
        "--format",
        default="csv",
        choices=list(FORMATS),
        help="Output format (default: csv)",
    )
    sub.add_argument("--out", default="", help="Write here instead of data/exports/")
    sub.set_defaults(func=cmd_export)

    args = parser.parse_args()
    args._project_root = project_root

    if args.command is None:
        parser.print_help()
        print("\n  Quick start:")
        print("    openvz-leads install   — Install dependencies")
        print("    openvz-leads setup     — Configure OpenVZ Leads (first time)")
        print("    openvz-leads run       — Find, analyse and draft, on a loop")
        print("    openvz-leads dashboard — Open the web dashboard")
        print("    openvz-leads review    — Approve the drafts it wrote")
        print("    openvz-leads export    — Take leads and drafts elsewhere")
        print()
        sys.exit(0)

    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\n  Interrupted. Goodbye.")
        sys.exit(130)
    except Exception as e:
        # ConfigError and friends carry actionable messages — show them
        # cleanly instead of a raw traceback.
        from openvz_leads.config import ConfigError

        if isinstance(e, ConfigError):
            print(f"\n  Configuration problem:\n  {e}\n")
        else:
            print(f"\n  Error running 'openvz-leads {args.command}': {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
