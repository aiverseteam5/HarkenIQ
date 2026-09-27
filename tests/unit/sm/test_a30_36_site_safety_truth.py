"""S3-E1-0 (A30.36): the Site Manager's per-site safety truth.

THE INVARIANT (locked): a site-local safety fact at site S -- halt,
suppression, budget window, drop-back -- does not change when only another
site's state changes, and everything the Site Manager EMITS about S (the
`FleetSafetyState` it reports for S, and the lease it issues a device at S)
derives only from S's own state and from halts that genuinely stop S.

Every test drives a REAL Site Manager (production `make_state`, both
servicers, a real port) through the doors production traffic uses, and
reads it the way Central Command does. See `e1_0_estate`.
"""

from __future__ import annotations

import itertools
import json
import pathlib

import pytest

from harkeniq.proto import harkeniq_pb2

from tests.unit.sm import e1_0_estate as E

GOLDEN = json.loads(
    (pathlib.Path(__file__).parent / "golden" / "a30_36_main_sm_safety.json").read_text()
)

_LEASE_CLOCK = ("lease_expiry", "grace_expiry", "issued_at")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture
async def stack():
    s = await E.build(("alpha", "beta"))
    await E.push_policy(s)
    yield s
    await E.close(s)


async def enroll(stack, site):
    """A real node identity enrolled at `site` (E1.3: token-bound)."""
    from harkeniq.autonomy.identity import AgentIdentity
    from harkeniq_sm.enrollment import EnrollmentService

    identity = AgentIdentity.generate()
    service = EnrollmentService(stack.state.sessionmaker, stack.config)
    async with stack.state.sessionmaker() as session:
        secret, _ = await service.issue(session, site_id=stack.site_ids[site])
        await session.commit()
    ack = await stack.agent.RegisterAgent(harkeniq_pb2.AgentRegistration(
        agent_id=identity.agent_id, agent_name=f"lease-{site}",
        vendor="Dell", model="R750", public_key_pem=identity.public_key_pem,
        enrollment_token=secret,
    ))
    assert ack.accepted, ack
    identity.set_sm_public_key(stack.state.identity.sm_public_key_pem)
    return identity


async def lease(stack, identity) -> dict:
    """The lease the Site Manager issues this node now, clock removed."""
    from harkeniq.autonomy.lease import AuthorizationLease

    ack = await stack.agent.Heartbeat(harkeniq_pb2.AgentHeartbeat(
        agent_id=identity.agent_id, agent_name="lease", state="OBSERVING",
    ))
    assert ack.accepted and ack.authorization_lease
    parsed = AuthorizationLease.parse(bytes(ack.authorization_lease), identity)
    return {
        "stop_switch": parsed.stop_switch,
        "suppression_domains": list(parsed.suppression_domains),
        "budget_remaining": dict(sorted(parsed.budget_remaining.items())),
        "action_classes": sorted(parsed.action_classes),
        "risk_ceiling": parsed.risk_ceiling,
    }


async def facts(stack, site, identities) -> dict:
    """Everything the Site Manager emits about `site`."""
    return {
        "snapshot": E.scrub(await E.snapshot(stack, site)),
        "lease": await lease(stack, identities[site]),
    }


async def decision(stack, site, authorization="human_approval"):
    """The Site Manager's own dispatch decision for a device at `site`."""
    from types import SimpleNamespace

    return await stack.sm_servicer._sm_execution_decision(
        action_type="SEL_CLEAR", device=SimpleNamespace(capabilities=None),
        device_site_id=stack.site_ids[site], authorization=authorization,
    )


