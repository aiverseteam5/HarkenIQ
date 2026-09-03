"""S6 campaigns: the governance properties, at Central Command.

Grouped by the invariant each defends, because these are the rules that
make a campaign safe rather than merely convenient:

  approval binds to a PLAN, not to a campaign
  APPROVED != EXECUTABLE != EXECUTED
  revalidation may narrow, never widen
  a halted site voids its own later authorizations, and only its own
  selection is not an execution payload, and not an authorization grant
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from harkeniq.capabilities import declare
from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.campaigns import (
    APPLICABILITY_ELIGIBLE,
    APPLICABILITY_EXCLUDED,
    APPLICABILITY_UNKNOWN,
    APPLICABILITY_WARN,
    REVAL_LOST_CAPABILITY,
    REVAL_NEWLY_DENIED,
    REVAL_OK,
    STATUS_PREFLIGHTED,
    WAVE_APPROVED,
    WAVE_AUTONOMOUS,
    WAVE_PENDING_APPROVAL,
    WAVE_VOIDED,
    campaign_terminal_state,
    can_seek_approval,
    narrow_only,
    plan_is_current,
    revalidate_target,
    target_applicability,
    wave_subject_ref,
    waves_to_void,
)
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCFleetCache, CCSite
from harkeniq_cc.runtime import AppState

from tests.unit.cc.conftest import seed_tenant_admin

TENANT = "t1"
SERVER = declare("redfish", ["IDENTIFY_LED", "COLLECT_DIAGNOSTICS"], "server")
NARROW = declare("redfish", ["COLLECT_DIAGNOSTICS"], "server")   # LED not permitted
SWITCH = declare("gnmi", ["INTERFACE_DISABLE"], "switch")


def _dev(agent_id, caps=SERVER, site="s1", cls="server"):
    return SimpleNamespace(
        agent_id=agent_id, agent_name=agent_id, site_id=site,
        device_class=cls, capabilities=caps,
    )


# ---------------------------------------------------------------------------
# Applicability: capability vs policy vs unknown
# ---------------------------------------------------------------------------


class TestApplicability:
    def test_a_capable_permitted_device_is_eligible(self):
        v, _ = target_applicability(_dev("d1"), "IDENTIFY_LED", True)
        assert v == APPLICABILITY_ELIGIBLE

    def test_implemented_but_not_permitted_is_a_WARNING_not_an_exclusion(self):
        """Policy is not capability. A17.7 in campaign form."""
        v, reason = target_applicability(_dev("d1", NARROW), "IDENTIFY_LED", True)
        assert v == APPLICABILITY_WARN
        assert "does not currently permit" in reason

    def test_a_protocol_that_cannot_do_it_is_excluded(self):
        v, reason = target_applicability(
            _dev("sw1", SWITCH, cls="switch"), "IDENTIFY_LED", True
        )
        assert v == APPLICABILITY_EXCLUDED
        assert "protocol does not implement" in reason

    def test_platform_unimplemented_excludes_everything(self):
        v, reason = target_applicability(_dev("d1"), "INTERFACE_RESET", False)
        assert v == APPLICABILITY_EXCLUDED
        assert "no executor in this platform" in reason

    def test_an_undeclared_device_is_unknown_never_incapable(self):
        v, reason = target_applicability(_dev("d1", None), "IDENTIFY_LED", True)
        assert v == APPLICABILITY_UNKNOWN
        assert "not the same as incapable" in reason


# ---------------------------------------------------------------------------
# Approval binds to a plan
# ---------------------------------------------------------------------------


class TestApprovalBinding:
    def _ref(self, **kw):
        base = dict(
            campaign_id="c1", campaign_version=1, site_id="s1", wave_index=0,
            device_agent_ids=["d1", "d2"], plan_hash="h1",
        )
        base.update(kw)
        return wave_subject_ref(**base)

    def test_the_digest_fits_the_ledger_column(self):
        """subject_ref is String(64); a readable composite would not fit."""
        assert len(self._ref()) == 32

    @pytest.mark.parametrize("field,value", [
        ("campaign_id", "c2"),
        ("campaign_version", 2),
        ("site_id", "s2"),
        ("wave_index", 1),
        ("device_agent_ids", ["d1", "d3"]),
        ("plan_hash", "h2"),
    ])
    def test_every_binding_component_changes_the_subject(self, field, value):
        """All six, computed directly rather than transitively.

        This is what makes a stale approval structurally unable to
        authorize new work: it no longer addresses the subject at all.
        """
        assert self._ref() != self._ref(**{field: value})

    def test_device_order_does_not_change_the_subject(self):
        assert self._ref(device_agent_ids=["d2", "d1"]) == self._ref()


# ---------------------------------------------------------------------------
# Revalidation: narrow, refuse, never widen
# ---------------------------------------------------------------------------


class TestRevalidation:
    def test_a_still_capable_device_proceeds(self):
        v, _ = revalidate_target(_dev("d1"), "IDENTIFY_LED", True, False)
        assert v == REVAL_OK

    def test_a_device_that_lost_the_capability_is_skipped(self):
        v, reason = revalidate_target(
            _dev("d1", SWITCH), "IDENTIFY_LED", True, False
        )
        assert v == REVAL_LOST_CAPABILITY
        assert "capable when the campaign was approved" in reason

    def test_an_absent_device_is_skipped(self):
        v, _ = revalidate_target(None, "IDENTIFY_LED", True, False)
        assert v == "absent"

    def test_acknowledged_policy_denial_proceeds_to_the_node(self):
        """The node is the final policy authority and its refusal is
        evidence — that is what the acknowledgement bought."""
        v, _ = revalidate_target(_dev("d1", NARROW), "IDENTIFY_LED", True, True)
        assert v == REVAL_OK

    def test_unacknowledged_policy_denial_is_skipped(self):
        v, reason = revalidate_target(
            _dev("d1", NARROW), "IDENTIFY_LED", True, False
        )
        assert v == REVAL_NEWLY_DENIED
        assert "no acknowledgement covers it" in reason

    def test_narrow_only_never_adds(self):
        assert narrow_only({"a", "b"}, {"b", "c"}) == {"b"}
        assert narrow_only({"a"}, {"a", "b", "c"}) == {"a"}

    def test_narrow_only_can_empty_a_wave(self):
        assert narrow_only({"a", "b"}, set()) == set()


class TestPlanCurrency:
    def test_an_unchanged_plan_is_current(self):
        assert plan_is_current("h1", "h1") is True

    def test_a_changed_plan_is_not(self):
        assert plan_is_current("h1", "h2") is False

    def test_a_missing_stored_hash_fails_closed(self):
        """No stored plan must never read as 'the plan is fine'."""
        assert plan_is_current("", "") is False
        assert plan_is_current("", "h1") is False


# ---------------------------------------------------------------------------
# A halted site voids its OWN later authorizations
# ---------------------------------------------------------------------------


class TestVoiding:
    def _wave(self, site, idx, status):
        return SimpleNamespace(site_id=site, wave_index=idx, status=status)

    def test_authorized_but_unstarted_waves_are_voided(self):
        rows = [
            self._wave("s1", 1, WAVE_APPROVED),
            self._wave("s1", 2, WAVE_PENDING_APPROVAL),
            self._wave("s1", 3, WAVE_AUTONOMOUS),
        ]
        assert len(waves_to_void(rows, "s1")) == 3

    def test_finished_waves_are_left_alone(self):
        rows = [
            self._wave("s1", 0, "completed"),
            self._wave("s1", 1, "failed"),
            self._wave("s1", 2, WAVE_VOIDED),
        ]
        assert waves_to_void(rows, "s1") == []

    def test_another_sites_waves_are_untouched(self):
        """A halted site is not a halted campaign."""
        rows = [
            self._wave("s1", 1, WAVE_APPROVED),
            self._wave("s2", 1, WAVE_APPROVED),
        ]
        voided = waves_to_void(rows, "s1")
        assert [w.site_id for w in voided] == ["s1"]


class TestPartialSuccess:
    def _site(self, status):
        return SimpleNamespace(status=status)

    def test_all_completed_is_completed(self):
        assert campaign_terminal_state(
            [self._site("completed"), self._site("completed")]
        ) == "completed"

    def test_one_halted_is_halted_not_completed(self):
        """Partial success is never rounded up."""
        assert campaign_terminal_state(
            [self._site("completed"), self._site("halted")]
        ) == "halted"

    def test_still_running_is_not_terminal(self):
        assert campaign_terminal_state(
            [self._site("completed"), self._site("running")]
        ) is None


# ---------------------------------------------------------------------------
# Wired: selection is not a payload, and not a grant
# ---------------------------------------------------------------------------


async def _stack(role="tenant_owner"):
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sm = make_sessionmaker(engine)
    app = create_app(AppState(
        config=CCConfig(tenant_id=TENANT, insecure=True),
        engine=engine, sessionmaker=sm,
    ))

    # A23-5: a rowless tenant is STRICT now (A23.11). The acting
    # principal holds a real founding grant, the way tenant birth
    # seeds one (A23.14 D4), instead of being tenant-wide by the
    # `legacy_open` synthesis a missing row used to give.
    await seed_tenant_admin(sm, TENANT, f"kc-{role}", role=role)

    async def _fake():
        return UserContext(
            user_id=f"kc-{role}", email=f"{role}@example.com",
            tenant_id=TENANT, role=role,
            permissions=list(ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS["viewer"])),
        )

    app.dependency_overrides[get_current_user] = _fake
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ), sm


async def _seed(sm, devices=(("node-1", "server", SERVER),)):
    async with sm() as session:
        site = CCSite(
            tenant_id=TENANT, site_name="DC-1",
            sm_endpoint="sm:50051", sm_token="tok",
        )
        session.add(site)
        await session.flush()
        for agent_id, cls, caps in devices:
            session.add(CCFleetCache(
                site_id=site.id, agent_id=agent_id, agent_name=agent_id,
                vendor="Dell", model="R750", device_class=cls,
                observation="observed", health="OK", capabilities=caps,
            ))
        await session.commit()
        return site.id


def _body(site_id, action="IDENTIFY_LED", name="Q3 rollout"):
    return {
        "name": name, "description": "s6 test", "action_type": action,
        "params": {"target": "Drive 0"},
        "scopes": [{"scope_type": "site", "scope_ref": site_id}],
    }


class TestSelectionIsNotAPayload:
    @pytest.mark.asyncio
    async def test_scope_rows_never_enter_the_action_params(self):
        """Regression for a defect found reconciling the first cut.

        Scopes were stored in `params`, and the dispatcher sends `params`
        to the node verbatim — so governance selection would have shipped
        to every device as execution parameters.
        """
        client, sm = await _stack()
        site_id = await _seed(sm)
        created = await client.post("/api/campaigns/", json=_body(site_id))
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["params"] == {"target": "Drive 0"}
        assert "__scopes__" not in body["params"]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_scope_rows_live_in_their_own_table(self):
        from harkeniq_cc.db.repos import CampaignRepo

        client, sm = await _stack()
        site_id = await _seed(sm)
        cid = (await client.post("/api/campaigns/", json=_body(site_id))).json()["id"]
        async with sm() as session:
            rows = await CampaignRepo(session).scopes(cid)
        assert [(r.scope_type, r.scope_ref) for r in rows] == [("site", site_id)]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_creating_a_campaign_grants_no_authorization(self):
        """Selection is not an authorization grant: `cc_scope_grants` is
        untouched, so building a campaign cannot widen anybody's reach."""
        from sqlalchemy import func, select

        from harkeniq_cc.db.models import CCScopeGrant

        client, sm = await _stack()
        site_id = await _seed(sm)
        async with sm() as session:
            before = (
                await session.execute(select(func.count(CCScopeGrant.id)))
            ).scalar()
        await client.post("/api/campaigns/", json=_body(site_id))
        async with sm() as session:
            after = (
                await session.execute(select(func.count(CCScopeGrant.id)))
            ).scalar()
        assert before == after
        await client.aclose()


