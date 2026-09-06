"""存储层：抽象接口 + 双档引擎（DESIGN §8）。

- 业务层只依赖 `base.py` 的抽象接口，不 import SQLAlchemy 模型。
- `db.py` 提供 async engine / session，SQLite 自动应用 WAL / busy_timeout / foreign_keys。
"""
