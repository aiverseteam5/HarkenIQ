"""Site Manager persistence models (SQLAlchemy 2.0, async).

JSON columns use JSONB on PostgreSQL and plain JSON elsewhere so the
same models run on TimescaleDB/Postgres (production) and aiosqlite
(unit tests). ``verdict_reports`` and ``heartbeats`` become Timescale
hypertables when the extension is present (see migrations).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
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


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Rack(Base):
    __tablename__ = "racks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.id"))
    name: Mapped[str] = mapped_column(String(255))
    row_label: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (UniqueConstraint("site_id", "name"),)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.id"))
    agent_id: Mapped[str] = mapped_column(String(255), unique=True)
    agent_name: Mapped[str] = mapped_column(String(255), default="")
    vendor: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(255), default="")
    service_tag: Mapped[str] = mapped_column(String(255), default="")
    bmc_location: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    # R4-2 P14: firmware inventory [{component, name, version}] (R-AGENT-17)
    firmware: Mapped[list | None] = mapped_column(JSONVariant, nullable=True)
    rack_id: Mapped[str | None] = mapped_column(ForeignKey("racks.id"), nullable=True)
    rack_suggestion: Mapped[str | None] = mapped_column(String(255), nullable=True)
    peers: Mapped[list | None] = mapped_column(JSONVariant, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FaultDomain(Base):
    __tablename__ = "fault_domains"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.id"))
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(16))  # power | cooling | network
    status: Mapped[str] = mapped_column(String(16), default="inferred")  # inferred | confirmed
    confidence: Mapped[float] = mapped_column(Float, default=0.6)
    source: Mapped[str] = mapped_column(String(32), default="inference")
    # inference | operator | yaml_import
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confirmed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("site_id", "name"),)


class DomainMembership(Base):
    __tablename__ = "domain_memberships"

    domain_id: Mapped[str] = mapped_column(ForeignKey("fault_domains.id"), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), primary_key=True)
    added_by: Mapped[str] = mapped_column(String(255), default="")
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VerdictReportRow(Base):
    __tablename__ = "verdict_reports"

    # Hypertable-compatible: composite PK includes the time column.
    time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    device_id: Mapped[str] = mapped_column(String(32), index=True)
    sensor_id: Mapped[str] = mapped_column(String(255))
    skill_name: Mapped[str] = mapped_column(String(255))
    severity: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[list | None] = mapped_column(JSONVariant, nullable=True)

    __table_args__ = (Index("ix_verdicts_device_time", "device_id", "time"),)


class HeartbeatRow(Base):
    __tablename__ = "heartbeats"

    time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    device_id: Mapped[str] = mapped_column(String(32), index=True)
    state: Mapped[str] = mapped_column(String(32))
    health_summary: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    peer_status: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)


class AgentStatus(Base):
    __tablename__ = "agent_status"

    device_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_state: Mapped[str] = mapped_column(String(32), default="")
    last_health: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    last_peer_status: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)


class DeviceSubsystemState(Base):
    __tablename__ = "device_subsystem_state"

    device_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    subsystem: Mapped[str] = mapped_column(String(32), primary_key=True)
    severity: Mapped[str] = mapped_column(String(16))
    onset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.id"))
    kind: Mapped[str] = mapped_column(String(32))
    # device | shared_power | rack_thermal | batch_component | network_ambiguity
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | resolved
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("incidents.id"), nullable=True)
    device_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    domain_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    subsystem: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    inferred: Mapped[bool] = mapped_column(Boolean, default=False)
    title: Mapped[str] = mapped_column(String(512), default="")
    correlation_meta: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    explanation: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)  # R3b-1 C1: LLM enrichment
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_incidents_status_kind", "status", "kind"),)


class ActionRow(Base):
    __tablename__ = "actions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"))
    agent_action_id: Mapped[str] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(64))
    sensor_id: Mapped[str] = mapped_column(String(255), default="")
    skill_name: Mapped[str] = mapped_column(String(255), default="")
    verdict_severity: Mapped[str] = mapped_column(String(16), default="")
    params: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    # pending | approved | denied | delivered | completed | failed | superseded
    proposed_at: Mapped[str] = mapped_column(String(64), default="")
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    incident_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("device_id", "agent_action_id"),)


class AgentIdentityRow(Base):
    """R3a: per-agent Ed25519 public key + SM-issued certificate (A2.4)."""

    __tablename__ = "agent_identities"

    agent_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    public_key_pem: Mapped[bytes] = mapped_column(Text)
    certificate: Mapped[bytes | None] = mapped_column(Text, nullable=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ActionOutcomeRow(Base):
    """R3b-1 C8: persisted action outcomes for knowledge base."""

    __tablename__ = "sm_action_outcomes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    action_id: Mapped[str] = mapped_column(String(255))
    action_type: Mapped[str] = mapped_column(String(64))
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    outcome: Mapped[str] = mapped_column(String(32))  # SUCCESS/PARTIAL/FAILURE/UNKNOWN/ROLLBACK
    fault_resolved: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    pre_state: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    post_state: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    side_effects: Mapped[list | None] = mapped_column(JSONVariant, nullable=True)
    operator_override: Mapped[bool] = mapped_column(Boolean, default=False)
    override_reason: Mapped[str] = mapped_column(String(512), default="")
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reported_to_cc: Mapped[bool] = mapped_column(Boolean, default=False)  # R3b-3: watermark

    __table_args__ = (Index("ix_outcomes_device_type", "device_id", "action_type"),)


class ErrorBudgetRow(Base):
    """R3b-1 C8: persisted error budget state."""

    __tablename__ = "sm_error_budgets"

    action_type: Mapped[str] = mapped_column(String(64), primary_key=True)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    min_success_rate: Mapped[float] = mapped_column(Float, default=0.95)
    dropped_back: Mapped[bool] = mapped_column(Boolean, default=False)
    dropped_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLogRow(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(64))
    subject: Mapped[str] = mapped_column(String(255), default="")
    detail: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    # R4-2 P12: SHA-256 hash chain (harkeniq.audit.chain); one chain per
    # service, seq 1..N, unique so a racing appender fails instead of
    # forking the chain.
    seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prev_hash: Mapped[str] = mapped_column(String(64), default="")
    entry_hash: Mapped[str] = mapped_column(String(64), default="")

    __table_args__ = (UniqueConstraint("seq", name="uq_audit_log_seq"),)


class FirmwareCampaign(Base):
    """Staged, blast-radius-aware firmware rollout (R4-3 P19, OQ-21).

    Lifecycle: draft -> approved (explicit human sign-off, audited) ->
    running -> completed | halted. A campaign halts on the FIRST device
    failure -- after rolling that device back blue-green -- and never
    auto-continues past a failure.
    """

    __tablename__ = "firmware_campaigns"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.id"))
    component: Mapped[str] = mapped_column(String(32), default="bmc")
    vendor: Mapped[str] = mapped_column(String(64), default="")
    target_version: Mapped[str] = mapped_column(String(64))
    image_uri: Mapped[str] = mapped_column(String(512), default="")
    image_sha256: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="draft")
    current_wave: Mapped[int] = mapped_column(Integer, default=0)
    wave_count: Mapped[int] = mapped_column(Integer, default=0)
    max_wave_size: Mapped[int] = mapped_column(Integer, default=5)
    created_by: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_by: Mapped[str] = mapped_column(String(255), default="")
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    halt_reason: Mapped[str] = mapped_column(String(512), default="")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FirmwareCampaignTarget(Base):
    """Per-device state within a firmware campaign."""

    __tablename__ = "firmware_campaign_targets"

    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("firmware_campaigns.id"), primary_key=True
    )
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.id"), primary_key=True
    )
    wave_index: Mapped[int] = mapped_column(Integer, default=0)
    #: pending | completed | failed | rolled_back | skipped
    status: Mapped[str] = mapped_column(String(16), default="pending")
    pre_version: Mapped[str] = mapped_column(String(64), default="")
    post_version: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str] = mapped_column(String(512), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DirectedDirective(Base):
    """SM-initiated work delivered to an agent on its poll (R5).

    The delivery inversion: agents dial out to the SM; the SM cannot
    dial agents. Directed work (firmware campaign steps, marketplace
    skill installs) is queued here, handed out on PollDirectives, and
    closed by ReportDirectiveResult.

    Lifecycle: pending -> delivered -> completed | failed. A directive
    that stays delivered past its deadline is treated as failed by the
    waiting caller (e.g. the firmware updater's timeout).
    """

    __tablename__ = "sm_directives"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"))
    kind: Mapped[str] = mapped_column(String(32))  # action | skill_install
    action_type: Mapped[str] = mapped_column(String(64), default="")
    params: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    skill_id: Mapped[str] = mapped_column(String(255), default="")
    skill_version: Mapped[str] = mapped_column(String(32), default="")
    yaml_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    tier: Mapped[str] = mapped_column(String(32), default="")
    validation_state: Mapped[str] = mapped_column(String(32), default="")
    issued_by: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    result_detail: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_sm_directives_device_status", "device_id", "status"),
    )
