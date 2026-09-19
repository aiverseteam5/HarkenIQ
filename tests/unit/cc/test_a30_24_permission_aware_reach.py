"""A6-4B0b-S2 (spec A30.24): a read takes its reach from the grants that
carry the permission the read requires -- and from no other grant.

P1, reproduced before this slice: `ResolvedScope.site_ids`, `tenant_wide`
and the permission-less `covers_*` helpers are built from EVERY effective
grant, whatever its `permission_subset` carries, and a read route took its
PERMISSION from the role. So grant A (site-1, full role) plus grant B
(site-3, narrowed to `incident.view`) made site-3's fleet, approvals,
outcomes and audit readable.

    READABLE(P, r) = exists effective grant G: P in G.permissions
                                               and G covers r

What is proven here, and how:

* **The algebra** -- a generated matrix over every permission, every
  grant mix the spec names and every kind of target asserts that
  `read_reach(scope, P)` equals `scope.permits(P, target)`. There is no
  second implementation of coverage for them to disagree about.
* **The behaviour** -- the production stack, the production `get_scope`,
  persisted grants, STRICT. Every case is paired with a control in which
  the SAME grant is left un-narrowed, so a route that returned nothing
  for an unrelated reason cannot pass as a fix.
* **The boundary** -- a repository filter refuses a bare `ResolvedScope`,
  and a source-level guard fails when an API module reads the
  permission-neutral projection or filters on a permission its own route
  guard does not accept.
* **What did NOT change** -- a principal whose grants all carry the full
  role reads through exactly the sets it read through before, and F1
  (device / device_class under-reach) is still open: general B0b's.
"""

from __future__ import annotations

import ast
import itertools
import pathlib
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import select

import harkeniq_cc.api as api_pkg
from harkeniq_cc.auth import ROLE_PERMISSIONS
from harkeniq_cc.db.models import (
    CCApprovalRoute, CCCandidateSkill, CCFleetCache, CCOutcomeHistory,
    CCScopeGrant,
)
from harkeniq_cc.db import repos as repos_module
from harkeniq_cc.db.repos import (
    AuditRepo, CampaignRepo, FleetCacheRepo, IncidentRepo, SiteRepo,
    require_read_reach, scope_sites,
)
from harkeniq_cc.scope import (
    SCOPE_DEVICE, SCOPE_DEVICE_CLASS, SCOPE_ORG_UNIT, SCOPE_SITE,
    SCOPE_TENANT, ReadReach, ScopeError, SCOPE_ONLY_MARKER, empty_scope,
    read_reach, resolve, where_reach,
)

from tests.unit.cc.test_e1_persona_matrix import (
    TENANT, _client, _grant, _stack, _strict,
)

VOCABULARY = sorted({p for perms in ROLE_PERMISSIONS.values() for p in perms} - {"*"})
OWNER = list(ROLE_PERMISSIONS["tenant_owner"])


# ---------------------------------------------------------------------------
# A small pure estate for the algebra
# ---------------------------------------------------------------------------

UNITS = [
    NS(id="root", path="/root/"),
    NS(id="ra", path="/root/ra/"),
    NS(id="a1", path="/root/ra/a1/"),
    NS(id="rb", path="/root/rb/"),
]
SITES = [
    NS(id="site-a", org_unit_id="a1"),
    NS(id="site-b", org_unit_id="a1"),
    NS(id="site-c", org_unit_id="rb"),
]
DEVICES = [
    ("dev-a", "site-a", "server"), ("dev-b", "site-b", "switch"),
    ("dev-c", "site-c", "server"), ("dev-x", "site-c", ""),
]


def _row(scope_type, ref="", subset=None, **kw):
    base = dict(
        scope_type=scope_type, scope_ref=ref, permission_subset=subset,
        revoked_at=None, expires_at=None, role=None, realm="",
    )
    base.update(kw)
    return NS(**base)


def _resolve(rows, perms=None, principal_type="user", enforcement="strict"):
    return resolve(
        tenant_id=TENANT, principal_type=principal_type, principal_ref="p",
        role_permissions=list(OWNER if perms is None else perms),
        grant_rows=rows, org_units=UNITS, sites=SITES, enforcement=enforcement,
    )


PAST = datetime.now(timezone.utc) - timedelta(days=1)

