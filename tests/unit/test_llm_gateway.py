"""review/llm_gateway 测试：传输层(fake backend)、JSON 修复链、schema 归一化。"""

from __future__ import annotations

import pytest

from codereview_ai.domain.models import Category, Severity
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
    with pytest.raises(LLMError):
        parse_review_json("garbage")


def test_parse_review_json_accepts_fenced_text():
    import json

    raw = '```json\n' + json.dumps(CLEAN, ensure_ascii=False) + '\n```'
    r = parse_review_json(raw)
    assert r.findings[0].category == Category.BUG
