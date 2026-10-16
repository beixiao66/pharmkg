#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L4 第②步 · 按小节送 + JSON 约束 + generic_only（消融层级 2）
=============================================================

对着**冻结的朴素基线**（``data/extracted/baseline/``）比，一次只动三个变量，
每个变量都是决策 #30 已经写下、而基线**故意没有**做的事：

============================  ==========================  ==================
变量                          基线                        本层
============================  ==========================  ==================
输入单元                      **整篇**说明书（最多 111 KB）  **整节**（【禁忌】）
输出约束                      **无**（自由文本）            **JSON**（`json_object`）
"该不该进图"                  **不判**                      **`generic_only` 标记**
============================  ==========================  ==================

为什么这三件事要一起上：**它们补的是同一个洞**。第一层（原子级）基线已经满分
1.000，而第二层（边级）F1 只有 0.469——**47 条假阳性边里 32 条出自"金标准明确
判定不该进图"的原子**。也就是说 L4 缺的不是"抽得准"，而是"抽完之后判该不该进图"。
这个判定按决策 #30 属于**抽取阶段**（不需要再多花一次 LLM 调用，也不需要对齐器去猜）。

> ⚠️ **为什么只跑【禁忌】一节。** 决策 #30 还规定了【注意事项】+【儿童用药】→
> `CAUTION_FOR`，但**这两节没有金标准**。没有金标准的产物**不可评测**，
> 做出来只是"看起来做了"。按本项目的纪律，不可评测的产出先不做——
> 等 CAUTION_FOR 的标注跟上再补。本层与基线的**唯一可比面就是【禁忌】**。

⚠️ **零样本。** 不写 few-shot 例子（决策 #26：**严禁取自冻结的测试集**）。
提示词里的判据是"通用过敏禁忌"这个**概念的定义**，不是照着金标准某几行反推的。

用法
----
    python scripts/extract_sections.py --dry-run   # 只报告规模与成本，不调 LLM
    python scripts/extract_sections.py             # 全量 36 篇，已跑过的跳过
    python scripts/extract_sections.py --limit 3   # 先试 3 篇

产出（``data/extracted/section-json/``，**入库**——它是消融实验的一格）
--------------------------------------------------------------------
    prompt.txt         完整提示词（system + user 模板），一字不改
    run_meta.json      模型、参数、时间、git commit、每篇的输入哈希与 token 用量
    raw/<药名>.json    模型**原始响应**，一字不改（解析失败时它是唯一的证据）
    items/<药名>.json  解析后的条目 ``[{text, generic_only}]``（给评分脚本读）
    summary.csv        药名 / 条目数 / generic_only 数 / token / 耗时
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import time
from pathlib import Path

from insert_io import TEXT_DIR, split_sections
from opencmkg_io import REPO_ROOT

#: scripts/ 与 backend/ 是两个独立目录。把 backend 挂进 sys.path 只为复用 app.config
sys.path.insert(0, str(REPO_ROOT / "backend"))

OUT_DIR = REPO_ROOT / "data" / "extracted" / "section-json"
RAW_DIR = OUT_DIR / "raw"
ITEMS_DIR = OUT_DIR / "items"

#: 本层只跑【禁忌】：它是基线的唯一可比面，也是**有金标准**的那一节。
#: 决策 #30 里的【注意事项】【儿童用药】→ CAUTION_FOR 待标注跟上后再补。
SECTIONS = ("禁忌",)

#: 从第 0 步延续下来的调用约定（与基线一致，好让 token / 耗时可比）
TEMPERATURE = 0.0
MAX_TOKENS = 2048
TIMEOUT = 180
NET_RETRY = 3

