"""A30.34 (D10): the hostile-serialization sweep, anchored on MACHINE_SURFACE.

A25.9's sweep walked the routes under one router PREFIX, the Operational
Agent router. Attention and Incidents became machine reads in A6-4A under
another prefix and were never swept -- while they served machines the
Console's payload, so any field added for a person reached an agent by
default (B2-F6). That is the same shape as B0c's metering gap, and it gets
the same cure: completeness is anchored on the DECLARATION, never on where
a handler happens to live.

So this module's targets are generated from `MACHINE_SURFACE` -- all 14
routes, including the response of the one write -- and the suite fails when
a declared route has no entry, or an entry names a route the plane does not
declare. Each entry names the machine projection that answers it, checked
against the handler the running app actually serves. Attention is the ONE
route still awaiting its projection; it is pinned by a strict expected
failure that A6-4B2-2 must retire.

One poisoned estate, one tenant-wide machine principal (the strongest
reader: it holds every site, so anything withheld from it is withheld by
the contract and not by scope), every route, every key and value at every
depth.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import import_module

import pytest

from harkeniq.capabilities import declare
from harkeniq_cc.db.models import (
    CCAgentCapability, CCAgentIdentity, CCAgentPreflight, CCAgentProposal,
    CCAgentSubmission, CCApprovalPolicy, CCApprovalRecord, CCFleetCache,
    CCIncident,
)
from harkeniq_cc.receipts import MACHINE_IDENTITY_FIELDS, MACHINE_INTERNAL_FIELDS
from harkeniq_cc.route_contract import (
    MACHINE_SURFACE, SURFACE_BOTH, SURFACE_MACHINE, route_handlers, called_names,
)

import sqlalchemy as sa

from tests.unit.cc import b2_estate as B
from tests.unit.cc.test_a6_2_machine_surface import (
    ACKNOWLEDGER, ACTIVATOR, APPROVER, BUILDER, DIRECTIVE, DISPATCH,
    EXEC_PAYLOAD, ISSUER, POLICY_NAME, PREFLIGHT_OPERATOR, RAW_EVIDENCE,
    ROTATOR,
)

PLANTED_IDENTITIES = (
    BUILDER, ACTIVATOR, ACKNOWLEDGER, PREFLIGHT_OPERATOR, ISSUER, ROTATOR,
    APPROVER, POLICY_NAME,
)
PLANTED_INTERNALS = (EXEC_PAYLOAD, RAW_EVIDENCE, DIRECTIVE, DISPATCH)

#: B2's withheld set (A30.34, D10): keys a machine incident or attention
#: response may never carry.
B2_WITHHELD = frozenset({
    "title", "correlation", "evidence_cited", "similar_past_incidents",
    "device_id", "action_id", "risk_score", "factors", "rank", "sites",
    "attention_driver_label", "reasons", "sample_count", "is_parent",
    "recommended_next",
})

AGENT = "b2-sweep-agent"


@dataclass(frozen=True)
class Target:
    """What one machine route is, and what the sweep holds it to."""

    #: Dotted names of the machine projection builders that answer it --
    #: each must be called by the handler the running app serves (or BE the
    #: handler, for a projection named inline). Empty only when `pending`.
    projection: tuple[str, ...] = ()
    #: The slice that owes this route its machine projection.
    pending: str = ""
    #: A22.2: the dry-run returns the agent's own REAL resolved parameters,
    #: evidence and rationale -- including the incident title its
    #: `evidence.observed` quotes. The one exemption from the internals and
    #: title checks, never from identity, correlation or generated text.
    dry_run: bool = False
    #: A24: the write answers with the resolved `params` of the agent's OWN
    #: just-admitted candidate -- the values its dry-run already showed it
    #: (A22.2). Exempt from the internals check for that one key; every
    #: other internal (evidence, rationale, authorization basis, directive,
    #: dispatch detail) is still refused, and so is every planted value.
    own_params: bool = False
    #: B2's withheld set applies (the incident contract, and Attention once
    #: B2-2 lands).
    b2: bool = False


_R = "harkeniq_cc.receipts."
_OA = "harkeniq_cc.api.operational_agents."
_IP = "harkeniq_cc.incident_projection."

SWEEP: dict[tuple[str, str], Target] = {
    ("GET", "/api/operational-agents/{agent_id}"):
        Target((_R + "machine_agent_view",)),
    ("GET", "/api/operational-agents/{agent_id}/runtime"):
        Target((_R + "machine_runtime_view",)),
    ("GET", "/api/operational-agents/{agent_id}/identity"):
        Target((_R + "machine_identity_view",)),
    ("GET", "/api/operational-agents/{agent_id}/preflight"):
        Target((_R + "machine_preflight_view",)),
    ("GET", "/api/operational-agents/{agent_id}/dry-run"):
        Target((_OA + "dry_run_agent",), dry_run=True),
    ("GET", "/api/operational-agents/{agent_id}/ingress"):
        Target(("harkeniq_cc.provenance.build_ingress_health",)),
    ("GET", "/api/operational-agents/{agent_id}/proposals"):
        Target((_R + "machine_proposal_items",)),
    ("GET", "/api/operational-agents/{agent_id}/proposals/{proposal_id}"):
        Target((_R + "build_receipt",)),
    ("GET", "/api/operational-agents/{agent_id}/submissions/{submission_id}"):
        Target((_R + "build_receipt",)),
    ("GET", "/api/attention/"):
        Target(pending="A6-4B2-2"),
    ("GET", "/api/incidents/"):
        Target((_IP + "machine_incident_list", _IP + "machine_incident_item"), b2=True),
    ("GET", "/api/incidents/{incident_id}"):
        Target((_IP + "machine_incident_detail", _IP + "machine_incident_item"), b2=True),
    ("GET", "/api/operational-agents/{agent_id}/discovery"):
        Target(("harkeniq_cc.discovery.build_discovery",)),
    ("POST", "/api/operational-agents/{agent_id}/proposals"):
        Target((_OA + "_submission_result",), own_params=True),
}

#: The routes still awaiting their machine projection. Exactly one, and it
#: must shrink to none.
PENDING = {route: t.pending for route, t in SWEEP.items() if t.pending}


def _resolve(dotted: str):
    module, _, name = dotted.rpartition(".")
    return getattr(import_module(module), name)


# ---------------------------------------------------------------------------
# The estate
# ---------------------------------------------------------------------------


async def _estate():
    """B2's poisoned incidents, plus a fully configured agent whose every
    governance act carries a human's name."""
    stack = await B.build()
    now = datetime.now(timezone.utc)
    async with stack.sessionmaker() as session:
        row = (await session.execute(sa.select(CCFleetCache).where(
            CCFleetCache.agent_id == stack.device("A")))).scalar_one()
        row.capabilities = declare(
            "redfish", ["SEL_CLEAR", "IDENTIFY_LED", "COLLECT_DIAGNOSTICS"], "server",
        )
        # A condition the dry-run can propose against: an SEL incident on
        # node-s3-a, poisoned like every other.
        session.add(CCIncident(
            incident_id=stack.tagged("b2-inc-a-log"), tenant_id=stack.tenant,
            site_id=stack.site("A"), device_agent_id=stack.device("A"),
            kind="device", subsystem="log", status="open", confidence=1.0,
            title=f"node-s3-a: log CRITICAL {B.TITLE}",
            correlation_meta={B.CORR_KEY: B.CORR_VALUE},
        ))
        from harkeniq_cc.db.models import CCOperationalAgent
        session.add(CCOperationalAgent(
            id=AGENT, tenant_id=stack.tenant, name=AGENT, status="active",
            version=1, activated_version=1, autonomy_ceiling=2,
            max_proposals_per_day=25, created_by=BUILDER, updated_by=BUILDER,
            activated_by=ACTIVATOR, activation_acknowledged_by=ACKNOWLEDGER,
            activation_acknowledged_version=1,
            activation_subject_ref="b2-act-subject",
        ))
        await session.flush()
        for kind, ref in (
            ("read", "attention"), ("read", "autonomy"), ("read", "incidents"),
            ("ingress", "proposals"), ("action_class", "SEL_CLEAR"),
        ):
            session.add(CCAgentCapability(
                agent_id=AGENT, tenant_id=stack.tenant, kind=kind, capability_ref=ref,
            ))
        session.add(CCAgentPreflight(
            agent_id=AGENT, tenant_id=stack.tenant, configuration_version=1,
            overall="ready", can_activate=True, requires_acknowledgement=True,
            requires_activation_approval=True, produced_by=PREFLIGHT_OPERATOR,
            result={
                "agent_id": AGENT, "tenant_id": stack.tenant,
                "configuration_version": 1, "overall": "ready",
                "can_activate": True, "requires_acknowledgement": True,
                "requires_activation_approval": True,
                "unattended_classes": ["SEL_CLEAR"], "blocked_dimensions": [],
                "warn_dimensions": [], "unknown_dimensions": [],
                "dimensions": [{"dimension": "capabilities", "verdict": "ready",
                                "detail": "one class bound"}],
                "by_dimension": {"capabilities": "ready"},
                "contract": {"authority": "grants nothing"},
            },
        ))
        session.add(CCAgentIdentity(
            tenant_id=stack.tenant, agent_id=AGENT, realm="tenant-demo",
            keycloak_client_id=f"op-agent-{AGENT}", keycloak_sub="sub-b2",
            status="active", issued_by=ISSUER, rotated_by=ROTATOR, rotated_at=now,
        ))
        session.add(CCApprovalPolicy(
            tenant_id=stack.tenant, name=POLICY_NAME, required_approvers=2,
            created_by="kc-owner",
        ))
        session.add(CCApprovalRecord(
            tenant_id=stack.tenant, subject_type="agent_activation",
            subject_ref="b2-act-subject", approver_ref="kc-approver",
            approver_email=APPROVER, decision="approved", scope_ok=True,
            decided_at=now,
        ))
        proposal = CCAgentProposal(
            tenant_id=stack.tenant, agent_id=AGENT, actor=f"op-agent:{AGENT}@v1",
            agent_version=1, site_id=stack.site("A"), device_agent_id=stack.device("A"),
            action_type="SEL_CLEAR", params={"secret_param": EXEC_PAYLOAD},
            rationale="because", evidence={"diagnosis": RAW_EVIDENCE},
            disposition="requires_approval", disposition_reason="needs a human",
            authorization_basis="human_approval", status="dispatched",
            decided_by=APPROVER, decided_at=now, dedupe_key="b2-sweep-k",
            directive_id=DIRECTIVE, dispatch_reason=DISPATCH, dispatched_at=now,
        )
        session.add(proposal)
        await session.flush()
        session.add(CCApprovalRecord(
            tenant_id=stack.tenant, subject_type="agent_proposal",
            subject_ref=proposal.id, approver_ref="kc-approver",
            approver_email=APPROVER, decision="approved", scope_ok=True,
            decided_at=now,
        ))
        submission = CCAgentSubmission(
            tenant_id=stack.tenant, agent_id=AGENT, agent_version=1,
            idempotency_key="b2-sweep-k", request_digest="d", candidate_ref="c",
            proposal_id=proposal.id, code="", reason="",
        )
        session.add(submission)
        await session.commit()
        ids = {"proposal_id": proposal.id, "submission_id": submission.id}
    await stack.grant(AGENT, "tenant", "", role="", principal_type="agent")
    stack.as_machine(AGENT)
    return stack, ids


