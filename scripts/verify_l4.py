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
    ("data/extracted/alignment_eval.json", "rule_only.hit", 41),
    ("data/extracted/alignment_eval.json", "rule_plus_llm.hit", 45),
    ("data/extracted/alignment_eval.json", "n_gold", 52),
    ("data/extracted/baseline/score_edges.json",
     "layer2_edges.rule_only.f1", 0.4921),
    ("data/extracted/baseline/score_edges.json",
     "layer2_edges.rule_plus_llm.f1", 0.624),
    ("data/extracted/section-json/score_edges.json",
     "layer2_edges.rule_plus_llm.f1", 0.624),
    ("data/extracted/section-json/score_edges.json",
     "layer2_edges.rule_plus_llm.flagged.f1", 0.729),
    ("data/extracted/section-json/score_edges.json",
     "layer2_edges.rule_plus_llm.oracle.f1", 0.8261),
    # generic_only 的正类**只有 `仅文本`**（= 通用过敏禁忌）。两个口径现在是同一个数——
    # 之前"原子级 0.745 / 条目级 0.941"的差距来自把 `删除` 那些行(残片/举例/理由/后果)
    # 也当成了正类，那是**换了概念**。
    ("data/extracted/section-json/score_edges.json",
     "generic_only_flag.vs_gold.item_level.f1", 0.9412),
    ("data/extracted/section-json/score_edges.json",
     "generic_only_flag.vs_gold.atom_level.f1", 0.9412),
    ("data/extracted/baseline/fidelity.json",
     "loose_punct_equiv.rate", 0.94),
    ("data/extracted/section-json/fidelity.json",
     "loose_punct_equiv.rate", 1.0),
    # 逐条查不到的**条数**也要钉住：只钉比率的话，"6 条"变成"7 条"
    # 但总数也变了的时候可能看不出来。
    ("data/extracted/baseline/fidelity.json", "len:not_found", 6),
    ("data/extracted/section-json/fidelity.json", "len:not_found", 0),
    # CAUTION_FOR 层（两节各一份 fidelity；注意【注意事项】有 7 条查不到原文）
    ("data/extracted/caution-json/fidelity_注意事项.json",
     "loose_punct_equiv.rate", 0.9893),
    ("data/extracted/caution-json/fidelity_注意事项.json",
     "len:not_found", 7),
    ("data/extracted/caution-json/fidelity_儿童用药.json",
     "loose_punct_equiv.rate", 1.0),
    ("data/extracted/caution-json/fidelity_儿童用药.json",
     "len:not_found", 0),
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
    # CAUTION_FOR 层：三个都**离线**。抽取那一步会全部跳过（72 个单元都已存在），
    # 正好也证明"断点续跑不写文件"。
    import extract_sections
    steps += [
        ("CAUTION_FOR 抽取（应全部跳过、不写文件）", extract_sections.main,
         ["--sections", "注意事项,儿童用药", "--out", "caution-json"], (0,)),
        # 【注意事项】本来就有 7 条查不到原文（那正是要报出来的现象）→ 期望退出码是 1
        ("忠实度 · 注意事项（全量，预期 7 条查不到）", check_fidelity.main,
         ["--method", "caution-json", "--section", "注意事项", "--sample", "0"], (1,)),
        ("忠实度 · 儿童用药（全量）", check_fidelity.main,
         ["--method", "caution-json", "--section", "儿童用药", "--sample", "0"], (0,)),
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
    # ⚠️ **这一步绝不允许"拿不到状态就当通过"。**
    #    踩过：先前用 `capture_output=True` 抓 git 的输出，而沙箱禁止外部进程的管道，
    #    stdout 变成 None → 异常被吞掉 → 却照样打印了"✓ 零变动"。
    #    **工具报了一个它根本没做的检查的绿灯**，这比不做检查更危险。
    #    所以：① 改成把 git 输出重定向到**临时文件**（绕开管道）；
    #          ② 真拿不到就**判失败**，不许静默通过。
    tmp = REPO_ROOT / "_tmp" / "_git_status.txt"
    tmp.parent.mkdir(parents=True, exist_ok=True)

    def git_lines(*argv: str) -> list[str] | None:
        """跑一条 git 命令、输出落到**文件**（不依赖管道）。拿不到返回 None。"""
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                subprocess.run(["git", *argv], cwd=REPO_ROOT, stdout=fh,
                               stderr=subprocess.DEVNULL, timeout=60, check=False)
            return [ln for ln in tmp.read_text(encoding="utf-8").splitlines()
                    if ln.strip()]
        except Exception:  # noqa: BLE001
            return None

    # 判据必须比**内容**，不能比 git 的 stat 状态。踩过两个方向：
    #   · 管道被拦 → stdout 为 None → 异常被吞 → 却打印"✓ 零变动"（**假绿**）
    #   · `git status` 把"工作区是 CRLF、仓库是 LF"的文件一直报成 M，
    #     而内容其实一致（**假红**）——那是我脚本自己写 CRLF 造成的，已改成写 LF
    #   · 就算两边都是 LF，沙箱里 git 写不了 `.git/index`，stat 缓存刷不动，
    #     `git status` 仍会报 M——此时**内容哈希与 HEAD 完全相同**
    # 所以最终的判据是**逐字节比对 HEAD**：一次 `git archive` 拿到 HEAD 里的
    # `data/extracted/`，再用 tarfile 逐个比。不依赖 index、不依赖工作区状态。
    import tarfile
    tar_path = REPO_ROOT / "_tmp" / "_head_extracted.tar"
    archive = git_lines  # 复用同名风格：下面单独调一次 archive
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            subprocess.run(["git", "archive", "--format=tar",
                            f"--output={tar_path}", "HEAD", "data/extracted"],
                           cwd=REPO_ROOT, stdout=fh,
                           stderr=subprocess.DEVNULL, timeout=120, check=False)
    except Exception:  # noqa: BLE001
        pass

    if not tar_path.exists():
        log("  ✗ **这一步没验证成功**：拿不到 HEAD 里的 data/extracted。")
        log("    不能当成通过——要么修环境，要么手工核对提交的产物。")
        failed += 1
    else:
        mismatched, missing = [], []
        with tarfile.open(tar_path) as tf:
            for m in tf.getmembers():
                if not m.isfile():
                    continue
                head = tf.extractfile(m).read()
                disk_path = REPO_ROOT / m.name
                if not disk_path.exists():
                    missing.append(m.name)
                elif disk_path.read_bytes() != head:
                    mismatched.append(m.name)
        untracked = git_lines("ls-files", "--others", "--exclude-standard",
                             "--", "data/extracted")
        n_head = sum(1 for _ in tarfile.open(tar_path).getmembers())
        if mismatched:
            failed += 1
            log(f"  ✗ 有 {len(mismatched)} 个产物与 HEAD **逐字节不同**（内容漂了）：")
            for f in mismatched[:10]:
                log(f"      {f}")
        if missing:
            failed += 1
            log(f"  ✗ 有 {len(missing)} 个 HEAD 里的产物在磁盘上不见了：")
            for f in missing[:10]:
                log(f"      {f}")
        if untracked is None:
            failed += 1
            log("  ✗ 拿不到未跟踪文件清单——这一步没验证。")
        elif untracked:
            failed += 1
            log(f"  ✗ 有 {len(untracked)} 个**未跟踪**的新产物（HEAD 里没有）：")
            for f in untracked[:10]:
                log(f"      {f}")
        if not mismatched and not missing and not untracked:
            log(f"  ✓ data/extracted 下 **{n_head} 个产物与 HEAD 逐字节一致、"
                f"无新增**（提交的产物可复现）")
        try:
            tar_path.unlink()
        except OSError:
            pass

    log("")
    if failed:
        log(f"✗ 自检不通过：{failed} 处。上面每一处都要么是数字真的变了"
            f"（那就同步改文档与 EXPECTED），要么是产物漂了。")
        return 1
    log(f"✓ 自检全过：{len(steps)} 个步骤 + {len(EXPECTED)} 条数字断言 + 产物零漂移")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
