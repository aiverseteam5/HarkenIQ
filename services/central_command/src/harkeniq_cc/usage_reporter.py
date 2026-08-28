"""Usage reporter: periodically report daily usage to Harken Console."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

import httpx

from harkeniq_cc.db.repos import SiteRepo, UsageSnapshotRepo
from harkeniq_cc.sm_client import SMClient

logger = logging.getLogger("harkeniq.cc.usage_reporter")


async def usage_report_loop(state) -> None:
    """Report daily usage snapshots to the Console (L4).

    Each cycle queries every active site for the previous day's usage via
    GetUsageSnapshot, stores the result locally in cc_usage_snapshots, and
    optionally forwards it to the Console's internal usage-events endpoint.
    """
    interval = state.config.usage_report_interval_s
    client = SMClient(state.config.sm_tls_ca)
    logger.info("Usage reporter started (interval=%.0fs)", interval)
    while True:
        await asyncio.sleep(interval)
        try:
            async with state.sessionmaker() as session:
                sites = await SiteRepo(session).list_all(state.config.tenant_id)
                yesterday = (date.today() - timedelta(days=1)).isoformat()
                for site in sites:
                    try:
                        usage = await client.get_usage_snapshot(
                            site.sm_endpoint,
                            site.sm_token,
                            state.config.tenant_id,
                            site.id,
                            yesterday,
                        )
                        # Store locally
                        await UsageSnapshotRepo(session).upsert(
                            date=yesterday,
                            site_id=site.id,
                            tenant_id=state.config.tenant_id,
                            node_count=usage["node_count"],
                            agent_versions=usage.get("agent_versions"),
                        )
                        # Report to Console if configured
                        if state.config.console_url:
                            try:
                                async with httpx.AsyncClient() as http:
                                    await http.post(
                                        f"{state.config.console_url}/api/internal/usage-events",
                                        json={
                                            "tenant_id": state.config.tenant_id,
                                            "site_id": site.id,
                                            "site_name": site.site_name,
                                            "date": yesterday,
                                            "node_count": usage["node_count"],
                                            "agent_versions": usage.get(
                                                "agent_versions"
                                            ),
                                        },
                                        headers={
                                            "Authorization": f"Bearer {state.config.console_api_key}"
                                        },
                                        timeout=10,
                                    )
                            except Exception as exc:
                                logger.warning(
                                    "Console usage report failed for %s: %s",
                                    site.site_name,
                                    exc,
                                )
                        await session.commit()
                        logger.debug(
                            "Usage report OK for %s: %d nodes",
                            site.site_name,
                            usage["node_count"],
                        )
                    except Exception as exc:
                        logger.error(
                            "Usage report failed for %s: %s", site.site_name, exc
                        )
                        await session.rollback()
        except Exception as exc:
            logger.error("Usage report cycle error: %s", exc)
