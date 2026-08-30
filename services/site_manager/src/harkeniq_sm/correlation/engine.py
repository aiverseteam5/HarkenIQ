"""Correlation engine: onset-triggered rules + periodic sweeper (R-S4).

Onset transitions from ingest run the relevant rules immediately; the
sweeper re-runs everything on a short cadence to catch late quorums,
network ambiguity, recoveries, and parent hold-down resolution.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from harkeniq_sm.config import SMConfig
from harkeniq_sm.correlation import rules
from harkeniq_sm.db.repos import DeviceRepo, DomainRepo, SiteRepo, SubsystemStateRepo
from harkeniq_sm.incidents import IncidentService

logger = logging.getLogger("harkeniq.sm.correlation")

# QA-021: agent subsystem -> suppression event family (A2.6 policy keys)
_EVENT_FAMILY = {
    "psu": "power",
    "power": "power",
    "thermal": "thermal",
    "fan": "thermal",
    "network": "connectivity",
    "interface": "connectivity",
}


class CorrelationEngine:
    def __init__(self, sessionmaker, config: SMConfig, suppression=None) -> None:
        self.sessionmaker = sessionmaker
        self.config = config
        self.incidents = IncidentService(config)
        # QA-021: SuppressionEngine (A2.6), attached by the runtime
        self.suppression = suppression
        self._lock = asyncio.Lock()

    async def _site_ids(self, session) -> list[str]:
        """Every active site this Site Manager serves.

        Bootstraps the configured site when there are none yet, so a
        first boot still correlates.
        """
        from sqlalchemy import select

        from harkeniq_sm.db.models import Site

        rows = (
            await session.execute(select(Site).where(Site.status == "active"))
        ).scalars().all()
        if rows:
            return [r.id for r in rows]
        site = await SiteRepo(session).get_or_create(self.config.site_name)
        return [site.id]

    async def on_onset(
        self, device_id: str, subsystem: str, severity: str, onset_at: datetime
    ) -> None:
        """Ingest hook: consolidate the child, then correlate its subsystem."""
        async with self._lock:
            async with self.sessionmaker() as session:
                device = await DeviceRepo(session).get(device_id)
                if device is None:
                    return
                # E1.3: correlate inside the DEVICE'S OWN site, never the
                # Site Manager's configured one. Two devices in different
                # buildings failing at the same moment are a coincidence,
                # not a shared cause -- and with two sites on one process
                # the old code would have produced a shared-power incident
                # spanning estates that share no power at all.
                site_id = device.site_id
                if not site_id:
                    logger.warning(
                        "device %s has no site; refusing to correlate it "
                        "into a guessed one", device_id,
                    )
                    return
                await self.incidents.ensure_device_incident(
                    session, site_id, device, subsystem, severity, onset_at
                )
                if subsystem == "psu":
                    await rules.shared_power(
                        session, self.config, site_id, self.incidents
                    )
                elif subsystem == "thermal":
                    await rules.rack_thermal(
                        session, self.config, site_id, self.incidents
                    )
                await rules.batch_component(
                    session, self.config, site_id, self.incidents,
                    datetime.now(timezone.utc),
                )
                await self._evaluate_suppression(
                    session, device_id, subsystem, severity
                )
                await session.commit()

    async def sweep(self, now: Optional[datetime] = None) -> None:
        """Late quorums, ambiguity votes, recoveries, parent hold-down."""
        now = now or datetime.now(timezone.utc)
        async with self._lock:
            async with self.sessionmaker() as session:
                # E1.3: one pass PER SITE. A single pass over a Site
                # Manager serving several sites would let one site's
                # devices form a quorum with another's.
                for site_id in await self._site_ids(session):
                    await rules.shared_power(
                        session, self.config, site_id, self.incidents
                    )
                    await rules.rack_thermal(
                        session, self.config, site_id, self.incidents
                    )
                    await rules.batch_component(
                        session, self.config, site_id, self.incidents, now
                    )
                    await rules.network_ambiguity(
                        session, self.config, site_id, self.incidents, now
                    )
                    await rules.tor_connectivity(
                        session, self.config, site_id, self.incidents, now
                    )
                await self.incidents.resolve_recovered_children(session)
                await self.incidents.resolve_recovered_ambiguities(session)
                await self.incidents.auto_resolve_parents(session)
                await self._suppression_recovery(session)
                await session.commit()

    # -- QA-021: correlated-conclusion suppression (A2.6) -------------------

    async def _evaluate_suppression(
        self, session, device_id: str, subsystem: str, severity: str
    ) -> None:
        """Feed a verdict onset into the SuppressionEngine for every fault
        domain the device belongs to (Path 1 / Path 2 / hair-trigger)."""
        if self.suppression is None or severity not in ("WARNING", "CRITICAL"):
            return
        import time as _time

        from harkeniq_sm.suppression import CorrelationEvent

        family = _EVENT_FAMILY.get(subsystem, "component")
        domains = await DomainRepo(session).domains_for_device(device_id)
        for domain in domains:
            self.suppression.evaluate(
                CorrelationEvent(
                    device_id=device_id,
                    domain_id=domain.id,
                    domain_kind=domain.kind,
                    event_family=family,
                    severity=severity,
                    timestamp=_time.time(),
                )
            )

    async def _suppression_recovery(self, session) -> None:
        """Periodic auto-recovery check for suppressed domains: all member
        devices back to OK starts the stability clock (10 min)."""
        if self.suppression is None:
            return
        suppressed = self.suppression.get_suppressed_domains()
        if not suppressed:
            return
        non_ok_devices = {
            s.device_id for s in await SubsystemStateRepo(session).non_ok()
        }
        domain_repo = DomainRepo(session)
        for domain_id in suppressed:
            members = await domain_repo.members(domain_id)
            all_healthy = not any(m in non_ok_devices for m in members)
            self.suppression.check_auto_recovery(domain_id, all_healthy)

    async def run(self) -> None:
        """Sweeper loop; cancelled with the runtime TaskGroup."""
        interval = self.config.correlation.sweeper_interval_s
        while True:
            await asyncio.sleep(interval)
            try:
                await self.sweep()
            except Exception:  # pragma: no cover - keep sweeping
                logger.exception("correlation sweep failed")
