"""S3-E1 (A30.37): the bounded global reason, for every reader (brief step 21).

The S3 estate (four sites, sentinel values at the hidden ones), a TEST-ONLY
member holding the gate, and an agent at site A proposing through the real
evaluator. Then every projection each reader may see:

    tenant-wide human, org/site human, device and device-class human, a
    principal with no scope, the machine's own view, the approval queue.

What passes is ONE thing: the constant global row (code, constant text,
scope `global`) and the bounded state -- for every reader, the tenant owner
included. Never the member's id, never a hidden site, device, count or
reason, never raw evidence.
"""

from __future__ import annotations

import json

import pytest

from harkeniq_cc import agent_runtime
from harkeniq_cc import global_safety as G

from tests.unit.cc import s3_estate as E
from tests.unit.cc.s3_estate import ALL, SITES
from tests.unit.cc.test_a30_26_autonomy_scope_isolation import _agent, _read

MEMBER_ID = "test_reader_matrix_member"


class Holding:
    member_id = MEMBER_ID

    def evaluate(self, context):
        return G.MemberVerdict.CONSTRAIN


@pytest.fixture(autouse=True)
def _gate(monkeypatch):
    monkeypatch.setattr(G, "_ACTIVE", (Holding(),))


async def _held_estate():
    stack = await E.build(ALL)
    agent_id = await _agent(stack, ("A",), name="S3E1 Reader", ingress=True)
    await E.seed_incident(stack, "A")
    stats = await agent_runtime.run_once(stack.state, stack.tenant)
    assert stats["proposed"] == 1, stats
    return stack, agent_id


def _global_rows(blocking) -> list:
    return [r for r in blocking or [] if isinstance(r, dict) and r.get("scope") == "global"]


def _assert_bounded(payload, holds, where):
    text = json.dumps(payload, default=str)
    assert MEMBER_ID not in text, where
    assert "SECRET-MEMBER" not in text, where
    assert E.leaks(payload, holds) == [], where


READERS = {
    # persona: (path-reading role, the sites it holds for the leak walk)
    "tenant": None,
    "org_ab": ("A", "B"),
    "site_a": ("A",),
    "device_a": (),
    "class_server": (),
    "no_scope": (),
}


class TestTheContract:
    @pytest.mark.parametrize("name", sorted(READERS))
    async def test_every_reader_reads_the_bounded_gate(self, name):
        stack, _agent_id = await _held_estate()
        if name == "tenant":
            contract = (await stack.as_person().get("/api/autonomy/")).json()
            holds = tuple(ALL)
        else:
            subject, _ = await E.persona(stack, name)
            contract = await _read(stack, subject)
            holds = READERS[name]
        for row in contract["action_classes"]:
            assert row["global_safety"] == {
                "state": "constrained", "reason_codes": [G.GLOBAL_SAFETY_CONSTRAINT],
            }, (name, row["action_type"])
            assert row["final_execution_eligibility"] == {
                "disposition": "denied", "reason_codes": [G.GLOBAL_SAFETY_CONSTRAINT],
            }, (name, row["action_type"])
            assert _global_rows(row["blocking_conditions"]) == [G.global_row()], name
        _assert_bounded(contract, holds, name)


