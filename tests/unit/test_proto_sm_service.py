"""SiteManagerService proto stubs: verify the generated messages exist."""

from harkeniq.proto import harkeniq_pb2, harkeniq_pb2_grpc


class TestServiceExists:
    def test_site_manager_service_exists(self):
        assert hasattr(harkeniq_pb2_grpc, "SiteManagerServiceServicer")
        assert hasattr(harkeniq_pb2_grpc, "SiteManagerServiceStub")
        assert hasattr(harkeniq_pb2_grpc, "add_SiteManagerServiceServicer_to_server")


class TestMessages:
    def test_register_site_message(self):
        msg = harkeniq_pb2.SiteRegistration(
            tenant_id="t1", site_id="s1", site_name="dc-blr",
            cc_endpoint="https://cc.lab:8090",
            license_key_fingerprint="abc123",
        )
        assert msg.tenant_id == "t1"
        assert msg.site_id == "s1"
        assert msg.site_name == "dc-blr"
        assert msg.cc_endpoint == "https://cc.lab:8090"
        assert msg.license_key_fingerprint == "abc123"

    def test_fleet_snapshot_message(self):
        device = harkeniq_pb2.FleetDevice(
            agent_id="a1", agent_name="srv-1",
            vendor="Dell", model="R750",
            observation="observed", health="ok",
        )
        incident = harkeniq_pb2.FleetIncident(
            incident_id="inc-1", kind="device",
            status="open", title="PSU failure",
        )
        action = harkeniq_pb2.FleetAction(
            action_id="act-1", type="fan_boost",
            device_agent_id="a1", status="pending",
        )
        snapshot = harkeniq_pb2.FleetSnapshot(
            devices=[device],
            incidents=[incident],
            pending_actions=[action],
            snapshot_at_unix=1724175600,
        )
        assert len(snapshot.devices) == 1
        assert len(snapshot.incidents) == 1
        assert len(snapshot.pending_actions) == 1

    def test_approval_route_message(self):
        msg = harkeniq_pb2.ApprovalRouteRequest(
            action_id="act-1", decision="approved",
            decided_by="op@lab", tenant_id="t1",
        )
        assert msg.decision == "approved"
        assert msg.decided_by == "op@lab"

    def test_usage_snapshot_message(self):
        msg = harkeniq_pb2.UsageSnapshot(
            tenant_id="t1", site_id="s1", date="2026-08-20",
            node_count=42, agent_versions={"0.1.0": 40, "0.2.0": 2},
        )
        assert msg.node_count == 42
        assert msg.agent_versions["0.1.0"] == 40
        assert msg.agent_versions["0.2.0"] == 2

    def test_policy_update_message(self):
        msg = harkeniq_pb2.PolicyUpdate(
            tenant_id="t1", site_id="s1",
            approval_policies_json='{"auto_approve": ["identify_led"]}',
            autonomy_budgets_json='{"fan_boost": 10}',
        )
        assert msg.approval_policies_json == '{"auto_approve": ["identify_led"]}'
        assert msg.autonomy_budgets_json == '{"fan_boost": 10}'
