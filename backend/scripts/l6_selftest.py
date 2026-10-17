#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L6 · 用药安全校验自测
=====================

覆盖 20 项，重点在**这个功能最容易骗人的地方**：

* **"无记录"必须显式出现**，不能被省略——用户会把"没提到"读成"没问题"
* **排序**：Major 必须在最前；`Unknown` **单独一档**（不是"最轻"）
* **人群标签只收图里存在的名字**（拼错不许静默通过：那会变成漏报）
* **数据覆盖必须报出来**（12 条人群边这件事用户有权知道）
* 加药只接受**图谱里已有的 Drug**（决策 #16），且重复加靠唯一约束判（409）
* 跨用户隔离

用法
----
    cd backend
    .venv\\Scripts\\python.exe scripts\\l6_selftest.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient          # noqa: E402
from sqlalchemy import delete, func, select        # noqa: E402

from app.config import get_settings                # noqa: E402
from app.db import SessionLocal                    # noqa: E402
from app.main import app                           # noqa: E402
from app.models import AppUser, MedicineBox        # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []

#: 演示用的三味药。**id 不写死**——用 /drugs/search 现取（顺带把检索也测了）。
DEMO = ["卡维地洛片", "依度沙班", "乌帕达西替尼"]


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
    user_a, user_b = f"l6_a_{stamp}", f"l6_b_{stamp}"
    pwd = "l6-selftest-pw-123456"
    client = TestClient(app)

    print("L6 · 用药安全校验自测")
    print(f"  PostgreSQL  {st.postgres_user}@{st.postgres_host}/{st.postgres_db}")
    print(f"  测试用户    {user_a} / {user_b}（跑完会删掉）")
    print()

    def reg(u: str) -> str:
        r = client.post("/auth/register", json={"username": u, "password": pwd})
        r.raise_for_status()
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    ha, hb = reg(user_a), reg(user_b)
    ids: dict[str, str] = {}

    try:
        print("── 人群/过敏标签枚举 + 覆盖度（决策 #42）──")
        r = client.get("/medicine-box/population-vocabulary")
        vocab = r.json() if r.status_code == 200 else []
        tags = {v["tag"] for v in vocab}
        cov = {v["tag"]: v for v in vocab}
        check("枚举接口 200", r.status_code == 200, f"{len(vocab)} 个标签")
        check("人群 8 + 过敏史 2 = 10（**不再等于图里的 5 个 Population**）",
              len(vocab) == 10, f"{len(vocab)} 个")
        check("**含** 儿童/老年/肝功能不全/肾功能不全（#42 要的就是让这些标签存在）",
              {"儿童", "老年", "肝功能不全", "肾功能不全"} <= tags)
        check("人群用产品名 妊娠期/哺乳期/育龄期（**不是图的 孕妇/哺乳期女性**）",
              {"妊娠期", "哺乳期", "育龄期"} <= tags
              and not ({"孕妇", "哺乳期女性", "育龄女性"} & tags))
        check("过敏史单独成组（group=allergy）",
              cov["三环类药物过敏"]["group"] == "allergy"
              and cov["大环内酯类药物过敏"]["group"] == "allergy")
        check("**妊娠期覆盖度 = 图里 孕妇 的 high 边数（4）**",
              cov["妊娠期"]["edges"] == 4 and cov["妊娠期"]["mapped"] is True,
              f"edges={cov['妊娠期']['edges']} mapped={cov['妊娠期']['mapped']}")
        check("**儿童覆盖度 = 0 且 mapped=False（0 就说 0，不许假装能查）**",
              cov["儿童"]["edges"] == 0 and cov["儿童"]["mapped"] is False,
              f"edges={cov['儿童']['edges']} mapped={cov['儿童']['mapped']}")

        print("\n── 药物检索（决策 #16：只从已有节点里选）──")
        for name in DEMO:
            r = client.get("/drugs/search", params={"q": name})
            # ⚠️ 响应是 {items,total,limit,truncated}，**不是裸数组**。
            #    改这个结构的原因是用户实测「搜『卡』找不到卡维地洛片」：
            #    以「卡」开头 29 个药，而它排第 17 位，原来 LIMIT 10 把它切掉、
            #    页面上毫无提示。**「被截断」必须成为数据。**
            body = r.json() if r.status_code == 200 else {}
            hits = body.get("items", [])
            exact = next((h for h in hits if h["name"] == name), None)
            check(f"检索 {name} 命中同名节点", exact is not None,
                  f"{len(hits)} 条" + (f"  首条={hits[0]['name']}" if hits else ""))
            if exact:
                ids[name] = exact["id"]
        check("命中 id 的格式是 drug:<名字>",
              all(v.startswith("drug:") for v in ids.values()),
              str(list(ids.values())[:1]))

        # ── 截断必须报出来（本次修复的核心，钉进自测防回退）──
        b = client.get("/drugs/search", params={"q": "卡"}).json()
        check("搜『卡』→ truncated=True（**不是静默截断**）",
              b["truncated"] is True and b["total"] > len(b["items"]),
              f"total={b['total']} shown={len(b['items'])} truncated={b['truncated']}")
        check("搜『卡』的结果里**能出现卡维地洛片**（须排在显示条数以内）",
              any(h["name"] == "卡维地洛片" for h in b["items"]),
              f"共显示 {len(b['items'])} 条")
        b2 = client.get("/drugs/search", params={"q": "卡维"}).json()
        check("搜『卡维』→ 只 1 条、不截断、就是卡维地洛片",
              b2["total"] == 1 and b2["truncated"] is False
              and b2["items"][0]["name"] == "卡维地洛片",
              str(b2["items"]))

        print("\n── 加药 ──")
        r = client.post("/medicine-box", json={"drug_id": "drug:不存在的药"}, headers=ha)
        check("加图谱里没有的药 → 404", r.status_code == 404, f"→ {r.status_code}")
        for name in DEMO:
            r = client.post("/medicine-box", json={"drug_id": ids[name]}, headers=ha)
            check(f"加 {name} → 201", r.status_code == 201, f"→ {r.status_code}")
        r = client.post("/medicine-box", json={"drug_id": ids[DEMO[0]]}, headers=ha)
        check("重复加同一个药 → 409（靠唯一约束判）", r.status_code == 409,
              f"→ {r.status_code}")
        r = client.get("/medicine-box", headers=ha)
        check("药箱列表 3 条", len(r.json()) == 3, f"{len(r.json())} 条")
        r = client.get("/medicine-box", headers=hb)
        check("B 的药箱是空的（跨用户隔离）", r.json() == [], str(r.json()))

        print("\n── 两两相互作用 ──")
        r = client.get("/medicine-box/risk", headers=ha)
        risk = r.json()
        pairs = risk["pairs"]
        check("3 个药 → 3 对，**每对都出卡**（含无记录的）", len(pairs) == 3,
              f"{len(pairs)} 对")
        check("第 1 对是 Major（排序：Major 最前）",
              pairs[0]["level"] == "Major",
              f"{pairs[0]['drug_a']}↔{pairs[0]['drug_b']} {pairs[0]['level']}")
        no_edge = [p for p in pairs if p["level"] is None]
        check("**无记录的药对也出卡**，且写明「未收录」",
              all(p["note"] and "未收录" in p["note"] for p in no_edge),
              f"{len(no_edge)} 对无记录")
        check("**无记录的卡带 risk_hint，明说「未收录 ≠ 可以同服」**",
              all("不代表可以同服" in (p.get("risk_hint") or "") or
                  "≠" in (p.get("risk_hint") or "") for p in no_edge),
              repr((no_edge[0].get("risk_hint") if no_edge else "")[:40]))
        check("来源标注是 DDInter", "DDInter" in risk["attribution"],
              risk["attribution"][:40])
        check("带免责声明", bool(risk["disclaimer"]))

        print("\n── 人群禁忌（决策 #42：标签名 vs 图节点名）──")
        r = client.patch("/auth/me/population-tags",
                         json={"tags": ["不存在的标签"]}, headers=ha)
        check("非法标签 → 422", r.status_code == 422, f"→ {r.status_code}")
        r = client.patch("/auth/me/population-tags",
                         json={"tags": ["孕妇"]}, headers=ha)
        check("**旧图节点名『孕妇』不再合法 → 422**（防老数据静默生效）",
              r.status_code == 422, f"→ {r.status_code}")
        r = client.patch("/auth/me/population-tags",
                         json={"tags": ["妊娠期"],
                               "allergy_history": ["大环内酯类药物过敏"]}, headers=ha)
        check("设置 妊娠期 + 过敏史 → 200", r.status_code == 200,
              f"→ {r.status_code}")
        check("两个字段分别存住了（**没有混在一起**）",
              r.json()["population_tags"] == ["妊娠期"]
              and r.json()["allergy_history"] == ["大环内酯类药物过敏"],
              f"{r.json()['population_tags']} / {r.json()['allergy_history']}")
        r = client.get("/medicine-box/risk", headers=ha)
        risk = r.json()
        pop = risk["population"]
        check("药箱里 乌帕达西替尼 命中（high）",
              any("乌帕达西替尼" in p["drug"] for p in pop),
              str([(p["drug"], p["tag"]) for p in pop]))
        check("**回填的是标签名 妊娠期，同时保留图节点名 孕妇**（映射没丢信息）",
              any(p["tag"] == "妊娠期" and p.get("graph_node") == "孕妇"
                  for p in pop),
              str([(p["tag"], p.get("graph_node")) for p in pop]))
        check("人群命中带说明书原句", all(p.get("evidence") for p in pop) if pop else False,
              str([p.get("evidence") for p in pop][:1]))
        cov = risk["coverage"]
        check("**数据覆盖报出来了**：人群边 12 条 / high 8 条",
              cov["population_edges_total"] == 12 and cov["population_edges_high"] == 8,
              f"total={cov['population_edges_total']} high={cov['population_edges_high']}")
        check("覆盖说明里写明「没查到不等于没有」",
              "没查到不等于没有" in cov["note"])
        check("相互作用总边数报出来（15 万级）",
              cov["interaction_edges_total"] > 100000,
              f"{cov['interaction_edges_total']:,}")

        print("\n── 移除 ──")
        r = client.delete(f"/medicine-box/{ids[DEMO[2]]}", headers=ha)
        check("移除 乌帕达西替尼 → 204", r.status_code == 204, f"→ {r.status_code}")
        r = client.delete(f"/medicine-box/{ids[DEMO[2]]}", headers=ha)
        check("再移除同一个 → 404", r.status_code == 404, f"→ {r.status_code}")
        r = client.get("/medicine-box/risk", headers=ha)
        check("移除后 2 个药 → 1 对", len(r.json()["pairs"]) == 1,
              f"{len(r.json()['pairs'])} 对")

    finally:
        print("\n── 清理 ──")
        with SessionLocal() as db:
            db.execute(delete(AppUser).where(
                AppUser.username.in_([user_a, user_b])))
            db.commit()
            left = db.execute(select(func.count(AppUser.id)).where(
                AppUser.username.in_([user_a, user_b]))).scalar()
            # ⚠️ 这里原来断言的是「整张 medicine_box 表为空」——**那是错的断言**，
            #    它依赖全局状态：用户自己在页面上加的药也在同一张表里，
            #    于是自测会在"表里恰好有别人的数据"时失败（实测撞到，全表 2 行）。
            #    要测的是**级联删除有没有留下孤儿行**，不是"表是空的"。
            orphans = db.execute(
                select(func.count(MedicineBox.id)).where(
                    ~MedicineBox.user_id.in_(select(AppUser.id)))).scalar()
            total = db.execute(select(func.count(MedicineBox.id))).scalar()
            check("测试用户已删除", left == 0)
            check("级联删除没留下孤儿行（别人药箱里的数据不算）", orphans == 0,
                  f"全表 {total} 行，其中孤儿 {orphans} 行")

    print()
    print(f"── 汇总 ──  通过 {len(PASSED)}  失败 {len(FAILED)}")
    if FAILED:
        for n in FAILED:
            print(f"    ✗ {n}")
        return 1
    print("✓ L6 用药安全校验自测全过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
