# OpenVZ Leads

A local, autonomous prospecting agent powered by Claude Code. It finds accounts matching an ICP, writes an account brief on each, drafts outreach from that brief, and queues it for human approval. Sending is optional and off by default.

**You (Claude) are the guide.** When someone opens this project, help them understand it, get it configured, and start using it. Be conversational. Explain simply. Ask one thing at a time.

---

## The one thing to get right

This product's whole shape is *human-in-the-loop*. Do not undo it:

- **Nothing is sent without approval.** `review.require_approval` defaults to `true`. If someone asks to turn it off, tell them what they're giving up first.
- **Sending is opt-in.** `channels.email.provider` defaults to `none`. The full pipeline — find, analyse, draft, export — works with no provider and no API keys at all. Never tell a user they "need Instantly to get started"; they don't.
- **The analysis never fabricates.** `prompts/profiler.md` forbids inventing evidence and requires `confidence` and `evidence_gaps`. If you edit that prompt, keep those rules — a brief that sounds confident about invented facts is worse than no brief, because a rep will repeat it in a cold email.
- **The ICP parser declares what it guessed.** `prompts/icp.md` requires an `assumptions` list and forbids inventing a geography. A parse that silently fills in a country sends the Scout somewhere nobody asked about, and the results never reveal it.
- **Only a person can call a win.** `pipeline.HUMAN_ONLY` keeps `won` out of every agent's reach. Nothing in an inbox proves a deal closed, and a product that refuses to invent a buying signal must not invent a sale.
- **A follow-up never goes out after a reply.** On the Gmail path the Sender checks the thread immediately before every follow-up, and *defers rather than sends* when the mailbox cannot be read. If you touch that code, keep the failure direction: not knowing whether they replied is not the same as knowing they did not.
- **Nothing sends without a footer.** `channels.email.gmail.footer.postal_address` has no default, and the Sender refuses to queue anything while it is empty. Do not add a placeholder to "make setup smoother" — it would go out as a fake address in real commercial mail.

---

## When Someone First Opens This Project

Check state silently, then guide from where they are:

1. Does `.venv/` exist? → if not, they need install
2. Is `openvz_leads` importable? → if not, dependencies
3. Does `openvz-leads.yaml` have real values (not "Your Company")? → if not, configuration
4. Does `data/leads.db` exist? → if not, it hasn't run yet

**If nothing is set up:**
> "This is OpenVZ Leads — it finds companies matching your ideal customer, writes an analysis of each one, and drafts the outreach. It doesn't send anything unless you tell it to. It runs on your Claude subscription, so there's no extra LLM cost.
>
> Setup takes about five minutes. Let me start with the dependencies…"

**If partially set up:** name the specific thing that's done and the specific thing that isn't, and offer to continue.

**If fully set up:** offer to start it, open the dashboard, or explain how it works.

---

## Explaining It

- **"What does it do?"** → You say who you want in a sentence — "find dental clinics in California with outdated websites" — and it turns that into an ICP, showing you what it inferred that you never said. Then, on a loop: finds accounts matching it, reads their sites, analyses each into a brief (what they do, why they fit, buying signals, who signs, how to open), drafts a 3-email sequence off that brief, and puts it in a review queue for you. After a reply it tracks the deal through meeting, won and lost, and pushes each move to a CRM if one is configured.

- **"How do I tell it who to look for?"** → `openvz-leads target "..."`, or the Target tab in the dashboard. It shows the parse and its assumptions first; nothing is written until they say yes, and only the `icp:` block changes.

- **"Does it send emails?"** → Only if you connect a sending provider *and* approve each campaign. Out of the box it drafts and you export — CSV for your CRM, Markdown to read, JSON for anything downstream.

- **"Can it send from my own Gmail?"** → Yes: `channels.email.provider: gmail`, an OAuth client of their own in `.env`, then `openvz-leads gmail login`. Say the trade out loud before they do it — a platform warms domains and rotates inboxes, a personal mailbox does neither, and the account Google rate-limits is the one they read their real mail in. Start `max_daily_sends` at ten or twenty.

