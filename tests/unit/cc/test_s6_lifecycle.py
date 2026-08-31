"""S6 end to end: campaign lifecycle over a REAL Site Manager servicer.

Direct-servicer and pure tests cannot prove the thing that matters here:
that Central Command and the Site Manager agree about a plan across the
wire, and that an approval taken against one plan cannot authorize work
under another. So this drives a real gRPC servicer with real fault
domains, through create -> preflight -> acknowledge -> submit -> approve
-> advance -> settle.
"""

from __future__ import annotations

import grpc
import pytest

from harkeniq.capabilities import declare
from harkeniq.proto import harkeniq_pb2_grpc
from harkeniq_cc.campaign_runner import (
    advance_campaign,
    build_waves,
    campaign_actor,
    plan_sites,
    preflight,
)
from harkeniq_cc.campaigns import WAVE_APPROVED, WAVE_PENDING_APPROVAL, WAVE_VOIDED
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCFleetCache, CCSite
from harkeniq_cc.db.repos import CampaignRepo
from harkeniq_cc.runtime import AppState
from harkeniq_sm.approvals import ApprovalService
from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.base import (
    create_all as sm_create_all,
    make_engine as sm_engine,
    make_sessionmaker as sm_sessionmaker,
)
from harkeniq_sm.db.models import Device, DomainMembership, FaultDomain, Site
from harkeniq_sm.grpc_server import SiteManagerServiceServicer

TENANT = "t1"
CC_SITE_ID = "cc-site-1"
SERVER = declare("redfish", ["IDENTIFY_LED", "COLLECT_DIAGNOSTICS"], "server")


@pytest.fixture
async def stack():
    """A real SM on a real port, and a CC pointed at it."""
    sm_db_engine = sm_engine("sqlite+aiosqlite:///:memory:")
    await sm_create_all(sm_db_engine)
    sm_db = sm_sessionmaker(sm_db_engine)
    async with sm_db() as session:
        site = Site(name="site-1", cc_site_id=CC_SITE_ID, status="active")
        session.add(site)
        await session.flush()
        for i in (1, 2, 3):
            session.add(Device(
                id=f"dev-{i}", site_id=site.id, agent_id=f"node-{i}",
                agent_name=f"node-{i}", device_class="server",
            ))
        session.add(FaultDomain(
            id="dom-a", site_id=site.id, name="pdu-a", kind="power",
        ))
        await session.flush()
        # node-1 and node-2 share a domain, so they can never share a wave.
        session.add_all([
            DomainMembership(domain_id="dom-a", device_id="dev-1"),
            DomainMembership(domain_id="dom-a", device_id="dev-2"),
        ])
        await session.commit()

    sm_config = SMConfig(insecure=True, site_name="site-1")
    servicer = SiteManagerServiceServicer(
        sm_db, ApprovalService(sm_db, sm_config), sm_config,
    )
    server = grpc.aio.server()
    harkeniq_pb2_grpc.add_SiteManagerServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    cc_db_engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(cc_db_engine)
    cc_db = make_sessionmaker(cc_db_engine)
    async with cc_db() as session:
        cc_site = CCSite(
            id=CC_SITE_ID, tenant_id=TENANT, site_name="DC-1",
            sm_endpoint=f"127.0.0.1:{port}", sm_token="tok",
        )
        session.add(cc_site)
        await session.flush()
        for i in (1, 2, 3):
            session.add(CCFleetCache(
                site_id=CC_SITE_ID, agent_id=f"node-{i}", agent_name=f"node-{i}",
                vendor="Dell", model="R750", device_class="server",
                observation="observed", health="OK", capabilities=SERVER,
            ))
        await session.commit()

    state = AppState(
        config=CCConfig(tenant_id=TENANT, insecure=True),
        engine=cc_db_engine, sessionmaker=cc_db,
    )
    yield state, cc_db, sm_db
    await server.stop(grace=None)
    await sm_db_engine.dispose()
    await cc_db_engine.dispose()


async def _campaign(session, action="IDENTIFY_LED"):
    repo = CampaignRepo(session)
    campaign = await repo.create(
        tenant_id=TENANT, name="Q3", action_type=action,
        params={"target": "Drive 0"}, created_by="ops@example.com",
    )
    await repo.replace_scopes(campaign.id, [("site", CC_SITE_ID)])
    await session.commit()
    return campaign


async def _preflight(state, session, campaign):
    return await preflight(
        session, state, tenant_id=TENANT, campaign=campaign,
        scope_rules=await CampaignRepo(session).scopes(campaign.id),
        resolved_site_ids=[], actor="ops@example.com",
    )


