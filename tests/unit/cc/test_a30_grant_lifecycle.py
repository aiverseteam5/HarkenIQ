"""A6-4B0a (A30): expired means expired, everywhere reach is evaluated.

THE DEFECT, AS REPRODUCED
-------------------------
`OperationalAgentRepo.list_scopes()` filtered `revoked_at` and not
`expires_at`, and it was the input every operational reach path resolved
from. So an EXPIRED grant of type `site`, `device` or `device_class`
still yielded devices:

    case                    is_active   canonical site_ids   evaluator saw
    revoked site grant      False       []                   []
    EXPIRED site grant      False       []                   ['dev-1']
    EXPIRED device grant    False       []                   ['dev-2']
    EXPIRED class grant     False       []                   ['dev-2']

Revoked was correctly closed. Expired was fail-open, and it reached the
evaluator (proposals), dry-run, runtime, agent-view and preflight.

WHY IT WAS NOT ONE MISSING `WHERE` CLAUSE
-----------------------------------------
Central Command had TWO reach paths. The canonical one
(`resolve()` -> `ResolvedScope`) is lifecycle-correct and its `site_ids`
projection cannot represent a `device` or `device_class` grant. The raw
one (`list_scopes()` -> `resolve_scope()`) is type-complete and
lifecycle-blind. `agent_runtime` passed BOTH -- raw rows as `scopes`,
canonical sites as `resolved_site_ids` -- so the runtime compensated for
the first defect by importing the second and took the union of both
errors.

Adding `expires_at` to the raw read would have made the second path
lifecycle-correct and left it a second path. A30.4 (AD-1) separates the
two questions by name instead:

    ADMINISTRATIVE GRANT ROW ACCESS   what is configured, expired
                                      included -- `clear_scopes` must
                                      see a lapsed row to retire it
    EFFECTIVE AUTHORIZATION REACH     what may be operated on now, and
                                      there is exactly one answer

WHAT THIS MODULE HOLDS
----------------------
* no operational reach path consumes unfiltered grant rows (structural)
* `resolve_scope` refuses an inactive row whatever hands it one
* the full lifecycle matrix resolves identically at every reader
* an expired agent stops proposing
* `clear_scopes` still retires expired-but-unrevoked rows
* the administrative reads that must keep seeing lapsed rows still do
* F1 is NOT fixed here, and that is asserted, not assumed
"""

from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import httpx
import pytest

from harkeniq_cc import agent_runtime
from harkeniq_cc.agent_lifecycle import run_preflight, runtime_state
from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import (
    CCFleetCache,
    CCOperationalAgent,
    CCScopeGrant,
    CCSite,
)
from harkeniq_cc.db.repos import OperationalAgentRepo
from harkeniq_cc.governance import AgentReach, load_agent_reach
from harkeniq_cc.operational_agent import resolve_scope
from harkeniq_cc.runtime import AppState
from harkeniq_cc.scope import (
    SCOPE_ONLY_MARKER,
    is_active,
    resolve,
)

from tests.unit.cc.conftest import seed_tenant_admin

TENANT = "t1"

CC_SRC = pathlib.Path(__file__).resolve().parents[3] / (
    "services/central_command/src/harkeniq_cc"
)

PAST = datetime.now(timezone.utc) - timedelta(days=1)
FUTURE = datetime.now(timezone.utc) + timedelta(days=30)


def _row(scope_type, ref, *, revoked=None, expires=None, realm=None):
    """A grant row shaped like `cc_scope_grants`, without a database."""
    return NS(
        scope_type=scope_type, scope_ref=ref, revoked_at=revoked,
        expires_at=expires, permission_subset=None, role=None, realm=realm,
    )


# ---------------------------------------------------------------------------
# The structural invariant: the two questions never meet in one function
# ---------------------------------------------------------------------------


def _functions_calling(name: str) -> set[str]:
    """Every function in the CC package that calls `name`, by qualname."""
    found: set[str] = set()
    for path in CC_SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                called = (
                    func.attr if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name)
                    else ""
                )
                if called == name:
                    found.add(f"{path.name}:{node.name}")
    return found


