#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L4 第③步 · 说明书抽取的边入图 —— `CONTRAINDICATED_FOR` → Neo4j
================================================================

把 L4 抽出来并对齐好的 ``(药, 节点)`` 写成图谱里的边，L4 到此闭环：
**说明书文本 → 禁忌原子 → 图谱节点 → 可查询的边**。

验收标准（基线 §13.4）：**能查到「某药 → 某人群禁用」**。

节点映射策略
------------
====================  ==========================================================
情况                   处理
====================  ==========================================================
说明书药名**精确命中**  挂到 L1 已有的 ``Drug`` 节点
命中不了               **新建物质级 ``Drug`` 节点**，``source="insert"``，
                      并把 OpenCMKG 里那些候选名记进 ``form_candidates``
疾病节点               图里有就挂上去，没有才新建（``source="insert"``）
人群节点               **图里一个都没有**（L1 没导过人群体），按决策 #30 的
                      受控词表新建 ``Population`` 节点
====================  ==========================================================

⚠️ **为什么"命中不了就新建"而不是硬挂到最像的那个**：实测 36 个药里有 **9 个**
命中不了，全都是 OpenCMKG 的**剂型分裂**——`来氟米特` 只有 `来氟米特片`、
`他克莫司` 只有 `他克莫司缓释胶囊`/`他克莫司胶囊`、`伤寒疫苗` 只有
`伤寒疫苗（减毒活）`/`（灭活）`。把一份**物质级**说明书挂到某一个**剂型级**节点上，
等于凭空断言"这个禁忌只属于那个剂型"，在医疗场景是实质性的错误。宁可新建一个
干净的物质级节点（与决策 #27 方案 B 同一条理由）。

边的属性（每一处都为了可追溯、可回滚、可过滤）
----------------------------------------------
``source``        固定 ``"insert"``，与 ``opencmkg`` / ``ddinter`` 区分，便于按来源回滚
``method``        产出它的方法名（如 ``section-json``）
``section``       来自哪一小节（决策 #30）
``align_level``   对齐器的判定级别（①精确 / ②包含 / ③前后缀 / ④新建 / ⑤LLM）
``confidence``    级别 → 置信档（high / medium / low，沿用决策 #28 的分档思路）
``evidence``      支撑这条边的**原子原文**（第一条）
``evidence_all``  全部支撑原子（去重排序）
``n_evidence``    支撑原子条数

⚠️ **诚实声明**：本脚本会把**实测的**第二层分数打出来。当前方法在 11 篇金标准上
F1 约 0.68，也就是**图里这批边有三成左右是假阳性**。它们带着 ``align_level`` /
``confidence`` 就是为了让下游（L6）能按可信度过滤——**别把这批边当 ground truth**。

用法
----
    python scripts/import_insert_edges.py --dry-run      # 只报告要写什么，不写库
    python scripts/import_insert_edges.py                # 写入（MERGE，可重跑）
    python scripts/import_insert_edges.py --stats-only   # 只打印现状 + 验收查询
    python scripts/import_insert_edges.py --reset        # 只删本脚本写的东西
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from align_nodes import Aligner, load_cache, save_cache
from insert_io import TEXT_DIR
from l4_pipeline import SECTION_RELATION, extract_edges, group_edges
from opencmkg_io import REPO_ROOT, SOURCE_TAG as SKELETON_SOURCE

sys.path.insert(0, str(REPO_ROOT / "backend"))

#: 本脚本写入的一切都带这个标记，`--reset` 与统计都按它认领
INSERT_SOURCE = "insert"

#: 允许写的关系类型——**白名单**，因为关系类型要拼进 Cypher 字符串
ALLOWED_RELATIONS = ("CONTRAINDICATED_FOR", "CAUTION_FOR")

POPULATION_LABEL = "Population"


def log(msg: str = "") -> None:
    print(msg, flush=True)


def connect():
    from app.config import get_settings
    from neo4j import GraphDatabase

    st = get_settings()
    driver = GraphDatabase.driver(
        st.neo4j_uri, auth=(st.neo4j_user, st.neo4j_password),
        connection_timeout=10, connection_acquisition_timeout=10)
    driver.verify_connectivity()
    return driver, st


# ---------------------------------------------------------------- 药名解析

