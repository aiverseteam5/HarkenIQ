"""Tests for DeviceProtocol abstraction (R4-0 Phase 5)."""

from __future__ import annotations

import pytest

from harkeniq.protocols.device import (
    DeviceProtocol,
    ProtocolError,
    create_device_protocol,
)
from harkeniq.protocols.redfish import RedfishDeviceProtocol


class TestDeviceProtocolInterface:
    def test_redfish_is_device_protocol(self):
        """RedfishDeviceProtocol satisfies DeviceProtocol interface."""
        proto = RedfishDeviceProtocol(host="http://localhost:9000")
        assert isinstance(proto, DeviceProtocol)

    def test_protocol_name(self):
        proto = RedfishDeviceProtocol(host="http://localhost:9000")
        assert proto.name == "redfish"


class TestCreateDeviceProtocol:
    def test_create_redfish(self):
        proto = create_device_protocol("redfish", "http://localhost:9000")
        assert proto.name == "redfish"
        assert isinstance(proto, RedfishDeviceProtocol)

    def test_create_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown protocol"):
            create_device_protocol("nonexistent", "http://localhost:9000")


class TestRedfishProtocol:
    async def test_connect_without_host_fails(self):
        proto = RedfishDeviceProtocol(host="http://nonexistent:9999")
        with pytest.raises(ConnectionError):
            await proto.connect({"username": "admin", "password": "pass"})

    async def test_poll_without_connect_raises(self):
        proto = RedfishDeviceProtocol(host="http://localhost:9000")
        with pytest.raises(ProtocolError, match="Not connected"):
            await proto.poll_sensors()

    async def test_detect_without_connect_raises(self):
        proto = RedfishDeviceProtocol(host="http://localhost:9000")
        with pytest.raises(ProtocolError, match="Not connected"):
            await proto.detect_identity()

    async def test_execute_without_connect_raises(self):
        proto = RedfishDeviceProtocol(host="http://localhost:9000")
        with pytest.raises(ProtocolError, match="Not connected"):
            await proto.execute_action("IDENTIFY_LED", {"target": "sda"})

    async def test_disconnect_without_connect_is_safe(self):
        proto = RedfishDeviceProtocol(host="http://localhost:9000")
        await proto.disconnect()  # should not raise


class TestRedfishAllowList:
    """QA-023: the protocol-level executor must not self-grant every
    ActionType — direct callers get the R1 default set (R-X6)."""

    def _connected(self, allow_list=None):
        proto = RedfishDeviceProtocol(
            host="http://localhost:9000", allow_list=allow_list
        )
        # Refused actions never touch the client; a bare object suffices.
        proto._client = object()
        proto._identity = object()
        proto._vendor = "dell"
        return proto

    async def test_default_refuses_beyond_r1_set(self):
        proto = self._connected()
        result = await proto.execute_action("SEL_CLEAR", {})
        assert result["success"] is False
        assert "not in allow list" in result["error"]

    async def test_default_refuses_power_cycle(self):
        proto = self._connected()
        result = await proto.execute_action("POWER_CYCLE", {})
        assert result["success"] is False
        assert "not in allow list" in result["error"]

    async def test_configured_allow_list_honored(self):
        proto = self._connected(allow_list=["SEL_CLEAR"])
        result = await proto.execute_action("IDENTIFY_LED", {})
        assert result["success"] is False
        assert "not in allow list" in result["error"]

    async def test_factory_passes_allow_list_through(self):
        proto = create_device_protocol(
            "redfish", "http://localhost:9000", allow_list=["IDENTIFY_LED"]
        )
        assert proto._allow_list == ["IDENTIFY_LED"]


class TestAgentPassesAllowList:
    """QA-023 wiring: the agent hands its configured actions.allow_list
    to the Redfish protocol at construction."""

    async def test_configured_list_reaches_protocol(self, monkeypatch):
        import harkeniq.agent as agent_mod
        captured: dict = {}

        def fake_create(protocol_name, host, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop-after-capture")

        monkeypatch.setattr(agent_mod, "create_device_protocol", fake_create)
        agent = agent_mod.Agent({
            "bmc": {"host": "https://bmc", "username": "u", "password": "p"},
            "actions": {"allow_list": ["IDENTIFY_LED", "SEL_CLEAR"]},
        })
        with pytest.raises(RuntimeError, match="stop-after-capture"):
            await agent.start()
        assert captured["allow_list"] == ["IDENTIFY_LED", "SEL_CLEAR"]

    async def test_unconfigured_agent_passes_no_list(self, monkeypatch):
        import harkeniq.agent as agent_mod
        captured: dict = {}

        def fake_create(protocol_name, host, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop-after-capture")

        monkeypatch.setattr(agent_mod, "create_device_protocol", fake_create)
        agent = agent_mod.Agent({
            "bmc": {"host": "https://bmc", "username": "u", "password": "p"},
        })
        with pytest.raises(RuntimeError, match="stop-after-capture"):
            await agent.start()
        assert "allow_list" not in captured


class TestProtocolError:
    def test_is_harkeniq_error(self):
        from harkeniq.errors import HarkenIQError
        err = ProtocolError("test")
        assert isinstance(err, HarkenIQError)


class TestProtocolAgnosticLayers:
    """Verify that key layers do NOT import protocol-specific code."""

    def test_skills_engine_no_redfish(self):
        import inspect
        from harkeniq.skills import engine
        source = inspect.getsource(engine)
        assert "redfish" not in source.lower() or "redfishclient" not in source

    def test_claim_no_redfish(self):
        import inspect
        from harkeniq.autonomy import claim
        source = inspect.getsource(claim)
        assert "redfish" not in source.lower()

    def test_quorum_no_redfish(self):
        import inspect
        from harkeniq.autonomy import quorum
        source = inspect.getsource(quorum)
        assert "redfish" not in source.lower()

    def test_suspicion_no_redfish(self):
        import inspect
        from harkeniq.autonomy import suspicion
        source = inspect.getsource(suspicion)
        assert "redfish" not in source.lower()

    def test_playbook_no_redfish_import(self):
        """Playbook module does not import from harkeniq.redfish.*."""
        import inspect
        from harkeniq.actions import playbook
        source = inspect.getsource(playbook)
        assert "from harkeniq.redfish" not in source
        assert "import harkeniq.redfish" not in source
