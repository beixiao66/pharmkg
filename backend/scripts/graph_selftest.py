#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L8/L9 · 图谱浏览接口自测（`/graph/*`）
=====================================

三个接口：实体检索、邻域子图、最短路径。

测的重点是**上限与诚实**——图谱浏览最容易出的两个问题是"太大拖死前端"和
"画了一部分但没说"：

* **截断必须显式**：`卡维地洛片` 在图里有 338 条边，`limit=8` 时
  `truncated` 必须为真，否则用户会以为"它只和 8 个东西有关"。
* **按度数排序**：实体检索按连边数降序——孤点进去是空白页。
* **路径要有跳数上限**：不设上限就是全图搜索；且**超时 ≠ 没有路径**，要分开报。
* **不存在要 404**，不能返回空图。

用法
----
    cd backend
    .venv\\Scripts\\python.exe scripts\\graph_selftest.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient          # noqa: E402
from sqlalchemy import delete, select              # noqa: E402

from app.config import get_settings                # noqa: E402
from app.db import SessionLocal                    # noqa: E402
from app.main import app                           # noqa: E402
from app.models import AppUser                     # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASSED if ok else FAILED).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"   {detail}" if detail else ""),
          flush=True)
    return ok


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    st = get_settings()
    if not st.jwt_configured:
        print("✗ JWT_SECRET 未设置")
        return 2

    stamp = str(int(time.time()))
    user = f"l8graph_{stamp}"
    pwd = "graph-selftest-pw-123456"
    client = TestClient(app)

    print("图谱浏览接口自测")
    print(f"  测试用户  {user}（跑完会删掉）")
    print()

    try:
        print("── 鉴权 ──")
        for path, params in (("/graph/entities", {"q": "卡维"}),
                             ("/graph/subgraph", {"name": "卡维地洛片"}),
                             ("/graph/path",
                              {"src": "卡维地洛片", "dst": "孕妇"})):
            r = client.get(path, params=params)
            check(f"没 token → 401  {path}", r.status_code == 401,
                  f"→ {r.status_code}")

        r = client.post("/auth/register", json={"username": user, "password": pwd})
        r.raise_for_status()
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}

        print("\n── 实体检索 ──")
        r = client.get("/graph/entities", params={"q": "卡维地洛"}, headers=h)
        hits = r.json() if r.status_code == 200 else []
        check("200 且命中", r.status_code == 200 and bool(hits), f"{len(hits)} 个")
        check("带标签与度数", all("label" in x and "degree" in x for x in hits[:2]))
        check("卡维地洛片 的度数为正（它有 300+ 条边）",
              any(x["name"] == "卡维地洛片" and x["degree"] > 0 for x in hits),
              str([(x["name"], x["degree"]) for x in hits[:2]]))
        check("**按度数降序**（孤点不该排前面）",
              all(hits[i]["degree"] >= hits[i + 1]["degree"]
                  for i in range(len(hits) - 1)),
              str([x["degree"] for x in hits[:5]]))

        print("\n── 邻域子图 ──")
        r = client.get("/graph/subgraph",
                       params={"name": "卡维地洛片", "limit": 8}, headers=h)
        d = r.json() if r.status_code == 200 else {}
        check("200", r.status_code == 200, f"→ {r.status_code}")
        check("中心节点正确", d.get("center") == "卡维地洛片", str(d.get("center")))
        check("边数不超过 limit", len(d.get("edges") or []) <= 8,
              str(len(d.get("edges") or [])))
        check("**truncated=True**（该实体有 300+ 条边，只画 8 条必须说出来）",
              d.get("truncated") is True, str(d.get("truncated")))
        check("中心节点在节点表里",
              any(n["id"] == "卡维地洛片" for n in (d.get("nodes") or [])))
        check("每条边的两端都在节点表里",
              all(e["source"] in {n["id"] for n in d["nodes"]}
                  and e["target"] in {n["id"] for n in d["nodes"]}
                  for e in (d.get("edges") or [])))

        r = client.get("/graph/subgraph",
                       params={"name": "不存在的实体"}, headers=h)
        check("不存在的实体 → 404", r.status_code == 404, f"→ {r.status_code}")

        print("\n── 路径追溯 ──")
        r = client.get("/graph/path",
                       params={"src": "卡维地洛片", "dst": "孕妇", "max_hops": 4},
                       headers=h)
        p = r.json() if r.status_code == 200 else {}
        check("200 且找到路径", r.status_code == 200 and p.get("found") is True,
              f"→ {r.status_code} found={p.get('found')}")
        check("hop 数与边数一致", p.get("hops") == len(p.get("edges") or []),
              f"{p.get('hops')} vs {len(p.get('edges') or [])}")
        check("路径首尾对得上",
              (p.get("nodes") or [{}])[0].get("id") == "卡维地洛片"
              and (p.get("nodes") or [{}])[-1].get("id") == "孕妇",
              str([n["id"] for n in (p.get("nodes") or [])]))
        check("路径不超过 max_hops", (p.get("hops") or 0) <= 4, str(p.get("hops")))

        r = client.get("/graph/path",
                       params={"src": "卡维地洛片", "dst": "不存在"}, headers=h)
        check("端点不存在 → 404", r.status_code == 404, f"→ {r.status_code}")
        r = client.get("/graph/path",
                       params={"src": "卡维地洛片", "dst": "孕妇", "max_hops": 7},
                       headers=h)
        check("**跳数超过上限(6) → 422**（不设上限就是全图搜索）",
              r.status_code == 422, f"→ {r.status_code}")

    finally:
        with SessionLocal() as db:
            db.execute(delete(AppUser).where(AppUser.username == user))
            db.commit()
            left = db.execute(select(AppUser.id).where(
                AppUser.username == user)).scalars().all()
        print("\n── 清理 ──")
        check("测试用户已删除", not left)

    print()
    print("── 汇总 ──")
    print(f"  通过 {len(PASSED)}  失败 {len(FAILED)}")
    if FAILED:
        for n in FAILED:
            print(f"    ✗ {n}")
        return 1
    print("✓ 图谱浏览接口自测全过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
