"""Telling a CRM what happened.

OpenVZ Leads owns the pipeline up to the point a person replies. Everything
after that — the meeting, the deal, the loss — belongs in whatever system
already holds your customers, and asking someone to retype it there is how
two records drift apart until neither is true.

So every stage change is an event, and every event is offered to a CRM:

    provider: none      record it locally and stop (the default)
    provider: webhook   POST it to webhook_url
    provider: file      append it to data/crm-sync.jsonl for a later import

## The payload

Stable. Additive changes only — a receiver that reads three fields today
keeps working.

```json
{
  "event": "stage_change",
  "event_id": "9f2c1a...",
  "occurred_at": "2026-08-21T09:14:03",
  "source": "openvz-leads",
  "from_stage": "replied",
  "to_stage": "meeting",
  "reason": "Asked for a time on Thursday",
  "actor": "handler",
  "contact": {
    "id": "...", "first_name": "...", "last_name": "...",
    "email": "...", "title": "...", "linkedin_url": "...", "score": 72
  },
  "company": {"name": "...", "domain": "...", "website": "...", "industry": "..."},
  "brief": {"fit_score": 7, "confidence": "medium", "summary": "..."}
}
```

`brief` is present only when the Profiler has analysed the account. It is a
summary, not the whole analysis: a CRM field is not the right home for a
document, and the full brief is one `openvz-leads export profiles` away.

## Failures

A stage change is never lost to a sync failure. The event is written locally
first and marked unsynced; a failed push leaves it that way and the next
cycle retries. Only a 4xx — a receiver saying "this request is wrong", which
will be just as wrong next time — marks the event permanently failed so it
stops blocking the queue behind it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import httpx

from openvz_leads.config import CrmConfig

logger = logging.getLogger("openvz_leads.crm")

# The receiver told us the request itself is wrong. Retrying an unchanged
# request against the same complaint is a loop, not resilience.
_PERMANENT_STATUS = frozenset({400, 401, 403, 404, 405, 410, 413, 422})

SYNC_FILE_NAME = "crm-sync.jsonl"


class CrmSync:
    """Pushes stage changes to whatever the config points at."""

    def __init__(self, config: CrmConfig | None = None, env=None):
        self.config = config or CrmConfig()
        self.env = env

    @property
    def enabled(self) -> bool:
        return self.config.sync_enabled

    def wants(self, stage: str) -> bool:
        """Whether this stage is one the CRM asked to hear about."""
        if not self.enabled:
            return False
        stages = self.config.sync_stages
        # An empty list means "everything" rather than "nothing": someone who
        # deletes the list wants less configuration, not silence.
        return not stages or stage in stages

    def describe(self) -> str:
        if not self.enabled:
            return "off — stage changes are recorded locally only"
        if self.config.provider == "file":
            return f"file — appending to data/{SYNC_FILE_NAME}"
        host = (self.config.webhook_url or "").split("/")[2:3]
        return f"webhook — {host[0] if host else 'not configured'}"

    # ── Sending ──

    async def push(self, payload: dict) -> tuple[bool, str, bool]:
        """Deliver one event.

        Returns (ok, error, permanent). `permanent` separates "try again in a
        minute" from "this will never work", which is the difference between
        a queue that drains and a queue that jams.
        """
        if not self.enabled:
            return True, "", False
        if self.config.provider == "file":
            return self._append_to_file(payload)
        return await self._post(payload)

    def _append_to_file(self, payload: dict) -> tuple[bool, str, bool]:
        from openvz_leads import paths

        try:
            target = paths.data_dir() / SYNC_FILE_NAME
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            return True, "", False
        except OSError as e:
            # A full or read-only disk is worth retrying; a bad path is not,
            # but we cannot tell them apart, so retry and let the log say why.
            return False, f"could not write the sync file: {e}", False

    async def _post(self, payload: dict) -> tuple[bool, str, bool]:
        url = (self.config.webhook_url or "").strip()
        if not url:
            return False, (
                "crm.provider is 'webhook' but crm.webhook_url is empty."
            ), True

        headers = {"Content-Type": "application/json"}
        token = getattr(self.env, "crm_webhook_token", "") if self.env else ""
        if token:
            header_name = self.config.auth_header or "Authorization"
            headers[header_name] = (
                f"Bearer {token}" if header_name.lower() == "authorization" else token
            )

        try:
            async with httpx.AsyncClient(
                timeout=self.config.timeout_seconds
            ) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except Exception as e:
            return False, f"could not reach the CRM: {e}", False

        if 200 <= resp.status_code < 300:
            return True, "", False
        permanent = resp.status_code in _PERMANENT_STATUS
        return (
            False,
            f"CRM returned {resp.status_code}: {resp.text[:200]}",
            permanent,
        )


# ── Payload construction ──────────────────────────────────────────────


def build_payload(event: dict, prospect, company=None, profile: dict | None = None) -> dict:
    """Turn a stage_events row plus its records into the documented shape."""
    payload = {
        "event": "stage_change",
        "event_id": event.get("id", ""),
        "occurred_at": _iso(event.get("created_at")),
        "source": "openvz-leads",
        "from_stage": event.get("from_stage", "") or "",
        "to_stage": event.get("to_stage", "") or "",
        "reason": event.get("reason", "") or "",
        "actor": event.get("actor", "") or "",
        "contact": _contact(prospect),
        "company": _company(prospect, company),
    }
    brief = _brief(profile)
    if brief:
        payload["brief"] = brief
    return payload


def _iso(value) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def _contact(prospect) -> dict:
    if prospect is None:
        return {}
    return {
        "id": getattr(prospect, "id", ""),
        "first_name": getattr(prospect, "first_name", ""),
        "last_name": getattr(prospect, "last_name", ""),
        "email": getattr(prospect, "email", ""),
        "email_verified": bool(getattr(prospect, "email_verified", False)),
        "title": getattr(prospect, "title", ""),
        "seniority": getattr(prospect, "seniority", ""),
        "linkedin_url": getattr(prospect, "linkedin_url", ""),
        "score": getattr(prospect, "score", 0),
        "source": getattr(prospect, "source", ""),
    }


def _company(prospect, company) -> dict:
    if company is not None:
        return {
            "id": getattr(company, "id", ""),
            "name": getattr(company, "name", ""),
            "domain": getattr(company, "domain", ""),
            "website": getattr(company, "website", ""),
            "industry": getattr(company, "industry", ""),
            "company_size": getattr(company, "company_size", ""),
            "location": getattr(company, "location", ""),
        }
    # No company record — the prospect still carries denormalized copies, and
    # a CRM would rather have a name than nothing.
    return {
        "name": getattr(prospect, "company", "") if prospect else "",
        "industry": getattr(prospect, "industry", "") if prospect else "",
        "company_size": getattr(prospect, "company_size", "") if prospect else "",
    }


def _brief(profile: dict | None) -> dict:
    """A summary of the analysis, not the analysis.

    Buying signals are included because they are the part a salesperson wants
    in front of them when the record opens. `avoid` is included for the same
    reason it exists at all: the sentence that kills the deal should travel
    with the record, not stay behind in a file nobody opens.
    """
    if not profile or not isinstance(profile, dict):
        return {}
    signals = profile.get("buying_signals") or []
    return {
        "fit_score": profile.get("fit_score"),
        "confidence": profile.get("confidence", ""),
        "summary": (profile.get("company_snapshot") or "")[:600],
        "buying_signals": [
            s.get("signal", "") if isinstance(s, dict) else str(s)
            for s in signals[:5]
        ],
        "avoid": [str(a) for a in (profile.get("avoid") or [])[:5]],
    }
