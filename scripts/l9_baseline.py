#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L9 · 消融实验第 ① 项：纯 LLM 基线
==================================

§13.4/§515 要求「跑出消融实验对比数字」，第一项是 **纯 LLM vs 图谱增强 LLM**。
本项目此前**完全没有这一臂的产物**（`data/eval/` 下没有任何 baseline 文件），
所以那些 95.7% 一直是"自己考自己"。本脚本补上对照组。

## 公平性：只动一个变量

| 维度 | 图谱增强臂 | 纯 LLM 基线臂 |
|---|---|---|
| 模型 | `qwen-plus` | `qwen-plus`（**同一模型**） |
| temperature / max_tokens | 0 / 400 | 0 / 400（**同参数**） |
| 输入 | 问题 + **图谱查询结果** | 问题（**无任何图谱信息**） |
| 约束 | 只能用结果里的信息 | 无此约束（允许用自身知识） |

**唯一的差别就是"有没有图谱证据"**——这样涨幅/跌幅才能归因到图谱，而不是
"换了提示词碰巧变好"（决策 #34 的纪律）。

## 基线臂的提示词为什么不写"不许编造"

如果也给它加上"不得补充、不得推断"，那它就**不是**纯 LLM 了——那个约束本身就是
图谱系统的一部分。**基线必须是"一个普通助手被问到同样问题时的回答"**，
包括它会自信地编。要观察的正是这个差别。

## 评分维度（人工填，两列就够）

* `哪个更可靠` —— 图谱 / 基线 / 相当
* `基线有没有编造` —— 有 / 无

第二列是这次消融的**核心**：如果纯 LLM 确实会编出图谱里没有的药名/剂量/禁忌，
那就是"图谱增强"最有力的证据。

用法
----
    python scripts/l9_baseline.py            # 生成（会调 LLM，约 1–2 分钟）
    python scripts/l9_baseline.py --dry-run  # 只打印题号和提示词，不调模型
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys

from opencmkg_io import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "backend"))

EVAL = REPO_ROOT / "data" / "eval"
ANSWERS = EVAL / "l5_answers.json"
OUT_JSON = EVAL / "l9_baseline.json"
OUT_CSV = EVAL / "l9_baseline_review.csv"

#: ⚠️ 基线臂的提示词**刻意不含任何"不得编造"的约束**——见模块 docstring。
#:
#: ⚠️ 三个片段**必须全是普通字符串**（单花括号）。第一版把中间那行写成了 f-string
#:    并用 `{{question}}` 转义，结果 f-string 先把它解成了 `{question}`，
#:    而后面普通字符串里的 `{{disclaimer}}` 仍保留双括号，再 `.format()` 就成了
#:    **字面的 `{disclaimer}`** —— 实测 Q24 的回答直接输出了
#:    `Acetazolamide tablets{disclaimer}`。混用 f-string 与普通字符串的转义，
#:    是这类 bug 的固定来源。
BASELINE_PROMPT = (
    "你是医药问答助手。请回答用户的问题。\n\n"
    "用户问题：{question}\n\n"
    "回答末尾原样附上这句免责声明：{disclaimer}\n"
    "只输出回答本身，不要解释你的做法。"
)

#: 基线臂用的**中性**免责声明。
#:
#: ⚠️ **不能用图谱臂那句**——它写着「来自**知识图谱**」，等于告诉基线"你有图谱"，
#:    那会污染对照组。免责声明是样板文字，不是医学信息，所以这里换掉不算改动变量；
#:    但两个臂的这句**确实不同**，报告里必须写出来，不能装作一样。
NEUTRAL_DISCLAIMER = "以上内容仅供参考，不能替代医生诊断。"


def log(msg: str = "") -> None:
    print(msg, flush=True)


def get_disclaimer() -> str:
    """基线臂的免责声明（中性版，见 NEUTRAL_DISCLAIMER 的说明）。"""
    return NEUTRAL_DISCLAIMER


