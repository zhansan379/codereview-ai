# OCR 优点详细拆解与复用决策

> 对象：`D:\Project\goProject\open-code-review-main` —— 阿里 OpenCodeReview，Go 写的 AI 代码审查 CLI。
> 用途：把它的设计精华拆开，供 codereview-ai 复用。每一节末尾标注**复用决策**（✅抄 / ✂️改造 / 📖借鉴即可 / ❌不抄）。
> 配套：确定性定位的 Python 移植见 [`diff_anchor.py`](diff_anchor.py)。

一句话总结：**OCR 是「确定性工程 × Agent 混合驱动」**——凡是不能错的事（文件筛选、位置定位、打包、规则匹配）全用工程逻辑，只有该动态决策的事（召回上下文、权衡）才交给 LLM。而旧 Python 版和我们初稿最大的问题，恰恰是**把这些确定性的活也交给了 LLM**（llm 拍行号、正则捞分数、不加约束）。

---

## 1. 确定性行级定位（最高价值，✅ 已移植 diff_anchor.py）

**核心思想：不让 LLM 输出精确行号，让它输出它看到的代码片段 `existing_code`，系统用片段在 diff 里做纯字符串匹配定位真实行号。**

LLM 输出的 `LlmComment` 关键字段（`internal/model/review.go:7`）：

```go
type LlmComment struct {
    Path           string   `json:"path"`
    Content        string   `json:"content"`
    SuggestionCode string   `json:"suggestion_code,omitempty"` // 建议的新代码
    ExistingCode   string   `json:"existing_code,omitempty"`   // 指代问题的原代码片段 —— LLM 只出这个
    StartLine, EndLine int  // 由 diff.ResolveLineNumbers 钉出来的真实行号，不是 LLM 拍的
    Thinking       string   `json:"thinking,omitempty"`
    Category       string   // bug|security|performance|maintainability|test|style|documentation|other
    Severity       string   // critical|high|medium|low
}
```

**定位四件套**（`internal/diff/resolver.go` + `relocation.go`，Python 移植见 `diff_anchor.py`）：
1. **hunk 内匹配** `resolveFromHunk`：拿 `existing_code` 逐行 normalize（TrimSpace + 剥 `+`/`-` 前缀），先新侧后旧侧，只匹配**连续**非空行序列。
2. **全文兜底** `resolveFromFileContent`：hunk 没命中，扫描整个新文件内容，同样跳过空行做连续匹配（blank-tolerant）。
3. **跨文件迁移** `RelocateAcrossFiles`：片段其实属于另一个文件（声明/实现拆分常见），**纯字符串匹配**找唯一命中才迁移文件、path/行号一起改；0/多命中都放弃——样板代码合法出现在多文件时，猜不如不猜。
4. **LLM 重定位** `ReLocationTask`：以上全失败的极端兜底才调 LLM（`relocation.go:BuildReLocationMessages`）。

**为什么必须抄**：行号在 LLM 上下文里极不稳定，直接要求它输出 file+line 必然漂移——这正是鉴权 benchmark 里"位置漂移"缺陷的根因。换 LHL 自己出 snippet、工程定位行号后，Locality 从根上保证。**这彻底替换掉我们初稿里的 `linevalidate`（LLM 拍行号 + 我们校验）方案。**

---

## 2. Agent 主循环与编排（✂️ 大改抄）

### 大循环结构（`internal/agent/agent.go:Run` → `internal/llmloop/loop.go:RunMainTask`）

```
parse diffs → injectDiffMap(全量diff只读映射) → filterDiffs(确定性筛选)
  → 成本预估(仅在配置了预算时) → dispatchSubtasks(每 FileGroup 一个 goroutine)
     → per-group: plan(可跳过) → tool-loop → AsyncCodeComment → 内存压缩
  → 全部组完成的评论 → collector 汇总
```

- **注入 DiffMap**：`agent.go` 把**所有**解析出的 diff（含被滤掉的）做成只读映射，让 LLM 可以查询关联文件的 diff —— 避免"滤掉的调用方是实现看不到上下文"。
- **工具注册表 + skill 声明**：`tool.Registry` 集中注册；`Tool.to_schema()` 单一来源生成 schema 喂给 LLM。
- **每文件组一个独立对话**（key=`fileGroupKey`），组内上下文隔离、组间天然并发 → 超大变更稳定且可扩展（结合 §3 分组）。

