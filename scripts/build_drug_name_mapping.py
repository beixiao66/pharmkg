#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第 0 步 · 中英药名映射实验（基线 §13.6）
========================================

回答一个问题：**DDInter 的 1,939 个英文药名，有多少能映射到 OpenCMKG 的
4,289 个中文药名？**

为什么先做这个：它是 L2（药物相互作用导入）的**唯一前置**，也是基线 §10 里
唯一还没答的高风险项。覆盖率数字直接决定"我的药箱"能不能用——用户输入的
药名认不出来，整个用药安全校验就是空转。

三个产出（基线 §13.6）
----------------------
1. **双口径覆盖率**——全量 1,939 与 Major 级 1,501。门槛卡在后者（§13.5）
2. **映射准确率**——分层抽 200 条供人工核对
3. **可直接复用的映射表**——L2 入库时直接读

用法
----
    python scripts/build_drug_name_mapping.py                  # 全量（首次会调 LLM）
    python scripts/build_drug_name_mapping.py --limit 40       # 小样本试跑
    python scripts/build_drug_name_mapping.py --no-llm         # 只用翻译缓存，不打 API
    python scripts/build_drug_name_mapping.py --retranslate    # 忽略缓存重译

翻译结果带缓存（``data/processed/ddinter_drug_cn.json``），**重跑不会重复花钱**。

匹配策略
--------
按基线决策 #23 的五级字符串匹配，**从高置信到低置信依次尝试，首个命中即采用**：

    ① 原样精确     译名与词表完全相同
    ② 别名表精确   命中的是 OpenCMKG 自带 SameAs 里的别名
    ③ 归一化精确   去剂型后缀 + 去括号后相同
    ④ 去盐归一化   再去掉盐/酯形式（盐酸、钠、钾…）后相同
    ⑤ 拼音相同    同音不同字      —— **待确认**
    ⑥ 编辑距离     形近（rapidfuzz >= 85）—— **待确认**

**⑤⑥ 标为「待确认」，不计入确定命中。** 这是本实验的实测结论，不是保守起见：
把 53 条编辑距离命中逐条判读，**约 20 条是错的**（柔红霉素→红霉素、
氯噻嗪→氢氯噻嗪、异环磷酰胺→环磷酰胺、凝血因子XIII→凝血因子VIII）。
中文药名常常只差一两个字却完全是两种药，编辑距离在医药场景天然不可靠。
按项目「不臆造」原则，这类命中只能作为**候选**推给用户确认（对应 §6.5
的"澄清反问"），不能静默当作映射结果。

**不用向量**。药名匹配是字符串与别名问题，不是语义问题——用户输入的是药名
（"阿莫西林胶囊"），不是描述（"治嗓子疼的药"）。详见基线 §6.5。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from rapidfuzz import fuzz, process
from pypinyin import lazy_pinyin

from opencmkg_io import (
    DDINTER_DIR,
    REPO_ROOT,
    RELATION_TYPES,
    SAME_AS_RELATION,
    SOURCE_TAG,
    load_entities_dict,
    load_triples,
)

# 与 L1 导入脚本一样：依赖缺失要说是解释器用错了，而不是抛 ModuleNotFoundError
sys.path.insert(0, str(REPO_ROOT / "backend"))
try:
    from app.config import get_settings
except ModuleNotFoundError as _exc:  # pragma: no cover
    _venv = REPO_ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
    print(f"✗ 缺少依赖 {_exc.name!r} —— 当前用的 Python 解释器不对。")
    print(f"  请改用：\"{_venv}\" scripts\\build_drug_name_mapping.py")
    raise SystemExit(1)

DDINTER_CODES = "ABDHLPRV"
PROCESSED_DIR = REPO_ROOT / "data" / "processed"