def _walk(value):
    """Every key and every value, as text, for sentinel searches."""
    return json.dumps(value, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# F-1: halt
# ---------------------------------------------------------------------------


class TestF1Halt:
    async def test_the_per_site_matrix_and_b_never_moves_a(self, stack):
        ids = {s: await enroll(stack, s) for s in stack.sites}
        seen_a: dict[bool, dict] = {}
        for a_halt, b_halt in itertools.product((False, True), repeat=2):
            await E.halt(stack, "alpha", active=a_halt)
            await E.halt(stack, "beta", active=b_halt)
            a, b = await facts(stack, "alpha", ids), await facts(stack, "beta", ids)
            assert a["snapshot"]["sm_stop_switch"] is a_halt
            assert b["snapshot"]["sm_stop_switch"] is b_halt
            assert a["lease"]["stop_switch"] is a_halt
            assert b["lease"]["stop_switch"] is b_halt
            # A's emitted facts depend on A's halt alone.
            seen_a.setdefault(a_halt, a)
            assert a == seen_a[a_halt], (a_halt, b_halt)

    @pytest.mark.parametrize("scope", ["tenant", "site_manager"])
    async def test_a_halt_that_stops_every_site_is_reported_at_every_site(
        self, stack, scope,
    ):
        await E.halt(stack, None, scope=scope)
        for site in stack.sites:
            assert (await E.snapshot(stack, site))["sm_stop_switch"] is True
        await E.halt(stack, None, scope=scope, active=False)
        for site in stack.sites:
            assert (await E.snapshot(stack, site))["sm_stop_switch"] is False

    async def test_the_in_memory_flag_halts_every_site_the_process_serves(self, stack):
        stack.state.autonomy.activate_stop_switch("e10")
        for site in stack.sites:
            assert (await E.snapshot(stack, site))["sm_stop_switch"] is True
        stack.state.autonomy.deactivate_stop_switch("e10")
        for site in stack.sites:
            assert (await E.snapshot(stack, site))["sm_stop_switch"] is False

    async def test_what_is_reported_is_what_is_enforced(self, stack):
        """Snapshot == lease == the Site Manager's own dispatch refusal."""
        ids = {s: await enroll(stack, s) for s in stack.sites}
        states = [
            ("none", lambda: None),
            ("site-a", lambda: E.halt(stack, "alpha")),
            ("site-b", lambda: E.halt(stack, "beta")),
            ("emergency", lambda: E.halt(stack, None, scope="site_manager")),
            ("tenant", lambda: E.halt(stack, None, scope="tenant")),
        ]
        for name, apply in states:
            for site in stack.sites:
                await E.halt(stack, site, active=False)
            for scope in ("site_manager", "tenant"):
                await E.halt(stack, None, scope=scope, active=False)
            result = apply()
            if result is not None:
                await result
            for site in stack.sites:
                reported = (await E.snapshot(stack, site))["sm_stop_switch"]
                leased = (await lease(stack, ids[site]))["stop_switch"]
                refused = (await decision(stack, site)).refused_by in (
                    "site_stop", "manager_halt",
                )
                assert reported == leased == refused, (name, site)


# ---------------------------------------------------------------------------
# F-2: suppression
# ---------------------------------------------------------------------------


class TestF2Suppression:
    @pytest.mark.parametrize("suppressed", [("alpha",), ("beta",), ("alpha", "beta")])
    async def test_a_site_reports_only_its_own_fault_domains(self, suppressed):
        stack = await E.build(("alpha", "beta"))
        try:
            ids = {s: await enroll(stack, s) for s in stack.sites}
            for site in suppressed:
                await E.suppress(stack, site)
            for site in stack.sites:
                snap = await E.snapshot(stack, site)
                lz = await lease(stack, ids[site])
                expected = [stack.domain_id(site)] if site in suppressed else []
                assert [s["domain_id"] for s in snap["suppressions"]] == expected
                assert lz["suppression_domains"] == expected
        finally:
            await E.close(stack)

    async def test_hostile_site_b_sentinels_never_reach_site_a(self, stack):
        from harkeniq_sm.db.repos import ErrorBudgetRepo
        from harkeniq_sm.suppression import SuppressionState

        ids = {s: await enroll(stack, s) for s in stack.sites}
        before = await facts(stack, "alpha", ids)
        stack.state.suppression._active[stack.domain_id("beta")] = SuppressionState(
            domain_id=stack.domain_id("beta"), domain_kind="power",
            event_family="SENTINEL-FAMILY-B", trigger_reason="SENTINEL-REASON-B",
            device_count=97531, triggered_at=1_700_000_000.0,
        )
        async with stack.state.sessionmaker() as session:
            for _ in range(8):
                await ErrorBudgetRepo(session).record(
                    stack.site_ids["beta"], "SENTINEL_CLASS", "FAILURE",
                )
            await session.commit()
        await E.halt(stack, "beta")
        await E.report(stack, "b1", "SEL_CLEAR", n=3)

        after = await facts(stack, "alpha", ids)
        text = _walk(after)
        for sentinel in (
            stack.domain_id("beta"), "SENTINEL", "97531", stack.site_ids["beta"],
        ):
            assert sentinel not in text, sentinel
        assert after == before
        # Non-vacuity: site B itself reports every one of them.
        b_text = _walk(await facts(stack, "beta", ids))
        for sentinel in (stack.domain_id("beta"), "SENTINEL-REASON-B", "97531",
                         "SENTINEL_CLASS"):
            assert sentinel in b_text, sentinel

    async def test_a_suppression_whose_domain_no_longer_exists_belongs_to_no_site(
        self, stack,
    ):
        from harkeniq_sm.suppression import SuppressionState

        ids = {s: await enroll(stack, s) for s in stack.sites}
        stack.state.suppression._active["dom-ghost"] = SuppressionState(
            domain_id="dom-ghost", domain_kind="power", event_family="power",
            trigger_reason="direct_dependency", device_count=2,
            triggered_at=1_700_000_000.0,
        )
        for site in stack.sites:
            assert (await E.snapshot(stack, site))["suppressions"] == []
            assert (await lease(stack, ids[site]))["suppression_domains"] == []

    async def test_order_is_the_fault_domain_order_not_the_trigger_order(self, stack):
        from harkeniq_sm.db.models import FaultDomain
        from harkeniq_sm.suppression import SuppressionState

        async with stack.state.sessionmaker() as session:
            session.add(FaultDomain(
                id=f"dom-aaa-alpha", site_id=stack.site_ids["alpha"],
                name="pdu-first", kind="power",
            ))
            await session.commit()
        # Triggered in reverse id order.
        for domain_id in (stack.domain_id("alpha"), "dom-aaa-alpha"):
            stack.state.suppression._active[domain_id] = SuppressionState(
                domain_id=domain_id, domain_kind="power", event_family="power",
                trigger_reason="direct_dependency", device_count=2,
                triggered_at=1_700_000_000.0,
            )
        snap = await E.snapshot(stack, "alpha")
        assert [s["domain_id"] for s in snap["suppressions"]] == sorted(
            ["dom-aaa-alpha", stack.domain_id("alpha")]
        )


# ---------------------------------------------------------------------------
# F-3: budget windows and drop-back
# ---------------------------------------------------------------------------


class TestF3BudgetAndDropBack:
    @pytest.mark.parametrize("spender,idle", [("alpha", "beta"), ("beta", "alpha")])
    async def test_a_spent_window_is_the_spenders_alone(self, stack, spender, idle):
        ids = {s: await enroll(stack, s) for s in stack.sites}
        idle_before = await facts(stack, idle, ids)
        device = stack.devices(spender)[0]
        assert await E.report(stack, device, "SEL_CLEAR", n=3) == ["accepted"] * 3

        spent = await facts(stack, spender, ids)
        assert spent["snapshot"]["site_budgets"]["SEL_CLEAR"] == 0
        assert spent["lease"]["budget_remaining"]["SEL_CLEAR"] == 0
        idle_after = await facts(stack, idle, ids)
        assert idle_after["snapshot"]["site_budgets"]["SEL_CLEAR"] == 3
        assert idle_after["lease"]["budget_remaining"]["SEL_CLEAR"] == 3
        # Byte for byte, apart from the error-budget evidence the spender's
        # own successes wrote at ITS site.
        assert idle_after == idle_before

    @pytest.mark.parametrize("failing,clear", [("alpha", "beta"), ("beta", "alpha")])
    async def test_drop_back_is_the_failing_sites_alone(self, stack, failing, clear):
        ids = {s: await enroll(stack, s) for s in stack.sites}
        clear_before = await facts(stack, clear, ids)
        device = stack.devices(failing)[0]
        assert await E.report(
            stack, device, "SEL_CLEAR", status="FAILED", success=False, n=5,
        ) == ["accepted"] * 5

        dropped = await facts(stack, failing, ids)
        budgets = {b["action_type"]: b for b in dropped["snapshot"]["error_budgets"]}
        assert budgets["SEL_CLEAR"]["dropped_back"] is True
        assert dropped["lease"]["budget_remaining"]["SEL_CLEAR"] == 0
        assert (await decision(stack, failing, "autonomous_grant")).refused_by == "autonomy"
        assert await facts(stack, clear, ids) == clear_before
        assert (await decision(stack, clear, "autonomous_grant")).permitted

    async def test_an_unattributable_lease_is_exhausted_never_unlimited(
        self, stack, monkeypatch,
    ):
        """A lease for a node whose site cannot be resolved carries no
        site's window: every policy class reads 0 ("propose")."""
        from harkeniq_sm import grpc_server
        from harkeniq_sm.db.repos import DeviceRepo

        identity = await enroll(stack, "alpha")
        await E.suppress(stack, "alpha")

        class Unresolvable(DeviceRepo):
            async def get_by_agent_id(self, agent_id):
                return None

        monkeypatch.setattr(grpc_server, "DeviceRepo", Unresolvable)
        got = await lease(stack, identity)
        for policy in E.POLICIES:
            assert got["budget_remaining"][policy["action_type"]] == 0
        assert got["suppression_domains"] == []

    async def test_the_enforcer_refuses_a_site_less_window(self):
        from harkeniq_sm.autonomy import SMAutonomyEnforcer

        enforcer = SMAutonomyEnforcer()
        enforcer.update_policy(E.POLICIES)
        with pytest.raises(ValueError):
            enforcer.record_execution("", "SEL_CLEAR")
        with pytest.raises(ValueError):
            enforcer.budget_for_site("")

    async def test_a_policy_change_re_limits_every_sites_window(self):
        from harkeniq_sm.autonomy import SMAutonomyEnforcer

        enforcer = SMAutonomyEnforcer()
        enforcer.update_policy(E.POLICIES)
        enforcer.record_execution("s1", "SEL_CLEAR")
        enforcer.update_policy([{**E.POLICIES[0], "max_per_window": 10}])
        assert enforcer.budget_for_site("s1")["SEL_CLEAR"] == 9
        assert enforcer.budget_for_site("s2")["SEL_CLEAR"] == 10


class TestBreakGlassRead:
    async def test_every_served_site_is_listed_with_its_own_windows(self, stack):
        """The Site Manager's site-token read: per site, never one map."""
        from httpx import ASGITransport, AsyncClient

        from harkeniq_sm.app import create_app

        await E.report(stack, "a1", "SEL_CLEAR", n=2)
        app = create_app(stack.state)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://sm",
        ) as client:
            body = (await client.get("/api/autonomy")).json()
        assert "budgets" not in body
        windows = body["budgets_by_site"]
        assert windows[stack.site_ids["alpha"]]["SEL_CLEAR"]["remaining"] == 1
        assert windows[stack.site_ids["beta"]]["SEL_CLEAR"]["remaining"] == 3
        assert body["site_names"][stack.site_ids["beta"]] == stack.site_name("beta")


