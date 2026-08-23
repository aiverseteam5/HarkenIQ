"""Blast radius rate limiting per action type per fault domain (spec A2.1).

Prevents the agent from executing too many actions of the same type
within a time window.  Limits are per-action-type with configurable
windows and cooldowns.  This is the agent-local rate limiter; the SM
enforces site-wide limits independently.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass

from harkeniq.models import ActionType

logger = logging.getLogger("harkeniq.autonomy.blast_radius")


@dataclass
class RateLimit:
    """Rate limit configuration for one action type."""

    max_per_window: int
    window_seconds: float
    cooldown_seconds: float  # minimum time between executions


# Default rate limits per action type (A2.1)
DEFAULT_LIMITS: dict[ActionType, RateLimit] = {
    ActionType.IDENTIFY_LED: RateLimit(max_per_window=999, window_seconds=60, cooldown_seconds=0),
    ActionType.COLLECT_DIAGNOSTICS: RateLimit(max_per_window=999, window_seconds=60, cooldown_seconds=0),
    ActionType.FAN_RESET: RateLimit(max_per_window=2, window_seconds=86400, cooldown_seconds=300),
    ActionType.SEL_CLEAR: RateLimit(max_per_window=999, window_seconds=3600, cooldown_seconds=0),
    ActionType.BMC_RESET: RateLimit(max_per_window=1, window_seconds=14400, cooldown_seconds=900),
    ActionType.POWER_CYCLE: RateLimit(max_per_window=1, window_seconds=1800, cooldown_seconds=1800),
    ActionType.POWER_CAP_ADJUST: RateLimit(max_per_window=3, window_seconds=3600, cooldown_seconds=300),
}


class BlastRadiusLimiter:
    """Agent-local rate limiter for action execution.

    Tracks execution timestamps per action type and enforces max-per-window
    and cooldown limits.  Does NOT enforce cross-device or cross-fault-domain
    limits (that's the SM's job via suppression policy).
    """

    def __init__(self, limits: dict[ActionType, RateLimit] | None = None) -> None:
        self._limits = limits or dict(DEFAULT_LIMITS)
        # action_type -> list of execution timestamps (monotonic)
        self._history: dict[ActionType, list[float]] = defaultdict(list)

    def allows(self, action_type: ActionType, now: float | None = None) -> bool:
        """Check whether executing this action type would violate rate limits."""
        now = now or time.monotonic()
        limit = self._limits.get(action_type)
        if limit is None:
            return True  # no limit configured

        history = self._history[action_type]

        # Cooldown check: time since last execution
        if history and (now - history[-1]) < limit.cooldown_seconds:
            logger.debug(
                "Blast radius: %s blocked by cooldown (%.0fs remaining)",
                action_type.value,
                limit.cooldown_seconds - (now - history[-1]),
            )
            return False

        # Window check: count executions within window
        window_start = now - limit.window_seconds
        recent = [t for t in history if t > window_start]
        if len(recent) >= limit.max_per_window:
            logger.debug(
                "Blast radius: %s at limit (%d/%d in %.0fs window)",
                action_type.value,
                len(recent),
                limit.max_per_window,
                limit.window_seconds,
            )
            return False

        return True

    def record(self, action_type: ActionType, now: float | None = None) -> None:
        """Record that an action was executed."""
        now = now or time.monotonic()
        self._history[action_type].append(now)
        # Trim old entries (keep last 100 per type)
        if len(self._history[action_type]) > 100:
            self._history[action_type] = self._history[action_type][-50:]

    def reset(self, action_type: ActionType | None = None) -> None:
        """Reset rate limit history (for testing or after operator override)."""
        if action_type:
            self._history[action_type].clear()
        else:
            self._history.clear()
