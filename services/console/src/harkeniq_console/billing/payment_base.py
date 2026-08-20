"""Payment provider abstract base class.

Concrete adapters (Razorpay, Stripe) implement this interface. The
router selects the adapter based on tenant billing_country.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class PaymentProvider(ABC):
    """Gateway-agnostic payment operations."""

    @abstractmethod
    async def ensure_customer(self, tenant_id: str, email: str, name: str) -> str:
        """Create or retrieve the customer record. Returns provider customer ID."""
        ...

    @abstractmethod
    async def create_payment(
        self, customer_id: str, amount: int, currency: str, description: str
    ) -> dict:
        """Initiate a payment. Returns provider-specific payment object."""
        ...

    @abstractmethod
    async def handle_webhook(self, payload: bytes, signature: str) -> dict:
        """Verify and process an inbound webhook. Returns parsed event."""
        ...

    @abstractmethod
    async def refund(self, payment_id: str, amount: int) -> dict:
        """Issue a full or partial refund."""
        ...

    @abstractmethod
    async def reconcile(self, tenant_id: str) -> dict:
        """Reconcile payment records with the gateway ledger."""
        ...
