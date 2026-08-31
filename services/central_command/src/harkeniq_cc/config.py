"""Central Command configuration.

Sources (highest wins): environment (``HARKEN_CC_*``) > YAML file
(``HARKEN_CC_CONFIG``) > defaults. Kept deliberately simpler than the
agent's four-layer precedence: CC is a deployed service, not a
field-configured binary.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml


@dataclass
class CCConfig:
    tenant_id: str = ""
    dsn: str = "sqlite+aiosqlite:///:memory:"
    http_host: str = "0.0.0.0"
    http_port: int = 8090
    keycloak_url: str = "http://localhost:8180"
    # Browser-facing issuer base (QA-005); empty = same as keycloak_url.
    keycloak_public_url: str = ""
    keycloak_realm: str = ""
    #: E1.4: the PLATFORM realm's name, so Central Command can recognise
    #: being pointed at it. CC serves ONE tenant (spec §3) and must
    #: validate against that tenant's own realm; pinned to the platform
    #: realm it would accept vendor-staff identities as tenant operators,
    #: which is the boundary E1.4 exists to close.
    platform_realm: str = "harkeniq-platform"
    keycloak_client_id: str = "harkeniq-cc"
    console_url: str = ""
    # QA-018: CA bundle for TLS to Site Managers; empty = plaintext (lab).
    sm_tls_ca: str = ""
    console_api_key: str = ""
    license_key_path: str = ""
    # QA-019: Console-issued Ed25519 public key that verifies the license
    # file. Required whenever license_key_path is set (fail-closed).
    license_verify_key_path: str = ""
    usage_report_interval_s: float = 86400.0
    site_poll_interval_s: float = 300.0
    pattern_detect_interval_s: float = 300.0
    # R5-2: marketplace install sync (Console pull -> SM push)
    marketplace_sync_interval_s: float = 300.0
    # A1: Operational Agent evaluation cadence. Deliberately slower than
    # the fleet poll: an agent must reason over state the poller has
    # already refreshed, and proposing faster than the evidence changes
    # would only produce duplicates the dedupe key throws away.
    agent_evaluate_interval_s: float = 120.0
    #: S6: how often the campaign reconciler looks for an eligible wave.
    campaign_interval_s: float = 60.0
    # R4-2 P15: warranty enrichment (Dell TechDirect; empty = disabled)
    dell_api_client_id: str = ""
    dell_api_client_secret: str = ""
    warranty_refresh_interval_s: float = 86400.0
    warranty_ttl_s: float = 604800.0
    insecure: bool = False

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.tenant_id and not self.insecure:
            errors.append("tenant_id is required (or set insecure for lab use)")
        if not 0 < self.http_port < 65536:
            errors.append(f"http_port must be 1-65535, got {self.http_port}")
        # E1.4: Central Command serves ONE tenant and must validate against
        # that tenant's own realm. Pinned to the platform realm it accepts
        # vendor-staff identities carrying tenant roles as tenant
        # operators -- the exact boundary this slice closes. Refused at
        # startup rather than left as a live misconfiguration, because a
        # deployment that boots wrong looks identical to one that is right.
        if (
            not self.insecure
            and self.keycloak_realm
            and self.platform_realm
            and self.keycloak_realm == self.platform_realm
        ):
            errors.append(
                f"keycloak_realm is the PLATFORM realm ({self.platform_realm!r}): "
                "Central Command serves one tenant and must validate against "
                "that tenant's own realm, or a platform identity becomes a "
                "tenant operator"
            )
        return errors


_ENV_MAP = {
    "HARKEN_CC_TENANT_ID": "tenant_id",
    "HARKEN_CC_DSN": "dsn",
    "HARKEN_CC_HTTP_HOST": "http_host",
    "HARKEN_CC_HTTP_PORT": "http_port",
    "HARKEN_CC_KEYCLOAK_URL": "keycloak_url",
    "HARKEN_CC_KEYCLOAK_PUBLIC_URL": "keycloak_public_url",
    "HARKEN_CC_KEYCLOAK_REALM": "keycloak_realm",
    "HARKEN_CC_PLATFORM_REALM": "platform_realm",
    "HARKEN_CC_KEYCLOAK_CLIENT_ID": "keycloak_client_id",
    "HARKEN_CC_CONSOLE_URL": "console_url",
    "HARKEN_CC_SM_TLS_CA": "sm_tls_ca",
    "HARKEN_CC_CONSOLE_API_KEY": "console_api_key",
    "HARKEN_CC_LICENSE_KEY_PATH": "license_key_path",
    "HARKEN_CC_LICENSE_VERIFY_KEY_PATH": "license_verify_key_path",
    "HARKEN_CC_USAGE_REPORT_INTERVAL_S": "usage_report_interval_s",
    "HARKEN_CC_SITE_POLL_INTERVAL_S": "site_poll_interval_s",
    "HARKEN_CC_PATTERN_DETECT_INTERVAL_S": "pattern_detect_interval_s",
    "HARKEN_CC_MARKETPLACE_SYNC_INTERVAL_S": "marketplace_sync_interval_s",
    "HARKEN_CC_AGENT_EVALUATE_INTERVAL_S": "agent_evaluate_interval_s",
    "HARKEN_CC_CAMPAIGN_INTERVAL_S": "campaign_interval_s",
    "HARKEN_CC_DELL_API_CLIENT_ID": "dell_api_client_id",
    "HARKEN_CC_DELL_API_CLIENT_SECRET": "dell_api_client_secret",
    "HARKEN_CC_WARRANTY_REFRESH_INTERVAL_S": "warranty_refresh_interval_s",
    "HARKEN_CC_WARRANTY_TTL_S": "warranty_ttl_s",
    "HARKEN_CC_INSECURE": "insecure",
}


def _coerce(raw: str, current: Any) -> Any:
    if isinstance(current, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int) and not isinstance(current, bool):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    return raw


def load_cc_config(
    env: Optional[Mapping[str, str]] = None,
    yaml_path: Optional[str] = None,
) -> CCConfig:
    """Build the effective CC config from defaults, YAML, then environment."""
    env_map = os.environ if env is None else env
    config = CCConfig()

    path = yaml_path or env_map.get("HARKEN_CC_CONFIG", "")
    if path:
        data = yaml.safe_load(Path(path).read_text()) or {}
        for key, value in data.items():
            if hasattr(config, key):
                setattr(config, key, value)

    for var, attr in _ENV_MAP.items():
        if var in env_map:
            setattr(config, attr, _coerce(env_map[var], getattr(config, attr)))

    return config
