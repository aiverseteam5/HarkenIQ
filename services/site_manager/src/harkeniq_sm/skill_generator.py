"""LLM-assisted candidate skill generation (R3b-1 C2).

When the SM's reasoning pipeline diagnoses a novel situation (no existing
skill matched), the LLM can generate a candidate skill YAML.  The
candidate is stored with tier=AUTO_GENERATED, validation_state=DRAFT,
and must be reviewed by a human before promotion.

Uses the same LLMProvider as C1 (LLM Explain).  Different prompt format.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from harkeniq.autonomy.skill_lifecycle import (
    SkillPackage,
    SkillTier,
    ValidationState,
)

logger = logging.getLogger("harkeniq.sm.skill_generator")


class SkillGenerator:
    """Generates candidate skill YAML from LLM diagnosis.

    Input: a diagnosis (component, evidence, root cause, suggested action)
    Output: a SkillCandidate (YAML text + SkillPackage metadata)
    """

    def __init__(self, llm_provider) -> None:
        self._provider = llm_provider

    async def generate(
        self,
        device_id: str,
        component: str,
        severity: str,
        root_cause: str,
        suggested_action: str,
        evidence: list[dict[str, Any]],
        target: str = "",
    ) -> Optional["SkillCandidate"]:
        """Generate a candidate skill from a diagnosis.

        Returns None if the LLM fails or produces invalid output.
        """
        if not target:
            target = self._infer_target(component)

        messages = self._build_messages(
            device_id, component, severity, root_cause,
            suggested_action, evidence, target,
        )
        completion = await self._provider.complete(messages)
        if completion is None:
            return None

        # Extract YAML block from LLM response
        yaml_text = self._extract_yaml(completion)
        if not yaml_text:
            logger.warning("LLM did not produce valid YAML for skill generation")
            return None

        skill_id = f"auto-{component.replace(':', '-').replace('.', '-').lower()}-{int(time.time()) % 100000}"
        package = SkillPackage(
            skill_id=skill_id,
            version="0.1.0",
            tier=SkillTier.AUTO_GENERATED,
            validation_state=ValidationState.DRAFT,
        )

        return SkillCandidate(
            skill_id=skill_id,
            yaml_text=yaml_text,
            package=package,
            source_device=device_id,
            source_component=component,
            generated_at=time.time(),
        )

    def _build_messages(
        self, device_id: str, component: str, severity: str,
        root_cause: str, suggested_action: str,
        evidence: list, target: str,
    ) -> list[dict[str, str]]:
        system = (
            "You are a HarkenIQ skill author. Generate a YAML skill file "
            "that detects the hardware fault described below. Follow this format exactly:\n\n"
            "name: <descriptive-kebab-case-name>\n"
            "version: 1\n"
            "target: <fan|disk|memory|psu|thermal>\n"
            "description: <one line>\n"
            "rules:\n"
            "  - condition: \"<field operator value>\"\n"
            "    verdict: <CRITICAL|WARNING>\n"
            "    message: \"<human message with {field} placeholders>\"\n\n"
            "Use only these condition operators: ==, !=, <, >, <=, >=, AND, OR, NOT.\n"
            "Use only normalized field names for the target type.\n"
            "Output ONLY the YAML block, no explanation."
        )

        evidence_text = "\n".join(f"  - {e}" for e in evidence) if evidence else "  (none)"
        user = (
            f"Device: {device_id}\n"
            f"Component: {component}\n"
            f"Target type: {target}\n"
            f"Severity: {severity}\n"
            f"Root cause: {root_cause}\n"
            f"Suggested action: {suggested_action}\n"
            f"Evidence:\n{evidence_text}\n\n"
            "Generate the skill YAML:"
        )

        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _extract_yaml(self, completion: str) -> str:
        """Extract YAML content from LLM response.

        Handles responses wrapped in ```yaml ... ``` blocks or raw YAML.
        """
        # Try to find a fenced code block
        if "```" in completion:
            parts = completion.split("```")
            for i, part in enumerate(parts):
                if i % 2 == 1:  # odd parts are inside fences
                    # Strip optional language tag
                    lines = part.strip().splitlines()
                    if lines and lines[0].strip().lower() in ("yaml", "yml"):
                        return "\n".join(lines[1:]).strip()
                    return part.strip()

        # Try raw YAML (starts with "name:")
        stripped = completion.strip()
        if stripped.startswith("name:"):
            return stripped

        return ""

    def _infer_target(self, component: str) -> str:
        """Infer the skill target type from the component name."""
        comp_lower = component.lower()
        for target in ("fan", "disk", "memory", "psu", "thermal"):
            if target in comp_lower:
                return target
        if "dimm" in comp_lower or "ecc" in comp_lower:
            return "memory"
        if "drive" in comp_lower or "ssd" in comp_lower or "hdd" in comp_lower:
            return "disk"
        if "temp" in comp_lower or "inlet" in comp_lower:
            return "thermal"
        return "thermal"  # safe default


class SkillCandidate:
    """A generated candidate skill awaiting human review."""

    def __init__(
        self,
        skill_id: str,
        yaml_text: str,
        package: SkillPackage,
        source_device: str = "",
        source_component: str = "",
        generated_at: float = 0.0,
    ) -> None:
        self.skill_id = skill_id
        self.yaml_text = yaml_text
        self.package = package
        self.source_device = source_device
        self.source_component = source_component
        self.generated_at = generated_at or time.time()

    def to_dict(self) -> dict:
        return {
            "skill_id": self.skill_id,
            "yaml_text": self.yaml_text,
            "package": self.package.to_dict(),
            "source_device": self.source_device,
            "source_component": self.source_component,
            "generated_at": self.generated_at,
        }
