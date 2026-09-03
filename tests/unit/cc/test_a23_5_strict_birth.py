"""A23-5: strict birth (spec A23.11, A23.14).

Three things have to be true together, and each is false without the
others:

* an EXISTING tenant keeps the posture it already had, stated explicitly
  by migration 0021 rather than implied by a missing row;
* an unpinned tenant reads STRICT, so the platform's most permissive
  posture can no longer be reached by an absence;
* a NEW tenant gets an administrator at birth, because strict
  enforcement with nobody granted is a tenant nobody can administer.

The first without the second changes nothing; the second without the
first locks out every upgraded deployment; the second without the third
births tenants that can never be used.
"""

from __future__ import annotations

import httpx
import pytest

from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCTenantSettings
from harkeniq_cc.db.repos import AuditRepo, ScopeGrantRepo, TenantSettingsRepo
from harkeniq_cc.governance import load_scope
from harkeniq_cc.runtime import AppState
from harkeniq_cc.scope import (
    ENFORCEMENT_LEGACY_OPEN,
    ENFORCEMENT_STRICT,
    PRINCIPAL_USER,
    SCOPE_TENANT,
    count_tenant_admins,
)
from harkeniq_cc.tenant_birth import SEED_ACTOR, seed_tenant_birth

TENANT = "tenant-demo"
REALM = "tenant-demo"
OWNER = "kc-owner-subject"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _db():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    return engine, make_sessionmaker(engine)


class _State:
    """The bits of AppState `seed_tenant_birth` reads."""

    def __init__(self, sessionmaker, *, tenant=TENANT, realm=REALM,
                 console_url="http://console"):
        self.sessionmaker = sessionmaker
        self.config = CCConfig(
            tenant_id=tenant, keycloak_realm=realm, insecure=True,
        )
        self.config.console_url = console_url
        self.config.console_api_key = "k"


def _console(monkeypatch, owners, reason=""):
    """Stand in for the Console's internal owners endpoint."""
    from harkeniq_cc import identity_client

    async def _get(state, path):
        if reason:
            return None, reason
        return {"owners": owners}, ""

    monkeypatch.setattr(identity_client, "get", _get)


# ---------------------------------------------------------------------------
# The default
# ---------------------------------------------------------------------------


class TestTheDefaultIsStrict:
    @pytest.mark.asyncio
    async def test_an_unpinned_tenant_reads_strict(self):
        """The invariant A23.11 retires: missing row -> legacy_open."""
        engine, sm = await _db()
        async with sm() as session:
            assert await TenantSettingsRepo(session).enforcement(
                "never-seen"
            ) == ENFORCEMENT_STRICT
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_an_explicit_posture_of_either_kind_is_honoured(self):
        engine, sm = await _db()
        async with sm() as session:
            repo = TenantSettingsRepo(session)
            await repo.set_enforcement("t-legacy", ENFORCEMENT_LEGACY_OPEN, "x")
            await repo.set_enforcement("t-strict", ENFORCEMENT_STRICT, "x")
            await session.commit()
        async with sm() as session:
            repo = TenantSettingsRepo(session)
            assert await repo.enforcement("t-legacy") == ENFORCEMENT_LEGACY_OPEN
            assert await repo.enforcement("t-strict") == ENFORCEMENT_STRICT
        await engine.dispose()

    def test_the_model_default_agrees_with_the_repository_default(self):
        """Two defaults, one answer.

        The column default and `enforcement()` are different code paths
        to the same question, and E1.2 shipped them disagreeing -- the
        model's own docstring promised `strict` while both answered
        `legacy_open`. A divergence here is an authorization posture
        that depends on which path a row took.
        """
        column = CCTenantSettings.__table__.c.scope_enforcement
        assert column.default.arg == ENFORCEMENT_STRICT

    @pytest.mark.asyncio
    async def test_a_never_granted_human_gets_no_synthesis_on_an_unpinned_tenant(self):
        """A23.10 and A23.11 meet here.

        Synthesis needs `legacy_open`; an unpinned tenant no longer
        supplies it, so an ungranted principal reaches nothing.
        """
        engine, sm = await _db()
        async with sm() as session:
            scope = await load_scope(
                session, tenant_id=TENANT, principal_ref="kc-nobody",
                role_permissions=list(ROLE_PERMISSIONS["tenant_owner"]),
            )
        assert scope.enforcement == ENFORCEMENT_STRICT
        assert scope.tenant_wide is False
        assert scope.synthesis == "strict"
        await engine.dispose()


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------


