#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L5 · 尖刀实验的问题集（**手工撰写 + 当场冻结**）
================================================

**为什么先做这个**：实体链接与 Text2Cypher 都要"对着问题量成功率"。
问题集不定下来，后面所有实验都建在流沙上。而且按决策 #26，
**few-shot 严禁取自冻结的测试集**——所以问题集一经用来调提示词，
它就不能再当测试集。于是顺序只能是：**先冻结问题集，再写提示词**。

**这 28 条是怎么来的**（措辞手工写，金标准必须"有据可查"）：

* **措辞是人写的**——按 §6.1 的六类意图各写几条，模仿真人提问的口气
  （会漏字、会用剂型简称、会问否定句）。
* **金标准一律是可执行的 Cypher**（``gold_cypher`` 列），由
  :mod:`verify_l5_questions` 逐条跑一遍、确认真能从图里查出东西。
  **"图里到底有没有这个事实"不能靠印象**——这正是 L4 那两次栽跟头的教训。
* **`must_link` 是实体链接的答案**：这条问题里**必须**被认出来的实体名
  （写用户在问题里说的那个词，不是图谱里的规范名）。
  ``卡维地洛`` 在问题里出现、而图里只有 ``卡维地洛片``——这正是要考的地方。

六类意图（§6.1）
----------------
================  ====  ==========================================
相互作用            5     药 ↔ 药
疾病用药            5     病 → 药（`RECOMMEND_DRUG` 正向）
适应症              4     药 → 病（**同一条边反向走**，容易写错方向）
禁忌                6     `CONTRAINDICATED_FOR`（L4 刚入库的边）
药品信息            4     节点属性（英文名等）
其他                4     **不该给事实**：图谱没有 / 需要澄清 / 完全无关
================  ====  ==========================================

用法
----
    python scripts/build_l5_questions.py            # 写 data/eval/l5_questions.csv
    python scripts/verify_l5_questions.py           # 逐条跑 gold_cypher 验金标准
