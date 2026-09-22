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
    """QA-033: CC-pushed patterns become evidence for reasoning.

    A30.29: the mirror is per SITE -- a device's reasoning consumes the
    projection pushed for the device's own site -- and every consumed
    pattern comes back with the marker it was pushed with (None for an
    unmarked, pre-A30.29 payload).
    """

    async def _site_of(self, db, agent_id):
        async with db() as session:
            return (await DeviceRepo(session).get_by_agent_id(agent_id)).site_id

    async def test_matching_patterns_selected_by_scope(self, db, ingest):
        await ingest.register(
            agent_id="a1", agent_name="srv-01", vendor="Dell", model="R750",
        )
        site = await self._site_of(db, "a1")
        for pattern in (
            {
                "pattern_id": "pat-1", "pattern_type": "batch_failure",
                "description": "Dell R750 PSU batch failing",
                "affected_scope": {"vendor": "Dell", "model": "R750"},
                "confidence": 0.9,
                "generation_visibility": {"scope": "site", "site_id": "cc-site-1",
                                          "projection_version": 1},
            },
            {
                "pattern_id": "pat-2", "pattern_type": "anomaly",
                "description": "HPE-only issue",
                "affected_scope": {"vendor": "HPE"},
                "confidence": 0.8,
            },
            {
                "pattern_id": "pat-3", "pattern_type": "reliability",
                "description": "Fleet-wide (wildcard scope)",
                "affected_scope": {},
                "confidence": 0.7,
            },
        ):
            ingest.fleet_patterns.put(site, pattern)
        evidence, consumed = await ingest._matching_fleet_patterns("a1")
        ids = [e["fleet_pattern"]["pattern_id"] for e in evidence]
        assert set(ids) == {"pat-1", "pat-3"}  # HPE-scoped pattern excluded
        # The marker rides beside the evidence, positionally: pat-1 was
        # pushed marked, pat-3 was not.
        by_id = dict(zip(ids, consumed))
        assert by_id["pat-1"] is not None and by_id["pat-1"].site_id == "cc-site-1"
        assert by_id["pat-3"] is None
        # The citation shape A30.28's read grammar knows is unchanged: the
        # marker is NOT quoted into the prompt evidence.
        for entry in evidence:
            assert set(entry["fleet_pattern"]) == {
                "pattern_id", "pattern_type", "description", "confidence"}

    async def test_another_sites_projection_is_not_consumed(self, db, ingest):
        """The multi-site rule: site B's row for a pattern is invisible
        to a site-A device even when site A holds no row for it."""
        await ingest.register(
            agent_id="a1", agent_name="srv-01", vendor="Dell", model="R750",
        )
        ingest.fleet_patterns.put("some-other-site", {
            "pattern_id": "pat-b", "affected_scope": {},
            "description": "site B's projection",
        })
        assert await ingest._matching_fleet_patterns("a1") == ([], [])

    async def test_legacy_row_consulted_only_where_the_site_holds_none(self, db, ingest):
        await ingest.register(
            agent_id="a1", agent_name="srv-01", vendor="Dell", model="R750",
        )
        site = await self._site_of(db, "a1")
        ingest.fleet_patterns.put_legacy({
            "pattern_id": "pat-1", "affected_scope": {},
            "description": "the whole tenant's payload, pre-A30.28",
        })
        ingest.fleet_patterns.put_legacy({
            "pattern_id": "pat-2", "affected_scope": {},
            "description": "another unbounded payload",
        })
        ingest.fleet_patterns.put(site, {
            "pattern_id": "pat-1", "affected_scope": {},
            "description": "site's own bounded projection",
            "generation_visibility": {"scope": "site", "site_id": "cc-site-1",
                                      "projection_version": 1},
        })
        evidence, consumed = await ingest._matching_fleet_patterns("a1")
        by_id = {e["fleet_pattern"]["pattern_id"]: e["fleet_pattern"]["description"]
                 for e in evidence}
        assert by_id == {
            "pat-1": "site's own bounded projection",   # supersedes the legacy row
            "pat-2": "another unbounded payload",       # no site row: legacy, unmarked
        }
        markers = dict(zip(by_id, consumed))
        assert markers["pat-1"].site_id == "cc-site-1"
        assert markers["pat-2"] is None

    async def test_unknown_device_yields_nothing(self, db, ingest):
        ingest.fleet_patterns.put_legacy(
            {"pattern_id": "pat-1", "affected_scope": {}},
        )
        assert await ingest._matching_fleet_patterns("ghost") == ([], [])

    async def test_empty_mirror_fast_path(self, ingest):
        assert await ingest._matching_fleet_patterns("a1") == ([], [])
