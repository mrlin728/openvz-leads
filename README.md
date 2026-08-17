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

## 三件事

| | 做什么 | 谁在做 |
|---|---|---|
| **找客户** | 按你的 ICP 上网搜公司、爬官网、找出决策人、推断并验证邮箱。不依赖 Apollo / ZoomInfo | Scout |
| **分析客户** | 每个客户一份简报：他们是做什么的、为什么匹配（打分 + 理由）、采购信号、可能的痛点、决策链、破冰角度、以及**不要说什么** | Profiler |
| **写开发信** | 基于上面那份简报写 3 封序列，遵守成熟的冷邮件框架和一条严格的「不许编造」规则 | Writer |

然后它停下来。**默认不发信。**

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

### 跑起来

```bash
openvz-leads run          # 心跳循环：找 → 分析 → 写 → 排队等你审
openvz-leads dashboard    # 浏览器打开 http://localhost:5555
```

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
├── brain.py         claude -p 无头封装 + skills 注入
├── state.py         SQLite（WAL），带线性 schema 迁移
├── exporter.py      CSV / Markdown / JSON 导出
├── dashboard.py     本地 FastAPI 仪表盘（中英双语）
├── config.py        配置加载 + 校验
├── trainer.py       从官网学产品
│
├── agents/
│   ├── scout.py     找客户（DuckDuckGo → Bing → Google → Serper）
│   ├── profiler.py  客户分析          ← 本版新增
│   ├── writer.py    写开发信序列
│   ├── sender.py    投递（可选，只处理已批准的活动）
│   ├── handler.py   回复分类与应对
│   └── analyst.py   流水线报表
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
| `channels.email.provider` | `none` | `none` = 只起草不发送 |
| `profiling.output_language` | `English` | 客户分析用什么语言写 |
| `profiling.min_score` | `5` | 低于这个分的客户不做分析（省 Claude 调用） |
| `profiling.max_per_cycle` | `5` | 每轮最多分析几个 |
| `usage.max_daily_claude_percent` | `80` | 每天最多用掉多少配额 |
| `usage.quiet_hours` | 22:00–07:00 | 静默时段 |

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
- 页脚里你真实的实体邮寄地址——**投递前在发信平台的活动设置里打开这一项**

**GDPR / PECR（欧盟与英国）** — B2B 冷邮件需要站得住脚的「正当利益」：内容必须与收件人的职务真正相关。你要能说清数据从哪来，反对必须毫不费力。如果你说不出某个具体的人为什么会在意这件事，就不该给他发信——资格判定规则里也是这么写的。

**机器人披露** — 部分司法辖区（如加州 B.O.T. Act）要求在商业通信中披露自动化。本项目被指示：如果对方问「你是不是 AI」，**永远如实回答**。不要把它改成别的样子。

**LinkedIn** — 浏览器自动化违反 LinkedIn 服务条款，可能导致账号被限制或封禁。该功能默认关闭；要开就用保守的频率限制，和一个你输得起的账号。

### 送达率：要么养号，要么烧号

用你的主域名发冷邮件、或者第一天就上量，会让你永久进垃圾箱。第一封活动之前：

1. **买一个专用发信域名**（比如用 `getacme.com` 而不是 `acme.com`），让主域名的声誉永远不暴露在风险里。把它指向你真实的站点。
2. **给发信域名配好 SPF、DKIM、DMARC。**三个缺一个，Gmail 和 Outlook 就会把你扔进垃圾箱。
3. **养 2–4 周再上量。**主流发信平台都有内置 warmup，打开并一直开着。
4. **慢慢爬坡**：每个邮箱从每天 10–20 封开始，每天加 5 封左右。默认的 `max_daily_sends: 50` 是天花板，不是起点。
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
