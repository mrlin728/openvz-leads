"""OpenVZ Leads' main heartbeat loop. Find them. Understand them. Reach them."""

import asyncio
import logging
import signal
import sys
from datetime import datetime, time, timedelta

import pytz

from openvz_leads.brain import Brain
from openvz_leads.config import ConfigError, load_config, load_env, LeadsConfig
from openvz_leads.state import StateManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("openvz_leads")

# Backoff for consecutive failed cycles: 60s, 120s, 240s, ... capped at 15 min
ERROR_BACKOFF_BASE = 60
ERROR_BACKOFF_CAP = 900


def in_quiet_hours(config: LeadsConfig) -> bool:
    """Check if we're currently in quiet hours."""
    qh = config.usage.quiet_hours
    tz = pytz.timezone(qh.timezone)
    now = datetime.now(tz).time()
    start = time.fromisoformat(qh.start)
    end = time.fromisoformat(qh.end)

    if start <= end:
        return start <= now <= end
    else:
        # Quiet hours cross midnight (e.g., 22:00 - 07:00)
        return now >= start or now <= end


def seconds_until_quiet_hours_end(config: LeadsConfig) -> int:
    """Calculate seconds until quiet hours end."""
    qh = config.usage.quiet_hours
    tz = pytz.timezone(qh.timezone)
    now = datetime.now(tz)
    end = time.fromisoformat(qh.end)
    end_today = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)

    if end_today <= now:
        # End time is tomorrow
        end_today = end_today + timedelta(days=1)

    delta = end_today - now
    return max(int(delta.total_seconds()), 60)


async def decide_next_action(
    brain: Brain,
    state: StateManager,
    config: LeadsConfig,
    summary: dict | None = None,
) -> str:
    """Decide what OpenVZ Leads should do next based on current state.

    Deterministic priority rules, not a Claude call — the decision is fully
    derivable from pipeline counts, so spending budget (and a fragile LLM
    string-parse) on it would be waste.

    The order mirrors the product pipeline: find → analyse → draft → (human
    reviews) → send. Campaigns sitting in the review queue are deliberately
    *not* an action: they are waiting on a person, not on the agent.
    """
    if summary is None:
        summary = await state.get_state_summary()

    prospects = summary.get("prospects") or {}
    new_prospects = prospects.get("new", 0) if isinstance(prospects, dict) else 0
    approved_campaigns = summary.get("approved_campaigns", 0) or 0
    pending_review = summary.get("pending_review", 0) or 0
    open_conversations = summary.get("open_conversations", 0) or 0

    can_send = config.channels.email.sending_enabled

    needs_profiling = False
    if config.profiling.enabled:
        # Same filter the Profiler itself uses, so a backlog of low-scoring
        # prospects it will never touch can't stall the rest of the pipeline.
        needs_profiling = bool(
            await state.get_prospects_needing_profile(
                min_score=config.profiling.min_score, limit=1
            )
        )

    if open_conversations > 0:
        action, reason = "handle_replies", f"{open_conversations} open conversation(s) waiting"
    elif approved_campaigns > 0 and can_send:
        action, reason = (
            "send_campaign",
            f"{approved_campaigns} approved campaign(s) ready to deploy",
        )
    elif needs_profiling:
        action, reason = "profile", "prospects are waiting to be analysed"
    elif new_prospects > 0:
        action, reason = "write_campaign", f"{new_prospects} analysed prospect(s) need outreach"
    elif new_prospects < 20:
        action, reason = "prospect", f"only {new_prospects} new prospect(s); pipeline needs leads"
    else:
        action, reason = "idle", "pipeline is healthy; running analysis"

    logger.info(f"Decision: {action} ({reason})")
    if pending_review:
        logger.info(
            f"{pending_review} campaign(s) are waiting for your review — "
            "run 'openvz-leads review list' or open the dashboard."
        )
    return action


