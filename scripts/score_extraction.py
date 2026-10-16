#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L4 抽取结果评分（P / R / F1）
==============================

对谁评分
--------
`data/eval/contraindications_gold.csv`：**11 篇人工标注**的【禁忌】金标准
（决策 #30 / Q6 之后定的评测集，标好即冻结）。

评分单位：「最小并列项」，不是「原文行」
------------------------------------------
**两边都按 、 ， ； 切成最小并列项（下称"原子"），再比原子集合。**

为什么不用"一条原文行"当单位——实测证据：基线在多塞平上把原文一行里塞的
10 个疾病**正确地拆成了 10 条**。若按原文行算，这 10 条里只有 1 条命中、
另外 9 条会被判成假阳性，精确率掉到 0.1——**把一个正确行为判成错误**。
而基线 §5 写着 ``CONTRAINDICATED_FOR : Drug → Disease/Population``，
图谱本来就会生成 10 条边。

原子级还有个好处：**对粒度不敏感**。模型把整行原样返回、还是拆成 10 条，
切出来的原子集合是同一个，得分一样。粒度差异不再污染指标。

归一化的三件事（都有实测依据）
------------------------------
1. **去掉括号补充说明** ``（…）``——模型常省掉 ``（见【药物相互作用】）``
   这类交叉引用（实测奥希替尼）。不处理会把它判成漏抽。
2. **去标点与空白**——"严重的肝、肾功能损害者" 与 "严重的肝肾功能损害者" 应等价。
3. **去 markdown 强调** ``**`` ``*``。

⚠️ 一个已知的、有意为之的取舍：判定用**最长公共子串占比**（阈值 0.85），
不用整串相等。所以"改写了措辞但意思相同"的条目会判**命中**。
评测的是**有没有抽到**，不是**会不会改写**——这与标注时"照抄原文"的
要求是配套的。

用法
----
    python scripts/score_extraction.py                    # 评基线（自由文本产物）
    python scripts/score_extraction.py --show-misses      # 顺便列出漏抽/多抽
    python scripts/score_extraction.py --input <目录> --method <名字>
    python scripts/score_extraction.py --method section-json \
        --items-dir data/extracted/section-json/items      # 评结构化产物

⚠️ **自由文本产物没有 `generic_only` 标记，一律记 `None`**，不默认成 `False`。
把"没有这个字段"和"模型判它不是通用过敏禁忌"混为一谈，等于白送一批假成绩。

产出
----
    data/extracted/<method>/score.json    逐药得分 + 汇总（入库，消融实验的对照基准）
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import re
import sys
from pathlib import Path

from insert_io import read_csv_rows
from opencmkg_io import REPO_ROOT

GOLD_FILE = REPO_ROOT / "data" / "eval" / "contraindications_gold.csv"
EXTRACTED_DIR = REPO_ROOT / "data" / "extracted"

#: 原子切分：顿号、逗号、分号
ATOM_SPLIT = re.compile(r"[、，,；;]")

#: 太短的原子不参与（"的"、"等"这类残渣）
MIN_ATOM = 2

#: 最长公共子串占 needle 的比例达到它就算命中
THRESHOLD = 0.85

#: 输出里的条目行：``- x`` / ``* x`` / ``1. x`` / ``(2) x``
#: 注意要求"标记后面有空白"，否则 ``**无**`` 这种加粗会被误认成项目符号
BULLET = re.compile(r"^\s*(?:[-*•●·]\s+|\d+[.、)]\s*|[（(]\d+[）)]\s*)")


def log(msg: str = "") -> None:
    print(msg, flush=True)


def norm(s: str) -> str:
    """归一化：去 markdown、去括号补充说明、去标点空白、转小写。"""
    s = re.sub(r"[*_`]+", "", s)
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    s = re.sub(r"[\s\u3000]+", "", s)
    s = re.sub(r"[^\w]+", "", s)
    return s.lower()


def split_atoms_raw(text: str) -> list[str]:
    """按 、，； 切出原子，**保留原文措辞**（供人阅读与标注）。

    与 :func:`atoms` 的区别：这个不归一化，是给人看的。
    """
    t = re.sub(r"^[\s•●·\-*?]+", "", text).strip()
    return [p.strip().strip("。").strip()
            for p in ATOM_SPLIT.split(t) if p.strip()]


def atoms(text: str) -> list[str]:
    """把一段文字切成最小并列项（归一化后）。"""
    out = []
    for part in split_atoms_raw(text):
        n = norm(part)
        if len(n) >= MIN_ATOM:
            out.append(n)
    return out


def lcs_len(a: str, b: str) -> int:
    """最长公共**子串**长度（连续）。"""
    if not a or not b:
        return 0
    m = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return m.find_longest_match(0, len(a), 0, len(b)).size


def cover(needle: str, hay: str) -> float:
    return lcs_len(needle, hay) / len(needle) if needle else 0.0


