# 部署到阿里云 ECS（GitHub Actions 自动部署）

> 返回 [README](../README.md)

push 到 `main` → GitHub Runner 构建镜像 → 推阿里云 ACR → SSH 让 ECS 拉取并重启容器，
单次端到端约 3~5 分钟。工作流文件：[.github/workflows/deploy-aliyun.yml](../.github/workflows/deploy-aliyun.yml)。

## 为什么是这个链路

| 方案 | 端到端 | 结论 |
|------|--------|------|
| Runner 构建 → 推 ACR → ECS 同地域 VPC 内网拉取 | ~3 分钟 | **采用**。ACR 并行分块上传跨境不慢，内网拉取免流量、秒级 |
| Runner 直接 SSH 传产物 | ~15 分钟 | 放弃。GitHub 海外 Runner 到国内 ECS 的 SSH 单流实测约 40KB/s |
| 服务器上编译 | — | 放弃。ECS 为 2C2G 且 swap 常满，`uv sync` + semgrep 装机必 OOM |

对应的两条硬约束：**镜像一律在 Runner 上构建**，**服务器只做 pull + up**。

## 首次部署

### 1. 服务器准备

```bash
ssh root@<服务器IP>
mkdir -p /opt/codereview-ai && cd /opt/codereview-ai
```

同目录建 `.env`，至少含 4 枚必配密钥（缺失时容器启动即 fail-fast 退出，日志里会打印对应生成命令）：

```bash
cat > /opt/codereview-ai/.env <<EOF
CR_SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')
CR_WEBHOOK_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')
CR_ADMIN_PASSWORD=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
CR_ENCRYPTION_KEY=$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')
EOF
chmod 600 /opt/codereview-ai/.env
```

`CR_ENCRYPTION_KEY` 必须恰是 Fernet 密钥（32 字节 urlsafe-base64，44 字符）——`token_urlsafe(48)` 生成的串不是合法 Fernet 格式，启动校验会明确拦下；上面这行的写法等价于 `Fernet.generate_key()`，且不依赖 cryptography 库。

`CR_ADMIN_PASSWORD` 就是后台（`/admin`）的登录口令，用户名固定 `admin`。注意它**只在空库首次启动时生效**（`seed_rbac` 在用户表非空时整体跳过），容器重启、改 `.env` 都不会改已存的密码；要换口令走后台用户页的「重置密码」（`POST /api/users/{id}/reset-password`），或删掉 `appdb` 卷从零重建（数据一并清空）。

模型与平台凭据（LLM 模型、GitLab/GitHub token、通知器等）**不必写进 .env**：在后台对应页面配置即可，配置落 DB 并在保存后热生效（env 同名变量永远优先于 DB）。若确实要用 env，把 `CR_LLM_MODEL` / `CR_LLM_API_KEY` / `CR_LLM_BASE_URL` / `CR_GITLAB_TOKEN` 等追加进同一 `.env`。

安全组放行入方向 `5001/TCP`（控制台 → 实例 → 安全组 → 入方向规则）。同机若 80/8080 已被占用，本服务固定用 5001。

### 2. 配置 GitHub Secrets

仓库 → Settings → Secrets and variables → Actions → New repository secret，共 8 个：

| Secret | 说明 |
|--------|------|
| `SSH_HOST` | 服务器公网 IP |
| `SSH_USER` | SSH 用户名（如 `root`） |
| `SSH_PORT` | SSH 端口（可选，缺省 22） |
| `SSH_PRIVATE_KEY` | 专用部署密钥的**私钥整文件内容** |
| `ACR_REGISTRY` | ACR 访问域名，如 `crpi-xxxxxxxx.cn-beijing.personal.cr.aliyuncs.com` |
| `ACR_NAMESPACE` | ACR 命名空间 |
| `ACR_DOCKER_USERNAME` / `ACR_DOCKER_PASSWORD` | ACR「访问凭证」页设置的固定密码对 |

生成专用部署密钥（别复用个人密钥），公钥装到服务器：

