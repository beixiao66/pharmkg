"""FastAPI 依赖：取当前登录用户、取数据库会话。"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser
from app.security import decode_access_token

#: ``auto_error=False``：**故意**不让 FastAPI 自己抛 403。
#: 它的默认行为是缺 Authorization 头时返回 **403**，而语义上"没带令牌"是
#: **401**（未认证）——403 是"认证了但没权限"。这两个码混用会让前端的
#: "跳登录页"逻辑失效（它按 401 判断）。
_bearer = HTTPBearer(auto_error=False)


def _unauthorized(detail: str = "未认证或令牌无效") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> AppUser:
    """从 Bearer 令牌解出用户。任何一步不成立都是 401，且**不泄露细节**。"""
    if credentials is None or not credentials.credentials:
        raise _unauthorized("缺少 Authorization: Bearer <token>")

    payload = decode_access_token(credentials.credentials)
    if not payload:
        raise _unauthorized("令牌无效或已过期")

    sub = payload.get("sub")
    if not sub:
        raise _unauthorized("令牌里没有 sub")

    user = db.get(AppUser, int(sub))
    if user is None:
        # 令牌签名合法但用户已被删——同样按未认证处理。
        raise _unauthorized("用户不存在")
    return user
