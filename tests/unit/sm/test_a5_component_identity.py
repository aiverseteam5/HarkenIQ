"""A5: the Site Manager stops discarding component identity (spec A22.4).

A verdict's sensor id is ``"<subsystem>:<component>"``. Ingest has always
split off the subsystem and thrown the remainder away, so an incident said
"this device's disks are unhealthy" and nothing downstream could say WHICH
disk -- which is why Central Command could never supply IDENTIFY_LED's
``target`` or INTERFACE_*'s ``interface``, and why A4 made those classes
addressable but unexecutable.

The negative cases matter most: an incident with no reported component must
come out EMPTY rather than plausible, because Central Command turns "no
component" into a refusal and "a component" into a real action on real
hardware.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from harkeniq_sm.db.models import Device, Site
from harkeniq_sm.db.repos import TelemetryRepo

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def device(db):
    async with db() as session:
        session.add(Site(id="site-a", name="site-1", cc_site_id="cc-a",
                         status="active"))
        await session.flush()
        session.add(Device(id="dev-1", site_id="site-a", agent_id="node-1",
                           agent_name="n1", device_class="server"))
        await session.commit()
    return "dev-1"


async def _verdict(db, device_id, sensor_id, severity, *, offset=0,
                   skill="disk-health"):
    async with db() as session:
        await TelemetryRepo(session).add_verdict(
            device_id, NOW + timedelta(seconds=offset), sensor_id, skill,
            severity, "m", None,
        )
        await session.commit()


class TestTheComponentSurvivesTheSubsystemSplit:
    async def test_it_names_the_component_not_just_the_subsystem(
        self, db, device
    ):
        await _verdict(db, device, "disk:Disk.Bay.3", "CRITICAL")
        async with db() as session:
            found = await TelemetryRepo(session).affected_components(
                device, "disk",
            )
        assert [c["component"] for c in found] == ["Disk.Bay.3"]
        assert found[0]["severity"] == "CRITICAL"

    async def test_a_healthy_component_is_not_affected(self, db, device):
        """OK verdicts are the steady state; they name nothing to act on."""
        await _verdict(db, device, "disk:Disk.Bay.3", "OK")
        await _verdict(db, device, "disk:Disk.Bay.4", "HEALTHY", offset=1)
        async with db() as session:
            assert await TelemetryRepo(session).affected_components(
                device, "disk",
            ) == []

    async def test_another_subsystem_is_never_borrowed(self, db, device):
        """A thermal fault must not hand a disk action a fan's identity."""
        await _verdict(db, device, "fan:Fan1", "CRITICAL", skill="fan-health")
        async with db() as session:
            repo = TelemetryRepo(session)
            assert await repo.affected_components(device, "disk") == []
            assert [c["component"] for c in
                    await repo.affected_components(device, "fan")] == ["Fan1"]

    async def test_another_device_is_never_borrowed(self, db, device):
        async with db() as session:
            session.add(Device(id="dev-2", site_id="site-a", agent_id="node-2",
                               agent_name="n2", device_class="server"))
            await session.commit()
        await _verdict(db, "dev-2", "disk:Disk.Bay.9", "CRITICAL")
        async with db() as session:
            assert await TelemetryRepo(session).affected_components(
                device, "disk",
            ) == []

    async def test_each_component_appears_once_at_its_newest_verdict(
        self, db, device
    ):
        await _verdict(db, device, "disk:Disk.Bay.3", "WARNING", offset=0)
        await _verdict(db, device, "disk:Disk.Bay.3", "CRITICAL", offset=10)
        async with db() as session:
            found = await TelemetryRepo(session).affected_components(
                device, "disk",
            )
        assert len(found) == 1
        assert found[0]["severity"] == "CRITICAL"

    async def test_several_failing_components_are_all_reported(self, db, device):
        """One action addresses one drive; the operator needs both."""
        await _verdict(db, device, "disk:Disk.Bay.3", "CRITICAL", offset=0)
        await _verdict(db, device, "disk:Disk.Bay.7", "WARNING", offset=1)
        async with db() as session:
            found = await TelemetryRepo(session).affected_components(
                device, "disk",
            )
        assert {c["component"] for c in found} == {"Disk.Bay.3", "Disk.Bay.7"}

    async def test_an_interface_port_resolves_the_same_way(self, db, device):
        """R6's gNMI actions need a port name and never had one at CC."""
        await _verdict(db, device, "interface:Ethernet4", "CRITICAL",
                       skill="interface-health")
        async with db() as session:
            found = await TelemetryRepo(session).affected_components(
                device, "interface",
            )
        assert [c["component"] for c in found] == ["Ethernet4"]

    async def test_an_unnamed_subsystem_asks_nothing(self, db, device):
        """A blank prefix would match every sensor on the device."""
        await _verdict(db, device, "disk:Disk.Bay.3", "CRITICAL")
        async with db() as session:
            assert await TelemetryRepo(session).affected_components(
                device, "",
            ) == []


class TestItRidesTheSnapshot:
    async def test_the_incident_carries_its_components_to_central_command(
        self, db, device
    ):
        """Empty stays empty: unknown at CC, never "nothing affected"."""
        from harkeniq_sm.config import SMConfig
        from harkeniq_sm.db.models import Incident
        from harkeniq_sm.directives import DirectiveService
        from harkeniq_sm.grpc_server import SiteManagerServiceServicer
        from harkeniq.proto import harkeniq_pb2
        import json

        await _verdict(db, device, "disk:Disk.Bay.3", "CRITICAL")
        async with db() as session:
            session.add(Incident(
                id="inc-1", site_id="site-a", kind="device", status="open",
                device_id=device, subsystem="disk", title="disk failing",
            ))
            session.add(Incident(
                id="inc-2", site_id="site-a", kind="device", status="open",
                device_id=device, subsystem="memory", title="memory failing",
            ))
            await session.commit()

        config = SMConfig(insecure=True, site_name="site-1")
        servicer = SiteManagerServiceServicer(
            db, None, config, DirectiveService(db, config),
        )
        snapshot = await servicer.GetFleetSnapshot(
            harkeniq_pb2.FleetSnapshotRequest(site_id="cc-a"), None,
        )
        by_id = {i.incident_id: i for i in snapshot.incidents}
        carried = json.loads(by_id["inc-1"].components_json)
        assert len(carried) == 1
        assert carried[0]["component"] == "Disk.Bay.3"
        assert carried[0]["severity"] == "CRITICAL"
        assert carried[0]["skill_name"] == "disk-health"
        assert carried[0]["at"]
        # No memory verdict was reported, so the field stays EMPTY and
        # Central Command reads unknown rather than "nothing affected".
        assert by_id["inc-2"].components_json == ""
