"""TX-2 (spec A32.4): rack placement travels SM -> CC over the real wire.

The shape of the S5 and Capability Registry transport tests next door, for
their reason: the bug class that survives every direct-servicer test lives
in the client's proto -> dict layer and in a poller that drops what it was
handed (QA-042). So this starts at a REAL Site Manager servicer on a real
port and ends in the catalogue -- through the real `SMClient`, the real
`fleet_poll_loop` and the real mirror.

What is proven:

* **E0.2 holds for racks.** One Site Manager serving two sites reports each
  site's OWN racks, and in each only that site's OWN devices. A device whose
  `rack_id` names a rack of the other site is reported placed nowhere --
  that rack's id never crosses into the wrong site's report.
* **Operator-assigned only.** `rack_suggestion`, the name heuristic, never
  becomes a rack and never places a device.
* **Unknown is not zero.** A Site Manager that predates TX-2 -- or that
  cannot assemble the report -- yields `reported=False`, which Central
  Command stores as UNKNOWN and the mirror holds on, rather than an empty
  report that would retire every rack the site has.
* **The poller carries it.** One cycle of the shipped poll loop writes
  `cc_site_topology`, and the mirror turns it into Site CONTAINS Rack
  CONTAINS Device.
"""

from __future__ import annotations

import asyncio

import grpc
import pytest
from sqlalchemy import select

from harkeniq.proto import harkeniq_pb2, harkeniq_pb2_grpc
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.models import CCSite, CCSiteTopology
from harkeniq_cc.db.repos import ResourceRepo, SiteTopologyRepo
from harkeniq_cc.resource_catalogue import (
    LEGACY_DEVICE,
    LEGACY_RACK,
    LEGACY_SITE,
    TYPE_RACK,
)
from harkeniq_cc.resource_mirror import reconcile
from harkeniq_cc.runtime import make_state
from harkeniq_cc.sm_client import SMClient
from harkeniq_sm.approvals import ApprovalService
from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_sm.db.models import Device, Rack, Site
from harkeniq_sm.db.repos import RackRepo
from harkeniq_sm.grpc_server import SiteManagerServiceServicer

CC_SITE_A = "a" * 32
CC_SITE_B = "b" * 32
TENANT = "tenant-tx2-wire"


async def _sm(*, servicer_cls=SiteManagerServiceServicer):
    """One Site Manager serving two sites, A and B, each with its own racks.

    site A: rack a-01 holds node-a1 and node-a2; rack a-02 is empty;
            node-a3 is unplaced but its NAME hints a rack (rack_suggestion);
            node-a4's rack_id names B's rack (an enrolment moved under it).
    site B: rack b-01 holds node-b1.
    """
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    db = make_sessionmaker(engine)
    async with db() as session:
        site_a = Site(id="sma" * 10 + "xx", name="site-a", cc_site_id=CC_SITE_A)
        site_b = Site(id="smb" * 10 + "xx", name="site-b", cc_site_id=CC_SITE_B)
        session.add_all([site_a, site_b])
        await session.flush()
        racks = {
            "a-01": Rack(id="rk-a01", site_id=site_a.id, name="a-01"),
            "a-02": Rack(id="rk-a02", site_id=site_a.id, name="a-02", row_label="R1"),
            "b-01": Rack(id="rk-b01", site_id=site_b.id, name="b-01"),
        }
        session.add_all(racks.values())
        await session.flush()

        def dev(agent, site, rack=None, suggestion=None):
            return Device(
                site_id=site.id, agent_id=agent, agent_name=agent,
                device_class="server", rack_id=rack, rack_suggestion=suggestion,
            )

        session.add_all([
            dev("node-a1", site_a, "rk-a01"),
            dev("node-a2", site_a, "rk-a01"),
            dev("node-a3", site_a, suggestion="rack-07"),
            dev("node-a4", site_a, "rk-b01"),
            dev("node-b1", site_b, "rk-b01"),
        ])
        await session.commit()
    config = SMConfig(insecure=True, site_name="site-a")
    servicer = servicer_cls(db, ApprovalService(db, config), config)
    server = grpc.aio.server()
    harkeniq_pb2_grpc.add_SiteManagerServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    return f"127.0.0.1:{port}", server, engine


async def _snapshot(endpoint, cc_site_id):
    return await SMClient().get_fleet_snapshot(endpoint, "tok", TENANT, cc_site_id)


