#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L8 · 标记图谱里的垃圾药名（**标记，不删除**）
=============================================

## 为什么先修这个（其余三个靶点都排在它后面）

L8 诊断出五类问题，按"**是否污染用户看得见的答案**"排序，只有一类是压倒性的：

| 脏药名 | 被当成"推荐用药"出现在 |
|---|---|
| `8-11` | **119** 个疾病 |
| `SP` | **109** 个 |
| `Ⅰ` | **71** 个 |
| `pH4` | **61** 个 |

共 **28 个脏名、584 条推荐边**，而且已经污染了 L5 的 4 道题答案
（Q06/Q07/Q08/Q09 里出现 `冻干`/`Ⅰ`/`Ⅱ`/`SP`）。

对比另外三类：自环 112 条**不进 L5 的 6 类意图、也不进 L6**（用户看不到）；
作为禁忌对象的脏名 **0 条**；超长名多数是**合法的疫苗株名**。

## 为什么"标记"而不是"删除"

删除图节点**不可逆**，而这些"垃圾"里混着判断风险——比如 `18AA-Ⅰ` 是复方氨基酸
注射液的组分名，它可能是某个母体药的一部分。**先打标，让所有查询能一致地过滤；
要不要真删，等看过清单的人决定。**

标记是**可逆**的：`--unflag` 摘掉属性即可，数据不动一行。

## 判据（两类，都很保守）

1. **纯编码/规格**：整名只由字母/数字/罗马数字/冒号斜杠短横组成，且 ≤6 字
   （`1`、`21`、`2∶1`、`30R`、`15AA`、`18AA-Ⅰ`、`pH4`、`SP`、`TP-MCT`…）
2. **孤立的剂型/工艺词**：整名就是一个剂型或工艺词，没有实质成分
   （`冻干`、`注射用`、`口服`、`片`、`胶囊`…）

⚠️ **不做**：按"名字短"或"名字长"判——那会误伤 `吗啡`（2 字，合法）
和疫苗株名（合法但很长）。判据必须能说清"为什么它不像药名"。

用法
----
    python scripts/flag_junk_nodes.py                 # 只预览（默认，不写库）
    python scripts/flag_junk_nodes.py --apply         # 打标
    python scripts/flag_junk_nodes.py --unflag        # 撤销打标
"""

from __future__ import annotations

import argparse
import io
import re
import sys

from opencmkg_io import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "backend"))

#: 整名只由字母/数字/罗马数字/冒号斜杠短横点组成 → 像编码而不像药名
CODE_RE = re.compile(r"^[0-9A-Za-zⅠⅡⅢⅣⅤ∶:\-/\.]+$")

#: 整名就是一个剂型/工艺词（没有实质成分）。
BARE_FORM_WORDS = frozenset({
    "冻干", "注射用", "口服", "外用", "粉针", "水针", "片", "胶囊", "颗粒",
    "注射液", "口服液", "软膏", "乳膏", "凝胶", "喷雾剂", "气雾剂", "栓",
    "散", "丸", "滴丸", "糖浆", "贴", "酊", "洗剂", "搽剂", "擦剂",
    "复方", "提取物", "流浸膏", "浸膏",
})

#: 打标写在这个属性上。**用质量字段而不是删节点**，所有查询按它过滤即可。
QUALITY_KEY = "quality"
REASON_KEY = "junk_reason"


def log(msg: str = "") -> None:
    print(msg, flush=True)


def classify(name: str) -> str | None:
    """返回垃圾原因；不是垃圾返回 ``None``。"""
    if not name:
        return None
    if name in BARE_FORM_WORDS:
        return "孤立剂型/工艺词（没有实质成分）"
    if CODE_RE.match(name) and len(name) <= 6:
        return "纯编码/规格（不像药名）"
    return None


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="标记图谱里的垃圾药名")
    ap.add_argument("--apply", action="store_true", help="真的打标")
    ap.add_argument("--unflag", action="store_true", help="撤销打标")
    args = ap.parse_args(argv)

    from app.config import get_settings
    from neo4j import GraphDatabase

    st = get_settings()
    driver = GraphDatabase.driver(st.neo4j_uri,
                                 auth=(st.neo4j_user, st.neo4j_password))
    log("L8 · 标记垃圾药名（标记而非删除，可逆）")
    log(f"  Neo4j  {st.neo4j_uri}")
    log("")

    with driver.session() as s:
        if args.unflag:
            n = s.run("MATCH (x:Drug) WHERE x.`" + QUALITY_KEY + "` = 'junk' "
                      "SET x.`" + QUALITY_KEY + "` = null, "
                      "x.`" + REASON_KEY + "` = null RETURN count(x) AS c"
                      ).single()["c"]
            log(f"  ✓ 已撤销 {n} 个节点的 junk 标记")
            driver.close()
            return 0

        names = [r["n"] for r in s.run(
            "MATCH (x:Drug) WHERE x.name IS NOT NULL RETURN x.name AS n")]
        hits = [(n, classify(n)) for n in names]
        hits = [(n, why) for n, why in hits if why]

        log(f"  Drug 总数 {len(names)}；判定为垃圾 {len(hits)} 个")
        by_reason: dict[str, list[str]] = {}
        for n, why in hits:
            by_reason.setdefault(why, []).append(n)
        for why, ns in by_reason.items():
            log(f"    {why}：{len(ns)} 个")
            log(f"      {sorted(ns)[:20]}")

        # 影响面：这些名字被当成"推荐用药"多少次（这是修它的理由）
        affected = s.run(
            "MATCH (dz:Disease)-[r:RECOMMEND_DRUG]->(x:Drug) "
            "WHERE x.name IN $ns RETURN count(r) AS c, "
            "count(DISTINCT x) AS k", ns=[n for n, _ in hits]).single()
        log("")
        log(f"  影响面：{affected['k']} 个脏名被当成推荐用药，共 "
            f"**{affected['c']}** 条边")

        if not args.apply:
            log("")
            log("  （预览模式，未写库。加 --apply 才真的打标）")
            driver.close()
            return 0

        for n, why in hits:
            s.run("MATCH (x:Drug {name: $n}) "
                  "SET x.`" + QUALITY_KEY + "` = 'junk', "
                  "x.`" + REASON_KEY + "` = $why", n=n, why=why)
        got = s.run("MATCH (x:Drug) WHERE x.`" + QUALITY_KEY + "` = 'junk' "
                    "RETURN count(x) AS c").single()["c"]
        log("")
        log(f"  ✓ 已打标 {got} 个节点（`{QUALITY_KEY}='junk'`）")
        log(f"    撤销：python scripts/flag_junk_nodes.py --unflag")

    driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
