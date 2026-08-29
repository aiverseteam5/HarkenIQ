"""S5: the wired /api/autonomy endpoint — RBAC, tenant isolation, scope.

The composer is tested purely in test_s5_autonomy_contract.py; this
covers what only the wired endpoint can prove: who may read the posture
(D2's read-split, which S5 must not narrow), that one tenant's safety
state never appears in another's contract, and that S5 added no mutation
surface of its own.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import (
    CCAutonomyBudget,
    CCOutcomeHistory,
    CCSafetyState,
    CCSite,
)
from harkeniq_cc.runtime import AppState

TENANT = "t1"
OTHER = "t2"


async def _stack(role: str = "tenant_owner", tenant: str = TENANT):
    config = CCConfig(tenant_id=tenant, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)

    async def _fake():
        return UserContext(
            user_id=f"kc-{role}", email=f"{role}@example.com", tenant_id=tenant,
            role=role,
            permissions=list(ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS["viewer"])),
            is_platform_user=role == "platform_super_admin",
        )

    app.dependency_overrides[get_current_user] = _fake
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    )
    return client, sessionmaker


async def _seed(sessionmaker, tenant: str, *, level: int = 2,
                site_name: str = "DC-1", dropped_back: bool = False,
                outcomes: int = 0):
    async with sessionmaker() as session:
        site = CCSite(
            tenant_id=tenant, site_name=site_name,
            sm_endpoint="sm:50051", sm_token="tok",
        )
        session.add(site)
        await session.flush()
        # One budget row per (tenant, device_type) — seeding a second site
        # for the same tenant must not try to insert a duplicate.
        from sqlalchemy import select

        existing = (await session.execute(
            select(CCAutonomyBudget).where(CCAutonomyBudget.tenant_id == tenant)
        )).scalars().first()
        if existing is None:
            session.add(CCAutonomyBudget(
                tenant_id=tenant, device_type="*", level=level,
                budget_limit=10, budget_period="daily",
            ))
        session.add(CCSafetyState(
            site_id=site.id, tenant_id=tenant, reported=True,
            as_of=datetime.now(timezone.utc), sm_stop_switch=False,
            suppressions=[],
            error_budgets=[{
                "action_type": "SEL_CLEAR", "total_count": 8,
                "success_count": 2, "failure_count": 6,
                "dropped_back": dropped_back,
            }] if dropped_back else [],
            site_budgets={},
        ))
        for i in range(outcomes):
            session.add(CCOutcomeHistory(
                site_id=site.id, action_id=f"a{i}", action_type="SEL_CLEAR",
                device_agent_id="dev-1", vendor="Dell", model="R750",
                outcome="SUCCESS" if i % 4 else "FAILURE", fault_resolved=True,
                recorded_at=datetime.now(timezone.utc),
            ))
        await session.commit()
        return site.id


def _by_type(payload) -> dict:
    return {c["action_type"]: c for c in payload["action_classes"]}


class TestRbac:
    """D2: posture READS are open to every tenant role. S5 must not narrow it."""

    @pytest.mark.parametrize("role,expected", [
        ("tenant_owner", 200),
        ("tenant_admin", 200),
        ("operator", 200),
        ("viewer", 200),
    ])
    @pytest.mark.asyncio
    async def test_every_tenant_role_may_read_posture(self, role, expected):
        client, sessionmaker = await _stack(role)
        await _seed(sessionmaker, TENANT)
        resp = await client.get("/api/autonomy/")
        assert resp.status_code == expected
        await client.aclose()

    @pytest.mark.asyncio
    async def test_a_viewer_sees_that_it_may_not_change_posture(self):
        client, sessionmaker = await _stack("viewer")
        await _seed(sessionmaker, TENANT)
        body = (await client.get("/api/autonomy/")).json()
        assert body["actor"]["may_observe"] is True
        assert body["actor"]["may_change_posture"] is False
        await client.aclose()

    @pytest.mark.asyncio
    async def test_s5_added_no_mutation_surface(self):
        """Every autonomy mutation stays on /api/policies/* at site.manage."""
        client, sessionmaker = await _stack("tenant_owner")
        await _seed(sessionmaker, TENANT)
        for verb in ("post", "put", "patch", "delete"):
            resp = await getattr(client, verb)("/api/autonomy/")
            assert resp.status_code == 405, f"{verb} should not exist"
        await client.aclose()


class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_another_tenants_safety_state_never_appears(self):
        client, sessionmaker = await _stack("tenant_owner", TENANT)
        await _seed(sessionmaker, TENANT, site_name="mine")
        await _seed(sessionmaker, OTHER, site_name="theirs", dropped_back=True)
        body = (await client.get("/api/autonomy/")).json()
        site_names = {s["name"] for s in body["scope"]["sites"]}
        assert site_names == {"mine"}
        assert body["safety_state"]["suppressions"] == []
        assert _by_type(body)["SEL_CLEAR"]["safety"]["error_budget"] is None
        await client.aclose()


class TestContractOverTheWire:
    @pytest.mark.asyncio
    async def test_posture_and_ladder_are_served(self):
        client, sessionmaker = await _stack("operator")
        await _seed(sessionmaker, TENANT, level=2)
        body = (await client.get("/api/autonomy/")).json()
        assert body["posture"]["configured_level"] == 2
        assert body["posture"]["level_source"] == "budget_row"
        assert [lvl["level"] for lvl in body["posture"]["ladder"]] == [0, 1, 2, 3]
        assert _by_type(body)["SEL_CLEAR"]["disposition"] == "autonomous"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_firmware_is_never_autonomous_over_the_wire(self):
        client, sessionmaker = await _stack("tenant_owner")
        await _seed(sessionmaker, TENANT, level=3)
        body = (await client.get("/api/autonomy/")).json()
        by = _by_type(body)
        for action_type in (
            "FIRMWARE_UPDATE", "FIRMWARE_ROLLBACK",
            "INTERFACE_RESET", "INTERFACE_DISABLE",
        ):
            assert by[action_type]["disposition"] == "denied"
            assert by[action_type]["never_budget_grantable"] is True
        await client.aclose()

    @pytest.mark.asyncio
    async def test_drop_back_reaches_the_tenant_surface(self):
        """The whole point of S5's transport: a demotion is now visible."""
        client, sessionmaker = await _stack("viewer")
        await _seed(sessionmaker, TENANT, level=2, dropped_back=True)
        body = (await client.get("/api/autonomy/")).json()
        sel = _by_type(body)["SEL_CLEAR"]
        assert sel["disposition"] == "requires_approval"
        assert sel["safety"]["error_budget"]["dropped_back"] is True
        assert any(
            b["code"] == "error_budget_dropped_back"
            for b in sel["blocking_conditions"]
        )
        await client.aclose()

    @pytest.mark.asyncio
    async def test_real_outcomes_become_evidence(self):
        client, sessionmaker = await _stack("operator")
        await _seed(sessionmaker, TENANT, level=2, outcomes=8)
        body = (await client.get("/api/autonomy/")).json()
        ev = _by_type(body)["SEL_CLEAR"]["evidence"]
        assert ev["executions"] == 8
        assert ev["success_rate"] is not None
        await client.aclose()

    @pytest.mark.asyncio
    async def test_site_filter_narrows(self):
        client, sessionmaker = await _stack("tenant_owner")
        site_a = await _seed(sessionmaker, TENANT, site_name="A")
        await _seed(sessionmaker, TENANT, site_name="B")
        body = (await client.get(f"/api/autonomy/?site_id={site_a}")).json()
        assert [s["id"] for s in body["scope"]["sites"]] == [site_a]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_action_type_filter_narrows(self):
        client, sessionmaker = await _stack("tenant_owner")
        await _seed(sessionmaker, TENANT)
        body = (await client.get("/api/autonomy/?action_type=sel_clear")).json()
        assert [c["action_type"] for c in body["action_classes"]] == ["SEL_CLEAR"]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_a_tenant_with_no_sites_reads_unknown_not_safe(self):
        client, _ = await _stack("tenant_owner")
        body = (await client.get("/api/autonomy/")).json()
        assert body["safety_state"]["reported"] is False
        assert body["posture"]["level_source"] == "unconfigured"
        await client.aclose()
