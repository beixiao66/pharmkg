#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L4 第①步 · 朴素基线的运行与冻结
================================

按决策 #30 / Q6：**L4 的第一个动作是跑朴素基线并冻结**，之后所有改进
（按小节切分、JSON 约束、两次抽取比对）都对着它比。

「朴素」是刻意定义的，四个"没有"：

* **没有按小节切分**——整篇说明书一次送（正文最多 111 KB）
* **没有一次调用之外的处理**——一篇一次 ``chat.completions``，不重试内容
* **没有 JSON schema 约束**——自由文本输出
* **没有自检**——不做格式校验、不做一致性比对、不做补救

⚠️ **基线必须先于任何改进跑完并存档**。事后补的基线没有说服力，
答辩时会被质疑"你是照着改进结果反推的基线"。

为什么不重采：说明书已在 ``data/collected/text/`` 冻结（决策 #31），
本步**完全离线**，只调百炼。

用法
----
    python scripts/extract_baseline.py --dry-run     # 只报告规模与成本，不调 LLM
    python scripts/extract_baseline.py               # 全量 36 篇，已跑过的跳过
    python scripts/extract_baseline.py --limit 3     # 先试 3 篇
    python scripts/extract_baseline.py --force       # 连已跑过的也重跑

产出（``data/extracted/baseline/``，**入库**——冻结的基线是论文证据链的一环）
--------------------------------------------------------------------------------
    prompt.txt         完整提示词（system + user 模板），一字不改
    run_meta.json      模型、参数、时间、git commit、每篇的输入哈希与 token 用量
    raw/<药名>.txt     每篇的**原始输出**，不做任何加工
    summary.csv        药名 / 输出字数 / token / 耗时，便于一眼看全貌
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import time
from pathlib import Path

from insert_io import TEXT_DIR
from opencmkg_io import REPO_ROOT

#: scripts/ 与 backend/ 是两个独立目录。把 backend 挂进 sys.path 只为复用 app.config
sys.path.insert(0, str(REPO_ROOT / "backend"))

OUT_DIR = REPO_ROOT / "data" / "extracted" / "baseline"
RAW_DIR = OUT_DIR / "raw"

#: 从第 0 步延续下来的调用约定
TEMPERATURE = 0.0
MAX_TOKENS = 2048
TIMEOUT = 180
#: 网络层重试次数——注意这**不是**自检：只重试"调用没成功"，
#: 不检查"模型答得对不对"。内容层面的补救属于改进组，不属于基线。
NET_RETRY = 3

SYSTEM_PROMPT = """你是一名药学信息抽取助手。用户会给你一份药品说明书的全文。

请阅读全文后，列出该药品的**禁忌**。

要求：
- 只依据说明书原文，不要补充原文没有的信息
- 按条目分行输出
- 不要输出与禁忌无关的章节内容"""

USER_TEMPLATE = """以下是药品说明书全文：

{text}"""

#: L4 真正要抽的三节（决策 #30 / Q4）——用来对比"整篇送"与"按小节送"的输入量
TARGET_SECTIONS = ("禁忌", "注意事项", "儿童用药")


def target_chars(text: str) -> int:
    """只数【禁忌】【注意事项】【儿童用药】三节的字符数。"""
    total = 0
    cur = None
    for ln in text.split("\n"):
        if ln.startswith("## "):
            cur = ln[3:].strip()
            continue
        if cur in TARGET_SECTIONS:
            total += len(ln) + 1
    return total


def log(msg: str = "") -> None:
    print(msg, flush=True)


def git_commit() -> str:
    """读 .git 确定当前 commit——不调 subprocess（沙箱下管道会被拦）。"""
    git_dir = REPO_ROOT / ".git"
    head = (git_dir / "HEAD")
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


def load_docs() -> list[dict]:
    """读 36 份分节文本。整篇原样送，不做任何裁剪。"""
    docs = []
    for p in sorted(TEXT_DIR.glob("*.txt")):
        text = p.read_text(encoding="utf-8")
        docs.append({
            "drug": p.stem,
            "text": text,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "chars": len(text),
        })
    return docs


def make_client():
    from app.config import get_settings

    settings = get_settings()
    if not settings.llm_configured:
        raise SystemExit(
            "✗ 百炼未配齐（缺 DASHSCOPE_API_KEY / DASHSCOPE_BASE_URL）"
        )
    from openai import OpenAI

    return OpenAI(api_key=settings.dashscope_api_key,
                  base_url=settings.dashscope_base_url), settings