# ---------------------------------------------------------------------------
# F-7: the node-path report at a non-default site
# ---------------------------------------------------------------------------


class TestF7NodePathReport:
    async def test_an_enrolled_device_reports_under_its_own_site(self, stack):
        from sqlalchemy import select

        from harkeniq_sm.db.models import ActionRow, Device

        async with stack.state.sessionmaker() as session:
            before = await session.get(Device, stack.device_row("b1"))
            seen_before = before.last_seen_at
        assert await E.report(stack, "b1", "SEL_CLEAR") == ["accepted"]
        async with stack.state.sessionmaker() as session:
            device = await session.get(Device, stack.device_row("b1"))
            rows = (await session.execute(
                select(ActionRow).where(ActionRow.device_id == device.id)
            )).scalars().all()
        assert device.site_id == stack.site_ids["beta"]
        assert len(rows) == 1 and rows[0].status == "completed"
        assert device.last_seen_at is not None and device.last_seen_at != seen_before
        eb = {b["action_type"]: b for b in (await E.snapshot(stack, "beta"))["error_budgets"]}
        assert eb["SEL_CLEAR"]["total_count"] == 1
        assert (await E.snapshot(stack, "alpha"))["error_budgets"] == []

    async def test_an_unknown_device_keeps_the_legacy_path(self, stack):
        from harkeniq_sm.db.repos import DeviceRepo
        from harkeniq_sm.db.models import Site
        from sqlalchemy import select

        request = harkeniq_pb2.ActionReport(
            agent_id="never-enrolled", action_id="x-1", type="SEL_CLEAR",
            sensor_id="log:x", status="PENDING",
            proposed_at="2026-09-27T12:00:00Z",
        )
        ack = await stack.agent.ReportAction(request)
        assert ack.accepted
        async with stack.state.sessionmaker() as session:
            device = await DeviceRepo(session).get_by_agent_id("never-enrolled")
            site = (await session.execute(
                select(Site).where(Site.id == device.site_id)
            )).scalar_one()
        assert site.name == stack.config.site_name


