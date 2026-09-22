"""The S4 twin estates (spec A30.28): REAL learning over a hidden site.

S3's estate seeds learned signals by hand and every one of them carries
``evidence = {}`` -- which is exactly why A23-1's sweep and A30.26's
sentinels never saw E3-F1. Nothing here is hand-written: outcome rows go
into `cc_outcome_history`, and the production `IntelligenceEngine` turns
them into fleet patterns, learned signals and learning cycles, so the
payloads under test are the payloads production writes.

TWO ESTATES, ONE BAND. `X` and `Y` differ ONLY at site C, which the scoped
readers under test do not hold, and the difference is chosen so that every
number a scoped reader is shown lands in the SAME band in both:

===========  ==========================  ==========================
             X                           Y
===========  ==========================  ==========================
SEL_CLEAR    35/54 = 0.648 -> 0.65       37/58 = 0.638 -> 0.65
             conf 1.0                    conf 1.0
BMC_RESET    8/14 = 0.571 -> 0.55        9/16 = 0.5625 -> 0.55
             conf 0.70 -> 0.75           conf 0.80 -> 0.75
POWER_CYCLE  9/15, conf 0.75 (site A only; IDENTICAL in both)
===========  ==========================  ==========================

So a scoped reader must be returned the SAME BYTES by both estates, and a
tenant-wide reader must not (the non-vacuity guard). BMC_RESET and
POWER_CYCLE also share a confidence band while their exact confidences
SWAP order between the estates (0.70 < 0.75 < 0.80): that is the row-order
side channel.

The band arithmetic is restated in the table above and asserted by
`test_the_twins_really_share_every_band`, not imported from the code under
test, so the fixture cannot share a mistake with the projection.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Iterable

from sqlalchemy import select, update

from harkeniq_cc.db.models import (
    CCFleetPattern,
    CCIncident,
    CCLearnedSignal,
    CCLearningCycle,
    CCOutcomeHistory,
)
from harkeniq_cc.intelligence import IntelligenceEngine

from . import s3_estate as E

#: Extra outcome rows, on top of S3's: site key -> (action, SUCCESS, FAILURE).
#: Site A is CONSTANT. Only site C differs between the twins.
COMMON = {
    "A": [("SEL_CLEAR", 0, 3), ("POWER_CYCLE", 6, 9)],
}
VARIANTS: dict[str, dict[str, list[tuple[str, int, int]]]] = {
    "X": {"C": [("SEL_CLEAR", 3, 17), ("BMC_RESET", 0, 8)]},
    "Y": {"C": [("SEL_CLEAR", 5, 19), ("BMC_RESET", 1, 9)]},
}
#: Cycle reach the distribution loop would have recorded. It COUNTS the
#: estate, so it differs between the twins and must never reach a scoped
#: reader.
CYCLE_REACH = {"X": (3, 31), "Y": (4, 37)}

#: What the tenant totals are, for the non-vacuity assertions.
EXPECTED = {
    "X": {"SEL_CLEAR": (35, 54), "BMC_RESET": (8, 14), "POWER_CYCLE": (9, 15)},
    "Y": {"SEL_CLEAR": (37, 58), "BMC_RESET": (9, 16), "POWER_CYCLE": (9, 15)},
}

INCIDENT_TELEMETRY = "3 of 5 fans report 95% duty"


async def build(variant: str, sites: Iterable[str] = E.ALL, **kwargs) -> E.Stack:
    """S3's estate plus real learning, for one twin."""
    stack = await E.build(sites, **kwargs)
    keys = [k for k in E.ALL if k in set(sites)]
    extra = {**COMMON, **VARIANTS[variant]}

    async with stack.sessionmaker() as session:
        n = 0
        for key in keys:
            for action, ok, bad in extra.get(key, []):
                for result in ["SUCCESS"] * ok + ["FAILURE"] * bad:
                    n += 1
                    session.add(CCOutcomeHistory(
                        site_id=stack.site(key),
                        action_id=stack.tagged(f"s4-act-{key}-{n}"),
                        action_type=action, device_agent_id=stack.device(key),
                        vendor="Dell", model="R750", outcome=result,
                        fault_resolved=result == "SUCCESS",
                        ingested_at=E.AS_OF + timedelta(days=1, seconds=n),
                        recorded_at=E.AS_OF,
                    ))
        await session.commit()

    async with stack.sessionmaker() as session:
        await IntelligenceEngine().run_cycle(session, stack.tenant)
        await session.commit()
    await _pin_the_clock(stack)

    sites_reached, devices_reached = CYCLE_REACH[variant]
    async with stack.sessionmaker() as session:
        await session.execute(
            update(CCLearningCycle)
            .where(CCLearningCycle.tenant_id == stack.tenant)
            .values(sites_distributed=sites_reached, devices_applied=devices_reached)
        )
        await session.commit()
    return stack


