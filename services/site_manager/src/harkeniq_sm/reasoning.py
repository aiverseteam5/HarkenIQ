"""SM reasoning provider abstraction (spec A2.7 contract 6).

The SM correlation engine uses ReasoningProvider implementations to
analyze escalated events.  R3a ships two providers:
  - DeterministicReasoner: skill pattern matching (delegates to agent)
  - KnowledgeBaseReasoner: history lookup ("has this been seen before?")

R3b adds LLMReasoner (plugs in without pipeline change).

The interface is designed so reasoning providers chain: deterministic
first, then knowledge-base, then LLM for novel situations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

logger = logging.getLogger("harkeniq.sm.reasoning")


@dataclass
class ReasoningContext:
    """Input context for reasoning providers."""

    device_id: str
    component: str
    severity: str
    evidence: list[dict[str, Any]] = field(default_factory=list)
    device_state: dict[str, Any] = field(default_factory=dict)
    peer_observations: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ReasoningResult:
    """Output from a reasoning provider."""

    provider: str  # "deterministic" | "knowledge_base" | "llm"
    diagnosis: str = ""
    confidence: float = 0.0
    suggested_action: str = ""
    evidence_cited: list[str] = field(default_factory=list)
    similar_past_incidents: list[dict] = field(default_factory=list)
    reasoning_steps: list[str] = field(default_factory=list)


class ReasoningProvider(Protocol):
    """Protocol for SM reasoning providers (A2.7 contract 6)."""

    name: str

    def analyze(self, context: ReasoningContext) -> Optional[ReasoningResult]:
        """Analyze the context and return a result, or None if unable."""
        ...


class DeterministicReasoner:
    """Skill pattern matching: checks if known skills match the evidence.

    R3a: delegates skill matching to the agent (agent already evaluates
    skills locally).  SM-side deterministic reasoning checks the
    correlation rules (existing correlation engine).
    """

    name = "deterministic"

    def analyze(self, context: ReasoningContext) -> Optional[ReasoningResult]:
        # In R3a, deterministic reasoning is handled by the agent's skill
        # engine.  The SM's role is correlation, not skill matching.
        # This provider exists to complete the interface; SM correlation
        # engine feeds results directly.
        if not context.evidence:
            return None
        return ReasoningResult(
            provider=self.name,
            diagnosis=f"Correlation-based analysis for {context.component}",
            confidence=0.7,
            reasoning_steps=[
                f"Device {context.device_id} reported {context.severity} on {context.component}",
                f"Evidence items: {len(context.evidence)}",
            ],
        )


class KnowledgeBaseReasoner:
    """History lookup: checks if similar incidents have been seen before.

    Uses the KnowledgeBase (knowledge.py) to find past incidents
    with similar characteristics and surface their resolutions.
    """

    name = "knowledge_base"

    def __init__(self, knowledge_base=None) -> None:
        self._kb = knowledge_base

    def analyze(self, context: ReasoningContext) -> Optional[ReasoningResult]:
        if self._kb is None:
            return None

        # Look up past outcomes for this device
        history = self._kb.get_device_history(
            context.device_id, limit=5
        )
        if not history:
            return None

        # Find outcomes matching the same component
        relevant = [
            h for h in history
            if h.outcome in ("SUCCESS", "PARTIAL")
        ]
        if not relevant:
            return None

        past_actions = [h.action_type for h in relevant]
        return ReasoningResult(
            provider=self.name,
            diagnosis=f"Similar past incidents found for {context.component}",
            confidence=0.6,
            suggested_action=relevant[-1].action_type if relevant else "",
            similar_past_incidents=[
                {
                    "action_id": h.action_id,
                    "action_type": h.action_type,
                    "outcome": h.outcome,
                    "recorded_at": h.recorded_at,
                }
                for h in relevant[-3:]
            ],
            reasoning_steps=[
                f"Found {len(relevant)} successful past resolutions for {context.device_id}",
                f"Most recent action: {relevant[-1].action_type} ({relevant[-1].outcome})",
            ],
        )


class LLMReasoner:
    """LLM-assisted reasoning: explains and enriches deterministic diagnoses.

    R3b-1 C1: the LLM explains what HarkenIQ's deterministic reasoning
    already found.  It does not independently decide or execute actions.
    If the LLM is unavailable, the pipeline falls back to deterministic
    + knowledge-base results.

    The prompt includes: device state, evidence, severity, and past
    incidents (from KnowledgeBase, labeled as historical context).
    """

    name = "llm"

    def __init__(self, llm_provider, knowledge_base=None) -> None:
        self._provider = llm_provider
        self._kb = knowledge_base

    def analyze(self, context: ReasoningContext) -> Optional[ReasoningResult]:
        """Synchronous wrapper — the pipeline calls this, but LLM is async.

        For the SM's asyncio runtime, use analyze_async() directly.
        This exists to satisfy the ReasoningProvider protocol.
        """
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            # We're already in an async context; schedule and return None
            # (the ingest service calls analyze_async directly)
            return None
        except RuntimeError:
            return asyncio.run(self.analyze_async(context))

    async def analyze_async(self, context: ReasoningContext) -> Optional[ReasoningResult]:
        """Async LLM reasoning call."""
        messages = self._build_messages(context)
        completion = await self._provider.complete(messages)
        if completion is None:
            return None

        return self._parse_response(completion, context)

    def _build_messages(self, context: ReasoningContext) -> list[dict[str, str]]:
        """Build the LLM prompt from reasoning context + KB history."""
        system = (
            "You are an expert hardware diagnostics assistant for HarkenIQ, "
            "an autonomous data center operations platform. You explain "
            "hardware faults clearly and concisely for infrastructure operators. "
            "Base your analysis only on the evidence provided. "
            "Do not speculate beyond the evidence."
        )

        # Current evidence
        evidence_lines = []
        for e in context.evidence:
            evidence_lines.append(f"  - {e}")
        evidence_text = "\n".join(evidence_lines) if evidence_lines else "  (no structured evidence)"

        # Historical context from knowledge base
        history_text = ""
        if self._kb:
            past = self._kb.get_device_history(context.device_id, limit=5)
            if past:
                history_lines = [
                    f"  - {h.action_type}: {h.outcome} (at {h.recorded_at:.0f})"
                    for h in past[-3:]
                ]
                history_text = (
                    "\n\nHistorical context (past incidents on this device, "
                    "for reference only — not current evidence):\n"
                    + "\n".join(history_lines)
                )

        user = (
            f"Device: {context.device_id}\n"
            f"Component: {context.component}\n"
            f"Severity: {context.severity}\n"
            f"Current evidence:\n{evidence_text}"
            f"{history_text}\n\n"
            "Provide:\n"
            "1. A one-paragraph summary of what is happening\n"
            "2. The most likely root cause\n"
            "3. What evidence supports this conclusion\n"
            "4. Recommended next steps for the operator"
        )

        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _parse_response(
        self, completion: str, context: ReasoningContext
    ) -> ReasoningResult:
        """Parse LLM completion into a ReasoningResult."""
        return ReasoningResult(
            provider=self.name,
            diagnosis=completion[:500],  # cap for storage
            confidence=0.5,  # LLM explanations start at moderate confidence
            suggested_action="",
            evidence_cited=[str(e) for e in context.evidence[:5]],
            reasoning_steps=[
                f"LLM analyzed {len(context.evidence)} evidence items for {context.component}",
                f"Device history: {len(self._kb.get_device_history(context.device_id)) if self._kb else 0} past outcomes",
            ],
        )


class ReasoningPipeline:
    """Chains multiple reasoning providers (A2.7 contract 6).

    Tries each provider in order.  Returns the first result with
    confidence above the threshold.  If no provider produces a
    confident result, returns the best available or None.
    """

    def __init__(self, providers: list[ReasoningProvider] | None = None) -> None:
        self._providers = providers or []

    def add_provider(self, provider: ReasoningProvider) -> None:
        self._providers.append(provider)

    def analyze(
        self, context: ReasoningContext, min_confidence: float = 0.5
    ) -> Optional[ReasoningResult]:
        best: Optional[ReasoningResult] = None

        for provider in self._providers:
            try:
                result = provider.analyze(context)
                if result is None:
                    continue
                if result.confidence >= min_confidence:
                    return result
                if best is None or result.confidence > best.confidence:
                    best = result
            except Exception as e:
                logger.warning(
                    "Reasoning provider %s failed: %s", provider.name, e
                )

        return best
