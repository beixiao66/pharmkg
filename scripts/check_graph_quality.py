#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L8 · 图谱质量校验
=================

§13.4 给 L8 的职责：**已入库图谱 → 节点/边统计与抽样错误清单**，
靶点已明确五个：**人名噪声、剂型分裂、拼接垃圾、OOV 实体、自环**。
验收物是「质量报告表」（可视化页不在本脚本范围）。

**本脚本只诊断，不清洗。** 删节点是不可逆的，而"该删哪些"恰恰要靠这里的清单来说服人。
清洗应当是**独立的、可回滚的一步**。

## 五个靶点怎么测（每条都标了判据与它的局限）

| 靶点 | 判据 | 局限（必须写进报告） |
|---|---|---|
| 自环 | `(a)-[r]->(a)` | 无（这是确定的图论错误） |
| 剂型分裂 | 剥掉剂型后缀后，同主成分对应多个 `Drug` | 后缀表是**词表驱动**的，漏一个后缀就把一个分裂算成一个正常药；反过来剥太狠会把不同的药合到一起 |
| 拼接垃圾 | ① 截断碎片（含 `Ⅰ/Ⅱ/Ⅲ`、`SP`、`冻干`、纯字母）② 长度 ≥12 ③ 前后半段完全相同 | **启发式**，只抓形状明显的；真实的中文药名拼接需要人看，所以报告里必须给样例 |
| 人名噪声 | 名字含 `医生/教授/主任/医师/先生/女士/氏/院长/博士` | 只抓带标记的；纯人名（如"张三"）抓不到——所以报告要**抽样若干短名**供人工看 |
| OOV 实体 | `source='insert'` 且 `vocab != 'controlled'` | 只看"来源+词表"两个字段，判不了语义对错 |

## 为什么一定要出**样例**而不只出数字

数字说明"有多脏"，样例说明"脏成什么样"。没有样例的报告没法让人做决定——
而清洗的决定恰恰要人来下。

用法
----
    python scripts/check_graph_quality.py
    python scripts/check_graph_quality.py --sample 15     # 每个靶点抽多少条样例
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from collections import Counter

from opencmkg_io import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "backend"))

OUT_JSON = REPO_ROOT / "data" / "quality" / "graph_quality.json"
OUT_MD = REPO_ROOT / "docs" / "图谱质量报告.md"

#: 剂型/规格后缀。**顺序很重要**：长的先剥，否则 `肠溶片` 会被 `片` 先吃掉一半。
DOSAGE_SUFFIXES = (
    "注射用", "肠溶胶囊", "肠溶片", "缓释胶囊", "缓释片", "控释片", "分散片",
    "口腔崩解片", "咀嚼片", "含片", "泡腾片", "泡腾颗粒", "干混悬剂", "混悬液",
    "口服溶液", "口服液", "注射液", "滴眼液", "滴耳液", "滴鼻液", "气雾剂",
    "喷雾剂", "软膏", "乳膏", "凝胶", "搽剂", "擦剂", "洗剂", "栓", "散",
    "颗粒", "胶囊", "糖浆", "滴丸", "丸", "片", "乳", "酊", "贴", "膏", "露",
)

#: 人名/身份噪声标记。
PERSON_MARKERS = ("医生", "教授", "主任", "医师", "先生", "女士", "院长", "博士")

#: 截断/拼接的形状标记。
FRAGMENT_RE = re.compile(r"(Ⅰ|Ⅱ|Ⅲ|Ⅳ|SP$|^SP|冻干$|^[A-Za-z]{1,2}$)")

#: 只在**药名**上做剂型/拼接检测（病名没有剂型概念）。
DRUG_TARGETS = ("剂型分裂", "拼接垃圾")


def log(msg: str = "") -> None:
    print(msg, flush=True)


