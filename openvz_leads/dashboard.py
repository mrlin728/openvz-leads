"""OpenVZ Leads Dashboard — local web UI to set up, control, and monitor OpenVZ Leads."""

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

import aiosqlite
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from openvz_leads import paths, pipeline
from openvz_leads.state import _default_db_path

logger = logging.getLogger("openvz_leads.dashboard")

PROJECT_ROOT = paths.workspace()
# Honour the same OPENVZ_LEADS_DB override the agent uses, so the dashboard
# never ends up reading a different database than the one being written.
DB_PATH = _default_db_path()
ENV_FILE = paths.env_file()
CONFIG_FILE = paths.config_file()
PID_FILE = paths.data_dir() / "leads.pid"
LOG_FILE = paths.data_dir() / "leads.log"

app = FastAPI(title="OpenVZ Leads Dashboard")

# OpenVZ Leads process tracking
_agent_process: subprocess.Popen | None = None
_agent_started_at: datetime | None = None
_env_lock = asyncio.Lock()


# ── Helpers ──


async def query_db(sql: str, params: tuple = ()) -> list[dict]:
    """Run a query and return results as list of dicts.

    Never raises: a missing DB file, missing table, or malformed schema
    returns [] so no dashboard route can 500 on an empty install.
    """
    if not DB_PATH.exists():
        return []
    try:
        async with aiosqlite.connect(str(DB_PATH)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("query_db failed (%s): %s", sql.split(None, 4)[:4], e)
        return []


def _mask_key(key: str) -> str:
    """Mask an API key for display: show first 4 and last 4 chars."""
    if not key or len(key) < 10:
        return "****" if key else ""
    return key[:4] + "****" + key[-4:]


def _read_env_file() -> dict[str, str]:
    """Read .env file and return as dict."""
    env_vars = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env_vars[key.strip()] = value.strip()
    return env_vars


def _write_env_file(updates: dict[str, str]):
    """Update .env file with new values, preserving existing entries."""
    existing = _read_env_file()
    existing.update(updates)
    lines = [f"{k}={v}" for k, v in existing.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    load_dotenv(str(ENV_FILE), override=True)


def _check_agent_pid() -> int | None:
    """Check if there's a running OpenVZ Leads process from a PID file."""
    global _agent_process, _agent_started_at
    if _agent_process and _agent_process.poll() is None:
        return _agent_process.pid
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)  # Check if process exists
            return pid
        except (ValueError, ProcessLookupError, PermissionError):
            PID_FILE.unlink(missing_ok=True)
    return None


# ── Setup Status ──


@app.get("/api/setup-status")
async def get_setup_status():
    """Check what's configured and what still needs setup.

    "Required" here means required *to produce something useful*, which is a
    much shorter list than it used to be: Claude, and a product to sell. An
    outbound provider is not on it — the whole pipeline runs without one and
    you export the results — and listing Instantly as required told every new
    user they were 0% set up while holding a working install.
    """
    import shutil

    checks = []

    # 1. Claude Code CLI — the one genuinely non-negotiable dependency.
    claude_found = shutil.which("claude") is not None
    checks.append({
        "id": "claude_cli", "label": "Claude Code CLI",
        "done": claude_found,
        "required": True,
        "help": "This is the thinking engine, and it runs on the Claude "
                "subscription you already have. Install it from "
                "https://claude.ai/download, then run: claude login",
    })

    # 2. Python environment — only meaningful when running from source. A
    #    bundled app carries its own interpreter, so asking someone who
    #    double-clicked a .dmg to create a virtualenv is nonsense.
    if not paths.is_frozen():
        checks.append({
            "id": "venv", "label": "Python virtual environment",
            "done": (PROJECT_ROOT / ".venv").is_dir(),
            "required": True,
            "help": "Run: python3 -m venv .venv && source .venv/bin/activate && pip install -e .",
        })

    # 3. Product configured — it cannot write outreach for a product it has
    #    never heard of.
    config_valid = False
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            company = (cfg.get("persona") or {}).get("company", "")
            product = (cfg.get("product") or {}).get("name", "")
            config_valid = company not in ("Your Company", "") and product not in ("Your Product", "")
        except Exception:
            pass
    checks.append({
        "id": "config", "label": "Your product configured",
        "done": config_valid,
        "required": True,
        "help": "Point it at your own site and let it learn: "
                "openvz-leads train https://your-company.com — or ask Claude "
                "to walk you through it.",
    })

    # 4. Product knowledge — written by the trainer alongside the config.
    product_trained = (paths.skills_dir() / "product_knowledge.md").exists()
    checks.append({
        "id": "product_trained", "label": "Product knowledge written",
        "done": product_trained,
        "required": True,
        "help": "Generated by 'openvz-leads train <url>' together with the config.",
    })

    # 5. A target of their own. The shipped config carries an example ICP,
    #    and an install that never replaced it does not fail — it quietly
    #    spends a day finding marketing agencies for someone selling to
    #    dentists. That is worse than an error, so it is a required check.
    checks.append({
        "id": "target", "label": "Target set",
        "done": _target_is_customised(),
        "required": True,
        "help": "Say who you're after in the Target tab, or run: "
                "openvz-leads target \"dental clinics in California\"",
    })

    # ── Optional from here down ──────────────────────────────────────

    env_vars = _read_env_file()

    # Reliable search. Not required, but the single biggest quality lever.
    serper_key = env_vars.get("SERPER_API_KEY", "") or os.getenv("SERPER_API_KEY", "")
    checks.append({
        "id": "serper", "label": "Search API key (recommended)",
        "done": bool(serper_key),
        "required": False,
        "help": "Without one, prospecting scrapes DuckDuckGo and Bing and gets "
                "rate-limited. serper.dev is about $5 per 2,500 searches, and "
                "is not a subscription. Add it in Settings.",
    })

    # Sending — genuinely optional, and off by default.
    instantly_key = env_vars.get("INSTANTLY_API_KEY", "") or os.getenv("INSTANTLY_API_KEY", "")
    instantly_set = bool(instantly_key) and instantly_key != "your_instantly_api_key_here"
    instantly_works = False
    if instantly_set:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    "https://api.instantly.ai/api/v2/accounts",
                    headers={"Authorization": f"Bearer {instantly_key}"},
                )
                instantly_works = resp.status_code == 200
        except Exception:
            pass
    checks.append({
        "id": "instantly", "label": "Sending provider (optional)",
        "done": instantly_works,
        "required": False,
        "help": "Only needed if you want it to send for you. Without it, "
                "outreach is drafted, queued for your review, and exported "
                "from the Export tab. Human review stays on either way.",
    })

    required = [c for c in checks if c["required"]]
    done = sum(1 for c in required if c["done"])
    percent = int(done / len(required) * 100) if required else 100

    return {
        "checks": checks,
        # The dashboard reads completed/total_required; done/total are kept
        # as aliases so the CLI and any script can use the obvious names.
        "completed": done,
        "total_required": len(required),
        "done": done,
        "total": len(required),
        "percent": percent,
        "workspace": str(PROJECT_ROOT),
        "engine": _engine_summary(),
    }


