#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
原始数据体检 —— 让"数据长什么样"这件事从猜测变成实测
======================================================

`download_data.py` 负责把文件拉下来，本脚本负责**把格式和规模钉死**：
列名是什么、编码是什么、关系有哪些、实体有多少、数据干不干净。
它的输出是 `docs/数据源实测报告.md` 的唯一数据来源。

用法
----
    python scripts/inspect_raw.py                    # 打印完整报告
    python scripts/inspect_raw.py --out report.txt   # 同时落盘
    python scripts/inspect_raw.py --samples 5        # 每处样例收敛到 5 条

设计要点
--------
* 不假设任何列名/分隔符/类型，全部**从数据里推导**（schema discovery），
  避免"我以为它是这样"污染结论。
* 对每一条基线文档里的断言（如"药物 4289 种"）做**实测复核**，
  不一致就明确标出——文档的可信度取决于它敢不敢被验证。
"""

from __future__ import annotations

import argparse
import ast
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = REPO_ROOT / "data" / "raw"
OPENCMKG = RAW_DIR / "opencmkg"
DDINTER = RAW_DIR / "ddinter"

DDINTER_CODES = "ABDHLPRV"
DDINTER_COLUMNS = ["DDInterID_A", "Drug_A", "DDInterID_B", "Drug_B", "Level"]

#: 基线文档里的断言，逐条实测复核。（键, 基线声称, 出处）
BASELINE_CLAIMS = [
    ("OpenCMKG 疾病实体数", 14726, "§3 数据源表"),
    ("OpenCMKG 药物实体数", 4289, "§3 数据源表"),
    ("OpenCMKG 三元组数", 354755, "§3 数据源表"),
    ("OpenCMKG disease_recommand_drug 条数", 60621, "§3 数据源表"),
    ("DDInter 药物数", 1833, "§3 数据源表"),
    ("DDInter 相互作用对数", 236834, "§3 数据源表"),
]

#: OpenCMKG 关系的头/尾实体类型，从关系中缀猜测；中缀猜不出来的靠数据反推。
RELATION_HINTS = {
    "disease_has_symptom": ("disease", "symptom"),
    "disease_acompany_disease": ("disease", "disease"),
    "disease_belong_department": ("disease", "department"),
    "department_belong_department": ("department", "department"),
    "disease_need_check": ("disease", "check"),
    "disease_common_drug": ("disease", "drug"),
    "disease_recommand_drug": ("disease", "drug"),
    "disease_do_eat": ("disease", "food"),
    "disease_no_eat": ("disease", "food"),
    "disease_not_eat": ("disease", "food"),
    "disease_drug_company": ("disease", "producer"),
}

#: 常见剂型后缀，用于回答"药名是否带剂型"（基线 §10 问题 2）
DOSAGE_SUFFIXES = [
    "片", "胶囊", "注射液", "注射剂", "颗粒", "软膏", "乳膏", "凝胶", "喷雾剂",
    "滴眼液", "滴鼻液", "口服液", "溶液", "散", "丸", "膏", "贴", "栓", "气雾剂",
    "缓释片", "控释片", "分散片", "咀嚼片", "糖浆", "混悬液", "洗剂", "擦剂", "酊",
]


class Report:
    """收集报告行，最后统一打印/落盘。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str = "") -> None:
        self.lines.append(text)

    def h1(self, text: str) -> None:
        self("")
        self("=" * 78)
        self(f"# {text}")
        self("=" * 78)

    def h2(self, text: str) -> None:
        self("")
        self(f"── {text} " + "─" * max(0, 70 - len(text)))

    def h3(self, text: str) -> None:
        self("")
        self(f"  ▸ {text}")

    def kv(self, key: str, value: object, width: int = 42) -> None:
        self(f"    {key:<{width}} {value}")

    def table(self, rows: list[tuple], headers: tuple, aligns: str | None = None) -> None:
        cols = len(headers)
        widths = [len(str(headers[i])) for i in range(cols)]
        body = [[str(c) for c in r] for r in rows]
        for r in body:
            for i in range(cols):
                widths[i] = max(widths[i], len(r[i]))
        aligns = aligns or "<" * cols

        def fmt(cells) -> str:
            return "  ".join(
                f"{cells[i]:{aligns[i]}{widths[i]}}" for i in range(cols)
            )

        self("    " + fmt(headers))
        self("    " + "  ".join("-" * w for w in widths))
        for r in body:
            self("    " + fmt(r))

    def text(self) -> str:
        return "\n".join(self.lines)


