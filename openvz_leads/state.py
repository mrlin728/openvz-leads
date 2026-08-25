"""SQLite state manager. All of OpenVZ Leads' memory lives here.

Concurrency: the DB runs in WAL mode (set persistently at init) so the
dashboard can read while the agent writes. Every connection gets a busy
timeout so concurrent writers wait instead of raising "database is locked".

Migrations: schema changes are applied via a linear, idempotent migration
list tracked with SQLite's ``PRAGMA user_version`` so existing user DBs
upgrade cleanly in place.
"""

import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

import aiosqlite

from openvz_leads.models.company import Company
from openvz_leads.models.prospect import Prospect
from openvz_leads.models.campaign import Campaign, EmailStep  # noqa: F401 (EmailStep re-exported)
from openvz_leads.models.conversation import Conversation, Message  # noqa: F401

def _default_db_path() -> Path:
    """Where the database lives.

    Defaults to `data/leads.db` beside the install. OPENVZ_LEADS_DB overrides
    it, so the CLI and dashboard can be pointed at a specific workspace (and
    so tests never touch a real pipeline).
    """
    override = os.getenv("OPENVZ_LEADS_DB", "").strip()
    if override:
        return Path(override).expanduser()
    from openvz_leads import paths

    return paths.data_dir() / "leads.db"


DB_PATH = _default_db_path()

