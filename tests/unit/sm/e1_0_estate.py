"""S3-E1-0 (A30.36): a REAL Site Manager serving several sites, driven only
through its own RPCs and controls.

The Site Manager is built by production code (`runtime.make_state`, both
servicers, `build_server`) and served on a real port. Everything that
changes its state goes through the same doors production traffic uses: a
node's `ReportAction` and `ReportVerdict`, Central Command's `PushPolicy`,
and the persisted halts. Central Command reads it through its own
`SMClient` (the QA-042 lesson: a field decoded nowhere is a feed that
carries nothing) and ingests it through its own `SafetyStateRepo`.

This module deliberately uses ONLY interfaces that exist unchanged on
`main` before S3-E1-0, so the same stages can be recorded there (the golden
Central Command differential) and replayed here.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional

import grpc

from harkeniq.proto import harkeniq_pb2, harkeniq_pb2_grpc

TENANT = "t-e10"
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)

#: node -> the site it is enrolled at.
DEVICES = {"a1": "alpha", "a2": "alpha", "b1": "beta", "b2": "beta"}

#: The windows Central Command pushes (a tenant's `max_per_window`).
POLICIES = [
    {"action_type": "SEL_CLEAR", "max_per_window": 3,
     "window_seconds": 3600, "risk_level": "low"},
    {"action_type": "BMC_RESET", "max_per_window": 5,
     "window_seconds": 3600, "risk_level": "low"},
]

#: The subsystem a report names, per class (the condition catalogue's).
SUBSYSTEM = {"SEL_CLEAR": "log", "BMC_RESET": "bmc"}


@dataclass
class Stack:
    tag: str
    sites: tuple
    state: Any
    config: Any
    server: Any
    endpoint: str
    channel: Any
    agent: Any
    sm: Any
    site_ids: dict            # short name -> the Site Manager's site id
    counter: int = 0
    closed: bool = field(default=False)

    # -- names, all suffixed so a shared PostgreSQL database stays clean ----
    def site_name(self, site: str) -> str:
        return f"{site}{self.tag}"

    def cc_site(self, site: str) -> str:
        return f"cc-{site}{self.tag}"

    def agent_id(self, device: str) -> str:
        return f"e10-{device}{self.tag}"

    def device_row(self, device: str) -> str:
        return f"dev{device}{self.tag}"

    def domain_id(self, site: str) -> str:
        return f"dom-{site}{self.tag}"

    def devices(self, site: Optional[str] = None) -> list[str]:
        return [
            d for d, s in DEVICES.items()
            if s in self.sites and (site is None or s == site)
        ]


async def build(sites=("alpha", "beta"), *, dsn: Optional[str] = None,
                tag: str = "") -> Stack:
    """A Site Manager serving `sites`, the first being its configured site."""
    from harkeniq_sm.config import SMConfig
    from harkeniq_sm.db.models import Device, FaultDomain, Site
    from harkeniq_sm.db.repos import DomainRepo
    from harkeniq_sm.grpc_server import (
        AgentServiceServicer,
        SiteManagerServiceServicer,
        build_server,
    )
    from harkeniq_sm.runtime import make_state

    config = SMConfig(
        insecure=True, site_token="", site_name=f"{sites[0]}{tag}",
        grpc_host="127.0.0.1", grpc_port=0,
        dsn=dsn or "sqlite+aiosqlite:///:memory:",
    )
    state = await make_state(config)

    probe = Stack(tag, tuple(sites), state, config, None, "", None, None, None, {})
    async with state.sessionmaker() as session:
        for site in sites:
            row = Site(name=probe.site_name(site), cc_site_id=probe.cc_site(site))
            session.add(row)
            await session.flush()
            probe.site_ids[site] = row.id
        for device in probe.devices():
            session.add(Device(
                id=probe.device_row(device),
                site_id=probe.site_ids[DEVICES[device]],
                agent_id=probe.agent_id(device), agent_name=device,
                vendor="Dell", model="R750",
            ))
        await session.flush()
        # One power fault domain per site, holding that site's devices: two
        # psu onsets inside it within 30 s are a REAL suppression (A2.6).
        for site in sites:
            session.add(FaultDomain(
                id=probe.domain_id(site), site_id=probe.site_ids[site],
                name=f"pdu-{site}{tag}", kind="power",
            ))
            await session.flush()
            await DomainRepo(session).set_members(
                probe.domain_id(site),
                [probe.device_row(d) for d in probe.devices(site)],
                added_by="e10",
            )
        await session.commit()

    servicer = AgentServiceServicer(
        state.ingest, approvals=state.approvals,
        identity_service=getattr(state, "identity", None),
        directives=getattr(state, "directives", None),
        autonomy=getattr(state, "autonomy", None),
        suppression=getattr(state, "suppression", None),
    )
    sm_servicer = SiteManagerServiceServicer(
        state.sessionmaker, state.approvals, config,
        directives=getattr(state, "directives", None),
        autonomy=getattr(state, "autonomy", None),
        ingest=state.ingest,
        suppression=getattr(state, "suppression", None),
    )
    server, port = build_server(config, servicer, sm_servicer=sm_servicer)
    await server.start()
    endpoint = f"127.0.0.1:{port}"
    channel = grpc.aio.insecure_channel(endpoint)
    probe.server = server
    probe.endpoint = endpoint
    probe.channel = channel
    probe.agent = harkeniq_pb2_grpc.AgentServiceStub(channel)
    probe.sm = harkeniq_pb2_grpc.SiteManagerServiceStub(channel)
    probe.servicer = servicer
    probe.sm_servicer = sm_servicer
    return probe


async def close(stack: Stack) -> None:
    if stack.closed:
        return
    stack.closed = True
    await stack.channel.close()
    await stack.server.stop(grace=None)
    await stack.state.engine.dispose()
    tmp = getattr(stack.state, "tmp_db_path", None)
    if tmp:
        import os

        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# The doors production traffic uses
# ---------------------------------------------------------------------------


async def push_policy(stack: Stack, policies=POLICIES) -> None:
    """Central Command's `PushPolicy` -- the tenant's windows."""
    ack = await stack.sm.PushPolicy(harkeniq_pb2.PolicyUpdate(
        tenant_id=TENANT, site_id=stack.cc_site(stack.sites[0]),
        autonomy_budgets_json=json.dumps({"policies": policies}),
    ))
    assert ack.accepted, ack.reason


async def report(stack: Stack, device: str, action_type: str, *,
                 status: str = "COMPLETED", success: bool = True,
                 n: int = 1) -> list[str]:
    """A node's `ReportAction`, `n` times. Returns what each call got."""
    got = []
    for _ in range(n):
        stack.counter += 1
        request = harkeniq_pb2.ActionReport(
            agent_id=stack.agent_id(device),
            action_id=f"e10-act-{stack.counter}{stack.tag}",
            type=action_type,
            sensor_id=f"{SUBSYSTEM.get(action_type, 'log')}:e10",
            skill_name="e10", verdict_severity="WARNING", params_json="{}",
            status=status, proposed_at="2026-09-27T12:00:00Z",
            outcome_json=json.dumps({"success": success}),
        )
        try:
            ack = await stack.agent.ReportAction(request)
            got.append("accepted" if ack.accepted else "refused")
        except grpc.aio.AioRpcError as exc:
            got.append(f"error:{exc.code().name}")
    return got


