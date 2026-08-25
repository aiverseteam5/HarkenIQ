"""IngestService: registration, heartbeat, verdict, onset semantics."""

import pytest

from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.repos import (
    DeviceRepo,
    StatusRepo,
    SubsystemStateRepo,
    TelemetryRepo,
)
from harkeniq_sm.ingest import IngestService


@pytest.fixture
def ingest(db):
    return IngestService(db, SMConfig(insecure=True, site_name="site-test"))


OK_SUMMARY = {"psu": "OK", "thermal": "OK", "fan": "OK"}


class TestRegistration:
    async def test_register_creates_device(self, db, ingest):
        site_name = await ingest.register(
            agent_id="a1", agent_name="rack-12-srv-04", vendor="Dell",
            model="R750", service_tag="TAG1",
            bmc_location_json='{"rack": "12"}', peers=["10.0.0.2:5150"],
        )
        assert site_name == "site-test"
        async with db() as session:
            device = await DeviceRepo(session).get_by_agent_id("a1")
            assert device.vendor == "Dell"
            assert device.bmc_location == {"rack": "12"}
            assert device.peers == ["10.0.0.2:5150"]

    async def test_register_bad_location_json_tolerated(self, db, ingest):
        await ingest.register(agent_id="a1", bmc_location_json="{not json")
        async with db() as session:
            device = await DeviceRepo(session).get_by_agent_id("a1")
            assert device.bmc_location is None


class TestHeartbeat:
    async def test_heartbeat_autocreates_and_records(self, db, ingest):
        assert await ingest.heartbeat(
            "a1", "srv-1", "OBSERVING", dict(OK_SUMMARY), {"p1": "ALIVE"}
        )
        async with db() as session:
            device = await DeviceRepo(session).get_by_agent_id("a1")
            status = await StatusRepo(session).get(device.id)
            assert status.last_state == "OBSERVING"
            assert status.last_peer_status == {"p1": "ALIVE"}
            assert (
                await SubsystemStateRepo(session).get(device.id, "psu")
            ).severity == "OK"

    async def test_onset_set_kept_cleared(self, db, ingest):
        events = []

        async def hook(device_id, subsystem, severity, onset_at):
            events.append((subsystem, severity, onset_at))

        ingest.on_onset = hook
        bad = dict(OK_SUMMARY, psu="CRITICAL")
        await ingest.heartbeat("a1", "srv-1", "EVALUATING", bad, {})
        assert [(s, sev) for s, sev, _ in events] == [("psu", "CRITICAL")]
        first_onset = events[0][2]

        # Continuing fault: onset preserved, no new event.
        await ingest.heartbeat("a1", "srv-1", "EVALUATING", bad, {})
        assert len(events) == 1
        async with db() as session:
            device = await DeviceRepo(session).get_by_agent_id("a1")
            state = await SubsystemStateRepo(session).get(device.id, "psu")
            assert state.onset_at.replace(tzinfo=None) == first_onset.replace(tzinfo=None)

        # Recovery clears; next fault is a fresh onset.
        await ingest.heartbeat("a1", "srv-1", "OBSERVING", dict(OK_SUMMARY), {})
        await ingest.heartbeat("a1", "srv-1", "EVALUATING", bad, {})
        assert len(events) == 2
        assert events[1][2] > first_onset


class TestVerdict:
    async def test_verdict_persists_and_sets_onset(self, db, ingest):
        assert await ingest.verdict(
            "a1", "psu:PS1", "psu_health", "CRITICAL",
            evidence_json='[{"field": "input_voltage", "value": 0}]',
        )
        async with db() as session:
            device = await DeviceRepo(session).get_by_agent_id("a1")
            rows = await TelemetryRepo(session).recent_verdicts(device.id)
            assert rows[0].severity == "CRITICAL"
            assert rows[0].evidence[0]["field"] == "input_voltage"
            state = await SubsystemStateRepo(session).get(device.id, "psu")
            assert state.severity == "CRITICAL"
            assert state.onset_at is not None

    async def test_healthy_verdict_clears(self, db, ingest):
        await ingest.verdict("a1", "psu:PS1", "psu_health", "CRITICAL")
        await ingest.verdict("a1", "psu:PS1", "psu_health", "HEALTHY")
        async with db() as session:
            device = await DeviceRepo(session).get_by_agent_id("a1")
            state = await SubsystemStateRepo(session).get(device.id, "psu")
            assert state.severity == "OK"
            assert state.onset_at is None


class TestFleetPatternEnrichment:
    """QA-033: CC-pushed patterns become evidence for reasoning."""

    async def test_matching_patterns_selected_by_scope(self, db, ingest):
        await ingest.register(
            agent_id="a1", agent_name="srv-01", vendor="Dell", model="R750",
        )
        ingest.fleet_patterns = {
            "pat-1": {
                "pattern_id": "pat-1", "pattern_type": "batch_failure",
                "description": "Dell R750 PSU batch failing",
                "affected_scope": {"vendor": "Dell", "model": "R750"},
                "confidence": 0.9,
            },
            "pat-2": {
                "pattern_id": "pat-2", "pattern_type": "anomaly",
                "description": "HPE-only issue",
                "affected_scope": {"vendor": "HPE"},
                "confidence": 0.8,
            },
            "pat-3": {
                "pattern_id": "pat-3", "pattern_type": "reliability",
                "description": "Fleet-wide (wildcard scope)",
                "affected_scope": {},
                "confidence": 0.7,
            },
        }
        evidence = await ingest._matching_fleet_patterns("a1")
        ids = {e["fleet_pattern"]["pattern_id"] for e in evidence}
        assert ids == {"pat-1", "pat-3"}  # HPE-scoped pattern excluded

    async def test_unknown_device_yields_nothing(self, db, ingest):
        ingest.fleet_patterns = {
            "pat-1": {"pattern_id": "pat-1", "affected_scope": {}},
        }
        assert await ingest._matching_fleet_patterns("ghost") == []

    async def test_empty_mirror_fast_path(self, ingest):
        assert await ingest._matching_fleet_patterns("a1") == []
