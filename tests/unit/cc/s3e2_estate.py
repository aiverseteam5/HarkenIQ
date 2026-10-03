"""S3-E2 (spec A30.39): ONE estate for historical proposal evidence.

It extends the S3 sentinel estate (`s3_estate`) -- four sites, two regions,
the production app, persisted grants, STRICT -- with what the viewer track
record has to tell apart:

    site A   node-s3-a   server   SEL_CLEAR 7/0   (S3)
             node-e2-a2  server   SEL_CLEAR 2/1
             sw-e2-a3    switch   SEL_CLEAR 1/0
    site B   node-s3-b   switch   SEL_CLEAR 9/2   (S3)
             blank-e2-b2 ""       SEL_CLEAR 1/1   (a blank class matches no class grant, R1)
             node-s3-a            SEL_CLEAR 3/0   MOVED: node-s3-a resolves at A only,
                                                  so these rows are SITE B's (B0b owner rule)
    site C   node-s3-c   server   SEL_CLEAR 0/13, BMC_RESET 6/0   (S3)
    site D   node-s3-d   server   nothing

Two independent oracles, restated here and never imported from the code
they check:

* `readable(persona, row)` -- the B0b owner rule over the persona's
  `fleet.view` grants, from the estate definition;
* `expected_track_record(rows)` -- `_evidence_for`'s arithmetic, restated.

And the stored-proposal sentinels: numbers that cannot occur anywhere else in
a payload (nine digits -- longer than any run in an ISO timestamp, so the
"7777" flake shape cannot recur), a rationale sentinel and an unknown
evidence key a scoped reader must never be handed.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Iterable, Optional

from sqlalchemy import delete, select

from harkeniq_cc.db.models import (
    CCAgentProposal,
    CCFleetCache,
    CCIncident,
    CCOperationalAgent,
    CCOutcomeHistory,
)

from tests.unit.cc import s3_estate as S3
from tests.unit.cc.s3_estate import ALL, AS_OF, SITES

ACTION = "SEL_CLEAR"

#: (device id, site key, class) added beside S3's one device per site.
E2_DEVICES = (
    ("node-e2-a2", "A", "server"),
    ("sw-e2-a3", "A", "switch"),
    ("blank-e2-b2", "B", ""),
)

#: The device each site's S3 row names, and its class.
S3_DEVICES = {k: (SITES[k].device, SITES[k].device_class) for k in ALL}

#: (site key, device id, action, SUCCESS count, FAILURE count) -- every
#: outcome row in the estate, S3's included, in seeding order.
OUTCOME_ROWS: tuple = (
    ("A", "node-s3-a", "SEL_CLEAR", 7, 0),
    ("B", "node-s3-b", "SEL_CLEAR", 9, 2),
    ("C", "node-s3-c", "SEL_CLEAR", 0, 13),
    ("C", "node-s3-c", "BMC_RESET", 6, 0),
    ("A", "node-e2-a2", "SEL_CLEAR", 2, 1),
    ("A", "sw-e2-a3", "SEL_CLEAR", 1, 0),
    ("B", "node-s3-a", "SEL_CLEAR", 3, 0),      # moved device: site-owned at B
    ("B", "blank-e2-b2", "SEL_CLEAR", 1, 1),
)

#: device id -> (site key, class), as the fleet cache says NOW.
FLEET: dict[str, tuple[str, str]] = {
    **{dev: (k, cls) for k, (dev, cls) in S3_DEVICES.items()},
    **{dev: (k, cls) for dev, k, cls in E2_DEVICES},
}

# ---------------------------------------------------------------------------
# Personas: S3's, plus the two S3-E2 needs
# ---------------------------------------------------------------------------

PERSONAS: dict[str, list[tuple]] = {
    **{name: grants for name, (grants, _holds) in S3.PERSONAS.items()},
    "class_switch": [("device_class", "switch", None)],
    # An approver at A whose grant carries no fleet.view (case 16).
    "approve_only_a": [("site", "A", ["action.approve"])],
    "device_a2": [("device", "node-e2-a2", None)],
}

#: Every persona whose fleet.view reach is not the whole tenant.
SCOPED = tuple(name for name in PERSONAS if name != "tenant")


async def persona(stack, name: str) -> str:
    """Persist this persona's grants; returns its subject."""
    subject = f"kc-e2-{name}"
    for scope_type, ref, subset in PERSONAS[name]:
        if ref.startswith("@"):
            ref = stack.regions[ref[1:]]
        elif scope_type == "site":
            ref = stack.site(ref)
        elif scope_type == "device":
            ref = stack.tagged(ref)
        await stack.grant(subject, scope_type, ref, subset=subset)
    return subject


