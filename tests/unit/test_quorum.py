"""Tests for QuorumEngine four-way disambiguation (R3b-2 Phase 5)."""

from __future__ import annotations

import pytest

from harkeniq.autonomy.quorum import QuorumEngine
from harkeniq.models import QuorumVerdict


def _engine():
    return QuorumEngine(my_agent_id="agent-me")


class TestQuorumDeviceDown:
    def test_all_peers_lost_suspect(self):
        """Case 1: All neighbours lost the device → DEVICE_DOWN."""
        engine = _engine()
        verdict = engine.disambiguate(
            suspect_device_id="device-x",
            peer_views={
                "peer-a": {"can_reach_suspect": False, "is_alive": True},
                "peer-b": {"can_reach_suspect": False, "is_alive": True},
            },
            my_peers_alive=2,
            total_peers=2,
        )
        assert verdict == QuorumVerdict.DEVICE_DOWN

    def test_three_peers_all_lost(self):
        engine = _engine()
        verdict = engine.disambiguate(
            suspect_device_id="device-x",
            peer_views={
                "a": {"can_reach_suspect": False, "is_alive": True},
                "b": {"can_reach_suspect": False, "is_alive": True},
                "c": {"can_reach_suspect": False, "is_alive": True},
            },
            my_peers_alive=3,
            total_peers=3,
        )
        assert verdict == QuorumVerdict.DEVICE_DOWN


class TestQuorumLinkDown:
    def test_peers_still_reach_suspect(self):
        """Case 2: Others still reach the device → LINK_DOWN."""
        engine = _engine()
        verdict = engine.disambiguate(
            suspect_device_id="device-x",
            peer_views={
                "peer-a": {"can_reach_suspect": True, "is_alive": True},
                "peer-b": {"can_reach_suspect": True, "is_alive": True},
            },
            my_peers_alive=2,
            total_peers=2,
        )
        assert verdict == QuorumVerdict.LINK_DOWN

    def test_majority_reach_suspect(self):
        engine = _engine()
        verdict = engine.disambiguate(
            suspect_device_id="device-x",
            peer_views={
                "a": {"can_reach_suspect": True, "is_alive": True},
                "b": {"can_reach_suspect": True, "is_alive": True},
                "c": {"can_reach_suspect": False, "is_alive": True},
            },
            my_peers_alive=3,
            total_peers=3,
        )
        assert verdict == QuorumVerdict.LINK_DOWN


class TestQuorumNodeFailed:
    def test_link_up_agent_silent(self):
        """Case 3: Link up, node silent → NODE_FAILED."""
        engine = _engine()
        verdict = engine.check_node_failed(
            suspect_device_id="device-x",
            link_up=True,
            agent_responding=False,
        )
        assert verdict == QuorumVerdict.NODE_FAILED

    def test_link_up_agent_responding(self):
        engine = _engine()
        verdict = engine.check_node_failed(
            suspect_device_id="device-x",
            link_up=True,
            agent_responding=True,
        )
        assert verdict is None  # not node-failed


class TestQuorumIsolated:
    def test_lost_all_peers(self):
        """Case 4: Lost every neighbour → ISOLATED (R-AGENT-6)."""
        engine = _engine()
        verdict = engine.disambiguate(
            suspect_device_id="device-x",
            peer_views={
                "a": {"can_reach_suspect": False, "is_alive": False},
                "b": {"can_reach_suspect": False, "is_alive": False},
            },
            my_peers_alive=0,
            total_peers=2,
        )
        assert verdict == QuorumVerdict.ISOLATED

    def test_isolated_reports_on_self(self):
        """R-AGENT-6: isolated node reports on itself."""
        engine = _engine()
        verdict = engine.disambiguate(
            suspect_device_id="device-x",
            peer_views={},
            my_peers_alive=0,
            total_peers=3,
        )
        assert verdict == QuorumVerdict.ISOLATED


class TestQuorumInsufficientObservers:
    def test_no_peers_configured(self):
        engine = _engine()
        verdict = engine.disambiguate(
            suspect_device_id="device-x",
            peer_views={},
            my_peers_alive=0,
            total_peers=0,
        )
        assert verdict == QuorumVerdict.INCONCLUSIVE

    def test_single_unreachable_peer(self):
        """R-M13: single-node evidence where corroboration obtainable."""
        engine = _engine()
        verdict = engine.disambiguate(
            suspect_device_id="device-x",
            peer_views={
                "a": {"can_reach_suspect": False, "is_alive": False},
            },
            my_peers_alive=0,
            total_peers=1,
        )
        # All peers lost → ISOLATED takes precedence
        assert verdict == QuorumVerdict.ISOLATED


class TestQuorumRequiresTwoObservers:
    def test_one_alive_peer_only(self):
        """R-M14: liveness conclusions need 2+ observers.
        With 1 alive peer + ourselves = 2 observers → quorum possible."""
        engine = _engine()
        verdict = engine.disambiguate(
            suspect_device_id="device-x",
            peer_views={
                "a": {"can_reach_suspect": False, "is_alive": True},
            },
            my_peers_alive=1,
            total_peers=1,
        )
        # 2 observers (us + peer-a), both lost suspect → DEVICE_DOWN
        assert verdict == QuorumVerdict.DEVICE_DOWN
