"""A23-3: authorization recovery + delegation integrity (spec A23.6, A23.8, A23.9).

The adversarial matrix the slice was scoped against, A through K. Every
API assertion is a request against the real ASGI app over the real
resolver and repositories; the resolver assertions execute `resolve()`
itself. Nothing here asserts that a UI hid anything.

The invariants under test:

    NO GRANT -> NO OPERATIONAL SCOPE
    A GRANT CAN NEVER DISAPPEAR INTO BROADER AUTHORITY

Two cases here were strict xfails until A23-4 landed: a REVOKED or
EXPIRED grant under `legacy_open` used to synthesize. A23.10's
never-granted-vs-previously-granted correction removed that, and the
marks with it; the cases now assert green and the full matrix lives in
`test_a23_4_synthesis.py`.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from harkeniq_cc import grant_integrity, scope as scope_mod
from harkeniq_cc.api import scope_grants as scope_grants_api
from harkeniq_cc.auth import ROLE_PERMISSIONS
from harkeniq_cc.db.models import (
    CCAuditLog,
    CCFleetCache,
    CCOrgUnit,
    CCScopeGrant,
    CCSite,
)
from harkeniq_cc.db.repos import (
    AuditRepo,
    OperationalAgentRepo,
    OrgUnitRepo,
    ScopeGrantRepo,
    TenantSettingsRepo,
)
from harkeniq_cc.grant_integrity import (
    GrantIntegrityError,
    admin_transition,
    guard_last_admin,
    role_permissions_for,
)
from harkeniq_cc.scope import (
    ENFORCEMENT_LEGACY_OPEN,
    ENFORCEMENT_STRICT,
    PRINCIPAL_USER,
    SCOPE_ORG_UNIT,
    SCOPE_SITE,
    SCOPE_TENANT,
    count_tenant_admins,
    preflight_strict,
    resolve,
)

from tests.unit.cc.test_e1_scope_api_and_chain import (
    TENANT,
    _client,
    _estate,
    _stack,
    _strict,
)

NOW = datetime.now(timezone.utc)
PAST = NOW - timedelta(days=1)
FUTURE = NOW + timedelta(days=30)
OWNER = ROLE_PERMISSIONS["tenant_owner"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed(strict: bool = True):
    app, sessionmaker = await _stack()
    estate = await _estate(sessionmaker)
    if strict:
        await _strict(sessionmaker)
    return app, sessionmaker, estate


async def _grant(sessionmaker, ref, scope_type, scope_ref="", role="tenant_owner",
                 subset=None, expires_at=None, principal_type=PRINCIPAL_USER,
                 realm=""):
    async with sessionmaker() as session:
        row = await ScopeGrantRepo(session).grant(
            tenant_id=TENANT, principal_type=principal_type, principal_ref=ref,
            scope_type=scope_type, scope_ref=scope_ref, role=role,
            permission_subset=subset, expires_at=expires_at, realm=realm,
            granted_by="seed",
        )
        await session.commit()
        return row.id


async def _row(sessionmaker, grant_id):
    async with sessionmaker() as session:
        return await ScopeGrantRepo(session).get(TENANT, grant_id)


async def _rows_for(sessionmaker, ref):
    async with sessionmaker() as session:
        return (await session.execute(
            select(CCScopeGrant).where(
                CCScopeGrant.tenant_id == TENANT, CCScopeGrant.principal_ref == ref,
            )
        )).scalars().all()


async def _legacy(sessionmaker):
    async with sessionmaker() as session:
        await TenantSettingsRepo(session).set_enforcement(
            TENANT, ENFORCEMENT_LEGACY_OPEN, "test"
        )
        await session.commit()


async def _audit(sessionmaker, action: str) -> list:
    async with sessionmaker() as session:
        return (await session.execute(
            select(CCAuditLog).where(
                CCAuditLog.tenant_id == TENANT, CCAuditLog.action == action,
            )
        )).scalars().all()


async def _chain_valid(sessionmaker) -> bool:
    async with sessionmaker() as session:
        return (await AuditRepo(session).verify_chain()).valid


async def _vanish_unit(sessionmaker, estate, key: str):
    """Delete an org unit OUT OF BAND -- the shape the API now refuses,
    and the shape a database operator or an older release can still
    produce. Sites hanging from it are moved to the region first."""
    async with sessionmaker() as session:
        unit = await OrgUnitRepo(session).get(TENANT, estate[key])
        for site in await OrgUnitRepo(session).sites_in(TENANT, unit.id):
            site.org_unit_id = unit.parent_id
        await session.flush()
        await session.execute(delete(CCOrgUnit).where(CCOrgUnit.id == unit.id))
        await session.commit()


async def _vanish_site(sessionmaker, site_id: str):
    async with sessionmaker() as session:
        await session.execute(
            delete(CCFleetCache).where(CCFleetCache.site_id == site_id)
        )
        await session.execute(delete(CCSite).where(CCSite.id == site_id))
        await session.commit()


async def _leaf(sessionmaker, estate, name="Leaf") -> str:
    """An empty unit under Region A: deletable but for a grant."""
    async with sessionmaker() as session:
        repo = OrgUnitRepo(session)
        parent = await repo.get(TENANT, estate["rega"])
        unit = await repo.create(TENANT, name=name, unit_type="hall", parent=parent)
        await session.commit()
        return unit.id


def _grant_body(ref, scope_type, scope_ref="", role="operator", **extra):
    return {"principal_ref": ref, "scope_type": scope_type,
            "scope_ref": scope_ref, "role": role, **extra}


# ---------------------------------------------------------------------------
# A. The last administrator
# ---------------------------------------------------------------------------


class TestLastAdmin:
    @pytest.mark.asyncio
    async def test_revoking_the_only_admin_is_refused_and_audited(self):
        app, sm, estate = await _seed()
        gid = await _grant(sm, "kc-owner", SCOPE_TENANT)
        async with _client(app, "tenant_owner", "kc-owner") as c:
            resp = await c.delete(f"/api/scope-grants/{gid}")
        assert resp.status_code == 409, resp.text
        assert "last active tenant-scope grant" in resp.json()["detail"]
        assert "Nothing has been changed" in resp.json()["detail"]
        assert (await _row(sm, gid)).revoked_at is None
        refused = await _audit(sm, "scope.revoke_refused")
        assert len(refused) == 1 and refused[0].detail["reason"] == "last_admin"
        assert refused[0].actor_ref == "kc-owner"
        assert await _chain_valid(sm)

    @pytest.mark.asyncio
    async def test_removing_role_manage_by_overwrite_is_refused(self):
        """The revive path REPLACES the stored row's role and subset, so
        posting the same grant with a smaller role is a mutation of the
        last administrator and is judged as one.

        Under strict, the only principal able to touch the last admin's
        grant is that admin (self-grant, refused). The honest attacker is
        a `legacy_open` principal with NO grant: synthesized tenant-wide
        reach, full role permissions, and -- because a synthesized grant
        never counts -- not an administrator."""
        app, sm, estate = await _seed(strict=False)
        gid = await _grant(sm, "kc-owner", SCOPE_TENANT)
        async with _client(app, "tenant_owner", "kc-mutator") as c:
            assert (await c.get("/api/scope-grants/me")).json()["tenant_wide"]
            lesser = await c.post("/api/scope-grants/", json=_grant_body(
                "kc-owner", SCOPE_TENANT, role="site_admin"))
            assert lesser.status_code == 409, lesser.text
            assert "last active tenant-scope grant" in lesser.json()["detail"]
            narrowed = await c.post("/api/scope-grants/", json=_grant_body(
                "kc-owner", SCOPE_TENANT, role="tenant_owner",
                permission_subset=["fleet.view"]))
            assert narrowed.status_code == 409, narrowed.text
            revoked = await c.delete(f"/api/scope-grants/{gid}")
            assert revoked.status_code == 409, revoked.text
            # A mutation that does not touch the administrator is fine.
            other = await c.post("/api/scope-grants/", json=_grant_body(
                "kc-x", SCOPE_SITE, estate["site-1"], role="operator"))
            assert other.status_code == 201, other.text
        row = await _row(sm, gid)
        assert row.role == "tenant_owner" and row.permission_subset is None
        assert row.revoked_at is None
        refused = await _audit(sm, "scope.grant_refused")
        assert len(refused) == 2 and {r.detail["reason"] for r in refused} == {"last_admin"}
        assert len(await _audit(sm, "scope.revoke_refused")) == 1

    @pytest.mark.asyncio
    async def test_with_two_admins_the_same_mutations_are_allowed(self):
        app, sm, estate = await _seed(strict=False)
        gid = await _grant(sm, "kc-owner", SCOPE_TENANT)
        await _grant(sm, "kc-second", SCOPE_TENANT)
        async with _client(app, "tenant_owner", "kc-mutator") as c:
            lesser = await c.post("/api/scope-grants/", json=_grant_body(
                "kc-owner", SCOPE_TENANT, role="site_admin"))
            assert lesser.status_code == 201, lesser.text
        assert (await _row(sm, gid)).role == "site_admin"
        # kc-second is now the last: refused.
        async with _client(app, "tenant_owner", "kc-mutator") as c:
            grants = (await c.get("/api/scope-grants/")).json()["grants"]
            second = [g for g in grants if g["principal_ref"] == "kc-second"][0]["id"]
            assert (await c.delete(f"/api/scope-grants/{second}")).status_code == 409

    @pytest.mark.asyncio
    async def test_the_count_refuses_a_lesser_role_on_the_last_admin(self):
        """The rule itself, on the transition object: one admin, replaced
        by a shape without role.manage -> refused. Independent of who is
        calling, which is the point of putting it in the domain."""
        rows = [scope_grants_fixture("g1", "kc-a", SCOPE_TENANT, "tenant_owner")]
        t = admin_transition(
            rows, exclude_ids=("g1",),
            replacement=grant_integrity.grant_shape(
                principal_type="user", principal_ref="kc-a",
                scope_type=SCOPE_TENANT, role="site_admin"),
        )
        assert t.before == 1 and t.after == 0 and t.removes_last
        t2 = admin_transition(
            rows, exclude_ids=("g1",),
            replacement=grant_integrity.grant_shape(
                principal_type="user", principal_ref="kc-a",
                scope_type=SCOPE_TENANT, role="tenant_owner",
                permission_subset=["fleet.view"]),
        )
        assert t2.removes_last

    @pytest.mark.asyncio
    async def test_any_expiry_on_the_only_admin_is_refused(self):
        """A23.8 says 'setting an expiry on'. A FUTURE expiry is still a
        scheduled lockout, so it is refused too, not just a past one."""
        app, sm, estate = await _seed(strict=False)
        gid = await _grant(sm, "kc-owner", SCOPE_TENANT)
        async with _client(app, "tenant_owner", "kc-mutator") as c:
            for when in (FUTURE, PAST):
                r = await c.post("/api/scope-grants/", json=_grant_body(
                    "kc-owner", SCOPE_TENANT, role="tenant_owner",
                    expires_at=when.isoformat()))
                assert r.status_code == 409, (when, r.text)
        assert (await _row(sm, gid)).expires_at is None
        refused = await _audit(sm, "scope.grant_refused")
        assert {r.detail["reason"] for r in refused} == {"last_admin"}
        assert all(r.detail["permanent_admins_after"] == 0 for r in refused)

    @pytest.mark.asyncio
    async def test_two_admins_may_expire_one_but_never_both(self):
        app, sm, estate = await _seed(strict=False)
        await _grant(sm, "kc-owner", SCOPE_TENANT)
        await _grant(sm, "kc-second", SCOPE_TENANT)
        async with _client(app, "tenant_owner", "kc-mutator") as c:
            one = await c.post("/api/scope-grants/", json=_grant_body(
                "kc-owner", SCOPE_TENANT, role="tenant_owner",
                expires_at=FUTURE.isoformat()))
            assert one.status_code == 201, one.text
            # kc-owner still counts as ACTIVE, so "before" is 2 -- but
            # expiring kc-second would leave no PERMANENT administrator.
            both = await c.post("/api/scope-grants/", json=_grant_body(
                "kc-second", SCOPE_TENANT, role="tenant_owner",
                expires_at=FUTURE.isoformat()))
            assert both.status_code == 409, both.text
            assert "last active tenant-scope grant" in both.json()["detail"]

    @pytest.mark.asyncio
    async def test_reassigning_the_only_admin_off_tenant_scope_is_refused(self):
        app, sm, estate = await _seed(strict=False)
        gid = await _grant(sm, "kc-owner", SCOPE_TENANT)
        async with _client(app, "tenant_owner", "kc-mutator") as c:
            r = await c.post(f"/api/scope-grants/{gid}/reassign", json={
                "scope_type": SCOPE_ORG_UNIT, "scope_ref": estate["rega"]})
            assert r.status_code == 409, r.text
            # With a second admin the move is allowed, and the moved
            # principal is no longer an administrator afterwards.
            await _grant(sm, "kc-second", SCOPE_TENANT)
            r = await c.post(f"/api/scope-grants/{gid}/reassign", json={
                "scope_type": SCOPE_ORG_UNIT, "scope_ref": estate["rega"]})
            assert r.status_code == 200, r.text
            grants = (await c.get("/api/scope-grants/")).json()["grants"]
            second = [g for g in grants if g["principal_ref"] == "kc-second"][0]["id"]
            assert (await c.delete(f"/api/scope-grants/{second}")).status_code == 409
        assert (await _row(sm, gid)).revoked_at is not None

    @pytest.mark.asyncio
    async def test_the_strict_flip_without_an_admin_is_refused_and_audited(self):
        app, sm, estate = await _seed(strict=False)
        async with _client(app, "tenant_owner", "kc-owner") as c:
            r = await c.put("/api/tenant-settings/scope-enforcement",
                            json={"mode": "strict"})
        assert r.status_code == 409
        assert "Nothing has been changed" in r.json()["detail"]
        assert len(await _audit(sm, "scope.enforcement_refused")) == 1
        async with sm() as session:
            assert await TenantSettingsRepo(session).enforcement(TENANT) == \
                ENFORCEMENT_LEGACY_OPEN

    @pytest.mark.asyncio
    async def test_a_grantless_legacy_tenant_is_not_blocked_from_its_first_admin(self):
        """Zero admins is not 'the last admin'. The first tenant grant
        must be creatable, or nobody could ever administer."""
        app, sm, estate = await _seed(strict=False)
        async with _client(app, "tenant_owner", "kc-owner") as c:
            r = await c.post("/api/scope-grants/", json=_grant_body(
                "kc-first", SCOPE_TENANT, role="tenant_owner"))
        assert r.status_code == 201, r.text


def scope_grants_fixture(gid, ref, scope_type, role, **kw):
    from types import SimpleNamespace
    return SimpleNamespace(
        id=gid, principal_type="user", principal_ref=ref, scope_type=scope_type,
        scope_ref=kw.get("scope_ref", ""), role=role,
        permission_subset=kw.get("subset"), expires_at=kw.get("expires_at"),
        revoked_at=kw.get("revoked_at"), realm=kw.get("realm", ""),
    )


class TestOneCountingFunction:
    def test_preflight_strict_asks_the_counting_function(self):
        assert "count_tenant_admins(" in inspect.getsource(preflight_strict)

    def test_the_router_keeps_no_count_of_its_own(self):
        src = inspect.getsource(scope_grants_api)
        assert "ROLE_PERMISSIONS" not in src
        assert "ADMIN_PERMISSION in" not in src
        assert "guard_last_admin(" in src

    def test_a_stale_realm_admin_never_counts(self):
        rows = [
            scope_grants_fixture("g1", "kc-old", SCOPE_TENANT, "tenant_owner",
                                 realm="tenant-old"),
            scope_grants_fixture("g2", "kc-new", SCOPE_TENANT, "tenant_owner",
                                 realm="tenant-demo"),
            scope_grants_fixture("g3", "kc-pre", SCOPE_TENANT, "tenant_owner",
                                 realm=""),
        ]
        assert count_tenant_admins(rows, role_permissions_for, realm="tenant-demo") == 2
        assert count_tenant_admins(rows, role_permissions_for, realm="") == 3

    def test_an_agent_row_never_counts(self):
        row = scope_grants_fixture("g", "agent-1", SCOPE_TENANT, "tenant_owner")
        row.principal_type = "agent"
        assert count_tenant_admins([row], role_permissions_for) == 0

    def test_a_row_without_a_role_counts_for_nothing(self):
        row = scope_grants_fixture("g", "kc-a", SCOPE_TENANT, "")
        assert count_tenant_admins([row], role_permissions_for) == 0

    def test_a_synthesized_grant_never_counts(self):
        caller = resolve(
            tenant_id=TENANT, principal_type="user", principal_ref="kc-a",
            role_permissions=OWNER, grant_rows=[], enforcement=ENFORCEMENT_LEGACY_OPEN,
        )
        assert caller.tenant_wide
        assert count_tenant_admins([], role_permissions_for, caller_scope=caller) == 0

    def test_the_callers_contribution_is_judged_on_the_locked_row_not_the_stale_scope(self):
        """Two admins, A and B. B narrows A's subset (drops role.manage)
        and commits. A's request, resolved BEFORE that commit, still
        believes A holds role.manage. Under the lock A's own row is
        re-read: the narrowed subset wins, A no longer counts, and
        revoking B is refused."""
        stale_row = scope_grants_fixture("ga", "kc-a", SCOPE_TENANT, "tenant_owner")
        a_scope = resolve(
            tenant_id=TENANT, principal_type="user", principal_ref="kc-a",
            role_permissions=OWNER, grant_rows=[stale_row], enforcement=ENFORCEMENT_STRICT,
        )
        assert a_scope.permits("role.manage", tenant_object=True)
        locked_rows = [
            scope_grants_fixture("ga", "kc-a", SCOPE_TENANT, "tenant_owner",
                                 subset=["fleet.view"]),
            scope_grants_fixture("gb", "kc-b", SCOPE_TENANT, "tenant_owner"),
        ]
        t = admin_transition(
            locked_rows, caller_scope=a_scope, caller_role_permissions=OWNER,
            exclude_ids=("gb",),
        )
        assert t.before == 1 and t.after == 0 and t.removes_last
        # Without the token, the legacy scope-based reading would have
        # counted A -- which is exactly the window the token closes.
        naive = admin_transition(locked_rows, caller_scope=a_scope, exclude_ids=("gb",))
        assert naive.after == 1

    def test_the_callers_own_row_is_not_counted_when_it_is_the_one_removed(self):
        row = scope_grants_fixture("g1", "kc-a", SCOPE_TENANT, "")
        caller = resolve(
            tenant_id=TENANT, principal_type="user", principal_ref="kc-a",
            role_permissions=OWNER, grant_rows=[row], enforcement=ENFORCEMENT_STRICT,
        )
        assert count_tenant_admins([row], role_permissions_for, caller_scope=caller) == 1
        assert count_tenant_admins(
            [row], role_permissions_for, caller_scope=caller, exclude_ids=("g1",)
        ) == 0


class TestLastAdminIsSerialized:
    """Two admins revoking each other at once must not both pass."""

    def test_the_lock_is_taken_before_the_rows_are_read(self):
        src = inspect.getsource(guard_last_admin)
        assert src.index("lock_tenant_authorization(") < src.index("list_all(")

    @pytest.mark.asyncio
    async def test_on_postgresql_a_transaction_lock_keyed_on_the_tenant_is_taken(self):
        from types import SimpleNamespace

        from harkeniq.audit.chain import advisory_lock_key

        statements: list = []

        class _Result:
            def scalars(self):
                return self

            def all(self):
                return []

        class _Session:
            bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

            async def execute(self, stmt, params=None):
                statements.append((str(stmt), params))
                return _Result()

        await guard_last_admin(
            _Session(), tenant_id="t-1", realm="", caller_scope=None,
            mutation="test", audit="scope.revoke_refused",
        )
        lock = [p for s, p in statements if "pg_advisory_xact_lock" in s]
        assert lock and lock[0]["key"] == advisory_lock_key("cc.scope_admins.t-1")
        # Transaction-scoped: nothing here releases it; the commit does.
        assert not any("unlock" in s for s, _ in statements)


# ---------------------------------------------------------------------------
# B. Multi-admin
# ---------------------------------------------------------------------------


class TestMultiAdmin:
    @pytest.mark.asyncio
    async def test_one_of_two_admins_may_be_revoked_and_the_other_stands(self):
        app, sm, estate = await _seed()
        await _grant(sm, "kc-owner", SCOPE_TENANT)
        g2 = await _grant(sm, "kc-second", SCOPE_TENANT)
        async with _client(app, "tenant_owner", "kc-owner") as c:
            assert (await c.delete(f"/api/scope-grants/{g2}")).status_code == 200
            me = (await c.get("/api/scope-grants/me")).json()
            assert me["tenant_wide"] is True
            # ...and kc-owner is now the last: their own grant is safe.
            grants = (await c.get("/api/scope-grants/")).json()["grants"]
            own = [g for g in grants if g["principal_ref"] == "kc-owner"][0]["id"]
            assert (await c.delete(f"/api/scope-grants/{own}")).status_code == 409
        assert (await _row(sm, g2)).revoked_at is not None
        async with _client(app, "tenant_owner", "kc-second") as c:
            assert (await c.get("/api/scope-grants/me")).json()["tenant_wide"] is False


# ---------------------------------------------------------------------------
# C. Self-grant
# ---------------------------------------------------------------------------


class TestSelfGrant:
    @pytest.mark.asyncio
    async def test_a_tenant_wide_owner_cannot_grant_themselves_anything(self):
        app, sm, estate = await _seed()
        await _grant(sm, "kc-owner", SCOPE_TENANT)
        async with _client(app, "tenant_owner", "kc-owner") as c:
            for body in (
                _grant_body("kc-owner", SCOPE_SITE, estate["site-1"], role="viewer"),
                _grant_body("kc-owner", SCOPE_TENANT, role="tenant_owner"),
                _grant_body("kc-owner", SCOPE_ORG_UNIT, estate["a1"], role="operator"),
                _grant_body("kc-owner@example.com", SCOPE_SITE, estate["site-1"],
                            role="viewer"),
                _grant_body("KC-OWNER@EXAMPLE.COM", SCOPE_TENANT, role="viewer"),
            ):
                r = await c.post("/api/scope-grants/", json=body)
                assert r.status_code == 403, (body, r.text)
                assert "self-grant is forbidden" in r.json()["detail"]
        assert len(await _rows_for(sm, "kc-owner")) == 1  # nothing was created
        assert not await _rows_for(sm, "kc-owner@example.com")
        refused = await _audit(sm, "scope.grant_refused")
        assert len(refused) == 5
        assert {r.detail["reason"] for r in refused} == {"self_grant"}
        assert await _chain_valid(sm)

    @pytest.mark.asyncio
    async def test_a_narrowed_grantor_is_refused_by_identity_not_by_scope(self):
        """Within their own scope, with a lesser role: the refusal must
        still be the self-grant rule, not a scope check that happened
        to say no."""
        app, sm, estate = await _seed()
        await _grant(sm, "kc-cm", SCOPE_ORG_UNIT, estate["a1"])
        async with _client(app, "tenant_owner", "kc-cm") as c:
            r = await c.post("/api/scope-grants/", json=_grant_body(
                "kc-cm", SCOPE_SITE, estate["site-1"], role="viewer"))
        assert r.status_code == 403
        assert "self-grant is forbidden" in r.json()["detail"]
        assert "outside your authorized scope" not in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_reassigning_ones_own_grant_is_a_self_grant(self):
        app, sm, estate = await _seed()
        await _grant(sm, "kc-owner", SCOPE_TENANT)
        own = await _grant(sm, "kc-owner", SCOPE_ORG_UNIT, estate["a1"], role="operator")
        async with _client(app, "tenant_owner", "kc-owner") as c:
            r = await c.post(f"/api/scope-grants/{own}/reassign", json={
                "scope_type": SCOPE_ORG_UNIT, "scope_ref": estate["rega"]})
        assert r.status_code == 403 and "self-grant" in r.json()["detail"]
        assert (await _row(sm, own)).scope_ref == estate["a1"]

    def test_the_rule_does_not_depend_on_a_scope(self):
        from types import SimpleNamespace

        user = SimpleNamespace(user_id="kc-me", email="Me@Example.com")
        with pytest.raises(GrantIntegrityError) as exc:
            grant_integrity.refuse_self_grant(user, "user", "kc-me")
        assert exc.value.code == "self_grant" and exc.value.status == 403
        with pytest.raises(GrantIntegrityError):
            grant_integrity.refuse_self_grant(user, "user", "me@example.com")
        grant_integrity.refuse_self_grant(user, "user", "kc-other")
        grant_integrity.refuse_self_grant(user, "agent", "kc-me")


# ---------------------------------------------------------------------------
# D, F, G. Delegation: permission authority
# ---------------------------------------------------------------------------


class TestDelegationAuthority:
    HELD = ["role.manage", "fleet.view", "incident.view", "site.manage", "site.view"]

    @pytest.mark.asyncio
    async def test_a_grantor_delegates_what_they_hold_and_nothing_more(self):
        app, sm, estate = await _seed()
        await _grant(sm, "kc-cm", SCOPE_ORG_UNIT, estate["a1"], subset=self.HELD)
        async with _client(app, "tenant_owner", "kc-cm") as c:
            # viewer = fleet.view + incident.view: both held. Allowed.
            ok = await c.post("/api/scope-grants/", json=_grant_body(
                "kc-x", SCOPE_SITE, estate["site-1"], role="viewer"))
            assert ok.status_code == 201, ok.text
            # operator carries action.approve: not held. Refused.
            no = await c.post("/api/scope-grants/", json=_grant_body(
                "kc-x", SCOPE_SITE, estate["site-1"], role="operator"))
            assert no.status_code == 403, no.text
            assert "action.approve" in no.json()["detail"]
            assert "naming a broader role does not restore" in no.json()["detail"]
            # A subset of site_admin restricted to held permissions: allowed.
            sub_ok = await c.post("/api/scope-grants/", json=_grant_body(
                "kc-y", SCOPE_SITE, estate["site-1"], role="site_admin",
                permission_subset=["fleet.view", "site.manage"]))
            assert sub_ok.status_code == 201, sub_ok.text
            # The same role with a subset naming what is not held: refused.
            sub_no = await c.post("/api/scope-grants/", json=_grant_body(
                "kc-z", SCOPE_SITE, estate["site-1"], role="site_admin",
                permission_subset=["action.approve"]))
            assert sub_no.status_code == 403, sub_no.text
        refused = await _audit(sm, "scope.grant_refused")
        assert {r.detail["reason"] for r in refused} == {"exceeds_grantor"}
        assert "action.approve" in refused[0].detail["missing"]
        assert "fleet.view" not in refused[0].detail["missing"]
        assert not await _rows_for(sm, "kc-z")

    @pytest.mark.asyncio
    async def test_a_narrowed_tenant_owner_cannot_regain_a_withheld_permission(self):
        """F: the role nominally has action.approve; the grant withholds
        it; delegating tenant_owner or operator (both carry it) is
        refused; auditor (which does not) is allowed."""
        app, sm, estate = await _seed()
        withheld = [p for p in OWNER if p != "action.approve"]
        await _grant(sm, "kc-cm", SCOPE_TENANT, subset=withheld)
        async with _client(app, "tenant_owner", "kc-cm") as c:
            for role in ("tenant_owner", "operator", "site_admin"):
                r = await c.post("/api/scope-grants/", json=_grant_body(
                    "kc-x", SCOPE_TENANT, role=role))
                assert r.status_code == 403, (role, r.text)
                assert "action.approve" in r.json()["detail"]
            r = await c.post("/api/scope-grants/", json=_grant_body(
                "kc-x", SCOPE_TENANT, role="auditor"))
            assert r.status_code == 201, r.text
            # And an explicit subset of tenant_owner WITHOUT action.approve
            # is exactly what they hold: allowed.
            r = await c.post("/api/scope-grants/", json=_grant_body(
                "kc-y", SCOPE_SITE, estate["site-1"], role="tenant_owner",
                permission_subset=withheld))
            assert r.status_code == 201, r.text

    @pytest.mark.asyncio
    async def test_role_escalation_through_a_valid_scope_is_refused(self):
        """G: the target scope is inside the grantor's; the target ROLE
        carries what the grantor does not."""
        app, sm, estate = await _seed()
        await _grant(sm, "kc-cm", SCOPE_ORG_UNIT, estate["a1"],
                     subset=["role.manage", "fleet.view"])
        async with _client(app, "tenant_owner", "kc-cm") as c:
            r = await c.post("/api/scope-grants/", json=_grant_body(
                "kc-x", SCOPE_SITE, estate["site-1"], role="site_admin"))
        assert r.status_code == 403
        assert "site.manage" in r.json()["detail"]
        assert not await _rows_for(sm, "kc-x")

    @pytest.mark.asyncio
    async def test_the_recorded_role_bounds_the_recipient(self):
        """The other half of delegation: what was delegated is what the
        recipient gets, whatever their own token says. A `viewer` grant
        to a principal whose token is tenant_owner resolves to viewer."""
        app, sm, estate = await _seed()
        await _grant(sm, "kc-owner", SCOPE_TENANT)
        async with _client(app, "tenant_owner", "kc-owner") as c:
            r = await c.post("/api/scope-grants/", json=_grant_body(
                "kc-x", SCOPE_TENANT, role="viewer"))
            assert r.status_code == 201, r.text
        async with _client(app, "tenant_owner", "kc-x") as c:
            me = (await c.get("/api/scope-grants/me")).json()
            assert me["grants"][0]["permissions"] == sorted(ROLE_PERMISSIONS["viewer"])
            # The route guard passes (the token says tenant_owner); the
            # object gate refuses (the grant says viewer).
            r = await c.post("/api/scope-grants/", json=_grant_body(
                "kc-y", SCOPE_SITE, estate["site-1"], role="viewer"))
            assert r.status_code == 403
            assert "role.manage" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_human_grant_must_name_a_tenant_role(self):
        app, sm, estate = await _seed()
        await _grant(sm, "kc-owner", SCOPE_TENANT)
        async with _client(app, "tenant_owner", "kc-owner") as c:
            r = await c.post("/api/scope-grants/", json=_grant_body(
                "kc-x", SCOPE_SITE, estate["site-1"], role=""))
            assert r.status_code == 400 and "must name the tenant role" in r.text
            r = await c.post("/api/scope-grants/", json=_grant_body(
                "kc-x", SCOPE_SITE, estate["site-1"], role="platform_super_admin"))
            assert r.status_code == 400
        assert not await _rows_for(sm, "kc-x")

    def test_the_resolver_applies_the_recorded_role_as_a_ceiling(self):
        row = scope_grants_fixture("g", "kc-a", SCOPE_TENANT, "viewer")
        scope = resolve(
            tenant_id=TENANT, principal_type="user", principal_ref="kc-a",
            role_permissions=OWNER, grant_rows=[row], enforcement=ENFORCEMENT_STRICT,
            role_ceiling_for=grant_integrity.role_ceiling_for,
        )
        assert scope.grants[0].permissions == frozenset(ROLE_PERMISSIONS["viewer"])
        assert not scope.permits("role.manage", tenant_object=True)
        # No role recorded: exactly as before.
        bare = scope_grants_fixture("g", "kc-a", SCOPE_TENANT, "")
        scope = resolve(
            tenant_id=TENANT, principal_type="user", principal_ref="kc-a",
            role_permissions=OWNER, grant_rows=[bare], enforcement=ENFORCEMENT_STRICT,
            role_ceiling_for=grant_integrity.role_ceiling_for,
        )
        assert scope.grants[0].permissions == frozenset(OWNER)
        # A ceiling never widens: viewer recorded, viewer token -> viewer.
        scope = resolve(
            tenant_id=TENANT, principal_type="user", principal_ref="kc-a",
            role_permissions=ROLE_PERMISSIONS["viewer"], grant_rows=[
                scope_grants_fixture("g", "kc-a", SCOPE_TENANT, "tenant_owner")],
            enforcement=ENFORCEMENT_STRICT,
            role_ceiling_for=grant_integrity.role_ceiling_for,
        )
        assert scope.grants[0].permissions == frozenset(ROLE_PERMISSIONS["viewer"])


# ---------------------------------------------------------------------------
# E. Delegation: reach
# ---------------------------------------------------------------------------


class TestDelegationReach:
    @pytest.mark.asyncio
    async def test_a_site_scoped_grantor_delegates_within_the_site_only(self):
        app, sm, estate = await _seed()
        await _grant(sm, "kc-sa", SCOPE_SITE, estate["site-1"])
        async with _client(app, "tenant_owner", "kc-sa") as c:
            ok = await c.post("/api/scope-grants/", json=_grant_body(
                "kc-x", SCOPE_SITE, estate["site-1"], role="operator"))
            assert ok.status_code == 201, ok.text
            for body in (
                _grant_body("kc-x", SCOPE_SITE, estate["site-3"], role="operator"),
                _grant_body("kc-x", SCOPE_TENANT, role="viewer"),
                _grant_body("kc-x", SCOPE_ORG_UNIT, estate["a1"], role="viewer"),
                _grant_body("kc-x", "device_class", "server", role="viewer"),
            ):
                r = await c.post("/api/scope-grants/", json=body)
                assert r.status_code == 403, (body, r.text)
                assert "outside your authorized scope" in r.json()["detail"]
        rows = await _rows_for(sm, "kc-x")
        assert [(r.scope_type, r.scope_ref) for r in rows] == [(SCOPE_SITE, estate["site-1"])]


# ---------------------------------------------------------------------------
# H, J. Vanished targets and the lifecycle
# ---------------------------------------------------------------------------


class TestVanishedTarget:
    @pytest.mark.asyncio
    async def test_a_deleted_unit_leaves_an_inert_grant_and_no_reach(self):
        app, sm, estate = await _seed()
        await _grant(sm, "kc-owner", SCOPE_TENANT)
        gid = await _grant(sm, "kc-x", SCOPE_ORG_UNIT, estate["a1"], role="site_admin")
        await _vanish_unit(sm, estate, "a1")
        for posture in (ENFORCEMENT_STRICT, ENFORCEMENT_LEGACY_OPEN):
            if posture == ENFORCEMENT_LEGACY_OPEN:
                await _legacy(sm)
            async with _client(app, "site_admin", "kc-x") as c:
                me = (await c.get("/api/scope-grants/me")).json()
                assert me["tenant_wide"] is False, posture
                assert me["site_ids"] == [] and me["org_unit_paths"] == []
                assert me["administered"] is True
                assert me["inert_grants"] == [{
                    "scope_type": SCOPE_ORG_UNIT, "scope_ref": estate["a1"],
                    "reason": "org_unit_missing"}]
                fleet = (await c.get("/api/fleet/")).json()
                assert fleet.get("devices", fleet.get("items", [])) == [], posture
        async with _client(app, "tenant_owner", "kc-owner") as c:
            listed = (await c.get("/api/scope-grants/")).json()["grants"]
            row = [g for g in listed if g["id"] == gid][0]
            assert row["target_status"] == "missing" and row["effective"] is False
        assert (await _row(sm, gid)).revoked_at is None  # retained, not removed

    @pytest.mark.asyncio
    async def test_a_missing_site_is_zero_reach_with_a_reason(self):
        app, sm, estate = await _seed()
        gid = await _grant(sm, "kc-x", SCOPE_SITE, estate["site-1"], role="operator")
        await _vanish_site(sm, estate["site-1"])
        await _legacy(sm)
        async with _client(app, "operator", "kc-x") as c:
            me = (await c.get("/api/scope-grants/me")).json()
            assert me["tenant_wide"] is False and me["site_ids"] == []
            assert me["inert_grants"][0]["reason"] == "site_missing"
            fleet = (await c.get("/api/fleet/")).json()
            assert fleet.get("devices", fleet.get("items", [])) == []
        assert (await _row(sm, gid)).revoked_at is None

    def test_covers_site_is_false_for_an_id_outside_the_current_site_set(self):
        row = scope_grants_fixture("g", "kc-a", SCOPE_SITE, "operator", scope_ref="site-gone")
        from types import SimpleNamespace
        sites = [SimpleNamespace(id="site-here", org_unit_id=None)]
        scope = resolve(
            tenant_id=TENANT, principal_type="user", principal_ref="kc-a",
            role_permissions=OWNER, grant_rows=[row], sites=sites,
            enforcement=ENFORCEMENT_LEGACY_OPEN,
        )
        assert not scope.covers_site("site-gone")
        assert not scope.permits("fleet.view", site_id="site-gone")
        assert not scope.tenant_wide and scope.site_ids == frozenset()
        assert scope.is_empty() and scope.administered
        assert scope.grants[0].inert and scope.grants[0].inert_reason == "site_missing"

    def test_an_inert_grant_cannot_be_delegated_and_delegates_nothing(self):
        gone = scope_grants_fixture("g", "kc-a", SCOPE_ORG_UNIT, "tenant_owner",
                                    scope_ref="unit-gone")
        creator = resolve(
            tenant_id=TENANT, principal_type="user", principal_ref="kc-a",
            role_permissions=OWNER, grant_rows=[gone], enforcement=ENFORCEMENT_LEGACY_OPEN,
        )
        assert creator.grants[0].inert and not creator.tenant_wide
        owner = resolve(
            tenant_id=TENANT, principal_type="user", principal_ref="kc-o",
            role_permissions=OWNER,
            grant_rows=[scope_grants_fixture("g", "kc-o", SCOPE_TENANT, "tenant_owner")],
            enforcement=ENFORCEMENT_STRICT,
        )
        assert not owner.can_delegate(creator)     # nothing there to hand over
        assert not creator.can_delegate(owner)     # inert delegates nothing
        assert not creator.may_ever("fleet.view")

    def test_no_decision_method_reads_an_inert_grant(self):
        """Every branch of `permits` and every coverage helper skips an
        inert grant, structurally: the frozen dataclass answers False
        from each `covers_*` and `permits` skips it before any check."""
        from harkeniq_cc.scope import Grant
        g = Grant(scope_type=SCOPE_TENANT, scope_ref="", permissions=frozenset(OWNER),
                  inert=True, inert_reason="org_unit_missing")
        assert not g.covers_tenant() and not g.covers_site("s", "/p/")
        assert not g.covers_org_unit("/p/") and not g.covers_device("d", "s", "/p/", "server")
        for name in ("covers_site", "covers_org_unit", "covers_device", "covers_tenant"):
            assert "self.inert" in inspect.getsource(getattr(Grant, name))
        assert "grant.inert" in inspect.getsource(scope_mod.ResolvedScope.permits)


class TestLifecycleFailsClosed:
    @pytest.mark.parametrize("shape", ["revoked", "expired", "orphaned", "bad_type", "bad_site"])
    @pytest.mark.asyncio
    async def test_under_strict_every_ineffective_grant_reaches_nothing(self, shape):
        app, sm, estate = await _seed()
        kwargs = dict(role="site_admin")
        if shape == "revoked":
            gid = await _grant(sm, "kc-x", SCOPE_ORG_UNIT, estate["a1"], **kwargs)
            async with sm() as session:
                await ScopeGrantRepo(session).revoke(
                    await ScopeGrantRepo(session).get(TENANT, gid), "test")
                await session.commit()
        elif shape == "expired":
            await _grant(sm, "kc-x", SCOPE_ORG_UNIT, estate["a1"], expires_at=PAST, **kwargs)
        elif shape == "orphaned":
            await _grant(sm, "kc-x", SCOPE_ORG_UNIT, estate["a1"], **kwargs)
            await _vanish_unit(sm, estate, "a1")
        elif shape == "bad_type":
            await _grant(sm, "kc-x", "galaxy", "andromeda", **kwargs)
        else:
            await _grant(sm, "kc-x", SCOPE_SITE, "f" * 32, **kwargs)
        async with _client(app, "site_admin", "kc-x") as c:
            me = (await c.get("/api/scope-grants/me")).json()
            assert me["tenant_wide"] is False and me["site_ids"] == [], shape
            fleet = (await c.get("/api/fleet/")).json()
            assert fleet.get("devices", fleet.get("items", [])) == [], shape

    @pytest.mark.parametrize("shape", ["orphaned", "bad_site", "bad_type_with_orphan"])
    @pytest.mark.asyncio
    async def test_under_legacy_open_a_vanished_target_never_synthesizes(self, shape):
        app, sm, estate = await _seed(strict=False)
        if shape == "orphaned":
            await _grant(sm, "kc-x", SCOPE_ORG_UNIT, estate["a1"], role="site_admin")
            await _vanish_unit(sm, estate, "a1")
        elif shape == "bad_site":
            await _grant(sm, "kc-x", SCOPE_SITE, "f" * 32, role="site_admin")
        else:
            await _grant(sm, "kc-x", "galaxy", "andromeda", role="site_admin")
            await _grant(sm, "kc-x", SCOPE_ORG_UNIT, estate["a1"], role="site_admin")
            await _vanish_unit(sm, estate, "a1")
        async with _client(app, "site_admin", "kc-x") as c:
            me = (await c.get("/api/scope-grants/me")).json()
            assert me["tenant_wide"] is False and me["site_ids"] == [], shape
            assert me["inert_grants"], shape

    @pytest.mark.asyncio
    async def test_under_legacy_open_a_revoked_grant_does_not_synthesize(self):
        app, sm, estate = await _seed(strict=False)
        gid = await _grant(sm, "kc-x", SCOPE_ORG_UNIT, estate["a1"], role="site_admin")
        async with sm() as session:
            await ScopeGrantRepo(session).revoke(
                await ScopeGrantRepo(session).get(TENANT, gid), "test")
            await session.commit()
        async with _client(app, "site_admin", "kc-x") as c:
            me = (await c.get("/api/scope-grants/me")).json()
            assert me["tenant_wide"] is False

    @pytest.mark.asyncio
    async def test_under_legacy_open_an_expired_grant_to_a_legacy_target_does_not_synthesize(self):
        app, sm, estate = await _seed(strict=False)
        await _grant(sm, "kc-x", SCOPE_ORG_UNIT, estate["a1"], role="site_admin",
                     expires_at=PAST)
        await _vanish_unit(sm, estate, "a1")
        async with _client(app, "site_admin", "kc-x") as c:
            me = (await c.get("/api/scope-grants/me")).json()
            assert me["tenant_wide"] is False


# ---------------------------------------------------------------------------
# I. Org-unit deletion
# ---------------------------------------------------------------------------


class TestOrgUnitDeletion:
    @pytest.mark.asyncio
    async def test_delete_is_refused_under_a_grant_and_allowed_after_reassignment(self):
        app, sm, estate = await _seed()
        await _grant(sm, "kc-owner", SCOPE_TENANT)
        leaf = await _leaf(sm, estate)
        gid = await _grant(sm, "kc-x", SCOPE_ORG_UNIT, leaf, role="site_admin")
        async with _client(app, "tenant_owner", "kc-owner") as c:
            r = await c.delete(f"/api/org-units/{leaf}")
            assert r.status_code == 409, r.text
            assert "referenced by 1 active scope grant" in r.json()["detail"]
            refused = await _audit(sm, "org_unit.delete_refused")
            assert refused[0].detail["grant_ids"] == [gid]
            assert refused[0].detail["principals"] == [
                {"principal_type": "user", "principal_ref": "kc-x"}]
            # The safe path: reassign, then delete.
            moved = await c.post(f"/api/scope-grants/{gid}/reassign", json={
                "scope_type": SCOPE_ORG_UNIT, "scope_ref": estate["a1"]})
            assert moved.status_code == 200, moved.text
            assert moved.json()["revoked"]["id"] == gid
            assert (await c.delete(f"/api/org-units/{leaf}")).status_code == 200
        # kc-x now reaches A1 and only A1: no fallback, no widening.
        async with _client(app, "site_admin", "kc-x") as c:
            me = (await c.get("/api/scope-grants/me")).json()
            assert me["tenant_wide"] is False
            assert me["site_ids"] == [estate["site-1"]]
            assert me["inert_grants"] == []
        granted = [a for a in await _audit(sm, "scope.granted")
                   if a.detail.get("reassigned_from") == gid]
        assert len(granted) == 1
        assert await _chain_valid(sm)

    @pytest.mark.asyncio
    async def test_revocation_also_clears_the_way(self):
        app, sm, estate = await _seed()
        await _grant(sm, "kc-owner", SCOPE_TENANT)
        leaf = await _leaf(sm, estate)
        gid = await _grant(sm, "kc-x", SCOPE_ORG_UNIT, leaf, role="site_admin")
        async with _client(app, "tenant_owner", "kc-owner") as c:
            assert (await c.delete(f"/api/org-units/{leaf}")).status_code == 409
            assert (await c.delete(f"/api/scope-grants/{gid}")).status_code == 200
            assert (await c.delete(f"/api/org-units/{leaf}")).status_code == 200

    @pytest.mark.asyncio
    async def test_an_expired_grant_does_not_pin_the_unit(self):
        app, sm, estate = await _seed()
        await _grant(sm, "kc-owner", SCOPE_TENANT)
        leaf = await _leaf(sm, estate)
        await _grant(sm, "kc-x", SCOPE_ORG_UNIT, leaf, role="site_admin", expires_at=PAST)
        async with _client(app, "tenant_owner", "kc-owner") as c:
            assert (await c.delete(f"/api/org-units/{leaf}")).status_code == 200
        async with _client(app, "site_admin", "kc-x") as c:
            assert (await c.get("/api/scope-grants/me")).json()["tenant_wide"] is False

    @pytest.mark.asyncio
    async def test_an_agents_grant_pins_the_unit_until_it_is_revoked(self):
        app, sm, estate = await _seed()
        await _grant(sm, "kc-owner", SCOPE_TENANT)
        leaf = await _leaf(sm, estate)
        async with sm() as session:
            await OperationalAgentRepo(session).add_scope(
                agent_id="agent-1", tenant_id=TENANT, scope_type=SCOPE_ORG_UNIT,
                scope_ref=leaf, granted_by="test")
            await session.commit()
        async with _client(app, "tenant_owner", "kc-owner") as c:
            r = await c.delete(f"/api/org-units/{leaf}")
            assert r.status_code == 409
        async with sm() as session:
            n = await OperationalAgentRepo(session).clear_scopes("agent-1", revoked_by="kc-owner")
            await session.commit()
        assert n == 1
        rows = await _rows_for(sm, "agent-1")
        assert len(rows) == 1 and rows[0].revoked_at is not None  # revoked, not deleted
        async with _client(app, "tenant_owner", "kc-owner") as c:
            assert (await c.delete(f"/api/org-units/{leaf}")).status_code == 200

    @pytest.mark.asyncio
    async def test_reassign_is_gated_on_both_targets(self):
        app, sm, estate = await _seed()
        await _grant(sm, "kc-owner", SCOPE_TENANT)
        await _grant(sm, "kc-cm", SCOPE_ORG_UNIT, estate["a1"])
        gid = await _grant(sm, "kc-x", SCOPE_ORG_UNIT, estate["a1"], role="viewer")
        other = await _grant(sm, "kc-y", SCOPE_ORG_UNIT, estate["b1"], role="viewer")
        async with _client(app, "tenant_owner", "kc-cm") as c:
            # Out of the cluster: refused on the NEW target.
            r = await c.post(f"/api/scope-grants/{gid}/reassign", json={
                "scope_type": SCOPE_ORG_UNIT, "scope_ref": estate["regb"]})
            assert r.status_code == 403
            # Somebody else's grant, elsewhere: refused on the OLD target.
            r = await c.post(f"/api/scope-grants/{other}/reassign", json={
                "scope_type": SCOPE_SITE, "scope_ref": estate["site-1"]})
            assert r.status_code == 403
            # Within the cluster: allowed.
            r = await c.post(f"/api/scope-grants/{gid}/reassign", json={
                "scope_type": SCOPE_SITE, "scope_ref": estate["site-1"]})
            assert r.status_code == 200, r.text
            # The same target again: nothing to do.
            new = r.json()["grant"]["id"]
            r = await c.post(f"/api/scope-grants/{new}/reassign", json={
                "scope_type": SCOPE_SITE, "scope_ref": estate["site-1"]})
            assert r.status_code == 400
        assert (await _row(sm, other)).scope_ref == estate["b1"]


# ---------------------------------------------------------------------------
# K. Mixed and adversarial
# ---------------------------------------------------------------------------


class TestMixed:
    @pytest.mark.asyncio
    async def test_an_orphan_grant_elsewhere_does_not_make_the_admin_revocable(self):
        """K.39: last admin + another principal's orphaned tenant_owner
        grant. The orphan neither counts as an administrator nor
        resolves to anything under legacy_open."""
        app, sm, estate = await _seed(strict=False)
        gid = await _grant(sm, "kc-owner", SCOPE_TENANT)
        await _grant(sm, "kc-second", SCOPE_TENANT)
        leaf = await _leaf(sm, estate)
        await _grant(sm, "kc-orphan", SCOPE_ORG_UNIT, leaf, role="tenant_owner")
        await _vanish_unit(sm, {"leaf": leaf}, "leaf")
        async with _client(app, "tenant_owner", "kc-second") as c:
            # Two admins: kc-second may revoke kc-owner...
            assert (await c.delete(f"/api/scope-grants/{gid}")).status_code == 200
        async with _client(app, "tenant_owner", "kc-owner") as c:
            # ...and kc-owner, now revoked, resolves to NOTHING under
            # legacy_open (A23-4: previously granted, no synthesis). The
            # grant list is empty for them and kc-second's grant is out
            # of their reach entirely -- the count never even gets asked.
            assert (await c.get("/api/scope-grants/")).json()["grants"] == []
            second = (await _rows_for(sm, "kc-second"))[0].id
            assert (await c.delete(f"/api/scope-grants/{second}")).status_code in (403, 404)
        async with _client(app, "tenant_owner", "kc-orphan") as c:
            me = (await c.get("/api/scope-grants/me")).json()
            assert me["tenant_wide"] is False and me["site_ids"] == []

    @pytest.mark.asyncio
    async def test_a_vanishing_unit_and_a_role_manage_mutation_together(self):
        """K.43: the unit under B's tenant_owner grant cannot be deleted;
        after reassignment B holds role.manage over ROOT, not the tenant,
        so A is still the last administrator."""
        app, sm, estate = await _seed()
        ga = await _grant(sm, "kc-a", SCOPE_TENANT)
        leaf = await _leaf(sm, estate)
        gb = await _grant(sm, "kc-b", SCOPE_ORG_UNIT, leaf, role="tenant_owner")
        async with _client(app, "tenant_owner", "kc-a") as c:
            assert (await c.delete(f"/api/org-units/{leaf}")).status_code == 409
            r = await c.post(f"/api/scope-grants/{gb}/reassign", json={
                "scope_type": SCOPE_ORG_UNIT, "scope_ref": estate["root"]})
            assert r.status_code == 200, r.text
            assert (await c.delete(f"/api/org-units/{leaf}")).status_code == 200
            assert (await c.delete(f"/api/scope-grants/{ga}")).status_code == 409
        async with _client(app, "tenant_owner", "kc-b") as c:
            # B reaches every site (root) yet is not a tenant administrator
            # and cannot touch A's tenant grant -- it is not even visible.
            me = (await c.get("/api/scope-grants/me")).json()
            assert me["tenant_wide"] is False
            assert set(me["site_ids"]) == {estate["site-1"], estate["site-3"]}
            grants = (await c.get("/api/scope-grants/")).json()["grants"]
            assert not [g for g in grants if g["scope_type"] == SCOPE_TENANT]
            assert (await c.delete(f"/api/scope-grants/{ga}")).status_code == 403

    @pytest.mark.asyncio
    async def test_a_self_grant_into_broader_scope_by_a_narrowed_grantor(self):
        app, sm, estate = await _seed()
        await _grant(sm, "kc-cm", SCOPE_ORG_UNIT, estate["a1"], subset=["role.manage", "fleet.view"])
        async with _client(app, "tenant_owner", "kc-cm") as c:
            r = await c.post("/api/scope-grants/", json=_grant_body(
                "kc-cm", SCOPE_TENANT, role="tenant_owner"))
            assert r.status_code == 403 and "self-grant" in r.json()["detail"]
        assert len(await _rows_for(sm, "kc-cm")) == 1


# ---------------------------------------------------------------------------
# Agent lifecycle consistency
# ---------------------------------------------------------------------------


class TestAgentLifecycle:
    @pytest.mark.asyncio
    async def test_rebinding_revokes_and_revives_rather_than_deleting(self):
        app, sm, estate = await _seed()
        async with sm() as session:
            repo = OperationalAgentRepo(session)
            first = await repo.add_scope(agent_id="agent-1", tenant_id=TENANT,
                                         scope_type=SCOPE_SITE, scope_ref=estate["site-1"])
            await session.commit()
            first_id = first.id
        async with sm() as session:
            repo = OperationalAgentRepo(session)
            await repo.clear_scopes("agent-1", revoked_by="kc-owner")
            again = await repo.add_scope(agent_id="agent-1", tenant_id=TENANT,
                                         scope_type=SCOPE_SITE, scope_ref=estate["site-1"])
            await session.commit()
            assert again.id == first_id and again.revoked_at is None
        rows = await _rows_for(sm, "agent-1")
        assert len(rows) == 1

    def test_retire_revokes_the_agents_scope(self):
        from harkeniq_cc.api import operational_agents
        src = inspect.getsource(operational_agents.transition_agent)
        assert "clear_scopes(agent.id, revoked_by=" in src
        assert '"scopes_revoked"' in src


# ---------------------------------------------------------------------------
# The route contract knows the new route
# ---------------------------------------------------------------------------


def test_reassign_is_declared_object_gated_and_audited():
    from harkeniq_cc.route_contract import OBJECT_GATED, ROUTE_CONTRACT
    assert ROUTE_CONTRACT[("POST", "/api/scope-grants/{grant_id}/reassign")] == (
        "role.manage", OBJECT_GATED, True,
    )
