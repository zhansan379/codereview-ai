### github 申请

1. 跳转到 https://github.com/settings/apps 页面
2. PAT 分两种，都在同一处：GitHub 头像 → Settings → Developer settings（左下角）→ Personal access tokens。

方式 A：Classic（最省事，推荐自托管场景）
- 申请页：https://github.com/settings/tokens
- 点 Generate new token (classic)，勾选 scope 为 repo（含私有仓库全部读写 + PR review + issue/commit 评论 + commit status）。

<img width="2549" height="1191" alt="image" src="https://github.com/user-attachments/assets/a6f7769d-dff3-4877-b4a5-0d7c3aae0868" />

方式 B：Fine-grained（更细粒度，权限最小化）
- 生成页：https://github.com/settings/personal-access-tokens/new
- 选 "Only select repositories"（限定你部署时要审查的仓库）；
- Permissions 里给 Contents: Read + Issues: Write + Pull requests: Write + Commit statuses: Write(或 Read and write)；
- 之所以要 Write（不只是 Read），是因为审查结果要写回平台侧无感知，需要：发行级 review（POST /pulls/{n}/reviews）、issue 总结评论、PR/commit 评论、以及可选失败阻合并的 commit status（POST /statuses/{sha}）。

<img width="2526" height="1176" alt="image" src="https://github.com/user-attachments/assets/5e19b81b-c3d9-4313-929a-465e78649c3d" />
