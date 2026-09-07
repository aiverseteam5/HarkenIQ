"""A26 (A25.13): the governance visibility boundary.

`GET /api/policies/groups/{group_id}` was authorized with `fleet.view`
and returned every approver's email address, role, canonical
`principal_ref` and subject-binding status. `fleet.view` reaches down to
`viewer` and sits inside the A20.3 machine ceiling, so an authenticated
Operational Agent — and any human viewer — could enumerate the tenant's
APPROVERS.

WHY THIS MODULE DRIVES REAL HTTP WITH PERSISTED GRANTS
------------------------------------------------------
A26's first implementation guarded these routes with
`require_permission("governance.view")` alone, and this module's first
version overrode `get_scope` with a scope SYNTHESISED from the
principal's own permissions. Both halves were wrong in the same
direction, so the tests could not see it. A route guard answers "could
this actor ever hold this permission", and E1.2 says explicitly that it
cannot answer the other question, because `permission_subset` is PER
GRANT.

Driven against the production `get_scope` with rows in
`cc_scope_grants` under STRICT enforcement, the shipped code let a
SITE-scoped `site_admin`, an ORG-scoped `site_admin`, and a tenant-wide
grant whose subset was `["fleet.view"]` all read every approver's email
address.

So this module reuses the persona matrix's production stack. Nothing
here overrides scope. Every ALLOW below had to be granted, and every
DENY is a grant that does not reach.

FOUR PROPERTIES
---------------
* **Eligibility is not authority.** A role permission is a ceiling. The
  resolved grant decides, and revoked/expired/inert/narrowed fail closed.
* **Visibility is not authority, in BOTH directions.** `action.approve`
  does not confer `governance.view` and `governance.view` does not
  confer `action.approve`.
* **A machine principal cannot hold it** — structurally, through the
  A20.3 intersection, whatever bindings or grants it is given.
* **Posture stays broadly readable; the AUTHOR does not.** The policy
  list is PROJECTED, not filtered, and the projection follows effective
  authority rather than role membership.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext
from harkeniq_cc.db.models import (
    CCApprovalGroup, CCApprovalGroupMember, CCApprovalPolicy, CCScopeGrant,
)
from harkeniq_cc.scope import SCOPE_ORG_UNIT, SCOPE_SITE, SCOPE_TENANT
from harkeniq_cc.route_contract import MACHINE_JOBS

from tests.unit.cc.test_e1_persona_matrix import (
    TENANT, _client, _grant, _legacy, _stack, _strict,
)

APPROVER_EMAIL = "alice@example.com"
APPROVER_SUBJECT = "kc-alice"
GROUP_NAME = "SRE on-call"
SLACK = "#sre-approvals"
POLICY_AUTHOR = "kc-policy-author"

GROUPS = "/api/policies/groups"
POLICIES = "/api/policies/"


# ---------------------------------------------------------------------------
# Fixtures: the production stack, plus governance objects to protect
# ---------------------------------------------------------------------------


async def _governance_estate(sessionmaker) -> str:
    """One approval group with a named approver, and one policy."""
    async with sessionmaker() as session:
        group = CCApprovalGroup(
            tenant_id=TENANT, name=GROUP_NAME, slack_channel=SLACK,
            github_team="sre", required_count=2, created_by="kc-owner",
        )
        session.add(group)
        await session.flush()
        session.add(CCApprovalGroupMember(
            group_id=group.id, user_email=APPROVER_EMAIL,
            principal_ref=APPROVER_SUBJECT, role="approver",
        ))
        session.add(CCApprovalPolicy(
            tenant_id=TENANT, name="Dual auth for firmware",
            required_approvers=2, created_by=POLICY_AUTHOR,
        ))
        await session.commit()
        return group.id


async def _lifecycle_grant(sessionmaker, principal, **kwargs) -> None:
    """A grant written directly, so revoked/expired/inert can be shaped.

    `ScopeGrantRepo.grant` deliberately REVIVES a revoked row (A23.10),
    which is exactly what these cases must not do.
    """
    async with sessionmaker() as session:
        session.add(CCScopeGrant(
            tenant_id=TENANT, principal_type="user", principal_ref=principal,
            granted_by="test", **kwargs,
        ))
        await session.commit()


def _machine_client(app, agent_id="agent-A"):
    """An authenticated Operational Agent, resolved as A3 resolves one."""
    from harkeniq_cc.machine_identity import machine_permissions

    async def _fake():
        return UserContext(
            user_id=agent_id, email=f"op-agent:{agent_id}@v1", tenant_id=TENANT,
            role="", species="agent", identity_id="id-1",
                machine_jobs=MACHINE_JOBS,
            # The widest a machine could ever hold: every read binding the
            # platform has, intersected with the A20.3 ceiling.
            permissions=machine_permissions(
                ["attention", "fleet", "incidents", "autonomy", "learning"],
                ["proposals"],
            ),
        )

    app.dependency_overrides[get_current_user] = _fake
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _code_without_prose(obj) -> str:
    """Source with every docstring removed.

    These modules explain the defects they close, so `policies.py` names
    `has_permission(user, "governance.view")` on purpose -- it is the
    thing that went wrong. A structural test that grepped raw source
    would be asserting on the explanation instead of the implementation,
    which is the mistake A25 already made once and replaced with hostile
    input. Judge the CODE.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(obj)))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


