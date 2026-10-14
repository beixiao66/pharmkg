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
import json
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

    def top_candidates(self, n: str, k: int = 15) -> list[str]:
        """按最长公共子串占比取前 k 个词表名，供 ⑤ LLM 挑选。

        必须给候选：不给的话模型只会一路"新建"，⑤ 的价值就被埋没了。
        """
        scored = []
        for v in self.disease:
            if not v:
                continue
            lo, hi = min(len(v), len(n)), max(len(v), len(n))
            if hi > lo * 2:
                continue
            m = difflib.SequenceMatcher(None, n, v, autojunk=False)
            s = m.find_longest_match(0, len(n), 0, len(v)).size / hi
            if s > 0.3:
                scored.append((s, v))
        scored.sort(reverse=True)
        return [v for _s, v in scored[:k]]


# ── ⑤ LLM 兜底 ────────────────────────────────────────────────────────

LLM_SYSTEM = """你是医学实体对齐助手。给你一条药品说明书里的禁忌表述，以及从知识图谱
疾病词表中挑出的候选名，请判断它应当对齐到哪个节点。

规则：
1. 优先选候选中**语义等价**的那一个。**不要选更具体的亚型**——
   例如「卒中」不要选「出血性脑卒中」，「感染」不要选「感染性急性小管间质性肾炎」。
2. 候选里确实没有等价的，就按规范中文病名**新建**一个节点名：简短、规范、
   不带严重程度（严重/重度/中度）与时间限定（近期/最近N个月内）。
3. 人群类（孕妇 / 哺乳期女性 / 育龄女性 / 儿童 / 老年人 / 药物过敏者 等）
   node_type 填「人群」；其余填「疾病」。

只输出 JSON，不要解释：
{"action": "existing|new", "node": "节点名", "node_type": "疾病|人群", "why": "不超过20字"}"""


def load_gold_edges() -> list[dict]:
    """金标准里**入图**的行（`node_type ∈ 疾病/人群/用药`）。"""
    rows, _ = read_csv_rows(NODE_FILE)
    return [r for r in rows
            if (r.get("verdict") or "").strip() == "保留"
            and (r.get("node_type") or "").strip() in ("疾病", "人群", "用药")]


CACHE_FILE = REPO_ROOT / "data" / "extracted" / "alignment_cache.json"


def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(cache: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def llm_align(al: Aligner, atom: str, cache: dict):
    """⑤：把原子 + 候选交给百炼判定。结果按原子文本缓存，重跑不再花钱。"""
    if atom in cache:
        return cache[atom]

    sys.path.insert(0, str(REPO_ROOT / "backend"))
    from app.config import get_settings
    from openai import OpenAI

    st = get_settings()
    if not st.llm_configured:
        raise SystemExit("✗ 百炼未配齐，无法做 LLM 兜底")
    client = OpenAI(api_key=st.dashscope_api_key, base_url=st.dashscope_base_url)

    n = normalize(atom)
    cands = al.top_candidates(n, 15)
    user = (f"禁忌表述：{atom}\n"
            f"（归一化后：{n}）\n\n"
            f"候选节点名（来自知识图谱疾病词表）：\n"
            + "\n".join(f"- {c}" for c in cands))
    resp = client.chat.completions.create(
        model=st.llm_model,
        messages=[{"role": "system", "content": LLM_SYSTEM},
                  {"role": "user", "content": user}],
        temperature=0.0, max_tokens=300, timeout=120,
    )
    text = resp.choices[0].message.content or ""
    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else {}
    out = {"node": str(data.get("node", "")).strip(),
           "node_type": str(data.get("node_type", "疾病")).strip(),
           "action": str(data.get("action", "new")).strip(),
           "why": str(data.get("why", "")).strip(),
           "candidates": cands[:15]}
    cache[atom] = out
    return out


def do_eval(use_llm: bool = False) -> int:
    al = Aligner()
    gold = load_gold_edges()
    cache = load_cache() if use_llm else {}
    print("L4 对齐器 · 在金标准上量命中率")
    print(f"  目标词表  疾病 {len(al.disease):,} · 人群 {len(al.population)}")
    print(f"  金标准    {len(gold)} 条入图边")
    print(f"  ⑤ LLM     {'开启' if use_llm else '关闭（只量规则）'}")
    print()

    hit_by_level: dict[str, int] = {}
    miss: list[tuple[str, str, str, str]] = []
    rule_hit = final_hit = 0
    llm_called = llm_fixed = 0
    for r in gold:
        atom, want = r["atom"], r["node"]
        rule = al.align(atom)
        # 比节点名时先归一化：**节点身份不该取决于一个连字符或"严重"二字**。
        # 实测踩过：`葡萄糖半乳糖吸收障碍` vs `葡萄糖-半乳糖吸收障碍`、
        # `骨髓功能受损` vs `骨髓功能严重受损` 都被判成错。
        rule_ok = normalize(rule["node"]) == normalize(want)
        rule_hit += rule_ok

        got = rule
        if use_llm and rule["level"] == "④新建":
            # 规则什么都没找到才交 LLM——这就是"兜底"的定义
            ll = llm_align(al, atom, cache)
            llm_called += 1
            if ll.get("node"):
                got = {"node": ll["node"],
                       "node_type": ll.get("node_type", "疾病"),
                       "level": f"⑤LLM({ll.get('action', '')})",
                       "normalized": rule["normalized"]}
        ok = normalize(got["node"]) == normalize(want)
        final_hit += ok
        if use_llm and rule["level"] == "④新建" and ok and not rule_ok:
            llm_fixed += 1
        lv = got["level"] + ("✓" if ok else "✗")
        hit_by_level[lv] = hit_by_level.get(lv, 0) + 1
        if not ok:
            miss.append((atom, want, got["node"], got["level"]))

    print("  各级命中：")
    for k in sorted(hit_by_level):
        print(f"    {k:<14} {hit_by_level[k]:>3}")

    print()
    print(f"  ① 规则单独       {rule_hit}/{len(gold)} = {rule_hit / len(gold):.1%}")
    if use_llm:
        save_cache(cache)
        print(f"  ⑤ LLM 调用       {llm_called} 条（④新建 的那些）")
        print(f"  ② 规则 + LLM     {final_hit}/{len(gold)} = "
              f"{final_hit / len(gold):.1%}")
        print(f"     LLM 救回        {llm_fixed} 条")
        print(f"  缓存             {CACHE_FILE.relative_to(REPO_ROOT)}")
    print()
    print(f"  仍不对 {len(miss)} 条")
    if miss:
        print()
        print("  ── 没对上的 ──")
        for atom, want, got, lv in miss:
            print(f"    {atom[:26]:<28} 期望 {want:<14} 实得 {got}（{lv}）")
    return 0


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="L4 原子→节点 对齐器")
    ap.add_argument("--eval", action="store_true", help="在金标准上量命中率")
    ap.add_argument("--llm", action="store_true",
                    help="开启 ⑤ LLM 兜底（对 ④新建 的原子调模型；结果带缓存）")
    ap.add_argument("--atom", default=None, help="对单个原子跑一遍，看各级判定")
    args = ap.parse_args(argv)

    if args.atom:
        al = Aligner()
        print(f"原子：{args.atom}")
        print(f"  归一化 → {normalize(args.atom)}")
        print(f"  判定   → {al.align(args.atom)}")
        return 0
    return do_eval(use_llm=args.llm)


if __name__ == "__main__":
    raise SystemExit(main())
