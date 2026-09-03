"""Internal service-to-service endpoints.

Called by Central Command (usage snapshots, marketplace install pulls).
QA-035: authenticated by the shared CC<->Console API key — CC has sent
``Authorization: Bearer <console_api_key>`` since R5-2; the Console never
checked it until now. Secure mode with no key configured fails CLOSED.
"""

from __future__ import annotations

import hmac
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_console.api.deps import get_session
from harkeniq_console.billing.metering import MeteringService
from harkeniq_console.db.repos import TenantRepo, UserRepo


async def require_internal_key(request: Request) -> None:
    """QA-035: the CC<->Console credential pair, actually enforced."""
    config = request.app.state.console.config
    if config.insecure:
        return
    expected = getattr(config, "internal_api_key", "")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="internal API key not configured (fail closed)",
        )
    provided = request.headers.get("authorization", "")
    if not hmac.compare_digest(provided, f"Bearer {expected}"):
        raise HTTPException(status_code=401, detail="invalid internal key")


router = APIRouter(
    prefix="/api/internal",
    tags=["internal"],
    dependencies=[Depends(require_internal_key)],
)

_metering = MeteringService()


class UsageEventPayload(BaseModel):
    site_name: str
    date: str
    node_count: int
    agent_versions: dict | None = None


class UsageEventsRequest(BaseModel):
    tenant_id: str
    events: list[UsageEventPayload]


