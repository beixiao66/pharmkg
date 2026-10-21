#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L5 尖刀实验 ②：Text2Cypher
===========================

**分层的前提**：实体**已给定**（用 :mod:`l5_entity_linking` 的输出），
意图也取自问题集。所以这一轮量的**只剩"会不会写 Cypher"**——
实体没链对、意图判错，都不算在它头上（这是刻意的，否则错因混在一起分不清该修哪层）。

判分口径：**执行结果集比对**（用户拍板）
----------------------------------------
Cypher 的写法本就不唯一（``{name:'X'}`` 与 ``WHERE n.name='X'`` 完全等价），
所以不能比字符串。要真连库跑，比**结果集**。

* **严格**：行的**多重集完全相同**（忽略列名，保留列顺序）——主口径
* **宽松**：**第一列的值集合**相同——用来容纳"多返回了一个属性列"这种合理行为

两档一起报，理由与 L4 的忠实度检查一样：**只报一个数字时，读者无从判断
差的那部分是因为写错了，还是因为写法不同。**

§6.1 第 ③ 步的三道闸都要真的做
--------------------------------
1. **白名单校验**（只读、必须有 MATCH 与 RETURN）——在 :mod:`l5_cypher_prompt`
2. **超时**——Neo4j 查询超时
3. **条数上限**——返回行数超过上限判失败（防笛卡尔积把服务拖死）

对照（消融）——**证明这两样东西真的有用**，而不是"我觉得有用"
------------------------------------------------------------
* ``--no-fewshot``：只给 schema，不给示例
* ``--no-schema``：只给示例，不给 schema

用法
----
    python scripts/l5_text2cypher.py                      # 主数字
    python scripts/l5_text2cypher.py --repeat 3           # 另跑 2 次测抖动
    python scripts/l5_text2cypher.py --no-fewshot         # 消融
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from collections import Counter
from pathlib import Path

from insert_io import read_csv_rows
from opencmkg_io import REPO_ROOT
from l5_cypher_prompt import (build_messages, extract_cypher,
                              looks_like_clarification, validate_cypher)
from l5_entity_linking import INTENT_LABELS, build_index, resolve, scan

sys.path.insert(0, str(REPO_ROOT / "backend"))

QUESTIONS_FILE = REPO_ROOT / "data" / "eval" / "l5_questions.csv"
FEWSHOT_FILE = REPO_ROOT / "data" / "eval" / "l5_fewshot.csv"
OUT_FILE = REPO_ROOT / "data" / "eval" / "l5_text2cypher.json"

#: 单条查询最多返回多少行。超了判失败——防止一个笛卡尔积把库拖死（§6.1 第 ③ 步）。
MAX_ROWS = 500

#: 见 build_l5_fewshot 里的说明：不设 max_tokens 时模型可能陷入退化重复循环，
#: 一路写到超时。这个是**必须**的硬闸。
MAX_TOKENS = 300
LLM_TIMEOUT = 60.0
LLM_RETRIES = 3


def log(msg: str = "") -> None:
    print(msg, flush=True)


def norm_row(record) -> tuple:
    """把一行结果规范化成可比较的元组。

    * **必须按列的原始顺序**取（``record.values()`` 保序），列名忽略——
      模型给列取什么别名不该影响对错。
    * 列表值排序后拼接：``collect(...)`` 的返回顺序本就不保证。
    * 浮点统一成 6 位有效数字，避免 ``1.0`` 与 ``1`` 这种噪声。
    """
    out = []
    for v in record.values():
        if isinstance(v, (list, tuple, set)):
            out.append("[" + ", ".join(sorted(str(x) for x in v)) + "]")
        elif isinstance(v, float):
            out.append(f"{v:.6g}")
        elif v is None:
            out.append("∅")
        else:
            out.append(str(v))
    return tuple(out)


def rows_of(session, cypher: str, timeout: float) -> tuple[list[tuple], str]:
    """执行并取回结果。返回 ``(行集合, 失败原因)``；成功时原因为 ``""``。"""
    try:
        result = session.run(cypher, timeout=timeout)
        rows = [norm_row(r) for r in result]
    except Exception as exc:  # noqa: BLE001
        return [], f"{type(exc).__name__}: {str(exc)[:80]}"
    if len(rows) > MAX_ROWS:
        return [], f"返回 {len(rows)} 行，超过上限 {MAX_ROWS}"
    return rows, ""


