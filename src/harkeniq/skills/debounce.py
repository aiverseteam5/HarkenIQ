"""N-of-M verdict debounce (Doc 06 §5.1, Doc 13 §4.3, Doc 12 §6).

Filters noisy sensor readings so verdict transitions require sustained
evidence:

- CRITICAL: N consecutive CRITICAL polls within the last M (default 2 of 3).
  Doc 12 §6 requires consecutive matches (C,H,C stays HEALTHY).
- WARNING: N WARNING polls within the last M, non-consecutive (default 3 of 5).
- Recovery: N consecutive HEALTHY polls (default 3) to leave a degraded state.
- TRENDING verdicts are never debounced (Doc 13 §4.3).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from harkeniq.errors import ConfigError
from harkeniq.models import DebounceConfig, DebounceState, VerdictSeverity

logger = logging.getLogger("harkeniq.skills.debounce")

DEFAULT_CONFIG = {
    "critical": [2, 3],
    "warning": [3, 5],
    "recovery": [3, 3],
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pair(config: dict, key: str) -> tuple[int, int]:
    raw = config.get(key, DEFAULT_CONFIG[key])
    if (
        not isinstance(raw, (list, tuple)) or len(raw) != 2
        or not all(isinstance(v, int) and v >= 1 for v in raw)
        or raw[0] > raw[1]
    ):
        raise ConfigError(f"debounce.{key} must be [count, window] with 1 <= count <= window, got {raw!r}")
    return raw[0], raw[1]


def _max_run(window: list[VerdictSeverity], severity: VerdictSeverity) -> int:
    """Longest run of consecutive entries equal to severity."""
    best = run = 0
    for v in window:
        run = run + 1 if v == severity else 0
        best = max(best, run)
    return best


class Debouncer:
    """Maintains per-(sensor_id, skill_name) debounce state."""

    def __init__(self, config: Optional[dict] = None) -> None:
        config = config or {}
        self.critical = _pair(config, "critical")
        self.warning = _pair(config, "warning")
        self.recovery = _pair(config, "recovery")
        self._states: dict[tuple[str, str], DebounceState] = {}

    def get_state(self, sensor_id: str, skill_name: str) -> Optional[DebounceState]:
        return self._states.get((sensor_id, skill_name))

    def restore_states(self, states: dict[tuple[str, str], DebounceState]) -> None:
        self._states.update(states)

    def reload_skills(self, skill_names: set[str]) -> None:
        """Drop debounce state for skills that no longer exist (SIGHUP)."""
        self._states = {
            key: state for key, state in self._states.items() if key[1] in skill_names
        }

    def apply(
        self,
        sensor_id: str,
        skill_name: str,
        raw: VerdictSeverity,
        override: Optional[DebounceConfig] = None,
    ) -> tuple[VerdictSeverity, DebounceState]:
        """Feed one raw verdict; return the (debounced verdict, state).

        A per-rule override (Doc 07 §4.3) replaces the [count, window] pair
        for the raw verdict's severity.
        """
        state = self._states.get((sensor_id, skill_name))
        if state is None:
            state = DebounceState(sensor_id=sensor_id, skill_name=skill_name)
            self._states[(sensor_id, skill_name)] = state

        # TRENDING is never debounced (Doc 13 §4.3)
        if raw == VerdictSeverity.TRENDING:
            return raw, state

        crit_n, crit_w = self.critical
        warn_n, warn_w = self.warning
        if override is not None:
            if raw == VerdictSeverity.CRITICAL:
                crit_n, crit_w = override.count, override.window
            elif raw == VerdictSeverity.WARNING:
                warn_n, warn_w = override.count, override.window
        rec_n, rec_w = self.recovery

        max_window = max(crit_w, warn_w, rec_w)
        state.window.append(raw)
        del state.window[:-max_window]

        new = state.current_debounced
        if _max_run(state.window[-crit_w:], VerdictSeverity.CRITICAL) >= crit_n:
            new = VerdictSeverity.CRITICAL
        elif state.window[-warn_w:].count(VerdictSeverity.WARNING) >= warn_n:
            new = VerdictSeverity.WARNING
        elif (
            state.current_debounced != VerdictSeverity.HEALTHY
            and len(state.window) >= rec_n
            and all(v == VerdictSeverity.HEALTHY for v in state.window[-rec_n:])
        ):
            new = VerdictSeverity.HEALTHY

        # Track the config applied to this raw severity (informational)
        if raw == VerdictSeverity.CRITICAL:
            state.threshold_count, state.window_size = crit_n, crit_w
        elif raw == VerdictSeverity.WARNING:
            state.threshold_count, state.window_size = warn_n, warn_w
        else:
            state.threshold_count, state.window_size = rec_n, rec_w

        if new != state.current_debounced:
            logger.info(
                "Debounced verdict for %s/%s: %s -> %s",
                sensor_id, skill_name, state.current_debounced.value, new.value,
            )
            state.current_debounced = new
            state.last_transition_at = _utc_now()

        return state.current_debounced, state