async def _interruptible_sleep(seconds: float, stop_event: asyncio.Event) -> bool:
    """Sleep up to `seconds`, waking immediately on shutdown.

    Returns True if a shutdown was requested during the sleep.
    """
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def heartbeat(stop_event: asyncio.Event | None = None):
    """OpenVZ Leads' main loop. Wakes up, decides, acts, sleeps. Repeat."""
    if stop_event is None:
        stop_event = asyncio.Event()

    logger.info("=" * 60)
    logger.info("OpenVZ Leads is online. Find them. Understand them. Reach them.")
    logger.info("=" * 60)

    try:
        config = load_config()
    except (ConfigError, Exception) as e:
        if isinstance(e, (KeyboardInterrupt, asyncio.CancelledError)):
            raise
        logger.error(f"Cannot start — configuration error:\n{e}")
        return
    env = load_env()
    state = StateManager()
    brain = Brain(state)

    await state.init_db()
    logger.info("Database initialized.")

    # Import agents here to avoid circular imports
    from openvz_leads.agents.scout import Scout
    from openvz_leads.agents.profiler import Profiler
    from openvz_leads.agents.writer import Writer
    from openvz_leads.agents.sender import Sender
    from openvz_leads.agents.handler import Handler
    from openvz_leads.agents.analyst import Analyst

    scout = Scout(brain, state, config, env)
    profiler = Profiler(brain, state, config)
    writer = Writer(brain, state, config)
    sender = Sender(brain, state, config, env)
    handler = Handler(brain, state, config, env)
    analyst = Analyst(state)

    if not config.channels.email.sending_enabled:
        logger.info(
            "Draft-only mode: no outbound provider configured. "
            "Prospects are found and analysed, outreach is drafted and queued "
            "for review, and nothing is ever sent. Export with "
            "'openvz-leads export'."
        )
    elif config.review.require_approval:
        logger.info("Human review is ON — no campaign is sent until you approve it.")
    else:
        logger.warning(
            "Human review is OFF — approved campaigns will be sent automatically."
        )

    interval = config.usage.heartbeat_interval_minutes * 60
    max_calls = max(int(200 * (config.usage.max_daily_claude_percent / 100)), 1)
    consecutive_errors = 0

    while not stop_event.is_set():
        try:
            # 1. Check quiet hours
            if in_quiet_hours(config):
                sleep_for = seconds_until_quiet_hours_end(config)
                logger.info(f"Quiet hours. Sleeping for {sleep_for // 60} minutes.")
                if await _interruptible_sleep(sleep_for, stop_event):
                    break
                continue

            # 2. Check usage budget
            if not await brain.is_within_budget(max_calls):
                logger.info(
                    f"Daily usage limit reached ({max_calls} calls). "
                    "Sleeping 1h, then re-checking (resets at midnight)."
                )
                if await _interruptible_sleep(3600, stop_event):
                    break
                continue

            # 3. Decide what to do
            logger.info("Checking pipeline state...")
            summary = await state.get_state_summary()
            action = await decide_next_action(brain, state, config, summary=summary)

            # 4. Execute — run independent agents in parallel where possible
            # Handler is always safe to run alongside other agents
            tasks = []
            has_open_convos = summary.get("open_conversations", 0) > 0

            if action == "handle_replies":
                tasks.append(("handle_replies", handler.run()))
            elif action == "prospect":
                tasks.append(("prospect", scout.run()))
                # Also handle replies in parallel if needed
                if has_open_convos:
                    tasks.append(("handle_replies", handler.run()))
                # Analyst is cheap (no Claude calls) — keep analytics fresh
                tasks.append(("analyze", analyst.run()))
            elif action == "profile":
                tasks.append(("profile", profiler.run()))
                if has_open_convos:
                    tasks.append(("handle_replies", handler.run()))
            elif action == "write_campaign":
                tasks.append(("write_campaign", writer.run()))
                if has_open_convos:
                    tasks.append(("handle_replies", handler.run()))
            elif action == "send_campaign":
                tasks.append(("send_campaign", sender.run()))
            elif action == "idle":
                tasks.append(("analyze", analyst.run()))

            if len(tasks) > 1:
                logger.info(f"Running {len(tasks)} agents in parallel: {[t[0] for t in tasks]}")

            # Run all tasks, catch errors per-task so one bad agent
            # never takes down the cycle
            results = await asyncio.gather(
                *[t[1] for t in tasks], return_exceptions=True
            )
            for (name, _), result in zip(tasks, results):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, Exception):
                    logger.error(f"Agent {name} failed: {result}", exc_info=result)

            # 5. Log the action (best-effort; never kills the loop)
            try:
                await state.log_action(action_type=action, agent="main")
            except Exception as e:
                logger.warning(f"Failed to log action '{action}': {e}")

            consecutive_errors = 0

            # 6. Sleep until next heartbeat
            logger.info(
                f"Cycle complete. Sleeping for {config.usage.heartbeat_interval_minutes} minutes."
            )
            if await _interruptible_sleep(interval, stop_event):
                break

        except (KeyboardInterrupt, asyncio.CancelledError):
            break
        except Exception as e:
            consecutive_errors += 1
            backoff = min(
                ERROR_BACKOFF_BASE * (2 ** (consecutive_errors - 1)),
                ERROR_BACKOFF_CAP,
            )
            logger.error(
                f"Error in heartbeat (failure #{consecutive_errors}): {e}",
                exc_info=True,
            )
            logger.info(f"Recovering... sleeping {backoff}s before retry.")
            if await _interruptible_sleep(backoff, stop_event):
                break

    logger.info("OpenVZ Leads shutting down. Deals don't close themselves, but I need a break.")


def _needs_setup() -> bool:
    """Check if OpenVZ Leads needs first-time setup."""
    from pathlib import Path
    project_root = Path(__file__).parent.parent
    env_file = project_root / ".env"
    config_file = project_root / "openvz-leads.yaml"

    # If .env doesn't exist, definitely needs setup
    if not env_file.exists():
        return True

    # If config still has placeholder values, needs setup
    if config_file.exists():
        try:
            with open(config_file) as f:
                import yaml
                config = yaml.safe_load(f)
            if not isinstance(config, dict):
                return True
            company = (config.get("persona") or {}).get("company", "")
            if company in ("Your Company", ""):
                return True
        except Exception:
            return True
    else:
        return True

    return False


async def _run_with_signals():
    """Run the heartbeat with SIGINT/SIGTERM wired to a graceful shutdown."""
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_shutdown(sig_name: str):
        if stop_event.is_set():
            logger.info("Second shutdown signal — exiting immediately.")
            sys.exit(1)
        logger.info(f"Received {sig_name}. Finishing current work, then shutting down...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig.name)
        except (NotImplementedError, RuntimeError):
            # Windows / non-main-thread fallback
            signal.signal(sig, lambda s, f: _request_shutdown(signal.Signals(s).name))

    await heartbeat(stop_event)


def main():
    """Entry point."""
    # Check for first-time setup
    if _needs_setup():
        print("\n  First time running OpenVZ Leads? Let's get you set up.\n")
        from openvz_leads.setup import run_setup
        asyncio.run(run_setup())
        return

    try:
        asyncio.run(_run_with_signals())
    except KeyboardInterrupt:
        logger.info("Goodbye.")


if __name__ == "__main__":
    main()
