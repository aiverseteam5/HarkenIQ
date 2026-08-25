"""Skill evaluation engine (Doc 07 §4).

Evaluates all rules of a skill against one normalized sensor reading and
produces a single Verdict. Highest severity wins; debounce is populated
downstream (Doc 13), not applied here.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from harkeniq.models import (
    Evidence,
    SkillDefinition,
    SkillRule,
    Verdict,
    VerdictSeverity,
)
from harkeniq.skills.expression import evaluate

logger = logging.getLogger("harkeniq.skills.engine")


class _SafeDict(dict):
    """Leave unknown {placeholders} intact during message formatting."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def build_context(sensor: Any, baseline: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Build the evaluation context dict from a normalized sensor reading.

    Accepts a dataclass (NormalizedFan, NormalizedDisk, ...) or a plain dict.
    Baseline fields (baseline_mean, baseline_stddev, deviation) are merged in
    when provided (Doc 07 §3.4: available when confidence >= 0.5).
    """
    if dataclasses.is_dataclass(sensor) and not isinstance(sensor, type):
        context = dataclasses.asdict(sensor)
    elif isinstance(sensor, dict):
        context = dict(sensor)
    else:
        raise TypeError(f"sensor must be a dataclass or dict, got {type(sensor).__name__}")

    if baseline:
        context.update(baseline)
    return context


def format_message(template: str, context: dict[str, Any]) -> str:
    """Substitute {field} placeholders from the context (Doc 07 §2)."""
    return template.format_map(_SafeDict(context))


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _evidence_fields(context: dict[str, Any]) -> dict[str, Any]:
    """Snapshot of scalar sensor fields for evidence (Doc 07 §4.4)."""
    return {
        k: v for k, v in context.items()
        if v is not None and not isinstance(v, (dict, list))
    }


def evaluate_skill(
    skill: SkillDefinition,
    sensor: Any,
    baseline: Optional[dict[str, Any]] = None,
    timestamp: Optional[str] = None,
) -> Verdict:
    """Evaluate one skill against one sensor reading; return a single Verdict.

    Doc 07 §4.1:
    1. All rules are evaluated in order (no short-circuit across rules).
    2. All matching rules produce evidence.
    3. Highest severity wins (CRITICAL > WARNING > TRENDING > HEALTHY > UNKNOWN).
    4. No match: default_verdict applies.
    """
    context = build_context(sensor, baseline)
    ts = timestamp or _utc_now()
    sensor_name = context.get("name", "")
    sensor_id = f"{skill.target}:{sensor_name}"

    matched: list[tuple[int, SkillRule]] = []
    for index, rule in enumerate(skill.rules):
        try:
            if evaluate(rule.parsed_ast, context):
                matched.append((index, rule))
        except Exception:
            logger.exception(
                "Rule %d of skill %s failed evaluating %s", index, skill.name, sensor_id
            )

    if not matched:
        return Verdict(
            sensor_id=sensor_id,
            skill_name=skill.name,
            severity=skill.default_verdict,
            message=f"{sensor_name}: {skill.default_verdict.value}",
            evidence=[],
            timestamp=ts,
        )

    evidence = [
        Evidence(
            sensor_id=sensor_id,
            skill_name=skill.name,
            rule_index=index,
            condition=rule.condition,
            fields=_evidence_fields(context),
            timestamp=ts,
            baseline_confidence=(baseline or {}).get("baseline_confidence", 0.0),
        )
        for index, rule in matched
    ]

    # Highest severity wins; ties broken by rule order (first match)
    _, winner = max(matched, key=lambda pair: (pair[1].verdict, -pair[0]))

    return Verdict(
        sensor_id=sensor_id,
        skill_name=skill.name,
        severity=winner.verdict,
        message=format_message(winner.message_template, context),
        evidence=evidence,
        timestamp=ts,
    )


def winning_rule(skill: SkillDefinition, verdict: Verdict) -> Optional[SkillRule]:
    """Return the rule that produced a verdict's severity (for action lookup).

    The winning rule is the first evidence entry whose rule verdict matches
    the final severity.
    """
    for ev in verdict.evidence:
        rule = skill.rules[ev.rule_index]
        if rule.verdict == verdict.severity:
            return rule
    return None


# ---------------------------------------------------------------------------
# SkillEngine coordinator (Doc 10 §2.8, Doc 13 §4.1)
# ---------------------------------------------------------------------------

# Map skill target -> NormalizedDevice collection attribute
_TARGET_COLLECTIONS = {
    "fan": "fans",
    "disk": "disks",
    "memory": "memory",
    "psu": "psus",
    "thermal": "thermals",
    "interface": "interfaces",  # R6
}

# BMC health pass-through mapping used in learning mode (Doc 13 §2.3)
_HEALTH_TO_SEVERITY = {
    "OK": VerdictSeverity.HEALTHY,
    "Warning": VerdictSeverity.WARNING,
    "Critical": VerdictSeverity.CRITICAL,
}


class SkillEngine:
    """Skill evaluation coordinator (Doc 10 §2.8).

    Runs the per-poll pipeline from Doc 13 §4.1 for every (skill, sensor)
    pair: baseline update -> skill evaluation -> trending -> debounce.

    Confidence gating (Doc 13 §2.3):
      - < 0.5: learning mode — BMC health pass-through only
      - 0.5–0.99: expression evaluation enabled, trending disabled
      - 1.0: full evaluation + trending
    """

    def __init__(
        self,
        skills: list[SkillDefinition],
        debounce_config: Optional[dict] = None,
        trending_engine: Optional["TrendingEngine"] = None,
        time_fn=None,
    ) -> None:
        from harkeniq.skills.debounce import Debouncer
        from harkeniq.skills.trending import TrendingEngine

        self._skills: dict[str, SkillDefinition] = {s.name: s for s in skills}
        self.debouncer = Debouncer(debounce_config)
        self.trending = trending_engine or TrendingEngine()
        self._pending_actions: list["Action"] = []
        # QA-008: injectable clock. The demo compresses time; trending
        # slopes are per-hour, so evaluating on the wall clock under
        # compression produced -5,402,706 RPM/hr headlines. The demo
        # injects a narrative clock; production uses time.time.
        self._time_fn = time_fn

    async def evaluate(
        self,
        device: Any,
        timestamp: Optional[float] = None,
    ) -> list[Verdict]:
        """Evaluate all skills against all matching sensors on the device."""
        import time as _time

        if timestamp is not None:
            ts_unix = timestamp
        elif self._time_fn is not None:
            ts_unix = self._time_fn()
        else:
            ts_unix = _time.time()
        ts_iso = datetime.fromtimestamp(ts_unix, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self._pending_actions = []
        verdicts: list[Verdict] = []

        for skill in self._skills.values():
            sensors = getattr(device, _TARGET_COLLECTIONS[skill.target], [])
            metric_field = skill.trending[0].field if skill.trending else None
            for sensor in sensors:
                # Unpopulated DIMM slots are not faults (Doc 08 §4.8); an
                # Absent PSU/fan IS a fault and must be evaluated.
                if skill.target == "memory" and getattr(sensor, "state", "") == "Absent":
                    continue
                verdicts.extend(
                    self._evaluate_sensor(skill, sensor, metric_field, ts_unix, ts_iso)
                )
        return verdicts

    def _evaluate_sensor(
        self,
        skill: SkillDefinition,
        sensor: Any,
        metric_field: Optional[str],
        ts_unix: float,
        ts_iso: str,
    ) -> list[Verdict]:
        sensor_id = f"{skill.target}:{sensor.name}"
        health = getattr(sensor, "health", "Unknown")

        # 1. Baseline update (Doc 13 §4.1 step 1)
        value = getattr(sensor, metric_field, None) if metric_field else None
        if value is not None:
            self.trending.update_baseline(sensor_id, float(value), ts_unix, health)
        confidence = self.trending.confidence(sensor_id)

        # 2. Skill evaluation with confidence gating (Doc 13 §2.3)
        if confidence < 0.5:
            raw_severity = _HEALTH_TO_SEVERITY.get(health, VerdictSeverity.UNKNOWN)
            verdict = Verdict(
                sensor_id=sensor_id,
                skill_name=skill.name,
                severity=raw_severity,
                message=(
                    f"{sensor.name}: BMC health {health} "
                    f"(baseline learning, confidence {confidence:.2f})"
                ),
                evidence=[Evidence(
                    sensor_id=sensor_id,
                    skill_name=skill.name,
                    rule_index=-1,  # pass-through, no rule involved
                    condition="",
                    fields={"health": health},
                    timestamp=ts_iso,
                    baseline_confidence=confidence,
                )],
                timestamp=ts_iso,
            )
            winner = None
            context = build_context(sensor)
        else:
            baseline_fields = self._baseline_fields(sensor_id, value, confidence)
            verdict = evaluate_skill(skill, sensor, baseline_fields, ts_iso)
            winner = winning_rule(skill, verdict)
            context = build_context(sensor, baseline_fields)

        results: list[Verdict] = []

        # 3. Trending — only at full confidence; never debounced (Doc 13 §4.3)
        if confidence >= 1.0 and skill.trending:
            for trend in self.trending.compute_trend(sensor_id, skill.trending, context):
                results.append(Verdict(
                    sensor_id=sensor_id,
                    skill_name=skill.name,
                    severity=VerdictSeverity.TRENDING,
                    message=trend.message,
                    evidence=[Evidence(
                        sensor_id=sensor_id,
                        skill_name=skill.name,
                        rule_index=-1,
                        condition=f"trending:{trend.field}:{trend.direction}",
                        fields={
                            "slope": trend.slope,
                            "r_squared": trend.r_squared,
                            "current_value": trend.current_value,
                            "threshold_value": trend.threshold_value,
                            "time_to_threshold_hours": trend.time_to_threshold_hours,
                        },
                        timestamp=ts_iso,
                        baseline_confidence=confidence,
                    )],
                    timestamp=ts_iso,
                ))

        # 4. Debounce threshold verdict (Doc 13 §4.1 step 4)
        override = winner.debounce if winner else None
        debounced, dstate = self.debouncer.apply(
            sensor_id, skill.name, verdict.severity, override
        )
        if debounced == verdict.severity:
            verdict.debounce_state = dstate
            final = verdict
        else:
            final = Verdict(
                sensor_id=sensor_id,
                skill_name=skill.name,
                severity=debounced,
                message=(
                    f"{sensor.name}: {debounced.value} "
                    f"(debounce holding; raw {verdict.severity.value})"
                ),
                evidence=verdict.evidence,
                timestamp=ts_iso,
                debounce_state=dstate,
            )
        results.append(final)

        # 5. Action recommendation — only once debounce confirms (Doc 07 §5)
        if (
            winner is not None
            and winner.action is not None
            and debounced == verdict.severity
            and debounced in (VerdictSeverity.WARNING, VerdictSeverity.CRITICAL)
        ):
            self._pending_actions.append(self._build_action(
                skill, sensor_id, winner, debounced, context, ts_iso
            ))

        return results

    def _baseline_fields(
        self,
        sensor_id: str,
        value: Optional[float],
        confidence: float,
    ) -> Optional[dict[str, Any]]:
        """Baseline-derived expression fields (Doc 13 §4.2), conf >= 0.5 only."""
        baseline = self.trending.get_baseline(sensor_id)
        if baseline is None:
            return None
        if value is None or baseline.stddev == 0:
            deviation = 0.0  # Doc 13 §5.1: constant baseline => zero deviation
        else:
            deviation = (float(value) - baseline.mean) / baseline.stddev
        return {
            "baseline_mean": baseline.mean,
            "baseline_stddev": baseline.stddev,
            "deviation": deviation,
            "baseline_confidence": confidence,
        }

    def _build_action(
        self,
        skill: SkillDefinition,
        sensor_id: str,
        rule: SkillRule,
        severity: VerdictSeverity,
        context: dict[str, Any],
        ts_iso: str,
    ) -> "Action":
        import uuid

        from harkeniq.models import Action, ActionStatus

        return Action(
            id=uuid.uuid4().hex,
            type=rule.action.type,
            params={
                key: format_message(str(val), context)
                for key, val in rule.action.params.items()
            },
            status=ActionStatus.PENDING,
            sensor_id=sensor_id,
            skill_name=skill.name,
            verdict_severity=severity,
            proposed_at=ts_iso,
        )

    def get_pending_actions(self) -> list["Action"]:
        """Actions proposed during the latest evaluate() awaiting approval."""
        return list(self._pending_actions)

    def reload_skills(self, skills: list[SkillDefinition]) -> None:
        """Hot-reload skills (SIGHUP); drops debounce state of removed skills."""
        self._skills = {s.name: s for s in skills}
        self.debouncer.reload_skills(set(self._skills))
        logger.info("Skills reloaded: %d skills active", len(self._skills))
