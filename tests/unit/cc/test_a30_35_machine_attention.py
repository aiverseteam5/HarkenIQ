"""A6-4B2-2 (spec A30.35): the machine Attention contract.

Everything behavioural runs on the PRODUCTION stack -- the real `get_scope`,
persisted AGENT grants, STRICT (`tests/unit/cc/b2_2_estate.py`). No scope is
synthesised from a principal's permissions (A26.11).

THE ORACLE IS DELETION EQUIVALENCE (A30.26). For every scoped machine
persona, its Attention over the full, poisoned estate must equal its
Attention over an estate in which everything it may not read does not
exist -- decided by the estate's own restatement of the owner rule, not by
the code under test -- for every band and every limit. A tenant-wide machine
reads the difference (non-vacuity), and the unchanged human composer still
shows B2-F5 for the same scoped reader.

Where each ratified requirement is held:

    D2  select before aggregate          TestDeletionEquivalence,
                                         TestFilterBeforeAggregate
        hidden-estate non-influence      TestHiddenInfluence
        ordering / counts                TestOrderingIsolation, TestCountIsolation
    D3  incident permission composition  TestIncidentPermissionComposition
        embedded approval context        TestApprovalContext
    D7  machine next_step                TestNextStep
    D8  freshness                        TestFreshness
        raw correlation withheld         TestRawCorrelationWithheld
        generated-content trust          TestTrustEnvelopes
        the contract, named              TestTheContractIsNamed, TestTheViewIsTheToken
        human compatibility              TestHumanCompatibility
        B0c exactly-once                 TestExactlyOnce
        structural guards                TestStructure
        nothing moved                    TestNothingMoved
"""

from __future__ import annotations

import ast
import json
import pathlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

import harkeniq_cc
from harkeniq_cc import attention_projection as P
from harkeniq_cc import freshness as F
from harkeniq_cc.db.models import (
    CCAgentReadWindow,
    CCApprovalRoute,
    CCFleetCache,
    CCFleetPattern,
    CCIncident,
    CCLearnedSignal,
    CCOutcomeHistory,
    CCScopeGrant,
)
from harkeniq_cc.governance import (
    MachineAttentionSelection,
    load_machine_attention,
    require_machine_attention_selection,
)
from harkeniq_cc.receipts import MACHINE_IDENTITY_FIELDS, MACHINE_INTERNAL_FIELDS
from harkeniq_cc.route_contract import (
    JOB_ATTENTION, JOB_METER, MACHINE_SURFACE, ROUTE_CONTRACT, SURFACE_BOTH,
    meter_census,
)

from tests.unit.cc import b2_2_estate as E
from tests.unit.cc import s3_estate as S3

GOLDEN = pathlib.Path(__file__).parent / "golden" / "a30_35_human_attention.json"
SRC = pathlib.Path(harkeniq_cc.__file__).parent

ITEM_KEYS = {
    "order", "target", "labels", "reported", "device_class", "health",
    "observation", "driver", "risk", "cves", "warranty_state", "prior_learning",
    "fleet_patterns", "incidents", "approvals", "freshness", "next_step",
}
ANSWER_KEYS = {"view", "contract", "contract_version", "as_of", "returned", "items"}
#: B2's withheld set (A30.34 D10) -- the Console's Attention fields among them.
B2_WITHHELD = frozenset({
    "title", "correlation", "evidence_cited", "similar_past_incidents",
    "device_id", "action_id", "risk_score", "factors", "rank", "sites",
    "attention_driver_label", "reasons", "sample_count", "is_parent",
    "recommended_next",
})
#: Authority-shaped keys a next step, or anything else here, must never carry.
AUTHORITY = frozenset({
    "requires_approval", "available", "unavailable_reason", "can_execute",
    "authorized", "safe_to_execute", "capability", "propose_action",
    "candidate_ref", "summary", "policy", "required_approvers",
})
NEVER_KEYS = B2_WITHHELD | AUTHORITY | MACHINE_INTERNAL_FIELDS | MACHINE_IDENTITY_FIELDS | {
    "correlation_meta", "tenant_id", "generated_at", "cohort_failure_rate",
    "explanation", "diagnosis", "generated",
}

BANDS = (None, "high", "medium", "low", "insufficient_data", "no-such-band")
#: Personas whose answer is non-empty: the band x limit matrix runs for them.
READERS = ("m-org-ab", "m-site-a", "m-device-a1", "m-class-switch", "m-mixed",
           "m-incident-c", "m-attention-only")
EMPTY = ("m-revoked", "m-expired", "m-lapsed-all", "m-none")


async def _reads(stack, agent_id: str) -> int:
    async with stack.sessionmaker() as session:
        return int((await session.execute(
            sa.select(sa.func.coalesce(sa.func.sum(CCAgentReadWindow.reads), 0))
            .where(CCAgentReadWindow.tenant_id == stack.tenant,
                   CCAgentReadWindow.agent_id == agent_id)
        )).scalar_one())


async def _get(stack, **params):
    async with stack.client() as client:
        return await client.get("/api/attention/", params=params or None)


def _walk(node, path=()):
    yield from E.leaves(node, path)


def _devices(body) -> list[str]:
    return [i["target"]["device_agent_id"] for i in body["items"]]


async def _matrix(stack, name: str) -> dict:
    """Every band x every limit from 1 to N+1 (and none), normalised."""
    n = (await E.read(stack, name))["returned"]
    out = {}
    for band in BANDS:
        for limit in (None, *range(1, n + 2)):
            params = {k: v for k, v in (("band", band), ("limit", limit)) if v is not None}
            out[json.dumps(params, sort_keys=True)] = E.normalised(
                await E.read(stack, name, **params))
    return out


async def _human(stack, name: str, **params) -> dict:
    subject, role = await E.human(stack, name)
    stack.as_person(subject, role)
    res = await _get(stack, **params)
    assert res.status_code == 200, res.text
    stack.as_person()
    return res.json()


# ---------------------------------------------------------------------------
# D2: deletion equivalence -- the master oracle
# ---------------------------------------------------------------------------


