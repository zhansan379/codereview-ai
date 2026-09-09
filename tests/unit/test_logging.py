"""logging 模块测试：脱敏、trace_id 上下文、JSON 结构化。"""

from __future__ import annotations

import io
import json
import logging

from codereview_ai import logging as crlog


class TestMask:
    def test_masks_authorization_bearer(self):
        out = crlog.mask("Authorization: Bearer abc123.def456.ghi789")
        assert "[REDACTED]" in out
        assert "abc123.def456.ghi789" not in out

    def test_masks_query_token(self):
        out = crlog.mask("https://x.com/?sign=abc&token=secret")
        assert "sign=[REDACTED]" in out
        assert "token=[REDACTED]" in out
        assert "secret" not in out

    def test_masks_field_value_forms(self):
        cases = [
            "api key=sk-123",
            ".token=abc",
            "webhook_secret: xx",
            "password=p@ss",
        ]
        for text in cases:
            out = crlog.mask(text)
            assert "[REDACTED]" in out, f"not masked: {text!r} -> {out!r}"

    def test_does_not_over_mask_plain_words(self):
        out = crlog.mask("the secret to good code is testing =) key insight")
        # 无 "=值" 的令牌字段写法不应被误伤（这里没有 =/:/space+value 的令牌形态）
        assert "secret to good code" in out

    def test_leaves_normal_text(self):
        text = "review finished for pr #42"
        assert crlog.mask(text) == text


class TestJsonFormatter:
    def test_emits_trace_id_and_fields(self):
        fmt = crlog.JsonFormatter()
        record = logging.LogRecord(
            name="codereview_ai", level=logging.INFO, pathname=__file__, lineno=1,
            msg="handling %s", args=("mr",), exc_info=None,
        )
        record.trace_id = "abc123"
        record.pr_node = "42"
        data = json.loads(fmt.format(record))
        assert data["trace_id"] == "abc123"
        assert data["pr_node"] == "42"
        assert data["message"] == "handling mr"
        assert data["level"] == "INFO"

    def test_masks_in_message(self):
        fmt = crlog.JsonFormatter()
        record = logging.LogRecord(
            name="codereview_ai", level=logging.INFO, pathname=__file__, lineno=1,
            msg="failed, Authorization: Bearer leak", args=(), exc_info=None,
        )
        data = json.loads(fmt.format(record))
        assert "leak" not in data["message"]


def test_trace_context_sets_and_resets():
    assert crlog.TRACE_ID.get() == ""

    async def run():
        crlog.TRACE_ID.set("outer")
        async with crlog.trace("w-") as tid:
            assert tid == "outer"  # 已有则继承
            assert crlog.TRACE_ID.get() == "outer"
        # 内层 contextvar reset 回外层值（仍为 outer）
        assert crlog.TRACE_ID.get() == "outer"

    import asyncio

    asyncio.run(run())


async def test_trace_generates_when_empty():
    crlog.TRACE_ID.set("")
    async with crlog.trace("w-") as tid:
        assert tid.startswith("w-")
        assert len(tid) == 2 + 12


def test_setup_logging_emits_masked_standard_to_stream():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    crlog.setup_logging(log_level="INFO", handler=handler)
    logger = crlog.get_logger()
    logger.info("deploying to webhook_secret=%s", "topsecret")
    stream.flush()
    line = stream.getvalue().strip().splitlines()[-1]
    # 标准格式（与 uvicorn 一致），脱敏仍生效
    assert line.startswith("INFO:")
    assert "topsecret" not in line
    assert "webhook_secret=[REDACTED]" in line


def test_setup_logging_json_when_explicit():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    crlog.setup_logging(log_level="INFO", handler=handler, fmt=crlog.JsonFormatter())
    logger = crlog.get_logger()
    logger.info("handling %s", "mr")
    stream.flush()
    line = stream.getvalue().strip().splitlines()[-1]
    data = json.loads(line)
    assert data["level"] == "INFO"
    assert data["message"] == "handling mr"
    assert data["logger"] == "codereview_ai"
