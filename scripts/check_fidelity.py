#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L4 · 忠实度查证（基线 §13.5 的 L4 验收标准之一）
================================================

基线 §13.5 给 L4 定了两条验收标准：

============  ================================================
**完整性**    抽 10–20 篇，人工通读整篇说明书列出应抽条目，再与抽取结果比对
**忠实度**    抽 100 条，逐条回**说明书原文**查证"这条关系是不是原文写的"
============  ================================================

**完整性**由 `data/eval/` 的 11 篇人工标注金标准覆盖（§10）。
本脚本补上**忠实度**，而且它与"完整性"有一个关键区别：

> §13.5 自己写了：**忠实度是「查证题」而非医学判断题**——判断依据是
> **"原文有没有这么写"**，不是"医学上对不对"。

"原文有没有这么写"**可以自动查**：把抽出来的每条原子，拿回它所属那一节的原文里
做子串匹配。模型若是照抄，就应该是原文的子串；若是改写、拼接、或凭空补出来的，
就查不到。所以这一条**不需要医学背景，也不需要人工**。

两个严格度
----------
================  ==========================================================
严格              去空白后**逐字**子串匹配（只容忍换行/空格的排版差异）
宽松              再去掉全半角标点差异后匹配（容忍 ``(`` / ``（``、``,`` / `，`）
================  ==========================================================

**两个都报。** 只报严格会把"模型把 ``（`` 写成 ``(``" 也算成不忠实（那其实是排版）；
只报宽松又会把"模型只是把标点重排了一遍"混进"逐字照抄"里。

⚠️ 匹配范围是**该原子所属的那一节正文**（例如【禁忌】节），不是整篇说明书——
因为我们只把那一节送给模型，"整篇里别处有"不算这条抽取忠实。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from align_nodes import load_cache
from insert_io import TEXT_DIR, split_sections
from l4_pipeline import SECTION_RELATION, extract_edges, raw_atoms_of
from opencmkg_io import REPO_ROOT

#: 全半角标点的等价类。只统一**排版**差异，不动任何汉字。
PUNCT_EQUIV = {
    "（": "(", "）": ")", "，": ",", "；": ";", "。": ".", "：": ":",
    "！": "!", "？": "?", "【": "[", "】": "]", "、": ",", "“": '"',
    "”": '"', "‘": "'", "’": "'", "—": "-", "～": "~", "·": ".", "　": "",
}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def squash(text: str) -> str:
    """严格归一化：去掉一切空白。"""
    return re.sub(r"[\s\u3000]+", "", text or "")


def squash_punct(text: str) -> str:
    """宽松归一化：去空白 + 统一全半角标点的写法。"""
    s = squash(text)
    return "".join(PUNCT_EQUIV.get(ch, ch) for ch in s)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="L4 忠实度查证（基线 §13.5）")
    ap.add_argument("--method", default="section-json", help="用哪个方法的产物")
    ap.add_argument("--section", default="禁忌", help="查哪一节")
    ap.add_argument("--items-dir", default=None, help="结构化条目目录")
    ap.add_argument("--sample", type=int, default=100,
                    help="基线 §13.5 要求「抽 100 条」；0 = 全查")
    ap.add_argument("--scope", choices=("auto", "section", "whole"), default="auto",
                    help="查证范围：section = 只查送进去的那一节；"
                         "whole = 整篇。auto = 结构化产物查该节、自由文本产物查整篇")
    ap.add_argument("--show-fails", action="store_true", help="列出查不到的条目")
    args = ap.parse_args(argv)

    base = REPO_ROOT / "data" / "extracted" / args.method
    raw_items = Path(args.items_dir) if args.items_dir else base / "items"
    if not raw_items.is_absolute():
        raw_items = (REPO_ROOT / raw_items).resolve()
    # 忠实度是**任何**抽取产物的性质，所以自由文本产物（朴素基线）也要能查。
    # 没有 items/ 就按 raw/*.txt 的自由文本读——两种产物走同一段原子切分代码。
    if raw_items.exists():
        items_dir: Path | None = raw_items
    else:
        items_dir = None
        log(f"  注意：{args.method} 没有 items/ 目录，按自由文本产物（raw/*.txt）处理")

    # 原文按药切好。**查证范围必须等于"模型当时看到的输入"**：
    #   按小节送的那层只看到【禁忌】一节；朴素基线看到的是整篇。
    #   拿整篇去查小节产物会放过"从别的节抄来"的条目，反过来会冤枉基线。
    whole: dict[str, str] = {}
    sources: dict[str, str] = {}
    for p in sorted(TEXT_DIR.glob("*.txt")):
        text = p.read_text(encoding="utf-8")
        whole[p.stem] = text
        sources[p.stem] = split_sections(text).get(args.section, "")

    from align_nodes import Aligner  # 只为复用同一个"条目 → 原子"口径
    al = Aligner()
    drugs = sorted(sources)
    records = extract_edges(drugs, base=base / "raw", items_dir=items_dir, al=al,
                            section=args.section, use_llm=False)
    # 忠实度只问"抽出来的这条在不在原文里"，与对齐器判定无关，所以不用 ⑤。

    if args.scope == "whole" or (args.scope == "auto" and items_dir is None):
        sources, scope = whole, "整篇说明书"
    else:
        scope = f"【{args.section}】一节"

    log("L4 忠实度查证（基线 §13.5：逐条回说明书原文查证「原文有没有这么写」）")
    log(f"  方法        {args.method}    查证范围　{scope}")
    log(f"  条目来源    "
        + (str(items_dir.relative_to(REPO_ROOT)) if items_dir
           else str((base / "raw").relative_to(REPO_ROOT)) + "（自由文本）"))
    log(f"  原子总数    {len(records)}")
    log("")

    # 抽样：**按药轮转**，不是"排序后取前 N"。
    # 踩过：第一版直接 sorted()[:100]，样本全落在字母靠前的那几个药上
    # （三氧化二砷 / 乌帕达西替尼 / 他克莫司…），"抽 100 条"变成了"抽 4 个药"。
    # 轮转后 100 条能覆盖到全部 36 个药。顺序仍然确定，跑两次同一批。
    ordered = sorted({(r["drug"], r["atom"]) for r in records})
    if not args.sample:
        sample = ordered
    else:
        by_drug: dict[str, list[str]] = {}
        for drug, atom in ordered:
            by_drug.setdefault(drug, []).append(atom)
        sample, idx = [], 0
        while len(sample) < args.sample:
            added = False
            for drug in sorted(by_drug):
                if idx < len(by_drug[drug]):
                    sample.append((drug, by_drug[drug][idx]))
                    added = True
                    if len(sample) >= args.sample:
                        break
            if not added:
                break
            idx += 1
    n_drugs = len({d for d, _ in sample})
    log(f"  查证样本    {len(sample)} 条，覆盖 {n_drugs} 个药"
        + (f"（§13.5 要求 {args.sample} 条，全量 {len(ordered)} 条）"
           if args.sample else "（全量）"))
    log("")

    strict_ok = loose_ok = 0
    fails: list[tuple[str, str]] = []
    loose_only: list[tuple[str, str]] = []
    for drug, atom in sample:
        src = sources.get(drug, "")
        s, l = squash(src), squash_punct(src)
        a_strict, a_loose = squash(atom), squash_punct(atom)
        in_strict = bool(a_strict) and a_strict in s
        in_loose = bool(a_loose) and a_loose in l
        strict_ok += in_strict
        loose_ok += in_loose
        if not in_loose:
            fails.append((drug, atom))
        elif not in_strict:
            loose_only.append((drug, atom))

    n = len(sample)
    log("── 结果 ──")
    if not n:
        log("  ✗ 一条原子都没有——产物路径不对？")
        return 1
    log(f"  严格（逐字，只容忍空白差异）      {strict_ok}/{n} = "
        f"{strict_ok / n:.1%}")
    log(f"  宽松（再容忍全半角标点差异）      {loose_ok}/{n} = "
        f"{loose_ok / n:.1%}")
    log(f"  只有宽松能过的（就是标点写法不同） {len(loose_only)} 条")
    log(f"  **宽松也过不了的（改写/拼接/补写）** {len(fails)} 条 ← 这才是真问题")
    log("")
    if fails:
        log("  ── 查不到原文的 ──")
        for drug, atom in fails[:20]:
            log(f"    {drug:<10}{atom[:66]}")
        if len(fails) > 20:
            log(f"    …… 另有 {len(fails) - 20} 条")
        log("")
    if loose_only and args.show_fails:
        log("  ── 只差标点写法的 ──")
        for drug, atom in loose_only[:10]:
            log(f"    {drug:<10}{atom[:66]}")
        log("")

    payload = {
        "method": args.method,
        "section": args.section,
        "scope": scope,
        "criterion": "基线 §13.5 L4 忠实度：逐条回说明书原文查证「原文有没有这么写」",
        "n_atoms_total": len(ordered),
        "n_sample": n,
        "strict_only_whitespace": {"ok": strict_ok,
                                   "rate": round(strict_ok / n, 4) if n else None},
        "loose_punct_equiv": {"ok": loose_ok,
                              "rate": round(loose_ok / n, 4) if n else None},
        "punct_only_diff": len(loose_only),
        "not_found": [{"drug": d, "atom": a} for d, a in fails],
    }
    # 一个方法可能跑了好几节（如 CAUTION_FOR 的【注意事项】+【儿童用药】），
    # 这时**每节各写一份**——否则后跑的那节会把前一节的结果覆盖掉（踩过）。
    name = "fidelity.json" if args.section == "禁忌" else f"fidelity_{args.section}.json"
    out = base / name
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    log(f"  已写 {out.relative_to(REPO_ROOT)}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
