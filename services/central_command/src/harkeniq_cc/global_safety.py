"""The closed global safety gate (S3-E1, spec A30.37; A30.30; A30.36 D1).

SITE-LOCAL AUTONOMY + GLOBAL SAFETY GATE = FINAL EXECUTION ELIGIBILITY.
This module owns the second term and the conjunction. The first is the one
composer's (`harkeniq_cc.autonomy.build_autonomy`, over the target's own
site); nothing here recomputes it.

What the gate is
----------------
A CLOSED, typed set of members. A member reads a typed snapshot of the whole
estate's safety state -- including sites a reader does not hold, which is the
ONE ratified channel through which a hidden site may REDUCE eligibility
(A30.30(c)) -- and answers with an enum. Nothing else leaves it: no member
id, no site, no device, no fault domain, no count, no text. Every projection
the gate reaches carries exactly one code, `GLOBAL_SAFETY_CONSTRAINT`
(A30.30(h)).

What the gate is not
--------------------
It is never a grant. There is no state it can return that widens anything:
`final_execution_eligibility` answers the local disposition when the gate is
clear and DENIED otherwise, so the gate can only leave eligibility where the
site put it or lower it. It never converts a MODE either (A30.37, answer 1):
the approval path a proposal or a campaign takes is the site-local
assessment's; the gate holds EXECUTION, on both bases (D3).

The production registry is EMPTY (D1). A member is named in the
specification by dated amendment BEFORE it exists in code; none has been.
An empty registry is `clear` and changes nothing.

The TEST-ONLY probe
-------------------
`TestOnlyProbeMember` exists so the framework can be proved on a real,
secure-mode stack (A30.37, answer 2). It is registered ONLY when
`HARKEN_CC_GLOBAL_SAFETY_TEST_PROBE` names a trigger file, which the compose
gate sets through its own override and nothing shipped sets; startup logs a
WARNING whenever it is registered. It can only narrow -- it has no other
answer to give.
"""

from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Protocol, runtime_checkable

logger = logging.getLogger("harkeniq.cc.global_safety")

# ---------------------------------------------------------------------------
# Vocabulary (D10). Closed.
# ---------------------------------------------------------------------------

#: The ONE reason code the gate contributes to any projection (A30.30(h)).
#: The exact ratified string; there is no second spelling anywhere.
GLOBAL_SAFETY_CONSTRAINT = "GLOBAL_SAFETY_CONSTRAINT"

#: The scope of the gate's blocking row. It names nothing: a `global` row has
#: no site, no device, no domain -- which is why every reader may see it.
SCOPE_GLOBAL = "global"

STATE_CLEAR = "clear"
STATE_CONSTRAINED = "constrained"
#: A member errored, or answered anything but its enum. FAILS CLOSED: it
#: narrows exactly as `constrained` does (D1). Kept distinct so the platform
#: can say the gate could not be evaluated rather than inventing a reason.
STATE_UNKNOWN = "unknown"

GATE_STATES = frozenset({STATE_CLEAR, STATE_CONSTRAINED, STATE_UNKNOWN})

#: The constant text of the gate's row. It says THAT execution is held and
#: what releases it -- never what holds it or where.
GLOBAL_ROW_DETAIL = (
    "a global safety constraint is active; execution is held, whatever the "
    "approval, until it clears"
)

#: The constant text a withheld dispatch records for the gate. It says what
#: holds, never what happens next: the synchronous approval path records it
#: on a terminal failure (A30.17, D6; F-12), where "resumes" would be false.
GLOBAL_WITHHELD_REASON = (
    f"withheld ({GLOBAL_SAFETY_CONSTRAINT}): a global safety constraint is "
    "active; nothing is dispatched while it holds"
)


class MemberVerdict(enum.Enum):
    """What a member may answer. There is no member answer that grants."""

    CLEAR = "clear"
    CONSTRAIN = "constrain"


# ---------------------------------------------------------------------------
# What a member reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SiteSafetyFact:
    """One site's reported safety state, as a member may READ it.

    Input only. Nothing here reaches a projection: a member's only output
    is a `MemberVerdict`.
    """

    site_id: str
    reported: bool
    sm_stop_switch: bool
    dropped_back: frozenset
    exhausted: frozenset
    suppressions: int


@dataclass(frozen=True)
class GlobalSafetyContext:
    """The question a member answers: may THIS class execute at THIS target?

    `target_site_id` is empty when the question is class-level (a composite
    over several sites, or a reader with no single focus).
    """

    tenant_id: str
    action_type: str
    target_site_id: str
    estate: tuple
    now: datetime


@runtime_checkable
class GlobalSafetyMember(Protocol):
    """A gate member: a closed identifier and one enum-valued question."""

    member_id: str

    def evaluate(self, context: GlobalSafetyContext) -> MemberVerdict: ...


