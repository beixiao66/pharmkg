"""用药安全校验（L6）：药箱、两两相互作用、人群禁忌。

放在路由里而不是单独一层，是因为这里的逻辑**全部是"取数 + 排序 + 组装"**，
没有独立于 HTTP 的复用点；抽一层反而多一次跳转。
真到要复用时再抽——但现在不预支。

## 为什么"无记录"也要显式输出

§6.3 要求输出「药物对 + 严重程度 + 来源」。但对**图谱里没有这条边**的药对，
最危险的写法是"不输出"——用户会把"没提到"读成"没问题"。
所以这里**每对都出一张卡**，没有边的写「图谱中无记录」，
并把**数据覆盖**一并返回（12 条人群边 / 150,522 条相互作用边都是实测数）。

## 人群标签的口径（决策 #41）

标签枚举**直接用图里那 5 个 `Population` 名**，不用 §5 原先计划的那 6 个人群。
原因：图里的 Population 是 L4 从说明书抽出来的**实际存在**的 5 个
（孕妇 / 哺乳期女性 / 育龄女性 / 三环类药物过敏者 / 大环内酯类药物过敏者）。
原计划的 `儿童 / 老年 / 肝功能不全 / 肾功能不全` **图里一个节点都没有**——
提供这样的标签，用户标了也永远查不出东西，**那是假的安全感**。
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException, Query, status
from neo4j import GraphDatabase
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app import population
from app.db import get_db
from app.deps import get_current_user
from app.models import AppUser, MedicineBox
from app.schemas import (DrugHit, DrugSearchResult, MedicineBoxCreate,
                         MedicineBoxItem, MedicineBoxRisk,
                         PopulationTagsUpdate, RiskCoverage, RiskPair,
                         RiskPopulation, TagCoverage, UserResponse)

# ⚠️ 拼音匹配模块在 `scripts/` 下（与 l5 的脚本同目录），backend 需要把它挂进 sys.path。
#    这里自己挂一次，不依赖 `l5_service` 先被导入——**不要靠别人的导入顺序**。
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from pinyin_match import PinyinIndex  # noqa: E402

router = APIRouter(tags=["用药安全校验（L6）"])

#: 药物拼音索引（进程级单例）。
#: 构建要扫一遍药物名：实测 5,666 个 / **0.4 秒**——每个请求重建一次会让检索慢到不可用，
#: 与 `l5_service` 里实体索引的做法保持一致。加锁是因为同步路由跑在线程池里，
#: 并发首请求会同时进来。
_PINYIN: dict[str, PinyinIndex | None] = {"index": None}
_PINYIN_LOCK = Lock()


def _pinyin_index() -> PinyinIndex:
    with _PINYIN_LOCK:
        if _PINYIN["index"] is None:
            drv = _driver()
            try:
                with drv.session() as s:
                    names = [r["name"] for r in s.run(
                        "MATCH (d:Drug) WHERE coalesce(d.quality,'ok') <> 'junk' "
                        "AND d.name IS NOT NULL RETURN d.name AS name")]
            finally:
                drv.close()
            _PINYIN["index"] = PinyinIndex(names)
        return _PINYIN["index"]

#: 严重程度排序：**`Unknown` 单独一档**（§6.3）——它不是"最轻"，是"不知道"，
#: 混进 Minor 会让用户以为风险低。
LEVEL_ORDER = {"Major": 0, "Moderate": 1, "Minor": 2, "Unknown": 3, None: 4}
LEVEL_LABEL = {"Major": "高风险", "Moderate": "中等", "Minor": "轻微",
               "Unknown": "严重程度未知", None: "未收录"}

DISCLAIMER = "以上结果来自知识图谱与公开数据集，仅供参考，不能替代医生或药师的判断。"

#: DDInter 的许可要求署名（§6.3 的"来源标注"字段）。
DDINTER_ATTRIBUTION = "药物相互作用数据来自 DDInter（CC BY-NC-SA 4.0）"

#: 药箱规模上限。N 个药 → N(N-1)/2 对；50 个药是 1225 对，已经够大。
MAX_BOX_SIZE = 50


def _driver():
    st = get_settings()
    return GraphDatabase.driver(st.neo4j_uri,
                                auth=(st.neo4j_user, st.neo4j_password))


def population_vocabulary(session) -> list[dict]:
    """合法标签枚举 = **产品标签名**，并带上每个标签在图里的覆盖度。

    ⚠️ 不再返回图里的 `Population` 名。图节点名是 L4 抽出来的副产品，
    不该直接当产品枚举（决策 #42）；映射集中在 `app/population.py`。
    """
    cov = population.coverage_of(session)
    order = {"population": 0, "allergy": 1}
    return [{"tag": t, **v} for t, v in
            sorted(cov.items(), key=lambda kv: (order[kv[1]["group"]], kv[0]))]


@router.get("/drugs/search", response_model=DrugSearchResult,
            summary="从已有 Drug 节点里检索（决策 #16：不往图谱加数据）")
def search_drugs(q: str = Query(min_length=1, max_length=32),
                 limit: int = Query(default=20, ge=1, le=50)) -> DrugSearchResult:
    """按名字**精确或前缀**检索。

    ⚠️ 只用 `STARTS WITH`，不用 `CONTAINS`：`CONTAINS` 会把「卡」匹到一长串
    名字里含这个字的复方名里，用户根本没法选。前缀检索的结果集小且可控。

    ⚠️ **必须把「总数」与「是否被截断」返回给前端。**
    实测：以「卡」开头的药物有 **29** 个，而 `卡维地洛片` 排第 **17** 位——
    `LIMIT 10` 把它切掉了，页面上却没有任何提示，用户很自然地读成
    「图谱里没有卡维地洛片」。**「被截断」必须说出来**，否则用户永远分不清
    「没有」和「没显示」。

    排序改成 **精确命中优先 → 短名 → 字典序**（原来是单纯的 `size(name), name`，
    即最短优先，本身没有意义，还把 5 个字的 `卡维地洛片` 挤出了前 10）。
    """
    order = ("ORDER BY CASE WHEN d.name = $q THEN 0 ELSE 1 END, "
             "size(d.name), d.name")
    drv = _driver()
    try:
        with drv.session() as s:
            # ⚠️ 排除 quality='junk' 的药名（见 scripts/flag_junk_nodes.py）。
            #    否则用户能检索到并加进药箱一个叫 `30R`/`SP`/`冻干` 的"药"，
            #    之后整条风险链路都在算一个不存在的东西。
            total = s.run("MATCH (d:Drug) WHERE (d.name = $q "
                          "OR d.name STARTS WITH $q) "
                          "AND coalesce(d.quality,'ok') <> 'junk' "
                          "RETURN count(d) AS c", q=q).single()["c"]
            rows = list(s.run(
                "MATCH (d:Drug) WHERE (d.name = $q OR d.name STARTS WITH $q) "
                "AND coalesce(d.quality,'ok') <> 'junk' "
                "RETURN d.id AS id, d.name AS name, d.name_en AS name_en "
                + order + " LIMIT $lim", q=q, lim=limit))

            # ── 字面查不到时，退一步按**读音**找 ─────────────────────────
            # 真实需求：「卡维地洛」打成「卡位低落」——读起来一样，三个字都不一样。
            # 字形匹配（精确/前缀）对此无能为力，而拼音键是**精确相等**的
            # （ka wei di luo），所以既救得回来，又不必放宽字面匹配的标准。
            #
            # ⚠️ 只在**字面完全没结果**时才走这条路：能在字面上找到的药，
            #    不该被同音候选混进来（否则搜「阿司匹林」会混进同音的其他药）。
            matched_by = None
            if total == 0:
                names, kind = _pinyin_index().match(q, limit=limit)
                if names:
                    rows = list(s.run(
                        "MATCH (d:Drug) WHERE d.name IN $names "
                        "AND coalesce(d.quality,'ok') <> 'junk' "
                        "RETURN d.id AS id, d.name AS name, d.name_en AS name_en "
                        + order + " LIMIT $lim", q=q, names=names, lim=limit))
                    total = len(rows)
                    matched_by = kind
        return DrugSearchResult(
            items=[DrugHit(id=r["id"], name=r["name"], name_en=r["name_en"])
                   for r in rows],
            total=total, limit=limit, truncated=total > len(rows),
            matched_by=matched_by)
    finally:
        drv.close()


@router.get("/medicine-box/population-vocabulary", response_model=list[TagCoverage],
            summary="合法标签枚举 + 每个标签在图谱里的覆盖度")
def get_population_vocabulary() -> list[TagCoverage]:
    """⚠️ **必须带覆盖度**：拆完字段后 `儿童`/`老年`/`肝功能不全`/`肾功能不全`
    在图里**一条边都没有**，前端要能如实显示"0 条"，而不是让用户以为查过了。"""
    drv = _driver()
    try:
        with drv.session() as s:
            return [TagCoverage(**v) for v in population_vocabulary(s)]
    finally:
        drv.close()


@router.patch("/auth/me/population-tags", response_model=UserResponse,
              summary="设置我的人群标签与过敏史")
def set_population_tags(body: PopulationTagsUpdate,
                        user: AppUser = Depends(get_current_user),
                        db: Session = Depends(get_db)) -> UserResponse:
    """校验后写入两个字段（决策 #42）。

    ⚠️ 校验用的是**产品标签名**（`妊娠期`/`哺乳期`/…），不是图节点名。
    拼错一律 422——标签拼错不会报错、只会**静默地什么都查不到**，
    在安全功能里那是漏报方向。宁可在这里把用户拦住。

    ⚠️ 人群与过敏史**分别校验**（两个不同词表），混填会被拒。
    """
    tags = population.normalize_tags(body.tags)
    allergies = population.normalize_tags(body.allergy_history)
    illegal = ([t for t in body.tags if t not in population.POPULATION_TAGS]
               + [t for t in body.allergy_history if t not in population.ALLERGY_TAGS])
    if illegal:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(f"不支持的标签 {illegal}；"
                    f"人群可用 {sorted(population.POPULATION_TAGS)}，"
                    f"过敏史可用 {sorted(population.ALLERGY_TAGS)}"))
    user.population_tags = tags
    user.allergy_history = allergies
    db.commit()
    db.refresh(user)
    return UserResponse(id=user.id, username=user.username,
                        population_tags=list(user.population_tags or []),
                        allergy_history=list(user.allergy_history or []),
                        created_at=user.created_at)


@router.get("/medicine-box", response_model=list[MedicineBoxItem],
            summary="我的药箱")
def list_box(user: AppUser = Depends(get_current_user),
             db: Session = Depends(get_db)) -> list[MedicineBoxItem]:
    rows = db.execute(select(MedicineBox).where(MedicineBox.user_id == user.id)
                      .order_by(MedicineBox.added_at)).scalars().all()
    return [MedicineBoxItem(drug_id=r.drug_id,
                            drug_name=r.drug_name_snapshot,
                            added_at=r.added_at) for r in rows]


@router.post("/medicine-box", response_model=MedicineBoxItem,
             status_code=status.HTTP_201_CREATED, summary="往药箱加一个药")
def add_to_box(body: MedicineBoxCreate,
               user: AppUser = Depends(get_current_user),
               db: Session = Depends(get_db)) -> MedicineBoxItem:
    """加药。

    ⚠️ **只接受图谱里已存在的 `Drug`**（决策 #16：前端从不往图谱加数据）。
    所以这里必须回图里核一遍 `drug_id`，并把**名字快照**存下来——
    图谱重建后用户仍然看得到自己加的是什么（决策 #25 的 `drug_name_snapshot`）。
    """
    drug_id = body.drug_id.strip()

    drv = _driver()
    try:
        with drv.session() as s:
            rec = s.run("MATCH (d:Drug {id: $x}) RETURN d.name AS n",
                        x=drug_id).single()
    finally:
        drv.close()
    if rec is None:
        raise HTTPException(status_code=404,
                            detail=f"图谱里没有 id={drug_id} 这个药物")

    n = db.execute(select(MedicineBox).where(MedicineBox.user_id == user.id)
                   ).scalars().all()
    if len(n) >= MAX_BOX_SIZE:
        raise HTTPException(status_code=422,
                            detail=f"药箱最多 {MAX_BOX_SIZE} 个药")

    item = MedicineBox(user_id=user.id, drug_id=drug_id,
                       drug_name_snapshot=rec["n"])
    db.add(item)
    try:
        db.commit()
    except IntegrityError:
        # UNIQUE(user_id, drug_id) —— 重复加药靠约束判，不靠"先查再插"
        db.rollback()
        raise HTTPException(status_code=409, detail=f"{rec['n']} 已经在药箱里了")
    db.refresh(item)
    return MedicineBoxItem(drug_id=item.drug_id,
                           drug_name=item.drug_name_snapshot,
                           added_at=item.added_at)


@router.delete("/medicine-box/{drug_id:path}",
               status_code=status.HTTP_204_NO_CONTENT, summary="从药箱移除")
def remove_from_box(drug_id: str,
                    user: AppUser = Depends(get_current_user),
                    db: Session = Depends(get_db)) -> None:
    res = db.execute(delete(MedicineBox).where(MedicineBox.user_id == user.id,
                                               MedicineBox.drug_id == drug_id))
    if not res.rowcount:
        db.rollback()
        raise HTTPException(status_code=404, detail="药箱里没有这个药")
    db.commit()
    return None


@router.get("/medicine-box/risk", response_model=MedicineBoxRisk,
            summary="用药安全校验：两两相互作用 + 人群禁忌")
def check_risk(user: AppUser = Depends(get_current_user),
               db: Session = Depends(get_db)) -> MedicineBoxRisk:
    """对药箱做两件事，并把**数据覆盖**一并返回。

    1. **两两组合**查 `INTERACTS_WITH`，按 Major > Moderate > Minor > Unknown 排序；
       **没有边的也出一张卡**，写「图谱中无记录」——不输出会被读成"没问题"。
    2. **人群禁忌**：把用户的人群标签与 `CONTRAINDICATED_FOR → Population` 求交。
       只取 `confidence='high'`（L4 的边实测约 33% 假阳性），
       并把"过滤掉了多少"也报出来。
    """
    st = get_settings()
    box = db.execute(select(MedicineBox).where(MedicineBox.user_id == user.id)
                     .order_by(MedicineBox.added_at)).scalars().all()
    ids = [b.drug_id for b in box]
    name_of = {b.drug_id: b.drug_name_snapshot for b in box}
    tags = list(user.population_tags or [])
    allergies = list(user.allergy_history or [])

    pairs: list[RiskPair] = []
    pop_hits: list[RiskPopulation] = []
    coverage_edges = pop_total = pop_high = 0

    if ids:
        drv = _driver()
        try:
            with drv.session() as s:
                # ── 两两相互作用：一次取回所有相关边，Python 侧配对 ──
                found: dict[tuple[str, str], dict] = {}
                for r in s.run(
                        "MATCH (a:Drug)-[e:INTERACTS_WITH]-(b:Drug) "
                        "WHERE a.id IN $ids AND b.id IN $ids "
                        "RETURN a.id AS a, b.id AS b, e.level AS lv, "
                        "e.source AS src", ids=ids):
                    key = tuple(sorted((r["a"], r["b"])))
                    found[key] = {"level": r["lv"], "source": r["src"]}
                coverage_edges = s.run(
                    "MATCH ()-[r:INTERACTS_WITH]->() RETURN count(r) AS c"
                ).single()["c"]

                for i in range(len(ids)):
                    for j in range(i + 1, len(ids)):
                        a, b = ids[i], ids[j]
                        hit = found.get(tuple(sorted((a, b))))
                        pairs.append(RiskPair(
                            drug_a=name_of[a], drug_b=name_of[b],
                            level=(hit or {}).get("level"),
                            level_label=LEVEL_LABEL[(hit or {}).get("level")],
                            source=(hit or {}).get("source"),
                            # ⚠️ 措辞从「图谱中无记录」改成「图谱未收录」，
                            #    并加一句 risk_hint。原来的说法 + 绿色标签
                            #    会被读成「可以一起吃」——而"没查到"和"没有"
                            #    在这里根本分不开（全图只有 15 万条 DDInter 边）。
                            note="" if hit else "图谱未收录这对组合的相互作用",
                            risk_hint="" if hit else
                            "图谱未收录 ≠ 可以同服。可能只是数据未覆盖，"
                            "请以说明书与医生/药师判断为准。"))
                pairs.sort(key=lambda p: (LEVEL_ORDER[p.level], p.drug_a))

                # ── 人群禁忌 ──
                pop_total = s.run(
                    "MATCH (:Drug)-[r:CONTRAINDICATED_FOR]->(:Population) "
                    "RETURN count(r) AS c").single()["c"]
                pop_high = s.run(
                    "MATCH (:Drug)-[r:CONTRAINDICATED_FOR]->(:Population) "
                    "WHERE r.confidence='high' RETURN count(r) AS c"
                ).single()["c"]
                # 产品标签 → 图节点名（决策 #42 的映射层）。
                # ⚠️ 直接把标签名拿去查图会**静默漏配**（`妊娠期` ≠ `孕妇`）。
                gnodes = population.graph_population_names(tags, allergies)
                node2tag = {}
                for tag, graphs in {**population.POPULATION_TAGS,
                                    **population.ALLERGY_TAGS}.items():
                    for g in graphs:
                        node2tag.setdefault(g, tag)
                if gnodes:
                    for r in s.run(
                            "MATCH (d:Drug)-[e:CONTRAINDICATED_FOR]->(p:Population) "
                            "WHERE d.id IN $ids AND p.name IN $nodes "
                            "AND e.confidence = 'high' "
                            "RETURN d.name AS drug, p.name AS gnode, "
                            "e.evidence AS ev", ids=ids, nodes=gnodes):
                        pop_hits.append(RiskPopulation(
                            drug=r["drug"],
                            # 回填成**产品标签名**（用户看到的应是 妊娠期，不是 孕妇）
                            tag=node2tag.get(r["gnode"], r["gnode"]),
                            graph_node=r["gnode"], evidence=r["ev"]))
        finally:
            drv.close()

    return MedicineBoxRisk(
        pairs=pairs, population=pop_hits,
        coverage=RiskCoverage(
            box_size=len(ids),
            pairs_checked=len(pairs),
            interaction_edges_total=coverage_edges,
            population_edges_total=pop_total,
            population_edges_high=pop_high,
            population_edges_used=len(pop_hits),
            confidence_filter="high",
            note=("人群禁忌只统计 `confidence='high'` 的边：L4 抽出的禁忌边实测约 33% 假阳性，"
                  f"而这类边全图只有 {pop_total} 条（其中 high {pop_high} 条）。"
                  "「没查到不等于没有」——覆盖不足时请以说明书与医生判断为准。"),
        ),
        population_tags=tags,
        allergy_history=allergies,
        disclaimer=DISCLAIMER,
        attribution=DDINTER_ATTRIBUTION,
    )
