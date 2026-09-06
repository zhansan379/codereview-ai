# 反面清单：原项目踩过的坑

> 全部来自对 `AI-Codereview-Gitlab` 的实际代码核验（部分为实测复现）。
> 新项目在设计和 code review 时，逐条对照，**这些坑一个都不要再踩**。

---

## 🔴 安全类（实测验证）

### A1. Webhook 无签名校验，token 兼作 secret

原代码 `biz/api/routes/webhook.py:53`：
```python
github_token = os.getenv('GITHUB_ACCESS_TOKEN') or request.headers.get('X-GitHub-Token')
```

配了环境变量后，**请求不带任何凭证也能通过**。公网任意人可 POST 触发 LLM 消费、
并借你的 token 往仓库写评论。全仓 grep 不到任何 `X-Hub-Signature-256` 校验。

**新项目要求**：
- `WEBHOOK_SECRET` 与平台 API token **完全分离**，是两个配置项
- 对原始 body bytes 做 HMAC，用 `hmac.compare_digest` 比对
- 校验失败返回 401，且**不要**在响应里说明失败原因
- 允许配置 IP 白名单作为第二道防线

### A2. Agentic 沙箱可绕过 → 任意代码执行

原代码 `biz/agent/tools/run_command.py:133` 用 `subprocess.run(cmd, shell=True)`，
但白名单**只检查第一个 token**（`:116-123`）。实测复现：

```
'echo hi && python -c "print(1+1)"'   → 未拦截，python 被执行（python 在黑名单里）
'ls && echo BLOCKLIST_REACHED'        → 未拦截
```

Linux 下（Docker 实际部署环境）`;`、`$(...)`、反引号同样有效。

敏感文件检查同样可绕——`:104` 只对含 `/` 或以 `.` 开头的 token 检查：
```
'cat ./id_rsa' → 拦截 ✅
'cat id_rsa'   → 未拦截，成功读出私钥内容 ❌
```

**新项目要求**（三选一，不要自己发明第四种）：
1. `shell=False` + `shlex.split` 后**逐 token** 校验，拒绝一切 shell 元字符
2. 整个 agent 会话跑在一次性容器里（只读挂载 + `--network=none` + 非 root + 资源限额）
3. 直接不提供 shell 工具，只给 `read_file` / `grep_repo` / `list_dir` 这类**结构化工具**

推荐 2 或 3。方案 3 在能力上损失很小，但把攻击面砍到几乎为零。

> ⚠️ 特别注意：被 review 的代码本身就是**不可信输入**。攻击者可以提一个 MR，
> 在代码注释里写 prompt injection 诱导 agent 执行命令。这不是理论风险。

### A3. Dashboard 密钥硬编码进仓库

`ui.py:56` 的默认 `SECRET_KEY` 是写死的 64 位十六进制串，
任何人都能拿它离线伪造 30 天有效的 auth cookie。配合 `ui.py:49-50` 默认 `admin/admin`。

**新项目要求**：无默认密钥。启动时若未配置 `SECRET_KEY` 直接 **fail-fast 退出**，
并在日志里打印一条生成命令（`python -c "import secrets;print(secrets.token_urlsafe(32))"`）。

### A4. 26 处 HTTP 调用零 timeout，14 处 `verify=False`

实测 grep：`requests.get/post/put` 共 26 处，**带 timeout 的 0 处**。
任一挂起请求会永久占住一个 worker 进程。同时 GitLab/Gitea 调用全部 `verify=False`——
不校验 TLS 却携带 access token。

**新项目要求**：统一封装一个 `HttpClient`，timeout 是构造参数且**无默认 None**；
禁止在业务代码里直接 `import httpx` 发请求。自签证书场景用 `SSL_CERT_FILE` 指定 CA，
而不是关掉校验。

### A5. 敏感信息进日志

`webhook.py:62` 把完整 payload（含全部源码 diff）打进 INFO 日志；
`dingtalk.py:27` 把含 access_token 的签名 URL 打进日志。

**新项目要求**：日志脱敏过滤器，对 token / key / secret / password / Authorization 统一打码；
payload 只记 event_type + repo + 编号，不记 body。

---

## 🟠 架构类

### B1. 队列是裸 fork（从 Redis 退化而来）

`biz/utils/queue.py` 全文 9 行：
```python
def handle_queue(function, data, token, url, url_slug):
    Process(target=function, args=(data, token, url, url_slug)).start()
```

无池、无上限、无 `join()`（僵尸进程堆积）、无持久化、无重试。
**重启即丢任务**。git 历史里有 `移除redis相关代码和配置`——是从 RQ 主动退回来的。

**新项目要求**：见 `DESIGN.md` 的队列抽象，任务必须有持久化状态机 + 重试 + 并发上限。

### B2. 幂等检查有 TOCTOU 竞态

`worker.py:183` 查 `check_mr_last_commit_id_exists`，但要到 LLM 跑完（数十秒后）
才在 `event_manager.py:39` 落库。中间窗口内并发 webhook 会重复 review + 重复推送。

**新项目要求**：**入队即落库**一条 `queued` 状态记录，用
`UNIQUE(provider, repo_id, pr_number, head_sha)` 约束抢占，靠数据库而不是靠先查后写。

### B3. LLM 错误被伪装成 review 结果

`biz/llm/client/deepseek.py:41-49`（所有 client 都是这个模式）：
```python
except Exception as e:
    return f"调用DeepSeek API时出错: {str(e)}"
```

API 挂了会**返回一个字符串**，这个字符串接着被当成 review 结果**发到 MR 评论里**、
被打分（匹配不到 → 0 分）、被存进数据库。调用方无从区分成功与失败。