#: Every mix the spec names (A30.24 / boundary section 9).
MIXES = {
    "tenant + narrow site": [
        _row(SCOPE_TENANT, subset=["incident.view"]),
        _row(SCOPE_SITE, "site-a"),
    ],
    "org + narrow site": [
        _row(SCOPE_ORG_UNIT, "a1", subset=["fleet.view"]),
        _row(SCOPE_SITE, "site-c", subset=["incident.view", "audit.view"]),
    ],
    "site + device": [
        _row(SCOPE_SITE, "site-a"),
        _row(SCOPE_DEVICE, "dev-c", subset=["incident.view"]),
    ],
    "site + device_class": [
        _row(SCOPE_SITE, "site-a", subset=["fleet.view"]),
        _row(SCOPE_DEVICE_CLASS, "switch", subset=["action.approve"]),
    ],
    "two sites, different subsets": [
        _row(SCOPE_SITE, "site-a"),
        _row(SCOPE_SITE, "site-b", subset=["incident.view"]),
    ],
    "expired": [
        _row(SCOPE_SITE, "site-a", subset=["incident.view"]),
        _row(SCOPE_SITE, "site-b", expires_at=PAST),
    ],
    "revoked": [
        _row(SCOPE_SITE, "site-a", subset=["incident.view"]),
        _row(SCOPE_TENANT, revoked_at=PAST),
    ],
    "inert": [
        _row(SCOPE_SITE, "site-gone"),
        _row(SCOPE_ORG_UNIT, "unit-gone"),
        _row(SCOPE_SITE, "site-a", subset=["fleet.view"]),
    ],
    "empty subset": [
        _row(SCOPE_TENANT, subset=[]),
        _row(SCOPE_SITE, "site-a", subset=["fleet.view"]),
    ],
    "subset names a permission the role lacks": [
        _row(SCOPE_SITE, "site-a", subset=["no.such.permission"]),
    ],
    "full grants only": [
        _row(SCOPE_ORG_UNIT, "a1"), _row(SCOPE_SITE, "site-c"),
    ],
}


