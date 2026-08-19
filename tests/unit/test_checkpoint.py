"""Checkpoint persistence tests (Doc 06 §7, Doc 13 §7, Doc 10 §2.14)."""

import math

import pytest

from harkeniq.errors import CheckpointReadError
from harkeniq.models import (
    Baseline,
    Evidence,
    Peer,
    PeerStatus,
    RegressionState,
    TrendingRule,
    Verdict,
    VerdictSeverity,
)
from harkeniq.skills.trending import TrendingEngine
from harkeniq.state.checkpoint import CheckpointManager

T0 = 1_700_000_000.0
STEP = 60.0


@pytest.fixture
async def mgr(tmp_path):
    m = CheckpointManager(tmp_path / "checkpoint.db")
    yield m
    await m.close()


def make_baseline(sensor_id="fan:Fan1", **overrides):
    kwargs = dict(
        sensor_id=sensor_id,
        mean=9500.0,
        stddev=120.0,
        variance=14400.0,
        m2=288000.0,
        min_val=9100.0,
        max_val=9900.0,
        sample_count=20,
        ring_buffer=[(T0 + i * STEP, 9500.0 + i) for i in range(20)],
        first_sample_at="2023-11-14T22:13:20Z",
        last_sample_at="2026-08-19T00:00:00Z",
        degraded_baseline=False,
        regression_state=RegressionState(
            sum_x=10.0, sum_y=190000.0, sum_xy=95000.0,
            sum_x2=3.5, sum_y2=1.8e9, n=20,
        ),
    )
    kwargs.update(overrides)
    return Baseline(**kwargs)


def make_verdict(sensor_id="fan:Fan1", severity=VerdictSeverity.CRITICAL,
                 message="Fan Fan1 failed", produced_at="2026-08-19T00:00:00Z"):
    return Verdict(
        sensor_id=sensor_id,
        skill_name="fan-health",
        severity=severity,
        message=message,
        evidence=[Evidence(
            sensor_id=sensor_id,
            skill_name="fan-health",
            rule_index=0,
            condition="health == 'Critical'",
            fields={"health": "Critical", "speed_rpm": 0},
            timestamp=produced_at,
            baseline_confidence=1.0,
        )],
        timestamp=produced_at,
    )


async def save_minimal(mgr, **overrides):
    payload = dict(
        sensor_readings={},
        baselines={},
        verdicts=[],
        peers=[],
        agent_meta={},
        log_cursors={},
    )
    payload.update(overrides)
    await mgr.save_checkpoint(**payload)


class TestSetup:
    async def test_wal_mode_enabled(self, mgr):
        row = mgr._conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0] == "wal"

    async def test_all_tables_created(self, mgr):
        names = {
            r["name"] for r in mgr._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "agent_meta", "sensor_readings", "baselines", "verdicts",
            "peers", "log_cursors", "actions", "audit_log",
        } <= names

    async def test_empty_load(self, mgr):
        state = await mgr.load_checkpoint()
        assert state["agent_meta"] == {}
        assert state["baselines"] == {}
        assert state["verdicts"] == []
        assert state["peers"] == []


