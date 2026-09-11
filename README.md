<div align="center">

# codereview-ai

**自托管的 AI 代码审查平台。Webhook 进来，行级评论回去。**

<img alt="codereview-ai" src="./assets/banner.webp">

<img alt="license: Apache 2.0" src="https://img.shields.io/badge/license-Apache%202.0-black">
<img alt="python: 3.11+" src="https://img.shields.io/badge/python-3.11+-black">
<img alt="stack: FastAPI + Vue 3" src="https://img.shields.io/badge/stack-FastAPI%20%2B%20Vue%203-black">

</div>

<p align="center">
🇺🇸 <a href="./README.en.md">English</a> | 🇨🇳 <a href="./README.md">简体中文</a>
</p>

## 这是什么

一个部署在你自己机器上的代码审查服务。在 GitHub 或 GitLab 挂一个 webhook，之后每一个打开或更新的 PR / MR 都会被自动审查，意见以行级 inline 评论写回改动所在的那一行，同时推送到钉钉 / 飞书 / 企微。webhook 万一漏了事件，或者项目刚接入时已经有一批 PR 开着，后台可以主动补拉一轮把它们捞回来。配置、模型、项目、权限、统计都在自带的 Vue 管理后台里。

<img width="2549" height="1191" alt="image" src="https://github.com/user-attachments/assets/173bc789-b4a7-4a92-bcbe-e2d9a1ff8a9a" />

<img width="2549" height="1191" alt="image" src="https://github.com/user-attachments/assets/4b8a42f2-9348-4bfa-87ad-a32133c5e77f" />

<img width="2549" height="1191" alt="image" src="https://github.com/user-attachments/assets/2bbc929b-1cb4-4914-8660-89a44d74007c" />


## 为什么需要它

做审查的方式上，它站在"确定性的工程 × 会翻仓库的 agent"这一档：**凡是能被工程确定性解决的问题，绝不让模型赌**。行号由引擎按代码片段纯字符串匹配钉出，模型报的行号伤不到评论位置；文件分组按硬上限切开、LLM 只做闭卷的分组判断；agent 探一圈仓库，出任何岔子整条退回 diff，绝不把 agent 的失败记成任务失败。

这套基准不是凭空造的——哪些环节可以放手让模型发挥、哪些必须交给确定性的工程兜底，全靠对照这类项目找出来的。它的血统：diff / agent 双档与"模型不填行号、只粘片段"的锚定，直接移植自 **open-code-review**（阿里巴巴）的 grouping / plan 门控 / 预算闸门 / resolver；跨组去重用的内容指纹取自 **pr-agent** 的 *body_fp OR code_fp*；而"webhook 进来 → 异步队列 → 行级评论回写 + IM 推送 + 自托管后台"这个平台形态，最接近 **AI-Codereview-Gitlab**。

**优势**

- **行号锚定最稳** — 模型只粘代码片段、引擎钉行号；OCR 也重定位，但它是"模型给行号 + 事后 relocation"，本项目是压根不收模型的行号。这是和 pr-agent 这类"靠模型填行号走 inline"最大的分野：幻觉行号从一开始就进不了评论位置。
- **一套服务管全部项目** — pr-agent / OCR 是"一个 PR 跑一次"的命令或 Action；本项目是常驻服务：webhook 验签 → 异步队列 → 行级回写 + IM 推送 + 看板 + 日报，多项目、主动补拉、崩溃回放、每项目独立开关都在一个后台里。
- **agent 翻车不当事故** — agentic 全仓推理只读，任何一环出问题整条退回 diff，不会留一条"任务 failed"却没人对结论负责。OCR 的 grade 也兜底，但没有"回到 diff 这份必保结论"的服务级保证。
- **跨轮不重复花钱** — 相邻轮次未变文件按内容哈希复用上一轮结论；一次性审查请求没有"上一轮"可复用。
- **完整的ui页面** — 更便捷的操作，包括仪表盘，审查记录，项目，IM通知，拉取缓存等....

## 你会得到什么

<img alt="核心能力" src="./assets/features.webp">