### 循环内的坑位（我们 B8 修复的直接范本）
- **grace round**（`loop.go:504`）：工具预算耗尽后给的**终轮只允许 `code_comment`/`task_done`** 补交结论，防止"该收尾却还在乱试工具"。
- **空轮检测**：连续空轮 → 追加一条 user 消息明确"无有效输出，请给结论或调用工具"，再超则 `main_loop_stop` 分类退出（不是原样重发白烧 token）。

### 评论异步化（`llmloop/pool.go` `CommentWorkerPool`）
- `code_comment` 结果不阻塞主循环：`pool.SubmitFor(taskKey, ...)` 异步后处理（定位/反思），每轮末 `AwaitKey(groupKey)` 排空。主循环只负责推进对话，后处理并行。

---

## 3. 文件分组 —— LLM 语义打包（✂️ 大改抄，替换我们的机械分片）

`internal/agent/grouping.go`：
- 分组 LLM **只看文件元数据**（`formatDiffEntry` = `STATUS  path (+N/-M)`，替换 `{{file_list}}`），**不含 diff 内容**——便宜且聚焦。
- 本地决策链 `groupDiffs`：
  - `len ≤ 1` → 无条件 per-file 短路
  - `files < 4 且 churn < 200` → **BundleAll 单组**（小改动不值得分组）
  - `files ≥ 4`（或 GroupingMinFiles=0）→ 调 LLM 语义分组
  - LLM 失败 → 降级全 per-file（每个文件一组）
- 分组后强约束：`maxFilesPerGroup = 10`（切超大组）、`enforceGroupTokenBudget(80% MaxTokens)`（超预算拆单文件组）。
- 语义价值：`message_en.properties` + `message_zh.properties` 这种**关联文件**被并到一组，上下文互看更准；组间隔离避免查询互相污染。

---

## 4. 内存压缩（✂️ 抄，替换我们 B8 的"截断"）

`internal/llmloop/compression.go` **三区划分**，不是简单丢旧消息：
```go
tokenSoftThreshold    = 0.60 // 超 60% MaxTokens：异步后台压缩
tokenWarningThreshold = 0.80 // 超 80% MaxTokens：立即同步压缩
```
- **frozen zone** 恒为 `messages[0:2]`（system + 首条 user）。
- **active zone** 从尾部反向数 complete rounds（assistant + 其 tool 结果），累加到 budget = 80% − 预留 summary 为止。
- 中间 **compress zone** 序列化成 `<id role><content><reasoning>`，发给 `MEMORY_COMPRESSION_TASK` 做摘要，摘要以 `<previous_review_summary>` 追加进**第 2 条 user**。
- `addNextMessage` 追加前若已超 80% 同步压缩；只超 soft 且已有任务则不再起（限频）；**压缩失败不截断**——宁超限不回退丢证据。
- 每个对话独立 mem 压缩（per-conversation）。

---

## 5. 预算闸门与成本预估（✅ 抄）

`internal/agent/estimate.go` 常量：
```go
promptOverheadTokens  = 2000
avgMainRoundsPerFile  = 7
avgOutputTokensPerRound = 700
// 单文件 PLAN 阶段额外 +400
estimateDiffFileTokens = diffTokens + 2000 + 400 + (diffTokens+2000)*7 + 700*7
```
- 门控在 `dispatchSubtasks`：`MaxTokensBudget > 0` 时，每个组在获得 semaphore **之前**算
  `projected = TotalTokensUsed() + Σ estimate(group.diffs)`，超预算 **停止调度全部剩余组**（在飞组可跑完），记 `budgetExceeded` + pending `FailureBudget`，返回**部分评论**而非丢弃全部。
- 删除文件返回 0，不计入。
- 预跑只做**量级警告**，真实用量事后按 API usage 上报（不打款计费）。
- 与 `runtimeConfigSHA256` 挂钩便于审计。

---

## 6. 场景化工具集（✂️ 抄，但砍掉 run_command）

`internal/tool/` —— 沉淀出审查场景专用工具集，focus 稳定：

