"""Tenant service placement: which L1-L3 stack serves which tenant.

The Console proxied every infrastructure surface to one global
``config.cc_url``, so a per-tenant URL would have promised a boundary the
backend did not keep — every tenant saw the same Central Command. Placement
now resolves through ``tenant_services``, and the property that matters is
that resolution is **fail-closed**: an unregistered tenant is refused, never
quietly handed a shared endpoint.
"""

import httpx
import pytest

from harkeniq_console.app import create_app
from harkeniq_console.auth import UserContext, get_current_user
from harkeniq_console.config import ConsoleConfig
from harkeniq_console.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_console.db.models import Tenant
from harkeniq_console.db.repos import TenantServiceRepo
from harkeniq_console.runtime import AppState, seed_service_placement


async def _stack(role: str = "platform_super_admin", *, tenants: int = 1,
                 cc_url: str = ""):
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sm = make_sessionmaker(engine)
    ids = []
    async with sm() as session:
        for i in range(tenants):
            t = Tenant(name=f"T{i}", slug=f"t{i}", billing_country="US")
            session.add(t)
            await session.flush()
            ids.append(t.id)
        await session.commit()

    config = ConsoleConfig(insecure=False, cc_url=cc_url)
    state = AppState(config=config, engine=engine, sessionmaker=sm)
    app = create_app(state)

    async def _fake_user() -> UserContext:
        return UserContext(
            user_id="kc-sub-1", email=f"{role}@example.com",
            tenant_id=None, role=role, permissions=[], is_platform_user=True,
        )

    app.dependency_overrides[get_current_user] = _fake_user
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        follow_redirects=True,
    )
    return client, engine, sm, state, ids


class TestResolutionIsFailClosed:
    async def test_unregistered_tenant_is_refused_not_defaulted(self):
        """The whole point: no placement means refuse, never fall back."""
        client, engine, _sm, _state, ids = await _stack(
            cc_url="http://should-never-be-used:9999",
        )
        try:
            resp = await client.get(f"/api/t/{ids[0]}/fleet/summary")
            assert resp.status_code == 503
            assert "no central_command registered" in resp.json()["detail"]
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_resolve_returns_none_for_unknown_tenant(self):
        _client, engine, sm, _state, _ids = await _stack()
        try:
            async with sm() as session:
                assert await TenantServiceRepo(session).resolve(
                    "no-such-tenant", "central_command",
                ) is None
        finally:
            await _client.aclose()
            await engine.dispose()

    async def test_one_tenants_placement_never_answers_for_another(self):
        """Tenant A registered, tenant B not. B must not inherit A's stack."""
        client, engine, sm, _state, ids = await _stack(tenants=2)
        try:
            async with sm() as session:
                await TenantServiceRepo(session).register(
                    tenant_id=ids[0], service_kind="central_command",
                    endpoint_url="http://cc-a.internal",
                )
                await session.commit()

                repo = TenantServiceRepo(session)
                a = await repo.resolve(ids[0], "central_command")
                b = await repo.resolve(ids[1], "central_command")
                assert a is not None and a.endpoint_url == "http://cc-a.internal"
                assert b is None

            resp = await client.get(f"/api/t/{ids[1]}/fleet/summary")
            assert resp.status_code == 503
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_disabled_placement_does_not_resolve(self):
        _client, engine, sm, _state, ids = await _stack()
        try:
            async with sm() as session:
                repo = TenantServiceRepo(session)
                row = await repo.register(
                    tenant_id=ids[0], service_kind="central_command",
                    endpoint_url="http://cc.internal",
                )
                await repo.disable(row)
                await session.commit()
                assert await repo.resolve(ids[0], "central_command") is None
        finally:
            await _client.aclose()
            await engine.dispose()

    async def test_reregister_retires_the_previous_placement(self):
        """Moving a tenant to a new stack keeps the old row as history."""
        _client, engine, sm, _state, ids = await _stack()
        try:
            async with sm() as session:
                repo = TenantServiceRepo(session)
                await repo.register(
                    tenant_id=ids[0], service_kind="central_command",
                    endpoint_url="http://old.internal",
                )
                await repo.register(
                    tenant_id=ids[0], service_kind="central_command",
                    endpoint_url="http://new.internal",
                )
                await session.commit()

                active = await repo.resolve(ids[0], "central_command")
                assert active.endpoint_url == "http://new.internal"
                assert len(await repo.list_by_tenant(ids[0])) == 2
        finally:
            await _client.aclose()
            await engine.dispose()


