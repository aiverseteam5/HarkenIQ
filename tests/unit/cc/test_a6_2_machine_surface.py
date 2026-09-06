"""A6-2 remediation: the WHOLE machine-readable Operational Agent surface.

The receipt endpoints were designed for a machine principal from the
start. The five routes beside them were not: `get_agent`, the agent
listing, the preflight read, the runtime read and the identity read all
became reachable by an authenticated agent, and every one of them kept
the projection it was written for -- the Console's.

Four defects, four properties, one module.

* **HIGH 1 -- projection follows authorization.** A machine receives an
  allow-listed projection built by NAMING what may pass. Not the rich
  payload with fields removed: a subtractive filter leaks the next field
  somebody adds upstream, which is precisely how this surface came to
  leak. Proven by hostile serialization -- planted values and forbidden
  keys are searched for RECURSIVELY through the whole document, on every
  machine-readable route, and the same estate is read by a human
  administrator to prove the rich projection survived for them.

* **HIGH 2 -- approval state is per proposal.** Completion is read from
  the ledger by `subject_ref = proposal.id`. Caching it under
  `(action_type, device_agent_id)` made two proposals sharing an agent, a
  device and an action class share one answer, decided by list order.

* **MEDIUM -- accounting identity is server-derived.** The bucket comes
  from the token, never from the path, the body or a query value; and a
  refusal is charged to the CALLER before any decision about the target.

* **LOW -- accounting owns its own transaction.** It never commits or
  rolls back work it did not create. Structural here; the durability
  proof is on real PostgreSQL, in
  `tests/integration/test_a6_read_accounting_isolation.py`, because
  in-memory sqlite is a StaticPool -- ONE shared connection -- so no two
  sessions can be isolated from each other there whatever the code does.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from harkeniq_cc.db.models import (
    CCAgentIdentity, CCAgentPreflight, CCAgentProposal, CCAgentReadWindow,
    CCApprovalPolicy, CCApprovalRecord, CCOperationalAgent, CCScopeGrant,
)
from harkeniq_cc.receipts import (
    MACHINE_IDENTITY_FIELDS, MACHINE_INTERNAL_FIELDS,
)

from tests.unit.cc.test_a6_2_receipt import (
    TENANT, _agent_row, _site, _stack, _work,
)

# `asyncio_mode = "auto"` (pyproject) drives the async tests; a global
# asyncio mark would only warn on the structural ones, which are sync.

PREFIX = "/api/operational-agents"

# ---------------------------------------------------------------------------
# Planted values. Every one of these is a real column a real deployment
# fills in, and none of them may reach a machine principal.
# ---------------------------------------------------------------------------

BUILDER = "builder@example.com"
ACTIVATOR = "activator@example.com"
ACKNOWLEDGER = "acknowledger@example.com"
PREFLIGHT_OPERATOR = "preflight-operator@example.com"
ISSUER = "issuer@example.com"
ROTATOR = "rotator@example.com"
APPROVER = "approver@example.com"
POLICY_NAME = "SECRET-DUAL-AUTHORIZATION-POLICY"
EXEC_PAYLOAD = "EXEC-PAYLOAD"
RAW_EVIDENCE = "RAW-EVIDENCE-BLOB"
DIRECTIVE = "dir-INTERNAL"
DISPATCH = "DISPATCH-INTERNALS"

#: Planted values that may never appear on ANY machine response.
PLANTED_IDENTITIES = (
    BUILDER, ACTIVATOR, ACKNOWLEDGER, PREFLIGHT_OPERATOR, ISSUER, ROTATOR,
    APPROVER, POLICY_NAME,
)
#: Planted values that may never appear on a LIFECYCLE/STATUS response.
PLANTED_INTERNALS = (EXEC_PAYLOAD, RAW_EVIDENCE, DIRECTIVE, DISPATCH)


def _body_without_docstring(fn):
    """The function's CODE, with its prose removed.

    These modules explain the defects they close, so a docstring names
    `session.rollback()` and `agent_id` on purpose. A structural test that
    grepped the source would be testing the explanation rather than the
    implementation -- the mistake A25 already made once and replaced with
    hostile input.
    """
    node = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
    if (
        node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    ):
        node.body = node.body[1:]
    return node


def _walk(node, keys: set, values: set) -> None:
    """Every key and every string, at every depth. Hostile by design.

    A top-level key check would pass a payload that nested the leak one
    level down, and `json.dumps` substring matching alone would miss a
    forbidden KEY whose value happened to be empty.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            keys.add(key)
            _walk(value, keys, values)
    elif isinstance(node, list):
        for item in node:
            _walk(item, keys, values)
    elif isinstance(node, str):
        values.add(node)


def _inspect(payload) -> tuple[set, set]:
    keys: set = set()
    values: set = set()
    _walk(payload, keys, values)
    return keys, values


