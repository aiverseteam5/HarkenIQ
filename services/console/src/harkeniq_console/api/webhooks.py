"""Payment gateway webhook endpoints (public, no auth).

Razorpay and Stripe call these endpoints to notify about payment events.
Signature verification is handled by the payment adapter.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_console.api.deps import get_console_state, get_session
from harkeniq_console.billing.engine import BillingEngine
from harkeniq_console.billing.router import get_payment_provider
from harkeniq_console.db.models import utcnow
from harkeniq_console.db.repos import InvoiceRepo, PaymentRepo

logger = logging.getLogger("harkeniq.console.api.webhooks")

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

_engine = BillingEngine()


@router.post("/stripe")
async def stripe_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    state = request.app.state.console
    cfg = state.config

    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")

    adapter = get_payment_provider(
        "US",
        stripe_secret_key=cfg.stripe_secret_key,
        stripe_webhook_secret=cfg.stripe_webhook_secret,
    )

    try:
        event = await adapter.handle_webhook(payload, signature)
    except Exception as exc:
        logger.warning("Stripe webhook verification failed: %s", exc)
        raise HTTPException(400, "webhook verification failed") from exc

    return await _process_event(session, event, "stripe")


@router.post("/razorpay")
async def razorpay_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    state = request.app.state.console
    cfg = state.config

    payload = await request.body()
    signature = request.headers.get("x-razorpay-signature", "")

    adapter = get_payment_provider(
        "IN",
        razorpay_key_id=cfg.razorpay_key_id,
        razorpay_key_secret=cfg.razorpay_key_secret,
    )

    try:
        event = await adapter.handle_webhook(payload, signature)
    except Exception as exc:
        logger.warning("Razorpay webhook verification failed: %s", exc)
        raise HTTPException(400, "webhook verification failed") from exc

    return await _process_event(session, event, "razorpay")


async def _process_event(
    session: AsyncSession, event: dict, provider: str,
) -> dict:
    provider_event_id = event.get("provider_event_id", "")

    # idempotency — skip if already processed
    if provider_event_id:
        existing = await PaymentRepo(session).get_by_provider_event_id(
            provider_event_id,
        )
        if existing is not None:
            logger.info("Webhook event %s already processed", provider_event_id)
            return {"status": "already_processed"}

    event_type = event.get("event_type", "")
    provider_payment_id = event.get("provider_payment_id", "")

    if event_type == "payment_completed" and provider_payment_id:
        # find the payment record by provider_payment_id
        from sqlalchemy import select
        from harkeniq_console.db.models import Payment

        result = await session.execute(
            select(Payment).where(
                Payment.provider_payment_id == provider_payment_id,
            )
        )
        payment = result.scalar_one_or_none()

        if payment is not None:
            await PaymentRepo(session).update(
                payment,
                status="completed",
                completed_at=utcnow(),
                provider_event_id=provider_event_id,
            )

            if payment.invoice_id:
                await _engine.record_payment_and_restore(
                    session,
                    payment.tenant_id,
                    payment.invoice_id,
                    {
                        "provider": provider,
                        "provider_payment_id": provider_payment_id,
                    },
                )
            await session.commit()
            logger.info(
                "Payment completed: %s via %s", provider_payment_id, provider,
            )
        else:
            logger.warning(
                "No payment found for provider_payment_id %s",
                provider_payment_id,
            )

    elif event_type == "payment_failed" and provider_payment_id:
        from sqlalchemy import select
        from harkeniq_console.db.models import Payment

        result = await session.execute(
            select(Payment).where(
                Payment.provider_payment_id == provider_payment_id,
            )
        )
        payment = result.scalar_one_or_none()

        if payment is not None:
            await PaymentRepo(session).update(
                payment,
                status="failed",
                provider_event_id=provider_event_id,
            )
            await session.commit()
            logger.info("Payment failed: %s via %s", provider_payment_id, provider)

    return {"status": "processed", "event_type": event_type}
