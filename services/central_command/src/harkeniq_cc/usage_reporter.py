"""Usage reporter: periodically report daily usage to Harken Console."""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("harkeniq.cc.usage_reporter")


async def usage_report_loop(state) -> None:
    """Report daily usage snapshots to the Console (L4).

    Full implementation in R2b Phase 2; stub sleeps indefinitely.
    """
    interval = state.config.usage_report_interval_s
    logger.info("Usage reporter started (interval=%.0fs, stub)", interval)
    while True:
        await asyncio.sleep(interval)
        # TODO: aggregate UsageSnapshotRepo data for today,
        # POST to state.config.console_url via httpx.
