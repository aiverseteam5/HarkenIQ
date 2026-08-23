"""Skill lifecycle model (spec A2.7 contract 3).

Wraps existing skill definitions with versioning, validation state,
deployment history, and outcome statistics.  R3a captures this metadata;
R3b skill distribution and auto-generation populate the lifecycle fields.

This does NOT replace the existing skills/loader.py or skills/engine.py.
It adds lifecycle tracking on top of the existing skill evaluation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class SkillTier(str, Enum):
    CORE = "core"                # built by HarkenIQ team
    VENDOR = "vendor"            # vendor-specific
    COMMUNITY = "community"      # open-source contributions
    AUTO_GENERATED = "auto_generated"  # LLM-created (R3b)


class ValidationState(str, Enum):
    DRAFT = "draft"
    TESTED = "tested"
    CANARY = "canary"
    PROMOTED = "promoted"
    DEPRECATED = "deprecated"


@dataclass
class SkillOutcomeStats:
    """Aggregated outcome statistics for a skill."""

    success_count: int = 0
    failure_count: int = 0
    rollback_count: int = 0
    total_executions: int = 0

    @property
    def success_rate(self) -> float:
        if self.total_executions == 0:
            return 1.0
        return self.success_count / self.total_executions

    def record(self, outcome: str) -> None:
        self.total_executions += 1
        if outcome == "SUCCESS":
            self.success_count += 1
        elif outcome in ("FAILURE", "UNKNOWN"):
            self.failure_count += 1
        elif outcome == "ROLLBACK_TRIGGERED":
            self.rollback_count += 1


@dataclass
class DeploymentRecord:
    """Record of a skill deployment event."""

    deployed_at: float
    deployed_by: str = "system"
    version: str = ""
    target_scope: str = "all"  # "all" | "canary" | specific agent_ids


@dataclass
class SkillPackage:
    """Skill lifecycle metadata (A2.7 contract 3).

    Wraps the existing SkillDefinition with lifecycle tracking.
    The skill_id and version come from the YAML file's skill.id
    and skill.version fields (Doc 07 §3).
    """

    skill_id: str
    version: str = "1.0.0"
    vendor: str = ""
    device_types: list[str] = field(default_factory=list)
    tier: SkillTier = SkillTier.CORE
    validation_state: ValidationState = ValidationState.PROMOTED
    test_cases: list[str] = field(default_factory=list)
    deployment_history: list[DeploymentRecord] = field(default_factory=list)
    outcome_stats: SkillOutcomeStats = field(default_factory=SkillOutcomeStats)
    created_at: float = field(default_factory=time.time)
    last_deployed_at: Optional[float] = None

    def record_deployment(self, deployed_by: str = "system", scope: str = "all") -> None:
        self.deployment_history.append(DeploymentRecord(
            deployed_at=time.time(),
            deployed_by=deployed_by,
            version=self.version,
            target_scope=scope,
        ))
        self.last_deployed_at = time.time()

    def record_outcome(self, outcome: str) -> None:
        self.outcome_stats.record(outcome)

    def to_dict(self) -> dict:
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "vendor": self.vendor,
            "device_types": self.device_types,
            "tier": self.tier.value,
            "validation_state": self.validation_state.value,
            "outcome_stats": {
                "success_rate": f"{self.outcome_stats.success_rate:.1%}",
                "total": self.outcome_stats.total_executions,
            },
        }