class TestAdministrativeAccessIsNotAReachSource:
    def test_the_old_name_is_gone(self):
        """`list_scopes` must not exist. A rename is the audit.

        Leaving the old name would leave the old habit: it read like a
        scope read and behaved like an unfiltered row dump.
        """
        assert not hasattr(OperationalAgentRepo, "list_scopes")
        assert hasattr(OperationalAgentRepo, "list_administrative_scope_rows")

    def test_no_function_both_reads_raw_rows_and_resolves_reach(self):
        """The two questions are asked in different places, always.

        This is the invariant, not the narrowing. A function that holds
        the configured rows AND resolves reach is one edit away from
        passing the first into the second, which is exactly how the
        defect was built.
        """
        admin = _functions_calling("list_administrative_scope_rows")
        reach = _functions_calling("resolve_scope") | _functions_calling("evaluate")
        both = admin & reach
        assert both == set(), (
            "these functions read CONFIGURED grant rows and resolve "
            f"operational reach in the same place: {sorted(both)}. "
            "Administrative row access is not an authorization source "
            "(A30.4)."
        )

    def test_every_operational_reach_path_goes_through_one_function(self):
        """`load_agent_reach` is the only way to ask an agent's reach."""
        callers = _functions_calling("load_agent_reach")
        expected = {
            "agent_runtime.py:evaluate_agents",
            "agent_lifecycle.py:run_preflight",
            "agent_lifecycle.py:runtime_state",
            "operational_agents.py:_refuse_zero_reach",
            "operational_agents.py:get_agent",
            "operational_agents.py:dry_run_agent",
            "operational_agents.py:submit_proposal",
        }
        missing = expected - callers
        assert not missing, (
            f"these operational reach paths no longer resolve canonically: "
            f"{sorted(missing)}"
        )


# ---------------------------------------------------------------------------
# `resolve_scope` refuses an inactive row, whatever hands it one
# ---------------------------------------------------------------------------


class TestResolveScopeRefusesInactiveRows:
    DEVICES = [
        NS(agent_id="dev-1", site_id="site-a", device_class="server"),
        NS(agent_id="dev-2", site_id="site-b", device_class="switch"),
    ]

    @pytest.mark.parametrize("scope_type,ref,expected", [
        ("site", "site-a", ["dev-1"]),
        ("device", "dev-2", ["dev-2"]),
        ("device_class", "switch", ["dev-2"]),
    ])
    def test_an_expired_row_reaches_nothing(self, scope_type, ref, expected):
        live = _row(scope_type, ref)
        dead = _row(scope_type, ref, expires=PAST)

        assert [
            d.agent_id for d in resolve_scope([live], self.DEVICES, ())
        ] == expected, "an ACTIVE grant must still reach its devices"
        assert resolve_scope([dead], self.DEVICES, ()) == [], (
            f"an EXPIRED {scope_type} grant reached devices"
        )

    def test_a_revoked_row_reaches_nothing(self):
        dead = _row("site", "site-a", revoked=PAST)
        assert resolve_scope([dead], self.DEVICES, ()) == []

    def test_a_future_expiry_is_still_reach(self):
        """Time-bounded is not expired. The common case must not break."""
        row = _row("site", "site-a", expires=FUTURE)
        assert [
            d.agent_id for d in resolve_scope([row], self.DEVICES, ())
        ] == ["dev-1"]

    def test_a_lifecycle_free_rule_is_untouched(self):
        """A campaign scope rule carries no lifecycle and keeps working.

        `cc_campaign_scopes` has no `expires_at` and no `revoked_at`.
        `is_active` reads attributes that are simply absent and answers
        True, so this filter narrows exactly one thing: a lapsed grant
        row.
        """
        rule = NS(scope_type="site", scope_ref="site-a")
        assert is_active(rule) is True
        assert [
            d.agent_id for d in resolve_scope([rule], self.DEVICES, ())
        ] == ["dev-1"]


# ---------------------------------------------------------------------------
# The lifecycle matrix, resolved the way production resolves it
# ---------------------------------------------------------------------------


class TestTheLifecycleMatrix:
    UNIT = NS(id="u1", path="/u1/")
    SITES = [NS(id="site-a", org_unit_id="u1"), NS(id="site-b", org_unit_id=None)]
    DEVICES = [
        NS(agent_id="dev-1", site_id="site-a", device_class="server"),
        NS(agent_id="dev-2", site_id="site-b", device_class="switch"),
    ]

    def _reach(self, rows, *, realm=""):
        scope = resolve(
            tenant_id=TENANT, principal_type="agent", principal_ref="a1",
            role_permissions=[SCOPE_ONLY_MARKER], grant_rows=rows,
            org_units=[self.UNIT], sites=self.SITES, enforcement="strict",
        )
        return scope, [
            d.agent_id
            for d in resolve_scope(
                scope.effective_grants, self.DEVICES, scope.site_ids
            )
        ]

    @pytest.mark.parametrize("label,rows,expected", [
        ("active site",      [_row("site", "site-a")],                    ["dev-1"]),
        ("active org_unit",  [_row("org_unit", "u1")],                    ["dev-1"]),
        ("active device",    [_row("device", "dev-2")],                   ["dev-2"]),
        ("active class",     [_row("device_class", "switch")],            ["dev-2"]),
        ("future-expiring",  [_row("site", "site-a", expires=FUTURE)],    ["dev-1"]),
        ("revoked",          [_row("site", "site-a", revoked=PAST)],      []),
        ("expired site",     [_row("site", "site-a", expires=PAST)],      []),
        ("expired device",   [_row("device", "dev-2", expires=PAST)],     []),
        ("expired class",    [_row("device_class", "switch", expires=PAST)], []),
        ("inert org_unit",   [_row("org_unit", "gone")],                  []),
        ("vanished site",    [_row("site", "site-zz")],                   []),
        ("unknown type",     [_row("nonsense", "x")],                     []),
        ("no rows",          [],                                          []),
    ])
    def test_matrix(self, label, rows, expected):
        _scope, devices = self._reach(rows)
        assert devices == expected, label

    def test_an_expired_row_is_still_evidence(self):
        """A23.10 survives: expired is INEFFECTIVE, not never-granted.

        The synthesis rule must not start handing a lapsed principal
        tenant-wide reach because its only grant stopped counting.
        """
        scope, devices = self._reach([_row("site", "site-a", expires=PAST)])
        assert devices == []
        assert scope.previously_granted is True
        assert scope.tenant_wide is False

    def test_a_mixed_set_keeps_only_what_is_live(self):
        _scope, devices = self._reach([
            _row("site", "site-a"),
            _row("device", "dev-2", expires=PAST),
        ])
        assert devices == ["dev-1"], "the expired half must not survive"


