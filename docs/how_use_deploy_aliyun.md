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

同目录建 `.env`：4 枚应用必配密钥（缺失时容器启动即 fail-fast 退出，日志里会打印对应生成命令）+ 3 条 PostgreSQL 变量（compose 插值就要用，缺了 `up` 直接报错）。

```bash
PGPASS=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
cat > /opt/codereview-ai/.env <<EOF
CR_SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')
CR_WEBHOOK_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')
CR_ADMIN_PASSWORD=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
CR_ENCRYPTION_KEY=$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')
CR_DB_USER=codereview
CR_DB_PASSWORD=$PGPASS
CR_DATABASE_URL=postgresql+asyncpg://codereview:$PGPASS@postgres:5432/codereview
EOF
chmod 600 /opt/codereview-ai/.env
```

密码用 `token_urlsafe` 生成是为了**连接串安全**：它的字符集（`A-Za-z0-9-_`）不含 `@ : / ? #`，可以直接拼进 URL，不必再做 percent-encoding——自己换密码时请留意这点。

`CR_ENCRYPTION_KEY` 必须恰是 Fernet 密钥（32 字节 urlsafe-base64，44 字符）——`token_urlsafe(48)` 生成的串不是合法 Fernet 格式，启动校验会明确拦下；上面这行的写法等价于 `Fernet.generate_key()`，且不依赖 cryptography 库。

`CR_ADMIN_PASSWORD` 就是后台（`/admin`）的登录口令，用户名固定 `admin`。注意它**只在空库首次启动时生效**（`seed_rbac` 在用户表非空时整体跳过），容器重启、改 `.env` 都不会改已存的密码；要换口令走后台用户页的「重置密码」（`POST /api/users/{id}/reset-password`），或删掉数据库从零重建（数据一并清空）。

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

## 数据存储（PostgreSQL）

生产用 PG，不用 SQLite。两个理由：SQLite 是单文件单写者，容器里跑的是长驻服务 + 定时任务 + 补拉轮询多路并发写，遇到「database is locked」只能靠重试；而 PG 让后续换机/扩容/接 RDS 只是换个连接串。SQLite 仍是**本地与桌面版**的默认档（README 的「单容器开箱即用」卖点不变），本项目源码按 URL 方言自动适配，两条路都走得通。

构成：`codereview-ai` 与 `postgres` 两个容器，PG 只在 compose 网络内、**不对宿主暴露 5432**（运维一律走 `docker compose exec`）。建表由 app 启动时的 `init_db` 自动完成，无需手写 DDL；`postgres` 容器在**空卷首启**时按 `POSTGRES_DB/USER/PASSWORD` 建库建账号。

小内存机的调参（都在 `docker-compose.prod.yml` 的 `command` 里）：

| 参数 | 值 | 为什么 |
|------|-----|--------|
| `shared_buffers` | 128MB | 默认值也是 128MB，但这台 2G 机上还跑着 MySQL/Redis/nginx 与另一个业务容器，不再往上加 |
| `max_connections` | 50 | 默认 100，PG 每连接有固定内存开销；本机只有单实例单用户应用 |
| `work_mem` / `maintenance_work_mem` | 4MB / 48MB | 排序与维护作业的每操作上限，压小防止大查询把整机内存吃光 |
| `effective_cache_size` | 512MB | 给规划器的「可缓存」估算，不是实际分配 |
| 容器内存上限 | 512M（app 768M） | 两容器合计仍给同机既有业务留余量 |

验证：

```bash
docker compose -f docker-compose.prod.yml ps                      # 两个服务都 healthy
docker compose -f docker-compose.prod.yml exec postgres pg_isready -U codereview
docker compose -f docker-compose.prod.yml exec postgres psql -U codereview -d codereview -c '\dt'   # init_db 建的表
curl -s http://localhost:5001/ready                               # {"status":"ok","checks":{"db":"ok"}}
```

备份与恢复（数据在命名卷 `pgdata`，`down` 不删）：

```bash
# 备份（自定义格式，支持选择性恢复；可加进 crontab）
docker compose -f docker-compose.prod.yml exec -T postgres pg_dump -U codereview -Fc codereview > ~/backup-$(date +%F).dump
# 恢复（先停 app 免写入竞争）
docker compose -f docker-compose.prod.yml stop codereview-ai
docker compose -f docker-compose.prod.yml exec -T postgres pg_restore -U codereview -d codereview --clean --if-exists < ~/backup-2026-10-08.dump
docker compose -f docker-compose.prod.yml start codereview-ai
```

两个「只在空卷首启生效」的坑，改之前先想清楚：

1. `CR_DB_USER` / `CR_DB_PASSWORD` 只在 `pgdata` **首次初始化**时写进数据库。之后改 `.env` 与库内账密就不再一致，app 会以「password authentication failed」连不上——要换账密得 `docker compose ... down -v` 删卷重建（**数据清空**），或进 `psql` 用 `ALTER USER` 改库、同时改 `.env` 里的连接串。
2. `CR_ADMIN_PASSWORD` 同理只在首次播种时生效（见上文）。

