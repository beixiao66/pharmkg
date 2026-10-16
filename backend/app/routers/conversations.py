"""会话与消息接口——**L5 落库就靠这一组**（§6.1 第 ⑥ 步）。

L5 的每一轮要落五样东西：原问题、改写后的问题、Cypher、证据、回答。
其中"回答"是 ``role=assistant`` 那条消息的 ``content``，
其余三项挂在同一个 ``MessageCreate`` 上。

⚠️ **所有查询都带 ``user_id`` 条件**，不做"先按 id 取、再比 owner"。
后者只要有一条路径忘了比，就是越权读别人的会话；前者从 SQL 层就不可能取错。
查不到时返回 404 而不是 403——403 等于告诉对方"这个 id 是存在的"。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import AppUser, Conversation, Message
from app.schemas import (ConversationCreate, ConversationResponse,
                         MessageCreate, MessageResponse)

router = APIRouter(prefix="/conversations", tags=["会话"])

#: 会话标题取首条问题的前 N 字（§6.2：不必额外调 LLM）。
TITLE_CHARS = 20


def _get_owned(db: Session, user: AppUser, conv_id: int) -> Conversation:
    conv = db.execute(
        select(Conversation).where(Conversation.id == conv_id,
                                   Conversation.user_id == user.id)
    ).scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="会话不存在")
    return conv


def _to_response(db: Session, conv: Conversation) -> ConversationResponse:
    n = db.execute(select(func.count(Message.id))
                   .where(Message.conversation_id == conv.id)).scalar() or 0
    return ConversationResponse(id=conv.id, title=conv.title,
                                created_at=conv.created_at,
                                updated_at=conv.updated_at, message_count=n)


@router.post("", response_model=ConversationResponse,
             status_code=status.HTTP_201_CREATED, summary="新建会话")
def create_conversation(body: ConversationCreate,
                        user: AppUser = Depends(get_current_user),
                        db: Session = Depends(get_db)) -> ConversationResponse:
    conv = Conversation(user_id=user.id, title=body.title)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return _to_response(db, conv)


@router.get("", response_model=list[ConversationResponse],
            summary="我的会话列表（按最近活跃）")
def list_conversations(limit: int = Query(default=50, ge=1, le=200),
                       user: AppUser = Depends(get_current_user),
                       db: Session = Depends(get_db)) -> list[ConversationResponse]:
    rows = db.execute(
        select(Conversation).where(Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc()).limit(limit)
    ).scalars().all()
    return [_to_response(db, c) for c in rows]


@router.get("/{conv_id}/messages", response_model=list[MessageResponse],
            summary="取会话消息（默认最近 3 轮，§6.2 的上下文窗口）")
def list_messages(conv_id: int,
                  limit: int = Query(default=6, ge=1, le=200),
                  user: AppUser = Depends(get_current_user),
                  db: Session = Depends(get_db)) -> list[MessageResponse]:
    """取消息。

    ⚠️ 默认 ``limit=6``（= 3 轮，一问一答）。L5 的上下文窗口只要最近 N 轮，
    一次拉全部历史既费 token 又没有用（§6.2 建议 N=3）。

    返回的是**最近** N 条，但按**时间正序**排列——模型看到的顺序必须是对话顺序，
    倒着喂进去会让它把最新的一轮当成最早的一轮。
    """
    conv = _get_owned(db, user, conv_id)
    rows = db.execute(
        select(Message).where(Message.conversation_id == conv.id)
        .order_by(Message.id.desc()).limit(limit)
    ).scalars().all()
    return [MessageResponse.model_validate(m) for m in reversed(rows)]


@router.post("/{conv_id}/messages", response_model=MessageResponse,
             status_code=status.HTTP_201_CREATED, summary="追加一条消息（L5 落库）")
def append_message(conv_id: int, body: MessageCreate,
                   user: AppUser = Depends(get_current_user),
                   db: Session = Depends(get_db)) -> MessageResponse:
    """追加一条消息，并刷新会话的 ``updated_at``。

    ``updated_at`` 必须跟着动：会话列表是**按最近活跃**排的，
    不回写的话用户刚聊过的会话会沉到底下。
    """
    conv = _get_owned(db, user, conv_id)

    msg = Message(conversation_id=conv.id, role=body.role, content=body.content,
                  rewritten_query=body.rewritten_query, cypher=body.cypher,
                  evidence=body.evidence)
    db.add(msg)

    # 首条用户消息到达时补标题——放在这里而不是创建会话时，
    # 是因为"会话名"应该来自用户真正问的第一句话，不是占位串。
    if body.role == "user" and not conv.title.strip():
        conv.title = body.content.strip()[:TITLE_CHARS]

    db.commit()
    db.refresh(msg)
    return MessageResponse.model_validate(msg)
