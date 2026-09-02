"""Identity evidence: the email<->subject pairs the platform itself wrote.

A23-2 named `actor_of` as the one way to derive a stable identity from a
representation, and it deliberately refuses an email address: an
address cannot be turned into a subject without the identity provider.
This module is the one place that DOES pair the two -- from records
where the subject and the address were observed together on a single
authenticated request -- so the enforcement-impact census (A22.10) and
the scope loader's prior-grant evidence (A23-4) resolve an alias the
same way, and nothing else guesses.
"""

from __future__ import annotations

from typing import Any


async def identity_evidence(session: Any, tenant_id: str) -> dict[str, str]:
    """Email -> stable subject, from records the platform itself wrote.

    Two stores pair an address with a subject at write time:
    `cc_approval_records` (approver_ref + approver_email, E0.1) and
    `cc_approval_group_members` (principal_ref + user_email). Those pairs
    are evidence, not inference: the subject and the address were
    observed together on one authenticated request. Nothing else is
    consulted, and an address with no such pair stays unresolved.

    ONE implementation (A23-2, A23-4): the enforcement-impact census
    resolves observed audit actors through it, and the scope loader
    asks it, reversed, for the aliases a subject has been recorded under
    so that a grant keyed by one of them counts as prior evidence.
    """
    from sqlalchemy import select as _select

    from harkeniq_cc.db.models import (
        CCApprovalGroup,
        CCApprovalGroupMember,
        CCApprovalRecord,
    )

    out: dict[str, str] = {}
    rows = (await session.execute(
        _select(CCApprovalRecord.approver_email, CCApprovalRecord.approver_ref)
        .where(CCApprovalRecord.tenant_id == tenant_id)
        .distinct()
    )).all()
    rows += (await session.execute(
        _select(CCApprovalGroupMember.user_email, CCApprovalGroupMember.principal_ref)
        .join(CCApprovalGroup, CCApprovalGroup.id == CCApprovalGroupMember.group_id)
        .where(CCApprovalGroup.tenant_id == tenant_id)
        .distinct()
    )).all()
    for email, ref in rows:
        email = (email or "").strip().lower()
        ref = (ref or "").strip()
        if email and ref and "@" in email and "@" not in ref:
            out.setdefault(email, ref)
    return out


async def subject_aliases(session: Any, tenant_id: str, subject: str) -> set[str]:
    """The email addresses the platform has paired with `subject`.

    The reverse question of :func:`identity_evidence`, from the same
    pairs. Exact, recorded pairs only -- never a guess.
    """
    subject = (subject or "").strip()
    if not subject:
        return set()
    pairs = await identity_evidence(session, tenant_id)
    return {email for email, ref in pairs.items() if ref == subject}