def resolve_drugs(session, names: list[str]) -> dict[str, dict]:
    """说明书药名 → ``Drug`` 节点名。精确命中就挂上，否则新建物质级节点。"""
    out: dict[str, dict] = {}
    for name in names:
        hit = session.run("MATCH (d:`Drug` {name: $n}) RETURN d.name AS n",
                          n=name).single()
        if hit:
            out[name] = {"node": hit["n"], "is_new": False, "candidates": []}
            continue
        cands = [r["n"] for r in session.run(
            "MATCH (d:`Drug`) WHERE d.name CONTAINS $n "
            "RETURN d.name AS n ORDER BY n LIMIT 10", n=name)]
        out[name] = {"node": name, "is_new": True, "candidates": cands}
    return out


def import_new_drug_nodes(session, resolved: dict[str, dict], now: str) -> int:
    rows = [{"name": info["node"], "cands": info["candidates"]}
            for info in resolved.values() if info["is_new"]]
    if not rows:
        return 0
    session.run(
        "UNWIND $rows AS row "
        "MERGE (d:`Drug` {name: row.name}) "
        "  ON CREATE SET d.id = 'drug:' + row.name, "
        "                d.source = $source, "
        "                d.created_by = 'L4', "
        "                d.created_at = $now "
        "SET d.form_candidates = row.cands",
        rows=rows, source=INSERT_SOURCE, now=now)
    return len(rows)


# ---------------------------------------------------------------- 目标节点

def count_existing(session, label: str, names: list[str]) -> int:
    if not names:
        return 0
    return session.run(
        f"MATCH (n:`{label}`) WHERE n.name IN $names RETURN count(n) AS c",
        names=names).single()["c"]


def ensure_target_nodes(session, targets: dict[str, list[str]], now: str) -> dict:
    """确保目标节点存在。``targets`` = ``{"疾病": [名...], "人群": [名...]}``。

    返回 ``{"疾病": {"existing": n, "created": n}, ...}``——
    **"图里本来就有"与"这次新建"必须分开报**，否则说不清这批边动了图谱多少。
    """
    info: dict[str, dict] = {}
    spec = {"疾病": ("Disease", "disease"), "人群": (POPULATION_LABEL, "population")}
    for typ, names in targets.items():
        if not names:
            info[typ] = {"existing": 0, "created": 0}
            continue
        label, prefix = spec[typ]
        existing = count_existing(session, label, names)
        rows = [{"name": n, "id": f"{prefix}:{n}"} for n in sorted(set(names))]
        extra = ", n.vocab = 'controlled'" if typ == "人群" else ""
        session.run(
            f"UNWIND $rows AS row "
            f"MERGE (n:`{label}` {{name: row.name}}) "
            f"  ON CREATE SET n.id = row.id, n.source = $source, "
            f"                n.created_by = 'L4', n.created_at = $now{extra}",
            rows=rows, source=INSERT_SOURCE, now=now)
        info[typ] = {"existing": existing, "created": len(rows) - existing}
    return info


# ---------------------------------------------------------------- 写边

def write_edges(session, edges: list[dict], now: str) -> dict[str, int]:
    """写 ``CONTRAINDICATED_FOR`` / ``CAUTION_FOR`` 边。

    ``MERGE`` 保证重跑幂等；属性一律 ``SET``，所以改了抽取规则重跑即刷新，
    不会留下半新半旧的边。
    """
    by_rel: dict[str, list[dict]] = defaultdict(list)
    for e in edges:
        rel = SECTION_RELATION.get(e["section"])
        if rel is None:
            raise SystemExit(f"✗ 小节 {e['section']!r} 没有对应的关系类型")
        if rel not in ALLOWED_RELATIONS:
            raise SystemExit(f"✗ 关系类型 {rel!r} 不在白名单里")
        by_rel[rel].append({
            "drug": e["drug"], "node": e["node"], "label": e["label"],
            "method": e["method"], "section": e["section"],
            "level": e["align_level"], "confidence": e["confidence"],
            "evidence": e["evidence"], "evidence_all": e["evidence_all"],
            "n_evidence": len(e["evidence_all"]),
        })

    written: dict[str, int] = {}
    for rel, rows in sorted(by_rel.items()):
        n = 0
        for label in ("Disease", POPULATION_LABEL):
            sub = [r for r in rows if r["label"] == label]
            if not sub:
                continue
            session.run(
                f"UNWIND $rows AS row "
                f"MATCH (d:`Drug` {{name: row.drug}}) "
                f"MATCH (t:`{label}` {{name: row.node}}) "
                f"MERGE (d)-[r:`{rel}`]->(t) "
                f"  ON CREATE SET r.source = $source, r.created_at = $now "
                f"SET r.method = row.method, r.section = row.section, "
                f"    r.align_level = row.level, r.confidence = row.confidence, "
                f"    r.evidence = row.evidence, "
                f"    r.evidence_all = row.evidence_all, "
                f"    r.n_evidence = row.n_evidence, r.updated_at = $now",
                rows=sub, source=INSERT_SOURCE, now=now)
            n += len(sub)
        written[rel] = n
    return written


