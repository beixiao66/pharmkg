#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L5 尖刀实验 ①：实体链接（确定性扫描、最长优先）
================================================

**为什么先做这一步**：§13.4 点名 L5 的两个高风险点是**实体链接**与 **Text2Cypher**。
两者必须**分层量**——实体没链对，Cypher 里就是错的节点名，出问题时根本分不清
是"没认出药名"还是"不会写 Cypher"。所以这一轮**只看实体链接**：
意图**直接取问题集里标好的**（假设意图识别是准的），把它当已知量。

算法（全部可解释、无 LLM）
--------------------------
1. 从图里取 **Drug / Disease / Population** 三类节点名建索引，并建**前缀索引**
   （长度 ≥2 的前缀 → 完整名字）。只取这三类是因为 §6.1 的六类意图只会查它们。
2. 在问题上**从左到右、最长优先**扫；命中就跳过整个词，避免"卡维地洛片"里
   再匹出"卡维地洛"。
3. 每个命中分四种结果：

   ==============  ==================================================
   精确            扫描到的串**就是**节点名 → 置信 high
   补全            串只是某个节点名的**前缀**，且候选唯一 → medium
   需澄清          前缀候选**多于一个** → **不猜**（§13.3）
   无法链接        连前缀都不是 → 认不出
   ==============  ==================================================

⚠️ **"按前缀取第一个"是错的**，实测候选里夹着截断垃圾与复方制剂：
``茶碱`` → ``茶碱缓释片`` / ``茶碱缓释胶``（截断垃圾）/ ``茶碱缓释胶囊``；
``二甲双胍`` → ``二甲双胍格列本脲片(Ⅰ)``（**复方**，不是单方）。
取错就等于把两种药的相互作用算到一种药头上。

用法
----
    python scripts/l5_entity_linking.py                # 判分
    python scripts/l5_entity_linking.py --show-mentions # 顺便打印每次命中的依据
