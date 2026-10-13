<div align="center">
<img alt="license: Apache 2.0" src="https://img.shields.io/badge/license-Apache%202.0-black">
<img alt="python: 3.11+" src="https://img.shields.io/badge/python-3.11+-black">
<img alt="stack: FastAPI + Vue 3" src="https://img.shields.io/badge/stack-FastAPI%20%2B%20Vue%203-black">

</div>

<p align="center">
🇺🇸 <a href="./README.en.md">English</a> | 🇨🇳 <a href="./README.md">简体中文</a>
</p>

## 这是什么

一个部署在你自己机器上的代码审查服务。在 GitHub / GitLab / Gitee 挂一个 webhook，之后每一个打开或更新的 PR / MR 都会被自动审查，意见以行级 inline 评论写回改动所在的那一行，同时推送到钉钉 / 飞书 / 企微。webhook 万一漏了事件，或者项目刚接入时已经有一批 PR 开着，后台可以主动补拉一轮把它们捞回来。配置、模型、项目、权限、统计都在自带的 Vue 管理后台里。

分组 / 门控 / 预算闸门移植自 **open-code-review**（阿里巴巴），跨组去重沿用 **pr-agent** 的内容指纹，平台形态最接近 **AI-Codereview-Gitlab**。

## 页面预览

<table>
  <tr>
    <td align="center" width="50%"><b>仪表盘</b><br><sub>审查任务、问题分布、14 天趋势与成本</sub><br><img width="450" alt="仪表盘" src="https://github.com/user-attachments/assets/173bc789-b4a7-4a92-bcbe-e2d9a1ff8a9a"></td>
    <td align="center" width="50%"><b>审查记录</b><br><sub>MR 汇总 / 明细，筛选、重试与导出</sub><br><img width="450" alt="审查记录" src="https://github.com/user-attachments/assets/4b8a42f2-9348-4bfa-87ad-a32133c5e77f"></td>
  </tr>
  <tr>
    <td align="center" width="50%"><b>审查详情</b><br><sub>总结 Markdown + 结构化 findings（严重度 / 行号 / 来源）</sub><br><img width="450" alt="审查详情" src="https://github.com/user-attachments/assets/2bbc929b-1cb4-4914-8660-89a44d74007c"></td>
    <td align="center" width="50%"><b>行级 inline 评论</b><br><sub>意见回写到改动所在的那一行</sub><br><img width="450" alt="行级 inline 评论" src="https://github.com/user-attachments/assets/8b488eb4-f5e9-4711-8898-aaa634ed8776"></td>
  </tr>
  <tr>
    <td align="center" width="50%"><b>评分总结评论</b><br><sub>五维打分，总分与结论一次给全</sub><br><img width="450" alt="评分总结评论" src="https://github.com/user-attachments/assets/8c356076-37d3-44b7-8022-0f826296cf42"></td>
    <td align="center" width="50%"><b>IM 日报推送</b><br><sub>钉钉 / 飞书 / 企微，任务与问题分布按日汇总</sub><br><img width="450" alt="IM 日报推送" src="https://github.com/user-attachments/assets/1878447e-93f5-42c5-aa88-849fea0b870f"></td>
  </tr>
</table>

## 功能一览

- **三平台 webhook 接入** — GitHub / GitLab / Gitee，PR / MR 打开或更新即自动审查；一套服务管多个项目，逐项目独立开关
- **行级 inline 评论** — 意见回写到改动所在的那一行；模型只负责粘贴代码片段，行号由引擎按内容匹配钉出，幻觉行号进不了评论位置
- **IM 推送** — 钉钉 / 飞书 / 企业微信机器人，审查完成即推，带 findings 明细与评分
- **主动补拉** — webhook 漏事件、服务停机、接入前的存量 PR，手动一键或定时轮询补回；同 head 已审自动跳过
- **三层审查管线** — LLM diff 审查 + 按需触发的 agentic 全仓推理 + 可选的 semgrep 静态分析，融合去重成一份意见
- **跨轮缓存** — 相邻轮次未变文件按内容哈希复用上一轮结论，高频 PR 省钱
- **自托管管理后台** — 仪表盘、审查记录、项目管理、成员分析、提交分析、IM 通知与凭据配置，一个 Vue 3 后台全包
- **私有化部署** — Docker 单容器 + SQLite 开箱即用，需要时切 PostgreSQL；代码不出内网

## 两种审查模式

两条路径共用同一个队列、同一套回写与统计，区别只在**模型能看到多少东西**。每个项目在后台单独选，默认 `diff`；`agentic` 另需全局打开 `CR_AGENT_REVIEW_ENABLED`。

