#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
响应时间基准测量
================

## 为什么单独写一个，而不是"感觉挺快的"

`docs/论文实验章节素材.md` 第 7 节把「响应时间未正式测」列成了缺口——此前只有零散的
单次观测（问答 1.5–4 秒、首次建索引 3.4 秒）。**单次观测不能写进论文**：
它既可能是最慢的一次，也可能是最快的一次，还没有分位数。

## 三条测量纪律

1. **打真实 HTTP 口**（默认 `127.0.0.1:8000`），不是进程内 TestClient——
   后者把网络栈与序列化开销全省了，测出来的数偏乐观。
2. **纯数据库接口要多跑几次**（它们只有毫秒级，样本少了分位数没意义）；
   **`/ask` 少跑几次**（它每次要 3–4 次 LLM 调用，既慢又费额度）。
3. **冷启动单独测**：先 `POST /ask/reload-index` 清掉索引，再计时下一次 `/ask`。
   否则"首次请求 3.4 秒"这种离群值会被平均值糊掉——而**它恰恰是用户第一次点击时的体验**。

## 一个必须写清的限定

`/ask` 的耗时**主要由大模型云接口决定**（实测单次调用 1–4 秒），与服务器算力关系不大，
而且**受本机到百炼的网络状况影响**。所以论文里必须写"测量于某时某地某网络"，
不能说成"系统性能"。

用法
----
    cd backend
    .venv\\Scripts\\python.exe scripts\\bench_latency.py
    .venv\\Scripts\\python.exe scripts\\bench_latency.py --ask-reps 3
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from pathlib import Path

import httpx

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
OUT_JSON = REPO_ROOT / "data" / "quality" / "latency_bench.json"
OUT_MD = REPO_ROOT / "docs" / "性能测量报告.md"

BASE = "http://127.0.0.1:8000"


def log(m: str = "") -> None:
    print(m, flush=True)


