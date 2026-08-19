"""Coverage map OQ-12 contract: observation != health; silence != healthy."""

from datetime import datetime, timedelta, timezone

from harkeniq_sm.config import SMConfig
from harkeniq_sm.coverage import coverage_entry, observation_state, worst_health


class FakeDevice:
    id = "d1"
    agent_id = "a1"
    agent_name = "srv-1"


class FakeStatus:
    def __init__(self, age_s, health):
        self.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=age_s)
        self.last_health = health
        self.last_state = "OBSERVING"


CFG = SMConfig(insecure=True)  # stale 150s, unobserved 600s


class TestObservationState:
    def test_thresholds(self):
        now = datetime.now(timezone.utc)
        assert observation_state(now - timedelta(seconds=30), CFG) == "observed"
        assert observation_state(now - timedelta(seconds=200), CFG) == "stale"
        assert observation_state(now - timedelta(seconds=700), CFG) == "unobserved"
        assert observation_state(None, CFG) == "unobserved"

    def test_naive_datetime_treated_utc(self):
        naive = datetime.utcnow() - timedelta(seconds=10)
        assert observation_state(naive, CFG) == "observed"


class TestWorstHealth:
    def test_ranking(self):
        assert worst_health({"psu": "OK", "thermal": "OK"}) == "ok"
        assert worst_health({"psu": "WARNING", "thermal": "CRITICAL"}) == "critical"
        assert worst_health(None) == "unknown"


class TestCoverageContract:
    def test_observed_healthy(self):
        entry = coverage_entry(FakeDevice(), FakeStatus(10, {"psu": "OK"}), CFG)
        assert entry["observation"] == "observed"
        assert entry["health"] == "ok"

    def test_silent_device_never_healthy(self):
        """OQ-12: a device that stopped reporting must not show healthy."""
        for age in (200, 700):
            entry = coverage_entry(FakeDevice(), FakeStatus(age, {"psu": "OK"}), CFG)
            assert entry["observation"] in ("stale", "unobserved")
            assert entry["health"] == "unknown"

    def test_never_seen_device(self):
        entry = coverage_entry(FakeDevice(), None, CFG)
        assert entry["observation"] == "unobserved"
        assert entry["health"] == "unknown"
        assert entry["last_heartbeat_at"] is None

    def test_observed_critical_stays_critical(self):
        entry = coverage_entry(FakeDevice(), FakeStatus(10, {"psu": "CRITICAL"}), CFG)
        assert entry["observation"] == "observed"
        assert entry["health"] == "critical"
