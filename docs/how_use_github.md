# GitHub 接入教程

从零把 GitHub 仓库接进审查：申请 Personal Access Token → 配进本服务 → 新增项目 → 配置 webhook（或主动补拉）→ 验证。Gitee 接入见 [Gitee 接入教程](how_use_gitee.md)。

GitHub 支持**两条审查触发通道**，可任选也可同时用：

| 通道 | 实时性 | 要求 |
| --- | --- | --- |
| Webhook（实时轨） | PR 打开/更新即审 | GitHub 服务器能访问到你的服务地址 |
| [主动补拉（轮询轨）](how_use_poll.md) | 按间隔或手动触发 | 无外网暴露要求，适合本地/内网部署 |

无论哪条通道，都必须先完成**第一步：申请 Token** 并配置进本服务。

## 第一步：申请 Personal Access Token（PAT）

PAT 分两种，入口在同一处：GitHub 头像 → Settings → Developer settings（左下角）→ Personal access tokens。

**方式 A：Classic（最省事，推荐自托管场景）**

- 申请页：https://github.com/settings/tokens
- 点 Generate new token (classic)，勾选 scope 为 repo（含私有仓库全部读写 + PR review + issue/commit 评论 + commit status）。

<img width="2549" height="1191" alt="image" src="https://github.com/user-attachments/assets/a6f7769d-dff3-4877-b4a5-0d7c3aae0868" />

**方式 B：Fine-grained（更细粒度，权限最小化）**

- 生成页：https://github.com/settings/personal-access-tokens/new
- 选 "Only select repositories"（限定你部署时要审查的仓库）；
- Permissions 里给 Contents: Read + Issues: Write + Pull requests: Write + Commit statuses: Write(或 Read and write)；
- 之所以要 Write（不只是 Read），是因为审查结果要写回平台侧无感知，需要：发行级 review（POST /pulls/{n}/reviews）、issue 总结评论、PR/commit 评论、以及可选失败阻合并的 commit status（POST /statuses/{sha}）。

<img width="2526" height="1176" alt="image" src="https://github.com/user-attachments/assets/5e19b81b-c3d9-4313-929a-465e78649c3d" />

## 第二步：把 Token 配进本服务

两种方式二选一（环境变量优先级更高，页面保存的作为兜底）。

**方式 A：后台页面（推荐，热更生效）**

1. 登录管理后台 → **平台接入** 页 → GitHub 卡片；
2. URL 保持默认 `https://api.github.com`（GitHub Enterprise Server 才需要改）；
3. Token 粘贴第一步的 PAT → 点 **测试连接**；
4. 保存，立即生效，无需重启。

<img width="2549" height="1191" alt="image" src="https://github.com/user-attachments/assets/da0bbb4f-e127-4c2f-9172-5eedb3a95149" />

**方式 B：环境变量**

```bash
CR_GITHUB_TOKEN=<你的 PAT>
# 可省略，默认即 https://api.github.com
CR_GITHUB_URL=https://api.github.com
```

## 第三步：新增项目

管理后台 → **项目管理** → 新增项目：

- 代码托管平台选 **GitHub**；
- 仓库 URL 填仓库首页链接（如 `https://github.com/acme/widgets`）→ 点解析，自动回填 `repo_id`；
- 按需开启 PR 自动审查等选项后保存。

## 第四步：配置 Webhook（实时轨）

1. 进入仓库页面，点击 **Settings → Webhooks → Add webhook**；
2. **Payload URL** 填本服务地址：`https://<你的服务地址>/webhook`；
3. **Content type** 选 `application/json`；
4. **Secret**（在 Content type 下方，可能需要向下滚动）：填与后端 `CR_WEBHOOK_SECRET` 一致的值（GitHub 用该密码做 HMAC-SHA256 签名校验，不一致会被 401 拒绝）；
5. **Which events would you like to trigger this webhook?** 选 **Let me select individual events**，然后勾选：
   - **Pull requests**（PR 审查必须）
   - **Pushes**（push 轨按需勾选）
6. 确保 **Active** 已勾选，点击 **Add webhook** 保存。

> 截图示例：

<img width="2526" height="1302" alt="image" src="https://github.com/user-attachments/assets/373320ba-11a2-49a6-b688-9faea5ab0c18" />

<img width="2526" height="1189" alt="image" src="https://github.com/user-attachments/assets/2cb309cd-fb31-46a1-8f55-5167489e4655" />

<img width="2560" height="1200" alt="image" src="https://github.com/user-attachments/assets/b06abfd4-7ebb-4638-972a-ea313564b6e6" />

> 本地开发时 GitHub 公网访问不到 `localhost`，需要内网穿透（frp / ngrok 等）把服务暴露出去；或者不用 webhook，改用[主动补拉](how_use_poll.md)。

## 第五步（可选）：主动补拉（轮询轨）

本地/内网部署无法暴露公网、webhook 漏了事件、或接入前已有存量 PR 时，可以不开 webhook，改用主动补拉兜底。触发配置与平台无关：三种触发方式、补拉范围与幂等说明见[主动补拉（轮询轨）](how_use_poll.md)。

<img width="2560" height="1200" alt="image" src="https://github.com/user-attachments/assets/3c6e58f6-fa10-4b17-b042-e8ccaada1250" />

## 验证闭环

1. 在接入的 GitHub 仓库里开一个 PR（或用补拉手动触发）；
2. 任务列表出现该 PR 的审查任务，跑完状态变绿；
3. 回到 GitHub PR 页面：应能看到总结评论 + 行级评论（评分/告警按项目配置）。

## 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| webhook 返回 401 invalid signature | WebHook 密码与 `CR_WEBHOOK_SECRET` 不一致，两边改成同一个值 |
| 「测试连接」失败 | Token 填错/过期，或 URL 不是 `https://api.github.com`（GitHub Enterprise Server 填实例的 API 地址） |
| 能连上但读不了 PR | Fine-grained Token 缺 Contents / Pull requests 权限，回到第一步补勾后重新生成 |
| webhook 请求到达但没产生审查 | 依次查：平台凭据与 LLM 是否配齐（worker 未启动会只入队不审查）、项目是否启用、事件 action 是否在触发范围（opened / synchronize / reopened；评论、关闭动作不触发） |
| 总结评论发了但行级评论少了几条 | 行级评论只挂**在 diff 中出现的行**上，不在 diff 的行会被跳过；另确认 PAT 有 Pull requests / Issues 写权限 |
| docker compose 部署时 webhook 收不到 | 容器内 `localhost` 指容器自身，URL 要写宿主机可达地址或让 GitHub 能路由到 compose 映射出的端口 |