class TestTheFirstGrantSeam:
    @pytest.mark.asyncio
    async def test_it_writes_one_ordinary_tenant_grant(self):
        engine, sm = await _db()
        async with sm() as session:
            row = await ScopeGrantRepo(session).seed_first_grant(
                tenant_id=TENANT, principal_ref=OWNER, role="tenant_owner",
                realm=REALM, granted_by=SEED_ACTOR,
            )
            await session.commit()
            assert row is not None
            assert row.principal_type == PRINCIPAL_USER
            assert row.scope_type == SCOPE_TENANT
            assert row.scope_ref == ""
            assert row.expires_at is None
            assert row.permission_subset is None
            assert row.granted_by == SEED_ACTOR
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_it_refuses_when_the_tenant_holds_any_grant(self):
        engine, sm = await _db()
        async with sm() as session:
            repo = ScopeGrantRepo(session)
            await repo.grant(
                tenant_id=TENANT, principal_type=PRINCIPAL_USER,
                principal_ref="kc-someone", scope_type=SCOPE_TENANT,
                role="viewer", granted_by="an administrator",
            )
            await session.commit()
        async with sm() as session:
            assert await ScopeGrantRepo(session).seed_first_grant(
                tenant_id=TENANT, principal_ref=OWNER, role="tenant_owner",
                realm=REALM, granted_by=SEED_ACTOR,
            ) is None
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_a_revoked_grant_still_stops_it(self):
        """The reason this is not `ScopeGrantRepo.grant` (A23.10).

        `grant()` REVIVES a revoked row. If provisioning went through it,
        a deliberately removed administrator could be restored by a
        provisioning pass -- authority returning through an absence,
        which is the exact thing A23-4 spent a slice removing.
        """
        engine, sm = await _db()
        async with sm() as session:
            repo = ScopeGrantRepo(session)
            row = await repo.grant(
                tenant_id=TENANT, principal_type=PRINCIPAL_USER,
                principal_ref=OWNER, scope_type=SCOPE_TENANT,
                role="tenant_owner", granted_by="an administrator",
            )
            await repo.revoke(row, "an administrator")
            await session.commit()
        async with sm() as session:
            assert await ScopeGrantRepo(session).seed_first_grant(
                tenant_id=TENANT, principal_ref=OWNER, role="tenant_owner",
                realm=REALM, granted_by=SEED_ACTOR,
            ) is None
            rows = await ScopeGrantRepo(session).list_for_principal(
                TENANT, OWNER, include_revoked=True,
            )
            assert len(rows) == 1 and rows[0].revoked_at is not None
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_the_seeded_grant_is_an_ordinary_administrator(self):
        """It confers nothing special, and A23.8 protects it like any other."""
        engine, sm = await _db()
        async with sm() as session:
            await ScopeGrantRepo(session).seed_first_grant(
                tenant_id=TENANT, principal_ref=OWNER, role="tenant_owner",
                realm=REALM, granted_by=SEED_ACTOR,
            )
            await session.commit()
        async with sm() as session:
            rows = await ScopeGrantRepo(session).list_all(TENANT)
            n = count_tenant_admins(
                rows,
                lambda r: list(ROLE_PERMISSIONS.get(r.role, [])),
                realm=REALM,
            )
            assert n == 1
            # And removing it would leave none, which is what A23.8 refuses.
            after = count_tenant_admins(
                rows,
                lambda r: list(ROLE_PERMISSIONS.get(r.role, [])),
                realm=REALM,
                exclude_ids=[rows[0].id],
            )
            assert after == 0
        await engine.dispose()


