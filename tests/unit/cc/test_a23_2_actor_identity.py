"""A23-2: stable actor identity on the audit log (spec A23.7).

`cc_audit_log.actor` was written in three forms -- Keycloak subject,
email, and "email or subject" -- and the A22.10 migration census compared
those strings to subject-keyed grants, so a granted person recorded by
email read as ungranted. This file proves the correction end to end:

* every NEW audit row carries `actor_ref`, written through the ONE
  `actor_of()` helper, for humans, machine principals, agents and
  campaigns alike;
* historical rows are left exactly as they were: `actor_ref` NULL,
  `actor` untouched, and a chain written before the column existed still
  verifies, because `actor_ref` is outside `_chain_payload`;
* readers are dual-form: the audit API returns both, and its actor filter
  answers to either the stable reference or the legacy string;
* the impact census resolves observed actors to stable identity, uses
  in-repo evidence (never fuzzy matching) for a legacy email, and reports
  what it cannot resolve instead of counting it as a different person.
"""

from __future__ import annotations

import inspect

import httpx
import pytest
from sqlalchemy import select

from harkeniq_cc.actor import actor_of
from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCAuditLog, CCSite
from harkeniq_cc.db.repos import (
    ApprovalGroupRepo,
    AuditRepo,
    ScopeGrantRepo,
)
from harkeniq_cc.runtime import AppState
from harkeniq_cc.scope import SCOPE_SITE, SCOPE_TENANT

TENANT = "tenant-a23"
SUB = "6f9c1f0e-3d1a-4a71-9d1b-1c2f3a4b5c6d"


def _human(role="tenant_owner", sub=SUB, email="ops@example.com"):
    return UserContext(
        user_id=sub, email=email, tenant_id=TENANT, role=role,
        permissions=list(ROLE_PERMISSIONS[role]),
    )


def _machine(agent_id="0123456789abcdef0123456789abcdef"):
    return UserContext(
        user_id=agent_id, email=f"op-agent:{agent_id}@v3", tenant_id=TENANT,
        role="", permissions=["fleet.view"], species="agent", identity_id="ident",
    )


async def _stack():
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    app = create_app(AppState(
        config=CCConfig(tenant_id=TENANT, insecure=True),
        engine=engine, sessionmaker=sessionmaker,
    ))
    return app, sessionmaker


def _client(app, user: UserContext):
    async def _fake():
        return user

    app.dependency_overrides[get_current_user] = _fake
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# The one helper
# ---------------------------------------------------------------------------


class TestActorOf:
    def test_a_human_context_resolves_to_the_subject_never_the_email(self):
        assert actor_of(_human()) == SUB

    def test_a_machine_context_resolves_to_the_agent_id(self):
        """A3 keys an agent's grants on the agent id, and so does this."""
        assert actor_of(_machine()) == "0123456789abcdef0123456789abcdef"

    def test_an_attribution_key_resolves_to_the_agent_across_versions(self):
        a = "0123456789abcdef0123456789abcdef"
        assert actor_of(f"op-agent:{a}@v1") == a
        assert actor_of(f"op-agent:{a}@v7") == a

    def test_a_campaign_actor_is_versionless(self):
        assert actor_of("campaign:abc@v2") == "campaign:abc"
        assert actor_of("campaign:abc@v9") == "campaign:abc"

    def test_a_bare_subject_is_itself(self):
        assert actor_of(SUB) == SUB
        assert actor_of("0123456789abcdef0123456789abcdef") == "0123456789abcdef0123456789abcdef"

    def test_an_email_is_display_and_resolves_to_nothing(self):
        assert actor_of("ops@example.com") is None

    def test_free_text_and_machine_prefix_are_unresolved(self):
        assert actor_of("seed") is None
        assert actor_of("machine:some-service-account-sub") is None
        assert actor_of("") is None
        assert actor_of(None) is None

    def test_system_is_system(self):
        assert actor_of("system") == "system"
        assert actor_of("system:poller") == "system:poller"

    def test_there_is_exactly_one_helper(self):
        import harkeniq_cc.actor as module

        public = [
            n for n in dir(module)
            if not n.startswith("_")
            and inspect.isfunction(getattr(module, n))
            and getattr(module, n).__module__ == module.__name__
        ]
        assert public == ["actor_of"], public


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


