# codereview-ai

自托管、开源的 AI 代码审查平台。Webhook 自动审查 MR/PR，**行级 inline 评论**，
LiteLLM 多模型接入，钉钉/飞书/企业微信推送，Vue 管理后台。

> **当前状态**：设计稿完成，尚未开始编码。本项目从
> [AI-Codereview-Gitlab](https://github.com/sunmh207/AI-Codereview-Gitlab)
> 的架构教训出发，用可长期演进的工程质量重做。
> 技术栈：**Vue 3 + FastAPI**。

## 文档

| 文档 | 说明 |
|---|---|
| [`docs/PRD.md`](docs/PRD.md) | 需求文档：功能清单、优先级、MVP 范围、验收标准 |
| [`docs/DESIGN.md`](docs/DESIGN.md) | **详细设计**：架构、领域模型、DB、审查引擎、队列、沙箱、部署、里程碑 |
| [`reference/README.md`](reference/README.md) | 可复用素材索引 |
| [`reference/platform_payload_map.md`](reference/platform_payload_map.md) | 各平台 webhook 字段/API/行级评论构造 —— 最有复用价值的资料 |
| [`reference/antipatterns.md`](reference/antipatterns.md) | 旧项目踩过的坑（带实测证据），开发与 review 时逐条对照 |
| [`reference/tokens.py`](reference/tokens.py) | 可直接复用的 token 计数/截断片段 |
| [`reference/prompt_templates.yml`](reference/prompt_templates.yml) | 结构化输出的 prompt 模板（含锚定定位、增量审查、injection 防御） |
| [`reference/im_payloads.md`](reference/im_payloads.md) | 钉钉/飞书/企微协议 |
| [`reference/ocr_notes.md`](reference/ocr_notes.md) | **阿里 OpenCodeReview 详细拆解 + 复用决策**（锚定定位/语义分组/内存压缩/预算闸门/工具集/规则引擎） |
| [`reference/pr_agent_notes.md`](reference/pr_agent_notes.md) | **Qodo PR-Agent 详细拆解 + 复用决策**（LLM 输出修复链/diff 预算/内容指纹去重/finding 状态机/配置白名单/双平台行级评论机制） |
| [`reference/diff_anchor.py`](reference/diff_anchor.py) | **确定性行级定位**：existing_code 片段 → 纯字符串匹配出真实行号（可直接落成 `review/location.py`） |

## 核心特性（v0.1）

- 🔒 Webhook 签名校验（GitHub/Gitea 用 HMAC，GitLab 用 secret-token），`WEBHOOK_SECRET` 与平台 token 分离
- 📝 **行级 inline 评论**（GitLab position / GitHub 批量 review）+ existing_code 锚定定位（工程匹配行号，非 LLM 拍号）
- 🧩 **增量审查**：追加 commit 只审增量，不重复已提问题
- 🎯 **语义分组 + 内存压缩**：LLM 按关联文件打包、会话超 60%/80% token 阈值自动压缩（借鉴阿里 OpenCodeReview）
- 📊 **结构化输出**：JSON schema 约束评分与问题，告别正则捞分
- 🛡️ 静态分析融合（stretch）：ruff/eslint 接管确定性问题
- 🤖 Agentic 审查（stretch）：**无 shell 的**容器沙箱探索
- 🔀 LiteLLM 统一多模型 + 重试 + 成本归因
- 💬 钉钉/飞书/企微推送，评分低于阈值 @ 提交者
- 🖥️ Vue 管理后台：审查记录、项目/模型/IM 配置、统计看板
- 💾 SQLite / PostgreSQL 双档切换；任务持久化不丢

## 快速启动

```bash
# simple 档（单容器，SQLite + 进程内队列）
export CR_SECRET_KEY=$(python -c 'import secrets;print(secrets.token_urlsafe(48))')
export CR_WEBHOOK_SECRET=$(python -c 'import secrets;print(secrets.token_urlsafe(48))')
export CR_ENCRYPTION_KEY=$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
docker compose up -d
```

未实现完成，以上为目标形态。