async def _pin_the_clock(stack: E.Stack) -> None:
    """Keep the engine's sub-second ORDER; remove the wall clock.

    The engine stamps patterns, cycles and signals in detection order --
    cohorts ranked by tenant attempt total -- a few microseconds apart.
    That order is the side channel under test, so it is preserved exactly;
    the instants are moved inside ONE fixed minute so that a build which
    happens to straddle a real minute boundary cannot change the result.
    """
    base = E.AS_OF + timedelta(days=2)
    async with stack.sessionmaker() as session:
        for model, fields in (
            (CCFleetPattern, ("detected_at",)),
            (CCLearningCycle, ("started_at", "updated_at")),
            (CCLearnedSignal, ("first_observed_at", "last_confirmed_at")),
        ):
            rows = list((await session.execute(
                select(model).where(model.tenant_id == stack.tenant)
            )).scalars())
            for name in fields:
                stamped = [r for r in rows if getattr(r, name, None) is not None]
                stamped.sort(key=lambda r: getattr(r, name))
                for rank, row in enumerate(stamped):
                    setattr(row, name, base + timedelta(microseconds=rank + 1))
        await session.commit()


async def aliases(stack: E.Stack) -> dict[str, str]:
    """pattern id -> a name derived from its CONTENT.

    Production pattern ids are random (`pat-<uuid>`), so an id carries
    nothing about the estate -- and differs between any two builds. A byte
    comparison of two estates names patterns by what they conclude.
    """
    names = {
        p.id: f"<{p.pattern_type}:{(p.affected_scope or {}).get('action_type', '')}>"
        for p in await patterns(stack)
    }
    # A learned signal's row id is a random uuid too; its key is its content.
    names.update({s.id: f"<signal:{s.signal_key}>" for s in await signals(stack)})
    return names


async def patterns(stack: E.Stack) -> list[CCFleetPattern]:
    async with stack.sessionmaker() as session:
        return list((await session.execute(
            select(CCFleetPattern)
            .where(CCFleetPattern.tenant_id == stack.tenant)
            # DETECTION order: what the repository, the distributor and a
            # citing Site Manager all follow.
            .order_by(CCFleetPattern.detected_at)
        )).scalars())


async def signals(stack: E.Stack) -> list[CCLearnedSignal]:
    async with stack.sessionmaker() as session:
        return list((await session.execute(
            select(CCLearnedSignal)
            .where(CCLearnedSignal.tenant_id == stack.tenant)
            .order_by(CCLearnedSignal.signal_key)
        )).scalars())


async def seed_cited_incident(stack: E.Stack, key: str = "A") -> str:
    """An incident whose diagnosis CITES the fleet patterns, as a Site
    Manager that received them BEFORE this slice would have stored them:
    `str({"fleet_pattern": {...}})` of the raw tenant payload
    (`ingest._matching_fleet_patterns`, `reasoning.py`), beside one piece
    of the device's own telemetry, which must come back untouched."""
    cited = [
        str({"fleet_pattern": {
            "pattern_id": p.id, "pattern_type": p.pattern_type,
            "description": p.description, "confidence": p.confidence,
        }})
        for p in await patterns(stack)
    ]
    incident_id = stack.tagged(f"s4-inc-{key.lower()}")
    async with stack.sessionmaker() as session:
        session.add(CCIncident(
            incident_id=incident_id, tenant_id=stack.tenant,
            site_id=stack.site(key), kind="device", status="open",
            title="Fan duty rising", device_agent_id=stack.device(key),
            subsystem="fan", confidence=0.9,
            explanation={
                "provider": "llm", "root_cause": "airflow",
                "confidence": 0.8, "reasoning": "fans saturating",
                "evidence_cited": [INCIDENT_TELEMETRY] + cited,
                "similar_past_incidents": [],
            },
        ))
        await session.commit()
    return incident_id


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

#: Keys whose values are wall-clock instants. A30.28 names timestamps as a
#: separate semantic and a follow-up; they also differ between ANY two
#: builds, so they cannot be part of a byte comparison of two estates.
_CLOCK_KEYS = frozenset({
    "detected_at", "resolved_at", "first_observed_at", "last_confirmed_at",
    "started_at", "completed_at", "created_at", "updated_at", "decided_at",
    "generated_at", "as_of", "evaluated_at", "expires_at", "opened_at",
    "last_seen_at",
})


def comparable(payload):
    """A response with wall-clock instants removed; ORDER IS KEPT."""
    if isinstance(payload, dict):
        return {k: comparable(v) for k, v in payload.items() if k not in _CLOCK_KEYS}
    if isinstance(payload, list):
        return [comparable(v) for v in payload]
    return payload


def canonical(payload, names: dict[str, str]) -> str:
    """Bytes for an equality check. Key order is normalised (JSON objects
    are unordered); LIST order is not, because order is a channel. Random
    pattern ids are replaced by content-derived names (`aliases`)."""
    text = json.dumps(comparable(payload), sort_keys=True)
    for pattern_id, name in names.items():
        text = text.replace(pattern_id, name)
    return text


def walk(payload, path=""):
    """Every (path, key-or-leaf) in a JSON tree, keys included."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield f"{path}.{key}", key
            yield from walk(value, f"{path}.{key}")
    elif isinstance(payload, list):
        for i, value in enumerate(payload):
            yield from walk(value, f"{path}[{i}]")
    else:
        yield path, payload


def hidden_markers(payload, holds: Iterable[str]) -> list[str]:
    """Paths at which a site the reader does not hold is NAMED."""
    hidden = [m for key in E.ALL if key not in set(holds) for m in E.MARKERS[key]]
    found = []
    for path, leaf in walk(payload):
        if isinstance(leaf, str) and any(marker in leaf for marker in hidden):
            found.append(f"{path} = {leaf!r}")
    return found
