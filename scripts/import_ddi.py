#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L2 · 药物相互作用导入 —— DDInter → Neo4j
========================================

把 DDInter 的 160,235 对相互作用写进图谱，挂在 L1 建好的药物骨架上。

用法
----
    python scripts/import_ddi.py                 # 全量导入（约 1 分钟）
    python scripts/import_ddi.py --limit 2000    # 每个等级只导前 2000 对（冒烟）
    python scripts/import_ddi.py --stats-only    # 只打印现状 + 验收查询，不写
    python scripts/import_ddi.py --reset         # 先清掉上次导入的 ddinter 数据

节点映射策略（基线决策 #27：**方案 B**）
----------------------------------------
第 0 步实验证明：只有 **6.0%** 的 Major 药对两端能映射到 OpenCMKG 已有的药。
若只导入能映射上的边，药箱放两个药会有 94% 的概率答"无记录"——L6 形同虚设。

所以：

========================================  ====================================
情况                                      处理
========================================  ====================================
①–④ 级「确定命中」                        挂到 L1 已有的 OpenCMKG 药物节点
⑤⑥ 级「待确认」                            **按未映射处理**，新建节点
未命中 / 无译文                            新建节点，用 LLM 译名作节点名
========================================  ====================================

⚠️ **「待确认」为什么不当映射用**：第 0 步实测编辑距离 53 条命中里约 20 条是错的
（`柔红霉素→红霉素`、`氯噻嗪→氢氯噻嗪`）。把柔红霉素的相互作用挂到红霉素上，
在医疗场景是**实质性的错误**。所以宁可为它新建一个节点——代价只是图谱里
多一个"没有适应症"的药，而不是给用户一条错的相互作用。

这些新建节点带 ``source = "ddinter"``，与 L1 的 ``source = "opencmkg"`` 区分开，
便于按来源回滚（基线 §13.8）。它们**只有相互作用数据，没有适应症**——
论文里要把「DDI 覆盖」与「药品详情覆盖」两个口径分开陈述。

边属性
------
只有 ``level`` 与 ``source``（基线决策 #19 的方案 A——CSV 里没有机制与处理建议）。
方向按**节点名排序后固定**，保证同一对药只有一条边、重跑幂等。
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter, defaultdict

from opencmkg_io import REPO_ROOT, SOURCE_TAG

# scripts/ 与 backend/ 是两个独立目录。把 backend 挂进 sys.path 只为复用 app.config
sys.path.insert(0, str(REPO_ROOT / "backend"))
try:
    from app.config import get_settings
except ModuleNotFoundError as _exc:  # pragma: no cover
    _venv = REPO_ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
    print(f"✗ 缺少依赖 {_exc.name!r} —— 当前用的 Python 解释器不对。")
    print(f"  请改用：\"{_venv}\" scripts\\import_ddi.py")
    raise SystemExit(1)

from build_drug_name_mapping import CONFIRMED_LEVELS  # noqa: E402
from ddinter_io import (  # noqa: E402
    DDI_SOURCE,
    LEVEL_RANK,
    LEVELS_ORDERED,
    load_mapping,
    load_pairs,
)

#: L1 写入的标记（用来判断节点是不是"已有的骨架药物"）
SKELETON_SOURCE = SOURCE_TAG

BATCH_SIZE = 5000


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 读取

def resolve_nodes(mapping: dict[str, dict], names: set[str]) -> dict[str, dict]:
    """把每个 DDInter 药名解析成一个图谱节点。

    返回 ``{en_name: {"node": 节点名, "is_new": 是否新建, "level": 匹配级别}}``。
    """
    resolved: dict[str, dict] = {}
    for en in names:
        row = mapping.get(en)
        if row is None or not row["cn_name"]:
            # 连译文都没有（实测只有 1 个）——用英文名兜底，仍然建节点
            resolved[en] = {"node": en, "is_new": True, "match": "无译文"}
            continue
        if row["match_level"] in CONFIRMED_LEVELS:
            resolved[en] = {"node": row["matched_cn"], "is_new": False,
                            "match": row["match_level"]}
        else:
            # 「待确认」与「未命中」一视同仁：新建节点。
            # 理由见模块开头——把柔红霉素的边挂到红霉素上是实质性错误。
            resolved[en] = {"node": row["cn_name"], "is_new": True,
                            "match": row["match_level"]}
    return resolved


