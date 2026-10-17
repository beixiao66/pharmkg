"""人群 / 过敏史标签的唯一事实来源（L6 · 决策 #42）。

## 为什么要有这一层

图里的 `Population` 节点名是 **L4 从 36 份说明书抽出来的副产品**
（`孕妇`/`哺乳期女性`/`育龄女性`/`三环类药物过敏者`/`大环内酯类药物过敏者`），
而**产品该给的标签是照临床需求设计的**。两者不该绑死：

* 产品要能标 `儿童` / `老年` / `肝功能不全`，哪怕图里**暂时没有**这些节点；
* 图里的节点将来可能改名，不该让用户已填的标签失效。

所以这里放一张**显式映射**：产品标签 → 图里的 `Population` 名（可能为空）。

⚠️ **没有这张映射就会静默漏配**：决策 #41 当时发现 `哺乳期` 与图里的
`哺乳期女性` 字符串不同，用户标了也永远匹配不上——**安全功能里的漏报方向**。
映射把这件事变成"写在一处、可审查"。

## 两个字段为什么要拆开

`三环类药物过敏者` **不是人群，是过敏史**。原来把它塞进 `population_tags`，
逼得界面只能叫「人群 / 过敏史」来打补丁。现在拆成：

* `app_user.population_tags` —— 妊娠期 / 哺乳期 / 儿童 / …
* `app_user.allergy_history` —— 三环类药物过敏 / 大环内酯类药物过敏

## 覆盖度为什么必须报出来

图里指向人群的禁忌边**一共只有 12 条**（high 8）。拆完之后 `儿童`/`老年`/…
这些标签下面**一条边都没有**。若界面只说"标了就会查"，
用户会得到"没查到风险"＝**假的安全感**。

所以每个标签都带上 `edges`（该标签在图谱里有多少条 high 置信边），
**0 就是 0，如实显示**——让"无数据"成为显式信息。
"""

from __future__ import annotations

#: 人群标签 → 图里的 `Population` 名（空元组＝图里暂无对应节点）。
POPULATION_TAGS: dict[str, tuple[str, ...]] = {
    "妊娠期": ("孕妇",),
    "哺乳期": ("哺乳期女性",),
    "育龄期": ("育龄女性",),
    "儿童": (),
    "婴幼儿": (),
    "老年": (),
    "肝功能不全": (),
    "肾功能不全": (),
}

#: 过敏史标签 → 图里的 `Population` 名。
ALLERGY_TAGS: dict[str, tuple[str, ...]] = {
    "三环类药物过敏": ("三环类药物过敏者",),
    "大环内酯类药物过敏": ("大环内酯类药物过敏者",),
}

#: 旧数据兼容：`app_user.population_tags` 里可能存着**图节点名**（决策 #41 时期写的）。
#: 迁移与读路径都要能把它翻成标签名，否则老用户的数据会静默失效。
LEGACY_GRAPH_NAME_TO_TAG = {
    "孕妇": "妊娠期",
    "哺乳期女性": "哺乳期",
    "育龄女性": "育龄期",
    "三环类药物过敏者": "三环类药物过敏",
    "大环内酯类药物过敏者": "大环内酯类药物过敏",
}


def normalize_tags(values) -> list[str]:
    """把任意来源的标签值规整成**标签名**，并去掉非法的。

    * 已经合法 → 原样保留；
    * 是旧的图节点名 → 翻成标签名（覆盖决策 #41 时期写入的数据）；
    * 都不认识 → 丢掉（**不静默保留垃圾值**）。
    """
    valid = set(POPULATION_TAGS) | set(ALLERGY_TAGS)
    out: list[str] = []
    for v in values or []:
        s = str(v).strip()
        s = LEGACY_GRAPH_NAME_TO_TAG.get(s, s)
        if s in valid and s not in out:
            out.append(s)
    return out


def graph_population_names(*tag_groups) -> list[str]:
    """把若干组标签翻译成**图里的 `Population` 名**（用于查边）。

    多个字段的标签合并去重——因为风险检查要把"人群"和"过敏史"一起查。
    """
    names: list[str] = []
    for tags in tag_groups:
        for t in tags or []:
            for g in POPULATION_TAGS.get(t, ()) or ALLERGY_TAGS.get(t, ()):
                if g not in names:
                    names.append(g)
    return names


def coverage_of(session) -> dict[str, dict]:
    """每个标签在**图谱里**有多少条 high 置信的禁忌边。

    ⚠️ 这是本模块最重要的输出：**0 也要说 0**。
    只报"能用的"会让用户以为那些标签查过了没问题。
    """
    # 一次把所有 Population 上的 high 边数拿回来，别一个标签一条查询
    counts: dict[str, int] = {}
    for r in session.run(
            "MATCH (:Drug)-[r:CONTRAINDICATED_FOR]->(p:Population) "
            "WHERE r.confidence = 'high' "
            "RETURN p.name AS n, count(r) AS c"):
        counts[r["n"]] = r["c"]

    out: dict[str, dict] = {}
    for group, table in (("population", POPULATION_TAGS),
                         ("allergy", ALLERGY_TAGS)):
        for tag, graphs in table.items():
            if graphs:
                n = sum(counts.get(g, 0) for g in graphs)
                out[tag] = {"group": group, "graph_nodes": list(graphs),
                            "edges": n, "mapped": True}
            else:
                # 图里暂无对应节点：边数 0，且**明确告诉前端这是"未接入"，不是"查过没风险"**
                out[tag] = {"group": group, "graph_nodes": [],
                            "edges": 0, "mapped": False}
    return out
