"""A6-4B0b-S1 (A30.22) on a REAL PostgreSQL: every target, current authority.

The sqlite matrix lives in tests/unit/cc/test_a30_22_multi_target_authority.py.
This file asks what sqlite cannot answer honestly: on the engine production
runs, with JSONB ledgers and timestamptz grant lifecycles, does a wave over
[A, B, C] refuse a principal covering [A] and [A, B], allow one covering all
three, record the SET on the ledger -- and, once approved, refuse to
dispatch the moment one approver's grant is revoked, then dispatch again
when only that grant is restored?

The Site Manager is faked at the client boundary (plan and dispatch); the
approval route, the resolver, the ledger, the campaign runner and the
dispatch ledger are real.

Audit rows are deliberately NOT cleaned up: the chain is append-only.

Gated on ``HARKEN_TEST_CC_PG_DSN``.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa

from harkeniq.capabilities import declare
from harkeniq_cc import campaign_runner
from harkeniq_cc.api import approvals as approvals_api
from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.approval_policy import SUBJECT_CAMPAIGN_WAVE
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.campaign_runner import advance_campaign
from harkeniq_cc.campaigns import (
    REVAL_AUTHORITY_LOST,
    WAVE_APPROVED,
    WAVE_DISPATCHED,
    WAVE_PENDING_APPROVAL,
    wave_subject_ref,
)
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCAuditLog, CCFleetCache, CCScopeGrant, CCSite
from harkeniq_cc.db.repos import ApprovalRecordRepo, CampaignRepo, ScopeGrantRepo
from harkeniq_cc.runtime import AppState
from harkeniq_cc.scope import SCOPE_DEVICE

from tests.unit.cc.conftest import ConsoleRealm, seed_tenant_admin

DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")
PLAN = "pg-plan-hash"
SERVER = declare("redfish", ["IDENTIFY_LED", "COLLECT_DIAGNOSTICS"], "server")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="HARKEN_TEST_CC_PG_DSN not set"),
]


class FakeSM:
    """The Site Manager at the client boundary: plans deterministically and
    accepts every dispatch, so what this file measures is Central Command."""

    dispatches: list = []

    def __init__(self, *_a, **_kw):
        pass

    async def plan_campaign_waves(self, endpoint, token, **kw):
        return {"planned": True, "plan_hash": PLAN}

    async def dispatch_action(self, endpoint, token, **kw):
        FakeSM.dispatches.append(kw)
        return {"accepted": True, "directive_id": uuid.uuid4().hex, "reason": ""}


@pytest.fixture(autouse=True)
def _fake_sm(monkeypatch):
    FakeSM.dispatches = []
    monkeypatch.setattr(campaign_runner, "SMClient", FakeSM)
    monkeypatch.setattr(approvals_api, "SMClient", FakeSM)
    yield


#: A30.23: the identity plane each test's Central Command asks, at
#: dispatch, for every approver's CURRENT realm roles. One per test; the
#: real Console internal router in secure mode over the Keycloak double.
_REALM: dict = {}


@pytest.fixture(autouse=True)
async def _identity_plane(monkeypatch):
    realm = await ConsoleRealm(f"s1-realm-{uuid.uuid4().hex[:8]}").start()
    realm.person("kc-owner", "tenant_owner")
    realm.person("kc-abc", "operator")
    realm.wire(monkeypatch)
    _REALM["current"] = realm
    yield realm
    _REALM.pop("current", None)
    await realm.stop()


class Stack:
    def __init__(self, app, state, tenant):
        self.app, self.state, self.tenant = app, state, tenant
        self.sessionmaker = state.sessionmaker
        self.persona = ("kc-owner", "owner@example.com", "tenant_owner")

    def as_person(self, sub, role="operator"):
        self.persona = (sub, f"{sub}@example.com", role)
        return self

    def client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
        )


async def _stack() -> Stack:
    tenant = f"s1-{uuid.uuid4().hex[:10]}"
    configure_auth("", "", "", insecure=True)
    engine = make_engine(DSN)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(
        config=CCConfig(tenant_id=tenant, insecure=True),
        engine=engine, sessionmaker=sessionmaker,
    )
    app = create_app(state)
    await seed_tenant_admin(sessionmaker, tenant, "kc-owner")
    _REALM["current"].attach(state)
    stack = Stack(app, state, tenant)

    async def _fake():
        sub, email, role = stack.persona
        return UserContext(
            user_id=sub, email=email, tenant_id=tenant, role=role,
            permissions=list(ROLE_PERMISSIONS[role]),
        )

    app.dependency_overrides[get_current_user] = _fake
    return stack


async def _estate(stack) -> dict:
    """One site, three devices, one running campaign whose only wave names
    all three -- built the way preflight + submit leave them."""
    tenant = stack.tenant
    devices = [f"node-{uuid.uuid4().hex[:8]}" for _ in range(3)]
    async with stack.sessionmaker() as session:
        site = CCSite(tenant_id=tenant, site_name=f"pg-{uuid.uuid4().hex[:6]}",
                      sm_endpoint="sm:50051", sm_token="tok")
        session.add(site)
        await session.flush()
        for d in devices:
            session.add(CCFleetCache(
                site_id=site.id, agent_id=d, agent_name=d, vendor="Dell",
                model="R750", device_class="server", observation="observed",
                health="OK", capabilities=SERVER,
            ))
        repo = CampaignRepo(session)
        campaign = await repo.create(
            tenant_id=tenant, name="pg-s1", action_type="IDENTIFY_LED",
            params={"target": "Drive 0"}, created_by="ops@example.com",
            status="running",
        )
        await repo.replace_scopes(campaign.id, [("site", site.id)])
        await repo.replace_targets(campaign.id, [
            {"site_id": site.id, "device_agent_id": d, "device_name": d,
             "device_class": "server", "applicability": "eligible"}
            for d in devices
        ])
        await repo.replace_sites(campaign.id, [{
            "site_id": site.id, "site_name": site.site_name, "status": "pending",
            "order_index": 0, "wave_count": 1,
        }])
        subject = wave_subject_ref(campaign.id, campaign.version, site.id, 0, devices, PLAN)
        await repo.add_wave(
            campaign_id=campaign.id, campaign_version=campaign.version,
            site_id=site.id, wave_index=0, plan_hash=PLAN,
            device_agent_ids=sorted(devices), domain_span=3,
            subject_ref=subject, status=WAVE_PENDING_APPROVAL,
        )
        await session.commit()
        return {"site": site.id, "devices": sorted(devices),
                "campaign_id": campaign.id, "subject": subject}


async def _grant(stack, sub, *devices, expires_at=None):
    async with stack.sessionmaker() as session:
        for d in devices:
            await ScopeGrantRepo(session).grant(
                tenant_id=stack.tenant, principal_type="user", principal_ref=sub,
                scope_type=SCOPE_DEVICE, scope_ref=d, role="operator",
                granted_by="kc-owner", expires_at=expires_at,
            )
        await session.commit()


async def _revoke(stack, sub, device):
    async with stack.sessionmaker() as session:
        (row,) = (await session.execute(
            sa.select(CCScopeGrant).where(
                CCScopeGrant.tenant_id == stack.tenant,
                CCScopeGrant.principal_ref == sub,
                CCScopeGrant.scope_ref == device,
                CCScopeGrant.revoked_at.is_(None),
            )
        )).scalars().all()
        await ScopeGrantRepo(session).revoke(row, "kc-owner")
        await session.commit()


async def _approve(stack, subject):
    async with stack.client() as c:
        return await c.post(f"/api/approvals/{subject}/approve")


async def _records(stack, subject):
    async with stack.sessionmaker() as session:
        return list(await ApprovalRecordRepo(session).list_for_subject(
            SUBJECT_CAMPAIGN_WAVE, subject,
        ))


async def _advance(stack, campaign_id):
    async with stack.sessionmaker() as session:
        campaign = await CampaignRepo(session).get(stack.tenant, campaign_id)
        result = await advance_campaign(
            session, stack.state, tenant_id=stack.tenant, campaign=campaign,
        )
        await session.commit()
        return result


async def _wave(stack, campaign_id):
    async with stack.sessionmaker() as session:
        (wave,) = await CampaignRepo(session).waves(campaign_id)
        return wave


async def _withheld(stack, campaign_id):
    async with stack.sessionmaker() as session:
        return (await session.execute(
            sa.select(sa.func.count()).select_from(CCAuditLog).where(
                CCAuditLog.tenant_id == stack.tenant,
                CCAuditLog.action == "campaign.wave_withheld",
                CCAuditLog.subject == campaign_id,
            )
        )).scalar_one()


@pytest.mark.asyncio
async def test_partial_coverage_refuses_and_full_coverage_records_the_set():
    stack = await _stack()
    estate = await _estate(stack)
    a, b, c = estate["devices"]
    await _grant(stack, "kc-a", a)
    await _grant(stack, "kc-ab", a, b)
    await _grant(stack, "kc-abc", a, b, c)

    r = await _approve(stack.as_person("kc-a"), estate["subject"])
    assert r.status_code == 403 and "2 of the 3 devices" in r.json()["detail"], r.text
    r = await _approve(stack.as_person("kc-ab"), estate["subject"])
    assert r.status_code == 403 and "1 of the 3 devices" in r.json()["detail"], r.text
    assert await _records(stack, estate["subject"]) == [], "refused, not recorded"

    r = await _approve(stack.as_person("kc-abc"), estate["subject"])
    assert r.status_code == 200 and r.json()["decision"] == "approved", r.text
    (record,) = await _records(stack, estate["subject"])
    # JSONB round-trip: the evidence names the SET, and no representative.
    assert record.authority_snapshot["target_device_agent_ids"] == [a, b, c]
    assert record.authority_snapshot["target_device_agent_id"] == ""
    assert record.approver_ref == "kc-abc"


@pytest.mark.asyncio
async def test_revoking_one_approvers_grant_after_approval_withholds_dispatch():
    """§21 live sequence, on the production engine: approved while
    authorized -> revoke B -> the same approved wave does not dispatch ->
    restore ONLY B -> it does."""
    stack = await _stack()
    estate = await _estate(stack)
    a, b, c = estate["devices"]
    await _grant(stack, "kc-abc", a, b, c)
    r = await _approve(stack.as_person("kc-abc"), estate["subject"])
    assert r.status_code == 200 and r.json()["decision"] == "approved", r.text
    assert (await _wave(stack, estate["campaign_id"])).status == WAVE_APPROVED

    await _revoke(stack, "kc-abc", b)
    result = await _advance(stack, estate["campaign_id"])
    assert not result["advanced"], result
    assert "no longer hold 'action.approve' over every device" in result["blocked"][0]["reason"]
    assert FakeSM.dispatches == [], "nothing crossed CC -> SM"
    wave = await _wave(stack, estate["campaign_id"])
    assert wave.status == WAVE_APPROVED, "the historical decision stands"
    assert len(await _records(stack, estate["subject"])) == 1
    async with stack.sessionmaker() as session:
        repo = CampaignRepo(session)
        assert await repo.dispatches(estate["campaign_id"]) == []
        for target in await repo.targets(estate["campaign_id"]):
            assert target.revalidation == REVAL_AUTHORITY_LOST
    assert await _withheld(stack, estate["campaign_id"]) == 1

    await _advance(stack, estate["campaign_id"])
    assert await _withheld(stack, estate["campaign_id"]) == 1, "one entry per cause"
    assert FakeSM.dispatches == []

    await _grant(stack, "kc-abc", b)
    result = await _advance(stack, estate["campaign_id"])
    assert result["advanced"], result
    assert (await _wave(stack, estate["campaign_id"])).status == WAVE_DISPATCHED
    assert sorted(d["device_agent_id"] for d in FakeSM.dispatches) == [a, b, c]


@pytest.mark.asyncio
async def test_the_timestamptz_expiry_boundary_holds_for_one_target():
    stack = await _stack()
    estate = await _estate(stack)
    a, b, c = estate["devices"]
    await _grant(stack, "kc-lapsed", a, c)
    await _grant(stack, "kc-lapsed", b,
                 expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    r = await _approve(stack.as_person("kc-lapsed"), estate["subject"])
    assert r.status_code == 403 and "1 of the 3 devices" in r.json()["detail"], r.text

    async with stack.sessionmaker() as session:
        await session.execute(
            sa.update(CCScopeGrant)
            .where(CCScopeGrant.tenant_id == stack.tenant,
                   CCScopeGrant.principal_ref == "kc-lapsed",
                   CCScopeGrant.scope_ref == b)
            .values(expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
        )
        await session.commit()
    r = await _approve(stack.as_person("kc-lapsed"), estate["subject"])
    assert r.status_code == 200 and r.json()["decision"] == "approved", r.text


@pytest.mark.asyncio
async def test_a_realm_role_demotion_after_approval_withholds_until_restored(_identity_plane):
    """A30.23 on the production engine. The approver holds `operator` in
    the realm and grants over every device; they approve the immutable
    wave; the realm demotes them -- grants and JSONB ledger untouched --
    and the same approved wave is withheld with `current_role` named on
    the audit entry. Restoring the role lets the SAME approval count."""
    realm = _identity_plane
    stack = await _stack()
    estate = await _estate(stack)
    a, b, c = estate["devices"]
    await _grant(stack, "kc-abc", a, b, c)
    r = await _approve(stack.as_person("kc-abc"), estate["subject"])
    assert r.status_code == 200 and r.json()["decision"] == "approved", r.text
    (record,) = await _records(stack, estate["subject"])
    assert record.authority_snapshot["role"] == "operator"

    realm.demote("kc-abc", "operator")
    realm.promote("kc-abc", "viewer")
    result = await _advance(stack, estate["campaign_id"])
    assert not result["advanced"], result
    assert "causes: current_role" in result["blocked"][0]["reason"]
    assert FakeSM.dispatches == [], "nothing crossed CC -> SM"
    assert (await _wave(stack, estate["campaign_id"])).status == WAVE_APPROVED
    (record,) = await _records(stack, estate["subject"])
    assert record.authority_snapshot["role"] == "operator", "the JSONB ledger is untouched"
    async with stack.sessionmaker() as session:
        live = [g for g in await ScopeGrantRepo(session).list_for_principal(stack.tenant, "kc-abc")
                if g.revoked_at is None]
        assert len(live) == 3, "the grants are intact: this is not a scope refusal"
        row = (await session.execute(
            sa.select(CCAuditLog).where(
                CCAuditLog.action == "campaign.wave_withheld",
                CCAuditLog.subject == estate["campaign_id"],
            )
        )).scalars().one()
        (lost,) = row.detail["lost"]
        assert lost == {
            "approver_ref": "kc-abc", "cause": "current_role", "current_role": "viewer",
            "reason": "", "uncovered": 3, "of": 3,
        }
        for target in await CampaignRepo(session).targets(estate["campaign_id"]):
            assert target.revalidation == REVAL_AUTHORITY_LOST

    realm.promote("kc-abc", "operator")
    result = await _advance(stack, estate["campaign_id"])
    assert result["advanced"], result
    assert (await _wave(stack, estate["campaign_id"])).status == WAVE_DISPATCHED
    assert sorted(d["device_agent_id"] for d in FakeSM.dispatches) == [a, b, c]
    assert len(await _records(stack, estate["subject"])) == 1, "nobody approved twice"
