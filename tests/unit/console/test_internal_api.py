"""QA-035: the CC<->Console internal API key, actually enforced.

CC has sent ``Authorization: Bearer <console_api_key>`` since R5-2; the
Console shipped with "No auth (internal network)" and never checked it.
"""

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from harkeniq_console.api.internal import require_internal_key
from harkeniq_console.config import ConsoleConfig
from harkeniq_console.runtime import AppState

KEY = "shared-cc-key"


def make_app(insecure: bool, key: str) -> FastAPI:
    from fastapi import Depends

    app = FastAPI()
    app.state.console = AppState(
        config=ConsoleConfig(insecure=insecure, internal_api_key=key)
    )

    @app.get("/probe", dependencies=[Depends(require_internal_key)])
    async def probe() -> dict:
        return {"ok": True}

    return app


async def call(app: FastAPI, headers: dict | None = None) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get("/probe", headers=headers or {})


class TestInternalKey:
    @pytest.mark.asyncio
    async def test_correct_key_accepted(self):
        resp = await call(
            make_app(False, KEY), {"Authorization": f"Bearer {KEY}"}
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_missing_key_rejected(self):
        assert (await call(make_app(False, KEY))).status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_key_rejected(self):
        resp = await call(
            make_app(False, KEY), {"Authorization": "Bearer wrong"}
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_unconfigured_key_fails_closed(self):
        # Secure mode + no key = 503, never open.
        resp = await call(
            make_app(False, ""), {"Authorization": "Bearer anything"}
        )
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_insecure_mode_allows(self):
        assert (await call(make_app(True, ""))).status_code == 200