def estate_from_rows(safety_rows: Iterable[Any], now: datetime) -> tuple:
    """A typed, read-only snapshot of every site's safety state.

    Built from Central Command's `cc_safety_state` rows. `reported` is the
    composer's own freshness rule (`autonomy.site_reported`), so a member
    and a local assessment can never disagree about whether a site spoke.
    """
    from harkeniq_cc.autonomy import site_reported

    facts = []
    for row in safety_rows:
        budgets = getattr(row, "site_budgets", None) or {}
        facts.append(SiteSafetyFact(
            site_id=str(getattr(row, "site_id", "") or ""),
            reported=site_reported(row, now),
            sm_stop_switch=bool(getattr(row, "sm_stop_switch", False)),
            dropped_back=frozenset(
                str(e.get("action_type", ""))
                for e in (getattr(row, "error_budgets", None) or [])
                if isinstance(e, dict) and e.get("dropped_back")
            ),
            exhausted=frozenset(
                str(at) for at, remaining in budgets.items() if remaining == 0
            ),
            suppressions=len(getattr(row, "suppressions", None) or []),
        ))
    return tuple(sorted(facts, key=lambda f: f.site_id))


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GlobalSafetyVerdict:
    """The gate's answer for one question. Bounded by construction."""

    state: str

    def __post_init__(self) -> None:
        if self.state not in GATE_STATES:
            raise ValueError(
                f"global safety state {self.state!r} is not one of "
                f"{sorted(GATE_STATES)} (spec A30.37)"
            )

    @property
    def clear(self) -> bool:
        return self.state == STATE_CLEAR

    @property
    def reason_codes(self) -> tuple:
        return () if self.clear else (GLOBAL_SAFETY_CONSTRAINT,)

    def as_dict(self) -> dict:
        """The projection. The state and the one code -- nothing else exists."""
        return {"state": self.state, "reason_codes": list(self.reason_codes)}


CLEAR = GlobalSafetyVerdict(STATE_CLEAR)
CONSTRAINED = GlobalSafetyVerdict(STATE_CONSTRAINED)
UNKNOWN = GlobalSafetyVerdict(STATE_UNKNOWN)


def require_verdict(verdict) -> GlobalSafetyVerdict:
    """A `GlobalSafetyVerdict`, or a TypeError. There is no implicit clear."""
    if isinstance(verdict, GlobalSafetyVerdict):
        return verdict
    raise TypeError(
        "a global safety verdict is required, not "
        f"{type(verdict).__name__}: 'no gate' is not a value (spec A30.37)"
    )


# ---------------------------------------------------------------------------
# The conjunction (FINAL EXECUTION ELIGIBILITY)
# ---------------------------------------------------------------------------


def strictest(verdicts: Iterable[Any]) -> GlobalSafetyVerdict:
    """One verdict for several targets: constrained over unknown over clear.

    Both non-clear states fail closed; the order only chooses which word is
    reported. An empty iterable is a question nobody asked -- UNKNOWN.
    """
    states = {require_verdict(v).state for v in verdicts}
    if not states:
        return UNKNOWN
    if STATE_CONSTRAINED in states:
        return CONSTRAINED
    if STATE_UNKNOWN in states:
        return UNKNOWN
    return CLEAR


def final_execution_eligibility(local_disposition: str, verdict) -> str:
    """THE conjunction. The local disposition when the gate is clear; DENIED
    when it is not. Never above the local assessment (A30.37 invariant ii).

    DENIED here means "may not execute now", on any basis -- a human's
    approval does not override the gate (D3). It is an EXECUTION answer;
    the MODE a proposal was admitted under never reads it (answer 1).
    """
    from harkeniq_cc.autonomy import DENIED

    verdict = require_verdict(verdict)
    if verdict.clear:
        return local_disposition
    return DENIED


def final_block(local_disposition: str, verdict) -> dict:
    """The `final_execution_eligibility` block a contract carries."""
    verdict = require_verdict(verdict)
    return {
        "disposition": final_execution_eligibility(local_disposition, verdict),
        "reason_codes": list(verdict.reason_codes),
    }


def global_row() -> dict:
    """The gate's blocking row, built from constants and nothing else."""
    return {
        "code": GLOBAL_SAFETY_CONSTRAINT,
        "detail": GLOBAL_ROW_DETAIL,
        "scope": SCOPE_GLOBAL,
    }


def is_global_row(row: Any) -> bool:
    return isinstance(row, dict) and row.get("scope") == SCOPE_GLOBAL


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


