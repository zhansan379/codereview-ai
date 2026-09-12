"""LLM 语义分组适配器：`grouping.GroupLLM` 的传输层实现（DESIGN §7.2 步骤 3）。

- 用 OCR `grouping_task` 模板调**一次** LLM：system 当系统指令，user 把 `{{file_list}}`
  填成 `STATUS  path (+N/-M)` 元数据（复用 `grouping.format_diff_entry` 已有产物）。
- 复用 `llm_gateway.LLMGateway.complete(json_object=True)` + `repair_json_array`，
  不新增网络链路；坏 JSON / 不可行 → 返回 `None`（`SemanticGrouper.group()` 既有降级 per-file）。
- OCR 输出带 `label` 的对象，本层剥成纯路径组 `list[list[str]]`；
  与 `GroupLLM.group_metadata` 对齐。
"""

from __future__ import annotations

from codereview_ai.review.agentic.capture import ACTIVE_RECORDER
from codereview_ai.review.agentic.prompts import GROUPING_TASK_SYSTEM, GROUPING_TASK_USER
from codereview_ai.review.llm_gateway import LLMGateway, repair_json_array


class LLMGroupAdapter:
    """实现 `GroupLLM`：调一次 LLM 把变更文件按语义归成 ≤10 文件的组。

    错误不在这里兜——抛 `LLMError` 或返回 `None` 都会触发 `SemanticGrouper.group()`
    的 per-file 降级（`grouping.py` 决策链 184-207 行）。只承诺「或给出合法路径组，或 None」。
    """

    def __init__(self, gateway: LLMGateway) -> None:
        self._gateway = gateway

    async def group_metadata(self, entries: list[str], max_files: int) -> list[list[str]] | None:
        """`GroupLLM` 契约：文件元数据 → 纯路径组；失败/坏 JSON → None（外层降级）。"""
        user = GROUPING_TASK_USER.replace("{{file_list}}", "\n".join(entries))
        messages = [
            {"role": "system", "content": GROUPING_TASK_SYSTEM},
            {"role": "user", "content": user},
        ]
        raw = await self._gateway.complete(messages)
        # 分组调用 = 一条独立的 `grouping` 轮，走对话采集，让会话页出现「文件分组」泳道
        # （对齐上游 grouping_task）。无 usage（complete 只回文本）；
        # 异常被 record 内部吞掉不阻断分组。
        rec = ACTIVE_RECORDER.get()
        if rec is not None:
            await rec.record(
                "grouping",
                request=messages,
                response={"content": raw, "tool_calls": [], "usage": None},
                model=self._gateway.model or "",
            )
        obj = repair_json_array(raw)
        if obj is None:
            return None
        return [
            group["files"]
            for group in obj
            if isinstance(group, dict) and isinstance(group.get("files"), list)
        ]
