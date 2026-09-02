"""A2: the governed Operational Agent lifecycle, wired.

CREATE -> CONFIGURE -> PREFLIGHT -> ACKNOWLEDGE -> APPROVAL (where
required) -> ACTIVATE -> RUN -> OBSERVE.

The stateful half of A2. Every judgement it makes is imported from
`agent_activation` (pure) or from a governance composer that already
exists; what lives here is the fetching, the sequencing and the
persistence.

The readiness contract is assembled SERVER-SIDE and stored. The Console
consumes it and never recreates it -- if the page could compute its own
verdicts, an operator would approve something different from what the
activation gate enforces, and the divergence would be invisible until
it mattered.

It creates no capability model (the Registry), no approval model (the
E0.1 ledger), no scope model (E1.2) and no execution path
(`DispatchAction` onto the node funnel).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from harkeniq.capabilities import action_facts, effective_actions

from harkeniq_cc.agent_activation import (
    BLOCKED,
    activation_provenance,
    build_preflight,
    skill_install_targets,
    validate_skill_against_reach,
)
from harkeniq_cc.capabilities import implemented_actions, reachable_action_classes
from harkeniq_cc.db.repos import (
    AgentPreflightRepo,
    AuditRepo,
    FleetCacheRepo,
    OperationalAgentRepo,
    SiteRepo,
    StopSwitchRepo,
)
from harkeniq_cc.governance import load_agent_scope, load_autonomy_contract
from harkeniq_cc.operational_agent import (
    KIND_ACTION_CLASS,
    KIND_SKILL,
    attribution_key,
    bound_action_classes,
    resolve_scope,
)

logger = logging.getLogger("harkeniq.cc.agent_lifecycle")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def budget_window_start(period: str, now: Optional[datetime] = None) -> datetime:
    """The start of the current budget window (D2)."""
    now = now or _utcnow()
    if (period or "daily") == "weekly":
        return now - timedelta(days=7)
    if period == "monthly":
        return now - timedelta(days=30)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def executions_used(session, tenant_id: str, agent) -> int:
    """How much of this agent's execution budget is spent (A19.7).

    ONE notion of consumption, so the preflight's report, the runtime
    view and the dispatch gate can never quote different numbers at an
    operator -- a budget that says "3 of 5" on one screen and refuses on
    another is worse than no budget.

    Two parts, and both are executions:

      settled    outcome history under this agent's attribution key --
                 the existing execution accounting, unchanged.
      in flight  proposals already dispatched unattended and not yet
                 settled. They are running; the allowance is spent.

    Proposals, by contrast, are never counted. Intent is not
    consumption: a proposal that is never executed costs nothing.

    Counted per AGENT, across its configuration versions. Attribution
    still records the exact version that decided each outcome -- D3
    requires that -- but the allowance belongs to the agent, or an
    ordinary edit would refill a spent budget.
    """
    from harkeniq_cc.db.repos import AgentProposalRepo

    since = budget_window_start(agent.budget_period)
    settled = await AgentPreflightRepo(session).count_executions(
        tenant_id, agent.id, since,
    )
    in_flight = await AgentProposalRepo(session).count_in_flight(
        tenant_id, agent.id, since,
    )
    return int(settled) + int(in_flight)


async def _per_device_reach(devices, bound: set[str]) -> list[dict]:
    """What each in-scope device declares, kept as three separate facts.

    `implemented` and `effective` stay apart all the way through: "no
    code for it" and "this node does not permit it" are different
    problems with different fixes, and the preflight has to be able to
    say which one an operator has.
    """
    rows = []
    for device in devices:
        declaration = getattr(device, "capabilities", None)
        implemented = implemented_actions(declaration)
        permitted = effective_actions(declaration)
        rows.append({
            "device_agent_id": device.agent_id,
            "device_name": getattr(device, "agent_name", "") or device.agent_id,
            "site_id": device.site_id,
            "declared": implemented is not None,
            "implemented": sorted(implemented) if implemented is not None else None,
            "effective": sorted(permitted) if permitted is not None else None,
        })
    return rows


async def _validate_skills(
    session, state, *, tenant_id: str, skill_refs: list[str],
    reach: dict, platform_implemented: set[str],
    catalogue_classes: Optional[set[str]] = None,
) -> list[dict]:
    """Fetch each bound skill and judge it against executor reach.

    A skill is a COMPOSITION over capabilities that already exist. It may
    not expand permission, scope, capability, autonomy or approval
    authority -- and the one thing it can do, recommend an action, is
    exactly what gets governed here.

    The YAML is fetched over the EXISTING CC->Console internal channel
    (`/api/internal`, `console_api_key`, established by R5-2), so no new
    trust direction is introduced. `parse_skill` stays the untrusted-YAML
    safety boundary.
    """
    from harkeniq_cc.skill_fetch import fetch_skill_definition

    rows: list[dict] = []
    for skill_id in skill_refs:
        definition, error = await fetch_skill_definition(state, tenant_id, skill_id)
        if definition is None:
            rows.append({
                "skill_id": skill_id, "usable": None, "recommended": [],
                "unsupported": [], "reason": error or "skill could not be fetched",
            })
            continue
        from harkeniq_cc.agent_activation import skill_recommended_actions

        recommended = skill_recommended_actions(definition)
        row = validate_skill_against_reach(
            skill_id, recommended, platform_implemented,
            set(reach.get("implemented") or ()), bool(reach.get("unknown")),
            catalogue_classes=catalogue_classes,
        )
        row["name"] = getattr(definition, "name", skill_id)
        row["version"] = str(getattr(definition, "version", "") or "")
        rows.append(row)
    return rows


async def run_preflight(
    session, state, *, tenant_id: str, agent, actor: str, actor_ref: str = ""
) -> dict:
    """Assemble every dimension and store the result immutably.

    Mandatory before activation. An operator must never be the first
    person to discover, after switching an agent on, that a third of its
    scope cannot run what it is bound to.
    """
    repo = OperationalAgentRepo(session)
    pre_repo = AgentPreflightRepo(session)

    scope_rows = await repo.list_scopes(agent.id)
    caps = await repo.list_capabilities(agent.id)
    bound = bound_action_classes(caps)
    skill_refs = sorted(
        c.capability_ref for c in caps if c.kind == KIND_SKILL
    )

    # E1.2: the agent's reach comes from the SAME resolver a human's
    # does, expanded through the org tree, then flattened by the one
    # `resolve_scope` the evaluator uses. No second scope model.
    realm_ok: Optional[bool] = None
    resolved_site_ids: list[str] = []
    try:
        agent_scope = await load_agent_scope(
            session, tenant_id=tenant_id, agent_id=agent.id
        )
        resolved_site_ids = list(agent_scope.site_ids)
        realm_ok = True
    except Exception:  # noqa: BLE001 -- an unresolvable identity is UNKNOWN
        logger.warning("could not resolve scope for agent %s", agent.id)
        realm_ok = None

    devices = await FleetCacheRepo(session).list_all(tenant_id)
    in_scope = resolve_scope(scope_rows, devices, resolved_site_ids)
    if realm_ok is True and scope_rows and not in_scope and devices:
        # Grants that resolve to nothing while the tenant HAS devices is
        # the E1.4 orphaned-grant shape. Reported, not guessed at.
        realm_ok = True

    per_device = await _per_device_reach(in_scope, set(bound))
    reach = reachable_action_classes(in_scope)
    facts = action_facts()
    platform_implemented = {k for k, v in facts.items() if v["implemented"]}

    contract = await load_autonomy_contract(
        session, tenant_id=tenant_id,
        actor_id=attribution_key(agent.id, agent.version),
        actor_species="agent", permissions=["fleet.view"],
    )
    class_rows = {
        row["action_type"]: row for row in contract.get("action_classes", [])
    }
    stop = await StopSwitchRepo(session).get(tenant_id)
    safety = contract.get("safety_state") or {}

    # A21.8: a skill may recommend only what this tenant's catalogue maps.
    from harkeniq_cc.db.repos import CapabilityCatalogueRepo

    catalogue_classes = {
        r.action_type
        for r in await CapabilityCatalogueRepo(session).list_for_tenant(tenant_id)
        if r.enabled
    }
    skill_rows = await _validate_skills(
        session, state, tenant_id=tenant_id, skill_refs=skill_refs,
        reach=reach, platform_implemented=platform_implemented,
        catalogue_classes=catalogue_classes,
    )

    executions = await executions_used(session, tenant_id, agent)

    result = build_preflight(
        agent=agent, tenant_id=tenant_id, scope_rows=scope_rows,
        in_scope_devices=in_scope, bound_classes=bound, skill_rows=skill_rows,
        class_rows=class_rows, reach=reach, per_device_reach=per_device,
        executions_used=executions,
        stop_switch_active=bool(stop is not None and getattr(stop, "active", False)),
        safety_reported=bool(safety.get("reported")),
        realm_ok=realm_ok, preflight_version=agent.version,
    )
    result["skills"] = skill_rows
    result["devices"] = per_device

    await pre_repo.supersede_all(agent.id)
    await pre_repo.store(
        agent_id=agent.id, tenant_id=tenant_id,
        configuration_version=agent.version,
        overall=result["overall"], can_activate=result["can_activate"],
        requires_acknowledgement=result["requires_acknowledgement"],
        requires_activation_approval=result["requires_activation_approval"],
        result=result, produced_by=actor,
    )
    # A fresh preflight supersedes any previous acknowledgement: the set
    # of warnings may have changed under it.
    agent.activation_acknowledged_by = ""
    agent.activation_acknowledged_at = None
    agent.activation_acknowledged_version = 0
    await session.flush()

    await AuditRepo(session).append(
        actor=actor, action="operational_agent.preflighted",
        subject=agent.id, tenant_id=tenant_id,
        detail={
            "configuration_version": agent.version,
            "overall": result["overall"],
            "blocked": result["blocked_dimensions"],
            "warn": result["warn_dimensions"],
            "unknown": result["unknown_dimensions"],
            "requires_activation_approval": result["requires_activation_approval"],
            "unattended_classes": result["unattended_classes"],
        },
    )
    return result


async def acknowledge_preflight(
    session, *, tenant_id: str, agent, actor: str, actor_ref: str = ""
) -> dict:
    """A named human accepts this configuration's warnings and unknowns.

    Bound to the version it was given for. Editing the agent bumps that
    version and this stops counting, because otherwise somebody
    acknowledges one configuration and the estate runs another.
    """
    pre_repo = AgentPreflightRepo(session)
    row = await pre_repo.current(agent.id)
    if row is None or int(row.configuration_version) != int(agent.version):
        raise ValueError(
            "run preflight for this configuration version before acknowledging it"
        )
    agent.activation_acknowledged_by = actor
    agent.activation_acknowledged_at = _utcnow()
    agent.activation_acknowledged_version = int(agent.version)
    await session.flush()

    result = row.result or {}
    await AuditRepo(session).append(
        actor=actor, action="operational_agent.acknowledged",
        subject=agent.id, tenant_id=tenant_id,
        detail={
            "configuration_version": agent.version,
            "warn": result.get("warn_dimensions") or [],
            "unknown": result.get("unknown_dimensions") or [],
        },
    )
    return {
        "acknowledged_by": actor,
        "configuration_version": agent.version,
        "warn": result.get("warn_dimensions") or [],
        "unknown": result.get("unknown_dimensions") or [],
    }


async def install_bound_skills(
    session, state, *, tenant_id: str, agent, preflight: dict, actor: str,
    actor_ref: str = "",
) -> dict:
    """Deliver every usable bound skill onto the devices that can use it.

    Per DEVICE and attributable: one ledger row per (agent, version,
    skill, device), so a re-activation cannot install twice and an
    operator can see exactly which devices received what.

    A device whose protocol cannot perform the skill's recommended
    actions is SKIPPED WITH A REASON, never silently omitted -- an
    operator reading "installed" for a forty-device agent needs to know
    it reached thirty-one of them and why.
    """
    from harkeniq_cc.skill_fetch import fetch_skill_definition
    from harkeniq_cc.sm_client import SMClient

    pre_repo = AgentPreflightRepo(session)
    per_device = {d["device_agent_id"]: d for d in (preflight.get("devices") or [])}
    out = {"installed": 0, "skipped": 0, "skills": []}

    for skill in preflight.get("skills") or []:
        if skill.get("usable") is False:
            out["skipped"] += 1
            out["skills"].append({
                "skill_id": skill["skill_id"], "installed": 0,
                "reason": skill.get("reason", "unusable"),
            })
            continue
        definition, error = await fetch_skill_definition(
            state, tenant_id, skill["skill_id"]
        )
        if definition is None:
            out["skills"].append({
                "skill_id": skill["skill_id"], "installed": 0,
                "reason": error or "could not fetch",
            })
            continue

        targets = skill_install_targets(
            skill.get("recommended") or [], list(per_device.values())
        )
        by_site: dict[str, list[str]] = {}
        for device_id in targets["install"]:
            row = per_device.get(device_id) or {}
            by_site.setdefault(row.get("site_id", ""), []).append(device_id)

        queued = 0
        for site_id, devices in by_site.items():
            site = await SiteRepo(session).get_by_id(site_id)
            if site is None or site.tenant_id != tenant_id:
                continue
            fresh = [
                d for d in devices
                if not await pre_repo.already_installed(
                    agent.id, agent.version, skill["skill_id"], d
                )
            ]
            if not fresh:
                continue
            try:
                ack = await SMClient(state.config.sm_tls_ca).install_skill(
                    site.sm_endpoint, site.sm_token or "",
                    tenant_id=tenant_id, site_id=site_id,
                    skill_name=skill["skill_id"],
                    skill_version=str(skill.get("version") or "1"),
                    yaml_content=getattr(definition, "raw_yaml", "") or "",
                    tier="community", validation_state="tested",
                    issued_by=attribution_key(agent.id, agent.version),
                    device_agent_ids=fresh,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("skill %s install failed: %s", skill["skill_id"], exc)
                ack = {"accepted": False, "reason": str(exc)}
            for device_id in fresh:
                await pre_repo.record_install(
                    agent_id=agent.id, agent_version=agent.version,
                    skill_id=skill["skill_id"], device_agent_id=device_id,
                    site_id=site_id,
                    skill_version=str(skill.get("version") or "1"),
                    status="queued" if ack.get("accepted") else "failed",
                    detail=str(ack.get("reason", ""))[:512],
                )
            if ack.get("accepted"):
                queued += len(fresh)

        for skipped in targets["skip"]:
            await pre_repo.record_install(
                agent_id=agent.id, agent_version=agent.version,
                skill_id=skill["skill_id"],
                device_agent_id=skipped["device_agent_id"],
                site_id=(per_device.get(skipped["device_agent_id"]) or {}).get(
                    "site_id", ""
                ),
                status="skipped", detail=skipped["reason"][:512],
            )
        out["installed"] += queued
        out["skipped"] += len(targets["skip"])
        out["skills"].append({
            "skill_id": skill["skill_id"], "installed": queued,
            "skipped": [s["device_agent_id"] for s in targets["skip"]],
        })

    await session.flush()
    if out["skills"]:
        await AuditRepo(session).append(
            actor=actor, action="operational_agent.skills_installed",
            subject=agent.id, tenant_id=tenant_id,
            detail={"configuration_version": agent.version, **out},
        )
    return out


async def runtime_state(session, *, tenant_id: str, agent) -> dict:
    """What the runtime can HONESTLY say about this agent.

    Only signals the platform actually produces. A dimension it cannot
    observe reads UNKNOWN rather than being filled with a plausible
    value -- inventing health is worse than admitting ignorance, because
    an operator acts on it.
    """
    from harkeniq_cc.db.repos import AgentProposalRepo

    repo = OperationalAgentRepo(session)
    pre_repo = AgentPreflightRepo(session)
    actor = attribution_key(agent.id, agent.version)

    window_start = budget_window_start(agent.budget_period)
    executions = await executions_used(session, tenant_id, agent)
    proposals = await AgentProposalRepo(session).count_since(
        tenant_id, agent.id, window_start,
    )

    scope_rows = await repo.list_scopes(agent.id)
    resolved: list[str] = []
    try:
        resolved = list(
            (await load_agent_scope(
                session, tenant_id=tenant_id, agent_id=agent.id
            )).site_ids
        )
    except Exception:  # noqa: BLE001
        resolved = []
    devices = resolve_scope(
        scope_rows, await FleetCacheRepo(session).list_all(tenant_id), resolved
    )
    now = _utcnow()
    fresh, stale, unknown_seen = 0, 0, 0
    for device in devices:
        last_seen = getattr(device, "last_seen_at", None)
        if last_seen is None:
            # The site has never reported a reading for this device. The
            # honest answer is UNKNOWN, not "stale" and not "fresh".
            unknown_seen += 1
        elif (now - last_seen) <= timedelta(minutes=15):
            fresh += 1
        else:
            stale += 1

    # A19.11: installation is per DEVICE, so the report is too. A summary
    # count would tell an operator that a forty-device agent "installed"
    # without saying it reached thirty-one, or which nine it missed and
    # why -- and the reason is the whole point of skipping with one.
    installs = await pre_repo.installs(agent.id, agent.version)
    install_state: dict[str, int] = {}
    by_skill: dict[str, dict] = {}
    for row in installs:
        install_state[row.status] = install_state.get(row.status, 0) + 1
        entry = by_skill.setdefault(
            row.skill_id,
            {"skill_id": row.skill_id, "skill_version": row.skill_version,
             "devices": [], "counts": {}},
        )
        entry["counts"][row.status] = entry["counts"].get(row.status, 0) + 1
        entry["devices"].append({
            "device_agent_id": row.device_agent_id,
            "site_id": row.site_id,
            "status": row.status,
            "detail": row.detail or "",
            "installed_at": (
                row.installed_at.isoformat() if row.installed_at else None
            ),
        })
    for entry in by_skill.values():
        entry["devices"].sort(key=lambda d: d["device_agent_id"])

    preflight = await pre_repo.current(agent.id)
    limit = int(agent.execution_budget or 0)
    return {
        "agent_id": agent.id,
        "actor": actor,
        "activation_state": agent.status,
        "configuration_version": int(agent.version),
        # A19.9, from the ONE provenance rule the detail view also uses.
        **activation_provenance(agent),
        "last_evaluated_at": (
            agent.last_evaluated_at.isoformat() if agent.last_evaluated_at else None
        ),
        "evaluation": (
            "unknown" if agent.last_evaluated_at is None else "observed"
        ),
        "devices": {
            "in_scope": len(devices),
            "seen_recently": fresh,
            "stale": stale,
            # Never counted as healthy OR unhealthy.
            "never_reported": unknown_seen,
        },
        "budget": {
            "period": agent.budget_period,
            "limit": limit,
            "executions_used": executions,
            "remaining": (max(0, limit - executions) if limit else None),
            "exhausted": bool(limit and executions >= limit),
        },
        "proposals_in_window": proposals,
        "skills": install_state,
        "skills_by_id": sorted(by_skill.values(), key=lambda s: s["skill_id"]),
        "paused_reason": agent.paused_reason or None,
        "preflight": {
            "exists": preflight is not None,
            "configuration_version": (
                int(preflight.configuration_version) if preflight else None
            ),
            "overall": preflight.overall if preflight else "unknown",
            "current": bool(
                preflight is not None
                and int(preflight.configuration_version) == int(agent.version)
            ),
        },
    }
