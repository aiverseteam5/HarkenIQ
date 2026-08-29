"""Fleet poller: periodically pull device snapshots from all registered SMs.

R3b-3: also extracts action outcomes from FleetSnapshot.outcomes for the
CC learning pipeline (R-C1 fleet-wide pattern detection).
"""

from __future__ import annotations

import asyncio
import logging

from harkeniq_cc.db.repos import FleetCacheRepo, SiteRepo
from harkeniq_cc.sm_client import SMClient

logger = logging.getLogger("harkeniq.cc.fleet_poller")


async def fleet_poll_loop(state) -> None:
    """Poll all registered Site Managers for fleet snapshots.

    Each cycle iterates every active site, calls GetFleetSnapshot via
    SMClient, and upserts the returned devices into cc_fleet_cache.
    Failures are logged per-site and do not abort the cycle.
    """
    interval = state.config.site_poll_interval_s
    client = SMClient(state.config.sm_tls_ca)
    logger.info("Fleet poller started (interval=%.0fs)", interval)
    while True:
        await asyncio.sleep(interval)
        try:
            async with state.sessionmaker() as session:
                sites = await SiteRepo(session).list_all(state.config.tenant_id)
                for site in sites:
                    try:
                        snapshot = await client.get_fleet_snapshot(
                            site.sm_endpoint,
                            site.sm_token,
                            state.config.tenant_id,
                            site.id,
                        )
                        cache = FleetCacheRepo(session)
                        await cache.clear_site(site.id)
                        for dev in snapshot.get("devices", []):
                            await cache.upsert_device(
                                site_id=site.id,
                                agent_id=dev["agent_id"],
                                agent_name=dev.get("agent_name", ""),
                                vendor=dev.get("vendor", ""),
                                model=dev.get("model", ""),
                                observation=dev.get("observation", ""),
                                health=dev.get("health", ""),
                                subsystems=dev.get("subsystems"),
                                service_tag=dev.get("service_tag", ""),
                                firmware=dev.get("firmware"),
                                device_class=dev.get("device_class", ""),
                            )
                        # R3b-3: ingest action outcomes for fleet learning
                        outcomes = snapshot.get("outcomes", [])
                        if outcomes:
                            await _ingest_outcomes(session, site.id, outcomes)

                        # QA-033: ingest SM-generated candidate skills for
                        # the R-C1 learning loop
                        candidates = snapshot.get("candidate_skills", [])
                        if candidates:
                            await _ingest_candidates(
                                session, state.config.tenant_id,
                                site.id, candidates,
                            )

                        # P0 2026-08-29 (final-assessment C2): the snapshot's
                        # pending_actions were decoded and DISCARDED, so
                        # cc_approval_routes was never written in production
                        # and the tenant approval surface listed nothing.
                        await _ingest_pending_actions(
                            session, site.id,
                            snapshot.get("pending_actions", []),
                        )

                        # S4: real incidents, with their diagnosis, replace
                        # the critical-health pseudo-incidents.
                        await _ingest_incidents(
                            session, state.config.tenant_id, site.id,
                            snapshot.get("incidents", []),
                        )

                        await SiteRepo(session).update_last_seen(site)
                        await session.commit()

                        # QA-022: re-converge autonomy policy + stop
                        # switch each cycle (SM state is in-process; an
                        # SM restart or missed immediate push heals here).
                        try:
                            from harkeniq_cc.policy_push import (
                                build_autonomy_payload,
                            )
                            payload = await build_autonomy_payload(
                                session, state.config.tenant_id
                            )
                            await client.push_policy(
                                site.sm_endpoint,
                                site.sm_token,
                                state.config.tenant_id,
                                site.id,
                                autonomy_budgets_json=payload,
                            )
                        except Exception as exc:
                            logger.warning(
                                "Policy push failed for %s: %s",
                                site.site_name, exc,
                            )
                        logger.debug(
                            "Fleet poll OK for %s: %d devices, %d outcomes",
                            site.site_name,
                            len(snapshot.get("devices", [])),
                            len(outcomes),
                        )
                    except Exception as exc:
                        logger.error(
                            "Fleet poll failed for %s: %s", site.site_name, exc
                        )
                        await session.rollback()
        except Exception as exc:
            logger.error("Fleet poll cycle error: %s", exc)