CACHE_FILE = PROCESSED_DIR / "ddinter_drug_cn.json"
MAPPING_FILE = PROCESSED_DIR / "drug_name_mapping.csv"
REVIEW_FILE = PROCESSED_DIR / "mapping_review_sample.csv"
REPORT_FILE = PROCESSED_DIR / "mapping_report.json"

#: 每次送多少药名给 LLM。太大容易输出截断，太小请求数多。
TRANSLATE_BATCH = 25
#: 编辑距离命中阈值（rapidfuzz 0–100）。低于这个数宁可判"未命中"。
FUZZY_THRESHOLD = 85

#: 提示词里给的对照答案，用于**校验提示词本身是否有效**。
#: 值写成可接受答案的集合——同一个药常有多个规范译名。
SPOT_CHECK: dict[str, set[str]] = {
    "Warfarin": {"华法林"},
    "Metformin": {"二甲双胍"},
    "Ibuprofen": {"布洛芬"},
    "Amoxicillin": {"阿莫西林"},
    "Paracetamol": {"对乙酰氨基酚", "扑热息痛"},
    "Acetylsalicylic acid": {"阿司匹林", "乙酰水杨酸"},
    "Clarithromycin": {"克拉霉素"},
    "Omeprazole": {"奥美拉唑"},
}

DOSAGE_SUFFIXES = sorted([
    "缓释胶囊", "缓释片", "控释片", "肠溶片", "分散片", "咀嚼片", "泡腾片", "含片",
    "薄膜衣片", "胶囊", "颗粒", "口服液", "口服溶液", "注射液", "注射剂", "滴眼液",
    "滴鼻液", "喷雾剂", "气雾剂", "软膏", "乳膏", "凝胶", "洗剂", "擦剂", "涂剂",
    "糖浆", "混悬液", "溶液", "丸", "散", "膏", "贴", "栓", "酊", "片", "膜",
], key=len, reverse=True)

SALT_PREFIXES = sorted([
    "盐酸", "硫酸", "枸橼酸", "马来酸", "富马酸", "酒石酸", "醋酸", "磷酸", "乳酸",
    "琥珀酸", "氢溴酸", "硝酸", "碳酸", "葡萄糖酸", "门冬氨酸", "双羟萘酸",
], key=len, reverse=True)

#: 盐/酯形式的**后缀**（华法林钠 → 华法林）。
#: 剥离后只有在词表里真能命中时才会被采用，所以"氯化钠 → 氯化"这类误剥
#: 不会造成错配——它只是查不到而已。
SALT_SUFFIXES = sorted([
    "盐酸盐", "硫酸盐", "磷酸盐", "酒石酸盐", "钠", "钾", "钙", "镁", "铵",
], key=len, reverse=True)


# ---------------------------------------------------------------- 归一化

def to_halfwidth(text: str) -> str:
    """全角转半角（词表里混着全角和半角括号、字母）。"""
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def normalize(name: str) -> str:
    """去括号内容、去空格、迭代剥离剂型后缀。"""
    s = to_halfwidth(name).strip()
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    s = re.sub(r"\s+", "", s)
    changed = True
    while changed:
        changed = False
        for suffix in DOSAGE_SUFFIXES:
            if s.endswith(suffix) and len(s) - len(suffix) >= 2:
                s = s[: -len(suffix)]
                changed = True
                break
    return s


def strip_salt(name: str) -> str:
    """去掉盐/酯形式——前缀（盐酸、枸橼酸…）与后缀（钠、钾、钙…）。

    例：``盐酸二甲双胍`` → ``二甲双胍``；``华法林钠`` → ``华法林``。

    两边都会走这一步，且结果只在**能命中词表时**才被采用，所以
    ``氯化钠`` → ``氯化`` 这种过度剥离不会造成错配。
    """
    for prefix in SALT_PREFIXES:
        if name.startswith(prefix) and len(name) - len(prefix) >= 2:
            name = name[len(prefix):]
            break
    for suffix in SALT_SUFFIXES:
        if name.endswith(suffix) and len(name) - len(suffix) >= 2:
            return name[: -len(suffix)]
    return name


