"""The autonomy contract: what may run without a human in this tenant, and why.

S5 (2026-08-29). This module owns the GOVERNANCE SUBSTRATE for action —
the layer that sits between "this actor may address this capability"
(RBAC) and "this specific action may run on this device right now"
(preconditions, lease, blast radius, stop switch).

Three axes, deliberately never merged
-------------------------------------
  PERMISSION      may this ACTOR address the capability at all
                  (fleet.view / action.approve / site.manage — identical
                  for humans and agents, tenant-scoped, Keycloak-issued)
  AUTONOMY        may this ACTION CLASS proceed without a human decision
                  in this tenant                          <-- this module
  EXECUTION GATES may this SPECIFIC ACTION run right now
                  (node funnel — unchanged, and never overridden here)

**Autonomy is not permission, and autonomy is not execution
authorization.** A level-3 tenant still fails on preconditions; an
`action.approve` holder still cannot bypass a failed one (A10.3). What
this contract produces is a PREDICTION an actor may plan with, never a
grant. The node funnel remains the only thing that authorizes execution.

One contract, many consumers
----------------------------
The Console is the first consumer of `build_autonomy`; the Operational
Agent (A0/A1) is the second and gets nothing extra. If this composition
lived in the browser, every future consumer would re-derive it and drift
from what the operator sees. Same argument that put attention at CC.

The ladder below is also the SINGLE SOURCE OF TRUTH for the CC -> SM
policy push: `harkeniq_cc.policy_push` imports `grants_for_level` rather
than keeping its own copy, and a test fails if the two ever disagree.

Boundaries this module must never move
--------------------------------------
No action whose risk class is "high" is ever grantable by an autonomy
budget (FIRMWARE_UPDATE, FIRMWARE_ROLLBACK, INTERFACE_RESET,
INTERFACE_DISABLE). They keep their dedicated approval paths: campaign
approval per OQ-21, T1 quorum + SM + CC approval per A9. This is stated
as a derived rule over ACTION_RISK rather than a hand-kept list so a new
high-risk action type is fenced the moment it is classified.

Action classes that are neither granted nor structurally fenced are
reported as `not_budget_mapped` — visible, named, and recorded in the
capability roadmap (design doc S11) as registry candidates. S5 reports
that gap; it does not close it by widening a boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

#: Bump when a consumer would have to change to read the payload.
CONTRACT_VERSION = "1"

# ---------------------------------------------------------------------------
# The ladder (A10.4, ratified). Declared once, consumed everywhere.
# ---------------------------------------------------------------------------

LEVEL_OBSERVE = 0
LEVEL_SUGGEST = 1
LEVEL_BATCH = 2
LEVEL_AUTONOMOUS = 3

#: level 2 grants these action classes, bounded by the budget's limit/period.
LEVEL_2_ACTIONS: dict[str, str] = {"SEL_CLEAR": "low", "BMC_RESET": "low"}

#: level 3 additionally grants these.
LEVEL_3_ACTIONS: dict[str, str] = {
    "POWER_CYCLE": "medium",
    "POWER_CAP_ADJUST": "medium",
    "CONFIG_RESTORE": "medium",
}

#: Risk class that no autonomy budget may ever grant. Derived rule, not a
#: list: classify an action "high" and it is fenced automatically.
NEVER_GRANTABLE_RISK = "high"

#: Advancement evidence bar, per design doc S5 ("95% over 50+, per class").
#: Deliberately the same numbers as the skill promotion gate — one bar for
#: "the fleet has earned this", whatever is being earned.
PROMOTION_SUCCESS_RATE = 0.95
PROMOTION_MIN_EXECUTIONS = 50

#: Below this many outcomes we refuse to characterise a success rate at
#: all. Matches the SM error-budget model, which will not judge a class
#: on fewer than five outcomes either.
MIN_EVIDENCE_OUTCOMES = 5

# Dispositions
AUTONOMOUS = "autonomous"
REQUIRES_APPROVAL = "requires_approval"
DENIED = "denied"
NOT_BUDGET_MAPPED = "not_budget_mapped"

# Blocking-condition scopes. Only tenant/site scope changes a class's
# disposition; domain scope is contextual (suppression fences a fault
# domain, not an action class) and an actor must evaluate it per target.
SCOPE_TENANT = "tenant"
SCOPE_SITE = "site"
SCOPE_DOMAIN = "domain"

LADDER: list[dict[str, Any]] = [
    {
        "level": LEVEL_OBSERVE,
        "name": "observe",
        "grants": [],
        "statement": "Diagnosis only. Nothing runs without a human decision.",
    },
    {
        "level": LEVEL_SUGGEST,
        "name": "suggest",
        "grants": [],
        "statement": (
            "The system proposes remediation with evidence. Every action "
            "still waits for a named human."
        ),
    },
    {
        "level": LEVEL_BATCH,
        "name": "batch",
        "grants": sorted(LEVEL_2_ACTIONS),
        "statement": (
            "Low-risk recovery runs unattended within the budget: clearing "
            "a full event log, resetting an unresponsive BMC."
        ),
    },
    {
        "level": LEVEL_AUTONOMOUS,
        "name": "autonomous",
        "grants": sorted(set(LEVEL_2_ACTIONS) | set(LEVEL_3_ACTIONS)),
        "statement": (
            "Adds medium-risk recovery that interrupts service on one "
            "device: power cycle, power cap change, config restore."
        ),
    },
]


def grants_for_level(level: int) -> dict[str, str]:
    """Action classes an autonomy budget at ``level`` grants: {type: risk}.

    THE mapping. `policy_push` builds the SM payload from this and the
    API reports from this, so the posture an operator reads and the
    policy an enforcer receives cannot drift apart.
    """
    if level < LEVEL_BATCH:
        return {}
    granted = dict(LEVEL_2_ACTIONS)
    if level >= LEVEL_AUTONOMOUS:
        granted.update(LEVEL_3_ACTIONS)
    return granted


def granted_at_level(action_type: str) -> Optional[int]:
    """Lowest level that grants this class, or None if never grantable."""
    if action_type in LEVEL_2_ACTIONS:
        return LEVEL_BATCH
    if action_type in LEVEL_3_ACTIONS:
        return LEVEL_AUTONOMOUS
    return None


def never_budget_grantable(risk: str) -> bool:
    """True when no autonomy budget may ever grant this action's class."""
    return (risk or "").lower() == NEVER_GRANTABLE_RISK


