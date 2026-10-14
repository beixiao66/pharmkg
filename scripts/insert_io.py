#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
丁香园·用药助手 说明书读取层（L3 采集 / L3 重解析 / L4 抽取 共用）
====================================================================

把「HTML → 分节结构化说明书」这件事收敛到一处，免得采集、重解析、
抽取三步各写一遍、各踩一遍同样的坑。

数据形态（已实测）
------------------
详情页是**压缩成一整行的** SSR HTML（449 KB 里只有 6 个换行符）：

.. code-block:: html

    <div class="page_item__x" data-menu-index="4">
      <h2 class="page_name__x"><span>禁忌</span></h2>
      <div class="page_content__x">
        <div class="ck-content">
          <ul><li>对本品活性成份或任何辅料过敏者禁用</li>
              <li>活动性结核病或活动性严重感染</li>…</ul>
        </div>
      </div>
    </div>

排版靠三样东西，**都不产生文本节点**：

* 段落 = ``<div>``
* 段内换行 = ``<p class="new-line"></p>``（**空** ``<p>``，靠 CSS 撑开）
* 列表项 = ``<li>``

踩过的三个坑
------------
1. **换行只能靠标签结构发出来，不能事后猜。**
   初版只把 ``handle_data`` 收到的文本拼起来，而上面三样全是标签、
   没有文本节点——4 条禁忌于是被焊成
   ``对本品活性成份或任何辅料过敏者禁用活动性结核病或活动性严重感染重度肝功能损害妊娠``。
   实测：473 个正文区域、11,895 个块级闭合标签；重解析出 **11,031 行**，
   而旧产物全文只有 **644 行**（**18.5 倍压缩**），391/417 个已写小节
   是**恰好 1 行**。
   正确做法：**块级标签进出各发一个换行**（:data:`BLOCK_TAGS`）。
2. **小节名不能做成白名单。**
   初版只把 ``SECTIONS`` 里列到的小节写进 txt，而那份名单漏了
   【临床试验】【毒理研究】【用药须知】【化学成份】——**56 个小节被静默丢弃**，
   可 ``index.csv`` 的 ``sections`` 列照旧列着它们。
   **元数据说有、产物里没有**，比缺数据更危险。
   现在按 HTML 里的**文档顺序**全部写出；:data:`KNOWN_SECTIONS` 只用于
   报告"出现了没见过的节名"，不参与过滤、也不参与排序。
3. **空标签（``<br>``、``<img>``）不能让 depth 漂移。**
   ``HTMLParser`` 不认识自闭合标签：``<br/>`` 会同时触发
   ``handle_starttag`` 和 ``handle_endtag``，而 ``<br>`` 只触发前者。
   初版无条件 ``depth += 1 / -= 1``，depth 会一路偏掉、小节边界随之错位。
   现在显式维护 :data:`VOID_TAGS`。
4. **表格不能按普通块级标签拍平。**
   站点用的是真 ``<table>``。让每个 ``td`` 各占一行，行列对应关系就没了；
   更麻烦的是**空单元格不产生任何文本**，拍平之后分不清"原文就是空"和
   "解析掉了"——与【禁忌】= ``无`` 是同一类诚信问题。
   实测 36 份里共 **136 张表**（临床试验 86、不良反应 24、用法用量 21、
   药代动力学 3、药理作用 1、注意事项 1；其中只有 1 张落在 L4 抽取目标里）。
   现在：**一行表格行 = 一行文本**，格与格之间发 ``" | "``（第 2 格起、
   **空单元格照发**），行结束发换行；格内的 ``<br>``（一格列多个条目）
   换成 ``"；"``，以免把这一行拆散。见 :data:`CELL_TAGS` / :data:`CELL_SEP`。

搜索结果页（候选条目）
----------------------
一个药在丁香园常有多个同名不同厂的说明书。候选在
``li.DrugsItem_drugs-item__`` 里，每条带**药名 / 厂商 / 成分**：

.. code-block:: html

    <li class="DrugsItem_drugs-item__x">
      <a class="DrugsItem_link__x" href="/pc/drug/<id>?ky=…">
        <h3 class="DrugsItem_drugs-item-name__x"><span>瑞福（乌帕替尼缓释片） 15mg；30mg；45mg</span></h3>
        <p class="DrugsItem_drugs-item-cnName__x">厂商：<span>AbbVie…</span>   成分：<span>本品主要成份为…</span></p>
      </a>
    </li>