def to_pinyin(name: str) -> str:
    return "".join(lazy_pinyin(name))


# ---------------------------------------------------------------- 数据

def load_ddinter_drugs() -> tuple[set[str], set[str], int]:
    """返回 (全部药名, Major 级涉及的药名, 原始记录数)。"""
    records: list[dict] = []
    for code in DDINTER_CODES:
        path = DDINTER_DIR / f"ddinter_downloads_code_{code}.csv"
        with path.open(encoding="utf-8", newline="") as f:
            records.extend(csv.DictReader(f))

    all_names: set[str] = set()
    major_names: set[str] = set()
    for r in records:
        all_names.add(r["Drug_A"])
        all_names.add(r["Drug_B"])
        if r["Level"] == "Major":
            major_names.add(r["Drug_A"])
            major_names.add(r["Drug_B"])
    return all_names, major_names, len(records)


def load_drug_vocab() -> tuple[list[str], dict[str, str]]:
    """返回 (OpenCMKG 中文药名列表, 别名表 alias → 规范名)。

    别名表来自 OpenCMKG 自带的 ``SameAs`` 关系里 **两端都是 drug** 的那些。
    实测这项只有个位数——SameAs 绝大多数是症状同义词，对药名映射几乎没帮助。
    这正是本实验要报告的一个发现（基线 §6.5 曾把 SameAs 列为别名表主要来源）。
    """
    entities, _ = load_entities_dict()
    vocab = list(entities["drug"])

    name_index = {n: t for t, names in entities.items() for n in names}
    triples, _ = load_triples()

    alias: dict[str, str] = {}
    for head, rel, tail in triples:
        if rel != SAME_AS_RELATION:
            continue
        if name_index.get(head) == "drug" and name_index.get(tail) == "drug":
            alias[head] = tail
    return vocab, alias


# ---------------------------------------------------------------- 翻译

SYSTEM_PROMPT = """你是药学数据工程师。把输入的英文药物名称翻译成**中文通用名**\
（中国药典 / 国家药监局使用的规范名称）。

规则：
1. 只给通用名，不带剂型、规格、商品名。例：Metformin → 二甲双胍（不是"盐酸二甲双胍片"）
2. 疫苗、抗原、生物制品按标准中文名翻译。例：Clostridium tetani toxoid antigen → 破伤风类毒素
3. 括号里的修饰要译成中文并保留。例：Miconazole (topical) → 咪康唑（外用）
4. 复方制剂保留"复方"字样，成分之间用"、"连接
5. 确实没有通行译名的（冷门化学品、植物提取物），按音译或意译给出最可能的中文名，**不要留空**

只输出 JSON，不要任何解释：
{"items": [{"en": "原文", "cn": "中文名"}, ...]}
顺序与输入一致，条数必须与输入相同。"""


def translate_batch(client, model: str, names: list[str]) -> dict[str, str]:
    """调一次 LLM 翻译一批药名。返回 {英文: 中文}。"""
    user = json.dumps(names, ensure_ascii=False)
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
        timeout=120,
    )
    try:
        resp = client.chat.completions.create(response_format={"type": "json_object"}, **kwargs)
    except Exception:
        # 部分模型/网关不支持 json_object，退回普通模式（提示词已要求只输出 JSON）
        resp = client.chat.completions.create(**kwargs)

    text = resp.choices[0].message.content or ""
    data = json.loads(text)
    items = data.get("items", data if isinstance(data, list) else [])
    out: dict[str, str] = {}
    for it in items:
        if isinstance(it, dict) and it.get("en") and it.get("cn"):
            out[str(it["en"])] = str(it["cn"]).strip()
    return out


