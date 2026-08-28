"""One process, one asyncio loop: HTTP as the sole server task.

``AppState`` is the shared spine — engine/sessionmaker hang off it.
Production schema comes from alembic (entrypoint runs ``upgrade head``
first); sqlite DSNs get ``create_all`` for lab use.
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

from harkeniq_console.config import ConsoleConfig
from harkeniq_console.db.base import create_all, make_engine, make_sessionmaker

logger = logging.getLogger("harkeniq.console.runtime")


@dataclass
class AppState:
    config: ConsoleConfig
    engine: object = None
    sessionmaker: object = None
    http_port: int = 0
    started: asyncio.Event = field(default_factory=asyncio.Event)
    # Set when an in-memory sqlite DSN was remapped to a temp file;
    # removed on shutdown.
    tmp_db_path: Optional[str] = None


async def make_state(config: ConsoleConfig) -> AppState:
    state = AppState(config=config)
    dsn = config.dsn
    if dsn.startswith("sqlite") and ":memory:" in dsn:
        fd, state.tmp_db_path = tempfile.mkstemp(
            prefix="harkeniq-console-", suffix=".db"
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
    return state


async def seed_service_placement(state: AppState) -> None:
    """Turn a configured ``cc_url`` into an explicit placement row.

    ``cc_url`` used to be the request-time destination for every tenant's
    infrastructure pages. It is now a *seed* only: on a single-tenant
    install (the sovereign and demo shape) it registers that one tenant's
    Central Command in ``tenant_services`` and says so in the log.

    It deliberately does nothing when more than one tenant exists. Guessing
    which of several tenants a lone URL belongs to is how one tenant ends
    up reading another's fleet, and the registry is fail-closed precisely
    so that never happens silently — a multi-tenant install registers
    placements explicitly through the admin API.
    """
    from sqlalchemy import select

    from harkeniq_console.db.models import Tenant
    from harkeniq_console.db.repos import TenantServiceRepo

    cc_url = getattr(state.config, "cc_url", "")
    if not cc_url or state.sessionmaker is None:
        return

    async with state.sessionmaker() as session:
        tenant_ids = (
            await session.execute(select(Tenant.id).limit(2))
        ).scalars().all()
        if len(tenant_ids) != 1:
            if len(tenant_ids) > 1:
                logger.info(
                    "cc_url set but %d tenants exist; not guessing a "
                    "placement — register them via /api/admin/tenant-services",
                    len(tenant_ids),
                )
            return

        repo = TenantServiceRepo(session)
        if await repo.resolve(tenant_ids[0], "central_command") is not None:
            return
        await repo.register(
            tenant_id=tenant_ids[0],
            service_kind="central_command",
            endpoint_url=cc_url,
            registered_by="startup-seed",
        )
        await session.commit()
        logger.info(
            "Seeded central_command placement for sole tenant %s -> %s",
            tenant_ids[0], cc_url,
        )


async def run(config: ConsoleConfig, state: Optional[AppState] = None) -> None:
    """Serve until cancelled. ``state`` injection is for tests."""
    from harkeniq_console.app import create_app  # late: app imports routers

    if state is None:
        state = await make_state(config)

    await seed_service_placement(state)

    uv_config = uvicorn.Config(
        create_app(state),
        host=config.http_host,
        port=config.http_port,
        log_level="warning",
    )
    http_server = uvicorn.Server(uv_config)

    async def serve_http() -> None:
        await http_server.serve()

    async def announce_started() -> None:
        while not http_server.started:
            await asyncio.sleep(0.02)
        for server in http_server.servers:
            for sock in server.sockets:
                state.http_port = sock.getsockname()[1]
        state.started.set()

    from harkeniq_console.billing.reconciler import run_reconciler

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(serve_http(), name="http")
            tg.create_task(announce_started(), name="announce")
            tg.create_task(
                run_reconciler(state.sessionmaker), name="billing-reconciler",
            )
    finally:
        state.started.clear()
        http_server.should_exit = True
        if state.engine is not None:
            try:
                await state.engine.dispose()
            except asyncio.CancelledError:
                pass
        if state.tmp_db_path:
            with contextlib.suppress(OSError):
                os.unlink(state.tmp_db_path)
