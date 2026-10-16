#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
拿 §5 的 schema 表去核 Neo4j（文档不再是人写的、没人核的孤本）
================================================================

**为什么需要这个**：2026-10-16 发现 §5 的 `Drug` 属性列写的是
``id, name_cn, name_en, aliases[], atc_code``——那是照 **OpenCMKG 源字段名**抄的，
而入库时只落了 ``id / name / name_en / source``。实测 ``name_cn`` / ``aliases`` /
``atc_code`` **在图里一个都不存在**。

**这个错写会直接毁掉 L5**：照它写 Text2Cypher 的提示词，生成的每一条 Cypher
都会写成 ``d.name_cn`` → 全是 null → 全部查不到，而现象看起来像"图谱里没有数据"。
**文档错了，比没有文档更危险**——因为它会被信。

所以：这张表从此**逐项对图核**。核两件事：

1. **属性**：表里声明的每个属性，在该标签上必须真有（至少一个节点非空）。
2. **规模**：表里那个"实测"数字必须等于图里的真实节点/关系数。

顺带核**方向**——`RECOMMEND_DRUG` 是 `Disease → Drug` 这件事，
是 L5 写 Cypher 时最容易搞反的地方（"这药治什么"要走反向）。

用法
----
    python scripts/check_schema_doc.py
    python scripts/check_schema_doc.py --doc docs/需求与技术方案基线.md
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

from opencmkg_io import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "backend"))

DEFAULT_DOC = REPO_ROOT / "docs" / "需求与技术方案基线.md"

#: §5 里那两张表的标题，用来定位表格块。
NODE_HEADING = "**节点**"
RELATION_HEADINGS = ("**关系**", "**新增关系")

#: 不在图上核的标签（占位或未使用）。
SKIP_LABELS = {"Label"}

NUM_RE = re.compile(r"\d[\d,]*")
#: 属性列表在 markdown 里是**一整个反引号块**（`` `id, name, name_en, source` ``），
#: 所以要先整块取出、**再按逗号拆**。踩过：直接 findall 会把整串当成一个属性名，
#: 于是查 `n.\`id, name, name_en, source\`` —— 报"属性不存在"，其实是我自己解析错了。
PROP_RE = re.compile(r"`([^`]+)`")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def parse_props(cell: str, label: str) -> list[str]:
    """从"关键属性"那一格里取出属性名列表（按逗号拆、去掉 ``[]``、过滤非标识符）。"""
    props: list[str] = []
    for chunk in PROP_RE.findall(cell):
        for piece in chunk.split(","):
            piece = piece.strip().strip("`").strip()
            if piece.endswith("[]"):
                piece = piece[:-2]
            if not piece or piece == label:
                continue
            m = IDENT_RE.fullmatch(piece)
            if m:
                props.append(piece)
    return props


def log(msg: str = "") -> None:
    print(msg, flush=True)


def parse_tables(text: str) -> tuple[list[list[str]], list[list[str]]]:
    """从 markdown 里切出节点表与关系表的行（``|`` 分隔，已去空行与表头分隔行）。"""
    lines = text.split("\n")

    def collect(start: int) -> list[list[str]]:
        rows: list[list[str]] = []
        started = False
        for line in lines[start:]:
            s = line.strip()
            if s.startswith("|"):
                started = True
                cells = [c.strip() for c in s.strip("|").split("|")]
                if set("".join(cells)) <= set("-: "):
                    continue                      # 表头分隔行
                rows.append(cells)
            elif started:
                break
        return rows[1:] if rows else []           # 去掉表头

    node_rows: list[list[str]] = []
    rel_rows: list[list[str]] = []
    for i, line in enumerate(lines):
        if line.strip().startswith(NODE_HEADING):
            node_rows = collect(i)
        elif any(line.strip().startswith(h) for h in RELATION_HEADINGS):
            rel_rows.extend(collect(i))
    return node_rows, rel_rows


