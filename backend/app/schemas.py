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


class PopulationTagsUpdate(BaseModel):
    #: 合法值＝图里实际存在的 `Population` 名（决策 #41）；服务端会校验。
    tags: list[str] = Field(default_factory=list, max_length=10)


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


class RiskPopulation(BaseModel):
    drug: str
    tag: str
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
    disclaimer: str
    attribution: str
