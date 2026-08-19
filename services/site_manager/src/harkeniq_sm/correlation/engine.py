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
from harkeniq_sm.db.repos import DeviceRepo, SiteRepo
from harkeniq_sm.incidents import IncidentService

logger = logging.getLogger("harkeniq.sm.correlation")


class CorrelationEngine:
    def __init__(self, sessionmaker, config: SMConfig) -> None:
        self.sessionmaker = sessionmaker
        self.config = config
        self.incidents = IncidentService(config)
        self._lock = asyncio.Lock()

    async def on_onset(
        self, device_id: str, subsystem: str, severity: str, onset_at: datetime
    ) -> None:
        """Ingest hook: consolidate the child, then correlate its subsystem."""
        async with self._lock:
            async with self.sessionmaker() as session:
                site = await SiteRepo(session).get_or_create(self.config.site_name)
                device = await DeviceRepo(session).get(device_id)
                if device is None:
                    return
                await self.incidents.ensure_device_incident(
                    session, site.id, device, subsystem, severity, onset_at
                )
                if subsystem == "psu":
                    await rules.shared_power(
                        session, self.config, site.id, self.incidents
                    )
                elif subsystem == "thermal":
                    await rules.rack_thermal(
                        session, self.config, site.id, self.incidents
                    )
                await rules.batch_component(
                    session, self.config, site.id, self.incidents,
                    datetime.now(timezone.utc),
                )
                await session.commit()

    async def sweep(self, now: Optional[datetime] = None) -> None:
        """Late quorums, ambiguity votes, recoveries, parent hold-down."""
        now = now or datetime.now(timezone.utc)
        async with self._lock:
            async with self.sessionmaker() as session:
                site = await SiteRepo(session).get_or_create(self.config.site_name)
                await rules.shared_power(session, self.config, site.id, self.incidents)
                await rules.rack_thermal(session, self.config, site.id, self.incidents)
                await rules.batch_component(
                    session, self.config, site.id, self.incidents, now
                )
                await rules.network_ambiguity(
                    session, self.config, site.id, self.incidents, now
                )
                await self.incidents.resolve_recovered_children(session)
                await self.incidents.resolve_recovered_ambiguities(session)
                await self.incidents.auto_resolve_parents(session)
                await session.commit()

    async def run(self) -> None:
        """Sweeper loop; cancelled with the runtime TaskGroup."""
        interval = self.config.correlation.sweeper_interval_s
        while True:
            await asyncio.sleep(interval)
            try:
                await self.sweep()
            except Exception:  # pragma: no cover - keep sweeping
                logger.exception("correlation sweep failed")
