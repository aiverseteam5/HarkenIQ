"""Incident consolidation (R-S5, OQ-11 R2a half).

Invariants: one open device-incident per (device, subsystem); repeats
attach to it; parent creation reparents children; late children attach;
a parent auto-resolves only after all children stay resolved for two
consecutive sweeps (hold-down against flapping).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from harkeniq_sm.config import SMConfig
from harkeniq_sm.coverage import observation_state
from harkeniq_sm.db.models import Incident
from harkeniq_sm.db.repos import IncidentRepo, StatusRepo, SubsystemStateRepo

PARENT_HOLDDOWN_CYCLES = 2


class IncidentService:
    def __init__(self, config: SMConfig) -> None:
        self.config = config
        self._holddown: dict[str, int] = {}

    async def ensure_device_incident(
        self,
        session,
        site_id: str,
        device,
        subsystem: str,
        severity: str,
        onset_at: datetime,
    ) -> Incident:
        """Create or update the single open child for (device, subsystem)."""
        repo = IncidentRepo(session)
        incident = await repo.open_device_incident(device.id, subsystem)
        if incident is None:
            return await repo.create(
                site_id=site_id,
                kind="device",
                device_id=device.id,
                subsystem=subsystem,
                title=f"{device.agent_name or device.agent_id}: {subsystem} {severity}",
                correlation_meta={"severity": severity, "onsets": 1},
            )
        meta = dict(incident.correlation_meta or {})
        meta["severity"] = severity
        meta["onsets"] = int(meta.get("onsets", 1)) + 1
        incident.correlation_meta = meta
        await session.flush()
        return incident

    async def resolve_recovered_children(self, session) -> int:
        """Close device incidents whose subsystem returned to OK."""
        repo = IncidentRepo(session)
        state_repo = SubsystemStateRepo(session)
        resolved = 0
        for incident in await repo.list_open():
            if incident.kind != "device" or not incident.device_id:
                continue
            state = await state_repo.get(incident.device_id, incident.subsystem or "")
            if state is not None and state.severity == "OK":
                await repo.resolve(incident)
                resolved += 1
        return resolved

    async def resolve_recovered_ambiguities(self, session) -> int:
        """Close network_ambiguity incidents once the device reports again."""
        repo = IncidentRepo(session)
        status_repo = StatusRepo(session)
        resolved = 0
        for incident in await repo.list_open():
            if incident.kind != "network_ambiguity" or not incident.device_id:
                continue
            status = await status_repo.get(incident.device_id)
            last = status.last_heartbeat_at if status else None
            if observation_state(last, self.config) == "observed":
                await repo.resolve(incident)
                resolved += 1
        return resolved

    async def auto_resolve_parents(self, session) -> int:
        """Two consecutive all-children-resolved sweeps close a parent."""
        repo = IncidentRepo(session)
        resolved = 0
        open_parents = [
            i for i in await repo.list_open()
            if i.kind in ("shared_power", "rack_thermal", "batch_component")
        ]
        for parent in open_parents:
            children = await repo.children(parent.id)
            if children and all(c.status == "resolved" for c in children):
                count = self._holddown.get(parent.id, 0) + 1
                if count >= PARENT_HOLDDOWN_CYCLES:
                    await repo.resolve(parent)
                    self._holddown.pop(parent.id, None)
                    resolved += 1
                else:
                    self._holddown[parent.id] = count
            else:
                self._holddown.pop(parent.id, None)
        return resolved

    def parent_attrs_for_domain(self, domain) -> dict:
        """Confidence/inferred labeling per A1.1."""
        if domain.status == "confirmed":
            return {"confidence": 1.0, "inferred": False}
        return {"confidence": domain.confidence, "inferred": True}
