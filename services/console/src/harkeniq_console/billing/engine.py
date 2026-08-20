"""Billing engine — invoice generation, credit notes, delinquency checks.

All methods are stubs for Phase 2.
"""

from __future__ import annotations


class BillingEngine:
    """Core billing operations."""

    async def generate_overage_invoice(self, tenant_id: str, period: str) -> dict:
        """Generate a true-up invoice for usage above the node commit."""
        raise NotImplementedError("BillingEngine.generate_overage_invoice — Phase 2")

    async def generate_commit_invoice(self, tenant_id: str, period: str) -> dict:
        """Generate the annual node-commit invoice."""
        raise NotImplementedError("BillingEngine.generate_commit_invoice — Phase 2")

    async def apply_credit_note(self, invoice_id: str, amount: int, reason: str) -> dict:
        """Apply a credit note against an invoice."""
        raise NotImplementedError("BillingEngine.apply_credit_note — Phase 2")

    async def check_delinquency(self, tenant_id: str) -> dict:
        """Evaluate delinquency state: grace -> restrict -> manual suspend."""
        raise NotImplementedError("BillingEngine.check_delinquency — Phase 2")
