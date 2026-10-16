#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L5 · 验证问题集的金标准（逐条跑 Cypher）
========================================

**金标准不能靠印象。** L4 已经栽过两次：一次是拿 `entities_dict` 当目标词表
（图里其实多 133 个名字），一次是把"没有金标准就不可评测"当成理由。
所以这 28 条问题里的每一条金标准，都**跑一遍**再说它成立。

对每条问题检查两件事：

1. **金标准查得出东西**——跑 `gold_cypher`，行数 ≥ `min_rows`。
   不成立说明**这条问题在图谱里没有答案**，它对 L5 毫无意义，必须改掉或删掉。
2. **实体说得出口**——`must_link` 里写的每个名字，在图里**能不能找到**。
   找不到不代表问题错（Q23「卡维地洛」就**应该**找不到、要靠补全），
   而是要把结论明确记下来，供实体链接那一轮对照。

用法
----
    python scripts/verify_l5_questions.py
    python scripts/verify_l5_questions.py --show-rows      # 顺便打印金标准查出来的行
"""

from __future__ import annotations

import argparse
import io
import sys

from insert_io import read_csv_rows
from opencmkg_io import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "backend"))

QUESTIONS_FILE = REPO_ROOT / "data" / "eval" / "l5_questions.csv"


def log(msg: str = "") -> None:
    print(msg, flush=True)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="验证 L5 问题集的金标准")
    ap.add_argument("--show-rows", action="store_true", help="打印查出来的行")
    args = ap.parse_args(argv)

    from app.config import get_settings
    from neo4j import GraphDatabase

    rows, enc = read_csv_rows(QUESTIONS_FILE)
    st = get_settings()
    driver = GraphDatabase.driver(st.neo4j_uri,
                                 auth=(st.neo4j_user, st.neo4j_password))

    log("L5 问题集 · 金标准验证")
    log(f"  问题集    {QUESTIONS_FILE.relative_to(REPO_ROOT)}"
        f"（{len(rows)} 条 / 编码 {enc}）")
    log(f"  Neo4j     {st.neo4j_uri}")
    log("")

    bad: list[str] = []
    with driver.session() as s:
        for r in rows:
            qid = r["qid"].strip()
            intent = r["intent"].strip()
            q = r["question"].strip()
            cypher = (r.get("gold_cypher") or "").strip()
            min_rows = int((r.get("min_rows") or "0").strip() or 0)

            # ── ① 金标准查得出东西吗 ──────────────────────────────
            if not cypher:
                # 「其他」那几条：本就不该有事实，只登记不判对错
                log(f"  ○ {qid} [{intent}] {q}")
                log(f"      无金标准 Cypher（本就不该给事实）")
                continue

            try:
                got = list(s.run(cypher))
                n = len(got)
            except Exception as exc:  # noqa: BLE001
                log(f"  ✗ {qid} [{intent}] {q}")
                log(f"      Cypher 跑不通：{type(exc).__name__}: {str(exc)[:90]}")
                bad.append(qid)
                continue

            ok = n >= min_rows
            if not ok:
                bad.append(qid)
            log(f"  {'✓' if ok else '✗'} {qid} [{intent}] {q}")
            log(f"      金标准 {n} 行（要求 ≥{min_rows}）")
            if args.show_rows and ok:
                for rec in got[:4]:
                    log(f"        · {list(rec.values())}")
                if n > 4:
                    log(f"        · …… 另有 {n - 4} 行")

            # ── ② must_link 里写的名字在图里找不找得到 ─────────────
            names = [x.strip() for x in (r.get("must_link") or "").split("；")
                     if x.strip()]
            for name in names:
                hit = list(s.run(
                    "MATCH (n) WHERE n.name = $x RETURN labels(n)[0] AS l LIMIT 1",
                    x=name))
                if hit:
                    log(f"      实体 {name!r} → 图里有（{hit[0]['l']}）")
                else:
                    pre = list(s.run(
                        "MATCH (n) WHERE n.name STARTS WITH $x "
                        "RETURN n.name AS n LIMIT 3", x=name))
                    if pre:
                        log(f"      实体 {name!r} → **图里没有完全同名的**，"
                            f"但有前缀匹配：{[p['n'] for p in pre]}")
                    else:
                        log(f"      实体 {name!r} → **图里找不到**（连前缀也没有）")

    driver.close()

    log("")
    log("── 汇总 ──")
    log(f"  问题 {len(rows)} 条；金标准不成立的 {len(bad)} 条"
        + (f"：{bad}" if bad else ""))
    if bad:
        log("  ✗ 这些问题的金标准查不出东西——它们对 L5 没有意义，"
            "要么改问题、要么改 Cypher。")
        return 1
    log("  ✓ 每条有金标准的问题都能查出东西")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
