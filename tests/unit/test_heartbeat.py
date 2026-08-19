"""Heartbeat protocol + peer tracker tests (Doc 06 §9, Doc 12 §2)."""

import json

import pytest

from harkeniq.errors import HeartbeatError
from harkeniq.heartbeat.protocol import MAX_PACKET_BYTES, build_packet, parse_packet
from harkeniq.heartbeat.tracker import HEALTH_BUFFER_SECONDS, PeerTracker
from harkeniq.models import HeartbeatPacket, PeerStatus

SECRET = "site-shared-secret"

HEALTH = {"fan": "OK", "disk": "WARNING", "memory": "OK", "psu": "OK", "thermal": "OK"}


def make_packet(seq=1, ts=1000.0, health=None, **overrides):
    fields = dict(
        v=1,
        agent_id="agent-aaaa",
        name="rack-12-server-04",
        seq=seq,
        ts=ts,
        state="OBSERVING",
        health_summary=dict(health or HEALTH),
    )
    fields.update(overrides)
    return HeartbeatPacket(**fields)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class TestProtocolRoundTrip:
    def test_round_trip(self):
        data = build_packet(make_packet(), SECRET)
        parsed = parse_packet(data, SECRET)
        assert parsed.agent_id == "agent-aaaa"
        assert parsed.name == "rack-12-server-04"
        assert parsed.seq == 1
        assert parsed.ts == 1000.0
        assert parsed.state == "OBSERVING"
        assert parsed.health_summary == HEALTH
        assert parsed.v == 1

    def test_packet_is_compact_json_with_hmac(self):
        data = build_packet(make_packet(), SECRET)
        payload = json.loads(data.decode())
        assert len(payload["hmac"]) == 64  # sha256 hex
        assert len(data) <= MAX_PACKET_BYTES

    def test_oversized_packet_rejected_on_build(self):
        big_health = {f"subsystem_{i}": "OK" for i in range(40)}
        with pytest.raises(HeartbeatError, match="exceeds"):
            build_packet(make_packet(health=big_health), SECRET)

    def test_oversized_datagram_rejected_on_parse(self):
        with pytest.raises(HeartbeatError, match="too large"):
            parse_packet(b"x" * (MAX_PACKET_BYTES + 1), SECRET)


class TestProtocolRejection:
    def test_wrong_secret_rejected(self):
        data = build_packet(make_packet(), SECRET)
        with pytest.raises(HeartbeatError, match="HMAC"):
            parse_packet(data, "wrong-secret")

    def test_tampered_payload_rejected(self):
        data = build_packet(make_packet(), SECRET)
        payload = json.loads(data.decode())
        payload["state"] = "ACTING"  # tamper without re-signing
        tampered = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        with pytest.raises(HeartbeatError, match="HMAC"):
            parse_packet(tampered, SECRET)

    def test_missing_hmac_rejected(self):
        payload = json.loads(build_packet(make_packet(), SECRET).decode())
        del payload["hmac"]
        data = json.dumps(payload).encode()
        with pytest.raises(HeartbeatError, match="missing fields: hmac"):
            parse_packet(data, SECRET)

    def test_missing_field_rejected(self):
        payload = json.loads(build_packet(make_packet(), SECRET).decode())
        del payload["agent_id"]
        with pytest.raises(HeartbeatError, match="agent_id"):
            parse_packet(json.dumps(payload).encode(), SECRET)

    def test_malformed_json_rejected(self):
        with pytest.raises(HeartbeatError, match="Malformed"):
            parse_packet(b"not json{", SECRET)

    def test_non_object_json_rejected(self):
        with pytest.raises(HeartbeatError, match="JSON object"):
            parse_packet(b'["a","list"]', SECRET)

    def test_wrong_version_rejected(self):
        packet = make_packet(v=2)
        data = build_packet(packet, SECRET)
        with pytest.raises(HeartbeatError, match="version"):
            parse_packet(data, SECRET)


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


def make_tracker(interval=10, multiplier=3, hosts=("10.0.1.101",)):
    return PeerTracker({
        "heartbeat": {"interval": interval, "timeout_multiplier": multiplier,
                      "port": 5150, "secret": SECRET},
        "peers": [{"host": h, "port": 5150} for h in hosts],
    })