class TestPlanningOverTheWire:
    @pytest.mark.asyncio
    async def test_preflight_stores_the_sites_own_plan(self, stack):
        state, cc_db, _ = stack
        async with cc_db() as session:
            campaign = await _campaign(session)
            summary = await _preflight(state, session, campaign)
            await session.commit()
            assert summary["eligible"] == 3
            assert summary["plans"]["planned"] == 1

            plan = await CampaignRepo(session).current_plan(campaign.id, CC_SITE_ID)
            assert plan is not None and plan.plan_hash
            # The safety property, computed at the site and carried here.
            for wave in plan.waves:
                members = set(wave["device_agent_ids"])
                assert not {"node-1", "node-2"} <= members

    @pytest.mark.asyncio
    async def test_no_domain_identity_is_stored_at_central_command(self, stack):
        """CC holds membership and a count, never the site's topology."""
        state, cc_db, _ = stack
        async with cc_db() as session:
            campaign = await _campaign(session)
            await _preflight(state, session, campaign)
            await session.commit()
            plan = await CampaignRepo(session).current_plan(campaign.id, CC_SITE_ID)
            blob = str(plan.waves)
            assert "dom-a" not in blob and "pdu-a" not in blob

    @pytest.mark.asyncio
    async def test_replanning_is_idempotent(self, stack):
        state, cc_db, _ = stack
        async with cc_db() as session:
            campaign = await _campaign(session)
            await _preflight(state, session, campaign)
            await session.commit()
            first = (await CampaignRepo(session).current_plan(
                campaign.id, CC_SITE_ID)).plan_hash
            await _preflight(state, session, campaign)
            await session.commit()
            second = (await CampaignRepo(session).current_plan(
                campaign.id, CC_SITE_ID)).plan_hash
        assert first == second


class TestWavesAndApproval:
    @pytest.mark.asyncio
    async def test_submit_raises_one_subject_per_site_wave(self, stack):
        """Q1: all at submit, so the decision set is known up front."""
        state, cc_db, _ = stack
        async with cc_db() as session:
            campaign = await _campaign(session)
            await _preflight(state, session, campaign)
            built = await build_waves(
                session, tenant_id=TENANT, campaign=campaign, autonomous=False,
            )
            await session.commit()
            assert built["waves"] >= 2
            assert built["awaiting_approval"] == built["waves"]
            waves = await CampaignRepo(session).waves(campaign.id)
            assert all(w.status == WAVE_PENDING_APPROVAL for w in waves)
            # Every subject is distinct and addressable.
            refs = [w.subject_ref for w in waves]
            assert len(set(refs)) == len(refs) and all(refs)

    @pytest.mark.asyncio
    async def test_an_autonomous_class_raises_no_approval_subject(self, stack):
        """No human decision to record, so no record is manufactured."""
        state, cc_db, _ = stack
        async with cc_db() as session:
            campaign = await _campaign(session)
            await _preflight(state, session, campaign)
            built = await build_waves(
                session, tenant_id=TENANT, campaign=campaign, autonomous=True,
            )
            await session.commit()
            assert built["awaiting_approval"] == 0
            waves = await CampaignRepo(session).waves(campaign.id)
            assert all(w.subject_ref == "" for w in waves)