class TestDeletionEquivalence:
    @pytest.mark.parametrize("name", READERS)
    async def test_every_band_and_every_limit_equals_the_estate_without_the_hidden(self, name):
        full = await E.build()
        reduced = await E.build(E.visible(name))
        assert await _matrix(full, name) == await _matrix(reduced, name), name

    @pytest.mark.parametrize("name", EMPTY)
    async def test_a_lapsed_or_absent_grant_reads_nothing_in_either_estate(self, name):
        full = await E.build()
        reduced = await E.build(E.visible(name))
        a, b = await E.read(full, name), await E.read(reduced, name)
        assert a["items"] == [] and a["returned"] == 0, name
        assert E.normalised(a) == E.normalised(b)

    async def test_non_vacuity_the_tenant_wide_machine_reads_the_difference(self):
        full = await E.build()
        for name in READERS:
            reduced = await E.build(E.visible(name))
            assert E.normalised(await E.read(full, "m-tenant")) != E.normalised(
                await E.read(reduced, "m-tenant")), (
                f"deleting what {name} may not read changed nothing for the "
                "tenant-wide control: the oracle proves nothing")

    async def test_a30_40_the_scoped_human_composer_no_longer_shows_b2_f5(self):
        """INVERTED by A30.40 (D-P2, D-P5), never deleted. B2-2 recorded here
        that the human path over the SAME persona's two estates DID differ --
        the tenant cohort decided a2 -- and left that residual for people. A
        person's attention is now composed from their own selection: the two
        estates agree on a2, still scored on a cohort prior (of the person's
        current view), and only the tenant-wide control tells them apart."""
        full = await E.build()
        reduced = await E.build(E.visible("m-site-a"))
        a = await _human(full, "h-site-a")
        b = await _human(reduced, "h-site-a")

        def a2(body, stack):
            return next(i for i in body["items"] if i["agent_id"] == E.agent(stack, "a2"))

        assert a2(a, full)["confidence"]["basis"] == "cohort_prior"
        assert a2(a, full) == a2(b, reduced), "B2-F5 reproduced for a scoped person"
        owner_full = await _human(full, "h-owner")
        owner_reduced = await _human(reduced, "h-owner")
        assert a2(owner_full, full)["risk_score"] != a2(owner_reduced, reduced)["risk_score"], (
            "the tenant-wide control no longer tells the estates apart")


# ---------------------------------------------------------------------------
# D2: select, then compose
# ---------------------------------------------------------------------------


class TestFilterBeforeAggregate:
    async def test_a_machine_is_never_scored_on_a_cohort(self):
        full = await E.build()
        for name in ("m-tenant",) + READERS:
            for item in (await E.read(full, name))["items"]:
                assert item["risk"]["basis"] in ("device_history", "insufficient_data"), (
                    name, item["risk"])

    async def test_too_little_history_is_insufficient_data_not_a_borrowed_band(self):
        full = await E.build()
        machine = {
            i["target"]["device_agent_id"]: i
            for i in (await E.read(full, "m-site-a"))["items"]
        }
        a2 = machine[E.agent(full, "a2")]
        assert a2["risk"] == {"basis": "insufficient_data", "band": "insufficient_data"}
        human = await _human(full, "h-site-a")
        h2 = next(i for i in human["items"] if i["agent_id"] == E.agent(full, "a2"))
        assert h2["confidence"]["basis"] == "cohort_prior"

    async def test_a_moved_devices_rows_at_a_site_not_held_are_not_its_history(self):
        """b22-mv failed six times at C before it moved to A. At C it no longer
        resolves, so those rows are C's (the owner rule): a reader of A does
        not score on them; a reader of C, or of the tenant, does."""
        full = await E.build()
        mv = E.agent(full, "mv")

        def risk(body):
            return next(i["risk"] for i in body["items"]
                        if i["target"]["device_agent_id"] == mv)

        assert risk(await E.read(full, "m-site-a")) == {
            "basis": "insufficient_data", "band": "insufficient_data"}
        assert risk(await E.read(full, "m-tenant")) == {
            "basis": "device_history", "band": "high"}
        assert risk(await E.read(full, "m-mixed")) == {
            "basis": "device_history", "band": "high"}

    async def test_the_outcome_read_applies_the_owner_rule_before_its_limit(self):
        """A window cut BEFORE the predicate would let hidden rows displace
        visible ones. Hidden rows are the OLDEST here, so a limit applied
        first would return nothing the reader may see."""
        from harkeniq_cc.db.repos import OutcomeHistoryRepo
        from harkeniq_cc.governance import machine_attention_selection, load_scope

        full = await E.build()
        agent_id = await E.machine(full, "m-site-a")
        async with full.sessionmaker() as session:
            for n in range(40):
                session.add(CCOutcomeHistory(
                    site_id=full.site("C"), action_id=f"old-{n}", action_type="SEL_CLEAR",
                    device_agent_id=E.agent(full, "c1"), vendor="Dell", model="R750",
                    outcome="FAILURE", recorded_at=E.T0 - timedelta(days=400),
                    ingested_at=E.T0,
                ))
            await session.commit()
            scope = await load_scope(
                session, tenant_id=full.tenant, principal_ref=agent_id,
                role_permissions=list(E.ALL_PERMISSIONS), principal_type="agent",
            )
            reach = machine_attention_selection(scope).fleet
            rows = await OutcomeHistoryRepo(session).list_device_outcome_dicts(
                full.tenant, limit=5, scope=reach,
            )
        assert len(rows) == 5
        assert {r["device_agent_id"] for r in rows} <= {
            E.agent(full, k) for k in ("a1", "a2", "a4", "mv")} | {full.tagged("b22-ghost")}

    async def test_no_hidden_pattern_or_signal_can_displace_a_visible_one(self):
        """The human path reads 200 patterns and 500 signals BEFORE S4 projects
        them (S4's recorded residual). A machine reads with no window."""
        full = await E.build()
        baseline = E.normalised(await E.read(full, "m-site-a"))
        async with full.sessionmaker() as session:
            for n in range(210):
                session.add(CCFleetPattern(
                    id=f"b22-flood-p{n}", tenant_id=full.tenant, pattern_type="batch_failure",
                    description=f"SECRET flood {n}",
                    affected_scope={"vendor": "Dell", "model": "R750",
                                    "action_type": "SEL_CLEAR", "sites": full.site("C")},
                    confidence=0.5, evidence={"site_failure_counts": {full.site("C"): 7777}},
                    status="active", detected_at=E.T0 + timedelta(minutes=n),
                ))
            for n in range(510):
                session.add(CCLearnedSignal(
                    id=f"b22-flood-s{n}", tenant_id=full.tenant, signal_key=f"flood:{n}",
                    scope_type="site", scope_ref=full.site("C"), action_type="SEL_CLEAR",
                    vendor="Dell", model="R750", statement=f"SECRET flood {n}",
                    evidence={}, confidence=0.99, source_pattern_id="", status="active",
                    observation_count=1, first_observed_at=E.T0, last_confirmed_at=E.T0,
                ))
            await session.commit()
        assert E.normalised(await E.read(full, "m-site-a")) == baseline
        # The human path keeps its windows -- recorded, not changed.
        human = await _human(full, "h-site-a")
        a1 = next(i for i in human["items"] if i["agent_id"] == E.agent(full, "a1"))
        assert a1["evidence"]["fleet_patterns"] == [], "the human window residual moved"


