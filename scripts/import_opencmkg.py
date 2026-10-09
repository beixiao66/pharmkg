#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L1 · 药品骨架导入 —— OpenCMKG → Neo4j
=====================================

把 OpenCMKG 的实体字典与三元组写进 Neo4j，构成整个医药图谱的骨架。
后续 L2（DDInter 相互作用）、L4（说明书抽取的禁忌/特殊人群）都挂在这些骨架上。

用法
----
    python scripts/import_opencmkg.py                  # 全量导入
    python scripts/import_opencmkg.py --sample 2000    # 每个关系只导前 2000 条（冒烟）
    python scripts/import_opencmkg.py --stats-only     # 只打印图里现状 + 冒烟测试，不写
    python scripts/import_opencmkg.py --reset          # 先清掉上次导入的 opencmkg 数据

幂等
----
写入**一律用 MERGE**，重跑不会产生重复节点或重复边（基线 §13.8）。
中途失败可以直接重跑，不必先清理。

来源标记
--------
所有节点与边都带 ``source = "opencmkg"``（基线 §13.8）。
``--reset`` 只删带这个标记的东西，**不动其他来源**——L2 的 DDI 边、L4 的说明书
抽取结果都不会被误伤。

设计取舍
--------
* **不做药名归一化**。"盐酸二甲双胍肠溶片" 原样入库。归一化是 L2/L4 接缝
  与查询期实体链接（§6.5 第 ① 级）的事，在 L1 做会丢掉剂型信息且无法回退。
* **上游数据的三处毛病在读取层统一修复**（见 ``opencmkg_io.load_entities_dict``）：
  裸 ``nan``、``symptom`` 里的嵌套 list（救回 6,833 个被埋掉的症状）、展平后去重。
  这些修复都带计数打印出来，不静默吞掉。
* **`SameAs` 只在两端都能反查到类型时建边**。实测 17,977 对里有 3,999 对
  至少一端不在词表中（没有节点可挂），跳过并计数，不造无类型的垃圾节点。
* **`Check` 节点从三元组现场收集**：`entities_dict` 里根本没有 `check` 类型
  （实测 `disease_need_check` 的尾实体 100% 不在词表中）。
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from typing import Any

from opencmkg_io import (  # noqa: E402
    NODE_LABEL,
    RELATION_TYPES,
    REPO_ROOT,
    SAME_AS_RELATION,
    SAME_AS_TYPE,
    SOURCE_TAG,
    build_name_index,
    load_entities_dict,
    load_triples,
)

# scripts/ 与 backend/ 是两个独立目录。这里把 backend 挂进 sys.path，
# 只为复用 app.config —— 配置必须只有一处来源，不在脚本里再解析一遍 .env。
sys.path.insert(0, str(REPO_ROOT / "backend"))
from app.config import get_settings  # noqa: E402

#: 每批提交多少行。太大占内存，太小网络往返多。
BATCH_SIZE = 5000


# ---------------------------------------------------------------- 基础设施

def log(msg: str = "") -> None:
    print(msg, flush=True)


def connect():
    """建 Neo4j 连接。连不上就快速失败，不让脚本干等。"""
    from neo4j import GraphDatabase

    settings = get_settings()
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
        connection_timeout=10,
        connection_acquisition_timeout=10,
    )
    driver.verify_connectivity()
    return driver


def ensure_constraints(session) -> None:
    """为每个 Label 建 name 唯一约束。

    这不只是数据质量保证——**MERGE 必须走索引，否则每行都要全标签扫描**，
    354,755 条三元组会慢到不可用。
    """
    for label in NODE_LABEL.values():
        name = f"pharmkg_{label.lower()}_name"
        session.run(
            f"CREATE CONSTRAINT {name} IF NOT EXISTS "
            f"FOR (n:`{label}`) REQUIRE n.name IS UNIQUE"
        )


def batched(rows: list, size: int = BATCH_SIZE):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


# ---------------------------------------------------------------- 写入

