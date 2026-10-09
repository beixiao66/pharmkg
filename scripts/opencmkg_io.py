#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenCMKG 原始文件读取层
=======================

``data/raw/opencmkg/`` 下的文件有两个**非常隐蔽**的坑，踩过一次就够了：

**坑一：``entities_dict.txt`` 是单行 Python dict 字面量，而且有三处毛病。**
1.4 MB 全在第一行，必须用 ``ast`` 解析。三处毛病是：
① 混进 1 个裸标识符 ``nan``（pandas 缺失值直接 ``str()`` 的产物）；
② ``symptom`` 列表里**嵌套了一个内层 list**，装着 6,834 个元素（上游把一整批
症状当成单个元素了）——不展平等于丢掉这 6,834 个症状，而 ``str()`` 之会造出
一个 71,618 字符的"症状名"，**直接撑爆 Neo4j 的索引长度上限**；
③ 展平后会出现重复。三者都在 :func:`load_entities_dict` 里统一处理并计数。

**坑二：``triples.txt`` 是合法 CSV，字段含逗号时会加双引号——但只给需要的字段加。**

    百日咳,disease_need_check,"耳,鼻,咽拭子细菌培养"      ← 尾部带引号
    "跖骨,趾骨骨折",disease_has_symptom,疲劳              ← 头部带引号

后果：``line.split(",")`` 会**静默丢掉** 2,171 行；``line.split(",", 2)``
数量看着对，却会把"头部带引号"的行切错位，**造出 ``趾骨骨折"`` 这种根本
不存在的关系名**。只有 ``csv`` 模块是对的。

这两个坑抽到这里由所有脚本共用——导入脚本和体检脚本如果各写一份，
将来修了一处，另一处还带着 bug。

