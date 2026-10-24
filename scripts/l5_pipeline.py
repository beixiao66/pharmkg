#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L5 · 端到端问答流水线（§6.1 的 ⓪→⑦ 串成一条）
===============================================

前面几轮把每个环节都单独量过了（实体链接 / Text2Cypher / 回答 / 改写 / 意图），
但它们是**各自独立的尖刀脚本**。这个脚本把它们**串成一条**，并给出**端到端数字**。

它**不重新实现任何环节**——全部 import 已验证过的模块。串起来的价值在两件事：

1. **端到端数字**：各环节单独都还行，合起来还行吗？
2. **分阶段失败归因**：答错时**是哪一步错的**。这是只有串起来才能回答的问题，
   也是分层设计最终要兑现的东西。

流程（§6.1）
------------
```
用户提问（可带历史）
  → ⓪ 查询改写（一律改写，决策 #39）
  → ① 意图识别（LLM 为主／规则法可选）
  → ② 实体链接（确定性扫描）
        └─ 需澄清 → **停下来反问**，不进 ③（§13.3）
  → ③ Text2Cypher（输出契约 + few-shot）→ 只读白名单
  → ④ 执行（超时 + 条数上限）
  → ⑤ 证据子图 + 回答（仅基于查询结果）
  → ⑥ ⑦ 落库由 --persist 走 L7 的真实 HTTP 接口
```

证据来源（用户拍板 (c)）
------------------------
**按意图的固定查询**（不依赖模型输出），**另加一条一致性检查**：
生成 Cypher 返回的字符串值必须能在证据里找到，对不上就报出来。
这样既保住"证据不依赖模型"，又能发现两者背离。

归因规则
--------
逐级比对，**第一个偏离的环节就是归因**（后面的偏离往往是它的后果）：

   改写 → 意图 → 实体 → Cypher 执行 → 结果集

用法
----
    python scripts/l5_pipeline.py                    # 单轮 + 多轮都跑
    python scripts/l5_pipeline.py --mode single
    python scripts/l5_pipeline.py --intent rule      # 用规则法判意图
    python scripts/l5_pipeline.py --no-rewrite       # 消融：不做改写
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from collections import Counter

from insert_io import read_csv_rows
from opencmkg_io import REPO_ROOT
from l5_answer import evidence_cypher, faithfulness, template_answer
from l5_cypher_prompt import build_messages, extract_cypher, validate_cypher
from l5_entity_linking import INTENT_LABELS, build_index, resolve, scan
from pinyin_match import pinyin_key
from l5_intent import llm_intent, rule_intent
from l5_rewrite import rewrite_prompt
from l5_text2cypher import MAX_ROWS, MAX_TOKENS, norm_row

sys.path.insert(0, str(REPO_ROOT / "backend"))

QUESTIONS_FILE = REPO_ROOT / "data" / "eval" / "l5_questions.csv"
MULTITURN_FILE = REPO_ROOT / "data" / "eval" / "l5_multiturn.csv"
FEWSHOT_FILE = REPO_ROOT / "data" / "eval" / "l5_fewshot.csv"
OUT_FILE = REPO_ROOT / "data" / "eval" / "l5_pipeline.json"

DISCLAIMER = "以上内容来自知识图谱，仅供参考，不能替代医生诊断。"
LLM_TIMEOUT = 60.0
LLM_RETRIES = 3


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 单轮流水线

def _uncovered(covered: list[tuple[int, int]], text: str) -> list[str]:
    """把一句话里**没有被实体命中覆盖**的片段切出来。

    例：「卡位低落和依度沙班能一起吃吗」+ 覆盖了「依度沙班」
    → ['卡位低落和', '能一起吃吗']（长度 < 2 的片段丢掉，没有匹配价值）
    """
    spans = sorted(covered)
    out: list[str] = []
    cur = 0
    for a, b in spans:
        if a > cur:
            seg = text[cur:a].strip()
            if len(seg) >= 2:
                out.append(seg)
        cur = max(cur, b)
    if cur < len(text):
        seg = text[cur:].strip()
        if len(seg) >= 2:
            out.append(seg)
    return out


