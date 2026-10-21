"""图谱浏览接口：实体检索、邻域子图、最短路径。

## 三个都必须有上限，否则一次点击能拖垮服务

* **子图**：中心节点可能是"枢纽"——有的药有 230 条相互作用、有的病有 137 条推荐用药。
  不设上限，前端会收到几千个节点，G6 直接卡死。所以按边数截断并**显式返回
  `truncated`**，让界面能说"这里只画了一部分"。
* **路径**：`shortestPath` 在 50 万条边的图上没有长度上限就是全图搜索。
  这里限制最多 6 跳 + 事务超时。
* **实体检索**：跨标签查名字，按"度数"（有多少条边）降序——**用户点进去是看关系的，
  孤点进去没东西看**。

## 为什么不做"按关系类型过滤"

看起来很有用，但**图谱里有 30+ 种关系类型，让用户先选类型再浏览，
比先看到一张图再决定要不要收窄，认知负担大得多**。先给全景 + 截断提示，
需要时再加过滤。这是取舍，不是遗漏。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from neo4j import GraphDatabase

from app.config import get_settings
from app.deps import get_current_user
from app.models import AppUser
from app.schemas import GraphEntityHit, GraphPath, GraphSubgraph

router = APIRouter(prefix="/graph", tags=["图谱浏览"])

#: 子图边数上限（一个中心节点最多返回这么多条边）
MAX_EDGES = 200
#: 路径最大跳数。**不设上限就是全图搜索**。
MAX_HOPS = 6
#: 路径查询的事务超时（秒）。超时宁可报错，也不要挂住请求。
PATH_TIMEOUT = 8.0


def _driver():
    st = get_settings()
    return GraphDatabase.driver(st.neo4j_uri,
                               auth=(st.neo4j_user, st.neo4j_password))


@router.get("/entities", response_model=list[GraphEntityHit],
            summary="实体检索（跨标签，按度数降序）")
def search_entities(q: str = Query(min_length=1, max_length=32),
                    limit: int = Query(default=20, ge=1, le=50),
                    user: AppUser = Depends(get_current_user)) -> list[GraphEntityHit]:
    """按名字精确或前缀检索**任意标签**的实体。

    ⚠️ 度数用 `count { (n)--() }` 算。**按度数降序**是因为浏览的目的是看关系：
    度数为 0 的孤点点进去是空白页，排在前面只会浪费点击。
    """
    drv = _driver()
    try:
        with drv.session() as s:
            rows = list(s.run(
                "MATCH (n) WHERE n.name = $q OR n.name STARTS WITH $q "
                "WITH n, count { (n)--() } AS deg "
                "RETURN n.name AS name, labels(n)[0] AS label, deg "
                "ORDER BY CASE WHEN n.name = $q THEN 0 ELSE 1 END, deg DESC, n.name "
                "LIMIT $lim", q=q, lim=limit))
        return [GraphEntityHit(name=r["name"], label=r["label"] or "Node",
                               degree=r["deg"]) for r in rows]
    finally:
        drv.close()


@router.get("/subgraph", response_model=GraphSubgraph, summary="邻域子图（1 跳）")
def subgraph(name: str = Query(min_length=1, max_length=64),
             limit: int = Query(default=120, ge=1, le=MAX_EDGES),
             user: AppUser = Depends(get_current_user)) -> GraphSubgraph:
    """以某实体为中心，取它的直接邻居。

    ⚠️ **多取一条边来判断有没有被截断**：SQL 里 `LIMIT n` 之后你无法知道
    "是刚好 n 条，还是还有更多"。所以查 `limit + 1`，多出来的那条扔掉、
    并把 `truncated` 置真——**界面因此能说"只画了一部分"，而不是让人以为图就长这样**。
    """
    drv = _driver()
    try:
        with drv.session() as s:
            c = s.run("MATCH (c) WHERE c.name = $n "
                      "RETURN c.name AS name, labels(c)[0] AS label LIMIT 1",
                      n=name).single()
            if not c:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                    detail=f"图谱里没有名为「{name}」的实体")
            rows = list(s.run(
                "MATCH (c {name: $n})-[r]-(o) "
                "RETURN type(r) AS t, o.name AS oname, labels(o)[0] AS olabel, "
                "startNode(r) = c AS outgoing "
                "ORDER BY t, o.name LIMIT $lim", n=name, lim=limit + 1))
    finally:
        drv.close()

    truncated = len(rows) > limit
    rows = rows[:limit]
    nodes = [{"id": c["name"], "label": c["name"], "type": c["label"] or "Node"}]
    seen = {c["name"]}
    edges = []
    for r in rows:
        on = r["oname"]
        if not on:
            continue
        if on not in seen:
            seen.add(on)
            nodes.append({"id": on, "label": on, "type": r["olabel"] or "Node"})
        a, b = (c["name"], on) if r["outgoing"] else (on, c["name"])
        edges.append({"source": a, "target": b, "label": r["t"], "type": r["t"]})
    return GraphSubgraph(center=c["name"], center_label=c["label"] or "Node",
                         nodes=nodes, edges=edges, truncated=truncated,
                         edge_limit=limit)


@router.get("/path", response_model=GraphPath, summary="两点之间的最短路径")
def shortest_path(src: str = Query(min_length=1, max_length=64),
                  dst: str = Query(min_length=1, max_length=64),
                  max_hops: int = Query(default=4, ge=1, le=MAX_HOPS),
                  user: AppUser = Depends(get_current_user)) -> GraphPath:
    """两点之间的最短路径（最多 `max_hops` 跳）。

    ⚠️ **不设跳数上限就是全图搜索**（50 万条边），所以默认 4 跳、上限 6 跳，
    并且给事务超时。超时不当作"没有路径"——那是两件不同的事，必须分开报。
    """
    drv = _driver()
    try:
        with drv.session() as s:
            miss = [x for x in (src, dst)
                    if not s.run("MATCH (n {name: $n}) RETURN count(n) AS c",
                                 n=x).single()["c"]]
            if miss:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                    detail=f"图谱里没有 {miss}")
            try:
                row = s.run(
                    "MATCH (a {name: $a}), (b {name: $b}), "
                    f"p = shortestPath((a)-[*..{max_hops}]-(b)) "
                    "RETURN [n IN nodes(p) | {name: n.name, label: labels(n)[0]}] "
                    "AS ns, "
                    "[r IN relationships(p) | {type: type(r), "
                    "from: startNode(r).name, to: endNode(r).name}] AS es",
                    a=src, b=dst, timeout=PATH_TIMEOUT).single()
            except Exception as exc:  # noqa: BLE001
                # 超时/中断与"没有路径"是两件事，不能混
                raise HTTPException(
                    status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                    detail=(f"路径查询超时（{type(exc).__name__}）。"
                            f"可调小 max_hops 再试；**这不代表两点之间没有路径**。")
                ) from exc
        if not row:
            return GraphPath(src=src, dst=dst, found=False, hops=0,
                             nodes=[], edges=[],
                             note=f"{max_hops} 跳以内没有找到路径")
        ns, es = row["ns"], row["es"]
        return GraphPath(
            src=src, dst=dst, found=True, hops=len(es),
            nodes=[{"id": n["name"], "label": n["name"],
                    "type": n["label"] or "Node"} for n in ns],
            edges=[{"source": e["from"], "target": e["to"],
                    "label": e["type"], "type": e["type"]} for e in es],
            note="")
    finally:
        drv.close()