def _fleet_view_grants(name: str) -> list[tuple]:
    return [
        (scope_type, ref) for scope_type, ref, subset in PERSONAS[name]
        if subset is None or "fleet.view" in subset
    ]


def has_fleet_view_reach(name: str) -> bool:
    return bool(_fleet_view_grants(name))


def tenant_wide(name: str) -> bool:
    return any(t == "tenant" for t, _ in _fleet_view_grants(name))


_REGION_SITES = {"@ab": ("A", "B"), "@cd": ("C", "D")}


def readable(name: str, site_key: str, device: str) -> bool:
    """The B0b owner rule (A30.25), restated from the estate definition.

    A row is readable when ONE fleet.view-carrying grant covers it: its site
    (directly or through an org unit), or -- when the row's device RESOLVES
    at the row's own site -- the device by id or by its current, non-blank
    class.
    """
    resolved = FLEET.get(device)
    resolves_here = resolved is not None and resolved[0] == site_key
    for scope_type, ref in _fleet_view_grants(name):
        if scope_type == "tenant":
            return True
        if scope_type == "org_unit" and site_key in _REGION_SITES[ref]:
            return True
        if scope_type == "site" and ref == site_key:
            return True
        if scope_type == "device" and resolves_here and ref == device:
            return True
        if (scope_type == "device_class" and resolves_here and resolved[1]
                and resolved[1].lower() == ref.lower()):
            return True
    return False


def expected_track_record(name: Optional[str], action: str = ACTION) -> Optional[dict]:
    """What `viewer_projected_evidence.outcome_evidence` must say for this
    persona -- `_evidence_for`'s arithmetic over the rows `readable` keeps,
    restated. ``None`` when the persona holds fleet.view nowhere."""
    if name is not None and not has_fleet_view_reach(name):
        return None
    total = success = failure = resolved = 0
    sites: set[str] = set()
    for site_key, device, act, ok, bad in OUTCOME_ROWS:
        if act != action or (name is not None and not readable(name, site_key, device)):
            continue
        total += ok + bad
        success += ok
        failure += bad
        resolved += ok
        if ok + bad:
            sites.add(site_key)
    sufficient = total >= 5
    return {
        "executions": total, "success": success, "failure": failure,
        "success_rate": round(success / total, 4) if sufficient else None,
        "resolution_rate": round(resolved / total, 4) if sufficient else None,
        "sites_observed": len(sites), "sufficient": sufficient,
        "window": "all_time",
    }


# ---------------------------------------------------------------------------
# The stack
# ---------------------------------------------------------------------------


async def build(*, keep=None, sites: Iterable[str] = ALL, **kw) -> S3.Stack:
    """The S3 stack plus the S3-E2 devices and outcome rows.

    `keep(site_key, device, action) -> bool` DELETES every outcome row it
    refuses, after seeding -- which is how the deletion-equivalence oracle's
    estate is made: the same estate with everything a persona cannot read
    removed, read by the tenant owner.
    """
    stack = await S3.build(sites, **kw)
    t = stack.tagged
    held = set(sites)
    async with stack.sessionmaker() as session:
        for dev, site_key, cls in E2_DEVICES:
            if site_key not in held:
                continue
            session.add(CCFleetCache(
                site_id=stack.site(site_key), agent_id=t(dev), agent_name=t(dev),
                vendor="Dell", model="R750", device_class=cls,
                observation="observed", health="Critical",
            ))
        n = 0
        for site_key, dev, act, ok, bad in OUTCOME_ROWS[4:]:
            if site_key not in held:
                continue
            for result in ["SUCCESS"] * ok + ["FAILURE"] * bad:
                n += 1
                session.add(CCOutcomeHistory(
                    site_id=stack.site(site_key), action_id=t(f"act-e2-{n}"),
                    action_type=act, device_agent_id=t(dev),
                    vendor="Dell", model="R750", outcome=result,
                    fault_resolved=result == "SUCCESS",
                    ingested_at=AS_OF + timedelta(seconds=5000 + n),
                    recorded_at=AS_OF,
                ))
        await session.commit()
    if keep is not None:
        await delete_outcomes(stack, keep)
    return stack


