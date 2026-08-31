"""A0: the Operational Agents API, wired.

The composer is tested purely elsewhere. This covers what only the wired
endpoint proves: who may create and who may only look (no new permission
was introduced), that a bundle can only reference capabilities and
scopes that already exist, that activation is a separate human act which
refuses an agent that could do nothing, and that one tenant's agents are
invisible to another.
"""

from __future__ import annotations

import httpx
import pytest

from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCFleetCache, CCSite
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


async def _seed_fleet(sessionmaker, tenant: str = TENANT) -> str:
    async with sessionmaker() as session:
        site = CCSite(
            tenant_id=tenant, site_name="DC-1",
            sm_endpoint="sm:50051", sm_token="tok",
        )
        session.add(site)
        await session.flush()
        session.add(CCFleetCache(
            site_id=site.id, agent_id="node-1", agent_name="rack1-node1",
            vendor="Dell", model="R750", device_class="server",
            observation="observed", health="OK",
        ))
        await session.commit()
        return site.id


def _body(site_id: str, **kw):
    body = {
        "name": "Night Shift",
        "description": "watches DC-1 overnight",
        "scopes": [{"scope_type": "site", "scope_ref": site_id}],
        "capabilities": [{"kind": "action_class", "capability_ref": "SEL_CLEAR"}],
    }
    body.update(kw)
    return body


async def _activate(client, agent_id):
    """Take an agent through the A2 governed activation lifecycle.

    PREFLIGHT -> ACKNOWLEDGE (where warned) -> APPROVE (where activation
    confers unattended execution) -> ACTIVATE. A propose-only agent
    needs no approval, which is why both steps are conditional.
    """
    pre = await client.post(f"/api/operational-agents/{agent_id}/preflight")
    assert pre.status_code == 200, pre.text
    result = pre.json()
    if result.get("requires_acknowledgement"):
        assert (
            await client.post(f"/api/operational-agents/{agent_id}/acknowledge")
        ).status_code == 200
    if result.get("requires_activation_approval"):
        detail = await client.get(f"/api/operational-agents/{agent_id}")
        subject = detail.json()["agent"]["activation_subject_ref"]
        assert (await client.post(f"/api/approvals/{subject}/approve")).status_code == 200
    return await client.post(f"/api/operational-agents/{agent_id}/activate")


class TestGovernance:
    @pytest.mark.asyncio
    async def test_viewer_can_read_but_not_create(self):
        client, sm = await _stack(role="viewer")
        site_id = await _seed_fleet(sm)
        assert (await client.get("/api/operational-agents/")).status_code == 200
        created = await client.post("/api/operational-agents/", json=_body(site_id))
        assert created.status_code == 403
        await client.aclose()

    @pytest.mark.asyncio
    async def test_operator_can_read_but_not_create(self):
        """action.approve is not site.manage: deciding is not configuring."""
        client, sm = await _stack(role="operator")
        site_id = await _seed_fleet(sm)
        assert (await client.get("/api/operational-agents/")).status_code == 200
        created = await client.post("/api/operational-agents/", json=_body(site_id))
        assert created.status_code == 403
        await client.aclose()

    @pytest.mark.asyncio
    async def test_site_admin_can_create(self):
        client, sm = await _stack(role="site_admin")
        site_id = await _seed_fleet(sm)
        created = await client.post("/api/operational-agents/", json=_body(site_id))
        assert created.status_code == 201
        await client.aclose()

    @pytest.mark.asyncio
    async def test_another_tenants_agent_is_a_404_not_an_empty_body(self):
        client, sm = await _stack()
        site_id = await _seed_fleet(sm)
        created = await client.post("/api/operational-agents/", json=_body(site_id))
        agent_id = created.json()["id"]
        await client.aclose()

        other, _ = await _stack(tenant=OTHER)
        assert (await other.get(f"/api/operational-agents/{agent_id}")).status_code == 404
        assert (await other.get("/api/operational-agents/")).json()["total"] == 0
        await other.aclose()


