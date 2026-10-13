<div align="center">

# codereview-ai

**Self-hosted AI code review. Webhook in, inline comments out.**

<img alt="license: Apache 2.0" src="https://img.shields.io/badge/license-Apache%202.0-black">
<img alt="python: 3.11+" src="https://img.shields.io/badge/python-3.11+-black">
<img alt="stack: FastAPI + Vue 3" src="https://img.shields.io/badge/stack-FastAPI%20%2B%20Vue%203-black">

</div>

<p align="center">
🇺🇸 <a href="./README.en.md">English</a> | 🇨🇳 <a href="./README.md">简体中文</a>
</p>

## What this is

A code review service that runs on your own machine. Point a webhook at it from GitHub, GitLab or Gitee, and every PR / MR that opens or updates gets reviewed automatically — findings land as inline comments on the exact changed lines, and get pushed to DingTalk / Feishu / WeCom. If a webhook never arrives, or a batch of PRs was already open when you onboarded the project, the admin panel can sweep the platform and pull them in. Models, projects, permissions and stats are all configured from the bundled Vue admin panel.

Grouping / gates / budget caps are ported from **open-code-review** (Alibaba), cross-group de-duplication follows **pr-agent**'s content fingerprint, and the platform shape is closest to **AI-Codereview-Gitlab**.

## Screenshots

<table>
  <tr>
    <td align="center" width="50%"><b>Dashboard</b><br><sub>review jobs, issue mix, 14-day trend and cost</sub><br><img width="450" alt="Dashboard" src="https://github.com/user-attachments/assets/173bc789-b4a7-4a92-bcbe-e2d9a1ff8a9a"></td>
    <td align="center" width="50%"><b>Review records</b><br><sub>per-PR rollup and per-job detail, filters, retry, export</sub><br><img width="450" alt="Review records" src="https://github.com/user-attachments/assets/4b8a42f2-9348-4bfa-87ad-a32133c5e77f"></td>
  </tr>
  <tr>
    <td align="center" width="50%"><b>Review detail</b><br><sub>Markdown summary + structured findings (severity / line / source)</sub><br><img width="450" alt="Review detail" src="https://github.com/user-attachments/assets/2bbc929b-1cb4-4914-8660-89a44d74007c"></td>
    <td align="center" width="50%"><b>Inline comments</b><br><sub>findings pinned to the exact changed line</sub><br><img width="450" alt="Inline comments" src="https://github.com/user-attachments/assets/8b488eb4-f5e9-4711-8898-aaa634ed8776"></td>
  </tr>
  <tr>
    <td align="center" width="50%"><b>Score summary</b><br><sub>five-dimension scorecard with the verdict</sub><br><img width="450" alt="Score summary" src="https://github.com/user-attachments/assets/8c356076-37d3-44b7-8022-0f826296cf42"></td>
    <td align="center" width="50%"><b>IM daily report</b><br><sub>DingTalk / Feishu / WeCom, jobs and issue mix per day</sub><br><img width="450" alt="IM daily report" src="https://github.com/user-attachments/assets/1878447e-93f5-42c5-aa88-849fea0b870f"></td>
  </tr>
</table>

## Features

- **Three forges via webhook** — GitHub / GitLab / Gitee; every PR / MR that opens or updates is reviewed automatically. One service runs many projects, each with its own switches.
- **Inline comments on the changed line** — the model only pastes a code snippet and the engine pins the line by content matching; a hallucinated line number never lands a comment.
- **IM push** — DingTalk / Feishu / WeCom robots; a push with findings detail and the score the moment a review finishes.
- **Backfill** — missed webhooks, downtime, PRs opened before onboarding: pull them back by hand or on a schedule; heads already reviewed are skipped.
- **Three-layer pipeline** — LLM diff review + on-demand agentic whole-repo reasoning + optional semgrep static analysis, fused and de-duplicated into one set of findings.
- **No paying twice across rounds** — unchanged files between adjacent rounds are reused by content hash, saving money on high-frequency PRs.
- **Self-hosted admin panel** — dashboard, review records, projects, member & commit analytics, notifier and credential config, all in one Vue 3 app.
- **Private deployment** — one Docker container + SQLite out of the box, PostgreSQL when you need it; your code never leaves the network.

## Two review modes

Both paths share the same queue, the same write-back and the same stats. The only difference is **how much the model gets to see**. Each project picks its own, `diff` by default; `agentic` additionally needs `CR_AGENT_REVIEW_ENABLED` on globally.