class Reading:
    """What one principal actually saw, over HTTP."""

    def __init__(self, listing, detail, policies):
        self.list_code = listing.status_code
        self.groups = (
            listing.json().get("groups", []) if listing.status_code == 200 else []
        )
        self.detail_code = detail.status_code
        self.approver_leaked = (
            APPROVER_EMAIL in detail.text or APPROVER_SUBJECT in detail.text
            or APPROVER_EMAIL in listing.text
        )
        self.policies_code = policies.status_code
        rows = (
            policies.json().get("policies", [])
            if policies.status_code == 200 else []
        )
        self.policy = rows[0] if rows else {}
        self.author_visible = "created_by" in self.policy
        self.posture_visible = "required_approvers" in self.policy

    def assert_denied(self, label: str) -> None:
        """DENY, in the canonical read shape: absent, never refused.

        `test_no_read_is_object_gated` holds that a read narrows rather
        than 403s, because a 403 confirms what it refuses -- and here the
        existence of a group id IS the topology. So the list returns no
        rows and the detail is 404, exactly as a cross-tenant id already
        answers.
        """
        assert self.list_code == 200, f"{label}: list {self.list_code}"
        assert self.groups == [], f"{label}: {len(self.groups)} group(s) leaked"
        assert self.detail_code == 404, f"{label}: detail {self.detail_code}"
        assert not self.approver_leaked, f"{label}: an approver identity leaked"

    def assert_allowed(self, label: str) -> None:
        assert self.list_code == 200, f"{label}: list {self.list_code}"
        assert len(self.groups) == 1, f"{label}: {len(self.groups)} groups"
        assert self.detail_code == 200, f"{label}: detail {self.detail_code}"
        assert self.approver_leaked, f"{label}: the governance reader saw nobody"


async def _read_as(app, role: str, user_id: str, group_id: str) -> Reading:
    async with _client(app, role, user_id) as c:
        return Reading(
            await c.get(GROUPS),
            await c.get(f"{GROUPS}/{group_id}"),
            await c.get(POLICIES),
        )


# ---------------------------------------------------------------------------
# 1. The HTTP lifecycle matrix — the authoritative proof
# ---------------------------------------------------------------------------


