"""Site-model YAML round-trip (A1.1: "dashboard or YAML").

Export renders the current model; import upserts it. Imported fault
domains are operator statements of record: status=confirmed,
confidence=1.0, source=yaml_import. Devices referenced before their
agents register become placeholder rows keyed by agent_id.
"""

from __future__ import annotations

import yaml

from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.repos import (
    AuditRepo,
    DeviceRepo,
    DomainRepo,
    RackRepo,
    SiteRepo,
)


class SiteYaml:
    def __init__(self, sessionmaker, config: SMConfig) -> None:
        self.sessionmaker = sessionmaker
        self.config = config

    async def export(self) -> str:
        async with self.sessionmaker() as session:
            site = await SiteRepo(session).get_or_create(self.config.site_name)
            racks = {r.id: r for r in await RackRepo(session).list_for_site(site.id)}
            devices = await DeviceRepo(session).list_for_site(site.id)
            domains_repo = DomainRepo(session)
            domains = await domains_repo.list_for_site(site.id)
            by_id = {d.id: d for d in devices}

            doc = {
                "site": site.name,
                "racks": [
                    {"name": r.name, **({"row": r.row_label} if r.row_label else {})}
                    for r in racks.values()
                ],
                "devices": [
                    {
                        "agent_id": d.agent_id,
                        **({"name": d.agent_name} if d.agent_name else {}),
                        **(
                            {"rack": racks[d.rack_id].name}
                            if d.rack_id and d.rack_id in racks
                            else {}
                        ),
                    }
                    for d in devices
                ],
                "fault_domains": [
                    {
                        "name": dom.name,
                        "kind": dom.kind,
                        "status": dom.status,
                        "members": sorted(
                            by_id[m].agent_id
                            for m in await domains_repo.members(dom.id)
                            if m in by_id
                        ),
                    }
                    for dom in domains
                ],
            }
            await session.commit()
        return yaml.safe_dump(doc, sort_keys=False)

    async def import_yaml(self, text: str, actor: str = "yaml") -> dict:
        """Apply a YAML document; returns counters for the API response."""
        doc = yaml.safe_load(text) or {}
        counters = {"racks": 0, "devices": 0, "domains": 0}
        async with self.sessionmaker() as session:
            site = await SiteRepo(session).get_or_create(self.config.site_name)
            rack_repo = RackRepo(session)
            device_repo = DeviceRepo(session)
            domain_repo = DomainRepo(session)

            racks_by_name = {}
            for entry in doc.get("racks") or []:
                rack = await rack_repo.get_or_create(site.id, entry["name"])
                if entry.get("row"):
                    rack.row_label = entry["row"]
                racks_by_name[rack.name] = rack
                counters["racks"] += 1

            for entry in doc.get("devices") or []:
                device = await device_repo.upsert_registration(
                    site_id=site.id,
                    agent_id=entry["agent_id"],
                    agent_name=entry.get("name", ""),
                )
                rack_name = entry.get("rack")
                if rack_name:
                    if rack_name not in racks_by_name:
                        racks_by_name[rack_name] = await rack_repo.get_or_create(
                            site.id, rack_name
                        )
                    device.rack_id = racks_by_name[rack_name].id
                counters["devices"] += 1

            for entry in doc.get("fault_domains") or []:
                domain = await domain_repo.get_by_name(site.id, entry["name"])
                if domain is None:
                    domain = await domain_repo.create(
                        site.id, entry["name"], entry["kind"]
                    )
                domain.kind = entry.get("kind", domain.kind)
                domain.source = "yaml_import"
                await domain_repo.confirm(domain, by=actor)
                member_ids = []
                for agent_id in entry.get("members") or []:
                    device = await device_repo.upsert_registration(
                        site_id=site.id, agent_id=agent_id
                    )
                    member_ids.append(device.id)
                await domain_repo.set_members(domain.id, member_ids, added_by=actor)
                counters["domains"] += 1

            await AuditRepo(session).append(
                actor, "site.yaml_import", site.name, counters
            )
            await session.commit()
        return counters
