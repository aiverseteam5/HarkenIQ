"""Tests for PartitionFence isolation and fencing (R3b-2 Phase 6)."""

from __future__ import annotations

from harkeniq.autonomy.partition_fence import PartitionFence


class TestPartitionFence:
    def test_all_peers_lost_simultaneously(self):
        """All peers lost → ISOLATED."""
        fence = PartitionFence(my_agent_id="me")
        fence._prev_alive_count = 3  # was connected
        newly = fence.check_isolation(peers_alive=0, total_peers=3)
        assert newly is True
        assert fence.is_isolated is True

    def test_gradual_peer_loss_still_isolates(self):
        fence = PartitionFence(my_agent_id="me")
        fence.check_isolation(peers_alive=3, total_peers=3)
        fence.check_isolation(peers_alive=1, total_peers=3)
        assert fence.is_isolated is False
        fence.check_isolation(peers_alive=0, total_peers=3)
        assert fence.is_isolated is True


class TestIsolatedReportsOnSelf:
    def test_isolated_is_fenced(self):
        """R-AGENT-6: isolated node is fenced (cannot execute)."""
        fence = PartitionFence(my_agent_id="me")
        fence._prev_alive_count = 2
        fence.check_isolation(peers_alive=0, total_peers=2)
        assert fence.is_fenced(auth_lease_valid=True) is True

    def test_not_isolated_with_valid_lease(self):
        fence = PartitionFence(my_agent_id="me")
        fence.check_isolation(peers_alive=2, total_peers=3)
        assert fence.is_fenced(auth_lease_valid=True) is False


class TestFencedCannotAct:
    def test_expired_auth_lease_fenced(self):
        """Expired auth lease → fenced even if not isolated."""
        fence = PartitionFence(my_agent_id="me")
        fence.check_isolation(peers_alive=2, total_peers=3)
        assert fence.is_fenced(auth_lease_valid=False) is True

    def test_isolated_and_expired_both_fence(self):
        fence = PartitionFence(my_agent_id="me")
        fence._prev_alive_count = 2
        fence.check_isolation(peers_alive=0, total_peers=2)
        assert fence.is_fenced(auth_lease_valid=False) is True


class TestPartitionRecovery:
    def test_peers_return_lifts_fence(self):
        fence = PartitionFence(my_agent_id="me")
        fence._prev_alive_count = 2
        fence.check_isolation(peers_alive=0, total_peers=2)
        assert fence.is_isolated is True

        recovered = fence.check_recovery(peers_alive=1)
        assert recovered is True
        assert fence.is_isolated is False
        assert fence.is_fenced(auth_lease_valid=True) is False

    def test_no_recovery_if_not_isolated(self):
        fence = PartitionFence(my_agent_id="me")
        assert fence.check_recovery(peers_alive=2) is False


class TestGracefulDegradation:
    def test_no_peers_configured(self):
        """No peers = not isolated (standalone mode)."""
        fence = PartitionFence(my_agent_id="me")
        fence.check_isolation(peers_alive=0, total_peers=0)
        assert fence.is_isolated is False

    def test_fence_status(self):
        fence = PartitionFence(my_agent_id="me")
        fence._prev_alive_count = 3
        fence.check_isolation(peers_alive=0, total_peers=3)
        status = fence.fence_status()
        assert status["isolated"] is True
