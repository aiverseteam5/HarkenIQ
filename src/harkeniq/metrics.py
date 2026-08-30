"""Prometheus metrics and health check infrastructure (R4-0 Phase 4).

Lightweight metrics collection using a simple in-process registry.
No external dependency on prometheus_client -- just a dict-based
registry that exports Prometheus text format on demand.

Provides:
  - MetricsRegistry: counter/gauge/histogram collection
  - HealthChecker: detailed health probe with DB/downstream checks
  - Text export for /metrics endpoint
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class MetricType(Enum):
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


@dataclass
class Metric:
    name: str
    help_text: str
    type: MetricType
    value: float = 0.0
    labels: dict[str, str] = field(default_factory=dict)
    # Histogram buckets
    _buckets: dict[float, int] = field(default_factory=dict)
    _sum: float = 0.0
    _count: int = 0


class MetricsRegistry:
    """Simple in-process metrics registry with Prometheus text export."""

    def __init__(self, service: str = "") -> None:
        self._service = service
        self._metrics: dict[str, Metric] = {}
        self._labeled: dict[str, dict[str, Metric]] = defaultdict(dict)

    def counter(self, name: str, help_text: str = "") -> None:
        """Register a counter metric."""
        if name not in self._metrics:
            self._metrics[name] = Metric(
                name=name, help_text=help_text, type=MetricType.COUNTER,
            )

    def gauge(self, name: str, help_text: str = "") -> None:
        """Register a gauge metric."""
        if name not in self._metrics:
            self._metrics[name] = Metric(
                name=name, help_text=help_text, type=MetricType.GAUGE,
            )

    def inc(self, name: str, value: float = 1.0) -> None:
        """Increment a counter or gauge."""
        m = self._metrics.get(name)
        if m:
            m.value += value

    def set_gauge(self, name: str, value: float) -> None:
        """Set a gauge to a specific value."""
        m = self._metrics.get(name)
        if m:
            m.value = value

    def observe(self, name: str, value: float) -> None:
        """Record an observation (for histogram-like tracking)."""
        m = self._metrics.get(name)
        if m:
            m._sum += value
            m._count += 1

    def get(self, name: str) -> Optional[float]:
        """Get current value of a metric."""
        m = self._metrics.get(name)
        return m.value if m else None

    def export_text(self) -> str:
        """Export all metrics in Prometheus text exposition format."""
        lines: list[str] = []
        for name, m in sorted(self._metrics.items()):
            if m.help_text:
                lines.append(f"# HELP {name} {m.help_text}")
            lines.append(f"# TYPE {name} {m.type.value}")
            if m.type == MetricType.HISTOGRAM:
                lines.append(f'{name}_sum {m._sum}')
                lines.append(f'{name}_count {m._count}')
            else:
                lines.append(f"{name} {m.value}")
        return "\n".join(lines) + "\n"


# -- Shared registry for HarkenIQ services --------------------------------

_registry = MetricsRegistry()


def get_registry() -> MetricsRegistry:
    """Get the global metrics registry."""
    return _registry


def init_common_metrics(service: str) -> MetricsRegistry:
    """Initialize common metrics for a HarkenIQ service."""
    r = get_registry()
    r._service = service
    r.counter("harkeniq_http_requests_total", "Total HTTP requests")
    r.counter("harkeniq_http_errors_total", "Total HTTP error responses")
    r.gauge("harkeniq_up", "Whether the service is running (1=up)")
    r.set_gauge("harkeniq_up", 1.0)
    r.gauge("harkeniq_start_time_seconds", "Service start time (unix)")
    r.set_gauge("harkeniq_start_time_seconds", time.time())
    return r


# -- Health Check ----------------------------------------------------------


@dataclass
class HealthStatus:
    """Result of a health check."""

    healthy: bool
    service: str
    checks: dict[str, bool] = field(default_factory=dict)
    details: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "healthy": self.healthy,
            "service": self.service,
            "checks": self.checks,
            "details": self.details,
        }


class HealthChecker:
    """Configurable health check with pluggable probes."""

    def __init__(self, service: str) -> None:
        self._service = service
        self._probes: dict[str, Callable] = {}

    def add_probe(self, name: str, probe: Callable) -> None:
        """Add a health probe. Probe should return True (healthy) or False."""
        self._probes[name] = probe

    async def check(self) -> HealthStatus:
        """Run all probes and return aggregated health status."""
        checks: dict[str, bool] = {}
        details: dict[str, str] = {}
        for name, probe in self._probes.items():
            try:
                result = probe()
                if hasattr(result, "__await__"):
                    result = await result
                checks[name] = bool(result)
            except Exception as e:
                checks[name] = False
                details[name] = str(e)

        return HealthStatus(
            healthy=all(checks.values()) if checks else True,
            service=self._service,
            checks=checks,
            details=details,
        )


# -- Service mounting (E0.3, 2026-08-30) -----------------------------------
#
# The registry above shipped with R4-0 and had NO callers: nothing built
# one, nothing incremented it, and no service exposed /metrics. All three
# services had real /healthz, so the platform could say whether it was
# alive and nothing about what it was doing.
#
# The helper below builds a registry PER APP rather than using the module
# global, because two services sharing one process (every test run) would
# otherwise share counters and report each other's traffic.


def mount_metrics(app, service: str):
    """Give a FastAPI app its own registry, a counting middleware, and /metrics.

    Returns the registry so a service can register metrics of its own.

    `/metrics` is unauthenticated, like `/healthz`, and carries only
    service-level counters -- request totals, error totals, up, start
    time. No tenant identifiers and no per-tenant series: a scrape
    endpoint is not a place to leak who a customer is.
    """
    from fastapi import Request
    from fastapi.responses import PlainTextResponse

    registry = MetricsRegistry(service)
    registry.counter("harkeniq_http_requests_total", "Total HTTP requests")
    registry.counter("harkeniq_http_errors_total", "Total HTTP error responses")
    registry.gauge("harkeniq_up", "Whether the service is running (1=up)")
    registry.set_gauge("harkeniq_up", 1.0)
    registry.gauge("harkeniq_start_time_seconds", "Service start time (unix)")
    registry.set_gauge("harkeniq_start_time_seconds", time.time())
    app.state.metrics = registry

    @app.middleware("http")
    async def _count_requests(request: Request, call_next):
        # Counting happens here rather than at each route so a route added
        # later cannot be silently unmeasured. /metrics counts itself,
        # which is honest: a scrape is a request.
        try:
            response = await call_next(request)
        except Exception:
            registry.inc("harkeniq_http_requests_total")
            registry.inc("harkeniq_http_errors_total")
            raise
        registry.inc("harkeniq_http_requests_total")
        if response.status_code >= 500:
            registry.inc("harkeniq_http_errors_total")
        return response

    @app.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
    async def metrics() -> str:
        return registry.export_text()

    return registry
