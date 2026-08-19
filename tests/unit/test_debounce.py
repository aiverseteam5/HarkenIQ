"""Debounce regression tests (Doc 12 §6, Doc 06 §5.1, Doc 13 §4.3)."""

import pytest

from harkeniq.errors import ConfigError
from harkeniq.models import DebounceConfig, VerdictSeverity
from harkeniq.skills.debounce import Debouncer

C = VerdictSeverity.CRITICAL
W = VerdictSeverity.WARNING
H = VerdictSeverity.HEALTHY
T = VerdictSeverity.TRENDING


def run(debouncer, sequence, sensor="fan:Fan1", skill="fan-health", override=None):
    result = None
    for raw in sequence:
        result, _ = debouncer.apply(sensor, skill, raw, override)
    return result


class TestDoc12Matrix:
    def test_critical_2_of_3(self):
        # 1 critical, 1 healthy, 1 critical -> still HEALTHY (only 1 consecutive)
        assert run(Debouncer(), [C, H, C]) == H

    def test_critical_2_of_3_consecutive(self):
        assert run(Debouncer(), [C, C]) == C

    def test_warning_3_of_5(self):
        # 3 warnings in 5 polls, non-consecutive
        assert run(Debouncer(), [W, H, W, H, W]) == W

    def test_warning_2_of_5(self):
        assert run(Debouncer(), [W, H, W, H, H]) == H

    def test_recovery_3_of_3(self):
        assert run(Debouncer(), [C, C, H, H, H]) == H

    def test_recovery_2_of_3(self):
        # 2 healthy + 1 warning after critical: still CRITICAL
        assert run(Debouncer(), [C, C, H, W, H]) == C

    def test_trending_no_debounce(self):
        assert run(Debouncer(), [T]) == T


class TestBehavior:
    def test_initial_state_healthy(self):
        assert run(Debouncer(), [H]) == H

    def test_critical_held_until_recovery(self):
        d = Debouncer()
        assert run(d, [C, C]) == C
        assert run(d, [H, H]) == C  # only 2 consecutive healthy
        assert run(d, [H]) == H  # third completes recovery

    def test_trending_does_not_pollute_window(self):
        d = Debouncer()
        run(d, [C])
        assert run(d, [T]) == T
        assert run(d, [C]) == C  # C,C still consecutive across the TRENDING

    def test_per_rule_override_critical_1_of_1(self):
        override = DebounceConfig(count=1, window=1)
        assert run(Debouncer(), [C], override=override) == C

    def test_per_rule_override_warning(self):
        override = DebounceConfig(count=2, window=2)
        assert run(Debouncer(), [W, W], override=override) == W

    def test_states_are_per_sensor_and_skill(self):
        d = Debouncer()
        run(d, [C, C], sensor="fan:Fan1")
        verdict, _ = d.apply("fan:Fan2", "fan-health", H)
        assert verdict == H
        assert d.get_state("fan:Fan1", "fan-health").current_debounced == C

    def test_transition_timestamp_recorded(self):
        d = Debouncer()
        run(d, [C, C])
        state = d.get_state("fan:Fan1", "fan-health")
        assert state.last_transition_at is not None

    def test_reload_skills_drops_removed(self):
        d = Debouncer()
        run(d, [C, C], skill="old-skill")
        run(d, [C, C], skill="kept-skill")
        d.reload_skills({"kept-skill"})
        assert d.get_state("fan:Fan1", "old-skill") is None
        assert d.get_state("fan:Fan1", "kept-skill") is not None

    def test_window_trimmed_to_max(self):
        d = Debouncer()
        run(d, [H] * 20)
        state = d.get_state("fan:Fan1", "fan-health")
        assert len(state.window) == 5  # max(3, 5, 3)


class TestConfig:
    def test_custom_config(self):
        d = Debouncer({"critical": [3, 4], "warning": [3, 5], "recovery": [3, 3]})
        assert run(d, [C, C]) == H  # needs 3 consecutive now
        assert run(d, [C]) == C

    def test_invalid_pair_raises(self):
        with pytest.raises(ConfigError):
            Debouncer({"critical": [3, 2]})  # count > window

    def test_invalid_type_raises(self):
        with pytest.raises(ConfigError):
            Debouncer({"warning": "3 of 5"})

    def test_zero_count_raises(self):
        with pytest.raises(ConfigError):
            Debouncer({"recovery": [0, 3]})