实测依据见 ``docs/数据源实测报告.md`` §2.1、§2.2。
"""

from __future__ import annotations

import ast
import csv
from pathlib import Path

# ---------------------------------------------------------------- 路径

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = REPO_ROOT / "data" / "raw"
OPENCMKG_DIR = RAW_DIR / "opencmkg"
DDINTER_DIR = RAW_DIR / "ddinter"

ENTITIES_DICT_FILE = OPENCMKG_DIR / "entities_dict.txt"
TRIPLES_FILE = OPENCMKG_DIR / "triples.txt"

#: 所有由本模块写入图的节点/边都带这个来源标记（基线 §13.8 的统一来源标记约定）
SOURCE_TAG = "opencmkg"


# ---------------------------------------------------------------- Schema

#: ``entities_dict.txt`` 的顶层键 → Neo4j 节点 Label
#:
#: 注意 ``check`` **不在** entities_dict 里（实测：``disease_need_check`` 的
#: 3,351 个尾实体有 100% 不在词表中），只能从三元组现场收集。
NODE_LABEL = {
    "disease": "Disease",
    "drug": "Drug",
    "symptom": "Symptom",
    "food": "Food",
    "department": "Department",
    "producer": "Producer",
    "treatment": "Treatment",
    "check": "Check",
}

#: OpenCMKG 关系名 → (头实体类型, 尾实体类型, Neo4j 关系类型)
#:
#: 13 类关系全部来自实测（``docs/数据源实测报告.md`` §2.3），
#: 与基线 §5 的关系表一致。``SameAs`` 不在此表中——它两端类型要反查词表，
#: 且有 22.2% 的对端不在词表里，单独处理。
RELATION_TYPES: dict[str, tuple[str, str, str]] = {
    "disease_recommand_drug":       ("disease", "drug", "RECOMMEND_DRUG"),
    "disease_has_symptom":          ("disease", "symptom", "HAS_SYMPTOM"),
    "disease_recommand_food":       ("disease", "food", "RECOMMEND_FOOD"),
    "disease_need_check":           ("disease", "check", "NEED_CHECK"),
    "disease_noteat_food":          ("disease", "food", "NOTEAT_FOOD"),
    "disease_acompany_disease":     ("disease", "disease", "ACCOMPANY_DISEASE"),
    "disease_eat_food":             ("disease", "food", "EAT_FOOD"),
    "disease_need_treatment":       ("disease", "treatment", "NEED_TREATMENT"),
    "drug_relate_producer":         ("drug", "producer", "RELATE_PRODUCER"),
    "disease_common_drug":          ("disease", "drug", "COMMON_DRUG"),
    "disease_belong_department":    ("disease", "department", "BELONG_DEPARTMENT"),
    "department_belong_department": ("department", "department", "DEPT_BELONG_DEPT"),
}

SAME_AS_RELATION = "SameAs"
SAME_AS_TYPE = "SAME_AS"


# ---------------------------------------------------------------- 读取

def load_entities_dict(
    path: Path | None = None,
    *,
    triples: list[tuple[str, str, str]] | None = None,
) -> tuple[dict[str, list[str]], dict[str, int]]:
    """解析 ``entities_dict.txt``，返回 ``({类型: [实体名...]}, 异常统计)``。

    上游这个文件有**三处毛病**，全部在这里一次处理掉：

    1. **裸标识符 ``nan``**（pandas 缺失值直接 ``str()`` 的产物）会让
       ``ast.literal_eval`` 抛 ``ValueError``。用占位符 ``__NAN__`` 替换，
       而不是跳过整行——"上游数据里混了个 nan"本身就是一条质量结论。
    2. **嵌套 list**：``symptom`` 列表里塞了**一个内层 list**，装着 6,833 个条目。
       不展平的话这些条目等同于丢失；更糟的是 ``str(内层list)`` 会造出一个
       71,618 字符的"症状名"，**直接撑爆 Neo4j 的索引长度上限**。
    3. **展平后可能出现重复**，顺序保留地去重。

    关于嵌套 list 的类型归属——上游把它放在 ``symptom`` 下，但**内容是混装的**，
    实测（2026-09-24）证据：

    ==========================  ========  ==========================================
    内容                        数量      判定依据
    ==========================  ========  ==========================================
    检查项（血凝试验、CT造影…）  3,214     47% 是 ``disease_need_check`` 的尾实体；
                                          且占全部检查项尾实体的 **95.9%**
    患者口语（拉一次粑粑、…）    3,619     未被任何关系引用
    ——                          0         出现在 ``disease_has_symptom`` 里的数量
    ==========================  ========  ==========================================

    所以整体当 ``Symptom`` 会造出 4,985 个同名双标签节点（同一名字既是
    ``Symptom`` 又是 ``Check``），图谱里一个名字必须只有一个 Label。

    :param triples: 传入三元组后，会用 ``disease_need_check`` 关系做**逐条归位**：
        是该关系尾实体的 → 归 ``check``，其余保留上游标注的 ``symptom``。
        不传则全部并入 ``symptom``（只读文件、不看关系时用）。
    """
    path = path or ENTITIES_DICT_FILE
    raw = path.read_text(encoding="utf-8").strip()

    class _NanToConstant(ast.NodeTransformer):
        def __init__(self) -> None:
            self.count = 0

        def visit_Name(self, node: ast.Name):  # noqa: N802 —— ast API 命名
            self.count += 1
            return ast.copy_location(ast.Constant(value="__NAN__"), node)

    fixer = _NanToConstant()
    tree = fixer.visit(ast.parse(raw, mode="eval"))
    data = ast.literal_eval(tree)

    if not isinstance(data, dict):
        raise ValueError(f"entities_dict.txt 顶层不是 dict，而是 {type(data).__name__}")

    stats = {
        "nan_placeholders": fixer.count,
        "nested_lists": 0,
        "nested_elements": 0,
        "nested_as_check": 0,
        "nested_as_symptom": 0,
        "duplicates_removed": 0,
        "non_string_items": 0,
    }

    #: 归位证据：这些名字出现在 disease_need_check 的尾实体位置
    check_tails = (
        {t for _, r, t in triples if r == "disease_need_check"}
        if triples is not None else set()
    )

    entities: dict[str, list[str]] = {}
    extra: dict[str, list[str]] = {}          # 归位到别的类型的条目

    for etype, items in data.items():
        if not isinstance(items, list):
            raise ValueError(f"entities_dict[{etype!r}] 不是 list，而是 {type(items).__name__}")

        flat: list[str] = []
        for item in items:
            if isinstance(item, list):          # ← 毛病 2：嵌套 list
                stats["nested_lists"] += 1
                stats["nested_elements"] += len(item)
                for sub in item:
                    if not isinstance(sub, str):
                        stats["non_string_items"] += 1
                        continue
                    if sub in check_tails:
                        extra.setdefault("check", []).append(sub)
                        stats["nested_as_check"] += 1
                    else:
                        flat.append(sub)
                        if triples is not None:
                            stats["nested_as_symptom"] += 1
            elif isinstance(item, str):
                flat.append(item)
            else:
                stats["non_string_items"] += 1

        deduped = list(dict.fromkeys(flat))     # ← 毛病 3：去重（保留首次出现顺序）
        stats["duplicates_removed"] += len(flat) - len(deduped)
        entities[str(etype)] = deduped

    for etype, names in extra.items():
        merged = entities.get(etype, []) + names
        entities[etype] = list(dict.fromkeys(merged))

    return entities, stats


def load_triples(path: Path | None = None) -> tuple[list[tuple[str, str, str]], dict[str, int]]:
    """解析 ``triples.txt``，返回 ``([(头, 关系, 尾)...], 统计)``。

    **必须用 csv 模块**，理由见模块开头。统计里的 ``naive_loss`` 是
    "用 ``line.split(',')`` 会丢多少行"——留作质量证据。
    """
    path = path or TRIPLES_FILE
    raw_lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    stats = {
        "total": len(raw_lines),
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


def build_name_index(entities: dict[str, list[str]]) -> dict[str, str]:
    """实体名 → 类型 的反查表。

    实测 53,863 个实体名**没有一个跨类型**（100% 唯一），所以这里的
    "后者覆盖前者"不会真的丢信息；仍返回唯一映射以便调用方直接用。
    """
    index: dict[str, str] = {}
    for etype, names in entities.items():
        for name in names:
            index[name] = etype
    return index
