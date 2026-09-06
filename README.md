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

### ① 通用：先导出 4 枚必配密钥并起服务

前置：`docker`；GitHub token 需 `workflow` scope（首推含 CI 工作流）。

```bash
# —— 生成 4 枚密钥：先生成存进变量 → export 给程序 → echo 打印，三行一步到位。
#    打印出的那一份(echo 输出的)和 export 用的是同一个值，改密钥就抄打印出来的。——
S=$(python -c 'import secrets;print(secrets.token_urlsafe(48))')   && export CR_SECRET_KEY=$S     && echo "CR_SECRET_KEY     = $S"
W=$(python -c 'import secrets;print(secrets.token_urlsafe(48))')   && export CR_WEBHOOK_SECRET=$W && echo "CR_WEBHOOK_SECRET = $W   ← 填到 GitLab/GitHub webhook（必须与系统一致）"
E=$(python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())') && export CR_ENCRYPTION_KEY=$E && echo "CR_ENCRYPTION_KEY = $E"
P=$(python -c 'import secrets;print(secrets.token_urlsafe(24))')   && export CR_ADMIN_PASSWORD=$P && echo "CR_ADMIN_PASSWORD = $P   ← 登录后台用，建议改成你能记住的"

# 关终端就丢；想重启还在，存进 .env（compose 会自动读）或用 setx
docker compose up -d
curl http://localhost:5001/health   # → 200 即就绪
```

> 💻 **Windows / PowerShell 用户看这里**：上面的 `export` 是 bash 语法，PowerShell 和 cmd 不认。
> PowerShell 用 `$env:VAR=`，测健康用 `curl.exe`（PowerShell 里 `curl` 是别的命令别名）：
>
> ```powershell
> # 生成＋打印一步到位：打印出来的就是实际生效的密钥，关终端就丢
> $env:CR_SECRET_KEY=(python -c 'import secrets;print(secrets.token_urlsafe(48))'); $env:CR_SECRET_KEY
> $env:CR_WEBHOOK_SECRET=(python -c 'import secrets;print(secrets.token_urlsafe(48))'); $env:CR_WEBHOOK_SECRET
> $env:CR_ENCRYPTION_KEY=(python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())'); $env:CR_ENCRYPTION_KEY
> $env:CR_ADMIN_PASSWORD=(python -c 'import secrets;print(secrets.token_urlsafe(24))'); $env:CR_ADMIN_PASSWORD
>
> docker compose up -d
> curl.exe http://localhost:5001/health
> ```
>
> 只想临时用用就 `$env:VAR = ...` 够了（关了终端就没了）；想**永久生效**（重启还在）再补一句
> `setx CR_SECRET_KEY "<值>"`（会写入用户级环境变量）。装了 **Git Bash** 的同学可直接照抄上面的 `export`。

### ② 接入 GitLab

```bash
# 平台信息（没配这些，平台推 MR 时 worker 不会真审）
export CR_GITLAB_URL=http://your-gitlab     # 自建 GitLab 根地址（如 http://gitlab.example.com）
export CR_GITLAB_TOKEN=glpat-xxxx           # GitLab Personal Access Token（api 权限）
export CR_LLM_MODEL=anthropic/claude-...    # 你要用的 LLM 模型

docker compose up -d

# 注：LOCAL_URL 改成公网可达地址（本地调试可先用临时内网穿透，如 ngrok http 5001）
LOCAL_URL=http://<你的公网地址>:5001/webhook
curl -X POST "$CR_GITLAB_URL/api/v4/projects/<project_id>/hooks" \
  -H "PRIVATE-TOKEN: $CR_GITLAB_TOKEN" \
  --data-urlencode "url=$LOCAL_URL" \
  --data-urlencode "token=$CR_WEBHOOK_SECRET" \
  --data-urlencode "merge_requests_events=true" \
  --data-urlencode "push_events=true"
# 返回体里的 "id" 即该 webhook 的 id，可用来删/查/重测
```

### ③ 接入 GitHub

