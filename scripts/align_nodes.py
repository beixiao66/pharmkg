#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L4 · 原子 → 图谱节点 的对齐器
==============================

L4 的产出终点是 `CONTRAINDICATED_FOR : Drug → Disease/Population`。
抽取（把说明书里的禁忌抠出来）实测**基线就已经满分**，
真正难的是这一步：**把"严重心脏病"变成图谱里的一个节点**。

分级（决策 #33：规则优先，LLM 兜底）
------------------------------------
===========  ================================================  ==========
级别          做什么                                            命中什么
===========  ================================================  ==========
① 精确        归一化后与目标词表（含人群变体）的名字完全相等        ``青光眼`` / ``妊娠期妇女``→``孕妇``
② 包含        **目标名 ⊆ 原子**（长度比 ≤ 2，名字 ≥ 3 字）        ``近期有心肌梗死发作史`` → ``心肌梗死``
③ 前后缀      最长公共子串贴着两边的头或尾且占比 ≥ 0.8            ``中性粒细胞减少`` → ``中性粒细胞减少症``
④ 新建        都没命中 → 用归一化后的原子名**新建 Disease 节点**   ``卒中`` / ``尿潴留``
⑤ LLM 兜底    ④新建 的那些交模型在候选里挑，或另起规范名（有缓存）  ``免疫缺陷状态`` → ``免疫缺陷``
===========  ================================================  ==========

② **只认一个方向**（目标名 ⊆ 原子）。反方向刻意不做——那是"越具体越像"的
陷阱：``卒中`` 会对到 ``出血性脑卒中``、``发热`` 会对到 ``脸发热``，在图谱里
都是假边。代价是 ``免疫缺陷状态`` 这种"原子比目标名窄"的命中不了，交给 ⑤。

目标词表
--------
* **疾病** = OpenCMKG 的 14,726 个病名（L1 骨架用的就是这套名字，不共用就串不起来）
* **人群** = 受控小词表（与 `population_tags` 对齐）**+ 它的同义变体**

⚠️ **纪律：不做针对评测集的手工同义词表。**
例如 ``甲状腺机能亢进 → 甲状腺功能亢进``、``粒细胞减少 → 中性粒细胞减少``
这类，**不写进规则**——那等于在冻结的测试集上调参，消融数字就废了。
它们应该由 ⑤ LLM 兜底解决，而"规则只能做到多少、剩下多少必须靠语义"
**本身就是要报告的结论**（与第 0 步"编辑距离在药名上不可靠"是同一类发现）。

🤔 **那为什么人群词表可以有变体表？**（见 `POPULATION_ALIASES`）
因为两者性质不同：**人群词表是我们自己定的受控词表**，``妊娠期妇女`` 与 ``孕妇``
在说明书里本来**就是同一个槽位**，一个受控条目理应写明它的常见写法——这是**词表定义**，
不是调参。而 ``甲状腺机能亢进`` 与 ``甲状腺功能亢进`` 是 **OpenCMKG 词表自己的毛病**，
"两个近义病名该不该合并"是**图谱质量问题**，不该由对齐器用一张隐藏的同义表糊过去：
那既掩盖了数据质量问题，又等于在冻结的评测集上调参。

⚠️ **顺带发现（已单列进实测报告）：OpenCMKG 的 14,726 个"病名"里混着大量
口语化病史短语和错别字**（``肝功能不正正常``、``勃起不太好硬``、``月经一直就不规律``…
窄模式一扫就 23 条）。词表越脏，② 的假阳性越多。这是 **L8 数据质量的一个新靶点**。

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
from opencmkg_io import (REPO_ROOT, graph_node_names, load_entities_dict,
                         load_triples)

EVAL_DIR = REPO_ROOT / "data" / "eval"
NODE_FILE = EVAL_DIR / "contraindications_nodes.csv"

#: 人群受控词表（与 population_tags 对齐；通用「药物过敏者」已按决策排除）
POPULATION = ("孕妇", "哺乳期女性", "育龄女性", "儿童", "老年人",
              "大环内酯类药物过敏者", "三环类药物过敏者")

