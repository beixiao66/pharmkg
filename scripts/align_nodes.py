#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L4 · 原子 → 图谱节点 的对齐器
==============================

L4 的产出终点是 `CONTRAINDICATED_FOR : Drug → Disease/Population`。
抽取（把说明书里的禁忌抠出来）实测**基线就已经满分**，
真正难的是这一步：**把"严重心脏病"变成图谱里的一个节点**。

分级（决策：规则优先，LLM 兜底）
--------------------------------
===========  ================================================  ==========
级别          做什么                                            命中什么
===========  ================================================  ==========
① 精确        归一化后与目标词表里的名字完全相等                  ``癫痫`` / ``青光眼``
② 包含        目标名 ⊆ 原子 或 原子 ⊆ 目标名（长度比 ≤ 2）        ``近期有心肌梗死发作史`` → ``心肌梗死``
③ 前后缀      最长公共子串贴着两边的头或尾且占比 ≥ 0.8            ``中性粒细胞减少`` → ``中性粒细胞减少症``
④ 新建        都没命中 → 用归一化后的原子名**新建 Disease 节点**   ``卒中`` / ``尿潴留``
⑤ LLM 兜底    ①②③④ 都不满意时交模型判定（**尚未接入**）
===========  ================================================  ==========

目标词表
--------
* **疾病** = OpenCMKG 的 14,726 个病名（L1 骨架用的就是这套名字，不共用就串不起来）
* **人群** = 受控小词表（与 `population_tags` 对齐）

⚠️ **纪律：不做针对评测集的手工同义词表。**
例如 ``甲状腺机能亢进 → 甲状腺功能亢进``、``粒细胞减少 → 中性粒细胞减少``
这类，**不写进规则**——那等于在冻结的测试集上调参，消融数字就废了。
它们应该由 ⑤ LLM 兜底解决，而"规则只能做到多少、剩下多少必须靠语义"
**本身就是要报告的结论**（与第 0 步"编辑距离在药名上不可靠"是同一类发现）。

用法
----
    python scripts/align_nodes.py --eval     # 在金标准上量各级命中率
    python scripts/align_nodes.py --atom 严重心脏病
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

from insert_io import read_csv_rows
from opencmkg_io import REPO_ROOT, load_entities_dict, load_triples

EVAL_DIR = REPO_ROOT / "data" / "eval"
NODE_FILE = EVAL_DIR / "contraindications_nodes.csv"

#: 人群受控词表（与 population_tags 对齐；通用「药物过敏者」已按决策排除）
POPULATION = ("孕妇", "哺乳期女性", "育龄女性", "儿童", "老年人",
              "大环内酯类药物过敏者", "三环类药物过敏者")

#: 剥离的**结构词**（"…的患者""…者""禁用"这类，是句式不是概念）
STRUCT_WORDS = ("的患者", "患者", "的病人", "病人", "的人", "患有", "患",
                "者", "禁用", "禁止使用", "不应使用本品", "不应使用", "慎用")

#: 剥离的**严重程度**（是同一个节点的分级，不是不同节点）
SEVERITY_WORDS = ("中度至重度", "中重度", "轻中度", "极重度", "严重", "重度", "中度", "轻度")

#: 剥掉的**限定语**（时间/条件，不改变节点身份）
QUALIFIER_PATTERNS = (
    r"最近\d+个月内(发生|患有)?",
    r"近\d+个月内(发生|患有)?",
    r"近期有?",
    r"过去或目前有",
    r"正在使用",
)

PUNCT = re.compile(r"[，。；：、,.;:！!？?【】\[\]“”\"'’‘·—\-~～/\\]+")
#: 前导的项目符号/编号。⚠️ **不能用 `\w`**——Python 的 `\w` 含中文，
#: 会把"癫痫"整个当符号剥掉，归一化结果变成空串（踩过）。
LEAD = re.compile(r"^[\s0-9A-Za-z.、()（）•●·\-*?]+")


def normalize(s: str) -> str:
    """归一化成"节点名候选"。

    只做**通用**的剥离：项目符号 / 括号补充 / 标点 / 结构词 / 严重程度 / 时间限定。
    不做任何针对具体病名的同义词替换（见模块文档的纪律说明）。
    """
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    s = LEAD.sub("", s)
    s = PUNCT.sub("", s)
    for pat in QUALIFIER_PATTERNS:
        s = re.sub(pat, "", s)
    for w in STRUCT_WORDS:
        s = s.replace(w, "")
    for w in SEVERITY_WORDS:
        s = s.replace(w, "")
    # 首尾残留的"的"（"重度的贫血" → "贫血"）
    s = re.sub(r"^的+|的+$", "", s)
    return s.strip()


