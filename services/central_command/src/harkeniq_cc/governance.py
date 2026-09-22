"""One loader for the autonomy contract, shared by every consumer.

A1 (2026-08-30). `GET /api/autonomy` and the Operational Agent evaluator
must read the SAME contract from the SAME inputs. If each assembled its
own repository reads they would drift the first time an input was added,
and the operator would be looking at a different governance state than
the agent acted on. That failure would be invisible until it mattered,
which is the worst shape a governance bug can take.

So the fetch lives here once. The composition still lives in
`harkeniq_cc.autonomy` and stays pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.autonomy import (
    build_autonomy,
    visible_blocking_conditions,
    visible_disposition_reason,
    visible_verdict_evidence,
)
from harkeniq_cc.capabilities import build_capability_registry
from harkeniq_cc.learning_projection import (
    project_candidate,
    project_citations,
    project_cycles,
    project_frozen_signals,
    project_generated,
    project_patterns,
    project_signals,
)
from harkeniq_cc.db.repos import (
    ApprovalPolicyRepo,
    AutonomyBudgetRepo,
    FleetCacheRepo,
    LearnedSignalRepo,
    OrgUnitRepo,
    OutcomeHistoryRepo,
    SafetyStateRepo,
    ScopeGrantRepo,
    SiteRepo,
    StopSwitchRepo,
    TenantSettingsRepo,
    require_read_reach,
)
from harkeniq_cc.scope import (
    PRINCIPAL_AGENT,
    PRINCIPAL_USER,
    SCOPE_ONLY_MARKER,
    ResolvedScope,
    read_reach,
    resolve,
)


async def load_scope(
    session: AsyncSession,
    *,
    tenant_id: str,
    principal_ref: str,
    role_permissions,
    principal_type: str = PRINCIPAL_USER,
    realm: str = "",
    aliases: Iterable[str] = (),
) -> ResolvedScope:
    """Resolve one principal's authorization scope. E1.2.

    The ONE loader for scope, for the same reason `load_autonomy_contract`
    is the one loader for the contract: a human and an agent that
    assembled their own inputs would drift, and a scope that drifts is an
    authorization bug rather than a display bug.

    Humans and Operational Agents differ only in `principal_type`. The
    rows, the tree, the enforcement mode and the resolver are identical.

    `aliases` (A23-4) are authenticated alternate references for the
    SAME principal -- the token's own email claim -- used only to find
    prior grant evidence. The platform's own recorded email<->subject
    pairs are added here from the one identity-evidence implementation.
    Evidence never authorizes: an alias-keyed row still confers nothing.
    """
    from harkeniq_cc.identity_evidence import subject_aliases
    from harkeniq_cc.grant_integrity import role_ceiling_for

    repo = ScopeGrantRepo(session)
    grants = await repo.list_for_principal(
        tenant_id, principal_ref, principal_type=principal_type,
        realm=realm,
    )
    # A23.10: NEVER GRANTED versus PREVIOUSLY GRANTED. The authorization
    # read above is realm-narrowed and revocation-filtered by design;
    # this read is neither, because a revoked row, a stale-realm row or
    # a row keyed by a recorded alias is still evidence that somebody
    # administered this principal. Evidence decides one thing -- no
    # synthesized tenant grant -- and adds no reach.
    refs = {principal_ref, *aliases}
    if principal_type == PRINCIPAL_USER:
        refs |= await subject_aliases(session, tenant_id, principal_ref)
    prior = await repo.grant_evidence(tenant_id, principal_type, refs)
    org_units = await OrgUnitRepo(session).list_all(tenant_id)
    sites = await SiteRepo(session).list_all(tenant_id)
    enforcement = await TenantSettingsRepo(session).enforcement(tenant_id)
    return resolve(
        tenant_id=tenant_id,
        principal_type=principal_type,
        principal_ref=principal_ref,
        role_permissions=role_permissions,
        grant_rows=grants,
        org_units=org_units,
        sites=sites,
        enforcement=enforcement,
        # A23-3: the role a grant RECORDS is a ceiling the grantor
        # asserted. Supplied here so the resolver stays ignorant of role
        # names and every caller of this loader narrows identically.
        role_ceiling_for=role_ceiling_for,
        prior_grants=prior,
    )


async def load_agent_scope(
    session: AsyncSession, *, tenant_id: str, agent_id: str
) -> ResolvedScope:
    """An Operational Agent's scope, through the same resolver.

    WHERE, never WHETHER (A22.13). This used to resolve with
    ``role_permissions=["*"]`` and justify it: an agent's authority is its
    A0 bindings plus the autonomy contract, and *it does not call the HTTP
    API, the CC-resident evaluator does*. A3 removed that premise -- an
    agent holds a credential now -- and resolved that way the scope
    answered ``permits("action.approve")`` with True. It could have
    approved its own proposals. It was latent only because all four call
    sites read `.site_ids`.

    `SCOPE_ONLY_MARKER` keeps the grant arithmetic identical, so no agent
    loses reach, while making a permission question on this scope an
    error instead of a yes.
    """
    return await load_scope(
        session,
        tenant_id=tenant_id,
        principal_ref=agent_id,
        role_permissions=[SCOPE_ONLY_MARKER],
        principal_type=PRINCIPAL_AGENT,
        # An agent is not a realm principal: its id is a CC row id, so
        # its grants carry no realm and are never narrowed by one.
        realm="",
    )


@dataclass(frozen=True)
class AgentReach:
    """What one Operational Agent may operate on, right now.

    A30.4/A30.10: THE operational reach answer. Central Command used to
    have two and they disagreed in opposite directions -- the canonical
    resolver was lifecycle-correct and its `site_ids` projection could
    not represent a `device` or `device_class` grant, while the raw
    repository read was type-complete and filtered `revoked_at` without
    `expires_at`. `agent_runtime` passed BOTH into `resolve_scope`, so
    the runtime compensated for the first defect by importing the second
    and took the union of their errors.

    `rules` is `ResolvedScope.effective_grants`: lifecycle-filtered and
    inert-filtered by `resolve()`, and still carrying `scope_type` and
    `scope_ref` on every surviving grant, so it is what `resolve_scope`
    always wanted and never got.

    F1 is deliberately NOT fixed here. `site_ids` remains lossy and the
    repository read filter still cannot express device reach; reaching
    back for raw grant rows to paper over that would reintroduce the
    exact path this type exists to remove. A6-4B0b corrects it properly.
    """

    scope: ResolvedScope
    rules: tuple
    devices: tuple

    @property
    def site_ids(self) -> frozenset[str]:
        return self.scope.site_ids


async def load_agent_reach(
    session: AsyncSession,
    *,
    tenant_id: str,
    agent_id: str,
    devices: Optional[Iterable] = None,
) -> AgentReach:
    """Resolve one agent's operational reach. The only way to ask.

    `devices` is the fleet to intersect against; omit it and the caller
    gets the resolved rules without a device read, which is what a
    caller composing its own `evaluate()` call needs.
    """
    from harkeniq_cc.operational_agent import resolve_scope

    scope = await load_agent_scope(
        session, tenant_id=tenant_id, agent_id=agent_id
    )
    rules = scope.effective_grants
    if devices is None:
        return AgentReach(scope=scope, rules=rules, devices=())
    return AgentReach(
        scope=scope,
        rules=rules,
        devices=tuple(resolve_scope(rules, devices, scope.site_ids)),
    )


def authorized_sites(reach) -> Optional[frozenset[str]]:
    """The sites whose autonomy facts this reach may read (A30.26).

    ``None`` means the whole tenant; anything else is the exact set. It is
    a PROJECTION of S2's `ReadReach` and adds no rule of its own: a site
    grant or an org-expanded site that carries the route's permission is
    in, and nothing else is -- a `device` or `device_class` grant names no
    site (R4), an inert, expired, revoked or subset-narrowed grant is not
    in the reach at all (A30.24), and a contextual site (general B0b) has
    no field on `ReadReach` to arrive through.

    Refuses anything that is not a `ReadReach`, a bare `ResolvedScope`
    included: that set is permission-NEUTRAL, and filtering autonomy facts
    through it would be P1 again.
    """
    reach = require_read_reach(reach)
    if reach is None:
        raise TypeError(
            "authorized_sites() needs the reader's ReadReach (A30.26); "
            "there is no reader-less projection of a proposal"
        )
    return None if reach.tenant_wide else frozenset(reach.site_ids)


#: The permission a site-derived AUTONOMY fact is read under (A30.26). It is
#: the guard on `/api/autonomy/`, and it stays the permission wherever such
#: a fact is projected -- including a route guarded by something else.
AUTONOMY_FACT_PERMISSION = "fleet.view"


@dataclass(frozen=True)
class AutonomyView:
    """Which sites' autonomy facts ONE reader may be shown (A30.26).

    A proposal records the blocking conditions and learned signals the
    evaluator decided on, and the evaluator decides over the whole
    tenant. Every projection of a proposal therefore needs to know whose
    eyes it is for, and this is the only way to say so.

    It exists as a TYPE for the reason `ReadReach` does: the alternative
    is an optional set where ``None`` means "unrestricted", and a
    projection that forgot the argument -- or was handed ``None`` by a
    caller who had nothing better -- would leak silently and pass its
    tests. `require_autonomy_view` refuses everything but one of these,
    and `autonomy_view` is its only constructor outside a test.
    """

    #: ``None`` = the whole tenant. Otherwise the exact set, possibly empty.
    sites: Optional[frozenset[str]]

    def blocking(self, rows) -> list:
        return visible_blocking_conditions(rows, self.sites)

    def evidence(self, evidence) -> dict:
        """The stored evidence, with its learned signals narrowed AND bounded.

        A30.26 drops a site-scoped entry whose site the reader does not
        hold. A30.28 then bounds what is left: a cohort entry is always
        kept, and its recorded statement says "30 of 40 attempts, across
        2 sites" -- row selection never looked inside a surviving row.
        """
        out = visible_verdict_evidence(evidence, self.sites)
        if self.sites is not None and "learned_signals" in out:
            out["learned_signals"] = project_frozen_signals(
                out.get("learned_signals"), self.sites,
            )
        return out

    def reason(self, reason, rows) -> str:
        """The stored reason, unless it is the text of a withheld row."""
        return visible_disposition_reason(reason, rows, self.sites)


def autonomy_view(scope) -> AutonomyView:
    """The reader's view of site-derived autonomy facts, from their scope.

    `read_reach(scope, "fleet.view")` and nothing else: NOT the reach of
    whatever route is projecting. The approval queue is guarded by
    `action.approve | audit.view`, and a grant can carry `action.approve`
    at a site while its subset withholds `fleet.view` there; narrowing by
    the queue's own reach would show that reader safety facts
    `/api/autonomy/` refuses them. Permission and coverage come from the
    SAME grant (A30.24), for the permission these facts require.
    """
    return AutonomyView(
        sites=authorized_sites(read_reach(scope, AUTONOMY_FACT_PERMISSION))
    )


def require_autonomy_view(view) -> AutonomyView:
    """The projection boundary of A30.26: an `AutonomyView`, or a TypeError."""
    if isinstance(view, AutonomyView):
        return view
    raise TypeError(
        "a proposal projection needs the reader's AutonomyView "
        "(harkeniq_cc.governance.autonomy_view(scope)), not "
        f"{type(view).__name__}: a stored verdict names sites the reader may "
        "not hold, and there is no reader-less projection of one (spec A30.26)"
    )


#: The permission a learned signal or fleet pattern is read under (A30.28).
#: It is the guard on `/api/learning/signals` and `/api/outcomes/patterns`,
#: and it stays the permission wherever that knowledge is projected --
#: including incident detail, which is guarded by `incident.view`. Same
#: rule, same reason, as `AUTONOMY_FACT_PERMISSION`.
LEARNING_FACT_PERMISSION = "fleet.view"


@dataclass(frozen=True)
class LearningView:
    """Which sites' LEARNING facts one reader may be shown (A30.28).

    A learned signal's conclusion is tenant knowledge (A23); its
    supporting evidence names sites, counts them, and carries tenant
    totals. Every projection of a signal or a pattern therefore needs to
    know whose eyes it is for. This is a TYPE for the reason
    `AutonomyView` is: the alternative is an optional set where ``None``
    means "unrestricted", and a projection that forgot the argument would
    leak silently and pass its tests.

    It resolves nothing. `sites` is `authorized_sites(read_reach(scope,
    "fleet.view"))` -- S2's reach, S3's projection of it -- and the rule
    itself lives once, in `harkeniq_cc.learning_projection`.
    """

    #: ``None`` = the whole tenant. Otherwise the exact set, possibly empty.
    sites: Optional[frozenset[str]]

    def signals(self, rows) -> list:
        return project_signals(rows, self.sites)

    def patterns(self, rows, *, limit: Optional[int] = None) -> list:
        return project_patterns(rows, self.sites, limit=limit)

    def citations(self, entries):
        """An incident diagnosis's `evidence_cited`, pattern citations bounded."""
        return project_citations(entries, self.sites)

    def cycles(self, payloads) -> list:
        return project_cycles(payloads, self.sites)

    def generated(self, explanation) -> tuple[dict, Optional[dict]]:
        """An incident diagnosis's generated block (A30.29): shown only when
        its recorded projection is one of THIS reader's sites now."""
        return project_generated(explanation, self.sites)

    def candidate(self, row) -> dict:
        """A candidate skill's generated fields (A30.29), same rule."""
        return project_candidate(row, self.sites)


