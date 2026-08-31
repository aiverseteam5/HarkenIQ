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
import hashlib
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
from harkeniq_sm.enrollment import EnrollmentError, EnrollmentService
from harkeniq_sm.stopswitch import SCOPE_TENANT, StopSwitchService
from harkeniq_sm.firmware_orchestrator import plan_waves
from harkeniq_sm.db.repos import (
    DomainRepo,
    ActionRepo,
    AuditRepo,
    DeviceRepo,
    IncidentRepo,
    SiteRepo,
    StatusRepo,
)
from harkeniq_sm.ingest import IngestService

logger = logging.getLogger("harkeniq.sm.grpc")


class AgentServiceServicer(harkeniq_pb2_grpc.AgentServiceServicer):
    def __init__(
        self, ingest: IngestService, approvals=None, identity_service=None,
        directives=None, autonomy=None, suppression=None,
        enrollment=None, stopswitch=None,
    ) -> None:
        self.ingest = ingest
        # E1.3: site resolution and the per-site halt. Constructed by the
        # runtime; defaulted here so every existing test double still works.
        # Defaulted from the ingest service so every existing test double
        # keeps working; `ingest=None` is a legitimate construction in the
        # auth-only tests, so neither default may assume it is present.
        self.enrollment = enrollment or (
            EnrollmentService(ingest.sessionmaker, ingest.config)
            if ingest is not None else None
        )
        self.stopswitch = stopswitch or (
            StopSwitchService(ingest.sessionmaker) if ingest is not None else None
        )
        self.approvals = approvals  # attached by the approvals phase
        self.identity_service = identity_service  # R3a: AgentIdentityService
        self.directives = directives  # R5: DirectiveService
        self.autonomy = autonomy  # QA-021: SMAutonomyEnforcer
        self.suppression = suppression  # QA-021: SuppressionEngine

    async def _agent_site_halted(self, agent_id: str):
        """Is a halt in force for this agent's own site? (E1.3)

        Returns True when halted, so the lease carries the same shape it
        always did. Falls back to the enforcer's in-memory flag only when
        the device's site cannot be resolved -- never to "not halted".
        """
        if self.stopswitch is None:
            # Not configured (a test double, or a deployment that predates
            # E1.3). Fall back to the enforcer's flag, which is the
            # pre-E1.3 behaviour -- absent machinery is not a halt.
            return self.autonomy.stop_switch_active
        try:
            async with self.ingest.sessionmaker() as session:
                device = await DeviceRepo(session).get_by_agent_id(agent_id)
                if device is None or not device.site_id:
                    return self.autonomy.stop_switch_active
                halt = await self.stopswitch.state_for(session, device.site_id)
                # The persisted halts, OR the in-memory flag: a Site
                # Manager mid-upgrade may still be carrying one.
                return halt.halted or self.autonomy.stop_switch_active
        except Exception:
            logger.exception("could not resolve the halt for %s", agent_id)
            # A safety state that cannot be READ is a halt. This is the
            # one place failing closed is right: an error here means we
            # do not know whether an operator has stopped this site.
            return True

    async def _resolve_enrollment(self, request):
        """Which site is this device at? Authoritatively (E1.3)."""
        async with self.ingest.sessionmaker() as session:
            enrollment = await self.enrollment.resolve(
                session, getattr(request, "enrollment_token", "")
            )
            if not enrollment.legacy_single_site:
                # Record the use, and the binding, on the same commit as
                # the resolution so a refused registration leaves nothing.
                await AuditRepo(session).append(
                    actor=f"agent:{request.agent_id}",
                    action="agent.enrolled",
                    subject=request.agent_id,
                    site_id=enrollment.site_id,
                    detail={
                        "site_name": enrollment.site_name,
                        "token_id": enrollment.token_id,
                    },
                )
            await session.commit()
        return enrollment

    async def _audit_enrollment_refusal(self, agent_id: str, exc) -> None:
        """A refused enrollment is a security event; it goes in the chain."""
        try:
            async with self.ingest.sessionmaker() as session:
                await AuditRepo(session).append(
                    actor=f"agent:{agent_id}",
                    action="agent.enrollment_refused",
                    subject=agent_id,
                    detail={"code": exc.code, "reason": exc.reason},
                )
                await session.commit()
        except Exception:  # pragma: no cover - auditing must not mask refusal
            logger.exception("could not audit an enrollment refusal")

    async def RegisterAgent(self, request, context):
        # E1.3, ratified D1: the site comes from the device's SITE-BOUND
        # enrollment credential, never from a field the agent fills in.
        # An agent that cannot prove its site does not get one.
        try:
            enrollment = await self._resolve_enrollment(request)
        except EnrollmentError as exc:
            logger.warning(
                "RegisterAgent refused for %s: %s", request.agent_id, exc.reason
            )
            await self._audit_enrollment_refusal(request.agent_id, exc)
            return harkeniq_pb2.RegistrationAck(
                accepted=False, reason=exc.reason
            )

        try:
            site_name = await self.ingest.register(
                site_id=enrollment.site_id,
                site_name=enrollment.site_name,
                agent_id=request.agent_id,
                agent_name=request.agent_name,
                vendor=request.vendor,
                model=request.model,
                service_tag=request.service_tag,
                bmc_location_json=request.bmc_location_json,
                peers=list(request.peers),
                firmware_json=request.firmware_json,
                device_class=request.device_class,
                capabilities_json=request.capabilities_json,
            )
        except ValueError as exc:
            # E1.3: the device is enrolled at another site. Refused with a
            # reason rather than silently left where it was.
            logger.warning(
                "RegisterAgent refused for %s: %s", request.agent_id, exc
            )
            await self._audit_enrollment_refusal(
                request.agent_id,
                type("E", (), {"code": "site_conflict", "reason": str(exc)})(),
            )
            return harkeniq_pb2.RegistrationAck(accepted=False, reason=str(exc))

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
                        site_id=enrollment.site_id,
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
                    # E0.2: the drop-back that gates THIS agent is its own
                    # site's. A failure pattern at another site the Site
                    # Manager serves must not reduce this agent's autonomy.
                    async with self.ingest.sessionmaker() as budget_session:
                        from harkeniq_sm.db.repos import ErrorBudgetRepo

                        agent_device = await DeviceRepo(
                            budget_session
                        ).get_by_agent_id(request.agent_id)
                        dropped = (
                            await ErrorBudgetRepo(
                                budget_session
                            ).dropped_back_types(agent_device.site_id)
                            if agent_device is not None and agent_device.site_id
                            else set()
                        )
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
                        # E1.3: the halt in force for THIS AGENT'S site
                        # (tenant, site or Site Manager-wide), not one
                        # boolean shared by every site on the process.
                        "stop_switch": await self._agent_site_halted(
                            request.agent_id
                        ),
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
        stopswitch=None,
    ) -> None:
        self.sessionmaker = sessionmaker
        # E1.3: the per-site halt. Defaulted from the sessionmaker so a
        # servicer built without one still resolves halts rather than
        # falling back to a Site Manager-wide boolean.
        self.stopswitch = stopswitch or StopSwitchService(sessionmaker)
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
            # E1.3 correctness: resolve the site CC actually named, not
            # this process's configured name. On a Site Manager serving
            # several sites the old lookup installed onto whichever site
            # the config happened to name -- the same shape as the
            # heartbeat and verdict bugs E1.3's gate found.
            site = await SiteRepo(session).get_by_cc_id(request.site_id)
            if site is None:
                site = await SiteRepo(session).get_or_create(self.config.site_name)
            devices = list(await DeviceRepo(session).list_for_site(site.id))
            await session.commit()

        # A2: install onto the NAMED devices only. An empty list keeps
        # the pre-A2 site-wide behaviour marketplace installs rely on;
        # an Operational Agent always names its devices, because
        # installing onto a whole site from a rack-scoped agent is a
        # scope escape dressed as a convenience.
        wanted = set(request.device_agent_ids)
        skipped_not_at_site: list[str] = []
        if wanted:
            present = {d.agent_id for d in devices}
            skipped_not_at_site = sorted(wanted - present)
            devices = [d for d in devices if d.agent_id in wanted]

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
            "InstallSkill %s v%s: %d directive(s) queued%s",
            request.skill_id, request.skill_version, queued,
            (f", {len(skipped_not_at_site)} requested device(s) are not at "
             f"this site") if skipped_not_at_site else "",
        )
        reason = ""
        if skipped_not_at_site:
            # Named, never silently dropped: a device that vanished from
            # an install with no reason cannot be told from one nobody
            # selected.
            reason = (
                f"{len(skipped_not_at_site)} requested device(s) are not at "
                f"this site: {', '.join(skipped_not_at_site[:5])}"
            )
        return harkeniq_pb2.SiteSkillInstallAck(
            accepted=True, queued=queued, reason=reason
        )

    async def PlanCampaignWaves(self, request, context):
        """S6: plan one site's campaign waves. READ-ONLY.

        Central Command owns the campaign and orders the sites; it has no
        fault-domain data and must never acquire any. This handler is the
        only place the two meet: CC names the eligible devices, this site
        answers with the exact device membership of each wave, computed by
        `plan_waves()` against its OWN authoritative fault domains.

        It writes nothing. Not a directive, not a campaign row, not an
        audit entry -- which is what makes "read-only" provable by a table
        snapshot rather than merely asserted. It cannot dispatch and it
        authorizes nothing; a plan is an answer, not a permission.

        What travels back is membership and a domain COUNT, never domain
        identities: Central Command reflecting this site's topology would
        make it a second representation of something only this tier owns.

        Determinism is part of the contract. The device list is sorted and
        each device's domains are sorted before planning, so the same
        request against the same state yields the same plan and the same
        hash -- which is what lets an approval bind to a plan at all.
        """
        from harkeniq.audit.chain import canonical_json

        action_type = (request.action_type or "").strip().upper()
        async with self.sessionmaker() as session:
            site = await SiteRepo(session).get_by_cc_id(request.site_id)
            if site is None or site.status != "active":
                reason = (
                    f"no active site bound to Central Command site id "
                    f"{request.site_id!r} at this Site Manager"
                    if site is None else f"site {site.name!r} is {site.status}"
                )
                logger.warning("PlanCampaignWaves unresolved: %s", reason)
                # planned=False is NOT "no waves". Central Command must
                # not read an unresolved site as an empty estate (A16.3).
                return harkeniq_pb2.CampaignPlan(
                    planned=False, reason=reason,
                    campaign_id=request.campaign_id,
                    campaign_version=request.campaign_version,
                    action_type=action_type,
                    generated_at_unix=int(time.time()),
                )

            device_repo = DeviceRepo(session)
            domain_repo = DomainRepo(session)

            # Resolve only devices that are REALLY at this site. One that
            # is not is reported back by name rather than dropped: a
            # device that vanished from a plan with no reason cannot be
            # told from one nobody selected.
            requested = sorted(set(request.device_agent_ids))
            resolved: dict[str, str] = {}
            unplannable: list[str] = []
            for agent_id in requested:
                device = await device_repo.get_by_agent_id(agent_id)
                if device is None or device.site_id != site.id:
                    unplannable.append(agent_id)
                else:
                    resolved[device.id] = agent_id

            domains_by_device: dict[str, list[str]] = {}
            for device_id in resolved:
                domains = await domain_repo.domains_for_device(device_id)
                domains_by_device[device_id] = sorted(d.id for d in domains)

            device_ids = sorted(resolved)
            max_wave_size = max(1, int(request.max_wave_size or 5))
            assignment = plan_waves(device_ids, domains_by_device, max_wave_size)

            by_wave: dict[int, list[str]] = {}
            domains_in_wave: dict[int, set[str]] = {}
            for device_id, wave in assignment.items():
                by_wave.setdefault(wave, []).append(resolved[device_id])
                domains_in_wave.setdefault(wave, set()).update(
                    domains_by_device.get(device_id, [])
                )

            waves = [
                harkeniq_pb2.CampaignPlanWave(
                    wave_index=idx,
                    device_agent_ids=sorted(by_wave[idx]),
                    domain_span=len(domains_in_wave.get(idx, set())),
                )
                for idx in sorted(by_wave)
            ]

            # The hash covers exactly what an approver is approving: the
            # campaign it belongs to, its version, this site, the action,
            # and the ordered wave membership. Anything material that
            # changes changes this, and a stale approval then cannot
            # address the new plan at all.
            plan_hash = hashlib.sha256(canonical_json({
                "campaign_id": request.campaign_id,
                "campaign_version": int(request.campaign_version),
                "site_id": request.site_id,
                "action_type": action_type,
                "waves": [
                    {"wave_index": w.wave_index,
                     "device_agent_ids": list(w.device_agent_ids)}
                    for w in waves
                ],
            })).hexdigest()

            return harkeniq_pb2.CampaignPlan(
                planned=True,
                site_id=request.site_id,
                campaign_id=request.campaign_id,
                campaign_version=request.campaign_version,
                action_type=action_type,
                waves=waves,
                unplannable_device_ids=unplannable,
                plan_hash=plan_hash,
                generated_at_unix=int(time.time()),
                separation_rule=(
                    f"at most one device per fault domain per wave, "
                    f"wave size capped at {max_wave_size}"
                ),
            )

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
            # E0.2: the device's own site governs its safety state.
            device_site_id = device.site_id if device else ""
        if device_id is None:
            return harkeniq_pb2.ActionDispatchAck(
                accepted=False,
                reason=f"device {request.device_agent_id!r} is not known at this site",
            )

        # Safety belongs to the site, so the site enforces it here rather
        # than trusting the caller's snapshot of it. The node re-checks
        # everything again; this refusal just avoids queueing work that
        # is already known to be refused.
        #
        # E1.3: THIS DEVICE'S OWN SITE decides. A halt at another site
        # this Site Manager happens to serve must not stop this one, and
        # a halt at this one must not be escapable by the process serving
        # others -- which is exactly what a single Site Manager-wide
        # boolean would have done in both directions.
        if self.stopswitch is not None and device_site_id:
            async with self.sessionmaker() as session:
                halt = await self.stopswitch.state_for(session, device_site_id)
            if halt.halted:
                return harkeniq_pb2.ActionDispatchAck(
                    accepted=False, reason=halt.reason
                )

        if self.autonomy is not None:
            # The enforcer's in-memory flag still counts. A Site Manager
            # mid-upgrade may be carrying one that has not been persisted
            # yet, and a halt that is live in the process must never be
            # escapable just because it is not in the table.
            if self.autonomy.stop_switch_active:
                return harkeniq_pb2.ActionDispatchAck(
                    accepted=False, reason="a stop switch is active"
                )
            if request.authorization == "autonomous_grant":
                async with self.sessionmaker() as session:
                    from harkeniq_sm.db.repos import ErrorBudgetRepo

                    # E0.2: this site's withdrawal, not the Site Manager's.
                    dropped = await ErrorBudgetRepo(session).dropped_back_types(
                        device_site_id
                    )
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
        # E0.2: persist the AUTHORITATIVE identity. Before this the id was
        # logged and discarded, so CC's site id and the SM's own primary
        # key were different id spaces that never matched -- and every
        # site-scoped read silently widened to the whole Site Manager.
        if not request.site_id:
            return harkeniq_pb2.SiteRegistrationAck(
                accepted=False,
                reason=(
                    "site_id is required: a site that cannot be identified "
                    "cannot be scoped"
                ),
            )
        if not request.site_name:
            return harkeniq_pb2.SiteRegistrationAck(
                accepted=False, reason="site_name is required",
            )

        async with self.sessionmaker() as session:
            repo = SiteRepo(session)
            audit = AuditRepo(session)
            bound = await repo.get_by_cc_id(request.site_id)
            if bound is not None:
                # Idempotent re-registration. A rename at CC is a label
                # change and is allowed; the binding itself is untouched.
                if bound.name != request.site_name:
                    if await repo.get_by_name(request.site_name) is not None:
                        return harkeniq_pb2.SiteRegistrationAck(
                            accepted=False,
                            reason=(
                                f"cannot rename site to {request.site_name!r}: "
                                f"another site here already has that name"
                            ),
                        )
                    previous, bound.name = bound.name, request.site_name
                    await audit.append(
                        "central-command", "site.renamed", bound.id,
                        detail={"from": previous, "to": request.site_name,
                                "cc_site_id": request.site_id},
                    )
                    await session.commit()
                logger.info(
                    "RegisterSite: already bound cc_site=%s -> site=%s",
                    request.site_id, bound.id,
                )
                return harkeniq_pb2.SiteRegistrationAck(
                    accepted=True, site_token=self.config.site_token,
                )

            existing = await repo.get_by_name(request.site_name)
            if existing is not None and existing.cc_site_id:
                # FAIL CLOSED. Re-pointing a bound site would move every
                # device, incident and outcome under it to a different
                # tenant-plane identity without anyone deciding to. The
                # audited unbind on the break-glass API is the recovery.
                logger.warning(
                    "RegisterSite refused: site %r is bound to cc_site=%s, "
                    "registration offered cc_site=%s",
                    request.site_name, existing.cc_site_id, request.site_id,
                )
                await audit.append(
                    "central-command", "site.bind_refused", existing.id,
                    detail={"site_name": request.site_name,
                            "bound_to": existing.cc_site_id,
                            "offered": request.site_id},
                )
                await session.commit()
                return harkeniq_pb2.SiteRegistrationAck(
                    accepted=False,
                    reason=(
                        f"site {request.site_name!r} is already bound to a "
                        f"different Central Command site identity; unbind it "
                        f"explicitly before re-registering"
                    ),
                )

            site = existing or await repo.get_or_create(request.site_name)
            await repo.bind(site, request.site_id)
            await audit.append(
                "central-command", "site.bound", site.id,
                detail={"site_name": site.name, "cc_site_id": request.site_id,
                        "tenant_id": request.tenant_id,
                        "cc_endpoint": request.cc_endpoint},
            )
            await session.commit()
            logger.info(
                "RegisterSite: bound cc_site=%s -> site=%s (%s)",
                request.site_id, site.id, site.name,
            )

        return harkeniq_pb2.SiteRegistrationAck(
            accepted=True,
            site_token=self.config.site_token,
        )

    async def GetFleetSnapshot(self, request, context):
        """One site's fleet, and never another's.

        E0.2: every read below is scoped to the resolved site. There is
        no fallback. Before this the device query missed (CC's site id
        and the SM's primary key are different id spaces) and fell back
        to every device, while incidents, pending actions, outcomes and
        candidate skills were never scoped at all -- and the outcome and
        candidate watermarks meant one site's poll CONSUMED another
        site's rows, which was data loss, not just leakage.
        """
        async with self.sessionmaker() as session:
            site = await SiteRepo(session).get_by_cc_id(request.site_id)
            if site is None or site.status != "active":
                # Explicit, safe, empty. Never broaden scope to answer.
                reason = (
                    f"no active site bound to Central Command site id "
                    f"{request.site_id!r} at this Site Manager"
                    if site is None else
                    f"site {site.name!r} is {site.status}"
                )
                logger.warning("GetFleetSnapshot unresolved: %s", reason)
                return harkeniq_pb2.FleetSnapshot(
                    snapshot_at_unix=int(time.time()),
                    site_resolved=False,
                    site_reason=reason,
                )

            devices = await DeviceRepo(session).list_for_site(site.id)

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
                        capabilities_json=(
                            json.dumps(device.capabilities)
                            if device.capabilities else ""
                        ),
                    )
                )

            # Open incidents -- this site's only.
            open_incidents = await IncidentRepo(session).list_open(site_id=site.id)
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

            # Pending actions -- scoped through the device that owns them.
            pending_actions = await ActionRepo(session).list_by_status(
                "pending", site_id=site.id,
            )
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
                # E0.2: scoped through the device. The watermark below
                # MARKS these rows reported, so an unscoped query did not
                # merely show another site's outcomes -- it consumed
                # them, and that site never received its own evidence.
                unreported = (
                    await session.execute(
                        select(ActionOutcomeRow)
                        .join(Device, Device.id == ActionOutcomeRow.device_id)
                        .where(
                            ActionOutcomeRow.reported_to_cc == False,  # noqa: E712
                            Device.site_id == site.id,
                        )
                        .limit(100)
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
                # E0.2: `source_device` is the agent id that produced the
                # candidate, so the site comes from that device. Same
                # consuming-watermark problem as outcomes above.
                unreported_cands = (
                    await session.execute(
                        select(CandidateSkillRow)
                        .join(
                            Device,
                            Device.agent_id == CandidateSkillRow.source_device,
                        )
                        .where(
                            CandidateSkillRow.reported_to_cc == False,  # noqa: E712
                            Device.site_id == site.id,
                        )
                        .limit(20)
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
            safety = await self._safety_state(session, site.id)

        return harkeniq_pb2.FleetSnapshot(
            devices=fleet_devices,
            incidents=fleet_incidents,
            pending_actions=fleet_actions,
            snapshot_at_unix=int(time.time()),
            outcomes=fleet_outcomes,
            candidate_skills=candidate_skills,
            safety=safety,
            site_resolved=True,
            site_reason="",
        )

    async def _safety_state(
        self, session, site_id: str
    ) -> "harkeniq_pb2.FleetSafetyState":
        """Compose FleetSafetyState for ONE site.

        E0.2: error budgets are per site, so a class withdrawn by one
        site's failures is not reported as withdrawn at another. The
        stop switch and suppression remain Site Manager wide, because
        those are properties of the execution boundary itself.
        """
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
            for row in await ErrorBudgetRepo(session).list_all(site_id=site_id):
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
        """Metered node count for ONE site.

        E0.2: this counted every device the Site Manager knows and
        returned the total labelled with the requested site id, so a Site
        Manager serving several sites would have billed each of them the
        full fleet. Metering feeds the ledger and invoices, which makes
        an unscoped count a commercial error, not only a display one.

        An unresolved site returns zero, never a total.
        """
        async with self.sessionmaker() as session:
            site = await SiteRepo(session).get_by_cc_id(request.site_id)
            if site is None:
                logger.warning(
                    "GetUsageSnapshot for unbound cc_site=%s: reporting zero",
                    request.site_id,
                )
                return harkeniq_pb2.UsageSnapshot(
                    tenant_id=request.tenant_id,
                    site_id=request.site_id,
                    date=request.date,
                    node_count=0,
                )

            node_count = (
                await session.execute(
                    select(func.count(Device.id)).where(Device.site_id == site.id)
                )
            ).scalar() or 0

            # Agent versions from AgentStatus.last_state (placeholder: version
            # info isn't yet tracked per-agent; return state distribution
            # instead), scoped to this site's devices.
            site_device_ids = {
                d.id for d in await DeviceRepo(session).list_for_site(site.id)
            }
            agent_versions: dict[str, int] = {}
            statuses = await StatusRepo(session).list_all()
            for s in statuses:
                if s.device_id not in site_device_ids:
                    continue
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
                # E1.3: a stop pushed from Central Command is the TENANT
                # stop. It reaches every site this Site Manager serves,
                # and it is persisted so a restart cannot silently resume
                # what an operator halted.
                if self.stopswitch is not None:
                    async with self.sessionmaker() as session:
                        await self.stopswitch.set_halt(
                            session, scope=SCOPE_TENANT, site_id=None,
                            active=desired, actor=by,
                            reason="pushed by Central Command",
                        )
                        await session.commit()
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
