"""E1.2: the scope resolver, in isolation.

Everything else in the slice stands on this, so its edges are proved
here once, purely. The invariants under test are the ones the product
promises:

* a subset only ever NARROWS a role
* grants do NOT union into broader authority
* ancestors are visible and never authoritative
* revoked and expired grants resolve to nothing
* humans and agents resolve identically
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from harkeniq_cc.scope import (
    ADMIN_PERMISSION,
    ENFORCEMENT_LEGACY_OPEN,
    ENFORCEMENT_STRICT,
    Grant,
    PRINCIPAL_AGENT,
    PRINCIPAL_USER,
    SCOPE_DEVICE,
    SCOPE_DEVICE_CLASS,
    SCOPE_ORG_UNIT,
    SCOPE_SITE,
    SCOPE_TENANT,
    effective_permissions,
    empty_scope,
    is_active,
    preflight_strict,
    resolve,
)

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
TENANT = "t1"

OWNER = ["fleet.view", "site.manage", "site.view", "action.approve", "role.manage"]
ADMIN = ["fleet.view", "site.manage", "site.view", "action.approve"]
OPERATOR = ["fleet.view", "action.approve"]


class _Unit:
    def __init__(self, uid, path, depth, parent=None, name=""):
        self.id, self.path, self.depth = uid, path, depth
        self.parent_id, self.name = parent, name or uid
        self.unit_type, self.sort_order = "region", 0


class _Site:
    def __init__(self, sid, unit_id=None):
        self.id, self.org_unit_id = sid, unit_id


class _Row:
    """A cc_scope_grants row."""

    def __init__(self, scope_type, scope_ref="", subset=None, role="",
                 expires_at=None, revoked_at=None):
        self.scope_type, self.scope_ref = scope_type, scope_ref
        self.permission_subset, self.role = subset, role
        self.expires_at, self.revoked_at = expires_at, revoked_at


# The tenant shape from the acceptance gate.
ROOT = _Unit("root", "/root/", 1, name="tenant-demo")
REGION_A = _Unit("rega", "/root/rega/", 2, "root", "Region A")
REGION_B = _Unit("regb", "/root/regb/", 2, "root", "Region B")
CLUSTER_A1 = _Unit("a1", "/root/rega/a1/", 3, "rega", "Cluster A1")
CLUSTER_A2 = _Unit("a2", "/root/rega/a2/", 3, "rega", "Cluster A2")
CLUSTER_B1 = _Unit("b1", "/root/regb/b1/", 3, "regb", "Cluster B1")
UNITS = [ROOT, REGION_A, REGION_B, CLUSTER_A1, CLUSTER_A2, CLUSTER_B1]

SITE1 = _Site("site-1", "a1")
SITE2 = _Site("site-2", "a1")
SITE3 = _Site("site-3", "b1")
SITES = [SITE1, SITE2, SITE3]


def build(rows, role_permissions=ADMIN, enforcement=ENFORCEMENT_STRICT,
          principal_type=PRINCIPAL_USER, ref="kc-1"):
    return resolve(
        tenant_id=TENANT,
        principal_type=principal_type,
        principal_ref=ref,
        role_permissions=role_permissions,
        grant_rows=rows,
        org_units=UNITS,
        sites=SITES,
        enforcement=enforcement,
        now=NOW,
    )


class TestSubsetNarrowsNeverWidens:
    def test_a_subset_intersects_the_role(self):
        assert effective_permissions(ADMIN, ["site.manage"]) == frozenset(
            {"site.manage"}
        )

    def test_a_subset_naming_what_the_role_lacks_grants_nothing(self):
        # An operator handed role.manage gets nothing: a delegator
        # cannot pass on authority they never held.
        assert effective_permissions(OPERATOR, ["role.manage"]) == frozenset()

    def test_a_partial_overlap_keeps_only_the_overlap(self):
        assert effective_permissions(
            OPERATOR, ["fleet.view", "role.manage", "site.manage"]
        ) == frozenset({"fleet.view"})

    def test_null_means_the_roles_full_set(self):
        assert effective_permissions(ADMIN, None) == frozenset(ADMIN)

    def test_an_empty_list_is_a_real_statement_of_nothing(self):
        # Deliberately different from null: "this grant carries no
        # permissions" is a thing an administrator may want to express.
        assert effective_permissions(ADMIN, []) == frozenset()

    def test_a_wildcard_role_passes_the_subset_through(self):
        assert effective_permissions(["*"], ["site.manage"]) == frozenset(
            {"site.manage"}
        )

    def test_the_widening_attempt_reaches_no_endpoint(self):
        scope = build(
            [_Row(SCOPE_SITE, "site-1", subset=["role.manage"])],
            role_permissions=OPERATOR,
        )
        assert not scope.permits("role.manage", site_id="site-1")
        assert not scope.may_ever("role.manage")


class TestGrantsDoNotUnionIntoAuthority:
    """The central invariant. Two narrow grants must not add up."""

    def _split(self):
        return build([
            _Row(SCOPE_SITE, "site-1", subset=["site.manage", "fleet.view"]),
            _Row(SCOPE_SITE, "site-3", subset=["fleet.view"]),
        ])

    def test_the_permission_applies_only_where_it_was_granted(self):
        scope = self._split()
        assert scope.permits("site.manage", site_id="site-1")
        assert not scope.permits("site.manage", site_id="site-3")

    def test_the_read_permission_applies_to_both(self):
        scope = self._split()
        assert scope.permits("fleet.view", site_id="site-1")
        assert scope.permits("fleet.view", site_id="site-3")

    def test_may_ever_is_a_fail_fast_and_not_a_decision(self):
        scope = self._split()
        # It says yes, because the actor holds it SOMEWHERE...
        assert scope.may_ever("site.manage")
        # ...and the actual decision still says no over site-3.
        assert not scope.permits("site.manage", site_id="site-3")

    def test_coverage_without_the_permission_borrows_nothing(self):
        scope = build([
            _Row(SCOPE_SITE, "site-1", subset=["fleet.view"]),
            _Row(SCOPE_ORG_UNIT, "regb", subset=["site.manage"]),
        ])
        # site-1 is covered, but only by a grant lacking site.manage.
        assert not scope.permits("site.manage", site_id="site-1")
        assert scope.permits("site.manage", site_id="site-3")


class TestScopeCoverage:
    def test_an_org_unit_grant_reaches_the_sites_beneath_it(self):
        scope = build([_Row(SCOPE_ORG_UNIT, "rega")])
        assert scope.site_ids == frozenset({"site-1", "site-2"})
        assert scope.permits("site.manage", site_id="site-1")

    def test_a_sibling_cluster_is_not_reached(self):
        scope = build([_Row(SCOPE_ORG_UNIT, "a1")])
        assert not scope.covers_org_unit(CLUSTER_A2.path)
        assert not scope.permits("site.manage", org_unit_path=CLUSTER_A2.path)

    def test_an_ancestor_is_not_reached_from_below(self):
        scope = build([_Row(SCOPE_ORG_UNIT, "a1")])
        assert not scope.covers_org_unit(REGION_A.path)
        assert not scope.permits("site.manage", org_unit_path=REGION_A.path)

    def test_sites_under_different_ancestors_can_both_be_granted(self):
        # The Site Admin persona: two sites with no common parent below
        # the root. Nothing requires a grant set to be contiguous.
        scope = build([_Row(SCOPE_SITE, "site-1"), _Row(SCOPE_SITE, "site-3")])
        assert scope.site_ids == frozenset({"site-1", "site-3"})
        assert not scope.covers_site("site-2")

    def test_a_tenant_grant_reaches_every_site(self):
        scope = build([_Row(SCOPE_TENANT)])
        assert scope.tenant_wide
        assert scope.site_ids == {"site-1", "site-2", "site-3"}
        assert scope.permits("site.manage", tenant_object=True)

    def test_only_a_tenant_grant_reaches_a_tenant_object(self):
        scope = build([_Row(SCOPE_ORG_UNIT, "rega")])
        assert not scope.permits("site.manage", tenant_object=True)

    def test_device_and_class_scopes_survive_the_merge(self):
        # A0's three scope types must keep working: dropping them would
        # take reach away from every shipped agent.
        scope = build([
            _Row(SCOPE_DEVICE, "node-7"),
            _Row(SCOPE_DEVICE_CLASS, "switch"),
        ])
        assert scope.covers_device("node-7")
        assert scope.covers_device("other", device_class="switch")
        assert not scope.covers_device("other", device_class="server")

    def test_a_device_scope_says_nothing_about_the_whole_site(self):
        scope = build([_Row(SCOPE_DEVICE, "node-7")])
        assert not scope.covers_site("site-1")


class TestAncestorsAreVisibleNotAuthoritative:
    def test_ancestors_are_collected_for_breadcrumbs(self):
        scope = build([_Row(SCOPE_ORG_UNIT, "a1")])
        assert scope.contextual_unit_ids == frozenset({"root", "rega"})

    def test_contextual_ids_never_appear_in_the_authority_fields(self):
        scope = build([_Row(SCOPE_ORG_UNIT, "a1")])
        assert scope.org_unit_paths == frozenset({CLUSTER_A1.path})
        assert not (scope.contextual_unit_ids & {CLUSTER_A1.id})

    def test_no_decision_method_reads_the_contextual_field(self):
        """The structural guarantee behind ratified L3.

        Emptying the authority fields while leaving the ancestors in
        place must produce a scope that decides NOTHING -- proving the
        decision path never consults them.
        """
        scope = build([_Row(SCOPE_ORG_UNIT, "a1")])
        neutered = type(scope)(
            tenant_id=scope.tenant_id,
            principal_type=scope.principal_type,
            principal_ref=scope.principal_ref,
            enforcement=scope.enforcement,
            grants=(),
            contextual_unit_ids=frozenset({"root", "rega", "a1"}),
            site_unit_paths=scope.site_unit_paths,
            unit_paths=scope.unit_paths,
        )
        for path in (ROOT.path, REGION_A.path, CLUSTER_A1.path):
            assert not neutered.permits("site.manage", org_unit_path=path)
            assert not neutered.covers_org_unit(path)
        assert not neutered.covers_site("site-1")
        assert not neutered.permits("site.manage", tenant_object=True)

    def test_seeing_an_ancestor_does_not_permit_mutating_it(self):
        scope = build([_Row(SCOPE_ORG_UNIT, "a1")])
        assert "rega" in scope.contextual_unit_ids
        assert not scope.permits("site.manage", org_unit_path=REGION_A.path)
        assert not scope.covers_org_unit_id("rega")


class TestLifecycle:
    def test_a_revoked_grant_reaches_nothing(self):
        scope = build([_Row(SCOPE_TENANT, revoked_at=NOW - timedelta(days=1))])
        assert scope.is_empty()
        assert not scope.permits("fleet.view", tenant_object=True)

    def test_an_expired_grant_reaches_nothing(self):
        scope = build([_Row(SCOPE_SITE, "site-1", expires_at=NOW - timedelta(hours=1))])
        assert scope.is_empty()

    def test_a_future_expiry_still_counts(self):
        scope = build([_Row(SCOPE_SITE, "site-1", expires_at=NOW + timedelta(days=1))])
        assert scope.covers_site("site-1")

    def test_is_active_handles_a_naive_timestamp(self):
        # sqlite returns naive datetimes for tz-aware writes; treating one
        # as expired (or not) by accident would be an authorization bug.
        naive = _Row(SCOPE_SITE, "s", expires_at=datetime(2027, 1, 1))
        assert is_active(naive, now=NOW)

    def test_a_grant_pointing_at_a_deleted_unit_reaches_nothing(self):
        scope = build([_Row(SCOPE_ORG_UNIT, "gone")])
        assert scope.is_empty()

    def test_an_unknown_scope_type_grants_nothing(self):
        scope = build([_Row("galaxy", "milky-way")])
        assert scope.is_empty()


class TestEnforcementModes:
    def test_legacy_open_gives_an_ungranted_principal_todays_reach(self):
        scope = build([], enforcement=ENFORCEMENT_LEGACY_OPEN)
        assert scope.tenant_wide
        assert scope.permits("site.manage", site_id="site-3")

    def test_strict_gives_an_ungranted_principal_nothing(self):
        scope = build([], enforcement=ENFORCEMENT_STRICT)
        assert scope.is_empty()
        assert not scope.permits("fleet.view", site_id="site-1")

    def test_legacy_open_still_honours_a_grant_when_one_exists(self):
        # A tenant adopts scoping one person at a time.
        scope = build(
            [_Row(SCOPE_ORG_UNIT, "a1")], enforcement=ENFORCEMENT_LEGACY_OPEN
        )
        assert not scope.tenant_wide
        assert not scope.covers_site("site-3")

    def test_the_empty_scope_reaches_nothing(self):
        scope = empty_scope(TENANT)
        assert scope.is_empty()
        assert not scope.permits("fleet.view", tenant_object=True)


class TestHumansAndAgentsResolveIdentically:
    def test_the_same_grant_yields_the_same_reach(self):
        rows = [_Row(SCOPE_ORG_UNIT, "a1")]
        human = build(rows, role_permissions=["*"], principal_type=PRINCIPAL_USER,
                      ref="p")
        agent = build(rows, role_permissions=["*"], principal_type=PRINCIPAL_AGENT,
                      ref="p")
        assert human.site_ids == agent.site_ids
        assert human.org_unit_paths == agent.org_unit_paths
        assert human.contextual_unit_ids == agent.contextual_unit_ids
        assert human.grants == agent.grants
        # Identical but for the label, which is the whole point of one
        # resolver: there is no second scope model for agents.
        assert human.principal_type != agent.principal_type

    def test_an_agent_is_bounded_by_its_rows_like_anyone_else(self):
        agent = build([_Row(SCOPE_SITE, "site-1")], role_permissions=["*"],
                      principal_type=PRINCIPAL_AGENT)
        assert agent.covers_site("site-1")
        assert not agent.covers_site("site-3")


class TestDelegationCeiling:
    def test_a_cluster_manager_cannot_delegate_the_sibling(self):
        creator = build([_Row(SCOPE_ORG_UNIT, "a1")])
        requested = build([_Row(SCOPE_ORG_UNIT, "a2")])
        assert not creator.can_delegate(requested)

    def test_a_cluster_manager_can_delegate_within_the_cluster(self):
        creator = build([_Row(SCOPE_ORG_UNIT, "a1")])
        requested = build([_Row(SCOPE_SITE, "site-1")])
        assert creator.can_delegate(requested)

    def test_nobody_below_tenant_can_delegate_tenant_scope(self):
        creator = build([_Row(SCOPE_ORG_UNIT, "rega")])
        requested = build([_Row(SCOPE_TENANT)])
        assert not creator.can_delegate(requested)

    def test_a_tenant_wide_principal_can_delegate_anything(self):
        creator = build([_Row(SCOPE_TENANT)])
        assert creator.can_delegate(build([_Row(SCOPE_ORG_UNIT, "b1")]))
        assert creator.can_delegate(build([_Row(SCOPE_SITE, "site-3")]))


class TestStrictPreflight:
    def test_a_tenant_with_a_named_admin_may_flip(self):
        rows = [_Row(SCOPE_TENANT, role="tenant_owner")]
        result = preflight_strict(rows, lambda r: OWNER if r.role else [])
        assert result.ok and result.admin_count == 1

    def test_a_tenant_with_no_admin_may_not(self):
        rows = [_Row(SCOPE_ORG_UNIT, "rega", role="tenant_owner")]
        result = preflight_strict(rows, lambda r: OWNER if r.role else [])
        assert not result.ok
        assert ADMIN_PERMISSION in result.reason
        assert "Nothing has been changed" in result.reason

    def test_a_subset_that_removes_role_manage_does_not_count(self):
        rows = [_Row(SCOPE_TENANT, subset=["fleet.view"], role="tenant_owner")]
        result = preflight_strict(rows, lambda r: OWNER)
        assert not result.ok

    def test_a_revoked_admin_does_not_count(self):
        rows = [
            _Row(SCOPE_TENANT, role="tenant_owner",
                 revoked_at=NOW - timedelta(days=1))
        ]
        result = preflight_strict(rows, lambda r: OWNER, now=NOW)
        assert not result.ok

    def test_an_expired_admin_does_not_count(self):
        rows = [
            _Row(SCOPE_TENANT, role="tenant_owner",
                 expires_at=NOW - timedelta(minutes=1))
        ]
        result = preflight_strict(rows, lambda r: OWNER, now=NOW)
        assert not result.ok

    def test_a_grant_with_no_role_named_does_not_count(self):
        # Counting it would let the flip pass on somebody who turns out
        # not to be an administrator -- the exact lockout L1 prevents.
        rows = [_Row(SCOPE_TENANT, role="")]
        result = preflight_strict(rows, lambda r: ROLE_LOOKUP(r))
        assert not result.ok

    def test_the_preflight_is_not_in_the_resolver(self):
        """L1 is an endpoint question, not a resolver special case.

        A resolver that knew about administrators would carry that case
        into every future caller, so `resolve()` must have no idea what
        `role.manage` means.
        """
        import inspect

        from harkeniq_cc import scope as scope_module

        source = inspect.getsource(scope_module.resolve)
        assert ADMIN_PERMISSION not in source
        assert "preflight" not in source


def ROLE_LOOKUP(row):
    from harkeniq_cc.auth import ROLE_PERMISSIONS

    return ROLE_PERMISSIONS.get(getattr(row, "role", "") or "", [])
