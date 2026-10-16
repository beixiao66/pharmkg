"""认证：bcrypt 存密码 + JWT 发令牌（基线决策 #24）。

**为什么不引 passlib**（决策 #24 已写明，这里再记一次，因为它是个很常见的坑）：
passlib 1.7.4 会去读 ``bcrypt.__about__.__version__``，而那个私有属性在
bcrypt 4.x 里被删了 → 直接抛异常。绕过它要装旧版 bcrypt 或打补丁。
而"加盐哈希 + 校验"本来就只有三行，用 ``bcrypt`` 原库最省事、也最少依赖。

**bcrypt 的两个硬限制**（都不是可配置项，撞上会直接抛异常）：

1. 只取密码**前 72 字节**，超出的部分被静默丢弃。所以先自己
   ``sha256`` 预散列成定长再交给它——同时也顺手解决了"超长输入"。
2. 盐已经包含在哈希串里（``$2b$12$<22字符盐><31字符摘要>``），
   所以**不需要单独存 salt 列**。
"""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from app.config import get_settings

#: bcrypt 的算法标识；显式写出来，换算法时这个字符串要一起改。
BCRYPT_PREFIX = "$2b$"


def _prehash(password: str) -> bytes:
    """把任意长度密码压成 44 字节的 base64。

    直接上 bcrypt 的话，超过 72 字节的部分会被**静默截断**——
    两个前 72 字节相同的不同密码会互相通过校验，这是真实的安全缺陷，不是洁癖。
    """
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    """生成 bcrypt 哈希（自带盐）。"""
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """校验密码。

    ⚠️ **哈希串格式不对时返回 False，而不是抛异常**：
    数据库里躺着一行坏数据（手工改过、或早期版本写的），
    不该让"登录"这个接口 500；而返回 False 的效果就是那个人登不上、别人不受影响。
    """
    try:
        return bcrypt.checkpw(_prehash(password), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


def create_access_token(user_id: int, username: str) -> str:
    """签发 JWT。``sub`` 放 user_id（字符串，JWT 规范要求）。"""
    settings = get_settings()
    if not settings.jwt_configured:
        raise RuntimeError(
            "JWT_SECRET 未设置——拒绝签发令牌。"
            "请在 .env 里填一个随机串（例如 python -c \"import secrets;"
            "print(secrets.token_hex(32))\"）。"
            "**绝不用默认密钥**：那等于任何人都能自己签发合法令牌。"
        )
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "username": username,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_expire_minutes))
                   .timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret,
                      algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any] | None:
    """解出载荷；无效（过期 / 签名不对 / 格式错）返回 ``None``。

    同样**不抛异常**：调用方只需要知道"这个令牌能不能用"。
    """
    settings = get_settings()
    if not settings.jwt_configured:
        return None
    try:
        return jwt.decode(token, settings.jwt_secret,
                          algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None