def learning_view(scope) -> LearningView:
    """The reader's view of learned knowledge, from their scope."""
    return LearningView(
        sites=authorized_sites(read_reach(scope, LEARNING_FACT_PERMISSION))
    )


def require_learning_view(view) -> LearningView:
    """The projection boundary of A30.28: a `LearningView`, or a TypeError."""
    if isinstance(view, LearningView):
        return view
    raise TypeError(
        "a learned-signal or fleet-pattern projection needs the reader's "
        "LearningView (harkeniq_cc.governance.learning_view(scope)), not "
        f"{type(view).__name__}: the stored evidence names sites the reader "
        "may not hold, and there is no reader-less projection of it "
        "(spec A30.28)"
    )


async def load_autonomy_contract(
    session: AsyncSession,
    *,
    tenant_id: str,
    actor_id: str,
    actor_species: str,
    permissions: Iterable[str],
    reach,
    site_id: Optional[str] = None,
    action_type: Optional[str] = None,
) -> dict:
    """Fetch every tenant-scoped input and compose the contract.

    Every read below is tenant-scoped by its repository; `site_id`
    narrows within the reader's own sites and can never widen beyond them.

    `reach` is REQUIRED and has no default (A30.26), so a caller cannot
    obtain the tenant-wide composition by leaving something out:

    * a `ReadReach` -- the contract is going back to a PRINCIPAL. It is
      composed over the sites that reach authorizes and over nothing
      else, selected BEFORE anything is folded (`select_site_inputs`), so
      no aggregate, boolean or disposition in it reflects another site;
    * ``None`` -- an INTERNAL DECISION PATH (the evaluator, the ingress
      re-derivation, the dry-run's reasoning, campaign submission, the
      activation preflight). These decide over the whole tenant and never
      return the contract. The call sites allowed to say so are
      allow-listed by a structural test.

    A bare `ResolvedScope` is refused, exactly as the repositories refuse
    one (A30.24).
    """
    visible_site_ids = (
        None if require_read_reach(reach) is None else authorized_sites(reach)
    )
    budgets = await AutonomyBudgetRepo(session).list_all(tenant_id)
    stop_switch = await StopSwitchRepo(session).get(tenant_id)
    outcomes = await OutcomeHistoryRepo(session).list_outcome_dicts(tenant_id)
    safety_rows = await SafetyStateRepo(session).list_for_tenant(tenant_id)
    sites = await SiteRepo(session).list_all(tenant_id)
    learned = await LearnedSignalRepo(session).list_active(tenant_id)
    if visible_site_ids is not None:
        # A30.28: a principal's contract quotes each signal's STATEMENT and
        # confidence. `select_site_inputs` decides which ROWS it may see;
        # this decides what is inside the ones that survive. An internal
        # decision path (`reach=None`) reasons over what is stored.
        learned = project_signals(learned, visible_site_ids)
    # Approval policies shape what "requires approval" actually means for
    # a class. Read at fleet.view even though managing them needs
    # site.manage: knowing an action needs two approvers is posture, and
    # the posture read-split (D2) is the whole point of this surface.
    policies = await ApprovalPolicyRepo(session).list_all(tenant_id)

    return build_autonomy(
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_species=actor_species,
        permissions=permissions,
        budgets=budgets,
        stop_switch=stop_switch,
        outcomes=outcomes,
        safety_rows=safety_rows,
        sites=sites,
        learned_signals=learned,
        approval_policies=policies,
        site_id=site_id,
        action_type=action_type,
        visible_site_ids=visible_site_ids,
    )


