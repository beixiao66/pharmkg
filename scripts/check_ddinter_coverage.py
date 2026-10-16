#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Q4 核查：DDInter 到底覆不覆盖那 2 条「合并用药禁忌」

背景（实测报告 §10.5(3) / 基线 §5 待定说明）：L4 节点金标准里有 2 行被标成
``node_type = 用药``，而 §5 的 `CONTRAINDICATED_FOR` 只允许 Disease/Population：

    奥希替尼 → 圣约翰草          （原文："本品不得与圣约翰草一起服用"）
    奥扎莫德 → 单胺氧化酶抑制剂    （原文："正在使用单胺氧化酶（MAO）抑制剂"）

**要不要把它们移出 L4 的金标准，取决于 DDInter 里有没有这两对**——
如果 DDInter 已经覆盖，那它们本来就该由 `INTERACTS_WITH` 承载，L4 不必重复记账；
如果没有，移出去就是**丢真实信息**。

这个脚本只做一件事：**查**。不改任何数据。

用法
----
    python scripts/check_ddinter_coverage.py
"""

from __future__ import annotations

import sys

from ddinter_io import load_mapping, load_pairs

#: 说明书里出现的「药」名 → 在 DDInter 里可能叫什么（英文/别名，全都小写匹配子串）
TARGETS = {
    "圣约翰草": ("john", "hyperic", "st. john", "wort"),
    "单胺氧化酶抑制剂": ("monoamine", "maoi", "mao "),
}

#: 说明书里的药 → DDInter 里的英文名（用第 0 步那份映射表反查）
DRUGS = {"奥希替尼": "osimertinib", "奥扎莫德": "ozanimod"}

#: 「单胺氧化酶抑制剂」是**药物类别**，而 DDInter 只收**单个药**。
#: 所以类别级的那条金标准边，要落到类别里的**个体药**上再查一次。
#: ⚠️ 这份名单是**手工列举的公开药理学常识**，不是从评测集反推的。
MAO_INHIBITORS = (
    "selegiline", "rasagiline", "moclobemide", "phenelzine",
    "tranylcypromine", "isocarboxazid", "linezolid", "procarbazine",
    "furazolidone", "methylene blue",
)


def log(msg: str = "") -> None:
    print(msg, flush=True)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    log("Q4 核查：DDInter 覆不覆盖那 2 条合并用药禁忌")
    log("")

    mapping = load_mapping()
    pairs, n_records, _conflicts = load_pairs()
    log(f"  DDInter 记录 {n_records:,} 条 → 去重药对 {len(pairs):,}")
    log(f"  中英药名映射表 {len(mapping):,} 条")
    log("")

    # DDInter 里所有药名 = 药对两端
    names = sorted({n for pair in pairs for n in pair})
    log(f"  DDInter 涉及药名 {len(names):,} 个")
    log("")

    log("── ① DDInter 里有没有「圣约翰草」/「单胺氧化酶抑制剂」这类条目 ──")
    hits: dict[str, list[str]] = {}
    for label, keys in TARGETS.items():
        found = [n for n in names if any(k in n.lower() for k in keys)]
        hits[label] = found
        log(f"  {label}：命中 {len(found)} 个")
        for n in found[:12]:
            log(f"      {n}")
        if len(found) > 12:
            log(f"      …… 另有 {len(found) - 12} 个")
    log("")

    log("── ② 映射表里这两个名字的中文写法（第 0 步那份表）──")
    for label, keys in TARGETS.items():
        found = [f"{en} → {row.get('cn_name','')}"
                 for en, row in mapping.items()
                 if any(k in en.lower() for k in keys)]
        log(f"  {label}：{len(found)} 条")
        for f in found[:10]:
            log(f"      {f}")
    log("")

    log("── ③ 奥希替尼 / 奥扎莫德 在 DDInter 里叫什么、各有多少条边 ──")
    drug_names: dict[str, list[str]] = {}
    for cn, en in DRUGS.items():
        found = [n for n in names if en in n.lower()]
        drug_names[cn] = found
        log(f"  {cn}（{en}）：命中 {len(found)} 个")
        for n in found[:6]:
            log(f"      {n}")
    log("")

    log("── ④ 关键问题：这两对边在 DDInter 里**存在吗** ──")
    for cn, found in drug_names.items():
        for other_cn, other_names in hits.items():
            hit_pairs = []
            for a in found:
                for b in other_names:
                    key = frozenset({a, b})
                    if key in pairs:
                        hit_pairs.append((a, b, pairs[key]))
            status = "✓ 有" if hit_pairs else "✗ 没有"
            log(f"  {cn} × {other_cn}：{status}"
                + (f"  {len(hit_pairs)} 条" if hit_pairs else ""))
            for a, b, lv in hit_pairs[:6]:
                log(f"      {a} ↔ {b}   {lv}")
    log("")
    log("── ⑤ 再挖一层：把「单胺氧化酶抑制剂」落到**个体药**上查 ──")
    log("     （DDInter 只收单个药，「单胺氧化酶抑制剂」是类别名，所以类别级那条边")
    log("       要问的是：类别里的**任何一个**个体药在 DDInter 里和奥扎莫德有边吗）")
    log("")
    individual: list[str] = []
    for kw in MAO_INHIBITORS:
        found = [n for n in names if kw in n.lower()]
        if found:
            individual.extend(found)
    individual = sorted(set(individual))
    log(f"   DDInter 里认得出的 MAO 抑制剂个体药 {len(individual)} 个：")
    for n in individual[:15]:
        log(f"       {n}")
    if len(individual) > 15:
        log(f"       …… 另有 {len(individual) - 15} 个")
    log("")
    for cn, found in drug_names.items():
        hit_pairs = []
        for a in found:
            for b in individual:
                key = frozenset({a, b})
                if key in pairs:
                    hit_pairs.append((a, b, pairs[key]))
        status = "✓ 有" if hit_pairs else "✗ 没有"
        log(f"  {cn} × MAO 抑制剂个体药：{status}"
            + (f"  {len(hit_pairs)} 条" if hit_pairs else ""))
        for a, b, lv in hit_pairs[:8]:
            log(f"      {a} ↔ {b}   {lv}")
    log("")
    log("  判读：")
    log("    · 若两对都「有」→ 它们本来就该由 INTERACTS_WITH 承载，")
    log("      L4 金标准里那 2 行可以移出（54 → 52 条边），不丢信息。")
    log("    · 若「没有」→ 移出去就是丢真实信息，应改 §5 给 Drug 留槽位。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