⚠ 页面里还有一份 Next.js 飞行数据（``self.__next_f``），里面同样出现
``DrugsItem_drugs-item__`` 字样，但那是**转义过的 JSON**
（``\\"className\\":\\"DrugsItem_…``）、不是标签，而且链接是 ``/drug/`` 而非
``/pc/drug/``。只匹配 ``class="…`` 前缀即可，否则候选数会凭空翻倍。

用法
----
    python scripts/insert_io.py          # 自检：把 raw/ 全解析一遍并报告
"""

from __future__ import annotations

import csv
import re
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

from opencmkg_io import REPO_ROOT

# ── 路径 ────────────────────────────────────────────────────────────────
COLLECTED_DIR = REPO_ROOT / "data" / "collected"
RAW_DIR = COLLECTED_DIR / "raw"
TEXT_DIR = COLLECTED_DIR / "text"
INDEX_FILE = COLLECTED_DIR / "index.csv"
CANDIDATE_FILE = COLLECTED_DIR / "candidates.csv"

#: L3 第①步选出的高危药名单（含 selected 列）
DRUGS_FILE = REPO_ROOT / "data" / "processed" / "high_risk_drugs.csv"

# ── 数据源 ──────────────────────────────────────────────────────────────
BASE = "https://drugs.dxy.cn"
SEARCH_URL = BASE + "/pc/search?keyword={kw}"

#: 来源标记（基线 §13.8 要求每条链路带 source）
SOURCE_TAG = "dxy"

#: ⚠ 页面上那句"网页版仅展示部分"**不能**当截断判据——它在**没有被截断**的页面上
#: 也会出现（实测西尼莫德页有这句话，可它的【适应症】【注意事项】都是完整的）。
#: 真正的判据是小节正文里有没有 ``<a class="page_login__…">登录</a>``，
#: 见 :meth:`_InsertParser._start`。

#: 登录后的 cookie 与浏览器配置目录（_tmp/ 已 gitignore）
COOKIE_FILE = REPO_ROOT / "_tmp" / "dxy_cookies.json"
PROFILE_DIR = REPO_ROOT / "_tmp" / "dxy-profile"

#: 探针药：它的【适应症】在小节被登录墙截断，登录后应变成完整文本。
#: 用**经验事实**判定登录是否生效，比猜某个 cookie 名可靠。
PROBE_DRUG = "西尼莫德"

#: 说明书小节的规范顺序。
#: ⚠ **这不是白名单**——不在表里的小节照样会写进 txt，只是会被报为"没见过的节名"。
#: 之前把它当白名单用，才丢了 4 个小节共 56 处。
KNOWN_SECTIONS = (
    "警示语", "成份", "性状", "规格", "适应症", "用法用量", "不良反应", "禁忌",
    "注意事项", "孕妇及哺乳期妇女用药", "儿童用药", "老年用药",
    "药物相互作用", "药物过量", "药理作用", "药理毒理", "毒理研究",
    "药代动力学", "临床试验", "用药须知", "化学成份",
    "贮藏", "包装", "有效期", "执行标准", "批准文号",
    "上市许可持有人", "生产企业", "药品分类", "修改日期", "核准日期",
)

#: 块级标签——**进出各发一个换行**。行内标签（span/i/u/strong/sub/font…）不在内。
#: 注意 ``td`` / ``th`` / ``tr`` **不在**这里，它们在 :data:`CELL_TAGS`
#: 与 :data:`ROW_TAGS` 里单独处理。
BLOCK_TAGS = frozenset({
    "div", "p", "li", "ul", "ol", "dl", "dt", "dd", "table", "thead", "tbody",
    "h1", "h2", "h3", "h4", "h5", "h6", "br", "hr",
    "blockquote", "pre", "figure", "figcaption", "section", "article",
    "header", "footer", "main", "aside", "nav",
})

#: 表格单元格 / 表格行——**单独处理**，不能当普通块级标签。
#:
#: 每一格发一个换行会把行列对应关系彻底打散（实测 36 份里有 **136 张表**），
#: 而且**空单元格不产生任何文本**，拍平之后分不清"原文就是空"和"解析掉了"——
#: 与【禁忌】= ``无`` 是同一类诚信问题。
#:
#: 规则：同一行里**第 2 格起先发分隔符（空单元格照发）**，行结束发换行。
#: 于是"表格的一行 = 文本的一行"。
CELL_TAGS = frozenset({"td", "th"})
ROW_TAGS = frozenset({"tr"})

#: 单元格之间的分隔符
CELL_SEP = " | "

#: 单元格**内部**的 ``<br>``——一格常列多个条目（如"带状疱疹<br/>单纯疱疹<br/>胃肠炎"），
#: 若也发换行就会把这一行拆散，所以换成不破行的分号
CELL_BR = "；"

#: 空标签——**不改变 depth**。HTMLParser 不认识它们，必须自己管。
VOID_TAGS = frozenset({
    "br", "hr", "img", "input", "meta", "link", "source", "col",
    "area", "base", "embed", "param", "track", "wbr",
})

# ── 搜索页的 class 前缀（后缀是 CSS-module 哈希，会随站点重新部署而变）──
_ITEM_CLS = "DrugsItem_drugs-item__"
_NAME_CLS = "DrugsItem_drugs-item-name__"
_META_CLS = "DrugsItem_drugs-item-cnName__"

#: 详情页的 class 前缀
_PAGE_NAME_CLS = "page_name__"
_PAGE_CONTENT_CLS = "page_content__"
_PAGE_DATE_CLS = "page_drug-date__"
_PAGE_LOGIN_CLS = "page_login__"
_PAGE_TITLE_CLS = "page_drug-title__"


def _norm(text: str) -> str:
    """压掉连续空白（页面里用大量 ``&nbsp;`` 对齐）。"""
    return re.sub(r"[ \t\u00a0]+", " ", text).strip()


def _cls(attrs) -> str:
    return dict(attrs).get("class") or ""


class _BaseParser(HTMLParser):
    """带 depth 追踪与"块级换行"的解析器基类。

    子类只管在 :meth:`_data` 里收文本、在 :meth:`_start` / :meth:`_end`
    里认自己的 class。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        #: 在表格单元格里的层数（格内一律不换行，保证"一行表格行 = 一行文本"）
        self._in_cell = 0
        #: 当前表格行已经开了几格（用来决定要不要发分隔符）
        self._cell_in_row = 0

    # 子类可覆写
    def _data(self, data: str) -> None:  # noqa: D102
        pass

    def _start(self, tag: str, attrs) -> None:  # noqa: D102
        pass

    def _end(self, tag: str) -> None:  # noqa: D102
        pass

    def handle_starttag(self, tag, attrs):
        if tag in CELL_TAGS:
            # 第 2 格起先发分隔符——**空单元格也照发**，
            # 这样"这格原文就是空"与"这格被解析丢了"能区分开
            if self._cell_in_row:
                self._data(CELL_SEP)
            self._cell_in_row += 1
            self._in_cell += 1
        elif tag in ROW_TAGS:
            self._cell_in_row = 0
        elif self._in_cell:
            # 格内：<br> 换成不破行的分号，其余块级标签一律不发换行
            if tag == "br":
                self._data(CELL_BR)
        elif tag in BLOCK_TAGS:
            self._data("\n")
        self._start(tag, attrs)
        if tag not in VOID_TAGS:
            self.depth += 1

    def handle_startendtag(self, tag, attrs):
        # <br/>、<img/> 这类自闭合标签：算一次 start，不算一次 end
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        if tag in CELL_TAGS:
            self._in_cell -= 1
        elif tag in ROW_TAGS:
            self._data("\n")
        elif self._in_cell:
            pass                      # 格内块级标签不换行
        elif tag in BLOCK_TAGS:
            self._data("\n")
        self.depth -= 1
        self._end(tag)

    def handle_data(self, data):
        self._data(data)


