"""R2a frozen exit gate (spec §7, plan R2a).

Two MockSimulators + two agents against one in-process Site Manager
(TLS + bearer token, ephemeral ports). An injected shared fault —
simultaneous PSU failures — must consolidate into exactly ONE
``shared_power`` parent incident with two children, with the approval
brokered through the SM: dashboard API approve → agent polls, executes,
reports COMPLETED.

Case 1 seeds the power domain as operator-confirmed via the YAML
round-trip (A1.1); case 2 leaves it to inference and asserts the parent
is labeled ``inferred`` at the configured 0.6 confidence.
"""

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from harkeniq.agent import Agent
from harkeniq.mock.simulator import MockSimulator
from harkeniq.models import ActionStatus
from harkeniq_sm.config import SMConfig
from harkeniq_sm.runtime import make_state, run

REPO = Path(__file__).parents[2]

BEAT = 0.2  # compressed agent cadence (sensor poll, SM heartbeat, action poll)
TOKEN = "r2a-exit-gate-token"
AGENT_IDS = ("exit-a", "exit-b")

# Compressed baseline learning: confidence >= 0.5 after a few polls so
# rule-matched actions can propose (Doc 13 §2.3).
FAST_LEARNING = {
    "baseline": {"min_samples": 8, "window_samples": 40, "critical_pause_samples": 3},
    "trending": {"min_samples": 8, "slope_threshold": 0.05,
                 "r_squared_min": 0.5, "max_projection_days": 90},
}

SITE_YAML = """\
site: e2e-site
devices:
  - agent_id: exit-a
  - agent_id: exit-b
fault_domains:
  - name: pdu-row-1
    kind: power
    members: [exit-a, exit-b]
"""


