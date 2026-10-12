#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L3 第②步 · 离线重解析（不碰网络）
==================================

``data/collected/raw/*.html`` 是采集当时的原始页面，**已经存下来了**。
所以解析规则一旦修正，就可以**离线**重跑，不必重新采集——
重采会改变样本，还可能再次触发站点的人机验证。

本脚本修的三件事
----------------
1. **块级换行丢失。** 旧产物把 4 条禁忌焊成
   ``对本品活性成份或任何辅料过敏者禁用活动性结核病或活动性严重感染…``
   （详见 ``insert_io`` 顶部的成因说明）。
2. **小节白名单丢节。** 【临床试验】【毒理研究】【用药须知】【化学成份】
   4 个节名不在旧名单里，共 **56 个小节被静默丢弃**，而 ``index.csv``
   的 ``sections`` 列照旧列着它们——**元数据说有、产物里没有**。
   现在按 HTML 文档顺序全写，并在收尾时自检
   「``sections`` 列 == txt 里实际出现的 ``## `` 小节」。
3. **``index.csv`` 被覆盖。** 断点续采那轮 ``collect_inserts.main()``
   直接覆写了清单，而那轮只处理剩下的药，于是清单**丢了 8 个已采药**
   （只剩 32 行，且从 rank=3 开始）。现在清单从 ``raw/`` + 药名单**重建**。

用法
----
    python scripts/reparse_inserts.py --check   # 只报告，不写任何文件
    python scripts/reparse_inserts.py           # 重写 text/*.txt 与 index.csv

复现「修复前 → 修复后」的头条数字
----------------------------------
磁盘上的旧产物已经被修好了，`--check` 只能显示 "473 → 473"。
要复现 **417 → 473 节、644 → 13,533 行**，把修复前那一版（``ee64f86``）
的 text/ 用 worktree 取出来当基准：

    git worktree add _tmp/old-wt ee64f86
    python scripts/reparse_inserts.py --check --old-dir _tmp/old-wt/data/collected/text
    git worktree remove --force _tmp/old-wt

⚠ 别用 ``git archive | tar``：Windows 上的 bsdtar 会把 UTF-8 中文文件名
按本地代码页解开，解出来是乱码（``乌帕达西替尼.txt`` → ``涔屽笗杈捐タ鏇垮凹.txt``），
``--old-dir`` 一个文件都匹配不上，结果会显示成 "0 → 473"。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from insert_io import (
    INDEX_FILE,
    RAW_DIR,
    TEXT_DIR,
    load_targets,
    parse_detail,
    read_candidates,
    read_index,
    read_text_header,
    render_text,
    text_sections,
    text_stats,
    write_index,
)

BASE_FIELDS = ("rank", "cn_name", "search_keyword", "en_names", "Major")


def _log(msg: str = "") -> None:
    print(msg, flush=True)


# ── 主流程 ──────────────────────────────────────────────────────────────
def build(check: bool, old_dir: Path | None = None) -> int:
    targets = load_targets()
    _log("L3 第②步 · 离线重解析")
    _log(f"  目标药      {len(targets)}（high_risk_drugs.csv 里 selected=1）")
    _log(f"  原始页面    {RAW_DIR.relative_to(RAW_DIR.parents[2])}/"
         f"*.html")
    _log(f"  模式        {'只检查（不写文件）' if check else '重写产物'}")
    _log("")

    rows: list[dict] = []
    old_secs = old_lines = new_secs = new_lines = 0
    collected = 0
    unknown: dict[str, int] = {}
    trivial: list[tuple[int, str]] = []
    mismatch: list[str] = []

    # 重解析算不出候选数（那要看搜索结果页），所以从旧清单**原样带过来**，
    # 不能凭空抹成空——"算不出的字段"和"字段确实是 0"必须分得开。
    old_rows = {r.get("search_keyword", ""): r for r in read_index()}

    for t in targets:
        kw = t["search_keyword"]
        base = {k: t.get(k, "") for k in BASE_FIELDS}
        raw = RAW_DIR / f"{kw}.html"
        txt = TEXT_DIR / f"{kw}.txt"

        if not raw.exists():
            rows.append({**base, "status": "无条目"})
            _log(f"  [ 无条目 ] {kw}")
            continue

        old_text = ""
        if old_dir is not None:
            # 对照基准取自别的目录（复现"修复前"数字用）
            p = old_dir / f"{kw}.txt"
            if p.exists():
                old_text = p.read_text(encoding="utf-8")
        elif txt.exists():
            old_text = txt.read_text(encoding="utf-8")
        header = read_text_header(txt)
        os_, ol_ = text_stats(old_text)

        info = parse_detail(raw.read_text(encoding="utf-8"))
        new_text = render_text(info, url=header["url"],
                              collected_at=header["collected_at"])
        ns_, nl_ = text_stats(new_text)
        sec_names = list(info["sections"])

        old_secs += os_
        old_lines += ol_
        new_secs += ns_
        new_lines += nl_
        collected += 1

        for k in info["unknown_sections"]:
            unknown[k] = unknown.get(k, 0) + 1

        jinji = info["sections"].get("禁忌", "")
        trivial.append((len(re.sub(r"\s", "", jinji)), kw))

        # 自检：index 的 sections 列必须与 txt 里真实出现的小节**逐字一致**
        joined = "、".join(sec_names)
        if "、".join(text_sections(new_text)) != joined:
            mismatch.append(kw)

        trunc = set(info["truncated"])
        rows.append({
            **base,
            "status": "已采集",
            "title": info["title"],
            "url": header["url"],
            "collected_at": header["collected_at"],
            "approval_date": info["核准日期"] or "",
            "revision_date": info["修改日期"] or "",
            "holder": (info["上市许可持有人"] or "").replace("\n", " "),
            "manufacturer": (info["生产企业"] or "").replace("\n", " "),
            "section_count": len(sec_names),
            "sections": joined,
            "complete_sections": "、".join(k for k in sec_names if k not in trunc),
            "truncated_sections": "、".join(sorted(trunc)),
            "unknown_sections": "、".join(info["unknown_sections"]),
            "candidate_count": old_rows.get(kw, {}).get("candidate_count", ""),
        })

        _log(f"  [ 已采集 ] {kw:<12} 小节 {os_:>2} → {ns_:>2}   "
             f"正文行 {ol_:>4} → {nl_:>5}   {info['title'][:30]}")

        if not check:
            txt.write_text(new_text, encoding="utf-8")

    # 候选数平时由采集那一步写进清单；这里只在 candidates.csv 确有该药记录时
    # 重算一遍，免得把显式的 0 覆盖成空。
    cand_by_kw: dict[str, int] = {}
    for c in read_candidates():
        cand_by_kw[c["search_keyword"]] = cand_by_kw.get(
            c["search_keyword"], 0) + 1
    for r in rows:
        kw = r["search_keyword"]
        if kw in cand_by_kw:
            r["candidate_count"] = str(cand_by_kw[kw])

    _log("")
    _log("── 前后对照 ──")
    _log(f"  已采药          {collected}/{len(targets)}")
    _log(f"  小节总数        {old_secs:,} → {new_secs:,}")
    _log(f"  正文行总数      {old_lines:,} → {new_lines:,}"
         f"   （{new_lines / max(1, old_lines):.1f} 倍）")
    _log(f"  每份平均        {old_lines / max(1, collected):.1f} 行 → "
         f"{new_lines / max(1, collected):.1f} 行")
    _log("")

    if unknown:
        _log("  ⚠ 出现了 KNOWN_SECTIONS 里没有的节名（已照样写入，未丢弃）：")
        for k, v in sorted(unknown.items(), key=lambda kv: -kv[1]):
            _log(f"      {v:>3} 份  {k}")
    else:
        _log("  ✓ 所有节名都在 KNOWN_SECTIONS 里")

    if mismatch:
        _log(f"  ✗ sections 列与产物不一致：{'、'.join(mismatch)}")
    else:
        _log(f"  ✓ {collected} 份的 sections 列与 txt 实际小节逐字一致")

    trivial.sort()
    _log("")
    _log("── 【禁忌】信息量最少的 8 个药（L4 要标注 generic_only 的依据）──")
    for n, kw in trivial[:8]:
        _log(f"      {n:>5} 字  {kw}")

    _log("")
    if check:
        _log("  （--check：未写入任何文件）")
    else:
        write_index(rows)
        _log(f"  已写 {INDEX_FILE.relative_to(INDEX_FILE.parents[2])}"
             f"（{len(rows)} 行）")
        _log(f"  已写 {TEXT_DIR.relative_to(TEXT_DIR.parents[2])}/*.txt"
             f"（{collected} 份）")
    return 0


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="L3 第②步：从 raw/*.html 离线重解析")
    ap.add_argument("--check", action="store_true",
                    help="只报告前后差异，不写任何文件")
    ap.add_argument("--old-dir", type=Path, default=None, metavar="DIR",
                    help="从别处的旧 text/ 目录取对照基准"
                         "（复现「修复前 → 修复后」数字用，见模块文档）")
    args = ap.parse_args(argv)
    return build(check=args.check, old_dir=args.old_dir)


if __name__ == "__main__":
    raise SystemExit(main())