class TestCreationGovernance:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["INTERFACE_RESET", "CLEAR_COUNTERS"])
    async def test_a_class_no_executor_implements_is_refused(self, action):
        """Never discovered after dispatch — refused at creation."""
        client, sm = await _stack()
        site_id = await _seed(sm)
        r = await client.post("/api/campaigns/", json=_body(site_id, action))
        assert r.status_code == 400
        assert "no executor in this platform implements" in r.json()["detail"]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_an_unknown_action_class_is_refused(self):
        client, sm = await _stack()
        site_id = await _seed(sm)
        r = await client.post("/api/campaigns/", json=_body(site_id, "REBOOT_ALL"))
        assert r.status_code == 400
        await client.aclose()

    @pytest.mark.asyncio
    async def test_a_campaign_with_no_scope_is_refused(self):
        client, sm = await _stack()
        await _seed(sm)
        body = _body("x")
        body["scopes"] = []
        r = await client.post("/api/campaigns/", json=body)
        assert r.status_code == 400
        await client.aclose()

    @pytest.mark.asyncio
    async def test_an_operator_cannot_create_a_campaign(self):
        """Deciding is not configuring."""
        client, sm = await _stack(role="operator")
        site_id = await _seed(sm)
        r = await client.post("/api/campaigns/", json=_body(site_id))
        assert r.status_code == 403
        await client.aclose()

    @pytest.mark.asyncio
    async def test_an_auditor_can_read_but_not_create(self):
        client, sm = await _stack(role="auditor")
        site_id = await _seed(sm)
        assert (await client.get("/api/campaigns/")).status_code == 200
        assert (
            await client.post("/api/campaigns/", json=_body(site_id))
        ).status_code == 403
        await client.aclose()


