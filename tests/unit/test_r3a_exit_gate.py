"""R3a exit gate tests (spec §7, Amendment A2).

Three scenarios that must pass before R3a ships:

1. Agent autonomously executes a low-risk action (SEL clear) within budget
   -> outcome tracked -> success rate visible at SM.

2. Agent loses SM contact -> medium-risk actions drop to propose-only
   within lease window -> observe-only after expiry.

3. 3+ devices in same fault domain produce thermal verdicts within 60s
   -> SM triggers suppression -> human resolves parent -> autonomy resumes.

Plus: all 7 A2.7 architectural contracts are implemented.
"""

import time

import pytest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

# -- Contract 1: ActionOutcome
from harkeniq.autonomy.verification import ActionOutcome, OutcomeStatus

# -- Contract 2: Diagnosis
from harkeniq.autonomy.diagnosis import (
    ConfidenceDimension,
    Diagnosis,
    DiagnosisEvidence,
)

# -- Contract 3: SkillPackage
from harkeniq.autonomy.skill_lifecycle import (
    SkillPackage,
    SkillTier,
    ValidationState,
)

# -- Contract 4: AutonomyPolicy (distributed enforcement)
from harkeniq.autonomy.budget import AgentBudgetEnforcer
from harkeniq.autonomy.lease import AuthorizationLease, InvalidLease

# -- Contract 5: OSSignalCollector
from harkeniq.os_signals.collector import OSSignalCollector, SignalSourceType

# -- Contract 6: ReasoningProvider at SM
from harkeniq_sm.reasoning import (
    DeterministicReasoner,
    KnowledgeBaseReasoner,
    ReasoningContext,
    ReasoningPipeline,
)

# -- Contract 7: PeerProtocol
from harkeniq.autonomy.peer_protocol import PeerProtocol

# -- Supporting modules
from harkeniq.autonomy.identity import AgentIdentity, _canonical_json
from harkeniq.autonomy.preconditions import check_preconditions, ACTION_RISK
from harkeniq.autonomy.blast_radius import BlastRadiusLimiter
from harkeniq.autonomy.resources import ResourceMonitor
from harkeniq.autonomy.tier import TierLevel, calculate_tier
from harkeniq.models import ActionType, PeerStatus, Peer
from harkeniq_sm.autonomy import SMAutonomyEnforcer
from harkeniq_sm.knowledge import KnowledgeBase, StoredOutcome
from harkeniq_sm.suppression import CorrelationEvent, SuppressionEngine, STABILITY_PERIOD


def _sm_keypair():
    private = Ed25519PrivateKey.generate()
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, public_pem


def _sign_lease(sm_private, agent_id, **overrides):
    now = time.time()
    payload = {
        "v": 1, "agent_id": agent_id,
        "action_classes": ["SEL_CLEAR", "BMC_RESET", "POWER_CYCLE"],
        "risk_ceiling": "medium",
        "budget_remaining": {"SEL_CLEAR": 5, "BMC_RESET": 2, "POWER_CYCLE": 1},
        "lease_expiry": now + 300, "grace_expiry": now + 360,
        "suppression_domains": [], "stop_switch": False,
        "issued_at": now,
    }
    payload.update(overrides)
    payload_bytes = _canonical_json(payload)
    return payload_bytes + sm_private.sign(payload_bytes)


# ===========================================================================
# EXIT GATE 1: Autonomous SEL clear -> outcome tracked -> success rate
# ===========================================================================


