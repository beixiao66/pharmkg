"""Alembic 环境（L7）。

与默认模板的三处不同，都有理由：

1. **连接串从 `app.config` 取**，不读 `alembic.ini`——密码不进仓库，换环境只改 `.env`。
2. **`target_metadata = Base.metadata`**，且**先把 `app.models` import 进来**。
   不 import 的话 `Base.metadata` 是空的，`--autogenerate` 会以为"所有表都该删掉"，
   生成一个把库清空的迁移。这是 alembic 最经典的事故。
3. **`compare_type=True`**：默认不比较列类型，改类型时 autogenerate 会静默漏掉。
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# backend/alembic/env.py → parents[1] = backend/，把它放进 sys.path 才能 import app.*
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings          # noqa: E402
from app.db import Base                      # noqa: E402
from app import models                       # noqa: E402,F401  ← 必须 import 才有表

config = context.config
config.set_main_option("sqlalchemy.url", get_settings().postgres_dsn)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库。"""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库执行。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection,
                          target_metadata=target_metadata,
                          compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