class _InsertParser(_BaseParser):
    """按 DOM 结构抽详情页字段（标准库实现，不引新依赖）。

    跟踪两个"层级"：

    * ``page_name__`` 的 ``h2``      → 小节名
    * ``page_content__`` 的 ``div``  → 该小节正文（可含嵌套 div，靠 depth 判结束）

    ``sections`` 是**普通 dict**，插入顺序即 HTML 里的文档顺序——重解析时
    就按这个顺序写 txt，不再排序、不再过滤。
    """

    def __init__(self) -> None:
        super().__init__()
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

    def _data(self, data: str) -> None:
        if self._name_level is not None:
            self._name_buf.append(data)
        elif self._content_level is not None:
            self._content_buf.append(data)
        elif self._in_date:
            self.date_parts.append(data)

    def _start(self, tag: str, attrs) -> None:
        cls = _cls(attrs)
        if tag == "h2" and cls.startswith(_PAGE_NAME_CLS):
            self._name_level = self.depth
            self._name_buf = []
        elif tag == "div" and cls.startswith(_PAGE_CONTENT_CLS):
            self._content_level = self.depth
            self._content_buf = []
            self._content_gated = False
        elif tag == "p" and cls.startswith(_PAGE_DATE_CLS):
            self._in_date = True
        elif tag == "a" and cls.startswith(_PAGE_LOGIN_CLS):
            # 登录墙就长在小节正文里：<a class="page_login__x">登录</a>
            self._content_gated = True

    def _end(self, tag: str) -> None:
        if self._name_level is not None and self.depth == self._name_level:
            name = _norm("".join(self._name_buf))
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


