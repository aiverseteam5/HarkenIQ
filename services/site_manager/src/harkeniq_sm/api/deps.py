"""Shared FastAPI dependencies."""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request


def sm_state(request: Request):
    return request.app.state.sm


async def require_site_token(request: Request) -> None:
    """Bearer auth on /api/*.

    P0 2026-08-29 (final assessment §6): this used to FAIL OPEN — with no
    site token configured, every /api/* route was unauthenticated. Config
    validation requires a token unless ``insecure`` is set, but a config
    path that skipped validation shipped an open HTTP surface. Now: no
    token + secure mode = 503 fail closed; the explicit ``insecure`` lab
    flag is the only bypass, matching CC and Console.
    """
    config = request.app.state.sm.config
    if not config.site_token:
        if getattr(config, "insecure", False):
            return
        raise HTTPException(
            status_code=503,
            detail="site token not configured (fail closed)",
        )
    provided = request.headers.get("authorization", "")
    expected = f"Bearer {config.site_token}"
    if not hmac.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid site token")
