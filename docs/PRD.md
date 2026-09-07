# codereview-ai 需求文档（PRD）

| 项 | 内容 |
|---|---|
| 产品名 | **codereview-ai** |
| 版本 | v0.1（MVP） |
| 文档日期 | 2026-09-06 |
| 技术栈 | Vue 3 + FastAPI |
| 定位 | 自托管、开源的 AI 代码审查平台 |

---

## 1. 背景与动机

### 1.1 为什么再做一个

市面上已有若干 AI code review 方案，但对中文团队 + 私有化部署场景，都有明确缺口：

| 方案 | 缺口 |
|---|---|
| CodeRabbit / Greptile | 闭源 SaaS，代码必须出境，金融/政企不可用 |
| Qodo PR-Agent | 开源，但以 GitHub 为中心，自建 GitLab 支持弱，中文语境一般 |
| AI-Codereview-Gitlab | 产品思路好，但工程实现是原型级：webhook 无签名校验、队列是裸 fork 会丢任务、沙箱可绕过、评分靠正则、三平台复制粘贴 |

**codereview-ai 的定位**：把 AI-Codereview-Gitlab 验证过的产品思路，
用可以长期演进的工程质量重做一遍，并补上它最关键的能力缺失——**行级评论**。

### 1.2 差异化

1. **行级 inline 评论**（对标商业产品的核心体验，前述开源方案普遍没做好）
2. **静态分析融合**：确定性问题交给 ruff/semgrep/eslint，LLM 只做语义与设计判断，显著降低幻觉与成本
3. **私有化优先**：单容器起步，国产模型一等公民，全链路可离线
4. **自建 GitLab 一等公民**，而不是 GitHub 的附属品

### 1.3 非目标（明确不做）

- 不做 SaaS 多租户（v0.1 单团队自托管）
- 不做 IDE 插件
- 不做代码自动修复与自动提交 PR（只给 suggestion，人来决定）
- 不做代码安全合规认证（不替代 SAST 专业产品）

---

## 2. 目标用户与场景

| 角色 | 场景 | 核心诉求 |
|---|---|---|
| **后端/前端开发** | 提交 MR 后收到审查意见 | 意见要准、要落在具体行上、不要刷屏 |
| **技术负责人** | 配置项目审查规则、看质量趋势 | 配置要可视化、统计要能看出问题在哪 |
| **运维/平台工程** | 部署与维护这套系统 | 一条命令起、有健康检查、出问题能查日志 |

**典型流程**：开发提交 MR → 30~90 秒后 bot 在具体代码行上留下评论 + 一条总结 →
IM 群收到通知 → 开发按建议修改并追加 commit → bot 只审查增量部分。

---

## 3. 功能需求

优先级：**P0** = MVP 必须有；**P1** = MVP 应该有；**P2** = v0.2 及以后。

### F1 Webhook 接入

| 编号 | 需求 | 优先级 |
|---|---|---|
| F1.1 | 接收 GitLab MR / Push 事件（Push 审查默认关，见 DESIGN §7.7） | P0 |
| F1.2 | 接收 GitHub PR / Push 事件（Push 审查默认关，见 DESIGN §7.7） | P0 |
| F1.3 | **签名/Token 校验**（GitHub/Gitea 为 HMAC；GitLab 为明文 `X-Gitlab-Token` 比对，见 DESIGN §6.1），全部 `hmac.compare_digest` 安全比对，`WEBHOOK_SECRET` 与平台 API token 分离 | P0 |
| F1.4 | 事件立即入库并返回 202，审查异步执行 | P0 |
| F1.5 | 幂等：同一 `(平台, 仓库, PR号, head_sha)` 只审查一次，由数据库唯一约束保证 | P0 |
| F1.6 | 只处理 `opened`/`reopened`/`synchronize`/`update`，忽略 close/merge | P0 |
| F1.7 | 可配置的分支规则（目标分支白名单/正则），不满足则跳过 | P1 |
| F1.8 | Gitee / Gitea 适配器 | P2 |
| F1.9 | IP 白名单作为第二道防线 | P2 |

### F2 审查引擎

