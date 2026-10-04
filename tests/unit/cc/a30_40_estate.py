"""A30.40: ONE poisoned PREDICTIVE estate. A harness, not a test.

Built on B2-2's poisoned Attention estate (`tests/unit/cc/b2_2_estate.py`),
which runs the S3 production stack -- the real `get_scope`, persisted
grants, STRICT enforcement. B2-2's tables are seeded unchanged; this file
adds the predictive poison B2-2 did not need, and its OWN human personas
and its OWN restatement of the owner rule. Nothing in B2-2's harness is
modified: its persona dict is read by its own tests at import time.

The base estate (B2-2's, by site): A holds a1 (R750, 7 outcomes), a2 (R750,
2 failures -- too few to score, so its CURRENT basis is a cohort prior), a3
(R750, none), a4 (switch, 5 failures), mv (R750, MOVED from C: one outcome
at A, six failures still recorded at C) and a site-owned "ghost" (3
failures at A); B holds b1 (switch) and b2 (R750, 1 failure); C holds c1
(R750, 20 failures) and c2 (switch, 10 failures); D holds d1 (R750, none).

The predictive poison, by default (HEAVY):

* three site-A servers whose model is seen ONLY at the hidden site C, one
  per edge rate: R760 (one failure at C -> 100%), R770 (one success at C ->
  0%) and R780 (one failure and two successes at C -> 1/3). A reader who
  does not hold C must read each as `insufficient_data`;
* a SITE-OWNED sentinel block at C (a device id that resolves nowhere): 3896
  failures and 364 successes. It makes the tenant R750 cohort 3929/4300 =
  0.9137 and the tenant outcome total `SENTINEL_TOTAL`; neither may reach
  anyone who does not hold C;
* LIGHT replaces the block with 60 successes (R750 cohort 33/100 = 0.33), so
  the tenant control's a3 crosses the medium/high band boundary between the
  two variants while a site-A reader's a3 must not move.

THE ORACLE IS INDEPENDENT. `visible()` restates the owner rule over this
file's own tables and imports nothing from `harkeniq_cc.scope`; `build(
keep=visible(name))` seeds only what that persona may read. A scoped
reader of the full estate must then read what a TENANT-WIDE reader reads of
that reduced estate (A30.40, "What is claimed, exactly").
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from sqlalchemy import insert

from harkeniq_cc.db.models import CCFleetCache, CCOutcomeHistory

from tests.unit.cc import b2_2_estate as E
from tests.unit.cc import s3_estate as S3

T0 = E.T0

# -- the extra estate ----------------------------------------------------------

#: Servers at A whose model exists only in hidden rows at C.
EXTRA_DEVICES: dict[str, E.Device] = {d.key: d for d in (
    E.Device("a6", "A", "server", "OK", "observed", "R760", "TAG-A6", "fresh"),
    E.Device("a7", "A", "server", "OK", "observed", "R770", "TAG-A7", "fresh"),
    E.Device("a8", "A", "server", "OK", "observed", "R780", "TAG-A8", "fresh"),
)}

SENTINEL_FAILURES, SENTINEL_SUCCESSES = 3896, 364
LIGHT_SUCCESSES = 60

#: (key, device key, site recorded at, model, successes, failures). A device
#: key that names no device (`ghost-*`) is a SITE-owned row at its site.
EDGE_OUTCOMES: list[tuple[str, str, str, str, int, int]] = [
    ("r760", "ghost-r760", "C", "R760", 0, 1),
    ("r770", "ghost-r770", "C", "R770", 1, 0),
    ("r780", "ghost-r780", "C", "R780", 2, 1),
]
SENTINEL_KEY = "sentinel"


def extra_outcomes(variant: str = "heavy") -> list[tuple[str, str, str, str, int, int]]:
    rows = list(EDGE_OUTCOMES)
    if variant == "heavy":
        rows.append((SENTINEL_KEY, "ghost-sentinel", "C", E.R750,
                     SENTINEL_SUCCESSES, SENTINEL_FAILURES))
    elif variant == "light":
        rows.append((SENTINEL_KEY, "ghost-sentinel", "C", E.R750, LIGHT_SUCCESSES, 0))
    else:
        raise ValueError(variant)
    return rows


ALL_DEVICES: dict[str, E.Device] = {**E.DEVICES, **EXTRA_DEVICES}

#: The tenant's outcome total in the HEAVY estate.
SENTINEL_TOTAL = (
    sum(ok + bad for *_r, ok, bad in E.OUTCOMES)
    + sum(ok + bad for *_r, ok, bad in extra_outcomes("heavy"))
)
#: The tenant R750 cohort failure rate in the HEAVY estate, as published (4dp).
_R750 = [(ok, bad) for dev, _site, _a, ok, bad in E.OUTCOMES
         if (E.DEVICES[dev].model if dev in E.DEVICES else E.R750) == E.R750]
SENTINEL_RATE = round(
    (sum(b for _o, b in _R750) + SENTINEL_FAILURES)
    / (sum(o + b for o, b in _R750) + SENTINEL_FAILURES + SENTINEL_SUCCESSES), 4,
)

# -- personas ------------------------------------------------------------------

#: The two permissions this estate is about. Every human here holds a tenant
#: role that carries both (checked against the vocabulary at import).
FLEET, INCIDENT = "fleet.view", "incident.view"


@dataclass(frozen=True)
class Persona:
    #: [(scope_type, ref, permission subset or None, lifecycle or None)]
    grants: tuple
    role: str = "site_admin"


PERSONAS: dict[str, Persona] = {
    "h-owner": Persona((), role="tenant_owner"),
    "h-org-ab": Persona((("org_unit", "@ab", None, None),)),
    "h-site-a": Persona((("site", "A", None, None),)),
    # a2 has two outcomes of its own: its basis is a cohort prior, and for a
    # device reader that cohort is its own rows (D-P9: one function).
    "h-device-a2": Persona((("device", "dev:a2", None, None),)),
    # The moved device: its rows at C are C's, not its.
    "h-device-mv": Persona((("device", "dev:mv", None, None),)),
    "h-class-server": Persona((("device_class", "server", None, None),)),
    "h-class-switch": Persona((("device_class", "switch", None, None),)),
    # fleet.view at A and C, incident.view only at A.
    "h-mixed": Persona((("site", "A", None, None), ("site", "C", (FLEET,), None))),
    # incident.view at C WITHOUT fleet.view there: C contributes nothing here.
    "h-mixed-incident": Persona((("site", "A", None, None), ("site", "C", (INCIDENT,), None))),
    "h-revoked": Persona((("site", "A", None, "revoked"),)),
    "h-expired": Persona((("site", "A", None, "expired"),)),
    "h-lapsed-all": Persona((("site", "A", None, "expired"), ("site", "B", None, "revoked"))),
    "h-no-grant": Persona(()),
    # action.approve at A, fleet.view nowhere.
    "h-approve-only": Persona((("site", "A", ("action.approve",), None),)),
}
#: Every persona whose reach is NOT the tenant.
SCOPED = tuple(name for name in PERSONAS if name != "h-owner")
#: Personas with no effective `fleet.view` reach at all.
ZERO_REACH = ("h-revoked", "h-expired", "h-lapsed-all", "h-no-grant", "h-approve-only")
#: Scoped personas that read something.
READERS = tuple(name for name in SCOPED if name not in ZERO_REACH)

_REGION = {key: site.region for key, site in S3.SITES.items()}


def _check_vocabulary() -> None:
    from harkeniq_console.permissions import ROLE_PERMISSIONS

    for role in {p.role for p in PERSONAS.values()}:
        held = set(ROLE_PERMISSIONS[role])
        assert "*" in held or {FLEET, INCIDENT} <= held, role


_check_vocabulary()


def _live(name: str) -> list[tuple]:
    if PERSONAS[name].role == "tenant_owner":
        return [("tenant", "", None)]
    return [(t, ref, sub) for t, ref, sub, life in PERSONAS[name].grants if life is None]


def _carries(subset, permission: str) -> bool:
    return subset is None or permission in subset


def covers_site(name: str, site: str, permission: str) -> bool:
    for t, ref, sub in _live(name):
        if not _carries(sub, permission):
            continue
        if t == "tenant" or (t == "site" and ref == site) or (
                t == "org_unit" and _REGION.get(site) == ref[1:]):
            return True
    return False


def covers_device(name: str, key: str, permission: str) -> bool:
    device = ALL_DEVICES[key]
    if covers_site(name, device.site, permission):
        return True
    for t, ref, sub in _live(name):
        if not _carries(sub, permission):
            continue
        if (t == "device" and ref == f"dev:{key}") or (
                t == "device_class" and ref == device.device_class):
            return True
    return False


def covers_row(name: str, site: str, device_key: str, permission: str) -> bool:
    """The owner rule, restated: a row naming a device that RESOLVES at the
    row's own site is the device's; every other row is its site's."""
    if device_key in ALL_DEVICES and ALL_DEVICES[device_key].site == site:
        return covers_device(name, device_key, permission)
    return covers_site(name, site, permission)


