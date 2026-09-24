"""FastAPI 应用入口。

当前只提供健康检查接口，作用是**验证骨架是否搭通**：
Neo4j 与 PostgreSQL 两个数据库都能连上，才算骨架就绪。

启动：
    cd backend
    uvicorn app.main:app --reload --port 8000

验证：
    浏览器打开 http://localhost:8000/docs
    或执行          curl http://localhost:8000/health
"""

from typing import Any, Dict

from fastapi import FastAPI
from neo4j import GraphDatabase
from sqlalchemy import create_engine, text

from app.config import get_settings

app = FastAPI(
    title="pharmkg API",
    description="基于 AI 与知识图谱的医药查询与智能用药系统",
    version="0.1.0",
)


@app.get("/", summary="根路径")
def root() -> Dict[str, str]:
    return {"service": "pharmkg", "docs": "/docs", "health": "/health"}


@app.get("/health", summary="健康检查：验证 Neo4j 与 PostgreSQL 是否连通")
def health() -> Dict[str, Any]:
    """分别探测两个数据库。

    刻意不抛异常：即使某个库连不上也返回 200，把失败原因写在响应体里，
    这样前端和自测脚本能一眼看出是哪一侧断了，而不是只拿到一个 500。
    """
    settings = get_settings()

    # ── Neo4j ────────────────────────────────────────────────
    neo4j_status: Dict[str, Any] = {"ok": False, "uri": settings.neo4j_uri}
    driver = None
    try:
        driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
        with driver.session() as session:
            record = session.run("MATCH (n) RETURN count(n) AS c").single()
            neo4j_status["nodes"] = record["c"] if record else 0
        neo4j_status["ok"] = True
    except Exception as exc:  # noqa: BLE001 —— 健康检查要吞掉所有异常
        neo4j_status["error"] = "{0}: {1}".format(type(exc).__name__, exc)
    finally:
        if driver is not None:
            driver.close()

    # ── PostgreSQL ───────────────────────────────────────────
    pg_status: Dict[str, Any] = {"ok": False, "database": settings.postgres_db}
    engine = None
    try:
        engine = create_engine(settings.postgres_dsn, pool_pre_ping=True)
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version()")).scalar()
        pg_status["ok"] = True
        pg_status["version"] = str(version).split(",")[0][:60]
    except Exception as exc:  # noqa: BLE001
        pg_status["error"] = "{0}: {1}".format(type(exc).__name__, exc)
    finally:
        if engine is not None:
            engine.dispose()

    both_ok = bool(neo4j_status["ok"] and pg_status["ok"])
    return {
        "status": "ok" if both_ok else "degraded",
        "neo4j": neo4j_status,
        "postgres": pg_status,
    }