def _longest_unique_pinyin(pinyin, seg: str) -> tuple[str, str] | None:
    """在一个片段里找**最长的、同音精确且唯一**的子串。

    为什么要找子串：切出来的片段常带虚词——「卡位低落**和**」。
    整段转拼音是 `ka wei di luo he`，对不上任何药名；而其中「卡位低落」
    正好等于「卡维地洛」的拼音。所以逐段枚举子串，取最长的那一个。

    为什么要求"唯一"：同音可能对应好几个药（多音字、同音异药）。
    问答里错认一个药会直接变成答案的主语，**宁可认不出，也不能猜一个**。
    """
    best: tuple[str, str] | None = None
    n = len(seg)
    for i in range(n):
        for j in range(i + 2, n + 1):
            sub = seg[i:j]
            if best is not None and len(sub) <= len(best[0]):
                continue
            # ⚠️ **至少 3 个字、至少 2 个音节**。少了这条会误配：
            #    实测「能一起吃吗」里的「能」→ 拼音 `neng` 正好是「能全素」的前缀，
            #    于是把"能全素"当成了用户说的药。虚词单字必须排除。
            if len(sub) < 3 or len(pinyin_key(sub).split()) < 2:
                continue
            # 为什么用 match() 而不是 exact()：药节点常带剂型后缀
            # （「卡维地洛**片**」），而用户说的是「卡维地洛」——
            # 拼音上前者是后者的**前缀**（ka wei di luo ⊂ ka wei di luo pian）。
            # 只认整词会把这种情况全部漏掉（实测踩过）。
            # match() 的前缀只在**音节边界**上做，所以不会切出半个音节。
            hits, _how = pinyin.match(sub, limit=5)
            if len(hits) == 1:
                best = (sub, hits[0])
    return best


