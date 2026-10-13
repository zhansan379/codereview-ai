# aacr-bench 未纳入评测的 15 条样本说明

全量 196 条里有 **15 条**在本次评测中没能产出有效结果，单独列在这里备查。
其中 **12 条是数据集本身的缺陷**（三家评审器都跑不了），另 **3 条是单点异常**（各只影响一家）。

> 参考：[aacr-bench 全量评测报告](./benchmark_aacr_bench.md)

## A. 12 条三家都跑不了（数据集缺陷）

### A.1 「数据集记录的提交已失效」11 条

**注意**：这些 PR 本身**都还能打开，里面的提交也都能点开**——失效的不是 PR，而是
**数据集记录的那个 `target_commit`**（采集时的 PR 头提交）。它和 PR 现在的 head **不是同一个提交**。

| # | 实例 | 仓库 | PR（可访问） | 数据集记录的 `target_commit`（**已失效，404**） | PR 当前 head（可访问） | 语言 / 类别 | 参考评论 |
|---|---|---|---|---|---|---|---|
| 1 | `keycloak__keycloak@79dbba9` | keycloak/keycloak | [#36457](https://github.com/keycloak/keycloak/pull/36457) | [`79dbba9b1790…`](https://github.com/keycloak/keycloak/commit/79dbba9b179015cfd599ebac63112e34c9d724af) | [`2b8f303cdab7…`](https://github.com/keycloak/keycloak/commit/2b8f303cdab77add175f272f277493df54df9f1a) | Java / Bug Fix | 9 |
| 2 | `keycloak__keycloak@460f800` | keycloak/keycloak | [#35645](https://github.com/keycloak/keycloak/pull/35645) | [`460f8008f86d…`](https://github.com/keycloak/keycloak/commit/460f8008f86d3fa8f62da63e26d8bdc306af60b2) | [`fb1052616c07…`](https://github.com/keycloak/keycloak/commit/fb1052616c07) | Java / 新功能 | 7 |
| 3 | `keycloak__keycloak@2373a48` | keycloak/keycloak | [#37465](https://github.com/keycloak/keycloak/pull/37465) | [`2373a483fdee…`](https://github.com/keycloak/keycloak/commit/2373a483fdeead9f1b5e815015349c6871de8c3a) | [`667286562d36…`](https://github.com/keycloak/keycloak/commit/667286562d36) | Java / 安全修复 | 7 |
| 4 | `keycloak__keycloak@21d4538` | keycloak/keycloak | [#37504](https://github.com/keycloak/keycloak/pull/37504) | [`21d4538b6f69…`](https://github.com/keycloak/keycloak/commit/21d4538b6f69a47dbd25571ea39a5d2a6fa788ef) | [`4f2571e2ccdd…`](https://github.com/keycloak/keycloak/commit/4f2571e2ccdd) | Java / 重构 | 21 |
| 5 | `keycloak__keycloak@1463502` | keycloak/keycloak | [#41672](https://github.com/keycloak/keycloak/pull/41672) | [`1463502078ae…`](https://github.com/keycloak/keycloak/commit/1463502078ae277b3613e02429ecafde921b892b) | [`abffd8017718…`](https://github.com/keycloak/keycloak/commit/abffd8017718) | Java / 依赖更新 | 1 |
| 6 | `ClickHouse__ClickHouse@fd6ba0c` | ClickHouse/ClickHouse | [#85266](https://github.com/ClickHouse/ClickHouse/pull/85266) | [`fd6ba0c19048…`](https://github.com/ClickHouse/ClickHouse/commit/fd6ba0c19048b6a546188661d1092c6c76a72796) | [`36a82280b136…`](https://github.com/ClickHouse/ClickHouse/commit/36a82280b136) | C++ / 重构 | 5 |
| 7 | `ClickHouse__ClickHouse@cc96255` | ClickHouse/ClickHouse | [#74070](https://github.com/ClickHouse/ClickHouse/pull/74070) | [`cc962559c45f…`](https://github.com/ClickHouse/ClickHouse/commit/cc962559c45fcee9706d86c6425d48e794676a67) | [`14463ed38c6f…`](https://github.com/ClickHouse/ClickHouse/commit/14463ed38c6f) | C++ / 新功能 | 16 |
| 8 | `comfyanonymous__ComfyUI@4936d01` | comfyanonymous/ComfyUI | [#7952](https://github.com/comfyanonymous/ComfyUI/pull/7952) | [`4936d01872b8…`](https://github.com/comfyanonymous/ComfyUI/commit/4936d01872b8719ede33f9a11dc8e898c33d39e4) | [`776120779567…`](https://github.com/comfyanonymous/ComfyUI/commit/776120779567) | Python / 新功能 | 5 |
| 9 | `comfyanonymous__ComfyUI@cfc3122` | comfyanonymous/ComfyUI | [#9560](https://github.com/comfyanonymous/ComfyUI/pull/9560) | [`cfc312296c0b…`](https://github.com/comfyanonymous/ComfyUI/commit/cfc312296c0b9f255bba6b4b2789ac7bbc66a7cb) | [`efd299d68149…`](https://github.com/comfyanonymous/ComfyUI/commit/efd299d68149) | Python / Bug Fix | 6 |
| 10 | `nodejs__node@b225544` | nodejs/node | [#56185](https://github.com/nodejs/node/pull/56185) | [`b2255442712c…`](https://github.com/nodejs/node/commit/b2255442712cb6db83d112deb6ba61197d06a5f3) | [`43838059a6de…`](https://github.com/nodejs/node/commit/43838059a6de) | JS / Bug Fix | 4 |
| 11 | `astral-sh__uv@ed57db2` | astral-sh/uv | [#11088](https://github.com/astral-sh/uv/pull/11088) | [`ed57db2b34c2…`](https://github.com/astral-sh/uv/commit/ed57db2b34c2a7a955b2dc76583dce7ace02e628) | [`75baf506d709…`](https://github.com/astral-sh/uv/commit/75baf506d709) | Rust / Bug Fix | 6 |

（表里第 4 列的链接**点开是 404**，第 3、5 列可正常访问。前 7 位的短 SHA 对应实例名里的后缀。）

### A.2 为什么「PR 还在、提交却打不开」

**一句话**：数据集记录的是**采集那一刻 PR 分支的头提交**，而 PR 分支后来被重做过；
旧提交从分支上脱落，成了没人引用的对象，被 GitHub 回收了。

以 `keycloak#36457` 为例（其余 10 条情况完全一致）：

| 检查项 | 结果 |
|---|---|
| 数据集记录的 base（`source_commit`） | `602df061…` → **能取到** ✓ |
| 数据集记录的 head（`target_commit`） | `79dbba9b…` → GitHub API 报 `422 No commit found`、网页 **404**、`git fetch --all --tags` 后本地也没有 ✗ |
| PR 现在的 head | `2b8f303c…` → **能取到**（网页 200）✓ |
| 两者是不是同一个提交 | **不是**：base 相同、head 不同 |

**推测的成因**（按可能性排序）

1. **贡献者在数据集采集之后重做了分支**——force-push、rebase、amend 或压缩提交。
   这是最常见的情况：PR 评审期间作者改了几轮，每改一次旧提交就从分支上脱落一次。
2. **PR 合并后 GitHub 回收不可达对象**。分支一重做，旧提交就不再被任何分支/标签/PR 引用指向；
   这类对象会在一段时间后被清理（fork 来源的 PR 尤其明显，合并后 fork 分支常被删掉）。
   数据集恰好采集在「中间态」，等再跑评测时那个中间态已经不存在了。
3. 数据集记录的是 **PR 的中间版本**，而参考评论也是针对那一版写的——所以丢失的不只是一个 commit，
   而是「被标注的那份代码快照」。

**为什么可以断定是上游删了对象，而不是本机缓存/网络问题**

- 11 条的 **base 全部能取到、只有 head 取不到**。如果是网络或本地缓存故障，base 也会一起挂。
- 11 条的 PR **全部是 closed + merged，且当前 head 全部与数据集记录不同**——系统性现象，不是偶发。
- GitHub API 对旧 head 直接返回 422（对象在服务端不存在），对当前 head 返回 200。
- `git ls-remote refs/pull/*/head` 里也找不到旧 head 的 tip，说明它既不在分支上、也不在 PR 引用上。

**能不能用 PR 现在的 head 顶替？不能。**
参考评论标注的是**原始 diff 的行号**。分支重做后 diff 完全不同——实测拿数据集的 base 对 PR 当前 head 求 diff，
结果是 **898 个文件、+8890/−21018 行**（原始样本只是个小 bug fix），参考评论里 9 条有 5 条连文件都不在新 diff 里。
硬套上去等于在评另一个样本，指标没有意义。

**结论**：这 11 条**无法从上游恢复**。要用它们，只能从数据集上游重新采集（拿 PR 当前 head 重新标注参考评论）。

### A.3 「仓库路径在 Windows 上非法」1 条

| # | 实例 | 仓库 | PR | 语言 / 类别 | 参考评论 | 失效原因 |
|---|---|---|---|---|---|---|
| 12 | `juspay__hyperswitch@0231de6` | juspay/hyperswitch | [#9130](https://github.com/juspay/hyperswitch/pull/9130) | Rust / 新功能 | 6 | 仓库路径在 Windows 上非法 |

这条的 `target_commit` **是好的**（`0231de6ae679…`，本地 clone 里就有），失败在别处：
仓库里 `postman/collection-dir/...` 含 Windows 不允许的路径成分，`git checkout` 直接报 `invalid path`。
`core.longpaths=true` 与 `core.protectNTFS=false` 都绕不过去，属平台限制（Linux/macOS 上可正常跑）。

## B. 3 条单点异常（各只影响一家）

这 3 条**跑得起来**，只是某一家没产出可用结果。它们仍按「评测了但生成 0 条」计入指标，
不影响三家的分母一致性。

| 实例 | 仓库 | PR | 语言 / 类别 | 改动行 | 参考评论 | 受影响方 | 现象 |
|---|---|---|---|---|---|---|---|
| `appwrite__appwrite@710b8bd` | appwrite/appwrite | [#9999](https://github.com/appwrite/appwrite/pull/9999) | PHP / Bug Fix | 46 | 18 | codereview-ai | CLI 每次 `exit=2`（agentic 运行失败），重试 4 次全部失败 |
| `ollama__ollama@498792c` | ollama/ollama | [#9654](https://github.com/ollama/ollama/pull/9654) | Go / 测试与 CI | 776 | 3 | OCR | OCR 自己返回 `status=skipped`：文件选择阶段在 776 行的 diff 上没选中任何文件 |
| `nextcloud__server@51b5ea9` | nextcloud/server | [#52846](https://github.com/nextcloud/server/pull/52846) | PHP / 重构 | 165 | 2 | Claude Code | 第一次 `exit=0` 但 stdout 没有结构化 JSON（存成 raw text）；重跑一次解析成功，产出 5 条 finding |

**这 3 条值得关注的点**

- `appwrite` 是**产品真实失败**（不是评测问题）：46 行的 PHP 改动、18 条参考评论，codereview-ai 每次都报错退出，
  与之前几次同类失败一样是模型侧报错，值得单独复现排查。
- `ollama` 是 **OCR 的文件选择漏了**：776 行的 diff 里一个文件都没选中，等于这条 PR 完全没被审。
- `nextcloud` 是 **Claude 的输出偶发不可解析**：同一输入重跑一次就正常了，说明是稳定性问题而非能力问题。

## C. 对指标的影响

- 12 条不可运行的样本，合计 **93 条参考评论**从分母里消失（1506 → 1413），三家一致，**不影响横向对比**。
- 分布上损失集中在 Java（keycloak 5 条）和 C++（ClickHouse 2 条），所以这两门语言的样本量比原始数据集略少。
- 3 条单点异常合计 23 条参考评论仍在分母内，但对应一家的命中为 0，会轻微拉低该家的召回（其中 appwrite 的 18 条
  对 codereview-ai 影响最明显）。

## D. 数据集链接

| 用途 | 链接 |
|---|---|
| 上游原始数据（alibaba/aacr-bench） | https://github.com/alibaba/aacr-bench<br>原始文件：https://raw.githubusercontent.com/alibaba/aacr-bench/main/dataset/positive_samples.json |
| 本项目工作副本（含标准格式数据集与评测框架） | https://github.com/zhansan379/aacr-bench |
| 标准格式数据集（评测框架直接消费） | `aacr-bench/evaluation/data/aacr_bench.jsonl`（196 行，每行一个实例） |
| 原始格式数据集（含 `githubPrUrl` / `category` / `project_main_language`） | `aacr-bench/dataset/positive_samples.json` |

原始文件校验值（`evaluation/benchmark/AACR-Bench/positive_samples.meta.json`）：

```
sha256 = 2fb282e496e6ffb97311f670c3337ef9284216f8db110cb889d226340891f718
```

**字段说明**（排查上面 15 条时用得上）

| 字段 | 含义 |
|---|---|
| `githubPrUrl` | 该样本对应的 GitHub PR（上表的 PR 链接即由此而来） |
| `source_commit` | 评审基线（PR 的 base），对应标准格式的 `base_commit`；A.1 的 11 条**都还在** |
| `target_commit` | 待评审的提交（采集时的 PR head），对应 `head_commit`；**A.1 的 11 条就是它失效了** |
| `category` | PR 类型：Bug Fix 53 / 新功能 44 / 重构 27 / 性能 19 / 测试与 CI 18 / 安全 13 / 文档 12 / 代码风格 8 / 依赖 2 |
| `project_main_language` | 仓库主语言，与评测报告里的语言分层口径一致 |
| `change_line_count` | 改动行数 |
| `comments` | 人工标注的参考评论（即评测的「标准答案」） |

## E. 复现核查用到的命令

```bash
# 1) 旧 head 在 GitHub 上是否还存在（A.1 的 11 条都会返回 422）
gh api repos/keycloak/keycloak/commits/79dbba9b179015cfd599ebac63112e34c9d724af

# 2) 网页状态码：旧 head 404，PR 当前 head 200
curl -s -o /dev/null -w "%{http_code}\n" -L https://github.com/keycloak/keycloak/commit/79dbba9b179015cfd599ebac63112e34c9d724af

# 3) PR 当前 head 与数据集记录是否一致（A.1 的 11 条都会显示不一致）
gh api repos/keycloak/keycloak/pulls/36457 --jq '{state, merged, head_sha:.head.sha}'

# 4) 本地是否可用（base 有、head 无）
git -C aacr-bench/evaluation/repo_ocr/keycloak__keycloak cat-file -e <sha>^{commit}

# 5) 旧 head 是否在 PR 引用上（查不到即不在）
git ls-remote https://github.com/keycloak/keycloak.git 'refs/pull/*/head' | grep <sha>
```
