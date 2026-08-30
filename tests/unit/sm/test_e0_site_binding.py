"""E0.2: the site binding is authoritative, and registration never overwrites it.

Central Command assigns the site id. Before this slice the Site Manager
received it and discarded it, so the two ends had different id spaces
and nothing could be scoped. Now it is persisted, it is unique, and a
registration that would re-point a bound site is refused rather than
silently moving every device, incident and outcome under it to a
different tenant-plane identity.

Recovery from a genuinely changed identity is the audited unbind on the
break-glass API, never a special case inside registration.
"""

from __future__ import annotations

import httpx
import pytest

from harkeniq.proto import harkeniq_pb2
from harkeniq_sm.app import create_app
from harkeniq_sm.approvals import ApprovalService
from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.repos import AuditRepo, SiteRepo
from harkeniq_sm.grpc_server import SiteManagerServiceServicer
from harkeniq_sm.runtime import AppState

TOKEN = "site-secret"


def _config(**kw):
    defaults = dict(insecure=True, site_name="alpha", grpc_port=0,
                    license_fingerprint="fp-1")
    defaults.update(kw)
    return SMConfig(**defaults)


def _servicer(db, config=None):
    config = config or _config()
    return SiteManagerServiceServicer(db, ApprovalService(db, config), config)


def _reg(site_id="cc-1", site_name="alpha", fp="fp-1"):
    return harkeniq_pb2.SiteRegistration(
        tenant_id="t1", site_id=site_id, site_name=site_name,
        cc_endpoint="cc:8090", license_key_fingerprint=fp,
    )


class TestBinding:
    async def test_registration_persists_the_cc_identity(self, db):
        ack = await _servicer(db).RegisterSite(_reg(), None)
        assert ack.accepted is True
        async with db() as session:
            site = await SiteRepo(session).get_by_name("alpha")
        assert site.cc_site_id == "cc-1"
        assert site.bound_at is not None

    async def test_re_registration_is_idempotent(self, db):
        servicer = _servicer(db)
        assert (await servicer.RegisterSite(_reg(), None)).accepted is True
        assert (await servicer.RegisterSite(_reg(), None)).accepted is True
        async with db() as session:
            sites = await SiteRepo(session).list_all()
        assert len(sites) == 1

    async def test_a_second_site_binds_alongside_the_first(self, db):
        """This is how one Site Manager comes to serve several sites."""
        servicer = _servicer(db)
        await servicer.RegisterSite(_reg("cc-1", "alpha"), None)
        assert (
            await servicer.RegisterSite(_reg("cc-2", "beta"), None)
        ).accepted is True
        async with db() as session:
            sites = {s.name: s.cc_site_id for s in await SiteRepo(session).list_all()}
        assert sites == {"alpha": "cc-1", "beta": "cc-2"}

    async def test_a_conflicting_identity_is_refused_and_nothing_changes(self, db):
        """Requirement 2: fail closed, never overwrite."""
        servicer = _servicer(db)
        await servicer.RegisterSite(_reg("cc-1", "alpha"), None)
        ack = await servicer.RegisterSite(_reg("cc-OTHER", "alpha"), None)
        assert ack.accepted is False
        assert "already bound" in ack.reason
        async with db() as session:
            site = await SiteRepo(session).get_by_name("alpha")
            actions = [r.action for r in await AuditRepo(session).list_all()]
        assert site.cc_site_id == "cc-1", "the binding was overwritten"
        assert "site.bind_refused" in actions

    async def test_a_rename_keeps_the_binding(self, db):
        """The name is a label; the identity is the binding."""
        servicer = _servicer(db)
        await servicer.RegisterSite(_reg("cc-1", "alpha"), None)
        ack = await servicer.RegisterSite(_reg("cc-1", "alpha-renamed"), None)
        assert ack.accepted is True
        async with db() as session:
            site = await SiteRepo(session).get_by_cc_id("cc-1")
            actions = [r.action for r in await AuditRepo(session).list_all()]
        assert site.name == "alpha-renamed"
        assert "site.renamed" in actions

    async def test_a_rename_onto_an_occupied_name_is_refused(self, db):
        servicer = _servicer(db)
        await servicer.RegisterSite(_reg("cc-1", "alpha"), None)
        await servicer.RegisterSite(_reg("cc-2", "beta"), None)
        ack = await servicer.RegisterSite(_reg("cc-1", "beta"), None)
        assert ack.accepted is False
        assert "already has that name" in ack.reason

    async def test_registration_without_a_site_id_is_refused(self, db):
        ack = await _servicer(db).RegisterSite(_reg(site_id=""), None)
        assert ack.accepted is False
        assert "site_id is required" in ack.reason

    async def test_registration_without_a_name_is_refused(self, db):
        ack = await _servicer(db).RegisterSite(_reg(site_name=""), None)
        assert ack.accepted is False

    async def test_a_bad_fingerprint_still_binds_nothing(self, db):
        ack = await _servicer(db).RegisterSite(_reg(fp="wrong"), None)
        assert ack.accepted is False
        async with db() as session:
            assert await SiteRepo(session).list_all() == []

    async def test_binding_is_audited(self, db):
        await _servicer(db).RegisterSite(_reg(), None)
        async with db() as session:
            audit = AuditRepo(session)
            bound = [r for r in await audit.list_all() if r.action == "site.bound"]
            assert bound and bound[0].detail["cc_site_id"] == "cc-1"
            assert (await audit.verify_chain()).valid is True