# ---------------------------------------------------------------------------
# Hidden-estate influence, planted
# ---------------------------------------------------------------------------


class TestHiddenInfluence:
    async def test_hidden_sentinels_appear_in_no_key_and_no_value(self):
        full = await E.build()
        for name in READERS + EMPTY:
            body = await E.read(full, name)
            hidden = [s for s in S3.ALL if s not in E.held_sites(name)]
            markers = E.site_markers(full, hidden)
            # A device or class reader reads its own devices wherever they sit.
            mine = {E.agent(full, k) for k in E.visible(name).devices}
            markers = [m for m in markers if m not in mine]
            if name in ("m-device-a1", "m-class-switch", "m-incident-c"):
                # context sites and readable incidents lend their ids to rows
                # about the reader's own devices (A30.25 R10)
                shown = {i["target"]["site_id"] for i in body["items"]}
                shown |= {o["incident_id"] for i in body["items"]
                          for o in i["incidents"].get("open", [])}
                shown |= {i["labels"]["site_name"] for i in body["items"]}
                markers = [m for m in markers if m not in shown]
            for path, leaf, _is_key in _walk(body):
                if isinstance(leaf, str):
                    for marker in markers:
                        assert marker not in leaf, (name, path, marker, leaf)

    async def test_the_never_set_is_absent_even_for_the_tenant(self):
        full = await E.build()
        for name in ("m-tenant",) + READERS:
            text = json.dumps(await E.read(full, name))
            for sentinel in E.NEVER_ON_MACHINE_ATTENTION:
                assert sentinel not in text, (name, sentinel)

    async def test_naming_a_hidden_site_answers_like_a_site_that_does_not_exist(self):
        """`?site_id=` narrows WITHIN what the machine may read. Asked for a
        site it does not hold, it must answer exactly as for no site at all
        -- anything else confirms the site (A30.26's probe oracle)."""
        full = await E.build()
        for name in ("m-site-a", "m-device-a1", "m-attention-only", "m-mixed"):
            for hidden in (s for s in S3.ALL if s not in E.held_sites(name)
                           and not any(E.DEVICES[k].site == s for k in E.visible(name).devices)):
                named = E.normalised(await E.read(full, name, site_id=full.site(hidden)))
                nowhere = E.normalised(await E.read(full, name, site_id="no-such-site"))
                assert named == nowhere, (name, hidden)
                assert named["items"] == [] and named["returned"] == 0

    async def test_poisoning_a_site_the_machine_does_not_hold_changes_nothing(self):
        """The live-gate scenario, in unit form: extreme, high-priority input
        planted at B and C AFTER a baseline -- critical fresh devices, pending
        actions, failures, fixable CVEs, incidents -- and the site-A machine's
        answer does not move by one byte, for every band and limit."""
        full = await E.build()
        before = await _matrix(full, "m-site-a")
        tenant_before = E.normalised(await E.read(full, "m-tenant"))
        async with full.sessionmaker() as session:
            for site in ("B", "C"):
                for n in range(3):
                    agent_id = full.tagged(f"b22-poison-{site}{n}")
                    session.add(CCFleetCache(
                        site_id=full.site(site), agent_id=agent_id, agent_name=agent_id,
                        vendor="Dell", model="R750", device_class="server",
                        observation="observed", health="Critical", service_tag="TAG-C1",
                        firmware=[{"component": "BIOS", "name": "BIOS", "version": "1.0"}],
                        last_seen_at=datetime.now(timezone.utc), snapshot_at=E.SNAPSHOT_AT,
                    ))
                    session.add(CCApprovalRoute(
                        site_id=full.site(site), action_id=f"{E.ACTION_ID}-p{site}{n}",
                        action_type="POWER_CYCLE", device_agent_id=agent_id,
                        routed_at=E.T0,
                    ))
                    session.add(CCIncident(
                        incident_id=full.tagged(f"b22-poison-inc-{site}{n}"),
                        tenant_id=full.tenant, site_id=full.site(site),
                        device_agent_id=agent_id, kind="device", subsystem="psu",
                        status="open", title=f"poison {E.TITLE}",
                        correlation_meta={E.CORR_KEY: E.CORR_VALUE},
                        opened_at=E.T0, first_seen_at=E.T0, last_seen_at=E.T0,
                    ))
                    for m in range(60):
                        session.add(CCOutcomeHistory(
                            site_id=full.site(site), action_id=f"poison-{site}{n}-{m}",
                            action_type="SEL_CLEAR", device_agent_id=agent_id,
                            vendor="Dell", model="R750", outcome="FAILURE",
                            recorded_at=E.T0, ingested_at=E.T0,
                        ))
            await session.commit()
        assert await _matrix(full, "m-site-a") == before
        assert E.normalised(await E.read(full, "m-tenant")) != tenant_before


# ---------------------------------------------------------------------------
# Ordering and counts
# ---------------------------------------------------------------------------


