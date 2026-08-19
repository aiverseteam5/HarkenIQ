"""Site-model YAML round trip: import = operator statement of record."""

import pytest

from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.repos import AuditRepo, DeviceRepo, DomainRepo, SiteRepo
from harkeniq_sm.sitemodel.yaml_io import SiteYaml

DOC = """
site: site-test
racks:
  - name: rack-12
    row: A
devices:
  - agent_id: a1
    name: rack-12-srv-01
    rack: rack-12
  - agent_id: a2
    name: rack-12-srv-02
    rack: rack-12
fault_domains:
  - name: pdu-a
    kind: power
    members: [a1, a2]
"""


@pytest.fixture
def config():
    return SMConfig(insecure=True, site_name="site-test")


@pytest.fixture
def site_yaml(db, config):
    return SiteYaml(db, config)


class TestImport:
    async def test_import_confirms_domains(self, db, config, site_yaml):
        counters = await site_yaml.import_yaml(DOC, actor="op@site")
        assert counters == {"racks": 1, "devices": 2, "domains": 1}
        async with db() as session:
            site = await SiteRepo(session).get_or_create(config.site_name)
            domain = await DomainRepo(session).get_by_name(site.id, "pdu-a")
            assert domain.status == "confirmed"
            assert domain.confidence == 1.0
            assert domain.source == "yaml_import"
            assert domain.confirmed_by == "op@site"
            assert len(await DomainRepo(session).members(domain.id)) == 2
            device = await DeviceRepo(session).get_by_agent_id("a1")
            assert device.rack_id is not None
            audit = await AuditRepo(session).list_all()
            assert audit[-1].action == "site.yaml_import"

    async def test_placeholder_devices_created(self, db, site_yaml):
        await site_yaml.import_yaml(
            "fault_domains:\n  - name: pdu-x\n    kind: power\n    members: [ghost]\n"
        )
        async with db() as session:
            assert await DeviceRepo(session).get_by_agent_id("ghost") is not None

    async def test_reimport_updates_members(self, db, config, site_yaml):
        await site_yaml.import_yaml(DOC)
        smaller = DOC.replace("members: [a1, a2]", "members: [a1]")
        await site_yaml.import_yaml(smaller)
        async with db() as session:
            site = await SiteRepo(session).get_or_create(config.site_name)
            domain = await DomainRepo(session).get_by_name(site.id, "pdu-a")
            assert len(await DomainRepo(session).members(domain.id)) == 1


class TestRoundTrip:
    async def test_export_reimports_cleanly(self, db, site_yaml):
        await site_yaml.import_yaml(DOC)
        exported = await site_yaml.export()
        counters = await site_yaml.import_yaml(exported)
        assert counters["domains"] == 1
        assert "pdu-a" in exported
        assert "rack-12" in exported