- **MR 打开即审** — 事件验签后立刻入队并返回 `202`，审查异步执行，结果锚定到新 head 的具体行号。
- **漏掉的能补回来** — webhook 没送到、服务重启期间的事件、项目接入前就开着的 PR，都可以主动补拉：手动点一次，或配个定时轮询按间隔扫全部启用项目。同 head 已审过的自动跳过，不重复投队。
- **代码不出内网** — SQLite + Docker 私有化部署；平台与模型凭据 Fernet 加密落库，后台改完即时热更。
- **三层审查管线** — LLM diff 审查、可选的 agentic 全仓推理（clone + 只读工具跨文件组分析）、semgrep 静态分析，融合成一份意见。
- **不重复花钱** — 相邻轮次未变更的文件按内容哈希复用上一轮结论，不再喂给模型。
- **跑得住** — asyncio 队列 + worker 池，并发数运行时可调；进程重启后自动回放遗留任务续跑。

## 工作方式

Webhook 事件经 HMAC 验签后写入异步队列，立即返回 `202`，请求不等待审查。主动补拉走同一条队列——调平台 API 列出打开的 PR / MR，给未审过的落一条 `queued` 记录入队，同样立即返回。worker 池取出任务，把 diff 交给 LLM 审查层，按开关叠加 semgrep 与 agentic 全仓分析，三路结论融合去重。

最终意见经 Forge API 回写为行级评论，同时落库供后台看板与日报使用。整条链路上没有外部服务参与调度。

## 两种审查模式

两条路径共用同一个队列、同一套回写与统计，区别只在**模型能看到多少东西**。每个项目在后台单独选，默认 `diff`；`agentic` 另需全局打开 `CR_AGENT_REVIEW_ENABLED`。

### diff —— 只读这次的改动

拿到 diff 直接开审，不碰仓库。文件先分组：改动小（文件少且总 churn < 200）懒得分就整包一组；改动大则花一次"只看路径与增删行数"的廉价调用做语义聚类，把关联文件并进一组（聚类失败退化成一文件一组），聚完再按硬上限切开（每组 ≤10 文件、≤1000 行 churn）。各组并发送审，结论按「文件 + 意见正文」的内容指纹去重后合并。

这一层的关键约定是**模型不报行号**——prompt 要求它把问题代码原样粘回来，行号由引擎自己钉：先在 diff hunk 内找连续匹配（新侧不中试旧侧），再退到整份新文件里找，最后才去别的文件里找，且只认唯一命中，撞上两处宁可放弃。模型幻觉出的行号因此伤不到评论位置。钉不上、或钉在 hunk 之外不可评论的意见一条不丢，统一并进总结评论；单文件超 600 行改动或 20 KB diff 会跳过，跳过清单同样写进总结，不当无事发生。

### agentic —— 带着工具去翻仓库

签名改了但漏改调用方、跨文件的状态假设，这类问题只看 diff 是看不出来的，得让模型自己去读仓库。开启后 agent 拿到六个**只读**工具：按行区间读文件（≤500 行）、`git grep` 定值搜索（≤100 命中）、按名找文件、读已解析的 diff、提交意见、结束会话。没有 shell，没有网络，没有写入。

仓库是每个 repo 一份**裸 clone**（带跨平台文件锁做增量 fetch，落在你配的缓存目录），读取一律走 `git cat-file blob <head sha>:<路径>`——没有工作区可逃逸，也不会有两个 PR 互相把 checkout 掀翻。会话按文件组独立跑，各带自己的上下文，每组 20 轮 / 300 秒 / 80k 提示 token；额度用尽不是直接收摊，而是插一轮只留「提交意见」和「结束」的收尾轮，逼它把手上的结论交出来。上下文涨到 60% 起后台压缩、80% 起同步压缩，压缩失败原样返回，绝不截断成半条 JSON。

单组的完整链路：大改动先出计划（单文件 churn ≥300 或整组 ≥600 行才触发）→ 主循环翻代码 → 无行号的意见重锚（确定性匹配 → 唯一跨文件匹配 → 让模型重挑片段）→ 事实核查（对着 diff 删掉铁证错评，这是唯一会删意见的环节）→ 跨组去重 → 一次 0-100 评分。每次 LLM 调用都按阶段落库，后台可按泳道回看整场对话。

### 出问题就退回 diff

沙箱关着、`git` 不在 PATH、clone 不到、目标 commit fetch 不下来、分组失败、agent 自己报 FAILED——任何一环出问题，整条退回普通 diff 审查，绝不把 agent 的失败转成任务级 failed。非平凡的 diff 上 agent 一条意见都没交，同样按这条兜底：空成功不当结论。库里记的是**实际走通**的那条路径，所以看板上 Agent 与 diff 的对比是真账。