def declared_count(cell: str) -> int | None:
    """取那个格子里的"实测"值。

    规则：**有「实测」就取它后面第一个数**，否则取最后一个数。
    例：``4,289（中文）/ 1,939（英文 DDI）→ **实测入库 5,696**`` → 5696
        ``**实测 17,974**（原写 17,977，差 3，原因未查明）`` → 17974
        ``16,197 → 17,135``（没有"实测"）→ 17135

    ⚠️ 只取"最后一个数"是不够的：改正说明里往往还带着旧数字（"原写 17,977，差 3"），
    最后一个数会变成 **3** —— 于是检查器报出一个假的不符（踩过）。
    """
    clean = cell.replace("**", "")
    m = re.search(r"实测[^\d]{0,4}(\d[\d,]*)", clean)
    if m:
        return int(m.group(1).replace(",", ""))
    nums = NUM_RE.findall(clean)
    if not nums:
        return None
    return int(nums[-1].replace(",", ""))


def parse_direction(cell: str) -> tuple[list[str], list[str], str] | None:
    """把 ``Disease → Drug`` / ``Drug ↔ Drug`` / ``Drug → Disease/Population`` 拆开。"""
    c = cell.replace("`", "").strip()
    for arrow, kind in (("→", "one"), ("↔", "both"), ("->", "one")):
        if arrow in c:
            left, right = c.split(arrow, 1)
            # ⚠️ 先把 markdown 的 **粗体** 星号剥掉：§5 里写着 ``Drug → **Disease**/Population``，
            #    不剥就会去查一个叫 `**Disease**` 的标签 → 0 实例 → 假报错（踩过）。
            clean = lambda xs: [x.replace("**", "").strip()
                                for x in xs if x.replace("**", "").strip()]
            src = clean(left.split("/"))
            dst = clean(right.split("/"))
            return src, dst, kind
    return None


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="拿 §5 的 schema 表核 Neo4j")
    ap.add_argument("--doc", type=Path, default=DEFAULT_DOC)
    args = ap.parse_args(argv)
    # ⚠️ 先 resolve 再 relative_to：传相对路径时 relative_to 会抛 ValueError，
    #    结果**脚本崩溃、退出码也是 1** —— 负向测试看起来"通过了"，其实是崩了。
    #    踩过：差点把崩溃当成"检查器检出错误"。（负向测试本身也会骗人。）
    doc = args.doc.resolve()
    try:
        doc_display: object = doc.relative_to(REPO_ROOT)
    except ValueError:
        doc_display = doc

    from app.config import get_settings
    from neo4j import GraphDatabase

    text = doc.read_text(encoding="utf-8")
    node_rows, rel_rows = parse_tables(text)
    st = get_settings()
    driver = GraphDatabase.driver(st.neo4j_uri,
                                 auth=(st.neo4j_user, st.neo4j_password))

    log("§5 schema 表 · 对图核验")
    log(f"  文档    {doc_display}")
    log(f"  节点表  {len(node_rows)} 行    关系表  {len(rel_rows)} 行")
    log(f"  Neo4j   {st.neo4j_uri}")
    log("")

    bad: list[str] = []
    empty_rels: list[str] = []
    with driver.session() as s:
        log("── 节点：属性与规模 ──")
        for cells in node_rows:
            if len(cells) < 4 or cells[0] in SKIP_LABELS:
                continue
            label = cells[0].strip("`")
            props = parse_props(cells[1], label)
            claimed = declared_count(cells[3])

            actual = s.run(f"MATCH (n:`{label}`) RETURN count(n) AS c"
                           ).single()["c"]

            # ① 属性必须真存在（至少一个节点非空）
            missing = []
            for p in props:
                n = s.run(f"MATCH (n:`{label}`) WHERE n.`{p}` IS NOT NULL "
                          f"RETURN count(n) AS c").single()["c"]
                if n == 0:
                    missing.append(p)

            ok_count = (claimed is None) or (claimed == actual)
            ok = not missing and ok_count
            if not ok:
                bad.append(f"{label}")
            log(f"  {'✓' if ok else '✗'} {label}")
            log(f"      属性声明 {props}")
            if missing:
                log(f"      ✗ **图里不存在这些属性**：{missing}"
                    f"  ← 照它写 Cypher 会全是 null")
            log(f"      节点数 实测 {actual:,}"
                + (f"  声明 {claimed:,}" + ("" if ok_count else "  ✗ 不符")
                   if claimed is not None else "  声明 （表格里没有数字）"))

        log("")
        log("── 关系：存在性、方向与规模 ──")
        for cells in rel_rows:
            if len(cells) < 4:
                continue
            rel = cells[0].strip("`")
            parsed = parse_direction(cells[1])
            if parsed is None:
                continue
            src, dst, kind = parsed
            try:
                total = s.run(f"MATCH ()-[r:`{rel}`]->() RETURN count(r) AS c"
                              ).single()["c"]
            except Exception as exc:  # noqa: BLE001
                log(f"  ✗ {rel}  查不通：{type(exc).__name__}")
                bad.append(rel)
                continue

            # 方向：至少存在一条满足声明标签的实例
            dirs_ok = {}
            for a in src:
                for b in dst:
                    pat = (f"MATCH (x:`{a}`)-[r:`{rel}`]-(y:`{b}`) "
                           if kind == "both" else
                           f"MATCH (x:`{a}`)-[r:`{rel}`]->(y:`{b}`) ")
                    dirs_ok[f"{a}-{b}"] = s.run(pat + "RETURN count(r) AS c"
                                                ).single()["c"]
            any_dir = any(v > 0 for v in dirs_ok.values())
            claimed = declared_count(cells[4]) if len(cells) > 4 else None
            ok_count = (claimed is None) or (claimed == total)
            # 方向声明里写的可能**不是标签**（如 SAME_AS 的"实体 ↔ 实体"），
            # 那种情况没得核，跳过方向判定而不是判它错。
            # 方向声明里写的可能**不是标签**（如 SAME_AS 的"实体 ↔ 实体"）——
            # 那就没得核，跳过方向判定而不是判它错。
            checkable_dir = any(
                s.run("MATCH (n) WHERE $x IN labels(n) RETURN count(n) AS c LIMIT 1",
                      x=x).single()["c"] > 0 for x in src + dst)
            empty = (total == 0)
            # 零实例的关系**方向无法核**（没有实例可看方向）→ 降为警告。
            # CAUTION_FOR 就是这种：决策 #38 决定"先抽不入图"，图里本来就该是空的。
            ok_dir = any_dir if (checkable_dir and not empty) else True
            ok = ok_dir and ok_count and not empty
            if not ok and not (empty and ok_dir and ok_count):
                bad.append(rel)
            elif empty and ok_dir and ok_count:
                empty_rels.append(rel)
            mark = "⚠️" if (empty and ok_dir and ok_count) else ("✓" if ok else "✗")
            log(f"  {mark} {rel}   （声明 {cells[1]}）")
            log(f"      实例 实测 {total:,} / 声明 "
                + (f"{claimed:,}" if claimed is not None else "（无）"))
            if empty:
                tail = "  （零实例：方向无从核起）"
            elif any_dir:
                tail = ""
            elif not checkable_dir:
                tail = "  （方向声明不是标签，跳过）"
            else:
                tail = "  ✗ 声明的方向一条都没有"
            log(f"      方向 " + "  ".join(f"{k}:{v:,}" for k, v in dirs_ok.items())
                + tail)

    driver.close()

    log("")
    log("── 汇总 ──")
    if empty_rels:
        log(f"  ⚠️ 有 {len(empty_rels)} 个关系**声明了但图里一条实例都没有**：{empty_rels}")
        log("     不判失败，但要确认这是**刻意的**（CAUTION_FOR 就是：决策 #38 决定先抽不入图）。")
        log("     若不是刻意的，说明入库没做全。")
    if bad:
        log(f"  ✗ 有 {len(bad)} 处与图不符：{bad}")
        log("    文档错了比没有文档更危险——它会被信。请改文档或改图。")
        return 1
    log("  ✓ §5 的节点属性、规模与关系方向**全部与图一致**")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
