"""Shared FastAPI dependencies."""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request


def sm_state(request: Request):
    return request.app.state.sm


async def require_site_token(request: Request) -> None:
    """Bearer auth on /api/* when a site token is configured."""
    config = request.app.state.sm.config
    if not config.site_token:
        return
    provided = request.headers.get("authorization", "")
    expected = f"Bearer {config.site_token}"
    if not hmac.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid site token")
