"""Phase 4 payment adapter and router tests."""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import pytest

from harkeniq_console.billing.payment_base import PaymentProvider
from harkeniq_console.billing.router import get_payment_provider


# ── PaymentProvider ABC ──────────────────────────────────────────────


class TestPaymentProviderBase:
    def test_is_abstract(self):
        from abc import ABC
        assert issubclass(PaymentProvider, ABC)

    def test_has_required_methods(self):
        methods = ["ensure_customer", "create_payment", "handle_webhook", "refund", "reconcile"]
        for m in methods:
            assert hasattr(PaymentProvider, m)

    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            PaymentProvider()


# ── RazorpayAdapter ──────────────────────────────────────────────────


class TestRazorpayAdapter:
    @pytest.fixture
    def adapter(self):
        with patch("harkeniq_console.billing.razorpay_adapter.razorpay") as mock_rz:
            mock_client = MagicMock()
            mock_rz.Client.return_value = mock_client
            from harkeniq_console.billing.razorpay_adapter import RazorpayAdapter
            a = RazorpayAdapter(key_id="rzp_test", key_secret="secret123")
            a.client = mock_client
            yield a, mock_client

    async def test_ensure_customer(self, adapter):
        a, client = adapter
        client.customer.create.return_value = {"id": "cust_abc"}
        result = await a.ensure_customer("t1", "test@acme.com", "Acme")
        assert result == "cust_abc"
        client.customer.create.assert_called_once()

    async def test_ensure_customer_passes_tenant(self, adapter):
        a, client = adapter
        client.customer.create.return_value = {"id": "cust_x"}
        await a.ensure_customer("t1", "e@x.com", "X")
        call_args = client.customer.create.call_args[0][0]
        assert call_args["notes"]["tenant_id"] == "t1"

    async def test_create_payment(self, adapter):
        a, client = adapter
        client.order.create.return_value = {
            "id": "order_123", "amount": 500000, "currency": "INR", "status": "created",
        }
        result = await a.create_payment("cust_abc", 500000, "INR", "Invoice")
        assert result["id"] == "order_123"
        assert result["provider"] == "razorpay"

    async def test_create_payment_params(self, adapter):
        a, client = adapter
        client.order.create.return_value = {"id": "o", "amount": 100, "currency": "INR"}
        await a.create_payment("cust_1", 100, "inr", "Test")
        call_args = client.order.create.call_args[0][0]
        assert call_args["amount"] == 100
        assert call_args["currency"] == "INR"

    async def test_handle_webhook_payment_captured(self, adapter):
        a, _ = adapter
        payload_dict = {
            "event": "payment.captured",
            "event_id": "evt_001",
            "payload": {"payment": {"entity": {
                "id": "pay_123", "amount": 5000, "currency": "INR", "status": "captured",
            }}},
        }
        payload = json.dumps(payload_dict).encode()
        sig = hmac.new(b"secret123", payload, hashlib.sha256).hexdigest()
        result = await a.handle_webhook(payload, sig)
        assert result["event_type"] == "payment_completed"
        assert result["provider_payment_id"] == "pay_123"

    async def test_handle_webhook_payment_failed(self, adapter):
        a, _ = adapter
        payload_dict = {
            "event": "payment.failed",
            "event_id": "evt_002",
            "payload": {"payment": {"entity": {"id": "pay_fail", "status": "failed"}}},
        }
        payload = json.dumps(payload_dict).encode()
        sig = hmac.new(b"secret123", payload, hashlib.sha256).hexdigest()
        result = await a.handle_webhook(payload, sig)
        assert result["event_type"] == "payment_failed"

    async def test_handle_webhook_refund(self, adapter):
        a, _ = adapter
        payload_dict = {
            "event": "refund.processed",
            "event_id": "evt_003",
            "payload": {"payment": {"entity": {"id": "pay_ref"}}},
        }
        payload = json.dumps(payload_dict).encode()
        sig = hmac.new(b"secret123", payload, hashlib.sha256).hexdigest()
        result = await a.handle_webhook(payload, sig)
        assert result["event_type"] == "refund_completed"

    async def test_handle_webhook_bad_signature(self, adapter):
        a, _ = adapter
        payload = b'{"event":"payment.captured"}'
        with pytest.raises(ValueError, match="signature verification failed"):
            await a.handle_webhook(payload, "bad_sig")

    async def test_refund(self, adapter):
        a, client = adapter
        client.payment.refund.return_value = {"id": "rfnd_1", "amount": 1000, "status": "processed"}
        result = await a.refund("pay_123", 1000)
        assert result["id"] == "rfnd_1"
        client.payment.refund.assert_called_once_with("pay_123", 1000)

    async def test_reconcile(self, adapter):
        a, client = adapter
        client.payment.all.return_value = {"items": [{"id": "p1"}, {"id": "p2"}]}
        result = await a.reconcile("t1")
        assert result["payments_checked"] == 2
        assert result["provider"] == "razorpay"


# ── StripeAdapter ────────────────────────────────────────────────────