class _PreTX2Servicer(SiteManagerServiceServicer):
    """A Site Manager that predates TX-2: it never sets field 10."""

    async def _topology(self, session, site_id, devices):
        return harkeniq_pb2.FleetTopology()


async def test_each_site_reports_its_own_racks_and_its_own_devices():
    endpoint, server, engine = await _sm()
    try:
        a = (await _snapshot(endpoint, CC_SITE_A))["topology"]
        b = (await _snapshot(endpoint, CC_SITE_B))["topology"]
    finally:
        await server.stop(grace=None)
        await engine.dispose()
    assert a["reported"] is True and b["reported"] is True
    assert a["racks"] == [
        {"rack_id": "rk-a01", "name": "a-01", "device_agent_ids": ["node-a1", "node-a2"]},
        {"rack_id": "rk-a02", "name": "a-02", "device_agent_ids": []},
    ]
    assert b["racks"] == [
        {"rack_id": "rk-b01", "name": "b-01", "device_agent_ids": ["node-b1"]},
    ]
    # E0.2: node-a4 names B's rack -- it is in NEITHER report's racks, and
    # B's rack id never appears in A's report.
    every = [m for rack in a["racks"] + b["racks"] for m in rack["device_agent_ids"]]
    assert "node-a4" not in every
    assert "rk-b01" not in str(a)
    # A rack suggestion is a guess from a name, never a rack.
    assert "rack-07" not in str(a) and "node-a3" not in every
    # The row label is a label, not an identity, and is not carried.
    assert "R1" not in str(a)


async def test_an_older_site_manager_reads_as_unknown_not_empty():
    endpoint, server, engine = await _sm(servicer_cls=_PreTX2Servicer)
    try:
        topology = (await _snapshot(endpoint, CC_SITE_A))["topology"]
    finally:
        await server.stop(grace=None)
        await engine.dispose()
    assert topology == {"reported": False, "racks": []}


async def test_a_report_that_cannot_be_assembled_degrades_to_unknown(monkeypatch):
    async def broken(self, site_id):
        raise RuntimeError("rack table unavailable")

    monkeypatch.setattr(RackRepo, "list_for_site", broken)
    endpoint, server, engine = await _sm()
    try:
        snapshot = await _snapshot(endpoint, CC_SITE_A)
    finally:
        await server.stop(grace=None)
        await engine.dispose()
    assert snapshot["topology"] == {"reported": False, "racks": []}
    # ... and costs the snapshot nothing else.
    assert snapshot["site_resolved"] is True
    assert {d["agent_id"] for d in snapshot["devices"]} == {
        "node-a1", "node-a2", "node-a3", "node-a4",
    }
    assert snapshot["safety"]["reported"] is True


async def test_an_unresolved_site_reports_no_topology_at_all():
    endpoint, server, engine = await _sm()
    try:
        snapshot = await _snapshot(endpoint, "c" * 32)
    finally:
        await server.stop(grace=None)
        await engine.dispose()
    assert snapshot["site_resolved"] is False
    assert snapshot["topology"] == {"reported": False, "racks": []}


