#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L5 尖刀实验 ④：意图识别（§6.1 第 ① 步）
=========================================

这一步**至今没做过**——前面三轮（实体链接 / Text2Cypher / 回答 / 改写）里，
意图一直是**从测试集读进去的已知输入**，为的是分层定位。现在补上它，
L5 的环节才算齐。

两种方法（用户拍板：LLM 为主 + 规则法作对照）
-----------------------------------------------
* **规则法**：零成本、完全可解释、可断言；口语变体多了会漏。
* **LLM 分类**：一句提示词给 6 类定义，一次调用返回类别名。

**为什么值得花那次调用**：意图决定两件事——实体链接允许哪些标签、
证据查询用哪一条。判错的代价会往后传。

评测只用 23 条
--------------
那 4 条标着「其他」的题（`expect=边界`）**不参与**（用户拍板）；
另外 `expect=澄清` 的 Q04 也不参与（它是澄清题，不属于六类里任何一类）：
其中 3 条其实是标错了（`阿司匹林能治癌症吗？` 是适应症、`感冒了吃什么药好？` 是疾病用药、
`孕妇能吃这个药吗？` 是无上下文的指代题），只有 `今天上海的天气怎么样？` 真属域外。
**拿一套明知有 3 条错的标签去评意图识别，等于拿脏尺子量。**

代价说清楚：23 条、每类 4–6 条，**样本很小**，数字只能当参考，不能当结论。

用法
----
    python scripts/l5_intent.py
