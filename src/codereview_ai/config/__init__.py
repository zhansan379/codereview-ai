"""配置层子包（DESIGN §16）：集中配置 + DB 驱动分层合并。

`Settings` 由此包重新导出，兼容历史 `from codereview_ai.config import Settings`。
"""

from __future__ import annotations

from codereview_ai.config.settings import REQUIRED_SECRETS, Settings

__all__ = ["REQUIRED_SECRETS", "Settings"]
