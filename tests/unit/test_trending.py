"""Unit tests for baseline (Welford) and trending (OLS) — Doc 13 §2-3, Doc 12 §2.2."""

import math
import random
import statistics

import pytest

from harkeniq.models import TrendingRule, VerdictSeverity
from harkeniq.skills.trending import TrendingEngine

T0 = 1_700_000_000.0  # arbitrary unix epoch base
STEP = 60.0  # 60-second polling


def make_engine(**overrides):
    config = {
        "baseline": {"min_samples": 10, "window_samples": 20, "critical_pause_samples": 3},
        "trending": {"min_samples": 10, "slope_threshold": 0.05,
                     "r_squared_min": 0.5, "max_projection_days": 90},
    }
    for section, values in overrides.items():
        config.setdefault(section, {}).update(values)
    return TrendingEngine(config)


def feed(engine, values, sensor="fan:Fan1", health="OK", start=T0, step=STEP):
    baseline = None
    for i, v in enumerate(values):
        baseline = engine.update_baseline(sensor, v, start + i * step, health)
    return baseline


def declining_rule(threshold_field="threshold_low_critical"):
    return TrendingRule(
        field="speed_rpm",
        direction="declining",
        verdict=VerdictSeverity.TRENDING,
        message_template="Fan {name} declining at {rate} RPM/hr, "
                         "projected to reach {threshold} in {time_to_threshold}",
        threshold_field=threshold_field,
    )


class TestWelford:
    def test_matches_statistics_module(self):
        rng = random.Random(42)
        values = [rng.gauss(9500, 150) for _ in range(120)]
        engine = make_engine(baseline={"window_samples": 200})
        b = feed(engine, values)
        assert b.sample_count == 120
        assert b.mean == pytest.approx(statistics.fmean(values), rel=1e-9)
        assert b.stddev == pytest.approx(statistics.pstdev(values), rel=1e-6)

    def test_eviction_matches_tail_window(self):
        rng = random.Random(7)
        values = [rng.gauss(50, 5) for _ in range(60)]
        engine = make_engine()  # window_samples = 20
        b = feed(engine, values)
        tail = values[-20:]
        assert len(b.ring_buffer) == 20
        assert b.sample_count == 20
        assert b.mean == pytest.approx(statistics.fmean(tail), rel=1e-9)
        assert b.stddev == pytest.approx(statistics.pstdev(tail), rel=1e-6)
        assert b.min_val == pytest.approx(min(tail))
        assert b.max_val == pytest.approx(max(tail))

    def test_all_identical_stddev_zero(self):
        # Doc 13 §5.1: no division by zero, stddev exactly 0
        b = feed(make_engine(), [9200.0] * 30)
        assert b.stddev == 0.0
        assert b.mean == 9200.0

    def test_first_last_sample_timestamps(self):
        b = feed(make_engine(), [1.0, 2.0, 3.0])
        assert b.first_sample_at == "2023-11-14T22:13:20Z"
        assert b.last_sample_at is not None


class TestConfidence:
    def test_untracked_sensor_zero(self):
        assert make_engine().confidence("nope") == 0.0

    def test_half_way(self):
        engine = make_engine()
        feed(engine, [1.0] * 5)  # min_samples = 10
        assert engine.confidence("fan:Fan1") == 0.5

    def test_capped_at_one(self):
        engine = make_engine()
        feed(engine, [1.0] * 15)
        assert engine.confidence("fan:Fan1") == 1.0


class TestCriticalFreeze:
    def test_critical_sample_not_learned(self):
        engine = make_engine()
        feed(engine, [100.0] * 5)
        b = engine.update_baseline("fan:Fan1", 0.0, T0 + 5 * STEP, "Critical")
        assert b.sample_count == 5
        assert b.mean == 100.0

    def test_recovery_pause(self):
        # critical_pause_samples = 3: skip 3 post-recovery samples
        engine = make_engine()
        feed(engine, [100.0] * 5)
        engine.update_baseline("fan:Fan1", 0.0, T0 + 5 * STEP, "Critical")
        for i in range(3):
            b = engine.update_baseline("fan:Fan1", 100.0, T0 + (6 + i) * STEP, "OK")
            assert b.sample_count == 5  # paused
        b = engine.update_baseline("fan:Fan1", 100.0, T0 + 9 * STEP, "OK")
        assert b.sample_count == 6  # resumed


