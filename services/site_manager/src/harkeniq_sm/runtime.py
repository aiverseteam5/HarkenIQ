"""One process, one asyncio loop: gRPC + HTTP as sibling tasks.

``AppState`` is the shared spine — engine/sessionmaker, ingest, and
(later phases) the correlation engine and approvals service hang off
it. Production schema comes from alembic (entrypoint runs ``upgrade
head`` first); sqlite DSNs get ``create_all`` for lab use.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import Optional

import uvicorn

from harkeniq_sm.approvals import ApprovalService
from harkeniq_sm.config import SMConfig
from harkeniq_sm.correlation.engine import CorrelationEngine
from harkeniq_sm.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_sm.grpc_server import (
    AgentServiceServicer,
    SiteManagerServiceServicer,
    build_server,
)
from harkeniq_sm.ingest import IngestService
from harkeniq_sm.sitemodel.inference import InferenceJob

logger = logging.getLogger("harkeniq.sm.runtime")


@dataclass
class AppState:
    config: SMConfig
    engine: object = None
    sessionmaker: object = None
    ingest: Optional[IngestService] = None
    approvals: object = None       # attached by the approvals phase
    correlation: Optional[CorrelationEngine] = None
    inference: Optional[InferenceJob] = None
    grpc_port: int = 0
    http_port: int = 0
    started: asyncio.Event = field(default_factory=asyncio.Event)
    # Set when an in-memory sqlite DSN was remapped to a temp file;
    # removed on shutdown.
    tmp_db_path: Optional[str] = None


async def make_state(config: SMConfig) -> AppState:
    state = AppState(config=config)
    dsn = config.dsn
    if dsn.startswith("sqlite") and ":memory:" in dsn:
        # In-memory sqlite rides ONE shared aiosqlite connection
        # (StaticPool), so the runtime's concurrent ingest/sweeper/API
        # sessions interleave transactions — a session-teardown ROLLBACK
        # can erase another session's in-flight writes. Remap to a
        # per-process temp file for real per-connection isolation.
        fd, state.tmp_db_path = tempfile.mkstemp(
            prefix="harkeniq-sm-", suffix=".db"
        )
        os.close(fd)
        dsn = f"sqlite+aiosqlite:///{state.tmp_db_path}"
        logger.warning(
            "In-memory sqlite DSN is unsafe under concurrency; "
            "using temp file %s instead", state.tmp_db_path,
        )
    state.engine = make_engine(dsn)
    if dsn.startswith("sqlite"):
        await create_all(state.engine)
    state.sessionmaker = make_sessionmaker(state.engine)
    state.ingest = IngestService(state.sessionmaker, config)
    state.correlation = CorrelationEngine(state.sessionmaker, config)
    state.ingest.on_onset = state.correlation.on_onset

    # R3b-1 C1: reasoning pipeline with optional LLM enrichment
    from harkeniq_sm.reasoning import (
        DeterministicReasoner, KnowledgeBaseReasoner, LLMReasoner, ReasoningPipeline,
    )
    pipeline = ReasoningPipeline()
    pipeline.add_provider(DeterministicReasoner())
    pipeline.add_provider(KnowledgeBaseReasoner())
    # R4-1: api_key is optional -- local OpenAI-compatible endpoints
    # (llama.cpp server, vLLM) run without authentication.
    if config.llm_enabled and config.llm_api_url:
        from harkeniq_sm.llm_provider import LLMProvider
        llm = LLMProvider(
            api_url=config.llm_api_url,
            api_key=config.llm_api_key,
            model=config.llm_model,
            timeout=config.llm_timeout_s,
            max_tokens=config.llm_max_tokens,
        )
        pipeline.add_provider(LLMReasoner(llm))
        logger.info("LLM reasoning enabled (model=%s)", config.llm_model)
    state.ingest.reasoning_pipeline = pipeline
    state.inference = InferenceJob(state.sessionmaker, config)
    state.approvals = ApprovalService(state.sessionmaker, config)
    return state


async def run(config: SMConfig, state: Optional[AppState] = None) -> None:
    """Serve until cancelled. ``state`` injection is for tests."""
    from harkeniq_sm.app import create_app  # late: app imports routers

    if state is None:
        state = await make_state(config)

    servicer = AgentServiceServicer(state.ingest, approvals=state.approvals)
    sm_servicer = SiteManagerServiceServicer(
        state.sessionmaker, state.approvals, config,
    )
    grpc_server, grpc_port = build_server(config, servicer, sm_servicer=sm_servicer)
    state.grpc_port = grpc_port

    uv_config = uvicorn.Config(
        create_app(state),
        host=config.http_host,
        port=config.http_port,
        log_level="warning",
    )
    http_server = uvicorn.Server(uv_config)

    async def serve_grpc() -> None:
        await grpc_server.start()
        logger.info("Site Manager '%s' gRPC up on port %d", config.site_name, grpc_port)
        await grpc_server.wait_for_termination()

    async def serve_http() -> None:
        await http_server.serve()

    async def inference_loop() -> None:
        while True:
            await asyncio.sleep(config.inference_interval_s)
            try:
                await state.inference.run_once()
            except Exception:  # pragma: no cover - keep proposing
                logger.exception("inference run failed")

    async def announce_started() -> None:
        while not http_server.started:
            await asyncio.sleep(0.02)
        for server in http_server.servers:
            for sock in server.sockets:
                state.http_port = sock.getsockname()[1]
        state.started.set()

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(serve_grpc(), name="grpc")
            tg.create_task(serve_http(), name="http")
            tg.create_task(announce_started(), name="announce")
            tg.create_task(state.correlation.run(), name="sweeper")
            tg.create_task(inference_loop(), name="inference")
    finally:
        state.started.clear()
        http_server.should_exit = True
        try:
            await grpc_server.stop(grace=1.0)
        except asyncio.CancelledError:
            pass
        if state.engine is not None:
            try:
                await state.engine.dispose()
            except asyncio.CancelledError:
                pass
        if state.tmp_db_path:
            with contextlib.suppress(OSError):
                os.unlink(state.tmp_db_path)
