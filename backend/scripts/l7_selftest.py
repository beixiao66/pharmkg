#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L7 会话管理最小版 · 端到端自测
==============================

**为什么要走 HTTP 而不是直接调函数**：L7 的交付物是*接口*。
直接调 `crud` 函数测不到路由注册、依赖注入、状态码、响应模型这几层——
而"401 还是 403""缺令牌该不该 401"这类问题恰恰只在这些层上出错。

**为什么还要直接查库**：`message.rewritten_query / cypher / evidence_json`
是 L9 评测的数据来源。接口把它们回显出来**不等于**落库了
（响应模型可以带一个没写进数据库的字段）。所以最后一步绕开 API、直接读库。

覆盖的 16 项
------------
============  ==========================================================
接口可用      `/health` 可达；`/docs` 的 openapi 能生成
注册          新建 201；重名 409；密码过短 422
登录          密码错 401；正确 200；**用户不存在与密码错返回同一句话**
令牌           `/auth/me` 无令牌 401；有令牌 200
              **伪造签名 401；过期令牌 401**
会话          无令牌 401；新建 201；标题由首条用户消息前 20 字自动生成
消息          追加 user/assistant 两条；`rewritten_query`/`cypher`/`evidence`
              **经接口回读完全一致**；**直接查库也完全一致**
顺序          按时间正序返回；`limit=1` 取到的是**最后**一条
越权          B 读 A 的会话 → **404**（不是 403：403 等于承认这个 id 存在）
清理          跑完把测试用户删掉（级联），库里不留痕
============  ==========================================================

用法
----
    cd backend
    .venv\\Scripts\\python.exe scripts\\l7_selftest.py
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import jwt                                    # noqa: E402
from fastapi.testclient import TestClient     # noqa: E402
from sqlalchemy import delete, select         # noqa: E402

