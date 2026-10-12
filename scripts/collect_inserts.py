#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L3 第②步 · 说明书采集（原型）
==============================

按 L3 第①步选出的高危药名单，逐药采集说明书正文，本地存档并记录来源。

数据源
------
丁香园 · 用药助手（``drugs.dxy.cn``）。

路线选择的经过（**只记在这里，不写进项目文档**）：
基线原本写的是"NMPA 官网逐条说明书"。实测 NMPA 挂在阿里云 WAF 后面，
会对所有非浏览器客户端下发混淆 JS 挑战；用真实 Chrome（Playwright，
有头、抹掉自动化特征、排除代理干扰）依然走不通——
请求序列固定是 ``412 → 挑战脚本 200 → 带 cookie 重试 400``。
而 NMPA 的公开库本身也只是注册信息（批准文号/厂家/剂型），并非说明书全文库。

丁香园用药助手公开可访问、无 WAF，说明书正文完整，
并带 ``核准日期 / 修改日期 / 上市许可持有人 / 生产企业``——满足基线的可追溯要求。

⚠️ **可信度边界（论文里要如实写）**：丁香园是**聚合平台**，属二手转录，
非 NMPA 批准的监管原件；且网页版对【注意事项】【用法用量】有登录墙，
【孕妇及哺乳期妇女用药】【老年用药】不展示。
所以本管线**以【禁忌】为主**，其余槽位拿到多少算多少，缺口如实标注。

用法
----
    python scripts/collect_inserts.py --login       # 先登录，导出会话 cookie
    python scripts/collect_inserts.py --open        # 触发人机验证时，手动过掉
    python scripts/collect_inserts.py --limit 3     # 原型：只采前 3 个
    python scripts/collect_inserts.py               # 全量（30 秒/请求）
    python scripts/collect_inserts.py --candidates  # 只补采搜索结果页的候选条目

⚠ **请求间隔不要低于 30 秒**：实测 6 秒会在十几个药之后触发
"系统检测到您的网络中存在异常流量 / 进行人机验证"。

解析规则（含"块级换行 / 小节白名单 / 空标签 depth"三个坑）全部收在
``insert_io`` 里——**采集与重解析共用同一个解析器**，两处产物不会漂移。

产出
----
    data/collected/index.csv          采集清单：标题、来源 URL、采集日期、核准/修改日期、
                                      持有人、厂家、小节清单、候选数
    data/collected/candidates.csv     同名不同厂的**全部候选**（药名/规格/厂家/成分），
                                      并标出实际选中的那一条
    data/collected/raw/<药名>.html    原始页面存档（可追溯、可复核）
    data/collected/text/<药名>.txt    分节文本，按 HTML 文档顺序（供 L4 抽取）

清单是**合并**写入的：断点续采不会再抹掉前面已采的行。
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from insert_io import (
    BASE,
    CANDIDATE_FILE,
    COOKIE_FILE,
    INDEX_FILE,
    PROBE_DRUG,
    PROFILE_DIR,
    RAW_DIR,
    SEARCH_URL,
    TEXT_DIR,
    load_targets as load_all_targets,
    parse_detail,
    parse_search,
    read_candidates,
    read_index,
    render_text,
    write_candidates,
    write_index,
)
from opencmkg_io import REPO_ROOT

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

#: 用来判断"登录有没有生效"的探针药见 insert_io.PROBE_DRUG。


def log(msg: str = "") -> None:
    print(msg, flush=True)


