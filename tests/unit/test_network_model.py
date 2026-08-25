"""R6-P1 unit tests: NetworkDevice model, model relocation shim, seams.

Covers (design doc §7, R6-P1 named tests):
- model round-trip for NormalizedInterface
- health rollup including interfaces (admin-down excluded)
- skill-engine evaluation against an interface reading
- shim test: every pre-move import path still resolves to the same objects
- loader: interface target valid; raw *_total counter fields rejected
"""

import dataclasses

import pytest

from harkeniq.models import VerdictSeverity
from harkeniq.protocols.model import (
    DeviceIdentity,
    HealthRollup,
    NormalizedDevice,
    NormalizedInterface,
    compute_health_rollup,
)
from harkeniq.skills.engine import SkillEngine
from harkeniq.skills.loader import parse_skill


def make_interface(**overrides) -> NormalizedInterface:
    data = {
        "name": "Ethernet0",
        "admin_state": "Up",
        "oper_state": "Up",
        "speed_mbps": 100000,
        "health": "OK",
        "in_error_rate": 0.0,
        "in_octet_rate": 1000.0,
    }
    data.update(overrides)
    return NormalizedInterface(**data)


def make_interface_skill(**overrides):
    data = {
        "name": "interface-errors",
        "version": 1,
        "target": "interface",
        "description": "test",
        "rules": [
            {
                "condition": "in_error_rate > 10",
                "verdict": "CRITICAL",
                "message": "Port {name} error rate high",
            }
        ],
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class TestNormalizedInterface:
    def test_round_trip(self):
        iface = make_interface(pre_fec_ber=1e-9, lag_name="PortChannel1")
        restored = NormalizedInterface(**dataclasses.asdict(iface))
        assert restored == iface

    def test_unobservable_defaults_are_none_not_zero(self):
        # None = "platform does not export this" (P0: vs has no CRC/optics).
        iface = NormalizedInterface(name="Ethernet0")
        assert iface.crc_error_rate is None
        assert iface.pre_fec_ber is None
        assert iface.optics_rx_power_dbm is None
        assert iface.queue_occupancy_max_pct is None

    def test_device_class_defaults_to_server(self):
        # Pre-R6 behavior preserved: everything is a server unless a
        # protocol says otherwise.
        assert DeviceIdentity().device_class == "server"
        assert NormalizedDevice().interfaces == []


class TestHealthRollup:
    def test_interface_health_in_rollup_and_overall(self):
        device = NormalizedDevice(
            interfaces=[
                make_interface(name="Ethernet0", health="OK"),
                make_interface(name="Ethernet4", health="Critical"),
            ]
        )
        rollup = compute_health_rollup(device)
        assert rollup.interface == "Critical"
        assert rollup.overall == "Critical"

    def test_admin_down_port_is_not_a_fault(self):
        # Operator shutdown mirrors the Absent-DIMM exclusion.
        device = NormalizedDevice(
            interfaces=[
                make_interface(name="Ethernet0", health="OK"),
                make_interface(
                    name="Ethernet4", health="Critical", admin_state="Down"
                ),
            ]
        )
        rollup = compute_health_rollup(device)
        assert rollup.interface == "OK"
        assert rollup.overall == "OK"

    def test_server_rollup_unchanged_by_empty_interfaces(self):
        # Regression guard: servers carry an empty list; overall must not
        # degrade to Unknown because of it.
        from harkeniq.protocols.model import NormalizedFan

        device = NormalizedDevice(fans=[NormalizedFan(name="Fan1", health="OK")])
        rollup = compute_health_rollup(device)
        assert rollup.interface == "Unknown"
        assert rollup.overall == "OK"

    def test_rollup_has_interface_field(self):
        assert HealthRollup().interface == "Unknown"


# ---------------------------------------------------------------------------
# Skill engine
# ---------------------------------------------------------------------------


def _primed_engine(skill):
    """Engine with baseline confidence 1.0 for interface:Ethernet0.

    Expression evaluation is confidence-gated (Doc 13 §2.3): a sensor with
    no baseline runs in learning mode (health pass-through). Prime one
    sample with min_samples=1 so the expression path is exercised.
    """
    from harkeniq.skills.trending import TrendingEngine

    trending = TrendingEngine({"baseline": {"min_samples": 1}})
    trending.update_baseline("interface:Ethernet0", 0.0, 0.0, "OK")
    permissive = {"critical": [1, 1], "warning": [1, 1], "recovery": [1, 1]}
    return SkillEngine([skill], permissive, trending_engine=trending)


class TestInterfaceSkillEvaluation:
    @pytest.mark.asyncio
    async def test_interface_skill_fires_on_error_rate(self):
        engine = _primed_engine(parse_skill(make_interface_skill()))
        device = NormalizedDevice(
            interfaces=[make_interface(name="Ethernet0", in_error_rate=50.0)]
        )
        verdicts = await engine.evaluate(device)
        critical = [v for v in verdicts if v.severity == VerdictSeverity.CRITICAL]
        assert len(critical) == 1
        assert "Ethernet0" in critical[0].message

    @pytest.mark.asyncio
    async def test_interface_skill_healthy_below_threshold(self):
        engine = _primed_engine(parse_skill(make_interface_skill()))
        device = NormalizedDevice(
            interfaces=[make_interface(name="Ethernet0", in_error_rate=0.1)]
        )
        verdicts = await engine.evaluate(device)
        assert all(v.severity != VerdictSeverity.CRITICAL for v in verdicts)

    @pytest.mark.asyncio
    async def test_unprimed_interface_runs_learning_passthrough(self):
        # No baseline -> learning mode: BMC/NOS health pass-through, no
        # expression firing even at a wild error rate. This is the A2.3
        # confidence gate working, not a gap.
        engine = SkillEngine([parse_skill(make_interface_skill())])
        device = NormalizedDevice(
            interfaces=[make_interface(name="Ethernet0", in_error_rate=999.0)]
        )
        verdicts = await engine.evaluate(device)
        assert len(verdicts) == 1
        assert verdicts[0].severity == VerdictSeverity.HEALTHY
        assert "learning" in verdicts[0].message


# ---------------------------------------------------------------------------
# Loader validation
# ---------------------------------------------------------------------------


class TestInterfaceTargetValidation:
    def test_interface_is_a_valid_target(self):
        skill = parse_skill(make_interface_skill())
        assert skill.target == "interface"

    def test_rate_fields_accepted(self):
        skill = parse_skill(make_interface_skill(rules=[{
            "condition": "crc_error_rate > 1 and optics_rx_power_dbm < -10",
            "verdict": "WARNING",
            "message": "Port {name} degrading",
        }]))
        assert skill.rules[0].verdict == VerdictSeverity.WARNING

    def test_raw_total_fields_rejected(self):
        # Deliberate: skills must never condition on monotonic totals
        # (design doc §7 decision 7) — only derived rates.
        from harkeniq.errors import SkillValidationError

        with pytest.raises(SkillValidationError):
            parse_skill(make_interface_skill(rules=[{
                "condition": "in_errors_total > 100",
                "verdict": "CRITICAL",
                "message": "bad",
            }]))


# ---------------------------------------------------------------------------
# Relocation shim
# ---------------------------------------------------------------------------


class TestModelRelocationShim:
    def test_every_pre_move_import_path_resolves(self):
        # The R5 import surface of harkeniq.redfish.normalize, verbatim.
        from harkeniq.redfish.normalize import (  # noqa: F401
            DeviceIdentity as ShimIdentity,
            HealthRollup as ShimRollup,
            NormalizedDevice as ShimDevice,
            NormalizedDisk,
            NormalizedFan,
            NormalizedLogEntry,
            NormalizedMemory,
            NormalizedPSU,
            NormalizedPowerMetrics,
            NormalizedThermal,
            compute_health_rollup as shim_rollup_fn,
            worst_health as shim_worst,
        )

        # Same objects, not copies — isinstance checks across the codebase
        # must keep working regardless of which path constructed the value.
        import harkeniq.protocols.model as model

        assert ShimIdentity is model.DeviceIdentity
        assert ShimDevice is model.NormalizedDevice
        assert ShimRollup is model.HealthRollup
        assert shim_rollup_fn is model.compute_health_rollup
        assert shim_worst is model.worst_health

    def test_new_name_also_available_via_shim(self):
        from harkeniq.redfish.normalize import NormalizedInterface as ShimIface

        assert ShimIface is NormalizedInterface
