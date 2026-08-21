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

from openvz_leads import paths
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
        for line in ENV_FILE.read_text().splitlines():
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
    ENV_FILE.write_text("\n".join(lines) + "\n")
    load_dotenv(str(ENV_FILE), override=True)


def _check_agent_pid() -> int | None:
    """Check if there's a running OpenVZ Leads process from a PID file."""
    global _agent_process, _agent_started_at
    if _agent_process and _agent_process.poll() is None:
        return _agent_process.pid
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
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
            with open(CONFIG_FILE) as f:
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
    }


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


@app.get("/api/companies")
async def get_companies():
    """All companies with contact counts."""
    rows = await query_db("""
        SELECT c.*,
            (SELECT COUNT(*) FROM prospects p WHERE p.company_id = c.id) as contact_count
        FROM companies c ORDER BY c.created_at DESC LIMIT 200
    """)
    return rows


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
        log_handle = open(LOG_FILE, "a")
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
        PID_FILE.write_text(str(_agent_process.pid))
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


@app.get("/api/prospects")
async def get_prospects():
    rows = await query_db("SELECT * FROM prospects ORDER BY created_at DESC LIMIT 200")
    return rows


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
        if prospect_ids:
            placeholders = ",".join("?" for _ in prospect_ids)
            row["recipients"] = await query_db(
                f"""SELECT first_name, last_name, title, company, email, score
                    FROM prospects WHERE id IN ({placeholders})
                    ORDER BY score DESC LIMIT 50""",
                tuple(prospect_ids),
            )
    return rows


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


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenVZ Leads — Command Deck</title>
<style>
  :root {
    --bg: #08090c;
    --panel: #0f1216;
    --panel-raised: #141922;
    --border: #1d2430;
    --border-strong: #2b3444;
    --text: #e9ecf2;
    --text-2: #9aa4b4;
    --text-3: #5c6774;
    --accent: #3ecf8e;
    --accent-deep: #22996a;
    --accent-soft: rgba(62, 207, 142, 0.12);
    --blue: #74a8ff;
    --amber: #e5b567;
    --red: #e06c75;
    --purple: #b48ce8;
    --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }
  ::selection { background: rgba(62,207,142,0.25); }

  html { color-scheme: dark; }

  body {
    font-family: var(--sans);
    background: var(--bg);
    background-image:
      radial-gradient(1100px 480px at 75% -12%, rgba(62,207,142,0.06), transparent 60%),
      radial-gradient(900px 420px at 8% -10%, rgba(116,168,255,0.05), transparent 55%);
    background-repeat: no-repeat;
    color: var(--text);
    min-height: 100vh;
    font-size: 14px;
    -webkit-font-smoothing: antialiased;
  }

  a { color: var(--blue); text-decoration: none; }
  a:hover { text-decoration: underline; }

  /* ── Header ── */
  header {
    position: sticky; top: 0; z-index: 100;
    background: rgba(8,9,12,0.82);
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    border-bottom: 1px solid var(--border);
    padding: 14px 32px;
    display: flex; align-items: center; justify-content: space-between; gap: 16px;
  }

  .brand { display: flex; align-items: center; gap: 12px; }
  .brand .mark {
    width: 34px; height: 34px; border-radius: 9px;
    background: linear-gradient(145deg, #2fbf82, #17795a);
    box-shadow: 0 0 0 1px rgba(62,207,142,0.35), 0 4px 14px rgba(62,207,142,0.18);
    display: flex; align-items: center; justify-content: center;
    font-weight: 800; font-size: 17px; color: #04140d; letter-spacing: -0.5px;
  }
  .brand h1 { font-size: 17px; font-weight: 700; letter-spacing: -0.3px; line-height: 1.1; }
  .brand .tagline {
    font-size: 10px; text-transform: uppercase; letter-spacing: 1.4px;
    color: var(--text-3); margin-top: 2px; font-weight: 600;
  }

  .header-controls { display: flex; align-items: center; gap: 10px; }

  .agent-status {
    display: flex; align-items: center; gap: 8px;
    font-size: 12px; font-weight: 600; color: var(--text-2);
    padding: 7px 14px;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 99px;
  }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .status-dot.running {
    background: var(--accent);
    box-shadow: 0 0 0 0 rgba(62,207,142,0.5);
    animation: pulse 2s infinite;
  }
  .status-dot.stopped { background: #4a5361; }
  .status-dot.offline { background: var(--red); }
  @keyframes pulse {
    0% { box-shadow: 0 0 0 0 rgba(62,207,142,0.45); }
    70% { box-shadow: 0 0 0 7px rgba(62,207,142,0); }
    100% { box-shadow: 0 0 0 0 rgba(62,207,142,0); }
  }

  .refresh-btn {
    background: var(--panel); border: 1px solid var(--border); color: var(--text-2);
    padding: 7px 14px; border-radius: 99px; cursor: pointer;
    font-size: 12px; font-weight: 600; font-family: inherit;
    transition: border-color .15s, color .15s;
  }
  .refresh-btn:hover { border-color: var(--border-strong); color: var(--text); }

  /* ── Nav ── */
  nav {
    position: sticky; top: 63px; z-index: 99;
    background: rgba(8,9,12,0.82);
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    border-bottom: 1px solid var(--border);
    padding: 0 24px; display: flex; overflow-x: auto;
    scrollbar-width: none;
  }
  nav::-webkit-scrollbar { display: none; }

  nav button {
    position: relative;
    background: none; border: none;
    color: var(--text-3); padding: 13px 14px; cursor: pointer;
    font-size: 13px; font-weight: 500; font-family: inherit;
    transition: color .15s; white-space: nowrap;
  }
  nav button:hover { color: var(--text-2); }
  nav button.active { color: var(--text); font-weight: 600; }
  nav button.active::after {
    content: ""; position: absolute; left: 14px; right: 14px; bottom: -1px;
    height: 2px; border-radius: 2px 2px 0 0; background: var(--accent);
  }

  main { padding: 28px 32px 64px; max-width: 1400px; margin: 0 auto; }

  .section { display: none; }
  .section.active { display: block; animation: rise .25s ease; }
  @keyframes rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }

  .section-head { margin-bottom: 20px; }
  .section-head h2 { font-size: 20px; font-weight: 700; letter-spacing: -0.4px; }
  .section-head p { font-size: 13px; color: var(--text-3); margin-top: 4px; }

  /* ── Cards ── */
  .card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
    padding: 24px; margin-bottom: 16px;
  }
  .card h2 { font-size: 15px; font-weight: 650; letter-spacing: -0.2px; margin-bottom: 16px; }

  /* ── Stat cards ── */
  .stats-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 14px; margin-bottom: 28px;
  }
  .stat-card {
    position: relative; overflow: hidden;
    background: linear-gradient(180deg, var(--panel-raised), var(--panel));
    border: 1px solid var(--border); border-radius: 12px; padding: 20px;
    transition: border-color .2s;
  }
  .stat-card:hover { border-color: var(--border-strong); }
  .stat-card::before {
    content: ""; position: absolute; top: 0; left: 20px; right: 20px; height: 1px;
    background: linear-gradient(90deg, transparent, rgba(62,207,142,0.35), transparent);
  }
  .stat-card .label {
    font-size: 10.5px; text-transform: uppercase; letter-spacing: 1.2px;
    color: var(--text-3); font-weight: 700; margin-bottom: 10px;
  }
  .stat-card .value {
    font-size: 34px; font-weight: 750; letter-spacing: -1px; line-height: 1;
    font-variant-numeric: tabular-nums;
  }
  .stat-card .breakdown { margin-top: 12px; display: flex; flex-wrap: wrap; gap: 5px; }
  .chip {
    font-size: 11px; font-weight: 550; color: var(--text-2);
    background: rgba(255,255,255,0.04); border: 1px solid var(--border);
    padding: 2px 9px; border-radius: 99px; font-variant-numeric: tabular-nums;
  }
  .chip b { color: var(--text); font-weight: 650; }

  /* ── Progress ── */
  .progress-wrap { margin-bottom: 24px; }
  .progress-label { display: flex; justify-content: space-between; margin-bottom: 8px; font-size: 13px; }
  .progress-label .pct { color: var(--text); font-weight: 700; font-variant-numeric: tabular-nums; }
  .progress-label .text { color: var(--text-3); }
  .progress-bar { background: rgba(255,255,255,0.05); border-radius: 99px; height: 8px; overflow: hidden; }
  .progress-fill { height: 100%; border-radius: 99px; transition: width .5s ease; }
  .progress-fill.green { background: linear-gradient(90deg, var(--accent-deep), var(--accent)); }
  .progress-fill.yellow { background: linear-gradient(90deg, #a3801f, var(--amber)); }

  /* ── Setup checklist ── */
  .check-item {
    display: flex; align-items: flex-start; gap: 14px;
    padding: 14px 0; border-bottom: 1px solid var(--border);
  }
  .check-item:last-child { border-bottom: none; }
  .check-icon {
    width: 22px; height: 22px; border-radius: 50%; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    font-size: 12px; margin-top: 1px;
  }
  .check-icon.done { background: var(--accent-soft); color: var(--accent); border: 1px solid rgba(62,207,142,0.3); }
  .check-icon.pending { background: transparent; color: var(--text-3); border: 1px dashed var(--border-strong); }
  .check-info { flex: 1; }
  .check-label { font-size: 14px; font-weight: 550; color: var(--text); }
  .check-label.done { color: var(--text-3); text-decoration: line-through; text-decoration-color: rgba(255,255,255,0.15); }
  .check-help { font-size: 12.5px; color: var(--text-3); margin-top: 4px; line-height: 1.5; }
  .optional-tag {
    font-size: 9.5px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.8px;
    background: rgba(116,168,255,0.1); color: var(--blue);
    padding: 2px 8px; border-radius: 99px; margin-left: 8px; vertical-align: 1px;
  }
  .subhead {
    margin: 20px 0 4px; font-size: 10.5px; color: var(--text-3);
    text-transform: uppercase; letter-spacing: 1.2px; font-weight: 700;
  }

  /* ── Forms ── */
  .form-group { margin-bottom: 16px; }
  .form-label {
    display: block; font-size: 11px; color: var(--text-2); margin-bottom: 7px;
    text-transform: uppercase; letter-spacing: 0.8px; font-weight: 650;
  }
  .form-input {
    width: 100%; padding: 10px 14px;
    background: rgba(255,255,255,0.03); border: 1px solid var(--border-strong);
    border-radius: 8px; color: var(--text); font-size: 14px; font-family: inherit;
    transition: border-color .15s, box-shadow .15s;
  }
  .form-input:focus { outline: none; border-color: var(--accent-deep); box-shadow: 0 0 0 3px rgba(62,207,142,0.12); }
  .form-input::placeholder { color: var(--text-3); }
  .form-row { display: flex; gap: 10px; align-items: flex-end; }
  .form-row .form-group { flex: 1; }
  .card .lede { font-size: 13px; color: var(--text-2); margin: -6px 0 18px; line-height: 1.6; }

  .btn {
    padding: 9px 20px; border: 1px solid transparent; border-radius: 8px;
    font-size: 13px; font-weight: 600; cursor: pointer; font-family: inherit;
    transition: background .15s, border-color .15s, transform .05s;
  }
  .btn:active { transform: translateY(1px); }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .btn-primary { background: var(--accent-deep); color: #eafff5; box-shadow: inset 0 1px 0 rgba(255,255,255,0.12); }
  .btn-primary:hover:not(:disabled) { background: #2ab27c; }
  .btn-danger { background: #8a2f36; color: #ffe9ea; }
  .btn-danger:hover:not(:disabled) { background: #a13940; }
  .btn-secondary { background: rgba(255,255,255,0.03); border-color: var(--border-strong); color: var(--text-2); }
  .btn-secondary:hover:not(:disabled) { color: var(--text); border-color: #3a4557; }
  .btn-sm { padding: 6px 13px; font-size: 12px; }
  .btn-group { display: flex; gap: 10px; margin-top: 16px; }

  .test-result { font-size: 13px; margin-top: 10px; padding: 9px 13px; border-radius: 8px; }
  .test-result.success { background: var(--accent-soft); color: var(--accent); border: 1px solid rgba(62,207,142,0.25); }
  .test-result.error { background: rgba(224,108,117,0.1); color: var(--red); border: 1px solid rgba(224,108,117,0.25); }
  .test-result.pending { background: rgba(255,255,255,0.04); color: var(--text-2); border: 1px solid var(--border); }

  /* ── Controls ── */
  .control-panel { display: grid; grid-template-columns: 1fr 1.4fr; gap: 16px; }
  @media (max-width: 900px) { .control-panel { grid-template-columns: 1fr; } }

  .status-big { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
  .status-big .dot { width: 13px; height: 13px; border-radius: 50%; }
  .status-big .dot.running { background: var(--accent); animation: pulse 2s infinite; }
  .status-big .dot.stopped { background: #4a5361; }
  .status-big .label { font-size: 19px; font-weight: 700; letter-spacing: -0.3px; }
  .status-big .label.running { color: var(--accent); }
  .status-big .label.stopped { color: var(--text-2); }
  .status-meta { font-size: 12px; color: var(--text-3); margin-bottom: 18px; font-variant-numeric: tabular-nums; min-height: 15px; }

  .log-viewer {
    background: #07080a; border: 1px solid var(--border); border-radius: 10px;
    padding: 14px 16px; font-family: var(--mono);
    font-size: 11.5px; color: #8fa39a; line-height: 1.65;
    max-height: 420px; overflow-y: auto; white-space: pre-wrap; word-break: break-all;
  }

  /* ── Help ── */
  .help-section { margin-bottom: 28px; }
  .help-section h2 { font-size: 17px; font-weight: 700; letter-spacing: -0.3px; margin-bottom: 10px; }
  .help-section p { font-size: 14px; color: var(--text-2); line-height: 1.7; margin-bottom: 10px; }
  .help-section code { background: rgba(255,255,255,0.05); padding: 2px 7px; border-radius: 5px; font-size: 12.5px; font-family: var(--mono); color: var(--text); }
  .help-section pre {
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 16px; font-size: 12.5px; font-family: var(--mono); color: var(--text-2); line-height: 1.7;
    overflow-x: auto; margin: 12px 0;
  }

  .file-table { width: 100%; font-size: 13px; border-collapse: collapse; }
  .file-table td { padding: 9px 12px; border-bottom: 1px solid var(--border); }
  .file-table tr:last-child td { border-bottom: none; }
  .file-table td:first-child { color: var(--text); font-family: var(--mono); font-size: 12px; white-space: nowrap; width: 220px; }
  .file-table td:last-child { color: var(--text-2); }

  details { margin-bottom: 8px; }
  details summary {
    cursor: pointer; padding: 12px 16px; background: var(--panel); border: 1px solid var(--border);
    border-radius: 10px; font-size: 13.5px; font-weight: 550; color: var(--text-2); list-style: none;
    transition: color .15s;
  }
  details summary:hover { color: var(--text); }
  details summary::-webkit-details-marker { display: none; }
  details summary::before { content: "+"; display: inline-block; width: 18px; color: var(--accent); font-weight: 700; }
  details[open] summary::before { content: "–"; }
  details[open] summary { border-radius: 10px 10px 0 0; border-bottom: none; color: var(--text); }
  details .faq-body {
    padding: 4px 16px 16px 34px; background: var(--panel); border: 1px solid var(--border); border-top: none;
    border-radius: 0 0 10px 10px; font-size: 13px; color: var(--text-2); line-height: 1.7;
  }

  /* ── Review queue & account briefs ── */
  .nav-count {
    display: none; margin-left: 6px; padding: 1px 7px; border-radius: 99px;
    background: var(--amber); color: #241a05; font-size: 10.5px; font-weight: 800;
    vertical-align: middle;
  }
  .nav-count.on { display: inline-block; }

  .review-card, .brief-card {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 12px; margin-bottom: 14px; overflow: hidden;
  }
  .review-card > .head, .brief-card > .head {
    padding: 16px 20px; border-bottom: 1px solid var(--border);
    display: flex; align-items: flex-start; justify-content: space-between; gap: 16px;
    flex-wrap: wrap;
  }
  .review-card .title, .brief-card .title {
    font-size: 15px; font-weight: 650; color: var(--text); letter-spacing: -0.2px;
  }
  .review-card .meta, .brief-card .meta {
    font-size: 12px; color: var(--text-3); margin-top: 4px;
  }
  .review-card .body, .brief-card .body { padding: 6px 20px 18px; }

  .email-step {
    border-left: 2px solid var(--border-strong);
    padding: 12px 0 12px 16px; margin-top: 12px;
  }
  .email-step .when {
    font-size: 10.5px; text-transform: uppercase; letter-spacing: 1px;
    color: var(--text-3); font-weight: 700;
  }
  .email-step .subject {
    font-size: 13.5px; font-weight: 650; color: var(--text); margin: 6px 0 8px;
  }
  .email-step .body-text {
    font-size: 13px; color: var(--text-2); line-height: 1.7; white-space: pre-wrap;
    font-family: var(--sans);
  }

  .review-actions {
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    padding: 14px 20px; border-top: 1px solid var(--border);
    background: rgba(255,255,255,0.015);
  }
  .review-actions input {
    flex: 1 1 220px; min-width: 0;
    background: var(--bg); border: 1px solid var(--border); color: var(--text);
    border-radius: 8px; padding: 8px 12px; font-size: 12.5px; font-family: inherit;
  }
  .review-actions input:focus { outline: none; border-color: var(--border-strong); }

  .fit-score {
    font-family: var(--mono); font-size: 20px; font-weight: 700; color: var(--accent);
    line-height: 1;
  }
  .fit-score.mid { color: var(--amber); }
  .fit-score.low { color: var(--red); }

  .brief-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 18px 28px; margin-top: 8px;
  }
  .brief-block h4 {
    font-size: 10.5px; text-transform: uppercase; letter-spacing: 1px;
    color: var(--text-3); font-weight: 700; margin-bottom: 8px;
  }
  .brief-block ul { list-style: none; }
  .brief-block li {
    font-size: 13px; color: var(--text-2); line-height: 1.6;
    padding-left: 14px; position: relative; margin-bottom: 5px;
  }
  .brief-block li::before {
    content: ""; position: absolute; left: 0; top: 9px;
    width: 4px; height: 4px; border-radius: 50%; background: var(--border-strong);
  }
  .brief-block.warn li::before { background: var(--red); }
  .brief-block p { font-size: 13px; color: var(--text-2); line-height: 1.7; }
  .sig { font-size: 10px; font-weight: 800; text-transform: uppercase;
         letter-spacing: 0.6px; margin-left: 6px; }
  .sig.high { color: var(--accent); }
  .sig.medium { color: var(--amber); }
  .sig.low { color: var(--text-3); }
  .evidence { color: var(--text-3); font-size: 12px; }

  /* ── Export ── */
  .export-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px;
  }
  .export-tile {
    background: var(--panel-raised); border: 1px solid var(--border);
    border-radius: 10px; padding: 16px 18px;
  }
  .export-tile h4 { font-size: 14px; font-weight: 650; color: var(--text); }
  .export-tile p {
    font-size: 12.5px; color: var(--text-3); line-height: 1.6; margin: 6px 0 12px;
  }
  .export-result {
    margin-top: 16px; font-size: 12.5px; color: var(--text-2);
    font-family: var(--mono); word-break: break-all;
  }

  /* ── Toast ── */
  .toast {
    position: fixed; bottom: 24px; right: 24px; padding: 12px 20px;
    border-radius: 10px; font-size: 13px; font-weight: 550; z-index: 1000;
    box-shadow: 0 12px 32px rgba(0,0,0,0.5);
    animation: toastIn .2s ease, toastOut .3s 2.2s forwards;
  }
  .toast.success { background: #0d2a1d; color: var(--accent); border: 1px solid rgba(62,207,142,0.4); }
  .toast.error { background: #2c1416; color: var(--red); border: 1px solid rgba(224,108,117,0.4); }
  @keyframes toastIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; } }
  @keyframes toastOut { from { opacity: 1; } to { opacity: 0; transform: translateY(6px); } }

  /* ── Tables ── */
  .table-card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
    overflow-x: auto;
  }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th {
    text-align: left; padding: 11px 16px; border-bottom: 1px solid var(--border);
    color: var(--text-3); font-weight: 700; font-size: 10.5px;
    text-transform: uppercase; letter-spacing: 1px; white-space: nowrap;
    background: rgba(255,255,255,0.015);
  }
  td {
    padding: 12px 16px; border-bottom: 1px solid var(--border); vertical-align: top;
    max-width: 300px; overflow: hidden; text-overflow: ellipsis; color: var(--text-2);
  }
  td:first-child { color: var(--text); font-weight: 550; }
  tr:last-child td { border-bottom: none; }
  tbody tr { transition: background .1s; }
  tbody tr:hover td { background: rgba(255,255,255,0.02); }
  .verified { color: var(--accent); }
  .muted { color: var(--text-3); }

  .badge {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 3px 10px; border-radius: 99px; font-size: 11px; font-weight: 650;
    border: 1px solid transparent; white-space: nowrap;
  }
  .badge::before { content: ""; width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
  .badge-new { background: rgba(116,168,255,0.1); color: var(--blue); border-color: rgba(116,168,255,0.22); }
  .badge-contacted, .badge-open, .badge-objection { background: rgba(229,181,103,0.1); color: var(--amber); border-color: rgba(229,181,103,0.22); }
  .badge-replied, .badge-active, .badge-interested { background: var(--accent-soft); color: var(--accent); border-color: rgba(62,207,142,0.25); }
  .badge-meeting { background: rgba(180,140,232,0.12); color: var(--purple); border-color: rgba(180,140,232,0.25); }
  .badge-draft { background: rgba(255,255,255,0.05); color: var(--text-2); border-color: var(--border-strong); }
  .badge-closed { background: rgba(255,255,255,0.04); color: var(--text-3); border-color: var(--border); }
  .badge-lost, .badge-not_interested { background: rgba(224,108,117,0.1); color: var(--red); border-color: rgba(224,108,117,0.22); }
  .badge-unknown { background: rgba(255,255,255,0.04); color: var(--text-3); border-color: var(--border); }

  .campaign-card, .convo-card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
    padding: 24px; margin-bottom: 16px;
  }
  .campaign-card h3, .convo-card h3 { font-size: 16px; font-weight: 700; letter-spacing: -0.2px; margin-bottom: 6px; }
  .campaign-card .meta, .convo-card .meta {
    font-size: 12px; color: var(--text-3); margin-bottom: 18px;
    display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
  }

  .email-step {
    border-left: 2px solid var(--border-strong); padding: 14px 20px; margin: 0 0 12px 6px;
    border-radius: 0 10px 10px 0; background: rgba(255,255,255,0.015);
  }
  .email-step .step-num {
    font-size: 10px; color: var(--accent); text-transform: uppercase;
    letter-spacing: 1px; font-weight: 700; margin-bottom: 7px;
  }
  .email-step .subject { font-size: 14px; font-weight: 650; margin-bottom: 8px; }
  .email-step .body { font-size: 13px; color: var(--text-2); line-height: 1.7; white-space: pre-wrap; }

  .thread-msg {
    padding: 12px 16px; margin-bottom: 8px; border-radius: 12px; max-width: 78%;
    font-size: 13px; line-height: 1.6; white-space: pre-wrap;
  }
  .thread-msg.sent { background: rgba(62,207,142,0.08); border: 1px solid rgba(62,207,142,0.16); color: #cfeee0; margin-left: auto; border-bottom-right-radius: 4px; }
  .thread-msg.received { background: rgba(255,255,255,0.04); border: 1px solid var(--border); color: var(--text-2); border-bottom-left-radius: 4px; }
  .thread-msg .sender { font-size: 10.5px; color: var(--text-3); margin-bottom: 5px; text-transform: uppercase; letter-spacing: 0.6px; font-weight: 700; }

  .activity-feed { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 8px 20px; }
  .activity-item { display: flex; gap: 18px; padding: 12px 0; border-bottom: 1px solid var(--border); font-size: 13px; align-items: baseline; }
  .activity-item:last-child { border-bottom: none; }
  .activity-item .time { color: var(--text-3); font-size: 12px; min-width: 130px; font-variant-numeric: tabular-nums; }
  .activity-item .agent {
    color: var(--accent); min-width: 84px; font-size: 11px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.8px;
  }
  .activity-item .action { color: var(--text-2); }

  /* ── Empty states ── */
  .empty {
    text-align: center; padding: 72px 24px;
    background: var(--panel); border: 1px dashed var(--border-strong); border-radius: 12px;
  }
  .empty .glyph {
    width: 46px; height: 46px; margin: 0 auto 18px; border-radius: 12px;
    background: rgba(255,255,255,0.03); border: 1px solid var(--border);
    display: flex; align-items: center; justify-content: center;
    font-size: 20px; color: var(--text-3);
  }
  .empty .title { font-size: 15px; font-weight: 650; color: var(--text); margin-bottom: 6px; }
  .empty .copy { font-size: 13px; color: var(--text-3); line-height: 1.6; max-width: 420px; margin: 0 auto; }
  .empty .copy b { color: var(--text-2); font-weight: 600; }

  @media (max-width: 700px) {
    header, main { padding-left: 18px; padding-right: 18px; }
    nav { padding: 0 10px; }
    .brand .tagline { display: none; }
  }
</style>
</head>
<body>

<header>
  <div class="brand">
    <div class="mark">L</div>
    <div>
      <h1>OpenVZ Leads</h1>
      <div class="tagline" data-i18n="brand.tagline">Find them. Understand them. Reach them.</div>
    </div>
  </div>
  <div class="header-controls">
    <div class="agent-status" id="header-status">
      <span class="status-dot stopped" id="header-dot"></span>
      <span id="header-status-text" data-i18n="status.checking">Checking…</span>
    </div>
    <button class="refresh-btn" onclick="toggleLang()" id="lang-btn">中文</button>
    <button class="refresh-btn" onclick="loadCurrentTab()" data-i18n="btn.refresh">Refresh</button>
  </div>
</header>

<nav>
  <button class="active" onclick="showTab('setup', this)" data-i18n="nav.setup">Setup</button>
  <button onclick="showTab('overview', this)" data-i18n="nav.overview">Overview</button>
  <button onclick="showTab('companies', this)" data-i18n="nav.companies">Companies</button>
  <button onclick="showTab('prospects', this)" data-i18n="nav.contacts">Contacts</button>
  <button onclick="showTab('briefs', this)" data-i18n="nav.briefs">Account briefs</button>
  <button onclick="showTab('review', this)">
    <span data-i18n="nav.review">Review</span><span class="nav-count" id="review-count"></span>
  </button>
  <button onclick="showTab('campaigns', this)" data-i18n="nav.campaigns">Campaigns</button>
  <button onclick="showTab('conversations', this)" data-i18n="nav.conversations">Conversations</button>
  <button onclick="showTab('export', this)" data-i18n="nav.export">Export</button>
  <button onclick="showTab('activity', this)" data-i18n="nav.activity">Activity</button>
  <button onclick="showTab('settings', this)" data-i18n="nav.settings">Settings</button>
  <button onclick="showTab('controls', this)" data-i18n="nav.controls">Controls</button>
  <button onclick="showTab('help', this)" data-i18n="nav.help">Help</button>
</nav>

<main>

<!-- Setup -->
<div id="setup" class="section active">
  <div class="section-head"><h2 data-i18n="setup.title">Setup</h2><p data-i18n="setup.sub">Everything OpenVZ Leads needs before it can start closing.</p></div>
  <div class="card">
    <div class="progress-wrap" id="setup-progress"></div>
    <div id="setup-checklist"></div>
  </div>
</div>

<!-- Overview -->
<div id="overview" class="section">
  <div class="section-head"><h2 data-i18n="overview.title">Pipeline Overview</h2><p data-i18n="overview.sub">Live counts from OpenVZ Leads' local database. Refreshes automatically.</p></div>
  <div class="stats-grid" id="stats-grid"></div>
</div>

<!-- Companies -->
<div id="companies" class="section">
  <div class="section-head"><h2 data-i18n="companies.title">Companies</h2><p data-i18n="companies.sub">Organizations OpenVZ Leads has researched. Click a row to see its contacts.</p></div>
  <div id="companies-list"></div>
</div>

<!-- Contacts -->
<div id="prospects" class="section">
  <div class="section-head"><h2 data-i18n="contacts.title">Contacts</h2><p data-i18n="contacts.sub">People OpenVZ Leads has found and verified.</p></div>
  <div id="prospects-table"></div>
</div>

<!-- Account briefs -->
<div id="briefs" class="section">
  <div class="section-head">
    <h2 data-i18n="briefs.title">Account briefs</h2>
    <p data-i18n="briefs.sub">What each account does, why they fit, what would make them buy, and who signs. Every point is a hypothesis over collected evidence — check the confidence line.</p>
  </div>
  <div id="briefs-list"></div>
</div>

<!-- Review queue -->
<div id="review" class="section">
  <div class="section-head">
    <h2 data-i18n="review.title">Review queue</h2>
    <p data-i18n="review.sub">Outreach drafted for you. Nothing is sent until you approve it here.</p>
  </div>
  <div id="review-list"></div>
</div>

<!-- Campaigns -->
<div id="campaigns" class="section">
  <div class="section-head"><h2 data-i18n="campaigns.title">Campaigns</h2><p data-i18n="campaigns.sub">Email sequences OpenVZ Leads has written and deployed.</p></div>
  <div id="campaigns-list"></div>
</div>

<!-- Export -->
<div id="export" class="section">
  <div class="section-head">
    <h2 data-i18n="export.title">Export</h2>
    <p data-i18n="export.sub">Take the work out. Files land in data/exports/ — no outbound channel needed.</p>
  </div>
  <div class="card">
    <div id="export-grid" class="export-grid"></div>
    <div id="export-result" class="export-result"></div>
  </div>
</div>

<!-- Conversations -->
<div id="conversations" class="section">
  <div class="section-head"><h2 data-i18n="conversations.title">Conversations</h2><p data-i18n="conversations.sub">Every reply, and how OpenVZ Leads handled it.</p></div>
  <div id="conversations-list"></div>
</div>

<!-- Activity -->
<div id="activity" class="section">
  <div class="section-head"><h2 data-i18n="activity.title">Activity</h2><p data-i18n="activity.sub">A running log of every action OpenVZ Leads' agents have taken.</p></div>
  <div id="activity-list"></div>
</div>

<!-- Settings -->
<div id="settings" class="section">
  <div class="section-head"><h2 data-i18n="settings.title">Settings</h2><p data-i18n-html="settings.sub">Credentials are stored locally in <span style="font-family:var(--mono);font-size:12px">.env</span> — never sent anywhere except the services themselves.</p></div>
  <div class="card">
    <h2 data-i18n="settings.instantlyTitle">Instantly (Email Platform)</h2>
    <p class="lede" data-i18n-html="settings.instantlyLede">Required. Get your API key from <a href="https://app.instantly.ai/app/settings/integrations" target="_blank" rel="noopener">Instantly Settings &gt; Integrations</a>.</p>
    <div class="form-group">
      <label class="form-label" for="instantly-key" data-i18n="settings.apiKey">API Key</label>
      <div class="form-row">
        <div class="form-group" style="margin-bottom:0">
          <input type="password" class="form-input" id="instantly-key" data-i18n-placeholder="settings.instantlyPlaceholder" placeholder="Enter your Instantly API key" autocomplete="off">
        </div>
        <button class="btn btn-secondary btn-sm" onclick="toggleVisibility('instantly-key')" data-i18n="settings.show">Show</button>
        <button class="btn btn-secondary btn-sm" onclick="testInstantly()" data-i18n="settings.test">Test</button>
      </div>
      <div id="instantly-test-result"></div>
    </div>
    <button class="btn btn-primary" onclick="saveInstantly()" data-i18n="settings.save">Save</button>
  </div>

  <div class="card">
    <h2>LinkedIn <span class="optional-tag" data-i18n="settings.optional">optional</span></h2>
    <p class="lede" data-i18n="settings.linkedinLede">For automated LinkedIn prospecting. OpenVZ Leads logs in and searches like a human.</p>
    <div class="form-group">
      <label class="form-label" for="linkedin-email" data-i18n="settings.emailOrUser">Email / Username</label>
      <input type="text" class="form-input" id="linkedin-email" placeholder="your@email.com" autocomplete="off">
    </div>
    <div class="form-group">
      <label class="form-label" for="linkedin-password" data-i18n="settings.password">Password</label>
      <input type="password" class="form-input" id="linkedin-password" data-i18n-placeholder="settings.passwordPlaceholder" placeholder="Enter password" autocomplete="new-password">
    </div>
    <button class="btn btn-primary" onclick="saveLinkedIn()" data-i18n="settings.save">Save</button>
  </div>

  <div class="card">
    <h2>Cloudflare <span class="optional-tag" data-i18n="settings.optional">optional</span></h2>
    <p class="lede" data-i18n="settings.cloudflareLede">For deep website crawling with JavaScript rendering during product training. ~$5/month.</p>
    <div class="form-group">
      <label class="form-label" for="cf-account-id" data-i18n="settings.accountId">Account ID</label>
      <input type="text" class="form-input" id="cf-account-id" data-i18n-placeholder="settings.cfAccountPlaceholder" placeholder="Your Cloudflare Account ID" autocomplete="off">
    </div>
    <div class="form-group">
      <label class="form-label" for="cf-api-token" data-i18n="settings.apiToken">API Token</label>
      <input type="password" class="form-input" id="cf-api-token" data-i18n-placeholder="settings.cfTokenPlaceholder" placeholder="Your Cloudflare API Token" autocomplete="off">
    </div>
    <button class="btn btn-primary" onclick="saveCloudflare()" data-i18n="settings.save">Save</button>
  </div>
</div>

<!-- Controls -->
<div id="controls" class="section">
  <div class="section-head"><h2 data-i18n="controls.title">Controls</h2><p data-i18n="controls.sub">Start and stop OpenVZ Leads' heartbeat loop, and watch what it's doing.</p></div>
  <div class="control-panel">
    <div class="card">
      <h2 data-i18n="controls.agent">Agent</h2>
      <div class="status-big" id="control-status">
        <div class="dot stopped" id="control-dot"></div>
        <span class="label stopped" id="control-label" data-i18n="controls.stopped">Stopped</span>
      </div>
      <div class="status-meta" id="control-meta"></div>
      <div class="btn-group">
        <button class="btn btn-primary" id="btn-start" onclick="startAgent()" data-i18n="controls.start">Start OpenVZ Leads</button>
        <button class="btn btn-danger" id="btn-stop" onclick="stopAgent()" style="display:none" data-i18n="controls.stop">Stop OpenVZ Leads</button>
      </div>
    </div>
    <div class="card">
      <h2 data-i18n="controls.logs">Recent Logs</h2>
      <div class="log-viewer" id="log-viewer" data-i18n="controls.noLogs">No logs yet. Start OpenVZ Leads to see activity.</div>
      <div class="btn-group">
        <button class="btn btn-secondary btn-sm" onclick="loadLogs()" data-i18n="controls.refreshLogs">Refresh Logs</button>
      </div>
    </div>
  </div>
</div>

<!-- Help -->
<div id="help" class="section">
  <div class="section-head"><h2 data-i18n="help.title">Help</h2><p data-i18n="help.sub">What OpenVZ Leads is, how it works, and how to fix the usual problems.</p></div>

  <div class="help-section">
    <h2 data-i18n="help.whatTitle">What is OpenVZ Leads?</h2>
    <p data-i18n="help.whatBody1">OpenVZ Leads is an autonomous AI sales agent. Once set up, OpenVZ Leads runs on its own: finds people who match your ideal customer, writes personalized cold emails, sends them through your email platform, reads every reply, handles objections, and works toward booking a meeting. You review everything through this dashboard.</p>
    <p data-i18n="help.whatBody2">OpenVZ Leads runs on your Claude Max subscription, so there are no extra AI costs. Everything stays on your machine in one folder.</p>
  </div>

  <div class="help-section">
    <h2 data-i18n="help.startTitle">Getting Started</h2>
    <p data-i18n="help.startBody">There are three things to do:</p>
    <pre data-i18n="help.startSteps">1. Go to the Settings tab and enter your Instantly API key
2. Train OpenVZ Leads on your product (through Claude or the command line)
3. Go to the Controls tab and click Start</pre>
    <p data-i18n="help.startNote">The Setup tab shows you exactly what's done and what still needs to happen.</p>
  </div>

  <div class="help-section">
    <h2 data-i18n="help.whereTitle">Where Everything Lives</h2>
    <p data-i18n="help.whereBody">Everything OpenVZ Leads needs is inside this one project folder. Nothing is stored elsewhere.</p>
    <div class="table-card" style="padding:6px 4px">
    <table class="file-table">
      <tr><td>.env</td><td data-i18n="help.file.env">Your API keys and credentials (never shared or committed)</td></tr>
      <tr><td>openvz-leads.yaml</td><td data-i18n="help.file.yaml">Your product info, target customers, and behavior settings</td></tr>
      <tr><td>skills/</td><td data-i18n="help.file.skills">Sales knowledge files. Edit these to change how OpenVZ Leads writes and sells.</td></tr>
      <tr><td>skills/product_knowledge.md</td><td data-i18n="help.file.product">Everything OpenVZ Leads knows about your product (auto-generated from training)</td></tr>
      <tr><td>prompts/</td><td data-i18n="help.file.prompts">Prompt templates for each agent. Advanced customization.</td></tr>
      <tr><td>data/leads.db</td><td data-i18n="help.file.db">Database with all prospects, campaigns, and conversations</td></tr>
      <tr><td>data/leads.log</td><td data-i18n="help.file.log">Log file showing what OpenVZ Leads is doing</td></tr>
    </table>
    </div>
  </div>

  <div class="help-section">
    <h2 data-i18n="help.keysTitle">Getting Your API Keys</h2>

    <details>
      <summary data-i18n="help.instantlySummary">Instantly API Key (required)</summary>
      <div class="faq-body" data-i18n-html="help.instantlyBody">
        <p>Instantly is the email platform OpenVZ Leads uses to send campaigns.</p>
        <p>1. Sign up at <a href="https://instantly.ai" target="_blank" rel="noopener">instantly.ai</a> (you need the Growth plan for API access)</p>
        <p>2. Go to Settings &gt; Integrations</p>
        <p>3. Copy your API key</p>
        <p>4. Paste it in the Settings tab here</p>
      </div>
    </details>

    <details>
      <summary data-i18n="help.linkedinSummary">LinkedIn Credentials (optional)</summary>
      <div class="faq-body" data-i18n-html="help.linkedinBody">
        <p>If you want OpenVZ Leads to find prospects on LinkedIn, enter your LinkedIn email and password. OpenVZ Leads uses a real browser to search LinkedIn like a human would, with random delays and rate limits to avoid detection.</p>
        <p>If you skip this, OpenVZ Leads will find prospects through Google searches and company website scraping instead.</p>
      </div>
    </details>

    <details>
      <summary data-i18n="help.cloudflareSummary">Cloudflare Browser Rendering (optional)</summary>
      <div class="faq-body" data-i18n-html="help.cloudflareBody">
        <p>This is only used during product training (when OpenVZ Leads crawls your website to learn about your product). It handles JavaScript-heavy websites that a basic crawler can't read.</p>
        <p>1. Sign up at <a href="https://dash.cloudflare.com" target="_blank" rel="noopener">Cloudflare</a> (paid Workers plan, ~$5/month)</p>
        <p>2. Go to Workers &amp; Pages &gt; Browser Rendering</p>
        <p>3. Create an API token with Browser Rendering Edit permissions</p>
        <p>4. Enter your Account ID and API Token in the Settings tab</p>
        <p>Without this, OpenVZ Leads uses a built-in crawler that works fine for most websites but can't render JavaScript.</p>
      </div>
    </details>
  </div>

  <div class="help-section">
    <h2 data-i18n="help.issuesTitle">Common Issues</h2>

    <details>
      <summary data-i18n="help.issue.notFound">"command not found: openvz-leads"</summary>
      <div class="faq-body" data-i18n-html="help.issue.notFoundBody">You need to activate the virtual environment first: <code>source .venv/bin/activate</code></div>
    </details>

    <details>
      <summary data-i18n="help.issue.401">Instantly API returns 401</summary>
      <div class="faq-body" data-i18n-html="help.issue.401Body">Your API key is wrong, or you need the Growth plan (the free plan doesn't include API access). Double-check the key in Settings &gt; Integrations in your Instantly dashboard.</div>
    </details>

    <details>
      <summary data-i18n="help.issue.claude">Claude headless mode fails</summary>
      <div class="faq-body" data-i18n-html="help.issue.claudeBody">Make sure you've run <code>claude login</code> in your terminal and have an active Claude Max subscription. OpenVZ Leads uses your existing subscription, not a separate API key.</div>
    </details>

    <details>
      <summary data-i18n="help.issue.noProspects">OpenVZ Leads isn't finding prospects</summary>
      <div class="faq-body" data-i18n-html="help.issue.noProspectsBody">Check that your ICP (ideal customer profile) in openvz-leads.yaml has realistic titles, industries, and geography. If LinkedIn is set up, check that the credentials are correct. Check the Activity tab to see what OpenVZ Leads has been trying to do.</div>
    </details>
  </div>
</div>

</main>

<script>
let currentTab = 'setup';
let companyDrill = false;      // true while viewing a single company's contacts
let _companies = [], _prospects = [], _campaigns = [];

// ── Language ──
// Only the chrome and the strings this dashboard writes are translated. Data
// from the database (company names, email bodies, account briefs) is shown as
// stored — the brief's language is set by profiling.output_language in
// openvz-leads.yaml, not here.

const I18N = {
  zh: {
    'brand.tagline': '找得到 · 看得懂 · 写得出',
    'btn.refresh': '刷新',
    'nav.setup': '配置', 'nav.overview': '总览', 'nav.companies': '公司',
    'nav.contacts': '联系人', 'nav.briefs': '客户分析', 'nav.review': '待审核',
    'nav.campaigns': '活动', 'nav.conversations': '对话', 'nav.export': '导出',
    'nav.activity': '动态', 'nav.settings': '设置', 'nav.controls': '控制台',
    'nav.help': '帮助',
    'briefs.title': '客户分析',
    'briefs.sub': '每个客户是做什么的、为什么匹配、什么会让他们买单、谁拍板。全部是基于已采集证据的推断——请看置信度那一行。',
    'review.title': '审核队列',
    'review.sub': '已为你起草的开发信。你在这里批准之前，不会发出任何一封。',
    'campaigns.title': '活动', 'campaigns.sub': '已写好和已投递的邮件序列。',
    'export.title': '导出',
    'export.sub': '把成果带走。文件写到 data/exports/，不需要配置任何发信通道。',
    'export.leads': '客户名单', 'export.leadsDesc': '所有联系人及其打分、状态和来源。',
    'export.profiles': '客户分析', 'export.profilesDesc': '完整的客户分析报告。',
    'export.emails': '开发信', 'export.emailsDesc': '每个活动的完整邮件序列和收件人。',
    'export.running': '正在导出…', 'export.done': '已导出：',
    'review.approve': '批准', 'review.reject': '拒绝',
    'review.notePlaceholder': '备注（可选，会随决定一起保存）',
    'review.recipients': '收件人',
    'review.empty': '没有待审核的内容',
    'review.emptyCopy': '当 OpenVZ Leads 写完开发信后，会出现在这里等你批准。',
    'review.approved': '已批准', 'review.rejected': '已拒绝',
    'review.gone': '这个活动已经不在待审核状态了。',
    'briefs.empty': '还没有客户分析',
    'briefs.emptyCopy': '找到客户后，OpenVZ Leads 会逐个分析。运行 <b>openvz-leads run</b> 开始。',
    'brief.fit': '匹配度', 'brief.confidence': '置信度',
    'brief.whatTheyDo': '他们是做什么的', 'brief.why': '为什么匹配',
    'brief.risks': '风险', 'brief.pains': '可能的痛点',
    'brief.signals': '采购信号', 'brief.chain': '决策链',
    'brief.angles': '破冰角度', 'brief.avoid': '不要说',
    'brief.gaps': '证据缺口',
    'brief.role': '这个联系人', 'brief.buyer': '拍板人', 'brief.champion': '内部推动者',
    'brief.blocker': '阻力方',
    'offline.title': '连不上仪表盘服务',
    'offline.copy': '仪表盘进程可能已经停止。用 <b>openvz-leads dashboard</b> 重启后刷新本页。',
     // ── 界面外壳 ──
    'status.checking': '检查中…',
    'status.offline': '连不上',
    'status.running': 'OpenVZ Leads 正在运行',
    'status.stopped': 'OpenVZ Leads 已停止',
    // ── 各分区标题 ──
    'setup.title': '配置',
    'setup.sub': 'OpenVZ Leads 开始干活之前需要的全部东西。',
    'overview.title': '管道总览',
    'overview.sub': '来自本机数据库的实时计数，会自动刷新。',
    'companies.title': '公司',
    'companies.sub': 'OpenVZ Leads 调研过的公司。点一行看它下面的联系人。',
    'contacts.title': '联系人',
    'contacts.sub': 'OpenVZ Leads 找到并核实过的人。',
    'conversations.title': '对话',
    'conversations.sub': '每一封回信，以及 OpenVZ Leads 是怎么处理的。',
    'activity.title': '动态',
    'activity.sub': '各个 agent 做过的每一个动作的流水账。',
    'settings.title': '设置',
    'settings.sub': '凭据只存在本机的 <span style="font-family:var(--mono);font-size:12px">.env</span> 里 —— 除了对应的服务本身，不会发去任何地方。',
    'controls.title': '控制台',
    'controls.sub': '启动和停止心跳循环，并观察它在做什么。',
    'help.title': '帮助',
    'help.sub': 'OpenVZ Leads 是什么、怎么运转，以及常见问题怎么解决。',
    // ── 配置进度 ──
    'setup.percent': '完成 {pct}%',
    'setup.steps': '必做步骤已完成 {done}/{total}',
    'setup.optionalHead': '可选',
    'setup.optionalTag': '可选',
    'setup.check.claude_cli.label': 'Claude Code CLI',
    'setup.check.claude_cli.help': '这是它的思考引擎，跑在你已有的 Claude 订阅上。到 https://claude.ai/download 装好，然后执行：claude login',
    'setup.check.venv.label': 'Python 虚拟环境',
    'setup.check.venv.help': '执行：python3 -m venv .venv && source .venv/bin/activate && pip install -e .',
    'setup.check.config.label': '你的产品已配置',
    'setup.check.config.help': '把它指向你自己的网站让它自学：openvz-leads train https://your-company.com —— 也可以让 Claude 带你走一遍。',
    'setup.check.product_trained.label': '产品知识已写入',
    'setup.check.product_trained.help': '由 “openvz-leads train <url>” 连同配置一起生成。',
    'setup.check.serper.label': '搜索 API 密钥（推荐）',
    'setup.check.serper.help': '没有它，找人只能去爬 DuckDuckGo 和 Bing，很快就会被限流。serper.dev 大约 5 美元 2500 次搜索，不是订阅制。在「设置」里填。',
    'setup.check.instantly.label': '发信通道（可选）',
    'setup.check.instantly.help': '只有想让它替你发信才需要。不配也一样：开发信照写、进审核队列、从「导出」拿走。人工审核这一环两种情况下都在。',
    // ── 设置页 ──
    'settings.instantlyTitle': 'Instantly（发信平台）',
    'settings.instantlyLede': '必填。在 <a href="https://app.instantly.ai/app/settings/integrations" target="_blank" rel="noopener">Instantly 设置 &gt; Integrations</a> 里拿 API 密钥。',
    'settings.apiKey': 'API 密钥',
    'settings.instantlyPlaceholder': '填入你的 Instantly API 密钥',
    'settings.show': '显示',
    'settings.test': '测试',
    'settings.save': '保存',
    'settings.optional': '可选',
    'settings.linkedinLede': '用于自动在 LinkedIn 上找人。OpenVZ Leads 会像真人一样登录并搜索。',
    'settings.emailOrUser': '邮箱 / 用户名',
    'settings.password': '密码',
    'settings.passwordPlaceholder': '输入密码',
    'settings.passwordSaved': '密码已保存（要改就输入新的）',
    'settings.cloudflareLede': '用于产品训练阶段抓取需要执行 JavaScript 的网站。约每月 5 美元。',
    'settings.accountId': 'Account ID',
    'settings.apiToken': 'API Token',
    'settings.cfAccountPlaceholder': '你的 Cloudflare Account ID',
    'settings.cfTokenPlaceholder': '你的 Cloudflare API Token',
    'settings.savedInstantly': 'Instantly API 密钥已保存。',
    'settings.savedLinkedIn': 'LinkedIn 凭据已保存。',
    'settings.savedCloudflare': 'Cloudflare 凭据已保存。',
    'settings.saveFailed': '保存失败 —— 仪表盘还在运行吗？',
    'settings.testing': '正在测试…',
    'settings.testUnreachable': '连不上仪表盘服务。',
    // ── 控制台 ──
    'controls.agent': 'Agent',
    'controls.start': '启动 OpenVZ Leads',
    'controls.stop': '停止 OpenVZ Leads',
    'controls.logs': '最近日志',
    'controls.refreshLogs': '刷新日志',
    'controls.noLogs': '还没有日志。启动 OpenVZ Leads 后这里会有动静。',
    'controls.running': '运行中',
    'controls.stopped': '已停止',
    'controls.startedAt': '启动于',
    'controls.idleNote': 'OpenVZ Leads 每隔几分钟醒一次，把该做的做掉，然后接着睡。',
    'controls.started': 'OpenVZ Leads 已启动。',
    'controls.wasStopped': 'OpenVZ Leads 已停止。',
    'controls.startFailed': '启动失败。',
    'controls.stopFailed': '停止失败。',
    // ── 总览 ──
    'stats.prospects': '联系人',
    'stats.campaigns': '活动',
    'stats.conversations': '对话',
    'stats.actions': '已记录动作',
    'stats.claudeToday': '今日 Claude 调用',
    'stats.noneYet': '暂无',
    'stats.empty': '还没有管道数据',
    'stats.emptyCopy': '到 <b>控制台</b> 标签页把 OpenVZ Leads 启动起来，它就会自己开始找人、写信、发信。',
    // ── 公司 ──
    'companies.empty': '还没有公司',
    'companies.emptyCopy': 'Scout agent 还没调研过任何公司。先把<b>配置</b>做完，再到<b>控制台</b>启动 OpenVZ Leads。',
    'companies.col.name': '公司',
    'companies.col.domain': '域名',
    'companies.col.industry': '行业',
    'companies.col.size': '规模',
    'companies.col.location': '地区',
    'companies.col.contacts': '联系人数',
    'companies.col.source': '来源',
    'companies.col.added': '加入时间',
    'companies.contactsOf': '{company} —— 联系人',
    'companies.back': '&larr; 返回公司列表',
    'companies.noContacts': '这家公司下面还没有联系人。',
    // ── 联系人 ──
    'contacts.empty': '还没有联系人',
    'contacts.emptyCopy': 'OpenVZ Leads 还没找到人。跑起来之后，Scout agent 会按 <b>openvz-leads.yaml</b> 里的理想客户画像去网上找人。',
    'contacts.col.name': '姓名',
    'contacts.col.title': '职位',
    'contacts.col.company': '公司',
    'contacts.col.email': '邮箱',
    'contacts.col.phone': '电话',
    'contacts.col.linkedin': 'LinkedIn',
    'contacts.col.status': '状态',
    'contacts.col.source': '来源',
    'contacts.col.added': '加入时间',
    'contacts.profile': '主页',
    'contacts.feedback': '反馈',
    // ── 活动 ──
    'campaigns.empty': '还没有活动',
    'campaigns.emptyCopy': 'Writer agent 还没起草任何序列。等 OpenVZ Leads 攒够打过分的联系人，它会自己开始写。',
    'campaigns.untitled': '未命名活动',
    'campaigns.prospects': '{count} 位联系人',
    'campaigns.sendAfter': '{days} 天后发送',
    'campaigns.noSteps': '这个活动里还没有邮件。',
    'campaigns.emailN': '邮件 {step}',
    // ── 对话 ──
    'conversations.empty': '还没有对话',
    'conversations.emptyCopy': '目前还没有人回信。有人回了之后，Handler agent 会给每封回信分类并作答，每个会话都会出现在这里。',
    'conversations.unknown': '未知',
    'conversations.noMessages': '这个会话里还没有消息。',
    // ── 动态 ──
    'activity.empty': '还没有动态',
    'activity.emptyCopy': 'OpenVZ Leads 还没做过任何动作。找到的每个人、写出的每封信、处理的每条回复，都会在发生的那一刻出现在这里。',
    // ── 审核 ──
    'review.sendsImmediately': '立即发送',
    'review.daysLater': '{days} 天后',
    'review.emailN': '邮件 {step}',
    'review.emailCount': '{count} 封邮件',
    'review.recipientCount': '{count} 位收件人',
    'review.recipientsWithCount': '收件人（{count}）',
    // ── 导出 ──
    'export.failed': '导出失败',
    // ── 反馈 ──
    'feedback.prompt': '写下你的反馈：',
    'feedback.contact': '对这位联系人的反馈：',
    'feedback.campaign': '对这个活动的反馈：',
    'feedback.saved': '反馈已保存，OpenVZ Leads 会把它考虑进去。',
    'feedback.failed': '反馈没能保存。',
    // ── 帮助页 ──
    'help.whatTitle': 'OpenVZ Leads 是什么？',
    'help.whatBody1': 'OpenVZ Leads 是一个自主运行的 AI 销售 agent。配置好之后它自己跑：找到符合你理想客户画像的人，写出个性化的开发信，通过你的发信平台发出去，读每一封回信，处理异议，一路推到约上会。所有环节你都在这个仪表盘里过目。',
    'help.whatBody2': 'OpenVZ Leads 跑在你的 Claude Max 订阅上，没有额外的模型费用。所有东西都留在你自己的机器上，就在一个文件夹里。',
    'help.startTitle': '怎么开始',
    'help.startBody': '一共三件事：',
    'help.startSteps': '1. 到「设置」标签页填上你的 Instantly API 密钥\n2. 让 OpenVZ Leads 学习你的产品（通过 Claude 或命令行）\n3. 到「控制台」标签页点启动',
    'help.startNote': '「配置」标签页会明确告诉你哪些做完了、还差什么。',
    'help.whereTitle': '东西都在哪',
    'help.whereBody': 'OpenVZ Leads 需要的一切都在这一个项目文件夹里，不会存到别处。',
    'help.file.env': '你的 API 密钥和凭据（不会外传，也不会提交）',
    'help.file.yaml': '你的产品信息、目标客户和行为设置',
    'help.file.skills': '销售知识文件。改这些就能改变它怎么写、怎么卖。',
    'help.file.product': 'OpenVZ Leads 关于你产品的全部认知（训练时自动生成）',
    'help.file.prompts': '每个 agent 的提示词模板。进阶定制用。',
    'help.file.db': '数据库，装着全部联系人、活动和对话',
    'help.file.log': '日志文件，看它此刻在做什么',
    'help.keysTitle': '怎么拿到这些密钥',
    'help.instantlySummary': 'Instantly API 密钥（必填）',
    'help.instantlyBody': '<p>Instantly 是 OpenVZ Leads 用来发送活动的邮件平台。</p><p>1. 到 <a href="https://instantly.ai" target="_blank" rel="noopener">instantly.ai</a> 注册（要 Growth 套餐才有 API 权限）</p><p>2. 进 Settings &gt; Integrations</p><p>3. 复制 API 密钥</p><p>4. 粘贴到这里的「设置」标签页</p>',
    'help.linkedinSummary': 'LinkedIn 账号（可选）',
    'help.linkedinBody': '<p>如果你想让 OpenVZ Leads 去 LinkedIn 上找人，就填上你的 LinkedIn 邮箱和密码。它用真实浏览器像真人那样搜索，带随机延时和频率限制，避免被识别。</p><p>不填也行，它会改用 Google 搜索和公司官网抓取来找人。</p>',
    'help.cloudflareSummary': 'Cloudflare 浏览器渲染（可选）',
    'help.cloudflareBody': '<p>只在产品训练阶段用得上（也就是 OpenVZ Leads 抓你的网站学习你的产品那一步）。它能处理基础爬虫读不了的重 JavaScript 网站。</p><p>1. 到 <a href="https://dash.cloudflare.com" target="_blank" rel="noopener">Cloudflare</a> 注册（付费 Workers 套餐，约每月 5 美元）</p><p>2. 进 Workers &amp; Pages &gt; Browser Rendering</p><p>3. 创建一个带 Browser Rendering Edit 权限的 API token</p><p>4. 把 Account ID 和 API Token 填到「设置」标签页</p><p>不配也行，它会用内置爬虫，对大多数网站够用，只是不能执行 JavaScript。</p>',
    'help.issuesTitle': '常见问题',
    'help.issue.notFound': '“command not found: openvz-leads”',
    'help.issue.notFoundBody': '要先激活虚拟环境：<code>source .venv/bin/activate</code>',
    'help.issue.401': 'Instantly API 返回 401',
    'help.issue.401Body': '密钥不对，或者你需要 Growth 套餐（免费套餐没有 API 权限）。到 Instantly 后台的 Settings &gt; Integrations 里再核对一遍密钥。',
    'help.issue.claude': 'Claude 无头模式失败',
    'help.issue.claudeBody': '确认你已经在终端跑过 <code>claude login</code>，并且 Claude Max 订阅在有效期内。OpenVZ Leads 用的是你已有的订阅，不是另外的 API 密钥。',
    'help.issue.noProspects': 'OpenVZ Leads 找不到人',
    'help.issue.noProspectsBody': '检查 openvz-leads.yaml 里的理想客户画像，职位、行业和地区是否现实。如果配了 LinkedIn，检查账号密码对不对。再到「动态」标签页看看它一直在试着做什么。',
  },
  en: {
    'brand.tagline': 'Find them. Understand them. Reach them.',
    'btn.refresh': 'Refresh',
    'nav.setup': 'Setup', 'nav.overview': 'Overview', 'nav.companies': 'Companies',
    'nav.contacts': 'Contacts', 'nav.briefs': 'Account briefs', 'nav.review': 'Review',
    'nav.campaigns': 'Campaigns', 'nav.conversations': 'Conversations', 'nav.export': 'Export',
    'nav.activity': 'Activity', 'nav.settings': 'Settings', 'nav.controls': 'Controls',
    'nav.help': 'Help',
    'briefs.title': 'Account briefs',
    'briefs.sub': 'What each account does, why they fit, what would make them buy, and who signs. Every point is a hypothesis over collected evidence — check the confidence line.',
    'review.title': 'Review queue',
    'review.sub': 'Outreach drafted for you. Nothing is sent until you approve it here.',
    'campaigns.title': 'Campaigns', 'campaigns.sub': 'Email sequences OpenVZ Leads has written and deployed.',
    'export.title': 'Export',
    'export.sub': 'Take the work out. Files land in data/exports/ — no outbound channel needed.',
    'export.leads': 'Leads', 'export.leadsDesc': 'Every contact with score, status and source.',
    'export.profiles': 'Account briefs', 'export.profilesDesc': 'The full written analysis per account.',
    'export.emails': 'Outreach drafts', 'export.emailsDesc': 'Full sequences and recipients per campaign.',
    'export.running': 'Exporting…', 'export.done': 'Exported to ',
    'review.approve': 'Approve', 'review.reject': 'Reject',
    'review.notePlaceholder': 'Note (optional — saved with your decision)',
    'review.recipients': 'Recipients',
    'review.empty': 'Nothing to review',
    'review.emptyCopy': 'Drafts appear here for your approval once OpenVZ Leads has written them.',
    'review.approved': 'Approved', 'review.rejected': 'Rejected',
    'review.gone': 'That campaign is no longer awaiting review.',
    'briefs.empty': 'No account briefs yet',
    'briefs.emptyCopy': 'Once prospects are found, OpenVZ Leads analyses them one by one. Start with <b>openvz-leads run</b>.',
    'brief.fit': 'Fit', 'brief.confidence': 'confidence',
    'brief.whatTheyDo': 'What they do', 'brief.why': 'Why they fit',
    'brief.risks': 'Risks', 'brief.pains': 'Likely pains',
    'brief.signals': 'Buying signals', 'brief.chain': 'Decision chain',
    'brief.angles': 'Opening angles', 'brief.avoid': 'Do not say',
    'brief.gaps': 'Evidence gaps',
    'brief.role': 'This contact', 'brief.buyer': 'Economic buyer', 'brief.champion': 'Champion',
    'brief.blocker': 'Blocker',
    'offline.title': "Dashboard can't reach the server",
    'offline.copy': 'The dashboard process may have stopped. Restart it with <b>openvz-leads dashboard</b> and refresh this page.',
     // ── Chrome ──
    'status.checking': 'Checking…',
    'status.offline': 'Offline',
    'status.running': 'OpenVZ Leads is running',
    'status.stopped': 'OpenVZ Leads is stopped',
    // ── Section heads ──
    'setup.title': 'Setup',
    'setup.sub': "Everything OpenVZ Leads needs before it can start closing.",
    'overview.title': 'Pipeline Overview',
    'overview.sub': "Live counts from OpenVZ Leads' local database. Refreshes automatically.",
    'companies.title': 'Companies',
    'companies.sub': 'Organizations OpenVZ Leads has researched. Click a row to see its contacts.',
    'contacts.title': 'Contacts',
    'contacts.sub': 'People OpenVZ Leads has found and verified.',
    'conversations.title': 'Conversations',
    'conversations.sub': 'Every reply, and how OpenVZ Leads handled it.',
    'activity.title': 'Activity',
    'activity.sub': "A running log of every action OpenVZ Leads' agents have taken.",
    'settings.title': 'Settings',
    'settings.sub': 'Credentials are stored locally in <span style="font-family:var(--mono);font-size:12px">.env</span> — never sent anywhere except the services themselves.',
    'controls.title': 'Controls',
    'controls.sub': "Start and stop OpenVZ Leads' heartbeat loop, and watch what it's doing.",
    'help.title': 'Help',
    'help.sub': 'What OpenVZ Leads is, how it works, and how to fix the usual problems.',
    // ── Setup progress ──
    'setup.percent': '{pct}% complete',
    'setup.steps': '{done} of {total} required steps done',
    'setup.optionalHead': 'Optional',
    'setup.optionalTag': 'optional',
    'setup.check.claude_cli.label': 'Claude Code CLI',
    'setup.check.claude_cli.help': 'This is the thinking engine, and it runs on the Claude subscription you already have. Install it from https://claude.ai/download, then run: claude login',
    'setup.check.venv.label': 'Python virtual environment',
    'setup.check.venv.help': 'Run: python3 -m venv .venv && source .venv/bin/activate && pip install -e .',
    'setup.check.config.label': 'Your product configured',
    'setup.check.config.help': 'Point it at your own site and let it learn: openvz-leads train https://your-company.com — or ask Claude to walk you through it.',
    'setup.check.product_trained.label': 'Product knowledge written',
    'setup.check.product_trained.help': "Generated by 'openvz-leads train <url>' together with the config.",
    'setup.check.serper.label': 'Search API key (recommended)',
    'setup.check.serper.help': 'Without one, prospecting scrapes DuckDuckGo and Bing and gets rate-limited. serper.dev is about $5 per 2,500 searches, and is not a subscription. Add it in Settings.',
    'setup.check.instantly.label': 'Sending provider (optional)',
    'setup.check.instantly.help': 'Only needed if you want it to send for you. Without it, outreach is drafted, queued for your review, and exported from the Export tab. Human review stays on either way.',
    // ── Settings ──
    'settings.instantlyTitle': 'Instantly (Email Platform)',
    'settings.instantlyLede': 'Required. Get your API key from <a href="https://app.instantly.ai/app/settings/integrations" target="_blank" rel="noopener">Instantly Settings &gt; Integrations</a>.',
    'settings.apiKey': 'API Key',
    'settings.instantlyPlaceholder': 'Enter your Instantly API key',
    'settings.show': 'Show',
    'settings.test': 'Test',
    'settings.save': 'Save',
    'settings.optional': 'optional',
    'settings.linkedinLede': 'For automated LinkedIn prospecting. OpenVZ Leads logs in and searches like a human.',
    'settings.emailOrUser': 'Email / Username',
    'settings.password': 'Password',
    'settings.passwordPlaceholder': 'Enter password',
    'settings.passwordSaved': 'Password saved (enter new to change)',
    'settings.cloudflareLede': 'For deep website crawling with JavaScript rendering during product training. ~$5/month.',
    'settings.accountId': 'Account ID',
    'settings.apiToken': 'API Token',
    'settings.cfAccountPlaceholder': 'Your Cloudflare Account ID',
    'settings.cfTokenPlaceholder': 'Your Cloudflare API Token',
    'settings.savedInstantly': 'Instantly API key saved.',
    'settings.savedLinkedIn': 'LinkedIn credentials saved.',
    'settings.savedCloudflare': 'Cloudflare credentials saved.',
    'settings.saveFailed': 'Save failed — is the dashboard still running?',
    'settings.testing': 'Testing…',
    'settings.testUnreachable': 'Could not reach the dashboard server.',
    // ── Controls ──
    'controls.agent': 'Agent',
    'controls.start': 'Start OpenVZ Leads',
    'controls.stop': 'Stop OpenVZ Leads',
    'controls.logs': 'Recent Logs',
    'controls.refreshLogs': 'Refresh Logs',
    'controls.noLogs': 'No logs yet. Start OpenVZ Leads to see activity.',
    'controls.running': 'Running',
    'controls.stopped': 'Stopped',
    'controls.startedAt': 'started',
    'controls.idleNote': 'OpenVZ Leads wakes every few minutes, does what needs doing, and sleeps.',
    'controls.started': 'OpenVZ Leads started.',
    'controls.wasStopped': 'OpenVZ Leads stopped.',
    'controls.startFailed': 'Failed to start.',
    'controls.stopFailed': 'Failed to stop.',
    // ── Overview ──
    'stats.prospects': 'Prospects',
    'stats.campaigns': 'Campaigns',
    'stats.conversations': 'Conversations',
    'stats.actions': 'Actions Logged',
    'stats.claudeToday': 'Claude calls today',
    'stats.noneYet': 'none yet',
    'stats.empty': 'No pipeline data yet',
    'stats.emptyCopy': 'Start OpenVZ Leads from the <b>Controls</b> tab and it will begin prospecting, writing, and sending on its own.',
    // ── Companies ──
    'companies.empty': 'No companies yet',
    'companies.emptyCopy': "OpenVZ Leads' Scout agent hasn't researched any companies. Finish <b>Setup</b>, then start OpenVZ Leads from the <b>Controls</b> tab.",
    'companies.col.name': 'Company',
    'companies.col.domain': 'Domain',
    'companies.col.industry': 'Industry',
    'companies.col.size': 'Size',
    'companies.col.location': 'Location',
    'companies.col.contacts': 'Contacts',
    'companies.col.source': 'Source',
    'companies.col.added': 'Added',
    'companies.contactsOf': '{company} — Contacts',
    'companies.back': '&larr; Back to Companies',
    'companies.noContacts': 'No contacts found at this company yet.',
    // ── Contacts ──
    'contacts.empty': 'No contacts yet',
    'contacts.emptyCopy': "OpenVZ Leads hasn't found any prospects. Once it's running, the Scout agent searches the web for people matching your ideal customer profile in <b>openvz-leads.yaml</b>.",
    'contacts.col.name': 'Name',
    'contacts.col.title': 'Title',
    'contacts.col.company': 'Company',
    'contacts.col.email': 'Email',
    'contacts.col.phone': 'Phone',
    'contacts.col.linkedin': 'LinkedIn',
    'contacts.col.status': 'Status',
    'contacts.col.source': 'Source',
    'contacts.col.added': 'Added',
    'contacts.profile': 'Profile',
    'contacts.feedback': 'Feedback',
    // ── Campaigns ──
    'campaigns.empty': 'No campaigns yet',
    'campaigns.emptyCopy': "The Writer agent hasn't drafted any sequences. It kicks in automatically once OpenVZ Leads has scored prospects to write for.",
    'campaigns.untitled': 'Untitled Campaign',
    'campaigns.prospects': '{count} prospect(s)',
    'campaigns.sendAfter': 'send after {days} days',
    'campaigns.noSteps': 'No email steps in this campaign.',
    'campaigns.emailN': 'Email {step}',
    // ── Conversations ──
    'conversations.empty': 'No conversations yet',
    'conversations.emptyCopy': 'No prospects have replied so far. When they do, the Handler agent classifies each reply and responds — every thread shows up here.',
    'conversations.unknown': 'Unknown',
    'conversations.noMessages': 'No messages in this thread yet.',
    // ── Activity ──
    'activity.empty': 'No activity yet',
    'activity.emptyCopy': "OpenVZ Leads hasn't taken any actions. Every prospect found, email written, and reply handled will appear here the moment it happens.",
    // ── Review ──
    'review.sendsImmediately': 'sends immediately',
    'review.daysLater': '{days} day(s) later',
    'review.emailN': 'Email {step}',
    'review.emailCount': '{count} email(s)',
    'review.recipientCount': '{count} recipient(s)',
    'review.recipientsWithCount': 'Recipients ({count})',
    // ── Export ──
    'export.failed': 'Export failed',
    // ── Feedback ──
    'feedback.prompt': 'Add your feedback:',
    'feedback.contact': 'Feedback on this contact:',
    'feedback.campaign': 'Leave feedback on this campaign:',
    'feedback.saved': 'Feedback saved. OpenVZ Leads will take it into account.',
    'feedback.failed': 'Could not save feedback.',
    // ── Help ──
    'help.whatTitle': 'What is OpenVZ Leads?',
    'help.whatBody1': 'OpenVZ Leads is an autonomous AI sales agent. Once set up, OpenVZ Leads runs on its own: finds people who match your ideal customer, writes personalized cold emails, sends them through your email platform, reads every reply, handles objections, and works toward booking a meeting. You review everything through this dashboard.',
    'help.whatBody2': 'OpenVZ Leads runs on your Claude Max subscription, so there are no extra AI costs. Everything stays on your machine in one folder.',
    'help.startTitle': 'Getting Started',
    'help.startBody': 'There are three things to do:',
    'help.startSteps': '1. Go to the Settings tab and enter your Instantly API key\n2. Train OpenVZ Leads on your product (through Claude or the command line)\n3. Go to the Controls tab and click Start',
    'help.startNote': "The Setup tab shows you exactly what's done and what still needs to happen.",
    'help.whereTitle': 'Where Everything Lives',
    'help.whereBody': 'Everything OpenVZ Leads needs is inside this one project folder. Nothing is stored elsewhere.',
    'help.file.env': 'Your API keys and credentials (never shared or committed)',
    'help.file.yaml': 'Your product info, target customers, and behavior settings',
    'help.file.skills': 'Sales knowledge files. Edit these to change how OpenVZ Leads writes and sells.',
    'help.file.product': 'Everything OpenVZ Leads knows about your product (auto-generated from training)',
    'help.file.prompts': 'Prompt templates for each agent. Advanced customization.',
    'help.file.db': 'Database with all prospects, campaigns, and conversations',
    'help.file.log': 'Log file showing what OpenVZ Leads is doing',
    'help.keysTitle': 'Getting Your API Keys',
    'help.instantlySummary': 'Instantly API Key (required)',
    'help.instantlyBody': '<p>Instantly is the email platform OpenVZ Leads uses to send campaigns.</p><p>1. Sign up at <a href="https://instantly.ai" target="_blank" rel="noopener">instantly.ai</a> (you need the Growth plan for API access)</p><p>2. Go to Settings &gt; Integrations</p><p>3. Copy your API key</p><p>4. Paste it in the Settings tab here</p>',
    'help.linkedinSummary': 'LinkedIn Credentials (optional)',
    'help.linkedinBody': '<p>If you want OpenVZ Leads to find prospects on LinkedIn, enter your LinkedIn email and password. OpenVZ Leads uses a real browser to search LinkedIn like a human would, with random delays and rate limits to avoid detection.</p><p>If you skip this, OpenVZ Leads will find prospects through Google searches and company website scraping instead.</p>',
    'help.cloudflareSummary': 'Cloudflare Browser Rendering (optional)',
    'help.cloudflareBody': "<p>This is only used during product training (when OpenVZ Leads crawls your website to learn about your product). It handles JavaScript-heavy websites that a basic crawler can't read.</p><p>1. Sign up at <a href=\"https://dash.cloudflare.com\" target=\"_blank\" rel=\"noopener\">Cloudflare</a> (paid Workers plan, ~$5/month)</p><p>2. Go to Workers &amp; Pages &gt; Browser Rendering</p><p>3. Create an API token with Browser Rendering Edit permissions</p><p>4. Enter your Account ID and API Token in the Settings tab</p><p>Without this, OpenVZ Leads uses a built-in crawler that works fine for most websites but can't render JavaScript.</p>",
    'help.issuesTitle': 'Common Issues',
    'help.issue.notFound': '"command not found: openvz-leads"',
    'help.issue.notFoundBody': 'You need to activate the virtual environment first: <code>source .venv/bin/activate</code>',
    'help.issue.401': 'Instantly API returns 401',
    'help.issue.401Body': "Your API key is wrong, or you need the Growth plan (the free plan doesn't include API access). Double-check the key in Settings &gt; Integrations in your Instantly dashboard.",
    'help.issue.claude': 'Claude headless mode fails',
    'help.issue.claudeBody': "Make sure you've run <code>claude login</code> in your terminal and have an active Claude Max subscription. OpenVZ Leads uses your existing subscription, not a separate API key.",
    'help.issue.noProspects': "OpenVZ Leads isn't finding prospects",
    'help.issue.noProspectsBody': 'Check that your ICP (ideal customer profile) in openvz-leads.yaml has realistic titles, industries, and geography. If LinkedIn is set up, check that the credentials are correct. Check the Activity tab to see what OpenVZ Leads has been trying to do.',
  },
};

let LANG = 'en';
try { LANG = localStorage.getItem('ovzLeadsLang') || 'en'; } catch { LANG = 'en'; }

function t(key) {
  const table = I18N[LANG] || I18N.en;
  return table[key] !== undefined ? table[key] : (I18N.en[key] !== undefined ? I18N.en[key] : key);
}

// Interpolating variant: tf('setup.percent', {pct: 40}). Placeholders can be
// reordered in a translation, which Chinese needs — '{done}/{total}' does not
// sit where 'X of Y' does.
function tf(key, params) {
  return String(t(key)).replace(/\{(\w+)\}/g, (whole, name) =>
    Object.prototype.hasOwnProperty.call(params || {}, name) ? String(params[name]) : whole
  );
}

function applyLang() {
  document.documentElement.lang = LANG === 'zh' ? 'zh-CN' : 'en';
  document.querySelectorAll('[data-i18n]').forEach(el => {
    el.textContent = t(el.getAttribute('data-i18n'));
  });
  // Markup-bearing copy — a link, a <code>, the .env chip. textContent would
  // print the tags, so these entries carry their own HTML. Every one of them
  // is authored in this file; none comes from the database or the network.
  document.querySelectorAll('[data-i18n-html]').forEach(el => {
    el.innerHTML = t(el.getAttribute('data-i18n-html'));
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
    el.setAttribute('placeholder', t(el.getAttribute('data-i18n-placeholder')));
  });
  const btn = document.getElementById('lang-btn');
  if (btn) btn.textContent = LANG === 'zh' ? 'EN' : '中文';
}

function toggleLang() {
  LANG = LANG === 'zh' ? 'en' : 'zh';
  try { localStorage.setItem('ovzLeadsLang', LANG); } catch {}
  applyLang();
  loadCurrentTab();
}

// ── Utilities ──

function escHtml(s) {
  if (s === null || s === undefined || s === '') return '';
  return String(s).replace(/[&<>"']/g, ch => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]
  ));
}

function badge(status) {
  const safe = String(status || 'unknown');
  const cls = 'badge-' + safe.toLowerCase().replace(/[^a-z0-9]+/g, '_');
  return '<span class="badge ' + escHtml(cls) + '">' + escHtml(safe) + '</span>';
}

function formatDate(d) {
  if (!d) return '';
  try {
    const dt = new Date(d);
    if (isNaN(dt)) return escHtml(d);
    const locale = LANG === 'zh' ? 'zh-CN' : 'en-US';
    return dt.toLocaleString(locale, {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
  } catch { return escHtml(d); }
}

function emptyState(glyph, title, copy) {
  return '<div class="empty"><div class="glyph">' + glyph + '</div>' +
    '<div class="title">' + title + '</div>' +
    '<div class="copy">' + copy + '</div></div>';
}

function offlineState() {
  return emptyState('&#9888;', t('offline.title'), t('offline.copy'));
}

async function api(path, opts) {
  try {
    const r = await fetch(path, opts);
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

// Like api(), but keeps the server's error body — routes that refuse an
// action (409 on an already-decided campaign, 400 on a bad export) explain
// why, and swallowing that would leave the user guessing.
async function apiWithError(path, opts) {
  try {
    const r = await fetch(path, opts);
    let body = null;
    try { body = await r.json(); } catch {}
    return {ok: r.ok, data: body};
  } catch {
    return {ok: false, data: null};
  }
}

function showToast(msg, type) {
  const t = document.createElement('div');
  t.className = 'toast ' + type;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2600);
}

function toggleVisibility(inputId) {
  const el = document.getElementById(inputId);
  el.type = el.type === 'password' ? 'text' : 'password';
}

// ── Tabs ──

function showTab(id, btn) {
  currentTab = id;
  if (id === 'companies') companyDrill = false;
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  if (btn) btn.classList.add('active');
  loadCurrentTab();
}

function loadCurrentTab() {
  switch (currentTab) {
    case 'setup': loadSetupStatus(); break;
    case 'overview': loadStats(); break;
    case 'companies': if (!companyDrill) loadCompanies(); break;
    case 'prospects': loadProspects(); break;
    case 'briefs': loadBriefs(); break;
    case 'review': loadReview(); break;
    case 'export': renderExport(); break;
    case 'campaigns': loadCampaigns(); break;
    case 'conversations': loadConversations(); break;
    case 'activity': loadActivity(); break;
    case 'settings': loadSettings(); break;
    case 'controls': loadAgentStatus(); loadLogs(); break;
  }
}

// ── Account briefs ──

function briefList(heading, items, warn) {
  const clean = (items || []).filter(Boolean);
  if (!clean.length) return '';
  return '<div class="brief-block' + (warn ? ' warn' : '') + '">' +
    '<h4>' + escHtml(heading) + '</h4><ul>' +
    clean.map(i => '<li>' + escHtml(i) + '</li>').join('') +
    '</ul></div>';
}

function briefSignals(signals) {
  const clean = (signals || []).filter(s => s && s.signal);
  if (!clean.length) return '';
  return '<div class="brief-block"><h4>' + escHtml(t('brief.signals')) + '</h4><ul>' +
    clean.map(s => {
      const strength = String(s.strength || 'low').toLowerCase();
      let li = '<li>' + escHtml(s.signal) +
        '<span class="sig ' + escHtml(strength) + '">' + escHtml(strength) + '</span>';
      if (s.evidence) li += '<br><span class="evidence">' + escHtml(s.evidence) + '</span>';
      return li + '</li>';
    }).join('') + '</ul></div>';
}

function briefAngles(angles) {
  const clean = (angles || []).filter(a => a && a.angle);
  if (!clean.length) return '';
  return '<div class="brief-block"><h4>' + escHtml(t('brief.angles')) + '</h4><ul>' +
    clean.map(a => {
      let li = '<li>' + escHtml(a.angle);
      if (a.why_it_lands) li += '<br><span class="evidence">' + escHtml(a.why_it_lands) + '</span>';
      return li + '</li>';
    }).join('') + '</ul></div>';
}

function briefChain(chain) {
  if (!chain) return '';
  const rows = [
    [t('brief.role'), chain.this_contact_role],
    [t('brief.buyer'), chain.likely_economic_buyer],
    [t('brief.champion'), chain.likely_champion],
    [t('brief.blocker'), chain.likely_blocker],
  ].filter(r => r[1]);
  if (!rows.length) return '';
  return '<div class="brief-block"><h4>' + escHtml(t('brief.chain')) + '</h4><ul>' +
    rows.map(r => '<li>' + escHtml(r[0]) + ': ' + escHtml(r[1]) + '</li>').join('') +
    '</ul></div>';
}

async function loadBriefs() {
  const el = document.getElementById('briefs-list');
  const rows = await api('/api/profiles');
  if (rows === null) { el.innerHTML = offlineState(); return; }
  if (!rows.length) {
    el.innerHTML = emptyState('&#128269;', t('briefs.empty'), t('briefs.emptyCopy'));
    return;
  }

  el.innerHTML = rows.map(r => {
    const p = r.profile || {};
    const score = Number(p.fit_score || 0);
    const scoreCls = score >= 7 ? '' : (score >= 4 ? ' mid' : ' low');
    const snap = p.company_snapshot || {};
    const name = [r.first_name, r.last_name].filter(Boolean).join(' ');

    let body = '';
    if (snap.what_they_do) {
      body += '<div class="brief-block"><h4>' + escHtml(t('brief.whatTheyDo')) + '</h4><p>' +
        escHtml(snap.what_they_do) + '</p></div>';
    }
    body += briefList(t('brief.why'), p.fit_reasons);
    body += briefList(t('brief.pains'), p.pain_hypotheses);
    body += briefSignals(p.buying_signals);
    body += briefChain(p.decision_chain);
    body += briefAngles(p.opening_angles);
    body += briefList(t('brief.risks'), p.risks, true);
    body += briefList(t('brief.avoid'), p.avoid, true);
    body += briefList(t('brief.gaps'), p.evidence_gaps);

    return '<div class="brief-card">' +
      '<div class="head"><div>' +
        '<div class="title">' + escHtml(r.company || '—') + '</div>' +
        '<div class="meta">' + escHtml(name) + ' &middot; ' + escHtml(r.title || '') +
          (r.email ? ' &middot; ' + escHtml(r.email) : '') + '</div>' +
      '</div><div style="text-align:right">' +
        '<div class="fit-score' + scoreCls + '">' + score + '/10</div>' +
        '<div class="meta">' + escHtml(t('brief.confidence')) + ': ' +
          escHtml(p.confidence || 'low') + '</div>' +
      '</div></div>' +
      '<div class="body"><div class="brief-grid">' + body + '</div></div>' +
    '</div>';
  }).join('');
}

// ── Review queue ──

function updateReviewCount(n) {
  const el = document.getElementById('review-count');
  if (!el) return;
  el.textContent = n > 0 ? String(n) : '';
  el.classList.toggle('on', n > 0);
}

async function loadReview() {
  const el = document.getElementById('review-list');
  const rows = await api('/api/review/pending');
  if (rows === null) { el.innerHTML = offlineState(); return; }
  updateReviewCount(rows.length);
  if (!rows.length) {
    el.innerHTML = emptyState('&#10003;', t('review.empty'), t('review.emptyCopy'));
    return;
  }

  el.innerHTML = rows.map(c => {
    const steps = (c.sequence || []).map(s => {
      const delay = Number(s.delay_days || 0);
      const when = delay === 0
        ? t('review.sendsImmediately')
        : tf('review.daysLater', {days: delay});
      return '<div class="email-step">' +
        '<div class="when">' + escHtml(tf('review.emailN', {step: s.step || '?'})) +
          ' &middot; ' + escHtml(when) + '</div>' +
        '<div class="subject">' + escHtml(s.subject || '') + '</div>' +
        '<div class="body-text">' + escHtml(s.body || '') + '</div>' +
      '</div>';
    }).join('');

    const recipients = (c.recipients || []).map(r =>
      '<li>' + escHtml([r.first_name, r.last_name].filter(Boolean).join(' ')) +
      ' — ' + escHtml(r.title || '') + ', ' + escHtml(r.company || '') +
      (r.email ? ' &middot; ' + escHtml(r.email) : '') + '</li>'
    ).join('');

    const recipientBlock = recipients
      ? '<div class="brief-block" style="margin-top:18px"><h4>' +
        escHtml(tf('review.recipientsWithCount', {count: (c.prospect_ids || []).length})) + '</h4>' +
        '<ul>' + recipients + '</ul></div>'
      : '';

    const id = escHtml(c.id);
    return '<div class="review-card" id="rc-' + id + '">' +
      '<div class="head"><div>' +
        '<div class="title">' + escHtml(c.name || '—') + '</div>' +
        '<div class="meta">' +
          escHtml(tf('review.emailCount', {count: (c.sequence || []).length})) + ' &middot; ' +
          escHtml(tf('review.recipientCount', {count: (c.prospect_ids || []).length})) + '</div>' +
      '</div>' + badge(c.status) + '</div>' +
      '<div class="body">' + steps + recipientBlock + '</div>' +
      '<div class="review-actions">' +
        '<input id="note-' + id + '" placeholder="' + escHtml(t('review.notePlaceholder')) + '">' +
        '<button class="btn btn-primary btn-sm" onclick="decideReview(\'' + id + '\', true)">' +
          escHtml(t('review.approve')) + '</button>' +
        '<button class="btn btn-danger btn-sm" onclick="decideReview(\'' + id + '\', false)">' +
          escHtml(t('review.reject')) + '</button>' +
      '</div>' +
    '</div>';
  }).join('');
}

async function decideReview(id, approved) {
  const input = document.getElementById('note-' + id);
  const note = input ? input.value : '';
  const res = await apiWithError('/api/review/' + encodeURIComponent(id), {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({approved: approved, note: note}),
  });
  if (!res.ok) {
    showToast((res.data && res.data.error) || t('review.gone'), 'error');
  } else {
    showToast(approved ? t('review.approved') : t('review.rejected'), 'success');
  }
  loadReview();
}

// ── Export ──

const EXPORT_TILES = [
  {dataset: 'leads', formats: ['csv', 'markdown', 'json']},
  {dataset: 'profiles', formats: ['markdown', 'json']},
  {dataset: 'emails', formats: ['markdown', 'csv', 'json']},
];

function renderExport() {
  const el = document.getElementById('export-grid');
  el.innerHTML = EXPORT_TILES.map(tile => {
    const buttons = tile.formats.map(f =>
      '<button class="btn btn-secondary btn-sm" ' +
      'onclick="runExport(\'' + tile.dataset + '\', \'' + f + '\')">' +
      f.toUpperCase() + '</button>'
    ).join('');
    return '<div class="export-tile">' +
      '<h4>' + escHtml(t('export.' + tile.dataset)) + '</h4>' +
      '<p>' + escHtml(t('export.' + tile.dataset + 'Desc')) + '</p>' +
      '<div class="btn-group">' + buttons + '</div>' +
    '</div>';
  }).join('');
}

async function runExport(dataset, format) {
  const out = document.getElementById('export-result');
  out.textContent = t('export.running');
  const res = await apiWithError('/api/export', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({dataset: dataset, format: format}),
  });
  if (!res.ok) {
    out.textContent = '';
    showToast((res.data && res.data.error) || t('export.failed'), 'error');
    return;
  }
  out.textContent = t('export.done') + res.data.path;
  showToast(res.data.name, 'success');
}

// ── Setup ──

async function loadSetupStatus() {
  const data = await api('/api/setup-status');
  if (!data || !data.checks) {
    document.getElementById('setup-progress').innerHTML = '';
    document.getElementById('setup-checklist').innerHTML = offlineState();
    return;
  }
  const pct = data.percent || 0;
  const color = pct === 100 ? 'green' : 'yellow';

  document.getElementById('setup-progress').innerHTML =
    '<div class="progress-label">' +
      '<span class="pct">' + escHtml(tf('setup.percent', {pct: pct})) + '</span>' +
      '<span class="text">' +
        escHtml(tf('setup.steps', {done: data.completed, total: data.total_required})) + '</span>' +
    '</div>' +
    '<div class="progress-bar"><div class="progress-fill ' + color + '" style="width:' + pct + '%"></div></div>';

  // The checklist comes from /api/setup-status in English. The check ids are
  // stable, so the label and help are looked up here and the server's text is
  // the fallback — a check added server-side still renders, untranslated,
  // rather than showing a bare key.
  const checkText = (c, field) => {
    const key = 'setup.check.' + c.id + '.' + field;
    const translated = t(key);
    return translated === key ? (c[field] || '') : translated;
  };

  const renderCheck = (c, optional) => {
    const icon = c.done
      ? '<span class="check-icon done">&#10003;</span>'
      : '<span class="check-icon pending">&#9679;</span>';
    return '<div class="check-item">' + icon +
      '<div class="check-info">' +
        '<div class="check-label ' + (c.done ? 'done' : '') + '">' + escHtml(checkText(c, 'label')) +
          (optional ? ' <span class="optional-tag">' + escHtml(t('setup.optionalTag')) + '</span>' : '') + '</div>' +
        (!c.done ? '<div class="check-help">' + escHtml(checkText(c, 'help')) + '</div>' : '') +
      '</div></div>';
  };

  let html = data.checks.filter(c => c.required).map(c => renderCheck(c, false)).join('');
  const optional = data.checks.filter(c => !c.required);
  if (optional.length) {
    html += '<div class="subhead">' + escHtml(t('setup.optionalHead')) + '</div>';
    html += optional.map(c => renderCheck(c, true)).join('');
  }
  document.getElementById('setup-checklist').innerHTML = html;
}

// ── Settings ──

async function loadSettings() {
  const data = await api('/api/settings');
  if (!data) return;
  document.getElementById('instantly-key').value = data.instantly_api_key || '';
  document.getElementById('linkedin-email').value = data.linkedin_email || '';
  document.getElementById('linkedin-password').value = '';
  document.getElementById('cf-account-id').value = data.cloudflare_account_id || '';
  document.getElementById('cf-api-token').value = '';
  if (data.linkedin_password_set) {
    document.getElementById('linkedin-password').placeholder = t('settings.passwordSaved');
  }
}

async function saveEnv(payload, okMsg) {
  const data = await api('/api/settings/env', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  if (data && data.success) showToast(okMsg, 'success');
  else showToast((data && data.message) || t('settings.saveFailed'), 'error');
}

function saveInstantly() {
  saveEnv({INSTANTLY_API_KEY: document.getElementById('instantly-key').value.trim()}, t('settings.savedInstantly'));
}

function saveLinkedIn() {
  const payload = {LINKEDIN_EMAIL: document.getElementById('linkedin-email').value.trim()};
  const pass = document.getElementById('linkedin-password').value;
  if (pass) payload.LINKEDIN_PASSWORD = pass;
  saveEnv(payload, t('settings.savedLinkedIn'));
}

function saveCloudflare() {
  saveEnv({
    CLOUDFLARE_ACCOUNT_ID: document.getElementById('cf-account-id').value.trim(),
    CLOUDFLARE_API_TOKEN: document.getElementById('cf-api-token').value.trim()
  }, t('settings.savedCloudflare'));
}

async function testInstantly() {
  const key = document.getElementById('instantly-key').value.trim();
  const el = document.getElementById('instantly-test-result');
  el.innerHTML = '<div class="test-result pending">' + escHtml(t('settings.testing')) + '</div>';
  const data = await api('/api/settings/test-instantly', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({api_key: key})
  });
  if (!data) {
    el.innerHTML = '<div class="test-result error">' + escHtml(t('settings.testUnreachable')) + '</div>';
    return;
  }
  el.innerHTML = '<div class="test-result ' + (data.success ? 'success' : 'error') + '">' + escHtml(data.message) + '</div>';
}

// ── Controls ──

async function loadAgentStatus() {
  const data = await api('/api/agent/status');
  const headerDot = document.getElementById('header-dot');
  const headerText = document.getElementById('header-status-text');

  if (!data) {
    headerDot.className = 'status-dot offline';
    headerText.textContent = t('status.offline');
    return;
  }
  const running = !!data.running;

  headerDot.className = 'status-dot ' + (running ? 'running' : 'stopped');
  headerText.textContent = running ? t('status.running') : t('status.stopped');
  document.getElementById('control-dot').className = 'dot ' + (running ? 'running' : 'stopped');
  const label = document.getElementById('control-label');
  label.className = 'label ' + (running ? 'running' : 'stopped');
  label.textContent = running ? t('controls.running') : t('controls.stopped');

  const meta = document.getElementById('control-meta');
  if (running && data.pid) {
    let info = 'PID ' + escHtml(String(data.pid));
    if (data.started_at) info += ' &middot; ' + escHtml(t('controls.startedAt')) + ' ' + formatDate(data.started_at);
    meta.innerHTML = info;
  } else {
    meta.innerHTML = escHtml(t('controls.idleNote'));
  }

  document.getElementById('btn-start').style.display = running ? 'none' : '';
  document.getElementById('btn-stop').style.display = running ? '' : 'none';
}

async function startAgent() {
  const btn = document.getElementById('btn-start');
  btn.disabled = true;
  const data = await api('/api/agent/start', {method: 'POST'});
  if (data && data.success) showToast(t('controls.started'), 'success');
  else showToast((data && data.message) || t('controls.startFailed'), 'error');
  btn.disabled = false;
  loadAgentStatus();
}

async function stopAgent() {
  const btn = document.getElementById('btn-stop');
  btn.disabled = true;
  const data = await api('/api/agent/stop', {method: 'POST'});
  if (data && data.success) showToast(t('controls.wasStopped'), 'success');
  else showToast((data && data.message) || t('controls.stopFailed'), 'error');
  btn.disabled = false;
  loadAgentStatus();
}

async function loadLogs() {
  const data = await api('/api/agent/logs');
  const el = document.getElementById('log-viewer');
  if (data && data.lines && data.lines.length) {
    const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
    el.textContent = data.lines.join('\n');
    if (stick) el.scrollTop = el.scrollHeight;
  } else {
    el.textContent = t('controls.noLogs');
  }
}

// ── Pipeline data ──

async function loadStats() {
  const grid = document.getElementById('stats-grid');
  const data = await api('/api/stats');
  if (!data) { grid.innerHTML = offlineState(); return; }
  if (data.error) {
    grid.innerHTML = emptyState('&#9670;', t('stats.empty'), t('stats.emptyCopy'));
    return;
  }
  const p = data.prospects || {}, c = data.campaigns || {}, v = data.conversations || {};
  const chips = (map) => {
    const entries = Object.entries(map || {});
    if (!entries.length) return '<span class="chip muted">' + escHtml(t('stats.noneYet')) + '</span>';
    return entries.map(([k, n]) =>
      '<span class="chip">' + escHtml(k) + ' <b>' + escHtml(String(n)) + '</b></span>'
    ).join('');
  };
  const card = (label, value, breakdown) =>
    '<div class="stat-card"><div class="label">' + label + '</div>' +
    '<div class="value">' + value + '</div>' +
    '<div class="breakdown">' + breakdown + '</div></div>';

  grid.innerHTML =
    card(escHtml(t('stats.prospects')), p.total || 0, chips(p.by_status)) +
    card(escHtml(t('stats.campaigns')), c.total || 0, chips(c.by_status)) +
    card(escHtml(t('stats.conversations')), v.total || 0, chips(v.by_status)) +
    card(escHtml(t('stats.actions')), data.actions_total || 0,
      '<span class="chip">' + escHtml(t('stats.claudeToday')) + ' <b>' +
      escHtml(String(data.claude_calls_today || 0)) + '</b></span>');
}

async function loadCompanies() {
  companyDrill = false;
  const el = document.getElementById('companies-list');
  const data = await api('/api/companies');
  if (!data) { el.innerHTML = offlineState(); return; }
  _companies = data;
  if (!data.length) {
    el.innerHTML = emptyState('&#9906;', t('companies.empty'), t('companies.emptyCopy'));
    return;
  }
  const companyCols = ['name', 'domain', 'industry', 'size', 'location', 'contacts', 'source', 'added'];
  let html = '<div class="table-card"><table><thead><tr>' +
    companyCols.map(c => '<th>' + escHtml(t('companies.col.' + c)) + '</th>').join('') +
    '</tr></thead><tbody>';
  data.forEach((c, i) => {
    const website = c.website || (c.domain ? 'https://' + c.domain : '');
    const nameLink = website
      ? '<a href="' + escHtml(website) + '" target="_blank" rel="noopener" onclick="event.stopPropagation()">' + escHtml(c.name) + '</a>'
      : escHtml(c.name);
    html += '<tr style="cursor:pointer" onclick="showCompanyContacts(' + i + ')">' +
      '<td>' + nameLink + '</td><td class="muted">' + escHtml(c.domain) + '</td><td>' + escHtml(c.industry) + '</td>' +
      '<td>' + escHtml(c.company_size) + '</td><td>' + escHtml(c.location) + '</td>' +
      '<td>' + (c.contact_count || 0) + '</td><td class="muted">' + escHtml(c.source) + '</td>' +
      '<td class="muted">' + formatDate(c.created_at) + '</td></tr>';
  });
  el.innerHTML = html + '</tbody></table></div>';
}

async function showCompanyContacts(index) {
  const company = _companies[index];
  if (!company) return;
  companyDrill = true;
  const el = document.getElementById('companies-list');
  const data = await api('/api/companies/' + encodeURIComponent(company.id) + '/contacts');
  let html = '<div class="card"><h2>' +
    escHtml(tf('companies.contactsOf', {company: company.name})) + '</h2>' +
    '<button class="btn btn-secondary btn-sm" onclick="loadCompanies()" style="margin-bottom:16px">' +
    t('companies.back') + '</button>';
  if (!data || !data.length) {
    html += '<p style="color:var(--text-3);font-size:13px">' + escHtml(t('companies.noContacts')) + '</p></div>';
  } else {
    const drillCols = ['name', 'title', 'email', 'phone', 'linkedin', 'status', 'source'];
    html += '<div class="table-card"><table><thead><tr>' +
      drillCols.map(c => '<th>' + escHtml(t('contacts.col.' + c)) + '</th>').join('') +
      '</tr></thead><tbody>';
    for (const p of data) {
      const emailIcon = p.email_verified ? ' <span class="verified">&#10003;</span>' : '';
      const phoneIcon = p.phone_verified ? ' <span class="verified">&#10003;</span>' : '';
      html += '<tr><td>' + escHtml(p.first_name) + ' ' + escHtml(p.last_name) + '</td>' +
        '<td>' + escHtml(p.title) + '</td><td>' + escHtml(p.email) + emailIcon + '</td>' +
        '<td>' + escHtml(p.phone) + phoneIcon + '</td>' +
        '<td>' + (p.linkedin_url ? '<a href="' + escHtml(p.linkedin_url) + '" target="_blank" rel="noopener">' + escHtml(t('contacts.profile')) + '</a>' : '') + '</td>' +
        '<td>' + badge(p.status) + '</td><td class="muted">' + escHtml(p.source) + '</td></tr>';
    }
    html += '</tbody></table></div></div>';
  }
  el.innerHTML = html;
}

async function submitFeedback(entityType, entityId, promptText) {
  const comment = prompt(promptText || t('feedback.prompt'));
  if (!comment) return;
  const data = await api('/api/feedback', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({entity_type: entityType, entity_id: entityId, comment: comment})
  });
  if (data && data.success) showToast(t('feedback.saved'), 'success');
  else showToast((data && data.message) || t('feedback.failed'), 'error');
}

function fbProspect(i) {
  const p = _prospects[i];
  if (p) submitFeedback('contact', p.id, t('feedback.contact'));
}

function fbCampaign(i) {
  const c = _campaigns[i];
  if (c) submitFeedback('campaign', c.id, t('feedback.campaign'));
}

async function loadProspects() {
  const el = document.getElementById('prospects-table');
  const data = await api('/api/prospects');
  if (!data) { el.innerHTML = offlineState(); return; }
  _prospects = data;
  if (!data.length) {
    el.innerHTML = emptyState('&#9673;', t('contacts.empty'), t('contacts.emptyCopy'));
    return;
  }
  const contactCols = ['name', 'title', 'company', 'email', 'phone', 'status', 'source', 'added'];
  let html = '<div class="table-card"><table><thead><tr>' +
    contactCols.map(c => '<th>' + escHtml(t('contacts.col.' + c)) + '</th>').join('') +
    '<th></th></tr></thead><tbody>';
  data.forEach((p, i) => {
    const emailV = p.email ? (escHtml(p.email) + (p.email_verified ? ' <span class="verified">&#10003;</span>' : '')) : '';
    const phoneV = p.phone ? (escHtml(p.phone) + (p.phone_verified ? ' <span class="verified">&#10003;</span>' : '')) : '';
    html += '<tr><td>' + escHtml(p.first_name) + ' ' + escHtml(p.last_name) + '</td>' +
      '<td>' + escHtml(p.title) + '</td><td>' + escHtml(p.company) + '</td>' +
      '<td>' + emailV + '</td><td>' + phoneV + '</td><td>' + badge(p.status) + '</td>' +
      '<td class="muted">' + escHtml(p.source) + '</td><td class="muted">' + formatDate(p.created_at) + '</td>' +
      '<td><button class="btn btn-secondary btn-sm" onclick="fbProspect(' + i + ')">' +
      escHtml(t('contacts.feedback')) + '</button></td></tr>';
  });
  el.innerHTML = html + '</tbody></table></div>';
}

async function loadCampaigns() {
  const el = document.getElementById('campaigns-list');
  const data = await api('/api/campaigns');
  if (!data) { el.innerHTML = offlineState(); return; }
  _campaigns = data;
  if (!data.length) {
    el.innerHTML = emptyState('&#9993;', t('campaigns.empty'), t('campaigns.emptyCopy'));
    return;
  }
  let html = '';
  data.forEach((c, i) => {
    let stepsHtml = '';
    for (const step of (c.sequence || [])) {
      stepsHtml += '<div class="email-step"><div class="step-num">' +
        escHtml(tf('campaigns.emailN', {step: step.step || '?'})) +
        (step.delay_days
          ? ' &middot; ' + escHtml(tf('campaigns.sendAfter', {days: step.delay_days}))
          : '') + '</div>' +
        '<div class="subject">' + escHtml(step.subject) + '</div>' +
        '<div class="body">' + escHtml(step.body) + '</div></div>';
    }
    const pc = (c.prospect_ids || []).length;
    html += '<div class="campaign-card"><h3>' + escHtml(c.name || t('campaigns.untitled')) + '</h3>' +
      '<div class="meta">' + badge(c.status) + '<span>' + escHtml(c.channel || 'email') + '</span>' +
      '<span>' + escHtml(tf('campaigns.prospects', {count: pc})) + '</span>' +
      '<span>' + formatDate(c.created_at) + '</span>' +
      '<button class="btn btn-secondary btn-sm" onclick="fbCampaign(' + i + ')">' +
      escHtml(t('contacts.feedback')) + '</button></div>' +
      (stepsHtml || '<p style="color:var(--text-3);font-size:13px">' + escHtml(t('campaigns.noSteps')) + '</p>') + '</div>';
  });
  el.innerHTML = html;
}

async function loadConversations() {
  const el = document.getElementById('conversations-list');
  const data = await api('/api/conversations');
  if (!data) { el.innerHTML = offlineState(); return; }
  if (!data.length) {
    el.innerHTML = emptyState('&#9737;', t('conversations.empty'), t('conversations.emptyCopy'));
    return;
  }
  let html = '';
  for (const c of data) {
    let threadHtml = '';
    for (const msg of (c.thread || [])) {
      const cls = msg.sender === 'openvz_leads' ? 'sent' : 'received';
      threadHtml += '<div class="thread-msg ' + cls + '"><div class="sender">' + escHtml(msg.sender) +
        ' &middot; ' + formatDate(msg.timestamp) + '</div>' + escHtml(msg.content) + '</div>';
    }
    const name = [c.first_name, c.last_name].filter(Boolean).join(' ') || t('conversations.unknown');
    html += '<div class="convo-card"><h3>' + escHtml(name) +
      (c.company ? ' <span style="color:var(--text-3);font-weight:500">&mdash; ' + escHtml(c.company) + '</span>' : '') + '</h3>' +
      '<div class="meta">' + badge(c.status) + (c.intent ? badge(c.intent) : '') +
      '<span>' + escHtml(c.prospect_email || '') + '</span><span>' + formatDate(c.updated_at) + '</span></div>' +
      (threadHtml || '<p style="color:var(--text-3);font-size:13px">' + escHtml(t('conversations.noMessages')) + '</p>') + '</div>';
  }
  el.innerHTML = html;
}

async function loadActivity() {
  const el = document.getElementById('activity-list');
  const data = await api('/api/activity');
  if (!data) { el.innerHTML = offlineState(); return; }
  if (!data.length) {
    el.innerHTML = emptyState('&#9202;', t('activity.empty'), t('activity.emptyCopy'));
    return;
  }
  let html = '<div class="activity-feed">';
  for (const a of data) {
    html += '<div class="activity-item"><span class="time">' + formatDate(a.created_at) + '</span>' +
      '<span class="agent">' + escHtml(a.agent) + '</span>' +
      '<span class="action">' + escHtml(a.action_type) + '</span></div>';
  }
  el.innerHTML = html + '</div>';
}

// ── Init & live refresh ──

// The review badge is visible from every tab — a draft waiting on you is the
// one thing you should never have to go looking for.
async function refreshReviewBadge() {
  const stats = await api('/api/stats');
  if (!stats || !stats.campaigns) return;
  const byStatus = stats.campaigns.by_status || {};
  updateReviewCount((byStatus.pending_review || 0) + (byStatus.draft || 0));
}

applyLang();
loadSetupStatus();
loadAgentStatus();
refreshReviewBadge();

// Agent status: quick poll
setInterval(loadAgentStatus, 8000);
setInterval(() => { if (!document.hidden) refreshReviewBadge(); }, 20000);

// Data tabs: auto-refresh live stats without clobbering form input
setInterval(() => {
  if (document.hidden) return;
  switch (currentTab) {
    case 'setup': loadSetupStatus(); break;
    case 'overview': loadStats(); break;
    case 'companies': if (!companyDrill) loadCompanies(); break;
    case 'prospects': loadProspects(); break;
    case 'briefs': loadBriefs(); break;
    case 'campaigns': loadCampaigns(); break;
    case 'conversations': loadConversations(); break;
    case 'activity': loadActivity(); break;
    case 'controls': loadLogs(); break;
    // review: never auto-refreshed (a note may be half-typed)
    // settings & help: never auto-refreshed (user may be typing)
  }
}, 15000);
</script>
</body>
</html>
"""


def start_dashboard(host: str = "127.0.0.1", port: int = 5555):
    """Start the dashboard server."""
    import uvicorn

    print(f"\n  OpenVZ Leads Dashboard running at http://{host}:{port}")
    print("  Press Ctrl+C to stop.\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