class TestWriters:
    @pytest.mark.asyncio
    async def test_a_new_row_carries_the_stable_ref_and_keeps_the_display(self):
        _, sessionmaker = await _stack()
        async with sessionmaker() as session:
            row = await AuditRepo(session).append(
                actor="ops@example.com", actor_ref=actor_of(_human()),
                action="x", tenant_id=TENANT,
            )
            await session.commit()
        assert row.actor == "ops@example.com"
        assert row.actor_ref == SUB

    @pytest.mark.asyncio
    async def test_a_writer_holding_only_an_attribution_key_still_gets_a_ref(self):
        _, sessionmaker = await _stack()
        async with sessionmaker() as session:
            agent = await AuditRepo(session).append(
                actor="op-agent:0123456789abcdef0123456789abcdef@v2",
                action="agent_proposal.created", tenant_id=TENANT,
            )
            campaign = await AuditRepo(session).append(
                actor="campaign:c1@v3", action="campaign.wave_dispatched",
                tenant_id=TENANT,
            )
            email = await AuditRepo(session).append(
                actor="ops@example.com", action="legacy.shape", tenant_id=TENANT,
            )
            await session.commit()
        assert agent.actor_ref == "0123456789abcdef0123456789abcdef"
        assert campaign.actor_ref == "campaign:c1"
        assert email.actor_ref is None, "unresolvable is recorded as unresolvable"

    @pytest.mark.asyncio
    async def test_every_api_audit_write_carries_the_callers_stable_ref(self):
        """Drive real mutations through the app and read the ledger back."""
        app, sessionmaker = await _stack()
        owner = _human()
        async with sessionmaker() as session:
            await ScopeGrantRepo(session).grant(
                tenant_id=TENANT, principal_type="user", principal_ref=SUB,
                scope_type=SCOPE_TENANT, scope_ref="", role="tenant_owner",
                granted_by="seed",
            )
            await session.commit()
        async with _client(app, owner) as client:
            r = await client.post("/api/org-units/", json={"name": "Region", "unit_type": "region"})
            assert r.status_code == 201, r.text
            r = await client.post("/api/policies/", json={"name": "dual"})
            assert r.status_code in (200, 201), r.text
            r = await client.post("/api/scope-grants/", json={
                "principal_ref": "somebody-else", "scope_type": "tenant", "role": "auditor",
            })
            assert r.status_code == 201, r.text
        async with sessionmaker() as session:
            rows = (await session.execute(
                select(CCAuditLog).where(CCAuditLog.tenant_id == TENANT)
            )).scalars().all()
        assert len(rows) >= 3
        assert all(r.actor_ref == SUB for r in rows), [(r.action, r.actor_ref) for r in rows]

    def test_no_api_audit_write_bypasses_the_helper(self):
        """Every `AuditRepo(...).append(` in the API layer names actor_ref."""
        import pathlib

        import harkeniq_cc.api as api_pkg

        offenders = []
        for path in sorted(pathlib.Path(api_pkg.__path__[0]).glob("*.py")):
            text = path.read_text()
            idx = 0
            while True:
                idx = text.find(").append(", idx)
                if idx < 0:
                    break
                block = text[idx: text.find(")", idx + 10) + 400]
                # An attribution key (`proposal.actor`) is derivable by
                # the helper inside `append`; a human's identity is not,
                # so a handler acting as a user must pass actor_ref.
                derivable = "actor=proposal.actor" in block
                if (
                    "AuditRepo" in text[max(0, idx - 40): idx]
                    and "actor_ref=" not in block
                    and not derivable
                ):
                    offenders.append(f"{path.name}:{text[:idx].count(chr(10)) + 1}")
                idx += 10
        assert not offenders, offenders


# ---------------------------------------------------------------------------
# Historical rows and the chain
# ---------------------------------------------------------------------------


