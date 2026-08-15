"""Password hashing and JWT issuance/verification.

- Passwords: argon2id (memory-hard; the current recommendation for new systems).
- Tokens: short-lived access token + revocable refresh token. The refresh token's
  jti is stored in Redis so logout is real rather than cosmetic.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import Settings
from app.core.errors import AuthenticationError

_hasher = PasswordHasher()

TokenType = Literal["access", "refresh"]
REFRESH_DENYLIST_PREFIX = "auth:revoked_refresh:"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def _create_token(
    settings: Settings,
    subject: str,
    token_type: TokenType,
    ttl: timedelta,
    extra: dict[str, Any] | None = None,
) -> tuple[str, str, datetime]:
    now = datetime.now(UTC)
    expires_at = now + ttl
    jti = str(uuid.uuid4())
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": settings.APP_NAME,
    }
    if extra:
        payload.update(extra)
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, jti, expires_at


def create_access_token(
    settings: Settings, subject: str, *, role: str
) -> tuple[str, str, datetime]:
    return _create_token(
        settings,
        subject,
        "access",
        timedelta(minutes=settings.ACCESS_TOKEN_TTL_MINUTES),
        {"role": role},
    )


def create_refresh_token(settings: Settings, subject: str) -> tuple[str, str, datetime]:
    return _create_token(
        settings, subject, "refresh", timedelta(days=settings.REFRESH_TOKEN_TTL_DAYS)
    )


def decode_token(settings: Settings, token: str, *, expected_type: TokenType) -> dict[str, Any]:
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            issuer=settings.APP_NAME,
            options={"require": ["exp", "iat", "sub", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("Invalid token") from exc

    if payload.get("type") != expected_type:
        raise AuthenticationError(f"Expected a {expected_type} token")
    return payload