def match(gold: list[str], out: list[str]) -> tuple[int, list[str], list[str]]:
    """贪心一对一匹配。返回 ``(TP, 漏掉的金标准原子, 对不上的输出原子)``。"""
    pairs = []
    for i, g in enumerate(gold):
        for j, o in enumerate(out):
            c = cover(g, o)
            if c >= THRESHOLD:
                pairs.append((c, i, j))
    pairs.sort(reverse=True)
    used_g: set[int] = set()
    used_o: set[int] = set()
    tp = 0
    for _c, i, j in pairs:
        if i in used_g or j in used_o:
            continue
        used_g.add(i)
        used_o.add(j)
        tp += 1
    return (tp,
            [g for i, g in enumerate(gold) if i not in used_g],
            [o for j, o in enumerate(out) if j not in used_o])


def load_gold() -> dict[str, dict]:
    """读金标准，按药归并。"""
    rows, enc = read_csv_rows(GOLD_FILE)
    out: dict[str, dict] = {}
    for r in rows:
        drug = r["drug"].strip()
        d = out.setdefault(drug, {"n_gold": None, "raw": [], "enc": enc})
        n = (r.get("n_gold") or "").strip()
        if n.isdigit():
            d["n_gold"] = int(n)
        d["raw"].append((r.get("gold_text") or "").strip())
    for drug, d in out.items():
        if d["n_gold"] == 0:
            # n_gold=0 = 原文明确无禁忌：那行 gold_text（"无"）只记录事实，
            # 不当成一条禁忌来算（决策已定：忽略数据行）。
            d["atoms"] = []
        else:
            d["atoms"] = [a for line in d["raw"] for a in atoms(line)]
        d["lines"] = d["raw"]
    return out


def load_output_items(path: Path) -> list[dict]:
    """从一个方法的产物里取条目。→ ``[{"text": ..., "generic_only": ...}]``

    支持两种产物格式，**它们不是等价的**：

    * **自由文本**（朴素基线）：``- x`` 这类项目符号行。``generic_only`` 一律
      是 ``None``——基线**没有**这个标记，不许假装它有，更不许默认成 ``False``
      混进"判对了"的账里。
    * **结构化**（按小节送那一层）：``[{text, generic_only}]`` 的 JSON。
      这才是决策 #30 里 ``generic_only`` 该待的地方。

    路径后缀决定读哪种；读不出来或没有该文件 → 空表（= "没给出条目"）。
    """
    if path.suffix == ".json":
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        out: list[dict] = []
        for it in data:
            if isinstance(it, str):
                out.append({"text": it.strip(), "generic_only": None})
            elif isinstance(it, dict) and str(it.get("text", "")).strip():
                g = it.get("generic_only")
                out.append({"text": str(it["text"]).strip(),
                            "generic_only": g if isinstance(g, bool) else None})
        return [it for it in out if it["text"]]

    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    items = []
    for ln in text.split("\n"):
        if BULLET.match(ln):
            items.append({"text": BULLET.sub("", ln).strip(),
                          "generic_only": None})
    return items


def item_path(base: Path, drug: str) -> Path:
    """优先结构化产物（``.json``），没有就退回自由文本（``.txt``）。"""
    j = base / f"{drug}.json"
    return j if j.exists() else base / f"{drug}.txt"


