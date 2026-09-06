# reference/ —— 可复用素材

从 `AI-Codereview-Gitlab` 中提取、核对并改造过的素材。
**不是可直接运行的代码库**，是写新项目时用来抄的参考资料。

| 文件 | 内容 | 复用方式 |
|---|---|---|
| [`platform_payload_map.md`](platform_payload_map.md) | GitLab/GitHub/Gitea/Gitee 的 webhook 字段路径、API 端点、签名算法、**行级评论的构造方式** | 📖 **最有价值**。照着实现适配器，省掉大量翻文档时间 |
| [`antipatterns.md`](antipatterns.md) | 原项目踩过的坑，逐条带文件行号与实测证据 | ⚠️ 开发和 code review 时逐条对照 |
| [`prompt_templates.yml`](prompt_templates.yml) | 重新设计的 prompt 模板：结构化输出、行级定位、增量审查、injection 防御、四种风格 | ✂️ 稍作调整可直接放进 `conf/` |
| [`tokens.py`](tokens.py) | token 计数 / 截断 / 分片，含离线降级 | ✅ 可直接复制到 `src/codereview_ai/utils/tokens.py` |
| [`im_payloads.md`](im_payloads.md) | 钉钉/飞书/企微的签名算法、消息体、长度限制与方言差异 | 📖 协议部分照抄，实现部分重写 |
| [`ocr_notes.md`](ocr_notes.md) | **阿里 OpenCodeReview 详细拆解** + 每条能力的复用决策（锚定定位/语义分组/内存压缩/预算闸门/工具集/规则引擎/scan/session/telemetry/分发） | 📖 写新项目引擎层时逐节对照，标注 ✅/✂️/📖 的按对应方式复用 |
| [`pr_agent_notes.md`](pr_agent_notes.md) | **Qodo PR-Agent 详细拆解** + 复用决策（LLM 输出修复链/diff 预算/内容指纹去重/finding 状态机/配置白名单/GitHub 与 GitLab 行级评论机制） | 📖 补 OCR 没覆盖的健壮性字位；写回层与配置层按它逐节对照 |
| [`diff_anchor.py`](diff_anchor.py) | **确定性行级定位**：existing_code 片段 → 逐行 normalize + 纯字符串匹配出真实行号；输出 `side`/`old_line`/`new_line`/`edit_type`（齐 GitLab/GitHub 回写） | ✅ 直接复制到 `src/codereview_ai/review/location.py`，替换原「LLM 拍行号 + 校验」方案；注意需提供 `new_file_content` 供全文/跨文件兜底（DESIGN §7.1） |

## 复用度说明

- ✅ **直接可用**：`tokens.py`、`diff_anchor.py`（后者已实测四种场景，可直接落成 `review/location.py`）
- ✂️ **改造后可用**：`prompt_templates.yml`（行号校验部分按 `diff_anchor.py` 方案改写）
- 📖 **只抄知识，不抄代码**：`.md` 系列。原项目实现方式基本都要重写
  （无 timeout、无重试、同步阻塞、`verify=False`），但它踩出来的**协议细节和字段路径**是真金白银。`ocr_notes.md` 是**引擎层设计蓝图**——哪些该交给工程逻辑、哪些才交给 LLM，结论直接指导新项目的审查引擎架构。

## 没有被提取的部分及原因

| 原项目模块 | 为什么不复用 |
|---|---|
| `biz/llm/client/*`（7 个厂商适配器） | 新项目用 LiteLLM，这 7 个文件整体消失 |
| `biz/utils/queue.py` | 9 行裸 fork，无任何可取之处 |
| `biz/service/review_service.py` | 裸 sqlite + ad-hoc ALTER TABLE，改用 SQLAlchemy + Alembic |
| `ui.py`（690 行 Streamlit） | 技术栈改为 Vue，且原实现把 UI 与业务逻辑焊死 |
| `biz/agent/tools/run_command.py` | 沙箱可绕过（已实测），设计思路本身要推翻 |
| `biz/platforms/*/webhook_handler.py` | 三份复制粘贴，只提取其中的**字段知识**到 `platform_payload_map.md` |
| `biz/event/event_manager.py` | blinker 是同步的，新项目用 async 事件总线；但**解耦思路保留** |
| `biz/cmd/` | 原项目的本地 CLI 审查工具，与新项目定位无关 |
