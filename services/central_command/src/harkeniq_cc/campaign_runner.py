"""S6 campaign orchestration: preflight, acknowledgement, advance.

The stateful half of S6. Every judgement it makes is imported from
`harkeniq_cc.campaigns` (pure) or from a governance composer that
already exists; what lives here is the sequencing, the persistence and
the dispatch.

It consumes, and never reimplements:

    E1.2 scope resolver      which devices a campaign may reach
    Capability Registry      whether an executor can perform the class
    autonomy contract        whether a human is required
    E0.1 approval ledger     the decision itself
    DispatchAction           delivery onto the existing directive path
    audit chain              the record
    outcome writer           evidence, error budgets, learning

The two behaviours worth reading closely are the acknowledgement gate
and dispatch-time revalidation. Both exist because capability truth
decays between preflight and execution, and because a decision taken on
stale truth is indistinguishable from a decision taken carelessly.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from harkeniq.capabilities import action_facts

from harkeniq_cc.campaigns import (
    WAVE_AUTONOMOUS,
    WAVE_PENDING_APPROVAL,
    WAVE_RUNNABLE,
    WAVE_VOIDED,
    APPLICABILITY_ELIGIBLE,
    APPLICABILITY_EXCLUDED,
    APPLICABILITY_EXCLUDED_BY_OPERATOR,
    APPLICABILITY_UNKNOWN,
    APPLICABILITY_WARN,
    DISPATCHABLE,
    NEEDS_ACKNOWLEDGEMENT,
    REVAL_OK,
    STATUS_ACKNOWLEDGED,
    STATUS_AWAITING_APPROVAL,
    STATUS_COMPLETED,
    STATUS_HALTED,
    STATUS_PREFLIGHTED,
    STATUS_RUNNING,
    acknowledgement_valid,
    campaign_terminal_state,
    narrow_only,
    plan_site_order,
    revalidate_target,
    target_applicability,
    plan_is_current,
    wave_subject_ref,
    waves_to_void,
)
from harkeniq_cc.db.repos import (
    AuditRepo,
    CampaignRepo,
    FleetCacheRepo,
    SiteRepo,
)
from harkeniq_cc.operational_agent import resolve_scope
from harkeniq_cc.sm_client import SMClient

logger = logging.getLogger("harkeniq.cc.campaigns")

ATTRIBUTION_PREFIX = "campaign:"


def campaign_actor(campaign_id: str, version: int) -> str:
    """The actor a campaign's work carries everywhere.

    Versioned for the same reason an Operational Agent's is: an outcome
    must name the exact configuration that produced it, so editing a
    campaign can never rewrite what an earlier wave ran under.
    """
    return f"{ATTRIBUTION_PREFIX}{campaign_id}@v{int(version)}"


def parse_campaign_actor(actor: str) -> Optional[tuple[str, int]]:
    if not actor or not actor.startswith(ATTRIBUTION_PREFIX):
        return None
    body = actor[len(ATTRIBUTION_PREFIX):]
    if "@v" not in body:
        return None
    campaign_id, _, version = body.rpartition("@v")
    try:
        return campaign_id, int(version)
    except ValueError:
        return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def preflight(
    session,
    state,
    *,
    tenant_id: str,
    campaign,
    scope_rules,
    resolved_site_ids,
    actor: str,
) -> dict:
    """Resolve targets, ask the Registry about every one, and store it.

    This is the step that makes the product requirement true: a campaign
    must never discover that the executor cannot perform the capability
    only after dispatch. Every device the scope reaches gets a verdict
    here, and the excluded ones are KEPT with their reason -- an absent
    row would leave an operator unable to tell "not selected" from
    "cannot do it".
    """
    facts = action_facts()
    fact = facts.get(campaign.action_type)
    if fact is None:
        raise ValueError(f"{campaign.action_type} is not a governed action class")

    devices = await FleetCacheRepo(session).list_all(tenant_id)
    in_scope = resolve_scope(scope_rules, devices, resolved_site_ids)
    sites = {s.id: s.site_name for s in await SiteRepo(session).list_all(tenant_id)}

    rows: list[dict] = []
    for device in in_scope:
        verdict, reason = target_applicability(
            device, campaign.action_type, fact["implemented"],
        )
        rows.append({
            "site_id": device.site_id,
            "device_agent_id": device.agent_id,
            "device_name": device.agent_name or device.agent_id,
            "device_class": device.device_class or "server",
            "applicability": verdict,
            "reason": reason,
        })

    repo = CampaignRepo(session)
    await repo.replace_targets(campaign.id, rows)

    # Only sites with at least one DISPATCHABLE target get a branch. A
    # site whose every device was excluded is not a site this campaign
    # visits, and creating an empty branch would make it look pending
    # forever.
    live_sites = {
        r["site_id"] for r in rows if r["applicability"] in DISPATCHABLE
    }
    await repo.replace_sites(campaign.id, [
        {"site_id": sid, "site_name": name, "order_index": idx, "status": "pending"}
        for idx, sid, name in plan_site_order(live_sites, sites)
    ])

    campaign.status = STATUS_PREFLIGHTED
    campaign.preflight_at = _utcnow()
    campaign.updated_at = _utcnow()
    # A fresh preflight supersedes any previous acknowledgement: the set
    # of warned targets may have changed under it.
    campaign.acknowledged_by = ""
    campaign.acknowledged_at = None
    campaign.acknowledged_version = 0
    await session.flush()

    # The ratified flow: capability preflight, THEN ask each site how its
    # devices must be batched. Planning is the site's answer, not ours.
    plan_summary = await plan_sites(
        session, state, tenant_id=tenant_id, campaign=campaign, actor=actor,
    )

    summary = _summarise(rows)
    summary["plans"] = plan_summary
    await AuditRepo(session).append(
        actor=actor,
        action="campaign.preflighted",
        subject=campaign.id,
        tenant_id=tenant_id,
        detail={
            "action_type": campaign.action_type,
            "version": campaign.version,
            **summary,
        },
    )
    return summary


def _summarise(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["applicability"]] = counts.get(r["applicability"], 0) + 1
    return {
        "considered": len(rows),
        "eligible": counts.get(APPLICABILITY_ELIGIBLE, 0),
        "warn_not_permitted": counts.get(APPLICABILITY_WARN, 0),
        "unknown": counts.get(APPLICABILITY_UNKNOWN, 0),
        "excluded_unimplemented": counts.get(APPLICABILITY_EXCLUDED, 0),
        "excluded_by_operator": counts.get(APPLICABILITY_EXCLUDED_BY_OPERATOR, 0),
        "sites": len({r["site_id"] for r in rows if r["applicability"] in DISPATCHABLE}),
    }


async def acknowledge(
    session,
    *,
    tenant_id: str,
    campaign,
    exclude_device_ids: list[str],
    actor: str,
) -> dict:
    """A named human settles every warned or unknown target (D2).

    Excluding is a real decision and is recorded as one: the target keeps
    its row and gains `excluded_by_operator`, so the campaign's history
    still shows the device was considered and who removed it.

    The acknowledgement is bound to the campaign VERSION it was given
    for. Editing the campaign bumps that version and invalidates this,
    because otherwise a person acknowledges v1 and the estate runs v2.
    """
    repo = CampaignRepo(session)
    targets = list(await repo.targets(campaign.id))
    excluded = set(exclude_device_ids or [])

    for t in targets:
        if t.device_agent_id in excluded and t.applicability in DISPATCHABLE:
            t.applicability = APPLICABILITY_EXCLUDED_BY_OPERATOR
            t.reason = f"excluded by {actor} before approval"
            t.updated_at = _utcnow()

    remaining = [t for t in targets if t.applicability in DISPATCHABLE]
    accepted = [t for t in remaining if t.applicability in NEEDS_ACKNOWLEDGEMENT]

    campaign.acknowledged_by = actor
    campaign.acknowledged_at = _utcnow()
    campaign.acknowledged_version = int(campaign.version)
    campaign.status = STATUS_ACKNOWLEDGED
    campaign.updated_at = _utcnow()

    # Sites whose every target was just excluded stop being visited.
    live_sites = {t.site_id for t in remaining}
    site_rows = await repo.sites(campaign.id)
    for row in site_rows:
        if row.site_id not in live_sites and row.status == "pending":
            row.status = "skipped"
            row.halt_reason = "every target at this site was excluded"
    await session.flush()

    await AuditRepo(session).append(
        actor=actor,
        action="campaign.acknowledged",
        subject=campaign.id,
        tenant_id=tenant_id,
        detail={
            "version": campaign.version,
            "excluded": sorted(excluded),
            "accepted_warned": [t.device_agent_id for t in accepted],
            "accepted_count": len(accepted),
        },
    )
    return {
        "acknowledged_by": actor,
        "version": campaign.version,
        "excluded": sorted(excluded),
        "accepted_warned": len(accepted),
        "remaining": len(remaining),
    }


async def revalidate_wave(
    session,
    *,
    tenant_id: str,
    campaign,
    site_id: str,
    approved_device_ids: list[str],
) -> dict:
    """Re-read capability and policy immediately before dispatch (D2).

    Preflight can be days old. An allow list changes, an agent is rolled
    back, a device is re-protocolled -- and a decision taken on stale
    truth is indistinguishable from a careless one. So the Registry is
    consulted again, here, at the wave boundary.

    The result may only ever NARROW the approved set. A device that has
    become capable since approval is not swept in: the approved target
    set is the blast radius a person signed off, and widening it here
    would turn revalidation into a path to executing on devices nobody
    approved. That is enforced by intersection, not by trusting this
    function to only remove.
    """
    facts = action_facts()
    fact = facts.get(campaign.action_type) or {"implemented": False}
    acknowledged = acknowledgement_valid(campaign)

    devices = {
        d.agent_id: d
        for d in await FleetCacheRepo(session).list_all(tenant_id)
        if d.site_id == site_id
    }
    repo = CampaignRepo(session)

    proceed: list[str] = []
    skipped: list[dict] = []
    for agent_id in approved_device_ids:
        verdict, reason = revalidate_target(
            devices.get(agent_id),
            campaign.action_type,
            fact["implemented"],
            acknowledged,
        )
        target = await repo.get_target(campaign.id, agent_id)
        if target is not None:
            target.revalidation = verdict
            target.revalidation_reason = reason
            target.updated_at = _utcnow()
        if verdict == REVAL_OK:
            proceed.append(agent_id)
        else:
            if target is not None:
                target.status = "skipped"
            skipped.append({"device_agent_id": agent_id, "verdict": verdict,
                            "reason": reason})

    # Narrow-only, enforced rather than assumed.
    final = sorted(narrow_only(approved_device_ids, proceed))
    await session.flush()

    if skipped:
        await AuditRepo(session).append(
            actor=campaign_actor(campaign.id, campaign.version),
            action="campaign.targets_skipped",
            subject=campaign.id,
            tenant_id=tenant_id,
            detail={"site_id": site_id, "skipped": skipped},
        )
    return {"dispatch": final, "skipped": skipped}


async def dispatch_wave(
    session,
    state,
    *,
    tenant_id: str,
    campaign,
    site,
    wave_index: int,
    device_ids: list[str],
    plan_hash: str,
    authorization: str,
    decided_by: str,
) -> dict:
    """Hand one site-wave to the Site Manager that owns those devices.

    Delivery only. Central Command already governed this decision; the
    Site Manager queues it on the existing directive transport and the
    node runs its unchanged funnel, which can still refuse -- and that
    refusal becomes attributed evidence, which is exactly what an
    acknowledged policy denial is for.

    Every dispatch is written to the ledger FIRST. The composite key is
    the idempotency guarantee: a replay cannot execute a device twice in
    a wave because the second row cannot exist.
    """
    repo = CampaignRepo(session)
    site_row = await SiteRepo(session).get_by_id(site.site_id)
    actor = campaign_actor(campaign.id, campaign.version)
    dispatched, skipped = [], []

    if site_row is None or site_row.tenant_id != tenant_id:
        return {"dispatched": [], "skipped": [
            {"reason": "site is no longer registered to this tenant"}
        ]}

    client = SMClient(state.config.sm_tls_ca)
    for agent_id in device_ids:
        if await repo.already_dispatched(
            campaign.id, campaign.version, site.site_id, agent_id,
            wave_index, plan_hash,
        ):
            skipped.append({"device_agent_id": agent_id, "reason": "already dispatched"})
            continue
        try:
            result = await client.dispatch_action(
                site_row.sm_endpoint,
                site_row.sm_token or "",
                tenant_id=tenant_id,
                site_id=site.site_id,
                device_agent_id=agent_id,
                action_type=campaign.action_type,
                params_json=json.dumps(campaign.params or {}),
                actor=actor,
                authorization=authorization,
                decided_by=decided_by,
            )
        except Exception as exc:  # transport failure is a site problem
            logger.warning(
                "campaign %s dispatch to %s failed: %s", campaign.id, agent_id, exc
            )
            result = {"accepted": False, "detail": str(exc)}

        await repo.record_dispatch(
            campaign_id=campaign.id,
            campaign_version=campaign.version,
            site_id=site.site_id,
            device_agent_id=agent_id,
            wave_index=wave_index,
            plan_hash=plan_hash,
            directive_id=result.get("directive_id", ""),
            actor=actor,
            authorization=authorization,
            decided_by=decided_by,
            accepted=bool(result.get("accepted")),
            detail=str(result.get("detail", ""))[:512],
        )
        target = await repo.get_target(campaign.id, agent_id)
        if target is not None:
            target.status = "dispatched" if result.get("accepted") else "failed"
            if not result.get("accepted"):
                target.error = str(result.get("detail", ""))[:512]
            target.updated_at = _utcnow()
        (dispatched if result.get("accepted") else skipped).append({
            "device_agent_id": agent_id,
            "directive_id": result.get("directive_id", ""),
            "reason": "" if result.get("accepted") else str(result.get("detail", "")),
        })

    await session.flush()
    await AuditRepo(session).append(
        actor=actor,
        action="campaign.wave_dispatched",
        subject=campaign.id,
        tenant_id=tenant_id,
        detail={
            "site_id": site.site_id,
            "wave": wave_index,
            "dispatched": [d["device_agent_id"] for d in dispatched],
            "skipped": skipped,
            "authorization": authorization,
        },
    )
    return {"dispatched": dispatched, "skipped": skipped}


async def settle_campaign(session, *, tenant_id: str, campaign) -> Optional[str]:
    """Close the campaign when every site branch has finished.

    Partial success is first-class: a campaign where one site halted and
    seven completed settles as `halted` WITH per-site detail, never
    rounded up to completed or down to failed.
    """
    repo = CampaignRepo(session)
    rows = await repo.sites(campaign.id)
    live = [r for r in rows if r.status != "skipped"]
    terminal = campaign_terminal_state(live)
    if terminal is None:
        return None
    campaign.status = terminal
    campaign.completed_at = _utcnow()
    campaign.updated_at = _utcnow()
    if terminal == STATUS_HALTED:
        halted = [r.site_id for r in live if r.status == "halted"]
        campaign.halt_reason = (
            f"{len(halted)} of {len(live)} site(s) halted: {', '.join(halted)}"
        )
    await session.flush()
    await AuditRepo(session).append(
        actor=campaign_actor(campaign.id, campaign.version),
        action=f"campaign.{terminal}",
        subject=campaign.id,
        tenant_id=tenant_id,
        detail={
            "sites_completed": sum(1 for r in live if r.status == "completed"),
            "sites_halted": sum(1 for r in live if r.status == "halted"),
            "halt_reason": campaign.halt_reason,
        },
    )
    return terminal


async def plan_sites(session, state, *, tenant_id: str, campaign, actor: str) -> dict:
    """Ask every in-scope site for its wave plan and store it immutably.

    Central Command never plans a wave. It names the eligible devices and
    the site answers with exact membership, computed from fault domains
    only that tier owns. What comes back is stored verbatim, hashed by
    the site, and becomes the thing an approver approves.

    `planned=False` is a real answer and is NOT "this site has no
    devices": the site could not be resolved, so it is skipped with the
    reason recorded rather than quietly treated as empty (A16.3).
    """
    repo = CampaignRepo(session)
    client = SMClient(state.config.sm_tls_ca)
    out: dict[str, Any] = {"planned": 0, "unplanned": 0, "waves": 0, "sites": []}

    for site_row in await repo.sites(campaign.id):
        if site_row.status == "skipped":
            continue
        eligible = sorted(
            t.device_agent_id
            for t in await repo.targets(campaign.id, site_id=site_row.site_id)
            if t.applicability in DISPATCHABLE
        )
        site = await SiteRepo(session).get_by_id(site_row.site_id)
        if site is None or site.tenant_id != tenant_id or not eligible:
            site_row.status = "skipped"
            site_row.halt_reason = (
                "site is not registered to this tenant" if site is None
                else "no eligible target at this site"
            )
            out["unplanned"] += 1
            continue
        try:
            plan = await client.plan_campaign_waves(
                site.sm_endpoint,
                site.sm_token or "",
                tenant_id=tenant_id,
                site_id=site_row.site_id,
                campaign_id=campaign.id,
                campaign_version=campaign.version,
                action_type=campaign.action_type,
                device_agent_ids=eligible,
                max_wave_size=campaign.max_wave_size,
            )
        except Exception as exc:
            logger.warning(
                "campaign %s could not plan site %s: %s",
                campaign.id, site_row.site_id, exc,
            )
            site_row.status = "pending"
            site_row.halt_reason = f"planning failed: {exc}"[:1024]
            out["unplanned"] += 1
            continue

        if not plan.get("planned"):
            site_row.status = "skipped"
            site_row.halt_reason = plan.get("reason", "site did not plan")[:1024]
            out["unplanned"] += 1
            continue

        # Defensive, and load-bearing: the site may only ever answer with
        # devices we asked about. A plan containing anything else would be
        # the Site Manager widening the target set, which must be
        # impossible -- so the whole plan is rejected, fail closed.
        requested = set(eligible)
        returned = {
            d for w in plan["waves"] for d in w["device_agent_ids"]
        }
        if not returned <= requested:
            extra = sorted(returned - requested)
            site_row.status = "skipped"
            site_row.halt_reason = (
                f"plan rejected: site returned device(s) that were not "
                f"requested ({', '.join(extra[:5])})"
            )[:1024]
            logger.error(
                "campaign %s: site %s returned unrequested devices %s",
                campaign.id, site_row.site_id, extra,
            )
            out["unplanned"] += 1
            continue

        stored = await repo.store_plan(
            campaign_id=campaign.id,
            campaign_version=campaign.version,
            site_id=site_row.site_id,
            plan_hash=plan["plan_hash"],
            waves=plan["waves"],
            unplannable=plan["unplannable_device_ids"],
            separation_rule=plan.get("separation_rule", ""),
            generated_at=datetime.fromtimestamp(
                plan.get("generated_at_unix") or 0, tz=timezone.utc
            ),
        )
        await repo.supersede_plans(campaign.id, site_row.site_id, stored.plan_hash)

        # A device the site could not resolve is surfaced on its target
        # row, never silently dropped from the run.
        for agent_id in plan["unplannable_device_ids"]:
            target = await repo.get_target(campaign.id, agent_id)
            if target is not None:
                target.status = "skipped"
                target.reason = "the site could not resolve this device"
                target.updated_at = _utcnow()

        site_row.plan_hash = stored.plan_hash
        site_row.wave_count = len(plan["waves"])
        site_row.current_wave = 0
        site_row.status = "pending"
        site_row.halt_reason = ""
        out["planned"] += 1
        out["waves"] += len(plan["waves"])
        out["sites"].append({
            "site_id": site_row.site_id,
            "site_name": site_row.site_name,
            "plan_hash": stored.plan_hash,
            "waves": [
                {"wave_index": w["wave_index"],
                 "devices": w["device_agent_ids"],
                 "domain_span": w["domain_span"]}
                for w in plan["waves"]
            ],
        })
        await AuditRepo(session).append(
            actor=actor,
            action="campaign.plan_received",
            subject=campaign.id,
            tenant_id=tenant_id,
            detail={
                "site_id": site_row.site_id,
                "plan_hash": stored.plan_hash,
                "waves": len(plan["waves"]),
                "devices": sum(len(w["device_agent_ids"]) for w in plan["waves"]),
                "unplannable": plan["unplannable_device_ids"],
            },
        )

    # A re-plan invalidates any wave built on the previous one.
    await repo.clear_waves(campaign.id)
    await session.flush()
    return out


async def build_waves(session, *, tenant_id: str, campaign, autonomous: bool) -> dict:
    """Materialise one approval subject per site-wave (Q1: all at submit).

    Every site-wave of the whole campaign version becomes a row before
    execution begins, so the set of decisions a campaign needs is known
    and fixed up front rather than appearing one at a time. That is what
    makes the governance deterministic, and it is what lets a Console
    offer batch review without the underlying records ever merging.

    An autonomous class raises NO approval subject at all -- there is no
    human decision to record, and manufacturing one would imply a human
    reviewed something nobody was asked to review.
    """
    repo = CampaignRepo(session)
    await repo.clear_waves(campaign.id)
    created, pending = 0, 0

    for site_row in await repo.sites(campaign.id):
        if site_row.status == "skipped":
            continue
        plan = await repo.current_plan(campaign.id, site_row.site_id)
        if plan is None:
            continue
        for wave in plan.waves or []:
            devices = sorted(wave.get("device_agent_ids") or [])
            if not devices:
                continue
            status = WAVE_AUTONOMOUS if autonomous else WAVE_PENDING_APPROVAL
            subject = "" if autonomous else wave_subject_ref(
                campaign.id, campaign.version, site_row.site_id,
                wave["wave_index"], devices, plan.plan_hash,
            )
            await repo.add_wave(
                campaign_id=campaign.id,
                campaign_version=campaign.version,
                site_id=site_row.site_id,
                wave_index=int(wave["wave_index"]),
                plan_hash=plan.plan_hash,
                device_agent_ids=devices,
                domain_span=int(wave.get("domain_span") or 0),
                subject_ref=subject,
                status=status,
            )
            created += 1
            if not autonomous:
                pending += 1
    await session.flush()
    return {"waves": created, "awaiting_approval": pending,
            "autonomous": created - pending}


async def void_site_waves(
    session, *, tenant_id: str, campaign, site_id: str, reason: str
) -> int:
    """A halted site loses the authorization for its later waves (Q3).

    Those waves were approved as part of a sequence whose predecessor has
    now failed, so the assumption behind the approval is gone. Leaving
    them approved is stale authorization, and stale authorization is how
    a halted site quietly resumes on a decision nobody would make again.

    Explicit and audited. Only this site: another site's approvals stand,
    because a halted site is not a halted campaign.
    """
    repo = CampaignRepo(session)
    rows = waves_to_void(await repo.waves(campaign.id), site_id)
    for row in rows:
        row.status = WAVE_VOIDED
        row.void_reason = reason[:512]
        row.settled_at = _utcnow()
    if rows:
        await session.flush()
        await AuditRepo(session).append(
            actor=campaign_actor(campaign.id, campaign.version),
            action="campaign.waves_voided",
            subject=campaign.id,
            tenant_id=tenant_id,
            detail={
                "site_id": site_id,
                "voided": [
                    {"wave_index": r.wave_index, "subject_ref": r.subject_ref}
                    for r in rows
                ],
                "reason": reason,
            },
        )
    return len(rows)


async def advance_campaign(session, state, *, tenant_id: str, campaign) -> dict:
    """Move every eligible site forward by at most one wave.

    One wave per site per call, so progress is observable and
    interruptible — the same discipline the firmware orchestrator uses,
    and the reason a runaway campaign cannot exist.

    The order of gates matters and is not arbitrary:

      1. stop switch      absolute, and cheapest to check
      2. wave authorized  APPROVED (or autonomous). Not yet EXECUTABLE.
      3. plan still current   re-ask the site; a changed plan REFUSES the
                              wave rather than narrowing it, because a
                              subset of an authorization nobody gave is
                              not a smaller version of the same decision
      4. capability/policy    narrows, never widens
      5. dispatch             onto the existing funnel, which may refuse

    Sites advance independently. One halting does not stop the others.
    """
    from harkeniq_cc.db.repos import StopSwitchRepo

    repo = CampaignRepo(session)
    result: dict[str, Any] = {"advanced": [], "blocked": [], "halted": []}

    stop = await StopSwitchRepo(session).get(tenant_id)
    if stop is not None and getattr(stop, "active", False):
        result["blocked"].append({
            "reason": "the tenant stop switch is active; no wave may advance",
        })
        return result

    # Close out anything already dispatched before looking for new work:
    # a wave that finished this pass makes the next one eligible now,
    # and a wave that failed must halt its site before we advance it.
    await settle_dispatched_waves(session, tenant_id=tenant_id, campaign=campaign)

    site_rows = [s for s in await repo.sites(campaign.id) if s.status in
                 ("pending", "running")]
    concurrency = max(1, int(campaign.site_concurrency or 1))
    running = [s for s in site_rows if s.status == "running"]
    startable = [s for s in site_rows if s.status == "pending"]
    active = running + startable[: max(0, concurrency - len(running))]

    for site_row in active:
        outcome = await _advance_site(
            session, state, tenant_id=tenant_id, campaign=campaign,
            site_row=site_row,
        )
        result[outcome["bucket"]].append(outcome["detail"])

    await settle_campaign(session, tenant_id=tenant_id, campaign=campaign)
    await session.flush()
    return result


async def _advance_site(session, state, *, tenant_id: str, campaign, site_row) -> dict:
    repo = CampaignRepo(session)
    waves = [
        w for w in await repo.waves(campaign.id, site_id=site_row.site_id)
        if w.status not in ("completed", "failed", "denied", "voided")
    ]
    if not waves:
        site_row.status = "completed"
        site_row.completed_at = _utcnow()
        return {"bucket": "advanced",
                "detail": {"site_id": site_row.site_id, "state": "completed"}}

    wave = sorted(waves, key=lambda w: w.wave_index)[0]

    if wave.status == "dispatched":
        return {"bucket": "blocked", "detail": {
            "site_id": site_row.site_id, "wave": wave.wave_index,
            "reason": "the current wave is still settling",
        }}
    if wave.status not in WAVE_RUNNABLE:
        return {"bucket": "blocked", "detail": {
            "site_id": site_row.site_id, "wave": wave.wave_index,
            "reason": f"wave is {wave.status}, not authorized to run",
        }}

    # -- 3. is the plan the approver approved still the plan? --------------
    site = await SiteRepo(session).get_by_id(site_row.site_id)
    if site is None or site.tenant_id != tenant_id:
        return await _halt_site(
            session, tenant_id=tenant_id, campaign=campaign, site_row=site_row,
            reason="site is no longer registered to this tenant",
        )
    eligible = sorted(
        t.device_agent_id
        for t in await repo.targets(campaign.id, site_id=site_row.site_id)
        if t.applicability in DISPATCHABLE
    )
    try:
        replan = await SMClient(state.config.sm_tls_ca).plan_campaign_waves(
            site.sm_endpoint, site.sm_token or "",
            tenant_id=tenant_id, site_id=site_row.site_id,
            campaign_id=campaign.id, campaign_version=campaign.version,
            action_type=campaign.action_type,
            device_agent_ids=eligible,
            max_wave_size=campaign.max_wave_size,
        )
    except Exception as exc:
        return await _halt_site(
            session, tenant_id=tenant_id, campaign=campaign, site_row=site_row,
            reason=f"could not re-plan before dispatch: {exc}",
        )

    if not replan.get("planned") or not plan_is_current(
        wave.plan_hash, replan.get("plan_hash", "")
    ):
        # The estate changed materially under an approved plan. Refuse the
        # wave, supersede, and require a fresh decision -- a changed fault
        # domain must never silently widen an approved blast radius.
        await repo.supersede_plans(
            campaign.id, site_row.site_id, replan.get("plan_hash", "")
        )
        voided = await void_site_waves(
            session, tenant_id=tenant_id, campaign=campaign,
            site_id=site_row.site_id,
            reason=(
                "the site's wave plan changed after approval; the approved "
                "plan no longer describes this estate"
            ),
        )
        site_row.status = "pending"
        site_row.plan_hash = ""
        site_row.halt_reason = "plan changed; re-plan and re-approve"
        await AuditRepo(session).append(
            actor=campaign_actor(campaign.id, campaign.version),
            action="campaign.wave_refused_plan_changed",
            subject=campaign.id,
            tenant_id=tenant_id,
            detail={
                "site_id": site_row.site_id,
                "wave": wave.wave_index,
                "approved_plan_hash": wave.plan_hash,
                "current_plan_hash": replan.get("plan_hash", ""),
                "waves_voided": voided,
            },
        )
        return {"bucket": "blocked", "detail": {
            "site_id": site_row.site_id, "wave": wave.wave_index,
            "reason": "plan changed after approval; new approval required",
        }}

    # -- 4. capability and policy, which may only narrow -------------------
    reval = await revalidate_wave(
        session, tenant_id=tenant_id, campaign=campaign,
        site_id=site_row.site_id,
        approved_device_ids=list(wave.device_agent_ids or []),
    )
    if not reval["dispatch"]:
        wave.status = "completed"
        wave.settled_at = _utcnow()
        site_row.current_wave = wave.wave_index + 1
        return {"bucket": "advanced", "detail": {
            "site_id": site_row.site_id, "wave": wave.wave_index,
            "dispatched": 0,
            "reason": "every device in this wave was skipped at revalidation",
        }}

    # -- 5. dispatch onto the existing funnel ------------------------------
    site_row.status = "running"
    site_row.started_at = site_row.started_at or _utcnow()
    dispatch = await dispatch_wave(
        session, state, tenant_id=tenant_id, campaign=campaign, site=site_row,
        wave_index=wave.wave_index, device_ids=reval["dispatch"],
        plan_hash=wave.plan_hash,
        authorization=(
            "autonomous_grant" if wave.status == WAVE_AUTONOMOUS
            else "human_approval"
        ),
        decided_by=wave.decided_by,
    )
    wave.status = "dispatched"
    wave.dispatched_at = _utcnow()
    site_row.current_wave = wave.wave_index
    return {"bucket": "advanced", "detail": {
        "site_id": site_row.site_id, "wave": wave.wave_index,
        "dispatched": len(dispatch["dispatched"]),
        "skipped": len(dispatch["skipped"]),
    }}


async def _halt_site(session, *, tenant_id: str, campaign, site_row, reason: str) -> dict:
    """Halt one site and void the authorization its later waves carried."""
    site_row.status = "halted"
    site_row.halt_reason = reason[:1024]
    site_row.completed_at = _utcnow()
    voided = await void_site_waves(
        session, tenant_id=tenant_id, campaign=campaign,
        site_id=site_row.site_id, reason=f"site halted: {reason}",
    )
    await AuditRepo(session).append(
        actor=campaign_actor(campaign.id, campaign.version),
        action="campaign.site_halted",
        subject=campaign.id,
        tenant_id=tenant_id,
        detail={"site_id": site_row.site_id, "reason": reason,
                "waves_voided": voided},
    )
    return {"bucket": "halted", "detail": {
        "site_id": site_row.site_id, "reason": reason, "waves_voided": voided,
    }}


async def run_campaigns_once(state, tenant_id: str) -> dict:
    """One reconciliation pass over every live campaign.

    Reconciliation, not scheduling: it looks at what each campaign's state
    says should happen next and does at most one wave per site. That is
    what makes it safe to run on a timer, safe to run twice, and safe to
    run alongside an operator pressing advance -- all three paths call
    `advance_campaign`, and the dispatch ledger's composite key makes a
    duplicate physically unable to exist.

    Restart-safe for the same reason: nothing is held in memory between
    passes. Every decision is re-derived from persisted campaign, wave,
    plan and ledger rows, so a process that dies mid-campaign resumes
    exactly where the database says it was.
    """
    from harkeniq_cc.db.repos import CampaignRepo

    stats = {"campaigns": 0, "advanced": 0, "blocked": 0, "halted": 0}
    async with state.sessionmaker() as session:
        repo = CampaignRepo(session)
        live = [
            c for c in await repo.list_all(tenant_id)
            if c.status in (STATUS_RUNNING, STATUS_AWAITING_APPROVAL)
        ]
        for campaign in live:
            # A campaign awaiting approval still gets a pass: its
            # autonomous sites (if any) may run, and an approved wave
            # becomes eligible the moment the decision lands.
            try:
                result = await advance_campaign(
                    session, state, tenant_id=tenant_id, campaign=campaign,
                )
            except Exception:
                logger.exception("campaign %s pass failed", campaign.id)
                continue
            stats["campaigns"] += 1
            stats["advanced"] += len(result["advanced"])
            stats["blocked"] += len(result["blocked"])
            stats["halted"] += len(result["halted"])
        await session.commit()
    return stats


async def campaign_loop(state) -> None:
    """Background reconciliation for campaigns (Q2).

    The orchestration mechanism for unattended progression. It is NOT an
    execution engine: it decides only which wave is next and then hands
    the work to `DispatchAction`, the same verb an operator's approval
    uses, onto the same directive transport and the same node funnel.
    """
    import asyncio

    interval = float(getattr(state.config, "campaign_interval_s", 60.0))
    tenant_id = state.config.tenant_id
    while True:
        try:
            if tenant_id:
                stats = await run_campaigns_once(state, tenant_id)
                if stats["campaigns"]:
                    logger.info("Campaigns: %s", stats)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- one bad pass must not kill the loop
            logger.exception("Campaign pass failed")
        await asyncio.sleep(interval)


async def settle_dispatched_waves(session, *, tenant_id: str, campaign) -> dict:
    """Close out dispatched waves from the outcomes they produced.

    Reads the EXISTING outcome path -- `cc_outcome_history`, the rows the
    fleet poller already ingests -- matched on the campaign's own actor.
    There is no second outcome channel, which is why a campaign's work
    also reaches error budgets, the aggregator, pattern detection and
    learning without anything extra being wired.

    Within a site the R4-3 rule stands: the first device failure halts
    that site, after which its later authorizations are voided (Q3).
    Across sites nothing propagates -- a halted site is not a halted
    campaign.
    """
    from sqlalchemy import select

    from harkeniq_cc.db.models import CCOutcomeHistory

    repo = CampaignRepo(session)
    actor = campaign_actor(campaign.id, campaign.version)
    out = {"completed": 0, "halted": 0, "waiting": 0}

    dispatched = [
        w for w in await repo.waves(campaign.id) if w.status == "dispatched"
    ]
    if not dispatched:
        return out

    rows = (
        await session.execute(
            select(CCOutcomeHistory).where(CCOutcomeHistory.actor == actor)
        )
    ).scalars().all()
    outcomes: dict[str, str] = {}
    for row in rows:
        # Last outcome per device wins: a retry settles the device.
        outcomes[row.device_agent_id] = (row.outcome or "").upper()

    ledger = {
        (d.site_id, d.wave_index, d.plan_hash, d.device_agent_id): d
        for d in await repo.dispatches(campaign.id)
        if d.campaign_version == campaign.version
    }

    for wave in dispatched:
        rows = [
            row for (site_id, idx, plan_hash, _), row in ledger.items()
            if site_id == wave.site_id and idx == wave.wave_index
            and plan_hash == wave.plan_hash
        ]
        # A device the Site Manager refused at dispatch never reaches a
        # node, so no outcome will ever arrive for it. It is a wave
        # failure, not something to wait on.
        refused = [r.device_agent_id for r in rows if not r.accepted]
        accepted = [r.device_agent_id for r in rows if r.accepted]

        if not accepted:
            # Nothing in this wave reached a node. Marking it completed
            # would let the site walk forward over work that never ran --
            # found by the lifecycle test, which dispatches against a
            # Site Manager with no directive transport.
            wave.status = "failed"
            wave.settled_at = _utcnow()
            out["halted"] += 1
            site_row = await repo.get_site(campaign.id, wave.site_id)
            if site_row is not None:
                await _halt_site(
                    session, tenant_id=tenant_id, campaign=campaign,
                    site_row=site_row,
                    reason=(
                        f"wave {wave.wave_index} reached no device: "
                        f"{len(refused)} dispatch(es) were refused by the site"
                    ),
                )
            continue

        settled = {d: outcomes[d] for d in accepted if d in outcomes}
        if len(settled) < len(accepted):
            out["waiting"] += 1
            continue

        failures = [d for d, o in settled.items() if o and o != "SUCCESS"]
        failures += refused
        for device_id, result in settled.items():
            target = await repo.get_target(campaign.id, device_id)
            if target is not None:
                target.outcome = result
                target.status = "completed" if result == "SUCCESS" else "failed"
                target.updated_at = _utcnow()

        wave.settled_at = _utcnow()
        site_row = await repo.get_site(campaign.id, wave.site_id)
        if failures:
            wave.status = "failed"
            out["halted"] += 1
            if site_row is not None:
                await _halt_site(
                    session, tenant_id=tenant_id, campaign=campaign,
                    site_row=site_row,
                    reason=(
                        f"wave {wave.wave_index} failed on "
                        f"{len(failures)} device(s): {', '.join(sorted(failures)[:5])}"
                    ),
                )
        else:
            wave.status = "completed"
            out["completed"] += 1
            if site_row is not None:
                site_row.current_wave = wave.wave_index + 1
    await session.flush()
    return out
