from __future__ import annotations

from datetime import UTC, datetime

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AuthenticationError
from app.core.logging import get_logger
from app.core.security import (
    REFRESH_DENYLIST_PREFIX,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    needs_rehash,
    verify_password,
)
from app.models.user import User
from app.repositories.user_repo import UserRepository
from app.schemas.auth import TokenPair

log = get_logger(__name__)


class AuthService:
    def __init__(self, session: AsyncSession, redis: aioredis.Redis, settings: Settings) -> None:
        self.repo = UserRepository(session)
        self.redis = redis
        self.settings = settings

    async def authenticate(self, email: str, password: str) -> User:
        user = await self.repo.get_by_email(email)

        if user is None:
            # Hash anyway so a missing account and a wrong password take
            # comparable time; otherwise login latency enumerates users.
            hash_password(password)
            raise AuthenticationError("Invalid email or password")

        if not verify_password(password, user.password_hash):
            raise AuthenticationError("Invalid email or password")

        if not user.is_active:
            raise AuthenticationError("Account is disabled")

        if needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)

        await self.repo.touch_login(user)
        return user

    async def issue_tokens(self, user: User) -> TokenPair:
        access, _, expires_at = create_access_token(self.settings, str(user.id), role=user.role)
        refresh, _, _ = create_refresh_token(self.settings, str(user.id))
        return TokenPair(access_token=access, refresh_token=refresh, expires_at=expires_at)

    async def refresh(self, refresh_token: str) -> TokenPair:
        payload = decode_token(self.settings, refresh_token, expected_type="refresh")
        jti = str(payload["jti"])

        if await self.redis.exists(f"{REFRESH_DENYLIST_PREFIX}{jti}"):
            raise AuthenticationError("Refresh token has been revoked")

        user = await self.repo.get(int(payload["sub"]))
        if user is None or not user.is_active:
            raise AuthenticationError("User no longer active")

        # Rotate: the presented refresh token is burned on use, so a stolen
        # token is usable at most once and the theft becomes detectable.
        await self._revoke(jti, payload["exp"])
        return await self.issue_tokens(user)

    async def logout(self, refresh_token: str) -> None:
        try:
            payload = decode_token(self.settings, refresh_token, expected_type="refresh")
        except AuthenticationError:
            return  # already invalid; logout is idempotent
        await self._revoke(str(payload["jti"]), payload["exp"])

    async def _revoke(self, jti: str, exp: int) -> None:
        ttl = max(1, int(exp - datetime.now(UTC).timestamp()))
        await self.redis.setex(f"{REFRESH_DENYLIST_PREFIX}{jti}", ttl, "1")