SYSTEM_PROMPT = """你是一名药学信息抽取助手。用户会给你药品说明书里【禁忌】这一节的原文。

请逐条列出该节里**该药禁用的情况**。

对每一条，判断它是不是「通用过敏禁忌」，并填进 `generic_only`：
- 「通用过敏禁忌」= 这一条**只**在说"对本品自身过敏"。过敏对象仅限：
  本品本身 / 它的活性成份 / 它的活性代谢产物 / 它的辅料 / 它的任一成份。
  这类条目几乎每份说明书都有，**不进知识图谱** → `generic_only` 填 `true`。
- 只要这一条里**另外指名了**本品以外的具体事物，就不是通用过敏禁忌
  → `generic_only` 填 `false`。例如：具体的疾病或症状、具体的人群
  （孕妇 / 儿童 / 老年人 / 育龄女性…）、另一类药物或另一种药
  （大环内酯类药物 / 圣约翰草…）。

要求：
- **照抄原文措辞**，不要改写、不要归纳、不要补充原文没有的信息
- 原文有几条就列几条，**照原文的分段来**，不要自己合并或拆散
- 若原文明确写着没有禁忌（例如整节只有一个「无」字，或"尚不明确"），
  `items` 返回**空数组**

只输出 JSON，不要解释：
{"items": [{"text": "照抄的原文条目", "generic_only": true}]}"""

USER_TEMPLATE = """药品说明书【{section}】小节原文：

{text}"""


def log(msg: str = "") -> None:
    print(msg, flush=True)


def git_commit() -> str:
    """读 .git 确定当前 commit——不调 subprocess（沙箱下管道会被拦）。"""
    git_dir = REPO_ROOT / ".git"
    head = git_dir / "HEAD"
    if not head.exists():
        return ""
    val = head.read_text(encoding="utf-8").strip()
    if not val.startswith("ref: "):
        return val
    ref = val[5:]
    p = git_dir / ref
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    packed = git_dir / "packed-refs"
    if packed.exists():
        for line in packed.read_text(encoding="utf-8").split("\n"):
            if line.endswith(" " + ref):
                return line.split(" ")[0]
    return ""


def load_units() -> list[dict]:
    """读 36 份分节文本，按 :data:`SECTIONS` 拆出**每一节一个输入单元**。"""
    units: list[dict] = []
    for p in sorted(TEXT_DIR.glob("*.txt")):
        text = p.read_text(encoding="utf-8")
        secs = split_sections(text)
        for name in SECTIONS:
            if name not in secs:
                units.append({"drug": p.stem, "section": name, "text": None,
                              "sha256": "", "chars": 0})
                continue
            body = secs[name]
            units.append({
                "drug": p.stem, "section": name, "text": body,
                "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "chars": len(body),
            })
    return units


def unit_key(u: dict) -> str:
    """一个输入单元对应一个产物文件名。多节时带上节名，避免互相覆盖。"""
    return u["drug"] if len(SECTIONS) == 1 else f"{u['drug']}__{u['section']}"


def make_client():
    from app.config import get_settings

    settings = get_settings()
    if not settings.llm_configured:
        raise SystemExit("✗ 百炼未配齐（缺 DASHSCOPE_API_KEY / DASHSCOPE_BASE_URL）")
    from openai import OpenAI

    return OpenAI(api_key=settings.dashscope_api_key,
                  base_url=settings.dashscope_base_url), settings


def call_once(client, model: str, section: str, text: str) -> dict:
    """一节一次调用，**带 JSON 约束**。返回原始响应与用量。"""
    last = None
    for attempt in range(1, NET_RETRY + 1):
        t0 = time.time()
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",
                     "content": USER_TEMPLATE.format(section=section, text=text)},
                ],
                temperature=TEMPERATURE,
                max_tokens=MAX_TOKENS,
                timeout=TIMEOUT,
                response_format={"type": "json_object"},
            )
            elapsed = time.time() - t0
            u = getattr(resp, "usage", None)
            return {
                "raw": (resp.choices[0].message.content or ""),
                "finish_reason": getattr(resp.choices[0], "finish_reason", ""),
                "prompt_tokens": getattr(u, "prompt_tokens", None),
                "completion_tokens": getattr(u, "completion_tokens", None),
                "elapsed": round(elapsed, 2),
                "error": "",
            }
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {str(exc)[:120]}"
            log(f"      第 {attempt} 次调用失败：{last}")
            if attempt < NET_RETRY:
                time.sleep(5 * attempt)
    return {"raw": "", "finish_reason": "", "prompt_tokens": None,
            "completion_tokens": None, "elapsed": 0.0, "error": last or ""}


