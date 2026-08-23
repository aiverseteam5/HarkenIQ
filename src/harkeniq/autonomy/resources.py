"""Agent resource self-monitoring and capability-aware degradation (spec A2.5).

Three deployment profiles (constrained/standard/performance) define
target/soft/hard limits for memory and CPU.  The agent monitors its own
usage, throttles at soft thresholds, and degrades capabilities at hard
limits while preserving safety-critical functions (heartbeat, authorization,
audit, action verification) for as long as possible.

Defense-in-depth: this is the INNER layer.  The OUTER layer is systemd
MemoryMax/CPUQuota (or cgroup/container limits), shipped as a drop-in
unit file matching the selected profile.
"""

from __future__ import annotations

import logging
import os
import resource
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("harkeniq.autonomy.resources")


class DegradationLevel(str, Enum):
    NORMAL = "normal"
    THROTTLED = "throttled"       # soft threshold: reduce polling frequency
    DEGRADED = "degraded"         # hard threshold: defer analysis, summarize telemetry
    OBSERVE_ONLY = "observe_only" # last resort: buffer everything


@dataclass
class ResourceProfile:
    """Three-tier resource limits (A2.5)."""

    name: str
    memory_target_mb: int
    memory_soft_mb: int
    memory_hard_mb: int
    cpu_target_pct: float
    cpu_soft_pct: float
    cpu_hard_pct: float


PROFILES = {
    "constrained": ResourceProfile(
        name="constrained",
        memory_target_mb=30, memory_soft_mb=40, memory_hard_mb=50,
        cpu_target_pct=2.0, cpu_soft_pct=3.0, cpu_hard_pct=5.0,
    ),
    "standard": ResourceProfile(
        name="standard",
        memory_target_mb=50, memory_soft_mb=75, memory_hard_mb=100,
        cpu_target_pct=5.0, cpu_soft_pct=7.0, cpu_hard_pct=10.0,
    ),
    "performance": ResourceProfile(
        name="performance",
        memory_target_mb=100, memory_soft_mb=150, memory_hard_mb=200,
        cpu_target_pct=10.0, cpu_soft_pct=15.0, cpu_hard_pct=20.0,
    ),
}


@dataclass
class ResourceSnapshot:
    """Current resource usage measurement."""

    memory_rss_mb: float
    cpu_percent: float  # user+sys over measurement window
    timestamp: float


class ResourceMonitor:
    """Self-monitoring with capability-aware degradation.

    Measures memory (RSS from /proc/self/status or resource module) and
    CPU (user+system time delta over measurement window).
    """

    def __init__(self, profile_name: str = "standard") -> None:
        self.profile = PROFILES.get(profile_name, PROFILES["standard"])
        self.level = DegradationLevel.NORMAL
        self._last_cpu_times: Optional[tuple[float, float]] = None  # (user, sys)
        self._last_cpu_check: float = 0.0
        self._consecutive_soft: int = 0
        self._consecutive_hard: int = 0
        # Degradation callback: set by agent to adjust poll intervals
        self.poll_interval_multiplier: float = 1.0

    def measure(self) -> ResourceSnapshot:
        """Take a resource usage snapshot."""
        memory_mb = self._get_rss_mb()
        cpu_pct = self._get_cpu_percent()
        return ResourceSnapshot(
            memory_rss_mb=memory_mb,
            cpu_percent=cpu_pct,
            timestamp=time.time(),
        )

    def evaluate(self, snapshot: ResourceSnapshot) -> DegradationLevel:
        """Evaluate resource usage against profile limits.

        Returns the new degradation level and updates internal state.
        The degradation sequence preserves safety-critical functions:
          1. THROTTLED: reduce non-essential polling (2x interval)
          2. DEGRADED: defer trending/baseline, summarize telemetry
          3. OBSERVE_ONLY: buffer everything, no new analysis
        """
        mem_exceeded = self._check_memory(snapshot.memory_rss_mb)
        cpu_exceeded = self._check_cpu(snapshot.cpu_percent)

        if mem_exceeded == "hard" or cpu_exceeded == "hard":
            self._consecutive_hard += 1
            self._consecutive_soft = 0
            if self._consecutive_hard >= 3:
                self.level = DegradationLevel.OBSERVE_ONLY
                self.poll_interval_multiplier = 4.0
            else:
                self.level = DegradationLevel.DEGRADED
                self.poll_interval_multiplier = 3.0
        elif mem_exceeded == "soft" or cpu_exceeded == "soft":
            self._consecutive_soft += 1
            self._consecutive_hard = 0
            if self._consecutive_soft >= 2:
                self.level = DegradationLevel.THROTTLED
                self.poll_interval_multiplier = 2.0
            # Single soft hit: stay at current level
        else:
            # Within target: recover
            self._consecutive_soft = 0
            self._consecutive_hard = 0
            self.level = DegradationLevel.NORMAL
            self.poll_interval_multiplier = 1.0

        return self.level

    def health_fields(self, snapshot: ResourceSnapshot) -> dict[str, str]:
        """Resource fields for heartbeat health_summary."""
        return {
            "resource_profile": self.profile.name,
            "resource_level": self.level.value,
            "memory_rss_mb": f"{snapshot.memory_rss_mb:.1f}",
            "cpu_percent": f"{snapshot.cpu_percent:.1f}",
        }

    def _check_memory(self, rss_mb: float) -> Optional[str]:
        if rss_mb >= self.profile.memory_hard_mb:
            return "hard"
        if rss_mb >= self.profile.memory_soft_mb:
            return "soft"
        return None

    def _check_cpu(self, cpu_pct: float) -> Optional[str]:
        if cpu_pct >= self.profile.cpu_hard_pct:
            return "hard"
        if cpu_pct >= self.profile.cpu_soft_pct:
            return "soft"
        return None

    def _get_rss_mb(self) -> float:
        """Get current RSS in MB.  Prefers /proc, falls back to resource module."""
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) / 1024.0  # kB -> MB
        except (OSError, ValueError, IndexError):
            pass
        # Fallback: resource module (returns kB on Linux)
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return usage.ru_maxrss / 1024.0

    def _get_cpu_percent(self) -> float:
        """Get CPU% over the measurement window (user+sys time delta)."""
        try:
            times = os.times()
            user, sys = times.user, times.system
        except (OSError, AttributeError):
            return 0.0

        now = time.monotonic()
        if self._last_cpu_times is None:
            self._last_cpu_times = (user, sys)
            self._last_cpu_check = now
            return 0.0

        dt = now - self._last_cpu_check
        if dt <= 0:
            return 0.0

        du = user - self._last_cpu_times[0]
        ds = sys - self._last_cpu_times[1]
        self._last_cpu_times = (user, sys)
        self._last_cpu_check = now

        return ((du + ds) / dt) * 100.0