class TestApprovalGate:
    @pytest.mark.asyncio
    async def test_a_campaign_cannot_be_submitted_without_preflight(self):
        client, sm = await _stack()
        site_id = await _seed(sm)
        cid = (await client.post("/api/campaigns/", json=_body(site_id))).json()["id"]
        r = await client.post(f"/api/campaigns/{cid}/submit")
        assert r.status_code == 409
        # A draft has never been preflighted, so it is refused on status.
        # Preflight is mandatory; there is no path to approval around it.
        assert "cannot seek approval" in r.json()["detail"]
        await client.aclose()

    def test_warned_targets_block_approval_until_settled(self):
        campaign = SimpleNamespace(
            status=STATUS_PREFLIGHTED, preflight_at=object(), version=1,
            acknowledged_by="", acknowledged_version=0,
        )
        targets = [SimpleNamespace(applicability=APPLICABILITY_WARN)]
        ok, reason = can_seek_approval(campaign, targets)
        assert ok is False
        assert "exclude or acknowledge" in reason

    def test_an_acknowledgement_for_an_older_version_does_not_count(self):
        campaign = SimpleNamespace(
            status=STATUS_PREFLIGHTED, preflight_at=object(), version=2,
            acknowledged_by="ops@example.com", acknowledged_version=1,
        )
        targets = [SimpleNamespace(applicability=APPLICABILITY_WARN)]
        ok, _ = can_seek_approval(campaign, targets)
        assert ok is False

    def test_a_current_acknowledgement_unblocks(self):
        campaign = SimpleNamespace(
            status=STATUS_PREFLIGHTED, preflight_at=object(), version=2,
            acknowledged_by="ops@example.com", acknowledged_version=2,
        )
        targets = [SimpleNamespace(applicability=APPLICABILITY_WARN)]
        ok, _ = can_seek_approval(campaign, targets)
        assert ok is True

    def test_no_dispatchable_target_means_nothing_to_approve(self):
        campaign = SimpleNamespace(
            status=STATUS_PREFLIGHTED, preflight_at=object(), version=1,
            acknowledged_by="", acknowledged_version=0,
        )
        targets = [SimpleNamespace(applicability=APPLICABILITY_EXCLUDED)]
        ok, reason = can_seek_approval(campaign, targets)
        assert ok is False
        assert "nothing to approve" in reason