async def _poisoned_estate(stack):
    """One agent, fully configured, with a human's name on every act."""
    site_id = await _site(stack)
    async with stack.sessionmaker() as session:
        session.add(CCOperationalAgent(
            id="agent-A", tenant_id=TENANT, name="Agent A",
            description="the poisoned agent", status="active",
            version=1, activated_version=1,
            autonomy_ceiling=1, require_approval_always=True,
            execution_budget=10, budget_period="daily",
            created_by=BUILDER, updated_by=BUILDER, activated_by=ACTIVATOR,
            activation_acknowledged_by=ACKNOWLEDGER,
            activation_acknowledged_version=1,
            activation_subject_ref="act-subject-1",
        ))
        session.add(CCScopeGrant(
            tenant_id=TENANT, principal_type="agent", principal_ref="agent-A",
            scope_type="site", scope_ref=site_id, granted_by="kc-owner",
        ))
        # A second agent, so "an agent inspects itself and no other" has
        # something to fail against.
        session.add(CCOperationalAgent(
            id="agent-B", tenant_id=TENANT, name="Agent B", status="active",
            version=1, activated_version=1, created_by=BUILDER,
        ))
        session.add(CCScopeGrant(
            tenant_id=TENANT, principal_type="agent", principal_ref="agent-B",
            scope_type="site", scope_ref=site_id, granted_by="kc-owner",
        ))
        session.add(CCAgentPreflight(
            agent_id="agent-A", tenant_id=TENANT, configuration_version=1,
            overall="ready", can_activate=True, requires_acknowledgement=True,
            requires_activation_approval=True,
            produced_by=PREFLIGHT_OPERATOR,
            result={
                "agent_id": "agent-A", "tenant_id": TENANT,
                "configuration_version": 1, "overall": "ready",
                "can_activate": True, "requires_acknowledgement": True,
                "requires_activation_approval": True,
                "unattended_classes": ["SEL_CLEAR"],
                "blocked_dimensions": [], "warn_dimensions": [],
                "unknown_dimensions": [],
                # The real shape `_row` produces: `dimension`, not `name`,
                # plus whatever extras a dimension carries. A fixture that
                # invented the key would have hidden the projection reading
                # the wrong one.
                "dimensions": [
                    {"dimension": "capabilities", "verdict": "ready",
                     "detail": "one class bound",
                     "permitted": 1, "warned": 0, "undeclared": 0},
                ],
                "by_dimension": {"capabilities": "ready"},
                "contract": {"authority": "grants nothing"},
            },
        ))
        session.add(CCAgentIdentity(
            tenant_id=TENANT, agent_id="agent-A", realm="tenant-demo",
            keycloak_client_id="op-agent-agent-A", keycloak_sub="sub-A",
            status="active", issued_by=ISSUER, rotated_by=ROTATOR,
            rotated_at=datetime.now(timezone.utc),
        ))
        session.add(CCAgentIdentity(
            tenant_id=TENANT, agent_id="agent-B", realm="tenant-demo",
            keycloak_client_id="op-agent-agent-B", keycloak_sub="sub-B",
            status="active", issued_by=ISSUER,
        ))
        # A dual-authorization policy, so the activation approval block
        # has a name and approvers to leak.
        session.add(CCApprovalPolicy(
            tenant_id=TENANT, name=POLICY_NAME, required_approvers=2,
            created_by="kc-owner",
        ))
        session.add(CCApprovalRecord(
            tenant_id=TENANT, subject_type="agent_activation",
            subject_ref="act-subject-1", approver_ref="kc-approver",
            approver_email=APPROVER, decision="approved", scope_ok=True,
            decided_at=datetime.now(timezone.utc),
        ))
        session.add(CCAgentProposal(
            tenant_id=TENANT, agent_id="agent-A", actor="op-agent:agent-A@v1",
            agent_version=1, site_id=site_id, device_agent_id="node-1",
            action_type="SEL_CLEAR", params={"secret_param": EXEC_PAYLOAD},
            rationale="because", evidence={"diagnosis": RAW_EVIDENCE},
            disposition="requires_approval", disposition_reason="needs a human",
            authorization_basis="human_approval", status="dispatched",
            decided_by=APPROVER, decided_at=datetime.now(timezone.utc),
            dedupe_key="k-poison", directive_id=DIRECTIVE,
            dispatch_reason=DISPATCH,
            dispatched_at=datetime.now(timezone.utc),
        ))
        await session.commit()
    return site_id


# ---------------------------------------------------------------------------
# The route/meter matrix. One table, consulted by three tests, and
# asserted COMPLETE against the router so a new machine-readable route
# cannot be added without deciding what it costs and what it says.
# ---------------------------------------------------------------------------

