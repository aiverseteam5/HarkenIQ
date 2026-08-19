"""The four §1 boundary-table rules. All times are SM receive time.

Each rule creates at most one open parent per scope, reparents the
matching device incidents under it, and attaches late children to an
already-open parent regardless of the original onset window.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select

from harkeniq_sm.config import SMConfig
from harkeniq_sm.coverage import observation_state
from harkeniq_sm.db.models import VerdictReportRow
from harkeniq_sm.db.repos import (
    DeviceRepo,
    DomainRepo,
    IncidentRepo,
    StatusRepo,
    SubsystemStateRepo,
)
from harkeniq_sm.incidents import IncidentService

_FAULT_SEVERITIES = ("WARNING", "CRITICAL")


async def _attach(session, incidents_svc, site_id, parent, device_id, subsystem):
    repo = IncidentRepo(session)
    child = await repo.open_device_incident(device_id, subsystem)
    if child is None:
        device = await DeviceRepo(session).get(device_id)
        state = await SubsystemStateRepo(session).get(device_id, subsystem)
        child = await incidents_svc.ensure_device_incident(
            session, site_id, device, subsystem,
            state.severity if state else "UNKNOWN",
            state.onset_at if state else None,
        )
    if child.parent_id != parent.id:
        child.parent_id = parent.id
    return child


async def _domain_rule(
    session,
    config: SMConfig,
    site_id: str,
    incidents_svc: IncidentService,
    *,
    domain_kind: str,
    subsystem: str,
    incident_kind: str,
    min_devices: int,
    window_s: float,
) -> list:
    """Shared power / rack thermal: quorum of onsets inside one domain."""
    created = []
    states = {
        s.device_id: s
        for s in await SubsystemStateRepo(session).non_ok(subsystem=subsystem)
    }
    if not states:
        return created
    incident_repo = IncidentRepo(session)
    domain_repo = DomainRepo(session)
    for domain in await domain_repo.list_for_site(site_id):
        if domain.kind != domain_kind:
            continue
        members = await domain_repo.members(domain.id)
        faulted = [(m, states[m]) for m in members if m in states]
        parent = await incident_repo.open_parent(incident_kind, domain_id=domain.id)
        if parent is None:
            onsets = [s.onset_at for _, s in faulted if s.onset_at is not None]
            if len(faulted) < min_devices or not onsets:
                continue
            if (max(onsets) - min(onsets)) > timedelta(seconds=window_s):
                continue
            attrs = incidents_svc.parent_attrs_for_domain(domain)
            parent = await incident_repo.create(
                site_id=site_id,
                kind=incident_kind,
                domain_id=domain.id,
                subsystem=subsystem,
                title=(
                    f"{incident_kind} in {domain.name} ({len(faulted)} devices)"
                ),
                correlation_meta={
                    "domain": domain.name,
                    "domain_status": domain.status,
                    "devices": [m for m, _ in faulted],
                },
                **attrs,
            )
            created.append(parent)
        for device_id, _state in faulted:
            await _attach(session, incidents_svc, site_id, parent, device_id, subsystem)
    return created


async def shared_power(session, config, site_id, incidents_svc) -> list:
    return await _domain_rule(
        session, config, site_id, incidents_svc,
        domain_kind="power", subsystem="psu", incident_kind="shared_power",
        min_devices=config.correlation.shared_power_min_devices,
        window_s=config.correlation.shared_power_window_s,
    )


async def rack_thermal(session, config, site_id, incidents_svc) -> list:
    return await _domain_rule(
        session, config, site_id, incidents_svc,
        domain_kind="cooling", subsystem="thermal", incident_kind="rack_thermal",
        min_devices=config.correlation.rack_thermal_min_devices,
        window_s=config.correlation.rack_thermal_window_s,
    )


def _models_from_evidence(evidence) -> set[str]:
    models = set()
    for entry in evidence or []:
        if not isinstance(entry, dict):
            continue
        fields = entry.get("fields") if isinstance(entry.get("fields"), dict) else entry
        model = fields.get("model") or fields.get("component_model")
        if model:
            models.add(str(model))
    return models


async def batch_component(
    session, config: SMConfig, site_id: str, incidents_svc: IncidentService,
    now: datetime,
) -> list:
    """≥N devices, same subsystem, same component model, 24 h window."""
    created = []
    lookback = now - timedelta(seconds=config.correlation.batch_component_window_s)
    rows = (
        await session.execute(
            select(VerdictReportRow).where(
                VerdictReportRow.severity.in_(_FAULT_SEVERITIES),
                VerdictReportRow.time >= lookback,
            )
        )
    ).scalars().all()
    groups: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        subsystem = row.sensor_id.split(":", 1)[0]
        for model in _models_from_evidence(row.evidence):
            groups.setdefault((subsystem, model), set()).add(row.device_id)

    incident_repo = IncidentRepo(session)
    open_batch = {
        (i.correlation_meta or {}).get("model"): i
        for i in await incident_repo.list_open()
        if i.kind == "batch_component"
    }
    for (subsystem, model), device_ids in groups.items():
        parent = open_batch.get(model)
        if parent is None:
            if len(device_ids) < config.correlation.batch_component_min_devices:
                continue
            parent = await incident_repo.create(
                site_id=site_id,
                kind="batch_component",
                subsystem=subsystem,
                title=(
                    f"batch_component {model} ({subsystem}, "
                    f"{len(device_ids)} devices)"
                ),
                correlation_meta={"model": model, "devices": sorted(device_ids)},
            )
            created.append(parent)
        for device_id in device_ids:
            state = await SubsystemStateRepo(session).get(device_id, subsystem)
            if state is not None and state.severity != "OK":
                await _attach(
                    session, incidents_svc, site_id, parent, device_id, subsystem
                )
    return created


def _peer_vote_map(devices) -> dict[str, str]:
    """peer_status key → device_id (agent_id first, then agent_name)."""
    by_key: dict[str, str] = {}
    for device in devices:
        if device.agent_id:
            by_key[device.agent_id] = device.id
        if device.agent_name:
            by_key.setdefault(device.agent_name, device.id)
    return by_key


async def network_ambiguity(
    session, config: SMConfig, site_id: str, incidents_svc: IncidentService,
    now: Optional[datetime] = None,
) -> list:
    """Silent-at-SM device: peers vote path-suspect vs device-down."""
    created = []
    devices = await DeviceRepo(session).list_for_site(site_id)
    statuses = {s.device_id: s for s in await StatusRepo(session).list_all()}
    by_key = _peer_vote_map(devices)
    incident_repo = IncidentRepo(session)

    for device in devices:
        status = statuses.get(device.id)
        if status is None:
            continue  # never reported; coverage map handles it
        observation = observation_state(status.last_heartbeat_at, config, now)
        if observation == "observed":
            continue
        votes: dict[str, str] = {}
        for voter in devices:
            if voter.id == device.id:
                continue
            voter_status = statuses.get(voter.id)
            if voter_status is None:
                continue
            fresh = observation_state(
                voter_status.last_heartbeat_at, config, now
            ) == "observed"
            if not fresh:
                continue
            for key, value in (voter_status.last_peer_status or {}).items():
                if by_key.get(key) == device.id:
                    votes[voter.agent_id] = value
        if not votes:
            continue
        if any(v == "ALIVE" for v in votes.values()):
            assessment, confidence = "path_suspect", 0.6
        elif all(v == "UNRESPONSIVE" for v in votes.values()):
            assessment, confidence = "device_down_peer_confirmed", 1.0
        else:
            assessment, confidence = "unknown", 0.5

        existing = await incident_repo.open_for_device("network_ambiguity", device.id)
        if existing is not None:
            meta = dict(existing.correlation_meta or {})
            meta.update({"votes": votes, "assessment": assessment})
            existing.correlation_meta = meta
            existing.confidence = confidence
            continue
        created.append(
            await incident_repo.create(
                site_id=site_id,
                kind="network_ambiguity",
                device_id=device.id,
                confidence=confidence,
                title=(
                    f"{device.agent_name or device.agent_id}: "
                    f"unreachable from SM ({assessment})"
                ),
                correlation_meta={"votes": votes, "assessment": assessment},
            )
        )
    return created
