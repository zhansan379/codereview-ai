# codereview-ai

自托管、开源的 AI 代码审查平台。Webhook 自动审查 MR/PR，**行级 inline 评论**，
LiteLLM 多模型接入，钉钉/飞书/企业微信推送，Vue 管理后台。

> **当前状态**：**v0.1.0 已发布**（tag `v0.1.0`）。PRD §6 验收标准 1-13 全部离线通过，
> Docker / CI / 覆盖率门 / 依赖扫描齐备，详见 [`docs/M6_SMOKE.md`](docs/M6_SMOKE.md)。
> 技术栈：**FastAPI + Vue 3**；原生 GitLab / GitHub 适配器。
>
> 本项目从 [AI-Codereview-Gitlab](https://github.com/sunmh207/AI-Codereview-Gitlab) 的
> 架构教训出发，用可长期演进的工程质量重做（踩坑清单见
> [`reference/antipatterns.md`](reference/antipatterns.md)）。

## 文档

| 文档 | 说明 |
|---|---|
| [`docs/PRD.md`](docs/PRD.md) | 需求文档：功能清单、优先级、MVP 范围、验收标准 |
| [`docs/DESIGN.md`](docs/DESIGN.md) | **详细设计**：架构、领域模型、DB、审查引擎、队列、沙箱、部署、里程碑 |
| [`docs/M6_SMOKE.md`](docs/M6_SMOKE.md) | **M6 冒烟清单 + 反模式 A1-C5 核查表**（标准 1-13 验收凭据） |
| [`reference/README.md`](reference/README.md) | 可复用素材索引 |
| [`reference/platform_payload_map.md`](reference/platform_payload_map.md) | 各平台 webhook 字段/API/行级评论构造 —— 最有复用价值的资料 |
| [`reference/antipatterns.md`](reference/antipatterns.md) | 旧项目踩过的坑（带实测证据），开发与 review 时逐条对照 |
| [`reference/tokens.py`](reference/tokens.py) | 可直接复用的 token 计数/截断片段 |
| [`reference/prompt_templates.yml`](reference/prompt_templates.yml) | 结构化输出的 prompt 模板（含锚定定位、增量审查、injection 防御） |
| [`reference/im_payloads.md`](reference/im_payloads.md) | 钉钉/飞书/企微协议 |
| [`reference/ocr_notes.md`](reference/ocr_notes.md) | **阿里 OpenCodeReview 详细拆解 + 复用决策**（锚定定位/语义分组/内存压缩/预算闸门/工具集/规则引擎） |
| [`reference/pr_agent_notes.md`](reference/pr_agent_notes.md) | **Qodo PR-Agent 详细拆解 + 复用决策**（LLM 输出修复链/diff 预算/内容指纹去重/finding 状态机/配置白名单/双平台行级评论机制） |
| [`reference/diff_anchor.py`](reference/diff_anchor.py) | **确定性行级定位**：existing_code 片段 → 纯字符串匹配出真实行号（`review/location.py`） |

## 核心特性（已实现）

- 🔒 **Webhook 签名校验**：GitHub/GitLab 双路径（HMAC / secret-token），统一 `hmac.compare_digest` 安全比对
- 📝 **行级 inline 评论**：GitLab position / GitHub 单次批量 review；existing_code 锚定定位（工程匹配行号，非 LLM 拍号）
- 🧩 **增量审查**：追加 commit 只审增量 diff，内容指纹去重不重复已提问题
- 🔁 **幂等 + 崩溃恢复**：同 MR 重复推送只审一次；任务滞留可 `recover_stale` 回收续跑
- 📊 **结构化输出**：JSON schema 约束评分与问题（`ReviewScores`），告别正则捞分
- 🛡️ **静态分析融合**：ruff 等接管确定性问题的静态检查层
- 🤖 **Agentic 审查**：**无 shell 的**容器沙箱探索（零执行攻击面）
- 🔀 **LiteLLM 统一多模型** + 失败 → 任务 `failed` 且不留误导性审计行
- 💬 **钉钉/飞书/企微推送**，评分低于阈值 @ 提交者
- 🔐 **密钥设计**：4 枚必配置密钥（`CR_*`）经 Fernet 加密落库、日志全量脱敏、缺密钥拒绝启动
- 🖥️ **Vue 管理后台**：审查记录、项目/模型/IM 配置、统计看板
- 💾 **SQLite / PostgreSQL 双档**；任务持久化不丢

## 快速启动（Docker）

前置：`docker`；GitHub token 需 `workflow` scope（首推含 CI 工作流）。

```bash
# 四枚必配密钥（缺失即拒绝启动；生成命令见下文）
export CR_SECRET_KEY=$(python -c 'import secrets;print(secrets.token_urlsafe(48))')
export CR_WEBHOOK_SECRET=$(python -c 'import secrets;print(secrets.token_urlsafe(48))')
export CR_ENCRYPTION_KEY=$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
export CR_ADMIN_PASSWORD=$(python -c 'import secrets;print(secrets.token_urlsafe(24))')

# 平台 token / 模型（可选；配齐才启动内置 worker）
export CR_GITLAB_URL= CR_GITLAB_TOKEN= CR_GITHUB_URL= CR_GITHUB_TOKEN=
export CR_LLM_MODEL=

docker compose up -d
curl http://localhost:5001/health   # → 200 即就绪
```

密钥可复用生成命令：`python -c 'import secrets;print(secrets.token_urlsafe(48))'`（SECRET_KEY/WEBHOOK_SECRET）、
`python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())'`（ENCRYPTION_KEY）。

### 这 4 枚密钥是啥？（大白话）

它们**不是**哪家平台（GitLab/GitHub/AI 服务商）给你的密码，而是**本系统自己家门的三把锁加一把钥匙**，都是你自己生成的随机串。缺了系统直接不启动（fail-fast），宁可不开机也不带病运行。

| 密钥 | 打个比方 | 真正干啥 |
|---|---|---|
| `CR_SECRET_KEY` | **门锁** | 给 webhook 和登录做签名、生成后台会话凭证 |
| `CR_WEBHOOK_SECRET` | **验门铃** | 收到 webhook 事件先验真伪：GitLab 当 `Secret token`、GitHub 做 HMAC 指纹。**伪造签名 → 401 直接拒** |
| `CR_ENCRYPTION_KEY` | **保险柜钥匙** | 把存进数据库的 api_key / IM token 用 Fernet 加密，平时读出来全是 `******` |
| `CR_ADMIN_PASSWORD` | **后台开门密码** | 管理员登录 Vue 后台用 |

> ⚠️ **`CR_ENCRYPTION_KEY` 最特殊**：必须是 `Fernet.generate_key()` 生成的 **base64 串**，
> 不能用 `token_urlsafe` 的串，否则 `Fernet(key)` 会校验失败拒绝启动。

> 顺手提醒：改了 `CR_WEBHOOK_SECRET`，记得去 GitLab/GitHub 那边把 webhook 的密钥也改成一样，
> 两边对不上就 401 收不到事件。

## 开发 / 验证（离线）

```bash
uv sync
uv run ruff check src tests                # lint
uv run mypy src                            # 类型
uv run pytest tests -q                     # 全量测试
uv run pytest tests \
  --cov=codereview_ai.forges --cov=codereview_ai.review \
  --cov=codereview_ai.worker --cov=codereview_ai.queue \
  --cov=codereview_ai.storage --cov-fail-under=83    # 核心覆盖率门
uv run pip-audit                           # 依赖漏洞扫描
```

- 全程离线可测：临时 SQLite / `httpx.MockTransport` / fake LLM 注入，不碰真实网络。
- PRD 标准 1-13 的离线验收测试在 [`tests/acceptance/`](tests/acceptance/)，映射关系见
  [`docs/M6_SMOKE.md`](docs/M6_SMOKE.md) §1。
- 当前覆盖率（M6 收口）：总 **82.9%**（门 70%）、核心 **84.0%**（门 83%），293 tests 全绿。

## 目录结构

```
src/codereview_ai/
  api/        FastAPI 路由（webhook / admin / auth / health）
  forges/     平台适配器（GitLab / GitHub）+ 签名校验
  review/     LLM 审查引擎（diff / location / increments / static_analysis / agentic）
  queue/      进程内任务队列 + worker（状态机 / 崩溃恢复）
  storage/    SQLite / PostgreSQL 存储（幂等唯一索引）
  notifiers/  钉钉 / 飞书 / 企微推送
  config/     密钥与环境配置（`CR_*` fail-fast）
  crypto/     Fernet 加密 / 脱敏
docs/         PRD / DESIGN / M6 冒烟清单
tests/        unit / acceptance
```