"""S3-E1 (A30.37): campaigns -- R4 at submission, final eligibility at advance.

A real Site Manager on a real port plans the waves; Central Command submits
through its own route and advances through its own runner.

* R4: a campaign is autonomous only if EVERY site in its plan is locally
  autonomous; one that is not makes it require approval; every site denied
  refuses it. A site that is NOT in the plan never moves it, and the global
  gate never converts its mode (answer 1).
* Advance: the gate holds approved AND autonomous waves (D3), an autonomous
  wave re-asks its site, and a refusal WITHHOLDS in S1's shape -- plan,
  ledger and approval subject untouched, one audit entry per distinct cause
  -- and the same wave dispatches when eligibility returns.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import grpc
import httpx
import pytest
import sqlalchemy as sa

from harkeniq.capabilities import declare
from harkeniq.proto import harkeniq_pb2_grpc
from harkeniq_cc import global_safety as G
from harkeniq_cc.agent_activation import SITE_LOCAL_WITHHELD_REASON
from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.approval_policy import SUBJECT_CAMPAIGN_WAVE
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.campaign_runner import advance_campaign, preflight
from harkeniq_cc.campaigns import (
    REVAL_EXECUTION_WITHHELD,
    WAVE_APPROVED,
    WAVE_PENDING_APPROVAL,
)
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import (
    CCApprovalRecord,
    CCAuditLog,
    CCAutonomyBudget,
    CCFleetCache,
    CCSafetyState,
    CCSite,
)
from harkeniq_cc.db.repos import ApprovalRecordRepo, CampaignRepo, ScopeGrantRepo
from harkeniq_cc.runtime import AppState
from harkeniq_sm.approvals import ApprovalService
from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.base import (
    create_all as sm_create_all,
    make_engine as sm_engine,
    make_sessionmaker as sm_sessionmaker,
)
from harkeniq_sm.db.models import Device, Site
from harkeniq_sm.grpc_server import SiteManagerServiceServicer

from tests.unit.cc.conftest import ConsoleRealm

TENANT = "t1"
OWNER = ("kc-owner", "owner@example.com")
PLAN_SITE = "cc-plan-site"
HIDDEN_SITE = "cc-hidden-site"
SERVER = declare("redfish", ["SEL_CLEAR", "IDENTIFY_LED"], "server")


class Toggle:
    member_id = "test_toggle"

    def __init__(self):
        self.on = False

    def evaluate(self, context):
        return G.MemberVerdict.CONSTRAIN if self.on else G.MemberVerdict.CLEAR


@pytest.fixture
async def world(monkeypatch):
    realm = await ConsoleRealm(TENANT).start()
    realm.person(OWNER[0], "tenant_owner")
    realm.wire(monkeypatch)
    toggle = Toggle()
    monkeypatch.setattr(G, "_ACTIVE", (toggle,))

    sm_db_engine = sm_engine("sqlite+aiosqlite:///:memory:")
    await sm_create_all(sm_db_engine)
    sm_db = sm_sessionmaker(sm_db_engine)
    async with sm_db() as session:
        site = Site(name="plan-site", cc_site_id=PLAN_SITE, status="active")
        session.add(site)
        await session.flush()
        for i in (1, 2):
            session.add(Device(id=f"dev-{i}", site_id=site.id, agent_id=f"node-{i}",
                               agent_name=f"node-{i}", device_class="server"))
        await session.commit()
    sm_config = SMConfig(insecure=True, site_name="plan-site")
    servicer = SiteManagerServiceServicer(sm_db, ApprovalService(sm_db, sm_config), sm_config)
    server = grpc.aio.server()
    harkeniq_pb2_grpc.add_SiteManagerServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    configure_auth("", "", "", insecure=True)
    cc_db_engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(cc_db_engine)
    cc_db = make_sessionmaker(cc_db_engine)
    now = datetime.now(timezone.utc)
    async with cc_db() as session:
        for site_id, endpoint in ((PLAN_SITE, f"127.0.0.1:{port}"),
                                  (HIDDEN_SITE, "127.0.0.1:1")):
            session.add(CCSite(id=site_id, tenant_id=TENANT, site_name=site_id,
                               sm_endpoint=endpoint, sm_token="tok"))
            session.add(CCSafetyState(
                site_id=site_id, tenant_id=TENANT, reported=True, as_of=now,
                ingested_at=now, sm_stop_switch=False, suppressions=[],
                error_budgets=[], site_budgets={},
            ))
        await session.flush()
        for i in (1, 2):
            session.add(CCFleetCache(
                site_id=PLAN_SITE, agent_id=f"node-{i}", agent_name=f"node-{i}",
                vendor="Dell", model="R750", device_class="server",
                observation="observed", health="OK", capabilities=SERVER,
            ))
        session.add(CCFleetCache(
            site_id=HIDDEN_SITE, agent_id="node-hidden", agent_name="node-hidden",
            vendor="Dell", model="R750", device_class="server",
            observation="observed", health="OK", capabilities=SERVER,
        ))
        session.add(CCAutonomyBudget(tenant_id=TENANT, device_type="*", level=2,
                                     budget_limit=100, budget_period="daily"))
        await ScopeGrantRepo(session).seed_first_grant(
            tenant_id=TENANT, principal_ref=OWNER[0], role="tenant_owner",
            realm="", granted_by="system:tenant_birth", note="test fixture",
        )
        await session.commit()

    state = AppState(config=CCConfig(tenant_id=TENANT, insecure=True),
                     engine=cc_db_engine, sessionmaker=cc_db)
    realm.attach(state)
    app = create_app(state)

    async def _owner():
        return UserContext(user_id=OWNER[0], email=OWNER[1], tenant_id=TENANT,
                           role="tenant_owner",
                           permissions=list(ROLE_PERMISSIONS["tenant_owner"]))

    app.dependency_overrides[get_current_user] = _owner
    yield state, cc_db, app, toggle
    await server.stop(grace=None)
    await realm.stop()
    await sm_db_engine.dispose()
    await cc_db_engine.dispose()


async def _safety(cc_db, site_id, **fields):
    async with cc_db() as session:
        row = await session.get(CCSafetyState, site_id)
        for name, value in fields.items():
            setattr(row, name, value)
        await session.commit()


async def _ready(state, cc_db, action="SEL_CLEAR") -> str:
    async with cc_db() as session:
        repo = CampaignRepo(session)
        campaign = await repo.create(tenant_id=TENANT, name="Q4", action_type=action,
                                     params={}, created_by=OWNER[1])
        await repo.replace_scopes(campaign.id, [("site", PLAN_SITE)])
        await session.commit()
        await preflight(session, state, tenant_id=TENANT, campaign=campaign,
                        scope_rules=await repo.scopes(campaign.id),
                        resolved_site_ids=[], actor=OWNER[1])
        await session.commit()
        return campaign.id


async def _submit(app, campaign_id):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        return await client.post(f"/api/campaigns/{campaign_id}/submit")


# ---------------------------------------------------------------------------
# R4 at submission
# ---------------------------------------------------------------------------


class TestSubmission:
    async def test_every_plan_site_autonomous_is_autonomous(self, world):
        state, cc_db, app, _ = world
        res = await _submit(app, await _ready(state, cc_db))
        assert res.status_code == 200, res.text
        assert (res.json()["disposition"], res.json()["requires_human_approval"]) == (
            "autonomous", False)

    async def test_a_site_outside_the_plan_never_moves_it(self, world):
        state, cc_db, app, _ = world
        await _safety(cc_db, HIDDEN_SITE, sm_stop_switch=True, reported=False,
                      error_budgets=[{"action_type": "SEL_CLEAR", "dropped_back": True,
                                      "total_count": 9, "success_count": 1,
                                      "failure_count": 8}],
                      site_budgets={"SEL_CLEAR": 0})
        res = await _submit(app, await _ready(state, cc_db))
        assert res.json()["disposition"] == "autonomous"

    async def test_a_plan_site_that_is_not_autonomous_requires_approval(self, world):
        state, cc_db, app, _ = world
        await _safety(cc_db, PLAN_SITE, reported=False)
        res = await _submit(app, await _ready(state, cc_db))
        assert res.status_code == 200, res.text
        assert (res.json()["disposition"], res.json()["requires_human_approval"]) == (
            "requires_approval", True)

    async def test_a_plan_whose_every_site_is_halted_is_refused(self, world):
        state, cc_db, app, _ = world
        await _safety(cc_db, PLAN_SITE, sm_stop_switch=True)
        res = await _submit(app, await _ready(state, cc_db))
        assert res.status_code == 409, res.text

    async def test_the_gate_never_converts_the_mode(self, world):
        state, cc_db, app, toggle = world
        toggle.on = True
        res = await _submit(app, await _ready(state, cc_db))
        assert res.json()["disposition"] == "autonomous"


# ---------------------------------------------------------------------------
# Final eligibility at advance
# ---------------------------------------------------------------------------


async def _audit(cc_db, action):
    async with cc_db() as session:
        return (await session.execute(
            sa.select(CCAuditLog).where(CCAuditLog.action == action)
        )).scalars().all()


async def _advance(state, cc_db, campaign_id) -> dict:
    async with cc_db() as session:
        campaign = await CampaignRepo(session).get(TENANT, campaign_id)
        result = await advance_campaign(session, state, tenant_id=TENANT, campaign=campaign)
        await session.commit()
        return result


async def _waves(cc_db, campaign_id):
    async with cc_db() as session:
        return await CampaignRepo(session).waves(campaign_id)


async def _dispatches(cc_db, campaign_id) -> int:
    from harkeniq_cc.db.models import CCCampaignDispatch

    async with cc_db() as session:
        return len((await session.execute(sa.select(CCCampaignDispatch).where(
            CCCampaignDispatch.campaign_id == campaign_id))).scalars().all())


async def _approve(cc_db, campaign_id):
    async with cc_db() as session:
        repo = CampaignRepo(session)
        for wave in await repo.waves(campaign_id):
            if wave.status != WAVE_PENDING_APPROVAL:
                continue
            wave.status = WAVE_APPROVED
            wave.decided_by = OWNER[1]
            await ApprovalRecordRepo(session).record(
                tenant_id=TENANT, subject_type=SUBJECT_CAMPAIGN_WAVE,
                subject_ref=wave.subject_ref, approver_ref=OWNER[0],
                approver_email=OWNER[1], decision="approved",
                authority_snapshot={"role": "tenant_owner", "permission": "action.approve",
                                    "target_site_id": wave.site_id,
                                    "target_device_agent_ids": list(wave.device_agent_ids)},
            )
        await session.commit()


class TestAdvance:
    async def test_the_gate_holds_an_autonomous_wave_then_releases_it(self, world):
        state, cc_db, app, toggle = world
        cid = await _ready(state, cc_db)
        assert (await _submit(app, cid)).json()["disposition"] == "autonomous"
        toggle.on = True
        for _ in range(3):
            result = await _advance(state, cc_db, cid)
            assert not result["advanced"]
            assert result["blocked"][0]["reason"] == G.GLOBAL_WITHHELD_REASON
        assert await _dispatches(cc_db, cid) == 0
        assert len(await _audit(cc_db, "campaign.wave_withheld")) == 1
        async with cc_db() as session:
            targets = await CampaignRepo(session).targets(cid)
        assert {t.revalidation for t in targets} == {REVAL_EXECUTION_WITHHELD}
        toggle.on = False
        assert (await _advance(state, cc_db, cid))["advanced"]
        assert await _dispatches(cc_db, cid) > 0

    async def test_the_gate_holds_an_approved_wave_and_leaves_its_ledger(self, world):
        state, cc_db, app, toggle = world
        await _safety(cc_db, PLAN_SITE, reported=False)       # -> requires approval
        cid = await _ready(state, cc_db)
        assert (await _submit(app, cid)).json()["requires_human_approval"] is True
        await _approve(cc_db, cid)
        before = [(w.id, w.status, w.plan_hash, w.subject_ref)
                  for w in await _waves(cc_db, cid)]
        async with cc_db() as session:
            ledger = len((await session.execute(sa.select(CCApprovalRecord))).scalars().all())
        toggle.on = True
        result = await _advance(state, cc_db, cid)
        assert result["blocked"][0]["reason"] == G.GLOBAL_WITHHELD_REASON
        assert [(w.id, w.status, w.plan_hash, w.subject_ref)
                for w in await _waves(cc_db, cid)] == before
        async with cc_db() as session:
            assert len((await session.execute(
                sa.select(CCApprovalRecord))).scalars().all()) == ledger
        assert await _dispatches(cc_db, cid) == 0

    async def test_an_autonomous_wave_re_asks_its_site(self, world):
        state, cc_db, app, _ = world
        cid = await _ready(state, cc_db)
        assert (await _submit(app, cid)).json()["disposition"] == "autonomous"
        stale = datetime.now(timezone.utc) - timedelta(hours=2)
        await _safety(cc_db, PLAN_SITE, as_of=stale, ingested_at=stale)
        result = await _advance(state, cc_db, cid)
        assert result["blocked"][0]["reason"] == SITE_LOCAL_WITHHELD_REASON
        assert await _dispatches(cc_db, cid) == 0

    async def test_an_approved_wave_at_a_halted_site_is_withheld(self, world):
        state, cc_db, app, _ = world
        await _safety(cc_db, PLAN_SITE, reported=False)
        cid = await _ready(state, cc_db)
        await _submit(app, cid)
        await _approve(cc_db, cid)
        await _safety(cc_db, PLAN_SITE, sm_stop_switch=True)
        result = await _advance(state, cc_db, cid)
        assert result["blocked"][0]["reason"] == SITE_LOCAL_WITHHELD_REASON
        assert await _dispatches(cc_db, cid) == 0

    async def test_a_hidden_site_never_holds_a_wave(self, world):
        state, cc_db, app, _ = world
        cid = await _ready(state, cc_db)
        await _submit(app, cid)
        await _safety(cc_db, HIDDEN_SITE, sm_stop_switch=True, reported=False)
        assert (await _advance(state, cc_db, cid))["advanced"]
