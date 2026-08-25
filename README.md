# OpenVZ Leads

**找得到 · 看得懂 · 写得出**

一个跑在本地的自主获客 agent：自己上网找到符合你 ICP 的客户，逐个做出可执行的客户分析，再据此写出开发信——然后停下来等你审核。

[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://www.python.org)
[![官网 OPENVZ AI](https://img.shields.io/badge/官网-www.openvzai.com-000000)](https://www.openvzai.com)

> English documentation: [README.en.md](README.en.md)
> 上游署名与改动清单：[NOTICE.md](NOTICE.md)

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

## 从一句话开始

```bash
openvz-leads target "帮我找美国牙科诊所"
openvz-leads target "Find dental clinics in California with outdated websites and 5-50 employees"
```

它把这句话解析成结构化 ICP，**并且把替你填的部分单独列出来**——你没说职位，它推断了三个；你没说规模，那就是任何规模都算。看过之后再决定要不要存。仪表盘的「定位」页是同一件事的图形版本。

「官网很旧」这类条件是这里的关键：它既不是行业也不是规模更不是地区，四个字段的解析会把它直接丢掉，然后返回一堆对自己官网很满意的诊所。它会进 `icp.keywords`，同时用来放宽搜索词、并作为**客户分析必须逐条核对的判据**（证实 / 证伪 / 无从判断）。

没有模型可用时（没装 Claude CLI、或配的 provider 没 key），会退回一套规则解析器：更粗糙，会明说自己是规则解析、置信度标 low，但输入框不会变成死的。

## 一条流水线

| | 做什么 | 谁在做 |
|---|---|---|
| **说清目标** | 一句自然语言 → ICP（行业 / 规模 / 地区 / 职位 / 附加条件），并列出替你填了什么 | `openvz-leads target` |
| **找客户** | 按 ICP 上网搜公司、读官网、找出决策人、推断并验证邮箱。不依赖 Apollo / ZoomInfo | Scout |
| **分析客户** | 每个客户一份简报：他们是做什么的、为什么匹配（打分 + 理由）、采购信号、可能的痛点、决策链、破冰角度、以及**不要说什么** | Profiler |
| **写开发信** | 基于上面那份简报写 3 封序列，遵守成熟的冷邮件框架和一条严格的「不许编造」规则 | Writer |
| **等你点头** | 活动进入审核队列。批准之前不会发出任何东西 | 你 |
| **跟进与回复** | 用你自己的 Gmail 发出去，按排程自动跟进；**对方一回复立刻停**，明确拒绝的判丢单 | Sender / Handler |
| **推进阶段** | 已触达 → 已回复 → 已约会面 → 赢单 / 丢单，每一步都记录，可同步到 CRM | `openvz-leads stage` |

然后它停下来。**默认不发信。**

**赢单只能由人来标。** 收件箱里没有任何东西能证明一单成了——一封热情的回信不是合同。同理，一个拒绝编造采购信号的产品，也不该允许自己编造一个成交。

## 为什么默认不发

原版 Harvey 是全自动闭环：写完直接投递。这个版本把发信拆成了可选插件，原因很实际——

- **不配任何发信通道也能用完整流程。**找客户 → 分析 → 写开发信 → 导出 CSV / Markdown，你拿去自己的邮箱或 CRM 发。不需要 Instantly 账号，不需要付费。
- **审核是默认开着的。**写好的活动进入审核队列，你在仪表盘或命令行批准之后才可能被发出去。没有「一觉醒来发现它给 200 个人发了胡编的东西」这种事。
- **分析是给人看的。**客户分析里每一条都标了证据来源和置信度，还专门列了「证据缺口」。它不假装自己什么都知道。

想让它自动发，配上 Instantly、把 `channels.email.provider` 改成 `instantly` 即可；即便如此，人工审核仍然默认开着，要关得自己去改 `review.require_approval`。

## 成本

大脑是 `claude -p` 无头调用，跑在你已有的 Claude 订阅上，**没有额外的 LLM 费用**。

唯一强烈建议加的是 Serper（约 $5 / 2500 次搜索，非订阅），没有它搜索会退化成爬 DuckDuckGo/Bing，容易被限流。其余所有 key 都是可选的。

---

## 快速开始

### 前置

- Python 3.11+
- [Claude Code CLI](https://claude.ai/download)，并且已经 `claude login`、订阅有效

### 装起来

```bash
git clone https://github.com/mrlin728/openvz-leads.git
cd openvz-leads
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
python -m playwright install chromium   # 可选，只有用 LinkedIn 才需要
```

### 配起来

两条路，选一条：

```bash
# A. 从你的官网自动学（推荐）——会生成 openvz-leads.yaml 和产品知识库
openvz-leads train https://your-company.com

# B. 交互式问答
openvz-leads setup
```

### 说清你要找谁

```bash
openvz-leads target "帮我找美国牙科诊所"
```

它会先把解析结果和替你填的东西打出来，你点头才写进 `openvz-leads.yaml`——而且只替换 `icp:` 这一段，文件其余部分连注释都原样保留。

### 跑起来

```bash
openvz-leads run          # 心跳循环：找 → 分析 → 写 → 排队等你审
openvz-leads dashboard    # 在浏览器打开 http://localhost:5555
                          # （装机版是双击图标，自带独立窗口，不走浏览器）
openvz-leads stage        # 看每个人现在在哪一步
openvz-leads gmail login  # 只有要用自己邮箱发信时才需要
openvz-leads gmail preview   # 打印下一封会发出去的信，不发
openvz-leads gmail test 你@自己的邮箱   # 把它真发给你自己看一眼
```

**第一次真发之前先跑 `gmail preview`。**它渲染的是队列里下一封真实的信，走的是发送时同一套函数 —— 所以它能证明四件否则只能拿陌生人来试的事：变量替换对了、页脚在且内容正确、账号授权好了、以及这封信看起来不像是机器漏了一格拼出来的。`gmail test` 再把同一封发给你自己，**只发给你在命令行里写的那个地址**。

---

## 日常怎么用

### 看它做了什么

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

### 审核开发信

```bash
openvz-leads review list                    # 列出待审核
openvz-leads review show <id>               # 看完整的三封邮件
openvz-leads review approve <id> --note "改了第二封的开头"
openvz-leads review reject <id> --note "痛点判断错了"
```

或者在仪表盘的「待审核」页里点。备注会随决定一起存进数据库。

### 导出

不接发信通道的话，这就是成果的出口：

```bash
openvz-leads export leads --format csv          # 客户名单，进 CRM 用
openvz-leads export profiles --format markdown  # 客户分析报告，给人读
openvz-leads export emails --format markdown    # 开发信全文 + 收件人
openvz-leads export leads --format json         # 给下游程序用
```

文件写到 `data/exports/`。CSV 用 UTF-8 BOM，Windows Excel 打开中文不乱码。

> `profiles` 不支持 CSV——客户分析是嵌套结构（信号、决策链、破冰角度），压平成表格会丢信息。用 `markdown` 读，用 `json` 处理。

---

## 一份客户分析长什么样

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

注意最后两块。**「不要说」和「证据缺口」是这份简报最有价值的部分**——它们防止销售在第一封信里说出一个客户一眼就能看穿是编的细节，那是最快毁掉一条线索的方式。

分析语言由 `profiling.output_language` 控制（`"简体中文"` / `"English"` / `"日本語"`…），和开发信本身的语言互不影响。

---

## 它是怎么决定下一步做什么的

不烧 Claude 调用来做这个决策——纯规则，从流水线计数直接推出来：

```
有未处理回复        → 处理回复
有已批准的活动 + 能发 → 发送
有未分析的客户       → 分析
有已分析待写的客户    → 写开发信
客户不够            → 找客户
都齐了              → 跑分析报表
```

**待审核的活动不构成一个动作**——它在等人，不在等 agent。

## 架构

```
openvz_leads/
├── main.py          心跳循环：静默时段 → 预算 → 决策 → 执行 → 记录 → 睡
├── brain.py         模型调用的唯一入口（CLI / OpenAI / DeepSeek）+ skills 注入
├── state.py         SQLite（WAL），带线性 schema 迁移
├── exporter.py      CSV / Markdown / JSON 导出
├── dashboard.py     本地 FastAPI 仪表盘（中英双语）
├── config.py        配置加载 + 校验
├── trainer.py       从官网学产品
├── icp.py           一句话 → ICP，并说清替你填了什么   ← 本版新增
├── pipeline.py      阶段机 + 阶段历史                  ← 本版新增
├── outreach.py      合并变量替换与页脚，缺值就拒发      ← 本版新增
│
├── agents/
│   ├── scout.py     找客户（DuckDuckGo → Bing → Google → Serper）
│   ├── profiler.py  客户分析          ← 本版新增
│   ├── writer.py    写开发信序列
│   ├── sender.py    投递（可选，只处理已批准的活动）
│   ├── handler.py   回复分类与应对
│   └── analyst.py   流水线报表
│
├── integrations/
│   ├── crawler.py   分层读网页：crawl4ai / basic / browser_use  ← 本版新增
│   ├── crm.py       阶段变化推给 CRM                            ← 本版新增
│   ├── gmail.py     从你自己的邮箱发信、按线程查回复    ← 本版新增
│   └── email_finder.py · instantly.py · linkedin.py · calendar.py
│
└── models/
    └── profile.py   客户分析的数据结构  ← 本版新增

prompts/    提示词模板，纯 markdown，直接改
skills/     销售知识库，纯 markdown，直接改
```

改它怎么卖，不用改代码：`skills/` 和 `prompts/` 都是 markdown。

## 配置速查

`openvz-leads.yaml` 里最该关注的几项：

| 配置 | 默认 | 说明 |
|---|---|---|
| `review.require_approval` | `true` | 关掉才会自动发。建议一直开着 |
| `channels.email.provider` | `none` | `none` = 只起草不发送；`gmail` = 用你自己的邮箱发；`instantly` = 用发信平台 |
| `profiling.output_language` | `English` | 客户分析用什么语言写 |
| `profiling.min_score` | `5` | 低于这个分的客户不做分析（省 Claude 调用） |
| `profiling.max_per_cycle` | `5` | 每轮最多分析几个 |
| `usage.max_daily_claude_percent` | `80` | 每天最多用掉多少配额 |
| `usage.quiet_hours` | 22:00–07:00 | 静默时段 |
| `model.provider` | `claude_cli` | 谁在思考。见下 |
| `crawl.provider` | `auto` | 怎么读网页。见下 |
| `crm.provider` | `none` | 阶段变化推给谁。见下 |

### 换个模型

默认是本机的 Claude Code CLI：不需要 API key，没有第二份账单，这也是「没有额外模型费用」这句话成立的原因。其余三个 provider 是给那个默认服务不了的场合准备的——没有交互式登录的服务器、已经在买 OpenAI 额度的团队、拿不到 Claude CLI 的环境。

```yaml
model:
  provider: "claude_cli"   # claude_cli | openai | deepseek | openai_compatible
  name: ""                 # 留空 = 该 provider 的默认模型
  base_url: ""             # 只有 openai_compatible 会读
```

key 放 `.env`：`OPENAI_API_KEY` / `DEEPSEEK_API_KEY`，或者用 `MODEL_API_KEY` 统一覆盖。`openai_compatible` 用来接任何说同一套 chat-completions 协议的东西——vLLM、OpenRouter、本地 Ollama。所有 agent 只跟 Brain 说话、从不直接碰 provider，所以这真的就是改一行。

### 怎么读网页

三层，从便宜到贵。`auto` 会挑装了的里面最好的那个，只有拿回来是空的或者被拦了才往下走。

| 层 | 能力 | 装法 |
|---|---|---|
| `basic` | httpx + BeautifulSoup。永远可用 | 自带 |
| `crawl4ai` | 渲染 JavaScript，返回 **Markdown**——模型读到的是文档，而不是脱掉标签的糊字 | `pip install "openvz-leads[crawl]"` |
| `browser_use` | 一个真的开浏览器的 agent。能过同意墙、能点进不存在于 URL 的页面。慢，而且**要它自己的 API key**（它驱动的是 API 模型，不是 Claude CLI） | `pip install "openvz-leads[browser]"` |

两个都没装也完全正常——那就是这套东西存在之前的行为。桌面安装包**刻意不打包**这两个：它们各自会拖进一整套浏览器运行时，为一个多数人用不到的层把下载体积翻几倍不值得。

浏览器层默认关着，要开得写 `crawl.browser_fallback: true`。

### 用你自己的 Gmail 发信

```yaml
channels:
  email:
    provider: "gmail"
    max_daily_sends: 20        # 从低往上加，见下
    gmail:
      read_scope: "metadata"   # metadata | readonly | none
      max_followups: 2
      min_followup_days: 2
      footer:
        postal_address: "你的真实通信地址"
```

两步：`.env` 里放 `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`（在你自己的 Google Cloud 项目里建一个「桌面应用」类型的 OAuth 客户端，并启用 Gmail API），然后 `openvz-leads gmail login` 在浏览器里授权账号。**密码不经过这个工具**，本机只存一个 refresh token，权限 0600。

换掉发信平台意味着这四件事从此归它自己管，每一件漏掉都会真的发出坏邮件：

| | 谁在做 |
|---|---|
| **排程** | 每一步一行发件箱记录，带「不早于」时间。`openvz-leads status` 和仪表盘的「发送队列」都能看到 |
| **合并变量** | `{{first_name}}` 之类由 `outreach.py` 替换。**没值就不发** —— first_name 兜底成 "there"，但 company 和 title 没有能让句子成立的兜底，缺了就作废这封并写明原因 |
| **退订页脚** | 每封信自动附上退订说明和通信地址。`postal_address` **故意留空**，填之前一封都发不出去 —— 占位地址会作为假地址进真邮件，比不发更糟 |
| **回复即停** | 每封跟进发出前查一次线程。**读不到邮箱时是延后而不是发** —— 「不知道对方是否回了」和「知道对方没回」不是一回事 |

`read_scope` 决定这个邮箱被允许读到什么：

- `metadata`（默认）—— 只读信头。够用来发现「他回了」并停掉跟进，而这个工具从头到尾不持有任何人回信的正文。
- `readonly` —— 连正文一起读，Handler 才能判断意图、才谈得上自动回复。范围比多数获客场景需要的宽，所以要你主动选。
- `none` —— 只发不读。跟进将无法停止，所以**启动时直接拒绝这个配置**，除非你把 `max_followups` 设成 0。

跟进是真线程：`In-Reply-To` 指向上一封，所以在对方的邮件客户端里是同一个会话，而不是三封互不相干的陌生来信。

**Gmail 上真正的风险是量。** 发信平台会养域名、轮换发件箱，个人邮箱两样都没有。一个第一天就发五十封冷邮件的邮箱，是会被 Google 限制的邮箱 —— 而被限制的那个，正是你读日常邮件的那个。`max_daily_sends` 从十几开始，用几周而不是几天往上加。

### 同步到 CRM

关系建立之后的事——记录、跟进、推进——本来就该在已经存着你客户的那套系统里。所以每一次阶段变化都是一个事件，都会推过去：

```yaml
crm:
  provider: "webhook"      # none | webhook | file
  webhook_url: "https://your-crm.example.com/hooks/leads"
```

payload 形状是固定的（只做增量改动），完整定义在 `openvz_leads/integrations/crm.py` 的模块文档里。bearer token 放 `.env` 的 `CRM_WEBHOOK_TOKEN`，不要写进 YAML。`provider: file` 会往 `data/crm-sync.jsonl` 追加，留着以后导入。

**阶段变化不会因为同步失败而丢。** 事件先落本地、标记未同步；推送失败就留在那儿，下一轮心跳重试。只有 4xx（对方说「你这个请求本身就不对」，下次也一样不对）才标记为永久失败，免得它一直堵住后面排队的事件。

事件是**按顺序**发的：一次失败被重放到后面去，会让 CRM 里的记录读起来像「先赢单后触达」。顺序是一段阶段历史里唯一的信息量。

数据库在 `data/leads.db`，普通 SQLite 文件，随便用什么工具打开看。想跑多个工作区（不同产品、不同 ICP），设 `OPENVZ_LEADS_DB` 环境变量指到不同路径即可。

## Docker 常驻

```bash
claude login          # 先确保 CLI 已认证
docker compose up -d
docker compose logs -f leads
```

---

## 合规与送达率（发信之前务必读）

OpenVZ Leads 帮你自动化外联，但**发件人是你**。冷邮件在多数地区合规操作是合法的，操作不当代价高昂（CAN-SPAM 罚则可达每封邮件 $53,088）。本项目保留了上游的合规默认值——别关掉它们。

### 法律要点

**CAN-SPAM（美国）** — 每封商业邮件必须有：
- 真实的主题行和准确的发件人名称/地址（写信规则里强制了这一点——不许伪造 "re:" 线程，不许冒充他人）
- 可用的退订方式，10 个工作日内处理（任何退订措辞——"stop"、"remove me"、"unsubscribe"——都会被立即且永久地执行）
- 页脚里你真实的实体邮寄地址 —— **谁来加这个页脚取决于你走哪条路**：
  - `provider: instantly` —— 平台加。投递前在发信平台的活动设置里打开这一项。
  - `provider: gmail` —— **没有别人会加**。Gmail 是邮箱不是发信平台，所以这个页脚由本项目自己附在每封信末尾，内容来自 `channels.email.gmail.footer`；`postal_address` 没填之前，一封都发不出去。

**GDPR / PECR（欧盟与英国）** — B2B 冷邮件需要站得住脚的「正当利益」：内容必须与收件人的职务真正相关。你要能说清数据从哪来，反对必须毫不费力。如果你说不出某个具体的人为什么会在意这件事，就不该给他发信——资格判定规则里也是这么写的。

**机器人披露** — 部分司法辖区（如加州 B.O.T. Act）要求在商业通信中披露自动化。本项目被指示：如果对方问「你是不是 AI」，**永远如实回答**。不要把它改成别的样子。

**LinkedIn** — 浏览器自动化违反 LinkedIn 服务条款，可能导致账号被限制或封禁。该功能默认关闭；要开就用保守的频率限制，和一个你输得起的账号。

### 送达率：要么养号，要么烧号

用你的主域名发冷邮件、或者第一天就上量，会让你永久进垃圾箱。第一封活动之前：

1. **买一个专用发信域名**（比如用 `getacme.com` 而不是 `acme.com`），让主域名的声誉永远不暴露在风险里。把它指向你真实的站点。
2. **给发信域名配好 SPF、DKIM、DMARC。**三个缺一个，Gmail 和 Outlook 就会把你扔进垃圾箱。
3. **养 2–4 周再上量。**主流发信平台都有内置 warmup，打开并一直开着。**Gmail 路径没有 warmup 可开** —— 这是选它要付的代价，只能靠爬坡慢来代替。
4. **慢慢爬坡**：每个邮箱从每天 10–20 封开始，每天加 5 封左右。默认的 `max_daily_sends: 50` 是天花板，不是起点。走 Gmail 的话把它调到 10–20 再开始 —— 被限制的是你读日常邮件的那个邮箱。
5. **盯住退信率和投诉率。**退信超过 3% 或出现任何垃圾投诉：暂停，清理名单质量，重新爬坡。发信前会验证邮箱来压低退信，但平台指标才是最终依据。

以上都不构成法律意见。如果你要大规模发送或面向受监管行业，请咨询律师。

---

## 常见问题

**`command not found: openvz-leads`** — 先激活虚拟环境：`source .venv/bin/activate`

**`externally-managed-environment`** — 用虚拟环境，别用系统 Python

**Claude 无头模式失败** — 需要 `claude login`，并且订阅有效

**搜索被限流** — 会自动退回 DuckDuckGo 和 Bing。想稳定，加个 Serper key

**它找的人不对** — 改 `openvz-leads.yaml` 里的 `icp`：行业、职位、公司规模、地区

**开发信写得不好** — 改 `skills/email_frameworks.md` 和 `prompts/writer.md`，都是纯 markdown

**分析太贵了** — 调高 `profiling.min_score`，或者调低 `profiling.max_per_cycle`

---

## 许可与署名

MIT。

本项目是 [Harvey](https://github.com/ethanplusai/harvey)（作者 Ethan Rogers，MIT）的品牌化衍生版本。原始版权声明保留在 [LICENSE](LICENSE) 中，相对上游的完整改动清单见 [NOTICE.md](NOTICE.md)。