class TestRoundTrip:
    async def test_agent_meta_upsert(self, mgr):
        await save_minimal(mgr, agent_meta={"agent_id": "agent-1", "seq": "5"})
        await save_minimal(mgr, agent_meta={"seq": "6"})
        state = await mgr.load_checkpoint()
        assert state["agent_meta"] == {"agent_id": "agent-1", "seq": "6"}

    async def test_sensor_readings(self, mgr):
        reading = {
            "sensor_type": "fan",
            "reading": {"speed_rpm": 9500, "health": "OK"},
            "health": "OK",
            "collected_at": "2026-08-19T00:00:00Z",
        }
        await save_minimal(mgr, sensor_readings={"fan:Fan1": reading})
        state = await mgr.load_checkpoint()
        assert state["sensor_readings"]["fan:Fan1"] == reading

    async def test_baseline_full_round_trip(self, mgr):
        b = make_baseline(degraded_baseline=True)
        await save_minimal(mgr, baselines={"fan:Fan1": b})
        loaded = (await mgr.load_checkpoint())["baselines"]["fan:Fan1"]
        assert loaded.mean == b.mean
        assert loaded.stddev == b.stddev
        assert loaded.variance == b.variance
        assert loaded.m2 == b.m2
        assert loaded.min_val == b.min_val
        assert loaded.max_val == b.max_val
        assert loaded.sample_count == 20
        assert loaded.ring_buffer == b.ring_buffer
        assert all(isinstance(p, tuple) for p in loaded.ring_buffer)
        assert loaded.first_sample_at == b.first_sample_at
        assert loaded.last_sample_at == b.last_sample_at
        assert loaded.degraded_baseline is True
        assert loaded.regression_state == b.regression_state

    async def test_verdict_round_trip(self, mgr):
        v = make_verdict()
        await save_minimal(mgr, verdicts=[v])
        loaded = (await mgr.load_checkpoint())["verdicts"]
        assert len(loaded) == 1
        got = loaded[0]
        assert got.sensor_id == v.sensor_id
        assert got.skill_name == v.skill_name
        assert got.severity == VerdictSeverity.CRITICAL
        assert got.message == v.message
        assert got.evidence == v.evidence
        assert got.timestamp == v.timestamp

    async def test_load_most_recent_verdict_per_sensor(self, mgr):
        await save_minimal(mgr, verdicts=[
            make_verdict(severity=VerdictSeverity.CRITICAL),
            make_verdict(severity=VerdictSeverity.HEALTHY, message="recovered"),
            make_verdict(sensor_id="fan:Fan2", severity=VerdictSeverity.WARNING,
                         message="Fan2 warn"),
        ])
        loaded = (await mgr.load_checkpoint())["verdicts"]
        by_sensor = {v.sensor_id: v for v in loaded}
        assert len(loaded) == 2
        assert by_sensor["fan:Fan1"].severity == VerdictSeverity.HEALTHY
        assert by_sensor["fan:Fan1"].message == "recovered"
        assert by_sensor["fan:Fan2"].severity == VerdictSeverity.WARNING

    async def test_peer_round_trip(self, mgr):
        peer = Peer(
            peer_id="agent-2",
            host="10.0.0.2",
            port=8500,
            last_heartbeat="2026-08-19T00:00:00Z",
            status=PeerStatus.ALIVE,
            last_known_health={"overall": "HEALTHY"},
        )
        await save_minimal(mgr, peers=[peer])
        loaded = (await mgr.load_checkpoint())["peers"]
        assert len(loaded) == 1
        got = loaded[0]
        assert got.peer_id == "agent-2"
        assert got.status == PeerStatus.ALIVE
        assert got.last_known_health == {"overall": "HEALTHY"}
        # stored lowercase per Doc 06 schema
        row = mgr._conn.execute("SELECT state FROM peers").fetchone()
        assert row["state"] == "alive"

    async def test_peer_without_health(self, mgr):
        await save_minimal(mgr, peers=[Peer(peer_id="agent-3", host="h", port=1)])
        got = (await mgr.load_checkpoint())["peers"][0]
        assert got.status == PeerStatus.UNKNOWN
        assert got.last_known_health is None

    async def test_log_cursors(self, mgr):
        await save_minimal(mgr, log_cursors={"idrac:sel": "42"})
        assert (await mgr.load_checkpoint())["log_cursors"] == {"idrac:sel": "42"}


class TestDirtyTracking:
    async def test_unchanged_baseline_not_rewritten(self, mgr):
        b = make_baseline()
        await save_minimal(mgr, baselines={"fan:Fan1": b})
        # Mutate a field the dirty marker does not cover; second save must skip.
        b.mean = 1.0
        await save_minimal(mgr, baselines={"fan:Fan1": b})
        loaded = (await mgr.load_checkpoint())["baselines"]["fan:Fan1"]
        assert loaded.mean == 9500.0

    async def test_changed_baseline_rewritten(self, mgr):
        b = make_baseline()
        await save_minimal(mgr, baselines={"fan:Fan1": b})
        b.mean = 9400.0
        b.sample_count = 21
        b.last_sample_at = "2026-08-19T00:01:00Z"
        await save_minimal(mgr, baselines={"fan:Fan1": b})
        loaded = (await mgr.load_checkpoint())["baselines"]["fan:Fan1"]
        assert loaded.mean == 9400.0
        assert loaded.sample_count == 21


