"""集中读取环境变量。

约定：所有配置项都在这里声明，业务代码里禁止散落 ``os.getenv``。
这样做的原因有两个：
  1. 换环境（本地 / 容器）时只需改 .env，不用翻遍代码；
  2. 缺哪个配置一目了然，不会出现"某个键拼错了导致静默用默认值"。
"""

from functools import lru_cache
from pathlib import Path
import sys
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
    #: 用 127.0.0.1 而非 localhost：后者会解析出 IPv6 + IPv4 两个地址，
    #: 连不上时超时按地址各付一次，健康检查平白慢一倍（实测 4s vs 2s）。
    neo4j_uri: str = "bolt://127.0.0.1:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""

    # ── PostgreSQL ───────────────────────────────────────────
    postgres_host: str = "127.0.0.1"   # 同上：不用 localhost，避免双栈重复超时
    postgres_port: int = 5432
    postgres_user: str = "pharmkg"
    postgres_password: str = ""
    postgres_db: str = "pharmkg"

    # ── 大模型：阿里云百炼（基线决策 #22，单一 provider）─────────
    #: 不再维护 provider 切换分支；多模型对比靠改 llm_model 实现。
    dashscope_api_key: Optional[str] = None
    #: OpenAI 兼容地址。**已实测打通**的是老域名（无需业务空间 ID）：
    #: https://dashscope.aliyuncs.com/compatible-mode/v1
    #: 官方推荐改用业务空间专属域名：
    #: https://<业务空间ID>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
    dashscope_base_url: str = ""
    #: 默认 qwen-plus——它可用文本接口；qwen3.8-* 等需多模态接口，别随手换
    llm_model: str = "qwen-plus"

    @property
    def llm_configured(self) -> bool:
        """LLM 是否配齐。

        调用方（L5）必须先查这个，缺配置时**明确报错**——
        静默降级成"没有 LLM 的问答"会让问题在很远的地方才暴露。
        """
        return bool(self.dashscope_api_key and self.dashscope_base_url)

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


# ---------------------------------------------------------------- 配置自检

def _mask(secret: Optional[str]) -> str:
    """打码显示密钥，供自检输出用。

    短密钥**一位都不显示**（只报长度），长密钥只显示首尾各 4 位——
    自检结果可能会被贴进聊天记录或 issue 里，不能让它变成泄漏渠道。
    """
    if not secret:
        return "✗ 未设置"
    if len(secret) <= 8:
        return f"✓ 已设置（{len(secret)} 字符）"
    return f"✓ {secret[:4]}****{secret[-4:]}（{len(secret)} 字符）"


def _self_check() -> int:
    """``python -m app.config`` —— 打印解析后的配置（密钥打码）。

    排查"为什么读不到配置"时先跑这个：它能区分
    「.env 没被读到」「键名拼错」「值确实没填」三种情况。
    退出码 0 = 配置齐全；1 = 还缺东西。
    """
    s = get_settings()
    env_file = ROOT_DIR / ".env"

    print("pharmkg 配置自检")
    print(f"  项目根目录       {ROOT_DIR}")
    print(f"  .env             {'存在' if env_file.exists() else '✗ 不存在（当前用代码默认值）'}")
    print()
    print("  ── 数据库 ──")
    print(f"  Neo4j            {s.neo4j_uri}   用户 {s.neo4j_user}")
    print(f"  Neo4j 密码       {_mask(s.neo4j_password)}")
    print(f"  PostgreSQL       {s.postgres_user}@{s.postgres_host}:{s.postgres_port}/{s.postgres_db}")
    print(f"  PostgreSQL 密码  {_mask(s.postgres_password)}")
    print()
    print("  ── 大模型（阿里云百炼，决策 #22）──")
    print(f"  状态             {'✓ 已配齐' if s.llm_configured else '✗ 未配齐（L5 之前必须补上）'}")
    print(f"  模型             {s.llm_model}")
    print(f"  base_url         {s.dashscope_base_url or '✗ 未设置'}")
    print(f"  API Key          {_mask(s.dashscope_api_key)}")

    if not s.llm_configured:
        print()
        print("  补法：把 .env.example 里的大模型段落抄进 .env；")
        print("        DASHSCOPE_BASE_URL 照抄百炼控制台给出的完整地址（含业务空间 ID）。")

    return 0 if s.llm_configured else 1


if __name__ == "__main__":
    # Windows 控制台默认 GBK，直接 print "✓" 会抛 UnicodeEncodeError；强制 UTF-8
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    raise SystemExit(_self_check())
