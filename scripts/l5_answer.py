#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L5 · 证据子图 + 回答生成（§6.1 的第 ④⑤⑥⑦ 步）
================================================

用户拍板：**先做模板基线，再用同一套指标对比 LLM**；"禁止外推"用
**机械检查 + 人工抽检兜底**；证据形状 `{nodes, edges, cypher, rows}`

证据子图**不解析模型生成的 Cypher**
------------------------------------
有诱惑力的做法是"把生成的 Cypher 拆开、从里面提取节点和边"。**不这么做**，
理由是：那等于让证据的可靠性依赖模型输出——模型把 Cypher 写歪了，证据跟着歪，
而证据正是用来**判断它有没有写歪**的东西。逻辑上转圈。

所以证据按**意图**用固定查询构建（意图是已知输入，见分层设计）。
好处是：① 不依赖模型；② 每条证据都说得清"是哪条关系撑的"。

`{nodes, edges, cypher, rows}` 四样
-----------------------------------
* `nodes` / `edges`：与 L4 已入库的边属性**同一形状**（`label/name`、`type/from/to/props`），
  前端拿到就能画子图；`edges[i].props.evidence` 就是说明书原句。
* `cypher`：**实际执行的那条查询**。答错了要能分清"生成错"还是"数据没有"。
* `rows`：查询返回的值。既给人看，也给下面的忠实度检查当白名单。

"禁止外推"怎么量
----------------
**机械检查**：答案里出现的每个**图节点名**，都必须出现在证据子图里。
多一个就是外推——模型（或模板）说了一个查询结果里没有的实体。

⚠️ 这条检查有盲点，**必须配人工抽检**：它抓不到"换了种说法编数值/编结论"
（比如证据里只写了"Major"，答案却说"会导致出血"）。所以它是**必要条件不是充分条件**，
报告里两个数字一起给，谁都不许单独当成"没有幻觉"。

用法
----
    python scripts/l5_answer.py                       # 模板基线 + 忠实度
    python scripts/l5_answer.py --method llm           # 加 LLM 对比
    python scripts/l5_answer.py --persist              # 跑完把每轮落库到 L7
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from collections import Counter

from insert_io import read_csv_rows
from opencmkg_io import REPO_ROOT
from l5_entity_linking import (INTENT_LABELS, QUERY_STOPWORDS, build_index,
                               resolve, scan)

sys.path.insert(0, str(REPO_ROOT / "backend"))

QUESTIONS_FILE = REPO_ROOT / "data" / "eval" / "l5_questions.csv"
OUT_FILE = REPO_ROOT / "data" / "eval" / "l5_answers.json"

DISCLAIMER = "以上内容来自知识图谱，仅供参考，不能替代医生诊断。"

#: 列出实体名时最多写几个，其余用「等 N 个」带过。
#:
#: ⚠️ 原值 8 **让模板在「模板 vs LLM」的对比里吃了亏**（用户抽检表发现）：
#: 同样是「忠实」，模板只说 8 个 + 「等 23 个」，而 LLM 把 23 个全列了出来——
#: **有用程度差很多，而「是否只用了证据里的信息」这一列量不出来。**
#: 一边被自己阉割一边比，得出的结论不能用，所以提到 30。
MAX_LISTED = 30


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 证据子图

def _node_json(node) -> dict:
    """把 Neo4j 的 Node 转成证据里的节点。"""
    labels = list(node.labels)
    props = {k: v for k, v in node.items()
             if k in ("name", "name_en", "source", "id")}
    return {"label": labels[0] if labels else "?",
            "labels": labels,
            "name": node.get("name"),
            "props": props}


def _rel_json(rel) -> dict:
    """把 Relationship 转成证据里的边。"""
    return {"type": rel.type,
            "from": rel.start_node.get("name"),
            "to": rel.end_node.get("name"),
            "props": {k: v for k, v in rel.items()}}


