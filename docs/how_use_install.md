# 安装与快速开始

> 返回 [README](../README.md)

三条部署路线，任选其一，装完后的配置步骤都一样：

- **官方镜像**（推荐）—— 免克隆，两步起服务，宿主机只要 Docker
- **克隆仓库本地构建** —— 要改代码、自己构建镜像
- **Windows 桌面单机版** —— 一个 exe，免 Docker 免 Python

## 方式一：拉取官方镜像，免克隆（推荐）

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

## 方式二：克隆仓库，本地构建

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

## 方式三：Windows 桌面单机版（exe，免 Docker 免 Python）

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

## 装完之后

后台登录后配置模型与平台，然后在 GitHub / GitLab / Gitee 添加 webhook 指向 `POST http://<你的主机>:5001/webhook`，打开一个 PR 即可看到审查评论。

- 平台接入：[GitHub 接入教程](how_use_github.md) / [Gitee 接入教程](how_use_gitee.md)
- webhook 漏事件、服务停机、存量 PR：[主动补拉（轮询轨）](how_use_poll.md)
- 换 PostgreSQL（standard 档）：[使用 PostgreSQL 存储](how_use_postgres.md)
- 部署到自己的服务器：[部署到阿里云 ECS（GitHub Actions 自动部署）](how_use_deploy_aliyun.md)

## 源码运行（不用 Docker）

```bash
uv sync
uv run uvicorn codereview_ai.main:app --host 0.0.0.0 --port 5001
```

管理后台在 `/admin`，交互式 API 文档在 `/docs`（`CR_OPENAPI_ENABLED=0` 可关闭），健康检查在 `/health`。完整配置项（`CR_` 前缀）与 API 清单见 `/docs` 与 `GET /openapi.json`。

## 开发

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
