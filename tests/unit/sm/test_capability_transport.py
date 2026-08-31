"""Capability Registry transport: the node's declaration reaches CC intact.

Four hops, and the Registry is worthless if any of them loses or invents
a fact: agent builds it, RegisterAgent carries it, the Site Manager
stores it, GetFleetSnapshot carries it on. This file covers the SM half
end to end, including the two compatibility directions that decide
whether a real fleet survives a staged upgrade:

  old agent  -> new SM   declares nothing, must read UNKNOWN not empty
  new agent  -> old SM   the SM simply ignores an unknown field (proto3)

and the rollback case that would otherwise silently erase truth: an
agent downgraded below the Registry re-registers with no declaration,
and the SM must keep the last one rather than blank the row.
"""

from __future__ import annotations

import json

import pytest

from harkeniq.capabilities import declare
from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.repos import DeviceRepo
from harkeniq_sm.ingest import IngestService

SWITCH_DECL = declare("gnmi", ["INTERFACE_DISABLE", "INTERFACE_ENABLE"], "switch")
SERVER_DECL = declare("redfish", ["IDENTIFY_LED", "SEL_CLEAR"], "server")


@pytest.fixture
def config():
    return SMConfig(insecure=True, site_name="site-test")


@pytest.fixture
def ingest(db, config):
    return IngestService(db, config)


class TestDeclarationIsStored:
    async def test_a_declaration_is_persisted_verbatim(self, db, ingest):
        await ingest.register(
            agent_id="sw-1", device_class="switch",
            capabilities_json=json.dumps(SWITCH_DECL),
        )
        async with db() as session:
            device = await DeviceRepo(session).get_by_agent_id("sw-1")
            assert device.capabilities == SWITCH_DECL
            assert device.capabilities["effective"] == [
                "INTERFACE_DISABLE", "INTERFACE_ENABLE"
            ]

    async def test_the_stored_declaration_separates_reach_from_policy(self, db, ingest):
        """Three sets survive the wire, not just the intersection."""
        narrow = declare("redfish", ["IDENTIFY_LED"], "server")
        await ingest.register(
            agent_id="srv-1", capabilities_json=json.dumps(narrow)
        )
        async with db() as session:
            stored = (await DeviceRepo(session).get_by_agent_id("srv-1")).capabilities
        assert "SEL_CLEAR" in stored["implemented"]
        assert "SEL_CLEAR" not in stored["allow_list"]
        assert "SEL_CLEAR" not in stored["effective"]


class TestCompatibility:
    async def test_an_agent_predating_the_registry_stores_null(self, db, ingest):
        """Proto3 delivers "". NULL is unknown; an empty declaration
        would be a claim the agent never made."""
        await ingest.register(agent_id="srv-old")
        async with db() as session:
            device = await DeviceRepo(session).get_by_agent_id("srv-old")
            assert device.capabilities is None

    async def test_a_downgraded_agent_does_not_erase_its_declaration(self, db, ingest):
        await ingest.register(
            agent_id="srv-1", capabilities_json=json.dumps(SERVER_DECL)
        )
        await ingest.register(agent_id="srv-1")  # rolled back build
        async with db() as session:
            device = await DeviceRepo(session).get_by_agent_id("srv-1")
            assert device.capabilities == SERVER_DECL

    async def test_a_malformed_declaration_is_ignored_not_stored(self, db, ingest):
        await ingest.register(
            agent_id="srv-1", capabilities_json="{not json"
        )
        async with db() as session:
            device = await DeviceRepo(session).get_by_agent_id("srv-1")
            assert device.capabilities is None

    async def test_a_non_object_declaration_is_refused(self, db, ingest):
        """A list or a string is not a declaration; storing it would
        make every consumer's `.get()` a crash."""
        await ingest.register(agent_id="srv-1", capabilities_json='["SEL_CLEAR"]')
        async with db() as session:
            device = await DeviceRepo(session).get_by_agent_id("srv-1")
            assert device.capabilities is None

    async def test_a_new_declaration_replaces_the_old_one(self, db, ingest):
        """An operator widening a node's allow list must be reflected."""
        await ingest.register(
            agent_id="srv-1",
            capabilities_json=json.dumps(declare("redfish", ["IDENTIFY_LED"])),
        )
        await ingest.register(
            agent_id="srv-1", capabilities_json=json.dumps(SERVER_DECL)
        )
        async with db() as session:
            stored = (await DeviceRepo(session).get_by_agent_id("srv-1")).capabilities
        assert "SEL_CLEAR" in stored["effective"]