def action_risk_map() -> dict[str, str]:
    """{ACTION_TYPE: risk} for every action the platform can execute.

    Sourced from the agent's own A2.1 classification so the contract can
    never describe a class the executor does not have, or miss one it does.
    """
    from harkeniq.autonomy.preconditions import ACTION_RISK

    return {a.value: risk for a, risk in ACTION_RISK.items()}


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def _evidence_for(action_type: str, outcomes: Iterable[dict]) -> dict[str, Any]:
    """Outcome evidence for one action class. Counts only, no modelling."""
    total = success = failure = resolved = 0
    sites: set[str] = set()
    for oc in outcomes:
        if oc.get("action_type") != action_type:
            continue
        total += 1
        result = (oc.get("outcome") or "").upper()
        if result == "SUCCESS":
            success += 1
        elif result in ("FAILURE", "ROLLBACK", "ROLLBACK_TRIGGERED"):
            failure += 1
        if oc.get("fault_resolved"):
            resolved += 1
        if oc.get("site_id"):
            sites.add(oc["site_id"])
    sufficient = total >= MIN_EVIDENCE_OUTCOMES
    return {
        "executions": total,
        "success": success,
        "failure": failure,
        # A rate over four outcomes is noise dressed as a measurement.
        "success_rate": round(success / total, 4) if sufficient else None,
        "resolution_rate": round(resolved / total, 4) if sufficient else None,
        "sites_observed": len(sites),
        "sufficient": sufficient,
        "window": "all_time",
    }


