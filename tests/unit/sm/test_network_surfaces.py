"""R6-P7 SM tests: device_class through registration + the TOR rule.

Named tests (review 8A): proto-compat (a pre-R6 agent sending no
device_class keeps "server"), and the mixed server+switch site correlating
a TOR event into ONE parent incident (A2.6 Connectivity row live).
"""

from datetime import datetime, timedelta, timezone

import pytest

from harkeniq_sm.config import SMConfig
from harkeniq_sm.correlation.engine import CorrelationEngine
from harkeniq_sm.correlation import rules
from harkeniq_sm.db.repos import (
    DeviceRepo,
    DomainRepo,
    IncidentRepo,
    SiteRepo,
    StatusRepo,
    SubsystemStateRepo,
)
from harkeniq_sm.incidents import IncidentService
from harkeniq_sm.ingest import IngestService


@pytest.fixture
def config():
    return SMConfig(insecure=True, site_name="site-test")


@pytest.fixture
def engine(db, config):
    return CorrelationEngine(db, config)


@pytest.fixture
def ingest(db, config, engine):
    service = IngestService(db, config)
    service.on_onset = engine.on_onset
    return service


class TestDeviceClassRegistration:
    async def test_switch_registration_stored(self, db, config, ingest):
        await ingest.register(
            agent_id="sw-1", agent_name="tor-a", vendor="sonic",
            model="Force10-S6000", device_class="switch",
        )
        async with db() as session:
            device = await DeviceRepo(session).get_by_agent_id("sw-1")
            assert device.device_class == "switch"

    async def test_pre_r6_agent_defaults_to_server(self, db, config, ingest):
        # Proto compat (regression-class): an old agent's registration has
        # no device_class field — proto3 delivers "" — the row must read
        # as a server, exactly as before R6.
        await ingest.register(
            agent_id="srv-1", agent_name="srv", vendor="dell",
            model="PowerEdge R750",
        )
        async with db() as session:
            device = await DeviceRepo(session).get_by_agent_id("srv-1")
            assert device.device_class == "server"

    async def test_reregistration_does_not_erase_class(self, db, config, ingest):
        await ingest.register(agent_id="sw-2", device_class="switch")
        await ingest.register(agent_id="sw-2")  # e.g. downgraded agent
        async with db() as session:
            device = await DeviceRepo(session).get_by_agent_id("sw-2")
            assert device.device_class == "switch"


class TestTorConnectivity:
    async def _seed(
        self, db, config, *, lost=3, spread_s=5.0, switch_fault=True,
        stale_age_s=300.0,
    ):
        """Network domain: 1 switch + 3 servers; `lost` servers go silent."""
        async with db() as session:
            site = await SiteRepo(session).get_or_create(config.site_name)
            device_repo = DeviceRepo(session)
            switch = await device_repo.upsert_registration(
                site_id=site.id, agent_id="tor-1", agent_name="tor-1",
                device_class="switch",
            )
            servers = []
            for i in range(3):
                servers.append(await device_repo.upsert_registration(
                    site_id=site.id, agent_id=f"srv-{i}",
                    agent_name=f"srv-{i}",
                ))
            domain_repo = DomainRepo(session)
            domain = await domain_repo.create(site.id, "tor-1-segment", "network")
            await domain_repo.confirm(domain, by="op")
            await domain_repo.set_members(
                domain.id, [switch.id] + [s.id for s in servers]
            )
            now = datetime.now(timezone.utc)
            status_repo = StatusRepo(session)
            # Switch still observed, reporting an interface fault.
            await status_repo.upsert(switch.id, now, "OBSERVING", None, None)
            if switch_fault:
                await SubsystemStateRepo(session).set(
                    switch.id, "interface", "CRITICAL", now
                )
            # Servers: `lost` go silent nearly simultaneously; rest observed.
            for i, server in enumerate(servers):
                if i < lost:
                    seen = now - timedelta(seconds=stale_age_s + i * spread_s)
                else:
                    seen = now
                await status_repo.upsert(server.id, seen, "OBSERVING", None, None)
            await session.commit()
            return switch, servers, site.id

    async def _sweep_rule(self, db, config, site_id):
        incidents_svc = IncidentService(config)
        async with db() as session:
            created = await rules.tor_connectivity(
                session, config, site_id, incidents_svc
            )
            await session.commit()
        return created

    async def test_mixed_fleet_one_parent_with_suspected_switch(self, db, config):
        switch, servers, site_id = await self._seed(db, config)
        await self._sweep_rule(db, config, site_id)
        async with db() as session:
            open_incidents = await IncidentRepo(session).list_open()
            parents = [i for i in open_incidents if i.kind == "tor_connectivity"]
            assert len(parents) == 1
            parent = parents[0]
            assert parent.correlation_meta["suspected_switch"] == "tor-1"
            assert len(parent.correlation_meta["lost_devices"]) == 3
            # The switch's interface incident is a CHILD of the parent.
            children = [
                i for i in open_incidents
                if i.parent_id == parent.id and i.device_id == switch.id
            ]
            assert children, "switch interface incident not attached as child"

    async def test_two_lost_devices_insufficient(self, db, config):
        _, _, site_id = await self._seed(db, config, lost=2)
        await self._sweep_rule(db, config, site_id)
        async with db() as session:
            open_incidents = await IncidentRepo(session).list_open()
            assert [i for i in open_incidents if i.kind == "tor_connectivity"] == []

    async def test_slow_spread_is_not_one_event(self, db, config):
        # Losses spread over minutes are attrition, not a shared cause.
        _, _, site_id = await self._seed(db, config, spread_s=120.0)
        await self._sweep_rule(db, config, site_id)
        async with db() as session:
            open_incidents = await IncidentRepo(session).list_open()
            assert [i for i in open_incidents if i.kind == "tor_connectivity"] == []

    async def test_no_duplicate_parent_on_resweep(self, db, config):
        _, _, site_id = await self._seed(db, config)
        await self._sweep_rule(db, config, site_id)
        await self._sweep_rule(db, config, site_id)
        async with db() as session:
            open_incidents = await IncidentRepo(session).list_open()
            assert len(
                [i for i in open_incidents if i.kind == "tor_connectivity"]
            ) == 1

    async def test_engine_sweep_runs_the_rule(self, db, config, engine):
        _, _, site_id = await self._seed(db, config)
        await engine.sweep()
        async with db() as session:
            open_incidents = await IncidentRepo(session).list_open()
            assert [i for i in open_incidents if i.kind == "tor_connectivity"]
