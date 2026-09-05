"""A25.6: the counters A6-2 needs, and the rules about what they may say.

Two constraints shape every name below.

**No tenant identifiers, ever.** `/metrics` is unauthenticated, like
`/healthz`, and a scrape endpoint is not a place to leak who a customer
is. So these are service-level counters with bounded, non-identifying
labels -- an outcome word, a refusal reason -- and never a tenant, an
agent id, a site or a device.

**Telemetry may never change behaviour.** Every recorder here is
best-effort and swallows its own failure. A registry that is absent, or a
counter that was never registered, must not stop a proposal from settling
or a read from being answered. That is the one place in this codebase
where swallowing an exception is the correct thing to do, and it is
deliberate rather than convenient.

What these are FOR, beyond dashboards: A25.1 retires the legacy
correlation path by measurement rather than by guess, so
`correlation_total{join="legacy"}` reaching zero across a release is the
evidence that permits the fallback to be deleted. And
`terminal_correlation_failures_total` is the counter that would have
surfaced the settle defect this slice exists to fix -- a dispatched
proposal whose outcome never arrives is invisible in every other view.
"""

from __future__ import annotations

from typing import Any, Optional

# ---------------------------------------------------------------------------
# Names. One place, so a producer and a dashboard cannot drift.
# ---------------------------------------------------------------------------

M_CORRELATION = "harkeniq_cc_settle_correlation_total"
M_TERMINAL_FAILURE = "harkeniq_cc_terminal_correlation_failures_total"
M_STATUS_READ = "harkeniq_cc_agent_status_reads_total"
M_READ_REFUSED = "harkeniq_cc_agent_status_read_refusals_total"
M_READ_RATE_LIMITED = "harkeniq_cc_agent_status_read_rate_limited_total"
M_CROSS_AGENT = "harkeniq_cc_agent_cross_agent_attempts_total"
M_CROSS_TENANT = "harkeniq_cc_agent_cross_tenant_attempts_total"
M_RECEIPT_NARROWED = "harkeniq_cc_agent_receipt_narrowed_total"

#: The process-wide registry, set by `create_app`. A module-level handle
#: exists only so a background loop that holds no app can still count;
#: the registry itself is per-app, which is the property E0.3 wanted.
_registry: Optional[Any] = None


def register_a6_metrics(registry: Any) -> None:
    """Register A6-2's counters on this app's registry."""
    global _registry  # noqa: PLW0603

    _registry = registry
    registry.counter(M_CORRELATION, "Proposal settlements by join type")
    registry.counter(
        M_TERMINAL_FAILURE,
        "Dispatched proposals whose outcome never correlated",
    )
    registry.counter(M_STATUS_READ, "Machine status reads served")
    registry.counter(M_READ_REFUSED, "Machine status reads refused")
    registry.counter(M_READ_RATE_LIMITED, "Machine status reads rate limited")
    registry.counter(
        M_CROSS_AGENT, "Attempts by an agent to read another agent"
    )
    registry.counter(
        M_CROSS_TENANT, "Attempts to read across a tenant boundary"
    )
    registry.counter(
        M_RECEIPT_NARROWED,
        "Receipts served narrowed because current authority was absent",
    )


def _inc(name: str, value: float = 1.0) -> None:
    """Increment, or do nothing at all. Never raise into a caller."""
    try:
        if _registry is not None:
            _registry.inc(name, value)
    except Exception:  # noqa: BLE001 - telemetry must never change behaviour
        pass


#: Bounded for the same reason as the refusal reasons above.
CORRELATION_JOINS: frozenset[str] = frozenset({"exact", "legacy"})


def record_correlation(join: str) -> None:
    """How a settlement was joined: `exact` or `legacy` (A25.1)."""
    if join not in CORRELATION_JOINS:
        return
    _inc(M_CORRELATION)
    _inc(f"{M_CORRELATION}_{join}")


def record_terminal_failure(count: int = 1) -> None:
    """A dispatched proposal that has not correlated within its window."""
    _inc(M_TERMINAL_FAILURE, float(count))


def record_status_read(narrowed: bool = False) -> None:
    _inc(M_STATUS_READ)
    if narrowed:
        _inc(M_RECEIPT_NARROWED)


#: Every reason a read may be refused, and the ONLY strings that may
#: reach a metric name. Bounded on purpose: these recorders build a
#: series name by interpolation, so an unbounded reason would be
#: unbounded cardinality -- and a caller that passed an agent id, a site
#: or a tenant would put an identifier into an unauthenticated scrape
#: endpoint. An unknown reason is counted as `other` rather than
#: rejected, because telemetry must never change behaviour.
READ_REFUSAL_REASONS: frozenset[str] = frozenset({
    "cross_agent",
    "cross_tenant",
    "not_found",
    "not_machine",
    "permission",
    "rate_limited",
    "other",
})


def record_read_refusal(reason: str) -> None:
    """A refused read, by bounded reason -- never by who was refused."""
    if reason not in READ_REFUSAL_REASONS:
        reason = "other"
    _inc(M_READ_REFUSED)
    _inc(f"{M_READ_REFUSED}_{reason}")
    if reason == "cross_agent":
        _inc(M_CROSS_AGENT)
    elif reason == "cross_tenant":
        _inc(M_CROSS_TENANT)


def record_read_rate_limited() -> None:
    _inc(M_READ_RATE_LIMITED)
