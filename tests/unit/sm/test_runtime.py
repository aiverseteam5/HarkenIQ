"""Runtime TaskGroup smoke: both servers up in one loop, clean shutdown."""

import asyncio

import grpc
import httpx
import pytest

from harkeniq.proto import harkeniq_pb2, harkeniq_pb2_grpc
from harkeniq_sm.config import SMConfig
from harkeniq_sm.runtime import make_state, run


@pytest.fixture
def config():
    return SMConfig(
        insecure=True, site_name="lab",
        http_host="127.0.0.1", http_port=0,
        grpc_host="127.0.0.1", grpc_port=0,
    )


async def test_memory_dsn_remapped_to_temp_file(config):
    """Regression: :memory: sqlite = ONE shared connection (StaticPool);
    concurrent runtime sessions interleave transactions, so a teardown
    ROLLBACK could erase another session's in-flight writes."""
    import os

    assert ":memory:" in config.dsn  # default lab DSN
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
        except asyncio.CancelledError:
            pass
    # shutdown removes the temp file
    assert not os.path.exists(state.tmp_db_path)


async def test_file_dsn_not_remapped(tmp_path):
    config = SMConfig(
        insecure=True, site_name="lab",
        dsn=f"sqlite+aiosqlite:///{tmp_path}/sm.db",
    )
    state = await make_state(config)
    assert state.tmp_db_path is None
    await state.engine.dispose()


async def test_grpc_and_http_side_by_side(config):
    state = await make_state(config)
    task = asyncio.create_task(run(config, state=state))
    try:
        await asyncio.wait_for(state.started.wait(), timeout=10)

        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{state.http_port}"
        ) as client:
            health = await client.get("/healthz")
            assert health.json() == {"status": "ok", "site": "lab"}

            async with grpc.aio.insecure_channel(
                f"127.0.0.1:{state.grpc_port}"
            ) as channel:
                stub = harkeniq_pb2_grpc.AgentServiceStub(channel)
                await stub.RegisterAgent(
                    harkeniq_pb2.AgentRegistration(agent_id="a1", agent_name="srv-1")
                )
                await stub.Heartbeat(
                    harkeniq_pb2.AgentHeartbeat(
                        agent_id="a1", state="OBSERVING",
                        health_summary={"psu": "OK"},
                    )
                )

            coverage = (await client.get("/api/coverage")).json()
        assert len(coverage) == 1
        assert coverage[0]["agent_id"] == "a1"
        assert coverage[0]["observation"] == "observed"
        assert coverage[0]["health"] == "ok"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
