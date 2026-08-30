"""E1.3: one Site Manager, many sites -- on the WRITE path too.

E0.2 proved every Central Command-facing READ resolves to one
authoritative site. This is the other half: registration, correlation,
identity and the halt.

The defect this slice exists to fix: an agent never said which site it
was at. `Ingest.register()` resolved the Site Manager's single configured
site and memoized it on the instance, so two sites on one process would
have put every device from both into one site row -- and the E0.2 reads
would then have scoped perfectly to a set that was already wrong.

Ratified invariants under test:

  D1  SITE IDENTITY MUST BE AUTHORITATIVE, NOT AGENT-DECLARED.
  D2  SITE IS THE NORMAL OPERATIONAL SAFETY BOUNDARY.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.models import (
    AgentIdentityRow,
    Device,
    Site,
    SiteEnrollmentToken,
    StopSwitchRow,
)
from harkeniq_sm.enrollment import (
    EnrollmentError,
    EnrollmentService,
    hash_token,
    is_usable,
    mint_token,
)
from harkeniq_sm.stopswitch import (
    DECISION_INPUTS,
    SCOPE_SITE,
    SCOPE_SITE_MANAGER,
    SCOPE_TENANT,
    StopSwitchService,
    execution_permitted,
    halt_state,
)


def _config(**kw):
    defaults = dict(insecure=True, site_name="alpha", grpc_port=0)
    defaults.update(kw)
    return SMConfig(**defaults)


async def _sites(db, *names):
    async with db() as session:
        rows = [Site(name=n, cc_site_id=f"cc-{n}") for n in names]
        session.add_all(rows)
        await session.commit()
        return {r.name: r.id for r in rows}


async def _issue(db, site_id, **kw):
    service = EnrollmentService(db, _config())
    async with db() as session:
        secret, row = await service.issue(session, site_id=site_id, **kw)
        await session.commit()
        return secret, row.id


# ---------------------------------------------------------------------------
# D1: the site is what the Site Manager knows
# ---------------------------------------------------------------------------


class TestCredentialShape:
    def test_a_credential_is_high_entropy_and_recognisable(self):
        token = mint_token()
        assert token.startswith("hqe_")
        assert len(token) == 68  # prefix + 64 hex
        assert token != mint_token()

    def test_only_the_hash_is_ever_stored(self):
        token = mint_token()
        digest = hash_token(token)
        assert len(digest) == 64
        assert token not in digest
        # Stable, so a presented secret resolves; one-way, so a leaked
        # database yields nothing an attacker could enroll with.
        assert hash_token(token) == digest
        assert hash_token(f" {token} ") == digest

    def test_revoked_and_expired_are_unusable(self):
        now = datetime(2026, 8, 30, tzinfo=timezone.utc)

        class Row:
            revoked_at = None
            expires_at = None

        assert is_usable(Row(), now=now)
        r = Row()
        r.revoked_at = now - timedelta(days=1)
        assert not is_usable(r, now=now)
        r = Row()
        r.expires_at = now - timedelta(minutes=1)
        assert not is_usable(r, now=now)
        r = Row()
        r.expires_at = now + timedelta(minutes=1)
        assert is_usable(r, now=now)

    def test_a_naive_expiry_is_not_read_as_expired(self):
        # sqlite hands back naive datetimes for tz-aware writes; getting
        # this wrong would silently invalidate every credential.
        class Row:
            revoked_at = None
            expires_at = datetime(2030, 1, 1)

        assert is_usable(Row(), now=datetime(2026, 8, 30, tzinfo=timezone.utc))


class TestResolution:
    @pytest.mark.asyncio
    async def test_a_credential_resolves_to_exactly_its_own_site(self, db):
        ids = await _sites(db, "alpha", "beta")
        secret_a, _ = await _issue(db, ids["alpha"])
        secret_b, _ = await _issue(db, ids["beta"])

        service = EnrollmentService(db, _config())
        async with db() as session:
            assert (await service.resolve(session, secret_a)).site_id == ids["alpha"]
            assert (await service.resolve(session, secret_b)).site_id == ids["beta"]

    @pytest.mark.asyncio
    async def test_an_unknown_credential_is_refused(self, db):
        await _sites(db, "alpha", "beta")
        service = EnrollmentService(db, _config())
        async with db() as session:
            with pytest.raises(EnrollmentError) as exc:
                await service.resolve(session, "hqe_" + "0" * 64)
            assert exc.value.code == "unknown"

    @pytest.mark.asyncio
    async def test_a_revoked_credential_is_refused(self, db):
        ids = await _sites(db, "alpha", "beta")
        secret, token_id = await _issue(db, ids["alpha"])
        service = EnrollmentService(db, _config())
        async with db() as session:
            row = await session.get(SiteEnrollmentToken, token_id)
            await service.revoke(session, row, revoked_by="op")
            await session.commit()
        async with db() as session:
            with pytest.raises(EnrollmentError) as exc:
                await service.resolve(session, secret)
            assert exc.value.code == "revoked"

    @pytest.mark.asyncio
    async def test_a_credential_for_a_retired_site_is_refused(self, db):
        ids = await _sites(db, "alpha", "beta")
        secret, _ = await _issue(db, ids["alpha"])
        async with db() as session:
            site = await session.get(Site, ids["alpha"])
            site.status = "retired"
            await session.commit()
        service = EnrollmentService(db, _config())
        async with db() as session:
            with pytest.raises(EnrollmentError) as exc:
                await service.resolve(session, secret)
            assert exc.value.code == "inactive_site"

    @pytest.mark.asyncio
    async def test_no_credential_on_a_MULTI_site_manager_is_refused(self, db):
        """The heart of D1.

        With two sites and no credential there is no correct answer, and
        guessing is exactly the bug: every device would land in one site.
        """
        await _sites(db, "alpha", "beta")
        service = EnrollmentService(db, _config())
        async with db() as session:
            with pytest.raises(EnrollmentError) as exc:
                await service.resolve(session, "")
            assert exc.value.code == "ambiguous"
            assert "2 sites" in exc.value.reason

    @pytest.mark.asyncio
    async def test_no_credential_on_a_SINGLE_site_manager_still_works(self, db):
        """Compatibility: an existing deployment upgrades untouched."""
        ids = await _sites(db, "alpha")
        service = EnrollmentService(db, _config())
        async with db() as session:
            resolved = await service.resolve(session, "")
            assert resolved.site_id == ids["alpha"]
            assert resolved.legacy_single_site is True

    @pytest.mark.asyncio
    async def test_use_is_recorded_so_a_credential_can_be_audited(self, db):
        ids = await _sites(db, "alpha", "beta")
        secret, token_id = await _issue(db, ids["alpha"])
        service = EnrollmentService(db, _config())
        async with db() as session:
            await service.resolve(session, secret)
            await session.commit()
        async with db() as session:
            row = await session.get(SiteEnrollmentToken, token_id)
            assert row.use_count == 1
            assert row.last_used_at is not None

    @pytest.mark.asyncio
    async def test_two_sites_can_never_share_one_secret(self, db):
        """The unique constraint IS the guarantee, not a check.

        A secret resolving to two sites is the ambiguity this table
        exists to remove, so the database refuses it.
        """
        import sqlalchemy.exc

        ids = await _sites(db, "alpha", "beta")
        digest = hash_token("hqe_shared")
        async with db() as session:
            session.add(SiteEnrollmentToken(
                site_id=ids["alpha"], token_hash=digest))
            await session.commit()
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            async with db() as session:
                session.add(SiteEnrollmentToken(
                    site_id=ids["beta"], token_hash=digest))
                await session.commit()


class TestRegistrationBindsTheSite:
    @pytest.mark.asyncio
    async def test_two_agents_with_different_credentials_land_apart(self, db):
        """The defect, inverted.

        Before E1.3 both of these would have landed in whichever site the
        Site Manager had configured.
        """
        from harkeniq_sm.ingest import IngestService

        ids = await _sites(db, "alpha", "beta")
        secret_a, _ = await _issue(db, ids["alpha"])
        secret_b, _ = await _issue(db, ids["beta"])

        service = EnrollmentService(db, _config())
        ingest = IngestService(db, _config())
        async with db() as session:
            ea = await service.resolve(session, secret_a)
            eb = await service.resolve(session, secret_b)
            await session.commit()

        await ingest.register(agent_id="node-a", site_id=ea.site_id,
                              site_name=ea.site_name)
        await ingest.register(agent_id="node-b", site_id=eb.site_id,
                              site_name=eb.site_name)

        async with db() as session:
            from harkeniq_sm.db.repos import DeviceRepo

            repo = DeviceRepo(session)
            a = await repo.get_by_agent_id("node-a")
            b = await repo.get_by_agent_id("node-b")
            assert a.site_id == ids["alpha"]
            assert b.site_id == ids["beta"]
            assert a.site_id != b.site_id

    @pytest.mark.asyncio
    async def test_the_single_site_memo_is_gone(self):
        """`Ingest._site` used to cache the configured site for the life
        of the process, which made "the site" a property of the Site
        Manager rather than of the device."""
        import inspect

        from harkeniq_sm import ingest as ingest_module

        source = inspect.getsource(ingest_module.IngestService)
        assert "self._site_id = site.id" not in source
        assert "_site_id: Optional[str] = None" not in source

    @pytest.mark.asyncio
    async def test_a_multi_site_manager_refuses_to_guess(self, db):
        from harkeniq_sm.ingest import IngestService

        await _sites(db, "alpha", "beta")
        ingest = IngestService(db, _config())
        with pytest.raises(ValueError) as exc:
            await ingest.register(agent_id="node-x")
        assert "must name one" in str(exc.value)


class TestIdentityIsIssuedForASite:
    @pytest.mark.asyncio
    async def test_a_device_cannot_move_site_by_re_registering(self, db):
        """Changing site is an explicit re-enrollment, not a claim."""
        from harkeniq_sm.agent_identity import AgentIdentityService

        ids = await _sites(db, "alpha", "beta")
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        from cryptography.hazmat.primitives import serialization

        # The service signs certificates, so it takes the SM's own key.
        service = AgentIdentityService(db, Ed25519PrivateKey.generate())
        key = Ed25519PrivateKey.generate()
        pem = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        # R3a: the agent id IS the hash of its public key.
        import hashlib

        agent_id = hashlib.sha256(pem).hexdigest()[:16]

        await service.register_agent(
            agent_id=agent_id, public_key_pem=pem, site_name="alpha",
            site_id=ids["alpha"],
        )
        with pytest.raises(ValueError) as exc:
            await service.register_agent(
                agent_id=agent_id, public_key_pem=pem, site_name="beta",
                site_id=ids["beta"],
            )
        assert "re-enrollment" in str(exc.value)

        async with db() as session:
            row = await session.get(AgentIdentityRow, agent_id)
            assert row.site_id == ids["alpha"]


# ---------------------------------------------------------------------------
# D2: the site is the normal safety boundary
# ---------------------------------------------------------------------------


class _Row:
    def __init__(self, scope, site_id, active, by="op", reason=""):
        self.scope, self.site_id, self.active = scope, site_id, active
        self.activated_by, self.reason = by, reason


class TestHaltScopes:
    def test_stopping_one_site_leaves_the_other_running(self):
        rows = [_Row(SCOPE_SITE, "A", True, "maintenance")]
        assert halt_state("A", rows).halted is True
        assert halt_state("B", rows).halted is False

    def test_the_site_manager_halt_stops_every_site(self):
        rows = [_Row(SCOPE_SITE_MANAGER, None, True, "emergency")]
        assert halt_state("A", rows).halted is True
        assert halt_state("B", rows).halted is True

    def test_the_tenant_stop_stops_every_site(self):
        rows = [_Row(SCOPE_TENANT, None, True, "cc")]
        for site in ("A", "B"):
            state = halt_state(site, rows)
            assert state.halted
            assert "tenant" in state.reason

    def test_an_inactive_row_halts_nothing(self):
        rows = [
            _Row(SCOPE_SITE, "A", False),
            _Row(SCOPE_SITE_MANAGER, None, False),
            _Row(SCOPE_TENANT, None, False),
        ]
        assert halt_state("A", rows).halted is False

    def test_the_reason_names_the_broadest_halt_in_force(self):
        rows = [
            _Row(SCOPE_SITE, "A", True, reason="psu work"),
            _Row(SCOPE_TENANT, None, True, reason="incident"),
        ]
        assert "tenant" in halt_state("A", rows).reason

    def test_the_state_reports_all_three_separately(self):
        rows = [_Row(SCOPE_SITE, "A", True, "op", "psu work")]
        d = halt_state("A", rows).as_dict()
        assert d["site_stop"]["active"] is True
        assert d["tenant_stop"]["active"] is False
        assert d["site_manager_halt"]["active"] is False


class TestPersistedHalts:
    @pytest.mark.asyncio
    async def test_a_halt_survives_a_restart(self, db):
        """The old switch was an in-memory boolean.

        An operator could halt a site, the process could restart, and
        autonomy would resume with nothing in the record saying it had
        ever stopped.
        """
        ids = await _sites(db, "alpha", "beta")
        switches = StopSwitchService(db)
        async with db() as session:
            await switches.set_halt(
                session, scope=SCOPE_SITE, site_id=ids["alpha"],
                active=True, actor="op", reason="psu work",
            )
            await session.commit()

        # A brand-new service object: nothing carried over in memory.
        async with db() as session:
            state = await StopSwitchService(db).state_for(session, ids["alpha"])
            assert state.halted
            assert state.site_reason == "psu work"
            other = await StopSwitchService(db).state_for(session, ids["beta"])
            assert other.halted is False

    @pytest.mark.asyncio
    async def test_lifting_is_idempotent_and_recorded(self, db):
        ids = await _sites(db, "alpha")
        switches = StopSwitchService(db)
        async with db() as session:
            await switches.set_halt(session, scope=SCOPE_SITE,
                                    site_id=ids["alpha"], active=True,
                                    actor="op")
            await switches.set_halt(session, scope=SCOPE_SITE,
                                    site_id=ids["alpha"], active=False,
                                    actor="op2")
            await session.commit()
        async with db() as session:
            rows = await switches.rows(session)
            assert len(rows) == 1
            assert rows[0].active is False
            assert rows[0].deactivated_by == "op2"

    @pytest.mark.asyncio
    async def test_a_site_halt_needs_a_site_and_the_others_refuse_one(self, db):
        switches = StopSwitchService(db)
        async with db() as session:
            with pytest.raises(ValueError):
                await switches.set_halt(session, scope=SCOPE_SITE,
                                        site_id=None, active=True, actor="op")
            with pytest.raises(ValueError):
                await switches.set_halt(session, scope=SCOPE_SITE_MANAGER,
                                        site_id="A", active=True, actor="op")


class TestExecutionDecision:
    """An autonomy level is a ceiling, never execution authority."""

    def _all_clear(self):
        return {name: True for name in DECISION_INPUTS}

    def test_every_input_must_be_evaluated(self):
        assert len(DECISION_INPUTS) == 10
        for name in DECISION_INPUTS:
            inputs = self._all_clear()
            del inputs[name]
            decision = execution_permitted(**inputs)
            assert decision.permitted is False
            assert decision.refused_by == name

    def test_an_unevaluated_input_is_a_refusal_not_a_pass(self):
        decision = execution_permitted(autonomy=True)
        assert decision.permitted is False
        assert "never evaluated" in decision.reason

    def test_all_clear_permits(self):
        assert execution_permitted(**self._all_clear()).permitted is True

    @pytest.mark.parametrize("refuser", DECISION_INPUTS)
    def test_any_single_input_refusing_is_a_refusal(self, refuser):
        inputs = self._all_clear()
        inputs[refuser] = f"{refuser} says no"
        decision = execution_permitted(**inputs)
        assert decision.permitted is False
        assert decision.refused_by == refuser
        assert decision.reason == f"{refuser} says no"

    def test_autonomy_alone_never_authorizes(self):
        """The failure this guards against, stated directly."""
        decision = execution_permitted(autonomy=True, site_stop=True)
        assert decision.permitted is False

    def test_the_decision_reports_what_it_considered(self):
        d = execution_permitted(**self._all_clear()).as_dict()
        assert d["inputs_considered"] == list(DECISION_INPUTS)
        assert "tenant_stop" in d["inputs_considered"]
        assert "site_stop" in d["inputs_considered"]
        assert "manager_halt" in d["inputs_considered"]


class TestNoSecondAuthorizationModel:
    def test_the_site_manager_never_imports_the_scope_resolver(self):
        """Ratified: no second RBAC resolver below Central Command.

        Asserted over the import graph rather than by review, because
        this is the kind of boundary that erodes one convenience import
        at a time.
        """
        import pathlib

        root = pathlib.Path("services/site_manager/src/harkeniq_sm")
        offenders = []
        for path in root.rglob("*.py"):
            text = path.read_text()
            if "harkeniq_cc" in text:
                offenders.append(str(path))
        assert not offenders, (
            "the Site Manager imported Central Command: authority is "
            "governed above the Site Manager, and a resolver here would "
            "be the second authorization model the architecture forbids:\n  "
            + "\n  ".join(offenders)
        )

    def test_enrollment_answers_where_never_who(self):
        import inspect

        from harkeniq_sm import enrollment

        source = inspect.getsource(enrollment)
        for forbidden in ("permission", "role", "ROLE_PERMISSIONS", "grant"):
            assert f"{forbidden}(" not in source


# ---------------------------------------------------------------------------
# Correlation: the correctness risk in this slice
# ---------------------------------------------------------------------------


class TestCorrelationDoesNotCrossSites:
    """The site is the operational correlation boundary.

    Two devices in different buildings failing at the same moment are a
    coincidence, not a shared cause. Before E1.3 both correlation entry
    points resolved the Site Manager's ONE configured site, so two sites
    on one process would have produced a shared-power incident spanning
    estates that share no power at all.
    """

    def test_on_onset_correlates_the_devices_own_site(self):
        import inspect

        from harkeniq_sm.correlation import engine

        source = inspect.getsource(engine.CorrelationEngine.on_onset)
        # It must not resolve a configured site...
        assert "get_or_create(self.config.site_name)" not in source
        # ...it must use the device's own.
        assert "device.site_id" in source

    def test_sweep_runs_once_per_site(self):
        import inspect

        from harkeniq_sm.correlation import engine

        source = inspect.getsource(engine.CorrelationEngine.sweep)
        assert "get_or_create(self.config.site_name)" not in source
        assert "for site_id in await self._site_ids(session)" in source

    @pytest.mark.asyncio
    async def test_a_device_with_no_site_is_not_correlated_into_a_guess(
        self, db
    ):
        """Refusing beats guessing: a device with no site correlated into
        the configured one would put it in somebody else's estate."""
        import inspect

        from harkeniq_sm.correlation import engine

        source = inspect.getsource(engine.CorrelationEngine.on_onset)
        assert "refusing to correlate" in source

    @pytest.mark.asyncio
    async def test_every_site_is_swept(self, db):
        from harkeniq_sm.correlation.engine import CorrelationEngine

        ids = await _sites(db, "alpha", "beta", "gamma")
        engine = CorrelationEngine.__new__(CorrelationEngine)
        engine.config = _config()
        async with db() as session:
            found = await CorrelationEngine._site_ids(engine, session)
        assert set(found) == set(ids.values())


