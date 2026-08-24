"""媒体票据密码学与严格原始 Cookie 解析（不得记录输入凭据）。"""
from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from typing import Iterable

import jwt

from .config import Settings

ALGORITHM = "HS256"
TICKET_KEY_LABEL = b"mouse-annotation/media/ticket-jwt/v1"
BINDING_KEY_LABEL = b"mouse-annotation/media/binding-jwt/v1"
RAW_BEARER_KEY_LABEL = b"mouse-annotation/media/raw-bearer-binding/v1"


def decode_master_secret(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != value:
        raise ValueError("media master secret is not canonical base64url")
    return decoded


def derive_key(master: bytes, label: bytes) -> bytes:
    return hmac.new(master, label, hashlib.sha256).digest()


@dataclass(frozen=True)
class MediaKeys:
    ticket: bytes
    binding_jwt: bytes
    raw_bearer: bytes

    @classmethod
    def from_settings(cls, settings: Settings) -> "MediaKeys":
        master = decode_master_secret(settings.media_master_secret)
        return cls(
            ticket=derive_key(master, TICKET_KEY_LABEL),
            binding_jwt=derive_key(master, BINDING_KEY_LABEL),
            raw_bearer=derive_key(master, RAW_BEARER_KEY_LABEL),
        )


def bearer_binding(raw_bearer: str, key: bytes) -> str:
    digest = hmac.new(key, raw_bearer.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def encode_media_jwt(payload: dict, key: bytes) -> str:
    return jwt.encode(payload, key, algorithm=ALGORITHM)


def decode_media_jwt(
    token: str, *, key: bytes, audience: str, expected_type: str, required: Iterable[str]
) -> dict:
    payload = jwt.decode(
        token,
        key,
        algorithms=[ALGORITHM],
        audience=audience,
        options={"require": list(required)},
    )
    if payload.get("aud") != audience or payload.get("typ") != expected_type:
        raise jwt.InvalidTokenError("invalid media token purpose")
    return payload


def raw_cookie_values(scope: dict, names: set[str]) -> dict[str, list[str]]:
    """保留所有 ASGI Cookie fields 及 field 内重复名，供 fail-closed 判定。"""
    found = {name: [] for name in names}
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() != b"cookie":
            continue
        value = raw_value.decode("latin-1")
        for part in value.split(";"):
            item = part.strip()
            if "=" in item:
                name, cookie_value = item.split("=", 1)
            else:
                name, cookie_value = item, ""
            if name in found:
                found[name].append(cookie_value)
    return found