class TestEffectiveAuthorityDecides:
    """Real handlers, production `get_scope`, persisted grants, STRICT."""

    async def test_active_tenant_wide_grant_allows(self):
        app, sm, _ = await _stack()
        await _strict(sm)
        gid = await _governance_estate(sm)
        await _grant(sm, "kc-t", SCOPE_TENANT, "", role="auditor")
        (await _read_as(app, "auditor", "kc-t", gid)).assert_allowed("tenant-wide")

    async def test_a_site_scoped_grant_denies_tenant_topology(self):
        """THE ratified case: `site_admin`'s ROLE holds it; the grant does not.

        Not a capability reduction -- site-specific governance topology
        would be a correctly scoped projection, and does not exist yet.
        """
        app, sm, estate = await _stack()
        await _strict(sm)
        gid = await _governance_estate(sm)
        await _grant(sm, "kc-s", SCOPE_SITE, estate.sites["site-1"],
                     role="site_admin")
        assert "governance.view" in ROLE_PERMISSIONS["site_admin"], (
            "this case is only meaningful while the ROLE holds it"
        )
        reading = await _read_as(app, "site_admin", "kc-s", gid)
        reading.assert_denied("site-scoped site_admin")
        assert not reading.author_visible

    async def test_an_org_unit_scoped_grant_denies(self):
        app, sm, estate = await _stack()
        await _strict(sm)
        gid = await _governance_estate(sm)
        await _grant(sm, "kc-u", SCOPE_ORG_UNIT, estate.units["a1"],
                     role="site_admin")
        reading = await _read_as(app, "site_admin", "kc-u", gid)
        reading.assert_denied("org-unit-scoped")
        assert not reading.author_visible

    async def test_a_region_wide_org_grant_still_denies(self):
        """Broad is not tenant-wide. A region is still not the tenant."""
        app, sm, estate = await _stack()
        await _strict(sm)
        gid = await _governance_estate(sm)
        await _grant(sm, "kc-r", SCOPE_ORG_UNIT, estate.units["region_a"],
                     role="site_admin")
        (await _read_as(app, "site_admin", "kc-r", gid)).assert_denied("region")

    async def test_a_device_scoped_grant_denies(self):
        app, sm, estate = await _stack()
        await _strict(sm)
        gid = await _governance_estate(sm)
        await _grant(sm, "kc-d", "device", estate.devices["site-1"],
                     role="site_admin")
        (await _read_as(app, "site_admin", "kc-d", gid)).assert_denied("device")

    async def test_a_device_class_scoped_grant_denies(self):
        app, sm, _ = await _stack()
        await _strict(sm)
        gid = await _governance_estate(sm)
        await _grant(sm, "kc-dc", "device_class", "server", role="site_admin")
        (await _read_as(app, "site_admin", "kc-dc", gid)).assert_denied(
            "device_class")

    async def test_a_subset_without_governance_view_denies(self):
        """Tenant-wide COVERAGE, and the permission withheld per grant.

        The case a route guard structurally cannot see: the ROLE holds
        `governance.view`, the grant's `permission_subset` does not, and
        `permits` checks coverage and permission on the SAME grant.
        """
        app, sm, _ = await _stack()
        await _strict(sm)
        gid = await _governance_estate(sm)
        await _grant(sm, "kc-n", SCOPE_TENANT, "", role="tenant_owner",
                     subset=["fleet.view"])
        reading = await _read_as(app, "tenant_owner", "kc-n", gid)
        reading.assert_denied("narrowed subset")
        assert reading.policies_code == 200, "posture must survive the narrowing"
        assert reading.posture_visible
        assert not reading.author_visible

    @pytest.mark.parametrize("case", ["revoked", "expired"])
    async def test_an_ended_grant_denies(self, case):
        app, sm, _ = await _stack()
        await _strict(sm)
        gid = await _governance_estate(sm)
        past = datetime.now(timezone.utc) - timedelta(days=1)
        await _lifecycle_grant(
            sm, f"kc-{case}", scope_type=SCOPE_TENANT, scope_ref="",
            role="auditor",
            **({"revoked_at": past} if case == "revoked" else {"expires_at": past}),
        )
        reading = await _read_as(app, "auditor", f"kc-{case}", gid)
        reading.assert_denied(case)
        assert not reading.author_visible

    async def test_an_inert_grant_denies(self):
        """A23-3: a grant whose target vanished covers nothing."""
        app, sm, _ = await _stack()
        await _strict(sm)
        gid = await _governance_estate(sm)
        await _lifecycle_grant(
            sm, "kc-inert", scope_type=SCOPE_ORG_UNIT,
            scope_ref="unit-that-vanished", role="auditor",
        )
        reading = await _read_as(app, "auditor", "kc-inert", gid)
        reading.assert_denied("inert / vanished target")
        assert not reading.author_visible

    async def test_a_grantless_principal_under_strict_denies(self):
        app, sm, _ = await _stack()
        await _strict(sm)
        gid = await _governance_estate(sm)
        (await _read_as(app, "auditor", "kc-nobody", gid)).assert_denied(
            "no grant at all")

    @pytest.mark.parametrize("role", ["tenant_owner", "site_admin", "auditor"])
    async def test_the_three_granted_roles_keep_it_with_tenant_authority(self, role):
        """A26.4 preserved: the role mapping is unchanged, and it works."""
        app, sm, _ = await _stack()
        await _strict(sm)
        gid = await _governance_estate(sm)
        await _grant(sm, f"kc-{role}", SCOPE_TENANT, "", role=role)
        (await _read_as(app, role, f"kc-{role}", gid)).assert_allowed(role)

    async def test_legacy_open_synthesis_is_unchanged(self):
        """A23.10 is not touched: a never-granted human keeps its posture.

        Asserted rather than assumed -- a fix that silently changed the
        legacy posture would break every deployment mid-upgrade.
        """
        app, sm, _ = await _stack()
        await _legacy(sm)
        gid = await _governance_estate(sm)
        (await _read_as(app, "auditor", "kc-legacy", gid)).assert_allowed(
            "legacy_open synthesis")

    async def test_legacy_open_does_not_rescue_a_narrowed_grant(self):
        """Synthesis is for the NEVER-granted. A real grant still decides."""
        app, sm, estate = await _stack()
        await _legacy(sm)
        gid = await _governance_estate(sm)
        await _grant(sm, "kc-legacy-site", SCOPE_SITE, estate.sites["site-1"],
                     role="site_admin")
        (await _read_as(app, "site_admin", "kc-legacy-site", gid)).assert_denied(
            "legacy_open + site grant")

    async def test_a_group_from_another_tenant_is_not_found(self):
        app, sm, _ = await _stack()
        await _strict(sm)
        await _governance_estate(sm)
        async with sm() as session:
            foreign = CCApprovalGroup(
                tenant_id="t-rival", name="Rival on-call", created_by="x",
            )
            session.add(foreign)
            await session.commit()
            foreign_id = foreign.id
        await _grant(sm, "kc-t", SCOPE_TENANT, "", role="tenant_owner")
        async with _client(app, "tenant_owner", "kc-t") as c:
            res = await c.get(f"{GROUPS}/{foreign_id}")
        assert res.status_code == 404
        assert "Rival on-call" not in res.text