def _advancement(
    action_type: str,
    risk: str,
    grant_level: Optional[int],
    configured_level: int,
    evidence: dict,
    dropped_back: bool,
) -> dict[str, Any]:
    """What would move this class up, stated as a distance, not a promise."""
    if never_budget_grantable(risk):
        return {
            "next_level": None,
            "gate": "not_available",
            "qualified_on_evidence": False,
            "blocked_by": ["never_budget_grantable"],
            "statement": (
                "Never granted by an autonomy budget at any level. This "
                "class runs only through its own approval path — campaign "
                "approval for firmware, T1 quorum plus Site Manager and "
                "Central Command approval for interface changes."
            ),
        }
    if grant_level is None:
        return {
            "next_level": None,
            "gate": "roadmap",
            "qualified_on_evidence": False,
            "blocked_by": ["not_budget_mapped"],
            "statement": (
                "No autonomy level maps this class today. It is recorded "
                "as a capability-registry candidate; mapping it is a "
                "product decision, not an evidence threshold."
            ),
        }
    if configured_level >= grant_level:
        # The level already grants this class. Advancement is no longer the
        # question — but "granted" is the wrong word if something withdrew
        # it, so say what must clear instead of implying it is running.
        if dropped_back:
            return {
                "next_level": None,
                "gate": "operator_review",
                "qualified_on_evidence": False,
                "blocked_by": ["error_budget_dropped_back"],
                "statement": (
                    f"Level {configured_level} grants this class, but the "
                    "error budget withdrew it after repeated failures. An "
                    "operator must review the failures and clear the "
                    "drop-back at the site before it runs unattended again."
                ),
            }
        return {
            "next_level": None,
            "gate": "granted",
            "qualified_on_evidence": True,
            "blocked_by": [],
            "statement": f"Granted at the tenant's configured level {configured_level}.",
        }

    executions = evidence["executions"]
    rate = evidence["success_rate"]
    blocked: list[str] = []
    if executions < PROMOTION_MIN_EXECUTIONS:
        blocked.append("insufficient_executions")
    if rate is None:
        blocked.append("insufficient_evidence")
    elif rate < PROMOTION_SUCCESS_RATE:
        blocked.append("success_rate_below_threshold")
    if dropped_back:
        blocked.append("error_budget_dropped_back")

    needed = max(0, PROMOTION_MIN_EXECUTIONS - executions)
    if not blocked:
        statement = (
            f"Evidence qualifies this class for level {grant_level}. "
            "Raising the level is a human decision."
        )
    elif rate is None:
        statement = (
            f"{needed} more executions before this class can be judged; "
            f"level {grant_level} needs >={PROMOTION_SUCCESS_RATE:.0%} success "
            f"over >={PROMOTION_MIN_EXECUTIONS} executions."
        )
    else:
        parts = []
        if needed:
            parts.append(f"{needed} more executions")
        if rate < PROMOTION_SUCCESS_RATE:
            parts.append(
                f"success rate {rate:.0%} must reach "
                f"{PROMOTION_SUCCESS_RATE:.0%} (currently below)"
            )
        if dropped_back:
            parts.append("the error-budget drop-back must be cleared")
        statement = (
            f"Level {grant_level} needs " + ", then ".join(parts) +
            ". A human then raises the level; evidence never raises it."
        )

    return {
        "next_level": grant_level,
        "gate": "human_ratified",
        "needs_executions": PROMOTION_MIN_EXECUTIONS,
        "needs_success_rate": PROMOTION_SUCCESS_RATE,
        "qualified_on_evidence": not blocked,
        "blocked_by": blocked,
        "statement": statement,
    }


# ---------------------------------------------------------------------------
# The composer
# ---------------------------------------------------------------------------