class TestDiscontinuity:
    def test_five_sigma_jump_resets_baseline(self):
        # Doc 13 §5.2
        engine = make_engine()
        values = [100.0, 101.0] * 8  # stddev = 0.5
        feed(engine, values)
        b = engine.update_baseline("fan:Fan1", 200.0, T0 + 16 * STEP, "OK")
        assert b.sample_count == 1
        assert b.mean == 200.0

    def test_small_jump_does_not_reset(self):
        engine = make_engine()
        feed(engine, [100.0, 101.0] * 8)
        b = engine.update_baseline("fan:Fan1", 101.5, T0 + 16 * STEP, "OK")
        assert b.sample_count == 17


class TestTimeGap:
    def test_gap_resets_regression_not_welford(self):
        # Doc 13 §5.4: gap > 5 * interval starts a new regression segment
        engine = make_engine()
        b = feed(engine, [100.0 + i for i in range(15)])
        assert b.regression_state.n == 15
        gap_ts = T0 + 15 * STEP + 1000  # > 300s gap
        b = engine.update_baseline("fan:Fan1", 115.0, gap_ts, "OK")
        assert b.regression_state.n == 1  # new segment
        assert b.sample_count == 16  # Welford continues


class TestDegradedBaseline:
    def test_flagged_when_learned_during_warning(self):
        engine = make_engine()
        b = feed(engine, [100.0] * 10, health="Warning")
        assert b.degraded_baseline is True

    def test_not_flagged_when_healthy(self):
        engine = make_engine()
        b = feed(engine, [100.0] * 10, health="OK")
        assert b.degraded_baseline is False


class TestRegression:
    def test_perfect_linear_fit(self):
        # y declines 60 units per hour (1 per 60s sample)
        engine = make_engine()
        b = feed(engine, [9500.0 - i for i in range(20)])
        slope, intercept, r2 = TrendingEngine._compute_regression(b.regression_state)
        assert slope == pytest.approx(-60.0, rel=1e-6)
        assert r2 == pytest.approx(1.0, abs=1e-9)

    def test_constant_data_zero_slope(self):
        engine = make_engine()
        b = feed(engine, [500.0] * 20)
        slope, _, r2 = TrendingEngine._compute_regression(b.regression_state)
        assert slope == 0.0
        assert r2 == 0.0

    def test_degenerate_n_below_2(self):
        engine = make_engine()
        b = feed(engine, [500.0])
        assert TrendingEngine._compute_regression(b.regression_state) == (0.0, 0.0, 0.0)

    def test_incremental_matches_batch_after_eviction(self):
        rng = random.Random(3)
        values = [1000.0 - 0.5 * i + rng.gauss(0, 2) for i in range(60)]
        engine = make_engine()
        b = feed(engine, values)
        slope_inc, _, r2_inc = TrendingEngine._compute_regression(b.regression_state)
        # Batch OLS over the surviving window
        pts = [((ts - T0) / 3600.0, v) for ts, v in b.ring_buffer]
        n = len(pts)
        sx = sum(x for x, _ in pts); sy = sum(y for _, y in pts)
        sxy = sum(x * y for x, y in pts); sx2 = sum(x * x for x, _ in pts)
        slope_batch = (n * sxy - sx * sy) / (n * sx2 - sx ** 2)
        assert slope_inc == pytest.approx(slope_batch, rel=1e-6)
        assert 0.0 <= r2_inc <= 1.0