# ---------------------------------------------------------------------------
# 2. A26.3 — visibility and authority are independent, both directions
# ---------------------------------------------------------------------------


class TestVisibilityIsNotAuthority:
    async def test_an_approver_reads_no_topology_and_keeps_its_queue(self):
        """`operator` holds `action.approve` and not `governance.view`."""
        app, sm, estate = await _stack()
        await _strict(sm)
        gid = await _governance_estate(sm)
        await _grant(sm, "kc-op", SCOPE_SITE, estate.sites["site-1"],
                     role="operator")
        async with _client(app, "operator", "kc-op") as c:
            listing = await c.get(GROUPS)
            detail = await c.get(f"{GROUPS}/{gid}")
            queue = await c.get("/api/approvals/")
            records = await c.get("/api/approvals/act-1/records")
            posture = await c.get(POLICIES)
        # Refused at layer 1: the ROLE never holds it, so this is the one
        # shape where 403 is right -- nothing about a group was named.
        assert listing.status_code == 403
        assert detail.status_code == 403
        assert APPROVER_EMAIL not in detail.text
        assert queue.status_code != 403, "an approver lost their queue"
        assert records.status_code != 403, (
            "an approver lost the evidence they decide on"
        )
        assert posture.status_code == 200, "an approver lost posture"

    async def test_a_governance_reader_decides_nothing(self):
        app, sm, _ = await _stack()
        await _strict(sm)
        gid = await _governance_estate(sm)
        await _grant(sm, "kc-aud", SCOPE_TENANT, "", role="auditor")
        async with _client(app, "auditor", "kc-aud") as c:
            assert (await c.get(f"{GROUPS}/{gid}")).status_code == 200
            assert (await c.post(
                "/api/approvals/act-1/approve", json={})).status_code == 403
            assert (await c.post(
                "/api/approvals/act-1/deny", json={})).status_code == 403
            assert (await c.get("/api/approvals/")).status_code != 403, (
                "the auditor reads evidence under audit.view (E0.3)"
            )

    async def test_a_governance_reader_mutates_no_governance(self):
        app, sm, _ = await _stack()
        await _strict(sm)
        gid = await _governance_estate(sm)
        await _grant(sm, "kc-aud", SCOPE_TENANT, "", role="auditor")
        async with _client(app, "auditor", "kc-aud") as c:
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


