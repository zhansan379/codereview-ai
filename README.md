# <img width="24" height="24" alt="image" src="https://github.com/user-attachments/assets/d108c6ef-3293-4bf9-87fc-777cddebeac2" /> codereview-ai

<img width="2848" height="1600" alt="AI对话_客服系统稳定性指标" src="https://github.com/user-attachments/assets/237300ac-f4a3-4846-8aaa-5f50663bef24" />

一个部署在你自己机器上的代码审查服务。在 GitHub / GitLab / Gitee 挂一个 webhook，之后每一个打开或更新的 PR / MR 都会被自动审查，意见以行级 inline 评论写回改动所在的那一行，同时推送到钉钉 / 飞书 / 企微。webhook 万一漏了事件，或者项目刚接入时已经有一批 PR 开着，后台可以主动补拉一轮把它们捞回来。配置、模型、项目、权限、统计都在自带的 Vue 管理后台里。

分组 / 门控 / 预算闸门移植自 **open-code-review**（阿里巴巴），跨组去重沿用 **pr-agent** 的内容指纹，平台形态最接近 **AI-Codereview-Gitlab**。

项目演示地址：http://60.205.230.218:5001/admin 账号密码均为：test_user

## 功能一览

- **三平台 webhook 接入** — GitHub / GitLab / Gitee，PR / MR 打开或更新即自动审查
- **IM 推送** — 钉钉 / 飞书 / 企业微信机器人，审查完成即推，带 findings 明细与评分
- **三层审查管线** — LLM diff 审查 + 按需触发的 agentic 推理 + 可选 semgrep 静态分析
- **跨轮缓存** — 相邻轮次未变文件按内容哈希复用上一轮结论，高频 PR 省钱
- **自托管管理后台** — 仪表盘、审查记录、项目管理、成员分析、提交分析、IM 通知与凭据配置
- **私有化部署** — Docker 单容器 + SQLite 开箱即用，需要时切 PostgreSQL；代码不出内网

## 两种审查模式

同一套分组、定位、回写管线，审查方式二选一：

| | **diff 模式**（默认） | **agent 模式** |
| --- | --- | --- |
| 怎么审 | 把本次改动直接交给模型，一组一次调用 | 每组一个会调工具的 AI 会话，最多 50 轮 |
| 能看什么 | 只看改动文本 | 可翻整个仓库：读文件、搜关键词、查文件名 |
| 成本 / 速度 | 低 / 快 | 高 / 慢（换取更全上下文与更高召回） |
| 适用 | 日常改动、成本敏感 | 大改、跨文件影响、需要查证的场景 |

两种模式共用同一套：范围过滤、分组、行号锚定（模型只给代码片段、行号由引擎锚定）、五维评分、
增量复用与平台回写；**agent 失败或零产出会自动降级为 diff 模式兜底**，保证至少有一条审查落回。

分阶段业务流程、diff / 增量 / agent 内部机制见 **[Agent 工作流图](docs/agentic_workflow.md)**。

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

## 快速开始

三条部署路线，任选其一，装完后的配置步骤都一样：

- **官方镜像**（推荐）—— 免克隆，两步起服务，宿主机只要 Docker
- **克隆仓库本地构建** —— 要改代码、自己构建镜像
- **Windows 桌面单机版** —— 一个 exe，免 Docker 免 Python

### 方式一：拉取官方镜像，免克隆（推荐）

镜像发布在 GitHub Container Registry，准备一个空目录，两步起服务（命令在 bash 与 PowerShell 中均可直接执行）：

```bash
mkdir codereview-ai && cd codereview-ai

# 1) 生成四枚密钥：直接借用官方镜像里的 Python，宿主机什么都不用装
docker run --rm ghcr.io/zhansan379/codereview-ai:latest python -c "
import os, base64, secrets
print('CR_SECRET_KEY=' + secrets.token_urlsafe(48))
print('CR_WEBHOOK_SECRET=' + secrets.token_urlsafe(48))
print('CR_ENCRYPTION_KEY=' + base64.urlsafe_b64encode(os.urandom(32)).decode())
print('CR_ADMIN_PASSWORD=' + secrets.token_urlsafe(24))
"
```

把终端输出的四行存成当前目录的 `.env` 文件（密码行可当场换成你自己的登录密码；任意文本编辑器均可，注意别存成 `.env.txt`）：

```ini
CR_SECRET_KEY=<粘贴第 1 行>
CR_WEBHOOK_SECRET=<粘贴第 2 行>
CR_ENCRYPTION_KEY=<粘贴第 3 行>
CR_ADMIN_PASSWORD=<粘贴第 4 行，或换成你自己的密码>
```

