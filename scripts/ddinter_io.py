#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DDInter 读取层（L2 / L3 共用）
==============================

把「读 DDInter 8 个 CSV、按无序药对去重」这件事收敛到一处，
免得每个下游脚本各写一遍、各踩一遍同样的坑。

数据形态（已实测）
------------------
* 8 个分片文件 ``ddinter_downloads_code_{A,B,D,H,L,P,R,V}.csv``
* 列：``DDInterID_A, Drug_A, DDInterID_B, Drug_B, Level``
* **同一对药会跨分片重复出现**：222,383 条记录去重后只有 160,235 对
* 等级只有 Major / Moderate / Minor / Unknown 四种
* **没有机制说明，也没有处理建议**——这两样不在数据里（基线 §5 注）

两个容易踩的地方
----------------
1. **药对是无序的**。``(A,B)`` 与 ``(B,A)`` 是同一条相互作用，
   用 ``frozenset`` 去重，否则边数会翻倍。
2. **同一对药偶尔会带着不同的 Level 出现**（实测 0 例，但代码要能应付）。
   ``load_pairs`` 取**首次出现的**并在 ``conflicts`` 里报告，不静默丢弃。
"""

from __future__ import annotations

import csv
from pathlib import Path

from opencmkg_io import DDINTER_DIR, REPO_ROOT

#: 8 个分片文件的代号（官方按 ATC 分类首字母分片）
DDINTER_CODES = "ABDHLPRV"

#: 第 0 步产出的中英药名映射表
MAPPING_FILE = REPO_ROOT / "data" / "processed" / "drug_name_mapping.csv"

#: 由 DDInter 写入的节点/边的来源标记（基线 §13.8 要求每条链路带 source）
DDI_SOURCE = "ddinter"

#: 等级排序，数值越小越严重。同一对节点出现多个等级时取最严重的。
LEVEL_RANK = {"Major": 0, "Moderate": 1, "Minor": 2, "Unknown": 3}

#: 按严重程度从高到低
LEVELS_ORDERED = ("Major", "Moderate", "Minor", "Unknown")


def load_records() -> list[dict]:
    """读全部 8 个分片，返回原始记录（未去重）。

    需要「一个药出现在多少条相互作用里」这类**逐条**统计时用它；
    要边表本身请用 :func:`load_pairs`。
    """
    records: list[dict] = []
    for code in DDINTER_CODES:
        path = DDINTER_DIR / f"ddinter_downloads_code_{code}.csv"
        if not path.exists():
            raise FileNotFoundError(
                f"缺少分片 {path.name}——请先运行：python scripts/download_data.py"
            )
        with path.open(encoding="utf-8", newline="") as f:
            records.extend(csv.DictReader(f))
    return records


def load_pairs() -> tuple[dict[frozenset, str], int, list[str]]:
    """返回 ``({frozenset(药A, 药B): Level}, 原始记录数, 等级冲突描述)``。

    药对是**无序**的，用 ``frozenset`` 做键；同一对药重复出现只算一次。
    """
    records = load_records()
    pairs: dict[frozenset, str] = {}
    conflicts: list[str] = []
    for r in records:
        key = frozenset((r["Drug_A"], r["Drug_B"]))
        if key in pairs:
            if pairs[key] != r["Level"]:
                conflicts.append(
                    f"{r['Drug_A']} ↔ {r['Drug_B']}: {pairs[key]} vs {r['Level']}"
                )
            continue
        pairs[key] = r["Level"]
    return pairs, len(records), conflicts


def load_mapping() -> dict[str, dict]:
    """读第 0 步产出的中英药名映射表，键是 DDInter 的英文药名。"""
    return load_mapping_from(MAPPING_FILE)


def load_mapping_from(path: Path) -> dict[str, dict]:
    if not Path(path).exists():
        raise FileNotFoundError(
            f"找不到映射表 {path}——请先运行：python scripts/build_drug_name_mapping.py"
        )
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        return {r["en_name"]: r for r in csv.DictReader(f)}


def count_by_drug(pairs: dict[frozenset, str]) -> dict[str, dict[str, int]]:
    """统计每个药在各等级上出现了多少次。

    返回 ``{英文药名: {"Major": n, "Moderate": n, ..., "total": n}}``。
    **每条相互作用对两端的药各计一次**——这是"这个药有多少条相互作用"，
    不是"有多少对药"。
    """
    table: dict[str, dict[str, int]] = {}
    for key, level in pairs.items():
        for name in key:
            row = table.setdefault(
                name, {lv: 0 for lv in LEVELS_ORDERED} | {"total": 0}
            )
            row[level] = row.get(level, 0) + 1
            row["total"] += 1
    return table


if __name__ == "__main__":
    import sys

    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    print("DDInter 读取层 · 自检")
    print()
    pairs, n_records, conflicts = load_pairs()
    print(f"  原始记录    {n_records:,}")
    print(f"  去重后药对  {len(pairs):,}")
    print(f"  等级冲突    {len(conflicts)}")
    print()
    by_level: dict[str, int] = {}
    for lv in pairs.values():
        by_level[lv] = by_level.get(lv, 0) + 1
    print("  按等级：")
    for lv in LEVELS_ORDERED:
        if lv in by_level:
            print(f"    {lv:<10} {by_level[lv]:>8,}")
    print()
    table = count_by_drug(pairs)
    print(f"  涉及药物    {len(table):,}")
    top = sorted(table.items(), key=lambda kv: -kv[1]["Major"])[:5]
    print("  Major 级相互作用最多的 5 个药：")
    for name, c in top:
        print(f"    {name:<34} Major {c['Major']:>3}  共 {c['total']:>3}")
