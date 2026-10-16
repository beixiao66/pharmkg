#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L5 · Text2Cypher 的提示词、Cypher 抽取与安全校验（共用模块）
==========================================================

这个模块被两处 import：

* :mod:`build_l5_fewshot`——生成 few-shot 候选（决策 #26：LLM 生成候选 → 筛选固化）
* :mod:`l5_text2cypher`——真正的评测

**共用是有意的**：few-shot 示例必须与评测时**同一种提示词形状**，
否则示例是"另一种任务"的示范，不但没用还可能误导。

⚠️ 这里的 schema **不是照 §5 抄的**。§5 那张表曾经把 `Drug` 的属性写成
`id, name_cn, name_en, aliases[], atc_code`——而 `name_cn` / `aliases` / `atc_code`
**在图里根本不存在**（实测报告 §17）。照它写提示词，生成的每条 Cypher 都会写成
`d.name_cn` → 全 null，而现象看起来像"图谱里没有数据"。
**这份 schema 由 `scripts/check_schema_doc.py` 对图核过**，改这里之前先跑它。
"""

from __future__ import annotations

import re

#: **输出契约**——每个意图该返回什么。
#:
#: 为什么必须有它：第一版没有，模型根本不知道该 RETURN 什么。实测它对着
#: "一氧化氮和丙胺卡因能一起用吗？" 写出了
#: ``RETURN d1.name AS drug1, d2.name AS drug2, d1.name AS drug1_cn, …``
#: ——**无限重复地给同一列起别名，一直写到被 max_tokens 截断**。
#: 那是退化循环（degenerate repetition），不是"格式没写好"。
#:
#: 试过的无效手段：``presence_penalty=1.0``、``frequency_penalty=1.0`` **都没用**。
#: 真正有效的是**在提示词里明确禁止**（输出从 200+ token 掉到 90、自然结束）。
#: 另外 ``max_tokens`` 是必须的硬闸——不设的话这个循环会一直写到超时。
#:
#: ⚠️ **禁忌只取 `confidence='high'`** 这条是 **2026-10-16 用户拍板补的**。
#: 起因：评测里 Q15/Q16 模型返回了全部 10 条禁忌，而金标准只取 8/5 条——
#: 金标准悄悄加了一条**问题里没说**的过滤，等于在考"模型能不能猜到我未言明的策略"。
#: **那是坏题，不是难模型。** 所以把策略挪到**明说的接口契约**里。
#: 保留这个策略本身是有理由的：L4 抽出来的 `CONTRAINDICATED_FOR` 实测约 **33% 假阳性**
#: （实测报告 §11），对着用户展示未加区分的边是不负责任的。
#:
#: 注意这是**接口契约**（返回哪些值 + 什么范围），不是答案本身：
#: **没有**告诉模型怎么写 MATCH、也没有告诉它答案是什么。

OUTPUT_CONTRACT = """\
输出要求（按意图返回什么）
  相互作用 → 返回相互作用的**级别**（关系属性 level）
  疾病用药 → 返回**药物名**
  适应症   → 返回**疾病名**
  禁忌     → 返回**禁忌对象的名字**，并且**只返回高置信度的边**：
             在 Cypher 里加 ``WHERE r.confidence = 'high'``
             （关系必须绑成变量 ``-[r:CONTRAINDICATED_FOR]->`` 才能这么过滤）
  药品信息 → 返回**所问的那个属性**（如英文名 name_en）

⚠️ RETURN 里只放**简单的列**：变量本身（``d.name``）或它的属性（``r.level``）。

⚠️ **关系的属性必须先绑成变量**：在 MATCH 里写成 ``-[r:REL]-``，
   再用 ``r.level`` 取。**不能**把关系模式直接塞进 RETURN——
   ``RETURN a-[:REL]-b AS x`` 是语法错误（实测模型就这么写了，直接报 SyntaxError）。

⚠️ 同一列**只写一次**，不要给它起多个别名，也不要重复 RETURN 同一个表达式——
   那样会写出无限长的 RETURN（实测过：一直写到被 max_tokens 截断）。
"""

#: 提示词里注入的 schema。**只写 L5 的六类意图真的会用到的部分**——
#: 把 13 类关系全塞进去只会稀释注意力，还容易诱导模型去查无关的关系。
SCHEMA_TEXT = """\
节点（属性名就是 name，**没有 name_cn**）
  (:Drug       {name, name_en})
  (:Disease    {name})
  (:Symptom    {name})
  (:Population {name})          人群，如 孕妇 / 哺乳期女性 / 肝功能不全

关系（⚠️ 方向很重要，写反了查不到）
  (:Disease)-[:RECOMMEND_DRUG]->(:Drug)              疾病 → 推荐用药
  (:Disease)-[:COMMON_DRUG]->(:Drug)                 疾病 → 常用药
  (:Disease)-[:HAS_SYMPTOM]->(:Symptom)              疾病 → 症状
  (:Drug)-[:INTERACTS_WITH]-(:Drug)                  药物相互作用（无向）
        属性 level: 'Major' / 'Moderate' / 'Minor' / 'Unknown'
  (:Drug)-[:CONTRAINDICATED_FOR]->(:Disease|:Population)
        属性 confidence: 'high' / 'medium'，evidence: 说明书原句

注意
  · "这药治什么"要**反向**走 RECOMMEND_DRUG：MATCH (dz:Disease)-[:RECOMMEND_DRUG]->(d:Drug {name:'X'})
  · 药名必须与给定实体**完全一致**（用 {name:'X'}，不要用 CONTAINS 去模糊匹配）
  · 不要加 LIMIT（用户没要求条数时，要返回全部）
  · 只允许 MATCH / OPTIONAL MATCH / WITH / WHERE / RETURN / ORDER BY，
    禁止任何写操作
