"""Runtime TaskGroup smoke: HTTP up in one loop, clean shutdown."""

import asyncio

import httpx

from harkeniq_console.config import ConsoleConfig
from harkeniq_console.runtime import make_state, run


def _config(**kw):
    return ConsoleConfig(
        insecure=True,
        http_host="127.0.0.1",
        http_port=0,
        **kw,
    )


async def test_memory_dsn_remapped_to_temp_file():
    """Regression: :memory: sqlite = ONE shared connection (StaticPool);
    concurrent runtime sessions interleave transactions, so a teardown
    ROLLBACK could erase another session's in-flight writes."""
    import os

    config = _config()
    assert ":memory:" in config.dsn
    state = await make_state(config)
    assert state.tmp_db_path is not None
    assert os.path.exists(state.tmp_db_path)
    assert ":memory:" not in str(state.engine.url)
    task = asyncio.create_task(run(config, state=state))
    try:
        await asyncio.wait_for(state.started.wait(), timeout=10)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, BaseExceptionGroup):
            pass
    # shutdown removes the temp file
    assert not os.path.exists(state.tmp_db_path)


async def test_file_dsn_not_remapped(tmp_path):
    config = _config(dsn=f"sqlite+aiosqlite:///{tmp_path}/console.db")
    state = await make_state(config)
    assert state.tmp_db_path is None
    await state.engine.dispose()


async def test_http_health_endpoint():
    config = _config()
    state = await make_state(config)
    task = asyncio.create_task(run(config, state=state))
    try:
        await asyncio.wait_for(state.started.wait(), timeout=10)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{state.http_port}"
        ) as client:
            health = await client.get("/healthz")
            assert health.json() == {"status": "ok", "service": "console"}
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, BaseExceptionGroup):
            pass
