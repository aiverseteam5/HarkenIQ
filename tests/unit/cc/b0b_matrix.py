"""The A6-4B0b cross-reader matrix (spec A30.25). A harness, not a test.

One estate, one set of personas, one set of device-bearing readers, and
ONE statement of what each persona should read -- computed by the canonical
Python rule (`ReadReach.covers_owned` over a `FleetIndex`) and therefore
independent of the SQL predicates it is compared against:

    repository visibility  ==  canonical permission-aware coverage

Parameterised by sessionmaker and tenant so the SAME matrix runs on sqlite
(tests/unit/cc/test_a30_25_canonical_reach.py) and on a real PostgreSQL
(tests/integration/test_a30_25_canonical_reach_pg.py), where `lower()`,
the correlated `EXISTS`, JSONB subsets and timestamptz lifecycles are the
engine's own.

The estate is built so every negative control has something to hide:

    site s1 (cluster a1)   node-1 server · sw-1 switch · blank-1 class ""
    site s2 (cluster a1)   node-2 server · sw-2 switch · swup-2 "SWITCH"
    site s3 (cluster b1)   node-3 server

plus rows that name a device which does NOT resolve at the row's site (an
unknown device, and a device that exists at another site), a site-owned
parent incident with no device, and an outcome carrying the non-canonical
identifier the Site Manager's fallback would write (D10).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable, Optional

from harkeniq_cc.auth import ROLE_PERMISSIONS
from harkeniq_cc.db.models import (
    CCAgentProposal, CCApprovalRoute, CCCandidateSkill, CCFleetCache,
    CCIncident, CCLearnedSignal, CCOutcomeHistory, CCSafetyState, CCScopeGrant,
    CCSite,
)
from harkeniq_cc.db.repos import (
    AgentProposalRepo, ApprovalRouteRepo, AuditRepo, FleetCacheRepo,
    IncidentRepo, OrgUnitRepo, OutcomeHistoryRepo, SiteRepo,
)
from harkeniq_cc.governance import load_scope
from harkeniq_cc.learned_signals import cohort_ref
from harkeniq_cc.scope import ReadReach, read_reach
from harkeniq_cc.target_authority import FleetIndex

OWNER = list(ROLE_PERMISSIONS["tenant_owner"])
PAST = datetime.now(timezone.utc) - timedelta(hours=1)

FLEET_VIEW = ("fleet.view",)
INCIDENT_VIEW = ("incident.view",)
APPROVAL_READ = ("action.approve", "audit.view")

#: (key, site, class). `blank-1` carries NO class: R1 says it matches no
#: class grant, and it must still be readable by site and by id.
DEVICES = (
    ("node-1", "s1", "server"), ("sw-1", "s1", "switch"), ("blank-1", "s1", ""),
    ("node-2", "s2", "server"), ("sw-2", "s2", "switch"),
    # The STORED class is upper-case: `lower()` has to work on the column
    # side too, on both engines, exactly as `covers_device` compares.
    ("swup-2", "s2", "SWITCH"),
    ("node-3", "s3", "server"),
)


@dataclass
class Estate:
    tenant: str
    tag: str
    units: dict[str, str] = field(default_factory=dict)
    sites: dict[str, str] = field(default_factory=dict)
    agent_ids: dict[str, str] = field(default_factory=dict)      # key -> agent id
    fleet_row_ids: dict[str, str] = field(default_factory=dict)  # key -> cc_fleet_cache.id
    #: reader family -> [(row key, site key, device agent id)]
    rows: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)
    proposal_ids: dict[str, str] = field(default_factory=dict)
    #: persona -> principal, so a persona's grants are written exactly once.
    principals: dict[str, str] = field(default_factory=dict)

    def dev(self, key: str) -> str:
        return self.agent_ids[key]

    def name(self, stem: str) -> str:
        return f"{stem}-{self.tag}"


async def seed_estate(sessionmaker, tenant: str, tag: Optional[str] = None) -> Estate:
    estate = Estate(tenant=tenant, tag=tag or uuid.uuid4().hex[:8])
    n = estate.name
    async with sessionmaker() as session:
        units = OrgUnitRepo(session)
        root = await units.create(tenant, name=n("root"), unit_type="organization", parent=None)
        region_a = await units.create(tenant, name=n("Region A"), unit_type="region", parent=root)
        region_b = await units.create(tenant, name=n("Region B"), unit_type="region", parent=root)
        a1 = await units.create(tenant, name=n("Cluster A1"), unit_type="cluster", parent=region_a)
        b1 = await units.create(tenant, name=n("Cluster B1"), unit_type="cluster", parent=region_b)
        estate.units = {"root": root.id, "region_a": region_a.id,
                        "region_b": region_b.id, "a1": a1.id, "b1": b1.id}
        for key, unit in (("s1", a1), ("s2", a1), ("s3", b1)):
            site = CCSite(tenant_id=tenant, site_name=n(key), sm_endpoint="sm:50051",
                          sm_token="tok", org_unit_id=unit.id)
            session.add(site)
            await session.flush()
            estate.sites[key] = site.id
            await AuditRepo(session).append(
                actor="seed", action="seed.site", subject=n(f"audit-{key}"),
                tenant_id=tenant, site_id=site.id,
            )

        for key, site_key, device_class in DEVICES:
            agent_id = n(key)
            row = CCFleetCache(
                site_id=estate.sites[site_key], agent_id=agent_id, agent_name=agent_id,
                vendor="Dell", model="R750", health="ok", device_class=device_class,
                service_tag=n(f"TAG-{key}"),
            )
            session.add(row)
            await session.flush()
            estate.agent_ids[key] = agent_id
            estate.fleet_row_ids[key] = row.id
        # Names a device Central Command cannot identify anywhere, and the
        # identifier the SM's outcome fallback would write (D10).
        estate.agent_ids["ghost"] = n("ghost-dev")
        estate.agent_ids["noncanon"] = uuid.uuid4().hex

        s, d = estate.sites, estate.dev

        def incident(key, site_key, device="", parent=None, kind="device", meta=None):
            session.add(CCIncident(
                incident_id=n(key), tenant_id=tenant, site_id=s[site_key],
                device_agent_id=device, status="open", kind=kind, title=key,
                parent_incident_id=n(parent) if parent else None,
                correlation_meta=meta, opened_at=datetime.now(timezone.utc),
            ))
            estate.rows.setdefault("incidents", []).append((n(key), site_key, device))

        # A correlated parent names NO device: it is the site's.
        incident("parent-1", "s1", kind="shared_power",
                 meta={"domain": "PDU-A", "devices": [d("node-1"), d("sw-1")]})
        incident("child-node-1", "s1", d("node-1"), parent="parent-1",
                 meta={"severity": "critical", "onsets": 1})
        # A DEVICE-owned incident whose correlation block names its PEERS.
        incident("child-sw-1", "s1", d("sw-1"), parent="parent-1",
                 kind="network_ambiguity",
                 meta={"votes": {d("node-1"): "ALIVE"}, "assessment": "LINK_DOWN"})
        incident("inc-node-1", "s1", d("node-1"))
        incident("inc-blank-1", "s1", d("blank-1"))
        incident("inc-node-2", "s2", d("node-2"))
        incident("inc-sw-2", "s2", d("sw-2"))
        incident("inc-node-3", "s3", d("node-3"))
        # A child of parent-1 that is NOT at parent-1's site. Whoever reads
        # the parent does not thereby read this: R2 holds for a site
        # principal too, not only for a device one.
        incident("child-far-3", "s3", d("node-3"), parent="parent-1")
        incident("inc-ghost-1", "s1", d("ghost"))   # unknown device
        incident("inc-moved-2", "s2", d("node-1"))  # node-1 is at s1, not s2

        def route(key, site_key, device, decided=False):
            session.add(CCApprovalRoute(
                site_id=s[site_key], action_id=n(key), action_type="SEL_CLEAR",
                device_agent_id=device,
                decision="approved" if decided else None,
                decided_by="kc-owner" if decided else None,
                decided_at=datetime.now(timezone.utc) if decided else None,
            ))
            family = "history" if decided else "pending"
            estate.rows.setdefault(family, []).append((n(key), site_key, device))

        for key, site_key, _cls in DEVICES:
            route(f"act-{key}", site_key, d(key))
            route(f"done-{key}", site_key, d(key), decided=True)
        route("act-ghost-1", "s1", d("ghost"))
        route("act-siteonly-1", "s1", "")            # names no device

        for key, site_key in (("node-1", "s1"), ("sw-1", "s1"), ("node-2", "s2"),
                              ("node-3", "s3"), ("ghost", "s1")):
            proposal = CCAgentProposal(
                tenant_id=tenant, agent_id=n("agent")[:32],
                actor=f"op-agent:{n('agent')[:32]}@v1", agent_version=1,
                site_id=s[site_key], device_agent_id=d(key), action_type="SEL_CLEAR",
                params={}, rationale="b0b", evidence={},
                disposition="requires_approval", status="awaiting_approval",
            )
            session.add(proposal)
            await session.flush()
            estate.proposal_ids[key] = proposal.id
            estate.rows.setdefault("proposals", []).append((proposal.id, site_key, d(key)))

        def outcome(key, site_key, device):
            session.add(CCOutcomeHistory(
                site_id=s[site_key], action_id=n(key), action_type="SEL_CLEAR",
                device_agent_id=device, vendor="Dell", model="R750",
                outcome="SUCCESS", actor="seed",
            ))
            estate.rows.setdefault("outcomes", []).append((n(key), site_key, device))

        for key, site_key, _cls in DEVICES:
            outcome(f"oc-{key}", site_key, d(key))
        outcome("oc-noncanon-1", "s1", d("noncanon"))

        # REAL safety state at every site. Without it the autonomy contract
        # has no site governance facts in it, and a test asserting that a
        # principal receives none of them passes against an empty list --
        # which is exactly how R4 looked true until the live gate ran.
        for site_key in ("s1", "s2", "s3"):
            session.add(CCSafetyState(
                site_id=s[site_key], tenant_id=tenant, reported=True,
                as_of=datetime.now(timezone.utc), sm_stop_switch=(site_key == "s2"),
                suppressions=[{"domain": n(f"PDU-{site_key}"), "reason": "correlated"}],
                error_budgets=[{"action_type": "SEL_CLEAR", "total_count": 10,
                                "success_count": 3, "failure_count": 7,
                                "dropped_back": site_key == "s2"}],
                site_budgets={"SEL_CLEAR": 5},
            ))
        for site_key in ("s1", "s2", "s3"):
            session.add(CCCandidateSkill(
                skill_id=n(f"skill-{site_key}"), tenant_id=tenant, site_id=s[site_key],
                yaml_text="name: x", source_device=d(f"node-{site_key[1]}"),
            ))
            session.add(CCLearnedSignal(
                tenant_id=tenant, signal_key=n(f"sitesig-{site_key}"), scope_type="site",
                scope_ref=s[site_key], action_type="SEL_CLEAR", vendor="Dell",
                model="R750", statement=n(f"SITE-KNOWLEDGE-{site_key}"),
                evidence={}, confidence=0.7,
            ))
        session.add(CCLearnedSignal(
            tenant_id=tenant, signal_key=n("cohortsig"), scope_type="cohort",
            scope_ref=cohort_ref("Dell", "R750"), action_type="SEL_CLEAR",
            vendor="Dell", model="R750", statement=n("COHORT-KNOWLEDGE"),
            evidence={}, confidence=0.7,
        ))
        await session.commit()
    return estate


# ---------------------------------------------------------------------------
# Personas: real, persisted grants resolved through the ONE loader
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class G:
    """One grant to persist. `ref` is an estate key, resolved at seed time."""
    scope_type: str
    ref: str = ""
    subset: Optional[tuple] = None
    expired: bool = False
    revoked: bool = False
    other_tenant: bool = False
    raw_ref: bool = False   # `ref` is already a literal, not an estate key


PERSONAS: dict[str, tuple[G, ...]] = {
    # the five scope types
    "tenant":          (G("tenant"),),
    "org-region_a":    (G("org_unit", "region_a"),),
    "org-a1":          (G("org_unit", "a1"),),
    "org-b1":          (G("org_unit", "b1"),),
    "site-s1":         (G("site", "s1"),),
    "site-s3":         (G("site", "s3"),),
    "device-node-1":   (G("device", "node-1"),),
    "device-sw-1":     (G("device", "sw-1"),),
    "device-blank-1":  (G("device", "blank-1"),),
    "device-node-3":   (G("device", "node-3"),),
    "class-server":    (G("device_class", "server", raw_ref=True),),
    "class-switch":    (G("device_class", "switch", raw_ref=True),),
    "class-SWITCH":    (G("device_class", "SWITCH", raw_ref=True),),
    "site-s3+device-node-1": (G("site", "s3"), G("device", "node-1")),
    "site-s2+class-switch":  (G("site", "s2"), G("device_class", "switch", raw_ref=True)),
    # negative controls
    "missing-device":  (G("device", "ghost"),),
    "blank-class":     (G("device_class", "", raw_ref=True),),
    "blank-device":    (G("device", "", raw_ref=True),),
    "unknown-class":   (G("device_class", "storage", raw_ref=True),),
    "expired":         (G("device", "node-1", expired=True),
                        G("device_class", "switch", raw_ref=True, expired=True)),
    "revoked":         (G("device", "node-1", revoked=True), G("site", "s1", revoked=True)),
    "inert":           (G("site", "site-gone", raw_ref=True),
                        G("org_unit", "unit-gone", raw_ref=True)),
    "other-tenant":    (G("tenant", other_tenant=True),
                        G("device", "node-1", other_tenant=True)),
    "subset-incident-only": (G("device", "node-1", subset=("incident.view",)),),
    "subset-fleet-only":    (G("device_class", "switch", raw_ref=True, subset=("fleet.view",)),),
    "ungranted":       (),
}


async def persist_persona(sessionmaker, estate: Estate, name: str) -> str:
    """Write one persona's grants (once) and return its principal ref."""
    if name in estate.principals:
        return estate.principals[name]
    principal = f"kc-{name}-{estate.tag}"
    estate.principals[name] = principal
    async with sessionmaker() as session:
        for g in PERSONAS[name]:
            ref = g.ref
            if not g.raw_ref and g.scope_type == "org_unit":
                ref = estate.units[g.ref]
            elif not g.raw_ref and g.scope_type == "site":
                ref = estate.sites[g.ref]
            elif not g.raw_ref and g.scope_type == "device":
                ref = estate.dev(g.ref)
            session.add(CCScopeGrant(
                tenant_id=f"other-{estate.tag}" if g.other_tenant else estate.tenant,
                principal_type="user", principal_ref=principal,
                scope_type=g.scope_type, scope_ref=ref, role="tenant_owner",
                permission_subset=list(g.subset) if g.subset is not None else None,
                granted_by="seed",
                expires_at=PAST if g.expired else None,
                revoked_at=PAST if g.revoked else None,
            ))
        await session.commit()
    return principal