```bash
# 平台信息（token 用 PAT，需 repo + admin:repo_hook 权限）
export CR_GITHUB_URL=https://api.github.com   # GitHub Enterprise 换成你的实例
export CR_GITHUB_TOKEN=ghp_xxxx              # GitHub PAT
export CR_LLM_MODEL=anthropic/claude-...      # 你要用的 LLM 模型

docker compose up -d

# 注：LOCAL_URL 改成公网可达地址（本地调试可先用临时内网穿透，如 ngrok http 5001）
LOCAL_URL=http://<你的公网地址>:5001/webhook
curl -X POST "https://api.github.com/repos/<owner>/<repo>/hooks" \
  -H "Authorization: Bearer $CR_GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  -d "{\"config\":{\"url\":\"$LOCAL_URL\",\"content_type\":\"json\",\"secret\":\"$CR_WEBHOOK_SECRET\"},\"events\":[\"pull_request\",\"push\"],\"active\":true}"
```

> webhook 的 `token`/`secret` 都源自 `CR_WEBHOOK_SECRET`，**务必和上面⓶③里保持一致**，对不上就 401 收不到事件。
> `CR_WEBHOOK_SECRET`、`CR_GITLAB_TOKEN`、`CR_GITHUB_TOKEN`、`CR_LLM_MODEL` 这几枚是**可选**的：
> 不配也能起服务（后台、看板可用），但只有配齐了，平台推 M/R PR 时内置 worker 才会真跑审查并回写评论。

### ④ 本地跑（uv，免 Docker）

不想装 Docker 就用本地 uv：写密钥进 `.env`（一次性打印 + 永久保存，本地与 compose 都自动读），再 `uvicorn` 起服务。

先在仓库根目录跑这个，生成 4 枚密钥写进 `.env` 并**当场打印**（`CR_WEBHOOK_SECRET` 抄到平台用）：

```bash
python - <<'EOF'
import secrets
from cryptography.fernet import Fernet
keys = [
    ("CR_SECRET_KEY",     secrets.token_urlsafe(48)),
    ("CR_WEBHOOK_SECRET", secrets.token_urlsafe(48)),
    ("CR_ENCRYPTION_KEY", Fernet.generate_key().decode()),
    ("CR_ADMIN_PASSWORD", secrets.token_urlsafe(24)),
]
with open(".env", "w") as f:
    for k, v in keys:
        f.write(f"{k}={v}\n")
        print(f"{k} = {v}")     # ← 当场打印，凭这份抄 webhook 密钥
EOF
```

然后启动（首次先 `uv sync` 装依赖）：

```bash
uv sync
uv run uvicorn codereview_ai.main:app --host 0.0.0.0 --port 5001
# 另开终端验证：
curl http://localhost:5001/health   # → 200 即就绪
```

想真审平台 MR，启动前在 `.env` 末尾追加上面②③里的平台信息（`CR_GITLAB_URL/CR_GITLAB_TOKEN` 或 `CR_GITHUB_URL/CR_GITHUB_TOKEN` + `CR_LLM_MODEL`），uvicorn 启动时才会起内置 worker。

> 本地跑没容器那层；webhook 回调仍要公网可达才能被 GitLab/GitHub 连上（本地调试可 `ngrok http 5001`）。
> `.env` 会在 `.gitignore` 里，不进仓库；缺密钥直接 `uvicorn` 会 fail-fast 退出并打印缺哪枚。

## 密钥说明（大白话）

它们**不是**哪家平台（GitLab/GitHub/AI 服务商）给你的密码，而是**本系统自己家门的三把锁加一把钥匙**，都是你自己生成的随机串。缺了系统直接不启动（fail-fast），宁可不开机也不带病运行。

| 密钥 | 打个比方 | 真正干啥 |
|---|---|---|
| `CR_SECRET_KEY` | **门锁** | 给 webhook 和登录做签名、生成后台会话凭证 |
| `CR_WEBHOOK_SECRET` | **验门铃** | 收到 webhook 事件先验真伪：GitLab 当 `Secret token`、GitHub 做 HMAC 指纹。**伪造签名 → 401 直接拒** |
| `CR_ENCRYPTION_KEY` | **保险柜钥匙** | 把存进数据库的 api_key / IM token 用 Fernet 加密，平时读出来全是 `******` |
| `CR_ADMIN_PASSWORD` | **后台开门密码** | 管理员登录 Vue 后台用 |

生成命令（可反复用）：SECRET_KEY / WEBHOOK_SECRET 用
`python -c 'import secrets;print(secrets.token_urlsafe(48))'`，
ENCRYPTION_KEY 用 `python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())'`。

> ⚠️ **`CR_ENCRYPTION_KEY` 最特殊**：必须是 `Fernet.generate_key()` 生成的 **base64 串**，
> 不能用 `token_urlsafe` 的串，否则 `Fernet(key)` 会校验失败拒绝启动。

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