```bash
ssh-keygen -t ed25519 -f ~/.ssh/codereview-ai-deploy -N "" -C "codereview-ai-deploy"
ssh -i ~/.ssh/codereview-ai-deploy root@<服务器IP> "echo KEY_LOGIN_OK"   # 先在服务器 authorized_keys 装入公钥
gh secret set SSH_PRIVATE_KEY < ~/.ssh/codereview-ai-deploy
```

ACR 侧需要命名空间与镜像仓库 `codereview-ai` 已存在（个人版控制台「镜像仓库 → 创建镜像仓库」；命名空间已有时仓库名可直接推，推不存在的仓库会被拒）。

未配置 Secrets 时工作流会被 guard 步骤跳过并保持绿色，配齐后 push 即自动部署。

### 3. 触发与验证

```bash
gh workflow run "Deploy (Aliyun ECS)"     # 手动触发；或直接 push 到 main
gh run list --limit 3
gh run watch <run-id>
```

成功标志：run 末尾打印 `部署成功：http://<IP>:5001/admin`。浏览器打开 `/admin`，用 `admin` + `.env` 里的 `CR_ADMIN_PASSWORD` 登录。

## 日常运维

```bash
cd /opt/codereview-ai
docker compose -f docker-compose.prod.yml ps            # 状态（含 healthy）
docker compose -f docker-compose.prod.yml logs -f       # 日志
docker compose -f docker-compose.prod.yml up -d         # 手动重启
docker compose -f docker-compose.prod.yml down          # 停止（appdb 卷保留）
```

数据在命名卷 `appdb`（SQLite 落 `/app/data/app.db`），`down` 不删卷；`up -d --remove-orphans` 由 CI 执行，重建容器不丢数据。切 PostgreSQL 见 [how_use_postgres.md](how_use_postgres.md)。

### 回滚

CI 每次推送都会额外打 `app-<短sha>` tag（复用已上传层，几乎不增加成本），服务器上换成目标版本即可：

```bash
docker pull crpi-xxxxxxxx.cn-beijing.personal.cr.aliyuncs.com/<ns>/codereview-ai:app-<短sha>
docker tag  crpi-xxxxxxxx.cn-beijing.personal.cr.aliyuncs.com/<ns>/codereview-ai:app-<短sha> codereview-ai:latest
cd /opt/codereview-ai && docker compose -f docker-compose.prod.yml up -d
```

### 排查

| 现象 | 原因 |
|------|------|
| run 里 `缺少 /opt/codereview-ai/.env` | 服务器 `.env` 未建（见步骤 1） |
| 启动即退出、日志有 `CR_*` 缺失提示 | `.env` 少了必配密钥，或 `CR_ENCRYPTION_KEY` 不是合法 Fernet 密钥 |
| 健康检查超时 | 看 CI 打印的容器日志；多半是 `.env` 配置问题而非网络 |
| 容器 `unhealthy`、日志停在 `Building codereview-ai @ file:///app` 与成串 `Downloading` | 容器启动时 `uv run` 又同步了一次环境并从 PyPI 拉包（含 dev 依赖，跨境极慢）。镜像 CMD 已用 `uv run --no-sync`（构建期已 sync 完备）；自建镜像不要去掉这个参数 |
| 拉取很慢 | 服务器与 ACR 不同地域时退回公网域名；把 ACR 实例与 ECS 放同地域可走 VPC 内网 |
| `denied: unknown manifest class for application/vnd.oci.empty.v1+json` | buildx 默认的 provenance/sbom attestation 被 ACR 个人版拒收；工作流已置 `provenance/sbom: false` |
| 外部访问不通、服务器上 `curl localhost:5001/health` 正常 | 安全组没放行 5001/TCP |

## 与其它流水线的关系

- [ci.yml](../.github/workflows/ci.yml)：ruff + mypy + pytest 覆盖率门，push/PR 均跑，与部署并行（部署不等待测试，需要卡门禁可把本工作流改成 `workflow_run` 触发）；
- [docker.yml](../.github/workflows/docker.yml)：发布 GHCR 官方镜像与版本 tag，面向「docker compose pull」用户，保留不动；
- [desktop.yml](../.github/workflows/desktop.yml)：Windows 桌面版打包。
