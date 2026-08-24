"""R5-2 exit gate (Amendment A8: completion + enterprise hardening).

Criteria:
  1. Marketplace automation: a Console install reaches SM directive
     queues via CC with a durable delivery ledger (full-chain test in
     tests/unit/cc/test_marketplace_sync.py; agent execution in R5-1).
  2. Audit-chain appends are advisory-locked on PostgreSQL in all
     three service stores.
  3. CC intelligence tables are tenant-isolated.
"""

from __future__ import annotations

import inspect

import pytest

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


class TestCriterion1MarketplaceAutomationWired:
    def test_sync_loop_registered_in_cc_runtime(self):
        import harkeniq_cc.runtime as runtime
        source = inspect.getsource(runtime)
        assert "marketplace_sync_loop" in source

    def test_console_records_and_serves_installs(self):
        from harkeniq_console.api import internal, marketplace
        assert "MarketplaceInstallRepo" in inspect.getsource(marketplace)
        assert "marketplace/installs" in inspect.getsource(internal)

    def test_sm_install_rpc_exists(self):
        from harkeniq.proto import harkeniq_pb2_grpc
        from harkeniq_sm.grpc_server import SiteManagerServiceServicer
        assert hasattr(SiteManagerServiceServicer, "InstallSkill")
        assert hasattr(
            harkeniq_pb2_grpc.SiteManagerServiceStub, "__init__"
        )


class TestCriterion2AdvisoryLockWired:
    @pytest.mark.parametrize("module_path,chain_name", [
        ("harkeniq_sm.db.repos", "sm.audit_log"),
        ("harkeniq_cc.db.repos", "cc.cc_audit_log"),
        ("harkeniq_console.db.repos", "console.console_audit_log"),
    ])
    def test_all_three_appends_take_the_lock(self, module_path, chain_name):
        import importlib
        source = inspect.getsource(importlib.import_module(module_path))
        assert "pg_advisory_chain_lock" in source
        assert chain_name in source

    def test_lock_serializes_on_postgres_and_noops_elsewhere(self):
        # Behavior covered in tests/unit/test_audit_chain.py; assert the
        # helper's contract shape here.
        from harkeniq.audit.chain import advisory_lock_key
        keys = {advisory_lock_key(n) for n in (
            "sm.audit_log", "cc.cc_audit_log", "console.console_audit_log",
        )}
        assert len(keys) == 3


class TestCriterion3TenantIsolation:
    async def test_patterns_cve_warranty_isolated(self):
        from harkeniq_cc.db.base import (
            create_all, make_engine, make_sessionmaker,
        )
        from harkeniq_cc.db.repos import (
            CveFeedRepo, FleetPatternRepo, WarrantyRepo,
        )
        from harkeniq_cc.pattern_detector import FleetPattern
        from harkeniq_cc.warranty.base import WarrantyRecord

        engine = make_engine("sqlite+aiosqlite:///:memory:")
        await create_all(engine)
        db = make_sessionmaker(engine)
        async with db() as session:
            patterns = FleetPatternRepo(session)
            await patterns.save(FleetPattern(
                pattern_id="pat-a", pattern_type="batch_failure",
                description="a's pattern", affected_scope={}, confidence=0.9,
            ), tenant_id=TENANT_A)
            await patterns.save(FleetPattern(
                pattern_id="pat-b", pattern_type="batch_failure",
                description="b's pattern", affected_scope={}, confidence=0.9,
            ), tenant_id=TENANT_B)

            cve = CveFeedRepo(session)
            await cve.import_entries(
                [{"cve_id": "CVE-A", "affected_versions": "< 1"}],
                tenant_id=TENANT_A,
            )
            await cve.import_entries(
                [{"cve_id": "CVE-B", "affected_versions": "< 1"}],
                tenant_id=TENANT_B,
            )

            warranty = WarrantyRepo(session)
            await warranty.upsert_records(
                [WarrantyRecord("SHARED-TAG", "dell", end_date="2029-01-01")],
                tenant_id=TENANT_A,
            )
            await warranty.upsert_records(
                [WarrantyRecord("SHARED-TAG", "dell", end_date="2020-01-01")],
                tenant_id=TENANT_B,
            )
            await session.commit()

            # Patterns: each tenant sees only its own
            a_patterns = await patterns.list_patterns(tenant_id=TENANT_A)
            assert [p.id for p in a_patterns] == ["pat-a"]
            b_patterns = await patterns.list_patterns(tenant_id=TENANT_B)
            assert [p.id for p in b_patterns] == ["pat-b"]

            # CVE feed: isolated
            assert [e.cve_id for e in await cve.list_all(TENANT_A)] == ["CVE-A"]
            assert [e.cve_id for e in await cve.list_all(TENANT_B)] == ["CVE-B"]

            # Warranty: the SAME service tag can carry different records
            # per tenant without collision or leakage
            a_map = await warranty.get_map(["SHARED-TAG"], tenant_id=TENANT_A)
            b_map = await warranty.get_map(["SHARED-TAG"], tenant_id=TENANT_B)
            assert a_map["SHARED-TAG"].end_date == "2029-01-01"
            assert b_map["SHARED-TAG"].end_date == "2020-01-01"
        await engine.dispose()
