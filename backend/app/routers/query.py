"""医药查询接口：按病查推荐用药、两两查相互作用、疾病名检索。

## 为什么要单独一个路由而不是塞进 `/medicine-box`

药箱是"**我的清单**"（有状态、要持久化）；这里是"**查一下**"（无状态）。
两者的授权、缓存、错误语义都不同，混在一起会让药箱那个已经很长的路由继续膨胀。

## 三条和药箱一致的"诚实规则"，必须在接口层就守住

1. **垃圾药名要过滤**（`quality='junk'`）。否则"某病推荐用药"里会混进
   `冻干`/`Ⅰ`/`SP`/`8-11` 这类碎片——实测有 30 个这样的名字被当成推荐用药。
2. **无记录必须显式**：两两相互作用查不到时返回 `found: false` + `risk_hint`，
   **不能用"没返回"表示"没问题"**。图谱只收录了 15 万对 DDInter 边，
   "没查到"和"没有"在这里分不开。
3. **不返回"严重程度"以外的推断**：DDInter 批量文件只有 `Level` 一列，
   **没有机制/处理建议/替代用药**（那些在它网页端的配对详情页上）。
   接口层若编出这些字段，就是在替数据源撒谎。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from neo4j import GraphDatabase

from app.config import get_settings
from app.deps import get_current_user
from app.models import AppUser
#: 严重程度的中文标签与排序、免责声明、DDInter 署名——与药箱**共用一份**，
#: 避免两个地方各写一套然后慢慢漂移。
from app.routers.medicine_box import (DDINTER_ATTRIBUTION, DISCLAIMER,
                                      LEVEL_LABEL, LEVEL_ORDER)
from app.schemas import (DiseaseDrugs, DiseaseHit, InteractionLookup)

router = APIRouter(prefix="/query", tags=["医药查询"])

#: 垃圾药名的过滤条件（标记见 `scripts/flag_junk_nodes.py`）。
_NOT_JUNK = "coalesce(d.quality,'ok') <> 'junk'"


def _driver():
    st = get_settings()
    return GraphDatabase.driver(st.neo4j_uri,
                               auth=(st.neo4j_user, st.neo4j_password))


@router.get("/diseases", response_model=list[DiseaseHit],
            summary="疾病名检索（用于按病查药的候选列表）")
def search_diseases(q: str = Query(min_length=1, max_length=32),
                    limit: int = Query(default=20, ge=1, le=50),
                    user: AppUser = Depends(get_current_user)) -> list[DiseaseHit]:
    """按名字**精确或前缀**检索疾病，并按"推荐用药条数"降序。

    ⚠️ 排序用 `drugs`（该病在图里有多少条推荐用药）而不是名字长度：
    用户要的是"能查出东西的病"，**空壳疾病排在前面只是浪费点击**。
    """
    drv = _driver()
    try:
        with drv.session() as s:
            rows = list(s.run(
                "MATCH (dz:Disease) WHERE dz.name = $q OR dz.name STARTS WITH $q "
                "OPTIONAL MATCH (dz)-[r:RECOMMEND_DRUG]->(d:Drug) "
                f"WHERE {_NOT_JUNK} "
                "WITH dz, count(r) AS drugs "
                "RETURN dz.name AS name, drugs "
                "ORDER BY CASE WHEN dz.name = $q THEN 0 ELSE 1 END, "
                "drugs DESC, dz.name LIMIT $lim", q=q, lim=limit))
        return [DiseaseHit(name=r["name"], drug_count=r["drugs"]) for r in rows]
    finally:
        drv.close()


@router.get("/disease-drugs", response_model=DiseaseDrugs,
            summary="按病查推荐用药")
def disease_drugs(name: str = Query(min_length=1, max_length=64),
                  limit: int = Query(default=200, ge=1, le=500),
                  user: AppUser = Depends(get_current_user)) -> DiseaseDrugs:
    """返回某疾病的推荐用药。

    ⚠️ **这是 OpenCMKG 的 `RECOMMEND_DRUG` 关系**，不是临床指南。
    实测这条关系里有明显噪声（例如给"病毒性肠炎"推荐一堆抗生素、
    把**已停用**的去痛片列为 37 个疾病的推荐用药）。所以：
    * 返回值里带 `source` 与总数，让调用方知道覆盖面；
    * `disclaimer` 明确写"数据源为 OpenCMKG，不代表临床指南"。
    """
    drv = _driver()
    try:
        with drv.session() as s:
            exists = s.run("MATCH (dz:Disease {name: $n}) RETURN count(dz) AS c",
                           n=name).single()["c"]
            if not exists:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                    detail=f"图谱里没有疾病「{name}」")
            total = s.run(
                "MATCH (dz:Disease {name: $n})-[:RECOMMEND_DRUG]->(d:Drug) "
                f"WHERE {_NOT_JUNK} RETURN count(d) AS c", n=name).single()["c"]
            rows = list(s.run(
                "MATCH (dz:Disease {name: $n})-[r:RECOMMEND_DRUG]->(d:Drug) "
                f"WHERE {_NOT_JUNK} "
                "RETURN d.name AS name, coalesce(d.source,'') AS src "
                "ORDER BY d.name LIMIT $lim", n=name, lim=limit))
        return DiseaseDrugs(
            disease=name, total=total,
            drugs=[{"name": r["name"], "source": r["src"]} for r in rows],
            truncated=total > len(rows),
            disclaimer=("推荐用药来自 OpenCMKG 的『疾病-推荐药物』关系，"
                        "「不是临床指南」；该关系存在明显噪声（例如给病毒性肠炎"
                        "推荐抗生素、把已停用的药物仍列为推荐）。请以说明书与"
                        "医生判断为准。"))
    finally:
        drv.close()


@router.get("/interaction", response_model=InteractionLookup,
            summary="两两相互作用直查（给两味药名）")
def interaction(drug_a: str = Query(min_length=1, max_length=64),
                drug_b: str = Query(min_length=1, max_length=64),
                user: AppUser = Depends(get_current_user)) -> InteractionLookup:
    """查两味药之间的 `INTERACTS_WITH`。

    ⚠️ **查不到不等于可以同服。** 图谱只收录 DDInter 中能与中国药名对齐的部分
    （1,387 种药 / 150,522 对）。所以 `found: false` 时一定同时返回 `risk_hint`，
    并且严重程度标签是「未收录」而不是绿色对勾。
    """
    st = get_settings()
    drv = _driver()
    try:
        with drv.session() as s:
            miss = [x for x in (drug_a, drug_b)
                    if not s.run("MATCH (d:Drug {name: $n}) RETURN count(d) AS c",
                                 n=x).single()["c"]]
            if miss:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"图谱里没有药物 {miss}（可先用 /drugs/search 找名字）")
            row = s.run(
                "MATCH (a:Drug {name: $a})-[r:INTERACTS_WITH]-(b:Drug {name: $b}) "
                "RETURN r.level AS lv, r.source AS src LIMIT 1",
                a=drug_a, b=drug_b).single()
        if not row:
            return InteractionLookup(
                drug_a=drug_a, drug_b=drug_b, found=False,
                level=None, level_label=LEVEL_LABEL[None], source=None,
                note="图谱未收录这对组合的相互作用",
                risk_hint=("图谱未收录 ≠ 可以同服。可能只是数据未覆盖，"
                           "请以说明书与医生/药师判断为准。"),
                disclaimer=DISCLAIMER, attribution=DDINTER_ATTRIBUTION,
                coverage_note=("图谱收录 DDInter 中可与中国药名对齐的部分："
                               "1,387 种药物、150,522 对相互作用。"))
        lv = row["lv"]
        return InteractionLookup(
            drug_a=drug_a, drug_b=drug_b, found=True,
            level=lv, level_label=LEVEL_LABEL.get(lv, lv),
            source=row["src"], note="",
            risk_hint="" if lv in ("Major", "Moderate") else
            ("严重程度未知（Unknown）「不等于轻微」，请按最坏情况对待。"
             if lv == "Unknown" else ""),
            disclaimer=DISCLAIMER, attribution=DDINTER_ATTRIBUTION,
            coverage_note=("图谱收录 DDInter 中可与中国药名对齐的部分："
                           "1,387 种药物、150,522 对相互作用。"
                           "DDInter 的批量文件只有风险等级，"
                           "「机制/处理建议/替代用药不在其中」。"))
    finally:
        drv.close()
