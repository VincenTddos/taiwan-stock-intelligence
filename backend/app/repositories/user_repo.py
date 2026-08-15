from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from app.models.user import User
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(
            User.email == email.lower().strip(),
            User.deleted_at.is_(None),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def touch_login(self, user: User) -> None:
        user.last_login_at = datetime.now(UTC)
        await self.session.flush()