class _SearchParser(_BaseParser):
    """抽搜索结果页里的说明书**候选条目**（同名不同厂的都收下）。"""

    def __init__(self) -> None:
        super().__init__()
        self.items: list[dict] = []
        self._cur: dict | None = None
        self._item_level: int | None = None
        self._name_level: int | None = None
        self._meta_level: int | None = None
        self._name_buf: list[str] = []
        self._meta_buf: list[str] = []

    def _data(self, data: str) -> None:
        if self._name_level is not None and self._item_level is not None:
            self._name_buf.append(data)
        elif self._meta_level is not None and self._item_level is not None:
            self._meta_buf.append(data)

    def _start(self, tag: str, attrs) -> None:
        cls = _cls(attrs)
        if tag == "li" and cls.startswith(_ITEM_CLS):
            self._item_level = self.depth
            self._cur = {"href": "", "name": "", "manufacturer": "",
                         "ingredient": ""}
            self._name_buf, self._meta_buf = [], []
        elif self._cur is not None and tag == "a" and not self._cur["href"]:
            href = dict(attrs).get("href") or ""
            # 飞行数据里的链接是 /drug/，真 HTML 里是 /pc/drug/——只认后者
            if "/pc/drug/" in href:
                self._cur["href"] = href
        elif tag == "h3" and cls.startswith(_NAME_CLS):
            self._name_level = self.depth
            self._name_buf = []
        elif tag == "p" and cls.startswith(_META_CLS):
            self._meta_level = self.depth
            self._meta_buf = []

    def _end(self, tag: str) -> None:
        if self._name_level is not None and self.depth == self._name_level:
            if self._cur is not None:
                self._cur["name"] = _norm("".join(self._name_buf))
            self._name_level = None
        elif self._meta_level is not None and self.depth == self._meta_level:
            if self._cur is not None:
                name, ing = _split_meta(_norm("".join(self._meta_buf)))
                self._cur["manufacturer"] = name
                self._cur["ingredient"] = ing
            self._meta_level = None
        if self._item_level is not None and self.depth == self._item_level:
            if self._cur is not None and self._cur["name"]:
                title, spec = _split_spec(self._cur["name"])
                self.items.append({
                    "name": self._cur["name"],
                    "title": title,
                    "spec": spec,
                    "manufacturer": self._cur["manufacturer"],
                    "ingredient": self._cur["ingredient"],
                    "href": self._cur["href"],
                    "url": _abs_url(self._cur["href"]),
                })
            self._cur = None
            self._item_level = None


def _abs_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    return BASE + ("" if href.startswith("/") else "/") + href


def search_url(kw: str) -> str:
    """该药在搜索结果页的 URL。

    采集失败或"无条目"时用它留证——**这张清单要能说明我们查过哪里**。
    确定性重建，所以重解析不必从别处搬运。
    """
    return SEARCH_URL.format(kw=urllib.parse.quote(kw))


def _split_meta(meta: str) -> tuple[str, str]:
    """把 ``厂商：X   成分：Y`` 拆成 ``(X, Y)``。拆不到就留空。"""
    man = ing = ""
    m = re.search(r"厂商[：:](.*?)(?=成分[：:]|$)", meta, re.S)
    if m:
        man = m.group(1).strip()
    m = re.search(r"成分[：:](.*)$", meta, re.S)
    if m:
        ing = m.group(1).strip()
    return man, ing