def fmt(x: float) -> str:
    return "—" if x is None else f"{x:.3f}"


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="L4 抽取结果评分")
    ap.add_argument("--method", default="baseline", help="方法名（决定输出目录）")
    ap.add_argument("--input", default=None,
                    help="输出目录，默认 data/extracted/<method>/raw")
    ap.add_argument("--items-dir", default=None,
                    help="结构化产物的目录（[{text,generic_only}] 的 JSON）；"
                         "给了就读它，不给就读 --input 下的自由文本")
    ap.add_argument("--show-misses", action="store_true",
                    help="列出漏抽与多抽的原子")
    args = ap.parse_args(argv)

    indir = Path(args.input) if args.input else EXTRACTED_DIR / args.method / "raw"
    if not indir.is_absolute():
        indir = (REPO_ROOT / indir).resolve()
    if not indir.exists():
        raise SystemExit(f"✗ 找不到输出目录 {indir}")
    items_dir = None
    if args.items_dir:
        items_dir = Path(args.items_dir)
        if not items_dir.is_absolute():
            items_dir = (REPO_ROOT / items_dir).resolve()
        if not items_dir.exists():
            raise SystemExit(f"✗ 找不到条目目录 {items_dir}")

    gold = load_gold()
    log(f"L4 抽取评分 · {args.method}")
    log(f"  金标准      {GOLD_FILE.relative_to(REPO_ROOT)}"
        f"（{len(gold)} 药 / 编码 {next(iter(gold.values()))['enc']}）")
    log(f"  方法输出    {indir.relative_to(REPO_ROOT)}")
    if items_dir:
        log(f"  条目来源    {items_dir.relative_to(REPO_ROOT)}（结构化："
            f"带 generic_only）")
    log(f"  原子切分    、，；  最短 {MIN_ATOM} 字   匹配阈值 {THRESHOLD}")
    log("")

    rows, misses = [], []
    sum_tp = sum_fp = sum_fn = 0
    zero_gold_ok = None
    n_items = n_gen = n_gen_unknown = 0

    for drug in sorted(gold):
        g = gold[drug]
        base = items_dir if items_dir else indir
        src = (base / f"{drug}.json") if items_dir else item_path(base, drug)
        out_items = load_output_items(src)
        out_atoms = [a for it in out_items for a in atoms(it["text"])]
        n_items += len(out_items)
        n_gen += sum(1 for it in out_items if it["generic_only"] is True)
        n_gen_unknown += sum(1 for it in out_items
                             if it["generic_only"] is None)

        if g["n_gold"] == 0:
            # 零禁忌样本：正确行为是"不给出任何禁忌条目"
            ok = len(out_items) == 0
            zero_gold_ok = ok
            rows.append({
                "drug": drug, "kind": "零禁忌", "n_gold_atoms": 0,
                "n_out_atoms": len(out_atoms), "tp": 0, "fp": len(out_atoms),
                "fn": 0, "p": None, "r": None, "f1": None,
                "zero_gold_correct": ok,
            })
            log(f"  {drug:<14} n_gold=0（原文无禁忌）  "
                f"模型给出条目 {len(out_items)} 条  → "
                f"{'✓ 正确声明无禁忌' if ok else '✗ 编出了禁忌'}")
            continue

        tp, miss, extra = match(g["atoms"], out_atoms)
        fp, fn = len(out_atoms) - tp, len(g["atoms"]) - tp
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * p * r / (p + r) if p + r else 0.0

        sum_tp += tp
        sum_fp += fp
        sum_fn += fn
        rows.append({
            "drug": drug, "kind": "有禁忌", "n_gold_atoms": len(g["atoms"]),
            "n_out_atoms": len(out_atoms), "tp": tp, "fp": fp, "fn": fn,
            "p": round(p, 4), "r": round(r, 4), "f1": round(f1, 4),
        })
        log(f"  {drug:<14} 金标准 {len(g['atoms']):>2} 原子   输出 {len(out_atoms):>2} 原子"
            f"   TP {tp:>2}  FP {fp:>2}  FN {fn:>2}"
            f"   P {p:.3f}  R {r:.3f}  F1 {f1:.3f}")
        if miss or extra:
            misses.append((drug, miss, extra))

    scored = [r for r in rows if r["kind"] == "有禁忌"]
    micro_p = sum_tp / (sum_tp + sum_fp) if sum_tp + sum_fp else 0.0
    micro_r = sum_tp / (sum_tp + sum_fn) if sum_tp + sum_fn else 0.0
    micro_f1 = (2 * micro_p * micro_r / (micro_p + micro_r)
                if micro_p + micro_r else 0.0)
    macro_p = sum(r["p"] for r in scored) / len(scored) if scored else 0.0
    macro_r = sum(r["r"] for r in scored) / len(scored) if scored else 0.0
    macro_f1 = sum(r["f1"] for r in scored) / len(scored) if scored else 0.0

    log("")
    log("── 汇总 ──")
    log(f"  参评药数        {len(scored)}（另 1 个零禁忌样本单列）")
    log(f"  原子总数        TP {sum_tp}  FP {sum_fp}  FN {sum_fn}")
    log(f"  Micro           P {micro_p:.3f}  R {micro_r:.3f}  F1 {micro_f1:.3f}")
    log(f"  Macro           P {macro_p:.3f}  R {macro_r:.3f}  F1 {macro_f1:.3f}")
    log(f"  零禁忌样本      {'✓ 正确声明无禁忌' if zero_gold_ok else '✗ 编出了禁忌'}")
    if n_items:
        log(f"  条目中 generic_only 标为 true 的  {n_gen} / {n_items}")
        if n_gen_unknown:
            log(f"  ⚠️ 有 {n_gen_unknown} 条**没有** generic_only 标记"
                f"（自由文本产物一律算无标记，不许默认成 false）")

    if args.show_misses and misses:
        log("")
        log("── 漏抽 / 多抽 ──")
        for drug, miss, extra in misses:
            log(f"  {drug}")
            for m in miss:
                log(f"      漏  {m}")
            for e in extra:
                log(f"      多  {e}")

    out_dir = EXTRACTED_DIR / args.method
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "method": args.method,
        "gold_file": str(GOLD_FILE.relative_to(REPO_ROOT)),
        "input": str(indir.relative_to(REPO_ROOT)),
        "items_dir": str(items_dir.relative_to(REPO_ROOT)) if items_dir else None,
        "n_items": n_items,
        "n_generic_only": n_gen,
        "n_generic_only_unknown": n_gen_unknown,
        "threshold": THRESHOLD,
        "min_atom": MIN_ATOM,
        "unit": "最小并列项（按 、，； 切分，两边同样处理）",
        "micro": {"p": round(micro_p, 4), "r": round(micro_r, 4),
                  "f1": round(micro_f1, 4),
                  "tp": sum_tp, "fp": sum_fp, "fn": sum_fn},
        "macro": {"p": round(macro_p, 4), "r": round(macro_r, 4),
                  "f1": round(macro_f1, 4)},
        "zero_gold_correct": zero_gold_ok,
        "per_drug": rows,
    }
    (out_dir / "score.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log("")
    log(f"  已写 {(out_dir / 'score.json').relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
