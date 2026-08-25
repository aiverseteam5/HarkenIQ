"""Console configuration.

Sources (highest wins): environment (``HARKEN_CONSOLE_*``) > YAML file
(``HARKEN_CONSOLE_CONFIG``) > defaults. Same pattern as the Site Manager.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml


@dataclass
class ConsoleConfig:
    dsn: str = "sqlite+aiosqlite:///:memory:"
    http_host: str = "0.0.0.0"
    http_port: int = 8100
    keycloak_url: str = "http://localhost:8180"
    # Browser-facing issuer base (QA-005): in compose the browser reaches
    # Keycloak at localhost:8180 while services fetch JWKS via
    # keycloak:8080 — tokens carry the PUBLIC issuer. Empty = same as
    # keycloak_url (single-host deployments).
    keycloak_public_url: str = ""
    keycloak_admin_user: str = "admin"
    keycloak_admin_password: str = ""
    platform_realm: str = "harkeniq-platform"
    platform_client_id: str = "harkeniq-console"
    # QA-029: Central Command base URL. The SPA calls L3 surfaces (fleet,
    # approvals, policies, ...) against its own origin; the Console proxies
    # those prefixes to CC. Empty = proxy disabled (screens 404 as before).
    cc_url: str = ""
    # QA-035: shared CC<->Console credential; CC sends it as a bearer on
    # /api/internal calls (its HARKEN_CC_CONSOLE_API_KEY must match).
    internal_api_key: str = ""
    license_signing_key_path: str = ""
    license_verify_key_path: str = ""
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    webhook_base_url: str = ""
    support_email_from: str = "support@harkeniq.com"
    insecure: bool = False
    ui_dist: str = ""

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.keycloak_admin_password and not self.insecure:
            errors.append(
                "keycloak_admin_password is required (or set insecure for lab use)"
            )
        if not 0 < self.http_port < 65536:
            errors.append(f"http_port must be 1-65535, got {self.http_port}")
        return errors


_ENV_MAP = {
    "HARKEN_CONSOLE_DSN": "dsn",
    "HARKEN_CONSOLE_HTTP_HOST": "http_host",
    "HARKEN_CONSOLE_HTTP_PORT": "http_port",
    "HARKEN_CONSOLE_KEYCLOAK_URL": "keycloak_url",
    "HARKEN_CONSOLE_KEYCLOAK_PUBLIC_URL": "keycloak_public_url",
    "HARKEN_CONSOLE_KEYCLOAK_ADMIN_USER": "keycloak_admin_user",
    "HARKEN_CONSOLE_KEYCLOAK_ADMIN_PASSWORD": "keycloak_admin_password",
    "HARKEN_CONSOLE_PLATFORM_REALM": "platform_realm",
    "HARKEN_CONSOLE_PLATFORM_CLIENT_ID": "platform_client_id",
    "HARKEN_CONSOLE_CC_URL": "cc_url",
    "HARKEN_CONSOLE_INTERNAL_API_KEY": "internal_api_key",
    "HARKEN_CONSOLE_LICENSE_SIGNING_KEY_PATH": "license_signing_key_path",
    "HARKEN_CONSOLE_LICENSE_VERIFY_KEY_PATH": "license_verify_key_path",
    "HARKEN_CONSOLE_RAZORPAY_KEY_ID": "razorpay_key_id",
    "HARKEN_CONSOLE_RAZORPAY_KEY_SECRET": "razorpay_key_secret",
    "HARKEN_CONSOLE_STRIPE_SECRET_KEY": "stripe_secret_key",
    "HARKEN_CONSOLE_STRIPE_WEBHOOK_SECRET": "stripe_webhook_secret",
    "HARKEN_CONSOLE_WEBHOOK_BASE_URL": "webhook_base_url",
    "HARKEN_CONSOLE_SUPPORT_EMAIL_FROM": "support_email_from",
    "HARKEN_CONSOLE_INSECURE": "insecure",
    "HARKEN_CONSOLE_UI_DIST": "ui_dist",
}


def _coerce(raw: str, current: Any) -> Any:
    if isinstance(current, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int) and not isinstance(current, bool):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    return raw


def load_console_config(
    env: Optional[Mapping[str, str]] = None,
    yaml_path: Optional[str] = None,
) -> ConsoleConfig:
    """Build the effective Console config from defaults, YAML, then environment."""
    env_map = os.environ if env is None else env
    config = ConsoleConfig()

    path = yaml_path or env_map.get("HARKEN_CONSOLE_CONFIG", "")
    if path:
        data = yaml.safe_load(Path(path).read_text()) or {}
        for key, value in data.items():
            if hasattr(config, key):
                setattr(config, key, value)

    for var, attr in _ENV_MAP.items():
        if var in env_map:
            setattr(config, attr, _coerce(env_map[var], getattr(config, attr)))

    return config