- **`diff` — the diff only**: files are grouped and reviewed concurrently; line numbers are pinned by the engine with pure string matching over the pasted snippet, and results are merged deterministically. Cheap, certain, fast.
- **`agentic` — read the repo yourself, in a read-only sandbox**: one session per file group, a multi-turn tool loop (read / grep / find / diff) chasing context across files, context compaction, and a grace round when a budget is hit. Pricier, slower, deeper.

## Quick start

Three paths, pick one — the setup steps afterwards are the same:

- **Official image** (recommended) — no clone, two steps, all you need is Docker
- **Clone and build locally** — for changing the code
- **Windows desktop edition** — a single exe, no Docker, no Python

Open `http://localhost:5001/admin`, sign in, configure a model and a forge, then point a webhook at `POST http://<your-host>:5001/webhook` and open a PR.

Full steps — secret generation, running from source, dev and quality gates — are in [Install & quick start](docs/how_use_install.md) (Chinese).

## Docs

- [Install & quick start](docs/how_use_install.md) (Chinese)
- [GitHub onboarding guide](docs/how_use_github.md)
- [Gitee onboarding guide](docs/how_use_gitee.md)
- [Scheduled pull (backfill)](docs/how_use_poll.md)
- [Use PostgreSQL storage (standard tier)](docs/how_use_postgres.md)
- [Deploy to your own Aliyun ECS (GitHub Actions CI/CD)](docs/how_use_deploy_aliyun.md)
- [Agent workflow diagram](docs/agentic_workflow.md) (Chinese)

IM robot setup (Chinese):

- [DingTalk: custom robot access](https://open.dingtalk.com/document/group/custom-robot-access)
- [Feishu: custom robot guide](https://open.feishu.cn/document/uktmuktmuktm/uctm5yjl3eto24ynxkjn)
- [WeCom group robot webhook](https://www.tencentcloud.com/zh/document/product/1254/78645)

## Roadmap

What is planned next, in priority order.

**Doing next**

- [ ] **MCP feedback loop** — let the author's AI coding assistant (Claude Code / Cursor / Codex CLI) fetch findings after a push and fix them, closing the "push → wait for review → fetch findings → fix critical/high → push again" loop. Four steps: project-level API tokens → `wait_for_review` / `get_findings` endpoints (reviews are async, so wait semantics are the crux) → streamable HTTP MCP server → one-click AGENTS.md snippet from the settings page; plus a thin CLI shell (`cra check --pr <url>`) for setups without MCP and for git hooks / CI.
- [ ] **Project-level rule engine** — inject additional review rules matched by `path` / glob, first match wins. The schema is already in place (the `ProjectRule` table carries `path_glob` / `priority` / `system_merge`) but nothing references it yet: it needs a repository layer, an admin API, and the match-and-inject step at review time.
- [ ] **Native suggestion blocks** — render the `suggestion_code` we already capture as GitHub `suggestion` fences (`start_line` for multi-line) so a fix can be applied from the diff in one click instead of only read.
- [ ] **Four review voices** — professional / sarcastic / gentle / humorous, wording only, never the score. The config field and plumbing exist; the per-voice prompt presets do not.
- [ ] **standard queue tier** — Redis + arq for multi-instance deployments. Storage is wired to PostgreSQL already (see [tutorial](docs/how_use_postgres.md)); the queue still runs the single-node asyncio backend with the interface abstracted, not wired. (The single-node simple tier is entirely sufficient for now.)

**Later**

- [ ] More platforms — Gitea adapter (Gitee is already supported: webhook + scheduled pull, see the [Gitee guide](docs/how_use_gitee.md)); webhook IP allowlist as a second line of defense behind signature verification
- [ ] Deeper context — repo knowledge base (retrieve team conventions and past findings into the prompt), diff-less whole-file audits, `@bot` follow-up questions from developers
- [ ] More outputs — generic webhook and email notifiers, weekly / monthly reports, HTML report export (currently xlsx)
- [ ] **Full-auto fix loop** — once a review finishes, launch a headless agent (`claude -p` / `codex exec`) with the findings, open a PR only (never push directly) and trigger a re-review; per-project off-by-default switch, critical/high only. Depends on the MCP loop landing first.
- [ ] Admin polish — standalone task board, dark mode, zh/en i18n

## License

[Apache License 2.0](./LICENSE).

## About

[@zhansan379](https://github.com/zhansan379) — this started as "I want my own MRs reviewed automatically" and grew into a platform you can host, point at your own model, and watch on your own dashboard. Issues and PRs welcome.
