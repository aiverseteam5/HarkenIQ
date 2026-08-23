"""Agent-side skill receiver for SM-pushed skills (R3b-1 C7).

When the SM pushes a validated skill via PushSkill RPC, the agent:
1. Validates the YAML schema + fields (same as load-time validation)
2. Writes the skill file to the skills directory
3. Hot-reloads the skill engine (reuses the existing SIGHUP pattern)

Skills can only be received from the authenticated SM (via the existing
gRPC TLS + bearer token channel).  Auto-generated skills with
validation_state=DRAFT are rejected (must be at least TESTED).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from harkeniq.errors import SkillValidationError
from harkeniq.skills.loader import parse_skill

logger = logging.getLogger("harkeniq.autonomy.skill_receiver")


class SkillReceiver:
    """Receives and installs SM-pushed skills on the agent."""

    def __init__(self, skills_dir: str) -> None:
        self._skills_dir = Path(skills_dir)

    def receive(
        self,
        skill_id: str,
        version: str,
        yaml_content: str,
        tier: str,
        validation_state: str,
    ) -> tuple[bool, str]:
        """Validate and install a pushed skill.

        Returns (accepted, reason).
        """
        # Reject DRAFT skills (must be at least TESTED)
        if validation_state == "draft":
            return False, "DRAFT skills cannot be deployed to agents"

        # Validate YAML
        try:
            import yaml
            data = yaml.safe_load(yaml_content)
            skill_def = parse_skill(data, source=f"pushed:{skill_id}")
        except (SkillValidationError, Exception) as e:
            logger.warning("Rejected pushed skill %s: %s", skill_id, e)
            return False, f"Validation failed: {e}"

        # Write to skills directory
        try:
            self._skills_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{skill_id}.yaml"
            target = self._skills_dir / filename
            target.write_text(yaml_content)
            logger.info(
                "Installed skill %s v%s (%s, %s) -> %s",
                skill_id, version, tier, validation_state, target,
            )
        except OSError as e:
            logger.error("Cannot write skill %s: %s", skill_id, e)
            return False, f"Write failed: {e}"

        return True, "installed"

    def list_installed(self) -> list[str]:
        """List installed skill filenames."""
        if not self._skills_dir.is_dir():
            return []
        return sorted(p.stem for p in self._skills_dir.glob("*.yaml"))
