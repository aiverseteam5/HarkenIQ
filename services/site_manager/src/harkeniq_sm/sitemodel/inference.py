"""Fault-domain inference (A1.1): propose, never confirm.

Proposals are idempotent — deterministic names derived from the member
set mean a re-run upserts nothing new. Confidence stays at the
configured inferred level (0.6) until an operator confirms.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select

from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.models import VerdictReportRow
from harkeniq_sm.db.repos import DeviceRepo, DomainRepo, SiteRepo, StatusRepo
from harkeniq_sm.sitemodel.discovery import components, peer_graph


def _member_tag(agent_ids: list[str]) -> str:
    digest = hashlib.sha1("|".join(sorted(agent_ids)).encode()).hexdigest()
    return digest[:8]


class InferenceJob:
    def __init__(self, sessionmaker, config: SMConfig) -> None:
        self.sessionmaker = sessionmaker
        self.config = config

    async def run_once(self, now: Optional[datetime] = None) -> list[str]:
        """Propose inferred domains; returns names of newly created ones."""
        now = now or datetime.now(timezone.utc)
        created: list[str] = []
        async with self.sessionmaker() as session:
            site = await SiteRepo(session).get_or_create(self.config.site_name)
            devices = await DeviceRepo(session).list_for_site(site.id)
            domains = DomainRepo(session)

            # Member sets already claimed per kind: a proposal whose members
            # are covered by an existing same-kind domain (e.g. operator
            # confirmed or YAML imported) adds nothing — and a duplicate
            # power domain would split one shared fault into two parents.
            covered: dict[str, list[frozenset]] = {}
            for existing in await domains.list_for_site(site.id):
                covered.setdefault(existing.kind, []).append(
                    frozenset(await domains.members(existing.id))
                )

            async def propose(name: str, kind: str, member_ids: list[str]) -> None:
                if await domains.get_by_name(site.id, name) is not None:
                    return
                proposed = frozenset(member_ids)
                if any(proposed <= members for members in covered.get(kind, [])):
                    return
                domain = await domains.create(
                    site.id, name, kind,
                    status="inferred",
                    confidence=self.config.correlation.inferred_domain_confidence,
                    source="inference",
                )
                await domains.set_members(domain.id, member_ids, added_by="inference")
                covered.setdefault(kind, []).append(proposed)
                created.append(name)

            # Cooling: rack hints group ≥2 devices.
            racks: dict[str, list] = {}
            for device in devices:
                if device.rack_suggestion:
                    racks.setdefault(device.rack_suggestion, []).append(device)
            for rack, members in racks.items():
                if len(members) >= 2:
                    await propose(
                        f"cooling-{rack}", "cooling", [d.id for d in members]
                    )

            # Network: peer-adjacency connected components ≥2.
            statuses = {
                s.device_id: s for s in await StatusRepo(session).list_all()
            }
            for component in components(peer_graph(devices, statuses)):
                if len(component) >= 2:
                    member_devices = [d for d in devices if d.id in component]
                    tag = _member_tag([d.agent_id for d in member_devices])
                    await propose(f"network-{tag}", "network", list(component))

            # Power: co-onset of psu verdicts within the shared-power window.
            for group in await self._co_onset_groups(session, devices, now):
                tag = _member_tag([d.agent_id for d in group])
                await propose(f"power-{tag}", "power", [d.id for d in group])

            await session.commit()
        return created

    async def _co_onset_groups(self, session, devices, now: datetime) -> list[list]:
        """Devices whose psu faults arrived within one shared-power window."""
        window = timedelta(seconds=self.config.correlation.shared_power_window_s)
        lookback = now - timedelta(
            seconds=self.config.correlation.batch_component_window_s
        )
        rows = (
            await session.execute(
                select(VerdictReportRow.device_id, VerdictReportRow.time)
                .where(
                    VerdictReportRow.sensor_id.like("psu%"),
                    VerdictReportRow.severity.in_(("WARNING", "CRITICAL")),
                    VerdictReportRow.time >= lookback,
                )
                .order_by(VerdictReportRow.time)
            )
        ).all()
        by_id = {d.id: d for d in devices}
        groups: list[list] = []
        cluster: dict[str, datetime] = {}
        cluster_start: Optional[datetime] = None

        def close_cluster() -> None:
            if len(cluster) >= self.config.correlation.shared_power_min_devices:
                members = [by_id[i] for i in cluster if i in by_id]
                if len(members) >= self.config.correlation.shared_power_min_devices:
                    groups.append(members)

        for device_id, time in rows:
            if cluster_start is not None and time - cluster_start > window:
                close_cluster()
                cluster, cluster_start = {}, None
            if cluster_start is None:
                cluster_start = time
            cluster.setdefault(device_id, time)
        close_cluster()
        return groups
