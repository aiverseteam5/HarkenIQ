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