| 编号 | 需求 | 优先级 |
|---|---|---|
| F2.1 | **diff 审查**：拉取变更 → 过滤文件类型 → LLM 审查 → 结构化结论 | P0 |
| F2.2 | **结构化输出**：JSON schema 约束（`{content, existing_code, suggestion_code, category, severity, path, thinking}`），category/severity 归一到枚举，非法值降级而非失败 | P0 |
| F2.3 | **锚定定位（existing_code）**：不让 LLM 出精确行号，让它贴它看到的代码片段；系统在 diff 里纯字符串匹配出真实行号（hunk 内 → 全文 → 跨文件） | P0 |
| F2.4 | 文件类型过滤 + 单文件体积上限；被跳过的文件必须在报告中显式列出 | P0 |
| F2.5 | 超长 diff 用 **LLM 语义分组**（按文件归属打包，≤10 文件/组，小改动整包不分组）+ map-reduce 汇总，禁止静默截断 | P0 |
| F2.6 | **增量审查**：追加 commit 时只审查 `last_reviewed_sha..head_sha` 的增量，并携带上次结论避免重复 | P0 |
| F2.7 | **静态分析融合**：先跑 ruff/eslint/semgrep，结果注入 prompt 让 LLM 不重复报告；工具发现的问题独立成 finding | P1 |
| F2.8 | **Agentic 审查**：LLM 可调用结构化工具（`read_file`/`grep_repo`/`file_find`/`file_read_diff`/`task_done`）探索仓库 | P1 |
| F2.9 | Agentic 必须运行在**真正隔离的沙箱**中（容器 / **无 shell 工具**，只给结构化只读工具），且任意阶段失败自动降级为 diff 审查 | P1 |
| F2.10 | **内存压缩**：会话 token 超 60% 上限异步后台压缩、超 80% 立即同步压缩，压缩失败宁超限也不丢证据 | P1 |
| F2.11 | **预算闸门**：调度前用 diff 体积预跑每组 token 成本，超预算停止调度剩余组并返回已有的部分评论 | P0 |
| F2.12 | Agentic 收尾（grace round）：工具预算耗尽后给终轮只允许提交评论/结束，防止乱试工具 | P1 |
| F2.13 | 四种审查风格（专业/毒舌/绅士/幽默），仅影响措辞不影响评分 | P1 |
| F2.14 | **项目级规则引擎**：按 `path`/glob 匹配注入追加规则（首个匹配者胜），支持合并系统规则 | P0 |
| F2.15 | Prompt injection 防御：明确声明被审查内容为不可信输入；被审查代码视为不可信数据 | P0 |
| F2.16 | 单次审查的 token 与耗时预算上限，超限强制收尾 | P0 |
| F2.17 | 仓库知识库 / RAG（索引团队规范与历史结论） | P2 |
| F2.18 | whole-file scan：无 diff 的历史代码审计模式（整文件审查，复用行号定位） | P2 |
| F2.19 | **LLM 输出修复链**：JSON/YAML 解析失败时按层修复（去围栏/去控制符/块标量/抹字符重试/截到末合法元素），而非直接判失败 | P1 |
| F2.20 | **内容指纹去重**：finding 以 `hash(file+body)` 为身份标识（非行号），跨轮去重 + 支持增量状态机 | P0 |

### F3 结果回写

| 编号 | 需求 | 优先级 |
|---|---|---|
| F3.1 | **行级 inline 评论**：GitLab discussions + position，GitHub pulls/reviews 批量提交；两者真实行号都由**同一个定位 walker** 算出（产出 `edit_type`/`old_line`/`new_line`） | P0 |
| F3.2 | **行号校验层**：finding 的真实行号由 `existing_code` 锚定匹配得到（非 LLM 拍号）；仍无法定位的降级并入总结评论而非丢弃 | P0 |
| F3.3 | 一条总结评论（评分 + 问题统计 + 跳过文件说明） | P0 |
| F3.4 | GitHub 用**单次** review API 提交整批（只产生一封通知邮件），422 时拆分合法/非法并降级重试；**用 `line`+`side`+`start_line` 稳定锚点，不用会随 commit 漂移的 legacy `position`** | P0 |
| F3.5 | 重复审查时，**按内容指纹**跳过已发过的评论/更新已有总结评论，而非不断新增 | P0 |
| F3.6 | `suggestion` 渲染为平台原生的建议块（GitHub `suggestion` code fence / 多行 `start_line`） | P1 |
| F3.7 | 评分低于阈值时把 MR 标记为需修改 / 阻塞合并（CI status check） | P2 |

> **F3.7 落地差异（GitLab vs GitHub）**：bot 只负责在 head commit 上打状态——GitLab 发 `failed`、GitHub 发 `failure`。是否**真正挡住合并**由仓库的合并保护规则决定：GitLab 侧，protected branch 开了"合并前需流水线通过"，`failed` 状态出现即自动禁掉合并按钮（红叉即阻断）；GitHub 侧，`failure` 只是显示红叉、**不影响 Merge 按钮**，必须去 branches 的 protected branch 规则里把 `codereview-ai` 勾进 **required status checks** 才能真正拦截（这一步只能在 GitHub 后台配，代码无法完成）。参见 `forges/base.py` 的 `post_commit_status`。
| F3.8 | 开发者在评论中 `@bot` 追问，bot 带上下文回复 | P2 |

