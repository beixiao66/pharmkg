#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L5 · 多轮测试集（从已冻结的 28 条**反向构造**，用户拍板）
=========================================================

**为什么反向构造而不是另写一套**：金标准自包含问题就是那 28 条里**已经逐条验证过**的
那一条（`verify_l5_questions.py` 验过"图里查得出东西"）。另写一套就得重验一遍，
而重验这件事上一轮已经证明它值这个工时——但没必要做两遍。

构造法（机械，可复现）
----------------------
* **第 1 轮**：一句"引出实体"的话（按意图给模板），把实体名说出口；
* **第 2 轮**：把原问题里的**主实体换成代词**（它 / 这个药 / 这个病），其余照旧。
* **金标准自包含问题＝原问题**（一字不改）。

于是这条对话**必须**经过改写才能答对——第 2 轮里已经没有药名了。

⚠️ 所以还要验一件事：**第 2 轮确实不含那个实体**。听起来显然，但如果模板没换干净
（比如"大环内酯类药物过敏者"里还留着字），改写就变成可跳过、测试也就白做了。
`build` 时会用**同一个实体链接器**去扫第 2 轮，扫出主实体就报错。

用法
----
    python scripts/build_l5_multiturn.py
"""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

from insert_io import read_csv_rows
from opencmkg_io import REPO_ROOT
from l5_entity_linking import INTENT_LABELS, build_index, resolve, scan

sys.path.insert(0, str(REPO_ROOT / "backend"))

QUESTIONS_FILE = REPO_ROOT / "data" / "eval" / "l5_questions.csv"
OUT_FILE = REPO_ROOT / "data" / "eval" / "l5_multiturn.csv"

FIELDS = ["mid", "qid", "intent", "turn1", "turn2", "gold_question",
          "must_rewrite", "notes"]

#: 第 1 轮的"引出实体"模板。按意图分，措辞尽量像真人随手说的。
#: ``{e}`` = 主实体（第 2 轮里要被代词替掉的那个）。
OPENERS: dict[str, str] = {
    "相互作用": "我在吃 {e}，想问问用药的事。",
    "疾病用药": "家里老人查出 {e}，我查一下。",
    "适应症": "医生给我开了 {e}。",
    "禁忌": "{e} 这个药我在吃。",
    "药品信息": "我在用 {e} 这个药。",
}

#: 主实体的代词（第 2 轮用它替换实体名）。
PRONOUN: dict[str, str] = {
    "相互作用": "它",
    "疾病用药": "这个病",
    "适应症": "它",
    "禁忌": "它",
    "药品信息": "它",
}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    from app.config import get_settings
    from neo4j import GraphDatabase

    rows, _ = read_csv_rows(QUESTIONS_FILE)
    st = get_settings()
    driver = GraphDatabase.driver(st.neo4j_uri,
                                 auth=(st.neo4j_user, st.neo4j_password))

    log("L5 多轮测试集（从 28 条反向构造）")
    log("")

    out: list[dict] = []
    bad: list[str] = []
    with driver.session() as s:
        names, prefixes = build_index(s)
        max_len = max((len(x) for x in names), default=2)

        for r in rows:
            qid = r["qid"].strip()
            intent = r["intent"].strip()
            expect = (r.get("expect") or "链接").strip()
            # ⚠️ `澄清` 那一条（Q04 茶碱）也要跳过：它的实体本来就扫不出来
            #    （3 个候选 → 需澄清），拿它构造多轮会得到"金标准里扫不出主实体"。
            #    它与"边界"一样，不属于能端到端跑通的题型。
            if expect != "链接":
                continue
            gold_q = r["question"].strip()
            gold_nodes = [x.strip() for x in (r.get("gold_nodes") or "").split("；")
                          if x.strip()]
            if not gold_nodes:
                continue

            allowed = INTENT_LABELS.get(intent, ("Drug", "Disease", "Population"))
            # 主实体 = 金标准里的**第一个药/病**；`相互作用`/`禁忌` 有第二个实体时，
            # 第一个才是被代词替掉的那个（第 1 轮引出它）。
            main_e = gold_nodes[0]
            rest = gold_nodes[1:]

            turn1 = OPENERS.get(intent, "我在问 {e}。").format(e=main_e)

            # ⚠️ 第 2 轮不能做**字符串替换**（`gold_q.replace(main_e, "它")`）：
            #    问题是 `卡维地洛和依度沙班…`，而规范名是 `卡维地洛片`——
            #    替换匹配不上，实体原样留在第 2 轮，改写就变成可跳过、测试白做。
            #    实测被下面的验证抓到了（Q01/Q23）。
            #    正确做法：**用同一个扫描器按位置替换**——扫到什么词、就替什么词。
            pronoun = PRONOUN.get(intent, "它")
            spans = []
            for a, b, piece, kind in scan(gold_q, names, prefixes, max_len):
                res = resolve(piece, kind, allowed, names, prefixes)
                if res["node"] == main_e:
                    spans.append((a, b))
            turn2 = gold_q
            for a, b in reversed(spans):          # 倒着替，索引才不会错位
                turn2 = turn2[:a] + pronoun + turn2[b:]
            must_rewrite = bool(spans)

            # ── 验：第 2 轮里确实扫不出主实体 ──
            found = []
            for _a, _b, piece, kind in scan(turn2, names, prefixes, max_len):
                res = resolve(piece, kind, allowed, names, prefixes)
                if res["node"]:
                    found.append(res["node"])
            if main_e in found:
                bad.append(f"{qid}: 第 2 轮仍能扫出主实体 {main_e}")
                must_rewrite = False
            if not spans:
                bad.append(f"{qid}: 金标准问题里**根本没扫出**主实体 {main_e}"
                           f"（扫到的是 {found}）——说明金标准与词表不一致")

            out.append({"mid": f"M{len(out) + 1:02d}", "qid": qid, "intent": intent,
                        "turn1": turn1, "turn2": turn2, "gold_question": gold_q,
                        "must_rewrite": must_rewrite,
                        "notes": ("其余实体：" + "、".join(rest)) if rest else ""})

    driver.close()

    log(f"  构造 {len(out)} 组两轮对话")
    by_intent: dict[str, int] = {}
    for o in out:
        by_intent[o["intent"]] = by_intent.get(o["intent"], 0) + 1
    log(f"  意图覆盖 {by_intent}")
    log("")
    log("  样例（前 3 组）：")
    for o in out[:3]:
        log(f"    {o['mid']} [{o['intent']}]")
        log(f"      第1轮  {o['turn1']}")
        log(f"      第2轮  {o['turn2']}")
        log(f"      金标准 {o['gold_question']}")
    log("")
    if bad:
        log("  ✗ 有问题：")
        for b in bad:
            log(f"    {b}")
        log("  （第 2 轮没把实体换干净 → 改写会变成可跳过 → 测试白做）")
        return 1

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\n")
    w.writeheader()
    for o in out:
        w.writerow(o)
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(buf.getvalue(), encoding="utf-8", newline="\n")
    log(f"  ✓ 已冻结 {OUT_FILE.relative_to(REPO_ROOT)}")
    log("  ✓ 每组都验过：第 2 轮扫不出主实体 → 必须改写")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
