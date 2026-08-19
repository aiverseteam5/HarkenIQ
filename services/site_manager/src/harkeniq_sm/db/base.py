"""Async engine/session factory for the Site Manager database."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from harkeniq_sm.db.models import Base


def make_engine(dsn: str, echo: bool = False) -> AsyncEngine:
    return create_async_engine(dsn, echo=echo)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_all(engine: AsyncEngine) -> None:
    """Create the schema directly (tests / sqlite). Production uses alembic."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
