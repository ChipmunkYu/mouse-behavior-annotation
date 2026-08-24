"""认证：PBKDF2 密码哈希 + JWT Bearer 令牌。"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .database import get_db
from .models import User

PBKDF2_ITERATIONS = 200_000
ALGORITHM = "HS256"

_bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, expected = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError, AttributeError):
        return False


def create_access_token(user_id: int, settings: Settings | None = None) -> str:
    s = settings or get_settings()
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=s.access_token_expire_minutes)
    payload = {"sub": str(user_id), "iat": now, "exp": expire, "jti": secrets.token_urlsafe(24)}
    return jwt.encode(payload, s.secret_key, algorithm=ALGORITHM)


@dataclass(frozen=True)
class AuthContext:
    user: User
    raw_token: str
    claims: dict


def authenticate_token(raw_token: str, db: Session, settings: Settings) -> AuthContext:
    try:
        payload = jwt.decode(raw_token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user_id_raw = payload.get("sub")
    if user_id_raw is None or payload.get("exp") is None:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    try:
        user_id = int(user_id_raw)
        int(payload["exp"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid token payload")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User no longer exists")
    return AuthContext(user=user, raw_token=raw_token, claims=payload)


def get_auth_context(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> AuthContext:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return authenticate_token(credentials.credentials, db, request.app.state.settings)


def get_current_user(context: AuthContext = Depends(get_auth_context)) -> User:
    """保持普通路由既有的 User 依赖契约。"""
    return context.user
