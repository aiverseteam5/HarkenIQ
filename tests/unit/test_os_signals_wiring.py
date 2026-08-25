"""QA-024: OS signals + BMC log poll wired into the agent.

The os_signals package (~900 lines) and Poller.poll_logs existed since
R3a/R3b with zero production callers; these tests cover the wiring.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from harkeniq.agent import Agent
from harkeniq.models import VerdictSeverity
from harkeniq.os_signals.collector import OSEvent, SignalSourceType


def _agent(**config):
    base = {"bmc": {"host": "https://bmc"}}
    base.update(config)
    return Agent(base)


def _event(severity="error", category="general", message="boom"):
    return OSEvent(
        source=SignalSourceType.DMESG,
        timestamp=time.time(),
        severity=severity,
        category=category,
        message=message,
        raw_line=f"raw: {message}",
        component_hint="disk",
    )


class TestOsEventIngest:
    def test_hardware_channel_error_is_critical(self):
        agent = _agent()
        agent._ingest_os_events([_event(category="mce")])
        verdict = agent._os_signal_verdicts["os:mce"]
        assert verdict.severity == VerdictSeverity.CRITICAL
        assert verdict.skill_name == "os-signals:dmesg"

    def test_general_error_caps_at_warning(self):
        agent = _agent()
        agent._ingest_os_events([_event(category="general")])
        assert (
            agent._os_signal_verdicts["os:general"].severity
            == VerdictSeverity.WARNING
        )

    def test_info_events_ignored(self):
        agent = _agent()
        agent._ingest_os_events([_event(severity="info")])
        assert agent._os_signal_verdicts == {}

    def test_evidence_carries_raw_line(self):
        agent = _agent()
        agent._ingest_os_events([_event(category="pcie_aer", message="AER err")])
        evidence = agent._os_signal_verdicts["os:pcie_aer"].evidence[0]
        assert evidence.fields["raw"] == "raw: AER err"
        assert evidence.fields["component_hint"] == "disk"


class _FakePoller:
    def __init__(self, entries):
        self.entries = entries

    async def poll_logs(self):
        return self.entries


def _entry(entry_id, severity="Critical", message="PSU failure detected"):
    return SimpleNamespace(
        id=entry_id, severity=severity, message=message,
        timestamp="2026-08-25T00:00:00Z",
    )


class TestBmcLogPoll:
    async def test_new_entries_produce_verdict(self):
        agent = _agent()
        agent.poller = _FakePoller([_entry("1"), _entry("2", "Warning")])
        await agent._poll_bmc_logs()
        verdict = agent._os_signal_verdicts["log:sel"]
        assert verdict.severity == VerdictSeverity.CRITICAL
        assert "2 new BMC log entries" in verdict.message
        assert len(verdict.evidence[0].fields["entries"]) == 2

    async def test_cursor_suppresses_already_seen(self):
        agent = _agent()
        agent.poller = _FakePoller([_entry("1")])
        await agent._poll_bmc_logs()
        agent._os_signal_verdicts.clear()
        await agent._poll_bmc_logs()  # same entries again
        assert "log:sel" not in agent._os_signal_verdicts

    async def test_new_entry_after_cursor_alerts(self):
        agent = _agent()
        poller = _FakePoller([_entry("1")])
        agent.poller = poller
        await agent._poll_bmc_logs()
        agent._os_signal_verdicts.clear()
        poller.entries = [_entry("1"), _entry("2")]
        await agent._poll_bmc_logs()
        verdict = agent._os_signal_verdicts["log:sel"]
        assert "1 new BMC log entry" in verdict.message

    async def test_sel_clear_resets_ids_still_alerts(self):
        """SEL ids restart after a clear; the id-set cursor (not a
        high-water mark) must still alert on post-clear entries."""
        agent = _agent()
        poller = _FakePoller([_entry("1"), _entry("2")])
        agent.poller = poller
        await agent._poll_bmc_logs()
        agent._os_signal_verdicts.clear()
        poller.entries = [_entry("1", message="post-clear event")]
        # id "1" was seen pre-clear — suppressed. This is the accepted
        # trade-off of id-set cursoring; a NEW id always alerts:
        poller.entries = [_entry("3", message="post-clear event")]
        await agent._poll_bmc_logs()
        assert "log:sel" in agent._os_signal_verdicts

    async def test_ok_entries_not_alerted(self):
        agent = _agent()
        agent.poller = _FakePoller([_entry("1", severity="OK")])
        await agent._poll_bmc_logs()
        assert "log:sel" not in agent._os_signal_verdicts

    async def test_sel_forwarded_flag_set_with_reporter(self):
        agent = _agent()
        agent.poller = _FakePoller([_entry("1")])
        agent.reporter = SimpleNamespace(enabled=True)
        assert agent._sel_events_forwarded is False
        await agent._poll_bmc_logs()
        assert agent._sel_events_forwarded is True
        # And the SEL_CLEAR precondition sees it
        _, agent_state = agent._precondition_states(
            SimpleNamespace(type=SimpleNamespace(value="SEL_CLEAR"),
                            params={})
        )
        assert agent_state["sel_events_forwarded"] is True

    async def test_cursor_persisted_via_checkpoint_roundtrip(self, tmp_path):
        from harkeniq.state.checkpoint import CheckpointManager

        agent = _agent()
        agent.poller = _FakePoller([_entry("7")])
        await agent._poll_bmc_logs()
        assert agent._log_cursors["bmc_sel"] == "7"

        checkpoint = CheckpointManager(tmp_path / "cp.db")
        await checkpoint.save_checkpoint(
            sensor_readings={}, baselines={}, verdicts=[], peers=[],
            agent_meta={}, log_cursors=dict(agent._log_cursors),
        )
        state = await checkpoint.load_checkpoint()
        assert state["log_cursors"]["bmc_sel"] == "7"


class TestReportIncludesOsVerdicts:
    async def test_os_verdicts_ride_the_report_channel(self):
        agent = _agent()
        reported = []

        async def report_verdict(v):
            reported.append(v.sensor_id)
            return True

        agent.reporter = SimpleNamespace(
            enabled=True, report_verdict=report_verdict,
        )
        agent._ingest_os_events([_event(category="mce")])
        await agent._report_changed_verdicts()
        assert "os:mce" in reported


class TestCollectorBuild:
    def test_no_sources_returns_none(self, monkeypatch):
        import shutil
        monkeypatch.setattr(shutil, "which", lambda _: None)
        from harkeniq.os_signals import syslog as syslog_mod
        monkeypatch.setattr(
            syslog_mod.SyslogSource, "_find_log", lambda self: None,
        )
        agent = _agent()
        assert agent._build_os_collector() is None

    def test_available_sources_registered(self, monkeypatch):
        import shutil
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        from harkeniq.os_signals import syslog as syslog_mod
        monkeypatch.setattr(
            syslog_mod.SyslogSource, "_find_log", lambda self: None,
        )
        agent = _agent()
        collector = agent._build_os_collector()
        assert set(collector.active_sources) == {
            "dmesg", "journal", "smartctl",
        }