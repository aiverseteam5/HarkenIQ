"""Site Manager configuration.

Sources (highest wins): environment (``HARKEN_SM_*``) > YAML file
(``HARKEN_SM_CONFIG``) > defaults. Kept deliberately simpler than the
agent's four-layer precedence: the SM is a deployed service, not a
field-configured binary.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml


@dataclass
class CorrelationThresholds:
    """Windows/quorums for the §1 boundary-table rules (SM receive time)."""

    shared_power_min_devices: int = 2
    shared_power_window_s: float = 120.0
    rack_thermal_min_devices: int = 2
    rack_thermal_window_s: float = 300.0
    batch_component_min_devices: int = 3
    batch_component_window_s: float = 86400.0
    ambiguity_peer_fresh_s: float = 120.0
    sweeper_interval_s: float = 10.0
    inferred_domain_confidence: float = 0.6


@dataclass
class SMConfig:
    site_name: str = "site-1"
    dsn: str = "sqlite+aiosqlite:///:memory:"
    http_host: str = "0.0.0.0"
    http_port: int = 8080
    grpc_host: str = "0.0.0.0"
    grpc_port: int = 50051
    site_token: str = ""
    tls_cert: str = ""
    tls_key: str = ""
    insecure: bool = False  # plaintext gRPC; unit tests / lab only
    heartbeat_interval_s: float = 60.0  # agents' expected SM heartbeat cadence
    stale_after_s: float = 150.0        # 2.5x heartbeat interval
    unobserved_after_s: float = 600.0
    inference_interval_s: float = 300.0
    ui_dist: str = ""                   # path to built dashboard, optional
    # R3b-1 C1: LLM Explain
    llm_enabled: bool = False
    llm_api_url: str = ""              # e.g. "https://api.anthropic.com"
    llm_api_key: str = ""              # env: HARKEN_SM_LLM_API_KEY
    llm_model: str = ""                # e.g. "claude-sonnet-4-20250514"
    llm_timeout_s: float = 30.0
    llm_max_tokens: int = 1024
    # R4-3 P18: air-gapped LLM model integrity (OQ-18). When llm_model_path
    # is set, SM verifies the file's SHA-256 against llm_model_sha256 at
    # startup and disables the LLM (NullLLMProvider) on mismatch/missing --
    # a corrupted model must never serve. Model updates are explicit
    # operator actions (copy file, restart SM); no auto-download.
    llm_model_path: str = ""
    llm_model_sha256: str = ""
    # R5: directed-directive transport (firmware campaigns wait on the
    # agent to poll, execute, and report -- allow for slow BMC flashes)
    directive_timeout_s: float = 900.0
    directive_poll_interval_s: float = 2.0
    correlation: CorrelationThresholds = field(default_factory=CorrelationThresholds)

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.site_token and not self.insecure:
            errors.append("site_token is required (or set insecure for lab use)")
        if not self.insecure and not (self.tls_cert and self.tls_key):
            errors.append("tls_cert and tls_key are required unless insecure is set")
        if not 0 < self.http_port < 65536:
            errors.append(f"http_port must be 1-65535, got {self.http_port}")
        if not 0 < self.grpc_port < 65536:
            errors.append(f"grpc_port must be 1-65535, got {self.grpc_port}")
        if self.stale_after_s >= self.unobserved_after_s:
            errors.append("stale_after_s must be < unobserved_after_s")
        return errors


_ENV_MAP = {
    "HARKEN_SM_SITE_NAME": "site_name",
    "HARKEN_SM_DSN": "dsn",
    "HARKEN_SM_HTTP_HOST": "http_host",
    "HARKEN_SM_HTTP_PORT": "http_port",
    "HARKEN_SM_GRPC_HOST": "grpc_host",
    "HARKEN_SM_GRPC_PORT": "grpc_port",
    "HARKEN_SM_SITE_TOKEN": "site_token",
    "HARKEN_SM_TLS_CERT": "tls_cert",
    "HARKEN_SM_TLS_KEY": "tls_key",
    "HARKEN_SM_INSECURE": "insecure",
    "HARKEN_SM_UI_DIST": "ui_dist",
    "HARKEN_SM_LLM_ENABLED": "llm_enabled",
    "HARKEN_SM_LLM_API_URL": "llm_api_url",
    "HARKEN_SM_LLM_API_KEY": "llm_api_key",
    "HARKEN_SM_LLM_MODEL": "llm_model",
    "HARKEN_SM_LLM_MODEL_PATH": "llm_model_path",
    "HARKEN_SM_LLM_MODEL_SHA256": "llm_model_sha256",
    "HARKEN_SM_DIRECTIVE_TIMEOUT_S": "directive_timeout_s",
    "HARKEN_SM_DIRECTIVE_POLL_INTERVAL_S": "directive_poll_interval_s",
}


def _coerce(raw: str, current: Any) -> Any:
    if isinstance(current, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int) and not isinstance(current, bool):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    return raw


def load_sm_config(
    env: Optional[Mapping[str, str]] = None,
    yaml_path: Optional[str] = None,
) -> SMConfig:
    """Build the effective SM config from defaults, YAML, then environment."""
    env_map = os.environ if env is None else env
    config = SMConfig()

    path = yaml_path or env_map.get("HARKEN_SM_CONFIG", "")
    if path:
        data = yaml.safe_load(Path(path).read_text()) or {}
        corr = data.pop("correlation", None) or {}
        for key, value in data.items():
            if hasattr(config, key) and key != "correlation":
                setattr(config, key, value)
        for key, value in corr.items():
            if hasattr(config.correlation, key):
                setattr(config.correlation, key, value)

    for var, attr in _ENV_MAP.items():
        if var in env_map:
            setattr(config, attr, _coerce(env_map[var], getattr(config, attr)))

    for f in fields(CorrelationThresholds):
        var = f"HARKEN_SM_CORR_{f.name}".upper()
        if var in env_map:
            setattr(
                config.correlation, f.name,
                _coerce(env_map[var], getattr(config.correlation, f.name)),
            )

    return config
