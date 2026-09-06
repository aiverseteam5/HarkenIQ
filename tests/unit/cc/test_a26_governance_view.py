"""A26 (A25.13): the governance visibility boundary.

`GET /api/policies/groups/{group_id}` was authorized with `fleet.view`
and returned every approver's email address, role, canonical
`principal_ref` and subject-binding status. `fleet.view` reaches down to
`viewer` and sits inside the A20.3 machine ceiling, so an authenticated
Operational Agent — and any human viewer — could enumerate the tenant's
APPROVERS. Tenant isolation held throughout; the authorization boundary
did not.

Four properties carry this module.

* **Visibility is not authority, in BOTH directions.** `action.approve`
  does not confer `governance.view` (an `operator` may decide a subject
  and may not enumerate the topology), and `governance.view` does not
  confer `action.approve` (an `auditor` may inspect and may not decide).
  Neither is ever inferred from the other.
* **A machine principal cannot hold it.** Not by policy — structurally,
  through the A20.3 intersection, whatever bindings it is given.
* **Posture stays broadly readable; the AUTHOR does not.** The policy
  list is still `fleet.view` (A13/E0.3, S1 D2) and is PROJECTED, not
  filtered: a fleet-only caller is built an operational answer by name,
  never handed the rich object with a field deleted.
* **Fail-closed survives the grant.** A narrowed `permission_subset` that
  does not name `governance.view` does not acquire it because the
  underlying role now carries it.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import httpx
import pytest

from harkeniq_cc.api.deps import get_current_user, get_scope
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import (
    CCApprovalGroup, CCApprovalGroupMember, CCApprovalPolicy,
)
from harkeniq_cc.runtime import AppState

TENANT = "t-gov"
OTHER_TENANT = "t-rival"

APPROVER_EMAIL = "alice@example.com"
APPROVER_SUBJECT = "kc-alice"
GROUP_NAME = "SRE on-call"
SLACK = "#sre-approvals"
POLICY_AUTHOR = "kc-policy-author"

GROUPS = "/api/policies/groups"
POLICIES = "/api/policies/"


# ---------------------------------------------------------------------------
# Stack
# ---------------------------------------------------------------------------


class Stack:
    def __init__(self, app, state):
        self.app, self.state = app, state
        self.sessionmaker = state.sessionmaker
        self.principal: UserContext | None = None
        self.tenant_wide = True
        self.site_ids: set = set()

    def as_role(self, role: str, tenant: str = TENANT):
        """A real persona: exactly the permissions the role actually grants."""
        self.principal = UserContext(
            user_id=f"kc-{role}", email=f"{role}@example.com", tenant_id=tenant,
            role=role, permissions=list(ROLE_PERMISSIONS[role]),
        )
        return self

    def as_permissions(self, permissions, *, role="custom", tenant=TENANT):
        """A principal holding EXACTLY these permissions.

        Used to isolate one permission from the persona that usually
        carries it -- the point of A26.3 is that the two questions are
        independent, which a role-shaped fixture alone cannot show.
        """
        self.principal = UserContext(
            user_id=f"kc-{role}", email=f"{role}@example.com", tenant_id=tenant,
            role=role, permissions=list(permissions),
        )
        return self

    def as_machine(self, agent_id="agent-A", permissions=None):
        """An authenticated Operational Agent, resolved the way A3 does."""
        from harkeniq_cc.machine_identity import machine_permissions

        perms = (
            list(permissions) if permissions is not None
            # The real intersection: every read binding the platform has,
            # so this is the MOST a machine could ever hold.
            else machine_permissions(
                ["attention", "fleet", "incidents", "autonomy", "learning"],
                ["proposals"],
            )
        )
        self.principal = UserContext(
            user_id=agent_id, email=f"op-agent:{agent_id}@v1", tenant_id=TENANT,
            role="", permissions=perms, species="agent", identity_id="id-1",
        )
        return self

    def client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://t",
        )


async def _stack():
    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    state = AppState(
        config=config, engine=engine, sessionmaker=make_sessionmaker(engine),
    )
    app = create_app(state)
    stack = Stack(app, state)

    async def _fake_user():
        assert stack.principal is not None, "the test chose no persona"
        return stack.principal

    async def _fake_scope():
        """A REAL ResolvedScope, from the ONE resolver.

        Deliberately not a stub with a `tenant_wide` attribute: the
        scope-consuming routes call `permits()`, and a hand-rolled object
        that answered it would be a second authorization model living in
        a test. The grant is tenant-wide so scope never masks the
        PERMISSION question this module is about.
        """
        from harkeniq_cc.db.models import CCScopeGrant
        from harkeniq_cc.scope import ENFORCEMENT_STRICT, resolve

        principal = stack.principal
        grant = CCScopeGrant(
            tenant_id=principal.tenant_id, principal_type="user",
            principal_ref=principal.user_id, scope_type="tenant",
            scope_ref="", granted_by="kc-owner",
            permission_subset=list(principal.permissions),
        )
        return resolve(
            tenant_id=principal.tenant_id, principal_type="user",
            principal_ref=principal.user_id,
            role_permissions=list(principal.permissions),
            grant_rows=[grant], enforcement=ENFORCEMENT_STRICT,
        )

    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_scope] = _fake_scope
    return stack


async def _seed(stack, tenant: str = TENANT) -> str:
    """One approval group with a named approver, and one policy."""
    async with stack.sessionmaker() as session:
        group = CCApprovalGroup(
            tenant_id=tenant, name=GROUP_NAME, slack_channel=SLACK,
            github_team="sre", required_count=2, created_by="kc-owner",
        )
        session.add(group)
        await session.flush()
        session.add(CCApprovalGroupMember(
            group_id=group.id, user_email=APPROVER_EMAIL,
            principal_ref=APPROVER_SUBJECT, role="approver",
        ))
        session.add(CCApprovalPolicy(
            tenant_id=tenant, name="Dual auth for firmware",
            required_approvers=2, group_id=group.id,
            created_by=POLICY_AUTHOR,
        ))
        await session.commit()
        return group.id


# ---------------------------------------------------------------------------
# 1. The defect: who may read approval topology
# ---------------------------------------------------------------------------


class TestApprovalTopologyIsNotFleetVisibility:
    @pytest.mark.parametrize("persona", ["viewer", "operator"])
    async def test_a_fleet_view_human_is_refused_both_routes(self, persona):
        stack = await _stack()
        gid = await _seed(stack)
        stack.as_role(persona)
        async with stack.client() as c:
            listing = await c.get(GROUPS)
            detail = await c.get(f"{GROUPS}/{gid}")
        assert listing.status_code == 403, f"{persona} enumerated the groups"
        assert detail.status_code == 403, f"{persona} read the approvers"
        for body in (listing.text, detail.text):
            assert APPROVER_EMAIL not in body
            assert APPROVER_SUBJECT not in body
            assert GROUP_NAME not in body

    async def test_a_machine_principal_is_refused_both_routes(self):
        """The headline. The agent holds the widest machine set possible."""
        stack = await _stack()
        gid = await _seed(stack)
        stack.as_machine()
        assert "fleet.view" in stack.principal.permissions
        async with stack.client() as c:
            listing = await c.get(GROUPS)
            detail = await c.get(f"{GROUPS}/{gid}")
        assert listing.status_code == 403
        assert detail.status_code == 403
        assert APPROVER_EMAIL not in detail.text
        assert APPROVER_SUBJECT not in detail.text

    @pytest.mark.parametrize("persona", ["tenant_owner", "site_admin", "auditor"])
    async def test_a_governance_reader_still_reads_it(self, persona):
        stack = await _stack()
        gid = await _seed(stack)
        stack.as_role(persona)
        async with stack.client() as c:
            listing = await c.get(GROUPS)
            detail = await c.get(f"{GROUPS}/{gid}")
        assert listing.status_code == 200, persona
        assert detail.status_code == 200, persona
        members = detail.json()["members"]
        assert [m["email"] for m in members] == [APPROVER_EMAIL]
        assert members[0]["principal_ref"] == APPROVER_SUBJECT
        assert members[0]["subject_bound"] is True

    async def test_a_group_from_another_tenant_is_not_found(self):
        stack = await _stack()
        foreign = await _seed(stack, tenant=OTHER_TENANT)
        stack.as_role("tenant_owner")
        async with stack.client() as c:
            res = await c.get(f"{GROUPS}/{foreign}")
        assert res.status_code == 404
        assert APPROVER_EMAIL not in res.text


# ---------------------------------------------------------------------------
# 2. A26.3 — the two directions are independent
# ---------------------------------------------------------------------------


class TestVisibilityIsNotAuthority:
    async def test_approve_authority_does_not_confer_topology(self):
        """`action.approve` alone reads no topology (A26.3)."""
        stack = await _stack()
        gid = await _seed(stack)
        stack.as_permissions(["fleet.view", "action.approve", "incident.view"])
        async with stack.client() as c:
            assert (await c.get(GROUPS)).status_code == 403
            assert (await c.get(f"{GROUPS}/{gid}")).status_code == 403

    async def test_the_approver_keeps_the_evidence_they_decide_on(self):
        """Refusing topology must not break approving.

        A decision needs the records for THAT subject, which E0.3 already
        gates on `action.approve` OR `audit.view`. Topology is a different
        question and stays refused.
        """
        stack = await _stack()
        stack.as_role("operator")
        async with stack.client() as c:
            records = await c.get("/api/approvals/act-1/records")
            queue = await c.get("/api/approvals/")
        assert records.status_code != 403, (
            "an approver lost the evidence they decide on"
        )
        assert queue.status_code != 403

    async def test_topology_visibility_does_not_confer_approval(self):
        """`governance.view` alone approves nothing (A26.2)."""
        stack = await _stack()
        gid = await _seed(stack)
        stack.as_permissions(["governance.view"])
        async with stack.client() as c:
            assert (await c.get(f"{GROUPS}/{gid}")).status_code == 200
            approve = await c.post("/api/approvals/act-1/approve", json={})
            deny = await c.post("/api/approvals/act-1/deny", json={})
            queue = await c.get("/api/approvals/")
        assert approve.status_code == 403, "a governance reader approved an action"
        assert deny.status_code == 403
        assert queue.status_code == 403, (
            "governance.view is not an approvals-queue permission"
        )

    async def test_a_governance_reader_mutates_no_governance(self):
        """Read only. Every mutation stays at site.manage (A26.6)."""
        stack = await _stack()
        gid = await _seed(stack)
        stack.as_permissions(["governance.view", "fleet.view"])
        async with stack.client() as c:
            for method, path, payload in (
                ("post", GROUPS, {"name": "new"}),
                ("patch", f"{GROUPS}/{gid}", {"name": "renamed"}),
                ("delete", f"{GROUPS}/{gid}", None),
                ("post", f"{GROUPS}/{gid}/members",
                 {"email": "bob@example.com", "role": "approver"}),
                ("post", POLICIES, {"name": "p"}),
                ("post", "/api/policies/stop-switch", {}),
            ):
                call = getattr(c, method)
                res = await (call(path) if payload is None
                             else call(path, json=payload))
                assert res.status_code == 403, f"{method.upper()} {path}"

    async def test_the_auditor_acquires_no_authority_with_it(self):
        """A13/OQ-24 holds: read-only everything, and nothing else."""
        stack = await _stack()
        gid = await _seed(stack)
        stack.as_role("auditor")
        async with stack.client() as c:
            assert (await c.get(f"{GROUPS}/{gid}")).status_code == 200
            assert (await c.post(GROUPS, json={"name": "x"})).status_code == 403
            assert (await c.post(
                "/api/approvals/act-1/approve", json={})).status_code == 403


# ---------------------------------------------------------------------------
# 3. A26.5 — a machine principal cannot hold it, structurally
# ---------------------------------------------------------------------------


class TestTheMachineCeilingIsUnchanged:
    def test_governance_view_is_not_in_the_ceiling(self):
        from harkeniq_cc.machine_identity import MACHINE_PRINCIPAL_CEILING

        assert MACHINE_PRINCIPAL_CEILING == frozenset({
            "fleet.view", "incident.view", "proposal.submit",
        }), "A26.5: the machine ceiling must not move in this slice"
        assert "governance.view" not in MACHINE_PRINCIPAL_CEILING

    def test_no_binding_maps_to_it(self):
        from harkeniq_cc.machine_identity import (
            INGRESS_BINDING_PERMISSIONS, READ_BINDING_PERMISSIONS,
        )

        for table in (READ_BINDING_PERMISSIONS, INGRESS_BINDING_PERMISSIONS):
            for ref, implied in table.items():
                assert "governance.view" not in implied, ref

    def test_every_binding_at_once_still_cannot_reach_it(self):
        """The intersection, exercised rather than assumed.

        A binding table and a ceiling that were one object would let a
        future binding raise the ceiling. Grant EVERY binding and sweep
        the whole vocabulary.
        """
        from harkeniq_cc.machine_identity import (
            INGRESS_BINDING_PERMISSIONS, READ_BINDING_PERMISSIONS,
            machine_permissions,
        )

        effective = set(machine_permissions(
            list(READ_BINDING_PERMISSIONS), list(INGRESS_BINDING_PERMISSIONS),
        ))
        assert "governance.view" not in effective, effective

    def test_a_machine_holding_it_anyway_is_not_produced_by_the_resolver(self):
        """Belt and braces: if one were forged, the route still answers it.

        This is deliberately NOT a claim that the route checks species --
        it does not, and should not. The guarantee is that the resolver
        can never mint such a principal, which the three tests above pin.
        """
        from harkeniq_cc.machine_identity import machine_permissions

        forged = machine_permissions(["fleet", "incidents"], ["proposals"])
        assert "governance.view" not in forged


# ---------------------------------------------------------------------------
# 4. A26.7 — the policy list is projected, not filtered
# ---------------------------------------------------------------------------


class TestThePolicyListIsProjected:
    async def test_a_fleet_only_caller_gets_posture_without_the_author(self):
        stack = await _stack()
        await _seed(stack)
        stack.as_role("viewer")
        async with stack.client() as c:
            res = await c.get(POLICIES)
        assert res.status_code == 200, "posture must stay broadly readable (D2)"
        policy = res.json()["policies"][0]
        assert policy["required_approvers"] == 2, "posture was lost"
        assert policy["name"] == "Dual auth for firmware"
        assert "created_by" not in policy, (
            "governance identity metadata reached a fleet-only caller"
        )
        assert POLICY_AUTHOR not in res.text

    async def test_a_machine_principal_gets_posture_without_the_author(self):
        stack = await _stack()
        await _seed(stack)
        stack.as_machine()
        async with stack.client() as c:
            res = await c.get(POLICIES)
        assert res.status_code == 200
        assert "created_by" not in res.json()["policies"][0]
        assert POLICY_AUTHOR not in res.text

    @pytest.mark.parametrize("persona", ["tenant_owner", "site_admin", "auditor"])
    async def test_a_governance_reader_gets_the_author(self, persona):
        stack = await _stack()
        await _seed(stack)
        stack.as_role(persona)
        async with stack.client() as c:
            res = await c.get(POLICIES)
        assert res.status_code == 200
        assert res.json()["policies"][0]["created_by"] == POLICY_AUTHOR

    async def test_the_two_projections_differ_by_exactly_one_field(self):
        """The governance answer ADDS; it is not a different object."""
        stack = await _stack()
        await _seed(stack)
        stack.as_role("viewer")
        async with stack.client() as c:
            posture = (await c.get(POLICIES)).json()["policies"][0]
        stack.as_role("auditor")
        async with stack.client() as c:
            governed = (await c.get(POLICIES)).json()["policies"][0]
        assert set(governed) - set(posture) == {"created_by"}
        assert not set(posture) - set(governed)
        for key in posture:
            assert posture[key] == governed[key], key

    def test_the_projection_is_built_positively(self):
        """Structural: neither builder strips a field after the fact.

        A subtractive filter passes every behavioural test above and
        leaks the next field somebody adds upstream -- which is exactly
        how the approval-group detail came to leak.
        """
        from harkeniq_cc.api import policies as mod

        for name in ("_policy_posture_dict", "_policy_dict"):
            code = ast.unparse(ast.parse(textwrap.dedent(
                inspect.getsource(getattr(mod, name)))))
            assert ".pop(" not in code, f"{name} filters by removal"
            assert "del " not in code, f"{name} filters by removal"

    def test_the_posture_projection_names_no_identity_field(self):
        from harkeniq_cc.api import policies as mod

        class _P:
            id = tenant_id = name = device_type = action_type = "x"
            risk_level = approval_mode = status = group_id = "x"
            time_window_json = None
            required_approvers = 1
            created_by = POLICY_AUTHOR
            created_at = updated_at = None

        posture = mod._policy_posture_dict(_P())
        for forbidden in ("created_by", "updated_by", "approvers", "members"):
            assert forbidden not in posture, forbidden
        assert POLICY_AUTHOR not in str(posture)


# ---------------------------------------------------------------------------
# 5. A26.8 — scope and grant lifecycle still fail closed
# ---------------------------------------------------------------------------


class TestGrantLifecycleStillFailsClosed:
    """The resolver decides what a principal effectively holds.

    A26 adds a permission to a ROLE; it must not add reach to a grant
    that was deliberately narrowed, nor revive one that has ended.
    """

    def _resolve(self, grants, *, role_permissions, ref="kc-x"):
        """The REAL resolver, with the REAL A23-3 role ceiling.

        `role_ceiling_for` is what the production loader supplies; a test
        that omitted it would silently skip the very rule A26.8 relies
        on (a subset may never exceed the role it was recorded under).
        """
        from harkeniq_cc.scope import ENFORCEMENT_STRICT, resolve

        return resolve(
            tenant_id=TENANT, principal_type="user", principal_ref=ref,
            role_permissions=role_permissions, grant_rows=grants,
            enforcement=ENFORCEMENT_STRICT,
            role_ceiling_for=lambda row: (
                list(ROLE_PERMISSIONS[row.role]) if row.role else None
            ),
        )

    async def test_a_narrowed_subset_does_not_acquire_it(self):
        """The headline of A26.8, and deliberately fail-closed.

        A grant narrowed to `fleet.view` before A26 does not start
        carrying `governance.view` because `tenant_owner` now does.
        """
        from harkeniq_cc.db.models import CCScopeGrant

        stack = await _stack()
        async with stack.sessionmaker() as session:
            grant = CCScopeGrant(
                tenant_id=TENANT, principal_type="user",
                principal_ref="kc-narrow", scope_type="tenant", scope_ref="",
                granted_by="kc-owner", role="tenant_owner",
                permission_subset=["fleet.view"], realm="tenant-demo",
            )
            session.add(grant)
            await session.commit()
            scope = self._resolve(
                [grant], role_permissions=list(ROLE_PERMISSIONS["tenant_owner"]),
                ref="kc-narrow",
            )
        assert scope.permits("fleet.view")
        assert not scope.permits("governance.view"), (
            "a narrowed grant acquired governance.view from the role"
        )

    async def test_an_expired_or_revoked_grant_confers_nothing(self):
        from datetime import datetime, timedelta, timezone

        from harkeniq_cc.db.models import CCScopeGrant

        past = datetime.now(timezone.utc) - timedelta(days=1)
        for label, kwargs in (
            ("expired", {"expires_at": past}),
            # Revocation is `revoked_at`; there is no status column.
            ("revoked", {"revoked_at": past}),
        ):
            grant = CCScopeGrant(
                tenant_id=TENANT, principal_type="user",
                principal_ref="kc-gone", scope_type="tenant", scope_ref="",
                granted_by="kc-owner", role="tenant_owner",
                permission_subset=["governance.view"], realm="tenant-demo",
                **kwargs,
            )
            scope = self._resolve(
                [grant], role_permissions=list(ROLE_PERMISSIONS["tenant_owner"]),
                ref="kc-gone",
            )
            assert not scope.permits("governance.view"), label

    async def test_a_grant_naming_it_explicitly_still_works(self):
        """Fail-closed must not mean fail-always."""
        from harkeniq_cc.db.models import CCScopeGrant

        grant = CCScopeGrant(
            tenant_id=TENANT, principal_type="user", principal_ref="kc-gov",
            scope_type="tenant", scope_ref="", granted_by="kc-owner",
            role="auditor", permission_subset=["governance.view"],
            realm="tenant-demo",
        )
        scope = self._resolve(
            [grant], role_permissions=list(ROLE_PERMISSIONS["auditor"]),
            ref="kc-gov",
        )
        assert scope.permits("governance.view")

    async def test_the_recorded_role_is_still_a_ceiling(self):
        """A23-3: a subset cannot exceed the role it was granted under."""
        from harkeniq_cc.db.models import CCScopeGrant

        grant = CCScopeGrant(
            tenant_id=TENANT, principal_type="user", principal_ref="kc-op",
            scope_type="tenant", scope_ref="", granted_by="kc-owner",
            role="operator", permission_subset=["governance.view"],
            realm="tenant-demo",
        )
        scope = self._resolve(
            [grant], role_permissions=list(ROLE_PERMISSIONS["operator"]),
            ref="kc-op",
        )
        assert not scope.permits("governance.view"), (
            "a subset named a permission the recorded role never held"
        )


# ---------------------------------------------------------------------------
# 6. Vocabulary, parity and the structural sweep
# ---------------------------------------------------------------------------


class TestTheVocabularyAndTheRoleMap:
    def test_governance_view_is_in_the_canonical_catalogue(self):
        from harkeniq_console.permissions import PERMISSIONS

        assert "governance.view" in PERMISSIONS
        assert len(PERMISSIONS) == 25, "A26 adds exactly one permission"

    @pytest.mark.parametrize("role,expected", [
        ("tenant_owner", True), ("site_admin", True), ("auditor", True),
        ("operator", False), ("viewer", False),
    ])
    def test_the_role_mapping_is_exactly_as_ratified(self, role, expected):
        from harkeniq_console.permissions import (
            ROLE_PERMISSIONS as CONSOLE_ROLES,
        )

        assert ("governance.view" in ROLE_PERMISSIONS[role]) is expected
        assert ("governance.view" in CONSOLE_ROLES[role]) is expected

    def test_platform_support_acquires_nothing(self):
        """A12.1: vendor staff gain no tenant governance visibility."""
        from harkeniq_console.permissions import (
            ROLE_PERMISSIONS as CONSOLE_ROLES,
        )

        assert "governance.view" not in CONSOLE_ROLES["platform_support"]
        assert "platform_support" not in ROLE_PERMISSIONS

    def test_no_tenant_plane_platform_bypass_was_introduced(self):
        """`platform_super_admin` holds the whole catalogue BY DEFINITION.

        That is unchanged by A26 and is not a bypass: Central Command
        pins one realm and refuses a platform-realm token, and the
        Console — where the platform plane lives — exposes no
        approval-group route at all. Asserted rather than assumed.
        """
        import importlib
        import pkgutil

        import harkeniq_console.api as console_api
        from harkeniq_console.permissions import PERMISSIONS
        from harkeniq_console.permissions import (
            ROLE_PERMISSIONS as CONSOLE_ROLES,
        )

        assert CONSOLE_ROLES["platform_super_admin"] == set(PERMISSIONS)
        paths: set[str] = set()
        for mod in pkgutil.iter_modules(console_api.__path__):
            router = getattr(
                importlib.import_module(f"harkeniq_console.api.{mod.name}"),
                "router", None,
            )
            if router is not None:
                paths |= {r.path for r in router.routes}
        assert paths, "the Console router sweep found nothing to check"
        assert not [p for p in paths if "policies/groups" in p], (
            "the platform plane grew an approval-topology route"
        )


class TestNoTopologyRouteRemainsBehindFleetView:
    """Structural: the boundary is a property of the contract, not a habit."""

    def test_no_approval_group_route_is_reachable_with_fleet_view(self):
        from harkeniq_cc.route_contract import ROUTE_CONTRACT

        offenders = [
            (m, p) for (m, p), (perm, _, _) in ROUTE_CONTRACT.items()
            if "policies/groups" in p and perm == "fleet.view"
        ]
        assert not offenders, offenders

    def test_the_two_reads_declare_governance_view(self):
        from harkeniq_cc.route_contract import ROUTE_CONTRACT

        for path in ("/api/policies/groups", "/api/policies/groups/{group_id}"):
            perm, _, audited = ROUTE_CONTRACT[("GET", path)]
            assert perm == "governance.view", path
            # A26.9(2): read auditing does not exist anywhere yet, and
            # this slice does not invent it.
            assert audited is False, path

    def test_the_mutations_did_not_move(self):
        from harkeniq_cc.route_contract import ROUTE_CONTRACT

        for method, path in (
            ("POST", "/api/policies/groups"),
            ("PATCH", "/api/policies/groups/{group_id}"),
            ("DELETE", "/api/policies/groups/{group_id}"),
            ("POST", "/api/policies/groups/{group_id}/members"),
        ):
            perm, _, audited = ROUTE_CONTRACT[(method, path)]
            assert perm == "site.manage", (method, path)
            assert audited is True

    def test_the_ratified_posture_reads_did_not_move(self):
        """A26.6: three reads keep `fleet.view` on ratified grounds."""
        from harkeniq_cc.route_contract import ROUTE_CONTRACT

        for path in ("/api/policies/", "/api/policies/autonomy",
                     "/api/policies/stop-switch"):
            perm, _, _ = ROUTE_CONTRACT[("GET", path)]
            assert perm == "fleet.view", path

    def test_the_approvals_surface_did_not_move(self):
        from harkeniq_cc.route_contract import ROUTE_CONTRACT

        for path in ("/api/approvals/", "/api/approvals/history",
                     "/api/approvals/{action_id}/records"):
            perm, _, _ = ROUTE_CONTRACT[("GET", path)]
            assert perm == "action.approve", path

    def test_there_is_one_permission_predicate(self):
        """No second permission model: the guards ask the one function."""
        from harkeniq_cc.api import deps

        for name in ("require_permission", "require_any_permission"):
            code = ast.unparse(ast.parse(textwrap.dedent(
                inspect.getsource(getattr(deps, name)))))
            assert "has_permission(" in code, name
