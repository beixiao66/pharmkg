#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L8 · 图谱质量接口自测（`GET /quality/graph`）
=============================================

看板页的数据源。重点不是"能不能返回 JSON"，而是：

* **鉴权**：没 token 必须 401
* **五个靶点齐全**（自环 / 剂型分裂 / 拼接垃圾 / 人名噪声 / OOV）——
  少一个看板就会缺一块而不报错
* **带 `_meta`**：快照时间与"这是快照不是实时统计"的提醒必须一起给，
  否则看板会用旧数字描述新图
* **数字与 docs/图谱质量报告.md 对得上**（抽查三个关键数：自环 112、
  剂型分裂 509 主成分 / 1,243 药、OOV 60）——两份材料数字不一致时，
  两份都不可信

用法
----
    cd backend
    .venv\\Scripts\\python.exe scripts\\quality_selftest.py
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
    user = f"l8q_{stamp}"
    pwd = "l8-quality-selftest-pw-123456"
    client = TestClient(app)

    print("L8 · 图谱质量接口自测")
    print(f"  测试用户  {user}（跑完会删掉）")
    print()

    try:
        print("── 鉴权 ──")
        r = client.get("/quality/graph")
        check("没 token → 401", r.status_code == 401, f"→ {r.status_code}")

        r = client.post("/auth/register", json={"username": user, "password": pwd})
        r.raise_for_status()
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}

        print("\n── 内容 ──")
        r = client.get("/quality/graph", headers=h)
        check("200", r.status_code == 200, f"→ {r.status_code}")
        d = r.json() if r.status_code == 200 else {}

        for key, name in (("scale", "规模"), ("self_loop", "自环"),
                          ("dosage_split", "剂型分裂"), ("junk_concat", "拼接垃圾"),
                          ("person_noise", "人名噪声"), ("oov", "OOV 实体")):
            check(f"五个靶点齐全：{name}", key in d)

        meta = d.get("_meta") or {}
        check("**带快照时间**（看板要显示，否则不知道新不新）",
              bool(meta.get("generated_at")), str(meta.get("generated_at")))
        check("**带『这是快照』的提醒**",
              "快照" in (meta.get("note") or ""), (meta.get("note") or "")[:40])

        print("\n── 数字与 docs/图谱质量报告.md 是否一致（抽查三个）──")
        loops = (d.get("self_loop") or {}).get("count")
        check("自环 = 112", loops == 112, str(loops))
        ds = d.get("dosage_split") or {}
        check("剂型分裂 = 509 主成分 / 1,243 药",
              ds.get("split_bases") == 509 and ds.get("split_drugs") == 1243,
              f"{ds.get('split_bases')} / {ds.get('split_drugs')}")
        oov = (d.get("oov") or {}).get("oov_count")
        check("OOV = 60", oov == 60, str(oov))

        check("规模数字在（看板顶部要用）",
              (d.get("scale") or {}).get("nodes_total", 0) > 0,
              str((d.get("scale") or {}).get("nodes_total")))
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
    print("✓ 图谱质量接口自测全过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
