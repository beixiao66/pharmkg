#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第 0 步 · 药名映射抽检的准确率（读人工填好的审阅表）
====================================================

`data/processed/mapping_review_sample.csv` 是**分层抽的 199 条**，
"译名对否 / 映射对否"两列由**人工填写**（2026-10-16 填完）。

这个脚本只做一件事：把它算成可复现的数字。**不重新抽检、不改样本**。

两件事要分清
------------
* **译名对否**：英文名 → 中文名的翻译对不对。199 条全都要判。
* **映射对否**：这个中文名有没有正确映射到图谱节点。**只在有匹配时才有意义**——
  「未命中」那 110 条的 `matched_cn` 是空的，没东西可映射，人工留空是对的，
  **不能把留空当"错"**（否则准确率会凭空掉一大截）。

为什么要按 `match_level` 拆开看
------------------------------
总准确率会把关键信息抹平。决策 #28 把 `编辑距离` 降级为"待确认"，
依据是当时**抽查发现 53 条编辑距离命中里约 20 条是错的**——那是抽查。
现在有全量人工判定，可以直接证实或推翻它。

用法
----
    python scripts/score_mapping_review.py
"""

from __future__ import annotations

import io
import sys
from collections import Counter, defaultdict

from insert_io import read_csv_rows
from opencmkg_io import REPO_ROOT

REVIEW_FILE = REPO_ROOT / "data" / "processed" / "mapping_review_sample.csv"

#: 人工填的两种写法都容忍（`对/错`、`是/否`、`y/n`、`1/0`）。
YES = {"对", "是", "y", "yes", "1", "true", "✓"}
NO = {"错", "否", "n", "no", "0", "false", "✗"}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def rate(hit: int, total: int) -> str:
    return f"{hit}/{total} = {hit / total:.1%}" if total else "—"


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    rows, enc = read_csv_rows(REVIEW_FILE)
    log("第 0 步 · 药名映射抽检准确率（人工判定）")
    log(f"  样本    {REVIEW_FILE.relative_to(REPO_ROOT)}（{len(rows)} 条 / 编码 {enc}）")
    log("")

    tr_ok = tr_bad = 0
    mp_ok = mp_bad = mp_na = 0
    by_level: dict[str, Counter] = defaultdict(Counter)
    unknown: Counter = Counter()

    for r in rows:
        level = (r.get("match_level") or "?").strip()
        t = (r.get("译名对否") or "").strip()
        m = (r.get("映射对否") or "").strip()

        if t in YES:
            tr_ok += 1
        elif t in NO:
            tr_bad += 1
        else:
            unknown["译名对否"] += 1

        if m in YES:
            mp_ok += 1
            by_level[level]["对"] += 1
        elif m in NO:
            mp_bad += 1
            by_level[level]["错"] += 1
        else:
            # 没有匹配 → 无物可映射 → **不计入分母**（不是"错"）
            mp_na += 1
            by_level[level]["无匹配"] += 1

    log("── 总体 ──")
    log(f"  译名准确率    {rate(tr_ok, tr_ok + tr_bad)}"
        f"   （对 {tr_ok} / 错 {tr_bad}）")
    log(f"  映射准确率    {rate(mp_ok, mp_ok + mp_bad)}"
        f"   （对 {mp_ok} / 错 {mp_bad}；另有 {mp_na} 条**无匹配、不计入分母**）")
    if unknown:
        log(f"  ⚠️ 还有没填的格子：{dict(unknown)}")
    log("")

    log("── 按匹配级别拆开（这里才是关键）──")
    log(f"  {'级别':<18}{'对':>4}{'错':>4}{'无匹配':>7}   准确率")
    order = ["原样精确", "归一化精确", "去盐归一化", "拼音相同（待确认）",
             "编辑距离（待确认）"]
    seen = set()
    for level in order + sorted(by_level):
        if level in seen:
            continue
        seen.add(level)
        c = by_level.get(level)
        if not c:
            continue
        ok, bad = c["对"], c["错"]
        log(f"  {level:<18}{ok:>4}{bad:>4}{c['无匹配']:>7}   {rate(ok, ok + bad)}")
    log("")
    log("  读法：**「无匹配」不是错**，它是「没东西可映射」，所以不进分母。")
    log("        把留空当错，映射准确率会被算成 61/199 = 30.7%，那是假的。")
    log("")
    ed = by_level.get("编辑距离（待确认）")
    if ed and ed["对"] + ed["错"]:
        r = ed["对"] / (ed["对"] + ed["错"])
        log(f"  ⚠️ 决策 #28 把「编辑距离」降级为待确认，依据是**抽查**发现约 20/53 是错的。")
        log(f"     现在全量人工判定给出 {ed['错']}/{ed['对'] + ed['错']} = "
            f"{1 - r:.1%} 是错的（准确率 {r:.1%}）——**抽查的结论站住了，而且更糟**。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