class TestAStoredProposal:
    async def test_the_owner_reads_the_constant_row_and_nothing_else(self):
        stack, agent_id = await _held_estate()
        listed = (await stack.as_person().get(
            f"/api/operational-agents/{agent_id}/proposals")).json()
        (proposal,) = listed["proposals"]
        assert _global_rows(proposal["blocking_conditions"]) == [G.global_row()]
        assert MEMBER_ID not in json.dumps(listed)

    @pytest.mark.parametrize("name,holds", [("site_a", ("A",)), ("org_ab", ("A", "B"))])
    async def test_a_scoped_human_reads_the_constant_row(self, name, holds):
        stack, agent_id = await _held_estate()
        subject, _ = await E.persona(stack, name)
        for path in (f"/api/operational-agents/{agent_id}/proposals",
                     f"/api/operational-agents/{agent_id}"):
            payload = await _read(stack, subject, path)
            proposals = payload["proposals"]
            assert proposals, (name, path)
            for proposal in proposals:
                inner = proposal.get("proposal", proposal)
                assert _global_rows(inner["blocking_conditions"]) == [G.global_row()], path
            _assert_bounded(payload, holds, (name, path))

    async def test_the_approval_view(self):
        stack, _agent_id = await _held_estate()
        for subject, holds in ((None, tuple(ALL)), ("site_a", ("A",))):
            if subject is None:
                queue = (await stack.as_person().get("/api/approvals/")).json()
            else:
                sub, _ = await E.persona(stack, subject)
                queue = await _read(stack, sub, "/api/approvals/")
            items = [i for i in queue["actions"] if i.get("origin") == "agent"]
            assert items, subject
            for item in items:
                assert _global_rows(item["proposal"]["blocking_conditions"]) == \
                    [G.global_row()], subject
            _assert_bounded(queue, holds, ("approvals", subject))

    async def test_a_tampered_stored_row_is_rebuilt_for_every_reader(self):
        """Whatever a stored row says, only the constant leaves."""
        from harkeniq_cc.db.models import CCAgentProposal

        stack, agent_id = await _held_estate()
        async with stack.sessionmaker() as session:
            from sqlalchemy import select

            row = (await session.execute(select(CCAgentProposal).where(
                CCAgentProposal.agent_id == agent_id))).scalar_one()
            row.blocking_conditions = [
                r if r.get("scope") != "global" else {
                    **r, "detail": f"SECRET-C at {SITES['C'].id}",
                    "site_id": SITES["C"].id, "member_id": MEMBER_ID,
                }
                for r in row.blocking_conditions
            ]
            await session.commit()
        subject, _ = await E.persona(stack, "site_a")
        for payload in (
            (await stack.as_person().get(
                f"/api/operational-agents/{agent_id}/proposals")).json(),
            await _read(stack, subject, f"/api/operational-agents/{agent_id}/proposals"),
        ):
            (proposal,) = payload["proposals"]
            assert _global_rows(proposal["blocking_conditions"]) == [G.global_row()]
            assert MEMBER_ID not in json.dumps(payload)
            assert "SECRET-C" not in json.dumps(payload)


class TestTheMachine:
    async def test_its_own_proposals_and_discovery_are_bounded(self):
        stack, agent_id = await _held_estate()
        async with stack.as_machine(agent_id).client() as client:
            own = (await client.get(
                f"/api/operational-agents/{agent_id}/proposals")).json()
            discovery = (await client.get(
                f"/api/operational-agents/{agent_id}/discovery")).json()
        assert own["view"] == "machine"
        (item,) = own["proposals"]
        assert _global_rows(item["proposal"]["blocking_conditions"]) == [G.global_row()]
        _assert_bounded(own, ("A",), "machine proposals")
        sel = next(c for c in discovery["action_classes"] if c["action_type"] == "SEL_CLEAR")
        assert sel["governance"]["global_safety"] == {
            "state": "constrained", "reason_codes": [G.GLOBAL_SAFETY_CONSTRAINT],
        }
        assert G.GLOBAL_SAFETY_CONSTRAINT in sel["currently_operable"]["blocked_by"]
        assert sel["currently_operable"]["state"] == "not_operable"
        _assert_bounded(discovery, ("A",), "machine discovery")

    async def test_the_empty_registry_blocks_nothing_in_discovery(self, monkeypatch):
        monkeypatch.setattr(G, "_ACTIVE", ())
        stack = await E.build(ALL)
        agent_id = await _agent(stack, ("A",), name="S3E1 Clear", ingress=True)
        async with stack.as_machine(agent_id).client() as client:
            discovery = (await client.get(
                f"/api/operational-agents/{agent_id}/discovery")).json()
        for row in discovery["action_classes"]:
            if row["governance"] is not None:
                assert row["governance"]["global_safety"]["state"] == "clear"
            assert G.GLOBAL_SAFETY_CONSTRAINT not in row["currently_operable"]["blocked_by"]
