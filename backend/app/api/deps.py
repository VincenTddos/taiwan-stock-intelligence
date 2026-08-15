"""FastAPI dependencies: settings, db session, redis, current user, RBAC."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import get_redis
from app.core.config import Settings, get_settings
from app.core.errors import AuthenticationError, PermissionDeniedError
from app.core.security import decode_token
from app.db.session import get_session
from app.models.user import ROLE_ORDER, User
from app.repositories.user_repo import UserRepository

bearer_scheme = HTTPBearer(auto_error=False)

SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_redis_dep(settings: SettingsDep) -> AsyncGenerator[aioredis.Redis, None]:
    yield get_redis(settings)


RedisDep = Annotated[aioredis.Redis, Depends(get_redis_dep)]


async def get_current_user(
    session: SessionDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Missing bearer token")

    payload = decode_token(settings, credentials.credentials, expected_type="access")
    user = await UserRepository(session).get(int(payload["sub"]))

    if user is None or not user.is_active or user.deleted_at is not None:
        raise AuthenticationError("User no longer active")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(minimum: str) -> object:
    """Role hierarchy: viewer < analyst < admin."""

    async def _guard(user: CurrentUser) -> User:
        if ROLE_ORDER.get(user.role, -1) < ROLE_ORDER[minimum]:
            raise PermissionDeniedError(f"Requires role '{minimum}' or higher")
        return user

    return Depends(_guard)