def run_turn(session, client, st, question: str, *, intent_gold: str = "",
             gold_nodes: list[str] | None = None, gold_rows: list | None = None,
             history: list[tuple[str, str]] | None = None,
             names=None, prefixes=None, aliases=None, pinyin=None, max_len=2, fewshot=None,
             intent_method: str = "llm", do_rewrite: bool = True,
             answer_method: str = "template", trace: bool = False) -> dict:
    """跑完一轮，返回**每一步的中间结果**（归因要用）。"""
    gold_nodes = gold_nodes or []
    out: dict = {"question": question, "rewritten": question, "step": "ok"}

    def say(m: str) -> None:
        if trace:
            log(m)

    # ── ⓪ 查询改写 ──────────────────────────────────────────────
    if do_rewrite and history:
        resp = client.chat.completions.create(
            model=st.llm_model, temperature=0, max_tokens=200,
            messages=[{"role": "user",
                       "content": rewrite_prompt(history, question)}])
        out["rewritten"] = (resp.choices[0].message.content or "").strip()
    say(f"      ⓪ 改写 → {out['rewritten']}")

    # ── ① 意图识别 ──────────────────────────────────────────────
    if intent_method == "llm":
        out["intent"] = llm_intent(client, st.llm_model, out["rewritten"])
    else:
        out["intent"] = rule_intent(out["rewritten"])
    say(f"      ① 意图 → {out['intent']}")
    if out["intent"] not in INTENT_LABELS:
        out["intent"] = "其他"

    # ── ② 实体链接 ──────────────────────────────────────────────
    allowed = INTENT_LABELS.get(out["intent"], ("Drug", "Disease", "Population"))
    ents: list[str] = []
    ambiguities: list[str] = []
    for _a, _b, piece, kind in scan(out["rewritten"], names, prefixes, max_len):
        res = resolve(piece, kind, allowed, names, prefixes, aliases=aliases)
        if res["status"] == "需澄清":
            ambiguities.append(piece)
        if res["node"] and res["node"] not in ents:
            ents.append(res["node"])
    out["entities"] = ents
    say(f"      ② 实体 → {ents}" + (f"  需澄清 {ambiguities}" if ambiguities else ""))

    if ambiguities:
        # §13.3：有歧义就停下来反问，**不进 ③**
        out["step"] = "澄清"
        out["answer"] = (f"你说的「{ambiguities[0]}」在图谱里对应多个条目，"
                         f"请确认是哪一个，我再查。{DISCLAIMER}")
        out["faithful"] = True
        return out

    # ── ②.5 拼音兜底：字形没认出来的**片段**，再按读音试一次 ─────────
    #
    # 真实需求：「卡维地洛」被写成「卡位低落」——读起来一样，三个字都不一样。
    #
    # ⚠️ 第一版写错了：只在"一个实体都没认出"时才兜底。可实际问题常常是
    #    「卡位低落**和依度沙班**能一起吃吗」——依度沙班认出来了，`ents` 非空，
    #    兜底分支永远不进。**兜底要针对"没被覆盖的那几段文字"**，不是整句。
    #
    # ⚠️ 与"药品检索"的安全不对称：检索里多给候选没关系（用户会挑），
    #    **问答里错认一个实体会直接变成答案的主语**。所以这里只采用
    #    **同音精确且唯一**的匹配，并在答案里明说是按读音理解的。
    pinyin_note = ""
    if pinyin is not None:
        covered = [(a, b) for a, b, _p, _k in
                   scan(out["rewritten"], names, prefixes, max_len)]
        for seg in _uncovered(covered, out["rewritten"]):
            found = _longest_unique_pinyin(pinyin, seg)
            if found and found[1] not in ents:
                piece, node = found
                ents.append(node)
                pinyin_note += (
                    f"（「{piece}」在图谱中没有同名字面，这里按读音理解为「{node}」"
                    f"——如果理解错了，请用完整药名再问一次。）\n\n")
                say(f"      ② 拼音兜底 → {piece} ≈ {node}")

    if not ents:
        out["step"] = "无实体"
        out["answer"] = (
            # ⚠️ 这里**不能说"不在查询范围内"**。用户问「Lindane能治什么」时，
            #    问题明明在范围内（药物→适应症），失败的只是"认不出实体"。
            #    把三件事（超出范围 / 没认出实体 / 认出了但无数据）混成一句，
            #    会让用户以为系统不支持这类问题，从而放弃——这是实测反馈出来的。
            "我在你的问题里没有认出图谱中的药物、疾病或人群名称，所以查不到东西。\n\n"
            "这不代表没有相关信息，只是这条链路认不出它。可以试试："
            "① 用图谱里存在的名称（可先在「图谱浏览」里搜一下）；"
            "② 换个说法，例如「某某药和某某药能一起吃吗」「某某病可以用哪些药」。"
            f"{DISCLAIMER}")
        out["faithful"] = True
        return out

    # ── ③ Text2Cypher ───────────────────────────────────────────
    resp = client.chat.completions.create(
        model=st.llm_model, temperature=0, max_tokens=MAX_TOKENS,
        messages=build_messages(out["rewritten"], out["intent"], ents, fewshot))
    cypher = extract_cypher(resp.choices[0].message.content or "")
    ok, why = validate_cypher(cypher)
    out["cypher"] = cypher
    if not ok:
        out["step"] = f"白名单未过"
        out["answer"] = f"生成的查询未通过只读校验（{why}）。{DISCLAIMER}"
        return out
    say(f"      ③ Cypher → {cypher[:90]}")

    # ── ④ 执行 ──────────────────────────────────────────────────
    try:
        rows = [norm_row(r) for r in session.run(cypher, timeout=10.0)]
    except Exception as exc:  # noqa: BLE001
        out["step"] = "执行失败"
        out["answer"] = f"查询执行失败（{type(exc).__name__}）。{DISCLAIMER}"
        return out
    if len(rows) > MAX_ROWS:
        out["step"] = "超条数上限"
        out["answer"] = f"查询结果超过 {MAX_ROWS} 行上限，已拒绝。{DISCLAIMER}"
        return out
    out["rows"] = rows
    say(f"      ④ 执行 → {len(rows)} 行")

    # ── ⑤ 证据子图 + 回答 ───────────────────────────────────────
    _cy, ev = evidence_cypher(out["intent"], ents, session)
    out["evidence"] = ev
    # 一致性检查（用户拍板 (c)）：生成 Cypher 的字符串值必须能在证据里找到
    ev_texts = {n["name"] for n in ev["nodes"] if n["name"]}
    for e in ev["edges"]:
        ev_texts |= {e["from"], e["to"]}
        ev_texts |= {v for v in (e["props"] or {}).values() if isinstance(v, str)}
    for n in ev["nodes"]:
        ev_texts |= {v for v in (n["props"] or {}).values() if isinstance(v, str)}
    ev_texts.discard(None)
    stray = set()
    for r in rows:
        for v in r:
            s_ = str(v)
            # 只查"看起来像名字"的值：太短的数字/单词不判
            if len(s_) >= 2 and s_ not in ev_texts:
                stray.add(s_)
    out["evidence_consistent"] = not stray
    out["stray"] = sorted(stray)[:5]

    if answer_method == "template":
        out["answer"] = pinyin_note + template_answer(out["intent"], ents, ev)
    else:
        from l5_answer import llm_answer
        out["answer"] = pinyin_note + llm_answer(
            out["rewritten"], out["intent"], ents, ev, st)
    f_ok, f_bad = faithfulness(out["answer"], ev, names)
    out["faithful"] = f_ok
    out["out_of_evidence"] = sorted(f_bad)

    # ── 归因：逐级比对，第一个偏离的就是原因 ────────────────────
    if gold_rows is not None:
        if sorted(rows) == sorted(gold_rows):
            out["result_ok"] = True
        else:
            out["result_ok"] = False
            if set(gold_nodes) - set(ents):
                out["step"] = "实体缺失" if out.get("rewritten") == question else "改写没补全"
            elif out["intent"] != intent_gold:
                out["step"] = "意图判错"
            else:
                out["step"] = "Cypher 或结果不符"
    return out


