"""Central Command test fixtures: in-memory aiosqlite database."""

import pytest

from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker


@pytest.fixture
async def db():
    """Fresh in-memory database; yields an async sessionmaker."""
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    yield make_sessionmaker(engine)
    await engine.dispose()


@pytest.fixture
async def session(db):
    async with db() as s:
        yield s
