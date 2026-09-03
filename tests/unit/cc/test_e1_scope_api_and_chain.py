"""E1.2: grant administration, the L1 preflight, approval scope, and the
promise that adding `site_id` to the audit log breaks no chain.

The chain test is the one that would be expensive to get wrong: the
approved design puts `site_id` OUTSIDE the hash payload precisely so
existing chains stay verifiable, and "outside" is a claim about
`AuditRepo._chain_payload` that has to be checked rather than believed.
"""

from __future__ import annotations

import httpx
import pytest

from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCApprovalRoute, CCFleetCache, CCSite
from harkeniq_cc.db.repos import (
    ApprovalRecordRepo,
    AuditRepo,
    OrgUnitRepo,
    ScopeGrantRepo,
    TenantSettingsRepo,
)
from harkeniq_cc.runtime import AppState
from harkeniq_cc.scope import (
    ENFORCEMENT_LEGACY_OPEN,
    ENFORCEMENT_STRICT,
    SCOPE_ORG_UNIT,
    SCOPE_SITE,
    SCOPE_TENANT,
)

TENANT = "tenant-demo"


async def _stack():
    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    app = create_app(
        AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    )
    return app, sessionmaker


def _client(app, role, user_id):
    async def _fake():
        return UserContext(
            user_id=user_id, email=f"{user_id}@example.com", tenant_id=TENANT,
            role=role,
            permissions=list(ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS["viewer"])),
        )

    app.dependency_overrides[get_current_user] = _fake
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _estate(sessionmaker) -> dict:
    out: dict = {}
    async with sessionmaker() as session:
        repo = OrgUnitRepo(session)
        root = await repo.create(TENANT, name="demo", unit_type="organization",
                                 parent=None)
        rega = await repo.create(TENANT, name="Region A", unit_type="region",
                                 parent=root)
        regb = await repo.create(TENANT, name="Region B", unit_type="region",
                                 parent=root)
        a1 = await repo.create(TENANT, name="Cluster A1", unit_type="cluster",
                               parent=rega)
        b1 = await repo.create(TENANT, name="Cluster B1", unit_type="cluster",
                               parent=regb)
        out.update(root=root.id, rega=rega.id, regb=regb.id, a1=a1.id, b1=b1.id)
        for name, unit in (("site-1", a1), ("site-3", b1)):
            site = CCSite(tenant_id=TENANT, site_name=name,
                          sm_endpoint="sm:1", sm_token="t", org_unit_id=unit.id)
            session.add(site)
            await session.flush()
            out[name] = site.id
            session.add(CCFleetCache(
                site_id=site.id, agent_id=f"node-{name}", agent_name=name,
                vendor="Dell", model="R750", health="ok", device_class="server",
            ))
        await session.commit()
    return out


async def _grant(sessionmaker, ref, scope_type, scope_ref="", role="",
                 subset=None):
    async with sessionmaker() as session:
        await ScopeGrantRepo(session).grant(
            tenant_id=TENANT, principal_type="user", principal_ref=ref,
            scope_type=scope_type, scope_ref=scope_ref, role=role,
            permission_subset=subset, granted_by="test",
        )
        await session.commit()


async def _strict(sessionmaker):
    async with sessionmaker() as session:
        await TenantSettingsRepo(session).set_enforcement(
            TENANT, ENFORCEMENT_STRICT, "test"
        )
        await session.commit()


# ---------------------------------------------------------------------------
# The audit chain
# ---------------------------------------------------------------------------


