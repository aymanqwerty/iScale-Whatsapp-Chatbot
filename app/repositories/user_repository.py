"""Persistence for `User`."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_phone(self, phone: str) -> User | None:
        result = await self._session.execute(select(User).where(User.phone == phone))
        return result.scalar_one_or_none()

    async def get_by_id(self, user_id: int) -> User | None:
        return await self._session.get(User, user_id)

    async def get_or_create(self, phone: str, profile_name: str | None = None) -> User:
        """Fetch the contact, creating it on first contact.

        The unique index on `phone` is the real guard against duplicates; two
        concurrent first-messages from the same number would otherwise race.
        """
        user = await self.get_by_phone(phone)
        if user is not None:
            if profile_name and user.profile_name != profile_name:
                user.profile_name = profile_name
            return user

        user = User(phone=phone, profile_name=profile_name)
        self._session.add(user)
        await self._session.flush()
        return user

    async def set_name(self, user: User, name: str) -> User:
        user.name = name
        await self._session.flush()
        return user
