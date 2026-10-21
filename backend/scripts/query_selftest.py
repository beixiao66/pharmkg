#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L8/L9 · 医药查询接口自测（`/query/*`）
=====================================

三个接口：疾病名检索、按病查推荐用药、两两相互作用直查。

测的重点仍然是**这个功能最容易骗人的地方**：

* **垃圾药名必须被过滤**：实测有 30 个 `quality='junk'` 的名字（`SP`/`Ⅰ`/`冻干`/
  `8-11`…）被当成推荐用药，出现 591 次。接口过滤一层，页面才好放心显示。
* **"未收录"必须显式且带风险提示**：`found=False` 时要有 `risk_hint`，
  且标签是「未收录」而不是绿色对勾——"没查到"和"没有"分不开。
* **严重程度未知不等于轻微**：`Unknown` 要提示按最坏情况对待。
* **不存在的东西要 404**，不能返回空列表让人以为"没有记录"。

用法
----
    cd backend
    .venv\\Scripts\\python.exe scripts\\query_selftest.py
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

#: 已知的垃圾药名（`scripts/flag_junk_nodes.py` 打的标）
JUNK = {"冻干", "Ⅰ", "Ⅱ", "SP", "TP", "8-11", "30R", "pH4", "1"}


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
    user = f"l8query_{stamp}"
    pwd = "query-selftest-pw-123456"
    client = TestClient(app)

    print("医药查询接口自测")
    print(f"  测试用户  {user}（跑完会删掉）")
    print()

    try:
        print("── 鉴权 ──")
        for path, params in (("/query/diseases", {"q": "类风湿"}),
                             ("/query/disease-drugs", {"name": "类风湿关节炎"}),
                             ("/query/interaction",
                              {"drug_a": "卡维地洛片", "drug_b": "依度沙班"})):
            r = client.get(path, params=params)
            check(f"没 token → 401  {path}", r.status_code == 401,
                  f"→ {r.status_code}")

        r = client.post("/auth/register", json={"username": user, "password": pwd})
        r.raise_for_status()
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}

        print("\n── 疾病名检索 ──")
        r = client.get("/query/diseases", params={"q": "类风湿"}, headers=h)
        hits = r.json() if r.status_code == 200 else []
        check("200 且命中", r.status_code == 200 and bool(hits), f"{len(hits)} 个")
        check("按『推荐用药条数』降序（用户要的是能查出东西的病）",
              len(hits) >= 2 and hits[0]["drug_count"] >= hits[-1]["drug_count"],
              str([x["drug_count"] for x in hits[:4]]))
        check("类风湿关节炎在图里", any(x["name"] == "类风湿关节炎" for x in hits))

        print("\n── 按病查推荐用药 ──")
        r = client.get("/query/disease-drugs",
                       params={"name": "类风湿关节炎"}, headers=h)
        d = r.json() if r.status_code == 200 else {}
        check("200", r.status_code == 200, f"→ {r.status_code}")
        check("total = 23（与消融里那个 23 一致）", d.get("total") == 23,
              str(d.get("total")))
        names = [x["name"] for x in d.get("drugs") or []]
        check("**推荐用药里没有垃圾药名**（junk 已被接口过滤）",
              not (set(names) & JUNK), str(sorted(set(names) & JUNK)))
        check("带 disclaimer（说明是 OpenCMKG 而非临床指南）",
              "不是临床指南" in (d.get("disclaimer") or ""))
        check("带 source 字段", all("source" in x for x in (d.get("drugs") or [])[:3]))

        print("\n── 两两相互作用：有记录 ──")
        r = client.get("/query/interaction",
                       params={"drug_a": "卡维地洛片", "drug_b": "依度沙班"},
                       headers=h)
        i = r.json() if r.status_code == 200 else {}
        check("200 且 found", r.status_code == 200 and i.get("found") is True)
        check("等级 = Major / 标签 高风险",
              i.get("level") == "Major" and i.get("level_label") == "高风险",
              f"{i.get('level')} / {i.get('level_label')}")
        check("带来源（DDInter）", "DDInter" in (i.get("attribution") or ""))
        check("**说明批量文件没有机制/处理建议/替代用药**",
              "机制" in (i.get("coverage_note") or ""))

        print("\n── 两两相互作用：未收录 ──")
        r = client.get("/query/interaction",
                       params={"drug_a": "卡维地洛片", "drug_b": "骨肽片"},
                       headers=h)
        i2 = r.json() if r.status_code == 200 else {}
        check("200 且 found=False", r.status_code == 200 and i2.get("found") is False)
        check("**标签是「未收录」而不是'轻微/安全'**",
              i2.get("level_label") == "未收录", str(i2.get("level_label")))
        check("**必须带 risk_hint（未收录 ≠ 可以同服）**",
              "≠" in (i2.get("risk_hint") or ""),
              (i2.get("risk_hint") or "")[:40])
        check("带覆盖说明（只收录 15 万对）",
              "150,522" in (i2.get("coverage_note") or ""))

        print("\n── 不存在的东西要 404，不能返回空列表 ──")
        r = client.get("/query/disease-drugs", params={"name": "不存在的病"},
                       headers=h)
        check("不存在的疾病 → 404", r.status_code == 404, f"→ {r.status_code}")
        r = client.get("/query/interaction",
                       params={"drug_a": "不存在药", "drug_b": "依度沙班"},
                       headers=h)
        check("不存在的药物 → 404", r.status_code == 404, f"→ {r.status_code}")
        r = client.get("/query/diseases", params={"q": ""}, headers=h)
        check("空关键词 → 422", r.status_code == 422, f"→ {r.status_code}")

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
    print("✓ 医药查询接口自测全过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
