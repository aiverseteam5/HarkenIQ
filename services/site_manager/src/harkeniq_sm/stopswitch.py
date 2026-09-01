"""Per-site halt, and the Site Manager-wide emergency halt. E1.3.

Ratified D2:

    SITE IS THE NORMAL OPERATIONAL SAFETY BOUNDARY.

Stopping Site A stops Site A. Site B keeps operating if Site B is
independently healthy. The Site Manager-wide emergency halt still exists
and still stops everything the process serves, but it is a separate,
explicitly audited action -- **never** the default meaning of the site
control.

Two things this fixes at once
-----------------------------
Before E1.3 the switch was a single in-memory boolean on
``SMAutonomyEnforcer``. It was neither per site nor persisted, so an
operator could halt a site, the process could restart, and autonomy would
silently resume with nothing in the record saying it had ever stopped. A
stop switch that forgets is worse than no stop switch, because it is
trusted.

Where this sits in the decision
-------------------------------
The halt is ONE input among ten, and never on its own a licence to run.
The full chain a governed execution must satisfy::

    tenant stop  +  site stop  +  SM emergency halt  +  agent scope
      +  permission  +  capability  +  autonomy  +  lease
      +  preconditions  +  blast radius

Any one of them refusing is a refusal. An autonomy level is a ceiling,
never an unconditional execution authority -- which is exactly what
:func:`execution_permitted` is here to make structural.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

logger = logging.getLogger("harkeniq.sm.stopswitch")

#: Three distinct halts, and they are NOT interchangeable.
#:
#:   tenant        pushed down from Central Command. Stops the tenant,
#:                 therefore every site this Site Manager serves.
#:   site          the normal operational boundary (ratified D2). Stops
#:                 one site and leaves the others running.
#:   site_manager  an operator's emergency halt for the whole process.
#:                 Explicit, separately audited, and NEVER the default
#:                 meaning of the site control.
SCOPE_TENANT = "tenant"
SCOPE_SITE = "site"
SCOPE_SITE_MANAGER = "site_manager"
SCOPES = (SCOPE_TENANT, SCOPE_SITE, SCOPE_SITE_MANAGER)


@dataclass(frozen=True)
class HaltState:
    """Which halts are in force for one site, and why."""

    site_id: str
    tenant_halted: bool = False
    tenant_halted_by: str = ""
    tenant_reason: str = ""
    site_halted: bool = False
    site_halted_by: str = ""
    site_reason: str = ""
    manager_halted: bool = False
    manager_halted_by: str = ""
    manager_reason: str = ""

    @property
    def halted(self) -> bool:
        return self.tenant_halted or self.site_halted or self.manager_halted

    @property
    def reason(self) -> str:
        if self.tenant_halted:
            return (
                "the tenant stop switch is active"
                + (f": {self.tenant_reason}" if self.tenant_reason else "")
            )
        if self.manager_halted:
            return (
                "Site Manager emergency halt is active"
                + (f": {self.manager_reason}" if self.manager_reason else "")
            )
        if self.site_halted:
            return (
                "this site is stopped"
                + (f": {self.site_reason}" if self.site_reason else "")
            )
        return ""

    def as_dict(self) -> dict:
        return {
            "site_id": self.site_id,
            "halted": self.halted,
            "tenant_stop": {
                "active": self.tenant_halted,
                "activated_by": self.tenant_halted_by,
                "reason": self.tenant_reason,
            },
            "site_stop": {
                "active": self.site_halted,
                "activated_by": self.site_halted_by,
                "reason": self.site_reason,
            },
            "site_manager_halt": {
                "active": self.manager_halted,
                "activated_by": self.manager_halted_by,
                "reason": self.manager_reason,
            },
            "reason": self.reason,
        }


def halt_state(site_id: str, rows: Sequence) -> HaltState:
    """Fold the persisted rows into one answer for one site. Pure."""
    site_row = next(
        (
            r for r in rows
            if r.scope == SCOPE_SITE and r.site_id == site_id and r.active
        ),
        None,
    )
    manager_row = next(
        (r for r in rows if r.scope == SCOPE_SITE_MANAGER and r.active), None
    )
    tenant_row = next(
        (r for r in rows if r.scope == SCOPE_TENANT and r.active), None
    )
    return HaltState(
        site_id=site_id,
        tenant_halted=tenant_row is not None,
        tenant_halted_by=getattr(tenant_row, "activated_by", "") or "",
        tenant_reason=getattr(tenant_row, "reason", "") or "",
        site_halted=site_row is not None,
        site_halted_by=getattr(site_row, "activated_by", "") or "",
        site_reason=getattr(site_row, "reason", "") or "",
        manager_halted=manager_row is not None,
        manager_halted_by=getattr(manager_row, "activated_by", "") or "",
        manager_reason=getattr(manager_row, "reason", "") or "",
    )


# ---------------------------------------------------------------------------
# The governed execution decision
# ---------------------------------------------------------------------------


#: The ten inputs, in the order they are evaluated. Cheapest and most
#: absolute first, so a halted site never pays for a blast-radius walk.
DECISION_INPUTS = (
    "tenant_stop",
    "site_stop",
    "manager_halt",
    "agent_scope",
    "permission",
    "capability",
    "autonomy",
    "lease",
    "preconditions",
    "blast_radius",
)


@dataclass(frozen=True)
class ExecutionDecision:
    permitted: bool
    refused_by: str = ""
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "permitted": self.permitted,
            "refused_by": self.refused_by,
            "reason": self.reason,
            "inputs_considered": list(DECISION_INPUTS),
        }


#: A4 (spec A21.6): WHICH STAGE OWNS WHICH INPUT.
#:
#: `execution_permitted` is one model evaluated in three places, because
#: no single place can see all ten inputs: RBAC is decided at Central
#: Command, the site's own safety and the device's declared capability at
#: the Site Manager, and the lease, preconditions and blast radius at the
#: node that holds them.
#:
#: Splitting it is the honest shape. The alternative -- having the Site
#: Manager pass True for inputs it cannot see -- would assert that a
#: precondition passed when nobody checked it, which is exactly the
#: failure the fail-closed default exists to prevent.
#:
#: A test asserts these three partition DECISION_INPUTS exactly, so an
#: input can never be dropped by the split or evaluated by nobody.
CC_INPUTS = ("permission",)
SM_DISPATCH_INPUTS = (
    "tenant_stop", "site_stop", "manager_halt",
    "agent_scope", "capability", "autonomy",
)
NODE_INPUTS = ("lease", "preconditions", "blast_radius")


def execution_permitted(required=DECISION_INPUTS, **inputs) -> ExecutionDecision:
    """May this action run, considering every governing input?

    Each keyword is either ``True``/``None`` (this input does not
    object) or a string (this input refuses, and the string says why).
    Anything missing is treated as **not yet evaluated and therefore
    refusing** -- an input nobody supplied must never read as consent.

    That default is the whole point. The failure this guards against is
    an autonomy level being read as permission to act: autonomy is one
    input, evaluated seventh, and it can only ever fail to object.

    `required` names the inputs THIS STAGE owns (A21.6). It defaults to
    all ten, so the full-chain meaning is unchanged; a stage passes its
    own tuple and the inputs it does not own are evaluated by the stage
    that holds them. Narrowing `required` never weakens the rule inside a
    stage: an input the stage owns and did not supply still refuses.
    """
    for name in required:
        if name not in inputs:
            return ExecutionDecision(
                permitted=False,
                refused_by=name,
                reason=(
                    f"{name} was never evaluated; an unevaluated governing "
                    "input is a refusal, not a pass"
                ),
            )
        verdict = inputs[name]
        if verdict is True or verdict is None:
            continue
        if verdict is False:
            return ExecutionDecision(
                permitted=False, refused_by=name, reason=f"{name} refused"
            )
        return ExecutionDecision(
            permitted=False, refused_by=name, reason=str(verdict)
        )
    return ExecutionDecision(permitted=True)


class StopSwitchService:
    """Reads and writes the persisted halts."""

    def __init__(self, sessionmaker) -> None:
        self.sessionmaker = sessionmaker

    async def rows(self, session) -> Sequence:
        from sqlalchemy import select

        from harkeniq_sm.db.models import StopSwitchRow

        return (
            await session.execute(select(StopSwitchRow))
        ).scalars().all()

    async def state_for(self, session, site_id: str) -> HaltState:
        return halt_state(site_id, await self.rows(session))

    async def set_halt(
        self,
        session,
        *,
        scope: str,
        site_id: Optional[str],
        active: bool,
        actor: str,
        reason: str = "",
    ):
        """Activate or lift one halt. Idempotent by (scope, site_id)."""
        from sqlalchemy import select

        from harkeniq_sm.db.models import StopSwitchRow

        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {SCOPES}")
        if scope == SCOPE_SITE and not site_id:
            raise ValueError("a site halt needs a site")
        if scope in (SCOPE_SITE_MANAGER, SCOPE_TENANT) and site_id:
            raise ValueError(f"the {scope} halt is not site-scoped")

        stmt = select(StopSwitchRow).where(StopSwitchRow.scope == scope)
        stmt = stmt.where(
            StopSwitchRow.site_id.is_(None)
            if site_id is None
            else StopSwitchRow.site_id == site_id
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = StopSwitchRow(scope=scope, site_id=site_id)
            session.add(row)

        now = datetime.now(timezone.utc)
        row.active = active
        row.reason = reason
        if active:
            row.activated_by, row.activated_at = actor, now
        else:
            row.deactivated_by, row.deactivated_at = actor, now
        await session.flush()
        logger.warning(
            "%s halt %s by %s (site=%s)",
            scope, "ACTIVATED" if active else "lifted", actor, site_id or "-",
        )
        return row

    async def decision_inputs(self, session, site_id: str) -> dict:
        """The three halt inputs for :func:`execution_permitted`.

        Returned as the refusal STRINGS the decision expects, so a caller
        cannot accidentally pass a truthy halt as consent.
        """
        state = await self.state_for(session, site_id)
        return {
            "tenant_stop": (
                state.reason if state.tenant_halted else True
            ),
            "site_stop": state.reason if state.site_halted else True,
            "manager_halt": state.reason if state.manager_halted else True,
        }