class TestComputeTrend:
    CONTEXT = {"name": "Fan1", "speed_rpm": 9481.0, "threshold_low_critical": 480}

    def _declining_engine(self):
        engine = make_engine()
        feed(engine, [9500.0 - i for i in range(20)])  # -60 RPM/hr
        return engine

    def test_happy_path(self):
        results = self._declining_engine().compute_trend(
            "fan:Fan1", [declining_rule()], self.CONTEXT
        )
        assert len(results) == 1
        r = results[0]
        assert r.slope == pytest.approx(-60.0, rel=1e-6)
        assert r.direction == "declining"
        assert r.threshold_name == "threshold_low_critical"
        assert r.threshold_value == 480.0
        # (480 - 9481) / -60 = 150.02 hours
        assert r.time_to_threshold_hours == pytest.approx(150.0, rel=0.01)
        assert r.confidence == 1.0
        assert "Fan1" in r.message and "-60.0" in r.message and "days" in r.message

    def test_confidence_below_one_no_trend(self):
        engine = make_engine()
        feed(engine, [9500.0 - i for i in range(5)])  # < min_samples
        assert engine.compute_trend("fan:Fan1", [declining_rule()], self.CONTEXT) == []

    def test_direction_filter_rising_rule_on_declining_data(self):
        rule = TrendingRule(
            field="speed_rpm", direction="rising",
            verdict=VerdictSeverity.TRENDING, message_template="x",
        )
        assert self._declining_engine().compute_trend("fan:Fan1", [rule], self.CONTEXT) == []

    def test_rising_fan_not_reported(self):
        # Doc 13 §3.3: fan speed-up is thermal response, not a fault
        engine = make_engine()
        feed(engine, [9500.0 + i for i in range(20)])
        assert engine.compute_trend("fan:Fan1", [declining_rule()], self.CONTEXT) == []

    def test_flat_data_no_trend(self):
        engine = make_engine()
        feed(engine, [9500.0] * 20)
        assert engine.compute_trend("fan:Fan1", [declining_rule()], self.CONTEXT) == []

    def test_noisy_data_r_squared_gate(self):
        rng = random.Random(9)
        engine = make_engine()
        feed(engine, [9500.0 + rng.gauss(0, 300) for _ in range(20)])
        assert engine.compute_trend("fan:Fan1", [declining_rule()], self.CONTEXT) == []

    def test_projection_beyond_horizon_skipped(self):
        # Slow decline: tth far beyond 90 days
        engine = make_engine(trending={"slope_threshold": 0.001})
        feed(engine, [9500.0 - 0.005 * i for i in range(20)])  # -0.3 RPM/hr
        assert engine.compute_trend("fan:Fan1", [declining_rule()], self.CONTEXT) == []

    def test_trend_moving_away_skipped(self):
        # Declining but threshold is above current: tth < 0
        ctx = dict(self.CONTEXT, threshold_low_critical=15000)
        assert self._declining_engine().compute_trend("fan:Fan1", [declining_rule()], ctx) == []

    def test_missing_threshold_in_context_skipped(self):
        ctx = {"name": "Fan1", "speed_rpm": 9481.0}
        assert self._declining_engine().compute_trend("fan:Fan1", [declining_rule()], ctx) == []

    def test_rule_without_threshold_field(self):
        # memory/psu style: rising rule with no projection target
        engine = make_engine()
        feed(engine, [float(i) for i in range(20)], sensor="memory:DIMM_A1")
        rule = TrendingRule(
            field="ecc_correctable_lifetime", direction="rising",
            verdict=VerdictSeverity.TRENDING,
            message_template="DIMM {name} ECC errors rising at {rate}/hr",
        )
        results = engine.compute_trend(
            "memory:DIMM_A1", [rule], {"name": "DIMM_A1", "ecc_correctable_lifetime": 19}
        )
        assert len(results) == 1
        assert results[0].time_to_threshold_hours == math.inf
        assert math.isnan(results[0].threshold_value)

    def test_numeric_threshold_constant(self):
        # disk style: project SSD wear toward 0
        engine = make_engine()
        feed(engine, [100.0 - 0.1 * i for i in range(20)], sensor="disk:Disk0")
        rule = TrendingRule(
            field="life_left_pct", direction="declining",
            verdict=VerdictSeverity.TRENDING,
            message_template="Disk wear, replacement in {time_to_threshold}",
            threshold_field="0",
        )
        results = engine.compute_trend(
            "disk:Disk0", [rule], {"name": "Disk0", "life_left_pct": 98.1}
        )
        assert len(results) == 1
        assert results[0].threshold_value == 0.0
        # slope = -6/hr, tth = 98.1/6 ≈ 16.35 h
        assert results[0].time_to_threshold_hours == pytest.approx(16.35, rel=0.01)

    def test_gap_suppresses_trend_until_segment_rebuilt(self):
        engine = make_engine()
        feed(engine, [9500.0 - i for i in range(15)])
        engine.update_baseline("fan:Fan1", 9484.0, T0 + 15 * STEP + 1000, "OK")
        # Only 1 sample in the new regression segment
        assert engine.compute_trend("fan:Fan1", [declining_rule()], self.CONTEXT) == []


