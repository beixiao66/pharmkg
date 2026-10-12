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
    python scripts/collect_inserts.py --limit 3      # 原型：只采前 3 个
    python scripts/collect_inserts.py                # 全量
    python scripts/collect_inserts.py --delay 8      # 放慢（默认 6 秒/请求）

产出
----
    data/collected/index.csv          采集清单：来源 URL、采集日期、核准/修改日期、厂家、状态
    data/collected/raw/<药名>.html    原始页面存档（可追溯、可复核）
    data/collected/text/<药名>.txt    纯文本，按说明书小节切好（供 L4 抽取）
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from opencmkg_io import REPO_ROOT

COLLECTED_DIR = REPO_ROOT / "data" / "collected"
RAW_DIR = COLLECTED_DIR / "raw"
TEXT_DIR = COLLECTED_DIR / "text"
INDEX_FILE = COLLECTED_DIR / "index.csv"

DRUGS_FILE = REPO_ROOT / "data" / "processed" / "high_risk_drugs.csv"

BASE = "https://drugs.dxy.cn"
SEARCH_URL = BASE + "/pc/search?keyword={kw}"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

#: 说明书小节标题——用于把正文切成结构化段落。顺序即说明书里的通常顺序。
SECTIONS = [
    "警示语", "成份", "规格", "适应症", "用法用量", "不良反应", "禁忌",
    "注意事项", "孕妇及哺乳期妇女用药", "儿童用药", "老年用药",
    "药物相互作用", "药物过量", "药理作用", "药理毒理", "药代动力学",
    "贮藏", "包装", "有效期", "执行标准", "批准文号",
    "上市许可持有人", "生产企业", "药品分类", "修改日期", "核准日期",
]

#: 网页版被登录墙截断的标记
GATE_MARK = "网页版仅展示部分"


#: 登录后的 cookie 与浏览器配置目录都放 _tmp/（已 gitignore，不进仓库）
COOKIE_FILE = REPO_ROOT / "_tmp" / "dxy_cookies.json"
PROFILE_DIR = REPO_ROOT / "_tmp" / "dxy-profile"

#: 用来判断"登录有没有生效"的探针药：它的【适应症】在小节被登录墙截断，
#: 登录后应变成完整文本。用**经验事实**判定，比猜某个 cookie 名可靠。
PROBE_DRUG = "西尼莫德"


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


def html_to_lines(html: str) -> list[str]:
    """把 HTML 拍成一行一段的纯文本。"""
    t = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<style.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"</(p|div|li|h[1-6]|tr)>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    return [ln.strip() for ln in t.split("\n") if ln.strip()]


def parse_search(html: str) -> list[dict]:
    """从搜索结果页取出说明书条目（药名 + 详情页链接）。"""
    # 详情页链接形如 /pc/drug/<id>?ky=<关键词>
    out, seen = [], set()
    for m in re.finditer(r'href="(/pc/drug/[^"]+)"', html):
        href = m.group(1)
        if href in seen:
            continue
        seen.add(href)
        out.append({"href": href, "url": BASE + href})
    return out