class Fetcher:
    """带礼貌限速与退避的抓取器。

    实测直接连发若干请求就会吃 429，所以**每个请求之间固定间隔**，
    遇到 429/403/503 再按倍数退避——比盲目重试更不容易把 IP 打进黑名单。
    """

    def __init__(self, delay: float, cookies: dict[str, str] | None = None):
        self.delay = delay
        self.ctx = ssl.create_default_context()
        self.last = 0.0
        self.cookies = cookies or {}
        if self.cookies:
            log(f"  已加载 {len(self.cookies)} 个登录 cookie："
                f"{', '.join(sorted(self.cookies)[:4])}…")

    def get(self, url: str, *, referer: str | None = None) -> str | None:
        # 限速：距上次请求不足 delay 就补足
        gap = time.time() - self.last
        if gap < self.delay:
            time.sleep(self.delay - gap)

        headers = dict(HEADERS)
        if self.cookies:
            headers["Cookie"] = "; ".join(
                f"{k}={v}" for k, v in self.cookies.items())
        if referer:
            headers["Referer"] = referer
        req = urllib.request.Request(url, headers=headers)

        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30,
                                            context=self.ctx) as r:
                    raw = r.read(1_000_000)
                    self.last = time.time()
                    return raw.decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                self.last = time.time()
                if e.code in (429, 403, 503):
                    back = self.delay * (attempt + 2) * 2
                    log(f"      HTTP {e.code}，退避 {back:.0f}s 后重试")
                    time.sleep(back)
                    continue
                log(f"      HTTP {e.code} {e.reason}")
                return None
            except Exception as e:  # noqa: BLE001
                self.last = time.time()
                log(f"      {type(e).__name__}: {str(e)[:90]}")
                time.sleep(self.delay * (attempt + 1))
        return None


def load_cookies() -> dict[str, str]:
    if not COOKIE_FILE.exists():
        return {}
    try:
        return json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def probe_gate(fetcher: Fetcher) -> tuple[bool, str]:
    """抓一次探针药，看【适应症】是否还被登录墙截断。

    返回 ``(登录是否生效, 说明)``。用真实页面结果判定，不猜 cookie 名。
    """
    html = fetcher.get(SEARCH_URL.format(kw=urllib.parse.quote(PROBE_DRUG)))
    if not html:
        return False, "搜索失败"
    hits = parse_search(html)
    if not hits:
        return False, "无条目"
    detail = None
    for _ in range(3):
        got = fetcher.get(hits[0]["url"])
        if got and 'class="page_drug-title__' in got:
            detail = got
            break
        time.sleep(2)
    if not detail:
        return False, "只拿到壳页面"
    info = parse_detail(detail)
    trunc = info["truncated"]
    if "适应症" in trunc or "注意事项" in trunc:
        return False, f"仍有截断：{'、'.join(sorted(trunc))}"
    return True, "【适应症】【注意事项】均已完整"