class TestReachIsPermitsProjected:
    """`read_reach(scope, P)` IS `permits(P, target)`, for every target."""

    @pytest.mark.parametrize("mix", sorted(MIXES))
    def test_every_permission_every_target(self, mix):
        scope = _resolve(MIXES[mix])
        for permission in VOCABULARY:
            reach = read_reach(scope, permission)
            for site in SITES:
                assert reach.covers_site(site.id) == scope.permits(
                    permission, site_id=site.id
                ), (mix, permission, site.id)
                # The SET and the predicate are one answer.
                assert (site.id in reach.site_ids or reach.tenant_wide) == \
                    reach.covers_site(site.id), (mix, permission, site.id)
            for unit in UNITS:
                assert reach.covers_org_unit(unit.path) == scope.permits(
                    permission, org_unit_path=unit.path
                ), (mix, permission, unit.id)
            for agent_id, site_id, device_class in DEVICES:
                assert reach.covers_device(agent_id, site_id, device_class) == \
                    scope.permits(
                        permission, site_id=site_id, device_agent_id=agent_id,
                        device_class=device_class,
                    ), (mix, permission, agent_id)
            assert reach.tenant_wide == scope.permits(
                permission, tenant_object=True
            ), (mix, permission)

    @pytest.mark.parametrize("mix", sorted(MIXES))
    def test_any_of_is_the_union_of_each(self, mix):
        scope = _resolve(MIXES[mix])
        for a, b in itertools.combinations(
            ["fleet.view", "incident.view", "action.approve", "audit.view"], 2
        ):
            both, ra, rb = read_reach(scope, a, b), read_reach(scope, a), read_reach(scope, b)
            assert both.site_ids == ra.site_ids | rb.site_ids
            assert both.tenant_wide == (ra.tenant_wide or rb.tenant_wide)
            assert both.device_ids == ra.device_ids | rb.device_ids
            assert both.device_classes == ra.device_classes | rb.device_classes

    def test_the_matrix_is_not_vacuous(self):
        """The old projection and the new one really do differ here."""
        scope = _resolve(MIXES["two sites, different subsets"])
        assert scope.site_ids == {"site-a", "site-b"}, "P1's precondition"
        assert scope.covers_site("site-b") is True
        assert read_reach(scope, "incident.view").site_ids == {"site-a", "site-b"}
        assert read_reach(scope, "fleet.view").site_ids == {"site-a"}
        assert read_reach(scope, "action.approve").site_ids == {"site-a"}
        assert read_reach(scope, "audit.view").site_ids == {"site-a"}

    def test_a_narrowed_tenant_grant_is_not_tenant_wide_for_other_reads(self):
        """The `tenant_wide` shortcut was P1 at full strength."""
        scope = _resolve(MIXES["tenant + narrow site"])
        assert scope.tenant_wide is True, "P1's precondition"
        incident = read_reach(scope, "incident.view")
        assert incident.tenant_wide and incident.site_ids == {"site-a", "site-b", "site-c"}
        fleet = read_reach(scope, "fleet.view")
        assert not fleet.tenant_wide and fleet.site_ids == {"site-a"}

    def test_device_and_class_grants_carry_their_own_permission(self):
        scope = _resolve(MIXES["site + device"])
        assert read_reach(scope, "incident.view").device_ids == {"dev-c"}
        assert read_reach(scope, "fleet.view").device_ids == frozenset()
        assert not read_reach(scope, "fleet.view").covers_device("dev-c", "site-c", "server")
        scope = _resolve(MIXES["site + device_class"])
        assert read_reach(scope, "action.approve").device_classes == {"switch"}
        assert read_reach(scope, "fleet.view").device_classes == frozenset()
        assert not read_reach(scope, "fleet.view").covers_device("dev-b", "site-b", "switch")

    def test_the_role_is_not_a_substitute_for_the_grant(self):
        """Role holds P, the only grant does not: nothing is readable."""
        scope = _resolve([_row(SCOPE_TENANT, subset=["incident.view"])])
        assert "fleet.view" in OWNER
        reach = read_reach(scope, "fleet.view")
        assert reach.is_empty() and not reach.tenant_wide and not reach.site_ids

    def test_the_grant_is_not_a_substitute_for_the_role(self):
        """Grant names P, the role does not carry it: `role & subset`."""
        scope = _resolve(
            [_row(SCOPE_TENANT, subset=["site.manage"])],
            perms=ROLE_PERMISSIONS["viewer"],
        )
        assert "site.manage" not in ROLE_PERMISSIONS["viewer"]
        assert read_reach(scope, "site.manage").is_empty()

    def test_lifecycle_and_inertness_never_contribute(self):
        for mix, absent in (
            ("expired", "site-b"), ("revoked", "site-c"), ("inert", "site-gone"),
        ):
            scope = _resolve(MIXES[mix])
            for permission in VOCABULARY:
                reach = read_reach(scope, permission)
                assert absent not in reach.site_ids, (mix, permission)
                assert not reach.tenant_wide, (mix, permission)

    def test_legacy_synthesis_still_reads_tenant_wide(self):
        """A23.10 is untouched: the never-granted keep the role's reach."""
        scope = _resolve([], perms=ROLE_PERMISSIONS["operator"],
                         enforcement="legacy_open")
        assert read_reach(scope, "fleet.view").tenant_wide
        assert not read_reach(scope, "role.manage").tenant_wide

    def test_wildcard_counts_exactly_as_in_permits(self):
        scope = _resolve([_row(SCOPE_SITE, "site-a")], perms=["*"])
        assert read_reach(scope, "fleet.view").site_ids == {"site-a"}
        assert scope.permits("fleet.view", site_id="site-a")

    def test_context_is_never_an_input(self):
        """An org grant's ancestors are breadcrumbs, not reach."""
        scope = _resolve([_row(SCOPE_ORG_UNIT, "a1")])
        assert scope.contextual_unit_ids, "the fixture must have context"
        reach = read_reach(scope, "fleet.view")
        assert not reach.covers_org_unit("/root/ra/")
        assert not reach.covers_org_unit("/root/")
        assert reach.site_ids == {"site-a", "site-b"}


class TestSiteIdsKeepsItsMeaning:
    """`ResolvedScope.site_ids` is unchanged, and full grants read as before."""

    def test_the_neutral_projection_is_untouched(self):
        scope = _resolve(MIXES["two sites, different subsets"])
        assert scope.site_ids == {"site-a", "site-b"}
        assert scope.tenant_wide is False

    @pytest.mark.parametrize("role", ["tenant_owner", "site_admin", "operator", "auditor", "viewer"])
    @pytest.mark.parametrize("rows", [
        [_row(SCOPE_TENANT)], [_row(SCOPE_ORG_UNIT, "ra")],
        [_row(SCOPE_ORG_UNIT, "a1"), _row(SCOPE_SITE, "site-c")],
        [_row(SCOPE_SITE, "site-a")],
    ], ids=["tenant", "org", "org+site", "site"])
    def test_full_grant_personas_read_through_identical_sets(self, role, rows):
        """The human regression, as arithmetic: for every permission the
        role holds, the reach IS the old projection. Only a grant that
        withholds a permission can read differently."""
        scope = _resolve(rows, perms=ROLE_PERMISSIONS[role])
        for permission in ROLE_PERMISSIONS[role]:
            reach = read_reach(scope, permission)
            assert reach.site_ids == scope.site_ids, (role, permission)
            assert reach.tenant_wide == scope.tenant_wide, (role, permission)
            assert reach.org_unit_paths == scope.org_unit_paths

    def test_a_device_grant_never_becomes_site_reach(self):
        """F1 was closed by A30.25 WITHOUT touching this. A device grant
        reaches its device, contributes no site to `site_ids` -- neutral
        or permission-aware -- and the SITE filter stays false for it. The
        device is read through the device-aware predicate instead, which
        is what keeps context from ever becoming authority."""
        scope = _resolve([_row(SCOPE_DEVICE, "dev-a")])
        reach = read_reach(scope, "fleet.view")
        assert scope.site_ids == frozenset() and reach.site_ids == frozenset()
        assert reach.device_ids == {"dev-a"}
        assert str(scope_sites(CCFleetCache.site_id, reach)) == "false"
        assert not scope.covers_site("site-a") and not reach.covers_site("site-a")