class TestHistoricalRows:
    def test_actor_ref_is_not_in_the_hash_payload(self):
        source = inspect.getsource(AuditRepo._chain_payload)
        assert "actor_ref" not in source
        assert '"actor": row.actor' in source

    @pytest.mark.asyncio
    async def test_a_chain_written_before_the_column_still_verifies(self):
        """Rows with actor_ref NULL and rows with it set share one chain."""
        _, sessionmaker = await _stack()
        async with sessionmaker() as session:
            repo = AuditRepo(session)
            # Historical shape: written with a display string, no ref.
            legacy = await repo.append(actor="ops@example.com", action="a", tenant_id=TENANT)
            legacy.actor_ref = None   # exactly what 0020 leaves behind
            await session.flush()
            await repo.append(actor=SUB, actor_ref=SUB, action="b", tenant_id=TENANT)
            await repo.append(actor="ops@example.com", actor_ref=SUB, action="c", tenant_id=TENANT)
            await session.commit()
            result = await repo.verify_chain()
            assert result.valid and result.length == 3

            # Setting or clearing actor_ref on any row changes no hash.
            rows = (await session.execute(select(CCAuditLog).order_by(CCAuditLog.seq))).scalars().all()
            rows[1].actor_ref = None
            rows[0].actor_ref = "anything"
            await session.flush()
            assert (await repo.verify_chain()).valid

    @pytest.mark.asyncio
    async def test_the_api_returns_both_forms_and_filters_by_either(self):
        app, sessionmaker = await _stack()
        auditor = _human(role="auditor", sub="aud-1")
        async with sessionmaker() as session:
            await ScopeGrantRepo(session).grant(
                tenant_id=TENANT, principal_type="user", principal_ref="aud-1",
                scope_type=SCOPE_TENANT, scope_ref="", role="auditor", granted_by="seed",
            )
            repo = AuditRepo(session)
            legacy = await repo.append(actor="ops@example.com", action="old", tenant_id=TENANT)
            legacy.actor_ref = None
            await repo.append(actor="ops@example.com", actor_ref=SUB, action="new", tenant_id=TENANT)
            await session.commit()
        async with _client(app, auditor) as client:
            everything = (await client.get("/api/audit/")).json()["entries"]
            by_ref = (await client.get(f"/api/audit/?actor={SUB}")).json()
            by_legacy = (await client.get("/api/audit/?actor=ops@example.com")).json()
        forms = {(e["action"], e["actor"], e["actor_ref"]) for e in everything}
        assert ("old", "ops@example.com", None) in forms
        assert ("new", "ops@example.com", SUB) in forms
        assert {e["action"] for e in by_ref["entries"]} == {"new"} and by_ref["total"] == 1
        assert {e["action"] for e in by_legacy["entries"]} == {"old", "new"}
        assert by_legacy["total"] == 2


# ---------------------------------------------------------------------------
# The census
# ---------------------------------------------------------------------------