class Aligner:
    def __init__(self) -> None:
        triples, _ = load_triples()
        entities, _ = load_entities_dict(triples=triples)
        self.disease = set(entities.get("disease", []))
        self.population = set(POPULATION)

    # -- 各级判定 ------------------------------------------------------
    def _exact(self, n: str) -> tuple[str, str] | None:
        if n in self.disease:
            return n, "疾病"
        if n in self.population:
            return n, "人群"
        return None

    def _contain(self, n: str) -> tuple[str, str] | None:
        """**只认一个方向**：目标名 ⊆ 原子。

        反方向（原子 ⊆ 目标名）**刻意不做**——那是"越具体越像"的陷阱：
        ``卒中`` 会对到 ``出血性脑卒中``、``发热`` 会对到 ``脸发热``，
        在图谱里都是假边。这条不是拍脑袋：前面调相似度时已经栽过两次。

        代价是 ``免疫缺陷状态`` 这类"原子比目标名窄"的命中不了——留给 ⑤ LLM。
        """
        if not n:
            return None
        best = None
        for v in self.disease:
            # 下限不是拍脑袋：去掉它就会冒出「发热 → 热」「拉普乳糖酶缺乏 → 拉」
            # 这种**一个字的巧合匹配**。要求匹配到的病名至少占原子一半、
            # 且不少于 3 个字——1~2 个字的名字不承载身份。
            if not v or v not in n:
                continue
            if len(v) < 3 or len(v) * 2 < len(n):
                continue
            if best is None or len(v) > len(best):
                best = v
        return (best, "疾病") if best else None

    def _similar(self, n: str) -> tuple[str, str, float] | None:
        """最长公共子串**贴着两边的头或尾**且占比 ≥ 0.8。"""
        best, best_s = "", 0.0
        for v in self.disease:
            if not v:
                continue
            lo, hi = min(len(v), len(n)), max(len(v), len(n))
            if hi > lo * 2:
                continue
            m = difflib.SequenceMatcher(None, n, v, autojunk=False)
            blk = m.find_longest_match(0, len(n), 0, len(v))
            if blk.size == 0:
                continue
            head = blk.a == 0 and blk.b == 0
            tail = blk.a + blk.size == len(n) and blk.b + blk.size == len(v)
            if not (head or tail):
                continue
            s = blk.size / hi
            if s > best_s:
                best, best_s = v, s
        return (best, "疾病", best_s) if best_s >= 0.8 else None

    def align(self, atom: str) -> dict:
        n = normalize(atom)
        for level, fn in (("①精确", self._exact),
                          ("②包含", self._contain)):
            got = fn(n)
            if got:
                return {"node": got[0], "node_type": got[1], "level": level,
                        "normalized": n}
        got = self._similar(n)
        if got:
            return {"node": got[0], "node_type": got[1], "level": "③前后缀",
                    "normalized": n}
        return {"node": n, "node_type": "疾病", "level": "④新建",
                "normalized": n}


def load_gold_edges() -> list[dict]:
    """金标准里**入图**的行（`node_type ∈ 疾病/人群/用药`）。"""
    rows, _ = read_csv_rows(NODE_FILE)
    return [r for r in rows
            if (r.get("verdict") or "").strip() == "保留"
            and (r.get("node_type") or "").strip() in ("疾病", "人群", "用药")]


def do_eval() -> int:
    al = Aligner()
    gold = load_gold_edges()
    print("L4 对齐器 · 在金标准上量命中率")
    print(f"  目标词表  疾病 {len(al.disease):,} · 人群 {len(al.population)}")
    print(f"  金标准    {len(gold)} 条入图边")
    print()

    hit_by_level: dict[str, int] = {}
    miss: list[tuple[str, str, str, str]] = []
    for r in gold:
        atom, want = r["atom"], r["node"]
        got = al.align(atom)
        ok = got["node"] == want
        lv = got["level"] + ("✓" if ok else "✗")
        hit_by_level[lv] = hit_by_level.get(lv, 0) + 1
        if not ok:
            miss.append((atom, want, got["node"], got["level"]))

    print("  各级命中：")
    for k in sorted(hit_by_level):
        print(f"    {k:<8} {hit_by_level[k]:>3}")

    hit = len(gold) - len(miss)
    print()
    print(f"  对齐准确率  {hit}/{len(gold)} = {hit / max(1, len(gold)):.1%}")
    print(f"  需 LLM 兜底 {len(miss)}")
    if miss:
        print()
        print("  ── 规则没对上的（这些就是 LLM 该接手的）──")
        for atom, want, got, lv in miss:
            print(f"    {atom[:26]:<28} 期望 {want:<12} 实得 {got}（{lv}）")
    return 0


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="L4 原子→节点 对齐器")
    ap.add_argument("--eval", action="store_true", help="在金标准上量命中率")
    ap.add_argument("--atom", default=None, help="对单个原子跑一遍，看各级判定")
    args = ap.parse_args(argv)

    if args.atom:
        al = Aligner()
        print(f"原子：{args.atom}")
        print(f"  归一化 → {normalize(args.atom)}")
        print(f"  判定   → {al.align(args.atom)}")
        return 0
    return do_eval()


if __name__ == "__main__":
    raise SystemExit(main())
