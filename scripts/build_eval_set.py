#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L4 评测集构建与校验
====================

按已定决策：**从 36 份里抽 10 份做人工标注**，作为 L4 的金标准；
准确率**以"一条禁忌"为单位算 P / R / F1**。

抽样规则（可复现，写进论文）
----------------------------
**按【禁忌】正文长度分层，5 层各随机取 2 篇，随机种子固定；再确定性强纳入
1 篇极端样本。** 共 **11 篇**。

不改成"随便挑"或"挑禁忌最长的"：

* 全随机 → 可能抽不到"禁忌只有 13 字"那几篇，而那正是最容易翻车的样本；
* 挑最长的 → 等于对着长文本出题，recall 会虚高；
* 分层保证覆盖从"无"到 391 字的整个区间，**消融实验的数字才有解释力**。

**为什么还要强纳入**：分层抽样没抽中 ``托法替布``——它【禁忌】正文只有 ``无``
一个字，是"原文没有禁忌"与"我们没抽到"会不会混的**唯一试金石**。
它落在第 1 层内，靠随机抽不到就等于没测。标注它约 10 秒，成本可忽略。

「一条禁忌」的定义（**决定 P/R/F1 的分母**）
--------------------------------------------
**按"独立的人群 / 情形"拆开。** 例：

    原文：严重的肝、肾功能损害者、孕妇及长期接触砷或有砷中毒者禁用。
    → 3 条：① 严重的肝、肾功能损害者   ② 孕妇   ③ 长期接触砷或有砷中毒者

为什么不照原句算 1 条：图谱里每条 ``CONTRAINDICATED_FOR`` 边对应**一个**
"谁不能用"（决策 #30 的 e：一条禁忌一条边），粒度必须对齐。
照原句算的话，"只抽到 1 条、漏掉另外 2 个人群"会被判**完全正确**——
等于把最容易出的错藏起来。

⚠️ **纪律**：这 11 份一旦标好就冻结，**不得进入任何提示词或 few-shot**
（与决策 #26 同一纪律）。否则等于考前透题。

用法
----
    python scripts/build_eval_set.py --list      # 看抽中了哪 10 份、为什么
    python scripts/build_eval_set.py             # 生成待标注表（不覆盖已有标注）
    python scripts/build_eval_set.py --force     # 重新生成（会丢掉已有标注！）
    python scripts/build_eval_set.py --verify    # 校验标注是否填完、条数是否自洽

产出
----
    data/eval/contraindications_gold.csv    待人工标注：一行 = 一条禁忌
