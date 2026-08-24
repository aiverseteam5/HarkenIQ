"""Warranty provider interface (R4-2 P15).

Vendor coverage reality (verified 2026-08):
  - Dell: TechDirect APIs are real and documented -- OAuth2 client
    credentials + asset-entitlements v5, batched service-tag lookup.
  - HPE: there is NO public warranty API for ProLiant/HPE gear (only
    the support.hpe.com web lookup; the "HP Warranty API" covers HP
    Inc. PCs, not HPE servers). HPE coverage therefore comes from the
    manual import endpoint (POST /api/warranty/import), not an adapter.

Providers are config-gated: no credentials, no provider, no calls.
Fetched records are cached in cc_warranty with a TTL (the design doc's
rate-limit/caching requirement) -- the fleet poller never triggers
vendor API calls directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class WarrantyRecord:
    """Normalized warranty/entitlement state for one device."""

    service_tag: str
    vendor: str = ""
    service_level: str = ""
    start_date: str = ""   # ISO date (YYYY-MM-DD)
    end_date: str = ""     # ISO date (YYYY-MM-DD)
    source: str = ""       # "dell_techdirect" | "import" | "mock"


def warranty_status(end_date: str, now: datetime | None = None) -> str:
    """Derive display status from the entitlement end date.

    active / expiring (within 90 days) / expired / unknown.
    """
    if not end_date:
        return "unknown"
    try:
        end = datetime.fromisoformat(end_date[:10]).replace(tzinfo=timezone.utc)
    except ValueError:
        return "unknown"
    current = now or datetime.now(timezone.utc)
    if end < current:
        return "expired"
    if (end - current).days <= 90:
        return "expiring"
    return "active"


class WarrantyProvider(ABC):
    """Fetches warranty records for a batch of service tags."""

    name: str = "base"

    @abstractmethod
    async def fetch(self, service_tags: list[str]) -> list[WarrantyRecord]:
        """Fetch records for the given tags. Tags the vendor does not
        know are simply absent from the result (never guessed)."""


class MockWarrantyProvider(WarrantyProvider):
    """Deterministic provider for tests and demo stacks."""

    name = "mock"

    def __init__(self, records: dict[str, WarrantyRecord] | None = None) -> None:
        self.records = records or {}
        self.calls: list[list[str]] = []

    async def fetch(self, service_tags: list[str]) -> list[WarrantyRecord]:
        self.calls.append(list(service_tags))
        return [self.records[t] for t in service_tags if t in self.records]