# ---------------------------------------------------------------------------
# F1 is deliberately NOT fixed here
# ---------------------------------------------------------------------------


class TestF1RemainsOpenOnPurpose:
    def test_site_ids_still_cannot_express_device_reach(self):
        """A6-4B0b's defect, asserted as still present.

        If this ever starts passing by accident, the under-reach was
        fixed by something that was not B0b -- most likely by reaching
        back for raw grant rows, which is the path B0a removed.
        """
        scope = resolve(
            tenant_id=TENANT, principal_type="user", principal_ref="p1",
            role_permissions=["fleet.view"],
            grant_rows=[_row("device", "dev-1")],
            sites=[NS(id="site-a", org_unit_id=None)], enforcement="strict",
        )
        assert scope.site_ids == frozenset(), (
            "site_ids gained device reach; that is A6-4B0b's job"
        )
        assert scope.covers_device("dev-1") is True, (
            "canonical coverage must still say the device is in scope"
        )


# ---------------------------------------------------------------------------
# The live stack: an expired agent stops proposing
# ---------------------------------------------------------------------------


class FakeSM:
    accepted = True
    calls: list = []

    def __init__(self, *_a, **_kw):
        pass

    async def dispatch_action(self, endpoint, token, **kw):
        FakeSM.calls.append(kw)
        return {"accepted": True, "directive_id": "d1", "reason": ""}


@pytest.fixture(autouse=True)
def _fake_sm(monkeypatch):
    FakeSM.calls = []
    monkeypatch.setattr(agent_runtime, "SMClient", FakeSM)
    yield


async def _stack():
    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)
    await seed_tenant_admin(sessionmaker, TENANT, "kc-owner", role="tenant_owner")

    async def _fake():
        return UserContext(
            user_id="kc-owner", email="owner@example.com", tenant_id=TENANT,
            role="tenant_owner", permissions=list(ROLE_PERMISSIONS["tenant_owner"]),
        )

    app.dependency_overrides[get_current_user] = _fake
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    )
    return client, state


async def _seed_agent(state, *, expires=None):
    """One agent, one site grant, one device. `expires` sets the lifecycle."""
    async with state.sessionmaker() as session:
        site = CCSite(
            tenant_id=TENANT, site_name="DC-1",
            sm_endpoint="sm:50051", sm_token="tok",
        )
        session.add(site)
        await session.flush()
        session.add(CCFleetCache(
            site_id=site.id, agent_id="node-1", agent_name="rack1-node1",
            vendor="Dell", model="R750", device_class="server",
            observation="observed", health="Critical",
        ))
        agent = CCOperationalAgent(
            tenant_id=TENANT, name="lifecycle-probe", status="active",
            version=1, activated_version=1, created_by="kc-owner",
            autonomy_ceiling=0,
        )
        session.add(agent)
        await session.flush()
        session.add(CCScopeGrant(
            tenant_id=TENANT, principal_type="agent", principal_ref=agent.id,
            scope_type="site", scope_ref=site.id, granted_by="kc-owner",
            expires_at=expires,
        ))
        await session.commit()
        return agent.id, site.id


