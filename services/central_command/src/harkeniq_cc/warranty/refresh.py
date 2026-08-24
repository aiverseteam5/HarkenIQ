"""Warranty refresh loop (R4-2 P15).

Periodically finds fleet devices whose warranty cache entry is missing
or older than the TTL and fetches fresh records from the configured
provider. Only Dell has an API provider today; other vendors' records
arrive via POST /api/warranty/import and are refreshed the same way
only if a provider for their vendor ever exists (until then the import
is authoritative and the loop leaves them alone).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from harkeniq_cc.db.repos import FleetCacheRepo, WarrantyRepo
from harkeniq_cc.warranty.base import WarrantyProvider

logger = logging.getLogger("harkeniq.cc.warranty")


def make_provider(config) -> Optional[WarrantyProvider]:
    """Build the configured provider (credential-gated); None = disabled."""
    if config.dell_api_client_id and config.dell_api_client_secret:
        from harkeniq_cc.warranty.dell_techdirect import DellTechDirectProvider

        return DellTechDirectProvider(
            client_id=config.dell_api_client_id,
            client_secret=config.dell_api_client_secret,
        )
    return None


async def refresh_once(state, provider: WarrantyProvider) -> int:
    """One refresh cycle; returns the number of records updated."""
    async with state.sessionmaker() as session:
        devices = await FleetCacheRepo(session).list_all(state.config.tenant_id)
        # The Dell provider only answers for Dell tags; don't burn rate
        # limit on tags the vendor cannot know.
        tags = [
            d.service_tag for d in devices
            if d.service_tag and (provider.name != "dell_techdirect"
                                  or d.vendor == "dell")
        ]
        warranty_repo = WarrantyRepo(session)
        stale = await warranty_repo.stale_or_missing_tags(
            tags, state.config.warranty_ttl_s,
            tenant_id=state.config.tenant_id,
        )
        if not stale:
            return 0
        records = await provider.fetch(stale)
        count = await warranty_repo.upsert_records(
            records, tenant_id=state.config.tenant_id
        )
        await session.commit()
        logger.info(
            "Warranty refresh: %d stale tag(s), %d record(s) updated",
            len(stale), count,
        )
        return count


async def warranty_refresh_loop(state) -> None:
    """Background task: TTL-driven warranty refresh."""
    provider = make_provider(state.config)
    if provider is None:
        logger.info("Warranty refresh disabled (no vendor API credentials)")
        return
    interval = state.config.warranty_refresh_interval_s
    logger.info("Warranty refresh loop started (interval=%.0fs, provider=%s)",
                interval, provider.name)
    while True:
        await asyncio.sleep(interval)
        try:
            await refresh_once(state, provider)
        except Exception as exc:
            logger.error("Warranty refresh cycle error: %s", exc)