class GlobalSafetyGate:
    """The closed gate over one estate snapshot.

    Built by a loader (it may do I/O through a member); asked by pure code
    through `verdict`. Each (class, target) is evaluated once and memoized,
    so one decision pass asks every member once per question.
    """

    def __init__(
        self,
        *,
        members: Iterable[Any],
        tenant_id: str,
        estate: tuple,
        now: Optional[datetime] = None,
    ) -> None:
        self._members = tuple(members)
        self._tenant_id = tenant_id
        self._estate = tuple(estate)
        self._now = now or datetime.now(timezone.utc)
        self._memo: dict[tuple[str, str], GlobalSafetyVerdict] = {}

    @property
    def member_count(self) -> int:
        return len(self._members)

    def verdict(self, action_type: str, target_site_id: str = "") -> GlobalSafetyVerdict:
        key = ((action_type or "").upper(), target_site_id or "")
        cached = self._memo.get(key)
        if cached is not None:
            return cached
        result = self._evaluate(*key)
        self._memo[key] = result
        return result

    def _evaluate(self, action_type: str, target_site_id: str) -> GlobalSafetyVerdict:
        if not self._members:
            return CLEAR
        context = GlobalSafetyContext(
            tenant_id=self._tenant_id,
            action_type=action_type,
            target_site_id=target_site_id,
            estate=self._estate,
            now=self._now,
        )
        constrained = False
        failed = False
        for member in self._members:
            member_id = getattr(member, "member_id", type(member).__name__)
            try:
                answer = member.evaluate(context)
            except Exception:  # noqa: BLE001 -- D1: a member that errors fails closed
                logger.warning(
                    "global safety member %s failed to evaluate; failing closed",
                    member_id, exc_info=True,
                )
                failed = True
                continue
            if answer is MemberVerdict.CLEAR:
                continue
            if answer is MemberVerdict.CONSTRAIN:
                constrained = True
                continue
            # Anything but the enum -- a truthy string, None, a bool -- is not
            # an answer this gate recognizes, and unrecognized is not clear.
            logger.warning(
                "global safety member %s answered %r, not a MemberVerdict; "
                "failing closed", member_id, answer,
            )
            failed = True
        if constrained:
            return CONSTRAINED
        if failed:
            return UNKNOWN
        return CLEAR


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

#: The PRODUCTION members. EMPTY (A30.36 D1): no production member is
#: invented to exercise the mechanism, and a member is named in the
#: specification by dated amendment before it exists here. A structural test
#: holds this empty.
PRODUCTION_MEMBERS: tuple = ()

#: The configuration key that registers the TEST-ONLY probe.
PROBE_ENV = "HARKEN_CC_GLOBAL_SAFETY_TEST_PROBE"

TEST_PROBE_MEMBER_ID = "test_only_probe"

#: A first line that makes the probe raise, so fail-closed can be proved live.
PROBE_RAISE = "raise"

#: The members this process evaluates. `configure` sets it once, at startup.
_ACTIVE: tuple = PRODUCTION_MEMBERS


class TestOnlyProbeMember:
    """TEST-ONLY. Never a production member, never registered by default.

    Constrains while its trigger file exists. The file's lines name the
    action classes it constrains; an empty file constrains every class; a
    first line of ``raise`` makes it raise, so a fail-closed gate can be
    proved on a live stack. It has no answer that clears anything the site
    assessment did not already clear.
    """

    __test__ = False  # not a pytest test class, whatever its name says

    member_id = TEST_PROBE_MEMBER_ID

    def __init__(self, trigger_path: str) -> None:
        self.trigger_path = trigger_path

    def evaluate(self, context: GlobalSafetyContext) -> MemberVerdict:
        if not os.path.exists(self.trigger_path):
            return MemberVerdict.CLEAR
        with open(self.trigger_path, encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh.read().splitlines() if ln.strip()]
        if lines and lines[0].lower() == PROBE_RAISE:
            raise RuntimeError("TEST-ONLY probe asked to fail")
        classes = {ln.upper() for ln in lines}
        if not classes or context.action_type.upper() in classes:
            return MemberVerdict.CONSTRAIN
        return MemberVerdict.CLEAR


def configure(config: Any) -> tuple:
    """Set this process's members from its configuration. Called ONCE, at startup.

    Production: `PRODUCTION_MEMBERS`, which is empty. The TEST-ONLY probe is
    added only when its configuration key names a trigger file, and a
    WARNING says so every time -- a deployment that set it by accident must
    not run with it silently.
    """
    global _ACTIVE
    members = PRODUCTION_MEMBERS
    trigger = (getattr(config, "global_safety_test_probe", "") or "").strip()
    if trigger:
        logger.warning(
            "TEST-ONLY global safety probe REGISTERED (trigger file %s). It can "
            "only narrow execution, never widen it, and it must never be set "
            "in production (%s).", trigger, PROBE_ENV,
        )
        members = members + (TestOnlyProbeMember(trigger),)
    _ACTIVE = members
    return members


def active_members() -> tuple:
    """The members this process evaluates: `PRODUCTION_MEMBERS` unless
    `configure` registered the TEST-ONLY probe."""
    return _ACTIVE