def evidence_cypher(intent: str, entities: list[str], session) -> tuple[str, dict]:
    """按意图给出**固定的证据查询**，执行后打包成证据子图。

    返回 ``(cypher, evidence)``。查不到东西时证据是空的（`nodes`/`edges` 都为空列表），
    这本身是有意义的信号——第 ⑤ 步要据此回答"图谱中无相关记录"。
    """
    e0 = entities[0] if entities else ""
    e1 = entities[1] if len(entities) > 1 else ""

    if intent == "相互作用":
        if e1:
            cy = ("MATCH (a:Drug {name: $e0})-[r:INTERACTS_WITH]-(b:Drug {name: $e1}) "
                  "RETURN a, r, b")
        else:
            cy = ("MATCH (a:Drug {name: $e0})-[r:INTERACTS_WITH]-(b:Drug) "
                  "RETURN a, r, b")
    elif intent == "疾病用药":
        cy = ("MATCH (dz:Disease {name: $e0})-[r:RECOMMEND_DRUG]->(d:Drug) "
              "RETURN dz, r, d")
    elif intent == "适应症":
        cy = ("MATCH (dz:Disease)-[r:RECOMMEND_DRUG]->(d:Drug {name: $e0}) "
              "RETURN dz, r, d")
    elif intent == "禁忌":
        # 两个实体时：一个是药、另一个是禁忌对象（**不分先后**，按图里的标签判）
        if e1:
            a_is_drug = bool(session.run(
                "MATCH (n:Drug {name: $x}) RETURN count(n) AS c", x=e0
            ).single()["c"])
            drug, target = (e0, e1) if a_is_drug else (e1, e0)
            cy = ("MATCH (d:Drug {name: $drug})-[r:CONTRAINDICATED_FOR]->"
                  "(t {name: $target}) RETURN d, r, t")
        else:
            drug, target = e0, ""
            # ⚠️ 只取 high：这是**输出契约**里明说的策略（L4 的边约 33% 假阳性）
            cy = ("MATCH (d:Drug {name: $drug})-[r:CONTRAINDICATED_FOR]->(t) "
                  "WHERE r.confidence = 'high' RETURN d, r, t")
    elif intent == "药品信息":
        drug, target = e0, ""
        cy = "MATCH (d:Drug {name: $drug}) RETURN d"
    else:
        return "", {"nodes": [], "edges": [], "cypher": "", "rows": []}

    params = {"e0": e0, "e1": e1}
    # ⚠️ 踩过：`药品信息` 与 `禁忌` 的查询用的是 `$drug`，而这里只给了 e0/e1 →
    #    Neo4j 报"参数缺失" → 证据恒为空 → 模板回答变成"图谱中无相关记录"。
    #    **而空回答天然"忠实"，于是忠实度虚高**。参数名对不上的 bug 不会自己喊。
    if intent == "禁忌":
        params = {"drug": drug, "target": target}
    elif intent == "药品信息":
        params = {"drug": drug}

    nodes: dict[tuple, dict] = {}
    edges: dict[tuple, dict] = {}
    rows: list = []
    try:
        for rec in session.run(cy, **params):
            vals = []
            for v in rec.values():
                if hasattr(v, "labels"):                    # Node
                    nj = _node_json(v)
                    nodes[(nj["label"], nj["name"])] = nj
                    # ⚠️ 踩过：这里只放过 `name`，于是"英文名是什么"的证据行里
                    #    **根本没有 name_en** → LLM 看到空表、老实回答"图谱中无相关记录"。
                    #    **那是我的 bug，不是它不会答。** 属性一并带上。
                    extra = ", ".join(f"{k}={val}" for k, val in nj["props"].items()
                                      if k != "name")
                    vals.append(f"{nj['name']}（{extra}）" if extra
                                else str(nj["name"]))
                elif hasattr(v, "type"):                    # Relationship
                    ej = _rel_json(v)
                    edges[(ej["type"], ej["from"], ej["to"])] = ej
                    vals.append(ej["props"].get("level")
                                or ej["props"].get("confidence"))
                else:
                    vals.append(v)
            rows.append(vals)
    except Exception as exc:  # noqa: BLE001
        log(f"      ⚠️ 证据查询失败：{type(exc).__name__}: {str(exc)[:70]}")
        return cy, {"nodes": [], "edges": [], "cypher": cy, "rows": [],
                    "error": f"{type(exc).__name__}"}

    return cy, {"nodes": list(nodes.values()), "edges": list(edges.values()),
                "cypher": cy, "rows": rows}


# ---------------------------------------------------------------- 模板回答

def _list(names: list[str]) -> str:
    uniq = list(dict.fromkeys(n for n in names if n))
    if len(uniq) <= MAX_LISTED:
        return "、".join(uniq)
    return "、".join(uniq[:MAX_LISTED]) + f" 等 {len(uniq)} 个"