class TestBreakGlassUnbind:
    @pytest.fixture
    async def api(self, db):
        config = _config(site_token=TOKEN)
        state = AppState(config=config)
        state.sessionmaker = db
        app = create_app(state)
        await _servicer(db, config).RegisterSite(_reg(), None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://sm",
        ) as client:
            yield client, db, config

    async def test_bindings_are_readable(self, api):
        client, _, _ = api
        res = await client.get(
            "/api/site/bindings", headers={"authorization": f"Bearer {TOKEN}"},
        )
        assert res.status_code == 200
        row = res.json()["sites"][0]
        assert row["cc_site_id"] == "cc-1" and row["bound"] is True

    async def test_unbind_clears_the_binding_and_audits_it(self, api):
        client, db, _ = api
        res = await client.post(
            "/api/site/alpha/unbind",
            headers={"authorization": f"Bearer {TOKEN}"},
            json={"actor": "ops@example.com", "confirm_site_name": "alpha",
                  "reason": "central command restored from backup"},
        )
        assert res.status_code == 200
        assert res.json()["previous_cc_site_id"] == "cc-1"
        async with db() as session:
            site = await SiteRepo(session).get_by_name("alpha")
            entries = [r for r in await AuditRepo(session).list_all()
                       if r.action == "site.unbound"]
        assert site.cc_site_id is None
        assert entries[0].actor == "ops@example.com"
        assert entries[0].detail["reason"] == "central command restored from backup"

    async def test_after_unbind_a_new_identity_binds_cleanly(self, api):
        client, db, config = api
        await client.post(
            "/api/site/alpha/unbind",
            headers={"authorization": f"Bearer {TOKEN}"},
            json={"actor": "ops", "confirm_site_name": "alpha", "reason": "restore"},
        )
        ack = await _servicer(db, config).RegisterSite(_reg("cc-NEW", "alpha"), None)
        assert ack.accepted is True
        async with db() as session:
            site = await SiteRepo(session).get_by_name("alpha")
        assert site.cc_site_id == "cc-NEW"

    async def test_a_mismatched_confirmation_is_refused(self, api):
        client, db, _ = api
        res = await client.post(
            "/api/site/alpha/unbind",
            headers={"authorization": f"Bearer {TOKEN}"},
            json={"actor": "ops", "confirm_site_name": "beta", "reason": "oops"},
        )
        assert res.status_code == 400
        async with db() as session:
            assert (await SiteRepo(session).get_by_name("alpha")).cc_site_id == "cc-1"

    async def test_unbind_requires_the_site_token(self, api):
        client, _, _ = api
        res = await client.post(
            "/api/site/alpha/unbind",
            json={"actor": "ops", "confirm_site_name": "alpha", "reason": "x"},
        )
        assert res.status_code == 401

    async def test_unbinding_an_unbound_site_is_a_conflict(self, api):
        client, _, _ = api
        headers = {"authorization": f"Bearer {TOKEN}"}
        body = {"actor": "ops", "confirm_site_name": "alpha", "reason": "x"}
        assert (await client.post(
            "/api/site/alpha/unbind", headers=headers, json=body,
        )).status_code == 200
        assert (await client.post(
            "/api/site/alpha/unbind", headers=headers, json=body,
        )).status_code == 409