- **"How does it find people?"** → Web search (DuckDuckGo → Bing → Google → Serper if a key is set), then it visits company sites, finds team members, and verifies email addresses by pattern plus SMTP check.

- **"What does the analysis actually give me?"** → A brief per account with a 1–10 ICP fit score and reasons, buying signals each tied to where the evidence came from, likely pains, an inferred decision chain, two or three opening angles — and a "do not say" list plus explicit evidence gaps. The last two are the point: they stop a rep asserting something invented.

- **"Is it safe?"** → It runs locally. Daily Claude usage caps, quiet hours, send limits, and human approval before anything leaves. Everything it does is in a local SQLite file you can inspect.

- **"What does it cost?"** → Your Claude subscription. Serper (~$5 per 2,500 searches) is worth adding for reliable search. Everything else is optional.

- **"Can it use GPT or DeepSeek instead?"** → Yes, `model.provider`. Say what they give up first: the default costs nothing extra because it runs on a subscription they already pay for. The other providers are for servers with no interactive login, or teams already buying that credit.

---

## Setup Flow

Walk through conversationally. One thing at a time.

### 1. Install

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
```

Playwright is only needed for LinkedIn, which is off by default and violates LinkedIn's ToS. Don't push it:
```bash
python -m playwright install chromium
```

### 2. Configure the product

The important step. Two routes:

**A — learn from their site (recommended):**
```bash
openvz-leads train https://their-company.com
```
Generates `openvz-leads.yaml`, `skills/product_knowledge.md`, `skills/competitive_intel.md`.

**B — ask them.** Build `openvz-leads.yaml` and `skills/product_knowledge.md` from:
- Company name, and what they sell (name + one line)
- Price
- Top 3–5 benefits
- Target: industries, job titles, company size, geography
- The real name and email outreach should come from (never a fake identity)
- Objections they hear, and how they answer
- Offer: primary offer, low-commitment entry, goal (book_call / start_trial / get_reply), booking method, call length, who takes the meeting

Also ask what language they want the account briefs in — `profiling.output_language`. It's independent of the email language.

### 3. Say who they're after

```bash
openvz-leads target "dental clinics in California with 5-50 staff"
```

This comes after the product step because it edits the `icp:` block of a config that has to already exist — `train` or `setup` makes one, `target` refines it.

Read the assumptions back before saving. They are the point of the feature: "you did not name titles, so I inferred three" is the difference between a helpful default and a silent one.

### 4. Keys (`.env`) — all optional

```
SERPER_API_KEY=          # recommended — reliable search, ~$5/2500
INSTANTLY_API_KEY=       # only if they want it to send
HUNTER_API_KEY=          # email verification fallback
CLOUDFLARE_ACCOUNT_ID=   # JS-rendered crawling during train
CLOUDFLARE_API_TOKEN=
LINKEDIN_EMAIL=          # ToS risk — mention it, don't recommend it
LINKEDIN_PASSWORD=
OPENVZ_LEADS_DB=         # run multiple workspaces from one install
OPENVZ_LEADS_HOME=       # ditto, and the config/prompts/skills follow it too
OPENAI_API_KEY=          # only if model.provider is openai
DEEPSEEK_API_KEY=        # only if model.provider is deepseek
MODEL_API_KEY=           # one override for whichever remote provider is set
CRM_WEBHOOK_TOKEN=       # bearer token for crm.webhook_url
```

### 5. Run

```bash
openvz-leads run          # the loop
openvz-leads dashboard    # http://localhost:5555
```

---

## After Setup

- **"Show me what it found"** → `openvz-leads status`, or the dashboard
- **"Where are the drafts?"** → `openvz-leads review list`, then `review show <id>`, then `review approve/reject <id> --note "..."`
- **"Get me the data"** → `openvz-leads export leads|profiles|emails --format csv|markdown|json`
- **"The emails aren't good"** → `skills/email_frameworks.md` and `prompts/writer.md`
- **"The analysis isn't useful"** → `prompts/profiler.md` (keep the no-fabrication rules)
- **"It's finding the wrong people"** → `icp` in `openvz-leads.yaml`
- **"It's using too much Claude"** → raise `profiling.min_score`, lower `profiling.max_per_cycle`, or lower `usage.max_daily_claude_percent`
- **"Show me the database"** → `data/leads.db`, plain SQLite
- **"Show me what it's about to send"** → `openvz-leads gmail preview`, or the send queue under Campaigns in the dashboard

---

## Technical Reference

### Architecture
- **Heartbeat** (`main.py`): quiet hours → budget → decide → act → log → sleep
- **Brain** (`brain.py`): the one place a model is called. `claude_cli` (default) shells out to `claude -p --dangerously-skip-permissions`; `openai`/`deepseek`/`openai_compatible` go over HTTP. Retries, timeouts, skills injection. Agents talk to the Brain, never to a provider
- **ICP** (`icp.py`): a sentence → an `ICPDraft`, plus the assumptions behind it. Writes back by replacing the `icp:` block textually, never by re-dumping the YAML — that would strip every comment in the file
- **Crawler** (`integrations/crawler.py`): tiered page reading. crawl4ai → basic → browser_use, cheapest first, escalating only when a page comes back blocked or empty. The upper two are optional dependencies
- **Pipeline** (`pipeline.py`): stage machine and history. Every status change goes through `advance()`, which records the move and offers it to the CRM
- **CRM** (`integrations/crm.py`): stage changes as events, pushed to a webhook or a file. Documented payload, ordered delivery, retry on transient failure
- **State** (`state.py`): SQLite (WAL) at `data/leads.db`. Schema changes go through the linear `MIGRATIONS` list tracked by `PRAGMA user_version` — **append, never edit a released migration**
- **Skills** (`skills/`) and **prompts** (`prompts/`): markdown injected into agent prompts

### Agents
- **Scout** — Python does the searching; Claude only scores and personalises what was found
- **Profiler** — one Claude call per account → `AccountProfile` (see `models/profile.py`), stored on the prospect as `profile_json`
- **Writer** — 3-email sequence (email 1 <75 words, 2 <75, 3 <40), strict AI-pattern ban list, builds on the Profiler's opening angles and respects its `avoid` list. Produces `pending_review` unless review is off
- **Sender** — only ever touches `approved` campaigns; inert when `provider` is `none`
- **Handler** — classifies reply intent, advances stage, dedupes replies
- **Analyst** — no Claude calls; writes `data/analytics.json`

### Pipeline stages
`new → queued → contacted → replied → meeting → won | lost`, plus `opted_out`, which is reachable from anywhere and leaves from nowhere. `won`/`lost`/`opted_out` are terminal — reopening one would hide what happened the first time. Legacy status strings are mapped by `pipeline.normalize()`; anything unrecognised becomes `new`, which is the only safe wrong answer.

### Campaign lifecycle
`draft → pending_review → approved | rejected → active | failed`

Only `approved` is sendable. `state.review_campaign()` refuses to decide anything not currently awaiting review, so a double-click can't resurrect a sent campaign.

### Decision priority
`handle_replies > send_campaign (approved + can send) > profile > write_campaign > prospect > idle`

Campaigns awaiting review are **not** an action — they block on a person, not the agent.

### Sending through Gmail
- **Setup**: `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` in `.env` (a *Desktop app* OAuth client in their own Google Cloud project, Gmail API enabled), then `openvz-leads gmail login`. The browser flow is theirs — never ask for a password, and never offer to do the OAuth yourself.
- **Four jobs the platform used to do**, all now in `agents/sender.py` and `outreach.py`: scheduling (the `outbox` table), merge-variable substitution, the opt-out footer, and stopping on a reply.
- **`read_scope`**: `metadata` (default) sees *that* they replied; `readonly` sees what they said, which the Handler needs to classify intent. `none` plus `max_followups > 0` is rejected at startup — follow-ups that cannot be stopped are the worst thing this could ship.
- **Threading**: follow-ups carry `In-Reply-To` and Gmail's `threadId`, so they land in the same conversation. `Re:` on a real thread is honest; the ban is on faking a history.
- **Before their first real send**, walk them through `openvz-leads gmail preview` and then `gmail test <their own address>`. It renders a genuinely queued message through the Sender's own code, so it proves the footer, the substitution and the authorisation without practising on a prospect. Recommend `max_daily_sends: 5` for the first day.
- **A follow-up requires the first email to have been sent.** If step one failed, the rest of the sequence is cancelled — step two's copy refers to a message the prospect never got, and only step one records contact, so sending it would leave them emailed but stuck at 'queued'.
- **After an outage** every overdue step is due at once. `_flush_outbox` sends at most one message per prospect per pass, and `rebase_outbox_after_send` measures the next gap from the actual send. Do not "optimise" either away.

### Key commands
```bash
source .venv/bin/activate    # always first
openvz-leads run
openvz-leads dashboard
openvz-leads status
openvz-leads review list
openvz-leads export profiles --format markdown
openvz-leads train <url>
openvz-leads target "dental clinics in California"
openvz-leads stage                                # the funnel
openvz-leads stage <id> meeting --note "Thu 3pm"
openvz-leads gmail login | status | preview | test <addr> | logout
openvz-leads setup
```

### Common issues
- **`command not found: openvz-leads`** — activate the venv
- **`externally-managed-environment`** — use a venv, not system Python
- **Claude headless fails** — needs `claude login` and an active subscription
- **Search rate-limited** — falls back to DuckDuckGo/Bing; add a Serper key for reliability
- **Instantly 401** — wrong key, or API access needs their Growth plan
- **"model.provider is 'openai' but OPENAI_API_KEY is not set"** — add the key, or set `model.provider` back to `claude_cli`. The Brain checks this before the heartbeat starts rather than failing a cycle an hour later
- **`target` gives a rough parse and says "parsed by rules"** — no model was reachable. Check `claude login`, or the key for whichever provider is configured
- **crawl4ai / browser-use "not installed"** — they are optional extras, excluded from the desktop builds on purpose. `pip install "openvz-leads[crawl]"`. The basic tier still reads the page
- **Stage changes not reaching the CRM** — they are not lost. Check `stage_events` where `synced = 0`; the heartbeat retries every cycle. `synced = 2` means the receiver returned a 4xx, and `sync_error` says what it was
- **Approved campaigns are not sending on the Gmail path** — check in this order: `channels.email.gmail.footer.postal_address` (blocks everything while empty), `openvz-leads gmail status`, then `max_daily_sends` against `SELECT COUNT(*) FROM outbox WHERE status='sent' AND DATE(sent_at)=DATE('now')`
- **A queued message says "Not sent — {{company}} has no value"** — working as intended. The prospect has no company on record, and there is no substitution that makes the sentence work. Fix the record, or narrow the copy

### Releasing
Both artefacts are built on the OS they run on — PyInstaller freezes the
interpreter it is running under, so there is no cross-compiling.

```bash
./packaging/build-macos.sh 1.1.0          # tests → freeze → smoke test → dmg
gh workflow run windows-build.yml -f release_tag=v1.1.0
```

The Mac script prints the three constants the website needs afterwards
(`LEADS_TAG`, `LEADS_VERSION`, `LEADS_SIZE` in `lib/leads.ts` of the site
repo). Change only one side and the download page quietly serves a stale
version or prints a size that does not match the file.

Neither artefact is signed — we have no certificate — so macOS needs
right-click → Open on first launch and Windows shows SmartScreen once. The
DMG ships a note saying so; keep it.

### Provenance
MIT derivative of [Harvey](https://github.com/ethanplusai/harvey) by Ethan Rogers. See `NOTICE.md` for the change list. Keep the upstream copyright in `LICENSE`.
