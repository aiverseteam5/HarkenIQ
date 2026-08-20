"""Razorpay payment adapter (India/INR).

All methods raise NotImplementedError — implementation requires reading
current Razorpay API docs and will land in Phase 2.
"""

from __future__ import annotations

from harkeniq_console.billing.payment_base import PaymentProvider


class RazorpayAdapter(PaymentProvider):
    """Razorpay gateway adapter for Indian tenants."""

    def __init__(self, key_id: str, key_secret: str) -> None:
        self.key_id = key_id
        self.key_secret = key_secret

    async def ensure_customer(self, tenant_id: str, email: str, name: str) -> str:
        raise NotImplementedError("RazorpayAdapter.ensure_customer — Phase 2")

    async def create_payment(
        self, customer_id: str, amount: int, currency: str, description: str
    ) -> dict:
        raise NotImplementedError("RazorpayAdapter.create_payment — Phase 2")

    async def handle_webhook(self, payload: bytes, signature: str) -> dict:
        raise NotImplementedError("RazorpayAdapter.handle_webhook — Phase 2")

    async def refund(self, payment_id: str, amount: int) -> dict:
        raise NotImplementedError("RazorpayAdapter.refund — Phase 2")

    async def reconcile(self, tenant_id: str) -> dict:
        raise NotImplementedError("RazorpayAdapter.reconcile — Phase 2")
