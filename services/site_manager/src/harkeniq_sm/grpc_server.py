"""gRPC services: agent-initiated + Central-Command-initiated RPCs.

AgentServiceServicer: agent → SM (agent-initiated, SM never dials nodes).
SiteManagerServiceServicer: CC → SM (CC dials SM to register, poll fleet,
route approvals, collect usage, and push policy).

TLS server credentials + bearer-token interceptor unless the config is
explicitly insecure (lab / unit tests). Action RPCs delegate to the
approvals service once it is attached; until then they are politely
refused so agents keep retrying without error noise.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import grpc
from sqlalchemy import func, select

from harkeniq.proto import harkeniq_pb2, harkeniq_pb2_grpc
from harkeniq_sm.approvals import ApprovalService
from harkeniq_sm.auth import BearerTokenInterceptor
from harkeniq_sm.config import SMConfig
from harkeniq_sm.coverage import observation_state, worst_health
from harkeniq_sm.db.models import Device, DeviceSubsystemState
from harkeniq_sm.db.repos import ActionRepo, DeviceRepo, IncidentRepo, SiteRepo, StatusRepo
from harkeniq_sm.ingest import IngestService

logger = logging.getLogger("harkeniq.sm.grpc")


class AgentServiceServicer(harkeniq_pb2_grpc.AgentServiceServicer):
    def __init__(
        self, ingest: IngestService, approvals=None, identity_service=None,
        directives=None, autonomy=None, suppression=None,
    ) -> None:
        self.ingest = ingest
        self.approvals = approvals  # attached by the approvals phase
        self.identity_service = identity_service  # R3a: AgentIdentityService
        self.directives = directives  # R5: DirectiveService
        self.autonomy = autonomy  # QA-021: SMAutonomyEnforcer
        self.suppression = suppression  # QA-021: SuppressionEngine

    async def RegisterAgent(self, request, context):
        site_name = await self.ingest.register(
            agent_id=request.agent_id,
            agent_name=request.agent_name,
            vendor=request.vendor,
            model=request.model,
            service_tag=request.service_tag,
            bmc_location_json=request.bmc_location_json,
            peers=list(request.peers),
            firmware_json=request.firmware_json,
            device_class=request.device_class,
        )

        # R3a: if agent sent a public key, register identity and issue cert
        sm_public_key_pem = b""
        agent_certificate = b""
        peer_keys: dict[str, bytes] = {}
        peer_keys_signature = b""
        if self.identity_service and request.public_key_pem:
            try:
                sm_public_key_pem, agent_certificate = (
                    await self.identity_service.register_agent(
                        agent_id=request.agent_id,
                        public_key_pem=bytes(request.public_key_pem),
                        site_name=site_name,
                    )
                )
            except ValueError as e:
                logger.warning("Agent identity registration failed: %s", e)

            # R3b-2: distribute peer public keys
            try:
                peer_keys, peer_keys_signature = (
                    await self.identity_service.get_peer_keys(
                        exclude_agent_id=request.agent_id,
                    )
                )
            except Exception as e:
                logger.warning("Peer key distribution failed: %s", e)

        return harkeniq_pb2.RegistrationAck(
            accepted=True,
            site_name=site_name,
            sm_public_key_pem=sm_public_key_pem,
            agent_certificate=agent_certificate,
            peer_keys=peer_keys,
            peer_keys_signature=peer_keys_signature,
        )

    async def Heartbeat(self, request, context):
        accepted = await self.ingest.heartbeat(
            agent_id=request.agent_id,
            agent_name=request.agent_name,
            state=request.state,
            health_summary=dict(request.health_summary),
            peer_status=dict(request.peer_status),
        )

        # R3a: issue signed authorization lease if identity service is active.
        # QA-021: the lease carries the enforcer's real budgets, stop-switch
        # state, and the suppression engine's active domains — no more
        # unlimited-budget defaults.
        lease_bytes = b""
        lease_expiry_unix = 0
        if self.identity_service and accepted:
            try:
                kwargs: dict = {}
                if self.autonomy is not None:
                    from harkeniq.actions.executor import DEFAULT_ALLOW_LIST

                    policy_actions = self.autonomy.policy_actions()
                    classes = sorted(set(DEFAULT_ALLOW_LIST) | set(policy_actions))
                    budgets = {a: -1 for a in classes}
                    budgets.update(
                        self.autonomy.get_budget_for_agent(request.agent_id)
                    )
                    # S5: a class the error budget has dropped back gets a
                    # remaining budget of 0, which the lease reads as
                    # "propose" — drop back to Approve, exactly the A2.2
                    # semantic. Not "deny": the action is still the right
                    # one, it just no longer runs without a human.
                    async with self.ingest.sessionmaker() as budget_session:
                        from harkeniq_sm.db.repos import ErrorBudgetRepo

                        dropped = await ErrorBudgetRepo(
                            budget_session
                        ).dropped_back_types()
                    for action_type in dropped:
                        if action_type in budgets:
                            budgets[action_type] = 0
                    rank = {"none": 0, "low": 1, "medium": 2, "high": 3}
                    ceiling = max(
                        ["low", *policy_actions.values()],
                        key=lambda r: rank.get(r, 1),
                    )
                    kwargs = {
                        "action_classes": classes,
                        "budget_remaining": budgets,
                        "risk_ceiling": ceiling,
                        "stop_switch": self.autonomy.stop_switch_active,
                    }
                if self.suppression is not None:
                    kwargs["suppression_domains"] = (
                        self.suppression.get_suppressed_domains()
                    )
                lease_bytes, lease_expiry_unix = (
                    await self.identity_service.issue_lease(
                        agent_id=request.agent_id, **kwargs,
                    )
                )
            except Exception as e:
                logger.warning("Lease issuance failed for %s: %s", request.agent_id, e)

        return harkeniq_pb2.HeartbeatAck(
            accepted=accepted,
            authorization_lease=lease_bytes,
            lease_expiry_unix=lease_expiry_unix,
        )

    async def ReportVerdict(self, request, context):
        accepted = await self.ingest.verdict(
            agent_id=request.agent_id,
            sensor_id=request.sensor_id,
            skill_name=request.skill_name,
            severity=request.verdict,
            evidence_json=request.evidence_json,
        )
        return harkeniq_pb2.VerdictAck(accepted=accepted)

    async def ReportAction(self, request, context):
        if self.approvals is None:
            return harkeniq_pb2.ActionAck(accepted=False)
        accepted = await self.approvals.report_action(request)
        # QA-021: completed executions draw down the site budget window
        if accepted and self.autonomy is not None and request.status == "COMPLETED":
            self.autonomy.record_execution(request.type)
        return harkeniq_pb2.ActionAck(accepted=accepted)

    async def PollActionDecisions(self, request, context):
        if self.approvals is None:
            return harkeniq_pb2.DecisionList()
        decisions = await self.approvals.poll_decisions(request.agent_id)
        return harkeniq_pb2.DecisionList(decisions=decisions)

    async def PollDirectives(self, request, context):
        """R5: hand pending SM-initiated directives to the polling agent."""
        if self.directives is None:
            return harkeniq_pb2.DirectiveList()
        directives = await self.directives.poll(request.agent_id)
        return harkeniq_pb2.DirectiveList(directives=directives)

    async def ReportDirectiveResult(self, request, context):
        """R5: agent settles a directive it executed."""
        if self.directives is None:
            return harkeniq_pb2.DirectiveAck(accepted=False)
        accepted = await self.directives.report_result(
            agent_id=request.agent_id,
            directive_id=request.directive_id,
            success=request.success,
            detail=request.detail,
        )
        return harkeniq_pb2.DirectiveAck(accepted=accepted)

    async def PushSkill(self, request, context):
        """R3b-1 C7: SM pushes validated skills to agents.

        This handler is on the AgentServiceServicer because the SM
        calls PushSkill on the agent's stub (SM -> Agent direction).
        In practice, the SM iterates registered agents and pushes.
        The agent validates + hot-loads the skill.
        """
        # Agent-side handling: validate YAML and accept/reject
        # For now, the SM servicer doesn't implement this (agents do).
        # This stub exists for proto compatibility.
        return harkeniq_pb2.SkillDistributionAck(
            accepted=True, reason="accepted by SM stub"
        )


_SEVERITY_RANK = {"OK": 0, "TRENDING": 1, "UNKNOWN": 2, "WARNING": 3, "CRITICAL": 4}


def _worst_severity(severities: list[str]) -> str:
    """Return the worst severity from a list; defaults to 'ok'."""
    worst = "OK"
    for s in severities:
        if _SEVERITY_RANK.get(s, 2) > _SEVERITY_RANK.get(worst, 0):
            worst = s
    return worst.lower()


def _ts(dt: Optional[datetime]) -> int:
    """Convert a datetime to a unix timestamp; 0 if None."""
    if dt is None:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


class SiteManagerServiceServicer(harkeniq_pb2_grpc.SiteManagerServiceServicer):
    """RPCs from Central Command (CC dials SM)."""

    def __init__(
        self,
        sessionmaker,
        approvals: ApprovalService,
        config: SMConfig,
        directives=None,
        autonomy=None,
        ingest=None,
        suppression=None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.approvals = approvals
        self.config = config
        self.directives = directives  # R5-2: skill installs from CC
        self.autonomy = autonomy  # QA-021/022: SMAutonomyEnforcer
        self.ingest = ingest  # QA-033: fleet-pattern mirror lives here
        # S5: SuppressionEngine — its active domains are safety state CC
        # must see, not just something leases carry to agents.
        self.suppression = suppression

    async def InstallSkill(self, request, context):
        """R5-2: CC pushes a marketplace skill; queue directives for
        every device on the site. Static validation runs BEFORE anything
        is queued -- an unparseable skill never reaches an agent."""
        if self.directives is None:
            return harkeniq_pb2.SiteSkillInstallAck(
                accepted=False, reason="directive transport not configured"
            )
        from harkeniq_sm.skill_validation import SkillValidator

        result = SkillValidator().validate_static(request.yaml_content)
        if not result.passed:
            return harkeniq_pb2.SiteSkillInstallAck(
                accepted=False,
                reason="; ".join(result.errors)[:256] or "validation failed",
            )
        async with self.sessionmaker() as session:
            site = await SiteRepo(session).get_or_create(self.config.site_name)
            devices = list(await DeviceRepo(session).list_for_site(site.id))
            await session.commit()
        queued = 0
        for device in devices:
            await self.directives.enqueue_skill_install(
                device_id=device.id,
                skill_id=request.skill_id,
                skill_version=request.skill_version or "1",
                yaml_content=request.yaml_content,
                tier=request.tier or "community",
                validation_state=request.validation_state or "tested",
                issued_by=request.issued_by or "marketplace",
            )
            queued += 1
        logger.info(
            "InstallSkill %s v%s: %d directive(s) queued",
            request.skill_id, request.skill_version, queued,
        )
        return harkeniq_pb2.SiteSkillInstallAck(accepted=True, queued=queued)

    async def DispatchAction(self, request, context):
        """A1: queue one decided action for a device on this site.

        This verb DELIVERS a decision; it does not make one. Central
        Command has already established the actor, the permission and
        the approval (or the tenant's autonomy grant); the node still
        runs its unchanged gate funnel and can refuse. The Site Manager's
        job here is the two things only it can do: resolve the device,
        and refuse work that its own live safety state already forbids.
        """
        if self.directives is None:
            return harkeniq_pb2.ActionDispatchAck(
                accepted=False, reason="directive transport not configured"
            )
        action_type = (request.action_type or "").strip().upper()
        if not action_type:
            return harkeniq_pb2.ActionDispatchAck(
                accepted=False, reason="action_type is required"
            )
        # An action class the executor does not implement must never be
        # queued: it would sit as a directive nothing can ever settle.
        from harkeniq.models import ActionType

        try:
            ActionType(action_type)
        except ValueError:
            return harkeniq_pb2.ActionDispatchAck(
                accepted=False,
                reason=f"unknown action type {action_type!r}",
            )
        try:
            params = json.loads(request.params_json or "{}")
            if not isinstance(params, dict):
                raise ValueError("params_json must be an object")
        except (json.JSONDecodeError, ValueError) as exc:
            return harkeniq_pb2.ActionDispatchAck(
                accepted=False, reason=f"unparseable params_json: {exc}"
            )

        async with self.sessionmaker() as session:
            device = await DeviceRepo(session).get_by_agent_id(
                request.device_agent_id
            )
            device_id = device.id if device else None
        if device_id is None:
            return harkeniq_pb2.ActionDispatchAck(
                accepted=False,
                reason=f"device {request.device_agent_id!r} is not known at this site",
            )

        # Safety belongs to the site, so the site enforces it here rather
        # than trusting the caller's snapshot of it. The node re-checks
        # everything again; this refusal just avoids queueing work that
        # is already known to be refused.
        if self.autonomy is not None:
            if self.autonomy.stop_switch_active:
                return harkeniq_pb2.ActionDispatchAck(
                    accepted=False, reason="site stop switch is active"
                )
            if request.authorization == "autonomous_grant":
                async with self.sessionmaker() as session:
                    from harkeniq_sm.db.repos import ErrorBudgetRepo

                    dropped = await ErrorBudgetRepo(session).dropped_back_types()
                if action_type in dropped:
                    return harkeniq_pb2.ActionDispatchAck(
                        accepted=False,
                        reason=(
                            f"autonomy for {action_type} was withdrawn by the "
                            f"error budget; a human decision is required"
                        ),
                    )

        directive_id = await self.directives.enqueue_action(
            device_id=device_id,
            action_type=action_type,
            params=params,
            issued_by=request.decided_by or request.actor or "central-command",
            actor=request.actor,
            authorization_basis=request.authorization,
            proposal_id=request.proposal_id,
        )
        logger.info(
            "DispatchAction %s on %s for %s (%s) -> directive %s",
            action_type, request.device_agent_id, request.actor or "unattributed",
            request.authorization or "sm_authority", directive_id,
        )
        return harkeniq_pb2.ActionDispatchAck(
            accepted=True, directive_id=directive_id
        )

    async def RegisterSite(self, request, context):
        if not request.license_key_fingerprint:
            return harkeniq_pb2.SiteRegistrationAck(
                accepted=False,
                reason="license_key_fingerprint is required",
            )
        # QA-037: this is the bootstrap RPC (exempt from the token
        # interceptor); the fingerprint is its credential. When the SM is
        # deployed with an expected fingerprint, it must match.
        expected = getattr(self.config, "license_fingerprint", "")
        # P0 2026-08-29 (final assessment §6): with NO fingerprint
        # configured, this RPC accepted any non-empty value — and it is
        # the RPC that hands out the site token. Secure mode now refuses
        # registration until a fingerprint is configured; the explicit
        # ``insecure`` lab flag is the only bypass.
        if not expected and not getattr(self.config, "insecure", False):
            logger.warning(
                "RegisterSite refused: no license_fingerprint configured "
                "(fail closed) from cc=%s", request.cc_endpoint,
            )
            return harkeniq_pb2.SiteRegistrationAck(
                accepted=False,
                reason="SM has no license_fingerprint configured (fail closed)",
            )
        if expected and request.license_key_fingerprint != expected:
            logger.warning(
                "RegisterSite rejected: fingerprint mismatch from cc=%s",
                request.cc_endpoint,
            )
            return harkeniq_pb2.SiteRegistrationAck(
                accepted=False,
                reason="license fingerprint mismatch",
            )
        logger.info(
            "Site registration from CC: tenant=%s site=%s name=%s cc=%s",
            request.tenant_id, request.site_id, request.site_name,
            request.cc_endpoint,
        )
        return harkeniq_pb2.SiteRegistrationAck(
            accepted=True,
            site_token=self.config.site_token,
        )

    async def GetFleetSnapshot(self, request, context):
        async with self.sessionmaker() as session:
            devices = await DeviceRepo(session).list_for_site(request.site_id)
            # If no devices found by site_id, fall back to listing all devices
            # (lab/test environments may not have matching site_ids).
            if not devices:
                all_devs = (
                    await session.execute(select(Device).order_by(Device.agent_name))
                ).scalars().all()
                devices = all_devs

            fleet_devices = []
            for device in devices:
                status = await StatusRepo(session).get(device.id)
                obs = observation_state(
                    status.last_heartbeat_at if status else None, self.config,
                )
                # Subsystem rollup
                subs = (
                    await session.execute(
                        select(DeviceSubsystemState).where(
                            DeviceSubsystemState.device_id == device.id
                        )
                    )
                ).scalars().all()
                subsystem_map = {s.subsystem: s.severity for s in subs}
                if obs == "observed" and subsystem_map:
                    health = _worst_severity(list(subsystem_map.values()))
                elif obs == "observed":
                    health = worst_health(status.last_health if status else None)
                else:
                    health = "unknown"

                fleet_devices.append(
                    harkeniq_pb2.FleetDevice(
                        agent_id=device.agent_id,
                        agent_name=device.agent_name,
                        vendor=device.vendor,
                        model=device.model,
                        observation=obs,
                        health=health,
                        subsystems_json=json.dumps(subsystem_map) if subsystem_map else "{}",
                        last_seen_unix=_ts(device.last_seen_at),
                        service_tag=device.service_tag or "",
                        inventory_json=json.dumps(
                            {"firmware": device.firmware}
                        ) if device.firmware else "",
                        device_class=device.device_class or "server",
                    )
                )

            # Open incidents
            open_incidents = await IncidentRepo(session).list_open()
            fleet_incidents = []
            for inc in open_incidents:
                # Resolve device_agent_id from device_id
                device_agent_id = ""
                if inc.device_id:
                    dev = await DeviceRepo(session).get(inc.device_id)
                    if dev:
                        device_agent_id = dev.agent_id
                fleet_incidents.append(
                    harkeniq_pb2.FleetIncident(
                        incident_id=inc.id,
                        kind=inc.kind,
                        status=inc.status,
                        title=inc.title,
                        device_agent_id=device_agent_id,
                        subsystem=inc.subsystem or "",
                        opened_at_unix=_ts(inc.opened_at),
                        # S4: the diagnosis and its correlation context ride
                        # the snapshot, so the tenant surface can finally show
                        # WHY, not just that something is wrong.
                        parent_incident_id=inc.parent_id or "",
                        confidence=inc.confidence or 0.0,
                        inferred=bool(inc.inferred),
                        correlation_meta_json=(
                            json.dumps(inc.correlation_meta)
                            if inc.correlation_meta else ""
                        ),
                        explanation_json=(
                            json.dumps(inc.explanation) if inc.explanation else ""
                        ),
                    )
                )

            # Pending actions
            pending_actions = await ActionRepo(session).list_by_status("pending")
            fleet_actions = []
            for act in pending_actions:
                dev = await DeviceRepo(session).get(act.device_id)
                device_agent_id = dev.agent_id if dev else ""
                proposed_at_unix = 0
                if act.proposed_at:
                    try:
                        dt = datetime.fromisoformat(act.proposed_at)
                        proposed_at_unix = _ts(dt)
                    except (ValueError, TypeError):
                        pass
                fleet_actions.append(
                    harkeniq_pb2.FleetAction(
                        action_id=act.id,
                        type=act.type,
                        device_agent_id=device_agent_id,
                        severity=act.verdict_severity,
                        skill_name=act.skill_name,
                        status=act.status,
                        proposed_at_unix=proposed_at_unix,
                    )
                )

            # R3b-3: include unreported action outcomes for CC learning
            fleet_outcomes = []
            try:
                from harkeniq_sm.db.models import ActionOutcomeRow
                unreported = (
                    await session.execute(
                        select(ActionOutcomeRow).where(
                            ActionOutcomeRow.reported_to_cc == False  # noqa: E712
                        ).limit(100)
                    )
                ).scalars().all()
                for oc in unreported:
                    # Resolve device vendor/model
                    dev = await DeviceRepo(session).get(oc.device_id) if oc.device_id else None
                    fleet_outcomes.append(
                        harkeniq_pb2.FleetOutcome(
                            action_id=oc.action_id,
                            action_type=oc.action_type,
                            device_agent_id=dev.agent_id if dev else oc.device_id,
                            outcome=oc.outcome,
                            fault_resolved=oc.fault_resolved or False,
                            vendor=dev.vendor if dev else "",
                            model=dev.model if dev else "",
                            recorded_at_unix=_ts(oc.recorded_at),
                            # A1: attribution rides the evidence path, so
                            # an execution is still attributable once it
                            # has become a number in a success rate.
                            actor=oc.actor or "",
                        )
                    )
                    oc.reported_to_cc = True
                await session.commit()
            except Exception as e:
                logger.debug("Outcome reporting skipped: %s", e)

            # QA-033 feedback half: unreported candidate skills ride up
            # to CC for the R-C1 learning loop (same once-only pattern).
            candidate_skills = []
            try:
                import json as _json

                from harkeniq_sm.db.models import CandidateSkillRow
                unreported_cands = (
                    await session.execute(
                        select(CandidateSkillRow).where(
                            CandidateSkillRow.reported_to_cc == False  # noqa: E712
                        ).limit(20)
                    )
                ).scalars().all()
                for cand in unreported_cands:
                    candidate_skills.append(
                        harkeniq_pb2.CandidateSkill(
                            skill_id=cand.skill_id,
                            yaml_text=cand.yaml_text,
                            source_device=cand.source_device,
                            source_component=cand.source_component,
                            validation_state=cand.validation_state,
                            generated_at_unix=_ts(cand.generated_at),
                            warnings_json=_json.dumps(cand.warnings or []),
                            dry_run_matches=cand.dry_run_matches,
                        )
                    )
                    cand.reported_to_cc = True
                await session.commit()
            except Exception as e:
                logger.debug("Candidate skill reporting skipped: %s", e)

            # S5: the site's live autonomy safety state rides the snapshot.
            # Assembled inside the session so a failure here degrades to
            # `reported=False` — CC then shows the site as NOT REPORTING,
            # which is the honest answer. An unobserved safety state must
            # never round down to "safe".
            safety = await self._safety_state(session)

        return harkeniq_pb2.FleetSnapshot(
            devices=fleet_devices,
            incidents=fleet_incidents,
            pending_actions=fleet_actions,
            snapshot_at_unix=int(time.time()),
            outcomes=fleet_outcomes,
            candidate_skills=candidate_skills,
            safety=safety,
        )

    async def _safety_state(self, session) -> "harkeniq_pb2.FleetSafetyState":
        """Compose FleetSafetyState from the live enforcer + persisted budgets."""
        try:
            from harkeniq_sm.db.repos import ErrorBudgetRepo

            suppressions = []
            engine = self.suppression
            if engine is not None:
                for domain_id, s in engine.get_state()[
                    "active_suppressions"
                ].items():
                    suppressions.append(harkeniq_pb2.SuppressedDomain(
                        domain_id=domain_id,
                        event_family=s.get("event_family", "") or "",
                        trigger_reason=s.get("trigger_reason", "") or "",
                        device_count=int(s.get("device_count", 0) or 0),
                        triggered_at_unix=int(s.get("triggered_at", 0) or 0),
                        all_clear_at_unix=int(s.get("all_clear_at") or 0),
                    ))

            budgets = []
            for row in await ErrorBudgetRepo(session).list_all():
                budgets.append(harkeniq_pb2.ActionErrorBudget(
                    action_type=row.action_type,
                    success_count=row.success_count or 0,
                    failure_count=row.failure_count or 0,
                    total_count=row.total_count or 0,
                    min_success_rate=row.min_success_rate or 0.95,
                    dropped_back=bool(row.dropped_back),
                    dropped_back_at_unix=_ts(row.dropped_back_at),
                ))

            site_budgets = []
            stop_switch = False
            if self.autonomy is not None:
                state = self.autonomy.get_state()
                stop_switch = bool(state.get("stop_switch"))
                for action_type, vals in (state.get("budgets") or {}).items():
                    site_budgets.append(harkeniq_pb2.SiteBudgetRemaining(
                        action_type=action_type,
                        remaining=int(vals.get("remaining", -1)),
                    ))

            return harkeniq_pb2.FleetSafetyState(
                reported=True,
                as_of_unix=int(time.time()),
                sm_stop_switch=stop_switch,
                suppressions=suppressions,
                error_budgets=budgets,
                site_budgets=site_budgets,
            )
        except Exception as e:  # noqa: BLE001 — degrade to unknown, never to safe
            logger.warning("Safety state unavailable for snapshot: %s", e)
            return harkeniq_pb2.FleetSafetyState(reported=False)

    async def RouteApproval(self, request, context):
        try:
            if request.decision == "approved":
                await self.approvals.approve(request.action_id, request.decided_by)
            elif request.decision == "denied":
                await self.approvals.deny(request.action_id, request.decided_by)
            else:
                return harkeniq_pb2.ApprovalRouteAck(
                    accepted=False,
                    delivered=False,
                    reason=f"unknown decision: {request.decision!r}",
                )
            return harkeniq_pb2.ApprovalRouteAck(accepted=True, delivered=True)
        except Exception as exc:
            logger.warning("RouteApproval failed: %s", exc)
            return harkeniq_pb2.ApprovalRouteAck(
                accepted=False, delivered=False, reason=str(exc),
            )

    async def GetUsageSnapshot(self, request, context):
        async with self.sessionmaker() as session:
            # Count unique devices
            node_count_result = await session.execute(
                select(func.count(Device.id))
            )
            node_count = node_count_result.scalar() or 0

            # Agent versions from AgentStatus.last_state (placeholder: version
            # info isn't yet tracked per-agent; return state distribution instead)
            agent_versions: dict[str, int] = {}
            statuses = await StatusRepo(session).list_all()
            for s in statuses:
                ver = s.last_state or "unknown"
                agent_versions[ver] = agent_versions.get(ver, 0) + 1

        return harkeniq_pb2.UsageSnapshot(
            tenant_id=request.tenant_id,
            site_id=request.site_id,
            date=request.date,
            node_count=node_count,
            agent_versions=agent_versions,
        )

    async def PushPolicy(self, request, context):
        """QA-021/022: apply CC autonomy policy for real (was log-and-ack).

        ``autonomy_budgets_json`` carries either a bare policy list or
        ``{"stop_switch": bool, "stop_switch_by": str, "policies": [...]}``
        where each policy is {action_type, max_per_window, window_seconds,
        risk_level}. Stop-switch transitions are audit-chained.
        """
        logger.info(
            "PushPolicy from CC: tenant=%s site=%s policies=%d bytes budgets=%d bytes",
            request.tenant_id, request.site_id,
            len(request.approval_policies_json),
            len(request.autonomy_budgets_json),
        )
        if self.autonomy is None:
            return harkeniq_pb2.PolicyAck(
                accepted=False, reason="autonomy enforcer not configured"
            )
        # QA-033: fleet patterns land durably (idempotent upsert) and in
        # the ingest mirror the enrichment path reads.
        if request.learned_patterns_json:
            try:
                patterns = json.loads(request.learned_patterns_json)
            except json.JSONDecodeError as e:
                return harkeniq_pb2.PolicyAck(
                    accepted=False, reason=f"invalid learned_patterns_json: {e}"
                )
            patterns = [
                p for p in (patterns if isinstance(patterns, list) else [])
                if isinstance(p, dict) and p.get("pattern_id")
            ]
            if patterns:
                from harkeniq_sm.db.repos import SMFleetPatternRepo
                async with self.sessionmaker() as session:
                    repo = SMFleetPatternRepo(session)
                    for pattern in patterns:
                        await repo.upsert(pattern)
                    await session.commit()
                if self.ingest is not None:
                    for pattern in patterns:
                        self.ingest.fleet_patterns[pattern["pattern_id"]] = pattern
                logger.info(
                    "Stored %d fleet pattern(s) from CC", len(patterns)
                )

        if not request.autonomy_budgets_json:
            return harkeniq_pb2.PolicyAck(accepted=True)
        try:
            payload = json.loads(request.autonomy_budgets_json)
        except json.JSONDecodeError as e:
            return harkeniq_pb2.PolicyAck(
                accepted=False, reason=f"invalid autonomy_budgets_json: {e}"
            )
        policies = payload if isinstance(payload, list) else payload.get("policies", [])
        policies = [p for p in policies if isinstance(p, dict) and p.get("action_type")]
        if policies:
            self.autonomy.update_policy(policies)
        if isinstance(payload, dict) and "stop_switch" in payload:
            desired = bool(payload["stop_switch"])
            by = str(
                payload.get("stop_switch_by") or f"cc:{request.tenant_id}"
            )
            if desired != self.autonomy.stop_switch_active:
                if desired:
                    self.autonomy.activate_stop_switch(by)
                else:
                    self.autonomy.deactivate_stop_switch(by)
                async with self.sessionmaker() as session:
                    from harkeniq_sm.db.repos import AuditRepo
                    await AuditRepo(session).append(
                        actor=by,
                        action="stop_switch.activate" if desired
                        else "stop_switch.deactivate",
                        subject=f"site:{request.site_id or self.config.site_name}",
                        detail={"source": "cc.push_policy"},
                    )
                    await session.commit()
        return harkeniq_pb2.PolicyAck(accepted=True)


def build_server(
    config: SMConfig,
    servicer: AgentServiceServicer,
    sm_servicer: Optional[SiteManagerServiceServicer] = None,
) -> tuple[grpc.aio.Server, int]:
    """Create the server and bind its port; returns (server, bound_port)."""
    interceptors = []
    if config.site_token:
        interceptors.append(BearerTokenInterceptor(config.site_token))
    elif not config.insecure:
        raise ValueError("site_token required unless insecure=true")

    server = grpc.aio.server(interceptors=interceptors)
    harkeniq_pb2_grpc.add_AgentServiceServicer_to_server(servicer, server)
    if sm_servicer is not None:
        harkeniq_pb2_grpc.add_SiteManagerServiceServicer_to_server(sm_servicer, server)
    address = f"{config.grpc_host}:{config.grpc_port}"

    if config.insecure and not (config.tls_cert and config.tls_key):
        port = server.add_insecure_port(address)
        logger.warning("gRPC listening INSECURE on %s (lab mode)", address)
    else:
        creds = grpc.ssl_server_credentials(
            [
                (
                    Path(config.tls_key).read_bytes(),
                    Path(config.tls_cert).read_bytes(),
                )
            ]
        )
        port = server.add_secure_port(address, creds)
        logger.info("gRPC listening (TLS) on %s", address)
    return server, port
