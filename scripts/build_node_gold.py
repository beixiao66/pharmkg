#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L4 节点级金标准 · 草表生成
============================

按已定决策：评测从"抽取"挪到**端到端「抽取 + 节点对齐」**。
评分单位是 ``(drug, node)`` 对，所以先要有一份**节点级金标准**。

为什么不能机械生成
------------------
顿号切分会切出大量**不是实体**的片段——实测：

    奥扎莫德  "除非患者体内有心脏起搏器"   限定语
              "且功能良好"                残片
              "例如肝炎和结核（见…）"       举例
    来氟米特  "玉米淀粉"                  辅料名
              "因为缺乏该患者群的临床使用经验" 理由
              "必须排除妊娠"               处置
    达沙替尼  "禁用本品"                  后果，不是人群

所以本脚本只做**机械的部分**（切原子、查图谱词表），
逐条的判定与节点名由 ``PROPOSALS`` 给出**我的提案**，
再交人工**确认 / 修改 / 删除**。

节点词表（已定）
----------------
* **疾病** → 对齐 OpenCMKG 已有的 Disease 节点（L1 骨架的 14,726 个病名）。
  不这样，L5 问答与 L6 校验就与骨架串不起来。
* **人群** → 受控小词表，与 `population_tags` 对齐：
  ``孕妇 / 哺乳期女性 / 育龄女性 / 儿童 / 老年人 / 药物过敏者``
* **用药** → 合并用药禁忌（圣约翰草、单胺氧化酶抑制剂）
* **删除** → 限定语 / 举例 / 后果 / 辅料名 / 理由，不是禁忌实体

严重程度修饰语：**剥掉**（严重 / 重度 / 中度 / 轻中度），
**保留**性质修饰（急性 / 慢性 / 活动性 / 复发性）——
重度肝损伤与肝损伤是同一个 Disease 节点，而急性与慢性常是不同的病种。

用法
----
    python scripts/build_node_gold.py            # 生成草表（不覆盖已有）
    python scripts/build_node_gold.py --force    # 重新生成
    python scripts/build_node_gold.py --verify   # 校验人工填好的节点表

产出
----
    data/eval/contraindications_nodes.csv   一行 = 一个原子，含判定/理由/节点
