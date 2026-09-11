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