def parse_items(raw: str) -> tuple[list[dict], str]:
    """把模型输出解析成 ``[{text, generic_only}]``。返回 ``(条目, 解析说明)``。

    **解析失败绝不静默**：原始响应已经一字不改地落盘，这里如实记下说明，
    由调用方计进 ``failed``——"模型答了但没解析出来"和"模型没答"是两件事，
    混在一起会让台账说谎。
    """
    txt = (raw or "").strip()
    if not txt:
        return [], "空响应"
    data = None
    try:
        data = json.loads(txt)
    except Exception:
        m = re.search(r"\{.*\}", txt, re.S)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception as exc:  # noqa: BLE001
                return [], f"JSON 解析失败：{type(exc).__name__}"
        else:
            return [], "响应里找不到 JSON 对象"
    if not isinstance(data, dict):
        return [], f"顶层不是对象（是 {type(data).__name__}）"
    raw_items = data.get("items")
    if raw_items is None:
        return [], "缺 items 字段"
    if not isinstance(raw_items, list):
        return [], f"items 不是数组（是 {type(raw_items).__name__}）"

    items, bad = [], 0
    for it in raw_items:
        if isinstance(it, str):
            items.append({"text": it.strip(), "generic_only": None})
            continue
        if not isinstance(it, dict):
            bad += 1
            continue
        t = str(it.get("text", "")).strip()
        if not t:
            bad += 1
            continue
        g = it.get("generic_only")
        items.append({"text": t,
                      "generic_only": g if isinstance(g, bool) else None})
    items = [it for it in items if it["text"]]
    note = ""
    if bad:
        note = f"{bad} 条格式不对已丢弃"
    if any(it["generic_only"] is None for it in items):
        note = (note + "；" if note else "") + "有条目没给 generic_only（记 None）"
    return items, note