def translate_all(names: list[str], cache: dict[str, str], *, use_llm: bool,
                  retranslate: bool, verbose: bool = True) -> dict[str, str]:
    """翻译缺失的名字，就地更新 cache。"""
    todo = names if retranslate else [n for n in names if n not in cache]
    if not todo:
        if verbose:
            log(f"  全部 {len(names):,} 个都已有译文，跳过 LLM")
        return cache
    if not use_llm:
        if verbose:
            log(f"  --no-llm：{len(todo):,} 个缺译文，跳过（覆盖率会偏低）")
        return cache

    settings = get_settings()
    if not settings.llm_configured:
        raise SystemExit("✗ 百炼未配齐（缺 DASHSCOPE_API_KEY / DASHSCOPE_BASE_URL），"
                         "跑 `python -m app.config` 看详情")

    from openai import OpenAI
    client = OpenAI(api_key=settings.dashscope_api_key,
                    base_url=settings.dashscope_base_url)

    batches = [todo[i:i + TRANSLATE_BATCH] for i in range(0, len(todo), TRANSLATE_BATCH)]
    log(f"  待翻译 {len(todo):,} 个，分 {len(batches)} 批（每批 {TRANSLATE_BATCH}）")
    log(f"  模型 {settings.llm_model}")

    t0 = time.time()
    failed = 0
    for i, batch in enumerate(batches, 1):
        try:
            got = translate_batch(client, settings.llm_model, batch)
        except Exception as exc:  # noqa: BLE001
            failed += len(batch)
            log(f"    批 {i}/{len(batches)} 失败：{type(exc).__name__}: {str(exc)[:80]}")
            continue
        cache.update(got)
        missing = [n for n in batch if n not in got]
        failed += len(missing)
        if verbose and (i % 10 == 0 or i == len(batches)):
            done = sum(len(b) for b in batches[:i])
            rate = done / max(0.001, time.time() - t0)
            log(f"    批 {i}/{len(batches)}  已处理 {done:,}  用时 {time.time() - t0:.0f}s"
                f"  ({rate:.1f} 个/秒)")

    log(f"  翻译完成：新增 {len(cache):,} 条，失败/缺失 {failed:,} 条，"
        f"用时 {time.time() - t0:.0f}s")
    return cache


# ---------------------------------------------------------------- 匹配

class Matcher:
    """五级字符串匹配器。词表侧预先归一化，查询时逐级降落。"""

    def __init__(self, vocab: list[str], alias: dict[str, str]):
        self.vocab = vocab
        self.alias = alias
        self.raw = set(vocab)
        self.by_norm: dict[str, str] = {}
        self.by_nosalt: dict[str, str] = {}
        self.by_pinyin: dict[str, str] = {}
        self.norm_list: list[str] = []
        for name in vocab:
            n = normalize(name)
            self.by_norm.setdefault(n, name)
            self.by_nosalt.setdefault(strip_salt(n), name)
            self.by_pinyin.setdefault(to_pinyin(n), name)
            self.norm_list.append(n)

    def match(self, cn: str) -> tuple[str | None, str, float]:
        """返回 (命中的词表名, 命中级别, 相似度)。未命中返回 (None, "未命中", 0)。"""
        if not cn:
            return None, "未命中", 0.0

        if cn in self.raw:                                   # ① 原样精确
            return cn, "原样精确", 100.0
        if cn in self.alias and self.alias[cn] in self.raw:   # ② 别名表
            return self.alias[cn], "别名表", 100.0

        n = normalize(cn)
        if n in self.by_norm:                                # ③ 归一化精确
            return self.by_norm[n], "归一化精确", 100.0

        ns = strip_salt(n)
        if ns in self.by_nosalt:                             # ④ 去盐归一化
            return self.by_nosalt[ns], "去盐归一化", 100.0

        py = to_pinyin(n)
        if py in self.by_pinyin:                             # ⑤ 拼音相同
            return self.by_pinyin[py], "拼音相同（待确认）", 100.0

        hit = process.extractOne(n, self.norm_list,           # ⑥ 编辑距离
                                 scorer=fuzz.ratio, score_cutoff=FUZZY_THRESHOLD)
        if hit:
            return hit[0], "编辑距离（待确认）", float(hit[1])
        return None, "未命中", 0.0