def graph_answer_of(item: dict) -> tuple[str, str]:
    """取出图谱增强臂的两个版本（模板 / LLM）与证据行数。"""
    a = item.get("answers") or {}
    tpl = ((a.get("template") or {}).get("text") or "").strip()
    llm = ((a.get("llm") or {}).get("text") or "").strip()
    rows = len((item.get("evidence") or {}).get("rows") or [])
    return tpl, llm, rows


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="L9 纯 LLM 基线")
    ap.add_argument("--dry-run", action="store_true", help="不调模型，只打印")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题（调试用）")
    args = ap.parse_args(argv)

    if not ANSWERS.exists():
        log(f"✗ 缺 {ANSWERS.relative_to(REPO_ROOT)}")
        return 2
    data = json.loads(ANSWERS.read_text(encoding="utf-8"))
    items = data.get("results") or []
    if args.limit:
        items = items[: args.limit]

    disclaimer = get_disclaimer()
    log("L9 · 纯 LLM 基线（消融第 ① 项）")
    log(f"  对照组：{'（dry-run，不调模型）' if args.dry_run else 'qwen-plus'}")
    log(f"  题数：{len(items)}（与图谱增强臂**同一批题**，按 qid 配对）")
    log(f"  ⚠️ 唯一变量＝有没有图谱证据；模型与参数与图谱臂完全相同")
    log("")

    rows_out: list[dict] = []
    if not args.dry_run:
        from app.config import get_settings
        from openai import OpenAI

        st = get_settings()
        client = OpenAI(api_key=st.dashscope_api_key,
                        base_url=st.dashscope_base_url,
                        timeout=60.0, max_retries=3)

    for it in items:
        qid = it.get("qid")
        question = it.get("question") or ""
        tpl, llm_txt, nrows = graph_answer_of(it)
        prompt = BASELINE_PROMPT.format(question=question,
                                        disclaimer=disclaimer)
        if args.dry_run:
            log(f"  [{qid}] {question}")
            log(f"        证据行数 {nrows}")
            continue
        resp = client.chat.completions.create(
            model=st.llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0, max_tokens=400)
        base = (resp.choices[0].message.content or "").strip()
        rows_out.append({
            "qid": qid, "intent": it.get("intent"), "question": question,
            "evidence_rows": nrows,
            "graph_template": tpl, "graph_llm": llm_txt,
            "baseline": base,
            "model": st.llm_model,
            "prompt": prompt,
            "params": {"temperature": 0, "max_tokens": 400},
        })
        log(f"  [{qid}] 基线 {len(base)} 字 / 图谱模板 {len(tpl)} 字")

    if args.dry_run:
        log("")
        log("  （dry-run 结束，未写文件）")
        return 0

    OUT_JSON.write_text(json.dumps(
        {"method": "pure-llm-baseline", "model": st.llm_model,
         "note": ("基线臂不给任何图谱证据；模型与参数与图谱增强臂完全相同，"
                  "**唯一变量＝有没有图谱证据**"),
         "results": rows_out},
        ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    #: 人工评分表。**两列就够**——列太多会导致评分疲劳、反而不可靠。
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["qid", "intent", "question", "证据行数",
                    "图谱增强回答（模板）", "纯 LLM 回答（无图谱）",
                    "哪个更可靠（图谱/基线/相当）", "基线有没有编造（有/无）", "备注"])
        for r in rows_out:
            w.writerow([r["qid"], r["intent"], r["question"], r["evidence_rows"],
                        r["graph_template"], r["baseline"], "", "", ""])

    log("")
    log(f"  ✓ 原始数据 {OUT_JSON.relative_to(REPO_ROOT)}")
    log(f"  ✓ 评分表   {OUT_CSV.relative_to(REPO_ROOT)}")
    log("")
    log("  下一步（人工，约半小时）：在评分表里逐行填两列——")
    log("    · 哪个更可靠：图谱 / 基线 / 相当")
    log("    · 基线有没有编造：有 / 无")
    log("  第二列是这次消融的核心：基线若编出图谱里没有的药名/禁忌，")
    log("  那就是『图谱增强』最有力的证据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