def _split_spec(name: str) -> tuple[str, str]:
    """从 ``瑞福（乌帕替尼缓释片） 15mg；30mg；45mg`` 里切出 ``(品名, 规格)``。

    规格是**原文里内嵌的**（站点没给单独字段），所以这里只把结尾那段
    带数字的剂量文字切出来；切不出来就留空。``name`` 始终保留完整原文，
    即使这个启发式猜错也不会丢信息。
    """
    i = max(name.rfind("）"), name.rfind(")"))
    if i >= 0:
        tail = name[i + 1:].strip()
        if tail and re.search(r"\d", tail):
            return name[:i + 1].strip(), tail
    return name.strip(), ""


def parse_search(html: str) -> list[dict]:
    """从搜索结果页取出说明书候选条目（按页面显示顺序）。

    返回的每一项含 ``name / title / spec / manufacturer / ingredient /
    href / url``。**降级行为**：一个 ``li`` 都没解析到时，回退到"抓第一个
    ``/pc/drug/`` 链接"，保证采集不至于因为站点改版而完全停摆。
    """
    p = _SearchParser()
    p.feed(html)
    if p.items:
        return p.items

    # 兜底：拿不到结构化条目就退回到最朴素的链接匹配
    out, seen = [], set()
    for m in re.finditer(r'href="(/pc/drug/[^"]+)"', html):
        href = m.group(1)
        if href in seen:
            continue
        seen.add(href)
        out.append({"name": "", "title": "", "spec": "", "manufacturer": "",
                    "ingredient": "", "href": href, "url": _abs_url(href)})
    return out


def parse_detail(html: str) -> dict:
    """从详情页取出结构化字段与各小节正文。

    为什么要按 DOM 结构解析而不是按文本行猜：页面长这样
    （class 后缀是 CSS-module 哈希，会随它重新部署而变，所以只匹配
    ``page_xxx__`` 前缀）——

    .. code-block:: html

        <p class="page_drug-date__x">
          <span>核准日期：<!-- -->2020年5月7日</span><span>|</span>
          <span>修改日期：<!-- -->2023年8月1日</span></p>
        <div class="page_item__x">
          <h2 class="page_name__x"><span>成份</span></h2>
          <div class="page_content__x"><div class="ck-content">…正文…</div></div>
        </div>

    早先按文本行猜的版本把「核准日期」和「修改日期」连成了一串、厂家取成
    ``None``，就是因为没利用这层结构。
    """
    p = _InsertParser()
    p.feed(html)

    # 药名：不假定标签名（实测是 <p>，但别赌），class 后缀是哈希只匹配前缀
    m = re.search(
        r'<(\w+)[^>]*class="' + _PAGE_TITLE_CLS + r'[^"]*"[^>]*>(.*?)</\1>',
        html, re.S)
    title = _norm(re.sub(r"<[^>]+>", "", m.group(2))) if m else ""

    dates = [d.strip() for d in
             "".join(p.date_parts).replace("|", "\n").split("\n") if d.strip()]
    approval = next((d for d in dates if "核准日期" in d), "")
    revision = next((d for d in dates if "修改日期" in d), "")
    approval = re.sub(r"^核准日期[：:]\s*", "", approval)
    revision = re.sub(r"^修改日期[：:]\s*", "", revision)

    secs = {k: v for k, v in p.sections.items() if v}
    return {
        "title": title,
        "核准日期": approval,
        "修改日期": revision,
        "上市许可持有人": secs.get("上市许可持有人", ""),
        "生产企业": secs.get("生产企业", ""),
        "truncated": p.truncated,
        "sections": secs,
        # 老版本会把这些节整节丢掉——现在丢不掉，但要让"出现了新节名"可见
        "unknown_sections": [k for k in secs if k not in KNOWN_SECTIONS],
    }