class TestDelegationCeilingAcrossEveryScopeType:
    """Regression: the live stack found a 500 here.

    The first cut wrote its own ceiling and passed `org_unit_id=` to
    `ResolvedScope.permits`, which takes `org_unit_path=` — an org-unit
    scope has to be resolved to its E1.1 materialized path first. Unit
    tests using only `site` scope never reached it, and org_unit is the
    headline case ("every site in Region West"). The ceiling now reuses
    the Operational Agent's `_scope_rule_within`, so there is one
    implementation and these four paths are exercised.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("scope_type,scope_ref", [
        ("site", "SITE"),
        ("org_unit", "unit-1"),
        ("device", "node-1"),
        ("device_class", "server"),
    ])
    async def test_every_scope_type_reaches_a_verdict_not_a_crash(
        self, scope_type, scope_ref
    ):
        client, sm = await _stack()
        site_id = await _seed(sm)
        body = _body(site_id)
        body["scopes"] = [{
            "scope_type": scope_type,
            "scope_ref": site_id if scope_ref == "SITE" else scope_ref,
        }]
        r = await client.post("/api/campaigns/", json=body)
        # 201 (permitted), 400 (ref does not exist) or 403 (out of scope)
        # are all real answers. A 500 is the bug this pins.
        assert r.status_code in (201, 400, 403), r.text
        await client.aclose()

    @pytest.mark.asyncio
    async def test_an_org_unit_scope_is_accepted_when_it_exists(self):
        """The headline case: a campaign scoped to a region."""
        client, sm = await _stack()
        site_id = await _seed(sm)
        unit = await client.post(
            "/api/org-units/", json={"name": "Region West", "unit_type": "region"}
        )
        assert unit.status_code in (200, 201), unit.text
        body = _body(site_id)
        body["scopes"] = [
            {"scope_type": "org_unit", "scope_ref": unit.json()["id"]}
        ]
        r = await client.post("/api/campaigns/", json=body)
        assert r.status_code == 201, r.text
        await client.aclose()

    @pytest.mark.asyncio
    async def test_an_unknown_org_unit_is_refused_not_crashed(self):
        client, sm = await _stack()
        site_id = await _seed(sm)
        body = _body(site_id)
        body["scopes"] = [{"scope_type": "org_unit", "scope_ref": "no-such-unit"}]
        r = await client.post("/api/campaigns/", json=body)
        assert r.status_code in (400, 403), r.text
        await client.aclose()


class TestApprovingAWaveThroughTheOneQueue:
    """Regression: the live stack found a 500 here.

    Every part of the campaign-wave decision was unit-tested EXCEPT
    actually calling it — the digest, the wave states and the binding all
    had cover, and the wired endpoint had none, so a missing import in
    the module survived a green suite. These tests drive the real
    endpoint.
    """

    async def _wave(self, sm, site_id, status=WAVE_PENDING_APPROVAL):
        from harkeniq_cc.campaigns import wave_subject_ref
        from harkeniq_cc.db.repos import CampaignRepo

        async with sm() as session:
            repo = CampaignRepo(session)
            campaign = await repo.create(
                tenant_id=TENANT, name="live", action_type="IDENTIFY_LED",
                params={}, created_by="ops@example.com", status="awaiting_approval",
            )
            devices = ["node-1"]
            subject = wave_subject_ref(
                campaign.id, campaign.version, site_id, 0, devices, "planhash"
            )
            await repo.add_wave(
                campaign_id=campaign.id, campaign_version=campaign.version,
                site_id=site_id, wave_index=0, plan_hash="planhash",
                device_agent_ids=devices, domain_span=1,
                subject_ref=subject, status=status,
            )
            await session.commit()
            return campaign.id, subject

    @pytest.mark.asyncio
    async def test_a_campaign_wave_can_actually_be_approved(self):
        client, sm = await _stack()
        site_id = await _seed(sm)
        campaign_id, subject = await self._wave(sm, site_id)
        r = await client.post(f"/api/approvals/{subject}/approve")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["origin"] == "campaign_wave"
        assert body["decision"] == "approved"
        assert body["decided_by"]
        assert body["campaign"]["campaign_id"] == campaign_id
        assert body["campaign"]["devices"] == ["node-1"]
        assert body["campaign"]["plan_hash"] == "planhash"
        # APPROVED is not EXECUTABLE, and the contract says so.
        assert "revalidated at dispatch" in body["note"]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_approving_moves_the_wave_not_the_campaign(self):
        from harkeniq_cc.db.repos import CampaignRepo

        client, sm = await _stack()
        site_id = await _seed(sm)
        campaign_id, subject = await self._wave(sm, site_id)
        await client.post(f"/api/approvals/{subject}/approve")
        async with sm() as session:
            repo = CampaignRepo(session)
            waves = await repo.waves(campaign_id)
            assert [w.status for w in waves] == [WAVE_APPROVED]
            assert waves[0].decided_by
        await client.aclose()

    @pytest.mark.asyncio
    async def test_a_wave_can_be_denied(self):
        client, sm = await _stack()
        site_id = await _seed(sm)
        _, subject = await self._wave(sm, site_id)
        r = await client.post(f"/api/approvals/{subject}/deny")
        assert r.status_code == 200, r.text
        assert r.json()["decision"] == "denied"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_deciding_the_same_wave_twice_is_refused(self):
        client, sm = await _stack()
        site_id = await _seed(sm)
        _, subject = await self._wave(sm, site_id)
        assert (
            await client.post(f"/api/approvals/{subject}/approve")
        ).status_code == 200
        second = await client.post(f"/api/approvals/{subject}/approve")
        assert second.status_code == 409
        await client.aclose()

    @pytest.mark.asyncio
    async def test_an_unknown_subject_is_404(self):
        client, sm = await _stack()
        await _seed(sm)
        r = await client.post("/api/approvals/deadbeef/approve")
        assert r.status_code == 404
        await client.aclose()

    @pytest.mark.asyncio
    async def test_an_operator_may_decide_a_wave(self):
        """action.approve, the same permission a node action needs."""
        client, sm = await _stack(role="operator")
        site_id = await _seed(sm)
        _, subject = await self._wave(sm, site_id)
        r = await client.post(f"/api/approvals/{subject}/approve")
        assert r.status_code == 200, r.text
        await client.aclose()

    @pytest.mark.asyncio
    async def test_an_auditor_may_not_decide_a_wave(self):
        client, sm = await _stack(role="auditor")
        site_id = await _seed(sm)
        _, subject = await self._wave(sm, site_id)
        r = await client.post(f"/api/approvals/{subject}/approve")
        assert r.status_code == 403
        await client.aclose()