#: path template -> (expected machine status, carries lifecycle internals?)
#:
#: `dry-run` is the one route exempt from the internals check, and only
#: from that one: A22.2 requires it to return the REAL resolved params,
#: evidence and rationale for the agent's OWN candidates. It is not
#: exempt from the identity check, and nothing is.
MACHINE_ROUTES: dict[str, tuple[int, bool]] = {
    "/": (200, False),
    "/catalogue": (403, False),
    "/{agent_id}": (200, False),
    "/{agent_id}/preflight": (200, False),
    "/{agent_id}/runtime": (200, False),
    "/{agent_id}/identity": (200, False),
    "/{agent_id}/dry-run": (200, True),
    "/{agent_id}/proposals": (200, False),
    "/{agent_id}/proposals/{proposal_id}": (200, False),
    "/{agent_id}/submissions/{submission_id}": (200, False),
}


def _machine_get_routes(app) -> set:
    """Every GET the router exposes, as a template under the prefix.

    Read from the app's own OpenAPI table, the way the route-contract
    test reads it: the routers are wrapped by middleware, so walking
    `app.routes` naively finds nothing.
    """
    spec = app.openapi()
    return {
        path[len(PREFIX):] or "/"
        for path, ops in spec["paths"].items()
        if path.startswith(PREFIX) and "get" in ops
    }


class TestTheMatrixIsComplete:
    async def test_every_get_on_this_router_has_a_decided_machine_answer(self):
        stack = await _stack()
        declared = set(MACHINE_ROUTES)
        actual = _machine_get_routes(stack.app)
        assert actual == declared, (
            "a GET on the Operational Agent router is not in the machine "
            f"matrix: {sorted(actual ^ declared)}. Every machine-readable "
            "route must decide what it costs and what it may say."
        )


# ---------------------------------------------------------------------------
# HIGH 1: the projection follows the authorization
# ---------------------------------------------------------------------------