#: 归入"确定命中"的级别。⑤⑥ 是近似匹配，实测有明显假阳性，**只算候选**。
CONFIRMED_LEVELS = {"原样精确", "别名表", "归一化精确", "去盐归一化"}
#: 近似匹配——作为候选推给用户确认，不计入覆盖率分子。
TENTATIVE_LEVELS = {"拼音相同（待确认）", "编辑距离（待确认）"}


# ---------------------------------------------------------------- 输出

def log(msg: str = "") -> None:
    print(msg, flush=True)


def write_mapping(rows: list[dict]) -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    with MAPPING_FILE.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "en_name", "is_major", "cn_name", "matched_cn", "match_level", "score",
        ])
        writer.writeheader()
        writer.writerows(rows)


def write_review_sample(rows: list[dict], size: int) -> list[dict]:
    """分层抽样供人工核对。

    为什么不直接随机抽 200：随机抽样会把绝大部分名额给"原样精确"那种
    几乎不可能出错的层，而真正需要人眼看的恰恰是「拼音相同」「编辑距离」
    「未命中」这几层。所以**低置信层给保底名额，其余按比例分配**。

    **两个判定列是两回事，不能合并成一个**：

    ============  ============  ================================  ==============
    行类型        待填列        判定标准                          得到的指标
    ============  ============  ================================  ==============
    有匹配        映射对否      ``en_name`` 与 ``matched_cn``      **映射准确率**
                                是不是**同一种药**
    未命中        译名对否      ``cn_name`` 是不是 ``en_name``     **翻译准确率**
                                的正确中文名
    ============  ============  ================================  ==============

    ``译名对否`` 所有行都可以填（它在校验 LLM 翻译这一步）；
    ``映射对否`` 只在 ``matched_cn`` 非空时才有意义。

    例：
      - ``Bismuth subsalicylate`` / 水杨酸铋 → 水杨酸      ：映射**错**（不是同一种药）
      - ``Cefuroxime`` / 头孢呋辛 → 头孢呋辛酯             ：映射**对**（酯形式，同一种药）
      - ``Abacavir`` / 阿巴卡韦 →（未命中）                ：译名**对**，未命中是词表没有
    """
    by_level: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_level[r["match_level"]].append(r)

    FLOOR_LOW = 30          # 低置信层保底
    FLOOR_OTHER = 8         # 高置信层保底
    LOW_LEVELS = ("未命中", "编辑距离（待确认）", "拼音相同（待确认）")

    levels = list(by_level)
    alloc = {
        lv: min(FLOOR_LOW if lv in LOW_LEVELS else FLOOR_OTHER, len(by_level[lv]))
        for lv in levels
    }
    remaining = size - sum(alloc.values())
    if remaining > 0:
        total = sum(len(by_level[lv]) for lv in levels)
        for lv in levels:
            share = round(remaining * len(by_level[lv]) / max(1, total))
            alloc[lv] += min(share, len(by_level[lv]) - alloc[lv])

    sample: list[dict] = []
    for lv, n in alloc.items():
        bucket = by_level[lv]
        if n <= 0:
            continue
        step = max(1, len(bucket) // n)      # 等距抽样，覆盖整个列表而不是只取头部
        sample.extend(bucket[::step][:n])

    with REVIEW_FILE.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "en_name", "is_major", "cn_name", "matched_cn", "match_level", "score",
            "译名对否", "映射对否", "备注",
        ])
        writer.writeheader()
        for r in sample:
            writer.writerow({**r, "译名对否": "", "映射对否": "", "备注": ""})
    return sample


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="中英药名映射实验（第 0 步）")
    parser.add_argument("--limit", type=int, default=None,
                        help="只处理前 N 个药名（小样本试跑）")
    parser.add_argument("--no-llm", action="store_true", help="只用翻译缓存，不调 API")
    parser.add_argument("--retranslate", action="store_true", help="忽略缓存重新翻译")
    parser.add_argument("--sample-size", type=int, default=200, help="人工核对样本量")
    args = parser.parse_args(argv)

    t_start = time.time()
    log("第 0 步 · 中英药名映射实验")
    log("")

    log("── 读取分母 ──")
    all_names, major_names, n_records = load_ddinter_drugs()
    log(f"  DDInter 记录 {n_records:,} 条")
    log(f"  全量药名     {len(all_names):,}")
    log(f"  Major 级药名 {len(major_names):,}")
    log("")

    names = sorted(all_names)
    if args.limit:
        # 试跑时把提示词自检用的常见药也带上，否则看那几个药没被翻译会误判成提示词失效
        names = sorted(set(SPOT_CHECK) | set(names[: args.limit]))
        log(f"  ⚠ --limit {args.limit}：本次只处理 {len(names)} 个（分母同步缩小）")
        log("")

    log("── 读取 OpenCMKG 药物词表与别名表 ──")
    vocab, alias = load_drug_vocab()
    log(f"  中文药名 {len(vocab):,}")
    log(f"  SameAs 别名（两端都是 drug）{len(alias):,} 条")
    if len(alias) < 50:
        log("  ⚠ 别名表几乎为空——实测 OpenCMKG 的 SameAs 绝大多数是**症状**同义词，")
        log("    对药名映射帮助有限。这条与基线 §6.5 的假设不符，是个要记录的发现。")
    log("")

    log("── 翻译（LLM，带缓存）──")
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    cache: dict[str, str] = {}
    if CACHE_FILE.exists():
        cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        log(f"  缓存命中 {len(cache):,} 条  {CACHE_FILE.relative_to(REPO_ROOT)}")
    cache = translate_all(names, cache, use_llm=not args.no_llm,
                          retranslate=args.retranslate)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    log("")

    # 提示词自检：几个常见药必须译对，否则说明提示词有问题，后面全是白干
    log("── 提示词自检（常见药是否译对）──")
    spot_ok = 0
    for en, acceptable in SPOT_CHECK.items():
        got = cache.get(en)
        if got is None:
            continue
        hit = got in acceptable
        spot_ok += hit
        log(f"  {'✅' if hit else '❌'}  {en:<22} → {got}"
            f"{'' if hit else f'   （期望 {acceptable}）'}")
    log(f"  {spot_ok}/{len(SPOT_CHECK)} 命中")
    log("")

    log("── 匹配 ──")
    matcher = Matcher(vocab, alias)
    rows: list[dict] = []
    for en in names:
        cn = cache.get(en, "")
        matched, level, score = matcher.match(cn)
        rows.append({
            "en_name": en,
            "is_major": "1" if en in major_names else "0",
            "cn_name": cn,
            "matched_cn": matched or "",
            "match_level": level,
            "score": f"{score:.0f}",
        })
    log(f"  处理 {len(rows):,} 个药名")
    log("")

    # ── 覆盖率（双口径 × 三档置信）────────────────────────
    def tiers(subset: list[dict]) -> tuple[int, int, int]:
        confirmed = sum(1 for r in subset if r["match_level"] in CONFIRMED_LEVELS)
        tentative = sum(1 for r in subset if r["match_level"] in TENTATIVE_LEVELS)
        return confirmed, tentative, len(subset) - confirmed - tentative

    total_all = len(rows)
    major_rows = [r for r in rows if r["is_major"] == "1"]
    total_major = len(major_rows)
    no_cn = sum(1 for r in rows if not r["cn_name"])
    c_all, t_all, m_all = tiers(rows)
    c_maj, t_maj, m_maj = tiers(major_rows)

    log("── 覆盖率（基线 §13.5 的双口径）──────────────────────")
    log("  ⚠ 覆盖率只算「确定命中」；「待确认」是近似匹配，实测有明显假阳性，")
    log("    按「不臆造」原则不计入，只能作为候选推给用户确认。")
    log("")
    log(f"  {'口径':<10}{'确定命中':>16}{'待确认':>10}{'未命中':>10}")
    log(f"  {'全量':<10}{c_all:>6,} / {total_all:<5,} {c_all / max(1, total_all):>5.1%}"
        f"{t_all:>10,}{m_all:>10,}")
    log(f"  {'Major 级':<10}{c_maj:>6,} / {total_major:<5,} {c_maj / max(1, total_major):>5.1%}"
        f"{t_maj:>10,}{m_maj:>10,}")
    if no_cn:
        log(f"  （其中 {no_cn:,} 个没有译文，按未命中计）")
    log("")

    level_counts = Counter(r["match_level"] for r in rows)
    log("── 各级命中分布（全量）──")
    for lv, c in level_counts.most_common():
        log(f"  {lv:<12} {c:>6,}  ({c / max(1, total_all):5.1%})")
    log("")
    major_levels = Counter(r["match_level"] for r in major_rows)
    log("── 各级命中分布（Major 级）──")
    for lv, c in major_levels.most_common():
        log(f"  {lv:<12} {c:>6,}  ({c / max(1, total_major):5.1%})")
    log("")

    # 抽样看几眼——数字之外得知道**具体是哪些**没对上，否则没法判断
    # 到底是"药不在词表里"（正常）还是"匹配方法有问题"（要修）。
    for lv in ("未命中", "编辑距离（待确认）", "拼音相同（待确认）",
               "去盐归一化", "归一化精确"):
        bucket = [r for r in rows if r["match_level"] == lv]
        if not bucket:
            continue
        log(f"── {lv} 样例（共 {len(bucket):,} 条，取前 8）──")
        for r in bucket[:8]:
            arrow = f"→ {r['matched_cn']}" if r["matched_cn"] else "（无匹配）"
            score = f" {r['score']}" if "编辑距离" in lv else ""
            log(f"  {r['en_name']:<32} {r['cn_name']:<20} {arrow}{score}")
        log("")

    # ── 落盘 ────────────────────────────────────────────────
    write_mapping(rows)
    sample = write_review_sample(rows, args.sample_size)
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": "DDInter CSV + OpenCMKG entities_dict",
        "denominator": {"all": total_all, "major": total_major},
        # 只有「确定命中」计入覆盖率；「待确认」是近似候选（见脚本头部说明）
        "coverage": {
            "all": round(c_all / max(1, total_all), 4),
            "major": round(c_maj / max(1, total_major), 4),
        },
        "confirmed": {"all": c_all, "major": c_maj},
        "tentative": {"all": t_all, "major": t_maj},
        "missed": {"all": m_all, "major": m_maj},
        "no_translation": no_cn,
        "level_counts_all": dict(level_counts),
        "level_counts_major": dict(major_levels),
        "review_sample_size": len(sample),
        "fuzzy_threshold": FUZZY_THRESHOLD,
        "prompt_spot_check": f"{spot_ok}/{len(SPOT_CHECK)}",
    }
    REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")

    log("── 产出 ──")
    for p in (MAPPING_FILE, REVIEW_FILE, REPORT_FILE, CACHE_FILE):
        log(f"  {p.relative_to(REPO_ROOT)}")
    log("")
    log(f"完成，用时 {time.time() - t_start:.1f}s")
    log("")
    log(f"下一步：打开 {REVIEW_FILE.relative_to(REPO_ROOT)} 人工核对")
    log("        『译名对否』：cn_name 是不是 en_name 的正确中文名（所有行都填）")
    log("        『映射对否』：en_name 与 matched_cn 是不是同一种药（仅 matched_cn 非空时填）")
    log("        填 对 / 错 / 存疑")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