# ---------------------------------------------------------------- 主流程

def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="L5 端到端问答流水线")
    ap.add_argument("--mode", choices=("single", "multi", "both"), default="both")
    ap.add_argument("--intent", choices=("llm", "rule"), default="llm")
    ap.add_argument("--answer", choices=("template", "llm"), default="template")
    ap.add_argument("--no-rewrite", action="store_true", help="消融：不做改写")
    ap.add_argument("--show", type=int, default=0, help="打印前几轮全过程")
    args = ap.parse_args(argv)

    from app.config import get_settings
    from neo4j import GraphDatabase
    from openai import OpenAI

    st = get_settings()
    if not st.llm_configured:
        log("✗ LLM 未配齐。")
        return 2

    qrows, _ = read_csv_rows(QUESTIONS_FILE)
    qmap = {r["qid"].strip(): r for r in qrows}
    single = [r for r in qrows if (r.get("expect") or "链接").strip() == "链接"]
    multi = []
    if MULTITURN_FILE.exists():
        multi, _ = read_csv_rows(MULTITURN_FILE)
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

    log("L5 端到端流水线（⓪→⑦ 串成一条）")
    log(f"  模式      单轮 {len(single)} 条 / 多轮 {len(multi)} 组")
    log(f"  意图识别  {args.intent}    回答  {args.answer}"
        f"    改写  {'关（消融）' if args.no_rewrite else '开'}")
    log(f"  模型      {st.llm_model}   few-shot {len(fewshot)} 条")
    log("")

    summary: dict = {}
    with driver.session() as s:
        names, prefixes, aliases = build_index(s)
        max_len = max((len(x) for x in names), default=2)

        gold_rows_cache: dict[str, list] = {}

        def gold_of(qid: str) -> list:
            if qid not in gold_rows_cache:
                r = qmap.get(qid, {})
                cy = (r.get("gold_cypher") or "").strip()
                try:
                    gold_rows_cache[qid] = [norm_row(x) for x in
                                            s.run(cy, timeout=10.0)] if cy else []
                except Exception:  # noqa: BLE001
                    gold_rows_cache[qid] = []
            return gold_rows_cache[qid]

        for mode in (("single",) if args.mode == "single"
                     else ("multi",) if args.mode == "multi"
                     else ("single", "multi")):
            rows_src = single if mode == "single" else multi
            ok = n = 0
            steps: Counter = Counter()
            detail = []
            log(f"════ {mode} ════")
            for i, r in enumerate(rows_src):
                r = dict(r)
                if mode == "single":
                    qid = r["qid"].strip()
                    question = r["question"].strip()
                    gold_q = question
                    history = None
                else:
                    qid = r["qid"].strip()
                    question = r["turn2"].strip()
                    gold_q = r["gold_question"].strip()
                    history = [("user", r["turn1"].strip())]
                gold_nodes = [x.strip() for x in (r.get("gold_nodes") or "").split("；")
                              if x.strip()] or [
                    x.strip() for x in (qmap[qid].get("gold_nodes") or "").split("；")
                    if x.strip()]
                intent_gold = r.get("intent", "").strip()
                g_rows = gold_of(qid)

                if args.show and i < args.show:
                    log(f"  {qid} [{intent_gold}] {question}")
                t0 = time.time()
                out = run_turn(s, client, st, question, intent_gold=intent_gold,
                               gold_nodes=gold_nodes, gold_rows=g_rows,
                               history=history, names=names, prefixes=prefixes, aliases=aliases,
              pinyin=None,   # ⚠️ 评测**不启用**拼音兜底：冻结产物的口径不变
                               max_len=max_len, fewshot=fewshot,
                               intent_method=args.intent,
                               do_rewrite=not args.no_rewrite,
                               answer_method=args.answer,
                               trace=bool(args.show and i < args.show))
                dt = time.time() - t0
                hit = bool(out.get("result_ok"))
                n += 1
                ok += hit
                steps[out["step"]] += 1
                if args.show and i < args.show:
                    log(f"      → {'✓' if hit else '✗'} {out['step']}  "
                        f"{dt:.1f}s")
                detail.append({"qid": qid, "question": question,
                               "rewritten": out.get("rewritten", ""),
                               "intent": out.get("intent"), "step": out["step"],
                               "result_ok": hit,
                               "faithful": out.get("faithful"),
                               "evidence_consistent": out.get("evidence_consistent"),
                               "entities": out.get("entities", []),
                               "answer": out.get("answer", "")[:200]})
            rate = ok / n if n else 0.0
            log(f"  ── 端到端 {ok}/{n} = {rate:.3f}")
            log(f"     分阶段落点 {dict(steps)}")
            log("")
            summary[mode] = {"ok": ok, "n": n, "rate": round(rate, 3),
                             "steps": dict(steps), "detail": detail}

    driver.close()

    log("── 总览 ──")
    for mode, d in summary.items():
        log(f"  {mode:<7} 端到端 {d['ok']}/{d['n']} = {d['rate']:.3f}"
            f"   落点 {d['steps']}")
    tag = (f"{args.intent}-{args.answer}"
           + ("-norewrite" if args.no_rewrite else ""))
    OUT_FILE.write_text(json.dumps({"tag": tag, "summary": summary},
                                   ensure_ascii=False, indent=2),
                        encoding="utf-8", newline="\n")
    log("")
    log(f"  ✓ 已写 {OUT_FILE.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
