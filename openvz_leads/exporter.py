"""Export — get your leads, account briefs and outreach out of the tool.

Without an outbound channel configured, this is how the work leaves OpenVZ
Leads: a CSV for your CRM, a Markdown pack you can read, or JSON for whatever
comes next. Nothing here talks to the network.
"""

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from openvz_leads.models.profile import AccountProfile
from openvz_leads.state import StateManager

logger = logging.getLogger("openvz_leads.exporter")

DATASETS = ("leads", "profiles", "emails")
FORMATS = ("csv", "markdown", "json")

# Which datasets a given format can actually represent well.
_UNSUPPORTED = {
    ("profiles", "csv"): (
        "An account brief is nested (signals, decision chain, angles) and "
        "flattens badly into CSV. Use 'markdown' to read it or 'json' to "
        "process it."
    ),
}


class ExportError(Exception):
    """Raised with an actionable message when an export can't be produced."""


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _default_path(dataset: str, fmt: str, out_dir: Path) -> Path:
    suffix = {"csv": "csv", "markdown": "md", "json": "json"}[fmt]
    return out_dir / f"openvz-leads-{dataset}-{_stamp()}.{suffix}"


class Exporter:
    def __init__(self, state: StateManager):
        self.state = state

    async def export(
        self,
        dataset: str = "leads",
        fmt: str = "csv",
        out_path: str | None = None,
    ) -> Path:
        """Write `dataset` as `fmt` and return the path written."""
        dataset = (dataset or "").strip().lower()
        fmt = (fmt or "").strip().lower()
        if dataset not in DATASETS:
            raise ExportError(
                f"Unknown dataset '{dataset}'. Choose one of: {', '.join(DATASETS)}."
            )
        if fmt not in FORMATS:
            raise ExportError(
                f"Unknown format '{fmt}'. Choose one of: {', '.join(FORMATS)}."
            )
        if (dataset, fmt) in _UNSUPPORTED:
            raise ExportError(_UNSUPPORTED[(dataset, fmt)])

        rows = await self._collect(dataset)
        if not rows:
            raise ExportError(
                f"Nothing to export for '{dataset}' yet. Run 'openvz-leads run' "
                "first, or check 'openvz-leads status'."
            )

        if out_path:
            path = Path(out_path).expanduser()
        else:
            out_dir = Path(self.state.db_path).parent / "exports"
            path = _default_path(dataset, fmt, out_dir)
        path.parent.mkdir(parents=True, exist_ok=True)

        if fmt == "json":
            self._write_json(path, dataset, rows)
        elif fmt == "csv":
            self._write_csv(path, rows)
        else:
            self._write_markdown(path, dataset, rows)

        logger.info(f"Exported {len(rows)} {dataset} record(s) to {path}")
        return path

    # ── Collection ──

    async def _collect(self, dataset: str) -> list[dict]:
        if dataset == "leads":
            return await self._collect_leads()
        if dataset == "profiles":
            return await self._collect_profiles()
        return await self._collect_emails()

    async def _collect_leads(self) -> list[dict]:
        prospects = await self.state.get_all_prospects()
        rows = []
        for p in prospects:
            profile = p.profile()
            rows.append({
                "first_name": p.first_name,
                "last_name": p.last_name,
                "title": p.title,
                "seniority": p.seniority,
                "email": p.email,
                "email_verified": "yes" if p.email_verified else "no",
                "linkedin_url": p.linkedin_url,
                "company": p.company,
                "industry": p.industry,
                "company_size": p.company_size,
                "scout_score": p.score,
                "fit_score": profile.get("fit_score", ""),
                "analysed": "yes" if profile else "no",
                "status": p.status,
                "source": p.source,
                "source_url": p.source_url,
                "notes": p.personalization_notes,
                "found_at": p.created_at.isoformat() if p.created_at else "",
            })
        return rows

    async def _collect_profiles(self) -> list[dict]:
        prospects = await self.state.get_profiled_prospects()
        rows = []
        for p in prospects:
            raw = p.profile()
            if not raw:
                continue
            try:
                profile = AccountProfile(**raw)
            except Exception:
                # Stored by an older/looser version — pass it through raw
                # rather than dropping the record entirely.
                rows.append({
                    "contact": p.full_name(),
                    "title": p.title,
                    "company": p.company,
                    "profile": raw,
                })
                continue
            rows.append({
                "contact": p.full_name(),
                "title": p.title,
                "company": p.company,
                "email": p.email,
                "profiled_at": p.profiled_at.isoformat() if p.profiled_at else "",
                "profile": profile.model_dump(),
            })
        return rows

    async def _collect_emails(self) -> list[dict]:
        campaigns = await self.state.get_all_campaigns()
        rows = []
        for c in campaigns:
            if not c.sequence:
                continue
            recipients = []
            for pid in c.prospect_ids:
                p = await self.state.get_prospect(pid)
                if p:
                    recipients.append({
                        "name": p.full_name(),
                        "title": p.title,
                        "company": p.company,
                        "email": p.email,
                    })
            rows.append({
                "campaign": c.name,
                "campaign_id": c.id,
                "status": c.status,
                "review_note": c.review_note,
                "recipient_count": len(recipients),
                "recipients": recipients,
                "sequence": [s.model_dump() for s in c.sequence],
                "created_at": c.created_at.isoformat() if c.created_at else "",
            })
        return rows

    # ── Writers ──

    @staticmethod
    def _write_json(path: Path, dataset: str, rows: list[dict]) -> None:
        payload = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "dataset": dataset,
            "count": len(rows),
            "records": rows,
        }
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    @staticmethod
    def _write_csv(path: Path, rows: list[dict]) -> None:
        # Flatten one level so nested email sequences still make sense as
        # columns; anything deeper is JSON-encoded in place.
        flat = []
        for row in rows:
            if "sequence" in row:
                base = {k: v for k, v in row.items() if k not in ("sequence", "recipients")}
                for step in row["sequence"]:
                    flat.append({
                        **base,
                        "step": step.get("step", ""),
                        "delay_days": step.get("delay_days", ""),
                        "subject": step.get("subject", ""),
                        "body": step.get("body", ""),
                    })
            else:
                flat.append({
                    k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                    for k, v in row.items()
                })

        fieldnames: list[str] = []
        for row in flat:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(key)

        # utf-8-sig so Excel on Windows opens non-ASCII names correctly.
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(flat)

    def _write_markdown(self, path: Path, dataset: str, rows: list[dict]) -> None:
        title = {
            "leads": "Leads",
            "profiles": "Account briefs",
            "emails": "Outreach drafts",
        }[dataset]
        out = [
            f"# OpenVZ Leads — {title}",
            "",
            f"Exported {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
            f"{len(rows)} record(s)",
            "",
        ]
        if dataset == "leads":
            out += self._markdown_leads(rows)
        elif dataset == "profiles":
            out += self._markdown_profiles(rows)
        else:
            out += self._markdown_emails(rows)
        path.write_text("\n".join(out), encoding="utf-8")

    @staticmethod
    def _markdown_leads(rows: list[dict]) -> list[str]:
        cols = ["first_name", "last_name", "title", "company", "email",
                "scout_score", "fit_score", "status"]
        out = ["| " + " | ".join(c.replace("_", " ").title() for c in cols) + " |",
               "|" + "---|" * len(cols)]
        for r in rows:
            cells = [str(r.get(c, "")).replace("|", "\\|") for c in cols]
            out.append("| " + " | ".join(cells) + " |")
        return out

    @staticmethod
    def _markdown_profiles(rows: list[dict]) -> list[str]:
        out = []
        for r in rows:
            p = r.get("profile") or {}
            out.append(f"## {r.get('company') or '(unknown company)'}")
            out.append("")
            out.append(
                f"**{r.get('contact', '')}** — {r.get('title', '')}"
                + (f" · {r['email']}" if r.get("email") else "")
            )
            out.append("")
            out.append(
                f"Fit **{p.get('fit_score', '?')}/10** · confidence "
                f"**{p.get('confidence', '?')}**"
            )
            out.append("")

            snapshot = p.get("company_snapshot") or {}
            if snapshot.get("what_they_do"):
                out += ["**What they do**", "", snapshot["what_they_do"], ""]

            def bullets(heading: str, items) -> list[str]:
                items = [i for i in (items or []) if i]
                if not items:
                    return []
                return [f"**{heading}**", ""] + [f"- {i}" for i in items] + [""]

            out += bullets("Why they fit", p.get("fit_reasons"))
            out += bullets("Risks", p.get("risks"))
            out += bullets("Likely pains", p.get("pain_hypotheses"))

            signals = p.get("buying_signals") or []
            if signals:
                out += ["**Buying signals**", ""]
                for s in signals:
                    line = f"- {s.get('signal', '')} *({s.get('strength', 'low')})*"
                    if s.get("evidence"):
                        line += f" — {s['evidence']}"
                    out.append(line)
                out.append("")

            chain = p.get("decision_chain") or {}
            if any(chain.values()):
                out += [
                    "**Decision chain**",
                    "",
                    f"- This contact: {chain.get('this_contact_role', 'unknown')}",
                    f"- Likely economic buyer: {chain.get('likely_economic_buyer') or '—'}",
                    f"- Likely champion: {chain.get('likely_champion') or '—'}",
                    f"- Likely blocker: {chain.get('likely_blocker') or '—'}",
                    "",
                ]

            angles = p.get("opening_angles") or []
            if angles:
                out += ["**Opening angles**", ""]
                for a in angles:
                    out.append(f"- {a.get('angle', '')}")
                    if a.get("why_it_lands"):
                        out.append(f"  - Why it lands: {a['why_it_lands']}")
                out.append("")

            out += bullets("Do not say", p.get("avoid"))
            out += bullets("Evidence gaps", p.get("evidence_gaps"))
            out += ["---", ""]
        return out

    @staticmethod
    def _markdown_emails(rows: list[dict]) -> list[str]:
        out = []
        for r in rows:
            out.append(f"## {r.get('campaign', '(unnamed campaign)')}")
            out.append("")
            out.append(
                f"Status: **{r.get('status', '')}** · "
                f"{r.get('recipient_count', 0)} recipient(s)"
            )
            if r.get("review_note"):
                out.append("")
                out.append(f"> Review note: {r['review_note']}")
            out.append("")
            for step in r.get("sequence", []):
                delay = step.get("delay_days", 0)
                when = "sends immediately" if not delay else f"{delay} day(s) later"
                out.append(f"### Email {step.get('step', '?')} — {when}")
                out.append("")
                out.append(f"**Subject:** {step.get('subject', '')}")
                out.append("")
                out.append(step.get("body", ""))
                out.append("")
            recipients = r.get("recipients") or []
            if recipients:
                out += ["**Recipients**", ""]
                for p in recipients:
                    out.append(
                        f"- {p.get('name', '')} — {p.get('title', '')}, "
                        f"{p.get('company', '')} · {p.get('email', '')}"
                    )
                out.append("")
            out += ["---", ""]
        return out