async def _read_all(stack, ids) -> dict:
    """Every declared machine route, once, as the sweep agent."""
    out = {}
    async with stack.client() as client:
        dry = await client.get(f"/api/operational-agents/{AGENT}/dry-run")
        assert dry.status_code == 200, dry.text
        would = dry.json()["would_propose"]
        assert would, "the estate produced no candidate; the write is unswept"
        for (method, template) in sorted(SWEEP):
            url = template.format(
                agent_id=AGENT, incident_id=B.iid(stack, "a-disk"), **ids,
            )
            if method == "POST":
                res = await client.post(url, json={
                    "candidate_ref": would[0]["candidate_ref"],
                    "idempotency_key": "b2-sweep-idem-0001",
                })
                assert res.status_code in (200, 201), (template, res.text)
            else:
                res = await client.get(url, params={"status": "all"}
                                       if template == "/api/incidents/" else None)
                assert res.status_code == 200, (template, res.status_code, res.text[:300])
            out[(method, template)] = res.json()
    return out


def _walk(node, path=()):
    if isinstance(node, dict):
        for key, value in node.items():
            yield path + (key,), key, True
            yield from _walk(value, path + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, path + (index,))
    else:
        yield path, node, False


def _generated(path) -> bool:
    return any(
        path[i] == "diagnosis" and i + 1 < len(path) and path[i + 1] == "generated"
        for i in range(len(path))
    )


