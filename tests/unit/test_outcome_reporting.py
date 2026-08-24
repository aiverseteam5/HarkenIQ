"""Tests for outcome reporting SM→CC pipeline (R3b-3 Phase 2)."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from harkeniq.proto import harkeniq_pb2


class TestFleetOutcomeProto:
    def test_fleet_outcome_message(self):
        """FleetOutcome proto message has all required fields."""
        outcome = harkeniq_pb2.FleetOutcome(
            action_id="act-123",
            action_type="FAN_RESET",
            device_agent_id="agent-a",
            outcome="SUCCESS",
            fault_resolved=True,
            vendor="dell",
            model="PowerEdge R750",
            recorded_at_unix=int(time.time()),
        )
        assert outcome.action_id == "act-123"
        assert outcome.action_type == "FAN_RESET"
        assert outcome.outcome == "SUCCESS"
        assert outcome.fault_resolved is True
        assert outcome.vendor == "dell"

    def test_fleet_snapshot_includes_outcomes(self):
        """FleetSnapshot.outcomes field carries outcome batch."""
        snapshot = harkeniq_pb2.FleetSnapshot(
            devices=[],
            incidents=[],
            pending_actions=[],
            snapshot_at_unix=int(time.time()),
            outcomes=[
                harkeniq_pb2.FleetOutcome(
                    action_id="act-1",
                    action_type="SEL_CLEAR",
                    device_agent_id="agent-x",
                    outcome="SUCCESS",
                ),
                harkeniq_pb2.FleetOutcome(
                    action_id="act-2",
                    action_type="BMC_RESET",
                    device_agent_id="agent-y",
                    outcome="FAILURE",
                ),
            ],
        )
        assert len(snapshot.outcomes) == 2
        assert snapshot.outcomes[0].action_type == "SEL_CLEAR"
        assert snapshot.outcomes[1].outcome == "FAILURE"

    def test_empty_outcomes_backward_compatible(self):
        """FleetSnapshot without outcomes still works (backward compat)."""
        snapshot = harkeniq_pb2.FleetSnapshot(
            devices=[],
            incidents=[],
            pending_actions=[],
            snapshot_at_unix=int(time.time()),
        )
        assert len(snapshot.outcomes) == 0


class TestCCOutcomeHistory:
    def test_outcome_history_model(self):
        """CCOutcomeHistory table model has correct columns."""
        from harkeniq_cc.db.models import CCOutcomeHistory

        row = CCOutcomeHistory(
            site_id="site-1",
            action_id="act-123",
            action_type="FAN_RESET",
            device_agent_id="agent-a",
            vendor="dell",
            model="R750",
            outcome="SUCCESS",
            fault_resolved=True,
        )
        assert row.action_type == "FAN_RESET"
        assert row.outcome == "SUCCESS"
        assert row.vendor == "dell"

    def test_outcome_history_table_name(self):
        from harkeniq_cc.db.models import CCOutcomeHistory
        assert CCOutcomeHistory.__tablename__ == "cc_outcome_history"


class TestOutcomeIngestion:
    async def test_ingest_outcomes(self):
        """_ingest_outcomes stores outcomes in CC database."""
        from harkeniq_cc.fleet_poller import _ingest_outcomes

        # Use a mock session that collects added objects
        added = []

        class MockSession:
            def add(self, obj):
                added.append(obj)

        outcomes = [
            {
                "action_id": "act-1",
                "action_type": "FAN_RESET",
                "device_agent_id": "agent-a",
                "vendor": "dell",
                "model": "R750",
                "outcome": "SUCCESS",
                "fault_resolved": True,
                "recorded_at_unix": int(time.time()),
            },
            {
                "action_id": "act-2",
                "action_type": "BMC_RESET",
                "device_agent_id": "agent-b",
                "outcome": "FAILURE",
            },
        ]

        await _ingest_outcomes(MockSession(), "site-1", outcomes)
        assert len(added) == 2
        assert added[0].action_type == "FAN_RESET"
        assert added[0].outcome == "SUCCESS"
        assert added[1].outcome == "FAILURE"

    async def test_ingest_empty_outcomes(self):
        """Empty outcomes list does nothing."""
        from harkeniq_cc.fleet_poller import _ingest_outcomes

        added = []

        class MockSession:
            def add(self, obj):
                added.append(obj)

        await _ingest_outcomes(MockSession(), "site-1", [])
        assert len(added) == 0


class TestSMOutcomeWatermark:
    def test_reported_to_cc_field(self):
        """ActionOutcomeRow has reported_to_cc watermark field."""
        from harkeniq_sm.db.models import ActionOutcomeRow
        row = ActionOutcomeRow(
            action_id="act-1",
            action_type="FAN_RESET",
            device_id="dev-1",
            outcome="SUCCESS",
        )
        assert row.reported_to_cc in (False, None)  # default (None before flush)