async def delete_outcomes(stack, keep) -> None:
    """Remove every outcome row `keep` refuses (the deletion estate)."""
    async with stack.sessionmaker() as session:
        by_site = {stack.site(k): k for k in ALL}
        # THIS stack's rows only: a PostgreSQL run shares its database.
        rows = (await session.execute(select(CCOutcomeHistory).where(
            CCOutcomeHistory.site_id.in_(sorted(by_site))
        ))).scalars().all()
        untag = (lambda ref: ref[: -len(stack.tag) - 1]) if stack.tag else (lambda r: r)
        doomed = [
            r.id for r in rows
            if not keep(by_site.get(r.site_id, ""), untag(r.device_agent_id),
                        r.action_type)
        ]
        if doomed:
            await session.execute(
                delete(CCOutcomeHistory).where(CCOutcomeHistory.id.in_(doomed))
            )
        await session.commit()


def keep_for(name: str):
    """The deletion estate's `keep` for one persona."""
    return lambda site_key, device, action: readable(name, site_key, device)


async def agent(stack, *, name: str, scopes: list[tuple[str, str]],
                actions=(ACTION,)) -> str:
    """An ACTIVE agent over these (scope_type, ref) rules, created through
    the production route by the tenant owner."""
    caps = [{"kind": "action_class", "capability_ref": a} for a in actions]
    rules = []
    for scope_type, ref in scopes:
        if scope_type == "site":
            ref = stack.site(ref)
        elif scope_type == "device":
            ref = stack.tagged(ref)
        rules.append({"scope_type": scope_type, "scope_ref": ref})
    async with stack.as_person().client() as client:
        res = await client.post("/api/operational-agents/", json={
            "name": name, "scopes": rules, "capabilities": caps,
        })
    assert res.status_code == 201, res.text
    agent_id = res.json()["id"]
    async with stack.sessionmaker() as session:
        row = await session.get(CCOperationalAgent, agent_id)
        row.status = "active"
        row.activated_version = row.version
        await session.commit()
    return agent_id


async def incident(stack, site_key: str, device: str, *, key: str,
                   title: str = "BMC event log saturated",
                   subsystem: str = "log", explanation=None) -> str:
    incident_id = stack.tagged(f"e2-inc-{key}")
    async with stack.sessionmaker() as session:
        session.add(CCIncident(
            incident_id=incident_id, tenant_id=stack.tenant,
            site_id=stack.site(site_key), kind="device", status="open",
            title=title, device_agent_id=stack.tagged(device),
            subsystem=subsystem, confidence=0.9, explanation=explanation,
        ))
        await session.commit()
    return incident_id


# ---------------------------------------------------------------------------
# Stored-proposal sentinels (cases 10, 11, 13, 15)
# ---------------------------------------------------------------------------

#: A creation-time count no reader but the tenant may ever be handed.
HIDDEN_EXECUTIONS = 918273645
HIDDEN_SUCCESS = 804561237
HIDDEN_FAILURE = HIDDEN_EXECUTIONS - HIDDEN_SUCCESS
HIDDEN_SITES = 731313131
HIDDEN_RATE = round(HIDDEN_SUCCESS / HIDDEN_EXECUTIONS, 4)
HIDDEN_RESOLUTION = 0.6180
#: The rendered percentage of HIDDEN_RATE, as the writer's clause prints it.
HIDDEN_PERCENT = f"{HIDDEN_RATE:.0%}"
HIDDEN_RATIONALE = "HIDDENRATIONALEe2"
HIDDEN_KEY = "HIDDENKEYe2"
HIDDEN_ATTENTION = 612345678

