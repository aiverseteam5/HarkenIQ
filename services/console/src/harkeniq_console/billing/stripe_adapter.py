"""Stripe payment adapter (US / EU).

Uses the ``stripe`` Python SDK. All amounts are in cents (smallest
USD/EUR unit). Webhook verification uses Stripe-Signature header.
"""

from __future__ import annotations

import logging

import stripe

from harkeniq_console.billing.payment_base import PaymentProvider

logger = logging.getLogger("harkeniq.console.billing.stripe")


class StripeAdapter(PaymentProvider):
    """Stripe gateway adapter for US/EU tenants."""

    def __init__(self, secret_key: str, webhook_secret: str) -> None:
        self.secret_key = secret_key
        self.webhook_secret = webhook_secret

    async def ensure_customer(self, tenant_id: str, email: str, name: str) -> str:
        customer = stripe.Customer.create(
            email=email,
            name=name,
            metadata={"tenant_id": tenant_id},
            api_key=self.secret_key,
        )
        logger.info("Stripe customer created: %s", customer.id)
        return customer.id

    async def create_payment(
        self, customer_id: str, amount: int, currency: str, description: str,
    ) -> dict:
        intent = stripe.PaymentIntent.create(
            amount=amount,
            currency=currency.lower(),
            customer=customer_id,
            description=description,
            api_key=self.secret_key,
        )
        return {
            "id": intent.id,
            "client_secret": intent.client_secret,
            "amount": intent.amount,
            "currency": intent.currency,
            "status": intent.status,
            "provider": "stripe",
        }

    async def handle_webhook(self, payload: bytes, signature: str) -> dict:
        event = stripe.Webhook.construct_event(
            payload, signature, self.webhook_secret,
        )

        event_type = event["type"]
        data_obj = event["data"]["object"]

        status_map = {
            "payment_intent.succeeded": "payment_completed",
            "payment_intent.payment_failed": "payment_failed",
            "charge.refunded": "refund_completed",
        }

        return {
            "event_type": status_map.get(event_type, event_type),
            "provider_payment_id": data_obj.get("id", ""),
            "provider_event_id": event.get("id", ""),
            "amount": data_obj.get("amount", 0),
            "currency": data_obj.get("currency", "usd"),
            "status": data_obj.get("status", ""),
            "raw_event_type": event_type,
        }

    async def refund(self, payment_id: str, amount: int) -> dict:
        refund = stripe.Refund.create(
            payment_intent=payment_id,
            amount=amount,
            api_key=self.secret_key,
        )
        return {
            "id": refund.id,
            "payment_id": payment_id,
            "amount": refund.amount,
            "status": refund.status,
        }

    async def reconcile(self, tenant_id: str) -> dict:
        intents = stripe.PaymentIntent.list(
            limit=100,
            api_key=self.secret_key,
        )
        return {
            "payments_checked": len(intents.get("data", [])),
            "discrepancies": [],
            "provider": "stripe",
        }
