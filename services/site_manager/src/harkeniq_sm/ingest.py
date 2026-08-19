"""Telemetry ingest: registration, heartbeats, verdicts (R-S2, R-S3).

All timestamps recorded here are Site Manager receive time — agent
clocks are untrusted (spec §7). Subsystem onset state is the substrate
the correlation engine reads: onset_at is set when a subsystem
transitions out of OK and preserved until it returns to OK.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.repos import (
    DeviceRepo,
    SiteRepo,
    StatusRepo,
    SubsystemStateRepo,
    TelemetryRepo,
)
from harkeniq_sm.sitemodel.discovery import rack_hint

logger = logging.getLogger("harkeniq.sm.ingest")

# health_summary values use "OK"; verdict severities use "HEALTHY".
_OK_VALUES = {"OK", "HEALTHY", ""}

OnsetHook = Callable[[str, str, str, datetime], Awaitable[None]]


def _normalize(severity: str) -> str:
    return "OK" if severity in _OK_VALUES else severity


class IngestService:
    """Persists agent-reported telemetry; one commit per event."""

    def __init__(self, sessionmaker, config: SMConfig) -> None:
        self.sessionmaker = sessionmaker
        self.config = config
        self._site_id: Optional[str] = None
        # Set by the correlation engine (phase: correlation); called after
        # commit for every onset transition (device_id, subsystem,
        # severity, onset_at).
        self.on_onset: Optional[OnsetHook] = None

    async def _site(self, session) -> str:
        if self._site_id is None:
            site = await SiteRepo(session).get_or_create(self.config.site_name)
            self._site_id = site.id
        return self._site_id

    async def register(
        self,
        agent_id: str,
        agent_name: str = "",
        vendor: str = "",
        model: str = "",
        service_tag: str = "",
        bmc_location_json: str = "",
        peers: Optional[list[str]] = None,
    ) -> str:
        """Upsert the device row; returns the site name (RegistrationAck)."""
        bmc_location = None
        if bmc_location_json:
            try:
                bmc_location = json.loads(bmc_location_json)
            except ValueError:
                logger.warning("Unparseable bmc_location_json from %s", agent_id)
        async with self.sessionmaker() as session:
            site_id = await self._site(session)
            await DeviceRepo(session).upsert_registration(
                site_id=site_id,
                agent_id=agent_id,
                agent_name=agent_name,
                vendor=vendor,
                model=model,
                service_tag=service_tag,
                bmc_location=bmc_location,
                peers=peers,
                rack_suggestion=rack_hint(agent_name),
            )
            await session.commit()
        return self.config.site_name

    async def heartbeat(
        self,
        agent_id: str,
        agent_name: str,
        state: str,
        health_summary: dict[str, str],
        peer_status: dict[str, str],
    ) -> bool:
        now = datetime.now(timezone.utc)
        onsets: list[tuple[str, str, str, datetime]] = []
        async with self.sessionmaker() as session:
            site_id = await self._site(session)
            device = await DeviceRepo(session).upsert_registration(
                site_id=site_id, agent_id=agent_id, agent_name=agent_name
            )
            await StatusRepo(session).upsert(
                device.id, now, state, dict(health_summary), dict(peer_status)
            )
            await TelemetryRepo(session).add_heartbeat(
                device.id, now, state, dict(health_summary), dict(peer_status)
            )
            for subsystem, severity in health_summary.items():
                onset = await self._apply_subsystem(
                    session, device.id, subsystem, severity, now
                )
                if onset:
                    onsets.append(onset)
            await session.commit()
        await self._fire(onsets)
        return True

    async def verdict(
        self,
        agent_id: str,
        sensor_id: str,
        skill_name: str,
        severity: str,
        evidence_json: str = "",
        message: str = "",
    ) -> bool:
        now = datetime.now(timezone.utc)
        evidence = None
        if evidence_json:
            try:
                evidence = json.loads(evidence_json)
            except ValueError:
                logger.warning("Unparseable evidence_json from %s", agent_id)
        subsystem = sensor_id.split(":", 1)[0]
        onsets: list[tuple[str, str, str, datetime]] = []
        async with self.sessionmaker() as session:
            site_id = await self._site(session)
            device = await DeviceRepo(session).upsert_registration(
                site_id=site_id, agent_id=agent_id
            )
            await TelemetryRepo(session).add_verdict(
                device.id, now, sensor_id, skill_name, severity, message, evidence
            )
            onset = await self._apply_subsystem(
                session, device.id, subsystem, severity, now
            )
            if onset:
                onsets.append(onset)
            await session.commit()
        await self._fire(onsets)
        return True

    async def _apply_subsystem(
        self, session, device_id: str, subsystem: str, severity: str, now: datetime
    ) -> Optional[tuple[str, str, str, datetime]]:
        """Update onset state; returns an onset event on OK→non-OK transition."""
        severity = _normalize(severity)
        repo = SubsystemStateRepo(session)
        current = await repo.get(device_id, subsystem)
        if severity == "OK":
            if current is not None and current.severity != "OK":
                await repo.set(device_id, subsystem, "OK", None)
            elif current is None:
                await repo.set(device_id, subsystem, "OK", None)
            return None
        if current is not None and current.severity != "OK" and current.onset_at:
            # Continuing fault: keep the original onset, refresh severity.
            await repo.set(device_id, subsystem, severity, current.onset_at)
            return None
        await repo.set(device_id, subsystem, severity, now)
        return (device_id, subsystem, severity, now)

    async def _fire(self, onsets) -> None:
        if self.on_onset is None:
            return
        for device_id, subsystem, severity, onset_at in onsets:
            try:
                await self.on_onset(device_id, subsystem, severity, onset_at)
            except Exception:  # pragma: no cover - correlation must not break ingest
                logger.exception("onset hook failed")
