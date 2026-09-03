"""A23-4: synthesis only for the never-granted, and never for an agent (spec A23.10).

The escalation this closes: under `legacy_open` the resolver handed a
synthesized tenant-wide grant to any principal whose EFFECTIVE grant
list was empty. A revoked grant, an expired grant, a grant narrowed to
nothing and (until A23-3) a grant to a vanished target all emptied that
list, so "was granted once and lost it" read exactly like "was never
granted". A23-3 kept vanished targets in the list as inert; A23-4 makes
the rule itself correct: the resolver now sees EVIDENCE -- every row
that ever named the principal, whatever its lifecycle state -- and
synthesizes only where there is none, and only for a human.

    NEVER GRANTED          -> legacy_open may synthesize (upgrade behaviour)
    PREVIOUSLY GRANTED     -> no synthesis, no fallback, no widening
    OPERATIONAL AGENT      -> no synthesis under ANY posture (A0)
    STRICT                 -> no synthesis, as before

The mandated matrix, 1..18, then the adversarial regression that must
fail the day the old rule comes back. Resolver rows execute `resolve()`
directly; API rows go through the real ASGI app, the one loader and the
real repositories.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from harkeniq_cc import scope as scope_mod
from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext
from harkeniq_cc.db.models import CCApprovalGroup, CCApprovalGroupMember
from harkeniq_cc.db.repos import (
    OperationalAgentRepo,
    ScopeGrantRepo,
    TenantSettingsRepo,
)
from harkeniq_cc.governance import load_agent_scope, load_scope
from harkeniq_cc.scope import (
    ENFORCEMENT_LEGACY_OPEN,
    ENFORCEMENT_STRICT,
    PRINCIPAL_AGENT,
    PRINCIPAL_USER,
    SCOPE_ONLY_MARKER,
    SCOPE_ORG_UNIT,
    SCOPE_SITE,
    SCOPE_TENANT,
    resolve,
)

from tests.unit.cc.test_a23_3_recovery import (
    _grant,
    _legacy,
    _row,
    _seed,
    _vanish_site,
    _vanish_unit,
)
from tests.unit.cc.test_e1_scope_api_and_chain import TENANT, _client, _strict

NOW = datetime.now(timezone.utc)
PAST = NOW - timedelta(days=1)
FUTURE = NOW + timedelta(days=30)
OWNER = ROLE_PERMISSIONS["tenant_owner"]
VIEWER = ROLE_PERMISSIONS["viewer"]
OTHER_TENANT = "tenant-other"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def row(scope_type, scope_ref="", *, role="site_admin", subset=None,
        revoked_at=None, expires_at=None, principal_type=PRINCIPAL_USER,
        realm="", ref="kc-x"):
    return SimpleNamespace(
        id=f"g-{scope_type}-{scope_ref}-{revoked_at}-{expires_at}",
        principal_type=principal_type, principal_ref=ref,
        scope_type=scope_type, scope_ref=scope_ref, role=role,
        permission_subset=subset, expires_at=expires_at, revoked_at=revoked_at,
        realm=realm,
    )


UNIT = SimpleNamespace(id="u-a", path="/root/u-a/", parent_id="root", org_unit_id=None)
ROOT = SimpleNamespace(id="root", path="/root/", parent_id=None, org_unit_id=None)
SITE_A = SimpleNamespace(id="site-a", org_unit_id="u-a")
SITE_B = SimpleNamespace(id="site-b", org_unit_id="root")
TREE = dict(org_units=[ROOT, UNIT], sites=[SITE_A, SITE_B])


def _resolve(rows, *, enforcement=ENFORCEMENT_LEGACY_OPEN, principal_type=PRINCIPAL_USER,
             role_permissions=OWNER, prior=(), ref="kc-x"):
    return resolve(
        tenant_id=TENANT, principal_type=principal_type, principal_ref=ref,
        role_permissions=role_permissions, grant_rows=rows,
        enforcement=enforcement, prior_grants=prior, **TREE,
    )


def _assert_no_widening(scope):
    """The A23.10 shape: no tenant reach, no site, nothing synthesized."""
    assert scope.tenant_wide is False
    assert scope.site_ids == frozenset()
    assert scope.org_unit_paths == frozenset()
    assert not any(g.synthesized for g in scope.grants)
    assert scope.synthesis != "never_granted"


async def _me(app, role, ref):
    async with _client(app, role, ref) as c:
        r = await c.get("/api/scope-grants/me")
        assert r.status_code == 200, r.text
        return r.json()


async def _fleet_count(app, role, ref) -> int:
    async with _client(app, role, ref) as c:
        r = await c.get("/api/fleet/?page_size=200")
        assert r.status_code == 200, r.text
        d = r.json()
        return len(d.get("devices", d.get("items", [])))


async def _revoke(sessionmaker, grant_id):
    async with sessionmaker() as session:
        repo = ScopeGrantRepo(session)
        await repo.revoke(await repo.get(TENANT, grant_id), "test")
        await session.commit()


async def _agent(sessionmaker, name="Loose", scopes=()):
    async with sessionmaker() as session:
        repo = OperationalAgentRepo(session)
        agent = await repo.create(
            tenant_id=TENANT, name=name, description="", autonomy_ceiling=0,
            require_approval_always=True, max_proposals_per_day=10,
            created_by="test",
        )
        for scope_type, scope_ref in scopes:
            await repo.add_scope(
                agent_id=agent.id, tenant_id=TENANT, scope_type=scope_type,
                scope_ref=scope_ref, granted_by="test",
            )
        await session.commit()
        return agent.id


def _machine_client(app, agent_id):
    async def _fake():
        return UserContext(
            user_id=agent_id, email="", tenant_id=TENANT, role="",
            permissions=["fleet.view", "incident.view"], species="agent",
            identity_id="ident-1",
        )

    app.dependency_overrides[get_current_user] = _fake
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# ---------------------------------------------------------------------------
# The resolver rule (matrix rows executed against resolve() itself)
# ---------------------------------------------------------------------------


class TestResolverRule:
    def test_1_never_granted_under_legacy_open_may_synthesize(self):
        scope = _resolve([])
        assert scope.tenant_wide is True
        assert scope.synthesis == "never_granted"
        assert scope.previously_granted is False
        assert scope.administered is False
        assert [g.synthesized for g in scope.grants] == [True]

    def test_2_an_active_narrow_grant_is_not_widened(self):
        scope = _resolve([row(SCOPE_SITE, "site-a")])
        assert scope.tenant_wide is False
        assert scope.site_ids == {"site-a"}
        assert scope.synthesis == "granted"
        assert scope.previously_granted is True

    def test_3_a_revoked_narrow_grant_never_synthesizes(self):
        scope = _resolve([row(SCOPE_SITE, "site-a", revoked_at=PAST)])
        _assert_no_widening(scope)
        assert scope.synthesis == "previously_granted"
        assert scope.previously_granted is True
        assert scope.administered is True
        assert scope.is_empty()

    def test_4_an_expired_narrow_grant_never_synthesizes(self):
        scope = _resolve([row(SCOPE_SITE, "site-a", expires_at=PAST)])
        _assert_no_widening(scope)
        assert scope.synthesis == "previously_granted"

    def test_5_an_orphaned_inert_grant_never_synthesizes(self):
        scope = _resolve([row(SCOPE_ORG_UNIT, "u-gone")])
        _assert_no_widening(scope)
        assert scope.synthesis == "granted"  # an inert grant IS a grant: retained
        assert scope.grants[0].inert and scope.grants[0].inert_reason == "org_unit_missing"

    def test_6_a_vanished_site_target_never_synthesizes(self):
        scope = _resolve([row(SCOPE_SITE, "site-gone")])
        _assert_no_widening(scope)
        assert scope.grants[0].inert_reason == "site_missing"

    def test_7_a_vanished_org_unit_target_with_a_revoked_sibling_never_synthesizes(self):
        scope = _resolve([
            row(SCOPE_ORG_UNIT, "u-gone"),
            row(SCOPE_SITE, "site-a", revoked_at=PAST),
        ])
        _assert_no_widening(scope)
        assert scope.previously_granted is True

    @pytest.mark.parametrize("bad", [
        row("bogus", "x"),                                   # unknown scope type
        row(SCOPE_SITE, "site-a", subset=[]),                # narrowed to nothing
        row(SCOPE_SITE, "site-a", subset=["not.a.permission"]),
    ])
    def test_8_an_invalid_or_empty_grant_is_evidence_not_absence(self, bad):
        scope = _resolve([bad])
        _assert_no_widening(scope)
        assert scope.grants == ()
        assert scope.previously_granted is True
        assert scope.synthesis == "previously_granted"

    def test_9_many_historical_grants_all_ineffective_never_synthesize(self):
        scope = _resolve([
            row(SCOPE_SITE, "site-a", revoked_at=PAST),
            row(SCOPE_SITE, "site-b", expires_at=PAST),
            row(SCOPE_ORG_UNIT, "u-a", revoked_at=PAST),
            row(SCOPE_TENANT, revoked_at=PAST),
            row("bogus", "x"),
        ])
        _assert_no_widening(scope)
        assert scope.grants == ()
        assert scope.synthesis == "previously_granted"

    def test_10_a_historical_grant_elsewhere_does_not_reach_here(self):
        scope = _resolve([row(SCOPE_SITE, "site-a", revoked_at=PAST)])
        assert scope.permits("fleet.view", site_id="site-b") is False
        assert scope.permits("fleet.view", site_id="site-a") is False
        assert scope.permits("fleet.view", tenant_object=True) is False
        assert scope.covers_site("site-b") is False

    def test_11_historical_grant_but_no_effective_reach_means_reach_none(self):
        scope = _resolve([row(SCOPE_SITE, "site-a", revoked_at=PAST)])
        assert scope.is_empty()
        assert scope.effective_grants == ()
        assert scope.may_ever("fleet.view") is False
        assert scope.device_ids == frozenset() and scope.device_classes == frozenset()

    def test_12_an_agent_with_no_rows_gets_no_synthesis(self):
        scope = _resolve([], principal_type=PRINCIPAL_AGENT,
                         role_permissions=[SCOPE_ONLY_MARKER], ref="agent-1")
        _assert_no_widening(scope)
        assert scope.synthesis == "agent"
        assert scope.previously_granted is False
        assert scope.is_empty()

    def test_13_an_agent_with_revoked_or_expired_rows_gets_no_synthesis(self):
        scope = _resolve(
            [row(SCOPE_SITE, "site-a", revoked_at=PAST, principal_type=PRINCIPAL_AGENT),
             row(SCOPE_SITE, "site-b", expires_at=PAST, principal_type=PRINCIPAL_AGENT)],
            principal_type=PRINCIPAL_AGENT, role_permissions=[SCOPE_ONLY_MARKER],
            ref="agent-1",
        )
        _assert_no_widening(scope)
        assert scope.synthesis == "agent"
        assert scope.previously_granted is True

    def test_12b_an_agent_reports_agent_under_strict_too(self):
        """A23.10: no synthesis for an agent under ANY posture, and the
        reason names the agent, not the posture."""
        scope = _resolve([], principal_type=PRINCIPAL_AGENT,
                         role_permissions=[SCOPE_ONLY_MARKER], ref="agent-1",
                         enforcement=ENFORCEMENT_STRICT)
        _assert_no_widening(scope)
        assert scope.synthesis == "agent"

    def test_14_a_genuinely_never_granted_human_keeps_legacy_behaviour(self):
        scope = _resolve([], role_permissions=VIEWER)
        assert scope.tenant_wide is True
        assert scope.site_ids == {"site-a", "site-b"}
        assert scope.grants[0].permissions == frozenset(VIEWER)
        assert scope.permits("fleet.view", site_id="site-b") is True
        assert scope.permits("site.manage", site_id="site-b") is False

    @pytest.mark.parametrize("rows", [[], [row(SCOPE_SITE, "site-a", revoked_at=PAST)]])
    def test_15_strict_mode_never_synthesizes(self, rows):
        scope = _resolve(rows, enforcement=ENFORCEMENT_STRICT)
        _assert_no_widening(scope)
        assert scope.synthesis == "strict"

    def test_18_the_a23_3_inert_contract_is_unchanged(self):
        """Regression against A23-3: inert grants are retained, reported,
        cover nothing, and block synthesis exactly as before."""
        scope = _resolve([row(SCOPE_ORG_UNIT, "u-gone"), row(SCOPE_SITE, "site-gone")])
        assert len(scope.grants) == 2 and all(g.inert for g in scope.grants)
        assert [g.inert_reason for g in scope.inert_grants] == ["org_unit_missing", "site_missing"]
        assert scope.effective_grants == ()
        assert scope.administered is True
        assert scope.is_empty()
        assert not any(g.synthesized for g in scope.grants)
        for g in scope.grants:
            assert not g.covers_tenant()
            assert not g.covers_site("site-a", "/root/u-a/")

    def test_prior_grants_are_evidence_only_and_never_reach(self):
        """A row passed as evidence adds NOTHING: not a site, not a
        permission, not a tenant flag -- even an ACTIVE tenant row."""
        scope = _resolve([], prior=[row(SCOPE_TENANT, role="tenant_owner")])
        _assert_no_widening(scope)
        assert scope.grants == ()
        assert scope.previously_granted is True
        assert scope.synthesis == "previously_granted"
        assert scope.permits("fleet.view", tenant_object=True) is False

    def test_the_synthesis_branch_is_structurally_gated(self):
        """The old rule was `if not grants and legacy_open`. The new one
        must name the principal type and the evidence, in the source."""
        src = inspect.getsource(scope_mod.resolve)
        assert "previously_granted" in src
        assert "principal_type != PRINCIPAL_USER" in src
        # Exactly one place constructs a synthesized grant, and it is the
        # never-granted branch.
        assert src.count("synthesized=True") == 1
        head, _, tail = src.partition("synthesized=True")
        assert 'synthesis = "never_granted"' in head.rsplit("elif", 1)[-1] or \
            'synthesis = "never_granted"' in head[-400:]


# ---------------------------------------------------------------------------
# Over the app: the one loader, the real repositories, the real tree
# ---------------------------------------------------------------------------


class TestOverTheApp:
    @pytest.mark.asyncio
    async def test_never_granted_and_previously_granted_are_different_answers(self):
        app, sm, estate = await _seed(strict=False)
        # Never granted: legacy behaviour, tenant-wide, both devices.
        me = await _me(app, "viewer", "kc-new")
        assert me["tenant_wide"] is True and me["synthesis"] == "never_granted"
        assert me["previously_granted"] is False and me["administered"] is False
        assert await _fleet_count(app, "viewer", "kc-new") == 2
        # Previously granted, now revoked: nothing.
        gid = await _grant(sm, "kc-x", SCOPE_SITE, estate["site-1"], role="viewer")
        await _revoke(sm, gid)
        me = await _me(app, "viewer", "kc-x")
        assert me["tenant_wide"] is False and me["synthesis"] == "previously_granted"
        assert me["previously_granted"] is True and me["administered"] is True
        assert me["site_ids"] == [] and me["grants"] == []
        assert await _fleet_count(app, "viewer", "kc-x") == 0

    @pytest.mark.asyncio
    async def test_4_expired_over_the_app(self):
        app, sm, estate = await _seed(strict=False)
        await _grant(sm, "kc-x", SCOPE_SITE, estate["site-1"], role="viewer", expires_at=PAST)
        me = await _me(app, "viewer", "kc-x")
        assert me["tenant_wide"] is False and me["synthesis"] == "previously_granted"
        assert await _fleet_count(app, "viewer", "kc-x") == 0

    @pytest.mark.asyncio
    async def test_7_vanished_org_unit_over_the_app(self):
        app, sm, estate = await _seed(strict=False)
        await _grant(sm, "kc-x", SCOPE_ORG_UNIT, estate["a1"], role="viewer")
        await _vanish_unit(sm, estate, "a1")
        me = await _me(app, "viewer", "kc-x")
        assert me["tenant_wide"] is False
        assert me["inert_grants"][0]["reason"] == "org_unit_missing"
        assert me["previously_granted"] is True and me["synthesis"] == "granted"
        assert await _fleet_count(app, "viewer", "kc-x") == 0

    @pytest.mark.asyncio
    async def test_6_vanished_site_over_the_app(self):
        app, sm, estate = await _seed(strict=False)
        await _grant(sm, "kc-x", SCOPE_SITE, estate["site-1"], role="viewer")
        await _vanish_site(sm, estate["site-1"])
        me = await _me(app, "viewer", "kc-x")
        assert me["tenant_wide"] is False
        assert me["inert_grants"][0]["reason"] == "site_missing"
        assert await _fleet_count(app, "viewer", "kc-x") == 0

    @pytest.mark.asyncio
    async def test_15_strict_over_the_app(self):
        app, sm, estate = await _seed(strict=True)
        me = await _me(app, "viewer", "kc-new")
        assert me["tenant_wide"] is False and me["synthesis"] == "strict"
        assert await _fleet_count(app, "viewer", "kc-new") == 0

    @pytest.mark.asyncio
    async def test_16_one_tenants_evidence_does_not_cross_into_another(self):
        app, sm, estate = await _seed(strict=False)
        async with sm() as session:
            r = await ScopeGrantRepo(session).grant(
                tenant_id=OTHER_TENANT, principal_type=PRINCIPAL_USER,
                principal_ref="kc-x", scope_type=SCOPE_TENANT, scope_ref="",
                role="tenant_owner", granted_by="seed",
            )
            await ScopeGrantRepo(session).revoke(r, "seed")
            await session.commit()
        # In THIS tenant kc-x was never granted: legacy behaviour holds,
        # because another tenant's rows are not this tenant's evidence.
        me = await _me(app, "viewer", "kc-x")
        assert me["synthesis"] == "never_granted" and me["previously_granted"] is False
        # And this tenant's revoked grant is not evidence over there.
        gid = await _grant(sm, "kc-y", SCOPE_SITE, estate["site-1"], role="viewer")
        await _revoke(sm, gid)
        async with sm() as session:
            # A23-5: the other tenant needs its own explicit posture --
            # a missing row is STRICT now, and the point of this
            # assertion is the `never_granted` LABEL, which only a
            # legacy tenant can produce.
            await TenantSettingsRepo(session).set_enforcement(
                OTHER_TENANT, ENFORCEMENT_LEGACY_OPEN, "migration:0021",
            )
            await session.commit()
        async with sm() as session:
            other = await load_scope(
                session, tenant_id=OTHER_TENANT, principal_ref="kc-y",
                role_permissions=VIEWER,
            )
        assert other.previously_granted is False and other.synthesis == "never_granted"

    @pytest.mark.asyncio
    async def test_17a_a_legacy_row_keyed_by_the_tokens_email_is_evidence_not_authority(self):
        """The token carries subject AND email, authenticated together.
        A grant row somebody once keyed by that email proves the person
        was administered -- and still authorizes nothing."""
        app, sm, estate = await _seed(strict=False)
        await _grant(sm, "KC-X@example.com", SCOPE_TENANT, role="tenant_owner")
        me = await _me(app, "viewer", "kc-x")   # _client's email is kc-x@example.com
        assert me["previously_granted"] is True
        assert me["synthesis"] == "previously_granted"
        assert me["tenant_wide"] is False and me["grants"] == []
        assert await _fleet_count(app, "viewer", "kc-x") == 0

    @pytest.mark.asyncio
    async def test_17b_a_recorded_email_subject_pair_resolves_the_alias_without_a_token(self):
        """The in-process loader has no email claim. The platform's own
        recorded pairs (A23-2 identity evidence) supply the alias."""
        app, sm, estate = await _seed(strict=False)
        async with sm() as session:
            group = CCApprovalGroup(tenant_id=TENANT, name="ops")
            session.add(group)
            await session.flush()
            session.add(CCApprovalGroupMember(
                group_id=group.id, user_email="Ops.Lead@example.com", principal_ref="kc-x",
            ))
            await session.commit()
        await _grant(sm, "ops.lead@example.com", SCOPE_SITE, estate["site-1"], role="viewer")
        async with sm() as session:
            scope = await load_scope(
                session, tenant_id=TENANT, principal_ref="kc-x", role_permissions=VIEWER,
            )
        assert scope.previously_granted is True and scope.synthesis == "previously_granted"
        assert scope.tenant_wide is False and scope.site_ids == frozenset()

    @pytest.mark.asyncio
    async def test_17c_an_unrelated_email_is_not_evidence(self):
        app, sm, estate = await _seed(strict=False)
        await _grant(sm, "someone-else@example.com", SCOPE_TENANT, role="tenant_owner")
        me = await _me(app, "viewer", "kc-x")
        assert me["previously_granted"] is False and me["synthesis"] == "never_granted"

    @pytest.mark.asyncio
    async def test_a_stale_realm_row_is_evidence_and_authorizes_nothing(self):
        """Ratified for A23-4: a row under another realm names this
        principal in this tenant. Evidence, fail closed; no reach."""
        app, sm, estate = await _seed(strict=False)
        await _grant(sm, "kc-x", SCOPE_TENANT, role="tenant_owner", realm="old-realm")
        async with sm() as session:
            scope = await load_scope(
                session, tenant_id=TENANT, principal_ref="kc-x",
                role_permissions=OWNER, realm="tenant-demo",
            )
        assert scope.grants == ()
        assert scope.previously_granted is True and scope.synthesis == "previously_granted"
        assert scope.tenant_wide is False

    @pytest.mark.asyncio
    async def test_12_13_an_agent_gets_no_synthesis_in_process_or_over_http(self):
        app, sm, estate = await _seed(strict=False)
        bare = await _agent(sm, "Bare")
        lost = await _agent(sm, "Lost", scopes=[(SCOPE_SITE, estate["site-1"])])
        async with sm() as session:
            await OperationalAgentRepo(session).clear_scopes(lost, revoked_by="test")
            await session.commit()
        for agent_id, evidence in ((bare, False), (lost, True)):
            async with sm() as session:
                scope = await load_agent_scope(session, tenant_id=TENANT, agent_id=agent_id)
            assert scope.tenant_wide is False and scope.site_ids == frozenset()
            assert scope.synthesis == "agent"
            assert scope.previously_granted is evidence
            async with _machine_client(app, agent_id) as c:
                r = await c.get("/api/fleet/?page_size=200")
                assert r.status_code == 200, r.text
                d = r.json()
                assert len(d.get("devices", d.get("items", []))) == 0
                me = (await c.get("/api/scope-grants/me")).json()
                assert me["tenant_wide"] is False and me["synthesis"] == "agent"
        # A scoped agent still reaches exactly its rows: nothing lost.
        kept = await _agent(sm, "Kept", scopes=[(SCOPE_SITE, estate["site-1"])])
        async with sm() as session:
            scope = await load_agent_scope(session, tenant_id=TENANT, agent_id=kept)
        assert scope.site_ids == {estate["site-1"]} and scope.synthesis == "granted"

    @pytest.mark.asyncio
    async def test_the_impact_report_still_names_the_scopeless_agent(self):
        """Reporting stayed (A22.10); only the reach went."""
        app, sm, estate = await _seed(strict=False)
        bare = await _agent(sm, "Bare")
        async with _client(app, "tenant_owner", "kc-o") as c:
            report = (await c.get("/api/tenant-settings/scope-enforcement/impact")).json()
        assert bare in [a["agent_id"] for a in report["agents_without_grant"]]


# ---------------------------------------------------------------------------
# The adversarial regression: the historical escalation, reproduced
# ---------------------------------------------------------------------------


class TestAdversarialRegression:
    """1. narrow grant  2. revoke / expire / delete the target
    3. the active-grant query is empty  4. resolve under legacy_open
    -> NO TENANT-WIDE ACCESS. Fails the day the old rule returns."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("how", ["revoke", "expire", "vanish_unit"])
    async def test_a_lost_narrow_grant_never_becomes_tenant_wide(self, how):
        app, sm, estate = await _seed(strict=False)
        gid = await _grant(sm, "kc-x", SCOPE_ORG_UNIT, estate["a1"], role="viewer")
        if how == "revoke":
            await _revoke(sm, gid)
        elif how == "expire":
            async with sm() as session:
                g = await ScopeGrantRepo(session).get(TENANT, gid)
                g.expires_at = PAST
                await session.commit()
        else:
            await _vanish_unit(sm, estate, "a1")

        # Step 3: the authorization read carries nothing effective.
        async with sm() as session:
            active = await ScopeGrantRepo(session).list_for_principal(TENANT, "kc-x")
            assert not [g for g in active if scope_mod.is_active(g)] or how == "vanish_unit"

        # Step 4: legacy_open, the real loader, the real app.
        await _legacy(sm)
        me = await _me(app, "viewer", "kc-x")
        assert me["tenant_wide"] is False, (how, me)
        assert me["site_ids"] == [] and me["org_unit_paths"] == [], (how, me)
        assert me["synthesis"] != "never_granted", (how, me)
        assert me["previously_granted"] is True
        assert await _fleet_count(app, "viewer", "kc-x") == 0
        async with sm() as session:
            scope = await load_scope(
                session, tenant_id=TENANT, principal_ref="kc-x", role_permissions=VIEWER,
            )
        assert scope.permits("fleet.view", site_id=estate["site-3"]) is False
        assert scope.permits("fleet.view", site_id=estate["site-1"]) is False

    def test_the_old_rule_would_have_widened_and_the_new_one_does_not(self):
        """Executed, not described: an empty active list with evidence."""
        revoked = row(SCOPE_SITE, "site-a", revoked_at=PAST)
        # What the loader used to hand the resolver: nothing (the query
        # filtered revoked rows), so the resolver saw the never-granted
        # shape and synthesized.
        old_shape = _resolve([])
        assert old_shape.tenant_wide is True          # legacy behaviour, never-granted
        # What it hands it now: the row as evidence.
        new_shape = _resolve([], prior=[revoked])
        assert new_shape.tenant_wide is False
        assert new_shape.synthesis == "previously_granted"
        # And a row that reaches the resolver but fails the lifecycle
        # filter is evidence by itself.
        assert _resolve([revoked]).tenant_wide is False