## 快速开始

```bash
git clone https://github.com/zhansan379/codereview-ai.git
cd codereview-ai

# 备份模板：完整环境变量清单 + 双语注解，常用的直接放开，不用的保持注释
cp .env.example .env

# 或手动写：四个密钥没有默认值，缺失即 fail-fast 退出
cat > .env <<'EOF'
CR_SECRET_KEY=<用下面的命令生成>
CR_WEBHOOK_SECRET=<用下面的命令生成>
CR_ENCRYPTION_KEY=<用下面的命令生成>
CR_ADMIN_PASSWORD=<用下面的命令生成>
EOF

docker compose up -d
open http://localhost:5001/admin
```

生成密钥：

```bash
python -c 'import secrets;print(secrets.token_urlsafe(48))'      # SECRET_KEY / WEBHOOK_SECRET
python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())'  # ENCRYPTION_KEY
python -c 'import secrets;print(secrets.token_urlsafe(24))'                          # ADMIN_PASSWORD
```

> `CR_ENCRYPTION_KEY` 必须是合法的 32 字节 urlsafe-base64 Fernet 密钥，不能用 `secrets.token_urlsafe` 顶替。

后台登录后配置模型与平台，然后在 GitHub / GitLab 添加 webhook 指向 `POST http://<你的主机>:5001/webhook`，打开一个 PR 即可看到审查评论。

[如何使用 Webhook](docs/how_use_webhook.md)

[如何申请 Secret](docs/how_apply_for_secret.md)

## 安装

不用 Docker 的话：

```bash
uv sync
uv run uvicorn codereview_ai.main:app --host 0.0.0.0 --port 5001
```

管理后台在 `/admin`，交互式 API 文档在 `/docs`（`CR_OPENAPI_ENABLED=0` 可关闭），健康检查在 `/health`。

前端如需自行构建：

```bash
cd frontend && npm install && npm run build   # 产物输出到 frontend/dist，由 /admin 托管
```

开发与质量门槛：

```bash
uv run ruff check src tests
uv run mypy src
uv run pytest tests --cov=codereview_ai --cov-fail-under=70
```

覆盖率总门槛 70%，核心模块（`forges` / `review` / `worker` / `queue` / `storage`）83%；CI 另跑 `pip-audit`。

完整配置项（`CR_` 前缀）与 API 清单见 `/docs` 与 `GET /openapi.json`。

## 路线图

接下来计划推进的部分，按优先级排序。

**优先做**

- [ ] **项目级规则引擎** — 按 `path` / glob 注入追加规则、首个匹配者胜，让审查策略能逐目录逐文件配置。数据层已就位（`ProjectRule` 表带 `path_glob` / `priority` / `system_merge`），但全树尚无引用：缺仓储层、管理接口，以及审查时的规则匹配与 prompt 注入。
- [ ] **平台原生 suggestion 建议块** — 把已经拿到的 `suggestion_code` 渲染成 GitHub 的 `suggestion` 代码围栏（多行用 `start_line`），让建议能在 diff 上一键 Apply，而不是只能读。
- [ ] **四种审查风格** — 专业 / 毒舌 / 绅士 / 幽默，只影响措辞不影响评分。配置入口与字段都在，缺各风格的 prompt 预设。
- [ ] **standard 存储 / 队列档** — PostgreSQL + Redis + arq，支撑多实例部署。队列与存储的接口已抽象好，后端未打通（单机 simple 档当前完全够用）。

**之后**

- [ ] 更多平台 — Gitee / Gitea 适配器；webhook IP 白名单作为签名校验之外的第二道防线
- [ ] 更深的上下文 — 仓库知识库（检索团队规范与历史结论注入 prompt）、无 diff 的整文件审计、开发者 `@bot` 追问
- [ ] 更多出口 — 通用 webhook 与邮件通知、周报 / 月报、HTML 报告导出（现为 xlsx）
- [ ] 后台体验 — 独立任务看板、深色模式、中英 i18n

## 许可证

[Apache License 2.0](./LICENSE)。

## 关于作者

[@zhansan379](https://github.com/zhansan379) — 这个项目从"想给自己的 MR 加个自动审查"开始，长成了一套能自己部署、自己配模型、自己看统计的完整平台。欢迎 issue 与 PR。