class TestAuditChainSurvivesSiteScoping:
    def test_site_id_is_not_in_the_hash_payload(self):
        """The design claim, checked against the code.

        If `site_id` ever entered `_chain_payload`, every chain written
        before E1.2 would stop verifying -- silently, and only on a
        deployment old enough to have one.
        """
        import inspect

        source = inspect.getsource(AuditRepo._chain_payload)
        assert "site_id" not in source
        assert set(_payload_keys()) == {
            "ts", "actor", "action", "subject", "tenant_id", "detail"
        }

    @pytest.mark.asyncio
    async def test_a_chain_written_without_sites_still_verifies_after(self):
        app, sessionmaker = await _stack()
        async with sessionmaker() as session:
            repo = AuditRepo(session)
            # Pre-E1.2 shape: no site recorded, because there was no column.
            for n in range(5):
                await repo.append(actor="old", action=f"legacy.{n}",
                                  tenant_id=TENANT)
            await session.commit()
        async with sessionmaker() as session:
            assert (await AuditRepo(session).verify_chain()).valid

        # Now write E1.2-shaped entries carrying a site, onto the SAME chain.
        async with sessionmaker() as session:
            repo = AuditRepo(session)
            for n in range(5):
                await repo.append(actor="new", action=f"scoped.{n}",
                                  tenant_id=TENANT, site_id=f"site-{n}")
            await session.commit()

        async with sessionmaker() as session:
            result = await AuditRepo(session).verify_chain()
            assert result.valid, "the chain broke when a site was recorded"
            assert result.length == 10

    @pytest.mark.asyncio
    async def test_two_entries_differing_only_by_site_hash_identically(self):
        """Direct evidence that the site is outside the payload."""
        app, sessionmaker = await _stack()
        async with sessionmaker() as session:
            repo = AuditRepo(session)
            a = await repo.append(actor="x", action="same", tenant_id=TENANT,
                                  site_id="site-1")
            payload_a = AuditRepo._chain_payload(a)
            b_row = type(a)(
                ts=a.ts, actor=a.actor, action=a.action, subject=a.subject,
                tenant_id=a.tenant_id, detail=a.detail, site_id="site-9",
            )
            assert AuditRepo._chain_payload(b_row) == payload_a
            await session.rollback()


def _payload_keys():
    from harkeniq_cc.db.models import CCAuditLog
    from datetime import datetime, timezone

    row = CCAuditLog(
        ts=datetime(2026, 1, 1, tzinfo=timezone.utc), actor="a", action="b",
        subject="c", tenant_id="t", detail=None, site_id="s",
    )
    return AuditRepo._chain_payload(row).keys()


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------


