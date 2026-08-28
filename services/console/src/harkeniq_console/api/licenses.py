"""License management endpoints."""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_console.api.deps import (
    get_session,
    require_super_admin,
    require_tenant_permission,
    tenant_scope,
)
from harkeniq_console.auth import UserContext
from harkeniq_console.permissions import has_permission

router = APIRouter(prefix="/api/tenants/{tenant_id}/licenses", tags=["licenses"])
validate_router = APIRouter(prefix="/api/licenses", tags=["licenses"])


class IssueLicenseBody(BaseModel):
    plan: str = "approve"
    node_commit: int = 100
    valid_months: int = 12


class RevokeLicenseBody(BaseModel):
    reason: str


class ValidateBody(BaseModel):
    license_key: str


def _license_dict(lic) -> dict:
    return {
        "id": lic.id,
        "tenant_id": lic.tenant_id,
        "fingerprint": lic.fingerprint,
        "plan": lic.plan,
        "node_commit": lic.node_commit,
        "valid_from": lic.valid_from.isoformat() if lic.valid_from else None,
        "valid_until": lic.valid_until.isoformat() if lic.valid_until else None,
        "status": lic.status,
        "issued_by": lic.issued_by,
        "issued_at": lic.issued_at.isoformat() if lic.issued_at else None,
        "revoked_by": lic.revoked_by,
        "revoked_at": lic.revoked_at.isoformat() if lic.revoked_at else None,
        "revoke_reason": lic.revoke_reason,
    }


@router.post("/")
async def issue_license(
    tenant_id: str,
    body: IssueLicenseBody,
    request: Request,
    user: UserContext = Depends(tenant_scope),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not (user.is_platform_user or has_permission(user.role, "license.manage", user.permissions)):
        raise HTTPException(status_code=403, detail="missing permission: license.manage")

    from harkeniq_console.db.repos import LicenseRepo, SettingsRepo, TenantRepo
    from harkeniq_console.licensing import (
        build_license_payload,
        generate_keypair,
        sign_license,
    )

    tenant = await TenantRepo(session).get_by_id(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="tenant not found")

    # QA-019: a configured key file wins — the signing key belongs in a
    # secret store/file, not the application database. The DB-stored
    # keypair remains the LAB fallback (insecure mode only); secure mode
    # without a key file refuses to mint licenses rather than silently
    # generating the crown jewels into Postgres.
    config = request.app.state.console.config
    key_path = config.license_signing_key_path
    if key_path:
        from pathlib import Path

        try:
            private_key_pem = Path(key_path).read_bytes()
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"license signing key unreadable: {exc}",
            )
    elif config.insecure:
        settings = SettingsRepo(session)
        keypair_setting = await settings.get("license_signing_keypair")
        if keypair_setting and keypair_setting.value:
            private_key_pem = keypair_setting.value["private_key"].encode()
        else:
            private_key_pem, public_key_pem = generate_keypair()
            await settings.set(
                "license_signing_keypair",
                {"private_key": private_key_pem.decode(),
                 "public_key": public_key_pem.decode()},
                updated_by=user.email,
            )
    else:
        raise HTTPException(
            status_code=503,
            detail="license_signing_key_path is not configured "
                   "(refusing to auto-generate a signing key in secure mode)",
        )

    payload = build_license_payload(
        tenant_id=tenant_id, plan=body.plan,
        node_commit=body.node_commit, valid_months=body.valid_months,
    )
    signed = sign_license(private_key_pem, payload)

    # sign_license computes the fingerprint internally; extract it from
    # the signed token rather than the input payload dict.
    import hashlib
    fp_payload = {k: v for k, v in payload.items() if k != "fingerprint"}
    canonical = json.dumps(fp_payload, sort_keys=True, separators=(",", ":")).encode()
    fingerprint = hashlib.sha256(canonical).hexdigest()

    lic = await LicenseRepo(session).create(
        tenant_id=tenant_id,
        license_key=signed,
        fingerprint=fingerprint,
        plan=body.plan,
        node_commit=body.node_commit,
        valid_from=datetime.fromtimestamp(payload["iat"], tz=timezone.utc),
        valid_until=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
        issued_by=user.email,
    )
    await session.commit()
    return _license_dict(lic)


@router.get("/")
async def list_licenses(
    tenant_id: str,
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _user: UserContext = Depends(require_tenant_permission("license.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from harkeniq_console.db.repos import LicenseRepo
    items, total = await LicenseRepo(session).list_by_tenant(
        tenant_id, status=status, page=page, page_size=page_size,
    )
    return {
        "items": [_license_dict(lic) for lic in items],
        "total": total, "page": page, "page_size": page_size,
    }


@router.get("/{license_id}")
async def get_license(
    tenant_id: str, license_id: str,
    _user: UserContext = Depends(require_tenant_permission("license.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from harkeniq_console.db.repos import LicenseRepo
    lic = await LicenseRepo(session).get_by_id(license_id)
    if not lic or lic.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="license not found")
    return _license_dict(lic)


@router.get("/{license_id}/download")
async def download_license(
    tenant_id: str, license_id: str,
    _user: UserContext = Depends(require_tenant_permission("license.view")),
    session: AsyncSession = Depends(get_session),
) -> Response:
    from harkeniq_console.db.repos import LicenseRepo
    lic = await LicenseRepo(session).get_by_id(license_id)
    if not lic or lic.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="license not found")
    return Response(
        content=lic.license_key,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="harkeniq-{lic.fingerprint[:12]}.lic"'},
    )


@router.post("/{license_id}/revoke")
async def revoke_license(
    tenant_id: str, license_id: str,
    body: RevokeLicenseBody,
    user: UserContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from harkeniq_console.db.repos import LicenseRepo
    repo = LicenseRepo(session)
    lic = await repo.get_by_id(license_id)
    if not lic or lic.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="license not found")
    if lic.status == "revoked":
        raise HTTPException(status_code=409, detail="license already revoked")
    await repo.revoke(lic, revoked_by=user.email, reason=body.reason)
    await session.commit()
    return _license_dict(lic)


@validate_router.post("/validate")
async def validate_license(body: ValidateBody) -> dict:
    try:
        parts = body.license_key.split(".")
        if len(parts) != 2:
            return {"valid": False, "errors": ["invalid license format"]}
        padding = "=" * (4 - len(parts[0]) % 4) if len(parts[0]) % 4 else ""
        payload_bytes = base64.urlsafe_b64decode(parts[0] + padding)
        payload = json.loads(payload_bytes)
        errors = []
        if payload.get("exp", 0) < time.time():
            errors.append("license expired")
        return {"valid": len(errors) == 0, "payload": payload, "errors": errors}
    except Exception as exc:
        return {"valid": False, "errors": [str(exc)]}