async def reach_for(sessionmaker, estate: Estate, principal: str, permissions) -> ReadReach:
    async with sessionmaker() as session:
        scope = await load_scope(
            session, tenant_id=estate.tenant, principal_ref=principal,
            role_permissions=list(OWNER),
        )
    return read_reach(scope, *permissions)


# ---------------------------------------------------------------------------
# Readers: what the REPOSITORY returns, keyed the way `estate.rows` is
# ---------------------------------------------------------------------------

Reader = Callable[[Any, Estate, ReadReach], Awaitable[set]]


async def _fleet_list_all(s, e, r):
    return {d.agent_id for d in await FleetCacheRepo(s).list_all(e.tenant, scope=r)}


async def _fleet_list_filtered(s, e, r):
    rows, total = await FleetCacheRepo(s).list_filtered(e.tenant, page_size=200, scope=r)
    assert total == len(rows), "the fleet COUNT disagrees with the rows it counts"
    return {d.agent_id for d in rows}


async def _fleet_counts(s, e, r):
    total = await FleetCacheRepo(s).count_total(e.tenant, scope=r)
    by_health = await FleetCacheRepo(s).count_by_health(e.tenant, scope=r)
    assert total == sum(by_health.values())
    # Return the covered devices so the comparison is a set, and use the
    # totals as a cross-check against the list.
    listed = {d.agent_id for d in await FleetCacheRepo(s).list_all(e.tenant, scope=r)}
    assert total == len(listed), "count_total disagrees with list_all"
    return listed


