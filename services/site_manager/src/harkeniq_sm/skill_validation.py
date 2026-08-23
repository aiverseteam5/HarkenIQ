"""Skill validation pipeline (R3b-1 C6, Platform-Design §Skill Validation).

Validates candidate skills before distribution to agents.  R3b-1 ships
two validation stages:
  1. Static analysis: YAML schema, field validation, safety checks
  2. Dry-run: condition matching against historical device state (no execution)

R3b-2+ adds: sandbox execution, canary deployment, graduated rollout.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from harkeniq.autonomy.skill_lifecycle import SkillPackage, ValidationState

logger = logging.getLogger("harkeniq.sm.skill_validation")


@dataclass
class ValidationResult:
    """Result of validating a candidate skill."""

    passed: bool
    stage: str  # "static_analysis" | "dry_run" | "sandbox" | "canary"
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dry_run_matches: int = 0  # how many historical states matched the skill


class SkillValidator:
    """Validates candidate skills through the validation pipeline.

    Each stage must pass before the next runs.  The SkillPackage's
    validation_state is updated as it progresses.
    """

    def validate_static(self, yaml_text: str) -> ValidationResult:
        """Stage 1: Static analysis of skill YAML.

        Checks:
        - Valid YAML syntax
        - Required fields present (name, version, target, rules)
        - Target is a known type
        - Condition expressions parse without error
        - Field references are valid for the target type
        - No dangerous action types for auto-generated skills
        """
        import yaml as yaml_mod
        from harkeniq.skills.loader import parse_skill
        from harkeniq.errors import SkillValidationError

        errors = []
        warnings = []

        # Parse YAML
        try:
            data = yaml_mod.safe_load(yaml_text)
        except yaml_mod.YAMLError as e:
            return ValidationResult(
                passed=False, stage="static_analysis",
                errors=[f"Invalid YAML: {e}"],
            )

        if not isinstance(data, dict):
            return ValidationResult(
                passed=False, stage="static_analysis",
                errors=["Skill file must contain a YAML mapping"],
            )

        # Validate through the existing skill parser
        try:
            skill_def = parse_skill(data, source="<candidate>")
        except SkillValidationError as e:
            return ValidationResult(
                passed=False, stage="static_analysis",
                errors=[str(e)],
            )

        # Safety checks for auto-generated skills
        for rule in skill_def.rules:
            if rule.action and rule.action.type.value in ("POWER_CYCLE", "BMC_RESET"):
                warnings.append(
                    f"Auto-generated skill recommends {rule.action.type.value} "
                    "- requires manual review before promotion"
                )

        return ValidationResult(
            passed=True, stage="static_analysis",
            warnings=warnings,
        )

    def validate_dry_run(
        self, yaml_text: str, historical_states: list[dict[str, Any]]
    ) -> ValidationResult:
        """Stage 2: Dry-run against historical device state.

        Evaluates the skill's conditions against past device readings
        to check whether the skill would have matched real data.  This
        catches skills with impossible conditions or overly broad matching.
        """
        import yaml as yaml_mod
        from harkeniq.skills.loader import parse_skill
        from harkeniq.errors import SkillValidationError

        try:
            data = yaml_mod.safe_load(yaml_text)
            skill_def = parse_skill(data, source="<candidate>")
        except Exception as e:
            return ValidationResult(
                passed=False, stage="dry_run",
                errors=[f"Cannot parse skill for dry-run: {e}"],
            )

        # Evaluate conditions against historical states using the parsed ASTs.
        # This is a dry-run: we check if conditions MATCH, not run the full
        # async evaluate pipeline.
        from harkeniq.skills.expression import evaluate as eval_expr

        matches = 0
        errors = []
        warnings = []

        for state in historical_states:
            try:
                for rule in skill_def.rules:
                    if eval_expr(rule.parsed_ast, state):
                        matches += 1
                        break  # at least one rule matched this state
            except Exception as e:
                errors.append(f"Evaluation error on historical state: {e}")
                break

        if not historical_states:
            warnings.append("No historical states available for dry-run validation")
        elif matches == 0:
            warnings.append(
                f"Skill matched 0 of {len(historical_states)} historical states "
                "- may have overly restrictive conditions"
            )
        elif matches == len(historical_states):
            warnings.append(
                f"Skill matched ALL {len(historical_states)} historical states "
                "- may have overly broad conditions"
            )

        return ValidationResult(
            passed=len(errors) == 0,
            stage="dry_run",
            errors=errors,
            warnings=warnings,
            dry_run_matches=matches,
        )

    def validate_and_promote(
        self,
        yaml_text: str,
        package: SkillPackage,
        historical_states: list[dict[str, Any]] | None = None,
    ) -> tuple[ValidationResult, SkillPackage]:
        """Run the full validation pipeline and update package state.

        Returns (final_result, updated_package).
        """
        # Stage 1: Static analysis
        static = self.validate_static(yaml_text)
        if not static.passed:
            return static, package

        package.validation_state = ValidationState.TESTED

        # Stage 2: Dry-run (if historical data available)
        if historical_states:
            dry_run = self.validate_dry_run(yaml_text, historical_states)
            if not dry_run.passed:
                return dry_run, package
            # Merge warnings
            static.warnings.extend(dry_run.warnings)
            static.dry_run_matches = dry_run.dry_run_matches

        return static, package
