"""Marketplace install sync: Console -> CC -> SM (R5-2, A8).

CC PULLS its tenant's install events from the Console internal API
(the existing CC->Console credential direction; Console never dials
CC), then pushes each installed skill to every registered Site
Manager via the InstallSkill RPC, which queues skill_install
directives agents pick up on their next poll (R5-1 transport).

Delivery is durably deduped in cc_skill_deliveries keyed by
(install_id, site_id) -- a restart never re-pushes, and a failed push
is retried on the next cycle (failed rows are re-attempted, delivered
rows are not).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

from harkeniq_cc.db.repos import AuditRepo, SiteRepo, SkillDeliveryRepo
from harkeniq_cc.sm_client import SMClient

logger = logging.getLogger("harkeniq.cc.marketplace_sync")


class MarketplaceSync:
    """One pull-and-push cycle, testable in isolation.

    ``transport`` is an optional httpx transport (ASGI in tests);
    ``sm_client`` is injectable for the same reason.
    """

    def __init__(
        self,
        state,
        sm_client: Optional[SMClient] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._state = state
        self._sm_client = sm_client or SMClient(
            getattr(state.config, "sm_tls_ca", "")
        )
        self._transport = transport

    async def _fetch_installs(self) -> list[dict]:
        config = self._state.config
        if not config.console_url:
            return []
        kwargs: dict = {"timeout": 30.0}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        try:
            async with httpx.AsyncClient(**kwargs) as client:
                response = await client.get(
                    f"{config.console_url.rstrip('/')}"
                    "/api/internal/marketplace/installs",
                    params={"tenant_id": config.tenant_id},
                    headers={
                        "Authorization": f"Bearer {config.console_api_key}"
                    },
                )
                response.raise_for_status()
                return response.json().get("installs", [])
        except Exception as exc:
            logger.warning("Marketplace install fetch failed: %s", exc)
            return []

    async def run_cycle(self) -> int:
        """Pull installs, push undelivered (install, site) pairs.

        Returns the number of successful deliveries this cycle.
        """
        installs = await self._fetch_installs()
        if not installs:
            return 0
        delivered = 0
        async with self._state.sessionmaker() as session:
            sites = await SiteRepo(session).list_all(
                self._state.config.tenant_id
            )
            deliveries = SkillDeliveryRepo(session)
            audit = AuditRepo(session)
            for install in installs:
                install_id = str(install.get("install_id", ""))
                if not install_id:
                    continue
                for site in sites:
                    existing = await deliveries.get(install_id, site.id)
                    if existing is not None and existing.status == "delivered":
                        continue
                    ack = await self._push(site, install)
                    await deliveries.record(
                        install_id=install_id,
                        site_id=site.id,
                        skill_name=str(install.get("skill_name", "")),
                        skill_version=str(install.get("skill_version", "")),
                        status="delivered" if ack.get("accepted") else "failed",
                        directives_queued=int(ack.get("queued", 0)),
                        detail=str(ack.get("reason", "")),
                    )
                    await audit.append(
                        "marketplace-sync",
                        "marketplace.skill.deliver"
                        if ack.get("accepted") else "marketplace.skill.deliver_failed",
                        install_id,
                        tenant_id=self._state.config.tenant_id,
                        detail={"site_id": site.id,
                                "skill_name": install.get("skill_name"),
                                "queued": ack.get("queued", 0),
                                "reason": ack.get("reason", "")},
                    )
                    if ack.get("accepted"):
                        delivered += 1
            await session.commit()
        if delivered:
            logger.info("Marketplace sync: %d delivery(ies)", delivered)
        return delivered

    async def _push(self, site, install: dict) -> dict:
        try:
            return await self._sm_client.install_skill(
                sm_endpoint=site.sm_endpoint,
                token=site.sm_token,
                tenant_id=self._state.config.tenant_id,
                site_id=site.id,
                skill_name=str(install.get("skill_name", "")),
                skill_version=str(install.get("skill_version", "1")),
                yaml_content=str(install.get("yaml_content", "")),
                tier=str(install.get("tier", "community")),
                issued_by=f"marketplace:{install.get('install_id', '')}",
            )
        except Exception as exc:
            logger.warning(
                "InstallSkill push to %s failed: %s", site.site_name, exc
            )
            return {"accepted": False, "queued": 0, "reason": str(exc)}


async def marketplace_sync_loop(state) -> None:
    """Background task: periodic Console pull + SM push."""
    if not state.config.console_url:
        logger.info("Marketplace sync disabled (no console_url)")
        return
    sync = MarketplaceSync(state)
    interval = state.config.marketplace_sync_interval_s
    logger.info("Marketplace sync loop started (interval=%.0fs)", interval)
    while True:
        await asyncio.sleep(interval)
        try:
            await sync.run_cycle()
        except Exception as exc:
            logger.error("Marketplace sync cycle error: %s", exc)
