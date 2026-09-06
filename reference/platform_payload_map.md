# 代码托管平台 Webhook 字段与 API 对照表

> 来源：从 `AI-Codereview-Gitlab` 的三份 `webhook_handler.py` 中提取并核对。
> 这是整份 reference 里**最有复用价值**的内容——它是别人踩过坑才摸清的字段路径，
> 照抄可以省掉大量翻文档和抓包的时间。
>
> ⚠️ 原项目所有 API 调用都**没有 timeout**、GitLab/Gitea 还带 `verify=False`。
> 复用字段路径，但不要复用它的调用方式。见 `antipatterns.md`。

---

## 1. 事件识别

| 平台 | 识别方式 | 事件值 |
|---|---|---|
| GitLab | body 里的 `object_kind` | `merge_request` / `push` |
| GitHub | HTTP header `X-GitHub-Event` | `pull_request` / `push` |
| Gitea | HTTP header `X-Gitea-Event` | `pull_request` / `push` |
| Gitee | HTTP header `X-Gitee-Event` | `Merge Request Hook` / `Push Hook` |

**注意分流顺序**：Gitea 的请求**同时**会带 `X-GitHub-Event`（它刻意兼容 GitHub），
所以必须**先判断 `X-Gitea-Event`**，否则 Gitea 事件会被错认成 GitHub。
原项目 `biz/api/routes/webhook.py:38-43` 就是这么处理的，这个顺序不能反。

---

## 2. 签名校验（原项目完全没做，新项目必须做）

| 平台 | Header | 算法 |
|---|---|---|
| GitHub | `X-Hub-Signature-256` | `sha256=` + HMAC-SHA256(secret, raw_body) |
| GitLab | `X-Gitlab-Token` | 明文 token 直接比对（GitLab 不提供 HMAC） |
| Gitea | `X-Gitea-Signature` | HMAC-SHA256(secret, raw_body)，无前缀 |
| Gitee | `X-Gitee-Token` + `X-Gitee-Timestamp` | HMAC-SHA256(secret, `timestamp\nsecret`) 后 base64 |

**关键实现细节**：必须对**原始 bytes**做 HMAC，不能对 `json.dumps(parsed)` 的结果做——
重新序列化会改变空格和键顺序，签名必然对不上。
FastAPI 里要用 `await request.body()` 拿原始字节，且要在 `request.json()` 之前或复用同一份 bytes。

所有比对一律用 `hmac.compare_digest`，不要用 `==`（时序攻击）。

---

## 3. Merge Request / Pull Request 字段路径

| 语义 | GitLab | GitHub | Gitea |
|---|---|---|---|
| 事件动作 | `object_attributes.action` | `action` | `action` |
| MR/PR 编号 | `object_attributes.iid` | `pull_request.number` | `pull_request.number` |
| 项目标识 | `object_attributes.target_project_id` | `repository.full_name` | `repository.full_name` |
| 项目名 | `project.name` | `repository.name` | `repository.name` |
| 项目 URL | `project.web_url` | `repository.html_url` | `repository.html_url` |
| 源分支 | `object_attributes.source_branch` | `pull_request.head.ref` | `pull_request.head.ref` |
| 目标分支 | `object_attributes.target_branch` | `pull_request.base.ref` | `pull_request.base.ref` |
| **最新 commit sha** | `object_attributes.last_commit.id` | `pull_request.head.sha` | `pull_request.head.sha` |
| 提交人 | `user.username` | `sender.login` | `sender.login` |
| 标题 | `object_attributes.title` | `pull_request.title` | `pull_request.title` |

> `last_commit sha` 是**幂等键的核心**。Gitea 有时 `head.sha` 缺失，
> 原项目 `worker.py:453` 的兜底链是：`head.sha` → `merge_commit_sha` → `last_commit_id`，
> 这个兜底值得保留。

**该处理哪些 action**：只处理 `open` / `opened` / `reopen` / `reopened` / `update` / `synchronize`。
`close` / `merge` 不该触发 review。原项目对此过滤得不严，会产生无意义的 review。

---

## 4. 拉取变更内容

### GitLab
```
GET {gitlab_url}/api/v4/projects/{project_id}/merge_requests/{iid}/changes?access_raw_diffs=true
Header: Private-Token: {token}
→ response.json()["changes"]  # list[{old_path, new_path, diff, new_file, deleted_file, renamed_file}]
```

### GitHub
```
GET https://api.github.com/repos/{owner}/{repo}/pulls/{number}/files
Header: Authorization: Bearer {token}
        Accept: application/vnd.github.v3+json
→ list[{filename, patch, additions, deletions, status, sha}]
```

### Gitea
```
GET {gitea_url}/api/v1/repos/{owner}/{repo}/pulls/{index}.diff
Header: Authorization: token {token}
→ 纯文本 unified diff，需要自己切分成 per-file
```

**统一成内部模型**（原项目把 GitHub 的结果手工转成 GitLab 格式，这个思路对，
但方向反了——不该以某个平台的格式为准，而应该定义中立模型）：