def held_sites(name: str) -> set[str]:
    return {s for s in S3.ALL if covers_site(name, s, FLEET)}


@dataclass(frozen=True)
class Keep:
    base: E.Keep
    devices: frozenset           # keys of EXTRA_DEVICES
    outcomes: frozenset          # keys of extra outcome rows
    variant: str = "heavy"

    @classmethod
    def everything(cls, variant: str = "heavy") -> "Keep":
        return cls(
            base=E.Keep.everything(),
            devices=frozenset(EXTRA_DEVICES),
            outcomes=frozenset(r[0] for r in extra_outcomes(variant)),
            variant=variant,
        )


def visible(name: str, variant: str = "heavy") -> Keep:
    """What human persona `name` may read, decided here and only here.

    The human Attention path reads incidents under the route's `fleet.view`
    reach (unchanged by A30.40, S2's composite follow-up), so incidents are
    kept by `fleet.view` here too.
    """
    devices = frozenset(k for k in E.DEVICES if covers_device(name, k, FLEET))
    outcomes = frozenset(
        i for i, (dev, site, *_r) in enumerate(E.OUTCOMES) if covers_row(name, site, dev, FLEET)
    )
    routes = frozenset(
        key for key, site, dev, *_r in E.ROUTES if covers_row(name, site, dev, FLEET)
    )
    incidents = frozenset(
        key for key, inc in E.INCIDENTS.items()
        if inc.site and covers_row(name, inc.site, inc.device, FLEET)
    )
    held = held_sites(name)
    signals = frozenset(
        key for key, scope_type, site, *_r in E.SIGNALS
        if scope_type == "cohort" or site in held
    )
    patterns = frozenset(
        key for key, _type, named, *_r in E.PATTERNS if not named or set(named) & held
    )
    extra_devices = frozenset(k for k in EXTRA_DEVICES if covers_device(name, k, FLEET))
    extra = frozenset(
        key for key, dev, site, *_r in extra_outcomes(variant)
        if covers_row(name, site, dev, FLEET)
    )
    referenced = (
        {E.DEVICES[k].site for k in devices}
        | {ALL_DEVICES[k].site for k in extra_devices}
        | {E.OUTCOMES[i][1] for i in outcomes}
        | {site for key, dev, site, *_r in extra_outcomes(variant) if key in extra}
        | {site for key, site, *_r in E.ROUTES if key in routes}
        | {E.INCIDENTS[k].site for k in incidents}
    )
    base = E.Keep(
        sites=frozenset(held | referenced), devices=devices, outcomes=outcomes,
        routes=routes, incidents=incidents, signals=signals, patterns=patterns,
    )
    return Keep(base=base, devices=extra_devices, outcomes=extra, variant=variant)


