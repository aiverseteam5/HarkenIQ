"""The Operational Agent runtime: evaluate, dispatch, settle.

A1 (2026-08-30). Three passes, run on one cadence:

  evaluate  active agents read the governed contracts over their scope
            and write labelled proposals
  dispatch  proposals that are DECIDED (a human approved one, or the
            tenant's autonomy contract granted the class) are handed to
            the site that owns the device
  settle    outcomes that came back up attribute themselves to the
            proposal that caused them

What this is not
----------------
It is not a second execution path. Dispatch calls one CC->SM verb that
queues on the existing directive transport; the node then runs the same
gate funnel a human-approved action runs, and can refuse.

It is not a second authorization model. Every disposition comes from the
S5 contract via `harkeniq_cc.governance`, the same object `/api/autonomy`
serves. This module never decides that something may run; it reads what
the tenant's governance already decided and carries it.

It is not a privileged caller. An agent reaches exactly the devices its
scope names and proposes exactly the action classes its bundle binds,
and it holds no credential (machine identity is A3).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from harkeniq_cc.db.repos import (
    AgentProposalRepo,
    AuditRepo,
    FleetCacheRepo,
    IncidentRepo,
    OperationalAgentRepo,
    OutcomeHistoryRepo,
    SiteRepo,
)
from harkeniq_cc.governance import (
    load_agent_scope,
    load_attention,
    load_autonomy_contract,
)
from harkeniq_cc.operational_agent import (
    BASIS_AUTONOMOUS,
    EVALUATING_STATUSES,
    PROPOSAL_APPROVED,
    PROPOSAL_AWAITING,
    PROPOSAL_BLOCKED,
    PROPOSAL_DISPATCHED,
    attribution_key,
    evaluate,
)
from harkeniq_cc.sm_client import SMClient

logger = logging.getLogger("harkeniq.cc.agent_runtime")

#: Permissions the evaluator reads the contract with. An agent evaluates
#: at the observation level only: it can see posture and evidence, it
#: cannot approve and it cannot change posture. Those stay human even
#: when the agent's proposal is the thing being approved.
AGENT_PERMISSIONS = ("fleet.view", "incident.view")


async def _incidents_by_device(session, tenant_id: str) -> dict[str, list[dict]]:
    """Open incidents keyed by device, with whether a diagnosis exists."""
    rows = await IncidentRepo(session).list_incidents(
        tenant_id, status="open", limit=1000,
    )
    out: dict[str, list[dict]] = {}
    for row in rows:
        if not row.device_agent_id:
            continue
        out.setdefault(row.device_agent_id, []).append({
            "incident_id": row.incident_id,
            "subsystem": row.subsystem,
            "title": row.title,
            "site_id": row.site_id,
            "diagnosis": bool(row.explanation),
            "confidence": row.confidence,
        })
    return out


async def evaluate_agents(state, tenant_id: str) -> list[Any]:
    """One evaluation pass for every active agent in the tenant.

    Returns the proposals created. Each agent reads the contract under
    its own attribution key, so the audit trail names the exact bundle
    version that reasoned.
    """
    created: list[Any] = []
    async with state.sessionmaker() as session:
        repo = OperationalAgentRepo(session)
        agents = [
            a for a in await repo.list_all(tenant_id)
            if a.status in EVALUATING_STATUSES
        ]
        if not agents:
            return []

        devices = await FleetCacheRepo(session).list_all(tenant_id)
        incidents = await _incidents_by_device(session, tenant_id)
        # The `attention` read every agent is bound to. Ranking the
        # agent's own scope by the SAME answer the operator sees is the
        # point of binding it: the agent works the list a human would
        # work, in the same order, rather than inventing a priority.
        attention = {
            item["agent_id"]: item
            for item in (await load_attention(session, tenant_id=tenant_id))["items"]
        }
        prop_repo = AgentProposalRepo(session)
        audit = AuditRepo(session)

        # A4 (A21.1): the condition -> capability mapping is the tenant's
        # catalogue now, not a module constant. Loaded ONCE per pass and
        # handed to the pure evaluator, so what an agent may propose is
        # something an operator can see and change.
        from harkeniq_cc.capability_catalogue import candidates_for
        from harkeniq_cc.db.repos import CapabilityCatalogueRepo

        catalogue_rows = await CapabilityCatalogueRepo(session).list_for_tenant(
            tenant_id
        )
        catalogue = {
            sub: candidates_for(catalogue_rows, sub)
            for sub in {r.subsystem for r in catalogue_rows}
        }

        midnight = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )

        for agent in agents:
            scopes = await repo.list_scopes(agent.id)
            caps = await repo.list_capabilities(agent.id)
            # E1.2: the agent's reach comes from the SAME resolver a
            # human's does. An org_unit scope has to be expanded through
            # the tree, and doing that here rather than in the composer
            # is what keeps one resolver instead of two.
            agent_scope = await load_agent_scope(
                session, tenant_id=tenant_id, agent_id=agent.id
            )
            contract = await load_autonomy_contract(
                session,
                tenant_id=tenant_id,
                actor_id=attribution_key(agent.id, agent.version),
                actor_species="agent",
                permissions=AGENT_PERMISSIONS,
            )
            seen_keys = await prop_repo.all_dedupe_keys(tenant_id)
            proposals = evaluate(
                catalogue=catalogue,
                agent=agent,
                scopes=scopes,
                resolved_site_ids=agent_scope.site_ids,
                capabilities=caps,
                devices=devices,
                incidents_by_device=incidents,
                autonomy_contract=contract,
                attention_by_device=attention,
                open_dedupe_keys=seen_keys,
                proposals_today=await prop_repo.count_since(
                    tenant_id, agent.id, midnight,
                ),
            )
            for payload in proposals:
                row = await prop_repo.create(**payload)
                created.append(row)
                await audit.append(
                    actor=row.actor,
                    action="agent_proposal.created",
                    subject=row.id,
                    tenant_id=tenant_id,
                    detail={
                        "agent_id": agent.id,
                        "action_type": row.action_type,
                        "device_agent_id": row.device_agent_id,
                        "disposition": row.disposition,
                        "status": row.status,
                        "reason": row.disposition_reason[:200],
                    },
                )
                logger.info(
                    "Proposal %s: %s %s on %s -> %s (%s)",
                    row.id, row.actor, row.action_type,
                    row.device_agent_id, row.status, row.disposition,
                )
            await repo.mark_evaluated(agent)
        await session.commit()
    return created


async def _unattended_allowed(session, tenant_id: str, proposal) -> tuple[bool, str]:
    """May this proposal run WITHOUT a human, right now? (A2 D2 / A19.7.)

    THIS is the production unattended path, so this is where the
    per-agent execution budget has to be asked. It was declared,
    migrated, reported in the preflight and asked nowhere: the dispatch
    loop shipped every approved proposal, `autonomous_grant` included,
    so an exhausted budget stopped nothing.

    Asked through `unattended_permitted`, which is the authoritative
    check -- not a second budget rule written here.
    """
    from harkeniq_cc.agent_activation import unattended_permitted
    from harkeniq_cc.agent_lifecycle import executions_used
    from harkeniq_cc.operational_agent import parse_attribution

    parsed = parse_attribution(getattr(proposal, "actor", "") or "")
    if parsed is None:
        # Not an Operational Agent's proposal. Nothing agent-shaped to ask.
        return True, ""
    agent_id, _version = parsed
    agent = await OperationalAgentRepo(session).get(tenant_id, agent_id)
    if agent is None:
        return False, "the agent that made this proposal no longer exists"
    return unattended_permitted(agent, await executions_used(
        session, tenant_id, agent,
    ))


async def dispatch_decided(state, tenant_id: str) -> list[Any]:
    """Hand every decided proposal to the site that owns its device.

    `approved` means one of two things, and the proposal records which:
    a named human approved it on the approvals queue, or the tenant's
    autonomy contract granted the class and no human was required. The
    basis travels with the dispatch because it changes what the node
    will accept.

    A2/D2: the basis also decides whether the per-agent execution budget
    gets a say. An `autonomous_grant` spends the agent's unattended
    allowance and is withheld once that is gone; a human's decision does
    not, because the budget caps DELEGATED work, never what a person
    chose to do.
    """
    dispatched: list[Any] = []
    async with state.sessionmaker() as session:
        prop_repo = AgentProposalRepo(session)
        pending = await prop_repo.list_by_status(tenant_id, [PROPOSAL_APPROVED])
        if not pending:
            return []
        site_repo = SiteRepo(session)
        audit = AuditRepo(session)
        client = SMClient(state.config.sm_tls_ca)
        for proposal in pending:
            if proposal.authorization_basis == BASIS_AUTONOMOUS:
                allowed, why = await _unattended_allowed(
                    session, tenant_id, proposal,
                )
                if not allowed:
                    # Withheld, not failed: the work is still valid and a
                    # human may still approve it. Exhaustion withdraws the
                    # unattended grant; it does not disable the agent.
                    await prop_repo.withhold_unattended(proposal, why)
                    await audit.append(
                        actor=proposal.actor,
                        action="agent_proposal.unattended_withheld",
                        subject=proposal.id,
                        tenant_id=tenant_id,
                        detail={
                            "reason": why,
                            "action_type": proposal.action_type,
                            "device_agent_id": proposal.device_agent_id,
                            "now_requires": "human_approval",
                        },
                    )
                    logger.info(
                        "Withheld unattended proposal %s: %s", proposal.id, why,
                    )
                    continue
            site = await site_repo.get_by_id(proposal.site_id)
            if site is None or site.tenant_id != tenant_id:
                await prop_repo.mark_failed(
                    proposal, "site is no longer registered to this tenant",
                )
                continue
            try:
                result = await client.dispatch_action(
                    site.sm_endpoint,
                    site.sm_token or "",
                    tenant_id=tenant_id,
                    site_id=proposal.site_id,
                    device_agent_id=proposal.device_agent_id,
                    action_type=proposal.action_type,
                    params_json=json.dumps(proposal.params or {}),
                    actor=proposal.actor,
                    authorization=proposal.authorization_basis,
                    decided_by=proposal.decided_by,
                    proposal_id=proposal.id,
                )
            except Exception as exc:  # noqa: BLE001 — a site being down
                # is not a decision; leave the proposal approved so the
                # next pass retries rather than losing a human's decision.
                logger.warning(
                    "Dispatch failed for proposal %s: %s", proposal.id, exc,
                )
                continue
            if not result.get("accepted"):
                reason = result.get("reason", "refused by the site manager")
                await prop_repo.mark_failed(proposal, reason)
                await audit.append(
                    actor=proposal.actor,
                    action="agent_proposal.refused",
                    subject=proposal.id,
                    tenant_id=tenant_id,
                    detail={"reason": reason, "site_id": proposal.site_id},
                )
                logger.warning(
                    "Site refused proposal %s: %s", proposal.id, reason,
                )
                continue
            await prop_repo.mark_dispatched(
                proposal, result.get("directive_id", ""),
            )
            dispatched.append(proposal)
            await audit.append(
                actor=proposal.actor,
                action="agent_proposal.dispatched",
                subject=proposal.id,
                tenant_id=tenant_id,
                detail={
                    "directive_id": result.get("directive_id", ""),
                    "authorization": proposal.authorization_basis,
                    "decided_by": proposal.decided_by,
                    "action_type": proposal.action_type,
                    "device_agent_id": proposal.device_agent_id,
                },
            )
            logger.info(
                "Dispatched proposal %s (%s) as directive %s",
                proposal.id, proposal.authorization_basis,
                result.get("directive_id", ""),
            )
        await session.commit()
    return dispatched


async def settle_outcomes(state, tenant_id: str) -> int:
    """Attribute returned outcomes back to the proposals that caused them.

    Outcomes arrive from the Site Manager keyed by device and action
    type, so the join is (device, action_type, oldest dispatched). An
    outcome that matches nothing is left alone: it is a human's action or
    a campaign's, and inventing an agent for it would corrupt the very
    attribution this slice exists to establish.
    """
    settled = 0
    async with state.sessionmaker() as session:
        prop_repo = AgentProposalRepo(session)
        open_rows = await prop_repo.list_by_status(tenant_id, [PROPOSAL_DISPATCHED])
        if not open_rows:
            return 0
        outcomes = await OutcomeHistoryRepo(session).list_outcome_dicts(tenant_id)
        audit = AuditRepo(session)
        # Newest first: an outcome that arrived after the dispatch is the
        # one that settles it.
        by_key: dict[tuple[str, str], list[dict]] = {}
        for oc in outcomes:
            key = (oc.get("device_agent_id", ""), oc.get("action_type", ""))
            by_key.setdefault(key, []).append(oc)

        for proposal in open_rows:
            candidates = by_key.get(
                (proposal.device_agent_id, proposal.action_type), []
            )
            match = None
            for oc in candidates:
                # Attribution wins when it is present; otherwise fall back
                # to the time window, since outcomes reported before this
                # slice carried no actor at all.
                if oc.get("actor") and oc["actor"] != proposal.actor:
                    continue
                ingested = oc.get("ingested_at")
                if (
                    proposal.dispatched_at
                    and ingested
                    and ingested < proposal.dispatched_at
                ):
                    continue
                match = oc
                break
            if match is None:
                continue
            await prop_repo.settle(proposal, match.get("outcome", "UNKNOWN"))
            settled += 1
            await audit.append(
                actor=proposal.actor,
                action="agent_proposal.settled",
                subject=proposal.id,
                tenant_id=tenant_id,
                detail={
                    "outcome": proposal.outcome,
                    "action_type": proposal.action_type,
                    "device_agent_id": proposal.device_agent_id,
                },
            )
        await session.commit()
    return settled


async def run_once(state, tenant_id: str) -> dict[str, int]:
    """One full pass. Separated from the loop so tests can drive it."""
    created = await evaluate_agents(state, tenant_id)
    dispatched = await dispatch_decided(state, tenant_id)
    settled = await settle_outcomes(state, tenant_id)
    return {
        "proposed": len(created),
        "dispatched": len(dispatched),
        "settled": settled,
        "awaiting_approval": sum(
            1 for p in created if p.status == PROPOSAL_AWAITING
        ),
        "blocked": sum(1 for p in created if p.status == PROPOSAL_BLOCKED),
        "autonomous": sum(
            1 for p in created if p.authorization_basis == BASIS_AUTONOMOUS
        ),
    }


async def operational_agent_loop(state) -> None:
    """Background task: evaluate, dispatch, settle, forever."""
    interval = float(getattr(state.config, "agent_evaluate_interval_s", 120.0))
    tenant_id = state.config.tenant_id
    while True:
        try:
            if tenant_id:
                stats = await run_once(state, tenant_id)
                if any(stats.values()):
                    logger.info("Operational agents: %s", stats)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — one bad pass must not kill the loop
            logger.exception("Operational agent pass failed")
        await asyncio.sleep(interval)
