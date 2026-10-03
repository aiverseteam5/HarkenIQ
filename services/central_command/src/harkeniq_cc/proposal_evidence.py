"""S3-E2 (spec A30.39): historical proposal evidence, as one reader may read it.

A proposal's `evidence` and `rationale` ARE `decision_evidence_at_creation`:
what `govern_proposal` froze when it governed the proposal. Every writer
since A1 composed that record over the WHOLE TENANT -- the outcome
statistic, the attention rank and score, and the closing sentence that
restates the statistic -- so a stored proposal carries facts from a broader
estate than most of the people who will ever read it hold.

Two things with two names, never interchangeable:

* the CREATION record is immutable. Nothing here writes, and nothing here
  re-derives it; a narrowed grant, a moved device or a deleted site does not
  change what the decision saw;
* the VIEWER projection (`viewer_projected_evidence`) is computed at read
  time from the reader's CURRENT canonical reach. It is a presentation: not
  a second decision record, not an authorization token, audited as nothing.

This module is the pure half: the writer's grammar for the rationale, the
creation-record projections for a scoped human and for a machine, and the
reduced machine sentence. The reader's type and its one loader live in
`harkeniq_cc.governance`, beside `AutonomyView` and `LearningView`.

Nothing here may resolve scope, read a row or consult a clock.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from harkeniq_cc.autonomy import DENIED
from harkeniq_cc.learning_projection import (
    MARK_PROJECTION,
    MARK_WITHHELD,
    PROJECTION_SCOPED,
)

# ---------------------------------------------------------------------------
# Vocabulary (A30.39), closed
# ---------------------------------------------------------------------------

#: What every proposal's creation record was composed over (D2). Written
#: nowhere: every writer since A1 composed over the whole tenant, and a
#: structural test pins `govern_proposal` to that composition, so a future
#: change of basis fails the suite and arrives by amendment.
CREATION_BASIS_TENANT = "tenant"

#: `evidence_scope` (D4). Derived from the reader's reach SHAPE only -- never
#: from whether hidden sites actually contributed, which would be one bit
#: about the hidden estate.
SCOPE_FULLY_VISIBLE = "fully_visible"
SCOPE_BROADER = "broader_than_current_view"

#: `viewer_projected_evidence.basis` (D6).
VIEWER_BASIS = "current_reach"
#: The reader holds `fleet.view` nowhere: there is no view to count in, and
#: zeros would claim "no executions" (D6).
UNAVAILABLE_NO_REACH = "no_fleet_view_reach"

#: The machine plane's projection marker (D9), beside S4's `scoped`.
PROJECTION_MACHINE = "machine"

#: The facts about the target device and its condition (D3). The ALLOW-LIST:
#: a scoped human and a machine are handed these and nothing else of the
#: creation record -- a key the writer might add later is withheld until it
#: is named here.
CREATION_CONDITION_KEYS: tuple[str, ...] = (
    "observed",
    "condition_kind",
    "subsystem",
    "incident_ids",
    "has_diagnosis",
    "remediation_provenance",
    "component",
    "components_reported",
    "device",
    "contract_version",
    "evaluated_at",
)

#: Composed over a broader estate than a scoped reader holds, and withheld
#: whole from one (D3): present as ``None`` whether or not the stored row
#: carries them, so the shape never varies.
CREATION_WITHHELD_KEYS: tuple[str, ...] = ("attention", "outcome_evidence")

#: Withheld from EVERY machine, tenant-scoped ones included (D9): machines
#: consume governed conclusions, not the evidence a decision was built from.
MACHINE_WITHHELD_KEYS: tuple[str, ...] = (
    "attention", "learned_signals", "outcome_evidence",
)

#: What replaces the track-record sentence for a scoped reader (D5). It
#: states the METHOD -- the record was composed over the whole tenant --
#: and nothing about its content.
WITHHELD_TRACK_RECORD = (
    " The track record this proposal was created with was composed across "
    "the whole tenant, which is broader than your current scope, so it is "
    "not shown here."
)


# ---------------------------------------------------------------------------
# The writer's grammar (D2): ONE sentence, two halves
# ---------------------------------------------------------------------------


def rationale_head(agent_name: str, device, condition: dict, candidate: dict) -> str:
    """What the agent saw and what it recommends.

    Takes NO evidence argument, by construction: nothing in the head can be
    a count, a rate or a site, because nothing that carries one reaches this
    function. That is what makes stripping the clause below a complete
    reduction (A30.28's text rule: reduce through the writer's grammar).
    """
    device_label = getattr(device, "agent_name", "") or device.agent_id
    detail = condition["detail"]
    # Incident titles already name their device; repeating it produced
    # "observed X: fan CRITICAL on X" on the live stack.
    where = "" if device_label and device_label in detail else f" on {device_label}"
    return (
        f"{agent_name} observed {detail}{where} and recommends "
        f"{candidate['action_type'].replace('_', ' ').lower()}: "
        f"{candidate['because']}."
    )


def track_record_clause(outcome_evidence) -> str:
    """The sentence restating the creation-time outcome statistic.

    The ONLY evidence-derived text in a rationale, and the same function
    wrote every stored sentence since A1 -- which is why a projection can
    re-render it from the stored `outcome_evidence` and remove exactly it.
    ``None`` reads as no evidence, exactly as the writer read it.
    """
    ev = outcome_evidence or {}
    rate = ev.get("success_rate")
    if rate is not None:
        return (
            f" This class has succeeded {rate:.0%} of the time across "
            f"{ev.get('executions', 0)} executions in this tenant."
        )
    if ev.get("executions"):
        return (
            f" The tenant has only {ev['executions']} recorded execution(s) "
            f"of this class, too few to judge a success rate."
        )
    return " This tenant has no recorded outcome for this class yet."


# ---------------------------------------------------------------------------
# The creation record, as a scoped human reads it (D3, D5)
# ---------------------------------------------------------------------------


def withheld_rationale(action_type: str, device_agent_id: str) -> str:
    """The fail-closed rationale: built from the action class and the target
    device id and nothing else (D5)."""
    label = str(action_type or "").replace("_", " ").lower() or "an action"
    return (
        f"Proposes {label} on {device_agent_id}. The recorded rationale "
        "draws on creation evidence outside your current scope and is not "
        "shown here."
    )


def scoped_creation_evidence(stored, learned_signals) -> dict:
    """A stored `evidence` for a reader whose reach is not the tenant.

    ALLOW-LIST, never deny-list: the condition facts pass, `learned_signals`
    arrives already projected by S3/S4 (the caller's `AutonomyView`), the
    two broader-estate blocks are ``None`` whatever was stored, and every
    other stored key is withheld and named.
    """
    source = stored if isinstance(stored, Mapping) else {}
    out: dict[str, Any] = {
        key: source[key] for key in CREATION_CONDITION_KEYS if key in source
    }
    withheld = set(CREATION_WITHHELD_KEYS)
    withheld.update(
        key for key in source
        if key not in CREATION_CONDITION_KEYS
        and key not in CREATION_WITHHELD_KEYS
        and key != "learned_signals"
    )
    if "learned_signals" in source:
        out["learned_signals"] = learned_signals
    for key in CREATION_WITHHELD_KEYS:
        out[key] = None
    out[MARK_PROJECTION] = PROJECTION_SCOPED
    out[MARK_WITHHELD] = sorted(withheld)
    return out


def scoped_rationale(stored, evidence, *, action_type: str,
                     device_agent_id: str) -> str:
    """A stored rationale for a reader whose reach is not the tenant (D5).

    The writer's own clause is re-rendered from the stored outcome evidence
    and, when the stored sentence ends with EXACTLY it, replaced by one
    constant sentence. Anything else -- a sentence that does not end with
    the clause, evidence that is missing or not a mapping, a statistic the
    clause cannot render -- fails closed to a sentence that names only the
    action class and the target.
    """
    text = stored if isinstance(stored, str) else ""
    fallback = withheld_rationale(action_type, device_agent_id)
    if not text or not isinstance(evidence, Mapping):
        return fallback
    outcome = evidence.get("outcome_evidence")
    if outcome is not None and not isinstance(outcome, Mapping):
        return fallback
    try:
        clause = track_record_clause(outcome)
    except (TypeError, ValueError, KeyError):
        return fallback
    if not clause or not text.endswith(clause):
        return fallback
    head = text[: -len(clause)]
    if not head:
        return fallback
    return head + WITHHELD_TRACK_RECORD


# ---------------------------------------------------------------------------
# The machine dry-run (D9): bounded conclusions only, for EVERY machine
# ---------------------------------------------------------------------------


def machine_dry_run_evidence(stored) -> dict:
    """What a machine's dry-run carries of the evidence a proposal WOULD
    freeze: the condition facts, by allow-list, and nothing that was built
    from outcomes, rank or learning -- whoever the machine is."""
    source = stored if isinstance(stored, Mapping) else {}
    out: dict[str, Any] = {
        key: source[key] for key in CREATION_CONDITION_KEYS if key in source
    }
    withheld = set(MACHINE_WITHHELD_KEYS)
    withheld.update(key for key in source if key not in CREATION_CONDITION_KEYS)
    for key in MACHINE_WITHHELD_KEYS:
        out[key] = None
    out[MARK_PROJECTION] = PROJECTION_MACHINE
    out[MARK_WITHHELD] = sorted(withheld)
    return out


#: Closed labels for the condition kinds the evaluator produces.
_KIND_LABEL = {
    "incident": "an open incident",
    "unreachable": "an unreachable controller",
}

#: A catalogue subsystem is interpolated only when it is a bounded code.
#: Anything else -- free text an operator typed, a very long key -- is
#: replaced by constant text rather than quoted.
_SUBSYSTEM_CODE = re.compile(r"^[a-z0-9][a-z0-9_.:*-]{0,63}$")

#: The one constant every machine rationale ends with.
MACHINE_RATIONALE_NOTE = (
    "Outcome statistics, attention scoring and learned signals are not part "
    "of the machine contract; this conclusion is information, not authority."
)


def machine_rationale(*, action_type: str, device_agent_id: str,
                      condition_kind: str, subsystem: str,
                      requires_human: bool, disposition: str) -> str:
    """The reduced machine rationale (D9), rendered from TYPED facts only.

    The action class, the target device id, the condition kind (a closed
    label), the catalogue subsystem (only as a bounded code), the current
    conclusion (from `requires_human` and the disposition) and constant
    text. Never the incident title, the catalogue's free-text `because`, a
    stored sentence, a count, a rate or another device.
    """
    label = str(action_type or "").replace("_", " ").lower() or "an action"
    kind = _KIND_LABEL.get(str(condition_kind or ""), "an observed condition")
    code = str(subsystem or "")
    where = (
        f" in the {code} subsystem" if _SUBSYSTEM_CODE.fullmatch(code) else ""
    )
    if disposition == DENIED:
        conclusion = "Governance denies it at present."
    elif requires_human:
        conclusion = "A human must approve it before anything runs."
    else:
        conclusion = (
            "It is within this agent's unattended grant; execution still "
            "passes every dispatch gate and the node's own funnel."
        )
    return (
        f"Recommends {label} on {device_agent_id} for {kind}{where}. "
        f"{conclusion} {MACHINE_RATIONALE_NOTE}"
    )


# ---------------------------------------------------------------------------
# The viewer projection (D6)
# ---------------------------------------------------------------------------


def viewer_block(outcome_evidence: Optional[dict], *, as_of: str,
                 reach_empty: bool) -> dict:
    """`viewer_projected_evidence`: the reader's CURRENT track record.

    `outcome_evidence` was computed from exactly the rows the reader's
    current canonical reach reads. A reader who holds `fleet.view` nowhere
    gets ``None`` and the reason -- never zeros, which would say "no
    executions" about a view that does not exist.
    """
    if reach_empty:
        return {
            "basis": VIEWER_BASIS, "as_of": as_of,
            "outcome_evidence": None,
            "unavailable_reason": UNAVAILABLE_NO_REACH,
        }
    return {
        "basis": VIEWER_BASIS, "as_of": as_of,
        "outcome_evidence": dict(outcome_evidence or {}),
        "unavailable_reason": None,
    }