"""

from __future__ import annotations

import argparse
import io
import re
import json
import sys
from collections import Counter

from insert_io import read_csv_rows
from opencmkg_io import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "backend"))

QUESTIONS_FILE = REPO_ROOT / "data" / "eval" / "l5_questions.csv"
OUT_FILE = REPO_ROOT / "data" / "eval" / "l5_intent.json"

INTENTS = ("相互作用", "适应症", "疾病用药", "禁忌", "药品信息", "其他")

DEFINITIONS = """\
- 相互作用：两种（或多种）药之间能不能一起用 / 同服 / 合用有没有风险
- 禁忌：某个药禁用于什么情况、哪些人不能用；或"某情况的人能不能用某药"
- 适应症：某个药**能治什么病**（药 → 病）
- 疾病用药：某个**病该用什么药**（病 → 药）
- 药品信息：某个药本身的信息，如英文名、别名、是什么药
- 其他：与上面都不沾边（域外问题、无法确定在问什么）
"""

#: 规则法。**顺序有意义**：先判更"专"的，再判更"泛"的。
#: 例如 `类风湿关节炎可以用哪些药？` 里既有"哪些药"也可能被"能用吗"之类的词碰到，
#: 所以把「相互作用」放最前、「疾病用药」放最后。
RULES: list[tuple[str, tuple[str, ...]]] = [
    # ⚠️ 这里的写法是**正则**不是固定串。踩过：
    #    `能用吗` 在 `孕妇能用乌帕达西替尼吗？` 里**不是子串**——药名夹在中间。
    #    "能不能用" 是**模式**（能…吗），不是词。同理 `能打吗`/`能吃吗`。
    ("相互作用", ("相互作用", "一起吃", "同服", "合用", "一起用", "配着吃", "能一起",
                  "需要.{0,4}注意", "有.{0,4}风险")),
    ("禁忌", ("禁忌", "禁用", "慎用", "过敏",
              "能[用吃打服的].{0,12}吗", "不[能可].{0,10}[用吃打服]")),
    ("适应症", ("治什么", "能治", "适应症", "用于哪些疾病", "治啥", "主治")),
    ("疾病用药", ("用什么药", "吃什么药", "哪些药", "推荐什么", "该用什么",
                  "可以用什么", "用什么")),
    # ⚠️ 「药品信息」必须排在「疾病用药」**之后**：
    #    第一版它在前，而关键词里有裸的 `什么药`——于是
    #    `劳力性心绞痛推荐用什么药？` 被抢成「药品信息」（实测 4 条全错）。
    #    修法两条：① 删掉与 `是什么药` 重复、却更宽的 `什么药`；
    #    ② 把它挪到后面。**具体优先于宽泛**，这条对规则表是通用的。
    ("药品信息", ("英文名", "是什么药", "别名", "说明书", "成分")),
]


def log(msg: str = "") -> None:
    print(msg, flush=True)


def rule_intent(text: str) -> str:
    """关键词/模式规则。命中第一个匹配的类别；都不中就是「其他」。

    ⚠️ 键按**正则**匹配，不是子串包含。原因是"能不能用"这类表达里
    会插进药名（`孕妇能用X吗？`），当成固定串就永远匹配不上。
    """
    for intent, keys in RULES:
        for k in keys:
            if re.search(k, text):
                return intent
    return "其他"


def llm_intent(client, model: str, text: str, temperature: float = 0) -> str:
    """LLM 分类：一次调用，只回类别名。

    ⚠️ 解析时**按类别名匹配**而不是"取第一个词"：模型偶尔会回
    `类别：禁忌` 或 `禁忌（因为…）`，直接取第一个词会拿到 `类别'。`
    """
    prompt = ("你要判断一句中文医药问题属于下面哪一类。\n\n"
              f"{DEFINITIONS}\n"
              f"问题：{text}\n\n"
              "只输出类别名本身（六个词之一），不要解释、不要标点。")
    resp = client.chat.completions.create(
        model=model, temperature=temperature, max_tokens=20,
        messages=[{"role": "user", "content": prompt}])
    raw = (resp.choices[0].message.content or "").strip()
    for name in INTENTS:
        if name in raw:
            return name
    return f"?{raw[:12]}"


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="L5 意图识别尖刀实验")
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args(argv)

    from app.config import get_settings
    from openai import OpenAI

    st = get_settings()
    if not st.llm_configured:
        log("✗ LLM 未配齐。")
        return 2

    rows, _ = read_csv_rows(QUESTIONS_FILE)
    # 只用 expect=链接 的那 23 条（4 条边界 + 1 条澄清不参与，见模块说明）
    targets = [r for r in rows if (r.get("expect") or "链接").strip() == "链接"]

    client = OpenAI(api_key=st.dashscope_api_key, base_url=st.dashscope_base_url,
                    timeout=60.0, max_retries=3)

    log("L5 尖刀实验 ④ · 意图识别")
    log(f"  模型      {st.llm_model}   temperature=0")
    log(f"  评测集    {len(targets)} 条（4 条「边界」题不参与，理由见模块说明）")
    log(f"  类别分布  {dict(Counter(r['intent'].strip() for r in targets))}")
    log("")

    stats = {m: {"ok": 0, "n": 0, "wrong": [], "confusion": Counter()}
             for m in ("rule", "llm")}
    details = []
    for r in targets:
        qid, gold = r["qid"].strip(), r["intent"].strip()
        q = r["question"].strip()
        g_rule = rule_intent(q)
        g_llm = llm_intent(client, st.llm_model, q)

        line = f"  {qid} [{gold}] {q}"
        marks = []
        for m, got in (("rule", g_rule), ("llm", g_llm)):
            stats[m]["n"] += 1
            hit = (got == gold)
            stats[m]["ok"] += hit
            stats[m]["confusion"][(gold, got)] += 1
            if not hit:
                stats[m]["wrong"].append((qid, gold, got))
            marks.append(f"{m}={'✓' if hit else '✗' + got}")
        log(line)
        log(f"      {'   '.join(marks)}")
        details.append({"qid": qid, "question": q, "gold": gold,
                        "rule": g_rule, "llm": g_llm})

    log("")
    log("── 汇总 ──")
    for m in ("rule", "llm"):
        d = stats[m]
        rate = d["ok"] / d["n"] if d["n"] else 0.0
        log(f"  {m:<5} 准确率 {d['ok']}/{d['n']} = {rate:.3f}")
    log("")
    for m in ("rule", "llm"):
        w = stats[m]["wrong"]
        if w:
            log(f"  {m} 判错的 {len(w)} 条：")
            for qid, gold, got in w:
                log(f"    {qid}  应为 {gold}  判成 {got}")
    log("")
    log("  ⚠️ 只有 23 条、每类 4–6 条，**样本很小**：数字只能当参考。")
    log("     要下结论得扩样本（那是测试集口径问题，需要用户拍板）。")

    OUT_FILE.write_text(json.dumps(
        {"model": st.llm_model, "n": len(targets),
         "rule": {"ok": stats["rule"]["ok"], "n": stats["rule"]["n"],
                  "wrong": stats["rule"]["wrong"]},
         "llm": {"ok": stats["llm"]["ok"], "n": stats["llm"]["n"],
                 "wrong": stats["llm"]["wrong"]},
         "details": details}, ensure_ascii=False, indent=2),
        encoding="utf-8", newline="\n")
    log(f"  ✓ 已写 {OUT_FILE.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
