#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L4 · 抽取产物 → 待入库的边（**评分与入库共用的一段代码**）
==========================================================

为什么单独抽一个模块
--------------------
L4 有两件事要读同一批产物、做同一件"原子 → 节点"的事：

* ``score_alignment.py`` 要**量**它（第二层 P/R/F1）
* ``import_insert_edges.py`` 要**写**它（`CONTRAINDICATED_FOR` 入图）

如果两边各写一份，迟早会漂移——而**漂移的后果是"入库的边不是评分的边"**，
那么报告里那个 F1 就管不住图里的数据。所以这一段只写一次，两边都调它。

数据流
------
    data/extracted/<method>/items/<药名>.json    ← 结构化条目（带 generic_only）
        ↓  按 、，； 切原子（保留原文措辞）
    原子
        ↓  Aligner.align（规则四级；④新建 的再走 ⑤ LLM 兜底）
    (药, 原子, 节点, 节点类型, 判定级别, generic_only)
        ↑
    ``extract_edges()`` 返回的就是这个列表——**两边看到的是同一批记录**

⚠️ 自由文本产物（朴素基线）没有 ``generic_only``，一律记 ``None``。
把它当成 ``False`` 会把"没有这个字段"混进"模型判它该进图"的账里。
"""

from __future__ import annotations

from pathlib import Path

from align_nodes import canonical_population, canon, llm_align
from score_extraction import MIN_ATOM, item_path, load_output_items, norm, split_atoms_raw

#: 本节 → 图谱关系。决策 #30：只抽【禁忌】→ ``CONTRAINDICATED_FOR``；
#: 【注意事项】+【儿童用药】→ ``CAUTION_FOR``（那两节**还没有金标准**，先不做）。
SECTION_RELATION = {
    "禁忌": "CONTRAINDICATED_FOR",
    "注意事项": "CAUTION_FOR",
    "儿童用药": "CAUTION_FOR",
}

#: 判定级别 → 置信档（沿用决策 #28 对药名五级匹配的分档思路：
#: 精确/包含算"确定"，相似/模型判定算"待确认"，新建算"低"）。
#: ⚠️ 这是**级别到档位的固定映射**，不是拿评测集调出来的阈值。
LEVEL_CONFIDENCE = {
    "①精确": "high",
    "②包含": "high",
    "③前后缀": "medium",
    "⑤LLM": "medium",
    "④新建": "low",
    "⑤LLM(new)": "low",
}


def raw_atoms_of(text: str) -> list[str]:
    """把一行条目切成原子，**保留原文措辞**（对齐器需要原文，不要先归一化）。

    过滤口径与 :func:`score_extraction.atoms` 一致：归一化后短于 ``MIN_ATOM`` 的丢掉。
    """
    return [p for p in split_atoms_raw(text) if len(norm(p)) >= MIN_ATOM]


def confidence_of(level: str) -> str:
    """把判定级别映射成置信档。认不出的级别按 ``low`` 处理（不假装它可信）。"""
    if level in LEVEL_CONFIDENCE:
        return LEVEL_CONFIDENCE[level]
    if level.startswith("⑤LLM(existing"):
        return "medium"
    return "low"


def load_items_for(base: Path, drug: str, *, items_dir: Path | None = None,
                   section: str | None = None) -> list[dict]:
    """取一个药的条目。给了 ``items_dir`` 就读结构化 JSON，否则读 ``base`` 下的自由文本。

    结构化产物的文件名有两种约定（与 ``extract_sections.unit_key`` 一致）：

    * 只跑一节（如【禁忌】）→ ``<药名>.json``
    * 跑多节（如 CAUTION_FOR 那两节）→ ``<药名>__<节名>.json``

    所以按 ``<药名>__<节名>`` → ``<药名>`` 的顺序试，**取第一个存在的**。
    """
    if items_dir:
        cands = []
        if section:
            cands.append(items_dir / f"{drug}__{section}.json")
        cands.append(items_dir / f"{drug}.json")
        for p in cands:
            if p.exists():
                return load_output_items(p)
        return []
    return load_output_items(item_path(base, drug))


def extract_edges(drugs, *, base: Path, items_dir: Path | None = None,
                  al, section: str = "禁忌", use_llm: bool = False,
                  cache: dict | None = None) -> list[dict]:
    """把若干药的抽取产物对齐成**待入库的边**。

    返回**逐原子**的记录（同一条边可能有多条记录——那正是"同一条边的多个证据"）：

    ``{drug, section, atom, node, node_key, node_type, level, confidence,
       generic_only, out_of_vocab_population}``

    记录顺序是确定的（药名排序 → 条目顺序 → 原子顺序），所以两边、多次跑都一致。

    ⚠️ **人群词表是封闭的**（决策 #33）。⑤ LLM 兜底返回 ``node_type=人群`` 时，
    先把它收回受控词表（``妊娠`` → ``孕妇``）；**收不回去的一律按"疾病"处理**，
    并打上 ``out_of_vocab_population=True``。理由：⑤ 实测会把
    ``体内有心脏起搏器``、``单胺氧化酶抑制剂使用期``、``特殊人群`` 这类
    **状态/用药情形**当成人群——让模型这样一点点撑大我自己定义的受控词表，
    比把它降级成疾病节点危险得多。这件事要**点名报出来**，不能替它掩盖。
    """
    out: list[dict] = []
    for drug in sorted(drugs):
        for it in load_items_for(base, drug, items_dir=items_dir, section=section):
            for atom in raw_atoms_of(it["text"]):
                got = al.align(atom)
                if use_llm and got["level"] == "④新建":
                    ll = llm_align(al, atom, cache if cache is not None else {})
                    if ll.get("node"):
                        got = {"node": ll["node"],
                               "node_type": ll.get("node_type", "疾病"),
                               "level": "⑤LLM"}
                node_type = got["node_type"]
                node = got["node"]
                # 人群词表封闭：收不回受控词表的一律降级成疾病（理由见上）
                node, node_type, oov = canonical_population(al, node, node_type)
                out.append({
                    "drug": drug,
                    "section": section,
                    "atom": atom,
                    "node": node,
                    "node_key": canon(node),
                    "node_type": node_type,
                    "level": got["level"],
                    "confidence": confidence_of(got["level"]),
                    "generic_only": it["generic_only"],
                    "out_of_vocab_population": oov,
                })
    return out


def group_edges(records: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """按 ``(药, 节点规范名)`` 归并——这就是第二层评分与入库的单位。"""
    grouped: dict[tuple[str, str], list[dict]] = {}
    for rec in records:
        grouped.setdefault((rec["drug"], rec["node_key"]), []).append(rec)
    return grouped