class TestNoMachineResponseCarriesAForbiddenField:
    async def _read_all(self, stack, site_id):
        """Every machine-readable route, once, as agent-A itself."""
        sub_id, prop_id = await _work(stack, site_id, key="k-work")
        out = {}
        async with stack.as_machine("agent-A").client() as c:
            for template, (expected, _) in MACHINE_ROUTES.items():
                url = PREFIX + template.format(
                    agent_id="agent-A", proposal_id=prop_id,
                    submission_id=sub_id,
                )
                res = await c.get(url)
                assert res.status_code == expected, (
                    f"{template} -> {res.status_code}: {res.text[:300]}"
                )
                out[template] = res.json()
        return out

    async def test_no_operator_identity_reaches_a_machine_anywhere(self):
        """Recursive, on every route, for keys AND values."""
        stack = await _stack()
        site_id = await _poisoned_estate(stack)
        for template, payload in (await self._read_all(stack, site_id)).items():
            keys, values = _inspect(payload)
            leaked_keys = keys & MACHINE_IDENTITY_FIELDS
            assert not leaked_keys, f"{template} leaked keys {leaked_keys}"
            for planted in PLANTED_IDENTITIES:
                assert not any(planted in v for v in values), (
                    f"{template} leaked {planted!r}"
                )

    async def test_no_execution_internal_reaches_a_lifecycle_read(self):
        stack = await _stack()
        site_id = await _poisoned_estate(stack)
        results = await self._read_all(stack, site_id)
        for template, payload in results.items():
            if MACHINE_ROUTES[template][1]:
                continue  # dry-run: A22.2 requires the real parameters
            keys, values = _inspect(payload)
            leaked_keys = keys & MACHINE_INTERNAL_FIELDS
            assert not leaked_keys, f"{template} leaked keys {leaked_keys}"
            for planted in PLANTED_INTERNALS:
                assert not any(planted in v for v in values), (
                    f"{template} leaked {planted!r}"
                )

    async def test_the_dry_run_exemption_is_narrow(self):
        """It may return parameters. It may not return people."""
        stack = await _stack()
        site_id = await _poisoned_estate(stack)
        results = await self._read_all(stack, site_id)
        keys, _ = _inspect(results["/{agent_id}/dry-run"])
        assert not (keys & MACHINE_IDENTITY_FIELDS)

    async def test_a_human_administrator_keeps_the_rich_projection(self):
        """Withholding from a machine must not withhold from an operator."""
        stack = await _stack()
        site_id = await _poisoned_estate(stack)
        sub_id, prop_id = await _work(stack, site_id, key="k-human")
        stack.as_person(role="tenant_owner")
        async with stack.client() as c:
            detail = (await c.get(f"{PREFIX}/agent-A")).json()
            listing = (await c.get(f"{PREFIX}/")).json()
            preflight = (await c.get(f"{PREFIX}/agent-A/preflight")).json()
            identity = (await c.get(f"{PREFIX}/agent-A/identity")).json()
            catalogue = await c.get(f"{PREFIX}/catalogue")
        assert catalogue.status_code == 200, "a human lost the builder surface"
        assert BUILDER in json.dumps(detail)
        assert ACTIVATOR in json.dumps(detail)
        assert EXEC_PAYLOAD in json.dumps(detail), (
            "the operator lost the executable parameters they decide on"
        )
        assert RAW_EVIDENCE in json.dumps(detail)
        assert BUILDER in json.dumps(listing)
        assert PREFLIGHT_OPERATOR in json.dumps(preflight)
        assert APPROVER in json.dumps(preflight), (
            "the approvals progress block lost its approvers for a human"
        )
        assert ISSUER in json.dumps(identity)
        assert sub_id and prop_id

    async def test_the_machine_detail_still_answers_the_question(self):
        """A withheld projection that says nothing is not a fix."""
        stack = await _stack()
        site_id = await _poisoned_estate(stack)
        await _work(stack, site_id, key="k-useful")
        async with stack.as_machine("agent-A").client() as c:
            body = (await c.get(f"{PREFIX}/agent-A")).json()
        assert body["view"] == "machine"
        assert body["agent"]["id"] == "agent-A"
        assert body["agent"]["status"] == "active"
        assert body["agent"]["activation_provenance"] == "recorded"
        assert body["agent"]["configuration_drifted"] is False
        assert body["scope"]["device_count"] >= 1
        assert body["proposals"], "the machine detail lost its own proposals"
        assert set(body["proposals"][0]) == {
            "proposal_id", "created_at", "proposal", "approval", "execution",
            "outcome", "terminal",
        }
        assert isinstance(body["posture"]["sites_not_reporting"], int), (
            "which sites are silent is estate detail; that some are is not"
        )

    async def test_the_machine_preflight_keeps_the_dimensions_it_may_pass(self):
        """Withholding must not silently blank the answer.

        The projection reads the human contract's own key. Reading a key
        that does not exist would have produced a list of nulls that every
        forbidden-field test still passes.
        """
        stack = await _stack()
        await _poisoned_estate(stack)
        async with stack.as_machine("agent-A").client() as c:
            body = (await c.get(f"{PREFIX}/agent-A/preflight")).json()
        assert body["exists"] is True
        assert body["overall"] == "ready"
        assert body["dimensions"] == [
            {"dimension": "capabilities", "verdict": "ready",
             "detail": "one class bound"},
        ], body["dimensions"]
        assert body["acknowledged"] is True, (
            "that it was acknowledged is a fact; by whom is not"
        )
        assert body["activation_approval"] == {
            "required": True, "state": "pending",
            "granted_count": 1, "required_count": 2,
        }, body["activation_approval"]

    async def test_the_machine_runtime_keeps_the_answers_it_may_pass(self):
        stack = await _stack()
        await _poisoned_estate(stack)
        async with stack.as_machine("agent-A").client() as c:
            body = (await c.get(f"{PREFIX}/agent-A/runtime")).json()
        assert body["activation_state"] == "active"
        assert body["configuration_drifted"] is False
        assert set(body["devices"]) == {
            "in_scope", "seen_recently", "stale", "never_reported",
        }
        assert body["budget"]["limit"] == 10
        assert "skills_by_id" not in body, (
            "the per-device skill inventory is an operator report"
        )

    async def test_the_machine_identity_reports_state_and_no_operator(self):
        stack = await _stack()
        await _poisoned_estate(stack)
        async with stack.as_machine("agent-A").client() as c:
            body = (await c.get(f"{PREFIX}/agent-A/identity")).json()
        assert body["exists"] is True
        assert body["status"] == "active"
        assert body["client_id"] == "op-agent-agent-A"
        assert body["rotated_at"], "its own credential's state is not withheld"
        assert "revoke_reason" not in body

    async def test_the_machine_detail_and_the_machine_list_cannot_disagree(self):
        """One set of blocks, so the two reads describe one proposal alike."""
        stack = await _stack()
        site_id = await _poisoned_estate(stack)
        _, prop_id = await _work(stack, site_id, key="k-same",
                                 status="dispatched", directive_id="dir-1")
        async with stack.as_machine("agent-A").client() as c:
            detail = (await c.get(f"{PREFIX}/agent-A")).json()
            listed = (await c.get(f"{PREFIX}/agent-A/proposals")).json()
        a = next(i for i in detail["proposals"] if i["proposal_id"] == prop_id)
        b = next(i for i in listed["proposals"] if i["proposal_id"] == prop_id)
        assert a == b

    async def test_the_machine_projections_are_built_positively(self):
        """Structural: none of them takes a payload and deletes from it.

        The instruction that produced this slice is explicit -- do not
        sanitize by removing fields. A `pop`/`del` here would pass every
        behavioural test above and silently leak the next field somebody
        adds upstream, which is exactly how the surface leaked.
        """
        from harkeniq_cc import receipts

        for name in (
            "machine_agent_view", "machine_agent_identity",
            "machine_agent_list_item", "machine_preflight_view",
            "machine_runtime_view", "machine_identity_view",
            "machine_proposal_items", "proposal_block", "approval_block",
        ):
            code = ast.unparse(_body_without_docstring(getattr(receipts, name)))
            assert ".pop(" not in code, f"{name} filters by removal"
            assert "del " not in code, f"{name} filters by removal"