def annotate_skeleton_nodes(session, resolved: dict[str, dict]) -> int:
    """给**匹配上的骨架节点**补上英文名。

    为什么需要：``Warfarin`` 去盐归一化后挂到了 ``华法林钠片``——如果不在节点上
    留下英文名，事后就说不清这个 DDInter 药物到底挂到了哪里，验收查询也没法按
    来源追溯。基线 §5 的 Drug 本来就该有 ``name_en``。

    一个骨架药若对应多个 DDInter 名，**只记下第一个**（``coalesce``）。
    实测这种情况很少，且不影响正确性——映射表里完整对应关系都在。
    """
    rows = [
        {"name": info["node"], "en": en}
        for en, info in resolved.items() if not info["is_new"]
    ]
    for batch in batched(rows):
        session.run(
            "UNWIND $rows AS row "
            "MATCH (d:`Drug` {name: row.name}) "
            "SET d.name_en = coalesce(d.name_en, row.en)",
            rows=batch,
        )
    return len(rows)


def batched(rows: list, size: int = BATCH_SIZE):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


# ---------------------------------------------------------------- 写入

def import_new_drug_nodes(session, resolved: dict[str, dict]) -> int:
    """为"映射不上"的药建节点。带 source=ddinter 与英文名。"""
    rows = [
        {"name": info["node"], "en": en}
        for en, info in resolved.items() if info["is_new"]
    ]
    for batch in batched(rows):
        session.run(
            "UNWIND $rows AS row "
            "MERGE (d:`Drug` {name: row.name}) "
            "  ON CREATE SET d.id = 'drug:' + row.name, "
            "                d.source = $source, "
            "                d.name_en = row.en",
            rows=batch,
            source=DDI_SOURCE,
        )
    return len(rows)


#: 严重程度排序（LEVEL_RANK）与等级顺序（LEVELS_ORDERED）都定义在 ddinter_io 里，
#: 与 L3 选样共用同一份定义，避免两处各写一份而悄悄漂移。


def import_interactions(session, pairs, resolved, limit: int | None):
    """写 INTERACTS_WITH 边。

    两件事必须在写之前**显式**做掉，不能交给 MERGE 的匹配顺序决定：

    1. **方向固定**：按节点名排序后从小的指向大的，这样同一对药只有一条边，
       重跑不会产生反向重复边。
    2. **等级归并**：不同的 DDInter 药对可能解析到**同一对节点**
       （例如 ``Insulin human (regular)`` 与 ``Insulin human (isophane)``
       都译成「人胰岛素」，它们各自的相互作用就落到同一对节点上）。
       这时取**最严重**的那个等级——在用药安全场景，宁可高估风险也不能低估。
       （实测这类归并约占 6%。）

    把归并放在 Python 侧而不是靠 ``MERGE ... ON CREATE``，是为了让"保留哪一级"
    由代码写死、可复现，而不是取决于批次顺序。
    """
    node_pair_levels: dict[tuple[str, str], list[str]] = defaultdict(list)
    unresolved = 0
    for key, level in pairs.items():
        a, b = sorted(key)
        ra, rb = resolved.get(a), resolved.get(b)
        if ra is None or rb is None:
            unresolved += 1
            continue
        na, nb = ra["node"], rb["node"]
        if na == nb:
            # 两个不同的 DDInter 药名解析到了同一个节点——自环，跳过
            unresolved += 1
            continue
        node_pair_levels[(na, nb) if na <= nb else (nb, na)].append(level)

    merged_conflicts = sum(
        1 for levels in node_pair_levels.values() if len(set(levels)) > 1)
    merged_total = sum(len(levels) - 1 for levels in node_pair_levels.values())

    by_level: dict[str, list[dict]] = defaultdict(list)
    for (lo, hi), levels in node_pair_levels.items():
        worst = min(levels, key=lambda lv: LEVEL_RANK.get(lv, 9))
        by_level[worst].append({"a": lo, "b": hi})

    written: dict[str, int] = {}
    for level in LEVELS_ORDERED:
        rows = by_level.get(level, [])
        if limit:
            rows = rows[:limit]
        if not rows:
            continue
        t0 = time.time()
        for batch in batched(rows):
            session.run(
                "UNWIND $rows AS row "
                "MATCH (a:`Drug` {name: row.a}) "
                "MATCH (b:`Drug` {name: row.b}) "
                "MERGE (a)-[r:`INTERACTS_WITH`]->(b) "
                "  ON CREATE SET r.level = $level, r.source = $source",
                rows=batch,
                level=level,
                source=DDI_SOURCE,
            )
        written[level] = len(rows)
        log(f"  {level:<10} {len(rows):>8,} 条   {time.time() - t0:5.1f}s")

    return written, unresolved, merged_total, merged_conflicts