# How long (seconds) a connection waits on a locked database before failing.
BUSY_TIMEOUT_SECONDS = 30.0


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _utcnow() -> datetime:
    """Naive UTC now (matches how timestamps are stored in the DB)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def local_day_bounds_utc(now: datetime | None = None) -> tuple[str, str]:
    """The current local day, expressed as the UTC range the DB stores.

    Timestamps are written as naive UTC. "Today" to a person is their local
    day. Comparing the two directly — `DATE(sent_at) = date.today()` — is
    wrong by the UTC offset, and wrong in a way that hides: east of UTC the
    two strings simply never match for the last hours of the local day, so a
    daily *send cap* silently counted zero and stopped capping anything.

    Returned as ISO strings so they compare correctly against the stored
    values, which are ISO strings too.
    """
    # An aware `now` brings its own idea of local with it, which is what
    # makes this testable from any machine. Without that the function would
    # always use the host's timezone, and a test asserting UTC behaviour
    # would pass or fail depending on where it ran — worse than no test.
    if now is None:
        local_now = datetime.now().astimezone()
    elif now.tzinfo is None:
        local_now = now.astimezone()
    else:
        local_now = now

    start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    # A day later in *local* terms, so a DST transition does not gain or lose
    # an hour of sending budget.
    end_local = start_local + timedelta(days=1)
    end_utc = end_local.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc.isoformat(), end_utc.isoformat()


def _norm(value: str | None) -> str:
    """Normalize an identity key (email/domain) for dedup: strip + lowercase."""
    return (value or "").strip().lower()


# ── Schema migrations ─────────────────────────────────────────────────
# Each entry is an idempotent SQL script. The index into this list + 1 is
# the schema version stored in PRAGMA user_version. Never edit or reorder
# released migrations — append new ones.

MIGRATIONS: list[str] = [
    # ── v1: base schema (idempotent, so pre-migration DBs adopt cleanly) ──
    """
    CREATE TABLE IF NOT EXISTS companies (
        id TEXT PRIMARY KEY,
        name TEXT DEFAULT '',
        domain TEXT DEFAULT '',
        website TEXT DEFAULT '',
        description TEXT DEFAULT '',
        industry TEXT DEFAULT '',
        company_size TEXT DEFAULT '',
        location TEXT DEFAULT '',
        source TEXT DEFAULT '',
        source_url TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS prospects (
        id TEXT PRIMARY KEY,
        company_id TEXT DEFAULT '' REFERENCES companies(id),
        first_name TEXT DEFAULT '',
        last_name TEXT DEFAULT '',
        email TEXT DEFAULT '',
        email_verified INTEGER DEFAULT 0,
        phone TEXT DEFAULT '',
        phone_verified INTEGER DEFAULT 0,
        linkedin_url TEXT DEFAULT '',
        title TEXT DEFAULT '',
        seniority TEXT DEFAULT '',
        department TEXT DEFAULT '',
        source TEXT DEFAULT '',
        source_url TEXT DEFAULT '',
        status TEXT DEFAULT 'new',
        score INTEGER DEFAULT 0,
        personalization_notes TEXT DEFAULT '',
        company TEXT DEFAULT '',
        industry TEXT DEFAULT '',
        company_size TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS campaigns (
        id TEXT PRIMARY KEY,
        name TEXT DEFAULT '',
        channel TEXT DEFAULT 'email',
        instantly_campaign_id TEXT DEFAULT '',
        sequence_json TEXT DEFAULT '[]',
        prospect_ids_json TEXT DEFAULT '[]',
        status TEXT DEFAULT 'draft',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        prospect_id TEXT REFERENCES prospects(id),
        campaign_id TEXT DEFAULT '',
        channel TEXT DEFAULT 'email',
        thread_json TEXT DEFAULT '[]',
        intent TEXT DEFAULT '',
        stage TEXT DEFAULT 'initial_outreach',
        status TEXT DEFAULT 'open',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS feedback (
        id TEXT PRIMARY KEY,
        entity_type TEXT DEFAULT '',
        entity_id TEXT DEFAULT '',
        comment TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS actions (
        id TEXT PRIMARY KEY,
        action_type TEXT,
        agent TEXT,
        details_json TEXT DEFAULT '{}',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS usage_log (
        id TEXT PRIMARY KEY,
        date TEXT UNIQUE,
        claude_calls INTEGER DEFAULT 0,
        usage_percent REAL DEFAULT 0.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS processed_replies (
        reply_id TEXT PRIMARY KEY,
        processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_companies_domain ON companies(domain);
    CREATE INDEX IF NOT EXISTS idx_prospects_company_id ON prospects(company_id);
    CREATE INDEX IF NOT EXISTS idx_prospects_status ON prospects(status);
    CREATE INDEX IF NOT EXISTS idx_prospects_email ON prospects(email);
    CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);
    CREATE INDEX IF NOT EXISTS idx_conversations_status ON conversations(status);
    CREATE INDEX IF NOT EXISTS idx_feedback_entity ON feedback(entity_type, entity_id);
    CREATE INDEX IF NOT EXISTS idx_usage_date ON usage_log(date);
    """,
    # ── v2: normalize identity keys, dedup, uniqueness, hot-path indexes ──
    """
    -- Normalize emails/domains so uniqueness is case-insensitive going forward.
    UPDATE prospects SET email = LOWER(TRIM(email)) WHERE email != LOWER(TRIM(email));
    UPDATE companies SET domain = LOWER(TRIM(domain)) WHERE domain != LOWER(TRIM(domain));

    -- Dedup existing rows (keep the earliest) so unique indexes can be built.
    DELETE FROM prospects WHERE email != '' AND rowid NOT IN (
        SELECT MIN(rowid) FROM prospects WHERE email != '' GROUP BY email
    );
    DELETE FROM prospects WHERE linkedin_url != '' AND rowid NOT IN (
        SELECT MIN(rowid) FROM prospects WHERE linkedin_url != '' GROUP BY linkedin_url
    );
    DELETE FROM companies WHERE domain != '' AND rowid NOT IN (
        SELECT MIN(rowid) FROM companies WHERE domain != '' GROUP BY domain
    );

    -- Enforce uniqueness at the DB level (partial: blank values allowed).
    CREATE UNIQUE INDEX IF NOT EXISTS uq_prospects_email
        ON prospects(email) WHERE email != '';
    CREATE UNIQUE INDEX IF NOT EXISTS uq_prospects_linkedin
        ON prospects(linkedin_url) WHERE linkedin_url != '';
    CREATE UNIQUE INDEX IF NOT EXISTS uq_companies_domain
        ON companies(domain) WHERE domain != '';

    -- Hot-path indexes: campaign stats subqueries, reply handling, dedup checks.
    CREATE INDEX IF NOT EXISTS idx_conversations_campaign_id ON conversations(campaign_id);
    CREATE INDEX IF NOT EXISTS idx_conversations_prospect_id ON conversations(prospect_id);
    CREATE INDEX IF NOT EXISTS idx_conversations_intent ON conversations(intent);
    CREATE INDEX IF NOT EXISTS idx_prospects_name_company
        ON prospects(LOWER(first_name), LOWER(last_name), LOWER(company));
    CREATE INDEX IF NOT EXISTS idx_prospects_status_updated ON prospects(status, updated_at);
    CREATE INDEX IF NOT EXISTS idx_actions_created_at ON actions(created_at);
    """,
    # ── v3: human review queue + account profiling ──
    """
    ALTER TABLE campaigns ADD COLUMN review_note TEXT DEFAULT '';
    ALTER TABLE campaigns ADD COLUMN reviewed_at TIMESTAMP;
    ALTER TABLE campaigns ADD COLUMN reviewed_by TEXT DEFAULT '';

    -- Account analysis produced by the Profiler agent, stored per prospect.
    ALTER TABLE prospects ADD COLUMN profile_json TEXT DEFAULT '';
    ALTER TABLE prospects ADD COLUMN profiled_at TIMESTAMP;

    -- Campaigns written before the review queue existed were 'draft', which
    -- now means "still being written". Move them into the queue so nothing
    -- silently becomes sendable on upgrade.
    UPDATE campaigns SET status = 'pending_review' WHERE status = 'draft';

    CREATE INDEX IF NOT EXISTS idx_prospects_profiled_at ON prospects(profiled_at);
    """,
    # ── v4: an explicit pipeline stage, with history ──
    """
    -- `status` has always held the stage; what was missing was when it last
    -- moved, why, and what it moved from. Without that a CRM sync can only
    -- push a snapshot, and "went to meeting then lost" is indistinguishable
    -- from "was always lost".
    ALTER TABLE prospects ADD COLUMN stage_changed_at TIMESTAMP;
    ALTER TABLE prospects ADD COLUMN stage_reason TEXT DEFAULT '';

    CREATE TABLE IF NOT EXISTS stage_events (
        id TEXT PRIMARY KEY,
        prospect_id TEXT REFERENCES prospects(id),
        from_stage TEXT DEFAULT '',
        to_stage TEXT DEFAULT '',
        reason TEXT DEFAULT '',
        -- who moved it: an agent name, or "human" from the dashboard/CLI.
        actor TEXT DEFAULT '',
        -- 0 = not yet pushed to the CRM, 1 = pushed, 2 = permanently failed.
        -- Kept per event rather than per prospect so a sync outage replays
        -- exactly the changes it missed instead of the current state.
        synced INTEGER DEFAULT 0,
        sync_error TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_stage_events_prospect ON stage_events(prospect_id);
    CREATE INDEX IF NOT EXISTS idx_stage_events_synced ON stage_events(synced);
    CREATE INDEX IF NOT EXISTS idx_stage_events_created ON stage_events(created_at);

    -- Existing rows have a stage but no timestamp for it; their last update
    -- is the closest true answer and beats leaving it null.
    UPDATE prospects SET stage_changed_at = updated_at WHERE stage_changed_at IS NULL;
    """,
    # ── v5: an outbox, for sending we schedule ourselves ──
    """
    -- Instantly held the schedule: we handed it a sequence and it decided
    -- when each step went out. Sending through the user's own mailbox means
    -- owning that, so every step of every sequence becomes a row here with
    -- the time it may be sent and the state it reached.
    --
    -- One row per (campaign, prospect, step), enforced below. That unique
    -- index is the double-send guard: scheduling the same campaign twice —
    -- a retry, a crash mid-deploy, someone clicking approve twice — inserts
    -- nothing the second time rather than queueing a duplicate email.
    CREATE TABLE IF NOT EXISTS outbox (
        id TEXT PRIMARY KEY,
        campaign_id TEXT DEFAULT '',
        prospect_id TEXT REFERENCES prospects(id),
        step INTEGER DEFAULT 1,
        subject TEXT DEFAULT '',
        body TEXT DEFAULT '',
        -- Not before this. The heartbeat sends what is due.
        send_after TIMESTAMP,
        -- pending → sent | cancelled | failed
        status TEXT DEFAULT 'pending',
        -- Why it was cancelled or how it failed. Read by a human, so it is
        -- a sentence rather than a code.
        reason TEXT DEFAULT '',
        provider_message_id TEXT DEFAULT '',
        provider_thread_id TEXT DEFAULT '',
        -- The RFC 2822 Message-ID, which is what the next step references so
        -- the follow-up lands in the same conversation.
        rfc_message_id TEXT DEFAULT '',
        attempts INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        sent_at TIMESTAMP
    );

    CREATE UNIQUE INDEX IF NOT EXISTS uq_outbox_step
        ON outbox(campaign_id, prospect_id, step);
    CREATE INDEX IF NOT EXISTS idx_outbox_due
        ON outbox(status, send_after);
    CREATE INDEX IF NOT EXISTS idx_outbox_prospect ON outbox(prospect_id);
    CREATE INDEX IF NOT EXISTS idx_outbox_thread ON outbox(provider_thread_id);
    """,
]

# Outbox lifecycle. `pending` is the only one the sender acts on, and the
# three others are all terminal — nothing ever goes back to pending, because
# a row that could be re-queued is a row that could be sent twice.
OUTBOX_STATUSES = ("pending", "sent", "cancelled", "failed")

# The prospect pipeline, in order. Mirrors openvz_leads/pipeline.py, which
# owns the transition rules; this tuple exists so state queries can group and
# order by stage without importing it.
PIPELINE_STAGES = (
    "new",        # found, not yet analysed or written to
    "queued",     # outreach drafted, waiting on review or sending
    "contacted",  # something actually went out
    "replied",    # they answered
    "meeting",    # booked
    "won",
    "lost",
    "opted_out",  # asked not to be contacted — terminal, and never reversed
)

# Campaign lifecycle. Writer produces `pending_review` (or `approved` when
# review is switched off); only `approved` is ever handed to a Sender.
CAMPAIGN_STATUSES = (
    "draft",           # being written
    "pending_review",  # waiting on a human
    "approved",        # human said yes — sendable
    "rejected",        # human said no
    "active",          # deployed to an outbound provider
    "failed",
)
SENDABLE_CAMPAIGN_STATUS = "approved"

# Column whitelists for dynamic UPDATEs (prevents SQL injection via kwargs).
_CAMPAIGN_COLUMNS = frozenset({
    "name", "channel", "instantly_campaign_id",
    "sequence_json", "prospect_ids_json", "status",
    "review_note", "reviewed_at", "reviewed_by",
})
_CONVERSATION_COLUMNS = frozenset({
    "prospect_id", "campaign_id", "channel",
    "thread_json", "intent", "stage", "status",
})


class StateManager:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or str(DB_PATH)

    @asynccontextmanager
    async def _connect(self):
        """Open a connection with sane concurrency settings.

        `timeout` maps to SQLite's busy handler, so writers wait for locks
        (e.g. while the dashboard holds a read) instead of erroring.
        """
        db = await aiosqlite.connect(self.db_path, timeout=BUSY_TIMEOUT_SECONDS)
        try:
            yield db
        finally:
            await db.close()

    async def init_db(self):
        """Create/upgrade the schema. Safe to call on every startup."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as db:
            # WAL is persistent in the DB file: readers (dashboard) never
            # block the writer (agent) and vice versa.
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")

            async with db.execute("PRAGMA user_version") as cursor:
                (version,) = await cursor.fetchone()

            for target, script in enumerate(MIGRATIONS, start=1):
                if version < target:
                    await db.executescript(script)
                    await db.execute(f"PRAGMA user_version = {target}")
                    await db.commit()

    # ── Companies ──

    async def add_company(self, company: Company) -> str:
        """Insert a company. If one with the same domain exists, return its id."""
        if not company.id:
            company.id = _new_id()
        company.domain = _norm(company.domain)
        async with self._connect() as db:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO companies
                   (id, name, domain, website, description, industry,
                    company_size, location, source, source_url, notes,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    company.id, company.name, company.domain, company.website,
                    company.description, company.industry, company.company_size,
                    company.location, company.source, company.source_url,
                    company.notes,
                    company.created_at.isoformat(),
                    company.updated_at.isoformat(),
                ),
            )
            await db.commit()
            if cursor.rowcount == 0 and company.domain:
                # Unique-domain conflict: hand back the existing record's id.
                async with db.execute(
                    "SELECT id FROM companies WHERE domain = ?", (company.domain,)
                ) as cur:
                    row = await cur.fetchone()
                    if row:
                        company.id = row[0]
        return company.id

    async def get_company(self, company_id: str) -> Company | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM companies WHERE id = ?", (company_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return Company(**dict(row)) if row else None

    async def get_company_by_domain(self, domain: str) -> Company | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM companies WHERE domain = ?", (_norm(domain),)
            ) as cursor:
                row = await cursor.fetchone()
                return Company(**dict(row)) if row else None

    async def get_contacts_for_company(self, company_id: str) -> list[Prospect]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE company_id = ? ORDER BY score DESC",
                (company_id,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [self._prospect_from_row(r) for r in rows]

    async def company_exists(self, domain: str) -> bool:
        async with self._connect() as db:
            async with db.execute(
                "SELECT 1 FROM companies WHERE domain = ?", (_norm(domain),)
            ) as cursor:
                return bool(await cursor.fetchone())

    # ── Prospects (Contacts) ──

    @staticmethod
    def _prospect_from_row(row: aiosqlite.Row) -> Prospect:
        d = dict(row)
        d["email_verified"] = bool(d.get("email_verified", 0))
        d["phone_verified"] = bool(d.get("phone_verified", 0))
        return Prospect(**d)

    async def add_prospect(self, prospect: Prospect) -> str:
        """Insert a prospect. Duplicates (same email or LinkedIn URL) are not
        re-inserted; the existing record's id is returned instead."""
        if not prospect.id:
            prospect.id = _new_id()
        prospect.email = _norm(prospect.email)
        prospect.linkedin_url = (prospect.linkedin_url or "").strip()
        async with self._connect() as db:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO prospects
                   (id, company_id, first_name, last_name, email, email_verified,
                    phone, phone_verified, linkedin_url, title, seniority,
                    department, source, source_url, status, score,
                    personalization_notes, company, industry, company_size,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    prospect.id, prospect.company_id,
                    prospect.first_name, prospect.last_name,
                    prospect.email, int(prospect.email_verified),
                    prospect.phone, int(prospect.phone_verified),
                    prospect.linkedin_url, prospect.title,
                    prospect.seniority, prospect.department,
                    prospect.source, prospect.source_url,
                    prospect.status, prospect.score,
                    prospect.personalization_notes,
                    prospect.company, prospect.industry, prospect.company_size,
                    prospect.created_at.isoformat(),
                    prospect.updated_at.isoformat(),
                ),
            )
            await db.commit()
            if cursor.rowcount == 0:
                # Unique-constraint conflict: resolve to the existing record.
                for column, value in (
                    ("email", prospect.email),
                    ("linkedin_url", prospect.linkedin_url),
                    ("id", prospect.id),
                ):
                    if not value:
                        continue
                    async with db.execute(
                        f"SELECT id FROM prospects WHERE {column} = ?", (value,)
                    ) as cur:
                        row = await cur.fetchone()
                        if row:
                            prospect.id = row[0]
                            break
        return prospect.id

    async def get_prospect(self, prospect_id: str) -> Prospect | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE id = ?", (prospect_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return self._prospect_from_row(row) if row else None

    async def get_prospects_by_status(self, status: str) -> list[Prospect]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [self._prospect_from_row(r) for r in rows]

    async def update_prospect_status(self, prospect_id: str, status: str):
        async with self._connect() as db:
            await db.execute(
                "UPDATE prospects SET status = ?, updated_at = ? WHERE id = ?",
                (status, _utcnow().isoformat(), prospect_id),
            )
            await db.commit()

    # ── Outbox ──

    async def schedule_outbox(self, rows: list[dict]) -> int:
        """Queue sends. Returns how many were new.

        INSERT OR IGNORE against the unique (campaign, prospect, step) index,
        so calling this twice for the same campaign queues nothing the second
        time. That is the whole double-send guard, and it lives in the
        database rather than in a caller's memory of what it already did.
        """
        if not rows:
            return 0
        now = _utcnow().isoformat()
        async with self._connect() as db:
            cursor = await db.executemany(
                """INSERT OR IGNORE INTO outbox
                   (id, campaign_id, prospect_id, step, subject, body,
                    send_after, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                [
                    (
                        _new_id(),
                        row["campaign_id"],
                        row["prospect_id"],
                        int(row.get("step", 1)),
                        row.get("subject", ""),
                        row.get("body", ""),
                        row["send_after"],
                        now,
                    )
                    for row in rows
                ],
            )
            await db.commit()
            return cursor.rowcount or 0

    async def get_due_outbox(self, limit: int = 25, now: datetime | None = None) -> list[dict]:
        """Pending sends whose time has come, oldest first.

        Ordered by (prospect, step) within time so a prospect's step 2 can
        never be sent in the same pass as their step 1 out of order.
        """
        moment = (now or _utcnow()).isoformat()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM outbox
                   WHERE status = 'pending' AND send_after <= ?
                   ORDER BY send_after ASC, prospect_id ASC, step ASC
                   LIMIT ?""",
                (moment, limit),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def mark_outbox_sent(
        self,
        outbox_id: str,
        *,
        message_id: str = "",
        thread_id: str = "",
        rfc_message_id: str = "",
    ) -> None:
        async with self._connect() as db:
            await db.execute(
                """UPDATE outbox
                   SET status = 'sent', sent_at = ?, provider_message_id = ?,
                       provider_thread_id = ?, rfc_message_id = ?,
                       attempts = attempts + 1
                   WHERE id = ?""",
                (
                    _utcnow().isoformat(),
                    message_id,
                    thread_id,
                    rfc_message_id,
                    outbox_id,
                ),
            )
            await db.commit()

    async def mark_outbox_failed(self, outbox_id: str, error: str) -> None:
        async with self._connect() as db:
            await db.execute(
                """UPDATE outbox
                   SET status = 'failed', reason = ?, attempts = attempts + 1
                   WHERE id = ?""",
                (error[:500], outbox_id),
            )
            await db.commit()

    async def defer_outbox(self, outbox_id: str, send_after: datetime, error: str = "") -> None:
        """Leave it pending, try again later. For failures worth retrying."""
        async with self._connect() as db:
            await db.execute(
                """UPDATE outbox
                   SET send_after = ?, reason = ?, attempts = attempts + 1
                   WHERE id = ? AND status = 'pending'""",
                (send_after.isoformat(), error[:500], outbox_id),
            )
            await db.commit()

    async def rebase_outbox_after_send(
        self, prospect_id: str, *, above_step: int, earliest: datetime
    ) -> int:
        """Push this prospect's later steps out to at least `earliest`.

        The schedule is computed when a campaign is queued, on the assumption
        that each step goes out roughly on time. It does not always: the agent
        is off for a week, the daily cap is hit, a mailbox is unreachable.
        When it catches up, every overdue step is due at once — and sending
        steps two and three ninety seconds apart is worse than sending them
        late, because it is the one thing the follow-up gap exists to prevent.

        So the gap is measured from when the previous step actually went out,
        not from when it was supposed to.
        """
        async with self._connect() as db:
            cursor = await db.execute(
                """UPDATE outbox
                   SET send_after = ?
                   WHERE prospect_id = ? AND status = 'pending'
                     AND step > ? AND send_after < ?""",
                (
                    earliest.isoformat(),
                    prospect_id,
                    above_step,
                    earliest.isoformat(),
                ),
            )
            await db.commit()
            return cursor.rowcount or 0

    async def cancel_outbox_for_prospect(
        self, prospect_id: str, reason: str, *, above_step: int = 0
    ) -> int:
        """Drop this prospect's queued sends. Returns how many were dropped.

        Only touches `pending` rows — a sent email cannot be unsent, and
        pretending otherwise in the record would be a lie about what the
        prospect received.
        """
        async with self._connect() as db:
            cursor = await db.execute(
                """UPDATE outbox
                   SET status = 'cancelled', reason = ?
                   WHERE prospect_id = ? AND status = 'pending' AND step > ?""",
                (reason[:500], prospect_id, above_step),
            )
            await db.commit()
            return cursor.rowcount or 0

    async def get_thread_anchor(self, campaign_id: str, prospect_id: str) -> dict | None:
        """The most recent sent message in this prospect's sequence.

        A follow-up threads onto it: Gmail wants the threadId, and the
        recipient's mail client wants In-Reply-To pointing at the RFC
        Message-ID. Without both, step two arrives as an unrelated cold email
        and the sequence reads as three strangers rather than one.
        """
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM outbox
                   WHERE campaign_id = ? AND prospect_id = ? AND status = 'sent'
                   ORDER BY step DESC LIMIT 1""",
                (campaign_id, prospect_id),
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def get_open_threads(self) -> list[dict]:
        """One sent row per live thread, for the reply check.

        Excludes prospects who already replied or opted out: their follow-ups
        are cancelled and there is nothing left to stop.
        """
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT o.prospect_id, o.campaign_id, o.provider_thread_id,
                          MAX(o.sent_at) AS last_sent_at,
                          p.email, p.status
                   FROM outbox o
                   JOIN prospects p ON p.id = o.prospect_id
                   WHERE o.status = 'sent'
                     AND o.provider_thread_id != ''
                     AND p.status NOT IN ('replied', 'opted_out', 'lost', 'won')
                   GROUP BY o.provider_thread_id"""
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def count_outbox_sent_today(self) -> int:
        """Messages actually sent today — the number a daily cap should count.

        Not the same as prospects contacted today: a follow-up is a send but
        not a new contact, and a cap that ignores follow-ups is a cap that
        lets a mailbox send three times what it was told to.
        """
        start, end = local_day_bounds_utc()
        async with self._connect() as db:
            async with db.execute(
                """SELECT COUNT(*) FROM outbox
                   WHERE status = 'sent' AND sent_at >= ? AND sent_at < ?""",
                (start, end),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    async def count_pending_outbox(self) -> int:
        async with self._connect() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM outbox WHERE status = 'pending'"
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    async def count_due_outbox(self, now: datetime | None = None) -> int:
        moment = (now or _utcnow()).isoformat()
        async with self._connect() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM outbox WHERE status = 'pending' AND send_after <= ?",
                (moment,),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    # ── Pipeline stages ──

    async def set_prospect_stage(
        self,
        prospect_id: str,
        to_stage: str,
        *,
        from_stage: str = "",
        reason: str = "",
        actor: str = "agent",
    ) -> str:
        """Move a prospect and record the move. Returns the stage_event id.

        The write and the event go in one transaction: an event without the
        stage change would replay a move that never happened, and a stage
        change without an event is invisible to the CRM sync forever.
        """
        event_id = _new_id()
        now = _utcnow().isoformat()
        async with self._connect() as db:
            if not from_stage:
                async with db.execute(
                    "SELECT status FROM prospects WHERE id = ?", (prospect_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    from_stage = row[0] if row else ""
            await db.execute(
                """UPDATE prospects
                   SET status = ?, stage_changed_at = ?, stage_reason = ?,
                       updated_at = ?
                   WHERE id = ?""",
                (to_stage, now, reason, now, prospect_id),
            )
            await db.execute(
                """INSERT INTO stage_events
                   (id, prospect_id, from_stage, to_stage, reason, actor,
                    synced, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
                (event_id, prospect_id, from_stage, to_stage, reason, actor, now),
            )
            await db.commit()
        return event_id

    async def get_unsynced_stage_events(self, limit: int = 50) -> list[dict]:
        """Stage changes the CRM has not been told about yet, oldest first.

        Oldest first matters: a CRM receiving "won" before "meeting" ends up
        with a record whose history reads backwards.
        """
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM stage_events
                   WHERE synced = 0
                   ORDER BY created_at ASC
                   LIMIT ?""",
                (limit,),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def mark_stage_event_synced(
        self, event_id: str, *, ok: bool = True, error: str = ""
    ):
        """2, not 0, on permanent failure — a retry loop that never gives up
        blocks every later event behind it."""
        async with self._connect() as db:
            await db.execute(
                "UPDATE stage_events SET synced = ?, sync_error = ? WHERE id = ?",
                (1 if ok else 2, error[:500], event_id),
            )
            await db.commit()

    async def get_stage_history(self, prospect_id: str) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM stage_events
                   WHERE prospect_id = ?
                   ORDER BY created_at ASC""",
                (prospect_id,),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def count_prospects_by_stage(self) -> dict:
        """Every stage present as a key, zeroes included.

        A funnel with holes in it is unreadable, and the dashboard should not
        have to know the stage list to draw one.
        """
        counts = {stage: 0 for stage in PIPELINE_STAGES}
        async with self._connect() as db:
            async with db.execute(
                "SELECT status, COUNT(*) FROM prospects GROUP BY status"
            ) as cursor:
                for status, total in await cursor.fetchall():
                    counts[status or "new"] = counts.get(status or "new", 0) + total
        return counts

    # ── Account profiling ──

    async def save_prospect_profile(self, prospect_id: str, profile: dict) -> None:
        """Attach an account analysis to a prospect."""
        async with self._connect() as db:
            await db.execute(
                """UPDATE prospects
                   SET profile_json = ?, profiled_at = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    json.dumps(profile, ensure_ascii=False),
                    _utcnow().isoformat(),
                    _utcnow().isoformat(),
                    prospect_id,
                ),
            )
            await db.commit()

    async def get_prospects_needing_profile(
        self, min_score: int = 0, limit: int = 5
    ) -> list[Prospect]:
        """Prospects worth analysing: not yet profiled, still in play, and
        scored highly enough to be worth a Claude call. Best first."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM prospects
                   WHERE (profiled_at IS NULL OR profile_json = '')
                     AND status NOT IN ('opted_out', 'lost', 'closed')
                     AND score >= ?
                   ORDER BY score DESC, created_at ASC
                   LIMIT ?""",
                (min_score, limit),
            ) as cursor:
                return [self._prospect_from_row(r) for r in await cursor.fetchall()]

    async def get_profiled_prospects(self) -> list[Prospect]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM prospects
                   WHERE profile_json != '' AND profile_json IS NOT NULL
                   ORDER BY score DESC, profiled_at DESC"""
            ) as cursor:
                return [self._prospect_from_row(r) for r in await cursor.fetchall()]

    async def get_all_prospects(self) -> list[Prospect]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects ORDER BY score DESC, created_at DESC"
            ) as cursor:
                return [self._prospect_from_row(r) for r in await cursor.fetchall()]

    async def get_prospect_by_email(self, email: str) -> Prospect | None:
        """Look up a prospect by email address (indexed, case-insensitive)."""
        email = _norm(email)
        if not email:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE email = ?", (email,)
            ) as cursor:
                row = await cursor.fetchone()
                return self._prospect_from_row(row) if row else None

    async def prospect_exists(
        self, email: str = "", linkedin_url: str = "",
        first_name: str = "", last_name: str = "", company: str = "",
    ) -> bool:
        async with self._connect() as db:
            if email:
                async with db.execute(
                    "SELECT 1 FROM prospects WHERE email = ?", (_norm(email),)
                ) as cursor:
                    if await cursor.fetchone():
                        return True
            if linkedin_url:
                async with db.execute(
                    "SELECT 1 FROM prospects WHERE linkedin_url = ?",
                    (linkedin_url.strip(),),
                ) as cursor:
                    if await cursor.fetchone():
                        return True
            # Name + company dedup (case-insensitive, expression-indexed)
            if first_name and last_name and company:
                async with db.execute(
                    """SELECT 1 FROM prospects
                       WHERE LOWER(first_name) = ? AND LOWER(last_name) = ?
                         AND LOWER(company) = ?""",
                    (first_name.lower(), last_name.lower(), company.lower()),
                ) as cursor:
                    if await cursor.fetchone():
                        return True
        return False

    async def count_prospects_by_status(self) -> dict[str, int]:
        async with self._connect() as db:
            async with db.execute(
                "SELECT status, COUNT(*) FROM prospects GROUP BY status"
            ) as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}

    # ── Feedback ──

    async def add_feedback(
        self, entity_type: str, entity_id: str, comment: str
    ) -> str:
        feedback_id = _new_id()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO feedback (id, entity_type, entity_id, comment)
                   VALUES (?, ?, ?, ?)""",
                (feedback_id, entity_type, entity_id, comment),
            )
            await db.commit()
        return feedback_id

    async def get_feedback(
        self, entity_type: str, entity_id: str
    ) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM feedback
                   WHERE entity_type = ? AND entity_id = ?
                   ORDER BY created_at DESC""",
                (entity_type, entity_id),
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def get_all_feedback(self) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM feedback ORDER BY created_at DESC LIMIT 100"
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    # ── Reply Deduplication ──

    async def is_reply_processed(self, reply_id: str) -> bool:
        """Check if a reply has already been processed."""
        if not reply_id:
            return False
        async with self._connect() as db:
            async with db.execute(
                "SELECT 1 FROM processed_replies WHERE reply_id = ?", (reply_id,)
            ) as cursor:
                return bool(await cursor.fetchone())

    async def mark_reply_processed(self, reply_id: str):
        """Mark a reply as processed to avoid double-handling."""
        if not reply_id:
            return
        async with self._connect() as db:
            await db.execute(
                "INSERT OR IGNORE INTO processed_replies (reply_id) VALUES (?)",
                (reply_id,),
            )
            await db.commit()

    # ── Campaigns ──

    async def add_campaign(self, campaign: Campaign) -> str:
        if not campaign.id:
            campaign.id = _new_id()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO campaigns
                   (id, name, channel, instantly_campaign_id, sequence_json,
                    prospect_ids_json, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    campaign.id, campaign.name, campaign.channel,
                    campaign.instantly_campaign_id, campaign.sequence_json(),
                    json.dumps(campaign.prospect_ids), campaign.status,
                    campaign.created_at.isoformat(),
                ),
            )
            await db.commit()
        return campaign.id

    @staticmethod
    def _campaign_from_row(row: aiosqlite.Row) -> Campaign:
        d = dict(row)
        d["sequence"] = Campaign.sequence_from_json(d.pop("sequence_json", None))
        d["prospect_ids"] = json.loads(d.pop("prospect_ids_json", None) or "[]")
        return Campaign(**d)

    async def get_campaigns_by_status(self, status: str) -> list[Campaign]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM campaigns WHERE status = ?", (status,)
            ) as cursor:
                return [self._campaign_from_row(r) for r in await cursor.fetchall()]

    async def get_campaign(self, campaign_id: str) -> Campaign | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return self._campaign_from_row(row) if row else None

    async def get_all_campaigns(self) -> list[Campaign]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM campaigns ORDER BY created_at DESC"
            ) as cursor:
                return [self._campaign_from_row(r) for r in await cursor.fetchall()]

    async def count_campaigns_by_status(self) -> dict[str, int]:
        async with self._connect() as db:
            async with db.execute(
                "SELECT status, COUNT(*) FROM campaigns GROUP BY status"
            ) as cursor:
                return {row[0]: row[1] for row in await cursor.fetchall()}

    async def review_campaign(
        self,
        campaign_id: str,
        approved: bool,
        note: str = "",
        reviewer: str = "human",
    ) -> bool:
        """Record a human decision on a queued campaign.

        Only campaigns actually waiting for review can be decided, so a
        double-click in the dashboard can't resurrect an already-sent
        campaign. Returns True when a decision was recorded.
        """
        async with self._connect() as db:
            cursor = await db.execute(
                """UPDATE campaigns
                   SET status = ?, review_note = ?, reviewed_at = ?, reviewed_by = ?
                   WHERE id = ? AND status IN ('draft', 'pending_review')""",
                (
                    "approved" if approved else "rejected",
                    (note or "").strip(),
                    _utcnow().isoformat(),
                    reviewer,
                    campaign_id,
                ),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def edit_pending_sequence(
        self, campaign_id: str, sequence: list[dict]
    ) -> list[dict] | None:
        """Replace the copy of a campaign that is still awaiting review.

        A reviewer who spots one wrong sentence should be able to fix it,
        not have to reject a whole three-email sequence and wait for the
        Writer to try again.

        Guarded the same way ``review_campaign`` is — only ``draft`` and
        ``pending_review`` can be edited. Once a campaign is approved its
        copy is what a person signed off on, and once it is active the copy
        may already be in someone's inbox; letting either be rewritten
        afterwards would mean the record no longer says what was sent.

        Only ``subject``, ``body`` and ``delay_days`` are taken from the
        caller. The step numbering and the length of the sequence come from
        what is already stored, so an edit cannot add a fourth email nobody
        reviewed or renumber the send schedule out from under the outbox.

        Returns the stored sequence, or None if the campaign was not open
        for editing.
        """
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT sequence_json FROM campaigns
                   WHERE id = ? AND status IN ('draft', 'pending_review')""",
                (campaign_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None

            try:
                current = json.loads(row["sequence_json"] or "[]")
            except (json.JSONDecodeError, TypeError):
                current = []
            if not isinstance(current, list):
                current = []

            edits = {}
            for item in sequence or []:
                if isinstance(item, dict) and item.get("step") is not None:
                    edits[str(item["step"])] = item

            merged = []
            for index, step in enumerate(current):
                step = dict(step) if isinstance(step, dict) else {}
                edit = edits.get(str(step.get("step", index + 1)))
                if edit:
                    if "subject" in edit:
                        step["subject"] = str(edit["subject"] or "").strip()
                    if "body" in edit:
                        step["body"] = str(edit["body"] or "").strip()
                    if "delay_days" in edit:
                        try:
                            # A negative delay would schedule a follow-up
                            # before the email it follows up on.
                            step["delay_days"] = max(0, int(edit["delay_days"]))
                        except (TypeError, ValueError):
                            pass
                merged.append(step)

            cursor = await db.execute(
                """UPDATE campaigns SET sequence_json = ?
                   WHERE id = ? AND status IN ('draft', 'pending_review')""",
                (json.dumps(merged, ensure_ascii=False), campaign_id),
            )
            await db.commit()
            return merged if cursor.rowcount > 0 else None

    async def update_campaign(self, campaign_id: str, **kwargs):
        """Update whitelisted campaign columns in a single atomic statement."""
        if not kwargs:
            return
        invalid = set(kwargs) - _CAMPAIGN_COLUMNS
        if invalid:
            raise ValueError(f"Invalid campaign column(s): {sorted(invalid)}")
        set_clause = ", ".join(f"{key} = ?" for key in kwargs)
        async with self._connect() as db:
            await db.execute(
                f"UPDATE campaigns SET {set_clause} WHERE id = ?",
                (*kwargs.values(), campaign_id),
            )
            await db.commit()

    # ── Conversations ──

    async def add_conversation(self, convo: Conversation) -> str:
        if not convo.id:
            convo.id = _new_id()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO conversations
                   (id, prospect_id, campaign_id, channel, thread_json,
                    intent, stage, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    convo.id, convo.prospect_id, convo.campaign_id,
                    convo.channel, convo.thread_json(), convo.intent,
                    convo.stage, convo.status, convo.created_at.isoformat(),
                    convo.updated_at.isoformat(),
                ),
            )
            await db.commit()
        return convo.id

    async def get_conversations_by_status(self, status: str) -> list[Conversation]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM conversations WHERE status = ?", (status,)
            ) as cursor:
                rows = await cursor.fetchall()
                convos = []
                for r in rows:
                    d = dict(r)
                    d["thread"] = Conversation.thread_from_json(d.pop("thread_json"))
                    convos.append(Conversation(**d))
                return convos

    async def update_conversation(self, convo_id: str, **kwargs):
        """Update whitelisted conversation columns atomically (bumps updated_at)."""
        if not kwargs:
            return
        invalid = set(kwargs) - _CONVERSATION_COLUMNS
        if invalid:
            raise ValueError(f"Invalid conversation column(s): {sorted(invalid)}")
        set_clause = ", ".join(f"{key} = ?" for key in kwargs)
        async with self._connect() as db:
            await db.execute(
                f"UPDATE conversations SET {set_clause}, updated_at = ? WHERE id = ?",
                (*kwargs.values(), _utcnow().isoformat(), convo_id),
            )
            await db.commit()

    # ── Actions Log ──

    async def log_action(self, action_type: str, agent: str, details: dict | None = None):
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO actions (id, action_type, agent, details_json) VALUES (?, ?, ?, ?)",
                (_new_id(), action_type, agent, json.dumps(details or {})),
            )
            await db.commit()

    # ── Analytics ──

    async def get_campaign_stats(self) -> list[dict]:
        """Get performance stats for each campaign (one indexed pass over conversations)."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT c.id, c.name, c.status, c.prospect_ids_json,
                          COUNT(v.id) as reply_count,
                          SUM(CASE WHEN v.intent = 'interested' THEN 1 ELSE 0 END) as interested_count,
                          SUM(CASE WHEN v.intent = 'objection' THEN 1 ELSE 0 END) as objection_count,
                          SUM(CASE WHEN v.intent = 'not_interested' THEN 1 ELSE 0 END) as not_interested_count
                   FROM campaigns c
                   LEFT JOIN conversations v ON v.campaign_id = c.id
                   WHERE c.status IN ('active', 'completed')
                   GROUP BY c.id
                   ORDER BY c.created_at DESC"""
            ) as cursor:
                rows = await cursor.fetchall()
                stats = []
                for r in rows:
                    d = dict(r)
                    for key in ("interested_count", "objection_count", "not_interested_count"):
                        d[key] = d[key] or 0
                    prospect_ids = json.loads(d.get("prospect_ids_json") or "[]")
                    d["leads_count"] = len(prospect_ids)
                    d["reply_rate"] = (
                        round(d["reply_count"] / len(prospect_ids) * 100, 1)
                        if prospect_ids else 0
                    )
                    stats.append(d)
                return stats

    async def get_intent_distribution(self) -> dict[str, int]:
        """Count conversations by intent."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT intent, COUNT(*) FROM conversations WHERE intent != '' GROUP BY intent"
            ) as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}

    async def get_stage_distribution(self) -> dict[str, int]:
        """Count conversations by sales stage."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT stage, COUNT(*) FROM conversations WHERE stage != '' GROUP BY stage"
            ) as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}

    # ── Usage Tracking ──

    async def get_usage_today(self) -> int:
        today = date.today().isoformat()
        async with self._connect() as db:
            async with db.execute(
                "SELECT claude_calls FROM usage_log WHERE date = ?", (today,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    async def increment_usage(self):
        today = date.today().isoformat()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO usage_log (id, date, claude_calls)
                   VALUES (?, ?, 1)
                   ON CONFLICT(date) DO UPDATE SET claude_calls = claude_calls + 1""",
                (_new_id(), today),
            )
            await db.commit()

    # ── Summary for Decision Making ──

    async def get_state_summary(self) -> dict:
        prospect_counts = await self.count_prospects_by_status()
        today = date.today().isoformat()
        async with self._connect() as db:
            async with db.execute(
                """SELECT
                     SUM(CASE WHEN status = 'draft' THEN 1 ELSE 0 END),
                     SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END),
                     SUM(CASE WHEN status = 'pending_review' THEN 1 ELSE 0 END),
                     SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END)
                   FROM campaigns"""
            ) as cursor:
                row = await cursor.fetchone()
                draft_campaigns = row[0] or 0
                active_campaigns = row[1] or 0
                pending_review = row[2] or 0
                approved_campaigns = row[3] or 0
            async with db.execute(
                """SELECT COUNT(*) FROM prospects
                   WHERE (profiled_at IS NULL OR profile_json = '')
                     AND status NOT IN ('opted_out', 'lost', 'closed')"""
            ) as cursor:
                unprofiled = (await cursor.fetchone())[0]
            async with db.execute(
                "SELECT COUNT(*) FROM conversations WHERE status = 'open'"
            ) as cursor:
                open_conversations = (await cursor.fetchone())[0]
            async with db.execute(
                "SELECT claude_calls FROM usage_log WHERE date = ?", (today,)
            ) as cursor:
                usage_row = await cursor.fetchone()
                usage_today = usage_row[0] if usage_row else 0

        return {
            "prospects": prospect_counts,
            "stages": await self.count_prospects_by_stage(),
            # Only ever non-zero on the Gmail path, where this product owns
            # the schedule. Surfaced in the summary because the heartbeat
            # decides what to do next from these counts, and a follow-up due
            # in an hour is work even when no campaign is awaiting deploy.
            "due_sends": await self.count_due_outbox(),
            "pending_sends": await self.count_pending_outbox(),
            "draft_campaigns": draft_campaigns,
            "active_campaigns": active_campaigns,
            "pending_review": pending_review,
            "approved_campaigns": approved_campaigns,
            "unprofiled_prospects": unprofiled,
            "open_conversations": open_conversations,
            "usage_today": usage_today,
        }