# ---------------------------------------------------------------------------
# Completeness: the sweep IS the declaration
# ---------------------------------------------------------------------------


class TestTheSweepIsTheDeclaration:
    def test_every_declared_route_has_an_entry_and_no_other_route_does(self):
        assert set(SWEEP) == set(MACHINE_SURFACE), (
            f"sweep and plane disagree: {sorted(set(SWEEP) ^ set(MACHINE_SURFACE))}. "
            "A route on the External Agent API plane must decide what it may say."
        )

    def test_the_plane_is_fourteen_routes_both_and_machine(self):
        assert len(SWEEP) == 14
        assert {s for s, _ in MACHINE_SURFACE.values()} == {SURFACE_BOTH, SURFACE_MACHINE}

    def test_every_route_but_the_pending_one_names_a_real_projection(self):
        for route, target in SWEEP.items():
            if target.pending:
                assert not target.projection, route
                continue
            assert target.projection, f"{route} names no machine projection"
            for dotted in target.projection:
                assert callable(_resolve(dotted)), (route, dotted)

    def test_the_named_projection_is_what_the_running_app_calls(self):
        """Not a list someone keeps: the handler the app SERVES must call it."""
        from harkeniq_cc.app import create_app
        from harkeniq_cc.auth import configure_auth
        from harkeniq_cc.config import CCConfig
        from harkeniq_cc.db.base import make_engine, make_sessionmaker
        from harkeniq_cc.runtime import AppState

        configure_auth("", "", "", insecure=True)
        engine = make_engine("sqlite+aiosqlite:///:memory:")
        app = create_app(AppState(
            config=CCConfig(tenant_id="t", insecure=True), engine=engine,
            sessionmaker=make_sessionmaker(engine),
        ))
        handlers = route_handlers(app)
        for route, target in SWEEP.items():
            handler = handlers[route]
            calls = called_names(handler)
            for dotted in target.projection:
                name = dotted.rpartition(".")[2]
                assert name == handler.__name__ or name in calls, (
                    f"{route} is answered by {handler.__name__}, which does not "
                    f"call {name}"
                )

    def test_exactly_one_route_awaits_its_projection(self):
        assert PENDING == {("GET", "/api/attention/"): "A6-4B2-2"}


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