class TestTheTwoConstructorsPartitionTheScopes:
    def _where_only(self, rows):
        return _resolve(rows, perms=[SCOPE_ONLY_MARKER], principal_type="agent")

    def test_read_reach_refuses_a_where_only_scope(self):
        with pytest.raises(ScopeError):
            read_reach(self._where_only([_row(SCOPE_SITE, "site-a")]), "fleet.view")

    def test_where_reach_refuses_a_principals_own_scope(self):
        with pytest.raises(ScopeError):
            where_reach(_resolve([_row(SCOPE_TENANT)]))

    def test_where_reach_is_the_agents_operational_reach(self):
        scope = self._where_only([
            _row(SCOPE_SITE, "site-a"), _row(SCOPE_SITE, "site-b", expires_at=PAST),
            _row(SCOPE_DEVICE, "dev-c"),
        ])
        reach = where_reach(scope)
        assert reach.site_ids == scope.site_ids == {"site-a"}
        assert reach.device_ids == {"dev-c"}

    def test_a_permission_must_be_named(self):
        scope = _resolve([_row(SCOPE_TENANT)])
        for bad in ((), ("",), (SCOPE_ONLY_MARKER,)):
            with pytest.raises(ScopeError):
                read_reach(scope, *bad)


# ---------------------------------------------------------------------------
# The repository boundary
# ---------------------------------------------------------------------------


class TestARepositoryRefusesABareScope:
    def test_the_guard(self):
        scope = _resolve([_row(SCOPE_TENANT)])
        assert require_read_reach(None) is None
        reach = read_reach(scope, "fleet.view")
        assert require_read_reach(reach) is reach
        for bare in (scope, empty_scope(TENANT), NS(tenant_wide=True, site_ids=set())):
            with pytest.raises(TypeError, match="A30.24"):
                require_read_reach(bare)

    async def test_every_scoped_repository_read_refuses_it(self):
        _app, sessionmaker, _estate = await _stack()
        bare = _resolve([_row(SCOPE_TENANT)])
        async with sessionmaker() as session:
            calls = [
                lambda: SiteRepo(session).list_all(TENANT, scope=bare),
                lambda: FleetCacheRepo(session).list_all(TENANT, scope=bare),
                lambda: FleetCacheRepo(session).count_total(TENANT, scope=bare),
                lambda: FleetCacheRepo(session).count_by_health(TENANT, scope=bare),
                lambda: IncidentRepo(session).list_incidents(TENANT, scope=bare),
                lambda: AuditRepo(session).list_filtered(tenant_id=TENANT, scope=bare),
                lambda: AuditRepo(session).count_filtered(tenant_id=TENANT, scope=bare),
                lambda: CampaignRepo(session).list_all(TENANT, scope=bare),
            ]
            for call in calls:
                with pytest.raises(TypeError, match="A30.24"):
                    await call()

    def test_no_repository_filter_reads_the_projection_by_getattr(self):
        """The duck-typed read is what let a bare scope through."""
        source = pathlib.Path(repos_module.__file__).read_text()
        assert not re.search(r'getattr\(\s*scope\s*,\s*"(tenant_wide|site_ids)"', source)


# ---------------------------------------------------------------------------
# Behaviour: production stack, production get_scope, persisted grants, STRICT
# ---------------------------------------------------------------------------

PRINCIPAL = "kc-mixed"