class TestOrderingIsolation:
    async def test_order_is_contiguous_and_the_hidden_leader_never_leads(self):
        full = await E.build()
        tenant = await E.read(full, "m-tenant")
        # The hidden extreme device leads the tenant-wide answer ...
        assert tenant["items"][0]["target"]["device_agent_id"] == E.agent(full, "c1")
        for name in READERS:
            body = await E.read(full, name)
            assert [i["order"] for i in body["items"]] == list(
                range(1, body["returned"] + 1)), name
            if "C" not in E.held_sites(name) and name != "m-class-switch":
                assert E.agent(full, "c1") not in _devices(body), name

    async def test_every_limit_is_a_prefix_of_the_unlimited_order(self):
        full = await E.build()
        for name in READERS:
            everything = await E.read(full, name)
            for limit in range(1, everything["returned"] + 1):
                page = await E.read(full, name, limit=limit)
                assert page["items"] == everything["items"][:limit], (name, limit)

    async def test_a_band_filter_never_renumbers_order(self):
        full = await E.build()
        everything = await E.read(full, "m-org-ab")
        order = {i["target"]["device_agent_id"]: i["order"] for i in everything["items"]}
        for band in ("high", "medium", "insufficient_data"):
            for item in (await E.read(full, "m-org-ab", band=band))["items"]:
                assert item["order"] == order[item["target"]["device_agent_id"]]
                assert item["risk"]["band"] == band


class TestCountIsolation:
    async def test_every_count_is_the_length_of_the_list_it_counts(self):
        full = await E.build()
        for name in ("m-tenant",) + READERS:
            body = await E.read(full, name)
            assert body["returned"] == len(body["items"])
            for item in body["items"]:
                approvals = item["approvals"]
                assert approvals["pending_count"] == len(approvals["pending"])
                if item["incidents"]["held"]:
                    assert item["incidents"]["open_count"] == len(item["incidents"]["open"])

    async def test_no_summary_rollup_or_rate_reaches_a_machine(self):
        full = await E.build()
        body = await E.read(full, "m-site-a")
        assert set(body) == ANSWER_KEYS
        for path, leaf, is_key in _walk(body):
            if isinstance(leaf, (int, float)) and not isinstance(leaf, bool):
                # the only numbers: order, counts and unit confidences
                assert path[-1] in ("order", "returned", "pending_count", "open_count",
                                    "confidence"), path


# ---------------------------------------------------------------------------
# D3: incident permission composition
# ---------------------------------------------------------------------------


class TestIncidentPermissionComposition:
    async def test_fleet_view_never_substitutes_for_incident_view(self):
        full = await E.build()
        body = await E.read(full, "m-attention-only")
        assert body["returned"] == 5
        for item in body["items"]:
            assert item["incidents"] == {"held": False}, item["incidents"]
            assert item["next_step"]["code"] not in ("review_diagnosis", "review_incident")
        text = json.dumps(body)
        for key in E.INCIDENTS:
            assert E.iid(full, key) not in text, key

    async def test_held_false_is_unknown_not_none(self):
        full = await E.build()
        body = await E.read(full, "m-attention-only")
        a1 = next(i for i in body["items"] if i["target"]["device_agent_id"] == E.agent(full, "a1"))
        # a1 HAS a diagnosed open incident; the machine is told it cannot know.
        assert a1["incidents"] == {"held": False}
        assert "open_count" not in a1["incidents"] and "open" not in a1["incidents"]

    async def test_mixed_grants_hold_one_site_and_not_the_other(self):
        full = await E.build()
        body = await E.read(full, "m-mixed")
        held = {i["target"]["device_agent_id"]: i["incidents"]["held"] for i in body["items"]}
        for key in ("a1", "a2", "a3", "a4", "mv"):
            assert held[E.agent(full, key)] is True, key
        for key in ("c1", "c2"):
            assert held[E.agent(full, key)] is False, key
        assert E.iid(full, "c1-psu") not in json.dumps(body)

    async def test_incident_view_without_fleet_view_adds_no_item(self):
        full = await E.build()
        body = await E.read(full, "m-incident-c")
        assert not {E.agent(full, k) for k in ("c1", "c2")} & set(_devices(body))
        # c1's incident is readable to this persona, but c1 is not in its
        # fleet reach: no item carries it.
        assert E.iid(full, "c1-psu") not in json.dumps(body)

    async def test_the_incident_read_is_narrowed_to_held_devices_in_sql(self):
        """The machine asks only for the devices it holds `incident.view` over,
        in the WHERE -- so a readable incident about any other device can
        never take a place in the window, or reach an item."""
        from harkeniq_cc.db.repos import IncidentRepo
        from harkeniq_cc.governance import load_scope, machine_attention_selection

        full = await E.build()
        agent_id = await E.machine(full, "m-incident-c")
        async with full.sessionmaker() as session:
            scope = await load_scope(
                session, tenant_id=full.tenant, principal_ref=agent_id,
                role_permissions=list(E.ALL_PERMISSIONS), principal_type="agent",
            )
            reach = machine_attention_selection(scope).incidents
            repo = IncidentRepo(session)
            everything = await repo.list_incidents(
                full.tenant, status="open", limit=1000, scope=reach)
            narrowed = await repo.list_incidents(
                full.tenant, status="open", limit=1000, scope=reach,
                device_agent_ids={E.agent(full, "a1")})
            none = await repo.list_incidents(
                full.tenant, status="open", limit=1000, scope=reach,
                device_agent_ids=set())
        assert E.iid(full, "c1-psu") in {r.incident_id for r in everything}
        assert {r.incident_id for r in narrowed} == {
            E.iid(full, "a1-disk"), E.iid(full, "a1-child")}
        assert list(none) == []

    async def test_device_and_class_grants_hold_their_own_devices(self):
        full = await E.build()
        a1 = (await E.read(full, "m-device-a1"))["items"][0]
        assert a1["incidents"]["held"] is True
        assert {o["incident_id"] for o in a1["incidents"]["open"]} == {
            E.iid(full, "a1-disk"), E.iid(full, "a1-child")}
        for item in (await E.read(full, "m-class-switch"))["items"]:
            assert item["incidents"]["held"] is True

    async def test_an_open_incident_carries_no_content(self):
        full = await E.build()
        for item in (await E.read(full, "m-tenant"))["items"]:
            for entry in item["incidents"].get("open", []):
                assert set(entry) == {"incident_id", "kind", "subsystem", "diagnosed",
                                      "opened_at"}

    async def test_a_resolved_incident_is_not_open(self):
        full = await E.build()
        text = json.dumps(await E.read(full, "m-tenant"))
        assert E.iid(full, "a2-old") not in text