class TestGrantAdministration:
    @pytest.mark.asyncio
    async def test_a_tenant_owner_grants_and_revokes(self):
        app, sessionmaker = await _stack()
        estate = await _estate(sessionmaker)
        await _strict(sessionmaker)
        await _grant(sessionmaker, "kc-owner", SCOPE_TENANT, role="tenant_owner")
        client = _client(app, "tenant_owner", "kc-owner")
        async with client:
            made = await client.post("/api/scope-grants/", json={
                "principal_ref": "kc-cm", "scope_type": "org_unit",
                "scope_ref": estate["a1"], "role": "site_admin",
            })
            assert made.status_code == 201, made.text
            grant_id = made.json()["id"]

            listed = (await client.get("/api/scope-grants/")).json()
            assert any(g["principal_ref"] == "kc-cm" for g in listed["grants"])

            gone = await client.delete(f"/api/scope-grants/{grant_id}")
            assert gone.status_code == 200
            assert gone.json()["revoked_at"] is not None

            # Revocation is a timestamp, not a delete: the row survives so
            # an approval's scope_snapshot stays addressable (L2).
            with_revoked = (
                await client.get("/api/scope-grants/?include_revoked=true")
            ).json()
            assert any(g["id"] == grant_id for g in with_revoked["grants"])

    @pytest.mark.asyncio
    async def test_a_cluster_manager_cannot_grant_outside_their_cluster(self):
        app, sessionmaker = await _stack()
        estate = await _estate(sessionmaker)
        await _strict(sessionmaker)
        # role.manage is what grants require; give it explicitly at a1.
        await _grant(sessionmaker, "kc-cm", SCOPE_ORG_UNIT, estate["a1"],
                     role="tenant_owner")
        client = _client(app, "tenant_owner", "kc-cm")
        async with client:
            outside = await client.post("/api/scope-grants/", json={
                "principal_ref": "kc-x", "scope_type": "org_unit",
                "scope_ref": estate["regb"], "role": "site_admin",
            })
            assert outside.status_code == 403

            inside = await client.post("/api/scope-grants/", json={
                "principal_ref": "kc-x", "scope_type": "site",
                "scope_ref": estate["site-1"], "role": "site_admin",
            })
            assert inside.status_code == 201, inside.text

    @pytest.mark.asyncio
    async def test_nobody_below_tenant_may_hand_out_tenant_scope(self):
        app, sessionmaker = await _stack()
        estate = await _estate(sessionmaker)
        await _strict(sessionmaker)
        await _grant(sessionmaker, "kc-cm", SCOPE_ORG_UNIT, estate["a1"],
                     role="tenant_owner")
        client = _client(app, "tenant_owner", "kc-cm")
        async with client:
            resp = await client.post("/api/scope-grants/", json={
                "principal_ref": "kc-x", "scope_type": "tenant",
                "role": "tenant_owner",
            })
            assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_a_subset_that_would_widen_a_role_is_refused_visibly(self):
        app, sessionmaker = await _stack()
        estate = await _estate(sessionmaker)
        await _strict(sessionmaker)
        await _grant(sessionmaker, "kc-owner", SCOPE_TENANT, role="tenant_owner")
        client = _client(app, "tenant_owner", "kc-owner")
        async with client:
            resp = await client.post("/api/scope-grants/", json={
                "principal_ref": "kc-op", "scope_type": "site",
                "scope_ref": estate["site-1"], "role": "operator",
                "permission_subset": ["role.manage"],
            })
            assert resp.status_code == 400
            assert "may only narrow" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_grant_to_a_nonexistent_target_is_refused(self):
        app, sessionmaker = await _stack()
        await _estate(sessionmaker)
        await _strict(sessionmaker)
        await _grant(sessionmaker, "kc-owner", SCOPE_TENANT, role="tenant_owner")
        client = _client(app, "tenant_owner", "kc-owner")
        async with client:
            resp = await client.post("/api/scope-grants/", json={
                "principal_ref": "kc-x", "scope_type": "org_unit",
                "scope_ref": "f" * 32, "role": "site_admin",
            })
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_granting_requires_role_manage(self):
        app, sessionmaker = await _stack()
        estate = await _estate(sessionmaker)
        client = _client(app, "site_admin", "kc-sa")
        async with client:
            resp = await client.post("/api/scope-grants/", json={
                "principal_ref": "kc-x", "scope_type": "site",
                "scope_ref": estate["site-1"],
            })
            assert resp.status_code == 403
            assert "role.manage" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_every_principal_may_read_their_own_scope(self):
        app, sessionmaker = await _stack()
        estate = await _estate(sessionmaker)
        await _strict(sessionmaker)
        await _grant(sessionmaker, "kc-cm", SCOPE_ORG_UNIT, estate["a1"],
                     role="site_admin")
        client = _client(app, "site_admin", "kc-cm")
        async with client:
            mine = (await client.get("/api/scope-grants/me")).json()
            assert mine["site_ids"] == [estate["site-1"]]
            # The contextual block is a separate, self-describing object
            # so nothing downstream can mistake it for reach.
            assert mine["contextual_unit_ids"]["authority"] is False
            assert set(mine["contextual_unit_ids"]["ids"]) == {
                estate["root"], estate["rega"]
            }