async def _estate_objects(sessionmaker, estate) -> None:
    """Something distinguishable at EVERY site, behind every permission."""
    async with sessionmaker() as session:
        for name, site_id in estate.sites.items():
            device = estate.devices[name]
            session.add(CCApprovalRoute(
                site_id=site_id, action_id=f"pending-{name}",
                action_type="SEL_CLEAR", device_agent_id=device,
            ))
            session.add(CCApprovalRoute(
                site_id=site_id, action_id=f"decided-{name}",
                action_type="SEL_CLEAR", device_agent_id=device,
                decision="approved", decided_by="kc-owner",
                decided_at=datetime.now(timezone.utc),
            ))
            session.add(CCOutcomeHistory(
                site_id=site_id, action_id=f"directive:{name}",
                action_type="SEL_CLEAR", device_agent_id=device,
                vendor="Dell", model=f"model-{name}", outcome="SUCCESS",
            ))
            session.add(CCCandidateSkill(
                skill_id=f"skill-{name}", tenant_id=TENANT, site_id=site_id,
                yaml_text="name: x", source_device=device,
            ))
        await session.commit()


async def _mixed_stack(narrow_subset, narrow_type=SCOPE_SITE, narrow_ref="site-3"):
    """Grant A: site-1, full role. Grant B: `narrow_ref`, `narrow_subset`.

    `narrow_subset=None` is the CONTROL: the same two grants with nothing
    withheld, which is what makes every absence below mean something.
    """
    app, sessionmaker, estate = await _stack()
    await _strict(sessionmaker)
    await _estate_objects(sessionmaker, estate)
    await _grant(sessionmaker, PRINCIPAL, SCOPE_SITE, estate.sites["site-1"],
                 role="tenant_owner")
    ref = {
        SCOPE_SITE: lambda: estate.sites[narrow_ref],
        SCOPE_ORG_UNIT: lambda: estate.units[narrow_ref],
        SCOPE_TENANT: lambda: "",
        SCOPE_DEVICE: lambda: estate.devices[narrow_ref],
        SCOPE_DEVICE_CLASS: lambda: narrow_ref,
    }[narrow_type]()
    await _grant(sessionmaker, PRINCIPAL, narrow_type, ref,
                 role="tenant_owner", subset=narrow_subset)
    return app, sessionmaker, estate


#: route -> (the permission its guard requires, the site-3 marker it shows)
def _reads(estate):
    s3 = estate.sites["site-3"]
    return {
        "/api/fleet/": ("fleet.view", "node-site-3"),
        "/api/agents/": ("fleet.view", "node-site-3"),
        "/api/predictive/risk": ("fleet.view", "node-site-3"),
        "/api/attention/": ("fleet.view", "node-site-3"),
        "/api/sites/": ("fleet.view", s3),
        f"/api/sites/{s3}": ("fleet.view", s3),
        "/api/outcomes/metrics": ("fleet.view", "model-site-3"),
        "/api/learning/candidates": ("fleet.view", "skill-site-3"),
        "/api/incidents/": ("incident.view", "inc-site-3"),
        "/api/incidents/inc-site-3": ("incident.view", "inc-site-3"),
        "/api/approvals/": ("action.approve", "pending-site-3"),
        "/api/approvals/history": ("action.approve", "decided-site-3"),
        "/api/audit/": ("audit.view", s3),
        # The tree shows a site-scoped principal the unit their site
        # hangs from. (The unit DETAIL asks org coverage, which a site
        # grant never has; the org-grant test below covers it.)
        "/api/org-units/": ("site.view", estate.units["b1"]),
    }


async def _sees(app, path, marker) -> bool:
    async with _client(app, "tenant_owner", PRINCIPAL) as c:
        resp = await c.get(path)
    assert resp.status_code in (200, 404), (path, resp.status_code, resp.text[:200])
    return resp.status_code == 200 and marker in resp.text