class TestAnAgentInspectsItselfAndNoOther:
    async def test_the_listing_shows_a_machine_only_its_own_row(self):
        stack = await _stack()
        await _poisoned_estate(stack)
        async with stack.as_machine("agent-A").client() as c:
            body = (await c.get(f"{PREFIX}/")).json()
        assert body["view"] == "machine"
        assert [a["id"] for a in body["agents"]] == ["agent-A"], (
            "a machine enumerated another agent through the listing"
        )

    async def test_a_human_still_sees_every_agent_they_may_reach(self):
        stack = await _stack()
        await _poisoned_estate(stack)
        stack.as_person(role="tenant_owner")
        async with stack.client() as c:
            body = (await c.get(f"{PREFIX}/")).json()
        assert {a["id"] for a in body["agents"]} == {"agent-A", "agent-B"}

    @pytest.mark.parametrize("suffix", [
        "", "/preflight", "/runtime", "/identity", "/proposals", "/dry-run",
    ])
    async def test_a_machine_naming_another_agent_is_refused(self, suffix):
        stack = await _stack()
        await _poisoned_estate(stack)
        async with stack.as_machine("agent-A").client() as c:
            res = await c.get(f"{PREFIX}/agent-B{suffix}")
        assert res.status_code == 403, (
            f"agent-A read agent-B{suffix}: {res.status_code}"
        )

    async def test_the_identity_read_had_no_self_rule_at_all(self):
        """The regression this closes: it is not enough that it 403s now."""
        stack = await _stack()
        await _poisoned_estate(stack)
        async with stack.as_machine("agent-A").client() as c:
            res = await c.get(f"{PREFIX}/agent-B/identity")
        assert res.status_code == 403
        assert ISSUER not in res.text
        assert "op-agent-agent-B" not in res.text

    async def test_the_builder_catalogue_refuses_a_machine(self):
        stack = await _stack()
        await _poisoned_estate(stack)
        async with stack.as_machine("agent-A").client() as c:
            res = await c.get(f"{PREFIX}/catalogue")
        assert res.status_code == 403
        assert "operator surface" in res.text


# ---------------------------------------------------------------------------
# HIGH 2: approval completion is per proposal
# ---------------------------------------------------------------------------


async def _two_proposals(stack, site_id, *, first="p-one", second="p-two"):
    """Two proposals sharing EVERY policy coordinate.

    Same agent, same device, same action class, therefore the same
    governing policy -- which is the whole point: the cache key was those
    coordinates, and they do not determine an approval state.
    """
    ids = []
    async with stack.sessionmaker() as session:
        session.add(CCApprovalPolicy(
            tenant_id=TENANT, name=POLICY_NAME, action_type="*",
            device_type="*", risk_level="*", required_approvers=2,
            created_by="kc-owner",
        ))
        for tag in (first, second):
            row = CCAgentProposal(
                tenant_id=TENANT, agent_id="agent-A",
                actor="op-agent:agent-A@v1", agent_version=1,
                site_id=site_id, device_agent_id="node-1",
                action_type="SEL_CLEAR", params={"reason": "x"},
                rationale="because", evidence={},
                disposition="requires_approval",
                disposition_reason="needs a human",
                authorization_basis="human_approval",
                status="awaiting_approval", dedupe_key=tag,
            )
            session.add(row)
            await session.flush()
            ids.append(row.id)
        await session.commit()
    return ids


async def _approve(stack, proposal_id, *, who, decision="approved"):
    async with stack.sessionmaker() as session:
        session.add(CCApprovalRecord(
            tenant_id=TENANT, subject_type="agent_proposal",
            subject_ref=proposal_id, approver_ref=who,
            approver_email=f"{who}@example.com", decision=decision,
            scope_ok=True, decided_at=datetime.now(timezone.utc),
        ))
        await session.commit()