# ---------------------------------------------------------------------------
# Cross-site non-interference
# ---------------------------------------------------------------------------


def _mutations(stack):
    """Every way one site's state can change, as (name, site, coroutine factory)."""
    out = []
    for site in stack.sites:
        device = stack.devices(site)[0]
        out += [
            ("halt", site, lambda s=site: E.halt(stack, s)),
            ("suppress", site, lambda s=site: E.suppress(stack, s)),
            ("execute", site, lambda d=device: E.report(stack, d, "SEL_CLEAR", n=2)),
            ("exhaust", site, lambda d=device: E.report(stack, d, "BMC_RESET", n=5)),
            ("drop_back", site, lambda d=device: E.report(
                stack, d, "BMC_RESET", status="FAILED", success=False, n=5)),
        ]
    return out


class TestNonInterference:
    @pytest.mark.parametrize("index", range(5))
    @pytest.mark.parametrize("changed", ["alpha", "beta"])
    async def test_one_sites_change_never_moves_the_other(self, index, changed):
        stack = await E.build(("alpha", "beta"))
        try:
            await E.push_policy(stack)
            ids = {s: await enroll(stack, s) for s in stack.sites}
            other = "beta" if changed == "alpha" else "alpha"
            name, site, apply = [m for m in _mutations(stack) if m[1] == changed][index]
            other_before = await facts(stack, other, ids)
            changed_before = await facts(stack, changed, ids)
            await apply()
            assert await facts(stack, other, ids) == other_before, name
            # Non-vacuity: the mutation moved the site it was applied to.
            assert await facts(stack, changed, ids) != changed_before, name
        finally:
            await E.close(stack)

    @pytest.mark.parametrize("moving,fixed", [("alpha", "beta"), ("beta", "alpha")])
    async def test_a_sequence_at_one_site_leaves_the_other_byte_identical(
        self, stack, moving, fixed,
    ):
        ids = {s: await enroll(stack, s) for s in stack.sites}
        fixed_facts = await facts(stack, fixed, ids)
        device = stack.devices(moving)[0]
        steps = [
            lambda: E.report(stack, device, "SEL_CLEAR"),
            lambda: E.halt(stack, moving),
            lambda: E.suppress(stack, moving),
            lambda: E.report(stack, device, "SEL_CLEAR", n=2),
            lambda: E.halt(stack, moving, active=False),
            lambda: E.report(stack, device, "BMC_RESET", status="FAILED",
                             success=False, n=5),
            lambda: E.halt(stack, moving),
        ]
        for step in steps:
            await step()
            assert await facts(stack, fixed, ids) == fixed_facts