async def _incident_list(s, e, r):
    rows = await IncidentRepo(s).list_incidents(e.tenant, status=None, limit=1000, scope=r)
    return {i.incident_id for i in rows}


async def _incident_visible_ids(s, e, r):
    wanted = [key for key, _site, _dev in e.rows["incidents"]]
    return await IncidentRepo(s).visible_ids(e.tenant, wanted, scope=r)


async def _pending(s, e, r):
    return {x.action_id for x in await ApprovalRouteRepo(s).list_pending(e.tenant, scope=r)}


async def _pending_paginated(s, e, r):
    rows, total = await ApprovalRouteRepo(s).list_pending_paginated(
        e.tenant, page=1, page_size=200, scope=r)
    assert total == len(rows), "the pending COUNT disagrees with the rows"
    return {x.action_id for x in rows}


async def _history(s, e, r):
    return {x.action_id for x in await ApprovalRouteRepo(s).list_history(e.tenant, scope=r)}


async def _history_paginated(s, e, r):
    rows, total = await ApprovalRouteRepo(s).list_history_paginated(
        e.tenant, page=1, page_size=200, scope=r)
    assert total == len(rows), "the history COUNT disagrees with the rows"
    return {x.action_id for x in rows}


async def _proposals(s, e, r):
    return {p.id for p in await AgentProposalRepo(s).list_awaiting_approval(e.tenant, scope=r)}