#: 人群受控词表的**同义变体**（决策 #33）。
#:
#: 这是**词表自身的定义**，不是"针对评测集的手工同义词表"——理由见模块文档。
#: 一句话：``妊娠期妇女`` 和 ``孕妇`` 是同一个槽位的两种写法，
#: 受控词表就该写明白；而 ``甲状腺机能亢进``/``甲状腺功能亢进`` 是
#: OpenCMKG 词表自己的病，该由"图谱质量"去治，不能靠对齐器偷偷合并。
#:
#: 刻意**不含**更窄/更宽的子集（``婴幼儿``、``新生儿``、``青少年``、
#: ``老年痴呆患者``…）——那些是**另一个**人群，不是一个名字的变体。
POPULATION_ALIASES: dict[str, tuple[str, ...]] = {
    "孕妇": ("妊娠期妇女", "妊娠妇女", "妊娠期女性", "妊娠女性", "妊娠期间",
             "妊娠期", "妊娠", "孕期妇女", "孕期女性", "怀孕妇女", "怀孕女性",
             "怀孕期间", "妊娠状态"),
    "哺乳期女性": ("哺乳期妇女", "哺乳妇女", "哺乳期", "哺乳期间", "哺乳",
                   "母乳喂养妇女", "授乳期妇女"),
    "育龄女性": ("育龄期女性", "育龄妇女", "育龄期妇女", "育龄期", "育龄",
                 "有生育能力的女性", "有生育潜力的女性", "有生育潜力女性",
                 "有生育能力的妇女"),
    "儿童": ("小儿", "儿科患者", "儿童患者"),
    "老年人": ("老年患者", "老年病人", "高龄患者", "高龄老年人", "老年",
               "65岁以上患者", "65岁及以上患者", "65岁以上老年人"),
    "大环内酯类药物过敏者": ("大环内酯类过敏者", "对大环内酯类药物过敏者",
                             "大环内酯类抗生素过敏者", "大环内酯类药物过敏",
                             "大环内酯类过敏"),
    "三环类药物过敏者": ("三环类过敏者", "对三环类药物过敏者",
                         "三环类抗抑郁药过敏者", "三环类抗抑郁药物过敏者",
                         "三环类药物过敏", "三环类过敏"),
}

#: 进 ②包含 匹配的**最短名字**。
#: 3 字的 ``妊娠期`` 会命中 ``妊娠期高血压``（那是**另一个病**），
#: 2 字的 ``小儿`` 会命中 ``小儿过敏性紫癜``、``老年`` 会命中 ``老年痴呆``。
#: 所以 <4 字的写法**只做 ①精确**、不参与包含匹配。
POP_CONTAIN_MIN = 4

#: 疾病名进 ②包含 匹配的最短长度（与原来的下限一致：1~2 个字不承载身份）
CONT_MIN_LEN = 3

#: 剥离的**结构词**（"…的患者""…者""禁用"这类，是句式不是概念）
STRUCT_WORDS = ("的患者", "患者", "的病人", "病人", "的人", "患有", "患",
                "者", "禁用", "禁止使用", "不应使用本品", "不应使用", "慎用",
                # 并列连词（清单里 "……；以及……" 的尾巴），零临床含义
                "以及")

#: 剥离的**严重程度**（是同一个节点的分级，不是不同节点）
SEVERITY_WORDS = ("中度至重度", "中重度", "轻中度", "极重度", "严重", "重度", "中度", "轻度")

#: 剥掉的**限定语**（时间/条件，不改变节点身份）。
#: 注意这里剥的是**句式**，不是概念——``未采取可靠避孕措施的育龄期女性``
#: 剥完就是 ``育龄期女性``，它没有变成另一个人群。
QUALIFIER_PATTERNS = (
    r"最近\d+个月内(发生|患有)?",
    r"近\d+个月内(发生|患有)?",
    r"近期有?",
    r"过去或目前有",
    r"正在使用",
    r"未(采取|使用)(有效|可靠|适当)?的?避孕措施(的)?",
)

PUNCT = re.compile(r"[，。；：、,.;:！!？?【】\[\]“”\"'’‘·—\-~～/\\]+")
#: 前导的项目符号/编号。⚠️ **不能用 `\w`**——Python 的 `\w` 含中文，
#: 会把"癫痫"整个当符号剥掉，归一化结果变成空串（踩过）。
LEAD = re.compile(r"^[\s0-9A-Za-z.、()（）•●·\-*?]+")


