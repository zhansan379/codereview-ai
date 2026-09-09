"""日志 + 脱敏 + trace_id 上下文（DESIGN §15.1 / §15.2）。

- 默认 `StandardFormatter` 输出与 uvicorn/fastapi 一致的标准文本；需要结构化
  JSON（含 `ts level logger trace_id task_id ...`）时用 `JsonFormatter`。
- `SensitiveFilter` 统一打码令牌类字段：token/key/secret/password/
  Authorization/URL 的 sign=/token=，**不记录请求 body 或 diff 正文**。
- `TRACE_ID` 为 contextvar；`trace()` 上下文管理器在整个异步链路里携带同一 id。
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import uuid4

LOG_LOGGER_NAME = "codereview_ai"

#: webhook 入口生成 → 队列 → worker → LLM/forge/IM 全程贯穿的追踪 id
TRACE_ID: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")


@asynccontextmanager
async def trace(prefix: str = "") -> AsyncIterator[str]:
    """在异步上下文里生成/继承一个 trace_id，作用域内 yield，退出时还原。"""
    tid = TRACE_ID.get() or f"{prefix}{uuid4().hex[:12]}"
    token = TRACE_ID.set(tid)
    try:
        yield tid
    finally:
        TRACE_ID.reset(token)


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------

_MASK = "[REDACTED]"


def mask(text: str) -> str:
    """对一段可日志文本做令牌脱敏。

    命中以下形式即打码：`Authorization: Bearer ...`、URL query 的
    `sign=`/`token=`/`key=`/`secret=`、以及 `.token=`/`key:`/`secret=` 等
    令牌类字段的取值。纯字符串替换，可安全用于任何日志消息。
    """
    if not text:
        return text
    # Authorization 请求头（Bearer 或裸值）
    text = re.sub(r"(?i)(Authorization:\s*Bearer\s+)\S+", rf"\1{_MASK}", text)
    text = re.sub(r"(?i)(Authorization:\s*)\S+(?=$|\s|[,;])", rf"\1{_MASK}", text)
    # URL query 里的令牌参数
    text = re.sub(
        r"(?i)([?&](?:sign|token|key|secret|api_key)=)([^&\s]*)",
        lambda m: f"{m.group(1)}{_MASK}",
        text,
    )
    # 令牌类字段 `=`/`:` 后的取值（含 .token=VALUE 加点写法）。
    # 只用 `=`/`:` 分隔——松散的空格写法（如 "secret to"）是正常英语，不应误伤。
    text = re.sub(
        r"(?i)((?:\.|^|(?<=\s))"
        r"(?:token|key|secret|password|api_key|webhook_secret)\b)"
        r"(\s*[=:]\s*)(?!\[REDACTED\])\S+",
        lambda m: f"{m.group(1)}{m.group(2)}{_MASK}",
        text,
    )
    return text


class SensitiveFilter(logging.Filter):
    """对 record 的消息打码（供 handler 使用，兼容不经过 formatter 的 handler）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        masked = mask(msg)
        if masked != msg:
            # 记录打码后的消息与原始参数，raw_args 保留 `%s` 的位置
            record.msg = masked
            record.args = ()
        return True


def _masked_message(record: logging.LogRecord) -> str:
    return mask(record.getMessage())


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------

class StandardFormatter(logging.Formatter):
    """输出与 uvicorn/fastapi 一致的标准文本（`INFO:     msg`）。

    通过 `levelprefix` 复刻 uvicorn 的对齐方式；脱敏由 `SensitiveFilter`
    在 formatter 之前完成，因此标准格式下令牌仍会被打码。
    """

    def format(self, record: logging.LogRecord) -> str:
        # 复刻 uvicorn 的 levelprefix：`INFO:     `（冒号后按 8 宽度补空格对齐）
        record.levelprefix = f"{record.levelname}:{' ' * (8 - len(record.levelname))}"
        return super().format(record)


class JsonFormatter(logging.Formatter):
    """把 LogRecord 渲染成单行 JSON（含脱敏、trace_id、业务字段）。"""

    _BASE_FIELDS = (
        "task_id", "provider", "repo", "pr_node", "event", "service",
    )

    def format(self, record: logging.LogRecord) -> str:
        # 日志 `ts` 面向读日志的人，用本地时区（并按系统 TZ 带偏移）；DB 里仍是 UTC 存储（§11）。
        ts = datetime.fromtimestamp(record.created).astimezone().isoformat()
        payload: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "trace_id": record.__dict__.get("trace_id") or TRACE_ID.get(),
            "message": _masked_message(record),
        }
        for field in self._BASE_FIELDS:
            if field in record.__dict__:
                payload[field] = record.__dict__[field]
        if record.exc_info:
            (typ, value, _tb) = record.exc_info
            if typ and value:
                payload["exception"] = f"{typ.__name__}: {value}"
        return json.dumps(payload, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# 日志装配
# ---------------------------------------------------------------------------

def get_logger(name: str = LOG_LOGGER_NAME) -> logging.Logger:
    """拿到统一 logger；trace_id 由 worker/webhook 通过 `extra={"trace_id": ...}` 注入。"""
    return logging.getLogger(name)


def setup_logging(
    *,
    log_level: str = "INFO",
    fmt: logging.Formatter | None = None,
    handler: logging.Handler | None = None,
) -> logging.Logger:
    """配置 `codereview_ai` 命名空间的根 logger（幂等）。

    Args:
        log_level: 日志级别。
        fmt: 自定义 formatter（默认 `StandardFormatter`，与 uvicorn 输出一致；
             需要结构化 JSON 时显式传 `JsonFormatter()`）。
        handler: 自定义 output（默认 stdout StreamHandler）。便于测试注入 StringIO。
    """
    logger = logging.getLogger(LOG_LOGGER_NAME)
    logger.setLevel(log_level.upper())
    logger.propagate = False
    if not handler:
        handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(fmt or StandardFormatter(fmt="%(levelprefix)s %(message)s"))
    handler.addFilter(SensitiveFilter())
    # 幂等：避免重复 add
    if not any(h is handler for h in logger.handlers):
        logger.handlers.clear()
        logger.addHandler(handler)
    return logger