```python
@dataclass
class FileDiff:
    old_path: str
    new_path: str
    diff: str            # unified diff hunk 文本
    additions: int
    deletions: int
    is_new: bool
    is_deleted: bool
    is_renamed: bool
```

### ⚠️ changes API 的延迟陷阱
GitLab / GitHub 在 MR 刚创建时，changes API 可能返回**空数组**（服务端还在算 diff）。
原项目的处理是重试 3 次、每次 `time.sleep(10)`（`gitlab/webhook_handler.py:82-84`）。

思路正确但实现有问题：**同步 sleep 会阻塞整个 worker**。
新项目应该用 `asyncio.sleep` + 指数退避，或者干脆把任务重新入队延迟执行。

---

## 5. 回写评论

### 5.1 整体评论（summary note）

| 平台 | 端点 |
|---|---|
| GitLab MR | `POST /api/v4/projects/{id}/merge_requests/{iid}/notes`  body: `{"body": "..."}` |
| GitLab Commit | `POST /api/v4/projects/{id}/repository/commits/{sha}/comments`  body: `{"note": "..."}` |
| GitHub PR | `POST /repos/{owner}/{repo}/issues/{number}/comments`  body: `{"body": "..."}` |
| GitHub Commit | `POST /repos/{owner}/{repo}/commits/{sha}/comments`  body: `{"body": "..."}` |
| Gitea PR | `POST /api/v1/repos/{owner}/{repo}/issues/{index}/comments` |

> 注意 GitLab commit 评论的字段是 `note` 不是 `body`，这是个容易踩的不一致。

### 5.2 行级评论（原项目没有，是新项目的核心差异点）

**GitLab —— 需要构造 position 对象，这是最麻烦的一步：**
```
POST /api/v4/projects/{id}/merge_requests/{iid}/discussions
{
  "body": "评论内容",
  "position": {
    "position_type": "text",
    "base_sha":  "<diff_refs.base_sha>",
    "head_sha":  "<diff_refs.head_sha>",
    "start_sha": "<diff_refs.start_sha>",
    "new_path":  "src/foo.py",
    "old_path":  "src/foo.py",
    "new_line":  42
  }
}
```
三个 sha 必须从 `GET /merge_requests/{iid}` 响应里的 **`diff_refs`** 字段取，
不能自己拼。取错会返回 400 且错误信息很不友好。

对删除的行用 `old_line`，新增/上下文行用 `new_line`。两者都给会报错。

**GitHub —— 相对简单，推荐用批量 review 而不是逐条评论：**
```
POST /repos/{owner}/{repo}/pulls/{number}/reviews
{
  "commit_id": "<head sha>",
  "event": "COMMENT",
  "body": "总体评价",
  "comments": [
    {"path": "src/foo.py", "line": 42, "side": "RIGHT", "body": "..."},
    {"path": "src/bar.py", "start_line": 10, "line": 15, "side": "RIGHT", "body": "..."}
  ]
}
```
一次请求提交全部行级评论 + 总结，**只产生一封通知邮件**，体验远好于逐条 POST。
GitLab 没有等价的批量接口，只能逐条 POST discussions（记得限流）。

### 5.3 行级评论的关键约束

> 这是做 inline comment 最容易翻车的地方，务必在设计时就考虑：

**两个平台都只接受落在 diff hunk 范围内的行号。**
LLM 很容易指出一个"文件里存在但本次没改动"的行，这种评论会被 API 拒绝（422）。

因此必须有一个**行号校验层**：解析 diff 拿到每个文件的可评论行号集合，
LLM 给出的 finding 若不在集合内，就降级并入 summary 评论，而不是直接丢弃或硬提交。

---

## 6. Push 事件

| 语义 | GitLab | GitHub | Gitea |
|---|---|---|---|
| 分支 | `ref`（`refs/heads/xxx`，需截取） | `ref` | `ref` |
| commit 列表 | `commits` | `commits` | `commits` |
| before / after | `before` / `after` | `before` / `after` | `before` / `after` |

**取 push 的变更**用 compare API 而不是逐个 commit 拉 diff：
```
GitLab: GET /api/v4/projects/{id}/repository/compare?from={before}&to={after}
GitHub: GET /repos/{owner}/{repo}/compare/{before}...{after}
```

新分支首次 push 时 `before` 全是 `0000...`，compare 会失败，需要特判。
**勘误**：旧项目其实处理了（`biz/platforms/gitlab/webhook_handler.py:318-324`）——
`before` 全 0 时改用**单 commit diff API** 拉首个提交的差量；`after` 全 0（删分支）则直接返回空。
新项目按 §7.7 的三分支照抄，但要加上 timeout/重试与幂等，别学它无幂等会重复审。

---

## 7. 分支保护判断

原项目有个 `target_branch_protected()`，用途是"只 review 合入受保护分支的 MR"：
```
GET /api/v4/projects/{id}/protected_branches
```
这个产品思路值得保留（避免 review 大量 feature→feature 的无意义 MR），
但**不该硬编码成规则**——新项目应做成项目级可配置的分支白名单/正则。
