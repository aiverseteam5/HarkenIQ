"""A6-4B0b-S3 (spec A30.26): ONE sentinel estate for autonomy scope isolation.

Four sites under two regions, each carrying safety state that can be told
apart from every other site's at a glance -- so that when a fact from a
site a reader does not hold reaches that reader, the payload itself says
which site it came from.

    region AB                      region CD
      A  s3-alpha    reported        C  s3-charlie  reported, DROPPED BACK,
      B  s3-bravo    reported                       stop switch ON, budget spent
                                     D  s3-delta    never reported

MAGNITUDE ENCODES PROVENANCE. Every number a site contributes to an
aggregate lives in its own decade band:

    A      1 ..      99
    B    100 ..   9 999
    C  10 000 and up

so a sum that includes site C is at least 10 000 and a sum that includes
site B is at least 100, whatever else is in it. A reader who does not hold
C may therefore receive NO number >= 10 000 anywhere in a payload, and one
who holds only A none >= 100 -- checked by walking every leaf, with no
knowledge of which field an aggregate lives in. That is what makes the
sentinel find a leak in a field nobody thought to list.

Names do the same job for strings: `SECRET-C`, `SECRET-REASON-C`,
`SECRET-SIGNAL-C`, the site id and the site name.

The estate is used three ways, and the first is the proof that matters:

* `build(sites=...)` seeds ANY subset, so a test can read the contract a
  tenant-wide reader gets over an estate in which the hidden sites DO NOT
  EXIST, and demand that a scoped reader of the full estate gets the same
  bytes. "A hidden site has no influence" and "deleting it changes
  nothing" are one statement (deletion equivalence);
* through the production stack -- the production `get_scope`, persisted
  grants, STRICT -- for every persona in the S3 matrix;
* as plain objects for the pure composer, including the poisoned rows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from typing import Iterable, Optional

import httpx

from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import (
    CCAutonomyBudget,
    CCFleetCache,
    CCIncident,
    CCLearnedSignal,
    CCOutcomeHistory,
    CCSafetyState,
    CCScopeGrant,
    CCSite,
)
from harkeniq_cc.db.repos import OrgUnitRepo, ScopeGrantRepo, TenantSettingsRepo
from harkeniq_cc.route_contract import MACHINE_JOBS
from harkeniq_cc.runtime import AppState
from harkeniq_cc.scope import ENFORCEMENT_STRICT

TENANT = "tenant-demo"
OWNER = "kc-s3-owner"
AS_OF = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

#: Band floors. A numeric leaf at or above a floor carries that site.
BAND_B = 100
BAND_C = 10_000


@dataclass(frozen=True)
class Site:
    key: str
    id: str
    name: str
    region: str
    device: str
    device_class: str


SITES: dict[str, Site] = {
    "A": Site("A", "s3-site-a", "s3-alpha", "ab", "node-s3-a", "server"),
    "B": Site("B", "s3-site-b", "s3-bravo", "ab", "node-s3-b", "switch"),
    "C": Site("C", "s3-site-c", "s3-charlie", "cd", "node-s3-c", "server"),
    "D": Site("D", "s3-site-d", "s3-delta", "cd", "node-s3-d", "server"),
}
ALL = tuple(SITES)

#: Strings that identify a site. A reader who does not hold the site may
#: not find ANY of them in a payload -- in a value or in a key.
MARKERS: dict[str, tuple[str, ...]] = {
    "A": ("s3-site-a", "s3-alpha", "fault-A", "reason-A", "signal-A", "node-s3-a"),
    "B": ("s3-site-b", "s3-bravo", "fault-B", "reason-B", "signal-B", "node-s3-b"),
    "C": ("s3-site-c", "s3-charlie", "SECRET-C", "SECRET-REASON-C",
          "SECRET-SIGNAL-C", "node-s3-c"),
    "D": ("s3-site-d", "s3-delta", "node-s3-d"),
}


def _suppression(domain: str, reason: str) -> dict:
    return {
        "domain_id": domain, "event_family": "power",
        "trigger_reason": reason, "device_count": 2,
    }


def _budget(action: str, success: int, failure: int, dropped: bool = False) -> dict:
    return {
        "action_type": action, "success_count": success,
        "failure_count": failure, "total_count": success + failure,
        "min_success_rate": 0.95, "dropped_back": dropped,
    }


#: Live safety state per site. D has a row and no report -- exactly what the
#: poller writes when a snapshot carried no safety state.
SAFETY: dict[str, dict] = {
    "A": {
        "reported": True, "sm_stop_switch": False,
        "suppressions": [_suppression("fault-A", "reason-A")],
        "error_budgets": [_budget("SEL_CLEAR", 9, 0)],
        "site_budgets": {"SEL_CLEAR": 5, "BMC_RESET": 3},
    },
    "B": {
        "reported": True, "sm_stop_switch": False,
        "suppressions": [_suppression("fault-B", "reason-B")],
        "error_budgets": [_budget("SEL_CLEAR", 800, 100)],
        "site_budgets": {"SEL_CLEAR": 500, "BMC_RESET": 300},
    },
    "C": {
        "reported": True, "sm_stop_switch": True,
        "suppressions": [_suppression("SECRET-C", "SECRET-REASON-C")],
        "error_budgets": [
            _budget("SEL_CLEAR", 10_000, 80_000, dropped=True),
            _budget("BMC_RESET", 20_000, 0),
        ],
        # BMC_RESET is SPENT at C: a hidden site that changes a
        # disposition without contributing a single large number.
        "site_budgets": {"SEL_CLEAR": 50_000, "BMC_RESET": 0},
    },
    "D": {"reported": False},
}

#: Outcome rows: (action, SUCCESS count, FAILURE count). Rows cannot live in
#: decade bands, so these are asserted as exact values and by deletion
#: equivalence; they are small distinct primes so no two subsets tie.
OUTCOMES: dict[str, list[tuple[str, int, int]]] = {
    "A": [("SEL_CLEAR", 7, 0)],
    "B": [("SEL_CLEAR", 9, 2)],
    "C": [("SEL_CLEAR", 0, 13), ("BMC_RESET", 6, 0)],
    "D": [],
}

#: (id, scope_type, site key or "", statement, confidence). One cohort
#: signal -- tenant knowledge by A23 -- and one per reporting site.
SIGNALS = [
    ("s3-sig-cohort", "cohort", "", "cohort-knowledge", 0.90),
    ("s3-sig-a", "site", "A", "signal-A", 0.80),
    ("s3-sig-b", "site", "B", "signal-B", 0.70),
    ("s3-sig-c", "site", "C", "SECRET-SIGNAL-C", 0.60),
]


# ---------------------------------------------------------------------------
# The independent oracle: what a reader holding `keys` must be told
# ---------------------------------------------------------------------------


def expected_error_budget(keys: Iterable[str], action: str = "SEL_CLEAR"):
    """The folded error budget over exactly these sites, or None.

    Computed from the estate definition above, not from the composer, so a
    test comparing the two is comparing two implementations.
    """
    total = success = failure = 0
    dropped: list[str] = []
    seen = False
    for key in sorted(keys):
        for entry in SAFETY[key].get("error_budgets", []):
            if entry["action_type"] != action:
                continue
            seen = True
            total += entry["total_count"]
            success += entry["success_count"]
            failure += entry["failure_count"]
            if entry["dropped_back"]:
                dropped.append(SITES[key].id)
    if not seen:
        return None
    return {
        "dropped_back": bool(dropped), "total": total, "success": success,
        "failure": failure, "sites_dropped_back": dropped,
        "success_rate": round(success / total, 4) if total else None,
    }


def expected_remaining(keys: Iterable[str], action: str = "SEL_CLEAR") -> dict:
    return {
        SITES[k].id: SAFETY[k]["site_budgets"][action]
        for k in sorted(keys)
        if action in SAFETY[k].get("site_budgets", {})
    }


def expected_domains(keys: Iterable[str]) -> list[str]:
    return [
        s["domain_id"] for k in sorted(keys)
        for s in SAFETY[k].get("suppressions", [])
    ]


def expected_executions(keys: Iterable[str], action: str = "SEL_CLEAR") -> int:
    return sum(
        ok + bad for k in keys for act, ok, bad in OUTCOMES[k] if act == action
    )


def expected_reporting(keys: Iterable[str]) -> list[str]:
    return sorted(SITES[k].id for k in keys if SAFETY[k].get("reported"))


# ---------------------------------------------------------------------------
# The sentinel walk
# ---------------------------------------------------------------------------


def leaves(payload):
    """Every key and every scalar in a JSON-shaped payload."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield key
            yield from leaves(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            yield from leaves(item)
    else:
        yield payload


def leaks(payload, holds: Iterable[str]) -> list[str]:
    """Everything in `payload` that came from a site outside `holds`.

    Strings by marker (values AND keys -- `site_budget_remaining` is keyed
    by site id), numbers by decade band. Returns the offending leaves so a
    failure names what leaked instead of only that something did.
    """
    holds = set(holds)
    hidden_markers = [
        (key, marker) for key in ALL if key not in holds
        for marker in MARKERS[key]
    ]
    found: list[str] = []
    for leaf in leaves(payload):
        if isinstance(leaf, bool) or leaf is None:
            continue
        if isinstance(leaf, str):
            for key, marker in hidden_markers:
                if marker in leaf:
                    found.append(f"site {key}: {marker!r} in {leaf!r}")
        elif isinstance(leaf, (int, float)):
            if leaf >= BAND_C:
                # Carries C. (For a reader who holds C it may also carry
                # B's band, which a magnitude cannot show; exact-value
                # assertions cover that, and no persona holds C without B.)
                if "C" not in holds:
                    found.append(f"site C: number {leaf}")
            elif leaf >= BAND_B and "B" not in holds:
                found.append(f"site B: number {leaf}")
    return found


def normalised(contract: dict) -> dict:
    """A contract with the two fields that legitimately differ removed.

    `generated_at` is a clock and `actor` names whoever asked. Everything
    else must be equal for deletion equivalence to hold.
    """
    out = json.loads(json.dumps(contract))
    out.pop("generated_at", None)
    out.pop("actor", None)
    return out


# ---------------------------------------------------------------------------
# Plain objects, for the pure composer
# ---------------------------------------------------------------------------


def pure_inputs(keys: Iterable[str] = ALL) -> dict:
    """`build_autonomy` keyword arguments over exactly these sites."""
    keys = [k for k in ALL if k in set(keys)]
    safety_rows = [
        NS(
            site_id=SITES[k].id, reported=SAFETY[k].get("reported", False),
            as_of=AS_OF if SAFETY[k].get("reported") else None,
            sm_stop_switch=SAFETY[k].get("sm_stop_switch", False),
            suppressions=SAFETY[k].get("suppressions", []),
            error_budgets=SAFETY[k].get("error_budgets", []),
            site_budgets=SAFETY[k].get("site_budgets", {}),
        )
        for k in keys
    ]
    sites = [NS(id=SITES[k].id, site_name=SITES[k].name) for k in keys]
    outcomes = [
        {"action_type": act, "outcome": result, "site_id": SITES[k].id,
         "fault_resolved": result == "SUCCESS"}
        for k in keys for act, ok, bad in OUTCOMES[k]
        for result in ["SUCCESS"] * ok + ["FAILURE"] * bad
    ]
    signals = [
        NS(id=sid, signal_key=sid, action_type="SEL_CLEAR", statement=text,
           confidence=conf, scope_type=kind,
           scope_ref=SITES[key].id if key else "Dell/R750")
        for sid, kind, key, text, conf in SIGNALS
        if not key or key in keys
    ]
    return {
        "tenant_id": TENANT, "actor_id": "user:oracle", "actor_species": "human",
        "permissions": ["fleet.view"],
        "budgets": [NS(device_type="*", level=2, budget_limit=10,
                       budget_period="daily", actions_used=0)],
        "stop_switch": NS(active=False, changed_by="", updated_at=None),
        "outcomes": outcomes, "safety_rows": safety_rows, "sites": sites,
        "learned_signals": signals,
        "now": datetime(2026, 9, 20, tzinfo=timezone.utc),
    }


# ---------------------------------------------------------------------------
# The production stack
# ---------------------------------------------------------------------------


class Stack:
    def __init__(self, app, state, regions, tenant=TENANT, tag=""):
        self.app, self.state = app, state
        self.sessionmaker = state.sessionmaker
        self.regions: dict[str, str] = regions
        self.tenant, self.tag = tenant, tag
        self._who = (OWNER, "tenant_owner", None)

    def tagged(self, ref: str) -> str:
        """An estate id, as THIS stack seeded it.

        The sqlite stacks are private databases and use the bare ids. A
        PostgreSQL stack shares its database with other runs, so every id
        carries a per-run suffix -- appended, so the markers above still
        find it by substring.
        """
        return f"{ref}-{self.tag}" if self.tag else ref

    def site(self, key: str) -> str:
        return self.tagged(SITES[key].id)

    def device(self, key: str) -> str:
        return self.tagged(SITES[key].device)

    def as_person(self, sub: str = OWNER, role: str = "tenant_owner"):
        self._who = (sub, role, None)
        return self

    def as_machine(self, agent_id: str,
                   permissions=("fleet.view", "incident.view", "proposal.submit")):
        self._who = (agent_id, "", list(permissions))
        return self

    def client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
        )

    async def get(self, path: str, **params):
        async with self.client() as client:
            return await client.get(path, params=params or None)

    async def grant(self, principal: str, scope_type: str, scope_ref: str = "",
                    *, role: str = "site_admin", subset=None,
                    principal_type: str = "user") -> str:
        async with self.sessionmaker() as session:
            row = await ScopeGrantRepo(session).grant(
                tenant_id=self.tenant, principal_type=principal_type,
                principal_ref=principal, scope_type=scope_type,
                scope_ref=scope_ref, permission_subset=subset, role=role,
                granted_by="s3-test",
            )
            await session.commit()
            return row.id

    async def lapse(self, grant_id: str, *, how: str) -> None:
        """Expire or revoke one grant, the two lifecycle ends A30 names."""
        past = datetime.now(timezone.utc) - timedelta(days=1)
        async with self.sessionmaker() as session:
            row = await session.get(CCScopeGrant, grant_id)
            if how == "expired":
                row.expires_at = past
            else:
                row.revoked_at = past
            await session.commit()