class TestProxyIsAuthorizedBeforeItRoutes:
    async def test_support_without_a_grant_never_reaches_the_stack(self):
        """Infrastructure pages are behind the same gate as everything else.

        The refusal must be the 403 from tenant_scope, not the 503 that
        would mean we got as far as looking for a placement.
        """
        client, engine, sm, _state, ids = await _stack(role="platform_support")
        try:
            async with sm() as session:
                await TenantServiceRepo(session).register(
                    tenant_id=ids[0], service_kind="central_command",
                    endpoint_url="http://cc.internal",
                )
                await session.commit()

            resp = await client.get(f"/api/t/{ids[0]}/fleet/summary")
            assert resp.status_code == 403
            assert "support access" in resp.json()["detail"]
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_tenant_user_cannot_proxy_into_another_tenant(self):
        client, engine, _sm, _state, ids = await _stack(tenants=2)
        try:
            app = client._transport.app  # type: ignore[attr-defined]

            async def _tenant_user() -> UserContext:
                return UserContext(
                    user_id="u1", email="v@t0.example", tenant_id=ids[0],
                    role="tenant_owner", permissions=[], is_platform_user=False,
                )

            app.dependency_overrides[get_current_user] = _tenant_user
            resp = await client.get(f"/api/t/{ids[1]}/fleet/summary")
            assert resp.status_code == 403
            assert "tenant scope mismatch" in resp.json()["detail"]
        finally:
            await client.aclose()
            await engine.dispose()


class TestSeeding:
    async def test_single_tenant_install_is_seeded_explicitly(self):
        """cc_url becomes a real row, not an implicit request-time default."""
        _client, engine, sm, state, ids = await _stack(
            cc_url="http://cc.demo:8090",
        )
        try:
            await seed_service_placement(state)
            async with sm() as session:
                row = await TenantServiceRepo(session).resolve(
                    ids[0], "central_command",
                )
            assert row is not None
            assert row.endpoint_url == "http://cc.demo:8090"
            assert row.registered_by == "startup-seed"
        finally:
            await _client.aclose()
            await engine.dispose()

    async def test_multi_tenant_install_is_never_guessed(self):
        """A lone cc_url must not be assigned to an arbitrary tenant."""
        _client, engine, sm, state, ids = await _stack(
            tenants=3, cc_url="http://cc.demo:8090",
        )
        try:
            await seed_service_placement(state)
            async with sm() as session:
                repo = TenantServiceRepo(session)
                for tid in ids:
                    assert await repo.resolve(tid, "central_command") is None
        finally:
            await _client.aclose()
            await engine.dispose()

    async def test_seeding_never_blocks_boot(self):
        """Seeding is a convenience; the registry is fail-closed anyway.

        Refusing to start because the table is not migrated yet would turn a
        missing nicety into an outage.
        """
        _client, engine, _sm, state, _ids = await _stack(cc_url="http://cc:8090")
        try:
            async with engine.begin() as conn:
                await conn.exec_driver_sql("DROP TABLE tenant_services")
            # Must not raise.
            await seed_service_placement(state)
        finally:
            await _client.aclose()
            await engine.dispose()

    async def test_seeding_is_idempotent(self):
        _client, engine, sm, state, ids = await _stack(cc_url="http://cc:8090")
        try:
            await seed_service_placement(state)
            await seed_service_placement(state)
            async with sm() as session:
                rows = await TenantServiceRepo(session).list_by_tenant(ids[0])
            assert len(rows) == 1
        finally:
            await _client.aclose()
            await engine.dispose()


class TestPlacementApi:
    async def test_register_and_list_and_disable(self):
        client, engine, _sm, _state, ids = await _stack()
        try:
            created = await client.post(
                f"/api/admin/tenant-services/{ids[0]}",
                json={
                    "service_kind": "central_command",
                    "endpoint_url": "http://cc-acme.internal",
                },
            )
            assert created.status_code == 200
            sid = created.json()["id"]

            listed = await client.get(f"/api/admin/tenant-services/{ids[0]}")
            assert listed.status_code == 200
            assert listed.json()["items"][0]["endpoint_url"] == "http://cc-acme.internal"

            disabled = await client.post(
                f"/api/admin/tenant-services/{ids[0]}/{sid}/disable",
            )
            assert disabled.status_code == 200
            assert disabled.json()["status"] == "disabled"
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_unknown_service_kind_is_rejected(self):
        client, engine, _sm, _state, ids = await _stack()
        try:
            resp = await client.post(
                f"/api/admin/tenant-services/{ids[0]}",
                json={"service_kind": "nonsense", "endpoint_url": "http://x"},
            )
            assert resp.status_code == 400
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_registering_for_an_unknown_tenant_404s(self):
        client, engine, _sm, _state, _ids = await _stack()
        try:
            resp = await client.post(
                "/api/admin/tenant-services/no-such-tenant",
                json={
                    "service_kind": "central_command",
                    "endpoint_url": "http://x",
                },
            )
            assert resp.status_code == 404
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_support_may_read_placements_but_not_write(self):
        """tenant.view lists; tenant.manage registers. platform_support has
        only the former."""
        client, engine, _sm, _state, ids = await _stack(role="platform_support")
        try:
            assert (
                await client.get(f"/api/admin/tenant-services/{ids[0]}")
            ).status_code == 200
            resp = await client.post(
                f"/api/admin/tenant-services/{ids[0]}",
                json={
                    "service_kind": "central_command",
                    "endpoint_url": "http://x",
                },
            )
            assert resp.status_code == 403
        finally:
            await client.aclose()
            await engine.dispose()