# -- building ------------------------------------------------------------------


def agent(stack, key: str) -> str:
    return stack.tagged(f"b22-{key}")


async def build(keep: Optional[Keep] = None, **kw) -> S3.Stack:
    """The S3 production stack over this estate -- all of it, or `keep`."""
    keep = keep or Keep.everything()
    stack = await E.build(keep.base, **kw)
    await seed_extra(stack, keep)
    return stack


async def seed_extra(stack, keep: Keep) -> None:
    t = stack.tagged
    async with stack.sessionmaker() as session:
        for key in sorted(keep.devices):
            d = EXTRA_DEVICES[key]
            session.add(CCFleetCache(
                id=t(f"a3040-fl-{key}"), site_id=stack.site(d.site),
                agent_id=agent(stack, key), agent_name=agent(stack, key),
                vendor="Dell", model=d.model, device_class=d.device_class,
                observation=d.observation, health=d.health,
                service_tag=d.service_tag, firmware=[],
                last_seen_at=E.SEEN[d.seen], snapshot_at=E.SNAPSHOT_AT,
            ))
        rows = []
        for key, dev, site, model, ok, bad in extra_outcomes(keep.variant):
            if key not in keep.outcomes:
                continue
            for n, result in enumerate(["SUCCESS"] * ok + ["FAILURE"] * bad):
                rows.append({
                    "site_id": stack.site(site), "action_id": t(f"a3040-oc-{key}-{n}"),
                    "action_type": "SEL_CLEAR", "device_agent_id": agent(stack, dev),
                    "vendor": "Dell", "model": model, "outcome": result,
                    "fault_resolved": result == "SUCCESS", "actor": "a3040",
                    "recorded_at": T0 - timedelta(days=2),
                    "ingested_at": T0 + timedelta(seconds=n),
                })
        if rows:
            await session.execute(insert(CCOutcomeHistory), rows)
        await session.commit()


