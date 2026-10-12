#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L3 第 ① 步 · 高危药选样（数据驱动）
====================================

L3 要采集 30–50 份说明书去补「禁忌 / 特殊人群慎用」。**选哪些药不能拍脑袋**——
按基线 §6.4 ①，从 DDInter 里统计**出现在 Major 级相互作用里频次最高的 N 种药**。

为什么按 Major 级频次排
-----------------------
一条 Major 级相互作用意味着"两药同服有严重风险"。一个药挂着的 Major 条数越多，
它在真实用药场景里被踩雷的机会就越大——**这正是用药安全校验最该覆盖的药**。
这是纯数据驱动的选样，可复现、可解释，不掺主观判断。

用法
----
    python scripts/select_high_risk_drugs.py              # 默认取前 40
    python scripts/select_high_risk_drugs.py --top 50
    python scripts/select_high_risk_drugs.py --show 60    # 打印前 60 看看

产出
----
``data/processed/high_risk_drugs.csv`` —— 全部 1,939 个药的完整排名，
带 ``selected`` 列标记哪些进了名单（**保留全量，方便随时调整 N 而不用重跑**）。
"""

from __future__ import annotations

import argparse
import csv
import re
import sys

from build_drug_name_mapping import CONFIRMED_LEVELS
from ddinter_io import LEVELS_ORDERED, count_by_drug, load_mapping, load_pairs
from opencmkg_io import REPO_ROOT

OUT_FILE = REPO_ROOT / "data" / "processed" / "high_risk_drugs.csv"

#: 去掉"（外用）""(topical)"这类括号补充——NMPA 上按通用名检索
PAREN = re.compile(r"[（(][^）)]*[）)]")


def log(msg: str = "") -> None:
    print(msg, flush=True)


def nmpa_keyword(cn_name: str) -> str:
    """把译名收拾成适合在 NMPA 检索的通用名。"""
    return PAREN.sub("", cn_name).strip()


def build_ranking(top: int) -> tuple[list[dict], dict]:
    """按 Major 级相互作用条数排名，并把同一个中文通用名的药合并。

    为什么要合并：DDInter 里 ``Insulin human`` 与 ``Insulin human (isophane)``
    这类条目会译成同一个中文名，它们是同一种药的不同剂型，
    分开统计会把一个药拆成好几行、排名虚高。
    """
    pairs, n_records, _ = load_pairs()
    per_drug = count_by_drug(pairs)
    mapping = load_mapping()

    merged: dict[str, dict] = {}
    unmapped_cn = 0
    for en, counts in per_drug.items():
        row = mapping.get(en)
        if row is None or not row["cn_name"]:
            # 连译文都没有（实测只有 1 个）——用英文名兜底，仍然算进去
            cn = en
            level = "无译文"
            node = en
            unmapped_cn += 1
        else:
            cn = row["cn_name"]
            level = row["match_level"]
            node = row["matched_cn"] if level in CONFIRMED_LEVELS else cn

        key = nmpa_keyword(cn)
        slot = merged.setdefault(key, {
            "cn_name": cn, "nmpa_keyword": key, "en_names": [],
            "match_level": level, "graph_node": node,
            "node_source": "opencmkg" if level in CONFIRMED_LEVELS else "ddinter",
            **{lv: 0 for lv in LEVELS_ORDERED}, "total": 0,
        })
        slot["en_names"].append(en)
        for lv in LEVELS_ORDERED:
            slot[lv] += counts.get(lv, 0)
        slot["total"] += counts["total"]
        # 合并时优先记"确定命中"的来源，信息更多
        if level in CONFIRMED_LEVELS and slot["match_level"] not in CONFIRMED_LEVELS:
            slot["match_level"] = level
            slot["graph_node"] = node
            slot["node_source"] = "opencmkg"

    rows = sorted(
        merged.values(),
        key=lambda r: (-r["Major"], -r["total"], r["cn_name"]),
    )
    for i, r in enumerate(rows, 1):
        r["rank"] = i
        r["selected"] = 1 if i <= top else 0
    stats = {
        "n_records": n_records,
        "n_pairs": len(pairs),
        "n_drugs": len(per_drug),
        "n_merged": len(rows),
        "unmapped_cn": unmapped_cn,
    }
    return rows, stats


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="L3 第①步：按 Major 级频次选高危药")
    parser.add_argument("--top", type=int, default=40,
                        help="选前 N 个进名单（基线要求 30–50），默认 40")
    parser.add_argument("--show", type=int, default=40, help="打印前 N 行")
    args = parser.parse_args(argv)

    log("L3 第①步 · 高危药选样")
    log("")
    rows, stats = build_ranking(args.top)

    log("── 数据 ──")
    log(f"  原始记录      {stats['n_records']:,} 条")
    log(f"  去重后药对    {stats['n_pairs']:,} 对")
    log(f"  涉及药物      {stats['n_drugs']:,} 个（DDInter 英文名）")
    log(f"  合并同通用名后 {stats['n_merged']:,} 个")
    if stats["unmapped_cn"]:
        log(f"  无译文兜底    {stats['unmapped_cn']} 个")
    log("")

    log(f"── 按 Major 级相互作用条数排名（前 {args.show}）──")
    log(f"  {'#':>3}  {'中文名':<16}{'Major':>6}{'Moderate':>9}{'合计':>7}  "
        f"{'匹配级别':<14}{'入图节点'}")
    log("  " + "-" * 88)
    for r in rows[: args.show]:
        mark = "★" if r["selected"] else " "
        log(f" {mark}{r['rank']:>3}  {r['cn_name']:<16}{r['Major']:>6}"
            f"{r['Moderate']:>9}{r['total']:>7}  {r['match_level']:<14}"
            f"{r['graph_node']}")

    sel = [r for r in rows if r["selected"]]
    log("")
    log(f"── 入选名单（前 {len(sel)} 个）──")
    log("  " + "、".join(r["nmpa_keyword"] for r in sel))
    log("")
    major_sum = sum(r["Major"] for r in sel)
    total_major = sum(r["Major"] for r in rows)
    log(f"  名单覆盖 Major 级相互作用 {major_sum:,} / {total_major:,} "
        f"= {major_sum / total_major:.1%}（按药计数，一对药算两次）")
    log(f"  其中挂在已有骨架药上的 {sum(1 for r in sel if r['node_source'] == 'opencmkg')} 个，"
        f"挂在 ddinter 新节点上的 {sum(1 for r in sel if r['node_source'] == 'ddinter')} 个")

    fields = ["rank", "selected", "cn_name", "nmpa_keyword", "en_names",
              "match_level", "graph_node", "node_source",
              *LEVELS_ORDERED, "total"]
    with OUT_FILE.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({**r, "en_names": " / ".join(sorted(r["en_names"]))})

    log("")
    log("── 产出 ──")
    log(f"  {OUT_FILE.relative_to(REPO_ROOT)}   （全量 {len(rows):,} 行，"
        f"selected=1 的进名单）")
    log("")
    log("下一步：L3 第②步，按 nmpa_keyword 去 NMPA 采集说明书（限速、记录 URL 与日期）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