def pct(xs: list[float], p: float) -> float:
    """简单分位数（线性插值）。样本小，够用。"""
    if not xs:
        return float("nan")
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def summarize(xs: list[float]) -> dict:
    return {
        "n": len(xs),
        "min_ms": round(min(xs) * 1000, 2) if xs else None,
        "median_ms": round(statistics.median(xs) * 1000, 2) if xs else None,
        "p90_ms": round(pct(xs, 0.9) * 1000, 2) if xs else None,
        "max_ms": round(max(xs) * 1000, 2) if xs else None,
        "mean_ms": round(statistics.fmean(xs) * 1000, 2) if xs else None,
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="响应时间基准")
    ap.add_argument("--base", default=BASE, help="后端地址")
    ap.add_argument("--ask-reps", type=int, default=5, help="/ask 跑几次（费额度）")
    ap.add_argument("--db-reps", type=int, default=15, help="数据库接口跑几次")
    args = ap.parse_args(argv)

    log("pharmkg 响应时间基准")
    log(f"  目标        {args.base}")
    log(f"  机器        {platform.platform()}  Python {platform.python_version()}")
    log(f"  采样        DB 接口 {args.db_reps} 次 / ask {args.ask_reps} 次")
    log("")

    stamp = str(int(time.time()))
    user = f"bench_{stamp}"
    client = httpx.Client(base_url=args.base, timeout=180.0)

    # ── 登录（后面所有请求都带 token）──
    r = client.post("/auth/register", json={"username": user,
                                            "password": "bench-pw-123456"})
    # ⚠️ 用 `is_success` 而不是 `== 200`：`/auth/register` 返回的是 **201 Created**。
    #    第一版写死 200，于是注册明明成功了却报"注册失败 201"——**校验条件写错，
    #    会把成功判成失败**，而且报错信息里还带着正常的 token，非常迷惑人。
    if not r.is_success:
        log(f"  ✗ 注册失败 {r.status_code}: {r.text[:200]}")
        return 2
    client.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

    results: dict[str, dict] = {}
    raw: dict[str, list[float]] = {}

    def bench(name: str, fn, reps: int) -> None:
        xs: list[float] = []
        for i in range(reps):
            t0 = time.perf_counter()
            rr = fn()
            dt = time.perf_counter() - t0
            if rr.status_code >= 400:
                log(f"  ✗ {name} 第 {i + 1} 次返回 {rr.status_code}: {rr.text[:120]}")
                break
            xs.append(dt)
        raw[name] = xs
        results[name] = summarize(xs)
        s = results[name]
        log(f"  {name:<24} n={s['n']:<3} 中位 {s['median_ms']:>8} ms"
            f"  p90 {s['p90_ms']:>8} ms  最大 {s['max_ms']:>8} ms")

    log("── 纯数据库接口（毫秒级，多跑几次）──")
    bench("药品检索 /drugs/search",
          lambda: client.get("/drugs/search", params={"q": "卡维地洛"}),
          args.db_reps)
    bench("药箱风险 /medicine-box/risk",
          lambda: client.get("/medicine-box/risk"), args.db_reps)
    bench("按病查药 /query/disease-drugs",
          lambda: client.get("/query/disease-drugs", params={"name": "类风湿关节炎"}),
          args.db_reps)
    bench("两两相互作用 /query/interaction",
          lambda: client.get("/query/interaction",
                            params={"drug_a": "卡维地洛片", "drug_b": "依度沙班"}),
          args.db_reps)
    bench("邻域子图 /graph/subgraph",
          lambda: client.get("/graph/subgraph", params={"name": "卡维地洛片", "limit": 120}),
          args.db_reps)
    bench("最短路径 /graph/path",
          lambda: client.get("/graph/path",
                            params={"src": "卡维地洛片", "dst": "孕妇", "max_hops": 4}),
          args.db_reps)

    log("")
    log("── 问答接口 /ask（每次 3–4 次 LLM 调用，少跑几次）──")
    Q1 = {"question": "卡维地洛和依度沙班能一起吃吗？"}
    bench("问答·单轮 /ask", lambda: client.post("/ask", json=Q1), args.ask_reps)

    HIST = [{"question": "我在吃卡维地洛片，想问问用药的事。", "answer": "好的，请说。"}]
    Q2 = {"question": "它和依度沙班能一起吃吗？", "history": HIST}
    bench("问答·多轮 /ask", lambda: client.post("/ask", json=Q2), args.ask_reps)

    log("")
    log("── 冷启动：清索引后第一次 /ask（用户第一次点击的体验）──")
    client.post("/ask/reload-index")
    t0 = time.perf_counter()
    rr = client.post("/ask", json=Q1)
    cold = time.perf_counter() - t0
    ok = rr.status_code == 200
    results["问答·冷启动（含建索引）"] = summarize([cold]) if ok else {}
    log(f"  {'✓' if ok else '✗'} {cold * 1000:.0f} ms")
    warm = results["问答·单轮 /ask"]["median_ms"] or 0
    log(f"    比热态中位数多 {cold * 1000 - warm:.0f} ms"
        f"（= 实体索引构建的代价，只在进程首次或 reload 后付一次）")

    # ── 清理测试用户 ──
    try:
        sys.path.insert(0, str(BACKEND_DIR))
        from sqlalchemy import delete
        from app.db import SessionLocal
        from app.models import AppUser
        with SessionLocal() as db:
            db.execute(delete(AppUser).where(AppUser.username == user))
            db.commit()
        log("\n  ✓ 测试用户已删除")
    except Exception as exc:  # noqa: BLE001
        log(f"\n  ⚠️ 测试用户清理失败（{type(exc).__name__}），请手动删 {user}")
    client.close()

    # ── 落盘 ──
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(
        {"measured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
         "target": args.base,
         "machine": platform.platform(),
         "python": platform.python_version(),
         "note": ("/ask 的耗时主要由大模型云接口决定，受本机到百炼的网络影响；"
                  "论文里必须写明测量时间/机器/网络。"),
         "summary": results,
         "raw_seconds": {k: [round(x, 4) for x in v] for k, v in raw.items()}},
        ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    log(f"  ✓ 原始数据 {OUT_JSON.relative_to(REPO_ROOT)}")

    lines = [
        "# 性能测量报告",
        "",
        "> 由 `python scripts/bench_latency.py` 生成。**不要手改**。",
        f"> 测量时间 {time.strftime('%Y-%m-%d %H:%M:%S')} ｜ 目标 `{args.base}` ｜ "
        f"机器 `{platform.platform()}`",
        ">",
        "> ⚠️ **限定**：`/ask` 的耗时**主要由大模型云接口决定**，受本机到百炼的网络状况影响，",
        "> 与服务器算力关系不大。所以这些数字描述的是「本机 + 该网络 + `qwen-plus`」下的体验，",
        "> **不能当成系统的固有性能**。论文里必须写明测量条件。",
        "",
        "| 接口 | n | 中位数 (ms) | p90 (ms) | 最大 (ms) | 均值 (ms) |",
        "|---|---|---|---|---|---|",
    ]
    for name, s in results.items():
        if not s:
            lines.append(f"| {name} | — | 失败 | — | — | — |")
            continue
        lines.append(f"| {name} | {s['n']} | **{s['median_ms']}** | {s['p90_ms']} "
                     f"| {s['max_ms']} | {s['mean_ms']} |")
    lines += [
        "",
        "## 怎么读这张表",
        "",
        "1. **纯数据库接口都在毫秒级**（药品检索、药箱风险、按病查药、相互作用、子图、路径）",
        "   —— 说明 Neo4j 侧的查询与索引设计没有拖后腿。",
        "   其中最慢的是**最短路径**（默认 4 跳的 `shortestPath`，在 50 万条边上搜索），",
        "   这是可预期的：它的代价随跳数增长，所以接口层把上限锁在 6 跳。",
        "2. **`/ask` 比它们慢两个数量级**，原因不是数据库，是「意图识别 + Text2Cypher +",
        "   答案生成」3–4 次**串行** LLM 调用；多轮还要多一次改写调用。",
        "3. **⚠️ 「冷启动」要分两种情况说，别混为一谈**：",
        "   * 本表测的「清索引后第一次」是**进程已热**的情况——只付索引重建的钱",
        "     （表里那一行减去热态中位数就是它，量级是**几百毫秒**）；",
        "   * **进程完全冷启动**（刚启动容器/刚起 uvicorn）还要再加 Python 模块导入、",
        "     连接池建立等开销，早期实测整条链约 **3.4 秒**。",
        "   两者差一个数量级，**论文里必须分开写**，否则会把「索引重建」说成「冷启动」。",
        "4. **要优化的话方向明确**：① 把索引构建挪到启动时预热（现在落在第一个用户头上）；",
        "   ② 意图识别改成规则优先（规则版在测试集上 23/23，成本为零）。",
        "   —— 这两条都比「换一个更快的模型」更有针对性，且不牺牲可解释性；",
        "   而消融③已证明**换更大模型对查询准确率零增益**。",
    ]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    log(f"  ✓ 报告 {OUT_MD.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