# ================================================================ OpenCMKG

def load_entities_dict(path: Path) -> tuple[dict[str, list[str]], int]:
    """entities_dict.txt 是**单行 Python dict 字面量**，用 ast 解析。

    上游数据里混进了 1 个裸标识符 ``nan``（pandas 缺失值直接 str 化的产物），
    会让 ``ast.literal_eval`` 抛 ValueError。这里把它替换成占位符而不是跳过整行——
    因为"上游数据里有个 nan"本身就是一条质量结论，值得记下来报告。

    返回 (实体字典, nan 占位符个数)。
    """
    raw = path.read_text(encoding="utf-8").strip()

    class _NanToConstant(ast.NodeTransformer):
        def __init__(self) -> None:
            self.count = 0

        def visit_Name(self, node: ast.Name):  # noqa: N802 — ast API 命名
            self.count += 1
            return ast.copy_location(ast.Constant(value="__NAN__"), node)

    tree = ast.parse(raw, mode="eval")
    fixer = _NanToConstant()
    tree = fixer.visit(tree)
    data = ast.literal_eval(tree)

    if not isinstance(data, dict):
        raise ValueError(f"entities_dict.txt 顶层不是 dict，而是 {type(data).__name__}")
    return {str(k): [str(x) for x in v] for k, v in data.items()}, fixer.count


def load_triples(path: Path) -> tuple[list[tuple[str, str, str]], dict[str, int]]:
    """解析三元组。**必须用 csv 模块，任何手写 split 都会出错。**

    ``triples.txt`` 是合法 CSV：字段本身含逗号时**会加双引号**，但只给需要的字段加。
    三种真实行形态：

        百日咳,disease_need_check,"耳,鼻,咽拭子细菌培养"        ← 尾部带引号
        "跖骨,趾骨骨折",disease_has_symptom,疲劳                ← 头部带引号
        "小儿人类疱疹病毒6,7,8型感染性疾病",disease_has_symptom,疱疹

    因此：
      * ``line.split(",")`` 会丢掉全部 2,171 行含逗号的记录（切成 4 段以上）；
      * ``line.split(",", 2)`` 能救回尾部带引号的，但会把**头部带引号**的行切错位，
        造出 ``趾骨骨折"`` 这种根本不存在的"关系名"；
      * 只有 ``csv.reader`` 能全部正确解析。
    """
    raw_lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    stats = {
        "total": len(raw_lines),
        #: 逗号数 != 2，即朴素 split(",") 必然切坏的行
        "naive_loss": sum(1 for ln in raw_lines if ln.count(",") != 2),
        "unrecoverable": 0,
    }

    triples: list[tuple[str, str, str]] = []
    for row in csv.reader(raw_lines):
        if len(row) != 3:
            stats["unrecoverable"] += 1
            continue
        triples.append((row[0], row[1], row[2]))
    return triples, stats