class TestApprovalContext:
    async def test_pending_is_a_bounded_conclusion(self):
        full = await E.build()
        for name in ("m-tenant",) + READERS:
            for item in (await E.read(full, name))["items"]:
                for entry in item["approvals"]["pending"]:
                    assert set(entry) == {"action_type", "lane", "awaiting_since"}
                    assert entry["lane"] == "node"

    async def test_no_handle_and_no_decider_ever(self):
        full = await E.build()
        for name in ("m-tenant",) + READERS:
            text = json.dumps(await E.read(full, name))
            assert E.ACTION_ID not in text and E.APPROVER not in text, name

    async def test_approvals_follow_the_routes_reach_not_the_incident_reach(self):
        """A machine that cannot read incidents still learns an action is
        waiting on a human -- or it would propose the same work again."""
        full = await E.build()
        body = await E.read(full, "m-attention-only")
        a2 = next(i for i in body["items"] if i["target"]["device_agent_id"] == E.agent(full, "a2"))
        assert a2["approvals"]["pending_count"] == 1
        assert a2["next_step"] == {"code": "review_pending_approval",
                                   "refs": [{"type": "device", "id": E.agent(full, "a2")}]}

    async def test_a_decided_route_never_shows(self):
        full = await E.build()
        body = await E.read(full, "m-site-a")
        a1 = next(i for i in body["items"] if i["target"]["device_agent_id"] == E.agent(full, "a1"))
        assert a1["approvals"] == {"pending_count": 0, "pending": []}


# ---------------------------------------------------------------------------
# D7: the next step
# ---------------------------------------------------------------------------


class TestNextStep:
    async def test_codes_are_closed_and_refs_are_ids_shown_in_the_item(self):
        full = await E.build()
        for name in ("m-tenant",) + READERS:
            for item in (await E.read(full, name))["items"]:
                step = item["next_step"]
                assert set(step) == {"code", "refs"}
                assert step["code"] in P.NEXT_STEP_CODES, step
                shown = {
                    "device": {item["target"]["device_agent_id"]},
                    "incident": {o["incident_id"] for o in item["incidents"].get("open", [])},
                    "cve": {c["cve_id"] for c in item["cves"] if c["fix_available"] and c["cve_id"]},
                }
                for ref in step["refs"]:
                    assert set(ref) == {"type", "id"}
                    assert ref["id"] in shown[ref["type"]], (name, step)

    async def test_an_unheld_item_never_takes_an_incident_step(self):
        full = await E.build()
        for name in ("m-tenant",) + READERS:
            for item in (await E.read(full, name))["items"]:
                if not item["incidents"]["held"]:
                    assert item["next_step"]["code"] not in (
                        "review_diagnosis", "review_incident"), (name, item["next_step"])

    async def test_the_step_is_the_composers_own_decision(self):
        full = await E.build()
        steps = {i["target"]["device_agent_id"]: i["next_step"]
                 for i in (await E.read(full, "m-site-a"))["items"]}
        assert steps[E.agent(full, "a1")] == {"code": "review_diagnosis", "refs": [
            {"type": "incident", "id": E.iid(full, "a1-disk")}]}
        assert steps[E.agent(full, "a3")] == {"code": "plan_firmware_remediation", "refs": [
            {"type": "cve", "id": "CVE-2026-1001"}]}
        assert steps[E.agent(full, "a4")] == {"code": "investigate_device", "refs": [
            {"type": "device", "id": E.agent(full, "a4")}]}
        assert steps[E.agent(full, "mv")] == {"code": "collect_evidence", "refs": []}

    async def test_a_non_cve_advisory_takes_the_step_but_is_never_a_ref(self):
        full = await E.build()
        b2 = next(i for i in (await E.read(full, "m-org-ab"))["items"]
                  if i["target"]["device_agent_id"] == E.agent(full, "b2"))
        assert b2["next_step"] == {"code": "plan_firmware_remediation", "refs": []}
        assert [c["cve_id"] for c in b2["cves"]] == [None]
        assert "VENDOR-ADVISORY" not in json.dumps(b2)

    def test_the_step_rule_reads_no_prose(self):
        cves = [{"cve_id": "CVE-2026-1001", "fix_available": True}]
        for capability, code in (
            ("propose_action", "other"), ("POWER_CYCLE all now", "other"), (None, "other"),
            ("review_pending_approval", "review_pending_approval"),
        ):
            step = P.machine_next_step(capability, device_agent_id="d", cves=cves,
                                       incidents={"held": False})
            assert step["code"] == code


# ---------------------------------------------------------------------------
# D8: freshness
# ---------------------------------------------------------------------------

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def _old_runtime_rule(last_seen, now):
    """`/runtime`'s inline rule on main, restated as the oracle."""
    if last_seen is None:
        return "unknown"
    return "fresh" if (now - last_seen) <= timedelta(minutes=15) else "stale"