class TestAnExpiredAgentReachesNothing:
    async def test_active_grant_reaches_the_device(self):
        _client, state = await _stack()
        agent_id, _site = await _seed_agent(state)
        async with state.sessionmaker() as session:
            reach = await load_agent_reach(
                session, tenant_id=TENANT, agent_id=agent_id,
                devices=[NS(agent_id="node-1", site_id=_site,
                            device_class="server")],
            )
        assert [d.agent_id for d in reach.devices] == ["node-1"]

    async def test_expired_grant_reaches_no_device(self):
        _client, state = await _stack()
        agent_id, site_id = await _seed_agent(state, expires=PAST)
        async with state.sessionmaker() as session:
            reach = await load_agent_reach(
                session, tenant_id=TENANT, agent_id=agent_id,
                devices=[NS(agent_id="node-1", site_id=site_id,
                            device_class="server")],
            )
        assert reach.devices == ()
        assert reach.rules == ()

    async def test_expired_agent_produces_no_proposal(self):
        """The write path. This is what the fail-open actually cost."""
        _client, state = await _stack()
        await _seed_agent(state, expires=PAST)
        created = await agent_runtime.evaluate_agents(state, TENANT)
        assert created == [], (
            "an agent whose grant has lapsed proposed work anyway"
        )

    async def test_runtime_state_reports_no_devices(self):
        _client, state = await _stack()
        agent_id, _site = await _seed_agent(state, expires=PAST)
        async with state.sessionmaker() as session:
            agent = await OperationalAgentRepo(session).get(TENANT, agent_id)
            runtime = await runtime_state(
                session, tenant_id=TENANT, agent=agent,
            )
        assert runtime["devices"]["in_scope"] == 0

    async def test_preflight_reports_no_reach(self):
        _client, state = await _stack()
        agent_id, _site = await _seed_agent(state, expires=PAST)
        async with state.sessionmaker() as session:
            agent = await OperationalAgentRepo(session).get(TENANT, agent_id)
            pre = await run_preflight(
                session, state, tenant_id=TENANT, agent=agent,
                actor="kc-owner", actor_ref="kc-owner",
            )
        scope_dim = next(
            d for d in pre["dimensions"] if d["dimension"] == "scope"
        )
        # The configured row is still COUNTED -- it exists -- and it
        # resolves to nothing, which is the distinction the preflight
        # already knew how to draw and now gets to draw truthfully.
        assert scope_dim["devices"] == 0
        assert scope_dim["scope_rows"] == 1
        assert scope_dim["verdict"] == "blocked"
        assert "resolves to no devices" in scope_dim["detail"]

    async def test_agent_view_reports_the_lapse_rather_than_no_scope(self):
        """The message an operator gets must not send them to re-bind.

        Dropping expired rules from the view is right; telling an
        operator "No scope assigned" when a rule IS assigned and has
        merely lapsed is not.
        """
        client, state = await _stack()
        agent_id, _site = await _seed_agent(state, expires=PAST)
        resp = await client.get(f"/api/operational-agents/{agent_id}")
        assert resp.status_code == 200
        scope_block = resp.json()["scope"]
        assert scope_block["device_count"] == 0
        assert scope_block["configured_rule_count"] == 1
        assert scope_block["ineffective_rule_count"] == 1
        assert "lapsed" in scope_block["statement"]


# ---------------------------------------------------------------------------
# The administrative reads that must keep seeing a lapsed row
# ---------------------------------------------------------------------------


class TestAdministrativeAccessStillSeesLapsedRows:
    async def test_clear_scopes_retires_an_expired_row(self):
        """The reason the administrative read exists.

        A row nobody can see is a row nobody can retire. A
        lifecycle-filtered read would leave an expired grant active
        forever on a retired agent.
        """
        _client, state = await _stack()
        agent_id, _site = await _seed_agent(state, expires=PAST)
        async with state.sessionmaker() as session:
            repo = OperationalAgentRepo(session)
            rows = await repo.list_administrative_scope_rows(agent_id)
            assert len(rows) == 1, "the expired row must still be readable"

            cleared = await repo.clear_scopes(agent_id, revoked_by="kc-owner")
            await session.commit()
            assert cleared == 1

        async with state.sessionmaker() as session:
            repo = OperationalAgentRepo(session)
            assert await repo.list_administrative_scope_rows(agent_id) == []

    async def test_an_agent_with_a_lapsed_grant_stays_visible(self):
        """No lockout. The person who would renew it can still see it.

        Visibility of the agent OBJECT is administrative. Resolving it
        through effective reach would hide a lapsed agent from the site
        administrator who owns it, leaving only a tenant-wide reader able
        to see the thing that needs renewing.
        """
        client, state = await _stack()
        agent_id, _site = await _seed_agent(state, expires=PAST)

        listed = await client.get("/api/operational-agents/")
        assert listed.status_code == 200
        assert any(
            row["id"] == agent_id for row in listed.json()["agents"]
        ), "a lapsed agent vanished from the list"

        detail = await client.get(f"/api/operational-agents/{agent_id}")
        assert detail.status_code == 200