class TestPermissionAndReachComeFromTheSameGrant:
    async def test_the_control_sees_site_3_everywhere(self):
        """Nothing withheld: every read below shows site-3. If this ever
        fails, the narrowing tests under it prove nothing."""
        app, _sm, estate = await _mixed_stack(None)
        for path, (_p, marker) in _reads(estate).items():
            assert await _sees(app, path, marker), path

    @pytest.mark.parametrize("subset", [
        ["incident.view"], ["fleet.view"], ["audit.view"], ["action.approve"],
        ["site.view"], ["fleet.view", "incident.view"], [],
    ], ids=lambda s: "+".join(s) or "empty")
    async def test_site_3_is_readable_only_under_what_its_grant_carries(self, subset):
        app, _sm, estate = await _mixed_stack(subset)
        for path, (permission, marker) in _reads(estate).items():
            expected = permission in subset
            if path.startswith("/api/approvals"):
                # The guard is `action.approve` OR `audit.view`.
                expected = bool({"action.approve", "audit.view"} & set(subset))
            assert await _sees(app, path, marker) == expected, (subset, path)

    async def test_site_1_is_never_lost(self):
        """Narrowing grant B takes nothing away from grant A."""
        app, _sm, estate = await _mixed_stack(["incident.view"])
        s1 = estate.sites["site-1"]
        for path, marker in (
            ("/api/fleet/", "node-site-1"), ("/api/incidents/", "inc-site-1"),
            ("/api/approvals/", "pending-site-1"), ("/api/audit/", s1),
            ("/api/sites/", s1), ("/api/outcomes/metrics", "model-site-1"),
        ):
            assert await _sees(app, path, marker), path

    async def test_a_narrowed_TENANT_grant_does_not_unfilter_other_reads(self):
        app, _sm, estate = await _mixed_stack(
            ["incident.view"], narrow_type=SCOPE_TENANT, narrow_ref="",
        )
        assert await _sees(app, "/api/incidents/", "inc-site-2")
        assert await _sees(app, "/api/incidents/", "inc-site-3")
        for path, marker in (
            ("/api/fleet/", "node-site-"), ("/api/approvals/", "pending-site-"),
            ("/api/approvals/history", "decided-site-"),
            ("/api/outcomes/metrics", "model-site-"),
            ("/api/learning/candidates", "skill-site-"),
        ):
            assert await _sees(app, path, marker + "1"), path
            for other in ("2", "3"):
                assert not await _sees(app, path, marker + other), (path, other)
        for other in ("site-2", "site-3"):
            assert not await _sees(app, "/api/audit/", estate.sites[other])
            assert not await _sees(app, "/api/sites/", estate.sites[other])

    async def test_a_narrowed_ORG_grant(self):
        app, _sm, estate = await _mixed_stack(
            ["incident.view"], narrow_type=SCOPE_ORG_UNIT, narrow_ref="region_b",
        )
        assert await _sees(app, "/api/incidents/", "inc-site-3")
        assert not await _sees(app, "/api/fleet/", "node-site-3")
        b1, region_b = estate.units["b1"], estate.units["region_b"]
        assert not await _sees(app, "/api/org-units/", b1)
        assert not await _sees(app, "/api/org-units/", region_b)
        assert not await _sees(app, f"/api/org-units/{b1}", b1)
        # The control: the same org grant, nothing withheld.
        app, _sm, estate = await _mixed_stack(
            None, narrow_type=SCOPE_ORG_UNIT, narrow_ref="region_b",
        )
        b1 = estate.units["b1"]
        assert await _sees(app, "/api/fleet/", "node-site-3")
        assert await _sees(app, "/api/org-units/", b1)
        assert await _sees(app, f"/api/org-units/{b1}", b1)

    async def test_the_capability_registry_counts_only_fleet_view_devices(self):
        async def devices_in_view(subset):
            app, _sm, _estate = await _mixed_stack(subset)
            async with _client(app, "tenant_owner", PRINCIPAL) as c:
                return (await c.get("/api/capabilities/")).json()["fleet"]["devices_in_view"]

        assert await devices_in_view(None) == 2
        assert await devices_in_view(["incident.view"]) == 1

    async def test_fleet_summary_counts_only_what_fleet_view_reaches(self):
        app, _sm, _estate = await _mixed_stack(["incident.view"])
        async with _client(app, "tenant_owner", PRINCIPAL) as c:
            narrowed = (await c.get("/api/fleet/summary")).json()
        assert (narrowed["total_nodes"], narrowed["sites_count"]) == (1, 1)
        app, _sm, _estate = await _mixed_stack(None)
        async with _client(app, "tenant_owner", PRINCIPAL) as c:
            control = (await c.get("/api/fleet/summary")).json()
        assert (control["total_nodes"], control["sites_count"]) == (2, 2)

    @pytest.mark.parametrize("narrow_type,ref", [
        (SCOPE_DEVICE, "site-3"), (SCOPE_DEVICE_CLASS, "server"),
    ])
    async def test_a_device_or_class_grant_with_an_unrelated_permission(
        self, narrow_type, ref,
    ):
        """The device detail read was already canonical about COVERAGE
        (`covers_device`) and blind about PERMISSION."""
        results = {}
        for label, subset in (("control", None), ("narrowed", ["incident.view"])):
            app, sm, estate = await _mixed_stack(
                subset, narrow_type=narrow_type, narrow_ref=ref,
            )
            async with sm() as session:
                row_id = (await session.execute(
                    select(CCFleetCache.id).where(
                        CCFleetCache.agent_id == estate.devices["site-3"])
                )).scalar_one()
            async with _client(app, "tenant_owner", PRINCIPAL) as c:
                results[label] = (
                    (await c.get(f"/api/fleet/{row_id}")).status_code,
                    (await c.get(f"/api/agents/{estate.devices['site-3']}")).status_code,
                )
        assert results == {"control": (200, 200), "narrowed": (404, 404)}

    async def test_lifecycle_an_expired_or_revoked_full_grant_reads_nothing(self):
        for shape in ({"expires_at": PAST}, {"revoked_at": PAST}):
            app, sm, estate = await _mixed_stack(["incident.view"])
            async with sm() as session:
                session.add(CCScopeGrant(
                    tenant_id=TENANT, principal_type="user",
                    principal_ref=PRINCIPAL, scope_type=SCOPE_SITE,
                    scope_ref=estate.sites["site-2"], role="tenant_owner",
                    granted_by="test", **shape,
                ))
                await session.commit()
            assert not await _sees(app, "/api/fleet/", "node-site-2"), shape
            assert not await _sees(app, "/api/incidents/", "inc-site-2"), shape

    async def test_tenant_isolation_another_tenants_grant_is_not_reach(self):
        app, sm, estate = await _mixed_stack(["incident.view"])
        async with sm() as session:
            session.add(CCScopeGrant(
                tenant_id="tenant-other", principal_type="user",
                principal_ref=PRINCIPAL, scope_type=SCOPE_TENANT, scope_ref="",
                role="tenant_owner", granted_by="test",
            ))
            await session.commit()
        assert not await _sees(app, "/api/fleet/", "node-site-3")
        assert not await _sees(app, "/api/fleet/", "node-site-2")

    async def test_site_admin_here_viewer_there(self):
        """The REALISTIC face of P1: no explicit subset at all.

        A23-3 made the role a grant RECORDS a ceiling. A person who is
        `site_admin` at site-1 and `viewer` at site-3 carries a
        `site_admin` token, so the route guard admits them to the approval
        queue and the grant list -- and the permission-neutral `site_ids`
        then showed them site-3's. Their site-3 grant carries `fleet.view`
        and `incident.view` and nothing else.
        """
        seen = {}
        for label, role_at_site_3 in (("control", "site_admin"), ("narrowed", "viewer")):
            app, sm, estate = await _stack()
            await _strict(sm)
            await _estate_objects(sm, estate)
            await _grant(sm, PRINCIPAL, SCOPE_SITE, estate.sites["site-1"],
                         role="site_admin")
            await _grant(sm, PRINCIPAL, SCOPE_SITE, estate.sites["site-3"],
                         role=role_at_site_3)
            await _grant(sm, "kc-somebody", SCOPE_SITE, estate.sites["site-3"],
                         role="viewer")
            async with _client(app, "site_admin", PRINCIPAL) as c:
                seen[label] = {
                    "fleet": "node-site-3" in (await c.get("/api/fleet/")).text,
                    "incidents": "inc-site-3" in (await c.get("/api/incidents/")).text,
                    "queue": "pending-site-3" in (await c.get("/api/approvals/")).text,
                    "history": "decided-site-3" in (await c.get("/api/approvals/history")).text,
                    "grants": "kc-somebody" in (await c.get("/api/scope-grants/")).text,
                    "queue@site-1": "pending-site-1" in (await c.get("/api/approvals/")).text,
                }
        everything = dict.fromkeys(seen["control"], True)
        assert seen["control"] == everything, seen["control"]
        assert seen["narrowed"] == {
            **everything, "queue": False, "history": False, "grants": False,
        }, seen["narrowed"]

    async def test_the_grant_list_follows_its_own_guard(self):
        """`user.view` OR `audit.view`: a site narrowed to something else
        does not open that site's authorization map."""
        for subset, expected in ((None, True), (["incident.view"], False),
                                 (["audit.view"], True)):
            app, sm, estate = await _mixed_stack(subset)
            await _grant(sm, "kc-somebody", SCOPE_SITE, estate.sites["site-3"],
                         role="viewer")
            assert await _sees(app, "/api/scope-grants/", "kc-somebody") == expected, subset


