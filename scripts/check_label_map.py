#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
翻译表防漂移检查
================

## 为什么需要

`frontend/src/labels.js` 里的 `TYPE_LABEL` 要同时覆盖**节点标签**
（`Disease` / `Symptom` …）和**关系类型**（`INTERACTS_WITH` …）。
它漏过两次，而且都是同一个后果：**英文枚举直接显示给用户**。

第一次：只写了 4 个节点标签，界面上出现 `Food`、`Department`。
第二次：关系类型漏了 6 个、还写错 2 个（`BELONGS_TO` ≠ `BELONG_DEPARTMENT`），
界面上出现 `COMMON_DRUG`、`EAT_FOOD`。

**人肉维护"图里的枚举清单"是记不住的**，所以把这件事交给脚本：
从 Neo4j 查出全部标签与关系类型，跟 `labels.js` 里的键做差集，缺一个就报错退出。

用法
----
    cd backend
    .venv\\Scripts\\python.exe ..\\scripts\\check_label_map.py
"""

from __future__ import annotations

import io
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LABELS_JS = REPO_ROOT / "frontend" / "src" / "labels.js"


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    src = LABELS_JS.read_text(encoding="utf-8")
    m = re.search(r"const TYPE_LABEL = \{(.*?)\n\}", src, re.S)
    if not m:
        print("  ✗ 在 labels.js 里找不到 TYPE_LABEL")
        return 2
    keys = set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", m.group(1), re.M))
    print(f"  labels.js 收录 {len(keys)} 个键")

    pwd = os.getenv("NEO4J_PASSWORD", "")
    if not pwd:
        for line in (REPO_ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("NEO4J_PASSWORD"):
                pwd = line.split("=", 1)[1].strip()
                break

    from neo4j import GraphDatabase
    drv = GraphDatabase.driver("bolt://127.0.0.1:7687", auth=("neo4j", pwd))
    try:
        with drv.session() as s:
            node_labels = {r["l"] for r in s.run(
                "CALL db.labels() YIELD label RETURN label AS l")}
            rel_types = {r["t"] for r in s.run(
                "CALL db.relationshipTypes() YIELD relationshipType AS t "
                "RETURN t")}
    finally:
        drv.close()

    # 有些标签是内部的（例如分页/迁移用的），不需要翻译
    IGNORE = {"X"}
    missing = sorted((node_labels | rel_types) - keys - IGNORE)
    print(f"  图里标签 {len(node_labels)} 个 / 关系类型 {len(rel_types)} 个")

    if missing:
        print(f"\n  ✗ 有 {len(missing)} 个枚举还没翻译（会原样显示成英文）：")
        for x in missing:
            kind = "节点标签" if x in node_labels else "关系类型"
            print(f"      {x}   （{kind}）")
        print("\n  去 frontend/src/labels.js 的 TYPE_LABEL 里补上。")
        return 1

    print("\n✓ 翻译表已覆盖图里全部枚举")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
