#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L5 · few-shot 示例的生成与冻结（决策 #26）
==========================================

**决策 #26 的做法**：LLM 按真实 schema 生成候选 → **人工筛选固化 10–15 条**、
覆盖 6 类意图、**严禁取自冻结的测试集**。

**分工（用户拍板）**：我生成 + 我自查 + **用户抽检**。所以"不泄题"这件事
由**脚本机械保证**，而不是靠肉眼扫：

* 与 `data/eval/l5_questions.csv` 的**问题**逐字比对，必须零相同；
* 把两边的**实体集合**也报出来（重叠不代表泄题，但重叠多了示例就不独立了）。

**为什么 few-shot 必须由 LLM 生成、而不能我手写**：我手写的 Cypher 一定是
"我心里那个写法"，而模型生成的才是"模型会写出来的形状"——用后者做示例，
示例与实际行为同分布。我筛的是**能不能跑通、结果对不对**。

筛选标准（三条都要过，不过就丢掉并打印原因）：
1. 过白名单校验（只读、有 MATCH 与 RETURN）；
2. 能**真的执行**（不报语法/语义错）；
3. **返回 ≥1 行**——返回 0 行的示例等于在教模型"这样写查不到东西"。

用法
----
    python scripts/build_l5_fewshot.py               # 生成并冻结
    python scripts/build_l5_fewshot.py --dry-run     # 只生成候选，不写文件
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path

from insert_io import read_csv_rows
from opencmkg_io import REPO_ROOT
from l5_cypher_prompt import build_messages, extract_cypher, validate_cypher
from l5_entity_linking import INTENT_LABELS, build_index, resolve, scan

sys.path.insert(0, str(REPO_ROOT / "backend"))

QUESTIONS_FILE = REPO_ROOT / "data" / "eval" / "l5_questions.csv"
OUT_FILE = REPO_ROOT / "data" / "eval" / "l5_fewshot.csv"

#: 逐条落盘的缓存。**踩过的坑**：第一版只在全部生成完之后才写文件，
#: 结果第 9 条 API 超时 → 整轮崩掉 → **前面 8 条白花**。
#: 现在每处理完一条就落盘，重跑自动跳过已完成的。
CACHE_FILE = REPO_ROOT / "data" / "eval" / "l5_fewshot_cache.json"

#: 单次请求超时（秒）。OpenAI 客户端默认 600s——对"生成一小段 Cypher"来说
#: 太长：真挂住时一等就是十分钟，而且看起来像"程序卡死"。60s 足够，
#: 配合 max_retries 让瞬时抖动自愈。
LLM_TIMEOUT = 60.0
LLM_RETRIES = 3

#: **必须设**。不设的话，模型偶尔会陷进退化重复循环（实测：给同一列起无数个别名），
#: 一路写到请求超时——看起来像"接口挂了"，其实是它在无限输出。
MAX_TOKENS = 300

FIELDS = ["fid", "intent", "question", "entities", "cypher", "rows", "source"]

#: 种子问题（我手写，**刻意避开测试集的实体**——27 个已用实体全部排除）。
#: 实体不写死在这里，而是**跑同一个实体链接器**得到——这样 few-shot 与评测
#: 共用一条链路，示例才与真实输入同分布。
SEEDS: list[tuple[str, str]] = [
    ("相互作用", "一氧化氮和丙胺卡因能一起用吗？"),
    ("相互作用", "丁巴比妥的相互作用有哪些？"),
    ("相互作用", "乙醇和丁巴比妥同服要紧吗？"),
    ("疾病用药", "冠心病推荐用什么药？"),
    ("疾病用药", "血瘀体质可以用哪些药？"),
    ("疾病用药", "冠心病有哪些症状？"),
    ("适应症", "复方丹参滴丸是治什么的？"),
    ("适应症", "脑心通胶囊能治什么病？"),
    ("禁忌", "氯氮平有哪些禁忌？"),
    ("禁忌", "来氟米特禁用于哪些情况？"),
    ("禁忌", "肾功能不全的人能用三氧化二砷吗？"),   # 换掉：原选药没有该禁忌
    ("禁忌", "奥扎莫德的禁忌里有没有孕妇？"),
    ("药品信息", "氯氮平的英文名是什么？"),
    ("药品信息", "木瓜的英文名是什么？"),          # 换掉：「他汀」不是图里的节点，实体链接产不出实体（这是设计使然：实体由链接器给，CONTAINS 式发现问句不属于六类意图）
]

#: 至少要固化多少条示例。少于这个数说明筛选太狠，示例覆盖不住意图。
MIN_EXAMPLES = 10