class TestFreshness:
    @pytest.mark.parametrize("last_seen,state", [
        (None, "unknown"),
        ("2026-09-27T11:59:00+00:00", "unknown"),       # not a timestamp object
        (NOW - timedelta(minutes=14, seconds=59), "fresh"),
        (NOW - timedelta(minutes=15), "fresh"),
        (NOW - timedelta(minutes=15, seconds=1), "stale"),
        ((NOW - timedelta(minutes=5)).replace(tzinfo=None), "fresh"),   # naive = UTC
        ((NOW - timedelta(hours=2)).replace(tzinfo=None), "stale"),
        (NOW + timedelta(minutes=3), "fresh"),          # a skewed clock, as before
    ])
    def test_the_boundaries(self, last_seen, state):
        assert F.freshness_state(last_seen, NOW) == state

    def test_it_is_runtimes_rule_for_every_zoned_value(self):
        for seconds in range(0, 3 * 3600, 7):
            seen = NOW - timedelta(seconds=seconds)
            assert F.freshness_state(seen, NOW) == _old_runtime_rule(seen, NOW), seconds

    async def test_each_item_reads_its_own_devices_reading(self):
        full = await E.build()
        states = {i["target"]["device_agent_id"]: i["freshness"]
                  for i in (await E.read(full, "m-site-a"))["items"]}
        expect = {"a1": "fresh", "a2": "stale", "a3": "unknown", "a4": "fresh", "mv": "stale"}
        for key, state in expect.items():
            block = states[E.agent(full, key)]
            assert block["state"] == state, key
            assert set(block) == {"state", "last_seen_at", "snapshot_at"}
        # a2's row was copied a minute ago; its reading is three hours old.
        a2 = states[E.agent(full, "a2")]
        assert a2["state"] == "stale" and a2["snapshot_at"] and a2["last_seen_at"]
        assert states[E.agent(full, "a3")]["last_seen_at"] is None

    async def test_runtime_and_attention_ask_the_same_rule(self):
        full = await E.build()
        agent_id = await E.enter(full, "m-site-a")
        attention = await E.read(full, "m-site-a")
        counts = {"fresh": 0, "stale": 0, "unknown": 0}
        for item in attention["items"]:
            counts[item["freshness"]["state"]] += 1
        full.as_person()
        async with full.client() as client:
            runtime = (await client.get(f"/api/operational-agents/{agent_id}/runtime")).json()
        assert runtime["devices"] == {
            "in_scope": attention["returned"],
            "seen_recently": counts["fresh"], "stale": counts["stale"],
            "never_reported": counts["unknown"],
        }

    async def test_freshness_decides_nothing(self):
        """Order and every other field are identical whatever the readings say:
        freshness is not a composer input."""
        full = await E.build()
        before = await E.read(full, "m-site-a")
        async with full.sessionmaker() as session:
            await session.execute(sa.update(CCFleetCache).values(last_seen_at=None))
            await session.commit()
        after = await E.read(full, "m-site-a")
        for a, b in zip(before["items"], after["items"]):
            a = {k: v for k, v in a.items() if k != "freshness"}
            b = {k: v for k, v in b.items() if k != "freshness"}
            assert a == b
        assert {i["freshness"]["state"] for i in after["items"]} == {"unknown"}

    def test_runtime_counts_through_the_function(self):
        tree = ast.parse((SRC / "agent_lifecycle.py").read_text())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "runtime_state")
        called = {getattr(n.func, "id", getattr(n.func, "attr", ""))
                  for n in ast.walk(fn) if isinstance(n, ast.Call)}
        assert "freshness_state" in called
        assert "timedelta" not in called, "a second copy of the window is back"


# ---------------------------------------------------------------------------
# Raw correlation and trust
# ---------------------------------------------------------------------------


class TestRawCorrelationWithheld:
    async def test_no_machine_reads_a_correlation_key_or_value(self):
        full = await E.build()
        for name in ("m-tenant",) + READERS:
            body = await E.read(full, name)
            for path, leaf, is_key in _walk(body):
                if is_key:
                    assert leaf not in ("correlation", "correlation_meta", "votes"), (name, path)
                elif isinstance(leaf, str):
                    assert E.CORR_KEY not in leaf and E.CORR_VALUE not in leaf, (name, path)
        # non-vacuous: a person reading the incident does see it
        full.as_person()
        async with full.client() as client:
            detail = (await client.get(f"/api/incidents/{E.iid(full, 'a1-disk')}")).json()
        assert E.CORR_VALUE in json.dumps(detail)


#: Where free text may sit, and the class it must be under.
ENVELOPES = {
    ("labels",): "operator_supplied",
    ("reported",): "untrusted_telemetry",
    ("cves", "installed"): "untrusted_telemetry",
    ("prior_learning", "statement"): "untrusted_telemetry",
    ("fleet_patterns", "description"): "untrusted_telemetry",
}


class TestTrustEnvelopes:
    async def test_every_string_is_a_code_an_id_a_time_or_enveloped(self):
        full = await E.build()
        body = await E.read(full, "m-tenant")
        for item in body["items"]:
            for path, leaf, is_key in _walk(item):
                if is_key or not isinstance(leaf, str) or path[-1] == "trust":
                    continue
                named = tuple(p for p in path if isinstance(p, str))
                envelope = next((cls for prefix, cls in ENVELOPES.items()
                                 if named[:len(prefix)] == prefix and len(named) > len(prefix)),
                                None)
                if envelope is None:
                    assert named[-1] in (
                        "device_agent_id", "site_id", "device_class", "health",
                        "observation", "driver", "basis", "band", "cve_id", "severity",
                        "warranty_state", "scope_type", "action_type", "last_confirmed_at",
                        "pattern_type", "incident_id", "kind", "subsystem", "opened_at",
                        "lane", "awaiting_since", "state", "last_seen_at", "snapshot_at",
                        "code", "type", "id",
                    ), (path, leaf)
                else:
                    holder = item
                    for part in path[:-1]:
                        holder = holder[part]
                    assert holder["trust"] == envelope, (path, holder.get("trust"))

    async def test_hostile_device_text_is_enveloped_as_telemetry(self):
        full = await E.build()
        body = await E.read(full, "m-site-a")
        seen = 0
        for path, leaf, _is_key in _walk(body):
            if isinstance(leaf, str) and "BMCHOSTILE" in leaf:
                seen += 1
                assert path[-1] == "component" and "installed" in path, path
        assert seen, "the hostile firmware name reached no item: the check proved nothing"

    async def test_attention_carries_no_generated_text(self):
        full = await E.build()
        for name in ("m-tenant",) + READERS:
            text = json.dumps(await E.read(full, name))
            assert E.GEN not in text and "suggested_action" not in text, name


# ---------------------------------------------------------------------------
# The contract, named -- and never selectable
# ---------------------------------------------------------------------------


