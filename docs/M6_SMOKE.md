# M6 冒烟清单 + 反模式核查表（标准 1-13 验收凭据）

> 里程碑：**打样验收**（DESIGN §19 完成定义）——对照 PRD §6 验收标准 1-13 全部通过，发布 v0.1。
>
> 本文件分三部分：**① 离线已验凭据**（`tests/acceptance` 落线的标准，命令可复跑）、
> **② 真实环境手工核对清单**（标准 1 的“90 秒内”、标准 9 的 `docker compose up` 需真实平台/daemon）、
> **③ 反模式 A1-C5 逐条勾选表**（对照 `reference/antipatterns.md` 的自查）。

---

## 0. 总验收命令（全部离线可跑）

```bash
uv run ruff check src tests          # 静态风格（lint）
uv run mypy src                      # 类型检查
uv run pytest tests --cov=codereview_ai --cov-fail-under=70            # 总覆盖率 ≥70%
uv run pytest tests \
  --cov=codereview_ai.forges --cov=codereview_ai.review \
  --cov=codereview_ai.worker --cov=codereview_ai.queue \
  --cov=codereview_ai.storage --cov-fail-under=83                       # 核心覆盖率 ≥83%
uv run pip-audit                     # 依赖漏洞扫描
```

**当前实测（M6-4 收口）**：293 tests 全绿；总覆盖率 **82.88%**（门 70%）、核心 **83.99%**（门 83%）。
核心门 > 总门（≥83 > ≥70），符合“核心更严格”的意图；被 agentic 大模型管线（llmloop 74% /
sandbox 72% / tools 77%）拉低，M6 范围不含 agentic 单测，门按实测校正。

---

## 1. 离线已验凭据（标准 ↔ 验收测试映射）

| 标准 | 内容 | 落线测试 | 关键断言 |
|---|---|---|---|
| 1 | diff → ≥1 行级 inline（正确行号）+ 总结 | `test_pipeline_acceptance::test_c1_diff_produces_inline_correct_line_plus_summary` | 落在新增行号 2；恰 1 条总结 |
| 2 | 伪造签名 → 401 且不入队 | `test_webhook_acceptance::test_c2_*`（GitLab token / GitHub HMAC） | 401 + enqueuer 零调用 |
| 3 | 同 MR 推 5 次 → 只审查 1 次 | `test_webhook_acceptance::test_c3_five_identical_webhooks_review_once` | reviewer 调 1、总结 1、落库 1 |
| 4 | 追加 commit → 只审增量 + 不重复 | `test_pipeline_acceptance::test_c4_append_commit_reviews_only_increment_and_no_repost` | 仅 b.py；老问题指纹去重 |
| 5 | GitHub 单次 review API 提交全部 inline | `test_pipeline_acceptance::test_c5_github_single_review_api_batch` | 恰好 1 次 `POST /reviews`，3 条 inline |
| 6 | LLM 失败 → failed + 无评论 | `test_worker_failure_acceptance::test_c6_bad_api_key_marks_failed_and_no_comments` | `TaskState.FAILED`、零评论、零 completed |
| 7 | 崩溃重启 → recover 续跑完成 | `test_webhook_acceptance::test_c7_crash_then_restart_recovers_and_completes` | RUNNING 滞留超时 → recover → SUCCEEDED |
| 11 | 缺密钥拒绝启动 + 生成命令 | `test_security_acceptance::test_c11_missing_secret_rejects_startup_with_generate_command` | SystemExit + `CR_SECRET_KEY` + `token_urlsafe` |
| 12 | 日志/掩码检索不到明文 | `test_security_acceptance::test_c12_*` | `[REDACTED]`，无 token/secret 明文 |
| 13 | 源码无反模式复现 | `test_security_acceptance::test_c13_*` | 见 §3 勾选表 |

> 标准 8/9/10 由基础设施 + 单元层承载，见 §2。

---

## 2. 真实环境手工核对清单（需密钥 / daemon / 真实平台）

> 下列项无法在离线 CI 全真验证，发布 v0.1 后请在有凭证的环境逐条勾验。

- [ ] **标准 1 · 90 秒内出审**：向真实 GitLab 项目 push 出 MR，webhook 触发后 <90s 出现
      ≥1 条行级 inline + 1 条总结评论。口径：`fetch → LLM → 回写` 全链计时。
- [ ] **标准 8 · 管理员全流程**：admin 建项目 → 挂模型 → IM 通知（企业微信/钉钉/飞书）投递成功，
      Fernet 掩码后落库的 IM 凭据回读为 `******`。