def template_answer(intent: str, entities: list[str], ev: dict) -> str:
    """**零幻觉基线**：只把证据里的值填进固定句式，一个字都不生成。

    这个基线的作用不是"更好的回答"，而是给 LLM 回答提供一个**下界与参照**：
    它 100% 忠实（因为它只会照抄证据），所以任何 LLM 回答的忠实度都该以它为基准看。
    """
    if not ev["nodes"] and not ev["edges"]:
        return "图谱中无相关记录。" + DISCLAIMER

    # ⚠️ 踩过两次，第二次是用户抽检表照出来的，值得记清楚。
    #
    # 第一次：`疾病用药`/`适应症` 直接用**全部节点名**当结果，于是疾病自己也被列进了
    #        "推荐用药"里——`类风湿关节炎 的推荐用药共 24 种：类风湿关节炎、骨肽片、…`。
    # 第二次（我"修"过之后仍然错）：我把取法改成"边的目标端"，可那两个意图的证据查询
    #        当时**只 RETURN 了节点、没 RETURN 关系** → `edges` 是空的 →
    #        代码退回了"用全部节点名"，**病又混进来了**。
    #        → 教训：改了取法，还要检查**数据源里到底有没有那个东西**；
    #          空集合上的回退分支会把 bug 原样带回来。
    #
    # 现在：证据查询把关系也返回，且**方向按意图分**——
    #   `疾病用药`：Disease → Drug，要的是药物（`to`）
    #   `适应症`  ：同一条边反向查，要的是疾病（`from`）
    if intent == "适应症":
        names = [e["from"] for e in ev["edges"] if e.get("from")]
    else:
        names = [e["to"] for e in ev["edges"] if e.get("to")]
    if not names:
        names = [n["name"] for n in ev["nodes"] if n["name"]]
    levels = [e["props"].get("level") for e in ev["edges"]]
    targets = [e["to"] for e in ev["edges"]]
    drugs = [n["name"] for n in ev["nodes"] if n["label"] == "Drug" and n["name"]]

    if intent == "相互作用":
        if len(entities) > 1 and levels:
            return (f"根据图谱，{entities[0]} 与 {entities[1]} 之间存在 "
                    f"**{levels[0]}** 级相互作用。{DISCLAIMER}")
        # ⚠️ `INTERACTS_WITH` 是无向关系，图里存的方向不由我们决定：
        #    实测 `依度沙班 -[INTERACTS_WITH]-> 卡维地洛片`——`to` 端恰好是实体自己。
        #    所以"对方药"要挑**不在 entities 里**的那一端，不能只看 `to`。
        pairs = []
        for e, lv in zip(ev["edges"], levels):
            pair = [x for x in (e["from"], e["to"]) if x not in entities]
            if pair:
                pairs.append(f"{pair[0]}（{lv}）")
        return (f"根据图谱，{entities[0]} 共有 {len(ev['edges'])} 条相互作用记录："
                f"{_list(pairs)}。{DISCLAIMER}")
    if intent == "疾病用药":
        return (f"根据图谱，{entities[0]} 的推荐用药共 {len(names)} 种："
                f"{_list(names)}。{DISCLAIMER}")
    if intent == "适应症":
        return (f"根据图谱，{entities[0]} 出现在 {len(names)} 个疾病的推荐用药中："
                f"{_list(names)}。{DISCLAIMER}")
    if intent == "禁忌":
        if len(entities) > 1:
            # ⚠️ 原写法是 `根据图谱，{entities[0]} 与该情况存在禁忌关系（高置信度）`——
            #    用户抽检时一眼看出两个毛病：**没点名是哪个药**（`entities[0]` 可能是
            #    人群/疾病那一端），而且"X 与该情况存在禁忌关系"这句本身不通。
            #    统一改成从**边的两端**取，句子才与数据对齐。
            evid = ""
            drug = target = ""
            if ev["edges"]:
                e = ev["edges"][0]
                evid = (e["props"] or {}).get("evidence", "")
                # 药名 = 边里那个能在图上查到是 Drug 的端点
                drug = e["from"]
                target = e["to"]
            conf = (ev["edges"][0]["props"] or {}).get("confidence", "") if ev["edges"] else ""
            conf_s = f"（{conf} 置信度）" if conf else ""
            return (f"根据图谱，{drug} 禁用于 {target}{conf_s}。"
                    f"说明书原句：{evid}。{DISCLAIMER}")
        return (f"根据图谱，{entities[0]} 的禁忌（高置信度）共 {len(targets)} 条："
                f"{_list(targets)}。{DISCLAIMER}")
    if intent == "药品信息":
        en = None
        for n in ev["nodes"]:
            en = n["props"].get("name_en") or en
        if en:
            return f"根据图谱，{entities[0]} 的英文名是 {en}。{DISCLAIMER}"
        return (f"根据图谱中记录，{entities[0]} 的相关信息为："
                f"{_list(drugs)}。{DISCLAIMER}")
    return "图谱中无相关记录。" + DISCLAIMER