| 工具 | 入参 | 返回 | 限制 |
|---|---|---|---|
| `code_comment` | `comments:[{content, existing_code, suggestion_code, category, severity, path, thinking}]` | `"Successfully commented."` | category/severity 归一化到枚举否则 other/low；缺 path 回退 groupKey；**是 LLM 唯一上报评论的通道** |
| `code_search` | `search_text, case_sensitive, use_perl_regexp, file_patterns:[]` | `File:…\n N|行内容`；超 100 截断提示 | `git grep` timeout 10s；拒绝 `..` |
| `file_read` | `file_path, start_line, end_line` | `IS_TRUNCATED/LINE_RANGE/行号|内容` | 每文件 ≤ fileReadMaxLines=500 |
| `file_read_diff` | `path_array:[]` | `==== FILE: path ====\n<diff>` | 只返回已解析 diff |
| `file_find` | `query_name, case_sensitive` | 路径列表 / not found | ≤100 |
| `task_done` | `state: DONE\|FAILED` | 终止循环 | FAILED→Fail |

- `code_comment` 的 `existing_code` 参数设计（README tools.json 注释）：**只要 LLM 贴它具体指哪几行**（建议贴新增行，非删/上下文），供定位锚定。
- 内置 `task_done`/`code_comment`，其余注册 Registry。
- **确定性修复** `comment_args_repair.go`：LLM 把 `comments` 序列化成 string 时（偶发），字符扫描转义未转义引号/控制符，修复后过 `repairedCommentsAcceptable`（content 非空、字段白名单、条数 ≥ 原 `"content":` 数）+ `hasSuspectTruncation`（奇数量引号=截断迹象）校验；不过保留 parse error 不强求。

> ❌ **坚决不抄 run_command**。OCR 本身也不提供壳命令，只有结构化工具。这跟我们的容器沙箱结论一致——不给 LLM 壳命令，从根上堵死 prompt-injection 逃逸。

---

## 7. 规则引擎（✂️ 抄，模板化注入而非纯 prompt 拼接）

`internal/config/rules/system_rules.go`：
- 数据结构：`SystemRule{DefaultRule; PathRules:[{Pattern, Rule}]}`；`path_rule_map` 自定义 `UnmarshalJSON` **保留 key 声明顺序，首个匹配者胜**。
- 匹配：`doublestar.Match`（支持 `**`/`{go,py}` 展开）+ 大小写 lower。
- **优先链**：项目自定义 `--rule` > 仓库 `.opencodereview/rule.json` > 用户 `~/.opencodereview/` > 系统内嵌。
- `merge_system_rule: true`：把系统规则拼在用户规则后，输出 `## System-Specific Rules` + `## User-Specific Rules` 两段。
- 规则值是 markdown 文本；规则文件白名单 `.md/.txt/.markdown`、512KB 上限、symlink 防逃逸。
- 代表性写法（`.opencodereview/rule.json`）：
  ```json
  { "rules": [
      { "path": "internal/llm/providers.go",
        "rule": "enforce inline string literals...",
        "merge_system_rule": true } ] }
  ```
- `FileFilter{Include,Exclude}` 支持用 glob 做文件过滤。

> 📖 借鉴：**规则按 path/pattern 精确匹配 + 模板化注入**，比"全局一条 prompt"聚焦得多。新项目要的是"项目级可配置规则（数据库存）+ 命中 path 后注入 prompt"，不一定要复制 doublestar 的复杂度，但**首个匹配者胜 + merge 语义**值得保留。

---

## 8. whole-file scan 模式（📖 借鉴，P2 扩展）

`internal/scan/` —— 审查**单个/整批文件的内容**（而非 diff），用于无 diff 的历史代码审计：
- `ScanItem{Path, Content, IsBinary, LineCount}` 承载整文件；**`AsDiff()` 把它伪装成 `Diff{NewFileContent=整文件}`**，从而**直接复用 diff 的行号 resolver** —— 一个漂亮的复用技巧。
- 文件过滤顺序：user-exclude → user-include → 扩展名 allowlist → 默认排除路径 → `ExcludeReason` 枚举（user_exclude/unsupported_ext/default_path/deleted/binary）。
- batch 切分：`by-language | by-directory | none`，先按 key 分桶再按 BatchSize 切 chunk，组内保序、组间按 key 确定性排序。

---

## 9. 会话持久化 / 断点续跑（📖 借鉴，P1 增强）

`internal/session/`：
- **JSONL 事件流**：`session_start / review_item_done / review_item_reused / review_item_failed / llm_request / llm_response / llm_error / tool_call / resume_lineage / session_end`，每行 `uuid,parentUuid` 链式，流式追加不重写。
- **可审计**：每次 LLM 请求/响应/工具调用/失败都落一条 —— 这比"只看结果"强得多，是 retry report 和调试的基石。
- **去重手段**：`ResumeState.Items map[fingerprint]ResumeItem`，key 是 diff 内容敏感指纹；`review_item_failed` 会删该指纹。**只信 manifest**（覆盖事实源），不信单行 checkpoint。
- **覆盖五集合**（`manifest.go:231`）：`Selected = Completed ∪ Reused ∪ Failed ∪ Waived`（互斥）。

