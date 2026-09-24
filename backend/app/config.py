"""集中读取环境变量。

约定：所有配置项都在这里声明，业务代码里禁止散落 ``os.getenv``。
这样做的原因有两个：
  1. 换环境（本地 / 容器）时只需改 .env，不用翻遍代码；
  2. 缺哪个配置一目了然，不会出现"某个键拼错了导致静默用默认值"。
"""

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py → 上溯三级 = 项目根目录
ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """环境变量配置。字段名大小写不敏感，对应 .env 中的大写键名。"""

    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Neo4j ────────────────────────────────────────────────
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""

    # ── PostgreSQL ───────────────────────────────────────────
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "pharmkg"
    postgres_password: str = ""
    postgres_db: str = "pharmkg"

    # ── 大模型 ───────────────────────────────────────────────
    llm_provider: str = "deepseek"
    deepseek_api_key: Optional[str] = None
    dashscope_api_key: Optional[str] = None

    @property
    def postgres_dsn(self) -> str:
        """SQLAlchemy 连接串。"""
        return (
            "postgresql+psycopg://{user}:{pwd}@{host}:{port}/{db}".format(
                user=self.postgres_user,
                pwd=self.postgres_password,
                host=self.postgres_host,
                port=self.postgres_port,
                db=self.postgres_db,
            )
        )


@lru_cache()
def get_settings() -> Settings:
    """带缓存的配置读取，避免每次请求都重新解析 .env。"""
    return Settings()