class TestPlatformPlaneGating:
    """Review finding (4 passes): tenant.view is shared vocabulary —
    tenant_owner holds it — so a bare permission check leaked the registry
    to customers. Platform-plane reads need platform credentials too."""

    async def test_tenant_owner_cannot_read_placements(self):
        client, engine, _sm, _state, ids = await _stack()
        try:
            app = client._transport.app  # type: ignore[attr-defined]

            async def _owner() -> UserContext:
                return UserContext(
                    user_id="u1", email="o@t0.example", tenant_id=ids[0],
                    role="tenant_owner", permissions=[], is_platform_user=False,
                )

            app.dependency_overrides[get_current_user] = _owner
            resp = await client.get(f"/api/admin/tenant-services/{ids[0]}")
            assert resp.status_code == 403
            assert "platform credentials" in resp.json()["detail"]
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_platform_support_reads_but_cannot_write(self):
        client, engine, _sm, _state, ids = await _stack(role="platform_support")
        try:
            assert (
                await client.get(f"/api/admin/tenant-services/{ids[0]}")
            ).status_code == 200
            resp = await client.post(
                f"/api/admin/tenant-services/{ids[0]}",
                json={"service_kind": "central_command",
                      "endpoint_url": "http://x.internal"},
            )
            assert resp.status_code == 403
        finally:
            await client.aclose()
            await engine.dispose()


class TestOneTenantOneCC:
    """Decided by Vinod 2026-08-28: one tenant -> one CC is the invariant.
    CC has no per-tenant filtering, so a shared endpoint silently serves
    one tenant's data under another tenant's URL."""

    async def test_endpoint_bound_to_another_tenant_is_refused(self):
        client, engine, _sm, _state, ids = await _stack(tenants=2)
        try:
            first = await client.post(
                f"/api/admin/tenant-services/{ids[0]}",
                json={"service_kind": "central_command",
                      "endpoint_url": "http://cc-shared.internal"},
            )
            assert first.status_code == 200
            second = await client.post(
                f"/api/admin/tenant-services/{ids[1]}",
                json={"service_kind": "central_command",
                      "endpoint_url": "http://cc-shared.internal"},
            )
            assert second.status_code == 409
            assert "another tenant" in second.json()["detail"]
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_same_tenant_may_rebind_its_own_endpoint(self):
        client, engine, _sm, _state, ids = await _stack()
        try:
            for _ in range(2):
                resp = await client.post(
                    f"/api/admin/tenant-services/{ids[0]}",
                    json={"service_kind": "central_command",
                          "endpoint_url": "http://cc-a.internal"},
                )
                assert resp.status_code == 200
        finally:
            await client.aclose()
            await engine.dispose()


class TestEndpointValidation:
    """The proxy forwards the caller's bearer token to this URL — an
    arbitrary string is an SSRF/exfiltration edge even super-admin-gated."""

    async def test_bad_endpoints_are_refused(self):
        client, engine, _sm, _state, ids = await _stack()
        try:
            for bad in ("ftp://cc.internal", "not-a-url",
                        "http://user:pw@cc.internal"):
                resp = await client.post(
                    f"/api/admin/tenant-services/{ids[0]}",
                    json={"service_kind": "central_command",
                          "endpoint_url": bad},
                )
                assert resp.status_code == 400, f"{bad} was accepted"
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_unknown_tenant_listing_is_404_not_empty(self):
        """"No placements" and "no such tenant" must not look identical."""
        client, engine, _sm, _state, _ids = await _stack()
        try:
            resp = await client.get("/api/admin/tenant-services/no-such-tenant")
            assert resp.status_code == 404
        finally:
            await client.aclose()
            await engine.dispose()


