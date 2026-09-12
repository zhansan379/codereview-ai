# Gitee 接入教程

Gitee 支持**两条审查触发通道**，可任选也可同时用：

| 通道 | 实时性 | 要求 |
| --- | --- | --- |
| Webhook（实时轨） | PR 打开/更新即审 | Gitee 服务器能访问到你的服务地址 |
| 主动补拉（轮询轨） | 按间隔或手动触发 | 无外网暴露要求，适合本地/内网部署 |

无论哪条通道，都必须先完成**第一步：申请 Token** 并配置进本服务。

---

## 第一步：申请 Gitee 私人令牌（Token）

1. 登录 Gitee，进入 **头像 → 设置 → 安全设置 → 私人令牌**（直达：<https://gitee.com/profile/personal_access_tokens>）；
2. 点 **生成新令牌**，勾选以下权限（scope）：

   | scope | 用途 |
   | --- | --- |
   | `user_info` | 「测试连接」探测（GET /user） |
   | `projects` | 读仓库/PR files、写 commit status |
   | `pull_requests` | 补拉 PR 列表、读 PR 详情与 diff |
   | `issues` / `notes` | 发 PR 总结评论与行级评论 |

   > 全部勾选最省事；最小化按上表勾即可。**Webhook 的创建在本仓库网页上手工完成，不需要 `hook` 权限。**
3. 提交后**令牌只显示一次**，立即复制保存。

## 第二步：把 Token 配进本服务

两种方式二选一（环境变量优先级更高，页面保存的作为兜底）。

**方式 A：后台页面（推荐，热更生效）**

1. 登录管理后台 → **平台接入** 页 → Gitee 卡片；
2. URL 保持默认 `https://gitee.com/api/v5`（自建 Gitee 企业版才需要改）；
3. Token 粘贴第一步的令牌 → 点 **测试连接**，能力矩阵会逐项显示连通/读/写权限是否 OK；
4. 保存，立即生效，无需重启。

**方式 B：环境变量**

```bash
CR_GITEE_TOKEN=<你的私人令牌>
# 可省略，默认即 https://gitee.com/api/v5
CR_GITEE_URL=https://gitee.com/api/v5
```

## 第三步：新增项目

管理后台 → **项目管理** → 新增项目：

- 代码托管平台选 **Gitee**；
- 仓库 URL 填仓库首页链接（如 `https://gitee.com/acme/widgets`）→ 点解析，自动回填 `repo_id`；
- 按需开启 MR 自动审查等选项后保存。

## 第四步：配置 Gitee Webhook（实时轨）

1. 打开要审查的 Gitee 仓库 → **管理 → WebHooks → 添加 webHook**；
2. **URL**：`https://<你的服务地址>/webhook`（注意路径是 `/webhook`）；
3. **WebHook 密码/签名验证**：勾选并填入服务端 `CR_WEBHOOK_SECRET` 的值。
   Gitee 会用该密码对每次请求做 HMAC-SHA256 签名（`X-Gitee-Token` + `X-Gitee-Timestamp` 请求头），服务端校验不过返回 401，所以**必须一致**；
4. **事件**：勾选 **Pull Request**（push 轨审查另勾 **Push**）；
5. 添加后点 **测试**，服务日志出现 202 入队记录即通。

> 本地开发时 Gitee 公网访问不到 `localhost`，需要内网穿透（frp / ngrok / 花生壳等）把服务暴露出去；或者跳过本步，直接用第五步补拉。

## 第五步（可选）：主动补拉（轮询轨）

不开 webhook 也能自动审查，三种触发方式：

- **调度页**：配置 poll 类型的定时任务（如每小时一轮）；
- **环境变量**：`.env` 里 `CR_POLL_ENABLED=true`、`CR_POLL_INTERVAL_SECONDS=3600`；
- **手动**：补拉页点「立即补拉」，立即扫一轮所有启用项目。

补拉范围默认只拉**打开态** PR/MR；要连已关闭/已合并一起拉（用于回溯补审），开 `CR_POLL_INCLUDE_CLOSED=true` 或后台「补拉范围」开关。已审过的同一 head 提交会被增量决策自动跳过，重复补拉天然幂等。

## 验证闭环

1. 在配置好的 Gitee 仓库里提一个 PR（或用补拉手动触发）；
2. 任务列表出现该 PR 的审查任务，跑完状态变绿；
3. 回到 Gitee PR 页面：应能看到总结评论 + 行级评论（评分/告警按项目配置）。

## 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| webhook 返回 401 invalid signature | WebHook 密码与 `CR_WEBHOOK_SECRET` 不一致，两边改成同一个值 |
| 「测试连接」失败 | Token 填错/过期，或 URL 不是 `https://gitee.com/api/v5`（别填 `gitee.com` 首页地址） |
| 能连上但读不了 PR | Token 缺 `pull_requests` / `projects` scope，回到第一步补勾后重新生成 |
| webhook 请求到达但没产生审查 | 依次查：平台凭据与 LLM 是否配齐（worker 未启动会只入队不审查）、项目是否启用、事件 action 是否在触发范围（open / update / reopen；评论、合并动作不触发） |
| 总结评论发了但行级评论少了几条 | 行级评论只挂**在 diff 中出现的行**上，不在 diff 的行会被跳过；另确认 Token 有 `notes` 权限 |
| docker compose 部署时 webhook 收不到 | 容器内 `localhost` 指容器自身，URL 要写宿主机可达地址或让 Gitee 能路由到 compose 映射出的端口 |
