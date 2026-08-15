from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Request, status

from app.api.deps import CurrentUser, RedisDep, SessionDep, SettingsDep, require_role
from app.models.platform import AuditLog
from app.models.user import User
from app.schemas.auth import LoginRequest, RefreshRequest, TokenPair, UserOut
from app.schemas.envelope import Envelope, envelope
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


async def _audit(
    session: SessionDep, request: Request, *, action: str, result: str, user_id: int | None
) -> None:
    from app.core.logging import request_id_var

    session.add(
        AuditLog(
            user_id=user_id,
            action=action,
            resource="auth",
            request_id=request_id_var.get(),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent", "")[:300] or None,
            result=result,
        )
    )


@router.post("/login", response_model=Envelope[TokenPair])
async def login(
    payload: LoginRequest,
    request: Request,
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
) -> Envelope[TokenPair]:
    service = AuthService(session, redis, settings)
    try:
        user = await service.authenticate(payload.email, payload.password)
    except Exception:
        await _audit(session, request, action="login", result="DENIED", user_id=None)
        await session.commit()
        raise
    tokens = await service.issue_tokens(user)
    await _audit(session, request, action="login", result="SUCCESS", user_id=user.id)
    return envelope(tokens, source=["SELF"])


@router.post("/refresh", response_model=Envelope[TokenPair])
async def refresh(
    payload: RefreshRequest,
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
) -> Envelope[TokenPair]:
    tokens = await AuthService(session, redis, settings).refresh(payload.refresh_token)
    return envelope(tokens, source=["SELF"])


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    payload: RefreshRequest,
    request: Request,
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
    user: CurrentUser,
) -> None:
    await AuthService(session, redis, settings).logout(payload.refresh_token)
    await _audit(session, request, action="logout", result="SUCCESS", user_id=user.id)


@router.get("/me", response_model=Envelope[UserOut])
async def me(user: CurrentUser) -> Envelope[UserOut]:
    return envelope(UserOut.model_validate(user), source=["SELF"])


@router.get(
    "/admin-only",
    response_model=Envelope[UserOut],
    summary="RBAC smoke test — proves the protected-route mechanism works",
)
async def admin_only(user: Annotated[User, require_role("admin")]) -> Envelope[UserOut]:
    return envelope(UserOut.model_validate(user), source=["SELF"])
