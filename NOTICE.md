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

上游的合规默认值（硬性退订处理、发送上限、真实身份声明）全部保留。
