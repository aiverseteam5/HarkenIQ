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

# ---------------------------------------------------------------------------
# A29 (A6-4A): the External Agent API plane
# ---------------------------------------------------------------------------

M_SURFACE_REFUSED = "harkeniq_cc_route_surface_refused_total"

#: Bounded, for the reason A25.11 recorded: `/metrics` is unauthenticated,
#: and interpolating a caller-supplied value into a metric NAME is a way
#: to put a tenant or agent id on a scrape surface. Anything unrecognised
#: collapses to `other`.
SURFACE_REFUSAL_REASONS = frozenset({
    "surface_not_allowed",
    "machine_job_not_bound",
    "machine_only_route",
    "other",
})


def record_surface_refusal(reason: str) -> None:
    """A request refused on species eligibility, by bounded reason.

    Also the REPORT half of A29's migration discipline: the counter tells
    an operator how much real traffic the narrowing turned away, by
    reason, without naming who was turned away.
    """
    if reason not in SURFACE_REFUSAL_REASONS:
        reason = "other"
    _inc(M_SURFACE_REFUSED)
    _inc(f"{M_SURFACE_REFUSED}_{reason}")


# ---------------------------------------------------------------------------
# A30.31 (A6-4B0c): every machine read the meter charges, by declared job
# ---------------------------------------------------------------------------

M_MACHINE_READ_METERED = "harkeniq_cc_machine_reads_metered_total"

#: The label for a machine request refused on a route that is not on the
#: plane at all: it declares no job to be counted under.
OFF_PLANE = "off_plane"


def _read_metered_jobs() -> frozenset[str]:
    """The machine jobs the read window meters, from the ONE declaration."""
    from harkeniq_cc.route_contract import JOB_METER, METER_READ

    return frozenset(job for job, meter in JOB_METER.items() if meter == METER_READ)


#: Bounded, and DERIVED from `JOB_METER` so a new read job cannot be
#: counted as `other` without anyone noticing. The job is a ROUTE fact
#: from the declaration, never a caller value; no tenant, agent, site,
#: device or path is a label here (A25.11).
MACHINE_READ_METER_LABELS: frozenset[str] = (
    _read_metered_jobs() | {OFF_PLANE, "other"}
)


def record_machine_read_metered(job: str) -> None:
    """One machine request charged to the read window, by the route's job.

    Moves on EVERY durable charge -- a 429 included, since the counter
    moves before the limit is compared -- so across the service it mirrors
    the durable read count exactly. The durable row attributes a charge to
    its tenant, agent and window; this attributes it to the job, which the
    row has no column for.
    """
    if job not in MACHINE_READ_METER_LABELS:
        job = "other"
    _inc(M_MACHINE_READ_METERED)
    _inc(f"{M_MACHINE_READ_METERED}_{job}")

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
    # A29 (A6-4A). Registered with EVERY bounded reason, not just the
    # base: `MetricsRegistry.inc` silently IGNORES an unregistered name,
    # so a suffixed counter that is only incremented never reaches
    # /metrics. Each reason set below is closed, so enumerating it here
    # is finite by construction and can never become an unbounded label.
    registry.counter(
        M_SURFACE_REFUSED,
        "Requests refused because the principal species may not use the route",
    )
    for _reason in sorted(SURFACE_REFUSAL_REASONS):
        registry.counter(
            f"{M_SURFACE_REFUSED}_{_reason}",
            f"Route-surface refusals: {_reason}",
        )
    # The same defect, found next door while fixing the above and fixed
    # with it: A25 increments these two suffixed families and registered
    # neither, so `correlation_total{join}` and the per-reason refusal
    # counts have been silent since A6-2. `record_correlation`'s whole
    # stated purpose (A25.1) is retiring the legacy join BY MEASUREMENT,
    # which a counter that never appears cannot do.
    for _join in sorted(CORRELATION_JOINS):
        registry.counter(
            f"{M_CORRELATION}_{_join}", f"Settlements joined: {_join}",
        )
    for _reason in sorted(READ_REFUSAL_REASONS):
        registry.counter(
            f"{M_READ_REFUSED}_{_reason}",
            f"Machine status read refusals: {_reason}",
        )
    # A30.31 (A6-4B0c): registered with every bounded label for the reason
    # the A29 block above records -- an unregistered suffix is silently
    # dropped by `MetricsRegistry.inc`.
    registry.counter(
        M_MACHINE_READ_METERED,
        "Machine requests charged to the read window",
    )
    for _job in sorted(MACHINE_READ_METER_LABELS):
        registry.counter(
            f"{M_MACHINE_READ_METERED}_{_job}",
            f"Machine requests charged to the read window: {_job}",
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

