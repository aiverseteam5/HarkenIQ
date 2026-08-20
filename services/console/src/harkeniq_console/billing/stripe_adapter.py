"""Stripe payment adapter (US/EU).

All methods raise NotImplementedError — implementation requires reading
current Stripe API docs and will land in Phase 2.
"""

from __future__ import annotations

from harkeniq_console.billing.payment_base import PaymentProvider


class StripeAdapter(PaymentProvider):
    """Stripe gateway adapter for US/EU tenants."""

    def __init__(self, secret_key: str, webhook_secret: str) -> None:
        self.secret_key = secret_key
        self.webhook_secret = webhook_secret

    async def ensure_customer(self, tenant_id: str, email: str, name: str) -> str:
        raise NotImplementedError("StripeAdapter.ensure_customer — Phase 2")

    async def create_payment(
        self, customer_id: str, amount: int, currency: str, description: str
    ) -> dict:
        raise NotImplementedError("StripeAdapter.create_payment — Phase 2")

    async def handle_webhook(self, payload: bytes, signature: str) -> dict:
        raise NotImplementedError("StripeAdapter.handle_webhook — Phase 2")

    async def refund(self, payment_id: str, amount: int) -> dict:
        raise NotImplementedError("StripeAdapter.refund — Phase 2")

    async def reconcile(self, tenant_id: str) -> dict:
        raise NotImplementedError("StripeAdapter.reconcile — Phase 2")