class TestStripeAdapter:
    @pytest.fixture
    def adapter(self):
        from harkeniq_console.billing.stripe_adapter import StripeAdapter
        return StripeAdapter(secret_key="sk_test_123", webhook_secret="whsec_123")

    @patch("stripe.Customer.create")
    async def test_ensure_customer(self, mock_create, adapter):
        mock_create.return_value = MagicMock(id="cus_stripe_1")
        result = await adapter.ensure_customer("t1", "test@acme.com", "Acme")
        assert result == "cus_stripe_1"
        mock_create.assert_called_once()

    @patch("stripe.Customer.create")
    async def test_ensure_customer_metadata(self, mock_create, adapter):
        mock_create.return_value = MagicMock(id="cus_s2")
        await adapter.ensure_customer("t1", "e@x.com", "X")
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["metadata"]["tenant_id"] == "t1"

    @patch("stripe.PaymentIntent.create")
    async def test_create_payment(self, mock_create, adapter):
        mock_create.return_value = MagicMock(
            id="pi_123", client_secret="cs_123", amount=5000, currency="usd", status="requires_payment_method",
        )
        result = await adapter.create_payment("cus_1", 5000, "USD", "Invoice")
        assert result["id"] == "pi_123"
        assert result["provider"] == "stripe"

    @patch("stripe.PaymentIntent.create")
    async def test_create_payment_params(self, mock_create, adapter):
        mock_create.return_value = MagicMock(id="pi", client_secret="c", amount=100, currency="usd", status="r")
        await adapter.create_payment("cus_1", 100, "usd", "Desc")
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["amount"] == 100
        assert call_kwargs["currency"] == "usd"

    @patch("stripe.Webhook.construct_event")
    async def test_handle_webhook_succeeded(self, mock_construct, adapter):
        mock_construct.return_value = {
            "type": "payment_intent.succeeded",
            "id": "evt_s1",
            "data": {"object": {"id": "pi_ok", "amount": 5000, "currency": "usd", "status": "succeeded"}},
        }
        result = await adapter.handle_webhook(b"payload", "sig")
        assert result["event_type"] == "payment_completed"
        assert result["provider_payment_id"] == "pi_ok"

    @patch("stripe.Webhook.construct_event")
    async def test_handle_webhook_failed(self, mock_construct, adapter):
        mock_construct.return_value = {
            "type": "payment_intent.payment_failed",
            "id": "evt_s2",
            "data": {"object": {"id": "pi_fail", "status": "failed"}},
        }
        result = await adapter.handle_webhook(b"payload", "sig")
        assert result["event_type"] == "payment_failed"

    @patch("stripe.Webhook.construct_event")
    async def test_handle_webhook_refund(self, mock_construct, adapter):
        mock_construct.return_value = {
            "type": "charge.refunded",
            "id": "evt_s3",
            "data": {"object": {"id": "ch_ref", "amount": 2000}},
        }
        result = await adapter.handle_webhook(b"payload", "sig")
        assert result["event_type"] == "refund_completed"

    @patch("stripe.Webhook.construct_event")
    async def test_handle_webhook_verification_error(self, mock_construct, adapter):
        import stripe
        mock_construct.side_effect = stripe.error.SignatureVerificationError("bad", "sig")
        with pytest.raises(stripe.error.SignatureVerificationError):
            await adapter.handle_webhook(b"payload", "bad_sig")

    @patch("stripe.Refund.create")
    async def test_refund(self, mock_create, adapter):
        mock_create.return_value = MagicMock(id="re_1", amount=1000, status="succeeded")
        result = await adapter.refund("pi_123", 1000)
        assert result["id"] == "re_1"
        mock_create.assert_called_once()

    @patch("stripe.PaymentIntent.list")
    async def test_reconcile(self, mock_list, adapter):
        mock_list.return_value = {"data": [{"id": "pi_1"}, {"id": "pi_2"}, {"id": "pi_3"}]}
        result = await adapter.reconcile("t1")
        assert result["payments_checked"] == 3
        assert result["provider"] == "stripe"


# ── Payment Router ───────────────────────────────────────────────────


class TestPaymentRouter:
    def test_india_routes_to_razorpay(self):
        provider = get_payment_provider("IN", razorpay_key_id="k", razorpay_key_secret="s")
        from harkeniq_console.billing.razorpay_adapter import RazorpayAdapter
        assert isinstance(provider, RazorpayAdapter)

    def test_us_routes_to_stripe(self):
        provider = get_payment_provider("US", stripe_secret_key="sk", stripe_webhook_secret="wh")
        from harkeniq_console.billing.stripe_adapter import StripeAdapter
        assert isinstance(provider, StripeAdapter)

    def test_eu_routes_to_stripe(self):
        provider = get_payment_provider("DE", stripe_secret_key="sk", stripe_webhook_secret="wh")
        from harkeniq_console.billing.stripe_adapter import StripeAdapter
        assert isinstance(provider, StripeAdapter)

    def test_case_insensitive(self):
        provider = get_payment_provider("in", razorpay_key_id="k", razorpay_key_secret="s")
        from harkeniq_console.billing.razorpay_adapter import RazorpayAdapter
        assert isinstance(provider, RazorpayAdapter)

    def test_unknown_country_routes_to_stripe(self):
        provider = get_payment_provider("JP", stripe_secret_key="sk", stripe_webhook_secret="wh")
        from harkeniq_console.billing.stripe_adapter import StripeAdapter
        assert isinstance(provider, StripeAdapter)