# The ICP the config ships with. Matching it means nobody has said who they
# are actually selling to.
_EXAMPLE_ICP = {
    "industries": ["SaaS", "Marketing Agency"],
    "company_size": "10-200 employees",
}


def _target_is_customised() -> bool:
    config = _load_config_or_none()
    if config is None:
        return False
    icp = config.icp
    if getattr(icp, "request", ""):
        return True
    return not (
        icp.industries == _EXAMPLE_ICP["industries"]
        and icp.company_size == _EXAMPLE_ICP["company_size"]
    )


def _engine_summary() -> dict:
    """What is doing the thinking, the reading and the CRM writing.

    Surfaced on the first screen because all three are now choices, and a
    setup screen that reports six ticks while the configured model has no key
    is telling you the opposite of what you need to know.
    """
    from openvz_leads.integrations.crawler import describe_tiers

    config = _load_config_or_none()
    if config is None:
        return {}

    from openvz_leads.brain import Brain
    from openvz_leads.config import load_env
    from openvz_leads.integrations.crm import CrmSync
    from openvz_leads.state import StateManager

    env = load_env()
    brain = Brain(StateManager(str(DB_PATH)), config.model, env)
    ready, why = brain.readiness()
    return {
        "brain": brain.describe(),
        "brain_ready": ready,
        "brain_problem": why,
        "crawl": describe_tiers(config.crawl),
        "crm": CrmSync(config.crm, env).describe(),
        "sending": _sending_summary(config, env),
        "sending_problem": _sending_problem(config, env),
    }


def _sending_summary(config, env) -> str:
    provider = config.channels.email.provider
    if provider == "none":
        return "off — drafted and queued for review, never sent"
    if provider == "gmail":
        from openvz_leads.integrations import gmail as gmail_api

        creds = gmail_api.load_credentials(env)
        who = creds.email_address or "no account authorised"
        return f"gmail — {who}"
    return provider


def _sending_problem(config, env) -> str:
    """Why nothing would send, or ''.

    Reported even when sending is off, because "off" is a choice and a
    misconfigured "on" is a surprise — and the two look identical from the
    outside until a campaign sits approved for a week.
    """
    if config.channels.email.provider != "gmail":
        return ""
    from openvz_leads.integrations import gmail as gmail_api

    footer = config.channels.email.gmail.footer.problem()
    if footer:
        return footer
    creds = gmail_api.load_credentials(env)
    client = gmail_api.GmailClient(creds, config.channels.email.gmail.read_scope)
    return client.readiness()[1]


@app.get("/api/settings")
async def get_settings():
    """Get current settings (API keys masked)."""
    env_vars = _read_env_file()
    # Also check os.environ as fallback
    for key in ["INSTANTLY_API_KEY", "LINKEDIN_EMAIL", "LINKEDIN_PASSWORD",
                "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"]:
        if key not in env_vars:
            env_vars[key] = os.getenv(key, "")

    return {
        "instantly_api_key": env_vars.get("INSTANTLY_API_KEY", ""),
        "instantly_api_key_masked": _mask_key(env_vars.get("INSTANTLY_API_KEY", "")),
        "linkedin_email": env_vars.get("LINKEDIN_EMAIL", ""),
        "linkedin_password_set": bool(env_vars.get("LINKEDIN_PASSWORD", "")),
        "cloudflare_account_id": env_vars.get("CLOUDFLARE_ACCOUNT_ID", ""),
        "cloudflare_api_token_masked": _mask_key(env_vars.get("CLOUDFLARE_API_TOKEN", "")),
    }


@app.post("/api/settings/env")
async def save_env_settings(request: Request):
    """Save environment variables to .env file."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)
    async with _env_lock:
        updates = {}
        for key in ["INSTANTLY_API_KEY", "LINKEDIN_EMAIL", "LINKEDIN_PASSWORD",
                     "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"]:
            if key in data and data[key] is not None:
                # Strip newlines so a crafted value can't inject extra .env entries
                updates[key] = str(data[key]).replace("\n", " ").replace("\r", " ").strip()
        if updates:
            try:
                _write_env_file(updates)
            except Exception as e:
                logger.warning("Failed to write .env: %s", e)
                return {"success": False, "message": "Could not write .env file."}
    return {"success": True}


@app.post("/api/settings/google")
async def save_google_settings(request: Request):
    """Store the OAuth *client*. The account is authorised from the CLI.

    Deliberately not a browser flow started from here: the loopback redirect
    has to land back on a port this process owns, and a half-finished
    authorisation begun in one tab and abandoned in another is a confusing
    failure to debug. One command, one browser window, one outcome.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    updates = {}
    for field, key in (
        ("client_id", "GOOGLE_CLIENT_ID"),
        ("client_secret", "GOOGLE_CLIENT_SECRET"),
    ):
        value = str(body.get(field) or "").strip()
        if value:
            updates[key] = value
    if not updates:
        return JSONResponse({"error": "Nothing to save."}, status_code=400)

    async with _env_lock:
        _write_env_file(updates)
    return {"ok": True, "saved": sorted(updates)}