def normalize(s: str) -> str:
    """归一化成"节点名候选"。

    只做**通用**的剥离：项目符号 / 括号补充 / 标点 / **一切空白** / 结构词 /
    严重程度 / 时间与条件限定。不做任何针对具体病名的同义词替换（见模块文档的纪律说明）。
    """
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    s = LEAD.sub("", s)
    s = PUNCT.sub("", s)
    # 去掉**一切空白**（含全角空格）。说明书里中英混排的空格是排版噪声：
    # ``最近 6 个月内发生心肌梗死`` 里的空格让 ``最近\d+个月内`` 这条**已有**规则
    # 整个失效（评测把这个问题暴露出来的）。这里不做任何语义替换。
    s = re.sub(r"[\s\u3000]+", "", s)
    for pat in QUALIFIER_PATTERNS:
        s = re.sub(pat, "", s)
    for w in STRUCT_WORDS:
        s = s.replace(w, "")
    for w in SEVERITY_WORDS:
        s = s.replace(w, "")
    # 首尾残留的"的"（"重度的贫血" → "贫血"）
    s = re.sub(r"^的+|的+$", "", s)
    return s.strip()


def canonical_population(al: "Aligner", node: str,
                         node_type: str) -> tuple[str, str, bool]:
    """把 ``node_type=人群`` 的判定**收回到受控词表**。

    返回 ``(规范名, 节点类型, 是否越出受控词表)``：

    * 本来就不是人群 → 原样返回
    * 是人群且能收回（``妊娠`` → ``孕妇``）→ 返回规范名
    * 是人群但**不在**受控词表里 → **降级成疾病**并标 ``True``

    为什么要收：受控词表是**封闭**的（决策 #33），⑤ LLM 不能靠"我觉得这算个人群"
    把它撑大。实测 ⑤ 会把 ``体内有心脏起搏器``、``单胺氧化酶抑制剂使用期``、
    ``特殊人群`` 当成人群；也会给出 ``妊娠``/``育龄期女性`` 这种**同一个概念的
    另一种写法**，不收就会在图里和 ``孕妇``/``育龄女性`` 并存成两个节点。

    这个规则**评分与入库必须共用一份**——所以放在这里，两边都调它。
    """
    if node_type != "人群":
        return node, node_type, False
    canon_pop = al.canon_population(node)
    if canon_pop:
        return canon_pop, "人群", False
    return node, "疾病", True


def canon(s: str) -> str:
    """比较两个节点名、或给边建键时用的**规范名**。

    与 :func:`normalize` 的唯一区别：剥空了就退回"去掉空白的原文"，
    免得空串既当节点名又当比较键。
    """
    return normalize(s) or re.sub(r"[\s\u3000]+", "", s or "")


def node_name(atom: str) -> str:
    """新建节点时用的名字：归一化结果；**剥空了就退回原文**。

    踩过：``不应使用本品`` 整条都是 STRUCT_WORDS，归一化后是**空串**，
    于是"新建"出了一个**没有名字的节点**。空串不能当节点名——
    图谱里没法索引、也没法给人看。
    """
    n = normalize(atom)
    if n:
        return n
    return LEAD.sub("", atom).strip() or atom.strip()


