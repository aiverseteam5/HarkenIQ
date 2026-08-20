"""Payment provider routing — selects Razorpay or Stripe by tenant region."""

from __future__ import annotations

from harkeniq_console.billing.payment_base import PaymentProvider
from harkeniq_console.billing.razorpay_adapter import RazorpayAdapter
from harkeniq_console.billing.stripe_adapter import StripeAdapter

# Countries routed to Razorpay; all others go to Stripe.
_RAZORPAY_COUNTRIES = {"IN"}


def get_payment_provider(
    billing_country: str,
    *,
    razorpay_key_id: str = "",
    razorpay_key_secret: str = "",
    stripe_secret_key: str = "",
    stripe_webhook_secret: str = "",
) -> PaymentProvider:
    """Return the payment adapter appropriate for *billing_country*."""
    if billing_country.upper() in _RAZORPAY_COUNTRIES:
        return RazorpayAdapter(key_id=razorpay_key_id, key_secret=razorpay_key_secret)
    return StripeAdapter(secret_key=stripe_secret_key, webhook_secret=stripe_webhook_secret)
