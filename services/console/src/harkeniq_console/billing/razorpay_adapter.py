"""Razorpay payment adapter (India / INR).

Uses the ``razorpay`` Python SDK. All amounts are in paise (smallest
INR unit). Webhook signature verification uses HMAC-SHA256.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

import razorpay

from harkeniq_console.billing.payment_base import PaymentProvider

logger = logging.getLogger("harkeniq.console.billing.razorpay")


class RazorpayAdapter(PaymentProvider):
    """Razorpay gateway adapter for Indian tenants."""

    def __init__(self, key_id: str, key_secret: str) -> None:
        self.key_id = key_id
        self.key_secret = key_secret
        self.client = razorpay.Client(auth=(key_id, key_secret))

    async def ensure_customer(self, tenant_id: str, email: str, name: str) -> str:
        customer = self.client.customer.create(
            {
                "name": name,
                "email": email,
                "notes": {"tenant_id": tenant_id},
            }
        )
        logger.info("Razorpay customer created: %s", customer["id"])
        return customer["id"]

    async def create_payment(
        self, customer_id: str, amount: int, currency: str, description: str,
    ) -> dict:
        order = self.client.order.create(
            {
                "amount": amount,
                "currency": currency.upper(),
                "notes": {
                    "customer_id": customer_id,
                    "description": description,
                },
            }
        )
        return {
            "id": order["id"],
            "amount": order["amount"],
            "currency": order["currency"],
            "status": order.get("status", "created"),
            "provider": "razorpay",
        }

    async def handle_webhook(self, payload: bytes, signature: str) -> dict:
        # verify HMAC-SHA256 signature
        expected = hmac.new(
            self.key_secret.encode(),
            payload,
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected, signature):
            raise ValueError("Razorpay webhook signature verification failed")

        event = json.loads(payload)
        event_type = event.get("event", "")
        entity = (
            event.get("payload", {})
            .get("payment", {})
            .get("entity", {})
        )

        status_map = {
            "payment.captured": "payment_completed",
            "payment.failed": "payment_failed",
            "refund.processed": "refund_completed",
        }

        return {
            "event_type": status_map.get(event_type, event_type),
            "provider_payment_id": entity.get("id", ""),
            "provider_event_id": event.get("event_id", event.get("id", "")),
            "amount": entity.get("amount", 0),
            "currency": entity.get("currency", "INR"),
            "status": entity.get("status", ""),
            "raw_event_type": event_type,
        }

    async def refund(self, payment_id: str, amount: int) -> dict:
        refund = self.client.payment.refund(payment_id, amount)
        return {
            "id": refund["id"],
            "payment_id": payment_id,
            "amount": refund.get("amount", amount),
            "status": refund.get("status", "processed"),
        }

    async def reconcile(self, tenant_id: str) -> dict:
        payments = self.client.payment.all({"count": 100})
        items = payments.get("items", [])
        return {
            "payments_checked": len(items),
            "discrepancies": [],
            "provider": "razorpay",
        }
