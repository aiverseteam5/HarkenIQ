"""S6: the read-only campaign planning contract, at the Site Manager.

The Site Manager is the only tier that knows fault domains, so it is the
only tier that may say which devices can safely run together. This file
proves the three properties Central Command depends on:

  READ-ONLY      the handler writes to no table at all, which is why
                 "it cannot mutate, dispatch or authorize" is provable
                 by snapshot rather than asserted in a docstring
  AUTHORITATIVE  waves come from real fault domains through the ONE
                 `plan_waves()`; nothing is duplicated into CC
  DETERMINISTIC  the same request against the same state yields the same
                 plan and the same hash, which is what lets an approval
                 bind to a plan at all

It also proves what does NOT travel: domain identities stay here. CC
receives membership and a count, because a Central Command that mirrored
this site's topology would be a second representation of something only
this tier owns.
"""

from __future__ import annotations

import pytest

from harkeniq.proto import harkeniq_pb2
from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.models import Device, DomainMembership, FaultDomain, Site
from harkeniq_sm.grpc_server import SiteManagerServiceServicer

CC_SITE_ID = "cc-site-1"


@pytest.fixture
async def planning(db):
    """One site, four devices, two power domains.

    d1+d2 share domain A, d3+d4 share domain B — so a correct planner
    must split each pair across waves, and two waves is the minimum.
    """
    async with db() as session:
        site = Site(name="site-1", cc_site_id=CC_SITE_ID, status="active")
        session.add(site)
        await session.flush()
        for i in range(1, 5):
            session.add(Device(
                id=f"dev-{i}", site_id=site.id, agent_id=f"node-{i}",
                agent_name=f"node-{i}", device_class="server",
            ))
        dom_a = FaultDomain(id="dom-a", site_id=site.id, name="pdu-a", kind="power")
        dom_b = FaultDomain(id="dom-b", site_id=site.id, name="pdu-b", kind="power")
        session.add_all([dom_a, dom_b])
        await session.flush()
        session.add_all([
            DomainMembership(domain_id="dom-a", device_id="dev-1"),
            DomainMembership(domain_id="dom-a", device_id="dev-2"),
            DomainMembership(domain_id="dom-b", device_id="dev-3"),
            DomainMembership(domain_id="dom-b", device_id="dev-4"),
        ])
        await session.commit()
    config = SMConfig(insecure=True, site_name="site-1")
    return SiteManagerServiceServicer(db, None, config)


def _request(**kw):
    base = dict(
        tenant_id="t1", site_id=CC_SITE_ID, campaign_id="camp-1",
        campaign_version=1, action_type="IDENTIFY_LED",
        device_agent_ids=["node-1", "node-2", "node-3", "node-4"],
        max_wave_size=5,
    )
    base.update(kw)
    return harkeniq_pb2.CampaignPlanRequest(**base)


async def _tables(db) -> dict:
    """Row counts for every table, for the read-only proof."""
    from sqlalchemy import text

    async with db() as session:
        names = [
            r[0] for r in (
                await session.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
            ).all()
        ]
        return {
            n: (await session.execute(text(f"SELECT count(*) FROM {n}"))).scalar()
            for n in names
        }


class TestReadOnly:
    async def test_the_handler_writes_nothing_at_all(self, db, planning):
        """Not a directive, not a plan row, not an audit entry.

        This is what makes 'read-only' a property rather than a claim: if
        planning ever grows a write, this test fails and whoever added it
        has to say why the contract changed.
        """
        before = await _tables(db)
        plan = await planning.PlanCampaignWaves(_request(), None)
        assert plan.planned is True
        assert await _tables(db) == before

    async def test_planning_twice_still_writes_nothing(self, db, planning):
        await planning.PlanCampaignWaves(_request(), None)
        before = await _tables(db)
        await planning.PlanCampaignWaves(_request(), None)
        assert await _tables(db) == before


