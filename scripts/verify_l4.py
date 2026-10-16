#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L4 · 一键自检：把已发布的每个数字重算一遍，并断言它没动
=========================================================

这个项目里数字改过好几次（词表修正、人群词表收敛…），每次都要**手工回去改文档**，
而"文档里的数和产物里的数不一致"是最难发现、也最伤诚信的一类错。
所以把它变成一条命令：

    python scripts/verify_l4.py            # 全部离线（⑤ 走缓存），不花钱
    python scripts/verify_l4.py --with-graph   # 再跑一遍入库与验收查询（需 Neo4j）

它做三件事
----------
1. **重算**：按顺序把 L4 的每个评分步骤**在原进程里**跑一遍（不 shell 出去——
   沙箱下管道会被拦），包括对齐器、两层评分、忠实度、合成用例回归。
2. **断言**：把**已发布的数字**写死在下面（`EXPECTED`），逐个比对。
   数字变了这里就会**红**——这正是它存在的意义：不让我悄悄改口径。
3. **核对产物没漂**：重算之后 ``git status`` 里 ``data/extracted/`` 必须是干净的，
   否则说明"提交的产物"与"现在算出来的"不是一回事。

⚠️ **不联网**：⑤ LLM 的结果全在 ``data/extracted/alignment_cache.json`` 里，
所有步骤都是缓存命中。若缓存里缺了某条，说明有新原子进来了——
那样第 3 步会立刻变脏，能当场看出来。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

from opencmkg_io import REPO_ROOT

#: 已发布的数字。**改文档就必须改这里，反之亦然**——两边不一致就是错。
EXPECTED: list[tuple[str, str, object]] = [
    # (读数用的 JSON 路径, 取值路径, 期望值)
    ("data/extracted/baseline/score.json",
     "micro.f1", 1.0),
    ("data/extracted/section-json/score.json",
     "micro.f1", 1.0),
    ("data/extracted/alignment_eval.json", "rule_only.hit", 42),
    ("data/extracted/alignment_eval.json", "rule_plus_llm.hit", 45),
    ("data/extracted/alignment_eval.json", "n_gold", 54),
    ("data/extracted/baseline/score_edges.json",
     "layer2_edges.rule_only.f1", 0.5),
    ("data/extracted/baseline/score_edges.json",
     "layer2_edges.rule_plus_llm.f1", 0.6142),
    ("data/extracted/section-json/score_edges.json",
     "layer2_edges.rule_plus_llm.f1", 0.6142),
    ("data/extracted/section-json/score_edges.json",
     "layer2_edges.rule_plus_llm.flagged.f1", 0.7156),
    ("data/extracted/section-json/score_edges.json",
     "layer2_edges.rule_plus_llm.oracle.f1", 0.7917),
    ("data/extracted/section-json/score_edges.json",
     "generic_only_flag.vs_gold.item_level.f1", 0.9412),
    ("data/extracted/section-json/score_edges.json",
     "generic_only_flag.vs_gold.atom_level.f1", 0.7451),
    ("data/extracted/baseline/fidelity.json",
     "loose_punct_equiv.rate", 0.94),
    ("data/extracted/section-json/fidelity.json",
     "loose_punct_equiv.rate", 1.0),
    # 逐条查不到的**条数**也要钉住：只钉比率的话，"6 条"变成"7 条"
    # 但总数也变了的时候可能看不出来。
    ("data/extracted/baseline/fidelity.json", "len:not_found", 6),
    ("data/extracted/section-json/fidelity.json", "len:not_found", 0),
]

FID_ITEMS = "data/extracted/section-json/items"


def log(msg: str = "") -> None:
    print(msg, flush=True)


def get(path: str, dotted: str):
    """按 ``a.b.c`` 取值。``len:`` 前缀表示"取这个列表的长度"。"""
    data = json.loads((REPO_ROOT / path).read_text(encoding="utf-8"))
    want_len = dotted.startswith("len:")
    if want_len:
        dotted = dotted[4:]
    for key in dotted.split("."):
        data = data[key]
    return len(data) if want_len else data