# ---------------------------------------------------------------------------
# 3. A26.5 — a machine principal cannot hold it, structurally
# ---------------------------------------------------------------------------


class TestTheMachineBoundaryIsUnchanged:
    async def test_a_machine_principal_reads_no_topology(self):
        app, sm, _ = await _stack()
        await _strict(sm)
        gid = await _governance_estate(sm)
        async with _machine_client(app) as c:
            listing = await c.get(GROUPS)
            detail = await c.get(f"{GROUPS}/{gid}")
            posture = await c.get(POLICIES)
        assert listing.status_code == 403
        assert detail.status_code == 403
        assert APPROVER_EMAIL not in detail.text
        # A29.8 (A6-4A) REVERSES the A26-era assumption asserted here.
        # A26 kept the posture reads at `fleet.view` on the ratified ground
        # that posture belongs to THE PEOPLE LIVING UNDER IT -- reasoning
        # about people, into which the machine ceiling swept machine
        # principals as a side effect. B8: a machine receives governance
        # CONCLUSIONS, never governance CONSTRUCTION, and approval policy
        # rules are construction. A6-4B may supply the conclusion.
        assert posture.status_code == 403, (
            "approval policy topology is governance construction (A29.8)"
        )
        assert APPROVER_EMAIL not in posture.text

    async def test_a_machine_with_a_tenant_grant_still_reads_no_topology(self):
        """Even granted tenant-wide, the A20.3 ceiling holds.

        `get_scope` resolves a machine with `role_permissions` = the
        ceiling intersection, so a grant cannot carry what the ceiling
        excludes. Belt and braces over the route guard.
        """
        app, sm, _ = await _stack()
        await _strict(sm)
        gid = await _governance_estate(sm)
        async with sm() as session:
            session.add(CCScopeGrant(
                tenant_id=TENANT, principal_type="agent",
                principal_ref="agent-A", scope_type=SCOPE_TENANT, scope_ref="",
                granted_by="test", role="tenant_owner",
            ))
            await session.commit()
        async with _machine_client(app) as c:
            assert (await c.get(GROUPS)).status_code == 403
            assert (await c.get(f"{GROUPS}/{gid}")).status_code == 403

    def test_governance_view_is_not_in_the_ceiling(self):
        from harkeniq_cc.machine_identity import MACHINE_PRINCIPAL_CEILING

        assert MACHINE_PRINCIPAL_CEILING == frozenset({
            "fleet.view", "incident.view", "proposal.submit",
        }), "A26.5: the machine ceiling must not move in this slice"

    def test_no_binding_maps_to_it(self):
        from harkeniq_cc.machine_identity import (
            INGRESS_BINDING_PERMISSIONS, READ_BINDING_PERMISSIONS,
        )

        for table in (READ_BINDING_PERMISSIONS, INGRESS_BINDING_PERMISSIONS):
            for ref, implied in table.items():
                assert "governance.view" not in implied, ref

    def test_every_binding_at_once_still_cannot_reach_it(self):
        from harkeniq_cc.machine_identity import (
            INGRESS_BINDING_PERMISSIONS, READ_BINDING_PERMISSIONS,
            machine_permissions,
        )

        effective = set(machine_permissions(
            list(READ_BINDING_PERMISSIONS), list(INGRESS_BINDING_PERMISSIONS),
        ))
        assert "governance.view" not in effective, effective