@app.post("/api/settings/test-instantly")
async def test_instantly(request: Request):
    """Test an Instantly API key."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    api_key = str(data.get("api_key", "") or "")
    if not api_key:
        return {"success": False, "message": "No API key provided."}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.instantly.ai/api/v2/accounts",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code == 200:
                return {"success": True, "message": "Connected to Instantly."}
            else:
                return {"success": False, "message": f"API returned {resp.status_code}. Check your key."}
    except Exception as e:
        return {"success": False, "message": f"Connection failed: {str(e)}"}


# ── Companies ──


# ── Browsing lists ──

# Ceiling on one page. High enough that scrolling still beats paging for a
# normal working set, low enough that a 40,000-row database cannot hand the
# browser a payload it will choke on rendering.
PAGE_MAX = 200


def _page_args(request: Request, sortable: dict[str, str], default_sort: str) -> dict:
    """Read the shared list query string: q, sort, order, limit, offset.

    `sortable` maps the name a client may send to the SQL fragment it means.
    Anything not in that map falls back to the default, so no part of an ORDER
    BY clause is ever built from user input.
    """
    params = request.query_params
    sort = params.get("sort") or ""
    order = "ASC" if (params.get("order") or "").lower() == "asc" else "DESC"

    def _int(name: str, fallback: int, cap: int) -> int:
        try:
            value = int(params.get(name) or fallback)
        except (TypeError, ValueError):
            return fallback
        return max(0, min(value, cap))

    return {
        "q": (params.get("q") or "").strip(),
        "sort": sortable.get(sort, sortable[default_sort]),
        "order": order,
        "limit": _int("limit", 50, PAGE_MAX) or 50,
        "offset": _int("offset", 0, 1_000_000),
    }


def _like(term: str) -> str:
    """A LIKE pattern that treats the wildcards as literal text.

    Someone searching for a literal % — an email with one in it, a company
    called "100%" — should not silently match everything.
    """
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


COMPANY_SORTS = {
    "created_at": "c.created_at",
    "name": "c.name",
    "contacts": "contact_count",
    "industry": "c.industry",
}


@app.get("/api/companies")
async def get_companies(request: Request):
    """One page of companies, with contact counts.

    Returns an envelope rather than a bare list: without `total` the page
    cannot tell "50 companies" from "the first 50 of 3,000", and the second
    is the one where a search box matters.
    """
    args = _page_args(request, COMPANY_SORTS, "created_at")
    where, params = "", []
    if args["q"]:
        where = "WHERE (c.name LIKE ? ESCAPE '\\' OR c.domain LIKE ? ESCAPE '\\' " \
                "OR c.industry LIKE ? ESCAPE '\\' OR c.location LIKE ? ESCAPE '\\')"
        params = [_like(args["q"])] * 4

    total_rows = await query_db(f"SELECT COUNT(*) AS n FROM companies c {where}", tuple(params))
    rows = await query_db(
        f"""SELECT c.*,
               (SELECT COUNT(*) FROM prospects p WHERE p.company_id = c.id) AS contact_count
            FROM companies c {where}
            ORDER BY {args["sort"]} {args["order"]}
            LIMIT ? OFFSET ?""",
        tuple(params) + (args["limit"], args["offset"]),
    )
    return {
        "rows": rows,
        "total": total_rows[0]["n"] if total_rows else 0,
        "limit": args["limit"],
        "offset": args["offset"],
    }


@app.get("/api/companies/{company_id}/contacts")
async def get_company_contacts(company_id: str):
    """Get all contacts for a specific company."""
    rows = await query_db(
        "SELECT * FROM prospects WHERE company_id = ? ORDER BY score DESC",
        (company_id,),
    )
    return rows


# ── Feedback ──


@app.post("/api/feedback")
async def add_feedback(request: Request):
    """Add a comment/feedback on any entity."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    entity_type = str(data.get("entity_type", "") or "")[:50]
    entity_id = str(data.get("entity_id", "") or "")[:100]
    comment = str(data.get("comment", "") or "").strip()[:4000]
    if not comment:
        return {"success": False, "message": "Comment is required."}
    feedback_id = uuid.uuid4().hex[:12]
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(DB_PATH)) as db:
            # Ensure the table exists so feedback works even on a fresh install
            await db.execute(
                """CREATE TABLE IF NOT EXISTS feedback (
                    id TEXT PRIMARY KEY,
                    entity_type TEXT,
                    entity_id TEXT,
                    comment TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )"""
            )
            await db.execute(
                "INSERT INTO feedback (id, entity_type, entity_id, comment) VALUES (?, ?, ?, ?)",
                (feedback_id, entity_type, entity_id, comment),
            )
            await db.commit()
    except Exception as e:
        logger.warning("Failed to save feedback: %s", e)
        return {"success": False, "message": "Could not save feedback."}
    return {"success": True, "id": feedback_id}


@app.get("/api/feedback/{entity_type}/{entity_id}")
async def get_feedback(entity_type: str, entity_id: str):
    """Get feedback for an entity."""
    rows = await query_db(
        "SELECT * FROM feedback WHERE entity_type = ? AND entity_id = ? ORDER BY created_at DESC",
        (entity_type, entity_id),
    )
    return rows


