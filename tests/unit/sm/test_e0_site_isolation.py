"""E0.2: two sites on one Site Manager, and neither can see the other.

The gate for this slice. Before E0.2 a single Site Manager answered
every site's poll with everything it knew:

  devices          matched on the wrong id space, then fell back to ALL
  incidents        never scoped
  pending actions  never scoped
  outcomes         never scoped, AND the watermark consumed them
  candidate skills never scoped, AND the watermark consumed them
  usage            counted the whole fleet, labelled with one site

The last two are worse than leakage: one site's poll marked another
site's rows reported, so that site never received its own evidence.

Every assertion below is made symmetrically, A against B and B against
A, because a scoping bug that happens to favour the first site is still
a scoping bug.
"""

from __future__ import annotations

import pytest

from harkeniq.proto import harkeniq_pb2
from harkeniq_sm.approvals import ApprovalService
from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.models import (
    ActionOutcomeRow,
    ActionRow,
    CandidateSkillRow,
    Device,
    ErrorBudgetRow,
    Incident,
    Site,
)
from harkeniq_sm.grpc_server import SiteManagerServiceServicer

CC_A = "cc-site-alpha"
CC_B = "cc-site-beta"


def _config(**kw):
    defaults = dict(insecure=True, site_name="alpha", grpc_port=0)
    defaults.update(kw)
    return SMConfig(**defaults)


async def _two_sites(db):
    """One Site Manager, two bound sites, each with its own everything."""
    async with db() as session:
        alpha = Site(name="alpha", cc_site_id=CC_A)
        beta = Site(name="beta", cc_site_id=CC_B)
        session.add_all([alpha, beta])
        await session.flush()

        dev_a = Device(site_id=alpha.id, agent_id="agent-alpha", agent_name="a1")
        dev_b = Device(site_id=beta.id, agent_id="agent-beta", agent_name="b1")
        session.add_all([dev_a, dev_b])
        await session.flush()

        session.add_all([
            Incident(site_id=alpha.id, kind="device", status="open",
                     device_id=dev_a.id, subsystem="fan", title="alpha fan"),
            Incident(site_id=beta.id, kind="device", status="open",
                     device_id=dev_b.id, subsystem="psu", title="beta psu"),
            ActionRow(device_id=dev_a.id, agent_action_id="aa-1",
                      type="SEL_CLEAR", status="pending", sensor_id="log:sel"),
            ActionRow(device_id=dev_b.id, agent_action_id="bb-1",
                      type="BMC_RESET", status="pending", sensor_id="log:sel"),
            ActionOutcomeRow(action_id="oa-1", action_type="SEL_CLEAR",
                             device_id=dev_a.id, outcome="SUCCESS",
                             actor="op-agent:alpha@v1"),
            ActionOutcomeRow(action_id="ob-1", action_type="SEL_CLEAR",
                             device_id=dev_b.id, outcome="FAILURE",
                             actor="op-agent:beta@v1"),
            CandidateSkillRow(skill_id="cand-alpha", yaml_text="name: a\n",
                              source_device="agent-alpha", validation_state="tested"),
            CandidateSkillRow(skill_id="cand-beta", yaml_text="name: b\n",
                              source_device="agent-beta", validation_state="tested"),
        ])
        await session.commit()
        return {"alpha": alpha.id, "beta": beta.id,
                "dev_a": dev_a.id, "dev_b": dev_b.id}


def _servicer(db):
    config = _config()
    return SiteManagerServiceServicer(db, ApprovalService(db, config), config)


async def _snap(db, cc_site_id):
    return await _servicer(db).GetFleetSnapshot(
        harkeniq_pb2.FleetSnapshotRequest(tenant_id="t1", site_id=cc_site_id),
        None,
    )


class TestSnapshotIsolation:
    async def test_devices(self, db):
        await _two_sites(db)
        a, b = await _snap(db, CC_A), await _snap(db, CC_B)
        assert [d.agent_id for d in a.devices] == ["agent-alpha"]
        assert [d.agent_id for d in b.devices] == ["agent-beta"]

    async def test_incidents(self, db):
        await _two_sites(db)
        a, b = await _snap(db, CC_A), await _snap(db, CC_B)
        assert [i.title for i in a.incidents] == ["alpha fan"]
        assert [i.title for i in b.incidents] == ["beta psu"]

    async def test_pending_actions(self, db):
        await _two_sites(db)
        a, b = await _snap(db, CC_A), await _snap(db, CC_B)
        assert [x.type for x in a.pending_actions] == ["SEL_CLEAR"]
        assert [x.type for x in b.pending_actions] == ["BMC_RESET"]

    async def test_outcomes(self, db):
        await _two_sites(db)
        a, b = await _snap(db, CC_A), await _snap(db, CC_B)
        assert [o.action_id for o in a.outcomes] == ["oa-1"]
        assert [o.action_id for o in b.outcomes] == ["ob-1"]

    async def test_candidate_skills(self, db):
        await _two_sites(db)
        a, b = await _snap(db, CC_A), await _snap(db, CC_B)
        assert [c.skill_id for c in a.candidate_skills] == ["cand-alpha"]
        assert [c.skill_id for c in b.candidate_skills] == ["cand-beta"]


