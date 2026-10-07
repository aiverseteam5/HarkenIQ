"""Predictive maintenance: per-device failure risk scoring (R4-3 P20).

Deterministic, explainable scoring over accumulated outcome history --
NOT a trained ML model. The amendment is explicit that real predictive
models need 6+ months of fleet outcome data; this module is the
infrastructure that makes that data actionable now and gives a trained
model a drop-in seat later.

Score composition (all factors reported back to the caller):
  - recency-weighted device failure rate: each outcome is weighted by
    0.5 ** (age_days / half_life). Recent failures matter; a failure
    from last quarter mostly doesn't.
  - cohort prior: the (vendor, model) fleet failure rate. Devices with
    too little history fall back to their cohort instead of pretending
    certainty.
  - current health state: a device sitting at critical/warning health
    is riskier than its history alone says.
  - warranty status: expired-warranty devices get a nudge -- aging
    hardware out of vendor coverage.

A device below MIN_DEVICE_SAMPLES with no cohort data is explicitly
"insufficient_data", never a fabricated score.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("harkeniq.cc.predictive")

DECAY_HALF_LIFE_DAYS = 30.0
MIN_DEVICE_SAMPLES = 5
RISK_HIGH = 0.6
RISK_MEDIUM = 0.3
HEALTH_BUMP = {"critical": 0.20, "warning": 0.10}
WARRANTY_EXPIRED_BUMP = 0.10
_FAILURE_OUTCOMES = ("FAILURE", "ROLLBACK")
#: What counts as a failed outcome for risk scoring -- public so the
#: database aggregates (A30.41) and the Python sums name ONE set.
FAILURE_OUTCOMES = _FAILURE_OUTCOMES


@dataclass
class DeviceRisk:
    """Failure-risk assessment for one device."""

    agent_id: str
    vendor: str = ""
    model: str = ""
    risk_score: float = 0.0
    band: str = "low"  # low | medium | high | insufficient_data
    sample_count: int = 0
    factors: dict = field(default_factory=dict)
    # S2 (2026-08-29): device identity beyond the agent hash. Without site
    # attribution a risk row cannot be placed on a site, which blocks both
    # the fleet -> site -> device drill-down AND any site-scoped consumer:
    # an agent bounded to one site could not tell which rows were its own.
    # Populated by the caller from the fleet cache; the scoring model
    # itself is unchanged and does not read these.
    site_id: str = ""
    agent_name: str = ""

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "site_id": self.site_id,
            "vendor": self.vendor,
            "model": self.model,
            "risk_score": round(self.risk_score, 4),
            "band": self.band,
            "sample_count": self.sample_count,
            "factors": self.factors,
        }


def _age_days(recorded_at: Optional[datetime], now: datetime) -> float:
    if recorded_at is None:
        return 0.0
    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=timezone.utc)
    return max(0.0, (now - recorded_at).total_seconds() / 86400.0)


def decay_weight(
    recorded_at: Optional[datetime],
    now: datetime,
    half_life_days: float = DECAY_HALF_LIFE_DAYS,
) -> float:
    """The weight of ONE outcome: 0.5 ** (age_days / half_life).

    The one definition. `weighted_failure_rate` sums it over rows, and A30.41
    computes the same sums in SQL: PostgreSQL mirrors this float pipeline
    (`db.outcome_sql.decay_weight`), and SQLite calls this function itself.
    """
    return 0.5 ** (_age_days(recorded_at, now) / half_life_days)


def weighted_failure_rate(
    outcomes: list[dict],
    now: Optional[datetime] = None,
    half_life_days: float = DECAY_HALF_LIFE_DAYS,
) -> tuple[float, float]:
    """Recency-weighted failure rate over outcome dicts.

    Each dict carries "outcome" and "recorded_at" (datetime | None).
    Returns (rate, total_weight); rate is 0.0 when there is no weight.
    """
    current = now or datetime.now(timezone.utc)
    total = 0.0
    failed = 0.0
    for oc in outcomes:
        weight = decay_weight(oc.get("recorded_at"), current, half_life_days)
        total += weight
        if oc.get("outcome") in _FAILURE_OUTCOMES:
            failed += weight
    if total <= 0.0:
        return 0.0, 0.0
    return failed / total, total


@dataclass(frozen=True)
class DeviceHistory:
    """A device's outcome history as its sufficient statistics (A30.41).

    `count` rows, and the two recency-weighted sums `weighted_failure_rate`
    would have produced over them -- computed by the database over EVERY
    row the reader may read, where the rows themselves were once read under
    a 50,000-row window that kept the oldest and dropped the newest.
    """

    count: int
    weighted_total: float
    weighted_failed: float

    def rate(self) -> float:
        """`weighted_failure_rate`'s rate: failed / total, 0.0 with no weight."""
        if self.weighted_total <= 0.0:
            return 0.0
        return self.weighted_failed / self.weighted_total


