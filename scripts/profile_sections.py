#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L4 · 小节内容画像：每节的内容到底能不能落进 §5 的槽位
======================================================

**为什么需要这个**：决策 #30 规定【注意事项】+【儿童用药】→ `CAUTION_FOR`，
而 §5 把 `CAUTION_FOR` 定义成 **`Drug → Population`**（属性 `level`）。
问题是这两节的**实际内容形态**是不是"某个人群需要慎用"。

不要靠印象回答这个问题——**数一下**。本脚本对每一节的每一行做关键词归类，报出
"含该类表述的行占比"。它只做**机械计数**，不做医学判断，所以谁都能复核。

归类（一行可同时命中多类，所以占比之和会 > 100%）
--------------------------------------------------
========================  ==========================================================
相互作用/合并用药          `CYP` / `合用` / `联合用药` / `相互作用` / `抑制剂` / `诱导剂` / `同服`
监测与检查                `监测` / `检查` / `测定` / `定期`
剂量与适应症              `剂量` / `减量` / `调整` / `适应症` / `给药`
**人群**                  `孕妇` / `妊娠` / `哺乳` / `儿童` / `老年` / `育龄` / `生育`
**疾病/状况**             `患者` / `功能不全` / `损害` / `障碍` / `病史` / `感染`
禁忌式表述                `禁用` / `禁止` / `不应使用` / `不得使用`
========================  ==========================================================

用法
----
    python scripts/profile_sections.py
"""

from __future__ import annotations

import sys

from insert_io import TEXT_DIR, split_sections

SECTIONS = ("禁忌", "注意事项", "儿童用药")

GROUPS: dict[str, tuple[str, ...]] = {
    "相互作用/合并用药": ("CYP", "合用", "联合用药", "相互作用", "抑制剂", "诱导剂", "同服"),
    "监测与检查": ("监测", "检查", "测定", "定期"),
    "剂量与适应症": ("剂量", "减量", "调整", "适应症", "给药"),
    "人群": ("孕妇", "妊娠", "哺乳", "儿童", "老年", "育龄", "生育"),
    "疾病/状况": ("患者", "功能不全", "损害", "障碍", "病史", "感染"),
    "禁忌式表述": ("禁用", "禁止", "不应使用", "不得使用"),
}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    docs = {p.stem: split_sections(p.read_text(encoding="utf-8"))
            for p in sorted(TEXT_DIR.glob("*.txt"))}

    log("L4 小节内容画像（机械计数，不做医学判断）")
    log(f"  {len(docs)} 份说明书")
    log("")
    log(f"  {'小节':<10}{'篇数':>5}{'字符':>9}{'行数':>7}")
    stats: dict[str, dict] = {}
    for sec in SECTIONS:
        bodies = [s.get(sec, "") for s in docs.values() if sec in s]
        chars = sum(len(b) for b in bodies)
        lines = [ln for b in bodies for ln in b.split("\n") if ln.strip()]
        stats[sec] = {"n_docs": len(bodies), "chars": chars,
                      "n_lines": len(lines), "lines": lines}
        log(f"  {sec:<10}{len(bodies):>5}{chars:>9,}{len(lines):>7}")
    log("")
    log("  含该类表述的**行占比**（一行可命中多类，占比之和 > 100%）：")
    header = f"  {'类别':<18}" + "".join(f"{s:>12}" for s in SECTIONS)
    log(header)
    for name, kws in GROUPS.items():
        cells = []
        for sec in SECTIONS:
            lines = stats[sec]["lines"]
            hit = sum(1 for ln in lines if any(k in ln for k in kws))
            cells.append(f"{hit / len(lines) * 100:>11.1f}%" if lines else "—")
        log(f"  {name:<18}" + "".join(cells))
    log("")
    log("  判读（§5 的槽位只有两处可落）：")
    log("    · 只有「人群」类能落进 `CAUTION_FOR`（Drug → Population）")
    log("    · 「相互作用/合并用药」类该进 `INTERACTS_WITH`")
    log("    · 「剂量与适应症」类由决策 #30 定死**只留文本、不入图**")
    log("    · 「监测与检查」「疾病/状况」两类**在 §5 里没有落点**")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