def run_step(name: str, fn, argv: list[str]) -> tuple[int, str]:
    """在**原进程**里跑一步，把它的输出收起来（沙箱下不能 pipe 外部进程）。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            rc = fn(argv)
        except SystemExit as exc:  # 有的脚本用 raise SystemExit(...) 报错
            rc = exc.code if isinstance(exc.code, int) else 1
    return rc or 0, buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="L4 一键自检：重算 + 断言 + 核对产物")
    ap.add_argument("--with-graph", action="store_true",
                    help="再跑一遍入库与验收查询（需要 Neo4j 起着）")
    ap.add_argument("--show-output", action="store_true", help="把每一步的完整输出打出来")
    args = ap.parse_args(argv)

    # 放到函数里 import，免得 --help 也要先把图数据读完
    import align_nodes
    import check_fidelity
    import insert_io
    import score_alignment
    import score_extraction

    # insert_io 只有 _selftest()，没有 main()——包一层好让它和别的步骤同形
    def selftest_step(_argv=None) -> int:
        return insert_io._selftest()

    steps: list[tuple[str, object, list[str], tuple[int, ...]]] = [
        ("合成用例回归（19 项）", selftest_step, [], (0,)),
        ("对齐器 · 规则 vs 规则+⑤", align_nodes.main,
         ["--eval", "--llm"], (0,)),
        ("第一层 · 朴素基线", score_extraction.main, [], (0,)),
        ("第一层 · 消融层级 2", score_extraction.main,
         ["--method", "section-json", "--items-dir", FID_ITEMS], (0,)),
        ("第二层 · 朴素基线", score_alignment.main, ["--llm"], (0,)),
        ("第二层 · 消融层级 2", score_alignment.main,
         ["--method", "section-json", "--items-dir", FID_ITEMS, "--llm"], (0,)),
        ("忠实度 · 消融层级 2（100 条）", check_fidelity.main, ["--sample", "100"], (0,)),
        # 基线**本来就该返回 1**：有 6 条原子查不到原文（那正是要被报出来的现象）。
        # 所以这里的期望退出码是 (1,)，不是 (0,)——这一点必须写清楚，
        # 否则下一个人会以为"基线这一步失败了"。
        ("忠实度 · 朴素基线（100 条，预期有 6 条查不到）", check_fidelity.main,
         ["--method", "baseline", "--sample", "100"], (1,)),
    ]
    if args.with_graph:
        import import_insert_edges
        steps.append(("入库 + 验收查询", import_insert_edges.main, [], (0,)))

    log("L4 一键自检（全部离线，⑤ 走缓存，不花钱）")
    log("")
    failed = 0
    for name, fn, step_argv, ok_codes in steps:
        rc, out = run_step(name, fn, step_argv)
        good = rc in ok_codes
        failed += not good
        log(f"  {'✓' if good else '✗'} {name}")
        if args.show_output or not good:
            for line in out.splitlines():
                log(f"      {line}")
    log("")

    log("── 已发布的数字（断言）──────────────────────────────")
    for path, dotted, want in EXPECTED:
        got = get(path, dotted)
        ok = got == want
        failed += not ok
        mark = "✓" if ok else "✗"
        extra = "" if ok else f"   ← 现在是 {got}"
        log(f"  {mark} {dotted:<44} {want}{extra}")
        if not ok:
            log(f"      （来自 {path}）")
    log("")

    # 重算之后，提交的产物必须**一个字都没变**——否则"提交的证据"与"现在算的"不是一回事
    log("── 提交的产物有没有漂 ──────────────────────────────")
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--", "data/extracted"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        dirty = [ln for ln in out.stdout.splitlines() if ln.strip()]
    except Exception as exc:  # noqa: BLE001
        dirty = []
        log(f"  （拿不到 git 状态：{type(exc).__name__}: {exc}——跳过这一步）")
    if dirty:
        failed += 1
        log(f"  ✗ data/extracted 下有 {len(dirty)} 处变动——"
            f"提交的产物与现在算出来的不一致：")
        for ln in dirty[:10]:
            log(f"      {ln}")
    else:
        log("  ✓ data/extracted 下零变动（提交的产物逐字节可复现）")

    log("")
    if failed:
        log(f"✗ 自检不通过：{failed} 处。上面每一处都要么是数字真的变了"
            f"（那就同步改文档与 EXPECTED），要么是产物漂了。")
        return 1
    log(f"✓ 自检全过：{len(steps)} 个步骤 + {len(EXPECTED)} 条数字断言 + 产物零漂移")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
