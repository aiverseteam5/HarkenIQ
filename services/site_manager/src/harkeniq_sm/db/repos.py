"""Thin data-access helpers. Each repo wraps one AsyncSession.

Commit responsibility stays with the caller (one commit per ingest
event / API request) so multi-table updates remain atomic.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_sm.db.models import (
    ActionRow,
    AgentStatus,
    AuditLogRow,
    Device,
    DeviceSubsystemState,
    DirectedDirective,
    DomainMembership,
    ErrorBudgetRow,
    FaultDomain,
    FirmwareCampaign,
    FirmwareCampaignTarget,
    HeartbeatRow,
    Incident,
    Rack,
    Site,
    SMFleetPatternRow,
    VerdictReportRow,
    utcnow,
)


class SiteRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_cc_id(self, cc_site_id: str) -> Optional[Site]:
        """The site Central Command means. The authoritative resolution."""
        if not cc_site_id:
            return None
        return (
            await self.session.execute(
                select(Site).where(Site.cc_site_id == cc_site_id)
            )
        ).scalar_one_or_none()

    async def get_by_name(self, name: str) -> Optional[Site]:
        return (
            await self.session.execute(select(Site).where(Site.name == name))
        ).scalar_one_or_none()

    async def bind(self, site: Site, cc_site_id: str) -> None:
        """Attach Central Command's identity to this site.

        Only ever called on an UNBOUND site. Rebinding a bound site is
        refused at the RPC, because a registration that silently
        re-pointed a site would move every device, incident and outcome
        under it to a different tenant-plane identity without anyone
        deciding to.
        """
        site.cc_site_id = cc_site_id
        site.bound_at = utcnow()

    async def unbind(self, site: Site) -> None:
        """Break-glass: clear the binding so the next RegisterSite re-binds.

        The audited recovery path for a Central Command that legitimately
        changed its site ids (a restore from backup). Deliberately a
        separate, explicit operation rather than a special case inside
        registration.
        """
        site.cc_site_id = None
        site.bound_at = None

    async def list_all(self) -> Sequence[Site]:
        return (
            await self.session.execute(select(Site).order_by(Site.name))
        ).scalars().all()

    async def get_or_create(self, name: str) -> Site:
        site = (
            await self.session.execute(select(Site).where(Site.name == name))
        ).scalar_one_or_none()
        if site is None:
            try:
                # Savepoint: a lost insert race must not roll back the
                # caller's other uncommitted work.
                async with self.session.begin_nested():
                    site = Site(name=name)
                    self.session.add(site)
            except IntegrityError:
                # Concurrent session won the race (sites.name is unique);
                # adopt the winner.
                site = (
                    await self.session.execute(
                        select(Site).where(Site.name == name)
                    )
                ).scalar_one()
        return site


class DeviceRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_agent_id(self, agent_id: str) -> Optional[Device]:
        return (
            await self.session.execute(
                select(Device).where(Device.agent_id == agent_id)
            )
        ).scalar_one_or_none()

    async def get(self, device_id: str) -> Optional[Device]:
        return await self.session.get(Device, device_id)

    async def list_for_site(self, site_id: str) -> Sequence[Device]:
        return (
            await self.session.execute(
                select(Device).where(Device.site_id == site_id).order_by(Device.agent_name)
            )
        ).scalars().all()

    async def upsert_registration(
        self,
        site_id: str,
        agent_id: str,
        agent_name: str = "",
        vendor: str = "",
        model: str = "",
        service_tag: str = "",
        bmc_location: Optional[dict] = None,
        peers: Optional[list[str]] = None,
        rack_suggestion: Optional[str] = None,
        firmware: Optional[list[dict]] = None,
        device_class: str = "",
        capabilities: Optional[dict] = None,
    ) -> Device:
        device = await self.get_by_agent_id(agent_id)
        if device is None:
            device = Device(site_id=site_id, agent_id=agent_id)
            self.session.add(device)
        elif site_id and device.site_id and device.site_id != site_id:
            # E1.3: a device does not move site by re-registering with a
            # different credential.
            #
            # The upsert already happened not to rewrite `site_id`, so
            # the outcome was right by accident. That is not enforcement:
            # a silent no-op tells the operator nothing, and the ratified
            # rule is that changing site is an EXPLICIT re-enrollment.
            # So it is refused, loudly, with a reason.
            raise ValueError(
                f"device {agent_id!r} is already enrolled at another site. "
                "Moving a device between sites is an explicit re-enrollment "
                "(retire it at the old site, then enroll it at the new one), "
                "not a re-registration with a different credential."
            )
        device.agent_name = agent_name or device.agent_name
        device.vendor = vendor or device.vendor
        device.model = model or device.model
        device.service_tag = service_tag or device.service_tag
        # R6: absent field (pre-R6 agent) leaves the default "server".
        if device_class:
            device.device_class = device_class
        if bmc_location:
            device.bmc_location = bmc_location
        if peers is not None:
            device.peers = list(peers)
        if rack_suggestion:
            device.rack_suggestion = rack_suggestion
        if firmware is not None:
            device.firmware = firmware
        # A re-registration that declares nothing must not erase what the
        # device declared before: an agent rolled back to a build without
        # the declaration would otherwise silently become "unknown reach"
        # and take its bound action classes with it.
        if capabilities is not None:
            device.capabilities = capabilities
        device.last_seen_at = utcnow()
        await self.session.flush()
        return device

    async def touch(self, device: Device, when: Optional[datetime] = None) -> None:
        device.last_seen_at = when or utcnow()


class RackRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_for_site(self, site_id: str) -> Sequence[Rack]:
        return (
            await self.session.execute(select(Rack).where(Rack.site_id == site_id))
        ).scalars().all()

    async def get_or_create(self, site_id: str, name: str) -> Rack:
        rack = (
            await self.session.execute(
                select(Rack).where(Rack.site_id == site_id, Rack.name == name)
            )
        ).scalar_one_or_none()
        if rack is None:
            rack = Rack(site_id=site_id, name=name)
            self.session.add(rack)
            await self.session.flush()
        return rack


class StatusRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(
        self,
        device_id: str,
        heartbeat_at: datetime,
        state: str,
        health: Optional[dict],
        peer_status: Optional[dict],
    ) -> AgentStatus:
        status = await self.session.get(AgentStatus, device_id)
        if status is None:
            status = AgentStatus(device_id=device_id, last_heartbeat_at=heartbeat_at)
            self.session.add(status)
        status.last_heartbeat_at = heartbeat_at
        status.last_state = state
        status.last_health = health
        status.last_peer_status = peer_status
        await self.session.flush()
        return status

    async def get(self, device_id: str) -> Optional[AgentStatus]:
        return await self.session.get(AgentStatus, device_id)

    async def list_all(self) -> Sequence[AgentStatus]:
        return (await self.session.execute(select(AgentStatus))).scalars().all()


class TelemetryRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add_verdict(
        self,
        device_id: str,
        time: datetime,
        sensor_id: str,
        skill_name: str,
        severity: str,
        message: str,
        evidence: Optional[list],
    ) -> VerdictReportRow:
        row = VerdictReportRow(
            time=time,
            device_id=device_id,
            sensor_id=sensor_id,
            skill_name=skill_name,
            severity=severity,
            message=message,
            evidence=evidence,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def affected_components(
        self, device_id: str, subsystem: str, *, limit: int = 200,
    ) -> list[dict]:
        """The components a subsystem fault actually names (A22.4).

        A verdict's sensor id is ``"<subsystem>:<component>"``. Ingest has
        always split off the subsystem and thrown the remainder away, so
        an incident said "this device's disks are unhealthy" and nothing
        downstream could say WHICH disk. That is why an Operational Agent
        could never supply IDENTIFY_LED's ``target``.

        Returns the newest non-OK verdict per component, newest first. An
        empty list means nothing was reported -- the caller reports that
        as unknown and refuses to propose, rather than guessing a
        component.
        """
        prefix = f"{(subsystem or '').strip()}:"
        if prefix == ":":
            return []
        rows = (await self.session.execute(
            select(VerdictReportRow)
            .where(
                VerdictReportRow.device_id == device_id,
                VerdictReportRow.sensor_id.like(f"{prefix}%"),
                VerdictReportRow.severity.notin_(("OK", "HEALTHY")),
            )
            .order_by(VerdictReportRow.time.desc())
            .limit(limit)
        )).scalars().all()
        seen: dict[str, dict] = {}
        for row in rows:
            component = row.sensor_id.split(":", 1)[1]
            if not component or component in seen:
                continue
            seen[component] = {
                "component": component,
                "severity": row.severity,
                "skill_name": row.skill_name,
                "at": row.time.isoformat() if row.time else "",
            }
        return list(seen.values())

    async def add_heartbeat(
        self,
        device_id: str,
        time: datetime,
        state: str,
        health_summary: Optional[dict],
        peer_status: Optional[dict],
    ) -> HeartbeatRow:
        row = HeartbeatRow(
            time=time,
            device_id=device_id,
            state=state,
            health_summary=health_summary,
            peer_status=peer_status,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def recent_verdicts(
        self, device_id: str, limit: int = 50
    ) -> Sequence[VerdictReportRow]:
        return (
            await self.session.execute(
                select(VerdictReportRow)
                .where(VerdictReportRow.device_id == device_id)
                .order_by(VerdictReportRow.time.desc())
                .limit(limit)
            )
        ).scalars().all()


class SubsystemStateRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, device_id: str, subsystem: str) -> Optional[DeviceSubsystemState]:
        return await self.session.get(DeviceSubsystemState, (device_id, subsystem))

    async def set(
        self,
        device_id: str,
        subsystem: str,
        severity: str,
        onset_at: Optional[datetime],
    ) -> DeviceSubsystemState:
        state = await self.get(device_id, subsystem)
        if state is None:
            state = DeviceSubsystemState(
                device_id=device_id, subsystem=subsystem, severity=severity
            )
            self.session.add(state)
        state.severity = severity
        state.onset_at = onset_at
        state.updated_at = utcnow()
        await self.session.flush()
        return state

    async def non_ok(self, subsystem: Optional[str] = None) -> Sequence[DeviceSubsystemState]:
        stmt = select(DeviceSubsystemState).where(DeviceSubsystemState.severity != "OK")
        if subsystem is not None:
            stmt = stmt.where(DeviceSubsystemState.subsystem == subsystem)
        return (await self.session.execute(stmt)).scalars().all()


class DomainRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, domain_id: str) -> Optional[FaultDomain]:
        return await self.session.get(FaultDomain, domain_id)

    async def get_by_name(self, site_id: str, name: str) -> Optional[FaultDomain]:
        return (
            await self.session.execute(
                select(FaultDomain).where(
                    FaultDomain.site_id == site_id, FaultDomain.name == name
                )
            )
        ).scalar_one_or_none()

    async def list_for_site(self, site_id: str) -> Sequence[FaultDomain]:
        return (
            await self.session.execute(
                select(FaultDomain).where(FaultDomain.site_id == site_id)
            )
        ).scalars().all()

    async def create(
        self,
        site_id: str,
        name: str,
        kind: str,
        status: str = "inferred",
        confidence: float = 0.6,
        source: str = "inference",
    ) -> FaultDomain:
        domain = FaultDomain(
            site_id=site_id, name=name, kind=kind,
            status=status, confidence=confidence, source=source,
        )
        self.session.add(domain)
        await self.session.flush()
        return domain

    async def members(self, domain_id: str) -> list[str]:
        rows = (
            await self.session.execute(
                select(DomainMembership.device_id).where(
                    DomainMembership.domain_id == domain_id
                )
            )
        ).scalars().all()
        return list(rows)

    async def set_members(
        self, domain_id: str, device_ids: list[str], added_by: str = ""
    ) -> None:
        existing = {
            m.device_id: m
            for m in (
                await self.session.execute(
                    select(DomainMembership).where(
                        DomainMembership.domain_id == domain_id
                    )
                )
            ).scalars().all()
        }
        wanted = set(device_ids)
        for device_id in wanted - existing.keys():
            self.session.add(
                DomainMembership(
                    domain_id=domain_id, device_id=device_id, added_by=added_by
                )
            )
        for device_id in existing.keys() - wanted:
            await self.session.delete(existing[device_id])
        await self.session.flush()

    async def domains_for_device(
        self, device_id: str, kind: Optional[str] = None
    ) -> Sequence[FaultDomain]:
        stmt = (
            select(FaultDomain)
            .join(DomainMembership, DomainMembership.domain_id == FaultDomain.id)
            .where(DomainMembership.device_id == device_id)
        )
        if kind is not None:
            stmt = stmt.where(FaultDomain.kind == kind)
        return (await self.session.execute(stmt)).scalars().all()

    async def confirm(self, domain: FaultDomain, by: str) -> FaultDomain:
        domain.status = "confirmed"
        domain.confidence = 1.0
        domain.confirmed_by = by
        domain.confirmed_at = utcnow()
        await self.session.flush()
        return domain


class IncidentRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, incident_id: str) -> Optional[Incident]:
        return await self.session.get(Incident, incident_id)

    async def open_device_incident(
        self, device_id: str, subsystem: str
    ) -> Optional[Incident]:
        return (
            await self.session.execute(
                select(Incident).where(
                    Incident.kind == "device",
                    Incident.status == "open",
                    Incident.device_id == device_id,
                    Incident.subsystem == subsystem,
                )
            )
        ).scalar_one_or_none()

    async def open_for_device(self, kind: str, device_id: str) -> Optional[Incident]:
        return (
            await self.session.execute(
                select(Incident).where(
                    Incident.kind == kind,
                    Incident.status == "open",
                    Incident.device_id == device_id,
                )
            )
        ).scalars().first()

    async def open_parent(
        self, kind: str, domain_id: Optional[str] = None, subsystem: Optional[str] = None
    ) -> Optional[Incident]:
        stmt = select(Incident).where(
            Incident.kind == kind, Incident.status == "open"
        )
        if domain_id is not None:
            stmt = stmt.where(Incident.domain_id == domain_id)
        if subsystem is not None:
            stmt = stmt.where(Incident.subsystem == subsystem)
        return (await self.session.execute(stmt)).scalars().first()

    async def create(self, **kwargs) -> Incident:
        incident = Incident(**kwargs)
        self.session.add(incident)
        await self.session.flush()
        return incident

    async def children(self, parent_id: str) -> Sequence[Incident]:
        return (
            await self.session.execute(
                select(Incident).where(Incident.parent_id == parent_id)
            )
        ).scalars().all()

    async def list_open(self, site_id: Optional[str] = None) -> Sequence[Incident]:
        """Open incidents, optionally for ONE site.

        E0.2: the fleet snapshot must pass a site. Sweep callers that
        resolve each incident on its own device's state legitimately pass
        None, because they decide per device and never compare across
        sites.
        """
        stmt = select(Incident).where(Incident.status == "open")
        if site_id is not None:
            stmt = stmt.where(Incident.site_id == site_id)
        return (
            await self.session.execute(stmt.order_by(Incident.opened_at))
        ).scalars().all()

    async def list_all(self) -> Sequence[Incident]:
        return (
            await self.session.execute(select(Incident).order_by(Incident.opened_at))
        ).scalars().all()

    async def resolve(self, incident: Incident) -> None:
        incident.status = "resolved"
        incident.resolved_at = utcnow()
        await self.session.flush()


class ActionRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, action_id: str) -> Optional[ActionRow]:
        return await self.session.get(ActionRow, action_id)

    async def get_by_agent_action(
        self, device_id: str, agent_action_id: str
    ) -> Optional[ActionRow]:
        return (
            await self.session.execute(
                select(ActionRow).where(
                    ActionRow.device_id == device_id,
                    ActionRow.agent_action_id == agent_action_id,
                )
            )
        ).scalar_one_or_none()

    async def list_by_status(
        self, status: str, site_id: Optional[str] = None
    ) -> Sequence[ActionRow]:
        """Actions in one state, optionally for ONE site.

        E0.2: an action has no site column of its own, so the scope comes
        through its device. The fleet snapshot must pass a site.
        """
        stmt = select(ActionRow).where(ActionRow.status == status)
        if site_id is not None:
            stmt = stmt.join(Device, Device.id == ActionRow.device_id).where(
                Device.site_id == site_id
            )
        return (
            await self.session.execute(stmt.order_by(ActionRow.reported_at))
        ).scalars().all()

    async def list_all(self) -> Sequence[ActionRow]:
        return (
            await self.session.execute(
                select(ActionRow).order_by(ActionRow.reported_at)
            )
        ).scalars().all()

    async def undelivered_decisions(self, device_id: str) -> Sequence[ActionRow]:
        return (
            await self.session.execute(
                select(ActionRow).where(
                    ActionRow.device_id == device_id,
                    ActionRow.status.in_(("approved", "denied")),
                    ActionRow.delivered_at.is_(None),
                )
            )
        ).scalars().all()


#: Serializes hash-chain appends within this process (R4-2 P12). The
#: UNIQUE constraint on audit_log.seq is the cross-process backstop: a
#: racing appender fails loudly instead of forking the chain.
_audit_chain_lock = asyncio.Lock()


class SMFleetPatternRepo:
    """QA-033: CC-pushed fleet patterns (idempotent upsert by id)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(self, pattern: dict) -> SMFleetPatternRow:
        pattern_id = str(pattern.get("pattern_id", ""))
        row = await self.session.get(SMFleetPatternRow, pattern_id)
        if row is None:
            row = SMFleetPatternRow(pattern_id=pattern_id)
            self.session.add(row)
        row.pattern_type = str(pattern.get("pattern_type", ""))
        row.description = str(pattern.get("description", ""))
        row.affected_scope = pattern.get("affected_scope")
        row.confidence = float(pattern.get("confidence", 0.0) or 0.0)
        row.evidence = pattern.get("evidence")
        row.detected_at = str(pattern.get("detected_at", ""))
        await self.session.flush()
        return row

    async def list_all(self) -> Sequence[SMFleetPatternRow]:
        return (
            await self.session.execute(
                select(SMFleetPatternRow).order_by(
                    SMFleetPatternRow.received_at.desc()
                )
            )
        ).scalars().all()


