"""ORM 模型：会话管理最小版的三张表（基线 §6.2）。

**为什么是这三张、没有 ``medicine_box``**

§13.4 写的是"L7 会话管理（**最小版**）"，它排在 L5 前面的唯一理由是：
L5 要把每一轮的问题、改写、Cypher、回答落库，所以**数据表与写入接口必须先存在**。
药箱是 **L6** 的输入，现在建表等于为还没设计的链路写代码——需要时再加一张表，
反而更省事（决策 #38 之后本项目一律"先问再做，不做不可验证的产出"）。

**表名与列名为什么这么定**

* ``app_user`` 而不是 ``user``：后者是 SQL 保留字，不加引号会直接语法错误。
* ``population_tags`` 用 **PostgreSQL 原生数组**而不是逗号串：L6 要按标签做包含查询
  （``WHERE '孕妇' = ANY(population_tags)``），存字符串就得每次 ``LIKE``，
  既慢又容易把"孕期"误配成"孕妇"。
* ``message.evidence_json`` 用 **JSONB** 而不是 TEXT：证据子图是结构化数据，
  JSONB 能直接按字段查、能建索引；存字符串就只是块二进制。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (ARRAY, BigInteger, DateTime, ForeignKey, Index, String,
                        Text, UniqueConstraint, func)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class AppUser(Base):
    """用户。"""

    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    #: bcrypt 哈希**本身**就是 60 字符的字符串（``$2b$12$…``）。
    #: 留 255 是为了将来换算法（argon2 更长）时不用改表。
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    #: 孕妇 / 哺乳期 / 儿童 / 老年 / 肝功能不全 / 肾功能不全 …
    population_tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="user", cascade="all, delete-orphan")


class Conversation(Base):
    """一次会话（多轮对话的容器）。"""

    __tablename__ = "conversation"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False)
    #: 由首条问题自动生成（取前 20 字），不必额外调 LLM（§6.2）。
    title: Mapped[str] = mapped_column(String(120), nullable=False,
                                       server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    user: Mapped[AppUser] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan")

    __table_args__ = (
        # 列表页永远是"我的会话、按最近活跃排"——这条索引就是照它建的。
        Index("ix_conversation_user_updated", "user_id", "updated_at"),
    )


class Message(Base):
    """一轮消息。

    ``role`` 只有 ``user`` / ``assistant`` 两种（系统提示词不入库）。

    **三个"必须落库"的字段**——它们是 L9 评测的数据来源，缺一个都做不了消融：

    * ``rewritten_query``：改写后的自包含问题。多轮指代消解**是否成功**只能靠它判。
    * ``cypher``：实际执行的 Cypher。答错了要能分清是"生成错"还是"数据没有"。
    * ``evidence_json``：证据子图。"每条回答附证据"是本项目的核心设计，
      证据不留下来，事后就没法核对回答是不是**仅基于查询结果**。
    """

    __tablename__ = "message"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conversation.id", ondelete="CASCADE"),
        nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    rewritten_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    cypher: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: ORM 属性叫 ``evidence``、**数据库列仍叫 ``evidence_json``**（§6.2 的写法）。
    #: 这样代码里读的是自然名字，接口字段名也自然，而表结构与基线文档一致——
    #: 反过来（属性叫 evidence_json）会让每个响应模型都要写别名。
    evidence: Mapped[dict | None] = mapped_column(
        "evidence_json", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    __table_args__ = (
        # 取"最近 N 轮"（§6.2 建议 N=3，控制 token）走这条索引。
        Index("ix_message_conv_id", "conversation_id", "id"),
    )


__all__ = ["AppUser", "Conversation", "Message"]
