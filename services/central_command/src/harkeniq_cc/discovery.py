"""A6-4B1: governed capability and parameter discovery (spec A30.32).

Pure. The router fetches every input and hands it in, so the whole answer is
unit-testable without a database -- the shape of `autonomy.build_autonomy`,
`capabilities.build_capability_registry` and `agent_activation`, on purpose:
discovery COMPOSES those answers and authors none of its own.

WHAT IT ANSWERS
---------------
For an Operational Agent reading ITSELF: what it may address, where, under
which governance conclusion, with which parameters -- as eight independent
facts per action class:

    1 exists              the class is in the governed ActionType vocabulary
    2 implemented         action_facts(): a protocol this build ships has it
    3 addressable         the ENABLED condition catalogue names it, or it is
                          campaign-only (D2: never "reachable")
    4 bound               an A0 action_class binding names it
    5 in_effective_scope  the Registry over the agent's READABLE reach
    6 governance          effective_disposition over the reach-composed
                          contract (S3)
    7 approval_required   required | not_required | unknown | not_applicable
    8 currently_operable  operable | not_operable | unknown

They stay eight. Nothing here, or anywhere in the response, is `allowed`,
`authorized`, `can_execute`, `safe_to_execute` or any equivalent: the one
field that combines facts -- `currently_operable` -- names the facts that
block it rather than collapsing them into a verdict.

WHAT IT MAY NOT DO
------------------
Confer anything. Discovery is descriptive: `govern_proposal` re-derives every
fact at proposal, the approval policy is evaluated at approval, current
authority is revalidated at dispatch, and the node's funnel is final. Nothing
in the response is ever accepted back as proof of anything, and it carries no
candidate reference, digest or signature for anyone to try.

Narrow after composing. The contract it reads is already composed over the
agent's authorized sites (A30.26); nothing here filters a composed value.

Learn, remember or quote. No learned signal, pattern, outcome evidence,
advancement story or generated text is named here (A30.28, A30.29).

THE ONE PLACE HIDDEN STATE ENTERS (A30.32, D3; S3-E1, A30.37)
--------------------------------------------------------------
Before S3-E1, admission folded TENANT-WIDE state, and `admission_reads_beyond`
said so. S3-E1 consumed that seam: admission reads the TARGET site's local
assessment and the closed global gate, and hidden state reaches this
response only as the gate's one bounded code. A class-level answer is now
the per-site one -- definitive when every site in this agent's composition
agrees and no target lies outside it, `depends_on_target_site` otherwise.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from harkeniq.capabilities import action_facts, parameter_contract
from harkeniq.models import ActionType

from harkeniq_cc.agent_activation import check_budget
from harkeniq_cc.autonomy import (
    ACTOR_AGENT,
    AUTONOMOUS,
    DENIED,
    REQUIRES_APPROVAL,
    SCOPE_DOMAIN,
    SCOPE_SITE,
    SCOPE_TENANT,
)
from harkeniq_cc.capabilities import (
    REACH_NO_DEVICES,
    REACH_NONE,
    REACH_NOT_PERMITTED,
    REACH_UNKNOWN,
    WHY_ALLOW_LIST,
    reach_state,
)
from harkeniq_cc.capability_catalogue import CAMPAIGN_ONLY_CLASSES
from harkeniq_cc.global_safety import (
    GLOBAL_SAFETY_CONSTRAINT,
    SCOPE_GLOBAL,
    UNKNOWN as GATE_UNKNOWN,
    final_block,
    require_verdict,
)
from harkeniq_cc.operational_agent import (
    STATUS_ACTIVE,
    _suppressed_sites,
    effective_disposition,
    proposal_budget_left,
)

#: Bump when a consumer would have to change to read the payload.
DISCOVERY_VERSION = "1"

#: Every machine projection says so, in one word (receipts.VIEW_MACHINE).
VIEW_MACHINE = "machine"

#: The permission the self-scope and every autonomy fact are read under
#: (A30.26, A30.32). It is the route's own guard.
DISCOVERY_PERMISSION = "fleet.view"

# -- fact 3: addressable (D2) ------------------------------------------------

#: An ENABLED row of the tenant's condition catalogue names the class: the
#: path an agent's proposals take.
PATH_CATALOGUE = "condition_catalogue"
#: FIRMWARE_UPDATE / FIRMWARE_ROLLBACK: addressable through an S6 campaign,
#: never proposed by an agent in response to a fault (A21.10).
PATH_CAMPAIGN = "campaign_only"
PATH_NONE = "none"

# -- fact 7: approval required (D3) ------------------------------------------

APPROVAL_REQUIRED = "required"
APPROVAL_NOT_REQUIRED = "not_required"
APPROVAL_UNKNOWN = "unknown"
APPROVAL_NOT_APPLICABLE = "not_applicable"
APPROVAL_STATES = frozenset({
    APPROVAL_REQUIRED, APPROVAL_NOT_REQUIRED, APPROVAL_UNKNOWN,
    APPROVAL_NOT_APPLICABLE,
})

#: Why the approval state is what it is. Closed.
BASIS_NOT_GOVERNED = "not_governed"
BASIS_NOT_BOUND = "not_bound"
BASIS_GOVERNANCE_DENIED = "governance_denied"
BASIS_GOVERNANCE_REQUIRES_APPROVAL = "governance_requires_approval"
#: A19 D2: a spent execution budget returns autonomous work to a human at
#: dispatch, so "not required" would be an over-promise.
BASIS_EXECUTION_BUDGET_EXHAUSTED = "execution_budget_exhausted"
#: D3: admission read state beyond this agent's reach. Retired by S3-E1
#: (A30.37): kept in the closed vocabulary, never produced.
BASIS_ADMISSION_BEYOND_REACH = "admission_beyond_reach"
#: `govern_proposal`'s per-target rule: a target at a site with a suppressed
#: fault domain needs a human, so a class-level answer depends on the target.
BASIS_DEPENDS_ON_TARGET_SITE = "depends_on_target_site"
BASIS_GOVERNANCE_AUTONOMOUS = "governance_autonomous"
#: The contract describes no row for a governed class. Every class has one
#: today; if that ever stops being true the answer is unknown, never
#: optimistic.
BASIS_NOT_DESCRIBED = "governance_not_described"

APPROVAL_BASES = frozenset({
    BASIS_NOT_GOVERNED, BASIS_NOT_BOUND, BASIS_GOVERNANCE_DENIED,
    BASIS_GOVERNANCE_REQUIRES_APPROVAL, BASIS_EXECUTION_BUDGET_EXHAUSTED,
    BASIS_ADMISSION_BEYOND_REACH, BASIS_DEPENDS_ON_TARGET_SITE,
    BASIS_GOVERNANCE_AUTONOMOUS, BASIS_NOT_DESCRIBED,
})

# -- fact 8: currently operable (D2, D3) --------------------------------------

OPERABLE = "operable"
NOT_OPERABLE = "not_operable"
OPERABILITY_UNKNOWN = "unknown"
OPERABILITY_STATES = frozenset({OPERABLE, NOT_OPERABLE, OPERABILITY_UNKNOWN})

#: Known blockers, in the order they are reported. Closed.
BLOCKERS = (
    "agent_not_active",
    "agent_paused",
    "proposal_budget_exhausted",
    "not_governed",
    "not_implemented",
    "not_bound",
    "not_addressable",
    "campaign_only",
    "parameters_unavailable",
    "no_devices_in_scope",
    "no_effective_reach",
    "not_permitted_on_any_node",
    "governance_denied",
    # S3-E1 (A30.37): the closed global safety gate is not clear. The exact
    # ratified code -- one spelling everywhere it appears.
    GLOBAL_SAFETY_CONSTRAINT,
)

#: Why operability cannot be stated definitively. Closed.
UNKNOWNS = (
    "executor_reach_undeclared",
    BASIS_ADMISSION_BEYOND_REACH,
    BASIS_NOT_DESCRIBED,
)

# -- fact 6: governance reason codes -----------------------------------------

#: The blocking codes a composed class row can carry, as `build_autonomy`
#: and `effective_disposition` emit them. An allow-list of codes that
#: ALREADY exist (a structural test holds it equal to every emitter); no code
#: is invented here. `site_suppressed` is absent on purpose: it is
#: `govern_proposal`'s per-TARGET rule and never appears on a class row --
#: discovery reports it as `depends_on_target_site` on the approval fact.
GOVERNANCE_REASON_CODES = frozenset({
    "never_budget_grantable",
    "not_budget_mapped",
    "stop_switch_active",
    "level_below_grant",
    "error_budget_dropped_back",
    "budget_window_exhausted",
    "domain_suppressed",
    "agent_requires_approval",
    "agent_ceiling_below_grant",
    # S3-E1 (A30.37).
    "site_safety_not_reported",
    "site_stop_switch_active",
    "agent_unattended_not_approved",
    GLOBAL_SAFETY_CONSTRAINT,
})

#: A code this module does not recognise is reported as this, never passed
#: through: a reason code is a closed vocabulary, not a free-text channel.
REASON_OTHER = "other"

_REASON_SCOPES = frozenset({SCOPE_TENANT, SCOPE_SITE, SCOPE_DOMAIN, SCOPE_GLOBAL})

# -- the composition ----------------------------------------------------------

COMPOSED_OVER_TENANT = "tenant"
COMPOSED_OVER_SITES = "authorized_sites"
#: S3-E1 (A30.37): admission reads the TARGET site's local assessment and the
#: closed gate's bounded verdict -- never the tenant-wide fold.
COMPOSED_OVER_TARGET_SITE = "target_site"

#: What admission composes over.
ADMISSION_COMPOSED_OVER = COMPOSED_OVER_TARGET_SITE


def admission_reads_beyond(reach) -> bool:
    """Does admission read governance state this agent's reach does not?

    THE E1 SEAM (A30.32, D3) -- consumed by S3-E1 (A30.37). Admission now
    reads the TARGET site's local assessment, which is a site this agent
    acts on, and the closed global gate, which reaches every projection as
    one bounded code. Neither is state beyond the reach, so the answer is
    no, for every reach, and `admission_beyond_reach` is no longer produced.
    Where discovery still cannot answer definitively -- targets at sites
    outside its own composition, or sites that disagree -- it says
    `depends_on_target_site`, which is the truth, not an over-promise.
    """
    return False


# ---------------------------------------------------------------------------
# The composer
# ---------------------------------------------------------------------------


def _addressable(name: str, conditions: dict[str, set[str]]) -> dict[str, Any]:
    if name in CAMPAIGN_ONLY_CLASSES:
        path = PATH_CAMPAIGN
    elif conditions.get(name):
        path = PATH_CATALOGUE
    else:
        path = PATH_NONE
    return {
        "value": path != PATH_NONE,
        "path": path,
        "conditions": sorted(conditions.get(name, ())),
    }


def _in_effective_scope(
    implemented: bool, registry_row: Optional[dict], devices_in_reach: int,
) -> dict[str, Any]:
    """Fact 5, from the Registry's own counts (A29.10: no second catalogue).

    `implementing` is what the Registry calls effective PLUS what it blocks
    only on the node's allow list -- the devices whose protocol has the class
    -- and the state comes from the one derivation the agent view shares.
    """
    if registry_row is None:
        # Not a governed class: the Registry does not describe it, and
        # nothing in reach can run it.
        devices, permitting, implementing, undeclared = devices_in_reach, 0, 0, 0
    else:
        devices = int(registry_row.get("devices_in_view") or 0)
        permitting = int(registry_row.get("effective_device_count") or 0)
        undeclared = int(registry_row.get("undeclared_device_count") or 0)
        allow_list_only = sum(
            int(b.get("device_count") or 0)
            for b in registry_row.get("blocked_by") or ()
            if b.get("reason") == WHY_ALLOW_LIST
        )
        implementing = permitting + allow_list_only
    return {
        "state": reach_state(
            implemented=implemented, devices_in_scope=devices,
            implementing=implementing, permitting=permitting,
            undeclared=undeclared,
        ),
        "devices_in_scope": devices,
        "implementing_devices": implementing,
        "permitting_devices": permitting,
        "undeclared_devices": undeclared,
    }


def _parameters(name: str) -> dict[str, Any]:
    """Fact-adjacent: what the class requires, from `parameter_contract`.

    Named field by field. Executor DEFAULTS are not published: Central
    Command resolves every parameter, the ingress accepts none (A24.2), and
    `source: default` already says no input is needed. No enum or range is
    added -- the contract has none (G8), and inventing one here would be a
    fourth place to get a device's identifier grammar wrong.
    """
    contract = parameter_contract(name)
    return {
        "required": list(contract["required"]),
        "resolvable": bool(contract["agent_resolvable"]),
        "unresolvable_reason": contract["unsatisfiable_reason"] or "",
        "items": [
            {
                "name": p["name"],
                "type": p["type"],
                "required": bool(p["required"]),
                "source": p["source"],
                "constraint": p["constraint"] or "",
                "missing_input": p["missing_input"] or "",
            }
            for p in contract["parameters"]
        ],
    }


def _reason_codes(blocking: Iterable[Any]) -> list[dict[str, str]]:
    """The blocking conditions as closed codes: never their text.

    A condition's `detail` is prose composed for a person, and a domain
    row's names its fault domain and the reason it was suppressed. What
    passes is the code, its scope, and -- for a site or domain row -- the
    site, which the S3 composition guarantees is one this agent reaches.
    Deduplicated: two suppressed domains at one site are one fact here.
    """
    seen: set[tuple[str, str, str]] = set()
    for row in blocking or ():
        if not isinstance(row, dict):
            continue
        code = row.get("code")
        code = code if code in GOVERNANCE_REASON_CODES else REASON_OTHER
        scope = row.get("scope")
        scope = scope if scope in _REASON_SCOPES else SCOPE_TENANT
        site = (row.get("site_id") or "") if scope != SCOPE_TENANT else ""
        seen.add((code, scope, site))
    out = []
    for code, scope, site in sorted(seen):
        entry = {"code": code, "scope": scope}
        if site:
            entry["site_id"] = site
        out.append(entry)
    return out


def _approval(
    *, exists: bool, bound: bool, governance: Optional[dict],
    budget_exhausted: bool, tenant_denied: bool, site_verdicts: set[str],
    sites_beyond_view: bool,
) -> dict[str, Any]:
    """Fact 7 (D3; S3-E1, A30.37). Every branch is a row of design §34m/§34r.

    Admission assesses a proposal against its TARGET site (A30.37), so the
    honest class-level answer is the per-site one: definitive only when
    every site in this agent's own composition gives the same answer AND
    no target lies at a site outside it; otherwise `depends_on_target_site`.
    The global gate is not asked here -- it never decides the mode
    (answer 1); it is fact 8's.
    """
    def _state(state: str, *basis: str) -> dict[str, Any]:
        return {"state": state, "basis": list(basis)}

    if not exists:
        return _state(APPROVAL_NOT_APPLICABLE, BASIS_NOT_GOVERNED)
    if not bound:
        return _state(APPROVAL_NOT_APPLICABLE, BASIS_NOT_BOUND)
    if governance is None:
        return _state(APPROVAL_UNKNOWN, BASIS_NOT_DESCRIBED)
    if tenant_denied:
        # A tenant-level denial (a fenced class, the tenant stop switch)
        # holds at every site, in view or not: definitive.
        return _state(APPROVAL_NOT_APPLICABLE, BASIS_GOVERNANCE_DENIED)
    if sites_beyond_view or len(site_verdicts) > 1:
        return _state(APPROVAL_UNKNOWN, BASIS_DEPENDS_ON_TARGET_SITE)
    # No site in the composition at all is the composite over nothing:
    # requires approval (unknown is never safe).
    verdict = next(iter(site_verdicts)) if site_verdicts else REQUIRES_APPROVAL
    if verdict == DENIED:
        return _state(APPROVAL_NOT_APPLICABLE, BASIS_GOVERNANCE_DENIED)
    if verdict == REQUIRES_APPROVAL:
        return _state(APPROVAL_REQUIRED, BASIS_GOVERNANCE_REQUIRES_APPROVAL)
    if verdict != AUTONOMOUS:
        return _state(APPROVAL_UNKNOWN, BASIS_NOT_DESCRIBED)
    if budget_exhausted:
        return _state(APPROVAL_REQUIRED, BASIS_EXECUTION_BUDGET_EXHAUSTED)
    return _state(APPROVAL_NOT_REQUIRED, BASIS_GOVERNANCE_AUTONOMOUS)


def _operability(blocked: set[str], unknown: set[str]) -> dict[str, Any]:
    """Fact 8: any known blocker is `not_operable`; else anything that cannot
    be stated definitively is `unknown`; else `operable` (D3)."""
    blocked_by = [code for code in BLOCKERS if code in blocked]
    unknowns = [code for code in UNKNOWNS if code in unknown]
    if blocked_by:
        state = NOT_OPERABLE
    elif unknowns:
        state = OPERABILITY_UNKNOWN
    else:
        state = OPERABLE
    return {"state": state, "blocked_by": blocked_by, "unknown": unknowns}


def _class_row(
    name: str,
    *,
    facts: dict,
    bound: set[str],
    conditions: dict[str, set[str]],
    registry_rows: dict,
    contract_rows: dict,
    site_rows: dict,
    devices_in_reach: int,
    agent,
    stop_switch_active: bool,
    agent_blockers: set[str],
    budget_exhausted: bool,
    sites_beyond_view: bool,
    gate,
    unattended_approved,
) -> dict[str, Any]:
    fact = facts.get(name)
    exists = fact is not None
    implemented = bool(fact and fact["implemented"])
    is_bound = name in bound
    addressable = _addressable(name, conditions)
    scope_fact = _in_effective_scope(
        implemented, registry_rows.get(name), devices_in_reach,
    )
    parameters = _parameters(name)
    gate = GATE_UNKNOWN if gate is None else require_verdict(gate)

    # Fact 6: only for a class this agent operates under (A30.6).
    governance = None
    row = contract_rows.get(name)
    site_verdicts: set[str] = set()
    tenant_denied = False
    if exists and is_bound and row is not None:
        verdict = effective_disposition(
            agent, row, stop_switch_active,
            unattended_approved=unattended_approved,
        )
        governance = {
            # The site-local assessment over this agent's composition
            # (A30.37); the gate and the conjunction beside it, never in it.
            "conclusion": verdict["disposition"],
            "reason_codes": _reason_codes(verdict["blocking_conditions"]),
            "budget_mapped": bool(row.get("budget_mapped")),
            "granted_at_level": row.get("granted_at_level"),
            "never_budget_grantable": bool(row.get("never_budget_grantable")),
            "global_safety": gate.as_dict(),
            "final_execution_eligibility": final_block(verdict["disposition"], gate),
        }
        tenant_denied = bool(stop_switch_active or row.get("never_budget_grantable"))
        # Each site in the composition, by the SAME rules admission applies
        # to a target there: its own local row, the agent's narrowing (D11
        # included) and `govern_proposal`'s suppression rule -- which only
        # narrows.
        for site_id, rows in site_rows.items():
            srow = rows.get(name)
            if srow is None:
                site_verdicts.add(REQUIRES_APPROVAL)
                continue
            answer = effective_disposition(
                agent, srow, stop_switch_active,
                unattended_approved=unattended_approved,
            )["disposition"]
            if answer != DENIED and site_id in _suppressed_sites(srow):
                answer = REQUIRES_APPROVAL
            site_verdicts.add(answer)

    approval = _approval(
        exists=exists, bound=is_bound, governance=governance,
        budget_exhausted=budget_exhausted, tenant_denied=tenant_denied,
        site_verdicts=site_verdicts, sites_beyond_view=sites_beyond_view,
    )

    blocked = set(agent_blockers)
    unknown: set[str] = set()
    if not exists:
        blocked.add("not_governed")
    elif not implemented:
        blocked.add("not_implemented")
    if not is_bound:
        blocked.add("not_bound")
    if exists:
        if addressable["path"] == PATH_CAMPAIGN:
            blocked.add("campaign_only")
        elif not addressable["value"]:
            blocked.add("not_addressable")
        if not parameters["resolvable"]:
            blocked.add("parameters_unavailable")
        state = scope_fact["state"]
        if state == REACH_NO_DEVICES:
            blocked.add("no_devices_in_scope")
        elif state == REACH_NONE:
            blocked.add("no_effective_reach")
        elif state == REACH_NOT_PERMITTED:
            blocked.add("not_permitted_on_any_node")
        elif state == REACH_UNKNOWN:
            unknown.add("executor_reach_undeclared")
    if governance is not None and approval["basis"] == [BASIS_GOVERNANCE_DENIED]:
        blocked.add("governance_denied")
    if exists and is_bound and not gate.clear:
        # S3-E1 (A30.37): execution is held -- on either basis (D3) -- while
        # the gate is not clear. One bounded code, and nothing under it.
        blocked.add(GLOBAL_SAFETY_CONSTRAINT)
    if approval["state"] == APPROVAL_UNKNOWN:
        # Only the reasons that leave PROGRESSION undecided. A class whose
        # approval depends on the target site progresses either way -- on
        # its own, or through a person -- so that basis is not one of them.
        unknown |= set(approval["basis"]) & {
            BASIS_ADMISSION_BEYOND_REACH, BASIS_NOT_DESCRIBED,
        }

    return {
        "action_type": name,
        "exists": exists,
        "risk": fact["risk"] if exists else None,
        "reversibility": fact["reversibility"] if exists else None,
        "inverse_action": fact["inverse_action"] if exists else None,
        "implemented": {
            "value": implemented,
            "by": list(fact["implemented_by"]) if exists else [],
        },
        "addressable": addressable,
        "bound": is_bound,
        "in_effective_scope": scope_fact,
        "parameters": parameters,
        "governance": governance,
        "approval_required": approval,
        "currently_operable": _operability(blocked, unknown),
    }


def build_discovery(
    *,
    agent,
    bound_classes: Iterable[str],
    reach,
    enforcement: str,
    registry: dict,
    contract: dict,
    catalogue: Iterable[Any],
    executions_used: int,
    proposals_today: int,
    site_contracts: dict,
    gate_verdicts: dict,
    unattended_approved,
    sites_beyond_view: bool,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Compose discovery for ONE agent reading itself. Pure: no I/O.

    `reach` is the agent's own `read_reach(scope, "fleet.view")`; `registry`
    is `load_capability_registry` over that reach and `contract` is
    `load_autonomy_contract(reach=reach)` -- both already composed over it,
    so nothing below narrows anything. `catalogue` is the tenant's condition
    catalogue (anything with `subsystem`, `action_type`, `enabled`).

    S3-E1 (A30.37): `site_contracts` are the site-local assessments of the
    sites in that same composition, one per site -- the answer admission
    reads for a target there. `gate_verdicts` is the closed gate's verdict
    per class over them. `unattended_approved` is D11's set.
    `sites_beyond_view` says a device this agent reads sits at a site outside
    the composition (a device or class grant), where no autonomy fact may be
    read (A30.26(j)) -- so no definitive per-site answer is possible there.
    """
    now = now or datetime.now(timezone.utc)
    facts = action_facts()
    bound = {str(c).upper() for c in bound_classes if c}
    names = sorted({a.value for a in ActionType} | bound)

    conditions: dict[str, set[str]] = {}
    for row in catalogue:
        if getattr(row, "enabled", False):
            conditions.setdefault(
                (getattr(row, "action_type", "") or "").upper(), set(),
            ).add(getattr(row, "subsystem", "") or "")

    registry_rows = {r["action_type"]: r for r in registry.get("classes") or ()}
    devices_in_reach = int((registry.get("fleet") or {}).get("devices_in_view") or 0)
    contract_rows = {r["action_type"]: r for r in contract.get("action_classes") or ()}
    posture = contract.get("posture") or {}
    safety = contract.get("safety_state") or {}
    stop_switch_active = bool((posture.get("stop_switch") or {}).get("active"))

    # The agent's own state, from the functions that act on it.
    budget = check_budget(agent, executions_used)
    budget_exhausted = bool(budget.get("exhausted"))
    left = proposal_budget_left(agent, proposals_today)
    paused = bool(getattr(agent, "paused_reason", "") or "")
    agent_blockers: set[str] = set()
    if getattr(agent, "status", "") != STATUS_ACTIVE:
        agent_blockers.add("agent_not_active")
    if paused:
        agent_blockers.add("agent_paused")
    if left <= 0:
        agent_blockers.add("proposal_budget_exhausted")

    beyond = admission_reads_beyond(reach)
    site_rows = {
        site_id: {
            r["action_type"]: r for r in (composed.get("action_classes") or ())
        }
        for site_id, composed in site_contracts.items()
    }

    return {
        "view": VIEW_MACHINE,
        "discovery_version": DISCOVERY_VERSION,
        "generated_at": now.isoformat(),
        "agent": {
            "id": agent.id,
            "species": ACTOR_AGENT,
            "configuration_version": int(agent.version),
            "status": agent.status,
            "paused": paused,
            "autonomy_ceiling": int(getattr(agent, "autonomy_ceiling", 0) or 0),
            "require_approval_always": bool(
                getattr(agent, "require_approval_always", True)
            ),
            "execution_budget": {
                "limit": int(budget.get("limit") or 0),
                "used": int(budget.get("used") or 0),
                "remaining": budget.get("remaining"),
                "period": getattr(agent, "budget_period", "daily") or "daily",
                "exhausted": budget_exhausted,
            },
            "proposal_budget": {
                "limit_per_day": int(getattr(agent, "max_proposals_per_day", 0) or 0),
                "used_today": int(proposals_today),
                "remaining_today": left,
            },
        },
        "scope": {
            "permission": DISCOVERY_PERMISSION,
            "enforcement": enforcement,
            "empty": reach.is_empty(),
            # Reach in its NATIVE types, never flattened into sites: a device
            # or class grant names no site, and no contextual site has a
            # field here to arrive through (A30.5, A29.9).
            "reach": {
                "tenant_wide": bool(reach.tenant_wide),
                "org_unit_ids": sorted(
                    unit_id for unit_id in reach.unit_paths
                    if reach.covers_org_unit_id(unit_id)
                ),
                "site_ids": sorted(reach.site_ids),
                "device_ids": sorted(reach.device_ids),
                "device_classes": sorted(reach.device_classes),
            },
            "devices_in_reach": devices_in_reach,
        },
        "governance_basis": {
            "discovery_composed_over": (
                COMPOSED_OVER_TENANT if reach.tenant_wide else COMPOSED_OVER_SITES
            ),
            "admission_composed_over": ADMISSION_COMPOSED_OVER,
            # Admission reads each target's own site (A30.37). Discovery's
            # per-site answers ARE that reading for every site in its
            # composition; a target outside it is the one case they are not.
            "matches_admission": not beyond and not sites_beyond_view,
            "sites_in_composition": len((contract.get("scope") or {}).get("sites") or ()),
            "tenant_stop_switch": stop_switch_active,
            "configured_level": int(posture.get("configured_level") or 0),
            # S3-E1 folds these into each site's local assessment (R3, D8):
            # a site that has not reported recently requires approval and a
            # halted one denies. Counted here; the answers are per class.
            "safety_reported": bool(safety.get("reported")),
            "every_site_reported": bool(safety.get("every_site_reported")),
            "sites_not_reporting": len(safety.get("sites_not_reporting") or ()),
            "sites_halted": len(safety.get("site_stop_switches") or ()),
        },
        "action_classes": [
            _class_row(
                name, facts=facts, bound=bound, conditions=conditions,
                registry_rows=registry_rows, contract_rows=contract_rows,
                site_rows=site_rows,
                devices_in_reach=devices_in_reach, agent=agent,
                stop_switch_active=stop_switch_active,
                agent_blockers=agent_blockers,
                budget_exhausted=budget_exhausted,
                sites_beyond_view=sites_beyond_view,
                gate=gate_verdicts.get(name),
                unattended_approved=unattended_approved,
            )
            for name in names
        ],
        "contract": dict(CONTRACT),
    }


