# OpenVZ Leads

**Find them. Understand them. Reach them.**

A local, autonomous prospecting agent. It finds accounts matching your ICP, writes a decision-ready brief on each one, drafts the outreach from that brief — and then stops and waits for you.

[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://www.python.org)
[![openvzai.com](https://img.shields.io/badge/site-www.openvzai.com-000000)](https://www.openvzai.com)

> 中文文档：[README.md](README.md)
> Upstream attribution and change list: [NOTICE.md](NOTICE.md)

```
$ openvz-leads run

============================================================
OpenVZ Leads is online. Find them. Understand them. Reach them.
============================================================
Draft-only mode: no outbound provider configured.
Human review is ON — no campaign is sent until you approve it.
Decision: prospect (only 0 new prospect(s); pipeline needs leads)
Scout: Added prospect Lena Fischer — Head of Procurement at Northwind Logistics
Decision: profile (prospects are waiting to be analysed)
Profiler: Northwind Logistics — fit 6/10, confidence medium.
Decision: write_campaign (1 analysed prospect(s) need outreach)
Writer: Campaign 'logistics-outreach' is waiting for your review.
```

---

## It starts with a sentence

```bash
openvz-leads target "Find dental clinics in California with outdated websites and 5-50 employees"
openvz-leads target "帮我找美国牙科诊所"
```

That becomes a structured ICP — **and a list of what it filled in that you never said.** You did not name job titles, so it inferred three. You did not give a size, so any size qualifies. You see that before anything is written. The dashboard's Target tab is the same thing with a text box.

The qualifier is the part that matters. "With outdated websites" is not an industry, a size or a place, so a four-field parse drops it and hands back clinics that are perfectly happy with their website. It lands in `icp.keywords`, which does two things: loosens the search queries, and becomes a criterion **the account analysis must check against evidence** — confirmed, contradicted, or unknown.

With no model available (no Claude CLI, or a configured provider with no key) a rule-based parser takes over. It is rougher, and it says so: `confidence: low`, `parsed by rules`. But the box still does something when you type in it.

## One pipeline

| | What happens | Who does it |
|---|---|---|
| **Say who** | A sentence → an ICP (industry / size / geography / titles / qualifiers), plus what was inferred | `openvz-leads target` |
| **Find** | Searches for companies matching the ICP, reads their sites, identifies decision-makers, infers and verifies emails. No Apollo, no ZoomInfo | Scout |
| **Understand** | One brief per account: what they do, why they fit (score + reasons), buying signals, likely pains, decision chain, opening angles — and **what not to say** | Profiler |
| **Write** | A 3-email sequence built on that brief, following proven frameworks under one hard rule: invent nothing | Writer |
| **Wait for you** | The campaign enters a review queue. Nothing leaves before you approve it | You |
| **Follow up** | Sent from your own Gmail on a schedule this owns; **the moment they reply, the rest of the sequence is dropped**, and a clear no is recorded as lost | Sender / Handler |
| **Move it along** | Contacted → replied → meeting → won / lost. Every move recorded, and pushed to a CRM if one is configured | `openvz-leads stage` |

Then it stops. **Nothing is sent by default.**

**Only a person can call a win.** Nothing in an inbox proves a deal closed — an enthusiastic reply is not a contract. A product that refuses to invent a buying signal has no business inventing a sale.

## Why sending is off by default

Upstream Harvey closes the loop automatically: written, then deployed. This fork makes sending an optional plugin, for practical reasons:

- **The full pipeline works with no outbound channel at all.** Find → analyse → draft → export as CSV/Markdown, then send from your own inbox or CRM. No Instantly account, no subscription.
- **Review is on by default.** Finished campaigns land in a review queue and can only be sent after you approve them, in the dashboard or from the CLI. No waking up to find it emailed 200 people something it made up.
- **The analysis is written to be read by a human.** Every point carries its evidence source and a confidence level, and there is an explicit "evidence gaps" section. It does not pretend to know what it doesn't.

To let it send: configure Instantly and set `channels.email.provider` to `instantly`. Human review still stays on — turning that off is a separate, deliberate change to `review.require_approval`.

## Cost

The brain is a headless `claude -p` call running on the Claude subscription you already have, so there is **no extra LLM cost**.

The one key worth adding is Serper (~$5 per 2,500 searches, no subscription); without it, search degrades to scraping DuckDuckGo/Bing and gets rate-limited. Every other key is optional.

---

## Quick start

### Prerequisites

- Python 3.11+
- [Claude Code CLI](https://claude.ai/download), authenticated with `claude login` and an active subscription

### Install

```bash
git clone https://github.com/mrlin728/openvz-leads.git
cd openvz-leads
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
python -m playwright install chromium   # optional, LinkedIn only
```

### Configure

Pick one:

```bash
# A. Learn from your own site (recommended) — writes openvz-leads.yaml
#    plus a product knowledge base
openvz-leads train https://your-company.com

# B. Interactive wizard
openvz-leads setup
```

### Say who you're after

```bash
openvz-leads target "Find dental clinics in California with 5-50 staff"
```

It prints the parse and everything it inferred, and only writes to
`openvz-leads.yaml` once you say yes — replacing the `icp:` block alone and
leaving the rest of the file, comments included, exactly as it was.

### Run

```bash
openvz-leads run          # heartbeat: find → analyse → draft → queue for review
openvz-leads dashboard    # http://localhost:5555 in your browser
                          # (the installed app opens its own window instead)
openvz-leads stage        # where everyone is in the pipeline
openvz-leads gmail login  # only if sending from your own mailbox
openvz-leads gmail preview        # print the next message, send nothing
openvz-leads gmail test you@…     # post that same message to yourself
```

**Run `gmail preview` before the first real send.** It renders the next message actually sitting in the queue, through the same functions the Sender uses, so it proves the four things otherwise only provable by emailing a stranger: the merge variables resolved, the footer is present and correct, the account is authorised, and the mail looks the way you meant it to. `gmail test` posts that same message to yourself — and only to the address you name.

---

## Day to day

### See what it has done

```bash
openvz-leads status
```

```
  OpenVZ Leads — Pipeline Status
  ============================================
  Prospects:              {'new': 34}
  Waiting to be analysed: 12
  Awaiting your review:   2
  Approved, ready to go:  1
  Active campaigns:       0
  Open conversations:     0
  Claude calls today:     47
```

### Review the drafts

```bash
openvz-leads review list                    # what's waiting
openvz-leads review show <id>               # read all three emails
openvz-leads review approve <id> --note "reworked email 2's opener"
openvz-leads review reject <id> --note "wrong read on their pain"
```

Or click through the dashboard's Review tab. Your note is stored with the decision.

### Export

Without an outbound channel, this is how the work leaves the tool:

```bash
openvz-leads export leads --format csv          # the list, for your CRM
openvz-leads export profiles --format markdown  # the briefs, to read
openvz-leads export emails --format markdown    # full sequences + recipients
openvz-leads export leads --format json         # for whatever's downstream
```

Files land in `data/exports/`. CSV is written with a UTF-8 BOM so Excel on Windows opens non-ASCII names correctly.

> `profiles` has no CSV form on purpose — a brief is nested (signals, decision chain, angles) and flattening it loses information. Use `markdown` to read it, `json` to process it.

---

## What a brief looks like

```markdown
## Northwind Logistics

**Lena Fischer** — Head of Procurement · lena@northwind.example

Fit **6/10** · confidence **low**

**What they do**
Regional freight forwarding across the Benelux

**Why they fit**
- Right title
- Size band matches

**Buying signals**
- Opened a second warehouse *(medium)* — homepage news item

**Decision chain**
- This contact: champion
- Likely economic buyer: Managing Director
- Likely blocker: IT

**Opening angles**
- Second warehouse means new carrier contracts
  - Why it lands: Timing

**Do not say**
- Do not claim they are struggling — no evidence of that

**Evidence gaps**
- No pricing or headcount data
- Website has no team page
```

Note the last two blocks. **"Do not say" and "evidence gaps" are the most valuable part of the brief** — they stop a rep asserting, in the first email, a detail the prospect can immediately tell was invented, which is the fastest way to kill a thread.

The brief's language is set by `profiling.output_language` (`"English"`, `"简体中文"`, `"日本語"`…), independent of the language the emails are written in.

---

## How it decides what to do next

No Claude call is spent on this — it's a deterministic rule derived from pipeline counts:

```
unhandled replies         → handle replies
approved campaigns + can send → send
unanalysed prospects      → profile
analysed prospects, no draft → write outreach
not enough prospects      → prospect
everything done           → run analytics
```

**Campaigns awaiting review are deliberately not an action** — they're waiting on a person, not on the agent.

## Architecture

```
openvz_leads/
├── main.py          heartbeat: quiet hours → budget → decide → act → log → sleep
├── brain.py         the one place a model is called (CLI / OpenAI / DeepSeek)
├── state.py         SQLite (WAL) with linear schema migrations
├── exporter.py      CSV / Markdown / JSON export
├── dashboard.py     local FastAPI dashboard (bilingual)
├── config.py        config loading + validation
├── trainer.py       learn the product from a website
├── icp.py           a sentence → an ICP, and what it inferred  ← new in this fork
├── pipeline.py      stage machine + stage history              ← new in this fork
├── outreach.py      merge variables and the footer; refuses on a gap ← new
│
├── agents/
│   ├── scout.py     prospecting (DuckDuckGo → Bing → Google → Serper)
│   ├── profiler.py  account analysis        ← new in this fork
│   ├── writer.py    outreach sequences
│   ├── sender.py    delivery (optional; approved campaigns only)
│   ├── handler.py   reply classification and response
│   └── analyst.py   pipeline analytics
│
├── integrations/
│   ├── crawler.py   tiered page reading: crawl4ai / basic / browser_use  ← new
│   ├── crm.py       stage changes, pushed to a CRM                       ← new
│   ├── gmail.py     sending from your own mailbox, and reading threads ← new
│   └── email_finder.py · instantly.py · linkedin.py · calendar.py
│
└── models/
    └── profile.py   the account-brief schema  ← new in this fork

prompts/    prompt templates — plain markdown, edit freely
skills/     sales knowledge — plain markdown, edit freely
```

Changing how it sells needs no code: `skills/` and `prompts/` are markdown.

## Configuration at a glance

The settings in `openvz-leads.yaml` worth knowing:

| Setting | Default | Notes |
|---|---|---|
| `review.require_approval` | `true` | Turn off and it sends automatically. Leave it on |
| `channels.email.provider` | `none` | `none` = draft-only; `gmail` = your own mailbox; `instantly` = a sending platform |
| `profiling.output_language` | `English` | Language the briefs are written in |
| `profiling.min_score` | `5` | Don't analyse below this score (saves Claude calls) |
| `profiling.max_per_cycle` | `5` | Analyses per heartbeat |
| `usage.max_daily_claude_percent` | `80` | Share of the daily quota it may use |
| `usage.quiet_hours` | 22:00–07:00 | When it stays idle |
| `model.provider` | `claude_cli` | What does the thinking. See below |
| `crawl.provider` | `auto` | How pages get read. See below |
| `crm.provider` | `none` | Where stage changes go. See below |

### Choosing a model

The default is the Claude Code CLI on your machine: no API key, no second bill, and the reason "no extra model cost" is a true sentence. The other providers exist for the cases that default cannot serve — a server with no interactive login, a team already buying OpenAI credit, somewhere the Claude CLI is not available.

```yaml
model:
  provider: "claude_cli"   # claude_cli | openai | deepseek | openai_compatible
  name: ""                 # blank = the provider's default model
  base_url: ""             # openai_compatible only
```

Keys live in `.env`: `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, or `MODEL_API_KEY` as a single override. `openai_compatible` covers anything speaking the same chat-completions API — vLLM, OpenRouter, a local Ollama. Every agent talks to the Brain and never to a provider, so this really is one line.

### How pages get read

Three tiers, cheapest first. `auto` uses the best one installed and only escalates when a page comes back blocked or empty.

| Tier | What it does | Install |
|---|---|---|
| `basic` | httpx + BeautifulSoup. Always available | included |
| `crawl4ai` | Renders JavaScript and returns **Markdown** — the model reads a document instead of de-tagged soup | `pip install "openvz-leads[crawl]"` |
| `browser_use` | An agent driving a real browser. Gets past consent walls and into pages that do not exist as URLs. Slow, and **needs an API key of its own** (it drives an API model, not the Claude CLI) | `pip install "openvz-leads[browser]"` |

Neither installed is a perfectly normal install — it is exactly what this did before the tiers existed. The desktop builds **deliberately exclude** both: each drags in a browser runtime, and multiplying the download for a tier most installs never reach is not a trade worth making.

The browser tier stays off until you set `crawl.browser_fallback: true`.

### Sending from your own Gmail

```yaml
channels:
  email:
    provider: "gmail"
    max_daily_sends: 20        # start low — see below
    gmail:
      read_scope: "metadata"   # metadata | readonly | none
      max_followups: 2
      min_followup_days: 2
      footer:
        postal_address: "Your real postal address"
```

Two steps: put `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` in `.env` (create an OAuth client of type *Desktop app* in your own Google Cloud project and enable the Gmail API on it), then `openvz-leads gmail login` to authorise an account in a browser. **No password passes through this tool** — only a refresh token is stored, locally, mode 0600.

Dropping the platform means four things become this product's job. Each one is a way to send a genuinely bad email:

| | What happens |
|---|---|
| **Scheduling** | One outbox row per step with a not-before time. Visible in `openvz-leads status` and the dashboard's send queue |
| **Merge variables** | `{{first_name}}` and friends are substituted by `outreach.py`. **A missing value stops the message** — `first_name` falls back to "there", but nothing makes "I saw 's site" into a sentence, so a missing `company` or `title` cancels that send with the reason recorded |
| **The footer** | An opt-out line and a postal address on every message. `postal_address` is empty on purpose and nothing sends until you fill it in — a placeholder would go out as a fake address in real mail |
| **Stop on reply** | Checked immediately before every follow-up. **If the mailbox cannot be read, the follow-up is deferred rather than sent** — not knowing whether they replied is not the same as knowing they did not |

`read_scope` decides what the mailbox may be read for:

- `metadata` (default) — headers only. Enough to see that they answered and stop the follow-ups, without this tool ever holding the text of anyone's reply.
- `readonly` — bodies too, so the Handler can classify intent and, if you turn it on, draft an answer. Broader than most prospecting needs, so you opt in.
- `none` — send only. Follow-ups could not be stopped, so **this configuration is rejected at startup** unless `max_followups` is 0.

Follow-ups are real threads: `In-Reply-To` points at the previous message, so they arrive as one conversation in the recipient's client rather than three unrelated cold emails.

**On Gmail, volume is the whole risk.** A platform warms domains and rotates inboxes; a personal mailbox does neither. A mailbox that sends fifty cold emails on its first day is a mailbox Google rate-limits — and the account it limits is the one you read your real mail in. Start `max_daily_sends` around ten or twenty and raise it over weeks, not days.

### Syncing to a CRM

What happens after a relationship exists — recording it, following up, moving it along — belongs in whatever system already holds your customers. So every stage change is an event, and every event is offered to it:

```yaml
crm:
  provider: "webhook"      # none | webhook | file
  webhook_url: "https://your-crm.example.com/hooks/leads"
```

The payload shape is stable and additive; it is documented in full at the top of `openvz_leads/integrations/crm.py`. A bearer token goes in `.env` as `CRM_WEBHOOK_TOKEN`, not in the YAML. `provider: file` appends to `data/crm-sync.jsonl` for a later import.

**A stage change is never lost to a sync failure.** The event is written locally and marked unsynced; a failed push leaves it that way and the next heartbeat retries. Only a 4xx — a receiver saying the request itself is wrong, which it will be next time too — marks the event permanently failed, so it stops blocking the queue behind it.

Events go out **in order**. A failed push replayed later would leave a CRM record reading "won" before "contacted", and order is the only information a stage history carries.

The database is `data/leads.db`, an ordinary SQLite file — open it with anything. To run several workspaces (different products, different ICPs), point `OPENVZ_LEADS_DB` at different paths.

## Always-on with Docker

```bash
claude login          # authenticate the CLI first
docker compose up -d
docker compose logs -f leads
```

---

## Legal & deliverability (read this before sending anything)

OpenVZ Leads automates outreach, but **you are the sender**. Cold email is legal in most places when done right and expensive when done wrong (CAN-SPAM penalties run to $53,088 *per email*). The compliant defaults are inherited from upstream — keep them.

### The law, in practice

**CAN-SPAM (US)** — every commercial email must have:
- A truthful subject line and accurate from-name/address (the copywriting rules enforce this — no fake "re:" threads, no impersonation)
- A working unsubscribe mechanism, honoured within 10 business days (any opt-out wording — "stop", "remove me", "unsubscribe" — is treated as immediate and permanent)
- Your valid physical mailing address in the footer — **who adds it depends on the channel**:
  - `provider: instantly` — the platform does. Enable it in the campaign settings before launching.
  - `provider: gmail` — **nothing else will.** Gmail is a mailbox, not a sending platform, so this product appends the footer itself from `channels.email.gmail.footer`, and refuses to send at all while `postal_address` is empty.

**GDPR / PECR (EU & UK)** — B2B cold email requires a defensible *legitimate interest*: the pitch must be genuinely relevant to the recipient's professional role. You must be able to say where you got their data, and objecting must be effortless. If you can't articulate why a specific person would care, don't email them — and the qualification rules say so.

**Bot disclosure** — some jurisdictions (e.g. California's B.O.T. Act) require disclosing automation in commercial communications. The agent is instructed to *always* answer truthfully if a prospect asks whether they're talking to an AI. Never configure it otherwise.

**LinkedIn** — browser automation violates LinkedIn's Terms of Service and can get the account restricted or banned. It is off by default; if you enable it, use conservative limits and an account you can afford to lose.

### Deliverability: warm up or burn out

Sending cold email from your main company domain, or at volume from day one, will land you in spam permanently. Before your first campaign:

1. **Buy a dedicated sending domain** (e.g. `getacme.com` instead of `acme.com`) so your primary domain's reputation is never at risk. Point it at your real site.
2. **Set up SPF, DKIM, and DMARC** on the sending domain. Without all three, Gmail and Outlook will junk you.
3. **Warm up for 2–4 weeks** before real volume. Most sending platforms have built-in warmup — turn it on and leave it on. **The Gmail path has no warm-up to turn on** — that is part of what you trade for it, and the only substitute is climbing more slowly.
4. **Ramp slowly**: start at 10–20 emails/day per inbox, add ~5/day. The default `max_daily_sends: 50` is a ceiling, not a starting point.
5. **Watch bounce and spam rates.** Bounce rate above ~3% or any spam complaints: pause, fix your list quality, ramp again. Emails are verified before sending to keep bounces low, but platform metrics are your ground truth.

None of this is legal advice — if you're sending at scale or into regulated industries, talk to a lawyer.

---

## Troubleshooting

**`command not found: openvz-leads`** — activate the venv first: `source .venv/bin/activate`

**`externally-managed-environment`** — use a venv, not system Python

**Claude headless mode fails** — you need `claude login` and an active subscription

**Search rate-limited** — it falls back to DuckDuckGo and Bing automatically. For reliability, add a Serper key

**It finds the wrong people** — tune `icp` in `openvz-leads.yaml`: industries, titles, company size, geography

**The emails aren't good** — edit `skills/email_frameworks.md` and `prompts/writer.md`, both plain markdown

**Analysis is too expensive** — raise `profiling.min_score` or lower `profiling.max_per_cycle`

---

## Licence & attribution

MIT.

This project is a branded derivative of [Harvey](https://github.com/ethanplusai/harvey) by Ethan Rogers (MIT). The original copyright notice is retained in [LICENSE](LICENSE); the full list of changes relative to upstream is in [NOTICE.md](NOTICE.md).