class TestCounterRates:
    """QA-032 (Doc 13 §5.6): ECC counters baseline the rate, not the raw
    count."""

    def test_first_sample_primes_no_rate(self):
        engine = make_engine()
        assert engine.counter_to_rate("memory:D1", 100.0, T0) is None

    def test_steady_counter_yields_rate_per_hour(self):
        engine = make_engine()
        engine.counter_to_rate("memory:D1", 100.0, T0)
        # +2 errors over 60s -> 120/hr
        rate = engine.counter_to_rate("memory:D1", 102.0, T0 + 60)
        assert rate == pytest.approx(120.0)

    def test_reset_detected_and_skipped(self, caplog):
        engine = make_engine()
        engine.counter_to_rate("memory:D1", 100.0, T0)
        with caplog.at_level("WARNING"):
            rate = engine.counter_to_rate("memory:D1", 5.0, T0 + 60)
        assert rate is None
        assert "counter reset detected" in caplog.text
        # Rate tracking restarts from the new value
        rate = engine.counter_to_rate("memory:D1", 6.0, T0 + 120)
        assert rate == pytest.approx(60.0)

    def test_zero_elapsed_returns_none(self):
        engine = make_engine()
        engine.counter_to_rate("memory:D1", 100.0, T0)
        assert engine.counter_to_rate("memory:D1", 101.0, T0) is None

    def test_zero_rate_dimm_stays_healthy(self):
        """A DIMM producing no errors has all-zero rates: stddev = 0,
        slope = 0 -> no trend (Doc 13 §5.1 / §5.6)."""
        engine = make_engine()
        rule = TrendingRule(
            field="ecc_correctable_lifetime", direction="rising",
            verdict=VerdictSeverity.TRENDING,
            message_template="rate {current_rate}/hr", counter=True,
        )
        count = 500.0
        for i in range(21):
            rate = engine.counter_to_rate("memory:D1", count, T0 + i * STEP)
            if rate is not None:
                engine.update_baseline("memory:D1", rate, T0 + i * STEP, "OK")
        results = engine.compute_trend(
            "memory:D1", [rule], {"ecc_correctable_lifetime": count}
        )
        assert results == []

    def test_accelerating_rate_trends_with_rate_message(self):
        """An accelerating error rate produces TRENDING with the current
        RATE in the message, never the raw lifetime count."""
        engine = make_engine()
        rule = TrendingRule(
            field="ecc_correctable_lifetime", direction="rising",
            verdict=VerdictSeverity.TRENDING,
            message_template="DIMM {name} rate {current_rate}/hr", counter=True,
        )
        count = 0.0
        for i in range(25):
            count += i * 2.0  # growing delta each poll -> rising rate
            rate = engine.counter_to_rate("memory:D1", count, T0 + i * STEP)
            if rate is not None:
                engine.update_baseline("memory:D1", rate, T0 + i * STEP, "OK")
        results = engine.compute_trend(
            "memory:D1", [rule],
            {"name": "D1", "ecc_correctable_lifetime": count},
        )
        assert len(results) == 1
        r = results[0]
        assert r.slope > 0
        # current_value is the last RATE (48 errors / 60s = 2880/hr),
        # not the raw counter (600)
        assert r.current_value == pytest.approx(2880.0)
        assert "2880.0/hr" in r.message and str(count) not in r.message


class TestCheckpointRoundTrip:
    def test_restore_preserves_baseline_and_trending(self):
        engine = make_engine()
        feed(engine, [9500.0 - i for i in range(20)])
        saved = engine.get_all_baselines()

        engine2 = make_engine()
        engine2.restore_baselines(saved)
        b = engine2.get_baseline("fan:Fan1")
        assert b is not None
        assert engine2.confidence("fan:Fan1") == 1.0
        results = engine2.compute_trend(
            "fan:Fan1", [declining_rule()], TestComputeTrend.CONTEXT
        )
        assert len(results) == 1
        assert results[0].slope == pytest.approx(-60.0, rel=1e-6)