"""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

from opencmkg_io import REPO_ROOT

OUT_FILE = REPO_ROOT / "data" / "eval" / "l5_questions.csv"

#: 表头。``min_rows`` = 金标准 Cypher 至少要返回几行（0 表示"本就不该有事实"）。
FIELDS = ["qid", "intent", "question", "must_link", "gold_cypher",
          "min_rows", "notes"]

#: ⚠️ 冻结于 2026-10-16（首次生成）。**加行可以，改已有行要写明理由并重测**——
#: 改了它，之前所有"在测试集上"的数字就都失效了（决策 #26 的同一条纪律）。
QUESTIONS: list[dict] = [
    # ── 相互作用 ────────────────────────────────────────────────
    {"qid": "Q01", "intent": "相互作用",
     "question": "卡维地洛和依度沙班能一起吃吗？",
     "must_link": "卡维地洛；依度沙班",
     "gold_cypher": "MATCH (a:Drug {name:'卡维地洛片'})-[r:INTERACTS_WITH]-(b:Drug {name:'依度沙班'}) RETURN r.level",
     "min_rows": 1,
     "notes": "用户说的是「卡维地洛」，图里只有「卡维地洛片」——考实体链接的剂型后缀"},
    {"qid": "Q02", "intent": "相互作用",
     "question": "丁丙诺啡和屈大麻酚有相互作用吗？",
     "must_link": "丁丙诺啡；屈大麻酚",
     "gold_cypher": "MATCH (a:Drug {name:'丁丙诺啡'})-[r:INTERACTS_WITH]-(b:Drug {name:'屈大麻酚'}) RETURN r.level",
     "min_rows": 1, "notes": "两端都是自然药名，没有剂型后缀"},
    {"qid": "Q03", "intent": "相互作用",
     "question": "乙酰唑胺片和盐酸二甲双胍肠溶片同服有风险吗？",
     "must_link": "乙酰唑胺片；盐酸二甲双胍肠溶片",
     "gold_cypher": "MATCH (a:Drug {name:'乙酰唑胺片'})-[r:INTERACTS_WITH]-(b:Drug {name:'盐酸二甲双胍肠溶片'}) RETURN r.level",
     "min_rows": 1, "notes": "两端都用全名，作为「不需要补全」的对照"},
    {"qid": "Q04", "intent": "相互作用",
     "question": "茶碱和卡维地洛一起用要紧吗？",
     "must_link": "茶碱；卡维地洛",
     "gold_cypher": "MATCH (a:Drug {name:'茶碱缓释胶囊'})-[r:INTERACTS_WITH]-(b:Drug {name:'卡维地洛片'}) RETURN r.level",
     "min_rows": 1, "notes": "两端都是简称，考两次补全"},
    {"qid": "Q05", "intent": "相互作用",
     "question": "ω-3脂肪酸和伊布替尼需要注意吗？",
     "must_link": "ω-3脂肪酸；伊布替尼",
     "gold_cypher": "MATCH (a:Drug {name:'ω-3脂肪酸'})-[r:INTERACTS_WITH]-(b:Drug {name:'伊布替尼'}) RETURN r.level",
     "min_rows": 1, "notes": "药名含连字符与希腊字母，考分词不会把它切碎"},

    # ── 疾病用药（病 → 药）──────────────────────────────────────
    {"qid": "Q06", "intent": "疾病用药",
     "question": "类风湿关节炎可以用哪些药？",
     "must_link": "类风湿关节炎",
     "gold_cypher": "MATCH (:Disease {name:'类风湿关节炎'})-[:RECOMMEND_DRUG]->(d:Drug) RETURN d.name",
     "min_rows": 5, "notes": "图里 23 种，取 ≥5 即可"},
    {"qid": "Q07", "intent": "疾病用药",
     "question": "劳力性心绞痛推荐用什么药？",
     "must_link": "劳力性心绞痛",
     "gold_cypher": "MATCH (:Disease {name:'劳力性心绞痛'})-[:RECOMMEND_DRUG]->(d:Drug) RETURN d.name",
     "min_rows": 5, "notes": ""},
    {"qid": "Q08", "intent": "疾病用药",
     "question": "病毒性肠炎该用什么药？",
     "must_link": "病毒性肠炎",
     "gold_cypher": "MATCH (:Disease {name:'病毒性肠炎'})-[:RECOMMEND_DRUG]->(d:Drug) RETURN d.name",
     "min_rows": 5, "notes": ""},
    {"qid": "Q09", "intent": "疾病用药",
     "question": "纤维肌痛综合征能用什么药？",
     "must_link": "纤维肌痛综合征",
     "gold_cypher": "MATCH (:Disease {name:'纤维肌痛综合征'})-[:RECOMMEND_DRUG]->(d:Drug) RETURN d.name",
     "min_rows": 5, "notes": ""},
    {"qid": "Q10", "intent": "疾病用药",
     "question": "肾阳虚吃什么药？",
     "must_link": "肾阳虚",
     "gold_cypher": "MATCH (:Disease {name:'肾阳虚'})-[:RECOMMEND_DRUG]->(d:Drug) RETURN d.name",
     "min_rows": 5, "notes": "中医病名，考词表里确实有它"},

    # ── 适应症（药 → 病，同一条边**反向**走）────────────────────
    {"qid": "Q11", "intent": "适应症",
     "question": "骨肽片是治什么的？",
     "must_link": "骨肽片",
     "gold_cypher": "MATCH (dz:Disease)-[:RECOMMEND_DRUG]->(d:Drug {name:'骨肽片'}) RETURN dz.name",
     "min_rows": 1, "notes": "⚠️ `RECOMMEND_DRUG` 的方向是 Disease→Drug，这条要走反向"},
    {"qid": "Q12", "intent": "适应症",
     "question": "洛索洛芬钠片有什么适应症？",
     "must_link": "洛索洛芬钠片",
     "gold_cypher": "MATCH (dz:Disease)-[:RECOMMEND_DRUG]->(d:Drug {name:'洛索洛芬钠片'}) RETURN dz.name",
     "min_rows": 1, "notes": ""},
    {"qid": "Q13", "intent": "适应症",
     "question": "速效救心丸能治什么病？",
     "must_link": "速效救心丸",
     "gold_cypher": "MATCH (dz:Disease)-[:RECOMMEND_DRUG]->(d:Drug {name:'速效救心丸'}) RETURN dz.name",
     "min_rows": 1, "notes": ""},
    {"qid": "Q14", "intent": "适应症",
     "question": "去痛片用于哪些疾病？",
     "must_link": "去痛片",
     "gold_cypher": "MATCH (dz:Disease)-[:RECOMMEND_DRUG]->(d:Drug {name:'去痛片'}) RETURN dz.name",
     "min_rows": 1, "notes": ""},

    # ── 禁忌（L4 刚入库的边；只挑 high 置信的）──────────────────
    {"qid": "Q15", "intent": "禁忌",
     "question": "多塞平有哪些禁忌？",
     "must_link": "多塞平",
     "gold_cypher": "MATCH (d:Drug {name:'多塞平'})-[r:CONTRAINDICATED_FOR]->(t) WHERE r.confidence='high' RETURN t.name",
     "min_rows": 2, "notes": "只认 high 置信——L4 的边实测约 33% 假阳性，别拿 medium 当金标准"},
    {"qid": "Q16", "intent": "禁忌",
     "question": "吗啡禁用于哪些情况？",
     "must_link": "吗啡",
     "gold_cypher": "MATCH (d:Drug {name:'吗啡'})-[r:CONTRAINDICATED_FOR]->(t) WHERE r.confidence='high' RETURN t.name",
     "min_rows": 2, "notes": ""},
    {"qid": "Q17", "intent": "禁忌",
     "question": "孕妇能用乌帕达西替尼吗？",
     "must_link": "孕妇；乌帕达西替尼",
     "gold_cypher": "MATCH (:Drug {name:'乌帕达西替尼'})-[:CONTRAINDICATED_FOR]->(t:Population {name:'孕妇'}) RETURN t.name",
     "min_rows": 1, "notes": "人群节点，考 Population 标签只有 5 个名字这件事"},
    {"qid": "Q18", "intent": "禁忌",
     "question": "对大环内酯类药物过敏的人能用他克莫司吗？",
     "must_link": "他克莫司；大环内酯类药物过敏者",
     "gold_cypher": "MATCH (:Drug {name:'他克莫司'})-[:CONTRAINDICATED_FOR]->(t {name:'大环内酯类药物过敏者'}) RETURN t.name",
     "min_rows": 1, "notes": "长实体名，考「最长优先」不会只匹到「大环内酯类」"},
    {"qid": "Q19", "intent": "禁忌",
     "question": "尼洛替尼的禁忌里有没有低钾血症？",
     "must_link": "尼洛替尼；低钾血症",
     "gold_cypher": "MATCH (:Drug {name:'尼洛替尼'})-[:CONTRAINDICATED_FOR]->(t {name:'低钾血症'}) RETURN t.name",
     "min_rows": 1, "notes": "**是非疑问句**——回答要能说「有」，而不是罗列全部禁忌"},
    {"qid": "Q20", "intent": "禁忌",
     "question": "伤寒疫苗孕妇能打吗？",
     "must_link": "伤寒疫苗；孕妇",
     "gold_cypher": "MATCH (:Drug {name:'伤寒疫苗'})-[:CONTRAINDICATED_FOR]->(t:Population {name:'孕妇'}) RETURN t.name",
     "min_rows": 1, "notes": "伤寒疫苗是 L4 里 8 个「剂型分裂」新建节点的之一"},

    # ── 药品信息 ────────────────────────────────────────────────
    {"qid": "Q21", "intent": "药品信息",
     "question": "卡维地洛片的英文名是什么？",
     "must_link": "卡维地洛片",
     "gold_cypher": "MATCH (d:Drug {name:'卡维地洛片'}) WHERE d.name_en IS NOT NULL RETURN d.name_en",
     "min_rows": 1, "notes": ""},
    {"qid": "Q22", "intent": "药品信息",
     "question": "盐酸二甲双胍肠溶片的英文名？",
     "must_link": "盐酸二甲双胍肠溶片",
     "gold_cypher": "MATCH (d:Drug {name:'盐酸二甲双胍肠溶片'}) WHERE d.name_en IS NOT NULL RETURN d.name_en",
     "min_rows": 1, "notes": ""},
    {"qid": "Q23", "intent": "药品信息",
     "question": "卡维地洛是什么药？",
     "must_link": "卡维地洛",
     "gold_cypher": "MATCH (d:Drug) WHERE d.name STARTS WITH '卡维地洛' RETURN d.name",
     "min_rows": 1, "notes": "只给简称——**这是实体链接最该考的一条**：必须补成「卡维地洛片」才查得到"},
    {"qid": "Q24", "intent": "药品信息",
     "question": "乙酰唑胺片的英文名是什么？",
     "must_link": "乙酰唑胺片",
     "gold_cypher": "MATCH (d:Drug {name:'乙酰唑胺片'}) WHERE d.name_en IS NOT NULL RETURN d.name_en",
     "min_rows": 1, "notes": ""},

    # ── 其他（**不该给事实**）────────────────────────────────────
    {"qid": "Q25", "intent": "其他",
     "question": "阿司匹林能治癌症吗？",
     "must_link": "",
     "gold_cypher": "",
     "min_rows": 0,
     "notes": "⚠️ 图里的阿司匹林叫「乙酰水杨酸」，直查「阿司匹林」**查不到**——"
              "正确行为是澄清或明说无记录，**不是硬猜**（§13.3）"},
    {"qid": "Q26", "intent": "其他",
     "question": "感冒了吃什么药好？",
     "must_link": "",
     "gold_cypher": "",
     "min_rows": 0,
     "notes": "「感冒」在词表里可能是「感冒」也可能是「普通感冒」，需要澄清——考多候选时不硬选"},
    {"qid": "Q27", "intent": "其他",
     "question": "孕妇能吃这个药吗？",
     "must_link": "",
     "gold_cypher": "",
     "min_rows": 0,
     "notes": "「这个药」没有上下文——必须澄清反问，绝不能拿上一轮的药硬套（也没有上一轮）"},
    {"qid": "Q28", "intent": "其他",
     "question": "今天上海的天气怎么样？",
     "must_link": "",
     "gold_cypher": "",
     "min_rows": 0,
     "notes": "完全超出医药范围——应明确拒答，考的是**不乱答**"},
]


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\n")
    w.writeheader()
    for row in QUESTIONS:
        w.writerow(row)
    OUT_FILE.write_text(buf.getvalue(), encoding="utf-8", newline="\n")

    by_intent: dict[str, int] = {}
    for row in QUESTIONS:
        by_intent[row["intent"]] = by_intent.get(row["intent"], 0) + 1

    print("L5 问题集（手工撰写 + 冻结）")
    print(f"  写入      {OUT_FILE.relative_to(REPO_ROOT)}")
    print(f"  条数      {len(QUESTIONS)}")
    for k, v in by_intent.items():
        print(f"    {k:<8}{v}")
    print()
    print("  ⚠️ 决策 #26：few-shot 严禁取自这里——它是**测试集**。")
    print("     下一步：python scripts/verify_l5_questions.py 逐条跑金标准 Cypher。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