async def _ingest_outcomes(session, site_id: str, outcomes: list[dict]) -> None:
    """Store action outcomes in cc_outcome_history for pattern detection."""
    from datetime import datetime, timezone
    from harkeniq_cc.db.models import CCOutcomeHistory

    for oc in outcomes:
        row = CCOutcomeHistory(
            site_id=site_id,
            action_id=oc.get("action_id", ""),
            action_type=oc.get("action_type", ""),
            device_agent_id=oc.get("device_agent_id", ""),
            vendor=oc.get("vendor", ""),
            model=oc.get("model", ""),
            outcome=oc.get("outcome", "UNKNOWN"),
            fault_resolved=oc.get("fault_resolved"),
            recorded_at=datetime.fromtimestamp(
                oc.get("recorded_at_unix", 0), tz=timezone.utc,
            ) if oc.get("recorded_at_unix") else datetime.now(timezone.utc),
        )
        session.add(row)
    logger.info("Ingested %d outcomes from site %s", len(outcomes), site_id)


async def _ingest_pending_actions(
    session, site_id: str, pending: list[dict],
) -> None:
    """Reconcile cc_approval_routes with the SM's current pending set (C2).

    New pending actions become undecided routes (idempotent on action_id).
    Undecided routes whose action no longer waits at the SM — decided
    locally via the SM dashboard/CLI, or expired — are closed as
    ``superseded`` so the CC queue never shows a decision that can no
    longer be delivered. Routes already decided at CC are untouched.
    """
    from harkeniq_cc.db.repos import ApprovalRouteRepo

    repo = ApprovalRouteRepo(session)
    pending_ids: set[str] = set()
    created = 0
    for act in pending:
        action_id = act.get("action_id", "")
        if not action_id:
            continue
        pending_ids.add(action_id)
        if await repo.get_by_action_id(action_id) is None:
            await repo.create(
                site_id=site_id,
                action_id=action_id,
                action_type=act.get("type", ""),
                device_agent_id=act.get("device_agent_id", ""),
            )
            created += 1
    superseded = 0
    for route in await repo.list_undecided_for_site(site_id):
        if route.action_id not in pending_ids:
            await repo.update_decision(route, "superseded", "sm")
            superseded += 1
    if created or superseded:
        logger.info(
            "Approval routes reconciled for site %s: %d new, %d superseded",
            site_id, created, superseded,
        )


async def _ingest_incidents(
    session, tenant_id: str, site_id: str, incidents: list[dict],
) -> None:
    """Persist the site's open incidents; infer resolution by absence (D3).

    The snapshot carries only OPEN incidents, so anything previously open
    at this site and missing now has cleared there. Resolution is inferred,
    never explained — a reason needs its own evidence and is deferred.
    """
    from harkeniq_cc.db.repos import IncidentRepo

    repo = IncidentRepo(session)
    present: set[str] = set()
    for inc in incidents:
        if not inc.get("incident_id"):
            continue
        present.add(inc["incident_id"])
        await repo.upsert(tenant_id, site_id, inc)
    resolved = await repo.resolve_absent(tenant_id, site_id, present)
    if incidents or resolved:
        logger.info(
            "Incidents for site %s: %d open, %d inferred resolved",
            site_id, len(present), resolved,
        )


async def _ingest_candidates(
    session, tenant_id: str, site_id: str, candidates: list[dict],
) -> None:
    """Store SM candidate skills in cc_candidate_skills (QA-033)."""
    from harkeniq_cc.db.repos import CandidateSkillRepo

    repo = CandidateSkillRepo(session)
    for cand in candidates:
        if not cand.get("skill_id"):
            continue
        await repo.upsert(tenant_id, site_id, cand)
    logger.info(
        "Ingested %d candidate skills from site %s", len(candidates), site_id
    )
