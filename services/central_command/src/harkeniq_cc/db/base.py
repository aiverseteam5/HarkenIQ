"""Async engine/session factory for the Central Command database."""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from harkeniq_cc.db.models import Base


def make_engine(dsn: str, echo: bool = False) -> AsyncEngine:
    engine = create_async_engine(dsn, echo=echo)
    if engine.dialect.name == "sqlite":
        # A30.41: SQLite renders an outcome row's recency weight with a
        # function implemented by `predictive.decay_weight` itself, so the
        # SQL and Python paths cannot disagree there. PostgreSQL needs none.
        from harkeniq_cc.db.outcome_sql import register_sqlite_functions

        event.listen(engine.sync_engine, "connect", register_sqlite_functions)
    return engine


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_all(engine: AsyncEngine) -> None:
    """Create the schema directly (tests / sqlite). Production uses alembic."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
