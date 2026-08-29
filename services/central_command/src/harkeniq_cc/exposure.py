"""CVE exposure matching: devices' firmware inventories vs the local feed.

Extracted from `api/firmware.py` in S2 (2026-08-29) because it now has two
consumers — the exposure endpoint and the attention capability — and the
product rule is one capability, one implementation, many consumers. The
feed stays local and operator-imported (air-gap safe); matching is on
demand, using the shared cross-vendor comparator.
"""

from __future__ import annotations

from harkeniq.compliance.versions import version_in_range


def match_exposures(devices, entries) -> list[dict]:
    """Match devices' firmware inventories against CVE feed entries.

    A feed entry's vendor/component may be "*" (applies to all). Devices
    with no firmware inventory contribute nothing rather than matching
    everything — absence of inventory is not evidence of exposure.
    """
    exposures: list[dict] = []
    for dev in devices:
        for fw in (dev.firmware or []):
            component = str(fw.get("component", ""))
            version = str(fw.get("version", ""))
            if not version:
                continue
            for entry in entries:
                if entry.vendor not in ("*", dev.vendor):
                    continue
                if entry.component not in ("*", component):
                    continue
                if version_in_range(version, entry.affected_versions):
                    exposures.append({
                        "agent_id": dev.agent_id,
                        "agent_name": dev.agent_name,
                        "site_id": dev.site_id,
                        "vendor": dev.vendor,
                        "model": dev.model,
                        "component": component,
                        "component_name": str(fw.get("name", "")),
                        "version": version,
                        "cve_id": entry.cve_id,
                        "severity": entry.severity,
                        "description": entry.description,
                        "fixed_version": entry.fixed_version,
                    })
    return exposures
