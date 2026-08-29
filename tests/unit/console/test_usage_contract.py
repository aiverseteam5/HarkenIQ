"""C3 (final assessment, P0 2026-08-29): the CC->Console metering contract,
proven over the wire.

CC used to POST a flat single-event body while the Console's
UsageEventsRequest requires ``{tenant_id, events:[...]}`` — every real
usage report 422ed and was swallowed as a warning, so metering (and
therefore billing) starved silently since R2b. This test builds the
payload with THE SAME function the CC reporter uses
(harkeniq_cc.usage_reporter.build_console_usage_payload) and POSTs it at
a real Console ASGI app with the internal key enforced — if either side's
schema moves, this breaks before a deployment does. QA-042's lesson as a
standing test category.
"""

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select

from harkeniq_cc.usage_reporter import build_console_usage_payload
from harkeniq_console.app import create_app
from harkeniq_console.config import ConsoleConfig
from harkeniq_console.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_console.db.models import UsageEvent
from harkeniq_console.runtime import AppState

KEY = "shared-cc-key"
TENANT = "tenant-1"


@pytest.fixture
async def console():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    config = ConsoleConfig(insecure=False, internal_api_key=KEY)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)
    # usage_events.tenant_id is a real FK on Postgres — seed the tenant the
    # way a real deployment has it, so the test stays honest.
    from harkeniq_console.db.models import Tenant

    async with sessionmaker() as session:
        session.add(Tenant(
            id=TENANT, name="Tenant One", slug="tenant-one",
            billing_country="US", currency="USD",
        ))
        await session.commit()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        yield client, sessionmaker
    await engine.dispose()


class TestUsageContract:
    async def test_cc_payload_is_accepted_and_recorded(self, console):
        client, sessionmaker = console
        usage = {"node_count": 227, "agent_versions": {"0.1.0": 227}}
        payload = build_console_usage_payload(
            TENANT, "site-blr-1", "2026-08-28", usage,
        )
        resp = await client.post(
            "/api/internal/usage-events",
            json=payload,
            headers={"Authorization": f"Bearer {KEY}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"recorded": 1}

        async with sessionmaker() as session:
            rows = (
                await session.execute(
                    select(UsageEvent).where(UsageEvent.tenant_id == TENANT)
                )
            ).scalars().all()
        assert len(rows) == 1
        assert rows[0].site_name == "site-blr-1"
        assert rows[0].node_count == 227
        assert str(rows[0].date) == "2026-08-28"  # stored as a real Date
        assert rows[0].source == "cc_report"

    async def test_the_old_flat_payload_is_still_refused(self, console):
        """The pre-fix shape must keep failing loudly — if the Console ever
        starts silently accepting it, two writers with different shapes
        would coexist."""
        client, _ = console
        resp = await client.post(
            "/api/internal/usage-events",
            json={
                "tenant_id": TENANT,
                "site_id": "s1",
                "site_name": "site-blr-1",
                "date": "2026-08-28",
                "node_count": 227,
                "agent_versions": None,
            },
            headers={"Authorization": f"Bearer {KEY}"},
        )
        assert resp.status_code == 422

    async def test_payload_shape_matches_console_schema_fields(self):
        """Field-level pin, independent of the app: every event key the CC
        builder emits is a declared Console schema field."""
        from harkeniq_console.api.internal import (
            UsageEventPayload,
            UsageEventsRequest,
        )

        payload = build_console_usage_payload(
            TENANT, "s", "2026-08-28", {"node_count": 1},
        )
        parsed = UsageEventsRequest.model_validate(payload)
        assert parsed.tenant_id == TENANT
        assert len(parsed.events) == 1
        assert set(payload["events"][0]) <= set(
            UsageEventPayload.model_fields
        )