"""

#: 禁止出现的关键字（写操作与过程调用）。
FORBIDDEN = (
    "CREATE", "MERGE", "DELETE", "DETACH", "SET", "REMOVE", "DROP",
    "CALL", "LOAD", "FOREACH", "USING", "APOC", "GRANT", "DENY",
)

#: Cypher 允许出现的关键字白名单（用于"不是 Cypher 就拒掉"）。
ALLOWED_KEYWORDS = (
    "MATCH", "OPTIONAL", "WITH", "WHERE", "RETURN", "ORDER", "BY", "ASC",
    "DESC", "AS", "DISTINCT", "AND", "OR", "NOT", "IN", "IS", "NULL",
    "COUNT", "COLLECT", "LIMIT", "UNWIND", "STARTS", "ENDS", "CONTAINS",
    "TRUE", "FALSE",
)

MAX_CYPHER_CHARS = 2000


def build_messages(question: str, intent: str, entities: list[str],
                   fewshot: list[dict] | None = None) -> list[dict]:
    """拼出送给 LLM 的 messages。

    **实体是"给定"的**——这是刻意的分层（实体链接已单独量过）。
    所以提示词里明确说"实体已给定、必须原样使用"，把变量只剩"会不会写 Cypher"。

    few-shot 示例以**同样的三段式**（意图 / 实体 / Cypher）呈现，
    而不是把示例塞成一段对话——形状一致才好模仿。
    """
    lines = [
        "你是把中文医药问题翻译成 Neo4j Cypher 的助手。",
        "",
        "图谱 schema：",
        SCHEMA_TEXT,
        "",
        OUTPUT_CONTRACT,
    ]
    if fewshot:
        lines += ["", "示例（照这个形状写）："]
        for ex in fewshot:
            lines += [
                "",
                f"意图：{ex['intent']}",
                f"实体：{'、'.join(ex['entities'])}",
                f"问题：{ex['question']}",
                "Cypher：",
                "```cypher",
                ex["cypher"].strip(),
                "```",
            ]
    lines += [
        "",
        "── 现在请处理这一条 ──",
        f"意图：{intent}",
        f"实体（已给定，必须原样使用）：{'、'.join(entities)}",
        f"问题：{question}",
        "",
        "只输出一个 ```cypher 代码块，里面是可直接执行的 Cypher。不要输出别的解释。",
    ]
    return [{"role": "user", "content": "\n".join(lines)}]


def extract_cypher(text: str) -> str:
    """从模型回复里取出 Cypher。

    优先取 ```` ```cypher ```` 代码块；没有就取任意代码块；
    再没有就把整段当 Cypher（有些模型会直接给一行）。
    """
    if not text:
        return ""
    m = re.search(r"```(?:cypher|Cypher|CYPHER|sql)?\s*\n(.*?)```", text, re.S)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\s*(.*?)```", text, re.S)
    if m:
        return m.group(1).strip()
    return text.strip()


def strip_string_literals(cypher: str) -> str:
    """去掉字符串字面量，再做关键字扫描。

    为什么必须先去字面量：药名或证据句里完全可能出现 ``SET`` / ``DELETE``
    这样的子串（中文药名少见，但证据句是整句中文，边界情况真实存在）。
    直接在原文上扫关键字会**误杀正确的查询**——而"误杀"和"放过"一样糟。
    """
    out, i, n = [], 0, len(cypher)
    while i < n:
        ch = cypher[i]
        if ch in "'\"":
            quote = ch
            i += 1
            while i < n:
                if cypher[i] == "\\":
                    i += 2
                    continue
                if cypher[i] == quote:
                    i += 1
                    break
                i += 1
            out.append("''")            # 用一个空字面量占位，保持结构
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def validate_cypher(cypher: str) -> tuple[bool, str]:
    """白名单校验（§6.1 第 ③ 步要求的"仅允许 MATCH/RETURN，禁止写操作"）。

    返回 ``(是否通过, 原因)``。**拒绝的理由要说清楚**——评测时要能区分
    "模型写错了"和"被我的校验误杀"。
    """
    if not cypher or not cypher.strip():
        return False, "空的 Cypher"

    if len(cypher) > MAX_CYPHER_CHARS:
        return False, f"太长（{len(cypher)} > {MAX_CYPHER_CHARS} 字符）"

    code = strip_string_literals(cypher)
    upper = code.upper()

    # 去掉注释行再扫——注释里出现 CREATE 不该拦
    code_nc = "\n".join(ln for ln in upper.split("\n")
                        if not ln.strip().startswith("//"))

    for kw in FORBIDDEN:
        if re.search(rf"\b{kw}\b", code_nc):
            return False, f"含禁止的关键字 {kw}（只读白名单）"

    if not re.search(r"\bMATCH\b", code_nc):
        return False, "没有 MATCH"
    if not re.search(r"\bRETURN\b", code_nc):
        return False, "没有 RETURN"

    # 不是 Cypher 的散文：没有任何大括号/括号/关系符号
    if not re.search(r"[\(\)\[\]{}:\-]", code):
        return False, "看起来不是 Cypher（连括号和冒号都没有）"
    return True, "ok"


def looks_like_clarification(text: str) -> bool:
    """模型是不是在**要求澄清**而不是给 Cypher。

    §13.3 允许（也要求）在实体有歧义时反问。所以评测里"没给 Cypher 但说明
    需要澄清"应当与"写不出来"区分开——这两件事的性质完全不同。
    """
    cypher = extract_cypher(text)
    if cypher and validate_cypher(cypher)[0]:
        return False
    keys = ("澄清", "确认", "是指", "哪一个", "哪种", "请说明", "歧义")
    return any(k in text for k in keys)