class TestWatermarksAreNotConsumedAcrossSites:
    """The destructive half: polling A must not spend B's evidence."""

    async def test_polling_alpha_leaves_beta_outcomes_unreported(self, db):
        await _two_sites(db)
        assert [o.action_id for o in (await _snap(db, CC_A)).outcomes] == ["oa-1"]
        # Beta has still never been polled, so its outcome must survive.
        beta = await _snap(db, CC_B)
        assert [o.action_id for o in beta.outcomes] == ["ob-1"]

    async def test_polling_beta_leaves_alpha_outcomes_unreported(self, db):
        await _two_sites(db)
        assert [o.action_id for o in (await _snap(db, CC_B)).outcomes] == ["ob-1"]
        alpha = await _snap(db, CC_A)
        assert [o.action_id for o in alpha.outcomes] == ["oa-1"]

    async def test_polling_alpha_leaves_beta_candidates_unreported(self, db):
        await _two_sites(db)
        assert [c.skill_id for c in (await _snap(db, CC_A)).candidate_skills] == [
            "cand-alpha"
        ]
        beta = await _snap(db, CC_B)
        assert [c.skill_id for c in beta.candidate_skills] == ["cand-beta"]

    async def test_each_site_still_reports_its_own_rows_exactly_once(self, db):
        await _two_sites(db)
        assert len((await _snap(db, CC_A)).outcomes) == 1
        assert len((await _snap(db, CC_A)).outcomes) == 0
        # Beta is unaffected by alpha having drained its own.
        assert len((await _snap(db, CC_B)).outcomes) == 1


class TestUsageIsolation:
    async def test_each_site_meters_only_its_own_nodes(self, db):
        await _two_sites(db)
        servicer = _servicer(db)
        for cc_id in (CC_A, CC_B):
            snap = await servicer.GetUsageSnapshot(
                harkeniq_pb2.UsageSnapshotRequest(
                    tenant_id="t1", site_id=cc_id, date="2026-08-30",
                ),
                None,
            )
            assert snap.node_count == 1, (
                f"{cc_id} metered {snap.node_count} nodes; a Site Manager "
                f"serving two sites must not bill each of them for both"
            )


class TestErrorBudgetIsolation:
    """Autonomy is earned on evidence, and one site's evidence is not
    another's."""

    async def test_a_drop_back_at_one_site_does_not_withdraw_the_other(self, db):
        ids = await _two_sites(db)
        async with db() as session:
            session.add(ErrorBudgetRow(
                site_id=ids["alpha"], action_type="SEL_CLEAR",
                success_count=1, failure_count=19, total_count=20,
                dropped_back=True,
            ))
            await session.commit()

        a, b = await _snap(db, CC_A), await _snap(db, CC_B)
        a_dropped = {e.action_type for e in a.safety.error_budgets if e.dropped_back}
        b_dropped = {e.action_type for e in b.safety.error_budgets if e.dropped_back}
        assert a_dropped == {"SEL_CLEAR"}
        assert b_dropped == set(), (
            "beta's autonomy was withdrawn by alpha's failures"
        )

    async def test_outcomes_build_the_budget_of_their_own_site(self, db):
        from harkeniq_sm.db.repos import ErrorBudgetRepo
        from harkeniq_sm.knowledge import MIN_OUTCOMES_TO_JUDGE
        from harkeniq_sm.outcomes import record_action_outcome

        ids = await _two_sites(db)
        async with db() as session:
            for i in range(MIN_OUTCOMES_TO_JUDGE):
                await record_action_outcome(
                    session, device_id=ids["dev_a"], action_id=f"x-{i}",
                    action_type="SEL_CLEAR", result="FAILURE",
                )
            await session.commit()

        async with db() as session:
            repo = ErrorBudgetRepo(session)
            assert "SEL_CLEAR" in await repo.dropped_back_types(ids["alpha"])
            assert "SEL_CLEAR" not in await repo.dropped_back_types(ids["beta"])

    async def test_recovery_lifts_one_site_only(self, db):
        from harkeniq_sm.db.repos import ErrorBudgetRepo

        ids = await _two_sites(db)
        async with db() as session:
            for site_id in (ids["alpha"], ids["beta"]):
                session.add(ErrorBudgetRow(
                    site_id=site_id, action_type="SEL_CLEAR",
                    success_count=1, failure_count=19, total_count=20,
                    dropped_back=True,
                ))
            await session.commit()

        async with db() as session:
            assert await ErrorBudgetRepo(session).recover(
                ids["alpha"], "SEL_CLEAR",
            ) is True
            await session.commit()

        async with db() as session:
            repo = ErrorBudgetRepo(session)
            assert await repo.dropped_back_types(ids["alpha"]) == set()
            assert await repo.dropped_back_types(ids["beta"]) == {"SEL_CLEAR"}


class TestNoBroadeningEver:
    async def test_an_unknown_site_gets_nothing(self, db):
        await _two_sites(db)
        snap = await _snap(db, "cc-site-does-not-exist")
        assert snap.site_resolved is False
        assert snap.site_reason
        for collection in (
            snap.devices, snap.incidents, snap.pending_actions,
            snap.outcomes, snap.candidate_skills,
        ):
            assert len(collection) == 0

    async def test_an_empty_site_id_gets_nothing(self, db):
        """The pre-E0.2 shape: CC sends an id the SM cannot match."""
        await _two_sites(db)
        snap = await _snap(db, "")
        assert snap.site_resolved is False
        assert len(snap.devices) == 0

    async def test_a_retired_site_is_not_served(self, db):
        await _two_sites(db)
        async with db() as session:
            from harkeniq_sm.db.repos import SiteRepo

            site = await SiteRepo(session).get_by_name("beta")
            site.status = "retired"
            await session.commit()
        snap = await _snap(db, CC_B)
        assert snap.site_resolved is False
        assert "retired" in snap.site_reason
        assert len(snap.devices) == 0
