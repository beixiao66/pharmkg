#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L9/A · 问答接口自测（`POST /ask`）
=================================

这个接口是"把 `l5_pipeline.run_turn` 接进后端"的那一层，最容易出错的不是查询逻辑，
而是**接缝**。所以这里重点测接缝，而不是重测 L5（L5 有它自己的评测）。

覆盖：

* **鉴权**：没 token 必须 401（不能因为"能跑通"就把接口裸着）
* **单轮**：意图 / 实体 / Cypher / 行数 / 答案 都对
* **可解释性契约**：`cypher` 必须返回（提案里承诺"能展开看实际执行的 Cypher"）
* **不自评**：返回里**不能有** `result_ok` 这类金标准字段（线上没有金标准）
* **多轮改写**：带指代的追问要补成药名——⚠️ 这条曾经坏过：
  `rewrite_prompt` 要 `('user', 内容)` 这种**带角色**的记录，第一版传的是
  `(问题, 回答)` 二元组，于是用户那句被渲染成"助手说的"，指代补不出来。
* **无实体**：问图谱管不着的事，要明确拒答，不能编
* **参数校验**：空问题、超长问题 → 422

用法
----
    cd backend
    .venv\\Scripts\\python.exe scripts\\l9_ask_selftest.py
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
    if not st.llm_configured:
        print("✗ LLM 未配置（问答离不开它）")
        return 2

    stamp = str(int(time.time()))
    user = f"l9a_{stamp}"
    pwd = "l9-ask-selftest-pw-123456"
    client = TestClient(app)

    print("L9/A · 问答接口自测")
    print(f"  模型        {st.llm_model}")
    print(f"  测试用户    {user}（跑完会删掉）")
    print()

    try:
        # ── 鉴权 ──
        print("── 鉴权 ──")
        r = client.post("/ask", json={"question": "卡维地洛和依度沙班能一起吃吗？"})
        check("没 token → 401", r.status_code == 401, f"→ {r.status_code}")

        r = client.post("/auth/register", json={"username": user, "password": pwd})
        r.raise_for_status()
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}

        # ── 单轮 ──
        print("\n── 单轮：相互作用 ──")
        t0 = time.time()
        r = client.post("/ask", headers=h,
                        json={"question": "卡维地洛和依度沙班能一起吃吗？"})
        dt = time.time() - t0
        check("单轮 200", r.status_code == 200, f"→ {r.status_code}  {dt:.1f}s")
        a = r.json() if r.status_code == 200 else {}
        check("意图 = 相互作用", a.get("intent") == "相互作用", str(a.get("intent")))
        check("实体两个都认出", set(a.get("entities") or []) >= {"卡维地洛片", "依度沙班"},
              str(a.get("entities")))
        check("step = ok", a.get("step") == "ok", str(a.get("step")))
        check("**返回了实际执行的 Cypher**（可解释性契约）",
              bool(a.get("cypher")) and "MATCH" in (a.get("cypher") or ""),
              (a.get("cypher") or "")[:60])
        check("有结果行", (a.get("row_count") or 0) >= 1, str(a.get("row_count")))
        check("答案里出现 Major", "Major" in (a.get("answer") or ""),
              (a.get("answer") or "")[:60])

        # ── 不自评 ──
        print("\n── 线上接口不该自评 ──")
        check("**返回里没有 result_ok**（金标准字段不该外泄）",
              "result_ok" not in a)
        check("返回里有 faithful（忠实度是机械检查，可以给）",
              "faithful" in a, str(a.get("faithful")))

        # ── 多轮改写 ──
        print("\n── 多轮：带指代的追问 ──")
        r = client.post("/ask", headers=h, json={
            "question": "它和依度沙班能一起吃吗？",
            "history": [{"question": "我在吃卡维地洛片，想问问用药的事。",
                         "answer": "好的，请说。"}],
        })
        a2 = r.json() if r.status_code == 200 else {}
        rw = a2.get("rewritten") or ""
        check("多轮 200", r.status_code == 200, f"→ {r.status_code}")
        check("**改写把「它」补成了药名**（曾经坏过：history 要角色化）",
              "卡维地洛片" in rw and "它" not in rw, rw[:60])
        check("实体两个都认出（改写生效的证据）",
              set(a2.get("entities") or []) >= {"卡维地洛片", "依度沙班"},
              str(a2.get("entities")))
        check("多轮答案是 Major 相互作用", "Major" in (a2.get("answer") or ""),
              (a2.get("answer") or "")[:60])

        # ── 无实体 / 拒答 ──
        print("\n── 图谱管不着的问题 ──")
        r = client.post("/ask", headers=h, json={"question": "今天天气怎么样？"})
        a3 = r.json() if r.status_code == 200 else {}
        check("无实体时明确拒答（不编）",
              a3.get("step") in ("无实体", "澄清"),
              f"step={a3.get('step')}  {(a3.get('answer') or '')[:40]}")
        check("拒答也带免责声明", "仅供参考" in (a3.get("answer") or ""))

        # ── 参数校验 ──
        print("\n── 参数校验 ──")
        r = client.post("/ask", headers=h, json={"question": ""})
        check("空问题 → 422", r.status_code == 422, f"→ {r.status_code}")
        r = client.post("/ask", headers=h, json={"question": "药" * 201})
        check("超长问题(201字) → 422", r.status_code == 422, f"→ {r.status_code}")

        # ── 索引重建 ──
        print("\n── 索引缓存 ──")
        r = client.post("/ask/reload-index", headers=h)
        check("可重建索引（图谱变更后要用）", r.status_code == 200,
              str(r.json() if r.status_code == 200 else r.status_code))

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
    print("✓ 问答接口自测全过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
