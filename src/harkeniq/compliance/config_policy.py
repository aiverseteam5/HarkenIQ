"""Config compliance policies + drift detection (R4-2 P13).

A ConfigPolicy declares the expected values of BMC config attributes
for matching device types. Drift detection compares a collected config
snapshot (DeviceProtocol.collect_config()) against the policy:

  - attribute present, value differs   -> DRIFT finding
  - attribute absent from the snapshot -> UNKNOWN finding (never a
    false drift -- an empty snapshot means the protocol/vendor has no
    config surface, not that the device is misconfigured)

Policies are YAML files (one policy per file), loaded from a directory
like skill files:

    policy_id: bmc-baseline
    name: BMC baseline configuration
    description: NTP and syslog must stay enabled
    device_types: ["dell"]        # or ["*"]
    severity: WARNING             # WARNING | CRITICAL
    expected:
      NTPConfigGroup.1.NTPEnable: Enabled
      SysLog.1.SysLogEnable: Enabled

Remediation (build_remediation_playbook) turns DRIFT findings into a
single-step CONFIG_RESTORE playbook whose verification checks confirm
every attribute is back at its expected value. Per the R4 risk
register, config writes default to dry-run and always require approval.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger("harkeniq.compliance")

VALID_SEVERITIES = ("WARNING", "CRITICAL")

_POLICY_KEYS = {
    "policy_id", "name", "description", "device_types", "severity", "expected",
}


class ConfigPolicyError(Exception):
    """Invalid config policy file."""


@dataclass
class ConfigPolicy:
    """Expected BMC config attribute values for matching device types."""

    policy_id: str
    name: str
    description: str = ""
    device_types: list[str] = field(default_factory=lambda: ["*"])
    severity: str = "WARNING"
    expected: dict[str, Any] = field(default_factory=dict)

    def matches_device(self, vendor: str) -> bool:
        return "*" in self.device_types or vendor in self.device_types


@dataclass
class DriftFinding:
    """One attribute out of compliance (or unobservable)."""

    policy_id: str
    key: str
    expected: Any
    actual: Any
    severity: str
    status: str  # "DRIFT" | "UNKNOWN"


def parse_policy(data: dict, source: str = "<inline>") -> ConfigPolicy:
    """Validate and build a ConfigPolicy from a parsed YAML mapping."""
    if not isinstance(data, dict):
        raise ConfigPolicyError(f"{source}: policy must be a mapping")
    unknown = set(data) - _POLICY_KEYS
    if unknown:
        raise ConfigPolicyError(f"{source}: unknown keys {sorted(unknown)}")
    for required in ("policy_id", "name", "expected"):
        if not data.get(required):
            raise ConfigPolicyError(f"{source}: {required!r} is required")
    if not isinstance(data["expected"], dict):
        raise ConfigPolicyError(f"{source}: 'expected' must be a mapping")
    severity = data.get("severity", "WARNING")
    if severity not in VALID_SEVERITIES:
        raise ConfigPolicyError(
            f"{source}: severity must be one of {VALID_SEVERITIES}, got {severity!r}"
        )
    device_types = data.get("device_types") or ["*"]
    if not isinstance(device_types, list):
        raise ConfigPolicyError(f"{source}: 'device_types' must be a list")
    return ConfigPolicy(
        policy_id=str(data["policy_id"]),
        name=str(data["name"]),
        description=str(data.get("description", "")),
        device_types=[str(d) for d in device_types],
        severity=severity,
        expected=dict(data["expected"]),
    )


def load_config_policies(directory: str | Path) -> dict[str, ConfigPolicy]:
    """Load all *.yaml policies from a directory (missing dir -> {})."""
    path = Path(directory)
    if not path.is_dir():
        logger.info("No config policy directory at %s", path)
        return {}
    policies: dict[str, ConfigPolicy] = {}
    for file in sorted(path.glob("*.yaml")):
        try:
            data = yaml.safe_load(file.read_text()) or {}
            policy = parse_policy(data, source=file.name)
        except (yaml.YAMLError, ConfigPolicyError) as e:
            logger.error("Skipping invalid config policy %s: %s", file.name, e)
            continue
        if policy.policy_id in policies:
            logger.error(
                "Skipping %s: duplicate policy_id %r", file.name, policy.policy_id
            )
            continue
        policies[policy.policy_id] = policy
    return policies


def detect_drift(
    snapshot: dict[str, Any], policy: ConfigPolicy
) -> list[DriftFinding]:
    """Compare a config snapshot against one policy."""
    findings: list[DriftFinding] = []
    for key, expected in policy.expected.items():
        if key not in snapshot:
            findings.append(DriftFinding(
                policy_id=policy.policy_id, key=key, expected=expected,
                actual=None, severity=policy.severity, status="UNKNOWN",
            ))
        elif snapshot[key] != expected:
            findings.append(DriftFinding(
                policy_id=policy.policy_id, key=key, expected=expected,
                actual=snapshot[key], severity=policy.severity, status="DRIFT",
            ))
    return findings


def build_remediation_playbook(
    findings: list[DriftFinding], policy: ConfigPolicy
):
    """Build a CONFIG_DRIFT_REMEDIATION playbook for DRIFT findings.

    Returns None when no finding has status DRIFT (UNKNOWN attributes
    are unobservable, not remediable).
    """
    import json

    from harkeniq.actions.playbook import Playbook, PlaybookStep
    from harkeniq.autonomy.verification import VerificationCheck
    from harkeniq.models import ActionType

    drifted = [f for f in findings if f.status == "DRIFT"]
    if not drifted:
        return None
    attributes = {f.key: f.expected for f in drifted}
    verification = [
        VerificationCheck(
            description=f"{f.key} restored to {f.expected!r}",
            field_path=f.key,
            operator="equals",
            expected=f.expected,
        )
        for f in drifted
    ]
    step = PlaybookStep(
        step_index=0,
        action_type=ActionType.CONFIG_RESTORE,
        description=(
            f"Restore {len(drifted)} drifted attribute(s) for policy "
            f"{policy.policy_id}"
        ),
        params={"attributes_json": json.dumps(attributes, sort_keys=True)},
        verification_checks=verification,
        verification_wait_seconds=1.0,
    )
    return Playbook(
        playbook_id=f"config-drift-{policy.policy_id}",
        name=f"Config drift remediation ({policy.name})",
        description=policy.description or policy.name,
        device_types=list(policy.device_types),
        steps=[step],
        risk_level="medium",
    )