- **`diff` — 只喂 diff**：按文件分组并发审查，行号由引擎按代码片段纯字符串匹配钉出，确定性合并去重。便宜、确定、快。
- **`agentic` — 只读沙箱里自己读仓库**：每文件组一个独立会话，多轮工具循环（read / grep / find / diff）跨文件追上下文，记忆压缩、预算触顶切 grace 收尾。更贵、更慢、理解更深。

## 快速开始

三条路线任选，装完后的配置步骤都一样：

- **官方镜像**（推荐）—— 免克隆，两步起服务，宿主机只要 Docker
- **克隆仓库本地构建** —— 要改代码、自己构建镜像
- **Windows 桌面单机版** —— 一个 exe，免 Docker 免 Python

起服务后打开 `http://localhost:5001/admin` 登录后台，配置模型与平台，然后在 GitHub / GitLab / Gitee 添加 webhook 指向 `POST http://<你的主机>:5001/webhook`，打开一个 PR 即可看到审查评论。

完整步骤（密钥生成、源码运行、开发与质量门槛）见 [安装与快速开始](docs/how_use_install.md)。

## 文档

- [安装与快速开始](docs/how_use_install.md)
- [GitHub 接入教程](docs/how_use_github.md)
- [Gitee 接入教程](docs/how_use_gitee.md)
- [主动补拉（轮询轨）](docs/how_use_poll.md)
- [使用 PostgreSQL 存储（standard 档）](docs/how_use_postgres.md)
- [部署到阿里云 ECS（GitHub Actions 自动部署）](docs/how_use_deploy_aliyun.md)
- [Agent 工作流图](docs/agentic_workflow.md)

IM 通道配置参考：

- [钉钉：自定义机器人接入](https://open.dingtalk.com/document/group/custom-robot-access)
- [飞书：自定义机器人使用指南](https://open.feishu.cn/document/uktmuktmuktm/uctm5yjl3eto24ynxkjn)
- [企业微信群机器人获取 Webhook](https://www.tencentcloud.com/zh/document/product/1254/78645)

## 路线图

**优先做**

- [ ] **AI 助手反馈闭环（MCP）** — 让提交者的 AI 编程助手（Claude Code / Cursor / Codex CLI）在 push 后自动取审查结果并修复，形成「push → 等审查 → 取 findings → 修 critical/high → 再 push」的闭环。分四步：项目级 API token 鉴权 → `wait_for_review` / `get_findings` 查询接口（审查异步，wait 语义是关键）→ 挂成 streamable HTTP MCP Server → 设置页一键生成 AGENTS.md 片段；另附 CLI 薄壳（`cra check --pr <url>`）给不支持 MCP 的场景与 git hook / CI 用。
- [ ] **项目级规则引擎** — 按 `path` / glob 注入追加规则、首个匹配者胜。数据层已就位（`ProjectRule` 表带 `path_glob` / `priority` / `system_merge`），缺仓储层、管理接口与审查时的规则匹配和 prompt 注入。
- [ ] **平台原生 suggestion 建议块** — 把已经拿到的 `suggestion_code` 渲染成 GitHub 的 `suggestion` 代码围栏（多行用 `start_line`），让建议能在 diff 上一键 Apply。
- [ ] **四种审查风格** — 专业 / 毒舌 / 绅士 / 幽默，只影响措辞不影响评分。配置入口与字段都在，缺各风格的 prompt 预设。
- [ ] **standard 队列档** — Redis + arq 支撑多实例部署。存储层已打通 PostgreSQL（见[教程](docs/how_use_postgres.md)）；队列仍是单机 asyncio 后端、接口已抽象好，未打通。（单机 simple 档当前完全够用。）

**之后**

- [ ] 更多平台 — Gitea 适配器（Gitee 已支持：webhook + 定时补拉，见[Gitee 教程](docs/how_use_gitee.md)）；webhook IP 白名单作为验签之外的第二道防线
- [ ] 更深的上下文 — 仓库知识库（检索团队规范与历史结论注入 prompt）、无 diff 的整文件审计、开发者 `@bot` 追问
- [ ] 更多出口 — 通用 webhook 与邮件通知、周报 / 月报、HTML 报告导出（现为 xlsx）
- [ ] **全自动修复闭环** — 审查完成后系统直接拉起 headless agent（`claude -p` / `codex exec`）喂入 findings，修完只开 PR 不直推并自动触发重审；项目级开关默认关、仅自动修 critical/high。依赖 MCP 闭环先落地、观察效果后再做。
- [ ] 后台体验 — 独立任务看板、深色模式、中英 i18n

## 许可证

[Apache License 2.0](./LICENSE)。

## 关于作者

[@zhansan379](https://github.com/zhansan379) — 这个项目从"想给自己的 MR 加个自动审查"开始，长成了一套能自己部署、自己配模型、自己看统计的完整平台。欢迎 issue 与 PR。
