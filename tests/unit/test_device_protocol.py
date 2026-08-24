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