#: Every string a scoped reader may never find, anywhere.
NEVER_SCOPED = (
    str(HIDDEN_EXECUTIONS), str(HIDDEN_SUCCESS), str(HIDDEN_FAILURE),
    str(HIDDEN_SITES), HIDDEN_RATIONALE, HIDDEN_KEY, str(HIDDEN_ATTENTION),
)

HIDDEN_OUTCOME_EVIDENCE = {
    "executions": HIDDEN_EXECUTIONS, "success": HIDDEN_SUCCESS,
    "failure": HIDDEN_FAILURE, "success_rate": HIDDEN_RATE,
    "resolution_rate": HIDDEN_RESOLUTION, "sites_observed": HIDDEN_SITES,
    "sufficient": True, "window": "all_time",
}

HEAD = (
    "S3E2 Legacy observed BMC event log saturated on node-s3-a and "
    "recommends sel clear: the event log is full."
)
#: Exactly what the writer's clause says for HIDDEN_OUTCOME_EVIDENCE --
#: restated, so the test does not trust the function it is checking.
HIDDEN_CLAUSE = (
    f" This class has succeeded {HIDDEN_PERCENT} of the time across "
    f"{HIDDEN_EXECUTIONS} executions in this tenant."
)


def sentinel_evidence(**extra) -> dict:
    return {
        "observed": "BMC event log saturated",
        "condition_kind": "incident",
        "subsystem": "log",
        "incident_ids": ["e2-legacy-inc"],
        "has_diagnosis": False,
        "remediation_provenance": "seed",
        "component": "",
        "components_reported": None,
        "attention": {"rank": 1, "band": "act_now", "driver": "health",
                      "risk_score": HIDDEN_ATTENTION},
        "outcome_evidence": dict(HIDDEN_OUTCOME_EVIDENCE),
        "learned_signals": [],
        "device": {"vendor": "Dell", "model": "R750", "device_class": "server",
                   "health": "Critical", "observation": "observed"},
        "contract_version": "1",
        "evaluated_at": "2026-09-01T12:00:00+00:00",
        **extra,
    }


#: key -> (rationale, evidence). `clean` is a well-formed legacy row;
#: `malformed` does not end with the writer's clause and quotes a hidden
#: count and site inside its own words; `extra_key` carries an evidence key
#: outside the allow-list.
SENTINEL_ROWS = {
    "clean": (HEAD + HIDDEN_CLAUSE, sentinel_evidence()),
    "malformed": (
        f"S3E2 Legacy {HIDDEN_RATIONALE}: {HIDDEN_EXECUTIONS} executions "
        "across s3-site-c said so.",
        sentinel_evidence(),
    ),
    "extra_key": (HEAD + HIDDEN_CLAUSE,
                  sentinel_evidence(legacy_tenant_stat=HIDDEN_KEY)),
    "no_evidence": (HEAD + HIDDEN_CLAUSE, None),
}


async def seed_sentinel_proposals(stack, agent_id: str, *,
                                  device: str = "node-s3-a",
                                  site_key: str = "A") -> dict[str, str]:
    """Stored proposals carrying the sentinels, awaiting approval at A."""
    ids: dict[str, str] = {}
    async with stack.sessionmaker() as session:
        agent_row = await session.get(CCOperationalAgent, agent_id)
        for key, (rationale, evidence) in SENTINEL_ROWS.items():
            row = CCAgentProposal(
                tenant_id=stack.tenant, agent_id=agent_id,
                actor=f"op-agent:{agent_id}@v{agent_row.version}",
                agent_version=agent_row.version,
                site_id=stack.site(site_key), device_agent_id=stack.tagged(device),
                action_type=ACTION, params={"reason": "the event log is full"},
                rationale=rationale, evidence=evidence,
                disposition="requires_approval",
                disposition_reason="tenant autonomy level 0 is below level 2",
                blocking_conditions=[],
                authorization_basis="human_approval",
                status="awaiting_approval", dedupe_key=f"e2-sentinel-{key}",
            )
            session.add(row)
            await session.flush()
            ids[key] = row.id
        await session.commit()
    return ids


# ---------------------------------------------------------------------------
# Walking a payload
# ---------------------------------------------------------------------------