class Aligner:
    def __init__(self) -> None:
        triples, _ = load_triples()
        entities, _ = load_entities_dict(triples=triples)
        # ⚠️ **必须用 `graph_node_names`，不能用 `entities_dict["disease"]`**：
        #    L1 写关系时还会把关系两端 MERGE 成节点，于是图里的病名比
        #    entities_dict 多 **133** 个（`免疫缺陷`、`休克`、`不孕不育`…）。
        #    拿 entities_dict 当目标词表，就会在 `免疫缺陷` **已经存在**的情况下
        #    "新建"出一个 `免疫缺陷状态`——**入库即重复节点**。
        self.disease = graph_node_names("disease", triples=triples, entities=entities)
        self.population = set(POPULATION)

        # ① 变体 → **规范名**。两侧都先 normalize 再比，否则词表里的
        #    ``…过敏者`` 会被 STRUCT_WORDS 的「者」剥掉、永远匹配不上（踩过）。
        self._pop_exact: dict[str, str] = {}
        for name in POPULATION:
            self._pop_exact[normalize(name)] = name
        for name, variants in POPULATION_ALIASES.items():
            for v in variants:
                self._pop_exact.setdefault(normalize(v), name)

        # ② 包含匹配的下标：``(用来匹配的名字, 返回的节点名, 节点类型)``。
        #    变体命中时必须返回**规范名**，不能把 ``妊娠期妇女`` 当节点建出来。
        #    排序是为了**确定性**——同分同长时不能让 set 的迭代顺序决定结果。
        self._contain_index: list[tuple[str, str, str]] = [
            (v, v, "疾病") for v in self.disease if len(v) >= CONT_MIN_LEN]
        self._contain_index += [
            (k, name, "人群")
            for k, name in self._pop_exact.items() if len(k) >= POP_CONTAIN_MIN]
        self._contain_index.sort()

        #: ③ 最近一次判定里**并列最高分**的候选（≥2 说明规则其实没依据挑，
        #: 只是按确定性顺序取了第一个——这个数要报出来，不能装作没有）
        self.ambiguous: list[str] = []

    def canon_population(self, name: str) -> str | None:
        """把一个人群名**收回到受控词表**；不在词表里返回 ``None``。

        受控词表是**封闭**的（决策 #33）：模型不能靠"我觉得这算个人群"把它撑大。
        ⑤ 兜底返回的 ``妊娠`` / ``育龄期女性`` / ``大环内酯类抗生素过敏``
        必须收回成 ``孕妇`` / ``育龄女性`` / ``大环内酯类药物过敏者``——
        否则同一份说明书的同一件事会在图里落成两个节点。
        """
        return self._pop_exact.get(normalize(name) or name)

    # -- 各级判定 ------------------------------------------------------
    def _exact(self, n: str) -> tuple[str, str] | None:
        if n in self.disease:
            return n, "疾病"
        if n in self._pop_exact:
            return self._pop_exact[n], "人群"
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
        best: tuple[str, str] | None = None
        for v, node, typ in self._contain_index:
            # 下限不是拍脑袋：去掉它就会冒出「发热 → 热」「拉普乳糖酶缺乏 → 拉」
            # 这种**一个字的巧合匹配**。要求匹配到的病名至少占原子一半、
            # 且不少于 3 个字——1~2 个字的名字不承载身份。
            if v not in n:
                continue
            if len(v) * 2 < len(n):
                continue
            if best is None or len(v) > len(best[0]):
                best = (v, node, typ)  # type: ignore[assignment]
        return (best[1], best[2]) if best else None

    def _similar(self, n: str) -> tuple[str, str, float] | None:
        """最长公共子串**贴着两边的头或尾**且占比 ≥ 0.8。"""
        best, best_s = "", 0.0
        self.ambiguous = []
        for v in sorted(self.disease):
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
                best, best_s, self.ambiguous = v, s, [v]
            elif s == best_s and s > 0:
                self.ambiguous.append(v)
        if best_s < 0.8:
            self.ambiguous = []
            return None
        return (best, "疾病", best_s)

    def align(self, atom: str) -> dict:
        n = normalize(atom)
        for level, fn in (("①精确", self._exact),
                          ("②包含", self._contain)):
            got = fn(n)
            if got:
                return {"node": got[0], "node_type": got[1], "level": level,
                        "normalized": n, "ambiguous": 0, "empty_norm": False}
        got = self._similar(n)
        if got:
            return {"node": got[0], "node_type": got[1], "level": "③前后缀",
                    "normalized": n, "ambiguous": len(self.ambiguous),
                    "empty_norm": False}
        return {"node": node_name(atom), "node_type": "疾病", "level": "④新建",
                "normalized": n, "ambiguous": 0, "empty_norm": not n}

    def top_candidates(self, n: str, k: int = 15) -> list[str]:
        """按最长公共子串占比取前 k 个词表名，供 ⑤ LLM 挑选。

        必须给候选：不给的话模型只会一路"新建"，⑤ 的价值就被埋没了。
        """
        scored = []
        for v in sorted(self.disease):
            if not v:
                continue
            lo, hi = min(len(v), len(n)), max(len(v), len(n))
            if hi > lo * 2:
                continue
            m = difflib.SequenceMatcher(None, n, v, autojunk=False)
            s = m.find_longest_match(0, len(n), 0, len(v)).size / hi
            if s > 0.3:
                scored.append((s, v))
        # 同分时按名字排，保证每次跑给模型的候选顺序一致（可复现）
        scored.sort(key=lambda t: (-t[0], t[1]))
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
    """金标准里**入图**的行（``node_type ∈ 疾病/人群``）。

    ⚠️ ``用药`` **不再是合法目标**（决策 #38 / 实测报告 §15）：`CONTRAINDICATED_FOR`
    的槽位是 ``Drug → Disease/Population``，"不得与 X 合用"这类**合并用药禁忌**
    该由 ``INTERACTS_WITH`` 承载——已实测 DDInter 覆盖得**更细**
    （`Ozanimod` 与 8 个 MAO 抑制剂个体药全为 Major，而说明书那句只是类别级表述）。

    所以这里**只认 疾病/人群**，并且**遇到别的类型就报出来**——
    免得以后有人往金标准里加了新类型的行，却被这里静默丢掉。
    """
    rows, _ = read_csv_rows(NODE_FILE)
    known = ("疾病", "人群", "仅文本", "-", "")
    odd = sorted({(r.get("node_type") or "").strip() for r in rows
                  if (r.get("verdict") or "").strip() == "保留"
                  and (r.get("node_type") or "").strip() not in known})
    if odd:
        print(f"⚠️ 金标准里有 {len(odd)} 种没见过的 node_type：{odd}")
        print("   它们**不会**被当成入图边——要么补进上面的 known，"
              "要么改金标准。别让它们静默消失。")
    return [r for r in rows
            if (r.get("verdict") or "").strip() == "保留"
            and (r.get("node_type") or "").strip() in ("疾病", "人群")]


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
                          encoding="utf-8", newline='\n')