```bash
# 2) 起容器（单行命令，任何 shell 通用）
docker run -d --name codereview-ai -p 5001:5001 --env-file .env -v codereview-ai-data:/app/data ghcr.io/zhansan379/codereview-ai:latest
```

浏览器打开 http://localhost:5001/admin，用 `CR_ADMIN_PASSWORD` 登录。

默认使用 SQLite，数据持久化在 `codereview-ai-data` 卷里；需要 PostgreSQL（standard 档）见[教程](how_use_postgres.md)。

> 注意：重新生成 `CR_ENCRYPTION_KEY` 后，数据库里已保存的平台凭据（token 等）会解不开，需要在后台重新配置一遍。

### 方式二：克隆仓库，本地构建

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

# 前端构建一次（dist 被 gitignore，镜像 COPY 用，缺了 build 必失败）
cd frontend && npm install && npm run build && cd ..

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

### 方式三：Windows 桌面单机版（exe，免 Docker 免 Python）

不装 Docker 也不装 Python：从 [GitHub Release](https://github.com/zhansan379/codereview-ai/releases/latest) 下载 `codereview-ai-windows-x64.zip`，解压到任意目录，双击 `codereview-ai.exe` 即用。

- 首次启动自动生成四枚密钥写入数据目录的 `.env`，并在控制台打印**后台登录密码**；浏览器自动打开 `http://127.0.0.1:5001/admin/`
- 控制台窗口保持开着（关窗 = 停止服务）；数据存在 exe 旁（便携模式）或 `%LOCALAPPDATA%\codereview-ai`，控制台会打印实际位置
- 功能与 Docker 版一致，ruff 静态检查随包附带；需要 semgrep 时机器上 `pip install semgrep` 即可

常用参数：

```text
codereview-ai.exe --port 5002        # 换端口（默认 5001，被占用会提示）
codereview-ai.exe --host 0.0.0.0     # 接收跨机 webhook 回调
codereview-ai.exe --no-browser       # 启动后不自动开浏览器
```

升级：下载新 zip 覆盖 exe 所在目录（`data/` 与 `.env` 在数据目录里，不受影响）。卸载：删解压目录；非便携模式连 `%LOCALAPPDATA%\codereview-ai` 一起删。

### 装完之后

后台登录后配置模型与平台，然后在 GitHub / GitLab / Gitee 添加 webhook 指向 `POST http://<你的主机>:5001/webhook`，打开一个 PR 即可看到审查评论。

- 平台接入：[GitHub 接入教程](how_use_github.md) / [Gitee 接入教程](how_use_gitee.md)
- webhook 漏事件、服务停机、存量 PR：[主动补拉（轮询轨）](how_use_poll.md)
- 换 PostgreSQL（standard 档）：[使用 PostgreSQL 存储](how_use_postgres.md)
- 部署到自己的服务器：[部署到阿里云 ECS（GitHub Actions 自动部署）](how_use_deploy_aliyun.md)

### 源码运行（不用 Docker）

```bash
uv sync
uv run uvicorn codereview_ai.main:app --host 0.0.0.0 --port 5001
```

管理后台在 `/admin`，交互式 API 文档在 `/docs`（`CR_OPENAPI_ENABLED=0` 可关闭），健康检查在 `/health`。完整配置项（`CR_` 前缀）与 API 清单见 `/docs` 与 `GET /openapi.json`。

### 开发

前端构建（`/admin` 托管 `frontend/dist`，Docker 镜像也会 COPY 它；改前端代码后重新执行）：

```bash
cd frontend && npm install && npm run build   # 产物输出到 frontend/dist
```

Docker 部署：改完代码重建本地镜像并重启 `docker compose build && docker compose up -d`；只想用官方镜像的话 `docker compose pull && docker compose up -d`。

质量门槛：

```bash
uv run ruff check src tests
uv run mypy src
uv run pytest tests --cov=codereview_ai --cov-fail-under=70
```

覆盖率总门槛 70%，核心模块（`forges` / `review` / `worker` / `queue` / `storage`）83%；CI 另跑 `pip-audit`。

桌面版打包：`uv run python scripts/build_exe.py`，产物在 `dist/codereview-ai/`（整个目录分发）；打 `v*` tag 推送会触发 `.github/workflows/desktop.yml`，在 windows-latest 上构建并把 `codereview-ai-windows-x64.zip` 挂到 GitHub Release。


## 许可证

[Apache License 2.0](./LICENSE)。

