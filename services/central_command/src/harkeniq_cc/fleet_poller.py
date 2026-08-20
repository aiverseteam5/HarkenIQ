"""Fleet poller: periodically pull device snapshots from all registered SMs."""

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
    client = SMClient()
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
                            )
                        await SiteRepo(session).update_last_seen(site)
                        await session.commit()
                        logger.debug(
                            "Fleet poll OK for %s: %d devices",
                            site.site_name,
                            len(snapshot.get("devices", [])),
                        )
                    except Exception as exc:
                        logger.error(
                            "Fleet poll failed for %s: %s", site.site_name, exc
                        )
                        await session.rollback()
        except Exception as exc:
            logger.error("Fleet poll cycle error: %s", exc)
