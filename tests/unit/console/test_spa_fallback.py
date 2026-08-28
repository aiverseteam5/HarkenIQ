"""Regression: ISSUE-001 — Console SPA deep links returned JSON 404.

Found by /qa on 2026-08-26 (report: .gstack/qa-reports/qa-report-localhost-2026-08-26.md).
Plain StaticFiles(html=True) resolves index.html only at "/", so the OIDC
redirect to /callback rendered {"detail":"Not Found"} and login dead-ended
in the browser. The SPA mount must fall back to index.html for any
non-API, non-file path — while API routes and real assets keep winning.
"""

import httpx
import pytest

from harkeniq_console.app import create_app
from harkeniq_console.config import ConsoleConfig
from harkeniq_console.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_console.runtime import AppState


@pytest.fixture
async def client(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body>SPA SHELL</body></html>")
    (dist / "app.js").write_text("console.log('bundle')")

    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sm = make_sessionmaker(engine)
    config = ConsoleConfig(insecure=True, ui_dist=str(dist))
    state = AppState(config=config, engine=engine, sessionmaker=sm)
    app = create_app(state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as c:
        yield c
    await engine.dispose()


async def test_root_serves_index(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "SPA SHELL" in resp.text


async def test_oidc_callback_serves_index(client):
    # The exact dead-end: Keycloak redirects here after login.
    resp = await client.get("/callback?code=abc&session_state=xyz")
    assert resp.status_code == 200
    assert "SPA SHELL" in resp.text


async def test_deep_route_refresh_serves_index(client):
    resp = await client.get("/fleet/some-device-id")
    assert resp.status_code == 200
    assert "SPA SHELL" in resp.text


async def test_real_assets_still_served(client):
    resp = await client.get("/app.js")
    assert resp.status_code == 200
    assert "bundle" in resp.text


async def test_api_routes_still_win(client):
    # API 404s must stay JSON 404s, never the SPA shell.
    resp = await client.get("/api/definitely-not-a-route")
    assert resp.status_code in (401, 404)
    assert "SPA SHELL" not in resp.text