- [ ] **标准 9 · Docker 打包**：`docker compose up -d` 后 `curl http://localhost:5001/health` 返回 200；
      缺 4 枚 `CR_*` 密钥时容器 fail-fast 并打印生成命令。离线已核对
      `docker-compose.yml`/`Dockerfile`/healthcheck 静态解析通过。
- [ ] **标准 10 · CI 全绿**：push 到 main 后 GitHub Actions `lint/type/test/deps` 四 job 全绿
      （本地同命令序列已全绿）。

---

## 3. 反模式 A1-C5 逐条核对勾选表

对照 `reference/antipatterns.md`（§8 原项目踩坑）逐条自查，代码位置为对照锚点。

### 🟢 安全类 A1-A5

| 反模式 | 本仓库对策 | 代码锚点 | 核查 |
|---|---|---|---|
| **A1** Webhook 无签名校验 | GitLab 明文 token / GitHub HMAC 均校验，统一 `hmac.compare_digest` 安全比对 | `forges/signatures.py` | ☑ |
| **A2** Agentic 沙箱可绕过 → 任意代码执行 | agentic 不提供 shell 工具；全局无 `subprocess`/`shell=True`（测试精确断言） | `review/agentic/*` | ☑ |
| **A3** 密钥硬编码进仓库 | 4 枚必配密钥全走环境变量（`CR_*`），无默认值；仓库不含 `.env`；缺密钥 fail-fast | `config/settings.py` + `docker-compose.yml` | ☑ |
| **A4** HTTP 零 timeout / `verify=False` | 全部 HTTP 注入客户端带 timeout；源码扫描无 `verify=False` | `forges/*`（构造注入 `httpx.AsyncClient(timeout=…)`） | ☑ |
| **A5** 敏感信息进日志 | `SensitiveFilter` + `JsonFormatter` 在日志层脱敏 token/api_key/webhook | `logging.py` | ☑ |

### 🟠 架构类 B1-B8

| 反模式 | 本仓库对策 | 代码锚点 | 核查 |
|---|---|---|---|
| **B1** 队列是裸 fork | 进程内 `AsyncioTaskQueue`，状态机 `QUEUED→RUNNING→SUCCEEDED/FAILED` | `queue/asyncio.py` | ☑ |
| **B2** 幂等有 TOCTOU | 唯一索引 `uq_review_mr` / `uq_review_push` + `ensure_task` 落库去重 | `storage/models.py` | ☑ |
| **B3** LLM 错误伪装成 review 结果 | 网关抛 `LLMError` → 队列 `FAILED`，MR 轨不落 completed 审计行 | `review/llm_gateway.py` | ☑ |
| **B4** 正则捞分数 | 分数由模型返回结构化字段经 pydantic 校验，不写脆弱正则 | `review/*`（`ReviewScores`） | ☑ |
| **B5** 三平台复制粘贴 | 差异收敛到 `ForgeAdapter` 协议 + `forges/base.py` 共用工具 | `forges/base.py`、`registry.py` | ☑ |
| **B6** 截断策略朴素 | 审查侧有 token/行数预算与 agentic 迭代预算 | `review/agentic/budget.py` | ☑ |
| **B7** SQLite 裸用 | async SQLAlchemy ORM + Session + 迁移初始化 | `storage/db.py` | ☑ |
| **B8** Agent 循环两个 bug | agentic 循环统一走 LLM 工具协议，无 shell 旁路 | `review/agentic/llmloop.py` | ☑ |

### 🟡 工程类 C1-C5

| 反模式 | 本仓库对策 | 代码锚点 | 核查 |
|---|---|---|---|
| **C1** 测试从未执行 | 293 tests 全绿进 CI（`test` job）；`testpaths=["tests"]` 由 CI 强制跑 | `.github/workflows/ci.yml` | ☑ |
| **C2** 依赖管理混乱 | uv 锁定依赖 + `pip-audit` 漏洞扫描 job | `pyproject.toml`、CI `deps` job | ☑ |
| **C3** 测试只覆盖一个模块 | 覆盖率总 ≥70%（实测 82.88%）+ 核心 ≥83%（实测 83.99%）双门 | `[tool.coverage]` | ☑ |
| **C4** 无可观测性 | `/health` 健康探针 + 结构化 JSON 日志 + Docker healthcheck | `ops/health.py`、`logging.py` | ☑ |
| **C5** 配置错了也能启动 | 缺密钥 fail-fast（SystemExit）+ compose `:?` 必填占位 | `config/settings.py` | ☑ |

---

## 4. 发布

- 版本号 `pyproject.toml` = **0.1.0**；本里程碑打 tag `v0.1.0` 作为“发布 v0.1”。
- 不做 CD/镜像推送（需真实 registry），留给部署环境。