# ── OpenVZ Leads Controls ──


@app.get("/api/agent/status")
async def get_agent_status():
    """Check if OpenVZ Leads is currently running."""
    pid = _check_agent_pid()
    started = _agent_started_at.isoformat() if _agent_started_at else None
    return {"running": pid is not None, "pid": pid, "started_at": started}


@app.post("/api/agent/start")
async def start_agent():
    """Start OpenVZ Leads' heartbeat loop as a subprocess."""
    global _agent_process, _agent_started_at

    if _check_agent_pid():
        return {"success": False, "message": "OpenVZ Leads is already running."}

    # Ensure data dir exists
    (PROJECT_ROOT / "data").mkdir(parents=True, exist_ok=True)

    try:
        # Popen writes to the file descriptor, so this wrapper's encoding
        # is never used for the child's output — it is spelled out anyway
        # so nobody has to work that out again.
        log_handle = open(LOG_FILE, "a", encoding="utf-8")
        try:
            _agent_process = subprocess.Popen(
                [sys.executable, "-m", "openvz_leads"],
                cwd=str(PROJECT_ROOT),
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
            )
        finally:
            # Child holds its own copies of the fds; don't leak ours.
            log_handle.close()
    except Exception as e:
        logger.warning("Failed to start OpenVZ Leads: %s", e)
        return {"success": False, "message": f"Failed to start OpenVZ Leads: {e}"}
    _agent_started_at = datetime.now()

    # Write PID file
    try:
        PID_FILE.write_text(str(_agent_process.pid), encoding="utf-8")
    except OSError as e:
        logger.warning("Could not write PID file: %s", e)

    return {"success": True, "pid": _agent_process.pid}


@app.post("/api/agent/stop")
async def stop_agent():
    """Stop the OpenVZ Leads subprocess."""
    global _agent_process, _agent_started_at

    pid = _check_agent_pid()
    if not pid:
        return {"success": False, "message": "OpenVZ Leads is not running."}

    try:
        os.kill(pid, signal.SIGTERM)
        # Wait briefly for graceful shutdown
        for _ in range(10):
            try:
                os.kill(pid, 0)
                await asyncio.sleep(0.5)
            except ProcessLookupError:
                break
        else:
            # Force kill if still running
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    except (ProcessLookupError, PermissionError):
        pass

    _agent_process = None
    _agent_started_at = None
    PID_FILE.unlink(missing_ok=True)

    return {"success": True}


