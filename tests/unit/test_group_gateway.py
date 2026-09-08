"""review/group_gateway 测试：`LLMGroupAdapter`（GroupLLM 的传输层实现）。

纯离线：注入 fake `LLMGateway.complete`，不触网络。断言 adapter 把 OCR 模板 + 元数据
拼进请求、剥掉 label 返回纯路径组、坏 JSON 返回 None（上层据此 per-file 降级）。
"""

from __future__ import annotations

import asyncio

from codereview_ai.review.agentic.prompts import GROUPING_TASK_SYSTEM
from codereview_ai.review.group_gateway import LLMGroupAdapter
from codereview_ai.review.llm_gateway import LLMGateway


def _gateway_returning(raw: str) -> LLMGateway:
    async def backend(messages):
        return raw

    return LLMGateway(model="fake/model", backend=backend)


def _run(coro):
    return asyncio.run(coro)


def test_adapter_sends_template_and_metadata():
    seen: list[list[dict]] = []

    async def backend(messages):
        seen.append(messages)
        return '[{"label": "i18n", "files": ["msg_en.properties", "msg_zh.properties"]}]'

    gw = LLMGateway(model="fake/model", backend=backend)
    adapter = LLMGroupAdapter(gw)
    out = _run(adapter.group_metadata(["M  msg_en.properties (+1/-0)", "M  msg_zh.properties (+1/-0)"], 10))
    assert out == [["msg_en.properties", "msg_zh.properties"]]
    # system 是 OCR grouping 模板；user 里带元数据行
    assert seen[0][0]["role"] == "system"
    assert seen[0][0]["content"] == GROUPING_TASK_SYSTEM
    assert "msg_en.properties (+1/-0)" in seen[0][1]["content"]
    assert "{{file_list}}" not in seen[0][1]["content"]


def test_adapter_returns_path_groups_only():
    gw = _gateway_returning('[{"label": "a", "files": ["a.py"]}, {"label": "c", "files": ["b.py", "c.py"]}]')
    out = _run(LLMGroupAdapter(gw).group_metadata(["a.py", "b.py", "c.py"], 10))
    assert out == [["a.py"], ["b.py", "c.py"]]


def test_adapter_skips_entries_without_files():
    # 混入缺 files / 非 dict 的元素 → 跳过，不给坏组
    gw = _gateway_returning('[{"label": "x", "files": ["a.py"]}, "junk", {"label": "y"}]')
    out = _run(LLMGroupAdapter(gw).group_metadata(["a.py"], 10))
    assert out == [["a.py"]]


def test_adapter_bad_json_returns_none():
    gw = _gateway_returning("totally not json")
    assert _run(LLMGroupAdapter(gw).group_metadata(["a.py"], 10)) is None


def test_adapter_dict_returns_none():
    # 目标是顶层数组；dict 一律视为不可行 → None（触发 per-file 降级）
    assert _run(LLMGroupAdapter(_gateway_returning("{}")).group_metadata(["a.py"], 10)) is None


def test_adapter_empty_complete_raises_llm_error():
    # 空返回在 LLMGateway.complete 就抛 LLMError；SemanticGrouper 会捕获并降级 per-file
    import pytest

    from codereview_ai.review.llm_gateway import LLMError

    with pytest.raises(LLMError):
        _run(LLMGroupAdapter(_gateway_returning("   ")).group_metadata(["a.py"], 10))