class TestTheContractIsNamed:
    async def test_the_answer_and_every_item_are_exactly_the_contract(self):
        full = await E.build()
        body = await E.read(full, "m-tenant")
        assert set(body) == ANSWER_KEYS
        assert (body["view"], body["contract"], body["contract_version"]) == (
            "machine", "attention", "1")
        assert body["as_of"].endswith("+00:00")
        for item in body["items"]:
            assert set(item) == ITEM_KEYS
            assert set(item["target"]) == {"device_agent_id", "site_id", "site_contextual"}
            assert set(item["labels"]) == {"trust", "site_name"}
            assert set(item["reported"]) == {"trust", "device_name", "vendor", "model"}
            assert set(item["risk"]) == {"basis", "band"}
            assert set(item["approvals"]) == {"pending_count", "pending"}
            for cve in item["cves"]:
                assert set(cve) == {"cve_id", "severity", "fix_available", "installed"}
                assert set(cve["installed"]) == {"trust", "component", "version"}
            for pattern in item["fleet_patterns"]:
                assert set(pattern) == {"pattern_type", "description", "confidence"}
            for signal in item["prior_learning"]:
                assert set(signal) == {"scope_type", "site_id", "action_type",
                                       "statement", "confidence", "last_confirmed_at"}

    async def test_no_withheld_or_authority_key_at_any_depth(self):
        full = await E.build()
        for name in ("m-tenant",) + READERS:
            keys = {leaf for _p, leaf, is_key in _walk(await E.read(full, name)) if is_key}
            assert not keys & NEVER_KEYS, (name, keys & NEVER_KEYS)

    async def test_a_device_reader_sees_its_site_as_context(self):
        full = await E.build()
        a1 = (await E.read(full, "m-device-a1"))["items"][0]
        assert a1["target"] == {"device_agent_id": E.agent(full, "a1"),
                                "site_id": full.site("A"), "site_contextual": True}
        assert a1["labels"]["site_name"] == full.tagged("s3-alpha")
        site_a = (await E.read(full, "m-site-a"))["items"][0]
        assert site_a["target"]["site_contextual"] is False


class TestTheViewIsTheToken:
    async def test_a_machine_cannot_ask_for_the_human_payload(self):
        full = await E.build()
        await E.enter(full, "m-site-a")
        async with full.client() as client:
            for params, headers in (({"view": "human"}, {}), ({}, {"X-View": "human"}),
                                    ({"format": "console"}, {"Accept": "application/json"})):
                res = await client.get("/api/attention/", params=params, headers=headers)
                assert res.json()["view"] == "machine", (params, headers)

    async def test_a_person_never_receives_the_machine_view(self):
        full = await E.build()
        body = await _human(full, "h-site-a")
        assert "view" not in body and "items" in body and "sites" in body


# ---------------------------------------------------------------------------
# Human compatibility: byte-identical to a golden recorded from `main`
# ---------------------------------------------------------------------------


class TestHumanCompatibility:
    async def test_the_tenant_wide_human_read_equals_main(self):
        """The tenant owner's every read is byte-identical to main's recording.
        The golden file is unchanged: it is still main's."""
        golden = json.loads(GOLDEN.read_text())
        full = await E.build()
        assert (await E.human_payloads(full))["h-owner"] == golden["human"]["h-owner"]

    async def test_a30_40_scoped_human_reads_no_longer_equal_main(self):
        """INVERTED by A30.40 (D-P5, D-P6), never deleted. This test asserted
        that B2-2 changed no person's attention, scoped readers included. A
        scoped person's attention is now composed from their own selection
        and says so ("in your current view"), so each scoped persona's answer
        now differs from main's recording. What it now equals is proven
        against the reduced-estate oracle in test_a30_40_predictive_privacy."""
        golden = json.loads(GOLDEN.read_text())
        full = await E.build()
        now = await E.human_payloads(full)
        for name in ("h-site-a", "h-device-a1", "h-class-switch"):
            assert now[name] != golden["human"][name], name

    async def test_the_internal_decision_paths_equal_main(self):
        """The evaluator, the dry-run's reasoning and the ingress re-derivation
        still decide over the whole tenant (the E1 residual): unchanged."""
        golden = json.loads(GOLDEN.read_text())
        full = await E.build()
        assert await E.internal_payloads(full) == golden["internal"]

    def test_the_golden_carries_the_residual_it_proves_unchanged(self):
        golden = json.loads(GOLDEN.read_text())
        site_a = golden["human"]["h-site-a"]["{}"]
        bases = {i["agent_id"]: i["confidence"]["basis"] for i in site_a["items"]}
        assert bases["b22-a2"] == "cohort_prior"


# ---------------------------------------------------------------------------
# B0c: exactly one charge per machine read
# ---------------------------------------------------------------------------


class TestExactlyOnce:
    @pytest.mark.parametrize("name", ["m-tenant", "m-site-a", "m-attention-only",
                                      "m-revoked", "m-expired", "m-lapsed-all", "m-none"])
    async def test_every_attention_read_costs_one(self, name):
        full = await E.build()
        agent_id = await E.enter(full, name)
        for params in ({}, {"band": "high"}, {"limit": 1}, {"limit": "0"},
                       {"limit": "not-a-number"}, {"site_id": "no-such-site"}):
            before = await _reads(full, agent_id)
            res = await _get(full, **params)
            assert res.status_code in (200, 422), (name, params, res.status_code)
            assert await _reads(full, agent_id) - before == 1, (name, params)

    async def test_a_person_is_never_metered(self):
        full = await E.build()
        await _human(full, "h-site-a")
        await _human(full, "h-owner", limit=1)
        async with full.sessionmaker() as session:
            assert (await session.execute(sa.select(CCAgentReadWindow))).scalars().all() == []

    async def test_the_route_is_declared_and_the_census_is_clean(self):
        full = await E.build()
        assert MACHINE_SURFACE[("GET", "/api/attention/")] == (SURFACE_BOTH, JOB_ATTENTION)
        assert JOB_METER[JOB_ATTENTION] == "read"
        assert meter_census(full.app) == []

    async def test_revoking_the_grant_removes_the_payload_now(self):
        full = await E.build()
        agent_id = await E.enter(full, "m-site-a")
        assert (await E.read(full, "m-site-a"))["returned"] == 5
        async with full.sessionmaker() as session:
            grants = (await session.execute(
                sa.select(CCScopeGrant).where(CCScopeGrant.principal_ref == agent_id)
            )).scalars().all()
        for grant in grants:
            await full.lapse(grant.id, how="revoked")
        before = await _reads(full, agent_id)
        body = await E.read(full, "m-site-a")
        assert body["items"] == [] and body["returned"] == 0
        assert await _reads(full, agent_id) - before == 1


