"""A30.41: every read the outcome window slice touches, below both windows.

The golden (`golden/a30_41_below_window.json`) was recorded by THIS module
with Central Command's production code byte-identical to `main` 565154b --
before A30.41 changed a line of it -- and recorded twice, written only
because both recordings were identical. Below 10,000 and 50,000 rows the
windows cut nothing, so every answer here must be unchanged by A30.41:
the tallies are integer arithmetic over the same rows, and the decay sums
are the same formula over the same rows in the same order.

Each read is stored as a SHA-256 of its canonical JSON, with full payloads
for a few small sections so a failure shows data. To see a full diff,
re-record at main and compare.
"""

from __future__ import annotations

import hashlib
import json

from tests.unit.cc import a30_40_estate as P
from tests.unit.cc import b2_2_estate as E
from tests.unit.cc import s3e2_estate as X

GOLDEN_PATH = "golden/a30_41_below_window.json"
MAIN = "565154be7c9f1e7e151793f437b8342d5521df0d"

METRICS_QUERIES = ({}, {"action_type": "SEL_CLEAR"}, {"vendor": "Dell"})
MACHINE_QUERIES = ({}, {"limit": 2}, {"site_id": "A"}, {"band": "high"})
VIEWER_ACTIONS = ("SEL_CLEAR", "BMC_RESET", "IDENTIFY_LED")
LEARNING_CYCLES = 4

#: Sections kept in full beside their digests: small, and the ones a
#: failure is easiest to read from.
FULL_SECTIONS = ("viewer", "learning")


def _key(q: dict) -> str:
    return json.dumps(q, sort_keys=True)


def canon(payload):
    return json.loads(json.dumps(payload, sort_keys=True))


async def humans(variant: str) -> dict:
    """Every human persona's predictive, Attention, metrics and autonomy."""
    stack = await P.build(P.Keep.everything(variant))
    out: dict = {}
    for name in P.PERSONAS:
        reads: dict = {"predictive": {}, "attention": {}, "metrics": {}, "autonomy": {}}
        for q in P.PREDICTIVE_QUERIES:
            reads["predictive"][_key(q)] = await P.predictive(stack, name, **P.query(stack, q))
        for q in P.ATTENTION_QUERIES:
            reads["attention"][_key(q)] = await P.attention(stack, name, **P.query(stack, q))
        for q in METRICS_QUERIES:
            reads["metrics"][_key(q)] = await P.get(stack, name, "/api/outcomes/metrics", **q)
        reads["autonomy"]["{}"] = E.normalised(await P.get(stack, name, "/api/autonomy/"))
        out[name] = canon(reads)
    stack.as_person()
    return out


#: A machine item's freshness is the WALL CLOCK's answer, not the outcome
#: history's: the estate stamps its fleet rows once per PROCESS, at import
#: (`b2_2_estate.NOW_BASE`), so the same read differs across two processes
#: by its clock fields, and -- 13 minutes into a long run -- by its state,
#: which flips from fresh to stale under A30.35's 15-minute rule (a CI
#: full-suite run on 3.12 found that). A30.41 does not touch freshness, so
#: the whole block is normalised; its rule is A30.35's to prove.
_CLOCK_FIELDS = ("state", "last_seen_at", "snapshot_at")


def _without_clock(payload: dict) -> dict:
    for item in payload.get("items", []):
        fresh = item.get("freshness")
        if isinstance(fresh, dict):
            for field in _CLOCK_FIELDS:
                if field in fresh:
                    fresh[field] = "<clock>"
    return payload


async def machines() -> dict:
    """Every machine persona's Attention, and the internal decision paths."""
    stack = await E.build()
    out: dict = {}
    for name in E.MACHINES:
        out[name] = {
            _key(q): _without_clock(
                E.normalised(await E.read(stack, name, **E._query(stack, q)))
            )
            for q in MACHINE_QUERIES
        }
    return {"attention": canon(out), "internal": await E.internal_payloads(stack)}


async def viewer() -> dict:
    """S3-E2's viewer track record, for every persona and three classes."""
    stack = await X.build()
    out: dict = {}
    for name in X.PERSONAS:
        subject = await X.persona(stack, name)
        view = await X.evidence_view(stack, subject)
        out[name] = {}
        for action in VIEWER_ACTIONS:
            block = dict(view.viewer(action))
            block.pop("as_of", None)
            out[name][action] = block
    return canon(out)


async def learning() -> dict:
    """The learning engine's aggregate and detections over four cycles."""
    from harkeniq_cc.intelligence import IntelligenceEngine

    stack = await P.build(P.Keep.everything("heavy"))
    engine = IntelligenceEngine()
    cycles = []
    for _ in range(LEARNING_CYCLES):
        async with stack.sessionmaker() as session:
            patterns = await engine.run_cycle(session, stack.tenant)
            await session.commit()
        cycles.append(sorted(
            [{"type": p.pattern_type, "scope": p.affected_scope,
              "severity": getattr(p, "severity", None),
              "description": getattr(p, "description", None)} for p in patterns],
            key=lambda d: json.dumps(d, sort_keys=True),
        ))
    metrics = [
        {"action_type": m.action_type, "vendor": m.vendor, "model": m.model,
         "total": m.total_count, "success": m.success_count, "failure": m.failure_count,
         "partial": m.partial_count, "resolved": m.fault_resolved_count,
         "site_counts": dict(sorted(m.site_counts.items())),
         "site_failure_counts": dict(sorted(m.site_failure_counts.items()))}
        for m in engine.aggregator.get_metrics()
    ]
    # Site ids are generated per build: name them by the estate's site keys.
    key_of = {stack.site(k): k for k in ("A", "B", "C", "D")}

    def relabel(node):
        if isinstance(node, dict):
            return {key_of.get(k, k): relabel(v) for k, v in node.items()}
        if isinstance(node, list):
            return [relabel(v) for v in node]
        return key_of.get(node, node) if isinstance(node, str) else node

    return canon(relabel({"cycles": cycles, "metrics": metrics}))


async def below_window_reads() -> dict:
    return {
        "humans_heavy": await humans("heavy"),
        "humans_light": await humans("light"),
        "machines": await machines(),
        "viewer": await viewer(),
        "learning": await learning(),
    }


def _sha(answer) -> str:
    return hashlib.sha256(
        json.dumps(answer, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def digests(reads: dict) -> dict:
    """{path: sha256}, one per answer, so a changed answer names one path."""
    out: dict = {}
    for variant in ("humans_heavy", "humans_light"):
        for name, sections in reads[variant].items():
            for section, by_query in sections.items():
                for q, answer in by_query.items():
                    out[f"{variant}/{name}/{section}/{q}"] = _sha(answer)
    for name, by_query in reads["machines"]["attention"].items():
        for q, answer in by_query.items():
            out[f"machines/attention/{name}/{q}"] = _sha(answer)
    for name, answer in reads["machines"]["internal"].items():
        out[f"machines/internal/{name}"] = _sha(answer)
    for name, by_action in reads["viewer"].items():
        for action, block in by_action.items():
            out[f"viewer/{name}/{action}"] = _sha(block)
    out["learning"] = _sha(reads["learning"])
    return out


def golden_of(reads: dict, *, head: str) -> dict:
    return {
        "recorded_at_commit": head,
        "production_code_equals": MAIN,
        "digests": digests(reads),
        "full": {k: reads[k] for k in FULL_SECTIONS},
    }