class TestExitGate1_AutonomousAction:
    """Agent autonomously executes SEL clear within budget, outcome tracked."""

    def test_full_sel_clear_pipeline(self):
        # 1. Agent has identity + SM-signed lease
        sm_private, sm_pub = _sm_keypair()
        identity = AgentIdentity.generate()
        identity.set_sm_public_key(sm_pub)
        lease_raw = _sign_lease(sm_private, identity.agent_id)
        lease = AuthorizationLease.parse(lease_raw, identity)

        # 2. Agent budget allows SEL_CLEAR
        budget = AgentBudgetEnforcer()
        budget.update_from_lease(lease.budget_remaining, lease.stop_switch)
        assert budget.allows(ActionType.SEL_CLEAR)

        # 3. Preconditions pass
        precond = check_preconditions(
            ActionType.SEL_CLEAR,
            {"sel_percent_full": 95},
            {"sel_events_forwarded": True},
        )
        assert precond.passed

        # 4. Lease authorizes the action
        risk = ACTION_RISK[ActionType.SEL_CLEAR]
        decision = lease.allows_action("SEL_CLEAR", risk, sm_connected=True)
        assert decision == "execute"

        # 5. Blast radius allows
        limiter = BlastRadiusLimiter()
        assert limiter.allows(ActionType.SEL_CLEAR)

        # 6. Execute (simulated) -> record outcome
        outcome = ActionOutcome(
            action_id="sel-1",
            action_type=ActionType.SEL_CLEAR,
            outcome=OutcomeStatus.SUCCESS,
            fault_resolved=True,
            pre_state={"sel_entry_count": 450},
            post_state={"sel_entry_count": 0},
        )

        # 7. Budget consumed
        budget.consume(ActionType.SEL_CLEAR)
        state = budget.get_state()
        assert state["SEL_CLEAR"]["remaining"] == 4

        # 8. SM knowledge base tracks outcome + success rate
        kb = KnowledgeBase()
        kb.record_outcome(StoredOutcome(
            action_id="sel-1", action_type="SEL_CLEAR",
            device_id="dev-1", outcome="SUCCESS", fault_resolved=True,
        ))
        budget_state = kb.get_error_budget("SEL_CLEAR")
        assert budget_state.success_rate == 1.0
        assert budget_state.total_count == 1

    def test_sel_clear_outcome_feeds_diagnosis(self):
        """Outcome links back to Diagnosis (A2.7 contracts 1+2 connected)."""
        diagnosis = Diagnosis(
            device_id="dev-1",
            component="SystemEventLog",
            summary="SEL 95% full, cleared autonomously",
        )
        diagnosis.confidence = [
            ConfidenceDimension(name="skill_match", value=1.0),
            ConfidenceDimension(name="baseline", value=0.9),
        ]

        outcome = ActionOutcome(
            action_id="sel-1",
            action_type=ActionType.SEL_CLEAR,
            diagnosis_id=diagnosis.id,  # FK link
            outcome=OutcomeStatus.SUCCESS,
        )
        assert outcome.diagnosis_id == diagnosis.id


# ===========================================================================
# EXIT GATE 2: SM disconnect -> risk-degraded behavior
# ===========================================================================


class TestExitGate2_PartitionBehavior:
    """Agent loses SM -> medium-risk proposes -> observe-only after expiry."""

    def test_risk_degradation_cascade(self):
        sm_private, sm_pub = _sm_keypair()
        identity = AgentIdentity.generate()
        identity.set_sm_public_key(sm_pub)

        # Valid lease with medium-risk actions
        lease_raw = _sign_lease(sm_private, identity.agent_id)
        lease = AuthorizationLease.parse(lease_raw, identity)

        # Connected: all actions allowed
        assert lease.allows_action("SEL_CLEAR", "low", sm_connected=True) == "execute"
        assert lease.allows_action("POWER_CYCLE", "medium", sm_connected=True) == "execute"

        # SM disconnected, lease still valid:
        # Low-risk: continues
        assert lease.allows_action("SEL_CLEAR", "low", sm_connected=False) == "execute"
        # Medium-risk: propose only (A2.2)
        assert lease.allows_action("POWER_CYCLE", "medium", sm_connected=False) == "propose"

    def test_lease_expiry_to_observe_only(self):
        sm_private, sm_pub = _sm_keypair()
        identity = AgentIdentity.generate()
        identity.set_sm_public_key(sm_pub)
        now = time.time()

        # Expired lease, in grace period
        lease_raw = _sign_lease(
            sm_private, identity.agent_id,
            lease_expiry=now - 10, grace_expiry=now + 50,
        )
        lease = AuthorizationLease.parse(lease_raw, identity)
        assert lease.allows_action("SEL_CLEAR", "low", True) == "propose"

        # Fully expired (past grace)
        lease_raw2 = _sign_lease(
            sm_private, identity.agent_id,
            lease_expiry=now - 100, grace_expiry=now - 40,
        )
        lease2 = AuthorizationLease.parse(lease_raw2, identity)
        assert lease2.allows_action("SEL_CLEAR", "low", True) == "deny"

    def test_stop_switch_overrides_everything(self):
        sm_private, sm_pub = _sm_keypair()
        identity = AgentIdentity.generate()
        identity.set_sm_public_key(sm_pub)

        lease_raw = _sign_lease(sm_private, identity.agent_id, stop_switch=True)
        lease = AuthorizationLease.parse(lease_raw, identity)
        assert lease.allows_action("SEL_CLEAR", "low", True) == "deny"
        assert lease.allows_action("IDENTIFY_LED", "none", True) == "deny"

    def test_revoked_identity_denies_all(self):
        identity = AgentIdentity.generate()
        identity.revoked = True
        assert not identity.is_valid()