def log(msg: str = "") -> None:
    print(msg, flush=True)


def link_question(question: str, intent: str, names, prefixes, max_len) -> list[str]:
    """用**与评测完全相同**的实体链接逻辑取实体。"""
    allowed = INTENT_LABELS.get(intent, ("Drug", "Disease", "Population"))
    got: list[str] = []
    for _a, _b, piece, kind in scan(question, names, prefixes, max_len):
        res = resolve(piece, kind, allowed, names, prefixes)
        if res["node"] and res["node"] not in got:
            got.append(res["node"])
    return got


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="生成并冻结 L5 的 few-shot 示例")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    from app.config import get_settings
    from neo4j import GraphDatabase
    from openai import OpenAI

    st = get_settings()
    if not st.llm_configured:
        log("✗ LLM 未配齐，无法生成候选。")
        return 2

    frozen, _ = read_csv_rows(QUESTIONS_FILE)
    frozen_questions = {r["question"].strip() for r in frozen}
    frozen_entities: set[str] = set()
    for r in frozen:
        for x in (r.get("gold_nodes") or "").split("；"):
            if x.strip():
                frozen_entities.add(x.strip())

    driver = GraphDatabase.driver(st.neo4j_uri,
                                 auth=(st.neo4j_user, st.neo4j_password))
    client = OpenAI(api_key=st.dashscope_api_key, base_url=st.dashscope_base_url,
                    timeout=LLM_TIMEOUT, max_retries=LLM_RETRIES)

    log("L5 few-shot 生成（LLM 生成候选 → 脚本筛选）")
    log(f"  模型      {st.llm_model}")
    log(f"  测试集    {len(frozen_questions)} 条问题、{len(frozen_entities)} 个实体"
        f"（few-shot 必须避开）")
    log("")

    cache: dict[str, dict] = {}
    if CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cache = {}
    log(f"  缓存      {len(cache)} 条已完成（重跑会跳过）" if cache else "  缓存      空")
    log("")

    kept: list[dict] = []
    dropped: list[tuple[str, str]] = []

    def save_cache() -> None:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                              encoding="utf-8", newline="\n")

    with driver.session() as s:
        names, prefixes = build_index(s)
        max_len = max((len(x) for x in names), default=2)

        for idx, (intent, question) in enumerate(SEEDS, 1):
            fid = f"F{idx:02d}"
            log(f"[{idx}/{len(SEEDS)}] {fid} [{intent}] {question}")

            if fid in cache:
                log(f"      缓存命中：{cache[fid].get('why', 'ok')}")
                if cache[fid].get("keep"):
                    kept.append(cache[fid]["row"])
                else:
                    dropped.append((fid, cache[fid].get("why", "")))
                continue

            # ① 先查泄题——问题逐字相同就直接拒（这一步在调 LLM 之前）
            if question.strip() in frozen_questions:
                log("      ✗ 与冻结测试集的问题**完全相同**，拒用")
                dropped.append((fid, "与测试集问题相同"))
                cache[fid] = {"keep": False, "why": "与测试集问题相同"}
                save_cache()
                continue

            entities = link_question(question, intent, names, prefixes, max_len)
            if not entities:
                log("      ✗ 实体链接没产出实体，无法生成")
                dropped.append((fid, "无实体"))
                cache[fid] = {"keep": False, "why": "无实体"}
                save_cache()
                continue
            log(f"      实体 {entities}")

            msgs = build_messages(question, intent, entities)
            try:
                resp = client.chat.completions.create(model=st.llm_model,
                                                      messages=msgs,
                                                      temperature=0,
                                                      max_tokens=MAX_TOKENS)
                raw = resp.choices[0].message.content or ""
            except Exception as exc:  # noqa: BLE001
                # ⚠️ **一条 API 抖动不该毁掉整轮**：记下来、继续下一条。
                #    第一版没有这个兜底，第 9 条超时就把前面 8 条一起带了走。
                why = f"API 失败 {type(exc).__name__}"
                log(f"      ✗ {why}: {str(exc)[:60]}")
                dropped.append((fid, why))
                cache[fid] = {"keep": False, "why": why}
                save_cache()
                continue
            cypher = extract_cypher(raw)

            ok, why = validate_cypher(cypher)
            if not ok:
                log(f"      ✗ 白名单没过：{why}")
                dropped.append((fid, f"白名单：{why}"))
                cache[fid] = {"keep": False, "why": f"白名单：{why}"}
                save_cache()
                continue

            try:
                executed = list(s.run(cypher))
                rows = len(executed)
            except Exception as exc:  # noqa: BLE001
                log(f"      ✗ 执行报错：{type(exc).__name__}: {str(exc)[:70]}")
                dropped.append((fid, f"执行报错 {type(exc).__name__}"))
                cache[fid] = {"keep": False,
                              "why": f"执行报错 {type(exc).__name__}"}
                save_cache()
                continue

            if rows == 0:
                log("      ✗ 返回 0 行——等于在教模型「这样写查不到东西」")
                dropped.append((fid, "返回 0 行"))
                cache[fid] = {"keep": False, "why": "返回 0 行"}
                save_cache()
                continue

            # ── 语义检查：**返回的东西得真的是"名字"** ──────────────────
            # 只判"能跑通、有行"是不够的。实测 `丁巴比妥的相互作用有哪些？`
            # 生成了 `MATCH (d:Drug {name:'丁巴比妥'})-[r:INTERACTS_WITH]-(other:Drug)
            # RETURN r.level` —— 跑得通、返回 170 行，但**只返回级别、
            # 不返回对方药名**，等于没回答"有哪些"。这种示例会教坏模型。
            #
            # 判据：**只给了一个实体**的问句（"有哪些…"），返回值里至少要有一个
            # 是图里的节点名；**给了两个实体**的问句（"A 和 B 能一起吃吗"）
            # 答案本来就是关系属性（level），不受这条约束。
            if len(entities) == 1 and intent in ("相互作用", "疾病用药",
                                                 "适应症", "禁忌"):
                recs = list(executed)
                vals = [str(v) for rec in recs for v in rec.values()
                        if isinstance(v, str)]
                has_name = False
                for v in vals[:60]:
                    if s.run("MATCH (n) WHERE n.name = $x "
                             "RETURN count(n) AS c LIMIT 1", x=v).single()["c"]:
                        has_name = True
                        break
                if not has_name:
                    log("      ✗ 返回值里没有一个图节点名——"
                        "「有哪些…」的问句只返回了关系属性，等于没回答")
                    dropped.append((fid, "返回值里没有节点名"))
                    cache[fid] = {"keep": False, "why": "返回值里没有节点名"}
                    save_cache()
                    continue

            log(f"      ✓ 通过，返回 {rows} 行")
            row = {"fid": fid, "intent": intent, "question": question,
                   "entities": "；".join(entities), "cypher": cypher,
                   "rows": rows, "source": "llm-generated, verified"}
            kept.append(row)
            cache[fid] = {"keep": True, "row": row, "why": f"返回 {rows} 行"}
            save_cache()

    driver.close()

    # ── 机械保证不泄题 ──────────────────────────────────────────────
    overlap_q = [k["question"] for k in kept if k["question"].strip() in frozen_questions]
    kept_entities = {e for k in kept for e in k["entities"].split("；") if e}
    overlap_e = sorted(kept_entities & frozen_entities)

    log("")
    log("── 汇总 ──")
    log(f"  种子 {len(SEEDS)} 条 → 固化 {len(kept)} 条，丢弃 {len(dropped)} 条")
    for fid, why in dropped:
        log(f"    ✗ {fid}  {why}")
    log("")
    log("── 不泄题的机械检查 ──")
    log(f"  问题逐字相同：{len(overlap_q)} 条  {'✓' if not overlap_q else '✗ ' + str(overlap_q)}")
    log(f"  实体与测试集重叠：{len(overlap_e)} 个"
        f"{'（' + '、'.join(overlap_e) + '）' if overlap_e else '  ✓'}")
    log("    说明：实体重叠不等于泄题（同一个药可以问不同的事），但重叠越少示例越独立。")

    by_intent: dict[str, int] = {}
    for k in kept:
        by_intent[k["intent"]] = by_intent.get(k["intent"], 0) + 1
    log("")
    log(f"  意图覆盖：{by_intent}")

    bad = bool(overlap_q) or len(kept) < MIN_EXAMPLES
    if bad:
        log("")
        log(f"  ✗ 不达标（要求：问题零重叠 且 至少 {MIN_EXAMPLES} 条）")
    else:
        log("")
        log("  ✓ 达标（问题零重叠，条数够）")

    if args.dry_run:
        log("  （--dry-run：不写文件）")
        return 1 if bad else 0
    if bad:
        log("  ✗ 不写文件——先修好种子问题再跑。")
        return 1

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\n")
    w.writeheader()
    for row in kept:
        w.writerow(row)
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(buf.getvalue(), encoding="utf-8", newline="\n")
    log(f"  ✓ 已冻结 {OUT_FILE.relative_to(REPO_ROOT)}")
    log("")
    log("  ⚠️ 请用户抽检这个文件。它一旦被用来调提示词，就不能再当测试集（决策 #26）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