class TestFaultDomainSeparation:
    async def test_devices_sharing_a_domain_are_never_in_one_wave(self, planning):
        """The safety property the whole contract exists to carry."""
        plan = await planning.PlanCampaignWaves(_request(), None)
        waves = {w.wave_index: set(w.device_agent_ids) for w in plan.waves}
        for members in waves.values():
            assert not {"node-1", "node-2"} <= members
            assert not {"node-3", "node-4"} <= members

    async def test_every_requested_device_is_planned_exactly_once(self, planning):
        plan = await planning.PlanCampaignWaves(_request(), None)
        placed = [d for w in plan.waves for d in w.device_agent_ids]
        assert sorted(placed) == ["node-1", "node-2", "node-3", "node-4"]
        assert len(placed) == len(set(placed))

    async def test_wave_size_cap_is_respected(self, planning):
        plan = await planning.PlanCampaignWaves(_request(max_wave_size=1), None)
        assert all(len(w.device_agent_ids) <= 1 for w in plan.waves)
        assert len(plan.waves) == 4

    async def test_the_separation_rule_is_stated(self, planning):
        plan = await planning.PlanCampaignWaves(_request(), None)
        assert "one device per fault domain per wave" in plan.separation_rule


class TestNoTopologyLeavesTheSite:
    async def test_domain_identities_are_not_returned(self, planning):
        """CC gets a COUNT, never the domain ids.

        Central Command mirroring this site's topology would make it a
        second representation of something only this tier owns.
        """
        plan = await planning.PlanCampaignWaves(_request(), None)
        blob = plan.SerializeToString()
        assert b"dom-a" not in blob
        assert b"dom-b" not in blob
        assert b"pdu-a" not in blob

    async def test_domain_span_is_a_count(self, planning):
        plan = await planning.PlanCampaignWaves(_request(), None)
        for w in plan.waves:
            assert w.domain_span == len(set(w.device_agent_ids))


class TestDeterminism:
    async def test_the_same_request_yields_the_same_hash(self, planning):
        a = await planning.PlanCampaignWaves(_request(), None)
        b = await planning.PlanCampaignWaves(_request(), None)
        assert a.plan_hash == b.plan_hash
        assert a.plan_hash

    async def test_request_order_does_not_change_the_plan(self, planning):
        a = await planning.PlanCampaignWaves(_request(), None)
        b = await planning.PlanCampaignWaves(
            _request(device_agent_ids=["node-4", "node-2", "node-3", "node-1"]),
            None,
        )
        assert a.plan_hash == b.plan_hash

    async def test_a_different_device_set_changes_the_hash(self, planning):
        a = await planning.PlanCampaignWaves(_request(), None)
        b = await planning.PlanCampaignWaves(
            _request(device_agent_ids=["node-1", "node-2"]), None
        )
        assert a.plan_hash != b.plan_hash

    async def test_a_different_campaign_version_changes_the_hash(self, planning):
        a = await planning.PlanCampaignWaves(_request(), None)
        b = await planning.PlanCampaignWaves(_request(campaign_version=2), None)
        assert a.plan_hash != b.plan_hash

    async def test_a_domain_change_changes_the_plan(self, db, planning):
        """The mechanism that refuses a stale approval.

        Moving a device between domains alters what may run together, so
        the plan must change -- which is how Central Command detects that
        an approved blast radius no longer describes the estate.
        """
        before = await planning.PlanCampaignWaves(_request(), None)
        async with db() as session:
            session.add(DomainMembership(domain_id="dom-a", device_id="dev-3"))
            await session.commit()
        after = await planning.PlanCampaignWaves(_request(), None)
        assert before.plan_hash != after.plan_hash


class TestUnresolvedAndUnplannable:
    async def test_an_unresolved_site_is_not_an_empty_estate(self, planning):
        """planned=False must never read as 'this site has no devices'."""
        plan = await planning.PlanCampaignWaves(
            _request(site_id="cc-site-does-not-exist"), None
        )
        assert plan.planned is False
        assert plan.reason
        assert list(plan.waves) == []

    async def test_a_device_at_another_site_is_reported_not_dropped(self, planning):
        plan = await planning.PlanCampaignWaves(
            _request(device_agent_ids=["node-1", "stranger"]), None
        )
        assert plan.planned is True
        assert list(plan.unplannable_device_ids) == ["stranger"]
        placed = [d for w in plan.waves for d in w.device_agent_ids]
        assert placed == ["node-1"]

    async def test_the_plan_never_contains_an_unrequested_device(self, planning):
        plan = await planning.PlanCampaignWaves(
            _request(device_agent_ids=["node-1"]), None
        )
        placed = {d for w in plan.waves for d in w.device_agent_ids}
        assert placed == {"node-1"}
