"""CC role grants are real and mirror the Console's permission model.

P0 2026-08-29 (final-assessment C1): CC's guards check fleet.view /
action.approve / site.manage / audit.view, but the old mapping granted
only "*" (admins) or the literal "view" — so every non-admin role was
locked out of the entire API: operators could not approve (contradicting
R-C4/spec §4) and viewers could not view. These tests pin:

1. Route behavior per role, through get_current_user itself (no
   permission-list override — the mapping under test is the thing that
   produces the permissions).
2. Parity between CC's ROLE_PERMISSIONS and the Console's for the shared
   tenant roles, so the two services' vocabularies cannot drift apart.
"""

import httpx
import pytest

from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.runtime import AppState

TENANT = "test-tenant"


def _user_for_role(role: str) -> UserContext:
    """Build the context exactly as get_current_user's mapping would."""
    return UserContext(
        user_id=f"kc-{role}",
        email=f"{role}@example.com",
        tenant_id=TENANT,
        role=role,
        permissions=list(ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS["viewer"])),
        is_platform_user=role == "platform_super_admin",
    )


async def _client_as(role: str):
    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    state = AppState(
        config=config, engine=engine, sessionmaker=make_sessionmaker(engine),
    )
    app = create_app(state)

    async def _fake():
        return _user_for_role(role)

    app.dependency_overrides[get_current_user] = _fake
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    )
    return client, engine


class TestRoleRouteBehavior:
    """Spec §4: operator approves, viewer views, auditor reads audit."""

    @pytest.mark.parametrize("role,path,expected", [
        # fleet.view reads — every tenant role may see the fleet
        ("operator", "/api/fleet/", 200),
        ("viewer", "/api/fleet/", 200),
        ("auditor", "/api/fleet/", 200),
        ("site_admin", "/api/fleet/", 200),
        # action.approve — operator and above (R-C4). A13/E0.3: the
        # AUDITOR reads it too, because approval history is the evidence
        # R-C3 promises and "read-only everything" is the ratified
        # auditor scope. A viewer still cannot: they hold neither
        # action.approve nor audit.view.
        ("operator", "/api/approvals/", 200),
        ("site_admin", "/api/approvals/", 200),
        ("auditor", "/api/approvals/", 200),
        ("viewer", "/api/approvals/", 403),
        ("auditor", "/api/approvals/history", 200),
        ("viewer", "/api/approvals/history", 403),
        # A13/E0.3: approval POSTURE (who must approve, how many) reads at
        # fleet.view, the same read-split S1 applied to autonomy budgets.
        # Every mutation stays at site.manage — asserted below.
        ("operator", "/api/policies/", 200),
        ("viewer", "/api/policies/", 200),
        ("auditor", "/api/policies/", 200),
        ("site_admin", "/api/policies/", 200),
        ("auditor", "/api/policies/groups", 200),
        # S1 (D2): autonomy POSTURE is readable by every tenant role —
        # the trust ladder must be visible to the people under it.
        ("operator", "/api/policies/autonomy", 200),
        ("viewer", "/api/policies/autonomy", 200),
        ("auditor", "/api/policies/autonomy", 200),
        ("operator", "/api/policies/stop-switch", 200),
        ("viewer", "/api/policies/stop-switch", 200),
        # audit.view — auditor and admins only
        ("auditor", "/api/audit/", 200),
        ("operator", "/api/audit/", 403),
        ("viewer", "/api/audit/", 403),
    ])
    async def test_get_per_role(self, role, path, expected):
        client, engine = await _client_as(role)
        try:
            assert (await client.get(path)).status_code == expected
        finally:
            await client.aclose()
            await engine.dispose()

    @pytest.mark.parametrize("role,expected", [
        # D2's hard boundary: posture READS opened up, mutations did not.
        ("operator", 403),
        ("viewer", 403),
        ("auditor", 403),
        ("site_admin", 200),
    ])
    async def test_autonomy_mutation_stays_site_manage(self, role, expected):
        client, engine = await _client_as(role)
        try:
            resp = await client.post(
                "/api/policies/autonomy",
                json={"device_type": "*", "level": 1,
                      "budget_limit": 5, "budget_period": "daily"},
            )
            assert resp.status_code == expected
            resp = await client.post("/api/policies/stop-switch")
            assert resp.status_code == expected
        finally:
            await client.aclose()
            await engine.dispose()

    @pytest.mark.parametrize("role,expected", [
        # A13/E0.3: opening the POLICY read must not open the write. An
        # auditor who can read who approves must still be unable to
        # change it, and an operator who works the queue must be unable
        # to rewrite the rule that governs the queue.
        ("operator", 403),
        ("viewer", 403),
        ("auditor", 403),
        ("site_admin", 200),
    ])
    async def test_policy_mutation_stays_site_manage(self, role, expected):
        client, engine = await _client_as(role)
        try:
            resp = await client.post(
                "/api/policies/", json={"name": "x", "required_approvers": 2},
            )
            assert resp.status_code == expected
            resp = await client.post(
                "/api/policies/groups", json={"name": "g"},
            )
            assert resp.status_code == expected
        finally:
            await client.aclose()
            await engine.dispose()

    @pytest.mark.parametrize("role,expected", [
        # A13/E0.3: an auditor reads approval evidence and decides nothing.
        ("auditor", 403),
        ("viewer", 403),
        ("operator", 404),   # permitted; the action simply does not exist
    ])
    async def test_approval_decision_stays_action_approve(self, role, expected):
        client, engine = await _client_as(role)
        try:
            resp = await client.post("/api/approvals/no-such-action/approve")
            assert resp.status_code == expected
        finally:
            await client.aclose()
            await engine.dispose()

    @pytest.mark.parametrize("role,expected", [
        # Write-grade imports were declared fleet.view before real grants
        # existed; they are site.manage now (C1 follow-on).
        ("operator", 403),
        ("viewer", 403),
        ("auditor", 403),
        ("site_admin", 200),
    ])
    async def test_warranty_import_is_write_grade(self, role, expected):
        client, engine = await _client_as(role)
        try:
            resp = await client.post("/api/warranty/import", json={"records": []})
            assert resp.status_code == expected
            resp = await client.post(
                "/api/firmware/cve-feed", json={"entries": []}
            )
            assert resp.status_code == expected
        finally:
            await client.aclose()
            await engine.dispose()


class TestConsoleParity:
    """CC's grants for shared tenant roles equal the Console's, restricted
    to nothing — full set equality, so a permission added on one side must
    be added (or consciously diverged) on the other."""

    @pytest.mark.parametrize("role", [
        "tenant_owner", "site_admin", "operator", "auditor", "viewer",
    ])
    def test_role_matches_console(self, role):
        from harkeniq_console.permissions import (
            ROLE_PERMISSIONS as CONSOLE_ROLE_PERMISSIONS,
        )

        assert set(ROLE_PERMISSIONS[role]) == set(
            CONSOLE_ROLE_PERMISSIONS[role]
        ), f"CC and Console grants diverge for {role}"

    def test_platform_support_gets_no_cc_grants(self):
        """A12.1: vendor staff have no live L3 access by default. The role
        is absent from CC's map; pick_role would default it to viewer, so
        it must never appear here with staff-grade grants."""
        assert "platform_support" not in ROLE_PERMISSIONS