def render_text(info: dict, *, url: str, collected_at: str) -> str:
    """把解析结果渲染成 ``text/<药名>.txt``。

    格式（供 L4 按 ``## `` 切小节）：

    .. code-block:: text

        # 瑞福（乌帕替尼缓释片）
        来源URL: https://drugs.dxy.cn/pc/drug/…
        采集日期: 2026-10-12
        核准日期: 2022年2月18日
        修改日期: 2025年5月27日
        上市许可持有人: AbbVie Deutschland GmbH & Co. KG
        生产企业: AbbVie Ireland NL B.V.
        被截断的小节: （无）

        ## 成份
        …

    **小节按 HTML 文档顺序写**，不排序、不按白名单过滤。
    """
    body = [
        f"# {info['title']}",
        f"来源URL: {url}",
        f"采集日期: {collected_at}",
        f"核准日期: {info['核准日期']}",
        f"修改日期: {info['修改日期']}",
        f"上市许可持有人: {info['上市许可持有人']}",
        f"生产企业: {info['生产企业']}",
        f"被截断的小节: {'、'.join(sorted(set(info['truncated']))) or '（无）'}",
        "",
    ]
    for name, text in info["sections"].items():
        body.append(f"## {name}")
        body.append(text)
        body.append("")
    return "\n".join(body)


def read_text_header(path: Path) -> dict:
    """从已存在的 txt 里读回 ``来源URL`` / ``采集日期``。

    重解析要保住采集当时的事实（URL、采集日期）——它们不在 raw HTML 里，
    只在 txt 头部。丢了就得重采，而重采会改变样本。
    """
    out = {"url": "", "collected_at": "", "title": ""}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").split("\n")[:12]:
        if line.startswith("# "):
            out["title"] = line[2:].strip()
        elif line.startswith("来源URL:"):
            out["url"] = line[len("来源URL:"):].strip()
        elif line.startswith("采集日期:"):
            out["collected_at"] = line[len("采集日期:"):].strip()
    return out


def load_targets(selected_only: bool = True) -> list[dict]:
    """读 L3 第①步的药名单。``selected_only`` 只取 ``selected == 1`` 的。"""
    if not DRUGS_FILE.exists():
        raise FileNotFoundError(
            f"找不到 {DRUGS_FILE}——请先运行：python scripts/select_high_risk_drugs.py"
        )
    with DRUGS_FILE.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if selected_only:
        rows = [r for r in rows if r.get("selected") == "1"]
    return rows


def text_sections(text: str) -> list[str]:
    """按出现顺序取 txt 里的 ``## `` 小节名。"""
    return [ln[3:].strip() for ln in text.split("\n") if ln.startswith("## ")]


def split_sections(text: str) -> dict[str, str]:
    """把 txt 拆回 ``{小节名: 正文}``，顺序同文件。

    与 :func:`render_text` 是一对逆操作。L4 **按小节送 LLM**（决策 #30 / Q3）
    就靠它取【禁忌】【注意事项】【儿童用药】三节。
    """
    out: dict[str, str] = {}
    cur: str | None = None
    buf: list[str] = []
    for ln in text.split("\n"):
        if ln.startswith("## "):
            if cur is not None:
                out[cur] = "\n".join(buf).strip("\n")
            cur = ln[3:].strip()
            buf = []
        elif cur is not None:
            buf.append(ln)
    if cur is not None:
        out[cur] = "\n".join(buf).strip("\n")
    return out


def text_stats(text: str) -> tuple[int, int]:
    """返回 ``(小节数, 正文行数)``。

    正文行**不含**头部元信息、也不含 ``## `` 标题行——只数真正承载说明书
    内容的行。新旧产物的"压缩倍数"就是按这个口径比的。
    """
    parts = text.split("\n## ")
    if len(parts) == 1:
        return 0, 0
    lines = 0
    for chunk in parts[1:]:
        body = chunk.split("\n", 1)[1] if "\n" in chunk else ""
        lines += len([ln for ln in body.split("\n") if ln.strip()])
    return len(parts) - 1, lines


#: index.csv 的列（重解析与采集共用，保证两处产物一致）
INDEX_FIELDS = [
    "rank", "cn_name", "search_keyword", "en_names", "Major", "status",
    "title", "url", "collected_at", "approval_date", "revision_date",
    "holder", "manufacturer", "section_count", "sections",
    "complete_sections", "truncated_sections", "unknown_sections",
    "candidate_count",
]

#: candidates.csv 的列。
#:
#: ⚠ ``maker`` 是**搜索结果页原样显示的"厂商"**，**不等于**详情页的【生产企业】：
#: 实测多拉司琼的 search 厂商 = 辽宁海思科 = 详情页的【上市许可持有人】，
#: 而详情页另有【生产企业】= 四川美大康佳乐；特立氟胺同样（盛世泰科 vs 南京海纳）。
#: 所以两个 CSV 的"厂家"列**不是同一个概念**，绝不能互相核对。
#: 详情页的持有人/生产企业分别记在 ``index.csv`` 的 ``holder`` / ``manufacturer``。
CANDIDATE_FIELDS = [
    "search_keyword", "idx", "selected", "title", "spec",
    "maker", "ingredient", "url",
]