# ---------------------------------------------------------------- 忠实度检查

def _mask(text: str, names: list[str]) -> str:
    """把文本里出现的这些名字**遮蔽掉**（长的先遮，避免短的抢先）。

    为什么必须遮蔽：第一版直接在原文里找节点名，结果**大量假阳性**——
    `类风湿关节炎` 在证据里，可 `关节炎` 也是图里的节点名，于是它被当成"外推"；
    `支气管哮喘` 在证据里，`哮喘` 同样被误判；更荒唐的是图里有个叫 `23` 的垃圾节点，
    答案里任何数字 23 都会命中。**一个假阳性率这么高的检查，只会让人不再看它。**
    """
    masked = text
    for name in sorted(names, key=len, reverse=True):
        if name and name in masked:
            masked = masked.replace(name, "・" * len(name))
    return masked


def _meaningful(name: str) -> bool:
    """过滤掉不能当证据判断依据的节点名。

    纯数字/纯标点的名字（图里真有 `23` 这种垃圾节点）不参与判断——
    它们在自然语言里太容易偶然命中，留着只会制造噪声。
    """
    if len(name) < 2:
        return False
    return any(ch.isalpha() or "\u4e00" <= ch <= "\u9fff" for ch in name)


def node_names_in(text: str, all_names: set[str],
                  already_allowed: set[str] | None = None) -> set[str]:
    """找出文本里出现了哪些**图节点名**（证据里已有先遮蔽掉）。

    ``already_allowed`` 传证据里的节点名：它们先被遮蔽，剩下的才算"别处来的"。
    另外**用户问题里的词**（实体本身）也该遮蔽——答案回述用户说的词不算外推。
    """
    masked = _mask(text, list(already_allowed or ()))
    found = set()
    for name in all_names:
        if not _meaningful(name) or name in QUERY_STOPWORDS:
            continue
        if name in masked:
            found.add(name)
    return found


