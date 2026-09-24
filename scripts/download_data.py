#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
原始数据下载与完整性校验 —— OpenCMKG + DDInter
================================================

本脚本是 `data/raw/` 的**唯一重建入口**。
仓库不提交原始数据（体积大 + 部分有再分发限制，见 .gitignore 第 1 节），
换了电脑或数据损坏时，跑一遍本脚本即可恢复。

用法
----
    python scripts/download_data.py                 # 下载全部（已存在且未损坏的跳过）
    python scripts/download_data.py --only ddinter  # 只下 DDInter
    python scripts/download_data.py --verify-only   # 只校验，不联网
    python scripts/download_data.py --check-upstream# 用 HEAD 比对上游体积，看数据是否更新过
    python scripts/download_data.py --force         # 无视已存在的文件，全部重下

产物
----
    data/raw/opencmkg/{triples.txt, entities_dict.txt, manual_annotation.csv, README.md}
    data/raw/ddinter/ddinter_downloads_code_{A,B,D,H,L,P,R,V}.csv
    data/raw/MANIFEST.json        每个文件的 URL / 体积 / SHA-256 / 下载时间 / 许可

退出码
------
    0  全部文件就位且校验通过
    1  有文件缺失、体积为 0 或 SHA-256 与 MANIFEST 不符

数据来源与许可
--------------
    OpenCMKG  https://github.com/RuiqingDing/OpenCMKG   —— 学术研究用途
    DDInter   https://ddinter.scbdd.com/download/       —— CC BY-NC-SA 4.0
    两者均为学术非商业使用；论文与系统内必须署名，公开衍生图谱需沿用同协议。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------- 路径与常量

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = REPO_ROOT / "data" / "raw"
MANIFEST_PATH = RAW_DIR / "MANIFEST.json"

CHUNK = 256 * 1024
RETRIES = 3
TIMEOUT = 60

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 pharmkg-academic/0.1"
)

OPENCMKG_BRANCH = "main"
OPENCMKG_BASE = f"https://raw.githubusercontent.com/RuiqingDing/OpenCMKG/{OPENCMKG_BRANCH}"
DDINTER_BASE = "https://ddinter.scbdd.com/static/media/download"

LICENSE_OPENCMKG = "学术研究用途（OpenCMKG README 声明），不得商用"
LICENSE_DDINTER = "CC BY-NC-SA 4.0"


# ---------------------------------------------------------------- 数据源登记

@dataclass(frozen=True)
class Source:
    """一个待下载的原始文件。"""

    group: str
    filename: str
    url: str
    license: str
    note: str
    #: 2025 年探查时观察到的字节数，仅用于"上游是否变过"的告警，不作为校验依据
    ref_bytes: int | None = None

    @property
    def relpath(self) -> str:
        return f"{self.group}/{self.filename}"

    @property
    def dest(self) -> Path:
        return RAW_DIR / self.group / self.filename


def build_sources() -> list[Source]:
    src: list[Source] = []

    # ---- OpenCMKG：疾病-药物骨架图谱
    src.append(Source(
        group="opencmkg", filename="triples.txt",
        url=f"{OPENCMKG_BASE}/triples.txt", license=LICENSE_OPENCMKG,
        note="主骨架三元组（疾病/药物/症状/食物/科室/生产商）", ref_bytes=19_593_875,
    ))
    src.append(Source(
        group="opencmkg", filename="entities_dict.txt",
        url=f"{OPENCMKG_BASE}/entities_dict.txt", license=LICENSE_OPENCMKG,
        note="实体字典（id → 名称 / 类型 / 别名）", ref_bytes=1_472_527,
    ))
    src.append(Source(
        group="opencmkg", filename="manual_annotation.csv",
        url=f"{OPENCMKG_BASE}/manual_annotation.csv", license=LICENSE_OPENCMKG,
        note="作者人工标注样本，可作为 L8 图谱质量校验的参照",
    ))
    src.append(Source(
        group="opencmkg", filename="README.md",
        url=f"{OPENCMKG_BASE}/README.md", license=LICENSE_OPENCMKG,
        note="上游说明与许可声明，留档用于论文署名核对",
    ))

    # ---- DDInter：药物相互作用
    for code in "ABDHLPRV":
        src.append(Source(
            group="ddinter", filename=f"ddinter_downloads_code_{code}.csv",
            url=f"{DDINTER_BASE}/ddinter_downloads_code_{code}.csv", license=LICENSE_DDINTER,
            note=f"DDInter DDI 数据分片 {code}（分片依据未明，同一药对会跨片重复，须去重）",
        ))

    return src


