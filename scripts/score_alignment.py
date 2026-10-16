#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L4 端到端**两层**评分
=====================

为什么要有第二层
----------------
`score_extraction.py` 量的是**第一层：抽取**——说明书里那条禁忌有没有被抽出来。
基线在这一层是 **P = R = F1 = 1.000**（满分）。

但 L4 的交付物不是"一段文字"，是**图谱里的边**：
``CONTRAINDICATED_FOR : Drug → Disease/Population``。
"抽出来了"离"进图了"还差一步**对齐**：``卒中`` 要变成图谱里的某个节点。
所以必须再量**第二层：边**——``(药, 节点)`` 这个集合的 P / R / F1。

两层一起报，才能说清"错在哪一层"。第一层满分而第二层不满分，
就说明**瓶颈完全在对齐**，而不是在模型有没有读懂说明书。

评分口径
--------
============  ==================================================
第一层 原子    最小并列项集合（与 `score_extraction.py` 同一口径，直接复用它）
第二层 边      ``(药, 节点规范化名)`` 集合——同一药同一节点只算一条边
============  ==================================================

**预测从 ``raw/`` 的模型输出走完全程**（不是从金标准原子出发）：
切原子 → 逐原子对齐 → 收集边。这样第一层的错也会如实传到第二层，
不是"只评对齐器"的理想化数字。

一个刻意保留的严苛之处
----------------------
金标准里有 9 行 ``仅文本``（"对本品任何成份过敏"这类）和 23 行 ``删除``
（残片、限定语、理由），它们**明确不该进图**。对齐器目前**没有"不入图"这个判定**
（每个原子都会得到一个节点），所以这些原子会变成假阳性边。
这个惩罚是**应该**的——正是第二层要暴露的东西；脚本会把它们单独列出来。

用法
----
    python scripts/score_alignment.py                 # 只跑规则
    python scripts/score_alignment.py --llm           # 规则 + ⑤ LLM 兜底
    python scripts/score_alignment.py --show-edges    # 列出对上的边与多出来的边

产出
----
    data/extracted/<method>/score_edges.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from align_nodes import Aligner, canon, load_cache, load_gold_edges, save_cache
from insert_io import read_csv_rows
from l4_pipeline import extract_edges, group_edges
from l4_pipeline import raw_atoms_of as _raw_atoms_of
from opencmkg_io import REPO_ROOT
from score_extraction import atoms, cover, item_path, load_gold, load_output_items, match, norm

EXTRACTED_DIR = REPO_ROOT / "data" / "extracted"
NODE_FILE = REPO_ROOT / "data" / "eval" / "contraindications_nodes.csv"

#: 金标准里**不该进图**的行：``verdict=删除`` 或 ``node_type ∈ 仅文本``
NON_GRAPH_TYPES = ("仅文本", "-")


def log(msg: str = "") -> None:
    print(msg, flush=True)


def load_non_graph_atoms() -> dict[str, list[str]]:
    """按药收集金标准里"**不该进图**"的原子文本（用于归因假阳性边）。"""
    rows, _ = read_csv_rows(NODE_FILE)
    out: dict[str, list[str]] = {}
    for r in rows:
        typ = (r.get("node_type") or "").strip()
        verdict = (r.get("verdict") or "").strip()
        if verdict == "保留" and typ in ("疾病", "人群", "用药"):
            continue
        out.setdefault(r["drug"].strip(), []).append(norm(r.get("atom") or ""))
    return out


def load_graph_labels() -> dict[str, list[tuple[str, bool]]]:
    """把节点金标准读成 ``{药: [(原子归一化, 该不该进图)]}``。

    ``True`` = 金标准判它**该进图**，``False`` = 判它**不该进图**
    （``仅文本`` / ``删除``）。有了它才能评"该不该进图"这件事本身。
    """
    rows, _ = read_csv_rows(NODE_FILE)
    out: dict[str, list[tuple[str, bool]]] = {}
    for r in rows:
        typ = (r.get("node_type") or "").strip()
        verdict = (r.get("verdict") or "").strip()
        should = (verdict == "保留" and typ in ("疾病", "人群"))
        out.setdefault(r["drug"].strip(), []).append(
            (norm(r.get("atom") or ""), should))
    return out