class TestStrictModePreflight:
    @pytest.mark.asyncio
    async def test_the_flip_is_refused_when_no_real_grant_exists(self):
        """The lockout L1 prevents.

        Under `legacy_open` a principal with no grants resolves
        tenant-wide -- that is what keeps upgrades working. Flipping to
        strict on the strength of that SYNTHESIZED grant would leave
        every principal, including the one flipping, with nothing.
        """
        app, sessionmaker = await _stack()
        estate = await _estate(sessionmaker)
        # A23-5: this scenario is an EXISTING legacy tenant flipping to
        # strict, so the posture is pinned the way migration 0021 pins
        # one. Left rowless the tenant would already be strict (A23.11)
        # and the caller would be refused by the route guard at 403,
        # never reaching the preflight this test is about.
        async with sessionmaker() as session:
            await TenantSettingsRepo(session).set_enforcement(
                TENANT, ENFORCEMENT_LEGACY_OPEN, "migration:0021",
            )
            await session.commit()
        client = _client(app, "tenant_owner", "kc-owner")
        async with client:
            resp = await client.put(
                "/api/tenant-settings/scope-enforcement", json={"mode": "strict"}
            )
            assert resp.status_code == 409
            assert "role.manage" in resp.json()["detail"]
            assert "Nothing has been changed" in resp.json()["detail"]

        # And nothing was applied. A23-5 changed what "unchanged" reads
        # as -- a rowless tenant answers STRICT now (A23.11), not
        # `legacy_open` -- so the refusal is asserted where it actually
        # lives: the refused flip wrote no row at all.
        async with sessionmaker() as session:
            assert await TenantSettingsRepo(session).enforcement(TENANT) == (
                ENFORCEMENT_LEGACY_OPEN
            )

    @pytest.mark.asyncio
    async def test_the_flip_succeeds_once_an_administrator_exists(self):
        app, sessionmaker = await _stack()
        await _estate(sessionmaker)
        await _grant(sessionmaker, "kc-owner", SCOPE_TENANT, role="tenant_owner")
        client = _client(app, "tenant_owner", "kc-owner")
        async with client:
            state = (await client.get("/api/tenant-settings/scope-enforcement")).json()
            assert state["strict_ready"] is True
            assert state["tenant_admin_count"] == 1

            resp = await client.put(
                "/api/tenant-settings/scope-enforcement", json={"mode": "strict"}
            )
            assert resp.status_code == 200
            assert resp.json()["scope_enforcement"] == "strict"

    @pytest.mark.asyncio
    async def test_a_subset_that_removes_role_manage_cannot_even_reach_the_flip(self):
        """Narrowing away role.manage removes the authority entirely.

        The caller is refused at the object gate (403) rather than the
        preflight (409) -- the subset took the permission away, so there
        is no question of whether an administrator would remain.
        """
        app, sessionmaker = await _stack()
        await _estate(sessionmaker)
        await _grant(sessionmaker, "kc-owner", SCOPE_TENANT, role="tenant_owner",
                     subset=["fleet.view", "site.manage"])
        client = _client(app, "tenant_owner", "kc-owner")
        async with client:
            resp = await client.put(
                "/api/tenant-settings/scope-enforcement", json={"mode": "strict"}
            )
            assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_an_org_scoped_principal_cannot_reach_the_flip_at_all(self):
        app, sessionmaker = await _stack()
        estate = await _estate(sessionmaker)
        await _grant(sessionmaker, "kc-cm", SCOPE_ORG_UNIT, estate["a1"],
                     role="tenant_owner")
        client = _client(app, "tenant_owner", "kc-cm")
        async with client:
            resp = await client.put(
                "/api/tenant-settings/scope-enforcement", json={"mode": "strict"}
            )
            assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_flipping_back_to_legacy_open_needs_no_preflight(self):
        app, sessionmaker = await _stack()
        await _estate(sessionmaker)
        await _grant(sessionmaker, "kc-owner", SCOPE_TENANT, role="tenant_owner")
        client = _client(app, "tenant_owner", "kc-owner")
        async with client:
            await client.put("/api/tenant-settings/scope-enforcement",
                             json={"mode": "strict"})
            back = await client.put("/api/tenant-settings/scope-enforcement",
                                    json={"mode": "legacy_open"})
            assert back.status_code == 200

    @pytest.mark.asyncio
    async def test_the_flip_is_audited(self):
        app, sessionmaker = await _stack()
        await _estate(sessionmaker)
        await _grant(sessionmaker, "kc-owner", SCOPE_TENANT, role="tenant_owner")
        client = _client(app, "tenant_owner", "kc-owner")
        async with client:
            await client.put("/api/tenant-settings/scope-enforcement",
                             json={"mode": "strict"})
        async with sessionmaker() as session:
            rows = await AuditRepo(session).list_filtered(
                tenant_id=TENANT, page_size=50
            )
            actions = [r.action for r in rows]
            assert "scope.enforcement_changed" in actions
            assert (await AuditRepo(session).verify_chain()).valid