def reset(session) -> dict[str, int]:
    """只清本次来源的数据，不碰 L1 的骨架。

    ⚠️ 顺序很重要：先删边，再删**由本脚本创建且已无任何边**的节点。
    用 ``source = 'ddinter'`` 作判据，L1 建的 62,479 个骨架节点一个都不会动。
    """
    rels = session.run(
        "MATCH ()-[r {source: $source}]->() RETURN count(r) AS c",
        source=DDI_SOURCE,
    ).single()["c"]
    session.run(
        "MATCH ()-[r {source: $source}]->() "
        "CALL (r) { DELETE r } IN TRANSACTIONS OF 10000 ROWS",
        source=DDI_SOURCE,
    )
    nodes = session.run(
        "MATCH (n {source: $source}) WHERE NOT (n)--() RETURN count(n) AS c",
        source=DDI_SOURCE,
    ).single()["c"]
    session.run(
        "MATCH (n {source: $source}) WHERE NOT (n)--() "
        "CALL (n) { DELETE n } IN TRANSACTIONS OF 10000 ROWS",
        source=DDI_SOURCE,
    )
    return {"rels_deleted": rels, "nodes_deleted": nodes}


# ---------------------------------------------------------------- 只读

def collect_stats(session) -> dict:
    by_source = {
        rec["src"]: rec["c"]
        for rec in session.run(
            "MATCH (d:Drug) RETURN coalesce(d.source, '<无来源>') AS src, count(*) AS c"
        )
    }
    rel_levels = {
        rec["lv"]: rec["c"]
        for rec in session.run(
            "MATCH ()-[r:INTERACTS_WITH]->() RETURN coalesce(r.level,'<无>') AS lv, count(*) AS c"
        )
    }
    total_drugs = session.run("MATCH (d:Drug) RETURN count(d) AS c").single()["c"]
    total_ddi = session.run(
        "MATCH ()-[r:INTERACTS_WITH]->() RETURN count(r) AS c"
    ).single()["c"]
    # 既有适应症又有相互作用的药（DDI 与骨架成功对接的那部分）
    linked = session.run(
        "MATCH (d:Drug)-[:INTERACTS_WITH]-() WHERE d.source = $s "
        "RETURN count(DISTINCT d) AS c",
        s=SKELETON_SOURCE,
    ).single()["c"]
    return {
        "drugs_by_source": by_source,
        "levels": rel_levels,
        "total_drugs": total_drugs,
        "total_ddi": total_ddi,
        "skeleton_drugs_with_ddi": linked,
    }


def print_stats(stats: dict) -> None:
    log("")
    log("── 图规模（Drug 相关）────────────────────────────────")
    log(f"  Drug 节点合计   {stats['total_drugs']:>9,}")
    for src, c in sorted(stats["drugs_by_source"].items(), key=lambda kv: -kv[1]):
        log(f"    {src:<14} {c:>9,}")
    log("")
    log(f"  INTERACTS_WITH 边 {stats['total_ddi']:>9,}")
    for lv in ("Major", "Moderate", "Minor", "Unknown"):
        if lv in stats["levels"]:
            log(f"    {lv:<10} {stats['levels'][lv]:>9,}")
    log("")
    log(f"  ★ 既有适应症又有 DDI 的骨架药：{stats['skeleton_drugs_with_ddi']:,}")
    log("    （其余 DDI 挂在新建节点上——它们只有相互作用、没有适应症）")


def smoke_test(session, resolved: dict[str, dict]) -> bool:
    """L2 的验收标准（基线 §13.2）：能查到"华法林 ↔ 乙酰水杨酸"的等级。

    **按系统真实路径测**：先用映射结果把用户会说的药名解析成节点名，再查边。
    这一步很关键——``Warfarin`` 实际挂到的是 ``华法林钠片``（去盐归一化匹配），
    硬写 ``华法林`` 去查会查不到，让人误以为是数据问题。
    """
    log("")
    log("── 验收测试（基线 §13.2 的 L2 跑通标志）──────────────")

    a_en, b_en = "Warfarin", "Acetylsalicylic acid"
    a_node = resolved.get(a_en, {}).get("node", a_en)
    b_node = resolved.get(b_en, {}).get("node", b_en)
    log(f"  药名解析：{a_en} → {a_node!r}")
    log(f"            {b_en} → {b_node!r}")

    rows = list(session.run(
        "MATCH (a:`Drug` {name: $a})-[r:`INTERACTS_WITH`]-(b:`Drug` {name: $b}) "
        "RETURN a.name AS a, b.name AS b, r.level AS level, r.source AS src",
        a=a_node, b=b_node,
    ))
    if not rows:
        log(f"  ✗ 查不到 {a_node} ↔ {b_node}")
        return False
    for rec in rows:
        log(f"  ✓ {rec['a']} ↔ {rec['b']}   Level={rec['level']}   来源={rec['src']}")

    major = session.run(
        "MATCH ()-[r:`INTERACTS_WITH` {level:'Major'}]->() RETURN count(r) AS c"
    ).single()["c"]
    log(f"  全图 Major 级边共 {major:,} 条")
    return True


