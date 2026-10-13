# <img width="24" height="24" alt="image" src="https://github.com/user-attachments/assets/d108c6ef-3293-4bf9-87fc-777cddebeac2" /> codereview-ai

一个部署在你自己机器上的代码审查服务。在 GitHub / GitLab / Gitee 挂一个 webhook，之后每一个打开或更新的 PR / MR 都会被自动审查，意见以行级 inline 评论写回改动所在的那一行，同时推送到钉钉 / 飞书 / 企微。webhook 万一漏了事件，或者项目刚接入时已经有一批 PR 开着，后台可以主动补拉一轮把它们捞回来。配置、模型、项目、权限、统计都在自带的 Vue 管理后台里。

分组 / 门控 / 预算闸门移植自 **open-code-review**（阿里巴巴），跨组去重沿用 **pr-agent** 的内容指纹，平台形态最接近 **AI-Codereview-Gitlab**。

## 功能一览

- **三平台 webhook 接入** — GitHub / GitLab / Gitee，PR / MR 打开或更新即自动审查
- **IM 推送** — 钉钉 / 飞书 / 企业微信机器人，审查完成即推，带 findings 明细与评分
- **三层审查管线** — LLM diff 审查 + 按需触发的 agentic 推理 + 可选 semgrep 静态分析
- **跨轮缓存** — 相邻轮次未变文件按内容哈希复用上一轮结论，高频 PR 省钱
- **自托管管理后台** — 仪表盘、审查记录、项目管理、成员分析、提交分析、IM 通知与凭据配置
- **私有化部署** — Docker 单容器 + SQLite 开箱即用，需要时切 PostgreSQL；代码不出内网

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

## 快速开始

三条路线任选，装完后的配置步骤都一样：

- **官方镜像**（推荐）—— 免克隆，两步起服务，宿主机只要 Docker
- **克隆仓库本地构建** —— 要改代码、自己构建镜像
- **Windows 桌面单机版** —— 一个 exe，免 Docker 免 Python

起服务后打开 `http://localhost:5001/admin` 登录后台，配置模型与平台，然后在 GitHub / GitLab / Gitee 添加 webhook 指向 `POST http://<你的主机>:5001/webhook`，打开一个 PR 即可看到审查评论。

完整步骤（密钥生成、源码运行、开发与质量门槛）见 [安装与快速开始](docs/how_use_install.md)。

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

## 许可证

[Apache License 2.0](./LICENSE)。

