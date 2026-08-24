"""Firmware update orchestration (R4-3 P19, OQ-21).

The highest-risk capability in the platform (bricked device = permanent
hardware loss), treated as a controlled lifecycle, never an ordinary
action:

  1. A campaign is CREATED against explicit devices; waves are planned
     so that no fault domain ever has more than one device updating in
     the same wave (the amendment's "never update >1 device per fault
     domain simultaneously"), with a hard cap on wave size.
  2. A human APPROVES the campaign (one approval, campaign level;
     audited on the R4-2 hash chain).
  3. Waves run STRICTLY sequentially. Wave N+1 starts only after every
     device in wave N completed and verified.
  4. On the FIRST device failure the device is rolled back blue-green
     (standby bank swap, OQ-21) and the campaign HALTS. It never
     auto-continues past a failure; a human inspects and creates a new
     campaign for the remainder.

Device-level updates run through a DeviceUpdater seam. Tests drive the
real Redfish path (ActionExecutor FIRMWARE_UPDATE/FIRMWARE_ROLLBACK
against the simulator's UpdateService); production wiring through the
agent decision-poll transport is the remaining integration step and is
deliberately NOT faked here -- an unconfigured orchestrator refuses to
advance rather than pretending to update hardware.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Protocol

from harkeniq_sm.db.models import utcnow
from harkeniq_sm.db.repos import (
    AuditRepo,
    DeviceRepo,
    DomainRepo,
    FirmwareCampaignRepo,
)

logger = logging.getLogger("harkeniq.sm.firmware")


@dataclass
class UpdateResult:
    success: bool
    pre_version: str = ""
    post_version: str = ""
    error: str = ""


class DeviceUpdater(Protocol):
    """Performs the actual per-device firmware update / rollback."""

    async def update(self, device, campaign) -> UpdateResult: ...

    async def rollback(self, device, campaign) -> bool: ...


def plan_waves(
    device_ids: list[str],
    domains_by_device: dict[str, list[str]],
    max_wave_size: int = 5,
) -> dict[str, int]:
    """Assign each device the earliest wave that keeps every fault
    domain at <= 1 device per wave and respects the wave-size cap.

    Deterministic: devices are processed in sorted order. Devices with
    no known fault domain are constrained only by the size cap.
    """
    wave_domains: list[set[str]] = []
    wave_sizes: list[int] = []
    assignment: dict[str, int] = {}
    for device_id in sorted(device_ids):
        domains = set(domains_by_device.get(device_id, []))
        wave = 0
        while True:
            if wave >= len(wave_domains):
                wave_domains.append(set())
                wave_sizes.append(0)
            if wave_sizes[wave] < max_wave_size and not (
                domains & wave_domains[wave]
            ):
                break
            wave += 1
        assignment[device_id] = wave
        wave_domains[wave] |= domains
        wave_sizes[wave] += 1
    return assignment


class FirmwareOrchestrator:
    """Campaign lifecycle driver. One SM, one site, explicit advances."""

    def __init__(self, sessionmaker, updater: Optional[DeviceUpdater] = None):
        self._sessionmaker = sessionmaker
        self.updater = updater

    async def create_campaign(
        self,
        site_id: str,
        device_ids: list[str],
        component: str,
        target_version: str,
        vendor: str = "",
        image_uri: str = "",
        image_sha256: str = "",
        created_by: str = "operator",
        max_wave_size: int = 5,
    ):
        """Plan waves and persist the campaign in draft state."""
        if not device_ids:
            raise ValueError("campaign needs at least one device")
        async with self._sessionmaker() as session:
            domain_repo = DomainRepo(session)
            domains_by_device: dict[str, list[str]] = {}
            for device_id in device_ids:
                domains = await domain_repo.domains_for_device(device_id)
                domains_by_device[device_id] = [d.id for d in domains]
            assignment = plan_waves(device_ids, domains_by_device, max_wave_size)

            repo = FirmwareCampaignRepo(session)
            campaign = await repo.create(
                site_id=site_id, component=component,
                target_version=target_version, vendor=vendor,
                image_uri=image_uri, image_sha256=image_sha256,
                created_by=created_by, max_wave_size=max_wave_size,
            )
            campaign.wave_count = max(assignment.values()) + 1
            device_repo = DeviceRepo(session)
            for device_id, wave in assignment.items():
                device = await device_repo.get(device_id)
                pre = ""
                for fw in (device.firmware or []) if device else []:
                    if fw.get("component") == component:
                        pre = str(fw.get("version", ""))
                await repo.add_target(campaign.id, device_id, wave, pre_version=pre)
            await AuditRepo(session).append(
                created_by, "firmware.campaign.create", campaign.id,
                detail={"component": component, "target_version": target_version,
                        "devices": len(device_ids),
                        "waves": campaign.wave_count},
            )
            await session.commit()
            logger.info(
                "Firmware campaign %s created: %d device(s) in %d wave(s)",
                campaign.id, len(device_ids), campaign.wave_count,
            )
            return campaign.id

    async def approve(self, campaign_id: str, actor: str) -> None:
        """Human sign-off; required before any device is touched."""
        async with self._sessionmaker() as session:
            repo = FirmwareCampaignRepo(session)
            campaign = await repo.get(campaign_id)
            if campaign is None:
                raise ValueError(f"campaign {campaign_id} not found")
            if campaign.status != "draft":
                raise ValueError(f"campaign is {campaign.status}, not draft")
            campaign.status = "approved"
            campaign.approved_by = actor
            campaign.approved_at = utcnow()
            await AuditRepo(session).append(
                actor, "firmware.campaign.approve", campaign_id,
                detail={"target_version": campaign.target_version},
            )
            await session.commit()

    async def advance(self, campaign_id: str) -> dict:
        """Run the current wave to completion (or halt on failure).

        Returns a summary dict. Explicitly operator/step-driven: each
        call processes at most one wave, so progress is observable and
        interruptible between waves.
        """
        if self.updater is None:
            raise RuntimeError(
                "no DeviceUpdater configured; refusing to advance a "
                "firmware campaign without a real update path"
            )
        async with self._sessionmaker() as session:
            repo = FirmwareCampaignRepo(session)
            campaign = await repo.get(campaign_id)
            if campaign is None:
                raise ValueError(f"campaign {campaign_id} not found")
            if campaign.status == "approved":
                campaign.status = "running"
                await session.commit()
            if campaign.status != "running":
                return {"status": campaign.status,
                        "detail": "campaign is not running"}

            wave = campaign.current_wave
            targets = await repo.targets(campaign_id, wave_index=wave)
            pending = [t for t in targets if t.status == "pending"]
            device_repo = DeviceRepo(session)
            audit = AuditRepo(session)
            await audit.append(
                "orchestrator", "firmware.wave.start", campaign_id,
                detail={"wave": wave, "devices": len(pending)},
            )
            await session.commit()

            for target in pending:
                device = await device_repo.get(target.device_id)
                result = await self.updater.update(device, campaign)
                if result.success:
                    await repo.update_target(
                        target, "completed", post_version=result.post_version,
                    )
                    await audit.append(
                        "orchestrator", "firmware.device.updated", campaign_id,
                        detail={"device_id": target.device_id,
                                "wave": wave,
                                "version": result.post_version},
                    )
                    await session.commit()
                    continue

                # OQ-21: blue-green rollback, then halt the campaign.
                rolled_back = await self.updater.rollback(device, campaign)
                await repo.update_target(
                    target,
                    "rolled_back" if rolled_back else "failed",
                    error=result.error,
                )
                campaign.status = "halted"
                campaign.halt_reason = (
                    f"device {target.device_id} failed in wave {wave}: "
                    f"{result.error}"
                )[:512]
                await audit.append(
                    "orchestrator", "firmware.campaign.halt", campaign_id,
                    detail={"device_id": target.device_id, "wave": wave,
                            "error": result.error,
                            "rolled_back": rolled_back},
                )
                await session.commit()
                logger.error("Campaign %s HALTED: %s",
                             campaign_id, campaign.halt_reason)
                return {"status": "halted", "wave": wave,
                        "halt_reason": campaign.halt_reason}

            # Wave complete -> next wave or done
            if wave + 1 >= campaign.wave_count:
                campaign.status = "completed"
                campaign.completed_at = utcnow()
                await audit.append(
                    "orchestrator", "firmware.campaign.complete", campaign_id,
                    detail={"waves": campaign.wave_count},
                )
            else:
                campaign.current_wave = wave + 1
            await session.commit()
            return {"status": campaign.status, "wave": wave,
                    "next_wave": campaign.current_wave}
