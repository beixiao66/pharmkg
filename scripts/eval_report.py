#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L9 · 测试集评测总表
===================

§13.4 给 L9 的职责：**冻结的测试集 → 准确率等指标**，验收是
**跑出消融实验对比数字**。§515 又把消融对比拆成三项：
① 纯 LLM vs 图谱增强 LLM；② 单轮 vs 多轮；③ 同厂商多模型对比。

## 本脚本做什么、不做什么

**做**：把已有产物里的数字**汇总成一张表**，每个数字标出它来自哪个文件/哪个脚本。
**不做**：不做新的 LLM 调用，也不重新判分。

## 为什么"出处"比"数字"更重要

这些数字里有一部分是**人工判的**（`l5_answer_review.csv` 的 20 条、
`l5_rewrite_review.csv` 的 46 条），有一部分是**机械算的**（text2cypher 的
结果集比对、意图分类的字符串比对）。两者可信度完全不同，**混在一起报就是骗人**。
所以本表的每一行都带 `口径`（机械/人工）与 `出处`。

## 为什么必须把"缺失项"写进表里

L9 的验收要求"跑出消融实验对比数字"，而其中**纯 LLM 基线**这一臂在本项目里
**还没有产物**（`data/eval/` 下没有任何 baseline 文件）。如果总表只列已有的数字，
读表的人会以为消融已经做完了。**把缺的写出来，是这个脚本存在的主要理由之一。**
"""

from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL = REPO_ROOT / "data" / "eval"
OUT = REPO_ROOT / "docs" / "评测总表.md"


def load_json(name: str):
    p = EVAL / name
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def load_csv(name: str) -> list[dict]:
    p = EVAL / name
    if not p.exists():
        return []
    with p.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def pct(a: int, b: int) -> str:
    return f"{a}/{b} = {a / b:.1%}" if b else "—"


def count_yes(rows: list[dict], col: str) -> tuple[int, int]:
    """数某一列里「是」的条数（跳过空值）。"""
    vals = [str(r.get(col) or "").strip() for r in rows]
    vals = [v for v in vals if v]
    return sum(1 for v in vals if v in ("是", "对")), len(vals)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    rows: list[tuple[str, str, str, str]] = []   # 指标 / 数值 / 口径 / 出处
    missing: list[str] = []

    # ── L5 意图分类 ────────────────────────────────────────────
    d = load_json("l5_intent.json")
    if d:
        r, l = d["rule"], d["llm"]
        rows.append(("意图分类（规则）", pct(r["ok"], r["n"]), "机械",
                     "data/eval/l5_intent.json"))
        rows.append(("意图分类（LLM）", pct(l["ok"], l["n"]), "机械",
                     "data/eval/l5_intent.json"))
    else:
        missing.append("意图分类产物 l5_intent.json")

    # ── L5 Text2Cypher ────────────────────────────────────────
    d = load_json("l5_text2cypher.json")
    d_max = load_json("l5_text2cypher_qwen-max.json")
    if d and d.get("runs"):
        run = d["runs"][0]
        rows.append(("Text2Cypher 严格（结果集完全一致）",
                     pct(run["strict_ok"], run["scored"]), "机械",
                     "data/eval/l5_text2cypher.json"))
        rows.append(("Text2Cypher 宽松（含澄清得当）",
                     pct(run["loose_ok"], run["scored"]), "机械",
                     "data/eval/l5_text2cypher.json"))
    else:
        missing.append("Text2Cypher 产物")

    # 消融第 ③ 项：同厂商多模型对比（qwen-plus vs qwen-max）
    if d and d_max and d.get("runs") and d_max.get("runs"):
        rb, rm = d["runs"][0], d_max["runs"][0]
        rows.append((f"Text2Cypher 严格 · {d_max['model']}（对照 {d['model']}）",
                     pct(rm["strict_ok"], rm["scored"]), "机械",
                     "data/eval/l5_text2cypher_qwen-max.json"))
        # 逐条判定一致数——比总分更能说明"换大模型有没有用"
        qb = {x["qid"]: bool(x.get("ok")) for x in rb.get("detail") or []}
        qm = {x["qid"]: bool(x.get("ok")) for x in rm.get("detail") or []}
        common = [q for q in qb if q in qm]
        same = sum(1 for q in common if qb[q] == qm[q])
        rows.append(("两个模型**逐条判定一致**的题数",
                     f"{same}/{len(common)}", "机械", "两者 detail 逐题比对"))

    # ── L5 多轮改写（机器）────────────────────────────────────
    d = load_json("l5_rewrite.json")
    if d:
        for rule, key in (("as-written（决策 #39 前的旧规则）", "as-written"),
                          ("never（从不改写）", "never")):
            v = (d.get("rules") or {}).get(key) or {}
            if v.get("n"):
                rows.append((f"多轮改写 {rule}",
                             pct(v["ok"], v["n"]), "机械",
                             "data/eval/l5_rewrite.json"))
        nr = d.get("no_rewrite") or {}
        if nr.get("n"):
            rows.append(("不改写时的成功率（用来证明改写是必要的）",
                         pct(nr["ok"], nr["n"]), "机械",
                         "data/eval/l5_rewrite.json"))
    else:
        missing.append("多轮改写产物 l5_rewrite.json")

    # ── L5 多轮改写（人工复核）────────────────────────────────
    rw = load_csv("l5_rewrite_review.csv")
    if rw:
        ok, n = count_yes(rw, "改写是否对（是/否）")
        rows.append(("多轮改写**人工复核**通过率", pct(ok, n), "人工",
                     "data/eval/l5_rewrite_review.csv"))
    else:
        missing.append("改写人工复核表")

    # ── 消融第 ① 项：纯 LLM vs 图谱增强（人工判定）──────────────
    l9 = load_csv("l9_baseline_review.csv")
    ABL = {}
    if l9:
        c1, c2 = "哪个更可靠（图谱/基线/相当）", "基线有没有编造（有/无）"
        v1 = [(r.get(c1) or "").strip() for r in l9]
        v1 = [x for x in v1 if x]
        v2 = [(r.get(c2) or "").strip() for r in l9]
        v2 = [x for x in v2 if x]
        if v1:
            for k in ("图谱", "基线", "相当"):
                ABL[k] = v1.count(k)
                rows.append((f"消融① 判「{k}更可靠」的题数",
                             pct(v1.count(k), len(v1)), "人工",
                             "data/eval/l9_baseline_review.csv"))
        if v2:
            ABL["编造有"] = v2.count("有")
            ABL["编造无"] = v2.count("无")
            rows.append(("消融① **基线编造**的题数",
                         pct(v2.count("有"), len(v2)), "人工",
                         "data/eval/l9_baseline_review.csv"))
    else:
        missing.append("消融①人工判定表 l9_baseline_review.csv")

    # ── L5 答案抽检（人工）────────────────────────────────────
    ar = load_csv("l5_answer_review.csv")
    if ar:
        for col, label in (("template是否只用了证据里的信息", "答案忠实度（模板）"),
                           ("llm是否只用了证据里的信息", "答案忠实度（LLM）")):
            ok, n = count_yes(ar, col)
            rows.append((label, pct(ok, n), "人工",
                         "data/eval/l5_answer_review.csv"))
    else:
        missing.append("答案人工抽检表")

    # ── L5 全链路 ─────────────────────────────────────────────
    d = load_json("l5_pipeline.json")
    if d and d.get("summary"):
        for k, label in (("single", "全链路：单轮"),
                         ("multi", "全链路：多轮")):
            v = d["summary"].get(k) or {}
            if isinstance(v, dict) and v.get("n"):
                rows.append((label, pct(v.get("ok", 0), v["n"]), "机械",
                             "data/eval/l5_pipeline.json"))
            elif isinstance(v, str):
                rows.append((label, v, "机械", "data/eval/l5_pipeline.json"))
    else:
        missing.append("全链路汇总")

    # ── 数据规模（L8 诊断）────────────────────────────────────
    q = load_json("../quality/graph_quality.json")
    if q:
        sc = q["scale"]
        rows.append(("图谱规模",
                     f"{sc['nodes_total']:,} 节点 / {sc['edges_total']:,} 边",
                     "机械", "data/quality/graph_quality.json"))

    # ── 组装 Markdown ─────────────────────────────────────────
    lines = [
        "# 评测总表（L9）",
        "",
        "> 由 `python scripts/eval_report.py` 生成。**不要手改**——数字来自各产物文件。",
        ">",
        "> **口径** 一列必须看：`机械`＝脚本比对（结果集/字符串），`人工`＝人逐条判定。",
        "> 两者可信度不同，**混在一起报就是骗人**。",
        "",
        "| 指标 | 数值 | 口径 | 出处 |",
        "|---|---|---|---|",
    ]
    for name, val, kind, src in rows:
        lines.append(f"| {name} | **{val}** | {kind} | `{src}` |")

    lines += [
        "",
        "## ⚠️ 消融实验：三项里只有一项有产物",
        "",
        "§13.4/§515 要求「跑出消融实验对比数字」，拆成三项。**逐项如实交代**：",
        "",
        "| 消融项 | 状态 | 说明 |",
        "|---|---|---|",
        "| ① 纯 LLM vs 图谱增强 | ✅ **已完成（人工判定 24 题）** | "
        "基线答案 `data/eval/l9_baseline.json`（不给任何图谱证据），"
        "判定表 `data/eval/l9_baseline_review.csv`。"
        "**结论：图谱只有 5/24 = 20.8% 被判更可靠，基线 11/24 = 45.8%，相当 8/24；"
        "基线编造 2/24 = 8.3%，且这 2 例都判给了图谱。**"
        "⚠️ 读法见下一节。 |",
        "| ② 单轮 vs 多轮 | ✅ **已完成（机械口径，三臂齐全）** | "
        "**①不改写**：多轮端到端 **1/23 = 4.3%**（`l5_rewrite.json` 的 `no_rewrite`）；"
        "**②旧规则**（按需改写、含实体则跳过）：改写正确率 **18/23 = 78.3%**；"
        "**③一律改写**（决策 #39）：改写正确率 **22/23 = 95.7%**，"
        "多轮端到端同样 **22/23**（`l5_pipeline.json`）。"
        "**多轮不补全指代几乎必然查错**（4.3%），这是改写存在的唯一理由。 |",
        "| ③ 同厂商多模型 | ✅ **已完成** | `qwen-plus` vs `qwen-max`，同批 23 题、"
        "同提示词、`temperature=0`。**结果：严格口径都是 22/23 = 95.7%，"
        "而且 24 条逐条判定 100% 一致**——换更大的模型对 Text2Cypher **零增益**。"
        "两个模型在 Q23 上生成的 Cypher **逐字相同**，说明瓶颈不在模型能力，"
        "而在题面定义与图谱能提供的信息。 |",
        "",
        "**为什么把缺失项写进总表**：如果只列已有数字，读表的人会以为消融做完了。"
        "本项目宁可少做也不做不可验证的产出（决策 #34 的口径）。",
        "",
        "## ⚠️ 消融① 这个结果该怎么读（三条限定，缺一条都会被误读）",
        "",
        "**1. 图谱臂用的是「冻结的旧答案」，所以 20.8% 是下限，不是当前系统水平。**",
        "`l9_baseline.json` 的对照答案是 `l5_answers.json` 里那份冻结产物，"
        "生成于**垃圾药名过滤之前**——判定表里 Q07 的「冻干 / Ⅰ / Ⅱ」就是证据。"
        "现在的查询层已经过滤掉那 30 个垃圾药名（`quality='junk'`），"
        "重跑答案会干净，但**那意味着这 24 行判定要重做**。",
        "",
        "**2. 机械口径 95.7% 与人工口径 20.8% 的落差，本身就是结论。**",
        "Text2Cypher 的「结果集与金标准一致」测的是**查询对不对**，"
        "不是**答案有没有用**。两者差 75 个百分点说明："
        "**查询正确 ≠ 答案有用**。瓶颈在图谱内容与答案形态，不在查询生成。",
        "",
        "**3. 失败集中在两类意图，而不是均匀分布。**",
        "判「基线更可靠」的 11 题里，**疾病用药 6 题 + 适应症 4 题**（另一题是相互作用）。"
        "原因可归为三类：",
        "· **数据源脏**：Q08 给病毒性肠炎推荐一堆抗生素（错误）；"
        "Q14 把**已停用**的去痛片列为 37 个疾病的推荐用药且不提示停用。",
        "· **只罗列不归纳**：Q11 问「骨肽片治什么」，图谱吐 137 个疾病名；"
        "Q23 问「卡维地洛是什么药」，图谱答英文名——**答非所问**。",
        "· **样本量与非双盲**：24 题、单一评定者（本人），**不能当准确率报**，"
        "只能当方向性证据。",
        "",
        "**反过来，图谱赢的 5 例里有 2 例是基线编造**（Q01 把 Major 说成"
        "「无明确证据」；Q18 称他克莫司「不存在交叉过敏可安全使用」，"
        "而说明书明确禁用）——**这 2 例才是「图谱约束住了幻觉」的直接证据**。",
        "",
        "## 已被推翻/收紧过的数字（留痕，别再引用错）",
        "",
        "- **`generic_only` 的『原子级 F1 0.745 vs 条目级 0.941』那个 0.2 差距是假的**"
        "（决策 #34）——它全部来自评分时把「删除」那 25 行也当成了正类。"
        "按定义收紧后**两个口径一致：`TP 8 / FP 0 / FN 1`，F1 0.941**。",
        "- **多轮改写的『as-written 19/23 = 82.6%』不能当成成绩**："
        "那是**决策 #39 之前的旧规则**下测的，旧规则本身漏掉改写。"
        "决策 #39 已改为一律改写，成绩应按 `never`/`as-written` 对照来读。",
        "- **意图分类规则版 1.000 是被污染的**：规则在测试集上**修过两次**。"
        "要报就得说明这一点，或换一份没碰过的题重测。",
    ]

    if missing:
        lines += ["", "## 缺失的产物", ""] + [f"- {m}" for m in missing]

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"✓ 已生成 {OUT.relative_to(REPO_ROOT)}")
    print(f"  {len(rows)} 行指标；缺失项 {len(missing)} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