def leaves(node, path=()):
    if isinstance(node, dict):
        for key, value in node.items():
            yield path + (key,), key, True
            yield from leaves(value, path + (key,))
    elif isinstance(node, (list, tuple)):
        for i, item in enumerate(node):
            yield from leaves(item, path + (i,))
    else:
        yield path, node, False


def sentinel_hits(payload, sentinels: Iterable = NEVER_SCOPED) -> list:
    """Every leaf (key or value) carrying a sentinel. Numbers by equality,
    strings by substring."""
    wanted = [str(s) for s in sentinels]
    numbers = {int(s) for s in wanted if s.isdigit()}
    hits = []
    for path, leaf, _is_key in leaves(payload):
        if isinstance(leaf, bool) or leaf is None:
            continue
        if isinstance(leaf, (int, float)) and leaf in numbers:
            hits.append((path, leaf))
        elif isinstance(leaf, str) and any(s in leaf for s in wanted):
            hits.append((path, leaf))
        elif isinstance(leaf, float) and leaf in (HIDDEN_RATE,):
            hits.append((path, leaf))
    return hits


async def stored(stack, proposal_id: str):
    async with stack.sessionmaker() as session:
        return await session.get(CCAgentProposal, proposal_id)


async def stored_bytes(stack) -> str:
    """Every stored proposal, canonically serialised: the immutability oracle."""
    async with stack.sessionmaker() as session:
        rows = (await session.execute(
            select(CCAgentProposal)
            .where(CCAgentProposal.tenant_id == stack.tenant)
            .order_by(CCAgentProposal.id)
        )).scalars().all()
        return json.dumps([
            {
                "id": r.id, "rationale": r.rationale, "evidence": r.evidence,
                "disposition": r.disposition,
                "disposition_reason": r.disposition_reason,
                "blocking_conditions": r.blocking_conditions,
                "authorization_basis": r.authorization_basis,
                "status": r.status, "site_id": r.site_id,
                "device_agent_id": r.device_agent_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ], sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# The scenario every surface is read over
# ---------------------------------------------------------------------------

#: A diagnosis on the proposal's own incident. Nothing generated may reach a
#: proposal (case 12): the evaluator records only `has_diagnosis`.
GENERATED = "GENSENTINELe2"


async def scenario(**build_kw):
    """The full estate, one site-A agent, what the REAL evaluator proposed,
    one candidate still open for a dry-run, and the stored sentinels.

    Returns (stack, ids) where ids names the agent, the evaluated proposal
    and the sentinel rows.
    """
    from harkeniq_cc import agent_runtime

    stack = await build(**build_kw)
    agent_id = await agent(stack, name="S3E2 Runtime", scopes=[("site", "A")])
    await incident(stack, "A", "node-s3-a", key="a1",
                   explanation={"summary": f"{GENERATED} the log is full"})
    stats = await agent_runtime.run_once(stack.state, stack.tenant)
    assert stats["proposed"] == 1, stats
    # Opened AFTER the pass, so the dry-run has a candidate to preview.
    await incident(stack, "A", "node-e2-a2", key="a2")
    async with stack.sessionmaker() as session:
        (evaluated,) = (await session.execute(
            select(CCAgentProposal).where(CCAgentProposal.agent_id == agent_id)
        )).scalars().all()
    ids = {"agent": agent_id, "evaluated": evaluated.id}
    ids.update(await seed_sentinel_proposals(stack, agent_id))
    return stack, ids


def _normalise_evidence(evidence):
    if not isinstance(evidence, dict):
        return evidence
    out = json.loads(json.dumps(evidence))
    if "evaluated_at" in out:
        out["evaluated_at"] = "<clock>"
    attention = out.get("attention")
    if isinstance(attention, dict):
        # Recency-weighted against the wall clock (the outcome rows carry a
        # fixed date), so the score -- and in time the band and rank -- move
        # with the calendar. The golden keeps the SHAPE; byte identity with
        # the stored row is asserted within one request, where no clock moves.
        out["attention"] = {k: type(v).__name__ for k, v in attention.items()}
    return out


def _by_label(ids: dict) -> dict:
    return {v: k for k, v in ids.items() if k != "agent"}


async def tenant_wide_payloads(stack, ids: dict) -> dict:
    """`evidence` and `rationale` of every proposal a TENANT-WIDE human reads,
    on every human surface -- the D8 golden, recorded from `main`."""
    label = _by_label(ids)
    agent_id = ids["agent"]
    owner = stack.as_person()

    def pick(items, key="proposal_id"):
        return {
            label.get(p[key], p[key]): {
                "evidence": _normalise_evidence(p.get("evidence")),
                "rationale": p.get("rationale"),
            }
            for p in items
        }

    queue = (await owner.get("/api/approvals/")).json()
    detail = (await owner.get(f"/api/operational-agents/{agent_id}")).json()
    listed = (await owner.get(f"/api/operational-agents/{agent_id}/proposals")).json()
    dry = (await owner.get(f"/api/operational-agents/{agent_id}/dry-run")).json()
    return {
        "queue": pick([a["proposal"] for a in queue["actions"]
                       if a.get("origin") == "agent"]),
        "detail": pick(detail["proposals"]),
        "list": pick(listed["proposals"]),
        "dry_run": {
            f"{p['device_agent_id']}:{p['action_type']}": {
                "evidence": _normalise_evidence(p.get("evidence")),
                "rationale": p.get("rationale"),
            }
            for p in dry["would_propose"]
        },
    }


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

APPROVAL_PERMISSIONS = ("action.approve", "audit.view")


def approve_readable(name: str, site_key: str, device: str) -> bool:
    """The queue's owner rule for this persona (A30.25), restated: grants
    carrying `action.approve` or `audit.view` (a persona's grant role is
    `site_admin`, which carries `action.approve`)."""
    resolved = FLEET.get(device)
    resolves_here = resolved is not None and resolved[0] == site_key
    for scope_type, ref, subset in PERSONAS[name]:
        if subset is not None and not set(subset) & set(APPROVAL_PERMISSIONS):
            continue
        if scope_type == "tenant":
            return True
        if scope_type == "org_unit" and site_key in _REGION_SITES[ref]:
            return True
        if scope_type == "site" and ref == site_key:
            return True
        if scope_type == "device" and resolves_here and ref == device:
            return True
        if (scope_type == "device_class" and resolves_here and resolved[1]
                and resolved[1].lower() == ref.lower()):
            return True
    return False


async def evidence_view(stack, subject: str, role: str = "site_admin"):
    """The reader's ProposalEvidenceView, resolved exactly as `get_scope`
    resolves a person (same loader, realm and alias) and loaded by the ONE
    production loader."""
    from harkeniq_cc.auth import ROLE_PERMISSIONS
    from harkeniq_cc.governance import load_proposal_evidence_view, load_scope

    async with stack.sessionmaker() as session:
        scope = await load_scope(
            session, tenant_id=stack.tenant, principal_ref=subject,
            realm=getattr(stack.state.config, "keycloak_realm", "") or "",
            role_permissions=list(ROLE_PERMISSIONS[role]),
            aliases=[f"{subject}@example.com"],
        )
        return await load_proposal_evidence_view(
            session, tenant_id=stack.tenant, scope=scope,
        )


async def seed_device_proposals(stack, agent_id: str) -> dict[str, str]:
    """One well-formed sentinel proposal about EVERY device, at the device's
    current site -- so every persona with approval reach has one to read."""
    rationale, evidence = SENTINEL_ROWS["clean"]
    ids: dict[str, str] = {}
    async with stack.sessionmaker() as session:
        for device, (site_key, _cls) in sorted(FLEET.items()):
            row = CCAgentProposal(
                tenant_id=stack.tenant, agent_id=agent_id,
                actor=f"op-agent:{agent_id}@v1", agent_version=1,
                site_id=stack.site(site_key), device_agent_id=stack.tagged(device),
                action_type=ACTION, params={"reason": "r"},
                rationale=rationale, evidence=json.loads(json.dumps(evidence)),
                disposition="requires_approval", disposition_reason="",
                blocking_conditions=[], authorization_basis="human_approval",
                status="awaiting_approval", dedupe_key=f"e2-device-{device}",
            )
            session.add(row)
            await session.flush()
            ids[device] = row.id
        await session.commit()
    return ids
