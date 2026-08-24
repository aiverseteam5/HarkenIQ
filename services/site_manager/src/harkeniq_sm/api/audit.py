"""SM audit endpoints: listing + hash-chain verification (R4-2 P12)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from harkeniq_sm.api.deps import require_site_token
from harkeniq_sm.db.repos import AuditRepo

router = APIRouter(
    prefix="/api/audit", dependencies=[Depends(require_site_token)]
)


@router.get("")
async def list_audit(request: Request) -> dict:
    state = request.app.state.sm
    async with state.sessionmaker() as session:
        rows = await AuditRepo(session).list_all()
        return {
            "entries": [
                {
                    "id": r.id,
                    "ts": r.ts.isoformat() if r.ts else None,
                    "actor": r.actor,
                    "action": r.action,
                    "subject": r.subject,
                    "detail": r.detail,
                    "seq": r.seq,
                    "prev_hash": r.prev_hash,
                    "entry_hash": r.entry_hash,
                }
                for r in rows
            ]
        }


@router.get("/verify")
async def verify_audit_chain(request: Request) -> dict:
    """Verify the SHA-256 audit hash chain (R4-2 P12, OQ-20: on-demand)."""
    state = request.app.state.sm
    async with state.sessionmaker() as session:
        result = await AuditRepo(session).verify_chain()
        return {
            "valid": result.valid,
            "length": result.length,
            "first_bad_seq": result.first_bad_seq,
            "error": result.error,
        }