class ErrorBudgetRepo:
    """Persisted A2.2 error budgets (S5).

    R3b-1 declared `sm_error_budgets` and R3a wrote the drop-back model,
    but nothing at runtime ever instantiated a KnowledgeBase — so the
    table had no writer, `is_action_type_dropped_back` had no caller, and
    automatic demotion, a RATIFIED safety property, did not happen on a
    running system. S5 wires it to the one place the Site Manager learns
    an action's terminal result (`ApprovalService._record_outcome`).

    The decision itself is not re-implemented here: `ErrorBudgetState`
    from `harkeniq_sm.knowledge` folds the outcome, so the persisted row
    and the in-memory knowledge base cannot judge the same evidence
    differently. This path only ever WITHDRAWS autonomy; nothing here can
    grant it.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(
        self, site_id: str, action_type: str, outcome: str
    ) -> tuple[ErrorBudgetRow, bool]:
        """Fold one outcome into this SITE's budget for this action type.

        E0.2: per site. One site's failures must not withdraw another
        site's autonomy, even when the same Site Manager executes for
        both.

        Returns (row, newly_dropped_back).
        """
        from harkeniq_sm.knowledge import ErrorBudgetState

        row = await self.session.get(ErrorBudgetRow, (site_id, action_type))
        if row is None:
            row = ErrorBudgetRow(site_id=site_id, action_type=action_type)
            self.session.add(row)
            await self.session.flush()
        state = ErrorBudgetState(
            action_type=action_type,
            success_count=row.success_count or 0,
            failure_count=row.failure_count or 0,
            total_count=row.total_count or 0,
            min_success_rate=row.min_success_rate or 0.95,
            dropped_back=bool(row.dropped_back),
        )
        newly = state.record((outcome or "").upper())
        row.success_count = state.success_count
        row.failure_count = state.failure_count
        row.total_count = state.total_count
        row.dropped_back = state.dropped_back
        if newly:
            row.dropped_back_at = utcnow()
        row.updated_at = utcnow()
        await self.session.flush()
        return row, newly

    async def list_all(self, site_id: Optional[str] = None) -> Sequence[ErrorBudgetRow]:
        """Budgets, for one site when given. Callers that report a site's
        safety state MUST pass one."""
        stmt = select(ErrorBudgetRow)
        if site_id is not None:
            stmt = stmt.where(ErrorBudgetRow.site_id == site_id)
        return (
            await self.session.execute(stmt.order_by(ErrorBudgetRow.action_type))
        ).scalars().all()

    async def dropped_back_types(self, site_id: str) -> set[str]:
        """Action types whose autonomy this SITE's error budget withdrew.

        `site_id` is required, not optional: every caller gates real
        execution, and a default that spanned sites would withdraw
        autonomy somewhere nobody decided about.
        """
        rows = (
            await self.session.execute(
                select(ErrorBudgetRow).where(
                    ErrorBudgetRow.site_id == site_id,
                    ErrorBudgetRow.dropped_back == True,  # noqa: E712
                )
            )
        ).scalars().all()
        return {r.action_type for r in rows}

    async def recover(self, site_id: str, action_type: str) -> bool:
        """Operator lifts a drop-back at ONE site: counters reset.

        E0.2: recovery is per site for the same reason withdrawal is.
        Clearing a drop-back at every site the Site Manager serves would
        restore autonomy where nobody looked at the failures.
        """
        row = await self.session.get(ErrorBudgetRow, (site_id, action_type))
        if row is None or not row.dropped_back:
            return False
        row.dropped_back = False
        row.dropped_back_at = None
        row.success_count = 0
        row.failure_count = 0
        row.total_count = 0
        row.updated_at = utcnow()
        await self.session.flush()
        return True


class AuditRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _chain_ts(ts) -> str:
        """Timezone-stable timestamp string for hashing.

        sqlite returns naive datetimes for values written tz-aware, so
        normalize to naive UTC before formatting -- the payload string
        must be identical at write time and at verify time.
        """
        from datetime import timezone as _tz
        if ts.tzinfo is not None:
            ts = ts.astimezone(_tz.utc).replace(tzinfo=None)
        return ts.isoformat()

    @staticmethod
    def _chain_payload(row: AuditLogRow) -> dict:
        return {
            "ts": AuditRepo._chain_ts(row.ts),
            "actor": row.actor,
            "action": row.action,
            "subject": row.subject,
            "detail": row.detail,
        }

    async def append(
        self, actor: str, action: str, subject: str = "",
        detail: Optional[dict] = None, site_id: Optional[str] = None,
    ) -> AuditLogRow:
        """Append one entry to this Site Manager's hash chain.

        E1.3: `site_id` is authorization/indexing metadata and is
        DELIBERATELY absent from `_chain_payload`, exactly as CC's E1.2
        column is. The chain hashes ts, actor, action, subject and detail
        and nothing else, so recording a site neither changes an entry's
        hash nor invalidates a chain written before this column existed.
        A test asserts that rather than trusting it.
        """
        from harkeniq.audit.chain import next_link, pg_advisory_chain_lock

        row = AuditLogRow(
            ts=utcnow(), actor=actor, action=action, subject=subject,
            detail=detail, site_id=site_id,
        )
        async with _audit_chain_lock:
            # R5-2: cross-replica serialization on PostgreSQL (held
            # through the caller's commit); no-op on sqlite.
            await pg_advisory_chain_lock(self.session, "sm.audit_log")
            tail = (
                await self.session.execute(
                    select(AuditLogRow.seq, AuditLogRow.entry_hash)
                    .where(AuditLogRow.seq.isnot(None))
                    .order_by(AuditLogRow.seq.desc())
                    .limit(1)
                )
            ).first()
            row.seq, row.prev_hash, row.entry_hash = next_link(
                tail[0] if tail else 0,
                tail[1] if tail else None,
                self._chain_payload(row),
            )
            self.session.add(row)
            await self.session.flush()
        return row

    async def list_all(self) -> Sequence[AuditLogRow]:
        return (
            await self.session.execute(select(AuditLogRow).order_by(AuditLogRow.ts))
        ).scalars().all()

    async def verify_chain(self):
        """Verify the audit hash chain (R4-2 P12); returns ChainVerification."""
        from harkeniq.audit.chain import verify_chain

        rows = (
            await self.session.execute(
                select(AuditLogRow)
                .where(AuditLogRow.seq.isnot(None))
                .order_by(AuditLogRow.seq)
            )
        ).scalars().all()
        return verify_chain(
            (r.seq, r.prev_hash, r.entry_hash, self._chain_payload(r))
            for r in rows
        )


class FirmwareCampaignRepo:
    """Firmware campaign persistence (R4-3 P19)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        site_id: str,
        component: str,
        target_version: str,
        vendor: str = "",
        image_uri: str = "",
        image_sha256: str = "",
        created_by: str = "",
        max_wave_size: int = 5,
    ) -> FirmwareCampaign:
        campaign = FirmwareCampaign(
            site_id=site_id, component=component, vendor=vendor,
            target_version=target_version, image_uri=image_uri,
            image_sha256=image_sha256, created_by=created_by,
            max_wave_size=max_wave_size,
        )
        self.session.add(campaign)
        await self.session.flush()
        return campaign

    async def get(self, campaign_id: str) -> Optional[FirmwareCampaign]:
        return await self.session.get(FirmwareCampaign, campaign_id)

    async def list_for_site(self, site_id: str) -> Sequence[FirmwareCampaign]:
        return (
            await self.session.execute(
                select(FirmwareCampaign)
                .where(FirmwareCampaign.site_id == site_id)
                .order_by(FirmwareCampaign.created_at.desc())
            )
        ).scalars().all()

    async def add_target(
        self, campaign_id: str, device_id: str, wave_index: int,
        pre_version: str = "",
    ) -> FirmwareCampaignTarget:
        target = FirmwareCampaignTarget(
            campaign_id=campaign_id, device_id=device_id,
            wave_index=wave_index, pre_version=pre_version,
        )
        self.session.add(target)
        await self.session.flush()
        return target

    async def targets(
        self, campaign_id: str, wave_index: Optional[int] = None
    ) -> Sequence[FirmwareCampaignTarget]:
        stmt = select(FirmwareCampaignTarget).where(
            FirmwareCampaignTarget.campaign_id == campaign_id
        )
        if wave_index is not None:
            stmt = stmt.where(FirmwareCampaignTarget.wave_index == wave_index)
        stmt = stmt.order_by(
            FirmwareCampaignTarget.wave_index, FirmwareCampaignTarget.device_id
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def update_target(
        self, target: FirmwareCampaignTarget, status: str,
        post_version: str = "", error: str = "",
    ) -> FirmwareCampaignTarget:
        target.status = status
        target.post_version = post_version
        target.error = error[:512]
        target.updated_at = utcnow()
        await self.session.flush()
        return target


class DirectiveRepo:
    """Directed-directive persistence (R5 SM->agent transport)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def enqueue(
        self,
        device_id: str,
        kind: str,
        action_type: str = "",
        params: Optional[dict] = None,
        skill_id: str = "",
        skill_version: str = "",
        yaml_content: Optional[str] = None,
        tier: str = "",
        validation_state: str = "",
        issued_by: str = "",
        actor: str = "",
        authorization_basis: str = "",
        proposal_id: str = "",
    ) -> DirectedDirective:
        directive = DirectedDirective(
            device_id=device_id, kind=kind, action_type=action_type,
            params=params, skill_id=skill_id, skill_version=skill_version,
            yaml_content=yaml_content, tier=tier,
            validation_state=validation_state, issued_by=issued_by,
            actor=actor, authorization_basis=authorization_basis,
            proposal_id=proposal_id,
        )
        self.session.add(directive)
        await self.session.flush()
        return directive

    async def get(self, directive_id: str) -> Optional[DirectedDirective]:
        return await self.session.get(DirectedDirective, directive_id)

    async def pending_for_device(
        self, device_id: str
    ) -> Sequence[DirectedDirective]:
        return (
            await self.session.execute(
                select(DirectedDirective)
                .where(
                    DirectedDirective.device_id == device_id,
                    DirectedDirective.status == "pending",
                )
                .order_by(DirectedDirective.created_at)
            )
        ).scalars().all()

    async def mark_delivered(self, directive: DirectedDirective) -> None:
        directive.status = "delivered"
        directive.delivered_at = utcnow()
        await self.session.flush()

    async def complete(
        self, directive_id: str, success: bool, detail: str = ""
    ) -> Optional[DirectedDirective]:
        directive = await self.get(directive_id)
        if directive is None:
            return None
        directive.status = "completed" if success else "failed"
        directive.result_detail = detail[:512]
        directive.completed_at = utcnow()
        await self.session.flush()
        return directive