class TestTrackerLiveness:
    def test_configured_peers_start_unknown(self):
        tracker = make_tracker(hosts=("10.0.1.101", "10.0.1.102"))
        peers = tracker.get_peers()
        assert len(peers) == 2
        assert all(p.status == PeerStatus.UNKNOWN for p in peers)

    def test_first_heartbeat_unknown_to_alive(self):
        tracker = make_tracker()
        event = tracker.record_heartbeat(make_packet(), "10.0.1.101", now=1000.0)
        assert event is not None
        assert event.old_status == PeerStatus.UNKNOWN
        assert event.new_status == PeerStatus.ALIVE
        peer = tracker.get_peer("10.0.1.101")
        assert peer.status == PeerStatus.ALIVE
        assert peer.peer_id == "agent-aaaa"
        assert peer.name == "rack-12-server-04"
        assert peer.last_known_health == HEALTH

    def test_steady_heartbeats_no_event(self):
        tracker = make_tracker()
        tracker.record_heartbeat(make_packet(seq=1), "10.0.1.101", now=1000.0)
        assert tracker.record_heartbeat(make_packet(seq=2), "10.0.1.101", now=1010.0) is None

    def test_unresponsive_after_3_missed(self):
        tracker = make_tracker(interval=10, multiplier=3)
        tracker.record_heartbeat(make_packet(), "10.0.1.101", now=1000.0)
        # 30s window: still alive at exactly the boundary
        assert tracker.check_liveness(now=1030.0) == []
        events = tracker.check_liveness(now=1030.1)
        assert len(events) == 1
        assert events[0].new_status == PeerStatus.UNRESPONSIVE
        assert tracker.get_peer("10.0.1.101").status == PeerStatus.UNRESPONSIVE

    def test_unresponsive_event_fires_once(self):
        tracker = make_tracker()
        tracker.record_heartbeat(make_packet(), "10.0.1.101", now=1000.0)
        tracker.check_liveness(now=1031.0)
        assert tracker.check_liveness(now=1041.0) == []

    def test_recovery_after_unresponsive(self):
        tracker = make_tracker()
        tracker.record_heartbeat(make_packet(seq=1), "10.0.1.101", now=1000.0)
        tracker.check_liveness(now=1031.0)
        event = tracker.record_heartbeat(make_packet(seq=2), "10.0.1.101", now=1040.0)
        assert event.old_status == PeerStatus.UNRESPONSIVE
        assert event.new_status == PeerStatus.ALIVE

    def test_unknown_peer_never_becomes_unresponsive(self):
        tracker = make_tracker()
        assert tracker.check_liveness(now=99999.0) == []
        assert tracker.get_peer("10.0.1.101").status == PeerStatus.UNKNOWN

    def test_unconfigured_host_ignored(self):
        tracker = make_tracker()
        assert tracker.record_heartbeat(make_packet(), "192.168.9.9", now=1000.0) is None
        assert tracker.get_peer("192.168.9.9") is None

    def test_stale_seq_dropped(self):
        tracker = make_tracker()
        tracker.record_heartbeat(make_packet(seq=5, health=HEALTH), "10.0.1.101", now=1000.0)
        stale = make_packet(seq=4, health={"fan": "Critical"})
        assert tracker.record_heartbeat(stale, "10.0.1.101", now=1001.0) is None
        assert tracker.get_peer("10.0.1.101").last_known_health == HEALTH


class TestWitnessEvidence:
    def test_health_buffer_retains_recent_summaries(self):
        tracker = make_tracker()
        for i in range(4):
            health = dict(HEALTH, disk=f"state-{i}")
            tracker.record_heartbeat(
                make_packet(seq=i + 1, health=health), "10.0.1.101", now=1000.0 + i * 10
            )
        buffer = tracker.get_peer("10.0.1.101").health_buffer
        assert [h["disk"] for h in buffer] == ["state-0", "state-1", "state-2", "state-3"]

    def test_health_buffer_trims_beyond_60s(self):
        tracker = make_tracker()
        for i in range(10):
            tracker.record_heartbeat(
                make_packet(seq=i + 1, health=dict(HEALTH, disk=f"s{i}")),
                "10.0.1.101",
                now=1000.0 + i * 10,
            )
        buffer = tracker.get_peer("10.0.1.101").health_buffer
        # Only summaries within HEALTH_BUFFER_SECONDS of the newest remain
        assert len(buffer) <= int(HEALTH_BUFFER_SECONDS / 10) + 1
        assert buffer[-1]["disk"] == "s9"
        assert buffer[0]["disk"] == "s3"

    def test_evidence_survives_unresponsive_transition(self):
        tracker = make_tracker()
        tracker.record_heartbeat(
            make_packet(seq=1, health=dict(HEALTH, psu="Critical")), "10.0.1.101", now=1000.0
        )
        tracker.check_liveness(now=1031.0)
        peer = tracker.get_peer("10.0.1.101")
        assert peer.status == PeerStatus.UNRESPONSIVE
        assert peer.last_known_health["psu"] == "Critical"
        assert peer.health_buffer[-1]["psu"] == "Critical"


class TestRestore:
    def test_restore_downgrades_alive_to_unknown(self):
        tracker1 = make_tracker()
        tracker1.record_heartbeat(make_packet(), "10.0.1.101", now=1000.0)
        saved = tracker1.get_peers()
        assert saved[0].status == PeerStatus.ALIVE

        tracker2 = make_tracker()
        tracker2.restore_peers(saved)
        peer = tracker2.get_peer("10.0.1.101")
        assert peer.status == PeerStatus.UNKNOWN  # liveness unknown after restart
        assert peer.peer_id == "agent-aaaa"
        assert peer.last_known_health == HEALTH

    def test_restore_keeps_unresponsive(self):
        tracker1 = make_tracker()
        tracker1.record_heartbeat(make_packet(), "10.0.1.101", now=1000.0)
        tracker1.check_liveness(now=1031.0)

        tracker2 = make_tracker()
        tracker2.restore_peers(tracker1.get_peers())
        assert tracker2.get_peer("10.0.1.101").status == PeerStatus.UNRESPONSIVE

    def test_restore_ignores_unconfigured_hosts(self):
        tracker1 = make_tracker(hosts=("10.0.1.101", "10.0.1.102"))
        tracker1.record_heartbeat(make_packet(), "10.0.1.101", now=1000.0)

        tracker2 = make_tracker(hosts=("10.0.1.101",))
        tracker2.restore_peers(tracker1.get_peers())
        assert tracker2.get_peer("10.0.1.102") is None
        assert len(tracker2.get_peers()) == 1
