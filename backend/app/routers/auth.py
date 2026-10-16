"""认证接口：注册与登录（决策 #24）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.models import AppUser
from app.schemas import (LoginRequest, RegisterRequest, TokenResponse,
                         UserResponse)
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["认证"])


def _token_response(user: AppUser) -> TokenResponse:
    settings = get_settings()
    return TokenResponse(
        access_token=create_access_token(user.id, user.username),
        expires_in=settings.jwt_expire_minutes * 60,
        user_id=user.id,
        username=user.username,
    )


@router.post("/register", response_model=TokenResponse,
             status_code=status.HTTP_201_CREATED, summary="注册并直接登录")
def register(body: RegisterRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """注册。

    ⚠️ **用户名重复靠数据库唯一约束判，不靠"先查再插"**：
    两个并发注册请求都能通过"查一下有没有"，然后一个插入成功、另一个撞约束。
    先查再插只是把竞态窗口缩小，并没有消除它。这里直接插，接住 ``IntegrityError``。
    """
    user = AppUser(username=body.username,
                   password_hash=hash_password(body.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"用户名 {body.username!r} 已存在")
    db.refresh(user)
    return _token_response(user)


@router.post("/login", response_model=TokenResponse, summary="登录")
def login(body: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """登录。

    ⚠️ 用户不存在与密码错误**返回同一句话**：分开提示等于送给攻击者一个
    "这个用户名存不存在"的探测接口。
    """
    user = db.execute(select(AppUser).where(AppUser.username == body.username)
                      ).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="用户名或密码不正确")
    return _token_response(user)


@router.get("/me", response_model=UserResponse, summary="当前登录用户")
def me(user: AppUser = Depends(get_current_user)) -> UserResponse:
    return UserResponse(id=user.id, username=user.username,
                        population_tags=list(user.population_tags or []),
                        created_at=user.created_at)
