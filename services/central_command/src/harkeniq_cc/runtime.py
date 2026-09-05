"""One process, one asyncio loop: HTTP as the primary task.

``AppState`` is the shared spine -- engine/sessionmaker, services. Production
schema comes from alembic (entrypoint runs ``upgrade head`` first); sqlite DSNs
get ``create_all`` for lab use.
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

from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker

logger = logging.getLogger("harkeniq.cc.runtime")


@dataclass
class AppState:
    config: CCConfig
    engine: object = None
    sessionmaker: object = None
    http_port: int = 0
    started: asyncio.Event = field(default_factory=asyncio.Event)
    # Set when an in-memory sqlite DSN was remapped to a temp file;
    # removed on shutdown.
    tmp_db_path: Optional[str] = None
    # R4-1: live IntelligenceEngine, set by the intelligence loop.
    intelligence: object = None
    # QA-019: verified LicenseInfo (None = no license configured)
    license: object = None
    # A25.6: the app's own MetricsRegistry, set by `create_app`. Carried
    # on the state rather than kept module-global so the background loops
    # and the HTTP routes count into ONE registry while two apps in one
    # process still cannot report each other's traffic -- the property
    # E0.3 mounted a per-app registry to get.
    metrics: object = None


async def make_state(config: CCConfig) -> AppState:
    state = AppState(config=config)
    dsn = config.dsn
    if dsn.startswith("sqlite") and ":memory:" in dsn:
        # In-memory sqlite rides ONE shared aiosqlite connection
        # (StaticPool), so the runtime's concurrent tasks interleave
        # transactions -- a session-teardown ROLLBACK can erase another
        # session's in-flight writes. Remap to a per-process temp file
        # for real per-connection isolation.
        fd, state.tmp_db_path = tempfile.mkstemp(
            prefix="harkeniq-cc-", suffix=".db"
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


async def run(config: CCConfig, state: Optional[AppState] = None) -> None:
    """Serve until cancelled. ``state`` injection is for tests."""
    from harkeniq_cc.app import create_app  # late: app imports routers
    from harkeniq_cc.agent_runtime import operational_agent_loop
    from harkeniq_cc.campaign_runner import campaign_loop
    from harkeniq_cc.fleet_poller import fleet_poll_loop
    from harkeniq_cc.intelligence import intelligence_loop
    from harkeniq_cc.marketplace_sync import marketplace_sync_loop
    from harkeniq_cc.usage_reporter import usage_report_loop
    from harkeniq_cc.warranty.refresh import warranty_refresh_loop

    if state is None:
        state = await make_state(config)

    # QA-019: load + verify the license at startup. Integrity failures
    # raise LicenseError and CC refuses to start; expired licenses run in
    # grace posture (see harkeniq_cc.license for the posture table).
    if state.license is None:
        from harkeniq_cc.license import load_license
        state.license = load_license(config)

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

    async def fleet_poller_task() -> None:
        await fleet_poll_loop(state)

    async def usage_reporter_task() -> None:
        await usage_report_loop(state)

    async def intelligence_task() -> None:
        await intelligence_loop(state)

    async def warranty_task() -> None:
        await warranty_refresh_loop(state)

    async def marketplace_task() -> None:
        await marketplace_sync_loop(state)

    async def operational_agent_task() -> None:
        await operational_agent_loop(state)

    async def campaign_task() -> None:
        await campaign_loop(state)

    async def identity_summary_task() -> None:
        # A20.9: aggregate counts to the platform plane. Counts only --
        # A12.1 is not amended, so no per-agent detail leaves the tenant.
        from harkeniq_cc.machine_identity import identity_summary_loop

        await identity_summary_loop(state)

    async def tenant_birth_task() -> None:
        """A23-5: the tenant's first administrator, once, at startup.

        Not a polling loop -- the primary trigger is provisioning, and
        this is where a Central Command deployment first serves its
        tenant. Every call after the first returns `already_born` and
        writes nothing; the retry cadence exists only as a recovery net
        for a Console that was unreachable at boot, which is the one
        failure that would otherwise leave a real tenant with no
        administrator.
        """
        from harkeniq_cc.tenant_birth import tenant_birth_once

        # Central Command and the Console boot independently, so the
        # first attempt usually finds no Console at all. Retry on a
        # short backoff and then steadily -- each attempt is one read
        # and one conditional write, and the whole task STOPS for good
        # the moment the tenant is born or is found already born.
        for delay in (0, 5, 15, 30) + (60,) * 30:
            if delay:
                await asyncio.sleep(delay)
            outcome = await tenant_birth_once(state)
            if outcome.status in ("seeded", "already_born", "skipped"):
                return
        logger.warning(
            "tenant birth never resolved an owner: the tenant is strict and "
            "unadministered, which /api/tenant-settings/scope-enforcement "
            "reports as locked_out"
        )

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(tenant_birth_task(), name="tenant_birth")
            tg.create_task(serve_http(), name="http")
            tg.create_task(announce_started(), name="announce")
            tg.create_task(fleet_poller_task(), name="fleet_poller")
            tg.create_task(usage_reporter_task(), name="usage_reporter")
            tg.create_task(intelligence_task(), name="intelligence")
            tg.create_task(warranty_task(), name="warranty")
            tg.create_task(marketplace_task(), name="marketplace_sync")
            tg.create_task(campaign_task(), name="campaigns")
            tg.create_task(
                identity_summary_task(), name="identity_summary"
            )
            tg.create_task(
                operational_agent_task(), name="operational_agents"
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
