"""Background delinquency reconciler — runs hourly inside the Console service."""

from __future__ import annotations

import asyncio
import logging

from harkeniq_console.billing.engine import BillingEngine
from harkeniq_console.db.models import Subscription
from sqlalchemy import select

logger = logging.getLogger("harkeniq.console.billing.reconciler")


async def run_reconciler(
    sessionmaker,
    interval_seconds: int = 3600,
) -> None:
    """Loop forever: check delinquency state for every active subscription."""
    engine = BillingEngine()

    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await _reconcile_once(sessionmaker, engine)
        except asyncio.CancelledError:
            logger.info("Reconciler shutting down")
            return
        except Exception:
            logger.exception("Reconciler cycle failed — will retry next interval")


async def _reconcile_once(sessionmaker, engine: BillingEngine) -> int:
    """Single reconciliation pass. Returns count of transitions."""
    transitions = 0
    async with sessionmaker() as session:
        subs = (
            await session.execute(
                select(Subscription).where(Subscription.status == "active")
            )
        ).scalars().all()

        for sub in subs:
            try:
                result = await engine.check_delinquency(session, sub.tenant_id)
                if result["status"] != "current":
                    transitions += 1
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception(
                    "Reconciler: failed for tenant %s", sub.tenant_id,
                )

    if transitions:
        logger.info("Reconciler: %d tenants with delinquency issues", transitions)
    return transitions