class TestApprovalScope:
    """Layer 4, and the ratified L2 snapshots."""

    async def _routed(self, sessionmaker, estate, site_key="site-3"):
        async with sessionmaker() as session:
            route = CCApprovalRoute(
                action_id=f"act-{site_key}",
                site_id=estate[site_key],
                device_agent_id=f"node-{site_key}",
                action_type="SEL_CLEAR",
            )
            session.add(route)
            await session.commit()
            return route.action_id

    @pytest.mark.asyncio
    async def test_an_approver_cannot_decide_outside_their_scope(self):
        app, sessionmaker = await _stack()
        estate = await _estate(sessionmaker)
        await _strict(sessionmaker)
        await _grant(sessionmaker, "kc-op", SCOPE_SITE, estate["site-1"],
                     role="operator")
        action_id = await self._routed(sessionmaker, estate, "site-3")

        client = _client(app, "operator", "kc-op")
        async with client:
            resp = await client.post(f"/api/approvals/{action_id}/approve")
            assert resp.status_code == 403
            assert "outside your authorized scope" in resp.json()["detail"]

        # Refused, NOT recorded: a name in the ledger beside a decision
        # they were not entitled to make would corrupt the evidence.
        async with sessionmaker() as session:
            records = await ApprovalRecordRepo(session).list_for_subject(
                "action", action_id
            )
            assert records == []

    @pytest.mark.asyncio
    async def test_an_approver_inside_their_scope_decides_and_snapshots(self):
        app, sessionmaker = await _stack()
        estate = await _estate(sessionmaker)
        await _strict(sessionmaker)
        await _grant(sessionmaker, "kc-op", SCOPE_SITE, estate["site-1"],
                     role="operator")
        action_id = await self._routed(sessionmaker, estate, "site-1")

        client = _client(app, "operator", "kc-op")
        async with client:
            resp = await client.post(f"/api/approvals/{action_id}/deny")
            assert resp.status_code == 200, resp.text

        async with sessionmaker() as session:
            records = await ApprovalRecordRepo(session).list_for_subject(
                "action", action_id
            )
            assert len(records) == 1
            record = records[0]
            # L2: the VALUES, not a verdict.
            assert record.scope_snapshot["site_ids"] == [estate["site-1"]]
            assert record.scope_snapshot["enforcement"] == "strict"
            assert record.authority_snapshot["permission"] == "action.approve"
            assert record.authority_snapshot["target_site_id"] == estate["site-1"]

    @pytest.mark.asyncio
    async def test_an_earlier_approval_survives_a_later_tree_change(self):
        """Ratified L2. A reorganisation must not void a real decision."""
        app, sessionmaker = await _stack()
        estate = await _estate(sessionmaker)
        await _strict(sessionmaker)
        await _grant(sessionmaker, "kc-op", SCOPE_SITE, estate["site-1"],
                     role="operator")
        action_id = await self._routed(sessionmaker, estate, "site-1")

        client = _client(app, "operator", "kc-op")
        async with client:
            await client.post(f"/api/approvals/{action_id}/deny")

        async with sessionmaker() as session:
            before = (
                await ApprovalRecordRepo(session).list_for_subject("action", action_id)
            )[0]
            snapshot = dict(before.scope_snapshot)

            # Now move the site to the other region and revoke the grant.
            repo = OrgUnitRepo(session)
            site = await session.get(CCSite, estate["site-1"])
            site.org_unit_id = estate["b1"]
            for row in await ScopeGrantRepo(session).list_for_principal(
                TENANT, "kc-op"
            ):
                await ScopeGrantRepo(session).revoke(row, "test")
            await session.commit()

        async with sessionmaker() as session:
            after = (
                await ApprovalRecordRepo(session).list_for_subject("action", action_id)
            )[0]
            assert after.decision == before.decision
            assert after.scope_snapshot == snapshot, (
                "a later reorganisation rewrote a recorded approval"
            )