class TestBundleValidation:
    @pytest.mark.asyncio
    async def test_refuses_an_action_class_the_executor_cannot_run(self):
        client, sm = await _stack()
        site_id = await _seed_fleet(sm)
        res = await client.post("/api/operational-agents/", json=_body(
            site_id,
            capabilities=[{"kind": "action_class", "capability_ref": "MAKE_COFFEE"}],
        ))
        assert res.status_code == 400
        assert "not an action class" in res.json()["detail"]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_refuses_a_site_this_tenant_does_not_own(self):
        client, sm = await _stack()
        await _seed_fleet(sm)
        res = await client.post("/api/operational-agents/", json=_body(
            "some-other-site",
        ))
        assert res.status_code == 400
        assert "not registered to this tenant" in res.json()["detail"]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_refuses_an_unknown_read_capability(self):
        client, sm = await _stack()
        site_id = await _seed_fleet(sm)
        res = await client.post("/api/operational-agents/", json=_body(
            site_id,
            capabilities=[{"kind": "read", "capability_ref": "everything"}],
        ))
        assert res.status_code == 400
        await client.aclose()

    @pytest.mark.asyncio
    async def test_duplicate_name_is_a_conflict(self):
        client, sm = await _stack()
        site_id = await _seed_fleet(sm)
        assert (await client.post(
            "/api/operational-agents/", json=_body(site_id)
        )).status_code == 201
        assert (await client.post(
            "/api/operational-agents/", json=_body(site_id)
        )).status_code == 409
        await client.aclose()

    @pytest.mark.asyncio
    async def test_required_reads_are_bound_even_when_omitted(self):
        """An agent must observe and must read the contract it cites."""
        client, sm = await _stack()
        site_id = await _seed_fleet(sm)
        res = await client.post("/api/operational-agents/", json=_body(site_id))
        refs = {
            c["capability_ref"] for c in res.json()["capabilities"]
            if c["kind"] == "read"
        }
        assert {"attention", "autonomy"} <= refs
        await client.aclose()


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_an_agent_starts_in_draft_and_evaluates_nothing(self):
        client, sm = await _stack()
        site_id = await _seed_fleet(sm)
        res = await client.post("/api/operational-agents/", json=_body(site_id))
        assert res.json()["status"] == "draft"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_activation_refuses_an_agent_with_no_scope(self):
        client, sm = await _stack()
        await _seed_fleet(sm)
        created = await client.post("/api/operational-agents/", json=_body(
            "ignored", scopes=[],
        ))
        agent_id = created.json()["id"]
        # A2: activation is gated on a preflight, so the protection now
        # arrives as a BLOCKED dimension rather than an ad-hoc check --
        # same guarantee, one contract instead of two places.
        assert (
            await _activate(client, agent_id)
        ).status_code == 409
        pre = await client.post(f"/api/operational-agents/{agent_id}/preflight")
        assert pre.status_code == 200, pre.text
        result = pre.json()
        assert result["can_activate"] is False
        assert "scope" in result["blocked_dimensions"]
        scope_row = next(
            d for d in result["dimensions"] if d["dimension"] == "scope"
        )
        assert "see no devices" in scope_row["detail"]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_activation_refuses_an_agent_with_no_action_class(self):
        client, sm = await _stack()
        site_id = await _seed_fleet(sm)
        created = await client.post("/api/operational-agents/", json=_body(
            site_id, capabilities=[{"kind": "read", "capability_ref": "fleet"}],
        ))
        agent_id = created.json()["id"]
        assert (
            await client.post(f"/api/operational-agents/{agent_id}/activate")
        ).status_code == 409
        pre = await client.post(f"/api/operational-agents/{agent_id}/preflight")
        result = pre.json()
        assert result["can_activate"] is False
        assert "capabilities" in result["blocked_dimensions"]
        caps_row = next(
            d for d in result["dimensions"] if d["dimension"] == "capabilities"
        )
        assert "propose nothing" in caps_row["detail"]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_activate_pause_and_refuse_a_repeat(self):
        client, sm = await _stack()
        site_id = await _seed_fleet(sm)
        agent_id = (await client.post(
            "/api/operational-agents/", json=_body(site_id)
        )).json()["id"]
        activated = await _activate(client, agent_id)
        assert activated.status_code == 200
        assert activated.json()["status"] == "active"
        assert activated.json()["activated_by"] == "tenant_owner@example.com"
        assert (await client.post(
            f"/api/operational-agents/{agent_id}/activate"
        )).status_code == 409
        assert (await client.post(
            f"/api/operational-agents/{agent_id}/pause"
        )).json()["status"] == "paused"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_configuration_change_bumps_the_version(self):
        """Attribution embeds the version, so history cannot be rewritten."""
        client, sm = await _stack()
        site_id = await _seed_fleet(sm)
        created = (await client.post(
            "/api/operational-agents/", json=_body(site_id)
        )).json()
        assert created["version"] == 1
        assert created["actor"].endswith("@v1")
        updated = await client.patch(
            f"/api/operational-agents/{created['id']}",
            json={"autonomy_ceiling": 2},
        )
        assert updated.json()["version"] == 2
        assert updated.json()["actor"].endswith("@v2")
        await client.aclose()

    @pytest.mark.asyncio
    async def test_retired_agents_cannot_be_reconfigured(self):
        client, sm = await _stack()
        site_id = await _seed_fleet(sm)
        agent_id = (await client.post(
            "/api/operational-agents/", json=_body(site_id)
        )).json()["id"]
        assert (await client.post(
            f"/api/operational-agents/{agent_id}/retire"
        )).json()["status"] == "retired"
        res = await client.patch(
            f"/api/operational-agents/{agent_id}", json={"description": "x"},
        )
        assert res.status_code == 409
        await client.aclose()

    @pytest.mark.asyncio
    async def test_lifecycle_is_audited_on_the_existing_chain(self):
        client, sm = await _stack()
        site_id = await _seed_fleet(sm)
        agent_id = (await client.post(
            "/api/operational-agents/", json=_body(site_id)
        )).json()["id"]
        assert (await _activate(client, agent_id)).status_code == 200
        entries = (await client.get("/api/audit/")).json()["entries"]
        actions = {e["action"] for e in entries}
        assert "operational_agent.created" in actions
        assert "operational_agent.activated" in actions
        # A2: preflight and acknowledgement are first-class events, not
        # side effects. A configuration somebody activated must be
        # reconstructable from the chain, including what they accepted.
        assert "operational_agent.preflighted" in actions
        await client.aclose()