def reset(session) -> dict[str, int]:
    """只删本脚本写的东西：先删边，再删**已无任何边**的 ``source='insert'`` 节点。

    ⚠️ 顺序很重要（与 ``import_ddi.reset`` 同一条理由）：先删节点会把边留下变悬空。
    """
    rels = session.run(
        "MATCH ()-[r]->() WHERE r.source = $source "
        "AND type(r) IN $rels RETURN count(r) AS c",
        source=INSERT_SOURCE, rels=list(ALLOWED_RELATIONS)).single()["c"]
    session.run(
        "MATCH ()-[r]->() WHERE r.source = $source AND type(r) IN $rels "
        "CALL (r) { DELETE r } IN TRANSACTIONS OF 10000 ROWS",
        source=INSERT_SOURCE, rels=list(ALLOWED_RELATIONS))
    nodes = session.run(
        "MATCH (n {source: $source}) WHERE NOT (n)--() RETURN count(n) AS c",
        source=INSERT_SOURCE).single()["c"]
    session.run(
        "MATCH (n {source: $source}) WHERE NOT (n)--() "
        "CALL (n) { DELETE n } IN TRANSACTIONS OF 10000 ROWS",
        source=INSERT_SOURCE)
    return {"rels_deleted": rels, "nodes_deleted": nodes}


# ---------------------------------------------------------------- 只读

def collect_stats(session) -> dict:
    created = {
        r["lbl"]: r["c"] for r in session.run(
            "MATCH (n {source: $s}) UNWIND labels(n) AS lbl "
            "RETURN lbl, count(*) AS c ORDER BY c DESC", s=INSERT_SOURCE)}
    edges = {
        r["t"]: r["c"] for r in session.run(
            "MATCH ()-[r]->() WHERE r.source = $s "
            "RETURN type(r) AS t, count(*) AS c ORDER BY c DESC", s=INSERT_SOURCE)}
    by_level = {
        r["lv"]: r["c"] for r in session.run(
            "MATCH ()-[r]->() WHERE r.source = $s "
            "RETURN coalesce(r.align_level,'<无>') AS lv, count(*) AS c "
            "ORDER BY c DESC", s=INSERT_SOURCE)}
    by_conf = {
        r["cf"]: r["c"] for r in session.run(
            "MATCH ()-[r]->() WHERE r.source = $s "
            "RETURN coalesce(r.confidence,'<无>') AS cf, count(*) AS c "
            "ORDER BY c DESC", s=INSERT_SOURCE)}
    total_edges = session.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
    total_nodes = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
    return {"created_by_label": created, "edges": edges,
            "by_level": by_level, "by_confidence": by_conf,
            "total_nodes": total_nodes, "total_edges": total_edges}


def print_stats(st: dict) -> None:
    log("")
    log("── 图现状 ────────────────────────────────────────────")
    log(f"  节点合计 {st['total_nodes']:>9,}   边合计 {st['total_edges']:>9,}")
    if st["created_by_label"]:
        log("  本脚本写入的节点（source=insert）：")
        for lbl, c in st["created_by_label"].items():
            log(f"    {lbl:<14}{c:>7,}")
    else:
        log("  本脚本还没有写入任何节点")
    if st["edges"]:
        log("  本脚本写入的边：")
        for t, c in st["edges"].items():
            log(f"    {t:<22}{c:>7,}")
        log("  按对齐级别：")
        for lv, c in st["by_level"].items():
            log(f"    {lv:<20}{c:>7,}")
        log("  按置信档：")
        for cf, c in st["by_confidence"].items():
            log(f"    {cf:<20}{c:>7,}")