class TestProxyForwardsFaithfully:
    """Refusal tests alone can go green while the forwarding silently drops
    params, bodies, or headers (the proto-to-dict lesson). Pin the hop."""

    async def test_method_params_body_and_headers(self):
        import httpx as _httpx

        seen = {}

        def _capture(req: _httpx.Request) -> _httpx.Response:
            seen["method"] = req.method
            seen["url"] = str(req.url)
            seen["auth"] = req.headers.get("authorization")
            seen["cookie"] = req.headers.get("cookie")
            seen["body"] = req.content
            return _httpx.Response(200, json={"ok": True})

        client, engine, sm, state, ids = await _stack()
        try:
            async with sm() as session:
                await TenantServiceRepo(session).register(
                    tenant_id=ids[0], service_kind="central_command",
                    endpoint_url="http://cc-upstream.internal",
                )
                await session.commit()

            # Rebuild the app with the transport seam installed.
            state.cc_transport = _httpx.MockTransport(_capture)
            app = create_app(state)

            async def _fake_user() -> UserContext:
                return UserContext(
                    user_id="kc-1", email="a@example.com", tenant_id=None,
                    role="platform_super_admin", permissions=[],
                    is_platform_user=True,
                )

            app.dependency_overrides[get_current_user] = _fake_user
            seamed = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
            )
            resp = await seamed.post(
                f"/api/t/{ids[0]}/approvals/abc/approve?force=1",
                json={"actor": "op"},
                headers={
                    "authorization": "Bearer tok-123",
                    "cookie": "session=nope",
                },
            )
            await seamed.aclose()

            assert resp.status_code == 200
            assert seen["method"] == "POST"
            assert seen["url"] == (
                "http://cc-upstream.internal/api/approvals/abc/approve?force=1"
            )
            assert seen["auth"] == "Bearer tok-123"
            assert seen["cookie"] is None, "cookies must not cross the proxy"
            assert b'"actor"' in seen["body"]
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_learning_prefix_is_proxied(self):
        """S1 (2026-08-29): /api/learning/* existed at CC with NO consumer —
        it was missing from _CC_PREFIXES, so the R-C1 learning loop ran
        headless and unreachable from the product. Pin the route."""
        import httpx as _httpx

        seen = {}

        def _capture(req: _httpx.Request) -> _httpx.Response:
            seen["url"] = str(req.url)
            return _httpx.Response(200, json={"candidates": []})

        client, engine, sm, state, ids = await _stack()
        try:
            async with sm() as session:
                await TenantServiceRepo(session).register(
                    tenant_id=ids[0], service_kind="central_command",
                    endpoint_url="http://cc-upstream.internal",
                )
                await session.commit()

            state.cc_transport = _httpx.MockTransport(_capture)
            app = create_app(state)

            async def _fake_user() -> UserContext:
                return UserContext(
                    user_id="kc-1", email="a@example.com", tenant_id=None,
                    role="platform_super_admin", permissions=[],
                    is_platform_user=True,
                )

            app.dependency_overrides[get_current_user] = _fake_user
            seamed = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
            )
            resp = await seamed.get(f"/api/t/{ids[0]}/learning/candidates")
            await seamed.aclose()

            assert resp.status_code == 200
            assert seen["url"] == (
                "http://cc-upstream.internal/api/learning/candidates"
            )
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_attention_prefix_is_proxied(self):
        """S2: the attention capability must be reachable on the path the
        browser actually uses. One contract, many consumers — the Console
        is the first, and its route has to exist."""
        import httpx as _httpx

        seen = {}

        def _capture(req: _httpx.Request) -> _httpx.Response:
            seen["url"] = str(req.url)
            return _httpx.Response(200, json={"items": [], "sites": []})

        client, engine, sm, state, ids = await _stack()
        try:
            async with sm() as session:
                await TenantServiceRepo(session).register(
                    tenant_id=ids[0], service_kind="central_command",
                    endpoint_url="http://cc-upstream.internal",
                )
                await session.commit()

            state.cc_transport = _httpx.MockTransport(_capture)
            app = create_app(state)

            async def _fake_user() -> UserContext:
                return UserContext(
                    user_id="kc-1", email="a@example.com", tenant_id=None,
                    role="platform_super_admin", permissions=[],
                    is_platform_user=True,
                )

            app.dependency_overrides[get_current_user] = _fake_user
            seamed = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
            )
            resp = await seamed.get(
                f"/api/t/{ids[0]}/attention?site_id=s-7"
            )
            await seamed.aclose()

            assert resp.status_code == 200
            # Empty `rest` maps to CC's canonical trailing-slash form (the
            # proxy does this deliberately to avoid a redirect loop), and
            # the site scope must survive the hop — otherwise a scoped
            # consumer silently receives the whole tenant.
            assert seen["url"] == (
                "http://cc-upstream.internal/api/attention/?site_id=s-7"
            )
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_schema_skew_degrades_to_503_not_500(self):
        """New binary + unmigrated DB must fail closed, not crash open."""
        client, engine, sm, _state, ids = await _stack()
        try:
            async with engine.begin() as conn:
                await conn.exec_driver_sql("DROP TABLE tenant_services")
            resp = await client.get(f"/api/t/{ids[0]}/fleet/summary")
            assert resp.status_code == 503
        finally:
            await client.aclose()
            await engine.dispose()