async def build(sites: Iterable[str] = ALL, *, engine=None,
                tenant: str = TENANT, tag: str = "") -> Stack:
    """The production app over an estate containing exactly `sites`.

    STRICT, with one founding tenant owner -- so a persona reads through
    the grants it was given and through nothing synthesized.

    `engine`, `tenant` and `tag` exist for the real-PostgreSQL run, which
    shares one migrated database across runs: it brings its own engine, a
    tenant of its own and a suffix for every id this seeds.
    """
    keys = [k for k in ALL if k in set(sites)]
    config = CCConfig(tenant_id=tenant, insecure=True)
    configure_auth("", "", "", insecure=True)
    if engine is None:
        engine = make_engine("sqlite+aiosqlite:///:memory:")
        await create_all(engine)
    sessionmaker = make_sessionmaker(engine)

    def _t(ref: str) -> str:
        return f"{ref}-{tag}" if tag else ref

    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)

    async with sessionmaker() as session:
        units = OrgUnitRepo(session)
        root = await units.create(
            tenant, name=tenant, unit_type="organization", parent=None,
        )
        regions = {
            "ab": await units.create(tenant, name="Region AB",
                                     unit_type="region", parent=root),
            "cd": await units.create(tenant, name="Region CD",
                                     unit_type="region", parent=root),
        }
        region_ids = {name: unit.id for name, unit in regions.items()}
        session.add(CCAutonomyBudget(
            tenant_id=tenant, device_type="*", level=2,
            budget_limit=10, budget_period="daily",
        ))
        for key in keys:
            site = SITES[key]
            session.add(CCSite(
                id=_t(site.id), tenant_id=tenant, site_name=_t(site.name),
                sm_endpoint="sm:50051", sm_token="tok",
                org_unit_id=region_ids[site.region],
            ))
            await session.flush()
            session.add(CCFleetCache(
                site_id=_t(site.id), agent_id=_t(site.device), agent_name=_t(site.device),
                vendor="Dell", model="R750", device_class=site.device_class,
                observation="observed", health="Critical",
            ))
            state_row = SAFETY[key]
            session.add(CCSafetyState(
                site_id=_t(site.id), tenant_id=tenant,
                reported=state_row.get("reported", False),
                as_of=AS_OF if state_row.get("reported") else None,
                sm_stop_switch=state_row.get("sm_stop_switch", False),
                suppressions=state_row.get("suppressions", []),
                error_budgets=state_row.get("error_budgets", []),
                site_budgets=state_row.get("site_budgets", {}),
            ))
            n = 0
            for action, ok, bad in OUTCOMES[key]:
                for result in ["SUCCESS"] * ok + ["FAILURE"] * bad:
                    n += 1
                    session.add(CCOutcomeHistory(
                        site_id=_t(site.id), action_id=_t(f"act-{key}-{n}"),
                        action_type=action, device_agent_id=_t(site.device),
                        vendor="Dell", model="R750", outcome=result,
                        fault_resolved=result == "SUCCESS",
                        # A fixed, ordered clock: the loader orders by it.
                        ingested_at=AS_OF + timedelta(seconds=ALL.index(key) * 1000 + n),
                        recorded_at=AS_OF,
                    ))
        for sid, kind, key, text, conf in SIGNALS:
            if key and key not in keys:
                continue
            session.add(CCLearnedSignal(
                id=_t(sid), tenant_id=tenant, signal_key=_t(sid), scope_type=kind,
                scope_ref=_t(SITES[key].id) if key else "Dell/R750",
                action_type="SEL_CLEAR", vendor="Dell", model="R750",
                statement=text, confidence=conf, status="active",
            ))
        await TenantSettingsRepo(session).set_enforcement(
            tenant, ENFORCEMENT_STRICT, "s3-test",
        )
        await session.commit()

    stack = Stack(app, state, region_ids, tenant=tenant, tag=tag)
    await stack.grant(OWNER, "tenant", role="tenant_owner")

    async def _user():
        ref, role, machine_permissions = stack._who
        if machine_permissions is not None:
            return UserContext(
                user_id=ref, email=f"op-agent:{ref}@v1", tenant_id=tenant,
                role="", permissions=machine_permissions, species="agent",
                identity_id="s3-identity", machine_jobs=MACHINE_JOBS,
            )
        return UserContext(
            user_id=ref, email=f"{ref}@example.com", tenant_id=tenant,
            role=role, permissions=list(ROLE_PERMISSIONS[role]),
        )

    app.dependency_overrides[get_current_user] = _user
    return stack