async def wait_until(predicate, timeout=30.0, message="condition not met in time"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(message)


def agent_config(sim, agent_id, agent_name, grpc_port, dev_tls, tmp_path):
    return {
        "agent": {"id": agent_id, "name": agent_name},
        "bmc": {"host": sim.url, "username": "admin", "password": "password",
                "verify_ssl": False},
        "skills": {"directory": str(REPO / "skills")},
        "polling": {"sensor_interval": BEAT},
        "checkpoint": {"path": str(tmp_path / f"{agent_id}.db"), "interval": 999999},
        "site_manager": {
            "host": "127.0.0.1",
            "port": grpc_port,
            "token": TOKEN,
            "tls": True,
            "tls_ca": dev_tls["ca"],
            "heartbeat_interval": BEAT,
            "action_poll_interval": BEAT,
        },
        **FAST_LEARNING,
    }


@pytest.fixture
async def stack(dev_tls, tmp_path):
    """SM (TLS + token) + two sims (Dell/HPE) + two agents, all in-process."""
    config = SMConfig(
        # File-backed sqlite: the default :memory: DSN shares ONE aiosqlite
        # connection (StaticPool) across all sessions, so the concurrent
        # ingest/sweeper/API transactions interleave — a session teardown
        # ROLLBACK can erase another session's in-flight parent incident.
        dsn=f"sqlite+aiosqlite:///{tmp_path}/sm.db",
        site_name="e2e-site",
        http_host="127.0.0.1", http_port=0,
        grpc_host="127.0.0.1", grpc_port=0,
        site_token=TOKEN,
        tls_cert=dev_tls["cert"], tls_key=dev_tls["key"],
        inference_interval_s=0.5,
    )
    config.correlation.sweeper_interval_s = 0.2

    state = await make_state(config)
    sm_task = asyncio.create_task(run(config, state=state))
    await asyncio.wait_for(state.started.wait(), timeout=10)

    sims = [MockSimulator(device="dell-r750", port=0, no_auth=True),
            MockSimulator(device="hpe-dl360-gen10", port=0, no_auth=True)]
    for sim in sims:
        await sim.start()

    agents = [
        Agent(agent_config(sims[0], "exit-a", "rack-1-srv-a",
                           state.grpc_port, dev_tls, tmp_path)),
        Agent(agent_config(sims[1], "exit-b", "rack-1-srv-b",
                           state.grpc_port, dev_tls, tmp_path)),
    ]
    agent_tasks = []

    def start_agents():
        agent_tasks.extend(
            asyncio.create_task(agent.run(install_signal_handlers=False))
            for agent in agents
        )

    client = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{state.http_port}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    yield SimpleNamespace(
        state=state, sims=sims, agents=agents, client=client,
        start_agents=start_agents,
    )

    await client.aclose()
    for agent in agents:
        agent.request_shutdown()
    for task in agent_tasks:
        await asyncio.wait_for(task, timeout=15)
    sm_task.cancel()
    try:
        await sm_task
    except asyncio.CancelledError:
        pass
    for sim in sims:
        await sim.stop()


async def both_observed(client):
    entries = (await client.get("/api/coverage")).json()
    seen = {e["agent_id"]: e for e in entries}
    return set(AGENT_IDS) <= seen.keys() and all(
        seen[a]["observation"] == "observed" for a in AGENT_IDS
    )


async def inject_psu_faults(sims):
    """Simultaneous PSU failures on both devices — the shared fault."""
    for sim in sims:
        await sim.inject_fault(
            "psu", "PS1", {"health": "Critical", "input_voltage": 0}
        )


async def shared_power_parents(client):
    cards = (await client.get("/api/incidents")).json()
    return [c for c in cards if c["kind"] == "shared_power"]


def psu_baselines_learned(agent) -> bool:
    """Actions only propose at baseline confidence >= 0.5 (Doc 13 §2.3),
    and Critical health freezes learning — so learn before injecting."""
    trending = agent.skill_engine.trending
    psu_ids = [
        sid for sid in trending.get_all_baselines() if sid.startswith("psu:")
    ]
    return bool(psu_ids) and all(
        trending.confidence(sid) >= 0.5 for sid in psu_ids
    )


class TestR2aExitGate:
    async def test_shared_fault_confirmed_domain_one_parent_and_brokered_approval(
        self, stack
    ):
        client = stack.client

        # Operator-confirmed power domain via the YAML round-trip (A1.1).
        response = await client.put("/api/site/yaml", content=SITE_YAML)
        assert response.status_code == 200, response.text
        assert response.json()["imported"]["domains"] == 1
        exported = (await client.get("/api/site/yaml")).text
        assert "pdu-row-1" in exported

        stack.start_agents()
        await wait_until(lambda: both_observed(client),
                         message="agents never reported in")

        async def learned():
            return all(psu_baselines_learned(a) for a in stack.agents)
        await wait_until(learned, message="psu baselines never learned")

        await inject_psu_faults(stack.sims)

        # Exactly ONE open shared_power parent with two children.
        async def consolidated():
            parents = await shared_power_parents(client)
            return len(parents) == 1 and len(parents[0]["children"]) == 2
        await wait_until(consolidated,
                         message="no consolidated shared_power parent")

        parents = await shared_power_parents(client)
        assert len(parents) == 1
        parent = parents[0]
        assert parent["status"] == "open"
        assert parent["inferred"] is False          # confirmed domain
        assert parent["confidence"] == 1.0
        assert {c["agent_id"] for c in parent["children"]} == set(AGENT_IDS)
        assert all(c["subsystem"] == "psu" for c in parent["children"])
        assert parent["correlation_meta"]["domain"] == "pdu-row-1"

        # Approval brokered through the SM: agent proposes, operator
        # approves over the API, agent polls / executes / completes.
        found = {}

        async def action_proposed():
            actions = (await client.get("/api/actions")).json()
            row = next(
                (a for a in actions
                 if a["agent_id"] == "exit-a" and a["status"] == "pending"
                 and a["type"] == "COLLECT_DIAGNOSTICS"),
                None,
            )
            if row:
                found["action"] = row
            return row is not None
        await wait_until(action_proposed,
                         message="exit-a never proposed an action")
        action = found["action"]

        response = await client.post(
            f"/api/actions/{action['id']}/approve", json={"actor": "vinod"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["decided_by"] == "vinod"

        async def completed():
            actions = (await client.get("/api/actions")).json()
            row = next(a for a in actions if a["id"] == action["id"])
            return row["status"] == "completed"
        await wait_until(completed, message="approved action never completed")

        # Agent side executed it and the Redfish action landed on the BMC.
        agent_a = stack.agents[0]
        queued = agent_a.action_queue.get(action["agent_action_id"])
        assert queued.status == ActionStatus.COMPLETED
        assert queued.outcome.success is True
        assert stack.sims[0].action_state["diagnostics"], "no diagnostics collected"

        # Consolidation stayed stable: still exactly one parent.
        parents = await shared_power_parents(client)
        assert len(parents) == 1
        assert len(parents[0]["children"]) == 2

    async def test_shared_fault_inferred_domain_parent_labeled(self, stack):
        client = stack.client
        stack.start_agents()
        await wait_until(lambda: both_observed(client),
                         message="agents never reported in")

        await inject_psu_faults(stack.sims)

        # No operator domain: inference proposes one from psu co-onset,
        # and the parent is created against it, labeled inferred at 0.6.
        async def inferred_parent():
            parents = await shared_power_parents(client)
            return (
                len(parents) == 1
                and parents[0]["inferred"] is True
                and len(parents[0]["children"]) == 2
            )
        await wait_until(inferred_parent,
                         message="no inferred shared_power parent")

        parents = await shared_power_parents(client)
        assert len(parents) == 1
        parent = parents[0]
        assert parent["confidence"] == pytest.approx(0.6)
        assert {c["agent_id"] for c in parent["children"]} == set(AGENT_IDS)