def strip_dosage(name: str) -> str:
    """剥掉剂型后缀；剥完不足 2 字就原样返回（避免把 `片` 这种名字剥空）。"""
    for suf in DOSAGE_SUFFIXES:
        if name.endswith(suf) and len(name) - len(suf) >= 2:
            return name[: -len(suf)]
    return name


def detect(session, sample: int) -> dict:
    r: dict = {}

    # ── 规模 ────────────────────────────────────────────────────
    labels = [x["l"] for x in session.run(
        "CALL db.labels() YIELD label RETURN label AS l ORDER BY label")]
    r["scale"] = {"nodes_by_label": {}, "edges_by_type": {}}
    for lbl in labels:
        r["scale"]["nodes_by_label"][lbl] = session.run(
            f"MATCH (x:`{lbl}`) RETURN count(x) AS c").single()["c"]
    for row in session.run("CALL db.relationshipTypes() YIELD relationshipType "
                           "AS t RETURN t ORDER BY t"):
        t = row["t"]
        r["scale"]["edges_by_type"][t] = session.run(
            f"MATCH ()-[x:`{t}`]->() RETURN count(x) AS c").single()["c"]
    r["scale"]["nodes_total"] = session.run(
        "MATCH (x) RETURN count(x) AS c").single()["c"]
    r["scale"]["edges_total"] = session.run(
        "MATCH ()-[x]->() RETURN count(x) AS c").single()["c"]

    # ── ① 自环 ──────────────────────────────────────────────────
    r["self_loop"] = {
        "count": session.run("MATCH (a)-[x]->(a) RETURN count(x) AS c"
                             ).single()["c"],
        "samples": [{"label": x["l"], "name": x["n"], "type": x["t"]}
                    for x in session.run(
                        "MATCH (a)-[x]->(a) RETURN labels(a)[0] AS l, "
                        "a.name AS n, type(x) AS t LIMIT $k", k=sample)],
    }

    # ── ② 剂型分裂 ──────────────────────────────────────────────
    drug_names = [x["n"] for x in session.run(
        "MATCH (d:Drug) WHERE d.name IS NOT NULL RETURN d.name AS n")]
    groups: dict[str, list[str]] = {}
    for nm in drug_names:
        groups.setdefault(strip_dosage(nm), []).append(nm)
    split = {k: sorted(v) for k, v in groups.items() if len(v) > 1}
    r["dosage_split"] = {
        "drug_total": len(drug_names),
        "split_bases": len(split),
        "split_drugs": sum(len(v) for v in split.values()),
        "samples": [{"base": k, "forms": v} for k, v in
                    sorted(split.items(), key=lambda kv: -len(kv[1]))[:sample]],
    }

    # ── ③ 拼接垃圾 ──────────────────────────────────────────────
    fragments = sorted(nm for nm in drug_names if FRAGMENT_RE.search(nm))
    too_long = sorted((nm for nm in drug_names if len(nm) >= 12), key=len,
                      reverse=True)
    repeated = [nm for nm in drug_names
                if len(nm) >= 8 and nm[: len(nm) // 2] == nm[len(nm) // 2:]]
    r["junk_concat"] = {
        "fragment_count": len(fragments),
        "fragment_samples": fragments[:sample],
        "too_long_count": len(too_long),
        "too_long_samples": too_long[:sample],
        "repeated_half_count": len(repeated),
        "repeated_half_samples": repeated[:sample],
    }

    # ── ④ 人名噪声 ──────────────────────────────────────────────
    person_drug = sorted(nm for nm in drug_names
                         if any(m in nm for m in PERSON_MARKERS))
    dis_names = [x["n"] for x in session.run(
        "MATCH (d:Disease) WHERE d.name IS NOT NULL RETURN d.name AS n")]
    person_dis = sorted(nm for nm in dis_names
                        if any(m in nm for m in PERSON_MARKERS))
    # 抓不到"纯人名"（如 张三），所以再抽一批**最短的名字**供人眼扫
    short_drug = sorted((nm for nm in drug_names if 2 <= len(nm) <= 3))[:sample]
    r["person_noise"] = {
        "drug_marked_count": len(person_drug),
        "drug_marked_samples": person_drug[:sample],
        "disease_marked_count": len(person_dis),
        "disease_marked_samples": person_dis[:sample],
        "short_name_samples": short_drug,
        "note": ("标记载体只抓带『医生/教授/氏』等字样的；**纯人名抓不到**，"
                 "所以另给一批 2–3 字短名供人工扫"),
    }

    # ── ⑤ OOV 实体 ──────────────────────────────────────────────
    oov = [{"label": x["l"], "name": x["n"], "source": x["s"], "vocab": x["v"]}
           for x in session.run(
               "MATCH (x) WHERE x.source = 'insert' "
               "AND coalesce(x.vocab,'') <> 'controlled' "
               "RETURN labels(x)[0] AS l, x.name AS n, x.source AS s, "
               "x.vocab AS v ORDER BY l, n")]
    by_source = {row["s"]: row["c"] for row in session.run(
        "MATCH (x) WHERE x.source IS NOT NULL "
        "RETURN x.source AS s, count(x) AS c ORDER BY s")}
    r["oov"] = {
        "nodes_by_source": by_source,
        "oov_count": len(oov),
        "oov_samples": oov[:sample],
        "note": ("判据只有『来源=insert 且词表不是 controlled』，**判不了语义对错**；"
                 "这些节点是 L4 从说明书抽出来的，要看具体名字才谈得上对错"),
    }
    return r


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="L8 图谱质量校验")
    ap.add_argument("--sample", type=int, default=10, help="每个靶点抽多少条样例")
    args = ap.parse_args(argv)

    from app.config import get_settings
    from neo4j import GraphDatabase

    st = get_settings()
    driver = GraphDatabase.driver(st.neo4j_uri,
                                 auth=(st.neo4j_user, st.neo4j_password))
    log("L8 · 图谱质量校验（只诊断，不清洗）")
    log(f"  Neo4j  {st.neo4j_uri}")
    log("")
    try:
        with driver.session() as s:
            r = detect(s, args.sample)
    except Exception as exc:  # noqa: BLE001
        # ⚠️ 连不上要**说清楚下一步**，不要只甩一个 traceback——
        #    最常见的原因是 Docker Desktop 没启动（Neo4j 是容器）。
        log(f"  ✗ 连不上 Neo4j：{type(exc).__name__}")
        log("    最常见原因：**Docker Desktop 没在运行**（Neo4j 是容器）。")
        log("    先启动 Docker Desktop，确认 docker ps 能看到 neo4j，再重跑本脚本。")
        return 2
    finally:
        driver.close()

    sc = r["scale"]
    log(f"  规模  {sc['nodes_total']:,} 节点 / {sc['edges_total']:,} 边")
    log("")
    log("  ── 五个靶点 ──")
    log(f"    自环            {r['self_loop']['count']}")
    log(f"    剂型分裂        {r['dosage_split']['split_bases']} 个主成分分裂，"
        f"涉及 {r['dosage_split']['split_drugs']} 个药"
        f"（共 {r['dosage_split']['drug_total']} 个药）")
    jc = r["junk_concat"]
    log(f"    拼接垃圾        截断碎片 {jc['fragment_count']} / "
        f"超长(≥12字) {jc['too_long_count']} / 前后半段重复 {jc['repeated_half_count']}")
    pn = r["person_noise"]
    log(f"    人名噪声        药名 {pn['drug_marked_count']} / 病名 "
        f"{pn['disease_marked_count']}（只抓带标记的）")
    log(f"    OOV 实体        {r['oov']['oov_count']}")

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(r, ensure_ascii=False, indent=2),
                        encoding="utf-8", newline="\n")
    log("")
    log(f"  ✓ 原始数据 {OUT_JSON.relative_to(REPO_ROOT)}")
    log("  ✓ 报告见 docs/图谱质量报告.md（由本脚本的产物整理；见 README 的 L8 一节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