#: persona -> (grants, the site keys whose autonomy facts it may read).
#: A grant is (scope_type, ref, subset); "@ab" / "@cd" name a region and a
#: bare site key names a site. `None` for the reach means the whole tenant.
PERSONAS: dict[str, tuple[list[tuple], Optional[tuple[str, ...]]]] = {
    "tenant":        ([("tenant", "", None)], None),
    "org_ab":        ([("org_unit", "@ab", None)], ("A", "B")),
    "site_a":        ([("site", "A", None)], ("A",)),
    "site_b":        ([("site", "B", None)], ("B",)),
    "site_d":        ([("site", "D", None)], ("D",)),
    "device_a":      ([("device", "node-s3-a", None)], ()),
    "class_server":  ([("device_class", "server", None)], ()),
    "no_scope":      ([], ()),
    # A30.24: permission from one grant never combines with reach from
    # another. Site C is granted -- for incidents only.
    "a_plus_narrow_c": (
        [("site", "A", None), ("site", "C", ["incident.view"])], ("A",)),
    # The tenant_wide shortcut, narrowed away from fleet.view.
    "tenant_narrow": ([("tenant", "", ["incident.view"])], ()),
    # An approver at C whose grant there withholds fleet.view.
    "a_plus_approve_c": (
        [("site", "A", None), ("site", "C", ["action.approve"])], ("A",)),
    "a_plus_device_c": (
        [("site", "A", None), ("device", "node-s3-c", None)], ("A",)),
}


async def persona(stack: Stack, name: str) -> tuple[str, Optional[tuple[str, ...]]]:
    """Persist this persona's grants; returns (subject, reach keys)."""
    grants, holds = PERSONAS[name]
    subject = f"kc-s3-{name}"
    for scope_type, ref, subset in grants:
        if ref.startswith("@"):
            ref = stack.regions[ref[1:]]
        elif ref in SITES and scope_type == "site":
            ref = stack.site(ref)
        elif scope_type == "device":
            ref = stack.tagged(ref)
        await stack.grant(subject, scope_type, ref, subset=subset)
    return subject, holds


async def seed_incident(stack: Stack, key: str, *, subsystem: str = "log") -> str:
    """One open incident the evaluator will propose against."""
    incident_id = stack.tagged(f"s3-inc-{key.lower()}")
    async with stack.sessionmaker() as session:
        session.add(CCIncident(
            incident_id=incident_id, tenant_id=stack.tenant,
            site_id=stack.site(key), kind="device", status="open",
            title="BMC event log saturated", device_agent_id=stack.device(key),
            subsystem=subsystem, confidence=0.9,
        ))
        await session.commit()
    return incident_id