def import_nodes(session, entities: dict[str, list[str]]) -> dict[str, int]:
    """把 entities_dict 里的实体全部建成节点。

    即使某个实体在任何三元组里都没出现，也先建成孤立节点——
    词表完整了，查询期实体链接才能命中。
    """
    counts: dict[str, int] = {}
    for etype, names in entities.items():
        label = NODE_LABEL.get(etype)
        if label is None:
            log(f"  ⚠ 未知实体类型 {etype!r}，跳过 {len(names):,} 个")
            continue
        rows = [{"name": n, "id": f"{etype}:{n}"} for n in names]
        for batch in batched(rows):
            session.run(
                f"UNWIND $rows AS row "
                f"MERGE (n:`{label}` {{name: row.name}}) "
                f"ON CREATE SET n.id = row.id, n.source = $source",
                rows=batch,
                source=SOURCE_TAG,
            )
        counts[label] = len(rows)
    return counts


def import_relations(session, triples, sample: int | None):
    """按关系类型分批写入。返回 ``(写入统计, SameAs 的原始药对列表)``。

    SameAs 单独交给 :func:`import_same_as` —— 它两端类型要反查词表，
    和这里"关系名直接决定 Label"的 12 类关系走法不同。
    """
    by_rel: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for h, r, t in triples:
        by_rel[r].append((h, t))

    stats: dict[str, Any] = {"written": {}, "unknown_relations": {}, "sample": sample}

    for source_rel, (head_type, tail_type, rel_type) in RELATION_TYPES.items():
        pairs = by_rel.get(source_rel, [])
        if sample:
            pairs = pairs[:sample]
        if not pairs:
            continue

        head_label = NODE_LABEL[head_type]
        tail_label = NODE_LABEL[tail_type]
        rows = [
            {"h": h, "t": t, "h_id": f"{head_type}:{h}", "t_id": f"{tail_type}:{t}"}
            for h, t in pairs
        ]

        t0 = time.time()
        for batch in batched(rows):
            session.run(
                f"UNWIND $rows AS row "
                f"MERGE (a:`{head_label}` {{name: row.h}}) "
                f"  ON CREATE SET a.id = row.h_id, a.source = $source "
                f"MERGE (b:`{tail_label}` {{name: row.t}}) "
                f"  ON CREATE SET b.id = row.t_id, b.source = $source "
                f"MERGE (a)-[r:`{rel_type}`]->(b) "
                f"  ON CREATE SET r.source = $source",
                rows=batch,
                source=SOURCE_TAG,
            )
        stats["written"][rel_type] = len(rows)
        log(f"  {rel_type:<20} {len(rows):>8,} 条   {time.time() - t0:5.1f}s")

    stats["unknown_relations"] = {
        r: len(p) for r, p in by_rel.items()
        if r not in RELATION_TYPES and r != SAME_AS_RELATION
    }
    return stats, by_rel.get(SAME_AS_RELATION, [])


def import_same_as(session, pairs, name_index: dict[str, str], sample: int | None):
    """写入 OpenCMKG 自带的 SameAs（同义词）关系。

    两端类型从词表反查——实测 53,863 个实体名**没有一个跨类型**，所以无歧义。
    反查不到的**直接跳过**：没有节点可挂，而造一个无类型的占位节点
    会污染图谱、也说不清它是什么。
    """
    stat = {"written": 0, "skipped_oov": 0, "deduped": 0, "examined": 0}
    seen = set()
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)

    for h, t in pairs:
        if sample is not None and stat["examined"] >= sample:
            break
        stat["examined"] += 1
        key = (h, t)
        if key in seen:
            stat["deduped"] += 1
            continue
        seen.add(key)

        h_type, t_type = name_index.get(h), name_index.get(t)
        if h_type is None or t_type is None:
            stat["skipped_oov"] += 1
            continue
        groups[(NODE_LABEL[h_type], NODE_LABEL[t_type])].append({"h": h, "t": t})

    for (h_label, t_label), rows in sorted(groups.items()):
        for batch in batched(rows):
            session.run(
                f"UNWIND $rows AS row "
                f"MERGE (a:`{h_label}` {{name: row.h}}) "
                f"MERGE (b:`{t_label}` {{name: row.t}}) "
                f"MERGE (a)-[r:`{SAME_AS_TYPE}`]->(b) "
                f"  ON CREATE SET r.source = $source",
                rows=batch,
                source=SOURCE_TAG,
            )
        stat["written"] += len(rows)
        log(f"  {SAME_AS_TYPE:<20} {len(rows):>8,} 条   ({h_label} → {t_label})")
    return stat


# ---------------------------------------------------------------- 只读