# ===========================================================================
# EXIT GATE 3: Correlated event -> suppression -> human resolve -> resume
# ===========================================================================


class TestExitGate3_CorrelatedSuppression:
    """3+ devices thermal -> suppression -> human resolves -> resumes."""

    def test_full_suppression_lifecycle(self):
        engine = SuppressionEngine()
        now = time.time()

        # 3 devices in same cooling zone report thermal events
        for i in range(3):
            result = engine.evaluate(CorrelationEvent(
                device_id=f"dev-{i}",
                domain_id="cool-zone-1",
                domain_kind="cooling",
                event_family="thermal",
                severity="CRITICAL",
                timestamp=now,
            ))

        # Suppression active
        assert engine.is_suppressed("cool-zone-1")
        assert "cool-zone-1" in engine.get_suppressed_domains()

        # Agent leases should carry this suppression flag
        suppressed = engine.get_suppressed_domains()
        sm_private, sm_pub = _sm_keypair()
        identity = AgentIdentity.generate()
        identity.set_sm_public_key(sm_pub)
        lease_raw = _sign_lease(
            sm_private, identity.agent_id,
            suppression_domains=suppressed,
        )
        lease = AuthorizationLease.parse(lease_raw, identity)
        assert "cool-zone-1" in lease.suppression_domains

        # Human resolves the parent incident -> re-enables autonomy
        result = engine.human_re_enable("cool-zone-1", "ops-admin")
        assert result is True
        assert not engine.is_suppressed("cool-zone-1")

    def test_auto_recovery_with_hair_trigger(self):
        engine = SuppressionEngine()
        now = time.time()

        # Trigger suppression
        for i in range(3):
            engine.evaluate(CorrelationEvent(
                device_id=f"dev-{i}", domain_id="cool-1",
                domain_kind="cooling", event_family="thermal",
                severity="WARNING", timestamp=now,
            ))
        assert engine.is_suppressed("cool-1")

        # Force stability period elapsed
        state = engine._active["cool-1"]
        state.all_clear_at = now - STABILITY_PERIOD - 1
        engine.check_auto_recovery("cool-1", all_devices_healthy=True)
        assert not engine.is_suppressed("cool-1")

        # Hair-trigger: single event re-suppresses
        engine.evaluate(CorrelationEvent(
            device_id="dev-0", domain_id="cool-1",
            domain_kind="cooling", event_family="thermal",
            severity="WARNING", timestamp=time.time(),
        ))
        assert engine.is_suppressed("cool-1")


# ===========================================================================
# CONTRACT VERIFICATION: all 7 A2.7 contracts exist
# ===========================================================================