# ---------------------------------------------------------------- 小工具

def human(n: float | int | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 网络

def open_url(url: str, method: str = "GET"):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method=method)
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def head_size(url: str) -> int | None:
    """HEAD 取上游体积；失败返回 None（不致命）。"""
    try:
        with open_url(url, "HEAD") as r:
            cl = r.headers.get("Content-Length")
            return int(cl) if cl and cl.isdigit() else None
    except Exception:
        return None


def download(src: Source, force: bool = False) -> tuple[bool, str]:
    """下载单个文件。返回 (是否成功, 说明)。已存在且体积相符则跳过。"""
    dest = src.dest
    part = dest.with_suffix(dest.suffix + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and not force:
        size = dest.stat().st_size
        if size > 0:
            return True, f"已存在，跳过（{human(size)}）"

    last_err = ""
    for attempt in range(1, RETRIES + 1):
        try:
            t0 = time.time()
            written = 0
            expected: int | None = None
            with open_url(src.url) as r:
                cl = r.headers.get("Content-Length")
                expected = int(cl) if cl and cl.isdigit() else None
                is_tty = sys.stdout.isatty()
                with part.open("wb") as f:
                    while True:
                        block = r.read(CHUNK)
                        if not block:
                            break
                        f.write(block)
                        written += len(block)
                        if is_tty and expected:
                            pct = written * 100 / expected
                            sys.stdout.write(
                                f"\r      {src.relpath}  {pct:5.1f}%  "
                                f"{human(written)}/{human(expected)}   "
                            )
                            sys.stdout.flush()
            if sys.stdout.isatty():
                sys.stdout.write("\r" + " " * 78 + "\r")
                sys.stdout.flush()

            if expected is not None and written != expected:
                raise IOError(f"传输不完整：收到 {written} B，响应头声明 {expected} B")
            if written == 0:
                raise IOError("下载到 0 字节")

            dest.unlink(missing_ok=True)
            part.replace(dest)
            dt = time.time() - t0
            speed = written / dt / 1024 if dt > 0 else 0
            return True, f"完成 {human(written)}  用时 {dt:.1f}s  {speed:.0f} KB/s"
        except Exception as e:  # noqa: BLE001 — 下载失败原因很多，统一重试
            last_err = f"{type(e).__name__}: {e}"
            part.unlink(missing_ok=True)
            if attempt < RETRIES:
                wait = 2 ** attempt
                log(f"      ! 第 {attempt} 次失败（{last_err}），{wait}s 后重试")
                time.sleep(wait)

    return False, f"失败（重试 {RETRIES} 次）：{last_err}"


# ---------------------------------------------------------------- MANIFEST

def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {}
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def write_manifest(entries: dict) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_说明": (
            "data/raw/ 的文件清单，由 scripts/download_data.py 生成。"
            "原始数据不入库，此文件同样被 .gitignore 排除；"
            "它只用于本机校验与复现，数据靠脚本 + 来源 URL 重建。"
        ),
        "generated_at": now_iso(),
        "files": {k: entries[k] for k in sorted(entries)},
    }
    MANIFEST_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------- 三个动作

def action_download(sources: list[Source], force: bool) -> int:
    manifest = load_manifest()
    entries = dict(manifest.get("files", {}))
    ok = bad = 0

    log(f"下载目标目录：{RAW_DIR}")
    log("")
    current_group = None
    for src in sources:
        if src.group != current_group:
            current_group = src.group
            log(f"── {current_group} ─────────────────────────────────────────────")
        log(f"  {src.relpath}")
        log(f"      {src.url}")
        success, msg = download(src, force=force)
        log(f"      {msg}")
        if success:
            st = src.dest.stat()
            entries[src.relpath] = {
                "group": src.group,
                "filename": src.filename,
                "url": src.url,
                "bytes": st.st_size,
                "sha256": sha256_of(src.dest),
                "downloaded_at": entries.get(src.relpath, {}).get("downloaded_at") or now_iso(),
                "license": src.license,
                "note": src.note,
            }
            ok += 1
        else:
            bad += 1
        log("")

    write_manifest(entries)
    log(f"清单已写入：{MANIFEST_PATH.relative_to(REPO_ROOT)}")
    log(f"结果：成功 {ok} 个，失败 {bad} 个")
    return 0 if bad == 0 else 1


