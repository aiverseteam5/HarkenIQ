"""Skill marketplace domain logic (R4-3 P17, OQ-22).

Trust model:
  - community: user-submitted, schema-validated, human-reviewed before
    publication. Runs on subscriber fleets only after explicit install.
  - verified: a community skill promoted after proving itself in the
    field -- >= 95% success rate over >= 50 executions on >= 50 devices.
    The thresholds align with the R3b-3 learning-feedback gate
    (PROMOTION_SUCCESS_RATE / PROMOTION_MIN_DEVICES).
  - core: HarkenIQ-authored, seeded by the platform; not submittable.

Validation reuses the agent's own skill parser (harkeniq.skills.loader
.parse_skill) -- the schema/DSL whitelist is the actual safety boundary
for untrusted YAML: unknown fields, unknown action types, and malformed
expressions are rejected before a reviewer ever sees the entry.
NOTE: a zero-execution skill must never pass the gate -- SkillOutcome
Stats.success_rate defaults to 1.0 on no data, so the gate checks raw
counts, never that property.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import yaml

PROMOTION_SUCCESS_RATE = 0.95
PROMOTION_MIN_EXECUTIONS = 50
PROMOTION_MIN_DEVICES = 50

DANGEROUS_ACTIONS = ("POWER_CYCLE", "BMC_RESET", "FIRMWARE_UPDATE")


@dataclass
class SubmissionValidation:
    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skill_name: str = ""
    target: str = ""
    version: int = 1
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "errors": self.errors,
            "warnings": self.warnings,
            "skill_name": self.skill_name,
            "target": self.target,
            "version": self.version,
        }


def validate_submission(yaml_text: str) -> SubmissionValidation:
    """Static validation of a community skill submission."""
    from harkeniq.errors import SkillError
    from harkeniq.skills.loader import parse_skill

    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        return SubmissionValidation(passed=False, errors=[f"Invalid YAML: {e}"])
    if not isinstance(data, dict):
        return SubmissionValidation(
            passed=False, errors=["Skill must be a YAML mapping"]
        )
    try:
        skill = parse_skill(data)
    except SkillError as e:
        return SubmissionValidation(passed=False, errors=[str(e)])

    warnings: list[str] = []
    for rule in skill.rules:
        if rule.action and rule.action.type.value in DANGEROUS_ACTIONS:
            warnings.append(
                f"Rule proposes disruptive action {rule.action.type.value}; "
                "review with extra scrutiny"
            )
    return SubmissionValidation(
        passed=True,
        warnings=warnings,
        skill_name=skill.name,
        target=skill.target,
        version=skill.version,
        description=skill.description or "",
    )


@dataclass
class PromotionCheck:
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    success_rate: float = 0.0

    def to_dict(self) -> dict:
        return {
            "eligible": self.eligible,
            "reasons": self.reasons,
            "success_rate": round(self.success_rate, 4),
        }


def check_promotion(
    total_executions: int,
    success_count: int,
    device_count: int,
) -> PromotionCheck:
    """Community -> verified gate (OQ-22). Counts, never trust ratios
    computed elsewhere -- zero executions must fail."""
    reasons: list[str] = []
    if total_executions < PROMOTION_MIN_EXECUTIONS:
        reasons.append(
            f"needs >= {PROMOTION_MIN_EXECUTIONS} executions "
            f"(has {total_executions})"
        )
    if device_count < PROMOTION_MIN_DEVICES:
        reasons.append(
            f"needs >= {PROMOTION_MIN_DEVICES} devices (has {device_count})"
        )
    rate = (success_count / total_executions) if total_executions else 0.0
    if total_executions and rate < PROMOTION_SUCCESS_RATE:
        reasons.append(
            f"success rate {rate:.1%} below "
            f"{PROMOTION_SUCCESS_RATE:.0%} threshold"
        )
    return PromotionCheck(
        eligible=not reasons, reasons=reasons, success_rate=rate
    )
