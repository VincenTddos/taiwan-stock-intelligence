"""Idempotent seed for local/dev environments.

Creates the initial admin account only. There is deliberately no sample market
data, no sample scores and no sample news: Phase 1 forbids fabricated market
information, and a seed file is exactly where such data tends to sneak in and
later get mistaken for real.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys

from sqlalchemy import select

from app.core.config import AppEnv, get_settings
from app.core.security import hash_password
from app.db.session import dispose_engine, get_sessionmaker
from app.models.user import Role, User


async def seed() -> int:
    settings = get_settings()
    email = os.getenv("SEED_ADMIN_EMAIL", "admin@twquant.dev").lower()
    password = os.getenv("SEED_ADMIN_PASSWORD")

    if password is None:
        if settings.APP_ENV in (AppEnv.PRODUCTION, AppEnv.STAGING):
            print("SEED_ADMIN_PASSWORD must be set outside local/test.", file=sys.stderr)
            return 1
        password = secrets.token_urlsafe(16)
        generated = True
    else:
        generated = False

    async with get_sessionmaker()() as session:
        existing = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        if existing is not None:
            print(f"admin user already exists: {email} (no changes made)")
            return 0

        session.add(
            User(
                email=email,
                password_hash=hash_password(password),
                display_name="Administrator",
                role=Role.ADMIN,
                is_active=True,
            )
        )
        await session.commit()

    print(f"created admin user: {email}")
    if generated:
        print(f"generated password: {password}")
        print("Store it now — it is not recoverable from the database.")
    return 0


async def _main() -> int:
    try:
        return await seed()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