"""

from __future__ import annotations

import argparse
import csv
import difflib
import sys
from pathlib import Path

from insert_io import read_csv_rows
from opencmkg_io import REPO_ROOT, load_entities_dict, load_triples
from score_extraction import split_atoms_raw

EVAL_DIR = REPO_ROOT / "data" / "eval"
GOLD_FILE = EVAL_DIR / "contraindications_gold.csv"
NODE_FILE = EVAL_DIR / "contraindications_nodes.csv"

FIELDS = ["drug", "atom_idx", "atom", "verdict", "node_type", "node",
          "in_graph", "suggest", "reason"]

#: 人群受控词表（与 population_tags 对齐）
POPULATION = ("孕妇", "哺乳期女性", "育龄女性", "儿童", "老年人", "药物过敏者")

#: 我的逐条提案：``(药, 原子序号) -> (判定, 类型, 节点名, 理由)``
#: 判定 ∈ 保留 / 需拆 / 删除。**这是提案，不是结论**——等人工确认。
PROPOSALS: dict[tuple[str, int], tuple[str, str, str, str]] = {
    ("他克莫司", 1): ("保留", "人群", "药物过敏者", "通用过敏禁忌（generic_only）"),
    ("他克莫司", 2): ("保留", "人群", "药物过敏者", "通用过敏禁忌，含大环内酯类交叉过敏"),

    ("伤寒疫苗", 1): ("保留", "人群", "药物过敏者", "通用过敏禁忌（generic_only）"),
    ("伤寒疫苗", 2): ("保留", "疾病", "急性疾病", ""),
    ("伤寒疫苗", 3): ("保留", "疾病", "慢性疾病", "剥掉「严重」"),
    ("伤寒疫苗", 4): ("需拆", "疾病", "发热", "原文并列「慢性疾病急性发作期」与「发热」，应拆两条"),
    ("伤寒疫苗", 5): ("保留", "人群", "孕妇", "原文「妊娠期妇女」"),

    ("克拉屈滨", 1): ("保留", "人群", "药物过敏者", "通用过敏禁忌（generic_only）"),

    ("多塞平", 1): ("保留", "疾病", "心脏病", "剥掉「严重」"),
    ("多塞平", 2): ("保留", "疾病", "心肌梗死", "原文「近期有心肌梗死发作史」"),
    ("多塞平", 3): ("保留", "疾病", "癫痫", ""),
    ("多塞平", 4): ("保留", "疾病", "青光眼", ""),
    ("多塞平", 5): ("保留", "疾病", "尿潴留", ""),
    ("多塞平", 6): ("保留", "疾病", "甲状腺功能亢进", "原文「甲状腺机能亢进」→规范名"),
    ("多塞平", 7): ("保留", "疾病", "肝功能损害", ""),
    ("多塞平", 8): ("保留", "疾病", "谵妄", ""),
    ("多塞平", 9): ("保留", "疾病", "中性粒细胞减少", "原文「粒细胞减少」→规范名"),
    ("多塞平", 10): ("保留", "人群", "药物过敏者", "三环类药物过敏"),

    ("奥希替尼", 1): ("保留", "人群", "药物过敏者", "通用过敏禁忌（generic_only）"),
    ("奥希替尼", 2): ("保留", "用药", "圣约翰草", "合并用药禁忌；「（见…）」已归一化去掉"),

    ("奥扎莫德", 1): ("保留", "人群", "药物过敏者", "通用过敏禁忌（generic_only）"),
    ("奥扎莫德", 2): ("保留", "疾病", "免疫缺陷", ""),
    ("奥扎莫德", 3): ("保留", "疾病", "心肌梗死", "剥掉「最近 6 个月内发生」"),
    ("奥扎莫德", 4): ("保留", "疾病", "不稳定型心绞痛", ""),
    ("奥扎莫德", 5): ("保留", "疾病", "卒中", ""),
    ("奥扎莫德", 6): ("保留", "疾病", "短暂性脑缺血发作", ""),
    ("奥扎莫德", 7): ("需拆", "疾病", "心力衰竭", "原文并列「需要住院的失代偿性心力衰竭」与「NYHA III/IV 级心力衰竭」"),
    ("奥扎莫德", 8): ("需拆", "疾病", "房室传导阻滞", "原文并列 II 度 II 型 / III 度 AV 阻滞 / 病态窦房结综合征"),
    ("奥扎莫德", 9): ("删除", "-", "", "限定语（「除非患者体内有心脏起搏器」），不是禁忌实体"),
    ("奥扎莫德", 10): ("删除", "-", "", "残片（「且功能良好」），切分产物"),
    ("奥扎莫德", 11): ("保留", "疾病", "活动性感染", "保留「活动性」，剥掉「重度」"),
    ("奥扎莫德", 12): ("保留", "疾病", "慢性感染", "保留「活动性」性质"),
    ("奥扎莫德", 13): ("删除", "-", "", "举例（「例如肝炎和结核」），不是独立禁忌"),
    ("奥扎莫德", 14): ("保留", "疾病", "恶性肿瘤", "剥掉「活动性」"),
    ("奥扎莫德", 15): ("保留", "疾病", "肝损伤", "剥掉「重度」与 Child-Pugh 分级"),
    ("奥扎莫德", 16): ("保留", "人群", "孕妇", "原文「妊娠期间」"),
    ("奥扎莫德", 17): ("保留", "人群", "育龄女性", "限定「未采取有效避孕措施」"),
    ("奥扎莫德", 18): ("保留", "用药", "单胺氧化酶抑制剂", "合并用药禁忌"),

    ("来氟米特", 1): ("删除", "-", "", "过敏条目的一部分，与原子 2 合起来才是一条"),
    ("来氟米特", 2): ("删除", "-", "", "同上；真正的人群是「药物过敏者」"),
    ("来氟米特", 3): ("删除", "-", "", "辅料名（玉米淀粉），非禁忌实体"),
    ("来氟米特", 4): ("删除", "-", "", "辅料名（低取代羟丙纤维素）"),
    ("来氟米特", 5): ("删除", "-", "", "辅料名（羟丙基纤维素）"),
    ("来氟米特", 6): ("删除", "-", "", "辅料名（聚乙二醇）"),
    ("来氟米特", 7): ("删除", "-", "", "辅料名（羟丙甲纤维素）"),
    ("来氟米特", 8): ("删除", "-", "", "辅料名（滑石粉）"),
    ("来氟米特", 9): ("保留", "人群", "药物过敏者", "通用过敏禁忌；辅料清单不拆，SJS/TEN/多形性红斑是重症表现举例"),
    ("来氟米特", 10): ("删除", "-", "", "过敏条目内的重症表现举例（中毒性表皮坏死松解症）"),
    ("来氟米特", 11): ("删除", "-", "", "残片 + 举例（多形性红斑）"),
    ("来氟米特", 12): ("保留", "疾病", "肝功能损害", "原文「肝功能受损」"),
    ("来氟米特", 13): ("保留", "疾病", "免疫缺陷", "剥掉「严重」"),
    ("来氟米特", 14): ("删除", "-", "", "举例（「例如 AIDS」），不是独立禁忌"),
    ("来氟米特", 15): ("需拆", "疾病", "贫血", "原文并列「骨髓功能严重受损」与「重度的贫血」"),
    ("来氟米特", 16): ("保留", "疾病", "白细胞减少", ""),
    ("来氟米特", 17): ("需拆", "疾病", "中性粒细胞减少", "原文并列「中性粒细胞减少」与「血小板减少」"),
    ("来氟米特", 18): ("保留", "疾病", "感染", "剥掉「严重」"),
    ("来氟米特", 19): ("保留", "疾病", "肾功能不全", "剥掉「中度至重度」"),
    ("来氟米特", 20): ("删除", "-", "", "理由（「因为缺乏该患者群的临床使用经验」）"),
    ("来氟米特", 21): ("保留", "疾病", "低蛋白血症", "剥掉「严重」"),
    ("来氟米特", 22): ("删除", "-", "", "举例（「例如患有肾病综合征」）"),
    ("来氟米特", 23): ("需拆", "人群", "孕妇", "原文并列「孕妇」与「未采取可靠避孕措施的育龄期女性」，句尾还附了处置语"),
    ("来氟米特", 24): ("删除", "-", "", "处置（「必须排除妊娠」），不是禁忌实体"),
    ("来氟米特", 25): ("保留", "人群", "哺乳期女性", ""),

    ("英夫利西单抗", 1): ("删除", "-", "", "过敏条目的一部分，与原子 2 合起来才是一条"),
    ("英夫利西单抗", 2): ("保留", "人群", "药物过敏者", "含「其它鼠源蛋白」交叉过敏"),
    ("英夫利西单抗", 3): ("需拆", "疾病", "结核病", "原文并列「结核病」与「其它活动性感染」"),
    ("英夫利西单抗", 4): ("删除", "-", "", "括号内举例（脓毒症），并入原子 3 的活动性感染"),
    ("英夫利西单抗", 5): ("删除", "-", "", "括号内举例（脓肿 / 机会性感染）"),
    ("英夫利西单抗", 6): ("保留", "疾病", "心力衰竭", "剥掉「中重度」"),

    ("达沙替尼", 1): ("保留", "人群", "药物过敏者", "通用过敏禁忌（generic_only）"),
    ("达沙替尼", 2): ("删除", "-", "", "后果（「禁用本品」），不是实体"),

    ("阿那格雷", 1): ("保留", "人群", "药物过敏者", "通用过敏禁忌（generic_only）"),
    ("阿那格雷", 2): ("保留", "疾病", "半乳糖不耐受", "保留「遗传性」性质"),
    ("阿那格雷", 3): ("需拆", "疾病", "乳糖酶缺乏", "原文并列「拉普乳糖酶缺乏」与「葡萄糖-半乳糖吸收障碍」"),
    ("阿那格雷", 4): ("删除", "-", "", "后果（「不应使用本品」）"),
    ("阿那格雷", 5): ("删除", "-", "", "理由（「因为制剂中含有乳糖」）"),
    ("阿那格雷", 6): ("保留", "疾病", "肝损伤", "剥掉「严重」"),
}


def suggest_node(node: str, vocab: set[str]) -> tuple[str, float]:
    """在 OpenCMKG 词表里找最接近的病名。**只认高置信匹配，宁缺勿滥。**

    这是三次试错后的结论，值得记下来：

    1. 分母用 ``min`` → 只要候选**包含**这个串就满分，于是
       ``卒中 → 出血性脑卒中(1.00)``、``感染 → 感染性急性小管间质性肾炎(1.00)``，
       **把一般概念对到更具体的亚型上**，图谱里就是假边。
    2. 分母改用 ``max`` → 罚了"多出来的字"，但又冒出
       ``卒中 → 中烧(0.50)``、``感染 → 痛感(0.50)``、``尿潴留 → 胃潴留(0.67)``
       这类**只共享一两个字的噪音**。
    3. 最终：**最长公共子串必须同时是两边的前缀或后缀**，且占比 ≥ 0.8。

    中文病名靠一两个字区分，通用字符串相似度在这里就是不够用——
    这本身就是"对齐"这一步的难点，也是 L4 真正的研究点。
    分数只是**给人看的建议**，最终节点名由人定。
    """
    best, best_s = "", 0.0
    for cand in vocab:
        if not cand:
            continue
        lo, hi = min(len(cand), len(node)), max(len(cand), len(node))
        if hi > lo * 2:                      # 长度差太远，不是同一个概念
            continue
        m = difflib.SequenceMatcher(None, node, cand, autojunk=False)
        blk = m.find_longest_match(0, len(node), 0, len(cand))
        if blk.size == 0:
            continue
        # 公共子串必须贴着两边的头或尾
        head = blk.a == 0 and blk.b == 0
        tail = (blk.a + blk.size == len(node)
                and blk.b + blk.size == len(cand))
        if not (head or tail):
            continue
        s = blk.size / hi
        if s > best_s:
            best, best_s = cand, s
    return best, best_s


def log(msg: str = "") -> None:
    print(msg, flush=True)


def load_atoms() -> list[dict]:
    """读金标准 → 原子行（跳过 n_gold=0 的零禁忌样本）。"""
    rows, _ = read_csv_rows(GOLD_FILE)
    by_drug: dict[str, list[str]] = {}
    for r in rows:
        if (r.get("n_gold") or "").strip() == "0":
            continue
        by_drug.setdefault(r["drug"], []).extend(split_atoms_raw(r["gold_text"]))

    out = []
    for drug in sorted(by_drug):
        for i, atom in enumerate(by_drug[drug], 1):
            out.append({"drug": drug, "atom_idx": i, "atom": atom})
    return out


def disease_vocab() -> set[str]:
    """OpenCMKG 的疾病词表（判断"这个节点图谱里有没有"）。

    ⚠ 必须传 triples：``entities_dict`` 里混进了本该归到 ``check`` 的一整份
    检查项词表，要靠 ``disease_need_check`` 关系逐条归位（见 ``opencmkg_io``）。
    不传的话词表是错的。
    """
    triples, _ = load_triples()
    entities, _ = load_entities_dict(triples=triples)
    return set(entities.get("disease", []))


def build(force: bool) -> int:
    atoms = load_atoms()
    vocab = disease_vocab()
    missing = [k for k in PROPOSALS
               if k not in {(a["drug"], a["atom_idx"]) for a in atoms}]
    uncovered = [(a["drug"], a["atom_idx"]) for a in atoms
                 if (a["drug"], a["atom_idx"]) not in PROPOSALS]

    log("L4 节点级金标准 · 草表")
    log(f"  原子总数    {len(atoms)}")
    log(f"  提案覆盖    {len(PROPOSALS)}   未覆盖 {len(uncovered)}   多余 {len(missing)}")
    log(f"  疾病词表    OpenCMKG {len(vocab):,} 个病名")
    log("")
    if uncovered:
        log(f"  ⚠ 没有提案的原子：{uncovered}")
    if missing:
        log(f"  ⚠ 提案里有对不上的键：{missing}")
    if uncovered or missing:
        return 1

    rows = []
    for a in atoms:
        verdict, ntype, node, reason = PROPOSALS[(a["drug"], a["atom_idx"])]
        in_graph = ""
        if ntype == "疾病" and node:
            in_graph = "是" if node in vocab else "否"
        elif ntype == "人群" and node:
            in_graph = "受控词表" if node in POPULATION else "✗ 不在词表"
        rows.append({
            "drug": a["drug"], "atom_idx": a["atom_idx"], "atom": a["atom"],
            "verdict": verdict, "node_type": ntype, "node": node,
            "in_graph": in_graph, "suggest": "", "reason": reason,
        })

    # 给"图谱里没有"的疾病节点找最接近的已有病名——这正是对齐本身要做的事
    need = sorted({r["node"] for r in rows
                   if r["node_type"] == "疾病" and r["in_graph"] == "否"})
    sugg: dict[str, tuple[str, float]] = {}
    for n in need:
        sugg[n] = suggest_node(n, vocab)
    for r in rows:
        if r["node_type"] == "疾病" and r["in_graph"] == "否":
            s, sc = sugg.get(r["node"], ("", 0.0))
            r["suggest"] = f"{s}（{sc:.2f}）" if sc >= 0.8 else "（需人工命名）"

    if NODE_FILE.exists() and not force:
        raise SystemExit(
            f"✗ {NODE_FILE.relative_to(REPO_ROOT)} 已存在，不覆盖"
            "（里面可能有你改过的内容）。\n  要重来请加 --force。"
        )
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    with NODE_FILE.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    keep = [r for r in rows if r["verdict"] == "保留"]
    split = [r for r in rows if r["verdict"] == "需拆"]
    drop = [r for r in rows if r["verdict"] == "删除"]
    log(f"  保留 {len(keep)}   需拆 {len(split)}   删除 {len(drop)}")
    log("")
    log("  按类型（保留的）：")
    for t in ("疾病", "人群", "用药"):
        ns = sorted({r["node"] for r in keep if r["node_type"] == t})
        log(f"    {t}（{len(ns)} 个不同节点）：{'、'.join(ns)}")
    log("")
    log("  疾病节点在图谱里的情况：")
    dis = sorted({r["node"] for r in keep if r["node_type"] == "疾病"})
    hit = [n for n in dis if n in vocab]
    miss = [n for n in dis if n not in vocab]
    log(f"    命中 OpenCMKG 已有病名 {len(hit)}/{len(dis)}：{'、'.join(hit)}")
    log(f"    需新建                {len(miss)}")
    for n in miss:
        s, sc = sugg.get(n, ("", 0.0))
        tag = f"→ 高置信同名：{s}（{sc:.2f}）" if sc >= 0.8 else "→ 需人工命名"
        log(f"      {n:<12} {tag}")
    log("")
    log(f"  已写 {NODE_FILE.relative_to(REPO_ROOT)}")
    log("")
    log("  请你逐行看 verdict / node_type / node 三列：")
    log("    · verdict=删除 的，确认它确实不是禁忌实体（看 reason）")
    log("    · verdict=需拆 的，按 reason 拆成两行")
    log("    · node 列改成你认为正确的节点名")
    log("  改完跑 --verify")
    return 0


def verify() -> int:
    rows, enc = read_csv_rows(NODE_FILE)
    log("L4 节点级金标准 · 校验")
    log(f"  编码 {enc}   行数 {len(rows)}")
    bad = []
    for r in rows:
        v = (r.get("verdict") or "").strip()
        if v not in ("保留", "需拆", "删除"):
            bad.append(f"{r['drug']}#{r['atom_idx']}: verdict 取值非法「{v}」")
        if v == "保留":
            t = (r.get("node_type") or "").strip()
            if t not in ("疾病", "人群", "用药"):
                bad.append(f"{r['drug']}#{r['atom_idx']}: node_type 非法「{t}」")
            if not (r.get("node") or "").strip():
                bad.append(f"{r['drug']}#{r['atom_idx']}: 保留但没有 node")
        if v == "需拆":
            bad.append(f"{r['drug']}#{r['atom_idx']}: 还标着「需拆」，请拆开或改成保留/删除")
    keep = [r for r in rows if r["verdict"] == "保留"]
    # 拆行时容易漏改序号——同药内 atom_idx 必须唯一
    seen: dict[tuple[str, str], int] = {}
    for r in rows:
        k = (r["drug"], (r.get("atom_idx") or "").strip())
        seen[k] = seen.get(k, 0) + 1
    for (drug, idx), cnt in seen.items():
        if cnt > 1:
            bad.append(f"{drug} 的 atom_idx「{idx}」重复了 {cnt} 次——拆行后要改成唯一的")
    log(f"  保留 {len(keep)} / 需拆 {sum(1 for r in rows if r['verdict'] == '需拆')}"
        f" / 删除 {sum(1 for r in rows if r['verdict'] == '删除')}")
    log(f"  节点总数（去重）{len({(r['drug'], r['node']) for r in keep})}")
    if bad:
        log("")
        for b in bad[:30]:
            log(f"  ✗ {b}")
        if len(bad) > 30:
            log(f"  …… 共 {len(bad)} 条")
        return 1
    log("  ✓ 全部合法")
    return 0


def lookup(keyword: str, limit: int = 25) -> int:
    """在 OpenCMKG 疾病词表里搜名字——``suggest`` 写着「需人工命名」时用它。

    词表文件是**一整行的 Python 字面量**，用 grep 搜会把整行几 MB 打出来，
    所以必须有这么个工具。
    """
    vocab = disease_vocab()
    hits = sorted(n for n in vocab if keyword in n)
    log(f"在 OpenCMKG 的 {len(vocab):,} 个疾病名里搜「{keyword}」")
    log(f"  命中 {len(hits)} 个")
    if len(hits) > limit:
        log(f"  （只显示前 {limit} 个）")
    for n in hits[:limit]:
        log(f"    {n}")
    log("")
    log("  怎么用：如果上面有**恰好**表达你要的那个概念的病名，就把 node 列填它")
    log("          （越具体的越要当心：「卒中」不要填成「出血性脑卒中」）")
    log("          确实没有，就按规范名自己写一个——它会成为新建的 Disease 节点")
    return 0


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="L4 节点级金标准草表")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的草表")
    ap.add_argument("--verify", action="store_true", help="校验人工填好的节点表")
    ap.add_argument("--lookup", metavar="关键词", default=None,
                    help="在 OpenCMKG 疾病词表里搜名字")
    args = ap.parse_args(argv)
    if args.lookup:
        return lookup(args.lookup)
    if args.verify:
        return verify()
    return build(args.force)


if __name__ == "__main__":
    raise SystemExit(main())