@router.post("/usage-events")
async def ingest_usage_events(
    body: UsageEventsRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    events = [e.model_dump() for e in body.events]
    count = await _metering.ingest_usage_batch(session, body.tenant_id, events)
    await session.commit()
    return {"recorded": count}


@router.get("/marketplace/installs")
async def list_marketplace_installs(
    tenant_id: str,
    since: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """R5-2: install events for a tenant, with the skill payloads.

    Pulled by the tenant's Central Command (CC->Console direction, same
    as usage reporting -- Console never dials CC). `since` is an ISO
    timestamp cursor; CC also dedupes durably on install_id.
    """
    from datetime import datetime

    from harkeniq_console.db.repos import MarketplaceInstallRepo, MarketplaceRepo

    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            since_dt = None
    installs = await MarketplaceInstallRepo(session).list_for_tenant(
        tenant_id, since=since_dt
    )
    marketplace = MarketplaceRepo(session)
    items = []
    for install in installs:
        entry = await marketplace.get_by_id(install.skill_entry_id)
        if entry is None or not entry.published:
            continue  # unpublished/withdrawn skills are never delivered
        items.append({
            "install_id": install.id,
            "installed_at": install.installed_at.isoformat()
            if install.installed_at else None,
            "installed_by": install.installed_by,
            "skill_name": entry.skill_name,
            "skill_version": entry.version,
            "tier": entry.tier,
            "yaml_content": entry.yaml_content,
        })
    return {"installs": items, "tenant_id": tenant_id}


@router.get("/marketplace/skills/{skill_id}")
async def internal_skill_by_id(
    skill_id: str,
    tenant_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """A2: serve one skill's YAML to Central Command by id.

    The fourth piece E0.3 named when it refused skill bindings rather
    than leave them accepted and inert. It rides the EXISTING CC<->Console
    credential pair on this router, so no new trust direction is created:
    Central Command already pulls marketplace installs here.

    Tenant-scoped deliberately. A published skill is readable by any
    tenant; an unpublished one only by the tenant that owns it, matching
    the tenant-identity read on `/api/marketplace/skills/{id}`. An
    internal caller must not become a way around that.
    """
    from harkeniq_console.db.repos import MarketplaceRepo

    entry = await MarketplaceRepo(session).get_by_id(skill_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="skill not found")
    if not entry.published and entry.tenant_id != tenant_id:
        # Same answer as "does not exist": confirming it would leak that
        # another tenant has a skill by this id.
        raise HTTPException(status_code=404, detail="skill not found")
    return {
        "skill_id": entry.id,
        "name": entry.name,
        "version": entry.version,
        "tier": entry.tier,
        "validation_state": entry.validation_state,
        "published": entry.published,
        "yaml_content": entry.yaml_content or "",
    }


# ---------------------------------------------------------------------------
# A3 machine identity (spec A20)
# ---------------------------------------------------------------------------
#
# Keycloak provisioning lives at the Console because the Console is
# already the identity plane -- it creates realms, roles, clients and
# owners (E1.4) and holds the only admin credentials in the platform.
#
# Central Command asks over the EXISTING internal channel it already uses
# for usage and marketplace pulls, so no new trust direction is created.
# The alternative -- giving Central Command its own Keycloak admin
# credentials -- would hand a tenant-plane service realm-admin power to
# solve a problem the identity plane already solves.
#
# The OPERATOR-facing surface stays at Central Command, where the agent
# lives and where site.manage and the E1.2 delegation ceiling are
# enforced. These endpoints are plumbing, not policy.


class ProvisionIdentityRequest(BaseModel):
    realm: str
    client_id: str


@router.post("/agent-identities")
async def provision_agent_identity(
    body: ProvisionIdentityRequest,
    request: Request,
) -> dict:
    """Create a service-account client. Returns the secret ONCE."""
    keycloak = getattr(request.app.state.console, "keycloak_admin", None)
    if keycloak is None:
        raise HTTPException(
            status_code=503, detail="keycloak admin is not configured",
        )
    try:
        uuid, secret = await keycloak.create_service_account_client(
            body.realm, body.client_id,
        )
        subject = await keycloak.get_service_account_subject(body.realm, uuid)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc
    return {
        "client_uuid": uuid,
        "client_id": body.client_id,
        "subject": subject,
        # Shown once, here, and never stored at Central Command.
        "secret": secret,
    }


class RotateIdentityRequest(BaseModel):
    realm: str
    client_id: str


@router.post("/agent-identities/rotate")
async def rotate_agent_identity(
    body: RotateIdentityRequest, request: Request,
) -> dict:
    """New secret, SAME client and SAME service-account subject.

    Rotation must never mint a second identity: one client, one subject,
    one row. Only the secret changes, so tokens already issued stay valid
    to their natural expiry and there is no execution gap.
    """
    keycloak = getattr(request.app.state.console, "keycloak_admin", None)
    if keycloak is None:
        raise HTTPException(503, "keycloak admin is not configured")
    uuid = await keycloak.find_client_uuid(body.realm, body.client_id)
    if not uuid:
        raise HTTPException(404, "service account client not found")
    try:
        secret = await keycloak.regenerate_client_secret(body.realm, uuid)
        subject = await keycloak.get_service_account_subject(body.realm, uuid)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"{type(exc).__name__}: {exc}") from exc
    return {"client_id": body.client_id, "subject": subject, "secret": secret}


class DisableIdentityRequest(BaseModel):
    realm: str
    client_id: str
    enabled: bool = False


@router.post("/agent-identities/set-enabled")
async def set_agent_identity_enabled(
    body: DisableIdentityRequest, request: Request,
) -> dict:
    """Stop Keycloak issuing NEW tokens.

    Not the authoritative revocation: Central Command's own status row
    refuses the tokens already issued, which is what makes revocation
    immediate rather than bounded by a token lifetime.
    """
    keycloak = getattr(request.app.state.console, "keycloak_admin", None)
    if keycloak is None:
        raise HTTPException(503, "keycloak admin is not configured")
    uuid = await keycloak.find_client_uuid(body.realm, body.client_id)
    if not uuid:
        # Already gone is the desired end state for a disable.
        return {"client_id": body.client_id, "enabled": body.enabled,
                "detail": "client not found; nothing to disable"}
    await keycloak.set_client_enabled(body.realm, uuid, body.enabled)
    return {"client_id": body.client_id, "enabled": body.enabled}


class IdentitySummaryRequest(BaseModel):
    """A20.9: aggregate operational visibility. COUNTS ONLY.

    Deliberately has no field that could carry an agent id, name, client
    id or subject. A12.1 is not amended: platform and vendor staff get no
    live tenant-plane identity access, and the shape of this payload is
    what makes that true rather than a promise about it.
    """

    tenant_id: str
    identities: int = 0
    active: int = 0
    revoked: int = 0
    retired: int = 0
    ever_seen: int = 0
    never_seen: int = 0
    most_recent_seen_at: str | None = None


@router.post("/agent-identity-summary")
async def ingest_agent_identity_summary(
    body: IdentitySummaryRequest, request: Request,
) -> dict:
    """Aggregate operational signal — NOT a metering event.

    Deliberately a separate endpoint from `/usage-events`, which feeds
    `MeteringService.ingest_usage_batch` and therefore billing. Mixing a
    non-billing operational signal into a billing ingest is a category
    error that could corrupt invoicing, so the channel is reused and the
    payload is not.

    Held in memory on the app state: this is an operational reading, and
    persisting per-tenant identity history at the platform plane would
    start to look like the per-agent visibility A12.1 forbids.
    """
    console = request.app.state.console
    store = getattr(console, "agent_identity_summaries", None)
    if store is None:
        store = {}
        console.agent_identity_summaries = store
    payload = body.model_dump()
    payload["received_at"] = datetime.now(timezone.utc).isoformat()
    store[body.tenant_id] = payload
    return {"accepted": True, "tenant_id": body.tenant_id}


@router.get("/tenants/by-realm/{realm}/owners")
async def tenant_owners_by_realm(
    realm: str, session: AsyncSession = Depends(get_session),
) -> dict:
    """A23-5: who administers the tenant that owns this realm (A23.14 D4).

    Central Command seeds its tenant's FIRST administrative grant and
    needs the owner's Keycloak SUBJECT to do it. The subject exists only
    here -- `users.keycloak_user_id`, written when the Console minted the
    owner -- so CC asks over the channel it already uses for marketplace
    pulls and agent identities. No new trust direction, and no Keycloak
    admin credential leaves the identity plane.

    Resolution is by REALM, not by slug and not by the Console's tenant
    id: E1.4 made `tenants.keycloak_realm` the authoritative unique
    binding, and it is the one identifier CC and the Console agree on
    (CC's `tenant_id` is the realm name, never the Console row id).

    Owners are returned with a subject or not at all. An owner row whose
    `keycloak_user_id` is NULL cannot be granted to -- a grant keyed on
    an email is not an authorization, it is a guess -- so it is omitted
    and CC reports the tenant unadministered rather than seeding
    something it cannot authenticate.
    """
    tenant = await TenantRepo(session).get_by_realm(realm)
    if tenant is None:
        raise HTTPException(status_code=404, detail="unknown realm")
    users, _ = await UserRepo(session).list_by_tenant(
        tenant.id, role="tenant_owner", page_size=200,
    )
    return {
        "tenant_id": tenant.id,
        "slug": tenant.slug,
        "keycloak_realm": tenant.keycloak_realm,
        "status": tenant.status,
        "owners": [
            {
                "keycloak_user_id": u.keycloak_user_id,
                "email": u.email,
                "status": u.status,
            }
            for u in users
            if u.keycloak_user_id
        ],
    }