async def verdict(stack: Stack, device: str, *, severity: str = "CRITICAL",
                  sensor: str = "psu:PS1") -> bool:
    """A node's `ReportVerdict` -- an onset feeds correlation and suppression."""
    ack = await stack.agent.ReportVerdict(harkeniq_pb2.VerdictReport(
        agent_id=stack.agent_id(device), sensor_id=sensor, skill_name="e10",
        verdict=severity, evidence_json="{}", timestamp_unix=int(time.time()),
    ))
    return bool(ack.accepted)


async def suppress(stack: Stack, site: str) -> None:
    """A REAL suppression: psu onsets on every device of the site's power domain."""
    for device in stack.devices(site):
        await verdict(stack, device)


async def halt(stack: Stack, site: Optional[str], *, active: bool = True,
               scope: str = "site") -> None:
    """A persisted halt, written by the Site Manager's own halt service."""
    from harkeniq_sm.stopswitch import StopSwitchService

    async with stack.state.sessionmaker() as session:
        await StopSwitchService(stack.state.sessionmaker).set_halt(
            session, scope=scope,
            site_id=stack.site_ids[site] if site else None,
            active=active, actor="e10", reason=f"e10 {scope} halt",
        )
        await session.commit()


async def snapshot(stack: Stack, site: str) -> dict:
    """What Central Command's own client decodes for one site's safety."""
    from harkeniq_cc.sm_client import SMClient

    snap = await SMClient().get_fleet_snapshot(
        stack.endpoint, "", TENANT, stack.cc_site(site),
    )
    return snap["safety"]


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