class TestApprovalStateIsNeverSharedBetweenProposals:
    async def _states(self, stack):
        async with stack.as_machine("agent-A").client() as c:
            body = (await c.get(f"{PREFIX}/agent-A/proposals")).json()
        return {
            item["proposal_id"]: item["approval"]
            for item in body["proposals"]
        }

    async def test_p1_approved_and_p2_pending_stay_independent(self):
        stack = await _stack()
        site_id = await _site(stack)
        await _agent_row(stack, site_id=site_id)
        p1, p2 = await _two_proposals(stack, site_id)
        await _approve(stack, p1, who="alice")
        await _approve(stack, p1, who="bob")

        states = await self._states(stack)
        assert states[p1]["state"] == "approved", states[p1]
        assert states[p1]["granted_count"] == 2
        assert states[p1]["required_count"] == 2
        assert states[p2]["state"] == "pending", (
            "an untouched proposal inherited its sibling's approval"
        )
        assert states[p2]["granted_count"] == 0
        assert states[p2]["required_count"] == 2, (
            "the policy resolution was lost with the completion cache"
        )

    async def test_the_reverse_order_gives_the_same_two_answers(self):
        """The defect was decided by list order, so assert both orders.

        The second proposal created is the one the list reaches first
        here, so approving IT proves the contamination could not have
        run the other way either.
        """
        stack = await _stack()
        site_id = await _site(stack)
        await _agent_row(stack, site_id=site_id)
        p1, p2 = await _two_proposals(stack, site_id)
        await _approve(stack, p2, who="alice")
        await _approve(stack, p2, who="bob")

        states = await self._states(stack)
        assert states[p2]["state"] == "approved"
        assert states[p1]["state"] == "pending"
        assert states[p1]["granted_count"] == 0

    async def test_a_denial_does_not_travel_to_a_sibling(self):
        """A denial is terminal (D16) -- for its own subject only."""
        stack = await _stack()
        site_id = await _site(stack)
        await _agent_row(stack, site_id=site_id)
        p1, p2 = await _two_proposals(stack, site_id)
        await _approve(stack, p1, who="carol", decision="denied")

        states = await self._states(stack)
        assert states[p1]["state"] == "denied"
        assert states[p2]["state"] == "pending", (
            "a denial on one proposal denied an untouched sibling"
        )

    async def test_the_list_and_the_receipt_agree_per_proposal(self):
        """The receipt reads one proposal, so it was never contaminated.

        Which makes it the reference: if the list disagrees with it, the
        list is wrong.
        """
        stack = await _stack()
        site_id = await _site(stack)
        await _agent_row(stack, site_id=site_id)
        p1, p2 = await _two_proposals(stack, site_id)
        await _approve(stack, p1, who="alice")

        states = await self._states(stack)
        async with stack.as_machine("agent-A").client() as c:
            for proposal_id in (p1, p2):
                receipt = (await c.get(
                    f"{PREFIX}/agent-A/proposals/{proposal_id}")).json()
                assert receipt["approval"] == states[proposal_id], proposal_id

    async def test_only_the_policy_resolution_is_cached(self):
        """Structural: the ledger read may not sit behind a cache.

        `machine_proposal_items` must call `approval_completion` once per
        proposal. A future refactor that reintroduces a completion cache
        passes every behavioural test above only until two proposals
        share coordinates again.
        """
        from harkeniq_cc import receipts

        code = ast.unparse(
            _body_without_docstring(receipts.machine_proposal_items))
        assert "policy_cache" in code
        assert "approval_completion(" in code
        # The completion is computed inside the per-proposal loop, not
        # read out of a dict keyed on anything.
        assert "cache[key] = await approval_completion" not in code

    async def test_the_policy_resolution_is_actually_reused(self):
        """Cheap is not the point, but it was the reason for the cache."""
        from harkeniq_cc import receipts

        calls = []
        original = receipts.resolve_governing_policy

        async def counting(session, tenant_id, action_type, device, *, cache=None):
            calls.append((action_type, device))
            return await original(
                session, tenant_id, action_type, device, cache=cache,
            )

        stack = await _stack()
        site_id = await _site(stack)
        await _agent_row(stack, site_id=site_id)
        await _two_proposals(stack, site_id)
        receipts.resolve_governing_policy = counting
        try:
            async with stack.as_machine("agent-A").client() as c:
                res = await c.get(f"{PREFIX}/agent-A/proposals")
        finally:
            receipts.resolve_governing_policy = original
        assert res.status_code == 200
        assert len(calls) == 2, calls
        # Two proposals, two calls into the cached resolver, ONE actual
        # resolution -- the cache did its job without deciding approval.


# ---------------------------------------------------------------------------
# MEDIUM: accounting identity, and accounting before the target decision
# ---------------------------------------------------------------------------


async def _windows(stack) -> dict:
    async with stack.sessionmaker() as session:
        rows = (await session.execute(
            sa.select(CCAgentReadWindow))).scalars().all()
    out: dict = {}
    for row in rows:
        out[(row.tenant_id, row.agent_id)] = (
            out.get((row.tenant_id, row.agent_id), 0) + row.reads
        )
    return out