# ---------------------------------------------------------------------------
# Birth
# ---------------------------------------------------------------------------


class TestTenantBirth:
    @pytest.mark.asyncio
    async def test_a_virgin_tenant_is_born_strict_with_its_administrator(
        self, monkeypatch,
    ):
        engine, sm = await _db()
        _console(monkeypatch, [{"keycloak_user_id": OWNER, "email": "o@x"}])
        state = _State(sm)
        async with sm() as session:
            outcome = await seed_tenant_birth(state, session)
            await session.commit()
        assert outcome.status == "seeded" and outcome.principal_ref == OWNER
        async with sm() as session:
            assert await TenantSettingsRepo(session).enforcement(
                TENANT
            ) == ENFORCEMENT_STRICT
            rows = await ScopeGrantRepo(session).list_all(TENANT)
            assert len(rows) == 1 and rows[0].principal_ref == OWNER
            scope = await load_scope(
                session, tenant_id=TENANT, principal_ref=OWNER,
                role_permissions=list(ROLE_PERMISSIONS["tenant_owner"]),
            )
            assert scope.tenant_wide is True
            assert scope.synthesis == "granted"
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_it_is_idempotent(self, monkeypatch):
        engine, sm = await _db()
        _console(monkeypatch, [{"keycloak_user_id": OWNER, "email": "o@x"}])
        state = _State(sm)
        for _ in range(3):
            async with sm() as session:
                outcome = await seed_tenant_birth(state, session)
                await session.commit()
        assert outcome.status == "already_born"
        async with sm() as session:
            assert len(await ScopeGrantRepo(session).list_all(TENANT)) == 1
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_a_migrated_tenant_is_never_seeded(self, monkeypatch):
        """A23.14 D5: migrated is not newly born.

        Migration 0021 pins an existing tenant, and that row is the stop
        sign -- a historical tenant with no usable administrator is
        reported, never handed a synthetic one.
        """
        engine, sm = await _db()
        _console(monkeypatch, [{"keycloak_user_id": OWNER, "email": "o@x"}])
        async with sm() as session:
            await TenantSettingsRepo(session).set_enforcement(
                TENANT, ENFORCEMENT_LEGACY_OPEN, "migration:0021",
            )
            await session.commit()
        async with sm() as session:
            outcome = await seed_tenant_birth(_State(sm), session)
            await session.commit()
        assert outcome.status == "already_born"
        async with sm() as session:
            assert await ScopeGrantRepo(session).list_all(TENANT) == []
            assert await TenantSettingsRepo(session).enforcement(
                TENANT
            ) == ENFORCEMENT_LEGACY_OPEN
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_a_tenant_that_ever_held_a_grant_is_never_seeded(
        self, monkeypatch,
    ):
        engine, sm = await _db()
        _console(monkeypatch, [{"keycloak_user_id": OWNER, "email": "o@x"}])
        async with sm() as session:
            repo = ScopeGrantRepo(session)
            row = await repo.grant(
                tenant_id=TENANT, principal_type=PRINCIPAL_USER,
                principal_ref="kc-gone", scope_type=SCOPE_TENANT,
                role="tenant_owner", granted_by="an administrator",
            )
            await repo.revoke(row, "an administrator")
            await session.commit()
        async with sm() as session:
            outcome = await seed_tenant_birth(_State(sm), session)
            await session.commit()
        assert outcome.status == "already_born"
        async with sm() as session:
            rows = await ScopeGrantRepo(session).list_all(
                TENANT, include_revoked=True,
            )
            assert len(rows) == 1 and rows[0].principal_ref == "kc-gone"
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_no_owner_subject_refuses_rather_than_inventing_one(
        self, monkeypatch,
    ):
        """An email is not an identity, so an unknown subject is a refusal."""
        engine, sm = await _db()
        _console(monkeypatch, [])
        async with sm() as session:
            outcome = await seed_tenant_birth(_State(sm), session)
            await session.commit()
        assert outcome.status == "unadministered"
        async with sm() as session:
            assert await ScopeGrantRepo(session).list_all(TENANT) == []
            assert await TenantSettingsRepo(session).get(TENANT) is None
            refusals = await AuditRepo(session).list_filtered(TENANT, page_size=50)
        actions = [r.action for r in refusals]
        assert "scope.grant_refused" in actions
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_an_unreachable_console_leaves_the_tenant_untouched(
        self, monkeypatch,
    ):
        engine, sm = await _db()
        _console(monkeypatch, [], reason="the Console could not be reached")
        async with sm() as session:
            outcome = await seed_tenant_birth(_State(sm), session)
            await session.commit()
        assert outcome.status == "unadministered"
        async with sm() as session:
            assert await ScopeGrantRepo(session).list_all(TENANT) == []
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_birth_is_audited_with_a_system_actor_and_no_secret(
        self, monkeypatch,
    ):
        engine, sm = await _db()
        _console(monkeypatch, [{"keycloak_user_id": OWNER, "email": "o@x"}])
        async with sm() as session:
            await seed_tenant_birth(_State(sm), session)
            await session.commit()
        async with sm() as session:
            rows = await AuditRepo(session).list_filtered(TENANT, page_size=50)
        by_action = {r.action: r for r in rows}
        assert "scope.granted" in by_action
        assert "scope.enforcement_changed" in by_action
        granted = by_action["scope.granted"]
        assert granted.actor == SEED_ACTOR
        assert granted.actor_ref == SEED_ACTOR
        assert granted.subject == OWNER
        assert granted.detail["seeded"] is True
        assert granted.detail["source"] == "tenant_birth"
        # The chain still verifies with a non-principal writer in it.
        async with sm() as session:
            result = await AuditRepo(session).verify_chain()
        assert result.valid
        await engine.dispose()