async def test_one_cycle_of_the_shipped_poller_lands_racks_in_the_catalogue():
    endpoint, server, engine = await _sm()
    config = CCConfig(insecure=True, tenant_id=TENANT, http_host="127.0.0.1",
                      http_port=0, site_poll_interval_s=0.05)
    state = await make_state(config)
    async with state.sessionmaker() as session:
        session.add_all([
            CCSite(id=CC_SITE_A, tenant_id=TENANT, site_name="site-a",
                   sm_endpoint=endpoint, sm_token="tok"),
            CCSite(id=CC_SITE_B, tenant_id=TENANT, site_name="site-b",
                   sm_endpoint=endpoint, sm_token="tok"),
        ])
        await session.commit()
    from harkeniq_cc.fleet_poller import fleet_poll_loop

    task = asyncio.create_task(fleet_poll_loop(state))
    try:
        rows = []
        for _ in range(200):
            async with state.sessionmaker() as session:
                rows = (await session.execute(
                    select(CCSiteTopology).order_by(CCSiteTopology.site_id)
                )).scalars().all()
            if len(rows) == 2:
                break
            await asyncio.sleep(0.05)
        assert [(r.site_id, r.tenant_id, r.reported) for r in rows] == [
            (CC_SITE_A, TENANT, True), (CC_SITE_B, TENANT, True),
        ]
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await server.stop(grace=None)
        await engine.dispose()

    async with state.sessionmaker() as session:
        await reconcile(session, TENANT)
        await session.commit()
        repo = ResourceRepo(session)
        site_a = await repo.get_by_legacy(TENANT, LEGACY_SITE, CC_SITE_A)
        rack = await repo.get_by_legacy(TENANT, LEGACY_RACK, "rk-a01")
        empty = await repo.get_by_legacy(TENANT, LEGACY_RACK, "rk-a02")
        rack_b = await repo.get_by_legacy(TENANT, LEGACY_RACK, "rk-b01")
        a1 = await repo.get_by_legacy(TENANT, LEGACY_DEVICE, "node-a1")
        a3 = await repo.get_by_legacy(TENANT, LEGACY_DEVICE, "node-a3")
        a4 = await repo.get_by_legacy(TENANT, LEGACY_DEVICE, "node-a4")
        assert rack.resource_type == TYPE_RACK and rack.legacy_site_id == CC_SITE_A
        assert rack_b.legacy_site_id == CC_SITE_B
        assert (await repo.primary_parent_edge(rack.id)).from_resource_id == site_a.id
        assert (await repo.primary_parent_edge(empty.id)).from_resource_id == site_a.id
        assert (await repo.primary_parent_edge(a1.id)).from_resource_id == rack.id
        # Unplaced, and named as if in a rack: under its site.
        assert (await repo.primary_parent_edge(a3.id)).from_resource_id == site_a.id
        # Its rack_id named another site's rack: never placed there.
        assert (await repo.primary_parent_edge(a4.id)).from_resource_id == site_a.id
        # No resource for a suggestion, and none for the row label.
        racks = await repo.list_mirrored(TENANT, LEGACY_RACK)
        assert {r.legacy_ref for r in racks} == {"rk-a01", "rk-a02", "rk-b01"}


async def test_an_unknown_report_is_stored_as_unknown_and_replaces_a_known_one():
    """Replace-on-poll: yesterday's racks never pose as today's report."""
    state = await make_state(CCConfig(insecure=True, tenant_id=TENANT,
                                      http_host="127.0.0.1", http_port=0))
    async with state.sessionmaker() as session:
        repo = SiteTopologyRepo(session)
        await repo.upsert(TENANT, CC_SITE_A, {"reported": True, "racks": [
            {"rack_id": "rk-a01", "name": "a-01", "device_agent_ids": ["n1"]},
        ]})
        await session.commit()
        await repo.upsert(TENANT, CC_SITE_A, {"reported": False})
        await session.commit()
        row = await session.get(CCSiteTopology, CC_SITE_A)
        assert (row.reported, row.racks) == (False, [])


@pytest.mark.parametrize("hostile", [
    None, "racks", 7, [None, 3, "x"],
    [{"rack_id": ""}, {"rack_id": 5}, {"name": "no id"}, {"rack_id": "x" * 256}],
])
async def test_a_malformed_report_stores_no_rack(hostile):
    state = await make_state(CCConfig(insecure=True, tenant_id=TENANT,
                                      http_host="127.0.0.1", http_port=0))
    async with state.sessionmaker() as session:
        row = await SiteTopologyRepo(session).upsert(
            TENANT, CC_SITE_A, {"reported": True, "racks": hostile},
        )
        await session.commit()
    assert row.reported is True and row.racks == []


async def test_members_are_cleaned_never_repaired():
    state = await make_state(CCConfig(insecure=True, tenant_id=TENANT,
                                      http_host="127.0.0.1", http_port=0))
    async with state.sessionmaker() as session:
        row = await SiteTopologyRepo(session).upsert(TENANT, CC_SITE_A, {
            "reported": True,
            "racks": [
                {"rack_id": "rk-2", "name": 9, "device_agent_ids": ["b", "a", "a", "", 4]},
                {"rack_id": "rk-1", "name": "z", "device_agent_ids": "not-a-list"},
                # Two entries for one rack id are BOTH kept: an ambiguity
                # the mirror must see to hold, not one to resolve here.
                {"rack_id": "rk-1", "name": "y", "device_agent_ids": []},
            ],
        })
        await session.commit()
    assert row.racks == [
        {"rack_id": "rk-2", "name": "", "device_agent_ids": ["a", "b"]},
        {"rack_id": "rk-1", "name": "y", "device_agent_ids": []},
        {"rack_id": "rk-1", "name": "z", "device_agent_ids": []},
    ]
