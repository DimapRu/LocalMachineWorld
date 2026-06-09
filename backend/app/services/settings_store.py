"""Async key-value store for application settings backed by SQLite.

This is a thin wrapper around the `app_settings` table for things like the
HuggingFace API token, which need to survive restarts but don't deserve a
dedicated table or `.env` entry.
"""

from typing import Optional

from app.database import async_session
from app.models import AppSetting


async def get_setting(key: str) -> Optional[str]:
    """Return the stored value for `key`, or None if not set."""
    async with async_session() as session:
        row = await session.get(AppSetting, key)
        return row.value if row else None


async def set_setting(key: str, value: str) -> None:
    """Insert or update the value for `key`. Empty string is a valid value
    and is used to represent 'cleared'."""
    async with async_session() as session:
        row = await session.get(AppSetting, key)
        if row is None:
            row = AppSetting(key=key, value=value)
            session.add(row)
        else:
            row.value = value
        await session.commit()
