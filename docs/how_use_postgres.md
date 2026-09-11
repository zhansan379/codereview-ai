# 使用 PostgreSQL 存储

本平台默认用 SQLite（simple 档，零配置、单机够用）。从 v0.1.0 起支持可选切到 PostgreSQL
（standard 档），适合多实例、需要共享数据库的场景。切换只影响**数据库**，建表由 `init_db`
自动完成，无需手写 DDL。

## 两种用法

### 方式 A：docker compose（推荐，自动帮你装好 PG）

项目根 `.env` 里追加：

```bash
# PG 专用（compose 内网服务名是 postgres）
CR_DB_USER=codereview
CR_DB_PASSWORD=<换一个强密码>
CR_DATABASE_URL=postgresql+asyncpg://codereview:<换一个强密码>@postgres:5432/codereview
```

> **为什么方式 A 比方式 B 多两条（`CR_DB_USER` / `CR_DB_PASSWORD`）？** 方式 A 连的是**要现起的
> 空 postgres 容器**：它第一次建数据卷时，必须有人告诉它建什么用户、设什么密码（容器据此把
> 账密写进 `pgdata`）。app 从头到尾只用 `CR_DATABASE_URL` 连库，这两条是**给 postgres 容器
> 初始化**用的，只在空卷时生效一次；方式 B 连的是早已存在的实例，账密已建好，故只需 URL。

启动：

```bash
docker compose --profile postgres up -d
```

- `--profile postgres` 才会拉起 postgres 服务；不带则还是纯 SQLite，行为不变。
- postgres 首次建空卷时用 `POSTGRES_DB` **自动建库**；app 启动时 `init_db` 自动建全表。

确认：

```bash
docker compose --profile postgres ps          # 两个服务都 healthy
docker compose exec postgres pg_isready -U codereview
curl http://localhost:5001/health             # app 探活
```

数据持久化在 `pgdata` 命名卷，`docker compose down` 不丢。彻底清空重建：

```bash
docker compose --profile postgres down -v      # -v 才删 pgdata 卷（慎用，清空数据）
```

### 方式 B：连已有 PostgreSQL 实例（本地 / 云 / RDS）

不用 compose，直接把 `CR_DATABASE_URL` 指到你自己的 PG：

```bash
CR_DATABASE_URL=postgresql+asyncpg://myuser:mypass@localhost:5432/codereview
```

**两个前提（都要满足）：**

1. **库要先存在** —— 应用不会自动 `CREATE DATABASE`（这点和 SQLite 不同）。
   ```bash
   psql -U myuser -h localhost -c 'CREATE DATABASE codereview;'
   ```
2. **asyncpg 已装** —— 已写入项目依赖，`uv sync` 后即有。验证：
   ```bash
   uv run python -c "import asyncpg; print(asyncpg.__version__)"
   ```

建表自动完成，无需手动建。

## `CR_DATABASE_URL` 怎么改

连接串整体结构是：

```
postgresql+asyncpg://<用户>:<密码>@<主机>:<端口>/<库名>
```

逐段对照怎么改：

| 段 | 含义 | 方式 A（compose） | 方式 B（已有实例） |
|---|---|---|---|
| `postgresql+asyncpg` | 方言 + 异步驱动，**固定不用改** | — | — |
| `<用户>` / `<密码>` | 连库账密 | 同 `CR_DB_USER`/`CR_DB_PASSWORD` | 实例里已有的账密 |
| `<主机>` | 数据库地址 | **服务名** `postgres`（compose 内网） | IP 或域名，如 `localhost` |
| `<端口>` | PG 端口 | `5432` | 实例实际端口 |
| `<库名>` | 连哪个库 | `codereview`（须与 `POSTGRES_DB` 一致） | 你建的那个库名 |

改的时候只需动**密码、主机、端口、库名**这几段；前面的 `postgresql+asyncpg://` 是固定的，
`<用户>:<密码>` 要和库的实际账密对得上。compose 里主机写**服务名 `postgres`**、方式 B 本地写
`localhost`，两者不通用。

## 从 SQLite 迁移

**没有自动迁移工具**（Alembic 已移除），两条路：

- **干净起步（推荐）**：新起一个空 `codereview` 库直接启动，`create_all` 自动建全表。
- **要保留已有审查数据**：自行搬库（如 `pgloader`）。留意差异：
  - PG 的 `DateTime(timezone=True)` 读回是**带时区**的，SQLite 是 naive，搬后可能需归一化。
  - 自增主键 SQLite 是 `INTEGER PRIMARY KEY`、PG 是 `SERIAL`。
  - 带 `WHERE event_type='mr'` 的部分唯一索引两方言都已定义，可对齐。

大多数场景直接新起 PG 库即可，不必搬旧数据。

## 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `database "codereview" does not exist` | 库没建。compose 用 `--profile postgres` 会自动建；裸连需手动 `CREATE DATABASE` |
| `ModuleNotFoundError: asyncpg` | 环境没装。`uv sync` 后重试 |
| 默认 `docker compose up` 没起 PG | 正常，profile 隔离，需加 `--profile postgres` |
| app 连不上 | host 在 compose 内是 `postgres`、本地是 `localhost`；检查 `.env` |

## 说明

本次实现只把**存储层**接到了 PostgreSQL（建表 + upsert 方言化），队列仍是单机的 asyncio
后端。