from app.config import get_settings           # noqa: E402
from app.db import SessionLocal               # noqa: E402
from app.main import app                      # noqa: E402
from app.models import AppUser, Conversation, Message  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASSED if ok else FAILED).append(name)
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f"   {detail}" if detail else ""), flush=True)
    return ok


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    settings = get_settings()
    if not settings.jwt_configured:
        print("✗ JWT_SECRET 未设置——先按 .env.example 补上再跑。")
        return 2

    stamp = str(int(time.time()))
    user_a = f"selftest_a_{stamp}"
    user_b = f"selftest_b_{stamp}"
    pwd = "selftest-pw-123456"

    print("L7 会话管理最小版 · 端到端自测")
    print(f"  PostgreSQL  {settings.postgres_user}@{settings.postgres_host}:"
          f"{settings.postgres_port}/{settings.postgres_db}")
    print(f"  测试用户    {user_a} / {user_b}（跑完会删掉）")
    print()

    client = TestClient(app)
    token_a = token_b = ""
    conv_id = 0

    try:
        # ── 接口可用 ────────────────────────────────────────────────
        print("── 接口可用 ──")
        r = client.get("/health")
        check("/health 返回 200", r.status_code == 200,
              f"status={r.json().get('status')}")
        r = client.get("/openapi.json")
        check("openapi 能生成（路由都注册上了）", r.status_code == 200,
              f"{len(r.json().get('paths', {}))} 条路径")

        # ── 注册 ────────────────────────────────────────────────────
        print("\n── 注册 ──")
        r = client.post("/auth/register", json={"username": user_a, "password": pwd})
        ok = check("A 注册 201", r.status_code == 201, f"→ {r.status_code}")
        if ok:
            token_a = r.json()["access_token"]
        r = client.post("/auth/register", json={"username": user_a, "password": pwd})
        check("重名注册 409", r.status_code == 409, f"→ {r.status_code}")
        r = client.post("/auth/register", json={"username": "x", "password": "1"})
        check("用户名/密码过短 422", r.status_code == 422, f"→ {r.status_code}")
        r = client.post("/auth/register", json={"username": user_b, "password": pwd})
        if r.status_code == 201:
            token_b = r.json()["access_token"]
        check("B 注册 201", r.status_code == 201, f"→ {r.status_code}")

        # ── 登录 ────────────────────────────────────────────────────
        print("\n── 登录 ──")
        r = client.post("/auth/login", json={"username": user_a, "password": pwd})
        check("正确密码登录 200", r.status_code == 200, f"→ {r.status_code}")
        r_bad = client.post("/auth/login", json={"username": user_a, "password": "wrong"})
        r_ghost = client.post("/auth/login",
                              json={"username": "no_such_user_zzz", "password": "wrong"})
        check("密码错 401", r_bad.status_code == 401, f"→ {r_bad.status_code}")
        check("用户不存在与密码错**返回同一句话**（不泄露用户是否存在）",
              r_bad.json().get("detail") == r_ghost.json().get("detail"),
              repr(r_bad.json().get("detail")))

        # ── 令牌 ────────────────────────────────────────────────────
        print("\n── 令牌 ──")
        r = client.get("/auth/me")
        check("无令牌 /auth/me → 401（不是 403）", r.status_code == 401,
              f"→ {r.status_code}")
        r = client.get("/auth/me", headers={"Authorization": f"Bearer {token_a}"})
        check("有令牌 /auth/me → 200", r.status_code == 200,
              r.json().get("username", ""))
        # 假密钥给够 32 字节，否则 PyJWT 会额外抛 InsecureKeyLengthWarning——
        # 那是"密钥太短"的提示，与"签名不对"是两码事，混在一起会干扰读日志。
        forged = jwt.encode({"sub": "1", "exp": int(time.time()) + 600},
                            "not-the-real-secret-but-long-enough-32B",
                            algorithm="HS256")
        r = client.get("/auth/me", headers={"Authorization": f"Bearer {forged}"})
        check("伪造签名的令牌 401", r.status_code == 401, f"→ {r.status_code}")
        expired = jwt.encode(
            {"sub": "1", "exp": int((datetime.now(timezone.utc)
                                     - timedelta(minutes=1)).timestamp())},
            settings.jwt_secret, algorithm=settings.jwt_algorithm)
        r = client.get("/auth/me", headers={"Authorization": f"Bearer {expired}"})
        check("过期令牌 401", r.status_code == 401, f"→ {r.status_code}")

        # ── 会话 ────────────────────────────────────────────────────
        print("\n── 会话 ──")
        r = client.post("/conversations", json={})
        check("无令牌建会话 401", r.status_code == 401, f"→ {r.status_code}")
        h_a = {"Authorization": f"Bearer {token_a}"}
        r = client.post("/conversations", json={}, headers=h_a)
        ok = check("建会话 201", r.status_code == 201, f"→ {r.status_code}")
        if ok:
            conv_id = r.json()["id"]
            check("新会话标题为空（等首条消息补）", r.json()["title"] == "",
                  repr(r.json()["title"]))
            check("新会话 message_count=0", r.json()["message_count"] == 0)

        # ── 消息：L5 落库的三样必须原样往返 ─────────────────────────
        print("\n── 消息（L5 落库）──")
        q = "那他克莫司呢？"
        rewritten = "他克莫司 有哪些禁忌？"
        cypher = ("MATCH (d:Drug {name:'他克莫司'})-[r:CONTRAINDICATED_FOR]->(t) "
                  "RETURN t.name AS node, r.confidence AS conf")
        evidence = {"nodes": [{"name": "孕妇", "label": "Population", "conf": "high"}],
                    "edges": [{"from": "他克莫司", "to": "孕妇",
                               "rel": "CONTRAINDICATED_FOR"}]}
        r = client.post(f"/conversations/{conv_id}/messages",
                        json={"role": "user", "content": q}, headers=h_a)
        check("追加 user 消息 201", r.status_code == 201, f"→ {r.status_code}")
        if conv_id:
            r2 = client.get(f"/conversations/{conv_id}/messages", headers=h_a)
            title = next((c["title"] for c in
                          client.get("/conversations", headers=h_a).json()
                          if c["id"] == conv_id), None)
            check("标题已由首条消息前 20 字自动生成",
                  title == q[:20], repr(title))

        r = client.post(f"/conversations/{conv_id}/messages",
                        json={"role": "assistant", "content": "他克莫司禁用于孕妇。",
                              "rewritten_query": rewritten, "cypher": cypher,
                              "evidence": evidence}, headers=h_a)
        check("追加 assistant 消息（带改写/Cypher/证据）201",
              r.status_code == 201, f"→ {r.status_code}")
        got = r.json() if r.status_code == 201 else {}
        check("接口回读：rewritten_query 原样", got.get("rewritten_query") == rewritten)
        check("接口回读：cypher 原样", got.get("cypher") == cypher)
        check("接口回读：evidence 原样（JSONB 往返）",
              got.get("evidence") == evidence)

        # 绕开 API，直接查库——证明**真的落库**了，而不只是被响应模型回显
        with SessionLocal() as db:
            row = db.execute(
                select(Message).where(Message.conversation_id == conv_id,
                                      Message.role == "assistant")
                .order_by(Message.id.desc())).scalars().first()
            check("直接查库：rewritten_query 真的存进去了",
                  row is not None and row.rewritten_query == rewritten)
            check("直接查库：cypher 真的存进去了",
                  row is not None and row.cypher == cypher)
            check("直接查库：evidence 真的存进去了（且是 dict 不是字符串）",
                  row is not None and row.evidence == evidence)

        # ── 顺序与窗口 ──────────────────────────────────────────────
        print("\n── 顺序与上下文窗口 ──")
        r = client.get(f"/conversations/{conv_id}/messages", headers=h_a)
        roles = [m["role"] for m in r.json()]
        check("按时间正序返回 user → assistant", roles == ["user", "assistant"],
              str(roles))
        r = client.get(f"/conversations/{conv_id}/messages?limit=1", headers=h_a)
        check("limit=1 取到的是**最后**一条（不是第一条）",
              len(r.json()) == 1 and r.json()[0]["role"] == "assistant",
              str([m["role"] for m in r.json()]))

        # ── 越权 ────────────────────────────────────────────────────
        print("\n── 越权 ──")
        h_b = {"Authorization": f"Bearer {token_b}"}
        r = client.get(f"/conversations/{conv_id}/messages", headers=h_b)
        check("B 读 A 的会话 → 404（不是 403）", r.status_code == 404,
              f"→ {r.status_code}")
        r = client.post(f"/conversations/{conv_id}/messages",
                        json={"role": "user", "content": "越权写入"}, headers=h_b)
        check("B 往 A 的会话写 → 404", r.status_code == 404, f"→ {r.status_code}")
        r = client.get("/conversations", headers=h_b)
        check("B 的会话列表里看不到 A 的会话",
              all(c["id"] != conv_id for c in r.json()), f"{len(r.json())} 条")

    finally:
        # ── 清理 ────────────────────────────────────────────────────
        print("\n── 清理 ──")
        with SessionLocal() as db:
            before = db.execute(select(Message)).scalars().all()
            db.execute(delete(AppUser).where(
                AppUser.username.in_([user_a, user_b])))
            db.commit()
            left = db.execute(select(AppUser).where(
                AppUser.username.in_([user_a, user_b]))).scalars().all()
            check("测试用户已删除", len(left) == 0)
            orphan = db.execute(
                select(Conversation).where(Conversation.id == conv_id)
            ).scalars().all() if conv_id else []
            check("会话被级联删除（没有留下孤儿行）", len(orphan) == 0,
                  f"清理前 Message 共 {len(before)} 行")

    print()
    print(f"── 汇总 ──  通过 {len(PASSED)}  失败 {len(FAILED)}")
    if FAILED:
        for name in FAILED:
            print(f"    ✗ {name}")
        return 1
    print("✓ L7 会话管理最小版自测全过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