async def add_hidden(stack, outcome: str, n: int, *, model: str = E.R750,
                     site: str = "C") -> None:
    """One more outcome at a site, on a device that resolves nowhere."""
    async with stack.sessionmaker() as session:
        session.add(CCOutcomeHistory(
            site_id=stack.site(site), action_id=stack.tagged(f"a3040-extra-{n}"),
            action_type="SEL_CLEAR", device_agent_id=agent(stack, "ghost-extra"),
            vendor="Dell", model=model, outcome=outcome,
            fault_resolved=outcome == "SUCCESS", actor="a3040",
            recorded_at=T0 - timedelta(days=1), ingested_at=T0 + timedelta(hours=n),
        ))
        await session.commit()


async def remove_hidden(stack, n: int) -> None:
    from sqlalchemy import delete

    async with stack.sessionmaker() as session:
        await session.execute(delete(CCOutcomeHistory).where(
            CCOutcomeHistory.action_id == stack.tagged(f"a3040-extra-{n}")))
        await session.commit()


# -- reading -------------------------------------------------------------------


async def human(stack, name: str) -> str:
    """Become human persona `name` on `stack`; returns its subject."""
    persona = PERSONAS[name]
    if persona.role == "tenant_owner":
        stack.as_person(S3.OWNER, "tenant_owner")
        return S3.OWNER
    subject = f"kc-a3040-{name}"
    made = stack.__dict__.setdefault("_a3040_humans", set())
    if subject not in made:
        made.add(subject)
        for scope_type, ref, subset, lifecycle in persona.grants:
            if ref.startswith("@"):
                ref = stack.regions[ref[1:]]
            elif ref.startswith("dev:"):
                ref = agent(stack, ref[4:])
            elif ref in S3.SITES:
                ref = stack.site(ref)
            grant_id = await stack.grant(
                subject, scope_type, ref, subset=list(subset) if subset else None,
            )
            if lifecycle:
                await stack.lapse(grant_id, how=lifecycle)
    stack.as_person(subject, persona.role)
    return subject


