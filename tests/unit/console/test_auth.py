"""Console auth: insecure mode, token extraction, error paths."""

import asyncio

import httpx
import pytest
from fastapi import FastAPI, Request

from harkeniq_console.auth import UserContext, _INSECURE_CONTEXT, get_current_user
from harkeniq_console.permissions import PERMISSIONS


class TestUserContext:
    def test_fields(self):
        ctx = UserContext(
            user_id="u1", email="alice@acme.com",
            tenant_id="t1", role="operator",
        )
        assert ctx.user_id == "u1"
        assert ctx.email == "alice@acme.com"
        assert ctx.tenant_id == "t1"
        assert ctx.role == "operator"
        assert ctx.permissions == []
        assert ctx.is_platform_user is False

    def test_platform_user(self):
        ctx = UserContext(
            user_id="u2", email="admin@harkeniq.com",
            tenant_id=None, role="platform_super_admin",
            is_platform_user=True,
        )
        assert ctx.is_platform_user is True
        assert ctx.tenant_id is None


class TestInsecureContext:
    def test_insecure_mock_user(self):
        ctx = _INSECURE_CONTEXT
        assert ctx.user_id == "insecure-dev"
        assert ctx.email == "dev@harkeniq.local"
        assert ctx.role == "platform_super_admin"
        assert ctx.is_platform_user is True

    def test_mock_user_is_platform_super_admin(self):
        """In insecure mode the mock user should be platform_super_admin."""
        ctx = _INSECURE_CONTEXT
        assert ctx.role == "platform_super_admin"


class TestGetCurrentUser:
    """Integration tests via a minimal FastAPI app."""

    @pytest.fixture
    def insecure_app(self):
        from harkeniq_console.config import ConsoleConfig
        from harkeniq_console.runtime import AppState

        app = FastAPI()
        state = AppState(config=ConsoleConfig(insecure=True))
        app.state.console = state

        @app.get("/me")
        async def me(request: Request):
            ctx = await get_current_user(request)
            return {"user_id": ctx.user_id, "role": ctx.role}

        return app

    @pytest.fixture
    def secure_app(self):
        from harkeniq_console.config import ConsoleConfig
        from harkeniq_console.runtime import AppState

        app = FastAPI()
        state = AppState(config=ConsoleConfig(insecure=False))
        app.state.console = state

        @app.get("/me")
        async def me(request: Request):
            ctx = await get_current_user(request)
            return {"user_id": ctx.user_id, "role": ctx.role}

        return app

    async def test_insecure_returns_mock(self, insecure_app):
        from httpx import ASGITransport
        async with httpx.AsyncClient(
            transport=ASGITransport(app=insecure_app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/me")
            assert resp.status_code == 200
            data = resp.json()
            assert data["user_id"] == "insecure-dev"
            assert data["role"] == "platform_super_admin"

    async def test_missing_token_returns_401(self, secure_app):
        from httpx import ASGITransport
        async with httpx.AsyncClient(
            transport=ASGITransport(app=secure_app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/me")
            assert resp.status_code == 401

    async def test_invalid_token_returns_error(self, secure_app):
        from httpx import ASGITransport
        async with httpx.AsyncClient(
            transport=ASGITransport(app=secure_app),
            base_url="http://test",
        ) as client:
            resp = await client.get(
                "/me", headers={"Authorization": "Bearer bad-token"},
            )
            # Until JWT validation is implemented, secure mode returns 501
            assert resp.status_code == 501

    async def test_non_bearer_header_returns_401(self, secure_app):
        from httpx import ASGITransport
        async with httpx.AsyncClient(
            transport=ASGITransport(app=secure_app),
            base_url="http://test",
        ) as client:
            resp = await client.get(
                "/me", headers={"Authorization": "Basic dXNlcjpwYXNz"},
            )
            assert resp.status_code == 401

    async def test_bearer_token_extraction(self, secure_app):
        """Bearer prefix is stripped before passing to validation."""
        from httpx import ASGITransport
        async with httpx.AsyncClient(
            transport=ASGITransport(app=secure_app),
            base_url="http://test",
        ) as client:
            # With a bearer token, we should get 501 (not 401)
            # because the token was extracted but JWT validation is stubbed
            resp = await client.get(
                "/me",
                headers={"Authorization": "Bearer eyJhbGciOiJSUzI1NiJ9.test.sig"},
            )
            assert resp.status_code == 501
