"""review/llm_gateway 测试：传输层(fake backend)、JSON 修复链、schema 归一化。"""

from __future__ import annotations

import pytest

from codereview_ai.domain.models import Category, Severity
from codereview_ai.review.fallback import FallbackLLMGateway, wrap_fallback
from codereview_ai.review.llm_gateway import (
    LLMError,
    LLMGateway,
    parse_review_json,
    repair_json,
)

CLEAN = {
    "summary": "整体不错",
    "scores": {"correctness": 30, "security": 20, "practices": 15, "performance": 3, "commit_quality": 2},  # noqa: E501
    "findings": [
        {
            "content": "这里可能 NPE",
            "category": "bug",
            "severity": "high",
            "file": "src/app.py",
            "existing_code": "def run():",
            "suggestion_code": "def run(p):",
        }
    ],
    "skipped_files": [],
}


def test_gateway_complete_returns_text_from_fake_backend():
    async def backend(messages):
        assert messages[0]["content"] == "hi"
        return '{"ok": true}'

    gw = LLMGateway(model="fake/model", backend=backend)
    text = ""
    import asyncio

    async def run():
        nonlocal text
        text = await gw.complete([{"role": "user", "content": "hi"}])

    asyncio.run(run())
    assert text == '{"ok": true}'


def test_gateway_litellm_backend_forwards_api_key_and_base_url(monkeypatch):
    """默认 litellm backend 应把显式的 api_key/base_url 透传给 acompletion（绕开 env 大小写）。"""
    import asyncio

    captured: dict = {}

    class _Msg:
        content = "hi"

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return _Resp()

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    gw = LLMGateway(
        model="openai/gpt-4o-mini",
        api_key="sk-secret-abc",
        base_url="https://example.com/v1",
    )

    async def run():
        return await gw.complete([{"role": "user", "content": "ping"}])

    assert asyncio.run(run()) == "hi"
    assert captured["api_key"] == "sk-secret-abc"
    assert captured["base_url"] == "https://example.com/v1"


def test_gateway_wraps_transport_failure_into_llm_error():
    async def boom(messages):
        raise TimeoutError("net down")

    gw = LLMGateway(model="fake/model", backend=boom)
    import asyncio

    async def run():
        with pytest.raises(LLMError):
            await gw.complete([{"role": "user", "content": "x"}])

    asyncio.run(run())


def test_gateway_empty_content_is_llm_error():
    async def empty_backend(messages):
        return "   "

    gw = LLMGateway(model="fake/model", backend=empty_backend)
    import asyncio

    async def run():
        with pytest.raises(LLMError):
            await gw.complete([])

    asyncio.run(run())


def test_repair_json_strips_code_fence():
    assert repair_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_repair_json_fixes_trailing_comma_and_control_char():
    assert repair_json('{"a": 1, "b": [1, 2,],}') == {"a": 1, "b": [1, 2]}
    assert repair_json('{"a": "x\x00y"}') == {"a": "xy"}


def test_repair_json_unquoted_keys():
    assert repair_json('{correctness: 10, practices: 5}') == {"correctness": 10, "practices": 5}


def test_repair_json_truncates_to_last_brace():
    # 对象已闭合但尾部残留说明文字 / 多余输出
    assert repair_json('{"a": 1, "b": {"c": 2}} 以上就是我的全部意见') == {"a": 1, "b": {"c": 2}}


def test_repair_json_returns_none_on_unrecoverable():
    assert repair_json("totally not json") is None


def test_parse_review_json_clean_dict():
    r = parse_review_json(CLEAN)
    assert r.scores.correctness == 30
    assert r.scores.total == 30 + 20 + 15 + 3 + 2
    assert r.findings[0].category == Category.BUG
    assert r.findings[0].severity == Severity.HIGH
    assert r.findings[0].line is None  # 锚定交由后续 location 步骤


def test_parse_review_json_normalizes_bad_enums_and_clamps_scores():
    data = {
        "summary": "x",
        "scores": {"correctness": 999, "security": -5, "practices": 10, "performance": 1, "commit_quality": 5},  # noqa: E501
        "findings": [{"content": "c", "file": "f.py", "category": "not-a-cat", "severity": "catastrophic"}],  # noqa: E501
        "skipped_files": [],
    }
    r = parse_review_json(data)
    assert r.scores.correctness == 40  # 上限 40
    assert r.scores.security == 0  # 下限 0
    assert r.findings[0].category == Category.OTHER
    assert r.findings[0].severity == Severity.LOW


def test_parse_review_json_drops_findings_without_file_or_content():
    data = {
        "summary": "x",
        "scores": {},
        "findings": [
            {"content": "", "file": "f.py"},
            {"content": "c", "file": ""},
            {"content": "ok", "file": "f.py"},
        ],
        "skipped_files": [],
    }
    r = parse_review_json(data)
    assert len(r.findings) == 1
    assert r.findings[0].content == "ok"


def test_parse_review_json_raises_on_undecodable_text():
    with pytest.raises(LLMError) as ei:
        parse_review_json("garbage")
    # 报错要带"情况分析"（长度 + 开头摘录），后台可直接展示为何失败
    assert "garbage" in str(ei.value)
    assert "无法解析为 JSON" in str(ei.value) and "字符" in str(ei.value)


def test_parse_review_json_accepts_fenced_text():
    import json

    raw = '```json\n' + json.dumps(CLEAN, ensure_ascii=False) + '\n```'
    r = parse_review_json(raw)
    assert r.findings[0].category == Category.BUG


# ---------- FallbackLLMGateway（自定义回退链） ----------


def _gw(model: str):
    return LLMGateway(model=model, backend=lambda msgs: model)


def test_fallback_switches_to_next_on_failure():
    async def boom(messages):
        raise LLMError("bad model")

    async def ok_from_b(messages):
        return "ok-from-b"

    gw1 = LLMGateway(model="openai/a", backend=boom)
    gw2 = LLMGateway(model="openai/b", backend=ok_from_b)

    fb = FallbackLLMGateway([gw1, gw2])
    import asyncio

    assert asyncio.run(fb.complete([{"role": "user", "content": "x"}])) == "ok-from-b"


def test_fallback_all_fail_raises():
    import asyncio

    async def boom(messages):
        raise LLMError("nope")

    fb = FallbackLLMGateway([
        LLMGateway(model="openai/a", backend=boom),
        LLMGateway(model="openai/b", backend=boom),
    ])
    with pytest.raises(LLMError) as ei:
        asyncio.run(fb.complete([{"role": "user", "content": "x"}]))
    assert "全部" in str(ei.value) or "均失败" in str(ei.value)


def test_fallback_model_property_returns_first():
    fb = FallbackLLMGateway([_gw("openai/a"), _gw("openai/b")])
    assert fb.model == "openai/a"


def test_wrap_fallback_single_returns_plain_gateway():
    gw = wrap_fallback([type("LLM", (), {"model": "m", "name": "m", "api_key": "", "base_url": "", "provider": "", "max_tokens": None, "temperature": None})()])
    assert isinstance(gw, LLMGateway)
    assert not isinstance(gw, FallbackLLMGateway)


def test_wrap_fallback_multiple_wraps_in_fallback():
    import asyncio

    def make(model: str):
        return type("LLM", (), {
            "model": model, "name": model, "api_key": "", "base_url": "",
            "provider": "", "max_tokens": None, "temperature": None,
        })()

    fb = wrap_fallback([make("openai/a"), make("openai/b")])
    assert isinstance(fb, FallbackLLMGateway)
    assert fb.model == "openai/a"
