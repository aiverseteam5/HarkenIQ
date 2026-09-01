"""A2: activation readiness for an Operational Agent.

Pure. Every input is fetched by the router or the runner and handed in,
so the whole judgement is unit-testable without a database -- the same
shape as `autonomy.build_autonomy`, `capabilities.build_capability_registry`
and `campaigns`, deliberately, because an operator will hold these side
by side.

WHAT A PREFLIGHT IS
-------------------
The answer to the question an operator actually has before switching an
agent on: *if I activate this, what will it do, where, and what will
stop it?* It is a CONTRACT, not a UI checklist -- machine-readable,
stored, version-bound and testable. Activation without one is refused.

Twelve dimensions are reported, each with one of four verdicts::

    READY    this dimension is satisfied
    BLOCKED  activation is refused until this changes
    WARN     activation may proceed, but a named human must accept it
    UNKNOWN  the platform cannot currently tell -- never read as either
             satisfied or failed

UNKNOWN is a first-class answer, exactly as it is in the Capability
Registry. A fleet mid-upgrade is UNKNOWN, not incapable, and treating
the two alike would make an agent unconfigurable for the duration.

THREE THINGS THIS MODULE MAY NOT DO
-----------------------------------
It confers nothing. A READY preflight is a statement about
configuration, not a grant: every proposal still passes the autonomy
contract, the approval ledger and the node's own funnel.

It owns no capability model (the Registry does), no approval model (the
E0.1 ledger does) and no execution path (`DispatchAction` onto the node
funnel does).

And it never invents runtime data. Every field reported here is
something the runtime actually produces; a dimension the platform cannot
observe reads UNKNOWN rather than being filled with a plausible value.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from harkeniq_cc.autonomy import AUTONOMOUS, DENIED

# -- verdicts ----------------------------------------------------------------

READY = "ready"
BLOCKED = "blocked"
WARN = "warn"
UNKNOWN = "unknown"

#: Verdict precedence when rolling twelve dimensions into one answer.
#: BLOCKED dominates: one refused dimension refuses the activation. WARN
#: outranks UNKNOWN because a warning is something a human can act on
#: now, and burying it under "we're not sure" would lose it.
_PRECEDENCE = {BLOCKED: 3, WARN: 2, UNKNOWN: 1, READY: 0}

# -- the twelve dimensions, in the order an operator reads them --------------

DIMENSIONS = (
    "identity",
    "tenant",
    "scope",
    "capabilities",
    "skills",
    "autonomy_ceiling",
    "approval_policy",
    "budget",
    "safety",
    "executor_reach",
    "configuration_version",
    "activation_state",
)


def roll_up(verdicts: Iterable[str]) -> str:
    """One answer from many. BLOCKED dominates; READY only if all agree."""
    worst = READY
    for verdict in verdicts:
        if _PRECEDENCE.get(verdict, 0) > _PRECEDENCE[worst]:
            worst = verdict
    return worst


def _row(name: str, verdict: str, detail: str, **extra: Any) -> dict:
    return {"dimension": name, "verdict": verdict, "detail": detail, **extra}


# -- individual dimensions ---------------------------------------------------


def check_identity(agent, realm_ok: Optional[bool]) -> dict:
    """Is this agent a principal the platform can still resolve?

    E1.4's lesson, made explicit: a realm migration orphaned every scope
    grant and the tenant was locked out INCLUDING its administrator,
    while everything looked fine. A silent lockout is indistinguishable
    from correct strict mode, so an unresolvable identity is refused BY
    NAME rather than quietly producing an agent that sees nothing.
    """
    if agent.status == "retired":
        return _row("identity", BLOCKED,
                    "this agent is retired; a retired agent cannot be activated")
    if realm_ok is None:
        return _row("identity", UNKNOWN,
                    "the platform could not resolve this agent's grants; "
                    "treat as unknown, not as valid")
    if not realm_ok:
        return _row(
            "identity", BLOCKED,
            "this agent's scope grants do not resolve in the tenant's current "
            "realm, so activating it would produce an agent that sees nothing "
            "while appearing configured",
        )
    return _row("identity", READY, f"agent {agent.id} resolves in this tenant")


def check_tenant(agent, tenant_id: str) -> dict:
    if agent.tenant_id != tenant_id:
        return _row("tenant", BLOCKED, "this agent belongs to another tenant")
    return _row("tenant", READY, f"tenant {tenant_id}")


def check_scope(scope_rows, in_scope_devices) -> dict:
    """No rows means no devices (A0); no devices means it would see nothing."""
    rows = list(scope_rows)
    devices = list(in_scope_devices)
    if not rows:
        return _row("scope", BLOCKED,
                    "no scope rows: this agent would see no devices at all",
                    scope_rows=0, devices=0)
    if not devices:
        return _row(
            "scope", BLOCKED,
            "the configured scope currently resolves to no devices, so this "
            "agent would observe nothing and propose nothing",
            scope_rows=len(rows), devices=0,
        )
    return _row("scope", READY,
                f"{len(devices)} device(s) across {len(rows)} scope row(s)",
                scope_rows=len(rows), devices=len(devices))


def check_capabilities(bound_classes, class_rows: dict, reach: dict) -> dict:
    """What the agent is bound to, and whether anything can perform it.

    Binding already refuses a class no executor implements (A17), so the
    interesting cases here are the ones that changed since: reach that
    became provably zero, and reach that is merely unknown.
    """
    bound = sorted(bound_classes)
    if not bound:
        return _row("capabilities", BLOCKED,
                    "no action class bound: this agent would propose nothing",
                    bound=[])
    implemented = reach.get("implemented") or set()
    unknown = bool(reach.get("unknown"))
    unreachable = [c for c in bound if c not in implemented]
    if unreachable and not unknown:
        return _row(
            "capabilities", BLOCKED,
            f"no device in scope implements {', '.join(unreachable)}",
            bound=bound, unreachable=unreachable,
        )
    if unreachable and unknown:
        return _row(
            "capabilities", UNKNOWN,
            f"{', '.join(unreachable)} is not implemented by any device that "
            f"has declared, and some devices have not declared yet",
            bound=bound, unreachable=unreachable,
        )
    missing_rows = [c for c in bound if c not in class_rows]
    if missing_rows:
        return _row("capabilities", UNKNOWN,
                    f"the autonomy contract describes no row for "
                    f"{', '.join(missing_rows)}", bound=bound)

    # A5 (A22.5): implemented and permitted is not the same as PROPOSABLE.
    # A class whose required parameter nothing in this platform can supply
    # will never produce a proposal, and a preflight that called that
    # READY would be telling a customer their agent is switched on and
    # working when it can only ever do nothing.
    from harkeniq.capabilities import parameter_contract

    unsatisfiable = {
        name: parameter_contract(name)["unsatisfiable_reason"]
        for name in bound
        if not parameter_contract(name)["agent_resolvable"]
    }
    if unsatisfiable and len(unsatisfiable) == len(bound):
        return _row(
            "capabilities", BLOCKED,
            "every bound class requires a parameter this platform cannot "
            f"supply, so this agent would propose nothing: "
            f"{'; '.join(unsatisfiable.values())}",
            bound=bound, unsatisfiable=sorted(unsatisfiable),
        )
    if unsatisfiable:
        return _row(
            "capabilities", WARN,
            f"{', '.join(sorted(unsatisfiable))} will never be proposed: "
            f"{'; '.join(unsatisfiable.values())}",
            bound=bound, unsatisfiable=sorted(unsatisfiable),
        )
    return _row("capabilities", READY,
                f"{len(bound)} class(es) bound and implemented in scope",
                bound=bound)


def check_executor_reach(bound_classes, per_device: list[dict]) -> dict:
    """Can the devices in scope actually run these classes RIGHT NOW?

    Three states kept apart, exactly as the Registry keeps them: no code
    (already blocked at binding), code but the node does not permit it
    (WARN -- policy is not capability, and the node's refusal becomes
    evidence), and undeclared (UNKNOWN -- never zero).
    """
    bound = set(bound_classes)
    if not per_device:
        return _row("executor_reach", UNKNOWN,
                    "no device reach could be evaluated", devices=[])
    permitted, warned, undeclared = 0, [], 0
    for row in per_device:
        if row.get("declared") is False:
            undeclared += 1
            continue
        effective = set(row.get("effective") or ())
        implemented = set(row.get("implemented") or ())
        if bound & effective:
            permitted += 1
        elif bound & implemented:
            warned.append(row.get("device_agent_id"))
    if permitted:
        verdict, detail = READY, (
            f"{permitted} device(s) both implement and permit a bound class"
        )
        if warned:
            verdict = WARN
            detail = (
                f"{permitted} device(s) ready; {len(warned)} implement a bound "
                f"class but their node does not currently permit it"
            )
    elif warned:
        verdict, detail = WARN, (
            f"{len(warned)} device(s) implement a bound class but no node "
            f"currently permits it; the agent will propose and the node will "
            f"refuse, and that refusal becomes attributed evidence"
        )
    elif undeclared:
        verdict, detail = UNKNOWN, (
            f"{undeclared} device(s) have not declared their capabilities; "
            f"reach is unknown, which is not the same as zero"
        )
    else:
        verdict, detail = BLOCKED, "no device in scope can perform a bound class"
    return _row("executor_reach", verdict, detail,
                permitted=permitted, warned=warned, undeclared=undeclared)


def check_autonomy(agent, bound_classes, class_rows: dict) -> dict:
    """The ceiling limits UNATTENDED behaviour, never existence (D1/3).

    An agent whose every class needs a human is a perfectly good agent.
    Refusing to activate it would conflate autonomy with permission,
    which is the distinction the whole trust ladder rests on.
    """
    unattended, attended, denied = [], [], []
    for name in sorted(bound_classes):
        row = class_rows.get(name) or {}
        disposition = row.get("disposition")
        if disposition == AUTONOMOUS and not agent.require_approval_always \
                and int(agent.autonomy_ceiling or 0) > 0:
            unattended.append(name)
        elif disposition == DENIED:
            denied.append(name)
        else:
            attended.append(name)
    detail = (
        f"ceiling {agent.autonomy_ceiling}; "
        f"{len(unattended)} class(es) may run unattended, "
        f"{len(attended)} need a human"
    )
    if denied:
        detail += f", {len(denied)} denied by the tenant contract"
    return _row("autonomy_ceiling", READY, detail,
                unattended=unattended, attended=attended, denied=denied)


def activation_grants_unattended(agent, bound_classes, class_rows: dict) -> list[str]:
    """The classes activation would let run WITHOUT a human (D1).

    This is the whole trigger for activation approval. Turning on an
    agent whose every action needs a human grants no new authority, so
    gating it is ceremony; turning on one that can act unattended is the
    moment real authority is conferred, and that is what a person should
    be asked about.
    """
    if agent.require_approval_always or int(agent.autonomy_ceiling or 0) <= 0:
        return []
    return sorted(
        name for name in bound_classes
        if (class_rows.get(name) or {}).get("disposition") == AUTONOMOUS
    )


def check_approval(agent, unattended: list[str]) -> dict:
    """Does switching this on need a named human? (D1, derived.)"""
    if unattended:
        return _row(
            "approval_policy", WARN,
            f"activation confers unattended execution for "
            f"{', '.join(unattended)}, so it requires approval on the "
            f"existing approvals queue",
            activation_approval_required=True, unattended=unattended,
        )
    return _row(
        "approval_policy", READY,
        "activation grants no unattended execution, so it needs no separate "
        "activation approval; every proposal still requires a human",
        activation_approval_required=False, unattended=[],
    )


def check_budget(agent, executions_used: int) -> dict:
    """Per-agent EXECUTION budget (D2).

    Counts actions actually executed under this agent's attribution, the
    way S5 budgets count actions -- not proposals. A proposal that is
    never executed consumes nothing, because intent is not consumption.

    Exhaustion stops UNATTENDED execution only. It means "this agent has
    spent its delegated unattended allowance", never "this agent is
    disabled": observation, reasoning, proposing and human-approved
    operation all continue.
    """
    limit = int(getattr(agent, "execution_budget", 0) or 0)
    if limit <= 0:
        return _row("budget", READY,
                    "no per-agent execution budget configured; the tenant and "
                    "site budgets still apply",
                    limit=0, used=int(executions_used), remaining=None)
    remaining = max(0, limit - int(executions_used))
    period = getattr(agent, "budget_period", "daily") or "daily"
    if remaining == 0:
        return _row(
            "budget", WARN,
            f"the {period} execution budget of {limit} is spent, so nothing "
            f"runs unattended until it resets; the agent still observes, "
            f"proposes, and executes what a human approves",
            limit=limit, used=int(executions_used), remaining=0, exhausted=True,
        )
    return _row("budget", READY,
                f"{remaining} of {limit} execution(s) remaining this {period}",
                limit=limit, used=int(executions_used), remaining=remaining,
                exhausted=False)


def check_safety(agent, stop_switch_active: bool, safety_reported: bool) -> dict:
    """Per-agent pause plus the platform switches it lives under.

    Nothing here can loosen a platform gate. A tenant or site stop
    switch stops the agent whatever this says.
    """
    if getattr(agent, "paused_reason", ""):
        return _row("safety", BLOCKED,
                    f"this agent is paused: {agent.paused_reason}",
                    paused=True)
    if stop_switch_active:
        return _row("safety", BLOCKED,
                    "the tenant stop switch is active; nothing this agent "
                    "proposes could run unattended", stop_switch=True)
    if not safety_reported:
        return _row("safety", UNKNOWN,
                    "no site has reported live safety state, so suppressions "
                    "and error budgets read UNKNOWN, never clear")
    return _row("safety", READY, "no stop switch, no pause, safety reported")


def check_skills(skill_rows: list[dict]) -> dict:
    """Skills are governed COMPOSITIONS, not permissions.

    A skill may not expand RBAC, scope, capability, autonomy or approval
    authority. What it can do is recommend an action -- and a skill
    recommending an action the executor cannot perform is unusable, so
    it is refused at binding or marked unusable before activation rather
    than discovered at dispatch.
    """
    if not skill_rows:
        return _row("skills", READY, "no skills bound", bound=0)
    unusable = [s for s in skill_rows if s.get("usable") is False]
    unknown = [s for s in skill_rows if s.get("usable") is None]
    if unusable:
        return _row(
            "skills", BLOCKED,
            f"{len(unusable)} skill(s) recommend an action no device in scope "
            f"can perform: " + ", ".join(s["skill_id"] for s in unusable),
            bound=len(skill_rows), unusable=[s["skill_id"] for s in unusable],
        )
    if unknown:
        return _row("skills", UNKNOWN,
                    f"{len(unknown)} skill(s) could not be validated against "
                    f"executor reach yet", bound=len(skill_rows))
    return _row("skills", READY, f"{len(skill_rows)} skill(s) validated",
                bound=len(skill_rows))


def check_configuration_version(agent, preflight_version: Optional[int]) -> dict:
    """Is the stored preflight the one THIS configuration needs? (D3.)

    Bound to the version it was produced for. Editing an agent bumps the
    version and invalidates it, because otherwise somebody acknowledges
    v1 and the estate runs v2.
    """
    if preflight_version is None:
        return _row("configuration_version", WARN,
                    f"no preflight has been run for version {agent.version}",
                    version=agent.version, preflight_version=None)
    if int(preflight_version) != int(agent.version):
        return _row(
            "configuration_version", BLOCKED,
            f"the stored preflight was produced for version {preflight_version} "
            f"and this agent is now version {agent.version}; re-run preflight",
            version=agent.version, preflight_version=int(preflight_version),
        )
    return _row("configuration_version", READY,
                f"preflight matches configuration version {agent.version}",
                version=agent.version, preflight_version=int(preflight_version))


def check_activation_state(agent) -> dict:
    if agent.status == "active":
        return _row("activation_state", READY, "already active")
    if agent.status == "retired":
        return _row("activation_state", BLOCKED, "retired agents cannot activate")
    return _row("activation_state", READY, f"status {agent.status!r}")


def build_preflight(
    *,
    agent,
    tenant_id: str,
    scope_rows: Iterable[Any],
    in_scope_devices: Iterable[Any],
    bound_classes: Iterable[str],
    skill_rows: list[dict],
    class_rows: dict,
    reach: dict,
    per_device_reach: list[dict],
    executions_used: int,
    stop_switch_active: bool,
    safety_reported: bool,
    realm_ok: Optional[bool],
    preflight_version: Optional[int] = None,
) -> dict:
    """The authoritative activation readiness contract.

    Machine-readable and human-readable at once: every dimension carries
    a verdict a caller can branch on and a sentence a person can act on.
    It is stored, version-bound and testable -- deliberately not a UI
    checklist, because the Console is one consumer of this and the
    activation gate is another, and if they could disagree an operator
    would approve something different from what runs.

    It confers nothing. READY is a statement about configuration, not a
    grant: every proposal still passes the autonomy contract, the
    approval ledger and the node's own funnel.
    """
    bound = sorted(set(bound_classes))
    unattended = activation_grants_unattended(agent, bound, class_rows)

    dimensions = [
        check_identity(agent, realm_ok),
        check_tenant(agent, tenant_id),
        check_scope(scope_rows, in_scope_devices),
        check_capabilities(bound, class_rows, reach),
        check_skills(skill_rows),
        check_autonomy(agent, bound, class_rows),
        check_approval(agent, unattended),
        check_budget(agent, executions_used),
        check_safety(agent, stop_switch_active, safety_reported),
        check_executor_reach(bound, per_device_reach),
        check_configuration_version(agent, preflight_version),
        check_activation_state(agent),
    ]
    by_name = {d["dimension"]: d for d in dimensions}
    overall = roll_up(d["verdict"] for d in dimensions)

    # A WARN is not a veto, but it is not nothing either: a named human
    # must accept it before activation, exactly as S6 requires for a
    # warned target. Without that, "warn" degrades into a colour on a
    # page nobody reads.
    warned = [d["dimension"] for d in dimensions if d["verdict"] == WARN]
    unknowns = [d["dimension"] for d in dimensions if d["verdict"] == UNKNOWN]
    blocked = [d["dimension"] for d in dimensions if d["verdict"] == BLOCKED]

    return {
        "agent_id": agent.id,
        "tenant_id": tenant_id,
        "configuration_version": int(agent.version),
        "overall": overall,
        "can_activate": overall != BLOCKED,
        "requires_acknowledgement": bool(warned or unknowns),
        "requires_activation_approval": bool(unattended),
        "unattended_classes": unattended,
        "blocked_dimensions": blocked,
        "warn_dimensions": warned,
        "unknown_dimensions": unknowns,
        "dimensions": dimensions,
        "by_dimension": {k: v["verdict"] for k, v in by_name.items()},
        "contract": {
            "authority": (
                "A READY preflight describes configuration; it grants "
                "nothing. Every proposal still passes the autonomy "
                "contract, the approval ledger and the node's own funnel, "
                "and the node remains the final execution authority."
            ),
            "unknown": (
                "UNKNOWN means the platform cannot currently tell. It is "
                "never read as satisfied and never as failed; a fleet "
                "mid-upgrade is unknown, not incapable."
            ),
            "versioning": (
                "This preflight is bound to configuration version "
                f"{int(agent.version)}. Editing the agent bumps the version "
                "and invalidates both this result and any acknowledgement "
                "taken against it."
            ),
        },
    }


#: Activation provenance: can the platform name the configuration this
#: agent is actually running?
PROV_RECORDED = "recorded"
PROV_UNKNOWN = "unknown"
PROV_INACTIVE = "inactive"


def activation_provenance(agent) -> dict:
    """Is this agent running a configuration we can name? (A19.9.)

    ONE computation, because the detail view and the runtime view both
    report drift and two copies of a rule diverge. Three answers, not
    two:

      inactive  not running, so there is nothing to drift.
      recorded  activation wrote the version it switched on, so drift is
                knowable and `active AND activated_version == version`
                is exactly "no drift".
      unknown   active, but `activated_version` is 0 -- it was activated
                before A2 recorded it. We do NOT know what is running.

    The last case is why this is not a one-line comparison. `version`
    starts at 1, so an upgraded pre-A2 agent compares 0 against 3 and a
    naive rule shouts DRIFT at every existing agent the moment a customer
    upgrades -- asserting a fact the platform does not have.

    Backfilling `activated_version = version` in the migration would be
    the same error in the other direction: it would assert these agents
    are running their current configuration, which nobody checked.
    Unknown is not zero and it is not a guess (A17.4); it is reported by
    name and an operator resolves it by re-running preflight.
    """
    version = int(getattr(agent, "version", 0) or 0)
    activated = int(getattr(agent, "activated_version", 0) or 0)
    if getattr(agent, "status", "") != "active":
        provenance = PROV_INACTIVE
    elif activated <= 0:
        provenance = PROV_UNKNOWN
    else:
        provenance = PROV_RECORDED
    return {
        "activated_version": activated,
        "activation_provenance": provenance,
        "configuration_drifted": (
            provenance == PROV_RECORDED and activated != version
        ),
    }


def preflight_is_current(preflight_row, agent) -> bool:
    """Does a stored preflight still describe this configuration? (D3.)"""
    if preflight_row is None:
        return False
    return int(preflight_row.configuration_version) == int(agent.version)


def acknowledgement_is_current(agent) -> bool:
    """Is the stored activation acknowledgement still the one needed?

    Same rule as the campaign's, and for the same reason: an edit must
    not silently carry a human's acceptance forward onto a configuration
    they never saw.
    """
    if not getattr(agent, "activation_acknowledged_by", ""):
        return False
    return int(getattr(agent, "activation_acknowledged_version", 0) or 0) == int(
        agent.version
    )


def may_activate(preflight: dict, agent) -> tuple[bool, str]:
    """The activation gate, stated once so every caller agrees."""
    if preflight is None:
        return False, (
            "activation preflight is mandatory: an agent cannot be activated "
            "without a stored, reviewable readiness result"
        )
    if int(preflight.get("configuration_version", -1)) != int(agent.version):
        return False, (
            f"the stored preflight is for configuration version "
            f"{preflight.get('configuration_version')} and this agent is now "
            f"version {agent.version}; re-run preflight"
        )
    if preflight.get("overall") == BLOCKED:
        return False, (
            "activation is blocked by: "
            + ", ".join(preflight.get("blocked_dimensions") or [])
        )
    if preflight.get("requires_acknowledgement") and not acknowledgement_is_current(
        agent
    ):
        return False, (
            "this preflight carries warnings or unknowns that a named person "
            "must accept or resolve before activation: "
            + ", ".join(
                (preflight.get("warn_dimensions") or [])
                + (preflight.get("unknown_dimensions") or [])
            )
        )
    return True, ""


def unattended_permitted(agent, executions_used: int) -> tuple[bool, str]:
    """May this agent act WITHOUT a human right now? (D2.)

    Budget exhaustion stops unattended execution and nothing else. The
    agent keeps observing, keeps proposing and keeps executing what a
    person approves -- a spent allowance is not a disabled agent.
    """
    if getattr(agent, "paused_reason", ""):
        return False, f"agent is paused: {agent.paused_reason}"
    if agent.require_approval_always:
        return False, "this agent requires a human for every action"
    if int(agent.autonomy_ceiling or 0) <= 0:
        return False, "this agent's autonomy ceiling is zero"
    limit = int(getattr(agent, "execution_budget", 0) or 0)
    if limit > 0 and int(executions_used) >= limit:
        return False, (
            f"this agent has spent its execution budget ({limit}); it may "
            f"still propose, and a human may still approve"
        )
    return True, ""


# ---------------------------------------------------------------------------
# Skills as governed compositions
# ---------------------------------------------------------------------------
#
# A skill is a COMPOSITION over capabilities that already exist, never a
# permission. Binding one may not expand RBAC, scope, capability,
# autonomy or approval authority -- it can only compose what the agent
# was already entitled to.
#
# What it CAN do is recommend an action, and that is exactly where it
# has to be governed: a skill recommending an action the executor cannot
# perform is unusable, so it is refused at binding or marked unusable
# before activation rather than discovered at dispatch. The Capability
# Registry answers "can the executor do this"; there is no skill-specific
# capability model and there must never be one.


def skill_recommended_actions(definition: Any) -> set[str]:
    """Every ActionType a skill's rules can recommend.

    Read from the parsed SkillDefinition rather than a declaration
    alongside it, so a skill cannot claim one thing and recommend
    another. `parse_skill` remains the untrusted-YAML safety boundary.
    """
    actions: set[str] = set()
    for rule in getattr(definition, "rules", []) or []:
        action = getattr(rule, "action", None)
        if action is None:
            continue
        action_type = getattr(action, "type", None)
        if action_type:
            actions.add(str(action_type).upper())
    return actions


def validate_skill_against_reach(
    skill_id: str,
    recommended: Iterable[str],
    platform_implemented: set,
    scope_implemented: set,
    scope_unknown: bool,
    catalogue_classes: Optional[set] = None,
) -> dict:
    """Is this skill usable by THIS agent, on the devices it reaches?

    `usable` is True / False / None, and None means UNKNOWN -- some
    device has not declared, so the answer cannot be given yet and must
    not be guessed either way.

    A skill that recommends nothing (pure diagnosis) is always usable:
    it composes observation, and observation needs no executor.

    A4 (A21.8) adds ONE rule and no authority: a skill may recommend only
    actions the tenant's capability catalogue names. A skill is a
    composition over capabilities the agent already holds -- recommending
    a class the tenant never mapped to any condition would be the skill
    introducing a capability by the back door, which is the one thing a
    skill may never do. `None` means "no catalogue supplied", which skips
    the check rather than refusing everything.
    """
    recommended = sorted({str(a).upper() for a in recommended})
    if not recommended:
        return {"skill_id": skill_id, "usable": True, "recommended": [],
                "unsupported": [],
                "reason": "recommends no action; diagnosis only"}
    if catalogue_classes is not None:
        uncatalogued = [a for a in recommended if a not in catalogue_classes]
        if uncatalogued:
            return {
                "skill_id": skill_id, "usable": False,
                "recommended": recommended, "unsupported": uncatalogued,
                "reason": (
                    f"recommends {', '.join(uncatalogued)}, which this "
                    f"tenant's capability catalogue does not map to any "
                    f"condition; a skill composes capabilities the agent "
                    f"already holds and cannot introduce one"
                ),
            }
    absent = [a for a in recommended if a not in platform_implemented]
    if absent:
        return {"skill_id": skill_id, "usable": False,
                "recommended": recommended, "unsupported": absent,
                "reason": (f"recommends {', '.join(absent)}, which no executor "
                           f"in this platform implements")}
    unreachable = [a for a in recommended if a not in scope_implemented]
    if unreachable and scope_unknown:
        return {"skill_id": skill_id, "usable": None,
                "recommended": recommended, "unsupported": unreachable,
                "reason": (f"recommends {', '.join(unreachable)}, which no "
                           f"DECLARED device in scope implements, and some "
                           f"devices have not declared yet")}
    if unreachable:
        return {"skill_id": skill_id, "usable": False,
                "recommended": recommended, "unsupported": unreachable,
                "reason": (f"recommends {', '.join(unreachable)}, which no "
                           f"device in this agent's scope implements")}
    return {"skill_id": skill_id, "usable": True, "recommended": recommended,
            "unsupported": [],
            "reason": "every recommended action is implemented in scope"}


def skill_install_targets(
    skill_recommends: Iterable[str], per_device_reach: list
) -> dict:
    """Which in-scope devices should actually receive this skill.

    A skill installs where it can do something. A device whose protocol
    cannot perform what the skill recommends is REPORTED, not silently
    skipped -- an operator who sees "installed" for an agent covering
    forty devices needs to know it reached thirty-one and why.

    An undeclared device receives it: unknown is not incapable, and the
    node's own allow list remains the final authority anyway.
    """
    recommends = {str(a).upper() for a in skill_recommends}
    install, skip = [], []
    for row in per_device_reach:
        device = row.get("device_agent_id")
        if row.get("declared") is False:
            install.append(device)
            continue
        implemented = {str(a).upper() for a in (row.get("implemented") or ())}
        if not recommends or recommends <= implemented:
            install.append(device)
        else:
            skip.append({"device_agent_id": device,
                         "reason": ("this device's protocol does not implement "
                                    + ", ".join(sorted(recommends - implemented)))})
    return {"install": sorted(install), "skip": skip}


def activation_subject_ref(
    agent_id: str, configuration_version: int, unattended: Iterable[str]
) -> str:
    """The approval subject for activating THIS configuration (D1).

    A digest over the agent, its configuration version and the classes
    activation would let run unattended. Binding is therefore
    structural: change the version or the unattended set and the digest
    no longer addresses this subject, so an approval cannot survive the
    edit it was not given for.

    `cc_approval_records.subject_ref` is 64 characters, so this is a
    digest and the readable mapping lives on the agent row.
    """
    import hashlib

    from harkeniq.audit.chain import canonical_json

    payload = canonical_json({
        "agent_id": agent_id,
        "configuration_version": int(configuration_version),
        "unattended": sorted(str(c) for c in unattended),
    })
    return hashlib.sha256(payload).hexdigest()[:32]


# ---------------------------------------------------------------------------
# D3: an approved proposal is not a guaranteed execution
# ---------------------------------------------------------------------------
#
#   Approved proposal version  !=  guaranteed execution.
#
# It means: "this proposal was authorized against THIS agent
# configuration version". Execution still requires the current hard
# gates, every time.
#
# So a V3 proposal keeps its V3 meaning and its V3 attribution -- it is
# never silently reinterpreted as V4 -- and it is never silently
# executed just because somebody once approved it. A revoked scope, an
# invalid identity, an active stop switch or a failed precondition may
# still refuse it, and the node's own allow list refuses it last.

#: The gates re-evaluated at dispatch, whatever version the proposal
#: carries. Ordered cheapest and most absolute first, like
#: DECISION_INPUTS at the Site Manager.
DISPATCH_GATES = (
    "agent_identity",
    "agent_active",
    "tenant_scope",
    "stop_switch",
    "budget",
)


def dispatch_permitted(**gates) -> tuple[bool, str]:
    """May a previously approved proposal dispatch RIGHT NOW?

    Each keyword is either ``True``/``None`` (this gate does not object)
    or a string saying why it refuses. A gate nobody supplied is treated
    as NOT EVALUATED and therefore refusing -- the same fail-closed
    default `execution_permitted` uses at the Site Manager, and for the
    same reason: an unevaluated governing input must never read as
    consent.

    These are Central Command's gates only. The Site Manager's lease,
    preconditions and blast radius, and the node's own allow list, run
    afterwards and independently. Nothing here can substitute for them.
    """
    for name in DISPATCH_GATES:
        if name not in gates:
            return False, (
                f"{name} was never evaluated; an unevaluated gate is a "
                f"refusal, not a pass"
            )
        verdict = gates[name]
        if verdict is True or verdict is None:
            continue
        if verdict is False:
            return False, f"{name} refused"
        return False, str(verdict)
    return True, ""


def proposal_version_is_honoured(proposal, agent) -> tuple[bool, str]:
    """A V3 proposal stays V3. It is never reinterpreted as V4.

    Returns whether the proposal's configuration is still coherent
    enough to act on, and why not when it is not. The version itself is
    never rewritten -- attribution must keep naming the configuration
    the decision was actually made against, or an outcome lies about
    what produced it.
    """
    from harkeniq_cc.operational_agent import parse_attribution

    parsed = parse_attribution(getattr(proposal, "actor", "") or "")
    if parsed is None:
        return False, "this proposal carries no agent attribution"
    agent_id, version = parsed
    if agent_id != agent.id:
        return False, "this proposal belongs to a different agent"
    # A superseded configuration is fine: the decision was coherent when
    # it was made, and the hard gates below still apply. What is NOT
    # fine is pretending it was made under the current one.
    return True, f"proposal retains configuration version {version}"