def link_entities(question: str, intent: str, names, prefixes, max_len
                  ) -> tuple[list[str], bool]:
    """取实体；第二个返回值表示"是否应当澄清"（多候选）。"""
    allowed = INTENT_LABELS.get(intent, ("Drug", "Disease", "Population"))
    got: list[str] = []
    clarify = False
    for _a, _b, piece, kind in scan(question, names, prefixes, max_len):
        res = resolve(piece, kind, allowed, names, prefixes)
        if res["status"] == "需澄清":
            clarify = True
        if res["node"] and res["node"] not in got:
            got.append(res["node"])
    return got, clarify


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="L5 Text2Cypher 尖刀实验")
    ap.add_argument("--repeat", type=int, default=1,
                    help="重复跑几遍（temperature=0）；>1 时报抖动")
    ap.add_argument("--no-fewshot", action="store_true", help="消融：不给示例")
    ap.add_argument("--no-schema", action="store_true", help="消融：不给 schema")
    ap.add_argument("--timeout", type=float, default=10.0, help="单条查询超时（秒）")
    ap.add_argument("--show-cypher", action="store_true")
    #: 同厂商多模型对比（§13.4 消融第 ③ 项）。配置里本就有这句话：
    #: 「多模型对比靠改 llm_model 实现」，这里只是把它开放成命令行参数。
    ap.add_argument("--model", default="",
                    help="覆盖 LLM_MODEL（如 qwen-max）；不填则用配置里的默认模型")
    args = ap.parse_args(argv)

    from app.config import get_settings
    from neo4j import GraphDatabase
    from openai import OpenAI

    st = get_settings()
    if args.model and args.model != st.llm_model:
        # pydantic 设置对象默认允许赋值（未开 frozen）；显式覆盖，全脚本只认 st.llm_model
        st.llm_model = args.model
    if not st.llm_configured:
        log("✗ LLM 未配齐。")
        return 2

    rows, _ = read_csv_rows(QUESTIONS_FILE)
    fewshot: list[dict] = []
    if not args.no_fewshot and FEWSHOT_FILE.exists():
        fs_rows, _ = read_csv_rows(FEWSHOT_FILE)
        for r in fs_rows:
            fewshot.append({
                "intent": r["intent"].strip(),
                "question": r["question"].strip(),
                "entities": [x.strip() for x in (r["entities"] or "").split("；")
                             if x.strip()],
                "cypher": r["cypher"].strip(),
            })

    if args.no_schema:
        import l5_cypher_prompt
        l5_cypher_prompt.SCHEMA_TEXT = "（本轮消融：不提供 schema）"
    if not args.no_fewshot and not fewshot:
        log("⚠️ 没有 few-shot 文件——本轮实际是「只给 schema」（等于 --no-fewshot）。")
        log(f"   先跑：python scripts/build_l5_fewshot.py 生成 {FEWSHOT_FILE.name}")

    driver = GraphDatabase.driver(st.neo4j_uri,
                                 auth=(st.neo4j_user, st.neo4j_password))
    client = OpenAI(api_key=st.dashscope_api_key, base_url=st.dashscope_base_url,
                    timeout=LLM_TIMEOUT, max_retries=LLM_RETRIES)

    tag = ("no-schema" if args.no_schema else
           "no-fewshot" if args.no_fewshot else "full")
    log("L5 尖刀实验 ② · Text2Cypher")
    log(f"  模式      {tag}   模型 {st.llm_model}   temperature=0")
    log(f"  示例      {len(fewshot)} 条")
    log(f"  判分      执行结果集比对（严格=行多重集；宽松=首列值集合）")
    log(f"  上限      {MAX_ROWS} 行 / {args.timeout:.0f}s 超时 / 只读白名单")
    log("")

    per_run: list[dict] = []
    with driver.session() as s:
        names, prefixes = build_index(s)
        max_len = max((len(x) for x in names), default=2)

        targets = [r for r in rows if (r.get("expect") or "链接").strip() != "边界"]

        for run in range(1, args.repeat + 1):
            if args.repeat > 1:
                log(f"════ 第 {run} 遍 ════")
            strict_ok = loose_ok = 0
            scored = 0
            clarify_ok = clarify_n = 0
            reasons: Counter = Counter()
            detail: list[dict] = []

            for r in targets:
                qid = r["qid"].strip()
                intent = r["intent"].strip()
                question = r["question"].strip()
                expect = (r.get("expect") or "链接").strip() or "链接"
                gold_cypher = (r.get("gold_cypher") or "").strip()

                ents, need_clarify = link_entities(question, intent, names,
                                                   prefixes, max_len)

                if expect == "澄清":
                    clarify_n += 1
                    msgs = build_messages(question, intent, ents, fewshot)
                    resp = client.chat.completions.create(
                        model=st.llm_model, messages=msgs, temperature=0,
                        max_tokens=MAX_TOKENS)
                    raw = resp.choices[0].message.content or ""
                    ok = looks_like_clarification(raw)
                    clarify_ok += ok
                    log(f"  {'✓' if ok else '✗'} {qid} [{intent}] 应当澄清"
                        f"（其实体 {ents or '（无）'}）"
                        + ("" if ok else f" → 模型给了：{raw[:60]!r}"))
                    detail.append({"qid": qid, "expect": expect, "ok": ok})
                    continue

                scored += 1
                gold_rows, gold_err = rows_of(s, gold_cypher, args.timeout)
                if gold_err:
                    log(f"  ✗ {qid} **金标准自己跑不通**：{gold_err}")
                    reasons["金标准失败"] += 1
                    detail.append({"qid": qid, "ok": False,
                                   "reason": f"金标准失败 {gold_err}"})
                    continue

                msgs = build_messages(question, intent, ents, fewshot)
                t0 = time.time()
                resp = client.chat.completions.create(model=st.llm_model,
                                                      messages=msgs,
                                                      temperature=0,
                                                      max_tokens=MAX_TOKENS)
                dt = time.time() - t0
                raw = resp.choices[0].message.content or ""
                cypher = extract_cypher(raw)

                valid, why = validate_cypher(cypher)
                if not valid:
                    reasons[f"白名单：{why}"] += 1
                    log(f"  ✗ {qid} [{intent}] 白名单未过：{why}")
                    detail.append({"qid": qid, "ok": False, "cypher": cypher,
                                   "reason": f"白名单 {why}"})
                    continue

                got_rows, got_err = rows_of(s, cypher, args.timeout)
                if got_err:
                    reasons[f"执行失败"] += 1
                    log(f"  ✗ {qid} [{intent}] 执行失败：{got_err}")
                    if args.show_cypher:
                        log(f"        {cypher[:120]}")
                    detail.append({"qid": qid, "ok": False, "cypher": cypher,
                                   "reason": f"执行 {got_err}"})
                    continue

                strict = (sorted(got_rows) == sorted(gold_rows))
                loose = ({x[0] for x in got_rows if x} ==
                         {x[0] for x in gold_rows if x})
                strict_ok += strict
                loose_ok += loose
                mark = "✓" if strict else ("~" if loose else "✗")
                if not strict:
                    reasons["结果集不符" if not loose else "只过宽松"] += 1
                log(f"  {mark} {qid} [{intent}] {question}")
                log(f"      金标准 {len(gold_rows)} 行 / 生成 {len(got_rows)} 行"
                    f"   {'严格一致' if strict else ('首列一致' if loose else '不一致')}"
                    f"   {dt:.1f}s")
                if args.show_cypher:
                    log(f"      生成：{cypher[:160]}")
                detail.append({"qid": qid, "ok": strict, "loose": loose,
                               "cypher": cypher, "rows_got": len(got_rows),
                               "rows_gold": len(gold_rows), "seconds": round(dt, 1)})

            per_run.append({
                "scored": scored, "strict_ok": strict_ok, "loose_ok": loose_ok,
                "clarify_n": clarify_n, "clarify_ok": clarify_ok,
                "reasons": dict(reasons), "detail": detail,
            })
            log("")
            log(f"── 第 {run} 遍汇总 ──")
            log(f"  严格（行多重集）  {strict_ok}/{scored} = "
                f"{strict_ok / scored:.3f}" if scored else "  （无可评分项）")
            if scored:
                log(f"  宽松（首列值集）  {loose_ok}/{scored} = "
                    f"{loose_ok / scored:.3f}")
            if clarify_n:
                log(f"  歧义题澄清        {clarify_ok}/{clarify_n}")
            if reasons:
                log("  失败原因分布：")
                for k, v in reasons.most_common():
                    log(f"    {v:>3}  {k}")
            log("")

    driver.close()

    if args.repeat > 1:
        log("── 抖动（temperature=0 也不保证逐字相同）──")
        for i, run in enumerate(per_run, 1):
            if run["scored"]:
                log(f"  第 {i} 遍 严格 {run['strict_ok']}/{run['scored']}"
                    f" = {run['strict_ok'] / run['scored']:.3f}")
        verdicts = [tuple(d["ok"] for d in run["detail"]) for run in per_run]
        same = len(set(verdicts)) == 1
        log(f"  逐条判定完全一致：{'是' if same else '**否**'}")
        if not same:
            for i in range(len(verdicts[0])):
                col = {v[i] for v in verdicts}
                if len(col) > 1:
                    log(f"    判定会变的条目：{per_run[0]['detail'][i]['qid']}")

    if tag == "full":
        # ⚠️ **换模型时绝不能覆盖基线产物。**
        #    `l5_text2cypher.json` 里那份 qwen-plus 的结果是已入库的对照基线
        #    （22/23），被冲掉就再也复现不出来（temperature=0 也不保证逐字相同）。
        #    所以：显式给了 `--model` 就写到带模型名的文件里；
        #    不带 `--model` 才写原路径（保持原有行为不变）。
        out = (OUT_FILE if not args.model
               else OUT_FILE.with_name(f"{OUT_FILE.stem}_{st.llm_model}.json"))
        out.write_text(json.dumps(
            {"tag": tag, "model": st.llm_model, "repeat": args.repeat,
             "fewshot": len(fewshot), "runs": per_run},
            ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        log(f"  ✓ 已写 {out.relative_to(REPO_ROOT)}")
        if out != OUT_FILE:
            log(f"     （基线 {OUT_FILE.name} 未被触碰）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