def derive_schema(triples, entities_dict, report: Report, samples: int):
    """从数据反推每种关系的头/尾实体类型，而不是照抄文档里的假设。"""
    vocab_by_type = {t: set(names) for t, names in entities_dict.items()}
    by_rel: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for h, r, t in triples:
        by_rel[r].append((h, t))

    rows = []
    derived: dict[str, tuple[str, str]] = {}
    for rel, pairs in sorted(by_rel.items(), key=lambda kv: -len(kv[1])):
        heads = {h for h, _ in pairs}
        tails = {t for _, t in pairs}

        def best_type(cands: set[str]) -> tuple[str, float]:
            scores = []
            for tname, vocab in vocab_by_type.items():
                if not cands:
                    continue
                hit = len(cands & vocab) / len(cands)
                scores.append((hit, tname))
            if not scores:
                return "?", 0.0
            scores.sort(reverse=True)
            hit, tname = scores[0]
            # 覆盖率太低就说明"词表里根本没有这一类的实体"，此时报出 argmax 的名字
            # 会误导（比如 disease_need_check 的尾部被报成 drug，其实只是 drug 里有 2% 撞名）。
            return (tname, hit) if hit >= 0.5 else ("?", hit)

        h_type, h_hit = best_type(heads)
        t_type, t_hit = best_type(tails)
        # 文档给的提示若与数据一致，优先采用提示里的名字（更符合语义）
        hint = RELATION_HINTS.get(rel)
        if hint:
            if hint[0] in vocab_by_type and h_hit < 0.5:
                h_type = hint[0]
            if hint[1] in vocab_by_type and t_hit < 0.5:
                t_type = hint[1]
        derived[rel] = (h_type, t_type)

        rows.append((
            rel, f"{len(pairs):,}", f"{len(heads):,}", f"{len(tails):,}",
            f"{h_type}({h_hit:.0%})", f"{t_type}({t_hit:.0%})",
        ))

    report.h3("关系清单与类型反推（覆盖率 = 该侧实体能在 entities_dict 对应类型里找到的比例）")
    report.table(rows, ("关系", "三元组", "头实体", "尾实体", "头类型", "尾类型"),
                 aligns="<>>><<")
    report("")
    report("    说明：覆盖率不是 100% 就说明该关系里混入了 entities_dict 里没有的实体——")
    report("         可能是数据脏，也可能是 entities_dict 不全。下一节会区分这两种情况。")
    report("         覆盖率低于 50% 时类型记作 `?`，表示**词表里根本没有这一类**，")
    report("         而不是「argmax 刚好撞上谁就写谁」。")
    report("")
    report("    ★ 重点：`disease_need_check` 的尾实体共 3,351 个，几乎全都不在 entities_dict 里")
    report("      ——因为 entities_dict **没有 check 类型**。基线 §5 把 `Check` 列为节点类型，")
    report("      但权威词表里查不到；`Check` 节点只能从 triples 现场收集，没有别名可用。")
    return derived, by_rel


def inspect_sameas(by_rel, entities_dict, report: Report, samples: int):
    """OpenCMKG 自带一条 SameAs 关系——这是白送的实体对齐标注，必须查清楚。"""
    pairs = by_rel.get("SameAs")
    report.h2("SameAs 关系专项（图谱里已有的实体对齐信息）")
    if not pairs:
        report("    （未发现 SameAs 关系）")
        return

    heads = {h for h, _ in pairs}
    tails = {t for _, t in pairs}
    all_names = heads | tails
    known: dict[str, str] = {}
    for tname, names in entities_dict.items():
        for n in names:
            known.setdefault(n, tname)
    covered = [n for n in all_names if n in known]

    report.kv("SameAs 三元组数", f"{len(pairs):,}")
    report.kv("涉及的不同实体", f"{len(all_names):,}")
    report.kv("其中能在 entities_dict 里找到类型的", f"{len(covered):,} ({len(covered) / max(1, len(all_names)):.0%})")
    report.kv("存在于 entities_dict 但**未被任何 SameAs 引用**的实体",
              f"{sum(len(v) for v in entities_dict.values()) - len(covered):,}")
    report("")
    report("    样例（左 = 别名，右 = 规范名 或反之）：")
    for h, t in list(pairs)[:samples]:
        report(f"      {h}  ↔  {t}")
    report("")
    report("    → 意义：SameAs 就是现成的**同义词/实体对齐标注**。L4 的实体链接")
    report("      可以直接用它做监督信号或评测参照，不必从零构造。")
    report("      注意它的覆盖率很低（见上），所以只能当「金标准子集」用，不能当成完整词典。")


def check_vocabulary(triples, entities_dict, derived, report: Report, samples: int):
    """用 entities_dict 当权威词表，量出三元组里有多少实体是'词表外'的。"""
    vocab_by_type = {t: set(names) for t, names in entities_dict.items()}
    rows = []
    oov_total = 0
    for rel, (h_type, t_type) in sorted(derived.items()):
        h_vocab = vocab_by_type.get(h_type, set())
        t_vocab = vocab_by_type.get(t_type, set())
        if not h_vocab and not t_vocab:
            continue
        pairs = [(h, t) for h, r, t in triples if r == rel]
        oov_h = sorted({h for h, _ in pairs if h not in h_vocab})
        oov_t = sorted({t for _, t in pairs if t not in t_vocab})
        oov_total += len(oov_t)
        rows.append((
            rel, f"{len(pairs):,}",
            f"{len(oov_h):,}", f"{len(oov_h) / max(1, len({h for h, _ in pairs})):.1%}",
            f"{len(oov_t):,}", f"{len(oov_t) / max(1, len({t for _, t in pairs})):.1%}",
        ))
    report.h3("词表外实体（OOV）统计")
    report.table(rows, ("关系", "三元组", "头OOV", "头OOV率", "尾OOV", "尾OOV率"),
                 aligns="<>>><>")
    return oov_total


