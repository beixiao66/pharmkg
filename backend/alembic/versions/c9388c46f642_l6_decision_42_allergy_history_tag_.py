"""L6 decision 42: allergy_history + 标签词表改名

Revision ID: c9388c46f642
Revises: 2c3ae8532931
Create Date: 2026-10-17 15:05:04.663169

## 这个迁移做两件事

1. **加 `app_user.allergy_history`** —— 把"过敏史"从"人群"里拆出来（决策 #42）。
2. **改老数据的值** —— 决策 #41 时期 `population_tags` 里存的是**图节点名**
   （`孕妇`/`哺乳期女性`/`育龄女性`/`三环类药物过敏者`/…），而决策 #42 起
   存的是**产品标签名**（`妊娠期`/`哺乳期`/`育龄期`/`三环类药物过敏`/…）。

   ⚠️ **不改这一步，老用户已填的标签会静默失效**：界面显示一个标签、
   查图时却翻译不出图节点名，于是"标了也查不到"——安全功能里的漏报方向。
   这类"数据看起来还在、语义已经变了"的问题最难察觉，所以这里显式迁移。

3. 顺带把两个**过敏史**的值从 `population_tags` 挪到 `allergy_history`
   （它们本来就不是人群）。

## 为什么写成一条不带 WHERE 的 UPDATE

因为它必须**幂等**：用 CASE 把"新名"原样保留、把"旧名"翻成新名、其余一律丢弃。
带 WHERE 的写法只能处理"恰好含旧名"的行，含混合值的行会漏；
而幂等写法跑第二遍结果不变，回滚后重跑也安全。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c9388c46f642'
down_revision: Union[str, None] = '2c3ae8532931'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: 旧（图节点名）→ 新（产品标签名）
_POP_MAP = {"孕妇": "妊娠期", "哺乳期女性": "哺乳期", "育龄女性": "育龄期"}
_ALG_MAP = {"三环类药物过敏者": "三环类药物过敏",
            "大环内酯类药物过敏者": "大环内酯类药物过敏"}
#: 已经是新名的，原样保留（幂等）
_POP_KEEP = ("妊娠期", "哺乳期", "育龄期", "儿童", "婴幼儿", "老年",
             "肝功能不全", "肾功能不全")


def _sql_array_case(mapping: dict[str, str], keep: tuple[str, ...]) -> str:
    """生成 `array_agg(DISTINCT CASE ... END)` 片段，用来就地改写 text[]。"""
    whens = "".join(f" WHEN t = '{k}' THEN '{v}'" for k, v in mapping.items())
    keeps = "".join(f" WHEN t = '{k}' THEN '{k}'" for k in keep)
    return (
        "(SELECT array_agg(DISTINCT(CASE WHEN FALSE THEN ''" + whens + keeps +
        " ELSE NULL END)) FROM unnest(population_tags) AS t "
        "WHERE (CASE WHEN FALSE THEN ''" + whens + keeps +
        " ELSE NULL END) IS NOT NULL)"
    )


def upgrade() -> None:
    op.add_column('app_user',
                  sa.Column('allergy_history', sa.ARRAY(sa.Text()),
                            server_default='{}', nullable=False))

    # ① 过敏史：从 population_tags 里挑出来，翻成新名，写进 allergy_history
    alg_cases = "".join(f" WHEN t = '{k}' THEN '{v}'" for k, v in _ALG_MAP.items())
    op.execute(sa.text(
        "UPDATE app_user SET allergy_history = COALESCE(("
        "  SELECT array_agg(DISTINCT(CASE WHEN FALSE THEN ''" + alg_cases +
        "    ELSE NULL END))"
        "  FROM unnest(population_tags) AS t"
        "  WHERE (CASE WHEN FALSE THEN ''" + alg_cases +
        "    ELSE NULL END) IS NOT NULL), '{}')"))

    # ② population_tags：旧名翻新名、新名保留、过敏史的值丢掉（已挪走）
    op.execute(sa.text(
        "UPDATE app_user SET population_tags = COALESCE(" +
        _sql_array_case(_POP_MAP, _POP_KEEP) + ", '{}')"))


def downgrade() -> None:
    # ⚠️ 只把**列**删掉；值不回迁。
    #    理由：downgrade 把 `妊娠期` 翻回 `孕妇` 之后，若再 upgrade 一次，
    #    ② 那条把过敏史挑出来的语句会在已删列的情况下重跑——同一个数据被
    #    翻两次会丢信息。宁可让 downgrade 是"丢列"，用户在旧版本里重填。
    op.drop_column('app_user', 'allergy_history')
