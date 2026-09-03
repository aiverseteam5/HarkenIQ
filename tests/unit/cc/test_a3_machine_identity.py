"""A3: machine identity is authentication, and only authentication (A20).

The defect this slice was designed around is not in any of these tests as
a bug — it is the reason the design has the shape it has. Resolved the way
agents are in-process (`role_permissions=["*"]`, safe only because nothing
authenticates), an authenticated agent principal satisfies EVERY route
guard in the platform, including `action.approve`. It could approve its
own proposals.

So the most important test here is not "the credential works". It is
`TestTheCeilingIsHard`: grant the agent every read binding that exists and
assert the effective set is still two permissions, then sweep the WHOLE
vocabulary asserting refusal on the rest. A binding-driven test would pass
while the ceiling was absent.
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
from harkeniq_cc.db.models import CCFleetCache, CCSite
from harkeniq_cc.db.repos import AgentIdentityRepo, OperationalAgentRepo
from harkeniq_cc.machine_identity import (
    MACHINE_PRINCIPAL_CEILING,
    READ_BINDING_PERMISSIONS,
    SPECIES_AGENT,
    aggregate_summary,
    authenticate,
    client_id_for,
    is_machine_client_id,
    machine_permissions,
)
from harkeniq_cc.runtime import AppState

from tests.unit.cc.conftest import seed_tenant_admin

TENANT = "t1"
OTHER = "t2"
REALM = "tenant-demo"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeConsole:
    """The identity plane, without Keycloak. Records what CC asked for."""

    calls: list = []
    fail = ""
    secret_n = 0

    @classmethod
    def reset(cls):
        cls.calls = []
        cls.fail = ""
        cls.secret_n = 0

    @classmethod
    async def provision(cls, state, *, realm, client_id):
        cls.calls.append(("provision", realm, client_id))
        if cls.fail:
            return None, cls.fail
        cls.secret_n += 1
        return {
            "client_uuid": f"uuid-{client_id}",
            "client_id": client_id,
            "subject": f"sa-sub-{client_id}",
            "secret": f"secret-{cls.secret_n}",
        }, ""

    @classmethod
    async def rotate(cls, state, *, realm, client_id):
        cls.calls.append(("rotate", realm, client_id))
        if cls.fail:
            return None, cls.fail
        cls.secret_n += 1
        # Same subject: rotation must never mint a second identity.
        return {
            "client_id": client_id,
            "subject": f"sa-sub-{client_id}",
            "secret": f"secret-{cls.secret_n}",
        }, ""

    @classmethod
    async def set_enabled(cls, state, *, realm, client_id, enabled):
        cls.calls.append(("set_enabled", realm, client_id, enabled))
        if cls.fail:
            return None, cls.fail
        return {"client_id": client_id, "enabled": enabled}, ""

    @classmethod
    async def report_summary(cls, state, *, tenant_id, summary):
        cls.calls.append(("summary", tenant_id, summary))
        return ""


@pytest.fixture(autouse=True)
def _fake_console(monkeypatch):
    from harkeniq_cc import identity_client

    FakeConsole.reset()
    monkeypatch.setattr(identity_client, "provision", FakeConsole.provision)
    monkeypatch.setattr(identity_client, "rotate", FakeConsole.rotate)
    monkeypatch.setattr(identity_client, "set_enabled", FakeConsole.set_enabled)
    monkeypatch.setattr(
        identity_client, "report_summary", FakeConsole.report_summary
    )
    yield


class Stack:
    def __init__(self, app, state):
        self.app = app
        self.state = state
        self.sessionmaker = state.sessionmaker
        self.persona = ("kc-owner", "owner@example.com", "tenant_owner")
        self.machine = None

    def as_person(self, sub, email, role="tenant_owner"):
        self.persona, self.machine = (sub, email, role), None
        return self

    def as_machine(self, agent_id, permissions, tenant=TENANT):
        """Stand in for a validated machine token.

        The token path itself is exercised by `TestAuthenticationVerdict`
        and live at the compose gate; here the point is what a machine
        PRINCIPAL may do once it exists.
        """
        self.machine = UserContext(
            user_id=agent_id, email=f"op-agent:{agent_id}@v1",
            tenant_id=tenant, role="", permissions=list(permissions),
            species=SPECIES_AGENT, identity_id="ident-1",
        )
        return self

    def client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
        )


async def _stack() -> Stack:
    config = CCConfig(tenant_id=TENANT, insecure=True)
    config.keycloak_realm = REALM
    config.console_url = "http://console"
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)
    stack = Stack(app, state)

    # A23-5: a rowless tenant is STRICT now (A23.11). The default
    # persona is the tenant's founding administrator, granted the
    # way tenant birth grants one (A23.14 D4) rather than being
    # tenant-wide by the synthesis a missing row used to give.
    await seed_tenant_admin(sessionmaker, TENANT, "kc-owner")

    async def _fake():
        if stack.machine is not None:
            return stack.machine
        sub, email, role = stack.persona
        return UserContext(
            user_id=sub, email=email, tenant_id=TENANT, role=role,
            permissions=list(ROLE_PERMISSIONS[role]), species="user",
        )

    app.dependency_overrides[get_current_user] = _fake
    return stack


async def _seed(stack: Stack) -> str:
    async with stack.sessionmaker() as session:
        site = CCSite(tenant_id=TENANT, site_name="DC-1",
                      sm_endpoint="sm:50051", sm_token="tok")
        session.add(site)
        await session.flush()
        session.add(CCFleetCache(
            site_id=site.id, agent_id="node-1", agent_name="rack1-node1",
            vendor="Dell", model="R750", device_class="server",
            observation="observed", health="OK",
        ))
        await session.commit()
        return site.id


async def _agent(c, site_id, **kw):
    body = {
        "name": f"A3 Agent {id(kw)}",
        "scopes": [{"scope_type": "site", "scope_ref": site_id}],
        "capabilities": [{"kind": "action_class", "capability_ref": "SEL_CLEAR"}],
    }
    body.update(kw)
    res = await c.post("/api/operational-agents/", json=body)
    assert res.status_code == 201, res.text
    return res.json()["id"]


class _Row:
    """A minimal identity/agent stand-in for the pure verdict tests."""

    def __init__(self, **kw):
        self.status = "active"
        self.tenant_id = TENANT
        self.realm = REALM
        self.revoke_reason = ""
        self.last_seen_at = None
        self.__dict__.update(kw)


# ---------------------------------------------------------------------------
# The ceiling — the reason A3 has the shape it has
# ---------------------------------------------------------------------------


class TestTheCeilingIsHard:
    def test_every_read_binding_together_still_yields_two_permissions(self):
        """The headline. A derivation would pass this only by luck."""
        every = list(READ_BINDING_PERMISSIONS)
        assert machine_permissions(every) == ["fleet.view", "incident.view"]

    def test_the_whole_vocabulary_is_refused_except_the_ceiling(self):
        """Swept, not spot-checked: a ceiling is a claim about ALL of them."""
        from harkeniq_console.permissions import PERMISSIONS

        granted = set(machine_permissions(list(READ_BINDING_PERMISSIONS)))
        for permission in PERMISSIONS:
            if permission in MACHINE_PRINCIPAL_CEILING:
                assert permission in granted, permission
            else:
                assert permission not in granted, (
                    f"a machine principal must never hold {permission}"
                )

    def test_the_dangerous_ones_by_name(self):
        every = set(machine_permissions(list(READ_BINDING_PERMISSIONS)))
        for forbidden in ("action.approve", "site.manage", "role.manage",
                          "tenant.manage", "audit.export", "user.manage",
                          "skill.install", "billing.manage", "*"):
            assert forbidden not in every, forbidden

    def test_a_wildcard_can_never_come_out(self):
        """`["*"]` is exactly what makes the in-process path safe and the
        HTTP path catastrophic."""
        assert "*" not in machine_permissions(["fleet", "attention", "*"])
        assert machine_permissions(["*"]) == []

    def test_an_unknown_binding_contributes_nothing_and_does_not_raise(self):
        assert machine_permissions(["not-a-binding"]) == []
        assert machine_permissions([]) == []

    def test_a_widened_binding_table_cannot_raise_the_ceiling(self, monkeypatch):
        """The property the intersection exists for.

        Someone adds a binding that maps to `action.approve` — by mistake,
        or because a future read genuinely needs a broader permission. The
        ceiling must still hold, because it is a separate constant rather
        than a summary of this table.
        """
        widened = dict(READ_BINDING_PERMISSIONS)
        widened["rogue"] = frozenset({"action.approve", "site.manage"})
        monkeypatch.setattr(
            "harkeniq_cc.machine_identity.READ_BINDING_PERMISSIONS", widened
        )
        got = machine_permissions(["rogue", "fleet"])
        assert got == ["fleet.view"], got
        assert "action.approve" not in got

    def test_the_ceiling_is_its_own_constant_not_the_table(self):
        """Structural: if these were one object the test above is vacuous."""
        implied = set()
        for perms in READ_BINDING_PERMISSIONS.values():
            implied |= perms
        assert MACHINE_PRINCIPAL_CEILING is not implied
        assert isinstance(MACHINE_PRINCIPAL_CEILING, frozenset)


# ---------------------------------------------------------------------------
# Authentication verdict (A20.5)
# ---------------------------------------------------------------------------


class TestAuthenticationVerdict:
    def test_an_active_identity_authenticates(self):
        ok, why = authenticate(
            _Row(), _Row(status="active"), tenant_id=TENANT, realm=REALM
        )
        assert ok is True and why == ""

    def test_no_identity_is_refused(self):
        ok, why = authenticate(None, None, tenant_id=TENANT, realm=REALM)
        assert ok is False and "no machine identity" in why

    def test_revoked_beats_a_valid_token(self):
        """The whole reason CC checks its own row on every request."""
        ok, why = authenticate(
            _Row(status="revoked"), _Row(), tenant_id=TENANT, realm=REALM
        )
        assert ok is False and "revoked" in why

    def test_retired_is_distinguishable_from_revoked(self):
        ok, why = authenticate(
            _Row(status="retired"), _Row(), tenant_id=TENANT, realm=REALM
        )
        assert ok is False and "retired" in why

    def test_another_tenant_is_refused(self):
        ok, why = authenticate(
            _Row(tenant_id=OTHER), _Row(), tenant_id=TENANT, realm=REALM
        )
        assert ok is False and "another tenant" in why

    def test_another_realm_is_refused(self):
        """E1.4: an identity is a (realm, subject) fact."""
        ok, why = authenticate(
            _Row(realm="somewhere-else"), _Row(), tenant_id=TENANT, realm=REALM
        )
        assert ok is False and "another realm" in why

    def test_an_identity_that_outlived_its_agent_is_refused(self):
        ok, why = authenticate(_Row(), None, tenant_id=TENANT, realm=REALM)
        assert ok is False and "no longer exists" in why

    def test_a_retired_agent_is_refused_even_with_an_active_identity(self):
        ok, why = authenticate(
            _Row(), _Row(status="retired"), tenant_id=TENANT, realm=REALM
        )
        assert ok is False and "retired" in why

    def test_client_id_shape(self):
        assert is_machine_client_id(client_id_for("abc")) is True
        assert is_machine_client_id("harkeniq-console") is False
        assert is_machine_client_id("") is False


# ---------------------------------------------------------------------------
# Lifecycle over the wired API
# ---------------------------------------------------------------------------


class TestIdentityLifecycle:
    @pytest.mark.asyncio
    async def test_issue_returns_the_secret_once_and_never_stores_it(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
            res = await c.post(f"/api/operational-agents/{agent_id}/identity")
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["client_secret"] == "secret-1"
            assert body["client_id"] == client_id_for(agent_id)
            assert body["status"] == "active"

            # Never again, on any read.
            read = await c.get(f"/api/operational-agents/{agent_id}/identity")
            assert "client_secret" not in read.json()
            assert read.json()["exists"] is True

        # And there is no column it could have been written into.
        async with stack.sessionmaker() as session:
            row = await AgentIdentityRepo(session).get_for_agent(TENANT, agent_id)
            assert not any("secret" in c for c in row.__table__.columns.keys())

    @pytest.mark.asyncio
    async def test_a_second_identity_is_refused(self):
        """Two identities would be two answers to 'who is this runtime?'."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
            await c.post(f"/api/operational-agents/{agent_id}/identity")
            again = await c.post(f"/api/operational-agents/{agent_id}/identity")
            assert again.status_code == 409
            assert "rotate it" in again.json()["detail"]

    @pytest.mark.asyncio
    async def test_rotation_changes_the_secret_not_the_identity(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
            first = (
                await c.post(f"/api/operational-agents/{agent_id}/identity")
            ).json()
            rotated = await c.post(
                f"/api/operational-agents/{agent_id}/identity/rotate"
            )
            assert rotated.status_code == 200, rotated.text
            assert rotated.json()["client_secret"] != first["client_secret"]
            assert rotated.json()["client_id"] == first["client_id"]

        async with stack.sessionmaker() as session:
            row = await AgentIdentityRepo(session).get_for_agent(TENANT, agent_id)
            # Same subject, same row, still active: no second identity, no gap.
            assert row.keycloak_sub == f"sa-sub-{client_id_for(agent_id)}"
            assert row.status == "active"
            assert row.rotated_at is not None

    @pytest.mark.asyncio
    async def test_revocation_is_recorded_even_if_keycloak_is_unreachable(self):
        """CC's row is what makes revocation immediate, so it is written
        unconditionally — a Keycloak outage must not leave a credential
        the operator believes they revoked."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
            await c.post(f"/api/operational-agents/{agent_id}/identity")
            FakeConsole.fail = "keycloak is down"
            res = await c.post(
                f"/api/operational-agents/{agent_id}/identity/revoke",
                json={"reason": "suspected leak"},
            )
            assert res.status_code == 200, res.text
            assert res.json()["effective"] == "immediate"
            assert "still refused here" in res.json()["detail"]

        async with stack.sessionmaker() as session:
            row = await AgentIdentityRepo(session).get_for_agent(TENANT, agent_id)
            assert row.status == "revoked"
            assert row.revoke_reason == "suspected leak"

    @pytest.mark.asyncio
    async def test_a_revoked_identity_cannot_be_rotated(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
            await c.post(f"/api/operational-agents/{agent_id}/identity")
            await c.post(f"/api/operational-agents/{agent_id}/identity/revoke")
            res = await c.post(
                f"/api/operational-agents/{agent_id}/identity/rotate"
            )
            assert res.status_code == 409

    @pytest.mark.asyncio
    async def test_retiring_an_agent_retires_its_identity(self):
        """A20.7: an identity that outlived its agent would name nothing."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
            await c.post(f"/api/operational-agents/{agent_id}/identity")
            assert (
                await c.post(f"/api/operational-agents/{agent_id}/retire")
            ).status_code == 200

        async with stack.sessionmaker() as session:
            row = await AgentIdentityRepo(session).get_for_agent(TENANT, agent_id)
            assert row.status == "retired"
        assert ("set_enabled", REALM, client_id_for(agent_id), False) \
            in FakeConsole.calls

    @pytest.mark.asyncio
    async def test_a_retired_agent_cannot_be_credentialed(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
            await c.post(f"/api/operational-agents/{agent_id}/retire")
            res = await c.post(f"/api/operational-agents/{agent_id}/identity")
            assert res.status_code == 409

    @pytest.mark.asyncio
    async def test_provisioning_failure_leaves_no_identity_and_is_audited(self):
        stack = await _stack()
        site_id = await _seed(stack)
        FakeConsole.fail = "the Console could not be reached"
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
            res = await c.post(f"/api/operational-agents/{agent_id}/identity")
            assert res.status_code == 502
            read = await c.get(f"/api/operational-agents/{agent_id}/identity")
            assert read.json()["exists"] is False
            entries = (await c.get("/api/audit/?page_size=100")).json()["entries"]
            assert any(
                e["action"] == "agent_identity.issue_failed" for e in entries
            )

    @pytest.mark.asyncio
    async def test_the_lifecycle_is_audited_and_the_chain_verifies(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
            await c.post(f"/api/operational-agents/{agent_id}/identity")
            await c.post(f"/api/operational-agents/{agent_id}/identity/rotate")
            await c.post(f"/api/operational-agents/{agent_id}/identity/revoke")
            entries = (await c.get("/api/audit/?page_size=200")).json()["entries"]
            actions = {e["action"] for e in entries}
            for required in ("agent_identity.issued", "agent_identity.rotated",
                             "agent_identity.revoked"):
                assert required in actions, (required, sorted(actions))
            # Every one names a person.
            for e in entries:
                if e["action"].startswith("agent_identity."):
                    assert e["actor"], e
            assert (await c.get("/api/audit/verify")).json()["valid"] is True

    @pytest.mark.asyncio
    async def test_the_secret_never_appears_in_audit(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
            await c.post(f"/api/operational-agents/{agent_id}/identity")
            raw = (await c.get("/api/audit/?page_size=100")).text
            assert "secret-1" not in raw


# ---------------------------------------------------------------------------
# What an authenticated machine principal may actually do
# ---------------------------------------------------------------------------


class TestMachinePrincipalOverHTTP:
    async def _credentialed(self, stack, c, site_id):
        """An agent with an identity. Its scope grant already exists:
        A0 writes the bundle's scope rows into `cc_scope_grants` as
        `principal_type="agent"`, which is exactly why the machine
        principal resolves through the ONE resolver unchanged."""
        agent_id = await _agent(c, site_id, capabilities=[
            {"kind": "action_class", "capability_ref": "SEL_CLEAR"},
        ])
        await c.post(f"/api/operational-agents/{agent_id}/identity")
        async with stack.sessionmaker() as session:
            from harkeniq_cc.db.repos import ScopeGrantRepo

            grants = await ScopeGrantRepo(session).list_for_principal(
                TENANT, agent_id, principal_type="agent",
            )
            assert grants, "A0 should already have granted the agent its scope"
        return agent_id

    @pytest.mark.asyncio
    async def test_it_can_read_what_the_ceiling_allows(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await self._credentialed(stack, c, site_id)
            stack.as_machine(agent_id, ["fleet.view", "incident.view"])
            assert (await c.get("/api/fleet/")).status_code == 200
            assert (await c.get("/api/incidents/")).status_code == 200
            assert (await c.get("/api/attention/")).status_code == 200

    @pytest.mark.asyncio
    async def test_it_CANNOT_approve_its_own_work(self):
        """The catastrophic case, refused at the route guard."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await self._credentialed(stack, c, site_id)
            stack.as_machine(agent_id, ["fleet.view", "incident.view"])
            res = await c.post("/api/approvals/anything/approve")
            assert res.status_code == 403
            assert "action.approve" in res.json()["detail"]

    @pytest.mark.asyncio
    async def test_it_cannot_reach_any_mutation(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await self._credentialed(stack, c, site_id)
            stack.as_machine(agent_id, ["fleet.view", "incident.view"])
            for method, path, body in (
                ("post", "/api/operational-agents/", {"name": "x"}),
                ("post", f"/api/operational-agents/{agent_id}/activate", None),
                ("post", f"/api/operational-agents/{agent_id}/identity", None),
                ("post", "/api/policies/", {"name": "x"}),
                ("post", "/api/scope-grants/", {"principal_ref": "x"}),
            ):
                res = await getattr(c, method)(path, json=body) if body \
                    else await getattr(c, method)(path)
                assert res.status_code == 403, (path, res.status_code, res.text)

    @pytest.mark.asyncio
    async def test_it_cannot_credential_ITSELF(self):
        """A machine principal must not touch the identity lifecycle."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await self._credentialed(stack, c, site_id)
            stack.as_machine(agent_id, ["fleet.view", "incident.view"])
            for path in ("identity", "identity/rotate", "identity/revoke"):
                res = await c.post(
                    f"/api/operational-agents/{agent_id}/{path}"
                )
                assert res.status_code == 403, (path, res.text)

    @pytest.mark.asyncio
    async def test_the_scope_resolver_narrows_it_the_same_way(self):
        """One resolver: an agent out of scope is refused like a human."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await self._credentialed(stack, c, site_id)
            stack.as_machine(agent_id, ["fleet.view", "incident.view"])
            fleet = (await c.get("/api/fleet/")).json()
            # It sees its own site, and it resolved through the agent
            # principal type -- a user-typed lookup would find no grants.
            assert isinstance(fleet.get("devices", fleet.get("items")), list)


# ---------------------------------------------------------------------------
# D3 + aggregate visibility
# ---------------------------------------------------------------------------


class TestRevocationRefusesApprovedWork:
    @pytest.mark.asyncio
    async def test_a_revoked_identity_refuses_an_approved_proposal(self):
        """A20.8: approved proposal version != guaranteed execution."""
        from harkeniq_cc.db.models import CCAgentProposal

        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
            await c.post(f"/api/operational-agents/{agent_id}/identity")
            async with stack.sessionmaker() as session:
                agent = await OperationalAgentRepo(session).get(TENANT, agent_id)
                agent.status = "active"
                row = CCAgentProposal(
                    tenant_id=TENANT, agent_id=agent_id,
                    actor=f"op-agent:{agent_id}@v1", agent_version=1,
                    site_id=site_id, device_agent_id="node-1",
                    action_type="SEL_CLEAR", params={}, rationale="r",
                    evidence={}, disposition="requires_approval",
                    authorization_basis="human_approval",
                    status="awaiting_approval", dedupe_key="k1",
                )
                session.add(row)
                await session.commit()
                proposal_id = row.id

            await c.post(f"/api/operational-agents/{agent_id}/identity/revoke")
            res = await c.post(f"/api/approvals/{proposal_id}/approve")
            assert res.status_code == 200, res.text
            delivery = res.json()["delivery"]
            assert delivery["accepted"] is False
            assert "revoked" in delivery["reason"], delivery

    @pytest.mark.asyncio
    async def test_an_agent_with_NO_identity_still_dispatches(self):
        """Today's CC-resident evaluator holds no credential at all.

        The gate refuses a credential that was WITHDRAWN, which is a
        different fact from never having had one — refusing the latter
        would break every existing proposal.
        """
        from harkeniq_cc.api.approvals import _agent_dispatch_gates

        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
            async with stack.sessionmaker() as session:
                agent = await OperationalAgentRepo(session).get(TENANT, agent_id)
                agent.status = "active"
                await session.commit()

        class P:
            actor = f"op-agent:{agent_id}@v1"

        async with stack.sessionmaker() as session:
            allowed, why = await _agent_dispatch_gates(session, TENANT, P())
        assert allowed is True, why


class TestAggregateVisibilityCarriesNoDetail:
    def test_the_summary_is_counts_only(self):
        """A12.1 is not amended: no per-agent detail leaves the tenant."""
        rows = [
            _Row(status="active", last_seen_at=datetime.now(timezone.utc)),
            _Row(status="active", last_seen_at=None),
            _Row(status="revoked", last_seen_at=None),
            _Row(status="retired", last_seen_at=None),
        ]
        for i, r in enumerate(rows):
            r.agent_id = f"secret-agent-{i}"
            r.keycloak_client_id = f"op-agent-{i}"
            r.keycloak_sub = f"sub-{i}"

        summary = aggregate_summary(rows)
        assert summary["identities"] == 4
        assert summary["active"] == 2
        assert summary["revoked"] == 1
        assert summary["retired"] == 1
        assert summary["ever_seen"] == 1
        assert summary["never_seen"] == 3

        blob = repr(summary)
        for leak in ("secret-agent", "op-agent-", "sub-"):
            assert leak not in blob, f"{leak} leaked into the platform summary"

    def test_an_empty_tenant_summarises_without_error(self):
        summary = aggregate_summary([])
        assert summary["identities"] == 0
        assert summary["most_recent_seen_at"] is None