def collect_stats(session) -> dict[str, Any]:
    nodes = {
        rec["label"]: rec["c"]
        for rec in session.run(
            "MATCH (n) RETURN coalesce(labels(n)[0], '<无标签>') AS label, count(*) AS c"
        )
    }
    rels = {
        rec["rel"]: rec["c"]
        for rec in session.run("MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS c")
    }
    by_source = {
        rec["src"]: rec["c"]
        for rec in session.run(
            "MATCH (n) RETURN coalesce(n.source, '<无来源>') AS src, count(*) AS c"
        )
    }
    isolated = session.run(
        "MATCH (n) WHERE NOT (n)--() RETURN count(n) AS c"
    ).single()["c"]
    total_nodes = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
    return {
        "nodes": nodes,
        "rels": rels,
        "by_source": by_source,
        "isolated": isolated,
        "total_nodes": total_nodes,
        "total_rels": sum(rels.values()),
    }


def print_stats(stats: dict[str, Any]) -> None:
    log("")
    log("── 图规模 ──────────────────────────────────────────────")
    log(f"  节点合计   {stats['total_nodes']:>9,}    （其中孤立节点 {stats['isolated']:,}）")
    log(f"  关系合计   {stats['total_rels']:>9,}")
    log("")
    log("  按 Label：")
    for label, c in sorted(stats["nodes"].items(), key=lambda kv: -kv[1]):
        log(f"    {label:<14} {c:>9,}")
    log("")
    log("  按关系类型：")
    for rel, c in sorted(stats["rels"].items(), key=lambda kv: -kv[1]):
        log(f"    {rel:<20} {c:>9,}")
    log("")
    log("  按来源标记（应全部是 opencmkg）：")
    for src, c in sorted(stats["by_source"].items(), key=lambda kv: -kv[1]):
        log(f"    {src:<14} {c:>9,}")


def smoke_test(session) -> bool | None:
    """L1 的验收标准：能查到"高血压 → 推荐药物"。

    采样模式下**返回 None 跳过**——每类只导前 N 条时，"高血压"大概率还没轮到
    （源文件是按疾病顺序排的），这时报失败是误导。
    """
    log("")
    log("── 冒烟测试（基线 §13.2 的 L1 跑通标志）──────────────────")
    rows = list(session.run(
        "MATCH (d:Disease)-[:RECOMMEND_DRUG]->(dr:Drug) "
        "WHERE d.name CONTAINS '高血压' "
        "RETURN d.name AS disease, collect(DISTINCT dr.name) AS drugs "
        "ORDER BY size(drugs) DESC LIMIT 5"
    ))
    if not rows:
        log("  ✗ 没有查到任何「高血压 → 推荐药物」结果")
        return False

    for rec in rows:
        drugs = rec["drugs"]
        log(f"  {rec['disease']}  →  {len(drugs)} 种药")
        log(f"      {'、'.join(drugs[:8])}{' …' if len(drugs) > 8 else ''}")

    total = session.run(
        "MATCH (d:Disease)-[:RECOMMEND_DRUG]->(dr:Drug) "
        "WHERE d.name CONTAINS '高血压' RETURN count(DISTINCT dr) AS c"
    ).single()["c"]
    log("")
    log(f"  ✓ 「高血压」共关联 {total} 种推荐药物")
    return True


def reset(session) -> dict[str, int]:
    """清掉本脚本上次导入的东西，不碰其他来源。

    **必须分批删**：354,752 条关系放一个事务里删，会直接撑爆 Neo4j 的
    单事务内存上限（实测报 ``MemoryPoolOutOfMemoryError``，上限 716.8 MiB）。
    用 ``CALL { ... } IN TRANSACTIONS`` 让服务端每 1 万行提交一次。
    """
    rel_count = session.run(
        "MATCH ()-[r {source: $source}]->() RETURN count(r) AS c",
        source=SOURCE_TAG,
    ).single()["c"]
    session.run(
        "MATCH ()-[r {source: $source}]->() "
        "CALL (r) { DELETE r } IN TRANSACTIONS OF 10000 ROWS",
        source=SOURCE_TAG,
    )

    # 关系删完后，本来源的节点全成了孤立点；同样分批删
    node_count = session.run(
        "MATCH (n {source: $source}) WHERE NOT (n)--() RETURN count(n) AS c",
        source=SOURCE_TAG,
    ).single()["c"]
    session.run(
        "MATCH (n {source: $source}) WHERE NOT (n)--() "
        "CALL (n) { DELETE n } IN TRANSACTIONS OF 10000 ROWS",
        source=SOURCE_TAG,
    )
    return {"rels_deleted": rel_count, "isolated_nodes_deleted": node_count}