def read_index(path: Path | None = None) -> list[dict]:
    """读回 ``index.csv``。**采集脚本用它做合并**，不要再整份覆写。

    断点续采曾经直接覆写清单，而那轮只处理剩下的药，于是清单丢了 8 个
    已采药——这就是"元数据与实际产物不一致"的另一种形态。
    """
    path = path or INDEX_FILE
    if not Path(path).exists():
        return []
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def read_candidates(path: Path | None = None) -> list[dict]:
    """读回 ``candidates.csv``（候选条目清单）。"""
    path = path or CANDIDATE_FILE
    if not Path(path).exists():
        return []
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_index(rows: list[dict], path: Path | None = None) -> Path:
    path = path or INDEX_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=INDEX_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


def write_candidates(rows: list[dict], path: Path | None = None) -> Path:
    path = path or CANDIDATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CANDIDATE_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


# ── 合成用例：把三处解析缺陷各钉一条，不依赖网络 ──────────────────────
_SAMPLE_DETAIL = """<html><body>
<p class="page_drug-date__x"><span>核准日期：2020年1月1日</span><span>|</span>
<span>修改日期：2021年2月2日</span></p>
<p class="page_drug-title__x">示例药片</p>
<div class="page_item__x"><h2 class="page_name__x"><span>禁忌</span></h2>
<div class="page_content__x"><div class="ck-content"><ul>
<li>第一条禁忌</li><li>第二条禁忌</li></ul></div></div></div>
<div class="page_item__x"><h2 class="page_name__x"><span>自定义节</span></h2>
<div class="page_content__x"><div class="ck-content"><div>甲段<br>乙段<p class="new-line"></p>丙段</div></div></div></div>
<div class="page_item__x"><h2 class="page_name__x"><span>末节示例</span></h2>
<div class="page_content__x"><div class="ck-content"><div>末节</div>
<table><thead><tr><th>项目</th><th>措施</th><th>指南</th></tr></thead>
<tbody><tr><td>甲</td><td>停药<br/>复查</td><td>第 12 周</td></tr>
<tr><td>乙</td><td>暂停</td><td></td></tr></tbody></table></div></div></div>
</body></html>"""

#: 注意 ``<script>`` 里那份是**转义过的飞行数据**（``\"`` 而非 ``"``，
#: 且链接是 ``/drug/`` 而非 ``/pc/drug/``），候选解析必须不把它算进去。
_SAMPLE_SEARCH = """<html><body>
<div class="Main_section__x"><div class="Main_title__x">说明书</div><ul>
<li class="DrugsItem_drugs-item__x"><a class="DrugsItem_link__x" href="/pc/drug/AAA?ky=x">
<h3 class="DrugsItem_drugs-item-name__x"><span>甲（示例片） 10mg</span></h3>
<p class="DrugsItem_drugs-item-cnName__x">厂商：<span>甲厂</span>   成分：<span>示例甲</span></p>
</a></li>
<li class="DrugsItem_drugs-item__x"><a class="DrugsItem_link__x" href="/pc/drug/BBB?ky=x">
<h3 class="DrugsItem_drugs-item-name__x"><span>乙（示例胶囊）</span></h3>
<p class="DrugsItem_drugs-item-cnName__x">厂商：<span>乙厂</span>   成分：<span>示例乙</span></p>
</a></li></ul></div>
<script>self.__next_f.push([1,"\\"className\\":\\"DrugsItem_drugs-item__x\\",\\"href\\":\\"/drug/CCC\\""])</script>
</body></html>"""


