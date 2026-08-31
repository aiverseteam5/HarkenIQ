"""Capability Registry over the wire: SM -> CC, against a real servicer.

The same shape as the QA-042 regression next door, and for the same
reason: the bug class that survives every direct-servicer test lives in
the client's proto->dict layer. A declaration the Site Manager stores
perfectly and the client silently drops would leave `/api/capabilities`
reporting an entire fleet as undeclared -- indistinguishable from a
fleet that genuinely has not upgraded, and therefore invisible.

Both directions are proven here: a declaration survives intact, and an
SM that sends nothing produces NULL rather than an empty declaration.
"""

from __future__ import annotations

import json

import grpc
import pytest

from harkeniq.capabilities import declare
from harkeniq.proto import harkeniq_pb2_grpc
from harkeniq_cc.sm_client import SMClient
from harkeniq_sm.approvals import ApprovalService
from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_sm.db.models import Device, Site
from harkeniq_sm.grpc_server import SiteManagerServiceServicer

CC_SITE_ID = "cc-site-1"
SWITCH_DECL = declare("gnmi", ["INTERFACE_DISABLE", "INTERFACE_ENABLE"], "switch")


@pytest.fixture
async def sm_wire():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    db = make_sessionmaker(engine)
    async with db() as session:
        site = Site(name="site-1", cc_site_id=CC_SITE_ID)
        session.add(site)
        await session.flush()
        session.add(Device(
            id="dev-sw", site_id=site.id, agent_id="sw-1",
            agent_name="tor-a", device_class="switch",
            capabilities=SWITCH_DECL,
        ))
        session.add(Device(
            id="dev-old", site_id=site.id, agent_id="srv-old",
            agent_name="legacy", device_class="server",
        ))
        await session.commit()

    config = SMConfig(insecure=True, site_name="site-1")
    servicer = SiteManagerServiceServicer(
        db, ApprovalService(db, config), config,
    )
    server = grpc.aio.server()
    harkeniq_pb2_grpc.add_SiteManagerServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    yield f"127.0.0.1:{port}"
    await server.stop(grace=None)
    await engine.dispose()


async def test_declaration_survives_the_wire_intact(sm_wire):
    snapshot = await SMClient().get_fleet_snapshot(
        sm_wire, "any-token", "t1", CC_SITE_ID,
    )
    devices = {d["agent_id"]: d for d in snapshot["devices"]}
    assert devices["sw-1"]["capabilities"] == SWITCH_DECL
    assert devices["sw-1"]["capabilities"]["protocol"] == "gnmi"
    assert devices["sw-1"]["capabilities"]["effective"] == [
        "INTERFACE_DISABLE", "INTERFACE_ENABLE"
    ]


async def test_an_undeclared_device_arrives_as_none_not_empty(sm_wire):
    """The distinction the whole Registry rests on, proven on the wire."""
    snapshot = await SMClient().get_fleet_snapshot(
        sm_wire, "any-token", "t1", CC_SITE_ID,
    )
    devices = {d["agent_id"]: d for d in snapshot["devices"]}
    assert devices["srv-old"]["capabilities"] is None


async def test_declaration_is_not_confused_with_firmware_inventory(sm_wire):
    """Both ride the snapshot as JSON strings; a crossed wire here would
    be invisible until an operator read the wrong panel."""
    snapshot = await SMClient().get_fleet_snapshot(
        sm_wire, "any-token", "t1", CC_SITE_ID,
    )
    device = next(d for d in snapshot["devices"] if d["agent_id"] == "sw-1")
    assert device["firmware"] == []
    assert device["capabilities"]["version"] == SWITCH_DECL["version"]