### F4 通知推送

| 编号 | 需求 | 优先级 |
|---|---|---|
| F4.1 | 钉钉 / 飞书 / 企业微信推送，含签名与超长截断 | P0 |
| F4.2 | 项目级路由：不同项目推送到不同群 | P0 |
| F4.3 | 评分低于阈值时 @ 提交者 | P1 |
| F4.4 | 通知失败重试（指数退避），且不影响审查主流程 | P0 |
| F4.5 | 通用 webhook 推送（自定义 URL） | P2 |
| F4.6 | 邮件推送 | P2 |

### F5 管理后台（Vue）

| 编号 | 需求 | 优先级 |
|---|---|---|
| F5.1 | 单用户登录（JWT），无默认密钥，未配置则 fail-fast | P0 |
| F5.2 | **审查记录列表**：筛选（项目/时间/评分/状态）、分页、详情查看 | P0 |
| F5.3 | **项目管理**：注册项目、配置分支规则、文件类型、审查策略、**规则引擎（path/glob 匹配的追加规则）**、prompt 追加片段、评分阈值、inline 严重级门槛（`min_inline_severity`，见 DESIGN §13.4） | P0 |
| F5.4 | **模型配置**：provider / model / api_key / base_url / 温度 / token 上限，支持连通性测试 | P0 |
| F5.5 | **IM 配置**：渠道开关、webhook URL、签名密钥、项目路由、@ 阈值 | P0 |
| F5.6 | **统计看板**：审查量趋势、评分分布、问题类别分布、项目排行、token 成本 | P0 |
| F5.7 | **任务监控**：队列积压、失败任务列表、手动重试 | P1 |
| F5.8 | **日志查询**：按 trace_id 检索一次审查的完整链路 | P1 |
| F5.9 | 深色模式 | P2 |
| F5.10 | i18n（中/英） | P2 |
| F5.11 | 多用户 + RBAC | P2 |

### F6 报告

| 编号 | 需求 | 优先级 |
|---|---|---|
| F6.1 | 定时日报（按项目分组，非按人排名） | P1 |
| F6.2 | 周报 / 月报，可自定义 prompt | P2 |
| F6.3 | HTML 报告导出与分享 | P2 |

### F7 可观测与运维

| 编号 | 需求 | 优先级 |
|---|---|---|
| F7.1 | `/health`（存活）与 `/ready`（依赖就绪）端点 | P0 |
| F7.2 | 结构化 JSON 日志，**trace_id 贯穿** webhook → 队列 → LLM → 回写 | P0 |
| F7.3 | 日志脱敏：token/key/secret/password 自动打码，不记录 diff 正文 | P0 |
| F7.4 | Prometheus `/metrics`：审查量、耗时、失败率、token 消耗、队列深度、审查轮次/工具调用数 | P1 |
| F7.5 | 启动时配置校验，关键项缺失直接退出并给出明确指引 | P0 |
| F7.6 | **审查事件流**：每次 LLM 请求/响应/工具调用/失败都落一条可检索事件（带 trace_id），支撑 retry 报告与成本审计 | P2 |
| F7.7 | OpenTelemetry 追踪 | P2 |

---

## 4. 非功能需求

### 4.1 性能

| 指标 | 目标 |
|---|---|
| Webhook 响应 | P99 < 200ms（仅落库 + 入队，不含审查） |
| diff 审查端到端 | P50 < 60s，P95 < 180s（1000 行以内变更） |
| Agentic 审查 | P95 < 8min，硬超时 10min |
| 后台列表接口 | P95 < 500ms（10 万条记录，分页 + 索引） |
| 并发 | 单实例默认 4 个并发审查，可配置 |

### 4.2 安全

- Webhook 强制 HMAC 校验，`hmac.compare_digest` 比对
- 所有出站 HTTP 强制 timeout，禁止 `verify=False`
- 密钥类配置在数据库中**加密存储**，后台只回显掩码
- Agentic 沙箱：容器隔离 + 只读挂载 + `--network=none` + 非 root + 资源限额
- 被审查代码视为不可信输入，prompt 层与执行层双重防护
- 依赖扫描（pip-audit / Trivy）纳入 CI

### 4.3 可靠性

