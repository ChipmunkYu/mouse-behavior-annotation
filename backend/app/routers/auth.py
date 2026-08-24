"""认证接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from ..auth import create_access_token, get_current_user, verify_password
from ..database import get_db
from ..models import User
from ..media_auth import MediaKeys, bearer_binding, encode_media_jwt
from ..schemas import LoginRequest, LoginResponse, UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(user)


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)) -> LoginResponse:
    user = db.query(User).filter(User.username == body.username).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    settings = request.app.state.settings
    token = create_access_token(user.id, settings)
    if settings.media_ticket_enabled:
        import jwt
        claims = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        _clear_binding_cookie(response, settings)
        _set_binding_cookie(response, settings, token, str(user.id), int(claims["exp"]))
    return LoginResponse(access_token=token, user=UserOut.model_validate(user))


def _clear_binding_cookie(response: Response, settings) -> None:
    response.set_cookie(
        settings.media_binding_cookie_name, "", max_age=0,
        path=settings.media_binding_cookie_path, secure=True, httponly=True, samesite="strict",
    )


def _set_binding_cookie(response: Response, settings, raw_token: str, sub: str, exp: int) -> None:
    from time import time
    keys = MediaKeys.from_settings(settings)
    now = int(time())
    binding = bearer_binding(raw_token, keys.raw_bearer)
    value = encode_media_jwt({
        "sub": sub, "binding": binding, "aud": settings.media_binding_audience,
        "typ": settings.media_binding_type, "iat": now, "exp": exp,
    }, keys.binding_jwt)
    response.set_cookie(
        settings.media_binding_cookie_name, value, max_age=max(0, exp - now),
        path=settings.media_binding_cookie_path, secure=True, httponly=True, samesite="strict",
    )


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response) -> Response:
    """无认证幂等清理；不读取 Bearer，也不执行数据操作。"""
    _clear_binding_cookie(response, request.app.state.settings)
    response.status_code = 204
    return response