class _InsertParser(HTMLParser):
    """按 DOM 结构抽说明书字段（标准库实现，不引新依赖）。

    跟踪两个"层级"：
      * ``page_name__`` 的 h2      → 小节名
      * ``page_content__`` 的 div  → 该小节正文（可含嵌套 div，靠 depth 判结束）
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.date_parts: list[str] = []
        self.sections: dict[str, str] = {}
        #: 被登录墙截断的小节名——L4 只能从完整小节里抽
        self.truncated: list[str] = []

        self._name_level: int | None = None
        self._name_buf: list[str] = []
        self._pending_name: str | None = None

        self._content_level: int | None = None
        self._content_buf: list[str] = []
        self._content_gated = False

        self._in_date = False

    @staticmethod
    def _cls(attrs) -> str:
        return dict(attrs).get("class") or ""

    def handle_starttag(self, tag, attrs):
        cls = self._cls(attrs)
        if tag == "h2" and cls.startswith("page_name__"):
            self._name_level = self.depth
            self._name_buf = []
        elif tag == "div" and cls.startswith("page_content__"):
            self._content_level = self.depth
            self._content_buf = []
            self._content_gated = False
        elif tag == "p" and cls.startswith("page_drug-date__"):
            self._in_date = True
        elif tag == "a" and cls.startswith("page_login__"):
            # 登录墙就长在小节正文里：<a class="page_login__x">登录</a>
            self._content_gated = True
        self.depth += 1

    def handle_endtag(self, tag):
        self.depth -= 1
        if self._name_level is not None and self.depth == self._name_level:
            name = "".join(self._name_buf).strip()
            if name:
                self._pending_name = name
                self.sections.setdefault(name, "")
            self._name_level = None
        elif self._content_level is not None and self.depth == self._content_level:
            text = "\n".join(
                ln.strip() for ln in "".join(self._content_buf).split("\n")
                if ln.strip())
            if self._pending_name:
                self.sections[self._pending_name] = text
                if self._content_gated:
                    self.truncated.append(self._pending_name)
            self._pending_name = None
            self._content_level = None
        if tag == "p" and self._in_date:
            self._in_date = False

    def handle_data(self, data):
        if self._name_level is not None:
            self._name_buf.append(data)
        elif self._content_level is not None:
            self._content_buf.append(data)
        elif self._in_date:
            self.date_parts.append(data)


def parse_detail(html: str) -> dict:
    """从详情页取出结构化字段与各小节正文。

    为什么要按 DOM 结构解析而不是按文本行猜：页面长这样
    （class 后缀是 CSS-module 哈希，会随它重新部署而变，所以只匹配
    ``page_xxx__`` 前缀）——

        <p class="page_drug-date__x">
          <span>核准日期：<!-- -->2020年5月7日</span><span>|</span>
          <span>修改日期：<!-- -->2023年8月1日</span></p>
        <div class="page_item__x">
          <h2 class="page_name__x"><span>成份</span></h2>
          <div class="page_content__x"><div class="ck-content">…正文…</div></div>
        </div>

    早先按文本行猜的版本把「核准日期」和「修改日期」连成了一串、厂家取成 None，
    就是因为没利用这层结构。
    """
    p = _InsertParser()
    p.feed(html)

    # 药名：不假定标签名（实测是 <p>，但别赌），class 后缀是哈希只匹配前缀
    m = re.search(r'<(\w+)[^>]*class="page_drug-title__[^"]*"[^>]*>(.*?)</\1>',
                  html, re.S)
    title = re.sub(r"<[^>]+>", "", m.group(2)).strip() if m else ""
    title = re.sub(r"\s+", " ", title)

    dates = [d.strip() for d in
             "".join(p.date_parts).replace("|", "\n").split("\n") if d.strip()]
    approval = next((d for d in dates if "核准日期" in d), "")
    revision = next((d for d in dates if "修改日期" in d), "")
    approval = re.sub(r"^核准日期[：:]\s*", "", approval)
    revision = re.sub(r"^修改日期[：:]\s*", "", revision)

    return {
        "title": title,
        "核准日期": approval,
        "修改日期": revision,
        "上市许可持有人": p.sections.get("上市许可持有人", ""),
        "生产企业": p.sections.get("生产企业", ""),
        "truncated": p.truncated,
        "sections": {k: v for k, v in p.sections.items() if v},
    }


def load_targets(top: int | None, limit: int | None) -> list[dict]:
    if not DRUGS_FILE.exists():
        raise SystemExit(
            f"✗ 找不到 {DRUGS_FILE.relative_to(REPO_ROOT)}\n"
            "  请先运行：python scripts/select_high_risk_drugs.py"
        )
    with DRUGS_FILE.open(encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["selected"] == "1"]
    if top:
        rows = rows[:top]
    if limit:
        rows = rows[:limit]
    return rows


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

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    TEXT_DIR.mkdir(parents=True, exist_ok=True)

    targets = load_targets(top=None, limit=args.limit)
    log(f"L3 第②步 · 说明书采集")
    log(f"  数据源    {BASE}（丁香园用药助手）")
    log(f"  限速      {args.delay}s/请求")
    log(f"  目标      {len(targets)} 个药")
    log("")

    fetcher = Fetcher(args.delay, cookies=load_cookies())
    results = []
    skipped = 0
    for i, row in enumerate(targets, 1):
        kw = row["search_keyword"]

        # 断点续采：已经存过原文的就跳过。
        # 采集很慢（30s/请求）又容易触发人机验证，不能每次从头再来。
        if (RAW_DIR / f"{kw}.html").exists() and not args.redo:
            skipped += 1
            log(f"[{i}/{len(targets)}] {kw} —— 已有，跳过")
            continue

        log(f"[{i}/{len(targets)}] {kw}")

        search_url = SEARCH_URL.format(kw=urllib.parse.quote(kw))
        html = fetcher.get(search_url)
        if not html:
            results.append({**row, "status": "搜索失败", "url": search_url})
            continue

        hits = parse_search(html)
        if not hits:
            log("      未找到说明书条目")
            results.append({**row, "status": "无条目", "url": search_url})
            continue
        log(f"      搜到 {len(hits)} 个条目，取第一个")

        # 详情页是 SSR 的，但边缘缓存**时好时坏**：实测同一 URL 一次返回
        # 112KB 的完整说明书、下一次只有 35KB 的 SPA 壳。
        # 所以必须校验内容是否真的在，不在就重试。
        detail = None
        for attempt in range(1, 5):
            got = fetcher.get(hits[0]["url"], referer=search_url)
            if got and 'class="page_drug-title__' in got:
                detail = got
                break
            log(f"      第 {attempt} 次拿到壳页面"
                f"（{len(got) if got else 0:,} 字符），重试")
            time.sleep(args.delay)
        if not detail:
            log("      ✗ 多次都是壳页面，放弃")
            results.append({**row, "status": "只拿到壳页面",
                            "url": hits[0]["url"]})
            continue

        info = parse_detail(detail)
        secs = info["sections"]
        trunc = set(info["truncated"])
        log(f"      标题 {info['title']}")
        log(f"      核准 {info['核准日期']}  修改 {info['修改日期']}  "
            f"厂家 {str(info['生产企业'])[:24]}")
        log(f"      完整小节：{'、'.join(k for k in secs if k not in trunc) or '（无）'}")
        if trunc:
            log(f"      被登录墙截断：{'、'.join(sorted(trunc))}")

        # 存档
        (RAW_DIR / f"{kw}.html").write_text(detail, encoding="utf-8")
        body = [f"# {info['title']}", f"来源URL: {hits[0]['url']}",
                f"采集日期: {time.strftime('%Y-%m-%d')}",
                f"核准日期: {info['核准日期']}", f"修改日期: {info['修改日期']}",
                f"上市许可持有人: {info['上市许可持有人']}",
                f"生产企业: {info['生产企业']}",
                f"被截断的小节: {'、'.join(sorted(trunc)) or '（无）'}", ""]
        for name in SECTIONS:
            if name in secs:
                body.append(f"## {name}")
                body.append(secs[name])
                body.append("")
        (TEXT_DIR / f"{kw}.txt").write_text("\n".join(body), encoding="utf-8")

        results.append({
            **row,
            "status": "已采集",
            "url": hits[0]["url"],
            "collected_at": time.strftime("%Y-%m-%d"),
            "approval_date": info["核准日期"] or "",
            "revision_date": info["修改日期"] or "",
            "holder": (info["上市许可持有人"] or "").replace("\n", " "),
            "manufacturer": (info["生产企业"] or "").replace("\n", " "),
            "sections": "、".join(secs),
            "complete_sections": "、".join(k for k in secs if k not in trunc),
            "truncated_sections": "、".join(sorted(trunc)),
        })
        log("")

    # 写清单
    fields = ["rank", "cn_name", "search_keyword", "en_names", "Major",
              "status", "url", "collected_at", "approval_date", "revision_date",
              "holder", "manufacturer", "sections", "complete_sections",
              "truncated_sections"]
    with INDEX_FILE.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)

    ok = sum(1 for r in results if r["status"] == "已采集")
    jinji = sum(1 for r in results
                if "禁忌" in r.get("complete_sections", "").split("、"))
    log("── 小结 ──")
    log(f"  成功采集            {ok}/{len(results)}")
    log(f"  【禁忌】完整可用    {jinji}")
    log(f"  有被截断的小节      "
        f"{sum(1 for r in results if r.get('truncated_sections'))}")
    log("")
    log(f"  清单 {INDEX_FILE.relative_to(REPO_ROOT)}")
    log(f"  原文 {RAW_DIR.relative_to(REPO_ROOT)}/   文本 {TEXT_DIR.relative_to(REPO_ROOT)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