> 📖 该设计与我们「任务状态机 + 幂等键」异曲同工，但补了一个点：**把 LLM 请求/响应/工具调用做成可检索的事件流**（而不只是任务行），这对打口问题排查和成本审计价值很大。

---

## 10. 可观测性基线（✅ 抄）

`internal/telemetry/`：
- **span 命名约定**：`scan.enumerate`、`scan.subtask.<path>`、`tool.execute.<name>`、`llm.request`。
- **事件**：`telemetry.Event(ctx,"scan.started", AnyToAttr("file.count",N), ...)`。
- **指标**：`ocr.review.duration_seconds`(histogram)、`ocr.files_reviewed_total`、`ocr.comments_generated_total`、`ocr.llm.requests_total`、`ocr.llm.tokens_used`、`ocr.llm.request_duration_seconds`、`ocr.tool.calls_total`、`ocr.tool.execution_duration_seconds`。
- 所有 record 前置 `if !IsEnabled()` 短路（禁用近零开销）。

---

## 11. 分发与集成（📖 借鉴，P2 决策：引擎做成可嵌入 CLI + JSON 契约）

Agent C 拆解结论（`cmd/opencodereview/output.go`、`action.yml`、`internal/delegate/`）：
- **JSON 输出契约是唯一集成边界**：`ocr review --format json` 顶层 `status / llm{provider,model} / trace_id / summary{files_reviewed,comments,total_tokens,...} / comments[] / warning` 等。CI、GitHub Action、插件都只消费这个 JSON。
- **delegate 无 LLM 模式（最大启示）**：`ocr delegate preview/rule` 只输出"选哪些文件 + 哪些规则 + diff"，LLM 完全交给外部宿主 agent（Claude Code / Cursor）。审查智能与分发**彻底解耦**。给宿主 agent 的就是一个 SKILL（五步流程 + 评论 schema），schema 与 OCR 自产 `LlmComment` **逐字段对齐**，是刻意契约。
- **分发多形态、一份源码多入口**：npm 双包（bin 启动器 + 6 平台 optionalDeps）、GitHub Action composite、release 二进制、install.sh/ps1。**契约守护**：`plugin-contract.yml`/`action-contract.yml` 用假 `ocr`/`npm` 驱动测试，防生态目录与 action 漂移。
- MCP 是**客户端**：OCR 自连外部 MCP server（stdio/streamable-HTTP），把外部工具注册进自家注册表供 LLM 用——**入站消费**，非自曝为服务端。

---

## 12. 复用决策汇总

| OCR 能力 | 决策 | 落到 codereview-ai |
|---|---|---|
| existing_code 锚定定位 + 四件套 | ✅ 抄 | 替换初稿 linevalidate → `review/location`（`diff_anchor.py` 已给） |
| LLM 语义分组（元数据入参、maxFiles=10、降级） | ✂️ 改 | 替换初稿机械分片 → `review/grouping` |
| llmloop 内存压缩三区、grace round、async pool | ✂️ 改 | agentic + 长对话审查止烧 token |
| 成本预估 + 预算闸门 | ✅ 抄 | pipeline 前置预算门 |
| 场景化工具集（无 run_command） | ✅ 抄 | agentic 工具集（结构化工具，禁止壳命令） |
| 规则引擎（path 匹配 + merge 语义） | ✂️ 改 | 项目级规则配置 → 模板化注入 |
| LlmComment 字段/schema | ✅ 抄 | Finding 模型加 `existing_code`/`suggestion_code` |
| whole-file scan + AsDiff 复用定位 | 📖 借鉴 | P2 审计模式 |
| JSONL 会话事件流（可审计对话） | 📖 借鉴 | P1 对话可追溯/成本审计 |
| telemetry 命名与指标 | ✅ 抄 | 可观测章节 |
| delegate 无 LLM 模式 + JSON 契约 | 📖 借鉴 | P2：引擎可嵌入；schema 对齐是硬约束 |

**复用后的净效果**：初稿里"行级评论透过度""token/时间失控""大变更覆盖不全""调试靠猜"四个风险，分别被 §1 锚定定位、§5 预算闸门、§3 语义分组、§9 事件流直接化解。