def write_prompt_archive(model: str, *, force: bool) -> None:
    """落盘提示词。**已冻结就不许改**——改了就是另一层，不是这一层。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    new = (
        "# 消融层级 2 提示词（按小节送 + JSON 约束 + generic_only）\n"
        "# 零样本；判据是「通用过敏禁忌」的**定义**，不是照金标准反推的。\n"
        "\n"
        "## system\n"
        f"{SYSTEM_PROMPT}\n"
        "\n"
        "## user（{section} / {text} 处填入节名与该节正文）\n"
        f"{USER_TEMPLATE}\n"
        "\n"
        "## 参数\n"
        f"model           = {model}\n"
        f"temperature     = {TEMPERATURE}\n"
        f"max_tokens      = {MAX_TOKENS}\n"
        f"timeout         = {TIMEOUT}\n"
        f"response_format = json_object\n"
        f"网络层重试      = {NET_RETRY}（只重试调用失败，不做内容自检）\n"
        f"输入单元        = 整节（{', '.join(SECTIONS)}）\n"
    )
    p = OUT_DIR / "prompt.txt"
    if p.exists() and p.read_text(encoding="utf-8") != new and not force:
        raise SystemExit(
            "✗ 提示词/参数与已冻结的 prompt.txt 不一致。\n"
            "  这一层一旦冻结就不能改——改了就是**另一层**。\n"
            "  确实要重建请换目录，或加 --force 覆盖（并同步更新文档）。\n"
        )
    p.write_text(new, encoding="utf-8")


def read_prev() -> tuple[dict[str, dict], dict]:
    """读回上一轮的 ``(按单元索引的旧记录, 头部字段)``。

    续跑**必须把旧记录带过来**，否则 summary.csv 只含本轮跑过的几篇，
    台账被悄悄截断——L3 ``index.csv`` 踩过（决策 #31 ③）。
    头部里的首次 ``git_commit`` / ``started_at`` 是出身证明，续跑不许改写。
    """
    p = OUT_DIR / "run_meta.json"
    if not p.exists():
        return {}, {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}, {}
    return {r["unit"]: r for r in data.get("docs", [])}, data


def write_guarded(path: Path, text: str, encoding: str = "utf-8") -> None:
    """写文件；被别的程序占住时给出人能看懂的提示，而不是甩一个 PermissionError。"""
    try:
        path.write_text(text, encoding=encoding)
    except PermissionError:
        raise SystemExit(
            f"✗ 写不了 {path.name}：文件正被其他程序占用。\n"
            "  多半是你正在记事本 / Excel 里开着它——关掉再跑即可。\n"
            "  已经跑出来的 raw/ 不会丢，重跑会自动跳过它们。"
        )


def _csv_text(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="L4：按小节送 + JSON 约束 + generic_only")
    ap.add_argument("--dry-run", action="store_true", help="只报告规模与成本，不调 LLM")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 个单元")
    ap.add_argument("--force", action="store_true", help="连已跑过的也重跑")
    ap.add_argument("--model", default=None, help="覆盖 .env 里的 LLM_MODEL")
    args = ap.parse_args(argv)

    units = load_units()
    if args.limit:
        units = units[:args.limit]

    total = sum(u["chars"] for u in units)
    log("L4 第②步 · 按小节送 + JSON 约束 + generic_only")
    log(f"  单元数      {len(units)}（节：{', '.join(SECTIONS)}）")
    log(f"  总字符      {total:,}")
    log(f"  输出目录    {OUT_DIR.relative_to(REPO_ROOT)}")
    log("")

    if args.dry_run:
        # 基线的输入量从它自己的台账里读——**不是我估的，是实测的**
        base = REPO_ROOT / "data" / "extracted" / "baseline" / "run_meta.json"
        btok = None
        if base.exists():
            try:
                btok = sum(r.get("prompt_tokens") or 0
                           for r in json.loads(base.read_text(encoding="utf-8"))
                           .get("docs", []))
            except Exception:
                btok = None
        log("  --dry-run：不调 LLM。")
        log(f"    本层输入（【禁忌】节）        {total:>9,} 字符")
        if btok:
            # 中文按约 1 字 ≈ 1 token 粗估；基线的真值直接取台账
            log(f"    基线输入（整篇，台账实测）     {btok:>9,} token")
            log(f"    → 本层约为基线的 "
                f"{(total + len(units) * 200) / max(1, btok) * 100:.1f}%（含提示词开销粗估）")
        log("")
        log("  字符最多的 3 节：")
        for u in sorted(units, key=lambda x: -x["chars"])[:3]:
            log(f"    {u['drug']:<14} {u['section']}  {u['chars']:>5} 字符")
        n_empty = sum(1 for u in units if not (u["text"] or "").strip())
        log(f"  节内容为空的单元 {n_empty} 个"
            + ("（会照送，由模型返回空数组）" if n_empty else ""))
        return 0

    # 先判断这一轮有没有活儿干，**再决定动不动手写文件**。
    # 这是从基线那次踩坑学来的：一篇没跑却重写了 run_meta.json，
    # 把 git_commit / started_at / counts 全改了，"冻结"当场失效。
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    ITEMS_DIR.mkdir(parents=True, exist_ok=True)
    pending = [u for u in units
               if args.force or not (RAW_DIR / f"{unit_key(u)}.json").exists()]
    if not pending:
        log(f"  {len(units)} 个单元的输出都已存在，没有要跑的。")
        log("  产物保持冻结，本次不做任何写入。")
        log("  （确实要重跑请加 --force——那会覆盖已有输出并更新台账）")
        return 0

    client, settings = make_client()
    model = args.model or settings.llm_model
    log(f"  模型        {model}")
    log(f"  git commit  {git_commit()[:12] or '（未取到）'}")
    log("")

    write_prompt_archive(model, force=args.force)
    prev, prev_full = ({} , {}) if args.force else read_prev()

    meta = {
        "run": "L4 消融层级 2：按小节送 + JSON 约束 + generic_only",
        "decision": "#30 的后半（基线故意没做的那三件事）",
        "model": model,
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "timeout": TIMEOUT,
        "response_format": "json_object",
        "net_retry": NET_RETRY,
        "input": f"整节（data/collected/text/*.txt 的 {'、'.join(SECTIONS)} 节）",
        "output_constraint": "JSON（response_format=json_object）",
        "self_check": "解析失败如实记账（raw/ 原文一字不改地留着）",
        "baseline": "data/extracted/baseline（冻结，本层与它逐个变量对照）",
        "sections": list(SECTIONS),
        "git_commit": prev_full.get("git_commit") or git_commit(),
        "started_at": (prev_full.get("started_at")
                       or time.strftime("%Y-%m-%d %H:%M:%S")),
        "last_run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "docs": [],
    }

    rows = []
    done = skipped = failed = 0
    t_start = time.time()
    for i, u in enumerate(units, 1):
        key = unit_key(u)
        out_path = RAW_DIR / f"{key}.json"
        if out_path.exists() and not args.force:
            skipped += 1
            log(f"[{i}/{len(units)}] {key} —— 已有输出，跳过")
            old = prev.get(key)
            if old:
                meta["docs"].append(old)
                rows.append(old)
            continue

        log(f"[{i}/{len(units)}] {key}  （{u['section']} {u['chars']:,} 字符）")
        if u["text"] is None:
            # 该节在这份说明书里不存在——**如实记下，不假装抽过**
            rec = {"unit": key, "drug": u["drug"], "section": u["section"],
                   "input_chars": 0, "input_sha256": "",
                   "n_items": 0, "n_generic_only": 0, "output_chars": 0,
                   "finish_reason": "section_missing",
                   "prompt_tokens": 0, "completion_tokens": 0, "elapsed": 0.0,
                   "parse_note": "该节不存在，未调用", "error": ""}
            out_path.write_text("", encoding="utf-8")
            (ITEMS_DIR / f"{key}.json").write_text("[]", encoding="utf-8")
            meta["docs"].append(rec)
            rows.append(rec)
            skipped += 1
            log("      该节不存在，未调用")
            continue

        got = call_once(client, model, u["section"], u["text"])
        out_path.write_text(got["raw"], encoding="utf-8")
        items, note = parse_items(got["raw"])
        (ITEMS_DIR / f"{key}.json").write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

        rec = {
            "unit": key, "drug": u["drug"], "section": u["section"],
            "input_chars": u["chars"], "input_sha256": u["sha256"],
            "n_items": len(items),
            "n_generic_only": sum(1 for it in items if it["generic_only"]),
            "output_chars": len(got["raw"]),
            "finish_reason": got["finish_reason"],
            "prompt_tokens": got["prompt_tokens"],
            "completion_tokens": got["completion_tokens"],
            "elapsed": got["elapsed"],
            "parse_note": note,
            "error": got["error"],
        }
        meta["docs"].append(rec)
        rows.append(rec)

        if got["error"]:
            failed += 1
            log(f"      ✗ {got['error']}")
        elif note.startswith(("JSON 解析失败", "响应里找不到", "顶层不是",
                              "缺 items", "items 不是", "空响应")):
            failed += 1
            log(f"      ✗ 解析失败：{note}")
        else:
            done += 1
            log(f"      条目 {len(items)}（generic_only "
                f"{rec['n_generic_only']}） / in {got['prompt_tokens']} tok"
                f" / out {got['completion_tokens']} tok / {got['elapsed']}s"
                + (f" / {note}" if note else ""))

    meta["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    meta["wall_seconds"] = round(time.time() - t_start, 1)
    meta["counts"] = {"run": done, "skipped": skipped, "failed": failed}
    write_guarded(OUT_DIR / "run_meta.json",
                  json.dumps(meta, ensure_ascii=False, indent=2))
    if rows:
        write_guarded(OUT_DIR / "summary.csv", _csv_text(rows),
                      encoding="utf-8-sig")

    tin = sum(r["prompt_tokens"] or 0 for r in meta["docs"])
    tout = sum(r["completion_tokens"] or 0 for r in meta["docs"])
    n_items = sum(r["n_items"] for r in meta["docs"])
    n_gen = sum(r["n_generic_only"] for r in meta["docs"])
    log("")
    log("── 小结 ──")
    log(f"  本次跑通    {done}   跳过 {skipped}   失败 {failed}")
    log(f"  条目合计    {n_items}（其中判为 generic_only {n_gen}）")
    log(f"  输入 token  {tin:,}")
    log(f"  输出 token  {tout:,}")
    log(f"  墙钟        {meta['wall_seconds']}s")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