async def get(stack, name: str, path: str, **params) -> dict:
    await human(stack, name)
    async with stack.client() as client:
        res = await client.get(path, params=params or None)
    assert res.status_code == 200, (name, path, params, res.status_code, res.text[:300])
    return res.json()


async def predictive(stack, name: str, **params) -> dict:
    return await get(stack, name, "/api/predictive/risk", **params)


async def attention(stack, name: str, **params) -> dict:
    return E.normalised(await get(stack, name, "/api/attention/", **params))


def query(stack, params: dict) -> dict:
    return {k: (stack.site(v) if k == "site_id" else v) for k, v in params.items()}


PREDICTIVE_QUERIES: tuple[dict, ...] = (
    {}, {"band": "high"}, {"band": "medium"}, {"band": "insufficient_data"}, {"site_id": "A"},
)
ATTENTION_QUERIES: tuple[dict, ...] = (
    {}, {"band": "high"}, {"band": "medium"}, {"band": "insufficient_data"},
    {"limit": 2}, {"site_id": "A"},
)


async def tenant_wide_payloads(stack) -> dict:
    """What the tenant owner reads of both predictive surfaces."""
    out: dict = {"predictive": {}, "attention": {}}
    for params in PREDICTIVE_QUERIES:
        out["predictive"][json.dumps(params, sort_keys=True)] = await predictive(
            stack, "h-owner", **query(stack, params))
    for params in ATTENTION_QUERIES:
        out["attention"][json.dumps(params, sort_keys=True)] = await attention(
            stack, "h-owner", **query(stack, params))
    stack.as_person()
    return json.loads(json.dumps(out, sort_keys=True))


# -- the deletion-equivalence projection of an Attention answer -----------------

#: D-P6, restated here rather than imported: the phrase a scoped reader's
#: predictive REASONS carry. Removing it gives the tenant-wide sentence.
SCOPED_PHRASE = " in your current view"
#: The confidence explanations, scoped -> tenant-wide. The cohort sentence
#: does not just gain the phrase: "across the fleet" is exactly what is no
#: longer true for a scoped reader.
EXPLANATIONS = {
    "Scored from this device's own outcome history in your current view.":
        "Scored from this device's own outcome history.",
    "Too few outcomes for this device; scored from the failure rate of the "
    "same vendor and model in your current view.":
        "Too few outcomes for this device; scored from the failure rate of the "
        "same vendor and model across the fleet.",
    "Not enough evidence in your current view to score this device. This is "
    "not a clean bill of health — it is an absence of data.":
        "Not enough evidence to score this device. This is not a clean bill of "
        "health — it is an absence of data.",
}
#: Reasons that quote S4's bounded class-B learning (tenant knowledge, where
#: deletion equivalence does not apply: A23).
_LEARNING_REASON = re.compile(r"^(Learned for |Fleet-wide failure rate)")


def comparable(answer: dict) -> dict:
    """An Attention answer with S4's class-B learning removed and the D-P6
    wording mapped back: what deletion equivalence is claimed over."""
    out = json.loads(json.dumps(answer))
    for item in out.get("items", []):
        evidence = item.get("evidence", {})
        evidence.pop("learned_signals", None)
        evidence.pop("fleet_patterns", None)
        item["reasons"] = [r.replace(SCOPED_PHRASE, "") for r in item.get("reasons", [])
                           if not _LEARNING_REASON.match(r)]
        confidence = item.get("confidence", {})
        if "explanation" in confidence:
            text = confidence["explanation"]
            confidence["explanation"] = EXPLANATIONS.get(text, text)
    return out


def walk(node, path=()):
    """(path, leaf) for every key and every scalar, at every depth."""
    yield from E.leaves(node, path)
