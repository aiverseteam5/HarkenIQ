"""A2: per-device skill installation at the Site Manager.

Two defects this closes, both of the same family — a request naming one
thing and the handler acting on another:

  `InstallSkill` resolved `config.site_name` and ignored the `site_id`
  Central Command actually sent, so on a Site Manager serving several
  sites it installed onto whichever site this process happened to be
  configured for. Same shape as the heartbeat and verdict bugs E1.3's
  gate found.

  `SiteSkillInstall` carried no device list, so every install fanned out
  to every device on the site. An Operational Agent scoped to six racks
  would have installed onto the whole estate — a scope escape dressed as
  a convenience.
"""

from __future__ import annotations

import pytest

from harkeniq.proto import harkeniq_pb2
from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.models import Device, Site
from harkeniq_sm.directives import DirectiveService
from harkeniq_sm.grpc_server import SiteManagerServiceServicer

SKILL_YAML = """
name: disk-health
version: 1
target: disk
description: A2 targeting test
rules:
  - condition: "health == 'Critical'"
    verdict: CRITICAL
    message: "Disk {name} has failed"
    action:
      type: IDENTIFY_LED
      params:
        target: "{name}"
"""


@pytest.fixture
async def two_sites(db):
    """One Site Manager, two sites, two devices each."""
    async with db() as session:
        a = Site(id="site-a", name="site-1", cc_site_id="cc-a", status="active")
        b = Site(id="site-b", name="site-2", cc_site_id="cc-b", status="active")
        session.add_all([a, b])
        await session.flush()
        for site_id, names in (("site-a", ["a1", "a2"]), ("site-b", ["b1", "b2"])):
            for name in names:
                session.add(Device(
                    id=f"dev-{name}", site_id=site_id, agent_id=name,
                    agent_name=name, device_class="server",
                ))
        await session.commit()
    config = SMConfig(insecure=True, site_name="site-1")
    return SiteManagerServiceServicer(db, None, config, DirectiveService(db, config))


def _request(**kw):
    base = dict(
        tenant_id="t1", site_id="cc-a", skill_id="disk-health",
        skill_version="1", yaml_content=SKILL_YAML, tier="community",
        validation_state="tested", issued_by="op-agent:ag1@v1",
    )
    base.update(kw)
    return harkeniq_pb2.SiteSkillInstall(**base)


async def _queued_devices(db) -> set[str]:
    from sqlalchemy import select

    from harkeniq_sm.db.models import DirectedDirective

    async with db() as session:
        rows = (
            await session.execute(
                select(DirectedDirective).where(
                    DirectedDirective.kind == "skill_install"
                )
            )
        ).scalars().all()
        return {r.device_id for r in rows}


class TestPerDeviceTargeting:
    async def test_naming_devices_installs_only_onto_them(self, db, two_sites):
        ack = await two_sites.InstallSkill(
            _request(device_agent_ids=["a1"]), None
        )
        assert ack.accepted is True
        assert ack.queued == 1
        assert await _queued_devices(db) == {"dev-a1"}

    async def test_an_empty_list_keeps_the_site_wide_behaviour(self, db, two_sites):
        """Marketplace installs still want every device on the site."""
        ack = await two_sites.InstallSkill(_request(), None)
        assert ack.queued == 2
        assert await _queued_devices(db) == {"dev-a1", "dev-a2"}

    async def test_a_device_at_another_site_is_reported_not_installed(
        self, db, two_sites
    ):
        ack = await two_sites.InstallSkill(
            _request(device_agent_ids=["a1", "b1"]), None
        )
        assert ack.queued == 1
        assert await _queued_devices(db) == {"dev-a1"}
        assert "b1" in ack.reason, "an unplaceable device must be named"

    async def test_naming_only_foreign_devices_installs_nothing(self, db, two_sites):
        ack = await two_sites.InstallSkill(
            _request(device_agent_ids=["b1", "b2"]), None
        )
        assert ack.queued == 0
        assert await _queued_devices(db) == set()


class TestSiteResolution:
    async def test_it_installs_at_the_site_central_command_named(self, db, two_sites):
        """Not the site this process is configured for."""
        ack = await two_sites.InstallSkill(
            _request(site_id="cc-b", device_agent_ids=["b1"]), None
        )
        assert ack.queued == 1
        assert await _queued_devices(db) == {"dev-b1"}

    async def test_the_configured_site_is_not_used_when_cc_names_another(
        self, db, two_sites
    ):
        await two_sites.InstallSkill(_request(site_id="cc-b"), None)
        installed = await _queued_devices(db)
        assert installed == {"dev-b1", "dev-b2"}
        assert "dev-a1" not in installed, (
            "installing onto the config-named site would be the pre-E1.3 bug"
        )


class TestValidationStillGuards:
    async def test_an_unparseable_skill_never_reaches_a_device(self, db, two_sites):
        ack = await two_sites.InstallSkill(
            _request(yaml_content="{{{ not yaml", device_agent_ids=["a1"]), None
        )
        assert ack.accepted is False
        assert await _queued_devices(db) == set()
