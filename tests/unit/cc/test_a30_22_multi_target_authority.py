"""A6-4B0b-S1 (spec A30.22): a SET of devices is authorized only when EVERY
device in it is -- by the decider's CURRENT scope, at approval and again
immediately before dispatch.

The defect: `_decide_campaign_wave` passed `wave.device_agent_ids[0]` into
the approval gate, so a principal whose grant reached ONE device approved a
wave over all of them, the governing policy followed the first device's
class, and the ledger named that one device. The runner then dispatched an
APPROVED wave without ever re-asking whether its approvers still held
authority over the set, and skill delivery fanned out over the device list
the preflight had stored.

Every deny case here is a NON-VACUOUS negative control: the same principal,
policy, wave and gates that ALLOW when coverage is complete, with exactly one
device's coverage removed. Refusals are REFUSED, NOT RECORDED -- the house
rule from E1.2: a name in the ledger beside a decision they were never
entitled to make would corrupt the evidence.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timedelta, timezone

import grpc
import httpx
import pytest
import sqlalchemy as sa

from harkeniq.capabilities import declare
from harkeniq.proto import harkeniq_pb2_grpc
from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.approval_policy import (
    SUBJECT_CAMPAIGN_WAVE,
    resolve_policy_for_classes,
)
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.campaign_runner import advance_campaign, build_waves, preflight
from harkeniq_cc.campaigns import (
    REVAL_AUTHORITY_LOST,
    WAVE_APPROVED,
    WAVE_AUTONOMOUS,
    WAVE_DISPATCHED,
    WAVE_PENDING_APPROVAL,
    wave_subject_ref,
)
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import (
    CCAgentSkillInstall,
    CCAuditLog,
    CCFleetCache,
    CCOperationalAgent,
    CCScopeGrant,
    CCSite,
)
from harkeniq_cc.db.repos import (
    ApprovalPolicyRepo,
    ApprovalRecordRepo,
    CampaignRepo,
    ScopeGrantRepo,
)
from harkeniq_cc.runtime import AppState
from harkeniq_cc.scope import (
    ENFORCEMENT_STRICT,
    PRINCIPAL_USER,
    SCOPE_DEVICE,
    SCOPE_DEVICE_CLASS,
    SCOPE_SITE,
    SCOPE_TENANT,
    resolve,
)
from harkeniq_cc.target_authority import (
    WAVE_PERMISSION,
    AuthorizedTarget,
    TargetIntegrityError,
    all_targets_covered,
    uncovered_targets,
    wave_subject_matches,
    wave_targets,
)
from harkeniq_sm.approvals import ApprovalService
from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.base import (
    create_all as sm_create_all,
    make_engine as sm_engine,
    make_sessionmaker as sm_sessionmaker,
)
from harkeniq_sm.db.models import Device, Site
from harkeniq_sm.grpc_server import SiteManagerServiceServicer

from tests.unit.cc.conftest import seed_tenant_admin

TENANT = "t-s1"
OPERATOR = list(ROLE_PERMISSIONS["operator"])
VIEWER = list(ROLE_PERMISSIONS["viewer"])
SERVER = declare("redfish", ["IDENTIFY_LED", "COLLECT_DIAGNOSTICS"], "server")
SWITCH = declare("gnmi", ["INTERFACE_DISABLE"], "switch")
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)

SRC = pathlib.Path(__file__).resolve().parents[3] / (
    "services/central_command/src/harkeniq_cc"
)


# ---------------------------------------------------------------------------
# Part A -- the pure helpers, against the REAL resolver
# ---------------------------------------------------------------------------


class _Row:
    """A cc_scope_grants row, as `resolve()` reads one."""

    def __init__(self, scope_type, scope_ref="", subset=None, role="operator",
                 expires_at=None, revoked_at=None):
        self.scope_type, self.scope_ref = scope_type, scope_ref
        self.permission_subset, self.role = subset, role
        self.expires_at, self.revoked_at = expires_at, revoked_at


class _Site:
    def __init__(self, sid):
        self.id, self.org_unit_id = sid, None


class _Fleet:
    def __init__(self, agent_id, site_id, device_class):
        self.agent_id, self.site_id, self.device_class = agent_id, site_id, device_class


class _Wave:
    def __init__(self, devices, site="s1", campaign_id="c1", version=1, index=0,
                 plan_hash="ph"):
        self.campaign_id, self.campaign_version = campaign_id, version
        self.site_id, self.wave_index, self.plan_hash = site, index, plan_hash
        self.device_agent_ids = list(devices)
        self.subject_ref = wave_subject_ref(
            campaign_id, version, site, index, devices, plan_hash,
        )


FLEET = [
    _Fleet("a", "s1", "server"),
    _Fleet("b", "s1", "server"),
    _Fleet("c", "s1", "server"),
    _Fleet("x", "s2", "server"),
    _Fleet("sw", "s1", "switch"),
    _Fleet("blank", "s1", ""),
]


def _scope(*rows, role_permissions=OPERATOR, now=NOW):
    return resolve(
        tenant_id=TENANT, principal_type=PRINCIPAL_USER, principal_ref="p",
        role_permissions=role_permissions, grant_rows=list(rows),
        sites=[_Site("s1"), _Site("s2")], enforcement=ENFORCEMENT_STRICT,
        now=now,
    )


def _abc():
    return wave_targets(_Wave(["a", "b", "c"]), FLEET)


class TestTheImmutableTargetSet:
    def test_the_set_is_identified_against_the_fleet_and_sorted(self):
        targets = wave_targets(_Wave(["c", "a", "b"]), FLEET)
        assert [t.device_agent_id for t in targets] == ["a", "b", "c"]
        assert {t.site_id for t in targets} == {"s1"}
        assert {t.device_class for t in targets} == {"server"}

    def test_an_empty_class_is_kept_empty_never_defaulted(self):
        """§8: empty/null class is NOT a wildcard, so it must not become
        'server' on the way to the gate."""
        (t,) = wave_targets(_Wave(["blank"]), FLEET)
        assert t.device_class == ""

    def test_an_empty_set_refuses(self):
        with pytest.raises(TargetIntegrityError) as exc:
            wave_targets(_Wave([]), FLEET)
        assert exc.value.reason == "empty_target_set"

    def test_a_duplicate_id_refuses_rather_than_dedupes(self):
        with pytest.raises(TargetIntegrityError) as exc:
            wave_targets(_Wave(["a", "b", "a"]), FLEET)
        assert exc.value.reason == "duplicate_target"
        assert exc.value.detail["devices"] == ["a"]

    def test_a_device_the_fleet_cannot_identify_refuses(self):
        with pytest.raises(TargetIntegrityError) as exc:
            wave_targets(_Wave(["a", "ghost"]), FLEET)
        assert exc.value.reason == "unknown_target"
        assert exc.value.detail["devices"] == ["ghost"]

    def test_a_device_at_another_site_is_not_this_waves_device(self):
        """`x` exists -- at s2. A wave at s1 naming it is a set Central
        Command cannot vouch for, not a device to authorize."""
        with pytest.raises(TargetIntegrityError) as exc:
            wave_targets(_Wave(["a", "x"], site="s1"), FLEET)
        assert exc.value.reason == "unknown_target"
        assert exc.value.detail["devices"] == ["x"]

    def test_the_input_is_not_mutated(self):
        wave = _Wave(["c", "a"])
        before = list(wave.device_agent_ids)
        wave_targets(wave, FLEET)
        assert wave.device_agent_ids == before


class TestTheSubjectIsAVerifiedBinding:
    def test_an_untouched_wave_matches_its_subject(self):
        assert wave_subject_matches(_Wave(["a", "b", "c"]))

    def test_a_tampered_device_list_no_longer_matches(self):
        wave = _Wave(["a", "b", "c"])
        wave.device_agent_ids.append("d")
        assert not wave_subject_matches(wave)

    def test_a_tampered_plan_hash_no_longer_matches(self):
        wave = _Wave(["a", "b", "c"])
        wave.plan_hash = "other"
        assert not wave_subject_matches(wave)

    def test_a_wave_with_no_subject_never_matches(self):
        wave = _Wave(["a"])
        wave.subject_ref = ""
        assert not wave_subject_matches(wave)


class TestEveryTargetNotAny:
    """The §9 matrix, at the predicate. `uncovered_targets` is the whole
    rule: an empty answer is ALLOW, anything else is DENY."""

    def test_device_a_only_leaves_b_and_c_uncovered(self):
        scope = _scope(_Row(SCOPE_DEVICE, "a"))
        assert uncovered_targets(scope, WAVE_PERMISSION, _abc()) == ("b", "c")
        assert not all_targets_covered(scope, WAVE_PERMISSION, _abc())

    def test_devices_a_and_b_leave_c_uncovered(self):
        scope = _scope(_Row(SCOPE_DEVICE, "a"), _Row(SCOPE_DEVICE, "b"))
        assert uncovered_targets(scope, WAVE_PERMISSION, _abc()) == ("c",)

    def test_devices_a_b_c_cover_the_set(self):
        scope = _scope(*(_Row(SCOPE_DEVICE, d) for d in "abc"))
        assert uncovered_targets(scope, WAVE_PERMISSION, _abc()) == ()
        assert all_targets_covered(scope, WAVE_PERMISSION, _abc())

    def test_a_tenant_grant_covers_the_set(self):
        assert uncovered_targets(_scope(_Row(SCOPE_TENANT)), WAVE_PERMISSION, _abc()) == ()

    def test_a_site_grant_on_the_waves_site_covers_the_set(self):
        assert uncovered_targets(_scope(_Row(SCOPE_SITE, "s1")), WAVE_PERMISSION, _abc()) == ()

    def test_a_site_grant_elsewhere_covers_nothing(self):
        assert uncovered_targets(
            _scope(_Row(SCOPE_SITE, "s2")), WAVE_PERMISSION, _abc()
        ) == ("a", "b", "c")

    def test_a_class_grant_covers_a_uniform_set(self):
        assert uncovered_targets(
            _scope(_Row(SCOPE_DEVICE_CLASS, "server")), WAVE_PERMISSION, _abc()
        ) == ()

    def test_a_class_grant_matching_only_some_targets_denies(self):
        mixed = wave_targets(_Wave(["a", "sw"]), FLEET)
        assert uncovered_targets(
            _scope(_Row(SCOPE_DEVICE_CLASS, "server")), WAVE_PERMISSION, mixed
        ) == ("sw",)
        assert uncovered_targets(
            _scope(_Row(SCOPE_DEVICE_CLASS, "switch")), WAVE_PERMISSION, mixed
        ) == ("a",)

    def test_an_empty_class_is_not_a_wildcard(self):
        """§8: a class grant never reaches a device whose class is unset."""
        targets = wave_targets(_Wave(["a", "blank"]), FLEET)
        assert uncovered_targets(
            _scope(_Row(SCOPE_DEVICE_CLASS, "server")), WAVE_PERMISSION, targets
        ) == ("blank",)

    def test_an_expired_grant_for_b_denies(self):
        scope = _scope(
            _Row(SCOPE_DEVICE, "a"),
            _Row(SCOPE_DEVICE, "b", expires_at=NOW - timedelta(seconds=1)),
            _Row(SCOPE_DEVICE, "c"),
        )
        assert uncovered_targets(scope, WAVE_PERMISSION, _abc()) == ("b",)

    def test_a_revoked_grant_for_c_denies(self):
        scope = _scope(
            _Row(SCOPE_DEVICE, "a"), _Row(SCOPE_DEVICE, "b"),
            _Row(SCOPE_DEVICE, "c", revoked_at=NOW - timedelta(seconds=1)),
        )
        assert uncovered_targets(scope, WAVE_PERMISSION, _abc()) == ("c",)

    def test_coverage_without_the_permission_is_not_authority(self):
        """Coverage and permission are checked on the SAME grant: a viewer
        who reaches every device still holds no action.approve."""
        scope = _scope(_Row(SCOPE_TENANT, role="viewer"), role_permissions=VIEWER)
        assert uncovered_targets(scope, WAVE_PERMISSION, _abc()) == ("a", "b", "c")

    def test_an_empty_target_set_is_not_nothing_to_refuse(self):
        with pytest.raises(TargetIntegrityError):
            uncovered_targets(_scope(_Row(SCOPE_TENANT)), WAVE_PERMISSION, ())

    def test_the_helper_takes_the_whole_set_and_changes_nothing(self):
        targets = _abc()
        scope = _scope(_Row(SCOPE_DEVICE, "a"))
        uncovered_targets(scope, WAVE_PERMISSION, targets)
        assert targets == _abc()


class TestOnePolicyGovernsTheSet:
    class _P:
        def __init__(self, pid, device_type="*", action_type="*", n=1):
            self.id, self.device_type, self.action_type = pid, device_type, action_type
            self.risk_level, self.status, self.required_approvers = "*", "active", n

    def test_a_uniform_set_resolves_to_one_policy(self):
        wildcard, servers = self._P("w"), self._P("s", device_type="server", n=2)
        policy, conflict = resolve_policy_for_classes(
            [wildcard, servers], action_type="IDENTIFY_LED",
            device_classes=["server", "server", "SERVER"],
        )
        assert policy is servers and conflict == ()

    def test_a_mixed_set_under_one_wildcard_is_not_a_conflict(self):
        wildcard = self._P("w")
        policy, conflict = resolve_policy_for_classes(
            [wildcard], action_type="IDENTIFY_LED", device_classes=["server", "switch"],
        )
        assert policy is wildcard and conflict == ()

    def test_classes_governed_differently_are_a_named_conflict(self):
        wildcard, switches = self._P("w"), self._P("sw", device_type="switch", n=2)
        policy, conflict = resolve_policy_for_classes(
            [wildcard, switches], action_type="IDENTIFY_LED",
            device_classes=["server", "switch"],
        )
        assert policy is None and conflict == ("sw", "w")

    def test_a_policy_for_one_class_and_none_for_another_is_a_conflict(self):
        """'No policy' is the single-approver default; a policy may demand
        more, so they are two different rules."""
        servers = self._P("s", device_type="server", n=2)
        policy, conflict = resolve_policy_for_classes(
            [servers], action_type="IDENTIFY_LED", device_classes=["server", "switch"],
        )
        assert policy is None and conflict == ("", "s")

    def test_an_empty_class_matches_only_wildcard(self):
        wildcard, servers = self._P("w"), self._P("s", device_type="server")
        policy, conflict = resolve_policy_for_classes(
            [wildcard, servers], action_type="IDENTIFY_LED", device_classes=[""],
        )
        assert policy is wildcard and conflict == ()


# ---------------------------------------------------------------------------
# Part B -- the approval route, with real personas and a real ledger
# ---------------------------------------------------------------------------


class Stack:
    def __init__(self, app, state):
        self.app, self.state = app, state
        self.sessionmaker = state.sessionmaker
        self.persona = ("kc-owner", "owner@example.com", "tenant_owner")

    def as_person(self, sub, email=None, role="operator"):
        self.persona = (sub, email or f"{sub}@example.com", role)
        return self

    def client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
        )


async def _stack() -> Stack:
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sm = make_sessionmaker(engine)
    state = AppState(
        config=CCConfig(tenant_id=TENANT, insecure=True), engine=engine, sessionmaker=sm,
    )
    app = create_app(state)
    # A23-5: a rowless tenant is STRICT. The acting principals hold real
    # grants, so the matrix runs under the posture a real tenant has.
    await seed_tenant_admin(sm, TENANT, "kc-owner")
    stack = Stack(app, state)

    async def _fake():
        sub, email, role = stack.persona
        return UserContext(
            user_id=sub, email=email, tenant_id=TENANT, role=role,
            permissions=list(ROLE_PERMISSIONS[role]),
        )

    app.dependency_overrides[get_current_user] = _fake
    return stack


async def _seed(stack, devices=(("a", "server"), ("b", "server"), ("c", "server")),
                elsewhere=(("x", "server"),)):
    """Two sites: the wave's, and one the wave never touches."""
    async with stack.sessionmaker() as session:
        s1 = CCSite(tenant_id=TENANT, site_name="DC-1", sm_endpoint="sm:50051", sm_token="t")
        s2 = CCSite(tenant_id=TENANT, site_name="DC-2", sm_endpoint="sm:50051", sm_token="t")
        session.add_all([s1, s2])
        await session.flush()
        for site, rows in ((s1, devices), (s2, elsewhere)):
            for agent_id, cls in rows:
                session.add(CCFleetCache(
                    site_id=site.id, agent_id=agent_id, agent_name=agent_id,
                    vendor="Dell", model="R750", device_class=cls,
                    observation="observed", health="OK",
                    capabilities=SWITCH if cls == "switch" else SERVER,
                ))
        await session.commit()
        return s1.id, s2.id


