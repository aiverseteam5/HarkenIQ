"""A6-3 / A27: proposal provenance and ingress operability.

A6-1 built the external runtime's write path, A6-2 its read path. Neither
built the OPERATOR's side: HarkenIQ accepted governed work from an
external runtime that a human could not distinguish, diagnose or
supervise.

`origin` reached `admit_proposal()` and landed only in audit-entry detail
-- no column, no API payload -- while `/api/approvals/` already spent the
word `origin` on the queue LANE, so an approver saw `"agent"` for a
proposal HarkenIQ reasoned itself AND for one an external runtime asked
for. And the three A6 ingress tables were written by the meter and the
submit route and read by nothing.

Five properties carry this module:

* **Provenance is persisted with the proposal**, in the same
  transaction, and is never inferred from the audit log afterwards.
* **Unknown is never manufactured.** A pre-A6-3 row reads NULL and
  projects `unknown`; it is never turned into `evaluator`.
* **It is projected where the DECISION is made** -- the approvals queue
  -- carrying the type and, for an external submission, the submission
  id, and nothing else that could identify a credential.
* **Ingress health is governed like every other agent read**: effective
  scope, grant lifecycle, machine-self.
* **Nothing invents connectivity**, and the derived `activity_state` has
  one deterministic answer for every input.

Driven against the persona matrix's production stack: nothing overrides
`get_scope`, every ALLOW had to be granted.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa

from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.auth import UserContext
from harkeniq_cc.db.models import (
    CCAgentIdentity, CCAgentProposal, CCAgentSubmission, CCOperationalAgent,
    CCScopeGrant,
)
from harkeniq_cc.provenance import (
    ACTIVITY_STATES, ORIGIN_EVALUATOR, ORIGIN_INGRESS, PROVENANCE_FIELDS,
    PROVENANCE_UNKNOWN, REFUSAL_SAMPLE, activity_state,
)
from harkeniq_cc.scope import SCOPE_SITE, SCOPE_TENANT
from harkeniq_cc.route_contract import MACHINE_JOBS

from tests.unit.cc.test_e1_persona_matrix import (
    TENANT, _client, _grant, _stack, _strict,
)

AGENT = "agent-A"
OTHER = "agent-B"
INGRESS = "/api/operational-agents/{}/ingress"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _agents(sessionmaker, site_id: str) -> None:
    async with sessionmaker() as session:
        for aid in (AGENT, OTHER):
            session.add(CCOperationalAgent(
                id=aid, tenant_id=TENANT, name=f"Agent {aid}", status="active",
                version=1, activated_version=1, created_by="kc-owner",
            ))
            session.add(CCScopeGrant(
                tenant_id=TENANT, principal_type="agent", principal_ref=aid,
                scope_type=SCOPE_SITE, scope_ref=site_id, granted_by="kc-owner",
            ))
        await session.commit()


async def _proposal(sessionmaker, *, site_id, provenance, agent=AGENT,
                    dedupe="k", status="awaiting_approval") -> str:
    async with sessionmaker() as session:
        row = CCAgentProposal(
            tenant_id=TENANT, agent_id=agent, actor=f"op-agent:{agent}@v1",
            agent_version=1, site_id=site_id, device_agent_id="node-site-1",
            action_type="SEL_CLEAR", params={"reason": "x"}, rationale="because",
            evidence={}, disposition="requires_approval",
            disposition_reason="needs a human", authorization_basis="human_approval",
            status=status, dedupe_key=dedupe, provenance_type=provenance,
        )
        session.add(row)
        await session.commit()
        return row.id


async def _submission(sessionmaker, *, proposal_id=None, agent=AGENT,
                      key="k", code="", reason="", when=None) -> str:
    async with sessionmaker() as session:
        row = CCAgentSubmission(
            tenant_id=TENANT, agent_id=agent, agent_version=1,
            idempotency_key=key, request_digest="d", candidate_ref="c",
            proposal_id=proposal_id, code=code, reason=reason,
        )
        if when is not None:
            row.created_at = when
        session.add(row)
        await session.commit()
        return row.id


async def _identity(sessionmaker, *, agent=AGENT, last_seen=None,
                    status="active", source="untrusted-source-string") -> None:
    async with sessionmaker() as session:
        session.add(CCAgentIdentity(
            tenant_id=TENANT, agent_id=agent, realm="tenant-demo",
            keycloak_client_id=f"op-agent-{agent}", keycloak_sub=f"sub-{agent}",
            status=status, issued_by="kc-owner", last_seen_at=last_seen,
            last_seen_source=source,
        ))
        await session.commit()


async def _attempts(sessionmaker, *, agent=AGENT, outcomes) -> None:
    from harkeniq_cc.db.models import CCAgentIngressAttempt

    async with sessionmaker() as session:
        for outcome, when in outcomes:
            row = CCAgentIngressAttempt(
                tenant_id=TENANT, agent_id=agent, outcome=outcome,
            )
            if when is not None:
                row.created_at = when
            session.add(row)
        await session.commit()


def _machine_client(app, agent_id=AGENT):
    from harkeniq_cc.machine_identity import machine_permissions

    async def _fake():
        return UserContext(
            user_id=agent_id, email=f"op-agent:{agent_id}@v1", tenant_id=TENANT,
            role="", species="agent", identity_id="id-1",
                machine_jobs=MACHINE_JOBS,
            permissions=machine_permissions(["fleet", "incidents"], ["proposals"]),
        )

    app.dependency_overrides[get_current_user] = _fake
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# ---------------------------------------------------------------------------
# 1. Provenance is persisted, transactionally, and never manufactured
# ---------------------------------------------------------------------------


class TestProvenanceIsPersisted:
    async def test_an_external_submission_persists_external_ingress(self):
        """A27.5: written in the SAME transaction as the proposal."""
        from harkeniq_cc.proposal_admission import admit_proposal

        _app, sm, estate = await _stack()
        async with sm() as session:
            row, code, _ = await admit_proposal(
                session, tenant_id=TENANT,
                payload=dict(
                    tenant_id=TENANT, agent_id=AGENT, actor="op-agent:agent-A@v1",
                    agent_version=1, site_id=estate.sites["site-1"],
                    device_agent_id="node-site-1", action_type="SEL_CLEAR",
                    params={"reason": "x"}, rationale="r", evidence={},
                    disposition="requires_approval", disposition_reason="d",
                    authorization_basis="human_approval",
                    status="awaiting_approval", dedupe_key="k-ext",
                ),
                origin=ORIGIN_INGRESS,
            )
            await session.commit()
        assert code == "", code
        assert row.provenance_type == "external_ingress"

    async def test_the_evaluator_persists_evaluator(self):
        from harkeniq_cc.proposal_admission import admit_proposal

        _app, sm, estate = await _stack()
        async with sm() as session:
            row, code, _ = await admit_proposal(
                session, tenant_id=TENANT,
                payload=dict(
                    tenant_id=TENANT, agent_id=AGENT, actor="op-agent:agent-A@v1",
                    agent_version=1, site_id=estate.sites["site-1"],
                    device_agent_id="node-site-1", action_type="SEL_CLEAR",
                    params={"reason": "x"}, rationale="r", evidence={},
                    disposition="requires_approval", disposition_reason="d",
                    authorization_basis="human_approval",
                    status="awaiting_approval", dedupe_key="k-int",
                ),
                origin=ORIGIN_EVALUATOR,
            )
            await session.commit()
        assert code == "", code
        assert row.provenance_type == "evaluator"

    async def test_a_refused_admission_writes_no_provenance_row_at_all(self):
        """No proposal, no provenance: there is nothing to attribute."""
        from harkeniq_cc.proposal_admission import admit_proposal

        _app, sm, estate = await _stack()
        payload = dict(
            tenant_id=TENANT, agent_id=AGENT, actor="op-agent:agent-A@v1",
            agent_version=1, site_id=estate.sites["site-1"],
            device_agent_id="node-site-1", action_type="SEL_CLEAR",
            params={"reason": "x"}, rationale="r", evidence={},
            disposition="requires_approval", disposition_reason="d",
            authorization_basis="human_approval", status="awaiting_approval",
            dedupe_key="k-dup",
        )
        async with sm() as session:
            await admit_proposal(session, tenant_id=TENANT, payload=dict(payload),
                                 origin=ORIGIN_INGRESS)
            await session.commit()
        async with sm() as session:
            row, code, _ = await admit_proposal(
                session, tenant_id=TENANT, payload=dict(payload),
                origin=ORIGIN_INGRESS,
            )
            await session.commit()
        assert row is None and code
        async with sm() as session:
            rows = (await session.execute(
                sa.select(CCAgentProposal))).scalars().all()
        assert len(rows) == 1

    async def test_a_historical_proposal_reads_unknown_and_is_never_backfilled(self):
        """A27.4. NULL in storage, `unknown` in the projection."""
        from harkeniq_cc.provenance import provenance_block, provenance_type

        _app, sm, estate = await _stack()
        pid = await _proposal(sm, site_id=estate.sites["site-1"],
                              provenance=None, dedupe="k-old")
        async with sm() as session:
            row = (await session.execute(
                sa.select(CCAgentProposal).where(CCAgentProposal.id == pid)
            )).scalars().one()
        assert row.provenance_type is None, "a historical row was backfilled"
        assert provenance_type(row) == PROVENANCE_UNKNOWN
        assert provenance_block(row) == {"type": "unknown"}

    async def test_an_unrecognised_discriminator_reads_unknown(self):
        """A value this code does not know is exactly as unknown as none.

        Hostile input: a future writer must not be able to put an
        unvetted string on a human decision surface.
        """
        from harkeniq_cc.provenance import provenance_block

        class _P:
            provenance_type = "<script>whatever</script>"

        assert provenance_block(_P()) == {"type": "unknown"}

    def test_the_vocabulary_is_closed_and_canonical(self):
        from harkeniq_cc.proposal_admission import PROVENANCE_TYPES

        assert PROVENANCE_TYPES == frozenset(
            {"evaluator", "external_ingress", "unknown"}
        )
        assert ORIGIN_INGRESS == "external_ingress", (
            "A27.3: the canonical value is not shortened to `ingress`"
        )

    def test_provenance_never_carries_identity_material(self):
        """A27.6: built by NAMING what may pass."""
        from harkeniq_cc.provenance import provenance_block

        class _P:
            provenance_type = "external_ingress"

        block = provenance_block(_P(), "sub-1")
        assert set(block) <= PROVENANCE_FIELDS
        assert set(block) == {"type", "submission_id"}
        for forbidden in ("client_id", "client_secret", "secret", "token",
                          "keycloak_sub", "realm", "identity_id"):
            assert forbidden not in block

    def test_the_submission_id_rides_only_with_an_external_proposal(self):
        from harkeniq_cc.provenance import provenance_block

        class _P:
            provenance_type = "evaluator"

        assert provenance_block(_P(), "sub-1") == {"type": "evaluator"}


# ---------------------------------------------------------------------------
# 2. Provenance where the decision is made
# ---------------------------------------------------------------------------


class TestProvenanceOnHumanSurfaces:
    async def test_the_approvals_queue_distinguishes_the_two(self):
        """THE point of the slice, on the surface where it is decided."""
        app, sm, estate = await _stack()
        await _strict(sm)
        site = estate.sites["site-1"]
        await _agents(sm, site)
        ext = await _proposal(sm, site_id=site, provenance="external_ingress",
                              dedupe="k-ext")
        internal = await _proposal(sm, site_id=site, provenance="evaluator",
                                   dedupe="k-int")
        legacy = await _proposal(sm, site_id=site, provenance=None,
                                 dedupe="k-old")
        sub = await _submission(sm, proposal_id=ext, key="k-ext")
        await _grant(sm, "kc-own", SCOPE_TENANT, "", role="tenant_owner")

        async with _client(app, "tenant_owner", "kc-own") as c:
            body = (await c.get("/api/approvals/")).json()
        by_id = {
            i["id"]: i for i in body["actions"] if i.get("origin") == "agent"
        }

        assert by_id[ext]["provenance"] == {
            "type": "external_ingress", "submission_id": sub,
        }
        assert by_id[internal]["provenance"] == {"type": "evaluator"}
        assert by_id[legacy]["provenance"] == {"type": "unknown"}
        # The queue LANE is unchanged -- A27.2 separated the questions
        # rather than renaming a shipped field.
        assert by_id[ext]["origin"] == "agent"

    async def test_the_agent_detail_view_carries_provenance(self):
        app, sm, estate = await _stack()
        await _strict(sm)
        site = estate.sites["site-1"]
        await _agents(sm, site)
        ext = await _proposal(sm, site_id=site, provenance="external_ingress",
                              dedupe="k-ext")
        sub = await _submission(sm, proposal_id=ext, key="k-ext")
        await _grant(sm, "kc-own", SCOPE_TENANT, "", role="tenant_owner")

        async with _client(app, "tenant_owner", "kc-own") as c:
            body = (await c.get(f"/api/operational-agents/{AGENT}")).json()
        item = next(p for p in body["proposals"] if p["proposal_id"] == ext)
        assert item["provenance"] == {
            "type": "external_ingress", "submission_id": sub,
        }

    async def test_the_page_resolves_submissions_in_one_query(self):
        """A27.6: one `IN` over the page, never one lookup per row."""
        from harkeniq_cc.db.repos import AgentSubmissionRepo

        _app, sm, estate = await _stack()
        site = estate.sites["site-1"]
        ids = []
        for n in range(5):
            pid = await _proposal(sm, site_id=site, provenance="external_ingress",
                                  dedupe=f"k-{n}")
            await _submission(sm, proposal_id=pid, key=f"k-{n}")
            ids.append(pid)

        seen = []
        async with sm() as session:
            repo = AgentSubmissionRepo(session)
            original = repo.for_proposals

            async def counting(tenant_id, proposal_ids):
                seen.append(list(proposal_ids))
                return await original(tenant_id, proposal_ids)

            repo.for_proposals = counting
            rows = await repo.for_proposals(TENANT, ids)
        assert len(seen) == 1 and len(seen[0]) == 5
        assert len(rows) == 5

    async def test_a_machine_projection_does_not_gain_provenance(self):
        """A27.7: an agent already knows it submitted."""
        app, sm, estate = await _stack()
        await _strict(sm)
        site = estate.sites["site-1"]
        await _agents(sm, site)
        await _proposal(sm, site_id=site, provenance="external_ingress",
                        dedupe="k-ext")
        async with _machine_client(app) as c:
            body = (await c.get(
                f"/api/operational-agents/{AGENT}/proposals")).json()
        assert body["view"] == "machine"
        assert body["proposals"], "the machine list lost its own proposals"
        assert "provenance" not in body["proposals"][0]


# ---------------------------------------------------------------------------
# 3. activity_state: deterministic precedence (A27.11)
# ---------------------------------------------------------------------------


class TestActivityStateIsDeterministic:
    NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

    def _state(self, **kw):
        base = dict(last_authenticated_at=None, last_attempt_at=None,
                    accepted=0, refused=0, throttled=0, now=self.NOW)
        base.update(kw)
        return activity_state(**base)

    def test_never_authenticated_wins_when_nothing_was_ever_observed(self):
        assert self._state() == "never_authenticated"

    def test_throttled_outranks_refused_and_recency(self):
        """Overlapping conditions must produce ONE answer."""
        assert self._state(
            last_authenticated_at=self.NOW, last_attempt_at=self.NOW,
            accepted=0, refused=5, throttled=1,
        ) == "throttled"

    def test_repeatedly_refused_outranks_recency(self):
        assert self._state(
            last_attempt_at=self.NOW, refused=3, accepted=0,
        ) == "repeatedly_refused"

    def test_one_acceptance_clears_repeatedly_refused(self):
        assert self._state(
            last_attempt_at=self.NOW, refused=3, accepted=1,
        ) == "active_recently"

    @pytest.mark.parametrize("age_s,expected", [
        (0, "active_recently"),
        (3599, "active_recently"),
        (3601, "idle"),
        (86399, "idle"),
        (86401, "no_recent_activity"),
    ])
    def test_the_recency_boundaries(self, age_s, expected):
        assert self._state(
            last_authenticated_at=self.NOW - timedelta(seconds=age_s),
        ) == expected

    def test_authentication_alone_counts_as_an_observation(self):
        """A runtime that authenticates and submits nothing is visible."""
        assert self._state(last_authenticated_at=self.NOW) == "active_recently"

    def test_every_answer_is_in_the_declared_set(self):
        """A derived state may never invent a value a consumer cannot map."""
        for auth in (None, self.NOW, self.NOW - timedelta(days=3)):
            for attempt in (None, self.NOW, self.NOW - timedelta(days=3)):
                for acc in (0, 2):
                    for ref in (0, 2):
                        for thr in (0, 1):
                            assert self._state(
                                last_authenticated_at=auth,
                                last_attempt_at=attempt,
                                accepted=acc, refused=ref, throttled=thr,
                            ) in ACTIVITY_STATES

    def test_naive_and_aware_timestamps_agree(self):
        """sqlite returns naive, PostgreSQL aware. One answer either way."""
        naive = self.NOW.replace(tzinfo=None)
        assert self._state(last_authenticated_at=naive) == self._state(
            last_authenticated_at=self.NOW)


# ---------------------------------------------------------------------------
# 4. Ingress health: contract, authorization, bounding
# ---------------------------------------------------------------------------


class TestIngressHealth:
    async def _estate(self):
        app, sm, estate = await _stack()
        await _strict(sm)
        site = estate.sites["site-1"]
        await _agents(sm, site)
        await _identity(sm, last_seen=datetime.now(timezone.utc))
        await _attempts(sm, outcomes=[
            ("accepted", None), ("accepted", None), ("rejected", None),
            ("replayed", None),
        ])
        pid = await _proposal(sm, site_id=site, provenance="external_ingress",
                              dedupe="k-ok")
        await _submission(sm, proposal_id=pid, key="k-ok")
        for n in range(3):
            await _submission(sm, key=f"k-no-{n}", code="candidate_not_current",
                              reason="this candidate is not current")
        return app, sm, site

    async def test_an_operator_in_scope_sees_the_contract(self):
        app, sm, site = await self._estate()
        await _grant(sm, "kc-own", SCOPE_TENANT, "", role="tenant_owner")
        async with _client(app, "tenant_owner", "kc-own") as c:
            res = await c.get(INGRESS.format(AGENT))
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["agent_id"] == AGENT
        assert body["credentialed"] is True
        assert body["identity_status"] == "active"
        act = body["submission_activity"]
        assert act["accepted"] == 2
        assert act["refused"] == 1
        assert act["attempts"] == 4
        assert act["last_accepted_at"], "an accepted submission was not seen"
        assert body["read_throttle"]["limit"] > 0
        assert len(body["recent_refusals"]) == 3
        assert body["recent_refusals"][0]["code"] == "candidate_not_current"
        assert body["activity_state"] in ACTIVITY_STATES

    async def test_it_never_claims_a_connection(self):
        """A27.10: HarkenIQ holds no heartbeat and asserts none.

        Judged on the FIELDS and the derived state, not on the prose --
        the `governs` sentence deliberately contains the word
        "connection" in order to say there isn't one, and testing prose
        is the mistake A25 already made once.
        """
        app, sm, _site = await self._estate()
        await _grant(sm, "kc-own", SCOPE_TENANT, "", role="tenant_owner")
        async with _client(app, "tenant_owner", "kc-own") as c:
            body = (await c.get(INGRESS.format(AGENT))).json()

        def keys(node, out):
            if isinstance(node, dict):
                for k, v in node.items():
                    out.add(k)
                    keys(v, out)
            elif isinstance(node, list):
                for i in node:
                    keys(i, out)
            return out

        for forbidden in ("connected", "connection", "online", "session_id",
                          "heartbeat", "last_seen_source"):
            assert forbidden not in keys(body, set()), forbidden
        assert "last_authenticated_at" in body
        assert body["activity_state"] in ACTIVITY_STATES

    async def test_the_caller_supplied_source_string_is_never_exposed(self):
        """A27.10: `last_seen_source` is caller-supplied text."""
        import json

        app, sm, _site = await self._estate()
        await _grant(sm, "kc-own", SCOPE_TENANT, "", role="tenant_owner")
        async with _client(app, "tenant_owner", "kc-own") as c:
            body = (await c.get(INGRESS.format(AGENT))).json()
        assert "untrusted-source-string" not in json.dumps(body)
        assert "last_seen_source" not in json.dumps(body)

    async def test_last_authenticated_at_comes_from_the_real_touch_path(self):
        """It must be the column `AgentIdentityRepo.touch()` writes."""
        from harkeniq_cc.db.repos import AgentIdentityRepo

        app, sm, _site = await self._estate()
        await _grant(sm, "kc-own", SCOPE_TENANT, "", role="tenant_owner")
        async with sm() as session:
            row = await AgentIdentityRepo(session).get_for_agent(TENANT, AGENT)
            await AgentIdentityRepo(session).touch(row, "test-source")
            await session.commit()
            expected = row.last_seen_at
        async with _client(app, "tenant_owner", "kc-own") as c:
            body = (await c.get(INGRESS.format(AGENT))).json()
        assert body["last_authenticated_at"].startswith(
            expected.isoformat()[:19]
        )

    async def test_an_agent_with_no_credential_says_so(self):
        app, sm, estate = await _stack()
        await _strict(sm)
        await _agents(sm, estate.sites["site-1"])
        await _grant(sm, "kc-own", SCOPE_TENANT, "", role="tenant_owner")
        async with _client(app, "tenant_owner", "kc-own") as c:
            body = (await c.get(INGRESS.format(AGENT))).json()
        assert body["credentialed"] is False
        assert body["identity_status"] == "none"
        assert body["last_authenticated_at"] is None
        assert body["activity_state"] == "never_authenticated"

    async def test_the_refusal_sample_is_bounded(self):
        """A27.9: the durable ledger is never scanned whole."""
        app, sm, _estate = await self._estate()
        for n in range(REFUSAL_SAMPLE + 15):
            await _submission(sm, key=f"k-many-{n}", code="duplicate",
                              reason="x" * 5000)
        await _grant(sm, "kc-own", SCOPE_TENANT, "", role="tenant_owner")
        async with _client(app, "tenant_owner", "kc-own") as c:
            body = (await c.get(INGRESS.format(AGENT))).json()
        assert len(body["recent_refusals"]) == REFUSAL_SAMPLE
        assert body["recent_refusal_limit"] == REFUSAL_SAMPLE
        # And the projected reason is truncated, so a long stored reason
        # cannot become an unbounded payload.
        assert all(len(r["reason"]) <= 256 for r in body["recent_refusals"])

    async def test_the_refusal_sample_is_newest_first(self):
        app, sm, _estate = await self._estate()
        now = datetime.now(timezone.utc)
        await _submission(sm, key="k-old", code="old",
                          when=now - timedelta(days=2))
        await _submission(sm, key="k-new", code="new", when=now)
        await _grant(sm, "kc-own", SCOPE_TENANT, "", role="tenant_owner")
        async with _client(app, "tenant_owner", "kc-own") as c:
            body = (await c.get(INGRESS.format(AGENT))).json()
        assert body["recent_refusals"][0]["code"] == "new"

    async def test_the_counts_are_bounded_to_the_attempt_window(self):
        """An attempt older than the window is not counted."""
        from harkeniq_cc.ingress_limits import ATTEMPT_WINDOW_S

        app, sm, _estate = await self._estate()
        old = datetime.now(timezone.utc) - timedelta(
            seconds=ATTEMPT_WINDOW_S * 3)
        await _attempts(sm, outcomes=[("accepted", old)] * 7)
        await _grant(sm, "kc-own", SCOPE_TENANT, "", role="tenant_owner")
        async with _client(app, "tenant_owner", "kc-own") as c:
            body = (await c.get(INGRESS.format(AGENT))).json()
        assert body["submission_activity"]["accepted"] == 2, (
            "attempts outside the window were counted"
        )
        assert body["submission_activity"]["window_seconds"] == ATTEMPT_WINDOW_S

    async def test_reading_health_does_not_spend_the_agents_allowance(self):
        """`usage` reads the counter; `admit_read` remains the only writer."""
        from harkeniq_cc.db.models import CCAgentReadWindow

        app, sm, _estate = await self._estate()
        await _grant(sm, "kc-own", SCOPE_TENANT, "", role="tenant_owner")
        async with _client(app, "tenant_owner", "kc-own") as c:
            await c.get(INGRESS.format(AGENT))
            await c.get(INGRESS.format(AGENT))
        async with sm() as session:
            rows = (await session.execute(
                sa.select(CCAgentReadWindow))).scalars().all()
        assert rows == [], "an operator's read charged the agent's bucket"


class TestIngressHealthAuthorization:
    async def _estate(self):
        app, sm, estate = await _stack()
        await _strict(sm)
        site = estate.sites["site-1"]
        await _agents(sm, site)
        await _identity(sm, last_seen=datetime.now(timezone.utc))
        return app, sm, estate

    async def test_a_scoped_operator_reads_an_agent_within_their_scope(self):
        app, sm, estate = await self._estate()
        await _grant(sm, "kc-op", SCOPE_SITE, estate.sites["site-1"],
                     role="operator")
        async with _client(app, "operator", "kc-op") as c:
            res = await c.get(INGRESS.format(AGENT))
        assert res.status_code == 200, res.text

    async def test_a_scoped_operator_cannot_read_an_out_of_scope_agent(self):
        """Absent, not refused -- the canonical READ_SCOPED shape."""
        app, sm, estate = await self._estate()
        async with sm() as session:
            session.add(CCOperationalAgent(
                id="agent-far", tenant_id=TENANT, name="Far", status="active",
                version=1, created_by="kc-owner",
            ))
            session.add(CCScopeGrant(
                tenant_id=TENANT, principal_type="agent",
                principal_ref="agent-far", scope_type=SCOPE_SITE,
                scope_ref=estate.sites["site-3"], granted_by="kc-owner",
            ))
            await session.commit()
        await _grant(sm, "kc-op", SCOPE_SITE, estate.sites["site-1"],
                     role="operator")
        async with _client(app, "operator", "kc-op") as c:
            res = await c.get(INGRESS.format("agent-far"))
        assert res.status_code == 404, res.status_code

    @pytest.mark.parametrize("case", ["revoked", "expired", "inert"])
    async def test_an_ended_or_inert_grant_fails_closed(self, case):
        app, sm, _estate = await self._estate()
        past = datetime.now(timezone.utc) - timedelta(days=1)
        kwargs = {
            "revoked": dict(scope_type=SCOPE_TENANT, scope_ref="",
                            revoked_at=past),
            "expired": dict(scope_type=SCOPE_TENANT, scope_ref="",
                            expires_at=past),
            "inert": dict(scope_type="org_unit", scope_ref="unit-vanished"),
        }[case]
        async with sm() as session:
            session.add(CCScopeGrant(
                tenant_id=TENANT, principal_type="user",
                principal_ref=f"kc-{case}", granted_by="t", role="operator",
                **kwargs,
            ))
            await session.commit()
        async with _client(app, "operator", f"kc-{case}") as c:
            res = await c.get(INGRESS.format(AGENT))
        assert res.status_code == 404, f"{case}: {res.status_code}"

    async def test_a_grantless_principal_under_strict_is_refused(self):
        app, sm, _estate = await self._estate()
        async with _client(app, "operator", "kc-nobody") as c:
            res = await c.get(INGRESS.format(AGENT))
        assert res.status_code == 404

    async def test_a_machine_reads_its_OWN_ingress_health(self):
        app, sm, _estate = await self._estate()
        async with _machine_client(app, AGENT) as c:
            res = await c.get(INGRESS.format(AGENT))
        assert res.status_code == 200, res.text
        assert res.json()["agent_id"] == AGENT

    async def test_a_machine_cannot_read_another_agents_ingress_health(self):
        """A25.5, through the existing helper -- no new self rule."""
        app, sm, _estate = await self._estate()
        await _identity(sm, agent=OTHER, last_seen=datetime.now(timezone.utc))
        async with _machine_client(app, AGENT) as c:
            res = await c.get(INGRESS.format(OTHER))
        assert res.status_code == 403, res.status_code
        assert OTHER not in res.text or "may inspect its own" in res.text

    async def test_the_machine_read_is_metered_like_every_other(self):
        from harkeniq_cc.db.models import CCAgentReadWindow

        app, sm, _estate = await self._estate()
        async with _machine_client(app, AGENT) as c:
            await c.get(INGRESS.format(AGENT))
        async with sm() as session:
            total = sum(w.reads for w in (await session.execute(
                sa.select(CCAgentReadWindow))).scalars().all())
        assert total == 1, "a machine ingress read was unmetered"

    async def test_a_cross_tenant_agent_is_not_found(self):
        app, sm, _estate = await self._estate()
        async with sm() as session:
            session.add(CCOperationalAgent(
                id="agent-rival", tenant_id="t-rival", name="Rival",
                status="active", version=1, created_by="x",
            ))
            await session.commit()
        await _grant(sm, "kc-own", SCOPE_TENANT, "", role="tenant_owner")
        async with _client(app, "tenant_owner", "kc-own") as c:
            res = await c.get(INGRESS.format("agent-rival"))
        assert res.status_code == 404


# ---------------------------------------------------------------------------
# 5. Structural: the contract describes runtime truth
# ---------------------------------------------------------------------------


class TestTheContractIsTruthful:
    ROUTE = ("GET", "/api/operational-agents/{agent_id}/ingress")

    def test_the_route_is_declared_and_scoped(self):
        from harkeniq_cc.route_contract import READ_SCOPED, ROUTE_CONTRACT

        perm, treatment, audited = ROUTE_CONTRACT[self.ROUTE]
        assert perm == "fleet.view", (
            "A27.8: ingress health is operational state, not approval "
            "authority -- action.approve must not be required"
        )
        assert treatment == READ_SCOPED
        assert audited is False, "A26.9(2): read auditing does not exist yet"

    def test_the_handler_consumes_the_scope(self):
        from harkeniq_cc.route_contract import route_handlers, scope_consumption

        from tests.unit.cc.test_e1_route_contract import _app

        found = scope_consumption(route_handlers(_app())[self.ROUTE])
        assert found.declares and found.consumes

    def test_no_new_permission_entered_the_vocabulary(self):
        from harkeniq_console.permissions import PERMISSIONS

        assert len(PERMISSIONS) == 25, "A27.12: A6-3 adds no permission"

    def test_the_machine_ceiling_did_not_move(self):
        from harkeniq_cc.machine_identity import MACHINE_PRINCIPAL_CEILING

        assert MACHINE_PRINCIPAL_CEILING == frozenset({
            "fleet.view", "incident.view", "proposal.submit",
        })

    def test_provenance_is_written_inside_admit_proposal(self):
        """A27.5: one write path, in the admission transaction."""
        from harkeniq_cc import proposal_admission as mod

        code = ast.unparse(ast.parse(textwrap.dedent(
            inspect.getsource(mod.admit_proposal))))
        assert 'provenance_type' in code
        assert code.index("provenance_type") < code.index("repo.create"), (
            "provenance must be set before the row is created"
        )

    def test_no_second_provenance_writer_exists(self):
        """One fact, one writer -- proven over the AST, not by grep.

        A second place that sets provenance is how two answers to one
        question get born. The model's own column declaration is not a
        writer, and the tests are not production code.
        """
        import pathlib

        root = pathlib.Path("services/central_command/src/harkeniq_cc")
        writers: list[str] = []
        for path in root.rglob("*.py"):
            if path.name == "models.py":
                continue
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:  # pragma: no cover
                continue
            for node in ast.walk(tree):
                targets = (
                    node.targets if isinstance(node, ast.Assign)
                    else [node.target] if isinstance(node, ast.AnnAssign)
                    else []
                )
                for t in targets:
                    named = (
                        (isinstance(t, ast.Attribute) and t.attr == "provenance_type")
                        or (isinstance(t, ast.Subscript)
                            and isinstance(t.slice, ast.Constant)
                            and t.slice.value == "provenance_type")
                    )
                    if named:
                        writers.append(f"{path}:{node.lineno}")
        assert len(writers) == 1, f"expected one writer, found: {writers}"
        assert "proposal_admission" in writers[0], writers
