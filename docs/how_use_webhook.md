### GitHub 添加教程如下：

1. 进入仓库页面，点击 **Settings → Webhooks → Add webhook**；
2. **Payload URL** 填本服务地址：`https://<你的服务地址>/webhook`；
3. **Content type** 选 `application/json`；
4. **Secret** 填与后端 `CR_WEBHOOK_SECRET` 一致的值（GitHub 用该密码做 HMAC-SHA256 签名校验，不一致会被 401 拒绝）；
5. **Which events would you like to trigger this webhook?** 选 **Let me select individual events**，然后勾选：
   - **Pull requests**（MR 审查必须）
   - **Pushes**（push 轨按需勾选）
6. 确保 **Active** 已勾选，点击 **Add webhook** 保存；
7. 先在平台配置页填好 GitHub 的 Token 与 URL（默认 `https://api.github.com`）并通过「测试连接」，审查回写评论才可用。

> 截图示例：

<img width="2526" height="1302" alt="image" src="https://github.com/user-attachments/assets/373320ba-11a2-49a6-b688-9faea5ab0c18" />

<img width="2549" height="1191" alt="image" src="https://github.com/user-attachments/assets/0250729c-b47a-4f86-b810-eff0d3005176" />

<img width="2560" height="1200" alt="image" src="https://github.com/user-attachments/assets/b06abfd4-7ebb-4638-972a-ea313564b6e6" />

也可以不配 webhook，改用「主动补拉」：在调度设置里启用 poll 轮询（或补拉页手动触发），系统会按项目定时拉取打开态 PR/MR 入队审查。

### gitee 添加步骤如下：

> 完整教程（Token 申请 + scope 说明 + 补拉轮询 + 常见问题）见 [docs/how_use_gitee.md](how_use_gitee.md)。

1. 仓库页面进入 **管理 → WebHooks → 添加 webHook**；
2. URL 填本服务地址：`https://<你的服务地址>/webhook`；
3. 密码填与后端 `CR_WEBHOOK_SECRET`一致的值（Gitee 用该密码做 HMAC 签名校验，不一致会被 401 拒绝）；
4. 事件勾选 **Pull Request**（push 轨按需勾选 **Push**）；
5. 先在平台配置页填好 Gitee 的 Token 与 URL（默认 `https://gitee.com/api/v5`）并通过「测试连接」，审查回写评论才可用。

也可以不配 webhook，改用「主动补拉」：在调度设置里启用 poll 轮询（或补拉页手动触发），系统会按项目定时拉取打开态 PR/MR 入队审查。

