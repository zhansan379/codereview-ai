"""一次性回填：把存量 review_finding.file 里的 cr-static 临时目录前缀剥掉。

背景：早期 ruff/semgrep 以输入目录路径落库（修复见 PR #7），老行 file 形如
`C:\\...\\Temp\\cr-static-<token>\\tests\\unit\\a.py`，与仓库相对 new_path 对不上。
本脚本把 `...cr-static-<token>/` 前缀剥掉并按魔术/正斜杠归一，使其与仓库相对路径一致。

用法：
    .venv/Scripts/python.exe scripts/backfill_static_file_path.py
    # 或指定库： DATABASE_URL="sqlite:///./data/app.db" 同上（默认已指向该库）

幂等：只处理含 `cr-static-` 前缀的行；重复运行第二次 0 变更。静态不依赖应用运行。
"""

from __future__ import annotations

import asyncio
import os
import re

from sqlalchemy import select

from codereview_ai.storage.db import create_engine, session_factory
from codereview_ai.storage.models import ReviewFinding

#: 匹配形如 `.../cr-static-<token>/` 的前缀（兼容 / 与 \）；其后即仓库相对路径。
_TMP_PREFIX = re.compile(r"^.*[\\/]cr-static-[^\\/]*[\\/]")

DEFAULT_URL = os.environ.get("DATABASE_URL", "sqlite:///./data/app.db")


def _fix_file(f: str) -> str | None:
    if "cr-static" not in f:
        return None
    m = _TMP_PREFIX.match(f)
    if not m:
        return None
    rel = f[m.end():].replace("\\", "/")
    return rel


async def main(url: str) -> None:
    engine = create_engine(url)
    session = session_factory(engine)
    changed: int = 0
    samples: list[str] = []
    async with session() as s:
        rows = (await s.execute(select(ReviewFinding))).scalars().all()
        for row in rows:
            fixed = _fix_file(row.file or "")
            if fixed is None or fixed == row.file:
                continue
            if len(samples) < 3:
                samples.append(f"{row.file}  ->  {fixed}")
            row.file = fixed
            changed += 1
        await s.commit()
    print(f"已回填 {changed} 条 review_finding.file")
    for line in samples:
        print("  " + line)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(DEFAULT_URL))