#: Values that are the clock, not the state.
_CLOCK_KEYS = frozenset({
    "as_of_unix", "triggered_at_unix", "all_clear_at_unix",
    "dropped_back_at_unix", "generated_at", "safety_as_of", "changed_at",
})


def scrub(value: Any, tag: str = "") -> Any:
    """The clock removed and the per-run tag stripped, for byte comparison."""
    def walk(v):
        if isinstance(v, dict):
            return {
                k: walk(x) for k, x in sorted(v.items()) if k not in _CLOCK_KEYS
            }
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v

    text = json.dumps(walk(value), sort_keys=True, default=str)
    if tag:
        text = text.replace(tag, "")
    return json.loads(text)


# ---------------------------------------------------------------------------
# Central Command's side of it (code unchanged by S3-E1-0)
# ---------------------------------------------------------------------------


def _catalogue() -> dict:
    from harkeniq_cc.capability_catalogue import SEED

    out: dict[str, list[dict]] = {}
    for entry in SEED:
        out.setdefault(entry["subsystem"], []).append({
            "action_type": entry["action_type"],
            "because": entry["because"],
            "provenance": entry["provenance"],
        })
    return out


async def central_command(safety_by_site: dict[str, dict]) -> dict:
    """Central Command's own ingest, contract and evaluator over these inputs.

    `safety_by_site` maps a short site name to the decoded safety dict, in
    exactly the shape the fleet poller hands `SafetyStateRepo.upsert`.
    """
    from harkeniq_cc.autonomy import build_autonomy
    from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
    from harkeniq_cc.db.repos import SafetyStateRepo
    from harkeniq_cc.operational_agent import evaluate

    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    db = make_sessionmaker(engine)
    try:
        async with db() as session:
            for site, safety in sorted(safety_by_site.items()):
                await SafetyStateRepo(session).upsert(TENANT, f"cc-{site}", safety)
            await session.commit()
            rows = list(await SafetyStateRepo(session).list_for_tenant(TENANT))
    finally:
        await engine.dispose()

    sites = sorted(safety_by_site)
    # S3-E1 (A30.37): Central Command reads what its production code reads --
    # the composer's inputs once, a composite contract over every site for a
    # reader, and each target's SITE-LOCAL assessment for a decision. The
    # gate is the production registry: empty.
    from harkeniq_cc.governance import AutonomyInputs, SiteAssessments, gate_verdicts
    from harkeniq_cc.global_safety import GlobalSafetyGate, PRODUCTION_MEMBERS

    inputs = AutonomyInputs(
        tenant_id=TENANT,
        budgets=(SimpleNamespace(
            device_type="*", level=2, budget_limit=3, budget_period="hourly",
            actions_used=0,
        ),),
        stop_switch=SimpleNamespace(active=False, changed_by="", updated_at=NOW),
        outcomes=(), safety_rows=tuple(rows),
        sites=tuple(SimpleNamespace(id=f"cc-{s}", site_name=s) for s in sites),
        learned=(), policies=(), now=NOW,
    )
    contract = build_autonomy(
        tenant_id=TENANT, actor_id="op-agent:e10@v1", actor_species="agent",
        permissions=["fleet.view"],
        budgets=list(inputs.budgets), stop_switch=inputs.stop_switch,
        outcomes=[], safety_rows=rows,
        sites=list(inputs.sites),
        now=NOW,
        global_safety=gate_verdicts(
            GlobalSafetyGate(members=PRODUCTION_MEMBERS, tenant_id=TENANT, estate=()),
        ),
    )
    assessments = SiteAssessments(
        inputs, actor_id="op-agent:e10@v1", actor_species="agent",
        permissions=["fleet.view"],
    )
    agent = SimpleNamespace(
        id="e10", tenant_id=TENANT, name="e10", description="", status="active",
        version=1, autonomy_ceiling=3, require_approval_always=False,
        max_proposals_per_day=100, created_by="", created_at=NOW,
        updated_at=NOW, activated_by="", activated_at=NOW,
        last_evaluated_at=None, paused_reason="", execution_budget=0,
    )
    devices = [
        SimpleNamespace(
            agent_id=f"cc-dev-{s}", agent_name=f"cc-dev-{s}", site_id=f"cc-{s}",
            device_class="server", health="OK", observation="observed",
            vendor="Dell", model="R750", capabilities=None,
        )
        for s in sites
    ]
    proposals = evaluate(
        catalogue=_catalogue(), agent=agent,
        scopes=[SimpleNamespace(scope_type="site", scope_ref=f"cc-{s}") for s in sites],
        resolved_site_ids={f"cc-{s}" for s in sites},
        capabilities=[
            SimpleNamespace(kind="action_class", capability_ref="SEL_CLEAR"),
            SimpleNamespace(kind="action_class", capability_ref="BMC_RESET"),
            SimpleNamespace(kind="read", capability_ref="attention"),
        ],
        devices=devices,
        incidents_by_device={
            d.agent_id: [
                {"incident_id": f"{d.agent_id}-log", "subsystem": "log",
                 "title": "SEL full"},
                {"incident_id": f"{d.agent_id}-bmc", "subsystem": "bmc",
                 "title": "BMC unresponsive"},
            ]
            for d in devices
        },
        assessments=assessments,
        # D11: the fixture agent's activation approved both classes.
        unattended_approved=frozenset({"SEL_CLEAR", "BMC_RESET"}),
        now=NOW,
    )
    classes = {
        row["action_type"]: {
            "disposition": row["disposition"],
            "disposition_reason": row["disposition_reason"],
            "blocking_conditions": row["blocking_conditions"],
            "safety": row["safety"],
        }
        for row in contract["action_classes"]
        if row["action_type"] in ("SEL_CLEAR", "BMC_RESET")
    }
    return scrub({
        "classes": classes,
        "posture_stop": contract["posture"]["stop_switch"],
        "safety_state": contract["safety_state"],
        "proposals": sorted(
            (
                {
                    "device": p["device_agent_id"],
                    "action_type": p["action_type"],
                    "status": p["status"],
                    "authorization_basis": p["authorization_basis"],
                    "disposition": p["disposition"],
                    "blocking": sorted(
                        [b.get("code"), b.get("site_id") or "", b.get("domain_id") or ""]
                        for b in p.get("blocking_conditions") or []
                    ),
                }
                for p in proposals
            ),
            key=lambda p: (p["device"], p["action_type"]),
        ),
    })