# ---------------------------------------------------------------------------
# The durable source-level guard
# ---------------------------------------------------------------------------

API_DIR = pathlib.Path(api_pkg.__file__).parent

#: Sites that may read the permission-NEUTRAL projection off `scope`, each
#: because it DESCRIBES the scope rather than filtering a read with it.
NEUTRAL_READ_ALLOWED = {
    ("scope_grants.py", "my_scope"),     # /api/scope-grants/me: self-description
    ("approvals.py", "_scope_snapshot"),  # L2: the authority an approval recorded
}

NEUTRAL_ATTRS = {
    "site_ids", "tenant_wide", "device_ids", "device_classes", "org_unit_paths",
    "covers_site", "covers_device", "covers_org_unit", "covers_org_unit_id",
}

#: Helpers that derive a reach themselves, and the permission they must use.
HELPER_PERMISSIONS = {
    ("campaigns.py", "_visible_sites"): {"fleet.view"},
    ("operational_agents.py", "_agent_visible"): {"fleet.view"},
    # A30.25: the ONE place the proposal reads derive their reach, so the
    # list narrowing and the receipts cannot use different permissions.
    ("operational_agents.py", "_proposal_reach"): {"fleet.view"},
}


def _functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _guard_permissions(fn, source) -> set[str]:
    """The permissions this route's own guard accepts, from its source."""
    start = (fn.decorator_list[0].lineno if fn.decorator_list else fn.lineno) - 1
    header = "\n".join(source.splitlines()[start:fn.body[0].lineno - 1])
    found = re.search(r"require_(?:any_)?permission\(([^)]*)\)", header)
    return set(re.findall(r'"([^"]+)"', found.group(1))) if found else set()


