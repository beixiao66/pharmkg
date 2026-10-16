#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L5 尖刀实验 ③：多轮查询改写（§6.1 第 ⓪ 步）
=============================================

用户拍板：**端到端判**——把改写结果喂进已经量过的单轮链路
（实体链接 → Text2Cypher），看最终结果集是否等于金标准；
并把**改写文本全部落盘**供人看。

判据为什么落在端到端而不是字符串
----------------------------------
改写的目的是**让后面的单轮链路能正确工作**，不是措辞像不像。
`那它有什么禁忌？` → `他克莫司有哪些禁忌？` 与 `他克莫司的禁忌是什么？`
都算成功，字符串比对会把后者判错。

⚠️ 顺带发现：§6.1 那条「若当前问题已含药物实体 → 跳过改写」**有个洞**
------------------------------------------------------------------
多轮集的第 2 轮是 `它和依度沙班能一起吃吗？`——**里面确实有药物实体**（依度沙班），
按规则就该跳过改写。可它缺的是 `卡维地洛`，跳过之后链路只认出一个药，必然答错。

所以"含药物实体"不等于"含**所需**实体"。两种口径都量出来给用户拍板：

* ``--skip-rule as-written``：照 §6.1 原文，含药物实体就跳过；
* ``--skip-rule never``：一律改写（把 ⓪ 步当无条件执行）。

另外跑一个**对照**：**完全不改写的原始第 2 轮**直接进链路——应当大面积失败，
用来证明这一步不是白做的。

用法
----
    python scripts/l5_rewrite.py                        # 两种跳过口径都跑 + 对照
    python scripts/l5_rewrite.py --skip-rule never
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
from collections import Counter

from insert_io import read_csv_rows
from opencmkg_io import REPO_ROOT
from l5_cypher_prompt import build_messages, extract_cypher, validate_cypher
from l5_entity_linking import INTENT_LABELS, build_index, resolve, scan
from l5_text2cypher import MAX_ROWS, MAX_TOKENS, norm_row

sys.path.insert(0, str(REPO_ROOT / "backend"))

MULTITURN_FILE = REPO_ROOT / "data" / "eval" / "l5_multiturn.csv"
QUESTIONS_FILE = REPO_ROOT / "data" / "eval" / "l5_questions.csv"
FEWSHOT_FILE = REPO_ROOT / "data" / "eval" / "l5_fewshot.csv"
OUT_FILE = REPO_ROOT / "data" / "eval" / "l5_rewrite.json"
REVIEW_FILE = REPO_ROOT / "data" / "eval" / "l5_rewrite_review.csv"

#: 「最近 N 轮」的 N。§6.2 建议 3；本集只有 2 轮，写死 3 也不会有副作用。
CONTEXT_TURNS = 3
LLM_TIMEOUT = 60.0
LLM_RETRIES = 3


def log(msg: str = "") -> None:
    print(msg, flush=True)


def rewrite_prompt(history: list[tuple[str, str]], question: str) -> str:
    """⓪ 步的提示词：最近 N 轮 + 当前问题 → 一个自包含的完整问题。

    刻意**不给 schema、不给 Cypher**——它只做"把话补全"这一件事。
    给了反而可能诱导它顺手把问题也改了。
    """
    lines = ["你在帮一个医药问答系统做**查询改写**。", ""]
    lines.append("下面是最近几轮对话：")
    for role, content in history[-CONTEXT_TURNS:]:
        lines.append(f"  {'用户' if role == 'user' else '助手'}：{content}")
    lines += [
        "",
        f"用户最新一句：{question}",
        "",
        "请把这句改写成**一个自包含的完整问题**：把里面的代词"
        "（它 / 这个药 / 这个病 等）替换成上文里实际提到的那个具体的药名或病名，"
        "使得这句话**单独拿出来也能看懂**。",
        "",
        "硬性要求：",
        "1. 只补全指代，**不要改变用户问的意图**，也不要增加用户没问的内容；",
        "2. 如果最新一句本来就已经自包含（没有任何代词/省略），就原样输出它；",
        "3. 只输出改写后的问题本身，不要解释、不要加引号。",
    ]
    return "\n".join(lines)