#: What the response means, stated inside it. Constant text only.
CONTRACT: dict[str, str] = {
    "authority": (
        "Discovery is descriptive only. Nothing in this response is "
        "authority, and no field, value, timestamp or version in it is ever "
        "accepted back as proof of anything."
    ),
    "revalidation": (
        "At proposal, govern_proposal re-derives every fact from current "
        "state; at approval, the approval policy is evaluated; at dispatch, "
        "current authority is revalidated; at the node, its own funnel "
        "remains the final execution authority."
    ),
    "facts": (
        "Eight independent facts per action class: exists, implemented, "
        "addressable, bound, in_effective_scope, governance, "
        "approval_required and currently_operable. They are never combined "
        "into a single permission."
    ),
    "addressable": (
        "The enabled condition catalogue, or the campaign-only mechanism, "
        "names this class. It does not mean in scope, permitted, autonomous, "
        "executable or currently operable."
    ),
    "conclusion_basis": (
        "Governance is composed over this agent's own authorized reach. "
        "Admission assesses each proposal against its target site's own "
        "local assessment, so a class-level answer is definitive only when "
        "every site in that reach agrees and no target lies outside it; "
        "otherwise it depends on the target site."
    ),
    "approval_states": (
        "required: a definitive current conclusion. not_required: definitive "
        "under current production semantics. unknown: the answer depends on "
        "the target site, or cannot be described. not_applicable: denied, "
        "or not applicable to this agent."
    ),
    "global_safety": (
        "A closed global safety gate holds execution, whatever the approval, "
        "while it is not clear. It is reported as one bounded code, "
        "GLOBAL_SAFETY_CONSTRAINT, and never names what or where. It never "
        "changes whether approval is required."
    ),
    "operability": (
        "Central Command readiness at generated_at: operable, not_operable "
        "(a known blocker) or unknown. Never a statement that execution will "
        "succeed; the Site Manager's lease, preconditions, blast radius and "
        "stop switch, and the node's allow list, decide that at execution."
    ),
    "parameters": (
        "Resolved by Central Command from reported evidence. The ingress "
        "accepts no parameters, and executor defaults are not shown."
    ),
}