@dataclass(frozen=True)
class DeviceOutcomeStats:
    """Everything a principal's predictive answer reads from outcome history
    (A30.41), computed by the database over EVERY row the reader may read.

    `devices` maps a device to its `DeviceHistory`; `cohort_tallies` are
    (vendor, model, outcome, count) rows for `cohort_failure_rates` (empty
    for a machine, which is never scored on a cohort); `total` is the number
    of rows read -- `outcomes_considered`.
    """

    devices: dict
    cohort_tallies: tuple
    total: int


def band_for(score: float) -> str:
    if score >= RISK_HIGH:
        return "high"
    if score >= RISK_MEDIUM:
        return "medium"
    return "low"


def score_device(
    agent_id: str,
    outcomes: list[dict] = (),
    cohort_failure_rate: Optional[float] = None,
    health: str = "",
    warranty_status: str = "",
    vendor: str = "",
    model: str = "",
    now: Optional[datetime] = None,
    history: Optional[DeviceHistory] = None,
) -> DeviceRisk:
    """Score one device. See module docstring for the composition.

    `outcomes` is the device's rows (the internal decision paths, which keep
    their window until Phase 3). `history` is the same device's sufficient
    statistics, read by the database over every row (A30.41): given, it
    decides the sample count and the recency-weighted rate, and the rest of
    the composition is shared, line for line.
    """
    if history is not None:
        sample_count = history.count
    else:
        sample_count = len(outcomes)
    factors: dict = {}

    if sample_count >= MIN_DEVICE_SAMPLES:
        if history is not None:
            base = history.rate()
        else:
            base, _ = weighted_failure_rate(outcomes, now=now)
        factors["weighted_failure_rate"] = round(base, 4)
        factors["basis"] = "device_history"
    elif cohort_failure_rate is not None:
        base = cohort_failure_rate
        factors["cohort_failure_rate"] = round(base, 4)
        factors["basis"] = "cohort_prior"
    else:
        return DeviceRisk(
            agent_id=agent_id, vendor=vendor, model=model,
            risk_score=0.0, band="insufficient_data",
            sample_count=sample_count,
            factors={"basis": "insufficient_data",
                     "min_samples": MIN_DEVICE_SAMPLES},
        )

    score = base
    health_bump = HEALTH_BUMP.get((health or "").lower(), 0.0)
    if health_bump:
        score += health_bump
        factors["health_bump"] = health_bump
        factors["health"] = health
    if (warranty_status or "").lower() == "expired":
        score += WARRANTY_EXPIRED_BUMP
        factors["warranty_expired_bump"] = WARRANTY_EXPIRED_BUMP

    score = max(0.0, min(1.0, score))
    return DeviceRisk(
        agent_id=agent_id, vendor=vendor, model=model,
        risk_score=score, band=band_for(score),
        sample_count=sample_count, factors=factors,
    )


def cohort_failure_rates(outcomes: list[dict]) -> dict[tuple[str, str], float]:
    """Plain (unweighted) failure rate per (vendor, model) cohort.

    A row may carry `count` (A30.41: a database tally of that many identical
    rows); it weighs the row, and a raw row weighs one. The integer sums are
    the sums over the expanded rows.
    """
    totals: dict[tuple[str, str], int] = {}
    failures: dict[tuple[str, str], int] = {}
    for oc in outcomes:
        key = (oc.get("vendor", ""), oc.get("model", ""))
        n = int(oc.get("count", 1))
        totals[key] = totals.get(key, 0) + n
        if oc.get("outcome") in _FAILURE_OUTCOMES:
            failures[key] = failures.get(key, 0) + n
    return {
        key: failures.get(key, 0) / total
        for key, total in totals.items() if total > 0
    }