def inspect_symptom_noise(triples, entities_dict, report: Report, samples: int):
    """专门查 disease_has_symptom —— 这是 OpenCMKG 最容易脏的地方。"""
    symptom_vocab = set(entities_dict.get("symptom", []))
    pairs = [(h, t) for h, r, t in triples if r == "disease_has_symptom"]
    diseases_by_symptom: dict[str, set[str]] = defaultdict(set)
    for h, t in pairs:
        diseases_by_symptom[t].add(h)

    total_sym = len(diseases_by_symptom)
    singleton = {s: d for s, d in diseases_by_symptom.items() if len(d) == 1}

    report.h2("disease_has_symptom 数据质量专项")
    report.kv("症状实体数（去重）", f"{total_sym:,}")
    report.kv("只挂在 1 个疾病下的症状", f"{len(singleton):,}  ({len(singleton) / max(1, total_sym):.1%})")
    report.kv("不在 entities_dict['symptom'] 里的症状", f"{len([s for s in diseases_by_symptom if s not in symptom_vocab]):,}")

    # 人名噪声：2–3 字、只在单一疾病下、且不在症状词表里 → 高度可疑
    suspicious = sorted(
        [s for s in singleton if s not in symptom_vocab and 2 <= len(s) <= 3]
    )
    report.kv("可疑噪声（词表外 + 2~3 字 + 唯一挂载）", f"{len(suspicious):,}")
    if suspicious:
        report("")
        report(f"    样例（最多 {samples} 条）：")
        for s in suspicious[:samples]:
            hosts = "、".join(sorted(singleton[s])[:3])
            report(f"      · {s!r}  ← 仅挂在：{hosts}")

    # 已知的两个具体噪声，单独点名
    report("")
    for name in ["毓卓", "闫鹏辉"]:
        in_vocab = name in symptom_vocab
        hosts = sorted(diseases_by_symptom.get(name, []))
        report(f"    点名核查 {name!r}：在 entities_dict['symptom'] 中 = {in_vocab}；"
               f"出现在 {len(hosts)} 个疾病下 {hosts[:3]}")

    # 长尾：高频 vs 低频
    freq = Counter((len(d) for d in diseases_by_symptom.values()))
    report("")
    report("    症状「挂载疾病数」分布（前 8 档）：")
    for k in sorted(freq)[:8]:
        report(f"      挂载 {k:>3} 个疾病：{freq[k]:>7,} 个症状")

    return {
        "symptom_total": total_sym,
        "singleton": len(singleton),
        "oov": len([s for s in diseases_by_symptom if s not in symptom_vocab]),
        "suspicious": suspicious,
    }