class TestAuditChainSurvivesSiteScoping:
    def test_site_id_is_not_in_the_hash_payload(self):
        """Adding a column the payload does not name must leave every
        chain written before it verifiable -- the same trade CC made in
        E1.2, checked here against this service's own payload."""
        import inspect

        from harkeniq_sm.db.repos import AuditRepo

        source = inspect.getsource(AuditRepo._chain_payload)
        assert "site_id" not in source

    @pytest.mark.asyncio
    async def test_a_chain_written_without_sites_still_verifies(self, db):
        from harkeniq_sm.db.repos import AuditRepo

        async with db() as session:
            repo = AuditRepo(session)
            for n in range(4):
                await repo.append(actor="old", action=f"legacy.{n}")
            await session.commit()
        async with db() as session:
            assert (await AuditRepo(session).verify_chain()).valid

        async with db() as session:
            repo = AuditRepo(session)
            for n in range(4):
                await repo.append(
                    actor="new", action=f"scoped.{n}", site_id=f"site-{n}"
                )
            await session.commit()

        async with db() as session:
            result = await AuditRepo(session).verify_chain()
            assert result.valid, "the chain broke when a site was recorded"
            assert result.length == 8


class TestSiteScopedReads:
    """An operator's read names a site, or is unambiguous. Never all."""

    @pytest.mark.asyncio
    async def test_a_multi_site_manager_refuses_an_unnamed_read(self, db):
        from fastapi import HTTPException

        from harkeniq_sm.api.site_scope import resolve_site

        await _sites(db, "alpha", "beta")
        request = _fake_request(db)
        with pytest.raises(HTTPException) as exc:
            await resolve_site(request, None)
        assert exc.value.status_code == 400
        assert "alpha" in exc.value.detail and "beta" in exc.value.detail

    @pytest.mark.asyncio
    async def test_a_single_site_manager_answers_without_being_asked(self, db):
        from harkeniq_sm.api.site_scope import resolve_site

        ids = await _sites(db, "alpha")
        scope = await resolve_site(_fake_request(db), None)
        assert scope.id == ids["alpha"]

    @pytest.mark.asyncio
    async def test_naming_a_site_resolves_it(self, db):
        from harkeniq_sm.api.site_scope import resolve_site

        ids = await _sites(db, "alpha", "beta")
        scope = await resolve_site(_fake_request(db), "beta")
        assert scope.id == ids["beta"]

    @pytest.mark.asyncio
    async def test_naming_a_site_this_manager_does_not_serve_is_404(self, db):
        from fastapi import HTTPException

        from harkeniq_sm.api.site_scope import resolve_site

        await _sites(db, "alpha", "beta")
        with pytest.raises(HTTPException) as exc:
            await resolve_site(_fake_request(db), "gamma")
        assert exc.value.status_code == 404