async def _outcomes(s, e, r):
    rows = await OutcomeHistoryRepo(s).list_outcome_dicts(e.tenant, limit=1000, scope=r)
    return {o["action_id"] for o in rows}


#: reader -> (permissions its route guard accepts, row family, fn)
READERS: dict[str, tuple[tuple, str, Reader]] = {
    "fleet.list_all":            (FLEET_VIEW, "fleet", _fleet_list_all),
    "fleet.list_filtered+count": (FLEET_VIEW, "fleet", _fleet_list_filtered),
    "fleet.counts":              (FLEET_VIEW, "fleet", _fleet_counts),
    "incidents.list":            (INCIDENT_VIEW, "incidents", _incident_list),
    "incidents.visible_ids":     (INCIDENT_VIEW, "incidents", _incident_visible_ids),
    "approvals.pending":         (APPROVAL_READ, "pending", _pending),
    "approvals.pending+count":   (APPROVAL_READ, "pending", _pending_paginated),
    "approvals.history":         (APPROVAL_READ, "history", _history),
    "approvals.history+count":   (APPROVAL_READ, "history", _history_paginated),
    "proposals.awaiting":        (APPROVAL_READ, "proposals", _proposals),
    "outcomes.list":             (FLEET_VIEW, "outcomes", _outcomes),
}