def call_once(client, model: str, text: str) -> dict:
    """一篇一次调用。返回原始输出与用量。"""
    last = None
    for attempt in range(1, NET_RETRY + 1):
        t0 = time.time()
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",
                     "content": USER_TEMPLATE.format(text=text)},
                ],
                temperature=TEMPERATURE,
                max_tokens=MAX_TOKENS,
                timeout=TIMEOUT,
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


def write_prompt_archive(model: str, *, force: bool) -> None:
    """落盘提示词。**已冻结就不许改**——改了就是另一条基线。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    new = (
        "# 基线提示词（决策 #30 / Q6，冻结于首次运行）\n"
        "# 一字不改地存档——它是这条基线的定义本身。\n"
        "\n"
        "## system\n"
        f"{SYSTEM_PROMPT}\n"
        "\n"
        "## user（{text} 处填入整篇说明书文本）\n"
        f"{USER_TEMPLATE}\n"
        "\n"
        "## 参数\n"
        f"model           = {model}\n"
        f"temperature     = {TEMPERATURE}\n"
        f"max_tokens      = {MAX_TOKENS}\n"
        f"timeout         = {TIMEOUT}\n"
        f"网络层重试      = {NET_RETRY}（只重试调用失败，不做内容自检）\n"
    )
    p = OUT_DIR / "prompt.txt"
    if p.exists() and p.read_text(encoding="utf-8") != new and not force:
        raise SystemExit(
            "✗ 提示词/参数与已冻结的 prompt.txt 不一致。\n"
            "  基线一旦冻结就不能改——改了就是**另一条**基线。\n"
            "  确实要重建请换目录，或加 --force 覆盖（并同步更新文档）。\n"
        )
    p.write_text(new, encoding="utf-8")


def read_prev_meta() -> dict[str, dict]:
    """读回上一次的 run_meta.json，按药名索引。

    续跑时**必须把旧记录带过来**——否则 summary.csv 只含本轮跑过的几篇，
    台账被悄悄截断。这正是 L3 ``index.csv`` 踩过的坑（决策 #31 ③）。
    """
    p = OUT_DIR / "run_meta.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {r["drug"]: r for r in data.get("docs", [])}
    except Exception:
        return {}


def read_prev_header() -> dict:
    """读回上一轮 meta 的**头部字段**。

    续跑只能**追加**，不能改写来源——首次运行的 ``git_commit`` / ``started_at``
    是这条基线的出身证明，被后来某次续跑覆盖掉就说不清了。
    """
    p = OUT_DIR / "run_meta.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_guarded(path: Path, text: str, encoding: str = "utf-8") -> None:
    """写文件；被别的程序占住时给出人能看懂的提示，而不是甩一个 PermissionError。

    Windows 上记事本 / Excel 打开文件时持有共享锁，脚本再以 ``w`` 打开就会
    ``[Errno 13] Permission denied``——这个报错对使用者毫无指向性。
    """
    try:
        path.write_text(text, encoding=encoding)
    except PermissionError:
        raise SystemExit(
            f"✗ 写不了 {path.name}：文件正被其他程序占用。\n"
            "  多半是你正在记事本 / Excel 里开着它——关掉再跑即可。\n"
            "  已经跑出来的 raw/ 输出不会丢，重跑会自动跳过它们。"
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

    ap = argparse.ArgumentParser(description="L4：朴素基线的运行与冻结")
    ap.add_argument("--dry-run", action="store_true",
                    help="只报告规模与成本，不调 LLM")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 篇")
    ap.add_argument("--force", action="store_true",
                    help="连已跑过的也重跑（默认跳过，支持断点续跑）")
    ap.add_argument("--model", default=None, help="覆盖 .env 里的 LLM_MODEL")
    args = ap.parse_args(argv)

    docs = load_docs()
    if args.limit:
        docs = docs[:args.limit]

    total_chars = sum(d["chars"] for d in docs)
    log("L4 第①步 · 朴素基线（整篇输入 / 单次调用 / 无 JSON 约束 / 无自检）")
    log(f"  篇数        {len(docs)}")
    log(f"  总字符      {total_chars:,}")
    log(f"  平均字符    {total_chars / max(1, len(docs)):,.0f}")
    log(f"  输出目录    {OUT_DIR.relative_to(REPO_ROOT)}")
    log("")

    if args.dry_run:
        tgt = sum(target_chars(d["text"]) for d in docs)
        # 中文按约 1 字 ≈ 1 token 粗估（真值只有调完才知道，见 run_meta.json）
        log("  --dry-run：不调 LLM。粗估输入 token：")
        log(f"    整篇送（基线）    ≈ {total_chars / 10000:>6.1f} 万 token"
            f"   100%")
        log(f"    按小节送（改进组）≈ {tgt / 10000:>6.1f} 万 token"
            f"   {tgt / max(1, total_chars) * 100:.1f}%")
        log(f"    → 基线大约是改进组的 {total_chars / max(1, tgt):.1f} 倍")
        log("")
        log("  前 3 篇与后 3 篇：")
        for d in docs[:3] + docs[-3:]:
            log(f"    {d['drug']:<14} 整篇 {d['chars']:>7,} 字符"
                f"   三节 {target_chars(d['text']):>6,} 字符")
        return 0

    # 先判断这一轮有没有活儿干，**再决定动不动手写文件**。
    # 曾经栽过：一篇都没跑（36 篇全部跳过）却仍然重写了 run_meta.json，
    # 把 git_commit / started_at / counts 全改了——"冻结"当场失效。
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    pending = [d for d in docs
               if args.force or not (RAW_DIR / f"{d['drug']}.txt").exists()]
    if not pending:
        log(f"  {len(docs)} 篇输出都已存在，没有要跑的篇目。")
        log("  产物保持冻结，本次不做任何写入。")
        log("  （确实要重跑请加 --force——那会覆盖已有输出并更新台账）")
        return 0

    client, settings = make_client()
    model = args.model or settings.llm_model
    log(f"  模型        {model}")
    log(f"  git commit  {git_commit()[:12] or '（未取到）'}")
    log("")

    write_prompt_archive(model, force=args.force)
    prev_meta = read_prev_meta()
    # 续跑只追加来源，不改写；--force 是重做，才允许重置
    prev_full = {} if args.force else read_prev_header()

    meta = {
        "run": "L4 朴素基线",
        "decision": "#30 / Q6",
        "model": model,
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "timeout": TIMEOUT,
        "net_retry": NET_RETRY,
        "input": "整篇说明书（data/collected/text/*.txt，原样，不裁剪）",
        "output_constraint": "无（自由文本，没有 JSON schema）",
        "self_check": "无",
        "git_commit": prev_full.get("git_commit") or git_commit(),
        "started_at": (prev_full.get("started_at")
                       or time.strftime("%Y-%m-%d %H:%M:%S")),
        "last_run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "docs": [],
    }

    rows = []
    done = skipped = failed = 0
    t_start = time.time()
    for i, d in enumerate(docs, 1):
        drug = d["drug"]
        out_path = RAW_DIR / f"{drug}.txt"
        if out_path.exists() and not args.force:
            skipped += 1
            log(f"[{i}/{len(docs)}] {drug} —— 已有输出，跳过")
            old = prev_meta.get(drug)
            if old:
                meta["docs"].append(old)
                rows.append(old)
            continue

        log(f"[{i}/{len(docs)}] {drug}  （{d['chars']:,} 字符）")
        got = call_once(client, model, d["text"])
        out_path.write_text(got["raw"], encoding="utf-8")

        rec = {
            "drug": drug,
            "input_chars": d["chars"],
            "input_sha256": d["sha256"],
            "output_chars": len(got["raw"]),
            "finish_reason": got["finish_reason"],
            "prompt_tokens": got["prompt_tokens"],
            "completion_tokens": got["completion_tokens"],
            "elapsed": got["elapsed"],
            "error": got["error"],
        }
        meta["docs"].append(rec)
        rows.append(rec)

        if got["error"]:
            failed += 1
            log(f"      ✗ {got['error']}")
        else:
            done += 1
            log(f"      输出 {len(got['raw']):,} 字符 / "
                f"in {got['prompt_tokens']} tok / out {got['completion_tokens']} tok"
                f" / {got['elapsed']}s")

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
    log("")
    log("── 小结 ──")
    log(f"  本次跑通    {done}   跳过 {skipped}   失败 {failed}")
    log(f"  输入 token  {tin:,}")
    log(f"  输出 token  {tout:,}")
    log(f"  墙钟        {meta['wall_seconds']}s")
    log("")
    log(f"  {OUT_DIR.relative_to(REPO_ROOT)}/")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