"""

from __future__ import annotations

import argparse
import csv
import random
import re
import sys
from pathlib import Path

from insert_io import TEXT_DIR, load_targets, split_sections
from opencmkg_io import REPO_ROOT

EVAL_DIR = REPO_ROOT / "data" / "eval"
GOLD_FILE = EVAL_DIR / "contraindications_gold.csv"

#: 分层数与每层取几篇
LAYERS = 5
PER_LAYER = 2
SEED = 20261013

#: 确定性强纳入的药——不靠随机，因为它覆盖的是**极端情形**，抽不到就等于没测。
#: 托法替布的【禁忌】正文只有「无」一个字：它是"原文没有禁忌"与
#: "我们没抽到"会不会混的唯一试金石（决策 #30 / Q5 的 generic_only）。
FORCE_INCLUDE = ("托法替布",)

#: 目标小节（决策 #30 / Q4）
SECTION = "禁忌"

REVIEW_FIELDS = ["drug", "n_gold", "item_idx", "gold_text", "src_line", "note"]


def log(msg: str = "") -> None:
    print(msg, flush=True)


def build_pool() -> list[dict]:
    """读 36 份，取【禁忌】正文与去空白长度。"""
    pool = []
    for p in sorted(TEXT_DIR.glob("*.txt")):
        text = p.read_text(encoding="utf-8")
        secs = split_sections(text)
        body = secs.get(SECTION, "")
        pool.append({
            "drug": p.stem,
            "body": body,
            "chars": len(re.sub(r"\s", "", body)),
            "lines": [ln for ln in body.split("\n") if ln.strip()],
        })
    return pool


def pick(pool: list[dict]) -> list[dict]:
    """分层随机取样 + 确定性强纳入——合起来可复现。"""
    ordered = sorted(pool, key=lambda d: (d["chars"], d["drug"]))
    n = len(ordered)
    # 尽量均分：36 份 / 5 层 = 7,7,7,7,8
    base, extra = divmod(n, LAYERS)
    rng = random.Random(SEED)
    chosen: list[dict] = []
    start = 0
    for i in range(LAYERS):
        size = base + (1 if i < extra else 0)
        layer = ordered[start:start + size]
        start += size
        k = min(PER_LAYER, len(layer))
        chosen.extend(rng.sample(layer, k))

    # 强纳入：已有的不重复加
    picked = {d["drug"] for d in chosen}
    for name in FORCE_INCLUDE:
        if name in picked:
            continue
        hit = next((d for d in pool if d["drug"] == name), None)
        if hit is not None:
            chosen.append(hit)
            picked.add(name)
    return chosen


def write_stub(chosen: list[dict], force: bool) -> int:
    if GOLD_FILE.exists() and not force:
        raise SystemExit(
            f"✗ {GOLD_FILE.relative_to(REPO_ROOT)} 已存在，**不覆盖**"
            "（里面可能有你辛苦标的内容）。\n"
            "  确实要重来请加 --force，并先自己备份。"
        )
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for d in sorted(chosen, key=lambda x: x["drug"]):
        src = d["lines"] or [""]
        for i, ln in enumerate(src, 1):
            note = ""
            if not d["lines"]:
                note = "原文该节为空——若确认无禁忌，把 n_gold 填 0 并删掉本行"
            elif d["chars"] <= 2:
                note = f"原文就是「{d['body'].strip()}」——若确认无禁忌，n_gold 填 0 并删掉本行"
            rows.append({
                "drug": d["drug"],
                "n_gold": "",                 # ← 待你填：该药一共几条禁忌
                "item_idx": i,
                "gold_text": ln,              # ← 待你核：原文照抄，不改写
                "src_line": i,
                "note": note,
            })
    with GOLD_FILE.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REVIEW_FIELDS)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def verify(chosen: list[dict]) -> int:
    if not GOLD_FILE.exists():
        raise SystemExit(f"✗ 还没有 {GOLD_FILE.relative_to(REPO_ROOT)}")
    with GOLD_FILE.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    by_drug: dict[str, list[dict]] = {}
    for r in rows:
        by_drug.setdefault(r["drug"], []).append(r)

    expect = {d["drug"] for d in chosen}
    got = set(by_drug)
    log(f"  应有 {len(expect)} 个药，表里有 {len(got)} 个")
    if expect - got:
        log(f"  ✗ 缺：{'、'.join(sorted(expect - got))}")
    if got - expect:
        log(f"  ✗ 多：{'、'.join(sorted(got - expect))}")

    bad = []
    total = 0
    log("")
    log(f"  {'药名':<14} {'n_gold':>6} {'行数':>4}  状态")
    for drug in sorted(got):
        items = by_drug[drug]
        n = items[0].get("n_gold", "").strip()
        if not n.isdigit():
            bad.append(f"{drug}: n_gold 未填")
            log(f"  {drug:<14} {'—':>6} {len(items):>4}  ✗ n_gold 未填")
            continue
        total += int(n)
        ok = int(n) == len(items)
        if not ok:
            bad.append(f"{drug}: n_gold={n} 但表里有 {len(items)} 行")
        log(f"  {drug:<14} {n:>6} {len(items):>4}  {'✓' if ok else '✗ 条数对不上'}")
    log("")
    log(f"  标注禁忌总条数 {total}")
    if bad:
        log("")
        for b in bad:
            log(f"  ✗ {b}")
        return 1
    log("  ✓ 10 份全部填妥且自洽")
    return 0


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="L4 评测集构建与校验")
    ap.add_argument("--list", action="store_true", help="只列出抽中的篇目")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的标注表")
    ap.add_argument("--verify", action="store_true", help="校验标注是否填妥")
    args = ap.parse_args(argv)

    pool = build_pool()
    n_missing = [d["drug"] for d in pool if not d["lines"]]
    chosen = pick(pool)

    if args.verify:
        log("L4 评测集 · 校验")
        return verify(chosen)

    forced = set(FORCE_INCLUDE)
    log("L4 评测集 · 分层抽样 + 强纳入")
    log(f"  候选        {len(pool)} 份（{SECTION} 一节非空 "
        f"{len(pool) - len(n_missing)} 份）")
    log(f"  规则        按【{SECTION}】去空白长度分 {LAYERS} 层，"
        f"每层随机取 {PER_LAYER} 篇，seed={SEED}")
    log(f"  强纳入      {'、'.join(FORCE_INCLUDE)}（极端样本，不靠随机）")
    log(f"  合计        {len(chosen)} 篇")
    log("")

    ordered = sorted(pool, key=lambda d: (d["chars"], d["drug"]))
    base, extra = divmod(len(ordered), LAYERS)
    start = 0
    sel = {d["drug"] for d in chosen}
    for lyr in range(LAYERS):
        size = base + (1 if lyr < extra else 0)
        layer = ordered[start:start + size]
        start += size
        names = "、".join(
            ("【" + d["drug"] + "】") if d["drug"] in sel else d["drug"]
            for d in layer)
        log(f"  第 {lyr + 1} 层（{layer[0]['chars']}–{layer[-1]['chars']} 字）")
        log(f"      {names}")
    log("")
    log("  抽中：")
    for d in sorted(chosen, key=lambda x: x["chars"]):
        tag = "   ← 强纳入" if d["drug"] in forced else ""
        log(f"      {d['chars']:>4} 字  {d['drug']}{tag}")
    log("")

    if args.list:
        return 0

    n = write_stub(chosen, args.force)
    log(f"  已写 {GOLD_FILE.relative_to(REPO_ROOT)}（{n} 行待核）")
    log("")
    log("  怎么填（「一条禁忌」= **一个独立的人群/情形**）：")
    log("    0. 例：原文「严重的肝、肾功能损害者、孕妇及长期接触砷或有砷中毒者禁用。」")
    log("       要拆成 **3 条**：严重的肝、肾功能损害者 / 孕妇 / 长期接触砷或有砷中毒者")
    log("       —— 表里已经按原文的每一行预填，你只需合并或拆分")
    log("    1. gold_text **照抄原文**，别改写")
    log("       —— 评价的是「有没有抽到」，不是「会不会改写」")
    log("    2. 每行的 n_gold 填该药**一共几条**（同一药的所有行填同一个数）")
    log("    3. 原文确实没有禁忌的（如托法替布正文只有「无」），"
        "n_gold 填 0 并删掉数据行")
    log("    4. 填完跑 --verify")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
