"""Tests for health checks and Prometheus metrics (R4-0 Phase 4)."""

from __future__ import annotations

import pytest

from harkeniq.metrics import (
    HealthChecker,
    HealthStatus,
    MetricsRegistry,
    init_common_metrics,
)


class TestMetricsRegistry:
    def test_counter(self):
        r = MetricsRegistry()
        r.counter("test_counter", "A test counter")
        r.inc("test_counter")
        r.inc("test_counter", 5.0)
        assert r.get("test_counter") == 6.0

    def test_gauge(self):
        r = MetricsRegistry()
        r.gauge("test_gauge", "A test gauge")
        r.set_gauge("test_gauge", 42.0)
        assert r.get("test_gauge") == 42.0

    def test_observe(self):
        r = MetricsRegistry()
        r.gauge("test_hist", "A test histogram")
        r.observe("test_hist", 0.5)
        r.observe("test_hist", 1.5)
        m = r._metrics["test_hist"]
        assert m._count == 2
        assert m._sum == pytest.approx(2.0)

    def test_get_nonexistent(self):
        r = MetricsRegistry()
        assert r.get("nope") is None

    def test_export_text(self):
        r = MetricsRegistry()
        r.counter("requests_total", "Total requests")
        r.inc("requests_total", 10)
        text = r.export_text()
        assert "# HELP requests_total Total requests" in text
        assert "# TYPE requests_total counter" in text
        assert "requests_total 10" in text


class TestInitCommonMetrics:
    def test_initializes_common(self):
        r = init_common_metrics("sm")
        assert r.get("harkeniq_up") == 1.0
        assert r.get("harkeniq_start_time_seconds") > 0
        text = r.export_text()
        assert "harkeniq_up" in text


class TestHealthChecker:
    async def test_all_healthy(self):
        checker = HealthChecker("sm")
        checker.add_probe("db", lambda: True)
        checker.add_probe("grpc", lambda: True)
        status = await checker.check()
        assert status.healthy is True
        assert status.checks["db"] is True
        assert status.checks["grpc"] is True

    async def test_one_unhealthy(self):
        checker = HealthChecker("sm")
        checker.add_probe("db", lambda: True)
        checker.add_probe("grpc", lambda: False)
        status = await checker.check()
        assert status.healthy is False
        assert status.checks["grpc"] is False

    async def test_probe_exception(self):
        checker = HealthChecker("sm")
        def failing_probe():
            raise ConnectionError("DB down")
        checker.add_probe("db", failing_probe)
        status = await checker.check()
        assert status.healthy is False
        assert status.checks["db"] is False
        assert "DB down" in status.details["db"]

    async def test_async_probe(self):
        async def async_probe():
            return True
        checker = HealthChecker("sm")
        checker.add_probe("async_check", async_probe)
        status = await checker.check()
        assert status.healthy is True

    async def test_no_probes_is_healthy(self):
        checker = HealthChecker("sm")
        status = await checker.check()
        assert status.healthy is True

    def test_health_status_to_dict(self):
        status = HealthStatus(
            healthy=True,
            service="sm",
            checks={"db": True},
            details={},
        )
        d = status.to_dict()
        assert d["healthy"] is True
        assert d["service"] == "sm"
        assert d["checks"]["db"] is True
