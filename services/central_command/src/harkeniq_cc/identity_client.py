"""A3: Central Command's calls to the identity plane (spec A20).

Keycloak provisioning lives at the Console -- it already creates realms,
roles, clients and owners (E1.4) and holds the only admin credentials in
the platform. Central Command asks over the EXISTING internal channel it
already uses for usage reports and marketplace pulls, so A3 creates no
new trust direction.

The alternative -- giving Central Command its own Keycloak admin
credentials -- would hand a tenant-plane service realm-admin power to
solve a problem the identity plane already solves.

Every function returns a REASON rather than raising, because a failure to
provision must produce an explicable refusal an operator can act on, not
a stack trace at an API boundary.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger("harkeniq.cc.identity_client")

_TIMEOUT = 15.0


def _endpoint(state, path: str) -> tuple[str, dict]:
    console_url = (getattr(state.config, "console_url", "") or "").rstrip("/")
    api_key = getattr(state.config, "console_api_key", "") or ""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return f"{console_url}{path}", headers


async def _post(state, path: str, body: dict) -> tuple[Optional[dict], str]:
    console_url = getattr(state.config, "console_url", "") or ""
    if not console_url:
        return None, "no Console URL is configured, so identities cannot be issued"
    url, headers = _endpoint(state, path)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(url, json=body, headers=headers)
    except Exception as exc:  # noqa: BLE001 -- transport failure is a reason
        logger.warning("identity call %s failed: %s", path, exc)
        return None, f"the Console could not be reached: {type(exc).__name__}: {exc}"
    if response.status_code >= 400:
        detail = ""
        try:
            detail = str(response.json().get("detail", ""))
        except Exception:  # noqa: BLE001
            detail = response.text[:200]
        return None, f"the Console answered {response.status_code}: {detail}"
    return response.json(), ""


async def provision(
    state, *, realm: str, client_id: str
) -> tuple[Optional[dict], str]:
    """Create the service-account client. The secret comes back ONCE."""
    return await _post(
        state, "/api/internal/agent-identities",
        {"realm": realm, "client_id": client_id},
    )


async def rotate(state, *, realm: str, client_id: str) -> tuple[Optional[dict], str]:
    """New secret, same client, same subject — so no second identity."""
    return await _post(
        state, "/api/internal/agent-identities/rotate",
        {"realm": realm, "client_id": client_id},
    )


async def set_enabled(
    state, *, realm: str, client_id: str, enabled: bool
) -> tuple[Optional[dict], str]:
    """Stop Keycloak issuing NEW tokens. CC's row refuses the existing ones."""
    return await _post(
        state, "/api/internal/agent-identities/set-enabled",
        {"realm": realm, "client_id": client_id, "enabled": enabled},
    )


async def report_summary(state, *, tenant_id: str, summary: dict) -> str:
    """A20.9: aggregate counts to the platform plane. Returns "" or a reason.

    Its own endpoint, deliberately NOT `/usage-events` -- that payload
    feeds metering and therefore billing, and an operational signal in a
    billing ingest could corrupt invoicing.
    """
    _, reason = await _post(
        state, "/api/internal/agent-identity-summary",
        {"tenant_id": tenant_id, **summary},
    )
    return reason