def print_measured_quality(method: str) -> None:
    """把**实测**的第二层分数打出来。图里有假阳性，这件事必须写在脸上。"""
    p = REPO_ROOT / "data" / "extracted" / method / "score_edges.json"
    if not p.exists():
        log(f"  （没找到 {p.relative_to(REPO_ROOT)}，无法引用实测质量）")
        return
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        l2 = d["layer2_edges"]["rule_plus_llm"]
        f = l2.get("flagged") or l2
    except Exception:
        return
    log("")
    log("  ⚠️ **这批边的实测质量**（11 篇金标准，第二层 `(药,节点)` 口径）：")
    log(f"     P {f['p']:.3f}  R {f['r']:.3f}  F1 {f['f1']:.3f}"
        f"（方法 {method}，已剔除 generic_only）")
    log("     也就是说**图里这批边约有 "
        f"{(1 - f['p']) * 100:.0f}% 是假阳性**。它们带着 align_level / confidence，"
        "就是给下游按可信度过滤用的——")
    log("     **不要把这批边当成 ground truth。**")


def acceptance(session) -> bool:
    """L4 验收（基线 §13.4）：能查到「某药 → 某人群禁用」。

    ⚠️ 基线 §13.4 举的例子是「华法林 → 孕妇禁用」，但**华法林不在 L3 选出的
    36 种药里**（它没进说明书采集样本）。所以这里用样本内等价的查询：
    ``来氟米特 → 孕妇``（金标准里确有这条边）与 ``他克莫司 → 大环内酯类药物过敏者``。
    """
    log("")
    log("── 验收查询（基线 §13.4：查得到「某药 → 某人群禁用」）──")
    ok = True
    # 第一条刻意挑**高置信**的：验收要能一眼看懂，不能拿一条证据可疑的先上
    checks = [("他克莫司", "大环内酯类药物过敏者", POPULATION_LABEL),
              ("伤寒疫苗", "孕妇", POPULATION_LABEL),
              ("阿那格雷", "肝损伤", "Disease")]
    for drug, node, label in checks:
        rows = list(session.run(
            f"MATCH (d:`Drug` {{name: $d}})-[r:`CONTRAINDICATED_FOR`]->"
            f"(t:`{label}` {{name: $n}}) "
            f"RETURN r.confidence AS cf, r.align_level AS lv, r.evidence AS ev, "
            f"       r.n_evidence AS ne",
            d=drug, n=node))
        if rows:
            for rec in rows:
                log(f"  ✓ {drug} → {node}（{label}）"
                    f"  置信 {rec['cf']} / {rec['lv']}"
                    + (f" / 另有 {rec['ne'] - 1} 条证据" if rec["ne"] > 1 else ""))
                log(f"      证据：{rec['ev'][:64]}")
        else:
            log(f"  ✗ 查不到 {drug} → {node}（{label}）")
            ok = False

    rows = list(session.run(
        "MATCH (t:`Population` {name: $n})<-[r:`CONTRAINDICATED_FOR`]-(d:`Drug`) "
        "WHERE r.source = $s RETURN d.name AS drug, r.confidence AS cf "
        "ORDER BY d.name", n="孕妇", s=INSERT_SOURCE))
    log(f"  ★ 反向查「孕妇」被哪些药标为禁忌：{len(rows)} 种药（这就是 L6 要用的形态）")
    for rec in rows:
        log(f"      {rec['drug']}（{rec['cf']}）")
    return ok


# ---------------------------------------------------------------- 入口