def _iso(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def build_autonomy(
    *,
    tenant_id: str,
    actor_id: str,
    actor_species: str,
    permissions: Iterable[str],
    budgets: Iterable[Any],
    stop_switch: Any,
    outcomes: Iterable[dict],
    safety_rows: Iterable[Any],
    sites: Iterable[Any],
    learned_signals: Iterable[Any] = (),
    approval_policies: Iterable[Any] = (),
    site_id: Optional[str] = None,
    action_type: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Compose the governance contract. Pure: no I/O, no clock of its own.

    `safety_rows` are per-site CCSafetyState rows (or anything with the
    same attributes). A site that has not reported is reported as
    UNKNOWN, never as safe — an unobserved safety state is the one thing
    a governance layer must not round down.
    """
    now = now or datetime.now(timezone.utc)
    held = set(permissions or ())
    wildcard = "*" in held
    outcomes = list(outcomes)
    safety_rows = list(safety_rows)
    sites = list(sites)

    if site_id:
        safety_rows = [s for s in safety_rows if s.site_id == site_id]
        sites = [s for s in sites if s.id == site_id]
        outcomes = [o for o in outcomes if o.get("site_id") == site_id]

    # -- posture ------------------------------------------------------------
    # Only the device_type="*" row shapes site-wide policy; SM enforcement
    # has no device dimension, so a device-scoped row is reported but never
    # treated as the tenant's level.
    fleet_budget = next(
        (b for b in budgets if getattr(b, "device_type", "*") == "*"), None
    )
    configured_level = int(getattr(fleet_budget, "level", 0) or 0) if fleet_budget else 0
    stop_active = bool(getattr(stop_switch, "active", False)) if stop_switch else False

    reported_sites = {s.site_id for s in safety_rows if getattr(s, "reported", True)}
    site_rows = [
        {
            "id": s.id,
            "name": getattr(s, "site_name", "") or "",
            "safety_reported": s.id in reported_sites,
            "safety_as_of": next(
                (_iso(r.as_of) for r in safety_rows if r.site_id == s.id), None
            ),
        }
        for s in sites
    ]

    # -- safety state, folded per action class ------------------------------
    error_budgets: dict[str, dict[str, Any]] = {}
    site_budget_remaining: dict[str, dict[str, int]] = {}
    suppressions: list[dict[str, Any]] = []
    site_stop: list[dict[str, Any]] = []
    for row in safety_rows:
        for entry in (getattr(row, "error_budgets", None) or []):
            at = entry.get("action_type", "")
            if not at:
                continue
            agg = error_budgets.setdefault(
                at,
                {"dropped_back": False, "total": 0, "success": 0,
                 "failure": 0, "sites_dropped_back": []},
            )
            agg["total"] += int(entry.get("total_count", 0) or 0)
            agg["success"] += int(entry.get("success_count", 0) or 0)
            agg["failure"] += int(entry.get("failure_count", 0) or 0)
            if entry.get("dropped_back"):
                agg["dropped_back"] = True
                agg["sites_dropped_back"].append(row.site_id)
        for at, remaining in (getattr(row, "site_budgets", None) or {}).items():
            site_budget_remaining.setdefault(at, {})[row.site_id] = remaining
        for dom in (getattr(row, "suppressions", None) or []):
            suppressions.append({**dom, "site_id": row.site_id})
        if getattr(row, "sm_stop_switch", False):
            site_stop.append({"site_id": row.site_id, "active": True})

    for agg in error_budgets.values():
        agg["success_rate"] = (
            round(agg["success"] / agg["total"], 4) if agg["total"] else None
        )

    # Learned knowledge, indexed by the class it speaks about.
    learning_by_action: dict[str, list[dict]] = {}
    for sig in learned_signals:
        at = (getattr(sig, "action_type", "") or "").upper()
        if not at:
            continue
        learning_by_action.setdefault(at, []).append({
            "signal_id": getattr(sig, "id", "") or getattr(sig, "signal_key", ""),
            "statement": getattr(sig, "statement", ""),
            "confidence": getattr(sig, "confidence", None),
            "scope_type": getattr(sig, "scope_type", ""),
            "scope_ref": getattr(sig, "scope_ref", ""),
        })

    policy_by_action: dict[str, Any] = {}
    for pol in approval_policies:
        at = (getattr(pol, "action_type", "") or "*").upper()
        if getattr(pol, "status", "active") != "active":
            continue
        policy_by_action.setdefault(at, pol)

    # -- one row per action class -------------------------------------------
    risks = action_risk_map()
    classes: list[dict[str, Any]] = []
    for at in sorted(risks):
        if action_type and at != action_type.upper():
            continue
        risk = risks[at]
        grant_level = granted_at_level(at)
        fenced = never_budget_grantable(risk)
        mapped = grant_level is not None
        evidence = _evidence_for(at, outcomes)
        eb = error_budgets.get(at)
        dropped = bool(eb and eb["dropped_back"])

        blocking: list[dict[str, Any]] = []
        if fenced:
            disposition = DENIED
            reason = (
                f"risk class {risk} is never granted by an autonomy budget"
            )
            blocking.append({
                "code": "never_budget_grantable",
                "detail": reason,
                "scope": SCOPE_TENANT,
            })
        elif not mapped:
            disposition = NOT_BUDGET_MAPPED
            reason = "no autonomy level maps this action class"
            blocking.append({
                "code": "not_budget_mapped",
                "detail": reason,
                "scope": SCOPE_TENANT,
            })
        else:
            reason = ""
            if stop_active:
                blocking.append({
                    "code": "stop_switch_active",
                    "detail": "the tenant stop switch denies all autonomous action",
                    "scope": SCOPE_TENANT,
                })
            if configured_level < grant_level:
                blocking.append({
                    "code": "level_below_grant",
                    "detail": (
                        f"tenant autonomy level {configured_level} is below "
                        f"level {grant_level}, which grants this class"
                    ),
                    "scope": SCOPE_TENANT,
                })
            if dropped:
                for sid in eb["sites_dropped_back"]:
                    blocking.append({
                        "code": "error_budget_dropped_back",
                        "detail": (
                            "success rate fell below the error budget; "
                            "autonomy for this class was withdrawn automatically"
                        ),
                        "scope": SCOPE_SITE,
                        "site_id": sid,
                    })
            exhausted = [
                sid for sid, rem in site_budget_remaining.get(at, {}).items()
                if rem == 0
            ]
            for sid in exhausted:
                blocking.append({
                    "code": "budget_window_exhausted",
                    "detail": "the budget for this window is spent at this site",
                    "scope": SCOPE_SITE,
                    "site_id": sid,
                })
            if stop_active:
                disposition = DENIED
                reason = "stop switch active"
            elif blocking:
                disposition = REQUIRES_APPROVAL
                reason = blocking[0]["detail"]
            else:
                disposition = AUTONOMOUS
                reason = (
                    f"granted at level {grant_level}; tenant is configured "
                    f"at level {configured_level}"
                )

        # Suppression fences a fault domain, not a class. Reported against
        # every class as context an actor must evaluate per target; it does
        # not change the class disposition.
        for dom in suppressions:
            blocking.append({
                "code": "domain_suppressed",
                "detail": (
                    f"fault domain {dom.get('domain_id', '')} is suppressed "
                    f"({dom.get('trigger_reason', 'correlated conclusion')})"
                ),
                "scope": SCOPE_DOMAIN,
                "site_id": dom.get("site_id", ""),
                "domain_id": dom.get("domain_id", ""),
            })

        policy = policy_by_action.get(at) or policy_by_action.get("*")
        classes.append({
            "action_type": at,
            "risk": risk,
            "required_permission": {
                "observe": "fleet.view",
                "approve": "action.approve",
                "change_posture": "site.manage",
            },
            "granted_at_level": grant_level,
            "budget_mapped": mapped,
            "never_budget_grantable": fenced,
            "disposition": disposition,
            "disposition_reason": reason,
            "blocking_conditions": blocking,
            "evidence": evidence,
            "learning": learning_by_action.get(at, []),
            "safety": {
                # An unreported site cannot vouch for safety; say so.
                "reported": bool(reported_sites),
                "error_budget": (
                    {k: v for k, v in eb.items() if k != "sites_dropped_back"}
                    | {"sites_dropped_back": eb["sites_dropped_back"]}
                ) if eb else None,
                "suppressed_domains": [
                    d for d in suppressions
                ],
                "site_budget_remaining": site_budget_remaining.get(at, {}),
            },
            "approval": {
                "required": disposition != AUTONOMOUS,
                "mode": getattr(policy, "approval_mode", "require_approval"),
                "required_approvers": int(
                    getattr(policy, "required_approvers", 1) or 1
                ),
                "policy_id": getattr(policy, "id", None),
            },
            "advancement": _advancement(
                at, risk, grant_level, configured_level, evidence, dropped,
            ),
        })

    return {
        "contract_version": CONTRACT_VERSION,
        "generated_at": now.isoformat(),
        "actor": {
            "identity": actor_id,
            "species": actor_species,
            "tenant_id": tenant_id,
            "may_observe": wildcard or "fleet.view" in held,
            "may_approve": wildcard or "action.approve" in held,
            "may_change_posture": wildcard or "site.manage" in held,
        },
        "scope": {
            "tenant_id": tenant_id,
            "site_id": site_id,
            "sites": site_rows,
        },
        "posture": {
            "stop_switch": {
                "active": stop_active,
                "changed_by": getattr(stop_switch, "changed_by", "") or "",
                # CCStopSwitch stamps `updated_at` on every flip; that IS the
                # change time. Reading a `changed_at` that does not exist gave
                # a silent null on the live stack.
                "changed_at": _iso(getattr(stop_switch, "updated_at", None)),
                "sites_reporting_active": len(site_stop),
            },
            "configured_level": configured_level,
            "level_source": "budget_row" if fleet_budget else "unconfigured",
            "budget_limit": int(getattr(fleet_budget, "budget_limit", 0) or 0),
            "budget_period": getattr(fleet_budget, "budget_period", "") or "",
            "actions_used": int(getattr(fleet_budget, "actions_used", 0) or 0),
            "device_scoped_budgets": [
                {
                    "device_type": b.device_type,
                    "level": b.level,
                    "enforced": False,
                }
                for b in budgets
                if getattr(b, "device_type", "*") != "*"
            ],
            "ladder": LADDER,
        },
        "safety_state": {
            "reported": bool(reported_sites),
            "sites_reporting": sorted(reported_sites),
            "sites_not_reporting": sorted(
                s["id"] for s in site_rows if not s["safety_reported"]
            ),
            "suppressions": suppressions,
            "error_budgets": [
                {"action_type": at, **vals} for at, vals in sorted(error_budgets.items())
            ],
            "site_stop_switches": site_stop,
        },
        "action_classes": classes,
    }
