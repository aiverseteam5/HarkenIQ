"""R3b-3 exit gate: verify all Advanced Remediation + Fleet Learning requirements."""

from __future__ import annotations

import json

import pytest

from harkeniq.security.credentials import (
    Credential,
    CredentialProviderChain,
    LocalCredentialProvider,
    MockCredentialProvider,
    VaultCredentialProvider,
)
from harkeniq.security.credential_rotation import (
    CredentialRotator,
    RotationStatus,
    generate_password,
)
from harkeniq.actions.playbook import (
    BMC_RECOVERY,
    BUILTIN_PLAYBOOKS,
    DISK_REPLACEMENT_PREP,
    THERMAL_MITIGATION,
    Playbook,
    PlaybookExecution,
    PlaybookStep,
)
from harkeniq.actions.playbook_executor import PlaybookExecutor
from harkeniq.autonomy.verification import VerificationCheck
from harkeniq.models import ActionType, PlaybookStatus
from harkeniq.proto import harkeniq_pb2


# -- OQ-14: CredentialProvider with Vault + Local + Mock --------------------


class TestOQ14CredentialProvider:
    async def test_local_provider_works(self):
        provider = LocalCredentialProvider({"bmc": {"username": "admin", "password": "pw"}})
        cred = await provider.get_credentials("dev-1")
        assert cred is not None and cred.source == "local"

    async def test_mock_provider_works(self):
        provider = MockCredentialProvider()
        cred = await provider.get_credentials("dev-1")
        assert cred is not None and cred.source == "mock"

    async def test_vault_provider_interface(self):
        provider = VaultCredentialProvider("http://vault:8200", "token")
        assert provider.provider_name == "vault"

    async def test_chain_fallback(self):
        """R-H7: chain ensures creds available even when Vault is down."""
        failing = LocalCredentialProvider({})  # returns None
        fallback = MockCredentialProvider()
        chain = CredentialProviderChain([failing, fallback])
        cred = await chain.get_credentials("dev-1")
        assert cred is not None
        assert cred.source == "mock"


# -- R-C1: Fleet Learning Loop Complete ------------------------------------


class TestRC1FleetLearningLoop:
    def test_outcome_reporting_proto(self):
        """FleetOutcome in FleetSnapshot enables SM→CC outcome flow."""
        snapshot = harkeniq_pb2.FleetSnapshot(
            outcomes=[
                harkeniq_pb2.FleetOutcome(
                    action_id="act-1", action_type="FAN_RESET",
                    device_agent_id="a-1", outcome="SUCCESS",
                    fault_resolved=True, vendor="dell", model="R750",
                ),
            ],
        )
        assert len(snapshot.outcomes) == 1
        assert snapshot.outcomes[0].outcome == "SUCCESS"

    def test_outcome_aggregation(self):
        from harkeniq_cc.outcome_aggregator import OutcomeAggregator
        agg = OutcomeAggregator()
        agg.ingest([
            {"action_type": "FAN_RESET", "vendor": "dell", "model": "R750", "outcome": "SUCCESS"},
            {"action_type": "FAN_RESET", "vendor": "dell", "model": "R750", "outcome": "FAILURE"},
        ])
        assert agg.get_fleet_success_rate("FAN_RESET") == pytest.approx(0.5)

    def test_pattern_detection(self):
        from harkeniq_cc.outcome_aggregator import OutcomeAggregator
        from harkeniq_cc.pattern_detector import PatternDetector
        agg = OutcomeAggregator()
        agg.ingest([
            {"action_type": "BMC_RESET", "vendor": "dell", "model": "R750",
             "outcome": "FAILURE"} for _ in range(6)
        ])
        detector = PatternDetector(batch_threshold=0.15, min_samples=5)
        patterns = detector.detect(agg)
        assert any(p.pattern_type == "batch_failure" for p in patterns)

    def test_knowledge_distribution(self):
        from harkeniq_cc.knowledge_distributor import KnowledgeDistributor
        from harkeniq_cc.pattern_detector import FleetPattern
        dist = KnowledgeDistributor()
        p = FleetPattern(
            pattern_id="pat-1", pattern_type="batch_failure",
            description="test", affected_scope={"vendor": "dell", "model": "R750"},
            confidence=0.9,
        )
        sites = [{"site_id": "s1", "devices": [{"vendor": "dell", "model": "R750"}]}]
        targets = dist.select_targets(p, sites)
        assert len(targets) == 1

    def test_learning_feedback_cycle(self):
        from harkeniq_cc.learning_feedback import LearningFeedbackTracker
        tracker = LearningFeedbackTracker(promotion_success_rate=0.95, promotion_min_devices=5)
        tracker.start_cycle("c1", "p1", "batch_failure", {"failure_rate": 0.3})
        tracker.record_skill_generated("c1", "skill-abc")
        tracker.record_distribution("c1", sites=2, devices=10)
        improvement = tracker.record_outcomes("c1", {"failure_rate": 0.05, "success_rate": 0.98})
        assert improvement > 50  # significant improvement
        promoted = tracker.check_promotion("c1")
        assert promoted is True


# -- Playbook Lifecycle Verified End-to-End --------------------------------


class TestPlaybookLifecycle:
    async def test_bmc_recovery_end_to_end(self):
        async def get_state(device_id):
            return {"sel_entry_count": 0, "bmc_responsive": True}
        executor = PlaybookExecutor(
            get_device_state=get_state, verification_wait_scale=0.0,
        )
        execution = await executor.execute_playbook(BMC_RECOVERY, "dev-1")
        assert execution.status == PlaybookStatus.COMPLETED
        assert len(execution.step_outcomes) == 2

    async def test_playbook_pause_and_resume(self):
        call_count = 0
        async def get_state(device_id):
            nonlocal call_count
            call_count += 1
            if call_count <= 4:
                return {"sel_entry_count": 999, "bmc_responsive": True}
            return {"sel_entry_count": 0, "bmc_responsive": True}
        executor = PlaybookExecutor(
            get_device_state=get_state, verification_wait_scale=0.0,
        )
        exe = await executor.execute_playbook(BMC_RECOVERY, "dev-1")
        assert exe.status == PlaybookStatus.PAUSED
        # Resume after fix
        resumed = await executor.resume(exe, BMC_RECOVERY)
        assert resumed.status == PlaybookStatus.COMPLETED

    def test_three_builtin_playbooks(self):
        assert len(BUILTIN_PLAYBOOKS) == 3
        for pb in BUILTIN_PLAYBOOKS.values():
            assert pb.step_count > 0
            assert len(pb.device_types) > 0


# -- Credential Rotation Blue-Green Verified --------------------------------


class TestCredentialRotation:
    async def test_blue_green_rotation(self):
        mock_creds = MockCredentialProvider()
        rotator = CredentialRotator(credential_provider=mock_creds)
        event = await rotator.rotate("dev-1", "old-admin")
        assert event.status == RotationStatus.SUCCESS
        # New creds stored
        cred = await mock_creds.get_credentials("dev-1")
        assert cred.username == event.new_username

    def test_password_generation(self):
        pw = generate_password(24)
        assert len(pw) == 24