**新项目要求**：LLM 层出错一律**抛异常**，永远不要把错误信息作为正常返回值。
任务状态机里 `failed` 是一个独立状态，失败的任务不回写评论。

### B4. 用正则从自由文本里捞分数

`code_reviewer.py:106`：
```python
match = re.search(r"总分[:：]\s*(\d+)分?", review_text)
```

换个模型、换个语言、模型多说一句话，分数就是 0。
**整个 Dashboard 的统计建立在这条正则上。**

**新项目要求**：用 structured output（JSON schema / function calling）。
评分、问题列表、文件行号全部结构化返回，解析失败就重试一次再判失败——不要静默降级成 0 分。

### B5. 三平台适配器纯复制粘贴

三个 `webhook_handler.py` 共 1068 行，github 和 gitea **恰好都是 369 行**，方法名逐一对应。
`worker.py` 里六个 handler 同样三套复制。
更荒诞的是 `webhook.py:9` 要从 **gitlab 包**里 import 通用的 `slugify_url` 给 github/gitea 用。

**新项目要求**：`ForgeAdapter` 抽象基类 + 中立领域模型。新增一个平台 = 新增一个文件，
不改动任何现有代码。

### B6. 截断策略过于朴素

`code_reviewer.py:79-81` 超过 `REVIEW_MAX_TOKENS` 就砍掉后面。
大 MR 会从中间截断，后面的文件**完全不被 review**，而且用户看不到任何提示。

**新项目要求**：按文件切分 + 优先级排序（改动大的、核心路径的优先）+ 分批 map-reduce。
被跳过的文件必须在报告里**显式列出**。

### B7. sqlite 裸用

六处 `with sqlite3.connect(...)`——注意 Python 里 `with` 对 sqlite 连接
**只提交事务、不关闭连接**，fd 会泄漏。没开 WAL、没设 `busy_timeout`，
而上面是 N 个并发进程写同一个文件，`database is locked` 是必然。
迁移是 ad-hoc `ALTER TABLE`（`review_service.py:48-71`）无版本表，
`init_db()` 在 import 时执行（`:218`），每个 fork 进程重跑一遍 DDL。

**新项目要求**：SQLAlchemy 2.0 + Alembic 迁移。sqlite 必须显式
`PRAGMA journal_mode=WAL` + `busy_timeout=5000`。

### B8. Agent 循环的两个小 bug（值得抄作业的反例）

- `runner.py:70-78`：空响应时 `continue` 但**不往 messages 里追加任何东西**，
  于是原样重发同一请求，必然再得到同样的空响应，白烧 3 次 LLM 调用才降级。
  → 正确做法：追加一条 user 消息明确提示"你没有产生有效输出，请给出最终结论或调用工具"。
- `runner.py:96`：每次工具调用后对**全部** messages 重算 token，O(n²) 分词。
  → 正确做法：增量累加。

---

## 🟡 工程类

### C1. 三个测试文件从未被执行

`biz/platforms/*/test_webhook_handler.py` 三个文件，
因为 `pytest.ini` 写了 `testpaths = tests`，**从写下那天起就没跑过**。
这比没有测试更危险——它提供了虚假的安全感。

**新项目要求**：CI 里加一条检查，扫描 `testpaths` 之外的 `test_*.py` 并报错。

### C2. 依赖管理混乱

实测：`Flask==3.0.3` 在 requirements.txt 里出现**两次**；
`python-dotenv` 被 6 个文件 import 却**未声明**（靠传递依赖侥幸可用）；
`spark-ai-python` 零引用的死依赖；pytest 三件套混进运行时依赖，随 Dockerfile 进了生产镜像。

**新项目要求**：`pyproject.toml` + `uv.lock`，运行时/开发依赖分组，CI 里跑 `uv sync --frozen`。

### C3. 测试只覆盖了一个模块

90 个测试**全在 `tests/agent/`**。`worker.py`（事件分发核心）、
7 个 LLM client、4 个 IM 渠道、`review_service` 零覆盖。

### C4. 无可观测性

无 `/health`、无 metrics、无 trace_id。日志给级别加 emoji 前缀（`⚠️`/`❌`），
破坏机器解析。用 Flask 开发服务器跑生产（`api.py:25`）。

### C5. 配置错了也能启动

`biz/utils/config_checker.py` 对缺失变量仅 `logger.warning` 不 fail-fast，
带病启动，问题延后到第一次 review 才暴露。

---

## ✅ 值得保留的好设计

公平起见，原项目这几点是对的，新项目应该继承：

1. **blinker 事件解耦**（`event_manager.py`）——把「review 完成」与「IM 推送 + 落库」解耦，
   这层切得很干净。新项目用 async 事件总线实现同样的思路。
2. **prompt 走 YAML 外置 + Jinja2 渲染**——比硬编码在 py 文件里好得多。
3. **`REVIEW_STYLE` 四种人格**（专业/毒舌/绅士/幽默）——这是该项目出圈的原因，
   技术上不值一提，但**产品传播价值极高**，务必保留并做得更好。
4. **按 project_name / url_slug 路由到不同 IM 群**（`dingtalk.py:49-58`）——
   多项目场景的真实刚需。实现方式（遍历环境变量）要换成数据库配置。
5. **`diff_only` / `agentic` 策略可插拔**——抽象方向是对的。
6. **changes API 延迟重试**——GitLab/GitHub 确实有这个延迟，
   知道要重试是踩过坑的经验，但要改成异步退避。
7. **`last_commit_id` 幂等键**的选择是正确的，只是用错了地方（见 B2）。