def _constants(tree) -> dict[str, str]:
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value.value
    return out


class TestNoApiModuleFiltersOnThePermissionNeutralScope:
    def _modules(self):
        for path in sorted(API_DIR.glob("*.py")):
            source = path.read_text()
            yield path.name, source, ast.parse(source)

    def test_nothing_reads_the_neutral_projection_off_scope(self):
        """`scope.site_ids`, `scope.tenant_wide`, `scope.covers_*` -- by
        attribute or by `getattr` -- are how P1 was written. Inside `api/`
        the name `scope` is the resolved scope; a read uses `reach`."""
        offenders = []
        for name, _source, tree in self._modules():
            for fn in _functions(tree):
                if (name, fn.name) in NEUTRAL_READ_ALLOWED:
                    continue
                for node in ast.walk(fn):
                    if isinstance(node, ast.Attribute) and node.attr in NEUTRAL_ATTRS \
                            and isinstance(node.value, ast.Name) \
                            and node.value.id in ("scope", "caller_scope", "creator_scope"):
                        offenders.append(f"{name}:{fn.name}:{node.lineno} .{node.attr}")
                    if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "getattr" \
                            and len(node.args) >= 2 \
                            and isinstance(node.args[0], ast.Name) \
                            and node.args[0].id == "scope" \
                            and isinstance(node.args[1], ast.Constant) \
                            and node.args[1].value in NEUTRAL_ATTRS:
                        offenders.append(f"{name}:{fn.name}:{node.lineno} getattr")
        assert offenders == [], offenders

    def test_the_allow_list_names_real_functions(self):
        present = {
            (name, fn.name) for name, _s, tree in self._modules()
            for fn in _functions(tree)
        }
        assert NEUTRAL_READ_ALLOWED <= present
        assert set(HELPER_PERMISSIONS) <= present

    def test_a_handler_filters_on_the_permission_its_guard_accepts(self):
        """Guarded on `incident.view`, filtered on `fleet.view` would be
        P1 again with a different spelling."""
        checked, problems = 0, []
        for name, source, tree in self._modules():
            constants = _constants(tree)
            for fn in _functions(tree):
                for node in ast.walk(fn):
                    if not (isinstance(node, ast.Call)
                            and getattr(node.func, "id", "") == "read_reach"):
                        continue
                    used = set()
                    for arg in node.args[1:]:
                        if isinstance(arg, ast.Constant):
                            used.add(arg.value)
                        elif isinstance(arg, ast.Name) and arg.id in constants:
                            used.add(constants[arg.id])
                        else:
                            problems.append(f"{name}:{fn.name} non-literal permission")
                    expected = HELPER_PERMISSIONS.get((name, fn.name)) \
                        or _guard_permissions(fn, source)
                    checked += 1
                    if used != expected:
                        problems.append(
                            f"{name}:{fn.name} filters on {sorted(used)}, "
                            f"guard accepts {sorted(expected)}"
                        )
        assert problems == [], problems
        assert checked >= 30, f"only {checked} read_reach call sites were found"

    def test_no_handler_hands_the_bare_scope_to_a_repository(self):
        """Belt to the runtime TypeError's braces: visible at review time."""
        repo_call = re.compile(r"Repo\(session\)\.\w+\([^)]*scope=scope", re.S)
        for name, source, _tree in self._modules():
            assert not repo_call.search(source), name