# ---------------------------------------------------------------- 入口

def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="把 OpenCMKG 骨架导入 Neo4j")
    parser.add_argument("--sample", type=int, default=None,
                        help="每个关系只导前 N 条（冒烟用；不传则全量）")
    parser.add_argument("--stats-only", action="store_true",
                        help="只打印图里现状与冒烟测试，不写任何数据")
    parser.add_argument("--reset", action="store_true",
                        help="先删除上次由本脚本写入的 opencmkg 节点与边")
    args = parser.parse_args(argv)

    settings = get_settings()
    log("L1 · OpenCMKG 骨架导入")
    log(f"  Neo4j     {settings.neo4j_uri}")
    log(f"  来源标记  {SOURCE_TAG}")
    if args.sample:
        log(f"  采样模式  每个关系只导前 {args.sample:,} 条")
    log("")

    t_start = time.time()
    driver = None
    try:
        driver = connect()
        with driver.session() as session:
            if args.reset:
                log("── 清理上次导入 ──")
                info = reset(session)
                log(f"  删除关系 {info['rels_deleted']:,} 条，"
                    f"孤立节点 {info['isolated_nodes_deleted']:,} 个")
                log("")

            if not args.stats_only:
                log("── 建唯一约束（MERGE 的索引基础）──")
                ensure_constraints(session)
                log(f"  {len(NODE_LABEL)} 个 Label 约束就绪")
                log("")

                log("── 读取原始数据 ──")
                # 先读三元组：词表里那条嵌套 list 的类型归属要靠关系证据来定
                triples, tstats = load_triples()
                entities, ed_stats = load_entities_dict(triples=triples)
                name_index = build_name_index(entities)
                log(f"  实体 {sum(len(v) for v in entities.values()):,} 个（{len(entities)} 类）")
                log(f"  三元组 {len(triples):,} 条（共 {tstats['total']:,} 行，"
                    f"无法解析 {tstats['unrecoverable']} 行）")
                log(f"  上游数据修复：展平嵌套 list {ed_stats['nested_lists']} 个"
                    f"（{ed_stats['nested_elements']:,} 个条目）")
                log(f"    其中 {ed_stats['nested_as_check']:,} 个按关系证据归为 check，"
                    f"{ed_stats['nested_as_symptom']:,} 个保留 symptom")
                log(f"    去重 {ed_stats['duplicates_removed']:,} 个，"
                    f"nan 占位 {ed_stats['nan_placeholders']} 个")
                log("")

                log("── 写入节点 ──")
                node_counts = import_nodes(session, entities)
                for label, c in sorted(node_counts.items(), key=lambda kv: -kv[1]):
                    log(f"  {label:<14} {c:>9,}")
                log("")

                log("── 写入关系 ──")
                rel_stats, same_as_pairs = import_relations(session, triples, args.sample)

                log("")
                log("── 写入 SameAs（同义词）──")
                sa = import_same_as(session, same_as_pairs, name_index, args.sample)
                log(f"  建边 {sa['written']:,} 条；"
                    f"跳过 {sa['skipped_oov']:,} 条（端点不在词表，无节点可挂）；"
                    f"去重 {sa['deduped']:,} 条")
                if sa["skipped_oov"]:
                    log("  → 这些同义词信息仍保留在 triples.txt 里，")
                    log("    L5 的别名表直接从文件读，不受影响。")

                if rel_stats["unknown_relations"]:
                    log("")
                    log(f"  ⚠ 未在 RELATION_TYPES 中登记的关系：{rel_stats['unknown_relations']}")

            stats = collect_stats(session)
            print_stats(stats)
            if args.sample:
                log("")
                log("  （采样模式：跳过冒烟测试——每类只导前 N 条时，")
                log("    『高血压』大概率还没轮到，报失败会误导。去掉 --sample 跑全量即可。）")
                ok = True
            else:
                ok = smoke_test(session)

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