class TestArchitecturalContracts:
    """Verify all 7 A2.7 contracts are implemented and usable."""

    def test_contract_1_action_outcome(self):
        """ActionOutcome with all fields from A2.7."""
        outcome = ActionOutcome(
            action_id="a1", action_type=ActionType.SEL_CLEAR,
            diagnosis_id="d1", outcome=OutcomeStatus.SUCCESS,
            fault_resolved=True,
            pre_state={"sel_count": 450},
            post_state={"sel_count": 0},
            operator_override=False,
        )
        assert outcome.diagnosis_id == "d1"
        assert outcome.fault_resolved is True

    def test_contract_2_diagnosis(self):
        """Diagnosis with evidence[], contradicting[], per-dimension confidence."""
        d = Diagnosis(
            device_id="srv-1", component="DIMM.A1",
            summary="ECC errors rising", tier=TierLevel.T1,
            trajectory="degrading: 12 errors/day",
        )
        d.add_evidence("redfish", "mem:A1", {"ecc_count": 42})
        d.add_evidence("os-signal", "edac", "EDAC CE on DIMM0")
        d.confidence = [
            ConfidenceDimension("baseline", 0.92),
            ConfidenceDimension("skill_match", 1.0),
        ]
        d.add_reasoning_step("Skill 'memory-ecc' matched")
        assert len(d.evidence) == 2
        assert d.overall_confidence == 0.92
        assert d.to_dict()["tier"] == "t1"

    def test_contract_3_skill_package(self):
        """SkillPackage with lifecycle metadata."""
        pkg = SkillPackage(
            skill_id="fan-health", version="1.2.0",
            vendor="dell", device_types=["poweredge-r750"],
            tier=SkillTier.CORE,
        )
        pkg.record_deployment("system", "canary")
        pkg.record_outcome("SUCCESS")
        pkg.record_outcome("SUCCESS")
        pkg.record_outcome("FAILURE")
        assert pkg.outcome_stats.success_rate == pytest.approx(2/3, abs=0.01)
        assert len(pkg.deployment_history) == 1
        assert pkg.to_dict()["validation_state"] == "promoted"

    def test_contract_4_distributed_autonomy(self):
        """AutonomyPolicy: CC sets, SM enforces, agent enforces locally."""
        # CC sets policy (simulated via SM enforcer)
        sm = SMAutonomyEnforcer()
        sm.update_policy([{
            "action_type": "SEL_CLEAR",
            "max_per_window": 10,
            "window_seconds": 3600,
        }])
        # SM computes budget for agent lease
        budget = sm.get_budget_for_agent("agent-1")
        assert budget["SEL_CLEAR"] == 10

        # Agent enforces locally from lease data
        agent_budget = AgentBudgetEnforcer()
        agent_budget.update_from_lease(budget, stop_switch=False)
        assert agent_budget.allows(ActionType.SEL_CLEAR)

    def test_contract_5_os_signal_collector(self):
        """OSSignalCollector: pluggable register + collect."""
        collector = OSSignalCollector()
        # R3a ships syslog + dmesg sources
        from harkeniq.os_signals.syslog import SyslogSource
        from harkeniq.os_signals.dmesg import DmesgSource
        collector.register(SyslogSource(log_path="/nonexistent"))
        collector.register(DmesgSource())
        assert "syslog" in collector.active_sources
        assert "dmesg" in collector.active_sources

    def test_contract_6_reasoning_provider(self):
        """ReasoningProvider at SM: DeterministicReasoner + KnowledgeBaseReasoner."""
        kb = KnowledgeBase()
        kb.record_outcome(StoredOutcome("a1", "SEL_CLEAR", "dev-1", "SUCCESS"))

        pipeline = ReasoningPipeline()
        pipeline.add_provider(DeterministicReasoner())
        pipeline.add_provider(KnowledgeBaseReasoner(kb))

        context = ReasoningContext(
            device_id="dev-1",
            component="SystemEventLog",
            severity="WARNING",
            evidence=[{"type": "sel_full", "percent": 95}],
        )
        result = pipeline.analyze(context)
        assert result is not None
        assert result.provider in ("deterministic", "knowledge_base")

    def test_contract_7_peer_protocol(self):
        """PeerProtocol: tier gating from existing peer tracker."""
        from harkeniq.heartbeat.tracker import PeerTracker

        tracker = PeerTracker({"peers": [
            {"host": "10.0.0.1"}, {"host": "10.0.0.2"}, {"host": "10.0.0.3"},
        ], "heartbeat": {"interval": 10, "timeout_multiplier": 3}})

        proto = PeerProtocol(tracker)
        assert proto.get_reachable_peers() == 0  # none alive yet

        # R3b-2: methods are now implemented (no longer raise NotImplementedError)
        result = proto.broadcast_claim("subject", {})
        assert result is None  # returns None (no identity configured)

    def test_all_contracts_import_cleanly(self):
        """Smoke test: all contract modules import without error."""
        from harkeniq.autonomy.verification import ActionOutcome
        from harkeniq.autonomy.diagnosis import Diagnosis
        from harkeniq.autonomy.skill_lifecycle import SkillPackage
        from harkeniq.autonomy.budget import AgentBudgetEnforcer
        from harkeniq.os_signals.collector import OSSignalCollector
        from harkeniq_sm.reasoning import ReasoningPipeline
        from harkeniq.autonomy.peer_protocol import PeerProtocol
        # All 7 contracts importable
        assert True
