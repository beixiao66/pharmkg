"""数据库连接与会话管理（PostgreSQL / SQLAlchemy 2.0）。

这一层只做三件事：建引擎、给每个请求一个 Session、给出 ``Base``。
业务代码不要在这里写，也不要散落 ``create_engine``。

⚠️ **两个容易踩的点**

1. ``pool_pre_ping=True``。PostgreSQL 容器重启、或连接被中间设备掐掉之后，
   连接池里那些"看着还在、其实已经死了"的连接会让**第一个请求**报
   ``OperationalError``。pre_ping 会在取连接时先探一下，坏的就换掉。
2. ``expire_on_commit=False``。默认值 ``True`` 意味着 commit 之后访问任何属性
   都会再发一次 SELECT；在 FastAPI 里"commit → 序列化响应"这个模式中，
   那一次多余的查询既慢又容易在 Session 已关闭后炸出 ``DetachedInstanceError``。
"""

from __future__ import annotations

from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。

    用 SQLAlchemy 2.0 的 ``DeclarativeBase``（而不是 1.x 的 ``declarative_base()``），
    这样模型上的类型注解能被 mypy 认出来。
    """


_settings = get_settings()

#: ``future=True`` 是 2.0 风格；``echo`` 关掉，SQL 日志靠需要时单独开。
engine = create_engine(
    _settings.postgres_dsn,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个 Session，请求结束一定关闭。

    ``yield`` 放在 ``try/finally`` 里——**异常路径也必须关连接**，
    否则一次报错就会漏掉一个连接，攒够了就把连接池耗干。
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