async def _grant(stack, sub, scope_type, scope_ref="", role="operator", expires_at=None):
    async with stack.sessionmaker() as session:
        await ScopeGrantRepo(session).grant(
            tenant_id=TENANT, principal_type="user", principal_ref=sub,
            scope_type=scope_type, scope_ref=scope_ref, role=role,
            granted_by="kc-owner", expires_at=expires_at,
        )
        await session.commit()


async def _revoke(stack, sub, scope_ref):
    async with stack.sessionmaker() as session:
        rows = (await session.execute(
            sa.select(CCScopeGrant).where(
                CCScopeGrant.principal_ref == sub, CCScopeGrant.scope_ref == scope_ref,
                CCScopeGrant.revoked_at.is_(None),
            )
        )).scalars().all()
        assert rows, f"no live grant on {scope_ref!r} to revoke"
        for row in rows:
            await ScopeGrantRepo(session).revoke(row, "kc-owner")
        await session.commit()


async def _wave(stack, site_id, devices, action="IDENTIFY_LED", tenant=TENANT,
                tamper_to=None):
    """A pending site-wave over `devices`. `tamper_to` rewrites the stored
    device list AFTER the subject was computed -- the out-of-band write the
    digest check exists to catch."""
    async with stack.sessionmaker() as session:
        repo = CampaignRepo(session)
        campaign = await repo.create(
            tenant_id=tenant, name="s1", action_type=action, params={},
            created_by="ops@example.com", status="awaiting_approval",
        )
        subject = wave_subject_ref(campaign.id, campaign.version, site_id, 0, devices, "ph")
        wave = await repo.add_wave(
            campaign_id=campaign.id, campaign_version=campaign.version,
            site_id=site_id, wave_index=0, plan_hash="ph",
            device_agent_ids=list(devices), domain_span=len(set(devices)),
            subject_ref=subject, status=WAVE_PENDING_APPROVAL,
        )
        if tamper_to is not None:
            wave.device_agent_ids = list(tamper_to)
        await session.commit()
        return campaign.id, subject


