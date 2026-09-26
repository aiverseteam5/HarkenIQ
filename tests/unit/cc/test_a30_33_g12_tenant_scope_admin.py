"""A30.33 (G12): tenant-scope Operational Agent administration.

An Operational Agent holding a tenant-scope grant -- written by
`POST /api/scope-grants/` in the canonical tenant representation,
`scope_type = "tenant"` with an EMPTY `scope_ref` -- could not be
administered by anyone: every administration route re-read the agent's
stored rows through the REQUEST model `ScopeRule`, whose `scope_ref`
carried `min_length=1` for every type, so the rebuild raised before any
authorization decision and the route answered 500. Identity revoke and
retire were among them.

These tests prove the fix and, as much, what it did not move:

* the model -- empty is valid for tenant and for nothing else;
* the rebuild -- a tenant row round-trips; a corrupt non-tenant row still
  fails closed and writes nothing;
* who may administer a tenant-scoped agent -- tenant-wide authority holding
  `site.manage`, and nobody narrower, lapsed, inert or foreign;
* every one of the ten administration calls, with its DURABLE effect;
* the table for every other scope type, unchanged;
* the request path -- a tenant rule is still never written through the
  agent API;
* A23 -- an agent's row never counts toward the last administrator.

Persisted grants under STRICT throughout (the S3 estate), so a principal
reads through what it was granted and through nothing synthesized.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.api.operational_agents import (
    ScopeRule, _agent_scope_rules, _scope_rule_within,
)
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext
from harkeniq_cc.db.models import (
    CCAgentIdentity, CCAgentPreflight, CCAuditLog, CCOperationalAgent,
    CCScopeGrant,
)
from harkeniq_cc.db.repos import OperationalAgentRepo, ScopeGrantRepo
from harkeniq_cc.scope import PRINCIPAL_AGENT, PRINCIPAL_USER, count_tenant_admins

# The identity plane without Keycloak: records what Central Command asked
# for. `_fake_console` is an autouse fixture and applies here by import.
from tests.unit.cc.test_a3_machine_identity import (  # noqa: F401
    FakeConsole, _fake_console,
)
from tests.unit.cc.test_a30_32_governed_discovery import (
    PREFIX, _agent, _estate, _grant,
)
from tests.unit.cc import s3_estate as E

#: The ten administration calls the eight routes serve (one route carries
#: three transitions). Bodies are valid, so a refusal is always a DECISION.
CALLS = (
    ("PATCH", "", {"description": "g12"}),
    ("PUT", "/bindings", {"scopes": [], "capabilities": []}),
    ("POST", "/preflight", None),
    ("POST", "/acknowledge", None),
    ("POST", "/identity", None),
    ("POST", "/identity/rotate", None),
    ("POST", "/identity/revoke", None),
    ("POST", "/activate", None),
    ("POST", "/pause", None),
    ("POST", "/retire", None),
)

#: The eight ROUTES behind those calls, as the route contract names them.
ROUTES = {
    ("PATCH", "/api/operational-agents/{agent_id}"),
    ("PUT", "/api/operational-agents/{agent_id}/bindings"),
    ("POST", "/api/operational-agents/{agent_id}/preflight"),
    ("POST", "/api/operational-agents/{agent_id}/acknowledge"),
    ("POST", "/api/operational-agents/{agent_id}/identity"),
    ("POST", "/api/operational-agents/{agent_id}/identity/rotate"),
    ("POST", "/api/operational-agents/{agent_id}/identity/revoke"),
    ("POST", "/api/operational-agents/{agent_id}/{transition}"),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _tenant_agent(stack, agent_id, **kw) -> str:
    """An agent holding ONE tenant grant, exactly as the grant route writes
    it (`scope_ref=""`). Returns the grant id."""
    await _agent(stack, agent_id, **kw)
    return await _grant(stack, agent_id, "tenant")


async def _state(stack, agent_id) -> dict:
    """Everything the ten calls could durably change about ONE agent."""
    async with stack.sessionmaker() as session:
        agent = await session.get(CCOperationalAgent, agent_id)
        identity = (await session.execute(
            sa.select(CCAgentIdentity).where(CCAgentIdentity.agent_id == agent_id)
        )).scalar_one_or_none()
        rows = (await session.execute(
            sa.select(CCScopeGrant).where(
                CCScopeGrant.principal_type == PRINCIPAL_AGENT,
                CCScopeGrant.principal_ref == agent_id,
            )
        )).scalars().all()
        preflights = (await session.execute(
            sa.select(sa.func.count()).select_from(CCAgentPreflight)
            .where(CCAgentPreflight.agent_id == agent_id)
        )).scalar_one()
        audit = (await session.execute(
            sa.select(CCAuditLog.action).where(CCAuditLog.subject == agent_id)
            .order_by(CCAuditLog.ts)
        )).scalars().all()
        return {
            "status": agent.status,
            "version": agent.version,
            "description": agent.description,
            "paused_reason": agent.paused_reason,
            "acknowledged_by": agent.activation_acknowledged_by,
            "identity": identity.status if identity is not None else None,
            "active_rows": sorted(
                (r.scope_type, r.scope_ref) for r in rows if r.revoked_at is None
            ),
            "revoked_rows": sorted(
                (r.scope_type, r.scope_ref) for r in rows if r.revoked_at is not None
            ),
            "preflights": preflights,
            "audit": list(audit),
        }


async def _call(stack, agent_id, method, suffix, body=None):
    async with stack.client() as client:
        return await client.request(
            method, f"{PREFIX}/{agent_id}{suffix}", json=body,
        )


async def _all_calls(stack, agent_id) -> dict[str, int]:
    out = {}
    for method, suffix, body in CALLS:
        res = await _call(stack, agent_id, method, suffix, body)
        out[f"{method} {suffix or '/'}"] = res.status_code
    return out


async def _human(stack, subject, scope_type, ref="", *, role="site_admin",
                 subset=None) -> str:
    """A persisted HUMAN grant. Returns its id."""
    return await stack.grant(subject, scope_type, ref, role=role, subset=subset)


@contextmanager
def _foreign(stack, tenant_id: str, subject: str):
    """Act as a tenant owner of ANOTHER tenant, then restore the stack."""
    original = stack.app.dependency_overrides[get_current_user]

    async def _other():
        return UserContext(
            user_id=subject, email=f"{subject}@example.com", tenant_id=tenant_id,
            role="tenant_owner", permissions=list(ROLE_PERMISSIONS["tenant_owner"]),
        )

    stack.app.dependency_overrides[get_current_user] = _other
    try:
        yield
    finally:
        stack.app.dependency_overrides[get_current_user] = original


# ---------------------------------------------------------------------------
# 1. The model: empty is valid for tenant and for nothing else
# ---------------------------------------------------------------------------


class TestTheModel:
    def test_tenant_carries_the_canonical_empty_reference(self):
        rule = ScopeRule(scope_type="tenant", scope_ref="")
        # Not normalised into a sentinel: the reference stays empty.
        assert rule.scope_ref == ""

    @pytest.mark.parametrize("scope_type", ["org_unit", "site", "device", "device_class"])
    def test_every_other_type_still_requires_a_reference(self, scope_type):
        with pytest.raises(ValidationError, match="requires a non-empty scope_ref"):
            ScopeRule(scope_type=scope_type, scope_ref="")

    def test_an_unknown_type_is_not_tenant(self):
        with pytest.raises(ValidationError):
            ScopeRule(scope_type="region", scope_ref="")
        with pytest.raises(ValidationError):
            ScopeRule(scope_type="", scope_ref="")
        with pytest.raises(ValidationError):
            ScopeRule(scope_type="TENANT", scope_ref="")

    def test_what_was_constructible_still_is(self):
        # A tenant row stored with a reference (G12-F1: the grant route
        # writes what it is given) and an unknown type with a reference
        # were constructible before; neither becomes a new 500.
        assert ScopeRule(scope_type="tenant", scope_ref="x").scope_ref == "x"
        assert ScopeRule(scope_type="region", scope_ref="x").scope_type == "region"
        assert ScopeRule(scope_type="site", scope_ref=" ").scope_ref == " "

    def test_the_reference_is_still_required_and_still_bounded(self):
        with pytest.raises(ValidationError):
            ScopeRule(scope_type="tenant")
        for scope_type in ("tenant", "site"):
            with pytest.raises(ValidationError):
                ScopeRule(scope_type=scope_type, scope_ref="x" * 129)
            assert ScopeRule(scope_type=scope_type, scope_ref="x" * 128)


# ---------------------------------------------------------------------------
# 2. Who may administer a tenant rule: asked explicitly, same answer
# ---------------------------------------------------------------------------


class _Recorder:
    """A scope that records the one question it is asked."""

    def __init__(self):
        self.asked = []
        self.unit_paths = {"u1": "/root/u1/"}

    def permits(self, permission, **target):
        self.asked.append((permission, target))
        return "answer"


class TestTheTenantQuestion:
    @pytest.mark.parametrize("permission", ["site.manage", "fleet.view"])
    def test_a_tenant_rule_asks_the_tenant_object_question(self, permission):
        scope = _Recorder()
        rule = SimpleNamespace(scope_type="tenant", scope_ref="")
        assert _scope_rule_within(scope, rule, permission) == "answer"
        assert scope.asked == [(permission, {"tenant_object": True})]

    def test_the_answer_is_what_the_fall_through_already_gave(self):
        # device_class and an unrecognised type still ask exactly this, so
        # the explicit branch changed who is ASKED nothing, only how.
        for scope_type in ("tenant", "device_class", "region"):
            scope = _Recorder()
            _scope_rule_within(
                scope, SimpleNamespace(scope_type=scope_type, scope_ref="x"),
            )
            assert scope.asked == [("site.manage", {"tenant_object": True})], scope_type

    def test_tenant_is_answered_before_any_other_type_is_considered(self):
        # Structural: tenant semantics may not ride on another type's branch
        # (the fall-through through device_class is what A30.33 retired).
        import inspect

        src = inspect.getsource(_scope_rule_within)
        tenant = src.index("rule.scope_type == SCOPE_TENANT")
        for other in ("SCOPE_SITE", "SCOPE_ORG_UNIT", "SCOPE_DEVICE"):
            assert tenant < src.index(f"rule.scope_type == {other}"), other

    def test_the_other_types_are_untouched(self):
        scope = _Recorder()
        for scope_type, ref, expected in (
            ("site", "s1", {"site_id": "s1"}),
            ("org_unit", "u1", {"org_unit_path": "/root/u1/"}),
            ("device", "d1", {"device_agent_id": "d1"}),
        ):
            scope.asked.clear()
            _scope_rule_within(scope, SimpleNamespace(scope_type=scope_type, scope_ref=ref))
            assert scope.asked == [("site.manage", expected)], scope_type


# ---------------------------------------------------------------------------
# 3. The rebuild
# ---------------------------------------------------------------------------


class TestTheRebuild:
    async def test_a_tenant_row_round_trips(self):
        stack = await _estate(("A",))
        await _tenant_agent(stack, "g12-rb")
        await _grant(stack, "g12-rb", "site", "A")
        async with stack.sessionmaker() as session:
            rules = await _agent_scope_rules(OperationalAgentRepo(session), "g12-rb")
        assert sorted((r.scope_type, r.scope_ref) for r in rules) == [
            ("site", stack.site("A")), ("tenant", ""),
        ]

    async def test_an_expired_tenant_row_is_still_configured_and_still_tenant(self):
        # A30.4: the ceiling reads CONFIGURED rows, lapsed included -- so an
        # expired tenant row still demands tenant authority to administer.
        stack = await _estate(("A",))
        grant_id = await _tenant_agent(stack, "g12-exp")
        await stack.lapse(grant_id, how="expired")
        async with stack.sessionmaker() as session:
            rules = await _agent_scope_rules(OperationalAgentRepo(session), "g12-exp")
        assert [(r.scope_type, r.scope_ref) for r in rules] == [("tenant", "")]
        subject, _ = await E.persona(stack, "site_a")
        stack.as_person(subject, "site_admin")
        assert (await _call(stack, "g12-exp", "PATCH", "", {"description": "x"})).status_code == 403
        stack.as_person()
        assert (await _call(stack, "g12-exp", "PATCH", "", {"description": "x"})).status_code == 200

    async def test_a_corrupt_non_tenant_row_still_fails_closed_and_writes_nothing(self):
        # No API writes a site row without a reference; if one exists, the
        # rebuild still refuses it rather than judging it. Nothing moves.
        stack = await _estate(("A",))
        await _agent(stack, "g12-bad")
        async with stack.sessionmaker() as session:
            session.add(CCScopeGrant(
                tenant_id=stack.tenant, principal_type=PRINCIPAL_AGENT,
                principal_ref="g12-bad", scope_type="site", scope_ref="",
                granted_by="corrupt",
            ))
            await session.commit()
        before = await _state(stack, "g12-bad")
        stack.as_person()
        with pytest.raises(ValidationError):
            await _call(stack, "g12-bad", "PATCH", "", {"description": "x"})
        assert await _state(stack, "g12-bad") == before


# ---------------------------------------------------------------------------
# 4. Every control, on a tenant-scoped agent, with its durable effect
# ---------------------------------------------------------------------------


class TestEveryControlWorks:
    async def test_the_full_lifecycle_through_every_route(self):
        stack = await _estate(("A", "C"))
        aid = "g12-life"
        await _tenant_agent(
            stack, aid, status="draft", ceiling=0, approval_always=True,
        )
        stack.as_person()

        res = await _call(stack, aid, "PATCH", "", {"description": "tenant-wide"})
        assert res.status_code == 200, res.text
        s = await _state(stack, aid)
        assert (s["description"], s["version"]) == ("tenant-wide", 2)

        res = await _call(stack, aid, "POST", "/preflight")
        assert res.status_code == 200, res.text
        assert (await _state(stack, aid))["preflights"] == 1

        res = await _call(stack, aid, "POST", "/acknowledge")
        assert res.status_code == 200, res.text
        assert (await _state(stack, aid))["acknowledged_by"]

        res = await _call(stack, aid, "POST", "/activate")
        assert res.status_code == 200, res.text
        assert (await _state(stack, aid))["status"] == "active"

        res = await _call(stack, aid, "POST", "/identity")
        assert res.status_code == 200, res.text
        assert (await _state(stack, aid))["identity"] == "active"
        assert FakeConsole.calls[-1][0] == "provision"

        res = await _call(stack, aid, "POST", "/identity/rotate")
        assert res.status_code == 200, res.text
        assert FakeConsole.calls[-1][0] == "rotate"
        assert (await _state(stack, aid))["identity"] == "active"

        res = await _call(stack, aid, "POST", "/pause")
        assert res.status_code == 200, res.text
        assert (await _state(stack, aid))["status"] == "paused"

        # THE SAFETY CONTROL: revoke. The row first, Keycloak disabled,
        # audited -- through the production route, on a tenant-scoped agent.
        res = await _call(stack, aid, "POST", "/identity/revoke", {"reason": "g12"})
        assert res.status_code == 200, res.text
        s = await _state(stack, aid)
        assert s["identity"] == "revoked"
        assert FakeConsole.calls[-1][0] == "set_enabled" and FakeConsole.calls[-1][-1] is False
        assert "agent_identity.revoked" in s["audit"]

        # THE OTHER: retire. The agent retires and holds no scope any more.
        res = await _call(stack, aid, "POST", "/retire")
        assert res.status_code == 200, res.text
        s = await _state(stack, aid)
        assert s["status"] == "retired"
        assert s["active_rows"] == [] and s["revoked_rows"] == [("tenant", "")]
        async with stack.sessionmaker() as session:
            retired = (await session.execute(
                sa.select(CCAuditLog.detail).where(
                    CCAuditLog.subject == aid,
                    CCAuditLog.action == "operational_agent.retired")
            )).scalar_one()
        assert retired["scopes_revoked"] == 1

    async def test_retire_retires_an_active_identity_on_a_tenant_scoped_agent(self):
        stack = await _estate(("A",))
        aid = "g12-retire"
        await _tenant_agent(stack, aid)
        stack.as_person()
        assert (await _call(stack, aid, "POST", "/identity")).status_code == 200
        FakeConsole.calls.clear()

        res = await _call(stack, aid, "POST", "/retire")
        assert res.status_code == 200, res.text
        s = await _state(stack, aid)
        assert s["status"] == "retired"
        assert s["identity"] == "retired"
        # Keycloak was asked to disable exactly this agent's client.
        disabled = [c for c in FakeConsole.calls if c[0] == "set_enabled"]
        assert len(disabled) == 1 and disabled[0][-1] is False, FakeConsole.calls
        assert aid in disabled[0][2], disabled
        assert "agent_identity.retired" in s["audit"]
        assert s["active_rows"] == []
        # A retired agent is a closed record: nothing reconfigures it.
        assert (await _call(stack, aid, "POST", "/identity")).status_code == 409

    async def test_a_bindings_replacement_cannot_carry_tenant_and_drops_it(self):
        # G12-F2, pinned: `PUT /bindings` is a FULL replacement and the agent
        # API does not speak tenant, so the tenant row is revoked (narrowing
        # only, audited, visible in the response).
        stack = await _estate(("A",))
        aid = "g12-bind"
        await _tenant_agent(stack, aid)
        stack.as_person()
        res = await _call(stack, aid, "PUT", "/bindings", {
            "scopes": [{"scope_type": "site", "scope_ref": stack.site("A")}],
            "capabilities": [{"kind": "action_class", "capability_ref": "SEL_CLEAR"}],
        })
        assert res.status_code == 200, res.text
        assert [(r["scope_type"], r["scope_ref"]) for r in res.json()["scopes"]] == [
            ("site", stack.site("A")),
        ]
        s = await _state(stack, aid)
        assert s["active_rows"] == [("site", stack.site("A"))]
        assert s["revoked_rows"] == [("tenant", "")]

        # ...and it cannot be put back through this API.
        res = await _call(stack, aid, "PUT", "/bindings", {
            "scopes": [{"scope_type": "tenant", "scope_ref": ""}],
            "capabilities": [],
        })
        assert res.status_code == 400, res.text
        assert (await _state(stack, aid))["active_rows"] == [("site", stack.site("A"))]


# ---------------------------------------------------------------------------
# 5. Who may administer it: tenant authority, and nobody narrower
# ---------------------------------------------------------------------------


async def _refused_everywhere(stack, aid, expected) -> None:
    before = await _state(stack, aid)
    codes = await _all_calls(stack, aid)
    assert set(codes.values()) == {expected}, codes
    assert await _state(stack, aid) == before, "a refused call changed something"


class TestNobodyNarrowerAdministersIt:
    """Every refusal is asked of all ten calls, and none may move anything."""

    async def _armed(self):
        stack = await _estate(("A", "B", "C"))
        aid = "g12-target"
        await _tenant_agent(stack, aid)
        stack.as_person()
        assert (await _call(stack, aid, "POST", "/identity")).status_code == 200
        return stack, aid

    @pytest.mark.parametrize(
        "persona", ["org_ab", "site_a", "device_a", "class_server", "no_scope"],
    )
    async def test_a_narrower_administrator_is_refused(self, persona):
        stack, aid = await self._armed()
        subject, _ = await E.persona(stack, persona)
        stack.as_person(subject, "site_admin")
        await _refused_everywhere(stack, aid, 403)

    async def test_tenant_scope_without_site_manage_is_refused(self):
        # The grant is tenant-wide, the token's role nominally holds
        # site.manage, and the grant withholds it: the grant decides.
        stack, aid = await self._armed()
        await _human(stack, "g12-narrow", "tenant", role="tenant_owner",
                     subset=["fleet.view", "incident.view"])
        stack.as_person("g12-narrow", "tenant_owner")
        await _refused_everywhere(stack, aid, 403)

    async def test_a_role_without_site_manage_is_refused_at_the_guard(self):
        stack, aid = await self._armed()
        await _human(stack, "g12-operator", "tenant", role="operator")
        stack.as_person("g12-operator", "operator")
        await _refused_everywhere(stack, aid, 403)

    @pytest.mark.parametrize("how", ["revoked", "expired"])
    async def test_a_lapsed_tenant_grant_confers_nothing(self, how):
        stack, aid = await self._armed()
        grant_id = await _human(stack, "g12-lapsed", "tenant", role="tenant_owner")
        stack.as_person("g12-lapsed", "tenant_owner")
        assert (await _call(stack, aid, "PATCH", "", {"description": "live"})).status_code == 200
        await stack.lapse(grant_id, how=how)
        await _refused_everywhere(stack, aid, 403)

    async def test_an_inert_grant_confers_nothing(self):
        # A grant to a site that does not exist is inert (A23-3): it covers
        # nothing, so it administers nothing -- not even a tenant row.
        stack, aid = await self._armed()
        await _human(stack, "g12-inert", "site", "no-such-site", role="site_admin")
        stack.as_person("g12-inert", "site_admin")
        await _refused_everywhere(stack, aid, 403)

    async def test_another_tenant_never_reaches_it(self):
        stack, aid = await self._armed()
        async with stack.sessionmaker() as session:
            await ScopeGrantRepo(session).grant(
                tenant_id="g12-rival", principal_type=PRINCIPAL_USER,
                principal_ref="g12-rival-owner", scope_type="tenant", scope_ref="",
                role="tenant_owner", granted_by="g12-test",
            )
            await session.commit()
        with _foreign(stack, "g12-rival", "g12-rival-owner"):
            await _refused_everywhere(stack, aid, 404)

    async def test_the_agent_cannot_administer_itself(self):
        stack, aid = await self._armed()
        stack.as_machine(aid)
        await _refused_everywhere(stack, aid, 403)

    async def test_a_second_tenant_administrator_is_as_good_as_the_first(self):
        stack, aid = await self._armed()
        await _human(stack, "g12-admin2", "tenant", role="tenant_owner")
        stack.as_person("g12-admin2", "tenant_owner")
        res = await _call(stack, aid, "POST", "/identity/revoke", {"reason": "second"})
        assert res.status_code == 200, res.text
        assert (await _state(stack, aid))["identity"] == "revoked"


# ---------------------------------------------------------------------------
# 6. Every other scope type: the table did not move
# ---------------------------------------------------------------------------


class TestTheOtherTypesAreUnchanged:
    @pytest.mark.parametrize("persona, role, a, c", [
        ("tenant", "tenant_owner", 200, 200),
        ("org_ab", "site_admin", 200, 403),
        ("site_a", "site_admin", 200, 403),
        ("device_a", "site_admin", 403, 403),
        ("class_server", "site_admin", 403, 403),
        ("no_scope", "site_admin", 403, 403),
    ])
    async def test_site_scoped_agents(self, persona, role, a, c):
        stack = await _estate(("A", "C"))
        await _agent(stack, "g12-at-a")
        await _grant(stack, "g12-at-a", "site", "A")
        await _agent(stack, "g12-at-c")
        await _grant(stack, "g12-at-c", "site", "C")
        subject, _ = await E.persona(stack, persona)
        stack.as_person(subject, role)
        got_a = (await _call(stack, "g12-at-a", "PATCH", "", {"description": "x"})).status_code
        got_c = (await _call(stack, "g12-at-c", "PATCH", "", {"description": "x"})).status_code
        assert (got_a, got_c) == (a, c)

    async def test_an_org_scoped_agent(self):
        stack = await _estate(("A", "C"))
        await _agent(stack, "g12-org")
        await _grant(stack, "g12-org", "org_unit", "ab")
        for persona, expected in (("org_ab", 200), ("site_a", 403), ("tenant", 200)):
            subject, _ = await E.persona(stack, persona)
            stack.as_person(subject, "tenant_owner" if persona == "tenant" else "site_admin")
            got = (await _call(stack, "g12-org", "PATCH", "", {"description": persona})).status_code
            assert got == expected, persona


# ---------------------------------------------------------------------------
# 7. The request path: a tenant rule is still never written by this API
# ---------------------------------------------------------------------------


def _create_body(name, scopes):
    return {"name": name, "scopes": scopes,
            "capabilities": [{"kind": "action_class", "capability_ref": "SEL_CLEAR"}]}


async def _agent_named(stack, name) -> bool:
    async with stack.sessionmaker() as session:
        return (await session.execute(
            sa.select(sa.func.count()).select_from(CCOperationalAgent)
            .where(CCOperationalAgent.name == name)
        )).scalar_one() > 0


class TestTheRequestPath:
    @pytest.mark.parametrize("who, rule, expected", [
        # A tenant rule: refused by the ceiling unless the caller is
        # tenant-wide, then by the agent API's own vocabulary.
        ("site_a", {"scope_type": "tenant", "scope_ref": ""}, 403),
        ("owner", {"scope_type": "tenant", "scope_ref": ""}, 400),
        ("owner", {"scope_type": "tenant", "scope_ref": "x"}, 400),
        # A non-tenant empty reference is still a validation failure.
        ("owner", {"scope_type": "site", "scope_ref": ""}, 422),
        ("site_a", {"scope_type": "site", "scope_ref": ""}, 422),
        ("owner", {"scope_type": "device_class", "scope_ref": ""}, 422),
        # An unrecognised type needs tenant authority, then is refused.
        ("site_a", {"scope_type": "region", "scope_ref": "x"}, 403),
        ("owner", {"scope_type": "region", "scope_ref": "x"}, 400),
        ("owner", {"scope_type": "region", "scope_ref": ""}, 422),
    ])
    async def test_create_never_writes_what_it_refuses(self, who, rule, expected):
        stack = await _estate(("A",))
        if who == "owner":
            stack.as_person()
        else:
            subject, _ = await E.persona(stack, who)
            stack.as_person(subject, "site_admin")
        name = f"g12-create-{who}-{rule['scope_type']}-{bool(rule['scope_ref'])}"
        async with stack.client() as client:
            res = await client.post(f"{PREFIX}/", json=_create_body(name, [rule]))
        assert res.status_code == expected, res.text
        assert not await _agent_named(stack, name)

    async def test_bindings_cannot_introduce_tenant_either(self):
        stack = await _estate(("A",))
        await _agent(stack, "g12-site-bind")
        await _grant(stack, "g12-site-bind", "site", "A")
        before = await _state(stack, "g12-site-bind")
        stack.as_person()
        res = await _call(stack, "g12-site-bind", "PUT", "/bindings", {
            "scopes": [{"scope_type": "tenant", "scope_ref": ""}], "capabilities": [],
        })
        assert res.status_code == 400, res.text
        assert (await _state(stack, "g12-site-bind"))["active_rows"] == before["active_rows"]

    async def test_the_published_schema_states_the_rule(self):
        stack = await _estate(("A",))
        schemas = stack.app.openapi()["components"]["schemas"]
        rules = [
            s for s in schemas.values()
            if "canonical reference is empty"
            in (s.get("properties", {}).get("scope_ref", {}).get("description") or "")
        ]
        assert len(rules) == 1
        ref = rules[0]["properties"]["scope_ref"]
        assert "minLength" not in ref and ref["maxLength"] == 128
        assert "scope_ref" in rules[0]["required"]


# ---------------------------------------------------------------------------
# 8. A23: an agent's row is never an administrator
# ---------------------------------------------------------------------------


class TestA23IsUntouched:
    def test_an_agent_tenant_row_never_counts_toward_the_last_administrator(self):
        def row(principal_type, ref, role):
            return SimpleNamespace(
                id=f"{principal_type}-{ref}", principal_type=principal_type,
                principal_ref=ref, scope_type="tenant", scope_ref="", role=role,
                permission_subset=None, revoked_at=None, expires_at=None, realm="",
            )

        perms = lambda r: ROLE_PERMISSIONS.get(r.role or "", [])  # noqa: E731
        # Even an agent row naming the most powerful role counts for nothing.
        rows = [row(PRINCIPAL_USER, "owner", "tenant_owner"),
                row(PRINCIPAL_AGENT, "g12-agent", "tenant_owner")]
        assert count_tenant_admins(rows, perms) == 1
        assert count_tenant_admins(rows[1:], perms) == 0

    async def test_administering_a_tenant_agent_leaves_the_human_administrators_alone(self):
        stack = await _estate(("A",))
        aid = "g12-a23"
        await _tenant_agent(stack, aid)

        async def _humans():
            async with stack.sessionmaker() as session:
                return sorted(
                    (r.principal_ref, r.revoked_at is None) for r in (await session.execute(
                        sa.select(CCScopeGrant).where(
                            CCScopeGrant.principal_type == PRINCIPAL_USER)
                    )).scalars().all()
                )

        before = await _humans()
        stack.as_person()
        for method, suffix, body in (
            ("POST", "/identity", None), ("POST", "/identity/revoke", None),
            ("PUT", "/bindings", {"scopes": [], "capabilities": []}),
            ("POST", "/retire", None),
        ):
            res = await _call(stack, aid, method, suffix, body)
            assert res.status_code == 200, (suffix, res.text)
        assert await _humans() == before


# ---------------------------------------------------------------------------
# 9. The inventory itself
# ---------------------------------------------------------------------------


class TestTheInventory:
    def test_the_eight_routes_are_the_rebuilds_callers_and_all_human_site_manage(self):
        import ast
        import inspect

        from harkeniq_cc.api import operational_agents as module
        from harkeniq_cc.route_contract import MACHINE_SURFACE, ROUTE_CONTRACT

        tree = ast.parse(inspect.getsource(module))
        callers = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and any(
                isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_agent_scope_rules"
                for n in ast.walk(node)
            ):
                for deco in node.decorator_list:
                    if isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute):
                        callers.add((deco.func.attr.upper(),
                                     PREFIX + deco.args[0].value))
        assert callers == ROUTES
        for key in ROUTES:
            permission, _, mutation = ROUTE_CONTRACT[key]
            assert (permission, mutation) == ("site.manage", True), key
            assert key not in MACHINE_SURFACE, key

    async def test_the_pre_fix_rule_raised_and_no_call_raises_now(self):
        """The regression, stated as the defect: the rebuild of a stored
        tenant row with the pre-fix rule raises; with the rule, it does not."""
        from pydantic import BaseModel, Field

        class PreFix(BaseModel):
            scope_type: str
            scope_ref: str = Field(..., min_length=1, max_length=128)

        with pytest.raises(ValidationError):
            PreFix(scope_type="tenant", scope_ref="")
        stack = await _estate(("A",))
        await _tenant_agent(stack, "g12-inv", status="draft", ceiling=0,
                            approval_always=True)
        stack.as_person()
        codes = await _all_calls(stack, "g12-inv")
        assert 500 not in codes.values(), codes


# ---------------------------------------------------------------------------
# 10. Recorded, not changed (G12-F1, G12-F3): today's behaviour, pinned so a
#     later decision changes a test deliberately rather than silently
# ---------------------------------------------------------------------------


class TestRecordedFollowUps:
    async def test_f1_the_grant_route_stores_a_tenant_reference_verbatim(self):
        # The canonical tenant reference is empty; the grant route does not
        # enforce that at write. The row it stores is still judged as tenant.
        stack = await _estate(("A",))
        await _agent(stack, "g12-f1")
        stack.as_person()
        async with stack.client() as client:
            res = await client.post("/api/scope-grants/", json={
                "principal_type": "agent", "principal_ref": "g12-f1",
                "scope_type": "tenant", "scope_ref": "not-canonical",
            })
        assert res.status_code == 201, res.text
        assert (await _state(stack, "g12-f1"))["active_rows"] == [("tenant", "not-canonical")]
        subject, _ = await E.persona(stack, "site_a")
        stack.as_person(subject, "site_admin")
        assert (await _call(stack, "g12-f1", "PATCH", "", {"description": "x"})).status_code == 403
        stack.as_person()
        assert (await _call(stack, "g12-f1", "PATCH", "", {"description": "x"})).status_code == 200

    async def test_f3_a_scope_less_agent_is_administered_without_being_visible(self):
        # PRE-EXISTING (A0/E1.2): the ceiling has nothing to cap for an agent
        # with no rows, while read visibility treats it as tenant-level. It
        # reaches no device, so no reach moves. Recorded for its own decision.
        stack = await _estate(("A",))
        await _agent(stack, "g12-f3")
        subject, _ = await E.persona(stack, "site_a")
        stack.as_person(subject, "site_admin")
        assert (await _call(stack, "g12-f3", "PATCH", "", {"description": "x"})).status_code == 200
        async with stack.client() as client:
            assert (await client.get(f"{PREFIX}/g12-f3")).status_code == 404