"""

from __future__ import annotations

import argparse
import io
import sys
from collections import defaultdict

from insert_io import read_csv_rows
from opencmkg_io import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "backend"))

QUESTIONS_FILE = REPO_ROOT / "data" / "eval" / "l5_questions.csv"

#: 索引哪几类节点。§6.1 的六类意图只会查这三类。
INDEX_LABELS = ("Drug", "Disease", "Population")

#: 每个意图允许链接到的标签——用来在"同名跨标签"时选对那一个。
#: ⚠️ 这不是作弊：**意图在本轮是已知输入**（分层实验的设计），
#: 真实系统里它由第 ① 步给出。
INTENT_LABELS: dict[str, tuple[str, ...]] = {
    "相互作用": ("Drug",),
    "疾病用药": ("Disease",),
    "适应症": ("Drug",),
    "禁忌": ("Drug", "Population", "Disease"),
    "药品信息": ("Drug",),
    "其他": ("Drug", "Disease", "Population"),
}

#: 前缀最短长度。1 个字的前缀会把"人""者"这种噪声全捞进来。
MIN_PREFIX = 2

#: **自动补全**的两个门槛（实测逼出来的，见下）。
#:
#: 只有"前缀候选唯一"是不够的：``今天上海的天气怎么样？`` 里的 **``今天``**（2 字）
#: 是图里那个垃圾节点 **``今天应该拉了四次``**（9 字）的前缀，且唯一——
#: 于是自动补全把它当成实体链了出来。**两个常见字去猜一个九字名字，没有任何证据。**
#:
#: 所以补全还要求：① 前缀至少 3 字；② 前缀覆盖候选名的比例 ≥ 0.6。
#: 验算：``卡维地洛``/``卡维地洛片`` = 0.83 ✓ 通过；``大环内酯类药物过敏``/
#: ``大环内酯类药物过敏者`` = 0.9 ✓ 通过；``今天``/``今天应该拉了四次`` = 0.22 ✗ 拦掉。
AUTOCOMPLETE_MIN_LEN = 3
AUTOCOMPLETE_COVERAGE = 0.6

#: **提问词汇**——它们在句子里是"槽位名"，**永远不是实体引用**。
#:
#: 为什么需要它：图里真的存在一个叫 **``禁忌``** 的节点（垃圾数据），
#: 于是 ``多塞平有哪些禁忌？`` 会把它当成第二个实体链出来，精确率掉一截。
#: 这份表只收 **L5 自己的提问用语**，**不是实体名的同义词表**——
#: 后者是这个项目明令禁止的（决策 #26 同一条纪律）。图里那两个垃圾节点该由 L8 清。
QUERY_STOPWORDS = frozenset({
    "禁忌", "禁忌症", "注意事项", "慎用", "禁用",
    "相互作用", "适应症", "用法", "用量", "用法用量",
    "不良反应", "副作用", "英文名", "说明书",
    "能吃", "能打", "能吃吗", "一起", "同服", "合用",
    "什么", "哪些", "怎么样", "有没有", "是什么",
    # ⚠️ 2026-10-16 补（端到端流水线抓到的）：`尼洛替尼的禁忌里**有没有**低钾血症？`
    #    被"澄清"短路了，而澄清对象是 **`没有`** ——图里有个垃圾节点叫「没有」。
    #    一个垃圾节点把整道题堵死，现象还很像"系统在认真澄清"。
    #    这里收的都是**疑问/连接用的功能词**，仍然不是实体名的同义词表。
    "没有", "有没", "是否", "可以", "需要", "怎么", "怎样", "多久", "多少",
    "能不能", "会不会", "要不要", "行不行",
})


def log(msg: str = "") -> None:
    print(msg, flush=True)


def build_index(session) -> tuple[set[str], dict[str, list[tuple[str, tuple[str, ...]]]], dict[str, str]]:
    """返回 ``(全部节点名, 前缀索引, 英文别名表)``。

    前缀索引的值是 ``[(完整名字, 标签元组)...]``——**标签要一起存**，
    因为同一个名字可能同时是 Symptom 和 Disease（实测全图 1,774 个），
    解析时得按意图挑对那一个。

    ⚠️ **英文别名（`name_en`）是后补的**，起因是一个真实反馈：
    系统能回答「林丹的英文名是 Lindane」，却**认不出**「Lindane能治什么」——
    因为索引里只有 `n.name`。图里 **1,803 / 5,696** 个药有英文名（DDInter 来源）。

    英文名只登记为**精确别名**，不进前缀索引：
    按前缀去匹配英文会产生大量无意义命中（`Lind` → `Lindane`、`Lin` → …），
    而中文可以按前缀补全——两种语言的匹配习惯不一样。

    别名表是小写的，便于大小写不敏感地查；`names` 里同时放原名与小写形式，
    这样用户输入 `Lindane` / `lindane` 都能被扫到。
    """
    by_name: dict[str, list[str]] = defaultdict(list)
    aliases: dict[str, str] = {}
    #: 英文名的**原始大小写**形式。必须单独收着：
    #: `scan` 按原样字符串匹配，而 `aliases` 的键是小写——
    #: 只放小写的话用户输入 `Lindane`（大写 L）就扫不到（实测踩过）。
    en_surface: set[str] = set()
    for rec in session.run(
            "MATCH (n) WHERE any(l IN labels(n) WHERE l IN $ls) "
            "RETURN n.name AS name, labels(n) AS ls, n.name_en AS en",
            ls=list(INDEX_LABELS)):
        name = rec["name"]
        if not name:
            continue
        for lb in rec["ls"]:
            if lb in INDEX_LABELS and lb not in by_name[name]:
                by_name[name].append(lb)
        en = (rec["en"] or "").strip()
        if en and en != name:
            # 英文名 → 中文节点名。同一个英文名指向多个中文名时**不登记**，
            # 让它们落到"认不出"而不是"猜一个"（宁可不答，不可答错）。
            key = en.lower()
            if key in aliases and aliases[key] != name:
                aliases.pop(key, None)
                aliases[f"__ambig__{key}"] = ""
            elif not aliases.get(f"__ambig__{key}"):
                aliases[key] = name
                en_surface.add(en)

    # 清掉歧义标记，并把它们的原始 key / 原始大小写形式一起排除
    for k in [k for k in aliases if k.startswith("__ambig__")]:
        raw_key = k[len("__ambig__"):]
        aliases.pop(raw_key, None)
        aliases.pop(k, None)
    for en in list(en_surface):
        if en.lower() not in aliases:
            en_surface.discard(en)

    names = set(by_name)
    # 英文别名加入可扫描集合（原始大小写 + 小写形式），但**不进前缀索引**
    names |= en_surface
    for key in aliases:
        names.add(key)

    prefixes: dict[str, list[tuple[str, tuple[str, ...]]]] = defaultdict(list)
    for name, labels in by_name.items():
        entry = (name, tuple(labels))
        for cut in range(MIN_PREFIX, len(name)):
            prefixes[name[:cut]].append(entry)
    for key in prefixes:
        prefixes[key].sort()
    return names, dict(prefixes), aliases


def scan(question: str, names: set[str], prefixes: dict,
         max_len: int) -> list[tuple[int, int, str, str]]:
    """最长优先扫描。返回 ``[(起, 止, 命中的串, 类型)]``，类型 ∈ {精确, 前缀}。"""
    hits: list[tuple[int, int, str, str]] = []
    i, n = 0, len(question)
    while i < n:
        matched = None
        upper = min(max_len, n - i)
        for length in range(upper, MIN_PREFIX - 1, -1):
            piece = question[i:i + length]
            if piece in names:
                matched = (length, piece, "精确")
                break
            if piece in prefixes:
                matched = (length, piece, "前缀")
                break
        if matched:
            length, piece, kind = matched
            # 提问词汇直接跳过（按它的长度前进，避免再从里面匹出更短的碎片）
            if piece not in QUERY_STOPWORDS:
                hits.append((i, i + length, piece, kind))
            i += length
        else:
            i += 1
    return hits


#: 三个**故意做坏**的对照策略——用来证明满分不是恒真的。
#: 只看"1.000"无法分辨"方法好"和"指标不会动"，必须让指标**当场失败给你看**。
CONTROLS = {
    "none": "什么都不链接（召回应为 0）",
    "first": "前缀**取第一个**候选、不澄清也不设门槛",
    "no-label": "不按意图过滤标签（同名跨标签时随便取）",
}


def resolve(piece: str, kind: str, allowed: tuple[str, ...],
            names: set[str], prefixes: dict, control: str | None = None,
            aliases: dict[str, str] | None = None) -> dict:
    """把一个命中解析成节点（或判定为需澄清 / 无法链接）。"""
    if control == "none":
        return {"status": "无法链接", "node": "", "note": "对照：什么都不链"}
    if kind == "精确":
        # 英文别名（name_en）在这里换成中文节点名——用户输入 `Lindane`，
        # 图里那个节点叫「林丹」。不加这一步，下游 Cypher 会拿一个不存在的节点名去查。
        if aliases and piece.lower() in aliases:
            node = aliases[piece.lower()]
            return {"status": "别名", "node": node, "note": f"{piece} → {node}"}
        return {"status": "精确", "node": piece, "note": ""}

    pool = prefixes.get(piece, [])
    if control == "no-label":
        cands = list(pool)
    else:
        cands = [(nm, lbs) for nm, lbs in pool
                 if any(lb in allowed for lb in lbs)]
    if control == "first":
        # 取第一个候选——正是实测证明会出错的那个做法
        return {"status": "补全", "node": cands[0][0],
                "note": "对照：前缀取第一个" } if cands else {
                "status": "无法链接", "node": "", "note": "对照：无候选"}
    # ⚠️ **顺序很关键**：多候选先判"需澄清"，覆盖度门槛只管"单候选自动补全"。
    #    反过来写（先卡覆盖度）会把 `茶碱` 这种**该澄清**的情形静默降级成"认不出"——
    #    §13.3 要的是**反问用户**，不是当作没事发生。这是实测踩出来的。
    if len(cands) >= 2:
        return {"status": "需澄清", "node": "",
                "note": f"{len(cands)} 个候选：" + "、".join(nm for nm, _ in cands[:5])}
    cands = [(nm, lbs) for nm, lbs in cands
             if len(piece) >= AUTOCOMPLETE_MIN_LEN
             and len(piece) / len(nm) >= AUTOCOMPLETE_COVERAGE]
    if len(cands) == 1:
        nm, lbs = cands[0]
        return {"status": "补全", "node": nm,
                "note": f"候选唯一、覆盖 {len(piece) / len(nm):.0%}：" + "/".join(lbs)}
    if not cands:
        return {"status": "证据不足", "node": "",
                "note": f"唯一候选但前缀证据不足（<{AUTOCOMPLETE_MIN_LEN} 字 或 "
                        f"覆盖 <{AUTOCOMPLETE_COVERAGE:.0%}）——不猜"}
    return {"status": "需澄清", "node": "",
            "note": f"{len(cands)} 个候选：" + "、".join(nm for nm, _ in cands[:5])}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="L5 实体链接尖刀实验")
    ap.add_argument("--show-mentions", action="store_true",
                    help="打印每次命中的依据")
    ap.add_argument("--control", choices=sorted(CONTROLS), default=None,
                    help="跑一个**故意做坏**的对照，用来证明指标会动："
                         + "；".join(f"{k}={v}" for k, v in CONTROLS.items()))
    args = ap.parse_args(argv)

    from app.config import get_settings
    from neo4j import GraphDatabase

    rows, _ = read_csv_rows(QUESTIONS_FILE)
    st = get_settings()
    driver = GraphDatabase.driver(st.neo4j_uri,
                                 auth=(st.neo4j_user, st.neo4j_password))

    log("L5 尖刀实验 ① · 实体链接（确定性扫描 / 最长优先）")
    log(f"  问题集    {QUESTIONS_FILE.relative_to(REPO_ROOT)}（{len(rows)} 条）")
    log(f"  索引标签  {'/'.join(INDEX_LABELS)}")
    if args.control:
        log(f"  ⚠️ 对照模式  {args.control} —— {CONTROLS[args.control]}")
    log("")

    with driver.session() as s:
        names, prefixes, aliases = build_index(s)
        max_len = max((len(x) for x in names), default=2)
        log(f"  索引      {len(names):,} 个节点名 · "
            f"{len(prefixes):,} 个前缀 · 最长名字 {max_len} 字")
        log("")

        tp = fp = fn = 0
        rows_ok = scored = clarify_ok = clarify_n = 0
        stat: dict[str, int] = defaultdict(int)
        others: list[tuple[str, str]] = []
        log("── 逐条 ──")
        for r in rows:
            qid, intent = r["qid"].strip(), r["intent"].strip()
            q = r["question"].strip()
            allowed = INTENT_LABELS.get(intent, INDEX_LABELS)
            expect = (r.get("expect") or "链接").strip() or "链接"
            expected = {x.strip() for x in (r.get("gold_nodes") or "").split("；")
                        if x.strip()}

            hits = scan(q, names, prefixes, max_len)
            got: set[str] = set()
            detail: list[str] = []
            per_hit_status: list[tuple[str, str]] = []
            for _a, _b, piece, kind in hits:
                res = resolve(piece, kind, allowed, names, prefixes,
                      control=args.control, aliases=aliases)
                stat[res["status"]] += 1
                per_hit_status.append((res["status"], res["node"]))
                if res["node"]:
                    got.add(res["node"])
                detail.append(f"{piece}→{res['node'] or res['status']}"
                              + (f"（{res['note']}）" if res["note"] and
                                 args.show_mentions else ""))

            if expect == "边界" or not expected:
                # 「边界」：该不该回答、怎么回答属于**回答层**，本轮只登记不判分
                others.append((qid, "、".join(sorted(got)) or "（未链出实体）"))
                log(f"  ○ {qid} [{intent}] {q}")
                log(f"      链出 {sorted(got) or '（无）'}   [边界：行为在回答层判]")
                continue

            if expect == "澄清":
                # 正确行为是**反问用户选哪个**（§13.3），所以"没链出节点"才是对的
                clarified = any(x[0] == "需澄清" for x in per_hit_status)
                clarify_ok += clarified
                log(f"  {'✓' if clarified else '✗'} {qid} [{intent}] {q}")
                log(f"      本条**有歧义**，应当澄清（不是猜一个）")
                log(f"      实得 {'需澄清 ✓' if clarified else '未澄清 ✗'}："
                    f"{'、'.join(sorted(got)) or '（未链出节点）'}")
                if args.show_mentions:
                    log(f"      命中 {' | '.join(detail)}")
                continue

            t = len(expected & got)
            f_p = len(got - expected)
            f_n = len(expected - got)
            tp += t
            fp += f_p
            fn += f_n
            perfect = (f_p == 0 and f_n == 0)
            rows_ok += perfect
            scored += 1
            log(f"  {'✓' if perfect else '✗'} {qid} [{intent}] {q}")
            log(f"      期望 {sorted(expected)}")
            log(f"      实得 {sorted(got) or '（无）'}"
                f"   TP {t} FP {f_p} FN {f_n}")
            if args.show_mentions and detail:
                log(f"      命中 {' | '.join(detail)}")

    driver.close()

    p = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * rec / (p + rec) if p + rec else 0.0

    log("")
    log("── 汇总（按 expect 分类判）──")
    log(f"  TP {tp}  FP {fp}  FN {fn}")
    log(f"  P {p:.3f}  R {rec:.3f}  F1 {f1:.3f}")
    log(f"  **整条全对：{rows_ok} / {scored}**（expect=链接 的那些）")
    log(f"  **歧义题澄清正确：{clarify_ok} / {scored and 1 or 1}**"
        f"（expect=澄清，本轮只有 Q04）")
    log("")
    log("── 每次命中的解析结果分布 ──")
    for k in ("精确", "补全", "需澄清", "证据不足"):
        log(f"    {k:<8}{stat.get(k, 0)}")
    if others:
        log("")
        log("── 「其他」那几条（本轮不判对错，交给意图/回答层）──")
        for qid, what in others:
            log(f"    {qid}  链出 {what}")
    log("")
    log("⚠️ 本轮把**意图当已知输入**（问题集里标好的）——这是刻意的分层：")
    log("   否则「没认出药名」和「意图判错」会混在一起，分不清该修哪一层。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