def _fake_request(db):
    """The minimum a site-scope dependency reads off a request."""

    class State:
        sessionmaker = db
        config = _config()

    class AppState:
        sm = State()

    class App:
        state = AppState()

    class Request:
        app = App()

    return Request()


class TestASiteChangeIsExplicit:
    """Ratified D1: changing site requires re-enrollment, not a claim.

    The upsert already happened not to rewrite `site_id`, so the outcome
    was right by accident. A silent no-op is not enforcement: it tells
    the operator nothing, and an accident is not a guarantee.
    """

    @pytest.mark.asyncio
    async def test_re_registering_with_another_sites_credential_is_refused(
        self, db
    ):
        from harkeniq_sm.db.repos import DeviceRepo

        ids = await _sites(db, "alpha", "beta")
        async with db() as session:
            await DeviceRepo(session).upsert_registration(
                site_id=ids["alpha"], agent_id="node-a", agent_name="a"
            )
            await session.commit()

        async with db() as session:
            with pytest.raises(ValueError) as exc:
                await DeviceRepo(session).upsert_registration(
                    site_id=ids["beta"], agent_id="node-a", agent_name="a"
                )
            assert "re-enrollment" in str(exc.value)

        async with db() as session:
            device = await DeviceRepo(session).get_by_agent_id("node-a")
            assert device.site_id == ids["alpha"], "the device moved"

    @pytest.mark.asyncio
    async def test_re_registering_with_its_own_credential_is_fine(self, db):
        from harkeniq_sm.db.repos import DeviceRepo

        ids = await _sites(db, "alpha")
        async with db() as session:
            repo = DeviceRepo(session)
            await repo.upsert_registration(
                site_id=ids["alpha"], agent_id="node-a", agent_name="a"
            )
            await repo.upsert_registration(
                site_id=ids["alpha"], agent_id="node-a", agent_name="a2"
            )
            await session.commit()
        async with db() as session:
            device = await DeviceRepo(session).get_by_agent_id("node-a")
            assert device.agent_name == "a2"

    def test_the_wire_has_no_field_an_agent_could_name_a_site_with(self):
        """The structural half of D1.

        Enforcement is only half the story: if the message carried a
        `site_name` an agent could set, somebody would eventually read it.
        """
        from harkeniq.proto import harkeniq_pb2

        fields = {
            f.name for f in harkeniq_pb2.AgentRegistration.DESCRIPTOR.fields
        }
        assert "site_name" not in fields
        assert "site_id" not in fields
        assert "enrollment_token" in fields
