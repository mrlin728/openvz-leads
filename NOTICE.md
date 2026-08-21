# NOTICE / 上游署名

**OpenVZ Leads** 是 [Harvey](https://github.com/ethanplusai/harvey) 的品牌化衍生版本。

- 上游项目：Harvey — Autonomous AI Sales Agent
- 上游作者：Ethan Rogers
- 上游许可：MIT License
- 上游仓库：https://github.com/ethanplusai/harvey

本项目在 MIT 许可下继续分发，原始版权声明保留在 [LICENSE](LICENSE) 中。

## 相对上游的主要改动

| 改动 | 说明 |
|------|------|
| 品牌与命名 | `harvey` → `openvz_leads`，CLI 命令 `openvz-leads`，配置 `openvz-leads.yaml`，数据库 `data/leads.db` |
| 发信解耦 | Instantly 从**必需**降级为**可选**。无任何发信通道时，从找客户到生成开发信的全流程仍可跑通 |
| 人工审核 | 新增 `review` 审核队列：草稿 → 待审核 → 已批准 → 发送。默认不自动发信 |
| 客户分析 | 新增 Profiler 子 agent，产出公司画像、ICP 匹配打分与理由、采购信号、决策链推断、破冰角度 |
| 导出 | 新增 `openvz-leads export`，支持 CSV / Markdown / JSON 导出客户名单、分析报告和开发信正文 |
| 双语 | 仪表盘与 CLI 支持中英文切换 |
| 自然语言定位 | 新增 `openvz_leads/icp.py` 与 `openvz-leads target`：一句话解析成结构化 ICP，并单独列出「替你填了什么」；新增 `icp.keywords` / `icp.exclusions`，让四个字段装不下的条件（「官网很旧」）既进搜索词也进分析判据。无模型时退回规则解析器 |
| 多模型 | `brain.py` 从「Claude CLI 的封装」改成「模型调用的唯一入口」：`claude_cli`（默认，无额外费用）/ `openai` / `deepseek` / 任意 OpenAI 兼容端点 |
| 分层抓取 | 新增 `integrations/crawler.py`：crawl4ai（渲染 JS，返回 Markdown）→ basic（httpx + BeautifulSoup）→ browser_use（真浏览器）。上面两层是可选依赖，都不装时行为与改造前完全一致 |
| 阶段机 | 新增 `pipeline.py` 与 `stage_events` 表：已找到 → 已起草 → 已触达 → 已回复 → 已约会面 → 赢单 / 丢单，带历史与终态规则。赢单只能由人标 |
| Gmail 发信 | 新增 `integrations/gmail.py` 与 `outbox` 表：用用户自己的邮箱发信、按线程做真正的跟进。发信平台原本负责的四件事全部自建 —— 排程、合并变量替换（缺值拒发而不是发出「Hi ,」）、退订页脚（`postal_address` 未填则一封不发）、以及每封跟进前的回复检查（读不到邮箱时延后而非发送） |
| CRM 同步 | 新增 `integrations/crm.py`：每次阶段变化按固定 payload 推给 webhook 或写入文件；失败重试、按顺序投递、4xx 才判永久失败 |

上游的合规默认值（硬性退订处理、发送上限、真实身份声明）全部保留。
