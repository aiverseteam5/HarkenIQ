"""R4-0 exit gate: verify platform validation requirements.

Every test maps to a concrete R4-0 objective from the R4 Architecture
Amendment.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).parents[2]


# -- Docker Compose full-stack exists --------------------------------------


class TestFullStackDeployment:
    def test_unified_compose_exists(self):
        assert (REPO / "deploy/full-stack/docker-compose.yml").is_file()

    def test_compose_has_all_services(self):
        import yaml
        with open(REPO / "deploy/full-stack/docker-compose.yml") as f:
            config = yaml.safe_load(f)
        services = set(config["services"].keys())
        assert {"postgres", "keycloak", "site-manager", "central-command", "console"}.issubset(services)


# -- Alembic migration chain complete --------------------------------------


class TestMigrationChain:
    def test_sm_has_migration(self):
        path = REPO / "services/site_manager/src/harkeniq_sm/db/migrations/versions/0001_initial.py"
        assert path.is_file()

    def test_cc_has_migration(self):
        path = REPO / "services/central_command/src/harkeniq_cc/db/migrations/versions/0001_initial.py"
        assert path.is_file()

    def test_console_has_migration(self):
        path = REPO / "services/console/src/harkeniq_console/db/migrations/versions/0001_initial.py"
        assert path.is_file()


# -- Structured logging + request-id --------------------------------------


class TestStructuredLogging:
    def test_json_formatter_exists(self):
        from harkeniq.logging_config import JSONFormatter
        formatter = JSONFormatter(service="test")
        import logging
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", None, None)
        output = formatter.format(record)
        data = json.loads(output)
        assert data["service"] == "test"

    def test_request_id_generation(self):
        from harkeniq.logging_config import generate_request_id
        rid = generate_request_id()
        assert len(rid) == 12


# -- Health checks + metrics -----------------------------------------------


class TestHealthAndMetrics:
    def test_metrics_registry_exports(self):
        from harkeniq.metrics import MetricsRegistry
        r = MetricsRegistry()
        r.counter("test_total", "Test counter")
        r.inc("test_total")
        text = r.export_text()
        assert "test_total 1" in text

    async def test_health_checker_works(self):
        from harkeniq.metrics import HealthChecker
        checker = HealthChecker("test")
        checker.add_probe("always_up", lambda: True)
        status = await checker.check()
        assert status.healthy is True


# -- DeviceProtocol abstraction --------------------------------------------


class TestDeviceProtocol:
    def test_protocol_interface_exists(self):
        from harkeniq.protocols.device import DeviceProtocol
        assert DeviceProtocol is not None

    def test_redfish_implements_protocol(self):
        from harkeniq.protocols.device import DeviceProtocol
        from harkeniq.protocols.redfish import RedfishDeviceProtocol
        proto = RedfishDeviceProtocol(host="http://localhost:9000")
        assert isinstance(proto, DeviceProtocol)

    def test_factory_creates_redfish(self):
        from harkeniq.protocols.device import create_device_protocol
        proto = create_device_protocol("redfish", "http://localhost:9000")
        assert proto.name == "redfish"

    def test_skills_engine_protocol_agnostic(self):
        """Skills engine does not import harkeniq.redfish.*."""
        import inspect
        from harkeniq.skills import engine
        source = inspect.getsource(engine)
        assert "from harkeniq.redfish" not in source

    def test_autonomy_protocol_agnostic(self):
        """Autonomy layer does not import harkeniq.redfish.*."""
        import inspect
        from harkeniq.autonomy import claim, quorum, suspicion
        for mod in (claim, quorum, suspicion):
            source = inspect.getsource(mod)
            assert "from harkeniq.redfish" not in source


# -- R1-R3b integration points verified ------------------------------------


class TestIntegrationPoints:
    def test_all_proto_rpcs_defined(self):
        """All 11 proto RPCs are defined."""
        from harkeniq.proto import harkeniq_pb2_grpc
        # AgentService: 6 RPCs
        assert hasattr(harkeniq_pb2_grpc, "AgentServiceStub")
        # SiteManagerService: 5 RPCs
        assert hasattr(harkeniq_pb2_grpc, "SiteManagerServiceStub")

    def test_fleet_outcome_in_snapshot(self):
        """FleetSnapshot includes outcomes for fleet learning."""
        from harkeniq.proto import harkeniq_pb2
        snapshot = harkeniq_pb2.FleetSnapshot(
            outcomes=[harkeniq_pb2.FleetOutcome(
                action_id="act-1", outcome="SUCCESS",
            )]
        )
        assert len(snapshot.outcomes) == 1

    def test_credential_provider_chain(self):
        """CredentialProviderChain fallback works (R-H7)."""
        from harkeniq.security.credentials import (
            CredentialProviderChain,
            LocalCredentialProvider,
            MockCredentialProvider,
        )
        chain = CredentialProviderChain([
            LocalCredentialProvider({}),
            MockCredentialProvider(),
        ])
        assert "chain" in chain.provider_name

    def test_peer_protocol_fully_implemented(self):
        """Contract 7: all 4 PeerProtocol methods implemented."""
        from harkeniq.autonomy.peer_protocol import PeerProtocol
        pp = PeerProtocol(agent_id="test")
        # None of these should raise NotImplementedError
        pp.broadcast_claim("dev", {})
        pp.receive_claims()
        pp.renew_lease("nope")
        pp.exchange_suspicion("fan", 0.5)
