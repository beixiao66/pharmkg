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

#: 健康检查里**每个数据库**的探测预算（秒）。
#:
#: 为什么必须显式设超时：驱动与连接池的默认值都很宽松
#: （Neo4j 建连 30s + 连接池等待 60s + 事务重试 30s；libpq 则完全不限），
#: 数据库不可达时 ``/health`` 会挂住好几分钟。**健康检查挂住比返回 degraded 更糟**
#: ——调用方（前端探活、容器 healthcheck、演示脚本）会被一起拖死，
#: 而且现象是"页面卡住"而不是"明确报错"，很难排查。
#:
#: 取值依据（数据库指向无进程监听的端口，实测）：
#:
#:     timeout   Neo4j   PostgreSQL(localhost)   PostgreSQL(127.0.0.1)
#:       1.0s    1.00s          4.13s                  —
#:       2.0s    2.00s          4.00s                2.00s
#:       3.0s    2.00s          6.00s                3.00s
#:
#: 两点结论：① Neo4j 驱动自己有 2s 上限，再调大没用；
#: ② **`localhost` 会解析出 IPv6 + IPv4 两个地址，超时按地址各付一次**，
#: 所以 ``config.py`` 里数据库 host 一律默认 ``127.0.0.1`` 而非 ``localhost``。
#:
#: 因此最坏情况 ≈ 2 × 2.0s = 4s（两个库依次探测），数据库正常时约 1s。
HEALTH_PROBE_TIMEOUT = 2.0

app = FastAPI(
    title="pharmkg API",
    description="基于 AI 与知识图谱的医药查询与智能用药系统",
    version="0.1.0",
)


@app.get("/", summary="根路径")
def root() -> Dict[str, str]:
    return {"service": "pharmkg", "docs": "/docs", "health": "/health"}


def _probe_neo4j(settings) -> Dict[str, Any]:
    """探测 Neo4j 是否可查询，顺带取节点数。

    刻意不抛异常：失败原因写进返回值，让调用方能一眼看出是哪一侧断了。
    """
    status: Dict[str, Any] = {"ok": False, "uri": settings.neo4j_uri}
    driver = None
    try:
        driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            # 单次建立连接的上限
            connection_timeout=HEALTH_PROBE_TIMEOUT,
            # 从连接池取连接的等待上限
            connection_acquisition_timeout=HEALTH_PROBE_TIMEOUT,
            # 不重试：健康检查要的是"此刻通不通"。默认会重试 30 秒，
            # 那 30 秒里即使服务其实已经好了也照样被判成不可用，没有意义。
            max_transaction_retry_time=0,
        )
        with driver.session() as session:
            record = session.run("MATCH (n) RETURN count(n) AS c").single()
            status["nodes"] = record["c"] if record else 0
        status["ok"] = True
    except Exception as exc:  # noqa: BLE001 —— 健康检查要吞掉所有异常
        status["error"] = "{0}: {1}".format(type(exc).__name__, exc)
    finally:
        if driver is not None:
            driver.close()
    return status


def _probe_postgres(settings) -> Dict[str, Any]:
    """探测 PostgreSQL 是否可连接。失败原因同样写进返回值而不抛出。"""
    status: Dict[str, Any] = {"ok": False, "database": settings.postgres_db}
    engine = None
    try:
        engine = create_engine(
            settings.postgres_dsn,
            pool_pre_ping=True,
            # libpq 的 connect_timeout 只接受整数秒
            connect_args={"connect_timeout": int(HEALTH_PROBE_TIMEOUT)},
        )
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version()")).scalar()
        status["ok"] = True
        status["version"] = str(version).split(",")[0][:60]
    except Exception as exc:  # noqa: BLE001
        status["error"] = "{0}: {1}".format(type(exc).__name__, exc)
    finally:
        if engine is not None:
            engine.dispose()
    return status


@app.get("/health", summary="健康检查：验证 Neo4j 与 PostgreSQL 是否连通")
def health() -> Dict[str, Any]:
    """分别探测两个数据库。

    刻意不抛异常：即使某个库连不上也返回 200，把失败原因写在响应体里，
    这样前端和自测脚本能一眼看出是哪一侧断了，而不是只拿到一个 500。

    **无论数据库通不通，本接口都在 HEALTH_PROBE_TIMEOUT 的量级内返回。**
    """
    settings = get_settings()

    neo4j_status = _probe_neo4j(settings)
    pg_status = _probe_postgres(settings)

    both_ok = bool(neo4j_status["ok"] and pg_status["ok"])
    return {
        "status": "ok" if both_ok else "degraded",
        "neo4j": neo4j_status,
        "postgres": pg_status,
    }
