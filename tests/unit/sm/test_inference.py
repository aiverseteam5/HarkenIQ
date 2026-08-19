"""Inference job: proposals from rack hints, adjacency, co-onset; idempotent."""

from datetime import datetime, timedelta, timezone

import pytest

from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.repos import (
    DeviceRepo,
    DomainRepo,
    SiteRepo,
    StatusRepo,
    TelemetryRepo,
)
from harkeniq_sm.sitemodel.inference import InferenceJob


@pytest.fixture
def config():
    return SMConfig(insecure=True, site_name="site-test")


@pytest.fixture
def job(db, config):
    return InferenceJob(db, config)


async def _seed_devices(db, config, names):
    async with db() as session:
        site = await SiteRepo(session).get_or_create(config.site_name)
        repo = DeviceRepo(session)
        devices = {}
        for agent_id, agent_name, rack in names:
            devices[agent_id] = await repo.upsert_registration(
                site_id=site.id, agent_id=agent_id, agent_name=agent_name,
                rack_suggestion=rack,
            )
        ids = {k: d.id for k, d in devices.items()}
        await session.commit()
    return ids


class TestCooling:
    async def test_rack_hint_domain(self, db, config, job):
        await _seed_devices(db, config, [
            ("a1", "rack-12-srv-01", "rack-12"),
            ("a2", "rack-12-srv-02", "rack-12"),
            ("a3", "rack-13-srv-01", "rack-13"),  # alone: no proposal
        ])
        created = await job.run_once()
        assert created == ["cooling-rack-12"]
        # idempotent
        assert await job.run_once() == []
        async with db() as session:
            site = await SiteRepo(session).get_or_create(config.site_name)
            domain = await DomainRepo(session).get_by_name(site.id, "cooling-rack-12")
            assert domain.status == "inferred"
            assert domain.confidence == 0.6
            assert domain.source == "inference"
            assert len(await DomainRepo(session).members(domain.id)) == 2


class TestNetwork:
    async def test_adjacency_component(self, db, config, job):
        ids = await _seed_devices(db, config, [
            ("a1", "srv-1", None), ("a2", "srv-2", None), ("a3", "srv-3", None),
        ])
        async with db() as session:
            device = await DeviceRepo(session).get_by_agent_id("a1")
            device.peers = ["a2"]
            await StatusRepo(session).upsert(
                ids["a2"], datetime.now(timezone.utc), "OBSERVING", None,
                {"srv-1": "ALIVE"},
            )
            await session.commit()
        created = await job.run_once()
        assert len(created) == 1 and created[0].startswith("network-")
        assert await job.run_once() == []


class TestPower:
    async def test_co_onset_within_window(self, db, config, job):
        ids = await _seed_devices(db, config, [
            ("a1", "srv-1", None), ("a2", "srv-2", None), ("a3", "srv-3", None),
        ])
        now = datetime.now(timezone.utc)
        async with db() as session:
            telemetry = TelemetryRepo(session)
            await telemetry.add_verdict(
                ids["a1"], now, "psu:PS1", "psu_health", "CRITICAL", "", None
            )
            await telemetry.add_verdict(
                ids["a2"], now + timedelta(seconds=30), "psu:PS1", "psu_health",
                "CRITICAL", "", None,
            )
            # outside the 120 s window: separate cluster of one → no proposal
            await telemetry.add_verdict(
                ids["a3"], now + timedelta(seconds=600), "psu:PS1", "psu_health",
                "CRITICAL", "", None,
            )
            await session.commit()
        created = await job.run_once(now=now + timedelta(seconds=700))
        assert len(created) == 1 and created[0].startswith("power-")
        async with db() as session:
            site = await SiteRepo(session).get_or_create(config.site_name)
            domain = await DomainRepo(session).get_by_name(site.id, created[0])
            members = await DomainRepo(session).members(domain.id)
            assert sorted(members) == sorted([ids["a1"], ids["a2"]])
        assert await job.run_once(now=now + timedelta(seconds=700)) == []

    async def test_confirmed_domain_covers_proposal(self, db, config, job):
        """No duplicate power domain when an operator domain covers the members.

        A second domain over the same members would split one shared fault
        into two shared_power parents — the R2a gate demands exactly one.
        """
        ids = await _seed_devices(db, config, [
            ("a1", "srv-1", None), ("a2", "srv-2", None),
        ])
        now = datetime.now(timezone.utc)
        async with db() as session:
            site = await SiteRepo(session).get_or_create(config.site_name)
            domain = await DomainRepo(session).create(site.id, "pdu-1", "power")
            await DomainRepo(session).confirm(domain, by="op")
            await DomainRepo(session).set_members(
                domain.id, [ids["a1"], ids["a2"]], added_by="op"
            )
            telemetry = TelemetryRepo(session)
            for agent in ("a1", "a2"):
                await telemetry.add_verdict(
                    ids[agent], now, "psu:PS1", "psu_health", "CRITICAL", "", None
                )
            await session.commit()
        assert await job.run_once(now=now + timedelta(seconds=130)) == []
