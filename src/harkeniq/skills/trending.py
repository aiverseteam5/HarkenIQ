"""Baseline management and trending analysis (Doc 13, Doc 10 §2.11).

TrendingEngine maintains per-sensor baselines (Welford's online algorithm
over a fixed-size ring buffer) and incremental OLS linear regression, and
produces TrendResult verdicts when a sensor drifts toward a threshold.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Optional

from harkeniq.models import Baseline, RegressionState, TrendingRule, TrendResult

logger = logging.getLogger("harkeniq.skills.trending")


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _humanize_hours(hours: float) -> str:
    if not math.isfinite(hours):
        return "unknown"
    if hours < 48:
        return f"{hours:.0f} hours"
    return f"{hours / 24:.0f} days"


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class TrendingEngine:
    """Manages per-sensor baselines and trending analysis (Doc 10 §2.11).

    All methods are safe: edge cases (identical samples, discontinuities,
    time gaps, division by zero) are handled internally and never raise.
    """

    def __init__(self, config: Optional[dict] = None) -> None:
        config = config or {}
        b = config.get("baseline", {})
        t = config.get("trending", {})
        self.window_samples: int = b.get("window_samples", 1440)
        self.min_samples: int = b.get("min_samples", 60)
        self.critical_pause_samples: int = b.get("critical_pause_samples", 5)
        self.trending_min_samples: int = t.get("min_samples", 60)
        self.slope_threshold: float = t.get("slope_threshold", 0.05)
        self.r_squared_min: float = t.get("r_squared_min", 0.5)
        self.max_projection_hours: float = t.get("max_projection_days", 90) * 24.0
        self.expected_interval: float = config.get("polling", {}).get("sensor_interval", 60)

        self._baselines: dict[str, Baseline] = {}
        # QA-032 (Doc 13 §5.6): last raw (ts, value) per counter-type
        # sensor for rate conversion. Not checkpointed: after a restart
        # the first sample re-primes and rate tracking resumes.
        self._counter_prev: dict[str, tuple[float, float]] = {}
        # Fixed x-axis epoch per sensor (unix ts of first sample). Using a
        # fixed epoch keeps the incremental regression sums valid across
        # ring-buffer evictions (shifting x would invalidate them).
        self._x_epoch: dict[str, float] = {}
        # Start of the most recent contiguous segment (Doc 13 §5.4): only
        # samples at/after this timestamp contribute to the regression.
        self._segment_start: dict[str, float] = {}
        # Non-OK sample count during the learning window (Doc 13 §5.3).
        self._non_ok_learning: dict[str, int] = {}

    # -- public API ---------------------------------------------------------

    def confidence(self, sensor_id: str) -> float:
        """Baseline confidence: min(1.0, sample_count / min_samples) (Doc 13 §2.3)."""
        baseline = self._baselines.get(sensor_id)
        if baseline is None:
            return 0.0
        if self.min_samples <= 0:
            return 1.0
        return min(1.0, baseline.sample_count / self.min_samples)

    def counter_to_rate(
        self, sensor_id: str, value: float, timestamp: float
    ) -> Optional[float]:
        """Convert a monotonic counter sample to a rate (Doc 13 §5.6).

        Returns the delta per hour since the previous sample, or None when
        no rate is computable yet (first sample, counter reset, or zero
        elapsed time). A reset (current < previous) is logged and restarts
        rate tracking from the new value.
        """
        prev = self._counter_prev.get(sensor_id)
        self._counter_prev[sensor_id] = (timestamp, value)
        if prev is None:
            return None
        prev_ts, prev_val = prev
        if value < prev_val:
            logger.warning(
                "ECC counter reset detected for %s (%.0f -> %.0f); "
                "restarting rate tracking", sensor_id, prev_val, value,
            )
            return None
        hours = (timestamp - prev_ts) / 3600.0
        if hours <= 0:
            return None
        return (value - prev_val) / hours

    def update_baseline(
        self,
        sensor_id: str,
        value: float,
        timestamp: float,
        current_health: str,
    ) -> Baseline:
        """Add a new sample to the sensor's baseline (Doc 13 §2.5)."""
        baseline = self._baselines.get(sensor_id)
        if baseline is None:
            baseline = self._new_baseline(sensor_id, timestamp)

        # CRITICAL freeze: never learn a fault condition as normal
        if current_health == "Critical":
            baseline.critical_pause_remaining = self.critical_pause_samples
            return baseline

        # Recovery pause: skip N samples after CRITICAL recovery
        if baseline.critical_pause_remaining > 0:
            baseline.critical_pause_remaining -= 1
            return baseline

        # Sudden discontinuity (Doc 13 §5.2): > 5 sigma jump resets the
        # baseline. Only applies once the baseline is trusted — during
        # learning the running stddev is too unstable to gate on.
        if (
            baseline.sample_count >= self.min_samples
            and baseline.stddev > 0
            and abs(value - baseline.mean) > 5 * baseline.stddev
        ):
            z = abs(value - baseline.mean) / baseline.stddev
            logger.warning(
                "Baseline reset for %s: value %s deviates %.1f sigma from mean %.1f",
                sensor_id, value, z, baseline.mean,
            )
            baseline = self._new_baseline(sensor_id, timestamp)

        # Time gap (Doc 13 §5.4): start a new regression segment
        if baseline.ring_buffer:
            gap = timestamp - baseline.ring_buffer[-1][0]
            if gap > 5 * self.expected_interval:
                logger.warning(
                    "Time gap detected: %.0fs between samples for %s", gap, sensor_id
                )
                baseline.regression_state = RegressionState()
                self._segment_start[sensor_id] = timestamp

        # Evict oldest sample if the ring buffer is full
        evicted_extremum = False
        while len(baseline.ring_buffer) >= baseline.buffer_size:
            old_ts, old_val = baseline.ring_buffer.pop(0)
            self._welford_remove(baseline, old_val)
            if old_ts >= self._segment_start.get(sensor_id, -math.inf):
                self._regression_remove(
                    baseline.regression_state, self._x(sensor_id, old_ts), old_val
                )
            baseline.regression_state.eviction_count += 1
            if baseline.regression_state.eviction_count % 1000 == 0:
                self._recompute_regression_sums(sensor_id, baseline)
            if old_val in (baseline.min_val, baseline.max_val):
                evicted_extremum = True

        # Degraded-baseline tracking (Doc 13 §5.3), before the sample lands
        if baseline.sample_count < self.min_samples and current_health != "OK":
            self._non_ok_learning[sensor_id] = self._non_ok_learning.get(sensor_id, 0) + 1

        # Add the new sample
        baseline.ring_buffer.append((timestamp, value))
        self._welford_update(baseline, value)
        self._regression_add(
            baseline.regression_state, self._x(sensor_id, timestamp), value
        )

        if evicted_extremum:
            values = [v for _, v in baseline.ring_buffer]
            baseline.min_val = min(values)
            baseline.max_val = max(values)
        else:
            baseline.min_val = min(baseline.min_val, value)
            baseline.max_val = max(baseline.max_val, value)

        baseline.first_sample_at = _iso(baseline.ring_buffer[0][0])
        baseline.last_sample_at = _iso(timestamp)

        if baseline.sample_count == self.min_samples:
            non_ok = self._non_ok_learning.get(sensor_id, 0)
            if non_ok > self.min_samples / 2:
                baseline.degraded_baseline = True
                logger.warning(
                    "Baseline for %s learned during degraded state "
                    "(%d of %d samples not OK)", sensor_id, non_ok, self.min_samples,
                )

        return baseline

    def compute_trend(
        self,
        sensor_id: str,
        trending_rules: list[TrendingRule],
        context: dict[str, Any],
    ) -> list[TrendResult]:
        """Compute trending verdicts for a sensor (Doc 13 §3.3-3.5).

        Only runs when baseline confidence == 1.0. TRENDING verdicts are
        never debounced (Doc 13 §4.3).
        """
        baseline = self._baselines.get(sensor_id)
        if baseline is None or self.confidence(sensor_id) < 1.0:
            return []

        results: list[TrendResult] = []
        reg = baseline.regression_state
        for rule in trending_rules:
            if len(baseline.ring_buffer) < self.trending_min_samples:
                continue
            if reg.n < self.trending_min_samples:
                continue  # regression segment too short (e.g. after a time gap)

            slope, _, r_squared = self._compute_regression(reg)

            # Direction filter: only degradation, never recovery (Doc 13 §3.3)
            if rule.direction == "declining" and slope >= 0:
                continue
            if rule.direction == "rising" and slope <= 0:
                continue
            if abs(slope) <= self.slope_threshold:
                continue
            if r_squared <= self.r_squared_min:
                continue

            if rule.counter:
                # The baseline holds rates, not raw counts (Doc 13 §5.6);
                # the context still carries the raw counter value.
                current = baseline.ring_buffer[-1][1]
            else:
                current = context.get(rule.field)
                if current is None:
                    current = baseline.ring_buffer[-1][1]

            threshold_name, threshold_value, tth = self._project(
                rule, context, float(current), slope
            )
            if rule.threshold_field is not None and tth is None:
                continue  # threshold unresolvable or projection out of bounds
            tth_hours = tth if tth is not None else math.inf

            message = rule.message_template.format_map(_SafeDict(
                dict(context),
                rate=round(slope, 1),
                current_rate=round(float(current), 1),
                threshold=threshold_value,
                time_to_threshold=_humanize_hours(tth_hours),
            ))

            results.append(TrendResult(
                sensor_id=sensor_id,
                field=rule.field,
                slope=slope,
                r_squared=r_squared,
                direction=rule.direction,
                current_value=float(current),
                threshold_name=threshold_name,
                threshold_value=threshold_value,
                time_to_threshold_hours=tth_hours,
                confidence=self.confidence(sensor_id),
                message=message,
            ))

        return results

    def get_baseline(self, sensor_id: str) -> Optional[Baseline]:
        return self._baselines.get(sensor_id)

    def get_all_baselines(self) -> dict[str, Baseline]:
        return dict(self._baselines)

    def restore_baselines(self, baselines: dict[str, Baseline]) -> None:
        """Restore baselines from checkpoint data (called on startup).

        Regression sums are recomputed from the restored ring buffer so the
        x-axis epoch is re-anchored consistently (the persisted sums used an
        epoch that is not itself persisted).
        """
        for sensor_id, baseline in baselines.items():
            self._baselines[sensor_id] = baseline
            if baseline.ring_buffer:
                self._x_epoch[sensor_id] = baseline.ring_buffer[0][0]
                self._segment_start[sensor_id] = -math.inf
                self._recompute_regression_sums(sensor_id, baseline)

    # -- internals ----------------------------------------------------------

    def _new_baseline(self, sensor_id: str, timestamp: float) -> Baseline:
        baseline = Baseline(sensor_id=sensor_id, buffer_size=self.window_samples)
        self._baselines[sensor_id] = baseline
        self._x_epoch[sensor_id] = timestamp
        self._segment_start[sensor_id] = -math.inf
        self._non_ok_learning[sensor_id] = 0
        return baseline

    def _x(self, sensor_id: str, timestamp: float) -> float:
        """X axis: hours since the sensor's fixed epoch (Doc 13 §3.1)."""
        return (timestamp - self._x_epoch.get(sensor_id, timestamp)) / 3600.0

    @staticmethod
    def _welford_update(baseline: Baseline, value: float) -> None:
        """Welford's online algorithm: add a sample (Doc 13 §2.2)."""
        baseline.sample_count += 1
        delta = value - baseline.mean
        baseline.mean += delta / baseline.sample_count
        delta2 = value - baseline.mean
        baseline.m2 += delta * delta2
        baseline.variance = baseline.m2 / baseline.sample_count
        baseline.stddev = math.sqrt(max(0.0, baseline.variance))

    @staticmethod
    def _welford_remove(baseline: Baseline, old_value: float) -> None:
        """Inverse Welford: remove the evicted oldest sample (Doc 13 §2.2)."""
        if baseline.sample_count <= 1:
            baseline.mean = 0.0
            baseline.m2 = 0.0
            baseline.variance = 0.0
            baseline.stddev = 0.0
            baseline.sample_count = 0
            return
        delta = old_value - baseline.mean
        baseline.sample_count -= 1
        baseline.mean -= delta / baseline.sample_count
        delta2 = old_value - baseline.mean
        baseline.m2 -= delta * delta2
        baseline.variance = baseline.m2 / baseline.sample_count
        baseline.stddev = math.sqrt(max(0.0, baseline.variance))

    @staticmethod
    def _regression_add(reg: RegressionState, x: float, y: float) -> None:
        reg.sum_x += x
        reg.sum_y += y
        reg.sum_xy += x * y
        reg.sum_x2 += x * x
        reg.sum_y2 += y * y
        reg.n += 1

    @staticmethod
    def _regression_remove(reg: RegressionState, x: float, y: float) -> None:
        reg.sum_x -= x
        reg.sum_y -= y
        reg.sum_xy -= x * y
        reg.sum_x2 -= x * x
        reg.sum_y2 -= y * y
        reg.n = max(0, reg.n - 1)

    @staticmethod
    def _compute_regression(reg: RegressionState) -> tuple[float, float, float]:
        """Return (slope, intercept, r_squared); (0, 0, 0) when degenerate."""
        n = reg.n
        if n < 2:
            return 0.0, 0.0, 0.0
        denom_x = n * reg.sum_x2 - reg.sum_x ** 2
        if denom_x <= 0:
            return 0.0, 0.0, 0.0
        denom_y = n * reg.sum_y2 - reg.sum_y ** 2
        if denom_y <= 1e-12 * max(1.0, n * reg.sum_y2):
            # Constant y (within float residue): flat, no trend (Doc 13 §5.1)
            return 0.0, reg.sum_y / n, 0.0
        cov = n * reg.sum_xy - reg.sum_x * reg.sum_y
        slope = cov / denom_x
        intercept = (reg.sum_y - slope * reg.sum_x) / n
        r_squared = (cov ** 2) / (denom_x * denom_y)
        return slope, intercept, min(1.0, max(0.0, r_squared))

    def _recompute_regression_sums(self, sensor_id: str, baseline: Baseline) -> None:
        """Rebuild the running sums from the ring buffer (float-drift guard)."""
        reg = baseline.regression_state
        evictions = reg.eviction_count
        fresh = RegressionState(eviction_count=evictions)
        segment_start = self._segment_start.get(sensor_id, -math.inf)
        for ts, val in baseline.ring_buffer:
            if ts >= segment_start:
                self._regression_add(fresh, self._x(sensor_id, ts), val)
        baseline.regression_state = fresh

    def _project(
        self,
        rule: TrendingRule,
        context: dict[str, Any],
        current: float,
        slope: float,
    ) -> tuple[str, float, Optional[float]]:
        """Resolve the threshold and project time-to-threshold (Doc 13 §3.4).

        Returns (threshold_name, threshold_value, hours-or-None). None hours
        means the projection is out of bounds or the threshold unresolvable;
        rules without a threshold_field return (\"\", nan, None).
        """
        if rule.threshold_field is None:
            return "", math.nan, None

        try:
            threshold = float(rule.threshold_field)
            name = rule.threshold_field
        except ValueError:
            name = rule.threshold_field
            raw = context.get(name)
            if raw is None or not isinstance(raw, (int, float)) or isinstance(raw, bool):
                return name, math.nan, None
            threshold = float(raw)

        if slope == 0:
            return name, threshold, None
        tth = (threshold - current) / slope
        if tth <= 0 or tth > self.max_projection_hours:
            return name, threshold, None
        return name, threshold, tth
