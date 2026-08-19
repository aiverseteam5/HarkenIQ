"""Coverage map (OQ-12): observation and health are SEPARATE fields.

Observation derives from agent_status.last_heartbeat_at at read time:
observed (< stale_after_s, default 2.5x heartbeat interval) → stale
(< unobserved_after_s, default 10 min) → unobserved. A silent device is
NEVER shown healthy — health degrades to "unknown" the moment
observation leaves "observed".
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from harkeniq_sm.config import SMConfig

_HEALTH_RANK = {"OK": 0, "TRENDING": 1, "UNKNOWN": 2, "WARNING": 3, "CRITICAL": 4}


def _aware(dt: datetime) -> datetime:
    # aiosqlite returns naive datetimes; stored values are UTC.
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def observation_state(
    last_heartbeat_at: Optional[datetime], config: SMConfig, now: Optional[datetime] = None
) -> str:
    if last_heartbeat_at is None:
        return "unobserved"
    now = now or datetime.now(timezone.utc)
    age = (now - _aware(last_heartbeat_at)).total_seconds()
    if age < config.stale_after_s:
        return "observed"
    if age < config.unobserved_after_s:
        return "stale"
    return "unobserved"


def worst_health(health_summary: Optional[dict[str, str]]) -> str:
    if not health_summary:
        return "unknown"
    worst = "OK"
    for severity in health_summary.values():
        if _HEALTH_RANK.get(severity, 2) > _HEALTH_RANK.get(worst, 0):
            worst = severity
    return worst.lower()


def coverage_entry(
    device, status, config: SMConfig, now: Optional[datetime] = None
) -> dict:
    """One coverage-map row. ``observation`` and ``health`` stay separate."""
    observation = observation_state(
        status.last_heartbeat_at if status else None, config, now
    )
    if observation == "observed":
        health = worst_health(status.last_health)
    else:
        health = "unknown"  # silent device never shown healthy (OQ-12)
    return {
        "device_id": device.id,
        "agent_id": device.agent_id,
        "agent_name": device.agent_name,
        "observation": observation,
        "health": health,
        "last_heartbeat_at": (
            _aware(status.last_heartbeat_at).isoformat() if status else None
        ),
        "state": status.last_state if status else "",
    }