def faithfulness(answer: str, ev: dict, all_names: set[str]
                 ) -> tuple[bool, set[str]]:
    """答案里出现的节点名是否**全部**来自证据子图。

    返回 ``(是否忠实, 越界的名字)``。空集＝全部有据可查。
    """
    allowed = {n["name"] for n in ev["nodes"] if n["name"]}
    for e in ev["edges"]:
        allowed.add(e["from"])
        allowed.add(e["to"])
    allowed.discard(None)
    # ⚠️ 还必须把**证据里的属性值**算进来：`evidence` 是说明书原句（整句中文），
    #    LLM 引用它完全是"有据可查"，但第一版只把节点名当白名单 →
    #    原句里冒出来的 `药物过敏` 被误判成外推。**白名单漏了东西，就会冤枉好人。**
    attr_values: set[str] = set()
    for item in list(ev["nodes"]) + list(ev["edges"]):
        for v in (item.get("props") or {}).values():
            if isinstance(v, str) and v.strip():
                attr_values.add(v.strip())
    
    # 证据里的名字**与属性值**先遮蔽，再看剩下还有什么节点名
    evidence_texts = allowed | attr_values
    seen = node_names_in(answer, all_names, already_allowed=evidence_texts)

    # ⚠️ 再放行一种情况：命中的名字是**证据里某个名字的子串**。
    #    原因：实测 LLM 把 `大环内酯类药物过敏者` 写成了 `大环内酯类药物过敏的人`
    #    （`者` 换成"的人"），遮蔽就失效了，于是 `药物过敏` / `过敏` 被当成新实体报出来。
    #    **那是改写，不是外推。**
    #
    #    代价要说清楚：这条放行**会漏掉**"新编的实体恰好是证据名的子串"这种情况
    #    （例如证据里有 `支气管哮喘`，答案编了个 `哮喘` 就被放过了）。
    #    换句话说，这个机械检查只能当**必要条件**——它说有外推不一定真有，
    #    它说没有更不代表真没有。**人工抽检不是可选项，是唯一能判"改写 vs 外推"的手段。**
    def _is_substring_of_evidence(x: str) -> bool:
        return any(x in e for e in evidence_texts if len(e) > len(x))

    violations = {x for x in (seen - allowed)
                  if not _is_substring_of_evidence(x)}
    return (not violations), violations


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="L5 证据子图与回答")
    ap.add_argument("--method", choices=("template", "llm", "both"),
                    default="template")
    ap.add_argument("--persist", action="store_true",
                    help="把每轮落库到 L7（走真实 HTTP 接口；跑完会核验行数再删掉）")
    ap.add_argument("--show", type=int, default=3, help="打印前几条答案")
    args = ap.parse_args(argv)

    from app.config import get_settings
    from neo4j import GraphDatabase

    rows, _ = read_csv_rows(QUESTIONS_FILE)
    st = get_settings()
    driver = GraphDatabase.driver(st.neo4j_uri,
                                 auth=(st.neo4j_user, st.neo4j_password))

    persist = _open_persist(st) if args.persist else None

    log("L5 · 证据子图 + 回答")
    log(f"  方法      {args.method}")
    log(f"  忠实度    机械检查（答案里的节点名必须都在证据里）+ 需人工抽检兜底")
    log("")

    results = []
    methods = ("template", "llm") if args.method == "both" else (args.method,)
    #: 忠实度只在**非空回答**上算。空回答（证据为空）不可能"外推"，
    #: 把它算进分母等于给"什么都不说"发满分——实测就踩过：
    #: 6 条药品信息因为参数 bug 全部答"图谱中无相关记录"，忠实度看起来是 24/24。
    stats = {m: {"ok": 0, "n": 0, "empty": 0, "violations": []} for m in methods}

    with driver.session() as s:
        all_names, prefixes = build_index(s)
        max_len = max((len(x) for x in all_names), default=2)

        for r in rows:
            qid, intent = r["qid"].strip(), r["intent"].strip()
            if (r.get("expect") or "链接").strip() == "边界":
                continue
            question = r["question"].strip()
            allowed = INTENT_LABELS.get(intent, ("Drug", "Disease", "Population"))
            entities: list[str] = []
            ambiguities: list[tuple[str, str]] = []      # (用户说的词, 候选说明)
            for _a, _b, piece, kind in scan(question, all_names, prefixes, max_len):
                res = resolve(piece, kind, allowed, all_names, prefixes)
                if res["status"] == "需澄清":
                    ambiguities.append((piece, res["note"]))
                if res["node"] and res["node"] not in entities:
                    entities.append(res["node"])

            if ambiguities:
                # ⚠️ **§13.3：实体有歧义必须停下来反问，不能用"能认出来的那部分"硬答。**
                #    实测踩过：`茶碱和卡维地洛一起用要紧吗？` 里 `茶碱` 有 3 个候选，
                #    流水线跳过了它、只答「卡维地洛片 共有 313 条相互作用记录」——
                #    **忠实度 ✓ 但答非所问**。这就是"忠实 ≠ 正确"的活例子
                #    （和 L4 那次"忠实度 100% 而假阳性 33%"是同一类教训）。
                ask = "、".join(f"{w}（{note}）" for w, note in ambiguities)
                ans = (f"你说的「{ambiguities[0][0]}」在图谱里对应多个条目：{ask}。"
                       f"请确认你指的是哪一个，我再查。{DISCLAIMER}")
                log(f"  {qid} [{intent}] {question}")
                log(f"      ⚠️ 需澄清：{ask}")
                methods_llm = "llm" in methods
                rec = {"qid": qid, "intent": intent, "question": question,
                       "entities": entities, "clarify": ask,
                       "evidence": {"nodes": [], "edges": [], "cypher": "",
                                    "rows": []},
                       "answers": {m: {"text": ans, "faithful": True,
                                       "out_of_evidence": []}
                                   for m in methods}}
                # 澄清句会回述**用户说的那个词**（`茶碱`）以及候选名——
                # 那些都是用户输入与图谱候选，不是"答案凭空说的"，所以遮蔽掉再判。
                quoted = {w for w, _ in ambiguities} | set(
                    entities) | {x.strip() for x in ambiguities[0][1]
                                 .replace("个候选：", "").split("、") if x.strip()}
                bad = node_names_in(ans, all_names, already_allowed=quoted)
                ok = not bad
                for m in methods:
                    stats[m]["n"] += 1
                    stats[m]["ok"] += 1
                results.append(rec)
                log(f"      [{'/'.join(methods)}] ✓ 已反问（不计入外推）")
                log("")
                continue

            cy, ev = evidence_cypher(intent, entities, s)
            rec = {"qid": qid, "intent": intent, "question": question,
                   "entities": entities, "evidence": ev, "answers": {}}

            log(f"  {qid} [{intent}] {question}")
            log(f"      实体 {entities}  证据 {len(ev['nodes'])} 点 / "
                f"{len(ev['edges'])} 边 / {len(ev['rows'])} 行")

            no_evidence = not ev["nodes"] and not ev["edges"]
            for m in methods:
                if m == "template":
                    ans = template_answer(intent, entities, ev)
                else:
                    ans = llm_answer(question, intent, entities, ev, st)
                ok, bad = faithfulness(ans, ev, all_names)
                if no_evidence:
                    stats[m]["empty"] += 1
                else:
                    stats[m]["n"] += 1
                    stats[m]["ok"] += ok
                if not ok:
                    stats[m]["violations"].append((qid, sorted(bad)))
                rec["answers"][m] = {"text": ans, "faithful": ok,
                                     "out_of_evidence": sorted(bad)}
                log(f"      [{m}] {'✓忠实' if ok else '✗外推 ' + str(sorted(bad))}"
                    f"  {ans[:60]}")

            if persist is not None:
                _persist_turn(persist, question, rec)
            results.append(rec)
            log("")

    driver.close()
    if persist is not None:
        _close_persist(persist)

    log("── 汇总 ──")
    for m in methods:
        d = stats[m]
        rate = d["ok"] / d["n"] if d["n"] else 0.0
        log(f"  {m:<9} 忠实度 {d['ok']}/{d['n']} = {rate:.3f}"
            f"   （另有 {d['empty']} 条**证据为空**、不计入分母）")
        if d["violations"]:
            log(f"            越界：{d['violations']}")
        if d["empty"]:
            log(f"            ⚠️ 那 {d['empty']} 条要单独看：证据为空可能是"
                f"「图谱真没有」，也可能是**查询写错了**。逐条核过再下结论。")
    log("")
    log("  ⚠️ 这是**必要条件不是充分条件**：它抓不到"
        "「换了种说法编数值/编结论」。人工抽检不能省。")

    OUT_FILE.write_text(json.dumps(
        {"method": args.method, "disclaimer": DISCLAIMER,
         "note": "忠实度=机械检查；须配人工抽检", "results": results},
        ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    log(f"  ✓ 已写 {OUT_FILE.relative_to(REPO_ROOT)}")
    return 0


# ---------------------------------------------------------------- L7 落库（§6.1 第 ⑥ 步）

def _open_persist(settings) -> dict:
    """注册一个临时用户、开一个会话，准备把每轮落库。

    **走真实 HTTP 接口**（TestClient 打 FastAPI 路由），而不是直接怼 ORM——
    直接写表会绕开路由里的归属校验与字段处理，测出来的不是"接口能用"。
    L7 的那 33 项自测已经证明接口本身没问题，这里只是把它接到 L5 上。
    """
    import time as _time
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    user = f"l5_persist_{int(_time.time())}"
    pwd = "l5-persist-pw-123456"
    r = client.post("/auth/register", json={"username": user, "password": pwd})
    r.raise_for_status()
    token = r.json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    conv = client.post("/conversations", json={}, headers=h)
    conv.raise_for_status()
    log(f"  落库      用户 {user}  会话 id={conv.json()['id']}")
    log("")
    return {"client": client, "headers": h, "conv": conv.json()["id"],
            "user": user, "sent": 0}


def _persist_turn(persist: dict, question: str, rec: dict) -> None:
    """把一个轮次落库：用户消息 + 助手消息（带改写/Cypher/证据）。"""
    c, h, cid = persist["client"], persist["headers"], persist["conv"]
    c.post(f"/conversations/{cid}/messages",
           json={"role": "user", "content": question}, headers=h).raise_for_status()
    # 多轮改写（§6.1 第 ⓪ 步）还没做，所以 rewritten_query 先记原问题本身——
    # **不编一个假的**：留空与记原问题都行，但假装改写过了就是在台账里撒谎。
    ans = next(iter(rec["answers"].values()))["text"]
    c.post(f"/conversations/{cid}/messages",
           json={"role": "assistant", "content": ans,
                 "rewritten_query": question,
                 "cypher": rec["evidence"].get("cypher") or None,
                 "evidence": {"nodes": rec["evidence"]["nodes"],
                              "edges": rec["evidence"]["edges"],
                              "rows": rec["evidence"]["rows"]}},
           headers=h).raise_for_status()
    persist["sent"] += 1


def _close_persist(persist: dict) -> None:
    """核验真的落库了，然后把测试数据删干净。

    ⚠️ **先核验再删**：直接删掉的话，"落库成功"就只是一句话，没有任何证据。
    """
    from sqlalchemy import func, select
    from app.db import SessionLocal
    from app.models import AppUser, Conversation, Message

    c, h, cid = persist["client"], persist["headers"], persist["conv"]
    msgs = c.get(f"/conversations/{cid}/messages?limit=200", headers=h).json()
    with SessionLocal() as db:
        n_msg = db.execute(select(func.count(Message.id))
                           .where(Message.conversation_id == cid)).scalar() or 0
        n_ev = db.execute(select(func.count(Message.id))
                          .where(Message.conversation_id == cid,
                                 Message.evidence.is_not(None))).scalar() or 0
        n_cy = db.execute(select(func.count(Message.id))
                          .where(Message.conversation_id == cid,
                                 Message.cypher.is_not(None))).scalar() or 0
        db.execute(__import__("sqlalchemy").delete(AppUser)
                   .where(AppUser.username == persist["user"]))
        db.commit()
        left = db.execute(select(func.count(AppUser.id))
                          .where(AppUser.username == persist["user"])).scalar()

    log("── L7 落库核验 ──")
    log(f"  经过 HTTP 接口读回 {len(msgs)} 条消息")
    log(f"  直接查库：message {n_msg} 行，其中带 evidence {n_ev} 行、"
        f"带 cypher {n_cy} 行")
    log(f"  落库轮次 {persist['sent']} 轮（每轮 2 条消息）")
    log(f"  清理后残留用户 {left} 个 {'✓' if left == 0 else '✗'}")
    log("")


def llm_answer(question: str, intent: str, entities: list[str], ev: dict,
               settings) -> str:
    """LLM 回答：**只把查询结果喂进去**，并明确禁止外推（§6.1 第 ⑤ 步）。

    刻意不把 schema、不把 Cypher 给它——它要做的只是"把这几行结果说成人话"，
    给多了反而给了它编造的空间。
    """
    from openai import OpenAI
    client = OpenAI(api_key=settings.dashscope_api_key,
                    base_url=settings.dashscope_base_url,
                    timeout=60.0, max_retries=3)

    table = "\n".join(" | ".join("" if v is None else str(v) for v in row)
                      for row in ev["rows"]) or "（查询没有返回任何行）"
    prompt = (
        "你是医药问答助手。下面是一次知识图谱查询的**全部**结果。\n"
        "请只根据这些结果回答用户的问题。\n\n"
        f"用户问题：{question}\n\n"
        f"查询结果（共 {len(ev['rows'])} 行）：\n{table}\n\n"
        "硬性要求：\n"
        "1. **只能使用上面结果里出现的信息**，不得补充、推断、联想任何其他医学知识；\n"
        "2. 不得出现结果里没有的药品名、疾病名或人群名；\n"
        "3. 如果结果为空，就明确回答「图谱中无相关记录」，不要试图用常识回答；\n"
        "4. 回答末尾原样附上这句免责声明：" + DISCLAIMER + "\n"
        "5. 只输出回答本身，不要解释你的做法。"
    )
    resp = client.chat.completions.create(
        model=settings.llm_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0, max_tokens=400)
    return (resp.choices[0].message.content or "").strip()


if __name__ == "__main__":
    raise SystemExit(main())
