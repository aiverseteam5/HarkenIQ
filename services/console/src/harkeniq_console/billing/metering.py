"""Usage metering — event recording, signed reports, summaries.

All methods are stubs for Phase 2.
"""

from __future__ import annotations


class MeteringService:
    """Track per-tenant node usage for billing true-ups."""

    async def record_usage_event(self, tenant_id: str, event: dict) -> None:
        """Record a single usage event (e.g. node check-in)."""
        raise NotImplementedError("MeteringService.record_usage_event — Phase 2")

    async def upload_signed_usage_report(self, tenant_id: str, report: bytes) -> dict:
        """Ingest a signed usage report from a Site Manager."""
        raise NotImplementedError("MeteringService.upload_signed_usage_report — Phase 2")

    async def get_usage_summary(self, tenant_id: str, period: str) -> dict:
        """Return aggregated usage for a billing period."""
        raise NotImplementedError("MeteringService.get_usage_summary — Phase 2")

    async def estimate_upcoming_trueup(self, tenant_id: str) -> dict:
        """Estimate the next monthly true-up based on current high-water."""
        raise NotImplementedError("MeteringService.estimate_upcoming_trueup — Phase 2")