def llm_align(al: Aligner, atom: str, cache: dict):
    """⑤：把原子 + 候选交给百炼判定。结果按原子文本缓存，重跑不再花钱。

    缓存里连 ``normalized`` 一起存：归一化规则一改，候选就变了，
    旧答案不能再当数——所以对不上就**重新调**，不让缓存悄悄带偏结果。
    """
    n = normalize(atom)
    hit = cache.get(atom)
    if hit and hit.get("normalized") == n:
        return hit

    sys.path.insert(0, str(REPO_ROOT / "backend"))
    from app.config import get_settings
    from openai import OpenAI

    st = get_settings()
    if not st.llm_configured:
        raise SystemExit("✗ 百炼未配齐，无法做 LLM 兜底")
    client = OpenAI(api_key=st.dashscope_api_key, base_url=st.dashscope_base_url)

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
           "normalized": n,
           "candidates": cands[:15]}
    cache[atom] = out
    return out


EVAL_JSON = REPO_ROOT / "data" / "extracted" / "alignment_eval.json"


def do_eval(use_llm: bool = False) -> int:
    al = Aligner()
    gold = load_gold_edges()
    cache = load_cache() if use_llm else {}
    n_gold = len(gold)
    print("L4 对齐器 · 在金标准上量命中率")
    print(f"  目标词表  疾病 {len(al.disease):,} · 人群 {len(al.population)}"
          f"（含变体 {len(al._pop_exact)} 种写法）")
    print(f"  金标准    {n_gold} 条入图边"
          f"（{len({r['drug'] for r in gold})} 药 / "
          f"{len({(r['drug'], r['node']) for r in gold})} 个不同的 (药,节点) 对）")
    print()

    lv_rule: dict[str, int] = {}
    lv_final: dict[str, int] = {}
    miss: list[tuple[str, str, str, str]] = []
    rule_hit = final_hit = 0
    llm_called = llm_fixed = llm_broke = 0
    ambiguous: list[tuple[str, list[str]]] = []
    broke: list[tuple[str, str, str]] = []
    per_atom: list[dict] = []

    for r in gold:
        atom, want = r["atom"], r["node"]
        rule = al.align(atom)
        if rule["level"] == "③前后缀" and rule.get("ambiguous", 0) > 1:
            ambiguous.append((atom, list(al.ambiguous)))
        # 比节点名时先归一化：**节点身份不该取决于一个连字符或"严重"二字**。
        # 实测踩过：`葡萄糖半乳糖吸收障碍` vs `葡萄糖-半乳糖吸收障碍`、
        # `骨髓功能受损` vs `骨髓功能严重受损` 都被判成错。
        rule_ok = canon(rule["node"]) == canon(want)
        rule_hit += rule_ok
        lv_rule[rule["level"] + ("✓" if rule_ok else "✗")] = \
            lv_rule.get(rule["level"] + ("✓" if rule_ok else "✗"), 0) + 1

        got = rule
        if use_llm and rule["level"] == "④新建":
            # 规则什么都没找到才交 LLM——这就是"兜底"的定义
            ll = llm_align(al, atom, cache)
            llm_called += 1
            if ll.get("node"):
                _node, _typ, _oov = canonical_population(
                    al, ll["node"], ll.get("node_type", "疾病"))
                got = {"node": _node, "node_type": _typ,
                       "level": f"⑤LLM({ll.get('action', '')})"
                                + ("(降级)" if _oov else ""),
                       "normalized": rule["normalized"]}
        ok = canon(got["node"]) == canon(want)
        final_hit += ok
        if use_llm and rule["level"] == "④新建":
            if ok and not rule_ok:
                llm_fixed += 1
            if rule_ok and not ok:
                llm_broke += 1
                broke.append((atom, rule["node"], got["node"]))
        lv = got["level"] + ("✓" if ok else "✗")
        lv_final[lv] = lv_final.get(lv, 0) + 1
        if not ok:
            miss.append((atom, want, got["node"], got["level"]))
        per_atom.append({"drug": r["drug"], "atom_idx": r["atom_idx"],
                         "atom": atom, "want": want,
                         "rule_node": rule["node"], "rule_level": rule["level"],
                         "rule_ok": rule_ok, "final_node": got["node"],
                         "final_level": got["level"], "final_ok": ok})

    if use_llm:
        print("  按级别（最终判定）：")
        for k in sorted(lv_final):
            print(f"    {k:<16} {lv_final[k]:>3}")
    else:
        print("  按级别：")
        for k in sorted(lv_rule):
            print(f"    {k:<16} {lv_rule[k]:>3}")

    print()
    print("  ── 对照 ──────────────────────────────────────────")
    print(f"  ① 规则单独（①②③④）   {rule_hit:>3}/{n_gold} = {rule_hit / n_gold:>6.1%}")
    if use_llm:
        print(f"  ② 规则 + ⑤ LLM 兜底    {final_hit:>3}/{n_gold} = "
              f"{final_hit / n_gold:>6.1%}")
        print(f"     ⑤ 调用 {llm_called} 条 · 救回 {llm_fixed} 条 · "
              f"弄坏 {llm_broke} 条（净 {final_hit - rule_hit:+d}）")
        if broke:
            print("     ⑤ 弄坏的（规则本来是对的）：")
            for atom, rn, ln in broke:
                print(f"       {atom[:24]:<26} 规则 {rn:<14} → ⑤ 改成 {ln}")
        save_cache(cache)
    else:
        print("  （加 --llm 得到 +⑤ LLM 兜底 的第二行）")

    if ambiguous:
        print()
        print(f"  ⚠️  ③前后缀 有 {len(ambiguous)} 条并列最高分——规则其实没依据挑，"
              f"只按确定性顺序取了第一个：")
        for atom, cands in ambiguous[:5]:
            print(f"     {atom[:20]:<22} 并列 {cands}")

    print()
    print(f"  仍不对 {len(miss)} 条")
    if miss:
        print()
        print("  ── 没对上的 ──")
        for atom, want, got, lv in miss:
            print(f"    {atom[:26]:<28} 期望 {want:<14} 实得 {got}（{lv}）")

    payload = {
        "gold_file": str(NODE_FILE.relative_to(REPO_ROOT)),
        "population_aliases": {k: list(v) for k, v in POPULATION_ALIASES.items()},
        "pop_contain_min": POP_CONTAIN_MIN,
        "n_gold": n_gold,
        "rule_only": {"hit": rule_hit, "acc": round(rule_hit / n_gold, 4),
                      "levels": dict(sorted(lv_rule.items()))},
        "rule_plus_llm": ({"hit": final_hit,
                           "acc": round(final_hit / n_gold, 4),
                           "levels": dict(sorted(lv_final.items())),
                           "llm_called": llm_called, "llm_fixed": llm_fixed,
                           "llm_broke": llm_broke,
                           "broke": [{"atom": a, "rule": r, "llm": l}
                                     for a, r, l in broke]}
                          if use_llm else None),
        "ambiguous_similarity": len(ambiguous),
        "misses": [{"drug": a, "want": w, "got": g, "level": l}
                   for a, w, g, l in miss],
        "per_atom": per_atom,
    }
    EVAL_JSON.parent.mkdir(parents=True, exist_ok=True)
    if use_llm:
        EVAL_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                             encoding="utf-8", newline='\n')
        print()
        print(f"  已写 {EVAL_JSON.relative_to(REPO_ROOT)}")
    else:
        print()
        print(f"  （只量了规则，没写 {EVAL_JSON.name}——"
              f"那份文件要同时装两个数，由 --llm 那次写全）")
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