# ---------------------------------------------------------------------------
# 4. A26.7/A26.11 — the policy list is projected by EFFECTIVE authority
# ---------------------------------------------------------------------------


class TestThePolicyProjection:
    async def test_effective_fleet_view_only_sees_no_author(self):
        app, sm, estate = await _stack()
        await _strict(sm)
        await _governance_estate(sm)
        await _grant(sm, "kc-v", SCOPE_SITE, estate.sites["site-1"],
                     role="viewer")
        async with _client(app, "viewer", "kc-v") as c:
            res = await c.get(POLICIES)
        assert res.status_code == 200, "posture must stay broadly readable (D2)"
        policy = res.json()["policies"][0]
        assert policy["required_approvers"] == 2, "posture was lost"
        assert "created_by" not in policy
        assert POLICY_AUTHOR not in res.text

    async def test_effective_tenant_governance_view_sees_the_author(self):
        app, sm, _ = await _stack()
        await _strict(sm)
        await _governance_estate(sm)
        await _grant(sm, "kc-aud", SCOPE_TENANT, "", role="auditor")
        async with _client(app, "auditor", "kc-aud") as c:
            res = await c.get(POLICIES)
        assert res.json()["policies"][0]["created_by"] == POLICY_AUTHOR

    async def test_nominal_role_alone_does_not_reveal_the_author(self):
        """The exact defect: `site_admin`'s ROLE holds `governance.view`."""
        app, sm, estate = await _stack()
        await _strict(sm)
        await _governance_estate(sm)
        await _grant(sm, "kc-s", SCOPE_SITE, estate.sites["site-1"],
                     role="site_admin")
        async with _client(app, "site_admin", "kc-s") as c:
            res = await c.get(POLICIES)
        assert res.status_code == 200
        assert "created_by" not in res.json()["policies"][0]
        assert POLICY_AUTHOR not in res.text

    async def test_the_two_projections_differ_by_exactly_one_field(self):
        app, sm, estate = await _stack()
        await _strict(sm)
        await _governance_estate(sm)
        await _grant(sm, "kc-v", SCOPE_SITE, estate.sites["site-1"],
                     role="viewer")
        await _grant(sm, "kc-aud", SCOPE_TENANT, "", role="auditor")
        async with _client(app, "viewer", "kc-v") as c:
            posture = (await c.get(POLICIES)).json()["policies"][0]
        async with _client(app, "auditor", "kc-aud") as c:
            governed = (await c.get(POLICIES)).json()["policies"][0]
        assert set(governed) - set(posture) == {"created_by"}
        assert not set(posture) - set(governed)
        for key in posture:
            assert posture[key] == governed[key], key

    async def test_the_write_response_still_carries_the_author(self):
        """A `site.manage` caller who just wrote the rule keeps it."""
        app, sm, _ = await _stack()
        await _strict(sm)
        await _grant(sm, "kc-own", SCOPE_TENANT, "", role="tenant_owner")
        async with _client(app, "tenant_owner", "kc-own") as c:
            res = await c.post(POLICIES, json={"name": "p", "required_approvers": 2})
        assert res.status_code == 200, res.text
        assert res.json()["policy"]["created_by"]

    def test_the_projection_is_built_positively(self):
        """Structural: neither builder strips a field after the fact."""
        from harkeniq_cc.api import policies as mod

        for name in ("_policy_posture_dict", "_policy_dict"):
            code = _code_without_prose(getattr(mod, name))
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
# 5. Vocabulary, parity, and the contract describing runtime truth
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
        from harkeniq_console.permissions import (
            ROLE_PERMISSIONS as CONSOLE_ROLES,
        )

        assert "governance.view" not in CONSOLE_ROLES["platform_support"]
        assert "platform_support" not in ROLE_PERMISSIONS

    def test_no_tenant_plane_platform_bypass_was_introduced(self):
        """`platform_super_admin` holds the whole catalogue BY DEFINITION.

        Unchanged by A26, and not a bypass: Central Command pins one realm
        and refuses a platform-realm token, and the Console -- where the
        platform plane lives -- exposes no approval-group route at all.
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
        assert not [p for p in paths if "policies/groups" in p]


class TestTheContractDescribesRuntimeTruth:
    """Supplementary to the HTTP matrix, never a substitute for it."""

    TOPOLOGY = (
        ("GET", "/api/policies/groups"),
        ("GET", "/api/policies/groups/{group_id}"),
    )

    def test_the_topology_reads_declare_governance_view_and_are_scoped(self):
        from harkeniq_cc.route_contract import READ_SCOPED, ROUTE_CONTRACT

        for route in self.TOPOLOGY:
            perm, treatment, audited = ROUTE_CONTRACT[route]
            assert perm == "governance.view", route
            assert treatment == READ_SCOPED, (
                f"{route} declares {treatment}: it consumes the resolved "
                "scope at runtime and must say so"
            )
            # A26.9(2): read auditing does not exist anywhere yet.
            assert audited is False, route

    def test_the_policy_list_is_declared_scoped_too(self):
        from harkeniq_cc.route_contract import READ_SCOPED, ROUTE_CONTRACT

        perm, treatment, _ = ROUTE_CONTRACT[("GET", "/api/policies/")]
        assert perm == "fleet.view", "posture stays broadly readable"
        assert treatment == READ_SCOPED, (
            "its payload narrows by scope; UNSCOPED would be a lie"
        )

    def test_no_approval_group_route_is_reachable_with_fleet_view(self):
        from harkeniq_cc.route_contract import ROUTE_CONTRACT

        offenders = [
            (m, p) for (m, p), (perm, _, _) in ROUTE_CONTRACT.items()
            if "policies/groups" in p and perm == "fleet.view"
        ]
        assert not offenders, offenders

    def test_the_handlers_actually_consume_the_scope(self):
        """The A23.2 census, aimed at exactly these three routes."""
        from harkeniq_cc.route_contract import route_handlers, scope_consumption

        from tests.unit.cc.test_e1_route_contract import _app

        handlers = route_handlers(_app())
        for route in (*self.TOPOLOGY, ("GET", "/api/policies/")):
            found = scope_consumption(handlers[route])
            assert found.declares, f"{route} takes no scope dependency"
            assert found.consumes, f"{route} accepts `scope` and never reads it"

    def test_the_authority_question_has_one_implementation(self):
        """One predicate, asking the canonical resolver. No second RBAC."""
        from harkeniq_cc.api import policies as mod

        code = _code_without_prose(mod._governance_authority)
        assert "permits(" in code and "tenant_object=True" in code
        module = _code_without_prose(mod)
        assert 'has_permission(user, "governance.view")' not in module, (
            "a nominal role check came back"
        )
        # Counted as CALLS, not as text: the definition line contains the
        # same characters, and a substring count would silently pass with
        # one call site missing.
        calls = [
            n for n in ast.walk(ast.parse(module))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "_governance_authority"
        ]
        assert len(calls) == 3, (
            f"{len(calls)} call sites: the three governance decisions "
            "(group list, group detail, policy projection) must all ask "
            "the one predicate"
        )

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
        from harkeniq_cc.route_contract import ROUTE_CONTRACT

        for path in ("/api/policies/autonomy", "/api/policies/stop-switch"):
            perm, _, _ = ROUTE_CONTRACT[("GET", path)]
            assert perm == "fleet.view", path

    def test_the_approvals_surface_did_not_move(self):
        from harkeniq_cc.route_contract import ROUTE_CONTRACT

        for path in ("/api/approvals/", "/api/approvals/history",
                     "/api/approvals/{action_id}/records"):
            perm, _, _ = ROUTE_CONTRACT[("GET", path)]
            assert perm == "action.approve", path

    def test_there_is_one_permission_predicate(self):
        from harkeniq_cc.api import deps

        for name in ("require_permission", "require_any_permission"):
            code = _code_without_prose(getattr(deps, name))
            assert "has_permission(" in code, name
