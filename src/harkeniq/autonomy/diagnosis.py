"""Structured diagnosis model (spec A2.7 contract 2, doc 01 R-M7).

A Diagnosis is a conclusion about a specific component, carrying:
- the affected component (specific enough to action)
- supporting evidence (readings with time spans)
- contradicting evidence
- per-dimension confidence
- tier that produced it
- trajectory (if progressive)
- recommended action
- reasoning path (how the conclusion was reached)

This is what `harken diagnose` produces and what the learning pipeline
(R3b) consumes.  R3a ships with DeterministicReasoner (skill matching)
and KnowledgeBaseReasoner (history lookup, P5).  R3b adds LLMReasoner.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from harkeniq.autonomy.tier import TierLevel


@dataclass
class DiagnosisEvidence:
    """A single piece of evidence supporting or contradicting a diagnosis."""

    source: str           # "redfish" | "os-signal" | "peer-witness" | "baseline"
    sensor_id: str
    reading: Any
    timestamp: float
    time_span_seconds: float = 0.0
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConfidenceDimension:
    """A single confidence dimension (A2.3: independent, extensible)."""

    name: str       # "baseline" | "skill_match" | "correlation" | "outcome_history"
    value: float    # 0.0 to 1.0
    detail: str = ""  # human-readable explanation


@dataclass
class Diagnosis:
    """Structured diagnosis output (R-M7, A2.7 contract 2).

    Replaces the simpler Verdict for R3a+ reasoning output.
    Verdicts remain as the skill evaluation result; Diagnosis wraps
    one or more verdicts with context, confidence, and reasoning path.
    """

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    device_id: str = ""
    component: str = ""         # specific: "DIMM.Socket.A1" not "memory"
    summary: str = ""           # one-line diagnosis

    # Evidence
    evidence: list[DiagnosisEvidence] = field(default_factory=list)
    contradicting: list[DiagnosisEvidence] = field(default_factory=list)

    # Confidence (per-dimension, not collapsed)
    confidence: list[ConfidenceDimension] = field(default_factory=list)

    # Context
    tier: TierLevel = TierLevel.T2
    trajectory: str = ""        # "stable" | "degrading" | "improving" + details
    recommended_action: str = ""
    reasoning_path: list[str] = field(default_factory=list)  # ordered steps

    # Timestamps
    created_at: float = field(default_factory=time.time)
    resolved_at: Optional[float] = None

    @property
    def overall_confidence(self) -> float:
        """Minimum confidence across all dimensions.

        This is a convenience accessor; the dimensions are the source of
        truth.  Do not use this for gating decisions (use individual
        dimensions per A2.3).
        """
        if not self.confidence:
            return 0.0
        return min(d.value for d in self.confidence)

    def add_evidence(
        self, source: str, sensor_id: str, reading: Any,
        timestamp: Optional[float] = None, **fields
    ) -> None:
        self.evidence.append(DiagnosisEvidence(
            source=source,
            sensor_id=sensor_id,
            reading=reading,
            timestamp=timestamp or time.time(),
            fields=fields,
        ))

    def add_reasoning_step(self, step: str) -> None:
        self.reasoning_path.append(step)

    def to_dict(self) -> dict:
        """Serialize for reporting / storage."""
        return {
            "id": self.id,
            "device_id": self.device_id,
            "component": self.component,
            "summary": self.summary,
            "evidence_count": len(self.evidence),
            "contradicting_count": len(self.contradicting),
            "confidence": [
                {"name": c.name, "value": c.value, "detail": c.detail}
                for c in self.confidence
            ],
            "tier": self.tier.value,
            "trajectory": self.trajectory,
            "recommended_action": self.recommended_action,
            "reasoning_path": self.reasoning_path,
            "created_at": self.created_at,
        }