async def _policy(stack, **kw):
    async with stack.sessionmaker() as session:
        row = await ApprovalPolicyRepo(session).create(
            tenant_id=TENANT, created_by="kc-owner", **kw,
        )
        await session.commit()
        return row.id


async def _records(stack, subject):
    async with stack.sessionmaker() as session:
        return list(await ApprovalRecordRepo(session).list_for_subject(
            SUBJECT_CAMPAIGN_WAVE, subject,
        ))


async def _approve(stack, subject):
    async with stack.client() as c:
        return await c.post(f"/api/approvals/{subject}/approve")


class TestApprovingASiteWave:
    """Deny cases are controls: the persona that ALLOWS with complete
    coverage is the one that DENIES with one device's coverage gone."""

    @pytest.mark.asyncio
    async def test_covering_a_only_denies_and_records_nothing(self):
        stack = await _stack()
        s1, _ = await _seed(stack)
        await _grant(stack, "kc-a", SCOPE_DEVICE, "a")
        _, subject = await _wave(stack, s1, ["a", "b", "c"])
        r = await _approve(stack.as_person("kc-a"), subject)
        assert r.status_code == 403, r.text
        assert "2 of the 3 devices" in r.json()["detail"]
        assert await _records(stack, subject) == []

    @pytest.mark.asyncio
    async def test_covering_a_and_b_denies(self):
        stack = await _stack()
        s1, _ = await _seed(stack)
        await _grant(stack, "kc-ab", SCOPE_DEVICE, "a")
        await _grant(stack, "kc-ab", SCOPE_DEVICE, "b")
        _, subject = await _wave(stack, s1, ["a", "b", "c"])
        r = await _approve(stack.as_person("kc-ab"), subject)
        assert r.status_code == 403, r.text
        assert "1 of the 3 devices" in r.json()["detail"]
        assert await _records(stack, subject) == []

    @pytest.mark.asyncio
    async def test_covering_a_b_c_allows_and_the_ledger_names_the_set(self):
        stack = await _stack()
        s1, _ = await _seed(stack)
        for d in "abc":
            await _grant(stack, "kc-abc", SCOPE_DEVICE, d)
        _, subject = await _wave(stack, s1, ["a", "b", "c"])
        r = await _approve(stack.as_person("kc-abc"), subject)
        assert r.status_code == 200, r.text
        assert r.json()["decision"] == "approved"
        (record,) = await _records(stack, subject)
        snap = record.authority_snapshot
        assert snap["target_device_agent_ids"] == ["a", "b", "c"]
        assert snap["target_device_agent_id"] == ""  # no representative
        assert snap["target_device_classes"] == ["server"]
        async with stack.sessionmaker() as session:
            entry = (await session.execute(
                sa.select(CCAuditLog).where(
                    CCAuditLog.action == "approval.approved",
                    CCAuditLog.subject == subject,
                )
            )).scalar_one()
        assert entry.detail["device_agent_ids"] == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_the_control_only_one_variable_moves(self):
        """Same persona, same policy, same wave shape: complete coverage
        ALLOWS; revoke c alone and the next identical wave DENIES."""
        stack = await _stack()
        s1, _ = await _seed(stack)
        for d in "abc":
            await _grant(stack, "kc-abc", SCOPE_DEVICE, d)
        _, w1 = await _wave(stack, s1, ["a", "b", "c"])
        assert (await _approve(stack.as_person("kc-abc"), w1)).status_code == 200
        await _revoke(stack, "kc-abc", "c")
        _, w2 = await _wave(stack, s1, ["a", "b", "c"])
        r = await _approve(stack.as_person("kc-abc"), w2)
        assert r.status_code == 403, r.text
        assert await _records(stack, w2) == []

    @pytest.mark.asyncio
    async def test_a_tenant_wide_grant_allows(self):
        stack = await _stack()
        s1, _ = await _seed(stack)
        _, subject = await _wave(stack, s1, ["a", "b", "c"])
        assert (await _approve(stack, subject)).status_code == 200

    @pytest.mark.asyncio
    async def test_a_site_grant_on_the_waves_site_allows(self):
        stack = await _stack()
        s1, _ = await _seed(stack)
        await _grant(stack, "kc-site", SCOPE_SITE, s1)
        _, subject = await _wave(stack, s1, ["a", "b", "c"])
        assert (await _approve(stack.as_person("kc-site"), subject)).status_code == 200

    @pytest.mark.asyncio
    async def test_a_site_grant_elsewhere_denies(self):
        stack = await _stack()
        s1, s2 = await _seed(stack)
        await _grant(stack, "kc-other", SCOPE_SITE, s2)
        _, subject = await _wave(stack, s1, ["a", "b", "c"])
        r = await _approve(stack.as_person("kc-other"), subject)
        assert r.status_code == 403 and "3 of the 3" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_class_grant_matching_every_target_allows(self):
        stack = await _stack()
        s1, _ = await _seed(stack)
        await _grant(stack, "kc-servers", SCOPE_DEVICE_CLASS, "server")
        _, subject = await _wave(stack, s1, ["a", "b", "c"])
        assert (await _approve(stack.as_person("kc-servers"), subject)).status_code == 200

    @pytest.mark.asyncio
    async def test_a_class_grant_matching_only_some_targets_denies(self):
        stack = await _stack()
        s1, _ = await _seed(stack, devices=(("a", "server"), ("b", "server"), ("sw", "switch")))
        await _grant(stack, "kc-servers", SCOPE_DEVICE_CLASS, "server")
        _, subject = await _wave(stack, s1, ["a", "b", "sw"], action="COLLECT_DIAGNOSTICS")
        r = await _approve(stack.as_person("kc-servers"), subject)
        assert r.status_code == 403 and "1 of the 3" in r.json()["detail"]
        assert await _records(stack, subject) == []

    @pytest.mark.asyncio
    async def test_an_empty_class_is_not_reached_by_a_class_grant(self):
        stack = await _stack()
        s1, _ = await _seed(stack, devices=(("a", "server"), ("blank", "")))
        await _grant(stack, "kc-servers", SCOPE_DEVICE_CLASS, "server")
        _, subject = await _wave(stack, s1, ["a", "blank"])
        r = await _approve(stack.as_person("kc-servers"), subject)
        assert r.status_code == 403 and "1 of the 2" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_an_expired_grant_for_b_denies(self):
        stack = await _stack()
        s1, _ = await _seed(stack)
        await _grant(stack, "kc-lapsed", SCOPE_DEVICE, "a")
        await _grant(stack, "kc-lapsed", SCOPE_DEVICE, "b",
                     expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        await _grant(stack, "kc-lapsed", SCOPE_DEVICE, "c")
        _, subject = await _wave(stack, s1, ["a", "b", "c"])
        r = await _approve(stack.as_person("kc-lapsed"), subject)
        assert r.status_code == 403 and "1 of the 3" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_revoked_grant_for_c_denies(self):
        stack = await _stack()
        s1, _ = await _seed(stack)
        for d in "abc":
            await _grant(stack, "kc-rev", SCOPE_DEVICE, d)
        await _revoke(stack, "kc-rev", "c")
        _, subject = await _wave(stack, s1, ["a", "b", "c"])
        r = await _approve(stack.as_person("kc-rev"), subject)
        assert r.status_code == 403 and "1 of the 3" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_target_the_fleet_cannot_identify_refuses_even_the_owner(self):
        stack = await _stack()
        s1, _ = await _seed(stack)
        _, subject = await _wave(stack, s1, ["a", "ghost"])
        r = await _approve(stack, subject)
        assert r.status_code == 409, r.text
        assert "unknown_target" in r.json()["detail"]
        assert await _records(stack, subject) == []

    @pytest.mark.asyncio
    async def test_a_tampered_device_list_refuses_even_the_owner(self):
        stack = await _stack()
        s1, _ = await _seed(stack)
        _, subject = await _wave(stack, s1, ["a", "b"], tamper_to=["a", "b", "c"])
        r = await _approve(stack, subject)
        assert r.status_code == 409, r.text
        assert "no longer matches its approved subject" in r.json()["detail"]
        assert await _records(stack, subject) == []

    @pytest.mark.asyncio
    async def test_a_duplicate_device_refuses(self):
        stack = await _stack()
        s1, _ = await _seed(stack)
        _, subject = await _wave(stack, s1, ["a", "a", "b"])
        r = await _approve(stack, subject)
        assert r.status_code == 409 and "duplicate_target" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_another_tenants_wave_is_absent(self):
        stack = await _stack()
        s1, _ = await _seed(stack)
        _, subject = await _wave(stack, s1, ["a", "b", "c"], tenant="someone-else")
        assert (await _approve(stack, subject)).status_code == 404

    @pytest.mark.asyncio
    async def test_classes_governed_by_different_policies_refuse(self):
        stack = await _stack()
        s1, _ = await _seed(stack, devices=(("a", "server"), ("b", "server"), ("sw", "switch")))
        await _policy(stack, name="everything", action_type="*", required_approvers=1)
        await _policy(stack, name="switches-dual", device_type="switch", required_approvers=2)
        _, subject = await _wave(stack, s1, ["a", "b", "sw"], action="COLLECT_DIAGNOSTICS")
        r = await _approve(stack, subject)  # the OWNER: authority is not the question
        assert r.status_code == 409, r.text
        assert "different approval policies" in r.json()["detail"]
        assert await _records(stack, subject) == []

    @pytest.mark.asyncio
    async def test_a_uniform_wave_takes_its_classs_policy(self):
        """Not the wildcard, and not the first device's: the class's."""
        stack = await _stack()
        s1, _ = await _seed(stack)
        await _policy(stack, name="everything", action_type="*", required_approvers=1)
        await _policy(stack, name="servers-dual", device_type="server", required_approvers=2)
        _, subject = await _wave(stack, s1, ["a", "b", "c"])
        r = await _approve(stack, subject)
        assert r.status_code == 200, r.text
        assert r.json()["approval"]["required"] == 2
        assert r.json().get("decision") is None  # 1 of 2, nothing executed

    @pytest.mark.asyncio
    async def test_partial_coverage_does_not_compose_across_approvers(self):
        """Under a two-approver policy X covering [a] and Y covering [b, c]
        are two approvers who each fail, not one approval of [a, b, c]."""
        stack = await _stack()
        s1, _ = await _seed(stack)
        await _policy(stack, name="dual", action_type="*", required_approvers=2)
        await _grant(stack, "kc-x", SCOPE_DEVICE, "a")
        await _grant(stack, "kc-y", SCOPE_DEVICE, "b")
        await _grant(stack, "kc-y", SCOPE_DEVICE, "c")
        _, subject = await _wave(stack, s1, ["a", "b", "c"])
        assert (await _approve(stack.as_person("kc-x"), subject)).status_code == 403
        assert (await _approve(stack.as_person("kc-y"), subject)).status_code == 403
        assert await _records(stack, subject) == []
        # Two approvers who each cover the whole set DO complete it.
        for d in "abc":
            await _grant(stack, "kc-z", SCOPE_DEVICE, d)
        first = await _approve(stack.as_person("kc-z"), subject)
        assert first.status_code == 200 and first.json().get("decision") is None
        owner = stack.as_person("kc-owner", "owner@example.com", "tenant_owner")
        second = await _approve(owner, subject)
        assert second.status_code == 200, second.text
        assert second.json()["decision"] == "approved"

    @pytest.mark.asyncio
    async def test_a_single_device_wave_behaves_as_before(self):
        stack = await _stack()
        s1, _ = await _seed(stack)
        await _grant(stack, "kc-a", SCOPE_DEVICE, "a")
        _, subject = await _wave(stack, s1, ["a"])
        assert (await _approve(stack.as_person("kc-a"), subject)).status_code == 200


# ---------------------------------------------------------------------------
# Part C -- dispatch re-asks current authority over the whole set
# ---------------------------------------------------------------------------

CC_SITE_ID = "cc-site-flat"


@pytest.fixture
async def flat_stack():
    """A real Site Manager with THREE devices sharing no fault domain, so
    the planner puts all three in ONE wave -- the shape the all-target
    rule is about."""
    sm_db_engine = sm_engine("sqlite+aiosqlite:///:memory:")
    await sm_create_all(sm_db_engine)
    sm_db = sm_sessionmaker(sm_db_engine)
    async with sm_db() as session:
        site = Site(name="site-flat", cc_site_id=CC_SITE_ID, status="active")
        session.add(site)
        await session.flush()
        for i in (1, 2, 3):
            session.add(Device(
                id=f"dev-{i}", site_id=site.id, agent_id=f"node-{i}",
                agent_name=f"node-{i}", device_class="server",
            ))
        await session.commit()
    sm_config = SMConfig(insecure=True, site_name="site-flat")
    servicer = SiteManagerServiceServicer(
        sm_db, ApprovalService(sm_db, sm_config), sm_config,
    )
    server = grpc.aio.server()
    harkeniq_pb2_grpc.add_SiteManagerServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    cc_db_engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(cc_db_engine)
    cc_db = make_sessionmaker(cc_db_engine)
    async with cc_db() as session:
        session.add(CCSite(
            id=CC_SITE_ID, tenant_id=TENANT, site_name="DC-flat",
            sm_endpoint=f"127.0.0.1:{port}", sm_token="tok",
        ))
        await session.flush()
        for i in (1, 2, 3):
            session.add(CCFleetCache(
                site_id=CC_SITE_ID, agent_id=f"node-{i}", agent_name=f"node-{i}",
                vendor="Dell", model="R750", device_class="server",
                observation="observed", health="OK", capabilities=SERVER,
            ))
        await session.commit()
    await seed_tenant_admin(cc_db, TENANT, "kc-owner")
    state = AppState(
        config=CCConfig(tenant_id=TENANT, insecure=True),
        engine=cc_db_engine, sessionmaker=cc_db,
    )
    yield state, cc_db
    await server.stop(grace=None)
    await sm_db_engine.dispose()
    await cc_db_engine.dispose()


async def _planned_campaign(state, session, autonomous=False):
    repo = CampaignRepo(session)
    campaign = await repo.create(
        tenant_id=TENANT, name="Q3", action_type="IDENTIFY_LED",
        params={"target": "Drive 0"}, created_by="ops@example.com",
    )
    await repo.replace_scopes(campaign.id, [("site", CC_SITE_ID)])
    await session.commit()
    await preflight(
        session, state, tenant_id=TENANT, campaign=campaign,
        scope_rules=await repo.scopes(campaign.id), resolved_site_ids=[],
        actor="ops@example.com",
    )
    await build_waves(session, tenant_id=TENANT, campaign=campaign, autonomous=autonomous)
    campaign.status = "running"
    await session.commit()
    (wave,) = await repo.waves(campaign.id)
    assert sorted(wave.device_agent_ids) == ["node-1", "node-2", "node-3"], (
        "the flat estate must plan one wave of three"
    )
    return campaign, wave


async def _ledger_approve(session, wave, sub, email, role="operator"):
    """What `_decide_campaign_wave` writes once the gate passes."""
    wave.status = WAVE_APPROVED
    wave.decided_by = email
    await ApprovalRecordRepo(session).record(
        tenant_id=TENANT, subject_type=SUBJECT_CAMPAIGN_WAVE,
        subject_ref=wave.subject_ref, approver_ref=sub, approver_email=email,
        decision="approved",
        authority_snapshot={"role": role, "permission": WAVE_PERMISSION,
                            "target_site_id": wave.site_id,
                            "target_device_agent_ids": list(wave.device_agent_ids)},
    )


async def _device_grants(session, sub, *devices, role="operator", expires_at=None):
    for d in devices:
        await ScopeGrantRepo(session).grant(
            tenant_id=TENANT, principal_type="user", principal_ref=sub,
            scope_type=SCOPE_DEVICE, scope_ref=d, role=role,
            granted_by="kc-owner", expires_at=expires_at,
        )


async def _advance(state, session, campaign):
    result = await advance_campaign(session, state, tenant_id=TENANT, campaign=campaign)
    await session.commit()
    return result


async def _withheld_audits(session, campaign_id):
    return (await session.execute(
        sa.select(sa.func.count()).select_from(CCAuditLog).where(
            CCAuditLog.action == "campaign.wave_withheld",
            CCAuditLog.subject == campaign_id,
        )
    )).scalar_one()


class TestDispatchRevalidatesEveryTarget:
    @pytest.mark.asyncio
    async def test_current_authority_over_the_whole_set_dispatches(self, flat_stack):
        state, cc_db = flat_stack
        async with cc_db() as session:
            campaign, wave = await _planned_campaign(state, session)
            await _device_grants(session, "kc-abc", "node-1", "node-2", "node-3")
            await _ledger_approve(session, wave, "kc-abc", "abc@example.com")
            await session.commit()
            result = await _advance(state, session, campaign)
            assert result["advanced"], result
            (wave,) = await CampaignRepo(session).waves(campaign.id)
            assert wave.status == WAVE_DISPATCHED

    @pytest.mark.asyncio
    async def test_a_grant_revoked_after_approval_withholds_the_whole_wave(self, flat_stack):
        """§21: approved while authorized, revoke B, same approved wave -> no
        execution. The decision stands; nothing crosses CC -> SM."""
        state, cc_db = flat_stack
        async with cc_db() as session:
            campaign, wave = await _planned_campaign(state, session)
            await _device_grants(session, "kc-abc", "node-1", "node-2", "node-3")
            await _ledger_approve(session, wave, "kc-abc", "abc@example.com")
            await session.commit()
            grants = ScopeGrantRepo(session)
            (row,) = [g for g in await grants.list_for_principal(TENANT, "kc-abc")
                      if g.scope_ref == "node-2"]
            await grants.revoke(row, "kc-owner")
            await session.commit()

            result = await _advance(state, session, campaign)
            assert not result["advanced"], result
            (blocked,) = result["blocked"]
            assert "no longer hold 'action.approve' over every device" in blocked["reason"]

            repo = CampaignRepo(session)
            (wave,) = await repo.waves(campaign.id)
            assert wave.status == WAVE_APPROVED, "the historical decision is untouched"
            assert wave.decided_by == "abc@example.com"
            assert len(await ApprovalRecordRepo(session).list_for_subject(
                SUBJECT_CAMPAIGN_WAVE, wave.subject_ref)) == 1
            assert await repo.dispatches(campaign.id) == [], "nothing crossed CC -> SM"
            for target in await repo.targets(campaign.id):
                assert target.revalidation == REVAL_AUTHORITY_LOST
                assert "no longer hold" in target.revalidation_reason
            assert await _withheld_audits(session, campaign.id) == 1

            # A second pass is not a second audit entry.
            await _advance(state, session, campaign)
            assert await _withheld_audits(session, campaign.id) == 1
            assert await repo.dispatches(campaign.id) == []

            # Restore ONLY the revoked grant: the same approved wave dispatches.
            await _device_grants(session, "kc-abc", "node-2")
            await session.commit()
            result = await _advance(state, session, campaign)
            assert result["advanced"], result
            (wave,) = await repo.waves(campaign.id)
            assert wave.status == WAVE_DISPATCHED

    @pytest.mark.asyncio
    async def test_an_expired_grant_withholds(self, flat_stack):
        state, cc_db = flat_stack
        async with cc_db() as session:
            campaign, wave = await _planned_campaign(state, session)
            await _device_grants(session, "kc-abc", "node-1", "node-2")
            await _device_grants(session, "kc-abc", "node-3",
                                 expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
            await _ledger_approve(session, wave, "kc-abc", "abc@example.com")
            await session.commit()
            (row,) = [g for g in await ScopeGrantRepo(session).list_for_principal(TENANT, "kc-abc")
                      if g.scope_ref == "node-3"]
            row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.commit()
            result = await _advance(state, session, campaign)
            assert not result["advanced"] and result["blocked"], result

    @pytest.mark.asyncio
    async def test_an_approved_status_with_nobody_on_the_ledger_withholds(self, flat_stack):
        """A bare status write is not an approval. This is the state the
        old lifecycle fixture produced, and the gate must refuse it."""
        state, cc_db = flat_stack
        async with cc_db() as session:
            campaign, wave = await _planned_campaign(state, session)
            wave.status = WAVE_APPROVED
            await session.commit()
            result = await _advance(state, session, campaign)
            assert not result["advanced"], result
            assert "0 of 1 approvals still stand" in result["blocked"][0]["reason"]
            assert await CampaignRepo(session).dispatches(campaign.id) == []

    @pytest.mark.asyncio
    async def test_a_device_list_edited_after_approval_withholds(self, flat_stack):
        state, cc_db = flat_stack
        async with cc_db() as session:
            campaign, wave = await _planned_campaign(state, session)
            await _ledger_approve(session, wave, "kc-owner", "owner@example.com", role="tenant_owner")
            wave.device_agent_ids = list(wave.device_agent_ids) + ["node-99"]
            await session.commit()
            result = await _advance(state, session, campaign)
            assert not result["advanced"], result
            assert "no longer matches its approved subject" in result["blocked"][0]["reason"]

    @pytest.mark.asyncio
    async def test_two_approvers_who_each_cover_the_set_then_one_loses_authority(self, flat_stack):
        state, cc_db = flat_stack
        async with cc_db() as session:
            await ApprovalPolicyRepo(session).create(
                tenant_id=TENANT, name="dual", created_by="kc-owner",
                action_type="*", required_approvers=2,
            )
            campaign, wave = await _planned_campaign(state, session)
            await _device_grants(session, "kc-x", "node-1", "node-2", "node-3")
            await _device_grants(session, "kc-y", "node-1", "node-2", "node-3")
            await _ledger_approve(session, wave, "kc-x", "x@example.com")
            await ApprovalRecordRepo(session).record(
                tenant_id=TENANT, subject_type=SUBJECT_CAMPAIGN_WAVE,
                subject_ref=wave.subject_ref, approver_ref="kc-y",
                approver_email="y@example.com", decision="approved",
                authority_snapshot={"role": "operator"},
            )
            await session.commit()
            (row,) = [g for g in await ScopeGrantRepo(session).list_for_principal(TENANT, "kc-y")
                      if g.scope_ref == "node-1"]
            await ScopeGrantRepo(session).revoke(row, "kc-owner")
            await session.commit()
            result = await _advance(state, session, campaign)
            assert not result["advanced"], result
            assert "1 of 2 approvals still stand" in result["blocked"][0]["reason"]

    @pytest.mark.asyncio
    async def test_an_autonomous_wave_carries_no_human_authority_to_revalidate(self, flat_stack):
        state, cc_db = flat_stack
        async with cc_db() as session:
            campaign, wave = await _planned_campaign(state, session, autonomous=True)
            assert wave.status == WAVE_AUTONOMOUS and not wave.subject_ref
            result = await _advance(state, session, campaign)
            assert result["advanced"], result


# ---------------------------------------------------------------------------
# Part D -- skill delivery re-resolves reach at activation
# ---------------------------------------------------------------------------


class _FakeSM:
    installs: list = []

    def __init__(self, *_a, **_kw):
        pass

    async def install_skill(self, endpoint, token, **kw):
        _FakeSM.installs.append(kw)
        return {"accepted": True, "queued": len(kw["device_agent_ids"]), "reason": ""}


class _FakeSkill:
    name, version, raw_yaml, rules = "fan-health", "1", "name: fan-health\n", []


class TestSkillDeliveryNarrowsToCurrentReach:
    @pytest.mark.asyncio
    async def test_a_device_the_agent_no_longer_reaches_is_skipped_with_a_reason(self, monkeypatch):
        from harkeniq_cc import agent_lifecycle, skill_fetch, sm_client

        _FakeSM.installs = []
        monkeypatch.setattr(sm_client, "SMClient", _FakeSM)

        async def _fetch(state, tenant_id, skill_id):
            return _FakeSkill(), None

        monkeypatch.setattr(skill_fetch, "fetch_skill_definition", _fetch)

        engine = make_engine("sqlite+aiosqlite:///:memory:")
        await create_all(engine)
        sm = make_sessionmaker(engine)
        state = AppState(config=CCConfig(tenant_id=TENANT, insecure=True),
                         engine=engine, sessionmaker=sm)
        async with sm() as session:
            site = CCSite(tenant_id=TENANT, site_name="DC", sm_endpoint="sm:50051", sm_token="t")
            session.add(site)
            await session.flush()
            for d in ("node-1", "node-2"):
                session.add(CCFleetCache(
                    site_id=site.id, agent_id=d, agent_name=d, vendor="Dell",
                    model="R750", device_class="server", observation="observed",
                    health="OK", capabilities=SERVER,
                ))
            agent = CCOperationalAgent(
                tenant_id=TENANT, name="narrow", status="active", version=1,
                activated_version=1, created_by="t", autonomy_ceiling=0,
            )
            session.add(agent)
            await session.flush()
            # Preflight saw BOTH devices; the grant on node-2 has since gone.
            session.add(CCScopeGrant(
                tenant_id=TENANT, principal_type="agent", principal_ref=agent.id,
                scope_type=SCOPE_DEVICE, scope_ref="node-1", granted_by="t",
            ))
            await session.commit()
            preflight_result = {
                "skills": [{"skill_id": "fan-health", "usable": True,
                            "recommended": [], "version": "1"}],
                "devices": [
                    {"device_agent_id": "node-1", "site_id": site.id, "declared": False},
                    {"device_agent_id": "node-2", "site_id": site.id, "declared": False},
                ],
            }
            out = await agent_lifecycle.install_bound_skills(
                session, state, tenant_id=TENANT, agent=agent,
                preflight=preflight_result, actor="t",
            )
            await session.commit()
            assert out["authority_narrowed"] == ["node-2"]
            assert out["installed"] == 1 and out["skipped"] == 1
            (call,) = _FakeSM.installs
            assert call["device_agent_ids"] == ["node-1"]
            rows = (await session.execute(sa.select(CCAgentSkillInstall))).scalars().all()
            by_device = {r.device_agent_id: r for r in rows}
            assert by_device["node-1"].status == "queued"
            assert by_device["node-2"].status == "skipped"
            assert "no longer in the agent's current reach" in by_device["node-2"].detail
            # Narrow-only: nothing the preflight did not report was added.
            assert set(by_device) == {"node-1", "node-2"}


# ---------------------------------------------------------------------------
# Part E -- the structural guard
# ---------------------------------------------------------------------------

AUTHORIZATION_MODULES = (
    "api/approvals.py", "campaign_runner.py", "campaigns.py", "target_authority.py",
)
TARGET_LIST_NAMES = ("device_agent_ids", "targets", "devices", "device_ids")


def _representative_selections(path: pathlib.Path) -> list[str]:
    """`<target list>[0]` and `next(iter(<target list>))` in one module."""
    found = []
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            idx = node.slice
            if isinstance(idx, ast.Constant) and idx.value == 0:
                text = ast.unparse(node.value)
                if any(n in text for n in TARGET_LIST_NAMES):
                    found.append(f"{path.name}:{node.lineno} {ast.unparse(node)}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "next" and node.args:
            inner = node.args[0]
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) \
                    and inner.func.id == "iter":
                text = ast.unparse(inner)
                if any(n in text for n in TARGET_LIST_NAMES):
                    found.append(f"{path.name}:{node.lineno} {ast.unparse(node)}")
    return found


def _calls_by_function(name: str) -> set[str]:
    found = set()
    for rel in AUTHORIZATION_MODULES:
        path = SRC / rel
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                f = call.func
                called = f.attr if isinstance(f, ast.Attribute) else (
                    f.id if isinstance(f, ast.Name) else "")
                if called == name:
                    found.add(f"{path.name}:{node.name}")
    return found


class TestNoRepresentativeTarget:
    def test_no_authorization_module_selects_a_representative(self):
        offenders = [
            o for rel in AUTHORIZATION_MODULES
            for o in _representative_selections(SRC / rel)
        ]
        assert offenders == [], offenders

    def test_the_wave_approval_asks_every_target(self):
        assert "approvals.py:_decide_campaign_wave" in _calls_by_function("wave_targets")
        assert "approvals.py:_decide_campaign_wave" in _calls_by_function("wave_subject_matches")
        assert "approvals.py:_record_and_evaluate" in _calls_by_function("uncovered_targets")

    def test_the_dispatch_boundary_asks_every_target(self):
        assert _calls_by_function("wave_dispatch_authority") == {
            "campaign_runner.py:_advance_site",
        }
        assert "target_authority.py:revalidate_wave_authority" in _calls_by_function("uncovered_targets")

    def test_the_predicate_is_the_canonical_one(self):
        """`uncovered_targets` asks `permits` and nothing else -- no raw
        grant rows, no site_ids, no second resolver."""
        src = (SRC / "target_authority.py").read_text()
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "uncovered_targets")
        attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        assert "permits" in attrs
        assert not attrs & {"site_ids", "grants", "device_ids", "device_classes"}


# ---------------------------------------------------------------------------
# Part F -- adversarial cases (§20)
# ---------------------------------------------------------------------------


class TestAdversarialTargets:
    def test_target_order_carries_no_authority(self):
        """The same set in any order is the same subject and the same
        answer: sorted at digest, sorted at identification."""
        scope = _scope(_Row(SCOPE_DEVICE, "a"), _Row(SCOPE_DEVICE, "b"))
        for order in (["a", "b", "c"], ["c", "b", "a"], ["b", "c", "a"]):
            wave = _Wave(order)
            assert wave_subject_matches(wave)
            assert uncovered_targets(scope, WAVE_PERMISSION, wave_targets(wave, FLEET)) == ("c",)

    def test_overlapping_site_and_device_grants_do_not_double_count_or_deny(self):
        scope = _scope(_Row(SCOPE_SITE, "s1"), _Row(SCOPE_DEVICE, "a"))
        assert uncovered_targets(scope, WAVE_PERMISSION, _abc()) == ()

    def test_overlapping_class_and_device_grants(self):
        scope = _scope(_Row(SCOPE_DEVICE_CLASS, "server"), _Row(SCOPE_DEVICE, "sw"))
        mixed = wave_targets(_Wave(["a", "b", "sw"]), FLEET)
        assert uncovered_targets(scope, WAVE_PERMISSION, mixed) == ()
        # ...and losing the one device grant re-exposes exactly that device.
        assert uncovered_targets(
            _scope(_Row(SCOPE_DEVICE_CLASS, "server")), WAVE_PERMISSION, mixed
        ) == ("sw",)

    def test_an_unknown_class_value_matches_no_class_grant(self):
        """The class vocabulary is server|switch; a device carrying
        anything else is reached by no class grant (and by any device,
        site or tenant grant, exactly as before)."""
        fleet = FLEET + [_Fleet("odd", "s1", "blade")]
        targets = wave_targets(_Wave(["a", "odd"]), fleet)
        assert uncovered_targets(
            _scope(_Row(SCOPE_DEVICE_CLASS, "server")), WAVE_PERMISSION, targets
        ) == ("odd",)
        assert uncovered_targets(_scope(_Row(SCOPE_SITE, "s1")), WAVE_PERMISSION, targets) == ()

    def test_a_maximum_length_device_id_is_authorized_like_any_other(self):
        """cc_fleet_cache.agent_id is String(255): an id that fills it is a
        device, and site authority reaches it. Width is not a deny."""
        long_id = "z" * 255
        fleet = FLEET + [_Fleet(long_id, "s1", "server")]
        targets = wave_targets(_Wave(["a", long_id]), fleet)
        assert uncovered_targets(_scope(_Row(SCOPE_SITE, "s1")), WAVE_PERMISSION, targets) == ()
        assert uncovered_targets(_scope(_Row(SCOPE_DEVICE, "a")), WAVE_PERMISSION, targets) == (long_id,)

    def test_an_over_length_id_the_fleet_cannot_hold_is_unknown(self):
        """Nothing can identify a 300-character id: PostgreSQL refuses the
        fleet row, so the wave names a device that does not exist."""
        with pytest.raises(TargetIntegrityError) as exc:
            wave_targets(_Wave(["a", "q" * 300]), FLEET)
        assert exc.value.reason == "unknown_target"

    def test_a_malformed_id_is_unknown_not_matched_loosely(self):
        for bad in ("A", " a", "a ", "a\n", ""):
            with pytest.raises(TargetIntegrityError):
                wave_targets(_Wave(["b", bad]), FLEET)

    def test_grants_on_another_tenants_devices_confer_nothing_here(self):
        """Scope is resolved per tenant; a grant row is never consulted
        across one. The resolver receives only this tenant's rows, so a
        principal granted elsewhere resolves here to nothing."""
        scope = _scope()  # no rows in THIS tenant
        assert uncovered_targets(scope, WAVE_PERMISSION, _abc()) == ("a", "b", "c")