# ---------------------------------------------------------------------------
# Central Command: the same function, fed truthful inputs
# ---------------------------------------------------------------------------


def _fields(snapshot: dict) -> set[str]:
    return set(snapshot)


class TestCentralCommandDifferential:
    """Recorded from UNMODIFIED `main` (3323b74) by the same stages."""

    @pytest.mark.parametrize("topology", ["multi", "single"])
    async def test_central_command_is_the_same_function(self, topology):
        """Main's recorded inputs, through this branch's Central Command,
        give main's recorded outputs -- every stage, both topologies."""
        for stage in GOLDEN[topology]:
            assert await E.central_command(stage["sm_raw"]) == stage["cc"], stage["stage"]

    async def test_single_site_inputs_equal_main_except_a_real_halt(self):
        ours = await E.run_stages("single")
        for recorded, now in zip(GOLDEN["single"], ours):
            assert recorded["stage"] == now["stage"]
            assert now["outcomes"] == recorded["outcomes"]
            if recorded["stage"] != "alpha_halted":
                assert now["sm"] == recorded["sm"], recorded["stage"]
                assert now["cc"] == recorded["cc"], recorded["stage"]
                continue
            # The one correction a single site can see (F-1): the site's own
            # persisted halt is reported.
            assert recorded["sm"]["alpha"]["sm_stop_switch"] is False
            assert now["sm"]["alpha"]["sm_stop_switch"] is True
            assert {**now["sm"]["alpha"], "sm_stop_switch": False} == recorded["sm"]["alpha"]
            # Central Command: posture counts it; no disposition moves.
            assert now["cc"]["classes"] == recorded["cc"]["classes"]
            assert now["cc"]["proposals"] == recorded["cc"]["proposals"]
            assert now["cc"]["posture_stop"]["sites_reporting_active"] == 1
            assert recorded["cc"]["posture_stop"]["sites_reporting_active"] == 0

    async def test_multi_site_inputs_differ_only_by_the_corrections(self):
        ours = await E.run_stages("multi")
        corrected = {"sm_stop_switch", "suppressions", "site_budgets", "error_budgets"}
        for recorded, now in zip(GOLDEN["multi"], ours):
            stage = recorded["stage"]
            assert stage == now["stage"]
            for site in ("alpha", "beta"):
                a, b = recorded["sm"][site], now["sm"][site]
                assert _fields(a) == _fields(b)
                for key in _fields(a) - corrected:
                    assert a[key] == b[key], (stage, site, key)
                # Alpha's drop-back evidence never differed: its reports
                # always reached its own site.
                if site == "alpha":
                    assert a["error_budgets"] == b["error_budgets"], stage
            # F-1: beta's halt is reported from the moment it exists.
            halted = stage in ("beta_halted", "alpha_bmc_failures", "beta_sel_failures")
            assert now["sm"]["beta"]["sm_stop_switch"] is halted
            assert recorded["sm"]["beta"]["sm_stop_switch"] is False
            assert now["sm"]["alpha"]["sm_stop_switch"] is False
            # F-2: beta's suppression is beta's.
            suppressed = stage in ("beta_domain_suppressed", "beta_halted",
                                   "alpha_bmc_failures", "beta_sel_failures")
            assert now["sm"]["alpha"]["suppressions"] == []
            assert [s["domain_id"] for s in now["sm"]["beta"]["suppressions"]] == (
                ["dom-beta"] if suppressed else [])
            if suppressed:
                assert [s["domain_id"] for s in recorded["sm"]["alpha"]["suppressions"]] == ["dom-beta"]
            # F-3 + F-7: each site's window is its own spending.
            assert now["sm"]["alpha"]["site_budgets"]["SEL_CLEAR"] == (
                3 if stage == "baseline" else 1)
            beta_spent = stage not in ("baseline", "alpha_executes_2")
            assert now["sm"]["beta"]["site_budgets"]["SEL_CLEAR"] == (0 if beta_spent else 3)
            if stage != "baseline":
                assert recorded["sm"]["beta"]["site_budgets"]["SEL_CLEAR"] == 1
            # F-7: beta's node-path reports now arrive.
            if stage in ("beta_executes_3", "beta_sel_failures"):
                assert all(o.startswith("error:") for o in recorded["outcomes"][0])
                assert now["outcomes"][0] == ["accepted"] * len(recorded["outcomes"][0])
            beta_budget = {b["action_type"]: b for b in now["sm"]["beta"]["error_budgets"]}
            if beta_spent:
                assert beta_budget["SEL_CLEAR"]["success_count"] == 3
            if stage == "beta_sel_failures":
                assert beta_budget["SEL_CLEAR"]["dropped_back"] is True
            assert recorded["sm"]["beta"]["error_budgets"] == []