def action_verify(sources: list[Source]) -> int:
    manifest = load_manifest().get("files", {})
    log("完整性校验")
    log("")
    log(f"  {'文件':<44} {'体积':>10}  {'SHA-256':<10} 状态")
    log("  " + "-" * 82)

    ok = bad = 0
    for src in sources:
        if not src.dest.exists():
            log(f"  {src.relpath:<44} {'—':>10}  {'—':<10} ✗ 缺失")
            bad += 1
            continue
        size = src.dest.stat().st_size
        if size == 0:
            log(f"  {src.relpath:<44} {human(size):>10}  {'—':<10} ✗ 空文件")
            bad += 1
            continue

        rec = manifest.get(src.relpath)
        if rec and rec.get("bytes") == size:
            digest = sha256_of(src.dest)
            if digest == rec.get("sha256"):
                state = "✓ 通过"
                ok += 1
            else:
                state = f"✗ SHA-256 不符（清单 {rec['sha256'][:12]}…）"
                bad += 1
            shown = digest[:8] + "…"
        else:
            state = "✓ 存在（首次校验，已补入清单）"
            shown = sha256_of(src.dest)[:8] + "…"
            ok += 1
        log(f"  {src.relpath:<44} {human(size):>10}  {shown:<10} {state}")

    log("")
    log(f"结果：通过 {ok} 个，异常 {bad} 个")
    return 0 if bad == 0 else 1


def action_check_upstream(sources: list[Source]) -> int:
    manifest = load_manifest().get("files", {})
    log("上游体积比对（HEAD 请求，不下载正文）")
    log("")
    log(f"  {'文件':<44} {'本地':>10} {'上游':>10}  结论")
    log("  " + "-" * 82)
    drift = 0
    for src in sources:
        local = src.dest.stat().st_size if src.dest.exists() else None
        upstream = head_size(src.url)
        if local is None:
            verdict = "本地缺失"
            drift += 1
        elif upstream is None:
            verdict = "上游未返回体积（跳过）"
        elif upstream == local:
            verdict = "一致"
        else:
            verdict = f"⚠ 上游已变化（差 {upstream - local:+,} B），建议 --force 重下"
            drift += 1
        log(f"  {src.relpath:<44} {human(local):>10} {human(upstream):>10}  {verdict}")
    log("")
    log(f"结果：{drift} 个文件需要关注")
    return 0


def action_list(sources: list[Source]) -> int:
    log(f"数据源登记表（共 {len(sources)} 个文件）")
    log("")
    for src in sources:
        log(f"  [{src.group}] {src.filename}")
        log(f"      URL      {src.url}")
        log(f"      许可     {src.license}")
        log(f"      说明     {src.note}")
        if src.ref_bytes:
            log(f"      参考体积 {src.ref_bytes:,} B（{human(src.ref_bytes)}）")
        log("")
    return 0


# ---------------------------------------------------------------- 入口

def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，中文会乱码；强制 UTF-8
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        description="下载并校验 OpenCMKG / DDInter 原始数据",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--only", choices=["opencmkg", "ddinter"],
                        help="只处理某一组数据")
    parser.add_argument("--force", action="store_true",
                        help="已存在的文件也重新下载")
    parser.add_argument("--verify-only", action="store_true",
                        help="只做本地完整性校验，不联网下载")
    parser.add_argument("--check-upstream", action="store_true",
                        help="用 HEAD 比对上游体积，判断数据是否更新过")
    parser.add_argument("--list", action="store_true",
                        help="只打印数据源登记表")
    args = parser.parse_args(argv)

    sources = build_sources()
    if args.only:
        sources = [s for s in sources if s.group == args.only]

    if args.list:
        return action_list(sources)
    if args.verify_only:
        return action_verify(sources)
    if args.check_upstream:
        return action_check_upstream(sources)

    rc = action_download(sources, force=args.force)
    log("")
    rc_verify = action_verify(sources)
    return 0 if (rc == 0 and rc_verify == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