# ---------------------------------------------------------------- 入口

def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="把 DDInter 相互作用导入 Neo4j")
    parser.add_argument("--limit", type=int, default=None,
                        help="每个等级只导前 N 对（冒烟用）")
    parser.add_argument("--stats-only", action="store_true",
                        help="只打印现状与验收查询，不写数据")
    parser.add_argument("--reset", action="store_true",
                        help="先删除上次由本脚本写入的 ddinter 边与节点")
    args = parser.parse_args(argv)

    settings = get_settings()
    log("L2 · DDInter 相互作用导入")
    log(f"  Neo4j     {settings.neo4j_uri}")
    log(f"  来源标记  {DDI_SOURCE}")
    if args.limit:
        log(f"  采样模式  每个等级只导前 {args.limit:,} 对")
    log("")

    t_start = time.time()
    driver = None
    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            connection_timeout=10,
            connection_acquisition_timeout=10,
        )
        driver.verify_connectivity()

        with driver.session() as session:
            if args.reset:
                log("── 清理上次导入 ──")
                info = reset(session)
                log(f"  删除边 {info['rels_deleted']:,} 条，"
                    f"节点 {info['nodes_deleted']:,} 个（只删 source=ddinter 的）")
                log("")

            # 读取与解析无论是否写库都要做：--stats-only 也要跑验收测试，
            # 而验收测试需要 resolved（要知道"华法林"实际挂到了哪个节点）。
            log("── 读取映射表与 DDInter 数据 ──")
            mapping = load_mapping()
            pairs, n_records, conflicts = load_pairs()
            log(f"  映射表        {len(mapping):,} 条")
            log(f"  DDInter 记录  {n_records:,} 条")
            log(f"  去重后药对    {len(pairs):,}")
            if conflicts:
                log(f"  ⚠ 同一药对出现不同等级：{len(conflicts)} 例，取首次出现的")
                for c in conflicts[:5]:
                    log(f"      {c}")
            log("")

            names = {n for key in pairs for n in key}
            resolved = resolve_nodes(mapping, names)
            new_cnt = sum(1 for v in resolved.values() if v["is_new"])
            log("── 节点映射（决策 #27 方案 B）──")
            log(f"  涉及药物      {len(resolved):,}")
            log(f"  挂到骨架节点  {len(resolved) - new_cnt:,}")
            log(f"  新建 ddinter 节点 {new_cnt:,}")
            log("")
            log("  按匹配级别：")
            for lv, c in Counter(v["match"] for v in resolved.values()).most_common():
                log(f"    {lv:<16} {c:>6,}")
            log("")

            if not args.stats_only:
                log("── 写入新建药物节点 ──")
                n = import_new_drug_nodes(session, resolved)
                log(f"  新建/合并 {n:,} 个节点")
                log("")

                log("── 给匹配上的骨架节点补英文名 ──")
                n = annotate_skeleton_nodes(session, resolved)
                log(f"  标注 {n:,} 个节点（用于追溯 DDInter 名挂到了哪里）")
                log("")

                log("── 写入 INTERACTS_WITH 边 ──")
                written, unresolved, merged_total, merged_conflicts = \
                    import_interactions(session, pairs, resolved, args.limit)
                if unresolved:
                    log(f"  （跳过 {unresolved:,} 对：两端解析到同一节点）")
                log(f"  （{merged_total:,} 对被并入同一对节点——"
                    f"其中 {merged_conflicts:,} 对原本等级不同，取最严重的）")

            stats = collect_stats(session)
            print_stats(stats)
            if args.limit:
                log("")
                log("  （采样模式：跳过验收测试，去掉 --limit 跑全量即可）")
                ok = True
            else:
                ok = smoke_test(session, resolved)

        log("")
        log(f"完成，用时 {time.time() - t_start:.1f}s")
        return 0 if ok else 1
    except Exception as exc:  # noqa: BLE001
        log("")
        log(f"✗ 导入失败：{type(exc).__name__}: {exc}")
        log("  写入用的是 MERGE，修好问题后直接重跑即可，不必先 --reset。")
        return 1
    finally:
        if driver is not None:
            driver.close()


if __name__ == "__main__":
    raise SystemExit(main())