def run_pipeline(session, client, model, question: str, intent: str,
                 names, prefixes, max_len, fewshot) -> dict:
    """把一个（已经自包含的）问题跑完单轮链路，返回结果集与过程。"""
    allowed = INTENT_LABELS.get(intent, ("Drug", "Disease", "Population"))
    ents: list[str] = []
    for _a, _b, piece, kind in scan(question, names, prefixes, max_len):
        res = resolve(piece, kind, allowed, names, prefixes)
        if res["node"] and res["node"] not in ents:
            ents.append(res["node"])
    if not ents:
        return {"entities": [], "rows": [], "cypher": "", "why": "没认出任何实体"}

    resp = client.chat.completions.create(
        model=model, temperature=0, max_tokens=MAX_TOKENS,
        messages=build_messages(question, intent, ents, fewshot))
    cypher = extract_cypher(resp.choices[0].message.content or "")
    ok, why = validate_cypher(cypher)
    if not ok:
        return {"entities": ents, "rows": [], "cypher": cypher,
                "why": f"白名单未过：{why}"}
    try:
        rows = [norm_row(r) for r in session.run(cypher, timeout=10.0)]
    except Exception as exc:  # noqa: BLE001
        return {"entities": ents, "rows": [], "cypher": cypher,
                "why": f"执行失败 {type(exc).__name__}"}
    if len(rows) > MAX_ROWS:
        return {"entities": ents, "rows": [], "cypher": cypher, "why": "超出条数上限"}
    return {"entities": ents, "rows": rows, "cypher": cypher, "why": ""}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="L5 多轮改写尖刀实验")
    ap.add_argument("--skip-rule", choices=("as-written", "never", "both"),
                    default="both")
    args = ap.parse_args(argv)

    from app.config import get_settings
    from neo4j import GraphDatabase
    from openai import OpenAI

    st = get_settings()
    if not st.llm_configured:
        log("✗ LLM 未配齐。")
        return 2

    mt, _ = read_csv_rows(MULTITURN_FILE)
    qrows, _ = read_csv_rows(QUESTIONS_FILE)
    gold_cypher = {r["qid"].strip(): (r.get("gold_cypher") or "").strip()
                   for r in qrows}
    gold_nodes = {r["qid"].strip(): [x.strip() for x in
                                     (r.get("gold_nodes") or "").split("；")
                                     if x.strip()] for r in qrows}
    fewshot = []
    if FEWSHOT_FILE.exists():
        fs, _ = read_csv_rows(FEWSHOT_FILE)
        for r in fs:
            fewshot.append({"intent": r["intent"].strip(),
                            "question": r["question"].strip(),
                            "entities": [x.strip() for x in
                                         (r["entities"] or "").split("；") if x.strip()],
                            "cypher": r["cypher"].strip()})

    driver = GraphDatabase.driver(st.neo4j_uri,
                                 auth=(st.neo4j_user, st.neo4j_password))
    client = OpenAI(api_key=st.dashscope_api_key, base_url=st.dashscope_base_url,
                    timeout=LLM_TIMEOUT, max_retries=LLM_RETRIES)

    rules = ("as-written", "never") if args.skip_rule == "both" else (args.skip_rule,)

    log("L5 尖刀实验 ③ · 多轮查询改写")
    log(f"  测试集    {MULTITURN_FILE.relative_to(REPO_ROOT)}（{len(mt)} 组两轮对话）")
    log(f"  模型      {st.llm_model}   temperature=0   few-shot {len(fewshot)} 条")
    log(f"  判据      端到端：改写结果过单轮链路后的**结果集**是否等于金标准")
    log(f"  跳过口径  {rules}")
    log("")

    summary: dict = {"rules": {}, "review": []}
    with driver.session() as s:
        names, prefixes = build_index(s)
        max_len = max((len(x) for x in names), default=2)

        # 金标准的结果集先算好（同一套口径）
        gold_rows: dict[str, list] = {}
        for qid, cy in gold_cypher.items():
            if not cy:
                continue
            try:
                gold_rows[qid] = [norm_row(r) for r in s.run(cy, timeout=10.0)]
            except Exception:  # noqa: BLE001
                gold_rows[qid] = []

        for rule in rules:
            ok = n = 0
            reasons: Counter = Counter()
            log(f"════ 跳过口径：{rule} ════")
            for row in mt:
                mid, qid = row["mid"], row["qid"]
                intent = row["intent"].strip()
                turn1, turn2 = row["turn1"].strip(), row["turn2"].strip()
                gold_q = row["gold_question"].strip()

                # §6.1 的规则：当前问题已含药物实体 → 跳过改写
                allowed = INTENT_LABELS.get(intent, ("Drug", "Disease", "Population"))
                has_drug = False
                for _a, _b, piece, kind in scan(turn2, names, prefixes, max_len):
                    res = resolve(piece, kind, allowed, names, prefixes)
                    if res["node"] and any(True for _ in [1]):
                        # 该节点是不是 Drug？直接问图
                        if s.run("MATCH (n:Drug {name:$x}) RETURN count(n) AS c",
                                 x=res["node"]).single()["c"]:
                            has_drug = True
                skipped = (rule == "as-written" and has_drug)

                if skipped:
                    rewritten = turn2
                else:
                    resp = client.chat.completions.create(
                        model=st.llm_model, temperature=0, max_tokens=200,
                        messages=[{"role": "user",
                                   "content": rewrite_prompt(
                                       [("user", turn1)], turn2)}])
                    rewritten = (resp.choices[0].message.content or "").strip()

                got = run_pipeline(s, client, st.llm_model, rewritten, intent,
                                   names, prefixes, max_len, fewshot)
                g = gold_rows.get(qid, [])
                hit = bool(g) and sorted(got["rows"]) == sorted(g)
                n += 1
                ok += hit
                if not hit:
                    reasons[got["why"] or "结果集不符"] += 1
                log(f"  {'✓' if hit else '✗'} {mid} [{intent}]"
                    + ("  （按规则跳过改写）" if skipped else ""))
                log(f"      第2轮  {turn2}")
                log(f"      改写为 {rewritten}")
                log(f"      实体 {got['entities']}"
                    + (f"  失败原因 {got['why']}" if got["why"] else ""))
                summary["review"].append({
                    "mid": mid, "qid": qid, "intent": intent, "rule": rule,
                    "turn1": turn1, "turn2": turn2, "rewritten": rewritten,
                    "gold_question": gold_q,
                    "skipped": "是" if skipped else "否",
                    "entities": "；".join(got["entities"]),
                    "结果集对否": "对" if hit else "错",
                    "改写对否（人工填）": "", "备注": ""})
            rate = ok / n if n else 0.0
            summary["rules"][rule] = {"ok": ok, "n": n, "rate": round(rate, 3),
                                      "reasons": dict(reasons)}
            log("")
            log(f"  ── {rule}：端到端 {ok}/{n} = {rate:.3f}")
            if reasons:
                log(f"     失败原因 {dict(reasons)}")
            log("")

        # ── 对照：**完全不改写**，把原始第 2 轮直接送进链路 ──
        log("════ 对照：完全不改写（原始第 2 轮直接进链路）════")
        c_ok = c_n = 0
        for row in mt:
            got = run_pipeline(s, client, st.llm_model, row["turn2"].strip(),
                               row["intent"].strip(), names, prefixes, max_len,
                               fewshot)
            g = gold_rows.get(row["qid"], [])
            hit = bool(g) and sorted(got["rows"]) == sorted(g)
            c_n += 1
            c_ok += hit
            log(f"  {'✓' if hit else '✗'} {row['mid']}"
                + (f"   {got['why']}" if got["why"] else ""))
        log("")
        log(f"  ── 不改写：端到端 {c_ok}/{c_n} = {c_ok / c_n:.3f}")
        summary["no_rewrite"] = {"ok": c_ok, "n": c_n,
                                 "rate": round(c_ok / c_n, 3)}

    driver.close()

    OUT_FILE.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                        encoding="utf-8", newline="\n")
    buf = io.StringIO()
    fields = ["mid", "qid", "intent", "rule", "turn1", "turn2", "rewritten",
              "gold_question", "skipped", "entities", "结果集对否",
              "改写对否（人工填）", "备注"]
    w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    w.writeheader()
    for r in summary["review"]:
        w.writerow(r)
    REVIEW_FILE.write_text(buf.getvalue(), encoding="utf-8", newline="\n")

    log("── 总览 ──")
    for rule, d in summary["rules"].items():
        log(f"  改写（{rule:<10}） 端到端 {d['ok']}/{d['n']} = {d['rate']:.3f}")
    d = summary["no_rewrite"]
    log(f"  不改写（对照）          端到端 {d['ok']}/{d['n']} = {d['rate']:.3f}")
    log("")
    log(f"  ✓ 已写 {OUT_FILE.relative_to(REPO_ROOT)}")
    log(f"  ✓ 改写文本全部落盘 {REVIEW_FILE.relative_to(REPO_ROOT)}"
        f"（含「改写对否」列，请人工抽检）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
