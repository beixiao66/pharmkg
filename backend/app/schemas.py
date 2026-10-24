"""请求 / 响应模型（Pydantic v2）。

⚠️ **不用 ``EmailStr``**：它需要额外装 ``email-validator``，而本项目的用户名
就是普通登录名（不需要是邮箱）。引一个只为校验邮箱格式的依赖不划算。

**响应模型显式声明每一个字段**，而不是 ``model_config = {"from_attributes": True}``
直接从 ORM 对象反序列化。理由：``password_hash`` 就在同一张表上，
显式声明能保证**它永远不会**因为某次改动被顺手带进响应里。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class OrmResponse(BaseModel):
    """从 ORM 对象直接构造的响应模型的基类。

    ``from_attributes=True`` 让它能接 ORM 实例（而不是只接 dict）。

    ⚠️ 这不等于"随便什么字段都会被带出去"：**每个子类都显式声明了字段**，
    Pydantic 只读声明过的名字。所以 ``password_hash`` 这种列即使在同一张表上，
    也不会因为某次改动被顺手写进响应里。
    """

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- 认证

class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: int
    username: str


class UserResponse(OrmResponse):
    id: int
    username: str
    population_tags: list[str]
    allergy_history: list[str]
    created_at: datetime


# ---------------------------------------------------------------- 会话与消息

class ConversationCreate(BaseModel):
    #: 可以先不给；首条用户消息进来时会自动补上前 20 字（§6.2）。
    title: str = Field(default="", max_length=120)


class ConversationResponse(OrmResponse):
    id: int
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int = 0


class MessageCreate(BaseModel):
    """写入一轮消息。

    ``rewritten_query`` / ``cypher`` / ``evidence`` 只在 ``role="assistant"``
    或 L5 落库时才有值，所以都是可选的。
    """

    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1)
    rewritten_query: str | None = None
    cypher: str | None = None
    evidence: dict[str, Any] | None = None


class MessageResponse(OrmResponse):
    id: int
    conversation_id: int
    role: str
    content: str
    rewritten_query: str | None
    cypher: str | None
    evidence: dict[str, Any] | None
    created_at: datetime


# ---------------------------------------------------------------- 用药安全校验（L6）

class DrugHit(BaseModel):
    id: str
    name: str
    name_en: str | None = None


class DrugSearchResult(BaseModel):
    """检索结果**必须带总数与截断标记**。

    实测教训：以「卡」开头的药有 29 个，`卡维地洛片` 排第 17 位，被 `LIMIT 10`
    切掉且**毫无提示**，用户因此以为图谱里没有这个药。
    **分不清「没有」和「没显示」，是检索功能最要命的体验问题。**
    """

    items: list[DrugHit]
    total: int
    limit: int
    truncated: bool
    #: 字面查不到时按**读音**兜底的标记（None = 字面命中）。
    #: 值 ∈ {"pinyin", "pinyin-prefix"}。界面据此提示"已按读音找到"，
    #: 而不是让用户以为这是字面命中——**匹配方式也是结果的一部分**。
    matched_by: str | None = None


class PopulationTagsUpdate(BaseModel):
    """人群 + 过敏史（决策 #42 拆成两个字段）。

    合法值＝`app/population.py` 里的产品标签名（**不是图节点名**）；服务端会校验。
    图节点名 ↔ 标签名的映射集中在那一个模块里——否则 `哺乳期` 与图里的
    `哺乳期女性` 字符串不同就会**静默漏配**（安全功能里的漏报方向）。
    """

    tags: list[str] = Field(default_factory=list, max_length=10)
    allergy_history: list[str] = Field(default_factory=list, max_length=10)


class TagCoverage(BaseModel):
    """**单个标签的覆盖度**——0 也要说 0。

    图里指向人群的禁忌边一共只有 12 条（high 8）。拆完字段后
    `儿童`/`老年`/`肝功能不全`/`肾功能不全` 下面**一条边都没有**；
    若界面只说「标了就会查」，用户会得到"没查到风险"＝**假的安全感**。
    """

    tag: str
    group: str                  # population | allergy
    graph_nodes: list[str]      # 映射到图里的 Population 名（空＝未接入）
    edges: int                  # 该标签在图中 high 置信边的条数
    mapped: bool                # False＝图里暂无对应节点，**不是**"查过没风险"


class MedicineBoxCreate(BaseModel):
    drug_id: str = Field(min_length=1, max_length=160)


class MedicineBoxItem(OrmResponse):
    drug_id: str
    drug_name: str
    added_at: datetime


class RiskPair(BaseModel):
    drug_a: str
    drug_b: str
    level: str | None            # None = 图谱里没有这条边
    level_label: str
    source: str | None
    note: str = ""
    #: **无记录时必须给的提醒**：图谱未收录 ≠ 可以同服。
    #: 界面上这条不能用绿色（绿色＝安全），要中性色 + 明确这句。
    risk_hint: str = ""


class RiskPopulation(BaseModel):
    drug: str
    tag: str                    # 命中的**产品标签名**（如 妊娠期），不是图节点名
    graph_node: str | None = None   # 实际命中的图节点名（如 孕妇）
    evidence: str | None = None


class RiskCoverage(BaseModel):
    """**数据覆盖**。它不是装饰——没有它，"没查到"会被读成"没问题"。"""

    box_size: int
    pairs_checked: int
    interaction_edges_total: int
    population_edges_total: int
    population_edges_high: int
    population_edges_used: int
    confidence_filter: str
    note: str


class MedicineBoxRisk(BaseModel):
    pairs: list[RiskPair]
    population: list[RiskPopulation]
    coverage: RiskCoverage
    population_tags: list[str]
    allergy_history: list[str]
    disclaimer: str
    attribution: str


# ---------------------------------------------------------------- 问答（L5）

class ChatTurn(BaseModel):
    """一轮历史对话（与数据库里的形状一致：问答成对）。"""

    question: str
    answer: str = ""


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=200)
    #: 多轮历史，按时间正序。**决策 #39：有历史就一律改写**，不判断"是否含指代"。
    history: list[ChatTurn] = Field(default_factory=list, max_length=10)
    do_rewrite: bool = True


class AskResponse(BaseModel):
    """一轮问答的结果。

    刻意**不包含"答得对不对"**——那是评测（L9）用金标准判的，
    线上系统不该自评。这里只回答"答了什么、依据什么"。
    """

    question: str
    rewritten: str              # 多轮改写后的问题（单轮时与 question 相同）
    intent: str | None
    entities: list[str]
    step: str | None            # ok/澄清/无实体/白名单未过/执行失败/超条数上限
    answer: str
    cypher: str | None          # 实际执行的 Cypher（可解释性）
    row_count: int
    rows_preview: list          # 前 20 行；有的查询会返回上百行
    evidence: dict | None       # 证据子图（前端可视化用）
    evidence_consistent: bool | None
    faithful: bool | None       # 机械忠实度检查的结论
    out_of_evidence: list[str]


# ---------------------------------------------------------------- 医药查询

class DiseaseHit(BaseModel):
    name: str
    #: 该病在图里有多少条推荐用药（用来排序：用户要的是"能查出东西的病"）
    drug_count: int


class DiseaseDrugs(BaseModel):
    disease: str
    total: int
    drugs: list[dict]           # [{name, source}]
    truncated: bool
    disclaimer: str


class InteractionLookup(BaseModel):
    """两两相互作用直查。

    ⚠️ `found=False` 时**必须**同时给 `risk_hint`——
    "没查到"与"没有"在图谱里分不开（只收录了 15 万对 DDInter 边）。
    """

    drug_a: str
    drug_b: str
    found: bool
    level: str | None
    level_label: str
    source: str | None
    note: str
    risk_hint: str
    disclaimer: str
    attribution: str
    coverage_note: str


# ---------------------------------------------------------------- 图谱浏览

class GraphEntityHit(BaseModel):
    name: str
    label: str
    #: 度数（有多少条边）。按它降序——孤点点进去是空白页。
    degree: int


class GraphSubgraph(BaseModel):
    center: str
    center_label: str
    nodes: list[dict]           # [{id, label, type}]
    edges: list[dict]           # [{source, target, label, type}]
    #: ⚠️ 是否被截断。界面必须说出来，否则让人以为"图就长这样"。
    truncated: bool
    edge_limit: int


class GraphPath(BaseModel):
    src: str
    dst: str
    found: bool
    hops: int
    nodes: list[dict]
    edges: list[dict]
    note: str
