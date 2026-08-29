"""Usage metering — event recording, signed report upload, summaries.

Every method takes an AsyncSession as the first argument.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_console.db.models import utcnow
from harkeniq_console.db.repos import (
    PriceBookRepo,
    SubscriptionRepo,
    TenantRepo,
    UsageEventRepo,
)

logger = logging.getLogger("harkeniq.console.billing.metering")


def _parse_date(val) -> date:
    """Accept date object or ISO string, return date."""
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, str):
        return date.fromisoformat(val)
    raise ValueError(f"cannot parse date from {val!r}")


class MeteringService:
    """Stateless metering service."""

    async def record_usage_event(
        self,
        session: AsyncSession,
        tenant_id: str,
        site_name: str,
        event_date: date,
        node_count: int,
        agent_versions: dict | list | None = None,
        source: str = "cc_report",
    ) -> dict:
        repo = UsageEventRepo(session)
        ev = await repo.record(
            tenant_id=tenant_id,
            site_name=site_name,
            date=event_date,
            node_count=node_count,
            agent_versions=agent_versions,
            source=source,
        )
        return {"id": ev.id, "tenant_id": tenant_id, "node_count": node_count}

    async def ingest_usage_batch(
        self,
        session: AsyncSession,
        tenant_id: str,
        events: list[dict],
    ) -> int:
        repo = UsageEventRepo(session)
        rows = [
            {
                "tenant_id": tenant_id,
                "site_name": e["site_name"],
                # P0 2026-08-29 (C3, found by the wire test): CC sends an
                # ISO string but usage_events.date is a Date column — the
                # raw string raises on sqlite AND asyncpg. The signed
                # upload path already parsed; this path never did.
                "date": _parse_date(e["date"]),
                "node_count": e["node_count"],
                "agent_versions": e.get("agent_versions"),
                "source": "cc_report",
            }
            for e in events
        ]
        count = await repo.record_batch(rows)
        logger.info("Ingested %d usage events for tenant %s", count, tenant_id)
        return count

    async def upload_signed_usage_report(
        self,
        session: AsyncSession,
        tenant_id: str,
        report_bytes: bytes,
        public_key_pem: str,
    ) -> dict:
        """Verify Ed25519-signed usage report and record events.

        Format: first 64 bytes = Ed25519 signature, rest = UTF-8 JSON payload.
        Payload: {"tenant_id": str, "period": str, "events": [{site_name, date, node_count}]}
        """
        if len(report_bytes) <= 64:
            raise ValueError("report too short — must have 64-byte signature + payload")

        signature = report_bytes[:64]
        payload_bytes = report_bytes[64:]

        # verify signature
        pub_key = load_pem_public_key(public_key_pem.encode())
        if not isinstance(pub_key, Ed25519PublicKey):
            raise ValueError("public key is not Ed25519")
        try:
            pub_key.verify(signature, payload_bytes)
        except Exception as exc:
            raise ValueError(f"signature verification failed: {exc}") from exc

        payload = json.loads(payload_bytes)
        if payload.get("tenant_id") != tenant_id:
            raise ValueError("tenant_id mismatch in signed report")

        events = payload.get("events", [])
        repo = UsageEventRepo(session)
        count = await repo.record_batch(
            [
                {
                    "tenant_id": tenant_id,
                    "site_name": e["site_name"],
                    "date": _parse_date(e["date"]),
                    "node_count": e["node_count"],
                    "agent_versions": e.get("agent_versions"),
                    "source": "signed_upload",
                }
                for e in events
            ]
        )
        logger.info(
            "Uploaded signed usage report: tenant=%s events=%d period=%s",
            tenant_id, count, payload.get("period", "?"),
        )
        return {"events_recorded": count, "period": payload.get("period")}

    async def get_usage_summary(
        self,
        session: AsyncSession,
        tenant_id: str,
        period_start: date,
        period_end: date,
    ) -> dict:
        repo = UsageEventRepo(session)
        high_water = await repo.get_high_water(tenant_id, period_start, period_end)
        daily = await repo.get_daily_counts(tenant_id, period_start, period_end)
        per_site = await repo.get_per_site_summary(tenant_id, period_start, period_end)

        return {
            "high_water": high_water,
            "daily_counts": [
                {"date": str(d.date), "node_count": d.node_count} for d in daily
            ],
            "per_site": per_site,
        }

    async def estimate_upcoming_trueup(
        self,
        session: AsyncSession,
        tenant_id: str,
    ) -> dict:
        tenant = await TenantRepo(session).get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"tenant {tenant_id} not found")

        sub = await SubscriptionRepo(session).get_by_tenant(tenant_id)
        if sub is None:
            return {
                "high_water_so_far": 0,
                "committed": 0,
                "estimated_overage": 0,
                "estimated_amount_cents": 0,
                "currency": tenant.currency,
            }

        now = utcnow()
        month_start = now.replace(day=1).date() if hasattr(now, "date") else now.replace(day=1)
        if hasattr(month_start, "date"):
            month_start = month_start.date()
        today = now.date() if hasattr(now, "date") else now

        repo = UsageEventRepo(session)
        high_water = await repo.get_high_water(tenant_id, month_start, today)
        overage = max(0, high_water - sub.node_commit)

        price = await PriceBookRepo(session).get_price(
            plan=sub.plan,
            currency=tenant.currency,
            billing_interval=sub.billing_frequency,
            version=sub.price_book_version,
        )
        price_per_node = price.node_price_cents if price else 0
        estimated_amount = overage * price_per_node

        return {
            "high_water_so_far": high_water,
            "committed": sub.node_commit,
            "estimated_overage": overage,
            "estimated_amount_cents": estimated_amount,
            "currency": tenant.currency,
        }
