#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键跑完全部自测
================

## 为什么需要这一个脚本

到目前为此自测分散在 7 个文件里（L4 数字断言 + L6/L7/问答/质量/查询/浏览），
逐个敲命令既容易漏、也不方便在答辩现场"一条命令证明系统是好的"。
**分散的自测等于没有自测**——你不会每次都记得跑全。

## 两个刻意的实现选择

1. **子进程输出写到文件，不用管道。** 本环境对管道式 stdio 有限制
   （Node 的 `spawn` 直接 EPERM；Python 的匿名管道虽能用，但没必要冒险）。
   写文件还顺带留下**可复查的日志**——答辩被问"你测过吗"时可以指给它看。
2. **只看退出码，不解析屏幕文字。** 解析"通过 N 失败 M"这种输出格式很脆：
   改一个字就对不上，然后"解析失败"会被误当成"测试失败"或"测试通过"。
   **退出码是契约，文字不是。**

用法
----
    cd backend
    .venv\\Scripts\\python.exe scripts\\run_all_selftests.py
    .venv\\Scripts\\python.exe scripts\\run_all_selftests.py --only ask query
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
LOG_DIR = REPO_ROOT / "_tmp" / "selftests"

#: (显示名, 脚本路径, 工作目录, 关键词)
#: 关键词只用于 --only 过滤，不参与判定。
TESTS: list[tuple[str, str, Path, str]] = [
    ("L4 数字断言与零漂移", "scripts/verify_l4.py", REPO_ROOT, "l4"),
    ("L6 用药安全校验", "scripts/l6_selftest.py", BACKEND_DIR, "l6"),
    ("L7 会话管理", "scripts/l7_selftest.py", BACKEND_DIR, "l7"),
    ("问答接口 /ask", "scripts/l9_ask_selftest.py", BACKEND_DIR, "ask"),
    ("图谱质量接口", "scripts/quality_selftest.py", BACKEND_DIR, "quality"),
    ("医药查询接口", "scripts/query_selftest.py", BACKEND_DIR, "query"),
    ("图谱浏览接口", "scripts/graph_selftest.py", BACKEND_DIR, "graph"),
    # 前端翻译表 vs 图里的枚举：漏一个，界面上就会蹦英文。
    ("标签翻译表覆盖", "../scripts/check_label_map.py", BACKEND_DIR, "labels"),
]


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="一键跑完全部自测")
    ap.add_argument("--only", nargs="*", default=None,
                    help="只跑名字含这些关键词的（l4/l6/l7/ask/quality/query/graph）")
    args = ap.parse_args(argv)

    picked = [t for t in TESTS
              if not args.only or any(k in t[0].lower() or k == t[3]
                                      for k in args.only)]
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    print("pharmkg 全量自测")
    print(f"  共 {len(picked)} 套；日志写到 {LOG_DIR.relative_to(REPO_ROOT)}/")
    print()

    results: list[tuple[str, bool, float, Path]] = []
    t_all = time.time()
    for name, rel, cwd, key in picked:
        script = cwd / rel
        log = LOG_DIR / f"{key}.log"
        if not script.exists():
            print(f"  ✗ {name}  —— 脚本不存在：{script}")
            results.append((name, False, 0.0, log))
            continue
        t0 = time.time()
        # ⚠️ stdout 指向**文件**而不是 PIPE：避免管道式 stdio 的限制，
        #    同时留下可复查的证据。
        with log.open("w", encoding="utf-8", errors="replace") as f:
            proc = subprocess.run([sys.executable, str(script)],
                                  cwd=str(cwd), stdout=f,
                                  stderr=subprocess.STDOUT,
                                  env=None, timeout=1800)
        dt = time.time() - t0
        ok = proc.returncode == 0
        results.append((name, ok, dt, log))
        print(f"  {'✓' if ok else '✗'} {name:<22}{dt:5.1f}s"
              f"   exit={proc.returncode}"
              + ("" if ok else f"   见 {log.name}"))

    total = time.time() - t_all
    passed = sum(1 for _, ok, _, _ in results if ok)
    print()
    print("── 汇总 ──")
    print(f"  {passed}/{len(results)} 套通过，总耗时 {total:.1f}s")
    if passed != len(results):
        print("  未通过的：")
        for name, ok, _, log in results:
            if not ok:
                print(f"    ✗ {name}   日志：{log}")
        return 1
    print("✓ 全部自测通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