def universe(estate: Estate, family: str) -> list[tuple[str, str, str]]:
    if family == "fleet":
        return [(estate.dev(k), site, estate.dev(k)) for k, site, _c in DEVICES]
    return estate.rows[family]


def canonical_visible(estate: Estate, index: FleetIndex, reach: ReadReach, family: str) -> set:
    """What the canonical rule says, with NO reference to the SQL.

    The owner rule in Python: the row's device as it currently resolves at
    the row's own site, then `covers_owned`. This is the right-hand side
    of the invariant.
    """
    visible = set()
    for key, site_key, device in universe(estate, family):
        site_id = estate.sites[site_key]
        if reach.covers_owned(site_id, index.resolve(site_id, device)):
            visible.add(key)
    return visible


@dataclass
class MatrixResult:
    checked: int = 0
    #: (persona, reader) -> visible keys
    seen: dict[tuple[str, str], set] = field(default_factory=dict)
    mismatches: list[str] = field(default_factory=list)


async def run_matrix(sessionmaker, estate: Estate,
                     personas: Optional[Iterable[str]] = None) -> MatrixResult:
    """Every persona x every reader: repository == canonical. Returns the
    evidence so the caller can assert it was not vacuous."""
    result = MatrixResult()
    async with sessionmaker() as session:
        index = FleetIndex(await FleetCacheRepo(session).list_all(estate.tenant))
    for name in (personas or PERSONAS):
        principal = await persist_persona(sessionmaker, estate, name)
        for reader, (permissions, family, fn) in READERS.items():
            reach = await reach_for(sessionmaker, estate, principal, permissions)
            async with sessionmaker() as session:
                got = await fn(session, estate, reach)
            # Only rows of THIS estate: a shared PostgreSQL holds other runs.
            mine = {key for key, _s, _d in universe(estate, family)}
            got &= mine
            want = canonical_visible(estate, index, reach, family)
            result.checked += 1
            result.seen[(name, reader)] = got
            if got != want:
                result.mismatches.append(
                    f"{name} x {reader}: repository-only {sorted(got - want)} "
                    f"canonical-only {sorted(want - got)}"
                )
    return result


async def context_sites(sessionmaker, estate: Estate, principal: str) -> tuple[set, set]:
    """(authoritative site keys, contextual site keys) for `fleet.view`."""
    reach = await reach_for(sessionmaker, estate, principal, FLEET_VIEW)
    by_id = {v: k for k, v in estate.sites.items()}
    async with sessionmaker() as session:
        authoritative = {by_id[s.id] for s in await SiteRepo(session).list_all(estate.tenant, scope=reach) if s.id in by_id}
        contextual = {by_id[s.id] for s in await SiteRepo(session).list_context(estate.tenant, scope=reach) if s.id in by_id}
    return authoritative, contextual