class TestDetailAndCatalogue:
    @pytest.mark.asyncio
    async def test_detail_answers_the_operator_questions(self):
        client, sm = await _stack()
        site_id = await _seed_fleet(sm)
        agent_id = (await client.post(
            "/api/operational-agents/", json=_body(site_id)
        )).json()["id"]
        view = (await client.get(f"/api/operational-agents/{agent_id}")).json()
        assert view["scope"]["device_count"] == 1
        assert view["scope"]["devices"][0]["agent_id"] == "node-1"
        classes = view["capabilities"]["action_classes"]
        assert [c["action_type"] for c in classes] == ["SEL_CLEAR"]
        assert classes[0]["disposition_reason"]
        assert "posture" in view
        await client.aclose()

    @pytest.mark.asyncio
    async def test_catalogue_says_which_classes_could_ever_fire(self):
        client, sm = await _stack()
        await _seed_fleet(sm)
        cat = (await client.get("/api/operational-agents/catalogue")).json()
        by_type = {c["action_type"]: c for c in cat["action_classes"]}
        assert by_type["SEL_CLEAR"]["proposable"] is True
        assert by_type["SEL_CLEAR"]["observed_conditions"] == ["log"]
        # A class with no observed condition mapped must say so rather
        # than looking like a live option.
        assert by_type["POWER_CAP_ADJUST"]["proposable"] is False
        assert by_type["POWER_CAP_ADJUST"]["note"]
        assert cat["scope_options"]["sites"][0]["name"] == "DC-1"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_no_execution_surface_exists_on_this_router(self):
        """An agent router that could act would be the parallel path."""
        client, sm = await _stack()
        site_id = await _seed_fleet(sm)
        agent_id = (await client.post(
            "/api/operational-agents/", json=_body(site_id)
        )).json()["id"]
        for path in ("execute", "run", "dispatch"):
            res = await client.post(f"/api/operational-agents/{agent_id}/{path}")
            assert res.status_code == 404, path
        await client.aclose()