class TestReadAccountingIsChargedToTheAuthenticatedCaller:
    async def test_a_successful_read_is_charged(self):
        stack = await _stack()
        site_id = await _site(stack)
        await _agent_row(stack, site_id=site_id)
        async with stack.as_machine("agent-A").client() as c:
            assert (await c.get(f"{PREFIX}/agent-A")).status_code == 200
        assert await _windows(stack) == {(TENANT, "agent-A"): 1}

    async def test_a_post_meter_not_found_is_charged(self):
        """A 404-producing poll must cost what a 200 costs."""
        stack = await _stack()
        site_id = await _site(stack)
        await _agent_row(stack, site_id=site_id)
        async with stack.as_machine("agent-A").client() as c:
            res = await c.get(f"{PREFIX}/agent-A/submissions/does-not-exist")
        assert res.status_code == 404
        assert await _windows(stack) == {(TENANT, "agent-A"): 1}

    async def test_a_cross_agent_refusal_is_charged_to_the_caller(self):
        """The defect: the self rule ran first, so this was free."""
        stack = await _stack()
        await _poisoned_estate(stack)
        async with stack.as_machine("agent-A").client() as c:
            res = await c.get(f"{PREFIX}/agent-B/runtime")
        assert res.status_code == 403
        charged = await _windows(stack)
        assert charged == {(TENANT, "agent-A"): 1}, charged

    async def test_the_target_agent_is_never_charged(self):
        """A caller-supplied id may not select whose allowance is spent."""
        stack = await _stack()
        await _poisoned_estate(stack)
        async with stack.as_machine("agent-A").client() as c:
            for suffix in ("", "/runtime", "/preflight", "/identity",
                           "/proposals", "/dry-run"):
                await c.get(f"{PREFIX}/agent-B{suffix}")
        charged = await _windows(stack)
        assert (TENANT, "agent-B") not in charged, (
            "six refusals were billed to the agent named in the PATH: a "
            "caller could exhaust another agent's polling allowance"
        )
        assert charged == {(TENANT, "agent-A"): 6}

    async def test_a_receipt_id_from_another_tenant_is_charged_to_the_caller(self):
        stack = await _stack()
        site_id = await _site(stack)
        await _agent_row(stack, site_id=site_id)
        from harkeniq_cc.db.models import CCAgentSubmission

        async with stack.sessionmaker() as session:
            foreign = CCAgentSubmission(
                tenant_id="other-tenant", agent_id="agent-A", agent_version=1,
                idempotency_key="k-foreign", request_digest="d",
                candidate_ref="c", proposal_id=None, code="", reason="",
            )
            session.add(foreign)
            await session.commit()
            foreign_id = foreign.id
        async with stack.as_machine("agent-A").client() as c:
            res = await c.get(f"{PREFIX}/agent-A/submissions/{foreign_id}")
        assert res.status_code == 404
        charged = await _windows(stack)
        assert charged == {(TENANT, "agent-A"): 1}
        assert ("other-tenant", "agent-A") not in charged, (
            "a foreign tenant id from the request selected the bucket"
        )

    async def test_a_human_never_creates_an_accounting_row(self):
        stack = await _stack()
        await _poisoned_estate(stack)
        stack.as_person(role="tenant_owner")
        # Dry-run is absent deliberately: for a HUMAN it resolves the real
        # E1.2 scope object, which this module's fake scope is not. The
        # human/no-charge property is the same on the eight routes below,
        # and a human dry-run is exercised in the A5 module against a real
        # scope.
        async with stack.client() as c:
            for suffix in ("", "/runtime", "/preflight", "/identity",
                           "/proposals"):
                await c.get(f"{PREFIX}/agent-A{suffix}")
            await c.get(f"{PREFIX}/")
            await c.get(f"{PREFIX}/catalogue")
        assert await _windows(stack) == {}, (
            "an operator's reads were billed to a machine bucket"
        )

    async def test_a_person_cannot_reach_the_receipt_surface_at_all(self):
        """No accounting row, and no receipt: 403 before either."""
        stack = await _stack()
        site_id = await _site(stack)
        sub_id, _ = await _work(stack, site_id, key="k-person")
        stack.as_person(role="tenant_owner")
        async with stack.client() as c:
            res = await c.get(f"{PREFIX}/agent-A/submissions/{sub_id}")
        assert res.status_code == 403
        assert await _windows(stack) == {}

    async def test_every_machine_route_charges_exactly_one_read(self):
        """The matrix, executed. An unmetered route is a free channel."""
        stack = await _stack()
        site_id = await _poisoned_estate(stack)
        sub_id, prop_id = await _work(stack, site_id, key="k-meter")
        for template, (expected, _) in MACHINE_ROUTES.items():
            fresh = await _stack()
            fresh_site = await _poisoned_estate(fresh)
            fresh_sub, fresh_prop = await _work(
                fresh, fresh_site, key="k-meter")
            url = PREFIX + template.format(
                agent_id="agent-A", proposal_id=fresh_prop,
                submission_id=fresh_sub,
            )
            async with fresh.as_machine("agent-A").client() as c:
                res = await c.get(url)
            assert res.status_code == expected, template
            charged = await _windows(fresh)
            assert charged == {(TENANT, "agent-A"): 1}, (
                f"{template} charged {charged}"
            )
        assert sub_id and prop_id

    async def test_the_bucket_cannot_be_selected_by_a_caller(self):
        """Structural: the accounting identity has ONE source.

        Behavioural tests can only probe the identifiers a caller happens
        to control today. This asserts the property directly: the charge
        reads `user.tenant_id` and `user.user_id` and nothing else --
        never the route's `agent_id`, a body field, a query value or a
        proposal id.
        """
        from harkeniq_cc.api import operational_agents as mod

        fn = _body_without_docstring(mod._charge_machine_read)
        params = {a.arg for a in fn.args.args}
        assert params == {"request", "user"}, params
        attrs = {
            node.attr for node in ast.walk(fn)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name) and node.value.id == "user"
        }
        assert attrs == {"tenant_id", "user_id"}, attrs
        # `request` exists for ONE purpose: reaching the canonical
        # sessionmaker. It is never read for a path or query value.
        request_uses = {
            ast.unparse(node) for node in ast.walk(fn)
            if isinstance(node, ast.Attribute)
            and "request" in ast.unparse(node).split(".")[0]
        }
        assert request_uses <= {
            "request.app", "request.app.state", "request.app.state.cc",
            "request.app.state.cc.sessionmaker",
        }, request_uses

    async def test_accounting_runs_before_the_target_decision(self):
        """Structural: the order the invariant names, in one sequence.

        `_machine_self_read` is the ONE place a machine read is admitted
        outside the receipt gate, and both must charge before deciding
        anything about the target.
        """
        from harkeniq_cc.api import operational_agents as mod

        for fn in (mod._machine_self_read, mod._machine_read_gate):
            code = ast.unparse(_body_without_docstring(fn))
            charge = code.index("_charge_machine_read")
            self_rule = code.index("_enforce_machine_self")
            assert charge < self_rule, (
                f"{fn.__name__} decides about the target before charging"
            )


