"""Fleet poller: periodically pull device snapshots from all registered SMs."""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("harkeniq.cc.fleet_poller")


async def fleet_poll_loop(state) -> None:
    """Poll all registered Site Managers for fleet snapshots.

    Full implementation in R2b Phase 2; stub sleeps indefinitely.
    """
    interval = state.config.site_poll_interval_s
    logger.info("Fleet poller started (interval=%.0fs, stub)", interval)
    while True:
        await asyncio.sleep(interval)
        # TODO: iterate state.sessionmaker -> SiteRepo.list_all,
        # call SMClient.get_fleet_snapshot for each, upsert FleetCacheRepo.