# ---------------------------------------------------------------------------
# Structural guards
# ---------------------------------------------------------------------------


def _tree(relative: str) -> ast.Module:
    return ast.parse((SRC / relative).read_text())


def _called(node) -> set[str]:
    return {getattr(n.func, "id", getattr(n.func, "attr", ""))
            for n in ast.walk(node) if isinstance(n, ast.Call)}


def _functions_calling(name: str) -> set[tuple[str, str]]:
    out = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and name in _called(fn):
                out.add((str(path.relative_to(SRC)), fn.name))
    return out


class TestStructure:
    def test_one_composer_one_body(self):
        assert _functions_calling("build_attention") == {("governance.py", "_compose_attention")}
        # A30.40 (D-P5): a person's selection enters the same body beside the
        # machine's; still ONE composer, ONE body.
        assert _functions_calling("_compose_attention") == {
            ("governance.py", "load_attention"), ("governance.py", "load_machine_attention"),
            ("governance.py", "load_human_attention")}

    def test_machine_attention_goes_through_its_own_selection(self):
        assert _functions_calling("load_machine_attention") == {("api/attention.py", "attention")}
        assert _functions_calling("MachineAttentionSelection") == {
            ("governance.py", "machine_attention_selection")}

    def test_the_machine_branch_never_reaches_the_tenant_wide_composer(self):
        tree = _tree("api/attention.py")
        branch = next(n for n in ast.walk(tree) if isinstance(n, ast.If)
                      and "is_machine" in _called(n.test))
        inside = _called(ast.Module(body=branch.body, type_ignores=[]))
        assert {"load_machine_attention", "machine_attention_selection",
                "machine_attention"} <= inside
        assert not inside & {"load_attention", "build_attention", "read_reach"}

    def test_the_projection_names_fields_and_never_spreads_or_strips(self):
        tree = _tree("attention_projection.py")
        for node in ast.walk(tree):
            assert not (isinstance(node, ast.Dict) and None in node.keys), "a ** spread"
            assert not isinstance(node, ast.Delete), "a del"
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in ("pop", "popitem", "update", "copy", "setdefault")
        assert "cohort" not in (SRC / "attention_projection.py").read_text().split(
            "BASES = frozenset(")[1].split(")")[0]

    def test_the_selection_refuses_anything_else(self):
        from harkeniq_cc.scope import ReadReach

        for wrong in (None, {}, ReadReach(tenant_id="t", permissions=("fleet.view",))):
            with pytest.raises(TypeError):
                require_machine_attention_selection(wrong)

    async def test_the_loader_refuses_a_bare_reach(self):
        from harkeniq_cc.scope import ReadReach

        full = await E.build()
        async with full.sessionmaker() as session:
            with pytest.raises(TypeError):
                await load_machine_attention(
                    session, tenant_id=full.tenant,
                    selection=ReadReach(tenant_id=full.tenant, permissions=("fleet.view",)),
                )
        assert MachineAttentionSelection.__dataclass_params__.frozen

    @pytest.mark.parametrize("fn,value", [
        (lambda v: P._lower_code(v, P.HEALTHS), "Critical-ish"),
        (lambda v: P._lower_code(v, P.OBSERVATIONS), 17),
        (lambda v: P._lower_code(v, P.DEVICE_CLASSES), "router"),
        (lambda v: P.machine_cve({"severity": v})["severity"], "catastrophic"),
        (lambda v: P.machine_pattern({"pattern_type": v})["pattern_type"], "new_kind"),
        (lambda v: P.machine_warranty_state({"end_date": v}, NOW), None),
        (lambda v: P.machine_next_step(v, device_agent_id="d", cves=[],
                                       incidents={"held": False})["code"], "propose_action"),
    ])
    def test_every_closed_vocabulary_falls_back(self, fn, value):
        assert fn(value) in ("other", "unknown")

    def test_an_unrecognised_driver_band_or_basis_is_other(self):
        item = {"agent_id": "d", "site_id": "s", "rank": 1, "attention_driver": "x",
                "band": "catastrophic", "confidence": {"basis": "cohort_prior"},
                "evidence": {}, "current_state": {}, "recommended_next": {}}
        fleet = SimpleNamespace(tenant_wide=True, covers_site=lambda s: True)
        out = P.machine_attention_item(item, row=None, held=False, fleet=fleet, now=NOW)
        assert out["driver"] == "other" and out["risk"] == {"basis": "other", "band": "other"}
        assert out["freshness"] == {"state": "unknown", "last_seen_at": None,
                                    "snapshot_at": None}

    def test_a_non_cve_shaped_id_is_null_never_echoed(self):
        for raw in ("VENDOR-1", "CVE-26-1", "CVE-2026-1 ", "cve-2026-1001", " CVE-2026-1001",
                    "CVE-2026-1001; rm -rf", 7):
            assert P.machine_cve({"cve_id": raw})["cve_id"] is None, raw
        assert P.machine_cve({"cve_id": "CVE-2026-12345"})["cve_id"] == "CVE-2026-12345"


class TestNothingMoved:
    def test_the_plane_and_the_contract_are_the_same_size(self):
        assert len(MACHINE_SURFACE) == 14
        assert sum(1 for m, _p in MACHINE_SURFACE if m == "GET") == 13
        assert len(ROUTE_CONTRACT) == 99

    def test_the_permissions_and_the_ceiling_are_unchanged(self):
        from harkeniq_cc.machine_identity import MACHINE_PRINCIPAL_CEILING
        from harkeniq_console.permissions import PERMISSIONS

        assert len(PERMISSIONS) == 25
        assert set(MACHINE_PRINCIPAL_CEILING) == {
            "fleet.view", "incident.view", "proposal.submit"}

    def test_no_migration(self):
        root = pathlib.Path(__file__).resolve().parents[3] / "services"
        heads = {
            "central_command/src/harkeniq_cc": "0027",
            "site_manager/src/harkeniq_sm": "0011",
            "console/src/harkeniq_console": "0004",
        }
        for package, head in heads.items():
            versions = root / package / "db" / "migrations" / "versions"
            names = sorted(p.name for p in versions.glob("[0-9]*.py"))
            assert names and names[-1].startswith(head), (package, names[-1:])