# ---------------------------------------------------------------------------
# The seeded administrator, over the real app
# ---------------------------------------------------------------------------


class TestOverTheApp:
    @pytest.mark.asyncio
    async def test_the_born_owner_can_administer_and_a_stranger_cannot(
        self, monkeypatch,
    ):
        engine, sm = await _db()
        _console(monkeypatch, [{"keycloak_user_id": OWNER, "email": "o@x"}])
        config = CCConfig(tenant_id=TENANT, keycloak_realm=REALM, insecure=True)
        configure_auth("", "", "", insecure=True)
        state = AppState(config=config, engine=engine, sessionmaker=sm)
        app = create_app(state)

        state.config.console_url = "http://console"
        state.config.console_api_key = "k"
        async with sm() as session:
            await seed_tenant_birth(state, session)
            await session.commit()

        def _client(sub, role):
            async def _fake():
                return UserContext(
                    user_id=sub, email=f"{sub}@x", tenant_id=TENANT, role=role,
                    permissions=list(ROLE_PERMISSIONS[role]),
                )

            app.dependency_overrides[get_current_user] = _fake
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
            )

        async with _client(OWNER, "tenant_owner") as c:
            me = (await c.get("/api/scope-grants/me")).json()
            assert me["tenant_wide"] is True
            assert me["synthesis"] == "granted"

        async with _client("kc-stranger", "tenant_owner") as c:
            me = (await c.get("/api/scope-grants/me")).json()
            # Strict, never granted: no reach, and no synthesis to rescue it.
            assert me["tenant_wide"] is False
            assert me["synthesis"] == "strict"
        await engine.dispose()