# ---------------------------------------------------------------------------
# The recorded stages (the same on `main` and on this branch)
# ---------------------------------------------------------------------------

STAGES = {
    "multi": [
        ("baseline", []),
        ("alpha_executes_2", [("report", "a1", "SEL_CLEAR", "COMPLETED", True, 2)]),
        ("beta_executes_3", [("report", "b1", "SEL_CLEAR", "COMPLETED", True, 3)]),
        ("beta_domain_suppressed", [("suppress", "beta")]),
        ("beta_halted", [("halt", "beta")]),
        ("alpha_bmc_failures", [("report", "a2", "BMC_RESET", "FAILED", False, 5)]),
        ("beta_sel_failures", [("report", "b2", "SEL_CLEAR", "FAILED", False, 5)]),
    ],
    "single": [
        ("baseline", []),
        ("alpha_executes_2", [("report", "a1", "SEL_CLEAR", "COMPLETED", True, 2)]),
        ("alpha_domain_suppressed", [("suppress", "alpha")]),
        ("alpha_bmc_failures", [("report", "a2", "BMC_RESET", "FAILED", False, 5)]),
        ("alpha_halted", [("halt", "alpha")]),
    ],
}

TOPOLOGY = {"multi": ("alpha", "beta"), "single": ("alpha",)}


async def run_stages(topology: str, *, dsn: Optional[str] = None,
                     tag: str = "") -> list[dict]:
    """Drive one topology through its stages; record SM truth and CC's reading."""
    stack = await build(TOPOLOGY[topology], dsn=dsn, tag=tag)
    try:
        await push_policy(stack)
        recorded = []
        for name, operations in STAGES[topology]:
            outcomes = []
            for op in operations:
                if op[0] == "report":
                    _, device, action_type, status, success, n = op
                    outcomes.append(await report(
                        stack, device, action_type, status=status,
                        success=success, n=n,
                    ))
                elif op[0] == "suppress":
                    await suppress(stack, op[1])
                elif op[0] == "halt":
                    await halt(stack, op[1])
            safety = {s: await snapshot(stack, s) for s in stack.sites}
            recorded.append({
                "stage": name,
                "outcomes": outcomes,
                "sm": scrub(safety, tag),
                "sm_raw": json.loads(json.dumps(safety).replace(tag, "") if tag else json.dumps(safety)),
                "cc": await central_command(
                    json.loads(json.dumps(safety).replace(tag, "")) if tag else safety
                ),
            })
        return recorded
    finally:
        await close(stack)