# ---------------------------------------------------------------------------
# LOW: the accounting transaction is the accounting's own
# ---------------------------------------------------------------------------


class TestAccountingOwnsItsOwnTransaction:
    def test_it_holds_no_reference_to_the_caller_session(self):
        """The strongest engine-independent form of the invariant.

        A behavioural proof needs two real connections, which in-memory
        sqlite cannot give (StaticPool: one connection, shared). So the
        property is asserted where it is decidable -- the function cannot
        commit or roll back the caller's work because it never receives
        it. The durability proof is the PostgreSQL test.
        """
        from harkeniq_cc.api import operational_agents as mod

        fn = _body_without_docstring(mod._charge_machine_read)
        assert {a.arg for a in fn.args.args} == {"request", "user"}
        code = ast.unparse(fn)
        assert "session.commit()" not in code
        assert "session.rollback()" not in code
        assert "state.cc.sessionmaker" in code, (
            "accounting must come from the canonical session infrastructure"
        )
        assert "accounting.commit()" in code

    def test_no_route_hands_the_caller_session_to_the_meter(self):
        """A single call site passing `session` would undo the whole fix."""
        from harkeniq_cc.api import operational_agents as mod

        src = inspect.getsource(mod)
        for line in src.splitlines():
            if "_charge_machine_read(" in line and "async def" not in line:
                assert "session" not in line, line.strip()

    async def test_the_counter_still_does_not_roll_back_its_caller(self):
        """The repo-level contract A6-2 already had, unchanged."""
        from harkeniq_cc.db.repos import AgentReadWindowRepo
        from harkeniq_cc.ingress_limits import read_window_start

        stack = await _stack()
        window = read_window_start()
        async with stack.sessionmaker() as session:
            session.add(CCAgentReadWindow(
                tenant_id=TENANT, agent_id="agent-A",
                window_start=window, reads=1))
            await session.commit()
        async with stack.sessionmaker() as session:
            used = await AgentReadWindowRepo(session).increment(
                tenant_id=TENANT, agent_id="agent-A", window_start=window)
            await session.commit()
        assert used == 2

    async def test_a_read_still_writes_no_governance_state(self):
        """Accounting is the ONLY thing a machine read may write."""
        from harkeniq_cc.db.base import Base

        stack = await _stack()
        site_id = await _poisoned_estate(stack)
        sub_id, prop_id = await _work(stack, site_id, key="k-nogov")

        async def snapshot():
            async with stack.sessionmaker() as session:
                return {
                    table.name: (await session.execute(
                        sa.select(sa.func.count()).select_from(table)
                    )).scalar_one()
                    for table in Base.metadata.sorted_tables
                }

        before = await snapshot()
        async with stack.as_machine("agent-A").client() as c:
            for template in MACHINE_ROUTES:
                await c.get(PREFIX + template.format(
                    agent_id="agent-A", proposal_id=prop_id,
                    submission_id=sub_id,
                ))
        after = await snapshot()
        changed = {k: (before[k], after[k]) for k in after if before[k] != after[k]}
        assert set(changed) <= {"cc_agent_read_windows", "cc_capability_catalogue"}, (
            f"a machine read wrote governance state: {changed}"
        )
