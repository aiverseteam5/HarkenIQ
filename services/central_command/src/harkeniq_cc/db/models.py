"""Central Command persistence models (SQLAlchemy 2.0, async).

JSON columns use JSONB on PostgreSQL and plain JSON elsewhere so the
same models run on TimescaleDB/Postgres (production) and aiosqlite
(unit tests).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSONVariant = JSON().with_variant(JSONB(), "postgresql")


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class CCSite(Base):
    __tablename__ = "cc_sites"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(64))
    site_name: Mapped[str] = mapped_column(String(255))
    sm_endpoint: Mapped[str] = mapped_column(String(512))
    sm_token: Mapped[str | None] = mapped_column(String(512), nullable=True)
    license_fingerprint: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(32), default="active")
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("tenant_id", "site_name"),)


class CCFleetCache(Base):
    __tablename__ = "cc_fleet_cache"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    site_id: Mapped[str] = mapped_column(ForeignKey("cc_sites.id"))
    agent_id: Mapped[str] = mapped_column(String(255))
    agent_name: Mapped[str] = mapped_column(String(255), default="")
    vendor: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(255), default="")
    # R6: "server" | "switch"
    device_class: Mapped[str] = mapped_column(String(32), default="server")
    observation: Mapped[str] = mapped_column(String(32), default="")
    health: Mapped[str] = mapped_column(String(32), default="")
    subsystems: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    # R4-2 P14/P15: warranty lookup key + firmware inventory
    service_tag: Mapped[str] = mapped_column(String(255), default="")
    firmware: Mapped[list | None] = mapped_column(JSONVariant, nullable=True)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_fleet_cache_site_id", "site_id"),)


class CCApprovalRoute(Base):
    __tablename__ = "cc_approval_routes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    site_id: Mapped[str] = mapped_column(ForeignKey("cc_sites.id"))
    action_id: Mapped[str] = mapped_column(String(64))
    action_type: Mapped[str] = mapped_column(String(64), default="")
    device_agent_id: Mapped[str] = mapped_column(String(255), default="")
    decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    routed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CCUsageSnapshot(Base):
    __tablename__ = "cc_usage_snapshots"

    date: Mapped[str] = mapped_column(String(10), primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("cc_sites.id"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    node_count: Mapped[int] = mapped_column(Integer, default=0)
    agent_versions: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CCAuditLog(Base):
    __tablename__ = "cc_audit_log"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(128))
    subject: Mapped[str] = mapped_column(String(255), default="")
    tenant_id: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    # R4-2 P12: SHA-256 hash chain (harkeniq.audit.chain); one chain per
    # service, seq unique so racing appenders fail instead of forking.
    seq: Mapped[int | None] = mapped_column(nullable=True)
    prev_hash: Mapped[str] = mapped_column(String(64), default="")
    entry_hash: Mapped[str] = mapped_column(String(64), default="")

    __table_args__ = (
        Index("ix_cc_audit_log_ts", "ts"),
        Index("ix_cc_audit_log_tenant_id", "tenant_id"),
        UniqueConstraint("seq", name="uq_cc_audit_log_seq"),
    )


# ---------------------------------------------------------------------------
# Phase 3 tables: approval policies, groups, autonomy budgets
# ---------------------------------------------------------------------------


class CCApprovalGroup(Base):
    """Named group of approvers, optionally linked to Slack/GitHub."""

    __tablename__ = "cc_approval_groups"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    slack_channel: Mapped[str] = mapped_column(String(255), default="")
    github_team: Mapped[str] = mapped_column(String(255), default="")
    required_count: Mapped[int] = mapped_column(Integer, default=1)
    escalation_chain: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    created_by: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("tenant_id", "name"),)


class CCApprovalGroupMember(Base):
    """Membership entry linking a user to an approval group."""

    __tablename__ = "cc_approval_group_members"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    group_id: Mapped[str] = mapped_column(String(32), ForeignKey("cc_approval_groups.id"))
    user_email: Mapped[str] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(32), default="approver")
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CCApprovalPolicy(Base):
    """Configurable approval routing rules per tenant."""

    __tablename__ = "cc_approval_policies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    device_type: Mapped[str] = mapped_column(String(64), default="*")
    action_type: Mapped[str] = mapped_column(String(64), default="*")
    risk_level: Mapped[str] = mapped_column(String(32), default="medium")
    time_window_json: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    approval_mode: Mapped[str] = mapped_column(String(32), default="require_approval")
    required_approvers: Mapped[int] = mapped_column(Integer, default=1)
    group_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("cc_approval_groups.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_by: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CCStopSwitch(Base):
    """QA-022: persisted fleet-wide stop switch (was an in-process dict).

    One row per tenant; survives CC restarts and is pushed to every SM
    via PushPolicy so leases carry it (R-C5).
    """

    __tablename__ = "cc_stop_switch"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    changed_by: Mapped[str] = mapped_column(String(255), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CCAutonomyBudget(Base):
    """Per-tenant, per-device-type autonomy tier and action budget.

    Levels: 0=observe, 1=suggest, 2=batch, 3=autonomous.
    """

    __tablename__ = "cc_autonomy_budgets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(64))
    device_type: Mapped[str] = mapped_column(String(64), default="*")
    level: Mapped[int] = mapped_column(Integer, default=0)
    budget_limit: Mapped[int] = mapped_column(Integer, default=0)
    budget_period: Mapped[str] = mapped_column(String(32), default="monthly")
    actions_used: Mapped[int] = mapped_column(Integer, default=0)
    learning_ramp_config: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("tenant_id", "device_type"),)


class CCOutcomeHistory(Base):
    """Fleet-wide action outcome history for learning (R3b-3, R-C1).

    Populated from FleetSnapshot.outcomes reported by each SM during
    fleet polling. Source of truth for pattern detection.
    """

    __tablename__ = "cc_outcome_history"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    site_id: Mapped[str] = mapped_column(String(32), ForeignKey("cc_sites.id"))
    action_id: Mapped[str] = mapped_column(String(64))
    action_type: Mapped[str] = mapped_column(String(64))
    device_agent_id: Mapped[str] = mapped_column(String(64))
    vendor: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    outcome: Mapped[str] = mapped_column(String(32))  # SUCCESS/PARTIAL/FAILURE/UNKNOWN/ROLLBACK
    fault_resolved: Mapped[bool | None] = mapped_column(nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_outcome_history_type_vendor", "action_type", "vendor"),
        Index("ix_outcome_history_device", "device_agent_id"),
    )


class CCWarranty(Base):
    """Warranty/lifecycle cache keyed by service tag (R4-2 P15).

    A separate table (NOT columns on cc_fleet_cache) because the fleet
    poller rebuilds the cache from scratch every cycle -- enrichment on
    cache rows would be wiped. TTL-refreshed by the warranty loop; also
    the caching layer the vendor API rate limits require.
    """

    __tablename__ = "cc_warranty"

    # R5-2 (A8): tenant-scoped; composite PK so shared-CC deployments
    # can never leak one tenant's warranty data to another.
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True, default="")
    service_tag: Mapped[str] = mapped_column(String(255), primary_key=True)
    vendor: Mapped[str] = mapped_column(String(64), default="")
    service_level: Mapped[str] = mapped_column(String(255), default="")
    start_date: Mapped[str] = mapped_column(String(32), default="")
    end_date: Mapped[str] = mapped_column(String(32), default="")
    source: Mapped[str] = mapped_column(String(32), default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CCCveEntry(Base):
    """Local CVE feed for firmware exposure matching (R4-2 P14).

    Air-gap safe by design: entries are imported from an offline JSON
    bundle by an operator (POST /api/firmware/cve-feed); CC never
    phones home to NVD or vendor feeds.
    """

    __tablename__ = "cc_cve_feed"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # R5-2 (A8): tenant-scoped feed
    tenant_id: Mapped[str] = mapped_column(String(64), default="")
    cve_id: Mapped[str] = mapped_column(String(32))
    vendor: Mapped[str] = mapped_column(String(64), default="*")
    component: Mapped[str] = mapped_column(String(32), default="*")
    #: version-range expression (harkeniq.compliance.versions), e.g. "< 7.10.30.00"
    affected_versions: Mapped[str] = mapped_column(String(255))
    fixed_version: Mapped[str] = mapped_column(String(64), default="")
    severity: Mapped[str] = mapped_column(String(16), default="medium")
    description: Mapped[str] = mapped_column(String(512), default="")
    published: Mapped[str] = mapped_column(String(32), default="")
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "cve_id", "vendor", "component",
                         name="uq_cc_cve_feed_entry"),
    )


class CCFleetPattern(Base):
    """Detected fleet-wide patterns (R3b-3, R-C1).

    Patterns detected by the PatternDetector from aggregated outcomes.
    Types: batch_failure, anomaly, reliability.
    """

    __tablename__ = "cc_fleet_patterns"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # R5-2 (A8): tenant-scoped patterns
    tenant_id: Mapped[str] = mapped_column(String(64), default="")
    pattern_type: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(String(512))
    affected_scope: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    confidence: Mapped[float] = mapped_column(default=0.0)
    evidence: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_fleet_patterns_type_status", "pattern_type", "status"),
        Index("ix_fleet_patterns_tenant", "tenant_id"),
    )


class CCCandidateSkill(Base):
    """SM-generated candidate skills for the R-C1 learning loop (QA-033).

    Ingested from FleetSnapshot.candidate_skills by the fleet poller.
    status: received → cycle_linked (matched to a fleet pattern's learning
    cycle) → promoted (LearningFeedbackTracker promotion criteria met;
    still requires the marketplace human review path to reach agents —
    auto-promotion is a recommendation, never a distribution).
    """

    __tablename__ = "cc_candidate_skills"

    skill_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # R5-2 (A8): tenant-scoped like cc_fleet_patterns
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True, default="")
    site_id: Mapped[str] = mapped_column(String(32), default="")
    yaml_text: Mapped[str] = mapped_column(Text)
    source_device: Mapped[str] = mapped_column(String(64), default="")
    source_component: Mapped[str] = mapped_column(String(128), default="")
    validation_state: Mapped[str] = mapped_column(String(16), default="draft")
    warnings: Mapped[list | None] = mapped_column(JSONVariant, nullable=True)
    dry_run_matches: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(32), default="received")
    cycle_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_candidate_skills_tenant_status", "tenant_id", "status"),
    )


class CCSkillDelivery(Base):
    """Durable dedup ledger for marketplace skill deliveries (R5-2).

    One row per (install event, site): the sync loop never re-pushes a
    delivered install, across restarts.
    """

    __tablename__ = "cc_skill_deliveries"

    install_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    site_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    skill_name: Mapped[str] = mapped_column(String(255), default="")
    skill_version: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(16), default="delivered")
    directives_queued: Mapped[int] = mapped_column(default=0)
    detail: Mapped[str] = mapped_column(String(512), default="")
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CCLearningCycle(Base):
    """The learning PROCESS, made durable (S3, 2026-08-29).

    One row per iteration of the R-C1 loop: a detected pattern, the
    candidate capability it produced, how far that was distributed, the
    measured improvement, and whether promotion was recommended.

    Why durable: the tracker held cycles in memory, so the record of what
    the fleet learned vanished on restart — and cc_candidate_skills.cycle_id
    already pointed at it, leaving a dangling reference. A learning
    substrate whose evidence does not survive a restart cannot support
    "improved future decisions", and no auditor can answer why a capability
    earned promotion. The in-memory tracker stays as the live working set;
    this is the ledger.
    """

    __tablename__ = "cc_learning_cycles"

    cycle_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="")
    pattern_id: Mapped[str] = mapped_column(String(64), default="")
    pattern_type: Mapped[str] = mapped_column(String(32), default="")
    skill_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sites_distributed: Mapped[int] = mapped_column(default=0)
    devices_applied: Mapped[int] = mapped_column(default=0)
    outcomes_before: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    outcomes_after: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    improvement_pct: Mapped[float | None] = mapped_column(nullable=True)
    # Recommended != promoted. Promotion stays governed (marketplace human
    # review); this column records only that the evidence bar was met.
    promotion_recommended: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(32), default="open")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_learning_cycles_tenant_status", "tenant_id", "status"),
    )


class CCLearnedSignal(Base):
    """Durable knowledge derived from a pattern and its outcomes (S3).

    Distinct from a pattern: a PATTERN is an evidence-derived recurring
    relationship detected fleet-wide; a LEARNED SIGNAL is the knowledge
    that relationship yields, projected onto the scope its evidence
    actually supports, and carried forward so it can inform tomorrow's
    attention, diagnosis and (later) an agent's reasoning.

    Scope is evidence-bound, never assumed global: cohort scope comes from
    the pattern's vendor/model, and site scope only from patterns that
    name failing sites (cross_site_batch). Device and tenant scope are NOT
    derived from patterns, because pattern evidence does not support them.

    A signal is knowledge, not authority: nothing here permits an action.
    """

    __tablename__ = "cc_learned_signals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), default="")
    # Stable identity for upsert, so re-detection refreshes rather than
    # duplicating: scope + action + cohort.
    signal_key: Mapped[str] = mapped_column(String(255), default="")
    scope_type: Mapped[str] = mapped_column(String(16), default="cohort")  # cohort|site
    scope_ref: Mapped[str] = mapped_column(String(128), default="")
    action_type: Mapped[str] = mapped_column(String(64), default="")
    vendor: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    statement: Mapped[str] = mapped_column(String(512), default="")
    evidence: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    confidence: Mapped[float] = mapped_column(default=0.0)
    source_pattern_id: Mapped[str] = mapped_column(String(64), default="")
    source_cycle_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    observation_count: Mapped[int] = mapped_column(default=1)
    first_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    last_confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    __table_args__ = (
        Index("ix_learned_signals_tenant_key", "tenant_id", "signal_key", unique=True),
        Index("ix_learned_signals_scope", "tenant_id", "scope_type", "scope_ref"),
    )


class CCIncident(Base):
    """Real incidents at Central Command (S4, 2026-08-29).

    Replaces the critical-health "pseudo-incidents" the fleet API used to
    synthesise. These are the Site Manager's own consolidated incidents,
    projected to the tenant plane with the diagnosis attached, so the
    Console and a future Operational Agent can answer WHY, not just WHAT.

    Hierarchy is preserved deliberately: SM consolidates correlated faults
    into one parent with children (a shared PDU fault is one parent and N
    children). Flattening here would show N incidents for one root cause,
    which is exactly what consolidation exists to prevent.

    Resolution follows D3: the snapshot carries only OPEN incidents, so an
    incident absent from a poll is inferred resolved. No resolution REASON
    is stored — that needs its own evidence and is deliberately deferred.

    `explanation` is the reasoning result. When its provider is "llm" the
    text is model-generated from device telemetry: it is evidence to reason
    ABOUT, never instruction to follow. Consumers get that provenance
    explicitly from the API rather than having to infer it.
    """

    __tablename__ = "cc_incidents"

    incident_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="")
    site_id: Mapped[str] = mapped_column(String(32), default="")
    kind: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(16), default="open")
    title: Mapped[str] = mapped_column(String(512), default="")
    device_agent_id: Mapped[str] = mapped_column(String(64), default="")
    subsystem: Mapped[str] = mapped_column(String(32), default="")
    parent_incident_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[float] = mapped_column(default=0.0)
    inferred: Mapped[bool] = mapped_column(default=False)
    correlation_meta: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    explanation: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    # Set when the incident stops appearing in the site's snapshot (D3
    # absence-inference). The row is kept: an incident that happened is
    # part of the record even after it clears.
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_cc_incidents_tenant_status", "tenant_id", "status"),
        Index("ix_cc_incidents_device", "tenant_id", "device_agent_id"),
    )


class CCSafetyState(Base):
    """Live autonomy safety state per site (S5, 2026-08-29).

    Suppression and error-budget drop-back are the two mechanisms that
    withdraw autonomy WITHOUT a human. Before S5 both lived only inside
    the Site Manager, reachable through its site-token break-glass API,
    so neither the tenant operator nor any future agent could observe a
    demotion that had already happened.

    This is a governance INPUT, not a display cache: `/api/autonomy`
    folds it into each action class's disposition, and an Operational
    Agent must evaluate the same state before it acts.

    One row per site, replaced on each poll. A site whose snapshot did
    not carry safety state is stored with `reported=False` and rendered
    as UNKNOWN — an unobserved safety state must never round down to
    "safe", which is the one direction a governance layer may not err.
    """

    __tablename__ = "cc_safety_state"

    site_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="")
    reported: Mapped[bool] = mapped_column(default=False)
    as_of: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sm_stop_switch: Mapped[bool] = mapped_column(default=False)
    #: [{domain_id, event_family, trigger_reason, device_count, ...}]
    suppressions: Mapped[list | None] = mapped_column(JSONVariant, nullable=True)
    #: [{action_type, success_count, failure_count, total_count,
    #:   min_success_rate, dropped_back, dropped_back_at}]
    error_budgets: Mapped[list | None] = mapped_column(JSONVariant, nullable=True)
    #: {action_type: remaining}; -1 means unlimited
    site_budgets: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    __table_args__ = (Index("ix_cc_safety_tenant", "tenant_id"),)