def load_generic_only_atoms() -> dict[str, list[str]]:
    """只取 ``node_type = 仅文本`` 的行——**这才是 `generic_only` 的定义域**。

    ⚠️ 踩过：原先评 `generic_only` 标记时，把"所有不该进图的原子"都当成正类，
    也就是把 ``删除`` 那 23 行（残片 / 举例 / 理由句 / 后果句）也算进去。
    但 `generic_only` 的定义是**「通用过敏禁忌」**——金标准里对应的是 ``仅文本``，
    **不是"任何不该进图的东西"**。

    这个口径问题在 2026-10-16 移出那 2 行合并用药禁忌时**暴露出来**：
    它们从 `保留/用药` 变成 `删除/-`，于是被算成 `generic_only` 的漏标，
    把标记的召回从 0.889 压到 0.727——**而它们根本不是过敏条目**。
    按定义收紧后，`generic_only` 只对 ``仅文本`` 负责。
    """
    rows, _ = read_csv_rows(NODE_FILE)
    out: dict[str, list[str]] = {}
    for r in rows:
        if (r.get("node_type") or "").strip() != "仅文本":
            continue
        out.setdefault(r["drug"].strip(), []).append(norm(r.get("atom") or ""))
    return out


def raw_atoms_of(item: str) -> list[str]:
    """把一行输出切成原子，**保留原文措辞**。

    真正的实现在 :mod:`l4_pipeline`（入库脚本要用同一段，避免"入库的边不是评分的边"）。
    """
    return _raw_atoms_of(item)


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="L4 端到端两层评分")
    ap.add_argument("--method", default="baseline", help="方法名（决定输出目录）")
    ap.add_argument("--llm", action="store_true", help="第二层再量一遍「规则 + ⑤ LLM」")
    ap.add_argument("--items-dir", default=None,
                    help="结构化产物的目录（[{text,generic_only}] 的 JSON）；"
                         "给了就读它，不给就读 <method>/raw 下的自由文本")
    ap.add_argument("--show-edges", action="store_true", help="列出对上的边与多出来的边")
    args = ap.parse_args(argv)

    indir = EXTRACTED_DIR / args.method / "raw"
    if not indir.exists():
        raise SystemExit(f"✗ 找不到输出目录 {indir}")
    items_dir = None
    if args.items_dir:
        items_dir = Path(args.items_dir)
        if not items_dir.is_absolute():
            items_dir = (REPO_ROOT / items_dir).resolve()
        if not items_dir.exists():
            raise SystemExit(f"✗ 找不到条目目录 {items_dir}")

    al = Aligner()
    cache = load_cache() if args.llm else {}

    gold = load_gold()
    gold_edges = load_gold_edges()
    non_graph = load_non_graph_atoms()
    graph_labels = load_graph_labels()
    generic_only = load_generic_only_atoms()

    gold_set = {(r["drug"].strip(), canon(r["node"])) for r in gold_edges}
    gold_drugs = {r["drug"].strip() for r in gold_edges}

    log(f"L4 端到端两层评分 · {args.method}")
    log(f"  方法输出    {indir.relative_to(REPO_ROOT)}")
    if items_dir:
        log(f"  条目来源    {items_dir.relative_to(REPO_ROOT)}（结构化：带 generic_only）")
    log(f"  金标准边    {len(gold_edges)} 行 → {len(gold_set)} 个不同的 (药,节点) 对"
        f"（{len(gold_drugs)} 药）")
    log(f"  第二层      {'规则 + ⑤ LLM' if args.llm else '只用规则'}"
        f"（人群变体 {len(al._pop_exact)} 种写法）")
    log("")

    # ── 第一层：原子（抽取） ─────────────────────────────────────────
    L1 = {"tp": 0, "fp": 0, "fn": 0}
    per_drug_atoms: dict[str, dict] = {}
    outputs: dict[str, list[dict]] = {}
    n_items = n_gen = n_gen_unknown = 0
    for drug in sorted(gold):
        g = gold[drug]
        base = items_dir if items_dir else indir
        src = (base / f"{drug}.json") if items_dir else item_path(base, drug)
        items = load_output_items(src)
        outputs[drug] = items
        n_items += len(items)
        n_gen += sum(1 for it in items if it["generic_only"] is True)
        n_gen_unknown += sum(1 for it in items if it["generic_only"] is None)
        if g["n_gold"] == 0:
            per_drug_atoms[drug] = {"kind": "零禁忌", "n_gold": 0,
                                    "n_out": len(items),
                                    "zero_ok": len(items) == 0}
            continue
        out_atoms = [a for it in items for a in atoms(it["text"])]
        tp, miss, extra = match(g["atoms"], out_atoms)
        fp, fn = len(out_atoms) - tp, len(g["atoms"]) - tp
        L1["tp"] += tp
        L1["fp"] += fp
        L1["fn"] += fn
        per_drug_atoms[drug] = {"kind": "有禁忌", "tp": tp, "fp": fp, "fn": fn,
                                "n_gold": len(g["atoms"]), "n_out": len(out_atoms)}
    l1_p = L1["tp"] / (L1["tp"] + L1["fp"]) if L1["tp"] + L1["fp"] else 0.0
    l1_r = L1["tp"] / (L1["tp"] + L1["fn"]) if L1["tp"] + L1["fn"] else 0.0
    l1_f1 = 2 * l1_p * l1_r / (l1_p + l1_r) if l1_p + l1_r else 0.0

    # ── 第二层：边 ───────────────────────────────────────────────────
    def is_nongraph(drug: str, atom: str) -> bool:
        """这个原子是不是金标准里**明确判定不该进图**的那一类。"""
        a = norm(atom)
        return any(cover(n, a) >= 0.85 or cover(a, n) >= 0.85
                   for n in non_graph.get(drug, []))

    def gold_should_graph(drug: str, atom: str) -> bool | None:
        """金标准判这个原子该不该进图；**没判过就返回 ``None``**（不猜）。

        多个候选都能对上时取**匹配分最高**的那个；同分时按"该进图"算——
        这个方向对 ``generic_only`` 标记**更宽容**，免得把标记的分数往下压。
        """
        a = norm(atom)
        best: tuple[float, bool] | None = None
        for t, should in graph_labels.get(drug, []):
            s = max(cover(t, a), cover(a, t))
            if s < 0.85:
                continue
            if best is None or s > best[0] or (s == best[0] and should):
                best = (s, should)
        return best[1] if best else None

    def metrics(pred: dict) -> dict:
        tp = len(set(pred) & gold_set)
        fp = len(set(pred) - gold_set)
        fn = len(gold_set - set(pred))
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * p * r / (p + r) if p + r else 0.0
        return {"tp": tp, "fp": fp, "fn": fn, "p": p, "r": r, "f1": f1}

    def edges(use_llm: bool) -> dict:
        # **入库脚本调的是同一个 extract_edges**——所以报告里的 F1
        # 管得住图里那批边，不会出现"评分一套、入库另一套"。
        records = extract_edges(sorted(gold), base=indir, items_dir=items_dir,
                                al=al, use_llm=use_llm, cache=cache)
        grouped = group_edges(records)
        sources = {k: [r["atom"] for r in v] for k, v in grouped.items()}
        types = {k: v[0]["node_type"] for k, v in grouped.items()}
        flags: dict[str, bool | None] = {}          # 原子原文 -> generic_only
        lv: dict[str, int] = {}
        for rec in records:
            flags[rec["atom"]] = rec["generic_only"]
            lv[rec["level"]] = lv.get(rec["level"], 0) + 1
        base = metrics(sources)
        base.update({"sources": sources, "types": types, "levels": lv})
        # (a) **真实过滤**：只用模型自己标的 generic_only，不看金标准。
        #     ``generic_only is not True`` 保证"没有标记"（基线自由文本）**不会被滤掉**
        #     ——不许把"没这个字段"当成"模型说它不必进图"。
        base["flagged"] = metrics(
            {k: v for k, v in sources.items()
             if any(flags.get(a) is not True for a in v)})
        # (b) 诊断用**上界**：假设"该不该进图"已经判对，只看**对齐**本身还剩多少错。
        kept = {k: v for k, v in sources.items()
                if any(not is_nongraph(k[0], a) for a in v)}
        base["oracle"] = metrics(kept)
        # 假阳性边里，有多少来自"金标准明确说不该进图"的原子
        base["fp_from_nongraph"] = [
            (k, v[0]) for k, v in sorted(sources.items())
            if k not in gold_set and is_nongraph(k[0], v[0])]
        return base

    res_rule = edges(False)
    res_llm = edges(True) if args.llm else None

    log("── 第一层：原子（抽取） ──")
    log(f"  TP {L1['tp']}  FP {L1['fp']}  FN {L1['fn']}"
        f"   P {l1_p:.3f}  R {l1_r:.3f}  F1 {l1_f1:.3f}")
    if n_gen_unknown:
        log(f"  ⚠️ {n_gen_unknown}/{n_items} 条没有 generic_only 标记（自由文本产物）"
            f"——下面「剔除 generic_only」那一栏对它们不生效")
    log("")
    log("── 第二层：边（对齐 → (药,节点) 集合） ──")
    log(f"  金标准 {len(gold_set)} 对边")
    rows = [("① 规则单独", res_rule)]
    if res_llm:
        rows.append(("② 规则 + ⑤ LLM", res_llm))
    for name, r in rows:
        log(f"  {name:<14} 预测 {len(r['sources']):>3} 对"
            f"   TP {r['tp']:>3}  FP {r['fp']:>3}  FN {r['fn']:>3}"
            f"   P {r['p']:.3f}  R {r['r']:.3f}  F1 {r['f1']:.3f}")
    log("")
    if n_gen:
        log(f"  **剔除模型自标 generic_only** 之后"
            f"（模型自己判的，不是真值）：")
        for name, r in rows:
            f = r["flagged"]
            log(f"  {name:<14} 预测 {f['tp'] + f['fp']:>3} 对"
                f"   TP {f['tp']:>3}  FP {f['fp']:>3}  FN {f['fn']:>3}"
                f"   P {f['p']:.3f}  R {f['r']:.3f} F1 {f['f1']:.3f}")
        log("")
    log("  诊断上界：假设「该不该进图」已判对（用真值，只看对齐本身）：")
    for name, r in rows:
        o = r["oracle"]
        log(f"  {name:<14} 预测 {o['tp'] + o['fp']:>3} 对"
            f"   TP {o['tp']:>3}  FP {o['fp']:>3}  FN {o['fn']:>3}"
            f"   P {o['p']:.3f}  R {o['r']:.3f} F1 {o['f1']:.3f}")
    log("")

    # ── generic_only 标记本身准不准 ──────────────────────────────────
    # 正类 = 金标准的 **`仅文本`** 行（= 「通用过敏禁忌」，这才是 generic_only 的定义）。
    # `删除` 那些行（残片/举例/理由句/后果句/合并用药）**不参与**——
    # 它们也"不该进图"，但**不是过敏条目**，拿它们当正类等于换了概念（踩过）。
    f_tp = f_fp = f_fn = f_skip = 0
    f_fn_list: list[tuple[str, str]] = []
    f_fp_list: list[tuple[str, str]] = []
    f_fn_fragment = 0
    i_tp = i_fp = i_fn = i_skip = 0

    def gold_is_generic_only(drug: str, atom: str) -> bool | None:
        """金标准对**这个原子**判过"是不是通用过敏禁忌"吗。

        ``True`` = 是（``仅文本``）；``False`` = 不是（金标准判了它该进图）；
        ``None`` = 金标准没判过这个问题（``删除`` 的那几类）。
        """
        a = norm(atom)
        best: tuple[float, bool] | None = None
        for t, should in graph_labels.get(drug, []):
            s = max(cover(t, a), cover(a, t))
            if s < 0.85:
                continue
            if best is None or s > best[0] or (s == best[0] and should):
                best = (s, should)
        if best is None:
            return None
        if best[1]:
            return False            # 金标准说该进图 → 它不是通用过敏禁忌
        for t in generic_only.get(drug, []):
            if cover(t, a) >= 0.85 or cover(a, t) >= 0.85:
                return True         # 仅文本 → 是通用过敏禁忌
        return None                 # 不该进图、但属于删除的那几类 → 不评

    for drug in sorted(gold):
        for it in outputs[drug]:
            atoms_here = raw_atoms_of(it["text"])
            labels = [gold_is_generic_only(drug, a) for a in atoms_here]
            said_generic = (it["generic_only"] is True)
            for a, is_generic in zip(atoms_here, labels):
                if is_generic is None:
                    f_skip += 1
                    continue
                if said_generic and is_generic:
                    f_tp += 1
                elif said_generic and not is_generic:
                    f_fp += 1
                    f_fp_list.append((drug, a))
                elif not said_generic and is_generic:
                    f_fn += 1
                    f_fn_list.append((drug, a))
                    if len(atoms_here) > 1:
                        f_fn_fragment += 1
            # **条目级**口径：与标记的粒度一致。金标准判过的那些原子里
            # 只要有**一条**是通用过敏禁忌，这一整条就该被标 generic_only。
            judged = [s for s in labels if s is not None]
            if not judged:
                i_skip += 1
                continue
            is_generic_item = any(judged)
            if said_generic and is_generic_item:
                i_tp += 1
            elif said_generic and not is_generic_item:
                i_fp += 1
            elif not said_generic and is_generic_item:
                i_fn += 1
    fP = f_tp / (f_tp + f_fp) if f_tp + f_fp else 0.0
    fR = f_tp / (f_tp + f_fn) if f_tp + f_fn else 0.0
    fF = 2 * fP * fR / (fP + fR) if fP + fR else 0.0
    iP = i_tp / (i_tp + i_fp) if i_tp + i_fp else 0.0
    iR = i_tp / (i_tp + i_fn) if i_tp + i_fn else 0.0
    iF = 2 * iP * iR / (iP + iR) if iP + iR else 0.0
    if not n_gen_unknown and (f_tp + f_fp + f_fn):
        log("── generic_only 标记本身准不准 ──")
        log(f"  原子级（与边级口径一致）：TP {f_tp}  FP {f_fp}  FN {f_fn}"
            f"   P {fP:.3f}  R {fR:.3f}  F1 {fF:.3f}")
        log(f"  条目级（与标记粒度一致）：TP {i_tp}  FP {i_fp}  FN {i_fn}"
            f"   P {iP:.3f}  R {iR:.3f}  F1 {iF:.3f}")
        if f_fp:
            log(f"  ⚠️ 原子级有 {f_fp} 条**误标**（标记会把这些真边删掉，"
                f"比漏标更危险）")
        else:
            log("  ✓ 一条都没误标：标记只要说 true，金标准就同意")
        if f_fn_fragment:
            log(f"  ⚠️ 原子级的 {f_fn} 条 FN 里，有 {f_fn_fragment} 条只是"
                f"**条目被 、，； 切出来的残片**——")
            log(f"     标记是**条目级**的，拿原子级评它必然低估。条目级才是它自己的口径。")
        if f_fn_list:
            log("")
            log("  原子级漏标的那些（模型说「指名了具体东西」，金标准说「不入图」）：")
            for d, a in f_fn_list[:12]:
                log(f"    {d:<10}{a[:52]}")
            if len(f_fn_list) > 12:
                log(f"    …… 另有 {len(f_fn_list) - 12} 条")
        if f_fp_list:
            log("")
            log("  误标的那些（标记会把这些**真边**删掉）：")
            for d, a in f_fp_list[:12]:
                log(f"    {d:<10}{a[:52]}")
        log("")

    # ── 两层的"单位"对不对得上 ──────────────────────────────────────
    # 第一层按 、，； 切原子；节点金标准里出现过对 ``或`` 再切的情形。
    # 一个抽取原子若**同时覆盖两条入图边**，那两条边就不可能都被单独预测出来——
    # 这是第二层召回率的**结构性上限**，必须单独说清楚，不能算在对齐器头上。
    shared: list[tuple[str, str, list[str]]] = []
    for drug in sorted(gold_drugs):
        # ⚠️ ``gold_set`` 是 set，**必须 sorted**——否则同一条记录里边的先后
        # 每跑一次都不一样（踩过：JSON 两次跑出来 SHA-256 不同）。
        gs = sorted(k[1] for k in gold_set if k[0] == drug)
        for it in outputs[drug]:
            for a in raw_atoms_of(it["text"]):
                hit = [g for g in gs if cover(g, norm(a)) >= 0.85]
                if len(hit) >= 2:
                    shared.append((drug, a, hit))
    unreachable = sum(len(h) - 1 for _d, _e, h in shared)

    log("── 两层单位对齐情况 ──")
    if shared:
        log(f"  ⚠️ 有 {len(shared)} 个抽取原子同时覆盖了多条入图边"
            f"（{unreachable} 条边因此**不可能**被单独预测出来）：")
        for drug, e, hit in shared:
            log(f"     {drug}：{e[:26]}  →  {' / '.join(hit)}")
    else:
        log("  ✓ 每个抽取原子最多对应一条入图边，两层的单位是对齐的")
    log("")
    log("── 逐药（第二层） ──")
    log(f"  {'药':<10}{'金标准':>6}{'预测':>6}{'TP':>5}{'FP':>5}{'FN':>5}"
        f"   {'规则 F1':>8}  {'+LLM F1':>8}")
    per_drug_edges = []
    for drug in sorted(gold_drugs):
        gs = {k for k in gold_set if k[0] == drug}
        row = {"drug": drug, "n_gold": len(gs)}
        cells = []
        for tag, r in (("rule", res_rule), ("llm", res_llm)):
            if r is None:
                cells.append(None)
                continue
            ps = {k for k in r["sources"] if k[0] == drug}
            tp = len(ps & gs)
            fp, fn = len(ps - gs), len(gs - ps)
            p = tp / (tp + fp) if tp + fp else 0.0
            rr = tp / (tp + fn) if tp + fn else 0.0
            f1 = 2 * p * rr / (p + rr) if p + rr else 0.0
            row[tag] = {"tp": tp, "fp": fp, "fn": fn, "p": round(p, 4),
                        "r": round(rr, 4), "f1": round(f1, 4),
                        "n_pred": len(ps)}
            cells.append(f1)
        per_drug_edges.append(row)
        n_pred = row.get("rule", {}).get("n_pred", 0)
        log(f"  {drug:<10}{row['n_gold']:>6}{n_pred:>6}"
            f"{row['rule']['tp']:>5}{row['rule']['fp']:>5}{row['rule']['fn']:>5}"
            f"   {cells[0]:>8.3f}  "
            + (f"{cells[1]:>8.3f}" if cells[1] is not None else f"{'—':>8}"))

    # ── 多出来的边从哪来 ────────────────────────────────────────────
    log("")
    log("── 假阳性边的来源（第二层） ──")
    log(f"  规则：FP {res_rule['fp']} 条，其中 {len(res_rule['fp_from_nongraph'])} 条"
        f"抽自金标准**明确判为不入图**的原子（仅文本/删除）")
    if res_llm:
        log(f"  +LLM：FP {res_llm['fp']} 条，其中 {len(res_llm['fp_from_nongraph'])} 条"
            f"同上")
    log("  ⚠️ 对齐器没有「不入图」这个判定——**这是抽取层的活**（决策 #30）。"
        "本栏的数字说明：这个判定一旦补上，第二层能涨多少")

    if args.show_edges:
        log("")
        log("── 边明细（规则） ──")
        for key in sorted(gold_set & set(res_rule["sources"])):
            log(f"    ✓ {key[0]:<10}{key[1]}")
        log("  -- 多出来的 --")
        for key in sorted(set(res_rule["sources"]) - gold_set):
            log(f"    ✗ {key[0]:<10}{key[1]:<24}"
                f" ← {res_rule['sources'][key][0][:30]}")

    if args.llm:
        save_cache(cache)

    out_dir = EXTRACTED_DIR / args.method
    out_dir.mkdir(parents=True, exist_ok=True)
    keys = ("tp", "fp", "fn", "p", "r", "f1")
    def slim(r: dict | None) -> dict | None:
        if r is None:
            return None
        out = {k: (round(r[k], 4) if isinstance(r[k], float) else r[k])
               for k in keys}
        for tag in ("oracle", "flagged"):
            out[tag] = {k: (round(r[tag][k], 4)
                            if isinstance(r[tag][k], float)
                            else r[tag][k]) for k in keys}
        return out

    payload = {
        "method": args.method,
        "input": str(indir.relative_to(REPO_ROOT)),
        "items_dir": str(items_dir.relative_to(REPO_ROOT)) if items_dir else None,
        "node_gold_file": str(NODE_FILE.relative_to(REPO_ROOT)),
        "unit": {"layer1": "最小并列项（原子）", "layer2": "(药, 节点规范名) 集合"},
        "layer1_atoms": {"p": round(l1_p, 4), "r": round(l1_r, 4),
                         "f1": round(l1_f1, 4), **L1},
        "generic_only_flag": {
            "n_items": n_items, "n_true": n_gen, "n_unknown": n_gen_unknown,
            "vs_gold": (None if (n_gen_unknown or not (f_tp + f_fp + f_fn))
                        else {"atom_level": {
                                  "tp": f_tp, "fp": f_fp, "fn": f_fn,
                                  "p": round(fP, 4), "r": round(fR, 4),
                                  "f1": round(fF, 4), "skipped": f_skip,
                                  "fn_fragment": f_fn_fragment},
                              "item_level": {
                                  "tp": i_tp, "fp": i_fp, "fn": i_fn,
                                  "p": round(iP, 4), "r": round(iR, 4),
                                  "f1": round(iF, 4), "skipped": i_skip},
                              "fn_atoms": [{"drug": d, "atom": a}
                                           for d, a in f_fn_list],
                              "fp_atoms": [{"drug": d, "atom": a}
                                           for d, a in f_fp_list]}),
        },
        "layer2_edges": {
            "n_gold": len(gold_set),
            "rule_only": slim(res_rule),
            "rule_plus_llm": slim(res_llm),
            "rule_levels": res_rule["levels"],
            "llm_levels": res_llm["levels"] if res_llm else None,
            "fp_from_nongraph": {
                "rule": len(res_rule["fp_from_nongraph"]),
                "llm": len(res_llm["fp_from_nongraph"]) if res_llm else None},
            "unit_mismatch": {
                "shared_atoms": len(shared),
                "unreachable_edges": unreachable,
                "detail": [{"drug": d, "atom": e, "edges": h}
                           for d, e, h in shared]},
        },
        "per_drug": per_drug_edges,
        "per_drug_atoms": per_drug_atoms,
    }
    # ⚠️ 只有 ``--llm`` 那次才落盘。这份文件要**同时装两个数**，
    # 而 rule-only 跑出来 ``rule_plus_llm`` 是 ``null``——无条件覆盖会把上一行抹掉。
    # 踩过：提交前顺手跑了两次 rule-only，**提交进去的就是缺了 LLM 行的版本**。
    # 与 ``align_nodes.do_eval`` 用同一条规矩，不在两处各定一套。
    if args.llm:
        (out_dir / "score_edges.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log("")
        log(f"  已写 {(out_dir / 'score_edges.json').relative_to(REPO_ROOT)}")
    else:
        log("")
        log(f"  （只量了规则，没写 score_edges.json——那份文件要同时装两个数，"
            f"由 --llm 那次写全）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