class TestNoMachineRouteLeaks:
    async def test_no_operator_identity_on_any_route(self):
        stack, ids = await _estate()
        for route, payload in (await _read_all(stack, ids)).items():
            for path, leaf, is_key in _walk(payload):
                if is_key:
                    assert leaf not in MACHINE_IDENTITY_FIELDS, (route, path)
                elif isinstance(leaf, str):
                    for planted in PLANTED_IDENTITIES:
                        assert planted not in leaf, (route, path, planted)

    async def test_no_raw_correlation_on_any_route(self):
        stack, ids = await _estate()
        for route, payload in (await _read_all(stack, ids)).items():
            for path, leaf, is_key in _walk(payload):
                if isinstance(leaf, str):
                    for planted in B.NEVER_ON_A_MACHINE:
                        assert planted not in leaf, (route, path, planted)
                if is_key:
                    assert leaf != "correlation" and leaf != "correlation_meta", (route, path)

    async def test_generated_text_only_under_diagnosis_generated_on_any_route(self):
        stack, ids = await _estate()
        seen = 0
        for route, payload in (await _read_all(stack, ids)).items():
            for path, leaf, is_key in _walk(payload):
                if not is_key and isinstance(leaf, str) and B.GEN in leaf:
                    seen += 1
                    assert _generated(path), (route, path, leaf)
        assert seen, "no generated text reached any route; the check proved nothing"

    async def test_no_execution_internal_on_a_lifecycle_read(self):
        stack, ids = await _estate()
        for route, payload in (await _read_all(stack, ids)).items():
            target = SWEEP[route]
            if target.dry_run or target.pending:
                continue
            allowed = {"params"} if target.own_params else set()
            for path, leaf, is_key in _walk(payload):
                if is_key:
                    assert leaf not in MACHINE_INTERNAL_FIELDS - allowed, (route, path)
                elif isinstance(leaf, str):
                    for planted in PLANTED_INTERNALS:
                        assert planted not in leaf, (route, path, planted)

    async def test_the_write_exemption_is_its_own_params_and_nothing_else(self):
        stack, ids = await _estate()
        payload = (await _read_all(stack, ids))[
            ("POST", "/api/operational-agents/{agent_id}/proposals")]
        keys = {leaf for _p, leaf, is_key in _walk(payload) if is_key}
        assert keys & MACHINE_INTERNAL_FIELDS == {"params"}, keys & MACHINE_INTERNAL_FIELDS
        assert payload["accepted"] is True and payload["proposal"]["action_type"] == "SEL_CLEAR"

    async def test_titles_and_raw_bmc_text_reach_only_where_named(self):
        """The incident contract withholds the title and envelopes BMC text.
        The dry-run's A22.2 exemption is the one place either may appear,
        under its evidence and rationale -- recorded in A30.34, not changed."""
        stack, ids = await _estate()
        for route, payload in (await _read_all(stack, ids)).items():
            target = SWEEP[route]
            if target.pending:
                continue
            for path, leaf, is_key in _walk(payload):
                if is_key or not isinstance(leaf, str):
                    continue
                if B.TITLE in leaf:
                    assert target.dry_run and (
                        "evidence" in path or "rationale" in path), (route, path)
                if "BMCHOSTILE" in leaf:
                    if target.b2:
                        assert path[-1] == "component" and "reported" in path, (route, path)
                    else:
                        assert target.dry_run and "evidence" in path, (route, path)

    async def test_b2_withheld_set_on_the_incident_contract(self):
        stack, ids = await _estate()
        for route, payload in (await _read_all(stack, ids)).items():
            if not SWEEP[route].b2:
                continue
            keys = {leaf for _p, leaf, is_key in _walk(payload) if is_key}
            assert not (keys & B2_WITHHELD), (route, keys & B2_WITHHELD)

    async def test_the_sweep_is_not_vacuous(self):
        """A person reading the same estate DOES see every planted value."""
        stack, ids = await _estate()
        stack.as_person()
        async with stack.client() as client:
            detail = (await client.get(f"/api/operational-agents/{AGENT}")).json()
            incident = (await client.get(
                f"/api/incidents/{B.iid(stack, 'b-netamb')}")).json()
        assert BUILDER in json.dumps(detail) and EXEC_PAYLOAD in json.dumps(detail)
        assert B.CORR_VALUE in json.dumps(incident) and B.TITLE in json.dumps(incident)


class TestTheOnePendingRoute:
    @pytest.mark.xfail(
        strict=True,
        reason="A6-4B2-2 owes Attention its machine projection (A30.34). "
               "When it lands this passes, and the strict mark must go.",
    )
    async def test_attention_withholds_the_b2_set(self):
        stack, ids = await _estate()
        payload = (await _read_all(stack, ids))[("GET", "/api/attention/")]
        keys = {leaf for _p, leaf, is_key in _walk(payload) if is_key}
        assert not (keys & B2_WITHHELD), keys & B2_WITHHELD
        assert payload.get("view") == "machine"

    async def test_attention_already_obeys_the_universal_rules(self):
        """Pending its projection, Attention still carries no identity, no
        raw correlation and no generated text -- held today, not deferred."""
        stack, ids = await _estate()
        payload = (await _read_all(stack, ids))[("GET", "/api/attention/")]
        text = json.dumps(payload)
        for planted in PLANTED_IDENTITIES + B.NEVER_ON_A_MACHINE + (B.GEN,):
            assert planted not in text, planted