class TestGrantsAreRealmScoped:
    """E1.4: a grant is a (realm, subject) fact.

    Keycloak subjects are realm-scoped: the same id means nothing across
    realms, and the same person has a different id in each. Keyed on the
    subject alone, moving a tenant onto its own realm silently orphaned
    every grant -- and under strict enforcement that locked the tenant
    out completely, including the administrator who would re-grant.
    """

    @pytest.mark.asyncio
    async def test_a_grant_from_another_realm_does_not_authorize(self):
        from harkeniq_cc.governance import load_scope

        app, sessionmaker = await _stack()
        estate = await _estate(sessionmaker)
        # Strict, or legacy_open synthesizes a tenant-wide grant and the
        # assertion would be about the fallback rather than the realm.
        await _strict(sessionmaker)
        async with sessionmaker() as session:
            await ScopeGrantRepo(session).grant(
                tenant_id=TENANT, principal_type="user", principal_ref="sub-1",
                scope_type=SCOPE_TENANT, role="tenant_owner",
                realm="harkeniq-platform", granted_by="test",
            )
            await session.commit()

        async with sessionmaker() as session:
            # Serving the TENANT realm: the platform-realm grant is not ours.
            scope = await load_scope(
                session, tenant_id=TENANT, principal_ref="sub-1",
                role_permissions=["fleet.view"], realm="tenant-demo",
            )
            assert scope.is_empty(), "a grant from another realm authorized"

        async with sessionmaker() as session:
            # Serving the realm it was made under: it counts.
            scope = await load_scope(
                session, tenant_id=TENANT, principal_ref="sub-1",
                role_permissions=["fleet.view"], realm="harkeniq-platform",
            )
            assert scope.tenant_wide

    @pytest.mark.asyncio
    async def test_a_pre_e14_grant_with_no_realm_still_counts(self):
        """An upgrade must change nothing."""
        from harkeniq_cc.governance import load_scope

        app, sessionmaker = await _stack()
        await _estate(sessionmaker)
        await _strict(sessionmaker)
        async with sessionmaker() as session:
            await ScopeGrantRepo(session).grant(
                tenant_id=TENANT, principal_type="user", principal_ref="legacy",
                scope_type=SCOPE_TENANT, role="tenant_owner", realm="",
                granted_by="test",
            )
            await session.commit()
        async with sessionmaker() as session:
            scope = await load_scope(
                session, tenant_id=TENANT, principal_ref="legacy",
                role_permissions=["fleet.view"], realm="tenant-demo",
            )
            assert scope.tenant_wide

    @pytest.mark.asyncio
    async def test_the_census_makes_a_realm_lockout_visible(self):
        """Without this, every principal simply sees nothing.

        A silent lockout is the worst shape this failure can take: it
        looks exactly like correctly-configured strict enforcement.
        """
        app, sessionmaker = await _stack()
        await _estate(sessionmaker)
        async with sessionmaker() as session:
            repo = ScopeGrantRepo(session)
            for ref in ("old-1", "old-2"):
                await repo.grant(
                    tenant_id=TENANT, principal_type="user", principal_ref=ref,
                    scope_type=SCOPE_TENANT, role="tenant_owner",
                    realm="harkeniq-platform", granted_by="test",
                )
            await session.commit()

        async with sessionmaker() as session:
            census = await ScopeGrantRepo(session).realm_census(TENANT)
            assert census == {"harkeniq-platform": 2}
            # Nothing for the realm this Central Command would serve.
            assert census.get("tenant-demo", 0) == 0

    @pytest.mark.asyncio
    async def test_an_agent_grant_is_never_narrowed_by_a_realm(self):
        """An agent id is a CC row id, not a realm subject."""
        from harkeniq_cc.governance import load_agent_scope

        app, sessionmaker = await _stack()
        estate = await _estate(sessionmaker)
        async with sessionmaker() as session:
            await ScopeGrantRepo(session).grant(
                tenant_id=TENANT, principal_type="agent", principal_ref="ag-1",
                scope_type=SCOPE_SITE, scope_ref=estate["site-1"],
                granted_by="test",
            )
            await session.commit()
        async with sessionmaker() as session:
            scope = await load_agent_scope(
                session, tenant_id=TENANT, agent_id="ag-1"
            )
            assert scope.covers_site(estate["site-1"])