def _selftest() -> int:
    """合成用例回归测试——三处解析缺陷各有对应用例钉住。"""
    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}\n        实际 {got!r}\n        期望 {want!r}")

    info = parse_detail(_SAMPLE_DETAIL)
    check("① 列表项各自成行（旧版焊成一行）",
          info["sections"]["禁忌"], "第一条禁忌\n第二条禁忌")
    check("② 空 <p class=new-line> 与 <br> 都要断行",
          info["sections"]["自定义节"], "甲段\n乙段\n丙段")
    check("③ <br> 非自闭合也不让 depth 漂移",
          list(info["sections"]), ["禁忌", "自定义节", "末节示例"])
    check("④ 未知节名照样写出，只被报告、不被丢弃",
          info["unknown_sections"], ["自定义节", "末节示例"])
    check("   核准/修改日期未粘连",
          (info["核准日期"], info["修改日期"]), ("2020年1月1日", "2021年2月2日"))
    check("   药名", info["title"], "示例药片")

    items = parse_search(_SAMPLE_SEARCH)
    check("⑤ 候选数（飞行数据那份不算）", len(items), 2)
    check("   候选一 品名", items[0]["title"], "甲（示例片）")
    check("   候选一 规格", items[0]["spec"], "10mg")
    check("   候选一 厂家", items[0]["manufacturer"], "甲厂")
    check("   候选一 成分", items[0]["ingredient"], "示例甲")
    check("   候选一 URL", items[0]["url"], BASE + "/pc/drug/AAA?ky=x")
    check("   候选二 无规格则不硬拆", items[1]["spec"], "")
    check("   降级兜底：认不出条目也要抓到链接",
          len(parse_search('<a href="/pc/drug/ZZZ">x</a>')), 1)

    tbl = info["sections"]["末节示例"]
    tbl_lines = tbl.split("\n")
    check("⑥ 表格：一行表格行 = 一行文本（不被 td/br 拆开）", len(tbl_lines), 4)
    check("   表格：格内 <br> 换成不破行的分号",
          "甲 | 停药；复查 | 第 12 周" in tbl, True)
    check("   表格：空单元格留下分隔符（原文空 ≠ 解析丢了）",
          tbl_lines[-1], "乙 | 暂停 |")

    # render_text 与 split_sections 是一对逆操作——L4 按小节送 LLM 靠它
    rt = split_sections(render_text(info, url="u", collected_at="d"))
    check("⑦ render_text ↔ split_sections 可逆",
          rt["禁忌"], "第一条禁忌\n第二条禁忌")
    check("   拆出的小节与原文同序",
          list(rt), ["禁忌", "自定义节", "末节示例"])

    print("  合成用例回归测试")
    if fails:
        for f in fails:
            print(f"    ✗ {f}")
        print(f"    {len(fails)} 项未通过")
        return 1
    print("    ✓ 19 项全过（四处解析缺陷 + 一对逆操作各有对应用例钉住）")
    return 0


if __name__ == "__main__":
    import sys

    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    print("说明书读取层 · 自检")
    print()
    if _selftest():
        raise SystemExit(1)
    print()
    files = sorted(RAW_DIR.glob("*.html"))
    print(f"  raw/*.html      {len(files)}")
    total_secs = total_lines = 0
    unknown: dict[str, int] = {}
    rows = []
    for p in files:
        info = parse_detail(p.read_text(encoding="utf-8"))
        n_secs, n_lines = text_stats(render_text(info, url="", collected_at=""))
        total_secs += n_secs
        total_lines += n_lines
        for k in info["unknown_sections"]:
            unknown[k] = unknown.get(k, 0) + 1
        rows.append((n_lines, n_secs, p.stem))

    print(f"  小节总数        {total_secs}")
    print(f"  正文总行数      {total_lines}")
    print(f"  每份平均        {total_lines / max(1, len(files)):.1f} 行 / "
          f"{total_secs / max(1, len(files)):.1f} 节")
    print()
    if unknown:
        print("  ⚠ 出现了 KNOWN_SECTIONS 里没有的节名：")
        for k, v in sorted(unknown.items(), key=lambda kv: -kv[1]):
            print(f"      {v:>3} 份  {k}")
    else:
        print("  ✓ 所有节名都在 KNOWN_SECTIONS 里")
    print()
    rows.sort()
    print("  行数最少的 3 份：")
    for n, s, name in rows[:3]:
        print(f"      {n:>5} 行 / {s:>2} 节  {name}")
    print("  行数最多的 3 份：")
    for n, s, name in rows[-3:]:
        print(f"      {n:>5} 行 / {s:>2} 节  {name}")
    print()
    probe = RAW_DIR / "乌帕达西替尼.html"
    if probe.exists():
        info = parse_detail(probe.read_text(encoding="utf-8"))
        print("  样例 · 乌帕达西替尼【禁忌】：")
        for ln in info["sections"].get("禁忌", "").split("\n"):
            print(f"      {ln}")