def do_open(timeout: float) -> int:
    """开浏览器，让你手动过掉人机验证。

    **刻意不发任何额外 HTTP 请求**——只读已加载页面的 DOM 来判断验证有没有过。
    触发人机验证后再去戳站点，只会让判定更糟。
    """
    from playwright.sync_api import sync_playwright

    log("── 人工验证模式 ──")
    log("  马上弹出 Chrome，请把页面上的人机验证过掉。")
    log("  过掉之后页面会自己刷出搜索结果，脚本会自动识别并退出。")
    log(f"  最长等待 {timeout / 60:.0f} 分钟。")
    log("")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), channel="chrome", headless=False,
            locale="zh-CN", viewport={"width": 1280, "height": 880},
            args=["--no-first-run", "--no-default-browser-check"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(SEARCH_URL.format(kw=urllib.parse.quote(PROBE_DRUG)),
                      timeout=60000, wait_until="domcontentloaded")
        except Exception as e:
            log(f"  打开失败：{str(e)[:120]}")

        t0 = time.time()
        ok = False
        while time.time() - t0 < timeout:
            time.sleep(5)
            try:
                body = page.content()
            except Exception:
                continue
            verified = "人机验证" not in body
            has_result = "氯氮平" in body
            log(f"  [{int(time.time() - t0):>3}s] 验证已过={verified}  "
                f"出现结果={has_result}")
            if verified and has_result:
                ok = True
                break

        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
        COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        COOKIE_FILE.write_text(json.dumps(cookies, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        ctx.close()

    log("")
    if ok:
        log("  ✓ 验证已过，会话已更新")
        log("  下一步：放慢速度重跑，例如 --delay 30")
        return 0
    log("  ✗ 超时，验证似乎还没过")
    return 1


def do_login(timeout: float) -> int:
    """开一个受控浏览器让你自己登录，然后把会话 cookie 导出。

    **不接收密码**：登录全程在你的浏览器窗口里完成，脚本只读 cookie。
    """
    from playwright.sync_api import sync_playwright

    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    log("── 登录模式 ──")
    log(f"  浏览器配置目录 {PROFILE_DIR.relative_to(REPO_ROOT)}")
    log("  马上会弹出一个 Chrome 窗口，请在里面登录丁香园账号。")
    log("  登录成功后脚本会自动检测到并导出会话，然后关闭窗口。")
    log(f"  最长等待 {timeout / 60:.0f} 分钟。")
    log("  ⚠ 密码请只输入在那个浏览器窗口里，不要发给我。")
    log("")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            channel="chrome",
            headless=False,
            locale="zh-CN",
            viewport={"width": 1280, "height": 860},
            args=["--no-first-run", "--no-default-browser-check"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(BASE, timeout=60000, wait_until="domcontentloaded")
        except Exception as e:
            log(f"  打开首页失败：{str(e)[:120]}")

        t0 = time.time()
        ok = False
        reason = "超时"
        round_no = 0
        probed_after: frozenset | None = None
        while time.time() - t0 < timeout:
            time.sleep(15)
            round_no += 1
            cookies = {c["name"]: c["value"] for c in ctx.cookies()}
            if not cookies:
                continue
            # 只在 cookie 集合**发生过变化**时才去探测站点——
            # 否则在等用户登录的几分钟里会白发一堆请求，平白吃 429。
            fp = frozenset(cookies)
            if fp == probed_after:
                continue
            probed_after = fp
            COOKIE_FILE.write_text(
                json.dumps(cookies, ensure_ascii=False, indent=2),
                encoding="utf-8")
            probe = Fetcher(delay=3.0, cookies=cookies)
            ok, reason = probe_gate(probe)
            log(f"  [{round_no}] {len(cookies)} 个 cookie，检测：{reason}")
            if ok:
                break

        # 无论成功与否都把 cookie 落盘，方便下次直接复用
        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
        COOKIE_FILE.write_text(json.dumps(cookies, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        ctx.close()

    log("")
    if ok:
        log(f"  ✓ 登录生效（{reason}）")
        log(f"  cookie 已存 {COOKIE_FILE.relative_to(REPO_ROOT)}"
            f"（{len(cookies)} 个，已 gitignore）")
        log("  下一步：python scripts/collect_inserts.py   （会自动带上会话）")
        return 0
    log(f"  ✗ 未检测到登录生效：{reason}")
    log("  可以重跑 --login 再试；cookie 已存盘，也可能仍然有用。")
    return 1


def _base_row(target: dict) -> dict:
    """取 index.csv 里来自药名单的固定几列。"""
    return {k: target.get(k, "") for k in
            ("rank", "cn_name", "search_keyword", "en_names", "Major")}


def _row_from_info(target: dict, info: dict, url: str, collected_at: str,
                   n_candidates: str = "") -> dict:
    """把一次成功采集整理成 index.csv 的一行。"""
    secs = list(info["sections"])
    trunc = set(info["truncated"])
    return {
        **_base_row(target),
        "status": "已采集",
        "title": info["title"],
        "url": url,
        "collected_at": collected_at,
        "approval_date": info["核准日期"] or "",
        "revision_date": info["修改日期"] or "",
        "holder": (info["上市许可持有人"] or "").replace("\n", " "),
        "manufacturer": (info["生产企业"] or "").replace("\n", " "),
        "section_count": len(secs),
        "sections": "、".join(secs),
        "complete_sections": "、".join(k for k in secs if k not in trunc),
        "truncated_sections": "、".join(sorted(trunc)),
        "unknown_sections": "、".join(info["unknown_sections"]),
        "candidate_count": n_candidates,
    }


def _candidate_rows(kw: str, hits: list[dict]) -> list[dict]:
    """候选条目 → candidates.csv 的行。``idx=0`` 即实际选中的那一份。"""
    return [{
        "search_keyword": kw,
        "idx": i,
        "selected": 1 if i == 0 else 0,
        "title": c.get("title", ""),
        "spec": c.get("spec", ""),
        "manufacturer": c.get("manufacturer", ""),
        "ingredient": c.get("ingredient", ""),
        "url": c.get("url", ""),
    } for i, c in enumerate(hits)]


def _flatten(by_kw: dict[str, list[dict]]) -> list[dict]:
    return [r for items in by_kw.values() for r in items]


def do_candidates(delay: float, redo: bool) -> int:
    """只采**搜索结果页**，把候选条目全部落盘（不碰详情页）。

    采集规则是"取第一个"，但一个药常有多个同名不同厂的说明书
    （多拉司琼的"立必复"就有 5mL:100mg 与 1mL:12.5mg 两种规格，
    厂家也对不上）。只记 URL 的话，"为什么是这一份"就无法审计——
    所以把候选全量记下来，并把候选数回填进清单。
    """
    targets = load_all_targets()
    log("L3 第②步 · 候选条目采集（只取搜索结果页）")
    log(f"  数据源    {BASE}")
    log(f"  限速      {delay}s/请求")
    log(f"  目标      {len(targets)} 个药")
    log("")

    by_kw: dict[str, list[dict]] = {}
    for c in read_candidates():
        by_kw.setdefault(c["search_keyword"], []).append(c)
    have = set(by_kw)

    fetcher = Fetcher(delay, cookies=load_cookies())
    fetched = skipped = empty = 0
    for i, row in enumerate(targets, 1):
        kw = row["search_keyword"]
        if kw in have and not redo:
            skipped += 1
            log(f"[{i}/{len(targets)}] {kw} —— 已有候选记录，跳过")
            continue

        html = fetcher.get(SEARCH_URL.format(kw=urllib.parse.quote(kw)))
        if not html:
            log(f"[{i}/{len(targets)}] {kw} —— 搜索失败")
            continue
        hits = parse_search(html)
        by_kw[kw] = _candidate_rows(kw, hits)
        fetched += 1
        if not hits:
            empty += 1
        log(f"[{i}/{len(targets)}] {kw:<12} 候选 {len(hits):>2} 个"
            + (f"   {hits[0]['title'][:36]}" if hits else ""))

    write_candidates(_flatten(by_kw))

    # 把候选数回填进 index.csv——**只改这一列**，其余原样保留
    rows = read_index()
    filled = 0
    for r in rows:
        kw = r.get("search_keyword", "")
        if kw in by_kw:
            r["candidate_count"] = str(len(by_kw[kw]))
            filled += 1
    if rows:
        write_index(rows)

    log("")
    log("── 小结 ──")
    log(f"  本次采集    {fetched}   跳过 {skipped}   无候选 {empty}")
    log(f"  覆盖药数    {len(by_kw)}/{len(targets)}")
    log(f"  候选总条数  {sum(len(v) for v in by_kw.values())}")
    log(f"  候选 > 1    {sum(1 for v in by_kw.values() if len(v) > 1)} 个药"
        f"（「取第一个」的任意性就体现在这里）")
    log(f"  回填清单    {filled} 行")
    log("")
    log(f"  {CANDIDATE_FILE.relative_to(REPO_ROOT)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="L3 第②步：采集说明书")
    ap.add_argument("--limit", type=int, default=None, help="只采前 N 个药")
    ap.add_argument("--delay", type=float, default=30.0,
                    help="每个请求之间的间隔秒数，默认 30"
                         "（实测 6 秒会在 5 分钟内触发丁香园的人机验证）")
    ap.add_argument("--redo", action="store_true",
                    help="连已采过的也重新采（默认跳过，支持断点续采）")
    ap.add_argument("--candidates", action="store_true",
                    help="只采搜索结果页，落地候选条目后退出（不碰详情页）")
    ap.add_argument("--login", action="store_true",
                    help="开浏览器让你登录，导出会话 cookie 后退出")
    ap.add_argument("--open", dest="verify", action="store_true",
                    help="开浏览器让你过掉人机验证（不发额外请求）")
    ap.add_argument("--login-timeout", type=float, default=420,
                    help="登录/验证等待秒数，默认 420（7 分钟）")
    args = ap.parse_args(argv)

    if args.verify:
        return do_open(args.login_timeout)
    if args.login:
        return do_login(args.login_timeout)
    if args.candidates:
        return do_candidates(args.delay, args.redo)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    TEXT_DIR.mkdir(parents=True, exist_ok=True)

    targets = load_all_targets()
    if args.limit:
        targets = targets[:args.limit]
    log("L3 第②步 · 说明书采集")
    log(f"  数据源    {BASE}（丁香园用药助手）")
    log(f"  限速      {args.delay}s/请求")
    log(f"  目标      {len(targets)} 个药")
    log("")

    fetcher = Fetcher(args.delay, cookies=load_cookies())

    # 清单是**合并**写入的，不是覆写：断点续采那轮只处理剩下的药，
    # 覆写会把前面已采的行整批抹掉（实测丢过 8 个药，清单从 40 行缩到 32 行）。
    prev = {r.get("search_keyword", ""): r for r in read_index()}
    rows: dict[str, dict] = {}
    cand_by_kw: dict[str, list[dict]] = {}
    for c in read_candidates():
        cand_by_kw.setdefault(c["search_keyword"], []).append(c)

    skipped = 0
    for i, row in enumerate(targets, 1):
        kw = row["search_keyword"]

        # 断点续采：已经存过原文的就跳过。采集很慢（30s/请求）又容易触发
        # 人机验证，不能每次从头再来。
        if (RAW_DIR / f"{kw}.html").exists() and not args.redo:
            skipped += 1
            log(f"[{i}/{len(targets)}] {kw} —— 已有，跳过")
            if kw in prev:
                rows[kw] = prev[kw]
            continue

        log(f"[{i}/{len(targets)}] {kw}")
        search_url = SEARCH_URL.format(kw=urllib.parse.quote(kw))
        html = fetcher.get(search_url)
        if not html:
            rows[kw] = {**_base_row(row), "status": "搜索失败", "url": search_url}
            continue

        hits = parse_search(html)
        if hits:
            cand_by_kw[kw] = _candidate_rows(kw, hits)
        else:
            log("      未找到说明书条目")
            rows[kw] = {**_base_row(row), "status": "无条目", "url": search_url}
            continue
        first = hits[0]
        log(f"      搜到 {len(hits)} 个候选，取第一个："
            f"{first['title'] or first['name'] or first['url']}")

        # 详情页是 SSR 的，但边缘缓存**时好时坏**：实测同一 URL 一次返回
        # 112KB 的完整说明书、下一次只有 35KB 的 SPA 壳。所以必须校验。
        detail = None
        for attempt in range(1, 5):
            got = fetcher.get(first["url"], referer=search_url)
            if got and 'class="page_drug-title__' in got:
                detail = got
                break
            log(f"      第 {attempt} 次拿到壳页面"
                f"（{len(got) if got else 0:,} 字符），重试")
            time.sleep(args.delay)
        if not detail:
            log("      ✗ 多次都是壳页面，放弃")
            rows[kw] = {**_base_row(row), "status": "只拿到壳页面",
                        "url": first["url"]}
            continue

        info = parse_detail(detail)
        trunc = set(info["truncated"])
        now = time.strftime("%Y-%m-%d")
        log(f"      标题 {info['title']}")
        log(f"      核准 {info['核准日期']}  修改 {info['修改日期']}  "
            f"厂家 {str(info['生产企业'])[:24]}")
        log(f"      完整小节："
            f"{'、'.join(k for k in info['sections'] if k not in trunc) or '（无）'}")
        if trunc:
            log(f"      被登录墙截断：{'、'.join(sorted(trunc))}")

        # 存档：原始页面 + 分节文本，文本由**同一个解析器**产出
        (RAW_DIR / f"{kw}.html").write_text(detail, encoding="utf-8")
        (TEXT_DIR / f"{kw}.txt").write_text(
            render_text(info, url=first["url"], collected_at=now),
            encoding="utf-8")
        rows[kw] = _row_from_info(row, info, first["url"], now, str(len(hits)))
        log("")

    out = [rows[r["search_keyword"]] for r in targets
           if r["search_keyword"] in rows]
    write_index(out)
    if cand_by_kw:
        write_candidates(_flatten(cand_by_kw))

    ok = sum(1 for r in out if r["status"] == "已采集")
    jinji = sum(1 for r in out
                if "禁忌" in (r.get("complete_sections") or "").split("、"))
    log("── 小结 ──")
    log(f"  成功采集            {ok}/{len(out)}")
    log(f"  【禁忌】完整可用    {jinji}")
    log(f"  有被截断的小节      "
        f"{sum(1 for r in out if r.get('truncated_sections'))}")
    log("")
    log(f"  清单 {INDEX_FILE.relative_to(REPO_ROOT)}")
    log(f"  原文 {RAW_DIR.relative_to(REPO_ROOT)}/   "
        f"文本 {TEXT_DIR.relative_to(REPO_ROOT)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