def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="把 L4 抽出的禁忌边写入 Neo4j")
    ap.add_argument("--method", default="section-json", help="用哪个方法的产物")
    ap.add_argument("--items-dir", default=None,
                    help="结构化条目目录，默认 data/extracted/<method>/items")
    ap.add_argument("--sections", default="禁忌",
                    help="要入库的小节（逗号分隔）。只有【禁忌】有金标准")
    ap.add_argument("--no-llm", action="store_true",
                    help="不要 ⑤ LLM 兜底（只写规则判出来的边）")
    ap.add_argument("--keep-generic-only", action="store_true",
                    help="不剔除 generic_only 的条目（默认剔除，理由见 §11）")
    ap.add_argument("--dry-run", action="store_true", help="只报告要写什么，不写库")
    ap.add_argument("--stats-only", action="store_true", help="只打印现状与验收查询")
    ap.add_argument("--reset", action="store_true",
                    help="先删掉上次由本脚本写入的边与孤立节点")
    args = ap.parse_args(argv)

    base = REPO_ROOT / "data" / "extracted" / args.method
    items_dir = Path(args.items_dir) if args.items_dir else base / "items"
    if not items_dir.is_absolute():
        items_dir = (REPO_ROOT / items_dir).resolve()
    if not items_dir.exists():
        raise SystemExit(f"✗ 找不到条目目录 {items_dir}")
    sections = [s.strip() for s in args.sections.split(",") if s.strip()]

    log("L4 第③步 · 说明书抽取的边入图")
    log(f"  方法        {args.method}")
    log(f"  条目来源    {items_dir.relative_to(REPO_ROOT)}")
    log(f"  小节        {'、'.join(sections)}"
        f" → {'、'.join(SECTION_RELATION[s] for s in sections)}")
    log(f"  来源标记    {INSERT_SOURCE}"
        f"（L1 骨架是 {SKELETON_SOURCE}）")
    log("")

    t_start = time.time()
    driver = None
    try:
        driver, settings = connect()
        log(f"  Neo4j       {settings.neo4j_uri}")
        log("")

        with driver.session() as session:
            if args.reset:
                log("── 清理上次导入（只删 source=insert）──")
                info = reset(session)
                log(f"  删除边 {info['rels_deleted']:,} 条，"
                    f"节点 {info['nodes_deleted']:,} 个")
                log("")

            if args.stats_only:
                print_stats(collect_stats(session))
                print_measured_quality(args.method)
                acceptance(session)
                return 0

            # ── 抽取 + 对齐（与评分脚本**同一段代码**）──────────────
            al = Aligner()
            cache = {} if args.no_llm else load_cache()
            drugs = sorted(p.stem for p in TEXT_DIR.glob("*.txt"))
            log(f"── 抽取与对齐（{len(drugs)} 药）──")
            records: list[dict] = []
            for sec in sections:
                records += extract_edges(drugs, base=base, items_dir=items_dir,
                                         al=al, section=sec,
                                         use_llm=not args.no_llm, cache=cache)
            log(f"  原子记录    {len(records):,}")
            log("  按判定级别：")
            for lv, c in Counter(r["level"] for r in records).most_common():
                log(f"    {lv:<20}{c:>7,}")

            kept = records
            if not args.keep_generic_only:
                kept = [r for r in records if r["generic_only"] is not True]
                log(f"  剔除 generic_only（模型自标）  "
                    f"{len(records) - len(kept)} 条原子记录")
            unknown = sum(1 for r in kept if r["generic_only"] is None)
            if unknown:
                log(f"  ⚠️ 其中 {unknown} 条**没有** generic_only 标记"
                    f"（自由文本产物）——它们不会被剔除")

            # ⑤ 想建"人群"但不在受控词表里的那些：**点名报出来**。
            # 受控词表是封闭的（决策 #33），不收就会把 `体内有心脏起搏器`、
            # `单胺氧化酶抑制剂使用期` 这类状态/用药情形塞进人群类别。
            oov_names = sorted({r["node"] for r in kept
                                if r["out_of_vocab_population"]})
            if oov_names:
                log(f"  ⚠️ ⑤ 给出 {len(oov_names)} 个**不在受控人群词表**里的"
                    f"「人群」，已按疾病处理：")
                for n in oov_names[:8]:
                    log(f"      {n}")
                if len(oov_names) > 8:
                    log(f"      …… 另有 {len(oov_names) - 8} 个")

            #: 级别 → 排序权重：同一条边有多条证据时，取**最可信**的那个级别
            rank = {"①精确": 0, "②包含": 1, "③前后缀": 2, "⑤LLM": 3, "④新建": 4}
            edges = []
            for (drug, _node_key), recs in sorted(group_edges(kept).items()):
                node_type = recs[0]["node_type"]
                # 证据取**最可信那一条**的原文，不是"排序第一"的原文——
                # 踩过：`来氟米特 → 孕妇` 原本显示的证据是 `必须排除妊娠`（一句处置），
                # 而真正的 `孕妇` 排在同一批证据后面，看的人会以为这条边是错的。
                best = min(range(len(recs)),
                           key=lambda i: (rank.get(recs[i]["level"], 9), i))
                edges.append({
                    "drug": drug,
                    "node": recs[0]["node"],
                    "label": POPULATION_LABEL if node_type == "人群" else "Disease",
                    "section": recs[0]["section"],
                    "method": args.method,
                    "align_level": recs[best]["level"],
                    "confidence": recs[best]["confidence"],
                    "evidence": recs[best]["atom"],
                    "n_more_evidence": len(recs) - 1,
                    "evidence_all": sorted({r["atom"] for r in recs}),
                })
            log(f"  归并成边    {len(edges):,} 条")
            log("  按节点类型：")
            for lbl, c in Counter(e["label"] for e in edges).most_common():
                log(f"    {lbl:<20}{c:>7,}")
            log("")

            # ⚠️ **缓存必须在这里就落盘**，不能等到写完边再存：
            #    `--dry-run` 会提前 return，那样 ⑤ 的结果就白花了钱——
            #    踩过：dry-run 之后重跑，154 条原子又调了一遍模型。
            if not args.no_llm:
                save_cache(cache)

            drug_names = sorted({e["drug"] for e in edges})
            resolved = resolve_drugs(session, drug_names)
            new_drugs = [k for k, v in resolved.items() if v["is_new"]]
            log("── 药名解析 ──")
            log(f"  涉及药        {len(resolved)}")
            log(f"  挂到已有节点   {len(resolved) - len(new_drugs)}")
            log(f"  新建物质级节点 {len(new_drugs)}："
                f"{'、'.join(new_drugs) or '（无）'}")
            for name in new_drugs:
                cands = resolved[name]["candidates"]
                if cands:
                    log(f"    {name} 的剂型分裂候选：{'、'.join(cands[:4])}"
                        f"{' …' if len(cands) > 4 else ''}")
            log("")

            targets: dict[str, list[str]] = {"疾病": [], "人群": []}
            for e in edges:
                targets["人群" if e["label"] == POPULATION_LABEL else "疾病"].append(
                    e["node"])

            if args.dry_run:
                log("── 目标节点（--dry-run，不写库）──")
                for typ, names in targets.items():
                    label = POPULATION_LABEL if typ == "人群" else "Disease"
                    uniq = sorted(set(names))
                    ex = count_existing(session, label, uniq)
                    log(f"  {typ:<4} 引用 {len(uniq):>3} 个：图里已有 {ex}，"
                        f"将新建 {len(uniq) - ex}")
                log("")
                log("  --dry-run：以上都没有写进图里。")
                return 0

            now = time.strftime("%Y-%m-%d %H:%M:%S")
            log("── 目标节点 ──")
            n_new = import_new_drug_nodes(session, resolved, now)
            if n_new:
                log(f"  新建 / 合并 Drug 节点 {n_new}")
            info = ensure_target_nodes(session, targets, now)
            for typ, d in info.items():
                log(f"  {typ:<4} 图里已有 {d['existing']:>3}，"
                    f"本次新建 {d['created']:>3}")
            log("")
            log("── 写边 ──")
            written = write_edges(session, edges, now)
            for rel, c in sorted(written.items()):
                log(f"  {rel:<22}{c:>7,}")
            log("")
            print_measured_quality(args.method)
            print_stats(collect_stats(session))
            ok = acceptance(session)
            log("")
            if ok:
                log(f"✓ L4 闭环：说明书文本 → 禁忌原子 → 图谱节点 → 可查询的边"
                    f"（{time.time() - t_start:.1f}s）")
            else:
                log("✗ 验收查询没全过，见上")
            return 0 if ok else 1
    except Exception as exc:  # noqa: BLE001
        log("")
        log(f"✗ 入图失败：{type(exc).__name__}: {exc}")
        log("  写入用的是 MERGE，修好问题后直接重跑即可，不必先 --reset。")
        return 1
    finally:
        if driver is not None:
            driver.close()


if __name__ == "__main__":
    raise SystemExit(main())