class TestPlanChangeRefusesTheWave:
    @pytest.mark.asyncio
    async def test_a_domain_change_after_approval_refuses_and_voids(self, stack):
        """The governance invariant, proven across the wire.

        A device joins a fault domain after the wave was approved. The
        re-plan therefore differs, so the approved plan no longer
        describes the estate -- the wave is REFUSED rather than narrowed,
        and the site's later authorizations are voided.
        """
        state, cc_db, sm_db = stack
        async with cc_db() as session:
            campaign = await _campaign(session)
            await _preflight(state, session, campaign)
            await build_waves(
                session, tenant_id=TENANT, campaign=campaign, autonomous=False,
            )
            repo = CampaignRepo(session)
            for wave in await repo.waves(campaign.id):
                wave.status = WAVE_APPROVED
                wave.decided_by = "ops@example.com"
            campaign.status = "running"
            await session.commit()

        # The estate changes underneath the approval.
        async with sm_db() as session:
            session.add(DomainMembership(domain_id="dom-a", device_id="dev-3"))
            await session.commit()

        async with cc_db() as session:
            campaign = await CampaignRepo(session).get(TENANT, campaign.id)
            result = await advance_campaign(
                session, state, tenant_id=TENANT, campaign=campaign,
            )
            await session.commit()
            assert any(
                "plan changed" in (b.get("reason") or "")
                for b in result["blocked"]
            ), result
            waves = await CampaignRepo(session).waves(campaign.id)
            assert all(w.status == WAVE_VOIDED for w in waves), (
                "a changed plan must void the authorizations built on it"
            )

    @pytest.mark.asyncio
    async def test_an_unchanged_plan_dispatches(self, stack):
        state, cc_db, _ = stack
        async with cc_db() as session:
            campaign = await _campaign(session)
            await _preflight(state, session, campaign)
            await build_waves(
                session, tenant_id=TENANT, campaign=campaign, autonomous=False,
            )
            repo = CampaignRepo(session)
            for wave in await repo.waves(campaign.id):
                wave.status = WAVE_APPROVED
            campaign.status = "running"
            await session.commit()

            result = await advance_campaign(
                session, state, tenant_id=TENANT, campaign=campaign,
            )
            await session.commit()
            # The SM has no directive transport in this fixture, so the
            # dispatch is refused there -- but the wave got PAST the plan
            # and capability gates, which is what this asserts.
            assert result["advanced"], result
            assert not any(
                "plan changed" in (b.get("reason") or "")
                for b in result["blocked"]
            )


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_advancing_twice_does_not_dispatch_twice(self, stack):
        """A repeated POST /advance, a loop tick and a restart all take
        this path; the ledger's composite key is what makes a duplicate
        physically unable to exist."""
        state, cc_db, _ = stack
        async with cc_db() as session:
            campaign = await _campaign(session)
            await _preflight(state, session, campaign)
            await build_waves(
                session, tenant_id=TENANT, campaign=campaign, autonomous=False,
            )
            repo = CampaignRepo(session)
            for wave in await repo.waves(campaign.id):
                wave.status = WAVE_APPROVED
            campaign.status = "running"
            await session.commit()

            await advance_campaign(session, state, tenant_id=TENANT, campaign=campaign)
            await session.commit()
            first = len(await repo.dispatches(campaign.id))
            await advance_campaign(session, state, tenant_id=TENANT, campaign=campaign)
            await session.commit()
            second = len(await repo.dispatches(campaign.id))
        assert first == second, "a second advance re-dispatched a wave"

    @pytest.mark.asyncio
    async def test_the_campaign_actor_is_versioned(self, stack):
        state, cc_db, _ = stack
        async with cc_db() as session:
            campaign = await _campaign(session)
        assert campaign_actor(campaign.id, 1).endswith("@v1")
        assert campaign_actor(campaign.id, 2) != campaign_actor(campaign.id, 1)


class TestWaveSettlement:
    @pytest.mark.asyncio
    async def test_a_wave_that_reached_no_device_fails_and_halts(self, stack):
        """Regression: found by the idempotency test.

        Settlement originally derived "expected" from targets still in
        `dispatched` state. When the Site Manager refused every dispatch
        those targets were already `failed`, so the expected set was
        empty, the wave looked complete, and the site walked forward over
        work that never ran. A wave that reached no device is a failure.
        """
        from harkeniq_cc.campaign_runner import settle_dispatched_waves

        state, cc_db, _ = stack
        async with cc_db() as session:
            campaign = await _campaign(session)
            await _preflight(state, session, campaign)
            await build_waves(
                session, tenant_id=TENANT, campaign=campaign, autonomous=False,
            )
            repo = CampaignRepo(session)
            for wave in await repo.waves(campaign.id):
                wave.status = WAVE_APPROVED
            campaign.status = "running"
            await session.commit()

            # This fixture's SM has no directive transport, so every
            # dispatch is refused at the site.
            await advance_campaign(session, state, tenant_id=TENANT, campaign=campaign)
            await session.commit()

            result = await settle_dispatched_waves(
                session, tenant_id=TENANT, campaign=campaign,
            )
            await session.commit()
            assert result["completed"] == 0, "a wave that ran nothing is not complete"

            site = await CampaignRepo(session).get_site(campaign.id, CC_SITE_ID)
            assert site.status == "halted"
            assert "reached no device" in site.halt_reason

    @pytest.mark.asyncio
    async def test_a_halted_site_voids_its_remaining_waves(self, stack):
        """Q3, end to end: stale authorization is never left standing."""
        from harkeniq_cc.campaign_runner import settle_dispatched_waves

        state, cc_db, _ = stack
        async with cc_db() as session:
            campaign = await _campaign(session)
            await _preflight(state, session, campaign)
            await build_waves(
                session, tenant_id=TENANT, campaign=campaign, autonomous=False,
            )
            repo = CampaignRepo(session)
            for wave in await repo.waves(campaign.id):
                wave.status = WAVE_APPROVED
            campaign.status = "running"
            await session.commit()

            await advance_campaign(session, state, tenant_id=TENANT, campaign=campaign)
            await settle_dispatched_waves(
                session, tenant_id=TENANT, campaign=campaign,
            )
            await session.commit()

            waves = await CampaignRepo(session).waves(campaign.id)
            later = [w for w in waves if w.wave_index > 0]
            assert later, "fixture should plan more than one wave"
            assert all(w.status == WAVE_VOIDED for w in later)
            assert all("site halted" in w.void_reason for w in later)