@app.get("/api/agent/logs")
async def get_agent_logs():
    """Get recent log lines."""
    if not LOG_FILE.exists():
        return {"lines": []}
    try:
        # Tail only the last 64KB so a huge log file never blocks the UI
        with open(LOG_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            text = f.read().decode("utf-8", errors="replace")
        lines = text.strip().splitlines()[-100:]
        return {"lines": lines}
    except Exception:
        return {"lines": []}


# ── Pipeline Data (existing endpoints) ──


@app.get("/api/stats")
async def get_stats():
    """Pipeline overview stats."""
    try:
        prospects = await query_db(
            "SELECT status, COUNT(*) as count FROM prospects GROUP BY status"
        )
        prospect_total = sum(r["count"] for r in prospects)
        prospect_map = {r["status"]: r["count"] for r in prospects}

        campaigns = await query_db(
            "SELECT status, COUNT(*) as count FROM campaigns GROUP BY status"
        )
        campaign_map = {r["status"]: r["count"] for r in campaigns}

        conversations = await query_db(
            "SELECT status, COUNT(*) as count FROM conversations GROUP BY status"
        )
        convo_map = {r["status"]: r["count"] for r in conversations}

        actions = await query_db("SELECT COUNT(*) as count FROM actions")
        action_count = actions[0]["count"] if actions else 0

        usage = await query_db(
            "SELECT claude_calls FROM usage_log WHERE date = date('now')"
        )
        usage_today = usage[0]["claude_calls"] if usage else 0

        return {
            "prospects": {"total": prospect_total, "by_status": prospect_map},
            "campaigns": {"total": sum(campaign_map.values()), "by_status": campaign_map},
            "conversations": {"total": sum(convo_map.values()), "by_status": convo_map},
            "actions_total": action_count,
            "claude_calls_today": usage_today,
        }
    except Exception as e:
        return {"error": str(e)}


PROSPECT_SORTS = {
    "created_at": "created_at",
    "score": "score",
    "name": "last_name",
    "company": "company",
    "status": "status",
    "title": "title",
}


@app.get("/api/prospects")
async def get_prospects(request: Request):
    """One page of contacts, filtered and sorted."""
    args = _page_args(request, PROSPECT_SORTS, "created_at")
    clauses, params = [], []
    if args["q"]:
        clauses.append(
            "(first_name LIKE ? ESCAPE '\\' OR last_name LIKE ? ESCAPE '\\' "
            "OR email LIKE ? ESCAPE '\\' OR company LIKE ? ESCAPE '\\' "
            "OR title LIKE ? ESCAPE '\\')"
        )
        params += [_like(args["q"])] * 5

    status = (request.query_params.get("status") or "").strip()
    # Only a stage the pipeline actually defines, so a hand-edited URL cannot
    # quietly return an empty page that looks like "no contacts".
    if status and status in pipeline.STAGES:
        clauses.append("status = ?")
        params.append(status)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    total_rows = await query_db(f"SELECT COUNT(*) AS n FROM prospects {where}", tuple(params))
    rows = await query_db(
        f"""SELECT * FROM prospects {where}
            ORDER BY {args["sort"]} {args["order"]}
            LIMIT ? OFFSET ?""",
        tuple(params) + (args["limit"], args["offset"]),
    )
    return {
        "rows": rows,
        "total": total_rows[0]["n"] if total_rows else 0,
        "limit": args["limit"],
        "offset": args["offset"],
    }


@app.get("/api/campaigns")
async def get_campaigns():
    rows = await query_db("SELECT * FROM campaigns ORDER BY created_at DESC LIMIT 100")
    for row in rows:
        try:
            row["sequence"] = json.loads(row.get("sequence_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["sequence"] = []
        try:
            row["prospect_ids"] = json.loads(row.get("prospect_ids_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["prospect_ids"] = []
    return rows


@app.get("/api/review/pending")
async def get_pending_review():
    """Campaigns waiting on a human, with everything needed to judge them."""
    rows = await query_db(
        """SELECT * FROM campaigns
           WHERE status IN ('pending_review', 'draft')
           ORDER BY created_at ASC"""
    )
    for row in rows:
        try:
            row["sequence"] = json.loads(row.get("sequence_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["sequence"] = []
        try:
            prospect_ids = json.loads(row.get("prospect_ids_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            prospect_ids = []
        row["prospect_ids"] = prospect_ids
        row["recipients"] = []
        row["checks"] = {"avoid": [], "evidence_gaps": [], "low_confidence": 0}
        if prospect_ids:
            placeholders = ",".join("?" for _ in prospect_ids)
            row["recipients"] = await query_db(
                f"""SELECT first_name, last_name, title, company, email, score
                    FROM prospects WHERE id IN ({placeholders})
                    ORDER BY score DESC LIMIT 50""",
                tuple(prospect_ids),
            )
            # The copy was written from these briefs, and the two things the
            # Profiler is required to declare — what it could not evidence,
            # and what must not be claimed — are exactly what a reviewer is
            # here to check the draft against. Reading them in another tab
            # is the same as not reading them.
            row["checks"] = await _review_checks(prospect_ids, placeholders)
    return rows


async def _review_checks(prospect_ids: list[str], placeholders: str) -> dict:
    """Collect the do-not-say list and evidence gaps behind a campaign."""
    brief_rows = await query_db(
        f"""SELECT company, profile_json FROM prospects
            WHERE id IN ({placeholders}) AND profile_json != '' AND profile_json IS NOT NULL""",
        tuple(prospect_ids),
    )
    # Grouped by the warning, not by the account. The Profiler tends to raise
    # the same gap across a whole segment — "no pricing page" on eleven
    # dental practices is one thing to check, not eleven — and listing it
    # once per company buries the warning that only came up on one.
    avoid: dict[str, set[str]] = {}
    gaps: dict[str, set[str]] = {}
    low_confidence = 0
    for brief_row in brief_rows:
        try:
            brief = json.loads(brief_row.get("profile_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(brief, dict):
            continue
        if str(brief.get("confidence") or "").lower() == "low":
            low_confidence += 1
        company = brief_row.get("company") or ""
        for key, sink in (("avoid", avoid), ("evidence_gaps", gaps)):
            for item in brief.get(key) or []:
                text = str(item).strip()
                if text:
                    sink.setdefault(text, set()).add(company)

    def _rank(grouped: dict[str, set[str]]) -> list[dict]:
        """Most widely applicable first, then alphabetically for a stable order."""
        return [
            {"text": text, "accounts": sorted(c for c in companies if c)}
            for text, companies in sorted(
                grouped.items(), key=lambda kv: (-len(kv[1]), kv[0])
            )
        ][:20]

    return {
        "avoid": _rank(avoid),
        "evidence_gaps": _rank(gaps),
        "low_confidence": low_confidence,
        "briefed": len(brief_rows),
    }


@app.post("/api/review/{campaign_id}")
async def decide_review(campaign_id: str, request: Request):
    """Approve or reject a queued campaign."""
    from openvz_leads.state import StateManager

    try:
        body = await request.json()
    except Exception:
        body = {}
    if "approved" not in body:
        return JSONResponse(
            {"error": "Body must include 'approved': true or false."}, status_code=400
        )
    approved = bool(body.get("approved"))
    note = str(body.get("note") or "").strip()

    state = StateManager(str(DB_PATH))
    try:
        ok = await state.review_campaign(campaign_id, approved=approved, note=note)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    if not ok:
        return JSONResponse(
            {"error": "That campaign is no longer awaiting review."}, status_code=409
        )
    return {"ok": True, "status": "approved" if approved else "rejected"}


@app.post("/api/review/{campaign_id}/sequence")
async def edit_review_sequence(campaign_id: str, request: Request):
    """Save a reviewer's edits to a draft that is still awaiting review."""
    from openvz_leads.state import StateManager

    try:
        body = await request.json()
    except Exception:
        body = {}
    sequence = body.get("sequence")
    if not isinstance(sequence, list):
        return JSONResponse(
            {"error": "Body must include 'sequence': a list of steps."}, status_code=400
        )

    state = StateManager(str(DB_PATH))
    try:
        merged = await state.edit_pending_sequence(campaign_id, sequence)
    except Exception as e:
        logger.exception("Failed to save review edits for %s", campaign_id)
        return JSONResponse({"error": str(e)}, status_code=500)
    if merged is None:
        return JSONResponse(
            {"error": "That campaign is no longer open for editing."}, status_code=409
        )
    return {"ok": True, "sequence": merged}


@app.get("/api/profiles")
async def get_profiles():
    """Account briefs written by the Profiler."""
    rows = await query_db(
        """SELECT id, first_name, last_name, title, email, company, industry,
                  score, status, profile_json, profiled_at
           FROM prospects
           WHERE profile_json != '' AND profile_json IS NOT NULL
           ORDER BY profiled_at DESC LIMIT 200"""
    )
    for row in rows:
        try:
            row["profile"] = json.loads(row.pop("profile_json", "") or "{}")
        except (json.JSONDecodeError, TypeError):
            row["profile"] = {}
    return rows


@app.post("/api/export")
async def run_export(request: Request):
    """Write an export file and report where it landed."""
    from openvz_leads.exporter import ExportError, Exporter
    from openvz_leads.state import StateManager

    try:
        body = await request.json()
    except Exception:
        body = {}
    dataset = str(body.get("dataset") or "leads")
    fmt = str(body.get("format") or "csv")

    state = StateManager(str(DB_PATH))
    # Same as the CLI: heading language follows profiling.output_language, so
    # a Chinese brief does not come out under English headings.
    language = None
    try:
        from openvz_leads.config import load_config

        language = load_config().profiling.output_language
    except Exception:
        pass
    try:
        path = await Exporter(state, language).export(dataset=dataset, fmt=fmt)
    except ExportError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return {"ok": True, "path": str(path), "name": path.name}


@app.get("/api/conversations")
async def get_conversations():
    rows = await query_db("""
        SELECT c.*, p.first_name, p.last_name, p.email as prospect_email, p.company
        FROM conversations c
        LEFT JOIN prospects p ON c.prospect_id = p.id
        ORDER BY c.updated_at DESC LIMIT 100
    """)
    for row in rows:
        try:
            row["thread"] = json.loads(row.get("thread_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["thread"] = []
    return rows


# ── Insights ──


def _rate(part: int, whole: int) -> float | None:
    """A percentage, or None when the denominator makes it meaningless.

    Returning 0.0 for "nothing sent yet" would draw a bar at zero and read as
    a bad reply rate, which is a different claim from having no data.
    """
    if not whole:
        return None
    return round(part / whole * 100, 1)


@app.get("/api/analytics")
async def get_analytics():
    """Everything the Insights tab shows.

    Counts are computed live rather than read from data/analytics.json: the
    Analyst only runs on idle cycles, so its file can be hours old, and a
    funnel that disagrees with the Overview tab is worse than no funnel.

    The written insights *are* taken from the file, because they are the one
    part that is not derivable from a count — and the file's own timestamp is
    passed through so the page can say how old they are.
    """
    stage_rows = await query_db("SELECT status, COUNT(*) AS n FROM prospects GROUP BY status")
    stages = {row["status"]: row["n"] for row in stage_rows}

    intent_rows = await query_db(
        "SELECT intent, COUNT(*) AS n FROM conversations WHERE intent != '' GROUP BY intent"
    )
    intents = {row["intent"]: row["n"] for row in intent_rows}

    # Reached-at-least-once, not currently-sitting-in: a prospect who replied
    # was contacted, and a funnel that forgets that shows contact falling to
    # zero the moment it starts working.
    #
    # This is read from stage_events rather than inferred by summing the
    # stages after it in STAGES, because two of them are not further down any
    # funnel: `lost` and `opted_out` are reachable from anywhere. Summing the
    # tail would count someone who opted out while still `queued` as having
    # been contacted, and quietly inflate every rate below it.
    reached_rows = await query_db(
        """SELECT to_stage AS stage, COUNT(DISTINCT prospect_id) AS n
           FROM stage_events WHERE to_stage != '' GROUP BY to_stage"""
    )
    reached = {row["stage"]: row["n"] for row in reached_rows}
    # A prospect whose stage predates stage_events has no event for where it
    # is now. Its current stage is the one thing we do know it reached.
    for stage, resting in stages.items():
        reached[stage] = max(reached.get(stage, 0), resting)
    contacted = reached.get("contacted", 0)
    replied = reached.get("replied", 0)
    meeting = reached.get("meeting", 0)
    won = stages.get("won", 0)
    opted_out = stages.get("opted_out", 0)

    campaigns = await query_db(
        """SELECT c.id, c.name, c.status, c.prospect_ids_json, c.created_at,
                  COUNT(v.id) AS reply_count,
                  SUM(CASE WHEN v.intent = 'interested' THEN 1 ELSE 0 END) AS interested_count,
                  SUM(CASE WHEN v.intent = 'objection' THEN 1 ELSE 0 END) AS objection_count
           FROM campaigns c
           LEFT JOIN conversations v ON v.campaign_id = c.id
           WHERE c.status IN ('active', 'completed')
           GROUP BY c.id
           ORDER BY c.created_at DESC
           LIMIT 25"""
    )
    for row in campaigns:
        try:
            recipients = len(json.loads(row.pop("prospect_ids_json", "") or "[]"))
        except (json.JSONDecodeError, TypeError):
            recipients = 0
        row["recipients"] = recipients
        row["reply_count"] = row.get("reply_count") or 0
        row["interested_count"] = row.get("interested_count") or 0
        row["objection_count"] = row.get("objection_count") or 0
        row["reply_rate"] = _rate(row["reply_count"], recipients)

    # Model spend over the last fortnight, so "it is using too much Claude"
    # is a shape on a chart rather than a hunch.
    usage = await query_db(
        "SELECT date, claude_calls FROM usage_log ORDER BY date DESC LIMIT 14"
    )
    usage.reverse()

    insights: list[str] = []
    generated_at = ""
    report_path = DB_PATH.parent / "analytics.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if isinstance(report, dict):
            raw = report.get("insights")
            insights = [str(i) for i in raw if str(i).strip()] if isinstance(raw, list) else []
            generated_at = str(report.get("generated_at") or "")
    except (OSError, json.JSONDecodeError, TypeError):
        pass  # the Analyst has not run yet, or wrote something unreadable

    return {
        "funnel": [
            {"stage": stage, "reached": reached.get(stage, 0), "resting": stages.get(stage, 0)}
            for stage in pipeline.STAGES
        ],
        "rates": {
            "reply": _rate(replied, contacted),
            "meeting": _rate(meeting, replied),
            "won": _rate(won, meeting),
            "opt_out": _rate(opted_out + intents.get("unsubscribe", 0), contacted),
            "interested": _rate(intents.get("interested", 0), contacted),
        },
        "totals": {
            "contacted": contacted,
            "replied": replied,
            "meeting": meeting,
            "won": won,
            "opted_out": opted_out,
        },
        "intents": intents,
        "campaigns": campaigns,
        "usage": usage,
        "insights": insights,
        "insights_generated_at": generated_at,
    }


@app.get("/api/activity")
async def get_activity():
    rows = await query_db("SELECT * FROM actions ORDER BY created_at DESC LIMIT 100")
    for row in rows:
        try:
            row["details"] = json.loads(row.get("details_json", "{}"))
        except (json.JSONDecodeError, TypeError):
            row["details"] = {}
    return rows


# ── Dashboard UI ──


# ── Target: a sentence in, an ICP out ─────────────────────────────────


def _load_config_or_none():
    """The config, or None. Every caller here has something useful to do
    without one — this dashboard's whole job on a fresh install is to be
    usable before the config is finished."""
    from openvz_leads.config import load_config

    try:
        return load_config(str(CONFIG_FILE)) if CONFIG_FILE.exists() else load_config()
    except Exception as e:
        logger.info("No usable config yet: %s", e)
        return None


@app.get("/api/icp")
async def get_icp():
    """The ICP as configured, plus the sentence it came from."""
    config = _load_config_or_none()
    if config is None:
        return {"configured": False, "icp": {}, "request": ""}
    icp = config.icp
    return {
        "configured": True,
        "request": getattr(icp, "request", ""),
        "icp": {
            "industries": icp.industries,
            "company_size": icp.company_size,
            "titles": icp.titles,
            "geography": icp.geography,
            "keywords": getattr(icp, "keywords", []),
            "exclusions": getattr(icp, "exclusions", []),
        },
    }


@app.post("/api/icp/parse")
async def parse_icp(request: Request):
    """Parse a natural-language request. Saves nothing.

    Deliberately split from applying it: the assumptions this returns are the
    reason the feature is trustworthy, and they are only worth anything if a
    person sees them before the config changes.
    """
    from openvz_leads.brain import Brain
    from openvz_leads.config import load_env
    from openvz_leads.icp import parse_request
    from openvz_leads.state import StateManager

    try:
        body = await request.json()
    except Exception:
        body = {}
    text = str(body.get("request") or "").strip()
    if not text:
        return JSONResponse({"error": "Say what you're looking for."}, status_code=400)

    config = _load_config_or_none()
    state = StateManager(str(DB_PATH))
    try:
        await state.init_db()
    except Exception as e:
        logger.warning("Could not open the database for an ICP parse: %s", e)

    brain = Brain(state, config.model if config else None, load_env())
    ready, why = brain.readiness()
    note = ""
    if not ready:
        # Still answer — the heuristic parser exists for exactly this — but
        # say plainly that the result is the worse one.
        note = why
        brain = None

    try:
        draft = await parse_request(
            brain, text, current=config.icp if config else None
        )
    except Exception as e:
        logger.error("ICP parse failed: %s", e)
        return JSONResponse({"error": f"Could not read that: {e}"}, status_code=500)

    return {
        "ok": draft.is_usable(),
        "via": draft.via,
        "note": note,
        "draft": draft.model_dump(),
    }


@app.post("/api/icp/apply")
async def apply_icp(request: Request):
    """Write a reviewed draft into openvz-leads.yaml."""
    from openvz_leads.icp import ICPDraft, apply_to_file

    try:
        body = await request.json()
    except Exception:
        body = {}
    draft_data = body.get("draft")
    if not isinstance(draft_data, dict):
        return JSONResponse({"error": "Body must include 'draft'."}, status_code=400)

    try:
        draft = ICPDraft(**draft_data)
    except Exception as e:
        return JSONResponse({"error": f"That draft is malformed: {e}"}, status_code=400)
    if not draft.is_usable():
        return JSONResponse(
            {"error": "That draft has nothing searchable in it."}, status_code=400
        )

    target = CONFIG_FILE
    if not target.exists():
        return JSONResponse(
            {
                "error": (
                    "There is no openvz-leads.yaml yet. Run 'openvz-leads setup' "
                    "or 'openvz-leads train <your-site>' first — the target is "
                    "only half of a configuration."
                )
            },
            status_code=409,
        )

    try:
        written = apply_to_file(draft, target)
    except Exception as e:
        logger.error("Could not write the ICP: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
    return {"ok": True, "path": written}


# ── Pipeline stages ───────────────────────────────────────────────────


@app.get("/api/pipeline")
async def get_pipeline():
    """The funnel: how many prospects sit at each stage."""
    from openvz_leads import pipeline as pl

    rows = await query_db("SELECT status, COUNT(*) AS n FROM prospects GROUP BY status")
    counts = {stage: 0 for stage in pl.STAGES}
    for row in rows:
        stage = pl.normalize(row.get("status") or "")
        counts[stage] = counts.get(stage, 0) + (row.get("n") or 0)

    config = _load_config_or_none()
    provider = config.crm.provider if config is not None else "none"
    # The provider id, not a sentence: the dashboard is bilingual and the
    # server is not, so anything that reaches the screen has to be a key the
    # client can translate.
    target = ""
    if config is not None and config.crm.sync_enabled:
        target = (
            f"data/{'crm-sync.jsonl'}"
            if provider == "file"
            else "/".join((config.crm.webhook_url or "").split("/")[2:3])
        )

    pending = 0
    if provider != "none":
        # Only meaningful when something is listening. With sync off every
        # event is unsynced forever by definition, and reporting that as a
        # backlog reads as a fault when it is the configured behaviour.
        rows = await query_db(
            "SELECT COUNT(*) AS n FROM stage_events WHERE synced = 0"
        )
        pending = rows[0]["n"] if rows else 0

    return {
        "stages": [
            {
                "id": stage,
                "count": counts.get(stage, 0),
                "description": pl.DESCRIPTIONS.get(stage, ""),
                "terminal": stage in pl.TERMINAL,
            }
            for stage in pl.STAGES
        ],
        "crm": {"provider": provider, "target": target},
        "unsynced": pending,
    }


@app.post("/api/prospects/{prospect_id}/stage")
async def move_stage(prospect_id: str, request: Request):
    """Move a prospect. Recorded as a human decision, because it is one."""
    from openvz_leads import pipeline as pl
    from openvz_leads.config import load_env
    from openvz_leads.state import StateManager

    try:
        body = await request.json()
    except Exception:
        body = {}
    target = str(body.get("stage") or "").strip().lower()
    if target not in pl.STAGES:
        return JSONResponse(
            {"error": f"'{target}' is not a stage."}, status_code=400
        )

    state = StateManager(str(DB_PATH))
    config = _load_config_or_none()
    crm = None
    if config is not None and config.crm.sync_enabled:
        from openvz_leads.integrations.crm import CrmSync

        crm = CrmSync(config.crm, load_env())

    prospect = await state.get_prospect(prospect_id)
    if prospect is None:
        return JSONResponse({"error": "No such prospect."}, status_code=404)

    ok, why = pl.can_move(prospect.status, target)
    if not ok and not bool(body.get("force")):
        return JSONResponse({"error": why}, status_code=409)

    moved = await pl.advance(
        state,
        prospect_id,
        target,
        reason=str(body.get("note") or "").strip(),
        actor="human",
        crm=crm,
        force=bool(body.get("force")),
    )
    if not moved:
        return JSONResponse({"error": "The move was refused."}, status_code=409)
    return {"ok": True, "stage": target}


@app.get("/api/prospects/{prospect_id}/stage")
async def stage_history(prospect_id: str):
    rows = await query_db(
        """SELECT from_stage, to_stage, reason, actor, synced, created_at
           FROM stage_events WHERE prospect_id = ?
           ORDER BY created_at ASC""",
        (prospect_id,),
    )
    return rows


# ── Gmail and the send queue ──────────────────────────────────────────


@app.get("/api/gmail/status")
async def gmail_status():
    """Whether this install can send, and what is missing if it cannot."""
    from openvz_leads.config import load_env
    from openvz_leads.integrations import gmail as gmail_api

    config = _load_config_or_none()
    settings = config.channels.email.gmail if config else None
    read_scope = settings.read_scope if settings else "metadata"

    env = load_env()
    creds = gmail_api.load_credentials(env)
    client = gmail_api.GmailClient(creds, read_scope)
    ready, why = client.readiness()

    return {
        "provider": config.channels.email.provider if config else "none",
        "ready": ready,
        "problem": why,
        "address": creds.email_address,
        "read_scope": read_scope,
        "scopes": creds.scopes,
        "client_configured": bool(env.google_client_id and env.google_client_secret),
        # Kept separate from `problem`: the credentials can be perfect while
        # the footer still blocks every send, and conflating the two sends
        # people to fix the wrong thing.
        "footer_problem": settings.footer.problem() if settings else "",
        "max_followups": settings.max_followups if settings else 0,
    }


@app.get("/api/outbox")
async def get_outbox():
    """What is queued, and what recently went out.

    The single most reassuring screen in a product that sends email by
    itself: not "trust me", but "here is the list, with the times".
    """
    rows = await query_db(
        """SELECT o.id, o.step, o.subject, o.status, o.reason, o.send_after,
                  o.sent_at, o.attempts, o.provider_thread_id,
                  p.first_name, p.last_name, p.email, p.company
           FROM outbox o
           LEFT JOIN prospects p ON p.id = o.prospect_id
           ORDER BY
             CASE o.status WHEN 'pending' THEN 0 ELSE 1 END,
             COALESCE(o.sent_at, o.send_after) ASC
           LIMIT 200"""
    )
    counts = await query_db(
        "SELECT status, COUNT(*) AS n FROM outbox GROUP BY status"
    )
    return {
        "rows": rows,
        "counts": {row["status"]: row["n"] for row in counts},
    }


# The page itself lives in openvz_leads/static/dashboard.html rather than in a
# 2,700-line string in this file. Two reasons: an editor can syntax-highlight
# and lint it, and a diff to the markup no longer reads as a diff to the API.
_page_cache: tuple[float, str] | None = None


def _read_page() -> str:
    """Return the dashboard page, re-reading it when the file changes.

    Cached on mtime so a normal run pays one read, while someone editing the
    markup sees their change on the next refresh without restarting uvicorn.
    """
    global _page_cache
    path = paths.static_file("dashboard.html")
    try:
        mtime = path.stat().st_mtime
    except OSError as e:
        logger.error("Dashboard page missing at %s: %s", path, e)
        return (
            "<!DOCTYPE html><meta charset='utf-8'><title>OpenVZ Leads</title>"
            "<p style='font:14px system-ui;padding:40px'>The dashboard page could "
            "not be found. This install looks incomplete — reinstall with "
            "<code>pip install -e .</code>.</p>"
        )
    if _page_cache is None or _page_cache[0] != mtime:
        _page_cache = (mtime, path.read_text(encoding="utf-8"))
    return _page_cache[1]


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return _read_page()


def start_dashboard(host: str = "127.0.0.1", port: int = 5555):
    """Start the dashboard server."""
    import uvicorn

    print(f"\n  OpenVZ Leads Dashboard running at http://{host}:{port}")
    print("  Press Ctrl+C to stop.\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