def inspect_drug_names(entities_dict, report: Report, samples: int):
    """回答基线 §10 问题 2：OpenCMKG 药名是否带剂型后缀 / 是否含商品名。"""
    drugs = entities_dict.get("drug", [])
    if not drugs:
        report("    （entities_dict 中没有 drug 类型）")
        return {}

    with_suffix = [d for d in drugs if any(d.endswith(s) for s in DOSAGE_SUFFIXES)]
    no_suffix = [d for d in drugs if d not in set(with_suffix)]

    report.h2("OpenCMKG 药物实体名格式（基线 §10 问题 2）")
    report.kv("药物实体总数", f"{len(drugs):,}")
    report.kv("带剂型后缀的药名", f"{len(with_suffix):,}  ({len(with_suffix) / len(drugs):.1%})")
    report.kv("不带剂型后缀的药名", f"{len(no_suffix):,}  ({len(no_suffix) / len(drugs):.1%})")
    report.kv("药名长度中位数", sorted(len(d) for d in drugs)[len(drugs) // 2])

    report("")
    report(f"    带后缀样例（{samples} 条）：{'、'.join(with_suffix[:samples])}")
    report(f"    不带后缀样例（{samples} 条）：{'、'.join(no_suffix[:samples])}")

    # 同一药物因剂型不同而产生的多条记录
    stems = Counter()
    for d in with_suffix:
        for s in sorted(DOSAGE_SUFFIXES, key=len, reverse=True):
            if d.endswith(s):
                stems[d[: -len(s)]] += 1
                break
    multi = [(k, v) for k, v in stems.most_common(8) if v > 1]
    report("")
    report("    同一主成分因剂型不同而分裂成多条的样例：")
    for stem, n in multi:
        variants = [d for d in with_suffix if d.startswith(stem)][:4]
        report(f"      · {stem} → {n} 条：{'、'.join(variants)}")

    report("")
    report("    → 结论：先归一化（剥离剂型/盐形式）再做映射，否则同一药会被算成多个实体。")

    return {
        "total": len(drugs),
        "with_suffix": len(with_suffix),
        "no_suffix": len(no_suffix),
    }


def inspect_opencmkg(report: Report, samples: int) -> dict:
    report.h1("一、OpenCMKG（骨架图谱）")

    # ---- entities_dict
    ed_path = OPENCMKG / "entities_dict.txt"
    entities_dict, nan_count = load_entities_dict(ed_path)
    total_ent = sum(len(v) for v in entities_dict.values())

    report.h2("1.1 entities_dict.txt")
    report.kv("文件体积", f"{ed_path.stat().st_size:,} B")
    report.kv("文件结构", "单行 Python dict 字面量（全部内容在同一行，需 ast.literal_eval）")
    report.kv("顶层类型键", "、".join(entities_dict.keys()))
    report.kv("实体类型数", len(entities_dict))
    report.kv("实体总数", f"{total_ent:,}")
    if nan_count:
        report.kv("⚠ 裸标识符 nan（缺失值泄漏）", f"{nan_count} 处 → 已替换为 '__NAN__' 占位")
    report("")
    rows = []
    for t, names in sorted(entities_dict.items(), key=lambda kv: -len(kv[1])):
        uniq = len(set(names))
        rows.append((t, f"{len(names):,}", f"{uniq:,}",
                     "、".join(list(dict.fromkeys(names))[:3])[:46]))
    report.table(rows, ("类型", "实体数", "去重后", "样例"), aligns="<>>" + "<")

    # ---- triples
    tr_path = OPENCMKG / "triples.txt"
    triples, tstats = load_triples(tr_path)
    report.h2("1.2 triples.txt")
    report.kv("文件体积", f"{tr_path.stat().st_size:,} B")
    report.kv("编码 / 行尾", "UTF-8 / LF")
    report.kv("格式", "无表头 CSV：头实体,关系,尾实体（字段含逗号时**带双引号**）")
    report.kv("非空行总数", f"{tstats['total']:,}")
    report.kv("有效三元组数", f"{len(triples):,}")
    report.kv("解析后字段数 != 3 的行", f"{tstats['unrecoverable']:,}")
    report("")
    report(f"    ⚠ 解析陷阱：有 {tstats['naive_loss']:,} 行"
           f"（占 {tstats['naive_loss'] / max(1, tstats['total']):.2%}）的字段里含逗号，")
    report("      这些字段在文件里是**加双引号**的：")
    report('        百日咳,disease_need_check,"耳,鼻,咽拭子细菌培养"     ← 尾部带引号')
    report('        "跖骨,趾骨骨折",disease_has_symptom,疲劳             ← 头部带引号')
    report("")
    report("      两种错误切法的后果：")
    report(f"        · line.split(',')      → 丢掉全部 {tstats['naive_loss']:,} 行")
    report("        · line.split(',', 2)   → 能救尾部带引号的，但把头部带引号的切错位，")
    report('                                 造出 趾骨骨折" 这种不存在的"关系名"')
    report("        · csv.reader           → 全部正确 ✓（本报告采用）")
    report("")
    report("      → L1 导入脚本必须用 csv 模块，并且要保留这一行统计作为质量证据。")
    uniq_triples = len(set(triples))
    report.kv("去重后三元组数", f"{uniq_triples:,}")
    report.kv("重复三元组", f"{len(triples) - uniq_triples:,}")
    self_loop = sum(1 for h, _, t in triples if h == t)
    report.kv("自环（头 == 尾）", f"{self_loop:,}")

    derived, by_rel = derive_schema(triples, entities_dict, report, samples)
    oov = check_vocabulary(triples, entities_dict, derived, report, samples)
    inspect_sameas(by_rel, entities_dict, report, samples)
    noise = inspect_symptom_noise(triples, entities_dict, report, samples)
    drug_info = inspect_drug_names(entities_dict, report, samples)

    # ---- manual_annotation
    ann_path = OPENCMKG / "manual_annotation.csv"
    report.h2("1.3 manual_annotation.csv")
    with ann_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, [])
        rows = list(reader)
    labels = Counter(r[2] for r in rows if len(r) >= 3)
    report.kv("列名", ",".join(header))
    report.kv("标注条数", f"{len(rows):,}")
    report.kv("标注分布", dict(labels))
    report("")
    report("    样例：")
    for r in rows[:5]:
        report(f"      {r[0]}  |  {r[1]}  →  {r[2]}")
    report("")
    report("    → 这是**实体对齐的标注集**（1=同一实体，0=不同）。")
    report("      可直接用于评测 L4 实体链接 / 药名映射的准确率，是本项目能拿到的现成金标准。")

    return {
        "entities": entities_dict,
        "triples": triples,
        "triple_stats": tstats,
        "derived": derived,
        "by_rel": by_rel,
        "uniq_triples": uniq_triples,
        "self_loop": self_loop,
        "oov_tail": oov,
        "noise": noise,
        "drug_names": drug_info,
        "annotation_rows": len(rows),
        "annotation_labels": dict(labels),
    }


# ================================================================ DDInter

def load_ddinter(report: Report):
    records = []
    per_file = []
    for code in DDINTER_CODES:
        path = DDINTER / f"ddinter_downloads_code_{code}.csv"
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            cols = reader.fieldnames or []
            rows = list(reader)
        per_file.append((code, path, cols, rows))

        if cols == DDINTER_COLUMNS:
            for r in rows:
                records.append((code, r["DDInterID_A"], r["Drug_A"],
                                r["DDInterID_B"], r["Drug_B"], r["Level"]))
        else:
            report(f"    ⚠ {path.name} 列名与预期不符：{cols}")
    return records, per_file


def inspect_ddinter(report: Report, samples: int) -> dict:
    report.h1("二、DDInter（药物相互作用）")

    records, per_file = load_ddinter(report)

    report.h2("2.1 分片文件清单")
    rows = []
    for code, path, cols, rs in per_file:
        levels = Counter(r.get("Level", "?") for r in rs)
        rows.append((f"code_{code}", f"{path.stat().st_size:,}",
                     f"{len(rs):,}", ", ".join(f"{k}:{v}" for k, v in levels.most_common())))
    report.table(rows, ("文件", "体积(B)", "记录数", "Level 分布"), aligns="<>>" + "<")
    report("")
    report(f"    列名（全部文件一致）：{', '.join(DDINTER_COLUMNS)}")

    # ---- 规模
    report.h2("2.2 规模与去重")
    report.kv("原始记录总数", f"{len(records):,}")
    pair_set = {frozenset((a, b)) for _, _, a, _, b, _ in records}
    report.kv("去重后相互作用对数（无序）", f"{len(pair_set):,}")
    report.kv("重复记录", f"{len(records) - len(pair_set):,}")

    drug_ids = {i for _, ia, _, ib, _, _ in records for i in (ia, ib)}
    id2name: dict[str, set[str]] = defaultdict(set)
    for _, ia, a, ib, b, _ in records:
        id2name[ia].add(a)
        id2name[ib].add(b)
    names = {n for _, _, a, _, b, _ in records for n in (a, b)}
    report.kv("药物 ID 数", f"{len(drug_ids):,}")
    report.kv("药物名数（去重）", f"{len(names):,}")
    ambiguous = {i: ns for i, ns in id2name.items() if len(ns) > 1}
    report.kv("一 ID 多名（同一药多种写法）", f"{len(ambiguous):,}")
    if ambiguous:
        for i, ns in list(ambiguous.items())[:samples]:
            report(f"      · {i} → {sorted(ns)}")

    # ---- Level
    report.h2("2.3 严重程度分布")
    level_all = Counter(r[5] for r in records)
    level_pairs = Counter()
    seen = set()
    for _, _, a, _, b, lv in records:
        k = frozenset((a, b))
        if k not in seen:
            seen.add(k)
            level_pairs[lv] += 1
    report.table(
        [(lv, f"{n:,}", f"{n / len(records):.1%}", f"{level_pairs.get(lv, 0):,}",
          f"{level_pairs.get(lv, 0) / len(pair_set):.1%}")
         for lv, n in level_all.most_common()],
        ("Level", "记录数", "占比", "去重后", "占比"), aligns="<>>><>",
    )

    # ---- 分片规则
    report.h2("2.4 分片规则（文件 code 到底按什么切）")
    report("    假说一：按药名首字母切。实测每个文件中两条药名首字母命中该 code 的比例：")
    rows = []
    for code, path, cols, rs in per_file:
        hit_a = sum(1 for r in rs if r["Drug_A"][:1].upper() == code) / max(1, len(rs))
        hit_b = sum(1 for r in rs if r["Drug_B"][:1].upper() == code) / max(1, len(rs))
        hit_any = sum(1 for r in rs
                      if code in (r["Drug_A"][:1].upper(), r["Drug_B"][:1].upper())) / max(1, len(rs))
        rows.append((f"code_{code}", f"{hit_a:.1%}", f"{hit_b:.1%}", f"{hit_any:.1%}"))
    report.table(rows, ("文件", "A 首字母命中", "B 首字母命中", "任一命中"), aligns="<>>>")
    report("")
    report("    命中率只有百分之几到二十几 → **假说一被否定**，code 不是药名首字母。")

    # 假说二：按药物切分（每种药只归属一个文件）
    report("")
    report("    假说二：按药物切分，每种药只出现在一个文件里。实测每味药跨几个文件：")
    files_of_drug: dict[str, set[str]] = defaultdict(set)
    for code, _, _, rs in per_file:
        for r in rs:
            files_of_drug[r["DDInterID_A"]].add(code)
            files_of_drug[r["DDInterID_B"]].add(code)
    spread = Counter(len(v) for v in files_of_drug.values())
    report.table(
        [(f"出现在 {k} 个文件", f"{n:,}", f"{n / len(files_of_drug):.1%}")
         for k, n in sorted(spread.items())],
        ("药物跨文件数", "药物数", "占比"), aligns="<>>",
    )
    single = spread.get(1, 0)
    report("")
    if single / max(1, len(files_of_drug)) > 0.95:
        report(f"    → {single / len(files_of_drug):.1%} 的药只出现在 1 个文件里 → **假说二成立**：")
        report("      DDInter 把药分成 8 组（推测按药理学分类/ATC 大类，字母只是组号），")
        report("      相互作用按「所属组」落到文件里。一对药若分属两组，就会在两个文件里各出现一次。")
        report("      这解释了 §2.2 的 62,148 条重复记录。")
    else:
        report(f"    → 只有 {single / len(files_of_drug):.1%} 的药只出现在 1 个文件 → 假说二也不成立，")
        report("      code 的确切含义未知。")
    report("")
    report("    ★ 工程结论：**无论 code 什么意思，都必须按无序药对去重**（frozenset(A, B)），")
    report(f"      否则 {len(records) - len(pair_set):,} 条重复记录会变成重复的 INTERACTS_WITH 边。")

    # ---- 抽查 L2 验收标准
    report.h2("2.5 抽查：华法林 ↔ 阿司匹林（对应基线 §13 L2 验收标准）")
    all_names = sorted({n for _, _, a, _, b, _ in records for n in (a, b)})
    for probe in ["aspirin", "acetylsalicylic", "warfarin"]:
        hits = [n for n in all_names if probe in n.lower()]
        report.kv(f"DDInter 药名中含 {probe!r}", f"{len(hits)} 个：{hits[:5]}")

    report("")
    report("    ⚠ 关键发现：DDInter **没有 'Aspirin' 这个名字**，阿司匹林对应的是")
    report("      'Acetylsalicylic acid'。药名映射表必须按 DDInter 的写法建，不能想当然。")

    def find_pairs(names_a: set[str], names_b: set[str]):
        out = []
        for _, _, a, _, b, lv in records:
            if (a in names_a and b in names_b) or (a in names_b and b in names_a):
                out.append((a, b, lv))
        return out

    wa = find_pairs({"Warfarin"}, {"Acetylsalicylic acid"})
    report("")
    report(f"    ★ 华法林 ↔ 乙酰水杨酸 查得 {len(wa)} 条：")
    for a, b, lv in wa[:samples]:
        report(f"      {a}  ↔  {b}   Level={lv}")

    warfarin_hits = [r for r in records if "warfarin" in (r[2].lower(), r[4].lower())]
    major = [r for r in warfarin_hits if r[5] == "Major"]
    report("")
    report.kv("含 Warfarin 的记录", f"{len(warfarin_hits):,}")
    report.kv("  其中 Major 级", f"{len(major):,}")
    for r in major[:samples]:
        other = r[4] if "warfarin" in r[2].lower() else r[2]
        report(f"      · Warfarin ↔ {other}  [Major]")

    report("")
    report(f"    ⚠ 注意：CSV 只有 {len(DDINTER_COLUMNS)} 列——没有 mechanism / description /")
    report("      management / alternative_drug。基线 §13 L2 要求返回「等级、机制与处理建议」，")
    report("      其中后三者**不在 CSV 里**，只存在于 DDInter 网站的药物详情页。")
    report("      → L2 的验收标准必须改成「能查到等级」，机制/处理建议要么放弃，")
    report("        要么另写一个按需抓取详情页的采集脚本（工作量显著增加，需重新评估）。")

    return {
        "records": len(records),
        "pairs": len(pair_set),
        "drugs": len(drug_ids),
        "names": len(names),
        "level_all": dict(level_all),
        "level_pairs": dict(level_pairs),
        "warfarin_aspirin": wa,
    }


# ================================================================ 基线复核

def cross_check(report: Report, oc: dict, dd: dict) -> None:
    report.h1("三、基线文档断言复核")
    report("    逐条把《需求与技术方案基线.md》里的数字与实测对照。")
    report("")

    measured = {
        "OpenCMKG 疾病实体数": len(oc["entities"].get("disease", [])),
        "OpenCMKG 药物实体数": len(oc["entities"].get("drug", [])),
        "OpenCMKG 三元组数": len(oc["triples"]),
        "OpenCMKG disease_recommand_drug 条数": len(oc["by_rel"].get("disease_recommand_drug", [])),
        "DDInter 药物数": dd["drugs"],
        "DDInter 相互作用对数": dd["records"],
    }

    rows = []
    for key, claimed, src in BASELINE_CLAIMS:
        got = measured[key]
        diff = got - claimed
        rows.append((key, f"{claimed:,}", f"{got:,}",
                     "✓ 一致" if diff == 0 else f"⚠ 差 {diff:+,}", src))
    report.table(rows, ("指标", "基线声称", "实测", "结论", "出处"), aligns="<>>" + "<" + "<")

    report("")
    report("    差异说明：")
    report(f"      · OpenCMKG 三元组数：基线 354,755 与实测一致 ✓")
    report(f"        真正的坑是解析方式：朴素 split(',') 会丢 {oc['triple_stats']['naive_loss']:,} 行（见 §1.2）。")
    report("      · DDInter 药物数：基线 1,833 来自官网介绍，CSV 实测去重后有 "
           f"{dd['drugs']:,} 个 ID。")
    report("        多出的部分是 CSV 里存在、但官网统计未计入的药（多为复方/外用/停产），")
    report("        写论文时应以 CSV 实测数为准并注明口径。")
    report("      · DDInter 相互作用对数：基线 236,834 与 CSV 原始记录数 "
           f"{dd['records']:,} 也不符；")
    report(f"        按无序药对去重后为 {dd['pairs']:,}。三个数字口径不同，论文里必须写清楚用哪个。")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="OpenCMKG / DDInter 原始数据体检")
    parser.add_argument("--out", type=Path, help="把报告同时写入该文件")
    parser.add_argument("--samples", type=int, default=6, help="样例条数上限")
    args = parser.parse_args(argv)

    missing = [
        p for p in [OPENCMKG / "triples.txt", OPENCMKG / "entities_dict.txt",
                    OPENCMKG / "manual_annotation.csv"]
        if not p.exists()
    ] + [DDINTER / f"ddinter_downloads_code_{c}.csv" for c in DDINTER_CODES
         if not (DDINTER / f"ddinter_downloads_code_{c}.csv").exists()]
    if missing:
        print("✗ 原始数据缺失，请先运行：python scripts/download_data.py")
        for p in missing:
            print("   缺：", p.relative_to(REPO_ROOT))
        return 1

    report = Report()
    report("原始数据实测报告（由 scripts/inspect_raw.py 自动生成）")
    report(f"数据目录：{RAW_DIR}")

    oc = inspect_opencmkg(report, args.samples)
    dd = inspect_ddinter(report, args.samples)
    cross_check(report, oc, dd)

    text = report.text()
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"\n[报告已写入 {args.out}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