- 任务持久化，进程重启不丢；`running` 超时任务自动回收重投
- LLM 调用失败重试 3 次（指数退避），最终失败标记 `failed` 且**不回写评论**
- LLM 层错误一律抛异常，禁止把错误信息当作返回值
- IM 推送失败不影响审查结果落库

### 4.4 可移植性

| 档位 | 存储 | 队列 | 适用 |
|---|---|---|---|
| **simple**（默认） | SQLite (WAL) | 进程内 asyncio | 试用、小团队、单机 |
| **standard** | PostgreSQL | Redis + arq | 生产、多实例 |

同一套代码，通过配置切换；存储与队列都抽象成接口。

### 4.5 可维护性

- Python 3.11+，全量 type hints，mypy 渐进式检查
- ruff（lint + format）+ pre-commit
- 测试分层：unit / integration / e2e，CI 门禁行覆盖率 ≥ 70%，核心模块 ≥ 85%
- 新增一个代码平台 = 新增一个适配器文件，不改动现有代码

---

## 5. MVP 范围

### 5.1 In Scope（v0.1）

✅ GitLab + GitHub webhook（签名校验、幂等、异步）
✅ diff 审查 + 增量审查 + 结构化输出
✅ 行级 inline 评论 + 行号校验 + 总结评论
✅ LiteLLM 多模型接入
✅ 钉钉/飞书/企微推送
✅ Vue 管理后台：登录、审查记录、项目配置、模型配置、IM 配置、统计看板
✅ 双档存储与队列
✅ 健康检查、结构化日志、trace_id
✅ Docker Compose 一键部署

### 5.2 Stretch（v0.1 有余力则做）

🔸 静态分析融合（先接 ruff + eslint）
🔸 Agentic 审查（容器沙箱）
🔸 日报
🔸 任务监控页

### 5.3 Out of Scope

❌ 多用户 RBAC ❌ Gitee/Gitea ❌ RAG 知识库 ❌ @bot 对话
❌ CI status check 卡合并 ❌ 周报月报 ❌ HTML 报告导出 ❌ i18n

---

## 6. 验收标准

**功能验收**

1. 在自建 GitLab 提一个含 3 个文件改动的 MR，90 秒内出现 ≥1 条落在正确行号上的 inline 评论 + 1 条总结评论
2. 伪造签名的 webhook 请求返回 401 且不产生任何任务
3. 同一 MR 重复推送 5 次 webhook，只产生 1 次审查
4. 追加一个 commit，第二次审查的 diff 只含增量，且不重复上次的问题
5. 在 GitHub 提 PR，所有 inline 评论通过**单次** review API 提交
6. LLM 配置错误的 api_key，任务标记 `failed`，**MR 上不出现任何评论**
7. 审查过程中重启服务，任务恢复并最终完成
8. 后台可完成项目注册 → 模型配置 → IM 配置全流程，无需改 `.env`

**质量验收**

9. `docker compose up -d` 后 60 秒内 `/health` 返回 200
10. CI 全绿：ruff / mypy / pytest（覆盖率达标）/ 依赖扫描
11. 未配置 `SECRET_KEY` 时服务拒绝启动并给出生成命令
12. 日志中检索不到任何 token / api_key 明文
13. 对照 `reference/antipatterns.md` 逐条核查，无一复现

---

## 7. 风险

| 风险 | 影响 | 应对 |
|---|---|---|
| 行级评论行号越界被平台拒绝 | 核心体验失效 | **existing_code 锚定定位**（非 LLM 拍号）+ 降级到总结评论；锚定算法最先落地验证 |
| 超长/超大变更审查 token 与耗时失控 | 费用超预期、覆盖不全 | 语义分组 + 预算闸门（前置预估、超预算返回部分评论）+ 内存压缩 |
| LLM 结构化输出不稳定 | 解析失败 | LiteLLM 的 JSON mode + schema 校验 + 一次重试 + 失败标记而非静默降级 |
| Agentic 成本与耗时失控 | 费用超预期 | 硬性 token/轮次/时间预算；默认关闭，显式开启 |
| Prompt injection | 沙箱逃逸 | 容器隔离 + 无 shell 工具 + prompt 层声明 |
| 误报导致开发者反感 | 产品被弃用 | 静态分析融合降低幻觉；inline 只发严重级 ≥ `min_inline_severity`（项目配置，默认 `high`），其余归入总结（DESIGN §13.4） |
| 单人维护 | 项目停滞 | 从第一天就有 CONTRIBUTING / 测试 / CI，降低外部贡献门槛 |