class TestPruning:
    async def test_verdicts_pruned_to_limit(self, mgr):
        verdicts = [
            make_verdict(message=f"v{i}", severity=VerdictSeverity.HEALTHY)
            for i in range(1005)
        ]
        await save_minimal(mgr, verdicts=verdicts)
        count = mgr._conn.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0]
        assert count == 1000
        # oldest rows dropped, newest kept
        loaded = (await mgr.load_checkpoint())["verdicts"]
        assert loaded[0].message == "v1004"

    async def test_pruning_is_per_sensor(self, mgr):
        verdicts = [make_verdict(message=f"a{i}") for i in range(1002)]
        verdicts += [make_verdict(sensor_id="fan:Fan2", message="b0")]
        await save_minimal(mgr, verdicts=verdicts)
        rows = mgr._conn.execute(
            "SELECT sensor_id, COUNT(*) c FROM verdicts GROUP BY sensor_id"
        ).fetchall()
        counts = {r["sensor_id"]: r["c"] for r in rows}
        assert counts == {"fan:Fan1": 1000, "fan:Fan2": 1}

    async def test_stale_baseline_discarded_on_load(self, mgr):
        fresh = make_baseline("fan:Fan1", last_sample_at="2026-08-19T00:00:00Z")
        stale = make_baseline("fan:Fan2", last_sample_at="2026-01-01T00:00:00Z")
        await save_minimal(mgr, baselines={"fan:Fan1": fresh, "fan:Fan2": stale})
        loaded = (await mgr.load_checkpoint())["baselines"]
        assert "fan:Fan1" in loaded
        assert "fan:Fan2" not in loaded
        # stale row also deleted from the database
        count = mgr._conn.execute("SELECT COUNT(*) FROM baselines").fetchone()[0]
        assert count == 1


class TestAuditAndCursors:
    async def test_audit_append_only(self, mgr):
        await mgr.save_audit_entry("IDENTIFY_LED", "fan:Fan1", "success",
                                   authorization="op@example.com")
        await mgr.save_audit_entry("COLLECT_DIAGNOSTICS", "disk:Disk0", "failure")
        rows = mgr._conn.execute(
            "SELECT * FROM audit_log ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["action"] == "IDENTIFY_LED"
        assert rows[0]["authorization"] == "op@example.com"
        assert rows[1]["outcome"] == "failure"
        assert rows[1]["authorization"] is None
        assert rows[0]["logged_at"].endswith("Z")

    async def test_log_cursor_get_set(self, mgr):
        assert await mgr.get_log_cursor("idrac:sel") is None
        await mgr.update_log_cursor("idrac:sel", "17")
        assert await mgr.get_log_cursor("idrac:sel") == "17"
        await mgr.update_log_cursor("idrac:sel", "18")
        assert await mgr.get_log_cursor("idrac:sel") == "18"


class TestErrors:
    async def test_load_corrupt_row_raises_read_error(self, mgr):
        with mgr._conn:
            mgr._conn.execute(
                "INSERT INTO verdicts "
                "(sensor_id, skill_name, verdict, evidence_json, produced_at) "
                "VALUES ('fan:Fan1', 'fan-health', 'CRITICAL', 'not json', 'x')"
            )
        with pytest.raises(CheckpointReadError):
            await mgr.load_checkpoint()

    async def test_reopen_existing_db(self, tmp_path):
        path = tmp_path / "checkpoint.db"
        m1 = CheckpointManager(path)
        await save_minimal(m1, agent_meta={"agent_id": "agent-1"})
        await m1.close()
        m2 = CheckpointManager(path)
        state = await m2.load_checkpoint()
        assert state["agent_meta"]["agent_id"] == "agent-1"
        await m2.close()


class TestTrendingIntegration:
    async def test_checkpoint_restore_trending_still_works(self, tmp_path):
        """Doc 13 §7.2: baselines survive restart and trending resumes."""
        config = {
            "baseline": {"min_samples": 10, "window_samples": 20,
                         "critical_pause_samples": 3},
            "trending": {"min_samples": 10, "slope_threshold": 0.05,
                         "r_squared_min": 0.5, "max_projection_days": 90},
        }
        engine = TrendingEngine(config)
        for i in range(20):
            engine.update_baseline("fan:Fan1", 9000.0 - i, T0 + i * STEP, "OK")

        # T0 is a 2023 epoch, so disable the 30-day stale-baseline discard
        mgr = CheckpointManager(tmp_path / "checkpoint.db",
                                max_baseline_age_days=10000)
        await save_minimal(mgr, baselines=engine.get_all_baselines())
        await mgr.close()

        mgr2 = CheckpointManager(tmp_path / "checkpoint.db",
                                 max_baseline_age_days=10000)
        loaded = (await mgr2.load_checkpoint())["baselines"]
        await mgr2.close()

        engine2 = TrendingEngine(config)
        engine2.restore_baselines(loaded)
        rule = TrendingRule(
            field="speed_rpm",
            direction="declining",
            verdict=VerdictSeverity.TRENDING,
            message_template="Fan {name} declining at {rate} RPM/hr",
            threshold_field="threshold_low_critical",
        )
        results = engine2.compute_trend(
            "fan:Fan1",
            [rule],
            {"name": "Fan1", "speed_rpm": 8981.0, "threshold_low_critical": 480},
        )
        assert len(results) == 1
        assert results[0].slope == pytest.approx(-60.0, rel=1e-6)
        assert results[0].time_to_threshold_hours > 0
        assert math.isfinite(results[0].time_to_threshold_hours)
