"""Correlation rules + engine (R-S4): the four §1 boundary-table cases."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from harkeniq_sm.config import SMConfig
from harkeniq_sm.correlation.engine import CorrelationEngine
from harkeniq_sm.db.repos import (
    DeviceRepo,
    DomainRepo,
    IncidentRepo,
    SiteRepo,
    StatusRepo,
    SubsystemStateRepo,
)
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


async def _seed_domain(db, config, kind, name, agent_ids, confirmed=True):
    async with db() as session:
        site = await SiteRepo(session).get_or_create(config.site_name)
        device_repo = DeviceRepo(session)
        ids = []
        for agent_id in agent_ids:
            device = await device_repo.upsert_registration(
                site_id=site.id, agent_id=agent_id, agent_name=f"srv-{agent_id}"
            )
            ids.append(device.id)
        repo = DomainRepo(session)
        domain = await repo.create(site.id, name, kind)
        if confirmed:
            await repo.confirm(domain, by="op")
        await repo.set_members(domain.id, ids)
        await session.commit()
        return domain.id, ids


async def _open_incidents(db, kind=None):
    async with db() as session:
        incidents = await IncidentRepo(session).list_open()
        return [i for i in incidents if kind is None or i.kind == kind]


CRIT = json.dumps([{"fields": {"health": "Critical"}}])


class TestSharedPower:
    async def test_confirmed_domain_one_parent_two_children(self, db, config, ingest):
        await _seed_domain(db, config, "power", "pdu-a", ["a1", "a2"])
        await ingest.verdict("a1", "psu:PS1", "psu_health", "CRITICAL", CRIT)
        await ingest.verdict("a2", "psu:PS1", "psu_health", "CRITICAL", CRIT)

        parents = await _open_incidents(db, "shared_power")
        assert len(parents) == 1
        parent = parents[0]
        assert parent.confidence == 1.0
        assert parent.inferred is False
        async with db() as session:
            children = await IncidentRepo(session).children(parent.id)
            assert len(children) == 2
            assert {c.kind for c in children} == {"device"}

        # Repeat verdicts: no new incidents (consolidation).
        await ingest.verdict("a1", "psu:PS1", "psu_health", "CRITICAL", CRIT)
        assert len(await _open_incidents(db)) == 3  # parent + 2 children

    async def test_inferred_domain_labeled(self, db, config, ingest):
        await _seed_domain(db, config, "power", "pdu-b", ["a1", "a2"], confirmed=False)
        await ingest.verdict("a1", "psu:PS1", "psu_health", "CRITICAL", CRIT)
        await ingest.verdict("a2", "psu:PS1", "psu_health", "CRITICAL", CRIT)
        parent = (await _open_incidents(db, "shared_power"))[0]
        assert parent.inferred is True
        assert parent.confidence == 0.6

    async def test_window_violation_no_parent(self, db, config, engine):
        domain_id, ids = await _seed_domain(db, config, "power", "pdu-a", ["a1", "a2"])
        now = datetime.now(timezone.utc)
        async with db() as session:
            repo = SubsystemStateRepo(session)
            await repo.set(ids[0], "psu", "CRITICAL", now - timedelta(seconds=500))
            await repo.set(ids[1], "psu", "CRITICAL", now)
            await session.commit()
        await engine.sweep(now=now)
        assert await _open_incidents(db, "shared_power") == []

    async def test_single_device_no_parent(self, db, config, ingest):
        await _seed_domain(db, config, "power", "pdu-a", ["a1", "a2"])
        await ingest.verdict("a1", "psu:PS1", "psu_health", "CRITICAL", CRIT)
        assert await _open_incidents(db, "shared_power") == []
        assert len(await _open_incidents(db, "device")) == 1

    async def test_late_child_attaches(self, db, config, ingest, engine):
        await _seed_domain(db, config, "power", "pdu-a", ["a1", "a2", "a3"])
        await ingest.verdict("a1", "psu:PS1", "psu_health", "CRITICAL", CRIT)
        await ingest.verdict("a2", "psu:PS1", "psu_health", "CRITICAL", CRIT)
        parent = (await _open_incidents(db, "shared_power"))[0]
        # Third member faults much later — still attaches to the open parent.
        await ingest.verdict("a3", "psu:PS1", "psu_health", "CRITICAL", CRIT)
        async with db() as session:
            children = await IncidentRepo(session).children(parent.id)
            assert len(children) == 3
        assert len(await _open_incidents(db, "shared_power")) == 1


class TestRackThermal:
    async def test_cooling_domain_parent(self, db, config, ingest):
        await _seed_domain(db, config, "cooling", "rack-12", ["a1", "a2"])
        await ingest.verdict("a1", "thermal:CPU1", "thermal_check", "WARNING", CRIT)
        await ingest.verdict("a2", "thermal:CPU1", "thermal_check", "WARNING", CRIT)
        parents = await _open_incidents(db, "rack_thermal")
        assert len(parents) == 1
        assert parents[0].subsystem == "thermal"


class TestBatchComponent:
    EVIDENCE = json.dumps([{"fields": {"model": "WD-X1000", "health": "Critical"}}])

    async def test_three_devices_same_model(self, db, config, ingest):
        for agent in ("a1", "a2", "a3"):
            await ingest.verdict(
                agent, "disk:sda", "disk_health", "CRITICAL", self.EVIDENCE
            )
        parents = await _open_incidents(db, "batch_component")
        assert len(parents) == 1
        parent = parents[0]
        assert parent.correlation_meta["model"] == "WD-X1000"
        async with db() as session:
            assert len(await IncidentRepo(session).children(parent.id)) == 3

    async def test_two_devices_insufficient(self, db, config, ingest):
        for agent in ("a1", "a2"):
            await ingest.verdict(
                agent, "disk:sda", "disk_health", "CRITICAL", self.EVIDENCE
            )
        assert await _open_incidents(db, "batch_component") == []

    async def test_different_models_not_grouped(self, db, config, ingest):
        for i, agent in enumerate(("a1", "a2", "a3")):
            evidence = json.dumps([{"fields": {"model": f"M-{i}"}}])
            await ingest.verdict(agent, "disk:sda", "disk_health", "CRITICAL", evidence)
        assert await _open_incidents(db, "batch_component") == []


class TestNetworkAmbiguity:
    async def _seed_statuses(self, db, config, target_age_s, votes):
        _, ids = await _seed_domain(db, config, "power", "unused", ["t", "v1", "v2"])
        now = datetime.now(timezone.utc)
        async with db() as session:
            repo = StatusRepo(session)
            await repo.upsert(
                ids[0], now - timedelta(seconds=target_age_s), "OBSERVING", None, None
            )
            for device_id, vote in zip(ids[1:], votes):
                await repo.upsert(device_id, now, "OBSERVING", None, {"t": vote})
            await session.commit()
        return ids

    async def test_alive_votes_mean_path_suspect(self, db, config, engine):
        ids = await self._seed_statuses(db, config, 300, ["ALIVE", "ALIVE"])
        await engine.sweep()
        incident = (await _open_incidents(db, "network_ambiguity"))[0]
        assert incident.device_id == ids[0]
        assert incident.correlation_meta["assessment"] == "path_suspect"
        assert incident.confidence == 0.6
        assert set(incident.correlation_meta["votes"].values()) == {"ALIVE"}

    async def test_unresponsive_quorum_means_device_down(self, db, config, engine):
        await self._seed_statuses(db, config, 300, ["UNRESPONSIVE", "UNRESPONSIVE"])
        await engine.sweep()
        incident = (await _open_incidents(db, "network_ambiguity"))[0]
        assert incident.correlation_meta["assessment"] == "device_down_peer_confirmed"
        assert incident.confidence == 1.0

    async def test_no_duplicate_and_resolves_on_return(self, db, config, engine):
        ids = await self._seed_statuses(db, config, 300, ["ALIVE", "ALIVE"])
        await engine.sweep()
        await engine.sweep()
        assert len(await _open_incidents(db, "network_ambiguity")) == 1
        # Device reports again → resolved.
        async with db() as session:
            await StatusRepo(session).upsert(
                ids[0], datetime.now(timezone.utc), "OBSERVING", None, None
            )
            await session.commit()
        await engine.sweep()
        assert await _open_incidents(db, "network_ambiguity") == []


class TestResolution:
    async def test_children_resolve_then_parent_holddown(self, db, config, ingest, engine):
        await _seed_domain(db, config, "power", "pdu-a", ["a1", "a2"])
        await ingest.verdict("a1", "psu:PS1", "psu_health", "CRITICAL", CRIT)
        await ingest.verdict("a2", "psu:PS1", "psu_health", "CRITICAL", CRIT)
        assert len(await _open_incidents(db, "shared_power")) == 1

        await ingest.verdict("a1", "psu:PS1", "psu_health", "HEALTHY")
        await ingest.verdict("a2", "psu:PS1", "psu_health", "HEALTHY")

        # Sweep 1: children resolve, parent held down.
        await engine.sweep()
        assert await _open_incidents(db, "device") == []
        assert len(await _open_incidents(db, "shared_power")) == 1
        # Sweep 2: hold-down satisfied, parent resolves.
        await engine.sweep()
        assert await _open_incidents(db, "shared_power") == []