async def load_capability_registry(
    session: AsyncSession,
    *,
    tenant_id: str,
    scope=None,
    site_id: Optional[str] = None,
    action_type: Optional[str] = None,
) -> dict:
    """Fetch the caller's visible fleet and compose the Registry.

    Same discipline as `load_autonomy_contract`, and for the same
    reason: the Console and the Operational Agent must read capability
    truth from ONE loader over ONE set of reads. If the page and the
    agent could see different capability sets, an operator would approve
    a proposal the agent should never have made and neither surface
    could explain why.

    `scope` is the E1.2 resolved scope. It is passed into the repository
    read, so what the Registry describes is exactly the fleet this
    principal may see -- never more, and never the whole tenant as a
    convenience.
    """
    devices = await FleetCacheRepo(session).list_all(tenant_id, scope=scope)
    sites = await SiteRepo(session).list_all(tenant_id, scope=scope)
    return build_capability_registry(
        tenant_id=tenant_id,
        devices=devices,
        sites=sites,
        site_id=site_id,
        action_type=action_type,
    )


async def load_attention(
    session: AsyncSession,
    *,
    tenant_id: str,
    learning,
    site_id: Optional[str] = None,
    scope=None,
    band: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """Fetch every input and compose the attention answer. ONE of these.

    `learning` is REQUIRED and has no default (A30.28), for the reason
    `load_autonomy_contract`'s `reach` has none: attention attaches learned
    signals and fleet patterns to every device, and this is the one read
    every Operational Agent is required to hold.

    * a `LearningView` -- the answer is going back to a PRINCIPAL, human or
      machine. Signals and patterns are projected for that reader BEFORE
      they are composed, so the evidence, the statement quoted inside
      `reasons[]` and the pattern description are all bounded at once;
    * ``None`` -- an INTERNAL DECISION PATH (the evaluator, the dry-run's
      reasoning, the ingress re-derivation). They read rank, band, driver
      and score off each item and never return the attention payload. The
      call sites allowed to say so are allow-listed by a structural test.

    `/api/attention` and the Operational Agent evaluator must rank the
    same devices from the same evidence, or the agent acts on a picture
    the operator has never seen. That was written down here and then NOT
    done: the router carried a near-verbatim copy whose `band` filter ran
    BEFORE ranking, so filtering reordered `rank` -- and rank is what
    decides which devices consume an agent's proposal budget. A5 (A22.11)
    makes this the only implementation.

    `scope` is E1.2's resolver (A22.9). It was missing entirely, so a
    site-scoped principal read every site's attention state -- and this is
    the ONE read every Operational Agent is required to hold. Both callers
    now supply the caller's own scope, which is also what makes "HTTP and
    in-process rank identically" true for a given principal rather than
    only for a tenant-wide one.

    `band` is a PURE FILTER applied AFTER ranking, which is what the
    endpoint's own contract always claimed: rank 1 means first in the
    principal's scope, never first on the page.
    """
    from harkeniq_cc.attention import build_attention
    from harkeniq_cc.db.repos import (
        ApprovalRouteRepo,
        CveFeedRepo,
        FleetCacheRepo,
        FleetPatternRepo,
        IncidentRepo,
        WarrantyRepo,
    )
    from harkeniq_cc.exposure import match_exposures
    from harkeniq_cc.predictive import cohort_failure_rates, score_device
    from harkeniq_cc.warranty.base import warranty_status

    devices = await FleetCacheRepo(session).list_all(tenant_id, scope=scope)
    if site_id:
        devices = [d for d in devices if d.site_id == site_id]
    outcomes = await OutcomeHistoryRepo(session).list_device_outcome_dicts(tenant_id)
    warranty_map = await WarrantyRepo(session).get_map(
        [d.service_tag for d in devices], tenant_id=tenant_id,
    )
    cve_entries = await CveFeedRepo(session).list_all(tenant_id=tenant_id)
    pending_routes = await ApprovalRouteRepo(session).list_pending(
        tenant_id, scope=scope,
    )
    patterns = await FleetPatternRepo(session).list_patterns(tenant_id=tenant_id)
    sites = await SiteRepo(session).list_all(tenant_id, scope=scope)
    learned = await LearnedSignalRepo(session).list_active(tenant_id)
    if learning is not None:
        view = require_learning_view(learning)
        patterns = view.patterns(patterns)
        learned = view.signals(learned)
    open_incidents = await IncidentRepo(session).list_incidents(
        tenant_id, status="open", site_id=site_id, limit=1000, scope=scope,
    )

    cohorts = cohort_failure_rates(outcomes)
    by_device: dict[str, list[dict]] = {}
    for oc in outcomes:
        by_device.setdefault(oc["device_agent_id"], []).append(oc)

    risks = []
    for dev in devices:
        warranty = warranty_map.get(dev.service_tag)
        risk = score_device(
            agent_id=dev.agent_id,
            outcomes=by_device.get(dev.agent_id, []),
            cohort_failure_rate=cohorts.get((dev.vendor, dev.model)),
            health=dev.health,
            warranty_status=warranty_status(warranty.end_date) if warranty else "",
            vendor=dev.vendor,
            model=dev.model,
        )
        risk.site_id = dev.site_id
        risk.agent_name = dev.agent_name
        risks.append(risk)

    result = build_attention(
        devices=devices,
        risks=risks,
        exposures=match_exposures(devices, cve_entries),
        warranty_map=warranty_map,
        pending_routes=pending_routes,
        patterns=patterns,
        sites=sites,
        tenant_id=tenant_id,
        learned_signals=learned,
        incidents=open_incidents,
    )
    # AFTER ranking, never before. Rank is assigned over the principal's
    # whole scope, so "rank 1" always means first in that scope.
    if band:
        result["items"] = [i for i in result["items"] if i.get("band") == band]
    if limit is not None:
        result["items"] = result["items"][:limit]
    result["returned"] = len(result["items"])
    return result