回退到 SQLite（本机临时排障用）：从 `.env` 里注释掉 `CR_DATABASE_URL`，`up -d` 后 app 会用挂载在 `/app/data` 的 `appdb` 卷——那是切 PG 之前的数据，仍在。

## 同机资源预算与长期运维

小内存云主机（2C2G 一档）上跑本服务的经验值，避免把宿主拖垮：

- **内存上限**：本文件里 app 768M + PG 512M 是按「同机还有别的业务」留的余量。要再开静态分析（semgrep 子进程数百 MB）或上调 PG，优先换机型或把 PG 换到 RDS，而不是抬这两个上限。
- **swap 不等于内存**：swap 满不代表在颠簸——真要看的是 `vmstat 1` 的 `si/so` 是否长期非 0、`dmesg` 有没有 OOM 记录。但 swap 满意味着**没有余量**，下一次尖峰就会被 OOM killer 处理（往往先杀 MySQL 这种大进程）。建议 swap ≥ 2GB；云盘 swap 慢，只当保险用。
- **长 uptime 的进程内存会漂移**：实测一台 96 天 uptime 的机器上，MySQL 空闲却从 ~400M 涨到 1.16G（其中 890M 被换出到 swap），重启一次即回收（干净 shutdown，几秒）。定期维护窗口重启大内存服务是划算的。
- **journald 默认无上限**：同一台机器上 journal 曾涨到 3.9G。设 `SystemMaxUse=100M`（`/etc/systemd/journald.conf`）后重启 `systemd-journald` 即生效。
- **容器日志**：本 compose 已限 3×10MB 轮转；没写 `logging` 的容器其 json 日志会一直涨，容器停了日志也还在盘上。
- **镜像堆积**：每次 `docker pull` 重推的 tag 都会留下无标签旧副本，工作流已在部署后执行 `docker image prune -f`（只删无标签且无容器引用的，带 tag 的本地镜像与 `app-<sha>` 回滚 tag 不受影响）。注意**已停止容器引用的镜像不会被 prune 回收**，那类要靠 `docker rm <容器>` 后再清理。

## 日常运维

```bash
cd /opt/codereview-ai
docker compose -f docker-compose.prod.yml ps            # 状态（app 与 postgres 都应 healthy）
docker compose -f docker-compose.prod.yml logs -f       # 全部日志
docker compose -f docker-compose.prod.yml logs -f postgres   # 只看数据库
docker compose -f docker-compose.prod.yml up -d         # 手动重启
docker compose -f docker-compose.prod.yml down          # 停止（pgdata/appdb 卷保留）
```

`up -d --remove-orphans` 由 CI 每次部署执行，重建容器不丢数据（PG 数据在 `pgdata` 卷）。SQLite 档的通用说明见 [how_use_postgres.md](how_use_postgres.md)。

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
| run 里 `.env 缺少变量：...` | 服务器 `.env` 未建或缺变量（见步骤 1）；名单由工作流预检打印 |
| 启动即退出、日志有 `CR_*` 缺失提示 | `.env` 少了必配密钥，或 `CR_ENCRYPTION_KEY` 不是合法 Fernet 密钥 |
| app 反复重启、日志 `password authentication failed` | `CR_DB_PASSWORD` 与 `pgdata` 首次初始化时写入的账密不一致（见「数据存储」两个坑） |
| `/ready` 返回 503 且 `db:unreachable` | PG 没起来或连接串写错；`logs postgres` 看库侧，`ps` 看是否 healthy |
| 健康检查超时 | 看 CI 打印的容器日志；多半是 `.env` 配置问题而非网络 |
| 容器 `unhealthy`、日志停在 `Building codereview-ai @ file:///app` 与成串 `Downloading` | 容器启动时 `uv run` 又同步了一次环境并从 PyPI 拉包（含 dev 依赖，跨境极慢）。镜像 CMD 已用 `uv run --no-sync`（构建期已 sync 完备）；自建镜像不要去掉这个参数 |
| 拉取很慢 | 服务器与 ACR 不同地域时退回公网域名；把 ACR 实例与 ECS 放同地域可走 VPC 内网 |
| `denied: unknown manifest class for application/vnd.oci.empty.v1+json` | buildx 默认的 provenance/sbom attestation 被 ACR 个人版拒收；工作流已置 `provenance/sbom: false` |
| 外部访问不通、服务器上 `curl localhost:5001/health` 正常 | 安全组没放行 5001/TCP |

## 与其它流水线的关系

- [ci.yml](../.github/workflows/ci.yml)：ruff + mypy + pytest 覆盖率门，push/PR 均跑，与部署并行（部署不等待测试，需要卡门禁可把本工作流改成 `workflow_run` 触发）；
- [docker.yml](../.github/workflows/docker.yml)：发布 GHCR 官方镜像与版本 tag，面向「docker compose pull」用户，保留不动；
- [desktop.yml](../.github/workflows/desktop.yml)：Windows 桌面版打包。