class TestImpactCensus:
    async def _seed(self, sessionmaker):
        async with sessionmaker() as session:
            session.add(CCSite(tenant_id=TENANT, site_name="s", sm_endpoint="sm:1", sm_token="t"))
            await session.flush()
            # Granted by subject.
            await ScopeGrantRepo(session).grant(
                tenant_id=TENANT, principal_type="user", principal_ref=SUB,
                scope_type=SCOPE_TENANT, scope_ref="", role="tenant_owner", granted_by="seed",
            )
            repo = AuditRepo(session)
            # Pre-A23-2 rows: the same person recorded three ways.
            for actor in ("ops@example.com", SUB, "ops@example.com"):
                row = await repo.append(actor=actor, action="legacy", tenant_id=TENANT)
                row.actor_ref = None
            # A new row, written properly.
            await repo.append(actor="ops@example.com", actor_ref=SUB, action="new", tenant_id=TENANT)
            # A different, ungranted person -- also recorded by subject.
            await repo.append(actor="b0b0b0b0-0000-4000-8000-000000000001",
                              actor_ref="b0b0b0b0-0000-4000-8000-000000000001",
                              action="new", tenant_id=TENANT)
            # An email nobody can pair with a subject.
            stray = await repo.append(actor="stranger@example.com", action="legacy", tenant_id=TENANT)
            stray.actor_ref = None
            # Non-people.
            await repo.append(actor="campaign:c1@v1", action="campaign.x", tenant_id=TENANT)
            await repo.append(actor="system", action="poll", tenant_id=TENANT)
            await session.commit()

    @pytest.mark.asyncio
    async def test_the_same_person_in_three_forms_is_one_granted_principal(self):
        app, sessionmaker = await _stack()
        await self._seed(sessionmaker)
        async with _client(app, _human()) as client:
            report = (await client.get("/api/tenant-settings/scope-enforcement/impact")).json()
        # The granted owner never appears as ungranted, in ANY form.
        assert SUB not in report["observed_principals_without_grant"]
        assert "ops@example.com" not in report["observed_principals_without_grant"]
        # The genuinely different, ungranted person does.
        assert report["observed_principals_without_grant"] == [
            "b0b0b0b0-0000-4000-8000-000000000001"
        ]
        detail = {d["principal_ref"]: d for d in report["observed_principals_detail"]}
        assert detail[SUB]["granted"] is True
        assert set(detail[SUB]["observed_as"]) == {"ops@example.com", SUB}
        # The stray email is reported as unresolved, not as a person.
        assert report["unresolved_legacy_actors"] == ["stranger@example.com"]
        assert "campaign" not in " ".join(report["observed_principals_without_grant"])

    @pytest.mark.asyncio
    async def test_a_legacy_email_is_resolved_only_through_in_repo_evidence(self):
        """A pre-A23-2 row by email, with the person granted by subject and
        no new row at all: only an approval record or a group membership
        that paired the address with the subject may resolve it."""
        app, sessionmaker = await _stack()
        async with sessionmaker() as session:
            session.add(CCSite(tenant_id=TENANT, site_name="s", sm_endpoint="sm:1", sm_token="t"))
            await ScopeGrantRepo(session).grant(
                tenant_id=TENANT, principal_type="user", principal_ref=SUB,
                scope_type=SCOPE_TENANT, scope_ref="", role="tenant_owner", granted_by="seed",
            )
            row = await AuditRepo(session).append(actor="ops@example.com", action="legacy", tenant_id=TENANT)
            row.actor_ref = None
            await session.commit()
        async with _client(app, _human()) as client:
            before = (await client.get("/api/tenant-settings/scope-enforcement/impact")).json()
        assert before["unresolved_legacy_actors"] == ["ops@example.com"]
        assert before["observed_principals_without_grant"] == []

        async with sessionmaker() as session:
            group = await ApprovalGroupRepo(session).create(
                tenant_id=TENANT, name="approvers", created_by="seed", required_count=1,
            )
            await ApprovalGroupRepo(session).add_member(
                group.id, user_email="ops@example.com", role="approver", principal_ref=SUB,
            )
            await session.commit()
        async with _client(app, _human()) as client:
            after = (await client.get("/api/tenant-settings/scope-enforcement/impact")).json()
        assert after["unresolved_legacy_actors"] == []
        detail = {d["principal_ref"]: d for d in after["observed_principals_detail"]}
        assert detail[SUB]["granted"] is True
        assert detail[SUB]["observed_as"] == ["ops@example.com"]

    @pytest.mark.asyncio
    async def test_a_site_scoped_grant_still_counts_as_granted(self):
        app, sessionmaker = await _stack()
        async with sessionmaker() as session:
            site = CCSite(tenant_id=TENANT, site_name="s", sm_endpoint="sm:1", sm_token="t")
            session.add(site)
            await session.flush()
            await ScopeGrantRepo(session).grant(
                tenant_id=TENANT, principal_type="user", principal_ref="op-sub",
                scope_type=SCOPE_SITE, scope_ref=site.id, role="operator", granted_by="seed",
            )
            await AuditRepo(session).append(actor="op@example.com", actor_ref="op-sub",
                                            action="new", tenant_id=TENANT)
            await session.commit()
        async with _client(app, _human()) as client:
            report = (await client.get("/api/tenant-settings/scope-enforcement/impact")).json()
        assert report["observed_principals_without_grant"] == []
        assert report["unresolved_legacy_actors"] == []


class TestSchema:
    def test_actor_ref_is_indexed_with_the_tenant(self):
        table = CCAuditLog.__table__
        names = {i.name: [c.name for c in i.columns] for i in table.indexes}
        assert names.get("ix_cc_audit_log_tenant_actor_ref") == ["tenant_id", "actor_ref"]
        assert table.c.actor_ref.nullable is True
        assert table.c.actor_ref.type.length >= 128
